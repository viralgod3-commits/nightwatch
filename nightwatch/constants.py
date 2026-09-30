"""Application, exchange and shell configuration defaults."""
from __future__ import annotations

# ========================================================================
# constants
# ========================================================================
APP_NAME = "Nightwatch Futures V2"


ORG_NAME = "Nightwatch"


# Compatibility mirrors for the untouched leadership surface. utilities.py is
# the sole typography authority and refreshes these after bundled fonts load.
UI_FONT_FAMILY = ""
NUMERIC_FONT_FAMILY = ""


DEFAULT_SYMBOL = "BTCUSDT"


DEFAULT_INTERVAL = "15m"


MAIN_REST = "https://fapi.binance.com"


TEST_REST = "https://testnet.binancefuture.com"


MAIN_WS = "wss://fstream.binance.com"


TEST_WS = "wss://fstream.binancefuture.com"


MAIN_TRADE_WS = "wss://ws-fapi.binance.com/ws-fapi/v1"


TEST_TRADE_WS = "wss://testnet.binancefuture.com/ws-fapi/v1"


MAIN_PRIVATE_WS = "wss://fstream.binance.com/private/ws/"


TEST_PRIVATE_WS = "wss://fstream.binancefuture.com/private/ws/"


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


TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "12h", "1d", "1w", "1M")


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


INDICATOR_KEYS = (
    "Bollinger Bands",
    "ATR",
    "Open Interest",
    "Liquidations",
    "Visible Volume Profile",
    "Session Volume Profile",
    "Major Price Levels",
    "Funding Rate History",
    "RSI",
    "EMA Trend",
    "VWAP",
    "Donchian Channels",
)


DEFAULT_INDICATOR_SHORTCUTS = {
    name: "Ctrl+" + ("1", "2", "3", "Q", "W", "E", "4", "5", "6")[index-1] if index <= 9 else ""
    for index, name in enumerate(INDICATOR_KEYS, 1)
}


INDICATOR_SETTING_DEFAULTS = {
    "EMA Trend": {"fast": 21, "medium": 50, "slow": 200},
    "VWAP": {"anchor": "week"},
    "Donchian Channels": {"period": 20},
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
    "Bollinger Bands": {"period": 20, "deviations": 2.0},
    "ATR": {"period": 14},
    "Open Interest": {"smoothing": 1},
    "Liquidations": {"minimum_notional": 100_000.0, "maximum_markers": 120},
    "Visible Volume Profile": {"bins": 52, "levels": 4, "width_pct": 19.0},
    "Session Volume Profile": {"bins": 56, "levels": 4, "width_pct": 12.0},
    "Major Price Levels": {"minimum_score": 48.0, "maximum_levels": 4},
    "Funding Rate History": {"smoothing": 3},
    "RSI": {
        "period": 14,
        "upper": 70.0,
        "lower": 30.0,
        "show_thresholds": False,
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


# ========================================================================
# shell_config
# ========================================================================
RIGHT_PANEL_NAMES = (
    "Market depth",
    "Trading / positions",
    "Large trades",
    "Watchlist",
    "Orders",
)

RIGHT_PANEL_LABELS = {
    "Market depth": "DEPTH",
    "Trading / positions": "TRADING",
    "Large trades": "LARGE TRADES",
    "Watchlist": "WATCHLIST",
    "Orders": "ORDER PANEL",
}

RIGHT_PANEL_DEFAULT_SIZES = {
    "Market depth": 300,
    "Trading / positions": 440,
    "Large trades": 260,
    "Watchlist": 220,
    "Orders": 240,
}

# Default template and legacy migration order; custom trees have no fixed order.
RIGHT_PANEL_ONE_COLUMN_ORDER = (
    "Market depth",
    "Large trades",
    "Trading / positions",
    "Orders",
    "Watchlist",
)

# Default two-column template and legacy migration order. Drag moves can
# freely change this arrangement; a lone template panel spans the row.
RIGHT_PANEL_TWO_COLUMN_FULL_WIDTH = (
    "Market depth",
)
RIGHT_PANEL_TWO_COLUMN_SECONDARY_ORDER = (
    "Trading / positions",
    "Orders",
    "Watchlist",
    "Large trades",
)

# Right-rail geometry is owned centrally by ui/panels.py. Feature widgets never
# dictate outer panel/grid geometry.
RIGHT_RAIL_CHART_MIN_WIDTH = 520
RIGHT_PANEL_SINGLE_MIN_WIDTH = 360
RIGHT_PANEL_TWO_COLUMN_CELL_MIN_WIDTH = 300
# Trading is deliberately width-stable in a paired two-column row; extra rail
# width belongs to information surfaces such as Watchlist / Large trades / Orders.
RIGHT_PANEL_TRADING_TWO_COLUMN_WIDTH = 330

# Shell-owned usability floors. Enabled panels can never become zero-height
# slivers, but their internal feature widgets remain geometry-neutral.
RIGHT_PANEL_MIN_HEIGHTS = {
    "Market depth": 110,
    "Trading / positions": 150,
    "Large trades": 130,
    "Watchlist": 90,
    "Orders": 90,
}

# Adjacent chart/panel windows use the same minimal black splitter gap.
# The larger transparent grab surface overlaps this gap so resizing stays easy.
RIGHT_PANEL_SPLITTER_VISUAL_WIDTH = 2
RIGHT_PANEL_SPLITTER_HIT_WIDTH = 16


# User-facing directional color modes. These are deliberately semantic choices,
# not RGB editors: ordinary users choose whether a surface follows the active
# theme or uses a familiar high-contrast green/red convention.


# ========================================================================
# fixed_orderbook_presentation
# ========================================================================
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

ORDERBOOK_STATE_COLORS = {
    "ABSORBING": "#35C4B2",
    "PULLING": "#D1A248",
    "STACKING": "#8F7AC8",
    "DEPLETING": "#F26B4B",
    "PERSISTENT": "#B7A6E2",
}

ORDERBOOK_STATE_MIN_WIDTH = 30
ORDERBOOK_STATE_FULL_LABEL_WIDTH = 54
ORDERBOOK_CONTROL_BAR_HEIGHT = 42
ORDERBOOK_CONTROL_BUTTON_HEIGHT = 30

DIRECTIONAL_COLOR_MODE_OPTIONS = (
    ("theme", "Theme colors"),
    ("classic", "Classic green / red"),
)

DIRECTIONAL_COLOR_MODE_DEFAULTS = {
    "candles": "theme",
    "orderbook": "theme",
}


THEME_DISPLAY_NAMES = {
    "Nightwatch": "Nightwatch Core",
    "Obsidian Signal": "Obsidian Signal",
    "Neon Reactor": "Neon Reactor",
    "Arctic Terminal": "Arctic Terminal",
    "Pulse Light": "Pulse Light",
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
DEV_UI_COLOR_PRESETS: dict[str, dict[str, str]] = {}


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
        "instrument_left": 0, "instrument_top": 0, "instrument_right": 0, "instrument_bottom": 0,
        "instrument_spacing": 1, "instrument_height": 42,
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
