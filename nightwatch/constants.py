"""Application, exchange and shell configuration defaults."""
from __future__ import annotations

APP_NAME = "Nightwatch Futures V2"


ORG_NAME = "Nightwatch"


DEFAULT_SYMBOL = "BTCUSDT"


DEFAULT_INTERVAL = "15m"


MAIN_REST = "https://fapi.binance.com"


TEST_REST = "https://demo-fapi.binance.com"


MAIN_WS = "wss://fstream.binance.com"


TEST_WS = "wss://demo-fstream.binance.com"


MAIN_TRADE_WS = "wss://ws-fapi.binance.com/ws-fapi/v1"


TEST_TRADE_WS = "wss://testnet.binancefuture.com/ws-fapi/v1"


MAIN_PRIVATE_WS = "wss://fstream.binance.com/private/ws/"


TEST_PRIVATE_WS = "wss://demo-fstream.binance.com/private/ws/"


STANDARD_ORDER_TYPES = (
    "LIMIT",
    "MARKET",
    "STOP_MARKET",
    "TAKE_PROFIT_MARKET",
    "STOP",
    "TAKE_PROFIT",
    "TRAILING_STOP_MARKET",
)


CONDITIONAL_ORDER_TYPES = {
    "STOP_MARKET",
    "TAKE_PROFIT_MARKET",
    "STOP",
    "TAKE_PROFIT",
    "TRAILING_STOP_MARKET",
}


# Bare B/S are intentionally lower-priority than symbol search unless the
# quick-order system is explicitly armed. Keep this policy shared by dispatch.
LOW_PRIORITY_TRADING_HOTKEYS = frozenset({"B", "S"})

# Bare digits belong to indicators and armed B/S sequences, not configurable
# trading actions or type-to-search.
BARE_NUMBER_SHORTCUTS = frozenset("0123456789")

# These keys are handled before configurable indicator/trading shortcuts.
TIMEFRAME_SHORTCUTS = tuple(f"Ctrl+{index}" for index in range(1, 10))
SHELL_RESERVED_SHORTCUTS = frozenset({
    *TIMEFRAME_SHORTCUTS,
    "Ctrl+A", "Ctrl+P", "Ctrl+PageUp", "Ctrl+PageDown", "Ctrl+O", "Ctrl+Shift+O",
    "Ctrl+Shift+A", "Ctrl+Shift+X", "Ctrl+Tab", "Ctrl+Shift+Tab",
    "Alt+F", "Alt+Shift+F", "Ctrl+Z", "Ctrl+Enter", "Ctrl+Return", "Ctrl+F", "F3", "F5", "F11", "Esc",
    "Delete", "Backspace", "Shift+Delete", "Alt+1", "Alt+2", "Alt+3", "Alt+4",
})


DEFAULT_TRADING_HOTKEYS = {
    "place_buy": "Ctrl+Shift+B",
    "place_sell": "Ctrl+Shift+S",
    "close_1": "Shift+1",
    "close_2": "Shift+2",
    "close_3": "Shift+3",
    "cancel_all": "Ctrl+Shift+C",
    "open_trading": "Ctrl+Shift+T",
    "refresh_account": "Ctrl+Shift+R",
    "kill_session": "Ctrl+Shift+K",
}


DEFAULT_QUICK_TRADING_PRESET = {
    "collateral_percent": 10.0,
    "leverage": 5,
    "order_mode": "MARKET",
    "time_in_force": "IOC",
    "slippage_enabled": False,
    "max_slippage_percent": 0.30,
    "reduce_only": False,
    "take_profit_enabled": False,
    "take_profit_percent": 2.0,
    "take_profit_close_percent": 100,
    "stop_loss_enabled": False,
    "stop_loss_percent": 1.0,
    "stop_loss_close_percent": 100,
    "close_1_percent": 25,
    "close_2_percent": 50,
    "close_3_percent": 100,
}


LEVERAGE_PRESETS = (1, 3, 5, 10, 20, 50)


INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
    "1M": 2592000,  # nominal drawing width; Binance supplies calendar-month timestamps
}


# Supported chart intervals are independent of the trader's quick-access strip.
TIMEFRAMES = tuple(INTERVAL_SECONDS)
DEFAULT_MARKET_BAR_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M")
MARKET_BAR_TIMEFRAME_LIMIT = len(TIMEFRAME_SHORTCUTS)
MARKET_BAR_TIMEFRAME_PRESETS = {
    "Default": DEFAULT_MARKET_BAR_TIMEFRAMES,
    "Intraday": ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"),
    "Swing": ("15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"),
    "Long term": ("1h", "4h", "6h", "12h", "1d", "1w", "1M"),
}


def normalized_market_bar_timeframes(values: object) -> tuple[str, ...]:
    """Restore a nonempty, ordered selection from possibly stale settings."""
    if not isinstance(values, (list, tuple)):
        return DEFAULT_MARKET_BAR_TIMEFRAMES
    selected = {value for value in values if isinstance(value, str) and value in TIMEFRAMES}
    return tuple(value for value in TIMEFRAMES if value in selected)[:MARKET_BAR_TIMEFRAME_LIMIT] or DEFAULT_MARKET_BAR_TIMEFRAMES


MARKET_SORT_MODES = {
    "hour_gainers",
    "hour_losers",
    "gainers",
    "losers",
    "volume",
    "volume_asc",
    "symbol",
    "symbol_desc",
    "price",
    "price_asc",
}


# The catalog is deliberately split by where a study appears. Keep menu,
# selection controls, and shortcut order derived from the same definition.
INDICATOR_GROUPS = {
    "Chart overlays": (
        "EMA Trend", "VWAP", "Supertrend", "Bollinger Bands",
        "Visible Volume Profile", "Liquidations",
    ),
    "Study panes": (
        "RSI", "ADX / DI", "Relative Volume", "Chaikin Money Flow",
        "Open Interest", "Funding Rate History",
    ),
}
INDICATOR_KEYS = tuple(name for names in INDICATOR_GROUPS.values() for name in names)


# One-key defaults prioritize price context, participation and derivatives.
INDICATOR_SHORTCUT_PRIORITY = (
    "VWAP", "EMA Trend", "Supertrend", "Visible Volume Profile", "RSI",
    "Relative Volume", "Open Interest", "Funding Rate History", "ADX / DI",
    "Bollinger Bands",
)
DEFAULT_INDICATOR_SHORTCUTS = {name: "" for name in INDICATOR_KEYS}
DEFAULT_INDICATOR_SHORTCUTS.update({
    name: str(index % 10) for index, name in enumerate(INDICATOR_SHORTCUT_PRIORITY, 1)
})
INDICATOR_SHORTCUT_CHOICES = tuple("1234567890") + (
    "Ctrl+Q", "Ctrl+W", "Ctrl+E", "Ctrl+S", "Ctrl+D", "Ctrl+0",
)


def normalized_indicator_shortcuts(
    values: object, *, legacy: bool = False, migrate_digit_zero: bool = False,
    repair_trend_shortcuts: bool = False,
) -> dict[str, str]:
    """Migrate old defaults while retaining compatible custom bindings/opt-outs."""
    values = values if isinstance(values, dict) else {}
    previous_defaults = dict(zip(
        INDICATOR_KEYS,
        ("Ctrl+" + key for key in ("1", "2", "3", "Q", "W", "E", "4", "5", "6", "7", "8", "9")),
    ))
    result, used = {}, set()
    for name, default in DEFAULT_INDICATOR_SHORTCUTS.items():
        shortcut = str(values.get(name, default))
        if legacy and name in values and (
            shortcut == previous_defaults[name] or shortcut in TIMEFRAME_SHORTCUTS
        ):
            shortcut = default
        if shortcut not in INDICATOR_SHORTCUT_CHOICES or shortcut in used:
            shortcut = ""
        result[name] = shortcut
        if shortcut:
            used.add(shortcut)
    # Version 3 had no bare-zero choice and left Bollinger Bands unassigned.
    # Fill that new default once; retain custom bindings and later opt-outs.
    if migrate_digit_zero and not result["Bollinger Bands"] and "0" not in used:
        result["Bollinger Bands"] = "0"
        used.add("0")
    # Older settings can retain empty bindings for the three trend overlays.
    # Repair those once on upgrade without displacing any custom assignment.
    if repair_trend_shortcuts:
        for name in ("VWAP", "EMA Trend", "Supertrend"):
            default = DEFAULT_INDICATOR_SHORTCUTS[name]
            if not result[name] and default not in used:
                result[name] = default
                used.add(default)
    return result


INDICATOR_DESCRIPTIONS = {
    "EMA Trend": "21 / 50 / 200 EMAs for trend and pullbacks. Periods follow the selected chart timeframe.",
    "VWAP": "Volume-weighted price from the selected UTC day, week or month. Uses quote/base traded volume; typical price is an approximation when quote volume is missing. An incomplete first anchor stays blank. Weekly candles require Week; monthly candles require Month.",
    "Supertrend": "ATR trailing trend line: midpoint ± multiplier × Wilder ATR. The active line switches when price crosses the trailing band. The live bar can change direction before it closes.",
    "Bollinger Bands": "Rolling close mean ± standard deviations. Band width describes volatility; touching a band alone is not a reversal signal.",
    "Visible Volume Profile": "Volume distribution estimated from OHLCV candle ranges, not exact trades at each price. Changes with the visible chart range.",
    "Liquidations": "Observed forced-order snapshots only. Red marks liquidated longs; green marks liquidated shorts. Binance may omit events, so this is not total liquidation volume.",
    "RSI": "Wilder momentum oscillator. Flat prices produce 50. RSI can stay above 70 or below 30 during a strong trend.",
    "ADX / DI": "Wilder ADX measures trend strength, while +DI and −DI indicate directional pressure. The threshold is a reference, not a trade signal. Values update during the live bar.",
    "Relative Volume": "Current base volume divided by mean volume of the prior completed candles. 1× is typical volume. An incomplete live bar can read low; this is not time-of-day adjusted.",
    "Chaikin Money Flow": "Sum of close-location-weighted volume / sum of volume. An OHLCV estimate of pressure, not taker buy/sell delta. Zero-range candles contribute no directional pressure. Values update during the live bar.",
    "Open Interest": "USD notional OI changes reflect both positioning and price. Rising OI alone does not identify whether longs or shorts are entering.",
    "Funding Rate History": "Rates per settlement, not annualized. Positive means longs pay shorts. Optional EMA smoothing uses actual settlements without clipping spikes.",
}


INDICATOR_SETTING_DEFAULTS = {
    "EMA Trend": {"fast": 21, "medium": 50, "slow": 200},
    "VWAP": {"anchor": "day"},
    "Supertrend": {"period": 10, "multiplier": 3.0},
    "Bollinger Bands": {"period": 20, "deviations": 2.0},
    "Visible Volume Profile": {"bins": 52, "levels": 4, "width_pct": 19.0},
    "Liquidations": {"minimum_notional": 100_000.0, "maximum_markers": 120},
    "RSI": {"period": 14, "upper": 70.0, "lower": 30.0, "show_thresholds": False},
    "ADX / DI": {"period": 14, "threshold": 25.0},
    "Relative Volume": {"period": 20},
    "Chaikin Money Flow": {"period": 20},
    "Open Interest": {"smoothing": 1},
    "Funding Rate History": {"smoothing": 3},
    "Auto Fibonacci": {
        "maximum_candidates": 5,
        "label_position": "right",
        "line_extent": "right_edge",
        "show_ratios": True,
        "show_prices": True,
        "show_anchor": True,
        "show_badge": True,
        "levels": [-0.618, -0.5, -0.382, -0.236, 0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0],
    },
}


CHART_CACHE_LIMIT = 24


ANALYSIS_CACHE_LIMIT = 12


CHART_CACHE_REFRESH_SECONDS = 12.0


ANALYSIS_CACHE_REFRESH_SECONDS = 90.0


MARKET_OVERVIEW_REFRESH_MS = 120_000


MARKET_VOLUME_CACHE_SECONDS = 600.0


DEPTH_FALLBACK_INTERVAL_MS = 2_500


MAX_CHART_CANDLES = 250_000


MAX_RENDER_CANDLES = 2_800


HISTORY_PAGE_LIMIT = 1000


BINANCE_WS_CONNECTION_LIMIT_5M = 300


# shell_config
RIGHT_PANEL_NAMES = (
    "Market depth",
    "Trading / positions",
    "Large trades",
    "Watchlist",
)

RIGHT_PANEL_LABELS = {
    "Market depth": "DEPTH",
    "Trading / positions": "TRADING",
    "Large trades": "LARGE TRADES",
    "Watchlist": "WATCHLIST",
}

RIGHT_PANEL_DEFAULT_SIZES = {
    "Market depth": 300,
    "Trading / positions": 440,
    "Large trades": 260,
    "Watchlist": 220,
}

# Default template and legacy migration order; custom trees have no fixed order.
RIGHT_PANEL_ONE_COLUMN_ORDER = (
    "Market depth",
    "Large trades",
    "Trading / positions",
    "Watchlist",
)

# Default two-column template and legacy migration order. Drag moves can
# freely change this arrangement; a lone template panel spans the row.
RIGHT_PANEL_TWO_COLUMN_FULL_WIDTH = (
    "Market depth",
)
RIGHT_PANEL_TWO_COLUMN_SECONDARY_ORDER = (
    "Trading / positions",
    "Watchlist",
    "Large trades",
)

# Right-rail geometry is owned centrally by ui/panels.py. Feature widgets never
# dictate outer panel/grid geometry.
RIGHT_RAIL_CHART_MIN_WIDTH = 520
RIGHT_PANEL_SINGLE_MIN_WIDTH = 360
RIGHT_PANEL_TWO_COLUMN_CELL_MIN_WIDTH = 300

# Shell-owned usability floors. Enabled panels can never become zero-height
# slivers, but their internal feature widgets remain geometry-neutral.
RIGHT_PANEL_MIN_HEIGHTS = {
    "Market depth": 110,
    "Trading / positions": 240,
    "Large trades": 130,
    "Watchlist": 90,
}

# Adjacent chart/panel windows use the same minimal black splitter gap.
# The larger transparent grab surface overlaps this gap so resizing stays easy.
RIGHT_PANEL_SPLITTER_VISUAL_WIDTH = 4
RIGHT_PANEL_SPLITTER_HIT_WIDTH = 16


# User-facing directional color modes. These are deliberately semantic choices,
# not RGB editors: ordinary users choose whether a surface follows the active
# theme or uses a familiar high-contrast green/red convention.


# fixed_orderbook_presentation
# The DOM is a trading instrument, not a themed application surface.  Keep its
# contrast, semantic colors and compact signal vocabulary stable across every
# application theme so screenshots, muscle memory and signal meaning do not
# change when the shell palette changes.
ORDERBOOK_FIXED_PALETTE = {
    "bg": "#000000",
    "surface_top": "#050607",
    "surface_raised": "#090A0C",
    "surface_center": "#0D0F11",
    "grid": "#1B1E22",
    "grid_strong": "#2B3036",
    "text": "#D5D8DC",
    "muted": "#7C838C",
    "bid": "#00C56A",
    "ask": "#F0143E",
    "bid_fill": "#062B19",
    "ask_fill": "#3A0A15",
    "bid_fill_strong": "#075B34",
    "ask_fill_strong": "#7B1028",
    "amber": "#C89A47",
    "mid": "#C8CDD2",
    "purple": "#9A87C8",
    "control": "#0D0F12",
    "control_hover": "#171A1F",
    "control_pressed": "#090B0D",
    "control_border": "#292D33",
    "control_hover_line": "#59616B",
}

ORDERBOOK_STATE_LABELS = {
    "ABSORBING": "ABSORB",
    "PULLING": "PULL",
    "STACKING": "STACK",
    "DEPLETING": "DEPLETE",
    "PERSISTENT": "WALL",
    "NORMAL": "",
}

# Narrow STATE cells use fixed two-letter badges, matching the reference DOM.
ORDERBOOK_STATE_ACRONYMS = {
    "ABSORBING": "AB",
    "PULLING": "PL",
    "STACKING": "ST",
    "DEPLETING": "DP",
    "PERSISTENT": "WL",
    "NORMAL": "",
}


ORDERBOOK_STATE_MIN_WIDTH = 30
ORDERBOOK_STATE_FULL_LABEL_WIDTH = 54

DIRECTIONAL_COLOR_MODE_OPTIONS = (
    ("theme", "Theme colors"),
    ("classic", "Classic green / red"),
)

DIRECTIONAL_COLOR_MODE_DEFAULTS = {
    "candles": "theme",
}


THEME_DISPLAY_NAMES = {
    "Nightwatch": "Nightwatch",
    "Neon Reactor": "Neon Reactor",
    "Arctic Terminal": "Arctic Terminal",
    "Pulse Light": "Pulse Light",
    "Graphite Pro": "Graphite Pro",
    "Deep Ocean": "Deep Ocean",
    "Copper Dusk": "Copper Dusk",
    "Paper Desk": "Paper Desk",
    "OLED Black": "OLED Black",
    "Amethyst": "Amethyst",
    "Evergreen": "Evergreen",
    "Cobalt Amber": "Cobalt Amber",
}

INITIAL_CHART_WARM_CANDLES = 5_800


TESTING_ENTRIES = (
    (
        "magnetic_order_rail",
        "Magnetic order rail",
        "Enable the chart-side magnetic execution rail with hover controls and explicit BUY/SELL submission through the normal trading gateway",
    ),
    (
        "chart_opengl",
        "OpenGL chart canvas (restart)",
        "Use a QOpenGLWidget viewport for the chart canvas; disable only for driver compatibility",
    ),
    (
        "chart_opengl_full_viewport",
        "OpenGL full-viewport repaint (restart)",
        "Required while the OpenGL canvas is enabled so QGraphicsView never relies on partial dirty regions",
    ),
    (
        "chart_isolated_composition",
        "Isolated GPU chart composition (restart)",
        "Experimental native chart surface; compare on the target GPU before enabling, as software OpenGL can be slower",
    ),
    (
        "chart_native_bar_renderer",
        "Native GPU candle / volume renderer",
        "A/B switch for Nightwatch's instanced OpenGL bars versus the cached QPainter fallback inside the same canvas",
    ),
    (
        "chart_lod_aggregation",
        "Candle LOD aggregation",
        "Aggregate distant candles into power-of-two draw buckets when many bars are visible; disable only for render profiling",
    ),
    (
        "multiple_chart_layouts",
        "Multiple chart layouts",
        "Enable the independent two-chart and four-chart workspace layouts",
    ),
)

DEV_UI_COLOR_FIELDS = (
    ("topbar", "Top bar"),
    ("bg", "App background"),
    ("shell_gap", "Panel / splitter gap"),
    ("panel", "Panel"),
    ("panel2", "Panel alternate"),
    ("header", "Header"),
    ("raised", "Raised surface"),
    ("control", "Control"),
    ("control_top", "Control highlight"),
    ("control_hover", "Control hover"),
    ("border", "Panel border"),
    ("control_border", "Control border"),
    ("separator", "Separator"),
    ("active", "Active surface"),
    ("active_line", "Active line"),
    ("text", "Primary text"),
    ("muted", "Muted text"),
    ("grid", "Chart grid"),
    ("green", "Buy / positive"),
    ("red", "Sell / negative"),
    ("depth_green", "Bid depth base"),
    ("depth_red", "Ask depth base"),
    ("cyan", "Primary accent"),
    ("info", "Information accent"),
    ("amber", "Warning accent"),
    ("purple", "Secondary accent / ratio"),
    ("take_profit", "Take-profit accent"),
    ("risk", "Stop / risk accent"),
    ("chart_bg", "Chart background"),
    ("chart_grid", "Chart grid"),
    ("chart_border", "Chart border / axes"),
    ("chart_panel2", "Chart raised surface"),
    ("chart_control", "Chart control surface"),
    ("chart_control_border", "Chart control border"),
    ("chart_text", "Chart text"),
    ("chart_muted", "Chart muted / axis text"),
    ("chart_green", "Chart positive"),
    ("chart_red", "Chart negative"),
    ("chart_cyan", "Chart primary accent"),
    ("chart_amber", "Chart warning accent"),
    ("chart_purple", "Chart secondary accent"),
    ("current_price_line", "Current-price line"),
    ("current_price_text", "Current-price text"),
    ("reference_price", "Reference price"),
    ("series_1", "Data series 1"),
    ("series_2", "Data series 2"),
    ("series_3", "Data series 3"),
    ("series_4", "Data series 4"),
    ("book_bid", "Order book bid"),
    ("book_ask", "Order book ask"),
    ("book_mid", "Order book midpoint"),
    ("book_grid", "Order book grid"),
    ("orderbook_bg", "Order book background"),
    ("orderbook_muted", "Order book muted"),
    ("orderbook_value_text", "Order book value text"),
    ("orderbook_badge_text", "Order book badge text"),
    ("orderflow_absorption", "Flow · absorption"),
    ("orderflow_stacking", "Flow · stacking"),
    ("orderflow_pulling", "Flow · pulling"),
    ("orderflow_depletion", "Flow · depletion"),
    ("orderflow_wall", "Flow · wall"),
    ("orderflow_rpi", "Flow · RPI"),
    ("orderflow_entry", "Account · entry"),
    ("orderflow_take_profit", "Account · take profit"),
    ("orderflow_stop", "Account · stop"),
    ("orderflow_liquidation", "Account · liquidation"),
    ("orderflow_order", "Account · working order"),
    ("rail_neutral", "Rail · neutral"),
    ("rail_buy", "Rail · buy"),
    ("rail_sell", "Rail · sell"),
    ("rail_take_profit", "Rail · take profit"),
    ("rail_stop", "Rail · stop"),
    ("rail_secondary", "Rail · secondary"),
    ("rail_warning", "Rail · warning"),
    ("metric_price", "Metric · price"),
    ("metric_volume", "Metric · volume"),
    ("metric_taker_buy", "Metric · taker buy"),
    ("metric_taker_sell", "Metric · taker sell"),
    ("metric_taker_neutral", "Metric · taker neutral"),
    ("metric_funding_positive", "Metric · positive funding"),
    ("metric_funding_negative", "Metric · negative funding"),
    ("metric_funding_neutral", "Metric · neutral funding"),
)

# The color tuner edits explicit overrides only. The resolver derives semantic
# children that are not explicitly overridden. Dead historical fill/volume color
# fields are intentionally not part of the active contract.
DEV_UI_COLOR_PROFILE_FIELDS = DEV_UI_COLOR_FIELDS

# Named developer color presets remain retired. A clean reset returns to the
# currently selected base theme, preserving the resolver as the only color path.


# Layout presets intentionally operate on the existing v1 geometry contract so
# imported/exported UI profiles remain compatible.  "Near zero" mirrors the
# reference profile used for the dense terminal redesign; the other presets add
# only deliberate separation between logical groups.
DEV_UI_LAYOUT_PRESETS = {
    "Near zero": {
        "outer_left": 0, "outer_top": 0, "outer_right": 0, "outer_bottom": 0,
        "topbar_left": 1, "topbar_top": 1, "topbar_right": 1, "topbar_bottom": 0,
        "topbar_group_gap": 2, "toolbar_gap": 1, "timeframe_gap": 1,
        "chart_column_left": 0, "chart_column_top": 0, "chart_column_right": 0, "chart_column_bottom": 0,
        "chart_column_spacing": 0,
        "right_splitter_left": 0, "right_splitter_top": 0, "right_splitter_right": 0, "right_splitter_bottom": 0,
        "right_panel_margin": 0,
        "instrument_left": 8, "instrument_top": 0, "instrument_right": 72, "instrument_bottom": 0,
        "instrument_spacing": 12, "instrument_height": 48,
        "fit_log_x": 0, "fit_log_y": 0,
        "chart_vertical_spacing": 0, "axis_width": 66,
    },
    "Dense": {
        "outer_left": 7, "outer_top": 0, "outer_right": 7, "outer_bottom": 0,
        "topbar_left": 2, "topbar_top": 1, "topbar_right": 2, "topbar_bottom": 1,
        "topbar_group_gap": 5, "toolbar_gap": 2, "timeframe_gap": 2,
        "chart_column_left": 0, "chart_column_top": 1, "chart_column_right": 1, "chart_column_bottom": 0,
        "chart_column_spacing": 1,
        "right_splitter_left": 0, "right_splitter_top": 1, "right_splitter_right": 0, "right_splitter_bottom": 0,
        "right_panel_margin": 1,
        "instrument_left": 2, "instrument_top": 1, "instrument_right": 2, "instrument_bottom": 1,
        "instrument_spacing": 2, "instrument_height": 42,
        "fit_log_x": 0, "fit_log_y": 0,
        "chart_vertical_spacing": 0, "axis_width": 66,
    },
    "Grouped": {
        "outer_left": 7, "outer_top": 0, "outer_right": 7, "outer_bottom": 0,
        "topbar_left": 3, "topbar_top": 2, "topbar_right": 3, "topbar_bottom": 2,
        "topbar_group_gap": 9, "toolbar_gap": 2, "timeframe_gap": 2,
        "chart_column_left": 0, "chart_column_top": 2, "chart_column_right": 2, "chart_column_bottom": 0,
        "chart_column_spacing": 2,
        "right_splitter_left": 0, "right_splitter_top": 2, "right_splitter_right": 0, "right_splitter_bottom": 0,
        "right_panel_margin": 1,
        "instrument_left": 3, "instrument_top": 2, "instrument_right": 3, "instrument_bottom": 2,
        "instrument_spacing": 2, "instrument_height": 42,
        "fit_log_x": 0, "fit_log_y": 0,
        "chart_vertical_spacing": 0, "axis_width": 66,
    },
    "Comfortable": {
        "outer_left": 7, "outer_top": 2, "outer_right": 7, "outer_bottom": 1,
        "topbar_left": 5, "topbar_top": 4, "topbar_right": 5, "topbar_bottom": 4,
        "topbar_group_gap": 12, "toolbar_gap": 4, "timeframe_gap": 4,
        "chart_column_left": 0, "chart_column_top": 4, "chart_column_right": 4, "chart_column_bottom": 0,
        "chart_column_spacing": 4,
        "right_splitter_left": 0, "right_splitter_top": 4, "right_splitter_right": 0, "right_splitter_bottom": 0,
        "right_panel_margin": 2,
        "instrument_left": 5, "instrument_top": 3, "instrument_right": 5, "instrument_bottom": 3,
        "instrument_spacing": 4, "instrument_height": 42,
        "fit_log_x": 0, "fit_log_y": 0,
        "chart_vertical_spacing": 0, "axis_width": 66,
    },
}

DEV_UI_LAYOUT_DEFAULTS = dict(DEV_UI_LAYOUT_PRESETS["Near zero"])


# Semantic surface controls. "Blocks" are structural containers; "elements"
# are controls/cards/fields that remain individually outlined.
DEV_UI_SURFACE_DEFAULTS = {
    "block_border_width": 0,
    "block_radius": 6,
    "element_radius": 4,
}

# Bottom status-bar tuner contract.
DEV_UI_STATUS_COLOR_FIELDS = (
    ("background", "Background"),
    ("border", "Border"),
    ("text", "Text"),
    ("message", "Message"),
    ("connection", "Connection"),
    ("connection_live", "Connection · live"),
    ("latency_live", "Latency · live"),
    ("latency_stale", "Latency · stale"),
)

DEV_UI_STATUS_GEOMETRY_DEFAULTS = {
    "height": 22,
    "outer_left": 0,
    "outer_top": 0,
    "outer_right": 0,
    "outer_bottom": 0,
    "padding_left": 5,
    "padding_top": 0,
    "padding_right": 5,
    "padding_bottom": 0,
    "spacing": 5,
}

# UI Tuner exposes these bundles instead of ten independent status geometry
# controls.  The underlying v1 fields remain unchanged for profile compatibility.
DEV_UI_STATUS_PRESETS = {
    "Compact": {
        "height": 20,
        "outer_left": 7, "outer_top": 0, "outer_right": 7, "outer_bottom": 0,
        "padding_left": 4, "padding_top": 0, "padding_right": 4, "padding_bottom": 0,
        "spacing": 4,
    },
    "Standard": dict(DEV_UI_STATUS_GEOMETRY_DEFAULTS),
    "Spacious": {
        "height": 32,
        "outer_left": 6, "outer_top": 1, "outer_right": 6, "outer_bottom": 3,
        "padding_left": 8, "padding_top": 1, "padding_right": 8, "padding_bottom": 1,
        "spacing": 10,
    },
}

