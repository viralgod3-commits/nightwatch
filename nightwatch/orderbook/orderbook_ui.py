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
    'control_hover': '#000000',
    'control_pressed': '#000000',
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
    ORDER_FLOW_AGGREGATION_MULTIPLIERS, DomExecutionContext, DomPositionOverlay,
    OrderFlowDisplayLevel, OrderFlowPresentationFrame,
    OrderFlowSnapshot, OrderFlowTradePrint,
)

from ..presentation import display_frame_interval_ms
from ..chart.analysis import LatestJob


def _clamp(value: float, low: float=0.0, high: float=1.0) -> float:
    return max(low, min(high, value))

def _order_flow_semantic_event(*, trade_reload_count: int, recent_replenished_notional: float, restack_count: int, recent_restacked_notional: float, rejected_buy_prints: int, rejected_sell_prints: int) -> tuple[str, str]:
    """Return the canonical sparse event for one analytical price level."""
    if trade_reload_count > 0 and recent_replenished_notional > 0.0:
        return ('reload', 'Reload')
    if restack_count > 0 and recent_restacked_notional > 0.0:
        return ('restack', 'Restack')
    if rejected_sell_prints > 0 and rejected_buy_prints == 0:
        return ('rejected_sell', 'Sell reject')
    if rejected_buy_prints > 0 and rejected_sell_prints == 0:
        return ('rejected_buy', 'Buy reject')
    if rejected_buy_prints > 0 and rejected_sell_prints > 0:
        return ('rejected_both', 'Two-way')
    return ('', '')

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
            if side == 'bid':
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
            semantic_event_kind, semantic_event_label = _order_flow_semantic_event(trade_reload_count=reload_count, recent_replenished_notional=replenished, restack_count=restack_count, recent_restacked_notional=restacked, rejected_buy_prints=rejected_buy, rejected_sell_prints=rejected_sell)
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
    return replace(snapshot, liquidity_scale=liquidity_scale, delta_scale=delta_scale, trade_scale=trade_scale, cumulative_depth_scale=depth_scale, bid_levels=bids, ask_levels=asks)


class _DomAggregation:
    """Worker-owned bucket cache; no widget or mutable GUI state is accessed."""

    def __init__(self):
        self.context = None
        self.cache = {}
        self.source = None
        self.display = None

    def prepare(self, context, snapshot):
        started = time.perf_counter()
        if context != self.context:
            self.context = context
            self.cache.clear()
            self.source = self.display = None
        _epoch, _symbol, multiplier, tick_size = context
        if (self.source is not None and self.display is not None
                and snapshot.bid_levels is self.source.bid_levels
                and snapshot.ask_levels is self.source.ask_levels):
            previous = self.display
            display = replace(
                snapshot, liquidity_scale=previous.liquidity_scale,
                delta_scale=previous.delta_scale, trade_scale=previous.trade_scale,
                cumulative_depth_scale=previous.cumulative_depth_scale,
                bid_levels=previous.bid_levels, ask_levels=previous.ask_levels,
            )
        else:
            display = aggregate_order_flow_snapshot(
                snapshot, multiplier, tick_size, self.cache
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
from collections import deque
from datetime import datetime, timezone
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal
from ..models import OrderFlowSnapshot, OrderFlowTradePrint
from ..models import human_number
from ..utilities import ElidedLabel, TextRole, set_text_role

class _TradesTapeModel(QtCore.QAbstractTableModel):
    """Bounded rows; format only new/changed prints, paint only the viewport."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[OrderFlowTradePrint, tuple[str, ...]]] = []
        self.decimals: int | None = None
        self.value_mode = 'quote'
        self.buy = QtGui.QColor(ORDERBOOK_REFERENCE['bid'])
        self.sell = QtGui.QColor(ORDERBOOK_REFERENCE['ask'])
        self.muted = QtGui.QColor(ORDERBOOK_REFERENCE['muted'])

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else 5

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ('TIME', 'SIDE', 'PRICE', 'QTY' if self.value_mode == 'base' else 'VALUE', 'RESULT')[section]
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
        if role == Qt.ItemDataRole.ForegroundRole:
            return self.muted if column in (0, 4) else QtGui.QColor(ORDERBOOK_REFERENCE['text']) if column == 3 else (self.buy if trade.aggressor_side.upper() == 'BUY' else self.sell)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if column in (2, 3) else Qt.AlignmentFlag.AlignLeft))
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"{trade.aggressor_side.upper()} aggressor\nPrice: {cells[2]}\n"
                    f"Quantity: {trade.quantity:g}\nValue: {human_number(trade.notional, money=True)}\n"
                    f"Relative size: {trade.relative_size:.1f}×\n500 ms outcome: {trade.outcome.replace('_', ' ').lower()}")
        return None

    def _row(self, trade):
        stamp = datetime.fromtimestamp(trade.event_time_ms / 1000.0, timezone.utc).strftime('%H:%M:%S') if trade.event_time_ms > 0 else '—'
        return trade, (stamp, 'BUY' if trade.aggressor_side.upper() == 'BUY' else 'SELL',
                       format_book_price(trade.price, self.decimals),
                       human_number(trade.quantity) if self.value_mode == 'base' else human_number(trade.notional, money=True),
                       {'FOLLOW_THROUGH': 'FOLLOW', 'REJECTED': 'REJECT'}.get(trade.outcome, '—'))

    @staticmethod
    def identity(trade):
        # Local sequences restart when transport is paused or resynchronized.
        return trade.received_monotonic, trade.sequence

    def set_trades(self, trades, *, reformat=False):
        # Normal updates prepend a batch and trim the tail; don't reset scroll or
        # allocate thousands of QTableWidgetItems on each depth frame.
        previous = [self.identity(row[0]) for row in self.rows]
        sequences = [self.identity(trade) for trade in trades]
        prefix = sequences.index(previous[0]) if previous and previous[0] in sequences else len(sequences)
        retained = len(sequences) - prefix
        if reformat or (previous and sequences[prefix:] != previous[:retained]):
            self.beginResetModel()
            self.rows = [self._row(trade) for trade in trades]
            self.endResetModel()
            return
        if len(previous) > retained:
            self.beginRemoveRows(QtCore.QModelIndex(), retained, len(previous) - 1)
            del self.rows[retained:]
            self.endRemoveRows()
        if prefix:
            self.beginInsertRows(QtCore.QModelIndex(), 0, prefix - 1)
            self.rows[:0] = [self._row(trade) for trade in trades[:prefix]]
            self.endInsertRows()
        for row in range(prefix, len(trades)):
            if self.rows[row][0] != trades[row]:
                self.rows[row] = self._row(trades[row])
                self.dataChanged.emit(self.index(row, 4), self.index(row, 4))


class _TradePriceDelegate(QtWidgets.QStyledItemDelegate):
    """Mute the shared leading digits; color the changed suffix at equal weight."""

    @staticmethod
    def common_prefix(price: str, previous: str) -> int:
        if len(price.partition('.')[0]) != len(previous.partition('.')[0]):
            return 0
        for index, (left, right) in enumerate(zip(price, previous)):
            if left != right:
                return index
        return min(len(price), len(previous))

    def paint(self, painter, option, index):
        model = index.model()
        text = str(index.data() or '')
        previous = str(model.index(index.row() + 1, index.column()).data() or '')
        prefix = self.common_prefix(text, previous) if previous else 0
        rect = QtCore.QRectF(option.rect).adjusted(4, 0, -5, 0)
        metrics = QtGui.QFontMetricsF(option.font)
        if metrics.horizontalAdvance(text) > rect.width():
            return super().paint(painter, option, index)
        painter.save()
        try:
            painter.setClipRect(option.rect)
            painter.setFont(option.font)
            painter.fillRect(option.rect, QtGui.QColor(ORDERBOOK_REFERENCE['bg']))
            x = rect.right() - metrics.horizontalAdvance(text)
            y = rect.top() + (rect.height() - metrics.height()) / 2 + metrics.ascent()
            painter.setPen(model.muted)
            painter.drawText(QtCore.QPointF(x, y), text[:prefix])
            painter.setPen(index.data(Qt.ItemDataRole.ForegroundRole))
            painter.drawText(QtCore.QPointF(x + metrics.horizontalAdvance(text[:prefix]), y), text[prefix:])
        finally:
            painter.restore()


class TradesTapeWidget(QtWidgets.QWidget):
    """Independent Large Trades panel using the analyzer's classification.

    History is bounded to 500 prints per mode for the current market/session.
    Snapshot ingestion keeps history while hidden without any table mutations.
    Visible presentation is coalesced to at most ten commits per second.
    """
    mode_changed = Signal(str)
    CAPACITY = 500

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme = {}
        self.symbol = DEFAULT_SYMBOL
        self.quote_volume_24h = 0.0
        self.current_threshold = 1000.0
        self._mode, self._value_mode = 'LARGE', 'quote'
        self._active = False
        self._last_snapshot_print_sequence = 0
        self._snapshot_sequence = 0
        self._epoch = 0
        self._latest_snapshot_prints = ()
        self._history: dict[tuple[int, int], OrderFlowTradePrint] = {}
        self._large_history: dict[tuple[int, int], OrderFlowTradePrint] = {}
        self._unresolved: set[int] = set()
        self._dirty = True
        self._reformat = False
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self._refresh_table)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(12, 10, 10, 10)
        controls.setSpacing(6)
        self.title = ElidedLabel('Trade tape')
        set_text_role(self.title, TextRole.PANEL_TITLE)
        controls.addWidget(self.title, 1)
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
        layout.addLayout(controls)
        self.table = QtWidgets.QTableView(self)
        self.model = _TradesTapeModel(self.table)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(2, _TradePriceDelegate(self.table))
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
        header.setMinimumSectionSize(22)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.table.setToolTip('Newest first · times in UTC. Teal: aggressive buy; rose: aggressive sell. Shared leading price digits are dimmed against the preceding displayed trade. Scroll down to hold your place; scroll to the top to follow live trades.')
        self.empty = QtWidgets.QLabel('Waiting for large trades…', self.table.viewport())
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        set_text_role(self.empty, TextRole.UI_CAPTION)
        self.table.viewport().installEventFilter(self)
        layout.addWidget(self.table, 1)
        self.apply_theme(theme)
        self._size_columns()

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            self.empty.setGeometry(self.table.viewport().rect())
        return super().eventFilter(watched, event)

    def _size_columns(self):
        metrics = self.table.fontMetrics()
        header = self.table.horizontalHeader()
        for column, sample in ((0, '00:00:00'), (1, 'SELL'), (3, '$999.99M'), (4, 'FOLLOW')):
            # QStyledItemDelegate adds text insets inside the stylesheet padding.
            # Reserve both so timestamps and abbreviated values remain complete.
            header.resizeSection(column, metrics.horizontalAdvance(sample) + 20)
        # Outcome remains available in the row tooltip when the panel is narrow.
        self.table.setColumnHidden(4, self.width() < 410)
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
            self.reset()
        self.quote_volume_24h = max(0.0, quote_volume_24h)
        decimals = _decimal_places_from_step(tick_size) if tick_size > 0 else None
        if decimals != self.model.decimals:
            self.model.decimals = decimals
            self._reformat = self._dirty = True
            self._schedule_refresh()

    def set_quote_volume(self, quote_volume_24h):
        self.quote_volume_24h = max(0.0, quote_volume_24h)

    def reset(self, *, preserve_history=False):
        self._refresh_timer.stop()
        self._epoch += 1
        self._unresolved.clear()
        self._latest_snapshot_prints = ()
        self._last_snapshot_print_sequence = self._snapshot_sequence = 0
        self._dirty = True
        if not preserve_history:
            self._history.clear()
            self._large_history.clear()
            self.current_threshold = 1000.0
            self.model.set_trades([])
            self.status.setText('Waiting for trades')
            self.empty.setText('Waiting for large trades…' if self._mode == 'LARGE' else 'Waiting for trades…')
            self.empty.show()

    def set_panel_active(self, active):
        self._active = bool(active)
        if not self._active:
            self._refresh_timer.stop()
        else:
            self._schedule_refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_refresh()

    def hideEvent(self, event):
        self._refresh_timer.stop()
        super().hideEvent(event)

    def mode(self):
        return self._mode

    def set_mode(self, mode, *, emit=True):
        mode = 'ALL' if str(mode).upper() == 'ALL' else 'LARGE'
        if mode == self._mode:
            return
        self._mode = mode
        self.mode_button.setText('All' if mode == 'ALL' else 'Large')
        self.title.setText('Trade tape')
        self.empty.setText('Waiting for trades…' if mode == 'ALL' else 'Waiting for large trades…')
        self._reformat = self._dirty = True
        self._schedule_refresh()
        if emit:
            self.mode_changed.emit(mode)

    def toggle_mode(self):
        self.set_mode('ALL' if self._mode == 'LARGE' else 'LARGE')

    def set_value_mode(self, mode):
        mode = 'base' if str(mode).lower() == 'base' else 'quote'
        if mode != self._value_mode:
            self._value_mode = self.model.value_mode = mode
            self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 3, 3)
            self._reformat = self._dirty = True
            self._schedule_refresh()

    def set_order_flow_snapshot(self, snapshot):
        if not isinstance(snapshot, OrderFlowSnapshot) or snapshot.symbol != self.symbol:
            return
        prints = snapshot.recent_prints
        if (snapshot.sequence < self._snapshot_sequence or
                (prints and prints[-1].sequence < self._last_snapshot_print_sequence)):
            self.reset(preserve_history=True)
        self._snapshot_sequence = snapshot.sequence
        threshold = max(1.0, float(snapshot.large_trade_threshold or 1.0))
        self._dirty |= threshold != self.current_threshold
        self.current_threshold = threshold
        if prints is not self._latest_snapshot_prints:
            self._latest_snapshot_prints = prints
            fresh = []
            for trade in reversed(prints):
                if trade.sequence <= self._last_snapshot_print_sequence:
                    break
                fresh.append(trade)
            for trade in reversed(fresh):
                key = self._epoch, trade.sequence
                self._history[key] = trade
                if trade.salience_class >= 1:
                    self._large_history[key] = trade
                if trade.outcome == 'UNRESOLVED':
                    self._unresolved.add(trade.sequence)
            self._dirty |= bool(fresh)
            if prints:
                self._last_snapshot_print_sequence = prints[-1].sequence
                first = prints[0].sequence
                # Analyzer sequences are contiguous; resolve outcomes without a
                # second pass over its whole 2048-print snapshot on every frame.
                for sequence in tuple(self._unresolved):
                    offset = sequence - first
                    if offset < 0:
                        self._unresolved.discard(sequence)
                    elif offset < len(prints):
                        trade = prints[offset]
                        if trade.sequence == sequence and trade.outcome != 'UNRESOLVED':
                            for history in (self._history, self._large_history):
                                key = self._epoch, sequence
                                if key in history:
                                    history[key] = trade
                            self._unresolved.discard(sequence)
                            self._dirty = True
            for history in (self._history, self._large_history):
                excess = len(history) - self.CAPACITY
                if excess > 0:
                    for sequence in list(history)[:excess]:
                        del history[sequence]
            self._unresolved = {sequence for sequence in self._unresolved
                                if (self._epoch, sequence) in self._history
                                or (self._epoch, sequence) in self._large_history}
        self._schedule_refresh()

    def _schedule_refresh(self):
        if self._dirty and self._active and self.isVisible() and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _refresh_table(self):
        if not self._active or not self.isVisible() or not self._dirty:
            return
        history = self._large_history if self._mode == 'LARGE' else self._history
        trades = list(reversed(history.values()))
        bar = self.table.verticalScrollBar()
        follow = bar.value() == 0
        top = self.table.rowAt(0)
        anchor = self.model.identity(self.model.rows[top][0]) if top >= 0 and top < len(self.model.rows) else None
        offset = self.table.rowViewportPosition(top) if top >= 0 else 0
        self.model.set_trades(trades, reformat=self._reformat)
        self._dirty = self._reformat = False
        if follow:
            self.table.scrollToTop()
        elif anchor is not None:
            row = next((i for i, trade in enumerate(trades) if self.model.identity(trade) == anchor), None)
            if row is not None:
                self.table.scrollTo(self.model.index(row, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
                bar.setValue(bar.value() - offset)
        self.empty.setVisible(not trades)
        self.status.setText(f'≥ {human_number(self.current_threshold, money=True)}' if self._mode == 'LARGE' else f'{len(trades)} trades')

    def apply_theme(self, theme):
        self.theme = {}
        p = ORDERBOOK_REFERENCE
        self.setStyleSheet(
            f"QWidget {{ background: {p['bg']}; color: {p['text']}; border: 0; }}"
            f"QTableView {{ background: {p['bg']}; color: {p['text']}; border: 0; }}"
            f"QHeaderView::section {{ background: {p['surface_raised']}; color: {p['muted']}; border: 0; padding: 7px 4px; }}"
            f"QTableView::item {{ border: 0; padding: 3px 4px; }}"
            f"QToolButton {{ background: {p['surface_raised']}; color: {p['text']}; border: 1px solid {p['grid_strong']}; border-radius: 5px; padding: 5px 8px; }}"
            f"QToolButton:hover {{ background: {p['control_hover']}; }}"
            f"QScrollBar:vertical {{ background: {p['bg']}; width: 7px; margin: 0; }}"
            f"QScrollBar::handle:vertical {{ background: {p['grid_strong']}; min-height: 24px; border-radius: 3px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}"
        )

import math
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal
from ..models import ORDER_FLOW_AGGREGATION_MULTIPLIERS
from ..utilities import TextRole, typography_controller, typography_font, typography_font_at_pixel_size, typography_min_pixel_size

class _OrderBookSurfaceButton(QtWidgets.QPushButton):
    """Keyboard-accessible control with a clear selected state."""
    BUTTON_HEIGHT = 30
    HORIZONTAL_PADDING = 8
    MIN_CONTENT_WIDTH = 28

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
            f"border: 1px solid transparent; border-radius: 6px; padding: 0 {self.HORIZONTAL_PADDING}px; }}"
            f"QPushButton:hover {{ color: {p['text']}; background: {p['control_hover']}; }}"
            f"QPushButton:pressed {{ background: {p['control_pressed']}; }}"
            f"QPushButton:checked {{ color: {p['text']}; background: {p['control_hover']}; "
            f"border-color: {p['grid_strong']}; }}"
            f"QPushButton:focus {{ border-color: {p['mid']}; }}"
            f"QPushButton:disabled {{ color: {p['muted']}; background: transparent; }}"
        )

    def minimumSizeHint(self):
        width = self.fontMetrics().horizontalAdvance(self.text()) + 2 * self.HORIZONTAL_PADDING + 2
        if not self.icon().isNull():
            width += self.iconSize().width() + (4 if self.text() else 0)
        return QtCore.QSize(max(self.MIN_CONTENT_WIDTH, int(self._compact_width or 0), width), self.BUTTON_HEIGHT)

    def sizeHint(self):
        return self.minimumSizeHint()


class OrderBookControlBar(QtWidgets.QFrame):
    """One responsive strip for view, price step, units, and display options."""
    aggregation_selected = Signal(int)
    density_selected = Signal(str)
    value_mode_selected = Signal(str)
    book_depth_toggled = Signal(bool)
    display_option_toggled = Signal(str, bool)
    CONTROL_BAR_HEIGHT = 48
    _DENSITIES = ('compact', 'normal', 'relaxed')
    _DENSITY_NAMES = {'compact': 'Compact', 'normal': 'Comfortable', 'relaxed': 'Spacious'}

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = {}
        self._tick_size, self._aggregation = 0.0, 1
        self._book_depth, self._density, self._value_mode = False, 'normal', 'quote'
        self._tape_enabled, self._tape_mode = True, 'LARGE'
        self._responsive_layout_state = None
        self.setObjectName('orderBookControlBar')
        self.setFixedHeight(self.CONTROL_BAR_HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        root = QtWidgets.QHBoxLayout(self)
        root.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._layout = root

        self._view_container, view_layout = self._make_group()
        self._view_container.setObjectName('orderBookViewSegment')
        self._view_group = QtWidgets.QButtonGroup(self)
        self.ladder_button = self._toggle_button('Ladder', 'Price ladder with resting liquidity and optional analytics')
        self.depth_button = self._toggle_button('Depth', 'Price-aligned size bars and cumulative depth')
        self.ladder_button.setAccessibleName('Show price ladder')
        self.depth_button.setAccessibleName('Show liquidity depth')
        for button in (self.ladder_button, self.depth_button):
            self._view_group.addButton(button)
            view_layout.addWidget(button)
        self.depth_button.toggled.connect(self.book_depth_toggled.emit)
        root.addWidget(self._view_container)

        self.step_label = QtWidgets.QLabel('Step', self)
        set_text_role(self.step_label, TextRole.ORDERBOOK_LABEL)
        root.addWidget(self.step_label)
        self._aggregation_container, aggregation_layout = self._make_group()
        self._aggregation_buttons = {}
        self._aggregation_group = QtWidgets.QButtonGroup(self)
        self._aggregation_menu = QtWidgets.QMenu(self)
        self._aggregation_actions = {}
        self._aggregation_action_group = QtGui.QActionGroup(self)
        self._aggregation_action_group.setExclusive(True)
        for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
            value = int(multiplier)
            button = self._toggle_button(f'{value}×', f'{value}× exchange tick', compact_width=32)
            button.setAccessibleName(f'Price step {value} times the exchange tick')
            self._aggregation_buttons[value] = button
            self._aggregation_group.addButton(button)
            aggregation_layout.addWidget(button)
            button.clicked.connect(lambda checked=False, selected=value: self.aggregation_selected.emit(selected) if checked else None)
            action = self._aggregation_menu.addAction(f'{value}× exchange tick')
            action.setCheckable(True)
            self._aggregation_action_group.addAction(action)
            action.triggered.connect(lambda checked=False, selected=value: self.aggregation_selected.emit(selected) if checked else None)
            self._aggregation_actions[value] = action
        root.addWidget(self._aggregation_container)
        self.aggregation_button = self._plain_button('1× ▾', 'Choose the price step')
        self.aggregation_button.setAccessibleName('Choose price aggregation')
        self.aggregation_button.clicked.connect(lambda: self._popup(self._aggregation_menu, self.aggregation_button))
        root.addWidget(self.aggregation_button)
        root.addStretch(1)

        self.value_mode_button = self._plain_button('USDT', 'Switch between quote value and base quantity')
        self.value_mode_button.setAccessibleName('Switch order book value units')
        self.value_mode_button.clicked.connect(self._toggle_value_mode)
        root.addWidget(self.value_mode_button)
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
        self.density_button.clicked.connect(self._show_density_menu)
        root.addWidget(self.density_button)

        self._display_menu = QtWidgets.QMenu(self)
        self._display_menu.addSection('Display')
        self._display_menu.addMenu(self._density_menu)
        self.units_action = self._display_menu.addAction('Show base-asset quantities')
        self.units_action.setCheckable(True)
        self.units_action.triggered.connect(lambda checked: self.value_mode_selected.emit('base' if checked else 'quote'))
        self._display_menu.addSeparator()
        self._display_menu.addSection('Price-level analytics')
        self._lane_actions = {}
        for key, label in (('flow', 'Aggressor flow · 5s'), ('delta', 'Liquidity change · 5s'), ('state', 'Liquidity signals'), ('memory', 'Liquidity history · 30s')):
            action = self._display_menu.addAction(label)
            action.setCheckable(True)
            action.toggled.connect(lambda enabled, lane=key: self.display_option_toggled.emit(lane, enabled))
            self._lane_actions[key] = action
        self._display_menu.addSeparator()
        self.tape_action = self._display_menu.addAction('Show trade tape when space allows')
        self.tape_action.setCheckable(True)
        self.tape_action.toggled.connect(lambda enabled: self.display_option_toggled.emit('tape', enabled))
        self.settings_button = self._plain_button('Display ▾', 'Analytics, units, row spacing, and trade tape')
        self.settings_button.setAccessibleName('Order book display options')
        self.settings_button.clicked.connect(lambda: self._popup(self._display_menu, self.settings_button))
        root.addWidget(self.settings_button)
        self.apply_theme(theme)
        self._refresh_typography()
        typography_controller().changed.connect(self._refresh_typography)
        self._refresh_text()
        self._refresh_responsive_layout(self.width())

    def _make_group(self):
        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
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

    def _show_density_menu(self):
        self._popup(self._density_menu, self.density_button)

    def _toggle_value_mode(self):
        self.value_mode_selected.emit('base' if self._value_mode == 'quote' else 'quote')


    def _refresh_typography(self):
        for button in self.findChildren(_OrderBookSurfaceButton):
            button.setFont(typography_font(TextRole.ORDERBOOK_CONTROL))
            button.updateGeometry()
        self._responsive_layout_state = None
        self._refresh_responsive_layout(self.width())

    def apply_theme(self, theme):
        p = ORDERBOOK_REFERENCE
        self.setStyleSheet(
            f"QFrame#orderBookControlBar {{ background: {p['control']}; border: 0; border-bottom: 1px solid {p['grid']}; }}"
            f"QWidget#orderBookViewSegment {{ background: {p['bg']}; border-radius: 7px; }}"
            f"QLabel {{ color: {p['muted']}; background: transparent; border: 0; }}"
        )
        for button in self.findChildren(_OrderBookSurfaceButton):
            button.set_theme({})
        for menu in self.findChildren(QtWidgets.QMenu):
            menu.setStyleSheet(
                f"QMenu {{ background: {p['surface_raised']}; color: {p['text']}; border: 1px solid {p['grid_strong']}; padding: 6px; }}"
                f"QMenu::item {{ padding: 7px 24px 7px 16px; border-radius: 4px; }}"
                f"QMenu::item:selected {{ background: {p['control_hover']}; }}"
                f"QMenu::separator {{ height: 1px; background: {p['grid']}; margin: 6px 8px; }}"
            )


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_responsive_layout(event.size().width())

    def _refresh_responsive_layout(self, width):
        full_width = self._aggregation_container.sizeHint().width()
        full_steps = width >= max(620, full_width + 420)
        state = (full_steps, width >= 480, width >= 350, width >= 550)
        if state == self._responsive_layout_state:
            return
        self._responsive_layout_state = state
        self._aggregation_container.setVisible(full_steps)
        self.aggregation_button.setVisible(not full_steps)
        self.step_label.setVisible(width >= 350)
        self.density_button.setVisible(width >= 480)
        self.value_mode_button.setVisible(width >= 350)
        self.settings_button.setText('Display ▾' if width >= 550 else '···')
        self._layout.setSpacing(6 if width >= 350 else 3)
        self._layout.setContentsMargins(8 if width >= 300 else 4, 8, 8 if width >= 300 else 4, 8)

    @staticmethod
    def _set_checked_without_signal(button, checked):
        blocker = QtCore.QSignalBlocker(button)
        button.setChecked(bool(checked))
        del blocker

    def set_state(self, *, aggregation, tick_size, preset, density, value_mode, tape_enabled, tape_mode, book_depth=False, overlays=None):
        self._aggregation = max(1, int(aggregation))
        self._tick_size = max(0.0, float(tick_size))
        self._density, self._value_mode = str(density or 'normal'), 'base' if value_mode == 'base' else 'quote'
        self._tape_enabled, self._tape_mode, self._book_depth = bool(tape_enabled), str(tape_mode), bool(book_depth)
        for key, action in self._lane_actions.items():
            self._set_checked_without_signal(action, bool((overlays or {}).get(key, True)))
            action.setEnabled(not self._book_depth)
        self._refresh_text()

    def _refresh_text(self):
        # Block both sides of an exclusive group: selecting one can uncheck the
        # other, and state restoration must never emit a user action.
        blockers = [QtCore.QSignalBlocker(button) for button in (self.ladder_button, self.depth_button)]
        self.ladder_button.setChecked(not self._book_depth)
        self.depth_button.setChecked(self._book_depth)
        del blockers
        blockers = [QtCore.QSignalBlocker(button) for button in self._aggregation_buttons.values()]
        for multiplier, button in self._aggregation_buttons.items():
            button.setChecked(multiplier == self._aggregation)
            effective = format_book_price(self._tick_size * multiplier) if self._tick_size else 'exchange tick'
            tip = f'Price step: {effective} ({multiplier}× exchange tick)'
            if multiplier > 1:
                tip += '\nGrouped prices are for analysis; use 1× for exact price selection.'
            button.setToolTip(tip)
            action = self._aggregation_actions[multiplier]
            self._set_checked_without_signal(action, multiplier == self._aggregation)
            action.setText(f'{multiplier}× · {effective}')
        del blockers
        self.aggregation_button.setText(f'{self._aggregation}× ▾')
        self.value_mode_button.setText('Qty' if self._value_mode == 'base' else 'USDT')
        self.value_mode_button.setToolTip('Base-asset quantity · click for USDT value' if self._value_mode == 'base' else 'USDT value · click for base-asset quantity')
        self._set_checked_without_signal(self.units_action, self._value_mode == 'base')
        self._set_checked_without_signal(self.tape_action, self._tape_enabled)
        for key, action in self._density_actions.items():
            self._set_checked_without_signal(action, key == self._density)
        self.density_button.setToolTip(f'Row spacing: {self._DENSITY_NAMES.get(self._density, "Comfortable")}')

import math
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, replace
from typing import ClassVar
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal
from ..models import DomExecutionContext, DomPositionOverlay, OrderFlowPresentationFrame
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


@dataclass(frozen=True, slots=True)
class DomAccountMarker:
    """One exact account/trading price before it is mapped onto display rows."""
    exact_price: float
    side: str
    role: str
    color: QtGui.QColor
    quantity: float = 0.0
    source: str = ''

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

def _compute_order_flow_dom_geometry(width: float, height: float, font_height: float=13.0, price_font_height: float | None=None, label_font_height: float | None=None, *, execution_active: bool=False, column_preferences: dict[str, object] | None=None, row_density: str='normal', presentation_preset: str='execution', essential_only: bool=False, previous_mode: str | None=None, previous_bbo_only: bool | None=None, price_min_width: float=90.0, amount_min_width: float=36.0, state_expanded_width: float | None=None, column_width_overrides: dict[str, float] | None=None, book_depth: bool=False) -> dict[str, object]:
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
    execution_height = max(24.0, float(math.ceil(label_h + 8.0))) if execution_active else 0.0
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
        execution_height = 0.0
        column_height = min(column_height, height * 0.20)
        center_height = min(center_height, height * 0.40)
        footer_height = 0.0
    top_height = title_height + metric_height
    execution_top = margin + top_height
    column_top = execution_top + execution_height
    table_top = column_top + column_height

    # The bottom edge is part of the rendered surface, not an unused outer
    # margin. Splitter heights are arbitrary pixel values, while a symmetric DOM
    # consumes rows in bid/ask pairs. Using a fixed row height therefore leaves
    # a remainder for almost every manual resize. Treat density as the nominal
    # (minimum) spacing, choose the number of complete pairs that fit, then
    # distribute the remainder uniformly across those rows. This makes the DOM
    # consume every vertical pixel without a dead strip and keeps both sides
    # perfectly aligned around the center band.
    fixed = margin + top_height + execution_height + column_height + center_height + footer_height
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
        'execution_height': execution_height, 'column_height': column_height,
        'row_height': row_height, 'nominal_row_height': nominal_row_height, 'center_height': center_height, 'footer_height': footer_height,
        'rows_per_side': rows_per_side, 'header_top': margin, 'execution_top': execution_top,
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
        self._book_depth = False
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
        self._value_mode = 'quote'
        self._market_signal: dict[str, object] | None = None
        self._last_layout_state: dict[str, object] = {}
        self._row_change_cues: dict[tuple[str, int | float], tuple[str, float, float]] = {}
        self._account_marker_side_latches: dict[tuple[object, ...], dict[str, object]] = {}
        self._execution_band_active_until = 0.0
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
        self.execution_context = DomExecutionContext(self.symbol)
        self._execution_text: list[tuple[str, QtGui.QColor]] = []
        self._execution_text_compact: list[tuple[str, QtGui.QColor]] = []
        self._execution_markers: tuple[DomAccountMarker, ...] = ()
        self._mapped_account_markers: dict[int, tuple[DomAccountMarker, ...]] = {}
        self._offscreen_account_markers: dict[str, tuple[DomAccountMarker, ...]] = {'above': (), 'below': ()}
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
        self._geometry = _compute_order_flow_dom_geometry(360.0, 520.0)
        self._prepared_sequence = -1
        self._prepared_size = (-1, -1)
        self._header_text: dict[str, tuple[str, QtGui.QColor]] = {}
        self._metric_items: list[tuple[str, str, QtGui.QColor]] = []
        self._footer_items: list[tuple[str, str, QtGui.QColor]] = []
        self._prepared_header_layout: dict[str, object] = {}
        self._footer_depth_summary = (0.5, '—', '—', False)
        self._prepared_column_labels: dict[str, str] = {}
        self._prepared_metric_draw_items: tuple[tuple[str, str, QtGui.QColor], ...] = ()
        self._prepared_footer_draw_items: tuple[tuple[str, QtGui.QColor], ...] = ()
        self._typography_ready = False
        self._theme_cache_ready = False
        self._freshness_text = 'Waiting for market data'
        self._market_status = 'CONNECTING'
        self._header_tooltip = 'Order-flow summary'
        self._center_tooltip = 'Current market price'
        self._footer_tooltip = 'Recent liquidity activity'
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
        self._latency_summary_cache = ''
        self._latency_summary_cache_at = 0.0
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
        # Amount-lane measurement is expensive (formatting + QFontMetrics). Cache
        # against the immutable level tuples so BBO/trade-only snapshots do not
        # remeasure up to 128 unchanged rows on the GUI thread.
        self._amount_width_cache_bid_levels: tuple[OrderFlowDisplayLevel, ...] | None = None
        self._amount_width_cache_ask_levels: tuple[OrderFlowDisplayLevel, ...] | None = None
        self._amount_width_cache_mode = ''
        self._amount_width_cache_font_key = ''
        self._amount_width_cache_value = 36.0
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
        self._execution_band_timer = QTimer(self)
        self._execution_band_timer.setSingleShot(True)
        self._execution_band_timer.timeout.connect(self._expire_execution_band)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self._refresh_typography()
        self._refresh_theme_cache()
        typography_controller().changed.connect(self._refresh_typography)

    def _refresh_typography(self) -> None:
        # Baseline typography is owned entirely by utilities.py. Density changes
        # vertical spacing only; the exact price font may still shrink to fit.
        self._row_font = typography_font(TextRole.ORDERBOOK_VALUE)
        self._price_font = typography_font(TextRole.ORDERBOOK_PRICE)
        self._metric_font = typography_font(TextRole.ORDERBOOK_METRIC)
        self._symbol_font = typography_font(TextRole.ORDERBOOK_SYMBOL)
        self._label_font = typography_font(TextRole.ORDERBOOK_LABEL)
        self._footer_font = typography_font(TextRole.ORDERBOOK_FOOTER_VALUE)
        self._center_price_font = typography_font(TextRole.ORDERBOOK_CENTER_PRICE)
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
        self._amount_width_cache_bid_levels = None
        self._amount_width_cache_ask_levels = None
        self._amount_width_cache_mode = ''
        self._amount_width_cache_font_key = ''
        self._amount_width_cache_value = 36.0
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._typography_ready = True
        if self._theme_cache_ready:
            self._prepare_display()
            self.update()

    @staticmethod
    def _contrast_text_color(color: QtGui.QColor) -> QtGui.QColor:
        r, g, b, _a = color.getRgb()
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return QtGui.QColor('#050506' if luminance >= 142.0 else '#F2F5F7')

    def _refresh_profile_bar_palette(self, theme: dict[str, object] | None) -> None:
        # Both presentation modes share the same restrained directional palette.
        # Applications can still call this theme hook without changing its API.
        del theme
        self._profile_colors = {
            'bid': QtGui.QColor(ORDERBOOK_REFERENCE['bid']),
            'ask': QtGui.QColor(ORDERBOOK_REFERENCE['ask']),
        }
        self._profile_bar_colors = {
            'bid': QtGui.QColor(ORDERBOOK_REFERENCE['bid_fill']),
            'ask': QtGui.QColor(ORDERBOOK_REFERENCE['ask_fill']),
        }
        # Keep DEPTH caps/outline crisp while pushing the large bar bodies one
        # visual step behind price and the cumulative-depth staircase.
        for color in self._profile_bar_colors.values():
            color.setAlpha(205)
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
            area.setAlpha(42 if side == 'ask' else 27)
            pen_color = QtGui.QColor(line)
            pen_color.setAlpha(170)
            pen = QtGui.QPen(pen_color)
            pen.setCosmetic(True)
            pen.setWidthF(1.15)

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
            self._profile_bar_text[side] = self._contrast_text_color(self._profile_bar_colors[side])
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
        self._execution_entry = QtGui.QColor(p['bid'])
        self._execution_tp = QtGui.QColor(p['bid'])
        self._execution_sl = QtGui.QColor(p['amber'])
        self._execution_liq = QtGui.QColor(p['ask'])
        self._execution_neutral = QtGui.QColor(p['text'])
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
        self._prepare_execution_display()
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


    def set_execution_context(self, context: DomExecutionContext | None) -> None:
        if context is None:
            context = DomExecutionContext(self.symbol)
        if context.symbol and context.symbol != self.symbol:
            return
        if context == self.execution_context:
            return
        had_context = bool(self.execution_context.positions or self.execution_context.orders)
        has_context = bool(context.positions or context.orders)
        self.execution_context = context
        if has_context:
            self._execution_band_active_until = 0.0
            self._execution_band_timer.stop()
        elif had_context:
            self._execution_band_active_until = time.monotonic() + 2.5
            self._execution_band_timer.start(2500)
        self._prepare_execution_display()
        self._prepared_sequence = -1
        self._prepare_display()
        self.update()

    def set_mark_price(self, mark: float) -> None:
        mark = safe_float(mark)
        if mark <= 0.0 or not self.execution_context.positions:
            return
        changed = False
        positions: list[DomPositionOverlay] = []
        for position in self.execution_context.positions:
            pnl = position.unrealized_pnl
            if position.entry_price > 0.0 and position.quantity > 0.0:
                signed = -position.quantity if position.side == 'SHORT' else position.quantity
                pnl = (mark - position.entry_price) * signed
            updated = replace(position, mark_price=mark, unrealized_pnl=pnl)
            positions.append(updated)
            changed = changed or updated != position
        if not changed:
            return
        self.execution_context = replace(self.execution_context, positions=tuple(positions))
        self._prepare_execution_display()
        top = int(float(self._geometry.get('execution_top', 0.0)))
        height = int(math.ceil(float(self._geometry.get('execution_height', 20.0)))) + 2
        if height > 2:
            self.update(0, max(0, top - 1), self.width(), max(1, height))

    def _prepare_execution_display(self) -> None:
        context = self.execution_context
        position_parts: list[str] = []
        compact_position_parts: list[str] = []
        position_color = self._muted
        for position in context.positions[:2]:
            side_short = 'L' if position.side == 'LONG' else 'S'
            entry = self._price_text(position.entry_price)
            qty = human_number(position.quantity)
            pnl = self._signed_money(position.unrealized_pnl)
            leverage = f' · {position.leverage}×' if position.leverage > 0 else ''
            liquidation = f' · LIQ {self._price_text(position.liquidation_price)}' if position.liquidation_price > 0.0 else ''
            position_parts.append(f'{side_short} {qty} @ {entry} · PNL {pnl}{leverage}{liquidation}')
            compact_position_parts.append(f'{side_short} {qty} @ {entry} · {pnl}')
            if abs(position.unrealized_pnl) > 0.005:
                position_color = self._bid if position.unrealized_pnl > 0.0 else self._ask
            elif position_color == self._muted:
                position_color = self._bid if position.side == 'LONG' else self._ask
        if len(context.positions) > 2:
            hidden = len(context.positions) - 2
            position_parts.append(f'+{hidden} POS')
            compact_position_parts.append(f'+{hidden}')
        if not position_parts:
            position_parts = ['FLAT']
            compact_position_parts = ['FLAT']
        if len(context.positions) > 1:
            position_color = self._text
        tp_count = sum((1 for order in context.orders if order.label == 'TP'))
        sl_count = sum((1 for order in context.orders if order.label == 'SL'))
        exit_count = sum((1 for order in context.orders if order.reduce_only))
        order_text = f'ORD {len(context.orders)}'
        if tp_count:
            order_text += f' · TP {tp_count}'
        if sl_count:
            order_text += f' · SL {sl_count}'
        if exit_count and (not (tp_count or sl_count)):
            order_text += f' · EXIT {exit_count}'
        order_color = self._text if context.orders else self._muted
        self._execution_text = [(' | '.join(position_parts), position_color), (order_text, order_color)]
        compact_order_text = f'O{len(context.orders)}'
        if tp_count:
            compact_order_text += f' · T{tp_count}'
        if sl_count:
            compact_order_text += f' · S{sl_count}'
        self._execution_text_compact = [(' | '.join(compact_position_parts), position_color), (compact_order_text, order_color)]
        markers: list[DomAccountMarker] = []
        for position in context.positions:
            if position.entry_price > 0.0:
                markers.append(DomAccountMarker(exact_price=position.entry_price, side=position.side, role='ENTRY', color=QtGui.QColor(self._execution_entry), quantity=max(0.0, position.quantity), source='POSITION'))
            if position.liquidation_price > 0.0:
                markers.append(DomAccountMarker(exact_price=position.liquidation_price, side=position.side, role='LIQ', color=QtGui.QColor(self._execution_liq), quantity=max(0.0, position.quantity), source='POSITION'))
        for order in context.orders:
            if order.price <= 0.0:
                continue
            if order.label == 'TP':
                color = self._execution_tp
            elif order.label == 'SL':
                color = self._execution_sl
            elif order.reduce_only:
                color = self._execution_sl
            else:
                color = self._execution_neutral
            markers.append(DomAccountMarker(exact_price=order.price, side=order.side, role=order.label, color=QtGui.QColor(color), quantity=max(0.0, order.quantity), source=order.source))
        self._execution_markers = tuple(markers)
        self._rebuild_account_marker_mapping()

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
        self.reset()
        self.execution_context = DomExecutionContext(self.symbol)
        self._prepare_execution_display()

    def set_price_tick_size(self, tick_size: float) -> None:
        self.price_tick_size = max(0.0, safe_float(tick_size))
        self._invalidate_aggregation()
        self._price_envelope_integer_digits = 0
        self._price_envelope_text = '0'
        if self.price_tick_size > 0.0:
            self.price_decimals = _decimal_places_from_step(self.price_tick_size)
        else:
            self.price_decimals = 0
        self._prepare_execution_display()
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
        except (TypeError, ValueError):
            resolved = 1
        if resolved not in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
            resolved = 1
        if resolved == self.aggregation_multiplier:
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

    def _bucket_price_for_multiplier(self, price: float, side: str, multiplier: int) -> float:
        if multiplier <= 1 or self.price_tick_size <= 0.0:
            return float(price)
        raw_tick = int(round(float(price) / self.price_tick_size))
        if side == 'bid':
            display_tick = raw_tick // multiplier * multiplier
        else:
            display_tick = (raw_tick + multiplier - 1) // multiplier * multiplier
        return display_tick * self.price_tick_size

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
        rows = max(0, int(rows_per_side))
        fixed_height = (
            float(geometry.get('margin', 0.0))
            + float(geometry.get('top_height', 0.0))
            + float(geometry.get('execution_height', 0.0))
            + float(geometry.get('column_height', 0.0))
            + float(geometry.get('center_height', 0.0))
            + float(geometry.get('footer_height', 0.0))
        )
        # Manual splitter resizing may stretch the effective row height to absorb
        # a pixel remainder. Density-driven panel resizing must use the nominal
        # spacing or repeated density changes would compound that stretch.
        row_height = max(1.0, float(geometry.get('nominal_row_height', geometry.get('row_height', 1.0))))
        return max(1, int(math.ceil(fixed_height + rows * row_height * 2.0)))

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
        self._amount_width_cache_bid_levels = None
        self._amount_width_cache_ask_levels = None
        self._amount_width_cache_mode = ''
        self._prepared_sequence = -1
        self._prepare_display(reuse_rows=False)
        self.update()
        if emit:
            self.presentation_changed.emit(self.presentation_state())

    def presentation_state(self) -> dict[str, object]:
        return {'preset': self._presentation_preset, 'book_depth': self._book_depth, 'density': self._row_density, 'values': self._value_mode, 'column_widths': self.column_width_state(), 'columns': self.column_preferences()}

    def set_book_depth_enabled(self, enabled: bool, *, emit: bool=True) -> None:
        enabled = bool(enabled)
        if enabled == self._book_depth:
            return
        if self._column_resize_active:
            self._finish_column_resize()
        self._book_depth = enabled
        if not enabled:
            self._reset_profile_animation()
        self._geometry_cache_key = None
        self._prepared_sequence = -1
        self._hover_price = 0.0
        self._hover_context = ''
        self.setToolTip('')
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
        return {'mode': str(self._geometry.get('mode', 'unknown')), 'columns': tuple(columns), 'semantic_columns': tuple(sorted(semantic_columns)), 'primary': str(self._geometry.get('primary_analytic', self._column_preferences.get('primary', 'flow'))), 'visible_analytics': tuple((name for name in ('flow', 'delta', 'memory') if name in semantic_columns)), 'bbo_only': bool(self._geometry.get('bbo_only', False)), 'density': self._row_density, 'preset': self._presentation_preset, 'values': self._value_mode}

    def _publish_layout_state(self) -> None:
        state = self.layout_state()
        if state != self._last_layout_state:
            self._last_layout_state = state
            self.layout_state_changed.emit(dict(state))

    def _reset_visual_scales(self) -> None:
        for key in self._visual_scales:
            self._visual_scales[key] = 0.0

    def reset(self, *, preserve_execution: bool=False) -> None:
        self._snapshot_prepare_timer.stop()
        self._freshness_timer.stop()
        self._market_signal_expiry_timer.stop()
        self._pending_source_snapshot = None
        self.source_snapshot = None
        self._applied_source_snapshot = None
        self.snapshot = None
        self._invalidate_aggregation()
        self._latest_received_sequence = -1
        self._latest_applied_sequence = -1
        self._last_painted_sequence = -1
        self._last_painted_depth_ingress_id = -1
        self._last_pipe_sample_at = 0.0
        self._latency_summary_cache = ''
        self._latency_summary_cache_at = 0.0
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
        if not preserve_execution:
            self.execution_context = DomExecutionContext(self.symbol)
            self._execution_text = [('FLAT', self._muted), ('ORD 0', self._muted)]
            self._execution_text_compact = [('FLAT', self._muted), ('O0', self._muted)]
            self._execution_markers = ()
        self._mapped_account_markers.clear()
        self._offscreen_account_markers = {'above': (), 'below': ()}
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
        if self._book_depth:
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
        if self._value_mode == 'quote':
            return self._signed_money(notional) if signed else self._money(notional)
        base = float(quantity) if quantity is not None else notional / price if price > 0.0 else 0.0
        if signed:
            return self._signed_quantity(base)
        return self._compact_scalar(base, money=False) if abs(base) > 1e-12 else '—'

    @staticmethod
    def _signed_percent(value: float) -> str:
        if not math.isfinite(value) or abs(value) < 0.05:
            return '0%'
        return f'{value:+.0f}%'

    def _price_text(self, value: float) -> str:
        if value <= 0.0 or not math.isfinite(value):
            return '—'
        decimals = self.price_decimals if self.price_tick_size > 0.0 else None
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
                self.aggregation_multiplier, self.price_tick_size)

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
            )
        return signature

    def _snapshot_chrome_signature(self) -> tuple[tuple[object, ...], ...]:
        """Return independently-diffable signatures for painted chrome bands.

        The old signature treated title/status, metrics, center/BBO and footer as
        one unit. Fast-moving metrics therefore repainted every chrome band even
        when those other bands were byte-for-byte unchanged. Keep the state
        grouped by the regions the painter can update independently.
        """
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
        if previous is not None and prepared_size_matches:
            if (
                display_snapshot.bid_levels is previous.bid_levels
                and display_snapshot.ask_levels is previous.ask_levels
            ):
                rows_unchanged = True
            else:
                # Snapshot tuples are immutable but partial analytical refreshes
                # may create new tuple objects even when the rows currently
                # visible in the DOM are value-identical. Avoid rebuilding
                # those rows merely because container identity changed.
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

    def _pipeline_latency_summary_text(self) -> str:
        now = time.monotonic()
        if self._latency_summary_cache_at > 0.0 and now - self._latency_summary_cache_at < self.LATENCY_DISPLAY_INTERVAL_SECONDS:
            return self._latency_summary_cache
        labels = (
            ('socket_to_parser', 'socket→parser'),
            ('parser_to_gui_dispatch', 'parser→GUI dispatch'),
            ('gui_dispatch_to_main', 'GUI ingress queue'),
            ('parser_to_main', 'parser→main'),
            ('main_depth_handler', 'main depth handler'),
            ('main_to_worker', 'worker queue'),
            ('worker_depth_process', 'depth analysis'),
            ('worker_wait', 'snapshot pacing wait'),
            ('snapshot_build', 'snapshot build'),
            ('worker_to_gui', 'GUI delivery'),
            ('dom_queue', 'DOM queue'),
            ('aggregation', 'display aggregation'),
            ('prepare', 'prepare total'),
            ('prepare_to_paint', 'paint wait'),
            ('paint', 'paint'),
            ('dom_to_paint', 'DOM receive→rows'),
            ('socket_to_paint', 'socket→rows'),
        )
        lines = []
        for key, label in labels:
            samples = self._latency_stage_samples.get(key)
            if not samples:
                continue
            lines.append(
                f"{label}: p50 {self._latency_ms_text(self._percentile(samples, 0.50))}, "
                f"p95 {self._latency_ms_text(self._percentile(samples, 0.95))}, "
                f"p99 {self._latency_ms_text(self._percentile(samples, 0.99))}, "
                f"MAX {self._latency_ms_text(self._latency_stage_max_ms.get(key, 0.0))}"
            )
        self._latency_summary_cache = '\n'.join(lines)
        self._latency_summary_cache_at = now
        return self._latency_summary_cache

    def _fitted_price_text(self, value: float, metrics: QtGui.QFontMetricsF, width: float) -> str:
        del metrics, width
        return self._price_text(value)

    def _required_price_lane_width(self, snapshot: OrderFlowSnapshot | None) -> float:
        """Width that preserves every exact price digit at the readability floor."""
        widest_text = self._update_price_format_envelope(snapshot)
        font = typography_font_at_pixel_size(
            self._price_font, typography_min_pixel_size(TextRole.ORDERBOOK_PRICE)
        )
        required = self._price_metrics_for_font(font).horizontalAdvance(widest_text) + (16.0 if self._book_depth else 4.0)
        return max(90.0, float(math.ceil(required)))

    def _required_amount_lane_width(self, snapshot: OrderFlowSnapshot | None) -> float:
        if snapshot is None or self._book_depth:
            return 36.0
        if (
            snapshot.bid_levels is self._amount_width_cache_bid_levels
            and snapshot.ask_levels is self._amount_width_cache_ask_levels
            and self._amount_width_cache_mode == self._value_mode
            and self._amount_width_cache_font_key == self._row_font_key
        ):
            return self._amount_width_cache_value
        texts = [
            self._row_amount(level.notional, level.price, quantity=level.quantity)
            for level in (*snapshot.bid_levels[:64], *snapshot.ask_levels[:64])
        ]
        widest = max((self._row_metrics.horizontalAdvance(text) for text in texts), default=0.0)
        measured = max(36.0, min(112.0, float(math.ceil(widest + 7.0))))
        self._amount_width_cache_bid_levels = snapshot.bid_levels
        self._amount_width_cache_ask_levels = snapshot.ask_levels
        self._amount_width_cache_mode = self._value_mode
        self._amount_width_cache_font_key = self._row_font_key
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
        price_padding = 16.0 if self._book_depth else 4.0
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

    def _draw_row_line_around_state(
        self,
        painter: QtGui.QPainter,
        left: float,
        y: float,
        right: float,
    ) -> None:
        """Draw a moving row marker without crossing the STATE text lane."""
        columns = self._geometry.get('columns', {})
        state_bounds = columns.get('state') if isinstance(columns, dict) else None
        if not (
            isinstance(state_bounds, tuple)
            and len(state_bounds) == 2
            and float(state_bounds[1]) > float(state_bounds[0])
        ):
            self._draw_snapped_line(painter, left, y, right, y)
            return

        state_left = max(float(left), float(state_bounds[0]))
        state_right = min(float(right), float(state_bounds[1]))
        if state_left > float(left):
            self._draw_snapped_line(painter, left, y, state_left, y)
        if state_right < float(right):
            self._draw_snapped_line(painter, state_right, y, right, y)

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

    def _refresh_geometry_for_size(self, width: int, height: int, snapshot: OrderFlowSnapshot | None, execution_active: bool) -> bool:
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
            execution_active,
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
                execution_active=execution_active,
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
                execution_active=execution_active,
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
        execution_active = bool(self.execution_context.positions or self.execution_context.orders or time.monotonic() < self._execution_band_active_until)
        snapshot = self.snapshot
        previous_rows_per_side = int(self._geometry.get('rows_per_side', 0))
        geometry_changed = self._refresh_geometry_for_size(width, height, snapshot, execution_active)
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
            self._header_tooltip = f'Order-flow status: {status_text}\n{source_text}'
            self._center_tooltip = 'Current market price is not available yet'
            self._footer_tooltip = 'Recent liquidity activity is not available yet'
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
        health_line = f'\nHealth: {health_reason}' if health_reason else ''
        pipeline_summary = self._pipeline_latency_summary_text()
        pipeline_line = f'\nPipeline rolling stages:\n{pipeline_summary}' if pipeline_summary else ''
        self._header_tooltip = f'Order-flow summary\nStatus: {status_text}{health_line}\n{latency_text} (current latency / rolling 10 s maximum; PIPE = last measured depth ingress){pipeline_line}\nSource: {source}\n{freshness_parts[0]}, {freshness_parts[1]}, {freshness_parts[2]}\nMicroprice bias: {snapshot.microprice_bias_bps:+.2f} bp\nTouch imbalance: {self._signed_percent(snapshot.touch_imbalance_pct)}\nNear-touch pressure (context): {self._signed_percent(snapshot.near_pressure_pct)}\nAggressor imbalance (5s): {self._signed_percent(snapshot.aggressor_imbalance_5s_pct)}\nRPI share (context, 5s): {snapshot.rpi_share_5s_pct:.0f}%\nDisplay aggregation: {self.aggregation_multiplier}× tick'
        self._center_tooltip = f'Midpoint: {center_text}\nBest bid: {self._price_text(snapshot.best_bid)}\nBest ask: {self._price_text(snapshot.best_ask)}\nSpread: {snapshot.spread_bps:.2f} bp'
        self._footer_tooltip = f'Liquidity activity\nAdded (5s): {self._money(snapshot.added_notional_5s)}\nCanceled (5s): {self._money(snapshot.cancelled_notional_5s)}\nReplenished (5s): {self._money(snapshot.replenished_notional_5s)}\nTaker buy / sell (1s): {self._money(snapshot.buy_notional_1s)} / {self._money(snapshot.sell_notional_1s)}'
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
        screen = self.screen()
        refresh = float(screen.refreshRate()) if screen is not None else 60.0
        if not math.isfinite(refresh) or refresh < 30.0:
            refresh = 60.0
        # Reference cadence: 144 Hz -> ~7 ms, 240 Hz -> ~4 ms.
        return max(4, min(16, int(round(1000.0 / refresh))))

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
        amount_right = min(float(lane[1]) - 2.0, float(lane[0]) + self._profile_amount_width)
        left, right = amount_right + 2.0, float(lane[1]) - 8.0
        plot_width = max(0.0, right - left)
        sides = {'bid': self._bid_rows[:limit], 'ask': self._ask_rows[:limit]}
        for side, rows in sides.items():
            if not rows:
                continue
            y = float(g['center_bottom'] if side == 'bid' else g['center_top'])
            direction = 1.0 if side == 'bid' else -1.0
            outline = QtGui.QPainterPath(QtCore.QPointF(left, y))
            for row in rows:
                key = (side, float(row.level.price))
                depth = self._profile_depth_current.get(key, row.profile_depth)
                x = left + plot_width * max(0.0, min(1.0, depth))
                outline.lineTo(x, y)
                y += direction * float(g['row_height'])
                outline.lineTo(x, y)
            area = QtGui.QPainterPath(outline)
            area.lineTo(left, y)
            area.closeSubpath()
            self._profile_paths[side] = (area, outline)

    def _prepare_liquidity_profile(self) -> None:
        """Prepare current depth targets and animate only their visual presentation.

        Size and cumulative depth keep the current linear, shared bid/ask scales.
        The reference smoothing function is applied after normalization, so no
        exchange value, analytical field, displayed quantity or hit price is delayed.
        """
        g = self._geometry
        limit = int(g['rows_per_side'])
        if self.snapshot is None or not self.snapshot.ready:
            limit = 0
        sides = {'bid': self._bid_rows[:limit], 'ask': self._ask_rows[:limit]}
        amounts: dict[str, list[tuple[float, float]]] = {}
        largest = 0.0
        totals: dict[str, float] = {}
        for side, rows in sides.items():
            cumulative = 0.0
            values = []
            for row in rows:
                level = row.level
                amount = max(0.0, level.quantity if self._value_mode == 'base' else level.notional)
                cumulative = cumulative + amount if self._value_mode == 'base' else max(0.0, level.cumulative_depth_notional)
                largest = max(largest, amount)
                values.append((amount, cumulative))
            amounts[side] = values
            totals[side] = cumulative
        largest_depth = max(totals.values(), default=0.0)
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
        self._profile_amount_width = max(58.0, min(118.0, measured_amount + 14.0))
        self._profile_scale_text = (
            f'Visible levels only · {unit}\n'
            f'Solid bars: size at price; full width = {self._compact_scalar(largest)} {unit}\n'
            f'Stepped outline: cumulative depth from the touch; full width = {self._compact_scalar(largest_depth)} {unit}\n'
            'Both sides share the same scale for each series.'
        )

        size_targets: dict[tuple[str, float], float] = {}
        depth_targets: dict[tuple[str, float], float] = {}
        for side, rows in sides.items():
            previous = 0.0
            for row, (amount, cumulative) in zip(rows, amounts[side]):
                row.profile_size = amount / largest if largest > 0.0 else 0.0
                row.profile_depth = cumulative / largest_depth if largest_depth > 0.0 else 0.0
                row.profile_previous_depth = previous
                previous = row.profile_depth
                key = (side, float(row.level.price))
                size_targets[key] = row.profile_size
                depth_targets[key] = row.profile_depth

        self._set_profile_animation_targets(size_targets, depth_targets)

    def _rebuild_hit_rows(self) -> None:
        if self._book_depth:
            self._prepare_liquidity_profile()
        hit_rows = []
        self._prepared_display_states.clear()
        self._mapped_account_markers.clear()
        self._offscreen_account_markers = {'above': (), 'below': ()}
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
        self._rebuild_account_marker_mapping()

    def _commit_geometry_for_current_size(self) -> None:
        if not hasattr(self, '_row_metrics'):
            return
        width = max(1, self.width())
        height = max(1, self.height())
        old_rows = int(self._geometry.get('rows_per_side', 0))
        execution_active = bool(self.execution_context.positions or self.execution_context.orders or time.monotonic() < self._execution_band_active_until)
        changed = self._refresh_geometry_for_size(width, height, self.snapshot, execution_active)
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

    def _expire_execution_band(self) -> None:
        if self.execution_context.positions or self.execution_context.orders:
            return
        if time.monotonic() < self._execution_band_active_until:
            remaining = max(1, int(math.ceil((self._execution_band_active_until - time.monotonic()) * 1000.0)))
            self._execution_band_timer.start(remaining)
            return
        self._execution_band_active_until = 0.0
        self._geometry_cache_key = None
        self._prepare_display()
        self.update()

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


    def _draw_execution_band(self, painter: QtGui.QPainter, bounds: QtCore.QRectF) -> None:
        top = float(self._geometry['execution_top'])
        height = float(self._geometry['execution_height'])
        if height <= 0.0:
            return
        rect = QtCore.QRectF(bounds.left(), top, bounds.width(), height)
        painter.fillRect(rect, self._surface_top)
        text_source = self._execution_text_compact if rect.width() < 300.0 else self._execution_text
        position_text, position_color = text_source[0] if text_source else ('FLAT', self._muted)
        order_text, order_color = text_source[1] if len(text_source) > 1 else ('ORD 0', self._muted)
        split = 0.72
        self._draw_text(painter, QtCore.QRectF(rect.left(), rect.top(), rect.width() * split, rect.height()), position_text, position_color, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=2.0)
        self._draw_text(painter, QtCore.QRectF(rect.left() + rect.width() * split, rect.top(), rect.width() * (1.0 - split), rect.height()), order_text, order_color, Qt.AlignmentFlag.AlignRight, font=self._label_font, pad=2.0)

    def _marker_key(self, price: float) -> int | float:
        if self.price_tick_size > 0.0:
            return int(round(price / self.price_tick_size))
        return round(price, 10)

    def _account_marker_display_side(self, marker: DomAccountMarker) -> str:
        """Resolve visual side with a small deadband/consecutive-frame latch."""
        role = marker.role.upper()
        side = marker.side.upper()
        if role in {'LMT', 'ORD'} and side in {'BUY', 'SELL'}:
            return 'bid' if side == 'BUY' else 'ask'
        snapshot = self.snapshot
        center = 0.0
        if snapshot is not None:
            center = snapshot.microprice or snapshot.midpoint
            if center <= 0.0 and snapshot.best_bid > 0.0 and (snapshot.best_ask > 0.0):
                center = (snapshot.best_bid + snapshot.best_ask) * 0.5
        fallback = 'bid' if side in {'BUY', 'LONG'} else 'ask' if side in {'SELL', 'SHORT'} else 'bid'
        if center <= 0.0:
            return fallback
        desired = 'bid' if marker.exact_price < center else 'ask' if marker.exact_price > center else fallback
        key = (role, side, round(float(marker.exact_price), 12), str(marker.source))
        entry = self._account_marker_side_latches.get(key)
        if entry is None:
            self._account_marker_side_latches[key] = {'side': desired, 'candidate': '', 'count': 0}
            return desired
        current = str(entry.get('side', desired))
        if desired == current:
            entry['candidate'] = ''
            entry['count'] = 0
            return current
        distance_bps = abs(marker.exact_price - center) / max(center, 1e-12) * 10000.0
        if distance_bps >= 3.0:
            entry.update(side=desired, candidate='', count=0)
            return desired
        if distance_bps <= 1.5:
            entry['candidate'] = ''
            entry['count'] = 0
            return current
        if entry.get('candidate') == desired:
            entry['count'] = int(entry.get('count', 0)) + 1
        else:
            entry['candidate'] = desired
            entry['count'] = 1
        if int(entry.get('count', 0)) >= 3:
            entry.update(side=desired, candidate='', count=0)
            return desired
        return current

    def _display_bucket_price(self, price: float, side: str) -> float:
        return self._bucket_price_for_multiplier(price, side, self.aggregation_multiplier)

    def account_price_to_display_row(self, marker: DomAccountMarker) -> tuple[int | None, str]:
        """Map exact account state onto the current display rows deterministically.

        Returns ``(row_index, "")`` for an onscreen row, otherwise
        ``(None, "above"|"below"|"")`` for an offscreen/no-row marker.
        """
        if not self._hit_rows:
            return (None, '')
        display_side = self._account_marker_display_side(marker)
        target = self._display_bucket_price(marker.exact_price, display_side)
        side_rows = [(index, entry) for index, entry in enumerate(self._hit_rows) if isinstance(entry[2], OrderFlowDisplayLevel) and entry[2].side == display_side]
        visible_prices = [float(entry[1]) for entry in self._hit_rows]
        if visible_prices:
            if target > max(visible_prices):
                return (None, 'above')
            if target < min(visible_prices):
                return (None, 'below')
        if not side_rows:
            return (None, '')
        target_key = self._marker_key(target)
        for index, entry in side_rows:
            if self._marker_key(float(entry[1])) == target_key:
                return (index, '')
        index, _entry = min(side_rows, key=lambda pair: abs(float(pair[1][1]) - target))
        return (index, '')

    def _rebuild_account_marker_mapping(self) -> None:
        grouped: dict[int, list[DomAccountMarker]] = {}
        active_marker_keys = {(marker.role.upper(), marker.side.upper(), round(float(marker.exact_price), 12), str(marker.source)) for marker in self._execution_markers}
        for key in tuple(self._account_marker_side_latches):
            if key not in active_marker_keys:
                self._account_marker_side_latches.pop(key, None)
        offscreen: dict[str, list[DomAccountMarker]] = {'above': [], 'below': []}
        if self._execution_markers and self._hit_rows:
            for marker in self._execution_markers:
                index, edge = self.account_price_to_display_row(marker)
                if index is not None:
                    grouped.setdefault(index, []).append(marker)
                elif edge in offscreen:
                    offscreen[edge].append(marker)
        priority = self._account_marker_priority
        self._mapped_account_markers = {index: tuple(sorted(markers, key=priority)) for index, markers in grouped.items()}
        self._offscreen_account_markers = {edge: tuple(sorted(markers, key=priority)) for edge, markers in offscreen.items()}

    @staticmethod
    def _account_marker_priority(marker: DomAccountMarker) -> int:
        return {'LIQ': 0, 'SL': 1, 'TP': 2, 'EXIT': 3, 'ENTRY': 4, 'LMT': 5, 'STOP': 5, 'TRG': 5, 'TRAIL': 5, 'ORD': 6}.get(marker.role, 7)

    def _account_marker_tag(self, marker: DomAccountMarker, multiplicity: int, width: float) -> str:
        role = marker.role
        side = marker.side.upper()
        base = {'ENTRY': 'E', 'TP': 'TP', 'SL': 'SL', 'LIQ': 'LQ', 'LMT': 'B' if side == 'BUY' else 'S', 'ORD': 'B' if side == 'BUY' else 'S', 'EXIT': 'X', 'STOP': 'ST', 'TRG': 'TR', 'TRAIL': 'TS'}.get(role, role[:2])
        if multiplicity > 1:
            if width < 30.0:
                compact_base = {'ENTRY': 'E', 'TP': 'T', 'SL': 'S', 'LIQ': 'L', 'EXIT': 'X', 'STOP': 'O', 'TRG': 'G', 'TRAIL': 'R', 'LMT': 'B' if side == 'BUY' else 'S', 'ORD': 'B' if side == 'BUY' else 'S'}.get(role, base[:1])
                return f'{compact_base}{multiplicity}'
            return f'{base}×{multiplicity}'
        return base

    def _draw_offscreen_account_markers(self, painter: QtGui.QPainter) -> None:
        columns = self._geometry.get('columns', {})
        if not isinstance(columns, dict) or 'price' not in columns or (not self._hit_rows):
            return
        row_height = float(self._geometry['row_height'])
        table_top = float(self._geometry['table_top'])
        footer_top = float(self._geometry['footer_top'])
        margin = float(self._geometry.get('margin', 0.0))
        price_left = float(columns['price'][0])
        available_right = max(margin + 1.0, price_left - 2.0)
        available_width = max(1.0, available_right - margin)
        if self._book_depth and 'liquidity' in columns:
            margin = max(float(columns['liquidity'][0]) + self._profile_amount_width + 2.0, self.width() * 0.48)
            available_width = max(1.0, float(columns['liquidity'][1]) - margin - 2.0)
        for edge, markers in self._offscreen_account_markers.items():
            if not markers:
                continue
            ordered = markers
            top = table_top if edge == 'above' else max(table_top, footer_top - row_height)
            clip_rect = QtCore.QRectF(margin, top, available_width, row_height)
            arrow = '↑' if edge == 'above' else '↓'
            lead = ordered[0]
            role = str(lead.role or 'ORD')
            exact = format_book_price(lead.exact_price, self.price_decimals)
            extra = f' +{len(ordered) - 1}' if len(ordered) > 1 else ''
            reference = 0.0
            if self.snapshot is not None:
                reference = float(self.snapshot.best_ask) if edge == 'above' else float(self.snapshot.best_bid)
            ticks_text = ''
            if self.price_tick_size > 0.0 and reference > 0.0:
                ticks = int(round(abs(lead.exact_price - reference) / self.price_tick_size))
                ticks_text = f' · {ticks}t'
            candidates = (f'{arrow} {role} {exact}{ticks_text}{extra}', f'{arrow} {role} {exact}{extra}', f'{arrow} {role} {exact}', f'{arrow} {role}', arrow)
            usable = max(1.0, clip_rect.width() - 8.0)
            label = arrow
            for candidate in candidates:
                if self._label_metrics.horizontalAdvance(candidate) <= usable:
                    label = candidate
                    break
            painter.save()
            try:
                painter.setClipRect(device_pixel_rect(self, clip_rect), Qt.ClipOperation.IntersectClip)
                fill = QtGui.QColor(ORDERBOOK_REFERENCE['bg'])
                label_width = min(clip_rect.width(), self._label_metrics.horizontalAdvance(label) + 10.0)
                badge = QtCore.QRectF(clip_rect.left() + 1.0, clip_rect.top() + 1.0, max(1.0, label_width), max(1.0, clip_rect.height() - 2.0))
                painter.fillRect(device_pixel_rect(self, badge), fill)
                self._draw_text(painter, badge, label, self._badge_text, Qt.AlignmentFlag.AlignLeft, font=self._label_font, pad=3.0)
                distinct: list[DomAccountMarker] = []
                seen: set[tuple[str, str]] = set()
                for marker in ordered:
                    signature = (marker.role, marker.side)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    distinct.append(marker)
                pip_size = max(self._physical_pixel_width() * 2.0, 2.0)
                gap = max(self._physical_pixel_width(), 1.0)
                pip_right = min(clip_rect.right() - 2.0, badge.right() - 2.0)
                y = badge.top() + 1.0 if edge == 'above' else badge.bottom() - pip_size - 1.0
                for index, marker in enumerate(distinct[:5]):
                    x = pip_right - (index + 1) * pip_size - index * gap
                    if x <= badge.left() + 2.0:
                        break
                    pip = QtCore.QRectF(x, y, pip_size, pip_size)
                    color = QtGui.QColor(marker.color)
                    color.setAlpha(235)
                    painter.fillRect(device_pixel_rect(self, pip), color)
            finally:
                painter.restore()

    def _draw_execution_overlays(self, painter: QtGui.QPainter) -> None:
        if not self._execution_markers or not self._hit_rows:
            return
        columns = self._geometry.get('columns', {})
        if not isinstance(columns, dict):
            return
        for index, marker_tuple in self._mapped_account_markers.items():
            if not marker_tuple or index < 0 or index >= len(self._hit_rows):
                continue
            row_rect, _row_price, row_level = self._hit_rows[index]
            markers = marker_tuple
            marker = markers[0]
            line_color = QtGui.QColor(marker.color)
            line_color.setAlpha(190 if marker.role in {'LIQ', 'SL', 'TP'} else 135)
            pen = QtGui.QPen(line_color)
            pen.setCosmetic(True)
            pen.setWidthF(0.0)
            if marker.role not in {'LIQ', 'SL', 'TP', 'ENTRY'}:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            self._draw_row_line_around_state(
                painter,
                row_rect.left(),
                row_rect.bottom() - self._physical_pixel_width(),
                row_rect.right(),
            )

            # The reference DOM has no dedicated OWN column. Keep account tags
            # away from STATE whenever possible: bid values are right-aligned so
            # their outer-left edge is free; ask values are left-aligned so their
            # outer-right edge is free. Fall back to STATE, then PRICE.
            display_side = row_level.side if isinstance(row_level, OrderFlowDisplayLevel) else self._account_marker_display_side(marker)
            preferred_lane = 'bid' if display_side == 'bid' else 'ask'
            if self._book_depth and 'liquidity' in columns:
                lane = 'liquidity'
            elif preferred_lane in columns:
                lane = preferred_lane
            elif 'state' in columns:
                lane = 'state'
            else:
                lane = 'price'
            anchor = self._column_rect(lane, row_rect.top(), row_rect.height())
            width = min(max(18.0, anchor.width() * 0.36), 34.0)
            if lane == 'bid':
                badge = QtCore.QRectF(anchor.left() + 1.0, anchor.top() + 2.0, min(width, anchor.width() - 2.0), max(1.0, anchor.height() - 4.0))
            elif lane in {'ask', 'liquidity'}:
                badge = QtCore.QRectF(max(anchor.left() + 1.0, anchor.right() - width - 1.0), anchor.top() + 2.0, min(width, anchor.width() - 2.0), max(1.0, anchor.height() - 4.0))
            elif lane == 'state':
                badge = anchor.adjusted(2.0, 2.0, -2.0, -2.0)
            else:
                badge = QtCore.QRectF(anchor.left() + 1.0, anchor.top() + 2.0, min(width, anchor.width() * 0.28), max(1.0, anchor.height() - 4.0))

            same_role_count = sum((1 for item in markers if item.role == marker.role and item.side == marker.side))
            tag = self._account_marker_tag(marker, same_role_count, badge.width())
            fill = QtGui.QColor(ORDERBOOK_REFERENCE['bg'])
            painter.save()
            try:
                painter.setClipRect(device_pixel_rect(self, anchor), Qt.ClipOperation.IntersectClip)
                painter.fillRect(device_pixel_rect(self, badge), fill)
                self._draw_numeric_text(painter, badge, tag, self._badge_text, Qt.AlignmentFlag.AlignHCenter, font=self._label_font, pad=1.0)
                distinct: list[DomAccountMarker] = []
                seen_roles: set[tuple[str, str]] = set()
                for item in markers:
                    signature = (item.role, item.side)
                    if signature in seen_roles:
                        continue
                    seen_roles.add(signature)
                    distinct.append(item)
                if len(distinct) > 1:
                    pip = max(self._physical_pixel_width() * 2.0, min(2.5, badge.width() / 7.0))
                    gap = max(self._physical_pixel_width(), 0.75)
                    visible = distinct[:3]
                    total = len(visible) * pip + max(0, len(visible) - 1) * gap
                    left = badge.center().x() - total * 0.5
                    y = badge.bottom() - pip - self._physical_pixel_width()
                    for pip_index, item in enumerate(visible):
                        color = QtGui.QColor(item.color)
                        color.setAlpha(240)
                        painter.fillRect(device_pixel_rect(self, QtCore.QRectF(left + pip_index * (pip + gap), y, pip, pip)), color)
            finally:
                painter.restore()
        self._draw_offscreen_account_markers(painter)

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
        side_color = self._bid if side == 'bid' else self._ask
        price_rect = self._column_rect('price', top, height)
        hovered = self._hover_price == level.price
        if row.is_native_touch:
            painter.fillRect(price_rect, self._best_bid_row_fill if side == 'bid' else self._best_ask_row_fill)
        if 'liquidity' in g['columns']:
            lane = self._column_rect('liquidity', top, height)
            amount_width = min(max(1.0, self._profile_amount_width), max(1.0, lane.width() - 8.0))
            amount_rect = QtCore.QRectF(lane.left(), top, amount_width, height)
            plot_left = min(lane.right(), amount_rect.right() + 2.0)
            plot_rect = QtCore.QRectF(plot_left, top, max(0.0, lane.right() - plot_left - 7.0), height)
            profile_key = (side, float(level.price))
            visual_size = self._profile_size_current.get(profile_key, row.profile_size)
            width = max(0.0, plot_rect.width()) * max(0.0, min(1.0, visual_size))
            row_top = top + 0.04 * height
            row_height = max(1.0, 0.92 * height)
            bar_top = row_top + 1.5
            bar_height = max(2.0, row_height - 3.0)
            bar_bottom = min(top + height, bar_top + bar_height)
            if width > 0.0 and bar_bottom > bar_top:
                bar = QtCore.QRectF(plot_rect.left(), bar_top, max(1.0, width), bar_bottom - bar_top)
                painter.fillRect(bar, self._profile_brushes[side])
                cap_width = min(1.5, bar.width())
                painter.fillRect(QtCore.QRectF(bar.right() - cap_width, bar.top(), cap_width, bar.height()), self._profile_caps[side])
            if hovered:
                painter.fillRect(QtCore.QRectF(float(g['margin']), top, float(g['inner_width']), height), self._profile_hovers[side])
            self._draw_numeric_text(painter, amount_rect, row.notional_text, self._text, Qt.AlignmentFlag.AlignLeft, font=self._row_font, pad=7.0)
        self._draw_price_text(
            painter, price_rect, row.price_text,
            side_color if row.is_native_touch else self._text,
            Qt.AlignmentFlag.AlignRight, pad=8.0,
        )
        if row.change_cue is not None and row.change_cue_color is not None:
            painter.fillRect(QtCore.QRectF(price_rect.left(), top + 2.0, 2.0, max(1.0, height - 4.0)), row.change_cue_color)
        if row.is_ltp_row:
            y = top + (1.0 - row.ltp_fraction) * height
            marker_height = min(8.0, max(2.0, height - 6.0))
            marker_top = max(top + 2.0, min(top + height - marker_height - 2.0, y - marker_height / 2.0))
            painter.fillRect(QtCore.QRectF(price_rect.right() - 3.0, marker_top, 2.0, marker_height), self._mid)

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
            execution_top = float(self._geometry['execution_top'])
            execution_bottom = execution_top + float(self._geometry['execution_height'])
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
            if band_dirty(execution_top, execution_bottom):
                self._draw_execution_band(painter, bounds)
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
                self._draw_execution_overlays(painter)
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
            'rows_per_side': int(self._geometry['rows_per_side']),
            'snapshot_sequence': max(self._prepared_sequence, self._latest_applied_sequence),
            'row_snapshot_sequence': self._prepared_sequence,
            'latest_applied_sequence': self._latest_applied_sequence,
            'execution_markers': len(self._execution_markers),
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
        for index, (rect, price, level) in enumerate(self._hit_rows):
            if rect.contains(position):
                return (index, price, level)
        return (None, 0.0, None)

    def _learning_mode_enabled(self) -> bool:
        window = self.window()
        return bool(window is not None and window.property('learningMode'))

    def _context_tooltip(self, position: QtCore.QPointF) -> tuple[str, str]:
        y = position.y()
        header_bottom = float(self._geometry['header_top']) + float(self._geometry['top_height'])
        execution_top = float(self._geometry['execution_top'])
        execution_bottom = execution_top + float(self._geometry['execution_height'])
        column_top = float(self._geometry['column_top'])
        if column_top <= y < column_top + float(self._geometry['column_height']):
            labels = {'state': 'Confirmed inferred liquidity state; non-normal labels require two distinct row analyses and local significance gates', 'bid': 'Resting bid liquidity at this price (quote notional)', 'price': 'Price spine; grouped rows are informational only', 'ask': 'Resting ask liquidity at this price (quote notional)', 'flow': 'Signed aggressive trade-flow imbalance at this price over 5 seconds', 'delta': 'Observed visible resting-liquidity change over 5 seconds, measured from depth updates', 'memory': 'Liquidity persistence history over the last 30 seconds'}
            if self.aggregation_multiplier > 1:
                labels.update({
                    'state': 'Native STATE classifications are hidden above 1×. Event badges describe activity contained in the bucket.',
                    'memory': '30s bucket liquidity persistence reconstructed from the constituent native levels; unknown history is preserved rather than treated as zero.',
                    'delta': 'Five-second visible resting-liquidity change for the displayed bucket, normalized by the bucket previous depth.',
                    'flow': 'Net aggressive-flow imbalance within this bucket over 5 seconds; opposing trades cancel in the ratio.',
                })
            labels['bid'] += '; intensity is relative to the current grouped view'
            labels['ask'] += '; intensity is relative to the current grouped view'
            for name, (left, right) in self._geometry['columns'].items():
                if float(left) <= position.x() < float(right):
                    return (f'column:{name}', labels.get(name, name))
        if y <= header_bottom:
            return ('header', self._header_tooltip)
        if execution_bottom > execution_top and execution_top <= y <= execution_bottom:
            position_text = self._execution_text[0][0] if self._execution_text else 'FLAT'
            order_text = self._execution_text[1][0] if len(self._execution_text) > 1 else 'ORD 0'
            return ('execution', f'Account context\nPosition: {position_text}\nWorking orders: {order_text}')
        if float(self._geometry['center_top']) <= y <= float(self._geometry['center_bottom']):
            return ('center', self._center_tooltip)
        if y >= float(self._geometry['footer_top']):
            return ('footer', self._footer_tooltip)
        if self.aggregation_multiplier > 1:
            return ('ladder', 'Aggregated display rows are informational. Switch to 1× tick to select an exact order price.')
        return ('ladder', 'Click a native-tick price row to copy it into the order ticket')

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

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._column_resize_active:
            self._update_column_resize(event.position().x())
            event.accept()
            return
        resize_pair = self._column_resize_boundary_at(event.position())
        if resize_pair is not None:
            self.setCursor(Qt.CursorShape.SplitHCursor)
            self._hover_context = f'resize:{resize_pair[0]}:{resize_pair[1]}'
            self.setToolTip('Drag to resize adjacent order-book columns')
            event.accept()
            return
        row_index, price, level = self._hover_level(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if level is not None and self.aggregation_multiplier == 1 else Qt.CursorShape.ArrowCursor)
        context_key = ''
        tooltip = ''
        if level is not None and row_index is not None:
            context_key = f'row:{level.side}:{self._marker_key(level.price)}'
            markers = self._mapped_account_markers.get(row_index, ())
            execution_lines: list[str] = []
            for marker in markers:
                quantity = f' {human_number(marker.quantity)}' if marker.quantity > 0.0 else ''
                execution_lines.append(f'{marker.role} {marker.side}{quantity} @ {self._price_text(marker.exact_price)}')
            execution_text = '\nAccount overlays:\n  ' + '\n  '.join(execution_lines) if execution_lines else ''
            display_state = self._prepared_display_states.get((level.side, self._marker_key(level.price)), level.state)
            state_description = self._STATE_DESCRIPTIONS.get(display_state, display_state.title())
            selection_text = '\nAggregated bucket: informational only. Switch to 1× tick for exact price selection.' if self.aggregation_multiplier > 1 else '\nClick to use this exact native-tick price in the order ticket.'
            state_short = self._STATE_LABELS.get(display_state, display_state[:7]) or 'NORMAL'
            compact_account = ''
            if markers:
                lead = sorted(markers, key=self._account_marker_priority)[0]
                compact_account = f'  •  ACCT {self._account_marker_tag(lead, 1, 40.0)}'
            reload_text = f'  •  RELOAD ×{level.trade_reload_count}' if level.trade_reload_count > 0 else ''
            restack_text = f'  •  RESTACK ×{level.restack_count}' if level.restack_count > 0 else ''
            semantic_label = str(getattr(level, 'semantic_event_label', '') or '')
            semantic_text = f'  •  EVENT {semantic_label}' if semantic_label else ''
            concise_tooltip = f'{level.side.upper()}  {self._price_text(level.price)}  •  {self._money(level.notional)}  •  STATE {state_short}\nSELL EXEC {self._money(level.sell_trade_notional_5s)}  •  BUY EXEC {self._money(level.buy_trade_notional_5s)}{reload_text}{restack_text}{semantic_text}{compact_account}'
            verbose_tooltip = f'{level.side.upper()}  {self._price_text(level.price)}\nResting liquidity: {self._money(level.notional)}\nObserved visible book change (5s): {self._signed_money(level.delta_notional_5s)}\nAggressive sells at price (5s): {self._money(level.sell_trade_notional_5s)} in {level.sell_trade_count_5s} prints\nAggressive buys at price (5s): {self._money(level.buy_trade_notional_5s)} in {level.buy_trade_count_5s} prints\nSemantic event: {semantic_label or 'none'}\nTrade-driven reload: {self._money(level.recent_replenished_notional)} · {level.trade_reload_count} events\nPull/restack: {self._money(level.recent_restacked_notional)} · {level.restack_count} events\nCumulative visible depth: {self._money(level.cumulative_depth_notional)}\n{state_description}\nSTATE confirmation: 2 distinct row analyses; local same-side significance gate applied\nAge: {level.age_seconds:.1f}s  •  persistence: {level.persistence_ratio * 100:.0f}%\n30s memory — peak: {self._money(level.history_peak_notional_30s)}, mean: {self._money(level.history_mean_notional_30s)}, presence: {level.history_presence_30s * 100:.0f}%{execution_text}{selection_text}'
            if self.aggregation_multiplier == 1:
                health, _reason = self._feed_health_status(self.snapshot)
                inference_live = health in {'LIVE', 'DEGRADED'} and not (self._trade_stream_known and not self._trade_stream_active)
                flags = ', '.join(level.state_flags) or 'NORMAL'
                if inference_live and level.persistent and display_state != 'PERSISTENT':
                    concise_tooltip += '  •  PERSISTENT LIQUIDITY'
                verbose_tooltip += (
                    f'\nInstantaneous qualifications (before badge confirmation): {flags if inference_live else "unavailable while feed is stale/incomplete"}'
                    f'\nNew passive adds (5s; excludes reload/repost): {self._money(level.new_passive_added_notional)}'
                    f'\nEffective cancels (5s; excludes reposted size): {self._money(level.effective_cancelled_notional)}'
                    '\nReload/restack are inferred same-price flows, not individual order identities.'
                )
            if self.aggregation_multiplier > 1:
                current_notional = max(0.0, float(level.notional))
                delta_notional = float(level.delta_notional_5s)
                previous_notional = max(0.0, current_notional - delta_notional)
                denominator = max(previous_notional, abs(delta_notional), 1e-9)
                delta_pct = max(-100.0, min(100.0, delta_notional / denominator * 100.0))
                history_known = any(
                    math.isfinite(float(value)) and float(value) >= 0.0
                    for value in level.liquidity_history_30s
                )
                liquidity_summary = (
                    f'\nLIQ 30s bucket — peak: {self._money(level.history_peak_notional_30s)}, '
                    f'mean: {self._money(level.history_mean_notional_30s)}, '
                    f'presence: {level.history_presence_30s * 100:.0f}%'
                    if history_known
                    else '\nLIQ 30s bucket: unavailable (insufficient native history)'
                )
                bucket_metrics = (
                    f'\nΔ BOOK (5s): {self._signed_percent(delta_pct)} · '
                    f'{self._signed_money(delta_notional)}'
                    f'{liquidity_summary}'
                )
                bucket_note = (
                    "\nBUCKET ACTIVITY · STATE remains native-price only."
                    "\nPrint markers mean a qualifying native print occurred inside this bucket, not at its displayed price."
                    "\nBar intensity is relative to this grouped view."
                )
                concise_tooltip = concise_tooltip.replace(f'STATE {state_short}', 'STATE UNCLASSIFIED')
                concise_tooltip += bucket_metrics
                verbose_tooltip = (
                    f'{level.side.upper()} BUCKET {self._price_text(level.price)}'
                    f'\nResting liquidity: {self._money(level.notional)}'
                    f'{bucket_metrics}'
                    f'\nAggressive sells in bucket: {self._money(level.sell_trade_notional_5s)} in {level.sell_trade_count_5s} prints'
                    f'\nAggressive buys in bucket: {self._money(level.buy_trade_notional_5s)} in {level.buy_trade_count_5s} prints'
                    f'\nNative activity in bucket: {semantic_label or "none"}'
                    f'{execution_text}{selection_text}'
                )
                signal = self._market_signal
                if isinstance(signal, dict) and float(signal.get('expires', 0.0)) > time.monotonic():
                    anchor_side = str(signal.get('anchor_side', ''))
                    anchor = float(signal.get('anchor_price', 0.0) or 0.0)
                    if (level.side == anchor_side and self._marker_key(level.price)
                            == self._marker_key(self._display_bucket_price(anchor, anchor_side))):
                        bucket_note += f'\n{signal.get("key", "SIGNAL")} · EXACT NATIVE ANCHOR {self._price_text(anchor)}'
                concise_tooltip += bucket_note
                verbose_tooltip += bucket_note
            tooltip = verbose_tooltip if self._learning_mode_enabled() else concise_tooltip
            if self._book_depth:
                cumulative = level.cumulative_depth_notional
                if self._value_mode == 'base':
                    prepared = self._bid_rows if level.side == 'bid' else self._ask_rows
                    cumulative = 0.0
                    for row in prepared:
                        cumulative += max(0.0, row.level.quantity)
                        if row.level.price == level.price:
                            break
                unit = 'USDT' if self._value_mode == 'quote' else self.symbol.removesuffix('USDT')
                tooltip = (
                    f'{level.side.upper()}  {self._price_text(level.price)}'
                    f'\nSize: {self._row_amount(level.notional, level.price, quantity=level.quantity)} {unit}'
                    f'\nCumulative: {self._compact_scalar(cumulative)} {unit}'
                    f'{execution_text}{selection_text}\n\n{self._profile_scale_text}'
                )
                if self._learning_mode_enabled():
                    tooltip += f'\n\n{verbose_tooltip}'
        else:
            context_key, tooltip = self._context_tooltip(event.position())
            if self._book_depth and context_key:
                tooltip += f'\n\n{self._profile_scale_text}'
        self.setToolTip(tooltip)
        changed_price = price != self._hover_price
        if changed_price or context_key != self._hover_context:
            old_rect = self._row_rect_for_price(self._hover_price) if changed_price else QtCore.QRect()
            self._hover_price = price
            self._hover_context = context_key
            self.setToolTip(tooltip)
            if changed_price:
                new_rect = self._row_rect_for_price(price)
                region = QtGui.QRegion()
                if not old_rect.isNull():
                    region += QtGui.QRegion(old_rect.adjusted(-1, -1, 1, 1))
                if not new_rect.isNull():
                    region += QtGui.QRegion(new_rect.adjusted(-1, -1, 1, 1))
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
            resize_pair = self._column_resize_boundary_at(event.position())
            if resize_pair is not None and self._begin_column_resize(resize_pair, event.position().x()):
                event.accept()
                return
            _row_index, price, _level = self._hover_level(event.position())
            if price > 0.0:
                if self.aggregation_multiplier > 1:
                    QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), 'AGGREGATED PRICE · switch to 1× tick for exact selection', self, self.rect(), 1400)
                    event.accept()
                    return
                self.price_selected.emit(price)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._column_resize_active:
            self._finish_column_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal
from ..models import DomExecutionContext, OrderFlowPresentationFrame
from ..models import ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowSnapshot
from ..models import safe_float


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


class _DomRasterProcess:
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
        self.pointer = None
        self.last_diagnostics = 0.0

    def _configure(self, value):
        self.epoch, config = value
        canvas, old = self.canvas, self.config
        if config['market_epoch'] != self.market_epoch:
            canvas.set_symbol(config['symbol'])
            canvas.reset()
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
            ('columns', canvas.set_column_preferences),
            ('widths', canvas.restore_column_width_state),
        )
        for key, setter in setters:
            if old.get(key) != config[key]:
                setter(config[key])
        canvas._frame_interval = config['interval']
        canvas._raster_dpr = config['dpr']
        canvas.setProperty('learningMode', config['learning'])
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

    def _surface(self, lease):
        from multiprocessing.shared_memory import SharedMemory
        canvas = self.canvas
        dpr = canvas.devicePixelRatioF()
        width, height = max(1, math.ceil(canvas.width()*dpr)), max(1, math.ceil(canvas.height()*dpr))
        shape = (width, height, dpr)
        if shape != self.shape:
            self._release_memory()
            size = width*height*4
            self.memory = SharedMemory(create=True, size=size*2)
            self.images = [QtGui.QImage(self.memory.buf[i*size:(i+1)*size], width, height,
                                       width*4, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                           for i in range(2)]
            for image in self.images:
                image.setDevicePixelRatio(dpr)
                image.fill(canvas._bg)
            self.shape, self.previous_slot = shape, None
            canvas._dirty_pixels = QtGui.QRegion(canvas.rect())
        slot = 1 - lease[1] if lease is not None and lease[0] == self.memory.name else 0
        target = self.images[slot]
        if self.previous_slot is not None and self.previous_slot != slot:
            painter = QtGui.QPainter(target)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
            painter.drawImage(QtCore.QPointF(), self.images[self.previous_slot])
            painter.end()
        self.previous_slot = slot
        return slot, target

    def step(self, commands, lease):
        commands = dict(commands)
        if 'config' in commands:
            self._configure(commands.pop('config'))
        canvas = self.canvas
        # Old-market commands are rejected independently of their arrival order.
        for name, tagged in commands.items():
            market_epoch, args = tagged
            if market_epoch != self.market_epoch:
                continue
            if name == 'pointer':
                self.pointer = args
            elif name == 'snapshot':
                if args is not None:
                    canvas.set_snapshot(args)
                    canvas._snapshot_prepare_timer.stop()
                    canvas._flush_pending_snapshot()
            elif name in ('set_execution_context', 'set_mark_price', 'set_microstructure_snapshot',
                          'set_book_validity', 'set_trade_stream_status'):
                getattr(canvas, name)(*args)
        self.application.processEvents()
        # Hover uses the same formatter and exact row semantics, off the GUI GIL.
        if self.pointer is not None:
            x, y, learning = self.pointer
            canvas.setProperty('learningMode', learning)
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
                  'tooltip': canvas.toolTip(), 'frame': None}
        if not canvas._dirty_pixels.isEmpty():
            slot, target = self._surface(lease)
            dirty, canvas._dirty_pixels = canvas._dirty_pixels, QtGui.QRegion()
            canvas._paint_target = target
            canvas.paintEvent(QtGui.QPaintEvent(dirty))
            canvas._paint_target = None
            result['frame'] = (self.memory.name, slot, *self.shape)
            result['geometry'] = canvas._geometry
            ready = canvas.snapshot is not None and canvas.snapshot.ready
            result['prices'] = (tuple(row.level.price for row in canvas._ask_rows) if ready else (),
                                tuple(row.level.price for row in canvas._bid_rows) if ready else ())
            result['sequence'] = canvas._latest_applied_sequence
            result['layout'] = canvas.layout_state()
        timers = canvas.findChildren(QTimer)
        due = [max(1, timer.remainingTime()) for timer in timers if timer.isActive()]
        result['next_at'] = time.monotonic() + min(due, default=float('inf')) / 1000.0
        now = time.monotonic()
        if now - self.last_diagnostics >= 1.0:
            result['diagnostics'] = canvas.performance_state()
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
        self._raster_ack_pending = False
        self._display_epoch = 0
        self._market_epoch = 0
        self._sent_config = None
        self._remote_diagnostics = {}
        self._remote_error = ''
        self._remote_typography = None
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
        self._queue_configuration()

    def _queue_configuration(self):
        link = self._process_link
        if link is None:
            return
        config = dict(symbol=self.symbol, market_epoch=self._market_epoch,
                      tick=self.price_tick_size, aggregation=self.aggregation_multiplier,
                      density=self._row_density, values=self._value_mode, depth=self._book_depth,
                      columns=self.column_preferences(), widths=self.column_width_state(),
                      size=(max(1,self.width()), max(1,self.height())), dpr=self.devicePixelRatioF(),
                      interval=display_frame_interval_ms(self), theme=self._bar_theme,
                      learning=self._learning_mode_enabled(),
                      typography=self._remote_typography)
        if config == self._sent_config:
            return
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
            execution_active=bool(self.execution_context.positions or self.execution_context.orders),
            book_depth=self._book_depth)
        fixed = sum(float(g[key]) for key in ('margin', 'top_height', 'execution_height',
                                              'column_height', 'center_height', 'footer_height'))
        return max(1, int(math.ceil(fixed + max(0, int(rows_per_side))*g['nominal_row_height']*2)))

    def update(self, *args):
        # Setters invalidate remote state; only a completed image dirties Qt.
        self._queue_configuration()

    def _prepare_display(self, *, reuse_rows=False):
        self._queue_configuration()

    def _prepare_execution_display(self):
        pass

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
        self._latest_received_sequence = snapshot.sequence
        self._send('snapshot', payload)

    def set_execution_context(self, context):
        context = context or DomExecutionContext(self.symbol)
        if context.symbol and context.symbol != self.symbol:
            return
        self.execution_context = context
        self._send('set_execution_context', (context,))

    def set_mark_price(self, mark):
        self._send('set_mark_price', (mark,))

    def set_microstructure_snapshot(self, snapshot):
        self._send('set_microstructure_snapshot', (snapshot,))

    def set_book_validity(self, valid, reason=''):
        self._send('set_book_validity', (valid, reason))

    def set_trade_stream_status(self, active, reason=''):
        self._send('set_trade_stream_status', (active, reason))

    def set_interaction_priority(self, active):
        # No row preparation or raster work runs here during chart interaction.
        self._interaction_priority_active = bool(active)

    def reset(self, *, preserve_execution=False):
        self._market_epoch += 1
        self._display_frame = None
        self._latest_received_sequence = -1
        if not preserve_execution:
            self.execution_context = DomExecutionContext(self.symbol)
        self._queue_configuration()
        self._send('set_execution_context', (self.execution_context,))
        QtWidgets.QWidget.update(self)

    def resizeEvent(self, event):
        self._queue_configuration()
        QtWidgets.QWidget.resizeEvent(self, event)

    def event(self, event):
        result = super().event(event)
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange:
            self._queue_configuration()
        return result

    def showEvent(self, event):
        self._queue_configuration()
        if self._process_link is not None:
            self._process_link.enable(True)
        QtWidgets.QWidget.showEvent(self, event)

    def hideEvent(self, event):
        if self._process_link is not None:
            self._process_link.enable(False)
            self._release_raster()
        QtWidgets.QWidget.hideEvent(self, event)

    def closeEvent(self, event):
        if self._process_link is not None:
            self._process_link.close()
        QtWidgets.QWidget.closeEvent(self, event)

    @QtCore.Slot(object)
    def _adopt_raster(self, result):
        self._raster_ack_pending = True
        self._remote_diagnostics['worker_pid'] = result['worker_pid']
        repaint = False
        if result['market_epoch'] == self._market_epoch:
            if result.get('frame') is not None:
                self._display_frame = result
                self._geometry = result['geometry']
                self._publish_visible_depth_rows()
                self._publish_layout_state()
                QtWidgets.QWidget.update(self)
                repaint = True
            if result['epoch'] == self._display_epoch:
                self.setToolTip(result.get('tooltip', ''))
            if 'diagnostics' in result:
                self._remote_diagnostics.update(result['diagnostics'])
        if not repaint or not self.isVisible():
            self._release_raster()

    def _release_raster(self):
        if not self._raster_ack_pending:
            return
        self._raster_ack_pending = False
        frame = self._display_frame
        lease = frame['frame'][:2] if frame is not None else None
        self._process_link.consumed(lease=lease)

    @QtCore.Slot(str)
    def _raster_failed(self, message):
        import logging
        logging.getLogger(__name__).error('DOM rendering process: %s', message)
        self._remote_error = 'Order book renderer unavailable'
        self._display_frame = None
        self.setToolTip(message)
        QtWidgets.QWidget.update(self)

    def paintEvent(self, event):
        started = time.perf_counter()
        painter = QtGui.QPainter(self)
        frame = self._display_frame
        if frame is None:
            painter.fillRect(event.rect(), self._bg)
            if self._remote_error:
                painter.setPen(self._muted)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._remote_error)
        else:
            image = frame['pixels'].images[frame['frame'][1]]
            # Exact device pixels normally; resizing temporarily scales only the
            # last finished image until the new geometry arrives.
            painter.drawImage(QtCore.QRectF(self.rect()), image)
        painter.end()
        self.last_paint_ms = (time.perf_counter()-started)*1000.0
        self.max_paint_ms = max(self.max_paint_ms, self.last_paint_ms)
        self._paint_timestamps.append(time.monotonic())
        # Release the next worker frame only after Qt has consumed these pixels.
        # A burst cannot queue several GUI commits ahead of a single paint.
        self._release_raster()

    def _hover_level(self, position):
        frame = self._display_frame
        if frame is None or frame['epoch'] != self._display_epoch:
            return None, 0.0, None
        g = frame['geometry']
        if g.get('bbo_only') or not g['margin'] <= position.x() <= g['margin']+g['inner_width']:
            return None, 0.0, None
        if position.y() <= g['center_top']:
            side, index = 0, max(0, math.ceil((g['center_top']-position.y())/g['row_height'])-1)
        elif position.y() >= g['center_bottom']:
            side, index = 1, max(0, math.ceil((position.y()-g['center_bottom'])/g['row_height'])-1)
        else:
            return None, 0.0, None
        prices = frame['prices'][side]
        if not 0 <= index < min(len(prices), int(g['rows_per_side'])):
            return None, 0.0, None
        return index, prices[index], None

    def mouseMoveEvent(self, event):
        if self._column_resize_active:
            self._update_column_resize(event.position().x())
        else:
            pair = self._column_resize_boundary_at(event.position())
            _, price, _ = self._hover_level(event.position())
            self.setCursor(Qt.CursorShape.SplitHCursor if pair else
                           Qt.CursorShape.PointingHandCursor if price and self.aggregation_multiplier == 1
                           else Qt.CursorShape.ArrowCursor)
            self._send('pointer', (event.position().x(), event.position().y(), self._learning_mode_enabled()))
        event.accept()

    def leaveEvent(self, event):
        self._send('pointer', (-1.0, -1.0, False))
        self.setToolTip('')
        if not self._column_resize_active:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        QtWidgets.QWidget.leaveEvent(self, event)

    def performance_state(self):
        state = dict(self._remote_diagnostics)
        state['renderer'] = 'isolated process / shared image'
        state['gui_blit_ms'] = self.last_paint_ms
        state['gui_blit_max_ms'] = self.max_paint_ms
        state['gui_paint_fps'] = self._actual_fps()
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
        self._tape_enabled = True
        self._tape_mode = 'LARGE'
        self._latest_snapshot: OrderFlowSnapshot | None = None
        self._last_depth_capacity = 0
        self.canvas = OrderFlowDomCanvas(theme, self)
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
        self.controls.aggregation_selected.connect(lambda value: self.set_aggregation_multiplier(value, emit=True))
        self.controls.density_selected.connect(lambda value: self.set_row_density(value, emit=True))
        self.controls.value_mode_selected.connect(lambda value: self.set_value_mode(value, emit=True))
        self.controls.book_depth_toggled.connect(self.set_book_depth_enabled)
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
        self.setStyleSheet(f"QWidget#orderBookWorkspace {{ background: {ORDERBOOK_REFERENCE['bg']}; }}")
        self.setMinimumSize(0, 0)
        self.set_symbol(self.symbol)
        self._sync_controls()
        QtCore.QTimer.singleShot(0, self._ensure_trades_tape)

    def _ensure_trades_tape(self):
        # Some hosts provide their own tape during construction. Do not replace
        # it; standalone use gets the same fully functioning trade surface.
        if self._tape is None:
            self._automatic_tape = TradesTapeWidget({}, self)
            self.set_trades_tape(self._automatic_tape)

    def _emit_presentation_changed(self) -> None:
        self.presentation_changed.emit(self.presentation_state())

    def _on_canvas_aggregation_changed(self, value: int) -> None:
        self._sync_controls()
        self.aggregation_changed.emit(int(value))

    def _on_canvas_column_preferences_changed(self, preferences: object) -> None:
        self._sync_controls()
        self.column_preferences_changed.emit(preferences)

    def _on_canvas_presentation_changed(self, _state: object) -> None:
        self._sync_controls()
        self._emit_presentation_changed()


    def _sync_controls(self) -> None:
        state = self.canvas.presentation_state()
        self.controls.set_state(aggregation=int(self.canvas.aggregation_multiplier), tick_size=float(self.price_tick_size), preset=str(state.get('preset', 'execution')), density=str(state.get('density', 'normal')), value_mode=str(state.get('values', 'quote')), tape_enabled=self._tape_enabled, tape_mode=self._tape_mode, book_depth=bool(state.get('book_depth', False)), overlays=self.canvas.column_preferences())

    def _reset_display_options(self) -> None:
        self.canvas.set_book_depth_enabled(False, emit=False)
        self.canvas.set_presentation_preset('execution', emit=False)
        self.canvas.set_row_density('normal', emit=False)
        self.canvas.set_value_mode('quote', emit=False)
        self.canvas.reset_column_widths(emit=False)
        # Execution is the canonical reference/default composition. Do not
        # contradict the preset by disabling every analytical lane after setting it.
        preferences = self.canvas.column_preferences()
        preferences.update({'state': True, 'memory': True, 'delta': True, 'flow': True, 'primary': 'flow'})
        self.canvas.set_column_preferences(preferences, emit=False)
        self._tape_enabled = True
        self._tape_mode = 'LARGE'
        if self._tape is not None:
            self._tape.set_mode('LARGE', emit=False)
            self._tape.set_value_mode('quote')
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
        self._sync_controls()

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
        tape.setParent(self.splitter)
        tape.set_market(self.symbol, 0.0, tick_size=self.price_tick_size)
        tape.set_panel_active(True)
        tape.setMinimumWidth(self.TAPE_MIN_WIDTH)
        tape.setMaximumWidth(self.TAPE_MAX_WIDTH)
        tape.set_mode(self._tape_mode, emit=False)
        tape.set_value_mode(self.canvas.value_mode())
        tape.mode_changed.connect(self._tape_mode_changed)
        self.splitter.addWidget(tape)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        if self._latest_snapshot is not None:
            tape.set_order_flow_snapshot(self._latest_snapshot)
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
        """Rare structural/recovery actions only; session controls live above the DOM."""
        menu = QtWidgets.QMenu(self)
        learning = menu.addAction('Learning mode tooltips')
        learning.setCheckable(True)
        window = self.window()
        learning.setChecked(bool(window is not None and window.property('learningMode')))
        learning.toggled.connect(self._set_learning_mode_from_settings)
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

    def _set_learning_mode_from_settings(self, enabled: bool) -> None:
        """Use the application's persisted Learning Mode as the sole tooltip switch."""
        window = self.window()
        setter = getattr(window, 'set_learning_mode', None) if window is not None else None
        if callable(setter):
            setter(bool(enabled))
            return
        if window is not None:
            window.setProperty('learningMode', bool(enabled))
        if not enabled:
            QtWidgets.QToolTip.hideText()

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
        if isinstance(source, OrderFlowSnapshot):
            self._latest_snapshot = source
            tape = self._tape
            if tape is not None:
                tape.set_order_flow_snapshot(source)
        self.canvas.set_snapshot(payload)

    def set_microstructure_snapshot(self, snapshot: object) -> None:
        self.canvas.set_microstructure_snapshot(snapshot)

    def set_interaction_priority(self, active: bool) -> None:
        self.canvas.set_interaction_priority(active)

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

    def column_preferences(self) -> dict[str, object]:
        return self.canvas.column_preferences()

    def set_primary_analytic(self, name: str, *, emit: bool=True) -> None:
        self.canvas.set_primary_analytic(name, emit=emit)

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
        rows_per_side = max(0, int(self.canvas.visible_rows_per_side()))
        self.canvas.set_row_density(normalized, emit=emit)
        self._sync_controls()
        self._publish_depth_capacity()
        if emit and rows_per_side > 0:
            target = self.controls.height() + self.canvas.height_for_rows(rows_per_side)
            self.right_rail_height_requested.emit(max(1, int(target)))

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
        self._sync_controls()
        self._publish_depth_capacity()

    def set_value_mode(self, mode: str, *, emit: bool=True) -> None:
        self.canvas.set_value_mode(mode, emit=emit)
        if self._tape is not None:
            self._tape.set_value_mode(self.canvas.value_mode())
        self._sync_controls()

    def presentation_state(self) -> dict[str, object]:
        state = self.canvas.presentation_state()
        state.update({'tape_enabled': self._tape_enabled, 'tape_mode': self._tape_mode})
        return state

    def restore_presentation_state(self, state: object, *, emit: bool=False) -> None:
        values = state if isinstance(state, dict) else {}
        self.canvas.set_presentation_preset('execution', emit=False)
        self.canvas.set_row_density(str(values.get('density', 'normal')), emit=False)
        self.canvas.set_value_mode(str(values.get('values', 'quote')), emit=False)
        self.canvas.restore_column_width_state(values.get('column_widths', {}))
        if isinstance(values.get('columns'), dict):
            self.canvas.set_column_preferences(values['columns'], emit=False)
        self.canvas.set_book_depth_enabled(bool(values.get('book_depth', False)), emit=False)
        self._tape_enabled = bool(values.get('tape_enabled', True))
        self._tape_mode = 'ALL' if str(values.get('tape_mode', 'LARGE')).upper() == 'ALL' else 'LARGE'
        if self._tape is not None:
            self._tape.set_mode(self._tape_mode, emit=False)
            self._tape.set_value_mode(self.canvas.value_mode())
        self._sync_tape_visibility(force_sizes=True)
        self._sync_controls()
        self._publish_depth_capacity()
        if emit:
            self._emit_presentation_changed()

    def layout_state(self) -> dict[str, object]:
        return self.canvas.layout_state()

    def set_execution_context(self, context: DomExecutionContext | None) -> None:
        self.canvas.set_execution_context(context)

    def set_mark_price(self, price: float) -> None:
        self.canvas.set_mark_price(price)


    def set_symbol(self, symbol: str, rules: object | None=None) -> None:
        normalized = str(symbol).upper().strip().removesuffix('.P') or 'BTCUSDT'
        changed = normalized != self.symbol
        self.symbol = normalized
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
        if self._tape is not None:
            self._tape.set_market(self.symbol, self._tape.quote_volume_24h, tick_size=self.price_tick_size)
        self._sync_controls()

    def reset(self, *, preserve_execution: bool=False) -> None:
        self._latest_snapshot = None
        self.canvas.reset(preserve_execution=preserve_execution)
        if self._tape is not None:
            self._tape.reset()

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
        state['presentation_preset'] = str(self.canvas.presentation_state().get('preset', 'execution'))
        state['row_density'] = self.canvas.row_density()
        state['value_mode'] = self.canvas.value_mode()
        state['book_depth'] = int(bool(self.canvas.presentation_state().get('book_depth', False)))
        return state
