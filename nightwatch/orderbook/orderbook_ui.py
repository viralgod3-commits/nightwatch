"""Order-book frontend: DOM, controls, presentation and Time & Sales."""
from __future__ import annotations


# Order-book presentation contract. Background overrides live in this module;
# analytical state vocabulary continues to come from the application's constants.
from ..constants import (
    DEFAULT_SYMBOL,
    ORDERBOOK_FIXED_PALETTE as _LEGACY_ORDERBOOK_PALETTE, ORDERBOOK_STATE_ACRONYMS,
    ORDERBOOK_STATE_FULL_LABEL_WIDTH, ORDERBOOK_STATE_LABELS,
    ORDERBOOK_STATE_MIN_WIDTH,
)

# The orderbook owns its presentation overrides: surrounding surfaces stay
# pure black, while measured liquidity and semantic accents retain color.
# Accent values follow the supplied constants palette.
ORDERBOOK_REFERENCE = {
    **_LEGACY_ORDERBOOK_PALETTE,
    'bg': '#000000',
    'surface_top': '#000000',
    'surface_raised': '#000000',
    'surface_center': '#000000',
    'grid': '#1B1E22',
    'grid_strong': '#2B3036',
    'text': '#D5D8DC',
    'muted': '#7C838C',
    'bid': '#00C56A',
    'ask': '#F0143E',
    'bid_fill': '#062B19',
    'ask_fill': '#3A0A15',
    'bid_fill_strong': '#075B34',
    'ask_fill_strong': '#7B1028',
    'amber': '#C89A47',
    'mid': '#C8CDD2',
    'purple': '#9A87C8',
    'control': '#000000',
    'control_hover': '#111316',
    'control_pressed': '#191C20',
    'control_border': '#292D33',
    'control_hover_line': '#59616B',
}
_ORDERBOOK_SIGNAL_COLORS = {'ABSORBING': '#35C4B2',
 'PULLING': '#D1A248',
 'STACKING': '#8F7AC8',
 'DEPLETING': '#F26B4B',
 'PERSISTENT': '#B7A6E2'}

_REFERENCE_FULL_COLUMNS = ('state', 'memory', 'delta', 'bid', 'price', 'ask', 'flow')
_REFERENCE_FULL_WEIGHTS = {
    'state': 58.0, 'memory': 78.0, 'delta': 72.0,
    'bid': 112.0, 'price': 126.0, 'ask': 112.0, 'flow': 70.0,
}
_STATE_COLUMN_MAX_WIDTH = 120.0
_PAIRED_ANALYTIC_WIDTH = 70.0
# Responsive layouts introduce analytical lanes as width becomes available.
from ..models import (
    ORDER_FLOW_AGGREGATION_MULTIPLIERS, DomPositionOverlay,
    OrderFlowDisplayLevel, OrderFlowPresentationFrame,
    OrderFlowSnapshot, OrderFlowTradePrint, order_flow_semantic_event,
)
from .revisions import (
    amount_inputs, amount_revision_key, levels_unchanged,
)
from .raster_regions import (
    bounded_region, copy_pixel_region, draw_native_region, pixel_region, region_area,
)
from .tape import TAPE_CAPACITY, TapeSeedRequired
from .tape_source import SharedTradeTapeSource

from ..presentation import DisplayRefreshObserver, display_frame_interval_ms
from ..chart.analysis import LatestJob


def _clamp(value: float, low: float=0.0, high: float=1.0) -> float:
    return max(low, min(high, value))

def _order_flow_view_scale(values: list[float]) -> float:
    positives = sorted((abs(value) for value in values if math.isfinite(value) and abs(value) > 1e-12))
    if not positives:
        return 0.0
    index = int(round((len(positives) - 1) * 0.9))
    return max(positives[index], positives[-1] * 0.25, 1e-12)

def _order_flow_view_intensity(value: float, scale: float) -> float:
    if scale <= 0.0 or not math.isfinite(value) or value == 0.0:
        return 0.0
    return math.sqrt(_clamp(abs(value) / scale))

def _aggregation_source_signature(level: OrderFlowDisplayLevel) -> tuple[object, ...]:
    """Return only source values that affect an aggregated bucket."""
    return (
        level.quantity,
        level.notional,
        level.delta_notional_5s,
        level.trade_notional_5s,
        level.signed_trade_notional_5s,
        level.rpi_trade_notional_5s,
        # Aggregated LIQ 30s is reconstructed from the native normalized strip
        # and its native peak, so both participate in cache invalidation.
        tuple(level.liquidity_history_30s),
        level.history_peak_notional_30s,
        # Age is presentation-only and advances every snapshot. Keeping it out
        # of the bucket signature lets otherwise-identical aggregate rows reuse
        # their cached analytical result. The cache-hit path refreshes age.
        level.persistence_ratio,
        level.replenishments,
        level.buy_trade_notional_5s,
        level.sell_trade_notional_5s,
        level.buy_trade_count_5s,
        level.sell_trade_count_5s,
        level.largest_buy_trade_5s,
        level.largest_sell_trade_5s,
        level.rejected_buy_prints_5s,
        level.rejected_sell_prints_5s,
        level.recent_replenished_notional,
        level.recent_restacked_notional,
        level.trade_reload_count,
        level.restack_count,
    )


def _aggregate_bucket_liquidity_history(
    members: list[OrderFlowDisplayLevel],
) -> tuple[tuple[float, ...], float, float, float]:
    """Combine native LIQ 30s strips into one notional-correct bucket strip.

    Native histories are normalized by each price level's own 30-second peak.
    Reconstruct each known bin to quote notional before summing so a small level
    cannot carry the same weight as a large wall. Bins are right-aligned because
    the newest bin is the shared temporal anchor. If any constituent is unknown
    for a bin, the aggregate bin stays unknown rather than assuming zero depth.
    """
    histories = [tuple(level.liquidity_history_30s) for level in members]
    bin_count = max((len(history) for history in histories), default=0)
    if bin_count <= 0:
        return (), 0.0, 0.0, 0.0

    bucket_bins: list[float | None] = []
    for bin_index in range(bin_count):
        total = 0.0
        known = True
        for level, history in zip(members, histories):
            source_index = bin_index - (bin_count - len(history))
            if source_index < 0:
                known = False
                break
            try:
                normalized = float(history[source_index])
                native_peak = float(level.history_peak_notional_30s)
            except (TypeError, ValueError, OverflowError):
                known = False
                break
            if (
                not math.isfinite(normalized)
                or normalized < 0.0
                or not math.isfinite(native_peak)
                or native_peak < 0.0
            ):
                known = False
                break
            total += max(0.0, normalized) * native_peak
        bucket_bins.append(total if known else None)

    known_totals = [value for value in bucket_bins if value is not None]
    if not known_totals:
        return tuple(-1.0 for _ in bucket_bins), 0.0, 0.0, 0.0

    peak = max(known_totals)
    mean = sum(known_totals) / len(known_totals)
    presence = sum(1 for value in known_totals if value > 0.0) / len(known_totals)
    normalized_bins = tuple(
        -1.0 if value is None else (value / peak if peak > 0.0 else 0.0)
        for value in bucket_bins
    )
    return normalized_bins, presence, peak, mean


def aggregate_order_flow_snapshot(
    snapshot: OrderFlowSnapshot,
    multiplier: int,
    tick_size: float,
    bucket_cache: dict[tuple[str, int], tuple[tuple[tuple[object, ...], ...], OrderFlowDisplayLevel]] | None=None,
    *, floor_asks: bool=False,
) -> OrderFlowSnapshot:
    """Return a display-only N-tick aggregation of one immutable snapshot.

    The canonical analyzer remains native-tick. This transformation is deliberately
    pure with respect to market state. When a caller supplies ``bucket_cache``,
    unchanged aggregate buckets reuse their previous row and only buckets whose
    aggregation-relevant source values changed are rebuilt.
    """
    try:
        multiplier = int(multiplier)
    except (TypeError, ValueError):
        multiplier = 1
    if multiplier not in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
        multiplier = 1
    try:
        tick_size = float(tick_size)
    except (TypeError, ValueError, OverflowError):
        tick_size = 0.0
    if not math.isfinite(tick_size):
        tick_size = 0.0
    if multiplier <= 1 or tick_size <= 0.0 or (not snapshot.ready):
        if bucket_cache is not None:
            bucket_cache.clear()
        return snapshot

    active_cache_keys: set[tuple[str, int]] = set()
    bucket_signatures: dict[tuple[str, int], tuple[tuple[object, ...], ...]] = {}

    def aggregate_side(levels: tuple[OrderFlowDisplayLevel, ...], side: str) -> tuple[OrderFlowDisplayLevel, ...]:
        if not levels:
            return ()
        buckets: dict[int, list[OrderFlowDisplayLevel]] = {}
        order: list[int] = []
        for level in levels:
            raw_tick = int(round(level.price / tick_size))
            if side == 'bid' or floor_asks:
                display_tick = raw_tick // multiplier * multiplier
            else:
                display_tick = (raw_tick + multiplier - 1) // multiplier * multiplier
            if display_tick not in buckets:
                buckets[display_tick] = []
                order.append(display_tick)
            buckets[display_tick].append(level)
        provisional: list[OrderFlowDisplayLevel] = []
        for display_tick in order:
            members = buckets[display_tick]
            cache_key = (side, display_tick)
            signature = tuple(_aggregation_source_signature(level) for level in members)
            active_cache_keys.add(cache_key)
            bucket_signatures[cache_key] = signature
            if bucket_cache is not None:
                cached = bucket_cache.get(cache_key)
                if cached is not None and cached[0] == signature:
                    cached_level = cached[1]
                    newest_age = min((level.age_seconds for level in members), default=0.0)
                    if not math.isclose(float(cached_level.age_seconds), float(newest_age), rel_tol=0.0, abs_tol=1e-12):
                        cached_level = replace(cached_level, age_seconds=float(newest_age))
                    provisional.append(cached_level)
                    continue
            quantity = sum((max(0.0, level.quantity) for level in members))
            notional = sum((max(0.0, level.notional) for level in members))
            delta = sum((level.delta_notional_5s for level in members))
            trade = sum((level.trade_notional_5s for level in members))
            signed_trade = sum((level.signed_trade_notional_5s for level in members))
            rpi = sum((max(0.0, level.rpi_trade_notional_5s) for level in members))
            buy_trade = sum((max(0.0, level.buy_trade_notional_5s) for level in members))
            sell_trade = sum((max(0.0, level.sell_trade_notional_5s) for level in members))
            buy_count = sum((max(0, int(level.buy_trade_count_5s)) for level in members))
            sell_count = sum((max(0, int(level.sell_trade_count_5s)) for level in members))
            largest_buy = max((level.largest_buy_trade_5s for level in members), default=0.0)
            largest_sell = max((level.largest_sell_trade_5s for level in members), default=0.0)
            rejected_buy = sum((max(0, int(level.rejected_buy_prints_5s)) for level in members))
            rejected_sell = sum((max(0, int(level.rejected_sell_prints_5s)) for level in members))
            replenished = sum((max(0.0, level.recent_replenished_notional) for level in members))
            restacked = sum((max(0.0, level.recent_restacked_notional) for level in members))
            reload_count = sum((max(0, int(level.trade_reload_count)) for level in members))
            restack_count = sum((max(0, int(level.restack_count)) for level in members))
            semantic_event_kind, semantic_event_label = order_flow_semantic_event(
                trade_reload_count=reload_count,
                recent_replenished_notional=replenished,
                restack_count=restack_count,
                recent_restacked_notional=restacked,
                rejected_buy_prints=rejected_buy,
                rejected_sell_prints=rejected_sell,
            )
            weight = notional if notional > 1e-12 else float(len(members))
            persistence = sum((level.persistence_ratio * max(level.notional, 0.0) for level in members)) / weight if notional > 1e-12 else sum((level.persistence_ratio for level in members)) / max(1, len(members))
            history, history_presence, history_peak, history_mean = _aggregate_bucket_liquidity_history(members)
            # STATE remains a native-price classification. Grouped rows expose
            # measured bucket sums and a separately reconstructed bucket history.
            state = 'NORMAL'
            provisional.append(OrderFlowDisplayLevel(
                side=side,
                price=display_tick * tick_size,
                quantity=quantity,
                notional=notional,
                delta_notional_5s=delta,
                trade_notional_5s=trade,
                signed_trade_notional_5s=signed_trade,
                rpi_trade_notional_5s=rpi,
                age_seconds=min((level.age_seconds for level in members), default=0.0),
                persistence_ratio=_clamp(persistence),
                replenishments=sum((max(0, int(level.replenishments)) for level in members)),
                state=state,
                liquidity_intensity=0.0,
                delta_intensity=0.0,
                trade_intensity=0.0,
                cumulative_depth_notional=0.0,
                depth_intensity=0.0,
                liquidity_history_30s=history,
                history_presence_30s=history_presence,
                history_peak_notional_30s=history_peak,
                history_mean_notional_30s=history_mean,
                buy_trade_notional_5s=buy_trade,
                sell_trade_notional_5s=sell_trade,
                buy_trade_count_5s=buy_count,
                sell_trade_count_5s=sell_count,
                largest_buy_trade_5s=largest_buy,
                largest_sell_trade_5s=largest_sell,
                rejected_buy_prints_5s=rejected_buy,
                rejected_sell_prints_5s=rejected_sell,
                recent_replenished_notional=replenished,
                recent_restacked_notional=restacked,
                trade_reload_count=reload_count,
                restack_count=restack_count,
                semantic_event_kind=semantic_event_kind,
                semantic_event_label=semantic_event_label,
            ))
        return tuple(provisional)

    bids = aggregate_side(snapshot.bid_levels, 'bid')
    asks = aggregate_side(snapshot.ask_levels, 'ask')
    notionals = [level.notional for level in (*bids, *asks)]
    deltas = [level.delta_notional_5s for level in (*bids, *asks)]
    trades = [level.trade_notional_5s for level in (*bids, *asks)]
    liquidity_scale = _order_flow_view_scale(notionals)
    delta_scale = _order_flow_view_scale(deltas)
    trade_scale = _order_flow_view_scale(trades)
    bid_depth = 0.0
    for level in bids:
        bid_depth += max(0.0, level.notional)
    ask_depth = 0.0
    for level in asks:
        ask_depth += max(0.0, level.notional)
    depth_scale = max(bid_depth, ask_depth, 1e-12)

    def with_intensities(levels: tuple[OrderFlowDisplayLevel, ...]) -> tuple[OrderFlowDisplayLevel, ...]:
        output: list[OrderFlowDisplayLevel] = []
        cumulative = 0.0
        for level in levels:
            cumulative += max(0.0, level.notional)
            liquidity_intensity = _order_flow_view_intensity(level.notional, liquidity_scale)
            delta_intensity = _order_flow_view_intensity(level.delta_notional_5s, delta_scale)
            trade_intensity = _order_flow_view_intensity(level.trade_notional_5s, trade_scale)
            depth_intensity = math.sqrt(_clamp(cumulative / depth_scale))
            if (
                level.liquidity_intensity == liquidity_intensity
                and level.delta_intensity == delta_intensity
                and level.trade_intensity == trade_intensity
                and level.cumulative_depth_notional == cumulative
                and level.depth_intensity == depth_intensity
            ):
                display = level
            else:
                display = replace(level, liquidity_intensity=liquidity_intensity, delta_intensity=delta_intensity, trade_intensity=trade_intensity, cumulative_depth_notional=cumulative, depth_intensity=depth_intensity)
            output.append(display)
            if bucket_cache is not None:
                display_tick = int(round(display.price / tick_size))
                cache_key = (display.side, display_tick)
                bucket_cache[cache_key] = (bucket_signatures[cache_key], display)
        return tuple(output)

    bids = with_intensities(bids)
    asks = with_intensities(asks)
    if bucket_cache is not None:
        for cache_key in tuple(bucket_cache):
            if cache_key not in active_cache_keys:
                bucket_cache.pop(cache_key, None)
    revisions = snapshot.component_revisions
    if revisions is not None:
        # A zero-sized view marker distinguishes the floor axis from the ladder
        # without changing the native price/amount revisions.
        revisions = replace(revisions, view=(*revisions.view, (multiplier, tick_size), (int(floor_asks), 0.0)))
    return replace(snapshot, liquidity_scale=liquidity_scale, delta_scale=delta_scale, trade_scale=trade_scale, cumulative_depth_scale=depth_scale, bid_levels=bids, ask_levels=asks, component_revisions=revisions)


class _DomAggregation:
    """Worker-owned bucket cache; no widget or mutable GUI state is accessed."""

    def __init__(self):
        self.context = None
        self.cache = {}
        self.source = None
        self.display = None
        self.reused = self.rebuilt = 0

    def prepare(self, context, snapshot):
        started = time.perf_counter()
        if context != self.context:
            self.context = context
            self.cache.clear()
            self.source = self.display = None
        _epoch, _symbol, multiplier, tick_size, floor_asks = context
        if (multiplier <= 1 or multiplier not in ORDER_FLOW_AGGREGATION_MULTIPLIERS
                or tick_size <= 0.0 or not math.isfinite(tick_size) or not snapshot.ready):
            self.cache.clear()
            display = snapshot
        elif (self.source is not None and self.display is not None
                and self.source.ready == snapshot.ready
                and levels_unchanged(snapshot, self.source)):
            self.reused += 1
            previous = self.display
            revisions = snapshot.component_revisions
            if revisions is not None:
                revisions = replace(revisions, view=(*revisions.view, (multiplier, tick_size), (int(floor_asks), 0.0)))
            display = replace(
                snapshot, liquidity_scale=previous.liquidity_scale,
                delta_scale=previous.delta_scale, trade_scale=previous.trade_scale,
                cumulative_depth_scale=previous.cumulative_depth_scale,
                bid_levels=previous.bid_levels, ask_levels=previous.ask_levels,
                component_revisions=revisions,
            )
        else:
            self.rebuilt += 1
            display = aggregate_order_flow_snapshot(
                snapshot, multiplier, tick_size, self.cache, floor_asks=floor_asks
            )
        self.source, self.display = snapshot, display
        return snapshot, display, started, time.perf_counter()

import math
from ..models import BOOK_DEPTH_FRESH_SECONDS, BOOK_BBO_FRESH_SECONDS, book_data_is_fresh
from PySide6 import QtGui
from ..models import safe_float

def _general_to_fixed(text: str) -> str:
    """Convert a short general-format float string to fixed notation."""
    lowered = text.lower()
    if 'e' not in lowered:
        if '.' in lowered:
            lowered = lowered.rstrip('0').rstrip('.')
        return '0' if lowered in {'', '-0'} else lowered
    mantissa, exponent_text = lowered.split('e', 1)
    exponent = int(exponent_text)
    negative = mantissa.startswith('-')
    if negative:
        mantissa = mantissa[1:]
    whole, _dot, fraction = mantissa.partition('.')
    digits = whole + fraction
    decimal_position = len(whole) + exponent
    if decimal_position <= 0:
        output = '0.' + '0' * -decimal_position + digits
    elif decimal_position >= len(digits):
        output = digits + '0' * (decimal_position - len(digits))
    else:
        output = digits[:decimal_position] + '.' + digits[decimal_position:]
    if '.' in output:
        output = output.rstrip('0').rstrip('.')
    if negative and output != '0':
        output = '-' + output
    return output

def format_book_price(value: float, decimals: int | None=None) -> str:
    """Fixed-point price text without arbitrary-precision number allocation."""
    value = safe_float(value)
    if not value:
        precision = max(0, min(16, int(decimals or 0)))
        return f'{0.0:.{precision}f}' if decimals is not None else '0'
    if decimals is not None:
        precision = max(0, min(16, int(decimals)))
        return f'{value:.{precision}f}'
    return _general_to_fixed(format(value, '.12g'))

def _decimal_places_from_float(value: float) -> int:
    """Match normalized decimal-place semantics from Python's short float repr."""
    text = str(value).lower()
    mantissa, marker, exponent_text = text.partition('e')
    fraction = mantissa.partition('.')[2].rstrip('0')
    exponent = int(exponent_text) if marker else 0
    return max(0, len(fraction) - exponent)

def _decimal_places_from_step(value: object) -> int:
    """Precision implied by an exchange tick/step without Decimal allocation."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric <= 0.0 or not math.isfinite(numeric):
        return 0
    return min(16, _decimal_places_from_float(numeric))
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal
from ..models import OrderFlowSnapshot, OrderFlowTradePrint
from .ipc import SnapshotDecoder, SnapshotSeedRequired
from ..models import human_number
from ..utilities import (
    ElidedLabel, TextRole, apply_text_render_hints, device_pixel_value, set_text_role,
    typography_font, typography_font_at_pixel_size, typography_min_pixel_size,
    typography_state_opacity,
)

def _format_tape_quote(value: float) -> str:
    # Tiny fills retain their value instead of rounding to $0.00.
    return '$' + format_book_price(value) if 0 < abs(value) < 0.01 else human_number(value, money=True)


class _TradesTapeModel(QtCore.QAbstractTableModel):
    """Bounded rows; format only new/changed prints, paint only the viewport."""

    OUTCOMES = {
        'FOLLOW_THROUGH': ('✓', 'follow-through'),
        'REJECTED': ('×', 'did not meet the follow-through threshold'),
        'UNRESOLVED': ('…', 'pending'),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[OrderFlowTradePrint, tuple[str, ...]]] = []
        self.keys = []
        self._indices = {}
        self.formatted_rows = self.corrected_rows = self.resets = 0
        self.decimals: int | None = None
        self.value_mode = 'quote'
        self.amount_decimals = {'base': 0, 'quote': 2}
        self.buy = QtGui.QColor(ORDERBOOK_REFERENCE['bid'])
        self.sell = QtGui.QColor(ORDERBOOK_REFERENCE['ask'])
        self.muted = QtGui.QColor(ORDERBOOK_REFERENCE['muted'])
        self.text_color = QtGui.QColor(ORDERBOOK_REFERENCE['text'])

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ('PRICE', 'SIZE', 'TAG', 'TIME')[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter if section == 2
                       else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        trade, cells = self.rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return cells[column]
        if role == Qt.ItemDataRole.UserRole:
            return trade
        if role == Qt.ItemDataRole.FontRole and column == 2:
            return typography_font(TextRole.UI_GLYPH)
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == 0:
                return self.buy if trade.aggressor_side.upper() == 'BUY' else self.sell
            if column == 1:
                return self.text_color
            if column == 2 and trade.outcome in {'FOLLOW_THROUGH', 'REJECTED'}:
                direction = trade.outcome_direction
                if not direction and trade.outcome == 'FOLLOW_THROUGH':
                    direction = 1 if trade.aggressor_side.upper() == 'BUY' else -1
                return self.buy if direction > 0 else self.sell if direction < 0 else self.muted
            return self.muted
        if role == Qt.ItemDataRole.TextAlignmentRole:
            horizontal = (Qt.AlignmentFlag.AlignRight if column in (0, 1)
                          else Qt.AlignmentFlag.AlignHCenter if column == 2
                          else Qt.AlignmentFlag.AlignRight)
            return int(Qt.AlignmentFlag.AlignVCenter | horizontal)
        if role == Qt.ItemDataRole.ToolTipRole:
            outcome = self.OUTCOMES.get(trade.outcome, ('—', 'unknown'))[1]
            direction = 'up' if trade.outcome_direction > 0 else 'down' if trade.outcome_direction < 0 else 'flat'
            movement = f"\nPrice direction: {direction}" if trade.outcome in {'FOLLOW_THROUGH', 'REJECTED'} else ''
            return (f"{trade.aggressor_side.upper()} aggressor\nPrice: {cells[0]}\n"
                    f"Quantity: {format_book_price(trade.quantity)}\nValue: {_format_tape_quote(trade.notional)}\n"
                    f"UTC time: {cells[3]}\nRelative size: {trade.relative_size:.1f}×\n500 ms outcome: {outcome}{movement}")
        return None

    def _row(self, trade):
        self.formatted_rows += 1
        stamp = datetime.fromtimestamp(trade.event_time_ms / 1000.0, timezone.utc).strftime('%H:%M:%S') if trade.event_time_ms > 0 else '—'
        amount = trade.quantity if self.value_mode == 'base' else trade.notional
        prefix = '' if self.value_mode == 'base' else '$'
        size = prefix + format_book_price(amount, self.amount_decimals[self.value_mode])
        return trade, (format_book_price(trade.price, self.decimals), size,
                       self.OUTCOMES.get(trade.outcome, ('—', 'unknown'))[0], stamp)

    @staticmethod
    def identity(trade):
        # Local sequences restart when transport is paused or resynchronized.
        return trade.received_monotonic, trade.sequence

    def _precision(self, entries, *, force=False):
        mode, precision = self.value_mode, self.amount_decimals[self.value_mode]
        for key, trade in entries:
            amount = trade.quantity if mode == 'base' else trade.notional
            if mode == 'quote' and not 0 < abs(amount) < .01:
                continue
            row = self._indices.get(key)
            if row is not None and not force:
                old = self.rows[row][0]
                if amount == (old.quantity if mode == 'base' else old.notional):
                    continue
            precision = max(precision, len(format_book_price(amount).partition('.')[2]))
        changed = precision != self.amount_decimals[mode]
        self.amount_decimals[mode] = precision
        return changed

    def clear(self):
        self.beginResetModel()
        self.rows, self.keys, self._indices = [], [], {}
        self.endResetModel()

    def set_frame(self, frame, *, reformat=False):
        patch = frame.patch
        updates = dict(patch.upserts)
        seed = frame.base_revision is None
        if patch.order is None and not seed:
            if patch.removed or any(key not in self._indices for key in updates):
                raise TapeSeedRequired('Tape membership changed without an ordering')
            reformat |= self._precision(patch.upserts)
            if reformat:
                for key, trade in patch.upserts:
                    row = self._indices[key]
                    self.rows[row] = trade, self.rows[row][1]
                self.reformat_rows()
            else:
                self._correct_rows(patch.upserts)
            return
        order = list(patch.order) if patch.order is not None else self.keys
        ordered_keys = set(order)
        wanted = set(updates) if seed else (set(self.keys) - set(patch.removed)) | set(updates)
        if (len(order) > TAPE_CAPACITY or len(ordered_keys) != len(order) or ordered_keys != wanted
                or (seed and patch.order is None)
                or any(key not in updates and (seed or key not in self._indices) for key in order)
                or any(key not in ordered_keys for key in updates)):
            raise TapeSeedRequired('Incomplete trade-tape view')
        reformat |= self._precision(patch.upserts)
        previous = self.keys
        prefix = order.index(previous[0]) if previous and previous[0] in order else len(order)
        retained = len(order) - prefix
        same_tail = order[prefix:] == previous[:retained]
        if same_tail and any(key not in updates for key in order[:prefix]):
            # A valid reorder or head removal can move an unchanged resident
            # row into this prefix. Rebuild from retained rows in that case.
            same_tail = False
        if seed or reformat or not same_tail:
            trades = [updates[key] if key in updates else self.rows[self._indices[key]][0] for key in order]
            self.beginResetModel()
            self.rows = [self._row(trade) for trade in trades]
            self.keys = list(order)
            self._indices = {key: row for row, key in enumerate(self.keys)}
            self.resets += 1
            self.endResetModel()
            return
        if len(previous) > retained:
            self.beginRemoveRows(QtCore.QModelIndex(), retained, len(previous) - 1)
            del self.rows[retained:]
            del self.keys[retained:]
            self.endRemoveRows()
            if retained and not prefix:
                # This price now has no older neighbor; its emphasis changed.
                self.dataChanged.emit(self.index(retained - 1, 0), self.index(retained - 1, 0))
        if prefix:
            self.beginInsertRows(QtCore.QModelIndex(), 0, prefix - 1)
            self.rows[:0] = [self._row(updates[key]) for key in order[:prefix]]
            self.keys[:0] = order[:prefix]
            self.endInsertRows()
        if patch.order is not None:
            self._indices = {key: row for row, key in enumerate(self.keys)}
        self._correct_rows(patch.upserts, prefix=prefix)

    def _correct_rows(self, entries, *, prefix=0):
        for key, trade in entries:
            row = self._indices[key]
            if row < prefix:
                continue
            old, cells = self.rows[row]
            numeric_change = any(getattr(old, name) != getattr(trade, name) for name in (
                'price', 'quantity', 'notional', 'event_time_ms', 'aggressor_side'))
            if numeric_change:
                self.rows[row] = self._row(trade)
            else:
                tag = self.OUTCOMES.get(trade.outcome, ('—', 'unknown'))[0]
                self.rows[row] = trade, (cells[0], cells[1], tag, cells[3])
            self.corrected_rows += 1
            self.dataChanged.emit(self.index(row, 0 if numeric_change else 2),
                                  self.index(row, 3 if numeric_change else 2))
            if row > 0 and old.price != trade.price:
                # The preceding displayed price compares its digits with this row.
                self.dataChanged.emit(self.index(row - 1, 0), self.index(row - 1, 0))

    def reformat_rows(self):
        self.beginResetModel()
        self.rows = [self._row(trade) for trade, _cells in self.rows]
        self.resets += 1
        self.endResetModel()


class _TradePriceDelegate(QtWidgets.QStyledItemDelegate):
    """Bounded layouts for changed prices and fixed amount unit hierarchy."""

    LAYOUT_CACHE_LIMIT = 128

    def __init__(self, parent=None, *, amount=False):
        super().__init__(parent)
        self._amount = bool(amount)
        self._layouts = OrderedDict()
        self._masks = OrderedDict()
        self._refresh_typography()
        typography_controller().changed.connect(self._refresh_typography)

    def _refresh_typography(self):
        regular_state = 'trade_amount_fraction' if self._amount else 'trade_price_regular'
        changed_state = 'trade_amount_units' if self._amount else 'trade_price_changed'
        self._regular_font = typography_font(TextRole.TABLE_VALUE, state=regular_state)
        self._changed_font = typography_font(TextRole.TABLE_VALUE, state=changed_state)
        self._font_keys = (self._regular_font.key(), self._changed_font.key())
        self._layouts.clear()
        parent = self.parent()
        if isinstance(parent, QtWidgets.QAbstractItemView):
            parent.viewport().update()

    @staticmethod
    def emphasis_mask(price: str, previous: str) -> tuple[bool, ...]:
        """Emphasize from the first differing place through the end of the price."""
        if not previous:
            return (True,) * len(price)
        point = price.find('.') if '.' in price else len(price)
        previous_point = previous.find('.') if '.' in previous else len(previous)
        offset = previous_point - point
        if offset > 0 and any('1' <= digit <= '9' for digit in previous[:offset]):
            return (True,) * len(price)
        for position, digit in enumerate(price):
            reference = position + offset
            older = previous[reference] if 0 <= reference < len(previous) else '0'
            if '0' <= digit <= '9' and digit != older:
                return (False,) * position + (True,) * (len(price) - position)
        return (False,) * len(price)

    @staticmethod
    def amount_emphasis_mask(text: str) -> tuple[bool, ...]:
        """Kraken-style amount hierarchy: major units and point, quieter fractions."""
        point = text.find('.') if '.' in text else len(text)
        return tuple((position <= point and (digit.isdigit() or digit in '.,'))
                     or digit in 'KMBT' for position, digit in enumerate(text))

    def _layout(self, text, mask, regular, changed, device, color):
        regular_opacity = typography_state_opacity('trade_amount_fraction' if self._amount else 'trade_price_regular')
        changed_opacity = typography_state_opacity('trade_amount_units' if self._amount else 'trade_price_changed')
        font_keys = self._font_keys if regular is self._regular_font and changed is self._changed_font else (regular.key(), changed.key())
        key = (text, mask, *font_keys,
               device.logicalDpiX(), device.logicalDpiY(), device.devicePixelRatioF(),
               color.rgba(), regular_opacity, changed_opacity)
        cached = self._layouts.get(key)
        if cached is not None:
            self._layouts.move_to_end(key)
            return cached
        layout = QtGui.QTextLayout(text, regular, device)
        layout.setCacheEnabled(True)
        dim_color = QtGui.QColor(color)
        dim_color.setAlphaF(color.alphaF() * regular_opacity)
        bright_color = QtGui.QColor(color)
        bright_color.setAlphaF(color.alphaF() * changed_opacity)
        formats = []
        start = 0
        for position in range(1, len(text) + 1):
            if position < len(text) and mask[position] == mask[start]:
                continue
            span = QtGui.QTextLayout.FormatRange()
            span.start, span.length = start, position - start
            span.format.setFont(changed if mask[start] else regular)
            span.format.setForeground(bright_color if mask[start] else dim_color)
            formats.append(span)
            start = position
        layout.setFormats(formats)
        layout.beginLayout()
        line = layout.createLine()
        if line.isValid():
            line.setLineWidth(1e9)
        layout.endLayout()
        cached = layout, line
        self._layouts[key] = cached
        if len(self._layouts) > self.LAYOUT_CACHE_LIMIT:
            self._layouts.popitem(last=False)
        return cached

    def paint(self, painter, option, index):
        model, row, column = index.model(), index.row(), index.column()
        if isinstance(model, _TradesTapeModel) and 0 <= row < len(model.rows):
            # The model already owns formatted rows. Avoid repeated Qt model
            # role callbacks and temporary indexes for each painted cell.
            trade, cells = model.rows[row]
            text = cells[column]
            previous = model.rows[row + 1][1][column] if row + 1 < len(model.rows) else ''
            color = model.text_color if self._amount else model.buy if trade.aggressor_side.upper() == 'BUY' else model.sell
        else:
            text = str(index.data() or '')
            previous = str(model.index(row + 1, column).data() or '')
            color = index.data(Qt.ItemDataRole.ForegroundRole)
        mask_key = (text, '' if self._amount else previous)
        mask = self._masks.get(mask_key)
        if mask is None:
            mask = self.amount_emphasis_mask(text) if self._amount else self.emphasis_mask(text, previous)
            self._masks[mask_key] = mask
            if len(self._masks) > self.LAYOUT_CACHE_LIMIT:
                self._masks.popitem(last=False)
        else:
            self._masks.move_to_end(mask_key)
        rect = QtCore.QRectF(option.rect).adjusted(8, 0, -8, 0)
        if not text or rect.width() <= 0:
            return
        device = painter.device()
        regular, changed = self._regular_font, self._changed_font
        layout, line = self._layout(text, mask, regular, changed, device, color)
        if line.naturalTextWidth() > rect.width():
            pixels = regular.pixelSize() if regular.pixelSize() > 0 else round(regular.pointSizeF() * device.logicalDpiY() / 72)
            size = max(typography_min_pixel_size(TextRole.TABLE_VALUE),
                       int(pixels * rect.width() / line.naturalTextWidth()))
            if size < pixels and not self._amount:
                regular = typography_font_at_pixel_size(regular, size)
                changed = typography_font_at_pixel_size(changed, size)
                layout, line = self._layout(text, mask, regular, changed, device, color)
            if line.naturalTextWidth() > rect.width():
                elide_mode = Qt.TextElideMode.ElideRight if self._amount else Qt.TextElideMode.ElideLeft
                shown = QtGui.QFontMetricsF(changed, device).elidedText(text, elide_mode, int(rect.width()))
                if self._amount:
                    mask = self.amount_emphasis_mask(shown)
                else:
                    suffix_length = len(shown) - 1 if shown.startswith('…') else 0
                    mask = (False,) + mask[-suffix_length:] if suffix_length > 0 else (False,) * len(shown)
                layout, line = self._layout(shown, mask, regular, changed, device, color)
        if not line.isValid():
            return
        painter.save()
        try:
            painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
            painter.fillRect(option.rect, QtGui.QColor(ORDERBOOK_REFERENCE['bg']))
            painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
            apply_text_render_hints(painter)
            painter.setPen(color)
            x = rect.right() - line.naturalTextWidth()
            y = rect.top() + (rect.height() - line.height()) / 2
            x = device_pixel_value(device, x)
            y = device_pixel_value(device, y + line.ascent()) - line.ascent()
            layout.draw(painter, QtCore.QPointF(x, y))
        finally:
            painter.restore()


class TradesTapeWidget(QtWidgets.QWidget):
    """Visible incremental view of one worker-owned, shared trade history."""
    mode_changed = Signal(str)
    value_mode_changed = Signal(str)
    CAPACITY = TAPE_CAPACITY

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme = {}
        self.symbol = DEFAULT_SYMBOL
        self.quote_volume_24h = 0.0
        self.current_threshold = 1000.0
        self._mode, self._value_mode = 'LARGE', 'quote'
        self._active = False
        self._presentation_clock = None
        self._interaction_priority_active = False
        self._frame_refresh_pending = False
        self._tape_source = None
        self._owned_tape_source = None
        self._tape_consumer = None
        self._tape_token = 0
        self._tape_revision = None
        self._pending_tape_frame = None
        self._print_cache_hits = self._print_cache_misses = 0
        self._dirty = True
        self._reformat = False
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._refresh_timer.setInterval(display_frame_interval_ms(self))
        self._refresh_timer.timeout.connect(self._refresh_table)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(8, 4, 8, 4)
        controls.setSpacing(6)
        self.status = ElidedLabel('Waiting for trades')
        set_text_role(self.status, TextRole.UI_CAPTION)
        self.status.setToolTip('Adaptive minimum value from the existing trade analyzer. Times are UTC. History retains up to 500 prints per mode for the current market session.')
        controls.addWidget(self.status, 1)
        self.mode_button = QtWidgets.QToolButton(self)
        self.mode_button.setText('Large')
        self.mode_button.setAutoRaise(True)
        self.mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_button.setToolTip('Switch between large trades and all trades')
        self.mode_button.clicked.connect(self.toggle_mode)
        controls.addWidget(self.mode_button)
        self.units_button = QtWidgets.QToolButton(self)
        self.units_button.setText('Value')
        self.units_button.setCheckable(True)
        self.units_button.setAutoRaise(True)
        self.units_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.units_button.setAccessibleName('Trade size units: quote value or base quantity')
        self.units_button.setToolTip('Show size as quote value or base quantity')
        self.units_button.clicked.connect(self.toggle_value_mode)
        controls.addWidget(self.units_button)
        layout.addLayout(controls)
        self.table = QtWidgets.QTableView(self)
        self.table.setProperty('essentialToolTip', True)
        self.model = _TradesTapeModel(self.table)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(0, _TradePriceDelegate(self.table))
        self.table.setItemDelegateForColumn(1, _TradePriceDelegate(self.table, amount=True))
        set_text_role(self.table, TextRole.TABLE_VALUE)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(28)
        header = self.table.horizontalHeader()
        header.show()
        set_text_role(header, TextRole.UI_CAPTION)
        header.setMinimumSectionSize(0)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(False)
        self.empty = QtWidgets.QLabel('Waiting for large trades…', self.table.viewport())
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        set_text_role(self.empty, TextRole.UI_CAPTION)
        self.table.viewport().installEventFilter(self)
        layout.addWidget(self.table, 1)
        self.apply_theme(theme)
        self._size_columns()
        self._display_refresh_observer = DisplayRefreshObserver(self, self._display_refresh_changed)

    def _display_refresh_changed(self):
        self._refresh_timer.setInterval(display_frame_interval_ms(self))
        if self._tape_source is not None:
            # Preserve the token and pending frame when only pacing changes.
            self._tape_source.refresh(self)

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            self.empty.setGeometry(self.table.viewport().rect())
            self._size_columns()
        return super().eventFilter(watched, event)

    def _size_columns(self):
        metrics = self.table.fontMetrics()
        header = self.table.horizontalHeader()
        width = max(0, self.table.viewport().width())
        # Stable lanes with the spare room shared evenly. Neither numeric
        # column consumes all the space left by a cramped tag/time pair.
        natural = (80, 74, 36, metrics.horizontalAdvance('00:00:00') + 16)
        compact = width < 180
        self.table.setColumnHidden(2, compact)
        self.table.setColumnHidden(3, width < sum(natural))
        if width >= sum(natural):
            extra = (width - sum(natural)) / 4
            sizes = [round(value + extra) for value in natural]
            sizes[-1] = width - sum(sizes[:-1])
        elif compact:
            # Keep price and size readable before spending narrow-panel space
            # on secondary columns. Full details remain in each row's tooltip.
            price = round(width * .52)
            sizes = (price, width - price, 0, 0)
        else:
            tag = 36
            numeric = max(0, width - tag)
            price = round(numeric * .52)
            sizes = (price, numeric - price, tag, 0)
        for column, size in enumerate(sizes):
            header.resizeSection(column, size)
        header.setFixedHeight(max(24, header.fontMetrics().height() + 8))
        self.table.verticalHeader().setDefaultSectionSize(max(26, metrics.height() + 9))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._size_columns()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'table') and event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.ApplicationFontChange):
            self._size_columns()

    def set_market(self, symbol: str, quote_volume_24h: float = 0.0, *, tick_size: float = 0.0):
        symbol = str(symbol).upper()
        if symbol != self.symbol:
            self.symbol = symbol
            # Market reset belongs to the producer. Clearing a newly selected
            # market from a delayed view update would erase its accepted trades.
            self.reset(clear_source=False)
        self.quote_volume_24h = max(0.0, quote_volume_24h)
        decimals = _decimal_places_from_step(tick_size) if tick_size > 0 else None
        if decimals != self.model.decimals:
            self.model.decimals = decimals
            self._reformat = self._dirty = True
            self._schedule_refresh()

    def set_quote_volume(self, quote_volume_24h):
        self.quote_volume_24h = max(0.0, quote_volume_24h)

    def reset(self, *, preserve_history=False, clear_source=True):
        self._refresh_timer.stop()
        self._frame_refresh_pending = False
        self._pending_tape_frame = None
        self._tape_revision = None
        self._dirty = True
        if not preserve_history:
            if clear_source and self._tape_source is not None:
                self._tape_source.command('clear_tape', (self.symbol,))
            self.current_threshold = 1000.0
            self.model.amount_decimals = {'base': 0, 'quote': 2}
            self.model.clear()
            self.status.setText('Waiting for trades')
            self.empty.setText('Waiting for large trades…' if self._mode == 'LARGE' else 'Waiting for trades…')
            self.empty.show()
        self._request_tape_view()

    def set_tape_source(self, source):
        if source is self._tape_source:
            return
        if self._tape_source is not None:
            self._tape_source.unregister(self._tape_consumer)
        if self._owned_tape_source is not None:
            self._owned_tape_source.close()
            self._owned_tape_source.deleteLater()
            self._owned_tape_source = None
        self._tape_source = source
        self._tape_consumer = source.register(self) if source is not None else None
        self.reset(clear_source=False)

    def _request_tape_view(self):
        self._tape_token += 1
        self._pending_tape_frame = None
        self._frame_refresh_pending = False
        if self._tape_source is not None:
            self._tape_source.refresh(self)

    def set_panel_active(self, active):
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if not self._active:
            self._refresh_timer.stop()
            self._frame_refresh_pending = False
        else:
            self._schedule_refresh()
        self._request_tape_view()

    def showEvent(self, event):
        super().showEvent(event)
        self._request_tape_view()
        self._schedule_refresh()

    def hideEvent(self, event):
        self._refresh_timer.stop()
        self._frame_refresh_pending = False
        super().hideEvent(event)
        self._request_tape_view()

    def set_presentation_clock(self, clock):
        if clock is self._presentation_clock:
            return
        if self._presentation_clock is not None:
            self._presentation_clock.interaction_frame.disconnect(self._commit_frame_refresh)
        self._presentation_clock = clock
        if clock is not None:
            clock.interaction_frame.connect(self._commit_frame_refresh)
        elif self._frame_refresh_pending:
            self._commit_frame_refresh()

    def set_interaction_priority(self, active):
        self._interaction_priority_active = bool(active)
        if not active and self._frame_refresh_pending:
            self._commit_frame_refresh()

    def _commit_frame_refresh(self, _frame_time=0.0):
        if not self._frame_refresh_pending:
            return
        self._frame_refresh_pending = False
        self._commit_table_refresh()

    def mode(self):
        return self._mode

    def set_mode(self, mode, *, emit=True):
        mode = 'ALL' if str(mode).upper() == 'ALL' else 'LARGE'
        if mode == self._mode:
            return
        self._mode = mode
        self.mode_button.setText('All' if mode == 'ALL' else 'Large')
        self._reformat = self._dirty = True
        # Rows and revisions belong to one mode. A delayed seed must not leave
        # the previous mode's prints under the newly selected filter.
        self.reset(clear_source=False)
        self._schedule_refresh()
        if emit:
            self.mode_changed.emit(mode)

    def toggle_mode(self):
        self.set_mode('ALL' if self._mode == 'LARGE' else 'LARGE')

    def value_mode(self):
        return self._value_mode

    def toggle_value_mode(self):
        self.set_value_mode('quote' if self._value_mode == 'base' else 'base')

    def set_value_mode(self, mode, *, emit=True):
        mode = 'base' if str(mode).lower() == 'base' else 'quote'
        self.units_button.setText('Qty' if mode == 'base' else 'Value')
        with QtCore.QSignalBlocker(self.units_button):
            self.units_button.setChecked(mode == 'base')
        if mode != self._value_mode:
            self._value_mode = self.model.value_mode = mode
            # A unit switch can expose amounts that were never measured in
            # that mode. Inspect retained rows once, even without a new frame.
            self.model._precision(zip(self.model.keys, (row[0] for row in self.model.rows)),
                                  force=True)
            self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 1, 1)
            self._reformat = self._dirty = True
            self._schedule_refresh()
            if emit:
                self.value_mode_changed.emit(mode)

    def set_order_flow_snapshot(self, snapshot):
        if not isinstance(snapshot, OrderFlowSnapshot) or snapshot.symbol != self.symbol:
            return
        if self._tape_source is None:
            source = SharedTradeTapeSource(symbol=self.symbol, parent=self)
            self.set_tape_source(source)
            self._owned_tape_source = source
        self._tape_source.ingest_snapshot(snapshot)

    def _receive_tape_frame(self, frame):
        if (frame.consumer != self._tape_consumer or frame.token != self._tape_token
                or frame.symbol != self.symbol or frame.mode != self._mode
                or not self._active or not self.isVisible()):
            return False
        if frame.base_revision is not None and frame.base_revision != self._tape_revision:
            self._request_tape_view()  # Re-subscription requests a complete seed.
            return False
        self._pending_tape_frame = frame
        self.current_threshold = frame.threshold
        self._dirty = True
        self._refresh_table()
        return True

    def _schedule_refresh(self):
        if (self._dirty and self._active and self.isVisible()
                and not self._frame_refresh_pending and not self._refresh_timer.isActive()):
            self._refresh_timer.start()

    def _refresh_table(self):
        if not self._active or not self.isVisible() or not self._dirty:
            return
        if self._interaction_priority_active and self._presentation_clock is not None:
            self._frame_refresh_pending = True
            self._presentation_clock.request()
            return
        self._commit_table_refresh()

    def _commit_table_refresh(self):
        if not self._active or not self.isVisible() or not self._dirty:
            return
        self._refresh_timer.stop()
        frame = self._pending_tape_frame
        if frame is None and self._tape_revision is None and not self.model.rows:
            return  # Keep the waiting state until this view receives its seed.
        bar = self.table.verticalScrollBar()
        follow = bar.value() == 0
        top = self.table.rowAt(0)
        anchor = self.model.keys[top] if 0 <= top < len(self.model.keys) else None
        offset = self.table.rowViewportPosition(top) if top >= 0 else 0
        if frame is not None:
            try:
                self.model.set_frame(frame, reformat=self._reformat)
            except TapeSeedRequired:
                self._request_tape_view()
                return
            self._tape_revision = frame.revision
            self._pending_tape_frame = None
            self._print_cache_misses += 1
            self._tape_source.command('ack_tape', (self._tape_consumer, self._tape_token, frame.revision))
        elif self._reformat:
            self.model.reformat_rows()
        self._dirty = self._reformat = False
        if follow:
            self.table.scrollToTop()
        elif anchor is not None:
            row = self.model._indices.get(anchor)
            if row is not None:
                self.table.scrollTo(self.model.index(row, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
                bar.setValue(bar.value() - offset)
        self.empty.setVisible(not self.model.rows)
        self.status.setText(f'≥ {human_number(self.current_threshold, money=True)}' if self._mode == 'LARGE' else f'{len(self.model.rows)} trades')

    def apply_theme(self, theme):
        self.theme = {}
        p = ORDERBOOK_REFERENCE
        self.setStyleSheet(
            f"QWidget {{ background: {p['bg']}; color: {p['text']}; border: 0; }}"
            f"QTableView {{ background: {p['bg']}; color: {p['text']}; border: 0; }}"
            f"QHeaderView::section {{ background: {p['bg']}; color: {p['muted']}; border: 0; padding: 3px 8px; }}"
            f"QTableView::item {{ border: 0; padding: 3px 8px; }}"
            f"QToolButton {{ background: {p['surface_raised']}; color: {p['text']}; border: 1px solid {p['grid_strong']}; border-radius: 3px; padding: 3px 8px; }}"
            f"QToolButton:hover {{ background: {p['control_hover']}; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 3px; margin: 0; }}"
            f"QScrollBar::handle:vertical {{ background: {p['grid']}; min-height: 24px; border-radius: 1px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}"
        )

from ..models import ORDER_FLOW_AGGREGATION_MULTIPLIERS
from ..utilities import TextRole, typography_controller, typography_font, typography_font_at_pixel_size, typography_min_pixel_size

class _OrderBookSurfaceButton(QtWidgets.QPushButton):
    """Keyboard-accessible control with a clear selected state."""
    BUTTON_HEIGHT = 28
    HORIZONTAL_PADDING = 6
    MIN_CONTENT_WIDTH = 26

    def __init__(self, text, theme, parent=None, *, compact_width=None):
        super().__init__(text, parent)
        self._compact_width = compact_width
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFixedHeight(self.BUTTON_HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.set_theme(theme)

    def set_theme(self, theme):
        p = ORDERBOOK_REFERENCE
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {p['muted']}; "
            f"border: 1px solid {p['control_border']}; border-radius: 5px; padding: 0 {self.HORIZONTAL_PADDING}px; }}"
            f"QPushButton:hover {{ color: {p['text']}; background: {p['control_hover']}; border-color: {p['control_hover_line']}; }}"
            f"QPushButton:pressed {{ background: {p['control_pressed']}; }}"
            f"QPushButton:checked {{ color: {p['text']}; background: {p['control_hover']}; "
            f"border-color: {p['control_hover_line']}; }}"
            f"QPushButton:focus {{ border-color: {p['mid']}; }}"
            f"QPushButton:disabled {{ color: #454B53; background: transparent; border-color: {p['grid']}; }}"
            f"QPushButton#orderBookViewChoice {{ border-color: transparent; }}"
            f"QPushButton#orderBookViewChoice:checked {{ background: {p['grid']}; }}"
            f"QPushButton#orderBookViewChoice:focus {{ border-color: {p['control_hover_line']}; }}"
        )

    def minimumSizeHint(self):
        width = self.fontMetrics().horizontalAdvance(self.text()) + 2 * self.HORIZONTAL_PADDING + 2
        if not self.icon().isNull():
            width += self.iconSize().width() + (4 if self.text() else 0)
        return QtCore.QSize(max(self.MIN_CONTENT_WIDTH, int(self._compact_width or 0), width), self.BUTTON_HEIGHT)

    def sizeHint(self):
        return self.minimumSizeHint()


class OrderBookControlBar(QtWidgets.QFrame):
    """Visible controls that keep price grouping and heatmap range within reach."""
    aggregation_selected = Signal(int)
    density_selected = Signal(str)
    value_mode_selected = Signal(str)
    book_depth_toggled = Signal(bool)
    depth_range_selected = Signal(float)
    auto_grouping_toggled = Signal(bool)
    reset_requested = Signal()
    display_option_toggled = Signal(str, bool)
    CONTROL_BAR_HEIGHT = 94
    _DENSITIES = ('compact', 'normal', 'relaxed')
    _DENSITY_NAMES = {'compact': 'Compact', 'normal': 'Balanced', 'relaxed': 'Spacious'}

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = {}
        self._tick_size, self._aggregation = 0.0, 1
        self._book_depth, self._density, self._value_mode = True, 'normal', 'base'
        self._auto_grouping, self._depth_range = True, 0.72
        self._tape_enabled, self._tape_mode = False, 'LARGE'
        self._responsive_layout_state = None
        self.setObjectName('orderBookControlBar')
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        root = QtWidgets.QVBoxLayout(self)
        root.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)
        self._layout = root
        self._top_layout = QtWidgets.QHBoxLayout()
        self._top_layout.setSpacing(4)
        root.addLayout(self._top_layout)

        self._view_container, view_layout = self._make_group()
        self._view_container.setObjectName('orderBookViewSegment')
        self._view_group = QtWidgets.QButtonGroup(self)
        self.depth_button = self._toggle_button('Heatmap', 'Heatmap and cumulative depth')
        self.ladder_button = self._toggle_button('Ladder', 'Price ladder and liquidity analytics')
        self.depth_button.setAccessibleName('Show liquidity heatmap')
        self.ladder_button.setAccessibleName('Show price ladder')
        for button in (self.depth_button, self.ladder_button):
            button.setObjectName('orderBookViewChoice')
            self._view_group.addButton(button)
            view_layout.addWidget(button)
        self.depth_button.toggled.connect(self.book_depth_toggled.emit)
        self._top_layout.addWidget(self._view_container)
        self._view_menu = QtWidgets.QMenu(self)
        self._view_action_group = QtGui.QActionGroup(self)
        self._view_actions = {}
        for enabled, name in ((True, 'Heatmap'), (False, 'Price ladder')):
            action = self._view_menu.addAction(name)
            action.setCheckable(True)
            self._view_action_group.addAction(action)
            action.triggered.connect(lambda checked=False, value=enabled: self.book_depth_toggled.emit(value) if checked else None)
            self._view_actions[enabled] = action
        self.view_button = self._plain_button('Heatmap ▾', 'Choose orderbook view')
        self.view_button.clicked.connect(lambda: self._popup(self._view_menu, self.view_button))
        self._top_layout.addWidget(self.view_button)
        self._top_layout.addStretch(1)
        self.value_mode_button = self._plain_button('Qty', 'Switch between base quantity and USDT value')
        self.value_mode_button.clicked.connect(self._toggle_value_mode)
        self._top_layout.addWidget(self.value_mode_button)
        self.settings_button = self._plain_button('···', 'Orderbook display options')
        self.settings_button.setAccessibleName('Orderbook display options')
        self._top_layout.addWidget(self.settings_button)

        self._step_layout = QtWidgets.QHBoxLayout()
        self._step_layout.setSpacing(4)
        root.addLayout(self._step_layout)
        self.step_label = QtWidgets.QLabel('Step', self)
        set_text_role(self.step_label, TextRole.ORDERBOOK_LABEL)
        self.step_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self._step_layout.addWidget(self.step_label)
        self.step_down_button = self._plain_button('−', 'Use a smaller price step', compact_width=26)
        self.step_down_button.setAccessibleName('Decrease price grouping')
        self.step_down_button.clicked.connect(lambda: self._change_aggregation(-1))
        self._step_layout.addWidget(self.step_down_button)
        self._aggregation_menu = QtWidgets.QMenu('Price grouping', self)
        self._aggregation_actions = {}
        self._aggregation_action_group = QtGui.QActionGroup(self)
        self._aggregation_action_group.setExclusive(True)
        for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
            value = int(multiplier)
            action = self._aggregation_menu.addAction(f'{value}× exchange tick')
            action.setCheckable(True)
            self._aggregation_action_group.addAction(action)
            action.triggered.connect(lambda checked=False, selected=value: self.aggregation_selected.emit(selected) if checked else None)
            self._aggregation_actions[value] = action
        self.aggregation_button = self._plain_button('1× ▾', 'Choose the price step')
        self.aggregation_button.setAccessibleName('Choose price grouping')
        self.aggregation_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.aggregation_button.setMinimumWidth(44)
        self.aggregation_button.setMaximumWidth(150)
        self.aggregation_button.clicked.connect(lambda: self._popup(self._aggregation_menu, self.aggregation_button))
        self._step_layout.addWidget(self.aggregation_button, 1)
        self.step_up_button = self._plain_button('+', 'Use a larger price step', compact_width=26)
        self.step_up_button.setAccessibleName('Increase price grouping')
        self.step_up_button.clicked.connect(lambda: self._change_aggregation(1))
        self._step_layout.addWidget(self.step_up_button)
        self.auto_button = self._toggle_button('Auto', 'Choose the price step automatically for this market')
        self.auto_button.setAccessibleName('Automatic price grouping')
        self.auto_button.toggled.connect(self.auto_grouping_toggled.emit)
        self._step_layout.addWidget(self.auto_button)
        self._step_layout.addStretch(1)
        self._density_menu = QtWidgets.QMenu('Row spacing', self)
        self._density_actions = {}
        self._density_action_group = QtGui.QActionGroup(self)
        self._density_action_group.setExclusive(True)
        for density in self._DENSITIES:
            action = self._density_menu.addAction(self._DENSITY_NAMES[density])
            action.setCheckable(True)
            self._density_action_group.addAction(action)
            action.triggered.connect(lambda checked=False, selected=density: self.density_selected.emit(selected) if checked else None)
            self._density_actions[density] = action
        self.density_button = self._plain_button('Rows ▾', 'Choose row spacing')
        self.density_button.setAccessibleName('Choose row spacing')
        self.density_button.clicked.connect(lambda: self._popup(self._density_menu, self.density_button))
        self._step_layout.addWidget(self.density_button)

        self._range_container = QtWidgets.QWidget(self)
        range_layout = QtWidgets.QHBoxLayout(self._range_container)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(6)
        self.range_label = QtWidgets.QLabel('Range', self)
        set_text_role(self.range_label, TextRole.ORDERBOOK_LABEL)
        range_layout.addWidget(self.range_label)
        self.range_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal, self)
        self.range_slider.setRange(5, 95)
        self.range_slider.setSingleStep(1)
        self.range_slider.setPageStep(8)
        self.range_slider.setFixedHeight(22)
        self.range_slider.setMinimumWidth(32)
        self.range_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.range_slider.setAccessibleName('Heatmap depth range')
        self.range_slider.setAccessibleDescription('Adjust the two rulers and the range used to scale liquidity.')
        self.range_slider.valueChanged.connect(lambda value: self.depth_range_selected.emit(value / 100.0))
        range_layout.addWidget(self.range_slider, 1)
        self.range_value = QtWidgets.QLabel('72%', self)
        self.range_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.range_value.setFixedWidth(32)
        set_text_role(self.range_value, TextRole.ORDERBOOK_FOOTER_VALUE)
        range_layout.addWidget(self.range_value)
        root.addWidget(self._range_container)

        self._display_menu = QtWidgets.QMenu(self)
        self._display_menu.addSection('Display')
        self._display_menu.addMenu(self._view_menu).setText('View')
        self._display_menu.addMenu(self._density_menu)
        self.units_action = self._display_menu.addAction('Show base-asset quantities')
        self.units_action.setCheckable(True)
        self.units_action.triggered.connect(lambda checked: self.value_mode_selected.emit('base' if checked else 'quote'))
        self.auto_action = self._display_menu.addAction('Automatic price grouping')
        self.auto_action.setCheckable(True)
        self.auto_action.triggered.connect(self.auto_grouping_toggled.emit)
        self._analytics_menu = self._display_menu.addMenu('Price-level analytics')
        self._lane_actions = {}
        for key, label in (('flow', 'Aggressor flow · 5s'), ('delta', 'Liquidity change · 5s'), ('state', 'Liquidity signals'), ('memory', 'Liquidity history · 30s')):
            action = self._analytics_menu.addAction(label)
            action.setCheckable(True)
            action.toggled.connect(lambda enabled, lane=key: self.display_option_toggled.emit(lane, enabled))
            self._lane_actions[key] = action
        self.tape_action = self._display_menu.addAction('Show trade tape when space allows')
        self.tape_action.setCheckable(True)
        self.tape_action.toggled.connect(lambda enabled: self.display_option_toggled.emit('tape', enabled))
        self._display_menu.addSeparator()
        reset_action = self._display_menu.addAction('Reset orderbook display')
        reset_action.triggered.connect(lambda _checked=False: self.reset_requested.emit())
        self.settings_button.clicked.connect(lambda: self._popup(self._display_menu, self.settings_button))
        self.apply_theme(theme)
        self._refresh_typography()
        typography_controller().changed.connect(self._refresh_typography)
        self._refresh_text()

    def _make_group(self):
        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)
        return container, layout

    def _plain_button(self, text, tooltip='', *, compact_width=None):
        button = _OrderBookSurfaceButton(text, self.theme, self, compact_width=compact_width)
        button.setToolTip(tooltip)
        return button

    def _toggle_button(self, text, tooltip='', *, compact_width=None):
        button = self._plain_button(text, tooltip, compact_width=compact_width)
        button.setCheckable(True)
        return button

    @staticmethod
    def _popup(menu, button):
        menu.popup(button.mapToGlobal(QtCore.QPoint(0, button.height() + 4)))

    def _toggle_value_mode(self):
        self.value_mode_selected.emit('base' if self._value_mode == 'quote' else 'quote')

    def _change_aggregation(self, direction):
        values = tuple(ORDER_FLOW_AGGREGATION_MULTIPLIERS)
        index = values.index(self._aggregation) if self._aggregation in values else 0
        selected = values[max(0, min(len(values) - 1, index + int(direction)))]
        self.aggregation_selected.emit(int(selected))

    def _refresh_typography(self):
        for button in self.findChildren(_OrderBookSurfaceButton):
            button.setFont(typography_font(TextRole.ORDERBOOK_CONTROL))
            button.updateGeometry()
        for menu in self.findChildren(QtWidgets.QMenu):
            menu.setFont(typography_font(TextRole.ORDERBOOK_CONTROL))
        self._responsive_layout_state = None
        self._refresh_responsive_layout(self.width())

    def apply_theme(self, theme):
        p = ORDERBOOK_REFERENCE
        self.setStyleSheet(
            f"QFrame#orderBookControlBar {{ background: {p['bg']}; border: 0; border-bottom: 1px solid {p['grid']}; }}"
            f"QWidget#orderBookViewSegment {{ background: {p['bg']}; border: 1px solid {p['grid_strong']}; border-radius: 6px; }}"
            f"QLabel {{ color: {p['muted']}; background: transparent; border: 0; }}"
            f"QSlider {{ background: transparent; border: 0; }}"
            f"QSlider::groove:horizontal {{ background: {p['grid_strong']}; height: 3px; border-radius: 1px; }}"
            f"QSlider::sub-page:horizontal {{ background: {p['control_hover_line']}; border-radius: 1px; }}"
            f"QSlider::handle:horizontal {{ background: {p['text']}; border: 2px solid {p['bg']}; width: 12px; margin: -6px 0; border-radius: 7px; }}"
            f"QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{ background: #FFFFFF; border-color: {p['control_hover_line']}; }}"
        )
        for button in self.findChildren(_OrderBookSurfaceButton):
            button.set_theme({})
        for menu in self.findChildren(QtWidgets.QMenu):
            menu.setStyleSheet(
                f"QMenu {{ background: {p['bg']}; color: {p['text']}; border: 1px solid {p['grid_strong']}; padding: 5px; }}"
                f"QMenu::item {{ padding: 7px 24px 7px 12px; border-radius: 4px; }}"
                f"QMenu::item:selected {{ background: {p['grid']}; color: {p['text']}; }}"
                f"QMenu::separator {{ height: 1px; background: {p['grid']}; margin: 5px 6px; }}"
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_responsive_layout(event.size().width())

    def _refresh_responsive_layout(self, width):
        # Narrow docks keep all controls reachable; the view menu and display
        # menu take over only when the corresponding buttons cannot fit.
        view_width = self._view_container.sizeHint().width()
        top_width = view_width + self.value_mode_button.sizeHint().width() + self.settings_button.sizeHint().width() + 24
        expanded_view = width >= max(220, top_width)
        short_view = width < 150
        self.view_button.setText('View ▾' if short_view else 'Heatmap ▾' if self._book_depth else 'Ladder ▾')
        show_units = expanded_view or width >= (self.view_button.sizeHint().width()
            + self.value_mode_button.sizeHint().width() + self.settings_button.sizeHint().width() + 24)
        show_range_label = width >= (self.range_label.sizeHint().width()
            + self.range_slider.minimumWidth() + self.range_value.width() + 24)
        auto_width = self.auto_button.sizeHint().width() + 4 if self._book_depth else 0
        step_width = max(44, min(150, self.aggregation_button.minimumSizeHint().width(),
                                max(44, width - 12 - auto_width)))
        self.aggregation_button.setMinimumWidth(step_width)
        required = step_width + 12
        # Keep grouping controls ahead of row spacing on narrow docks.
        show_auto = self._book_depth and width >= required + auto_width
        if show_auto:
            required += auto_width
        step_buttons_width = self.step_down_button.sizeHint().width() + self.step_up_button.sizeHint().width() + 8
        show_step_buttons = width >= required + step_buttons_width
        if show_step_buttons:
            required += step_buttons_width
        show_rows = width >= required + self.density_button.sizeHint().width() + 4
        if show_rows:
            required += self.density_button.sizeHint().width() + 4
        show_step_label = width >= required + self.step_label.sizeHint().width() + 4
        state = (expanded_view, show_units, show_range_label, short_view, show_auto, show_step_label,
                 show_step_buttons, show_rows, step_width, self._book_depth)
        if state == self._responsive_layout_state:
            return
        self._responsive_layout_state = state
        self._view_container.setVisible(expanded_view)
        self.view_button.setVisible(not expanded_view)
        self.value_mode_button.setVisible(show_units)
        self.range_label.setVisible(show_range_label)
        self.auto_button.setVisible(show_auto)
        self.step_label.setVisible(show_step_label)
        self.step_down_button.setVisible(show_step_buttons)
        self.step_up_button.setVisible(show_step_buttons)
        self.density_button.setVisible(show_rows)
        self._range_container.setVisible(self._book_depth)
        self.setFixedHeight(self.CONTROL_BAR_HEIGHT if self._book_depth else 68)

    @staticmethod
    def _set_checked_without_signal(button, checked):
        blocker = QtCore.QSignalBlocker(button)
        button.setChecked(bool(checked))
        del blocker

    def set_state(self, *, aggregation, tick_size, preset, density, value_mode, tape_enabled, tape_mode,
                  book_depth=False, overlays=None, depth_range=0.72, auto_grouping=True, range_available=True):
        self._aggregation = max(1, int(aggregation))
        self._tick_size = max(0.0, float(tick_size))
        self._density, self._value_mode = str(density or 'normal'), 'base' if value_mode == 'base' else 'quote'
        self._tape_enabled, self._tape_mode, self._book_depth = bool(tape_enabled), str(tape_mode), bool(book_depth)
        self._depth_range = max(0.05, min(0.95, float(depth_range)))
        self._auto_grouping = bool(auto_grouping)
        for key, action in self._lane_actions.items():
            self._set_checked_without_signal(action, bool((overlays or {}).get(key, True)))
            action.setEnabled(not self._book_depth)
        self._analytics_menu.menuAction().setVisible(not self._book_depth)
        self.auto_action.setEnabled(self._book_depth)
        self._refresh_text()
        self.range_slider.setEnabled(bool(range_available))
        self.range_slider.setToolTip('Adjust the range used to scale liquidity.' if range_available
                                     else 'Range becomes available when more price levels are shown.')
        if not range_available:
            self.range_value.setText('—')
        self._refresh_responsive_layout(self.width())

    def _refresh_text(self):
        blockers = [QtCore.QSignalBlocker(button) for button in (self.ladder_button, self.depth_button)]
        self.ladder_button.setChecked(not self._book_depth)
        self.depth_button.setChecked(self._book_depth)
        del blockers
        for enabled, action in self._view_actions.items():
            self._set_checked_without_signal(action, enabled == self._book_depth)
        self.view_button.setText('Heatmap ▾' if self._book_depth else 'Ladder ▾')
        for multiplier, action in self._aggregation_actions.items():
            effective = format_book_price(self._tick_size * multiplier) if self._tick_size else 'exchange tick'
            self._set_checked_without_signal(action, multiplier == self._aggregation)
            action.setText(f'{effective} · {multiplier}×')
        effective = format_book_price(self._tick_size * self._aggregation) if self._tick_size else f'{self._aggregation}×'
        self.aggregation_button.setText(f'{effective} ▾')
        self.aggregation_button.setAccessibleDescription(f'Current price step: {effective}')
        self.step_down_button.setEnabled(self._aggregation > ORDER_FLOW_AGGREGATION_MULTIPLIERS[0])
        self.step_up_button.setEnabled(self._aggregation < ORDER_FLOW_AGGREGATION_MULTIPLIERS[-1])
        self.value_mode_button.setText('Qty' if self._value_mode == 'base' else 'USDT')
        self.value_mode_button.setAccessibleName('Switch to USDT value' if self._value_mode == 'base' else 'Switch to base quantity')
        self._set_checked_without_signal(self.units_action, self._value_mode == 'base')
        self._set_checked_without_signal(self.tape_action, self._tape_enabled)
        self._set_checked_without_signal(self.auto_button, self._auto_grouping)
        self._set_checked_without_signal(self.auto_action, self._auto_grouping)
        with QtCore.QSignalBlocker(self.range_slider):
            self.range_slider.setValue(round(self._depth_range * 100))
        self.range_value.setText(f'{round(self._depth_range * 100)}%')
        for key, action in self._density_actions.items():
            self._set_checked_without_signal(action, key == self._density)
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, replace
from typing import ClassVar
from PySide6.QtCore import QTimer, Qt, Signal
from ..models import DomPositionOverlay, OrderFlowPresentationFrame
from ..models import ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowDisplayLevel, OrderFlowSnapshot
from ..models import human_number, safe_float
from ..utilities import TextRole, apply_text_render_hints, device_pixel_rect, device_pixel_value, typography_controller, typography_font

# Keep the depth-profile motion contract identical to the visual reference.
# Only rendered profile values are interpolated; market state remains immediate.
PROFILE_VALUE_SMOOTHING_TIME_CONSTANT = 0.085
PROFILE_VALUE_SETTLE_RELATIVE = 0.0001


def _profile_value_tolerance(target: float) -> float:
    return max(1e-8, abs(target) * PROFILE_VALUE_SETTLE_RELATIVE)


def _profile_values_need_animation(
    current: dict[tuple[str, float], float],
    targets: dict[tuple[str, float], float],
) -> bool:
    for key, target in targets.items():
        if abs(target - current.get(key, target)) > _profile_value_tolerance(target):
            return True
    return False


def _smooth_profile_value_map(
    current: dict[tuple[str, float], float],
    targets: dict[tuple[str, float], float],
    elapsed: float,
) -> bool:
    if not targets:
        return False

    dt = max(0.001, min(0.050, float(elapsed)))
    alpha = 1.0 - math.exp(-dt / PROFILE_VALUE_SMOOTHING_TIME_CONSTANT)
    moving = False

    for key, target in targets.items():
        value = current.get(key, target)
        delta = target - value
        tolerance = _profile_value_tolerance(target)
        if abs(delta) <= tolerance:
            current[key] = target
            continue
        current[key] = value + delta * alpha
        moving = True

    return moving


@dataclass(slots=True)
class PreparedDomRow:
    """Typed, presentation-only state for one visible DOM price row.

    The immutable :class:`OrderFlowDisplayLevel` remains the analytical source of
    truth. Unlike the previous open-ended dict, this contract makes every value
    consumed by the painter explicit and bounded.
    """
    level: OrderFlowDisplayLevel
    display_state: str
    liquidity_visual: float
    price_text: str
    notional_text: str
    event_text: str = ''
    event_kind: str = ''
    sell_large: bool = False
    buy_large: bool = False
    market_signal_text: str = ''
    market_signal_color: QtGui.QColor | None = None
    market_signal_fill: QtGui.QColor | None = None
    delta_color: QtGui.QColor | None = None
    change_cue: tuple[str, float, float] | None = None
    change_cue_color: QtGui.QColor | None = None
    is_native_touch: bool = False
    native_touch_fraction: float = 0.5
    is_ltp_row: bool = False
    ltp_fraction: float = 0.5
    profile_size: float = 0.0
    profile_depth: float = 0.0
    profile_previous_depth: float = 0.0
    profile_amount: float = 0.0
    profile_cumulative: float = 0.0
    profile_in_range: bool = True

def _compute_order_flow_dom_geometry(width: float, height: float, font_height: float=13.0, price_font_height: float | None=None, label_font_height: float | None=None, *, column_preferences: dict[str, object] | None=None, row_density: str='normal', presentation_preset: str='execution', essential_only: bool=False, previous_mode: str | None=None, previous_bbo_only: bool | None=None, price_min_width: float=90.0, amount_min_width: float=36.0, state_expanded_width: float | None=None, column_width_overrides: dict[str, float] | None=None, book_depth: bool=False) -> dict[str, object]:
    """Compute a deterministic width-driven DOM composition.

    BID / PRICE / ASK are always present. Analytical lanes are introduced only
    when the available width can support them without sacrificing exact prices.
    Readable header bands reserve market context; the remaining height is spent
    on symmetric price rows. User-selected analytics appear only when they fit.
    """
    del previous_mode, previous_bbo_only, presentation_preset
    preset = 'execution'
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    minimum_reference = 180.0 <= height < 250.0 and width >= 260.0
    margin = 6.0 if minimum_reference else 8.0
    inner_width = max(1.0, width - margin * 2.0)
    density = str(row_density or 'normal').lower()
    if density not in {'compact', 'normal', 'relaxed'}:
        density = 'normal'

    if book_depth:
        # Keep market context and the midpoint out of the price/quantity rows.
        row_height = max({'compact': 20.0, 'normal': 24.0, 'relaxed': 30.0}[density],
                         float(math.ceil(max(font_height, price_font_height or font_height) + 4.0)))
        price_width = min(width, max(42.0, float(price_min_width)))
        heat_width = min(8.0, max(0.0, width - price_width))
        plot_left = min(width, price_width + heat_width + 2.0)
        market_height = min(height, max(24.0, float(label_font_height or 11.0) + 8.0))
        totals_height = min(max(0.0, height - market_height),
                            float(label_font_height or 11.0) + 6.0 if width < 280.0 else 0.0)
        header_height = market_height + totals_height
        center_height = min(max(0.0, height - header_height),
                            max(24.0, font_height + float(label_font_height or 11.0) + 8.0
                                if width < 240.0 else font_height + 8.0))
        body_height = max(0.0, height - header_height - center_height)
        center_top = header_height + math.floor(body_height * 0.5)
        center_bottom = center_top + center_height
        rows = max(1, int(math.ceil(max(center_top - header_height, height - center_bottom) / row_height)) + 1)
        columns = {'price': (0.0, price_width)}
        if width > plot_left:
            columns['liquidity'] = (plot_left, width)
        return {
            'width': width, 'height': height, 'margin': 0.0, 'inner_width': width,
            'mode': 'profile', 'compact': width < 315.0, 'narrow': width < 236.0,
            'wide': width >= 674.0, 'row_density': density, 'shallow': height < 120.0,
            'bbo_only': False, 'price_only_due_width': len(columns) == 1,
            'depth_mode': 'profile', 'book_depth': True,
            'top_height': header_height, 'title_height': header_height, 'metric_height': 0.0,
            'column_height': 0.0, 'row_height': row_height, 'nominal_row_height': row_height,
            'center_height': center_height, 'footer_height': 0.0, 'rows_per_side': rows,
            'header_top': 0.0, 'column_top': 0.0, 'table_top': header_height,
            'center_top': center_top, 'center_bottom': center_bottom,
            'footer_top': height, 'footer_bottom': height,
            'columns': columns, 'column_minimums': {'price': price_width},
            'heat_left': price_width, 'heat_width': heat_width,
            'profile_market_height': market_height, 'profile_totals_height': totals_height,
            'state_full_label_width': 0.0, 'flow_max_width': 0.0,
            'column_preferences': {}, 'presentation_preset': preset,
            'primary_analytic': '', 'visible_analytics': (),
        }

    preferences: dict[str, object] = {'state': True, 'memory': True, 'delta': True, 'flow': True, 'primary': 'flow'}
    if isinstance(column_preferences, dict):
        for key in ('state', 'memory', 'delta', 'flow'):
            if key in column_preferences:
                preferences[key] = bool(column_preferences[key])
        primary = str(column_preferences.get('primary', 'flow')).lower()
        if primary in {'flow', 'delta', 'memory'}:
            preferences['primary'] = primary

    # Vertical chrome still uses the existing narrow breakpoints; horizontal
    # analytical composition is selected independently below.
    compact = inner_width < 315.0
    ultra_narrow = inner_width < 229.0
    mode = 'compact' if compact else 'standard'
    narrow = inner_width < 236.0
    wide = inner_width >= 674.0
    if minimum_reference:
        base_row_height = {'compact': 18.0, 'normal': 22.0, 'relaxed': 26.0}[density]
    elif ultra_narrow:
        base_row_height = {'compact': 18.0, 'normal': 22.0, 'relaxed': 26.0}[density]
    else:
        base_row_height = {'compact': 20.0, 'normal': 24.0, 'relaxed': 30.0}[density]
    # Density chooses spacing, but typography owns the clipping floor.  The DOM
    # fonts are pixel-sized, so use their measured logical-pixel heights directly.
    font_h = max(1.0, float(font_height))
    price_h = max(font_h, float(price_font_height if price_font_height is not None else font_h))
    label_h = max(1.0, float(label_font_height if label_font_height is not None else font_h))
    density_pad = {'compact': 3.0, 'normal': 6.0, 'relaxed': 10.0}[density]
    nominal_row_height = max(base_row_height, float(math.ceil(font_h + density_pad)))
    row_height = nominal_row_height

    # Column composition respects visibility preferences and available width.
    # The execution-safe BID / PRICE / ASK core is always present. As horizontal
    # space becomes available, add the smallest/highest-value analytical lanes
    # in a stable order: STATE, FLOW, Δ BOOK, then 30 s liquidity memory.
    # This keeps narrow rails readable while making expansion immediately useful.
    required_price = min(inner_width, max(42.0, float(price_min_width)))
    required_amount = min(inner_width, max(58.0, float(amount_min_width)))
    minimum_core = max(126.0, required_price + required_amount * 2.0)
    core_target_width = max(280.0, required_amount + required_price + required_amount)
    active_names: set[str] = {'bid', 'price', 'ask'}
    if not essential_only and inner_width >= minimum_core:
        remaining = max(0.0, inner_width - core_target_width)
        primary = str(preferences.get('primary', 'flow'))
        lane_order = tuple(dict.fromkeys((primary, 'flow', 'delta', 'state', 'memory')))
        costs = {'state': 46.0, 'flow': _PAIRED_ANALYTIC_WIDTH,
                 'delta': _PAIRED_ANALYTIC_WIDTH, 'memory': 76.0}
        for lane in lane_order:
            cost = costs[lane]
            if not preferences.get(lane, True) or remaining + 1e-9 < cost:
                continue
            active_names.add(lane)
            remaining -= cost

    names = [name for name in _REFERENCE_FULL_COLUMNS if name in active_names]
    # The analytical lanes have pixel widths. Only the three trading lanes
    # share ongoing growth as the rail is dragged wider. FLOW and Δ BOOK use
    # the same width even when restored settings contain old independent sizes.
    state_min_width = max(46.0, float(ORDERBOOK_STATE_MIN_WIDTH))
    measured_state_width = (
        float(state_expanded_width)
        if isinstance(state_expanded_width, (int, float)) and math.isfinite(float(state_expanded_width))
        else float(ORDERBOOK_STATE_FULL_LABEL_WIDTH)
    )
    state_full_label_width = min(
        _STATE_COLUMN_MAX_WIDTH,
        max(float(ORDERBOOK_STATE_FULL_LABEL_WIDTH), measured_state_width),
    )
    widths = {name: _PAIRED_ANALYTIC_WIDTH for name in names if name in {'flow', 'delta'}}
    if 'memory' in names:
        widths['memory'] = 76.0
    if 'state' in names:
        widths['state'] = state_min_width
        # STATE gets a bounded expansion only once the three trading lanes have
        # reached their full reference size; oversized event text uses acronyms.
        core_reference = sum(_REFERENCE_FULL_WEIGHTS[name] for name in ('bid', 'price', 'ask'))
        remaining = inner_width - sum(widths.values())
        widths['state'] += min(
            state_full_label_width - state_min_width,
            max(0.0, remaining - core_reference),
        )

    core_names = ('bid', 'price', 'ask')
    core_width = max(0.0, inner_width - sum(widths.values()))
    overrides = column_width_overrides if isinstance(column_width_overrides, dict) else {}
    core_weights = {}
    for name in core_names:
        raw = overrides.get(name)
        core_weights[name] = (
            float(raw) if isinstance(raw, (int, float)) and math.isfinite(float(raw)) and float(raw) > 8.0
            else _REFERENCE_FULL_WEIGHTS[name]
        )
    core_floors = {'bid': required_amount, 'price': required_price, 'ask': required_amount}
    if core_width + 1e-9 < sum(core_floors.values()):
        # At extremely narrow sizes, retain the price axis and share the remainder.
        widths['price'] = min(core_width, required_price)
        widths['bid'] = max(0.0, core_width - widths['price']) * 0.5
        widths['ask'] = max(0.0, core_width - widths['price'] - widths['bid'])
    else:
        flexible = set(core_names)
        remaining = core_width
        while flexible:
            total_weight = sum(core_weights[name] for name in flexible)
            constrained = [
                name for name in core_names if name in flexible
                and remaining * core_weights[name] / total_weight < core_floors[name]
            ]
            if not constrained:
                for name in flexible:
                    widths[name] = remaining * core_weights[name] / total_weight
                break
            for name in constrained:
                widths[name] = core_floors[name]
                remaining -= core_floors[name]
                flexible.remove(name)

    columns: dict[str, tuple[float, float]] = {}
    x = margin
    for index, name in enumerate(names):
        right = margin + inner_width if index == len(names) - 1 else x + widths[name]
        columns[name] = (x, right)
        x = right

    if book_depth:
        # Both sides share one left-hand price axis and one horizontal plot.
        # Amount labels overlay the bars, as in a vertical liquidity profile.
        price_width = min(inner_width, max(72.0, float(price_min_width)))
        columns = {'price': (margin, margin + price_width)}
        if inner_width - price_width > 1.0:
            columns['liquidity'] = (margin + price_width, margin + inner_width)

    # A readable instrument header, quiet metric cards, and a labeled spread
    # band replace the tightly packed terminal strips. Shallow docks retain the
    # ladder by reducing optional chrome before shrinking market numerics.
    shallow = height < 310.0
    title_height = max(32.0, float(math.ceil(label_h + 12.0)))
    metric_height = 0.0 if shallow else max(48.0, float(math.ceil(font_h + label_h + 15.0)))
    center_height = max(56.0, float(math.ceil(price_h + label_h + 19.0)))
    footer_height = 52.0 if not shallow else 28.0
    column_height = max(30.0, float(math.ceil(label_h + 12.0)))
    if shallow:
        title_height = 28.0
        center_height = max(36.0, float(math.ceil(price_h + 10.0)))
        column_height = max(24.0, float(math.ceil(label_h + 8.0)))
    if book_depth:
        footer_height = 36.0 if not shallow else 28.0
    if height < 120.0:
        title_height = min(title_height, height * 0.24)
        metric_height = 0.0
        column_height = min(column_height, height * 0.20)
        center_height = min(center_height, height * 0.40)
        footer_height = 0.0
    top_height = title_height + metric_height
    column_top = margin + top_height
    table_top = column_top + column_height

    # The bottom edge is part of the rendered surface, not an unused outer
    # margin. Splitter heights are arbitrary pixel values, while a symmetric DOM
    # consumes rows in bid/ask pairs. Using a fixed row height therefore leaves
    # a remainder for almost every manual resize. Treat density as the nominal
    # (minimum) spacing, choose the number of complete pairs that fit, then
    # distribute the remainder uniformly across those rows. This makes the DOM
    # consume every vertical pixel without a dead strip and keeps both sides
    # perfectly aligned around the center band.
    fixed = margin + top_height + column_height + center_height + footer_height
    available_rows = max(0.0, height - fixed)
    rows_per_side = int(available_rows // max(1.0, nominal_row_height * 2.0))
    bbo_only = rows_per_side < 1
    if bbo_only:
        rows_per_side = 0
        footer_height = 0.0
        center_top = table_top
        center_height = max(1.0, height - center_top)
        center_bottom = height
        footer_top = height
        footer_bottom = height
    else:
        row_height = available_rows / float(rows_per_side * 2)
        ladder_height = rows_per_side * row_height
        center_top = table_top + ladder_height
        center_bottom = center_top + center_height
        footer_top = center_bottom + ladder_height
        # Pin the final band to the real widget edge. Avoid deriving this from a
        # chain of floating-point additions: paint/hit geometry must share the
        # exact same bottom boundary during live splitter drags.
        footer_bottom = height

    column_minimums = {
        'state': float(ORDERBOOK_STATE_MIN_WIDTH), 'memory': 34.0, 'delta': 30.0, 'bid': max(46.0, required_amount),
        'price': max(42.0, min(required_price, inner_width)), 'ask': max(46.0, required_amount), 'flow': 30.0,
    }
    visible_analytics = tuple(name for name in ('flow', 'delta', 'memory') if name in columns)
    return {
        'width': width, 'height': height, 'margin': margin, 'inner_width': inner_width,
        'mode': mode, 'compact': compact, 'narrow': narrow, 'wide': wide,
        'row_density': density, 'shallow': bbo_only or height < 220.0,
        'bbo_only': bbo_only, 'price_only_due_width': len(columns) == 1 and 'price' in columns,
        'depth_mode': 'none' if bbo_only else 'profile' if book_depth else 'integrated',
        'book_depth': bool(book_depth),
        'top_height': top_height, 'title_height': title_height, 'metric_height': metric_height,
        'column_height': column_height,
        'row_height': row_height, 'nominal_row_height': nominal_row_height, 'center_height': center_height, 'footer_height': footer_height,
        'rows_per_side': rows_per_side, 'header_top': margin,
        'column_top': column_top, 'table_top': table_top, 'center_top': center_top,
        'center_bottom': center_bottom, 'footer_top': footer_top, 'footer_bottom': footer_bottom,
        'columns': columns, 'column_minimums': {name: column_minimums.get(name, 20.0) for name in columns},
        'state_full_label_width': state_full_label_width, 'flow_max_width': _PAIRED_ANALYTIC_WIDTH,
        'column_preferences': preferences, 'presentation_preset': preset,
        'primary_analytic': str(preferences.get('primary', 'flow')),
        'visible_analytics': visible_analytics,
    }

class _DomRasterCanvas(QtWidgets.QWidget):
    """Single-surface DOM + temporal flow cockpit for the current symbol."""
    price_selected = Signal(float)
    aggregation_changed = Signal(int)
    column_preferences_changed = Signal(object)
    layout_state_changed = Signal(object)
    presentation_changed = Signal(object)
    visible_depth_rows_changed = Signal(int)
    DEFAULT_COLUMN_PREFERENCES: ClassVar[dict[str, object]] = {'state': True, 'memory': True, 'delta': True, 'flow': True, 'primary': 'flow'}
    PRIMARY_ANALYTICS: ClassVar[tuple[str, ...]] = ('flow', 'delta', 'memory')
    RESIZABLE_COLUMN_NAMES: ClassVar[frozenset[str]] = frozenset({'bid', 'price', 'ask'})
    TEXT_LAYOUT_CACHE_CAPACITY = 4096
    PRICE_METRICS_CACHE_CAPACITY = 64
    MAX_VISIBLE_EVENTS_PER_SIDE = 2
    SNAPSHOT_TIMING_PRUNE_INTERVAL = 32
    DIRTY_REGION_FULL_REPAINT_ROW_RATIO = 0.70
    DEPTH_DEGRADED_SECONDS = 0.90
    BBO_DEGRADED_SECONDS = 1.20
    LATENCY_DISPLAY_INTERVAL_SECONDS = 1.0
    LATENCY_MAX_DISPLAY_WINDOW_SECONDS = 10.0
    _STATE_LABELS: ClassVar[dict[str, str]] = dict(ORDERBOOK_STATE_LABELS)
    _STATE_ACRONYMS: ClassVar[dict[str, str]] = dict(ORDERBOOK_STATE_ACRONYMS)
    _STATE_DESCRIPTIONS: ClassVar[dict[str, str]] = {'ABSORBING': 'Absorbing: executions are being met by replenishing liquidity', 'PULLING': 'Pulling: inferred cancellations exceed new passive adds, excluding reposted size', 'STACKING': 'Stacking: new passive liquidity exceeds cancellations, excluding reloads and reposts', 'DEPLETING': 'Depleting: executions are consuming the level', 'PERSISTENT': 'Wall: liquidity has remained present near this price', 'NORMAL': 'Normal: no strong temporal liquidity signal'}

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.theme = {}
        self._bar_theme = dict(theme or {})
        self.symbol = 'BTCUSDT'
        self.price_tick_size = 0.0
        self.price_decimals = 0
        self._price_envelope_integer_digits = 0
        self._price_envelope_text = '0'
        self._last_visible_depth_rows = -1
        self.aggregation_multiplier = 1
        self._aggregation_epoch = 0
        self._aggregation_worker = _DomAggregation()
        self._aggregation_job = LatestJob(
            QtCore.QThreadPool.globalInstance(), self,
            accept_intermediate=True, priority=1,
        )
        self._aggregation_job.ready.connect(self._aggregation_ready)
        self._aggregation_job.failed.connect(self._aggregation_failed)
        self._column_preferences: dict[str, object] = dict(self.DEFAULT_COLUMN_PREFERENCES)
        self._column_width_overrides: dict[str, dict[str, float]] = {}
        self._column_resize_boundary: tuple[str, str] | None = None
        self._column_resize_origin_x = 0.0
        self._column_resize_pair_total = 0.0
        self._column_resize_left_start = 0.0
        self._column_resize_right_start = 0.0
        self._column_resize_active = False
        self._book_validity_known = False
        self._book_valid = False
        self._book_valid_reason = ''
        self._trade_stream_known = False
        self._trade_stream_active = False
        self._trade_stream_reason = ''
        self._row_density = 'normal'
        self._presentation_preset = 'execution'
        self._book_depth = True
        self._profile_auto_grouping = True
        self._profile_grouped_symbol = None
        self._profile_ruler_fraction = 0.72
        self._profile_ruler_drag = False
        self._profile_group_drag_origin = None
        self._profile_group_drag_start_x = None
        self._profile_group_drag_multiplier = 1
        self._profile_group_drag_moved = False
        self._profile_ruler_totals = (0.0, 0.0)
        self._profile_anchor_price = 0.0
        self._profile_last_drawn_y = None
        self._profile_paths: dict[str, tuple[QtGui.QPainterPath, QtGui.QPainterPath]] = {}
        self._profile_brushes: dict[str, QtGui.QBrush] = {}
        self._profile_totals = (0.0, 0.0)
        self._profile_scale_text = ''
        self._profile_footer_text: tuple[str, str] = ('', '')
        self._profile_amount_width = 72.0
        self._profile_size_targets: dict[tuple[str, float], float] = {}
        self._profile_size_current: dict[tuple[str, float], float] = {}
        self._profile_depth_targets: dict[tuple[str, float], float] = {}
        self._profile_depth_current: dict[tuple[str, float], float] = {}
        self._profile_animation_last_frame = time.monotonic()
        self._profile_animation_timer = QTimer(self)
        self._profile_animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._profile_animation_timer.setInterval(16)
        self._profile_animation_timer.timeout.connect(self._advance_profile_animation)
        self._value_mode = 'base'
        self._market_signal: dict[str, object] | None = None
        self._last_layout_state: dict[str, object] = {}
        self._row_change_cues: dict[tuple[str, int | float], tuple[str, float, float]] = {}
        self.source_snapshot: OrderFlowSnapshot | None = None
        self._applied_source_snapshot: OrderFlowSnapshot | None = None
        self.snapshot: OrderFlowSnapshot | None = None
        self._latest_received_sequence = -1
        self._pending_source_snapshot: OrderFlowSnapshot | None = None
        self._last_snapshot_prepare_at = 0.0
        self._snapshot_received_at: dict[int, float] = {}
        self._snapshot_build_started_at: dict[int, float] = {}
        self._snapshot_build_completed_at: dict[int, float] = {}
        self._snapshot_prepare_started_at: dict[int, float] = {}
        self._snapshot_prepare_completed_at: dict[int, float] = {}
        self._snapshot_pipeline_timing: dict[int, dict[str, float]] = {}
        self._snapshot_received_count = 0
        self._snapshot_full_applied_count = 0
        self._snapshot_coalesced_count = 0
        self._snapshot_superseded_before_row_count = 0
        self._snapshot_build_samples: deque[float] = deque(maxlen=240)
        self._visual_scales = {'liquidity': 0.0, 'delta': 0.0, 'trade': 0.0, 'depth': 0.0}
        self._state_latches: dict[tuple[str, int | float], dict[str, object]] = {}
        self._prepared_display_states: dict[tuple[str, int | float], str] = {}
        # Visual-diff state is produced once while prepared rows/hit geometry are
        # rebuilt. Snapshot application keeps references to the previous maps, then
        # _prepare_display() atomically replaces them with the new prepared state.
        # This removes the old before/after signature walks from the hot path.
        self._prepared_row_signatures: dict[tuple[str, int | float], tuple[object, ...]] = {}
        self._prepared_row_rects: dict[tuple[str, int | float], QtCore.QRect] = {}
        self._prepared_chrome_signature: tuple[tuple[object, ...], ...] = ()
        self._event_sparsify_signatures: dict[str, tuple[object, ...]] = {'ask': (), 'bid': ()}
        self._event_sparsify_keep_keys: dict[str, frozenset[tuple[str, int | float]]] = {
            'ask': frozenset(),
            'bid': frozenset(),
        }
        self._geometry = _compute_order_flow_dom_geometry(360.0, 520.0, book_depth=self._book_depth)
        self._prepared_sequence = -1
        self._prepared_size = (-1, -1)
        self._header_text: dict[str, tuple[str, QtGui.QColor]] = {}
        self._metric_items: list[tuple[str, str, QtGui.QColor]] = []
        self._footer_items: list[tuple[str, str, QtGui.QColor]] = []
        self._prepared_header_layout: dict[str, object] = {}
        self._profile_header_key = None
        self._profile_header_items = ()
        self._footer_depth_summary = (0.5, '—', '—', False)
        self._prepared_column_labels: dict[str, str] = {}
        self._prepared_metric_draw_items: tuple[tuple[str, str, QtGui.QColor], ...] = ()
        self._prepared_footer_draw_items: tuple[tuple[str, QtGui.QColor], ...] = ()
        self._typography_ready = False
        self._theme_cache_ready = False
        self._freshness_text = 'Waiting for market data'
        self._market_status = 'CONNECTING'
        self._bid_rows: list[PreparedDomRow] = []
        self._ask_rows: list[PreparedDomRow] = []
        self._hit_rows: list[tuple[QtCore.QRectF, float, OrderFlowDisplayLevel]] = []
        self._hit_geometry_key = None
        self._hit_geometry = ((), ())
        self._hover_price = 0.0
        self._hover_context = ''
        self._last_trade_price = 0.0
        self._last_trade_side = ''
        self.last_paint_ms = 0.0
        self.max_paint_ms = 0.0
        self.last_prepare_ms = 0.0
        self.max_prepare_ms = 0.0
        self.last_diff_ms = 0.0
        self.max_diff_ms = 0.0
        self.last_snapshot_apply_ms = 0.0
        self.max_snapshot_apply_ms = 0.0
        self.last_snapshot_to_paint_ms = 0.0
        self.max_snapshot_to_paint_ms = 0.0
        self._has_snapshot_to_paint_sample = False
        self._latency_book_peak_ms = 0.0
        self._latency_dom_peak_ms = 0.0
        self._latency_display_updated_at = 0.0
        self._latency_display_book_ms: float | None = None
        self._latency_display_book_max_ms: float | None = None
        self._latency_display_dom_ms: float | None = None
        self._latency_display_dom_p95_ms: float | None = None
        self._latency_display_dom_max_ms: float | None = None
        # Display-only rolling windows. Raw latency measurements and diagnostic
        # maxima remain unchanged; these only provide the requested 10 s MAX.
        self._latency_book_display_window: deque[tuple[float, float]] = deque()
        self._latency_dom_display_window: deque[tuple[float, float]] = deque()
        self._latency_pipe_display_window: deque[tuple[float, float]] = deque()
        self.last_end_to_end_ms = 0.0
        self.max_end_to_end_ms = 0.0
        self._has_end_to_end_sample = False
        self._latency_stage_samples: dict[str, deque[float]] = {
            name: deque(maxlen=240)
            for name in (
                'socket_to_parser', 'parser_to_gui_dispatch', 'gui_dispatch_to_main',
                'parser_to_main', 'socket_to_main',
                'main_depth_handler', 'main_to_worker', 'worker_depth_process', 'worker_wait', 'snapshot_build',
                'worker_to_gui', 'gui_to_dom', 'dom_queue', 'aggregation', 'prepare',
                'prepare_to_paint', 'paint', 'dom_to_paint', 'socket_to_paint',
            )
        }
        self._latency_stage_max_ms: dict[str, float] = {name: 0.0 for name in self._latency_stage_samples}
        self._paint_count = 0
        self._full_repaint_count = 0
        self._partial_repaint_count = 0
        self._geometry_rebuild_count = 0
        self._requested_partial_repaint_count = 0
        self._repaint_request_reason_counts: Counter[str] = Counter()
        self._last_repaint_request_reason = ''
        self._last_repaint_request_region = 'none'
        self._last_paint_region = 'none'
        self._last_paint_region_rects = 0
        self._last_paint_region_bbox_pct = 0.0
        self._market_anchor_snapshot: OrderFlowSnapshot | None = None
        self._text_cache_hits = 0
        self._text_cache_misses = 0
        self._text_cache_evictions = 0
        self._last_painted_sequence = -1
        self._last_painted_depth_ingress_id = -1
        self._last_pipe_sample_at = 0.0
        self._required_paint_regions: dict[int, QtGui.QRegion] = {}
        self._required_paint_rows_seen: set[int] = set()
        self._latest_applied_sequence = -1
        self._paint_timestamps: deque[float] = deque(maxlen=240)
        self._full_paint_timestamps: deque[float] = deque(maxlen=240)
        self._fast_paint_timestamps: deque[float] = deque(maxlen=240)
        self._partial_paint_timestamps: deque[float] = deque(maxlen=240)
        self._text_layout_cache: OrderedDict[tuple[str, str, int], str] = OrderedDict()
        self._price_metrics_cache: OrderedDict[tuple[str, float, float], QtGui.QFontMetricsF] = OrderedDict()
        self._price_fit_cache: OrderedDict[tuple[object, ...], float] = OrderedDict()
        # Amount versions survive process transfer and ignore age/state-only
        # changes. Typography and units remain part of the measurement key.
        self._amount_width_cache_key = None
        self._amount_width_cache_context = self._amount_width_cache_inputs = None
        self._amount_width_cache_value = 36.0
        self._amount_width_cache_hits = self._amount_width_cache_misses = 0
        self._row_revision_reuses = 0
        self._geometry_cache_key: tuple[object, ...] | None = None
        self._price_protection_active = False
        self._suppress_change_cues_once = False
        self._interaction_priority_active = False
        # Snapshot preparation is intentionally outside the shared PresentationClock
        # transaction.  A tiny DOM-owned handoff lets the frame signal return so
        # chart/ticker consumers can schedule their paints before DOM row preparation
        # runs, while preserving the existing partial dirty-region machinery.
        self._snapshot_prepare_timer = QTimer(self)
        self._snapshot_prepare_timer.setSingleShot(True)
        self._snapshot_prepare_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._snapshot_prepare_timer.timeout.connect(self._flush_pending_snapshot)
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setSingleShot(True)
        self._freshness_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._freshness_timer.timeout.connect(self._expire_snapshot_freshness)
        self._resize_prepare_timer = QTimer(self)
        self._resize_prepare_timer.setSingleShot(True)
        self._resize_prepare_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._resize_prepare_timer.timeout.connect(self._flush_resize_prepare)
        self._cue_expiry_timer = QTimer(self)
        self._cue_expiry_timer.setSingleShot(True)
        self._cue_expiry_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._cue_expiry_timer.timeout.connect(self._expire_change_cues)
        self._market_signal_expiry_timer = QTimer(self)
        self._market_signal_expiry_timer.setSingleShot(True)
        self._market_signal_expiry_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._market_signal_expiry_timer.timeout.connect(self._expire_market_signal)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self._refresh_typography()
        self._refresh_theme_cache()
        typography_controller().changed.connect(self._refresh_typography)

    def _refresh_typography(self) -> None:
        # Both views use the app's configured fonts and numeric size roles.
        self._row_font = typography_font(TextRole.ORDERBOOK_VALUE)
        self._price_font = typography_font(TextRole.ORDERBOOK_PRICE)
        self._metric_font = typography_font(TextRole.ORDERBOOK_METRIC)
        self._symbol_font = typography_font(TextRole.ORDERBOOK_SYMBOL)
        self._label_font = typography_font(TextRole.ORDERBOOK_LABEL)
        self._footer_font = typography_font(TextRole.ORDERBOOK_FOOTER_VALUE)
        self._center_price_font = typography_font(TextRole.ORDERBOOK_CENTER_PRICE)
        self._profile_annotation_font = typography_font(TextRole.ORDERBOOK_FOOTER_VALUE)
        self._center_price_metrics = QtGui.QFontMetricsF(self._center_price_font)
        self._row_metrics = QtGui.QFontMetricsF(self._row_font)
        self._price_metrics = QtGui.QFontMetricsF(self._price_font)
        self._effective_price_font = QtGui.QFont(self._price_font)
        self._effective_price_metrics = QtGui.QFontMetricsF(self._effective_price_font)
        self._price_text_overflow = False
        self._metric_metrics = QtGui.QFontMetricsF(self._metric_font)
        self._symbol_metrics = QtGui.QFontMetricsF(self._symbol_font)
        self._label_metrics = QtGui.QFontMetricsF(self._label_font)
        self._row_font_key = self._row_font.toString()
        self._price_font_key = self._price_font.toString()
        self._effective_price_font_key = self._effective_price_font.toString()
        self._metric_font_key = self._metric_font.toString()
        self._symbol_font_key = self._symbol_font.toString()
        self._label_font_key = self._label_font.toString()
        self._text_layout_cache.clear()
        self._price_metrics_cache.clear()
        self._price_fit_cache.clear()
        self._amount_width_cache_key = None
        self._amount_width_cache_context = self._amount_width_cache_inputs = None
        self._amount_width_cache_value = 36.0
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._typography_ready = True
        if self._theme_cache_ready:
            self._prepare_display()
            self.update()

    def _refresh_profile_bar_palette(self, theme: dict[str, object] | None) -> None:
        del theme
        p = ORDERBOOK_REFERENCE
        self._profile_bg = QtGui.QColor(p['bg'])
        self._profile_price = QtGui.QColor(p['text'])
        self._profile_dim_price = QtGui.QColor('#515862')
        self._profile_last = QtGui.QColor(p['mid'])
        self._profile_colors = {
            'bid': QtGui.QColor(p['bid']),
            'ask': QtGui.QColor(p['ask']),
        }
        self._profile_bar_colors = {
            'bid': QtGui.QColor(p['bid_fill_strong']),
            'ask': QtGui.QColor(p['ask_fill_strong']),
        }
        # Keep DEPTH caps/outline crisp while pushing the large bar bodies one
        # visual step behind price and the cumulative-depth staircase.
        self._profile_brushes = {side: QtGui.QBrush(color) for side, color in self._profile_bar_colors.items()}
        self._profile_caps = {}
        self._profile_hovers = {}
        self._profile_bar_text = {}
        self._profile_fills = {}
        self._profile_pens = {}
        self._profile_in_bar_pens = {}
        for side in ('bid', 'ask'):
            line = QtGui.QColor(self._profile_colors[side])
            cap = QtGui.QColor(line)
            cap.setAlpha(205)
            hover = QtGui.QColor(line)
            hover.setAlpha(0)
            area = QtGui.QColor(self._profile_bar_colors[side])
            # The cumulative area is intentionally asymmetric: sell-side depth
            # keeps a slightly clearer red field while bid-side remains quieter.
            area.setAlpha(80)
            pen_color = QtGui.QColor(line)
            pen_color.setAlpha(255)
            pen = QtGui.QPen(pen_color)
            pen.setCosmetic(True)
            pen.setWidthF(1.0)

            # Inside a solid liquidity bar the bright cumulative-depth color is
            # too dominant.  Bars first cover the normal outline; a second,
            # clipped pass restores continuity using a tonal derivative of the
            # bar itself rather than the saturated directional accent.
            in_bar_color = QtGui.QColor(self._profile_bar_colors[side]).lighter(155)
            in_bar_color.setAlpha(115)
            in_bar_pen = QtGui.QPen(in_bar_color)
            in_bar_pen.setCosmetic(True)
            in_bar_pen.setWidthF(0.9)
            self._profile_caps[side] = cap
            self._profile_hovers[side] = hover
            self._profile_bar_text[side] = QtGui.QColor('#C4EAD5' if side == 'bid' else '#EDC1CD')
            self._profile_fills[side] = area
            self._profile_pens[side] = pen
            self._profile_in_bar_pens[side] = in_bar_pen

    def _refresh_theme_cache(self) -> None:
        p = ORDERBOOK_REFERENCE
        self._bg = QtGui.QColor(p['bg'])
        self._text = QtGui.QColor(p['text'])
        self._muted = QtGui.QColor(p['muted'])
        self._badge_text = QtGui.QColor(p['text'])
        self._grid = QtGui.QColor(p['grid'])
        self._grid_strong = QtGui.QColor(p['grid_strong'])
        self._bid = QtGui.QColor(p['bid'])
        self._ask = QtGui.QColor(p['ask'])
        self._mid = QtGui.QColor(p['mid'])
        self._amber = QtGui.QColor(p['amber'])
        self._purple = QtGui.QColor(p['purple'])
        self._surface_top = QtGui.QColor(p['surface_top'])
        self._surface_raised = QtGui.QColor(p['surface_raised'])
        self._price_axis_fill = QtGui.QColor(p['bg'])
        self._bid_zone_fill = QtGui.QColor(p['bid_fill'])
        self._bid_zone_fill.setAlpha(0)
        self._ask_zone_fill = QtGui.QColor(p['ask_fill'])
        self._ask_zone_fill.setAlpha(0)
        self._bid_fill = QtGui.QColor(p['bid_fill'])
        self._ask_fill = QtGui.QColor(p['ask_fill'])
        # Resting-size bars should communicate length first, not dominate the
        # ladder as opaque color blocks.
        self._bid_fill.setAlpha(150)
        self._ask_fill.setAlpha(150)
        self._bid_fill_strong = QtGui.QColor(p['bid_fill_strong'])
        self._ask_fill_strong = QtGui.QColor(p['ask_fill_strong'])
        self._hover_fill = QtGui.QColor(p['bg'])
        self._best_bid_row_fill = QtGui.QColor(p['bg'])
        self._best_ask_row_fill = QtGui.QColor(p['bg'])
        self._center_fill = QtGui.QColor(p['surface_center'])
        self._center_price_fill = QtGui.QColor(p['surface_center'])
        # Cell boundaries are structural guides only. Keep them intentionally
        # faint so liquidity, price and order-flow data dominate the ladder.
        separator_color = QtGui.QColor(p['grid'])
        separator_color.setAlpha(16)
        self._separator_pen = QtGui.QPen(separator_color)
        self._separator_pen.setCosmetic(True)
        self._separator_pen.setWidthF(0.0)
        # Column boundaries use the exact panel-border color used throughout the
        # app. A cosmetic zero-width pen resolves to one device pixel, so the
        # separators stay structurally clear without becoming visually heavy.
        self._column_border_pen = QtGui.QPen(QtGui.QColor(p['grid']))
        self._column_border_pen.setCosmetic(True)
        self._column_border_pen.setWidthF(0.0)
        strong_separator_color = QtGui.QColor(p['grid_strong'])
        strong_separator_color.setAlpha(24)
        self._strong_separator_pen = QtGui.QPen(strong_separator_color)
        self._strong_separator_pen.setCosmetic(True)
        self._strong_separator_pen.setWidthF(0.0)
        row_separator_color = QtGui.QColor(p['grid'])
        row_separator_color.setAlpha(10)
        self._row_separator_pen = QtGui.QPen(row_separator_color)
        self._row_separator_pen.setCosmetic(True)
        self._row_separator_pen.setWidthF(0.0)
        axis_color = QtGui.QColor(p['grid'])
        axis_color.setAlpha(18)
        self._axis_pen = QtGui.QPen(axis_color)
        self._axis_pen.setCosmetic(True)
        self._axis_pen.setWidthF(0.0)
        center_bid_line = QtGui.QColor(p['bid'])
        center_ask_line = QtGui.QColor(p['ask'])
        center_bid_line.setAlpha(150)
        center_ask_line.setAlpha(150)
        self._center_bid_pen = QtGui.QPen(center_bid_line)
        self._center_ask_pen = QtGui.QPen(center_ask_line)
        self._center_bid_pen.setCosmetic(True)
        self._center_ask_pen.setCosmetic(True)
        # History is gray with a colored recent tail in the reference.
        self._state_absorbing = QtGui.QColor(_ORDERBOOK_SIGNAL_COLORS['ABSORBING'])
        self._state_stacking = QtGui.QColor(_ORDERBOOK_SIGNAL_COLORS['STACKING'])
        self._state_pulling = QtGui.QColor(_ORDERBOOK_SIGNAL_COLORS['PULLING'])
        self._state_depleting = QtGui.QColor(_ORDERBOOK_SIGNAL_COLORS['DEPLETING'])
        self._state_wall = QtGui.QColor(_ORDERBOOK_SIGNAL_COLORS['PERSISTENT'])
        self._bid_signal_fill = QtGui.QColor(p['bg'])
        self._ask_signal_fill = QtGui.QColor(p['bg'])
        self._ltp_line_color = QtGui.QColor('#444444')
        self._ltp_pen = QtGui.QPen(self._ltp_line_color)
        self._ltp_pen.setCosmetic(True)
        self._metric_center_pen = QtGui.QPen(QtGui.QColor(p['grid']))
        self._metric_center_pen.setCosmetic(True)
        self._state_colors = {
            'ABSORBING': self._state_absorbing,
            'STACKING': self._state_stacking,
            'PULLING': self._state_pulling,
            'DEPLETING': self._state_depleting,
            'PERSISTENT': self._state_wall,
        }
        self._state_badge_fills = {}
        for state, color in self._state_colors.items():
            fill = QtGui.QColor(p['bg'])
            self._state_badge_fills[state] = fill
        self._center_bid_fill = QtGui.QColor(p['bg'])
        self._center_bid_fill.setAlpha(255)
        self._center_ask_fill = QtGui.QColor(p['bg'])
        self._center_ask_fill.setAlpha(255)
        self._center_price_fill = QtGui.QColor(p['bg'])
        self._native_bid_accent = self._bid
        self._native_ask_accent = self._ask
        self._cue_colors = {'add': self._bid, 'pull': self._amber, 'trade': self._ask}
        self._refresh_profile_bar_palette(self._bar_theme)
        self._profile_mid_pen = QtGui.QPen(self._amber)
        self._profile_mid_pen.setCosmetic(True)
        self._profile_mid_pen.setWidthF(1.0)
        self._prepared_sequence = -1
        self._theme_cache_ready = True
        if self._typography_ready:
            self._prepare_display()
            self.update()

    def apply_theme(self, theme: dict[str, str]) -> None:
        # The DOM chrome remains fixed. Only the reference depth-bar palette
        # follows directional theme tokens, matching the legacy bar contract.
        self.theme = {}
        self._bar_theme = dict(theme or {})
        self._refresh_profile_bar_palette(self._bar_theme)
        if self._book_depth:
            self._prepare_liquidity_profile()
            self.update()


    def set_symbol(self, symbol: str) -> None:
        normalized = str(symbol).upper().strip().removesuffix('.P') or 'BTCUSDT'
        if normalized == self.symbol:
            return
        self.symbol = normalized
        self._book_validity_known = False
        self._book_valid = False
        self._book_valid_reason = ''
        self._trade_stream_known = False
        self._trade_stream_active = False
        self._trade_stream_reason = ''
        self._price_envelope_integer_digits = 0
        self._price_envelope_text = '0'
        self._last_trade_price = 0.0
        self._last_trade_side = ''
        self._profile_grouped_symbol = None
        self.reset()

    def set_price_tick_size(self, tick_size: float) -> None:
        resolved = max(0.0, safe_float(tick_size))
        if resolved == self.price_tick_size:
            return
        self.price_tick_size = resolved
        self._profile_grouped_symbol = None
        self._invalidate_aggregation()
        self._price_envelope_integer_digits = 0
        self._price_envelope_text = '0'
        if self.price_tick_size > 0.0:
            self.price_decimals = _decimal_places_from_step(self.price_tick_size)
        else:
            self.price_decimals = 0
        self._state_latches.clear()
        self._reset_visual_scales()
        if self.source_snapshot is not None:
            self._commit_full_snapshot(self.source_snapshot, force=True)
        else:
            self._prepared_sequence = -1
            self._prepare_display()
            self.update()

    def set_aggregation_multiplier(self, multiplier: int, *, emit: bool=True) -> None:
        try:
            resolved = int(multiplier)
        except (TypeError, ValueError, OverflowError):
            resolved = 1
        if resolved not in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
            resolved = 1
        auto_grouping_changed = bool(emit and self._profile_auto_grouping)
        if emit:
            self._profile_auto_grouping = False
        if resolved == self.aggregation_multiplier:
            if auto_grouping_changed:
                self.presentation_changed.emit(self.presentation_state())
            return
        # A new grouping creates different analytical identities, including
        # when returning to native rows: require fresh confirmation snapshots.
        self._state_latches.clear()
        self._prepared_display_states.clear()
        self.aggregation_multiplier = resolved
        self._invalidate_aggregation()
        self._row_change_cues.clear()
        self._suppress_change_cues_once = True
        if self.source_snapshot is not None:
            self._commit_full_snapshot(self.source_snapshot, force=True)
        else:
            self._prepared_sequence = -1
            self._prepare_display()
            self.update()
        if emit:
            self.aggregation_changed.emit(resolved)
            self.presentation_changed.emit(self.presentation_state())

    def _bucket_price_for_multiplier(self, price: float, side: str, multiplier: int) -> float:
        if multiplier <= 1 or self.price_tick_size <= 0.0:
            return float(price)
        raw_tick = int(round(float(price) / self.price_tick_size))
        if side == 'bid' or self._book_depth:
            display_tick = raw_tick // multiplier * multiplier
        else:
            display_tick = (raw_tick + multiplier - 1) // multiplier * multiplier
        return display_tick * self.price_tick_size

    def _initialize_profile_grouping(self, snapshot: OrderFlowSnapshot, *, emit: bool=True) -> None:
        if (snapshot.symbol != self.symbol or not self._book_depth or not self._profile_auto_grouping or not snapshot.ready
                or self.price_tick_size <= 0.0 or self._profile_grouped_symbol == snapshot.symbol):
            return
        mid = snapshot.midpoint or snapshot.best_bid
        if mid <= 0.0:
            return
        step = 10.0 ** (math.floor(math.log10(mid)) - 2)
        target = max(1.0, step / self.price_tick_size)
        # Price-only defaults can collapse the whole received book into one
        # bucket. Fit the available depth before choosing an automatic step.
        spans = []
        if len(snapshot.bid_levels) > 1:
            spans.append(snapshot.bid_levels[0].price - snapshot.bid_levels[-1].price)
        if len(snapshot.ask_levels) > 1:
            spans.append(snapshot.ask_levels[-1].price - snapshot.ask_levels[0].price)
        if spans:
            row_height = max(20.0, float(self._geometry.get('row_height', 24.0)))
            target_rows = max(4, min(24, int(self.height() * 0.35 / row_height)))
            target = min(target, max(1.0, max(spans) / (self.price_tick_size * target_rows)))
        multiplier = min(ORDER_FLOW_AGGREGATION_MULTIPLIERS, key=lambda value: abs(math.log(value / target)))
        self._profile_grouped_symbol = snapshot.symbol
        self.set_aggregation_multiplier(multiplier, emit=False)
        if emit:
            self.aggregation_changed.emit(multiplier)
            self.presentation_changed.emit(self.presentation_state())

    def column_preferences(self) -> dict[str, object]:
        return dict(self._column_preferences)

    def set_column_preferences(self, preferences: dict[str, object] | None, *, emit: bool=True) -> None:
        # User visibility preferences are honored before responsive layout adds
        # analytics. Exact price and size lanes always remain visible.
        normalized: dict[str, object] = dict(self.DEFAULT_COLUMN_PREFERENCES)
        if isinstance(preferences, dict):
            for lane in ('state', 'memory', 'delta', 'flow'):
                if lane in preferences:
                    normalized[lane] = bool(preferences[lane])
            primary = str(preferences.get('primary', normalized['primary'])).lower()
            if primary in self.PRIMARY_ANALYTICS:
                normalized['primary'] = primary
        if normalized == self._column_preferences:
            return
        self._column_preferences = normalized
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=True)
        self.update()
        if emit:
            self.column_preferences_changed.emit(dict(normalized))

    def column_width_state(self) -> dict[str, dict[str, float]]:
        output: dict[str, dict[str, float]] = {}
        for preset, values in self._column_width_overrides.items():
            if preset not in {'execution', 'liquidity', 'footprint'} or not isinstance(values, dict):
                continue
            cleaned: dict[str, float] = {}
            for name, width in values.items():
                if name not in self.RESIZABLE_COLUMN_NAMES or not isinstance(width, (int, float)):
                    continue
                value = float(width)
                if math.isfinite(value) and 12.0 <= value <= 2000.0:
                    cleaned[str(name)] = round(value, 2)
            if cleaned:
                output[preset] = cleaned
        return output

    def restore_column_width_state(self, state: object) -> None:
        restored: dict[str, dict[str, float]] = {}
        if isinstance(state, dict):
            for preset in ('execution', 'liquidity', 'footprint'):
                raw = state.get(preset)
                if not isinstance(raw, dict):
                    continue
                cleaned: dict[str, float] = {}
                for name, width in raw.items():
                    if str(name) not in self.RESIZABLE_COLUMN_NAMES:
                        continue
                    try:
                        value = float(width)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(value) and 12.0 <= value <= 2000.0:
                        cleaned[str(name)] = value
                if cleaned:
                    restored[preset] = cleaned
        if restored == self._column_width_overrides:
            return
        self._column_width_overrides = restored
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=True)
        self.update()

    def reset_column_widths(self, preset: str | None=None, *, emit: bool=True) -> None:
        if preset is None:
            changed = bool(self._column_width_overrides)
            self._column_width_overrides.clear()
        else:
            normalized = str(preset).lower()
            changed = normalized in self._column_width_overrides
            self._column_width_overrides.pop(normalized, None)
        if not changed:
            return
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=True)
        self.update()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def set_primary_analytic(self, name: str, *, emit: bool=True) -> None:
        name = str(name).lower().strip()
        if name not in self.PRIMARY_ANALYTICS:
            return
        preferences = self.column_preferences()
        preferences['primary'] = name
        preferences[name] = True
        self.set_column_preferences(preferences, emit=emit)

    def row_density(self) -> str:
        return self._row_density

    def visible_rows_per_side(self) -> int:
        return max(0, int(self._geometry.get('rows_per_side', 0)))

    def _publish_visible_depth_rows(self) -> None:
        rows = self.visible_rows_per_side()
        if rows == self._last_visible_depth_rows:
            return
        self._last_visible_depth_rows = rows
        self.visible_depth_rows_changed.emit(rows)

    def height_for_rows(self, rows_per_side: int) -> int:
        """Return the canvas height required for an exact symmetric row count."""
        geometry = self._geometry
        # Profile capacity includes one extra row for a clipped edge. It must
        # not turn into another visible row each time density is changed.
        rows = max(0, int(rows_per_side) - int(self._book_depth))
        fixed_height = (
            float(geometry.get('margin', 0.0))
            + float(geometry.get('top_height', 0.0))
            + float(geometry.get('column_height', 0.0))
            + float(geometry.get('center_height', 0.0))
            + float(geometry.get('footer_height', 0.0))
        )
        # Manual splitter resizing may stretch the effective row height to absorb
        # a pixel remainder. Density-driven panel resizing must use the nominal
        # spacing or repeated density changes would compound that stretch.
        row_height = max(1.0, float(geometry.get('nominal_row_height', geometry.get('row_height', 1.0))))
        height = fixed_height + rows * row_height * 2.0
        if self._book_depth and not fixed_height.is_integer():
            height = math.floor(height) - 1
        return max(1, int(math.ceil(height)))

    def set_row_density(self, density: str, *, emit: bool=True) -> None:
        normalized = str(density or 'normal').lower()
        if normalized not in {'compact', 'normal', 'relaxed'}:
            normalized = 'normal'
        if normalized == self._row_density:
            return
        self._row_density = normalized
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display()
        self.update()
        self._publish_layout_state()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def cycle_row_density(self, step: int) -> None:
        values = ('compact', 'normal', 'relaxed')
        try:
            index = values.index(self._row_density)
        except ValueError:
            index = 1
        self.set_row_density(values[max(0, min(len(values) - 1, index + int(step)))])

    def set_presentation_preset(self, name: str, *, emit: bool=True) -> None:
        # FULL / LIQ / FLOW are retired. Keep the public method for persisted and
        # external callers, but collapse every legacy value to the one automatic
        # width-driven composition.
        del name
        if self._presentation_preset == 'execution':
            return
        self._presentation_preset = 'execution'
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=True)
        self.update()
        self._publish_layout_state()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def value_mode(self) -> str:
        return self._value_mode

    def set_value_mode(self, mode: str, *, emit: bool=True) -> None:
        normalized = 'base' if str(mode).lower() == 'base' else 'quote'
        if normalized == self._value_mode:
            return
        self._value_mode = normalized
        self._amount_width_cache_key = None
        self._amount_width_cache_context = self._amount_width_cache_inputs = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=False)
        self.update()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def presentation_state(self) -> dict[str, object]:
        return {'profile_version': 1, 'preset': self._presentation_preset, 'book_depth': self._book_depth, 'density': self._row_density, 'values': self._value_mode, 'depth_range': self._profile_ruler_fraction, 'auto_grouping': self._profile_auto_grouping, 'aggregation': self.aggregation_multiplier, 'column_widths': self.column_width_state(), 'columns': self.column_preferences()}

    def set_book_depth_enabled(self, enabled: bool, *, emit: bool=True) -> None:
        enabled = bool(enabled)
        if enabled == self._book_depth:
            return
        if self._column_resize_active:
            self._finish_column_resize()
        self._book_depth = enabled
        self._profile_ruler_drag = False
        self._profile_group_drag_origin = None
        self._invalidate_aggregation()
        if not enabled:
            self._reset_profile_animation()
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._hover_price = 0.0
        self._hover_context = ''
        self.setToolTip('')
        self._refresh_typography()
        if self.source_snapshot is not None:
            self._commit_full_snapshot(self.source_snapshot, force=True)
        else:
            self._prepare_display()
        self.update()
        self._publish_layout_state()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def set_microstructure_snapshot(self, snapshot: object) -> None:
        key = str(getattr(snapshot, 'signal_key', '') or '').upper()
        if not key:
            return
        old_signal = self._market_signal
        old_rect = QtCore.QRect()
        if isinstance(old_signal, dict):
            old_rect = self._row_rect_for_market_price(
                str(old_signal.get('anchor_side', '') or ''),
                float(old_signal.get('anchor_price', 0.0) or 0.0),
            )
        score = int(getattr(snapshot, 'signal_score', 0) or 0)
        anchor_side = str(getattr(snapshot, 'signal_anchor_side', '') or '').lower()
        try:
            anchor_price = float(getattr(snapshot, 'signal_anchor_price', 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            anchor_price = 0.0
        if anchor_side not in {'bid', 'ask'} or anchor_price <= 0.0 or (not math.isfinite(anchor_price)):
            self._market_signal = None
            self._market_signal_expiry_timer.stop()
            self._prepared_sequence = -1
            self._prepare_display(reuse_rows=False)
            if old_rect.isNull():
                self.update()
            else:
                self._request_repaint_region('market_signal', QtGui.QRegion(old_rect))
            return
        expires = time.monotonic() + 10.0
        self._market_signal = {'key': key, 'score': score, 'anchor_side': anchor_side, 'anchor_price': anchor_price, 'expires': expires}
        self._market_signal_expiry_timer.start(max(1, int(math.ceil((expires - time.monotonic()) * 1000.0))))
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=False)
        new_rect = self._row_rect_for_market_price(anchor_side, anchor_price)
        region = QtGui.QRegion()
        if not old_rect.isNull():
            region += QtGui.QRegion(old_rect)
        if not new_rect.isNull():
            region += QtGui.QRegion(new_rect)
        if region.isEmpty():
            self.update()
        else:
            self._request_repaint_region('market_signal', region)

    def layout_state(self) -> dict[str, object]:
        columns = tuple((str(name) for name in self._geometry.get('columns', {})))
        semantic_columns = set(columns)
        if bool(self._column_preferences.get('state', False)) and (not bool(self._geometry.get('compact', False))) and {'bid', 'ask'} & semantic_columns:
            semantic_columns.add('state')
        if bool(self._column_preferences.get('memory', False)) and 'memory' in columns and {'bid', 'ask'} & semantic_columns:
            semantic_columns.add('memory')
        return {'mode': str(self._geometry.get('mode', 'unknown')), 'columns': tuple(columns), 'semantic_columns': tuple(sorted(semantic_columns)), 'primary': str(self._geometry.get('primary_analytic', self._column_preferences.get('primary', 'flow'))), 'visible_analytics': tuple((name for name in ('flow', 'delta', 'memory') if name in semantic_columns)), 'bbo_only': bool(self._geometry.get('bbo_only', False)), 'density': self._row_density, 'preset': self._presentation_preset, 'values': self._value_mode, 'range_adjustable': bool(self._geometry.get('profile_range_adjustable', False))}

    def _publish_layout_state(self) -> None:
        state = self.layout_state()
        if state != self._last_layout_state:
            self._last_layout_state = state
            self.layout_state_changed.emit(dict(state))

    def _reset_visual_scales(self) -> None:
        for key in self._visual_scales:
            self._visual_scales[key] = 0.0

    def reset(self) -> None:
        self._cancel_pointer_interaction()
        self._snapshot_prepare_timer.stop()
        self._freshness_timer.stop()
        self._market_signal_expiry_timer.stop()
        self._pending_source_snapshot = None
        self.source_snapshot = None
        self._applied_source_snapshot = None
        self.snapshot = None
        self._amount_width_cache_key = None
        self._amount_width_cache_context = self._amount_width_cache_inputs = None
        self._invalidate_aggregation()
        self._latest_received_sequence = -1
        self._latest_applied_sequence = -1
        self._last_painted_sequence = -1
        self._last_painted_depth_ingress_id = -1
        self._last_pipe_sample_at = 0.0
        self._required_paint_regions.clear()
        self._required_paint_rows_seen.clear()
        self._market_anchor_snapshot = None
        self._snapshot_received_at.clear()
        self._snapshot_build_started_at.clear()
        self._snapshot_build_completed_at.clear()
        self._snapshot_prepare_started_at.clear()
        self._snapshot_prepare_completed_at.clear()
        self._snapshot_pipeline_timing.clear()
        self._state_latches.clear()
        self._reset_visual_scales()
        self._prepared_display_states.clear()
        self._prepared_row_signatures = {}
        self._prepared_row_rects = {}
        self._prepared_chrome_signature = ()
        self._event_sparsify_signatures = {'ask': (), 'bid': ()}
        self._event_sparsify_keep_keys = {'ask': frozenset(), 'bid': frozenset()}
        self._prepared_sequence = -1
        self._header_text.clear()
        self._metric_items.clear()
        self.last_snapshot_to_paint_ms = 0.0
        self._has_snapshot_to_paint_sample = False
        self._latency_book_peak_ms = 0.0
        self._latency_dom_peak_ms = 0.0
        self._latency_display_updated_at = 0.0
        self._latency_display_book_ms = None
        self._latency_display_book_max_ms = None
        self._latency_display_dom_ms = None
        self._latency_display_dom_p95_ms = None
        self._latency_display_dom_max_ms = None
        self._latency_book_display_window.clear()
        self._latency_dom_display_window.clear()
        self._latency_pipe_display_window.clear()
        self.last_end_to_end_ms = 0.0
        self.max_end_to_end_ms = 0.0
        self._has_end_to_end_sample = False
        self.max_snapshot_to_paint_ms = 0.0
        for samples in self._latency_stage_samples.values():
            samples.clear()
        for name in self._latency_stage_max_ms:
            self._latency_stage_max_ms[name] = 0.0
        self._footer_items.clear()
        self._market_signal = None
        self._bid_rows.clear()
        self._ask_rows.clear()
        self._hit_rows.clear()
        self._hover_price = 0.0
        self._hover_context = ''
        self._last_trade_price = 0.0
        self._last_trade_side = ''
        self._profile_paths.clear()
        self._reset_profile_animation()
        self._profile_totals = (0.0, 0.0)
        self._profile_footer_text = ('BID —', 'ASK —')
        self._prepare_display(reuse_rows=True)
        self.update()

    @staticmethod
    def _compact_scalar(value: float, *, money: bool=False) -> str:
        if not math.isfinite(value) or value <= 0.0:
            return '—'
        absolute = abs(value)
        scales = ((1000000000000.0, 'T'), (1000000000.0, 'B'), (1000000.0, 'M'), (1000.0, 'K'))
        threshold = 1.0
        suffix = ''
        for candidate, marker in scales:
            if absolute >= candidate:
                threshold, suffix = candidate, marker
                break
        scaled = absolute / threshold

        def precision(number: float) -> int:
            if number >= 100.0:
                return 0
            if number >= 10.0:
                return 1
            return 2

        decimals = precision(scaled)
        rounded = round(scaled, decimals)
        if rounded >= 1000.0 and suffix:
            order = ('K', 'M', 'B', 'T')
            index = order.index(suffix)
            if index + 1 < len(order):
                suffix = order[index + 1]
                threshold *= 1000.0
                scaled = absolute / threshold
                decimals = precision(scaled)

        if not suffix and scaled < 1.0:
            body = _general_to_fixed(format(scaled, '.3g'))
        elif decimals == 0:
            body = f'{scaled:.0f}'
        elif decimals == 1:
            body = f'{scaled:.1f}'.rstrip('0').rstrip('.')
        else:
            body = f'{scaled:.2f}'.rstrip('0').rstrip('.')
        return f"{('$' if money else '')}{body}{suffix}"

    @classmethod
    def _signed_money(cls, value: float) -> str:
        if not math.isfinite(value) or abs(value) <= 1e-12:
            return '—'
        sign = '+' if value > 0.0 else '−'
        absolute = abs(value)
        if absolute < 1.0:
            body = _general_to_fixed(format(absolute, '.3g'))
        else:
            body = cls._compact_scalar(absolute, money=False)
        return f'{sign}{body}'

    @classmethod
    def _money(cls, value: float) -> str:
        return cls._compact_scalar(value, money=True)

    @classmethod
    def _signed_quantity(cls, value: float) -> str:
        if not math.isfinite(value) or abs(value) <= 1e-12:
            return '—'
        sign = '+' if value > 0.0 else '−'
        return f'{sign}{cls._compact_scalar(abs(value), money=False)}'

    def _row_amount(self, notional: float, price: float, *, quantity: float | None=None, signed: bool=False) -> str:
        if self._book_depth and not signed:
            amount = notional if self._value_mode == 'quote' else float(quantity) if quantity is not None else notional / price if price > 0 else 0.0
            return self._profile_quantity_text(amount)
        if self._value_mode == 'quote':
            return self._signed_money(notional) if signed else self._money(notional)
        base = float(quantity) if quantity is not None else notional / price if price > 0.0 else 0.0
        if signed:
            return self._signed_quantity(base)
        return self._compact_scalar(base, money=False) if abs(base) > 1e-12 else '—'

    @staticmethod
    def _profile_quantity_text(value: float) -> str:
        if not math.isfinite(value) or value <= 0.0:
            return ''
        decimals = max(1, min(16, 1 - int(math.floor(math.log10(value)))))
        text = f'{value:.{decimals}f}'.rstrip('0').rstrip('.')
        return text[1:] if text.startswith('0.') else text

    @staticmethod
    def _signed_percent(value: float) -> str:
        if not math.isfinite(value) or abs(value) < 0.05:
            return '0%'
        return f'{value:+.0f}%'

    def _price_text(self, value: float) -> str:
        if value <= 0.0 or not math.isfinite(value):
            return '—'
        decimals = self.price_decimals if self.price_tick_size > 0.0 else None
        if self._book_depth and self.price_tick_size > 0.0:
            decimals = _decimal_places_from_step(self.price_tick_size * self.aggregation_multiplier)
        return format_book_price(value, decimals)

    def _metric_color(self, value: float, *, deadband: float=2.0) -> QtGui.QColor:
        if value > deadband:
            return self._bid
        if value < -deadband:
            return self._ask
        return self._text


    def _row_frame_interval_ms(self) -> int:
        return display_frame_interval_ms(self)


    def set_interaction_priority(self, active: bool) -> None:
        """Coalesce secondary row preparation to the display during chart input."""
        active = bool(active)
        if active == self._interaction_priority_active:
            return
        self._interaction_priority_active = active
        if self._pending_source_snapshot is not None:
            self._snapshot_prepare_timer.stop()
            self._schedule_snapshot_prepare()

    def _schedule_snapshot_prepare(self) -> None:
        if self._snapshot_prepare_timer.isActive():
            return
        delay = 1
        if self._interaction_priority_active:
            remaining = self._last_snapshot_prepare_at + self._row_frame_interval_ms() / 1000.0 - time.perf_counter()
            delay = max(delay, int(math.ceil(remaining * 1000)))
        self._snapshot_prepare_timer.start(delay)

    def set_book_validity(self, valid: bool, reason: str='') -> None:
        valid = bool(valid)
        normalized_reason = str(reason or ('READY' if valid else 'ORDER BOOK INVALID')).strip().upper()
        changed = (not self._book_validity_known) or valid != self._book_valid or normalized_reason != self._book_valid_reason
        self._book_validity_known = True
        self._book_valid = valid
        self._book_valid_reason = normalized_reason
        if not valid:
            self._cancel_pointer_interaction()
        if not changed:
            return
        self._prepared_sequence = -1
        if hasattr(self, '_row_metrics'):
            self._prepare_display(reuse_rows=True)
        self.update()

    def set_trade_stream_status(self, active: bool, reason: str='') -> None:
        active = bool(active)
        normalized_reason = str(reason or ('READY' if active else 'TRADE STREAM OFFLINE')).strip().upper()
        changed = (not self._trade_stream_known) or active != self._trade_stream_active or normalized_reason != self._trade_stream_reason
        self._trade_stream_known = True
        self._trade_stream_active = active
        self._trade_stream_reason = normalized_reason
        if not changed:
            return
        self._prepared_sequence = -1
        if hasattr(self, '_row_metrics'):
            self._prepare_display(reuse_rows=True)
        self.update()

    def _feed_health_status(self, snapshot: OrderFlowSnapshot | None) -> tuple[str, str]:
        if self._book_validity_known and not self._book_valid:
            reason = self._book_valid_reason or 'ORDER BOOK SYNCING'
            return ('STALE' if snapshot is not None and snapshot.ready else 'SYNCING', reason)
        if snapshot is None or not snapshot.ready:
            return ('CONNECTING', 'WAITING FOR SYNCHRONIZED DEPTH AND QUOTES')
        elapsed = max(0.0, time.monotonic() - snapshot.generated_monotonic)
        depth_age = None if snapshot.depth_age_seconds is None else snapshot.depth_age_seconds + elapsed
        bbo_age = None if snapshot.bbo_age_seconds is None else snapshot.bbo_age_seconds + elapsed
        if not snapshot.live or not book_data_is_fresh(depth_age, bbo_age):
            return ('STALE', 'BOOK STALE · SYNCHRONIZED')
        warnings: list[str] = []
        if self._trade_stream_known and not self._trade_stream_active:
            reason = self._trade_stream_reason or 'TRADE STREAM OFFLINE'
            if reason != 'PAUSED':
                warnings.append(reason)
        if depth_age is not None and math.isfinite(depth_age) and depth_age >= self.DEPTH_DEGRADED_SECONDS:
            warnings.append(self._age_text('DEPTH', depth_age))
        if bbo_age is not None and math.isfinite(bbo_age) and bbo_age >= self.BBO_DEGRADED_SECONDS:
            warnings.append(self._age_text('QUOTE', bbo_age))
        if warnings:
            return ('DEGRADED', '  •  '.join(dict.fromkeys(warnings)))
        return ('LIVE', '')

    def _syncing_message(self) -> str:
        if self._book_validity_known and not self._book_valid:
            age_ms = self._book_age_ms(self.snapshot)
            age = self._latency_ms_text(age_ms) if age_ms is not None else '--'
            reason = f' · {self._book_valid_reason}' if self._book_valid_reason else ''
            return f'LAST KNOWN · RESYNC{reason} · AGE {age}'
        return 'ORDER BOOK SYNCING'

    def set_snapshot(self, payload: object) -> None:
        """Accept the newest application-published frame without preparing it inline.

        ``PresentationClock.frame`` is delivered synchronously on the GUI thread.
        Keep the handoff cheap and retain only the newest snapshot. Bucket analysis
        runs in a worker; the DOM-owned event commits visible row presentation.
        """
        frame: OrderFlowPresentationFrame | None = None
        timing: dict[str, float] = {}
        snapshot: object = payload
        if (
            isinstance(payload, tuple)
            and len(payload) == 2
            and isinstance(payload[0], OrderFlowPresentationFrame)
            and isinstance(payload[1], dict)
        ):
            frame = payload[0]
            snapshot = frame.snapshot
            for key, value in payload[1].items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(numeric) and numeric > 0.0:
                    timing[str(key)] = numeric
        elif isinstance(payload, OrderFlowPresentationFrame):
            frame = payload
            snapshot = payload.snapshot
            for key in (
                'socket_received_mono', 'parser_done_mono', 'gui_dispatch_mono',
                'main_depth_received_mono', 'worker_emit_mono', 'worker_received_mono',
                'worker_depth_processed_mono',
            ):
                try:
                    numeric = float(getattr(frame, key, 0.0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(numeric) and numeric > 0.0:
                    timing[key] = numeric
        if not isinstance(snapshot, OrderFlowSnapshot):
            return
        if snapshot.symbol and snapshot.symbol != self.symbol:
            return
        if snapshot.sequence <= self._latest_received_sequence:
            return
        self._initialize_profile_grouping(snapshot)
        now = time.perf_counter()
        self._latest_received_sequence = snapshot.sequence
        self.source_snapshot = snapshot
        self._snapshot_received_at[snapshot.sequence] = now
        timing['dom_received_mono'] = now
        if timing:
            self._snapshot_pipeline_timing[snapshot.sequence] = timing
        if frame is not None:
            if frame.build_started_mono > 0.0:
                self._snapshot_build_started_at[snapshot.sequence] = float(frame.build_started_mono)
            if frame.build_completed_mono > 0.0:
                self._snapshot_build_completed_at[snapshot.sequence] = float(frame.build_completed_mono)
            if frame.build_started_mono > 0.0 and frame.build_completed_mono >= frame.build_started_mono:
                self._snapshot_build_samples.append((float(frame.build_completed_mono) - float(frame.build_started_mono)) * 1000.0)
        self._snapshot_received_count += 1
        if (
            self._snapshot_received_count % self.SNAPSHOT_TIMING_PRUNE_INTERVAL == 0
            or len(self._snapshot_received_at) > 320
        ):
            self._prune_snapshot_timing(snapshot.sequence)

        if self._pending_source_snapshot is not None:
            self._snapshot_coalesced_count += 1
            self._snapshot_superseded_before_row_count += 1
        self._pending_source_snapshot = snapshot
        self._schedule_snapshot_prepare()

    def _flush_pending_snapshot(self) -> None:
        snapshot = self._pending_source_snapshot
        if snapshot is None:
            return
        self._pending_source_snapshot = None
        if self._resize_prepare_timer.isActive():
            self._resize_prepare_timer.stop()
        now = time.perf_counter()
        self._last_snapshot_prepare_at = now
        self._snapshot_prepare_started_at[snapshot.sequence] = now
        timing = self._snapshot_pipeline_timing.get(snapshot.sequence)
        if timing is not None:
            timing['dom_prepare_started_mono'] = now
        self._commit_full_snapshot(snapshot, force=self.snapshot is None or not snapshot.ready)

    def _invalidate_aggregation(self) -> None:
        self._aggregation_epoch += 1
        self._aggregation_job.invalidate()

    def _aggregation_context(self) -> tuple:
        return (self._aggregation_epoch, self.symbol,
                self.aggregation_multiplier, self.price_tick_size, self._book_depth)

    def _commit_full_snapshot(self, snapshot: OrderFlowSnapshot, *, force: bool=False) -> None:
        if self.aggregation_multiplier > 1 and self.price_tick_size > 0:
            context = self._aggregation_context()
            self._aggregation_job.submit(
                (context, snapshot.sequence, bool(force)),
                self._aggregation_worker.prepare, context, snapshot,
            )
            return
        self._adopt_full_snapshot(snapshot, snapshot, force=force)

    @QtCore.Slot(object, object)
    def _aggregation_ready(self, key, result) -> None:
        context, sequence, force = key
        if context != self._aggregation_context():
            return
        if sequence < self._latest_applied_sequence:
            return
        snapshot, display, started, completed = result
        latest = self.source_snapshot
        if latest is not None and not latest.ready and snapshot.ready:
            return
        timing = self._snapshot_pipeline_timing.get(sequence)
        if timing is not None:
            timing['aggregation_started_mono'] = started
            timing['aggregation_completed_mono'] = completed
        self._adopt_full_snapshot(snapshot, display, force=force)

    @QtCore.Slot(object, str)
    def _aggregation_failed(self, key, message) -> None:
        if key[0] == self._aggregation_context():
            # Retain the last complete frame; freshness still expires locally.
            import logging
            logging.getLogger(__name__).error("DOM aggregation failed: %s", message)

    def _adopt_full_snapshot(self, snapshot, display_snapshot, *, force=False) -> None:
        previous_source = self._applied_source_snapshot
        self._latest_applied_sequence = snapshot.sequence
        self._market_anchor_snapshot = snapshot
        self._apply_source_snapshot(
            snapshot, display_snapshot=display_snapshot,
            force=force, previous_source=previous_source,
        )
        self._applied_source_snapshot = snapshot
        self._snapshot_full_applied_count += 1
        self._freshness_timer.stop()
        if snapshot.live and snapshot.depth_age_seconds is not None and snapshot.bbo_age_seconds is not None:
            elapsed = max(0.0, time.monotonic() - snapshot.generated_monotonic)
            remaining = min(
                BOOK_DEPTH_FRESH_SECONDS - snapshot.depth_age_seconds,
                BOOK_BBO_FRESH_SECONDS - snapshot.bbo_age_seconds,
            ) - elapsed
            if remaining >= 0.0:
                self._freshness_timer.start(max(1, int(math.ceil(remaining * 1000.0)) + 1))

    def _expire_snapshot_freshness(self) -> None:
        # A stalled worker cannot deliver the next frame that marks itself stale.
        self._prepare_display(reuse_rows=True)
        self.update()

    def _update_last_trade_anchor(self, snapshot: OrderFlowSnapshot) -> tuple[float, QtGui.QColor]:
        """Latch the newest normalized print and return the primary DOM price."""
        if snapshot.recent_prints:
            latest = snapshot.recent_prints[-1]
            if latest.price > 0.0 and math.isfinite(latest.price):
                self._last_trade_price = float(latest.price)
                self._last_trade_side = str(latest.aggressor_side or '').lower()
        value = self._last_trade_price or snapshot.midpoint or snapshot.microprice
        if self._last_trade_side == 'buy':
            color = self._bid
        elif self._last_trade_side == 'sell':
            color = self._ask
        else:
            color = self._mid
        return (value, color)

    def _header_title_region(self) -> QtGui.QRegion:
        top = max(0, int(math.floor(float(self._geometry.get('header_top', 0.0)))) - 1)
        height = int(math.ceil(float(self._geometry.get('title_height', 0.0)))) + 2
        return QtGui.QRegion(0, top, self.width(), max(1, height))

    def _center_region(self) -> QtGui.QRegion:
        if self._book_depth:
            g = self._geometry
            center = float(g.get('center_top', 0.0))
            step = float(g.get('profile_step', 0.0))
            current = center
            if self._last_trade_price > 0.0 and step > 0.0:
                bucket = math.floor(self._last_trade_price / step + 1e-7) * step
                side = 'ask' if self.snapshot is not None and self._last_trade_price >= self.snapshot.midpoint else 'bid'
                current = self._profile_price_row_top(bucket, side) + float(g['row_height']) * 0.5
            previous = self._profile_last_drawn_y if self._profile_last_drawn_y is not None else center
            half_row = float(g['row_height']) * 0.5 + 1.0
            top = max(0, int(math.floor(min(center - 1.0, previous - half_row, current - half_row))))
            bottom = min(self.height(), int(math.ceil(max(float(g['center_bottom']) + 1.0, previous + half_row, current + half_row))))
            return QtGui.QRegion(0, top, self.width(), max(0, bottom - top))
        top = max(0, int(math.floor(float(self._geometry.get('center_top', 0.0)))) - 1)
        height = int(math.ceil(float(self._geometry.get('center_height', 0.0)))) + 2
        return QtGui.QRegion(0, top, self.width(), max(1, height))

    @staticmethod
    def _color_signature(color: QtGui.QColor | None) -> int:
        return -1 if color is None else int(color.rgba())

    def _prepared_row_signature(self, row: PreparedDomRow) -> tuple[object, ...]:
        """Return only state that can change painted pixels for this row.

        The previous signature embedded the complete OrderFlowDisplayLevel plus
        scale-only fields that the normal DOM never paints. Changes in diagnostic,
        depth, or unused normalized analytics therefore dirtied rows and could push
        the ladder over the full-repaint threshold with no visual change.
        """
        level = row.level
        if not isinstance(level, OrderFlowDisplayLevel):
            return ()
        columns = self._geometry.get('columns', {})
        memory_signature = (
            tuple(level.liquidity_history_30s[-8:])
            if isinstance(columns, dict) and 'memory' in columns
            else ()
        )
        cue_kind = str(row.change_cue[0]) if row.change_cue is not None else ''
        delta_signature = (
            round(float(level.notional), 6),
            round(float(level.delta_notional_5s), 6),
        ) if isinstance(columns, dict) and 'delta' in columns else ()
        flow_signature = (
            round(float(level.trade_notional_5s), 6),
            round(float(level.signed_trade_notional_5s), 6),
            row.sell_large, row.buy_large,
        ) if isinstance(columns, dict) and 'flow' in columns else ()
        signature: tuple[object, ...] = (
            level.side, round(float(level.price), 12),
            round(float(level.notional), 6),
            memory_signature, delta_signature, flow_signature,
            row.display_state, row.price_text, row.notional_text,
            row.event_text, row.event_kind,
            row.market_signal_text,
            self._color_signature(row.market_signal_color),
            self._color_signature(row.market_signal_fill),
            round(row.liquidity_visual, 6),
            cue_kind, self._color_signature(row.change_cue_color),
            row.is_native_touch, round(row.native_touch_fraction, 6),
            row.is_ltp_row, round(row.ltp_fraction, 6),
        )
        if self._book_depth:
            signature += (
                round(row.profile_size, 6), round(row.profile_depth, 6),
                round(row.profile_previous_depth, 6),
                round(row.profile_amount, 8), round(row.profile_cumulative, 8), row.profile_in_range,
            )
        return signature

    def _snapshot_chrome_signature(self) -> tuple[tuple[object, ...], ...]:
        """Return independently-diffable signatures for painted chrome bands.

        The old signature treated title/status, metrics, center/BBO and footer as
        one unit. Fast-moving metrics therefore repainted every chrome band even
        when those other bands were byte-for-byte unchanged. Keep the state
        grouped by the regions the painter can update independently.
        """
        if self._book_depth:
            return (
                (self.symbol, self._market_status, self._profile_header_key,
                 self._profile_ruler_totals if self._geometry.get('profile_totals_height') else ()), (),
                (self._last_trade_price, self.snapshot.midpoint if self.snapshot is not None else 0.0,
                 self._profile_ruler_totals),
                (self._profile_anchor_price, self._geometry.get('profile_ask_anchor'),
                 self._geometry.get('profile_rulers')),
            )
        title_header: list[tuple[str, str, int]] = []
        for key in ('symbol', 'status', 'latency', 'latency_compact', 'source', 'spread'):
            text, color = self._header_text.get(key, ('', None))
            title_header.append((key, str(text), self._color_signature(color)))
        prepared_header = tuple(
            sorted((str(key), repr(value)) for key, value in self._prepared_header_layout.items())
        )
        metrics = tuple(
            (str(label), str(value), self._color_signature(color))
            for label, value, color in self._prepared_metric_draw_items
        )
        footer = tuple(
            (str(text), self._color_signature(color))
            for text, color in self._prepared_footer_draw_items
        )
        center: list[tuple[str, str, int]] = []
        for key in ('mid', 'mid_label', 'best_bid', 'best_ask'):
            text, color = self._header_text.get(key, ('', None))
            center.append((key, str(text), self._color_signature(color)))
        return (
            (str(self._market_status), tuple(title_header), prepared_header),
            (str(self._market_status), metrics),
            tuple(center),
            (footer, self._profile_footer_text, self._profile_totals) if self._book_depth else (footer, self._footer_depth_summary),
        )

    def _header_metric_region(self) -> QtGui.QRegion:
        top = float(self._geometry.get('header_top', 0.0)) + float(self._geometry.get('title_height', 0.0))
        height = float(self._geometry.get('metric_height', 0.0))
        region_top = max(0, int(math.floor(top)) - 1)
        region_height = int(math.ceil(height)) + 2
        return QtGui.QRegion(0, region_top, self.width(), max(1, region_height))

    def _footer_region(self) -> QtGui.QRegion:
        if self._book_depth:
            return QtGui.QRegion(self.rect())
        footer_top = max(0, int(math.floor(float(self._geometry.get('footer_top', 0.0)))) - 1)
        footer_bottom = min(self.height(), int(math.ceil(float(self._geometry.get('footer_bottom', footer_top)))) + 1)
        if footer_bottom > footer_top:
            return QtGui.QRegion(0, footer_top, self.width(), footer_bottom - footer_top)
        return QtGui.QRegion()

    def _snapshot_chrome_region(
        self,
        previous: tuple[tuple[object, ...], ...],
        current: tuple[tuple[object, ...], ...],
    ) -> QtGui.QRegion:
        regions = (
            self._header_title_region(),
            self._header_metric_region(),
            self._center_region(),
            self._footer_region(),
        )
        region = QtGui.QRegion()
        for index, band in enumerate(regions):
            old_signature = previous[index] if index < len(previous) else None
            new_signature = current[index] if index < len(current) else None
            if old_signature != new_signature:
                region += band
        return region

    def _snapshot_dirty_region(
        self,
        previous_signatures: dict[tuple[str, int | float], tuple[object, ...]],
        previous_rects: dict[tuple[str, int | float], QtCore.QRect],
        current_signatures: dict[tuple[str, int | float], tuple[object, ...]],
        current_rects: dict[tuple[str, int | float], QtCore.QRect],
        *,
        chrome_region: QtGui.QRegion,
    ) -> QtGui.QRegion:
        region = QtGui.QRegion(chrome_region)
        all_keys = previous_signatures.keys() | current_signatures.keys()
        changed_keys: list[tuple[str, int | float]] = []
        repaint_rects: list[QtCore.QRect] = []
        for key in all_keys:
            if previous_signatures.get(key) == current_signatures.get(key) and previous_rects.get(key) == current_rects.get(key):
                continue
            changed_keys.append(key)
            old_rect = previous_rects.get(key)
            new_rect = current_rects.get(key)
            if old_rect is not None and not old_rect.isNull():
                repaint_rects.append(QtCore.QRect(old_rect))
            if new_rect is not None and not new_rect.isNull():
                repaint_rects.append(QtCore.QRect(new_rect))

        # Once most visible rows changed, constructing a fragmented row region
        # costs more than repainting the already-small DOM surface. Keep the
        # fallback conservative; unlike the original 15% suggestion this does
        # not turn ordinary localized updates into full paints.
        visible_row_count = max(len(previous_signatures), len(current_signatures))
        if (
            visible_row_count >= 8
            and len(changed_keys) / visible_row_count >= self.DIRTY_REGION_FULL_REPAINT_ROW_RATIO
        ):
            return QtGui.QRegion(self.rect())

        if not repaint_rects:
            return region

        # Row rectangles share the same horizontal ladder extent. Merge touching
        # old/new rectangles into vertical bands before entering QRegion, avoiding
        # one Python→Qt union per changed row while repainting the same pixels.
        unique_rects = {
            (rect.x(), rect.y(), rect.width(), rect.height()): rect
            for rect in repaint_rects
        }
        ordered = sorted(unique_rects.values(), key=lambda rect: (rect.x(), rect.width(), rect.top(), rect.bottom()))
        bands: list[QtCore.QRect] = []
        for rect in ordered:
            if not bands:
                bands.append(QtCore.QRect(rect))
                continue
            previous = bands[-1]
            same_span = previous.x() == rect.x() and previous.width() == rect.width()
            if same_span and rect.top() <= previous.bottom() + 2:
                bands[-1] = previous.united(rect)
            else:
                bands.append(QtCore.QRect(rect))
        for band in bands:
            region += QtGui.QRegion(band)
        return region

    def _row_rect_for_marker_key(self, side: str, marker_key: int | float) -> QtCore.QRect:
        rect = self._prepared_row_rects.get((side, marker_key))
        return QtCore.QRect(rect) if rect is not None else QtCore.QRect()

    def _row_rect_for_market_price(self, side: str, price: float) -> QtCore.QRect:
        if price <= 0.0 or not math.isfinite(price):
            return QtCore.QRect()
        display_price = self._display_bucket_price(price, side)
        return self._row_rect_for_marker_key(side, self._marker_key(display_price))

    def _market_signal_anchor_is_visible(self, snapshot: OrderFlowSnapshot) -> bool:
        """Keep an event badge only while its original price bucket remains visible."""
        signal = self._market_signal
        if not isinstance(signal, dict):
            return True
        side = str(signal.get('anchor_side', '') or '')
        try:
            price = float(signal.get('anchor_price', 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return False
        if side not in {'bid', 'ask'} or price <= 0.0 or not math.isfinite(price):
            return False
        rows = max(0, int(self._geometry.get('rows_per_side', 0)))
        if rows <= 0:
            # A BBO-only/shallow layout cannot render a row badge. Do not destroy
            # the event merely because the user resized the panel.
            return True
        levels = snapshot.bid_levels if side == 'bid' else snapshot.ask_levels
        anchor_key = self._marker_key(self._display_bucket_price(price, side))
        return any(
            self._marker_key(level.price) == anchor_key
            for level in levels[:rows]
        )

    def _region_summary(self, region: QtGui.QRegion) -> str:
        if region.isEmpty():
            return 'none'
        bounds = region.boundingRect()
        surface_area = max(1, self.width() * self.height())
        bbox_area = max(0, bounds.width()) * max(0, bounds.height())
        pct = bbox_area / surface_area * 100.0
        return f'x={bounds.x()} y={bounds.y()} w={bounds.width()} h={bounds.height()} rects={region.rectCount()} bbox={pct:.1f}%'

    def _request_repaint_region(self, reason: str, region: QtGui.QRegion) -> None:
        if self.width() <= 0 or self.height() <= 0 or region.isEmpty():
            return
        clipped = region.intersected(QtGui.QRegion(self.rect()))
        if clipped.isEmpty():
            return
        self._requested_partial_repaint_count += 1
        self._repaint_request_reason_counts[str(reason)] += 1
        self._last_repaint_request_reason = str(reason)
        self._last_repaint_request_region = self._region_summary(clipped)
        self.update(clipped)

    def _prune_snapshot_timing(self, newest_sequence: int) -> None:
        floor = newest_sequence - 240
        for mapping in (
            self._snapshot_received_at, self._snapshot_build_started_at, self._snapshot_build_completed_at,
            self._snapshot_prepare_started_at, self._snapshot_prepare_completed_at, self._snapshot_pipeline_timing,
        ):
            for sequence in tuple(mapping):
                if sequence < floor:
                    mapping.pop(sequence, None)

    @staticmethod
    def _prepared_level_source_signature(level: OrderFlowDisplayLevel) -> tuple[object, ...]:
        """Source fields that require rebuilding a PreparedDomRow.

        Backend normalization-only fields (delta/trade/depth intensity and
        cumulative depth) are intentionally excluded. They are not painted by the
        normal DOM and must not defeat row reuse when only global scales move.
        """
        return (
            level.side, level.price, level.quantity, level.notional,
            level.delta_notional_5s, level.trade_notional_5s,
            level.signed_trade_notional_5s, level.state,
            tuple(level.liquidity_history_30s[-8:]),
            level.largest_buy_trade_5s, level.largest_sell_trade_5s,
            level.semantic_event_kind, level.semantic_event_label,
            level.recent_replenished_notional, level.recent_restacked_notional,
            level.trade_reload_count, level.restack_count,
        )

    @classmethod
    def _visible_level_rows_equivalent(
        cls,
        current: tuple[OrderFlowDisplayLevel, ...],
        previous: tuple[OrderFlowDisplayLevel, ...],
        limit: int,
    ) -> bool:
        count = max(0, int(limit))
        current_rows = current[:count]
        previous_rows = previous[:count]
        if len(current_rows) != len(previous_rows):
            return False
        return all(
            cls._prepared_level_source_signature(left)
            == cls._prepared_level_source_signature(right)
            for left, right in zip(current_rows, previous_rows)
        )

    def _apply_source_snapshot(self, snapshot: OrderFlowSnapshot, *, display_snapshot: OrderFlowSnapshot, force: bool=False, previous_source: OrderFlowSnapshot | None=None) -> None:
        apply_started = time.perf_counter()
        previous = self.snapshot
        if not force and previous is not None and (display_snapshot.sequence <= previous.sequence):
            return
        # Hold the previous immutable-by-convention maps by reference. The next
        # _prepare_display() installs fresh dicts rather than mutating these.
        previous_signatures = self._prepared_row_signatures
        previous_rects = self._prepared_row_rects
        previous_chrome_signature = self._prepared_chrome_signature
        previous_geometry_key = self._geometry_cache_key
        previous_ready = bool(previous is not None and previous.ready)
        prepared_size_matches = self._prepared_size == (max(1, self.width()), max(1, self.height()))
        rows_unchanged = False
        # Forced commits follow display/rule changes; equal source levels do
        # not imply that their cached price and amount text is still valid.
        if not force and previous is not None and prepared_size_matches:
            if levels_unchanged(display_snapshot, previous):
                rows_unchanged = True
                self._row_revision_reuses += 1
            else:
                # Changed components can leave the currently visible subset
                # unchanged. Keep the existing presentation-field comparison.
                visible_rows = max(0, int(self._geometry.get('rows_per_side', 0)))
                if visible_rows > 0:
                    rows_unchanged = (
                        self._visible_level_rows_equivalent(
                            display_snapshot.bid_levels, previous.bid_levels, visible_rows
                        )
                        and self._visible_level_rows_equivalent(
                            display_snapshot.ask_levels, previous.ask_levels, visible_rows
                        )
                    )
        self.snapshot = display_snapshot
        market_signal_cleared = False
        if not self._market_signal_anchor_is_visible(display_snapshot):
            self._market_signal = None
            self._market_signal_expiry_timer.stop()
            market_signal_cleared = True
        self._prepare_display(reuse_rows=rows_unchanged and not market_signal_cleared)
        requires_full_repaint = bool(
            force
            or previous is None
            or previous_ready != bool(display_snapshot.ready)
            or previous_geometry_key != self._geometry_cache_key
        )
        if requires_full_repaint:
            self._required_paint_rows_seen.discard(display_snapshot.sequence)
            self._required_paint_regions[display_snapshot.sequence] = QtGui.QRegion(self.rect())
            self.last_diff_ms = 0.0
            self.last_snapshot_apply_ms = max(0.0, (time.perf_counter() - apply_started) * 1000.0)
            self.max_snapshot_apply_ms = max(self.max_snapshot_apply_ms, self.last_snapshot_apply_ms)
            self.update()
            return
        diff_started = time.perf_counter()
        chrome_region = self._snapshot_chrome_region(
            previous_chrome_signature,
            self._prepared_chrome_signature,
        )
        dirty_region = self._snapshot_dirty_region(
            previous_signatures,
            previous_rects,
            self._prepared_row_signatures,
            self._prepared_row_rects,
            chrome_region=chrome_region,
        )
        self.last_diff_ms = max(0.0, (time.perf_counter() - diff_started) * 1000.0)
        self.max_diff_ms = max(self.max_diff_ms, self.last_diff_ms)
        if not dirty_region.isEmpty():
            self._required_paint_rows_seen.discard(display_snapshot.sequence)
            self._required_paint_regions[display_snapshot.sequence] = QtGui.QRegion(dirty_region)
        self._request_repaint_region(
            'row_snapshot',
            dirty_region,
        )
        self.last_snapshot_apply_ms = max(0.0, (time.perf_counter() - apply_started) * 1000.0)
        self.max_snapshot_apply_ms = max(self.max_snapshot_apply_ms, self.last_snapshot_apply_ms)

    @staticmethod
    def _age_text(label: str, age: float | None) -> str:
        if age is None or not math.isfinite(age) or age < 0.0:
            return f'{label} --'
        if age < 10.0:
            return f'{label} {age:.1f}s'
        return f'{label} {age:.0f}s'

    @staticmethod
    def _latency_ms_text(value_ms: float | None) -> str:
        """Compact millisecond text for trader-facing DOM freshness telemetry."""
        if value_ms is None or not math.isfinite(value_ms) or value_ms < 0.0:
            return '--'
        if value_ms < 10.0:
            return f'{value_ms:.1f}ms'
        if value_ms < 1000.0:
            return f'{value_ms:.0f}ms'
        return f'{value_ms / 1000.0:.1f}s'

    def _book_age_ms(self, snapshot: OrderFlowSnapshot | None) -> float | None:
        """Age of received depth provenance, falling back to analyzer depth age."""
        if snapshot is not None:
            timing = self._snapshot_pipeline_timing.get(snapshot.sequence, {})
            socket_received = timing.get('socket_received_mono')
            if socket_received is not None and math.isfinite(socket_received) and socket_received > 0.0:
                return max(0.0, (time.perf_counter() - socket_received) * 1000.0)
        if snapshot is None or snapshot.depth_age_seconds is None:
            return None
        try:
            base_age = float(snapshot.depth_age_seconds)
            generated = float(snapshot.generated_monotonic)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(base_age) or base_age < 0.0:
            return None
        elapsed = 0.0
        if math.isfinite(generated) and generated > 0.0:
            elapsed = max(0.0, time.monotonic() - generated)
        return (base_age + elapsed) * 1000.0

    def _latency_display_values(
        self,
        snapshot: OrderFlowSnapshot | None,
    ) -> tuple[float | None, float | None, float | None, float | None, float | None]:
        """Return throttled BOOK age plus measured presentation latency."""
        book_ms = self._book_age_ms(snapshot)
        dom_samples = self._latency_stage_samples['dom_to_paint']
        dom_ms = self.last_snapshot_to_paint_ms if self._has_snapshot_to_paint_sample else None
        now = time.monotonic()
        cutoff = now - self.LATENCY_MAX_DISPLAY_WINDOW_SECONDS
        if book_ms is not None and math.isfinite(book_ms) and book_ms >= 0.0:
            self._latency_book_peak_ms = max(self._latency_book_peak_ms, book_ms)
            self._latency_book_display_window.append((now, book_ms))
        if dom_ms is not None and math.isfinite(dom_ms) and dom_ms >= 0.0:
            self._latency_dom_peak_ms = max(self._latency_dom_peak_ms, dom_ms)
            self._latency_dom_display_window.append((now, dom_ms))
        while self._latency_book_display_window and self._latency_book_display_window[0][0] < cutoff:
            self._latency_book_display_window.popleft()
        while self._latency_dom_display_window and self._latency_dom_display_window[0][0] < cutoff:
            self._latency_dom_display_window.popleft()

        if (
            self._latency_display_updated_at <= 0.0
            or now - self._latency_display_updated_at >= self.LATENCY_DISPLAY_INTERVAL_SECONDS
        ):
            # Percentile calculation sorts the bounded sample deque. It is only
            # trader-visible at 1 Hz, so never do that sort at snapshot cadence.
            dom_p95_ms = self._percentile(dom_samples, 0.95) if dom_samples else None
            self._latency_display_updated_at = now
            self._latency_display_book_ms = book_ms
            self._latency_display_dom_ms = dom_ms
            self._latency_display_dom_p95_ms = dom_p95_ms
            self._latency_display_book_max_ms = max(
                (value for _stamp, value in self._latency_book_display_window),
                default=book_ms,
            )
            self._latency_display_dom_max_ms = max(
                (value for _stamp, value in self._latency_dom_display_window),
                default=dom_ms,
            )
        return (
            self._latency_display_book_ms,
            self._latency_display_book_max_ms,
            self._latency_display_dom_ms,
            self._latency_display_dom_p95_ms,
            self._latency_display_dom_max_ms,
        )

    def _record_latency_stage(self, name: str, start: float | None, end: float | None) -> float | None:
        if start is None or end is None or start <= 0.0 or end < start:
            return None
        value = (end - start) * 1000.0
        samples = self._latency_stage_samples.get(name)
        if samples is None:
            return None
        samples.append(value)
        self._latency_stage_max_ms[name] = max(self._latency_stage_max_ms.get(name, 0.0), value)
        return value

    def _fitted_price_text(self, value: float, metrics: QtGui.QFontMetricsF, width: float) -> str:
        del metrics, width
        return self._price_text(value)

    def _required_price_lane_width(self, snapshot: OrderFlowSnapshot | None) -> float:
        """Width that preserves every exact price digit at the readability floor."""
        widest_text = self._update_price_format_envelope(snapshot)
        if self._book_depth:
            # Reserve native precision at the configured font size. Grouping
            # may shorten row labels, but must not narrow their lane and shrink
            # the font that also renders the midpoint.
            return max(42.0, float(math.ceil(self._price_metrics.horizontalAdvance(widest_text) + 8.0)))
        font = typography_font_at_pixel_size(
            self._price_font, typography_min_pixel_size(TextRole.ORDERBOOK_PRICE)
        )
        required = self._price_metrics_for_font(font).horizontalAdvance(widest_text) + (16.0 if self._book_depth else 4.0)
        return max(90.0, float(math.ceil(required)))

    def _required_amount_lane_width(self, snapshot: OrderFlowSnapshot | None) -> float:
        if snapshot is None or self._book_depth:
            return 36.0
        revision = amount_revision_key(snapshot)
        context = (snapshot.symbol, self._value_mode, self._row_font_key)
        key = (context, revision)
        if revision is not None and key == self._amount_width_cache_key:
            self._amount_width_cache_hits += 1
            return self._amount_width_cache_value
        # A changed source version can affect only far-depth rows, or group
        # into the same displayed amounts. Verify the measured inputs before
        # paying for formatting/font metrics. Legacy producers use this path.
        inputs = amount_inputs(snapshot, 64)
        if context == self._amount_width_cache_context and inputs == self._amount_width_cache_inputs:
            self._amount_width_cache_key = key
            self._amount_width_cache_hits += 1
            return self._amount_width_cache_value
        self._amount_width_cache_misses += 1
        texts = [
            self._row_amount(notional, price, quantity=quantity)
            for side in inputs for price, quantity, notional in side
        ]
        widest = max((self._row_metrics.horizontalAdvance(text) for text in texts), default=0.0)
        measured = max(36.0, min(112.0, float(math.ceil(widest + 7.0))))
        self._amount_width_cache_key = key
        self._amount_width_cache_context, self._amount_width_cache_inputs = context, inputs
        self._amount_width_cache_value = measured
        return measured

    def _required_state_lane_width(self) -> float:
        """Width at which STATE/event text may switch from acronyms to full labels."""
        candidates = [
            'STATE', 'RELOAD ×50', 'RESTACK ×50', 'SELL REJECT', 'BUY REJECT',
            'TWO-WAY', 'BREAKOUT 99', 'BREAKDOWN 99',
            *(str(label).upper() for label in self._STATE_LABELS.values()),
        ]
        widest = max((self._label_metrics.horizontalAdvance(text) for text in candidates), default=0.0)
        return max(
            float(ORDERBOOK_STATE_FULL_LABEL_WIDTH),
            float(_REFERENCE_FULL_WEIGHTS.get('state', ORDERBOOK_STATE_FULL_LABEL_WIDTH)),
            float(math.ceil(widest + 8.0)),
        )

    def _update_visual_scales(self, snapshot: OrderFlowSnapshot) -> bool:
        """Track the newest snapshot scales without animating the live book.

        Resting-liquidity bars represent the current exchange book.  Animating the
        normalization denominator between source depth events makes unchanged rows
        expand/shrink on unrelated high-rate BBO/trade snapshots.  Keep these scale
        values event-driven and current; temporal continuity is provided separately
        by change cues and liquidity-history rails.
        """
        targets = {
            'liquidity': max(0.0, float(snapshot.liquidity_scale)),
            'delta': max(0.0, float(snapshot.delta_scale)),
            'trade': max(0.0, float(snapshot.trade_scale)),
            'depth': max(0.0, float(snapshot.cumulative_depth_scale)),
        }
        liquidity_changed = False
        depth_changed = False
        for key, target in targets.items():
            previous = float(self._visual_scales[key])
            self._visual_scales[key] = target
            tolerance = max(1e-09, abs(previous) * 0.004)
            if key == 'liquidity':
                liquidity_changed = abs(target - previous) > tolerance
            elif key == 'depth':
                depth_changed = abs(target - previous) > tolerance
        # Normal DOM paints only liquidity_visual. DEPTH mode also consumes the
        # cumulative-depth normalization, so refresh that scale only while active.
        return liquidity_changed or (self._book_depth and depth_changed)

    def _visual_intensity(self, value: float, scale_key: str) -> float:
        scale = self._visual_scales.get(scale_key, 0.0)
        if scale <= 1e-12 or not math.isfinite(value) or value == 0.0:
            return 0.0
        ratio = max(0.0, abs(value) / scale)
        gamma = 8.0
        return min(1.0, math.log1p(gamma * ratio) / math.log1p(gamma))

    def _display_state(
        self,
        level: OrderFlowDisplayLevel,
        now: float,
        *,
        allow_transition: bool=True,
        inference_allowed: bool | None=None,
    ) -> str:
        """Latch STATE badges only after the classifier repeats on a new snapshot.

        A single analytical refresh can contain transient depth churn. Requiring
        the same non-NORMAL classification on two distinct row analyses
        removes one-frame labels without adding a fixed time delay that behaves
        differently at different feed rates.
        """
        key = (level.side, self._marker_key(level.price))
        if inference_allowed is None:
            status, _reason = self._feed_health_status(self.snapshot)
            inference_allowed = not (
                self.aggregation_multiplier > 1
                or status in {'STALE', 'SYNCING', 'CONNECTING'}
                or (self._trade_stream_known and not self._trade_stream_active)
            )
        if not inference_allowed:
            self._state_latches.pop(key, None)
            return 'NORMAL'
        raw = level.state if level.state in self._STATE_LABELS else 'NORMAL'
        # A BBO-only publication may reuse cached analytics. It is not a second
        # independent confirmation of that row's classification.
        sequence = int(level.analysis_revision or getattr(self.snapshot, 'sequence', 0) or 0)
        entry = self._state_latches.get(key)
        if entry is None:
            if raw == 'NORMAL':
                self._state_latches[key] = {
                    'display': 'NORMAL', 'candidate': '', 'candidate_since': now,
                    'candidate_sequence': sequence, 'candidate_hits': 0,
                    'last_active': 0.0, 'last_seen': now, 'last_sequence': sequence,
                }
                return 'NORMAL'
            self._state_latches[key] = {
                'display': 'NORMAL', 'candidate': raw, 'candidate_since': now,
                'candidate_sequence': sequence, 'candidate_hits': 1,
                'last_active': 0.0, 'last_seen': now, 'last_sequence': sequence,
            }
            return 'NORMAL'

        previous_seen_sequence = int(entry.get('last_sequence', 0) or 0)
        entry['last_seen'] = now
        if sequence > 0:
            entry['last_sequence'] = sequence
        display = str(entry.get('display', 'NORMAL'))
        if not allow_transition:
            return display
        if raw == display:
            entry['candidate'] = ''
            entry['candidate_hits'] = 0
            entry['candidate_sequence'] = sequence
            # UI-only prepares (resize, hover-adjacent work, microstructure
            # repaint) must not extend the 350 ms hold. Refresh last_active only
            # when a distinct analytical snapshot confirms the displayed state.
            if raw != 'NORMAL' and (sequence <= 0 or sequence != previous_seen_sequence):
                entry['last_active'] = now
            return display
        if raw == 'NORMAL':
            entry['candidate'] = ''
            entry['candidate_hits'] = 0
            entry['candidate_sequence'] = sequence
            last_active = float(entry.get('last_active', 0.0))
            if display != 'NORMAL' and now - last_active < 0.35:
                return display
            entry['display'] = 'NORMAL'
            return 'NORMAL'

        candidate = str(entry.get('candidate', ''))
        if candidate != raw:
            entry['candidate'] = raw
            entry['candidate_since'] = now
            entry['candidate_sequence'] = sequence
            entry['candidate_hits'] = 1
            return display

        previous_sequence = int(entry.get('candidate_sequence', sequence) or 0)
        hits = int(entry.get('candidate_hits', 1))
        if sequence > 0 and sequence != previous_sequence:
            hits += 1
            entry['candidate_sequence'] = sequence
            entry['candidate_hits'] = hits
        elif sequence <= 0 and now - float(entry.get('candidate_since', now)) >= 0.10:
            # Defensive fallback for synthetic/test snapshots without a sequence.
            hits = 2
            entry['candidate_hits'] = hits

        if hits >= 2:
            entry['display'] = raw
            entry['candidate'] = ''
            entry['candidate_hits'] = 0
            entry['last_active'] = now
            return raw
        return display

    def _advance_state_latches(
        self,
        levels: tuple[OrderFlowDisplayLevel, ...],
        now: float,
        *,
        inference_allowed: bool | None=None,
    ) -> set[tuple[str, int | float]]:
        """Advance confirmation once per analytical snapshot, independent of row rebuilds."""
        active: set[tuple[str, int | float]] = set()
        for level in levels:
            if not isinstance(level, OrderFlowDisplayLevel):
                continue
            key = (level.side, self._marker_key(level.price))
            active.add(key)
            self._display_state(
                level, now, allow_transition=True,
                inference_allowed=inference_allowed,
            )
        return active

    def _prune_state_latches(self, now: float, active_keys: set[tuple[str, int | float]] | None=None) -> None:
        # STATE latches represent rows in the currently prepared ladder. Reuse of
        # immutable row visuals must not make a stable WALL/ABSORB look stale.
        if active_keys is not None:
            for key in tuple(self._state_latches):
                if key not in active_keys:
                    self._state_latches.pop(key, None)
        stale_cues = [key for key, entry in self._row_change_cues.items() if float(entry[2]) + 0.75 < now]
        for key in stale_cues:
            self._row_change_cues.pop(key, None)
        if len(self._row_change_cues) > 512:
            oldest = sorted(self._row_change_cues.items(), key=lambda item: float(item[1][2]))
            for key, _entry in oldest[:len(self._row_change_cues) - 512]:
                self._row_change_cues.pop(key, None)

    def _price_metrics_for_font(self, font: QtGui.QFont) -> QtGui.QFontMetricsF:
        key = (font.toString(), round(float(self.devicePixelRatioF()), 3), round(float(self.logicalDpiY()), 2))
        metrics = self._price_metrics_cache.get(key)
        if metrics is not None:
            self._price_metrics_cache.move_to_end(key)
            return metrics
        try:
            metrics = QtGui.QFontMetricsF(font, self)
        except TypeError:
            metrics = QtGui.QFontMetricsF(font)
        if len(self._price_metrics_cache) >= self.PRICE_METRICS_CACHE_CAPACITY:
            self._price_metrics_cache.popitem(last=False)
        self._price_metrics_cache[key] = metrics
        return metrics

    def _update_price_format_envelope(self, snapshot: OrderFlowSnapshot | None) -> str:
        values: list[float] = []
        if snapshot is not None:
            values.extend((snapshot.best_bid, snapshot.best_ask, snapshot.microprice, snapshot.midpoint))
        finite = [abs(float(value)) for value in values if value > 0.0 and math.isfinite(value)]
        if finite:
            maximum = max(finite)
            digits = 1 if maximum < 1.0 else int(math.floor(math.log10(maximum))) + 1
            digits = max(1, digits + 1)
            self._price_envelope_integer_digits = max(self._price_envelope_integer_digits, digits)
        elif self._price_envelope_integer_digits <= 0:
            self._price_envelope_integer_digits = 1
        if self.price_tick_size > 0.0:
            decimals = max(0, min(16, int(self.price_decimals)))
            text = '8' * self._price_envelope_integer_digits
            if decimals:
                text += '.' + '8' * decimals
        else:
            candidates = [self._price_text(value) for value in finite]
            current = max(candidates, key=len, default='0')
            text = max((self._price_envelope_text, current), key=len)
        self._price_envelope_text = text
        return text

    def _fit_effective_price_font(self, snapshot: OrderFlowSnapshot | None) -> None:
        """Fit exact-price text using the DOM's pixel-size typography contract."""
        font = QtGui.QFont(self._price_font)
        base_pixel = int(font.pixelSize())
        if base_pixel <= 0:
            point = max(1.0, float(font.pointSizeF()))
            base_pixel = max(1, int(round(point * max(72.0, float(self.logicalDpiY())) / 72.0)))
        base_pixel = max(typography_min_pixel_size(TextRole.ORDERBOOK_PRICE), base_pixel)
        price_left, price_right = self._geometry['columns']['price']
        price_padding = 8.0 if self._book_depth else 4.0
        available = max(8.0, float(price_right) - float(price_left) - price_padding)
        widest_text = self._update_price_format_envelope(snapshot)
        cache_key = (widest_text, round(available, 2), base_pixel, typography_min_pixel_size(TextRole.ORDERBOOK_PRICE), round(float(self.devicePixelRatioF()), 3))
        fitted_pixel = self._price_fit_cache.get(cache_key)
        if fitted_pixel is None:
            fitted_pixel = base_pixel
            for candidate_pixel in range(base_pixel, typography_min_pixel_size(TextRole.ORDERBOOK_PRICE) - 1, -1):
                candidate = typography_font_at_pixel_size(font, candidate_pixel)
                if self._price_metrics_for_font(candidate).horizontalAdvance(widest_text) <= available:
                    fitted_pixel = candidate_pixel
                    break
                fitted_pixel = typography_min_pixel_size(TextRole.ORDERBOOK_PRICE)
            if len(self._price_fit_cache) >= 128:
                self._price_fit_cache.popitem(last=False)
            self._price_fit_cache[cache_key] = float(fitted_pixel)
        else:
            self._price_fit_cache.move_to_end(cache_key)
        font = typography_font_at_pixel_size(font, int(round(float(fitted_pixel))))
        metrics = self._price_metrics_for_font(font)
        width = metrics.horizontalAdvance(widest_text)
        self._effective_price_font = font
        self._effective_price_metrics = metrics
        self._effective_price_font_key = font.toString()
        self._price_text_overflow = width > available + 0.5

    def _physical_pixel_width(self) -> float:
        return 1.0 / max(1.0, float(self.devicePixelRatioF()))

    def _snap(self, value: float) -> float:
        return device_pixel_value(self, float(value))

    def _draw_snapped_line(self, painter: QtGui.QPainter, x1: float, y1: float, x2: float, y2: float) -> None:
        painter.drawLine(QtCore.QPointF(self._snap(x1), self._snap(y1)), QtCore.QPointF(self._snap(x2), self._snap(y2)))

    def _row_market_anchor_state(self, level: OrderFlowDisplayLevel) -> tuple[bool, float, bool, float]:
        """Resolve BBO/LTP row membership once per published snapshot."""
        snapshot = self._market_anchor_snapshot or self.snapshot
        is_native_touch = False
        native_fraction = 0.5
        is_ltp_row = False
        ltp_fraction = 0.5

        def bucket_fraction(price: float) -> float:
            if self.aggregation_multiplier <= 1 or self.price_tick_size <= 0.0:
                return 0.5
            tick_span = max(1, self.aggregation_multiplier - 1) * self.price_tick_size
            if level.side == 'bid':
                low, high = (level.price, level.price + tick_span)
            else:
                low, high = (level.price - tick_span, level.price)
            return max(0.0, min(1.0, (price - low) / max(self.price_tick_size, high - low)))
        if snapshot is not None and snapshot.ready:
            native = snapshot.best_bid if level.side == 'bid' else snapshot.best_ask
            if native > 0.0 and math.isfinite(native):
                bucket = self._display_bucket_price(native, level.side)
                if self._marker_key(bucket) == self._marker_key(level.price):
                    is_native_touch = True
                    native_fraction = bucket_fraction(native)
        if snapshot is not None and snapshot.recent_prints:
            last_print = snapshot.recent_prints[-1]
            ltp = float(last_print.price)
            ltp_side = 'ask' if last_print.aggressor_side == 'buy' else 'bid'
            if ltp > 0.0 and math.isfinite(ltp) and (level.side == ltp_side):
                bucket = self._display_bucket_price(ltp, ltp_side)
                if self._marker_key(bucket) == self._marker_key(level.price):
                    is_ltp_row = True
                    ltp_fraction = bucket_fraction(ltp)
        return (is_native_touch, native_fraction, is_ltp_row, ltp_fraction)

    def _refresh_row_market_anchors(self) -> None:
        """Refresh BBO/LTP latches when analytical row tuples are reused."""
        rows = int(self._geometry.get('rows_per_side', 0))
        for row in (*self._bid_rows[:rows], *self._ask_rows[:rows]):
            level = row.level
            if not isinstance(level, OrderFlowDisplayLevel):
                continue
            row.is_native_touch, row.native_touch_fraction, row.is_ltp_row, row.ltp_fraction = self._row_market_anchor_state(level)

    def _prepare_row(
        self,
        level: OrderFlowDisplayLevel,
        *,
        now: float,
        allow_state_transition: bool,
        previous_metrics: dict[tuple[str, int | float], tuple[float, float]] | None=None,
        inference_allowed: bool | None=None,
    ) -> PreparedDomRow:
        delta = level.delta_notional_5s
        price_left, price_right = self._geometry['columns']['price']
        price_text = self._fitted_price_text(level.price, self._effective_price_metrics, float(price_right) - float(price_left))
        display_state = self._display_state(
            level, now, allow_transition=allow_state_transition,
            inference_allowed=inference_allowed,
        )
        if delta > 0.0:
            # More bid liquidity is bid-supportive; more ask liquidity is ask-side.
            delta_color = self._bid if level.side == 'bid' else self._ask
        elif delta < 0.0:
            # Removal reverses the directional meaning of the resting side.
            delta_color = self._ask if level.side == 'bid' else self._bid
        else:
            delta_color = self._muted
        marker_key = (level.side, self._marker_key(level.price))
        previous = (previous_metrics or {}).get(marker_key)
        if previous is not None:
            previous_notional, previous_trade = previous
            size_change = level.notional - previous_notional
            trade_change = max(0.0, level.trade_notional_5s - previous_trade)
            size_threshold = max(1.0, self._visual_scales['liquidity'] * 0.06, abs(previous_notional) * 0.12)
            trade_threshold = max(1.0, self._visual_scales['trade'] * 0.06)
            cue = ''
            if trade_change >= trade_threshold:
                cue = 'trade'
            elif size_change >= size_threshold:
                cue = 'add'
            elif size_change <= -size_threshold:
                cue = 'pull'
            if cue:
                expires = now + 0.75
                if marker_key not in self._row_change_cues and len(self._row_change_cues) >= 6:
                    oldest_key = min(self._row_change_cues, key=lambda key: float(self._row_change_cues[key][2]))
                    self._row_change_cues.pop(oldest_key, None)
                self._row_change_cues[marker_key] = (cue, now, expires)
                self._schedule_cue_expiry_repaint(expires)
        cue_entry = self._row_change_cues.get(marker_key)
        if cue_entry is not None and cue_entry[2] <= now:
            self._row_change_cues.pop(marker_key, None)
            cue_entry = None
        cue_color = self._cue_colors.get(str(cue_entry[0]), self._mid) if cue_entry is not None else None
        event_kind = str(getattr(level, 'semantic_event_kind', '') or '')
        event_text = str(getattr(level, 'semantic_event_label', '') or '')
        if event_kind == 'reload' and level.trade_reload_count > 1:
            event_text = f'{event_text} ×{level.trade_reload_count}'
        elif event_kind == 'restack' and level.restack_count > 1:
            event_text = f'{event_text} ×{level.restack_count}'
        market_signal_text = ''
        market_signal_color = self._muted
        signal = self._market_signal
        if isinstance(signal, dict):
            if float(signal.get('expires', 0.0)) <= now:
                self._market_signal = None
            else:
                anchor_side = str(signal.get('anchor_side', ''))
                anchor_price = float(signal.get('anchor_price', 0.0) or 0.0)
                display_anchor = self._display_bucket_price(anchor_price, anchor_side)
                if level.side == anchor_side and self._marker_key(level.price) == self._marker_key(display_anchor):
                    key = str(signal.get('key', ''))
                    score = int(signal.get('score', 0) or 0)
                    labels = {'BID ABSORPTION': 'Abs bid', 'ASK ABSORPTION': 'Abs ask', 'BULLISH BREAKOUT': 'Breakout', 'BEARISH BREAKDOWN': 'Breakdown'}
                    market_signal_text = labels.get(key, key[:12])
                    if score > 0:
                        market_signal_text += f' {score}'
                    market_signal_color = self._bid if key in {'BID ABSORPTION', 'BULLISH BREAKOUT'} else self._ask
        threshold = max(1.0, float(self.snapshot.large_trade_threshold)) if self.snapshot is not None else 1.0
        is_native_touch, native_touch_fraction, is_ltp_row, ltp_fraction = self._row_market_anchor_state(level)
        return PreparedDomRow(
            level=level,
            display_state=display_state,
            liquidity_visual=self._visual_intensity(level.notional, 'liquidity'),
            price_text=price_text,
            notional_text=self._row_amount(level.notional, level.price, quantity=level.quantity),
            event_text=event_text,
            event_kind=event_kind,
            sell_large=level.largest_sell_trade_5s >= threshold,
            buy_large=level.largest_buy_trade_5s >= threshold,
            market_signal_text=market_signal_text,
            market_signal_color=market_signal_color,
            market_signal_fill=(
                self._bid_signal_fill
                if market_signal_text and market_signal_color == self._bid
                else self._ask_signal_fill if market_signal_text else None
            ),
            delta_color=delta_color,
            change_cue=cue_entry,
            change_cue_color=cue_color,
            is_native_touch=is_native_touch,
            native_touch_fraction=native_touch_fraction,
            is_ltp_row=is_ltp_row,
            ltp_fraction=ltp_fraction,
        )


    @staticmethod
    def _event_salience(row: PreparedDomRow, visual_index: int) -> tuple[int, float, int]:
        """Rank sparse row events without turning the ladder into classifier spam."""
        kind = str(row.event_kind)
        priority = {'reload': 6, 'rejected_sell': 5, 'rejected_buy': 5, 'rejected_both': 5, 'restack': 4, 'depleting': 3, 'pulling': 2, 'stacking': 2}.get(kind, 1)
        level = row.level
        magnitude = 0.0
        if isinstance(level, OrderFlowDisplayLevel):
            if kind == 'reload':
                magnitude = float(level.recent_replenished_notional)
            elif kind == 'restack':
                magnitude = float(level.recent_restacked_notional)
            elif kind.startswith('rejected'):
                magnitude = float(level.trade_notional_5s)
            else:
                magnitude = abs(float(level.delta_notional_5s))
        return (priority, magnitude, -int(visual_index))

    def _sparsify_prepared_events(self) -> None:
        """Keep only the most salient row events on each side of the ladder.

        Professional DOMs surface changes at price; they do not repeat the same
        classifier label down every visible row.  Analytics remain intact in the
        prepared level/tooltips, while the paint contract promotes only a small
        number of events that deserve visual attention.
        """
        limit = max(1, int(self.MAX_VISIBLE_EVENTS_PER_SIDE))
        for side, rows in (('ask', self._ask_rows), ('bid', self._bid_rows)):
            candidates: list[tuple[tuple[int, float, int], int, tuple[str, int | float]]] = []
            signature_items: list[tuple[object, ...]] = []
            for index, row in enumerate(rows):
                event_text = str(row.event_text)
                if not event_text:
                    continue
                level = row.level
                row_key = (
                    str(level.side),
                    self._marker_key(level.price),
                ) if isinstance(level, OrderFlowDisplayLevel) else (side, index)
                score = self._event_salience(row, index)
                candidates.append((score, index, row_key))
                signature_items.append((row_key, event_text, str(row.event_kind), score))

            signature = tuple(signature_items)
            if signature == self._event_sparsify_signatures.get(side, ()):
                keep_keys = self._event_sparsify_keep_keys.get(side, frozenset())
            else:
                if len(candidates) <= limit:
                    keep_keys = frozenset(row_key for _score, _index, row_key in candidates)
                else:
                    keep_keys = frozenset(
                        row_key
                        for _score, _index, row_key in sorted(candidates, reverse=True)[:limit]
                    )
                self._event_sparsify_signatures[side] = signature
                self._event_sparsify_keep_keys[side] = keep_keys

            if len(candidates) <= limit:
                continue
            for _score, index, row_key in candidates:
                if row_key in keep_keys:
                    continue
                row = rows[index]
                row.event_text = ''
                row.event_kind = ''

    def _prepare_header_paint_content(self, width: int) -> None:
        margin = float(self._geometry.get('margin', 0.0))
        bounds_width = max(1.0, float(width) - margin * 2.0)
        self._prepared_header_layout = {
            'bounds_width': bounds_width,
            'symbol_width': bounds_width * 0.34,
            'status_width': bounds_width * 0.32,
            'spread_width': bounds_width * 0.34,
            'spread_left': bounds_width * 0.66,
            'status_left': bounds_width * 0.34,
            'show_source': False,
            'spread_text': f"SPREAD {self._header_text.get('spread', ('--', self._muted))[0]}",
        }
        if self._book_depth:
            height = float(self._geometry['profile_market_height'])
            spread = self._header_text.get('spread', ('', self._muted))[0]
            resync = self._book_validity_known and not self._book_valid
            color = self._header_text.get('status', ('', self._muted))[1]
            key = (width, height, self.symbol, self._market_status, resync, spread,
                   self._label_font_key, self._text.rgba(), self._muted.rgba(), color.rgba())
            if key != self._profile_header_key:
                self._profile_header_key = key
                status = 'Resync' if resync else {'LIVE': 'Live', 'DEGRADED': 'Delayed', 'STALE': 'Stale',
                          'LAST KNOWN': 'Resync', 'SYNCING': 'Syncing', 'CONNECTING': 'Connecting'}.get(self._market_status, '')
                status_width = min(max(0.0, width - 8.0), self._label_metrics.horizontalAdvance(status) + 4.0)
                status_left = width - 4.0 - status_width
                pair = self.symbol.removesuffix('USDT') + ' / USDT' if self.symbol.endswith('USDT') else self.symbol
                pair = self._label_metrics.elidedText(pair, Qt.TextElideMode.ElideRight, max(0, int(status_left - 16.0)))
                pair_width = min(max(0.0, status_left - 12.0), self._label_metrics.horizontalAdvance(pair) + 4.0)
                items = [(QtCore.QRectF(4.0, 0.0, pair_width, height), pair, self._text),
                         (QtCore.QRectF(status_left, 0.0, status_width, height), status, color)]
                spread_text = f'Spread {spread}'
                spread_left = pair_width + 16.0
                spread_width = status_left - spread_left - 8.0
                if (self._market_status == 'LIVE' and not resync and spread and spread != '--'
                        and self._label_metrics.horizontalAdvance(spread_text) <= spread_width):
                    items.append((QtCore.QRectF(spread_left, 0.0, spread_width, height), spread_text, self._muted))
                self._profile_header_items = tuple(items)

    def _prepare_column_header_content(self):
        unit = 'USDT' if self._value_mode == 'quote' else 'Qty'
        columns = self._geometry.get('columns', {})
        def label(name, full, short):
            bounds = columns.get(name, (0.0, 0.0))
            available = float(bounds[1]) - float(bounds[0]) - 14.0
            return full if self._label_metrics.horizontalAdvance(full) <= available else short
        self._prepared_column_labels = {
            'state': label('state', 'Signal', 'Sig'), 'memory': 'Liq · 30s', 'delta': 'Δ book',
            'bid': label('bid', f'Bid · {unit}', 'Bid'), 'price': 'Price',
            'ask': label('ask', f'Ask · {unit}', 'Ask'), 'flow': 'Flow · 5s',
        }

    def _prepare_metric_footer_paint_content(self, width):
        count = 4 if width >= 420 else 3 if width >= 300 else 2
        self._prepared_metric_draw_items = tuple((str(label), str(value), color) for label, value, color in self._metric_items[:count])
        footer = self._footer_items[:3 if width >= 350 else 2]
        names = ('Added', 'Canceled', 'Reloaded')
        self._prepared_footer_draw_items = tuple((f'{names[index]} {value}', color) for index, (_label, value, color) in enumerate(footer))
        snapshot = self.snapshot
        if snapshot is None or not snapshot.ready:
            self._footer_depth_summary = (0.5, '—', '—', False)
            return
        rows = int(self._geometry.get('rows_per_side', 0))
        attribute = 'quantity' if self._value_mode == 'base' else 'notional'
        bid = sum(max(0.0, float(getattr(level, attribute))) for level in snapshot.bid_levels[:rows])
        ask = sum(max(0.0, float(getattr(level, attribute))) for level in snapshot.ask_levels[:rows])
        total = bid + ask
        share = bid / total if total > 0.0 else 0.5
        formatter = self._compact_scalar if self._value_mode == 'base' else self._money
        self._footer_depth_summary = (round(share, 5), formatter(bid), formatter(ask), total > 0.0)

    def _prepare_paint_content(self, width: int) -> None:
        self._prepare_header_paint_content(width)
        self._prepare_column_header_content()
        self._prepare_metric_footer_paint_content(width)

    def _refresh_geometry_for_size(self, width: int, height: int, snapshot: OrderFlowSnapshot | None) -> bool:
        """Refresh only layout/font-fit state; safe to call synchronously on resize."""
        required_price_width = self._required_price_lane_width(snapshot)
        required_amount_width = self._required_amount_lane_width(snapshot)
        required_state_width = self._required_state_lane_width()
        width_overrides = self._column_width_overrides.get(self._presentation_preset, {})
        override_signature = tuple(
            sorted(
                (str(key), round(float(value), 2))
                for key, value in width_overrides.items()
                if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0.0
            )
        )
        geometry_key = (
            width, height,
            round(max(8.0, self._row_metrics.height()), 3),
            round(max(8.0, self._price_metrics.height()), 3),
            round(max(8.0, self._label_metrics.height()), 3),
            tuple(sorted((str(k), str(v)) for k, v in self._column_preferences.items())),
            self._row_density, self._presentation_preset, self._book_depth, override_signature,
            round(required_price_width, 2), round(required_amount_width, 2),
            round(required_state_width, 2),
        )
        geometry_changed = geometry_key != self._geometry_cache_key
        if geometry_changed:
            self._geometry = _compute_order_flow_dom_geometry(
                width, height,
                max(8.0, self._row_metrics.height()),
                max(8.0, self._price_metrics.height()),
                max(8.0, self._label_metrics.height()),
                column_preferences=self._column_preferences,
                row_density=self._row_density,
                presentation_preset=self._presentation_preset,
                previous_mode=str(self._geometry.get('mode', '')) or None,
                previous_bbo_only=bool(self._geometry.get('bbo_only', False)),
                price_min_width=required_price_width,
                amount_min_width=required_amount_width,
                state_expanded_width=required_state_width,
                column_width_overrides=width_overrides,
                book_depth=self._book_depth,
            )
            self._geometry_cache_key = geometry_key
            self._geometry_rebuild_count += 1
            self._publish_layout_state()
        self._fit_effective_price_font(snapshot)
        reapply_price_protection = self._price_text_overflow and len(self._geometry.get('columns', {})) > 3
        if geometry_changed and self._price_protection_active and not reapply_price_protection:
            self._price_protection_active = False
            self._publish_layout_state()
        if reapply_price_protection:
            fallback_preferences = dict(self._column_preferences)
            for name in self.PRIMARY_ANALYTICS:
                fallback_preferences[name] = False
            self._geometry = _compute_order_flow_dom_geometry(
                width, height,
                max(8.0, self._row_metrics.height()),
                max(8.0, self._price_metrics.height()),
                max(8.0, self._label_metrics.height()),
                column_preferences=fallback_preferences,
                row_density=self._row_density,
                presentation_preset=self._presentation_preset,
                essential_only=True,
                previous_mode=str(self._geometry.get('mode', '')) or None,
                previous_bbo_only=bool(self._geometry.get('bbo_only', False)),
                price_min_width=required_price_width,
                amount_min_width=required_amount_width,
                state_expanded_width=required_state_width,
                column_width_overrides=width_overrides,
                book_depth=self._book_depth,
            )
            self._geometry['primary_analytic'] = str(self._column_preferences.get('primary', 'flow'))
            self._geometry['column_preferences'] = dict(self._column_preferences)
            self._geometry['price_protected'] = True
            self._price_protection_active = True
            self._geometry_rebuild_count += 1
            self._fit_effective_price_font(snapshot)
            self._publish_layout_state()
        return geometry_changed

    def _prepare_display(self, *, reuse_rows: bool=False) -> None:
        started = time.perf_counter()
        if not hasattr(self, '_row_metrics'):
            return
        width = max(1, self.width())
        height = max(1, self.height())
        snapshot = self.snapshot
        previous_rows_per_side = int(self._geometry.get('rows_per_side', 0))
        geometry_changed = self._refresh_geometry_for_size(width, height, snapshot)
        if geometry_changed and int(self._geometry.get('rows_per_side', 0)) != previous_rows_per_side:
            reuse_rows = False
        now = time.monotonic()
        scale_changed = False
        if snapshot is not None:
            scale_changed = self._update_visual_scales(snapshot)
        if snapshot is None:
            status_text, health_reason = self._feed_health_status(None)
            self._market_status = status_text
            self._bid_rows = []
            self._ask_rows = []
            status_color = self._ask if status_text == 'STALE' else self._amber
            source_text = health_reason or 'WAITING FOR MARKET DATA'
            self._header_text = {'symbol': (self.symbol, self._text), 'status': (status_text, status_color), 'latency': ('BOOK -- · DOM --', self._muted), 'latency_compact': ('B-- D--', self._muted), 'source': (source_text, self._muted), 'spread': ('--', self._muted)}
            self._metric_items = []
            self._footer_items = []
            self._freshness_text = source_text
            self._prepare_paint_content(width)
            self._prepared_size = (width, height)
            self._rebuild_hit_rows()
            self._prepared_chrome_signature = self._snapshot_chrome_signature()
            elapsed = max(0.0, (time.perf_counter() - started) * 1000.0)
            self.last_prepare_ms = elapsed
            self.max_prepare_ms = max(self.max_prepare_ms, elapsed)
            return
        status_text, health_reason = self._feed_health_status(snapshot)
        state_inference_allowed = not (
            self.aggregation_multiplier > 1
            or status_text in {'STALE', 'SYNCING', 'CONNECTING'}
            or (self._trade_stream_known and not self._trade_stream_active)
        )
        rows = int(self._geometry['rows_per_side'])
        visible_levels = tuple(snapshot.bid_levels[:rows]) + tuple(snapshot.ask_levels[:rows])
        active_state_keys = self._advance_state_latches(
            visible_levels, now, inference_allowed=state_inference_allowed
        )
        self._prune_state_latches(now, active_state_keys)
        if not reuse_rows:
            previous_metrics: dict[tuple[str, int | float], tuple[float, float]] = {}
            if not self._suppress_change_cues_once:
                for prepared in (*self._bid_rows, *self._ask_rows):
                    previous_level = prepared.level
                    if isinstance(previous_level, OrderFlowDisplayLevel):
                        previous_metrics[previous_level.side, self._marker_key(previous_level.price)] = (previous_level.notional, previous_level.trade_notional_5s)
            self._bid_rows = [
                self._prepare_row(
                    level, now=now, allow_state_transition=False,
                    previous_metrics=previous_metrics,
                    inference_allowed=state_inference_allowed,
                )
                for level in snapshot.bid_levels[:rows]
            ]
            self._ask_rows = [
                self._prepare_row(
                    level, now=now, allow_state_transition=False,
                    previous_metrics=previous_metrics,
                    inference_allowed=state_inference_allowed,
                )
                for level in snapshot.ask_levels[:rows]
            ]
            self._sparsify_prepared_events()
            self._suppress_change_cues_once = False
        elif scale_changed:
            self._refresh_row_visual_scales()
        if reuse_rows:
            self._refresh_prepared_row_levels(
                inference_allowed=state_inference_allowed
            )
            self._refresh_row_market_anchors()
        source = str(snapshot.bbo_source or 'LOCAL BOOK').replace('_', ' ').upper()
        elapsed = max(0.0, now - snapshot.generated_monotonic)
        freshness_parts = tuple(
            self._age_text(label, None if age is None else age + elapsed)
            for label, age in (('DEPTH', snapshot.depth_age_seconds), ('QUOTE', snapshot.bbo_age_seconds), ('TRADES', snapshot.trade_age_seconds))
        )
        self._freshness_text = f'{source}  •  ' + '  •  '.join(freshness_parts)
        if self._book_validity_known and not self._book_valid and snapshot.ready:
            age_ms = self._book_age_ms(snapshot)
            age_text = self._latency_ms_text(age_ms) if age_ms is not None else '--'
            status_text = 'LAST KNOWN'
            reason = self._book_valid_reason or 'RESYNC'
            health_reason = f'RESYNC · {reason} · AGE {age_text}'
        self._market_status = status_text
        status_color = self._bid if status_text == 'LIVE' else self._ask if status_text in {'STALE', 'LAST KNOWN'} else self._amber
        if status_text == 'LIVE':
            source_text = ''
        elif health_reason:
            source_text = health_reason
            if status_text == 'DEGRADED' and health_reason not in self._freshness_text:
                source_text = f'{health_reason}  •  {self._freshness_text}'
        else:
            source_text = self._freshness_text
        price_left, price_right = self._geometry['columns']['price']
        # Keep the last-trade latch for the thin LTP row marker, but the
        # reference BBO band centers the quoted midpoint, not the latest print.
        self._update_last_trade_anchor(snapshot)
        center_value = snapshot.midpoint or snapshot.microprice
        # A midpoint can be half an exchange tick. Preserve that extra decimal
        # instead of rounding the midpoint to a tradable bid/ask precision.
        center_text = format_book_price(center_value, min(16, self.price_decimals + 1) if self.price_tick_size > 0 else None) if snapshot.ready else '—'
        book_age_ms, book_max_ms, dom_latency_ms, dom_p95_ms, dom_max_ms = self._latency_display_values(snapshot)
        book_age_text = self._latency_ms_text(book_age_ms)
        book_max_text = self._latency_ms_text(book_max_ms)
        dom_latency_text = self._latency_ms_text(dom_latency_ms)
        dom_max_text = self._latency_ms_text(dom_max_ms)
        pipe_text = self._latency_ms_text(self.last_end_to_end_ms) if self._has_end_to_end_sample else '--'
        now_for_latency = time.monotonic()
        cutoff_for_latency = now_for_latency - self.LATENCY_MAX_DISPLAY_WINDOW_SECONDS
        while self._latency_pipe_display_window and self._latency_pipe_display_window[0][0] < cutoff_for_latency:
            self._latency_pipe_display_window.popleft()
        pipe_max_10s_ms = max(
            (value for _stamp, value in self._latency_pipe_display_window),
            default=(self.last_end_to_end_ms if self._has_end_to_end_sample else None),
        )
        pipe_max_text = self._latency_ms_text(pipe_max_10s_ms)
        latency_text = (
            f'BOOK {book_age_text}  MAX10 {book_max_text} · '
            f'DOM {dom_latency_text}  MAX10 {dom_max_text} · '
            f'PIPE {pipe_text}  MAX10 {pipe_max_text}'
        )
        # Keep the always-visible header latency compact. Units and expanded
        # labels remain available from the existing header hover tooltip.
        book_age_compact = book_age_text.removesuffix('ms')
        book_max_compact = book_max_text.removesuffix('ms')
        dom_latency_compact = dom_latency_text.removesuffix('ms')
        dom_max_compact = dom_max_text.removesuffix('ms')
        pipe_compact = pipe_text.removesuffix('ms')
        pipe_max_compact = pipe_max_text.removesuffix('ms')
        latency_compact = (
            f'B {book_age_compact}/{book_max_compact} · '
            f'D {dom_latency_compact}/{dom_max_compact} · '
            f'P {pipe_compact}/{pipe_max_compact}'
        )
        latency_color = self._muted
        if book_age_ms is not None:
            if book_age_ms >= self.DEPTH_DEGRADED_SECONDS * 1000.0:
                latency_color = self._ask
            elif book_age_ms >= 350.0:
                latency_color = self._amber
        self._header_text = {'symbol': (snapshot.symbol or self.symbol, self._text), 'status': (status_text, status_color), 'latency': (latency_text, latency_color), 'latency_compact': (latency_compact, latency_color), 'source': (source_text, self._muted), 'spread': (f'{snapshot.spread_bps:.2f} bp' if snapshot.ready else '--', self._muted), 'mid': (center_text, self._mid), 'mid_label': ('MID', self._muted), 'best_bid': (self._price_text(snapshot.best_bid) if snapshot.ready else '--', self._bid), 'best_ask': (self._price_text(snapshot.best_ask) if snapshot.ready else '--', self._ask)}
        self._metric_items = [
            ('Microprice', f'{snapshot.microprice_bias_bps:+.2f} bp' if snapshot.ready else '--', self._metric_color(snapshot.microprice_bias_bps, deadband=0.02)),
            ('Pressure', self._signed_percent(snapshot.near_pressure_pct), self._metric_color(snapshot.near_pressure_pct)),
            ('Flow · 5s', self._signed_percent(snapshot.aggressor_imbalance_5s_pct), self._metric_color(snapshot.aggressor_imbalance_5s_pct)),
            ('RPI · 5s', f'{snapshot.rpi_share_5s_pct:.0f}%' if snapshot.ready else '--', self._purple),
        ]
        self._footer_items = [
            ('ADD', self._money(snapshot.added_notional_5s), self._text),
            ('CANCEL', self._money(snapshot.cancelled_notional_5s), self._amber if snapshot.cancelled_notional_5s > 0.0 else self._muted),
            ('REPL', self._money(snapshot.replenished_notional_5s), self._text),
            ('1s B/S', f'B {self._money(snapshot.buy_notional_1s)} / S {self._money(snapshot.sell_notional_1s)}', self._bid),
        ]
        self._prepare_paint_content(width)
        self._prepared_sequence = snapshot.sequence
        self._prepared_size = (width, height)
        self._rebuild_hit_rows()
        self._prepared_chrome_signature = self._snapshot_chrome_signature()
        completed = time.perf_counter()
        elapsed = max(0.0, (completed - started) * 1000.0)
        self.last_prepare_ms = elapsed
        self.max_prepare_ms = max(self.max_prepare_ms, elapsed)
        sequence = snapshot.sequence
        if sequence in self._snapshot_prepare_started_at and sequence not in self._snapshot_prepare_completed_at:
            self._snapshot_prepare_completed_at[sequence] = completed
            timing = self._snapshot_pipeline_timing.get(sequence)
            if timing is not None:
                timing['dom_prepare_completed_mono'] = completed

    def _refresh_prepared_row_levels(self, *, inference_allowed: bool | None=None) -> None:
        """Refresh immutable level references without rebuilding row visuals.

        ``OrderFlowDisplayLevel.age_seconds`` is deliberately excluded from
        value equality because it does not affect painted pixels.  When the
        visible rows are otherwise equal, keep the prepared visual state but
        attach the newest level objects so hover/tooltips still expose current
        analytical metadata such as age.
        """
        snapshot = self.snapshot
        if snapshot is None:
            return
        rows = max(0, int(self._geometry.get('rows_per_side', 0)))
        now = time.monotonic()
        threshold = max(1.0, float(snapshot.large_trade_threshold or 1.0))
        for prepared, level in zip(self._bid_rows[:rows], snapshot.bid_levels[:rows]):
            prepared.level = level
            prepared.sell_large = level.largest_sell_trade_5s >= threshold
            prepared.buy_large = level.largest_buy_trade_5s >= threshold
            display_state = self._display_state(
                level, now, allow_transition=False,
                inference_allowed=inference_allowed,
            )
            prepared.display_state = display_state
        for prepared, level in zip(self._ask_rows[:rows], snapshot.ask_levels[:rows]):
            prepared.level = level
            prepared.sell_large = level.largest_sell_trade_5s >= threshold
            prepared.buy_large = level.largest_buy_trade_5s >= threshold
            display_state = self._display_state(
                level, now, allow_transition=False,
                inference_allowed=inference_allowed,
            )
            prepared.display_state = display_state

    def _refresh_row_visual_scales(self) -> None:
        """Refresh only scale-dependent visuals that the active DOM paints."""
        rows = int(self._geometry.get('rows_per_side', 0))
        for row in (*self._bid_rows[:rows], *self._ask_rows[:rows]):
            level = row.level
            if not isinstance(level, OrderFlowDisplayLevel):
                continue
            row.liquidity_visual = self._visual_intensity(level.notional, 'liquidity')


    def _profile_animation_interval_ms(self) -> int:
        return display_frame_interval_ms(self)

    def _sample_profile_animation(self, now: float) -> bool:
        elapsed = now - self._profile_animation_last_frame
        moving_size = _smooth_profile_value_map(
            self._profile_size_current,
            self._profile_size_targets,
            elapsed,
        )
        moving_depth = _smooth_profile_value_map(
            self._profile_depth_current,
            self._profile_depth_targets,
            elapsed,
        )
        self._profile_animation_last_frame = now
        return moving_size or moving_depth

    def _reset_profile_animation(self) -> None:
        if hasattr(self, '_profile_animation_timer'):
            self._profile_animation_timer.stop()
        self._profile_size_targets.clear()
        self._profile_size_current.clear()
        self._profile_depth_targets.clear()
        self._profile_depth_current.clear()
        self._profile_animation_last_frame = time.monotonic()

    def _set_profile_animation_targets(
        self,
        size_targets: dict[tuple[str, float], float],
        depth_targets: dict[tuple[str, float], float],
    ) -> None:
        now = time.monotonic()
        self._sample_profile_animation(now)

        previous_size_targets = self._profile_size_targets
        previous_depth_targets = self._profile_depth_targets
        self._profile_size_current = {
            key: (
                self._profile_size_current.get(key, previous_size_targets[key])
                if key in previous_size_targets
                else target
            )
            for key, target in size_targets.items()
        }
        self._profile_depth_current = {
            key: (
                self._profile_depth_current.get(key, previous_depth_targets[key])
                if key in previous_depth_targets
                else target
            )
            for key, target in depth_targets.items()
        }
        self._profile_size_targets = dict(size_targets)
        self._profile_depth_targets = dict(depth_targets)
        self._profile_animation_last_frame = now

        if not self._book_depth or not self.isVisible():
            self._profile_size_current = dict(self._profile_size_targets)
            self._profile_depth_current = dict(self._profile_depth_targets)
            self._profile_animation_timer.stop()
        else:
            interval = self._profile_animation_interval_ms()
            if self._profile_animation_timer.interval() != interval:
                self._profile_animation_timer.setInterval(interval)
            if (
                _profile_values_need_animation(self._profile_size_current, self._profile_size_targets)
                or _profile_values_need_animation(self._profile_depth_current, self._profile_depth_targets)
            ):
                if not self._profile_animation_timer.isActive():
                    self._profile_animation_timer.start()
            else:
                self._profile_animation_timer.stop()

        self._rebuild_profile_paths_from_current()

    def _profile_ladder_rect(self) -> QtCore.QRect:
        top = max(0, int(math.floor(float(self._geometry.get('table_top', 0.0)))) - 1)
        bottom = min(
            self.height(),
            int(math.ceil(float(self._geometry.get('footer_top', self.height())))) + 1,
        )
        return QtCore.QRect(0, top, self.width(), max(1, bottom - top))

    def _advance_profile_animation(self) -> None:
        if not self._book_depth or not self.isVisible():
            self._profile_size_current = dict(self._profile_size_targets)
            self._profile_depth_current = dict(self._profile_depth_targets)
            self._profile_animation_timer.stop()
            self._rebuild_profile_paths_from_current()
            return

        moving = self._sample_profile_animation(time.monotonic())
        if not moving:
            self._profile_size_current = dict(self._profile_size_targets)
            self._profile_depth_current = dict(self._profile_depth_targets)
            self._profile_animation_timer.stop()
        self._rebuild_profile_paths_from_current()
        rect = self._profile_ladder_rect()
        if not rect.isNull():
            self.update(rect)

    def _rebuild_profile_paths_from_current(self) -> None:
        self._profile_paths = {}
        g = self._geometry
        lane = g.get('columns', {}).get('liquidity')
        if lane is None:
            return
        limit = int(g.get('rows_per_side', 0))
        if self.snapshot is None or not self.snapshot.ready:
            limit = 0
        left, right = float(lane[0]), float(lane[1]) - 3.0
        plot_width = max(0.0, right - left)
        sides = {'bid': self._bid_rows[:limit], 'ask': self._ask_rows[:limit]}
        for side, rows in sides.items():
            if not rows:
                continue
            row_height = float(g['row_height'])
            y = self._profile_row_top(rows[0]) + (row_height if side == 'ask' else 0.0)
            outline = QtGui.QPainterPath(QtCore.QPointF(left, y))
            previous_x = left
            for row in rows:
                top = self._profile_row_top(row)
                entry = top + row_height if side == 'ask' else top
                outline.lineTo(previous_x, entry)
                key = (side, float(row.level.price))
                depth = self._profile_depth_current.get(key, row.profile_depth)
                x = left + plot_width * max(0.0, min(1.0, depth))
                outline.lineTo(x, entry)
                y = top if side == 'ask' else top + row_height
                outline.lineTo(x, y)
                previous_x = x
            area = QtGui.QPainterPath(outline)
            area.lineTo(left, y)
            area.closeSubpath()
            self._profile_paths[side] = (area, outline)

    def _profile_row_top(self, row: PreparedDomRow) -> float:
        return self._profile_price_row_top(float(row.level.price), str(row.level.side))

    def _profile_price_row_top(self, price: float, side: str) -> float:
        g = self._geometry
        step = float(g.get('profile_step', 0.0))
        if step <= 0.0:
            return float(g['center_top'])
        row_height = float(g['row_height'])
        price_anchor = float(g.get('profile_anchor', self._profile_anchor_price))
        if side == 'ask':
            anchor = float(g.get('profile_ask_anchor', price_anchor + step))
            return float(g['center_top']) - row_height * (1.0 + max(0, round((price - anchor) / step)))
        return float(g['center_bottom']) + max(0, round((price_anchor - price) / step)) * row_height

    def _profile_ruler_limits(self) -> tuple[float, float, float]:
        g = self._geometry
        origin = (float(g['center_top']) + float(g['center_bottom'])) * 0.5
        visible = max(0.0, min(origin - float(g.get('table_top', 0.0)), float(g['height']) - origin))
        minimum = min(visible, float(g.get('profile_ruler_min', (float(g['center_height']) + float(g['row_height'])) * 0.5)))
        available = max(minimum, min(visible, float(g.get('profile_ruler_extent', visible))))
        return origin, minimum, available

    def _profile_rulers(self) -> tuple[float, float]:
        origin, minimum, available = self._profile_ruler_limits()
        distance = minimum + (available - minimum) * self._profile_ruler_fraction
        return origin - distance, origin + distance

    def set_depth_range(self, fraction: float, *, emit: bool=True) -> None:
        try:
            fraction = float(fraction)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(fraction):
            return
        fraction = max(0.05, min(0.95, fraction))
        if abs(fraction - self._profile_ruler_fraction) < 0.001:
            return
        self._profile_ruler_fraction = fraction
        self._prepare_display(reuse_rows=True)
        self.update()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def _prepare_liquidity_profile(self) -> None:
        """Scale this market's heat, order blocks and depth inside its rulers."""
        g = self._geometry
        limit = int(g['rows_per_side'])
        if self.snapshot is None or not self.snapshot.ready:
            limit = 0
        step = self.price_tick_size * self.aggregation_multiplier
        if step <= 0.0 and self.snapshot is not None:
            prices = sorted({row.level.price for row in (*self._bid_rows, *self._ask_rows)})
            step = min((b - a for a, b in zip(prices, prices[1:]) if b > a), default=1.0)
        g['profile_step'] = max(step, 1e-16)
        mid = self.snapshot.midpoint or self.snapshot.best_bid if self.snapshot is not None else 0.0
        anchor_tick = math.floor(mid / g['profile_step'] + 1e-7) if mid > 0.0 else 0
        self._profile_anchor_price = anchor_tick * g['profile_step']
        g['profile_anchor'] = self._profile_anchor_price
        sides = {'bid': self._bid_rows[:limit], 'ask': self._ask_rows[:limit]}
        ask_at_anchor = bool(sides['ask'] and math.isclose(sides['ask'][0].level.price,
                            self._profile_anchor_price, rel_tol=0.0, abs_tol=g['profile_step'] * 1e-5))
        g['profile_ask_anchor'] = self._profile_anchor_price + (0.0 if ask_at_anchor else g['profile_step'])
        origin = (float(g['center_top']) + float(g['center_bottom'])) * 0.5
        nearest = [(float(g['center_height']) + float(g['row_height'])) * 0.5]
        farthest = []
        for rows in sides.values():
            if rows:
                nearest.append(abs(self._profile_row_top(rows[0]) + float(g['row_height']) * 0.5 - origin))
                farthest.append(abs(self._profile_row_top(rows[-1]) + float(g['row_height']) * 0.5 - origin))
        g['profile_ruler_min'] = max(nearest)
        g['profile_ruler_extent'] = max(farthest, default=g['profile_ruler_min']) + float(g['row_height']) * 0.5
        _, minimum, available = self._profile_ruler_limits()
        g['profile_range_adjustable'] = bool(farthest and available - minimum > float(g['row_height']) * 0.5 + 1.0)
        ruler_top, ruler_bottom = self._profile_rulers()
        g['profile_rulers'] = (ruler_top, ruler_bottom)
        amounts: dict[str, list[tuple[float, float]]] = {}
        largest = 0.0
        totals: dict[str, float] = {}
        ruler_totals: dict[str, float] = {}
        for side, rows in sides.items():
            cumulative = 0.0
            selected_total = 0.0
            values = []
            for row in rows:
                level = row.level
                amount = max(0.0, level.quantity if self._value_mode == 'base' else level.notional)
                cumulative += amount
                row.profile_amount = amount
                row.profile_cumulative = cumulative
                midpoint = self._profile_row_top(row) + float(g['row_height']) * 0.5
                row.profile_in_range = ruler_top <= midpoint <= ruler_bottom
                if row.profile_in_range:
                    largest = max(largest, amount)
                    selected_total = cumulative
                values.append((amount, cumulative))
            amounts[side] = values
            totals[side] = cumulative
            ruler_totals[side] = selected_total
        largest_depth = max(ruler_totals.values(), default=0.0)
        self._profile_ruler_totals = (ruler_totals['bid'], ruler_totals['ask'])
        self._profile_totals = (totals['bid'], totals['ask'])
        unit = 'USDT' if self._value_mode == 'quote' else self.symbol.removesuffix('USDT')
        self._profile_footer_text = (
            f"BID {self._compact_scalar(totals['bid'])}",
            f"ASK {self._compact_scalar(totals['ask'])}",
        )
        if self.snapshot is None or not self.snapshot.ready:
            self._profile_footer_text = ('BID —', 'ASK —')
        amount_texts = [row.notional_text for rows in sides.values() for row in rows]
        measured_amount = max((self._row_metrics.horizontalAdvance(text) for text in amount_texts), default=0.0)
        self._profile_amount_width = max(32.0, measured_amount + 8.0)
        self._profile_scale_text = (
            f'Depth ruler range · {unit}\n'
            f'Solid bars: size at price; full width = {self._compact_scalar(largest)} {unit}\n'
            f'Stepped outline: cumulative depth from the touch; full width = {self._compact_scalar(largest_depth)} {unit}\n'
            'Both sides share the same scale for each series.'
        )

        size_targets: dict[tuple[str, float], float] = {}
        depth_targets: dict[tuple[str, float], float] = {}
        for side, rows in sides.items():
            previous = 0.0
            for row, (amount, cumulative) in zip(rows, amounts[side]):
                row.profile_size = min(1.0, amount / largest) if largest > 0.0 else 0.0
                row.profile_depth = min(1.0, cumulative / largest_depth) if largest_depth > 0.0 else 0.0
                row.profile_previous_depth = previous
                previous = row.profile_depth
                key = (side, float(row.level.price))
                size_targets[key] = row.profile_size
                depth_targets[key] = row.profile_depth

        self._set_profile_animation_targets(size_targets, depth_targets)

    def _rebuild_hit_rows(self) -> None:
        if self._book_depth:
            self._prepare_liquidity_profile()
            self._prepared_display_states.clear()
            self._prepared_row_signatures = {}
            self._prepared_row_rects = {}
            self._hit_rows = []
            if self.snapshot is not None and self.snapshot.ready:
                for row in (*self._ask_rows, *self._bid_rows):
                    key = (row.level.side, self._marker_key(row.level.price))
                    rect = QtCore.QRectF(0.0, self._profile_row_top(row), self.width(),
                                        float(self._geometry['row_height']))
                    if rect.intersects(QtCore.QRectF(self.rect())):
                        self._prepared_row_signatures[key] = self._prepared_row_signature(row)
                        self._prepared_row_rects[key] = rect.toAlignedRect().adjusted(-1, -1, 1, 1)
                        self._hit_rows.append((rect, row.level.price, row.level))
                        self._prepared_display_states[key] = str(row.display_state)
            return
        hit_rows = []
        self._prepared_display_states.clear()
        signatures: dict[tuple[str, int | float], tuple[object, ...]] = {}
        rects: dict[tuple[str, int | float], QtCore.QRect] = {}
        hit_geometry_active = bool(
            self.snapshot is not None
            and self.snapshot.ready
            and not bool(self._geometry.get('bbo_only'))
        )
        rows = int(self._geometry['rows_per_side'])
        if hit_geometry_active:
            row_height = float(self._geometry['row_height'])
            center_top = float(self._geometry['center_top'])
            center_bottom = float(self._geometry['center_bottom'])
            margin = float(self._geometry['margin'])
            width = float(self._geometry['inner_width'])
        else:
            row_height = center_top = center_bottom = margin = width = 0.0
        geometry_key = (hit_geometry_active, rows, row_height, center_top, center_bottom, margin, width)
        if geometry_key != self._hit_geometry_key:
            # Rectangles describe slots, not market values. Keep Qt wrappers
            # resident until layout changes instead of allocating/freeing two
            # rectangles per visible level on every snapshot.
            geometry = []
            for side in ('ask', 'bid'):
                slots = []
                if hit_geometry_active:
                    for index in range(rows):
                        top = (center_top - (index + 1) * row_height
                               if side == 'ask' else center_bottom + index * row_height)
                        rect = QtCore.QRectF(margin, top, width, row_height)
                        slots.append((rect, rect.toAlignedRect().adjusted(-1, -1, 1, 1)))
                geometry.append(tuple(slots))
            self._hit_geometry = tuple(geometry)
            self._hit_geometry_key = geometry_key
        ask_geometry, bid_geometry = self._hit_geometry
        for visual_index, row in enumerate(self._ask_rows):
            level = row.level
            if isinstance(level, OrderFlowDisplayLevel):
                key = (level.side, self._marker_key(level.price))
                signatures[key] = self._prepared_row_signature(row)
                if hit_geometry_active and visual_index < rows:
                    row_rect, dirty_rect = ask_geometry[visual_index]
                    hit_rows.append((row_rect, level.price, level))
                    rects[key] = dirty_rect
                    self._prepared_display_states[key] = str(row.display_state)
        for visual_index, row in enumerate(self._bid_rows):
            level = row.level
            if isinstance(level, OrderFlowDisplayLevel):
                key = (level.side, self._marker_key(level.price))
                signatures[key] = self._prepared_row_signature(row)
                if hit_geometry_active and visual_index < rows:
                    row_rect, dirty_rect = bid_geometry[visual_index]
                    hit_rows.append((row_rect, level.price, level))
                    rects[key] = dirty_rect
                    self._prepared_display_states[key] = str(row.display_state)
        # Install fresh maps only after the complete prepared state exists. Any
        # snapshot diff currently holding the old references remains stable.
        self._prepared_row_signatures = signatures
        self._prepared_row_rects = rects
        self._hit_rows = hit_rows
        if not hit_geometry_active:
            return

    def _commit_geometry_for_current_size(self) -> None:
        if not hasattr(self, '_row_metrics'):
            return
        width = max(1, self.width())
        height = max(1, self.height())
        old_rows = int(self._geometry.get('rows_per_side', 0))
        changed = self._refresh_geometry_for_size(width, height, self.snapshot)
        if not changed and self._prepared_size == (width, height):
            return
        new_rows = int(self._geometry.get('rows_per_side', 0))
        if self.snapshot is not None and self.snapshot.ready and new_rows > old_rows:
            now = time.monotonic()
            if len(self._bid_rows) < new_rows:
                self._bid_rows.extend(self._prepare_row(level, now=now, allow_state_transition=False, previous_metrics={}) for level in self.snapshot.bid_levels[len(self._bid_rows):new_rows])
            if len(self._ask_rows) < new_rows:
                self._ask_rows.extend(self._prepare_row(level, now=now, allow_state_transition=False, previous_metrics={}) for level in self.snapshot.ask_levels[len(self._ask_rows):new_rows])
        self._prepare_paint_content(width)
        self._rebuild_hit_rows()
        self._prepared_size = (width, height)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        self._geometry_cache_key = None
        self._prepared_size = (-1, -1)
        self._commit_geometry_for_current_size()
        if hasattr(self, '_row_metrics') and not self._resize_prepare_timer.isActive():
            self._resize_prepare_timer.start(display_frame_interval_ms(self))
        super().resizeEvent(event)

    def _flush_resize_prepare(self) -> None:
        if self._pending_source_snapshot is not None:
            if self._snapshot_prepare_timer.isActive():
                self._snapshot_prepare_timer.stop()
            self._flush_pending_snapshot()
            return
        self._prepare_display(reuse_rows=True)
        self._publish_visible_depth_rows()
        self.update()

    def _schedule_cue_expiry_repaint(self, expires: float) -> None:
        remaining_ms = max(1, int(math.ceil((expires - time.monotonic()) * 1000.0)))
        if not self._cue_expiry_timer.isActive() or self._cue_expiry_timer.remainingTime() > remaining_ms:
            self._cue_expiry_timer.start(remaining_ms)

    def _expire_change_cues(self) -> None:
        now = time.monotonic()
        stale = [key for key, entry in self._row_change_cues.items() if float(entry[2]) <= now]
        region = QtGui.QRegion()
        for side, marker_key in stale:
            rect = self._row_rect_for_marker_key(side, marker_key)
            if not rect.isNull():
                region += QtGui.QRegion(rect)
            self._row_change_cues.pop((side, marker_key), None)
            for prepared in (*self._bid_rows, *self._ask_rows):
                level = prepared.level
                if isinstance(level, OrderFlowDisplayLevel) and level.side == side and (self._marker_key(level.price) == marker_key):
                    prepared.change_cue = None
                    # Cue expiry mutates prepared row state outside
                    # _prepare_display(); keep the cached baseline in sync so the
                    # next snapshot diff does not compare against stale visuals.
                    self._prepared_row_signatures[(side, marker_key)] = self._prepared_row_signature(prepared)
                    break
        next_expiry = min((float(entry[2]) for entry in self._row_change_cues.values()), default=0.0)
        if next_expiry > now:
            self._schedule_cue_expiry_repaint(next_expiry)
        self._request_repaint_region('cue_expiry', region)

    def _expire_market_signal(self) -> None:
        signal = self._market_signal
        if not isinstance(signal, dict):
            return
        expires = float(signal.get('expires', 0.0) or 0.0)
        now = time.monotonic()
        if expires > now:
            self._market_signal_expiry_timer.start(max(1, int(math.ceil((expires - now) * 1000.0))))
            return
        anchor_side = str(signal.get('anchor_side', '') or '')
        anchor_price = float(signal.get('anchor_price', 0.0) or 0.0)
        old_rect = self._row_rect_for_market_price(anchor_side, anchor_price)
        self._market_signal = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=False)
        if old_rect.isNull():
            self.update()
        else:
            region = QtGui.QRegion(old_rect)
            new_rect = self._row_rect_for_market_price(anchor_side, anchor_price)
            if not new_rect.isNull():
                region += QtGui.QRegion(new_rect)
            self._request_repaint_region('market_signal_expiry', region)


    def _column_rect(self, name: str, top: float, height: float) -> QtCore.QRectF:
        left, right = self._geometry['columns'][name]
        return QtCore.QRectF(float(left), float(top), float(right) - float(left), float(height))

    def _draw_text(self, painter: QtGui.QPainter, rect: QtCore.QRectF, text: str, color: QtGui.QColor, alignment: Qt.AlignmentFlag, *, font: QtGui.QFont | None=None, pad: float=4.0) -> None:
        chosen_font = font or self._row_font
        painter.setFont(chosen_font)
        painter.setPen(color)
        text_rect = rect.adjusted(pad, 0.0, -pad, 0.0)
        if chosen_font == self._row_font:
            metrics = self._row_metrics
            font_key = self._row_font_key
        elif chosen_font == self._metric_font:
            metrics = self._metric_metrics
            font_key = self._metric_font_key
        elif chosen_font == self._symbol_font:
            metrics = self._symbol_metrics
            font_key = self._symbol_font_key
        elif chosen_font == self._label_font:
            metrics = self._label_metrics
            font_key = self._label_font_key
        elif chosen_font == self._effective_price_font:
            metrics = self._effective_price_metrics
            font_key = self._effective_price_font_key
        elif chosen_font == self._price_font:
            metrics = self._price_metrics
            font_key = self._price_font_key
        else:
            metrics = QtGui.QFontMetricsF(chosen_font)
            font_key = chosen_font.toString()
        available = max(1, int(text_rect.width()))
        cache_key = (text, font_key, available)
        rendered = self._text_layout_cache.get(cache_key)
        if rendered is None:
            rendered = metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
            if len(self._text_layout_cache) >= self.TEXT_LAYOUT_CACHE_CAPACITY:
                self._text_layout_cache.popitem(last=False)
                self._text_cache_evictions += 1
            self._text_layout_cache[cache_key] = rendered
            self._text_cache_misses += 1
        else:
            self._text_layout_cache.move_to_end(cache_key)
            self._text_cache_hits += 1
        painter.drawText(device_pixel_rect(self, text_rect), alignment | Qt.AlignmentFlag.AlignVCenter, rendered)

    def _draw_numeric_text(self, painter: QtGui.QPainter, rect: QtCore.QRectF, text: str, color: QtGui.QColor, alignment: Qt.AlignmentFlag, *, font: QtGui.QFont | None=None, pad: float=3.0, bar_contrast: bool=False) -> None:
        """Draw exact market numerics without ellipsis or cross-column bleed.

        Bar-backed values get at most one subtle dark baseline shadow. The DOM
        is permanently dark, so an opposite-luminance outline would create the
        bright glowing numerics visible in the old renderer.
        """
        if rect.width() <= 1.0 or rect.height() <= 1.0:
            return
        chosen_font = font or self._row_font
        text_rect = rect.adjusted(pad, 0.0, -pad, 0.0)
        painter.save()
        try:
            painter.setClipRect(device_pixel_rect(self, rect), Qt.ClipOperation.IntersectClip)
            painter.setFont(chosen_font)
            flags = alignment | Qt.AlignmentFlag.AlignVCenter
            if bar_contrast and text:
                shadow = QtGui.QColor('#000000')
                shadow.setAlpha(150)
                painter.setPen(shadow)
                px = max(0.65, min(1.0, self._physical_pixel_width()))
                painter.drawText(device_pixel_rect(self, text_rect.translated(0.0, px)), flags, text)
            painter.setPen(color)
            painter.drawText(device_pixel_rect(self, text_rect), flags, text)
        finally:
            painter.restore()

    def _draw_price_text(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        text: str,
        color: QtGui.QColor,
        alignment: Qt.AlignmentFlag=Qt.AlignmentFlag.AlignHCenter,
        *,
        pad: float=1.0,
        font: QtGui.QFont | None=None,
        metrics: QtGui.QFontMetricsF | None=None,
    ) -> None:
        """Draw an exact price or an explicit unavailable marker; never partial digits."""
        painter.save()
        try:
            painter.setClipRect(device_pixel_rect(self, rect), Qt.ClipOperation.IntersectClip)
            draw_font = font if isinstance(font, QtGui.QFont) else self._effective_price_font
            draw_metrics = metrics if isinstance(metrics, QtGui.QFontMetricsF) else self._effective_price_metrics
            painter.setFont(draw_font)
            painter.setPen(color)
            text_rect = rect.adjusted(pad, 0.0, -pad, 0.0)
            available = max(0.0, float(text_rect.width()))
            width = float(draw_metrics.horizontalAdvance(str(text))) if text else 0.0
            draw_text = str(text)
            if draw_text and width > available + 0.5:
                # Ellipsized prices such as ``12345…`` are unsafe in an execution
                # ladder because they still resemble a tradable exact value.
                # If the host is physically too narrow, show explicit unavailability.
                draw_text = '—'
                self._price_text_overflow = True
            painter.drawText(device_pixel_rect(self, text_rect), alignment | Qt.AlignmentFlag.AlignVCenter, draw_text)
        finally:
            painter.restore()

    def _draw_header_title(self, painter, bounds):
        g = self._geometry
        top, height = float(g['header_top']), float(g['title_height'])
        rect = QtCore.QRectF(bounds.left(), top, bounds.width(), height)
        painter.fillRect(rect, self._surface_top)
        status, color = self._header_text.get('status', ('CONNECTING', self._amber))
        color = self._bid if status == 'LIVE' else color
        symbol = self.symbol.removesuffix('USDT') + ' / USDT' if self.symbol.endswith('USDT') else self.symbol
        status_w = min(rect.width() * 0.43, max(65.0, self._label_metrics.horizontalAdvance(status) + 25.0))
        pill = QtCore.QRectF(rect.right() - status_w - 5.0, top + max(1.0, (height - 22.0) / 2), status_w, min(22.0, height - 2.0))
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        fill = QtGui.QColor(ORDERBOOK_REFERENCE['bg'])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, 5.0, 5.0)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QPointF(pill.left() + 10.0, pill.center().y()), 2.5, 2.5)
        painter.restore()
        self._draw_text(painter, pill.adjusted(18, 0, -2, 0), status, color, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=0)
        spread, spread_color = self._header_text.get('spread', ('—', self._muted))
        symbol_width = self._symbol_metrics.horizontalAdvance(symbol) + 15.0
        remaining = pill.left() - rect.left() - symbol_width - 10.0
        if remaining >= self._label_metrics.horizontalAdvance(f'Spread {spread}') + 10:
            self._draw_text(painter, QtCore.QRectF(rect.left() + symbol_width, top, remaining, height), f'Spread {spread}', spread_color, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=3.0)
        self._draw_text(painter, QtCore.QRectF(rect.left(), top, max(1.0, min(symbol_width, pill.left() - rect.left() - 5.0)), height), symbol, self._text, Qt.AlignmentFlag.AlignLeft, font=self._symbol_font, pad=6.0)

    def _draw_header_metrics(self, painter, bounds):
        g = self._geometry
        height = float(g['metric_height'])
        if height <= 0.0:
            return
        top = float(g['header_top']) + float(g['title_height'])
        rect = QtCore.QRectF(bounds.left(), top, bounds.width(), height)
        painter.fillRect(rect, self._surface_top)
        items = self._prepared_metric_draw_items
        if not items:
            items = (('Pressure', '—', self._muted), ('Flow · 5s', '—', self._muted))
        for index, (label, value, color) in enumerate(items):
            cell = QtCore.QRectF(rect.left() + index * rect.width() / len(items), top + 2.0, rect.width() / len(items), height - 4.0)
            if index:
                painter.setPen(self._column_border_pen)
                self._draw_snapped_line(painter, cell.left(), cell.top() + 7, cell.left(), cell.bottom() - 7)
            self._draw_text(painter, QtCore.QRectF(cell.left(), cell.top(), cell.width(), 18.0), label, self._muted, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=8)
            self._draw_numeric_text(painter, QtCore.QRectF(cell.left(), cell.top() + 18, cell.width(), cell.height() - 18), value, color, Qt.AlignmentFlag.AlignLeft, font=self._metric_font, pad=8)


    def _marker_key(self, price: float) -> int | float:
        if self.price_tick_size > 0.0:
            return int(round(price / self.price_tick_size))
        return round(price, 10)


    def _display_bucket_price(self, price: float, side: str) -> float:
        return self._bucket_price_for_multiplier(price, side, self.aggregation_multiplier)


    def _draw_column_header(self, painter: QtGui.QPainter, bounds: QtCore.QRectF) -> None:
        height = float(self._geometry['column_height'])
        if height <= 0.0:
            return
        top = float(self._geometry['column_top'])
        painter.fillRect(QtCore.QRectF(bounds.left(), top, bounds.width(), height), self._surface_raised)
        if self._book_depth:
            self._draw_text(painter, self._column_rect('price', top, height), 'PRICE', self._muted, Qt.AlignmentFlag.AlignRight, font=self._label_font, pad=8.0)
            if 'liquidity' in self._geometry['columns']:
                lane = self._column_rect('liquidity', top, height)
                amount_width = min(max(1.0, self._profile_amount_width), max(1.0, lane.width() - 8.0))
                unit = 'USDT' if self._value_mode == 'quote' else 'QTY'
                size_label = f'Size · {unit}' if amount_width >= 100.0 else 'Size'
                self._draw_text(painter, QtCore.QRectF(lane.left(), top, amount_width, height), size_label, self._muted, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=7.0)
                plot_left = min(lane.right(), lane.left() + amount_width + 2.0)
                plot_width = max(0.0, lane.right() - plot_left)
                depth_label = 'Cumulative depth' if plot_width >= 150.0 else 'Depth'
                self._draw_text(painter, QtCore.QRectF(plot_left, top, plot_width, height), depth_label, self._muted, Qt.AlignmentFlag.AlignRight, font=self._label_font, pad=8.0)
        else:
            labels = self._prepared_column_labels
            for name in self._geometry['columns']:
                rect = self._column_rect(name, top, height)
                alignment = Qt.AlignmentFlag.AlignRight if name in {'bid', 'delta', 'flow'} else Qt.AlignmentFlag.AlignLeft if name in {'ask', 'state', 'memory'} else Qt.AlignmentFlag.AlignHCenter
                text = labels.get(name, name.upper())
                color = self._bid if name == 'bid' else self._ask if name == 'ask' else self._muted
                self._draw_text(painter, rect, text, color, alignment, font=self._label_font, pad=7.0)

        painter.setPen(self._column_border_pen)
        for _name, (_left, right) in list(self._geometry['columns'].items())[:-1]:
            self._draw_snapped_line(painter, float(right), top, float(right), top + height)
        painter.setPen(self._separator_pen)
        self._draw_snapped_line(painter, bounds.left(), top + height, bounds.right(), top + height)

    def _profile_bar_clip_region(self) -> QtGui.QRegion:
        """Return the exact solid-bar pixels used by the DEPTH profile.

        The cumulative staircase uses this as a mask for its low-contrast
        in-bar pass.  Keeping the mask derived from the same geometry as
        ``_draw_profile_row`` prevents halos at bar ends or row boundaries.
        """
        if not self._book_depth or self.snapshot is None or not self.snapshot.ready:
            return QtGui.QRegion()
        g = self._geometry
        lane_values = g.get('columns', {}).get('liquidity')
        if lane_values is None:
            return QtGui.QRegion()
        limit = max(0, int(g.get('rows_per_side', 0)))
        if limit <= 0:
            return QtGui.QRegion()
        lane = self._column_rect('liquidity', float(g['table_top']), 1.0)
        amount_width = min(max(1.0, self._profile_amount_width), max(1.0, lane.width() - 8.0))
        plot_left = min(lane.right(), lane.left() + amount_width + 2.0)
        plot_width = max(0.0, lane.right() - plot_left - 7.0)
        if plot_width <= 0.0:
            return QtGui.QRegion()

        row_height = float(g['row_height'])
        center_top = float(g['center_top'])
        center_bottom = float(g['center_bottom'])
        region = QtGui.QRegion()
        sides = (
            ('ask', self._ask_rows[:limit], center_top, -1.0),
            ('bid', self._bid_rows[:limit], center_bottom, 1.0),
        )
        for side, rows, origin, direction in sides:
            del side
            for visual_index, row in enumerate(rows):
                top = origin + direction * visual_index * row_height
                if direction < 0.0:
                    top -= row_height
                key = (row.level.side, float(row.level.price))
                visual_size = self._profile_size_current.get(key, row.profile_size)
                width = plot_width * max(0.0, min(1.0, visual_size))
                if width <= 0.0:
                    continue
                row_top = top + 0.04 * row_height
                drawn_height = max(1.0, 0.92 * row_height)
                bar_top = row_top + 1.5
                bar_height = max(2.0, drawn_height - 3.0)
                bar_bottom = min(top + row_height, bar_top + bar_height)
                if bar_bottom <= bar_top:
                    continue
                rect = QtCore.QRectF(
                    plot_left, bar_top, max(1.0, width), bar_bottom - bar_top
                ).toAlignedRect()
                if not rect.isNull():
                    region += QtGui.QRegion(rect)
        return region

    def _draw_profile_ladder_inside_bars(self, painter: QtGui.QPainter) -> None:
        """Restore the staircase inside bars as a subordinate tonal seam."""
        if not self._book_depth or not self._profile_paths:
            return
        bar_region = self._profile_bar_clip_region()
        if bar_region.isEmpty():
            return
        painter.save()
        try:
            painter.setClipRegion(bar_region, Qt.ClipOperation.IntersectClip)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for side, (_area, outline) in self._profile_paths.items():
                pen = self._profile_in_bar_pens.get(side)
                if pen is None:
                    continue
                painter.setPen(pen)
                painter.drawPath(outline)
        finally:
            painter.restore()

    def _draw_table_backdrop(self, painter: QtGui.QPainter) -> None:
        top = float(self._geometry['table_top'])
        bottom = float(self._geometry['footer_top'])
        height = max(1.0, bottom - top)
        columns = self._geometry['columns']
        bounds = QtCore.QRectF(float(self._geometry['margin']), top, float(self._geometry['inner_width']), height)
        painter.fillRect(bounds, self._bg)
        if self._book_depth:
            if 'liquidity' in columns:
                lane = self._column_rect('liquidity', top, height)
                painter.setPen(self._row_separator_pen)
                for fraction in (0.25, 0.5, 0.75, 1.0):
                    plot_left = min(lane.right(), lane.left() + self._profile_amount_width + 2.0)
                    x = plot_left + max(0.0, lane.right() - plot_left - 7.0) * fraction
                    self._draw_snapped_line(painter, x, top, x, bottom)
            if self.snapshot is not None and self.snapshot.ready:
                # Draw the cumulative-depth staircase before the per-row bars.
                # This keeps it crisp in empty space while letting resting-liquidity
                # bars visually sit above it where they overlap.
                for side, (area, outline) in self._profile_paths.items():
                    # Keep the cumulative-depth plot background black; its outline
                    # conveys cumulative depth independently of the size bars.
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(self._profile_pens[side])
                    painter.drawPath(outline)
            return

        # Keep the ladder readable as three distinct zones. The tint is subtle
        # enough that the actual resting-size bars remain the visual data.
        if 'bid' in columns:
            painter.fillRect(self._column_rect('bid', top, height), self._bid_zone_fill)
        if 'ask' in columns:
            painter.fillRect(self._column_rect('ask', top, height), self._ask_zone_fill)
        if 'price' in columns:
            price_rect = self._column_rect('price', top, height)
            painter.fillRect(price_rect, self._price_axis_fill)

        painter.setPen(self._column_border_pen)
        for _name, (_left, right) in list(columns.items())[:-1]:
            self._draw_snapped_line(painter, float(right), top, float(right), bottom)

    def _state_color(self, state: str) -> QtGui.QColor:
        return self._state_colors.get(state, self._muted)

    def _state_lane_is_full(self, rect: QtCore.QRectF) -> bool:
        threshold = float(
            self._geometry.get('state_full_label_width', ORDERBOOK_STATE_FULL_LABEL_WIDTH)
        )
        return rect.width() + 1e-9 >= threshold

    def _state_text_fits(self, text: str, rect: QtCore.QRectF) -> bool:
        return self._label_metrics.horizontalAdvance(str(text)) + 8.0 <= rect.width() + 1e-9

    def _state_text_for_rect(self, state: str, rect: QtCore.QRectF) -> str:
        full = self._STATE_LABELS.get(state, state[:7]).upper()
        if not self._state_lane_is_full(rect) or not self._state_text_fits(full, rect):
            return self._STATE_ACRONYMS.get(state, state[:2]).upper()
        return full

    @staticmethod
    def _event_acronym(kind: str, text: str) -> str:
        upper = str(text).upper()
        for prefix, acronym in (
            ('ABS BID', 'BA'), ('ABS ASK', 'AA'), ('BREAKOUT', 'BO'), ('BREAKDOWN', 'BD'),
        ):
            if upper.startswith(prefix):
                return acronym
        return {
            'reload': 'RL', 'restack': 'RS', 'rejected_sell': 'SR',
            'rejected_buy': 'BR', 'rejected_both': 'TW',
        }.get(kind, ''.join(part[:1] for part in upper.split())[:2] or upper[:2])

    def _draw_signal_badge(self, painter, rect, text, color, fill=None, *, compact=False):
        if not text or rect.width() <= 4.0 or rect.height() <= 4.0:
            return
        width = min(max(16.0, self._label_metrics.horizontalAdvance(text) + 10.0), max(1.0, rect.width() - 6.0))
        height = min(20.0, rect.height() - 4.0)
        badge = QtCore.QRectF(rect.left() + 3, rect.center().y() - height / 2, width, height)
        badge_fill = QtGui.QColor(ORDERBOOK_REFERENCE['bg'])
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(badge_fill)
        painter.drawRoundedRect(badge, 4.0, 4.0)
        painter.restore()
        self._draw_text(painter, badge, text, color, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=2.0)

    def _draw_liquidity_memory(self, painter: QtGui.QPainter, level: OrderFlowDisplayLevel, rect: QtCore.QRectF) -> None:
        history = tuple(value for value in level.liquidity_history_30s if math.isfinite(float(value)))
        if not history or rect.width() <= 2.0 or rect.height() <= 2.0:
            return
        # The reference renders a tiny 30 s stepped area: neutral historical
        # silhouette with the newest samples in side color.
        values = history[-8:]
        count = len(values)
        segment = max(1.0, rect.width() / max(1, count))
        neutral = QtGui.QColor(ORDERBOOK_REFERENCE['grid_strong'])
        side = QtGui.QColor(self._bid_fill_strong if level.side == 'bid' else self._ask_fill_strong)
        side.setAlpha(145)
        recent_start = max(0, count - max(2, count // 3))
        for index, raw in enumerate(values):
            intensity = 0.0 if raw < 0.0 else max(0.06, min(1.0, float(raw)))
            bar_h = max(1.0, (rect.height() - 2.0) * intensity)
            left = rect.left() + index * segment
            fill = side if index >= recent_start else neutral
            painter.fillRect(QtCore.QRectF(left, rect.bottom() - bar_h - 1.0, max(1.0, segment + 0.25), bar_h), fill)


    def _draw_native_bbo_accent(self, painter: QtGui.QPainter, row: PreparedDomRow, price_rect: QtCore.QRectF, top: float, row_height: float) -> None:
        """Mark the native touch inside an aggregated best-price bucket."""
        if self.aggregation_multiplier <= 1 or self.price_tick_size <= 0.0 or (not row.is_native_touch):
            return
        level = row.level
        fraction = max(0.0, min(1.0, float(row.native_touch_fraction)))
        y = top + 1.0 + (1.0 - fraction) * max(0.0, row_height - 2.0)
        color = self._native_bid_accent if level.side == 'bid' else self._native_ask_accent
        accent_h = max(self._physical_pixel_width() * 2.0, 1.0)
        painter.fillRect(device_pixel_rect(self, QtCore.QRectF(price_rect.left() + 2.0, self._snap(y - accent_h * 0.5), max(1.0, price_rect.width() - 4.0), accent_h)), color)

    def _row_event_presentation(self, row: PreparedDomRow, display_state: str) -> tuple[str, QtGui.QColor, QtGui.QColor | None]:
        market_signal = str(row.market_signal_text)
        if market_signal:
            color = row.market_signal_color
            if not isinstance(color, QtGui.QColor):
                color = self._mid
            fill = row.market_signal_fill
            return (market_signal, color, fill if isinstance(fill, QtGui.QColor) else None)
        text = str(row.event_text)
        if not text:
            return ('', self._muted, None)
        kind = str(row.event_kind)
        if kind == 'reload':
            color = self._state_absorbing
        elif kind == 'restack':
            color = self._amber
        elif kind == 'rejected_sell':
            color = self._bid
        elif kind == 'rejected_buy':
            color = self._ask
        elif kind == 'rejected_both':
            color = self._mid
        else:
            color = self._state_color(display_state)
        fill = QtGui.QColor(ORDERBOOK_REFERENCE['bg'])
        return (text, color, fill)

    def _draw_row(self, painter: QtGui.QPainter, row: PreparedDomRow, top: float, row_height: float) -> None:
        if self._book_depth:
            self._draw_profile_row(painter, row, top, row_height)
            return
        level = row.level
        side_color = self._bid if level.side == 'bid' else self._ask
        columns = self._geometry['columns']
        row_rect = QtCore.QRectF(float(self._geometry['margin']), top, float(self._geometry['inner_width']), row_height)
        if row.is_native_touch:
            painter.fillRect(row_rect, self._best_bid_row_fill if level.side == 'bid' else self._best_ask_row_fill)
        if self._hover_price == level.price:
            painter.fillRect(row_rect, self._hover_fill)

        # Short-lived source changes are a thin edge cue, not another persistent
        # column. They now produce an actual pixel change when their timer fires.
        if row.change_cue is not None and isinstance(row.change_cue_color, QtGui.QColor):
            cue_w = max(1.0, self._physical_pixel_width() * 2.0)
            cue_x = row_rect.left() if level.side == 'bid' else row_rect.right() - cue_w
            painter.fillRect(device_pixel_rect(self, QtCore.QRectF(cue_x, top + 1.0, cue_w, max(1.0, row_height - 2.0))), row.change_cue_color)

        display_state = row.display_state
        state_rect = self._column_rect('state', top, row_height) if 'state' in columns else None
        if state_rect is not None and display_state != 'NORMAL':
            state_color = self._state_color(display_state)
            full_state_text = self._STATE_LABELS.get(display_state, display_state[:7]).upper()
            state_text = self._state_text_for_rect(display_state, state_rect)
            self._draw_signal_badge(
                painter, state_rect, state_text, state_color,
                self._state_badge_fills.get(display_state),
                compact=(state_text != full_state_text),
            )

        event_text, event_color, event_fill = self._row_event_presentation(row, display_state)
        if event_text:
            if state_rect is not None and display_state == 'NORMAL':
                full_event_text = str(event_text).upper()
                event_is_full = (
                    self._state_lane_is_full(state_rect)
                    and self._state_text_fits(full_event_text, state_rect)
                )
                shown = (
                    full_event_text
                    if event_is_full
                    else self._event_acronym(str(row.event_kind), event_text)
                )
                self._draw_signal_badge(
                    painter, state_rect, shown, event_color, event_fill, compact=not event_is_full
                )
            else:
                # STATE owns the lane; retain a minimal event edge marker rather
                # than stacking two labels in one row.
                anchor = state_rect if state_rect is not None else self._column_rect('price', top, row_height)
                pip_w = max(2.0, self._physical_pixel_width() * 3.0)
                pip_x = anchor.right() - pip_w - 1.0 if level.side == 'bid' else anchor.left() + 1.0
                painter.fillRect(device_pixel_rect(self, QtCore.QRectF(pip_x, top + 2.0, pip_w, max(2.0, row_height - 4.0))), event_color)

        if 'memory' in columns:
            memory_rect = self._column_rect('memory', top + 1.0, max(2.0, row_height - 2.0))
            self._draw_liquidity_memory(painter, level, memory_rect)

        if 'delta' in columns:
            delta_rect = self._column_rect('delta', top, row_height)
            current_notional = max(0.0, float(level.notional))
            delta_notional = float(level.delta_notional_5s)
            previous_notional = max(0.0, current_notional - delta_notional)
            denominator = max(previous_notional, abs(delta_notional), 1e-9)
            delta_pct = max(-100.0, min(100.0, delta_notional / denominator * 100.0))
            delta_significant = abs(delta_pct) > 2.0
            delta_color = row.delta_color if delta_significant and isinstance(row.delta_color, QtGui.QColor) else self._muted
            delta_text = self._signed_percent(delta_pct) if delta_significant else ''
            self._draw_numeric_text(painter, delta_rect, delta_text, delta_color, Qt.AlignmentFlag.AlignRight, font=self._row_font, pad=2.0)

        resting_name = 'bid' if level.side == 'bid' else 'ask'
        if resting_name in columns:
            resting_rect = self._column_rect(resting_name, top, row_height)
            intensity = max(0.0, min(1.0, float(row.liquidity_visual)))
            if intensity > 0.0:
                width = max(1.0, (resting_rect.width() - 3.0) * intensity)
                inner_h = max(4.0, row_height - 8.0)
                inner_top = top + (row_height - inner_h) * 0.5
                if level.side == 'bid':
                    bar = QtCore.QRectF(resting_rect.right() - width - 1.0, inner_top, width, inner_h)
                    fill = self._bid_fill
                else:
                    bar = QtCore.QRectF(resting_rect.left() + 1.0, inner_top, width, inner_h)
                    fill = self._ask_fill
                painter.fillRect(bar, fill)
            self._draw_numeric_text(
                painter, resting_rect, str(row.notional_text), side_color,
                Qt.AlignmentFlag.AlignRight if level.side == 'bid' else Qt.AlignmentFlag.AlignLeft,
                font=self._row_font, pad=7.0, bar_contrast=False,
            )

        price_rect = self._column_rect('price', top, row_height)
        painter.fillRect(price_rect, self._price_axis_fill)
        if row.is_native_touch:
            painter.fillRect(price_rect, self._best_bid_row_fill if level.side == 'bid' else self._best_ask_row_fill)
            painter.fillRect(QtCore.QRectF(price_rect.left() + 1, top + 5, 2, max(2.0, row_height - 10)), side_color)
        self._draw_price_text(painter, price_rect, str(row.price_text), side_color if row.is_native_touch else self._text, Qt.AlignmentFlag.AlignHCenter, pad=3.0)
        self._draw_native_bbo_accent(painter, row, price_rect, top, row_height)

        if 'flow' in columns:
            flow_rect = self._column_rect('flow', top, row_height)
            total = max(0.0, float(level.trade_notional_5s))
            signed = float(level.signed_trade_notional_5s)
            flow_pct = max(-100.0, min(100.0, signed / total * 100.0)) if total > 1e-9 else 0.0
            flow_significant = abs(flow_pct) > 2.0
            flow_color = self._bid if flow_pct > 2.0 else self._ask if flow_pct < -2.0 else self._muted
            self._draw_numeric_text(painter, flow_rect, self._signed_percent(flow_pct) if flow_significant else '', flow_color, Qt.AlignmentFlag.AlignRight, font=self._row_font, pad=2.0)
            # Preserve large-print execution information without resurrecting the
            # obsolete SELL TRADES / BUY TRADES columns.
            pip = max(1.5, self._physical_pixel_width() * 2.0)
            if row.sell_large:
                painter.fillRect(device_pixel_rect(self, QtCore.QRectF(flow_rect.left() + 1.0, top + 1.0, pip, pip)), self._ask)
            if row.buy_large:
                painter.fillRect(device_pixel_rect(self, QtCore.QRectF(flow_rect.right() - pip - 1.0, top + 1.0, pip, pip)), self._bid)

        painter.setPen(self._row_separator_pen)
        self._draw_snapped_line(painter, row_rect.left(), row_rect.bottom(), row_rect.right(), row_rect.bottom())

        if row.is_ltp_row:
            fraction = max(0.0, min(1.0, float(row.ltp_fraction)))
            line_y = top + 1.0 + (1.0 - fraction) * max(0.0, row_height - 2.0) if self.aggregation_multiplier > 1 and self.price_tick_size > 0.0 else top + row_height * 0.5
            # Mark the last print at the price edge so its cue never crosses
            # digits, quantities, or liquidity signals.
            marker_height = min(8.0, max(2.0, row_height - 6.0))
            marker_top = max(top + 2.0, min(top + row_height - marker_height - 2.0, line_y - marker_height / 2.0))
            painter.fillRect(QtCore.QRectF(price_rect.right() - 4.0, marker_top, 2.0, marker_height), self._mid)

    def _draw_profile_row(self, painter: QtGui.QPainter, row: PreparedDomRow, top: float, height: float) -> None:
        level = row.level
        g = self._geometry
        side = level.side
        hovered = self._hover_price == level.price
        bar_top, bar_height = top, max(1.0, height - 1.0)
        profile_key = (side, float(level.price))
        size = max(0.0, min(1.0, self._profile_size_current.get(profile_key, row.profile_size)))
        low = QtGui.QColor(ORDERBOOK_REFERENCE[f'{side}_fill']).getRgb()[:3]
        high = self._profile_colors[side].getRgb()[:3]
        heat = QtGui.QColor(*(round(a + (b - a) * size) for a, b in zip(low, high)))
        if not row.profile_in_range:
            heat.setAlpha(110)
        if row.profile_amount > 0.0:
            painter.fillRect(QtCore.QRectF(float(g['heat_left']), bar_top,
                                           float(g['heat_width']), bar_height), heat)
        if 'liquidity' in g['columns']:
            lane = self._column_rect('liquidity', top, height)
            plot_width = max(0.0, lane.width() - 3.0)
            if size > 0.0 and plot_width > 0.0:
                gradient = QtGui.QLinearGradient(lane.left(), 0.0, lane.right(), 0.0)
                alpha = min(235, round(40 + size * 195))
                if not row.profile_in_range:
                    alpha = round(alpha * 0.45)
                start = self._profile_colors[side].darker(155)
                end = self._profile_colors[side].darker(115)
                start.setAlpha(alpha)
                end.setAlpha(alpha)
                gradient.setColorAt(0.0, start)
                gradient.setColorAt(1.0, end)
                painter.fillRect(QtCore.QRectF(lane.left(), bar_top, plot_width * size,
                                               bar_height), QtGui.QBrush(gradient))
            if hovered:
                hover = QtGui.QColor(self._profile_colors[side])
                hover.setAlpha(130)
                painter.setPen(QtGui.QPen(hover, self._physical_pixel_width()))
                self._draw_snapped_line(painter, float(g['heat_left']), top + height - 1.0,
                                        lane.right() - 3.0, top + height - 1.0)

    def _draw_profile_row_values(self, painter: QtGui.QPainter, row: PreparedDomRow, top: float, height: float) -> None:
        side = row.level.side
        if 'liquidity' in self._geometry['columns']:
            lane = self._column_rect('liquidity', top, height)
            text_color = QtGui.QColor(self._profile_bar_text[side])
            if not row.profile_in_range:
                text_color.setAlpha(150)
            amount_left = lane.left()
            font = self._row_font
            amount_width = min(lane.right() - amount_left, self._profile_amount_width)
            amount_rect = QtCore.QRectF(amount_left, top, max(0.0, amount_width), height)
            self._draw_numeric_text(painter, amount_rect, row.notional_text, text_color,
                                    Qt.AlignmentFlag.AlignLeft, font=font, pad=4.0, bar_contrast=True)
            if self._hover_price == row.level.price:
                cumulative = self._profile_quantity_text(row.profile_cumulative)
                width = QtGui.QFontMetricsF(self._profile_annotation_font).horizontalAdvance(cumulative) + 4.0
                if not self._geometry['profile_totals_height']:
                    ruler_top, ruler_bottom = self._profile_rulers()
                    total_top = (
                        max(float(self._geometry['table_top']), min(ruler_top + 5.0, float(self._geometry['center_top']) - 20.0))
                        if side == 'ask' else
                        min(float(self._geometry['height']) - 20.0, max(ruler_bottom - 27.0, float(self._geometry['center_bottom'])))
                    )
                    if top < total_top + 20.0 and top + height > total_top:
                        return
                if lane.right() - width > amount_rect.right() + 4.0:
                    self._draw_numeric_text(painter, QtCore.QRectF(lane.right() - width - 3.0, top, width, height),
                                            cumulative, text_color, Qt.AlignmentFlag.AlignRight,
                                            font=self._profile_annotation_font, pad=0.0)

    def _paint_profile(self, painter: QtGui.QPainter, dirty: QtGui.QRegion) -> bool:
        g = self._geometry
        width, height = float(g['width']), float(g['height'])
        row_height = float(g['row_height'])
        ready = self.snapshot is not None and self.snapshot.ready
        ruler_top, ruler_bottom = self._profile_rulers()
        lane = g['columns'].get('liquidity')
        pens = {}
        painter.save()
        painter.setClipRect(QtCore.QRectF(0.0, float(g['table_top']), width,
                            max(0.0, height - float(g['table_top']))), Qt.ClipOperation.IntersectClip)
        if ready and lane is not None:
            left, right = float(lane[0]), float(lane[1]) - 3.0
            for side, (area, outline) in self._profile_paths.items():
                fill = QtGui.QLinearGradient(left, 0.0, right, 0.0)
                start = QtGui.QColor(ORDERBOOK_REFERENCE[f'{side}_fill'])
                end = QtGui.QColor(start)
                start.setAlpha(130)
                end.setAlpha(75)
                fill.setColorAt(0.0, start)
                fill.setColorAt(1.0, end)
                painter.fillPath(area, QtGui.QBrush(fill))
                line = QtGui.QLinearGradient(left, 0.0, right, 0.0)
                line.setColorAt(0.0, self._profile_colors[side].darker(140))
                line.setColorAt(1.0, self._profile_colors[side])
                pen = QtGui.QPen(QtGui.QBrush(line), self._physical_pixel_width())
                dim_line = QtGui.QLinearGradient(line)
                for position, color in line.stops():
                    dim_color = QtGui.QColor(color)
                    dim_color.setAlpha(100)
                    dim_line.setColorAt(position, dim_color)
                pens[side] = (pen, QtGui.QPen(QtGui.QBrush(dim_line), self._physical_pixel_width()))
            shade = QtGui.QColor(self._profile_bg)
            shade.setAlpha(150)
            painter.fillRect(QtCore.QRectF(left, 0.0, right - left, max(0.0, ruler_top)), shade)
            painter.fillRect(QtCore.QRectF(left, ruler_bottom, right - left,
                                           max(0.0, height - ruler_bottom)), shade)
        if ready:
            step = float(g['profile_step'])
            for side, anchor, start, direction, available in (
                ('ask', float(g['profile_ask_anchor']), float(g['center_top']) - row_height,
                 1.0, float(g['center_top']) - float(g['table_top'])),
                ('bid', self._profile_anchor_price, float(g['center_bottom']),
                 -1.0, height - float(g['center_bottom'])),
            ):
                for index in range(max(0, int(math.ceil(available / row_height)))):
                    top = start - index * row_height if side == 'ask' else start + index * row_height
                    price = anchor + direction * index * step
                    if price <= 0.0 or not dirty.intersects(QtCore.QRect(0, int(top), int(width), int(row_height) + 1)):
                        continue
                    in_range = ruler_top <= top + row_height * 0.5 <= ruler_bottom
                    major = round(price / step) % 10 == 0
                    color = QtGui.QColor('#FFFFFF') if major and in_range else self._profile_price if in_range else self._profile_dim_price
                    self._draw_price_text(painter, self._column_rect('price', top, row_height),
                                          self._price_text(price), color, Qt.AlignmentFlag.AlignRight, pad=4.0)
        rows_rendered = False
        if ready:
            for row in (*self._ask_rows, *self._bid_rows):
                top = self._profile_row_top(row)
                if dirty.intersects(QtCore.QRect(0, int(top), int(width), int(row_height) + 1)):
                    self._draw_profile_row(painter, row, top, row_height)
                    rows_rendered = True
            for side, (_area, outline) in self._profile_paths.items():
                for top, bottom, pen in (
                    (0.0, ruler_top, pens[side][1]),
                    (ruler_top, ruler_bottom, pens[side][0]),
                    (ruler_bottom, height, pens[side][1]),
                ):
                    if bottom <= top:
                        continue
                    painter.save()
                    painter.setClipRect(QtCore.QRectF(0.0, top, width, bottom - top), Qt.ClipOperation.IntersectClip)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(outline)
                    painter.restore()
            painter.setPen(QtGui.QPen(self._grid_strong, self._physical_pixel_width()))
            for y in (ruler_top, ruler_bottom):
                self._draw_snapped_line(painter, 0.0, y, width - 3.0, y)
            # Values are the last row layer so depth outlines cannot cross digits.
            for row in (*self._ask_rows, *self._bid_rows):
                top = self._profile_row_top(row)
                if dirty.intersects(QtCore.QRect(0, int(top), int(width), int(row_height) + 1)):
                    self._draw_profile_row_values(painter, row, top, row_height)
            if not g['profile_totals_height']:
                self._draw_profile_totals(painter, ruler_top, ruler_bottom)
            if self._last_trade_price > 0.0:
                bucket = math.floor(self._last_trade_price / g['profile_step'] + 1e-7) * g['profile_step']
                side = 'ask' if self._last_trade_price >= self.snapshot.midpoint else 'bid'
                top = self._profile_price_row_top(bucket, side)
                self._profile_last_drawn_y = top + row_height * 0.5
                # The print marker stays in the heat strip, clear of all digits.
                painter.fillRect(QtCore.QRectF(float(g['heat_left']), top + 2.0,
                                               2.0, max(1.0, row_height - 4.0)), self._profile_last)
            self._draw_profile_midpoint(painter)
        else:
            message = self._syncing_message() if self._book_validity_known and not self._book_valid else 'Waiting for market data'
            self._draw_text(painter, QtCore.QRectF(4.0, height * 0.4, max(0.0, width - 8), 40.0),
                            message, self._profile_price, Qt.AlignmentFlag.AlignHCenter, font=self._label_font)
        painter.restore()
        painter.fillRect(QtCore.QRectF(0.0, 0.0, width, float(g['table_top'])), self._profile_bg)
        for rect, text, color in self._profile_header_items:
            self._draw_text(painter, rect, text, color, Qt.AlignmentFlag.AlignLeft,
                            font=self._label_font, pad=0.0)
        if ready and g['profile_totals_height']:
            self._draw_profile_totals(painter, ruler_top, ruler_bottom)
        return rows_rendered

    def _draw_profile_totals(self, painter: QtGui.QPainter, ruler_top: float, ruler_bottom: float) -> None:
        """Keep ruler totals clear of individual quantities and midpoint digits."""
        g = self._geometry
        width = float(g['width'])
        bid, ask = self._profile_ruler_totals
        metrics = QtGui.QFontMetricsF(self._profile_annotation_font)
        if g['profile_totals_height']:
            top, height = float(g['profile_market_height']), float(g['profile_totals_height'])
            half = width * 0.5
            for side, value, left in (('bid', bid, 4.0), ('ask', ask, half + 2.0)):
                rect = QtCore.QRectF(left, top, max(0.0, half - 6.0), height)
                amount = self._profile_quantity_text(value) or '0'
                text = f'{side.title()} {amount}'
                if metrics.horizontalAdvance(text) > rect.width():
                    text = f'{side.title()} {self._compact_scalar(value) if value else "0"}'
                self._draw_numeric_text(painter, rect, text, self._profile_bar_text[side],
                                        Qt.AlignmentFlag.AlignLeft, font=self._profile_annotation_font, pad=0.0)
            return
        lane = g['columns'].get('liquidity')
        if lane is None:
            return
        available = max(0.0, float(lane[1]) - float(lane[0]) - self._profile_amount_width - 10.0)
        for side, value, y in (
            ('ask', ask, max(float(g['table_top']), min(ruler_top + 5.0, float(g['center_top']) - 20.0))),
            ('bid', bid, min(float(g['height']) - 20.0, max(ruler_bottom - 27.0, float(g['center_bottom'])))),
        ):
            text = self._profile_quantity_text(value) or '0'
            if metrics.horizontalAdvance(text) + 6.0 > available:
                text = self._compact_scalar(value) if value else '0'
            text_width = min(available, metrics.horizontalAdvance(text) + 6.0)
            rect = QtCore.QRectF(width - text_width - 3.0, y, text_width, 20.0)
            painter.fillRect(rect, self._profile_bg)
            self._draw_numeric_text(painter, rect, text, self._profile_bar_text[side],
                                    Qt.AlignmentFlag.AlignRight, font=self._profile_annotation_font, pad=3.0)

    def _draw_profile_midpoint(self, painter: QtGui.QPainter) -> None:
        """Give exact midpoint and imbalance digits their own protected space."""
        g = self._geometry
        top, bottom = float(g['center_top']), float(g['center_bottom'])
        width, height = float(g['width']), max(0.0, float(g['center_height']))
        painter.fillRect(QtCore.QRectF(0.0, top, width, height), self._profile_bg)
        painter.setPen(QtGui.QPen(self._grid_strong, self._physical_pixel_width()))
        for y in (top, bottom):
            self._draw_snapped_line(painter, 0.0, y, width, y)
        midpoint = self.snapshot.midpoint or self.snapshot.microprice
        precision = min(16, self.price_decimals + 1) if self.price_tick_size > 0.0 else None
        price_text = format_book_price(midpoint, precision)
        bid, ask = self._profile_ruler_totals
        delta = bid - ask
        delta_text = ('+' if delta > 0.0 else '−' if delta < 0.0 else '') + (self._profile_quantity_text(abs(delta)) or '0')
        delta_font = self._profile_annotation_font
        delta_width = QtGui.QFontMetricsF(delta_font).horizontalAdvance(delta_text) + 14.0
        stacked = width < 240.0
        available = max(1.0, width - 8.0 if stacked else width - delta_width - 16.0)
        font = QtGui.QFont(self._effective_price_font)
        pixel_size = font.pixelSize()
        while pixel_size > typography_min_pixel_size(TextRole.ORDERBOOK_PRICE) and QtGui.QFontMetricsF(font).horizontalAdvance(price_text) > available:
            pixel_size -= 1
            font = typography_font_at_pixel_size(self._effective_price_font, pixel_size)
        if stacked:
            price_rect = QtCore.QRectF(4.0, top, width - 8.0, height * 0.55)
            delta_rect = QtCore.QRectF(max(4.0, width - delta_width - 4.0), top + height * 0.55,
                                      min(delta_width, width - 8.0), height * 0.45)
        else:
            price_rect = QtCore.QRectF(4.0, top, available, height)
            delta_rect = QtCore.QRectF(max(4.0, width - delta_width - 4.0), top,
                                      min(delta_width, width - 8.0), height)
        self._draw_numeric_text(painter, price_rect, price_text, self._text,
                                Qt.AlignmentFlag.AlignLeft, font=font, pad=0.0)
        delta_color = self._profile_colors['bid' if delta >= 0.0 else 'ask']
        self._draw_numeric_text(painter, delta_rect.adjusted(10.0, 0.0, 0.0, 0.0),
                                delta_text, delta_color, Qt.AlignmentFlag.AlignRight,
                                font=delta_font, pad=0.0)
        if delta != 0.0:
            x, y = delta_rect.left() + 4.0, delta_rect.center().y()
            points = ((x - 3, y - 2), (x + 3, y - 2), (x, y + 3)) if delta < 0 else ((x - 3, y + 2), (x + 3, y + 2), (x, y - 3))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(delta_color)
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(a, b) for a, b in points]))

    def _draw_center_price_clipped(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        text: str,
        color: QtGui.QColor,
        alignment: Qt.AlignmentFlag,
        *,
        pad: float = 4.0,
        font: QtGui.QFont | None = None,
        metrics: QtGui.QFontMetricsF | None = None,
    ) -> None:
        """Draw an exact center-band price without bleeding into adjacent cells."""
        if rect.width() <= 1.0 or rect.height() <= 1.0:
            return
        painter.save()
        try:
            painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
            self._draw_price_text(
                painter, rect, text, color, alignment, pad=pad,
                font=font or self._center_price_font,
                metrics=metrics or self._center_price_metrics,
            )
        finally:
            painter.restore()

    def _draw_center(self, painter, bounds):
        top, height = float(self._geometry['center_top']), float(self._geometry['center_height'])
        rect = QtCore.QRectF(bounds.left(), top, bounds.width(), height)
        painter.fillRect(rect, self._center_fill)
        painter.setPen(self._column_border_pen)
        self._draw_snapped_line(painter, rect.left(), rect.top(), rect.right(), rect.top())
        self._draw_snapped_line(painter, rect.left(), rect.bottom(), rect.right(), rect.bottom())
        mid_text, mid_color = self._header_text.get('mid', ('—', self._mid))
        spread, _ = self._header_text.get('spread', ('—', self._muted))
        if self._book_depth:
            price_width = min(rect.width(), max(self._column_rect('price', top, height).width(), self._center_price_metrics.horizontalAdvance(mid_text) + 16))
            price_rect = QtCore.QRectF(rect.left(), top, price_width, height)
            if height >= 50:
                self._draw_text(painter, QtCore.QRectF(price_rect.left(), top + 3, price_rect.width(), 16), 'Midpoint', self._muted, Qt.AlignmentFlag.AlignRight, font=self._label_font, pad=8)
                price_rect.setTop(top + 19)
            self._draw_center_price_clipped(painter, price_rect, mid_text, mid_color, Qt.AlignmentFlag.AlignRight, pad=8)
            if rect.width() - price_width > 65:
                self._draw_text(painter, QtCore.QRectF(rect.left() + price_width + 8, top, rect.width() - price_width - 8, height), f'Spread {spread}', self._muted, Qt.AlignmentFlag.AlignRight, font=self._label_font, pad=8)
            return
        for name, key, caption, color in (('bid', 'best_bid', 'Best bid', self._bid), ('price', 'mid', 'Midpoint', mid_color), ('ask', 'best_ask', 'Best ask', self._ask)):
            cell = self._column_rect(name, top, height)
            if name == 'price':
                painter.fillRect(cell, self._center_price_fill)
            value, value_color = self._header_text.get(key, ('—', color))
            if height >= 50:
                self._draw_text(painter, QtCore.QRectF(cell.left(), top + 4, cell.width(), 16), caption, self._muted, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=2)
                cell.setTop(top + 20)
            font = self._center_price_font if name == 'price' else self._price_font
            metrics = self._center_price_metrics if name == 'price' else self._price_metrics
            # A larger center price must still fit the same exact-price envelope.
            if name == 'price' and metrics.horizontalAdvance(value) > cell.width() - 6:
                font, metrics = self._effective_price_font, self._effective_price_metrics
            self._draw_center_price_clipped(painter, cell, value, value_color, Qt.AlignmentFlag.AlignHCenter, pad=3, font=font, metrics=metrics)

    def _draw_footer(self, painter, bounds):
        height = float(self._geometry.get('footer_height', 0.0))
        if height <= 0:
            return
        top, bottom = float(self._geometry['footer_top']), float(self._geometry['footer_bottom'])
        rect = QtCore.QRectF(bounds.left(), top, bounds.width(), bottom - top)
        painter.fillRect(rect, self._surface_top)
        painter.setPen(self._column_border_pen)
        self._draw_snapped_line(painter, rect.left(), rect.top(), rect.right(), rect.top())
        if self._book_depth:
            bid, ask = self._profile_totals
            total = bid + ask
            share = bid / total if total > 0 else 0.5
            formatter = self._compact_scalar if self._value_mode == 'base' else self._money
            bid_text, ask_text = formatter(bid), formatter(ask)
            ready = total > 0
        else:
            share, bid_text, ask_text, ready = self._footer_depth_summary
        caption_height = min(25.0, rect.height() - 4.0)
        left_text = f'B {share * 100:.0f}%  {bid_text}' if ready else 'Bid —'
        right_text = f'{ask_text}  {(1 - share) * 100:.0f}% A' if ready else 'Ask —'
        if rect.width() < 320.0 and ready:
            left_text, right_text = f'Bid {share * 100:.0f}%', f'Ask {(1 - share) * 100:.0f}%'
        half = rect.width() * 0.40
        self._draw_numeric_text(painter, QtCore.QRectF(rect.left(), top + 2, half, caption_height), left_text, self._bid, Qt.AlignmentFlag.AlignLeft, font=self._footer_font, pad=6)
        self._draw_numeric_text(painter, QtCore.QRectF(rect.right() - half, top + 2, half, caption_height), right_text, self._ask, Qt.AlignmentFlag.AlignRight, font=self._footer_font, pad=6)
        if rect.width() >= 390:
            self._draw_text(painter, QtCore.QRectF(rect.left() + half, top + 2, rect.width() - 2 * half, caption_height), 'Visible depth', self._muted, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=0)
        bar_top = top + caption_height + 1
        if rect.height() >= 30:
            bar = QtCore.QRectF(rect.left() + 6, bar_top, max(0.0, rect.width() - 12), 3.0)
            painter.fillRect(bar, self._grid)
            if ready:
                painter.fillRect(QtCore.QRectF(bar.left(), bar.top(), bar.width() * share, bar.height()), self._bid)
                painter.fillRect(QtCore.QRectF(bar.left() + bar.width() * share + 2, bar.top(), max(0, bar.width() * (1 - share) - 2), bar.height()), self._ask)
        if not self._book_depth and rect.height() >= 46:
            items = self._prepared_footer_draw_items
            for index, (text, color) in enumerate(items):
                cell = QtCore.QRectF(rect.left() + index * rect.width() / len(items), top + 33, rect.width() / len(items), rect.height() - 33)
                self._draw_text(painter, cell, text, self._muted, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=2)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        started = time.perf_counter()
        if self._prepared_size != (max(1, self.width()), max(1, self.height())):
            self._commit_geometry_for_current_size()
        dirty_region = event.region()
        dirty = dirty_region.boundingRect()
        full_repaint = QtGui.QRegion(self.rect()).subtracted(dirty_region).isEmpty()
        self._last_paint_region = self._region_summary(dirty_region)
        self._last_paint_region_rects = dirty_region.rectCount()
        surface_area = max(1, self.width() * self.height())
        dirty_bounds = dirty_region.boundingRect()
        self._last_paint_region_bbox_pct = max(0, dirty_bounds.width()) * max(0, dirty_bounds.height()) / surface_area * 100.0
        if full_repaint:
            self._full_repaint_count += 1
        else:
            self._partial_repaint_count += 1
        paint_kind = self._classify_paint_region(dirty_region, full_repaint)
        painter = QtGui.QPainter(getattr(self, "_paint_target", None) or self)
        try:
            painter.setClipRegion(event.region())
            apply_text_render_hints(painter)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            if self._book_depth:
                painter.fillRect(dirty, self._profile_bg)
                rows_rendered = self._paint_profile(painter, dirty_region)
                painter.end()
                self._record_paint_timing(started, paint_kind, rows_painted=rows_rendered, painted_region=dirty_region)
                return
            painter.fillRect(dirty, self._bg)
            margin = float(self._geometry['margin'])
            bounds = QtCore.QRectF(margin, margin, max(1.0, self.width() - margin * 2.0), max(1.0, self.height() - margin * 2.0))

            def band_dirty(top: float, bottom: float) -> bool:
                band_top = max(0, int(math.floor(top)) - 1)
                band_bottom = min(self.height(), int(math.ceil(bottom)) + 1)
                return dirty_region.intersects(QtCore.QRect(0, band_top, self.width(), max(1, band_bottom - band_top)))
            header_top = float(self._geometry['header_top'])
            header_bottom = header_top + float(self._geometry['top_height'])
            header_title_bottom = header_top + float(self._geometry['title_height'])
            header_metric_top = header_title_bottom
            column_top = float(self._geometry['column_top'])
            column_bottom = column_top + float(self._geometry['column_height'])
            table_top = float(self._geometry['table_top'])
            center_top = float(self._geometry['center_top'])
            center_bottom = float(self._geometry['center_bottom'])
            footer_top = float(self._geometry['footer_top'])
            footer_bottom = float(self._geometry['footer_bottom'])
            ladder_dirty = band_dirty(table_top, footer_top)
            center_dirty = band_dirty(center_top, center_bottom)
            rows_rendered = False
            if band_dirty(header_top, header_title_bottom):
                self._draw_header_title(painter, bounds)
            if band_dirty(header_metric_top, header_bottom):
                self._draw_header_metrics(painter, bounds)
            if band_dirty(column_top, column_bottom):
                self._draw_column_header(painter, bounds)
            if ladder_dirty:
                self._draw_table_backdrop(painter)
            if bool(self._geometry.get('bbo_only')):
                if center_dirty:
                    if self.snapshot is not None and self.snapshot.ready:
                        self._draw_center(painter, bounds)
                        if self._book_depth:
                            self._draw_text(painter, QtCore.QRectF(bounds.left(), center_top, bounds.width(), max(1.0, center_bottom - center_top)), 'Expand panel for depth', self._muted, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=4.0)
                    else:
                        top = float(self._geometry['center_top'])
                        height = float(self._geometry['center_height'])
                        self._draw_text(painter, QtCore.QRectF(bounds.left(), top, bounds.width(), height), self._syncing_message(), self._muted, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=4.0)
                painter.end()
                self._record_paint_timing(started, paint_kind, rows_painted=False, painted_region=dirty_region)
                return
            if self.snapshot is None or not self.snapshot.ready:
                top = float(self._geometry['table_top'])
                bottom = float(self._geometry['footer_top'])
                if ladder_dirty:
                    painter.setFont(self._label_font)
                    painter.setPen(self._muted)
                    painter.drawText(device_pixel_rect(self, QtCore.QRectF(bounds.left(), top, bounds.width(), max(1.0, bottom - top))), Qt.AlignmentFlag.AlignCenter, self._syncing_message() if self._book_validity_known and not self._book_valid else 'Waiting for synchronized depth and quotes…')
                if band_dirty(footer_top, footer_bottom):
                    self._draw_footer(painter, bounds)
                painter.end()
                self._record_paint_timing(started, paint_kind, rows_painted=False, painted_region=dirty_region)
                return
            row_height = float(self._geometry['row_height'])
            table_top = float(self._geometry['table_top'])
            center_top = float(self._geometry['center_top'])
            center_bottom = float(self._geometry['center_bottom'])
            rows = int(self._geometry['rows_per_side'])
            visible_asks = self._ask_rows[:rows]
            for visual_index, row in enumerate(visible_asks):
                top = center_top - (visual_index + 1) * row_height
                row_rect = QtCore.QRectF(margin, top, float(self._geometry['inner_width']), row_height)
                if dirty_region.intersects(row_rect.toAlignedRect()):
                    self._draw_row(painter, row, top, row_height)
                    rows_rendered = True
            visible_bids = self._bid_rows[:rows]
            for visual_index, row in enumerate(visible_bids):
                top = center_bottom + visual_index * row_height
                row_rect = QtCore.QRectF(margin, top, float(self._geometry['inner_width']), row_height)
                if dirty_region.intersects(row_rect.toAlignedRect()):
                    self._draw_row(painter, row, top, row_height)
                    rows_rendered = True
            if ladder_dirty and self._book_depth:
                # Two-pass compositing: the saturated staircase was painted below
                # the bars in _draw_table_backdrop(); restore only its overlapping
                # pixels here using the quieter bar-derived pens.
                self._draw_profile_ladder_inside_bars(painter)
            if ladder_dirty:
                columns = self._geometry['columns']
                painter.setPen(self._column_border_pen)
                for _name, (_left, right) in list(columns.items())[:-1]:
                    self._draw_snapped_line(painter, float(right), table_top, float(right), float(self._geometry['footer_top']))
            if center_dirty:
                self._draw_center(painter, bounds)
            if band_dirty(footer_top, footer_bottom):
                self._draw_footer(painter, bounds)
            painter.end()
            self._record_paint_timing(started, paint_kind, rows_painted=rows_rendered, painted_region=dirty_region)
        finally:
            if painter.isActive():
                painter.end()

    def _classify_paint_region(self, region: QtGui.QRegion, full_repaint: bool) -> str:
        if full_repaint:
            return 'full'
        fast_region = self._header_title_region()
        fast_region += self._center_region()
        if region.subtracted(fast_region).isEmpty():
            return 'fast'
        return 'partial'

    def _record_paint_timing(self, started: float, paint_kind: str, *, rows_painted: bool, painted_region: QtGui.QRegion) -> None:
        now = time.perf_counter()
        elapsed = max(0.0, (now - started) * 1000.0)
        self.last_paint_ms = elapsed
        self.max_paint_ms = max(self.max_paint_ms, elapsed)
        self._paint_count += 1
        self._paint_timestamps.append(now)
        if paint_kind == 'full':
            self._full_paint_timestamps.append(now)
        elif paint_kind == 'fast':
            self._fast_paint_timestamps.append(now)
        else:
            self._partial_paint_timestamps.append(now)
        sequence = self._prepared_sequence
        visual_complete = rows_painted
        if sequence >= 0:
            if rows_painted:
                self._required_paint_rows_seen.add(sequence)
            required = self._required_paint_regions.get(sequence)
            if required is not None:
                remaining = required.subtracted(painted_region)
                if remaining.isEmpty():
                    self._required_paint_regions.pop(sequence, None)
                    visual_complete = sequence in self._required_paint_rows_seen
                else:
                    self._required_paint_regions[sequence] = remaining
                    visual_complete = False
        if visual_complete and sequence >= 0 and sequence != self._last_painted_sequence:
            self._required_paint_rows_seen.discard(sequence)
            self._last_painted_sequence = sequence
            received_at = self._snapshot_received_at.get(sequence)
            if received_at is not None:
                handoff = max(0.0, (now - received_at) * 1000.0)
                self.last_snapshot_to_paint_ms = handoff
                self.max_snapshot_to_paint_ms = max(self.max_snapshot_to_paint_ms, handoff)
                self._has_snapshot_to_paint_sample = True

            timing = self._snapshot_pipeline_timing.get(sequence, {})
            prepare_started = self._snapshot_prepare_started_at.get(sequence)
            prepare_completed = self._snapshot_prepare_completed_at.get(sequence)
            aggregation_started = timing.get('aggregation_started_mono')
            aggregation_completed = timing.get('aggregation_completed_mono')
            socket_received = timing.get('socket_received_mono')
            main_received = timing.get('main_depth_received_mono')
            worker_emit = timing.get('worker_emit_mono')
            worker_received = timing.get('worker_received_mono')
            worker_depth_processed = timing.get('worker_depth_processed_mono')
            build_started = self._snapshot_build_started_at.get(sequence) or timing.get('snapshot_build_started_mono')
            build_completed = self._snapshot_build_completed_at.get(sequence) or timing.get('snapshot_build_completed_mono')
            gui_delivery = timing.get('gui_delivery_mono')
            dom_received = timing.get('dom_received_mono') or received_at

            parser_done = timing.get('parser_done_mono')
            gui_dispatch = timing.get('gui_dispatch_mono')
            self._record_latency_stage('socket_to_parser', socket_received, parser_done)
            self._record_latency_stage('parser_to_gui_dispatch', parser_done, gui_dispatch)
            self._record_latency_stage('gui_dispatch_to_main', gui_dispatch, main_received)
            self._record_latency_stage('parser_to_main', parser_done, main_received)
            self._record_latency_stage('socket_to_main', socket_received, main_received)
            self._record_latency_stage('main_depth_handler', main_received, worker_emit)
            self._record_latency_stage('main_to_worker', worker_emit, worker_received)
            self._record_latency_stage('worker_depth_process', worker_received, worker_depth_processed)
            self._record_latency_stage('worker_wait', worker_depth_processed or worker_received, build_started)
            self._record_latency_stage('snapshot_build', build_started, build_completed)
            self._record_latency_stage('worker_to_gui', build_completed, gui_delivery)
            self._record_latency_stage('gui_to_dom', gui_delivery, dom_received)
            self._record_latency_stage('dom_queue', dom_received, prepare_started)
            self._record_latency_stage('aggregation', aggregation_started, aggregation_completed)
            self._record_latency_stage('prepare', prepare_started, prepare_completed)
            self._record_latency_stage('prepare_to_paint', prepare_completed, started)
            paint_samples = self._latency_stage_samples['paint']
            paint_samples.append(elapsed)
            self._latency_stage_max_ms['paint'] = max(self._latency_stage_max_ms['paint'], elapsed)
            self._record_latency_stage('dom_to_paint', dom_received, now)
            ingress_id = int(timing.get('depth_ingress_id', 0.0) or 0)
            if ingress_id > 0 and ingress_id != self._last_painted_depth_ingress_id:
                total = self._record_latency_stage('socket_to_paint', socket_received, now)
                if total is not None:
                    self._last_painted_depth_ingress_id = ingress_id
                    self.last_end_to_end_ms = total
                    self.max_end_to_end_ms = max(self.max_end_to_end_ms, total)
                    self._has_end_to_end_sample = True
                    self._last_pipe_sample_at = time.monotonic()
                    self._latency_pipe_display_window.append((self._last_pipe_sample_at, total))

    @staticmethod
    def _rolling_rate(timestamps: deque[float], now: float, window: float=2.0) -> float:
        if not timestamps or now - timestamps[-1] > window:
            return 0.0
        cutoff = now - window
        recent = [stamp for stamp in timestamps if stamp >= cutoff]
        if not recent:
            return 0.0
        span = min(window, max(0.25, now - recent[0]))
        return len(recent) / span

    def _actual_fps(self) -> float:
        return self._rolling_rate(self._paint_timestamps, time.perf_counter())

    @staticmethod
    def _percentile(samples: deque[float], percentile: float) -> float:
        if not samples:
            return 0.0
        values = sorted((float(value) for value in samples))
        index = int(round((len(values) - 1) * max(0.0, min(1.0, percentile))))
        return values[index]

    def performance_state(self) -> dict[str, float | int | str]:
        now = time.perf_counter()
        return {
            'paint_count': self._paint_count,
            'actual_fps': self._actual_fps(),
            'full_paints_per_second': self._rolling_rate(self._full_paint_timestamps, now),
            'fast_paints_per_second': self._rolling_rate(self._fast_paint_timestamps, now),
            'partial_paints_per_second': self._rolling_rate(self._partial_paint_timestamps, now),
            'last_paint_ms': self.last_paint_ms,
            'max_paint_ms': self.max_paint_ms,
            'last_prepare_ms': self.last_prepare_ms,
            'max_prepare_ms': self.max_prepare_ms,
            'last_diff_ms': self.last_diff_ms,
            'max_diff_ms': self.max_diff_ms,
            'last_snapshot_apply_ms': self.last_snapshot_apply_ms,
            'max_snapshot_apply_ms': self.max_snapshot_apply_ms,
            'last_snapshot_to_paint_ms': self.last_snapshot_to_paint_ms,
            'max_snapshot_to_paint_ms': self.max_snapshot_to_paint_ms,
            'last_end_to_end_ms': self.last_end_to_end_ms,
            'max_end_to_end_ms': self.max_end_to_end_ms,
            'snapshot_build_p50_ms': self._percentile(self._snapshot_build_samples, 0.5),
            'snapshot_build_p95_ms': self._percentile(self._snapshot_build_samples, 0.95),
            'snapshot_build_max_ms': max(self._snapshot_build_samples, default=0.0),
            **{
                f'pipeline_{name}_{metric}_ms': (
                    self._percentile(samples, percentile)
                    if metric != 'max' else self._latency_stage_max_ms.get(name, 0.0)
                )
                for name, samples in self._latency_stage_samples.items()
                for metric, percentile in (('p50', 0.50), ('p95', 0.95), ('p99', 0.99), ('max', 1.0))
            },
            **{f'pipeline_{name}_samples': len(samples) for name, samples in self._latency_stage_samples.items()},
            'full_repaints': self._full_repaint_count,
            'partial_repaints': self._partial_repaint_count,
            'partial_repaint_requests': self._requested_partial_repaint_count,
            'last_repaint_request_reason': self._last_repaint_request_reason or 'none',
            'last_repaint_request_region': self._last_repaint_request_region,
            'last_paint_region': self._last_paint_region,
            'last_paint_region_rects': self._last_paint_region_rects,
            'last_paint_region_bbox_pct': self._last_paint_region_bbox_pct,
            'repaint_request_row_snapshot': self._repaint_request_reason_counts.get('row_snapshot', 0),
            'repaint_request_hover': self._repaint_request_reason_counts.get('hover', 0),
            'repaint_request_cue_expiry': self._repaint_request_reason_counts.get('cue_expiry', 0),
            'repaint_request_market_signal': self._repaint_request_reason_counts.get('market_signal', 0),
            'repaint_request_market_signal_expiry': self._repaint_request_reason_counts.get('market_signal_expiry', 0),
            'snapshots_received': self._snapshot_received_count,
            'snapshots_full_applied': self._snapshot_full_applied_count,
            'snapshots_coalesced': self._snapshot_coalesced_count,
            'row_snapshots_superseded': self._snapshot_superseded_before_row_count,
            'geometry_rebuilds': self._geometry_rebuild_count,
            'text_cache_hits': self._text_cache_hits,
            'text_cache_misses': self._text_cache_misses,
            'text_cache_evictions': self._text_cache_evictions,
            'text_cache_entries': len(self._text_layout_cache),
            'aggregation_revision_reuses': self._aggregation_worker.reused,
            'aggregation_rebuilds': self._aggregation_worker.rebuilt,
            'row_revision_reuses': self._row_revision_reuses,
            'amount_width_cache_hits': self._amount_width_cache_hits,
            'amount_width_cache_misses': self._amount_width_cache_misses,
            'rows_per_side': int(self._geometry['rows_per_side']),
            'snapshot_sequence': max(self._prepared_sequence, self._latest_applied_sequence),
            'row_snapshot_sequence': self._prepared_sequence,
            'latest_applied_sequence': self._latest_applied_sequence,
            'layout_mode': str(self._geometry.get('mode', 'unknown')),
            'depth_mode': str(self._geometry.get('depth_mode', 'none')),
            'bbo_only': int(bool(self._geometry.get('bbo_only'))),
            'aggregation_multiplier': self.aggregation_multiplier,
            'visible_analytic_columns': ','.join(
                name for name in ('state', 'memory', 'delta', 'bid', 'price', 'ask', 'flow')
                if name in self._geometry.get('columns', {})
            ),
            'primary_analytic': str(self._geometry.get('primary_analytic', 'flow')),
            'visual_liquidity_scale': self._visual_scales['liquidity'],
            'visual_trade_scale': self._visual_scales['trade'],
            'visual_depth_scale': self._visual_scales['depth'],
            'price_text_overflow': int(bool(getattr(self, '_price_text_overflow', False))),
            'interaction_priority_active': int(self._interaction_priority_active),
            'row_frame_interval_ms': self._row_frame_interval_ms(),
        }

    def _hover_level(self, position: QtCore.QPointF) -> tuple[int | None, float, OrderFlowDisplayLevel | None]:
        if not self._geometry['table_top'] <= position.y() < self._geometry['footer_top']:
            return (None, 0.0, None)
        for index, (rect, price, level) in enumerate(self._hit_rows):
            # Adjacent rows share an edge; use the same half-open intervals as
            # the GUI endpoint so hover and click identify the same price.
            if (rect.left() <= position.x() < rect.right()
                    and rect.top() <= position.y() < rect.bottom()):
                return (index, price, level)
        return (None, 0.0, None)


    def _column_resize_boundary_at(self, position: QtCore.QPointF) -> tuple[str, str] | None:
        if bool(self._geometry.get('bbo_only', False)):
            return None
        y = float(position.y())
        top = float(self._geometry.get('column_top', 0.0))
        bottom = float(self._geometry.get('footer_top', self.height()))
        if y < top or y > bottom:
            return None
        columns = self._geometry.get('columns', {})
        if not isinstance(columns, dict) or len(columns) < 2:
            return None
        ordered = sorted(((str(name), float(bounds[0]), float(bounds[1])) for name, bounds in columns.items()), key=lambda item: item[1])
        tolerance = max(4.0, self._physical_pixel_width() * 4.0)
        x = float(position.x())
        for left, right in zip(ordered, ordered[1:]):
            if left[0] not in self.RESIZABLE_COLUMN_NAMES or right[0] not in self.RESIZABLE_COLUMN_NAMES:
                continue
            boundary = 0.5 * (left[2] + right[1])
            if abs(x - boundary) <= tolerance:
                return (left[0], right[0])
        return None

    def _begin_column_resize(self, pair: tuple[str, str], x: float) -> bool:
        columns = self._geometry.get('columns', {})
        if not isinstance(columns, dict):
            return False
        left_name, right_name = pair
        if left_name not in columns or right_name not in columns:
            return False
        preset_widths: dict[str, float] = {}
        for name, bounds in columns.items():
            try:
                width = float(bounds[1]) - float(bounds[0])
            except (TypeError, ValueError, IndexError):
                continue
            if name in self.RESIZABLE_COLUMN_NAMES and width > 0.0:
                preset_widths[str(name)] = width
        if left_name not in preset_widths or right_name not in preset_widths:
            return False
        self._column_width_overrides[self._presentation_preset] = preset_widths
        self._column_resize_boundary = pair
        self._column_resize_origin_x = float(x)
        self._column_resize_left_start = preset_widths[left_name]
        self._column_resize_right_start = preset_widths[right_name]
        self._column_resize_pair_total = self._column_resize_left_start + self._column_resize_right_start
        self._column_resize_active = True
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.grabMouse()
        return True

    def _update_column_resize(self, x: float) -> None:
        pair = self._column_resize_boundary
        if not self._column_resize_active or pair is None:
            return
        left_name, right_name = pair
        minimums = self._geometry.get('column_minimums', {})
        left_min = max(12.0, float(minimums.get(left_name, 12.0))) if isinstance(minimums, dict) else 12.0
        right_min = max(12.0, float(minimums.get(right_name, 12.0))) if isinstance(minimums, dict) else 12.0
        pair_total = max(left_min + right_min, float(self._column_resize_pair_total))
        proposed_left = self._column_resize_left_start + (float(x) - self._column_resize_origin_x)
        left_width = max(left_min, min(pair_total - right_min, proposed_left))
        right_width = pair_total - left_width
        overrides = dict(self._column_width_overrides.get(self._presentation_preset, {}))
        if abs(overrides.get(left_name, 0.0) - left_width) < 0.25 and abs(overrides.get(right_name, 0.0) - right_width) < 0.25:
            return
        overrides[left_name] = left_width
        overrides[right_name] = right_width
        self._column_width_overrides[self._presentation_preset] = overrides
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=True)
        self.update()

    def _finish_column_resize(self) -> None:
        if not self._column_resize_active:
            return
        self._column_resize_active = False
        self._column_resize_boundary = None
        try:
            self.releaseMouse()
        except RuntimeError:
            pass
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.presentation_changed.emit(self.presentation_state())

    def _profile_ruler_at(self, position: QtCore.QPointF) -> bool:
        return (self._book_depth and bool(self._geometry.get('profile_range_adjustable'))
                and float(self._geometry.get('heat_left', self.width())) <= position.x() < self.width()
                and float(self._geometry.get('table_top', 0.0)) <= position.y() < self.height()
                and any(abs(position.y() - y) <= 5.0 for y in self._profile_rulers()))

    def _cancel_pointer_interaction(self) -> None:
        active = self._profile_ruler_drag or self._profile_group_drag_origin is not None or self._column_resize_active
        self._profile_ruler_drag = False
        self._profile_group_drag_origin = None
        self._profile_group_drag_moved = False
        self._profile_group_drag_start_x = None
        self._column_resize_active = False
        self._column_resize_boundary = None
        if QtWidgets.QWidget.mouseGrabber() is self:
            self.releaseMouse()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if active:
            self.presentation_changed.emit(self.presentation_state())

    def _move_profile_control(self, position: QtCore.QPointF) -> bool:
        if self._profile_ruler_drag:
            origin, minimum, available = self._profile_ruler_limits()
            if available > minimum:
                self.set_depth_range((abs(position.y() - origin) - minimum) / (available - minimum), emit=False)
            return True
        if self._profile_group_drag_origin is not None:
            y, _price = self._profile_group_drag_origin
            distance = y - position.y()
            self._profile_group_drag_moved |= abs(distance) >= 8.0
            if self._profile_group_drag_start_x is not None:
                self._profile_group_drag_moved |= abs(position.x() - self._profile_group_drag_start_x) >= QtWidgets.QApplication.startDragDistance()
            if not self._profile_group_drag_moved:
                return True
            start = ORDER_FLOW_AGGREGATION_MULTIPLIERS.index(self._profile_group_drag_multiplier)
            index = max(0, min(len(ORDER_FLOW_AGGREGATION_MULTIPLIERS) - 1, start + int(distance / 24.0)))
            selected = ORDER_FLOW_AGGREGATION_MULTIPLIERS[index]
            # A click's normal pointer jitter must not turn off Auto or queue
            # configuration changes. Only an actual grouping step does that.
            if selected != self.aggregation_multiplier:
                self.set_aggregation_multiplier(selected)
            return True
        return False

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._move_profile_control(event.position()):
            event.accept()
            return
        if self._profile_ruler_at(event.position()):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            event.accept()
            return
        if self._column_resize_active:
            self._update_column_resize(event.position().x())
            event.accept()
            return
        resize_pair = self._column_resize_boundary_at(event.position())
        if resize_pair is not None:
            self.setCursor(Qt.CursorShape.SplitHCursor)
            event.accept()
            return
        _row_index, price, level = self._hover_level(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor
                       if level is not None and self.aggregation_multiplier == 1
                       else Qt.CursorShape.ArrowCursor)
        if price != self._hover_price:
            old_rect = self._row_rect_for_price(self._hover_price)
            self._hover_price = price
            new_rect = self._row_rect_for_price(price)
            region = QtGui.QRegion()
            for rect in (old_rect, new_rect):
                if not rect.isNull():
                    region += QtGui.QRegion(rect.adjusted(-1, -1, 1, 1))
            if not region.isEmpty():
                self._request_repaint_region('hover', region)
        super().mouseMoveEvent(event)

    def _row_rect_for_price(self, price: float) -> QtCore.QRect:
        if price <= 0.0:
            return QtCore.QRect()
        for rect, row_price, _level in self._hit_rows:
            if row_price == price:
                return rect.toAlignedRect()
        return QtCore.QRect()

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        old_rect = self._row_rect_for_price(self._hover_price)
        if not self._column_resize_active:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hover_price = 0.0
        self._hover_context = ''
        self.setToolTip('')
        if not old_rect.isNull():
            self._request_repaint_region('hover', QtGui.QRegion(old_rect.adjusted(-1, -1, 1, 1)))
        super().leaveEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        angle = event.angleDelta()
        dx = int(angle.x())
        dy = int(angle.y())
        if dy == 0 or abs(dx) > abs(dy):
            super().wheelEvent(event)
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.cycle_row_density(1 if dy > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._book_depth:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                if 0.0 <= event.position().y() < float(self._geometry.get('table_top', 0.0)):
                    self.customContextMenuRequested.emit(event.position().toPoint())
                    event.accept()
                    return
                if self._profile_ruler_at(event.position()):
                    self._profile_ruler_drag = True
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                    event.accept()
                    return
                _, price, _ = self._hover_level(event.position())
                if price > 0.0 and event.position().x() < float(self._geometry['heat_left']):
                    self._profile_group_drag_origin = (event.position().y(), price)
                    self._profile_group_drag_start_x = event.position().x()
                    self._profile_group_drag_multiplier = self.aggregation_multiplier
                    self._profile_group_drag_moved = False
                    event.accept()
                    return
            resize_pair = self._column_resize_boundary_at(event.position())
            if resize_pair is not None and self._begin_column_resize(resize_pair, event.position().x()):
                event.accept()
                return
            _row_index, price, _level = self._hover_level(event.position())
            if price > 0.0:
                if self.aggregation_multiplier > 1:
                    event.accept()
                    return
                self.price_selected.emit(price)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._profile_ruler_drag:
            self._profile_ruler_drag = False
            self.presentation_changed.emit(self.presentation_state())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._profile_group_drag_origin is not None:
            _, price = self._profile_group_drag_origin
            start_x = self._profile_group_drag_start_x
            self._profile_group_drag_origin = None
            self._profile_group_drag_start_x = None
            _, released_price, _ = self._hover_level(event.position())
            if (not self._profile_group_drag_moved and self.aggregation_multiplier == 1
                    and (start_x is None or abs(event.position().x() - start_x) < QtWidgets.QApplication.startDragDistance())
                    and released_price == price):
                self.price_selected.emit(price)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._column_resize_active:
            self._finish_column_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel_pointer_interaction()
            event.accept()
            return
        if self._book_depth and event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.set_depth_range(self._profile_ruler_fraction + (0.08 if event.key() == Qt.Key.Key_PageUp else -0.08))
            event.accept()
            return
        if self._book_depth and event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            index = ORDER_FLOW_AGGREGATION_MULTIPLIERS.index(self.aggregation_multiplier)
            index = max(0, min(len(ORDER_FLOW_AGGREGATION_MULTIPLIERS) - 1, index + (-1 if event.key() == Qt.Key.Key_Minus else 1)))
            self.set_aggregation_multiplier(ORDER_FLOW_AGGREGATION_MULTIPLIERS[index])
            event.accept()
            return
        super().keyPressEvent(event)
from ..models import OrderFlowPresentationFrame
from ..models import ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowSnapshot


class _DomRasterWorkerCanvas(_DomRasterCanvas):
    """The unchanged ladder renderer, confined to the child process's Qt thread."""

    def __init__(self, theme):
        self._dirty_pixels = QtGui.QRegion()
        self._raster_dpr = 1.0
        self._frame_interval = 7
        super().__init__(theme)
        self.setUpdatesEnabled(False)
        self.show()  # offscreen platform; keeps freshness/animation semantics

    def devicePixelRatioF(self):
        return self._raster_dpr

    def update(self, *args):
        if not args:
            region = QtGui.QRegion(self.rect())
        elif isinstance(args[0], QtGui.QRegion):
            region = args[0]
        elif isinstance(args[0], QtCore.QRect):
            region = QtGui.QRegion(args[0])
        else:
            region = QtGui.QRegion(QtCore.QRect(*args))
        self._dirty_pixels += region

    def _commit_full_snapshot(self, snapshot, *, force=False):
        # Already outside the application's interpreter/GIL. No nested thread
        # jobs or asynchronous aggregation may race the prepared image.
        _, display, started, completed = self._aggregation_worker.prepare(
            self._aggregation_context(), snapshot)
        self._adopt_full_snapshot(snapshot, display, force=force)

    def _profile_animation_interval_ms(self):
        return self._frame_interval

    def _row_frame_interval_ms(self):
        return self._frame_interval


class _DomRasterProcess:
    SNAPSHOT_INPUT = True

    def __init__(self, options):
        import os
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ['QT_FONT_DPI'] = str(options['dpi'])
        from ..utilities import load_app_fonts
        self.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        load_app_fonts(self.application, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.canvas = _DomRasterWorkerCanvas(options['theme'])
        self.config = {}
        self.epoch = self.market_epoch = -1
        self.memory = None
        self.images = []
        self.shape = None
        self.previous_slot = None
        self._slot_damage = [QtGui.QRegion(), QtGui.QRegion()]
        self._slot_revisions = [0, 0]
        self._frame_revision = 0
        self._sync_stats = dict(raster_sync_bytes=0, raster_sync_avoided_bytes=0,
                               raster_sync_copies=0, raster_sync_full_copies=0,
                               raster_sync_skipped=0, raster_sync_last_bytes=0,
                               raster_sync_last_ms=0.0, raster_sync_max_ms=0.0)
        self.pointer = None
        self.last_diagnostics = 0.0
        self._snapshot_decoder = SnapshotDecoder()

    def _configure(self, value):
        self.epoch, config = value
        canvas, old = self.canvas, self.config
        if config['market_epoch'] != self.market_epoch:
            canvas.set_symbol(config['symbol'])
            canvas.reset()
            self._snapshot_decoder = SnapshotDecoder()
            self.market_epoch = config['market_epoch']
        if old.get('typography') != config['typography']:
            profiles, globals_ = config['typography']
            typography_controller().configure(profiles, globals_)
        if old.get('theme') != config['theme']:
            canvas.apply_theme(config['theme'])
        setters = (
            ('tick', canvas.set_price_tick_size),
            ('aggregation', canvas.set_aggregation_multiplier),
            ('density', canvas.set_row_density),
            ('values', canvas.set_value_mode),
            ('depth', canvas.set_book_depth_enabled),
            ('depth_range', canvas.set_depth_range),
            ('columns', canvas.set_column_preferences),
            ('widths', canvas.restore_column_width_state),
        )
        for key, setter in setters:
            if old.get(key) != config[key]:
                setter(config[key])
        canvas._frame_interval = config['interval']
        if canvas._profile_animation_timer.interval() != canvas._frame_interval:
            canvas._profile_animation_timer.setInterval(canvas._frame_interval)
        canvas.set_interaction_priority(config.get('interaction_priority', False))
        canvas._raster_dpr = config['dpr']
        if old.get('size') != config['size'] or old.get('dpr') != config['dpr']:
            canvas.resize(*config['size'])
            canvas._prepared_size = (-1, -1)
            canvas._geometry_cache_key = None
            canvas._commit_geometry_for_current_size()
            canvas.update()
        self.config = config

    def _release_memory(self):
        self.canvas._paint_target = None
        self.images.clear()
        if self.memory is not None:
            self.memory.close()
            self.memory.unlink()
            self.memory = None
        self.shape = None
        self.previous_slot = None
        self._slot_damage = [QtGui.QRegion(), QtGui.QRegion()]
        self._slot_revisions = [0, 0]

    def _surface(self, lease):
        from multiprocessing.shared_memory import SharedMemory
        canvas = self.canvas
        dpr = canvas.devicePixelRatioF()
        width, height = max(1, math.ceil(canvas.width()*dpr)), max(1, math.ceil(canvas.height()*dpr))
        dpi = (canvas.logicalDpiX(), canvas.logicalDpiY())
        shape = (width, height, dpr, *dpi)
        if shape != self.shape:
            self._release_memory()
            size = width*height*4
            self.memory = SharedMemory(create=True, size=size*2)
            self.images = [QtGui.QImage(self.memory.buf[i*size:(i+1)*size], width, height,
                                       width*4, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                           for i in range(2)]
            for image in self.images:
                image.setDevicePixelRatio(dpr)
                # Match point-sized text to the canvas's measured logical DPI.
                image.setDotsPerMeterX(round(dpi[0] / 0.0254))
                image.setDotsPerMeterY(round(dpi[1] / 0.0254))
                image.fill(canvas._bg)
            self.shape, self.previous_slot = shape, None
            canvas._dirty_pixels = QtGui.QRegion(canvas.rect())
        slot = 1 - lease[1] if lease is not None and lease[0] == self.memory.name else 0
        return slot, self.images[slot]

    def _synchronize_surface(self, slot, dirty):
        stats = self._sync_stats
        stats['raster_sync_last_bytes'] = 0
        stats['raster_sync_last_ms'] = 0.0
        if self.previous_slot is None or self.previous_slot == slot:
            return
        started = time.perf_counter()
        target = self.images[slot]
        # Debt contains every paint missed by this slot, including unpublished
        # frames from a seed retry. Pixels fully replaced below need no copy.
        debt = pixel_region(self._slot_damage[slot], self.shape[2], target.rect())
        covered = pixel_region(dirty, self.shape[2], target.rect(), inward=True)
        copied = bounded_region(debt.subtracted(covered), target.rect())
        copied_bytes = region_area(copied) * 4
        full_bytes = target.width() * target.height() * 4
        copy_pixel_region(target, self.images[self.previous_slot], copied)
        elapsed = (time.perf_counter() - started) * 1000.0
        stats['raster_sync_bytes'] += copied_bytes
        stats['raster_sync_avoided_bytes'] += full_bytes - copied_bytes
        stats['raster_sync_last_bytes'] = copied_bytes
        stats['raster_sync_last_ms'] = elapsed
        stats['raster_sync_max_ms'] = max(stats['raster_sync_max_ms'], elapsed)
        stats['raster_sync_copies'] += int(bool(copied_bytes))
        stats['raster_sync_full_copies'] += int(copied_bytes == full_bytes)
        stats['raster_sync_skipped'] += int(not copied_bytes)

    def _surface_painted(self, slot, dirty):
        other = 1 - slot
        self._slot_damage[other] = bounded_region(
            self._slot_damage[other].united(dirty), self.canvas.rect())
        self._slot_damage[slot] = QtGui.QRegion()
        self._frame_revision += 1
        self._slot_revisions[slot] = self._frame_revision
        self.previous_slot = slot

    def _lease_matches(self, lease):
        return (lease is not None and len(lease) == 3 and self.memory is not None
                and lease[0] == self.memory.name and lease[1] in (0, 1)
                and lease[2] == self._slot_revisions[lease[1]])

    def _publish_surface(self, result, lease):
        slot = self.previous_slot
        if slot is None:
            return
        revision = self._slot_revisions[slot]
        if self._lease_matches(lease) and lease[1] == slot:
            return  # This complete image is already held by the GUI.
        base = tuple(lease) if self._lease_matches(lease) else None
        damage = self._slot_damage[lease[1]] if base is not None else QtGui.QRegion(self.canvas.rect())
        result['frame'] = (self.memory.name, slot, *self.shape[:3])
        result['frame_revision'] = revision
        result['frame_base'] = base
        # QRegion stays process-local; only bounded integer rectangles cross IPC.
        result['dirty_rects'] = tuple((r.x(), r.y(), r.width(), r.height()) for r in damage)
        canvas = self.canvas
        result['geometry'] = canvas._geometry
        ready = (canvas.snapshot is not None and canvas.snapshot.ready
                 and canvas._market_status not in {'STALE', 'LAST KNOWN', 'SYNCING', 'CONNECTING'})
        result['prices'] = (tuple(row.level.price for row in canvas._ask_rows) if ready else (),
                            tuple(row.level.price for row in canvas._bid_rows) if ready else ())
        result['prices_valid_until'] = (canvas.snapshot.generated_monotonic + min(
            BOOK_DEPTH_FRESH_SECONDS - canvas.snapshot.depth_age_seconds,
            BOOK_BBO_FRESH_SECONDS - canvas.snapshot.bbo_age_seconds,
        )) if ready else 0.0
        result['sequence'] = canvas._latest_applied_sequence
        result['layout'] = canvas.layout_state()

    def step(self, commands, lease):
        commands = dict(commands)
        if 'config' in commands:
            self._configure(commands.pop('config'))
        canvas = self.canvas
        seed_required = False
        # Old-market commands are rejected independently of their arrival order.
        for name, tagged in commands.items():
            market_epoch, args = tagged
            if market_epoch != self.market_epoch:
                continue
            if name == 'pointer':
                self.pointer = args
            elif name == 'snapshot':
                if args is not None:
                    try:
                        args = self._snapshot_decoder.decode(args, market_epoch)
                    except SnapshotSeedRequired:
                        # Keep the last complete image until the sender reseeds.
                        # No partial levels or trades enter the renderer.
                        seed_required = True
                        continue
                    canvas.set_snapshot(args)
                    if not canvas._interaction_priority_active:
                        canvas._snapshot_prepare_timer.stop()
                        canvas._flush_pending_snapshot()
            elif name in ('set_microstructure_snapshot',
                          'set_book_validity', 'set_trade_stream_status'):
                getattr(canvas, name)(*args)
        self.application.processEvents()
        # Preserve row hover and column-resize cursors in the raster process.
        if self.pointer is not None:
            x, y = self.pointer
            if x < 0:
                canvas.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
                self.pointer = None
            else:
                point = QtCore.QPointF(x, y)
                event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, point, point,
                                         Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                         Qt.KeyboardModifier.NoModifier)
                canvas.mouseMoveEvent(event)
        result = {'epoch': self.epoch, 'market_epoch': self.market_epoch,
                  'frame': None}
        if seed_required:
            result['_snapshot_seed_required'] = self.market_epoch
        if not canvas._dirty_pixels.isEmpty():
            slot, target = self._surface(lease)
            dirty = bounded_region(canvas._dirty_pixels, canvas.rect())
            canvas._dirty_pixels = QtGui.QRegion()
            self._synchronize_surface(slot, dirty)
            canvas._paint_target = target
            try:
                canvas.paintEvent(QtGui.QPaintEvent(dirty))
            finally:
                canvas._paint_target = None
            self._surface_painted(slot, dirty)
        # A discarded seed-recovery reply can contain the only new paint.
        # Republish that complete image even if the retry itself paints nothing.
        self._publish_surface(result, lease)
        timers = canvas.findChildren(QTimer)
        due = [max(1, timer.remainingTime()) for timer in timers if timer.isActive()]
        result['next_at'] = time.monotonic() + min(due, default=float('inf')) / 1000.0
        now = time.monotonic()
        if now - self.last_diagnostics >= 1.0:
            result['diagnostics'] = canvas.performance_state()
            result['diagnostics'].update(self._sync_stats)
            result['diagnostics']['snapshot_transport_received'] = self._snapshot_decoder.diagnostic_state()
            self.last_diagnostics = now
        result['_map'] = _map_dom_frame
        return result

    def close(self):
        self.canvas._aggregation_job.close()
        self.canvas.close()
        self._release_memory()


class _DomSharedPixels:
    def __init__(self, name, width, height, dpr):
        from multiprocessing.shared_memory import SharedMemory
        import sys
        kwargs = {'track': False} if sys.version_info >= (3, 13) else {}
        self.memory = SharedMemory(name=name, **kwargs)
        self.shape = (width, height, dpr)
        size = width*height*4
        self.images = [QtGui.QImage(self.memory.buf[i*size:(i+1)*size], width, height,
                                   width*4, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                       for i in range(2)]
        for image in self.images:
            image.setDevicePixelRatio(dpr)

    def refresh(self, slot):
        # External writes do not change QImage.cacheKey(). Give each completed
        # frame a fresh wrapper so a Qt paint engine cannot reuse stale pixels.
        width, height, dpr = self.shape
        size = width*height*4
        image = QtGui.QImage(self.memory.buf[slot*size:(slot+1)*size], width, height,
                             width*4, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        self.images[slot] = image

    def __del__(self):
        self.images.clear()
        self.memory.close()


import weakref
_DOM_PIXEL_MAPPINGS = weakref.WeakValueDictionary()


def _map_dom_frame(result):
    descriptor = result.get('frame')
    if descriptor is not None:
        name, slot, width, height, dpr = descriptor
        pixels = _DOM_PIXEL_MAPPINGS.get(name)
        if pixels is None:
            pixels = _DomSharedPixels(name, width, height, dpr)
            _DOM_PIXEL_MAPPINGS[name] = pixels
        pixels.refresh(slot)
        result['pixels'] = pixels
    return result


class OrderFlowDomCanvas(_DomRasterCanvas):
    """GUI endpoint: latest image adoption, one blit, and visible-price input.

    All ladder preparation, text layout, state inference, animation, visual
    diffing and raster drawing belong to _DomRasterProcess. Two leased shared
    images avoid copying pixels through pickle or overwriting a displayed frame.
    """

    def __init__(self, theme, parent=None):
        self._process_link = None
        self._display_frame = None
        self._pending_raster = None
        self._presentation_clock = None
        self._raster_ack_pending = False
        self._display_epoch = 0
        self._market_epoch = 0
        self._sent_config = None
        self._remote_diagnostics = {}
        self._remote_error = ''
        self._remote_typography = None
        self._unpainted_region = QtGui.QRegion()
        self._blit_stats = dict(gui_blit_pixels=0, gui_blit_last_pixels=0,
                               gui_full_paints=0, gui_partial_paints=0)
        super().__init__(theme, parent)
        self._aggregation_job.close()
        for timer in self.findChildren(QTimer):
            timer.stop()
        from .backend import _OrderBookProcessLink
        self._process_link = _OrderBookProcessLink(_DomRasterProcess,
            {'theme': dict(theme), 'dpi': self.logicalDpiX()}, self)
        self._process_link.ready.connect(self._adopt_raster)
        self._process_link.failed.connect(self._raster_failed)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._process_link.close)
        self._display_refresh_observer = DisplayRefreshObserver(self, self._queue_configuration)
        self._queue_configuration()

    def _queue_configuration(self):
        link = self._process_link
        if link is None:
            return
        config = dict(symbol=self.symbol, market_epoch=self._market_epoch,
                      tick=self.price_tick_size, aggregation=self.aggregation_multiplier,
                      density=self._row_density, values=self._value_mode, depth=self._book_depth,
                      depth_range=self._profile_ruler_fraction,
                      columns=self.column_preferences(), widths=self.column_width_state(),
                      size=(max(1,self.width()), max(1,self.height())), dpr=self.devicePixelRatioF(),
                      interval=display_frame_interval_ms(self), theme=self._bar_theme,
                      interaction_priority=self._interaction_priority_active,
                      typography=self._remote_typography)
        if config == self._sent_config:
            return
        # Pacing changes do not change the drawn prices or hit geometry. The
        # worker may keep its current image, so these must not invalidate it.
        if self._sent_config is None or any(
            value != self._sent_config[key] for key, value in config.items()
            if key not in {'interval', 'interaction_priority'}
        ):
            self._display_epoch += 1
        self._sent_config = config
        link.submit('config', (self._display_epoch, config))

    def _refresh_typography(self):
        super()._refresh_typography()
        from ..utilities import TYPOGRAPHY_DEFAULTS
        controller = typography_controller()
        self._remote_typography = ({role:controller.profile(role) for role in TYPOGRAPHY_DEFAULTS},
                                   controller.globals())
        self._queue_configuration()

    def height_for_rows(self, rows_per_side):
        # A density button must resize its host immediately, before the worker
        # returns. This rare control-layout calculation does not examine data.
        g = _compute_order_flow_dom_geometry(
            self.width(), self.height(), self._row_metrics.height(),
            self._price_metrics.height(), self._label_metrics.height(),
            row_density=self._row_density,
            book_depth=self._book_depth)
        fixed = sum(float(g[key]) for key in ('margin', 'top_height',
                                              'column_height', 'center_height', 'footer_height'))
        rows = max(0, int(rows_per_side) - int(self._book_depth))
        height = fixed + rows*g['nominal_row_height']*2
        if self._book_depth and not fixed.is_integer():
            height = math.floor(height) - 1
        return max(1, int(math.ceil(height)))

    def update(self, *args):
        # Setters invalidate remote state; only a completed image dirties Qt.
        self._queue_configuration()

    def _prepare_display(self, *, reuse_rows=False):
        self._queue_configuration()


    def _commit_geometry_for_current_size(self):
        self._queue_configuration()

    def _send(self, name, args):
        if self._process_link is not None:
            self._process_link.submit(name, (self._market_epoch, args))

    def set_snapshot(self, payload):
        frame = payload[0] if isinstance(payload, tuple) else payload
        snapshot = frame.snapshot if isinstance(frame, OrderFlowPresentationFrame) else frame
        if not isinstance(snapshot, OrderFlowSnapshot) or snapshot.symbol != self.symbol:
            return
        if snapshot.sequence <= self._latest_received_sequence:
            return
        self._initialize_profile_grouping(snapshot)
        self._latest_received_sequence = snapshot.sequence
        self._send('snapshot', payload)


    def set_microstructure_snapshot(self, snapshot):
        self._send('set_microstructure_snapshot', (snapshot,))

    def set_book_validity(self, valid, reason=''):
        # Keep input in sync immediately, before the worker paints the status.
        self._book_validity_known = True
        self._book_valid = bool(valid)
        if not valid:
            self._cancel_pointer_interaction()
        self._send('set_book_validity', (valid, reason))

    def set_trade_stream_status(self, active, reason=''):
        self._send('set_trade_stream_status', (active, reason))

    def set_interaction_priority(self, active):
        active = bool(active)
        if active == self._interaction_priority_active:
            return
        self._interaction_priority_active = active
        self._queue_configuration()
        if not active:
            self._commit_pending_raster()

    def set_presentation_clock(self, clock):
        if clock is self._presentation_clock:
            return
        if self._presentation_clock is not None:
            self._presentation_clock.interaction_frame.disconnect(self._commit_pending_raster)
        self._presentation_clock = clock
        if clock is not None:
            clock.interaction_frame.connect(self._commit_pending_raster)
        else:
            self._commit_pending_raster()

    def reset(self):
        self._cancel_pointer_interaction()
        self._market_epoch += 1
        self._pending_raster = None
        self._display_frame = None
        self._unpainted_region = QtGui.QRegion(self.rect())
        self._latest_received_sequence = -1
        self._queue_configuration()
        QtWidgets.QWidget.update(self)

    def resizeEvent(self, event):
        self._cancel_pointer_interaction()
        self._queue_configuration()
        QtWidgets.QWidget.resizeEvent(self, event)

    def event(self, event):
        result = super().event(event)
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange:
            self._queue_configuration()
        return result

    def showEvent(self, event):
        self._unpainted_region = QtGui.QRegion(self.rect())
        self._queue_configuration()
        if self._process_link is not None:
            self._process_link.enable(True)
        QtWidgets.QWidget.showEvent(self, event)

    def hideEvent(self, event):
        self._cancel_pointer_interaction()
        if self._process_link is not None:
            self._process_link.enable(False)
            self._pending_raster = None
            self._release_raster()
        QtWidgets.QWidget.hideEvent(self, event)

    def closeEvent(self, event):
        if self._process_link is not None:
            self._process_link.close()
        QtWidgets.QWidget.closeEvent(self, event)

    @QtCore.Slot(object)
    def _adopt_raster(self, result):
        self._raster_ack_pending = True
        if (result.get('frame') is not None
                and result['market_epoch'] == self._market_epoch
                and self.isVisible()
                and self._interaction_priority_active
                and self._presentation_clock is not None):
            # The leased image stays immutable until the shared frame's paint
            # acknowledges it. Only derived pixels wait; ingestion stays live.
            self._pending_raster = result
            self._presentation_clock.request()
            return
        self._commit_raster(result)

    def _commit_pending_raster(self, _frame_time=0.0):
        result, self._pending_raster = self._pending_raster, None
        if result is not None:
            self._commit_raster(result)

    def _commit_raster(self, result):
        self._remote_diagnostics['worker_pid'] = result['worker_pid']
        repaint = False
        if result['market_epoch'] == self._market_epoch:
            if result.get('frame') is not None:
                previous = self._display_frame
                if (previous is not None
                        and result.get('frame_base') == self._frame_lease(previous)
                        and previous['frame'][2:] == result['frame'][2:]
                        and self._native_frame(result)
                        and 'dirty_rects' in result):
                    damage = QtGui.QRegion()
                    for rect in result['dirty_rects']:
                        damage += QtCore.QRect(*rect)
                else:
                    # A new mapping, missing base, resized image or changed DPI
                    # cannot be applied as a delta to the current backing store.
                    damage = QtGui.QRegion(self.rect())
                self._display_frame = result
                self._geometry = result['geometry']
                self._publish_visible_depth_rows()
                self._publish_layout_state()
                self._unpainted_region = bounded_region(
                    self._unpainted_region.united(damage), self.rect())
                if not self._unpainted_region.isEmpty():
                    QtWidgets.QWidget.update(self, self._unpainted_region)
                    repaint = True
            if 'diagnostics' in result:
                self._remote_diagnostics.update(result['diagnostics'])
        if not repaint or not self.isVisible():
            self._release_raster()

    @staticmethod
    def _frame_lease(frame):
        return (*frame['frame'][:2], frame.get('frame_revision')) if frame is not None else None

    def _native_frame(self, frame):
        _, _, width, height, dpr = frame['frame']
        return (math.isclose(dpr, self.devicePixelRatioF())
                and width == math.ceil(self.width() * dpr)
                and height == math.ceil(self.height() * dpr))

    def _release_raster(self):
        # A paint of the previous image (for example during a window resize)
        # must not release the incoming image before its shared-frame adoption.
        if not self._raster_ack_pending or self._pending_raster is not None:
            return
        self._raster_ack_pending = False
        frame = self._display_frame
        lease = self._frame_lease(frame)
        self._process_link.consumed(lease=lease)

    @QtCore.Slot(str)
    def _raster_failed(self, message):
        import logging
        logging.getLogger(__name__).error('DOM rendering process: %s', message)
        self._remote_error = 'Order book renderer unavailable'
        self._pending_raster = None
        self._display_frame = None
        self._unpainted_region = QtGui.QRegion(self.rect())
        QtWidgets.QWidget.update(self)

    def paintEvent(self, event):
        started = time.perf_counter()
        painter = QtGui.QPainter(self)
        region = event.region().intersected(self.rect())
        painter.setClipRegion(region)
        blit_pixels = 0
        frame = self._display_frame
        if frame is None:
            painter.fillRect(event.rect(), self._bg)
            if self._remote_error:
                painter.setPen(self._muted)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._remote_error)
        else:
            image = frame['pixels'].images[frame['frame'][1]]
            if self._native_frame(frame):
                # Ceil-rounded fractional-DPI images extend past rect() slightly.
                # Clip native pixels instead of squeezing/resampling the glyphs.
                blit_pixels = draw_native_region(painter, image, region)
            else:
                # Keep resize feedback until the new-size/DPI frame arrives.
                painter.drawImage(QtCore.QRectF(self.rect()), image)
                dpr = self.devicePixelRatioF()
                bounds = QtCore.QRect(0, 0, math.ceil(self.width() * dpr), math.ceil(self.height() * dpr))
                blit_pixels = region_area(pixel_region(region, dpr, bounds))
        painter.end()
        self._unpainted_region = bounded_region(
            self._unpainted_region.subtracted(region), self.rect())
        self._blit_stats['gui_blit_pixels'] += blit_pixels
        self._blit_stats['gui_blit_last_pixels'] = blit_pixels
        full = QtGui.QRegion(self.rect()).subtracted(region).isEmpty()
        self._blit_stats['gui_full_paints' if full else 'gui_partial_paints'] += 1
        self.last_paint_ms = (time.perf_counter()-started)*1000.0
        self.max_paint_ms = max(self.max_paint_ms, self.last_paint_ms)
        self._paint_timestamps.append(time.monotonic())
        # Release the next worker frame only after Qt has consumed these pixels.
        # A burst cannot queue several GUI commits ahead of a single paint.
        self._release_raster()

    def _hover_level(self, position):
        frame = self._display_frame
        if (frame is None or frame['epoch'] != self._display_epoch
                or (self._book_validity_known and not self._book_valid)
                or time.monotonic() > frame.get('prices_valid_until', 0.0)
                or not self._native_frame(frame)):
            return None, 0.0, None
        g = frame['geometry']
        if g.get('bbo_only') or not g['margin'] <= position.x() < g['margin']+g['inner_width']:
            return None, 0.0, None
        # Layout arithmetic can land a few ULPs below an exact row boundary.
        # Stabilize the index before choosing the price drawn in that row.
        if g.get('book_depth'):
            if not float(g.get('table_top', 0.0)) <= position.y() < g['height']:
                return None, 0.0, None
            if position.y() < float(g['center_top']):
                side = 0
                slot = max(0, math.ceil((g['center_top'] - position.y()) / g['row_height'] - 1e-9) - 1)
                target = float(g.get('profile_ask_anchor', 0.0)) + slot * float(g.get('profile_step', 1.0))
            elif position.y() >= float(g['center_bottom']):
                side = 1
                slot = math.floor((position.y() - g['center_bottom']) / g['row_height'] + 1e-9)
                target = float(g.get('profile_anchor', 0.0)) - slot * float(g.get('profile_step', 1.0))
            else:
                return None, 0.0, None
            for index, price in enumerate(frame['prices'][side]):
                if math.isclose(price, target, rel_tol=0.0, abs_tol=max(1e-16, g.get('profile_step', 1.0) * 1e-5)):
                    return index, price, None
            return None, 0.0, None
        if not float(g['table_top']) <= position.y() < float(g['footer_top']):
            return None, 0.0, None
        if position.y() < g['center_top']:
            side, index = 0, max(0, math.ceil((g['center_top']-position.y())/g['row_height'] - 1e-9)-1)
        elif position.y() >= g['center_bottom']:
            side, index = 1, max(0, math.floor((position.y()-g['center_bottom'])/g['row_height'] + 1e-9))
        else:
            return None, 0.0, None
        prices = frame['prices'][side]
        if not 0 <= index < min(len(prices), int(g['rows_per_side'])):
            return None, 0.0, None
        return index, prices[index], None

    def _profile_ruler_at(self, position):
        frame = self._display_frame
        return (frame is not None and frame['epoch'] == self._display_epoch
                and self._native_frame(frame)
                and super()._profile_ruler_at(position))

    def _column_resize_boundary_at(self, position):
        frame = self._display_frame
        if (frame is None or frame['epoch'] != self._display_epoch
                or not self._native_frame(frame)):
            return None
        return super()._column_resize_boundary_at(position)

    def mouseMoveEvent(self, event):
        if self._move_profile_control(event.position()):
            event.accept()
            return
        if self._profile_ruler_at(event.position()):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self._send('pointer', (-1.0, -1.0))
        elif self._book_depth and 0.0 <= event.position().y() < float(self._geometry.get('table_top', 0.0)):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._send('pointer', (-1.0, -1.0))
        elif self._column_resize_active:
            self._update_column_resize(event.position().x())
        else:
            pair = self._column_resize_boundary_at(event.position())
            _, price, _ = self._hover_level(event.position())
            self.setCursor(Qt.CursorShape.SplitHCursor if pair else
                           Qt.CursorShape.PointingHandCursor if price and self.aggregation_multiplier == 1
                           else Qt.CursorShape.ArrowCursor)
            self._send('pointer', (event.position().x(), event.position().y()))
        event.accept()

    def leaveEvent(self, event):
        self._send('pointer', (-1.0, -1.0))
        self.setToolTip('')
        if not self._column_resize_active:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        QtWidgets.QWidget.leaveEvent(self, event)

    def performance_state(self):
        state = dict(self._remote_diagnostics)
        state.update(self._blit_stats)
        state['renderer'] = 'isolated process / shared image'
        state['gui_blit_ms'] = self.last_paint_ms
        state['gui_blit_max_ms'] = self.max_paint_ms
        state['gui_paint_fps'] = self._actual_fps()
        transport_state = getattr(self._process_link, 'snapshot_transport_state', None)
        if callable(transport_state):
            state['snapshot_transport'] = transport_state()
        state['worker_error'] = self._remote_error
        return state


class OrderBookWidget(QtWidgets.QWidget):
    """Adaptive price-centric order-flow workspace.

    ``OrderFlowDomCanvas`` owns all price-aligned state.  The chronological
    Time & Sales tape is a peer surface because chronology and price use
    different vertical coordinate systems; it is never forced into ladder Y
    alignment.  The shell may attach the tape lazily after first paint.
    """
    price_selected = Signal(float)
    aggregation_changed = Signal(int)
    column_preferences_changed = Signal(object)
    layout_state_changed = Signal(object)
    presentation_changed = Signal(object)
    right_rail_height_requested = Signal(int)
    snapshot_activity_requested = Signal(bool)
    depth_capacity_requested = Signal(int)
    DEPTH_CAPACITY_TIERS = (80, 120, 256, 512, 1000)
    DEPTH_OVERSCAN_ROWS = 12
    TAPE_MIN_WIDTH = 300
    TAPE_DEFAULT_WIDTH = 340
    TAPE_MAX_WIDTH = 460
    WIDE_CANVAS_MIN_WIDTH = 560
    EMBEDDED_TAPE_CONNECTED = True

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self.theme = {}
        self.symbol = 'BTCUSDT'
        self.price_tick_size = 0.0
        self._tape: TradesTapeWidget | None = None
        self._automatic_tape: TradesTapeWidget | None = None
        self._tape_source = None
        self._owned_tape_source = None
        self._tape_enabled = True
        self._tape_mode = 'LARGE'
        self._latest_snapshot: OrderFlowSnapshot | None = None
        self._last_depth_capacity = 0
        self.canvas = OrderFlowDomCanvas(theme, self)
        self._last_row_density = self.canvas.row_density()
        self.canvas.price_selected.connect(self.price_selected.emit)
        self.canvas.aggregation_changed.connect(self._on_canvas_aggregation_changed)
        self.canvas.column_preferences_changed.connect(self._on_canvas_column_preferences_changed)
        self.canvas.layout_state_changed.connect(self.layout_state_changed.emit)
        self.canvas.presentation_changed.connect(self._on_canvas_presentation_changed)
        self.canvas.visible_depth_rows_changed.connect(
            lambda _rows: self._publish_depth_capacity()
        )
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._show_context_menu)
        self.controls = OrderBookControlBar(theme, self)
        self.canvas.layout_state_changed.connect(lambda _state: self._sync_controls())
        self.controls.aggregation_selected.connect(lambda value: self.set_aggregation_multiplier(value, emit=True))
        self.controls.density_selected.connect(lambda value: self.set_row_density(value, emit=True))
        self.controls.value_mode_selected.connect(lambda value: self.set_value_mode(value, emit=True))
        self.controls.book_depth_toggled.connect(self.set_book_depth_enabled)
        self.controls.depth_range_selected.connect(self.canvas.set_depth_range)
        self.controls.auto_grouping_toggled.connect(self.set_auto_grouping)
        self.controls.reset_requested.connect(self._reset_display_options)
        self.controls.display_option_toggled.connect(self._on_display_option_toggled)
        # The chronological tape is a peer surface; it never consumes ladder
        # space on a narrow dock.
        self.splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.setStyleSheet(
            f"QSplitter::handle:horizontal {{ background: {ORDERBOOK_REFERENCE['bg']}; border: 0; border-left: 1px solid {ORDERBOOK_REFERENCE['grid']}; }}"
        )
        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.splitterMoved.connect(self._bound_tape_width)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.controls, 0)
        layout.addWidget(self.splitter, 1)
        self.setObjectName('orderBookWorkspace')
        self.setProperty('suppressNonessentialTooltips', True)
        self.setStyleSheet(f"QWidget#orderBookWorkspace {{ background: {ORDERBOOK_REFERENCE['bg']}; }}")
        self.setMinimumSize(0, 0)
        self.set_symbol(self.symbol)
        self._sync_controls()
        QtCore.QTimer.singleShot(0, self, self._ensure_trades_tape)

    def _ensure_trades_tape(self):
        # Some hosts provide their own tape during construction. Do not replace
        # it; standalone use gets the same fully functioning trade surface.
        if self._tape is None:
            self._automatic_tape = TradesTapeWidget({}, self)
            self.set_trades_tape(self._automatic_tape)

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

    def _ensure_tape_source(self):
        if self._tape_source is None:
            source = SharedTradeTapeSource(symbol=self.symbol, parent=self)
            self.set_tape_source(source)
            self._owned_tape_source = source
            if self._latest_snapshot is not None:
                source.ingest_snapshot(self._latest_snapshot)
        return self._tape_source

    def _emit_presentation_changed(self) -> None:
        self.presentation_changed.emit(self.presentation_state())

    def _on_canvas_aggregation_changed(self, value: int) -> None:
        self._sync_controls()
        self._publish_depth_capacity()
        self.aggregation_changed.emit(int(value))

    def _on_canvas_column_preferences_changed(self, preferences: object) -> None:
        self._sync_controls()
        self.column_preferences_changed.emit(preferences)

    def _on_canvas_presentation_changed(self, _state: object) -> None:
        density = self.canvas.row_density()
        density_changed = density != self._last_row_density
        self._last_row_density = density
        self._sync_controls()
        rows = self.canvas.visible_rows_per_side()
        if density_changed and rows > 0:
            target = self.controls.height() + self.canvas.height_for_rows(rows)
            self.right_rail_height_requested.emit(max(1, int(target)))
        self._emit_presentation_changed()


    def _sync_controls(self) -> None:
        state = self.canvas.presentation_state()
        self.controls.setVisible(True)
        self.controls.set_state(
            aggregation=int(self.canvas.aggregation_multiplier),
            tick_size=float(self.price_tick_size),
            preset=str(state.get('preset', 'execution')),
            density=str(state.get('density', 'normal')),
            value_mode=str(state.get('values', 'base')),
            tape_enabled=self._tape_enabled, tape_mode=self._tape_mode,
            book_depth=bool(state.get('book_depth', True)),
            overlays=self.canvas.column_preferences(),
            depth_range=float(state.get('depth_range', 0.72)),
            auto_grouping=bool(state.get('auto_grouping', True)),
            range_available=bool(self.canvas._geometry.get('profile_range_adjustable', False)),
        )

    def set_auto_grouping(self, enabled: bool) -> None:
        self.canvas._profile_auto_grouping = bool(enabled)
        self.canvas._profile_grouped_symbol = None
        if enabled and self._latest_snapshot is not None:
            self.canvas._initialize_profile_grouping(self._latest_snapshot)
        self._sync_controls()
        self._publish_depth_capacity()
        self._emit_presentation_changed()

    def _reset_display_options(self) -> None:
        self.canvas.set_book_depth_enabled(True, emit=False)
        self.canvas.set_presentation_preset('execution', emit=False)
        self.canvas.set_row_density('normal', emit=False)
        self._last_row_density = self.canvas.row_density()
        self.canvas.set_value_mode('base', emit=False)
        self.canvas.set_depth_range(0.72, emit=False)
        self.canvas._profile_auto_grouping = True
        self.canvas._profile_grouped_symbol = None
        if self._latest_snapshot is not None:
            self.canvas._initialize_profile_grouping(self._latest_snapshot)
        self.canvas.reset_column_widths(emit=False)
        # Execution is the canonical reference/default composition. Do not
        # contradict the preset by disabling every analytical lane after setting it.
        preferences = self.canvas.column_preferences()
        preferences.update({'state': True, 'memory': True, 'delta': True, 'flow': True, 'primary': 'flow'})
        self.canvas.set_column_preferences(preferences, emit=False)
        self._tape_enabled = False
        self._tape_mode = 'LARGE'
        if self._tape is not None:
            self._tape.set_mode('LARGE', emit=False)
        self._sync_tape_visibility(force_sizes=True)
        self._sync_controls()
        self.column_preferences_changed.emit(self.canvas.column_preferences())
        self._emit_presentation_changed()

    def _set_lane_visible(self, lane, visible):
        preferences = self.canvas.column_preferences()
        preferences[str(lane)] = bool(visible)
        self.canvas.set_column_preferences(preferences, emit=True)

    def _on_display_option_toggled(self, option, enabled):
        if option == 'tape':
            self.set_tape_enabled(enabled)
        else:
            self._set_lane_visible(option, enabled)

    def _required_depth_capacity(self) -> int:
        visible_rows = max(1, int(self.canvas.visible_rows_per_side()))
        multiplier = max(1, int(self.canvas.aggregation_multiplier))
        native_required = (visible_rows + self.DEPTH_OVERSCAN_ROWS) * multiplier
        native_required += max(0, multiplier - 1)
        for tier in self.DEPTH_CAPACITY_TIERS:
            if native_required <= tier:
                return tier
        return self.DEPTH_CAPACITY_TIERS[-1]

    def _publish_depth_capacity(self, *, force: bool=False) -> None:
        capacity = self._required_depth_capacity()
        if not force and capacity == self._last_depth_capacity:
            return
        self._last_depth_capacity = capacity
        self.depth_capacity_requested.emit(capacity)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self._sync_tape_visibility()
        self._publish_depth_capacity(force=True)

    def set_panel_active(self, active: bool) -> None:
        """Semantic host activity; reparenting must never restart market streams."""
        active = bool(active)
        if active == getattr(self, "_panel_active", False):
            return
        self._panel_active = active
        self.snapshot_activity_requested.emit(active)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_tape_visibility()
        # The control bar handles its own responsive geometry. Actual canvas
        # state/geometry signals refresh its values after the worker catches up.

    def set_trades_tape(self, tape: TradesTapeWidget | None) -> None:
        if tape is self._tape:
            return
        old = self._tape
        if old is not None:
            old.set_panel_active(False)
            try:
                old.mode_changed.disconnect(self._tape_mode_changed)
            except (TypeError, RuntimeError):
                pass
            old.setParent(None)
            if old is self._automatic_tape:
                self._automatic_tape = None
                old.deleteLater()
        self._tape = tape
        if tape is None:
            return
        tape.set_tape_source(self._ensure_tape_source())
        tape.set_presentation_clock(self.canvas._presentation_clock)
        tape.set_interaction_priority(self.canvas._interaction_priority_active)
        tape.setParent(self.splitter)
        tape.set_market(self.symbol, 0.0, tick_size=self.price_tick_size)
        tape.set_panel_active(True)
        tape.setMinimumWidth(self.TAPE_MIN_WIDTH)
        tape.setMaximumWidth(self.TAPE_MAX_WIDTH)
        tape.set_mode(self._tape_mode, emit=False)
        tape.mode_changed.connect(self._tape_mode_changed)
        self.splitter.addWidget(tape)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self._sync_tape_visibility(force_sizes=True)

    def _tape_mode_changed(self, mode: str) -> None:
        self._tape_mode = 'ALL' if str(mode).upper() == 'ALL' else 'LARGE'
        self._sync_controls()
        self._emit_presentation_changed()

    def set_tape_enabled(self, enabled: bool, *, emit: bool=True) -> None:
        enabled = bool(enabled)
        if enabled == self._tape_enabled:
            return
        self._tape_enabled = enabled
        self._sync_tape_visibility(force_sizes=True)
        self._sync_controls()
        if emit:
            self._emit_presentation_changed()


    def _protected_canvas_width(self) -> int:
        """Minimum ladder width that tape auto-layout is not allowed to steal.

        MEMORY and the other price-aligned analytical lanes outrank the
        chronological tape.  In Execution view MEMORY intentionally needs a
        larger canvas before it is introduced; if the user enabled it, the tape
        must wait until both can coexist rather than making MEMORY disappear at
        the exact resize point where Time & Sales appears.
        """
        return int(self.WIDE_CANVAS_MIN_WIDTH)

    def _desired_tape_width(self) -> int:
        return max(self.TAPE_MIN_WIDTH, min(self.TAPE_MAX_WIDTH, self.TAPE_DEFAULT_WIDTH))

    def _bound_tape_width(self, *_args: object) -> None:
        tape = self._tape
        if tape is None or not tape.isVisible():
            return
        total = max(1, self.splitter.width())
        required_canvas = self._protected_canvas_width()
        maximum_tape = max(self.TAPE_MIN_WIDTH, min(self.TAPE_MAX_WIDTH, total - required_canvas - self.splitter.handleWidth()))
        current = self.splitter.sizes()
        if len(current) < 2:
            return
        tape_width = min(maximum_tape, max(self.TAPE_MIN_WIDTH, int(current[1])))
        canvas_width = max(1, total - tape_width - self.splitter.handleWidth())
        if canvas_width < required_canvas:
            tape_width = max(self.TAPE_MIN_WIDTH, total - required_canvas - self.splitter.handleWidth())
            canvas_width = max(1, total - tape_width - self.splitter.handleWidth())
        if current[0] != canvas_width or current[1] != tape_width:
            blocker = QtCore.QSignalBlocker(self.splitter)
            self.splitter.setSizes([canvas_width, tape_width])
            del blocker

    def _sync_tape_visibility(self, *, force_sizes: bool=False) -> None:
        tape = self._tape
        if tape is None:
            return
        total = max(1, self.width())
        tape_width = self._desired_tape_width()
        required_canvas = self._protected_canvas_width()
        required_total = required_canvas + tape_width + self.splitter.handleWidth()
        should_show = bool(self.EMBEDDED_TAPE_CONNECTED and self._tape_enabled and self.isVisible() and (total >= required_total))
        visibility_changed = (not tape.isHidden()) != should_show
        if visibility_changed:
            tape.setVisible(should_show)
            self.splitter.refresh()
        if should_show and (force_sizes or visibility_changed):
            self.splitter.setSizes([max(required_canvas, total - tape_width - self.splitter.handleWidth()), tape_width])
        elif should_show:
            self._bound_tape_width()

    def _show_context_menu(self, pos: QtCore.QPoint) -> None:
        """The floating market label opens the heatmap's compact controls."""
        menu = QtWidgets.QMenu(self)
        views = QtGui.QActionGroup(menu)
        views.setExclusive(True)
        for enabled, name in ((True, 'Heatmap'), (False, 'Price ladder')):
            view = menu.addAction(name)
            view.setCheckable(True)
            view.setChecked(self.canvas._book_depth == enabled)
            views.addAction(view)
            view.triggered.connect(lambda checked=False, value=enabled:
                self.set_book_depth_enabled(value) if checked else None)
        menu.addMenu(self.controls._aggregation_menu).setText('Grouping')
        menu.addMenu(self.controls._display_menu).setText('Display')
        menu.addSeparator()
        reset_widths = menu.addAction('Reset column widths')
        reset_widths.setEnabled(bool(self.canvas.column_width_state()))
        reset_widths.triggered.connect(
            lambda: self.canvas.reset_column_widths(
                self.canvas.presentation_state().get('preset'), emit=True
            )
        )
        menu.addSeparator()
        reset_display = menu.addAction('Reset order book display')
        reset_display.triggered.connect(self._reset_display_options)
        menu.exec(self.canvas.mapToGlobal(pos))


    def set_order_flow_snapshot(self, snapshot: object) -> None:
        payload = snapshot
        if (
            isinstance(snapshot, tuple)
            and len(snapshot) == 2
            and isinstance(snapshot[0], OrderFlowPresentationFrame)
        ):
            source = snapshot[0].snapshot
        else:
            source = snapshot.snapshot if isinstance(snapshot, OrderFlowPresentationFrame) else snapshot
        if (not isinstance(source, OrderFlowSnapshot) or source.symbol != self.symbol
                or source.sequence <= self.canvas._latest_received_sequence):
            return
        self._ensure_tape_source().ingest_snapshot(source)
        self._latest_snapshot = source
        self.canvas.set_snapshot(payload)

    def set_microstructure_snapshot(self, snapshot: object) -> None:
        self.canvas.set_microstructure_snapshot(snapshot)

    def set_interaction_priority(self, active: bool) -> None:
        self.canvas.set_interaction_priority(active)
        if self._tape is not None:
            self._tape.set_interaction_priority(active)

    def set_presentation_clock(self, clock) -> None:
        self.canvas.set_presentation_clock(clock)
        if self._tape is not None:
            self._tape.set_presentation_clock(clock)

    def set_book_validity(self, valid: bool, reason: str='') -> None:
        self.canvas.set_book_validity(valid, reason)

    def set_trade_stream_status(self, active: bool, reason: str='') -> None:
        self.canvas.set_trade_stream_status(active, reason)

    def set_aggregation_multiplier(self, multiplier: int, *, emit: bool=False) -> None:
        self.canvas.set_aggregation_multiplier(multiplier, emit=emit)
        self._sync_controls()
        self._publish_depth_capacity()

    def aggregation_multiplier(self) -> int:
        return self.canvas.aggregation_multiplier

    def set_column_preferences(self, preferences: dict[str, object] | None, *, emit: bool=False) -> None:
        values = dict(preferences or {})
        self.canvas.set_column_preferences(values, emit=emit)
        self._sync_controls()

    def column_preferences(self) -> dict[str, object]:
        return self.canvas.column_preferences()

    def set_primary_analytic(self, name: str, *, emit: bool=True) -> None:
        self.canvas.set_primary_analytic(name, emit=emit)
        self._sync_controls()

    def set_row_density(self, density: str, *, emit: bool=True) -> None:
        normalized = str(density or 'normal').lower()
        if normalized not in {'compact', 'normal', 'relaxed'}:
            normalized = 'normal'
        if normalized == self.canvas.row_density():
            self._sync_controls()
            return

        # Row spacing is a panel-geometry control, not merely an internal paint
        # density toggle. Preserve the currently visible depth and ask the right
        # rail to resize this panel to the exact height required by the new row
        # height. This also removes any black remainder left below a capped DOM.
        self.canvas.set_row_density(normalized, emit=emit)
        self._last_row_density = self.canvas.row_density()
        self._sync_controls()
        self._publish_depth_capacity()

    def set_presentation_preset(self, name: str, *, emit: bool=True) -> None:
        # Compatibility entry point for old saved state/callers. Analytical lanes
        # are automatic now, so every legacy preset resolves to the same layout.
        # Do not alter the independent DEPTH toggle here.
        del name
        self.canvas.set_presentation_preset('execution', emit=emit)
        self._sync_controls()
        self._publish_depth_capacity()

    def set_book_depth_enabled(self, enabled: bool, *, emit: bool=True) -> None:
        self.canvas.set_book_depth_enabled(enabled, emit=emit)
        if enabled and self._latest_snapshot is not None:
            self.canvas._initialize_profile_grouping(self._latest_snapshot, emit=emit)
        self._sync_controls()
        self._publish_depth_capacity()

    def set_value_mode(self, mode: str, *, emit: bool=True) -> None:
        self.canvas.set_value_mode(mode, emit=emit)
        self._sync_controls()

    def presentation_state(self) -> dict[str, object]:
        state = self.canvas.presentation_state()
        state.update({'tape_enabled': self._tape_enabled, 'tape_mode': self._tape_mode})
        return state

    def restore_presentation_state(self, state: object, *, emit: bool=False) -> None:
        values = state if isinstance(state, dict) else {}
        migrated = values.get('profile_version') != 1
        self.canvas.set_presentation_preset('execution', emit=False)
        self.canvas.set_row_density('normal' if migrated else str(values.get('density', 'normal')), emit=False)
        self._last_row_density = self.canvas.row_density()
        self.canvas.set_value_mode('base' if migrated else str(values.get('values', 'base')), emit=False)
        self.canvas.restore_column_width_state(values.get('column_widths', {}))
        if isinstance(values.get('columns'), dict):
            self.canvas.set_column_preferences(values['columns'], emit=False)
        self.canvas.set_book_depth_enabled(True if migrated else bool(values.get('book_depth', True)), emit=False)
        self.canvas.set_depth_range(values.get('depth_range', 0.72), emit=False)
        self.canvas._profile_auto_grouping = migrated or bool(values.get('auto_grouping', False))
        self.canvas._profile_grouped_symbol = None
        if not migrated:
            self.canvas.set_aggregation_multiplier(values.get('aggregation', 1), emit=False)
        if self._latest_snapshot is not None:
            self.canvas._initialize_profile_grouping(self._latest_snapshot, emit=False)
        self._tape_enabled = False if migrated else bool(values.get('tape_enabled', False))
        self._tape_mode = 'ALL' if str(values.get('tape_mode', 'LARGE')).upper() == 'ALL' else 'LARGE'
        if self._tape is not None:
            self._tape.set_mode(self._tape_mode, emit=False)
        self._sync_tape_visibility(force_sizes=True)
        self._sync_controls()
        self._publish_depth_capacity()
        if emit:
            self._emit_presentation_changed()

    def layout_state(self) -> dict[str, object]:
        return self.canvas.layout_state()


    def set_symbol(self, symbol: str, rules: object | None=None) -> None:
        normalized = str(symbol).upper().strip().removesuffix('.P') or 'BTCUSDT'
        changed = normalized != self.symbol
        self.symbol = normalized
        if changed:
            self._latest_snapshot = None
        self.canvas.set_symbol(normalized)
        if changed and self._tape is not None:
            self._tape.set_market(normalized, 0.0)
        if rules is not None:
            self.set_symbol_rules(rules)
        elif changed:
            # Never bucket/format a new market with the previous symbol's tick.
            # The normal MainWindow path supplies rules immediately; this keeps
            # the public widget API safe when it is called standalone.
            self.set_symbol_rules(None)

    def set_symbol_rules(self, rules: object | None) -> None:
        tick_size = safe_float(getattr(rules, 'tick_size', 0.0)) if rules is not None else 0.0
        self.price_tick_size = max(0.0, tick_size)
        self.canvas.set_price_tick_size(self.price_tick_size)
        if self._latest_snapshot is not None:
            self.canvas._initialize_profile_grouping(self._latest_snapshot)
        if self._tape is not None:
            self._tape.set_market(self.symbol, self._tape.quote_volume_24h, tick_size=self.price_tick_size)
        self._sync_controls()

    def reset(self) -> None:
        self._latest_snapshot = None
        self.canvas.reset()
        if self._tape is not None:
            self._tape.reset(preserve_history=True)

    def apply_theme(self, theme: dict[str, str]) -> None:
        # Order-book chrome remains fixed; only DEPTH bars consume directional
        # theme tokens through the canvas' bar-only palette.
        self.theme = {}
        self.canvas.apply_theme(theme)
        self.controls.apply_theme({})
        self.splitter.setStyleSheet(
            f"QSplitter::handle:horizontal {{ background: {ORDERBOOK_REFERENCE['bg']}; border: 0; border-left: 1px solid {ORDERBOOK_REFERENCE['grid']}; }}"
        )
        if self._tape is not None:
            self._tape.apply_theme({})

    def performance_state(self) -> dict[str, float | int | str]:
        state = self.canvas.performance_state()
        state['tape_visible'] = int(bool(self._tape is not None and self._tape.isVisible()))
        if self._tape is not None:
            state['tape_view_frames'] = self._tape._print_cache_misses
            state['tape_view_formatted_rows'] = self._tape.model.formatted_rows
            state['tape_view_corrected_rows'] = self._tape.model.corrected_rows
            state['tape_view_model_resets'] = self._tape.model.resets
        state['presentation_preset'] = str(self.canvas.presentation_state().get('preset', 'execution'))
        state['row_density'] = self.canvas.row_density()
        state['value_mode'] = self.canvas.value_mode()
        state['book_depth'] = int(bool(self.canvas.presentation_state().get('book_depth', False)))
        return state
