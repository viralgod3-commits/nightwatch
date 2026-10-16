"""Prism order book: a fresh, price-first view of the existing market snapshots.

Only presentation lives here. The analyzer, tick aggregation, lossless snapshot
transport and leased pixel buffers remain the application's existing pipeline.
Prices never animate; short, worker-owned transitions affect depth bars only.
"""
from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal

from ..models import (
    BOOK_BBO_FRESH_SECONDS, BOOK_DEPTH_FRESH_SECONDS,
    ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowSnapshot,
    OrderFlowPresentationFrame, book_data_is_fresh, safe_float,
)
from ..presentation import DisplayRefreshObserver, display_frame_interval_ms
from ..theme import orderbook_palette
from ..utilities import (
    TextRole, TYPOGRAPHY_DEFAULTS, load_app_fonts, set_text_role,
    typography_controller, typography_font,
)
from .formatting import format_book_price
from .ipc import SnapshotDecoder, SnapshotSeedRequired
from .orderbook_ui import (
    TradesTapeWidget, _DomRasterProcess, _decimal_places_from_step,
    _map_dom_frame,
)
from .raster_regions import bounded_region, draw_native_region
from .revisions import amount_inputs, amount_revision_key
from .tape_source import SharedTradeTapeSource

_ALIGN = Qt.AlignmentFlag


def _snapshot_from(payload):
    frame = payload[0] if isinstance(payload, tuple) and len(payload) == 2 else payload
    return frame.snapshot if isinstance(frame, OrderFlowPresentationFrame) else frame


def _assets(symbol: str) -> tuple[str, str]:
    for quote in ('USDT', 'USDC', 'FDUSD', 'BTC', 'ETH'):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)], quote
    return symbol, 'QUOTE'


def _amount(value: float) -> str:
    """Compact sizes; retain small positive depth instead of printing zero."""
    if not math.isfinite(value) or value < 0:
        return '—'
    for scale, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if value >= scale:
            return f'{value / scale:.2f}'.rstrip('0').rstrip('.') + suffix
    if value >= 100:
        return f'{value:.1f}'.rstrip('0').rstrip('.')
    if value >= .01:
        return f'{value:.3f}'.rstrip('0').rstrip('.')
    return f'{value:.3g}' if 0 < value < .001 else format_book_price(value)


def _multiplier(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return 1
    return value if value in ORDER_FLOW_AGGREGATION_MULTIPLIERS else 1


def _mix(first, second, amount):
    a, b = QtGui.QColor(first), QtGui.QColor(second)
    return QtGui.QColor(*(round(x * (1 - amount) + y * amount)
                         for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


def _deadline(snapshot):
    if snapshot is None or not snapshot.ready or not snapshot.live:
        return 0.0
    if not book_data_is_fresh(snapshot.depth_age_seconds, snapshot.bbo_age_seconds):
        return 0.0
    return snapshot.generated_monotonic + min(
        BOOK_DEPTH_FRESH_SECONDS - snapshot.depth_age_seconds,
        BOOK_BBO_FRESH_SECONDS - snapshot.bbo_age_seconds,
    )


@dataclass(frozen=True, slots=True)
class _DepthLevel:
    price: float
    quantity: float
    notional: float
    complete: bool = True


class _DepthAggregation:
    """Group only displayed depth; leave temporal analytics in the analyzer."""

    def __init__(self):
        self.key = None
        self.levels = ((), ())
        self.reused = 0

    def prepare(self, snapshot, multiplier, tick, epoch):
        if not snapshot.ready:
            self.key = None
            self.levels = ((), ())
            return
        depth = snapshot.display_depth
        if depth is not None and depth.multiplier == multiplier and depth.tick_size == tick:
            key = ('display', epoch, snapshot.symbol, depth)
            if key == self.key:
                self.reused += 1
                return
            self.levels = tuple(tuple(_DepthLevel(*row) for row in side)
                                for side in (depth.asks, depth.bids))
            self.key = key
            return
        if (multiplier <= 1 or multiplier not in ORDER_FLOW_AGGREGATION_MULTIPLIERS
                or tick <= 0 or not math.isfinite(tick)):
            self.key = None
            self.levels = (snapshot.ask_levels, snapshot.bid_levels)
            return
        amounts = amount_revision_key(snapshot)
        if amounts is None:
            amounts = amount_inputs(snapshot)
        key = (epoch, snapshot.symbol, multiplier, tick, amounts)
        if key == self.key:
            self.reused += 1
            return

        def group(levels, *, bid):
            buckets = {}
            for level in levels:
                native = int(round(level.price / tick))
                bucket = (native if bid else native + multiplier - 1) // multiplier * multiplier
                members = buckets.get(bucket)
                if members is None:
                    members = buckets[bucket] = []
                members.append(level)
            # Preserve source order and actual quote sums, not bucket-price * size.
            return tuple(_DepthLevel(price * tick,
                sum(max(0., level.quantity) for level in members),
                sum(max(0., level.notional) for level in members))
                for price, members in buckets.items())

        self.levels = (group(snapshot.ask_levels, bid=False), group(snapshot.bid_levels, bid=True))
        self.key = key


@dataclass(frozen=True, slots=True)
class _Row:
    side: str
    price: float
    price_text: str
    size_text: str
    total_text: str
    amount: float
    ratio: float
    top: int
    touch: bool

    @property
    def key(self):
        return self.side, self.price


def _geometry(width, height, density, price_width, totals):
    """Symmetric depth around a fixed midpoint; exact prices take precedence."""
    margin = 12 if width >= 300 else 8
    price_width = min(max(96, price_width + 24), max(96, width - 100))
    left = (width - price_width) / 2
    right = left + price_width
    row_height = 20 if density == 'compact' else 24
    # Grow whitespace gradually. A larger panel must never suddenly show fewer
    # levels because it crossed a typography/density breakpoint.
    space = min(1., max(0., (height-340)/320))
    header = labels = 0
    center, footer = (round(a+(b-a)*space)
        for a, b in ((54, 62), (28, 34)))
    available = max(0, height - header - labels - center - footer)
    count = min(120, int(available // (2 * row_height)))
    # Spare height stays in the header/footer, never stretches rows on a feed update.
    gap = available - count * 2 * row_height
    top = header + labels + int(gap // 2)
    center_top = top + count * row_height
    center_bottom = center_top + center
    return dict(width=width, height=height, margin=margin, price_left=left,
                price_right=right, row_height=row_height, rows=count,
                table_top=top, center_top=center_top, center_bottom=center_bottom,
                footer_top=height-footer, totals=bool(totals and left-margin >= 155),
                header=header, labels=labels, center_height=center, footer_height=footer)


def _fit_depth_geometry(geometry, ask_rows, bid_rows, *, grouped):
    """Fit known buckets without treating the source-depth horizon as zero depth.

    Keep ``rows`` as the requested capacity, independent of how many buckets
    arrived. Otherwise sparse grouping would reduce the next depth request and
    progressively starve the view. Only the drawn/hit-tested bands are fitted.
    """
    g = dict(geometry)
    g['ask_rows'], g['bid_rows'] = ask_rows, bid_rows
    capacity = g['rows']
    if grouped and 0 < max(ask_rows, bid_rows) < capacity:
        band_height = capacity * g['row_height']
        # Modest extra breathing room, not giant rows for one coarse bucket.
        g['row_height'] = min(40, band_height // max(ask_rows, bid_rows))
    g['table_top'] = g['center_top'] - ask_rows * g['row_height']
    g['table_bottom'] = g['center_bottom'] + bid_rows * g['row_height']
    g['depth_limited'] = bool(grouped and capacity and
                              (ask_rows < capacity or bid_rows < capacity))
    return g


class _PrismRenderer(QtWidgets.QWidget):
    """Offscreen painter. No model resets, child row widgets or GUI-thread scans."""

    def __init__(self, theme):
        super().__init__()
        self._raster_dpr = 1.0
        self._paint_target = None
        self._dirty_pixels = QtGui.QRegion()
        self.snapshot = None
        self.source = None
        self._last_execution = None
        self.config = {}
        self.rows = ()
        self.geometry = {}
        self.book_valid = True
        self.trade_active = True
        self.status = 'SYNCING'
        self._aggregation = _DepthAggregation()
        self._text_cache = OrderedDict()
        self._bar_from = {}
        self._bar_to = {}
        self._animation_start = 0.0
        self._animation_end = 0.0
        self._last_chrome = None
        self._last_layout = None
        self.frames = 0
        self.last_render_ms = self.max_render_ms = 0.0
        self.apply_theme(theme)
        self._fonts()

    def devicePixelRatioF(self):
        return self._raster_dpr

    def apply_theme(self, theme):
        p = orderbook_palette(theme)
        self.palette_values = p
        self._bg = QtGui.QColor(p['bg'])
        self.ink = QtGui.QColor(p['text'])
        self.muted = QtGui.QColor(p['muted'])
        self.line = _mix(p['bg'], p['text'], .12)
        self.surface = _mix(p['bg'], p['text'], .035)
        self.spine = _mix(p['bg'], p['mid'], .045)
        self.accent = QtGui.QColor(p['mid'])
        self.side_colors = {s: QtGui.QColor(p[s]) for s in ('bid', 'ask')}
        self.fills = {s: _mix(p['bg'], p[s], .48) for s in ('bid', 'ask')}
        self.tracks = {s: _mix(p['bg'], p[s], .055) for s in ('bid', 'ask')}
        self._last_chrome = self._last_layout = None

    def _fonts(self):
        self.label_font = typography_font(TextRole.ORDERBOOK_LABEL)
        self.number_font = typography_font(TextRole.ORDERBOOK_VALUE)
        self.price_font = typography_font(TextRole.ORDERBOOK_PRICE)
        # A deliberate readable default while honoring custom font-family profiles.
        if self.price_font.pixelSize() == 11:
            self.price_font.setPixelSize(13)
        self.hero_font = typography_font(TextRole.ORDERBOOK_CENTER_PRICE)
        self.metrics = QtGui.QFontMetricsF(self.price_font)
        self._text_cache.clear()
        self._last_layout = None

    def configure(self, config):
        old = self.config
        if old.get('symbol') != config['symbol'] or old.get('market_epoch') != config['market_epoch']:
            self.snapshot = self.source = None
            self.rows = ()
            self._bar_from.clear()
            self._bar_to.clear()
            self._aggregation = _DepthAggregation()
            self.book_valid = True
            self.trade_active = True
            self._last_chrome = None
        if old.get('theme') != config['theme']:
            self.apply_theme(config['theme'])
        if old.get('typography') != config['typography']:
            profiles, globals_ = config['typography']
            typography_controller().configure(profiles, globals_)
            self._fonts()
        self.book_valid = config.get('book_valid', self.book_valid)
        self.trade_active = config.get('trade_active', self.trade_active)
        self.config = config
        self._raster_dpr = config['dpr']
        self.resize(*config['size'])
        if any(old.get(k) != v for k, v in config.items() if k != 'interval'):
            self._last_layout = None
        if self.source is not None:
            self.adopt(self.source)

    def adopt(self, source):
        self.source = self.snapshot = source
        self._last_execution = source.recent_prints[-1] if source.recent_prints else None
        self._aggregation.prepare(source, self.config['aggregation'], self.config['tick'],
                                  self.config['market_epoch'])

    def _price(self, price, *, native=False):
        if not math.isfinite(price) or price <= 0:
            return '—'
        tick = self.config.get('tick', 0)
        precision = (_decimal_places_from_step(tick, 1 if native else self.config['aggregation'])
                     if tick > 0 else None)
        text = format_book_price(price, precision)
        whole, dot, fraction = text.partition('.')
        return f'{int(whole):,}' + (dot + fraction if dot else '')

    def _bar_values(self, now):
        if now >= self._animation_end or self._animation_end <= self._animation_start:
            return self._bar_to
        fraction = min(1., max(0., (now-self._animation_start) / (self._animation_end-self._animation_start)))
        eased = 1 - (1-fraction)**3
        return {key: self._bar_from.get(key, target) + (target-self._bar_from.get(key, target))*eased
                for key, target in self._bar_to.items()}

    def prepare(self, now):
        snapshot = self.snapshot
        live = self.book_valid and _deadline(self.source) > now
        self.status = ('LIVE' if self.trade_active else 'TRADES OFF') if live else (
            'STALE' if snapshot is not None and snapshot.ready else 'SYNCING')
        levels = self._aggregation.levels
        # Stable price envelope prevents lane widths jittering when digits change.
        examples = [self._price(l.price) for side in levels for l in side[:1]]
        price_width = max((self.metrics.horizontalAdvance('8' * len(t)) for t in examples), default=84)
        g = _geometry(self.width(), self.height(), self.config.get('density'), price_width,
                      self.config.get('totals', True))
        count = g['rows']
        visible = [tuple(level for level in side[:count]
                         if math.isfinite(level.price) and level.price > 0
                         and math.isfinite(level.quantity) and level.quantity >= 0
                         and math.isfinite(level.notional) and level.notional >= 0) for side in levels]
        if snapshot is not None and snapshot.ready and any(visible):
            g = _fit_depth_geometry(g, len(visible[0]), len(visible[1]),
                                    grouped=self.config.get('aggregation', 1) > 1)
        else:
            g.update(ask_rows=0, bid_rows=0, table_bottom=g['center_bottom'], depth_limited=False)
        layout = (tuple(g.items()), self.config.get('values'), self.config.get('tick'),
                  self.config.get('aggregation'), self.config.get('symbol'), self._raster_dpr)
        full = self._last_layout != layout
        self.geometry = g
        quote = self.config.get('values') == 'quote'
        value = lambda l: l.notional if quote else l.quantity
        scale = max((value(l) for side in visible for l in side), default=0.)
        side_totals = [sum(value(l) for l in side) for side in visible]
        prepared = []
        for side_index, side in enumerate(visible):
            cumulative = 0.
            cumulative_complete = True
            for index, level in enumerate(side):
                amount = value(level)
                cumulative += amount
                complete = getattr(level, 'complete', True)
                cumulative_complete = cumulative_complete and complete
                top = (g['center_top']-(index+1)*g['row_height'] if side_index == 0
                       else g['center_bottom']+index*g['row_height'])
                prepared.append(_Row('ask' if side_index == 0 else 'bid', level.price,
                    self._price(level.price), ('' if complete else '≥') + _amount(amount),
                    ('' if cumulative_complete else '≥') + _amount(cumulative), amount,
                    amount / scale if scale else 0., top, index == 0))
        previous = {row.key: row for row in self.rows}
        previous_bars = self._bar_values(now)
        new_targets = {row.key: row.ratio for row in prepared}
        if new_targets != self._bar_to:
            # Animate only when price membership and geometry are stable.
            # A moved best price switches immediately to the new price/size mapping.
            animate = (not full and live and self.config.get('motion', True)
                       and set(new_targets) == set(self._bar_to))
            self._bar_from = {key: previous_bars.get(key, target) for key, target in new_targets.items()}
            self._bar_to = new_targets
            self._animation_start = now
            self._animation_end = now + .10 if animate else now
        self.rows = tuple(prepared)
        if full:
            self._dirty_pixels = QtGui.QRegion(self.rect())
        else:
            new_rows = {row.key: row for row in self.rows}
            for key in previous.keys() | new_rows.keys():
                before, after = previous.get(key), new_rows.get(key)
                if before != after:
                    for row in (before, after):
                        if row is not None:
                            self._dirty_pixels += QtCore.QRect(0, row.top, self.width(), g['row_height'])
        if now < self._animation_end:
            for row in self.rows:
                if self._bar_from.get(row.key) != self._bar_to.get(row.key):
                    self._dirty_pixels += QtCore.QRect(0, row.top, self.width(), g['row_height'])
        # The final animation frame must paint the exact target too.
        elif getattr(self, '_was_animating', False):
            for row in self.rows:
                self._dirty_pixels += QtCore.QRect(0, row.top, self.width(), g['row_height'])
        self._was_animating = now < self._animation_end
        self.side_totals = side_totals
        self.side_complete = tuple(all(getattr(level, 'complete', True) for level in side) for side in visible)
        self._display_live = live
        chrome = (self.status, snapshot.best_bid if snapshot else 0,
                  snapshot.best_ask if snapshot else 0, self._last_trade(now),
                  self._trade_delta(),
                  tuple(side_totals), self.side_complete, len(visible[0]), len(visible[1]))
        if chrome != self._last_chrome:
            for top, height in ((0, g['header']), (g['center_top'], g['center_height']), (g['footer_top'], g['footer_height'])):
                self._dirty_pixels += QtCore.QRect(0, top, self.width(), height)
            if self._last_chrome is not None and self._last_chrome[0] != self.status:
                self._dirty_pixels = QtGui.QRegion(self.rect())
        self._last_chrome, self._last_layout = chrome, layout

    def _last_trade(self, now):
        snapshot = self.source
        if snapshot is not None:
            trade = self._last_execution
            elapsed = max(0., now - snapshot.generated_monotonic)
            if (self.trade_active and snapshot.trade_age_seconds is not None
                    and snapshot.trade_age_seconds + elapsed <= 5.0 and trade is not None):
                if math.isfinite(trade.price) and trade.price > 0:
                    return 'LAST TRADE', trade.price, {'sell': 'ask', 'buy': 'bid'}.get(str(trade.aggressor_side).lower(), '')
            return 'MID PRICE', snapshot.midpoint, ''
        return 'MID PRICE', 0., ''

    def _text(self, painter, rect, text, color, font=None, align=_ALIGN.AlignRight, *, exact=False):
        if not text or rect.width() <= 1 or rect.height() <= 1:
            return
        font = font or self.number_font
        # Cache measurement, fitting and shaping together. The hot path avoids
        # allocating QFont/QFontMetrics or recalculating glyph advances per row.
        key = (id(font), text, int(rect.width()), exact)
        entry = self._text_cache.get(key)
        if entry is None:
            metrics = QtGui.QFontMetricsF(font)
            width = metrics.horizontalAdvance(text)
            if exact and width > rect.width():
                font = QtGui.QFont(font)
                font.setStretch(max(1, int((font.stretch() or 100) * rect.width() / width)))
            elif width > rect.width():
                text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
            cached = QtGui.QStaticText(text)
            cached.setTextFormat(Qt.TextFormat.PlainText)
            cached.setPerformanceHint(QtGui.QStaticText.PerformanceHint.AggressiveCaching)
            cached.prepare(QtGui.QTransform(), font)
            size = cached.size()
            entry = (cached, font, size.width(), size.height())
            self._text_cache[key] = entry
            if len(self._text_cache) > 3072:
                self._text_cache.popitem(last=False)
        cached, font, text_width, text_height = entry
        x = rect.left() if align == _ALIGN.AlignLeft else (
            rect.center().x()-text_width/2 if align == _ALIGN.AlignHCenter else rect.right()-text_width)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawStaticText(QtCore.QPointF(x, rect.center().y()-text_height/2), cached)

    def _rect(self, x, y, w, h):
        return QtCore.QRectF(x, y, max(0, w), max(0, h))

    def _paint_row(self, p, row, bars):
        g = self.geometry
        left, right, m, w = g['price_left'], g['price_right'], g['margin'], g['width']
        y, h = row.top, g['row_height']
        color = self.side_colors[row.side] if self._display_live else self.muted
        p.fillRect(self._rect(left, y, right-left, h), self.spine)
        if row.touch:
            p.fillRect(self._rect(left+3, y+1, right-left-6, h-2),
                       _mix(self._bg, color, .12))
        bid = row.side == 'bid'
        area = left-m-8
        bar_max = max(1, area - (78 if g['totals'] else 0))
        ratio = bars.get(row.key, row.ratio)
        bar = max(0, bar_max * ratio)
        bx = left-5-bar if bid else right+5
        if bar > 0:
            p.fillRect(self._rect(bx, y+2, bar, h-4), self.tracks[row.side] if self._display_live else self.surface)
            p.fillRect(self._rect(bx, y+h-6, bar, 3), self.fills[row.side] if self._display_live else self.surface)
            # One sharp terminal accent makes small depth changes visible.
            p.fillRect(self._rect(bx if bid else bx+bar-2, y+h-6, min(2, bar), 3), color)
        if g['totals']:
            total_x = m if bid else w-m-65
            self._text(p, self._rect(total_x, y, 65, h-1), row.total_text, self.muted,
                       self.label_font, _ALIGN.AlignLeft if bid else _ALIGN.AlignRight)
        self._text(p, self._rect(left+9, y, right-left-18, h), row.price_text, color,
                   self.price_font, _ALIGN.AlignHCenter, exact=True)
        value_rect = (self._rect(left-bar_max-9, y, bar_max-3, h) if bid
                      else self._rect(right+12, y, bar_max-3, h))
        self._text(p, value_rect.adjusted(0, 0, 0, -6), row.size_text, self.ink if self._display_live else self.muted,
                   self.number_font, _ALIGN.AlignRight if bid else _ALIGN.AlignLeft, exact=True)
        # A quiet outer marker retains side identity without full table grid lines.
        if row.touch:
            tag = ('BEST BID' if bid else 'BEST ASK') if area >= 72 else ('BID' if bid else 'ASK')
            tag_rect = (self._rect(right+12, y, w-right-m-12, h) if bid
                        else self._rect(m, y, left-m-12, h))
            self._text(p, tag_rect, tag, self.muted, self.label_font,
                       _ALIGN.AlignLeft if bid else _ALIGN.AlignRight)


    def _trade_delta(self):
        source = self.source
        if (source is None or not source.ready or not self.trade_active
                or not self._display_live or source.trade_age_seconds is None):
            return None
        delta = source.buy_notional_5s - source.sell_notional_5s
        return delta if math.isfinite(delta) else None

    def _paint_center(self, p, now):
        g = self.geometry
        y, h, w, m = g['center_top'], g['center_height'], g['width'], g['margin']
        p.fillRect(self._rect(0, y, w, h), self.surface)
        p.fillRect(self._rect(m, y, w-m*2, 1), self.line)
        p.fillRect(self._rect(m, y+h-1, w-m*2, 1), self.line)
        label, price, side = self._last_trade(now)
        color = self.side_colors.get(side, self.accent) if self._display_live else self.muted
        center_price = self._price(price, native=True)
        if label == 'MID PRICE' and price > 0:
            # A midpoint can be half a native tick; preserve that extra digit.
            decimals = _decimal_places_from_step(self.config['tick']) if self.config['tick'] else 11
            center_price = f'{price:,.{min(16, decimals+1)}f}'
        self._text(p, self._rect(g['price_left']+4, y+3,
                   g['price_right']-g['price_left']-8, h-6), center_price, color,
                   self.hero_font, _ALIGN.AlignHCenter, exact=True)
        if not self._display_live:
            status = 'LAST KNOWN' if self.source is not None and self.source.ready else 'SYNCING'
            self._text(p, self._rect(g['price_left']+4, y+3,
                       g['price_right']-g['price_left']-8, 13), status, self.muted,
                       self.label_font, _ALIGN.AlignHCenter)
        left_width = max(0., g['price_left']-m-12)
        quote = _assets(self.config['symbol'])[1]
        delta = self._trade_delta()
        delta_text = '—' if delta is None else ('+' if delta > 0 else '−' if delta < 0 else '') + _amount(abs(delta))
        delta_color = (self.side_colors['bid'] if delta is not None and delta > 0 else
                       self.side_colors['ask'] if delta is not None and delta < 0 else self.muted)
        self._text(p, self._rect(m, y+4, left_width, 17), f'Δ5s {quote}',
                   self.muted, self.label_font, _ALIGN.AlignLeft)
        self._text(p, self._rect(m, y+23, left_width, 23), delta_text,
                   delta_color, self.number_font, _ALIGN.AlignLeft, exact=True)
        right_x = g['price_right']+12
        right_width = max(0., w-m-right_x)
        spread = self.source.spread if self.source else 0.
        spread_text = format_book_price(max(0., spread), _decimal_places_from_step(self.config['tick']) if self.config['tick'] else None) if self.source and self.source.ready else '—'
        self._text(p, self._rect(right_x, y+4, right_width, 17), 'SPREAD', self.muted, self.label_font)
        self._text(p, self._rect(right_x, y+22, right_width, 20), spread_text, self.ink, self.number_font, exact=True)
        bps = f'{self.source.spread_bps:.2f} bps' if self.source and self.source.ready else '—'
        self._text(p, self._rect(right_x, y+h-18, right_width, 14), bps, self.muted, self.label_font)

    def _paint_footer(self, p):
        g = self.geometry
        m, y, w = g['margin'], g['footer_top'], g['width']
        amount_y = y + 3
        bar_y = y + g['footer_height'] - 7
        ask, bid = self.side_totals
        total = ask+bid
        ratio = bid/total if total > 0 else .5
        p.fillRect(self._rect(m, y, w-m*2, 1), self.line)
        bid_label = f'{_amount(bid)} · {ratio*100:.0f}%' if total else '—'
        ask_label = f'{(1-ratio)*100:.0f}% · {_amount(ask)}' if total else '—'
        if total and not self.side_complete[1]:
            bid_label = '≥' + bid_label
        if total and not self.side_complete[0]:
            ask_label = f'{(1-ratio)*100:.0f}% · ≥{_amount(ask)}'
        self._text(p, self._rect(m, amount_y, w/2-m-6, 19), bid_label, self.side_colors['bid'],
                   self.number_font, _ALIGN.AlignLeft)
        self._text(p, self._rect(w/2+6, amount_y, w/2-m-6, 19), ask_label, self.side_colors['ask'], self.number_font)
        span = w-2*m
        p.fillRect(self._rect(m, bar_y, span, 3), self.line)
        if total:
            p.fillRect(self._rect(m, bar_y, max(0, span*ratio-1), 3), self.side_colors['bid'])
            p.fillRect(self._rect(m+span*ratio+1, bar_y, max(0, span*(1-ratio)-1), 3), self.side_colors['ask'])

    def _paint_depth_limits(self, p):
        g = self.geometry
        if not g['depth_limited']:
            return
        # Quiet caps mark the loaded price range without explanatory text.
        for count, y in ((g['ask_rows'], g['table_top']),
                         (g['bid_rows'], g['table_bottom'])):
            if count < g['rows']:
                p.fillRect(self._rect(g['price_left'], y, g['price_right']-g['price_left'], 1), self.line)

    def paint(self, target, dirty, now):
        start = time.perf_counter()
        p = QtGui.QPainter(target)
        p.setClipRegion(dirty)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), self._bg)
        g = self.geometry
        bars = self._bar_values(now)
        for row in self.rows:
            if dirty.intersects(QtCore.QRect(0, row.top, self.width(), g['row_height'])):
                self._paint_row(p, row, bars)
        self._paint_center(p, now)
        self._paint_footer(p)
        self._paint_depth_limits(p)
        if not self.rows and g['rows'] >= 2:
            message = 'Synchronizing order book…' if self.status == 'SYNCING' else 'No depth available'
            self._text(p, self._rect(12, g['table_top'], self.width()-24, max(28, g['center_top']-g['table_top'])),
                       message, self.muted, self.label_font, _ALIGN.AlignHCenter)
        p.end()
        self.frames += 1
        self.last_render_ms = (time.perf_counter()-start)*1000
        self.max_render_ms = max(self.max_render_ms, self.last_render_ms)


class _PrismProcess(_DomRasterProcess):
    """Reuse only the proven two-slot pixel leases; presentation is independent."""
    SNAPSHOT_INPUT = True

    def __init__(self, options):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ['QT_FONT_DPI'] = str(options['dpi'])
        self.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        load_app_fonts(self.application, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.canvas = _PrismRenderer(options['theme'])
        self.config = {}
        self.epoch = self.market_epoch = -1
        self.memory = None
        self._slot_bytes = 0
        self.images = []
        self.shape = None
        self.previous_slot = None
        self._slot_damage = [QtGui.QRegion(), QtGui.QRegion()]
        self._slot_revisions = [0, 0]
        self._frame_revision = 0
        self._sync_stats = dict(raster_sync_bytes=0, raster_sync_avoided_bytes=0,
            raster_sync_copies=0, raster_sync_full_copies=0, raster_sync_skipped=0,
            raster_sync_last_bytes=0, raster_sync_last_ms=0., raster_sync_max_ms=0.,
            raster_surface_allocations=0, raster_surface_reuses=0)
        self._snapshot_decoder = SnapshotDecoder()
        self._pending_snapshot = None
        self._last_prepare = 0.
        self.last_diagnostics = 0.

    def _configure(self, value):
        self.epoch, config = value
        if config['market_epoch'] != self.market_epoch:
            self._snapshot_decoder = SnapshotDecoder()
            self._pending_snapshot = None
            self.market_epoch = config['market_epoch']
        self.canvas.configure(config)
        self.config = config

    def _publish_surface(self, result, lease):
        slot = self.previous_slot
        if slot is None:
            return
        canvas = self.canvas
        revision = self._slot_revisions[slot]
        if self._lease_matches(lease) and lease[1] == slot:
            return
        base = tuple(lease) if self._lease_matches(lease) else None
        damage = self._slot_damage[lease[1]] if base is not None else QtGui.QRegion(canvas.rect())
        result.update(frame=(self.memory.name, slot, *self.shape[:3]),
            frame_slot_bytes=self._slot_bytes, frame_revision=revision, frame_base=base,
            dirty_rects=tuple((r.x(), r.y(), r.width(), r.height()) for r in damage))

    def step(self, commands, lease):
        commands = dict(commands)
        config_changed = 'config' in commands
        if config_changed:
            self._configure(commands.pop('config'))
        canvas = self.canvas
        seed_required = False
        changed = config_changed
        for name, (epoch, payload) in commands.items():
            if epoch != self.market_epoch:
                continue
            if name == 'snapshot':
                try:
                    decoded = self._snapshot_decoder.decode(payload, epoch)
                except SnapshotSeedRequired:
                    seed_required = True
                else:
                    source = _snapshot_from(decoded)
                    if isinstance(source, OrderFlowSnapshot) and source.symbol == self.config['symbol']:
                        self._pending_snapshot = source
            elif name == 'book_validity':
                canvas.book_valid = bool(payload)
                changed = True
            elif name == 'trade_status':
                canvas.trade_active = bool(payload)
                changed = True
        now = time.monotonic()
        interval = max(1, self.config.get('interval', 16))/1000
        if self._pending_snapshot is not None and (changed or now >= self._last_prepare+interval):
            source, self._pending_snapshot = self._pending_snapshot, None
            if canvas.source is None or source.sequence > canvas.source.sequence:
                canvas.adopt(source)
            self._last_prepare = now
        canvas.prepare(now)
        result = dict(epoch=self.epoch, market_epoch=self.market_epoch, frame=None,
            geometry=canvas.geometry, status=canvas.status,
            prices=tuple(tuple(row.price for row in canvas.rows if row.side == side)
                         for side in ('ask', 'bid')),
            prices_valid_until=_deadline(canvas.source) if canvas.book_valid else 0.,
            sequence=canvas.source.sequence if canvas.source else -1,
            generated_monotonic=canvas.source.generated_monotonic if canvas.source else 0.)
        if seed_required:
            result['_snapshot_seed_required'] = self.market_epoch
        if not canvas._dirty_pixels.isEmpty():
            slot, target = self._surface(lease)
            dirty = bounded_region(canvas._dirty_pixels, canvas.rect())
            canvas._dirty_pixels = QtGui.QRegion()
            self._synchronize_surface(slot, dirty)
            canvas.paint(target, dirty, now)
            self._surface_painted(slot, dirty)
        self._publish_surface(result, lease)
        deadlines = []
        if self._pending_snapshot is not None:
            deadlines.append(self._last_prepare+interval)
        if now < canvas._animation_end:
            deadlines.append(min(canvas._animation_end, now+interval))
        freshness = _deadline(canvas.source)
        if freshness > now:
            deadlines.append(freshness+.001)
        if canvas.source and canvas.source.trade_age_seconds is not None:
            trade_expiry = canvas.source.generated_monotonic + 5 - canvas.source.trade_age_seconds
            if trade_expiry > now:
                deadlines.append(trade_expiry+.001)
        result['next_at'] = min(deadlines, default=float('inf'))
        if now-self.last_diagnostics >= 1:
            result['diagnostics'] = dict(self._sync_stats, render_frames=canvas.frames,
                last_render_ms=canvas.last_render_ms, max_render_ms=canvas.max_render_ms,
                text_cache_entries=len(canvas._text_cache),
                aggregation_cache_hits=canvas._aggregation.reused,
                snapshot_transport_received=self._snapshot_decoder.diagnostic_state())
            self.last_diagnostics = now
        result['_map'] = _map_dom_frame
        return result

    def close(self):
        self.canvas.close()
        self._release_memory()


class PrismCanvas(QtWidgets.QWidget):
    """GUI endpoint: blit a leased image and select the exact visible price."""
    price_selected = Signal(float)
    visible_depth_rows_changed = Signal(int)
    grouping_step_requested = Signal(int)

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(220, 250)
        self.symbol = 'BTCUSDT'
        self.price_tick_size = 0.
        self.aggregation_multiplier = 1
        self.values = 'base'
        self.density = 'comfortable'
        self.totals = True
        self.motion = True
        self.theme = orderbook_palette(theme)
        self._market_epoch = self._display_epoch = 0
        self._latest_received_sequence = -1
        self._display_frame = self._pending_raster = None
        self._sent_config = None
        self._book_valid = True
        self._trade_active = True
        self._raster_ack_pending = False
        self._presentation_clock = None
        self._interaction_priority_active = False
        self._press = None
        self._hover = None
        self._geometry = {}
        self._remote_error = ''
        self._remote_diagnostics = {}
        self._unpainted_region = QtGui.QRegion()
        self._unpainted_prices = QtGui.QRegion()
        self._last_rows = -1
        self._gui_paints = 0
        self._last_paint_ms = self._max_paint_ms = 0.
        self._latency_samples = 0
        self._last_latency_ms = 0.
        self._last_painted_sequence = -1
        self._wheel_angle_remainder = 0
        self._wheel_pixel_remainder = 0
        self._last_wheel_time = 0.
        from .backend import _OrderBookProcessLink
        self._process_link = _OrderBookProcessLink(_PrismProcess,
            {'theme': dict(self.theme), 'dpi': self.logicalDpiX()}, self)
        self._process_link.ready.connect(self._adopt_raster)
        self._process_link.failed.connect(self._raster_failed)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._process_link.close)
        self._display_refresh_observer = DisplayRefreshObserver(self, self._queue_configuration)
        typography_controller().changed.connect(self._refresh_typography)
        self._refresh_typography()
        # One expiry event changes status and disables hover even if the feed stops.
        self._freshness_timer = QtCore.QTimer(self)
        self._freshness_timer.setSingleShot(True)
        self._freshness_timer.timeout.connect(self._expire_freshness)
        self._queue_configuration()

    def _refresh_typography(self):
        controller = typography_controller()
        self._typography = ({role: controller.profile(role) for role in TYPOGRAPHY_DEFAULTS}, controller.globals())
        self._queue_configuration()

    def _queue_configuration(self):
        config = dict(symbol=self.symbol, market_epoch=self._market_epoch,
            tick=self.price_tick_size, aggregation=self.aggregation_multiplier,
            values=self.values, density=self.density, totals=self.totals, motion=self.motion,
            book_valid=self._book_valid, trade_active=self._trade_active,
            size=(max(1, self.width()), max(1, self.height())), dpr=self.devicePixelRatioF(),
            interval=display_frame_interval_ms(self), theme=self.theme,
            typography=self._typography)
        if config == self._sent_config:
            return
        if self._sent_config is None or any(v != self._sent_config.get(k) for k, v in config.items() if k != 'interval'):
            self._display_epoch += 1
            self._clear_pointer()
        self._sent_config = config
        self._process_link.submit('config', (self._display_epoch, config))

    def set_snapshot(self, payload):
        snapshot = _snapshot_from(payload)
        if (not isinstance(snapshot, OrderFlowSnapshot) or snapshot.symbol != self.symbol
                or snapshot.sequence <= self._latest_received_sequence):
            return False
        self._latest_received_sequence = snapshot.sequence
        self._process_link.submit('snapshot', (self._market_epoch, payload))
        return True

    def set_book_validity(self, valid, reason=''):
        del reason
        self._book_valid = bool(valid)
        if not valid:
            self._clear_pointer()
        self._process_link.submit('book_validity', (self._market_epoch, self._book_valid))

    def set_trade_stream_status(self, active, reason=''):
        del reason
        self._trade_active = bool(active)
        self._process_link.submit('trade_status', (self._market_epoch, self._trade_active))

    def set_presentation_clock(self, clock):
        if clock is self._presentation_clock:
            return
        if self._presentation_clock is not None:
            self._presentation_clock.frame.disconnect(self._commit_pending_raster)
        self._presentation_clock = clock
        if clock is not None:
            clock.frame.connect(self._commit_pending_raster)
        else:
            self._commit_pending_raster()

    def set_interaction_priority(self, active):
        self._interaction_priority_active = bool(active)
        if not active:
            self._commit_pending_raster()

    def reset(self):
        self._market_epoch += 1
        self._pending_raster = self._display_frame = None
        self._latest_received_sequence = -1
        self._last_painted_sequence = -1
        self._unpainted_prices = QtGui.QRegion()
        self._freshness_timer.stop()
        self._reset_wheel()
        self._clear_pointer()
        self._queue_configuration()
        self.update()
        self._release_raster()

    def showEvent(self, event):
        self._queue_configuration()
        self._process_link.enable(True)
        super().showEvent(event)

    def hideEvent(self, event):
        self._clear_pointer()
        self._process_link.enable(False)
        self._pending_raster = None
        self._release_raster()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._process_link.close()
        super().closeEvent(event)

    def resizeEvent(self, event):
        self._queue_configuration()
        self._publish_rows()
        super().resizeEvent(event)

    def event(self, event):
        result = super().event(event)
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange and hasattr(self, '_process_link'):
            self._queue_configuration()
        return result

    def _native_frame(self, frame):
        _, _, w, h, dpr = frame['frame']
        return (math.isclose(dpr, self.devicePixelRatioF())
                and w == math.ceil(self.width()*dpr) and h == math.ceil(self.height()*dpr))

    @staticmethod
    def _frame_lease(frame):
        return (*frame['frame'][:2], frame['frame_revision']) if frame else None

    def _adopt_raster(self, result):
        self._raster_ack_pending = True
        if (self._interaction_priority_active and self._presentation_clock is not None
                and self.isVisible() and result.get('frame') is not None
                and result['market_epoch'] == self._market_epoch):
            self._pending_raster = result
            self._presentation_clock.request()
        else:
            self._commit_raster(result)

    def _commit_pending_raster(self, _time=0.):
        result, self._pending_raster = self._pending_raster, None
        if result is not None:
            self._commit_raster(result)

    def _commit_raster(self, result):
        self._remote_diagnostics['worker_pid'] = result.get('worker_pid', 0)
        if result['market_epoch'] != self._market_epoch or result['epoch'] != self._display_epoch:
            self._release_raster()
            return
        self._remote_diagnostics.update(result.get('diagnostics', {}))
        frame = self._display_frame
        if result.get('frame') is not None:
            dirty = QtGui.QRegion(self.rect())
            if (frame and result.get('frame_base') == self._frame_lease(frame)
                    and frame['frame'][2:] == result['frame'][2:] and self._native_frame(result)):
                dirty = QtGui.QRegion()
                for r in result['dirty_rects']:
                    dirty += QtCore.QRect(*r)
            if (frame is None or frame['epoch'] != result['epoch']
                    or frame['geometry'] != result['geometry'] or frame['prices'] != result['prices']):
                # Hit metadata arrives before Qt paints the leased image. Do
                # not select a changed price until its row has been displayed.
                # Size/bar-only updates keep existing prices selectable.
                self._unpainted_prices = bounded_region(self._unpainted_prices.united(dirty), self.rect())
            self._display_frame = result
            self._geometry = result['geometry']
            self._publish_rows()
            self._unpainted_region = bounded_region(self._unpainted_region.united(dirty), self.rect())
        elif frame is not None:
            # Unchanged pixels may carry newer freshness, sequence or geometry.
            # Retain the held image and update only its associated metadata.
            for key in ('prices_valid_until', 'sequence', 'generated_monotonic', 'status', 'prices'):
                frame[key] = result[key]
        frame = self._display_frame
        if frame is not None:
            remaining = frame['prices_valid_until'] - time.monotonic()
            if remaining > 0:
                self._freshness_timer.start(max(1, math.ceil(remaining*1000)))
            else:
                self._freshness_timer.stop()
                self._clear_pointer()
        if self.isVisible() and not self._unpainted_region.isEmpty():
            self.update(self._unpainted_region)
        else:
            self._release_raster()

    def _release_raster(self):
        if self._raster_ack_pending and self._pending_raster is None:
            self._raster_ack_pending = False
            self._process_link.consumed(lease=self._frame_lease(self._display_frame))

    def _raster_failed(self, message):
        import logging
        logging.getLogger(__name__).error('Prism order book renderer: %s', message)
        self._remote_error = 'Order book renderer unavailable'
        self._display_frame = self._pending_raster = None
        self._clear_pointer()
        self.update()

    def _expire_freshness(self):
        self._clear_pointer()
        g = self._geometry
        if g:
            self.update(QtCore.QRect(0, g['center_top'], self.width(), g['center_height']))

    def paintEvent(self, event):
        started = time.perf_counter()
        p = QtGui.QPainter(self)
        region = event.region().intersected(self.rect())
        p.setClipRegion(region)
        frame = self._display_frame
        if frame is None:
            p.fillRect(event.rect(), QtGui.QColor(self.theme['bg']))
            p.setPen(QtGui.QColor(self.theme['muted']))
            p.drawText(self.rect(), _ALIGN.AlignCenter, self._remote_error or 'Synchronizing order book…')
        else:
            image = frame['pixels'].images[frame['frame'][1]]
            if self._native_frame(frame):
                draw_native_region(p, image, region)
            else:
                # Continuous resize feedback while the new-size worker frame arrives.
                p.drawImage(QtCore.QRectF(self.rect()), image)
            if self._hover is not None and self._hit_valid(frame):
                side, index, price = self._hover
                prices = frame['prices'][0 if side == 'ask' else 1]
                if index < len(prices) and prices[index] == price:
                    g = frame['geometry']
                    top = g['center_top']-(index+1)*g['row_height'] if side == 'ask' else g['center_bottom']+index*g['row_height']
                    accent = QtGui.QColor(self.theme[side])
                    fill = QtGui.QColor(accent)
                    fill.setAlpha(22)
                    p.fillRect(QtCore.QRectF(4, top, self.width()-8, g['row_height']), fill)
                    p.setPen(QtGui.QPen(accent, 1))
                    p.drawRect(QtCore.QRectF(4.5, top+.5, self.width()-9, g['row_height']-1))
            # Label last-known prices immediately if worker scheduling is delayed.
            if frame['status'] in {'LIVE', 'TRADES OFF'} and time.monotonic() >= frame['prices_valid_until']:
                g = frame['geometry']
                label_rect = QtCore.QRect(int(g['price_left'])+4, g['center_top']+3,
                    max(1, int(g['price_right']-g['price_left'])-8), 13)
                p.fillRect(label_rect, _mix(self.theme['bg'], self.theme['text'], .035))
                p.setPen(QtGui.QColor(self.theme['muted']))
                p.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
                p.drawText(label_rect, _ALIGN.AlignHCenter | _ALIGN.AlignVCenter, 'LAST KNOWN')
            if frame['sequence'] != self._last_painted_sequence and frame['generated_monotonic']:
                self._last_latency_ms = max(0., (time.monotonic()-frame['generated_monotonic'])*1000)
                self._latency_samples += 1
                self._last_painted_sequence = frame['sequence']
        p.end()
        self._unpainted_region = bounded_region(self._unpainted_region.subtracted(region), self.rect())
        self._unpainted_prices = self._unpainted_prices.subtracted(region)
        self._last_paint_ms = (time.perf_counter()-started)*1000
        self._max_paint_ms = max(self._max_paint_ms, self._last_paint_ms)
        self._gui_paints += 1
        self._release_raster()

    def _hit_valid(self, frame):
        return (frame is not None and frame['epoch'] == self._display_epoch
                and frame['market_epoch'] == self._market_epoch and self._book_valid
                and time.monotonic() < frame['prices_valid_until'] and self._native_frame(frame))

    def _hit(self, position):
        frame = self._display_frame
        if not self._hit_valid(frame):
            return None
        g = frame['geometry']
        x, y = position.x(), position.y()
        if not g['margin'] <= x < self.width()-g['margin']:
            return None
        if g['table_top'] <= y < g['center_top']:
            side, index = 'ask', int((g['center_top']-y-1e-7)//g['row_height'])
        elif g['center_bottom'] <= y < g['table_bottom']:
            side, index = 'bid', int((y-g['center_bottom'])//g['row_height'])
        else:
            return None
        prices = frame['prices'][0 if side == 'ask' else 1]
        if not 0 <= index < len(prices):
            return None
        top = g['center_top']-(index+1)*g['row_height'] if side == 'ask' else g['center_bottom']+index*g['row_height']
        if self._unpainted_prices.intersects(QtCore.QRect(0, top, self.width(), g['row_height'])):
            return None
        return side, index, prices[index]

    def _hover_region(self, hover):
        if hover is None or not self._geometry:
            return QtGui.QRegion()
        side, index, _ = hover
        g = self._geometry
        top = g['center_top']-(index+1)*g['row_height'] if side == 'ask' else g['center_bottom']+index*g['row_height']
        return QtGui.QRegion(QtCore.QRect(0, top, self.width(), g['row_height']))

    def _set_hover(self, hover):
        if hover == self._hover:
            return
        dirty = self._hover_region(self._hover).united(self._hover_region(hover))
        self._hover = hover
        self.setCursor(Qt.CursorShape.PointingHandCursor if hover is not None else Qt.CursorShape.ArrowCursor)
        if not dirty.isEmpty():
            self.update(dirty)

    def _clear_pointer(self):
        self._press = None
        if hasattr(self, '_hover'):
            self._set_hover(None)

    def mouseMoveEvent(self, event):
        self._set_hover(self._hit(event.position()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._clear_pointer()
        self._reset_wheel()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = (self._market_epoch, self._display_epoch, self._hit(event.position()))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        press, self._press = self._press, None
        if event.button() == Qt.MouseButton.LeftButton and press is not None:
            hit = self._hit(event.position())
            if hit is not None and press == (self._market_epoch, self._display_epoch, hit):
                self.price_selected.emit(hit[2])
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self.grouping_step_requested.emit(-1 if event.key() == Qt.Key.Key_Minus else 1)
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self._clear_pointer()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _reset_wheel(self):
        self._wheel_angle_remainder = self._wheel_pixel_remainder = 0
        self._last_wheel_time = 0.

    def wheelEvent(self, event):
        # Zoom-in means finer grouping. Accumulate high-resolution wheel and
        # trackpad samples; one gesture must not jump to the largest multiplier.
        now = time.monotonic()
        if event.phase() == Qt.ScrollPhase.ScrollBegin or now-self._last_wheel_time > .5:
            self._reset_wheel()
        self._last_wheel_time = now
        angle, pixels = event.angleDelta().y(), event.pixelDelta().y()
        if angle:
            if angle * self._wheel_angle_remainder < 0:
                self._wheel_angle_remainder = 0
            self._wheel_angle_remainder += angle
            steps = math.trunc(self._wheel_angle_remainder / 120)
            self._wheel_angle_remainder -= steps * 120
        elif pixels:
            if pixels * self._wheel_pixel_remainder < 0:
                self._wheel_pixel_remainder = 0
            self._wheel_pixel_remainder += pixels
            steps = math.trunc(self._wheel_pixel_remainder / 40)
            self._wheel_pixel_remainder -= steps * 40
        else:
            steps = 0
        if steps:
            self.grouping_step_requested.emit(-steps)
        if event.phase() == Qt.ScrollPhase.ScrollEnd:
            self._reset_wheel()
        # Do not pass vertical wheel gestures through to the chart/parent.
        event.accept()

    def visible_rows_per_side(self):
        return _geometry(self.width(), self.height(), self.density, 84, self.totals)['rows']

    def _publish_rows(self):
        rows = self.visible_rows_per_side()
        if rows != self._last_rows:
            self._last_rows = rows
            self.visible_depth_rows_changed.emit(rows)

    def performance_state(self):
        return dict(self._remote_diagnostics, worker_error=self._remote_error,
            last_paint_ms=self._last_paint_ms, max_paint_ms=self._max_paint_ms,
            gui_paints=self._gui_paints, last_snapshot_to_paint_ms=self._last_latency_ms,
            pipeline_dom_to_paint_samples=self._latency_samples, presentation_preset='prism')


class OrderBookWidget(QtWidgets.QWidget):
    """Application-facing order book; keeps the existing backend and signals."""
    price_selected = Signal(float)
    aggregation_changed = Signal(int)
    column_preferences_changed = Signal(object)
    layout_state_changed = Signal(object)
    presentation_changed = Signal(object)
    snapshot_activity_requested = Signal(bool)
    depth_capacity_requested = Signal(int)
    depth_view_requested = Signal(str, int, int, float)
    EMBEDDED_TAPE_CONNECTED = True

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = orderbook_palette(theme)
        self.symbol = 'BTCUSDT'
        self.price_tick_size = 0.
        self._latest_snapshot = None
        self._tape = self._tape_source = self._owned_tape_source = None
        self._tape_enabled = False
        self._tape_mode = 'LARGE'
        self._panel_active = False
        self._last_depth_capacity = 0
        self._last_depth_view = None
        self.canvas = PrismCanvas(self.theme, self)
        self.canvas.price_selected.connect(self.price_selected.emit)
        self.canvas.grouping_step_requested.connect(self._step_grouping)
        self.canvas.visible_depth_rows_changed.connect(self._publish_depth_capacity)
        self.splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 1)
        self.controls = QtWidgets.QFrame(self)
        self.controls.setObjectName('prismControls')
        toolbar = QtWidgets.QHBoxLayout(self.controls)
        toolbar.setContentsMargins(10, 6, 10, 6)
        toolbar.setSpacing(6)
        self.grouping = QtWidgets.QToolButton(self.controls)
        self.grouping.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.grouping.setAccessibleName('Price grouping')
        self.grouping.setToolTip('Price grouping. Wheel over the book: up = finer, down = coarser. Keyboard: + / −')
        self._group_menu = QtWidgets.QMenu(self.grouping)
        self._group_actions = {}
        group = QtGui.QActionGroup(self._group_menu)
        for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
            action = self._group_menu.addAction(str(multiplier))
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, value=multiplier:
                self.set_aggregation_multiplier(value, emit=True))
            self._group_actions[multiplier] = action
        self.grouping.setMenu(self._group_menu)
        toolbar.addWidget(self.grouping)
        toolbar.addStretch(1)
        self.unit = QtWidgets.QToolButton(self.controls)
        self.unit.setAccessibleName('Size units')
        self.unit.setToolTip('Switch sizes between base asset and quote value')
        self.unit.clicked.connect(lambda: self.set_value_mode('quote' if self.canvas.values == 'base' else 'base'))
        toolbar.addWidget(self.unit)
        self.options = QtWidgets.QToolButton(self.controls)
        self.options.setText('•••')
        self.options.setAccessibleName('Order book display options')
        self.options.setToolTip('Display options')
        self.options.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._options_menu = QtWidgets.QMenu(self.options)
        self._compact_action = self._toggle('Compact rows', lambda v: self._set_option('density', 'compact' if v else 'comfortable'))
        self._totals_action = self._toggle('Cumulative totals when space permits', lambda v: self._set_option('totals', v))
        self._motion_action = self._toggle('Smooth depth transitions', lambda v: self._set_option('motion', v))
        self._options_menu.addSeparator()
        self._tape_action = self._toggle('Trade tape (wide panels)', self.set_tape_enabled)
        self._tape_mode_action = self._toggle('Tape: all trades', lambda v: self._set_tape_mode('ALL' if v else 'LARGE'))
        self._options_menu.addSeparator()
        self._options_menu.addAction('Reset display', self._reset_display)
        self.options.setMenu(self._options_menu)
        toolbar.addWidget(self.options)
        for button in (self.grouping, self.unit, self.options):
            button.setMinimumHeight(26)
            set_text_role(button, TextRole.ORDERBOOK_CONTROL)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.controls)
        self.setObjectName('prismOrderBook')
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(lambda point: self._options_menu.popup(self.canvas.mapToGlobal(point)))
        self.apply_theme(theme)
        self._sync_controls()

    def _toggle(self, text, callback):
        action = self._options_menu.addAction(text)
        action.setCheckable(True)
        action.toggled.connect(callback)
        return action

    @staticmethod
    def _checked(action, checked):
        blocker = QtCore.QSignalBlocker(action)
        action.setChecked(checked)
        del blocker

    def _sync_controls(self):
        base, quote = _assets(self.symbol)
        multiplier = self.canvas.aggregation_multiplier
        self.grouping.setText(f'{multiplier}×')
        self.unit.setText(quote if self.canvas.values == 'quote' else base)
        for value, action in self._group_actions.items():
            self._checked(action, value == multiplier)
            action.setText(f'{value}×')
        for action, checked in ((self._compact_action, self.canvas.density == 'compact'),
                (self._totals_action, self.canvas.totals), (self._motion_action, self.canvas.motion),
                (self._tape_action, self._tape_enabled), (self._tape_mode_action, self._tape_mode == 'ALL')):
            self._checked(action, checked)

    def _set_option(self, name, value, *, emit=True):
        setattr(self.canvas, name, value)
        self.canvas._queue_configuration()
        self._sync_controls()
        self._publish_depth_capacity()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def _step_grouping(self, step):
        values = ORDER_FLOW_AGGREGATION_MULTIPLIERS
        index = values.index(self.canvas.aggregation_multiplier)
        target = values[max(0, min(len(values)-1, index+step))]
        if target != self.canvas.aggregation_multiplier:
            self.set_aggregation_multiplier(target, emit=True)

    def _reset_display(self):
        self.restore_presentation_state({}, emit=True)
        self.set_aggregation_multiplier(1, emit=True)

    def set_aggregation_multiplier(self, multiplier, *, emit=False):
        value = _multiplier(multiplier)
        self._set_option('aggregation_multiplier', value, emit=False)
        if emit:
            self.aggregation_changed.emit(value)
            self.presentation_changed.emit(self.presentation_state())

    def aggregation_multiplier(self):
        return self.canvas.aggregation_multiplier

    def set_value_mode(self, mode, *, emit=True):
        self._set_option('values', 'quote' if mode == 'quote' else 'base', emit=emit)

    def set_column_preferences(self, preferences, *, emit=False):
        # Old analytical-lane settings cannot overwrite this new composition.
        if isinstance(preferences, dict) and 'totals' in preferences:
            self._set_option('totals', bool(preferences['totals']), emit=emit)
        if emit:
            self.column_preferences_changed.emit(self.column_preferences())

    def column_preferences(self):
        return {'totals': self.canvas.totals}

    def presentation_state(self):
        return dict(prism_version=1, values=self.canvas.values, density=self.canvas.density,
            totals=self.canvas.totals, motion=self.canvas.motion, aggregation=self.aggregation_multiplier(),
            columns=self.column_preferences(), tape_enabled=self._tape_enabled, tape_mode=self._tape_mode)

    def restore_presentation_state(self, state, *, emit=False):
        values = state if isinstance(state, dict) and state.get('prism_version') == 1 else {}
        self.canvas.values = 'quote' if values.get('values') == 'quote' else 'base'
        self.canvas.density = 'compact' if values.get('density') == 'compact' else 'comfortable'
        self.canvas.totals = bool(values.get('totals', True))
        self.canvas.motion = bool(values.get('motion', True))
        self.canvas.aggregation_multiplier = _multiplier(values.get('aggregation', 1))
        self._tape_enabled = bool(values.get('tape_enabled', False))
        self._set_tape_mode(values.get('tape_mode', 'LARGE'), emit=False)
        self.canvas._queue_configuration()
        self._sync_controls()
        self._sync_tape_visibility()
        self._publish_depth_capacity()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def layout_state(self):
        return dict(preset='prism', density=self.canvas.density,
                    visible_rows_per_side=self.canvas.visible_rows_per_side(), columns=self.column_preferences())

    def _publish_depth_capacity(self, _rows=None, *, force=False):
        # Native analytics stay bounded; the feed worker groups its retained
        # book into only the requested visible rows before crossing processes.
        capacity = 80 if self.canvas.visible_rows_per_side()+12 <= 80 else 120
        if force or capacity != self._last_depth_capacity:
            self._last_depth_capacity = capacity
            self.depth_capacity_requested.emit(capacity)
        view = (self.symbol, self.canvas.visible_rows_per_side()+12,
                self.aggregation_multiplier(), self.price_tick_size)
        if force or view != self._last_depth_view:
            self._last_depth_view = view
            self.depth_view_requested.emit(*view)
            self.layout_state_changed.emit(self.layout_state())

    def set_symbol(self, symbol, rules=None):
        symbol = str(symbol).upper().strip().removesuffix('.P') or 'BTCUSDT'
        changed = symbol != self.symbol
        self.symbol = self.canvas.symbol = symbol
        if changed:
            self._latest_snapshot = None
            self.canvas.reset()
        if changed or rules is not None:
            self.set_symbol_rules(rules)
        self._sync_controls()
        if self._tape is not None:
            self._tape.set_market(symbol, tick_size=self.price_tick_size)

    def set_symbol_rules(self, rules):
        tick = safe_float(getattr(rules, 'tick_size', 0)) if rules is not None else 0.
        self.price_tick_size = self.canvas.price_tick_size = max(0., tick)
        self.canvas._queue_configuration()
        self._sync_controls()
        self._publish_depth_capacity()
        if self._tape is not None:
            self._tape.set_market(self.symbol, tick_size=self.price_tick_size)

    def set_order_flow_snapshot(self, payload):
        source = _snapshot_from(payload)
        if self.canvas.set_snapshot(payload):
            self._latest_snapshot = source
            if self._owned_tape_source is not None:
                self._owned_tape_source.ingest_snapshot(source)

    def set_microstructure_snapshot(self, _snapshot):
        # Depth and last executions come from the canonical order-flow snapshot.
        # Accept the host hook without adding a second, differently timed data source.
        pass

    def set_book_validity(self, valid, reason=''):
        self.canvas.set_book_validity(valid, reason)

    def set_trade_stream_status(self, active, reason=''):
        self.canvas.set_trade_stream_status(active, reason)

    def set_panel_active(self, active):
        if bool(active) != self._panel_active:
            self._panel_active = bool(active)
            self.snapshot_activity_requested.emit(self._panel_active)

    def set_presentation_clock(self, clock):
        self.canvas.set_presentation_clock(clock)
        if self._tape is not None:
            self._tape.set_presentation_clock(clock)

    def set_interaction_priority(self, active):
        self.canvas.set_interaction_priority(active)
        if self._tape is not None:
            self._tape.set_interaction_priority(active)

    def set_tape_source(self, source):
        if source is self._tape_source:
            return
        if self._owned_tape_source is not None:
            self._owned_tape_source.close()
            self._owned_tape_source.deleteLater()
            self._owned_tape_source = None
        self._tape_source = source
        if self._tape is not None:
            self._tape.set_tape_source(source)

    def _ensure_tape(self):
        if self._tape is not None:
            return
        if self._tape_source is None:
            source = SharedTradeTapeSource(symbol=self.symbol, parent=self)
            self.set_tape_source(source)
            self._owned_tape_source = source
            if self._latest_snapshot is not None:
                source.ingest_snapshot(self._latest_snapshot)
        self._tape = TradesTapeWidget(self.theme, self.splitter)
        self._tape.setMinimumWidth(300)
        self._tape.setMaximumWidth(460)
        self._tape.set_tape_source(self._tape_source)
        self._tape.set_market(self.symbol, tick_size=self.price_tick_size)
        self._tape.set_presentation_clock(self.canvas._presentation_clock)
        self._tape.set_interaction_priority(self.canvas._interaction_priority_active)
        self._tape.set_mode(self._tape_mode, emit=False)
        self._tape.mode_changed.connect(self._set_tape_mode)
        self.splitter.addWidget(self._tape)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([max(300, self.width()-340), 340])

    def set_tape_enabled(self, enabled, *, emit=True):
        self._tape_enabled = bool(enabled)
        self._sync_tape_visibility()
        self._sync_controls()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def _set_tape_mode(self, mode, *, emit=True):
        self._tape_mode = 'ALL' if str(mode).upper() == 'ALL' else 'LARGE'
        if self._tape is not None:
            self._tape.set_mode(self._tape_mode, emit=False)
        self._sync_controls()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def _sync_tape_visibility(self):
        # Keep the ladder readable; the native tape appears only when both fit.
        visible = self._tape_enabled and self.width() >= 680 and self.isVisible()
        if visible:
            self._ensure_tape()
        if self._tape is not None:
            self._tape.setVisible(visible)
            self._tape.set_panel_active(visible)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_tape_visibility()
        self._publish_depth_capacity(force=True)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._tape is not None:
            self._tape.set_panel_active(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_tape_visibility()

    def wheelEvent(self, event):
        self.canvas.wheelEvent(event)

    def reset(self):
        self._latest_snapshot = None
        self.canvas.reset()
        if self._tape is not None:
            self._tape.reset(preserve_history=True)

    def apply_theme(self, theme):
        self.theme = orderbook_palette(theme)
        self.canvas.theme = self.theme
        self.canvas._queue_configuration()
        p = self.theme
        self.setStyleSheet(f'''
            QWidget#prismOrderBook, QFrame#prismControls {{ background: {p['bg']}; }}
            QFrame#prismControls {{ border-top: 1px solid {p['grid']}; }}
            QToolButton {{ color: {p['text']}; background: {p['surface_raised']};
                border: 1px solid {p['grid']}; border-radius: 5px; padding: 3px 9px; }}
            QToolButton:hover {{ border-color: {p['mid']}; background: {p['control_hover']}; }}
            QToolButton:pressed {{ background: {p['control_pressed']}; }}
            QToolButton:focus {{ border-color: {p['mid']}; }}
            QToolButton::menu-indicator {{ image: none; width: 0; }}
            QSplitter::handle {{ background: {p['grid']}; }}
        ''')
        menu_style = f'''QMenu {{ background: {p['surface_raised']}; color: {p['text']};
            border: 1px solid {p['grid']}; padding: 5px; }}
            QMenu::item {{ padding: 7px 22px; }}
            QMenu::item:selected {{ background: {p['control_hover']}; }}
            QMenu::separator {{ height: 1px; background: {p['grid']}; margin: 4px 0; }}'''
        self._group_menu.setStyleSheet(menu_style)
        self._options_menu.setStyleSheet(menu_style)
        if self._tape is not None:
            self._tape.apply_theme(self.theme)

    def performance_state(self):
        return dict(self.canvas.performance_state(), tape_visible=int(self._tape is not None and self._tape.isVisible()),
                    value_mode=self.canvas.values, book_depth=1)
