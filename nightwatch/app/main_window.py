"""Main application shell, view composition, menus, and cross-subsystem wiring."""

from __future__ import annotations

import html
import json
import math
import os
import time
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings, QTimer, Qt

from ..entrypoint import AppComposition
from ..coin_catalog import (
    coin_base_symbol,
    coin_icon_exists,
    coin_icon_path,
    coin_remote_symbol,
)
from ..models import DiagnosticsPort
from ..presentation import (
    profile_callback,
    PresentationClock,
    performance_profile_active,
    record_performance_count,
    record_performance_timing,
)
from .. import constants
from ..alerts import (
    AlertCenter,
    AlertSettingsDialog,
    AlertsPanel,
    PriceAlertDialog,
)
from ..ui.developer_tools import (
    DataCacheDialog,
    HistoryDownloadDialog,
    MarketDataOptionsDialog,
    MarketHistoryDownloadDialog,
)
from ..trading.orders import RailAmendments, edit_active_rail_settings
from ..trading.account_state import normalize_order
from ..chart.magnetic_rail import (
    ORDER_RAIL_ORDER_PRESET_DEFAULTS,
    ORDER_RAIL_USER_KEYS,
    normalized_order_rail_config,
    normalized_order_rail_order_preset,
)
from ..chart.workspace import CHART_LAYOUTS, ChartWorkspace, MultiChartContainer
from ..chart.analysis import LatestJob
from ..constants import (
    APP_NAME,
    CONDITIONAL_ORDER_TYPES,
    DEFAULT_INTERVAL,
    DEFAULT_SYMBOL,
    DEFAULT_TRADING_HOTKEYS,
    LOW_PRIORITY_TRADING_HOTKEYS,
    DEV_UI_COLOR_PROFILE_FIELDS,
    DEV_UI_LAYOUT_DEFAULTS,
    DEV_UI_STATUS_COLOR_FIELDS,
    DEV_UI_STATUS_GEOMETRY_DEFAULTS,
    DEV_UI_STATUS_PRESETS,
    DEV_UI_SURFACE_DEFAULTS,
    DIRECTIONAL_COLOR_MODE_DEFAULTS,
    INDICATOR_KEYS,
    INDICATOR_SETTING_DEFAULTS,
    INITIAL_CHART_WARM_CANDLES,
    MARKET_SORT_MODES,
    MARKET_VOLUME_CACHE_SECONDS,
    ORG_NAME,
    RIGHT_PANEL_DEFAULT_SIZES,
    RIGHT_PANEL_NAMES,
    RIGHT_RAIL_CHART_MIN_WIDTH,
    TESTING_ENTRIES,
    THEME_DISPLAY_NAMES,
    TIMEFRAMES,
    normalized_market_bar_timeframes,
)
from ..models import (
    MarketDataHubPort,
    MicrostructureSnapshot,
    ORDER_FLOW_AGGREGATION_MULTIPLIERS,
    OrderFlowPresentationFrame,
    TradingGatewayPort,
)
from ..leadership import LeadershipTimelineWidget, SectorOverviewWidget, RotationScannerWidget
from ..models import (
    Candle,
    SymbolRules,
    format_price,
    human_number,
    perpetual_display_symbol,
    quantize_step,
    safe_float,
    shift_candle_time,
    validate_step,
)
from ..networking.binance import BinanceRest
from ..networking.binance import ApiTask, http_bytes, http_json, launch_task
from ..orderbook.backend import OrderFlowRuntime
from ..orderbook.orderbook_ui import OrderBookWidget, TradesTapeWidget
from ..theme import (
    CANDLE_STYLES,
    DEFAULT_THEME_NAME,
    RIGHT_LAYOUT_PRESETS,
    THEMES,
    build_shell_stylesheet,
    candle_directional_palette,
    chart_palette,
    orderbook_directional_palette,
    ui_palette,
)
from ..trading.orders import (
    build_magnetic_rail_order_request,
    build_quick_order_request,
    build_smart_exit_orders,
    is_shift_letter_shortcut,
    is_smart_exit_shortcut,
    protection_quantities,
)
from ..trading.trading_ui import (
    OrderPanel,
    QuickTradingSettingsDialog,
    TradingWorkspace,
)
from ..ui.developer_tools import (
    DeveloperDialog,
    MagneticRailLabDialog,
    MagneticRailPresetsDialog,
    UiTunerDialog,
)
from ..ui.dialogs import (
    IndicatorSettingsDialog,
    IndicatorShortcutsDialog,
    MicrostructureNewsCard,
    NightwatchSettingsDialog,
    RightPanelPresetsDialog,
)
from ..ui.market_widgets import (
    MarketFilterDialog,
    MarketStatsWidget,
    SymbolSearchDialog,
    WatchlistSidebarWidget,
    WatchlistWidget,
)
from ..ui.panels import (PanelSplitter, PanelSpec, RightRailController, valid_panel_names,
                         decode_tree, encode_tree, panel_ids, detach_panel, insert_panel, PanelNode, validate_tree)
from ..utilities import (
    DEV_UI_STATUS_FONT_DEFAULTS,
    TYPOGRAPHY_DEFAULTS,
    TYPOGRAPHY_GLOBAL_DEFAULTS,
    ChartSurfaceHost,
    InstrumentBar,
    MainToolbar,
    TerminalStatusBar,
    apply_typography,
    configure_typography,
    line_icon,
    set_tooltip_theme,
    tooltips_allowed,
    typography_controller,
)

COIN_ICON_REFRESH_MS = 7 * 24 * 60 * 60 * 1000
COINGECKO_DEMO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
COINGECKO_API = "https://api.coingecko.com/api/v3"
COINPAPRIKA_API = "https://api.coinpaprika.com/v1"
COIN_ICON_MAX_BYTES = 2_000_000


def _coin_icon_payload_valid(payload: bytes) -> bool:
    if len(payload) < 64 or len(payload) > COIN_ICON_MAX_BYTES:
        return False
    head = payload[:512].lstrip().lower()
    return bool(
        payload.startswith(b"\x89PNG\r\n\x1a\n")
        or payload.startswith(b"\xff\xd8\xff")
        or payload.startswith((b"GIF87a", b"GIF89a"))
        or (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
        or head.startswith(b"<svg")
        or b"<svg" in head[:256]
    )


def _write_coin_icon(base: str, payload: bytes) -> tuple[str, bool]:
    if not _coin_icon_payload_valid(payload):
        raise RuntimeError("Remote icon payload is not a supported image.")
    path = coin_icon_path(base)
    if not path:
        raise RuntimeError("Coin icon path is unavailable.")
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    existing = b""
    try:
        with open(path, "rb") as handle:
            existing = handle.read(COIN_ICON_MAX_BYTES + 1)
    except OSError:
        pass
    if existing == payload:
        return path, False
    temporary = f"{path}.{time.time_ns()}.tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
    return path, True


def _coingecko_icon_rows(remote_symbols: list[str]) -> dict[str, dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    headers = {"x-cg-demo-api-key": COINGECKO_DEMO_API_KEY} if COINGECKO_DEMO_API_KEY else {}
    unique = list(dict.fromkeys(symbol.lower() for symbol in remote_symbols if symbol))
    for start in range(0, len(unique), 50):
        chunk = unique[start:start + 50]
        payload = http_json(
            COINGECKO_API + "/coins/markets",
            params={
                "vs_currency": "usd",
                "symbols": ",".join(chunk),
                "include_tokens": "top",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
            },
            headers=headers,
            timeout=15.0,
        )
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if symbol and symbol not in rows_by_symbol:
                rows_by_symbol[symbol] = row
    return rows_by_symbol


def _coinpaprika_icon_rows() -> dict[str, dict[str, Any]]:
    payload = http_json(COINPAPRIKA_API + "/tickers", timeout=20.0)
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        current = rows_by_symbol.get(symbol)
        rank = int(safe_float(row.get("rank"), 9_999_999))
        current_rank = int(safe_float(current.get("rank"), 9_999_999)) if current else 9_999_999
        if current is None or rank < current_rank:
            rows_by_symbol[symbol] = row
    return rows_by_symbol


def _coingecko_candidate(row: dict[str, Any]) -> dict[str, str] | None:
    image_url = str(row.get("image") or "").strip()
    provider_id = str(row.get("id") or "").strip()
    if not image_url or not provider_id:
        return None
    return {
        "provider": "coingecko",
        "provider_id": provider_id,
        "remote_symbol": str(row.get("symbol") or "").upper(),
        "name": str(row.get("name") or ""),
        "image_url": image_url,
    }


def _coinpaprika_candidate(row: dict[str, Any]) -> dict[str, str] | None:
    provider_id = str(row.get("id") or "").strip()
    if not provider_id:
        return None
    return {
        "provider": "coinpaprika",
        "provider_id": provider_id,
        "remote_symbol": str(row.get("symbol") or "").upper(),
        "name": str(row.get("name") or ""),
        "image_url": f"https://static.coinpaprika.com/coin/{provider_id}/logo.png",
    }


def _store_coin_icon_candidate(base: str, candidate: dict[str, str], db: Any) -> bool:
    image_url = str(candidate.get("image_url") or "")
    payload = http_bytes(
        image_url,
        timeout=15.0,
        max_bytes=COIN_ICON_MAX_BYTES,
    )
    path, changed = _write_coin_icon(base, payload)
    db.save_coin_icon(
        base,
        provider=candidate.get("provider", ""),
        provider_id=candidate.get("provider_id", ""),
        remote_symbol=candidate.get("remote_symbol", ""),
        name=candidate.get("name", ""),
        filename=os.path.basename(path),
        image_url=image_url,
    )
    return changed


def _record_market_handler_profile(
    name: str,
    started: float,
    during_interaction: bool,
) -> None:
    if started <= 0.0:
        return
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    record_performance_timing(f"market.{name}_ms", elapsed_ms)
    record_performance_count(f"market.{name}_calls")
    if during_interaction:
        record_performance_timing(f"market.{name}.interaction_ms", elapsed_ms)
        record_performance_count(f"market.{name}.interaction_calls")


@dataclass(frozen=True, slots=True)
class UniversePreparation:
    """Worker-ready exchange universe derived without touching Qt objects."""

    symbols: tuple[str, ...]
    rules: dict[str, SymbolRules]
    exchange_symbols: tuple[dict[str, Any], ...]
    initial_tickers: tuple[dict[str, Any], ...]


def prepare_universe(payload: dict[str, Any]) -> UniversePreparation:
    """Build the validated USDT crypto-perpetual universe off the GUI thread."""
    exchange = payload.get("exchange", {}) if isinstance(payload, dict) else {}
    raw_symbols = exchange.get("symbols", []) if isinstance(exchange, dict) else []
    symbols: list[str] = []
    rules: dict[str, SymbolRules] = {}
    accepted_rows: list[dict[str, Any]] = []

    for raw in raw_symbols if isinstance(raw_symbols, list) else []:
        if not isinstance(raw, dict):
            continue
        contract_type = str(raw.get("contractType") or "").upper()
        underlying_type = str(raw.get("underlyingType") or "").upper()
        if (
            raw.get("status") != "TRADING"
            or contract_type != "PERPETUAL"
            or raw.get("quoteAsset") != "USDT"
            or (underlying_type and underlying_type != "COIN")
        ):
            continue
        symbol = str(raw.get("symbol") or "")
        if not symbol or "_" in symbol:
            continue

        symbols.append(symbol)
        accepted_rows.append(raw)
        filters = {
            entry.get("filterType"): entry
            for entry in raw.get("filters", [])
            if isinstance(entry, dict)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        market_filter = filters.get("MARKET_LOT_SIZE", lot_filter)
        notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
        percent_filter = filters.get("PERCENT_PRICE", {})
        rules[symbol] = SymbolRules(
            tick_size=price_filter.get("tickSize", "0.01"),
            lot_step=lot_filter.get("stepSize", "0.001"),
            market_step=market_filter.get(
                "stepSize", lot_filter.get("stepSize", "0.001")
            ),
            min_price=safe_float(price_filter.get("minPrice")),
            max_price=safe_float(price_filter.get("maxPrice"), float("inf")) or float('inf'),
            min_qty=safe_float(lot_filter.get("minQty")),
            max_qty=safe_float(lot_filter.get("maxQty"), float("inf")),
            min_market_qty=safe_float(
                market_filter.get("minQty"), safe_float(lot_filter.get("minQty"))
            ),
            max_market_qty=safe_float(
                market_filter.get("maxQty"),
                safe_float(lot_filter.get("maxQty"), float("inf")),
            ),
            min_notional=safe_float(
                notional_filter.get("notional") or notional_filter.get("minNotional")
            ),
            price_multiplier_up=safe_float(percent_filter.get("multiplierUp")),
            price_multiplier_down=safe_float(percent_filter.get("multiplierDown")),
            quote_asset=str(raw.get("quoteAsset") or "USDT"),
            margin_asset=str(raw.get("marginAsset") or raw.get("quoteAsset") or "USDT"),
        )

    accepted = set(symbols)
    normalized: list[dict[str, Any]] = []
    raw_tickers = payload.get("tickers", []) if isinstance(payload, dict) else []
    for raw in raw_tickers if isinstance(raw_tickers, list) else []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", raw.get("s")) or "")
        if symbol not in accepted:
            continue
        if "s" in raw:
            normalized.append(dict(raw))
        else:
            normalized.append(
                {
                    **raw,
                    "s": symbol,
                    "c": raw.get("lastPrice"),
                    "P": raw.get("priceChangePercent"),
                    "v": raw.get("volume"),
                    "q": raw.get("quoteVolume"),
                }
            )

    return UniversePreparation(
        symbols=tuple(symbols),
        rules=rules,
        exchange_symbols=tuple(accepted_rows),
        initial_tickers=tuple(normalized),
    )


class _TickerPreparation:
    """Single-consumer ingress; published ticker rows are never mutated again."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = deque()
        self.tickers = {}
        self.context = None

    def post(self, updates, valid, tracked, context=0):
        # Hold references to immutable ingress batches, not a per-symbol GUI merge.
        # The feed already batches/coalesces messages; only one consumer is queued.
        with self.lock:
            self.pending.append((updates, valid, tracked, context))

    def prepare(self, valid, context=0):
        with self.lock:
            # A running request must not consume a newer universe's ingress:
            # its publication will be rejected after the universe changes.
            pending = deque()
            while self.pending and self.pending[0][3] <= context:
                pending.append(self.pending.popleft())
        tickers = self.tickers
        changed = set()
        broad = False
        for updates, allowed, tracked, _context in pending:
            global_batch = len(updates) >= 40
            accepted = False
            for raw in updates:
                ticker = raw if "s" in raw else {
                    **raw, "s": raw.get("symbol"), "c": raw.get("lastPrice"),
                    "P": raw.get("priceChangePercent"), "v": raw.get("volume"),
                    "q": raw.get("quoteVolume"),
                }
                symbol = str(ticker.get("s") or "")
                if not symbol or (symbol not in allowed if allowed else global_batch or symbol not in tracked):
                    continue
                previous = tickers.get(symbol, {})
                stamp = safe_float(ticker.get("E") or ticker.get("closeTime") or ticker.get("C"))
                old_stamp = safe_float(previous.get("_price_time"))
                if stamp < old_stamp:
                    continue
                merged = dict(previous)
                merged.update((key, value) for key, value in ticker.items() if value is not None)
                merged["s"], merged["_price_time"] = symbol, max(stamp, old_stamp)
                tickers[symbol] = merged
                changed.add(symbol)
                accepted = True
            broad |= global_batch and accepted
        # Exchange metadata can invalidate rows accepted before a universe refresh.
        if valid:
            tickers = {symbol: row for symbol, row in tickers.items() if symbol in valid}
            changed.intersection_update(valid)
        if self.context is not None and self.context != context:
            # Republish rows whose earlier result may have been discarded.
            changed.update(tickers)
            broad = True
        self.context = context
        self.tickers = tickers
        return dict(tickers), frozenset(changed), broad


class MainWindow(QtWidgets.QMainWindow):
    # Stable latest-wins handoff for the advanced DOM. The analyzer itself is
    # worker-thread-owned; only immutable presentation frames return to the GUI.
    order_flow_snapshot_ready = QtCore.Signal(object)
    _order_flow_reset_requested = QtCore.Signal(int, str, float, float)
    _order_flow_active_requested = QtCore.Signal(int, bool)
    _order_flow_quote_volume_requested = QtCore.Signal(int, float)
    _order_flow_depth_requested = QtCore.Signal(int, object, object, int, object)
    _order_flow_depth_capacity_requested = QtCore.Signal(int, int)
    _order_flow_book_ticker_requested = QtCore.Signal(int, object)
    _order_flow_trade_batch_requested = QtCore.Signal(int, object)
    _order_flow_interaction_priority_requested = QtCore.Signal(int, bool)
    _order_flow_shutdown_requested = QtCore.Signal()

    ORDER_FLOW_ACTIVE_DECAY_MS = 500
    ORDER_FLOW_IDLE_DECAY_MS = 1000
    # Analyzer snapshots are latest-wins and intentionally paced separately from
    # raw market ingress. 16 ms is a CPU-work cadence, not a paint/FPS cap: the
    # DOM may repaint at the display cadence from the newest prepared snapshot.
    ORDER_FLOW_SNAPSHOT_MIN_INTERVAL_MS = 16
    ORDER_FLOW_INTERACTION_SNAPSHOT_MIN_INTERVAL_MS = 33
    # B/S temporarily owns bare percentage digits; after this deadline the same
    # number key belongs to the normal chart timeframe dispatcher again.
    QUICK_ORDER_SEQUENCE_SECONDS = 1.0

    def __init__(
        self,
        symbol: str | None = None,
        interval: str | None = None,
        theme_name: str | None = None,
        testnet: bool = False,
        diagnostics: DiagnosticsPort | None = None,
    ):
        super().__init__()
        application = QtWidgets.QApplication.instance()
        self._native_application_palette = (
            QtGui.QPalette(application.palette()) if application is not None else None
        )
        self.settings = QSettings(ORG_NAME, APP_NAME)
        # Secondary shell presentation is dirty-driven. Chart navigation owns a
        # dedicated refresh-aware clock so ticker/UI work can never extend a
        # direct-manipulation frame. Order-flow is independently worker-paced.
        self.presentation_clock = PresentationClock(self, self)
        self.presentation_clock.frame.connect(self._flush_presentation_frame)
        # Useful hovers are always available. Surface policies exclude the
        # order book and restrict trading guidance to time-in-force options.
        self.learning_mode = True
        try:
            saved_timeframes = json.loads(self.settings.value("ui/market_bar_timeframes_v1", "", str))
        except (TypeError, ValueError):
            saved_timeframes = None
        self.market_bar_timeframes = normalized_market_bar_timeframes(saved_timeframes)
        self.setProperty("learningMode", self.learning_mode)
        self.setProperty("chartTooltipsSuppressed", not self.learning_mode)
        # Expensive non-visible services are staged after the first shell show.
        # Ensure helpers keep user actions safe if they are reached first.
        self.app_db: Any | None = None
        self.hub: MarketDataHubPort | None = None
        startup_origin = (
            application.property("nightwatchStartupStartedMono")
            if application is not None
            else None
        )
        try:
            self._startup_started_mono = float(startup_origin)
        except (TypeError, ValueError):
            self._startup_started_mono = time.monotonic()
        self._startup_stage = 0
        self._startup_stages_pending = True
        self._startup_show_seen = False
        self._startup_first_paint_seen = False
        self._market_data_start_requested = False
        self._market_data_started_mono = 0.0
        self._warm_initial_chart_history_pending = False
        self._universe_prepare_token = 0
        self._universe_ready = False
        # Theme identity is a saved presentation preference. A CLI theme wins for
        # this launch; otherwise restore the last valid theme. Color overrides
        # remain an independent layer resolved after the selected base theme.
        stored_theme = self.settings.value("theme", DEFAULT_THEME_NAME, str)
        requested_theme = str(theme_name or stored_theme or DEFAULT_THEME_NAME)
        if requested_theme not in THEMES:
            requested_theme = DEFAULT_THEME_NAME
        self.theme_name = requested_theme
        self.settings.setValue("theme", self.theme_name)
        self.theme = THEMES[self.theme_name]
        # Legacy per-theme RGB appearance overrides are intentionally no longer
        # part of the user-facing color path. Keep the loader for old profiles,
        # but ordinary presentation now uses semantic Theme / Classic modes.
        self.settings.remove("appearance/directional_colors_v1")
        self.directional_color_modes = self._load_directional_color_modes()
        self.developer_ui_colors = self._load_developer_ui_colors()
        self.developer_ui_layout = self._load_developer_ui_layout()
        self.developer_ui_surfaces = self._load_developer_ui_surfaces()
        self.developer_typography = self._load_developer_typography()
        self.developer_typography_globals = self._load_developer_typography_globals()
        configure_typography(
            self.developer_typography,
            self.developer_typography_globals,
            notify=False,
        )
        self.ui_theme = ui_palette(self.theme, self._effective_ui_color_overrides())
        self._refresh_directional_surface_palettes()
        self.developer_ui_status = self._load_developer_ui_status()
        # A new key intentionally makes the reference hollow/filled treatment
        # the default once, while preserving later user selections.
        stored_candle_style = self.settings.value("candle_style_v4", "Hollow", str)
        stored_candle_style = {
            "Nightwatch": "Inked",
            "Solid": "Inked",
            "Outline": "Hollow",
            "Pulse": "Luminous",
            "Minimal": "Inked",
            "Exchange": "Inked",
            "High contrast": "Inked",
            "Kraken": "Inked",
            "TradingView": "Inked",
        }.get(stored_candle_style, stored_candle_style)
        self.candle_style = (
            stored_candle_style if stored_candle_style in CANDLE_STYLES else "Hollow"
        )
        self.auto_scale = self.settings.value("auto_scale", True, bool)
        self.logarithmic = self.settings.value("logarithmic", False, bool)
        # Every launch starts with a clean chart.  Indicator parameter settings
        # remain persistent, but active/visible indicator state is deliberately
        # session-local so a previous workspace cannot surprise the trader on
        # startup with stale studies or extra panes.
        self.indicators_enabled = True
        self.saved_indicators = self._default_indicators()
        # Active indicator state is intentionally session-local. Remove the
        # retired persisted key so future code cannot accidentally resurrect it.
        self.settings.remove("chart/active_indicators_v1")
        self.saved_study_heights: dict[str, float] = {}
        self.volume_bar_height_percent = max(
            5,
            min(
                45,
                int(
                    self.settings.value(
                        "chart/volume_bar_height_pct_v1",
                        30,
                        int,
                    )
                ),
            ),
        )
        stored_study_heights = self.settings.value("chart/study_heights_v1", "", str)
        if stored_study_heights:
            try:
                restored_heights = json.loads(stored_study_heights)
                if isinstance(restored_heights, dict):
                    self.saved_study_heights = {
                        str(name): float(value) for name, value in restored_heights.items()
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                self.saved_study_heights = {}
        self.indicator_settings = {
            name: dict(values) for name, values in INDICATOR_SETTING_DEFAULTS.items()
        }
        stored_indicator_settings = self.settings.value(
            "chart/indicator_settings_v1", "", str
        )
        if stored_indicator_settings:
            try:
                restored = json.loads(stored_indicator_settings)
            except (TypeError, json.JSONDecodeError):
                restored = {}
            if isinstance(restored, dict):
                for name, defaults in self.indicator_settings.items():
                    if isinstance(restored.get(name), dict):
                        defaults.update(restored[name])
        else:
            self.indicator_settings["RSI"] = {
                "period": self.settings.value("chart/rsi_period", 14, int),
                "upper": self.settings.value("chart/rsi_upper", 70.0, float),
                "lower": self.settings.value("chart/rsi_lower", 30.0, float),
                "show_thresholds": False,
            }
        if not self.settings.value("chart/liquidation_display_v2", False, bool):
            liquidation_settings = self.indicator_settings["Liquidations"]
            if safe_float(liquidation_settings.get("minimum_notional")) <= 0:
                liquidation_settings["minimum_notional"] = 100_000.0
            if int(liquidation_settings.get("maximum_markers", 250)) == 250:
                liquidation_settings["maximum_markers"] = 120
            self.settings.setValue("chart/liquidation_display_v2", True)
        restored_shortcuts: dict[str, object] = {}
        stored_indicator_shortcuts = self.settings.value(
            "chart/indicator_shortcuts_v1", "", str
        )
        if stored_indicator_shortcuts:
            try:
                restored_shortcuts = json.loads(stored_indicator_shortcuts)
            except (TypeError, json.JSONDecodeError):
                restored_shortcuts = {}
            if not isinstance(restored_shortcuts, dict):
                restored_shortcuts = {}
        self.indicator_shortcuts: dict[str, str] = {}
        used_shortcuts: set[str] = set()
        legacy_indicator_moves = {
            "Major Price Levels": ("Ctrl+A", "Ctrl+4"),
            "Funding Rate History": ("Ctrl+S", "Ctrl+5"),
            "RSI": ("Ctrl+D", "Ctrl+6"),
        }
        explicitly_claimed_shortcuts = {
            str(value)
            for name, value in restored_shortcuts.items()
            if name not in legacy_indicator_moves and str(value)
        }
        for name in INDICATOR_KEYS:
            shortcut = str(
                restored_shortcuts.get(name, constants.DEFAULT_INDICATOR_SHORTCUTS[name])
            )
            legacy_move = legacy_indicator_moves.get(name)
            if (
                legacy_move is not None
                and shortcut == legacy_move[0]
                and legacy_move[1] not in explicitly_claimed_shortcuts
            ):
                shortcut = legacy_move[1]
            if shortcut in set("0123456789"):
                shortcut = constants.DEFAULT_INDICATOR_SHORTCUTS[name]
            allowed = {"Ctrl+" + k for k in "123QWEASD4567890"}
            if (shortcut in allowed and shortcut not in used_shortcuts
                    and shortcut not in constants.SHELL_RESERVED_SHORTCUTS):
                self.indicator_shortcuts[name] = shortcut
                used_shortcuts.add(shortcut)
            else:
                self.indicator_shortcuts[name] = ""
        self.right_layout_presets = json.loads(json.dumps(RIGHT_LAYOUT_PRESETS))
        # The new presets persist actual splitter trees, not panel membership.
        try:
            configured = json.loads(self.settings.value("right_layout_presets_v2", "", str) or "{}")
            if isinstance(configured, dict):
                restored_presets = {}
                for name, preset in configured.items():
                    if not isinstance(preset, dict) or not str(name).strip() or name == "Custom":
                        continue
                    visible = valid_panel_names(preset.get("visible", ()), RIGHT_PANEL_NAMES)
                    tree = decode_tree(preset.get("tree"))
                    validate_tree(tree)
                    aliases = dict(zip(RIGHT_PANEL_NAMES, ("depth", "trading", "trades", "watchlist")))
                    if set(panel_ids(tree)) != {aliases[n] for n in visible}:
                        continue
                    restored_presets[name] = {**preset, "visible": visible}
                if restored_presets:
                    self.right_layout_presets = restored_presets
        except (ValueError, TypeError, RecursionError):
            pass
        self.right_layout_preset = self.settings.value("right_layout_preset", "Balanced", str)
        self._migrate_desk_layout = self.settings.value("right_rail/desk_layout_generation", 0, int) < 2
        if self._migrate_desk_layout:
            self.right_layout_presets.setdefault("Balanced", json.loads(json.dumps(RIGHT_LAYOUT_PRESETS["Balanced"])))
        if self._migrate_desk_layout or self.right_layout_preset not in (*self.right_layout_presets, "Custom"):
            self.right_layout_preset = "Balanced" if "Balanced" in self.right_layout_presets else next(iter(self.right_layout_presets))
        # Right-rail geometry/visibility is loaded once by RightRailController
        # after the actual panel contents exist. These aliases are synchronized
        # from its authoritative state for legacy call sites and UI labels.
        self.ticker_sort_mode = self.settings.value("ticker_sort_v2", "gainers", str)
        if self.ticker_sort_mode not in MARKET_SORT_MODES:
            self.ticker_sort_mode = "gainers"
        self.testnet = testnet
        self._composition = AppComposition(self, self.testnet, diagnostics=diagnostics)
        self.indicator_rest: BinanceRest | None = None
        stored_symbol = self.settings.value("last_symbol", DEFAULT_SYMBOL, str)
        stored_interval = self.settings.value("last_interval", DEFAULT_INTERVAL, str)
        self.current_symbol = self._normalize_symbol(symbol or stored_symbol or DEFAULT_SYMBOL)
        self.saved_drawings_by_symbol: dict[str, list[dict[str, Any]]] = {}
        stored_drawings = self.settings.value("chart/manual_drawings_v1", "", str)
        if stored_drawings:
            try:
                decoded_drawings = json.loads(stored_drawings)
            except (TypeError, json.JSONDecodeError):
                decoded_drawings = {}
            if isinstance(decoded_drawings, dict):
                self.saved_drawings_by_symbol = {
                    self._normalize_symbol(str(saved_symbol)): list(drawings)
                    for saved_symbol, drawings in decoded_drawings.items()
                    if self._normalize_symbol(str(saved_symbol))
                    and isinstance(drawings, list)
                }
        # Chart-layer visibility is deliberately session-only. Every launch
        # starts with both indicators and drawings visible.
        self.chart_visibility_mode = 0
        self._order_flow_generation = 1
        self._order_flow_tick_size = 0.0
        self._order_flow_depth_capacity = 120
        self._order_flow_depth_timing_counter = 0
        self._order_flow_diagnostic_state: dict[str, Any] = {}
        self._order_flow_runtime_thread = QtCore.QThread(self)
        self._order_flow_runtime_thread.setObjectName("nightwatch-order-flow")
        self._order_flow_runtime = OrderFlowRuntime(
            self.current_symbol,
            min_snapshot_interval_ms=self.ORDER_FLOW_SNAPSHOT_MIN_INTERVAL_MS,
            interaction_snapshot_interval_ms=(
                self.ORDER_FLOW_INTERACTION_SNAPSHOT_MIN_INTERVAL_MS
            ),
            active_decay_ms=self.ORDER_FLOW_ACTIVE_DECAY_MS,
            idle_decay_ms=self.ORDER_FLOW_IDLE_DECAY_MS,
        )
        self._order_flow_runtime.moveToThread(self._order_flow_runtime_thread)
        self._order_flow_reset_requested.connect(
            self._order_flow_runtime.reset_model, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_active_requested.connect(
            self._order_flow_runtime.set_active, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_quote_volume_requested.connect(
            self._order_flow_runtime.set_quote_volume, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_depth_requested.connect(
            self._order_flow_runtime.add_depth, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_depth_capacity_requested.connect(
            self._order_flow_runtime.set_depth_capacity,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        self._order_flow_book_ticker_requested.connect(
            self._order_flow_runtime.add_book_ticker, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_trade_batch_requested.connect(
            self._order_flow_runtime.add_trade_batch, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_interaction_priority_requested.connect(
            self._order_flow_runtime.set_interaction_priority,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        self._order_flow_shutdown_requested.connect(
            self._order_flow_runtime.shutdown, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_runtime.snapshot_ready.connect(
            self._on_order_flow_runtime_snapshot, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_runtime.microstructure_ready.connect(
            self._on_microstructure_runtime_snapshot, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_runtime.diagnostic_ready.connect(
            self._on_order_flow_runtime_diagnostic, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_runtime.failed.connect(
            self._on_order_flow_runtime_failed, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._order_flow_runtime.restarted.connect(self._on_order_flow_runtime_restarted, QtCore.Qt.ConnectionType.QueuedConnection)
        self._order_flow_runtime_thread.finished.connect(self._order_flow_runtime.deleteLater)
        self._order_flow_runtime_thread.start()
        self._order_flow_reset_requested.emit(
            self._order_flow_generation, self.current_symbol, 0.0, 0.0
        )
        # Combined order-flow analytics are valid only while both the public
        # depth/BBO and market trade sockets are live. Reconnects form a hard
        # inference boundary; state is reset rather than bridged across a gap.
        self._market_data_live = False
        self._book_valid = False
        self._book_valid_reason = "STARTING"
        self._frontend_submission_requests: set[str] = set()
        self._frontend_unknown_requests: set[str] = set()
        # Magnetic-rail submissions/cancellations are tracked independently by
        # gateway request ID so several armed rails may coexist safely.
        self._magnetic_rail_requests: dict[str, dict[str, Any]] = {}
        # Rail submission remains one user action even when Binance leverage must
        # be changed first. Entries are keyed by chart identity + draft id so the
        # leverage acknowledgement can resume the exact rail without a second keypress.
        self._magnetic_rail_leverage_waits: dict[tuple[int, int], dict[str, Any]] = {}
        self._magnetic_rail_cancel_requests: dict[str, dict[str, Any]] = {}
        self._recent_submission_fingerprints: dict[str, float] = {}
        self._order_flow_snapshot_active = False
        self._market_depth_active = False
        self.last_microstructure_signal_id = 0
        self.recent_microstructure_signals: deque[tuple[str, MicrostructureSnapshot]] = deque(maxlen=12)
        requested_interval = interval or stored_interval
        self.current_interval = requested_interval if requested_interval in TIMEFRAMES else DEFAULT_INTERVAL
        stored_chart_layout = self.settings.value(
            "chart/layout_mode_v1", "Single", str
        )
        self.chart_layout_mode = (
            stored_chart_layout if stored_chart_layout in CHART_LAYOUTS else "Single"
        )
        self.saved_auxiliary_charts: list[dict[str, str]] = []
        stored_auxiliary_charts = self.settings.value(
            "chart/auxiliary_markets_v1", "", str
        )
        if stored_auxiliary_charts:
            try:
                decoded_auxiliary_charts = json.loads(stored_auxiliary_charts)
            except (TypeError, json.JSONDecodeError):
                decoded_auxiliary_charts = []
            if isinstance(decoded_auxiliary_charts, list):
                self.saved_auxiliary_charts = [
                    item for item in decoded_auxiliary_charts if isinstance(item, dict)
                ][:3]
        self.testing_flags = {
            "chart_opengl": self.settings.value(
                "testing/chart_opengl_v2", True, bool
            ),
            "chart_opengl_full_viewport": self.settings.value(
                "testing/chart_opengl_full_viewport_v5", False, bool
            ),
            "multiple_chart_layouts": self.settings.value(
                "testing/multiple_chart_layouts_v1", True, bool
            ),
            "magnetic_order_rail": self.settings.value(
                "testing/magnetic_order_rail_v1", True, bool
            ),
            "chart_native_bar_renderer": self.settings.value(
                "testing/chart_native_bar_renderer_v1", True, bool
            ),
            "chart_lod_aggregation": self.settings.value(
                "testing/chart_lod_aggregation_v1", True, bool
            ),
        }
        self.magnetic_rail_config = self._load_magnetic_rail_config()
        self.magnetic_rail_order_presets, self.active_magnetic_rail_order_preset = self._load_magnetic_rail_order_presets()
        self.settings_dialog: NightwatchSettingsDialog | None = None
        self.developer_dialog: DeveloperDialog | None = None
        self.ui_tuner_dialog: UiTunerDialog | None = None
        self.magnetic_rail_lab_dialog: MagneticRailLabDialog | None = None
        self.trading_hotkeys = dict(DEFAULT_TRADING_HOTKEYS)
        stored_hotkeys = self.settings.value("trading/hotkeys_v1", "", str)
        if stored_hotkeys:
            try:
                decoded_hotkeys = json.loads(stored_hotkeys)
            except (TypeError, json.JSONDecodeError):
                decoded_hotkeys = {}
            if isinstance(decoded_hotkeys, dict):
                for action in self.trading_hotkeys:
                    if action in decoded_hotkeys:
                        shortcut = str(decoded_hotkeys[action])
                        self.trading_hotkeys[action] = (
                            DEFAULT_TRADING_HOTKEYS[action]
                            if is_shift_letter_shortcut(shortcut)
                            or is_smart_exit_shortcut(shortcut)
                            or shortcut in constants.SHELL_RESERVED_SHORTCUTS
                            else shortcut
                        )
        self.quick_trading_preset = dict(constants.DEFAULT_QUICK_TRADING_PRESET)
        stored_quick_preset = self.settings.value("trading/quick_preset_v1", "", str)
        if stored_quick_preset:
            try:
                decoded_quick_preset = json.loads(stored_quick_preset)
            except (TypeError, json.JSONDecodeError):
                decoded_quick_preset = {}
            if isinstance(decoded_quick_preset, dict):
                self.quick_trading_preset.update(
                    {
                        key: value
                        for key, value in decoded_quick_preset.items()
                        if key in constants.DEFAULT_QUICK_TRADING_PRESET
                    }
                )
        self.best_bid = 0.0
        self.best_ask = 0.0
        self._armed_order_side = ""
        self._armed_order_deadline = 0.0
        self._armed_order_timer = QTimer(self)
        self._armed_order_timer.setSingleShot(True)
        self._armed_order_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._armed_order_timer.setInterval(
            max(1, int(round(self.QUICK_ORDER_SEQUENCE_SECONDS * 1000.0)))
        )
        self._armed_order_timer.timeout.connect(self._expire_armed_order_sequence)
        self.valid_symbols: set[str] = set()
        self.symbol_rules: dict[str, SymbolRules] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self._ticker_preparation = _TickerPreparation()
        self._ticker_prepare_serial = 0
        self._ticker_prepare_pool = QtCore.QThreadPool(self)
        self._ticker_prepare_pool.setMaxThreadCount(1)
        self._ticker_prepare_job = LatestJob(self._ticker_prepare_pool, self, accept_intermediate=True)
        self._ticker_prepare_job.ready.connect(self._tickers_prepared)
        self._ticker_prepare_job.failed.connect(self._ticker_prepare_failed)

        self.last_price = 0.0
        self.tasks: set[ApiTask] = set()
        self.coin_icon_refresh_task: ApiTask | None = None
        self.coin_icon_lookup_tasks: dict[str, ApiTask] = {}
        self._coin_icon_deferred_missing: set[str] = set()
        # Worker callbacks can outlive the Qt surface during shutdown.  They
        # must become no-ops before child widgets / C++ wrappers are torn down.
        self._closing = False
        self.indicator_history_tasks: dict[str, ApiTask] = {}
        self.pending_ticker_symbols: set[str] = set()
        self.ticker_rank_dirty = False
        self._ticker_rank_timer = QTimer(self)
        self._ticker_rank_timer.setSingleShot(True)
        self._ticker_rank_timer.timeout.connect(self._flush_ticker_rank)
        self._deferred_mark_payload: dict[str, Any] | None = None
        self._deferred_interest_payload: dict[str, Any] | None = None
        self.market_filter_timeframe = "24h"
        self.market_filter_min_volume = 0.0
        self.market_volume_cache: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}
        self.market_volume_task: ApiTask | None = None
        self.pending_market_filter: tuple[str, float, str] | None = None
        self.search_hour_cache: dict[str, tuple[float, float]] = {}
        self.search_hour_attempts: dict[str, float] = {}
        self.search_hour_queue: list[str] = []
        self.search_hour_task: ApiTask | None = None
        self.watchlist_hour_task: ApiTask | None = None
        self.watchlist_hour_refresh_pending = False
        self.market_detail: dict[str, Any] = {}
        self.history_task: ApiTask | None = None
        self.lazy_chart_history_task: ApiTask | None = None
        self.history_cancel_requested = False
        self.market_history_dialog: MarketHistoryDownloadDialog | None = None
        self._close_after_market_history = False
        self._restore_maximized_after_fullscreen = True
        self._restore_geometry_after_fullscreen: QtCore.QByteArray | None = None
        self._restore_normal_rect_after_fullscreen: QtCore.QRect | None = None
        self._fullscreen_requested = False
        self._fullscreen_transition_serial = 0
        # Windows + QOpenGLWidget true fullscreen has a documented DWM popup
        # stacking limitation.  Use borderless-windowed fullscreen there instead.
        self._windows_borderless_fullscreen = False
        self._windows_windowed_style: int | None = None
        self._windows_windowed_ex_style: int | None = None
        self.symbol_search_dialog: SymbolSearchDialog | None = None
        self.symbol_search_pending = False
        self._syncing_drawing_buttons = False
        self.market_event_buffer: list[tuple[str, str, int, dict[str, Any]]] = []
        self._market_event_dropped = 0
        self._market_event_error = ""
        self._close_waiting_for_recorder = False
        self._market_event_pool = QtCore.QThreadPool(self)
        self._market_event_pool.setMaxThreadCount(1)
        self.market_event_task: ApiTask | None = None
        self.last_recorded_depth = 0.0
        self.last_recorded_funding = 0.0
        self._top_metrics_dirty = False
        self._top_metrics_timer = QTimer(self)
        self._top_metrics_timer.setSingleShot(True)
        self._top_metrics_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._top_metrics_timer.timeout.connect(self._flush_top_metrics_if_idle)
        self.start_maximized = True
        self.start_fullscreen = True
        self._ui_resize_active = False
        self._active_splitter_drags: set[int] = set()
        self._right_rail_geometry_signature: tuple[object, ...] | None = None
        self._market_data_started = False
        self._latest_trading_snapshot: dict[str, Any] = {}
        self._chart_orders_signature: tuple[Any, ...] | None = None
        self.trading_gateway: TradingGatewayPort = self._composition.create_trading_gateway()
        # Session-only safety gate: the first attempted trade after arming is
        # confirmed once, then every later order in this app run is immediate.
        self._first_trade_confirmation_complete = False
        self.pending_protections: dict[str, dict[str, Any]] = {}
        self.protection_request_clients: dict[str, str] = {}
        self.submitted_protection_clients: set[str] = set()
        self.active_protection_legs: dict[str, dict[str, Any]] = {}
        self.emergency_guards: dict[str, dict[str, Any]] = {}
        self.emergency_close_requests: dict[str, dict[str, Any]] = {}
        self.emergency_reserved: dict[tuple[str, str], float] = {}
        self._emergency_tranches: set[str] = set()
        self.emergency_timer = QTimer(self)
        self.emergency_timer.setInterval(100)
        self.emergency_timer.timeout.connect(self._check_emergency_guards)

        self.setWindowTitle(f"{APP_NAME} · Binance USD-M")
        screen = QtGui.QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1720, 1040)
        self.setMinimumHeight(min(600, max(320, available.height() - 80)))
        self.resize(min(1720, available.width()), min(1040, available.height()))
        self._build_ui()
        self._sync_shell_minimum_width()
        # Synchronize ChartWorkspace with the canonical restored/requested timeframe
        # before any layout or drawing state is restored. Startup market data is
        # requested with ``self.current_interval``; without this preparation the
        # chart keeps its constructor default interval and silently rejects the
        # first bootstrap snapshot/live klines when the persisted timeframe differs.
        self.chart.prepare_market(self.current_interval, reset_analysis=True)
        self._apply_developer_ui_layout()
        self._apply_developer_ui_status()
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._apply_stylesheet()
        self._reset_top_metrics()
        # Chart interaction priority protects direct manipulation frame time.
        # Cheap visible state stays at display cadence; only expensive broad
        # ranking/analytics recomputation is deferred until the gesture settles.
        self._chart_interaction_priority_active = False
        self._interaction_deferred_ticker_ui = False
        self.market_event_timer = QTimer(self)
        self.market_event_timer.setInterval(10_000)
        self.market_event_timer.timeout.connect(self._flush_market_events)
        self.market_event_timer.start()
        self.watchlist_hour_timer = QTimer(self)
        self.watchlist_hour_timer.setInterval(60_000)
        self.watchlist_hour_timer.timeout.connect(
            self._refresh_watchlist_hour_changes
        )
        self.watchlist_hour_timer.start()
        self.search_hour_timer = QTimer(self)
        self.search_hour_timer.setSingleShot(True)
        self.search_hour_timer.setInterval(60_000)
        self.search_hour_timer.timeout.connect(lambda: self._refresh_search_hour_changes(restart=True))
        self.resize_settle_timer = QTimer(self)
        self.resize_settle_timer.setSingleShot(True)
        self.resize_settle_timer.setInterval(140)
        self.resize_settle_timer.timeout.connect(self._finish_ui_resize)
        self.right_rail_controller.drag_started.connect(
            lambda: self._begin_splitter_drag(self.right_rail_controller)
        )
        self.right_rail_controller.drag_ended.connect(
            lambda: self._end_splitter_drag(self.right_rail_controller)
        )
        self.main_splitter.splitterMoved.connect(lambda *_args: self._note_ui_resize_activity())

        self.alert_center = AlertCenter(self)
        self.alert_center.in_app.connect(self.alerts_panel.append_alert)
        self.alert_center.manual_alerts_changed.connect(
            self.alerts_panel.set_active_count
        )
        self.alert_center.manual_alerts_changed.connect(self._sync_ticker_streams)
        self.alerts_panel.cancel_all_requested.connect(
            self.alert_center.clear_manual_alerts
        )
        self.chart.analysis_changed.connect(self.alert_center.set_analysis)
        self.alerts_panel.add_button.clicked.connect(self.add_price_alert)
        self.alerts_panel.settings_button.clicked.connect(self.edit_alert_settings)

        # MarketDataHub construction and signal wiring are deferred until
        # after first show; start_market_data() remains the only start gate.
        self.trading_workspace.order_requested.connect(self._submit_order)
        self.trading_workspace.batch_orders_requested.connect(
            self._submit_batch_orders
        )
        for ticket in self._trading_tickets():
            ticket.quick_settings_requested.connect(self.edit_quick_trading_settings)
        self.trading_gateway.request_succeeded.connect(self._trade_request_succeeded)
        self.trading_gateway.request_failed.connect(self._trade_request_failed)
        self.trading_gateway.leverage_changed.connect(self._magnetic_rail_leverage_changed)
        self.trading_gateway.armed_changed.connect(self._trading_armed_changed)
        self.trading_gateway.account_event.connect(self._trading_account_event)
        self.trading_gateway.protections_recovered.connect(self._restore_saved_protections)
        self.trading_gateway.snapshot_ready.connect(self._sync_chart_working_orders)
        self.trading_gateway.snapshot_ready.connect(self._sync_trading_snapshot)
        self.trading_gateway.snapshot_ready.connect(self._sync_ticker_streams)
        self.trading_gateway.problem.connect(self._on_problem)
        self.rail_amendments = RailAmendments(self, diagnostics=diagnostics)
        self.chart.working_order_moved.connect(self._modify_order_from_chart)
        self.chart_container.auxiliary_working_order_moved.connect(self._modify_order_from_chart)
        self.chart.order_rail_execution_requested.connect(
            lambda state: self._execute_magnetic_rail_order(
                self.current_symbol, state, self.chart
            )
        )
        self.chart.order_rail_cancel_requested.connect(
            lambda state: self._cancel_magnetic_rail_order(
                self.current_symbol, state, self.chart
            )
        )
        self.chart.order_rail_leverage_requested.connect(
            lambda leverage: self._request_magnetic_rail_leverage(
                self.current_symbol, int(leverage)
            )
        )
        self.chart_container.auxiliary_order_rail_execution_requested.connect(
            self._execute_magnetic_rail_order
        )
        self.chart_container.auxiliary_order_rail_cancel_requested.connect(
            self._cancel_magnetic_rail_order
        )
        self.chart_container.auxiliary_order_rail_leverage_requested.connect(
            self._request_magnetic_rail_leverage
        )
        self.chart_container.auxiliary_market_changed.connect(
            self._sync_auxiliary_chart_working_orders
        )
        self.chart.history_requested.connect(self._request_older_chart_history)
        self.orderbook.price_selected.connect(self._prefill_order_price)
        self.orderbook.snapshot_activity_requested.connect(
            self._set_market_depth_active
        )
        self.orderbook.depth_capacity_requested.connect(
            self._set_order_flow_depth_capacity
        )
        self.orderbook.set_tape_source(self._order_flow_runtime.tape_source)
        self.order_flow_snapshot_ready.connect(self.orderbook.set_order_flow_snapshot)
        self._sync_execution_ticket_state()
        self.watchlist.symbol_selected.connect(self._open_watchlist_symbol)
        self.watchlist.symbols_changed.connect(self._watchlist_changed)
        self.watchlist.groups_changed.connect(self._watchlist_groups_changed)

        self._restore_layout()
        self._restore_current_symbol_drawings()
        self._apply_chart_visibility_mode(show_status=False)
        self._select_timeframe_button(self.current_interval)
        self.alert_center.set_market(self.current_symbol, self.current_interval)
        self.watchlist.set_current(self.current_symbol)
        self._sync_ticker_streams()
        # showEvent/hideEvent own normal activation. This zero-delay sync covers
        # restored tab/panel visibility before the first market frame arrives and
        # drives the order-book network transport itself, not just DOM painting.
        QTimer.singleShot(0, self._sync_market_depth_networking)
        self._startup_mark("MainWindow ready · deferred stages pending")

    def start_market_data(self) -> None:
        """Request feed start after chart readiness; first-paint staging owns construction."""
        if self._market_data_started:
            return
        self._market_data_start_requested = True
        self._maybe_start_market_data()

    def _maybe_start_market_data(self) -> None:
        """Start feeds only when readiness requested and staged services are available."""
        if (
            self._market_data_started
            or not self._market_data_start_requested
            or not self._startup_first_paint_seen
            or self.hub is None
            or self._closing
        ):
            return
        self._market_data_started = True
        self._market_data_started_mono = time.perf_counter()
        self._startup_mark("market data start")
        self.hub.start(self.current_symbol, self.current_interval)
        QTimer.singleShot(1200, self._refresh_watchlist_hour_changes)

    # ------------------------------------------------------------------
    # Startup staging
    # ------------------------------------------------------------------

    def _startup_mark(self, label: str) -> None:
        elapsed = (time.monotonic() - self._startup_started_mono) * 1000.0
        line = f"{label} · +{elapsed:.0f} ms"
        print(f"[Nightwatch startup] {line}")

    def _ensure_app_database(self) -> Any:
        db = self.app_db
        if db is None:
            db = self._composition.create_database()
            self.app_db = db
            self._startup_mark("research database ready")
        return db

    def _ensure_indicator_rest(self) -> BinanceRest:
        rest = self.indicator_rest
        if rest is None:
            rest = BinanceRest(self.testnet)
            self.indicator_rest = rest
        return rest

    def _ensure_market_data_hub(self) -> MarketDataHubPort:
        hub = self.hub
        if hub is not None:
            return hub
        db = self._ensure_app_database()
        hub = self._composition.create_market_data_hub(db)
        self.hub = hub
        hub.set_interaction_priority(self._chart_interaction_priority_active or self._ui_resize_active)
        hub.set_market_data_api_key(self.trading_gateway.api_key)
        hub.set_orderbook_streaming_enabled(self._market_depth_should_stream())
        hub.set_orderbook_depth_capacity(self._order_flow_depth_capacity)
        self.stats.bind_metric_history(hub.rest)
        self.trading_gateway.credentials_changed.connect(
            hub.set_market_data_api_key
        )
        self.trading_gateway.credentials_changed.connect(
            self.stats.invalidate_metric_histories
        )
        if self.market_board is not None:
            self.market_board.bind_sources(
                hub.rest, db, self.watchlist, lambda: True
            )
        if self.sector_overview is not None and self.market_board is not None:
            self.sector_overview.bind_sources(
                hub.rest, db, self.market_board, lambda: True
            )
        hub.universe_ready.connect(self._on_universe)
        hub.bootstrap_ready.connect(self._on_bootstrap)
        hub.analysis_ready.connect(self._on_analysis)
        hub.interest_history_ready.connect(self._on_interest_history)
        hub.ticker_batch.connect(self._on_tickers)
        hub.kline.connect(self._on_kline)
        hub.depth.connect(self._on_depth)
        hub.book_ticker.connect(self._on_book_ticker)
        hub.mark_price.connect(self._on_mark)
        hub.liquidation.connect(self._on_liquidation)
        hub.trade_batch.connect(self._on_trade_batch)
        hub.interest.connect(self._on_interest)
        hub.status.connect(self._on_status)
        hub.book_validity.connect(self._on_book_validity)
        hub.trade_stream_status.connect(self._on_order_flow_trade_stream_status)
        hub.problem.connect(self._on_problem)
        self._startup_mark("market data hub ready")
        self._sync_ticker_streams()
        return hub


    def _run_deferred_startup_stage(self) -> None:
        """Stage non-visible startup work across separate event-loop turns."""
        if self._closing:
            return
        stage = self._startup_stage
        self._startup_stage += 1
        if stage == 0:
            self._ensure_app_database()
            QTimer.singleShot(0, self._run_deferred_startup_stage)
        elif stage == 1:
            self._ensure_market_data_hub()
            self._maybe_start_market_data()
            QTimer.singleShot(0, self._run_deferred_startup_stage)
        elif stage == 2:
            # The chronological Time & Sales implementation is retained for a
            # future dedicated right-rail panel, but it is no longer materialized
            # as an embedded peer of the order book.
            QTimer.singleShot(0, self._run_deferred_startup_stage)
        elif stage == 3:
            self._configure_crisp_ui()
            self._startup_mark("deferred startup stages complete")
            # Settings pages are built only on request. Hidden configuration
            # widgets must not compete with market data or chart presentation.

    def _fit_normal_window_to_available_height(self) -> None:
        """Keep restored normal mode inside the current monitor work area.

        Nightwatch is vertically dense; a stale short normal-window geometry can
        hide useful terminal space even though the monitor has room. Preserve
        the user's horizontal width/placement while making the normal window use
        the monitor's available vertical work area (excluding the taskbar/dock).
        """
        if self.isFullScreen() or self.isMaximized() or self._effective_fullscreen():
            return
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        margins = handle.frameMargins() if handle is not None else QtCore.QMargins()
        current = self.geometry()
        max_client_width = max(1, available.width() - margins.left() - margins.right())
        width = min(max(self.minimumWidth(), current.width()), max_client_width)
        height = max(
            self.minimumHeight(),
            available.height() - margins.top() - margins.bottom(),
        )
        height = min(height, max(1, available.height()))
        frame_width = width + margins.left() + margins.right()
        frame_left = current.left() - margins.left()
        min_frame_left = available.left()
        max_frame_left = max(
            min_frame_left, available.right() - frame_width + 1
        )
        frame_left = max(min_frame_left, min(frame_left, max_frame_left))
        client_left = frame_left + margins.left()
        client_top = available.top() + margins.top()
        target = QtCore.QRect(client_left, client_top, width, height)
        if current != target:
            self.setGeometry(target)

    def _enter_startup_fullscreen(self) -> None:
        """Apply the always-fullscreen startup contract after the first Qt show."""
        if (
            not self.start_fullscreen
            or self._closing
            or self._effective_fullscreen()
            or self._fullscreen_requested
        ):
            return
        action = getattr(self, "fullscreen_action", None)
        if action is not None:
            if action.isChecked():
                # Recover if another bootstrap path checked the QAction while its
                # toggled signal was blocked or otherwise failed to start entry.
                self._toggle_fullscreen(True)
            else:
                action.setChecked(True)
            return
        self._toggle_fullscreen(True)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if not self.start_fullscreen and not self.start_maximized:
            QTimer.singleShot(0, self._fit_normal_window_to_available_height)
        if not self._startup_show_seen:
            self._startup_show_seen = True
            self._startup_mark("first show")
            # Keep fullscreen startup owned by MainWindow rather than depending
            # on a particular entrypoint revision. The zero-delay handoff lets
            # Qt finish native window creation/polish before the Windows
            # borderless transition touches the HWND.
            if self.start_fullscreen:
                QTimer.singleShot(0, self._enter_startup_fullscreen)
        QTimer.singleShot(0, self, self._restore_terminal_keyboard_focus)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        if self._startup_first_paint_seen:
            return
        self._startup_first_paint_seen = True
        self._startup_mark("first shell paint")
        self._maybe_start_market_data()
        if self._startup_stages_pending:
            self._startup_stages_pending = False
            QTimer.singleShot(0, self._run_deferred_startup_stage)

    def statusBar(self) -> TerminalStatusBar:
        """Return the embedded left-workspace status surface.

        Nightwatch intentionally does not use QMainWindow's full-width status-bar
        slot anymore. Keeping this compatibility method means every existing
        status message continues to target the same TerminalStatusBar API while
        the right rail is free to span the complete window height.
        """
        bar = getattr(self, "_terminal_status_bar", None)
        if isinstance(bar, TerminalStatusBar):
            return bar
        return super().statusBar()  # type: ignore[return-value]

    @profile_callback("app.update_status_fps_ms")
    def _update_status_fps(self) -> None:
        bar = getattr(self, "_terminal_status_bar", None)
        if bar is None:
            return
        actual, target, active = self.presentation_clock.frame_rate(1.0)
        bar.set_fps(actual, target, active=active)
        canvas = self.orderbook.canvas
        available = self._market_data_live and self._book_valid
        state = canvas.performance_state() if available and canvas.isVisible() else {}
        render_ms = (
            state.get("last_snapshot_to_paint_ms")
            if state.get("pipeline_dom_to_paint_samples", 0) > 0 and not state.get("worker_error")
            else None
        )
        bar.set_orderbook_latency(render_ms, available=available)
        self._sync_execution_ticket_state()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        central.setObjectName("centralRoot")
        outer = QtWidgets.QVBoxLayout(central)
        self._outer_layout = outer
        outer.setContentsMargins(
            DEV_UI_LAYOUT_DEFAULTS["outer_left"],
            DEV_UI_LAYOUT_DEFAULTS["outer_top"],
            DEV_UI_LAYOUT_DEFAULTS["outer_right"],
            DEV_UI_LAYOUT_DEFAULTS["outer_bottom"],
        )
        outer.setSpacing(0)
        self.setCentralWidget(central)
        self.watchlist = WatchlistWidget(self.ui_theme, central)
        self.watchlist.hide()

        toolbar_height = MainToolbar.HEIGHT
        news_ribbon_height = 40
        self._news_ribbon_height = news_ribbon_height

        # One-click panel layouts sit immediately to the left of Settings.
        self.panel_layout_button = QtWidgets.QPushButton()
        self.panel_layout_button.setObjectName("topUtilityButton")
        self.panel_layout_button.setFixedSize(toolbar_height, toolbar_height)
        self.panel_layout_button.setIconSize(QtCore.QSize(18, 18))
        self.panel_layout_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.panel_layout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.panel_layout_button.setAccessibleName("Cycle panel layout")
        self.panel_layout_button.clicked.connect(self._cycle_quick_panel_layout)
        self.settings_button = QtWidgets.QPushButton()
        self.settings_button.setObjectName("topUtilityButton")
        self.settings_button.setText("")
        self.settings_button.setFixedSize(
            toolbar_height, toolbar_height
        )
        self.settings_button.setIconSize(QtCore.QSize(15, 15))
        self.settings_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setToolTip("Settings")
        self.settings_button.setProperty("informationalToolTip", True)
        self.panel_layout_button.setProperty("informationalToolTip", True)
        self.settings_button.clicked.connect(
            lambda _checked=False: self._show_settings_window()
        )

        self.workspace_group = QtWidgets.QButtonGroup(self)
        self.workspace_group.setExclusive(True)
        self.workspace_buttons: dict[str, QtWidgets.QPushButton] = {}
        workspace_nav = QtWidgets.QFrame()
        workspace_nav.setObjectName("workspaceNav")
        workspace_nav.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        workspace_nav_layout = QtWidgets.QHBoxLayout(workspace_nav)
        self._workspace_nav_layout = workspace_nav_layout
        workspace_nav_layout.setContentsMargins(0, 0, 0, 0)
        workspace_nav_layout.setSpacing(0)
        workspace_nav_layout.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        for index, (name, width) in enumerate(
            (("CHART", 60), ("LEADERS", 78), ("SECTORS", 78), ("ROTATION", 84))
        ):
            button = QtWidgets.QPushButton(name.title())
            button.setObjectName("workspaceNavButton")
            button.setCheckable(True)
            button.setFixedSize(width, toolbar_height)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(f"{name.title()} · Alt+{index + 1}")
            button.setProperty("informationalToolTip", True)
            button.clicked.connect(lambda _checked=False, page=index: self._switch_workspace(page))
            self.workspace_group.addButton(button)
            self.workspace_buttons[name] = button
            workspace_nav_layout.addWidget(button)
        self.workspace_buttons["CHART"].setChecked(True)

        self.microstructure_card = MicrostructureNewsCard()
        self.microstructure_card.setObjectName("microstructureCard")
        self.microstructure_card.setFixedHeight(news_ribbon_height)
        self.microstructure_card.setMinimumWidth(260)
        self.microstructure_card.setMaximumWidth(16777215)
        self.microstructure_card.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.microstructure_card.setToolTip("Click to open recent short-term buy and sell signals")
        self.microstructure_card.clicked.connect(self._open_microstructure_signals)
        self._reset_microstructure_card()
        # Keep signal collection/detail state alive, but the news/alerts ribbon
        # is intentionally not part of the visible layout for now.
        self.microstructure_card.hide()

        self.stats = MarketStatsWidget(
            self.ui_theme,
            compact=True,
        )
        self.stats.set_timeframes(self.market_bar_timeframes, self.current_interval)
        self.stats.set_symbol(self.current_symbol)

        # Timeframe is part of the market-context cluster and remains owned by
        # MarketStatsWidget; MainWindow remains authoritative for switching the
        # chart/feed interval.
        self.stats.set_interval(self.current_interval)
        self.stats.timeframe_selected.connect(self.switch_interval)

        # Chart controls are represented by QActions and surfaced through the
        # unified Settings window. Keeping QAction state authoritative preserves
        # shortcuts and avoids duplicating chart state in the settings UI.
        self.indicator_actions: dict[str, QtGui.QAction] = {}
        for name, enabled in self.saved_indicators.items():
            shortcut = self.indicator_shortcuts.get(name, "") or "—"
            action = QtGui.QAction(f"{shortcut}   {name}", self)
            action.setCheckable(True)
            action.setChecked(enabled)
            if name == "Funding Rate History":
                action.setToolTip(
                    "Historical Binance funding settlements in a resizable lower pane. "
                    "Positive funding means longs paid shorts; negative means shorts paid longs. "
                    "Drag the pane's right axis vertically to scale it."
                )
            elif name == "Liquidations":
                action.setToolTip(
                    "Red down = forced long close; green up = forced short close. Arrow size follows liquidation value."
                )
            elif name == "Major Price Levels":
                action.setToolTip(
                    "Shows a configurable number of high-confidence support/resistance zones. Confirmed multi-timeframe "
                    "price reactions carry most of the score; volume-at-price and retests add confirmation."
                )
            action.toggled.connect(
                lambda state, value=name: self._set_indicator_enabled(value, state)
            )
            self.indicator_actions[name] = action

        self.auto_fibonacci_action = QtGui.QAction("Auto Fibonacci cycle · Alt+F", self)
        self.auto_fibonacci_action.setToolTip(
            "Cycle through the configured number of high-confidence automatic Fibonacci anchor interpretations. "
            "Alt+F moves forward; Shift+Alt+F moves backward; the cycle includes OFF. "
            "Display and levels are configured in Indicator settings."
        )
        self.auto_fibonacci_action.triggered.connect(
            lambda: self._cycle_auto_fibonacci(1)
        )
        self.indicator_settings_action = QtGui.QAction("Indicator settings...", self)
        self.indicator_settings_action.setToolTip(
            "Configure calculation and display parameters for each chart indicator"
        )
        self.indicator_settings_action.triggered.connect(self.edit_indicator_settings)
        self.indicator_shortcuts_action = QtGui.QAction("Indicator shortcuts...", self)
        self.indicator_shortcuts_action.setToolTip(
            "Assign Ctrl shortcuts for chart indicators"
        )
        self.indicator_shortcuts_action.triggered.connect(self.edit_indicator_shortcuts)

        self.ruler_action = QtGui.QAction("Ruler", self)
        self.ruler_action.setCheckable(True)
        self.ruler_action.setToolTip(
            "Measure price change, percentage, elapsed time and bars · select, then click two chart points"
        )
        self.ruler_action.toggled.connect(
            lambda checked: self._toggle_drawing_tool("ruler", checked)
        )
        self.fibonacci_action = QtGui.QAction("Fibonacci retracement", self)
        self.fibonacci_action.setCheckable(True)
        self.fibonacci_action.setToolTip(
            "Draw 0, .236, .382, .5, .618, .786 and 1 levels · select, then click two chart points"
        )
        self.fibonacci_action.toggled.connect(
            lambda checked: self._toggle_drawing_tool("fibonacci", checked)
        )
        self.horizontal_action = QtGui.QAction("Magnetic order rail", self)
        self.horizontal_action.setCheckable(True)
        self.horizontal_action.setVisible(self.testing_flags["magnetic_order_rail"])
        self.horizontal_action.setToolTip(
            "Place one tick-snapped magnetic order rail with a chart click · "
            "hover the rail body to configure and explicitly submit through the normal trading gateway"
        )
        self.horizontal_action.toggled.connect(self._toggle_order_rail_tool)
        self.clear_drawings_action = QtGui.QAction("Clear drawings", self)

        self.main_toolbar = MainToolbar(
            workspace_nav, self.panel_layout_button, self.settings_button, central
        )
        self.main_toolbar.apply_theme(self.ui_theme)
        outer.addWidget(self.main_toolbar)
        self.instrument_bar = InstrumentBar(self.stats)

        self.chart = ChartWorkspace(
            self.chart_theme,
            use_opengl=self.testing_flags["chart_opengl"],
            opengl_full_viewport=self.testing_flags["chart_opengl_full_viewport"],
            native_bar_renderer=self.testing_flags["chart_native_bar_renderer"],
            lod_aggregation=self.testing_flags["chart_lod_aggregation"],
            magnetic_order_rail_enabled=self.testing_flags["magnetic_order_rail"],
            presentation_clock=None,
        )
        # Performance capture must observe real viewport paints.  The chart keeps
        # its existing independent scheduler; registering the viewport is passive
        # instrumentation only and does not alter frame production.
        self._observe_chart_surface(self.chart)
        self.chart.snapshot_committed.connect(self._on_chart_snapshot_committed)
        self.chart.history_merged.connect(self._on_chart_history_merged)
        self.chart.set_order_rail_market_symbol(self.current_symbol)
        self.chart.set_candle_style(self.candle_style)
        self.chart.set_auto_scale(self.auto_scale)
        self.chart.set_logarithmic(self.logarithmic)
        self.chart.set_indicator_settings(self.indicator_settings)
        self.chart.set_study_heights(self.saved_study_heights)
        self.chart.set_volume_bar_height_percent(self.volume_bar_height_percent)
        self.chart.set_indicators_enabled(self.indicators_enabled)
        self.chart.set_order_rail_lab_config(self.magnetic_rail_config)
        self.chart.set_symbol_rules(
            self.symbol_rules.get(self.current_symbol, SymbolRules())
        )
        self._apply_active_magnetic_rail_order_preset()

        # Chart scale controls live in Settings → Chart, matching the chart
        # settings gear workflow.  The chart surface itself stays free of FIT/LOG
        # chrome; Alt+R remains the one-shot fit shortcut.
        self.clear_drawings_action.triggered.connect(
            lambda _checked=False: self.chart.clear_drawings()
        )
        self.chart.drawing_mode_changed.connect(self._sync_drawing_actions)
        self.chart.order_rail_placement_changed.connect(self._sync_order_rail_action)
        self.chart.auto_scale_changed.connect(self._sync_auto_scale_action)
        self.chart.indicator_settings_requested.connect(self.edit_indicator_settings)
        for name, action in self.indicator_actions.items():
            self._set_indicator_enabled(name, action.isChecked())
        self.chart_container = MultiChartContainer(
            self.chart,
            self.chart_theme,
            self._composition.create_chart_market_data_hub,
            use_opengl=self.testing_flags["chart_opengl"],
            opengl_full_viewport=self.testing_flags["chart_opengl_full_viewport"],
            native_bar_renderer=self.testing_flags["chart_native_bar_renderer"],
            lod_aggregation=self.testing_flags["chart_lod_aggregation"],
            magnetic_order_rail_enabled=self.testing_flags["magnetic_order_rail"],
            presentation_clock=None,
        )
        self.chart_container.restore_auxiliary(self.saved_auxiliary_charts)
        self.chart_container.chart_added.connect(self._observe_chart_surface)
        self.chart_container.set_order_rail_lab_config(self.magnetic_rail_config)
        self.chart_container.set_order_rail_order_preset(
            self.magnetic_rail_order_presets[self.active_magnetic_rail_order_preset],
            self.active_magnetic_rail_order_preset,
        )
        self.chart_surface = ChartSurfaceHost(self.chart_container)
        self.chart_container.interaction_priority_changed.connect(
            self._set_chart_interaction_priority
        )
        self.orderbook = OrderBookWidget(self.orderbook_theme)
        self.orderbook.set_presentation_clock(self.presentation_clock)
        self.orderbook.set_symbol(self.current_symbol)
        self.orderbook.set_aggregation_multiplier(
            self.settings.value("orderbook/aggregation_multiplier_v1", 1, int)
        )
        self.orderbook.aggregation_changed.connect(
            lambda value: self.settings.setValue("orderbook/aggregation_multiplier_v1", int(value))
        )
        stored_orderbook_columns = self.settings.value("orderbook/columns_v1", "", str)
        try:
            restored_orderbook_columns = json.loads(stored_orderbook_columns) if stored_orderbook_columns else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            restored_orderbook_columns = {}
        if not isinstance(restored_orderbook_columns, dict):
            restored_orderbook_columns = {}

        stored_orderbook_presentation = self.settings.value(
            "orderbook/presentation_v2", "", str
        )
        try:
            restored_orderbook_presentation = (
                json.loads(stored_orderbook_presentation)
                if stored_orderbook_presentation else {}
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            restored_orderbook_presentation = {}
        if not isinstance(restored_orderbook_presentation, dict):
            restored_orderbook_presentation = {}

        if restored_orderbook_presentation:
            self.orderbook.restore_presentation_state(
                restored_orderbook_presentation, emit=False
            )
        else:
            # Explicit v1 -> v2 migration. The old primary lane becomes the
            # closest new semantic preset; tape defaults to adaptive LARGE mode.
            legacy_primary = str(restored_orderbook_columns.get("primary", "flow")).lower()
            legacy_memory = bool(restored_orderbook_columns.get("memory", False))
            migrated_preset = (
                "liquidity" if legacy_primary == "memory" or legacy_memory
                else "execution"
            )
            self.orderbook.restore_presentation_state(
                {
                    "preset": migrated_preset,
                    "density": "normal",
                    "values": "quote",
                    "tape_enabled": True,
                    "tape_mode": "LARGE",
                },
                emit=False,
            )
        # Presentation state and overlay preferences are persisted separately.
        # Apply the saved overlay state after the preset so user choices made in
        # the order-book-local Options menu survive application restarts.
        if restored_orderbook_columns:
            self.orderbook.set_column_preferences(
                restored_orderbook_columns,
                emit=False,
            )
        self.orderbook.column_preferences_changed.connect(
            self._persist_orderbook_column_preferences
        )
        self.orderbook.presentation_changed.connect(
            self._persist_orderbook_presentation
        )
        self.trading_workspace = TradingWorkspace(
            self.ui_theme,
            self.trading_gateway,
            symbol_rules=self.symbol_rules,
        )
        self.trading_workspace.position_trade_requested.connect(self._reduce_position_in_trade)
        self.order_panel = self.trading_workspace.ticket
        # Keep alert delivery, history and management available without occupying
        # a right-side panel. All existing alert/protection signal wiring remains.
        self.alerts_dialog = QtWidgets.QDialog(self)
        self.alerts_dialog.setWindowTitle("Alerts")
        self.alerts_dialog.resize(760, 420)
        alert_layout = QtWidgets.QVBoxLayout(self.alerts_dialog)
        alert_layout.setContentsMargins(8, 8, 8, 8)
        self.alerts_panel = AlertsPanel(self.alerts_dialog)
        alert_layout.addWidget(self.alerts_panel)
        self.large_trades = TradesTapeWidget(self.orderbook_theme)
        self.large_trades.set_tape_source(self._order_flow_runtime.tape_source)
        self.large_trades.set_presentation_clock(self.presentation_clock)
        self.large_trades.set_market(
            self.current_symbol,
            tick_size=safe_float(
                self.symbol_rules.get(self.current_symbol, SymbolRules()).tick_size
            ),
        )
        self.large_trades.set_mode(self.settings.value("trades/mode_v1", "LARGE", str), emit=False)
        self.large_trades.mode_changed.connect(
            lambda mode: self.settings.setValue("trades/mode_v1", mode)
        )
        self.large_trades.set_value_mode(self.settings.value("trades/value_mode_v1", "quote", str), emit=False)
        self.large_trades.value_mode_changed.connect(
            lambda mode: self.settings.setValue("trades/value_mode_v1", mode)
        )
        self.watchlist_sidebar = WatchlistSidebarWidget(self.watchlist)
        self.watchlist_sidebar.symbol_selected.connect(self._open_watchlist_symbol)
        self.watchlist_sidebar.toggle_current_requested.connect(
            self._toggle_active_symbol_watchlist
        )
        self.watchlist.icon_missing.connect(self._request_coin_icon)
        self.trading_workspace.set_close_presets(self.quick_trading_preset)

        self._update_toolbar_icons()

        self.main_splitter = PanelSplitter(Qt.Orientation.Horizontal, extended_hit_target=True)
        self.main_splitter.setContentsMargins(0, 0, 0, 0)
        self.main_splitter.setObjectName("mainChartSplitter")

        self.right_rail_controller = RightRailController(
            self.settings,
            [
                PanelSpec("depth", "Market depth", lambda: self.orderbook,
                          300, 110, RIGHT_PANEL_DEFAULT_SIZES["Market depth"],
                          self.orderbook.set_panel_active),
                PanelSpec("trading", "Trading / positions", lambda: self.trading_workspace,
                          300, 240, RIGHT_PANEL_DEFAULT_SIZES["Trading / positions"]),
                PanelSpec("trades", "Large trades", lambda: self.large_trades,
                          300, 130, RIGHT_PANEL_DEFAULT_SIZES["Large trades"],
                          self.large_trades.set_panel_active),
                PanelSpec("watchlist", "Watchlist", lambda: self.watchlist_sidebar,
                          300, 90, RIGHT_PANEL_DEFAULT_SIZES["Watchlist"]),
            ],
            self.right_layout_presets,
            initial_preset=self.right_layout_preset,
            parent=self,
            clock=self.presentation_clock,
        )
        if self._migrate_desk_layout:
            self.right_rail_controller.reset("Balanced", self.right_layout_presets["Balanced"])
            self.settings.setValue("right_rail/desk_layout_generation", 2)
        self.right_rail_host = self.right_rail_controller.rail
        self.panel_sections = self.right_rail_controller.sections
        self.right_layout_preset = self.right_rail_controller.state.active_preset
        self.right_rail_controller.state_changed.connect(self._right_rail_state_changed)
        self.right_rail_controller.composition_changed.connect(self._right_rail_composition_changed)
        self.right_rail_controller.geometry_changed.connect(self._right_rail_geometry_changed)
        self.orderbook.right_rail_height_requested.connect(
            lambda height: self.right_rail_controller.request_panel_height(
                "Market depth", height
            )
        )
        self.trading_workspace.rail_minimum_height_changed.connect(
            self.right_rail_controller.refresh_geometry_constraints
        )
        self.right_rail_controller.refresh_geometry_constraints()
        self.right_rail_controller.operation_rejected.connect(
            lambda message: self.statusBar().showMessage(message, 3500)
        )

        self.chart_container.setMinimumWidth(RIGHT_RAIL_CHART_MIN_WIDTH)
        self.chart_column = QtWidgets.QWidget()
        chart_column_layout = QtWidgets.QVBoxLayout(self.chart_column)
        self._chart_column_layout = chart_column_layout
        chart_column_layout.setContentsMargins(0, 0, 0, 0)
        chart_column_layout.setSpacing(0)
        instrument_host = QtWidgets.QWidget(self.chart_column)
        instrument_host.setObjectName("instrumentBarHost")
        instrument_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        instrument_layout = QtWidgets.QHBoxLayout(instrument_host)
        instrument_layout.setContentsMargins(0, 6, 0, 0)
        instrument_layout.setSpacing(0)
        instrument_layout.addWidget(self.instrument_bar)
        chart_column_layout.addWidget(instrument_host)
        chart_column_layout.addWidget(self.chart_surface, 1)
        self.instrument_bar.set_plot_alignment(self.chart_container, instrument_layout)
        # Build the shells once; Leaders restores its snapshot in a worker and
        # Sectors starts history work only when its workspace is visible.
        self.market_board = LeadershipTimelineWidget(self.ui_theme)
        self.market_board.symbol_selected.connect(self._open_market_from_board)
        self.market_board.tracked_changed.connect(self._sync_ticker_streams)
        self.market_board.restore_ui_state(self.settings)
        self.market_board.set_tickers(self.tickers)

        self.sector_overview = SectorOverviewWidget(self.ui_theme)
        self.sector_overview.symbol_selected.connect(self._open_market_from_board)
        self.sector_overview.restore_ui_state(self.settings)
        self.sector_overview.set_tickers(self.tickers)

        self.rotation_overview = RotationScannerWidget(self.ui_theme)
        self.rotation_overview.bind_leadership(self.market_board)
        self.rotation_overview.symbol_selected.connect(self._open_market_from_board)
        self.rotation_overview.restore_ui_state(self.settings)

        self.workspace_stack = QtWidgets.QStackedWidget()
        self.workspace_stack.setObjectName("workspaceStack")
        self.workspace_stack.addWidget(self.chart_column)
        self.workspace_stack.addWidget(self.market_board)
        self.workspace_stack.addWidget(self.sector_overview)
        self.workspace_stack.addWidget(self.rotation_overview)

        # The global toolbar spans both splitter columns. Chart-only instrument
        # context belongs to chart_column, exactly matching the chart/axis width.
        # Right panels start immediately below the global toolbar.
        self.left_workspace = QtWidgets.QWidget()
        self.left_workspace.setObjectName("leftWorkspace")
        left_layout = QtWidgets.QVBoxLayout(self.left_workspace)
        self._left_workspace_layout = left_layout
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self.workspace_stack, 1)

        self._terminal_status_bar = TerminalStatusBar(self.left_workspace)
        self._terminal_status_bar.venue.setText(
            "Binance Testnet" if self.testnet else "Binance"
        )
        self._terminal_status_bar.setContentsMargins(0, 0, 0, 0)
        self._terminal_status_bar.setFixedHeight(
            DEV_UI_STATUS_GEOMETRY_DEFAULTS["height"]
        )
        self._terminal_status_bar.showMessage("Loading Binance USD-M markets…")
        left_layout.addWidget(self._terminal_status_bar)
        self._fps_status_timer = QTimer(self)
        self._fps_status_timer.setInterval(1000)
        self._fps_status_timer.timeout.connect(self._update_status_fps)
        self._fps_status_timer.start()

        self.main_splitter.addWidget(self.left_workspace)
        self.main_splitter.addWidget(self.right_rail_host)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setStretchFactor(1, 0)
        self.right_rail_controller.attach_main_splitter(
            self.main_splitter,
            self.left_workspace,
            chart_minimum_width=RIGHT_RAIL_CHART_MIN_WIDTH,
        )
        outer.addWidget(self.main_splitter, 1)

        # The legacy menus remain as internal QAction containers so keyboard
        # shortcuts and existing action wiring remain stable. They are never
        # shown; Settings is the sole visible configuration entry point.
        menu_bar = self.menuBar()
        self._legacy_menu_bar = menu_bar
        menu_bar.setNativeMenuBar(False)
        menu_bar.setEnabled(True)
        menu_bar.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            False,
        )
        menu_bar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        view_menu = menu_bar.addMenu("View")

        # The toolbar cycles common layouts. Settings retains individual panel
        # toggles and custom presets through these shared actions.
        self.panel_actions: dict[str, QtGui.QAction] = {}
        self._sync_panel_registry_actions()
        self.right_rail_controller.registry_changed.connect(self._sync_panel_registry_actions)

        self._rebuild_layout_menu()

        self.chart_layout_menu = view_menu.addMenu("Chart layout")
        self.chart_layout_group = QtGui.QActionGroup(self)
        self.chart_layout_group.setExclusive(True)
        self.chart_layout_actions: dict[str, QtGui.QAction] = {}
        chart_layout_labels = {
            "Single": "Single chart",
            "2 Horizontal": "Two charts · side by side",
            "2 Vertical": "Two charts · stacked",
            "4 Grid": "Four charts · grid",
        }
        for layout_name in CHART_LAYOUTS:
            action = self.chart_layout_menu.addAction(chart_layout_labels[layout_name])
            action.setCheckable(True)
            action.setData(layout_name)
            action.triggered.connect(
                lambda _checked=False, value=layout_name: self._set_chart_layout(value)
            )
            self.chart_layout_group.addAction(action)
            self.chart_layout_actions[layout_name] = action

        view_menu.addSeparator()
        self.theme_menu = view_menu.addMenu("Theme")
        self.theme_action_group = QtGui.QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self.theme_actions: dict[str, QtGui.QAction] = {}
        for theme_name in THEMES:
            action = self.theme_menu.addAction(THEME_DISPLAY_NAMES.get(theme_name, theme_name))
            action.setCheckable(True)
            action.setChecked(theme_name == self.theme_name)
            action.triggered.connect(
                lambda checked=False, value=theme_name: self.apply_theme(value) if checked else None
            )
            self.theme_action_group.addAction(action)
            self.theme_actions[theme_name] = action

        view_menu.addSeparator()
        self.candle_style_menu = view_menu.addMenu("Candle style")
        self.candle_style_group = QtGui.QActionGroup(self)
        self.candle_style_group.setExclusive(True)
        self.candle_style_actions: dict[str, QtGui.QAction] = {}
        for style_name in CANDLE_STYLES:
            action = self.candle_style_menu.addAction(style_name)
            action.setCheckable(True)
            action.setChecked(style_name == self.candle_style)
            action.triggered.connect(
                lambda checked=False, value=style_name: (
                    self._set_candle_style(value) if checked else None
                )
            )
            self.candle_style_group.addAction(action)
            self.candle_style_actions[style_name] = action

        view_menu.addSeparator()
        self.indicator_menu = view_menu.addMenu("Indicators")
        self.indicator_menu.setToolTipsVisible(True)
        for action in self.indicator_actions.values():
            self.indicator_menu.addAction(action)
        self.indicator_menu.addSeparator()
        self.indicator_menu.addAction(self.auto_fibonacci_action)
        self.indicator_menu.addSeparator()
        self.indicator_menu.addAction(self.indicator_settings_action)
        self.indicator_menu.addAction(self.indicator_shortcuts_action)

        self.drawing_tools_menu = view_menu.addMenu("Drawing tools")
        self.drawing_tools_menu.setToolTipsVisible(True)
        self.drawing_tools_menu.addAction(self.ruler_action)
        self.drawing_tools_menu.addAction(self.fibonacci_action)
        self.drawing_tools_menu.addAction(self.horizontal_action)
        self.drawing_tools_menu.addSeparator()
        self.drawing_tools_menu.addAction(self.clear_drawings_action)

        self.chart_layers_menu = view_menu.addMenu("Chart layers")
        self.chart_layers_group = QtGui.QActionGroup(self)
        self.chart_layers_group.setExclusive(True)
        self.chart_visibility_actions: dict[int, QtGui.QAction] = {}
        for mode, label in enumerate((
            "Show all",
            "Hide indicators",
            "Hide indicators and drawings",
        )):
            action = self.chart_layers_menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            action.triggered.connect(
                lambda checked=False, value=mode: (
                    self._set_chart_visibility_mode(value) if checked else None
                )
            )
            self.chart_layers_group.addAction(action)
            self.chart_visibility_actions[mode] = action
        self._sync_chart_visibility_actions()

        view_menu.addSeparator()
        self.scale_menu = view_menu.addMenu("Price scale")
        self.auto_scale_action = self.scale_menu.addAction("Automatic fit")
        self.auto_scale_action.setCheckable(True)
        self.auto_scale_action.setChecked(self.auto_scale)
        self.auto_scale_action.toggled.connect(self._set_auto_scale)
        self.logarithmic_action = self.scale_menu.addAction("Logarithmic scale")
        self.logarithmic_action.setCheckable(True)
        self.logarithmic_action.setChecked(self.logarithmic)
        self.logarithmic_action.toggled.connect(self._set_logarithmic)
        self.scale_menu.addSeparator()
        self.fit_chart_action = self.scale_menu.addAction("Fit chart now", self._fit_chart)
        self.fit_chart_action.setShortcut(QtGui.QKeySequence("Alt+R"))
        self.fit_chart_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(self.fit_chart_action)
        self.fullscreen_action = view_menu.addAction("Full screen")
        self.fullscreen_action.setCheckable(True)
        # The bootstrap-level application event filter owns the physical F11 key.
        # Keep this QAction as the single fullscreen state authority only; assigning
        # another Qt shortcut here would recreate the competing F11 paths that made
        # Windows dispatch unreliable across focused child/native widgets.
        self.fullscreen_action.setShortcut(QtGui.QKeySequence())
        self.addAction(self.fullscreen_action)
        self.fullscreen_action.toggled.connect(self._toggle_fullscreen)
        data_menu = self.menuBar().addMenu("Data")
        self.data_menu = data_menu
        self.download_history_action = data_menu.addAction(
            "Download historical candles…",
            self.download_history,
        )
        self.download_market_history_action = data_menu.addAction(
            "Download market history data...",
            self.download_market_history,
        )
        self.load_history_action = data_menu.addAction(
            "Load historical CSV…",
            self.load_history_csv,
        )
        self.cancel_history_action = data_menu.addAction(
            "Cancel history download",
            self._cancel_history_download,
        )
        self.cancel_history_action.setEnabled(False)
        data_menu.addSeparator()
        data_menu.addAction(
            "Open research database…",
            lambda: DataCacheDialog(self._ensure_app_database(), self).exec(),
        )
        alert_menu = self.menuBar().addMenu("Alerts")
        self.alert_menu = alert_menu
        alert_menu.addAction("Open alerts", self._show_alerts_panel)
        alert_menu.addAction("Add price alert", self.add_price_alert)
        alert_menu.addAction("Delivery settings", self.edit_alert_settings)
        trading_menu = self.menuBar().addMenu("Trading")
        self.trading_menu = trading_menu
        trading_menu.addAction("Open trading panel", self._show_trading_sidebar)
        trading_menu.addAction("API credentials", self.order_panel.edit_credentials)
        trading_menu.addAction("Test API connection", self.test_trading_connection)
        trading_menu.addAction(
            "Reconcile unknown order outcomes",
            self.trading_gateway.reconcile_unknown_orders,
        )
        trading_menu.addAction(
            "Refresh account",
            lambda: self.trading_gateway.refresh_account(self.current_symbol, True),
        )
        trading_menu.addAction("Cancel all current-symbol orders", lambda: self.trading_gateway.cancel_all(self.current_symbol))
        trading_menu.addSeparator()
        trading_menu.addAction("Quick trading and shortcuts…", self.edit_quick_trading_settings)
        trading_menu.addAction("Magnetic rail presets…", self.edit_magnetic_rail_order_presets)
        trading_menu.addAction("Active order rail settings…", lambda: edit_active_rail_settings(self))
        trading_menu.addSeparator()
        self.trading_mode_action = trading_menu.addAction(
            "Testnet mode" if self.testnet else "Live Binance mode"
        )
        self.trading_mode_action.setEnabled(False)

        # Developer tooling is surfaced only through Settings → Developer.
        # Keep the testing QActions as the authoritative state/shortcut objects,
        # but do not expose a second top-level navigation authority.
        self.developer_test_actions: dict[str, QtGui.QAction] = {}
        for flag_name, label, tooltip in TESTING_ENTRIES:
            action = QtGui.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(bool(self.testing_flags.get(flag_name)))
            action.setToolTip(tooltip)
            action.toggled.connect(
                lambda checked, name=flag_name: self._set_testing_flag(name, checked)
            )
            self.developer_test_actions[flag_name] = action
        full_view_action = self.developer_test_actions.get("chart_opengl_full_viewport")
        if full_view_action is not None:
            full_view_action.setEnabled(bool(self.testing_flags.get("chart_opengl")))

        help_menu = self.menuBar().addMenu("Help")
        self.help_menu = help_menu
        help_menu.addAction("Hotkeys", self._show_hotkeys_reference)

        menu_bar.setVisible(False)
        menu_bar.setFixedHeight(0)

    def _adjust_orderbook_aggregation(self, step: int) -> None:
        if not hasattr(self, "orderbook"):
            return
        values = tuple(int(value) for value in ORDER_FLOW_AGGREGATION_MULTIPLIERS)
        current = int(self.orderbook.aggregation_multiplier())
        try:
            index = values.index(current)
        except ValueError:
            index = 0
        target = values[max(0, min(len(values) - 1, index + int(step)))]
        if target == current:
            return
        self.orderbook.set_aggregation_multiplier(target, emit=True)
        self.statusBar().showMessage(f"ORDER BOOK AGGREGATION · {target}×", 1500)

    def _persist_orderbook_column_preferences(self, preferences: object) -> None:
        values = preferences if isinstance(preferences, dict) else {}
        normalized: dict[str, object] = {
            key: bool(values.get(key, True))
            for key in ("state", "memory", "delta", "flow")
        }
        primary = str(values.get("primary", "flow")).lower()
        normalized["primary"] = primary if primary in {"flow", "delta", "memory"} else "flow"
        self.settings.setValue(
            "orderbook/columns_v1",
            json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        )
        self._sync_settings_window()

    def _persist_orderbook_presentation(self, state: object) -> None:
        values = state if isinstance(state, dict) else {}
        normalized = {
            "preset": str(values.get("preset", "execution")),
            "density": str(values.get("density", "normal")),
            "values": "base" if str(values.get("values", "quote")).lower() == "base" else "quote",
            "tape_enabled": bool(values.get("tape_enabled", True)),
            "tape_mode": "ALL" if str(values.get("tape_mode", "LARGE")).upper() == "ALL" else "LARGE",
        }
        width_state: dict[str, dict[str, float]] = {}
        raw_widths = values.get("column_widths", {})
        if isinstance(raw_widths, dict):
            allowed_columns = {"state", "memory", "delta", "bid", "price", "ask", "flow"}
            for preset in ("execution", "liquidity", "footprint"):
                raw_preset = raw_widths.get(preset)
                if not isinstance(raw_preset, dict):
                    continue
                cleaned: dict[str, float] = {}
                for name, raw_width in raw_preset.items():
                    if str(name) not in allowed_columns:
                        continue
                    try:
                        width = float(raw_width)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(width) and 12.0 <= width <= 2000.0:
                        cleaned[str(name)] = round(width, 2)
                if cleaned:
                    width_state[preset] = cleaned
        normalized["column_widths"] = width_state
        if normalized["preset"] not in {"execution", "liquidity", "footprint"}:
            normalized["preset"] = "execution"
        if normalized["density"] not in {"compact", "normal", "relaxed"}:
            normalized["density"] = "normal"
        self.settings.setValue(
            "orderbook/presentation_v2",
            json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        )
        self._sync_settings_window()

    def set_volume_bar_height_percent(self, value: int) -> None:
        value = max(5, min(45, int(value)))
        if value == int(getattr(self, "volume_bar_height_percent", 30)):
            return
        self.volume_bar_height_percent = value
        container = getattr(self, "chart_container", None)
        if container is not None:
            container.set_volume_bar_height_percent(value)
        else:
            chart = getattr(self, "chart", None)
            if chart is not None:
                chart.set_volume_bar_height_percent(value)
        self.settings.setValue("chart/volume_bar_height_pct_v1", value)

    def volume_bar_height_setting(self) -> int:
        chart = getattr(self, "chart", None)
        if chart is not None:
            return chart.volume_bar_height_setting()
        return int(getattr(self, "volume_bar_height_percent", 30))

    def _show_settings_window(self, category: str | None = None) -> None:
        dialog = self.settings_dialog
        if dialog is None:
            dialog = NightwatchSettingsDialog(self)
            self.settings_dialog = dialog
        was_visible = dialog.isVisible()
        category_aliases = {
            "Chart": "Chart & Indicators",
            "Indicators": "Chart & Indicators",
            "Panels & Layout": "Workspace",
            "Execution": "Trading",
            "Alerts": "Data & Alerts",
            "Data": "Data & Alerts",
            "Developer": "Advanced",
            "Help": "Advanced",
        }
        target = category_aliases.get(str(category), category)
        if target in dialog.CATEGORIES:
            dialog.select_category(target)
        if was_visible:
            dialog.sync_from_owner()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _sync_settings_window(self) -> None:
        dialog = getattr(self, "settings_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.sync_from_owner()

    @staticmethod
    def _default_indicators() -> dict[str, bool]:
        return {name: False for name in INDICATOR_KEYS}

    def _set_chart_layout(self, mode: str, *, persist: bool = True) -> None:
        if mode not in CHART_LAYOUTS:
            return
        if not self.testing_flags.get("multiple_chart_layouts", True) and mode != "Single":
            return
        if persist:
            self.chart_layout_mode = mode
            self.settings.setValue("chart/layout_mode_v1", mode)
        self.chart_container.set_layout_mode(mode)
        self._apply_auxiliary_symbol_rules()
        for name, action in self.chart_layout_actions.items():
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(name == mode)
            del blocker
        self.statusBar().showMessage(f"CHART LAYOUT · {mode.upper()}", 1800)
        self._sync_settings_window()

    def _apply_auxiliary_symbol_rules(self) -> None:
        for pane in self.chart_container.auxiliary:
            pane.chart.set_symbol_rules(
                self.symbol_rules.get(pane.symbol, SymbolRules())
            )

    def _apply_chart_layout_state(self) -> None:
        enabled = self.testing_flags.get("multiple_chart_layouts", True)
        self.chart_layout_menu.setEnabled(enabled)
        effective = self.chart_layout_mode if enabled else "Single"
        self._set_chart_layout(effective, persist=False)

    def _set_testing_flag(self, name: str, enabled: bool) -> None:
        if name not in self.testing_flags:
            return
        self.testing_flags[name] = bool(enabled)
        if name == "chart_opengl":
            setting_key = "testing/chart_opengl_v2"
        elif name == "chart_opengl_full_viewport":
            setting_key = "testing/chart_opengl_full_viewport_v5"
        else:
            setting_key = f"testing/{name}_v1"
        self.settings.setValue(setting_key, bool(enabled))
        self.settings.sync()
        restart_required = False
        if name in {"chart_opengl", "chart_opengl_full_viewport"}:
            # The viewport backend is selected during GraphicsView construction.
            restart_required = True
        elif name == "chart_native_bar_renderer":
            self.chart_container.set_native_bar_renderer_enabled(enabled)
        elif name == "chart_lod_aggregation":
            self.chart_container.set_lod_aggregation_enabled(enabled)
        elif name == "magnetic_order_rail":
            self.chart_container.set_magnetic_order_rail_enabled(enabled)
            self.chart.set_order_rail_lab_config(self.magnetic_rail_config)
            if hasattr(self, "horizontal_action"):
                blocker = QtCore.QSignalBlocker(self.horizontal_action)
                if not enabled:
                    self.horizontal_action.setChecked(False)
                self.horizontal_action.setVisible(enabled)
                del blocker
        elif name == "multiple_chart_layouts":
            self._apply_chart_layout_state()
        actions = getattr(self, "developer_test_actions", {})
        action = actions.get(name)
        if action is not None:
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(bool(self.testing_flags.get(name)))
            del blocker
        full_action = actions.get("chart_opengl_full_viewport")
        if full_action is not None:
            blocker = QtCore.QSignalBlocker(full_action)
            full_action.setChecked(bool(self.testing_flags.get("chart_opengl_full_viewport")))
            full_action.setEnabled(bool(self.testing_flags.get("chart_opengl")))
            del blocker
        suffix = " · RESTART TO APPLY" if restart_required else ""
        self.statusBar().showMessage(
            f"TESTING · {name.replace('_', ' ').upper()} · "
            f"{'ON' if enabled else 'OFF'}{suffix}",
            3200 if restart_required else 2200,
        )
        if self.developer_dialog is not None:
            self.developer_dialog.sync_testing_state()
        self._sync_settings_window()

    def _load_magnetic_rail_order_presets(self) -> tuple[dict[str, dict[str, Any]], str]:
        stored=self.settings.value("trading/magnetic_rail_presets_v1","",str); presets={}
        if stored:
            try: decoded=json.loads(stored)
            except (TypeError,json.JSONDecodeError): decoded={}
            if isinstance(decoded,dict):
                for name,value in decoded.items():
                    if isinstance(value,dict) and str(name).strip(): presets[str(name).strip()]=normalized_order_rail_order_preset(value)
        if not presets: presets={k:normalized_order_rail_order_preset(v) for k,v in ORDER_RAIL_ORDER_PRESET_DEFAULTS.items()}
        active=self.settings.value("trading/magnetic_rail_active_preset_v1","Limit Entry",str); active=active if active in presets else next(iter(presets)); return presets,active

    def _apply_active_magnetic_rail_order_preset(self) -> None:
        if not hasattr(self,"magnetic_rail_order_presets"): return
        name=self.active_magnetic_rail_order_preset
        if name not in self.magnetic_rail_order_presets: name=next(iter(self.magnetic_rail_order_presets)); self.active_magnetic_rail_order_preset=name
        preset=self.magnetic_rail_order_presets[name]; chart=getattr(self,"chart",None)
        if chart is not None: chart.set_order_rail_order_preset(preset,name)
        container=getattr(self,"chart_container",None)
        if container is not None:
            container.set_order_rail_order_preset(preset, name)

    def edit_magnetic_rail_order_presets(self) -> None:
        dialog=MagneticRailPresetsDialog(self.magnetic_rail_order_presets,self.active_magnetic_rail_order_preset,self)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted: return
        presets,active=dialog.values(); self.magnetic_rail_order_presets=presets; self.active_magnetic_rail_order_preset=active
        self.settings.setValue("trading/magnetic_rail_presets_v1",json.dumps(presets,sort_keys=True)); self.settings.setValue("trading/magnetic_rail_active_preset_v1",active); self.settings.sync(); self._apply_active_magnetic_rail_order_preset(); self.statusBar().showMessage(f"RAIL PRESET · {active} · CTRL+LEFT BUY / CTRL+RIGHT SELL",4500)

    def _load_magnetic_rail_config(self) -> dict[str, Any]:
        stored = self.settings.value("developer/magnetic_order_rail_v2", "", str)
        if not stored:
            return normalized_order_rail_config()
        try:
            decoded = json.loads(stored)
        except (TypeError, json.JSONDecodeError):
            decoded = {}
        return normalized_order_rail_config(
            decoded if isinstance(decoded, dict) else {}
        )

    def set_magnetic_rail_config(self, config: dict[str, Any]) -> None:
        normalized = normalized_order_rail_config(config)
        if normalized == self.magnetic_rail_config:
            return
        self.magnetic_rail_config = normalized
        self.settings.setValue(
            "developer/magnetic_order_rail_v2",
            json.dumps(
                {key: self.magnetic_rail_config[key] for key in ORDER_RAIL_USER_KEYS},
                separators=(",", ":"),
            ),
        )
        # Do not settings.sync() here: studio controls can emit many changes per
        # second and synchronous disk flushes would create avoidable UI stalls.
        chart = getattr(self, "chart", None)
        if chart is not None:
            chart.set_order_rail_lab_config(self.magnetic_rail_config)
        container = getattr(self, "chart_container", None)
        if container is not None:
            container.set_order_rail_lab_config(self.magnetic_rail_config)


    def developer_ui_profile_snapshot(self) -> dict[str, Any]:
        """Return the complete effective visual profile in portable JSON form."""
        controller = typography_controller()
        return {
            "schema": "nightwatch-ui-profile",
            "version": 1,
            "base_theme": self.theme_name,
            "directional_modes": dict(self.directional_color_modes),
            # Resolved colors are informational. Only explicit overrides are
            # imported back into the dependency-aware color resolver.
            "colors": {
                key: self.ui_theme.get(key, self.theme.get(key, ""))
                for key, _label in DEV_UI_COLOR_PROFILE_FIELDS
            },
            "color_overrides": dict(self.developer_ui_colors),
            "geometry": {
                key: int(self.developer_ui_layout.get(key, default))
                for key, default in DEV_UI_LAYOUT_DEFAULTS.items()
            },
            "surfaces": {
                key: int(self.developer_ui_surfaces.get(key, default))
                for key, default in DEV_UI_SURFACE_DEFAULTS.items()
            },
            "status_bar": dict(self.developer_ui_status),
            "typography": {
                role: dict(controller.profile(role))
                for role in TYPOGRAPHY_DEFAULTS
            },
            "typography_globals": dict(controller.globals()),
            "dpi_rounding": self.settings.value(
                "developer/typography_dpi_rounding_v1", "auto", str
            ),
            "magnetic_rail": {key: self.magnetic_rail_config[key] for key in ORDER_RAIL_USER_KEYS},
            "magnetic_rail_trading": {"activePreset": self.active_magnetic_rail_order_preset, "presets": self.magnetic_rail_order_presets},
        }

    def export_developer_ui_profile(self) -> None:
        """Save the current visual tuning as a portable profile."""
        default_name = "nightwatch-ui-profile.json"
        path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Nightwatch UI profile",
            default_name,
            "Nightwatch UI profile (*.json);;JSON files (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    self.developer_ui_profile_snapshot(),
                    handle,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "UI profile export",
                f"Could not export profile:\n{exc}",
            )
            return
        self.statusBar().showMessage(
            f"UI PROFILE EXPORTED · {os.path.basename(path)}",
            3000,
        )

    def import_developer_ui_profile(self) -> bool:
        """Load and apply a profile produced by export_developer_ui_profile()."""
        path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Import Nightwatch UI profile",
            "",
            "Nightwatch UI profile (*.json);;JSON files (*.json)",
        )
        if not path:
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                profile = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "UI profile import",
                f"Could not import profile:\n{exc}",
            )
            return False
        if (
            not isinstance(profile, dict)
            or profile.get("schema") != "nightwatch-ui-profile"
            or profile.get("version") != 1
        ):
            QtWidgets.QMessageBox.warning(
                self,
                "UI profile import",
                "This file is not a supported Nightwatch UI profile.",
            )
            return False

        # Old UI-color snapshots are intentionally not imported. They contained
        # fully resolved children and would pin semantic dependencies. Current
        # profiles persist only genuine explicit color overrides.
        colors = profile.get("color_overrides")
        imported_colors: dict[str, str] = {}
        if isinstance(colors, dict):
            allowed_colors = {key for key, _label in DEV_UI_COLOR_PROFILE_FIELDS}
            for key, value in colors.items():
                color = QtGui.QColor(str(value))
                if key in allowed_colors and color.isValid():
                    imported_colors[key] = color.name(
                        QtGui.QColor.NameFormat.HexRgb
                    ).upper()
        self.developer_ui_colors = imported_colors
        self.settings.remove("developer/ui_colors_v1")
        if imported_colors:
            self.settings.setValue(
                "developer/ui_color_overrides_v2",
                json.dumps(imported_colors, separators=(",", ":")),
            )
        else:
            self.settings.remove("developer/ui_color_overrides_v2")

        # Geometry/surface density remains a build-time contract, but typography
        # is a live semantic contract owned by TypographyController.  Older v1
        # profiles that omit typography simply preserve the current typography.
        status_bar = profile.get("status_bar")
        if isinstance(status_bar, dict):
            status_values = {
                key: status_bar.get(key)
                for key, _label in DEV_UI_STATUS_COLOR_FIELDS
                if key in status_bar
            }
            for key in DEV_UI_STATUS_FONT_DEFAULTS:
                if key in status_bar:
                    status_values[key] = status_bar.get(key)
            if status_values:
                self.developer_ui_status = self._normalize_developer_ui_status(status_values)
                self._developer_ui_status_custom = True
                self.settings.setValue(
                    "developer/ui_status_v1",
                    json.dumps(self.developer_ui_status, separators=(",", ":")),
                )

        self.developer_ui_layout = dict(DEV_UI_LAYOUT_DEFAULTS)
        self.developer_ui_surfaces = dict(DEV_UI_SURFACE_DEFAULTS)
        self.settings.remove("developer/ui_layout_v1")
        self.settings.remove("developer/ui_surfaces_v1")

        imported_typography = profile.get("typography")
        if isinstance(imported_typography, dict):
            merged_profiles = {
                role: typography_controller().profile(role)
                for role in TYPOGRAPHY_DEFAULTS
            }
            for role, values in imported_typography.items():
                if role not in TYPOGRAPHY_DEFAULTS or not isinstance(values, dict):
                    continue
                allowed = TYPOGRAPHY_DEFAULTS[role]
                merged_profiles[role].update(
                    {key: value for key, value in values.items() if key in allowed}
                )
            self.developer_typography = merged_profiles
        imported_globals = profile.get("typography_globals")
        if isinstance(imported_globals, dict):
            self.developer_typography_globals = {
                **self.developer_typography_globals,
                **{
                    key: value
                    for key, value in imported_globals.items()
                    if key in TYPOGRAPHY_GLOBAL_DEFAULTS
                },
            }
        if "dpi_rounding" in profile:
            self.developer_typography_globals["dpi_rounding"] = profile.get("dpi_rounding")

        configure_typography(
            self.developer_typography,
            self.developer_typography_globals,
        )
        self._save_developer_typography()

        imported_directional_modes = profile.get("directional_modes")
        if isinstance(imported_directional_modes, dict):
            self.directional_color_modes = self._normalize_directional_color_modes(
                imported_directional_modes
            )
            self._save_directional_color_modes()

        imported_theme = str(profile.get("base_theme") or self.theme_name)
        if imported_theme not in THEMES:
            imported_theme = DEFAULT_THEME_NAME
        self.settings.setValue("theme", imported_theme)
        self.apply_theme(imported_theme)
        self._apply_developer_ui_layout()
        self._apply_developer_ui_status()

        magnetic_rail = profile.get("magnetic_rail")
        if isinstance(magnetic_rail, dict):
            self.set_magnetic_rail_config(magnetic_rail)

        rail_trading = profile.get("magnetic_rail_trading")
        if isinstance(rail_trading, dict):
            raw_presets = rail_trading.get("presets")
            imported_presets: dict[str, dict[str, Any]] = {}
            if isinstance(raw_presets, dict):
                for name, values in raw_presets.items():
                    preset_name = str(name).strip()
                    if preset_name and isinstance(values, dict):
                        imported_presets[preset_name] = normalized_order_rail_order_preset(values)
            if imported_presets:
                active = str(rail_trading.get("activePreset") or "")
                if active not in imported_presets:
                    active = next(iter(imported_presets))
                self.magnetic_rail_order_presets = imported_presets
                self.active_magnetic_rail_order_preset = active
                self.settings.setValue(
                    "trading/magnetic_rail_presets_v1",
                    json.dumps(imported_presets, sort_keys=True),
                )
                self.settings.setValue(
                    "trading/magnetic_rail_active_preset_v1", active
                )
                self._apply_active_magnetic_rail_order_preset()

        self.settings.sync()
        if self.ui_tuner_dialog is not None:
            self.ui_tuner_dialog.sync_from_owner()
        if self.magnetic_rail_lab_dialog is not None:
            self.magnetic_rail_lab_dialog.sync_from_owner()
        self.statusBar().showMessage(
            f"UI PROFILE IMPORTED · {os.path.basename(path)}",
            4500,
        )
        return True


    def _effective_ui_color_overrides(self) -> dict[str, str]:
        """Return explicit Developer overrides for the canonical base theme.

        User-facing candle/order-book direction is now selected semantically as
        Theme colors or Classic green/red and is applied only to those surfaces.
        Legacy Appearance RGB values are intentionally not composed here.
        """
        return dict(self.developer_ui_colors)

    @staticmethod
    def _normalize_directional_color_modes(source: object) -> dict[str, str]:
        valid = {"theme", "classic"}
        result = dict(DIRECTIONAL_COLOR_MODE_DEFAULTS)
        if isinstance(source, dict):
            for surface in result:
                value = str(source.get(surface, result[surface])).casefold()
                if value in valid:
                    result[surface] = value
        return result

    def _load_directional_color_modes(self) -> dict[str, str]:
        values = {
            "candles": self.settings.value(
                "appearance/candle_directional_mode_v1",
                DIRECTIONAL_COLOR_MODE_DEFAULTS["candles"],
                str,
            ),
            "orderbook": self.settings.value(
                "appearance/orderbook_directional_mode_v1",
                DIRECTIONAL_COLOR_MODE_DEFAULTS["orderbook"],
                str,
            ),
        }
        return self._normalize_directional_color_modes(values)

    def _save_directional_color_modes(self) -> None:
        keys = {
            "candles": "appearance/candle_directional_mode_v1",
            "orderbook": "appearance/orderbook_directional_mode_v1",
        }
        for surface, default in DIRECTIONAL_COLOR_MODE_DEFAULTS.items():
            self.settings.setValue(
                keys[surface], self.directional_color_modes.get(surface, default)
            )

    def directional_color_mode(self, surface: str) -> str:
        key = str(surface).casefold()
        if key not in DIRECTIONAL_COLOR_MODE_DEFAULTS:
            raise KeyError(surface)
        return str(self.directional_color_modes.get(key, DIRECTIONAL_COLOR_MODE_DEFAULTS[key]))

    def set_directional_color_mode(self, surface: str, mode: str) -> None:
        key = str(surface).casefold()
        if key not in DIRECTIONAL_COLOR_MODE_DEFAULTS:
            return
        normalized = str(mode).casefold()
        if normalized not in {"theme", "classic"}:
            normalized = DIRECTIONAL_COLOR_MODE_DEFAULTS[key]
        if self.directional_color_modes.get(key) == normalized:
            return
        self.directional_color_modes[key] = normalized
        self._save_directional_color_modes()
        self._refresh_developer_ui_colors()
        self._sync_settings_window()

    def _refresh_directional_surface_palettes(self) -> None:
        chart_base = chart_palette(self.ui_theme)
        self.chart_theme = candle_directional_palette(
            chart_base, self.directional_color_modes.get("candles", "theme")
        )
        self.orderbook_theme = orderbook_directional_palette(
            self.ui_theme, self.directional_color_modes.get("orderbook", "theme")
        )


    def _load_developer_ui_colors(self) -> dict[str, str]:
        self.settings.remove("developer/ui_colors_v1")
        stored = self.settings.value("developer/ui_color_overrides_v2", "", str)
        if not stored:
            return {}
        try:
            decoded = json.loads(stored)
        except (TypeError, json.JSONDecodeError):
            return {}
        allowed = {key for key, _label in DEV_UI_COLOR_PROFILE_FIELDS}
        result: dict[str, str] = {}
        if isinstance(decoded, dict):
            for key, value in decoded.items():
                if key in allowed and QtGui.QColor(str(value)).isValid():
                    result[key] = QtGui.QColor(str(value)).name(QtGui.QColor.NameFormat.HexRgb).upper()
        return result

    def _load_developer_ui_layout(self) -> dict[str, int]:
        # Geometry is a build-time contract. Discard historical UI-Tuner state.
        self.settings.remove("developer/ui_layout_v1")
        self.settings.remove("developer/edge_geometry_v3")
        return dict(DEV_UI_LAYOUT_DEFAULTS)

    def _load_developer_ui_surfaces(self) -> dict[str, int]:
        self.settings.remove("developer/ui_surfaces_v1")
        return dict(DEV_UI_SURFACE_DEFAULTS)

    def _load_developer_typography(self) -> dict[str, dict[str, Any]]:
        stored = self.settings.value("developer/typography_v1", "", str)
        if not stored:
            return {}
        try:
            decoded = json.loads(stored)
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for role, values in decoded.items():
            if role not in TYPOGRAPHY_DEFAULTS or not isinstance(values, dict):
                continue
            allowed = TYPOGRAPHY_DEFAULTS[role]
            result[role] = {
                key: value for key, value in values.items() if key in allowed
            }
        return result

    def _default_developer_ui_status(self) -> dict[str, object]:
        t = self.ui_theme
        return {
            "background": t["panel"],
            "border": t.get("separator", t["border"]),
            "text": t["text"],
            "message": t["text"],
            "connection": t["muted"],
            "connection_live": t["green"],
            "latency_live": t["text"],
            "latency_stale": t.get("amber", t["muted"]),
            **DEV_UI_STATUS_GEOMETRY_DEFAULTS,
            **DEV_UI_STATUS_FONT_DEFAULTS,
        }

    def _normalize_developer_ui_status(self, source: object) -> dict[str, object]:
        result = self._default_developer_ui_status()
        values = source if isinstance(source, dict) else {}
        for key, _label in DEV_UI_STATUS_COLOR_FIELDS:
            color = QtGui.QColor(str(values.get(key, "")))
            if color.isValid():
                result[key] = color.name(QtGui.QColor.NameFormat.HexRgb).upper()
        # Geometry remains a build-time contract. Font roles are semantic
        # typography choices and may be tuned independently.
        result.update(DEV_UI_STATUS_GEOMETRY_DEFAULTS)
        for key, default in DEV_UI_STATUS_FONT_DEFAULTS.items():
            role = str(values.get(key, default))
            result[key] = role if role in TYPOGRAPHY_DEFAULTS else default
        return result

    def _load_developer_ui_status(self) -> dict[str, object]:
        stored = self.settings.value("developer/ui_status_v1", "", str)
        if not stored:
            self._developer_ui_status_custom = False
            return self._default_developer_ui_status()
        try:
            decoded = json.loads(stored)
        except (TypeError, json.JSONDecodeError):
            decoded = {}
        stored_values: dict[str, object] = {}
        if isinstance(decoded, dict):
            allowed = {key for key, _label in DEV_UI_STATUS_COLOR_FIELDS}
            allowed.update(DEV_UI_STATUS_FONT_DEFAULTS)
            stored_values = {
                key: value for key, value in decoded.items() if key in allowed
            }
        normalized = self._normalize_developer_ui_status(stored_values)
        self._developer_ui_status_custom = bool(stored_values)
        self.settings.setValue(
            "developer/ui_status_v1",
            json.dumps(normalized, separators=(",", ":")),
        )
        return normalized

    def _load_developer_typography_globals(self) -> dict[str, Any]:
        result = dict(TYPOGRAPHY_GLOBAL_DEFAULTS)
        stored = self.settings.value("developer/typography_global_v1", "", str)
        if stored:
            try:
                decoded = json.loads(stored)
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            if isinstance(decoded, dict):
                for key, value in decoded.items():
                    if key in result:
                        result[key] = value
        # Bootstrap owns DPI application before QApplication. Mirror the saved
        # restart-only choice into the controller/profile state for one coherent
        # configuration surface.
        result["dpi_rounding"] = self.settings.value(
            "developer/typography_dpi_rounding_v1",
            result.get("dpi_rounding", "auto"),
            str,
        )
        return result

    def _save_developer_typography(self) -> None:
        controller = typography_controller()
        overrides: dict[str, dict[str, Any]] = {}
        for role, defaults in TYPOGRAPHY_DEFAULTS.items():
            profile = controller.profile(role)
            changed = {
                key: value
                for key, value in profile.items()
                if key in defaults and value != defaults[key]
            }
            if changed:
                overrides[role] = changed
        self.developer_typography = overrides
        if overrides:
            self.settings.setValue(
                "developer/typography_v1",
                json.dumps(overrides, separators=(",", ":"), sort_keys=True),
            )
        else:
            self.settings.remove("developer/typography_v1")

        globals_ = controller.globals()
        self.developer_typography_globals = dict(globals_)
        global_changes = {
            key: value
            for key, value in globals_.items()
            if key in TYPOGRAPHY_GLOBAL_DEFAULTS
            and value != TYPOGRAPHY_GLOBAL_DEFAULTS[key]
        }
        if global_changes:
            self.settings.setValue(
                "developer/typography_global_v1",
                json.dumps(global_changes, separators=(",", ":"), sort_keys=True),
            )
        else:
            self.settings.remove("developer/typography_global_v1")

        dpi_rounding = str(globals_.get("dpi_rounding", "auto"))
        if dpi_rounding == "auto":
            self.settings.remove("developer/typography_dpi_rounding_v1")
        else:
            self.settings.setValue(
                "developer/typography_dpi_rounding_v1", dpi_rounding
            )

    def set_developer_typography_values(self, role: str, updates: dict[str, object]) -> None:
        if role not in TYPOGRAPHY_DEFAULTS or not isinstance(updates, dict):
            return
        overrides = {
            name: dict(values)
            for name, values in self.developer_typography.items()
            if name in TYPOGRAPHY_DEFAULTS and isinstance(values, dict)
        }
        role_values = dict(overrides.get(role, {}))
        allowed = TYPOGRAPHY_DEFAULTS[role]
        role_values.update({key: value for key, value in updates.items() if key in allowed})
        overrides[role] = role_values
        configure_typography(overrides, self.developer_typography_globals)
        self._save_developer_typography()

    def set_developer_typography_value(self, role: str, key: str, value: object) -> None:
        self.set_developer_typography_values(role, {key: value})

    def set_developer_typography_global(self, key: str, value: object) -> None:
        if key not in TYPOGRAPHY_GLOBAL_DEFAULTS:
            return
        globals_ = dict(self.developer_typography_globals)
        globals_[key] = value
        configure_typography(self.developer_typography, globals_)
        self._save_developer_typography()

    def reset_developer_typography_role(self, role: str) -> None:
        if role not in TYPOGRAPHY_DEFAULTS:
            return
        overrides = {
            name: dict(values)
            for name, values in self.developer_typography.items()
            if name != role and name in TYPOGRAPHY_DEFAULTS and isinstance(values, dict)
        }
        configure_typography(overrides, self.developer_typography_globals)
        self._save_developer_typography()

    def reset_developer_typography(self) -> None:
        configure_typography({}, TYPOGRAPHY_GLOBAL_DEFAULTS)
        self._save_developer_typography()

    def _refresh_developer_ui_colors(self) -> None:
        self.ui_theme = ui_palette(self.theme, self._effective_ui_color_overrides())
        self._refresh_directional_surface_palettes()
        if (
            hasattr(self, "developer_ui_status")
            and not bool(getattr(self, "_developer_ui_status_custom", False))
        ):
            # Keep the status bar coherent with the selected shell palette unless
            # the user explicitly customized the status-bar contract.
            self.developer_ui_status = self._default_developer_ui_status()
        self._apply_stylesheet()
        self.chart.apply_theme(self.chart_theme)
        self.chart_container.apply_theme(self.chart_theme)
        self.stats.apply_theme(self.ui_theme)
        self.watchlist.apply_theme(self.ui_theme)
        self.watchlist_sidebar.apply_theme(self.ui_theme)
        self.orderbook.apply_theme(self.orderbook_theme)
        if self.market_board is not None:
            self.market_board.apply_theme(self.ui_theme)
        self.rotation_overview.apply_theme(self.ui_theme)
        if self.sector_overview is not None:
            self.sector_overview.apply_theme(self.ui_theme)
        self.trading_workspace.apply_theme(self.ui_theme)
        if self.symbol_search_dialog is not None:
            self.symbol_search_dialog.apply_theme(self.ui_theme)
        self._sync_top_metrics()
        self._update_toolbar_icons()

    def set_developer_ui_color(self, key: str, value: str) -> None:
        allowed = {name for name, _label in DEV_UI_COLOR_PROFILE_FIELDS}
        color = QtGui.QColor(str(value))
        if key not in allowed or not color.isValid():
            return
        self.developer_ui_colors[key] = color.name(QtGui.QColor.NameFormat.HexRgb).upper()
        self.settings.setValue(
            "developer/ui_color_overrides_v2",
            json.dumps(self.developer_ui_colors, separators=(",", ":")),
        )
        self._refresh_developer_ui_colors()

    def apply_developer_ui_color_preset(self, name: str) -> None:
        """Apply one complete shell palette with a single refresh."""
        name = str(name)
        if name == "Theme default":
            self.reset_developer_ui_colors()
            return
        # Decorative presets leave advanced market/depth overrides intact.

    def current_developer_ui_color_preset(self) -> str:
        """Return the named palette matching the effective shell colors."""
        if not self.developer_ui_colors:
            return "Theme default"
        return "Custom"

    def reset_developer_ui_colors(self) -> None:
        self.developer_ui_colors.clear()
        self.settings.remove("developer/ui_color_overrides_v2")
        self.settings.remove("developer/ui_colors_v1")
        self._refresh_developer_ui_colors()

    def set_developer_ui_status_values(self, updates: dict[str, object]) -> None:
        allowed = {key for key, _label in DEV_UI_STATUS_COLOR_FIELDS}
        allowed.update(DEV_UI_STATUS_FONT_DEFAULTS)
        filtered = {key: value for key, value in updates.items() if key in allowed}
        if not filtered:
            # Status geometry remains a build-time contract.
            return
        updated = dict(self.developer_ui_status)
        updated.update(filtered)
        normalized = self._normalize_developer_ui_status(updated)
        if normalized == self.developer_ui_status:
            return
        self.developer_ui_status = normalized
        self._developer_ui_status_custom = True
        self.settings.setValue(
            "developer/ui_status_v1",
            json.dumps(self.developer_ui_status, separators=(",", ":")),
        )
        self._apply_developer_ui_status()

    def set_developer_ui_status_value(self, key: str, value: object) -> None:
        self.set_developer_ui_status_values({key: value})

    def apply_developer_ui_status_preset(self, name: str) -> None:
        preset = DEV_UI_STATUS_PRESETS.get(str(name))
        if preset is not None:
            self.set_developer_ui_status_values(dict(preset))

    def current_developer_ui_status_preset(self) -> str:
        for name, preset in DEV_UI_STATUS_PRESETS.items():
            if all(self.developer_ui_status.get(key) == value for key, value in preset.items()):
                return name
        return "Custom"

    def reset_developer_ui_status(self) -> None:
        self._developer_ui_status_custom = False
        self.developer_ui_status = self._default_developer_ui_status()
        self.settings.remove("developer/ui_status_v1")
        self._apply_developer_ui_status()

    def _apply_developer_ui_status(self) -> None:
        bar = self.statusBar()
        if isinstance(bar, TerminalStatusBar):
            bar.apply_tuning(self.developer_ui_status)
        self._apply_stylesheet()

    def current_developer_ui_layout_preset(self) -> str:
        return "Near zero"

    def _apply_developer_ui_layout(self) -> None:
        if not hasattr(self, "chart_surface"):
            return
        v = self.developer_ui_layout
        # Root horizontal edge gutters are structural and symmetric. They use
        # the same visual gap as adjacent chart/right-rail panes so neither side
        # sits closer to the screen edge. Top/bottom stay independently defined.
        self._outer_layout.setContentsMargins(
            v["outer_left"], v["outer_top"], v["outer_right"], v["outer_bottom"]
        )
        news_ribbon_height = int(getattr(self, "_news_ribbon_height", 40))
        self.microstructure_card.setFixedHeight(news_ribbon_height)
        self._left_workspace_layout.setContentsMargins(0, 0, 0, 0)
        self._workspace_nav_layout.setSpacing(0)
        self._chart_column_layout.setContentsMargins(
            v["chart_column_left"], v["chart_column_top"],
            v["chart_column_right"], v["chart_column_bottom"]
        )
        self._chart_column_layout.setSpacing(v["chart_column_spacing"])
        self.right_rail_host.setContentsMargins(0, 0, 0, 0)
        for splitter in self.right_rail_controller.interaction_splitters():
            splitter.setContentsMargins(0, 0, 0, 0)
        panel_margin = v["right_panel_margin"]
        self.right_rail_controller.set_panel_margin(panel_margin)
        self._update_right_rail_minimum_width(panel_margin=panel_margin)
        # UI Tuner geometry is cosmetic/internal. Re-assert structural splitter
        # interaction policy afterwards so stylesheet repolish cannot shrink the
        # invisible grab target or alter the controller-owned column contract.
        self._apply_splitter_interaction_policy()
        self.instrument_bar.set_developer_geometry(
            left=v["instrument_left"], top=v["instrument_top"],
            right=v["instrument_right"], bottom=v["instrument_bottom"],
            spacing=v["instrument_spacing"], height=v["instrument_height"],
        )
        self.chart.set_developer_layout_tuning(
            vertical_spacing=v["chart_vertical_spacing"],
            axis_width=v["axis_width"],
        )


    def _show_hotkeys_reference(self) -> None:
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Nightwatch hotkeys")
        dialog.setModal(True)
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            dialog.resize(
                max(320, min(670, available.width() - 40)),
                max(320, min(570, available.height() - 40)),
            )
        else:
            dialog.resize(670, 570)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        heading = QtWidgets.QLabel("APPLICATION, CHART & EXECUTION HOTKEYS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Read-only reference. Fixed execution grammar and the currently configured trading shortcuts are shown together with chart/application shortcuts."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        table = QtWidgets.QTableWidget()
        table.setObjectName("hotkeyReferenceTable")
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(("SHORTCUT", "ACTION"))
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setSortingEnabled(False)
        table.verticalHeader().hide()
        table.horizontalHeader().setSortIndicatorShown(False)
        table.horizontalHeader().setSectionsClickable(False)
        table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        rows = [
            ("F11", "Toggle borderless full screen"),
            ("Esc", "Exit full screen or cancel the active drawing tool"),
            ("Alt+R", "Fit the primary chart"),
            ("Ctrl+Tab", "Next workspace"),
            ("Ctrl+Shift+Tab", "Previous workspace"),
            ("Alt+1 / Alt+2 / Alt+3 / Alt+4", "Chart / Leaders / Sectors / Rotation"),
            ("Ctrl+P", "Cycle quick right-panel layout"),
            ("Ctrl+PageDown", "Next saved right-panel preset"),
            ("Ctrl+PageUp", "Previous saved right-panel preset"),
            ("Up / Down", "Select previous / next watchlist symbol"),
            ("Left / Right", "Select previous / next displayed timeframe"),
            ("Delete", "Delete a selected chart drawing"),
            ("Shift+Delete", "Clear drawings on the primary chart"),
            ("Ctrl+O", "Expand or collapse the account/orders drawer"),
            ("Ctrl+Shift+O", "Show or hide open orders on the chart"),
            ("Ctrl+Z", "Toggle primary-chart overview"),
            ("Alt+F", "Next automatic Fibonacci candidate"),
            ("Alt+Shift+F", "Previous automatic Fibonacci candidate"),
            ("Type letters/numbers", "Open symbol search while quick orders are locked"),
            ("Up / Down (search)", "Select the previous / next symbol-search result"),
            ("Enter (search)", "Open the selected symbol-search result"),
            ("B1-B9 / S1-S9 (unlocked)", "Quick BUY/SELL using 10%-90% collateral"),
            ("BB / SS (unlocked)", "Quick BUY/SELL using 100% collateral"),
            ("Ctrl+Shift+A", "Lock or unlock quick-order hotkeys"),
            ("Ctrl+Shift+X", "Create passive Smart Exit orders"),
            ("Shift+letter (unlocked)", "Open symbol search while quick-order hotkeys are unlocked"),
            ("Ctrl+Enter (ticket)", "Submit the focused manual execution ticket"),
        ]
        trading_labels = {
            "place_buy": "Submit configured quick BUY order",
            "place_sell": "Submit configured quick SELL order",
            "close_1": "Reduce selected position by close preset 1",
            "close_2": "Reduce selected position by close preset 2",
            "close_3": "Reduce selected position by close preset 3",
            "cancel_all": "Cancel current-symbol open orders",
            "open_trading": "Open Trading / Positions",
            "refresh_account": "Refresh trading account snapshot",
            "kill_session": "Cancel current-symbol orders and lock quick trading",
        }
        for action, shortcut in self.trading_hotkeys.items():
            if shortcut:
                rows.append((shortcut, trading_labels.get(action, action.replace("_", " ").title())))
        for index, timeframe in enumerate(self.market_bar_timeframes, start=1):
            label = timeframe.upper() if timeframe in {"1d", "1w"} else timeframe
            rows.append((str(index), f"Switch primary chart to {label}"))
        for indicator, shortcut in self.indicator_shortcuts.items():
            if shortcut:
                rows.append((shortcut, f"Toggle {indicator} indicator"))

        table.setRowCount(len(rows))
        for row_index, (shortcut, description) in enumerate(rows):
            table.setItem(row_index, 0, QtWidgets.QTableWidgetItem(shortcut))
            table.setItem(row_index, 1, QtWidgets.QTableWidgetItem(description))
        table.resizeRowsToContents()

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addWidget(table, 1)
        layout.addWidget(buttons)
        dialog.exec()

    def _configure_crisp_ui(self, root: QtWidgets.QWidget | None = None) -> None:
        """Apply non-overflow rules after first paint or to one lazy surface."""
        target = root or self
        for view in target.findChildren(QtWidgets.QAbstractItemView):
            view.setTextElideMode(Qt.TextElideMode.ElideRight)
        for table in target.findChildren(QtWidgets.QTableView):
            table.setWordWrap(False)
            header = table.horizontalHeader()
            header.setTextElideMode(Qt.TextElideMode.ElideRight)
            header.setMinimumSectionSize(
                26 if table.objectName() == "sidebarWatchlistTable" else 34
            )
        for combo in target.findChildren(QtWidgets.QComboBox):
            combo.view().setTextElideMode(Qt.TextElideMode.ElideRight)
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(4)
        for tabs in target.findChildren(QtWidgets.QTabBar):
            parent = tabs.parentWidget()
            if parent is not None and parent.objectName() == "tradingAccountTabs":
                tabs.setElideMode(Qt.TextElideMode.ElideNone)
                tabs.setUsesScrollButtons(True)
                tabs.setExpanding(True)
            else:
                tabs.setElideMode(Qt.TextElideMode.ElideRight)
                tabs.setUsesScrollButtons(True)
        for button in target.findChildren(QtWidgets.QAbstractButton):
            text_value = button.text().replace("&", "")
            if (
                text_value
                and button.width() > 0
                and button.fontMetrics().horizontalAdvance(text_value) > button.width() - 12
                and not button.toolTip()
            ):
                button.setToolTip(text_value)

    def _apply_stylesheet(self) -> None:
        t = self.ui_theme
        set_tooltip_theme(t)
        control_border = t["control_border"]
        surfaces = getattr(self, "developer_ui_surfaces", DEV_UI_SURFACE_DEFAULTS)
        section_radius = f"{int(surfaces['block_radius'])}px"
        status = getattr(self, "developer_ui_status", None)
        self.setStyleSheet(build_shell_stylesheet(self.theme_name, t, surfaces, status))
        if hasattr(self, "main_toolbar"):
            self.main_toolbar.apply_theme(t)
        if hasattr(self, "right_rail_controller"):
            self.right_rail_controller.set_panel_edge_style(
                radius=int(surfaces["block_radius"]),
                border_color=str(t.get("border", "#202020")),
                gap_color="#000000",
            )
        application = QtWidgets.QApplication.instance()
        if application is not None:
            if not hasattr(self, "_native_application_stylesheet"):
                self._native_application_stylesheet = application.styleSheet()
            dialog_border = f"border: 2px solid {control_border}; border-radius: {section_radius};"
            dialog_bg = t["panel2"]
            application.setStyleSheet(
                self._native_application_stylesheet +
                f"\nQDialog, QMessageBox {{ background: {dialog_bg}; color: {t['text']}; {dialog_border} }}"
            )
        base_palette = (
            QtGui.QPalette(self._native_application_palette)
            if self._native_application_palette is not None
            else QtGui.QPalette(self.palette())
        )
        palette = base_palette
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(t["bg"]))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(t["control"]))
        palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(t["panel2"]))
        palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(t["control"]))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.ColorRole.Light, QtGui.QColor(t["control_hover"]))
        palette.setColor(QtGui.QPalette.ColorRole.Midlight, QtGui.QColor(t["border"]))
        palette.setColor(QtGui.QPalette.ColorRole.Mid, QtGui.QColor(t["control_border"]))
        palette.setColor(QtGui.QPalette.ColorRole.Dark, QtGui.QColor(t["panel2"]))
        palette.setColor(QtGui.QPalette.ColorRole.Shadow, QtGui.QColor(t["bg"]))
        palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor(t["red"]))
        palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(t["muted"]))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(t["header"]))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(t["active"]))
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor(t["cyan"]))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.Text,
            QtGui.QColor(t["muted"]),
        )
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.ButtonText,
            QtGui.QColor(t["muted"]),
        )
        self.setPalette(palette)
        if application is not None:
            application.setPalette(palette)
        # QSS owns color/geometry; semantic typography owns font family, size,
        # weight and raster strategy. Apply it after QSS so pixel-sized legacy
        # selectors cannot silently override the active text role.
        apply_typography(self)
        # Re-polishing from the UI Tuner/theme path must never alter structural
        # splitter hit geometry or the controller-owned column contract.
        if hasattr(self, "right_rail_controller"):
            self._apply_splitter_interaction_policy()
            QTimer.singleShot(0, self._apply_splitter_interaction_policy)

    def _restore_layout(self) -> None:
        geometry = self.settings.value("window/geometry_v1")
        if geometry:
            self.restoreGeometry(geometry)
        window_state = self.settings.value("window/state_v1")
        if window_state:
            self.restoreState(window_state)
        # Nightwatch always launches in its borderless fullscreen presentation.
        # Saved normal/maximized geometry is retained solely for the in-session
        # F11/Escape transition back to windowed mode; it never decides startup.
        self.start_maximized = False
        self.start_fullscreen = True
        # These legacy startup-choice keys no longer own startup behavior.
        self.settings.remove("window/maximized")
        self.settings.remove("window/fullscreen")
        stored_groups = self.settings.value("watchlist/groups_v1", "", str)
        restored_groups = False
        if stored_groups:
            try:
                payload = json.loads(stored_groups)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("groups"), dict):
                groups = {
                    str(name): list(symbols)
                    for name, symbols in payload["groups"].items()
                    if isinstance(symbols, list)
                }
                self.watchlist.set_groups(
                    groups, str(payload.get("active", "")), emit=False
                )
                restored_groups = True
                self.watchlist_sidebar._groups_changed(self.watchlist.group_snapshot())
        stored_watchlist = self.settings.value("watchlist_symbols", "", str)
        if stored_watchlist and not restored_groups:
            try:
                symbols = json.loads(stored_watchlist)
            except (TypeError, json.JSONDecodeError):
                symbols = None
            if isinstance(symbols, list):
                cleaned = [
                    self._normalize_symbol(str(symbol))
                    for symbol in symbols
                    if str(symbol).strip()
                ]
                self.watchlist.set_symbols(cleaned, emit=False)
                self.watchlist_sidebar.refresh()

        # RightRailController owns panel membership, movable topology,
        # splitter proportions and rail width.
        # Legacy splitter/panel keys are migrated once into right_rail/state_v5.
        self.right_rail_controller.refresh_geometry_constraints()
        self._sync_right_panel_alignment()
        # The unified trading panel starts on the execution ticket.
        self.trading_workspace.set_page(0)
        self._sync_right_panel_alignment()
        # Leaders/Sectors restore their own state when lazily materialized.
        self._apply_chart_layout_state()
        stored_workspace = self.settings.value("active_workspace", 0, int)
        # Preserve the selected global workspace, including Rotation.
        if stored_workspace not in (0, 1, 2, 3):
            stored_workspace = 0
        self._switch_workspace(stored_workspace)
        self._sync_right_layout_actions()

    def _save_layout(self) -> None:
        self._store_current_symbol_drawings()
        fullscreen = self._effective_fullscreen()
        geometry = (
            self._restore_geometry_after_fullscreen
            if fullscreen and self._restore_geometry_after_fullscreen is not None
            else self.saveGeometry()
        )
        self.settings.setValue("window/geometry_v1", geometry)
        self.settings.setValue("window/state_v1", self.saveState())
        # Startup is always fullscreen, so normal/maximized/fullscreen choice is
        # no longer persisted. Geometry is still saved for an in-session exit.
        self.settings.setValue("theme", self.theme_name)
        self.settings.setValue("candle_style_v4", self.candle_style)
        self.settings.setValue("auto_scale", self.auto_scale)
        self.settings.setValue("logarithmic", self.logarithmic)
        self.settings.setValue("chart/rsi_period", self.chart.rsi_period)
        self.settings.setValue("chart/rsi_upper", self.chart.rsi_upper)
        self.settings.setValue("chart/rsi_lower", self.chart.rsi_lower)
        self.settings.setValue(
            "chart/indicator_settings_v1",
            json.dumps(self.chart.indicator_settings, separators=(",", ":")),
        )
        self.settings.setValue(
            "chart/indicator_shortcuts_v1",
            json.dumps(self.indicator_shortcuts, separators=(",", ":")),
        )
        self.settings.setValue(
            "chart/study_heights_v1",
            json.dumps(self.chart.saved_study_heights(), separators=(",", ":")),
        )
        self.settings.setValue(
            "chart/volume_bar_height_pct_v1",
            self.chart.volume_bar_height_setting(),
        )
        self.settings.setValue(
            "chart/manual_drawings_v1",
            json.dumps(self.saved_drawings_by_symbol, separators=(",", ":")),
        )
        self.settings.setValue("ticker_sort_v2", self.ticker_sort_mode)
        self.settings.setValue("last_symbol", self.current_symbol)
        self.settings.setValue("last_interval", self.current_interval)
        self.settings.setValue("chart/layout_mode_v1", self.chart_layout_mode)
        self.settings.setValue(
            "chart/auxiliary_markets_v1",
            json.dumps(
                self.chart_container.auxiliary_state(),
                separators=(",", ":"),
            ),
        )
        self.settings.setValue(
            "trading/hotkeys_v1", json.dumps(self.trading_hotkeys, sort_keys=True)
        )
        self.settings.setValue(
            "trading/quick_preset_v1",
            json.dumps(self.quick_trading_preset, sort_keys=True),
        )
        self.settings.setValue("watchlist_symbols", json.dumps(self.watchlist.symbols))
        self.settings.setValue(
            "watchlist/groups_v1",
            json.dumps(self.watchlist.group_snapshot(), separators=(",", ":")),
        )
        self.settings.setValue("active_workspace", self.workspace_stack.currentIndex())
        self.settings.setValue(
            "orderbook/columns_v1",
            json.dumps(
                self.orderbook.column_preferences(),
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.settings.setValue(
            "orderbook/presentation_v2",
            json.dumps(
                self.orderbook.presentation_state(),
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.right_rail_controller.capture_geometry()
        self.right_rail_controller.save_state()
        self.settings.remove("main_splitter_v4")
        self.settings.setValue("right_layout_preset", self.right_layout_preset)
        self.settings.setValue("right_layout_presets_v2", json.dumps(self.right_layout_presets, separators=(",", ":")))
        if self.market_board is not None:
            self.market_board.save_ui_state(self.settings)
        if self.sector_overview is not None:
            self.sector_overview.save_ui_state(self.settings)
        self.rotation_overview.save_ui_state(self.settings)
        self.settings.sync()

    def _switch_workspace(self, index: int) -> None:
        index = max(0, min(3, int(index)))
        self.setProperty("chartTooltipsSuppressed", not self.learning_mode)
        if not self.learning_mode:
            QtWidgets.QToolTip.hideText()
        if index != 0 and self.chart.drawing_mode is not None:
            self.chart.cancel_drawing()

        chart_workspace = index == 0
        # Global navigation and utilities remain visible; the instrument row
        # occupies only the Chart page above its viewport and price axis.
        self.instrument_bar.set_chart_context_visible(chart_workspace)
        if hasattr(self, "right_rail_controller"):
            if not chart_workspace and self.right_rail_host.isVisible():
                # Preserve the trader's rail geometry before making standalone
                # analytics workspaces consume the full application width.
                self.right_rail_controller.capture_geometry()
            self.right_rail_controller.set_host_active(chart_workspace)
            if hasattr(self, "_left_workspace_layout"):
                self._left_workspace_layout.setContentsMargins(
                    0, 0, 0, 0
                )

        self.workspace_stack.setCurrentIndex(index)
        market_board = self.market_board
        sector_overview = self.sector_overview
        if market_board is not None:
            market_board.set_active(index in (1, 3), visible=index == 1)
        if sector_overview is not None:
            sector_overview.set_active(index == 2)
        self.rotation_overview.set_active(index == 3)
        self.chart_container.set_workspace_active(chart_workspace)
        self.chart.set_presentation_active(chart_workspace)
        QTimer.singleShot(0, self._sync_market_depth_networking)

        name = ("CHART", "LEADERS", "SECTORS", "ROTATION")[index]
        button = self.workspace_buttons.get(name)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        if chart_workspace:
            self._refresh_chart_workspace_presentations()
            if hasattr(self, "right_rail_controller"):
                QTimer.singleShot(0, self.right_rail_controller.refresh_geometry_constraints)
                QTimer.singleShot(0, self.right_rail_controller.sync_interaction_surfaces)
    def _refresh_chart_workspace_presentations(self) -> None:
        """Catch visible chart/right-rail surfaces up from canonical latest state."""
        watchlist_latest = [
            self.tickers[symbol]
            for symbol in self.watchlist.symbols
            if symbol in self.tickers
        ]
        if watchlist_latest:
            self.watchlist.update_tickers(watchlist_latest)
        current = self.tickers.get(self.current_symbol)
        if current:
            self.stats.update_ticker(current)
            self.last_price = safe_float(current.get("c"), self.last_price)
            self.chart.set_last_price(
                self.last_price, safe_float(current.get("_price_time"))
            )
        mark_payload = self._deferred_mark_payload
        self._deferred_mark_payload = None
        if mark_payload is not None:
            self.stats.update_mark(mark_payload)
        interest_payload = self._deferred_interest_payload
        self._deferred_interest_payload = None
        if interest_payload is not None:
            reference = self.stats.mark or self.last_price
            self.stats.update_interest(interest_payload, reference)
        self._request_top_metrics_sync(immediate=True)
        if self.watchlist_hour_refresh_pending:
            self._refresh_watchlist_hour_changes(force=True)

    def _toggle_trading_account_view(self) -> bool:
        if self.workspace_stack.currentIndex() != 0 or not self.right_rail_controller.panel_active("trading"):
            return False
        self.trading_workspace.set_page(1 - self.trading_workspace.current_page())
        return True

    def _show_trading_sidebar(self) -> None:
        self._switch_workspace(0)
        self.trading_workspace.set_page(0)
        self._apply_preset_containing("Trading / positions")
        self.trading_workspace.set_symbol(
            self.current_symbol,
            self.symbol_rules.get(self.current_symbol, SymbolRules()),
        )
        if self.trading_gateway.has_credentials():
            self.trading_gateway.refresh_account(self.current_symbol)

    def _open_market_from_board(self, symbol: str) -> None:
        self.switch_symbol(symbol)
        self._switch_workspace(0)

    def _reduce_position_in_trade(self, position: dict[str, Any]) -> None:
        symbol = str(position.get("symbol") or "")
        if not symbol or symbol not in self.symbol_rules:
            self.statusBar().showMessage("Load exchange rules before reducing this position.", 5000)
            return
        active = next((row for row in self.trading_gateway.position_cache.values()
                       if row.get("symbol") == symbol
                       and str(row.get("positionSide") or "BOTH") == str(position.get("positionSide") or "BOTH")
                       and abs(safe_float(row.get("positionAmt"))) > 0), None)
        if active is None:
            self.statusBar().showMessage("This position is no longer open. Refresh account data.", 5000)
            return
        self._open_market_from_board(symbol)
        self._show_trading_sidebar()
        self.order_panel.apply_account_snapshot({"account": {"positions": list(self.trading_gateway.position_cache.values()), "availableBalance": self.trading_gateway.available_balance(self.order_panel.rules.margin_asset)}})
        self.order_panel.focus_position(active, enter_reduce=True)

    def _open_watchlist_symbol(self, symbol: str) -> None:
        self.switch_symbol(symbol)

    def _cycle_watchlist_symbol(self, step: int) -> None:
        """Switch the active chart to the previous/next saved watchlist symbol."""
        symbols = [
            str(symbol)
            for symbol in self.watchlist.symbols
            if str(symbol).strip()
            and (not self.valid_symbols or str(symbol) in self.valid_symbols)
        ]
        if not symbols:
            self.statusBar().showMessage("WATCHLIST · EMPTY", 1800)
            return

        direction = -1 if step < 0 else 1
        try:
            current_index = symbols.index(self.current_symbol)
        except ValueError:
            target_index = len(symbols) - 1 if direction < 0 else 0
        else:
            target_index = (current_index + direction) % len(symbols)

        target = symbols[target_index]
        self.switch_symbol(target)
        self.statusBar().showMessage(
            f"WATCHLIST {target_index + 1}/{len(symbols)} · "
            f"{perpetual_display_symbol(target)}",
            1600,
        )

    def _sync_chart_visibility_actions(self) -> None:
        actions = getattr(self, "chart_visibility_actions", {})
        for mode, action in actions.items():
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(mode == self.chart_visibility_mode)
            del blocker

    def _set_chart_visibility_mode(
        self, mode: int, *, show_status: bool = True
    ) -> None:
        mode = max(0, min(2, int(mode)))
        self.chart_visibility_mode = mode
        self._apply_chart_visibility_mode(show_status=show_status)

    def _apply_chart_visibility_mode(self, show_status: bool = True) -> None:
        mode = self.chart_visibility_mode
        self.chart.set_indicators_enabled(mode == 0)
        self.chart.set_drawings_visible(mode < 2)
        self._sync_chart_visibility_actions()
        if show_status:
            messages = (
                "CHART · ALL LAYERS VISIBLE",
                "CHART · INDICATORS HIDDEN",
                "CHART · INDICATORS AND DRAWINGS HIDDEN",
            )
            self.statusBar().showMessage(messages[mode], 1800)
        self._sync_settings_window()


    def _store_current_symbol_drawings(self) -> None:
        drawings = self.chart.export_manual_drawings()
        if drawings:
            self.saved_drawings_by_symbol[self.current_symbol] = drawings
        else:
            self.saved_drawings_by_symbol.pop(self.current_symbol, None)

    def _restore_current_symbol_drawings(self) -> None:
        drawings = self.saved_drawings_by_symbol.get(self.current_symbol, [])
        self.chart.import_manual_drawings(drawings)
        self.chart.set_drawings_visible(self.chart_visibility_mode < 2)


    def _toggle_active_symbol_watchlist(self) -> None:
        symbol = self._normalize_symbol(self.current_symbol)
        watched = self.watchlist.contains(symbol)
        changed = self.watchlist.set_symbol_watched(symbol, not watched)
        self.statusBar().showMessage(
            f"WATCHLIST · "
            f"{'REMOVED' if watched and changed else 'ADDED' if changed else 'UNCHANGED'} · "
            f"{perpetual_display_symbol(symbol)}",
            1800,
        )


    def _fetch_single_coin_icon(self, base: str, db: Any) -> dict[str, Any]:
        base = coin_base_symbol(base)
        remote = coin_remote_symbol(base)
        if not base or not remote:
            return {"base": base, "status": "missing"}
        if coin_icon_exists(base):
            return {"base": base, "status": "ready", "changed": False}
        if not db.coin_icon_retry_due(base, COIN_ICON_REFRESH_MS):
            return {"base": base, "status": "missing", "cached": True}

        coingecko_error: Exception | None = None
        coingecko_queried = False
        coingecko_candidate_found = False
        try:
            row = _coingecko_icon_rows([remote]).get(remote)
            coingecko_queried = True
            candidate = _coingecko_candidate(row) if row is not None else None
            coingecko_candidate_found = candidate is not None
            if candidate is not None:
                try:
                    changed = _store_coin_icon_candidate(base, candidate, db)
                    return {"base": base, "status": "ready", "changed": changed}
                except Exception as exc:
                    coingecko_error = exc
        except Exception as exc:
            coingecko_error = exc

        paprika_error: Exception | None = None
        paprika_queried = False
        paprika_candidate_found = False
        try:
            search = http_json(
                COINPAPRIKA_API + "/search",
                params={"q": remote, "c": "currencies", "limit": 10},
                timeout=15.0,
            )
            paprika_queried = True
            rows = search.get("currencies", []) if isinstance(search, dict) else []
            exact = [
                row for row in rows
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == remote
                and row.get("is_active", True) is not False
            ]
            exact.sort(key=lambda row: int(safe_float(row.get("rank"), 9_999_999)))
            candidate = _coinpaprika_candidate(exact[0]) if exact else None
            paprika_candidate_found = candidate is not None
            if candidate is not None:
                changed = _store_coin_icon_candidate(base, candidate, db)
                return {"base": base, "status": "ready", "changed": changed}
        except Exception as exc:
            paprika_error = exc

        if (
            coingecko_queried
            and paprika_queried
            and not coingecko_candidate_found
            and not paprika_candidate_found
        ):
            db.mark_coin_icon_missing(base)
            return {"base": base, "status": "missing"}
        details = "; ".join(
            str(error) for error in (coingecko_error, paprika_error) if error is not None
        )
        raise RuntimeError(details or f"Icon lookup failed for {base}.")

    def _request_coin_icon(self, symbol: str) -> None:
        base = coin_base_symbol(symbol)
        if not base or self._closing:
            return
        if coin_icon_exists(base):
            self.watchlist.icon_lookup_finished(base, True)
            return
        if self.coin_icon_refresh_task is not None:
            self._coin_icon_deferred_missing.add(base)
            return
        if base in self.coin_icon_lookup_tasks:
            return
        db = self._ensure_app_database()
        task: ApiTask

        def done(result: dict[str, Any]) -> None:
            self.tasks.discard(task)
            self.coin_icon_lookup_tasks.pop(base, None)
            if self._closing:
                return
            found = result.get("status") == "ready" and coin_icon_exists(base)
            self.watchlist.icon_lookup_finished(
                base,
                found,
                retry_seconds=COIN_ICON_REFRESH_MS / 1000 if not found else 3600.0,
            )

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            self.coin_icon_lookup_tasks.pop(base, None)
            if not self._closing:
                self.watchlist.icon_lookup_finished(base, False, retry_seconds=900.0)

        task = ApiTask(
            lambda: self._fetch_single_coin_icon(base, db),
            source="coin icon lookup",
        )
        self.coin_icon_lookup_tasks[base] = task
        self.tasks.add(task)
        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        QtCore.QThreadPool.globalInstance().start(task, -1)

    def _refresh_coin_icons_weekly(self, symbols: tuple[str, ...], db: Any) -> dict[str, Any]:
        if not db.claim_coin_icon_full_refresh(COIN_ICON_REFRESH_MS):
            return {
                "attempted": False,
                "resolved": [],
                "changed": [],
                "missing": [],
                "failed": [],
            }
        bases = sorted(
            {coin_base_symbol(symbol) for symbol in symbols if coin_base_symbol(symbol)}
        )
        remote_by_base = {base: coin_remote_symbol(base) for base in bases}
        remote_symbols = list(dict.fromkeys(remote_by_base.values()))
        resolved: list[str] = []
        changed: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        coingecko_rows: dict[str, dict[str, Any]] = {}
        coingecko_available = True
        try:
            coingecko_rows = _coingecko_icon_rows(remote_symbols)
        except Exception:
            coingecko_rows = {}
            coingecko_available = False

        paprika_rows: dict[str, dict[str, Any]] | None = None
        paprika_available = True

        def paprika_candidate(remote: str) -> dict[str, str] | None:
            nonlocal paprika_rows, paprika_available
            if paprika_rows is None and paprika_available:
                try:
                    paprika_rows = _coinpaprika_icon_rows()
                except Exception:
                    paprika_rows = {}
                    paprika_available = False
            row = (paprika_rows or {}).get(remote)
            return _coinpaprika_candidate(row) if row is not None else None

        for base in bases:
            if self._closing:
                break
            remote = remote_by_base[base]
            saved = False
            candidate_found = False
            row = coingecko_rows.get(remote)
            candidate = _coingecko_candidate(row) if row is not None else None
            candidate_found = candidate is not None
            if candidate is not None:
                try:
                    icon_changed = _store_coin_icon_candidate(base, candidate, db)
                    saved = True
                    if icon_changed:
                        changed.append(base)
                except Exception:
                    saved = False
            if not saved:
                candidate = paprika_candidate(remote)
                candidate_found = candidate_found or candidate is not None
                if candidate is not None:
                    try:
                        icon_changed = _store_coin_icon_candidate(base, candidate, db)
                        saved = True
                        if icon_changed and base not in changed:
                            changed.append(base)
                    except Exception:
                        saved = False
            if saved or coin_icon_exists(base):
                resolved.append(base)
            elif coingecko_available and paprika_available and not candidate_found:
                db.mark_coin_icon_missing(base)
                missing.append(base)
            else:
                failed.append(base)
        return {
            "attempted": True,
            "resolved": resolved,
            "changed": changed,
            "missing": missing,
            "failed": failed,
        }

    def _schedule_coin_icon_refresh(self) -> None:
        if self._closing or self.coin_icon_refresh_task is not None or not self.valid_symbols:
            return
        db = self._ensure_app_database()
        symbols = tuple(sorted(self.valid_symbols))
        task: ApiTask

        def done(result: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self.coin_icon_refresh_task is task:
                self.coin_icon_refresh_task = None
            if self._closing:
                return
            resolved = set(result.get("resolved") or ())
            changed = set(result.get("changed") or ())
            missing = set(result.get("missing") or ())
            failed = set(result.get("failed") or ())
            deferred = set(self._coin_icon_deferred_missing)
            self._coin_icon_deferred_missing.clear()
            watched_bases = {
                coin_base_symbol(symbol)
                for group in self.watchlist.groups.values()
                for symbol in group
            }
            for base in resolved & (deferred | watched_bases):
                self.watchlist.icon_lookup_finished(base, True)
            if changed and self.market_board is not None:
                self.market_board.invalidate_coin_icons(changed)
            for base in missing & (deferred | watched_bases):
                self.watchlist.icon_lookup_finished(
                    base, False, retry_seconds=COIN_ICON_REFRESH_MS / 1000
                )
            for base in failed & (deferred | watched_bases):
                self.watchlist.icon_lookup_finished(base, False, retry_seconds=900.0)
            for base in deferred - resolved - missing - failed:
                self._request_coin_icon(base)

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            if self.coin_icon_refresh_task is task:
                self.coin_icon_refresh_task = None
            if self._closing:
                return
            deferred = tuple(self._coin_icon_deferred_missing)
            self._coin_icon_deferred_missing.clear()
            for base in deferred:
                self._request_coin_icon(base)

        task = ApiTask(
            lambda: self._refresh_coin_icons_weekly(symbols, db),
            source="coin icon weekly refresh",
        )
        self.coin_icon_refresh_task = task
        self.tasks.add(task)
        task.signals.finished.connect(done)
        task.signals.failed.connect(failed)
        QtCore.QThreadPool.globalInstance().start(task, -1)

    def _watchlist_changed(self, _symbols: object = None) -> None:
        self.settings.setValue("watchlist_symbols", json.dumps(self.watchlist.symbols))
        self.settings.setValue(
            "watchlist/groups_v1",
            json.dumps(self.watchlist.group_snapshot(), separators=(",", ":")),
        )
        self._sync_ticker_streams()
        self._refresh_watchlist_hour_changes(force=True)

    def _watchlist_groups_changed(self, _snapshot: object = None) -> None:
        self.settings.setValue(
            "watchlist/groups_v1",
            json.dumps(self.watchlist.group_snapshot(), separators=(",", ":")),
        )
        self.settings.setValue("watchlist_symbols", json.dumps(self.watchlist.symbols))

    def _refresh_watchlist_hour_changes(self, force: bool = False) -> None:
        if (
            not force
            and hasattr(self, "watchlist_sidebar")
            and not self.watchlist_sidebar.isVisible()
        ):
            self.watchlist_hour_refresh_pending = True
            return
        symbols = [
            symbol
            for symbol in self.watchlist.symbols
            if not self.valid_symbols or symbol in self.valid_symbols
        ]
        symbols = list(dict.fromkeys(symbols))
        hub = self.hub
        if not symbols or hub is None:
            return
        if self.watchlist_hour_task is not None:
            if force:
                self.watchlist_hour_refresh_pending = True
            return
        if force:
            self.watchlist_hour_refresh_pending = False

        requested = tuple(symbols)
        task: ApiTask

        def done(values: dict[str, float]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.watchlist_hour_task is task:
                self.watchlist_hour_task = None
            self.watchlist.set_hour_changes(values)
            self._store_search_hour_changes(values)
            if self.watchlist_hour_refresh_pending:
                self.watchlist_hour_refresh_pending = False
                QTimer.singleShot(0, self._refresh_watchlist_hour_changes)

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.watchlist_hour_task is task:
                self.watchlist_hour_task = None
            if self.watchlist_hour_refresh_pending:
                self.watchlist_hour_refresh_pending = False
                QTimer.singleShot(0, self._refresh_watchlist_hour_changes)

        task = launch_task(
            lambda: hub.rest.watchlist_hour_changes(list(requested)),
            done,
            failed,
        )
        self.watchlist_hour_task = task
        self.tasks.add(task)

    @staticmethod
    def _normalize_symbol(value: str) -> str:
        symbol = value.upper().strip().replace("/", "").replace("-", "")
        return symbol.removesuffix(".P")

    def _store_search_hour_changes(self, values: dict[str, float]) -> None:
        now = time.monotonic()
        for symbol, value in values.items():
            if math.isfinite(value):
                self.search_hour_cache[symbol] = (value, now)
        if self.symbol_search_dialog is not None:
            self.symbol_search_dialog.set_hour_changes(values)

    def _refresh_search_hour_changes(self, restart: bool = False) -> None:
        dialog = self.symbol_search_dialog
        hub = self.hub
        if dialog is None or self.history_cancel_requested or hub is None:
            return
        if restart:
            self.search_hour_queue = list(dialog.symbols)
        if self.search_hour_task is not None:
            return
        now = time.monotonic()
        self.search_hour_queue = [symbol for symbol in self.search_hour_queue if now - max(
            self.search_hour_cache.get(symbol, (0.0, -math.inf))[1],
            self.search_hour_attempts.get(symbol, -math.inf),
        ) >= 60.0]
        if not self.search_hour_queue:
            self.search_hour_timer.start()
            return
        # Load the visible/query matches first, then complete the full search
        # universe so header sorting is not restricted to the first 120 rows.
        query = dialog.search.text().upper().strip().replace("/", "").replace("-", "")
        visible = {str(dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                   for row in range(dialog.table.rowCount())}
        self.search_hour_queue.sort(key=lambda symbol: (symbol not in visible, query not in symbol))
        requested = self.search_hour_queue[:24]
        del self.search_hour_queue[:24]
        self.search_hour_attempts.update({symbol: now for symbol in requested})
        task: ApiTask

        def done(values: dict[str, float]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.search_hour_task is task:
                self.search_hour_task = None
            self._store_search_hour_changes(values)
            if self.symbol_search_dialog is not None and not self.history_cancel_requested:
                QTimer.singleShot(0, self._refresh_search_hour_changes)

        task = launch_task(
            lambda: hub.rest.watchlist_hour_changes(requested),
            done,
            lambda _message: done({}),
        )
        self.search_hour_task = task
        self.tasks.add(task)

    def open_symbol_search(self, initial_text: str = "") -> None:
        self.symbol_search_pending = False
        if self.symbol_search_dialog is not None:
            if initial_text:
                self.symbol_search_dialog.search.insert(initial_text)
            self.symbol_search_dialog.search.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        symbols, volume_values = self._filtered_market_universe()
        if not symbols and self.current_symbol in self.valid_symbols:
            symbols = [self.current_symbol]
        dialog = SymbolSearchDialog(
            symbols,
            self.tickers,
            self.ui_theme,
            initial_text,
            self.ticker_sort_mode,
            self.market_filter_timeframe,
            self.market_filter_min_volume,
            volume_values,
            self,
            hour_changes={symbol: value for symbol, (value, stamp) in self.search_hour_cache.items()
                          if time.monotonic() - stamp < 60.0},
        )
        dialog.filter_requested.connect(self.edit_market_filter)
        dialog.sort_changed.connect(self._set_ticker_sort)
        frame = self.frameGeometry()
        dialog.move(
            frame.center().x() - dialog.width() // 2,
            frame.top() + 92,
        )
        self.symbol_search_dialog = dialog
        dialog.finished.connect(
            lambda result, current=dialog: self._symbol_search_finished(
                current,
                result,
            )
        )
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()
        # Shortcut focus selects the seeded character; the next key replaces it.
        dialog.search.setFocus(Qt.FocusReason.OtherFocusReason)
        dialog.search.deselect()
        dialog.search.setCursorPosition(len(dialog.search.text()))
        QTimer.singleShot(0, lambda: self._refresh_search_hour_changes(restart=True))

    def _symbol_search_finished(
        self,
        dialog: SymbolSearchDialog,
        result: int,
    ) -> None:
        if self.symbol_search_dialog is not dialog:
            return
        selected = dialog.selected_symbol
        self.search_hour_timer.stop()
        self.search_hour_queue.clear()
        self.symbol_search_dialog = None
        dialog.deleteLater()
        if (
            result == int(QtWidgets.QDialog.DialogCode.Accepted)
            and selected
        ):
            self.switch_symbol(selected)
        QTimer.singleShot(0, self, self._restore_terminal_keyboard_focus)

    def _toggle_drawing_tool(self, mode: str, checked: bool) -> None:
        if self._syncing_drawing_buttons:
            return
        if checked:
            self.chart.set_drawing_mode(mode)
        elif self.chart.drawing_mode == mode:
            self.chart.cancel_drawing()

    def _toggle_order_rail_tool(self, checked: bool) -> None:
        if self._syncing_drawing_buttons:
            return
        if not self.testing_flags.get("magnetic_order_rail", False):
            blocker = QtCore.QSignalBlocker(self.horizontal_action)
            self.horizontal_action.setChecked(False)
            del blocker
            return
        self.chart.set_order_rail_placement(bool(checked))

    def _sync_order_rail_action(self, enabled: bool) -> None:
        self._syncing_drawing_buttons = True
        try:
            blocker = QtCore.QSignalBlocker(self.horizontal_action)
            self.horizontal_action.setChecked(bool(enabled))
            del blocker
        finally:
            self._syncing_drawing_buttons = False

    def _cycle_auto_fibonacci(self, step: int = 1) -> None:
        if self.workspace_stack.currentIndex() != 0:
            self.statusBar().showMessage(
                "AUTO FIB · AVAILABLE IN THE CHART WORKSPACE", 3200
            )
            return
        if self.chart.drawing_mode is not None:
            self.chart.cancel_drawing()
        message = self.chart.cycle_auto_fibonacci(step)
        self.statusBar().showMessage(message, 2600)

    def _sync_drawing_actions(self, mode: object) -> None:
        self._syncing_drawing_buttons = True
        try:
            ruler_blocker = QtCore.QSignalBlocker(self.ruler_action)
            fibonacci_blocker = QtCore.QSignalBlocker(self.fibonacci_action)
            self.ruler_action.setChecked(mode == "ruler")
            self.fibonacci_action.setChecked(mode == "fibonacci")
            del ruler_blocker, fibonacci_blocker
        finally:
            self._syncing_drawing_buttons = False

    def _sync_auto_scale_action(self, enabled: bool) -> None:
        self.auto_scale = bool(enabled)
        action = getattr(self, "auto_scale_action", None)
        if action is None or action.isChecked() == self.auto_scale:
            return
        blocker = QtCore.QSignalBlocker(action)
        action.setChecked(self.auto_scale)
        del blocker


    def _reset_armed_order_sequence(self) -> None:
        self._armed_order_timer.stop()
        self._armed_order_side = ""
        self._armed_order_deadline = 0.0

    def _expire_armed_order_sequence(self) -> None:
        if not self._armed_order_side:
            return
        remaining = self._armed_order_deadline - time.monotonic()
        if remaining > 0:
            self._armed_order_timer.start(max(1, int(remaining * 1000)))
            return
        side = "BUY" if self._armed_order_side == "B" else "SELL"
        self._armed_order_timer.stop()
        self._armed_order_side = ""
        self._armed_order_deadline = 0.0
        self.statusBar().showMessage(
            f"QUICK {side} EXPIRED · PRESS {'B' if side == 'BUY' else 'S'} AGAIN",
            2200,
        )

    def _set_indicator_enabled(self, name: str, enabled: bool) -> None:
        self.chart.toggle_indicator(name, enabled)
        self._sync_settings_window()
        if not enabled or not self.chart.candles:
            return
        if name == "Funding Rate History":
            self._ensure_funding_history_for_chart()
        elif name == "Liquidations":
            self._load_recorded_liquidations()

    def _warm_initial_chart_history(self) -> None:
        hub = self.hub
        if (
            self.lazy_chart_history_task is not None
            or self.market_history_dialog is not None
            or hub is None

            or not self.chart.candles
            # Daily/weekly/monthly charts keep their initial snapshot and page
            # on demand instead of prewarming 5,800 bars on every timeframe.
            or constants.INTERVAL_SECONDS[self.current_interval] >= 86_400
            or self.chart._history_exhausted
            or len(self.chart.candles) >= INITIAL_CHART_WARM_CANDLES
        ):
            return

        symbol = self.current_symbol
        interval = self.current_interval
        generation = hub.generation
        db = self._ensure_app_database()
        interval_ms = int(constants.INTERVAL_SECONDS[interval] * 1000)
        oldest_time = float(self.chart.candles[0].time)
        before_ms = int(round(oldest_time * 1000.0))
        wanted = max(0, INITIAL_CHART_WARM_CANDLES - len(self.chart.candles))
        if before_ms <= 0 or interval_ms <= 0 or wanted <= 0:
            return

        task: ApiTask

        def load() -> dict[str, Any]:
            start_ms = max(0, before_ms - interval_ms * wanted)
            local_rows = db.load_candles(
                symbol,
                interval,
                start_ms,
                before_ms - 1,
            )
            local_by_time = {
                int(round(candle.time * 1000.0)): candle
                for candle in local_rows
            }
            contiguous_reverse: list[Candle] = []
            expected = before_ms - interval_ms
            while expected >= 0 and len(contiguous_reverse) < wanted:
                candle = local_by_time.get(expected)
                if candle is None:
                    break
                contiguous_reverse.append(candle)
                expected -= interval_ms
            local = list(reversed(contiguous_reverse))

            remaining = max(0, wanted - len(local))
            fetched: list[Candle] = []
            exhausted = False
            superseded = False
            network_before_ms = (
                int(round(local[0].time * 1000.0))
                if local
                else before_ms
            )

            while remaining > 0:
                if (
                    generation != hub.generation

                    or hub.stopping
                ):
                    superseded = True
                    break
                request_limit = min(1_000, remaining)
                page = hub.rest.older_candles(
                    symbol,
                    interval,
                    network_before_ms,
                    request_limit,
                )
                if not page:
                    exhausted = True
                    break
                db.cache_candles(symbol, interval, page)
                fetched.extend(page)
                next_before_ms = int(round(page[0].time * 1000.0))
                if next_before_ms <= 0 or next_before_ms >= network_before_ms:
                    exhausted = True
                    break
                network_before_ms = next_before_ms
                remaining -= len(page)
                if len(page) < request_limit:
                    exhausted = True
                    break

            combined = {candle.time: candle for candle in fetched}
            combined.update({candle.time: candle for candle in local})
            candles = [
                candle
                for candle in sorted(combined.values(), key=lambda row: row.time)
                if candle.time < oldest_time
            ]
            return {
                "symbol": symbol,
                "interval": interval,
                "generation": generation,
                "candles": candles[-wanted:],
                "exhausted": exhausted,
                "superseded": superseded,
            }

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.lazy_chart_history_task is task:
                self.lazy_chart_history_task = None
            if (
                payload.get("superseded")
                or payload.get("generation") != hub.generation
                or payload.get("symbol") != self.current_symbol
                or payload.get("interval") != self.current_interval
            ):
                return
            candles = list(payload.get("candles") or [])
            self.chart.prepend_history_page(
                candles,
                exhausted=bool(payload.get("exhausted")),
            )
            if candles:
                hub.inject_history(
                    self.current_symbol,
                    self.current_interval,
                    candles,
                )

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.lazy_chart_history_task is task:
                self.lazy_chart_history_task = None

        task = launch_task(load, done, failed)
        self.lazy_chart_history_task = task
        self.tasks.add(task)

    def _request_older_chart_history(self, oldest_time: float) -> None:
        hub = self.hub
        if (
            self.lazy_chart_history_task is not None
            or self.market_history_dialog is not None
            or hub is None

            or not self.chart.candles
        ):
            self.chart.history_request_failed()
            return

        symbol = self.current_symbol
        interval = self.current_interval
        generation = hub.generation
        db = self._ensure_app_database()
        interval_ms = int(constants.INTERVAL_SECONDS[interval] * 1000)
        before_ms = int(round(float(oldest_time) * 1000.0))
        page_limit = 1_000
        if before_ms <= 0 or interval_ms <= 0:
            self.chart.prepend_history_page([], exhausted=True)
            return

        task: ApiTask

        def load() -> dict[str, Any]:
            # SQLite is checked first so previously downloaded data
            # costs no network weight and appears immediately on subsequent pans.
            start_ms = int(round(shift_candle_time(before_ms / 1000.0, interval, -page_limit) * 1000.0))
            local_rows = db.load_candles(
                symbol, interval, start_ms, before_ms - 1
            )
            local_by_time = {
                int(round(candle.time * 1000.0)): candle
                for candle in local_rows
            }
            contiguous_reverse: list[Candle] = []
            expected = int(round(shift_candle_time(before_ms / 1000.0, interval, -1) * 1000.0))
            while expected > 0 and len(contiguous_reverse) < page_limit:
                candle = local_by_time.get(expected)
                if candle is None:
                    break
                contiguous_reverse.append(candle)
                expected = int(round(shift_candle_time(expected / 1000.0, interval, -1) * 1000.0))
            local = list(reversed(contiguous_reverse))

            remaining = page_limit - len(local)
            fetched: list[Candle] = []
            exhausted = False
            superseded = False
            if remaining > 0:
                if (
                    generation != hub.generation

                    or hub.stopping
                ):
                    superseded = True
                else:
                    network_before_ms = (
                        int(round(local[0].time * 1000.0))
                        if local
                        else before_ms
                    )
                    fetched = hub.rest.older_candles(
                        symbol, interval, network_before_ms, remaining
                    )
                    if fetched:
                        db.cache_candles(symbol, interval, fetched)
                    exhausted = len(fetched) < remaining

            # At most 1,000 rows: small-page de-duplication is cheap and avoids
            # relying on perfect local/network boundary alignment.
            combined = {candle.time: candle for candle in fetched}
            combined.update({candle.time: candle for candle in local})
            page = sorted(combined.values(), key=lambda candle: candle.time)
            page = [candle for candle in page if candle.time < oldest_time]
            return {
                "symbol": symbol,
                "interval": interval,
                "generation": generation,
                "candles": page[-page_limit:],
                "exhausted": exhausted and len(page) < page_limit,
                "superseded": superseded,
            }

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.lazy_chart_history_task is task:
                self.lazy_chart_history_task = None
            if (
                payload.get("superseded")
                or payload.get("generation") != hub.generation
                or payload.get("symbol") != self.current_symbol
                or payload.get("interval") != self.current_interval
            ):
                return
            candles = list(payload.get("candles") or [])
            self.chart.prepend_history_page(
                candles,
                exhausted=bool(payload.get("exhausted")),
            )
            if candles:
                hub.inject_history(
                    self.current_symbol,
                    self.current_interval,
                    candles,
                )
                funding_action = self.indicator_actions.get("Funding Rate History")
                if funding_action is not None and funding_action.isChecked():
                    self._ensure_funding_history_for_chart()
                liquidation_action = self.indicator_actions.get("Liquidations")
                if liquidation_action is not None and liquidation_action.isChecked():
                    self._load_recorded_liquidations()

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            if self.lazy_chart_history_task is task:
                self.lazy_chart_history_task = None
            if (
                generation == hub.generation
                and symbol == self.current_symbol
                and interval == self.current_interval
            ):
                self.chart.history_request_failed()

        task = launch_task(load, done, failed)
        self.lazy_chart_history_task = task
        self.tasks.add(task)

    def _chart_history_range(self) -> tuple[int, int] | None:
        if not self.chart.candles:
            return None
        start_ms = int(self.chart.candles[0].time * 1000)
        end_ms = int(
            max(
                time.time(),
                shift_candle_time(self.chart.candles[-1].time, self.current_interval),
            )
            * 1000
        )
        return start_ms, end_ms

    def _ensure_funding_history_for_chart(self) -> None:
        history_range = self._chart_history_range()
        if history_range is None:
            return
        symbol = self.current_symbol
        start_ms, end_ms = history_range
        task_key = f"funding:{symbol}:{start_ms}:{end_ms}"
        if task_key in self.indicator_history_tasks:
            return
        db = self._ensure_app_database()
        indicator_rest = self._ensure_indicator_rest()
        task: ApiTask

        def worker() -> list[dict[str, Any]]:
            coverage = db.event_download_coverage(symbol, "funding")
            ranges: list[tuple[int, int]] = []
            first = int(coverage.get("first_time") or 0)
            last = int(coverage.get("last_time") or 0)
            if first <= 0 or last <= 0:
                ranges.append((start_ms, end_ms))
            else:
                if start_ms < first:
                    ranges.append((start_ms, min(end_ms, first - 1)))
                if end_ms > last:
                    ranges.append((max(start_ms, last + 1), end_ms))
            for range_start, range_end in ranges:
                if range_start > range_end:
                    continue
                rows = indicator_rest.funding_range(
                    symbol, range_start, range_end
                )
                event_rows = [
                    (int(safe_float(row.get("fundingTime"))), dict(row))
                    for row in rows
                    if int(safe_float(row.get("fundingTime"))) > 0
                ]
                db.cache_market_event_page(symbol, "funding", event_rows)
            return db.load_market_events(
                symbol, ("funding",), start_ms, end_ms
            )

        def done(events: list[dict[str, Any]]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.indicator_history_tasks.pop(task_key, None)
            action = self.indicator_actions.get("Funding Rate History")
            if symbol != self.current_symbol or action is None or not action.isChecked():
                return
            self.chart.update_funding_history(
                [event["payload"] for event in events if isinstance(event.get("payload"), dict)]
            )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.indicator_history_tasks.pop(task_key, None)
            self.statusBar().showMessage(f"Funding history unavailable: {message}", 7000)

        task = launch_task(worker, done, failed)
        self.indicator_history_tasks[task_key] = task
        self.tasks.add(task)

    def _load_recorded_liquidations(self) -> None:
        history_range = self._chart_history_range()
        if history_range is None:
            self.statusBar().showMessage("Load a chart before loading liquidation history.", 3000)
            return
        symbol = self.current_symbol
        start_ms, end_ms = history_range
        task_key = f"liquidations:{symbol}:{start_ms}:{end_ms}"
        if task_key in self.indicator_history_tasks:
            return
        db = self._ensure_app_database()
        task: ApiTask

        def done(events: list[dict[str, Any]]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.indicator_history_tasks.pop(task_key, None)
            if symbol != self.current_symbol:
                return
            self.chart.update_liquidation_history(events)
            self.statusBar().showMessage(
                f"Loaded {len(events)} locally recorded liquidation events.", 3000
            )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.indicator_history_tasks.pop(task_key, None)
            self.statusBar().showMessage(f"Liquidation history unavailable: {message}", 7000)

        task = launch_task(
            lambda: db.load_market_events(
                symbol, ("liquidation",), start_ms, end_ms
            ),
            done,
            failed,
        )
        self.indicator_history_tasks[task_key] = task
        self.tasks.add(task)

    def _trading_armed_changed(self, armed: bool) -> None:
        if not armed:
            self._reset_armed_order_sequence()
        self._sync_execution_ticket_state()

    def _toggle_quick_order_lock(self) -> None:
        """Toggle the hotkey-only quick-order safety lock.

        Manual ticket submission, explicit close controls, and cancel controls
        remain independent of this state.
        """
        if self.trading_gateway.armed:
            self.trading_gateway.disarm()
            self.statusBar().showMessage("QUICK ORDERS LOCKED", 2200)
            return
        if not self.trading_gateway.has_credentials():
            self.order_panel.edit_credentials()
        if not self.trading_gateway.has_credentials():
            self.statusBar().showMessage("QUICK ORDERS LOCKED · CREDENTIALS REQUIRED", 4000)
            return
        if self.trading_gateway.arm():
            self.trading_gateway.ensure_cross(self.current_symbol)
            self.trading_gateway.refresh_account(self.current_symbol)
            self.statusBar().showMessage("QUICK ORDERS UNLOCKED", 2200)

    def _handle_armed_trading_key(
        self,
        event: QtGui.QKeyEvent,
        key: int,
        modifiers: Qt.KeyboardModifier,
        text_input: bool,
    ) -> bool:
        if (
            not self.trading_gateway.armed
            or text_input
            or self.symbol_search_dialog is not None
            or QtWidgets.QApplication.activePopupWidget() is not None
            or QtWidgets.QApplication.activeModalWidget() is not None
        ):
            return False
        modifier_mask = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        shortcut_modifiers = modifiers & modifier_mask
        smart_exit_modifiers = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
        )
        typed = event.text()
        if (
            self._armed_order_side
            and time.monotonic() >= self._armed_order_deadline
        ):
            self._expire_armed_order_sequence()
        if (
            shortcut_modifiers == smart_exit_modifiers
            and key == int(Qt.Key.Key_X)
        ):
            self._reset_armed_order_sequence()
            self._place_smart_exit()
            return True
        if shortcut_modifiers == Qt.KeyboardModifier.ShiftModifier and typed.isalpha():
            self._reset_armed_order_sequence()
            self.open_symbol_search(typed)
            return True
        if shortcut_modifiers != Qt.KeyboardModifier.NoModifier:
            self._reset_armed_order_sequence()
            return False
        if key in {int(Qt.Key.Key_Escape), int(Qt.Key.Key_Backspace)}:
            if self._armed_order_side:
                self._reset_armed_order_sequence()
                self.statusBar().showMessage("QUICK ORDER CANCELLED", 1200)
                return True
            return False
        letter = typed.upper()
        if letter in {"B", "S"}:
            if self._armed_order_side == letter:
                self._reset_armed_order_sequence()
                self._execute_armed_percentage_order(letter, 100)
            else:
                self._reset_armed_order_sequence()
                self._armed_order_side = letter
                self._armed_order_deadline = (
                    time.monotonic() + self.QUICK_ORDER_SEQUENCE_SECONDS
                )
                self._armed_order_timer.start()
                self.statusBar().showMessage(
                    f"QUICK {'BUY' if letter == 'B' else 'SELL'} · 1-9 = 10-90% · "
                    f"{letter} = 100% · {self.QUICK_ORDER_SEQUENCE_SECONDS:.1f}s",
                    2200,
                )
            return True
        if typed in set("0123456789"):
            if self._armed_order_side:
                side = self._armed_order_side
                self._reset_armed_order_sequence()
                if typed == "0":
                    self.statusBar().showMessage(
                        "QUICK ORDER IGNORED · USE 1-9 OR B/S", 2200
                    )
                else:
                    self._execute_armed_percentage_order(side, int(typed) * 10)
                return True
            # Quick trading being unlocked must not monopolize timeframe keys.
            # Once the B/S prefix has expired (or was never entered), let this
            # exact key event continue to the existing numeric timeframe handler.
            return False
        if typed.isalpha():
            self._reset_armed_order_sequence()
            return True
        return False

    def _finish_fps_benchmark(self, generation: int) -> None:
        if generation != getattr(self, "_fps_benchmark_generation", 0):
            return
        profile = self.presentation_clock.frame_profile()
        if not profile.get("completed"):
            QTimer.singleShot(250, self, lambda: self._finish_fps_benchmark(generation))
            return
        diagnostics = profile.get("diagnostics") or {}
        timings = diagnostics.get("timings") or {}
        counts = diagnostics.get("counts") or {}
        sums = diagnostics.get("sums") or {}
        lines = [
            (
                "[F3] "
                # Paint throughput alone can rise from continuous redraws. Keep
                # real frame intervals and request/event latency beside it.
                f"paint_rate={float(profile.get('paint_rate_fps', 0.0)):.1f}/s "
                f"frame_p95={float(profile.get('frame_p95_ms', 0.0)):.2f}ms "
                f"frame_p99={float(profile.get('frame_p99_ms', 0.0)):.2f}ms "
                f"event_p95={float(profile.get('event_loop_p95_ms', 0.0)):.2f}ms "
                f"event_p99={float(profile.get('event_loop_p99_ms', 0.0)):.2f}ms "
                f"event_max={float(profile.get('event_loop_max_ms', 0.0)):.2f}ms "
                f"paint_latency_p95={float(profile.get('paint_latency_p95_ms', 0.0)):.2f}ms "
                f"paint_latency_p99={float(profile.get('paint_latency_p99_ms', 0.0)):.2f}ms"
            )
        ]
        for name, values in sorted(timings.items()):
            lines.append(
                f"[F3 timing] {name}: n={int(values.get('count', 0))} "
                f"total={float(values.get('total_ms', 0.0)):.2f}ms "
                f"avg={float(values.get('avg_ms', 0.0)):.3f}ms "
                f"p95={float(values.get('p95_ms', 0.0)):.3f}ms "
                f"p99={float(values.get('p99_ms', 0.0)):.3f}ms "
                f"max={float(values.get('max_ms', 0.0)):.3f}ms"
            )
        for name, value in sorted(counts.items()):
            lines.append(f"[F3 count] {name}: {int(value)}")
        for name, value in sorted(sums.items()):
            lines.append(f"[F3 sum] {name}: {float(value):.0f}")
        charts = [self.chart]
        container = getattr(self, "chart_container", None)
        if container is not None:
            charts.extend(
                pane.chart for pane in tuple(getattr(container, "auxiliary", ()))
                if pane.isVisible() and pane.chart.isVisible()
            )
        for index, chart in enumerate(charts, 1):
            lines.append(f"[F3 GPU] chart={index} " + json.dumps(chart.diagnostic_state(), sort_keys=True))
        print("\n".join(lines), flush=True)
        self.statusBar().showMessage(lines[0], 12000)

    def _arm_current_order_rail_hotkey(self) -> bool:
        """Submit the current unarmed rail through its existing execution interface.

        Ctrl+A is arm-only: an already armed or pending rail is never toggled or
        mutated here. The rail's execution signal remains the sole entry point
        into the existing workspace / trading-gateway order path.
        """
        charts: list[ChartWorkspace] = [self.chart]
        container = getattr(self, "chart_container", None)
        if container is not None:
            for pane in tuple(getattr(container, "auxiliary", ())):
                chart = getattr(pane, "chart", None)
                if (
                    isinstance(chart, ChartWorkspace)
                    and pane.isVisible()
                    and chart.isVisible()
                ):
                    charts.append(chart)

        candidates: list[ChartWorkspace] = []
        for chart in charts:
            hud = getattr(chart, "order_rail_hud", None)
            if (
                hud is None
                or not hud.isVisible()
                or bool(getattr(hud, "armed", False))
                or bool(getattr(hud, "submission_pending", False))
                or bool(getattr(hud, "cancellation_pending", False))
                or chart.order_rail_raw_price() <= 0.0
            ):
                continue
            candidates.append(chart)

        if not candidates:
            return False

        cursor = QtGui.QCursor.pos()
        selected: ChartWorkspace | None = None
        for chart in candidates:
            viewport = chart.graphics.viewport()
            if viewport.rect().contains(viewport.mapFromGlobal(cursor)):
                selected = chart
                break
        if selected is None and len(candidates) == 1:
            selected = candidates[0]
        if selected is None:
            return False

        hud = selected.order_rail_hud
        if hud is None:
            return False

        # Start the exact armed contraction immediately on Ctrl+A, but keep the
        # semantic ``armed`` flag false until Binance confirms a working order.
        # The existing execution signal remains the only order-submission entry.
        hud.set_arm_submission_preview(True, animated=True)
        hud.execution_requested.emit(str(hud.side))

        # Direct validation/admission failures return synchronously without ever
        # becoming submission-pending. Reverse the optimistic visual immediately.
        if not hud.submission_pending and not hud.armed:
            hud.set_arm_submission_preview(False, animated=True)
        return True

    def _dismiss_visible_unarmed_order_rail(self) -> bool:
        """Dismiss the local rail draft for the chart under the pointer.

        This intentionally excludes armed/live rails and in-flight submissions.
        """
        charts: list[ChartWorkspace] = [self.chart]
        container = getattr(self, "chart_container", None)
        if container is not None:
            for pane in tuple(getattr(container, "auxiliary", ())):
                chart = getattr(pane, "chart", None)
                if (
                    isinstance(chart, ChartWorkspace)
                    and pane.isVisible()
                    and chart.isVisible()
                ):
                    charts.append(chart)

        candidates: list[ChartWorkspace] = []
        for chart in charts:
            hud = getattr(chart, "order_rail_hud", None)
            if (
                hud is None
                or not hud.isVisible()
                or bool(getattr(hud, "armed", False))
                or bool(getattr(hud, "submission_pending", False))
                or bool(getattr(hud, "cancellation_pending", False))
            ):
                continue
            candidates.append(chart)

        if not candidates:
            return False

        cursor = QtGui.QCursor.pos()
        for chart in candidates:
            viewport = chart.graphics.viewport()
            if viewport.rect().contains(viewport.mapFromGlobal(cursor)):
                return bool(chart.dismiss_unarmed_order_rail())

        # With exactly one visible draft there is no ambiguity even if focus is
        # currently on another non-text control.
        if len(candidates) == 1:
            return bool(candidates[0].dismiss_unarmed_order_rail())
        return False

    @staticmethod
    def _text_input_has_keyboard_focus(widget: QtWidgets.QWidget | None) -> bool:
        """Return whether a live, visible editor legitimately owns typing."""
        if not isinstance(
            widget,
            (
                QtWidgets.QLineEdit,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractSpinBox,
                QtWidgets.QComboBox,
            ),
        ):
            return False
        try:
            return bool(widget.isVisible() and widget.isEnabled() and widget.hasFocus())
        except RuntimeError:
            return False

    def _native_main_window_has_keyboard_foreground(self) -> bool:
        """Use the Windows foreground HWND only as a fallback for stale Qt state."""
        if os.name != "nt" or not self.isVisible():
            return False
        hwnd_value = self._windows_hwnd()
        if not hwnd_value:
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_foreground_window = user32.GetForegroundWindow
            get_foreground_window.argtypes = []
            get_foreground_window.restype = wintypes.HWND
            return int(get_foreground_window() or 0) == int(hwnd_value)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _owns_keyboard(self) -> bool:
        """Return whether terminal shortcuts genuinely belong to this window.

        QApplication.activeWindow() can transiently be None/stale around startup,
        fullscreen and owned-window transitions on Windows.  Do not drop the key
        when Qt's top-level bookkeeping lags the actual foreground HWND.
        """
        application = QtWidgets.QApplication.instance()
        if application is None or not self.isVisible():
            return False
        if (
            application.activeModalWidget() is not None
            or application.activePopupWidget() is not None
        ):
            return False

        native_foreground = self._native_main_window_has_keyboard_foreground()
        if (
            application.applicationState()
            != Qt.ApplicationState.ApplicationActive
            and not native_foreground
        ):
            return False
        if application.activeWindow() is self:
            return True

        focus_window = QtGui.QGuiApplication.focusWindow()
        window_handle = self.windowHandle()
        if window_handle is not None and focus_window is window_handle:
            return True
        return native_foreground

    def _restore_terminal_keyboard_focus(self) -> None:
        """Repair transient Qt focus loss without stealing focus from anything."""
        if self._closing or not self.isVisible():
            return
        application = QtWidgets.QApplication.instance()
        if application is None:
            return
        if (
            application.activeModalWidget() is not None
            or application.activePopupWidget() is not None
        ):
            return

        native_foreground = self._native_main_window_has_keyboard_foreground()
        if (
            application.applicationState()
            != Qt.ApplicationState.ApplicationActive
            and not native_foreground
        ):
            return

        active_window = application.activeWindow()
        focus_window = QtGui.QGuiApplication.focusWindow()
        window_handle = self.windowHandle()
        qt_owns_foreground = (
            active_window is self
            or (window_handle is not None and focus_window is window_handle)
        )
        if not qt_owns_foreground and not native_foreground:
            # A different visible Nightwatch top-level is allowed to keep focus.
            # This also prevents a close/hide callback from stealing focus from a
            # second dialog that is still open.
            if active_window is not None:
                return
            if any(
                window is not self and window.isWindow() and window.isVisible()
                for window in application.topLevelWidgets()
            ):
                return
            self.activateWindow()

        focus_widget = application.focusWidget()
        if self._text_input_has_keyboard_focus(
            focus_widget if isinstance(focus_widget, QtWidgets.QWidget) else None
        ):
            return
        if isinstance(focus_widget, QtWidgets.QWidget):
            try:
                if (
                    focus_widget.window() is self
                    and focus_widget.isVisible()
                    and focus_widget.isEnabled()
                    and focus_widget.hasFocus()
                ):
                    return
            except RuntimeError:
                pass

        chart = getattr(self, "chart", None)
        graphics = getattr(chart, "graphics", None)
        viewport = graphics.viewport() if graphics is not None else None
        if isinstance(viewport, QtWidgets.QWidget):
            try:
                if viewport.isVisible() and viewport.isEnabled():
                    viewport.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            except RuntimeError:
                pass

    def _sync_terminal_keyboard_activation(self) -> None:
        """Restore focus on activation; cancel quick-order prefixes on true loss."""
        application = QtWidgets.QApplication.instance()
        if application is None:
            return
        if (
            application.applicationState() == Qt.ApplicationState.ApplicationActive
            or self._native_main_window_has_keyboard_foreground()
        ):
            self._restore_terminal_keyboard_focus()
        else:
            self._reset_armed_order_sequence()

    def _handle_escape(self) -> bool:
        # Popup and auxiliary-window ownership is resolved before this path.
        if self._armed_order_side:
            self._reset_armed_order_sequence()
            self.statusBar().showMessage("QUICK ORDER CANCELLED", 1200)
            return True
        charts = [self.chart] + [pane.chart for pane in self.chart_container.auxiliary]
        for chart in charts:
            if not chart.isVisible():
                continue
            if chart._order_rail_dragging:
                chart._cancel_order_rail_drag()
                return True
            if chart.order_rail_placement_mode:
                chart.set_order_rail_placement(False)
                return True
            if chart.drawing_mode is not None:
                chart.cancel_drawing()
                return True
        if self._effective_fullscreen():
            self._exit_fullscreen()
            return True
        return False

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        event_type = event.type()
        if (
            event_type == QtCore.QEvent.Type.Show
            and os.name == "nt"
            and self._windows_borderless_fullscreen
            and isinstance(watched, QtWidgets.QWidget)
            and watched.windowType() == Qt.WindowType.Popup
            and self._popup_belongs_to_terminal(watched)
        ):
            # Qt finishes native popup creation after Show. Keep its own HWND
            # above fullscreen without moving or activating the chart window.
            QTimer.singleShot(
                0, watched,
                lambda popup=watched: self._raise_windows_fullscreen_popup(popup),
            )
        elif event_type == QtCore.QEvent.Type.ApplicationStateChange:
            # QApplication updates applicationState as this event completes.
            QTimer.singleShot(0, self, self._sync_terminal_keyboard_activation)
            if os.name == "nt":
                # Owned dialogs inherit their owner's z-order on Windows. Only
                # whole-app activation should change the fullscreen HWND's band.
                QTimer.singleShot(0, self, self._sync_windows_fullscreen_native_stack)
        elif event_type == QtCore.QEvent.Type.WindowActivate and watched is self:
            QTimer.singleShot(0, self, self._restore_terminal_keyboard_focus)
        elif (
            event_type in (QtCore.QEvent.Type.Close, QtCore.QEvent.Type.Hide)
            and isinstance(watched, QtWidgets.QWidget)
            and watched is not self
            and watched.isWindow()
        ):
            # Closing an owned dialog can leave QApplication.activeWindow() stale
            # or empty until the next mouse click. Repair that handoff after Qt
            # completes the close/hide event; the helper refuses to steal focus
            # from another visible dialog or another application.
            QTimer.singleShot(0, self, self._restore_terminal_keyboard_focus)

        if event_type in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride):
            if not self._owns_keyboard():
                self._reset_armed_order_sequence()
                return super().eventFilter(watched, event)
            if (
                event.key() == Qt.Key.Key_Escape
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
            ):
                if event_type == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if not event.isAutoRepeat() and self._handle_escape():
                    return True
        elif event_type == QtCore.QEvent.Type.Shortcut:
            if isinstance(watched, QtGui.QAction) and watched.parent() is self and not self._owns_keyboard():
                return True
        if event_type == QtCore.QEvent.Type.ToolTip and not tooltips_allowed(watched):
            QtWidgets.QToolTip.hideText()
            return True
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.isAutoRepeat()
            and self.trading_gateway.armed
        ):
            focus = QtWidgets.QApplication.focusWidget()
            text_input = self._text_input_has_keyboard_focus(
                focus if isinstance(focus, QtWidgets.QWidget) else None
            )
            if not text_input and event.text().upper() in set("BS0123456789"):
                return True
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and self.isVisible()
            and not event.isAutoRepeat()
        ):
            key = int(event.key())
            modifiers = event.modifiers()
            focus = QtWidgets.QApplication.focusWidget()
            text_input = self._text_input_has_keyboard_focus(
                focus if isinstance(focus, QtWidgets.QWidget) else None
            )
            if (
                not text_input
                and self.symbol_search_dialog is None
                and modifiers == Qt.KeyboardModifier.NoModifier
                and key in {int(Qt.Key.Key_Delete), int(Qt.Key.Key_Backspace)}
                and self._dismiss_visible_unarmed_order_rail()
            ):
                self.statusBar().showMessage("ORDER RAIL REMOVED", 1800)
                return True
            if key == int(Qt.Key.Key_F3) and modifiers == Qt.KeyboardModifier.NoModifier:
                self.presentation_clock.start_frame_profile(30.0)
                self._fps_benchmark_generation = getattr(self, "_fps_benchmark_generation", 0) + 1
                generation = self._fps_benchmark_generation
                QTimer.singleShot(30250, self, lambda: self._finish_fps_benchmark(generation))
                self.statusBar().showMessage("30s FPS benchmark started", 1800)
                return True
            if (modifiers == Qt.KeyboardModifier.AltModifier
                    and int(Qt.Key.Key_1) <= key <= int(Qt.Key.Key_4)):
                self._switch_workspace(key-int(Qt.Key.Key_1))
                return True
            if (
                modifiers & Qt.KeyboardModifier.ControlModifier
                and key in {int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab)}
            ):
                reverse = (
                    key == int(Qt.Key.Key_Backtab)
                    or bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                )
                step = -1 if reverse else 1
                self._switch_workspace(
                    (self.workspace_stack.currentIndex() + step)
                    % self.workspace_stack.count()
                )
                return True
            focus = QtWidgets.QApplication.focusWidget()
            text_input = isinstance(
                focus,
                (
                    QtWidgets.QLineEdit,
                    QtWidgets.QTextEdit,
                    QtWidgets.QPlainTextEdit,
                    QtWidgets.QAbstractSpinBox,
                    QtWidgets.QComboBox,
                ),
            )
            modifier_mask = (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
            shortcut_modifiers = modifiers & modifier_mask
            focused_ticket = self._ticket_for_widget(
                focus if isinstance(focus, QtWidgets.QWidget) else None
            )
            if (
                focused_ticket is not None
                and shortcut_modifiers == Qt.KeyboardModifier.ControlModifier
                and key in {int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter)}
            ):
                focused_ticket.prepare_order()
                return True
            typed = event.text()
            if (
                focused_ticket is not None
                and not text_input
                and not self.trading_gateway.armed
                and shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                and (typed.isdigit() or typed == ".")
            ):
                focused_ticket.quantity_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
                focused_ticket.quantity_edit.insert(typed)
                return True
            if (
                not text_input
                and self.symbol_search_dialog is None
                and shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                and key in {int(Qt.Key.Key_BracketLeft), int(Qt.Key.Key_BracketRight)}
                and hasattr(self, "orderbook")
                and self.orderbook.isVisible()
            ):
                self._adjust_orderbook_aggregation(-1 if key == int(Qt.Key.Key_BracketLeft) else 1)
                return True
            if (
                not text_input
                and self.symbol_search_dialog is None
                and self.workspace_stack.currentIndex() == 0
                and shortcut_modifiers == Qt.KeyboardModifier.ControlModifier
                and key == int(Qt.Key.Key_A)
            ):
                # Arm-only rail hotkey. Do not fall through to indicator or other
                # Ctrl+A handling even when the current rail is already armed.
                # One physical keypress may submit at most one order.
                if not event.isAutoRepeat():
                    self._arm_current_order_rail_hotkey()
                return True
            if (
                not text_input
                and shortcut_modifiers == Qt.KeyboardModifier.ControlModifier
                and key == int(Qt.Key.Key_P)
            ):
                if not event.isAutoRepeat():
                    self._cycle_quick_panel_layout()
                return True
            if (
                not text_input
                and shortcut_modifiers == Qt.KeyboardModifier.ControlModifier
                and key in {int(Qt.Key.Key_PageUp), int(Qt.Key.Key_PageDown)}
            ):
                self._cycle_right_layout(
                    -1 if key == int(Qt.Key.Key_PageUp) else 1
                )
                return True
            if (not text_input and self.symbol_search_dialog is None
                    and self.workspace_stack.currentIndex() == 0):
                sequence = QtGui.QKeySequence(event.keyCombination()).toString(QtGui.QKeySequence.SequenceFormat.PortableText)
                name = next((name for name, shortcut in self.indicator_shortcuts.items()
                             if shortcut and shortcut == sequence), None)
                if name:
                    if not event.isAutoRepeat():
                        self.indicator_actions[name].trigger()
                    return True
                if sequence == "Ctrl+Shift+O":
                    if not event.isAutoRepeat():
                        self._toggle_chart_orders()
                    return True
            if not text_input and self.symbol_search_dialog is None:
                if (
                    key == int(Qt.Key.Key_Delete)
                    and shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                    and self.chart.delete_selected_drawing()
                ):
                    self._store_current_symbol_drawings()
                    self.statusBar().showMessage("DRAWING DELETED", 1800)
                    return True
            if (
                not text_input
                and key == int(Qt.Key.Key_O)
                and shortcut_modifiers == Qt.KeyboardModifier.ControlModifier
                and not event.isAutoRepeat()
                and self._toggle_trading_account_view()
            ):
                return True
            if (
                not text_input
                and self.symbol_search_dialog is None
                and shortcut_modifiers
                == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
                and key == int(Qt.Key.Key_A)
            ):
                self._toggle_quick_order_lock()
                return True
            if self._handle_armed_trading_key(
                event, key, modifiers, text_input
            ):
                return True
            if (
                not text_input
                and key == int(Qt.Key.Key_F)
                and shortcut_modifiers
                in (
                    Qt.KeyboardModifier.AltModifier,
                    Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.ShiftModifier,
                )
            ):
                self._cycle_auto_fibonacci(
                    -1
                    if shortcut_modifiers
                    & Qt.KeyboardModifier.ShiftModifier
                    else 1
                )
                return True
            if (
                not text_input
                and self.workspace_stack.currentIndex() == 0
                and modifiers == Qt.KeyboardModifier.ControlModifier
                and key == int(Qt.Key.Key_Z)
            ):
                overview = self.chart.toggle_overview()
                self.statusBar().showMessage(
                    "CHART OVERVIEW" if overview else "CHART VIEW RESTORED",
                    1800,
                )
                return True
            if not text_input and self.symbol_search_dialog is None:
                pressed_sequence = QtGui.QKeySequence(
                    event.keyCombination()
                ).toString(QtGui.QKeySequence.SequenceFormat.PortableText)
                for action, configured in self.trading_hotkeys.items():
                    if (
                        configured
                        and configured not in set("0123456789")
                        and pressed_sequence == configured
                    ):
                        # Bare B/S are deliberately lower priority than symbol
                        # search while quick orders are locked. Persisted legacy
                        # bindings must not consume normal symbol typing.
                        if (
                            pressed_sequence.upper() in LOW_PRIORITY_TRADING_HOTKEYS
                            and not self.trading_gateway.armed
                        ):
                            continue
                        if event.isAutoRepeat():
                            return True
                        self._execute_trading_hotkey(action)
                        return True
            if (
                not text_input
                and self.symbol_search_dialog is None
                and shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                and key in {int(Qt.Key.Key_Up), int(Qt.Key.Key_Down)}
            ):
                self._cycle_watchlist_symbol(
                    -1 if key == int(Qt.Key.Key_Up) else 1
                )
                return True
            if (
                not text_input
                and self.symbol_search_dialog is None
                and self.workspace_stack.currentIndex() == 0
                and shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                and key in {int(Qt.Key.Key_Left), int(Qt.Key.Key_Right)}
                and self.market_bar_timeframes
            ):
                step = -1 if key == int(Qt.Key.Key_Left) else 1
                timeframes = self.market_bar_timeframes
                index = (
                    timeframes.index(self.current_interval)
                    if self.current_interval in timeframes
                    else (-1 if step > 0 else 0)
                )
                timeframe = timeframes[(index + step) % len(timeframes)]
                self.switch_interval(timeframe)
                timeframe_label = timeframe.upper() if timeframe in {"1d", "1w"} else timeframe
                self.statusBar().showMessage(f"TIMEFRAME · {timeframe_label}", 1800)
                return True
            if (
                key == int(Qt.Key.Key_Delete)
                and not text_input
                and shortcut_modifiers == Qt.KeyboardModifier.ShiftModifier
            ):
                self.chart.clear_drawings()
                self._store_current_symbol_drawings()
                self.statusBar().showMessage("Chart drawings cleared.", 1800)
                return True
            first_key = int(Qt.Key.Key_1)
            if (
                shortcut_modifiers == Qt.KeyboardModifier.NoModifier
                and not text_input
                and self.symbol_search_dialog is None
                and self.workspace_stack.currentIndex() == 0
                and first_key <= key < first_key + len(self.market_bar_timeframes)
            ):
                timeframe = self.market_bar_timeframes[key - first_key]
                self.switch_interval(timeframe)
                timeframe_label = (
                    timeframe.upper() if timeframe in {"1d", "1w"} else timeframe
                )
                self.statusBar().showMessage(
                    f"TIMEFRAME · {timeframe_label}",
                    1800,
                )
                return True
            blocked_modifiers = (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
            typed = event.text()
            if (
                not text_input
                and typed
                and typed.isalnum()
                and not event.modifiers() & blocked_modifiers
                and QtWidgets.QApplication.activePopupWidget() is None
                and self.symbol_search_dialog is None
                and not self.symbol_search_pending
                and not self.trading_gateway.armed
            ):
                self.open_symbol_search(typed)
                return True
        return super().eventFilter(watched, event)

    def _set_ticker_sort(self, mode: str) -> None:
        if mode not in MARKET_SORT_MODES:
            return
        self.ticker_sort_mode = mode
        if (
            self.symbol_search_dialog is not None
            and self.symbol_search_dialog.sort_mode != mode
        ):
            self.symbol_search_dialog.set_sort_mode(mode)

    def edit_market_filter(self) -> None:
        if self.market_volume_task is not None:
            self.statusBar().showMessage("The rolling-volume snapshot is still loading.", 4000)
            return
        dialog = MarketFilterDialog(
            self.market_filter_timeframe,
            self.market_filter_min_volume,
            self.symbol_search_dialog or self,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        timeframe, minimum = dialog.options()
        if minimum <= 0:
            self._apply_market_filter("24h", 0.0, self.ticker_sort_mode)
            return
        if timeframe == "24h":
            self._apply_market_filter(timeframe, minimum, self.ticker_sort_mode)
            return
        cached = self.market_volume_cache.get(timeframe)
        if cached and time.monotonic() - cached[0] < MARKET_VOLUME_CACHE_SECONDS:
            self._apply_market_filter(timeframe, minimum, self.ticker_sort_mode)
            return
        self.pending_market_filter = (timeframe, minimum, self.ticker_sort_mode)
        self._request_market_volumes(timeframe)

    def _request_market_volumes(self, timeframe: str) -> None:
        if self.market_volume_task is not None:
            return
        symbols = sorted(self.valid_symbols)
        if not symbols:
            self.pending_market_filter = None
            self.statusBar().showMessage("Markets are still loading. Try the filter again in a moment.", 5000)
            return
        hub = self._ensure_market_data_hub()
        hours = 1 if timeframe == "1h" else 4
        if self.symbol_search_dialog is not None:
            self.symbol_search_dialog.set_filter_loading(0)
        self.statusBar().showMessage(f"Loading accurate {timeframe} rolling volumes…")
        task: ApiTask

        def load() -> dict[str, dict[str, float]]:
            return hub.rest.market_interval_volumes(
                symbols,
                hours,
                task.signals.progress.emit,
            )

        def loaded(values: dict[str, dict[str, float]]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.market_volume_task = None
            self.market_volume_cache[timeframe] = (time.monotonic(), values)
            pending = self.pending_market_filter
            self.pending_market_filter = None
            if pending and pending[0] == timeframe:
                self._apply_market_filter(*pending)
            else:
                self._update_filter_button()

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.market_volume_task = None
            self.pending_market_filter = None
            self._update_filter_button()
            self.statusBar().showMessage(f"Could not load rolling volumes: {message}", 9000)

        task = ApiTask(load)
        task.signals.progress.connect(self._market_volume_progress)
        task.signals.finished.connect(loaded)
        task.signals.failed.connect(failed)
        self.market_volume_task = task
        self.tasks.add(task)
        QtCore.QThreadPool.globalInstance().start(task)

    def _market_volume_progress(self, completed: int, total: int) -> None:
        if self._closing or self.market_volume_task is None or total <= 0:
            return
        if self.symbol_search_dialog is not None:
            self.symbol_search_dialog.set_filter_loading(
                min(99, int(completed * 100 / total))
            )

    def _apply_market_filter(self, timeframe: str, minimum: float, sort_mode: str) -> None:
        self.market_filter_timeframe = timeframe
        self.market_filter_min_volume = max(0.0, minimum)
        self.ticker_sort_mode = sort_mode
        self._refresh_ticker_market_model()
        self._update_filter_button()
        if self.market_filter_min_volume > 0:
            shown = len(self._filtered_market_universe()[0])
            self.statusBar().showMessage(
                f"Market filter active · {timeframe} volume ≥ {human_number(minimum, money=True)} · {shown} pairs.",
                7000,
            )
        else:
            self.statusBar().showMessage("Market filter cleared.", 4000)

    def _market_volume_values(self) -> dict[str, float] | None:
        if self.market_filter_timeframe == "24h":
            return None
        cached = self.market_volume_cache.get(self.market_filter_timeframe)
        if not cached:
            return {}
        return {
            symbol: safe_float(values.get("quote"))
            for symbol, values in cached[1].items()
        }

    def _filtered_market_universe(self) -> tuple[list[str], dict[str, float] | None]:
        # Search is sourced only from the canonical exchangeInfo-filtered universe.
        # Falling back to the raw all-market ticker map can leak delivery,
        # USDC, and TradFi contracts such as BTCUSDT_YYMMDD into search.
        all_symbols = sorted(self.valid_symbols)
        if not all_symbols:
            all_symbols = [
                self.current_symbol
            ] if (
                self.current_symbol.endswith("USDT")
                and "_" not in self.current_symbol
            ) else []
        volume_values = self._market_volume_values()
        if self.market_filter_min_volume > 0:
            if volume_values is None:
                visible = [
                    symbol
                    for symbol in all_symbols
                    if safe_float(self.tickers.get(symbol, {}).get("q"))
                    >= self.market_filter_min_volume
                ]
            else:
                visible = [
                    symbol
                    for symbol in all_symbols
                    if safe_float(volume_values.get(symbol)) >= self.market_filter_min_volume
                ]
        else:
            visible = all_symbols
        return visible, volume_values

    def _refresh_ticker_market_model(self) -> None:
        if self.symbol_search_dialog is None:
            return
        visible, volume_values = self._filtered_market_universe()
        self.symbol_search_dialog.set_market_data(
            visible,
            self.tickers,
            self.ticker_sort_mode,
            self.market_filter_timeframe,
            self.market_filter_min_volume,
            volume_values,
        )
        self._refresh_search_hour_changes(restart=True)

    def _update_filter_button(self) -> None:
        if self.symbol_search_dialog is None:
            return
        volume_values = self._market_volume_values()
        self.symbol_search_dialog.set_filter_loading(None)
        self.symbol_search_dialog.set_filter_state(
            self.market_filter_timeframe,
            self.market_filter_min_volume,
            volume_values,
        )

    def _reset_top_metrics(self) -> None:
        for key, title in (
            ("funding", "FUNDING"),
            ("oi", "OPEN INTEREST"),
            ("volume", "ROLLING VOLUME"),
        ):
            self.stats.cards[key].set_detail(
                f"{perpetual_display_symbol(self.current_symbol)} · {title}",
                [("Status", "Waiting for market data…")],
            )

    def _reset_microstructure_card(self, *, preserve_render: bool = False) -> None:
        if self.microstructure_card.isHidden():
            return
        self.last_microstructure_signal_id = 0
        self.recent_microstructure_signals.clear()
        if preserve_render:
            # Symbol changes must not jump/restart the currently scrolling news
            # item or idle logo. Old queued signals are discarded so only the
            # item already on-screen is allowed to finish.
            self.microstructure_card.discard_pending()
        else:
            self.microstructure_card.clear_signal()
        self.microstructure_card.set_detail_html(
            "No high-confidence short-term signal has triggered for this symbol."
        )

    def _update_microstructure_card(self, snapshot: MicrostructureSnapshot) -> None:
        if snapshot.symbol != self.current_symbol:
            return
        # The order book is the price-aligned consumer of microstructure events.
        # Do not mutate the sleeping DOM after its transport has been disabled.
        if self._market_depth_active and self.right_rail_controller.panel_active("depth"):
            self.orderbook.set_microstructure_snapshot(snapshot)
        if self.microstructure_card.isHidden():
            return
        if (
            not snapshot.signal_sentence
            or snapshot.signal_id <= self.last_microstructure_signal_id
        ):
            return
        self.last_microstructure_signal_id = snapshot.signal_id
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.recent_microstructure_signals.appendleft((stamp, snapshot))
        bullish = snapshot.signal_key in {"BID ABSORPTION", "BULLISH BREAKOUT"}
        color = self.ui_theme["green" if bullish else "red"]
        self.microstructure_card.show_signal(snapshot.signal_sentence, color)
        rows = (
            ("Signal", snapshot.signal_sentence),
            ("Market buys · 5s", human_number(snapshot.buy_notional, money=True)),
            ("Market sells · 5s", human_number(snapshot.sell_notional, money=True)),
            ("Buy vs sell", f"{snapshot.imbalance_pct:+.1f}%"),
            ("Activity", f"{snapshot.flow_intensity:.2f}× usual"),
            ("Price move · 5s", f"{snapshot.price_change_bps / 100.0:+.3f}%"),
            ("Bid absorption", f"{snapshot.bid_absorption}/100"),
            ("Ask absorption", f"{snapshot.ask_absorption}/100"),
            ("Bid refill", f"{snapshot.bid_replenishment_pct:.0f}%"),
            ("Ask refill", f"{snapshot.ask_replenishment_pct:.0f}%"),
            ("Bid depth", f"{snapshot.bid_depth_change_pct:+.1f}%"),
            ("Ask depth", f"{snapshot.ask_depth_change_pct:+.1f}%"),
        )
        detail = "".join(
            f"<tr><td>{html.escape(label)}</td><td>&nbsp;&nbsp;{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        self.microstructure_card.set_detail_html(
            f'<b style="color:{color}">{html.escape(stamp)} · '
            f'{html.escape(snapshot.signal_key)}</b><table>{detail}</table>'
        )

    def _open_microstructure_signals(self) -> None:
        menu = QtWidgets.QMenu(self.microstructure_card)
        if not self.recent_microstructure_signals:
            empty = menu.addAction("No strong short-term signal has triggered for this symbol")
            empty.setEnabled(False)
        else:
            for stamp, snapshot in self.recent_microstructure_signals:
                bullish = snapshot.signal_key in {"BID ABSORPTION", "BULLISH BREAKOUT"}
                color = self.ui_theme["green" if bullish else "red"]
                action = QtWidgets.QWidgetAction(menu)
                row = QtWidgets.QPushButton(
                    f"{stamp} · {snapshot.signal_sentence}",
                    menu,
                )
                row.setCursor(Qt.CursorShape.PointingHandCursor)
                row.setToolTip("Open the execution, price-response and depth evidence")
                row.setStyleSheet(
                    "QPushButton {"
                    "border: 0; background: transparent; text-align: left;"
                    f"color: {color}; padding: 7px 12px;"
                    "}"
                    f"QPushButton:hover {{ background: {self.ui_theme['control_hover']}; }}"
                )

                def open_signal(
                    _checked: bool = False,
                    time_value: str = stamp,
                    value: MicrostructureSnapshot = snapshot,
                ) -> None:
                    menu.close()
                    self._show_microstructure_signal(time_value, value)

                row.clicked.connect(open_signal)
                action.setDefaultWidget(row)
                menu.addAction(action)
        anchor_below = self.microstructure_card.mapToGlobal(
            QtCore.QPoint(0, self.microstructure_card.height())
        )
        menu.ensurePolished()
        menu_size = menu.sizeHint()
        screen = QtGui.QGuiApplication.screenAt(anchor_below) or self.screen()
        target = QtCore.QPoint(anchor_below)
        if screen is not None:
            available = screen.availableGeometry()
            if target.y() + menu_size.height() > available.bottom() + 1:
                anchor_above = self.microstructure_card.mapToGlobal(QtCore.QPoint(0, 0))
                target.setY(anchor_above.y() - menu_size.height())
            target.setX(
                max(
                    available.left(),
                    min(target.x(), available.right() - menu_size.width() + 1),
                )
            )
            target.setY(
                max(
                    available.top(),
                    min(target.y(), available.bottom() - menu_size.height() + 1),
                )
            )
        menu.exec(target)

    def _show_microstructure_signal(
        self,
        stamp: str,
        snapshot: MicrostructureSnapshot,
    ) -> None:
        bullish = snapshot.signal_key in {"BID ABSORPTION", "BULLISH BREAKOUT"}
        direction = "BULLISH" if bullish else "BEARISH"
        color = self.ui_theme["green" if bullish else "red"]
        rows = (
            ("Market buys · 5s", human_number(snapshot.buy_notional, money=True)),
            ("Market sells · 5s", human_number(snapshot.sell_notional, money=True)),
            ("Buy vs sell difference", f"{snapshot.imbalance_pct:+.1f}%"),
            ("Direction", direction),
            ("Activity vs usual", f"{snapshot.flow_intensity:.2f}×"),
            ("Buyers holding", f"{snapshot.bid_absorption}/100"),
            ("Sellers holding", f"{snapshot.ask_absorption}/100"),
            ("Buy orders refilled", f"{snapshot.bid_replenishment_pct:.0f}%"),
            ("Sell orders refilled", f"{snapshot.ask_replenishment_pct:.0f}%"),
            ("Buy pressure", f"{snapshot.bullish_depletion}/100"),
            ("Sell pressure", f"{snapshot.bearish_depletion}/100"),
            ("Nearby buy orders", f"{snapshot.bid_depth_change_pct:+.1f}%"),
            ("Nearby sell orders", f"{snapshot.ask_depth_change_pct:+.1f}%"),
            ("Price move · 5s", f"{snapshot.price_change_bps / 100.0:+.3f}%"),
        )
        detail = "\n".join(f"{label}: {value}" for label, value in rows)
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(f"Short-term trading signal · {stamp}")
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
        dialog.setTextFormat(Qt.TextFormat.RichText)
        dialog.setText(
            f'<span style="color:{color};">{html.escape(snapshot.signal_sentence)}</span>'
        )
        dialog.setInformativeText(detail)
        dialog.exec()

    def _request_top_metrics_sync(self, *, immediate: bool = False) -> None:
        """Coalesce expensive metric-card detail/history rebuilds off hot market ingress."""
        self._top_metrics_dirty = True
        if self.workspace_stack.currentIndex() != 0:
            return
        if self._chart_interaction_priority_active or self._ui_resize_active:
            return
        if immediate:
            self._top_metrics_timer.stop()
            self._top_metrics_timer.start(0)
            return
        if not self._top_metrics_timer.isActive():
            self._top_metrics_timer.start(2000)

    @profile_callback("app.flush_top_metrics_if_idle_ms")
    def _flush_top_metrics_if_idle(self) -> None:
        if not self._top_metrics_dirty or self.workspace_stack.currentIndex() != 0:
            return
        if self._chart_interaction_priority_active or self._ui_resize_active:
            return
        self._top_metrics_dirty = False
        self._sync_top_metrics()

    def _sync_top_metrics(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        ticker = self.tickers.get(self.current_symbol, {})
        volume = safe_float(ticker.get("q"))
        mark_payload = self.stats.last_mark_payload
        interest = self.stats.last_interest_payload
        reference = self.stats.last_interest_reference
        open_interest = safe_float(interest.get("openInterest")) * reference


        premium = self.market_detail.get("premium") or {}
        funding_history = list(self.market_detail.get("funding_history") or [])
        funding_rows: list[tuple[str, str]] = []
        has_funding = bool(mark_payload or premium)
        current_rate = safe_float(
            mark_payload.get("r") if mark_payload else premium.get("lastFundingRate")
        ) * 100.0
        funding_rows.append(("Current rate", f"{current_rate:+.4f}%" if has_funding else "—"))
        next_funding = safe_float(
            mark_payload.get("T") if mark_payload else premium.get("nextFundingTime")
        ) / 1000.0
        if next_funding:
            remaining = max(0, int(next_funding - time.time()))
            funding_rows.append(
                ("Next funding", f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}:{remaining % 60:02d}")
            )
        mark_price = safe_float(mark_payload.get("p") or premium.get("markPrice"))
        index_price = safe_float(mark_payload.get("i") or premium.get("indexPrice"))
        if mark_price:
            funding_rows.append(("Mark price", format_price(mark_price)))
        if index_price:
            funding_rows.append(("Index price", format_price(index_price)))
        if index_price and mark_price:
            funding_rows.append(("Mark basis", f"{(mark_price / index_price - 1.0) * 100:+.4f}%"))
        for item in reversed(funding_history[-8:]):
            timestamp = safe_float(item.get("fundingTime")) / 1000.0
            label = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %H:%M UTC") if timestamp else "Past rate"
            funding_rows.append((label, f"{safe_float(item.get('fundingRate')) * 100:+.4f}%"))
        self.stats.cards["funding"].set_detail(
            f"{perpetual_display_symbol(self.current_symbol)} · FUNDING",
            funding_rows,
        )
        self.stats.cards["funding"].set_history(
            [(row.get("fundingTime"), safe_float(row.get("fundingRate")) * 100) for row in funding_history[-30:]],
            "Last 30 settled funding rates · % per settlement", percent=True, bars=True,
        )

        oi_history = list(self.market_detail.get("interest_detail") or [])
        oi_rows = [("Current OI", human_number(open_interest, money=True) if open_interest else "—")]
        for minutes, label in ((5, "5m change"), (60, "1h change"), (240, "4h change")):
            change_value = self._history_change(oi_history, open_interest, minutes)
            oi_rows.append((label, f"{change_value:+.2f}%" if change_value is not None else "—"))
        self.stats.update_taker_volume(self.market_detail.get("taker_volume"))
        long_short_rows = list(self.market_detail.get("long_short") or [])
        self.stats.update_long_short(
            long_short_rows,
            self.market_detail.get("top_long_short_accounts"),
            self.market_detail.get("top_long_short_positions"),
            requires_key=bool(self.market_detail.get("top_trader_requires_key", False)),
        )
        if long_short_rows:
            ratio = long_short_rows[-1]
            oi_rows.extend(
                (
                    ("Long / short", f"{safe_float(ratio.get('longShortRatio')):.3f}"),
                    ("Long accounts", f"{safe_float(ratio.get('longAccount')) * 100:.1f}%"),
                    ("Short accounts", f"{safe_float(ratio.get('shortAccount')) * 100:.1f}%"),
                )
            )
        self.stats.cards["oi"].set_detail(
            f"{perpetual_display_symbol(self.current_symbol)} · OPEN INTEREST",
            oi_rows,
        )
        self.stats.cards["oi"].set_history(
            [(row.get("timestamp"), row.get("sumOpenInterestValue")) for row in oi_history],
            "Open interest · USD value · 5-minute samples",
        )

        volume_source = self.market_detail.get("volume_detail") or []
        volume_rows = self._rolling_volume_detail(volume_source)
        volume_rows["24h"] = {
            "base": safe_float(ticker.get("v")),
            "quote": volume,
        }
        detail_rows: list[tuple[str, str]] = []
        for label in ("1h", "4h", "24h"):
            values = volume_rows.get(label, {})
            has_values = bool(volume_source) if label != "24h" else bool(ticker)
            detail_rows.append((f"{label} USDT", human_number(safe_float(values.get("quote")), money=True) if has_values else "—"))
            detail_rows.append((f"{label} base", human_number(safe_float(values.get("base"))) if has_values else "—"))
        self.stats.cards["volume"].set_detail(
            f"{perpetual_display_symbol(self.current_symbol)} · ROLLING VOLUME",
            detail_rows,
        )
        self.stats.cards["volume"].set_history(
            [(row[0], row[7]) for row in volume_source if len(row) > 7 and safe_float(row[6]) < time.time() * 1000][-60:],
            "Last 60 completed minutes · USDT volume per minute", bars=True,
        )
        if profile_started:
            record_performance_timing(
                "ticker.top_metrics_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )

    @staticmethod
    def _history_change(
        rows: list[dict[str, Any]],
        current_value: float,
        minutes: int,
    ) -> float | None:
        if not rows or current_value <= 0:
            return None
        target = max(safe_float(row.get("timestamp")) for row in rows) - minutes * 60_000
        reference = min(
            rows,
            key=lambda row: abs(safe_float(row.get("timestamp")) - target),
        )
        previous = safe_float(reference.get("sumOpenInterestValue"))
        if previous <= 0:
            return None
        return (current_value / previous - 1.0) * 100.0

    @staticmethod
    def _rolling_volume_detail(rows: list[list[Any]]) -> dict[str, dict[str, float]]:
        now_ms = int(time.time() * 1000)
        output: dict[str, dict[str, float]] = {}
        for hours, label in ((1, "1h"), (4, "4h"), (24, "24h")):
            cutoff = now_ms - hours * 3_600_000
            recent = [row for row in rows if len(row) > 7 and int(row[0]) >= cutoff]
            output[label] = {
                "base": sum(safe_float(row[5]) for row in recent),
                "quote": sum(safe_float(row[7]) for row in recent),
            }
        return output


    def switch_symbol(self, symbol: str) -> None:
        self._reset_armed_order_sequence()
        symbol = self._normalize_symbol(symbol)
        if not symbol:
            return
        if symbol == self.current_symbol:
            return
        if self.valid_symbols and symbol not in self.valid_symbols:
            return
        self._store_current_symbol_drawings()
        self._warm_initial_chart_history_pending = False
        self.current_symbol = symbol
        self._market_data_live = False
        self._book_valid = False
        self.best_bid = 0.0
        self.best_ask = 0.0
        self._reset_order_flow_runtime(
            tick_size=self.symbol_rules.get(symbol, SymbolRules()).tick_size,
            quote_volume=safe_float((self.tickers.get(symbol) or {}).get("q")),
        )
        self._reset_microstructure_card(preserve_render=True)
        self.watchlist.set_current(symbol)
        rules = self.symbol_rules.get(symbol, SymbolRules())
        self.chart.set_order_rail_market_symbol(symbol)
        self.chart.set_symbol_rules(rules)
        self.trading_workspace.set_symbol(symbol, rules)
        if self.trading_gateway.has_credentials():
            self.trading_gateway.ensure_cross(symbol)
        self.alert_center.set_market(symbol, self.current_interval)
        self.market_detail = {}
        self.stats.set_symbol(symbol)
        self.stats.reset()
        self._reset_top_metrics()
        self.last_price = 0.0
        self.orderbook.set_symbol(symbol, rules)
        self.orderbook.reset()
        self.last_recorded_depth = 0.0
        self.last_recorded_funding = 0.0
        # A market switch invalidates the current lazy-history request.  The
        # worker may finish in the background, but its generation/symbol/interval
        # guards prevent stale application while the new market can request its
        # own history immediately.
        self.lazy_chart_history_task = None
        self.chart.prepare_market(self.current_interval, reset_analysis=True)
        self._restore_current_symbol_drawings()
        if self._latest_trading_snapshot:
            self._sync_chart_working_orders(self._latest_trading_snapshot)
        self._sync_ticker_streams()
        hub = self.hub
        if hub is not None:
            hub.switch_market(symbol, self.current_interval)

    def switch_interval(self, interval: str) -> None:
        if interval not in TIMEFRAMES or interval == self.current_interval:
            return
        self._store_current_symbol_drawings()
        self._warm_initial_chart_history_pending = False
        self.current_interval = interval
        self._select_timeframe_button(interval)
        self.alert_center.set_market(self.current_symbol, interval)
        # The previous interval's worker is stale by definition.  Drop only
        # the active reference so the new interval is not blocked; completion
        # guards reject the old worker's result.
        self.lazy_chart_history_task = None
        self.chart.prepare_market(interval, reset_analysis=False)
        self._restore_current_symbol_drawings()
        hub = self.hub
        if hub is not None:
            hub.switch_market(self.current_symbol, interval)

    def _select_timeframe_button(self, interval: str) -> None:
        # Compatibility name retained for existing call sites. The old row of
        # timeframe buttons is gone; the compact market strip owns one selector.
        stats = getattr(self, "stats", None)
        if stats is not None:
            stats.set_interval(interval)

    def set_market_bar_timeframes(self, intervals: object) -> None:
        selected = normalized_market_bar_timeframes(intervals)
        if selected == self.market_bar_timeframes:
            return
        self.market_bar_timeframes = selected
        self.settings.setValue("ui/market_bar_timeframes_v1", json.dumps(selected))
        self.stats.set_timeframes(selected, self.current_interval)
        dialog = self.settings_dialog
        if dialog is not None and dialog.isVisible():
            dialog.sync_from_owner()

    def _fit_chart(self) -> None:
        self.chart.fit_chart()

    def _set_history_menu_state(
        self,
        mode: str | None = None,
        cancellable: bool = False,
    ) -> None:
        active = mode is not None
        self.download_history_action.setEnabled(not active)
        self.download_market_history_action.setEnabled(not active)
        self.load_history_action.setEnabled(not active)
        self.cancel_history_action.setEnabled(active and cancellable)
        self.download_history_action.setText(
            "Downloading historical candles… 0%"
            if mode == "download"
            else "Download historical candles…"
        )
        self.load_history_action.setText(
            "Loading historical CSV…"
            if mode == "load"
            else "Load historical CSV…"
        )
        if not active:
            self.cancel_history_action.setText("Cancel history download")

    def _cancel_history_download(self) -> None:
        if self.history_task is None:
            return
        self.history_cancel_requested = True
        self.cancel_history_action.setEnabled(False)
        self.cancel_history_action.setText("Cancellation requested…")
        self.statusBar().showMessage(
            "Waiting for the current Binance history request to finish…"
        )

    def download_market_history(self) -> None:
        if self.history_task is not None or self.market_history_dialog is not None:
            return
        self._ensure_market_data_hub()
        db = self._ensure_app_database()
        active_symbols = sorted(
            symbol for symbol in self.valid_symbols if symbol.endswith("USDT")
        )
        if not active_symbols:
            QtWidgets.QMessageBox.information(
                self,
                "Markets still loading",
                "Wait for the active Binance USD-M market list, then try again.",
            )
            return
        options = MarketDataOptionsDialog(len(active_symbols), self)
        if options.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        datasets = options.datasets()
        dataset_labels = {
            "candles": "candles",
            "funding": "funding",
            "premium_index": "premium index",
            "open_interest": "open interest",
        }
        selected_data = ", ".join(
            dataset_labels[key] for key in dataset_labels if key in datasets
        )
        answer = QtWidgets.QMessageBox.question(
            self,
            "Download market history data",
            f"Download and resume {selected_data} for {len(active_symbols)} active USDT perpetuals?\n\n"
            "This can take a long time and use substantial disk space. Live feeds and trading remain "
            "available while research requests use background rate-limit priority. You may pause "
            "after any complete page and resume later.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        dialog = MarketHistoryDownloadDialog(
            db, BinanceRest(self.testnet), active_symbols, datasets, self
        )
        self.market_history_dialog = dialog
        self.download_market_history_action.setEnabled(False)
        def finished(_result):
            if self.market_history_dialog is dialog:
                self.market_history_dialog = None
            self.download_market_history_action.setEnabled(True)
            self.statusBar().showMessage("Market-history downloader closed | committed pages remain in the local database.", 7000)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setModal(False)
        dialog.show()

    def download_history(self) -> None:
        if self.history_task is not None:
            return
        hub = self._ensure_market_data_hub()
        db = self._ensure_app_database()
        dialog = HistoryDownloadDialog(self.current_symbol, self.current_interval, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        days, save_csv = dialog.options()
        csv_path = ""
        if save_csv:
            documents = QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.DocumentsLocation
            )
            suggested = os.path.join(
                documents,
                f"Nightwatch_{self.current_symbol}_{self.current_interval}_history.csv",
            )
            csv_path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "Save historical candles",
                suggested,
                "CSV files (*.csv)",
            )
            if not csv_path:
                return
            if not csv_path.lower().endswith(".csv"):
                csv_path += ".csv"

        symbol = self.current_symbol
        interval = self.current_interval
        end_ms = int(time.time() * 1000)
        start_ms = 0 if days == 0 else end_ms - days * 86_400_000
        self.history_cancel_requested = False
        self.cancel_history_action.setText("Cancel history download")
        self._set_history_menu_state("download", cancellable=True)
        self.statusBar().showMessage(f"Downloading {symbol} · {interval} history…")

        task: ApiTask

        def load() -> dict[str, Any]:
            payload = hub.rest.download_history(
                symbol,
                interval,
                start_ms,
                end_ms,
                csv_path,
                task.signals.progress.emit,
                lambda: self.history_cancel_requested,
                lambda page: db.cache_candles(symbol, interval, page),
            )
            if not payload.get("cancelled"):
                first_time, last_time, _rows = db.candle_coverage(symbol, interval)
                db.mark_download_coverage(
                    symbol,
                    interval,
                    first_time,
                    last_time,
                    days == 0,
                )
            return payload

        def loaded(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.history_task = None
            self._set_history_menu_state()
            total = int(payload.get("total", 0))
            if payload.get("cancelled"):
                suffix = " · partial CSV kept" if payload.get("csv_path") else ""
                self.statusBar().showMessage(
                    f"History download cancelled after {total:,} candles{suffix}.",
                    7000,
                )
                return
            if (
                payload.get("symbol") == self.current_symbol
                and payload.get("interval") == self.current_interval
            ):
                self.chart.merge_history(payload.get("candles", []))
                hub.inject_history(
                    self.current_symbol,
                    self.current_interval,
                    payload.get("candles", []),
                )
                self.statusBar().showMessage(
                    f"History downloaded · {total:,} candles · preparing chart.",
                    10_000,
                )
            else:
                hub.inject_history(
                    str(payload.get("symbol", symbol)),
                    str(payload.get("interval", interval)),
                    payload.get("candles", []),
                )
                self.statusBar().showMessage(
                    f"History downloaded for {payload.get('symbol')} · {payload.get('interval')}; current chart was not changed.",
                    9000,
                )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.history_task = None
            self._set_history_menu_state()
            self.statusBar().showMessage(f"History download failed: {message}", 12_000)

        task = ApiTask(load)
        task.signals.progress.connect(self._update_history_progress)
        task.signals.finished.connect(loaded)
        task.signals.failed.connect(failed)
        self.history_task = task
        self.tasks.add(task)
        QtCore.QThreadPool.globalInstance().start(task)

    def _update_history_progress(self, _count: int, percent: int) -> None:
        if self._closing:
            return
        if self.history_task is not None and not self.history_cancel_requested:
            self.download_history_action.setText(
                f"Downloading historical candles… {max(0, min(100, percent))}%"
            )

    def load_history_csv(self) -> None:
        if self.history_task is not None:
            return
        hub = self._ensure_market_data_hub()
        db = self._ensure_app_database()
        path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load historical candles",
            "",
            "CSV files (*.csv)",
        )
        if not path:
            return
        symbol = self.current_symbol
        interval = self.current_interval
        self._set_history_menu_state("load")
        self.statusBar().showMessage(f"Loading {os.path.basename(path)}…")
        task: ApiTask

        def loaded(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.history_task = None
            self._set_history_menu_state()
            hub.inject_history(symbol, interval, payload.get("candles", []))
            if symbol == self.current_symbol and interval == self.current_interval:
                self.chart.merge_history(payload.get("candles", []))
                self.statusBar().showMessage(
                    "Historical CSV loaded · preparing chart.",
                    8000,
                )
            else:
                self.statusBar().showMessage(
                    f"Historical CSV loaded for {symbol} · {interval}; switch back to use it.",
                    8000,
                )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            self.history_task = None
            self._set_history_menu_state()
            self.statusBar().showMessage(f"Could not load history CSV: {message}", 12_000)

        def read_and_cache() -> dict[str, Any]:
            payload = BinanceRest.read_history_csv(symbol, interval, path)
            db.cache_candles(symbol, interval, payload.get("candles", []))
            return payload

        task = launch_task(read_and_cache, loaded, failed)
        self.history_task = task
        self.tasks.add(task)

    def _set_auto_scale(self, enabled: bool) -> None:
        self.auto_scale = bool(enabled)
        self.chart.set_auto_scale(self.auto_scale)
        self._sync_auto_scale_action(self.auto_scale)

    def _set_logarithmic(self, enabled: bool) -> None:
        self.logarithmic = bool(enabled)
        self.chart.set_logarithmic(self.logarithmic)
        action = getattr(self, "logarithmic_action", None)
        if action is not None and action.isChecked() != self.logarithmic:
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(self.logarithmic)
            del blocker

    def _update_toolbar_icons(self) -> None:
        layout_button = getattr(self, "panel_layout_button", None)
        if layout_button is not None:
            layout_button.setIcon(line_icon("panels", self.ui_theme["muted"], self.devicePixelRatioF()))
            self._sync_panel_layout_button()
        button = getattr(self, "settings_button", None)
        if button is not None:
            button.setIcon(
                line_icon("gear", self.ui_theme["muted"], self.devicePixelRatioF())
            )

    def _sync_fullscreen_controls(self, enabled: bool) -> None:
        enabled = bool(enabled)
        action = getattr(self, "fullscreen_action", None)
        if action is not None and action.isChecked() != enabled:
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(enabled)
            del blocker

    def _fullscreen_transition_failed(self, serial: int) -> None:
        if int(serial) != int(self._fullscreen_transition_serial):
            return
        self._fullscreen_requested = False
        self._windows_borderless_fullscreen = False
        self._windows_windowed_style = None
        self._windows_windowed_ex_style = None
        self._sync_fullscreen_controls(False)
        if self.isVisible():
            self._restore_windowed_geometry_after_fullscreen()

    def _restore_windowed_geometry_after_fullscreen(self) -> None:
        """Restore the pre-fullscreen window without an intermediate visible state."""
        normal_rect = self._restore_normal_rect_after_fullscreen

        if os.name == "nt":
            if self._restore_maximized_after_fullscreen:
                self.showMaximized()
                return
            # A failed/interrupted native transition can leave the Qt maximized
            # state bit set even after the HWND has been returned to normal
            # geometry. Clear that state before restoring a normal rectangle.
            if self.isMaximized():
                self.showNormal()
            if normal_rect is not None and normal_rect.isValid():
                self.setGeometry(QtCore.QRect(normal_rect))
            elif self._restore_geometry_after_fullscreen is not None:
                self.restoreGeometry(self._restore_geometry_after_fullscreen)
            QTimer.singleShot(0, self._fit_normal_window_to_available_height)
            return

        self.showNormal()
        if normal_rect is not None and normal_rect.isValid():
            self.setGeometry(QtCore.QRect(normal_rect))
        elif self._restore_geometry_after_fullscreen is not None:
            self.restoreGeometry(self._restore_geometry_after_fullscreen)
        if self._restore_maximized_after_fullscreen:
            QTimer.singleShot(0, self._restore_maximized_window_after_fullscreen)
        else:
            QTimer.singleShot(0, self._fit_normal_window_to_available_height)

    def _restore_maximized_window_after_fullscreen(self) -> None:
        self.showMaximized()

    def _effective_fullscreen(self) -> bool:
        return bool(self._windows_borderless_fullscreen or self.isFullScreen())

    def _windows_hwnd(self) -> int:
        if os.name != "nt":
            return 0
        try:
            return int(self.winId())
        except (RuntimeError, TypeError, ValueError):
            return 0

    def _apply_native_caption_fullscreen_request(self, enabled: bool) -> None:
        """Route the Windows maximize/restore caption button through F11 fullscreen."""
        if self._closing:
            return
        enabled = bool(enabled)
        action = getattr(self, "fullscreen_action", None)
        if enabled:
            if self._effective_fullscreen():
                self._sync_fullscreen_controls(True)
                return
            if action is not None and not action.isChecked():
                action.setChecked(True)
            else:
                self._toggle_fullscreen(True)
            return
        self._exit_fullscreen()

    def nativeEvent(self, event_type, message):
        """Make the native maximize/restore button mirror the F11 state machine.

        Windows normally interprets SC_RESTORE independently from our monitor-sized
        fullscreen HWND.  Consuming that command prevents the intermediate slightly
        smaller pseudo-window the user could previously reach from fullscreen.
        """
        if os.name == "nt":
            try:
                from ctypes import wintypes
                address = int(message)
            except (ImportError, TypeError, ValueError):
                address = 0
            if address:
                native_message = wintypes.MSG.from_address(address)
                if int(native_message.message) == 0x0112:  # WM_SYSCOMMAND
                    command = int(native_message.wParam) & 0xFFF0
                    sc_maximize = 0xF030
                    sc_restore = 0xF120
                    if command == sc_maximize and not self._effective_fullscreen():
                        QTimer.singleShot(
                            0,
                            lambda: self._apply_native_caption_fullscreen_request(True),
                        )
                        return True, 0
                    if command == sc_restore and self._effective_fullscreen():
                        QTimer.singleShot(
                            0,
                            lambda: self._apply_native_caption_fullscreen_request(False),
                        )
                        return True, 0
        return super().nativeEvent(event_type, message)

    @staticmethod
    def _set_windows_dwm_border_visual(hwnd_value: int, *, white: bool) -> None:
        """Use a white DWM frame in fullscreen and restore the system default on exit.

        The visible native WS_BORDER keeps DWM's stable composition boundary
        for the OpenGL-bearing owner while menus and dialogs are shown.
        """
        if os.name != "nt" or not int(hwnd_value):
            return
        try:
            import ctypes
            from ctypes import wintypes

            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            set_attribute = dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            set_attribute.restype = ctypes.c_long
            hwnd = wintypes.HWND(int(hwnd_value))

            # DWMWA_BORDER_COLOR = 34. COLORREF white is 0x00FFFFFF;
            # 0xFFFFFFFF restores the system-managed default on exit.
            border_color = ctypes.c_uint32(0x00FFFFFF if white else 0xFFFFFFFF)
            set_attribute(
                hwnd,
                34,
                ctypes.byref(border_color),
                ctypes.sizeof(border_color),
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return

    @staticmethod
    def _set_windows_redraw(hwnd, enabled: bool) -> None:
        """Suppress intermediate native paints during one geometry/state transaction."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendMessageW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            ]
            user32.SendMessageW.restype = ctypes.c_ssize_t
            # WM_SETREDRAW.  This does not destroy/recreate the HWND or the GL
            # surface; it merely prevents Windows from exposing an intermediate
            # restore/maximize frame while the final rectangle is installed.
            user32.SendMessageW(
                hwnd, 0x000B, wintypes.WPARAM(1 if enabled else 0), wintypes.LPARAM(0)
            )
            if enabled:
                user32.RedrawWindow.argtypes = [
                    wintypes.HWND, ctypes.c_void_p, wintypes.HANDLE, wintypes.UINT
                ]
                user32.RedrawWindow.restype = wintypes.BOOL
                rdw_invalidate = 0x0001
                rdw_updatenow = 0x0100
                rdw_allchildren = 0x0080
                user32.RedrawWindow(
                    hwnd, None, None, rdw_invalidate | rdw_updatenow | rdw_allchildren
                )
        except (AttributeError, OSError, ValueError):
            return

    def _set_windows_fullscreen_z_order(self, *, active: bool | None = None) -> None:
        """Keep active Windows fullscreen above the shell taskbar, not above other apps."""
        if os.name != "nt" or not self._windows_borderless_fullscreen:
            return
        hwnd_value = self._windows_hwnd()
        if not hwnd_value:
            return
        if active is None:
            application = QtWidgets.QApplication.instance()
            # Keep the fullscreen HWND stable while focus moves between Nightwatch
            # top-level windows. Moving the OpenGL-bearing main HWND between the
            # TOPMOST / NOTOPMOST bands for every dialog activation causes DWM/Qt
            # to expose and recompose the complete chart surface. Only whole-app
            # activation is allowed to change the main fullscreen z-band.
            active = bool(
                application is not None
                and application.applicationState()
                == Qt.ApplicationState.ApplicationActive
            )
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_window_long_ptr = user32.GetWindowLongPtrW
            get_window_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long_ptr.restype = ctypes.c_ssize_t
            hwnd = wintypes.HWND(hwnd_value)
            # Qt popups are topmost windows without a native owner. Raising an
            # already-topmost main HWND can cover them and recompose the chart.
            if bool(int(get_window_long_ptr(hwnd, -20)) & 0x00000008) == bool(active):
                return
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd_topmost = wintypes.HWND(-1)
            hwnd_notopmost = wintypes.HWND(-2)
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_noactivate = 0x0010
            swp_noownerzorder = 0x0200
            user32.SetWindowPos(
                hwnd,
                hwnd_topmost if active else hwnd_notopmost,
                0, 0, 0, 0,
                swp_nosize | swp_nomove | swp_noactivate | swp_noownerzorder,
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return

    def _sync_windows_fullscreen_native_stack(self) -> None:
        """Synchronize the owner and its dialogs at application activation edges."""
        if os.name != "nt" or not self._windows_borderless_fullscreen:
            return
        application = QtWidgets.QApplication.instance()
        if application is None:
            return
        active = application.applicationState() == Qt.ApplicationState.ApplicationActive
        self._set_windows_fullscreen_z_order(active=active)
        if active:
            self._raise_windows_fullscreen_popup(application.activePopupWidget())

    def _popup_belongs_to_terminal(self, popup: QtWidgets.QWidget) -> bool:
        # QWidget.isAncestorOf stops at top-level boundaries; popup widgets
        # retain a QObject parent chain even though their Win32 HWND is unowned.
        owner = popup.parent()
        while owner is not None:
            if owner is self:
                return True
            owner = owner.parent()
        return False

    def _raise_windows_fullscreen_popup(self, popup: QtWidgets.QWidget | None) -> None:
        """Keep Qt's unowned native menus above the active fullscreen HWND."""
        if (
            os.name != "nt"
            or not self._windows_borderless_fullscreen
            or not isinstance(popup, QtWidgets.QWidget)
            or not popup.isVisible()
            or popup.windowType() != Qt.WindowType.Popup
            or not self._popup_belongs_to_terminal(popup)
            or QtWidgets.QApplication.applicationState()
            != Qt.ApplicationState.ApplicationActive
        ):
            return
        handle = popup.windowHandle()
        if handle is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos(
                wintypes.HWND(int(handle.winId())), wintypes.HWND(-1),
                0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0010 | 0x0200,
            )
        except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
            return

    def _enter_windows_borderless_fullscreen(self, serial: int | None = None) -> None:
        """Enter monitor-filling Windows fullscreen without using Qt true fullscreen.

        The main HWND is converted to an actual borderless monitor-sized window.
        While Nightwatch is the active application it is placed in the topmost
        band so the Windows taskbar cannot cover the bottom of the client area.
        It is demoted as soon as the application loses activation so Alt+Tab and
        other applications retain normal desktop Z-order behavior.
        """
        if os.name != "nt":
            return
        if serial is None:
            serial = int(self._fullscreen_transition_serial)
        serial = int(serial)
        if (
            serial != int(self._fullscreen_transition_serial)
            or not self._fullscreen_requested
        ):
            return
        if self._windows_borderless_fullscreen:
            self._sync_fullscreen_controls(True)
            self._set_windows_fullscreen_z_order(active=True)
            return
        if not self.isVisible():
            self.show()
            QTimer.singleShot(
                0,
                lambda serial=serial: self._enter_windows_borderless_fullscreen(serial),
            )
            return

        hwnd_value = self._windows_hwnd()
        if not hwnd_value:
            self._fullscreen_transition_failed(serial)
            return

        hwnd = None
        style: int | None = None
        ex_style: int | None = None
        redraw_suppressed = False
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", RECT),
                    ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_window_long_ptr = user32.GetWindowLongPtrW
            set_window_long_ptr = user32.SetWindowLongPtrW
            get_window_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long_ptr.restype = ctypes.c_ssize_t
            set_window_long_ptr.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t
            ]
            set_window_long_ptr.restype = ctypes.c_ssize_t
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(MONITORINFO)
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL

            hwnd = wintypes.HWND(hwnd_value)
            gwl_style = -16
            gwl_exstyle = -20
            style = int(get_window_long_ptr(hwnd, gwl_style))
            ex_style = int(get_window_long_ptr(hwnd, gwl_exstyle))
            # Fullscreen uses the native maximized visual state so Windows renders
            # the restore-down caption glyph.  The stored windowed style is always
            # normalized to a restorable normal window, so leaving fullscreen shows
            # the single-square maximize glyph instead of restoring a native maximize.
            ws_maximize = 0x01000000
            ws_minimize = 0x20000000
            self._windows_windowed_style = style & ~(ws_maximize | ws_minimize)
            self._windows_windowed_ex_style = ex_style

            monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                self._fullscreen_transition_failed(serial)
                return

            # Keep Qt's WS_BORDER workaround for dialogs over fullscreen OpenGL.
            # Keep the native frame inside the monitor so its visible border
            # preserves the Windows 10 DWM composition workaround.
            ws_caption = 0x00C00000
            ws_thickframe = 0x00040000
            ws_border = 0x00800000
            ws_dlgframe = 0x00400000
            fullscreen_style = (
                style & ~(ws_caption | ws_thickframe | ws_dlgframe | ws_minimize)
            ) | ws_border | ws_maximize

            ws_ex_dlgmodalframe = 0x00000001
            ws_ex_windowedge = 0x00000100
            ws_ex_clientedge = 0x00000200
            ws_ex_staticedge = 0x00020000
            fullscreen_ex_style = ex_style & ~(
                ws_ex_dlgmodalframe
                | ws_ex_windowedge
                | ws_ex_clientedge
                | ws_ex_staticedge
            )

            self._set_windows_redraw(hwnd, False)
            redraw_suppressed = True

            # A maximized HWND carries a separate Windows placement state. Restore
            # it invisibly before installing the fullscreen native frame.
            if self.isMaximized():
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE

            set_window_long_ptr(hwnd, gwl_style, fullscreen_style)
            set_window_long_ptr(hwnd, gwl_exstyle, fullscreen_ex_style)
            self._set_windows_dwm_border_visual(hwnd_value, white=True)

            # Size the outer window to the monitor, retaining the visible border.
            rect = info.rcMonitor
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            swp_framechanged = 0x0020
            swp_showwindow = 0x0040
            swp_noownerzorder = 0x0200
            hwnd_topmost = wintypes.HWND(-1)

            if not user32.SetWindowPos(
                hwnd,
                hwnd_topmost,
                int(rect.left),
                int(rect.top),
                width,
                height,
                swp_framechanged | swp_showwindow | swp_noownerzorder,
            ):
                set_window_long_ptr(hwnd, gwl_style, style)
                set_window_long_ptr(hwnd, gwl_exstyle, ex_style)
                self._set_windows_dwm_border_visual(hwnd_value, white=False)
                self._set_windows_redraw(hwnd, True)
                redraw_suppressed = False
                self._fullscreen_transition_failed(serial)
                return

            # Make the monitor-filling HWND the foreground fullscreen surface.
            # Failures here are non-fatal: SetWindowPos already established the
            # required topmost geometry.
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            if (
                serial != int(self._fullscreen_transition_serial)
                or not self._fullscreen_requested
            ):
                set_window_long_ptr(hwnd, gwl_style, style)
                set_window_long_ptr(hwnd, gwl_exstyle, ex_style)
                self._set_windows_dwm_border_visual(hwnd_value, white=False)
                hwnd_notopmost = wintypes.HWND(-2)
                user32.SetWindowPos(
                    hwnd,
                    hwnd_notopmost,
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | swp_framechanged | swp_noownerzorder,
                )
                self._set_windows_redraw(hwnd, True)
                redraw_suppressed = False
                self._windows_windowed_style = None
                self._windows_windowed_ex_style = None
                self._restore_windowed_geometry_after_fullscreen()
                return

            self._windows_borderless_fullscreen = True
            self._sync_fullscreen_controls(True)
            self._set_windows_redraw(hwnd, True)
            redraw_suppressed = False
        except (AttributeError, OSError, ValueError) as exc:
            print(f"[Nightwatch fullscreen] Windows entry failed: {exc!r}")
            if hwnd is not None and style is not None:
                try:
                    set_window_long_ptr(hwnd, -16, int(style))
                    if ex_style is not None:
                        set_window_long_ptr(hwnd, -20, int(ex_style))
                    self._set_windows_dwm_border_visual(hwnd_value, white=False)
                    user32.SetWindowPos(
                        hwnd,
                        wintypes.HWND(-2),
                        0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0020 | 0x0200,
                    )
                except Exception:
                    pass
            if redraw_suppressed and hwnd is not None:
                try:
                    self._set_windows_redraw(hwnd, True)
                except Exception:
                    pass
            self._fullscreen_transition_failed(serial)

    def _exit_windows_borderless_fullscreen(self) -> None:
        if os.name != "nt" or not self._windows_borderless_fullscreen:
            return

        hwnd_value = self._windows_hwnd()
        style = self._windows_windowed_style
        ex_style = self._windows_windowed_ex_style
        self._windows_borderless_fullscreen = False
        self._windows_windowed_style = None
        self._windows_windowed_ex_style = None

        if hwnd_value and style is not None:
            redraw_suppressed = False
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                set_window_long_ptr = user32.SetWindowLongPtrW
                set_window_long_ptr.argtypes = [
                    wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t
                ]
                set_window_long_ptr.restype = ctypes.c_ssize_t
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, wintypes.UINT,
                ]
                user32.SetWindowPos.restype = wintypes.BOOL

                hwnd = wintypes.HWND(hwnd_value)
                self._set_windows_redraw(hwnd, False)
                redraw_suppressed = True

                # Leave the topmost band before restoring normal window chrome.
                hwnd_notopmost = wintypes.HWND(-2)
                user32.SetWindowPos(
                    hwnd,
                    hwnd_notopmost,
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010 | 0x0200,
                )

                set_window_long_ptr(hwnd, -16, int(style))
                if ex_style is not None:
                    set_window_long_ptr(hwnd, -20, int(ex_style))
                self._set_windows_dwm_border_visual(hwnd_value, white=False)

                # Recompute the non-client frame after restoring the normal style.
                user32.SetWindowPos(
                    hwnd,
                    hwnd_notopmost,
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0200,
                )

                self._set_windows_redraw(hwnd, True)
                redraw_suppressed = False
            except (AttributeError, OSError, ValueError) as exc:
                print(f"[Nightwatch fullscreen] Windows exit failed: {exc!r}")
                if redraw_suppressed:
                    try:
                        self._set_windows_redraw(hwnd, True)
                    except Exception:
                        pass

        self._restore_windowed_geometry_after_fullscreen()

    def _stabilize_fullscreen_chrome(self) -> None:
        """Keep permanent in-app chrome visible across fullscreen changes."""
        menu_bar = getattr(self, "_legacy_menu_bar", None)
        if menu_bar is not None:
            menu_bar.setVisible(False)
            menu_bar.setFixedHeight(0)
        status_bar = getattr(self, "_terminal_status_bar", None)
        if isinstance(status_bar, TerminalStatusBar):
            status_bar.setVisible(True)

    def _toggle_fullscreen(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._fullscreen_transition_serial += 1
        serial = int(self._fullscreen_transition_serial)
        self._fullscreen_requested = enabled

        if enabled:
            if self._effective_fullscreen():
                self._sync_fullscreen_controls(True)
                return
            # On Windows the native maximize button is an alias for F11. Exiting
            # fullscreen must therefore return to a normal restorable window with
            # the maximize glyph available, never to a native maximized state.
            self._restore_maximized_after_fullscreen = (
                False if os.name == "nt" else bool(self.isMaximized())
            )
            normal_rect = self.normalGeometry()
            if not normal_rect.isValid() or normal_rect.isEmpty():
                normal_rect = self.geometry()
            self._restore_normal_rect_after_fullscreen = QtCore.QRect(normal_rect)
            self._restore_geometry_after_fullscreen = self.saveGeometry()
            if os.name == "nt":
                self._enter_windows_borderless_fullscreen(serial)
            else:
                self.showFullScreen()
                self._sync_fullscreen_controls(True)
        else:
            if os.name == "nt" and self._windows_borderless_fullscreen:
                self._exit_windows_borderless_fullscreen()
            elif self.isFullScreen():
                self._restore_windowed_geometry_after_fullscreen()
            self._sync_fullscreen_controls(False)

        QTimer.singleShot(0, self._stabilize_fullscreen_chrome)
        QTimer.singleShot(80, self._stabilize_fullscreen_chrome)
        QTimer.singleShot(0, self, self._restore_terminal_keyboard_focus)
        QTimer.singleShot(80, self, self._restore_terminal_keyboard_focus)

    def _exit_fullscreen(self) -> None:
        action = getattr(self, "fullscreen_action", None)
        checked = bool(action is not None and action.isChecked())
        if not (self._effective_fullscreen() or checked):
            return
        if checked and action is not None:
            action.setChecked(False)
        else:
            # Recover from any action/native-state desynchronization instead of
            # relying on a signal when the action is already false.
            self._toggle_fullscreen(False)

    def _set_candle_style(self, style_name: str) -> None:
        if style_name not in CANDLE_STYLES:
            return
        self.candle_style = style_name
        self.chart.set_candle_style(style_name)
        self.settings.setValue("candle_style_v4", style_name)
        actions = getattr(self, "candle_style_actions", {})
        action = actions.get(style_name)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        self.statusBar().showMessage(f"CANDLES · {style_name}", 1800)
        self._sync_settings_window()


    def _apply_splitter_interaction_policy(self) -> None:
        self.right_rail_controller.sync_interaction_surfaces()

    def _update_right_rail_minimum_width(
        self, *, panel_margin: int | None = None
    ) -> None:
        del panel_margin
        self.right_rail_controller.refresh_geometry_constraints()


    def _set_right_panel_columns(
        self,
        columns: int,
        *,
        announce: bool = True,
        persist: bool = True,
        remember_current: bool = True,
    ) -> None:
        del persist, remember_current
        self.right_rail_controller.set_column_mode(columns)
        if announce:
            columns = self.right_rail_controller.column_mode
            self.statusBar().showMessage(
                "Right panels · independent columns" if columns == 2 else "Right panels · stacked",
                1800,
            )


    def _reset_right_panel_layout(self) -> None:
        default_name = "Balanced"
        presets = dict(self.right_layout_presets)
        presets[default_name] = json.loads(json.dumps(RIGHT_LAYOUT_PRESETS[default_name]))
        self.right_layout_presets = presets
        self.settings.setValue("right_layout_presets_v2", json.dumps(presets, separators=(",", ":")))
        self.right_rail_controller.update_presets(presets)
        self._rebuild_layout_menu()
        self.right_rail_controller.reset(
            default_name, self.right_layout_presets[default_name]
        )
        self.trading_workspace.set_page(0)
        self.settings.setValue("right_layout_preset", default_name)
        self.settings.sync()
        self.statusBar().showMessage("Right panel layout reset", 1800)

    def _rebuild_layout_menu(self) -> None:
        """Rebuild reusable panel-preset actions for Settings and shortcuts."""
        previous_group = getattr(self, "layout_action_group", None)
        if previous_group is not None:
            previous_group.deleteLater()
        for action in getattr(self, "layout_actions", {}).values():
            action.deleteLater()

        self.layout_actions: dict[str, QtGui.QAction] = {}
        action_group = QtGui.QActionGroup(self)
        action_group.setExclusive(True)
        for preset_name in self.right_layout_presets:
            action = QtGui.QAction(preset_name, self)
            action.setCheckable(True)
            action.setChecked(preset_name == self.right_layout_preset)
            action.triggered.connect(
                lambda _checked=False, name=preset_name: self._apply_right_layout_preset(name)
            )
            action_group.addAction(action)
            self.layout_actions[preset_name] = action
        self.layout_action_group = action_group


    def _sync_panel_registry_actions(self) -> None:
        for name in set(self.panel_actions) - set(self.panel_sections):
            self.panel_actions.pop(name).deleteLater()
        for name, section in self.panel_sections.items():
            if name not in self.panel_actions:
                action = QtGui.QAction(name, self)
                action.setCheckable(True)
                self.panel_actions[name] = action
                self.right_rail_controller.register_panel_action(name, action)
        if hasattr(self, "settings_dialog") and self.settings_dialog is not None:
            self._sync_settings_window()

    def _current_right_panel_definition(self) -> dict[str, Any]:
        controller = self.right_rail_controller
        controller.capture_geometry()
        state = controller.state
        titles = {pid: name for name, pid in state.aliases.items()}
        return {
            "tree": encode_tree(state.root),
            "visible": tuple(titles[pid] for pid in panel_ids(state.root) if pid in titles),
            "column_mode": controller.column_mode,
            "rail_width": state.rail_width(),
            "sections": tuple(RIGHT_PANEL_DEFAULT_SIZES.get(name, 160) for name in self.panel_sections),
        }

    def edit_right_panel_presets(self) -> None:
        dialog = RightPanelPresetsDialog(self.right_layout_presets, self, panel_names=tuple(self.panel_sections))
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._save_right_panel_presets(dialog.definitions(), dialog.editor.selected_name())

    def _save_right_panel_presets(self, configured: dict[str, dict[str, Any]], active_name: str | None = None) -> None:
        # Validate the entire draft before replacing any application state.
        aliases = self.right_rail_controller.state.aliases
        presets = {}
        names = set()
        for raw_name, definition in configured.items():
            name = str(raw_name).strip()
            if not name or name.casefold() == "custom" or name.casefold() in names:
                raise ValueError("Workspace preset names must be unique and nonempty")
            names.add(name.casefold())
            visible = valid_panel_names(definition.get("visible", ()), self.panel_sections)
            tree = decode_tree(definition.get("tree"))
            validate_tree(tree)
            if set(panel_ids(tree)) != {aliases[n] for n in visible}:
                raise ValueError("Preset tree must contain exactly its visible panels")
            presets[name] = {**definition, "visible": visible, "tree": encode_tree(tree),
                             "sections": tuple(RIGHT_PANEL_DEFAULT_SIZES.get(n, 160) for n in self.panel_sections)}
        if not presets or (active_name is not None and active_name not in presets):
            raise ValueError("Choose a saved workspace preset")
        previous_active = self.right_layout_preset
        self.right_layout_presets = presets
        self.settings.setValue("right_layout_presets_v2", json.dumps(presets, separators=(",", ":")))
        self.right_rail_controller.update_presets(presets)
        self._rebuild_layout_menu()
        selected = active_name or previous_active
        if selected in presets:
            self._apply_right_layout_preset(selected)
        else:
            self.right_rail_controller.save_state()
            self._right_rail_state_changed(self.right_rail_controller.state)
        self.settings.sync()
        self._sync_settings_window()

    def _quick_panel_layouts(self) -> tuple[str, ...]:
        return tuple(self.right_layout_presets)

    def _sync_panel_layout_button(self) -> None:
        button = getattr(self, "panel_layout_button", None)
        if button is None:
            return
        layouts = self._quick_panel_layouts()
        index = self._quick_panel_layout_index()
        current = "Custom" if index is None else layouts[index]
        following = layouts[0 if index is None else (index + 1) % len(layouts)]
        button.setToolTip(f"Layout: {current}\nClick for {following}\nIndividual panels and presets: Settings → Workspace")
        button.setAccessibleName(f"Panel layout: {current}. Next: {following}")

    def _quick_panel_layout_index(self) -> int | None:
        controller = getattr(self, "right_rail_controller", None)
        if controller is None:
            return None
        name = controller.state.active_preset
        layouts = self._quick_panel_layouts()
        return layouts.index(name) if name in layouts else None

    def _cycle_quick_panel_layout(self, _checked: bool = False) -> None:
        layouts = self._quick_panel_layouts()
        current = self._quick_panel_layout_index()
        self._apply_right_layout_preset(layouts[0 if current is None else (current + 1) % len(layouts)])

    def _sync_right_layout_actions(self) -> None:
        """Synchronize the authoritative right-rail state with actions/settings."""
        controller = getattr(self, "right_rail_controller", None)
        if controller is not None:
            self.right_layout_preset = controller.state.active_preset
        for name, action in getattr(self, "layout_actions", {}).items():
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(name == self.right_layout_preset)
            del blocker
        self._sync_panel_layout_button()
        self._sync_settings_window()

    def _apply_right_layout_preset(self, name: str) -> None:
        preset = self.right_layout_presets.get(name)
        if preset is None:
            return
        self.right_rail_controller.apply_preset(name, preset)
        self.trading_workspace.set_page(0)
        self.settings.setValue("right_layout_preset", name)
        self._sync_right_panel_alignment()
        self.statusBar().showMessage(f"Right panel layout · {name}", 1800)

    def _apply_preset_containing(self, panel_name: str) -> None:
        # Programmatic requests should reveal only the requested feature. They
        # must not silently replace the user's entire panel composition.
        self.right_rail_controller.ensure_panel(panel_name)


    def _right_rail_state_changed(self, state: object) -> None:
        self.right_layout_preset = self.right_rail_controller.state.active_preset
        self.settings.setValue("right_layout_preset", self.right_layout_preset)
        self._sync_right_layout_actions()
        self._sync_right_panel_alignment()
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentIndex() != 0:
            self.right_rail_controller.set_host_active(False)
        QTimer.singleShot(0, self._sync_market_depth_networking)
        self._sync_settings_window()

    def _right_rail_composition_changed(self) -> None:
        self._sync_right_panel_alignment()
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentIndex() != 0:
            self.right_rail_controller.set_host_active(False)
        QTimer.singleShot(0, self._sync_market_depth_networking)
    def _right_rail_geometry_changed(self) -> None:
        signature = (
            int(self.width()),
            int(self.right_rail_host.width()) if hasattr(self, 'right_rail_host') else 0,
            int(self.right_rail_controller.column_mode),
            self.right_rail_controller.revision,
        )
        if signature == self._right_rail_geometry_signature:
            return
        self._right_rail_geometry_signature = signature
        self._sync_shell_minimum_width()

    def _sync_shell_minimum_width(self) -> None:
        """Preserve control widths; expose the minimum the current layout can fit."""
        central = self.centralWidget()
        if central is not None and central.layout() is not None:
            width = max(0, central.layout().minimumSize().width())
            if self.minimumWidth() != width:
                self.setMinimumWidth(width)

    def _sync_right_panel_alignment(self) -> None:
        trading_section = self.panel_sections.get("Trading / positions")
        if (
            trading_section is None
            or not self.right_rail_controller.panel_enabled("Trading / positions")
        ):
            self.trading_workspace.set_bottom_panel(False)
            return

        placement = self.right_rail_controller.placement_context("trading")
        self.trading_workspace.set_bottom_panel(placement["touches_host_bottom"])

    def _cycle_right_layout(self, step: int = 1) -> None:
        names = tuple(self.right_layout_presets)
        if not names:
            return
        direction = -1 if step < 0 else 1
        if self.right_layout_preset in names:
            index = names.index(self.right_layout_preset)
        else:
            index = -1 if direction > 0 else 0
        self._apply_right_layout_preset(names[(index + direction) % len(names)])


    def _sync_ticker_streams(self, _symbols: object = None) -> None:
        hub = self.hub
        if hub is None:
            return
        tracked = [
            self.current_symbol,
            *self.alert_center.monitored_symbols(),
            *self.trading_gateway.open_position_symbols(),
            *self.watchlist.symbols,
            *(self.market_board.tracked_symbols if self.market_board is not None else ()),
        ]
        if self.valid_symbols:
            tracked = [symbol for symbol in tracked if symbol in self.valid_symbols]
        else:
            # Before exchangeInfo arrives, keep only explicitly local symbols;
            # never promote an unvalidated all-market ticker batch into the app universe.
            tracked = [
                symbol
                for symbol in tracked
                if symbol and symbol.endswith("USDT") and "_" not in symbol
            ]
        hub.set_ticker_symbols(list(dict.fromkeys(tracked))[:48])
        # Ticker subscriptions include positions/market-board rows, but candle
        # prefetch is intentionally limited to the user's explicit watchlist.
        hub.set_chart_prefetch_symbols(
            [
                symbol
                for symbol in self.watchlist.symbols
                if not self.valid_symbols or symbol in self.valid_symbols
            ]
        )

    def apply_theme(self, name: str) -> None:
        if name not in THEMES:
            name = DEFAULT_THEME_NAME
        self.theme_name = name
        self.settings.setValue("theme", name)
        self.theme = THEMES[name]
        self.ui_theme = ui_palette(self.theme, self._effective_ui_color_overrides())
        self._refresh_directional_surface_palettes()
        if not getattr(self, "_developer_ui_status_custom", False):
            self.developer_ui_status = self._default_developer_ui_status()
        display_name = THEME_DISPLAY_NAMES[name]
        for theme_name, action in getattr(self, "theme_actions", {}).items():
            blocker = QtCore.QSignalBlocker(action)
            action.setChecked(theme_name == name)
            del blocker
        self._apply_stylesheet()
        self.chart.apply_theme(self.chart_theme)
        self.chart_container.apply_theme(self.chart_theme)
        self.stats.apply_theme(self.ui_theme)
        self.watchlist.apply_theme(self.ui_theme)
        self.watchlist_sidebar.apply_theme(self.ui_theme)
        self.orderbook.apply_theme(self.orderbook_theme)
        if self.market_board is not None:
            self.market_board.apply_theme(self.ui_theme)
        self.rotation_overview.apply_theme(self.ui_theme)
        if self.sector_overview is not None:
            self.sector_overview.apply_theme(self.ui_theme)
        self.trading_workspace.apply_theme(self.ui_theme)
        if self.symbol_search_dialog is not None:
            self.symbol_search_dialog.apply_theme(self.ui_theme)
        self._sync_top_metrics()
        self._update_toolbar_icons()
        self.statusBar().showMessage(f"THEME · {display_name}", 1800)
        self._sync_settings_window()

    def _on_universe(self, payload: dict[str, Any]) -> None:
        """Prepare exchange rules off-thread, then apply one bounded GUI update."""
        self._universe_prepare_token += 1
        token = self._universe_prepare_token
        requested_started = time.perf_counter()
        task: ApiTask

        def build() -> UniversePreparation:
            prepared = prepare_universe(payload)
            return prepared

        def done(prepared: UniversePreparation) -> None:
            self.tasks.discard(task)
            if self._closing or token != self._universe_prepare_token:
                return
            self._apply_prepared_universe(prepared, requested_started)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing or token != self._universe_prepare_token:
                return
            self.statusBar().showMessage(
                f"MARKET RULE PREPARATION FAILED · {message}", 7000
            )

        task = launch_task(build, done, failed)
        self.tasks.add(task)

    def _apply_prepared_universe(
        self,
        prepared: UniversePreparation,
        requested_started: float,
    ) -> None:
        symbols = list(prepared.symbols)
        # TradingWorkspace receives this mapping by reference during construction.
        # Preserve object identity while installing the prepared rules.
        self.symbol_rules.clear()
        self.symbol_rules.update(prepared.rules)
        self.valid_symbols = frozenset(symbols)
        self._universe_ready = True

        hub = self.hub
        if hub is not None and hasattr(hub, "set_valid_ticker_symbols"):
            hub.set_valid_ticker_symbols(self.valid_symbols)

        self.orderbook.set_symbol_rules(
            self.symbol_rules.get(self.current_symbol, SymbolRules())
        )
        next_order_flow_tick = float(
            self.symbol_rules.get(self.current_symbol, SymbolRules()).tick_size or 0.0
        )
        if not math.isclose(
            self._order_flow_tick_size,
            next_order_flow_tick,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            self._reset_order_flow_runtime(tick_size=next_order_flow_tick)
        self.chart.set_symbol_rules(
            self.symbol_rules.get(self.current_symbol, SymbolRules())
        )

        exchange_symbols = list(prepared.exchange_symbols)
        if self.market_board is not None:
            self.market_board.set_universe(self.valid_symbols, exchange_symbols)
        if self.sector_overview is not None:
            self.sector_overview.set_universe(self.valid_symbols, exchange_symbols)
        self.chart_container.set_symbols(symbols)
        self.statusBar().market.setText(
            "Market: Open" if self.current_symbol in self.valid_symbols else "Market: —"
        )

        # Remove any unvalidated rows that may have arrived before exchangeInfo.
        self.tickers = {
            symbol: ticker
            for symbol, ticker in self.tickers.items()
            if symbol in self.valid_symbols
        }
        if self.market_board is not None:
            self.market_board.set_tickers(self.tickers)
        if self.sector_overview is not None:
            self.sector_overview.set_tickers(self.tickers)
        self.rotation_overview.set_tickers(self.tickers)
        self.pending_ticker_symbols.intersection_update(self.valid_symbols)

        supported_groups = {
            name: [symbol for symbol in group if symbol in self.valid_symbols]
            for name, group in self.watchlist.groups.items()
        }
        if supported_groups != self.watchlist.groups:
            self.watchlist.set_groups(
                supported_groups, self.watchlist.active_group, emit=False
            )
            self.watchlist_sidebar._groups_changed(self.watchlist.group_snapshot())
            self.settings.setValue(
                "watchlist_symbols", json.dumps(self.watchlist.symbols)
            )
            self.settings.setValue(
                "watchlist/groups_v1",
                json.dumps(self.watchlist.group_snapshot(), separators=(",", ":")),
            )

        self._schedule_coin_icon_refresh()
        self.watchlist.set_icon_requests_enabled(True)

        self._on_tickers(list(prepared.initial_tickers))
        if self.symbol_search_dialog is not None or self.workspace_stack.currentIndex() in (1, 2, 3):
            self.ticker_rank_dirty = True
            self.presentation_clock.request()
        self._apply_auxiliary_symbol_rules()
        self._sync_ticker_streams()
        self.trading_workspace.set_symbol(
            self.current_symbol,
            self.symbol_rules.get(self.current_symbol, SymbolRules()),
        )
        self._sync_execution_ticket_state()
        self.statusBar().showMessage(
            f"{len(symbols)} Binance USDT crypto perpetuals loaded.", 5000
        )
        if self.valid_symbols and self.current_symbol not in self.valid_symbols:
            replacement = (
                DEFAULT_SYMBOL if DEFAULT_SYMBOL in self.valid_symbols else symbols[0]
            )
            QTimer.singleShot(0, lambda value=replacement: self.switch_symbol(value))


    @profile_callback("app.on_bootstrap_ms")
    def _on_bootstrap(self, payload: dict[str, Any]) -> None:
        if payload.get("symbol") != self.current_symbol or payload.get("interval") != self.current_interval:
            return
        self.chart.set_snapshot(payload)

    def _on_chart_snapshot_committed(self, payload: dict[str, Any]) -> None:
        if payload.get("_history_merge"):
            return
        if (
            constants.INTERVAL_SECONDS[self.current_interval] < 86_400
            and not self.chart._history_exhausted
            and len(self.chart.candles) < INITIAL_CHART_WARM_CANDLES
        ):
            if self._book_valid:
                QTimer.singleShot(500, self._warm_initial_chart_history)
            else:
                self._warm_initial_chart_history_pending = True
        funding_action = self.indicator_actions.get("Funding Rate History")
        if funding_action is not None and funding_action.isChecked():
            self._ensure_funding_history_for_chart()
        liquidation_action = self.indicator_actions.get("Liquidations")
        if liquidation_action is not None and liquidation_action.isChecked():
            self._load_recorded_liquidations()
        self.statusBar().showMessage(
            f"{self.current_symbol} · {self.current_interval} · chart ready; analysis updates in the background.",
            5000,
        )

    def _on_chart_history_merged(self, count: int) -> None:
        self.statusBar().showMessage(f"History ready · chart {count:,} candles.", 8000)

    @profile_callback("app.on_analysis_ms")
    def _on_analysis(self, payload: dict[str, Any]) -> None:
        if payload.get("symbol") != self.current_symbol:
            return
        self.market_detail = dict(payload)
        self._top_metrics_dirty = True
        self.chart.set_analysis_snapshot(payload)
        premium = payload.get("premium") or {}
        if premium and not self.stats.last_mark_payload:
            self._on_mark(
                {
                    "p": premium.get("markPrice"),
                    "r": premium.get("lastFundingRate"),
                    "T": premium.get("nextFundingTime"),
                }
            )
        ticker = payload.get("ticker") or {}
        if ticker and not self.stats.last_ticker:
            self._on_tickers(
                [
                    {
                        "s": ticker.get("symbol", self.current_symbol),
                        "c": ticker.get("lastPrice"),
                        "P": ticker.get("priceChangePercent"),
                        "v": ticker.get("volume"),
                        "q": ticker.get("quoteVolume"),
                    }
                ]
            )
        interest = payload.get("interest") or {}
        if interest and not self.stats.last_interest_payload:
            self._on_interest(interest)
        self._request_top_metrics_sync(immediate=True)
        funding_count = len(payload.get("funding_history") or [])
        self.statusBar().showMessage(
            f"{self.current_symbol} · {self.current_interval} · "
            f"{funding_count} funding settlements loaded.",
            5000,
        )

    def _on_interest_history(self, payload: dict[str, Any]) -> None:
        if (
            payload.get("symbol") == self.current_symbol
            and payload.get("interval") == self.current_interval
        ):
            self.chart.update_oi_history(payload.get("oi_history", []))

    def _flush_presentation_frame(self, _frame_mono: float) -> None:
        """Commit secondary GUI state only when chart interaction is idle."""
        if (
            getattr(self, "pending_ticker_symbols", None)
            or getattr(self, "ticker_rank_dirty", False)
        ):
            self._flush_ticker_ui()
        if self._top_metrics_dirty and not self._top_metrics_timer.isActive():
            self._top_metrics_timer.start(2000)

    def _observe_chart_surface(self, chart: ChartWorkspace) -> None:
        self.presentation_clock.register_frame_source(chart.graphics.viewport())
        chart.render_surface_changed.connect(self.presentation_clock.register_frame_source)

    def _sync_background_priority(self) -> None:
        active = self._chart_interaction_priority_active or self._ui_resize_active
        for workspace in (self.market_board, self.sector_overview, self.rotation_overview):
            if workspace is not None:
                workspace.set_interaction_priority(active)
        if self.hub is not None:
            self.hub.set_interaction_priority(active)
        self.orderbook.set_interaction_priority(active)
        self.large_trades.set_interaction_priority(active)
        self._order_flow_interaction_priority_requested.emit(
            self._order_flow_generation, active
        )

    def _set_chart_interaction_priority(self, active: bool) -> None:
        """Give direct chart manipulation exclusive GUI presentation priority."""
        active = bool(active)
        if active == self._chart_interaction_priority_active:
            return
        self._chart_interaction_priority_active = active
        self._sync_background_priority()
        if active:
            # Keep pending rail geometry alive. The shared interaction phase
            # runs before _flush_presentation_frame, whose priority guard defers
            # ticker work while canonical market state continues updating.
            if self.pending_ticker_symbols or self.ticker_rank_dirty:
                self._interaction_deferred_ticker_ui = True
            return
        if (
            self._interaction_deferred_ticker_ui
            or self.pending_ticker_symbols
            or self.ticker_rank_dirty
        ):
            self.presentation_clock.request(immediate=True)
        if self._top_metrics_dirty and not self._top_metrics_timer.isActive():
            self._top_metrics_timer.start(1)

    def _on_tickers(self, updates: list[dict[str, Any]]) -> None:
        if self._closing or not updates:
            return
        tracked = frozenset((self.current_symbol, *self.watchlist.symbols))
        valid = self.valid_symbols  # installed as an immutable set by universe adoption
        self._ticker_preparation.post(updates, valid, tracked, self._universe_prepare_token)
        self._ticker_prepare_serial += 1
        self._ticker_prepare_job.submit(
            (self._universe_prepare_token, self._ticker_prepare_serial),
            self._ticker_preparation.prepare, valid, self._universe_prepare_token,
        )

    @QtCore.Slot(object, object)
    def _tickers_prepared(self, key, prepared) -> None:
        if self._closing or key[0] != self._universe_prepare_token:
            return
        tickers, changed, broad = prepared
        self.tickers = tickers
        # Custom alerts remain armed when their market is not on the chart.
        # Check only changed, monitored symbols; automatic chart alerts retain
        # their independent current-market kline path.
        for symbol in self.alert_center.monitored_symbols():
            if symbol in changed:
                self.alert_center.check_manual_price(symbol, safe_float(tickers.get(symbol, {}).get("c")))
        # Hidden consumers follow this immutable publication without re-running
        # analytics; visible consumers are updated by the presentation clock.
        if self.market_board is not None:
            self.market_board.tickers = tickers
        if self.sector_overview is not None:
            self.sector_overview.tickers = tickers
        self.rotation_overview.tickers = tickers
        current = tickers.get(self.current_symbol)
        if current and self.current_symbol in changed:
            self._order_flow_quote_volume_requested.emit(
                self._order_flow_generation, safe_float(current.get("q"))
            )
        workspace = self.workspace_stack.currentIndex()
        tracked = {*self.watchlist.symbols, self.current_symbol}
        if workspace in (0, 3) or self.symbol_search_dialog is not None:
            self.pending_ticker_symbols.update(changed & tracked if broad else changed)
        if broad and (self.symbol_search_dialog is not None or workspace in (1, 2, 3)):
            self.ticker_rank_dirty = True
        if not self.pending_ticker_symbols and not self.ticker_rank_dirty:
            return
        if self._chart_interaction_priority_active or self._ui_resize_active:
            self._interaction_deferred_ticker_ui = True
            return
        self.presentation_clock.request()

    @QtCore.Slot(object, str)
    def _ticker_prepare_failed(self, _key, message) -> None:
        if not self._closing:
            self.statusBar().showMessage(f"TICKER PREPARATION FAILED · {message}", 7000)

    @profile_callback("app.flush_ticker_ui_ms")
    def _flush_ticker_ui(self) -> None:
        flush_started = time.perf_counter()
        pending_symbols, self.pending_ticker_symbols = self.pending_ticker_symbols, set()
        pending = [
            self.tickers[symbol]
            for symbol in pending_symbols
            if symbol in self.tickers
        ]
        workspace_index = self.workspace_stack.currentIndex()
        broad_dirty = self.ticker_rank_dirty
        defer_broad = bool(
            broad_dirty
            and (self._chart_interaction_priority_active or self._ui_resize_active)
        )
        if broad_dirty and not defer_broad:
            # Let point updates paint before the full market/search model commit.
            # Repeated broad batches share one latest-state follow-up.
            if not self._ticker_rank_timer.isActive():
                self._ticker_rank_timer.start(1)
        elif pending and not broad_dirty and self.symbol_search_dialog is not None:
            self.symbol_search_dialog.update_tickers(pending)

        tracked = {*self.watchlist.symbols, self.current_symbol}
        watchlist_updates = [
            ticker for ticker in pending if ticker.get("s") in tracked
        ]
        if watchlist_updates:
            self.watchlist.update_tickers(watchlist_updates)

        # Point updates remain live. Full-map analytics above are the only ticker
        # work deliberately deferred during drag/resize.
        if pending and not broad_dirty:
            if workspace_index == 1 and self.market_board is not None:
                self.market_board.update_tickers(pending)
            elif workspace_index == 2 and self.sector_overview is not None:
                self.sector_overview.update_tickers(pending)
            elif workspace_index == 3:
                self.rotation_overview.update_tickers(pending)

        current = self.tickers.get(self.current_symbol) if self.current_symbol in pending_symbols else None
        if current:
            self.last_price = safe_float(current.get("c"), self.last_price)
            if workspace_index == 0:
                self.stats.update_ticker(current)
                self.chart.set_last_price(
                    self.last_price, safe_float(current.get("_price_time"))
                )
        if workspace_index == 0 and current:
            self._request_top_metrics_sync()

        self._interaction_deferred_ticker_ui = bool(defer_broad)
        elapsed_ms = max(0.0, (time.perf_counter() - flush_started) * 1000.0)
        if performance_profile_active():
            record_performance_timing("ticker.ui_flush_ms", elapsed_ms)
            record_performance_count("ticker.ui_flushes")

    def _flush_ticker_rank(self) -> None:
        if self._closing or not self.ticker_rank_dirty:
            return
        if self._chart_interaction_priority_active or self._ui_resize_active:
            self._interaction_deferred_ticker_ui = True
            return
        # Consume before calling models, retaining any reentrant newer dirties.
        self.ticker_rank_dirty = False
        if self.symbol_search_dialog is not None:
            self._refresh_ticker_market_model()
        workspace_index = self.workspace_stack.currentIndex()
        if workspace_index == 1 and self.market_board is not None:
            self.market_board.set_tickers(self.tickers)
        elif workspace_index == 2 and self.sector_overview is not None:
            self.sector_overview.set_tickers(self.tickers)
        elif workspace_index == 3:
            self.market_board.set_tickers(self.tickers)
            self.rotation_overview.set_tickers(self.tickers)

    def _on_kline(self, event: dict[str, Any]) -> None:
        row = event.get("k") or {}
        if (event.get("s") != self.current_symbol or row.get("i") != self.current_interval
                or safe_float(event.get("E")) < self.chart._last_kline_event):
            return
        price, closed = self.chart.update_kline(event)
        self._on_tickers([{"s": self.current_symbol, "c": row.get("c"), "E": event.get("E")}])
        self.alert_center.check_price(price)
        if closed and event.get("k"):
            symbol = self.current_symbol
            interval = self.current_interval
            candle = Candle.from_stream(event["k"])
            task: ApiTask

            def finished(_result: Any) -> None:
                self.tasks.discard(task)
                if self._closing:
                    return

            db = self._ensure_app_database()
            task = launch_task(
                lambda: db.cache_candles(symbol, interval, [candle]),
                finished,
                lambda _message: finished(None),
            )
            self.tasks.add(task)

    def _on_depth(self, event: dict[str, Any]) -> None:
        if not self._market_depth_active or event.get("s") != self.current_symbol:
            return
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        self._order_flow_depth_timing_counter = (self._order_flow_depth_timing_counter + 1) & 0x0F
        sample_pipeline_timing = self._order_flow_depth_timing_counter == 0
        main_depth_received_mono = time.perf_counter() if sample_pipeline_timing else 0.0
        profile_interaction = bool(self._chart_interaction_priority_active) if profile_started else False
        socket_mono_ms = safe_float(event.get("_socket_received_mono_ms"))
        if socket_mono_ms > 0:
            status_handler_mono_ms = time.perf_counter() * 1000.0
            self.statusBar().set_event_latency(
                safe_float(event.get("E")),
                safe_float(event.get("_server_received_ms")),
                socket_mono_ms,
                safe_float(event.get("_parser_done_mono_ms")),
                status_handler_mono_ms,
                latency=(
                    event.get("_latency")
                    if isinstance(event.get("_latency"), dict)
                    else None
                ),
            )
        analysis_bids = event.get("_analysis_bids")
        analysis_asks = event.get("_analysis_asks")
        bids, asks = self.chart.add_depth(event)
        self.best_bid = bids[0][0] if bids else 0.0
        self.best_ask = asks[0][0] if asks else 0.0
        if self._market_data_live and self._book_valid:
            # Keep chart ingestion bounded to the existing 120-level payload.
            # The order-flow worker receives the separate deeper source slice.
            # Temporal inference remains fixed to the existing top-120 band;
            # extra rows are resting-liquidity display depth only.
            worker_bids = analysis_bids if isinstance(analysis_bids, (list, tuple)) else bids
            worker_asks = analysis_asks if isinstance(analysis_asks, (list, tuple)) else asks
            try:
                source_revision = int(event.get("_analysis_revision") or event.get("u") or 0)
            except (TypeError, ValueError, OverflowError):
                source_revision = 0
            depth_timing: dict[str, float] | None = None
            if sample_pipeline_timing:
                worker_emit_mono = time.perf_counter()
                depth_timing = {
                    "socket_received_mono": socket_mono_ms / 1000.0 if socket_mono_ms > 0.0 else 0.0,
                    "parser_done_mono": safe_float(event.get("_parser_done_mono_ms")) / 1000.0,
                    "gui_dispatch_mono": safe_float(event.get("_gui_dispatch_mono_ms")) / 1000.0,
                    "main_depth_received_mono": main_depth_received_mono,
                    "worker_emit_mono": worker_emit_mono,
                }
            # The parser publication is immutable-by-convention and replaced, not
            # mutated, on the next source revision. Pass it by reference instead
            # of allocating another 120..1000-element tuple on the GUI thread.
            self._order_flow_depth_requested.emit(
                self._order_flow_generation,
                worker_bids,
                worker_asks,
                source_revision,
                depth_timing,
            )
        now = time.monotonic()
        if now - self.last_recorded_depth >= 5.0 and bids and asks:
            self.last_recorded_depth = now
            # Chart presentation remains intentionally bounded to 120 levels.
            # Market-event persistence keeps its 1000-level semantics via the
            # worker-computed aggregate attached to the same depth payload.
            bid_notional = safe_float(event.get("_depth_bid_notional"))
            ask_notional = safe_float(event.get("_depth_ask_notional"))
            if bid_notional <= 0.0:
                bid_notional = sum(price * quantity for price, quantity in bids)
            if ask_notional <= 0.0:
                ask_notional = sum(price * quantity for price, quantity in asks)
            self._record_market_event(
                "depth",
                {
                    "bid_notional": bid_notional,
                    "ask_notional": ask_notional,
                    "best_bid": bids[0][0],
                    "best_ask": asks[0][0],
                },
            )
        if profile_started:
            _record_market_handler_profile("depth", profile_started, profile_interaction)


    def _market_depth_should_stream(self) -> bool:
        """The DOM and trade history share one depth/BBO/trade transport."""
        if not hasattr(self, "orderbook") or not hasattr(self, "workspace_stack"):
            return False
        if self.workspace_stack.currentIndex() != 0:
            return False
        controller = getattr(self, "right_rail_controller", None)
        if controller is not None:
            return controller.panel_active("depth") or controller.panel_active("trades")
        return bool(self.orderbook.isVisible())

    def _set_order_flow_depth_capacity(self, limit_per_side: int) -> None:
        resolved = max(1, min(1000, int(limit_per_side)))
        self._order_flow_depth_capacity = resolved
        # The parser owns the canonical deep local book. Tell it only how many
        # near-market rows downstream presentation currently requires; storage
        # and synchronization depth are unchanged.
        hub = self.hub
        if hub is not None:
            hub.set_orderbook_depth_capacity(resolved)
        self._order_flow_depth_capacity_requested.emit(
            self._order_flow_generation, resolved
        )

    def _set_market_depth_active(self, active: bool) -> None:
        """Keep shared transport alive until both order-flow panels are hidden."""
        controller = getattr(self, "right_rail_controller", None)
        active = self._market_depth_should_stream() if controller is not None else bool(active)
        self._market_depth_active = active
        self._set_order_flow_snapshot_active(active)
        if not active:
            # Never leave a stale BBO available to execution helpers while the
            # depth transport is intentionally asleep.
            self.best_bid = 0.0
            self.best_ask = 0.0
        hub = self.hub
        if hub is not None:
            hub.set_orderbook_streaming_enabled(active)

    def _sync_market_depth_networking(self) -> None:
        self._set_market_depth_active(self._market_depth_should_stream())

    def _set_order_flow_snapshot_active(self, active: bool) -> None:
        """Enable/disable publication while worker-owned analytics continue."""
        active = bool(active)
        if active == self._order_flow_snapshot_active:
            return
        self._order_flow_snapshot_active = active
        self._order_flow_active_requested.emit(
            self._order_flow_generation, active
        )

    def _reset_order_flow_runtime(
        self,
        *,
        tick_size: float | None = None,
        quote_volume: float | None = None,
    ) -> None:
        """Create a new inference generation and reject all older queued work."""
        if tick_size is not None:
            self._order_flow_tick_size = max(0.0, float(tick_size))
        if quote_volume is None:
            quote_volume = safe_float((self.tickers.get(self.current_symbol) or {}).get("q"))
        self._order_flow_generation += 1
        tape = getattr(self, "large_trades", None)
        if tape is not None:
            tape.set_market(self.current_symbol, tick_size=self._order_flow_tick_size)
            tape.reset(preserve_history=True)
        self._order_flow_reset_requested.emit(
            self._order_flow_generation,
            self.current_symbol,
            self._order_flow_tick_size,
            max(0.0, float(quote_volume)),
        )
        # set_active is generation-guarded in the worker. Reassert it after every
        # reset so an old queued active command cannot control the new model.
        self._order_flow_active_requested.emit(
            self._order_flow_generation, self._order_flow_snapshot_active
        )

    @QtCore.Slot(int, object)
    def _on_order_flow_runtime_snapshot(self, generation: int, payload: object) -> None:
        if self._closing or int(generation) != self._order_flow_generation:
            return
        frame = payload
        timing: dict[str, float] = {}
        if (
            isinstance(payload, tuple)
            and len(payload) == 2
            and isinstance(payload[0], OrderFlowPresentationFrame)
            and isinstance(payload[1], dict)
        ):
            frame = payload[0]
            timing = {
                str(key): float(value)
                for key, value in payload[1].items()
                if isinstance(value, (int, float)) and math.isfinite(float(value))
            }
        if not isinstance(frame, OrderFlowPresentationFrame):
            return
        for key in (
            "socket_received_mono", "parser_done_mono", "gui_dispatch_mono",
            "main_depth_received_mono", "worker_emit_mono", "worker_received_mono",
            "worker_depth_processed_mono",
        ):
            try:
                numeric = float(getattr(frame, key, 0.0))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(numeric) and numeric > 0.0:
                timing[key] = numeric
        snapshot = frame.snapshot
        if snapshot.symbol != self.current_symbol:
            return
        timing["gui_delivery_mono"] = time.perf_counter()
        if self._market_depth_active and self.right_rail_controller.panel_active("depth"):
            self.order_flow_snapshot_ready.emit((frame, timing))

    @QtCore.Slot(int, object)
    def _on_microstructure_runtime_snapshot(self, generation: int, payload: object) -> None:
        if self._closing or int(generation) != self._order_flow_generation:
            return
        if not isinstance(payload, MicrostructureSnapshot):
            return
        if payload.symbol != self.current_symbol:
            return
        self._update_microstructure_card(payload)

    @QtCore.Slot(int, object)
    def _on_order_flow_runtime_diagnostic(self, generation: int, state: object) -> None:
        if int(generation) != self._order_flow_generation or not isinstance(state, dict):
            return
        self._order_flow_diagnostic_state = dict(state)

    @QtCore.Slot(int, str)
    def _on_order_flow_runtime_failed(self, generation: int, message: str) -> None:
        if self._closing or int(generation) != self._order_flow_generation:
            return
        self.statusBar().showMessage(str(message), 5000)
        self._book_valid = False
        self._reset_market_inference_boundary()
        self.orderbook.set_book_validity(False, 'ANALYSIS WORKER RECOVERING')

    @QtCore.Slot(int)
    def _on_order_flow_runtime_restarted(self, generation: int) -> None:
        if not self._closing and generation == self._order_flow_generation and self.hub is not None:
            self.hub.request_orderbook_resync()

    def order_flow_diagnostic_state(self) -> dict[str, Any]:
        return dict(self._order_flow_diagnostic_state)

    def _stop_order_flow_runtime(self) -> None:
        runtime = getattr(self, "_order_flow_runtime", None)
        thread = getattr(self, "_order_flow_runtime_thread", None)
        if runtime is None or thread is None:
            return
        # Close the process transport before quitting its Qt relay thread;
        # a queued shutdown slot alone can be skipped by QThread.quit().
        runtime.stop_transport()
        try:
            self._order_flow_shutdown_requested.emit()
        except RuntimeError:
            pass
        thread.finished.connect(self._finish_runtime_shutdown, QtCore.Qt.ConnectionType.QueuedConnection)
        thread.requestInterruption()
        thread.quit()

    @QtCore.Slot()
    def _finish_runtime_shutdown(self) -> None:
        QTimer.singleShot(0, self, self.close)

    def _on_book_ticker(self, payload: dict[str, Any]) -> None:
        """Feed real-time BBO into the temporal order-flow model."""
        if (
            not self._market_depth_active
            or not self._market_data_live
            or not self._book_valid
            or str(payload.get("s") or "").upper() != self.current_symbol
        ):
            return
        self._order_flow_book_ticker_requested.emit(
            self._order_flow_generation, dict(payload)
        )

    def _on_mark(self, payload: dict[str, Any]) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        profile_interaction = bool(self._chart_interaction_priority_active) if profile_started else False
        symbol = str(payload.get("s") or self.current_symbol).upper()
        mark = safe_float(payload.get("p"))
        if mark:
            self.trading_workspace.set_mark_price(mark, symbol)
            self._observe_emergency_mark(symbol, mark)
        if symbol != self.current_symbol:
            if profile_started:
                _record_market_handler_profile("mark", profile_started, profile_interaction)
            return
        self._sync_execution_ticket_state()
        if self.workspace_stack.currentIndex() == 0:
            self.stats.update_mark(payload)
            self._request_top_metrics_sync()
        else:
            self._deferred_mark_payload = dict(payload)
        now = time.monotonic()
        if now - self.last_recorded_funding >= 60.0:
            self.last_recorded_funding = now
            self._record_market_event("funding_rate", dict(payload), int(safe_float(payload.get("E")) or time.time() * 1000))
        if profile_started:
            _record_market_handler_profile("mark", profile_started, profile_interaction)

    def _on_liquidation(self, event: dict[str, Any]) -> None:
        order = event.get("o") or {}
        symbol = str(order.get("s") or "")
        if not symbol:
            return
        self._record_market_event(
            "liquidation",
            dict(event),
            int(safe_float(order.get("T") or event.get("E")) or time.time() * 1000),
            symbol,
        )
        if symbol != self.current_symbol:
            return
        price = safe_float(order.get("ap"))
        if price <= 0:
            price = safe_float(order.get("p"))
        quantity = safe_float(order.get("z"))
        if quantity <= 0:
            quantity = safe_float(order.get("l") or order.get("q"))
        if price <= 0 or quantity <= 0:
            return
        self.chart.add_liquidation(event)

    def _on_interest(self, payload: dict[str, Any]) -> None:
        reference = self.stats.mark or self.last_price
        # Model/history state remains live while hidden; only presentation is
        # deferred. ChartWorkspace.append_interest() is itself render-dormant.
        self.chart.append_interest(payload, reference)
        recorded = dict(payload)
        recorded["sumOpenInterestValue"] = safe_float(payload.get("openInterest")) * reference
        self._record_market_event("open_interest", recorded)
        if self.workspace_stack.currentIndex() != 0:
            self._deferred_interest_payload = dict(payload)
            return
        self.stats.update_interest(payload, reference)
        self._request_top_metrics_sync()

    def _on_trade_batch(self, payloads: list[dict[str, Any]]) -> None:
        """Consume one coalesced worker->GUI trade delivery per display slice."""
        symbol = self.current_symbol
        if (
            not self._market_depth_active
            or not self._market_data_live
            or not self._book_valid
        ):
            return
        matching = [payload for payload in payloads if payload.get("s") == symbol]
        if not matching:
            return

        self._order_flow_trade_batch_requested.emit(
            self._order_flow_generation, tuple(matching)
        )

    def _record_market_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        event_time: int | None = None,
        symbol: str | None = None,
    ) -> None:
        if self._closing or self._close_waiting_for_recorder:
            return
        if len(self.market_event_buffer) >= 10_000:
            del self.market_event_buffer[:500]
            self._market_event_dropped += 500
            self.statusBar().showMessage("LOCAL RECORDER OVERLOAD · dropped events are recorded as an explicit data gap", 5000)
        self.market_event_buffer.append(
            (
                symbol or self.current_symbol,
                event_type,
                int(event_time or time.time() * 1000),
                payload,
            )
        )
        if len(self.market_event_buffer) >= 500:
            self._flush_market_events()

    def _flush_market_events(self) -> None:
        if self.market_event_task is not None:
            return
        if self._market_event_dropped:
            self.market_event_buffer.append((self.current_symbol, "recorder_gap", int(time.time() * 1000),
                                             {"dropped_events": self._market_event_dropped, "reason": "bounded recorder queue overload"}))
            self._market_event_dropped = 0
        if not self.market_event_buffer:
            if self._close_waiting_for_recorder:
                QTimer.singleShot(0, self.close)
            return
        db = self._ensure_app_database()
        pending, self.market_event_buffer = self.market_event_buffer, []
        from ..market.recording import commit_market_events, spool_market_events
        spooling = bool(self._close_waiting_for_recorder and self._market_event_error)
        task: ApiTask

        def done(_result):
            self.tasks.discard(task)
            self.market_event_task = None
            self._market_event_error = ""
            if self.market_event_buffer:
                self._flush_market_events()
            elif self._close_waiting_for_recorder:
                QTimer.singleShot(0, self.close)

        def failed(message):
            self.tasks.discard(task)
            self.market_event_task = None
            self._market_event_error = message
            self.market_event_buffer[:0] = pending
            if len(self.market_event_buffer) > 10_000:
                excess = len(self.market_event_buffer) - 10_000
                del self.market_event_buffer[-excess:]
                self._market_event_dropped += excess
            self.statusBar().showMessage(f"Local recorder paused: {message}", 7000)
            if self._close_waiting_for_recorder and not spooling:
                self._flush_market_events()

        work = (lambda: spool_market_events(db, pending)) if spooling else (lambda: commit_market_events(db, pending))
        task = launch_task(work, done, failed, self._market_event_pool, source="local market recorder")
        self.market_event_task = task
        self.tasks.add(task)

    def _reset_market_inference_boundary(self) -> None:
        """Discard depth/trade inference that cannot cross a validity gap."""
        self.best_bid = 0.0
        self.best_ask = 0.0
        rules = self.symbol_rules.get(self.current_symbol, SymbolRules())
        self._reset_order_flow_runtime(
            tick_size=rules.tick_size,
            quote_volume=safe_float((self.tickers.get(self.current_symbol) or {}).get("q")),
        )
        self._reset_microstructure_card(preserve_render=True)
        self.orderbook.reset()

    def _on_book_validity(self, valid: bool, reason: str) -> None:
        """Treat synchronized-book readiness as separate from socket connectivity."""
        valid = bool(valid)
        changed = valid != self._book_valid
        self._book_valid = valid
        self._book_valid_reason = str(reason or ("READY" if valid else "INVALID"))
        self._sync_execution_ticket_state()
        if valid:
            self.orderbook.set_book_validity(True, self._book_valid_reason)
            if self._market_data_started_mono > 0.0:
                self._market_data_started_mono = 0.0
            if self._warm_initial_chart_history_pending:
                self._warm_initial_chart_history_pending = False
                QTimer.singleShot(500, self._warm_initial_chart_history)
            return
        if changed:
            self._reset_market_inference_boundary()
        # Apply the reason after reset so the DOM cannot lose the event-based
        # fault explanation while its market snapshot is cleared.
        self.orderbook.set_book_validity(False, self._book_valid_reason)
        if self._market_data_live and self._market_depth_active:
            self.statusBar().showMessage(
                f"ORDER BOOK SYNCING · {str(reason or 'INVALID').upper()}",
                5000,
            )

    def _on_order_flow_trade_stream_status(self, active: bool, reason: str) -> None:
        self.orderbook.set_trade_stream_status(bool(active), str(reason or ''))

    def _on_status(self, text: str, live: bool) -> None:
        live = bool(live)
        self._market_data_live = live
        self.statusBar().set_connection(live, text, self.current_symbol in self.valid_symbols)
        self._sync_execution_ticket_state()
        if not live:
            self.alert_center.reset_crossing_state()
            # Transport gaps are broader than book gaps: trades and depth can no
            # longer be combined, so reset temporal inference immediately. Book
            # validity remains independently owned by the public depth lifecycle.
            self._reset_market_inference_boundary()
            self.statusBar().showMessage(text, 5000)

    def _on_problem(self, message: str) -> None:
        self.statusBar().showMessage(message, 12_000)

    def add_price_alert(self) -> None:
        dialog = PriceAlertDialog(self.last_price or 1.0, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.alert_center.add_price_alert(dialog.price.value(), dialog.direction.currentText())

    def edit_alert_settings(self) -> None:
        dialog = AlertSettingsDialog(self.alert_center, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            dialog.apply()

    def edit_indicator_settings(self, initial_indicator: str | None = None) -> None:
        dialog = IndicatorSettingsDialog(
            self.chart.indicator_settings,
            self,
            initial_indicator if isinstance(initial_indicator, str) else None,
        )
        dialog.history_requested.connect(
            lambda name: self._load_recorded_liquidations()
            if name == "Liquidations"
            else None
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.indicator_settings = dialog.values()
        self.chart.set_indicator_settings(self.indicator_settings)
        self.settings.setValue("chart/rsi_period", self.chart.rsi_period)
        self.settings.setValue("chart/rsi_upper", self.chart.rsi_upper)
        self.settings.setValue("chart/rsi_lower", self.chart.rsi_lower)
        self.settings.setValue(
            "chart/indicator_settings_v1",
            json.dumps(self.chart.indicator_settings, separators=(",", ":")),
        )
        self.settings.sync()
        liquidation_action = self.indicator_actions.get("Liquidations")
        if liquidation_action is not None and liquidation_action.isChecked():
            self._load_recorded_liquidations()
        self.statusBar().showMessage(
            "INDICATOR SETTINGS SAVED",
            2500,
        )

    def edit_indicator_shortcuts(self) -> None:
        dialog = IndicatorShortcutsDialog(
            self.indicator_shortcuts, self,
            reserved_shortcuts={value for value in self.trading_hotkeys.values() if value},
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.indicator_shortcuts = dialog.shortcuts()
        self._refresh_indicator_action_labels()
        self.settings.setValue(
            "chart/indicator_shortcuts_v1",
            json.dumps(self.indicator_shortcuts, separators=(",", ":")),
        )
        self.settings.sync()
        self.statusBar().showMessage("INDICATOR SHORTCUTS SAVED", 2500)

    def _refresh_indicator_action_labels(self) -> None:
        for name, action in self.indicator_actions.items():
            shortcut = self.indicator_shortcuts.get(name, "") or "—"
            action.setText(f"{shortcut}   {name}")


    def _show_alerts_panel(self) -> None:
        self.alerts_dialog.show()
        self.alerts_dialog.raise_()
        self.alerts_dialog.activateWindow()

    def _trading_tickets(self) -> tuple[OrderPanel, ...]:
        return (self.order_panel,)

    def _ticket_for_widget(self, widget: QtWidgets.QWidget | None) -> OrderPanel | None:
        current: QtWidgets.QWidget | None = widget
        while current is not None:
            if isinstance(current, OrderPanel):
                return current
            current = current.parentWidget()
        return None


    @staticmethod
    def _informational_tooltip_allowed(watched: QtCore.QObject) -> bool:
        return tooltips_allowed(watched)

    def _execution_book_ready(self) -> bool:
        hub = getattr(self, "hub", None)
        return bool(self._book_valid and hub is not None and hub.execution_book_is_fresh())

    def _sync_execution_ticket_state(self) -> None:
        if not hasattr(self, "order_panel"):
            return
        rules_ready = (
            self._universe_ready and self.current_symbol in self.valid_symbols
        )
        live = self._market_data_live and rules_ready
        book_valid = self._execution_book_ready() and rules_ready
        if not rules_ready:
            reason = "MARKET RULES LOADING"
        else:
            reason = self._book_valid_reason if not self._book_valid else "READY" if book_valid else "BOOK STALE · SYNCHRONIZED"
        state = (live, book_valid, reason)
        if state == getattr(self, "_execution_ticket_market_state", None):
            return
        self._execution_ticket_market_state = state
        for ticket in self._trading_tickets():
            if hasattr(ticket, "set_execution_market_state"):
                ticket.set_execution_market_state(live, book_valid, reason)

    def _set_ticket_submission_state(
        self, state: str, detail: str = "", request_id: str = ""
    ) -> None:
        for ticket in self._trading_tickets():
            if hasattr(ticket, "set_submission_state"):
                ticket.set_submission_state(state, detail, request_id)

    def _refresh_frontend_submission_state(
        self, last_state: str = "", detail: str = "", request_id: str = ""
    ) -> None:
        in_flight = len(self._frontend_submission_requests)
        unknown = len(self._frontend_unknown_requests)
        if unknown:
            suffix = f" · {in_flight} OTHER IN FLIGHT" if in_flight else ""
            self._set_ticket_submission_state(
                "OUTCOME UNKNOWN",
                f"{unknown} REQUEST OUTCOME{'S' if unknown != 1 else ''} UNKNOWN" + suffix,
                request_id,
            )
            return
        if in_flight:
            self._set_ticket_submission_state(
                "SENDING",
                f"{in_flight} REQUEST{'S' if in_flight != 1 else ''} IN FLIGHT",
                request_id,
            )
            return
        self._set_ticket_submission_state(last_state or "READY", detail, request_id)

    def _set_ticket_protection_state(self, symbol: str, text: str) -> None:
        if str(symbol or "").upper() != self.current_symbol:
            return
        for ticket in self._trading_tickets():
            if hasattr(ticket, "set_protection_lifecycle"):
                ticket.set_protection_lifecycle(text)

    def _reserved_trading_shortcuts(self) -> set[str]:
        reserved = set(constants.SHELL_RESERVED_SHORTCUTS)
        reserved.update(
            str(value) for value in self.indicator_shortcuts.values() if str(value)
        )
        for action in self.findChildren(QtGui.QAction):
            sequences = action.shortcuts() if hasattr(action, "shortcuts") else []
            for sequence in sequences:
                text = sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText)
                if text:
                    reserved.add(text)
        return reserved

    @staticmethod
    def _submission_fingerprint(request: dict[str, Any]) -> str:
        order = dict(request.get("order") or {})
        fields = (
            "symbol", "side", "type", "quantity", "price", "triggerPrice",
            "stopPrice", "activatePrice", "callbackRate", "positionSide",
            "reduceOnly", "timeInForce", "workingType", "priceProtect",
        )
        normalized_order = {key: order.get(key) for key in fields if key in order}
        protections = request.get("protections") or {}
        normalized_protections = {
            kind: [
                {"price": item.get("price"), "percent": item.get("percent")}
                for item in list(protections.get(kind, []))[:4]
                if isinstance(item, dict)
            ]
            for kind in ("tp", "sl")
        }
        batch_orders = request.get("batch_orders") or []
        normalized_batch = []
        if isinstance(batch_orders, list):
            for row in batch_orders:
                if isinstance(row, dict):
                    normalized_batch.append(
                        {key: row.get(key) for key in fields if key in row}
                    )
        return json.dumps(
            {
                "order": normalized_order,
                "batch": normalized_batch,
                "protections": normalized_protections,
                "intent": request.get("position_intent"),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _frontend_duplicate_blocked(self, request: dict[str, Any]) -> tuple[bool, str]:
        fingerprint = self._submission_fingerprint(request)
        now = time.monotonic()
        self._recent_submission_fingerprints = {
            key: stamp
            for key, stamp in self._recent_submission_fingerprints.items()
            if now - stamp < 1.0
        }
        previous = self._recent_submission_fingerprints.get(fingerprint)
        return bool(previous is not None and now - previous < 0.250), fingerprint

    def _remember_frontend_submission(self, fingerprint: str) -> None:
        self._recent_submission_fingerprints[str(fingerprint)] = time.monotonic()

    def edit_quick_trading_settings(self) -> None:
        dialog = QuickTradingSettingsDialog(
            self.quick_trading_preset,
            self.trading_hotkeys,
            self,
            reserved_shortcuts=self._reserved_trading_shortcuts(),
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        preset, shortcuts = dialog.values()
        self.quick_trading_preset = preset
        self.trading_hotkeys = shortcuts
        self.trading_workspace.set_close_presets(preset)
        self.settings.setValue(
            "trading/quick_preset_v1", json.dumps(preset, sort_keys=True)
        )
        self.settings.setValue(
            "trading/hotkeys_v1", json.dumps(shortcuts, sort_keys=True)
        )
        self.settings.sync()
        message = "SHORTCUT PRESET SAVED"
        if self.trading_gateway.has_credentials():
            self.order_panel._request_leverage(int(preset["leverage"]))
            message += " · LEVERAGE SYNC STARTED"
        self.statusBar().showMessage(
            message + " · CTRL+SHIFT+A TOGGLE QUICK-ORDER LOCK",
            5000,
        )

    def test_trading_connection(self) -> None:
        if not self.trading_gateway.has_credentials():
            self.order_panel.edit_credentials()
        if not self.trading_gateway.has_credentials():
            return
        self.statusBar().showMessage("Testing signed Binance account access…")
        task: ApiTask

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            value = payload.get("dualSidePosition")
            hedge = value if isinstance(value, bool) else str(value).lower() == "true"
            mode = "HEDGE" if hedge else "ONE-WAY"
            self.statusBar().showMessage(
                f"Binance API connection verified · {mode} POSITION MODE", 9000
            )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self._closing:
                return
            QtWidgets.QMessageBox.warning(
                self,
                "Binance API connection failed",
                message,
            )

        task = launch_task(
            lambda: self.trading_gateway.rest.position_mode(
                self.trading_gateway.api_key,
                self.trading_gateway.api_secret,
                force=True,
            ),
            done,
            failed,
        )
        self.tasks.add(task)

    def _execute_trading_hotkey(self, action: str) -> None:
        if action == "open_trading":
            self._show_trading_sidebar()
            return
        if action == "refresh_account":
            self.trading_gateway.refresh_account(self.current_symbol, True)
            return
        if action == "cancel_all":
            self.trading_gateway.cancel_all(self.current_symbol)
            return
        if action == "kill_session":
            self.trading_gateway.cancel_all(self.current_symbol)
            self.trading_gateway.disarm()
            return
        if action not in {"place_buy", "place_sell", "close_1", "close_2", "close_3"}:
            return
        if action.startswith("close_"):
            position = self.trading_workspace.selected_position()
            if position is None:
                self.statusBar().showMessage(
                    f"No open {self.current_symbol} position to close.", 5000
                )
                return
            preset_index = max(0, min(2, int(action[-1]) - 1))
            self.trading_workspace.close_selected_position(
                self.trading_workspace.close_percentages[preset_index]
            )
            return
        if not self.trading_gateway.armed:
            self.statusBar().showMessage(
                "QUICK ENTRY BLOCKED · CTRL+SHIFT+A TO UNLOCK QUICK ORDERS", 5500
            )
            return
        if not (self._market_data_live and self._execution_book_ready()):
            self.statusBar().showMessage(
                "SHORTCUT ORDER BLOCKED · fresh synchronized book required", 7000
            )
            return
        target_leverage = int(self.quick_trading_preset["leverage"])
        if not self.quick_trading_preset.get("reduce_only") and (
            self.current_symbol not in self.trading_gateway.cross_ready
            or self.trading_gateway.current_leverage(self.current_symbol)
            != target_leverage
        ):
            self.statusBar().showMessage(
                "Shortcut blocked because current cross leverage does not match the saved preset.",
                8000,
            )
            return
        try:
            request = build_quick_order_request(
                self.trading_gateway,
                self.current_symbol,
                self.symbol_rules.get(self.current_symbol, SymbolRules()),
                "BUY" if action == "place_buy" else "SELL",
                self.quick_trading_preset,
                self.stats.mark or self.last_price,
                self.best_bid,
                self.best_ask,
                self.order_panel.hedge_mode,
            )
        except ValueError as exc:
            self.statusBar().showMessage(f"SHORTCUT ORDER BLOCKED · {exc}", 9000)
            return
        request["requires_arm"] = True
        self._submit_order(request)

    def _request_magnetic_rail_leverage(self, symbol: str, leverage: int) -> None:
        symbol = self._normalize_symbol(symbol)
        leverage = max(1, min(125, int(leverage)))
        if not symbol:
            return
        if not self.trading_gateway.has_credentials():
            self.trading_gateway.problem.emit(
                "Add Binance API credentials before changing magnetic-rail leverage."
            )
            return
        current = self.trading_gateway.current_leverage(symbol)
        # Rail execution is always CROSS by contract. cross_pending may belong to
        # the shell's background ensure_cross() call and must never suppress the
        # explicit rail leverage request. Only an already-matching leverage skips it.
        if current == leverage and symbol in self.trading_gateway.cross_ready:
            return
        self.trading_gateway.apply_cross_leverage(symbol, leverage)
        self.statusBar().showMessage(
            f"RAIL ORDER · {symbol} · SETTING {leverage}×, THEN SUBMITTING",
            4500,
        )

    def _magnetic_rail_leverage_changed(
        self,
        symbol: str,
        leverage: int,
        succeeded: bool,
        message: str,
    ) -> None:
        """Resume rail submissions waiting on the gateway's leverage acknowledgement."""
        symbol = self._normalize_symbol(symbol)
        confirmed = max(1, min(125, int(leverage)))
        waiting = [
            (key, pending)
            for key, pending in tuple(self._magnetic_rail_leverage_waits.items())
            if str(pending.get("symbol") or "") == symbol
        ]
        for key, pending in waiting:
            wanted = max(1, min(125, int(pending.get("leverage") or 1)))
            chart = pending.get("chart")
            draft_id = int(pending.get("draft_id") or 0)

            if not succeeded:
                self._magnetic_rail_leverage_waits.pop(key, None)
                if isinstance(chart, ChartWorkspace) and draft_id > 0:
                    chart.set_order_rail_submission_pending(draft_id, False)
                detail = str(message or "Binance rejected the leverage change")
                self.statusBar().showMessage(
                    f"RAIL ORDER BLOCKED · {symbol} · {detail}",
                    8000,
                )
                continue

            if confirmed != wanted:
                # A different leverage operation completed first. Explicitly apply
                # this rail's selected leverage; background cross_pending state is
                # not allowed to swallow the request.
                self._request_magnetic_rail_leverage(symbol, wanted)
                continue

            self._magnetic_rail_leverage_waits.pop(key, None)
            if not isinstance(chart, ChartWorkspace) or draft_id <= 0:
                continue

            current_state = chart.order_rail_state_for_draft(draft_id)
            if not isinstance(current_state, dict):
                chart.set_order_rail_submission_pending(draft_id, False)
                continue
            if (
                bool(current_state.get("armed"))
                or bool(current_state.get("cancellationPending"))
                or safe_float(current_state.get("railPrice")) <= 0
            ):
                chart.set_order_rail_submission_pending(draft_id, False)
                continue

            # submissionPending is deliberately true while leverage is changing;
            # this internal continuation is the only path allowed to pass it.
            self._execute_magnetic_rail_order(
                symbol, current_state, chart, leverage_confirmed=True
            )

    def _execute_magnetic_rail_order(
        self,
        symbol: str,
        state: dict[str, Any],
        chart: ChartWorkspace,
        *,
        leverage_confirmed: bool = False,
    ) -> None:
        """Translate one explicit rail BUY/SELL click into the normal order path."""
        if not isinstance(state, dict) or not isinstance(chart, ChartWorkspace):
            return
        symbol = self._normalize_symbol(symbol)
        draft_id = int(safe_float(state.get("railDraftId"), 0))
        if not symbol or draft_id <= 0 or safe_float(state.get("railPrice")) <= 0:
            return
        if (
            bool(state.get("armed"))
            or (bool(state.get("submissionPending")) and not leverage_confirmed)
            or bool(state.get("cancellationPending"))
        ):
            return
        rules = self.symbol_rules.get(symbol)
        if not isinstance(rules, SymbolRules):
            self.statusBar().showMessage(
                f"RAIL ORDER BLOCKED · {symbol} exchange rules unavailable",
                6500,
            )
            hub = getattr(self, "hub", None)
            if hub is not None and hasattr(hub, "request_universe_refresh"):
                hub.request_universe_refresh()
            return

        leverage = max(1, min(125, int(safe_float(state.get("leverage"), 5))))
        reducing = bool(state.get("reduceOnly"))
        if not reducing and not leverage_confirmed:
            confirmed_leverage = self.trading_gateway.current_leverage(symbol)
            # CROSS is the rail's fixed execution mode. A background ensure_cross
            # operation must not delay a fast rail order when leverage already matches.
            if (
                confirmed_leverage != leverage
                or symbol not in self.trading_gateway.cross_ready
            ):
                if not self.trading_gateway.has_credentials():
                    self.trading_gateway.problem.emit(
                        "Add Binance API credentials before submitting a magnetic-rail order."
                    )
                    return
                wait_key = (id(chart), draft_id)
                if wait_key not in self._magnetic_rail_leverage_waits:
                    self._magnetic_rail_leverage_waits[wait_key] = {
                        "chart": chart,
                        "draft_id": draft_id,
                        "symbol": symbol,
                        "leverage": leverage,
                    }
                    # Lock this draft immediately so repeated Ctrl+A cannot queue
                    # duplicate orders while Binance applies the leverage.
                    chart.set_order_rail_submission_pending(draft_id, True)
                self._request_magnetic_rail_leverage(symbol, leverage)
                return

        try:
            request = build_magnetic_rail_order_request(
                self.trading_gateway,
                symbol,
                rules,
                state,
                self.stats.mark if symbol == self.current_symbol else safe_float(state.get("railPrice")),
                self.best_bid if symbol == self.current_symbol else 0.0,
                self.best_ask if symbol == self.current_symbol else 0.0,
                self.trading_gateway.hedge_mode,
                [dict(row) for row in self.trading_gateway.position_cache.values()],
            )
        except ValueError as exc:
            self.statusBar().showMessage(f"RAIL ORDER BLOCKED · {exc}", 8000)
            return

        order = request.get("order") if isinstance(request, dict) else None
        if not isinstance(order, dict):
            return
        # Give every rail a stable client ID before transport. The gateway keeps
        # it for standard orders and converts it to clientAlgoId for conditional
        # orders, making later cancellation/reconciliation unambiguous even when
        # several visually identical rails are open at once.
        client_id = str(order.get("newClientOrderId") or "")
        if not client_id:
            client_id = self.trading_gateway.client_order_id("nwr")
            order["newClientOrderId"] = client_id
        algo = str(order.get("type") or "").upper() in CONDITIONAL_ORDER_TYPES

        chart.set_order_rail_submission_pending(draft_id, True)
        request_id = self._submit_order(request)
        if request_id:
            self._magnetic_rail_requests[request_id] = {
                "chart": chart,
                "draft_id": draft_id,
                "symbol": symbol,
                "client_id": client_id,
                "algo": algo,
                "cancel_requested": False,
                "order": dict(order),
            }
        else:
            chart.set_order_rail_submission_pending(draft_id, False)

    @staticmethod
    def _magnetic_rail_cancel_payload(
        symbol: str,
        working_order: dict[str, Any],
        *,
        client_id: str = "",
        algo_hint: bool = False,
    ) -> tuple[dict[str, Any], bool] | None:
        source = str(working_order.get("_source") or "").upper()
        algo = bool(
            algo_hint
            or source == "ALGO"
            or working_order.get("algoId") not in (None, "")
            or working_order.get("clientAlgoId") not in (None, "")
        )
        request: dict[str, Any] = {"symbol": symbol}
        if algo:
            # Binance's algo cancel endpoint accepts algoId/clientAlgoId. A
            # standard orderId is not a safe substitute for algoId, even if a
            # response happens to expose both fields.
            algo_id = working_order.get("algoId")
            client = (
                working_order.get("clientAlgoId")
                or working_order.get("clientOrderId")
                or client_id
            )
            if algo_id not in (None, ""):
                request["algoId"] = algo_id
            elif client:
                request["clientAlgoId"] = str(client)
            else:
                return None
        else:
            order_id = working_order.get("orderId")
            client = (
                working_order.get("clientOrderId")
                or working_order.get("origClientOrderId")
                or client_id
            )
            if order_id not in (None, ""):
                request["orderId"] = order_id
            elif client:
                request["origClientOrderId"] = str(client)
            else:
                return None
        return request, algo

    def _submit_magnetic_rail_cancel(
        self,
        symbol: str,
        chart: ChartWorkspace,
        draft_id: int,
        working_order: dict[str, Any],
        *,
        client_id: str = "",
        algo_hint: bool = False,
    ) -> bool:
        built = self._magnetic_rail_cancel_payload(
            symbol, working_order, client_id=client_id, algo_hint=algo_hint
        )
        if built is None:
            return False
        request, algo = built
        cancel_id = self.trading_gateway.submit_cancel(request, algo)
        if not self.trading_gateway.request_was_admitted(cancel_id):
            chart.set_order_rail_cancellation_pending(draft_id, False)
            self.statusBar().showMessage(
                f"RAIL CANCEL BLOCKED · {symbol} · no cancellable order identifier",
                7000,
            )
            return False
        self._magnetic_rail_cancel_requests[cancel_id] = {
            "chart": chart,
            "draft_id": int(draft_id),
            "symbol": symbol,
            "working_order": dict(working_order),
            "client_id": str(client_id or ""),
            "algo": bool(algo),
        }
        self.statusBar().showMessage(
            f"RAIL CANCEL · {symbol} · {cancel_id}", 5000
        )
        return True

    def _cancel_magnetic_rail_order(
        self,
        symbol: str,
        state: dict[str, Any],
        chart: ChartWorkspace,
    ) -> None:
        if not isinstance(state, dict) or not isinstance(chart, ChartWorkspace):
            return
        symbol = self._normalize_symbol(symbol)
        draft_id = int(safe_float(state.get("railDraftId"), 0))
        if not symbol or draft_id <= 0:
            return
        chart.set_order_rail_cancellation_pending(draft_id, True)

        # If placement is still awaiting an outcome, queue cancellation on that
        # exact submission. A positive placement response immediately cascades
        # into cancel; a deterministic rejection simply removes the local rail.
        for submission in self._magnetic_rail_requests.values():
            if (
                submission.get("chart") is chart
                and int(submission.get("draft_id") or 0) == draft_id
            ):
                submission["cancel_requested"] = True
                self.statusBar().showMessage(
                    f"RAIL CANCEL QUEUED · {symbol} · waiting for placement outcome",
                    6500,
                )
                return

        working_order = state.get("workingOrder")
        if isinstance(working_order, dict) and working_order:
            self._submit_magnetic_rail_cancel(
                symbol, chart, draft_id, working_order
            )
            return

        # An accepted rail can briefly exist before the account snapshot binds
        # its exchange identifier. Keep it visible/cancel-pending and refresh;
        # never remove an armed rail locally until Binance is known terminal.
        self.trading_gateway.refresh_account(None, all_open_orders=True)
        self.statusBar().showMessage(
            f"RAIL CANCEL WAITING · {symbol} · reconciling exchange order ID",
            6500,
        )

    def _execute_armed_percentage_order(self, prefix: str, percent: int) -> None:
        side = "BUY" if prefix == "B" else "SELL"
        if not (self._market_data_live and self._execution_book_ready()):
            self.statusBar().showMessage(
                "QUICK ORDER BLOCKED · fresh synchronized book required", 7000
            )
            return
        selected_leverage = self.order_panel.leverage.value()
        confirmed_leverage = self.trading_gateway.current_leverage(
            self.current_symbol
        )
        if (
            self.current_symbol in self.trading_gateway.cross_pending
            or confirmed_leverage <= 0
            or selected_leverage != confirmed_leverage
        ):
            self.statusBar().showMessage(
                "QUICK ORDER BLOCKED · wait for confirmed CROSS leverage",
                7000,
            )
            return
        preset = dict(constants.DEFAULT_QUICK_TRADING_PRESET)
        preset.update(
            {
                "collateral_percent": max(1, min(100, int(percent))),
                "leverage": confirmed_leverage,
                "order_mode": "MARKET",
                "slippage_enabled": False,
                "reduce_only": False,
                "take_profit_enabled": False,
                "stop_loss_enabled": False,
            }
        )
        try:
            request = build_quick_order_request(
                self.trading_gateway,
                self.current_symbol,
                self.symbol_rules.get(self.current_symbol, SymbolRules()),
                side,
                preset,
                self.stats.mark or self.last_price,
                self.best_bid,
                self.best_ask,
                self.order_panel.hedge_mode,
            )
        except ValueError as exc:
            self.statusBar().showMessage(f"QUICK ORDER BLOCKED · {exc}", 7000)
            return
        self.statusBar().showMessage(
            f"{side} MARKET · {int(percent):02d}% COLLATERAL · {confirmed_leverage}×",
            3500,
        )
        request["requires_arm"] = True
        self._submit_order(request)

    def _place_smart_exit(self) -> None:
        if not self.trading_gateway.armed:
            return
        if not (self._market_data_live and self._execution_book_ready()):
            self.statusBar().showMessage(
                "SMART EXIT NOT PLACED · fresh synchronized book required", 7000
            )
            return
        candidates = [
            dict(row)
            for (symbol, _side), row in self.trading_gateway.position_cache.items()
            if symbol == self.current_symbol
            and abs(safe_float(row.get("positionAmt"))) > 0
        ]
        position: dict[str, Any] | None = None
        if len(candidates) == 1:
            position = candidates[0]
        elif (
            len(candidates) > 1
            and (
                self.trading_workspace.current_page() == 1
                or self.order_panel.reduce_only.isChecked()
            )
        ):
            position = self.trading_workspace.selected_position()
            if position is not None and position.get("symbol") != self.current_symbol:
                position = None
        if position is None:
            self.statusBar().showMessage(
                f"SMART EXIT · SELECT A {self.current_symbol} POSITION", 5000
            )
            return
        rules = self.symbol_rules.get(self.current_symbol, SymbolRules())
        try:
            orders = build_smart_exit_orders(
                position,
                self.chart.candles,
                rules,
                self.best_bid,
                self.best_ask,
                self.trading_workspace.existing_exit_quantity(position),
            )
        except ValueError as exc:
            self.statusBar().showMessage(f"SMART EXIT NOT PLACED · {exc}", 7000)
            return
        request_id = self._submit_batch_orders(
            {
                "orders": orders,
                "rules": rules,
                "position_intent": "REDUCE",
            }
        )
        if not request_id:
            return
        levels = " · ".join(str(order["price"]) for order in orders)
        self.statusBar().showMessage(
            f"SMART EXIT · {len(orders)} POST-ONLY ORDER{'S' if len(orders) != 1 else ''} · {levels}",
            8000,
        )

    def _confirm_first_trade_attempt(self, summary: str) -> bool:
        if not self.trading_gateway.armed:
            return True
        if self._first_trade_confirmation_complete:
            return True
        # Mark the safety prompt as consumed when it is shown, not only when
        # accepted. It must appear exactly once per process lifetime.
        self._first_trade_confirmation_complete = True
        choice = QtWidgets.QMessageBox.question(
            self,
            "Confirm first trade",
            "Confirm the first order attempt of this Nightwatch run?\n\n"
            f"{summary}\n\n"
            "This confirmation will not appear again until Nightwatch is restarted.",
            (
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel
            ),
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if choice != QtWidgets.QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage(
                "FIRST TRADE CANCELED · CONFIRMATION WILL RETURN AFTER APP RESTART",
                6500,
            )
            return False
        return True

    @staticmethod
    def _order_attempt_summary(order: dict[str, Any]) -> str:
        details = [
            str(order.get("symbol") or "—"),
            str(order.get("side") or "—"),
            str(order.get("type") or "ORDER").replace("_", " "),
        ]
        quantity = order.get("quantity") or order.get("origQty")
        if quantity not in {None, ""}:
            details.append(f"SIZE {quantity}")
        price = order.get("price")
        if price not in {None, "", "0", 0}:
            details.append(f"PRICE {price}")
        trigger = order.get("triggerPrice") or order.get("stopPrice")
        if trigger not in {None, "", "0", 0}:
            details.append(f"TRIGGER {trigger}")
        return " · ".join(details)

    def _submit_batch_orders(self, request: dict[str, Any]) -> str:
        orders = [dict(order) for order in request.get("orders") or []]
        if not orders:
            return ""
        batch_request = {
            "order": {"symbol": orders[0].get("symbol"), "type": "BATCH", "quantity": len(orders), "side": orders[0].get("side")},
            "batch_orders": orders,
            "protections": {},
            "position_intent": request.get("position_intent"),
        }
        blocked, fingerprint = self._frontend_duplicate_blocked(batch_request)
        if blocked:
            self.statusBar().showMessage("DUPLICATE BATCH BLOCKED · previous request was just admitted", 2500)
            return ""
        summary = (
            f"{len(orders)}-ORDER BATCH · "
            f"{self._order_attempt_summary(orders[0])}"
        )
        if request.get("requires_arm") and not self._confirm_first_trade_attempt(summary):
            return ""
        request_id = self.trading_gateway.submit_batch_orders(
            orders,
            request.get("rules"),
            position_intent=str(request.get("position_intent") or "OPEN"),
            requires_arm=bool(request.get("requires_arm")),
        )
        if self.trading_gateway.request_was_admitted(request_id):
            self._remember_frontend_submission(fingerprint)
            self._frontend_submission_requests.add(request_id)
            self._refresh_frontend_submission_state(
                detail=f"{len(orders)}-ORDER BATCH", request_id=request_id
            )
            self.statusBar().showMessage(
                f"Sending {len(orders)}-order batch · {request_id}",
                5000,
            )
            return request_id
        return ""

    def _submit_order(self, request: dict[str, Any]) -> str:
        order = dict(request.get("order") or {})
        if not order:
            return ""
        reducing = str(request.get("position_intent") or "").upper() == "REDUCE" or str(order.get("reduceOnly", "")).lower() == "true"
        if order.get("type") == "MARKET" and not reducing and not self._execution_book_ready():
            self.statusBar().showMessage("OPEN MARKET ORDER BLOCKED · FRESH SYNCHRONIZED BOOK REQUIRED", 7000)
            return ""
        blocked, fingerprint = self._frontend_duplicate_blocked(request)
        if blocked:
            self.statusBar().showMessage(
                "DUPLICATE ORDER BLOCKED · previous identical request was just admitted",
                2500,
            )
            if self._frontend_submission_requests or self._frontend_unknown_requests:
                self._refresh_frontend_submission_state(
                    detail="DUPLICATE ORDER BLOCKED · " + self._order_attempt_summary(order)
                )
            else:
                self._set_ticket_submission_state(
                    "DUPLICATE BLOCKED", self._order_attempt_summary(order)
                )
            return ""
        if request.get("requires_arm") and not self._confirm_first_trade_attempt(
            self._order_attempt_summary(order)
        ):
            return ""
        protections = request.get("protections") or {}
        has_protections = bool(protections.get("tp") or protections.get("sl"))
        symbol = str(order.get("symbol") or "")
        resolved_rules = request.get("rules")
        if not isinstance(resolved_rules, SymbolRules):
            resolved_rules = self.symbol_rules.get(symbol)
        if has_protections and not isinstance(resolved_rules, SymbolRules):
            detail = (
                f"{symbol or 'UNKNOWN'} · exchange symbol rules are unavailable; "
                "the entry was not sent because its TP/SL could not be validated safely."
            )
            self.alerts_panel.append_alert("PROTECTION RULES UNAVAILABLE", detail)
            self.statusBar().showMessage("ORDER BLOCKED · waiting for exchange symbol rules", 7000)
            hub = getattr(self, "hub", None)
            if hub is not None and hasattr(hub, "request_universe_refresh"):
                hub.request_universe_refresh()
            return ""
        if has_protections:
            try:
                for kind in ("tp", "sl"):
                    targets = list(protections.get(kind, []))[:4]
                    protection_quantities(safe_float(order.get("quantity")), targets, resolved_rules)
                    for target in targets:
                        validate_step(str(target.get("price", "")), resolved_rules.tick_size, "Protection trigger", offset=str(resolved_rules.min_price))
            except ValueError as exc:
                self.alerts_panel.append_alert("ENTRY PROTECTION INVALID", f"{symbol} · {exc} · entry not sent")
                return ""
        if has_protections:
            # Register the plan before transmission. A fast market fill can be
            # delivered by the account stream before the request response.
            order.setdefault(
                "newClientOrderId",
                self.trading_gateway.client_order_id("nwp"),
            )
        context = {
            "requires_arm": bool(request.get("requires_arm")),
            "entry": order,
            "protections": protections,
            "position_intent": request.get("position_intent"),
            "rules": resolved_rules if isinstance(resolved_rules, SymbolRules) else SymbolRules(),
            "source": str(request.get("source") or ""),
            "collateral_asset": str(request.get("collateral_asset") or ""),
            "collateral_required": safe_float(request.get("collateral_required")),
            "rail_draft_id": int(safe_float(request.get("rail_draft_id"), 0)),
        }
        request_id = self.trading_gateway.submit_order(order, context)
        if has_protections and self.trading_gateway.request_was_admitted(request_id):
            client_id = str(order["newClientOrderId"])
            pending = {**context, "client_id": client_id}
            self.pending_protections[client_id] = pending
            self.protection_request_clients[request_id] = client_id
            self._set_ticket_protection_state(str(order.get("symbol") or ""), "PROTECTION QUEUED")
        if self.trading_gateway.request_was_admitted(request_id):
            self._remember_frontend_submission(fingerprint)
            self._frontend_submission_requests.add(request_id)
            self._refresh_frontend_submission_state(
                detail=self._order_attempt_summary(order), request_id=request_id
            )
            self.statusBar().showMessage(
                f"Sending {order.get('side')} {order.get('type')} · "
                f"{order.get('symbol')} · {request_id}",
                5000,
            )
            return request_id
        return ""

    def _trade_request_succeeded(self, request_id: str, result: object) -> None:
        frontend_submission = (
            request_id in self._frontend_submission_requests
            or request_id in self._frontend_unknown_requests
        )
        if frontend_submission:
            self._frontend_submission_requests.discard(request_id)
            self._frontend_unknown_requests.discard(request_id)
        payload = normalize_order(result) if isinstance(result, dict) else {"result": result}
        context = payload.pop("_context", {})
        transport = payload.pop("_transport", "REST")
        if isinstance(context, dict) and context.get("emergency_close"):
            self._release_emergency_reservation(context)
            self.emergency_close_requests.pop(request_id, None)
        registered_client = self.protection_request_clients.pop(request_id, "")
        order_id = payload.get("orderId") or payload.get("algoId") or "—"
        status = str(
            payload.get("status")
            or payload.get("algoStatus")
            or payload.get("orderStatus")
            or "ACCEPTED"
        )
        rail_cancel = self._magnetic_rail_cancel_requests.pop(request_id, None)
        if rail_cancel is not None:
            cancel_chart = rail_cancel.get("chart")
            cancel_draft_id = int(rail_cancel.get("draft_id") or 0)
            if isinstance(cancel_chart, ChartWorkspace) and cancel_draft_id > 0:
                cancel_chart.resolve_order_rail_cancellation(
                    cancel_draft_id, canceled=True
                )
            # Refresh all symbols so partially filled entries can publish their
            # terminal fill/account state and any queued TP/SL plan can settle.
            self.trading_gateway.refresh_account(None, all_open_orders=True)

        rail_submission = self._magnetic_rail_requests.pop(request_id, None)
        if rail_submission is not None:
            rail_chart = rail_submission.get("chart")
            rail_draft_id = int(rail_submission.get("draft_id") or 0)
            terminal = {
                "FILLED", "FINISHED", "CANCELED", "EXPIRED",
                "EXPIRED_IN_MATCH", "REJECTED",
            }
            is_working = status.upper() not in terminal
            entry = dict(rail_submission.get("order") or {})
            working_order = {**entry, **payload}
            working_order["symbol"] = str(
                working_order.get("symbol") or rail_submission.get("symbol") or ""
            ).upper()
            working_order["_source"] = (
                "ALGO" if rail_submission.get("algo") or payload.get("algoId") else "STANDARD"
            )
            client_id = str(rail_submission.get("client_id") or "")
            if working_order["_source"] == "ALGO":
                working_order.setdefault(
                    "clientAlgoId",
                    payload.get("clientAlgoId") or payload.get("clientOrderId") or client_id,
                )
            else:
                working_order.setdefault(
                    "clientOrderId",
                    payload.get("clientOrderId") or client_id,
                )
            if isinstance(rail_chart, ChartWorkspace) and rail_draft_id > 0:
                rail_chart.resolve_order_rail_submission(
                    rail_draft_id,
                    accepted=True,
                    working=is_working,
                    working_order=working_order,
                )
                if rail_submission.get("cancel_requested"):
                    if is_working:
                        rail_chart.set_order_rail_cancellation_pending(
                            rail_draft_id, True
                        )
                        self._submit_magnetic_rail_cancel(
                            str(rail_submission.get("symbol") or ""),
                            rail_chart,
                            rail_draft_id,
                            working_order,
                            client_id=client_id,
                            algo_hint=bool(rail_submission.get("algo")),
                        )
                    else:
                        rail_chart.resolve_order_rail_cancellation(
                            rail_draft_id, canceled=True
                        )
        filled = safe_float(
            payload.get("executedQty")
            or payload.get("cumQty")
            or payload.get("aq")
        )
        average = safe_float(payload.get("avgPrice") or payload.get("averagePrice"))
        message = (
            f"{request_id} · {transport} · ID {order_id} · {status} · "
            f"filled {human_number(filled)}"
        )
        if average:
            message += f" @ {format_price(average)}"
        self.alerts_panel.append_alert("BINANCE ORDER UPDATE", message)
        self.statusBar().showMessage(message, 8000)
        if frontend_submission:
            self._refresh_frontend_submission_state(
                status.upper() or "ACCEPTED", message, request_id
            )

        if isinstance(context, dict) and context.get("protections"):
            entry = dict(context.get("entry") or {})
            client_id = str(
                payload.get("clientOrderId")
                or payload.get("clientAlgoId")
                or entry.get("newClientOrderId")
                or entry.get("clientAlgoId")
                or registered_client
                or request_id
            )
            pending = self.pending_protections.get(client_id) or self.pending_protections.get(registered_client) or {**context, "client_id": client_id}
            pending["client_id"] = client_id
            for key in ('algoId', 'orderId', 'actualOrderId'):
                if payload.get(key) not in (None, '', '0', 0):
                    pending[key] = payload[key]
            terminal = {
                "FILLED", "FINISHED", "CANCELED", "EXPIRED",
                "EXPIRED_IN_MATCH", "REJECTED",
            }
            is_terminal = status.upper() in terminal and not payload.get('_awaiting_child')
            if filled > 0:
                self._submit_protection_plan(pending, filled, terminal=is_terminal)
            elif is_terminal:
                self.submitted_protection_clients.add(client_id)
                self.pending_protections.pop(client_id, None)
                if registered_client:
                    self.pending_protections.pop(registered_client, None)
            else:
                if registered_client and registered_client != client_id:
                    self.pending_protections.pop(registered_client, None)
                self.pending_protections[client_id] = pending

    def _trade_request_failed(
        self,
        request_id: str,
        message: str,
        uncertain: bool,
    ) -> None:
        frontend_submission = (
            request_id in self._frontend_submission_requests
            or request_id in self._frontend_unknown_requests
        )
        self._frontend_submission_requests.discard(request_id)
        if uncertain and frontend_submission:
            self._frontend_unknown_requests.add(request_id)
        elif not uncertain:
            self._frontend_unknown_requests.discard(request_id)
        failure_context = self.trading_gateway.failure_context(request_id)

        rail_cancel = self._magnetic_rail_cancel_requests.get(request_id)
        if rail_cancel is not None:
            cancel_chart = rail_cancel.get("chart")
            cancel_draft_id = int(rail_cancel.get("draft_id") or 0)
            # A cancel failure can race a fill, expiry or another cancellation.
            # Keep the rail locked until an authoritative ALL-open-orders
            # snapshot establishes whether the order still exists. This also
            # prevents a second Delete press from emitting duplicate cancels.
            rail_cancel["reconcile"] = True
            rail_cancel["uncertain"] = bool(uncertain)
            rail_cancel["failure_message"] = str(message)
            if isinstance(cancel_chart, ChartWorkspace) and cancel_draft_id > 0:
                cancel_chart.set_order_rail_cancellation_pending(
                    cancel_draft_id, True
                )
            self.trading_gateway.refresh_account(None, all_open_orders=True)

        rail_submission = (
            self._magnetic_rail_requests.get(request_id)
            if uncertain
            else self._magnetic_rail_requests.pop(request_id, None)
        )
        if rail_submission is not None:
            rail_chart = rail_submission.get("chart")
            rail_draft_id = int(rail_submission.get("draft_id") or 0)
            if isinstance(rail_chart, ChartWorkspace) and rail_draft_id > 0:
                if uncertain:
                    # The transport reported an error/outcome-unknown state. Keep
                    # the draft locked for reconciliation, but retract the optimistic
                    # Ctrl+A armed presentation until Binance state is confirmed.
                    rail_chart.set_order_rail_arm_submission_preview(
                        rail_draft_id, False, animated=True
                    )
                    rail_chart.set_order_rail_submission_pending(rail_draft_id, True)
                    if rail_submission.get("cancel_requested"):
                        rail_chart.set_order_rail_cancellation_pending(
                            rail_draft_id, True
                        )
                else:
                    rail_chart.resolve_order_rail_submission(
                        rail_draft_id,
                        accepted=False,
                        working=False,
                    )
                    if rail_submission.get("cancel_requested"):
                        rail_chart.resolve_order_rail_cancellation(
                            rail_draft_id, canceled=True
                        )
        if failure_context.get("emergency_close"):
            if not uncertain:
                self._release_emergency_reservation(failure_context)
                self.emergency_close_requests.pop(request_id, None)
            self.alerts_panel.append_alert(
                "EMERGENCY CLOSE NOT CONFIRMED",
                f"{failure_context.get('symbol', '—')} · {message} · no retry was sent.",
            )
        client_id = self.protection_request_clients.pop(request_id, "")
        if client_id and not uncertain:
            self.pending_protections.pop(client_id, None)
        title = "ORDER OUTCOME UNKNOWN" if uncertain else "ORDER REJECTED"
        if not uncertain and failure_context.get('protection_obsolete'):
            title = 'PROTECTION CANCELED'
        detail = f"{request_id} · {message}"
        if uncertain:
            detail += " · No retry was attempted; inspect orders and fills."
        self.alerts_panel.append_alert(title, detail)
        self.statusBar().showMessage(detail, 15_000)
        if frontend_submission:
            self._refresh_frontend_submission_state(
                "OUTCOME UNKNOWN" if uncertain else "REJECTED", detail, request_id
            )
        if failure_context.get("protective_leg") and not uncertain:
            self.active_protection_legs.pop(
                str(failure_context.get("client_id") or ""), None
            )
            if not failure_context.get('protection_obsolete'):
                self._start_emergency_close(failure_context, message)

    def _restore_saved_protections(self, recovered: dict) -> None:
        """Restore monitoring and cumulative allocation before processing new fills."""
        entries = []
        for record in recovered.get("records", []):
            client_id = record["client_id"]
            context = dict(record.get("context") or {})
            result = normalize_order(record.get('result') or {})
            status = str(result.get("status") or result.get("algoStatus") or "").upper()
            if record["kind"] == "leg":
                if status in {"NEW", "PARTIALLY_FILLED", "TRIGGERED", 'TRIGGERING', 'PENDING_NEW'}:
                    self.active_protection_legs[client_id] = context
                elif (status in {'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}
                      or (status == 'FINISHED' and result.get('_child_missing')
                          and safe_float(result.get('executedQty')) < safe_float(context.get('quantity')))):
                    self.active_protection_legs.pop(client_id, None)
                    self.alerts_panel.append_alert("RECOVERED PROTECTION REJECTED", f"{client_id} · inspect position risk before resuming trading")
                    if self.trading_gateway.has_open_position(str(context.get('symbol') or ''), str(context.get('position_side') or 'BOTH')):
                        self._start_emergency_close(context, f'Saved protection ended with {status} while offline or disconnected.')
                else:
                    self.active_protection_legs.pop(client_id, None)
            else:
                context["client_id"] = client_id
                context['_allocation_restored'] = True
                previous = self.pending_protections.get(client_id, {})
                context["filled_quantity"] = max(safe_float(context.get("filled_quantity")), safe_float(previous.get("filled_quantity")), safe_float(result.get("executedQty") or result.get("cumQty") or result.get("aq")))
                context["protected_quantity"] = max(safe_float(context.get("protected_quantity")), safe_float(previous.get("protected_quantity")))
                self.pending_protections[client_id] = context
                entries.append((context, status))
        if recovered.get("errors"):
            self.alerts_panel.append_alert("PROTECTION RECOVERY NEEDS REVIEW", "\n".join(recovered["errors"]))
        for context, status in entries:
            symbol = str((context.get('entry') or {}).get('symbol') or '')
            position_side = str((context.get('entry') or {}).get('positionSide') or 'BOTH')
            if (status in {"FILLED", "FINISHED", "CANCELED", "EXPIRED", 'EXPIRED_IN_MATCH', "REJECTED"}
                    and (context['filled_quantity'] <= 0 or not self.trading_gateway.has_open_position(symbol, position_side))):
                self.submitted_protection_clients.add(str(context['client_id']))
                self.pending_protections.pop(str(context['client_id']), None)
                continue
            if context["filled_quantity"] > 0:
                self._submit_protection_plan(context, context["filled_quantity"], terminal=status in {"FILLED", "FINISHED", "CANCELED", "EXPIRED", 'EXPIRED_IN_MATCH', "REJECTED"})
        self.alerts_panel.append_alert("PROTECTION MONITORING RESTORED", f"{len(entries)} saved entries · {len(self.active_protection_legs)} active legs reconciled by client ID")

    def _submit_protection_plan(
        self, context: dict[str, Any], filled_quantity: float, *, terminal: bool = True
    ) -> None:
        """Extend protection for newly filled quantity without canceling live legs.

        REST and account-stream updates contain cumulative fills. Reserving each
        tranche before submission makes duplicate/out-of-order updates harmless.
        Existing legs stay live while the additional tranche is acknowledged.
        """
        client_id = str(context.get("client_id", ""))
        if client_id in self.submitted_protection_clients:
            return
        pending = self.pending_protections.setdefault(client_id, context)
        if self.trading_gateway._protection_recovery_pending and not pending.get('_allocation_restored'):
            pending["filled_quantity"] = max(safe_float(pending.get("filled_quantity")), filled_quantity)
            self._set_ticket_protection_state(str((pending.get("entry") or {}).get("symbol") or ""), "PROTECTION RECOVERY PENDING")
            return
        filled = max(safe_float(pending.get("filled_quantity")), filled_quantity)
        protected = safe_float(pending.get("protected_quantity"))
        pending["filled_quantity"] = filled
        delta = Decimal(str(filled)) - Decimal(str(protected))
        if delta <= 0:
            if terminal:
                self.submitted_protection_clients.add(client_id)
                self.pending_protections.pop(client_id, None)
            return
        entry = dict(pending.get("entry") or {})
        plans = pending.get("protections") or {}
        symbol = str(entry.get("symbol") or "")
        rules = pending.get("rules")
        if not isinstance(rules, SymbolRules):
            rules = self.symbol_rules.get(symbol)
        position_side = str(entry.get("positionSide", "BOTH"))
        opposite = "SELL" if entry.get("side") == "BUY" else "BUY"
        self._set_ticket_protection_state(symbol, "PROTECTION SUBMITTING")
        try:
            if not isinstance(rules, SymbolRules):
                raise ValueError("Exchange symbol rules are unavailable.")
            prepared_sizes = {}
            for kind in ("tp", "sl"):
                targets = [dict(target) for target in list(plans.get(kind, []))[:4]]
                for target in targets:
                    target["price"] = validate_step(str(target.get("price", "")), rules.tick_size, "Protection trigger", offset=str(rules.min_price))
                prepared_sizes[kind] = (targets, protection_quantities(float(delta), targets, rules))
        except ValueError as exc:
            if not terminal and "quantity" in str(exc).lower():
                self._set_ticket_protection_state(symbol, "PARTIAL FILL · WAITING FOR MINIMUM PROTECTION LOT")
                return
            if terminal:
                self.submitted_protection_clients.add(client_id)
            self.alerts_panel.append_alert("PROTECTION PLAN INVALID", f"{symbol} · {exc}")
            self._start_emergency_close({
                "kind": "tp/sl", "symbol": symbol, "entry_side": str(entry.get("side") or ""),
                "position_side": position_side, "quantity": float(delta), "rules": rules,
                "client_id": f"{client_id}:{filled}",
            }, str(exc))
            if terminal:
                self.pending_protections.pop(client_id, None)
            return
        # One durable tranche reservation precedes every leg transmission.
        pending["protected_quantity"] = filled
        if terminal:
            self.submitted_protection_clients.add(client_id)
        prepared_legs = []
        for kind, order_type in (("tp", "TAKE_PROFIT_MARKET"), ("sl", "STOP_MARKET")):
            targets, quantities = prepared_sizes[kind]
            for target_index, (target, quantity) in enumerate(zip(targets, quantities)):
                leg_client_id = self.trading_gateway.client_order_id("nwtp" if kind == "tp" else "nwsl")
                order = {
                    "symbol": symbol, "side": opposite, "positionSide": position_side,
                    "type": order_type, "quantity": quantity, "triggerPrice": str(target["price"]),
                    "workingType": "MARK_PRICE", "priceProtect": True, "newClientOrderId": leg_client_id,
                }
                if position_side == "BOTH":
                    order["reduceOnly"] = True
                leg_context = {
                    "protective_leg": True, "kind": kind, "symbol": symbol,
                    "entry_side": str(entry.get("side") or ""), "position_side": position_side,
                    "quantity": quantity, "rules": rules, "entry_client_id": client_id,
                    "client_id": leg_client_id, "fill_total": filled,
                    "tranche_quantity": str(delta), "target_index": target_index,
                }
                self.active_protection_legs[leg_client_id] = dict(leg_context)
                prepared_legs.append((order, leg_context))
        self.trading_gateway.submit_protection_tranche(dict(pending), prepared_legs)
        if terminal:
            self.pending_protections.pop(client_id, None)
        self._set_ticket_protection_state(symbol, f"PROTECTION QUEUED · {len(prepared_legs)} NEW LEGS")
        self.statusBar().showMessage(f"Entry fill {filled:g} · queued {len(prepared_legs)} additional protection legs.", 9000)

    def _start_emergency_close(
        self,
        context: dict[str, Any],
        rejection: str,
    ) -> None:
        symbol = str(context.get("symbol") or "")
        position_side = str(context.get("position_side") or "BOTH")
        entry_side = str(context.get("entry_side") or "BUY").upper()
        tranche = str(context.get('entry_client_id') or '') + ':' + str(context.get('fill_total') or '')
        if context.get('entry_client_id') and context.get('fill_total'):
            if tranche in self._emergency_tranches:
                return
            self._emergency_tranches.add(tranche)
        try:
            requested = safe_float(self.trading_gateway.remaining_protection_quantity(context))
        except (ValueError, ArithmeticError):
            self.alerts_panel.append_alert('PROTECTION FAILURE · CLOSE NOT SENT', f'{symbol} · remaining tranche quantity could not be verified.')
            return
        rules = context.get("rules")
        if not isinstance(rules, SymbolRules):
            rules = self.symbol_rules.get(symbol)
        if not symbol or requested <= 0:
            self.alerts_panel.append_alert(
                "PROTECTION FAILURE · CLOSE NOT SENT",
                "The rejected protection leg did not contain a valid symbol and quantity.",
            )
            return
        if not isinstance(rules, SymbolRules):
            self.alerts_panel.append_alert(
                "PROTECTION FAILURE · CLOSE NOT SENT",
                f"{symbol} · exchange symbol rules are unavailable; automatic close quantity cannot be validated safely.",
            )
            hub = getattr(self, "hub", None)
            if hub is not None and hasattr(hub, "request_universe_refresh"):
                hub.request_universe_refresh()
            return
        reserve_key = (symbol, position_side)
        cached_position = self.trading_gateway.position_cache.get(reserve_key) or {}
        position_amount = abs(safe_float(cached_position.get("positionAmt")))
        already_reserved = self.emergency_reserved.get(reserve_key, 0.0)
        available = max(0.0, (position_amount or requested) - already_reserved)
        close_amount = min(requested, available)
        if close_amount <= 0:
            self.alerts_panel.append_alert(
                "PROTECTION FAILURE · NO OPEN AMOUNT",
                f"{symbol} · no unreserved position amount remained for this rejected leg.",
            )
            return
        try:
            if not math.isfinite(safe_float(rules.market_step)) or safe_float(rules.market_step) <= 0:
                raise ValueError("The exchange market quantity step is invalid.")
            quantity = quantize_step(str(close_amount), rules.market_step, offset=str(rules.min_market_qty))
            if not (0 < safe_float(quantity) and rules.min_market_qty <= safe_float(quantity) <= rules.max_market_qty):
                raise ValueError("The emergency close quantity is outside the exchange market-order range.")
        except ValueError as exc:
            detail = f"{symbol} · {exc} · automatic protection could not be established."
            self._set_ticket_protection_state(symbol, "PROTECTION FAILURE · CLOSE NOT SENT")
            self.alerts_panel.append_alert("PROTECTION FAILURE · CLOSE NOT SENT", detail)
            self.statusBar().showMessage(detail, 15000)
            return
        guard_id = str(context.get("client_id") or f"guard-{time.time_ns()}")
        if guard_id in self.emergency_guards:
            return
        self.emergency_reserved[reserve_key] = already_reserved + safe_float(quantity)
        self.emergency_guards[guard_id] = {
            "guard_id": guard_id,
            "symbol": symbol,
            "position_side": position_side,
            "entry_side": entry_side,
            "quantity": quantity,
            "rules": rules,
            "reserve_key": reserve_key,
            "reserved": safe_float(quantity),
            "started": time.monotonic(),
            "deadline": time.monotonic() + 1.5,
            "previous": None,
            "adverse_ticks": 0,
            "last_tick": 0.0,
            "rejection": rejection,
        }
        self._submit_emergency_close(guard_id, "confirmed protection rejection: " + rejection)
        self._set_ticket_protection_state(symbol, "PROTECTION REJECTED · FAIL-SAFE CLOSE REQUESTED")
        self.alerts_panel.append_alert(
            "PROTECTION REJECTED · FAIL-SAFE ACTIVE",
            f"{symbol} · {context.get('kind', 'TP/SL').upper()} · amount {quantity} · "
            "requesting one immediate risk-reducing market close through the durable gateway.",
        )

    def _observe_emergency_mark(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        now = time.monotonic()
        for guard_id, guard in list(self.emergency_guards.items()):
            if guard.get("symbol") != symbol:
                continue
            previous = guard.get("previous")
            guard["previous"] = price
            guard["last_tick"] = now
            if previous is None:
                continue
            long_position = guard.get("entry_side") == "BUY"
            adverse = price <= safe_float(previous) if long_position else price >= safe_float(previous)
            guard["adverse_ticks"] = int(guard.get("adverse_ticks", 0)) + 1 if adverse else 0
            if guard["adverse_ticks"] >= 2:
                self._submit_emergency_close(
                    guard_id,
                    "two consecutive adverse/flat mark-price ticks",
                )

    def _check_emergency_guards(self) -> None:
        now = time.monotonic()
        for guard_id, guard in list(self.emergency_guards.items()):
            if now >= safe_float(guard.get("deadline")):
                reason = (
                    "mark feed unavailable or stale"
                    if not guard.get("last_tick")
                    else "1.5-second protection window expired"
                )
                self._submit_emergency_close(guard_id, reason)
        if not self.emergency_guards:
            self.emergency_timer.stop()

    def _release_emergency_reservation(self, context: dict[str, Any]) -> None:
        key_value = context.get("reserve_key")
        if not isinstance(key_value, (tuple, list)) or len(key_value) != 2:
            return
        key = (str(key_value[0]), str(key_value[1]))
        remaining = max(
            0.0,
            self.emergency_reserved.get(key, 0.0) - safe_float(context.get("reserved")),
        )
        if remaining > 0:
            self.emergency_reserved[key] = remaining
        else:
            self.emergency_reserved.pop(key, None)

    def _submit_emergency_close(self, guard_id: str, reason: str) -> None:
        guard = self.emergency_guards.pop(guard_id, None)
        if guard is None:
            return
        symbol = str(guard["symbol"])
        position_side = str(guard["position_side"])
        side = "SELL" if guard["entry_side"] == "BUY" else "BUY"
        order: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": guard["quantity"],
            "positionSide": position_side,
            "newClientOrderId": self.trading_gateway.client_order_id("nwec"),
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        context = {
            "emergency_close": True,
            "reason": reason,
            "reserve_key": guard["reserve_key"],
            "reserved": guard["reserved"],
            "symbol": symbol,
            "rules": guard["rules"],
        }
        request_id = self.trading_gateway.submit_order(order, context)
        admitted = self.trading_gateway.request_was_admitted(request_id)
        if admitted:
            self.emergency_close_requests[request_id] = context
            self._set_ticket_protection_state(symbol, "FAIL-SAFE CLOSE SENT")
            self.alerts_panel.append_alert(
                "EMERGENCY CLOSE SENT",
                f"{symbol} · {side} MARKET {guard['quantity']} · {reason} · one transmission only.",
            )
        else:
            self._release_emergency_reservation(context)
        if not self.emergency_guards:
            self.emergency_timer.stop()

    def _trading_account_event(self, event: dict[str, Any]) -> None:
        self._sync_ticker_streams()
        if event.get("e") == "ORDER_TRADE_UPDATE":
            order = normalize_order(event.get('o') or {})
            client_id = self.trading_gateway.protection_client_id(order)
            status = str(order.get("X") or "").upper()
            terminal = {
                "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"
            }
            active_leg = self.active_protection_legs.get(client_id)
            if active_leg and status in {'REJECTED', 'EXPIRED_IN_MATCH', 'EXPIRED'}:
                self.active_protection_legs.pop(client_id, None)
                if safe_float(order.get('executedQty')) < safe_float(order.get('origQty')):
                    self._start_emergency_close(active_leg, f'Protection matching-engine order ended with {status}.')
            elif active_leg and status in {'FILLED', 'CANCELED'}:
                self.active_protection_legs.pop(client_id, None)
            if client_id in self.pending_protections:
                filled = safe_float(
                    order.get("z")
                    or order.get("executedQty")
                    or order.get("cumQty")
                )
                if filled > 0:
                    self._submit_protection_plan(
                        self.pending_protections[client_id], filled, terminal=status in terminal
                    )
                elif status in terminal:
                    self.pending_protections.pop(client_id, None)
                    self.submitted_protection_clients.add(client_id)
        elif event.get("e") == "ALGO_UPDATE":
            algo = normalize_order(event.get('o') or event.get('a') or event.get('algoOrder') or event)
            client_id = self.trading_gateway.protection_client_id(algo)
            status = str(
                algo.get("X")
                or algo.get("orderStatus")
                or algo.get("status")
                or event.get("X")
                or event.get("status")
                or ""
            ).upper()
            active_leg = self.active_protection_legs.get(client_id)
            if active_leg and status in {'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}:
                self.active_protection_legs.pop(client_id, None)
                reason = str(
                    algo.get("rm")
                    or algo.get("rejectReason")
                    or f"Binance protection ended with {status}."
                )
                self._start_emergency_close(active_leg, reason)
            elif active_leg and status == 'CANCELED':
                self.active_protection_legs.pop(client_id, None)
            terminal = {"FILLED", "FINISHED", "CANCELED", "EXPIRED", "REJECTED"}
            if client_id in self.pending_protections:
                filled = safe_float(
                    algo.get("aq")
                    or algo.get("z")
                    or algo.get("executedQty")
                    or algo.get("cumQty")
                )
                if filled > 0:
                    self._submit_protection_plan(
                        self.pending_protections[client_id], filled, terminal=status in terminal
                    )
                elif status in terminal and status != 'FINISHED':
                    self.pending_protections.pop(client_id, None)
                    self.submitted_protection_clients.add(client_id)
            self.alerts_panel.append_alert(
                "ALGO ORDER UPDATE",
                f"{algo.get('s', event.get('s', self.current_symbol))} · {status or 'UPDATED'}",
            )
        elif event.get("e") == "CONDITIONAL_ORDER_TRIGGER_REJECT":
            rejected = event.get("or") or event.get("o") or event
            client_id = self.trading_gateway.protection_client_id(rejected)
            active_leg = self.active_protection_legs.pop(client_id, None)
            if active_leg:
                reason = str(
                    rejected.get("r")
                    or rejected.get("rm")
                    or rejected.get("rejectReason")
                    or "Binance rejected the protection when it triggered."
                )
                self._start_emergency_close(active_leg, reason)

    @staticmethod
    def _magnetic_rail_snapshot_orders(snapshot: dict[str, Any]) -> list[dict[str, Any]] | None:
        if str(snapshot.get("ordersScope") or "").upper() != "ALL":
            return None
        rows: list[dict[str, Any]] = []
        for source, values in (
            ("STANDARD", snapshot.get("orders", [])),
            ("ALGO", snapshot.get("algoOrders", [])),
        ):
            for row in values if isinstance(values, list) else []:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or row.get("algoStatus") or "NEW").upper()
                if status not in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
                    continue
                rows.append({**row, "_source": source})
        return rows

    @staticmethod
    def _magnetic_rail_row_for_client(
        rows: list[dict[str, Any]], symbol: str, client_id: str
    ) -> dict[str, Any] | None:
        if not client_id:
            return None
        symbol = str(symbol).upper()
        for row in rows:
            if str(row.get("symbol") or "").upper() != symbol:
                continue
            candidate = str(
                row.get("clientOrderId")
                or row.get("clientAlgoId")
                or row.get("origClientOrderId")
                or ""
            )
            if candidate == client_id:
                return dict(row)
        return None

    @staticmethod
    def _magnetic_rail_reference_is_open(
        rows: list[dict[str, Any]], reference: dict[str, Any], client_id: str = ""
    ) -> bool:
        source = str(reference.get("_source") or "").upper()
        symbol = str(reference.get("symbol") or "").upper()
        ids = {
            str(value)
            for value in (
                reference.get("orderId"),
                reference.get("algoId"),
                reference.get("clientOrderId"),
                reference.get("clientAlgoId"),
                client_id,
            )
            if value not in (None, "")
        }
        if not ids:
            return True
        for row in rows:
            if symbol and str(row.get("symbol") or "").upper() != symbol:
                continue
            if source and str(row.get("_source") or "").upper() != source:
                continue
            row_ids = {
                str(value)
                for value in (
                    row.get("orderId"),
                    row.get("algoId"),
                    row.get("clientOrderId"),
                    row.get("clientAlgoId"),
                )
                if value not in (None, "")
            }
            if ids & row_ids:
                return True
        return False

    def _reconcile_magnetic_rail_requests(self, snapshot: dict[str, Any]) -> None:
        rows = self._magnetic_rail_snapshot_orders(snapshot)
        if rows is None:
            return

        # A placement request with an uncertain transport outcome can still be
        # identified unambiguously from the client ID that was assigned before
        # transmission. If it appears in the authoritative open-order snapshot,
        # arm that exact rail and honor any queued delete immediately.
        for request_id, submission in tuple(self._magnetic_rail_requests.items()):
            client_id = str(submission.get("client_id") or "")
            row = self._magnetic_rail_row_for_client(
                rows, str(submission.get("symbol") or ""), client_id
            )
            if row is None:
                continue
            chart = submission.get("chart")
            draft_id = int(submission.get("draft_id") or 0)
            if not isinstance(chart, ChartWorkspace) or draft_id <= 0:
                continue
            self._magnetic_rail_requests.pop(request_id, None)
            self._frontend_submission_requests.discard(request_id)
            self._frontend_unknown_requests.discard(request_id)
            chart.resolve_order_rail_submission(
                draft_id, accepted=True, working=True, working_order=row
            )
            if submission.get("cancel_requested"):
                chart.set_order_rail_cancellation_pending(draft_id, True)
                self._submit_magnetic_rail_cancel(
                    str(submission.get("symbol") or ""),
                    chart,
                    draft_id,
                    row,
                    client_id=client_id,
                    algo_hint=bool(submission.get("algo")),
                )

        # Failed/uncertain cancel transports are concluded from authoritative
        # account state. A normal in-flight cancel remains owned by its direct
        # request response.
        for cancel_id, pending in tuple(self._magnetic_rail_cancel_requests.items()):
            if not pending.get("reconcile"):
                continue
            reference = pending.get("working_order")
            if not isinstance(reference, dict):
                reference = {}
            if not reference.get("symbol") and pending.get("symbol"):
                reference = {**reference, "symbol": pending.get("symbol")}
            is_open = self._magnetic_rail_reference_is_open(
                rows, reference, str(pending.get("client_id") or "")
            )
            self._magnetic_rail_cancel_requests.pop(cancel_id, None)
            chart = pending.get("chart")
            draft_id = int(pending.get("draft_id") or 0)
            if isinstance(chart, ChartWorkspace) and draft_id > 0:
                chart.resolve_order_rail_cancellation(
                    draft_id, canceled=not is_open
                )
            self._frontend_unknown_requests.discard(cancel_id)
            if is_open:
                qualifier = (
                    "NOT CONFIRMED" if pending.get("uncertain") else "FAILED"
                )
                detail = str(pending.get("failure_message") or "").strip()
                suffix = f" · {detail}" if detail else ""
                self.statusBar().showMessage(
                    f"RAIL CANCEL {qualifier} · {pending.get('symbol', '—')} order is still open{suffix}",
                    9000,
                )

    def _sync_trading_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Retain shared account state and chart-rail reconciliation."""
        if not isinstance(snapshot, dict):
            return
        self._latest_trading_snapshot = dict(snapshot)
        self._reconcile_magnetic_rail_requests(self._latest_trading_snapshot)

    @staticmethod
    def _working_orders_for_symbol(
        snapshot: dict[str, Any], symbol: str
    ) -> list[dict[str, Any]]:
        symbol = str(symbol or "").upper()
        visible: list[dict[str, Any]] = []
        for row in snapshot.get("orders", []):
            if str(row.get("symbol") or "").upper() == symbol:
                visible.append({**dict(row), "_source": "STANDARD"})
        for row in snapshot.get("algoOrders", []):
            if str(row.get("symbol") or "").upper() == symbol:
                visible.append({**dict(row), "_source": "ALGO"})
        return visible

    def _sync_auxiliary_chart_working_orders(
        self, symbol: str, _interval: str, chart: ChartWorkspace
    ) -> None:
        if not isinstance(chart, ChartWorkspace):
            return
        symbol = self._normalize_symbol(symbol)
        chart.set_order_rail_market_symbol(symbol)
        chart.set_symbol_rules(self.symbol_rules.get(symbol, SymbolRules()))
        rows = self._working_orders_for_symbol(self._latest_trading_snapshot, symbol)
        chart.set_working_orders(
            rows, visible=getattr(self, "chart_orders_visible", True)
        )

    def _sync_chart_working_orders(self, snapshot: dict[str, Any]) -> None:
        if isinstance(snapshot, dict):
            self._latest_trading_snapshot = dict(snapshot)
        visible = self._working_orders_for_symbol(snapshot, self.current_symbol)
        self._chart_order_rows = visible
        # Auxiliary charts share the same account/order authority. Their rail
        # visuals are symbol-local presentations of the exact same Binance open
        # orders and therefore rehydrate on ticker changes without local copies.
        container = getattr(self, "chart_container", None)
        if container is not None:
            for pane in tuple(getattr(container, "auxiliary", ())):
                pane.chart.set_order_rail_market_symbol(pane.symbol)
                pane.chart.set_symbol_rules(
                    self.symbol_rules.get(pane.symbol, SymbolRules())
                )
                pane.chart.set_working_orders(
                    self._working_orders_for_symbol(snapshot, pane.symbol),
                    visible=getattr(self, "chart_orders_visible", True),
                )
        signature = tuple(
            (
                str(row.get("symbol") or ""),
                str(row.get("_source") or ""),
                str(row.get("orderId") or ""),
                str(row.get("algoId") or ""),
                str(row.get("clientOrderId") or ""),
                str(row.get("clientAlgoId") or ""),
                str(row.get("status") or row.get("algoStatus") or ""),
                str(row.get("side") or ""),
                str(row.get("type") or row.get("orderType") or row.get("algoType") or ""),
                str(row.get("price") or ""),
                str(row.get("actualPrice") or ""),
                str(row.get("triggerPrice") or ""),
                str(row.get("stopPrice") or ""),
                str(row.get("activatePrice") or row.get("activationPrice") or ""),
                str(row.get("origQty") or ""),
                str(row.get("quantity") or ""),
                str(row.get("totalQty") or ""),
                str(row.get("executedQty") or ""),
                str(row.get("cumQty") or ""),
                str(row.get("reduceOnly") or ""),
                str(row.get("closePosition") or ""),
                str(row.get("positionSide") or ""),
                str(row.get("timeInForce") or ""),
            )
            for row in visible
        )
        if signature == self._chart_orders_signature:
            return
        self._chart_orders_signature = signature
        self.chart.set_working_orders(
            visible,
            visible=getattr(self, "chart_orders_visible", True),
        )

    def _toggle_chart_orders(self) -> None:
        self.chart_orders_visible = not getattr(self, "chart_orders_visible", True)
        self.chart.set_working_orders(
            getattr(self, "_chart_order_rows", []),
            visible=self.chart_orders_visible,
        )
        for pane in tuple(getattr(self.chart_container, "auxiliary", ())):
            pane.chart.set_working_orders(
                self._working_orders_for_symbol(self._latest_trading_snapshot, pane.symbol),
                visible=self.chart_orders_visible,
            )
        self.statusBar().showMessage("CHART ORDERS " + ("ON" if self.chart_orders_visible else "OFF"), 1800)

    def _modify_order_from_chart(self, order: dict[str, Any]) -> None:
        self.rail_amendments.amend(order)

    def _prefill_order_price(self, price: float) -> None:
        applied = False
        target = "PRICE"
        for ticket in self._trading_tickets():
            if hasattr(ticket, "apply_external_price_prefill"):
                changed, target = ticket.apply_external_price_prefill(price)
                applied = applied or changed
        if applied:
            self.statusBar().showMessage(
                f"{target} PREFILLED · {format_price(price)}", 2500
            )
        else:
            self.statusBar().showMessage(
                f"{target} KEPT · FIELD ALREADY HAS A VALUE · FOCUS FIELD TO REPLACE",
                3500,
            )

    def event(self, event: QtCore.QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QtCore.QEvent.Type.LayoutRequest:
            self._sync_shell_minimum_width()
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange:
            self._update_toolbar_icons()
            self.trading_workspace.apply_theme(self.ui_theme)
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            # Windows caption maximize/restore is consumed in nativeEvent and routed
            # through the same fullscreen action as F11. State changes that still
            # arrive here are cosmetic/native bookkeeping only.
            QTimer.singleShot(0, self._stabilize_fullscreen_chrome)
        return handled

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "right_rail_controller"):
            self.right_rail_controller.refresh_geometry_constraints()
        if (
            hasattr(self, "resize_settle_timer")
            and hasattr(self, "chart")
            and hasattr(self, "workspace_stack")
            and self.workspace_stack.currentIndex() == 0
        ):
            self._begin_ui_resize()

    def _begin_splitter_drag(self, splitter: object) -> None:
        self._active_splitter_drags.add(id(splitter))
        self._begin_ui_resize(explicit_drag=True)

    def _end_splitter_drag(self, splitter: object) -> None:
        flush = getattr(splitter, "_flush_pending_grab_resize", None)
        if callable(flush):
            flush()
        self._active_splitter_drags.discard(id(splitter))
        if self._active_splitter_drags:
            return
        if self.resize_settle_timer.isActive():
            self.resize_settle_timer.stop()
        self._finish_ui_resize(force=True)

    def _begin_ui_resize(self, *, explicit_drag: bool=False) -> None:
        if not self._ui_resize_active:
            self._ui_resize_active = True
            self._sync_background_priority()
            self.chart_container.begin_interactive_resize()
        if explicit_drag or self._active_splitter_drags:
            self.resize_settle_timer.stop()
        else:
            self.resize_settle_timer.start()

    def _note_ui_resize_activity(self) -> None:
        if not self._ui_resize_active:
            self._begin_ui_resize(explicit_drag=bool(self._active_splitter_drags))
        elif not self._active_splitter_drags:
            self.resize_settle_timer.start()

    @profile_callback("app.finish_ui_resize_ms")
    def _finish_ui_resize(self, *, force: bool=False) -> None:
        if self._active_splitter_drags and not force:
            return
        if hasattr(self, "resize_settle_timer") and self.resize_settle_timer.isActive():
            self.resize_settle_timer.stop()
        if not self._ui_resize_active:
            return
        self._ui_resize_active = False
        self._sync_background_priority()
        self.right_rail_controller.capture_geometry()
        self.right_rail_controller.sync_interaction_surfaces()
        self.chart_container.end_interactive_resize()
        if self.pending_ticker_symbols or self.ticker_rank_dirty:
            self.presentation_clock.request(immediate=True)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if getattr(self, "_shutdown_started", False):
            thread = getattr(self, "_order_flow_runtime_thread", None)
            if thread is not None and thread.isRunning():
                event.ignore()
            else:
                event.accept()
            return
        if self.market_event_task is not None or self.market_event_buffer or self._market_event_dropped:
            self._close_waiting_for_recorder = True
            self.trading_gateway.disarm()
            self.statusBar().showMessage("Closing · saving the local recorder in the background", 5000)
            self._flush_market_events()
            event.ignore()
            return
        if (
            self.market_history_dialog is not None
            and self.market_history_dialog.task is not None
        ):
            self.market_history_dialog.request_pause()
            if not self._close_after_market_history:
                self._close_after_market_history = True

                def finish_close() -> None:
                    dialog = self.market_history_dialog
                    if dialog is not None:
                        dialog.accept()
                    QTimer.singleShot(0, self.close)

                self.market_history_dialog.safe_to_close.connect(finish_close)
            event.ignore()
            return
        self._closing = True
        self._shutdown_started = True
        self._ticker_prepare_job.close()
        self.history_cancel_requested = True
        # Cancel reads/retries and disconnect every worker signal
        # before Qt child teardown so late queued completions cannot call back
        # into deleted chart/order-book/status widgets.
        for task in tuple(self.tasks):
            task.cancel()
            signals = getattr(task, "signals", None)
            if signals is None:
                continue
            for name in ("finished", "failed", "progress"):
                signal = getattr(signals, name, None)
                if signal is None:
                    continue
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
        self.tasks.clear()
        self.indicator_history_tasks.clear()
        self.lazy_chart_history_task = None
        self.history_task = None
        self.market_volume_task = None
        self.market_event_task = None
        self.search_hour_task = None
        self.watchlist_hour_task = None
        if self.market_board is not None:
            self.market_board.shutdown()
        self.rotation_overview.shutdown()
        if self.sector_overview is not None:
            self.sector_overview.shutdown()
        self.search_hour_timer.stop()
        self.search_hour_queue.clear()
        self._save_layout()
        self.market_event_timer.stop()
        hub = self.hub
        if hub is not None:
            hub.stop()
        self._stop_order_flow_runtime()
        self.chart_container.stop()
        self.trading_gateway.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        if self._order_flow_runtime_thread.isRunning():
            event.ignore()
        else:
            event.accept()
