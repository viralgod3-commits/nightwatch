"""Shared domain values, numerical validation and candle matrix conversion."""
from __future__ import annotations


import math
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from .constants import INTERVAL_SECONDS


def shift_candle_time(open_time: float, interval: str, steps: int = 1) -> float:
    """Shift exchange candle opens, saturating backward searches at the epoch.

    INTERVAL_SECONDS['1M'] is a drawing width, not a calendar duration. Monthly
    opens and closes are the first of each UTC month, including leap years.
    """
    if interval != "1M":
        return max(0.0, float(open_time) + INTERVAL_SECONDS[interval] * steps)
    opened = datetime.fromtimestamp(float(open_time), timezone.utc)
    month_index = opened.year * 12 + opened.month - 1 + steps
    if month_index < 1970 * 12:
        return 0.0
    year, month = divmod(month_index, 12)
    return datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def human_number(value: float, money: bool = False) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    prefix = "$" if money else ""
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= threshold:
            return f"{sign}{prefix}{value / threshold:.2f}{suffix}"
    return f"{sign}{prefix}{value:,.2f}"


def format_price(value: float) -> str:
    """Magnitude-based fallback for contexts without instrument precision."""
    value = safe_float(value)
    if value >= 10000:
        return f"{value:,.1f}"
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def parse_compact_amount(value: str) -> float:
    text = value.strip().upper().replace("$", "").replace(",", "").replace(" ", "")
    if not text or text in {"NONE", "OFF", "NOFILTER"}:
        return 0.0
    multiplier = 1.0
    if text[-1:] in {"K", "M", "B", "T"}:
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[text[-1]]
        text = text[:-1]
    amount = float(text) * multiplier
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("Volume must be a positive number such as 10M or 1B.")
    return amount


def perpetual_display_symbol(symbol: str) -> str:
    normalized = symbol.upper().strip().removesuffix(".P")
    return f"{normalized}.P" if normalized else "—"


def chart_y(value: float, logarithmic: bool) -> float:
    return math.log10(max(value, 1e-300)) if logarithmic else value


def raw_price(value: float, logarithmic: bool) -> float:
    return 10.0 ** max(-300.0, min(300.0, value)) if logarithmic else value


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d/%m/%y · %H:%M:%S UTC")


def quantize_step(value: str, step: str, *, rounding: str = ROUND_DOWN, offset: str = '0') -> str:
    try:
        number = Decimal(value)
        quantum = Decimal(step)
        origin = Decimal(str(offset))
        if not number.is_finite() or not quantum.is_finite() or not origin.is_finite() or number <= 0 or number < origin or quantum < 0:
            raise InvalidOperation
        if quantum == 0:
            return format(number.normalize(), 'f')
        rounded = origin + ((number - origin) / quantum).to_integral_value(rounding=rounding) * quantum
        return format(rounded.normalize(), "f")
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid positive quantity or price.")


def validate_step(value: str, step: str, label: str, *, offset: str = '0') -> str:
    """Validate exchange precision without changing a value the user entered."""
    try:
        number = Decimal(str(value).strip())
        quantum = Decimal(str(step).strip())
        origin = Decimal(str(offset))
        if not number.is_finite() or number <= 0 or not quantum.is_finite() or not origin.is_finite() or quantum < 0:
            raise InvalidOperation
        if quantum == 0:
            return format(number, 'f')
        units = (number - origin) / quantum
        if units != units.to_integral_value():
            raise ValueError(
                f"{label} must follow increments of {format(quantum.normalize(), 'f')} from {format(origin.normalize(), 'f')}. "
                "The value was not rounded or changed."
            )
        return format(number, "f")
    except ValueError:
        raise
    except (InvalidOperation, TypeError):
        raise ValueError(f"Enter a valid positive {label.lower()}.")


from dataclasses import dataclass


@dataclass(slots=True)
class Candle:
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float

    @classmethod
    def from_rest(cls, row: list[Any]) -> Candle:
        return cls(
            time=safe_float(row[0]) / 1000.0,
            open=safe_float(row[1]),
            high=safe_float(row[2]),
            low=safe_float(row[3]),
            close=safe_float(row[4]),
            volume=safe_float(row[5]),
            quote_volume=safe_float(row[7]),
        )

    @classmethod
    def from_stream(cls, row: dict[str, Any]) -> Candle:
        return cls(
            time=safe_float(row.get("t")) / 1000.0,
            open=safe_float(row.get("o")),
            high=safe_float(row.get("h")),
            low=safe_float(row.get("l")),
            close=safe_float(row.get("c")),
            volume=safe_float(row.get("v")),
            quote_volume=safe_float(row.get("q")),
        )


@dataclass(slots=True)
class Zone:
    low: float
    high: float
    score: float
    timeframe: str
    touches: int
    kind: str = "support"
    source: str = "SWING"


@dataclass(slots=True)
class SymbolRules:
    tick_size: str = "0.01"
    lot_step: str = "0.001"
    market_step: str = "0.001"
    min_price: float = 0.0
    max_price: float = float("inf")
    min_qty: float = 0.0
    max_qty: float = float("inf")
    min_market_qty: float = 0.0
    max_market_qty: float = float("inf")
    min_notional: float = 0.0
    price_multiplier_up: float = 0.0
    price_multiplier_down: float = 0.0
    quote_asset: str = "USDT"
    margin_asset: str = "USDT"


@dataclass(slots=True)
class PriceAlert:
    symbol: str
    level: float
    direction: str
    active: bool = True

import numpy as np


def _candle_matrix_from_objects(candles: list[Candle]) -> np.ndarray:
    """Fallback for history/cache paths that still own Candle objects."""
    if isinstance(candles, CandlePages):
        return candles.to_matrix()
    count = len(candles)
    if not count:
        return np.empty((0, 7), dtype=np.float64)
    flat = np.fromiter(
        (
            value
            for candle in candles
            for value in (
                candle.time,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.quote_volume,
            )
        ),
        dtype=np.float64,
        count=count * 7,
    )
    return flat.reshape((count, 7))


def _coerce_candle_matrix(candles: Any) -> np.ndarray:
    if isinstance(candles, np.ndarray):
        if candles.ndim == 2 and candles.shape[1] >= 7:
            return np.ascontiguousarray(candles[:, :7], dtype=np.float64)
        return np.empty((0, 7), dtype=np.float64)
    if isinstance(candles, CandlePages):
        return candles.to_matrix()
    return _candle_matrix_from_objects(list(candles))


def chart_y_array(values: np.ndarray, logarithmic: bool) -> np.ndarray:
    if not logarithmic:
        return values
    output = np.full_like(values, np.nan, dtype=float)
    valid = np.isfinite(values) & (values > 0)
    output[valid] = np.log10(values[valid])
    return output


from dataclasses import field
from typing import Callable, Protocol

class DiagnosticsPort(Protocol):
    """Optional instrumentation supplied by bootstrap, never a runtime import."""

    def increment(self, key: str, amount: int = 1) -> None: ...
    def observe_ms(self, key: str, value: float) -> None: ...
    def warning(self, category: str, message: str) -> None: ...
    def error(self, category: str, message: str) -> None: ...


ORDER_FLOW_AGGREGATION_MULTIPLIERS: tuple[int, ...] = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
BOOK_DEPTH_FRESH_SECONDS = 1.5
BOOK_BBO_FRESH_SECONDS = 2.0


def book_data_is_fresh(depth_age: float | None, bbo_age: float | None) -> bool:
    """Execution freshness is distinct from sequence synchronization."""
    return (
        depth_age is not None and 0.0 <= depth_age <= BOOK_DEPTH_FRESH_SECONDS
        and bbo_age is not None and 0.0 <= bbo_age <= BOOK_BBO_FRESH_SECONDS
    )

@dataclass(frozen=True)
class MicrostructureSnapshot:
    symbol: str
    ready: bool
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    imbalance_pct: float = 0.0
    flow_intensity: float = 0.0
    bid_absorption: int = 0
    ask_absorption: int = 0
    bullish_depletion: int = 0
    bearish_depletion: int = 0
    bid_replenishment_pct: float = 0.0
    ask_replenishment_pct: float = 0.0
    bid_depth_change_pct: float = 0.0
    ask_depth_change_pct: float = 0.0
    price_change_bps: float = 0.0
    signal_id: int = 0
    signal_key: str = ''
    signal_sentence: str = ''
    signal_score: int = 0
    signal_anchor_side: str = ''
    signal_anchor_price: float = 0.0
    depth_age_seconds: float | None = None
    live: bool = False


@dataclass(frozen=True, slots=True)
class OrderFlowTradePrint:
    """One normalized execution retained for DOM/tape presentation.

    ``sequence`` is local and monotonic even when an exchange trade id is absent.
    The renderer may filter by ``salience_class`` but must never recompute the
    classification or aggressor side.
    """
    sequence: int
    event_time_ms: int
    received_monotonic: float
    price: float
    quantity: float
    notional: float
    aggressor_side: str
    normal_notional: float
    rpi_notional: float
    relative_size: float
    salience_class: int
    reference_midpoint: float = 0.0
    outcome_threshold: float = 0.0
    outcome: str = 'UNRESOLVED'
    outcome_direction: int = 0  # Observed midpoint direction at the outcome horizon.


@dataclass(frozen=True, slots=True)
class OrderFlowLevelMetrics:
    """Read-only temporal state for one currently displayed book level."""
    side: str
    price: float
    quantity: float
    notional: float
    age_seconds: float
    max_notional: float
    persistence_ratio: float
    recent_replenished_notional: float
    recent_restacked_notional: float
    trade_reload_count: int
    restack_count: int
    replenishments: int
    state: str
    state_flags: tuple[str, ...] = ()
    persistent: bool = False
    analysis_revision: int = field(default=0, compare=False)
    new_passive_added_notional: float = 0.0
    effective_cancelled_notional: float = 0.0
    visible_delta_notional: float = 0.0


def order_flow_semantic_event(
    *,
    trade_reload_count: int,
    recent_replenished_notional: float,
    restack_count: int,
    recent_restacked_notional: float,
    rejected_buy_prints: int,
    rejected_sell_prints: int,
) -> tuple[str, str]:
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


@dataclass(frozen=True, slots=True)
class OrderFlowDisplayLevel:
    """Display-ready immutable state for one DOM price level.

    Values are numeric by design: the renderer owns typography/formatting, but
    never sorting, market calculations or rolling-window analysis. Intensities
    are robustly normalized to ``0..1`` across the current snapshot so paint
    code can draw bars without rescanning all rows.
    """
    side: str
    price: float
    quantity: float
    notional: float
    delta_notional_5s: float
    trade_notional_5s: float
    signed_trade_notional_5s: float
    rpi_trade_notional_5s: float


    age_seconds: float = field(compare=False)
    persistence_ratio: float
    replenishments: int
    state: str
    liquidity_intensity: float
    delta_intensity: float
    trade_intensity: float
    cumulative_depth_notional: float = 0.0
    depth_intensity: float = 0.0
    liquidity_history_30s: tuple[float, ...] = ()
    history_presence_30s: float = 0.0
    history_peak_notional_30s: float = 0.0
    history_mean_notional_30s: float = 0.0
    buy_trade_notional_5s: float = 0.0
    sell_trade_notional_5s: float = 0.0
    buy_trade_count_5s: int = 0
    sell_trade_count_5s: int = 0
    largest_buy_trade_5s: float = 0.0
    largest_sell_trade_5s: float = 0.0
    rejected_buy_prints_5s: int = 0
    rejected_sell_prints_5s: int = 0
    recent_replenished_notional: float = 0.0
    recent_restacked_notional: float = 0.0
    trade_reload_count: int = 0
    restack_count: int = 0
    semantic_event_kind: str = ''
    semantic_event_label: str = ''
    state_flags: tuple[str, ...] = ()
    persistent: bool = False
    analysis_revision: int = field(default=0, compare=False)
    new_passive_added_notional: float = 0.0
    effective_cancelled_notional: float = 0.0


@dataclass(frozen=True, slots=True)
class OrderFlowComponentRevisions:
    """Producer-owned content versions, valid within one analyzer session.

    Level/print versions cover every record field and ordering. Amounts cover
    ordered price/quantity/notional inputs independently of age and analytics.
    Display aggregation changes ``view`` without changing the source versions.
    """
    stream: str
    bid_levels: int = 0
    ask_levels: int = 0
    recent_prints: int = 0
    amounts: int = 0
    view: tuple[tuple[int, float], ...] = ()


@dataclass(frozen=True, slots=True)
class OrderFlowSnapshot:
    """Stable backend -> DOM contract for one current-symbol frame.

    The contract deliberately contains already-sorted bid/ask rows and all
    temporal analytics needed by the future renderer.  It contains no QWidget,
    font, color or transport objects and can therefore be tested independently.
    """
    symbol: str
    sequence: int
    generated_monotonic: float
    ready: bool
    live: bool
    bbo_source: str
    depth_age_seconds: float | None
    bbo_age_seconds: float | None
    trade_age_seconds: float | None
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_quantity: float = 0.0
    best_ask_quantity: float = 0.0
    midpoint: float = 0.0
    spread: float = 0.0
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
    has_recent_activity: bool = False
    liquidity_scale: float = 0.0
    delta_scale: float = 0.0
    trade_scale: float = 0.0
    cumulative_depth_scale: float = 0.0
    large_trade_threshold: float = 0.0
    recent_prints: tuple[OrderFlowTradePrint, ...] = ()
    bid_levels: tuple[OrderFlowDisplayLevel, ...] = ()
    ask_levels: tuple[OrderFlowDisplayLevel, ...] = ()
    # None keeps legacy/direct snapshot producers compatible. Such consumers
    # compare contents instead of assuming that a missing version means zero.
    component_revisions: OrderFlowComponentRevisions | None = None


@dataclass(frozen=True, slots=True)
class OrderFlowPresentationFrame:
    """Immutable analyzer-to-view handoff with end-to-end timing metadata."""
    snapshot: OrderFlowSnapshot
    build_started_mono: float = 0.0
    build_completed_mono: float = 0.0


class MarketDataHubPort(Protocol):
    def execution_book_is_fresh(self, now: float | None = None) -> bool: ...

    rest: Any
    universe_ready: Any
    bootstrap_ready: Any
    analysis_ready: Any
    interest_history_ready: Any
    ticker_batch: Any
    kline: Any
    depth: Any
    book_ticker: Any
    mark_price: Any
    liquidation: Any
    trade_batch: Any
    interest: Any
    status: Any
    book_validity: Any
    problem: Any

    def start(self, symbol: str, interval: str) -> None:
        ...

    def set_market_data_api_key(self, api_key: str) -> None:
        ...

    def set_orderbook_streaming_enabled(self, enabled: bool) -> None:
        ...

    def set_orderbook_depth_capacity(self, limit_per_side: int) -> None:
        ...

    def set_valid_ticker_symbols(self, symbols: object) -> None:
        ...

    def set_interaction_priority(self, active: bool) -> None:
        ...

    def switch_market(self, symbol: str, interval: str) -> None:
        ...

    def stop(self) -> None:
        ...


class TradingGatewayPort(Protocol):
    state_changed: Any
    request_succeeded: Any
    request_failed: Any
    armed_changed: Any
    account_event: Any
    snapshot_ready: Any
    account_status_changed: Any
    problem: Any
    credentials_changed: Any
    leverage_changing: Any
    leverage_changed: Any
    protections_recovered: Any
    _protection_recovery_pending: bool
    api_key: str
    api_secret: str
    armed: bool
    stopping: bool
    account_loaded: bool
    testnet: bool
    cross_pending: Any
    cross_ready: Any
    queued_leverage: dict[str, int]
    hedge_mode: bool
    position_cache: Any
    rest: Any

    def has_credentials(self) -> bool:
        ...

    def set_credentials(self, api_key: str, api_secret: str) -> None:
        ...

    def available_balance(self, asset: str) -> float:
        ...

    def account_balance(self, asset: str) -> tuple[float | None, str]:
        ...

    def arm(self) -> bool:
        ...

    def disarm(self) -> None:
        ...

    def client_order_id(self, prefix: str='nw') -> str:
        ...

    def request_was_admitted(self, request_id: str) -> bool:
        ...

    def failure_context(self, request_id: str) -> dict[str, Any]:
        ...

    def submit_order(self, order: dict[str, Any], context: dict[str, Any] | None=None) -> str:
        ...

    def submit_protection_tranche(self, plan: dict, legs: list[tuple[dict, dict]]) -> None:
        ...

    def submit_batch_orders(self, orders: list[dict[str, Any]], rules: Any=None, position_intent: str='OPEN', requires_arm: bool=False) -> str:
        ...

    def placement_status(self) -> tuple[int, int, int]:
        ...

    def submit_modify(self, changes: dict[str, Any], rules: Any=None) -> str:
        ...

    def submit_cancel(self, request: dict[str, Any], algo: bool=False) -> str:
        ...

    def cancel_all(self, symbol: str) -> str:
        ...

    def refresh_account(self, symbol: str | None=None, all_open_orders: bool=False, *, poll_minimum_interval: float=0.0, follow_up: bool=False) -> None:
        ...

    def ensure_cross(self, symbol: str) -> None:
        ...

    def apply_cross_leverage(self, symbol: str, leverage: int) -> None:
        ...

    def current_leverage(self, symbol: str) -> int:
        ...

    def open_position_symbols(self) -> tuple[str, ...]:
        ...

    def reconcile_unknown_orders(self) -> None:
        ...


    def stop(self) -> None:
        ...


class ChartMarketDataPort(MarketDataHubPort, Protocol):
    """Minimal live feed needed by one auxiliary chart pane."""

    def switch_market(self, symbol: str, interval: str) -> None:
        ...

    def stop(self) -> None:
        ...


ChartMarketDataFactory = Callable[[Any], ChartMarketDataPort]


class UiTunerHostPort(Protocol):
    settings: Any
    ui_theme: dict[str, str]
    developer_ui_layout: dict[str, Any]
    developer_ui_surfaces: dict[str, Any]
    developer_ui_status: dict[str, Any]
    chart: Any

    def statusBar(self) -> Any:
        ...

    def set_developer_typography_values(self, role: str, values: dict[str, Any]) -> None:
        ...

    def set_developer_typography_value(self, role: str, key: str, value: Any) -> None:
        ...

    def set_developer_typography_global(self, key: str, value: Any) -> None:
        ...

    def reset_developer_typography_role(self, role: str) -> None:
        ...

    def reset_developer_typography(self) -> None:
        ...

    def apply_developer_ui_color_preset(self, name: str) -> None:
        ...

    def apply_developer_ui_status_preset(self, name: str) -> None:
        ...

    def current_developer_ui_color_preset(self) -> str:
        ...

    def current_developer_ui_layout_preset(self) -> str:
        ...

    def current_developer_ui_status_preset(self) -> str:
        ...

    def set_developer_ui_color(self, key: str, value: str) -> None:
        ...

    def set_developer_ui_status_value(self, key: str, value: Any) -> None:
        ...

    def reset_developer_ui_colors(self) -> None:
        ...

    def reset_developer_ui_status(self) -> None:
        ...

    def import_developer_ui_profile(self) -> None:
        ...

    def export_developer_ui_profile(self) -> None:
        ...


class MagneticRailLabHostPort(Protocol):
    magnetic_rail_config: dict[str, Any]

    def set_magnetic_rail_config(self, config: dict[str, Any]) -> None:
        ...


class DiagnosticsHostPort(Protocol):
    settings: Any
    chart: Any
    orderbook: Any
    testing_flags: dict[str, bool]

    def order_flow_diagnostic_state(self) -> dict[str, Any]:
        ...

    def _set_testing_flag(self, name: str, enabled: bool) -> None:
        ...


class SettingsHostPort(Protocol):
    ui_theme: dict[str, Any]
    orderbook_theme_name: str
    learning_mode: bool
    market_bar_timeframes: tuple[str, ...]
    theme_actions: Any
    candle_style_actions: Any
    chart_layout_actions: Any
    chart_visibility_actions: Any
    auto_scale_action: Any
    logarithmic_action: Any
    fit_chart_action: Any
    ruler_action: Any
    fibonacci_action: Any
    horizontal_action: Any
    clear_drawings_action: Any
    indicator_actions: Any
    indicator_settings_action: Any
    indicator_shortcuts_action: Any
    auto_fibonacci_action: Any
    panel_actions: Any
    trading_menu: Any
    alert_menu: Any
    data_menu: Any
    help_menu: Any
    right_layout_presets: Any
    right_rail_controller: Any
    ui_tuner_dialog: Any
    developer_dialog: Any
    magnetic_rail_lab_dialog: Any

    def set_directional_color_mode(self, surface: str, mode: str) -> None:
        ...

    def directional_color_mode(self, surface: str) -> str:
        ...

    def set_orderbook_theme(self, name: str) -> None:
        ...

    def set_market_bar_timeframes(self, intervals: object) -> None:
        ...

    def set_volume_bar_height_percent(self, value: int) -> None:
        ...

    def volume_bar_height_setting(self) -> int:
        ...

    def _set_right_panel_columns(self, columns: int) -> None:
        ...

    def _apply_right_layout_preset(self, name: str) -> None:
        ...

    def _current_right_panel_definition(self) -> dict[str, Any]:
        ...

    def _save_right_panel_presets(self, configured: dict[str, dict[str, Any]], active_name: str | None = None) -> None:
        ...

    def _reset_right_panel_layout(self) -> None:
        ...


class _CandleMatrixPage(Sequence):
    """Immutable numeric page; materialize stable Candle identities on demand.

    Large process results must not unpickle hundreds of thousands of Python
    objects while holding the GUI's GIL. Native arrays carry the history; only
    rows actually inspected by a consumer acquire a Python object.
    """
    def __init__(self, data, column=None):
        self.data = data
        self.column = column
        self.data.flags.writeable = False
        self._objects = {}

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if self.column is not None:
            return float(self.data[index, self.column])
        value = self._objects.get(index)
        if value is None:
            # setdefault preserves identity if two snapshot readers materialize
            # the same immutable row concurrently.
            value = self._objects.setdefault(index, Candle(*map(float, self.data[index])))
        return value

    def __reduce__(self):
        return type(self), (self.data, self.column)


class CandlePages(Sequence):
    """Copy-on-write pages; snapshots and live replacement copy at most 512 refs.

    History assembly happens in workers. Published pages never change, making
    snapshots safe while a live candle is replaced or the oldest page is evicted.
    """
    PAGE = 512

    def __init__(self, values=()):
        if isinstance(values, CandlePages):
            self._pages, self._start, self._size = values._pages, values._start, values._size
            return
        values = list(values)
        self._pages = tuple(tuple(values[i:i+self.PAGE]) for i in range(0, len(values), self.PAGE))
        self._start = 0
        self._size = len(values)

    @classmethod
    def from_matrix(cls, matrix, column=None):
        """Own an immutable packed history, with optional scalar time pages."""
        data = np.array(matrix, dtype=np.float64, order='C', copy=True)
        if data.ndim != 2 or (column is None and data.shape[1] != 7):
            raise ValueError('Candle history requires an N x 7 matrix')
        if column is not None and not 0 <= column < data.shape[1]:
            raise ValueError('History column is out of range')
        data.flags.writeable = False
        result = object.__new__(cls)
        result._pages = tuple(_CandleMatrixPage(data[i:i+cls.PAGE], column)
                              for i in range(0, len(data), cls.PAGE))
        result._start, result._size = 0, len(data)
        return result

    def to_matrix(self):
        """Copy native history directly; convert only edited object pages."""
        result = np.empty((self._size, 7), dtype=np.float64)
        offset, written = self._start, 0
        for page in self._pages:
            count = min(self._size - written, len(page) - offset)
            if isinstance(page, _CandleMatrixPage) and page.column is None:
                result[written:written+count] = page.data[offset:offset+count]
            else:
                result[written:written+count] = _candle_matrix_from_objects(page[offset:offset+count])
            written += count
            if written == self._size:
                break
            offset = 0
        return result

    def __reduce__(self):
        if self._size >= 4096 and isinstance(self[0], Candle):
            return type(self).from_matrix, (self.to_matrix(),)
        return type(self), (), self.__dict__

    def __len__(self):
        return self._size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._size))]
        if index < 0:
            index += self._size
        if not 0 <= index < self._size:
            raise IndexError(index)
        page, offset = divmod(self._start + index, self.PAGE)
        return self._pages[page][offset]

    def __iter__(self):
        remaining = self._size
        offset = self._start
        for page in self._pages:
            count = min(remaining, len(page) - offset)
            yield from page[offset:offset + count]
            remaining -= count
            if not remaining:
                break
            offset = 0

    def snapshot(self, first=0, last=None):
        result = object.__new__(type(self))
        start, stop, _ = slice(first, last).indices(self._size)
        absolute = self._start + start
        page, result._start = divmod(absolute, self.PAGE)
        result._size = max(0, stop-start)
        count = (result._start + result._size + self.PAGE-1)//self.PAGE
        result._pages = self._pages[page:page+count]
        return result

    def prepended(self, values):
        """Share resident pages, rebuilding only the incoming prefix and edge.

        The leading offset keeps every interior page aligned for constant-time
        indexing. Neither this sequence nor any previously published snapshot is
        mutated. Work is proportional to the new rows plus the page directory,
        not the number of retained candles.
        """
        prefix = tuple(values)
        if not prefix:
            return self.snapshot()
        if not self._size:
            return type(self)(prefix)
        result = object.__new__(type(self))
        result._start = (self._start - len(prefix)) % self.PAGE
        edge = (None,) * result._start + prefix + self._pages[0][self._start:]
        result._pages = tuple(edge[i:i+self.PAGE] for i in range(0, len(edge), self.PAGE)) + self._pages[1:]
        result._size = len(prefix) + self._size
        return result

    def extended(self, values):
        """Append a batch by copying the partial tail page once."""
        suffix = tuple(values)
        if not self._size:
            return type(self)(suffix)
        result = self.snapshot()
        if not suffix:
            return result
        page, offset = divmod(self._start + self._size, self.PAGE)
        edge = (self._pages[page][:offset] if offset else ()) + suffix
        result._pages = self._pages[:page] + tuple(edge[i:i+self.PAGE] for i in range(0, len(edge), self.PAGE))
        result._size += len(suffix)
        return result

    def updated(self, replacements):
        """Replace indexed rows, copying each affected page at most once."""
        result = self.snapshot()
        changed = {}
        for index, value in replacements.items():
            if index < 0:
                index += self._size
            if not 0 <= index < self._size:
                raise IndexError(index)
            page, offset = divmod(self._start + index, self.PAGE)
            if page not in changed:
                changed[page] = list(self._pages[page])
            changed[page][offset] = value
        if changed:
            pages = list(self._pages)
            for page, values in changed.items():
                pages[page] = tuple(values)
            result._pages = tuple(pages)
        return result

    def __setitem__(self, index, value):
        if index < 0:
            index += self._size
        if not 0 <= index < self._size:
            raise IndexError(index)
        page, offset = divmod(self._start + index, self.PAGE)
        values = list(self._pages[page])
        values[offset] = value
        self._pages = self._pages[:page] + (tuple(values),) + self._pages[page+1:]

    def append(self, value):
        page, offset = divmod(self._start+self._size, self.PAGE)
        if offset:
            self._pages = self._pages[:page] + (self._pages[page][:offset]+(value,),)
        else:
            self._pages = self._pages[:page] + ((value,),)
        self._size += 1

    def __delitem__(self, index):
        if not isinstance(index, slice) or index.start not in (0, None) or index.step not in (1, None):
            raise TypeError("Only prefix eviction is supported")
        _, stop, _ = index.indices(self._size)
        absolute = self._start + stop
        pages, self._start = divmod(absolute, self.PAGE)
        self._pages = self._pages[pages:]
        self._size -= stop
        if not self._size:
            self.clear()

    def clear(self):
        self._pages, self._start, self._size = (), 0, 0
