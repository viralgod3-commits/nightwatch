"""Order-book analytics and process transport; no widget dependencies."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any

from ..models import OrderFlowDisplayLevel, OrderFlowLevelMetrics, OrderFlowSnapshot, OrderFlowTradePrint
from ..models import BOOK_BBO_FRESH_SECONDS, BOOK_DEPTH_FRESH_SECONDS, book_data_is_fresh

def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0

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

@dataclass(slots=True)
class _OrderFlowTradeBucket:
    time: float
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    normal_notional: float = 0.0
    rpi_notional: float = 0.0
    count: int = 0

@dataclass(slots=True)
class _OrderFlowLiquidityBucket:
    time: float
    added_notional: float = 0.0
    cancelled_notional: float = 0.0
    replenished_notional: float = 0.0
    bid_added_notional: float = 0.0
    bid_cancelled_notional: float = 0.0
    ask_added_notional: float = 0.0
    ask_cancelled_notional: float = 0.0

@dataclass(slots=True)
class _PriceExecutionBucket:
    time: float
    buy: float = 0.0
    sell: float = 0.0
    normal: float = 0.0
    rpi: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    largest_buy: float = 0.0
    largest_sell: float = 0.0
    rejected_buy: int = 0
    rejected_sell: int = 0

@dataclass(slots=True)
class _LevelActivity:
    time: float
    added_notional: float = 0.0
    cancelled_notional: float = 0.0
    executed_notional: float = 0.0
    rpi_executed_notional: float = 0.0
    replenished_notional: float = 0.0
    restacked_notional: float = 0.0
    restacked_cancelled_notional: float = 0.0
    reserved_notional: float = 0.0
    visible_delta_notional: float = 0.0
    trade_reload_count: int = 0
    restack_count: int = 0
    owner: _OrderFlowLevelState | None = field(default=None, repr=False)
    retained: bool = field(default=True, repr=False)

@dataclass(slots=True)
class _LiquidityHistorySample:
    time: float
    notional: float

@dataclass(slots=True)
class _PendingExecution:
    normal_quantity: float
    time: float

@dataclass(slots=True)
class _RecentExecution:
    quantity: float
    time: float
    reloaded_quantity: float = 0.0

@dataclass(slots=True)
class _ReservedRepost:
    activity: _LevelActivity
    quantity: float
    reload_counted: bool = False
    restack_counted: bool = False

@dataclass(slots=True)
class _PendingCancellation:
    quantity: float
    price: float
    time: float
    reposts: list[_ReservedRepost] = field(default_factory=list)

@dataclass(slots=True)
class _FinalizedCancellation:
    quantity: float
    price: float
    time: float
    restacked_quantity: float = 0.0
    activity: _LevelActivity | None = None

@dataclass(slots=True)
class _OrderFlowLevelState:
    side: str
    price: float
    quantity: float = 0.0
    notional: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    max_notional: float = 0.0
    reference_peak_notional: float = 0.0
    reference_peak_time: float = -math.inf
    active: bool = False
    touches: int = 0
    replenishments: int = 0

    activity: deque[_LevelActivity] = field(default_factory=lambda: deque(maxlen=128))
    liquidity_history: deque[_LiquidityHistorySample] = field(default_factory=lambda: deque(maxlen=36))
    liquidity_history_revision: int = 0



    activity_added_notional: float = 0.0
    activity_cancelled_notional: float = 0.0
    activity_executed_notional: float = 0.0
    activity_rpi_executed_notional: float = 0.0
    activity_replenished_notional: float = 0.0
    activity_restacked_notional: float = 0.0
    activity_restacked_cancelled_notional: float = 0.0
    activity_reserved_notional: float = 0.0
    activity_visible_delta_notional: float = 0.0
    activity_trade_reload_count: int = 0
    activity_restack_count: int = 0

@dataclass(frozen=True, slots=True)
class OrderFlowMetrics:
    """Compact market-state metrics produced by :class:`OrderFlowAnalyzer`.

    This remains the compact analytics view used internally to compose the
    immutable :class:`OrderFlowSnapshot`; renderers never own these market
    calculations.
    """
    symbol: str
    ready: bool
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_quantity: float = 0.0
    best_ask_quantity: float = 0.0
    midpoint: float = 0.0
    spread_bps: float = 0.0
    microprice: float = 0.0
    microprice_bias_bps: float = 0.0
    near_pressure_pct: float = 0.0
    touch_imbalance_pct: float = 0.0
    buy_notional_1s: float = 0.0
    sell_notional_1s: float = 0.0
    buy_notional_5s: float = 0.0
    sell_notional_5s: float = 0.0
    buy_notional_15s: float = 0.0
    sell_notional_15s: float = 0.0
    aggressor_imbalance_5s_pct: float = 0.0
    rpi_notional_5s: float = 0.0
    rpi_share_5s_pct: float = 0.0
    added_notional_5s: float = 0.0
    cancelled_notional_5s: float = 0.0
    replenished_notional_5s: float = 0.0
    add_cancel_pressure_5s_pct: float = 0.0
    bid_liquidity_delta_5s: float = 0.0
    ask_liquidity_delta_5s: float = 0.0
    tracked_levels: int = 0

class OrderFlowAnalyzer:
    """Bounded temporal model for one symbol's visible order flow.

    Inputs are the existing canonical local depth book, the current-symbol
    real-time ``bookTicker`` stream and the RPI-aware ``aggTrade`` payloads.
    The class has no Qt or transport dependency and performs no rendering.

    Depth bookkeeping intentionally analyzes only a near-market slice.  Levels
    entering/leaving that slice because the market moved are not counted as
    adds/cancels at the far-depth boundary. Changes at the touch are observable
    even when the best price moves and contribute to stacking/pulling metrics.
    """
    DEPTH_LEVEL_LIMIT = 120
    PRESSURE_LEVELS = 24
    PRESSURE_DECAY_BPS = 6.0
    TRADE_BUCKET_SECONDS = 0.25
    LIQUIDITY_BUCKET_SECONDS = 0.25
    HISTORY_SECONDS = 120.0
    LEVEL_ACTIVITY_SECONDS = 5.0
    LEVEL_ACTIVITY_BUCKET_SECONDS = 0.05
    LEVEL_STALE_SECONDS = 120.0
    PENDING_EXECUTION_SECONDS = 2.0
    CANCEL_RECONCILIATION_SECONDS = 0.35
    MAX_LEVEL_STATES = 2048
    BBO_STALE_SECONDS = BOOK_BBO_FRESH_SECONDS
    DEPTH_STALE_SECONDS = BOOK_DEPTH_FRESH_SECONDS
    DEFAULT_SNAPSHOT_LEVEL_LIMIT = 80
    SNAPSHOT_LEVEL_LIMIT = 1000
    SNAPSHOT_LEVEL_REFRESH_SECONDS = 0.25
    LIQUIDITY_HISTORY_SECONDS = 30.0
    LIQUIDITY_HISTORY_SAMPLE_SECONDS = 1.0
    LIQUIDITY_HISTORY_BINS = 8
    PRINT_HISTORY_SECONDS = 8.0
    PRINT_THRESHOLD_REFRESH_SECONDS = 0.5
    PRINT_SAMPLE_CAPACITY = 1200
    PRINT_HISTORY_CAPACITY = 2048
    PRICE_PRINT_CAPACITY = 192
    RECENT_PRICE_KEY_CAPACITY = 4096
    RESTACK_WINDOW_SECONDS = 2.0
    PRINT_OUTCOME_SECONDS = 0.5
    PRINT_OUTCOME_NOISE_MULTIPLIER = 1.5
    UNRESOLVED_PRINT_CAPACITY = 512
    METRICS_CACHE_SECONDS = 0.05




    STATE_STACK_PULL_MEDIAN_FLOOR_RATIO = 0.50
    STATE_EXEC_MEDIAN_FLOOR_RATIO = 0.25
    STATE_LOCAL_MEDIAN_RADIUS = 4
    STATE_CALIBRATION_EVENT_CAPACITY = 256

    def __init__(self, symbol: str, tick_size: Any=0.0):
        self.symbol = ''
        self.tick_size = 0.0
        self.trade_buckets: deque[_OrderFlowTradeBucket] = deque(maxlen=512)
        self.liquidity_buckets: deque[_OrderFlowLiquidityBucket] = deque(maxlen=512)
        self._levels: dict[tuple[str, int | float], _OrderFlowLevelState] = {}
        self._current_keys: dict[str, set[int | float]] = {'bid': set(), 'ask': set()}
        self._previous_book: dict[str, dict[int | float, tuple[float, float]]] = {'bid': {}, 'ask': {}}
        self._pending_exec: dict[tuple[str, int | float], deque[_PendingExecution]] = {}
        self._recent_executions: dict[tuple[str, int | float], deque[_RecentExecution]] = {}
        self._pending_cancellations: dict[tuple[str, int | float], deque[_PendingCancellation]] = {}
        self._recent_finalized_cancellations: dict[tuple[str, int | float], deque[_FinalizedCancellation]] = {}
        self._depth_initialized = False
        self._depth_best_bid = 0.0
        self._depth_best_ask = 0.0
        self._depth_best_bid_quantity = 0.0
        self._depth_best_ask_quantity = 0.0
        self._book_bid = 0.0
        self._book_ask = 0.0
        self._book_bid_quantity = 0.0
        self._book_ask_quantity = 0.0
        self._book_ticker_time = -math.inf
        self._book_ticker_update_id = 0
        self._last_trade_id = -1
        self._near_pressure_pct = 0.0
        self._last_trim = -math.inf
        self._last_depth_time = -math.inf
        self._last_trade_time = -math.inf
        self._revision = 0
        self._snapshot_sequence = 0
        self._analysis_revision = 0
        self._level_revision = 0
        self._snapshot_level_cache_revision = -1
        self._snapshot_level_cache_time = -math.inf
        self._snapshot_level_cache_limit = 0
        self._current_level_order_revision = 0
        self._current_level_value_revision = 0
        self._current_level_sorted_revision = -1
        self._current_level_sorted_states: dict[str, tuple[_OrderFlowLevelState, ...]] = {'bid': (), 'ask': ()}
        self._side_median_cache_revision = -1
        self._side_median_cache: dict[str, float] = {'bid': 0.0, 'ask': 0.0}
        self._local_median_cache_revision = -1
        self._local_median_cache: dict[tuple[str, int | float], float] = {}
        self._snapshot_bid_levels: tuple[OrderFlowDisplayLevel, ...] = ()
        self._snapshot_ask_levels: tuple[OrderFlowDisplayLevel, ...] = ()
        self._snapshot_level_scales = (0.0, 0.0, 0.0)
        self._snapshot_base_levels: list[OrderFlowDisplayLevel] = []
        self._snapshot_base_indices_by_price: dict[int | float, tuple[int, ...]] = {}
        self._quote_volume_24h = 0.0
        self._print_sequence = 0
        self._recent_prints: deque[OrderFlowTradePrint] = deque(maxlen=self.PRINT_HISTORY_CAPACITY)
        self._price_prints: dict[int | float, deque[OrderFlowTradePrint]] = {}
        self._price_activity: dict[int | float, deque[_PriceExecutionBucket]] = {}
        self._price_print_revisions: dict[int | float, int] = {}
        self._price_execution_cache: dict[
            tuple[int | float, float],
            tuple[int, float, float, tuple[float, float, float, float, int, int, float, float, int, int]],
        ] = {}
        self._price_execution_cache_hits = 0
        self._liquidity_history_cache: dict[
            tuple[str, int | float],
            tuple[int, bool, float, float, float, tuple[tuple[float, ...], float, float, float]],
        ] = {}
        self._liquidity_history_cache_hits = 0
        self._recent_price_keys: OrderedDict[int | float, float] = OrderedDict()
        self._print_notional_samples: deque[float] = deque(maxlen=self.PRINT_SAMPLE_CAPACITY)
        self._large_trade_threshold = 1000.0
        self._print_threshold_stamp = -math.inf
        self._last_bbo_midpoint = 0.0
        self._last_bbo_spread = 0.0
        self._bbo_noise_ema = 0.0
        self._unresolved_prints: deque[OrderFlowTradePrint] = deque(maxlen=self.UNRESOLVED_PRINT_CAPACITY)
        self._unresolved_print_evictions = 0
        self._print_outcomes: dict[int, str] = {}
        self._print_revision = 0
        self._recent_prints_cache_revision = -1
        self._recent_prints_cache_expires = -math.inf
        self._recent_prints_cache: tuple[OrderFlowTradePrint, ...] = ()
        self._snapshot_dirty_price_keys: set[int | float] = set()
        self._snapshot_cached_price_keys: set[int | float] = set()
        self._snapshot_analyzed_price_keys: set[int | float] = set()
        self._snapshot_side_medians: dict[str, float] = {'bid': 0.0, 'ask': 0.0}
        self._snapshot_local_medians: dict[tuple[str, int | float], float] = {}
        self._snapshot_active_side_keys: dict[str, tuple[int | float, ...]] = {'bid': (), 'ask': ()}
        self._next_pending_cancellation_due = math.inf
        self._snapshot_level_cache_hits = 0
        self._snapshot_level_partial_refreshes = 0
        self._snapshot_level_full_refreshes = 0
        self._recent_print_cache_hits = 0
        self._metrics_cache_revision = -1
        self._metrics_cache_expires = -math.inf
        self._state_signal_counts: dict[str, int] = {}
        self._state_exit_counts: dict[str, int] = {}
        self._state_significance_suppressed: dict[str, int] = {}
        self._state_suppression_active: set[tuple[str, int | float, str]] = set()
        self._state_last_classification: dict[tuple[str, int | float], str] = {}
        self._state_last_flags: dict[tuple[str, int | float], tuple[str, ...]] = {}
        self._state_overlap_counts: dict[str, int] = {}
        self._state_calibration_events: deque[tuple[object, ...]] = deque(
            maxlen=self.STATE_CALIBRATION_EVENT_CAPACITY
        )
        self._metrics_cache: OrderFlowMetrics | None = None
        self._metrics_cache_hits = 0
        self._depth_capacity = self.DEFAULT_SNAPSHOT_LEVEL_LIMIT
        self._source_depth_bids: list[tuple[float, float]] | tuple[tuple[float, float], ...] = ()
        self._source_depth_asks: list[tuple[float, float]] | tuple[tuple[float, float], ...] = ()
        self._source_depth_revision = 0
        self._source_depth_update_id = 0
        self._display_only_depth_cache: OrderedDict[
            tuple[str, int | float], tuple[float, OrderFlowDisplayLevel]
        ] = OrderedDict()
        self._snapshot_source_depth_revision = -1
        self.reset(symbol, tick_size=tick_size)

    @staticmethod
    def _tick_value(value: Any) -> float:
        result = _number(value)
        return result if result > 0.0 else 0.0

    def reset(self, symbol: str, *, tick_size: Any | None=None) -> None:
        normalized_symbol = str(symbol).upper()
        symbol_changed = normalized_symbol != self.symbol
        self.symbol = normalized_symbol
        if symbol_changed:




            self._quote_volume_24h = 0.0
        if tick_size is not None:
            self.tick_size = self._tick_value(tick_size)
        self.trade_buckets.clear()
        self.liquidity_buckets.clear()
        self._levels.clear()
        self._current_keys = {'bid': set(), 'ask': set()}
        self._previous_book = {'bid': {}, 'ask': {}}
        self._source_depth_bids = ()
        self._source_depth_asks = ()
        self._source_depth_update_id = 0
        self._display_only_depth_cache.clear()
        self._source_depth_revision += 1
        self._snapshot_source_depth_revision = -1
        self._pending_exec.clear()
        self._recent_executions.clear()
        self._pending_cancellations.clear()
        self._recent_finalized_cancellations.clear()
        self._depth_initialized = False
        self._depth_best_bid = 0.0
        self._depth_best_ask = 0.0
        self._depth_best_bid_quantity = 0.0
        self._depth_best_ask_quantity = 0.0
        self._book_bid = 0.0
        self._book_ask = 0.0
        self._book_bid_quantity = 0.0
        self._book_ask_quantity = 0.0
        self._book_ticker_time = -math.inf
        self._book_ticker_update_id = 0
        self._last_trade_id = -1
        self._near_pressure_pct = 0.0
        self._last_trim = -math.inf
        self._last_depth_time = -math.inf
        self._last_trade_time = -math.inf
        self._revision += 1
        self._level_revision += 1
        self._snapshot_level_cache_revision = -1
        self._snapshot_level_cache_time = -math.inf
        self._snapshot_level_cache_limit = 0
        self._current_level_order_revision += 1
        self._current_level_value_revision += 1
        self._current_level_sorted_revision = -1
        self._current_level_sorted_states = {'bid': (), 'ask': ()}
        self._side_median_cache_revision = -1
        self._side_median_cache = {'bid': 0.0, 'ask': 0.0}
        self._local_median_cache_revision = -1
        self._local_median_cache.clear()
        self._snapshot_bid_levels = ()
        self._snapshot_ask_levels = ()
        self._snapshot_level_scales = (0.0, 0.0, 0.0)
        self._snapshot_base_levels.clear()
        self._snapshot_base_indices_by_price.clear()
        self._print_sequence = 0
        self._recent_prints.clear()
        self._price_prints.clear()
        self._price_activity.clear()
        self._price_print_revisions.clear()
        self._price_execution_cache.clear()
        self._price_execution_cache_hits = 0
        self._liquidity_history_cache.clear()
        self._liquidity_history_cache_hits = 0
        self._recent_price_keys.clear()
        self._print_notional_samples.clear()
        self._large_trade_threshold = self._liquidity_print_floor()
        self._print_threshold_stamp = -math.inf
        self._last_bbo_midpoint = 0.0
        self._last_bbo_spread = 0.0
        self._bbo_noise_ema = 0.0
        self._unresolved_prints.clear()
        self._unresolved_print_evictions = 0
        self._print_outcomes.clear()
        self._print_revision += 1
        self._recent_prints_cache_revision = -1
        self._recent_prints_cache_expires = -math.inf
        self._recent_prints_cache = ()
        self._snapshot_dirty_price_keys.clear()
        self._snapshot_cached_price_keys.clear()
        self._snapshot_analyzed_price_keys.clear()
        self._snapshot_side_medians = {'bid': 0.0, 'ask': 0.0}
        self._snapshot_local_medians.clear()
        self._snapshot_active_side_keys = {'bid': (), 'ask': ()}
        self._next_pending_cancellation_due = math.inf
        self._snapshot_level_cache_hits = 0
        self._snapshot_level_partial_refreshes = 0
        self._snapshot_level_full_refreshes = 0
        self._recent_print_cache_hits = 0
        self._metrics_cache_revision = -1
        self._metrics_cache_expires = -math.inf
        self._state_signal_counts.clear()
        self._state_exit_counts.clear()
        self._state_significance_suppressed.clear()
        self._state_suppression_active.clear()
        self._state_last_classification.clear()
        self._state_last_flags.clear()
        self._state_overlap_counts.clear()
        self._state_calibration_events.clear()
        self._metrics_cache = None
        self._metrics_cache_hits = 0

    def set_quote_volume(self, quote_volume_24h: Any) -> None:
        """Update the 24h quote-volume context used only for print salience."""
        value = max(0.0, _number(quote_volume_24h))
        if math.isclose(value, self._quote_volume_24h, rel_tol=0.0, abs_tol=1e-09):
            return
        self._quote_volume_24h = value
        self._print_threshold_stamp = -math.inf

    def _liquidity_print_floor(self) -> float:
        return max(750.0, min(250000.0, self._quote_volume_24h * 2.5e-06))

    def _adaptive_print_threshold(self, now: float) -> float:
        if now - self._print_threshold_stamp < self.PRINT_THRESHOLD_REFRESH_SECONDS:
            return self._large_trade_threshold
        self._print_threshold_stamp = now
        values = sorted(self._print_notional_samples)
        floor = self._liquidity_print_floor()
        if len(values) < 24:
            self._large_trade_threshold = floor
            return floor
        count = len(values)
        middle = count // 2
        median = values[middle] if count & 1 else (values[middle - 1] + values[middle]) * 0.5
        position = (count - 1) * 0.975
        lower = int(position)
        upper = min(count - 1, lower + 1)
        fraction = position - lower
        percentile = values[lower] + (values[upper] - values[lower]) * fraction
        deviations = sorted((abs(value - median) for value in values))
        deviation = (deviations[middle] if count & 1 else (deviations[middle - 1] + deviations[middle]) * 0.5) * 1.4826
        self._large_trade_threshold = max(floor, percentile, median * 6.0, median + deviation * 6.0)
        return self._large_trade_threshold

    def set_tick_size(self, tick_size: Any) -> None:
        """Adopt exchange tick precision before temporal state is accumulated."""
        value = self._tick_value(tick_size)
        if math.isclose(value, self.tick_size, rel_tol=0.0, abs_tol=1e-15):
            return
        self.reset(self.symbol, tick_size=value)

    def _price_key(self, price: float) -> int | float:
        if self.tick_size > 0.0:
            return int(round(price / self.tick_size))
        return round(price, 12)

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) & 1:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) * 0.5

    @staticmethod
    def _window_bucket_time(now: float, seconds: float) -> float:
        return math.floor(now / seconds) * seconds

    def _trade_bucket(self, now: float) -> _OrderFlowTradeBucket:
        """Return the time bucket while preserving chronological deque order.

        Live exchange arrival is normally monotonic, but explicit ``now=``
        callers, replay, and tests may deliver older timestamps. Trade-window
        aggregation relies on sorted buckets for its reverse-walk early exit,
        so handle out-of-order buckets exactly as liquidity buckets do.
        """
        bucket_time = self._window_bucket_time(now, self.TRADE_BUCKET_SECONDS)
        buckets = self.trade_buckets
        if not buckets or buckets[-1].time < bucket_time:
            bucket = _OrderFlowTradeBucket(bucket_time)
            buckets.append(bucket)
            return bucket
        if buckets[-1].time == bucket_time:
            return buckets[-1]
        insertion_index = 0
        for index in range(len(buckets) - 1, -1, -1):
            bucket = buckets[index]
            if bucket.time == bucket_time:
                return bucket
            if bucket.time < bucket_time:
                insertion_index = index + 1
                break
        bucket = _OrderFlowTradeBucket(bucket_time)
        if len(buckets) == buckets.maxlen:
            if insertion_index == 0:
                return bucket
            buckets.popleft()
            insertion_index -= 1
        buckets.insert(insertion_index, bucket)
        return bucket

    def _liquidity_bucket(self, now: float) -> _OrderFlowLiquidityBucket:
        bucket_time = self._window_bucket_time(now, self.LIQUIDITY_BUCKET_SECONDS)
        buckets = self.liquidity_buckets
        if not buckets or buckets[-1].time < bucket_time:
            bucket = _OrderFlowLiquidityBucket(bucket_time)
            buckets.append(bucket)
            return bucket
        if buckets[-1].time == bucket_time:
            return buckets[-1]
        insertion_index = 0
        for index in range(len(buckets) - 1, -1, -1):
            bucket = buckets[index]
            if bucket.time == bucket_time:
                return bucket
            if bucket.time < bucket_time:
                insertion_index = index + 1
                break
        bucket = _OrderFlowLiquidityBucket(bucket_time)
        if len(buckets) == buckets.maxlen:
            if insertion_index == 0:
                return bucket
            buckets.popleft()
            insertion_index -= 1
        buckets.insert(insertion_index, bucket)
        return bucket

    def _record_recent_execution(self, side: str, price_key: int | float, quantity: float, now: float) -> None:
        quantity = max(0.0, quantity)
        if quantity <= 0.0:
            return
        key = (side, price_key)
        queue = self._recent_executions.get(key)
        if queue is None:
            queue = deque(maxlen=128)
            self._recent_executions[key] = queue
        stamp = self._window_bucket_time(now, self.LEVEL_ACTIVITY_BUCKET_SECONDS)
        if queue and queue[-1].time == stamp:
            queue[-1].quantity += quantity
        else:
            queue.append(_RecentExecution(quantity=quantity, time=stamp))

    def _match_recent_reload(self, side: str, price_key: int | float, quantity: float, now: float) -> float:
        """Attribute later same-price additions to recently executed liquidity."""
        remaining = max(0.0, quantity)
        if remaining <= 0.0:
            return 0.0
        key = (side, price_key)
        queue = self._recent_executions.get(key)
        if queue is None:
            return 0.0
        cutoff = now - self.PENDING_EXECUTION_SECONDS
        while queue and queue[0].time < cutoff:
            queue.popleft()
        if not queue:
            self._recent_executions.pop(key, None)
            return 0.0
        matched_total = 0.0
        for recent in reversed(queue):
            if remaining <= 0.0:
                break
            available = max(0.0, recent.quantity - recent.reloaded_quantity)
            if available <= 0.0:
                continue
            matched = min(remaining, available)
            recent.reloaded_quantity += matched
            remaining -= matched
            matched_total += matched
        return matched_total

    def _attribute_reposts(self, pending: _PendingCancellation, quantity: float, *, reload: bool) -> float:
        """Resolve reserved additions on their original activity timestamps."""
        remaining = max(0.0, quantity)
        attributed = 0.0
        while pending.reposts and remaining > 0.0:
            repost = pending.reposts[0]
            activity = repost.activity
            matched = min(remaining, repost.quantity)
            notional = matched * pending.price
            new_reserved = max(0.0, activity.reserved_notional - notional)
            reserved_delta = new_reserved - activity.reserved_notional
            if reload:
                reload_delta = int(not repost.reload_counted)
                repost.reload_counted = True
                self._update_activity(
                    activity, reserved=reserved_delta, replenished=notional,
                    trade_reload_count=reload_delta,
                )
            else:
                restack_delta = int(not repost.restack_counted)
                repost.restack_counted = True
                self._update_activity(
                    activity, reserved=reserved_delta, restacked=notional,
                    restack_count=restack_delta,
                )
            remaining -= matched
            attributed += matched
            repost.quantity -= matched
            if repost.quantity <= 1e-15:
                pending.reposts.pop(0)
        return attributed

    def _record_finalized_cancellation(self, key: tuple[str, int | float], pending: _PendingCancellation) -> None:
        side, price_key = key
        state = self._levels.get(key)
        quantity = max(0.0, pending.quantity)
        cancelled = quantity * pending.price
        if cancelled <= 0.0:
            return
        restacked_quantity = self._attribute_reposts(pending, quantity, reload=False)
        activity = None
        if state is not None:
            activity = self._append_activity(state, pending.time, cancelled=cancelled)
            if activity is not None:
                self._update_activity(
                    activity,
                    restacked_cancelled=restacked_quantity * pending.price,
                )
        recent = self._recent_finalized_cancellations.get(key)
        if recent is None:
            recent = deque(maxlen=128)
            self._recent_finalized_cancellations[key] = recent
        recent.append(_FinalizedCancellation(
            quantity=quantity, price=pending.price, time=pending.time,
            restacked_quantity=restacked_quantity, activity=activity,
        ))
        bucket = self._liquidity_bucket(pending.time)
        bucket.cancelled_notional += cancelled
        if side == 'bid':
            bucket.bid_cancelled_notional += cancelled
        else:
            bucket.ask_cancelled_notional += cancelled
        self._revision += 1
        self._snapshot_dirty_price_keys.add(price_key)

    def _finalize_pending_cancellations(self, now: float) -> None:
        if now + 1e-12 < self._next_pending_cancellation_due:
            return
        cutoff = now - self.CANCEL_RECONCILIATION_SECONDS
        next_due = math.inf
        for key in list(self._pending_cancellations):
            queue = self._pending_cancellations.get(key)
            if queue is None:
                continue
            while queue and queue[0].time <= cutoff:
                self._record_finalized_cancellation(key, queue.popleft())
            if not queue:
                self._pending_cancellations.pop(key, None)
                continue
            next_due = min(next_due, queue[0].time + self.CANCEL_RECONCILIATION_SECONDS)
        self._next_pending_cancellation_due = next_due

    def _queue_cancellation(self, side: str, price_key: int | float, price: float, quantity: float, now: float) -> None:
        quantity = max(0.0, quantity)
        if quantity <= 0.0:
            return
        key = (side, price_key)
        queue = self._pending_cancellations.get(key)
        if queue is None:
            queue = deque(maxlen=128)
            self._pending_cancellations[key] = queue
        if len(queue) == queue.maxlen:
            self._record_finalized_cancellation(key, queue.popleft())
        queue.append(_PendingCancellation(quantity, price, now))
        self._next_pending_cancellation_due = min(
            self._next_pending_cancellation_due,
            now + self.CANCEL_RECONCILIATION_SECONDS,
        )

    def _match_finalized_restack(self, side: str, price_key: int | float, quantity: float, now: float) -> float:
        """Match reposted size against cancellations whose attribution is final.

        Cancellation/execution attribution must finish before a repost can be
        treated as restack with certainty. A separate short history lets that
        restack relationship survive beyond the much shorter late-trade
        reconciliation window.
        """
        remaining = max(0.0, quantity)
        if remaining <= 0.0:
            return 0.0
        key = (side, price_key)
        queue = self._recent_finalized_cancellations.get(key)
        if queue is None:
            return 0.0
        cutoff = now - self.RESTACK_WINDOW_SECONDS
        while queue and queue[0].time < cutoff:
            queue.popleft()
        if not queue:
            self._recent_finalized_cancellations.pop(key, None)
            return 0.0
        matched_total = 0.0
        for recent in reversed(queue):
            if remaining <= 0.0:
                break
            available = max(0.0, recent.quantity - recent.restacked_quantity)
            if available <= 0.0:
                continue
            matched = min(remaining, available)
            recent.restacked_quantity += matched
            if recent.activity is not None:
                self._update_activity(
                    recent.activity,
                    restacked_cancelled=matched * recent.price,
                )
            remaining -= matched
            matched_total += matched
        return matched_total

    def _match_late_execution(self, side: str, price_key: int | float, normal_quantity: float, now: float) -> float:
        """Reconcile depth-first executions, including reposts seen before the trade."""
        remaining = max(0.0, normal_quantity)
        key = (side, price_key)
        queue = self._pending_cancellations.get(key)
        if queue is None or remaining <= 0.0:
            return remaining
        while queue and remaining > 0.0:
            pending = queue[0]
            if now - pending.time >= self.CANCEL_RECONCILIATION_SECONDS:
                self._record_finalized_cancellation(key, queue.popleft())
                continue
            matched = min(remaining, pending.quantity)

            to_reload = matched
            for repost in pending.reposts:
                reloaded = min(to_reload, repost.quantity)
                self._liquidity_bucket(repost.activity.time).replenished_notional += reloaded * pending.price
                to_reload -= reloaded
                if to_reload <= 0.0:
                    break
            reloaded = self._attribute_reposts(pending, matched, reload=True)
            state = self._levels.get(key)
            if reloaded > 0.0 and state is not None:
                state.replenishments += 1


            self._record_recent_execution(side, price_key, matched - reloaded, now)
            remaining -= matched
            pending.quantity -= matched
            if pending.quantity <= 1e-15:
                queue.popleft()
        if not queue:
            self._pending_cancellations.pop(key, None)
        return remaining

    def _clear_level_suppression_tracking(self, key: tuple[str, int | float]) -> None:
        side, price_key = key
        for signal in ('ABSORBING', 'PULLING', 'STACKING', 'DEPLETING'):
            self._state_suppression_active.discard((side, price_key, signal))

    def _deactivate_level_classifier_tracking(self, key: tuple[str, int | float]) -> None:
        """Close any active classifier episode when a level leaves the book."""
        previous = self._state_last_classification.pop(key, 'NORMAL')
        if previous != 'NORMAL':
            self._state_exit_counts[previous] = self._state_exit_counts.get(previous, 0) + 1
        self._state_last_flags.pop(key, None)
        self._clear_level_suppression_tracking(key)

    def _drop_level_classifier_tracking(self, key: tuple[str, int | float]) -> None:
        """Discard classifier/telemetry state when a stale price level is evicted."""
        self._state_last_classification.pop(key, None)
        self._state_last_flags.pop(key, None)
        self._recent_executions.pop(key, None)
        self._recent_finalized_cancellations.pop(key, None)
        self._pending_exec.pop(key, None)
        if key[1] not in self._price_prints:
            for cache_key in tuple(self._price_execution_cache):
                if cache_key[0] == key[1]:
                    self._price_execution_cache.pop(cache_key, None)
        self._clear_level_suppression_tracking(key)

    def _trim(self, now: float, *, force: bool=False) -> None:
        if not force and now - self._last_trim < 0.5:
            return
        self._last_trim = now
        self._finalize_pending_cancellations(now)
        restack_cutoff = now - self.RESTACK_WINDOW_SECONDS
        for key in tuple(self._recent_finalized_cancellations):
            queue = self._recent_finalized_cancellations.get(key)
            if queue is None:
                continue
            while queue and queue[0].time < restack_cutoff:
                queue.popleft()
            if not queue:
                self._recent_finalized_cancellations.pop(key, None)
        cutoff = now - self.HISTORY_SECONDS
        while self.trade_buckets and self.trade_buckets[0].time < cutoff:
            self.trade_buckets.popleft()
        while self.liquidity_buckets and self.liquidity_buckets[0].time < cutoff:
            self.liquidity_buckets.popleft()
        print_cutoff = now - max(self.PRINT_HISTORY_SECONDS, self.LEVEL_ACTIVITY_SECONDS)
        for key, buckets in tuple(self._price_activity.items()):
            while buckets and buckets[0].time < now - self.LEVEL_ACTIVITY_SECONDS:
                buckets.popleft()
            if not buckets:
                self._price_activity.pop(key, None)
        prints_trimmed = False
        while self._recent_prints and self._recent_prints[0].received_monotonic < print_cutoff:
            self._recent_prints.popleft()
            prints_trimmed = True
        for key in list(self._price_prints):
            prints = self._price_prints.get(key)
            if prints is None:
                continue
            price_prints_trimmed = False
            while prints and prints[0].received_monotonic < print_cutoff:
                prints.popleft()
                price_prints_trimmed = True
            if price_prints_trimmed:
                self._bump_price_print_revision(key)
            if not prints:
                self._price_prints.pop(key, None)
                self._price_print_revisions.pop(key, None)
                for cache_key in tuple(self._price_execution_cache):
                    if cache_key[0] == key:
                        self._price_execution_cache.pop(cache_key, None)
        while self._recent_price_keys:
            key, stamp = next(iter(self._recent_price_keys.items()))
            if stamp >= print_cutoff:
                break
            self._recent_price_keys.pop(key, None)
        if prints_trimmed:
            self._print_revision += 1
        active_sequences = {trade.sequence for trade in self._recent_prints}
        for sequence in tuple(self._print_outcomes):
            if sequence not in active_sequences:
                self._print_outcomes.pop(sequence, None)
        pending_cutoff = now - self.PENDING_EXECUTION_SECONDS
        for key, queue in tuple(self._pending_exec.items()):
            while queue and queue[0].time < pending_cutoff:
                queue.popleft()
            if not queue:
                self._pending_exec.pop(key, None)
        for key in tuple(self._recent_executions):
            queue = self._recent_executions.get(key)
            if queue is None:
                continue
            while queue and queue[0].time < pending_cutoff:
                queue.popleft()
            if not queue:
                self._recent_executions.pop(key, None)
        stale_level_cutoff = now - self.LEVEL_STALE_SECONDS
        stale_levels = [key for key, state in self._levels.items() if not state.active and state.last_seen < stale_level_cutoff]
        for key in stale_levels:
            self._levels.pop(key, None)
            self._liquidity_history_cache.pop(key, None)
            self._drop_level_classifier_tracking(key)
        if len(self._levels) > self.MAX_LEVEL_STATES:
            overflow = len(self._levels) - self.MAX_LEVEL_STATES
            removable = sorted(((state.last_seen, key) for key, state in self._levels.items() if not state.active), key=lambda item: item[0])
            for _last_seen, key in removable[:overflow]:
                self._levels.pop(key, None)
                self._liquidity_history_cache.pop(key, None)
                self._drop_level_classifier_tracking(key)
            if len(self._levels) > self.MAX_LEVEL_STATES:
                excess = len(self._levels) - self.MAX_LEVEL_STATES
                oldest = sorted(((state.last_seen, key) for key, state in self._levels.items()), key=lambda item: item[0])
                protected = {('bid', key) for key in self._current_keys['bid']}
                protected.update((('ask', key) for key in self._current_keys['ask']))
                removed = 0
                for _last_seen, key in oldest:
                    if key in protected:
                        continue
                    self._levels.pop(key, None)
                    self._liquidity_history_cache.pop(key, None)
                    self._drop_level_classifier_tracking(key)
                    removed += 1
                    if removed >= excess:
                        break

    def _state(self, side: str, price: float, now: float) -> _OrderFlowLevelState:
        key = (side, self._price_key(price))
        state = self._levels.get(key)
        if state is None:
            state = _OrderFlowLevelState(side=side, price=price, first_seen=now, last_seen=now)
            self._levels[key] = state
        else:
            state.price = price
        return state

    @staticmethod
    def _update_activity(
        activity: _LevelActivity,
        *,
        added: float = 0.0,
        cancelled: float = 0.0,
        executed: float = 0.0,
        rpi_executed: float = 0.0,
        replenished: float = 0.0,
        restacked: float = 0.0,
        restacked_cancelled: float = 0.0,
        reserved: float = 0.0,
        visible_delta: float = 0.0,
        trade_reload_count: int = 0,
        restack_count: int = 0,
    ) -> None:
        activity.added_notional += added
        activity.cancelled_notional += cancelled
        activity.executed_notional += executed
        activity.rpi_executed_notional += rpi_executed
        activity.replenished_notional += replenished
        activity.restacked_notional += restacked
        activity.restacked_cancelled_notional += restacked_cancelled
        activity.reserved_notional += reserved
        activity.visible_delta_notional += visible_delta
        activity.trade_reload_count += int(trade_reload_count)
        activity.restack_count += int(restack_count)
        state = activity.owner
        if state is None or not activity.retained:
            return
        state.activity_added_notional += added
        state.activity_cancelled_notional += cancelled
        state.activity_executed_notional += executed
        state.activity_rpi_executed_notional += rpi_executed
        state.activity_replenished_notional += replenished
        state.activity_restacked_notional += restacked
        state.activity_restacked_cancelled_notional += restacked_cancelled
        state.activity_reserved_notional += reserved
        state.activity_visible_delta_notional += visible_delta
        state.activity_trade_reload_count += int(trade_reload_count)
        state.activity_restack_count += int(restack_count)

    @staticmethod
    def _drop_activity_bucket(state: _OrderFlowLevelState, activity: _LevelActivity) -> None:
        if not activity.retained:
            return
        activity.retained = False
        state.activity_added_notional -= activity.added_notional
        state.activity_cancelled_notional -= activity.cancelled_notional
        state.activity_executed_notional -= activity.executed_notional
        state.activity_rpi_executed_notional -= activity.rpi_executed_notional
        state.activity_replenished_notional -= activity.replenished_notional
        state.activity_restacked_notional -= activity.restacked_notional
        state.activity_restacked_cancelled_notional -= activity.restacked_cancelled_notional
        state.activity_reserved_notional -= activity.reserved_notional
        state.activity_visible_delta_notional -= activity.visible_delta_notional
        state.activity_trade_reload_count -= activity.trade_reload_count
        state.activity_restack_count -= activity.restack_count

    def _clear_level_activity(self, state: _OrderFlowLevelState) -> None:
        for activity in state.activity:
            activity.retained = False
        state.activity.clear()
        state.activity_added_notional = 0.0
        state.activity_cancelled_notional = 0.0
        state.activity_executed_notional = 0.0
        state.activity_rpi_executed_notional = 0.0
        state.activity_replenished_notional = 0.0
        state.activity_restacked_notional = 0.0
        state.activity_restacked_cancelled_notional = 0.0
        state.activity_reserved_notional = 0.0
        state.activity_visible_delta_notional = 0.0
        state.activity_trade_reload_count = 0
        state.activity_restack_count = 0

    def _append_activity(self, state: _OrderFlowLevelState, now: float, *, added: float=0.0, cancelled: float=0.0, executed: float=0.0, rpi_executed: float=0.0, replenished: float=0.0, restacked: float=0.0, visible_delta: float=0.0) -> _LevelActivity | None:
        if max(added, cancelled, executed, rpi_executed, replenished, restacked, abs(visible_delta)) <= 0.0:
            return None
        stamp = self._window_bucket_time(now, self.LEVEL_ACTIVITY_BUCKET_SECONDS)
        activities = state.activity
        activity = None
        insertion_index = 0
        for index in range(len(activities) - 1, -1, -1):
            existing = activities[index]
            if existing.time == stamp:
                activity = existing
                break
            if existing.time < stamp:
                insertion_index = index + 1
                break
        if activity is None:
            activity = _LevelActivity(stamp, owner=state)
            if len(activities) == activities.maxlen:
                if insertion_index == 0:
                    return None
                expired = activities.popleft()
                self._drop_activity_bucket(state, expired)
                insertion_index -= 1
            if insertion_index == len(activities):
                activities.append(activity)
            else:
                activities.insert(insertion_index, activity)
        self._update_activity(
            activity,
            added=added,
            cancelled=cancelled,
            executed=executed,
            rpi_executed=rpi_executed,
            replenished=replenished,
            restacked=restacked,
            visible_delta=visible_delta,
            trade_reload_count=int(replenished > 0.0),
            restack_count=int(restacked > 0.0),
        )
        return activity

    def _update_print_outcomes(self, now: float, bid: float, ask: float) -> None:
        """Resolve significant-print follow-through once, after the outcome horizon."""
        if bid <= 0.0 or ask <= bid:
            return
        midpoint = (bid + ask) * 0.5
        while self._unresolved_prints:
            trade = self._unresolved_prints[0]
            if now - trade.received_monotonic < self.PRINT_OUTCOME_SECONDS:
                break
            self._unresolved_prints.popleft()
            if trade.sequence in self._print_outcomes:
                continue
            reference = trade.reference_midpoint or trade.price
            threshold = max(trade.outcome_threshold, self.tick_size, 1e-12)
            signed_move = midpoint - reference
            with_aggressor = signed_move if trade.aggressor_side == 'buy' else -signed_move
            self._print_outcomes[trade.sequence] = 'FOLLOW_THROUGH' if with_aggressor >= threshold else 'REJECTED'
            price_key = self._price_key(trade.price)
            if self._print_outcomes[trade.sequence] == 'REJECTED':
                stamp = self._window_bucket_time(trade.received_monotonic, self.LEVEL_ACTIVITY_BUCKET_SECONDS)
                for bucket in reversed(self._price_activity.get(price_key, ())):
                    if bucket.time == stamp:
                        if trade.aggressor_side == 'buy':
                            bucket.rejected_buy += 1
                        else:
                            bucket.rejected_sell += 1
                        break
                    if bucket.time < stamp:
                        break
            self._snapshot_dirty_price_keys.add(price_key)
            self._bump_price_print_revision(price_key)
            self._print_revision += 1

    def _observe_bbo(self, now: float) -> None:
        """Observe the same canonical BBO used to score incoming prints."""
        bid, ask, _bid_quantity, _ask_quantity = self._effective_bbo(now)
        if bid <= 0.0 or ask <= bid:
            return
        midpoint = (bid + ask) * 0.5
        spread = ask - bid
        if self._last_bbo_midpoint > 0.0:
            move = abs(midpoint - self._last_bbo_midpoint)
            if move > 1e-15:
                self._bbo_noise_ema = move if self._bbo_noise_ema <= 0.0 else self._bbo_noise_ema * 0.88 + move * 0.12
        self._last_bbo_midpoint = midpoint
        self._last_bbo_spread = spread
        self._update_print_outcomes(now, bid, ask)

    def _queue_unresolved_print(self, trade: OrderFlowTradePrint) -> None:
        """Queue one salient print while making bounded-capacity loss observable."""
        if len(self._unresolved_prints) >= self.UNRESOLVED_PRINT_CAPACITY:
            self._unresolved_prints.popleft()
            self._unresolved_print_evictions += 1
        self._unresolved_prints.append(trade)

    def add_book_ticker(self, event: dict[str, Any], now: float | None=None) -> bool:
        if str(event.get('s') or '').upper() != self.symbol:
            return False
        bid = _number(event.get('b'))
        ask = _number(event.get('a'))
        bid_quantity = _number(event.get('B'))
        ask_quantity = _number(event.get('A'))
        if bid <= 0.0 or ask <= bid or bid_quantity <= 0.0 or (ask_quantity <= 0.0):
            return False
        raw_update_id = event.get('u')
        update_id = 0
        if raw_update_id not in (None, ''):
            try:
                update_id = int(raw_update_id)
            except (TypeError, ValueError, OverflowError):
                return False
            if update_id <= 0:
                return False
            if self._book_ticker_update_id and update_id <= self._book_ticker_update_id:
                return False
        current = time.monotonic() if now is None else float(now)
        self._book_bid = bid
        self._book_ask = ask
        self._book_bid_quantity = bid_quantity
        self._book_ask_quantity = ask_quantity
        self._book_ticker_time = current
        if update_id:
            self._book_ticker_update_id = update_id
        self._observe_bbo(current)
        self._revision += 1
        self._trim(current)
        return True

    def _record_print_notional_sample(self, value: float) -> None:




        self._print_notional_samples.append(max(0.0, float(value)))

    def _touch_recent_price_key(self, price_key: int | float, now: float) -> None:
        keys = self._recent_price_keys
        keys[price_key] = now
        keys.move_to_end(price_key)
        while len(keys) > self.RECENT_PRICE_KEY_CAPACITY:
            keys.popitem(last=False)

    def _bump_price_print_revision(self, price_key: int | float) -> None:
        self._price_print_revisions[price_key] = self._price_print_revisions.get(price_key, 0) + 1

    def _mark_snapshot_price_dirty(self, price_key: int | float, *, allow_structural_fallback: bool=False) -> None:
        if allow_structural_fallback and self._snapshot_cached_price_keys and price_key not in self._snapshot_cached_price_keys:
            self._level_revision += 1
            self._snapshot_dirty_price_keys.clear()
            self._snapshot_cached_price_keys.clear()
            return
        self._snapshot_dirty_price_keys.add(price_key)

    def _add_trade(self, event: dict[str, Any], current: float, *, trim: bool) -> bool:
        if str(event.get('s') or '').upper() != self.symbol:
            return False
        price = _number(event.get('p'))
        total_quantity = max(0.0, _number(event.get('q')))
        if price <= 0.0 or total_quantity <= 0.0:
            return False
        raw_trade_id = event.get('a')
        trade_id = -1
        if raw_trade_id not in (None, ''):
            try:
                trade_id = int(raw_trade_id)
            except (TypeError, ValueError, OverflowError):
                return False
            if trade_id < 0:
                return False
            if self._last_trade_id >= 0 and trade_id <= self._last_trade_id:
                return False
        total_notional = price * total_quantity
        normal_quantity = _number(event.get('_normal_quantity'))
        if '_normal_quantity' not in event:
            normal_quantity = total_quantity
        normal_quantity = max(0.0, min(total_quantity, normal_quantity))
        rpi_quantity = _number(event.get('_rpi_quantity'))
        if '_rpi_quantity' not in event:
            rpi_quantity = max(0.0, total_quantity - normal_quantity)
        rpi_quantity = max(0.0, min(total_quantity, rpi_quantity))
        normal_notional = price * normal_quantity
        rpi_notional = price * rpi_quantity
        maker_flag = event.get('m')
        if not isinstance(maker_flag, bool):
            return False
        buyer_is_maker = maker_flag
        aggressor_side = 'sell' if buyer_is_maker else 'buy'
        resting_side = 'bid' if buyer_is_maker else 'ask'
        threshold = max(1.0, self._adaptive_print_threshold(current))
        relative_size = total_notional / threshold
        salience_class = 3 if relative_size >= 5.0 else 2 if relative_size >= 2.5 else 1 if relative_size >= 1.0 else 0
        self._print_sequence += 1
        event_time_ms = int(_number(event.get('T') or event.get('E')))
        bbo_bid, bbo_ask, _bbo_bq, _bbo_aq = self._effective_bbo(current)
        reference_midpoint = (bbo_bid + bbo_ask) * 0.5 if bbo_bid > 0.0 and bbo_ask > bbo_bid else price
        reference_spread = bbo_ask - bbo_bid if bbo_bid > 0.0 and bbo_ask > bbo_bid else self._last_bbo_spread
        outcome_threshold = max(self.tick_size, reference_spread * 0.5, self._bbo_noise_ema * self.PRINT_OUTCOME_NOISE_MULTIPLIER, max(reference_midpoint, 1e-12) * 2e-05)
        trade_print = OrderFlowTradePrint(sequence=self._print_sequence, trade_id=trade_id, event_time_ms=event_time_ms, received_monotonic=current, price=price, quantity=total_quantity, notional=total_notional, aggressor_side=aggressor_side, normal_notional=normal_notional, rpi_notional=rpi_notional, relative_size=relative_size, salience_class=salience_class, reference_midpoint=reference_midpoint, outcome_threshold=outcome_threshold)
        self._recent_prints.append(trade_print)
        if salience_class >= 1:
            self._queue_unresolved_print(trade_print)
        price_key = self._price_key(price)
        price_prints = self._price_prints.get(price_key)
        if price_prints is None:
            price_prints = deque(maxlen=self.PRICE_PRINT_CAPACITY)
            self._price_prints[price_key] = price_prints
        price_prints.append(trade_print)


        activity_buckets = self._price_activity.get(price_key)
        if activity_buckets is None:
            activity_buckets = deque(maxlen=128)
            self._price_activity[price_key] = activity_buckets
        stamp = self._window_bucket_time(current, self.LEVEL_ACTIVITY_BUCKET_SECONDS)
        if not activity_buckets or activity_buckets[-1].time != stamp:
            activity_buckets.append(_PriceExecutionBucket(stamp))
        activity = activity_buckets[-1]
        activity.normal += normal_notional
        activity.rpi += rpi_notional
        if aggressor_side == 'buy':
            activity.buy += total_notional
            activity.buy_count += 1
            activity.largest_buy = max(activity.largest_buy, total_notional)
        else:
            activity.sell += total_notional
            activity.sell_count += 1
            activity.largest_sell = max(activity.largest_sell, total_notional)
        self._bump_price_print_revision(price_key)
        self._touch_recent_price_key(price_key, current)
        self._record_print_notional_sample(total_notional)
        bucket = self._trade_bucket(current)
        if aggressor_side == 'buy':
            bucket.buy_notional += total_notional
        else:
            bucket.sell_notional += total_notional
        bucket.normal_notional += normal_notional
        bucket.rpi_notional += rpi_notional
        bucket.count += 1
        pending_key = (resting_side, price_key)
        unmatched_normal_quantity = self._match_late_execution(resting_side, price_key, normal_quantity, current)
        if unmatched_normal_quantity > 0.0:
            queue = self._pending_exec.get(pending_key)
            if queue is None:
                queue = deque(maxlen=128)
                self._pending_exec[pending_key] = queue
            stamp = self._window_bucket_time(current, self.LEVEL_ACTIVITY_BUCKET_SECONDS)
            if queue and queue[-1].time == stamp:
                queue[-1].normal_quantity += unmatched_normal_quantity
            else:
                queue.append(_PendingExecution(unmatched_normal_quantity, stamp))
        state = self._levels.get(pending_key)
        if state is None:
            state = _OrderFlowLevelState(side=resting_side, price=price, first_seen=current, last_seen=current)
            self._levels[pending_key] = state
        self._append_activity(state, current, executed=normal_notional, rpi_executed=rpi_notional)
        self._last_trade_time = current
        if trade_id >= 0:
            self._last_trade_id = trade_id
        self._revision += 1
        self._print_revision += 1
        self._mark_snapshot_price_dirty(price_key, allow_structural_fallback=True)
        if trim:
            self._trim(current)
        return True

    def add_trade(self, event: dict[str, Any], now: float | None=None) -> bool:
        current = time.monotonic() if now is None else float(now)
        return self._add_trade(event, current, trim=True)

    def add_trade_batch(self, events: Any, now: float | None=None) -> bool:
        current = time.monotonic() if now is None else float(now)
        changed = False
        for event in events:
            changed = self._add_trade(event, current, trim=False) or changed
        if changed:
            self._trim(current)
        return changed

    def _clean_book_side(self, rows: list[tuple[float, float]]) -> dict[int | float, tuple[float, float]]:
        output: dict[int | float, tuple[float, float]] = {}
        for row in rows[:self.DEPTH_LEVEL_LIMIT]:
            try:
                price = float(row[0])
                quantity = float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price <= 0.0 or quantity <= 0.0 or (not math.isfinite(price * quantity)):
                continue
            output[self._price_key(price)] = (price, quantity)
        return output

    def set_depth_capacity(self, limit_per_side: int) -> int:
        """Set snapshot/display depth without changing temporal analysis state."""
        try:
            resolved = int(limit_per_side)
        except (TypeError, ValueError, OverflowError):
            resolved = self.DEFAULT_SNAPSHOT_LEVEL_LIMIT
        resolved = max(1, min(resolved, self.SNAPSHOT_LEVEL_LIMIT))
        self._depth_capacity = resolved
        return resolved

    def _display_only_depth_base(
        self,
        side: str,
        price: float,
        quantity: float,
    ) -> OrderFlowDisplayLevel:
        """Return/reuse a resting-liquidity row outside the 120-level analysis band."""
        cache_key = (side, self._price_key(price))
        cached = self._display_only_depth_cache.get(cache_key)
        if cached is not None and math.isclose(
            float(cached[0]), float(quantity), rel_tol=0.0, abs_tol=1e-12
        ):
            self._display_only_depth_cache.move_to_end(cache_key)
            return cached[1]
        notional = price * quantity
        display = OrderFlowDisplayLevel(
            side=side,
            price=price,
            quantity=quantity,
            notional=notional,
            delta_notional_5s=0.0,
            trade_notional_5s=0.0,
            signed_trade_notional_5s=0.0,
            rpi_trade_notional_5s=0.0,
            age_seconds=0.0,
            persistence_ratio=0.0,
            replenishments=0,
            state='NORMAL',
            liquidity_intensity=0.0,
            delta_intensity=0.0,
            trade_intensity=0.0,
            cumulative_depth_notional=0.0,
            depth_intensity=0.0,
            liquidity_history_30s=(),
            history_presence_30s=0.0,
            history_peak_notional_30s=0.0,
            history_mean_notional_30s=0.0,
            buy_trade_notional_5s=0.0,
            sell_trade_notional_5s=0.0,
            buy_trade_count_5s=0,
            sell_trade_count_5s=0,
            largest_buy_trade_5s=0.0,
            largest_sell_trade_5s=0.0,
            rejected_buy_prints_5s=0,
            rejected_sell_prints_5s=0,
            recent_replenished_notional=0.0,
            recent_restacked_notional=0.0,
            trade_reload_count=0,
            restack_count=0,
            semantic_event_kind='',
            semantic_event_label='',
            state_flags=(),
            persistent=False,
            analysis_revision=0,
            new_passive_added_notional=0.0,
            effective_cancelled_notional=0.0,
        )
        self._display_only_depth_cache[cache_key] = (float(quantity), display)
        self._display_only_depth_cache.move_to_end(cache_key)
        while len(self._display_only_depth_cache) > self.MAX_LEVEL_STATES * 2:
            self._display_only_depth_cache.popitem(last=False)
        return display

    def _extend_snapshot_with_source_depth(
        self,
        analyzed: list[OrderFlowDisplayLevel],
        source_rows: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        side: str,
        limit: int,
    ) -> list[OrderFlowDisplayLevel]:
        """Fill snapshot capacity from canonical depth without temporal inference."""
        if limit <= len(analyzed):
            return analyzed[:limit]
        output = list(analyzed)
        present = {(side, self._price_key(level.price)) for level in output}
        for index, row in enumerate(source_rows):
            if index >= limit:
                break
            try:
                price = float(row[0])
                quantity = float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price <= 0.0 or quantity <= 0.0 or not math.isfinite(price * quantity):
                continue
            key = (side, self._price_key(price))
            if key in present:
                continue
            output.append(self._display_only_depth_base(side, price, quantity))
            present.add(key)
        output.sort(key=lambda level: level.price, reverse=side == 'bid')
        return output[:limit]

    def _add_liquidity_activity(self, state: _OrderFlowLevelState, now: float, side: str, price: float, *, added_quantity: float, executed_quantity: float) -> None:
        added_quantity = max(0.0, added_quantity)
        executed_quantity = max(0.0, executed_quantity)
        added = added_quantity * price
        price_key = self._price_key(price)
        immediate_reload = min(added_quantity, executed_quantity)
        remaining = max(0.0, added_quantity - immediate_reload)
        delayed_reload = self._match_recent_reload(side, price_key, remaining, now)
        remaining = max(0.0, remaining - delayed_reload)
        replenished = (immediate_reload + delayed_reload) * price
        self._record_recent_execution(side, price_key, executed_quantity - immediate_reload, now)
        if replenished > 0.0:
            state.replenishments += 1
        activity = self._append_activity(state, now, added=added, replenished=replenished)
        if activity is not None and remaining > 0.0:
            queue = self._pending_cancellations.get((side, price_key), ())
            for pending in reversed(queue):
                if remaining <= 0.0:
                    break
                reserved = sum(repost.quantity for repost in pending.reposts)
                matched = min(remaining, max(0.0, pending.quantity - reserved))
                if matched > 0.0:
                    pending.reposts.append(_ReservedRepost(activity, matched))
                    self._update_activity(activity, reserved=matched * price)
                    remaining -= matched
            if remaining > 0.0:
                restacked = self._match_finalized_restack(side, price_key, remaining, now) * price
                if restacked > 0.0:
                    self._update_activity(
                        activity, restacked=restacked, restack_count=1
                    )
        if added > 0.0 or replenished > 0.0:


            self._snapshot_dirty_price_keys.add(price_key)
            bucket = self._liquidity_bucket(now)
            bucket.added_notional += added
            bucket.replenished_notional += replenished
            if side == 'bid':
                bucket.bid_added_notional += added
            else:
                bucket.ask_added_notional += added

    def _sample_level_liquidity(self, state: _OrderFlowLevelState, now: float, notional: float) -> bool:
        """Record at most one resting-liquidity observation per second.

        Returns True only when the display-visible history actually changed.
        """
        notional = max(0.0, float(notional))
        bucket = math.floor(now / self.LIQUIDITY_HISTORY_SAMPLE_SECONDS) * self.LIQUIDITY_HISTORY_SAMPLE_SECONDS
        history = state.liquidity_history
        sample = _LiquidityHistorySample(bucket, notional)
        if history and abs(history[-1].time - bucket) < 1e-09:
            previous = history[-1]
            if abs(previous.notional - notional) <= 1e-12:
                return False
            history[-1] = sample
            state.liquidity_history_revision += 1
            return True
        history.append(sample)
        state.liquidity_history_revision += 1
        return True

    def _liquidity_history_summary(self, state: _OrderFlowLevelState, now: float) -> tuple[tuple[float, ...], float, float, float]:
        """Return an 8-bin, 30-second resting-liquidity memory strip.

        The summary is cached per level revision, but only until the next exact
        temporal boundary that can alter a rolling bin. This preserves the old
        time-decay semantics while avoiding repeated scans when several snapshot
        builds touch the same dirty level inside an unchanged interval.
        """
        price_key = self._price_key(state.price)
        cache_key = (state.side, price_key)
        cached = self._liquidity_history_cache.get(cache_key)
        if cached is not None:
            revision, active, notional, built_at, expires_at, result = cached
            if (
                revision == state.liquidity_history_revision
                and active == bool(state.active)
                and math.isclose(notional, float(state.notional), rel_tol=0.0, abs_tol=1e-12)
                and built_at <= now < expires_at
            ):
                self._liquidity_history_cache_hits += 1
                return result

        bin_count = self.LIQUIDITY_HISTORY_BINS
        seconds = self.LIQUIDITY_HISTORY_SECONDS
        bin_width = seconds / bin_count
        start = now - seconds
        sums = [0.0] * bin_count
        counts = [0] * bin_count
        observed_values: list[float] = []
        next_expiry = math.inf
        for sample in state.liquidity_history:
            if sample.time < start:
                continue
            index = int((sample.time - start) / bin_width)
            if index < 0:
                continue
            if index >= bin_count:
                index = bin_count - 1
            sums[index] += sample.notional
            counts[index] += 1
            observed_values.append(sample.notional)




            if index > 0:
                boundary = sample.time + seconds - index * bin_width
            else:
                boundary = sample.time + seconds
            if boundary <= now + 1e-12:
                next_expiry = min(next_expiry, now)
            else:
                next_expiry = min(next_expiry, boundary)

        sample_seconds = max(1e-9, float(self.LIQUIDITY_HISTORY_SAMPLE_SECONDS))
        current_sample_bucket = math.floor(now / sample_seconds) * sample_seconds
        newest_sample_time = state.liquidity_history[-1].time if state.liquidity_history else -math.inf
        if state.active and state.notional >= 0.0 and (newest_sample_time < current_sample_bucket - 1e-09):
            sums[-1] += state.notional
            counts[-1] += 1
            observed_values.append(state.notional)
        if state.active:
            next_sample_bucket = current_sample_bucket + sample_seconds
            if next_sample_bucket > now:
                next_expiry = min(next_expiry, next_sample_bucket)

        peak = max(observed_values, default=0.0)
        mean = sum(observed_values) / len(observed_values) if observed_values else 0.0
        known = 0
        present = 0
        normalized: list[float] = []
        for index in range(bin_count):
            if counts[index] <= 0:
                normalized.append(-1.0)
                continue
            value = sums[index] / counts[index]
            known += 1
            if value > 0.0:
                present += 1
            normalized.append(value / peak if peak > 0.0 else 0.0)
        presence = present / known if known else 0.0
        result = (tuple(normalized), presence, peak, mean)
        self._liquidity_history_cache[cache_key] = (
            state.liquidity_history_revision,
            bool(state.active),
            float(state.notional),
            now,
            next_expiry,
            result,
        )
        return result

    def _process_side(self, side: str, current_map: dict[int | float, tuple[float, float]], now: float) -> None:
        previous = self._previous_book[side]
        current_keys = set(current_map)
        values_changed = False
        previous_keys = set(previous)
        current_prices = [value[0] for value in current_map.values()]
        previous_prices = [value[0] for value in previous.values()]
        current_min = min(current_prices) if current_prices else 0.0
        current_max = max(current_prices) if current_prices else 0.0
        previous_min = min(previous_prices) if previous_prices else 0.0
        previous_max = max(previous_prices) if previous_prices else 0.0
        for price_key in current_keys | previous_keys:
            current_value = current_map.get(price_key)
            previous_value = previous.get(price_key)
            value_changed = current_value != previous_value
            if value_changed:
                values_changed = True
                self._snapshot_dirty_price_keys.add(price_key)
            price = (current_value or previous_value or (0.0, 0.0))[0]
            if price <= 0.0:
                continue
            state = self._state(side, price, now)
            pending = self._pending_exec.pop((side, price_key), None)
            executed_quantity = sum(
                item.normal_quantity for item in (pending or ())
                if 0.0 <= now - item.time <= self.PENDING_EXECUTION_SECONDS
            )
            if current_value is not None:
                quantity = current_value[1]
                if not state.active:
                    if now - state.last_seen > self.PENDING_EXECUTION_SECONDS:
                        self._clear_level_activity(state)
                        state.liquidity_history.clear()
                        state.liquidity_history_revision += 1
                        state.replenishments = 0
                        state.max_notional = 0.0
                        state.reference_peak_notional = 0.0
                        state.reference_peak_time = now
                    state.first_seen = now
                    state.touches += 1
                    state.active = True
                state.last_seen = now
                state.quantity = quantity
                state.notional = price * quantity
                reference_peak = self._recent_reference_peak(state, now)
                state.reference_peak_notional = max(reference_peak, state.notional)
                state.reference_peak_time = now
                state.max_notional = max(state.max_notional, state.notional)
            elif state.active:
                state.active = False
                state.quantity = 0.0
                state.notional = 0.0
                self._deactivate_level_classifier_tracking((side, price_key))
            if current_value is not None:
                if self._sample_level_liquidity(state, now, state.notional):
                    self._snapshot_dirty_price_keys.add(price_key)
            if not self._depth_initialized:
                continue
            previous_quantity = previous_value[1] if previous_value is not None else 0.0
            current_quantity = current_value[1] if current_value is not None else 0.0
            infer_change = False
            if previous_value is not None and current_value is not None:
                infer_change = True
            elif current_value is not None and previous_value is None:
                infer_change = bool(previous_prices) and (price >= previous_min if side == 'bid' else price <= previous_max)
            elif previous_value is not None and current_value is None:
                infer_change = bool(current_prices) and (price >= current_min if side == 'bid' else price <= current_max)
            if current_value is None and previous_value is not None and infer_change:
                if self._sample_level_liquidity(state, now, 0.0):
                    self._snapshot_dirty_price_keys.add(price_key)
            if infer_change:
                self._append_activity(state, now, visible_delta=(current_quantity - previous_quantity) * price)
                residual = current_quantity - previous_quantity + executed_quantity
                added_quantity = max(0.0, residual)
                cancelled_quantity = max(0.0, -residual)
                self._add_liquidity_activity(state, now, side, price, added_quantity=added_quantity, executed_quantity=executed_quantity)
                if cancelled_quantity > 0.0:
                    self._queue_cancellation(side, price_key, price, cancelled_quantity, now)
        if current_keys != self._current_keys[side]:
            self._current_level_order_revision += 1
            values_changed = True
        if values_changed:
            self._current_level_value_revision += 1
        self._current_keys[side] = current_keys
        self._previous_book[side] = current_map

    def _compute_near_pressure(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
        if not bids or not asks:
            return 0.0
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        midpoint = (best_bid + best_ask) * 0.5
        if midpoint <= 0.0:
            return 0.0

        def weighted(rows: list[tuple[float, float]]) -> float:
            total = 0.0
            for price, quantity in rows[:self.PRESSURE_LEVELS]:
                price = float(price)
                quantity = float(quantity)
                if price <= 0.0 or quantity <= 0.0:
                    continue
                distance_bps = abs(price - midpoint) / midpoint * 10000.0
                weight = math.exp(-distance_bps / self.PRESSURE_DECAY_BPS)
                total += price * quantity * weight
            return total
        bid_weighted = weighted(bids)
        ask_weighted = weighted(asks)
        total = bid_weighted + ask_weighted
        if total <= 0.0:
            return 0.0
        return (bid_weighted - ask_weighted) / total * 100.0

    def add_depth(
        self,
        bids: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        asks: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        now: float | None=None,
        *,
        source_revision: int=0,
    ) -> bool:
        if not bids or not asks:
            return False
        try:
            best_bid, best_bid_quantity = (float(bids[0][0]), float(bids[0][1]))
            best_ask, best_ask_quantity = (float(asks[0][0]), float(asks[0][1]))
        except (TypeError, ValueError, IndexError):
            return False
        if best_bid <= 0.0 or best_ask <= best_bid:
            return False
        current = time.monotonic() if now is None else float(now)
        try:
            resolved_source_revision = int(source_revision)
        except (TypeError, ValueError, OverflowError):
            resolved_source_revision = 0
        if resolved_source_revision > 0:



            if resolved_source_revision != self._source_depth_update_id:
                self._source_depth_bids = bids
                self._source_depth_asks = asks
                self._source_depth_update_id = resolved_source_revision
                self._source_depth_revision += 1
        else:


            source_bids = tuple(bids[:self.SNAPSHOT_LEVEL_LIMIT])
            source_asks = tuple(asks[:self.SNAPSHOT_LEVEL_LIMIT])
            if source_bids != self._source_depth_bids or source_asks != self._source_depth_asks:
                self._source_depth_bids = source_bids
                self._source_depth_asks = source_asks
                self._source_depth_revision += 1
        bid_map = self._clean_book_side(bids)
        ask_map = self._clean_book_side(asks)
        if not bid_map or not ask_map:
            return False
        self._depth_best_bid = best_bid
        self._depth_best_ask = best_ask
        self._depth_best_bid_quantity = max(0.0, best_bid_quantity)
        self._depth_best_ask_quantity = max(0.0, best_ask_quantity)
        self._observe_bbo(current)
        self._near_pressure_pct = self._compute_near_pressure(bids, asks)
        self._finalize_pending_cancellations(current)
        self._process_side('bid', bid_map, current)
        self._process_side('ask', ask_map, current)
        self._depth_initialized = True
        self._last_depth_time = current
        self._revision += 1
        self._trim(current)
        return True

    def _trade_windows(
        self,
        now: float,
    ) -> tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        float,
    ]:
        """Aggregate the 1s/5s/15s trade windows in one deque walk.

        The previous implementation traversed the same recent trade buckets
        three times per snapshot.  Buckets are ordered by time, so one reverse
        traversal can accumulate the nested windows without changing the
        inclusive cutoff semantics of ``_trade_window``.
        """
        cutoff_1s = now - 1.0
        cutoff_5s = now - 5.0
        cutoff_15s = now - 15.0
        buy_1s = sell_1s = normal_1s = rpi_1s = 0.0
        buy_5s = sell_5s = normal_5s = rpi_5s = 0.0
        buy_15s = sell_15s = normal_15s = rpi_15s = 0.0
        next_expiry = math.inf
        for bucket in reversed(self.trade_buckets):
            stamp = bucket.time
            if stamp < cutoff_15s:
                break
            buy_15s += bucket.buy_notional
            sell_15s += bucket.sell_notional
            normal_15s += bucket.normal_notional
            rpi_15s += bucket.rpi_notional
            next_expiry = min(next_expiry, stamp + 15.0)
            if stamp >= cutoff_5s:
                buy_5s += bucket.buy_notional
                sell_5s += bucket.sell_notional
                normal_5s += bucket.normal_notional
                rpi_5s += bucket.rpi_notional
                next_expiry = min(next_expiry, stamp + 5.0)
                if stamp >= cutoff_1s:
                    buy_1s += bucket.buy_notional
                    sell_1s += bucket.sell_notional
                    normal_1s += bucket.normal_notional
                    rpi_1s += bucket.rpi_notional
                    next_expiry = min(next_expiry, stamp + 1.0)
        return (
            (buy_1s, sell_1s, normal_1s, rpi_1s),
            (buy_5s, sell_5s, normal_5s, rpi_5s),
            (buy_15s, sell_15s, normal_15s, rpi_15s),
            next_expiry,
        )

    def _liquidity_window(self, now: float, seconds: float) -> tuple[float, float, float, float, float, float, float, float]:
        cutoff = now - seconds
        added = cancelled = replenished = 0.0
        bid_added = bid_cancelled = ask_added = ask_cancelled = 0.0
        next_expiry = math.inf
        for bucket in reversed(self.liquidity_buckets):
            if bucket.time < cutoff:
                break
            added += bucket.added_notional
            cancelled += bucket.cancelled_notional
            replenished += bucket.replenished_notional
            bid_added += bucket.bid_added_notional
            bid_cancelled += bucket.bid_cancelled_notional
            ask_added += bucket.ask_added_notional
            ask_cancelled += bucket.ask_cancelled_notional
            next_expiry = min(next_expiry, bucket.time + seconds)
        return (added, cancelled, replenished, bid_added, bid_cancelled, ask_added, ask_cancelled, next_expiry)

    def _effective_bbo(self, now: float) -> tuple[float, float, float, float]:
        if now - self._book_ticker_time <= self.BBO_STALE_SECONDS and self._book_bid > 0.0 and (self._book_ask > self._book_bid):
            return (self._book_bid, self._book_ask, self._book_bid_quantity, self._book_ask_quantity)
        return (self._depth_best_bid, self._depth_best_ask, self._depth_best_bid_quantity, self._depth_best_ask_quantity)

    def metrics(self, now: float | None=None) -> OrderFlowMetrics:
        current = time.monotonic() if now is None else float(now)



        self._finalize_pending_cancellations(current)
        self._trim(current)
        if (
            self._metrics_cache is not None
            and self._metrics_cache_revision == self._revision
            and current < self._metrics_cache_expires
        ):
            self._metrics_cache_hits += 1
            return self._metrics_cache

        best_bid, best_ask, bid_quantity, ask_quantity = self._effective_bbo(current)
        ready = best_bid > 0.0 and best_ask > best_bid and self._depth_initialized
        cache_expires = current + self.METRICS_CACHE_SECONDS
        if (
            self._book_ticker_time > -math.inf
            and current <= self._book_ticker_time + self.BBO_STALE_SECONDS
        ):
            cache_expires = min(
                cache_expires,
                self._book_ticker_time + self.BBO_STALE_SECONDS,
            )
        if math.isfinite(self._next_pending_cancellation_due):
            cache_expires = min(cache_expires, self._next_pending_cancellation_due)

        if not ready:
            result = OrderFlowMetrics(
                symbol=self.symbol,
                ready=False,
                tracked_levels=len(self._current_keys['bid']) + len(self._current_keys['ask']),
            )
            self._metrics_cache = result
            self._metrics_cache_revision = self._revision
            self._metrics_cache_expires = max(current, cache_expires)
            return result

        midpoint = (best_bid + best_ask) * 0.5
        spread_bps = (best_ask - best_bid) / midpoint * 10000.0
        size_total = bid_quantity + ask_quantity
        microprice = midpoint
        if size_total > 0.0:
            microprice = (best_ask * bid_quantity + best_bid * ask_quantity) / size_total
        microprice_bias_bps = (microprice - midpoint) / midpoint * 10000.0
        touch_total = bid_quantity + ask_quantity
        touch_imbalance_pct = (bid_quantity - ask_quantity) / touch_total * 100.0 if touch_total > 0.0 else 0.0

        trade_1s, trade_5s, trade_15s, trade_expiry = self._trade_windows(current)
        buy_1s, sell_1s, _normal_1s, _rpi_1s = trade_1s
        buy_5s, sell_5s, normal_5s, rpi_5s = trade_5s
        buy_15s, sell_15s, _normal_15s, _rpi_15s = trade_15s
        if math.isfinite(trade_expiry):
            cache_expires = min(cache_expires, trade_expiry)
        flow_total_5s = buy_5s + sell_5s
        aggressor_imbalance = (buy_5s - sell_5s) / flow_total_5s * 100.0 if flow_total_5s > 0.0 else 0.0
        rpi_base = normal_5s + rpi_5s
        rpi_share = rpi_5s / rpi_base * 100.0 if rpi_base > 0.0 else 0.0

        (
            added,
            cancelled,
            replenished,
            bid_added,
            bid_cancelled,
            ask_added,
            ask_cancelled,
            liquidity_expiry,
        ) = self._liquidity_window(current, 5.0)
        if math.isfinite(liquidity_expiry):
            cache_expires = min(cache_expires, liquidity_expiry)
        liquidity_turnover = added + cancelled
        add_cancel_pressure = (added - cancelled) / liquidity_turnover * 100.0 if liquidity_turnover > 0.0 else 0.0

        result = OrderFlowMetrics(
            symbol=self.symbol, ready=True, best_bid=best_bid, best_ask=best_ask,
            best_bid_quantity=bid_quantity, best_ask_quantity=ask_quantity, midpoint=midpoint,
            spread_bps=spread_bps, microprice=microprice, microprice_bias_bps=microprice_bias_bps,
            near_pressure_pct=self._near_pressure_pct, touch_imbalance_pct=touch_imbalance_pct,
            buy_notional_1s=buy_1s, sell_notional_1s=sell_1s,
            buy_notional_5s=buy_5s, sell_notional_5s=sell_5s,
            buy_notional_15s=buy_15s, sell_notional_15s=sell_15s,
            aggressor_imbalance_5s_pct=aggressor_imbalance, rpi_notional_5s=rpi_5s,
            rpi_share_5s_pct=rpi_share, added_notional_5s=added, cancelled_notional_5s=cancelled,
            replenished_notional_5s=replenished, add_cancel_pressure_5s_pct=add_cancel_pressure,
            bid_liquidity_delta_5s=bid_added - bid_cancelled,
            ask_liquidity_delta_5s=ask_added - ask_cancelled,
            tracked_levels=len(self._current_keys['bid']) + len(self._current_keys['ask']),
        )
        self._metrics_cache = result
        self._metrics_cache_revision = self._revision
        self._metrics_cache_expires = max(current, cache_expires)
        return result

    def _level_activity(self, state: _OrderFlowLevelState, now: float) -> tuple[float, float, float, float, float, float, int, int, float, float, float]:
        cutoff = now - self.LEVEL_ACTIVITY_SECONDS
        while state.activity and state.activity[0].time < cutoff:
            expired = state.activity.popleft()
            self._drop_activity_bucket(state, expired)
        return (
            max(0.0, state.activity_added_notional),
            max(0.0, state.activity_cancelled_notional),
            max(0.0, state.activity_executed_notional),
            max(0.0, state.activity_rpi_executed_notional),
            max(0.0, state.activity_replenished_notional),
            max(0.0, state.activity_restacked_notional),
            max(0, state.activity_trade_reload_count),
            max(0, state.activity_restack_count),
            max(0.0, state.activity_restacked_cancelled_notional),
            max(0.0, state.activity_reserved_notional),
            state.activity_visible_delta_notional,
        )

    def _recent_reference_peak(self, state: _OrderFlowLevelState, now: float) -> float:
        """Return a recent peak that decays toward current liquidity over time.

        ``max_notional`` remains the episode peak used for persistence. This
        separate reference prevents one old spike from permanently inflating
        short-horizon activity thresholds while retaining exact sub-second peaks.
        The existing 30-second liquidity-memory horizon is used as the half-life.
        """
        current = max(0.0, state.notional)
        peak = max(current, state.reference_peak_notional)
        if peak <= current or not math.isfinite(state.reference_peak_time):
            return peak
        elapsed = max(0.0, now - state.reference_peak_time)
        half_life = max(self.LIQUIDITY_HISTORY_SECONDS, 1e-09)
        decayed = peak * math.pow(0.5, elapsed / half_life)
        return max(current, decayed)

    def _level_metric_for_state(self, state: _OrderFlowLevelState, now: float, median_notional: float) -> OrderFlowLevelMetrics:
        (added, cancelled, executed, rpi_executed, replenished, restacked,
         trade_reload_count, restack_count, restacked_cancelled, pending_restack,
         visible_delta) = self._level_activity(state, now)
        self._analysis_revision += 1
        age = max(0.0, now - state.first_seen)
        episode_peak = max(state.max_notional, state.notional, 1e-09)
        persistence = state.notional / episode_peak
        recent_reference_peak = max(self._recent_reference_peak(state, now), state.notional, 1e-09)
        reference = max(state.notional, recent_reference_peak * 0.25, 1e-09)
        median_notional = max(median_notional, 1e-09)
        stack_pull_floor = median_notional * self.STATE_STACK_PULL_MEDIAN_FLOOR_RATIO
        execution_floor = median_notional * self.STATE_EXEC_MEDIAN_FLOOR_RATIO






        new_passive_added = max(0.0, added - replenished - restacked - pending_restack)
        effective_cancelled = max(0.0, cancelled - restacked_cancelled)

        absorbing_base = (
            executed >= reference * 0.2
            and replenished >= executed * 0.45
            and state.notional >= recent_reference_peak * 0.55
        )
        pulling_base = (
            effective_cancelled >= max(new_passive_added * 1.5, reference * 0.25)
            and effective_cancelled > 0.0
        )
        stacking_base = (
            new_passive_added >= max(effective_cancelled * 1.5, reference * 0.25)
            and new_passive_added > 0.0
        )
        depleting_base = (
            executed >= max(replenished * 1.75, reference * 0.25)
            and state.notional < recent_reference_peak * 0.55
        )
        persistent_base = (
            age >= 8.0
            and state.notional >= median_notional * 2.0
            and persistence >= 0.65
        )

        absorbing = absorbing_base and executed >= execution_floor
        pulling = pulling_base and effective_cancelled >= stack_pull_floor
        stacking = stacking_base and new_passive_added >= stack_pull_floor
        depleting = depleting_base and executed >= execution_floor
        persistent = persistent_base



        state_flags = tuple(
            label
            for label, active in (
                ('ABSORBING', absorbing),
                ('DEPLETING', depleting),
                ('PULLING', pulling),
                ('STACKING', stacking),
                ('PERSISTENT', persistent),
            )
            if active
        )
        if absorbing:
            label = 'ABSORBING'
        elif depleting:
            label = 'DEPLETING'
        elif pulling:
            label = 'PULLING'
        elif stacking:
            label = 'STACKING'
        elif persistent:
            label = 'PERSISTENT'
        else:
            label = 'NORMAL'

        key = (state.side, self._price_key(state.price))


        suppression_conditions = {
            'ABSORBING': absorbing_base and executed < execution_floor,
            'PULLING': pulling_base and effective_cancelled < stack_pull_floor,
            'STACKING': stacking_base and new_passive_added < stack_pull_floor,
            'DEPLETING': depleting_base and executed < execution_floor,
        }
        for suppressed_label, is_suppressed in suppression_conditions.items():
            episode_key = (state.side, key[1], suppressed_label)
            if is_suppressed:
                if episode_key not in self._state_suppression_active:
                    self._state_suppression_active.add(episode_key)
                    self._state_significance_suppressed[suppressed_label] = (
                        self._state_significance_suppressed.get(suppressed_label, 0) + 1
                    )
                    self._state_calibration_events.append((
                        'SUPPRESSED', float(now), state.side, float(state.price),
                        suppressed_label, float(state.notional), float(median_notional),
                        float(reference), float(new_passive_added), float(effective_cancelled),
                        float(executed), float(replenished), float(restacked), float(pending_restack),
                        float(persistence), float(recent_reference_peak),
                        float(age), float(episode_peak), float(added), float(cancelled),
                    ))
            else:
                self._state_suppression_active.discard(episode_key)

        previous_flags = self._state_last_flags.get(key, ())
        if state_flags != previous_flags:
            self._state_last_flags[key] = state_flags
            if len(state_flags) > 1:
                overlap_key = '+'.join(state_flags)
                self._state_overlap_counts[overlap_key] = self._state_overlap_counts.get(overlap_key, 0) + 1
            self._state_calibration_events.append((
                'QUALIFICATION', float(now), state.side, float(state.price),
                previous_flags, state_flags, float(state.notional), float(median_notional),
                float(reference), float(new_passive_added), float(effective_cancelled),
                float(executed), float(replenished), float(restacked), float(pending_restack),
                float(persistence), float(recent_reference_peak),
                float(age), float(episode_peak), float(added), float(cancelled),
            ))

        previous_label = self._state_last_classification.get(key, 'NORMAL')
        if label != previous_label:
            self._state_last_classification[key] = label
            if previous_label != 'NORMAL':
                self._state_exit_counts[previous_label] = self._state_exit_counts.get(previous_label, 0) + 1
            if label != 'NORMAL':
                self._state_signal_counts[label] = self._state_signal_counts.get(label, 0) + 1
            self._state_calibration_events.append((
                'TRANSITION', float(now), state.side, float(state.price), previous_label, label,
                float(state.notional), float(median_notional), float(reference),
                float(new_passive_added), float(effective_cancelled), float(executed),
                float(replenished), float(restacked), float(pending_restack), float(persistence),
                float(recent_reference_peak), state_flags,
                float(age), float(episode_peak), float(added), float(cancelled),
            ))

        return OrderFlowLevelMetrics(
            side=state.side, price=state.price, quantity=state.quantity,
            notional=state.notional, age_seconds=age, max_notional=state.max_notional,
            persistence_ratio=persistence, recent_added_notional=added,
            recent_cancelled_notional=cancelled, recent_executed_notional=executed,
            recent_rpi_executed_notional=rpi_executed,
            recent_replenished_notional=replenished, recent_restacked_notional=restacked,
            trade_reload_count=trade_reload_count, restack_count=restack_count,
            replenishments=state.replenishments, state=label, state_flags=state_flags,
            persistent=persistent, analysis_revision=self._analysis_revision,
            visible_delta_notional=visible_delta,
            new_passive_added_notional=new_passive_added,
            effective_cancelled_notional=effective_cancelled,
        )

    def _current_level_states(self, limit: int) -> tuple[dict[str, list[_OrderFlowLevelState]], dict[str, float]]:
        if self._current_level_sorted_revision != self._current_level_order_revision:
            sorted_states: dict[str, tuple[_OrderFlowLevelState, ...]] = {'bid': (), 'ask': ()}
            for side in ('bid', 'ask'):
                states = [
                    state
                    for key in self._current_keys[side]
                    if (state := self._levels.get((side, key))) is not None
                    and state.active
                    and state.quantity > 0.0
                ]
                states.sort(key=lambda state: state.price, reverse=side == 'bid')
                sorted_states[side] = tuple(states)
            self._current_level_sorted_states = sorted_states
            self._current_level_sorted_revision = self._current_level_order_revision

        current_states = {
            side: list(self._current_level_sorted_states[side][:limit])
            for side in ('bid', 'ask')
        }


        if self._side_median_cache_revision != self._current_level_value_revision:
            self._side_median_cache = {
                side: self._median([state.notional for state in states])
                for side, states in self._current_level_sorted_states.items()
            }
            self._side_median_cache_revision = self._current_level_value_revision
        return current_states, self._side_median_cache

    def _local_state_medians(
        self,
        side_medians: dict[str, float],
    ) -> dict[tuple[str, int | float], float]:
        """Return cached same-side local-liquidity baselines for active levels.

        The neighborhood only changes when active level order or notional values
        change, so repeated display snapshots can reuse it exactly.
        """
        if self._local_median_cache_revision == self._current_level_value_revision:
            return self._local_median_cache
        radius = max(1, int(self.STATE_LOCAL_MEDIAN_RADIUS))
        output: dict[tuple[str, int | float], float] = {}
        for side in ('bid', 'ask'):
            states = self._current_level_sorted_states[side]
            fallback = max(0.0, float(side_medians.get(side, 0.0)))
            count = len(states)
            for index, state in enumerate(states):
                start = max(0, index - radius)
                stop = min(count, index + radius + 1)
                neighbors = [
                    states[position].notional
                    for position in range(start, stop)
                    if position != index and states[position].notional > 0.0
                ]
                local = self._median(neighbors) if len(neighbors) >= 3 else fallback
                output[(side, self._price_key(state.price))] = max(local, 1e-09)
        self._local_median_cache = output
        self._local_median_cache_revision = self._current_level_value_revision
        return output

    def level_metrics(self, now: float | None=None, *, limit_per_side: int=40) -> tuple[OrderFlowLevelMetrics, ...]:
        current = time.monotonic() if now is None else float(now)
        self._finalize_pending_cancellations(current)
        self._trim(current)
        limit = max(1, min(int(limit_per_side), self.DEPTH_LEVEL_LIMIT))
        current_states, side_medians = self._current_level_states(limit)
        local_medians = self._local_state_medians(side_medians)
        output: list[OrderFlowLevelMetrics] = []
        for side in ('ask', 'bid'):
            for state in current_states[side]:
                key = (side, self._price_key(state.price))
                output.append(self._level_metric_for_state(
                    state, current, local_medians.get(key, side_medians[side])
                ))
        return tuple(output)

    @staticmethod
    def _age(now: float, stamp: float) -> float | None:
        if not math.isfinite(stamp):
            return None
        return max(0.0, now - stamp)

    @staticmethod
    def _robust_scale(values: list[float]) -> float:
        positives = sorted((abs(value) for value in values if abs(value) > 1e-12))
        if not positives:
            return 0.0
        index = int(round((len(positives) - 1) * 0.9))
        return max(positives[index], positives[-1] * 0.25, 1e-12)

    @staticmethod
    def _intensity(value: float, scale: float) -> float:
        if scale <= 0.0 or value == 0.0:
            return 0.0
        return math.sqrt(_clamp(abs(value) / scale))

    def _price_execution_window(self, price: float, now: float, seconds: float=5.0) -> tuple[float, float, float, float, int, int, float, float, int, int]:
        """Return exact-price executions with revision/time-boundary caching.

        Normal/RPI totals are accumulated in the same bucket walk so callers do
        not perform additional per-price scans for the same five-second window.
        """
        price_key = self._price_key(price)
        window = max(0.0, float(seconds))
        revision = self._price_print_revisions.get(price_key, 0)
        cache_key = (price_key, window)
        cached = self._price_execution_cache.get(cache_key)
        if cached is not None:
            cached_revision, built_at, expires_at, result = cached
            if cached_revision == revision and built_at <= now < expires_at:
                self._price_execution_cache_hits += 1
                return result

        buckets = self._price_activity.get(price_key)
        zero = (0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0, 0)
        if not buckets:
            self._price_execution_cache[cache_key] = (revision, now, math.inf, zero)
            return zero

        cutoff = now - window
        buy = sell = normal = rpi = 0.0
        buy_count = sell_count = 0
        largest_buy = largest_sell = 0.0
        rejected_buy = rejected_sell = 0
        oldest_included = math.inf
        for bucket in reversed(buckets):
            if bucket.time < cutoff:
                break
            oldest_included = bucket.time
            buy += bucket.buy
            sell += bucket.sell
            normal += bucket.normal
            rpi += bucket.rpi
            buy_count += bucket.buy_count
            sell_count += bucket.sell_count
            largest_buy = max(largest_buy, bucket.largest_buy)
            largest_sell = max(largest_sell, bucket.largest_sell)
            rejected_buy += bucket.rejected_buy
            rejected_sell += bucket.rejected_sell
        result = (
            buy, sell, normal, rpi, buy_count, sell_count,
            largest_buy, largest_sell, rejected_buy, rejected_sell,
        )
        expires_at = oldest_included + window if math.isfinite(oldest_included) else math.inf
        self._price_execution_cache[cache_key] = (revision, now, expires_at, result)
        return result

    def _synthetic_level_metric(self, price_key: int | float, now: float, *, forced_side: str | None=None) -> OrderFlowLevelMetrics | None:
        prints = self._price_prints.get(price_key)
        if not prints:
            return None
        cutoff = now - self.LEVEL_ACTIVITY_SECONDS
        latest = prints[-1]
        if latest.received_monotonic < cutoff:
            return None
        price = float(latest.price)
        if forced_side in {'bid', 'ask'}:
            side = str(forced_side)
        else:
            best_bid, best_ask, _bid_q, _ask_q = self._effective_bbo(now)
            midpoint = (best_bid + best_ask) * 0.5 if best_bid > 0.0 and best_ask > best_bid else 0.0
            if midpoint <= 0.0:
                return None
            if abs(price - midpoint) <= max(self.tick_size, 1e-12) * 0.5:
                side = 'ask' if latest.aggressor_side == 'buy' else 'bid'
            else:
                side = 'bid' if price < midpoint else 'ask'
        (
            buy_trade, sell_trade, normal_trade, rpi_trade, *_rest
        ) = self._price_execution_window(price, now, self.LEVEL_ACTIVITY_SECONDS)
        total_trade = buy_trade + sell_trade
        if total_trade <= 0.0:
            return None
        state = self._levels.get((side, price_key))
        visible_delta = self._level_activity(state, now)[-1] if state is not None else 0.0
        return OrderFlowLevelMetrics(
            side=side,
            price=price,
            quantity=0.0,
            notional=0.0,
            age_seconds=0.0,
            max_notional=0.0,
            persistence_ratio=0.0,
            recent_added_notional=0.0,
            recent_cancelled_notional=0.0,
            recent_executed_notional=normal_trade,
            recent_rpi_executed_notional=rpi_trade,
            recent_replenished_notional=0.0,
            recent_restacked_notional=0.0,
            trade_reload_count=0,
            restack_count=0,
            replenishments=0,
            state='NORMAL', visible_delta_notional=visible_delta,
        )

    def _display_base_from_metric(self, level: OrderFlowLevelMetrics, now: float) -> OrderFlowDisplayLevel:


        delta = level.visible_delta_notional
        state = self._levels.get((level.side, self._price_key(level.price)))
        if state is not None:
            history, history_presence, history_peak, history_mean = self._liquidity_history_summary(state, now)
        else:
            history, history_presence, history_peak, history_mean = ((), 0.0, 0.0, 0.0)
        (
            buy_trade, sell_trade, _normal_trade, rpi_trade,
            buy_count, sell_count, largest_buy, largest_sell,
            rejected_buy, rejected_sell,
        ) = self._price_execution_window(level.price, now, self.LEVEL_ACTIVITY_SECONDS)
        exact_trade = buy_trade + sell_trade
        semantic_event_kind, semantic_event_label = _order_flow_semantic_event(
            trade_reload_count=level.trade_reload_count,
            recent_replenished_notional=level.recent_replenished_notional,
            restack_count=level.restack_count,
            recent_restacked_notional=level.recent_restacked_notional,
            rejected_buy_prints=rejected_buy,
            rejected_sell_prints=rejected_sell,
        )
        return OrderFlowDisplayLevel(
            side=level.side,
            price=level.price,
            quantity=level.quantity,
            notional=level.notional,
            delta_notional_5s=delta,
            trade_notional_5s=exact_trade,
            signed_trade_notional_5s=buy_trade - sell_trade,
            rpi_trade_notional_5s=rpi_trade,
            age_seconds=level.age_seconds,
            persistence_ratio=level.persistence_ratio,
            replenishments=level.replenishments,
            state=level.state,
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
            recent_replenished_notional=level.recent_replenished_notional,
            recent_restacked_notional=level.recent_restacked_notional,
            trade_reload_count=level.trade_reload_count,
            restack_count=level.restack_count,
            semantic_event_kind=semantic_event_kind,
            semantic_event_label=semantic_event_label,
            state_flags=level.state_flags,
            persistent=level.persistent, analysis_revision=level.analysis_revision,
            new_passive_added_notional=level.new_passive_added_notional,
            effective_cancelled_notional=level.effective_cancelled_notional,
        )

    def _normalize_display_levels(self, bases: list[OrderFlowDisplayLevel]) -> tuple[tuple[OrderFlowDisplayLevel, ...], tuple[OrderFlowDisplayLevel, ...], tuple[float, float, float]]:
        notionals = [level.notional for level in bases]
        deltas = [level.delta_notional_5s for level in bases]
        trades = [level.trade_notional_5s for level in bases]
        liquidity_scale = self._robust_scale(notionals)
        delta_scale = self._robust_scale(deltas)
        trade_scale = self._robust_scale(trades)
        cumulative_by_side = {'bid': 0.0, 'ask': 0.0}
        max_cumulative_depth = 0.0
        cumulative_values: list[float] = []
        for level in bases:
            cumulative = cumulative_by_side[level.side] + max(0.0, level.notional)
            cumulative_by_side[level.side] = cumulative
            cumulative_values.append(cumulative)
            max_cumulative_depth = max(max_cumulative_depth, cumulative)
        depth_scale = max(max_cumulative_depth, 1e-09)
        bids: list[OrderFlowDisplayLevel] = []
        asks: list[OrderFlowDisplayLevel] = []
        for level, cumulative in zip(bases, cumulative_values):
            display = replace(
                level,
                liquidity_intensity=self._intensity(level.notional, liquidity_scale),
                delta_intensity=self._intensity(level.delta_notional_5s, delta_scale),
                trade_intensity=self._intensity(level.trade_notional_5s, trade_scale),
                cumulative_depth_notional=cumulative,
                depth_intensity=math.sqrt(_clamp(cumulative / depth_scale)),
            )
            if display.side == 'bid':
                bids.append(display)
            else:
                asks.append(display)
        return tuple(bids), tuple(asks), (liquidity_scale, delta_scale, trade_scale)

    def _reuse_normalized_display_levels(
        self,
        bases: list[OrderFlowDisplayLevel],
        dirty: set[int | float],
    ) -> tuple[tuple[OrderFlowDisplayLevel, ...], tuple[OrderFlowDisplayLevel, ...], tuple[float, float, float]]:
        """Reuse global normalization when dirty rows did not alter its inputs."""
        previous = {
            (level.side, self._price_key(level.price)): level
            for level in (*self._snapshot_bid_levels, *self._snapshot_ask_levels)
        }
        bids: list[OrderFlowDisplayLevel] = []
        asks: list[OrderFlowDisplayLevel] = []
        for base in bases:
            key = (base.side, self._price_key(base.price))
            old = previous.get(key)
            if old is None:
                return self._normalize_display_levels(bases)
            if self._price_key(base.price) in dirty:
                display = replace(
                    base,
                    liquidity_intensity=old.liquidity_intensity,
                    delta_intensity=old.delta_intensity,
                    trade_intensity=old.trade_intensity,
                    cumulative_depth_notional=old.cumulative_depth_notional,
                    depth_intensity=old.depth_intensity,
                )
            else:
                display = old
            if display.side == 'bid':
                bids.append(display)
            else:
                asks.append(display)
        return tuple(bids), tuple(asks), self._snapshot_level_scales

    def _partial_refresh_snapshot_levels(self, now: float) -> tuple[tuple[OrderFlowDisplayLevel, ...], tuple[OrderFlowDisplayLevel, ...], tuple[float, float, float]]:
        dirty = set(self._snapshot_dirty_price_keys)
        if not dirty:
            self._snapshot_level_cache_hits += 1
            return self._snapshot_bid_levels, self._snapshot_ask_levels, self._snapshot_level_scales

        bases = self._snapshot_base_levels
        if not bases:
            self._snapshot_level_cache_revision = -1
            return self._snapshot_levels(now, self._snapshot_level_cache_limit or self.DEFAULT_SNAPSHOT_LEVEL_LIMIT)

        normalization_changed = False
        structural_refresh_required = False
        for price_key in dirty:
            for index in self._snapshot_base_indices_by_price.get(price_key, ()):
                existing = bases[index]
                state = self._levels.get((existing.side, price_key))
                metric: OrderFlowLevelMetrics | None = None
                if state is not None and state.active and state.quantity > 0.0:
                    metric = self._level_metric_for_state(
                        state,
                        now,
                        self._snapshot_local_medians.get(
                            (existing.side, price_key),
                            self._snapshot_side_medians.get(existing.side, 0.0),
                        ),
                    )
                else:
                    metric = self._synthetic_level_metric(price_key, now, forced_side=existing.side)
                if metric is None:


                    structural_refresh_required = True
                    break
                updated = self._display_base_from_metric(metric, now)
                if (
                    not math.isclose(updated.notional, existing.notional, rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(updated.delta_notional_5s, existing.delta_notional_5s, rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(updated.trade_notional_5s, existing.trade_notional_5s, rel_tol=0.0, abs_tol=1e-12)
                ):
                    normalization_changed = True
                bases[index] = updated
            if structural_refresh_required:
                break

        if structural_refresh_required:
            self._snapshot_level_cache_revision = -1
            return self._snapshot_levels(
                now, self._snapshot_level_cache_limit or self.DEFAULT_SNAPSHOT_LEVEL_LIMIT
            )

        if normalization_changed:
            bids, asks, scales = self._normalize_display_levels(bases)
        else:
            bids, asks, scales = self._reuse_normalized_display_levels(bases, dirty)
        self._snapshot_bid_levels = bids
        self._snapshot_ask_levels = asks
        self._snapshot_level_scales = scales
        self._snapshot_dirty_price_keys.difference_update(dirty)
        self._snapshot_level_partial_refreshes += 1
        return bids, asks, scales

    def _snapshot_levels(self, now: float, limit_per_side: int) -> tuple[tuple[OrderFlowDisplayLevel, ...], tuple[OrderFlowDisplayLevel, ...], tuple[float, float, float]]:
        limit = max(1, min(int(limit_per_side), self.SNAPSHOT_LEVEL_LIMIT))
        analysis_limit = min(limit, self.DEPTH_LEVEL_LIMIT)
        source_cache_fresh = (
            limit <= self.DEPTH_LEVEL_LIMIT
            or self._snapshot_source_depth_revision == self._source_depth_revision
        )
        structure_cache_valid = (
            self._snapshot_level_cache_revision == self._level_revision
            and self._snapshot_level_cache_limit == limit
            and source_cache_fresh
        )
        cache_fresh = (
            structure_cache_valid
            and now - self._snapshot_level_cache_time < self.SNAPSHOT_LEVEL_REFRESH_SECONDS
        )
        current_states: dict[str, list[_OrderFlowLevelState]] | None = None
        side_medians: dict[str, float] | None = None
        local_medians: dict[tuple[str, int | float], float] | None = None
        if structure_cache_valid:
            current_states, side_medians = self._current_level_states(analysis_limit)
            local_medians = self._local_state_medians(side_medians)
            active_side_keys = {
                side: tuple(self._price_key(state.price) for state in current_states[side])
                for side in ('bid', 'ask')
            }
            if active_side_keys == self._snapshot_active_side_keys:
                previous_local_medians = self._snapshot_local_medians
                self._snapshot_side_medians = side_medians
                self._snapshot_local_medians = local_medians


                for side in ('bid', 'ask'):
                    for price_key in active_side_keys[side]:
                        key = (side, price_key)
                        if not math.isclose(
                            float(previous_local_medians.get(key, 0.0)),
                            float(local_medians.get(key, 0.0)),
                            rel_tol=1e-12,
                            abs_tol=1e-9,
                        ):
                            self._snapshot_dirty_price_keys.add(price_key)
                if not cache_fresh:



                    self._snapshot_dirty_price_keys.update(self._snapshot_analyzed_price_keys)
                result = self._partial_refresh_snapshot_levels(now)
                if not cache_fresh:
                    self._snapshot_level_cache_time = now
                return result

        if current_states is None or side_medians is None:
            current_states, side_medians = self._current_level_states(analysis_limit)
        if local_medians is None:
            local_medians = self._local_state_medians(side_medians)
        self._snapshot_side_medians = side_medians
        self._snapshot_local_medians = local_medians
        self._snapshot_active_side_keys = {
            side: tuple(self._price_key(state.price) for state in current_states[side])
            for side in ('bid', 'ask')
        }
        metric_list: list[OrderFlowLevelMetrics] = []
        for side in ('ask', 'bid'):
            for state in current_states[side]:
                key = (side, self._price_key(state.price))
                metric_list.append(self._level_metric_for_state(
                    state, now, local_medians.get(key, side_medians[side])
                ))
        active_prices = {self._price_key(level.price) for level in metric_list}
        best_bid, best_ask, _bid_q, _ask_q = self._effective_bbo(now)
        bid_prices = [level.price for level in metric_list if level.side == 'bid']
        ask_prices = [level.price for level in metric_list if level.side == 'ask']
        far_bid = min(bid_prices, default=best_bid)
        far_ask = max(ask_prices, default=best_ask)
        cutoff = now - self.LEVEL_ACTIVITY_SECONDS
        for price_key, stamp in reversed(self._recent_price_keys.items()):
            if stamp < cutoff:
                break
            if price_key in active_prices:
                continue
            synthetic = self._synthetic_level_metric(price_key, now)
            if synthetic is None:
                continue
            if synthetic.side == 'bid':
                if far_bid > 0.0 and synthetic.price < far_bid - max(self.tick_size, 1e-12):
                    continue
            elif far_ask > 0.0 and synthetic.price > far_ask + max(self.tick_size, 1e-12):
                continue
            metric_list.append(synthetic)
            active_prices.add(price_key)

        bids_metrics = sorted((level for level in metric_list if level.side == 'bid'), key=lambda level: level.price, reverse=True)[:analysis_limit]
        asks_metrics = sorted((level for level in metric_list if level.side == 'ask'), key=lambda level: level.price)[:analysis_limit]
        analyzed_bids = [self._display_base_from_metric(level, now) for level in bids_metrics]
        analyzed_asks = [self._display_base_from_metric(level, now) for level in asks_metrics]
        self._snapshot_analyzed_price_keys = {
            self._price_key(level.price) for level in (*analyzed_bids, *analyzed_asks)
        }
        if limit > self.DEPTH_LEVEL_LIMIT:
            analyzed_bids = self._extend_snapshot_with_source_depth(
                analyzed_bids, self._source_depth_bids, 'bid', limit
            )
            analyzed_asks = self._extend_snapshot_with_source_depth(
                analyzed_asks, self._source_depth_asks, 'ask', limit
            )
        bases = [*analyzed_asks, *analyzed_bids]
        bids, asks, scales = self._normalize_display_levels(bases)
        self._snapshot_bid_levels = bids
        self._snapshot_ask_levels = asks
        self._snapshot_level_scales = scales
        self._snapshot_base_levels = list(bases)
        base_indices: dict[int | float, list[int]] = {}
        for index, level in enumerate(self._snapshot_base_levels):
            base_indices.setdefault(self._price_key(level.price), []).append(index)
        self._snapshot_base_indices_by_price = {
            price_key: tuple(indices)
            for price_key, indices in base_indices.items()
        }
        self._snapshot_level_cache_revision = self._level_revision
        self._snapshot_source_depth_revision = self._source_depth_revision
        self._snapshot_level_cache_time = now
        self._snapshot_level_cache_limit = limit
        self._snapshot_cached_price_keys = {self._price_key(level.price) for level in (*bids, *asks)}
        self._snapshot_dirty_price_keys.clear()
        self._snapshot_level_full_refreshes += 1
        return bids, asks, scales

    def _recent_prints_snapshot(self, now: float) -> tuple[OrderFlowTradePrint, ...]:
        if self._recent_prints_cache_revision == self._print_revision and now < self._recent_prints_cache_expires:
            self._recent_print_cache_hits += 1
            return self._recent_prints_cache
        cutoff = now - self.PRINT_HISTORY_SECONDS




        output = tuple(self._recent_prints)
        start = 0
        output_count = len(output)
        while start < output_count and output[start].received_monotonic < cutoff:
            start += 1
        if start:
            output = output[start:]
        if output and self._print_outcomes:
            first_sequence = output[0].sequence
            last_sequence = output[-1].sequence
            mutable: list[OrderFlowTradePrint] | None = None
            for sequence, outcome in self._print_outcomes.items():
                if sequence < first_sequence or sequence > last_sequence:
                    continue
                index = sequence - first_sequence
                if index < 0 or index >= len(output):
                    continue
                trade = output[index]


                if trade.sequence != sequence or trade.outcome == outcome:
                    continue
                if mutable is None:
                    mutable = list(output)
                mutable[index] = replace(trade, outcome=outcome)
            if mutable is not None:
                output = tuple(mutable)
        self._recent_prints_cache = output
        self._recent_prints_cache_revision = self._print_revision
        self._recent_prints_cache_expires = (
            output[0].received_monotonic + self.PRINT_HISTORY_SECONDS
            if output
            else math.inf
        )
        return self._recent_prints_cache

    def snapshot(self, now: float | None=None, *, limit_per_side: int | None=None) -> OrderFlowSnapshot:
        """Return one immutable, display-ready order-flow frame.

        The expensive temporal row analysis is cached for a short interval and
        invalidated by depth/trade mutations.  High-rate ``bookTicker`` events
        can therefore update BBO/microprice at display cadence without forcing
        a complete per-level temporal scan on each quote.
        """
        current = time.monotonic() if now is None else float(now)
        limit = self.DEFAULT_SNAPSHOT_LEVEL_LIMIT if limit_per_side is None else limit_per_side
        metrics = self.metrics(current)
        bids, asks, scales = self._snapshot_levels(current, limit)
        depth_age = self._age(current, self._last_depth_time)
        book_age = self._age(current, self._book_ticker_time)
        trade_age = self._age(current, self._last_trade_time)
        using_book = book_age is not None and book_age <= self.BBO_STALE_SECONDS and (self._book_bid > 0.0) and (self._book_ask > self._book_bid)
        bbo_source = 'bookTicker' if using_book else 'depth' if self._depth_initialized else 'none'
        bbo_age = book_age if using_book else depth_age
        live = bool(metrics.ready and book_data_is_fresh(depth_age, bbo_age))
        normal_5s = max(0.0, metrics.buy_notional_5s + metrics.sell_notional_5s - metrics.rpi_notional_5s)
        recent_activity = any((value > 1e-09 for value in (metrics.buy_notional_15s, metrics.sell_notional_15s, metrics.added_notional_5s, metrics.cancelled_notional_5s, metrics.replenished_notional_5s)))
        self._snapshot_sequence += 1
        return OrderFlowSnapshot(symbol=self.symbol, sequence=self._snapshot_sequence, data_revision=self._revision, generated_monotonic=current, ready=metrics.ready, live=live, bbo_source=bbo_source, depth_age_seconds=depth_age, bbo_age_seconds=bbo_age, trade_age_seconds=trade_age, best_bid=metrics.best_bid, best_ask=metrics.best_ask, best_bid_quantity=metrics.best_bid_quantity, best_ask_quantity=metrics.best_ask_quantity, midpoint=metrics.midpoint, spread=max(0.0, metrics.best_ask - metrics.best_bid), spread_bps=metrics.spread_bps, microprice=metrics.microprice, microprice_bias_bps=metrics.microprice_bias_bps, near_pressure_pct=metrics.near_pressure_pct, touch_imbalance_pct=metrics.touch_imbalance_pct, buy_notional_1s=metrics.buy_notional_1s, sell_notional_1s=metrics.sell_notional_1s, buy_notional_5s=metrics.buy_notional_5s, sell_notional_5s=metrics.sell_notional_5s, buy_notional_15s=metrics.buy_notional_15s, sell_notional_15s=metrics.sell_notional_15s, aggressor_imbalance_5s_pct=metrics.aggressor_imbalance_5s_pct, normal_notional_5s=normal_5s, rpi_notional_5s=metrics.rpi_notional_5s, rpi_share_5s_pct=metrics.rpi_share_5s_pct, added_notional_5s=metrics.added_notional_5s, cancelled_notional_5s=metrics.cancelled_notional_5s, replenished_notional_5s=metrics.replenished_notional_5s, add_cancel_pressure_5s_pct=metrics.add_cancel_pressure_5s_pct, bid_liquidity_delta_5s=metrics.bid_liquidity_delta_5s, ask_liquidity_delta_5s=metrics.ask_liquidity_delta_5s, has_recent_activity=recent_activity, liquidity_scale=scales[0], delta_scale=scales[1], trade_scale=scales[2], cumulative_depth_scale=max(bids[-1].cumulative_depth_notional if bids else 0.0, asks[-1].cumulative_depth_notional if asks else 0.0), large_trade_threshold=max(1.0, self._adaptive_print_threshold(current)), recent_prints=self._recent_prints_snapshot(current), bid_levels=bids, ask_levels=asks)

    def state_calibration_snapshot(self) -> dict[str, Any]:
        """Return bounded event-count telemetry for offline threshold calibration.

        Counters increment on observed episode changes, never on every poll.
        Short episodes between analyses can still be missed; compare calibration
        runs at a consistent analysis cadence.
        """
        return {
            'signal_entries': dict(self._state_signal_counts),
            'signal_exits': dict(self._state_exit_counts),
            'suppression_episodes': dict(self._state_significance_suppressed),

            'signal_transitions': dict(self._state_signal_counts),
            'significance_suppressed': dict(self._state_significance_suppressed),
            'stack_pull_median_floor_ratio': self.STATE_STACK_PULL_MEDIAN_FLOOR_RATIO,
            'execution_median_floor_ratio': self.STATE_EXEC_MEDIAN_FLOOR_RATIO,
            'local_median_radius': self.STATE_LOCAL_MEDIAN_RADIUS,
            'reference_peak_half_life_seconds': self.LIQUIDITY_HISTORY_SECONDS,
            'overlap_entries': dict(self._state_overlap_counts),
            'event_schema_version': 2,
            'activity_bucket_seconds': self.LEVEL_ACTIVITY_BUCKET_SECONDS,
            'recent_events': tuple(self._state_calibration_events),
        }

    def diagnostic_state(self) -> dict[str, Any]:
        """Small deterministic state summary used by tests and diagnostics."""
        return {'symbol': self.symbol, 'tick_size': self.tick_size, 'trade_buckets': len(self.trade_buckets), 'liquidity_buckets': len(self.liquidity_buckets), 'level_states': len(self._levels), 'pending_executions': len(self._pending_exec), 'recent_executions': sum((len(queue) for queue in self._recent_executions.values())), 'pending_cancellations': sum((len(queue) for queue in self._pending_cancellations.values())), 'recent_finalized_cancellations': sum((len(queue) for queue in self._recent_finalized_cancellations.values())), 'current_bid_levels': len(self._current_keys['bid']), 'current_ask_levels': len(self._current_keys['ask']), 'depth_capacity': self._depth_capacity, 'source_bid_levels': len(self._source_depth_bids), 'source_ask_levels': len(self._source_depth_asks), 'source_depth_update_id': self._source_depth_update_id, 'depth_initialized': self._depth_initialized, 'revision': self._revision, 'snapshot_sequence': self._snapshot_sequence, 'level_revision': self._level_revision, 'dirty_snapshot_prices': len(self._snapshot_dirty_price_keys), 'recent_price_keys': len(self._recent_price_keys), 'print_revision': self._print_revision, 'unresolved_prints': len(self._unresolved_prints), 'unresolved_print_capacity': self.UNRESOLVED_PRINT_CAPACITY, 'unresolved_print_evictions': self._unresolved_print_evictions, 'recent_print_cache_revision': self._recent_prints_cache_revision, 'snapshot_level_cache_hits': self._snapshot_level_cache_hits, 'current_level_order_revision': self._current_level_order_revision, 'current_level_sorted_revision': self._current_level_sorted_revision, 'snapshot_base_cache_levels': len(self._snapshot_base_levels), 'snapshot_level_partial_refreshes': self._snapshot_level_partial_refreshes, 'snapshot_level_full_refreshes': self._snapshot_level_full_refreshes, 'recent_print_cache_hits': self._recent_print_cache_hits, 'metrics_cache_hits': self._metrics_cache_hits, 'metrics_cache_revision': self._metrics_cache_revision, 'metrics_cache_expires': self._metrics_cache_expires, 'state_signal_transitions': sum(self._state_signal_counts.values()), 'state_signal_exits': sum(self._state_exit_counts.values()), 'state_significance_suppressed': sum(self._state_significance_suppressed.values()), 'state_active_suppressions': len(self._state_suppression_active), 'state_tracked_classifications': len(self._state_last_classification), 'state_overlap_entries': sum(self._state_overlap_counts.values()), 'price_execution_cache_hits': self._price_execution_cache_hits, 'price_execution_cache_entries': len(self._price_execution_cache), 'liquidity_history_cache_hits': self._liquidity_history_cache_hits, 'liquidity_history_cache_entries': len(self._liquidity_history_cache), 'last_depth_time': self._last_depth_time, 'last_trade_time': self._last_trade_time, 'book_ticker_time': self._book_ticker_time, 'book_ticker_update_id': self._book_ticker_update_id, 'last_trade_id': self._last_trade_id}


import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from ..models import MicrostructureSnapshot



@dataclass
class TradeBucket:
    time: float
    buy: float = 0.0
    sell: float = 0.0
    count: int = 0
    first_price: float = 0.0
    last_price: float = 0.0

@dataclass
class DepthSample:
    time: float
    bid_depth: float
    ask_depth: float
    best_bid: float
    best_ask: float
    midpoint: float

class MicrostructureAnalyzer:
    """Aggregate one symbol's executions and top-five depth into sparse signals.

    Displayed liquidity is never treated as a signal by itself.  An emitted event
    requires unusual *executed* flow, a matching price response, and either book
    depletion/follow-through or absorption followed by a confirmed reversal.
    """
    FLOW_WINDOW = 5.0
    DEPTH_WINDOW = 5.0
    TRADE_BUCKET_SECONDS = 0.25
    EVALUATION_SECONDS = 0.2
    RELATIVE_VOLUME_MIN = 2.6
    PERSISTENCE_SECONDS = 1.2
    GLOBAL_SIGNAL_SECONDS = 20.0
    REPEAT_SIGNAL_SECONDS = 90.0

    def __init__(self, symbol: str):
        self.trade_buckets: deque[TradeBucket] = deque(maxlen=240)
        self.depth_samples: deque[DepthSample] = deque(maxlen=120)
        self.symbol = ''
        self.last_evaluation = 0.0
        self.last_depth_sample = -math.inf
        self.last_depth_event = -math.inf
        self.signal_id = 0
        self.candidate_key = ''
        self.candidate_since = 0.0
        self.emitted_candidate_key = ''
        self.candidate_clear_since = 0.0
        self.last_any_signal_time = -math.inf
        self.last_signal_times: dict[str, float] = {}
        self.reset(symbol)

    def reset(self, symbol: str) -> None:
        self.symbol = str(symbol).upper()
        self.trade_buckets.clear()
        self.depth_samples.clear()
        self.last_evaluation = 0.0
        self.last_depth_sample = -math.inf
        self.last_depth_event = -math.inf
        self.signal_id = 0
        self.candidate_key = ''
        self.candidate_since = 0.0
        self.emitted_candidate_key = ''
        self.candidate_clear_since = 0.0
        self.last_any_signal_time = -math.inf
        self.last_signal_times.clear()

    def _add_trade(self, event: dict[str, Any], current: float) -> bool:
        if str(event.get('s') or '').upper() != self.symbol:
            return False
        price = _number(event.get('p'))
        quantity = _number(event.get('q'))
        notional = price * quantity
        if price <= 0 or notional <= 0:
            return False
        bucket_time = math.floor(current / self.TRADE_BUCKET_SECONDS) * self.TRADE_BUCKET_SECONDS
        if not self.trade_buckets or self.trade_buckets[-1].time != bucket_time:
            self.trade_buckets.append(TradeBucket(bucket_time))
        bucket = self.trade_buckets[-1]
        if not bucket.first_price:
            bucket.first_price = price
        bucket.last_price = price
        bucket.count += 1
        if bool(event.get('m')):
            bucket.sell += notional
        else:
            bucket.buy += notional
        return True

    def add_trade(self, event: dict[str, Any], now: float | None=None) -> MicrostructureSnapshot | None:
        current = time.monotonic() if now is None else float(now)
        if not self._add_trade(event, current):
            return None
        self._trim(current)
        return self._evaluate_if_due(current)

    def add_trade_batch(self, events: Iterable[dict[str, Any]], now: float | None=None) -> MicrostructureSnapshot | None:
        current = time.monotonic() if now is None else float(now)
        changed = False
        for event in events:
            changed = self._add_trade(event, current) or changed
        if not changed:
            return None
        self._trim(current)
        return self._evaluate_if_due(current)

    def _break_depth_continuity(self) -> None:
        """Discard comparisons that would span an unobserved/stale book interval."""
        self.depth_samples.clear()
        self.last_depth_sample = -math.inf
        self.candidate_key = ''
        self.candidate_since = 0.0
        self.emitted_candidate_key = ''
        self.candidate_clear_since = 0.0

    def add_depth(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], now: float | None=None) -> MicrostructureSnapshot | None:
        if not bids or not asks:
            return None
        current = time.monotonic() if now is None else float(now)
        shown_bids = [(float(price), float(quantity)) for price, quantity in bids[:5]]
        shown_asks = [(float(price), float(quantity)) for price, quantity in asks[:5]]
        best_bid = shown_bids[0][0]
        best_ask = shown_asks[0][0]
        if best_bid <= 0 or best_ask <= best_bid:
            return None
        if math.isfinite(self.last_depth_event) and current - self.last_depth_event > BOOK_DEPTH_FRESH_SECONDS:
            self._break_depth_continuity()
        self.last_depth_event = current
        sample = DepthSample(time=current, bid_depth=sum((price * quantity for price, quantity in shown_bids)), ask_depth=sum((price * quantity for price, quantity in shown_asks)), best_bid=best_bid, best_ask=best_ask, midpoint=(best_bid + best_ask) * 0.5)
        if current - self.last_depth_sample >= 0.18:
            self.depth_samples.append(sample)
            self.last_depth_sample = current
        self._trim(current)
        return self._evaluate_if_due(current)

    def _trim(self, now: float) -> None:
        while self.trade_buckets and now - self.trade_buckets[0].time > 60.0:
            self.trade_buckets.popleft()
        while self.depth_samples and now - self.depth_samples[0].time > self.DEPTH_WINDOW:
            self.depth_samples.popleft()

    def _evaluate_if_due(self, now: float) -> MicrostructureSnapshot | None:
        if now - self.last_evaluation < self.EVALUATION_SECONDS:
            return None
        self.last_evaluation = now
        return self.snapshot(now)

    def _trade_window(self, now: float, seconds: float) -> tuple[float, float, int]:
        cutoff = now - seconds
        buy = sell = 0.0
        count = 0
        for bucket in reversed(self.trade_buckets):
            if bucket.time < cutoff:
                break
            buy += bucket.buy
            sell += bucket.sell
            count += bucket.count
        return (buy, sell, count)

    def _expected_flow(self, now: float, current_total: float) -> tuple[float, float]:
        start = now - 25.0
        end = now - self.FLOW_WINDOW
        previous = 0.0
        oldest_time: float | None = None
        for bucket in reversed(self.trade_buckets):
            if bucket.time >= end:
                continue
            if bucket.time < start:
                break
            previous += bucket.buy + bucket.sell
            oldest_time = bucket.time
        if oldest_time is None:
            return (current_total, 0.0)
        covered_seconds = min(20.0, max(0.0, end - oldest_time))
        if previous <= 0 or covered_seconds <= 0:
            return (current_total, covered_seconds)
        return (previous * self.FLOW_WINDOW / covered_seconds, covered_seconds)

    @staticmethod
    def _side_metrics(samples: deque[DepthSample], side: str) -> tuple[float, float, float, float, float]:
        first = samples[0]
        last = samples[-1]
        if side == 'bid':
            start = max(first.bid_depth, 1e-09)
            end = last.bid_depth
            minimum = min((sample.bid_depth for sample in samples))
            first_price = first.best_bid
            last_price = last.best_bid
        else:
            start = max(first.ask_depth, 1e-09)
            end = last.ask_depth
            minimum = min((sample.ask_depth for sample in samples))
            first_price = first.best_ask
            last_price = last.best_ask
        drawdown = max(0.0, (start - minimum) / start)
        loss = max(start - minimum, 0.0)
        recovery = _clamp((end - minimum) / loss) if loss > start * 0.005 else 0.0
        best_move_bps = (last_price / max(first_price, 1e-09) - 1.0) * 10000.0
        return (start, end, drawdown, recovery, best_move_bps)

    @staticmethod
    def _market_noise(samples: deque[DepthSample]) -> tuple[float, float, float]:
        first = samples[0]
        last = samples[-1]
        midpoint_move = (last.midpoint / max(first.midpoint, 1e-09) - 1.0) * 10000.0
        squared = 0.0
        iterator = iter(samples)
        previous = next(iterator)
        for current in iterator:
            squared += math.log(current.midpoint / previous.midpoint) ** 2
            previous = current
        realized_bps = math.sqrt(squared) * 10000.0
        spread_bps = (last.best_ask - last.best_bid) / max(last.midpoint, 1e-09) * 10000.0
        tolerance = max(0.5, spread_bps * 1.5, realized_bps * 0.35)
        return (midpoint_move, spread_bps, tolerance)

    @staticmethod
    def _absorption_score(aggressor_share: float, traded_to_depth: float, drawdown: float, recovery: float, adverse_move: float, tolerance: float) -> int:
        """Continuous absorption evidence; signal validity is owned by snapshot()."""
        score = 100.0 * (
            0.2 * _clamp((aggressor_share - 0.52) / 0.22)
            + 0.2 * _clamp(traded_to_depth / 0.35)
            + 0.15 * _clamp(drawdown / 0.15)
            + 0.25 * _clamp((recovery - 0.4) / 0.5)
            + 0.2 * _clamp(1.0 - adverse_move / max(tolerance, 1e-09))
        )
        return int(round(score))

    @staticmethod
    def _depletion_score(aggressor_share: float, traded_to_depth: float, depth_drop: float, recovery: float, favorable_best_move: float, adverse_move: float, tolerance: float, spread_bps: float) -> int:
        """Continuous depletion evidence; signal validity is owned by snapshot()."""
        score = 100.0 * (
            0.22 * _clamp((aggressor_share - 0.52) / 0.22)
            + 0.23 * _clamp(traded_to_depth / 0.35)
            + 0.25 * _clamp(depth_drop / 0.35)
            + 0.15 * _clamp((0.55 - recovery) / 0.55)
            + 0.1 * _clamp(favorable_best_move / max(spread_bps, 0.5))
            + 0.05 * _clamp(1.0 - adverse_move / max(tolerance, 1e-09))
        )
        return int(round(score))

    def snapshot(self, now: float | None=None) -> MicrostructureSnapshot:
        current = time.monotonic() if now is None else float(now)
        buy, sell, trade_count = self._trade_window(current, self.FLOW_WINDOW)
        total = buy + sell
        imbalance = (buy - sell) / total * 100.0 if total > 0 else 0.0
        expected, baseline_seconds = self._expected_flow(current, total)
        intensity = total / max(expected, 1e-09) if total > 0 else 0.0
        depth_age = None if not math.isfinite(self.last_depth_event) else max(0.0, current - self.last_depth_event)
        depth_live = depth_age is not None and depth_age <= BOOK_DEPTH_FRESH_SECONDS
        if not depth_live and self.depth_samples:
            self._break_depth_continuity()
        samples = self.depth_samples
        if not depth_live or len(samples) < 4 or trade_count < 8 or total <= 0:
            return MicrostructureSnapshot(
                symbol=self.symbol,
                ready=False,
                headline='',
                buy_notional=buy,
                sell_notional=sell,
                imbalance_pct=imbalance,
                flow_intensity=intensity,
                depth_age_seconds=depth_age,
                live=False,
            )
        bid_start, bid_end, bid_drawdown, bid_recovery, bid_move = self._side_metrics(samples, 'bid')
        ask_start, ask_end, ask_drawdown, ask_recovery, ask_move = self._side_metrics(samples, 'ask')
        price_move, spread_bps, tolerance = self._market_noise(samples)
        buy_share = buy / total
        sell_share = sell / total
        buy_to_ask_depth = buy / max(ask_start, 1e-09)
        sell_to_bid_depth = sell / max(bid_start, 1e-09)
        flow_to_depth = total / max((bid_start + ask_start) * 0.5, 1e-09)
        active_flow = intensity >= 0.55 or flow_to_depth >= 0.05
        pressure_threshold = max(14.0, min(22.0, 22.0 - 4.0 * math.log(max(intensity, 1.0))))
        pressure = 'CALIBRATING'
        if baseline_seconds >= 15.0:
            pressure = 'NEUTRAL'
            if active_flow and imbalance >= pressure_threshold:
                pressure = 'BULLISH'
            elif active_flow and imbalance <= -pressure_threshold:
                pressure = 'BEARISH'

        bid_absorption_raw = self._absorption_score(sell_share, sell_to_bid_depth, bid_drawdown, bid_recovery, max(0.0, -price_move), tolerance)
        ask_absorption_raw = self._absorption_score(buy_share, buy_to_ask_depth, ask_drawdown, ask_recovery, max(0.0, price_move), tolerance)
        ask_depth_drop = max(0.0, (ask_start - ask_end) / max(ask_start, 1e-09))
        bid_depth_drop = max(0.0, (bid_start - bid_end) / max(bid_start, 1e-09))
        bullish_depletion_raw = self._depletion_score(buy_share, buy_to_ask_depth, ask_depth_drop, ask_recovery, max(0.0, ask_move), max(0.0, -price_move), tolerance, spread_bps)
        bearish_depletion_raw = self._depletion_score(sell_share, sell_to_bid_depth, bid_depth_drop, bid_recovery, max(0.0, -bid_move), max(0.0, price_move), tolerance, spread_bps)

        relative_volume_high = baseline_seconds >= 15.0 and intensity >= self.RELATIVE_VOLUME_MIN
        minimum_response = max(tolerance * 1.25, spread_bps * 0.9, 0.35)
        reversal_response = max(tolerance * 0.35, spread_bps * 0.35, 0.18)




        bullish_breakout = (
            relative_volume_high and trade_count >= 20 and buy_share >= 0.68
            and flow_to_depth >= 0.1 and price_move >= minimum_response
            and ask_depth_drop >= 0.2 and ask_recovery <= 0.45
            and ask_move >= max(0.25, spread_bps * 0.35)
        )
        bearish_breakdown = (
            relative_volume_high and trade_count >= 20 and sell_share >= 0.68
            and flow_to_depth >= 0.1 and price_move <= -minimum_response
            and bid_depth_drop >= 0.2 and bid_recovery <= 0.45
            and bid_move <= -max(0.25, spread_bps * 0.35)
        )
        bullish_absorption = (
            relative_volume_high and trade_count >= 20 and sell_share >= 0.68
            and sell_to_bid_depth >= 0.16 and bid_drawdown >= 0.08
            and bid_recovery >= 0.72 and price_move >= reversal_response
            and bid_move >= 0.0 and bid_absorption_raw >= 70
        )
        bearish_absorption = (
            relative_volume_high and trade_count >= 20 and buy_share >= 0.68
            and buy_to_ask_depth >= 0.16 and ask_drawdown >= 0.08
            and ask_recovery >= 0.72 and price_move <= -reversal_response
            and ask_move <= 0.0 and ask_absorption_raw >= 70
        )

        bid_absorption = bid_absorption_raw if bullish_absorption else min(bid_absorption_raw, 69)
        ask_absorption = ask_absorption_raw if bearish_absorption else min(ask_absorption_raw, 69)
        bullish_depletion = bullish_depletion_raw if bullish_breakout else min(bullish_depletion_raw, 69)
        bearish_depletion = bearish_depletion_raw if bearish_breakdown else min(bearish_depletion_raw, 69)

        def confluence_score(base: int, displacement: float, depth_move: float) -> int:
            return min(99, int(round(72.0 + 10.0 * _clamp((base - 70.0) / 25.0) + 7.0 * _clamp((intensity - self.RELATIVE_VOLUME_MIN) / 2.4) + 6.0 * _clamp(abs(displacement) / max(minimum_response * 2.0, 1e-09)) + 4.0 * _clamp(depth_move / 0.4))))
        qualified: list[tuple[int, str]] = []
        if bullish_breakout:
            qualified.append((confluence_score(bullish_depletion, price_move, ask_depth_drop), 'BULLISH BREAKOUT'))
        if bearish_breakdown:
            qualified.append((confluence_score(bearish_depletion, price_move, bid_depth_drop), 'BEARISH BREAKDOWN'))
        if bullish_absorption:
            qualified.append((confluence_score(bid_absorption, price_move, bid_drawdown), 'BID ABSORPTION'))
        if bearish_absorption:
            qualified.append((confluence_score(ask_absorption, price_move, ask_drawdown), 'ASK ABSORPTION'))
        signal_key = ''
        signal_sentence = ''
        signal_score = 0
        signal_anchor_side = ''
        signal_anchor_price = 0.0
        signal_anchor_midpoint = 0.0
        if qualified:
            score, name = max(qualified)
            if name != self.candidate_key:
                self.candidate_key = name
                self.candidate_since = current
            self.candidate_clear_since = 0.0
            if current - self.candidate_since >= self.PERSISTENCE_SECONDS and name != self.emitted_candidate_key and (current - self.last_any_signal_time >= self.GLOBAL_SIGNAL_SECONDS) and (current - self.last_signal_times.get(name, -math.inf) >= self.REPEAT_SIGNAL_SECONDS):
                self.signal_id += 1
                self.emitted_candidate_key = name
                self.last_any_signal_time = current
                self.last_signal_times[name] = current
                signal_key = name
                signal_score = score
                latest_depth = samples[-1]
                signal_anchor_side = 'bid' if name in {'BID ABSORPTION', 'BEARISH BREAKDOWN'} else 'ask'
                signal_anchor_price = float(latest_depth.best_bid) if signal_anchor_side == 'bid' else float(latest_depth.best_ask)
                signal_anchor_midpoint = float(latest_depth.midpoint)
                symbol = f'{self.symbol}.P'
                if name == 'BID ABSORPTION':
                    signal_sentence = f'{symbol} · BULLISH ABSORPTION · aggressive selling was absorbed, bids refilled and price reversed · {intensity:.1f}× activity'
                elif name == 'ASK ABSORPTION':
                    signal_sentence = f'{symbol} · BEARISH ABSORPTION · aggressive buying was absorbed, asks refilled and price reversed · {intensity:.1f}× activity'
                elif name == 'BULLISH BREAKOUT':
                    signal_sentence = f'{symbol} · BULLISH BREAKOUT · exceptional market buying cleared asks with price follow-through · {intensity:.1f}× activity'
                else:
                    signal_sentence = f'{symbol} · BEARISH BREAKDOWN · exceptional market selling cleared bids with price follow-through · {intensity:.1f}× activity'
        else:
            self.candidate_key = ''
            self.candidate_since = 0.0
            if not self.candidate_clear_since:
                self.candidate_clear_since = current
            elif current - self.candidate_clear_since >= 2.0:
                self.emitted_candidate_key = ''
        return MicrostructureSnapshot(
            symbol=self.symbol,
            ready=True,
            headline=signal_sentence,
            buy_notional=buy,
            sell_notional=sell,
            imbalance_pct=imbalance,
            pressure=pressure,
            flow_intensity=intensity,
            bid_absorption=bid_absorption,
            ask_absorption=ask_absorption,
            bullish_depletion=bullish_depletion,
            bearish_depletion=bearish_depletion,
            bid_replenishment_pct=bid_recovery * 100.0,
            ask_replenishment_pct=ask_recovery * 100.0,
            bid_depth_change_pct=(bid_end / bid_start - 1.0) * 100.0,
            ask_depth_change_pct=(ask_end / ask_start - 1.0) * 100.0,
            price_change_bps=price_move,
            best_bid_move_bps=bid_move,
            best_ask_move_bps=ask_move,
            signal_id=self.signal_id if signal_sentence else 0,
            signal_key=signal_key,
            signal_sentence=signal_sentence,
            signal_score=signal_score,
            signal_anchor_side=signal_anchor_side,
            signal_anchor_price=signal_anchor_price,
            signal_anchor_midpoint=signal_anchor_midpoint,
            depth_age_seconds=depth_age,
            live=True,
        )


import math
import time

from PySide6 import QtCore
from PySide6.QtCore import Qt

from ..models import OrderFlowPresentationFrame


class _LocalOrderFlowRuntime(QtCore.QObject):
    """Thread-owned, latest-wins runtime around :class:`OrderFlowAnalyzer`."""

    DIAGNOSTIC_EMIT_INTERVAL_SECONDS = 1.0

    snapshot_ready = QtCore.Signal(int, object)
    microstructure_ready = QtCore.Signal(int, object)
    diagnostic_ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)

    def __init__(
        self,
        symbol: str,
        *,
        tick_size: float = 0.0,
        quote_volume: float = 0.0,
        min_snapshot_interval_ms: int = 16,
        interaction_snapshot_interval_ms: int = 33,
        active_decay_ms: int = 500,
        idle_decay_ms: int = 1000,
    ) -> None:
        super().__init__()
        self._analyzer = OrderFlowAnalyzer(symbol, tick_size=tick_size)
        self._analyzer.set_quote_volume(quote_volume)
        self._microstructure = MicrostructureAnalyzer(symbol)
        self._generation = 0
        self._active = False
        self._dirty = True
        self._dirty_since_mono = 0.0
        self._last_build_mono = 0.0


        self._latest_depth_timing: dict[str, float] = {}
        self._depth_ingress_id = 0
        self._base_snapshot_interval_ms = max(1, int(min_snapshot_interval_ms))
        self._interaction_snapshot_interval_ms = max(
            self._base_snapshot_interval_ms,
            int(interaction_snapshot_interval_ms),
        )
        self._interaction_priority = False
        self._snapshot_level_limit = self._analyzer.DEFAULT_SNAPSHOT_LEVEL_LIMIT
        self._last_diagnostic_emit_mono = 0.0
        self._active_decay_ms = max(1, int(active_decay_ms))
        self._idle_decay_ms = max(1, int(idle_decay_ms))

        self._snapshot_timer = QtCore.QTimer(self)
        self._snapshot_timer.setSingleShot(True)
        self._snapshot_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._snapshot_timer.timeout.connect(self._flush_snapshot)

        self._decay_timer = QtCore.QTimer(self)
        self._decay_timer.setSingleShot(True)
        self._decay_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._decay_timer.timeout.connect(self._on_decay_due)

    @QtCore.Slot(int, str, float, float)
    def reset_model(
        self,
        generation: int,
        symbol: str,
        tick_size: float,
        quote_volume: float,
    ) -> None:
        self._generation = int(generation)
        self._snapshot_timer.stop()
        self._decay_timer.stop()
        self._analyzer.reset(symbol, tick_size=tick_size)
        self._analyzer.set_quote_volume(quote_volume)
        self._microstructure.reset(symbol)
        self._last_build_mono = 0.0
        self._latest_depth_timing.clear()
        self._depth_ingress_id = 0
        self._dirty = True
        self._dirty_since_mono = time.perf_counter()
        self._emit_diagnostic_state(force=True)
        if self._active:
            self._schedule_snapshot(immediate=True)

    @QtCore.Slot(int, bool)
    def set_active(self, generation: int, active: bool) -> None:
        if int(generation) != self._generation:
            return
        active = bool(active)
        if active == self._active:
            if active and self._dirty:
                self._schedule_snapshot(immediate=True)
            return
        self._active = active
        if not active:
            self._snapshot_timer.stop()
            self._decay_timer.stop()
            return
        self._dirty = True
        if self._dirty_since_mono <= 0.0:
            self._dirty_since_mono = time.perf_counter()
        self._schedule_snapshot(immediate=True)

    @QtCore.Slot(int, float)
    def set_quote_volume(self, generation: int, quote_volume: float) -> None:
        if int(generation) != self._generation:
            return
        self._analyzer.set_quote_volume(quote_volume)

    @QtCore.Slot(int, int)
    def set_depth_capacity(self, generation: int, limit_per_side: int) -> None:
        if int(generation) != self._generation:
            return
        previous = self._snapshot_level_limit
        resolved = self._analyzer.set_depth_capacity(limit_per_side)
        self._snapshot_level_limit = resolved
        if resolved != previous:
            self._mark_dirty()

    @QtCore.Slot(int, object, object, int, object)
    def add_depth(
        self,
        generation: int,
        bids: object,
        asks: object,
        source_revision: int = 0,
        timing: object = None,
    ) -> None:
        if int(generation) != self._generation:
            return
        timing_enabled = isinstance(timing, dict)
        worker_received_mono = time.perf_counter() if timing_enabled else 0.0
        try:
            changed = bool(
                self._analyzer.add_depth(
                    bids, asks, source_revision=int(source_revision or 0)
                )
            )
        except Exception as exc:
            self.failed.emit(self._generation, f"order-flow depth failed: {exc}")
        else:
            worker_depth_processed_mono = time.perf_counter() if timing_enabled else 0.0
            if changed:
                self._depth_ingress_id += 1
                if timing_enabled:
                    normalized_timing: dict[str, float] = {}
                    for key, value in timing.items():
                        try:
                            numeric = float(value)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if math.isfinite(numeric) and numeric > 0.0:
                            normalized_timing[str(key)] = numeric
                    normalized_timing["depth_ingress_id"] = float(self._depth_ingress_id)
                    normalized_timing["worker_received_mono"] = worker_received_mono
                    normalized_timing["worker_depth_processed_mono"] = worker_depth_processed_mono
                    self._latest_depth_timing = normalized_timing
                else:


                    self._latest_depth_timing.clear()
                self._mark_dirty()
        try:
            microstructure = self._microstructure.add_depth(bids, asks)
        except Exception as exc:
            self.failed.emit(self._generation, f"microstructure depth failed: {exc}")
        else:
            if microstructure is not None:
                self.microstructure_ready.emit(self._generation, microstructure)

    @QtCore.Slot(int, object)
    def add_book_ticker(self, generation: int, payload: object) -> None:
        if int(generation) != self._generation or not isinstance(payload, dict):
            return
        try:
            changed = bool(self._analyzer.add_book_ticker(payload))
        except Exception as exc:
            self.failed.emit(self._generation, f"order-flow BBO failed: {exc}")
            return
        if changed:
            self._mark_dirty()

    @QtCore.Slot(int, object)
    def add_trade_batch(self, generation: int, payloads: object) -> None:
        if int(generation) != self._generation:
            return
        try:
            changed = bool(self._analyzer.add_trade_batch(payloads))
        except Exception as exc:
            self.failed.emit(self._generation, f"order-flow trades failed: {exc}")
        else:
            if changed:
                self._mark_dirty()
        try:
            microstructure = self._microstructure.add_trade_batch(payloads)
        except Exception as exc:
            self.failed.emit(self._generation, f"microstructure trades failed: {exc}")
        else:
            if microstructure is not None:
                self.microstructure_ready.emit(self._generation, microstructure)

    def _snapshot_interval_ms(self) -> int:
        return (
            self._interaction_snapshot_interval_ms
            if self._interaction_priority
            else self._base_snapshot_interval_ms
        )

    @QtCore.Slot(int, bool)
    def set_interaction_priority(self, generation: int, active: bool) -> None:
        if int(generation) != self._generation:
            return
        active = bool(active)
        if active == self._interaction_priority:
            return
        self._interaction_priority = active
        if not self._active or not self._dirty:
            return



        if self._snapshot_timer.isActive():
            self._snapshot_timer.stop()
        self._schedule_snapshot()

    @QtCore.Slot()
    def shutdown(self) -> None:
        self._active = False
        self._snapshot_timer.stop()
        self._decay_timer.stop()

    def _mark_dirty(self) -> None:
        if self._dirty_since_mono <= 0.0:
            self._dirty_since_mono = time.perf_counter()
        self._dirty = True
        if self._active:
            self._schedule_snapshot()

    def _schedule_snapshot(self, *, immediate: bool = False) -> None:
        if not self._active or not self._dirty:
            return
        now = time.perf_counter()
        if immediate or self._last_build_mono <= 0.0:
            delay_ms = 0
        else:
            due = self._last_build_mono + self._snapshot_interval_ms() / 1000.0
            delay_ms = max(0, int(math.ceil((due - now) * 1000.0)))
        if self._snapshot_timer.isActive():
            remaining = self._snapshot_timer.remainingTime()
            if remaining >= 0 and remaining <= delay_ms:
                return
        self._snapshot_timer.start(delay_ms)

    @QtCore.Slot()
    def _flush_snapshot(self) -> None:
        if not self._active or not self._dirty:
            return
        now = time.perf_counter()
        if self._last_build_mono > 0.0:
            due = self._last_build_mono + self._snapshot_interval_ms() / 1000.0
            if now + 1e-12 < due:
                self._schedule_snapshot()
                return

        generation = self._generation
        self._dirty = False
        dirty_since = self._dirty_since_mono
        self._dirty_since_mono = 0.0
        started = time.perf_counter()
        try:
            snapshot = self._analyzer.snapshot(limit_per_side=self._snapshot_level_limit)
        except Exception as exc:
            self._dirty = True
            if self._dirty_since_mono <= 0.0:
                self._dirty_since_mono = dirty_since or started
            self.failed.emit(generation, f"order-flow snapshot failed: {exc}")
            self._last_build_mono = time.perf_counter()
            self._schedule_snapshot()
            return
        completed = time.perf_counter()
        self._last_build_mono = completed



        frame = OrderFlowPresentationFrame(
            snapshot=snapshot,
            build_started_mono=started,
            build_completed_mono=completed,
        )
        depth_timing = dict(self._latest_depth_timing)
        if depth_timing:



            self.snapshot_ready.emit(generation, (frame, depth_timing))
        else:
            self.snapshot_ready.emit(generation, frame)
        self._emit_diagnostic_state()

        if snapshot.ready or snapshot.has_recent_activity:
            decay_ms = (
                self._active_decay_ms
                if snapshot.has_recent_activity
                else self._idle_decay_ms
            )
            self._decay_timer.start(decay_ms)



        if self._dirty:
            self._schedule_snapshot()

    @QtCore.Slot()
    def _on_decay_due(self) -> None:
        if not self._active:
            return
        self._mark_dirty()

    def _emit_diagnostic_state(self, *, force: bool=False) -> None:



        now = time.perf_counter()
        if (
            not force
            and self._last_diagnostic_emit_mono > 0.0
            and now - self._last_diagnostic_emit_mono < self.DIAGNOSTIC_EMIT_INTERVAL_SECONDS
        ):
            return
        try:
            state = self._analyzer.diagnostic_state()
        except Exception:
            return
        self._last_diagnostic_emit_mono = now
        self.diagnostic_ready.emit(self._generation, state)




import multiprocessing
import threading


class _OrderBookProcessLink(QtCore.QObject):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    MAX_PENDING_BATCHES = 1024
    MAX_PENDING_BYTES = 16 * 1024 * 1024
    MAX_QUEUE_AGE_SECONDS = 1.5

    def __init__(self, factory, options, parent=None, *, ordered=False):
        super().__init__(parent)
        self._factory, self._options = factory, options
        self._ordered = ordered
        self._condition = threading.Condition()
        self._pending = [] if ordered else OrderedDict()
        self._closed = False
        self._enabled = False
        self._awaiting_consumer = False
        self._lease = None
        self._due = float('inf')
        self._thread = None
        self._pending_bytes = 0
        self._oldest_pending = 0.0
        self.destroyed.connect(lambda *_: self.close())

    def enable(self, active):
        with self._condition:
            self._enabled = bool(active)
            if active and self._thread is None and not self._closed:
                self._thread = threading.Thread(target=self._run, name='nightwatch-book-ipc', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def submit(self, name, value):
        with self._condition:
            if self._closed:
                return
            if self._ordered:
                size = 256
                if name == 'add_depth':
                    size += sum(len(levels) for levels in value[1:3]) * 64
                elif name == 'add_trade_batch':
                    size += len(value[1]) * 512
                now = time.monotonic()
                stale = bool(self._oldest_pending and now - self._oldest_pending > self.MAX_QUEUE_AGE_SECONDS)
                if len(self._pending) >= self.MAX_PENDING_BATCHES or self._pending_bytes + size > self.MAX_PENDING_BYTES or stale:
                    self._pending.clear()
                    self._pending_bytes = 0
                    self._closed = True
                    self.failed.emit('Order-flow input exceeded its memory/age budget; resetting inference and restarting the worker')
                else:
                    if not self._pending:
                        self._oldest_pending = now
                    self._pending.append((name, value))
                    self._pending_bytes += size
            else:
                self._pending[name] = value
            self._condition.notify_all()

    def consumed(self, *, lease=None):
        with self._condition:
            self._lease = lease
            self._awaiting_consumer = False
            self._condition.notify_all()

    def close(self):

        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _run(self):
        process = connection = None
        try:
            context = multiprocessing.get_context('spawn')
            connection, child = context.Pipe()
            process = context.Process(target=_orderbook_process_main,
                                      args=(child, self._factory, self._options),
                                      name='nightwatch-book-process', daemon=True)
            process.start()
            child.close()
            while True:
                with self._condition:
                    while not self._closed:
                        now = time.monotonic()
                        if self._enabled and not self._awaiting_consumer and (self._pending or now >= self._due):
                            break
                        timeout = max(0.001, self._due - now) if self._enabled and not self._awaiting_consumer else None
                        self._condition.wait(None if timeout is None or not math.isfinite(timeout) else timeout)
                    if self._closed:
                        break
                    commands = list(self._pending) if self._ordered else list(self._pending.items())
                    self._pending.clear()
                    self._pending_bytes = 0
                    self._oldest_pending = 0.0
                    lease = self._lease
                connection.send((commands, lease))
                while not connection.poll(0.05):
                    if self._closed:
                        return
                    if not process.is_alive():
                        raise RuntimeError(f'Orderbook worker exited ({process.exitcode})')
                result = connection.recv()
                if 'error' in result:
                    raise RuntimeError(result['error'])

                mapper = result.pop('_map', None)
                if mapper is not None:
                    result = mapper(result)
                result['worker_pid'] = process.pid
                with self._condition:


                    self._due = result.get('next_at', float('inf'))
                    self._awaiting_consumer = True
                if self._closed:
                    break
                try:
                    self.ready.emit(result)
                except RuntimeError:
                    break
        except Exception as exc:
            if not self._closed:
                try:
                    self.failed.emit(str(exc))
                except RuntimeError:
                    pass
        finally:
            with self._condition:
                self._closed = True
            if connection is not None:
                try:
                    connection.send(None)
                except (OSError, EOFError, BrokenPipeError):
                    pass
                connection.close()
            if process is not None and process.pid is not None:
                process.join(0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(0.5)
                process.close()


def _orderbook_process_main(connection, factory, options):
    worker = None
    try:

        import os
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[key] = '1'
        worker = factory(options)
        while True:
            request = connection.recv()
            if request is None:
                break
            connection.send(worker.step(*request))
    except EOFError:
        pass
    except Exception:
        import traceback
        try:
            connection.send({'error': traceback.format_exc()})
        except (OSError, EOFError):
            pass
    finally:
        if worker is not None:
            worker.close()
        connection.close()


class _OrderFlowAnalysisProcess:
    def __init__(self, options):
        self.application = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        self.runtime = _LocalOrderFlowRuntime(**options)
        self.events = []
        for name in ('snapshot_ready', 'microstructure_ready', 'diagnostic_ready', 'failed'):
            getattr(self.runtime, name).connect(
                lambda generation, value, name=name: self.events.append((name, generation, value)))

    def step(self, commands, _lease):
        for name, args in commands:
            getattr(self.runtime, name)(*args)
        self.application.processEvents()
        events, self.events = self.events, []
        due = [max(1, timer.remainingTime()) for timer in
               (self.runtime._snapshot_timer, self.runtime._decay_timer) if timer.isActive()]
        return {'events': events, 'next_at': time.monotonic() + min(due, default=float('inf')) / 1000.0}

    def close(self):
        self.runtime.shutdown()


class OrderFlowRuntime(QtCore.QObject):
    """Qt-compatible ingress relay for an isolated, lossless analysis process.

    Public signals/slots and generation checks retain the existing interface.
    Depth/trade events are ordered, never latest-wins: only derived presentation
    frames may be coalesced. Pickle, IPC and process lifecycle run off Qt.
    """
    snapshot_ready = QtCore.Signal(int, object)
    microstructure_ready = QtCore.Signal(int, object)
    diagnostic_ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    restarted = QtCore.Signal(int)

    def __init__(self, symbol, *, tick_size=0.0, quote_volume=0.0,
                 min_snapshot_interval_ms=16, interaction_snapshot_interval_ms=33,
                 active_decay_ms=500, idle_decay_ms=1000):
        super().__init__()
        self._generation = 0
        options = dict(symbol=symbol, tick_size=tick_size, quote_volume=quote_volume,
                       min_snapshot_interval_ms=min_snapshot_interval_ms,
                       interaction_snapshot_interval_ms=interaction_snapshot_interval_ms,
                       active_decay_ms=active_decay_ms, idle_decay_ms=idle_decay_ms)
        self._options = options
        self._stopping = False
        self._restart_pending = False
        self._restart_attempts = 0
        self._last_restart_mono = 0.0
        self._reset_args = (0, symbol, tick_size, quote_volume)
        self._active = False
        self._depth_capacity = 1000
        self._interaction_priority = False
        self._restart_timer = QtCore.QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._restart)
        self._link = _OrderBookProcessLink(_OrderFlowAnalysisProcess, options, self, ordered=True)
        self._link.ready.connect(self._deliver)
        self._link.failed.connect(self._failed)

    def _post(self, name, *args):
        if self._stopping or self._restart_pending:
            return
        self._link.submit(name, args)
        self._link.enable(True)

    @QtCore.Slot(object)
    def _deliver(self, result):
        try:
            for name, generation, value in result['events']:
                if generation == self._generation:
                    getattr(self, name).emit(generation, value)
        finally:
            self._link.consumed()

    @QtCore.Slot(str)
    def _failed(self, message):
        if self._stopping or self._restart_pending:
            return
        self._restart_pending = True
        self._link.close()
        self._restart_attempts = self._restart_attempts + 1 if time.monotonic() - self._last_restart_mono < 60 else 1
        self._last_restart_mono = time.monotonic()
        self._restart_timer.start(min(30_000, 500 * 2 ** min(self._restart_attempts - 1, 6)))
        self.failed.emit(self._generation, message)

    @QtCore.Slot()
    def _restart(self):
        if self._stopping:
            return
        old = self._link
        old.ready.disconnect(self._deliver)
        old.failed.disconnect(self._failed)
        self._link = _OrderBookProcessLink(_OrderFlowAnalysisProcess, self._options, self, ordered=True)
        self._link.ready.connect(self._deliver)
        self._link.failed.connect(self._failed)
        self._restart_pending = False
        self._post('reset_model', *self._reset_args)
        self._post('set_depth_capacity', self._generation, self._depth_capacity)
        self._post('set_active', self._generation, self._active)
        self._post('set_interaction_priority', self._generation, self._interaction_priority)
        self.restarted.emit(self._generation)

    @QtCore.Slot(int, str, float, float)
    def reset_model(self, generation, symbol, tick_size, quote_volume):
        self._generation = int(generation)
        self._reset_args = (generation, symbol, tick_size, quote_volume)
        self._post('reset_model', generation, symbol, tick_size, quote_volume)

    @QtCore.Slot(int, bool)
    def set_active(self, generation, active):
        self._active = bool(active)
        self._post('set_active', generation, active)

    @QtCore.Slot(int, float)
    def set_quote_volume(self, generation, quote_volume):
        self._reset_args = (*self._reset_args[:3], quote_volume)
        self._post('set_quote_volume', generation, quote_volume)

    @QtCore.Slot(int, int)
    def set_depth_capacity(self, generation, limit_per_side):
        self._depth_capacity = limit_per_side
        self._post('set_depth_capacity', generation, limit_per_side)

    @QtCore.Slot(int, object, object, int, object)
    def add_depth(self, generation, bids, asks, source_revision=0, timing=None):
        self._post('add_depth', generation, bids, asks, source_revision, timing)

    @QtCore.Slot(int, object)
    def add_book_ticker(self, generation, payload):
        self._post('add_book_ticker', generation, payload)

    @QtCore.Slot(int, object)
    def add_trade_batch(self, generation, payloads):
        self._post('add_trade_batch', generation, payloads)

    @QtCore.Slot(int, bool)
    def set_interaction_priority(self, generation, active):
        self._interaction_priority = bool(active)
        self._post('set_interaction_priority', generation, active)

    @QtCore.Slot()
    def shutdown(self):
        self.stop_transport()

    def stop_transport(self):
        self._stopping = True
        self._link.close()
