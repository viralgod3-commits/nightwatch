"""Leaders calculations, history, replay, presentation, and workspace controller."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import statistics
import threading
import time
import zlib
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal
from .models import Candle, safe_float
from .chart.analysis import LatestJob, run_analysis
from .research import WorkspaceHistory
from .networking.binance import ApiTask
from .utilities import (
    ElidedLabel,
    TextRole,
    apply_text_render_hints,
    device_pixel_rect,
    set_text_role,
    hide_hover_tooltip,
    show_hover_tooltip,
    typography_font,
)
from .coin_catalog import coin_base_symbol, coin_remote_symbol, coin_icon_bytes, coin_name
from .presentation import display_frame_interval_ms, profile_callback


HOUR = 3_600_000
BENCHMARK = "BTCUSDT"
HISTORY_HOURS = 200  # 7-day span + 24-hour replay + warm-up
LEADERS_REFRESH_MS = 300_000
log = logging.getLogger(__name__)


LEADERS_PALETTE: dict[str, str] = {
    "bg": "#000000",
    "panel": "#000000",
    "panel2": "#000000",
    "grid": "#1C1C20",
    "border": "#262629",
    "separator": "#1F1F22",
    "control": "#000000",
    "control_hover": "#000000",
    "control_border": "#3A3A3F",
    "header": "#000000",
    "active": "#000000",
    "active_line": "#E070D8",
    "text": "#EDEDED",
    "muted": "#8E8E96",
    "green": "#22D27A",
    "red": "#FF4757",
    "cyan": "#E070D8",
    "amber": "#E8A64A",
}

SECTORS_PALETTE = dict(LEADERS_PALETTE)


def _amount(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"${value / scale:.1f}{suffix}"
    return f"${value:,.0f}"


def _stamp(end: int, full: bool = False) -> str:
    return datetime.fromtimestamp(end / 1000, timezone.utc).strftime(
        "%d %b %H:%M UTC" if full else "%H:%M"
    )


def _window(series: dict[int, Candle], end: int, hours: int) -> list[Candle]:
    keys = range(end - (hours - 1) * HOUR, end + 1, HOUR)
    rows = [series.get(key) for key in keys]
    if any(row is None or not all(math.isfinite(value) for value in
           (row.open, row.close, row.high, row.quote_volume))
           or row.open <= 0 or row.close <= 0 or row.quote_volume < 0 for row in rows):
        return []
    return rows


def _relative(series: dict[int, Candle], btc: dict[int, Candle], end: int, hours: int = 1) -> float | None:
    rows, base = _window(series, end, hours), _window(btc, end, hours)
    if not rows or not base:
        return None
    return ((rows[-1].close / rows[0].open) / (base[-1].close / base[0].open) - 1) * 100


def _metrics(series: dict[int, Candle], btc: dict[int, Candle], end: int, high_beta: bool = False) -> dict[str, Any]:
    rs1 = _relative(series, btc, end)
    rs4 = _relative(series, btc, end, 4)
    rs24 = _relative(series, btc, end, 24)
    recent = _relative(series, btc, end, 2)
    previous = _relative(series, btc, end - 2 * HOUR, 2)
    rows = _window(series, end, 4)
    rows24 = _window(series, end, 24)
    baseline = _window(series, end - HOUR, 24)
    last = series.get(end)
    median_volume = statistics.median(row.quote_volume for row in baseline) if baseline else 0
    rvol = last.quote_volume / median_volume if last and median_volume > 0 else None
    usd4 = (rows[-1].close / rows[0].open - 1) * 100 if rows else None
    usd24 = (rows24[-1].close / rows24[0].open - 1) * 100 if rows24 else None
    delta = recent - previous if recent is not None and previous is not None else None
    state = "Waiting"
    if all(value is not None for value in (rs1, rs4, recent, previous, usd4, rvol)):
        if previous > 0.5 and (delta < -0.20 or rs1 < 0):
            state = "Cooling"
        elif rs1 > 0.25 and delta > 0.20 and usd4 > 0 and rvol is not None and rvol >= 1.3:
            state = "Improving"
        elif rs4 > (0.75 if high_beta else 0.5) and recent >= 0 and usd4 > 0:
            state = "Leading"
        elif rs4 < -0.5:
            state = "Lagging"
        else:
            state = "Flat"
    prior_range = _window(series, end - HOUR, 4)
    high = max(row.high for row in prior_range) if prior_range else None
    distance = (1 - last.close / high) * 100 if high and last else None
    usd1 = (last.close / last.open - 1) * 100 if last and last.open > 0 else None


    score = None
    if rs4 is not None:
        score_value = 50.0
        score_value += 18.0 * math.tanh(rs4 / 1.5)
        if rs24 is not None:
            score_value += 12.0 * math.tanh(rs24 / 4.0)
        if rs1 is not None:
            score_value += 8.0 * math.tanh(rs1 / 0.8)
        if delta is not None:
            score_value += 7.0 * math.tanh(delta / 0.6)
        if rvol is not None:
            score_value += 5.0 * math.tanh((rvol - 1.0) / 1.0)
        score = int(round(max(0.0, min(100.0, score_value))))

    return {"rs1": rs1, "rs4": rs4, "rs24": rs24, "delta": delta,
            "rvol": rvol, "usd1": usd1, "usd4": usd4, "usd24": usd24,
            "state": state, "score": score,
            "price": last.close if last else None,
            "high_distance": distance}


def _load_batch(rest: Any, db: Any, symbols: list[str], end: int,
                cancel: threading.Event, generation: int, sync_clock: bool) -> dict[str, Any]:
    clock_offset = None
    if sync_clock and not cancel.is_set():
        server = rest.sync_time()
        clock_offset = server - time.time() * 1000
        end = int((server - 2000) // HOUR) * HOUR
    result: dict[str, Any] = {"generation": generation, "end": end,
                              "clock_offset": clock_offset, "series": {}, "spot": {}, "spot_errors": {}, "errors": {}}
    start = end - HISTORY_HOURS * HOUR
    for symbol in symbols:
        if cancel.is_set():
            break
        cached = db.load_candles(symbol, "1h", start, end - 1)
        by_end = {int(round(row.time * 1000)) + HOUR: row for row in cached}
        try:


            missing = next((key for key in range(start + HOUR, end + 1, HOUR)
                            if key not in by_end), None)
            request_start = min(end - 2 * HOUR, missing - HOUR) if missing else end - 2 * HOUR
            if cancel.is_set():
                break
            raw = rest.get("/fapi/v1/klines", {
                "symbol": symbol, "interval": "1h", "startTime": request_start,
                "endTime": end - 1, "limit": HISTORY_HOURS,
            }, priority="background")
            fetched = [Candle.from_rest(row) for row in raw
                       if len(row) > 7 and int(row[0]) + HOUR <= end]
            fetched = [row for row in fetched if all(math.isfinite(value) for value in (
                row.time, row.open, row.close, row.high, row.low, row.quote_volume
            )) and row.open > 0 and row.close > 0 and row.quote_volume >= 0]
            db.cache_candles(symbol, "1h", fetched)
            by_end.update({int(round(row.time * 1000)) + HOUR: row for row in fetched})
            if not fetched or max(int(row.time * 1000) + HOUR for row in fetched) < end:
                result["errors"][symbol] = "Latest completed hour unavailable"
        except (RuntimeError, TimeoutError, OSError, ValueError, TypeError) as exc:
            result["errors"][symbol] = str(exc)

            by_end.pop(end, None)
        result["series"][symbol] = {key: row for key, row in by_end.items() if start < key <= end}
        if symbol != BENCHMARK and not cancel.is_set():


            spot_interval = "spot:1h"
            spot_cached = db.load_candles(symbol, spot_interval, start, end - 1)
            spot_by_end = {
                int(round(row.time * 1000)) + HOUR: row
                for row in spot_cached
            }
            spot_complete = all(
                key in spot_by_end
                for key in range(start + HOUR, end + 1, HOUR)
            )
            if not spot_complete:
                try:
                    if cancel.is_set():
                        break
                    missing = next(key for key in range(start + HOUR, end + 1, HOUR)
                                   if key not in spot_by_end)
                    spot = rest.spot_hour_history(
                        symbol,
                        end,
                        hours=min(HISTORY_HOURS, max(2, (end - missing) // HOUR + 1)),
                    )
                    spot = [
                        row for row in spot
                        if int(round(row.time * 1000)) + HOUR <= end
                    ]
                    if spot:
                        db.cache_candles(symbol, spot_interval, spot)
                        spot_by_end.update({
                            int(round(row.time * 1000)) + HOUR: row
                            for row in spot
                        })
                except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
                    result["spot_errors"][symbol] = str(exc)
            result["spot"][symbol] = {
                key: row
                for key, row in spot_by_end.items()
                if start < key <= end
            }
    return result


def _spot_share(spot: dict[int, Candle], future: dict[int, Candle], end: int):
    windows = [_window(source, at, 4) for source in (spot, future) for at in (end, end - 4 * HOUR)]
    if not all(windows):
        return None, None
    a, b, c, d = [sum(row.quote_volume for row in rows) for rows in windows]
    if a + c <= 0 or b + d <= 0:
        return None, None
    share, previous = 100 * a / (a + c), 100 * b / (b + d)
    return share, share - previous


def _load_details(rest: Any, symbol: str, end: int, live: bool,
                  cancel: threading.Event) -> dict[str, Any]:
    result: dict[str, Any] = {"key": (symbol, end, live), "at": time.monotonic(), "errors": {}}
    requests = {
        "funding": ("/fapi/v1/fundingRate", {"symbol": symbol, "endTime": end, "limit": 16}),
        "oi": ("/futures/data/openInterestHist", {
            "symbol": symbol, "period": "1h", "endTime": end, "limit": 3,
        }),
    }
    if live:
        requests["book"] = ("/fapi/v1/depth", {"symbol": symbol, "limit": 1000})
    for name, (path, params) in requests.items():
        if cancel.is_set():
            result["cancelled"] = True
            return result
        try:
            result[name] = rest.get(path, params, priority="background")
            if name == "book":
                result["book_at"] = time.monotonic()
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            result["errors"][name] = str(exc)
    if not cancel.is_set():
        try:
            result["spot"] = rest.spot_hour_history(symbol, end)
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            result["errors"]["spot"] = str(exc)
    result["cancelled"] = cancel.is_set()
    return result


class _LeaderAnalysis:
    """Snapshot of completed histories and filter values, without Qt controls."""

    def __init__(self, state):
        self.__dict__.update(state)

    def identity(self, symbol: str) -> tuple[str, str]:
        base = coin_base_symbol(symbol)
        name = coin_name(base)

        category = "Benchmark" if symbol == BENCHMARK else (
            _sector_overview_classify(symbol, {}) or self.categories.get(symbol, "—"))
        return name, category

    def _ordered(self) -> list[str]:
        query = self.query.strip().casefold().replace("/", "").removesuffix(".p")
        symbols = [symbol for symbol in self.symbols if not query or
                   query in symbol.casefold() or query in self.identity(symbol)[0].casefold()]
        category = getattr(self, "category_filter_value", "All Sectors")
        if category != "All Sectors":
            symbols = [s for s in symbols if self.identity(s)[1] == category]
        state = getattr(self, "state_filter", "All")
        if state != "All":
            symbols = [s for s in symbols if self.metrics.get(s, {}).get("state") == state]
        mode = self.sort_mode or "state"
        descending = bool(getattr(self, "sort_descending", mode != "pair"))
        ranks = {"Leading": 0, "Improving": 1, "Cooling": 2, "Flat": 3, "Lagging": 4, "Waiting": 5}
        def value(symbol):
            item = self.metrics.get(symbol, {})
            if mode == "pair":
                return symbol.casefold()
            if mode == "name":
                return self.identity(symbol)[0].casefold()
            if mode == "sector":
                return self.identity(symbol)[1].casefold()
            if mode == "state":
                return ranks.get(item.get("state"), 5)
            return item.get(mode)
        # Stable alphabetical ties, and absent/non-finite values last in either direction.
        symbols.sort()
        available, missing = [], []
        for symbol in symbols:
            v = value(symbol)
            (missing if v is None or isinstance(v, (float, int)) and not math.isfinite(v)
             else available).append(symbol)
        if mode == "state":
            available.sort(key=lambda symbol: -(self.metrics.get(symbol, {}).get("score") or 0))
        available.sort(key=value, reverse=descending if mode != "state" else not descending)
        return available + missing


def _prepare_leaders(state, cached=None):
    model = _LeaderAnalysis(state)
    if cached is not None:
        model.metrics = cached["metrics"]
        return {**cached, "ordered": model._ordered(), "dashboard": _prepare_dashboard(model)}
    end, hours = model.cursor, model.hours
    btc = model.series.get(BENCHMARK, {})
    model.metrics = {symbol: _metrics(model.series.get(symbol, {}), btc, end,
                     model.identity(symbol)[1].lower() in {"ai", "meme", "memes"}) for symbol in model.symbols}
    for symbol, metrics in model.metrics.items():
        share, delta = _spot_share(model.spot_series.get(symbol, {}), model.series.get(symbol, {}), end)
        metrics.update(spot_share=share, spot_delta=delta)
        metrics["spot_confirmed"] = delta is not None and delta > 0
    cohort = {symbol: rows for symbol in model.symbols
              if (rows := _window(model.series.get(symbol, {}), end, 8))}
    current_total = sum(row.quote_volume for rows in cohort.values() for row in rows[4:])
    previous_total = sum(row.quote_volume for rows in cohort.values() for row in rows[:4])
    if current_total > 0 and previous_total > 0:
        for symbol, rows in cohort.items():
            share = sum(row.quote_volume for row in rows[4:]) / current_total * 100
            previous_share = sum(row.quote_volume for row in rows[:4]) / previous_total * 100
            model.metrics[symbol]["volume_share"] = share
            model.metrics[symbol]["share_delta"] = share - previous_share


    ordered = model._ordered()
    all_ordered = sorted(model.symbols, key=lambda symbol: (model.metrics[symbol].get("score") is None, -(model.metrics[symbol].get("score") if model.metrics[symbol].get("score") is not None else 0), symbol))
    candidates = {}
    for status in ("Leading", "Improving", "Cooling"):
        matches = [symbol for symbol in all_ordered if model.metrics[symbol]["state"] == status
                   and (status != "Cooling" or model.metrics[symbol].get("share_delta", 0) < 0)
                   and (status != "Improving" or model.metrics[symbol].get("spot_confirmed", False))]
        matches.sort(key=lambda symbol: model.metrics[symbol].get("delta") or 0, reverse=status == "Improving")
        candidates[status] = matches
    events = []
    for symbol in all_ordered:
        series = model.series.get(symbol, {})
        high_beta = model.identity(symbol)[1].lower() in {"ai", "meme", "memes"}
        previous = _metrics(series, btc, end - 4 * HOUR, high_beta)
        for at in range(end - 3 * HOUR, end + 1, HOUR):
            current = _metrics(series, btc, at, high_beta)
            if current["state"] != previous["state"] and current["state"] in {"Improving", "Cooling", "Leading"}:
                events.append((at, symbol, current["state"].lower()))
            elif current["rs1"] is not None and previous["rs1"] is not None and current["rs1"] > 0 >= previous["rs1"]:
                events.append((at, symbol, "turned positive vs BTC"))
            previous = current
    events.sort(key=lambda item: (-item[0], item[1]))
    return {
        "end": end, "metrics": model.metrics, "ordered": ordered,
        "all_ordered": all_ordered, "candidates": candidates, "events": events[:30],
        "cohort_count": len(cohort),
        "covered": sum(bool(_window(model.series.get(symbol, {}), end, hours)) for symbol in model.symbols),
        "btc_ready": bool(_window(btc, end, hours)),
        "sparks": {symbol: [model.series.get(symbol, {}).get(at).close if at in model.series.get(symbol, {}) else None
                            for at in range(end - 23 * HOUR, end + 1, HOUR)] for symbol in model.symbols},
        "paths": {symbol: [model.series.get(symbol, {}).get(at).close if at in model.series.get(symbol, {}) else None
                            for at in range(end - 23 * HOUR, end + 1, HOUR)] for symbol in model.symbols},
        "dashboard": _prepare_dashboard(model),
    }


def _workspace_analysis(function, state, histories=None):


    return histories.analyze(function, state) if histories is not None else run_analysis(function, state)


def _save_leaders_snapshot(db, name, state):
    try:
        state = dict(state)
        state["series"] = encode_histories(state["series"], state["end"])
        state["spot"] = encode_histories(state["spot"], state["end"])
        db.save_workspace_snapshot(name, state)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        log.warning("Could not save Leaders snapshot: %s", exc)


class LeadershipTimelineWidget(QtWidgets.QWidget):
    details_changed = Signal()
    symbol_selected = Signal(str)
    tracked_changed = Signal()
    data_changed = Signal()

    def __init__(
        self,
        _ignored_theme: dict[str, str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ):


        super().__init__(parent)
        self.setObjectName("leadershipTimeline")
        self.theme = dict(LEADERS_PALETTE)
        self.rest = self.db = self.watchlist = None
        self.can_load = lambda: True
        self.active = self.closing = False
        self._view_visible = True
        self._interaction_paused = False
        self._refresh_pending = False
        self._render_dirty = True
        self._load_pending = False
        self._cache_loading = False
        self._cache_task = None
        self._cache_name = "leaders:v1:live"
        self._next_refresh_at = time.monotonic() + LEADERS_REFRESH_MS / 1000
        self.valid_symbols: set[str] = set()
        self.categories: dict[str, str] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.series: dict[str, dict[int, Candle]] = {}
        self.spot_series: dict[str, dict[int, Candle]] = {}
        self.spot_errors: dict[str, str] = {}
        self.fetched_for: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.symbols: list[str] = []
        self.eligible_count = 0
        self.end = 0
        self.clock_offset = 0.0
        self.generation = 0
        self.task = self.detail_task = None
        self.cancel = threading.Event()
        self.detail_cancel = threading.Event()
        self.details: dict[tuple[str, int, bool], dict[str, Any]] = {}
        self.detail_wanted: tuple[str, int, bool] | None = None
        self.selected = ""
        self.metrics: dict[str, dict[str, Any]] = {}
        self._rendering = False
        self._needs_clock = True
        self._coin_pixmaps: dict[str, QtGui.QPixmap] = {}
        self._analysis_serial = 0
        self._history_transport = WorkspaceHistory()
        self._analysis_job = LatestJob(QtCore.QThreadPool.globalInstance(), self, priority=-1)
        self._analysis_job.ready.connect(self._analysis_ready)
        self._analysis_job.failed.connect(self._analysis_failed)
        self._prepared_analysis = {}
        self._build_ui()
        self.load_timer = QtCore.QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(self._next_batch)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.refresh_timer.setInterval(LEADERS_REFRESH_MS)
        self.refresh_timer.timeout.connect(self.refresh)
        self.detail_timer = QtCore.QTimer(self)
        self.detail_timer.setSingleShot(True)
        self.detail_timer.setInterval(450)
        self.detail_timer.timeout.connect(self._request_details)
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.setInterval(900)
        self.play_timer.timeout.connect(self._play_step)
        self._apply_fixed_palette()
    def _build_ui(self):
        build_leaders(self)


    @staticmethod
    def chart_icon() -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(30, 30)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#9AA4B4"), 1.8))
        painter.drawLine(3, 26, 27, 26)
        for x, top in ((5, 16), (13, 5), (21, 11)):
            painter.drawRect(x, top, 4, 26 - top)
        painter.end()
        return QtGui.QIcon(pixmap)

    def identity(self, symbol: str) -> tuple[str, str]:
        base = coin_base_symbol(symbol)
        name = coin_name(base)

        category = "Benchmark" if symbol == BENCHMARK else (
            _sector_overview_classify(symbol, {}) or self.categories.get(symbol, "—"))
        return name, category

    def invalidate_coin_icons(self, symbols: object) -> None:
        changed = False
        for symbol in symbols if isinstance(symbols, (list, tuple, set, frozenset)) else (symbols,):
            base = coin_base_symbol(str(symbol or ""))
            if base and base in self._coin_pixmaps:
                self._coin_pixmaps.pop(base, None)
                changed = True
        if changed:
            self.table.viewport().update()

    def _coin_icon_changed(self, symbol: str) -> None:
        self.invalidate_coin_icons((symbol,))

    def paint_coin(self, painter: QtGui.QPainter, rect: QtCore.QRectF, symbol: str) -> None:
        base = coin_base_symbol(symbol)
        if base not in self._coin_pixmaps:
            pixmap = QtGui.QPixmap()
            payload = coin_icon_bytes(base)
            if payload:
                pixmap.loadFromData(payload)
            self._coin_pixmaps[base] = pixmap
        pixmap = self._coin_pixmaps[base]
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        if not pixmap.isNull():
            clip = QtGui.QPainterPath()
            clip.addEllipse(rect)
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            painter.fillRect(rect, QtGui.QColor(self.theme.get("panel2", self.theme.get("panel", "#12151B"))))
            painter.drawPixmap(rect, pixmap, QtCore.QRectF(pixmap.rect()))
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor(self.theme.get("control_border", self.theme.get("border", "#303030"))), 1))
            painter.setBrush(QtGui.QColor(self.theme.get("panel2", self.theme.get("panel", "#181C24"))))
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
            apply_text_render_hints(painter)
            font = typography_font(TextRole.INSTRUMENT_SYMBOL, emphasized=True)
            font.setPixelSize(max(10, int(rect.width() / 3)))
            painter.setFont(font)
            painter.setPen(QtGui.QColor(self.theme.get("text", "#DFE5EE")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, base[:2] or "·")
        painter.restore()

    def price_path(self, symbol: str) -> list[float | None]:
        return self._prepared_analysis.get("paths", {}).get(symbol, [])


    def bind_sources(self, rest: Any, db: Any, watchlist: Any, can_load: Any) -> None:
        previous = getattr(self, "watchlist", None)
        if previous is watchlist and self.rest is rest and self.db is db:
            self.can_load = can_load
            return
        if previous is not None:
            try:
                previous.symbols_changed.disconnect(self._sync_watch)
            except (RuntimeError, TypeError):
                pass
            try:
                previous.icon_changed.disconnect(self._coin_icon_changed)
            except (AttributeError, RuntimeError, TypeError):
                pass
        self.rest, self.db, self.watchlist, self.can_load = rest, db, watchlist, can_load
        watchlist.symbols_changed.connect(self._sync_watch)
        if hasattr(watchlist, "icon_changed"):
            watchlist.icon_changed.connect(self._coin_icon_changed)
        self._cache_name = "leaders:v1:" + ("testnet" if getattr(rest, "testnet", False) else "live")
        self.refresh_timer.start()
        self._next_refresh_at = time.monotonic() + LEADERS_REFRESH_MS / 1000
        self._cache_loading = True
        self._cache_task = ApiTask(lambda: load_leadership_state(db, self._cache_name))
        self._cache_task.signals.finished.connect(self._cache_restored)
        self._cache_task.signals.failed.connect(self._cache_failed)
        QtCore.QThreadPool.globalInstance().start(self._cache_task, -1)

    def _cache_restored(self, state: dict[str, Any] | None) -> None:
        self._cache_task = None
        self._cache_loading = False
        if self.closing:
            return
        if state:


            for name, value in state.items():
                if name == "categories" and self.valid_symbols:
                    continue
                setattr(self, name, value)
            self._render_dirty = True
            self.data_changed.emit()
        if self.active:
            if state:
                self.render()
            self.refresh(force=True)
        elif self._refresh_pending:
            self.refresh()


    def _cache_failed(self, error: str) -> None:
        log.warning("Could not restore Leaders snapshot: %s", error)
        self._cache_restored(None)

    def _save_cached_state(self) -> None:
        if self.db is None or self._cache_loading or not self.end or not self.series:
            return
        state = {
            "version": 1, "end": self.end, "clock_offset": self.clock_offset,
            "symbols": tuple(self.symbols), "eligible_count": self.eligible_count,
            "categories": dict(self.categories), "fetched_for": dict(self.fetched_for),
            "series": dict(self.series), "spot": dict(self.spot_series),
        }
        db, name = self.db, self._cache_name
        self._save_task = ApiTask(lambda: _save_leaders_snapshot(db, name, state))
        QtCore.QThreadPool.globalInstance().start(self._save_task, -1)

    def sector_hourly_snapshot(self) -> dict[str, Any]:
        """Expose the already-loaded completed hourly dataset read-only by convention.

        Sectors consumes these dictionaries on the GUI thread so it can reuse
        Leaders' 1H futures/spot history without issuing duplicate requests.
        """
        return {
            "end": self.end,
            "clock_offset": self.clock_offset,
            "symbols": tuple(self.symbols),
            "series": self.series,
            "spot": self.spot_series,
            "categories": self.categories,
        }

    def set_universe(self, symbols: set[str], metadata: list[dict[str, Any]]) -> None:
        self.valid_symbols = set(symbols)
        self.categories = {
            row["symbol"]: _sector_overview_classify(row["symbol"], row) or "—"
            for row in metadata if row.get("symbol") in self.valid_symbols
            and isinstance(row.get("underlyingSubType", []), list)
        }
        self.refresh(force=self.active)

    @property
    def tracked_symbols(self) -> list[str]:


        return []

    def set_tickers(self, tickers: dict[str, dict[str, Any]]) -> None:


        self.tickers = tickers
        if self.active and not self.symbols:
            self.refresh(force=True)
        self._refresh_live_prices()

    def _refresh_live_prices(self) -> None:
        if not self.active or not self._view_visible or self.closing or self.replay.value() != 24:
            return
        for row in range(self.table.rowCount()):
            symbol_item = self.table.item(row, 1)
            price_item = self.table.item(row, 3)
            if symbol_item is None or price_item is None:
                continue
            symbol = str(symbol_item.data(Qt.ItemDataRole.UserRole) or "")
            value = safe_float(self.tickers.get(symbol, {}).get("c"))
            if value > 0:
                text = _price(value)
                if price_item.text() != text:
                    price_item.setText(text)

    def update_tickers(self, updates: list[dict[str, Any]]) -> None:
        for ticker in updates:
            symbol = str(ticker.get("s", ""))
            if symbol:
                current = self.tickers.get(symbol)
                if current is None:
                    self.tickers[symbol] = dict(ticker)
                elif current is not ticker:
                    current.update(ticker)
        self._refresh_live_prices()

    def set_active(self, active: bool, *, visible: bool = True) -> None:
        was_active, was_visible = self.active, self._view_visible
        self.active = bool(active) and not self.closing
        self._view_visible = bool(visible)
        if not self._view_visible:
            self.detail_timer.stop()
            self.detail_cancel.set()
            self.play_timer.stop()
            self.play.setChecked(False)
        if self.active:
            if not was_active:
                self.refresh(force=True)
                if self._render_dirty:
                    self.render()
                self._schedule_details()
            elif self._view_visible and not was_visible:
                self.render()
                self._schedule_details()
            return


        self.load_timer.stop()
        self._load_pending = False
        self._refresh_pending = False
        self.cancel.set()
        self.detail_timer.stop()
        self.play_timer.stop()
        self.play.setChecked(False)
        self.detail_cancel.set()

    def set_interaction_priority(self, active: bool) -> None:
        """Pause optional startup batches while a chart is being manipulated."""
        self._interaction_paused = bool(active)
        if active:
            self.load_timer.stop()
            self.detail_timer.stop()
            self.cancel.set()
            self.detail_cancel.set()
        elif not self.closing:
            if self._refresh_pending:
                self.refresh(force=True)
            elif self.task is None and self._load_pending:
                self.load_timer.start(0)
            if self.active and self._render_dirty:
                self.render()

    def shutdown(self) -> None:
        self._analysis_job.close()
        self.closing = True
        self.active = False
        self.refresh_timer.stop()
        self.load_timer.stop()
        self.detail_timer.stop()
        self.play_timer.stop()
        self.cancel.set()
        self.detail_cancel.set()
        self._save_cached_state()

    def _filters_changed(self) -> None:
        self.generation += 1
        self.cancel.set()
        self.detail_cancel.set()
        self._needs_clock = True
        self.refresh(force=True)

    @profile_callback("workspace.leaders.refresh_ms")
    def refresh(self, *, force: bool = False) -> None:
        if self.closing:
            return
        remaining = self._next_refresh_at - time.monotonic()
        if not force and remaining > 0:
            if self.rest is not None and not self.refresh_timer.isActive():
                self.refresh_timer.start(max(1, math.ceil(remaining * 1000)))
            return
        if self._interaction_paused or self._cache_loading:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        self._next_refresh_at = time.monotonic() + LEADERS_REFRESH_MS / 1000
        self.refresh_timer.start(LEADERS_REFRESH_MS)
        if self.rest is None or not self.can_load() or not self.valid_symbols or not self.tickers:
            return
        minimum = int(self.liquidity.currentData() or 20_000_000)
        liquid = sorted((symbol for symbol in self.valid_symbols
                         if safe_float(self.tickers.get(symbol, {}).get("q")) >= minimum),
                        key=lambda symbol: (-safe_float(self.tickers[symbol].get("q")), symbol))
        self.eligible_count = len(liquid)
        count = int(self.limit.currentData() or 0)
        self.symbols = [symbol for symbol in (liquid[:count] if count else liquid) if symbol != BENCHMARK]
        end = int((time.time() * 1000 + self.clock_offset - 2000) // HOUR) * HOUR
        if end != self.end:
            self.generation += 1
            self.cancel.set()
            self._set_end(end)
            self._needs_clock = True
        wanted = {*self.symbols, BENCHMARK}
        self.series = {symbol: rows for symbol, rows in self.series.items() if symbol in wanted}
        self.spot_series = {symbol: rows for symbol, rows in self.spot_series.items() if symbol in wanted}
        self.render()
        self._load_pending = True
        if not self.task:
            self.load_timer.start(0)
        if self.active:
            self._schedule_details()

    @profile_callback("workspace.leaders.next_batch_ms")
    def _next_batch(self) -> None:
        if (
            self.task
            or self.closing
            or self._interaction_paused
            or not self._load_pending
            or self._cache_loading
            or self.rest is None
            or not self.can_load()
        ):
            return
        if not self.symbols or BENCHMARK not in self.valid_symbols:
            self._load_pending = False
            return
        now = time.monotonic()
        pending = [symbol for symbol in [BENCHMARK, *self.symbols]
                   if (self.fetched_for.get(symbol) != self.end or symbol not in self.series)
                   and self.retry_after.get(symbol, 0) <= now]
        if not pending:
            self._load_pending = False
            return


        batch = pending[:4 if self.active else 1]
        self.cancel = threading.Event()
        rest, db, end, cancel = self.rest, self.db, self.end, self.cancel
        generation, sync = self.generation, self._needs_clock
        self._needs_clock = False
        self.task = ApiTask(lambda: _load_batch(rest, db, batch, end, cancel, generation, sync))
        self.task.symbols = batch
        self.task.generation = generation
        self.task.signals.finished.connect(self._batch_finished)
        self.task.signals.failed.connect(self._batch_failed)
        QtCore.QThreadPool.globalInstance().start(self.task, -1)

    @profile_callback("workspace.leaders.batch_finished_ms")
    def _batch_finished(self, result: dict[str, Any]) -> None:
        self.task = None
        if self.closing:
            return
        if result["generation"] == self.generation:
            if result["clock_offset"] is not None:
                self.clock_offset = result["clock_offset"]
                self._set_end(result["end"])
            for symbol, rows in result["series"].items():
                if symbol not in {*self.symbols, BENCHMARK}:
                    continue
                self.series[symbol] = rows
                if symbol in result.get("spot", {}):
                    self.spot_series[symbol] = result["spot"][symbol]
                self.spot_errors.pop(symbol, None)
                if symbol in result.get("spot_errors", {}):
                    self.spot_errors[symbol] = result["spot_errors"][symbol]
                error = result["errors"].get(symbol)
                if error:
                    self.errors[symbol] = error
                    self.retry_after[symbol] = time.monotonic() + 120
                else:
                    self.errors.pop(symbol, None)
                    if symbol == BENCHMARK or symbol in result.get("spot", {}):
                        self.fetched_for[symbol] = result["end"]
            self.render()
            self.data_changed.emit()
        if self._load_pending and not self._interaction_paused:
            self.load_timer.start(1000 if self.active else 3000)

    def _batch_failed(self, error: str) -> None:
        task, self.task = self.task, None
        if self.closing:
            return
        if task is not None and task.generation == self.generation:
            for symbol in task.symbols:
                self.errors[symbol] = error
                self.retry_after[symbol] = time.monotonic() + 120
            self._needs_clock = True
        self.render()
        if self._load_pending and not self._interaction_paused:
            self.load_timer.start(1500 if self.active else 5000)

    def cursor_end(self) -> int:
        return self.end - (24 - self.replay.value()) * HOUR

    def _set_end(self, end: int) -> None:
        previous = self.cursor_end()
        replaying = self.end > 0 and self.replay.value() < 24
        self.end = end
        if replaying:
            blocker = QtCore.QSignalBlocker(self.replay)
            self.replay.setValue(max(0, min(24, 24 - (end - previous) // HOUR)))
            del blocker

    def _cursor_changed(self) -> None:
        self.render()
        self._schedule_details()

    def _seek(self, step: int) -> None:
        self.play.setChecked(False)
        self.replay.setValue(max(0, min(24, self.replay.value() + step)))

    def _latest(self) -> None:
        self.play.setChecked(False)
        self.replay.setValue(24)

    def _toggle_play(self, playing: bool) -> None:
        self.play.update()
        if playing:
            self.detail_timer.stop()
            self.detail_cancel.set()
            if self.replay.value() == 24:
                self.replay.setValue(0)
            self.play_timer.start()
        else:
            self.play_timer.stop()
            self._schedule_details()

    def _play_step(self) -> None:
        self.replay.setValue(min(24, self.replay.value() + 1))
        if self.replay.value() == 24:
            self.play.setChecked(False)


    def _analysis_view_key(self):
        return (self.generation, self.cursor_end(), tuple(self.symbols),
                self.span.currentData(), self.search.text(), self.sort.currentData(), self.sort_descending,
                getattr(self, "category_filter_value", "All Sectors"),
                getattr(self, "state_filter", "All"))

    @profile_callback("workspace.leaders.render_ms")
    def render(self) -> None:
        self._render_dirty = True
        if not self._view_visible:
            return
        if (not self.active or self.closing or self._interaction_paused
                or self._cache_loading or not hasattr(self, "table") or self._rendering):
            return
        self._analysis_serial += 1
        state = {
            "symbols": tuple(self.symbols), "series": dict(self.series),
            "spot_series": dict(self.spot_series), "categories": dict(self.categories),
            "cursor": self.cursor_end(), "hours": int(self.span.currentData() or 24),
            "query": self.search.text(), "sort_mode": self.sort.currentData(),
            "sort_descending": self.sort_descending,
            "category_filter_value": getattr(self, "category_filter_value", "All Sectors"),
            "state_filter": getattr(self, "state_filter", "All"),
            "valid_symbols": frozenset(self.valid_symbols),
            "tickers": {symbol: {"q": row.get("q")} for symbol, row in self.tickers.items()},
        }
        self._analysis_job.submit(
            (self._analysis_view_key(), self._analysis_serial),
            _workspace_analysis, _prepare_leaders, state, self._history_transport,
        )

    @QtCore.Slot(object, object)
    def _analysis_ready(self, key, prepared) -> None:
        if (self.closing or not self.active or not self._view_visible or self._interaction_paused
                or key[0] != self._analysis_view_key()):
            return
        self._prepared_analysis = prepared
        self._prepared_view_context = (self.generation, self.cursor_end(), tuple(self.symbols), self.span.currentData())
        self._rendering = True
        try:
            self._render(prepared)
            self._render_dirty = False
        finally:
            self._rendering = False

    @QtCore.Slot(object, str)
    def _analysis_failed(self, _key, message) -> None:
        self._render_dirty = True
        log.error("Leaders analysis failed: %s", message)

    def _render(self, prepared) -> None:
        end = prepared["end"]
        self.metrics = prepared["metrics"]
        ordered = prepared["ordered"]
        covered, btc_ready = prepared["covered"], prepared["btc_ready"]
        unavailable = sum(symbol in self.errors for symbol in self.symbols)
        self.coverage.setText(f"{covered}/{len(self.symbols)} histories · {self.eligible_count} liquid pairs"
                              + (f" · {unavailable} unavailable" if unavailable else "")
                              + (" · BTC pending" if not btc_ready else ""))
        self.coverage.setToolTip("\n".join(f"{symbol}: {error}" for symbol, error in self.errors.items()
                                          if symbol in {*self.symbols, BENCHMARK}) or "All displayed values use completed hourly candles.")
        self.asof.setText(("Latest completed hour · " if self.replay.value() == 24 else "Replay · ")
                          + (_stamp(end, True) if self.end else "Waiting for history"))
        self.scope.setText(f"Current liquid universe · 4H volume shares: {prepared['cohort_count']} comparable pairs · Volume baseline: prior 24H median · Tags: reference labels / Binance")
        self.scope.setToolTip("Replay keeps today's selected liquid universe. Volume shares compare two consecutive 4H windows using exactly the same covered pairs; BTC is the benchmark and is excluded from this altcoin turnover total.")
        self.sample_caption.setText(f"{len(ordered)} of {len(self.symbols)} coins · Returns in USD · RS score vs BTC · Click a header to sort")
        self.latest.setText(_stamp(end) if self.end else "—")
        self.latest.setToolTip(("Latest completed close: " if self.replay.value() == 24 else "Replay: ")
                               + (_stamp(end, True) if self.end else "Waiting for history") + "\nClick to return to the latest completed hour.")
        self.replay.setToolTip(_stamp(end, True) if self.end else "Waiting for history")

        headers = ["#", "Symbol", "Name", "Price", "1H %", "4H %", "24H %", "RS Score", "State", "Sector", "24H Trend"]
        blocker = QtCore.QSignalBlocker(self.table)
        scroll = self.table.verticalScrollBar().value()
        self.table.setUpdatesEnabled(False)
        try:
            if self.table.columnCount() != len(headers):
                self.table.setColumnCount(len(headers))
                self.table.setHorizontalHeaderLabels(headers)
            if not getattr(self, "_leader_headers_configured", False):
                header = self.table.horizontalHeader()
                header.setMinimumSectionSize(42)
                fixed_widths = {0: 38, 1: 108, 2: 130, 3: 92, 4: 70, 5: 70, 6: 74, 7: 76, 8: 100, 9: 112}
                for column in range(len(headers)):
                    if column in fixed_widths:
                        header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Fixed)
                        self.table.setColumnWidth(column, fixed_widths[column])
                    else:
                        header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)
                self._leader_headers_configured = True

            # Keep each symbol's items and delegate data across rankings. Qt's
            # native row sort moves the complete row, including its selection.
            wanted = set(ordered)
            seen = set()
            for row in range(self.table.rowCount() - 1, -1, -1):
                item = self.table.item(row, 1)
                symbol = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
                if symbol not in wanted or symbol in seen:
                    self.table.removeRow(row)
                else:
                    seen.add(symbol)
            positions = {self.table.item(row, 1).data(Qt.ItemDataRole.UserRole): row
                         for row in range(self.table.rowCount())}
            for symbol in ordered:
                if symbol not in positions:
                    positions[symbol] = self.table.rowCount()
                    self.table.insertRow(self.table.rowCount())
            self.row_symbols = ordered
            previous_rows = getattr(self, "_leader_row_cache", {})
            current_rows = {}
            theme_key = tuple(sorted(self.theme.items()))

            for rank, symbol in enumerate(ordered, 1):
                row_index = positions[symbol]
                metrics = self.metrics.get(symbol, {})
                name, category = self.identity(symbol)
                state = metrics.get("state", "Waiting")
                score = metrics.get("score")
                live_ticker_price = safe_float(self.tickers.get(symbol, {}).get("c"), 0.0) if self.replay.value() == 24 else 0.0
                price = live_ticker_price if live_ticker_price > 0 else metrics.get("price")
                spark = prepared["sparks"].get(symbol, [])
                price_text = _price(price)
                previous = previous_rows.get(symbol)
                rank_item = self.table.item(row_index, 0)
                if not isinstance(rank_item, _LeadershipRankItem):
                    rank_item = _LeadershipRankItem(str(rank))
                    self.table.setItem(row_index, 0, rank_item)
                elif rank_item.text() != str(rank):
                    rank_item.setText(str(rank))
                unchanged = (previous is not None
                             and previous[0][0] == name and previous[0][1] == category
                             and previous[0][2] == metrics and previous[0][3] == spark
                             and previous[0][4] == theme_key)
                if unchanged:
                    current_rows[symbol] = previous if previous[1] == price_text else (previous[0], price_text)
                    if previous[1] != price_text:
                        self.table.item(row_index, 3).setText(price_text)
                    continue
                # Snapshot only changed rows. Rebuilding thousands of temporary
                # metric tuples on unchanged commits also creates GC pressure.
                current_rows[symbol] = ((name, category, dict(metrics), list(spark), theme_key), price_text)
                values = [
                    str(rank),
                    symbol.removesuffix("USDT"),
                    name,
                    price_text,
                    _pct(metrics.get("usd1"), 1),
                    _pct(metrics.get("usd4"), 1),
                    _pct(metrics.get("usd24"), 1),
                    "—" if score is None else str(score),
                    state,
                    category,
                    "",
                ]
                for column, value in enumerate(values):
                    item = self.table.item(row_index, column)
                    if item is None:
                        item = QtWidgets.QTableWidgetItem()
                        self.table.setItem(row_index, column, item)
                    if item.text() != value:
                        item.setText(value)
                    item.setData(Qt.ItemDataRole.UserRole, symbol)
                    detail: dict[str, Any] = {"align": "left" if column in (1, 2, 9) else "right" if column in (3, 4, 5, 6, 7) else "center"}
                    if column == 1:
                        detail["role"] = "symbol"
                    elif column in (4, 5, 6):
                        amount = (metrics.get("usd1"), metrics.get("usd4"), metrics.get("usd24"))[column - 4]
                        background, foreground = _heat_color(amount, self.theme)
                        detail.update(background=background, foreground=foreground, numeric=True)
                    elif column == 7:
                        detail["numeric"] = True
                        if score is not None:
                            strength = max(0.0, min(1.0, score / 100.0))
                            detail["background"] = _blend_hex(
                                self.theme.get("panel2", self.theme.get("panel", "#181C24")),
                                self.theme.get("green", "#4DDFA4"),
                                0.10 + 0.34 * strength,
                            )
                            detail["foreground"] = self.theme.get("text", "#DFE5EE")
                        item.setToolTip("Display-only relative-strength score (0–100) derived from 1H/4H/24H BTC-relative return, acceleration and relative volume. It is not a forecast probability.")
                    elif column == 8:
                        detail.update(role="state", foreground=_leader_color(state, self.theme))
                    elif column == 10:
                        valid_spark = [value for value in spark if value is not None and math.isfinite(value)]
                        rising = len(valid_spark) >= 2 and valid_spark[-1] >= valid_spark[0]
                        detail.update(role="spark", spark=spark, foreground=self.theme.get("green", "#4DDFA4") if rising else self.theme.get("red", "#FF7A85"))
                        item.setToolTip("Last 24 completed hourly closes. Gaps remain gaps; replay uses the selected historical cursor.")
                    else:
                        detail["numeric"] = column in (0, 3)
                    if column not in (7, 10):
                        item.setToolTip(f"{symbol} · {name} · {category}\nDouble-click to open chart\n4H vs BTC: {_pct(metrics.get('rs4'))} · 24H vs BTC: {_pct(metrics.get('rs24'))}")
                    item.setData(DETAIL_ROLE, detail)
                    item.setForeground(QtGui.QColor(detail.get("foreground", self.theme.get("text", "#DFE5EE"))))

            if list(positions) != ordered:
                self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
            self._leader_row_cache = current_rows

        finally:
            self.table.setUpdatesEnabled(True)
        self.table.verticalScrollBar().setValue(scroll)

        if self.selected not in ordered:
            fallback_order = ordered
            self.selected = next((symbol for symbol in fallback_order if self.metrics[symbol]["state"] == "Leading"),
                                 next((symbol for symbol in fallback_order if self.metrics[symbol]["state"] == "Improving"),
                                      next(iter(ordered), "")))
        if self.selected in ordered:
            self.table.selectRow(ordered.index(self.selected))
        del blocker
        self.table.verticalScrollBar().setValue(scroll)
        self.chart_button.setEnabled(bool(self.selected))
        self.watch_button.setEnabled(bool(self.selected) and self.watchlist is not None)
        self.chart_button.setText("Open " + self.selected.removesuffix("USDT") if self.selected else "Open chart")

        for state, cards in self.cards.items():
            candidates = prepared["candidates"].get(state, [])
            self.empty[state].setVisible(not candidates)
            for index, card in enumerate(cards):
                card.setVisible(False)
                if index < len(candidates):
                    card.set_market(candidates[index], self.metrics[candidates[index]])

        update_dashboard(self, prepared["dashboard"])
        self._render_facts()
        self._render_changes(prepared["events"])
        self._schedule_details()


    def _render_changes(self, events) -> None:
        text = "Recent changes: " + "  ·  ".join(f"{_stamp(at)} {symbol.removesuffix('USDT')} {state}" for at, symbol, state in events[:5])
        self.changes.setText(text if events else "Recent changes: no new leadership changes in the last four completed hours")
        self.changes.setToolTip("\n".join(f"{_stamp(at, True)} {symbol} {state}" for at, symbol, state in events[:30]))

    def _select_row(self, row: int, column: int) -> None:
        if 0 <= row < len(self.row_symbols):
            symbol = self.row_symbols[row]
            if symbol != BENCHMARK:
                self.select_symbol(symbol)

    def _open_row(self, row: int, column: int) -> None:
        if 0 <= row < len(self.row_symbols):
            self.symbol_selected.emit(self.row_symbols[row])

    def select_symbol(self, symbol: str) -> None:
        if symbol not in self.metrics:
            return
        self.selected = symbol
        self.chart_button.setText("Open " + symbol.removesuffix("USDT"))
        self.chart_button.setEnabled(True)
        self.watch_button.setEnabled(self.watchlist is not None)
        if symbol in self.row_symbols:
            self.table.selectRow(self.row_symbols.index(symbol))
        self._render_facts()
        self._schedule_details()

    def toggle_watch(self, symbol: str) -> None:
        if self.watchlist is not None and symbol in self.valid_symbols:
            self.watchlist.set_symbol_watched(symbol, not self.watchlist.contains(symbol))

    def _sync_watch(self, _symbols: object = None) -> None:
        for cards in self.cards.values():
            for card in cards:
                card.sync_watch()
        watched = bool(self.watchlist and self.watchlist.contains(self.selected))
        self.watch_selected.setChecked(watched)
        self.watch_selected.setText("★ Watched" if watched else "☆ Watch")
        self.watch_selected.setToolTip("Remove from watchlist" if watched else "Add to watchlist")
        if hasattr(self, "watch_button"):
            self.watch_button.setText("★ Remove watch" if watched else "☆ Add watch")

    def _schedule_details(self) -> None:
        if not self._view_visible:
            return
        if not hasattr(self, "detail_timer"):
            return
        key = (self.selected, self.cursor_end(), self.replay.value() == 24)
        if key != self.detail_wanted:
            self.detail_cancel.set()
            self.detail_wanted = key
        if self.active and not self._interaction_paused and self.selected and not self.play.isChecked():
            if not self.detail_timer.isActive():
                self.detail_timer.start()

    def _request_details(self) -> None:
        if self.detail_task or not self.active or not self._view_visible or self._interaction_paused or not self.selected or self.rest is None or not self.can_load() or self.play.isChecked():
            return
        key = (self.selected, self.cursor_end(), self.replay.value() == 24)
        cached = self.details.get(key)
        if cached and time.monotonic() - cached["at"] < (90 if key[2] else 3600):
            return
        self.detail_cancel = threading.Event()
        rest, cancel = self.rest, self.detail_cancel
        self.detail_task = ApiTask(lambda: _load_details(rest, *key, cancel))
        self.detail_task.key = key
        self.detail_task.signals.finished.connect(self._details_finished)
        self.detail_task.signals.failed.connect(self._details_failed)
        QtCore.QThreadPool.globalInstance().start(self.detail_task)

    def _details_finished(self, result: dict[str, Any]) -> None:
        self.detail_task = None
        if self.closing:
            return
        if result.get("cancelled"):
            self._schedule_details()
            return
        self.details[result["key"]] = result
        self.details_changed.emit()
        while len(self.details) > 80:
            self.details.pop(next(iter(self.details)))
        if self.active and self._view_visible:
            self._render_facts()
        if result["key"] != self.detail_wanted:
            self._schedule_details()

    def _details_failed(self, error: str) -> None:
        task, self.detail_task = self.detail_task, None
        if task is not None and not self.closing:
            self.details[task.key] = {"key": task.key, "at": time.monotonic(), "errors": {"details": error}}
            if self.active and self._view_visible:
                self._render_facts()
            if task.key != self.detail_wanted:
                self._schedule_details()

    def _fact(self, name: str, text: str, tooltip: str = "", passed: bool = False) -> None:
        self.facts[name].setText(text)
        self.facts[name].setToolTip(tooltip or text)
        if name in self.fact_marks:
            mark = self.fact_marks[name]
            mark.setText("✓" if passed else "")
            mark.setStyleSheet("QLabel { border: 1.5px solid " + ("#4DDFA4" if passed else "#9AA4B4")
                              + "; border-radius: 8px; background: " + ("#4DDFA4" if passed else "transparent")
                              + "; color: #0B0D11; }")
            mark.setToolTip(tooltip or text)
            mark.setAccessibleName(text)

    def _render_facts(self) -> None:
        symbol, end = self.selected, self.cursor_end()
        item = self.metrics.get(symbol, {})
        self.confirmation.set_market(symbol, item)
        self._sync_watch()
        self._fact("relative", "Relative strength positive", "4H relative return vs BTC is positive.", item.get("rs4") is not None and item["rs4"] > 0)
        self._fact("share", "Volume share rising", "4H volume share is higher than the previous 4H window.", item.get("share_delta") is not None and item["share_delta"] > 0)
        self._fact("baseline", "Above volume baseline", "Latest 1H volume exceeds the previous 24H median.", item.get("rvol") is not None and item["rvol"] > 1)
        distance = item.get("high_distance")
        position = "—" if distance is None else "cleared" if distance < 0 else "nearby" if distance <= 1 else "distant"
        self._fact("range", f"4H range high: {position}",
                   "Highest high of the four complete hours preceding the selected candle.\n"
                   "Nearby means the selected close is within 1% below the prior high; cleared means above it.\n"
                   + (f"High is {abs(distance):.2f}% {'above' if distance >= 0 else 'below'} price." if distance is not None else "Four comparable candles unavailable."),
                   distance is not None and distance <= 1)
        rvol = item.get("rvol")
        self._fact("volume", "1H volume: " + (f"{rvol:.2f}× baseline" if rvol is not None else "—"),
                   "Latest completed hour's USDT quote volume / median of the preceding 24 completed hours; calibrated threshold ≥1.30×.", rvol is not None and rvol >= 1.3)
        live = self.replay.value() == 24
        result = self.details.get((symbol, end, live), {})
        for key, title in (("spot", "Spot participation"), ("funding", "Funding"), ("oi", "OI 1H")):
            self._fact(key, f"{title}: {'loading…' if symbol and not result and not self.play.isChecked() else '—'}",
                       result.get("errors", {}).get(key, "Data unavailable for this pair or historical hour."))
        funding = sorted((row for row in result.get("funding", []) if 0 < int(row.get("fundingTime", 0)) <= end),
                         key=lambda row: int(row["fundingTime"]))
        if funding:
            latest = funding[-1]
            at = int(latest["fundingTime"])
            rate = safe_float(latest.get("fundingRate"), float("nan"))
            interval = (at - int(funding[-2]["fundingTime"])) / HOUR if len(funding) >= 2 else None


            comparable = interval is not None and 0.5 <= interval <= 24 and end - at <= interval * HOUR
            rate8 = rate * 100 * 8 / interval if comparable and math.isfinite(rate) else None
            neutral = rate8 is not None and -0.005 <= rate8 <= 0.015
            label = "neutral" if neutral else "crowded" if rate8 is not None and rate8 > 0.03 else "positive" if rate8 is not None and rate8 > 0 else "negative" if rate8 is not None else _pct(rate * 100, 3)
            self._fact("funding", f"Funding: {label}",
                       f"Last settled rate: {_pct(rate * 100, 4)} at {_stamp(at, True)}.\n"
                       + (f"Observed settlement interval: {interval:g}H; 8H equivalent {_pct(rate8, 4)}.\n" if rate8 is not None else "Settlement interval or freshness is unavailable; no neutral classification.\n")
                       + "Calibrated band: −0.005% to +0.015% per 8H; >+0.030% is crowded. This is settled funding, not a forecast.", neutral)
        oi = sorted((row for row in result.get("oi", []) if 0 < int(row.get("timestamp", 0)) <= end),
                    key=lambda row: int(row["timestamp"]))
        if len(oi) >= 2:
            previous, current = oi[-2:]
            before = safe_float(previous.get("sumOpenInterest"))
            current_quantity = safe_float(current.get("sumOpenInterest"), float("nan"))
            at, before_at = int(current["timestamp"]), int(previous["timestamp"])
            if (before > 0 and math.isfinite(current_quantity) and current_quantity >= 0
                    and abs(at - before_at - HOUR) <= 5000 and end - at <= HOUR):
                value = (current_quantity / before - 1) * 100
                interpretation = ("expansion" if value > 0 else "short covering" if value < 0 else "unchanged") if (item.get("usd1") or 0) > 0 else ("short pressure" if value > 0 else "deleveraging" if value < 0 else "unchanged")
                self._fact("oi", f"OI 1H: {_pct(value)} · {interpretation}", "Change in open-interest quantity, not USD valuation. Price/OI interpretation is context, not proof of participant intent.", value > 0 and (item.get("usd1") or 0) > 0)
        spot = self.spot_series.get(symbol, {}) or {int(round(row.time * 1000)) + HOUR: row for row in result.get("spot", [])}
        future = self.series.get(symbol, {})
        windows = [_window(source, at, 4) for source in (spot, future) for at in (end, end - 4 * HOUR)]
        if all(windows):
            spot_now, spot_before, perp_now, perp_before = [sum(row.quote_volume for row in rows) for rows in windows]
            if spot_now + perp_now > 0 and spot_before + perp_before > 0:
                share = spot_now / (spot_now + perp_now) * 100
                old = spot_before / (spot_before + perp_before) * 100
                trend = "rising" if share > old else "falling" if share < old else "flat"
                self._fact("spot", f"Spot participation: {trend}",
                           f"Spot share {share:.1f}% ({share - old:+.2f} pp).\n"
                           "Spot / (spot + perpetual) USDT turnover over 4H; change versus the preceding 4H. Exact-symbol matches only.", share > old)
        self._fact("liquidity", "Liquidity: —" if live else "Liquidity: unavailable in replay",
                   result.get("errors", {}).get("book", "Current book liquidity is never substituted for an unrecorded historical book."))
        book = result.get("book", {})
        bids = [(safe_float(row[0]), safe_float(row[1])) for row in book.get("bids", [])]
        asks = [(safe_float(row[0]), safe_float(row[1])) for row in book.get("asks", [])]
        bids = sorted((row for row in bids if all(math.isfinite(value) and value > 0 for value in row)), reverse=True)
        asks = sorted(row for row in asks if all(math.isfinite(value) and value > 0 for value in row))
        if live and bids and asks and asks[0][0] > bids[0][0]:
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = (asks[0][0] - bids[0][0]) / mid * 100
            bid_depth = sum(price * qty for price, qty in bids if price >= mid * 0.995)
            ask_depth = sum(price * qty for price, qty in asks if price <= mid * 1.005)
            age = int(max(0, time.monotonic() - result.get("book_at", result.get("at", time.monotonic()))))
            sufficient = spread <= 0.10 and min(bid_depth, ask_depth) >= 25_000 and age <= 90
            state = "snapshot stale" if age > 90 else "sufficient" if sufficient else "below screen threshold"
            self._fact("liquidity", f"Liquidity: {state}",
                       f"Spread {spread:.3f}% · bid / ask within ±0.5%: {_amount(bid_depth)} / {_amount(ask_depth)}.\n"
                       f"Up to 1000 levels per side; snapshot {age}s old. More depth may exist outside those levels.\n"
                       "Screen threshold: spread ≤0.10%, at least $25K displayed on EACH side within ±0.5%, snapshot ≤90s old.\n"
                       "This is a screening condition, not an estimate that a particular order can fill without slippage.", sufficient)
        self.confirmation.setToolTip(self.confirmation.toolTip() + "\n" + self.facts["oi"].text())
        self.confirmation.spark.setToolTip("\n\n".join(self.facts[key].text() + "\n" + self.facts[key].toolTip()
                                                       for key in ("volume", "oi", "range", "spot", "funding", "liquidity")))

    def apply_theme(self, _theme: dict[str, str]) -> None:
        self._apply_fixed_palette()

    def _apply_fixed_palette(self) -> None:
        palette = dict(LEADERS_PALETTE)
        self.theme = palette
        if hasattr(self, "sector_bars"):
            self.sector_bars.set_theme(palette)
        if hasattr(self, "distribution_bar"):
            self.distribution_bar.set_theme(palette)
        for button_name in ("back", "play", "forward"):
            button = getattr(self, button_name, None)
            if button is not None and hasattr(button, "set_theme"):
                button.set_theme(palette)
        self.setStyleSheet(leaders_stylesheet(palette))
        self.render()


    def restore_ui_state(self, settings: QtCore.QSettings) -> None:
        for name, widget, default in (("span", self.span, 12), ("minimum_volume", self.liquidity, 20_000_000),
                                      ("limit", self.limit, 80), ("sort", self.sort, "state")):
            value = settings.value(f"markets/leadership/{name}", default, str if isinstance(default, str) else int)
            index = widget.findData(value)
            if index >= 0:
                blocker = QtCore.QSignalBlocker(widget)
                widget.setCurrentIndex(index)
                del blocker
        self.sort_descending = settings.value("markets/leadership/sort_descending", self.sort.currentData() not in ("pair", "name", "sector"), bool)
        _update_sort(self)
        self.selected = settings.value("markets/leadership/selected", "", str)
        self.search.setText(settings.value("markets/leadership/search", "", str))
        self.replay.setValue(max(0, min(24, settings.value("markets/leadership/replay", 24, int))))
        state = settings.value("markets/leadership/state", "All", str)
        _set_state_filter(self, state if state in self.state_buttons else "All")
        category = settings.value("markets/leadership/category", "All Sectors", str)
        if self.category_filter.findText(category) < 0:
            self.category_filter.addItem(category)
        self.category_filter.setCurrentText(category)
        for column, action in self.column_actions.items():
            action.setChecked(settings.value(f"markets/leadership/column_{column}", True, bool))

    def save_ui_state(self, settings: QtCore.QSettings) -> None:
        settings.setValue("markets/leadership/sort_descending", self.sort_descending)
        for name, widget in (("span", self.span), ("minimum_volume", self.liquidity), ("limit", self.limit), ("sort", self.sort)):
            settings.setValue(f"markets/leadership/{name}", widget.currentData())
        for name, value in (("selected", self.selected), ("search", self.search.text()),
                            ("replay", self.replay.value()), ("state", self.state_filter),
                            ("category", self.category_filter_value)):
            settings.setValue(f"markets/leadership/{name}", value)
        for column, action in self.column_actions.items():
            settings.setValue(f"markets/leadership/column_{column}", action.isChecked())

def _pct(value: float | None, digits: int = 2) -> str:
    return "-" if value is None or not math.isfinite(value) else f"{value:+.{digits}f}%"


class LeadershipSparkline(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.values: list[float | None] = []
        self.color = QtGui.QColor("#8AA9FF")
        self.setMinimumSize(70, 24)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(36)

    def set_values(self, values: list[float | None], color: str) -> None:
        self.values = values
        self.color = QtGui.QColor(color)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        values = [value for value in self.values if value is not None and math.isfinite(value)]
        if len(values) < 2:
            return
        low, high = min(values), max(values)
        span = max(high - low, abs(high) * 1e-8, 1e-15)
        rect = QtCore.QRectF(self.rect()).adjusted(2, 3, -2, -3)
        path, connected = QtGui.QPainterPath(), False
        for index, value in enumerate(self.values):
            if value is None or not math.isfinite(value):
                connected = False
                continue
            point = QtCore.QPointF(rect.left() + index * rect.width() / max(1, len(self.values) - 1),
                                  rect.bottom() - (value - low) / span * rect.height())
            if connected:
                path.lineTo(point)
            else:
                path.moveTo(point)
            connected = True
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        segment = []
        for index, value in enumerate([*self.values, None]):
            if value is not None and math.isfinite(value):
                segment.append(QtCore.QPointF(rect.left() + index * rect.width() / max(1, len(self.values)-1),
                                              rect.bottom() - (value-low)/span*rect.height()))
            else:
                if len(segment) > 1:
                    fill = QtGui.QPainterPath(segment[0])
                    for point in segment[1:]:
                        fill.lineTo(point)
                    fill.lineTo(segment[-1].x(), rect.bottom())
                    fill.lineTo(segment[0].x(), rect.bottom())
                    fill.closeSubpath()
                    gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
                    top = QtGui.QColor(self.color); top.setAlpha(45)
                    bottom = QtGui.QColor(self.color); bottom.setAlpha(0)
                    gradient.setColorAt(0, top); gradient.setColorAt(1, bottom)
                    painter.fillPath(fill, gradient)
                segment = []
        painter.setPen(QtGui.QPen(self.color, 1.4))
        painter.drawPath(path)


LEADER_COLORS = {
    "Leading": "#22D27A",
    "Improving": "#E070D8",
    "Cooling": "#E8A64A",
    "Lagging": "#FF4757",
    "Flat": "#8E8E96",
    "Waiting": "#55555C",
}


def _blend_hex(background: str, foreground: str, amount: float) -> str:
    """Blend two opaque colors without assuming a dark application theme."""
    amount = max(0.0, min(1.0, float(amount)))
    base = QtGui.QColor(background)
    accent = QtGui.QColor(foreground)
    if not base.isValid():
        base = QtGui.QColor("#12151B")
    if not accent.isValid():
        accent = QtGui.QColor("#9AA4B4")
    return QtGui.QColor(
        round(base.red() + (accent.red() - base.red()) * amount),
        round(base.green() + (accent.green() - base.green()) * amount),
        round(base.blue() + (accent.blue() - base.blue()) * amount),
    ).name()


def _leader_color(state: str, theme: dict[str, str]) -> str:
    return {
        "Leading": theme.get("green", LEADER_COLORS["Leading"]),
        "Improving": theme.get("cyan", LEADER_COLORS["Improving"]),
        "Cooling": theme.get("amber", LEADER_COLORS["Cooling"]),
        "Lagging": theme.get("red", LEADER_COLORS["Lagging"]),
        "Flat": theme.get("muted", LEADER_COLORS["Flat"]),
        "Waiting": theme.get("muted", LEADER_COLORS["Waiting"]),
    }.get(str(state), theme.get("muted", "#9AA4B4"))


DETAIL_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _price(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value <= 0:
        return "-"
    if value >= 1:
        return f"{value:,.2f}"
    if value < 1e-8:
        return f"{value:.3g}"
    return f"{value:.{min(12, max(4, 3 - int(math.floor(math.log10(value)))))}f}".rstrip("0")


def _heat_color(value: float | None, theme: dict[str, str]) -> tuple[str, str]:
    """Directional heat fill/ink for 1H/4H/24H return cells.

    These columns encode signed price change, so hue carries direction and fill
    strength carries magnitude: green = positive, red = negative, neutral =
    effectively unchanged at the table's one-decimal display precision.
    """
    base = theme.get("panel2", theme.get("panel", "#12151B"))
    muted = theme.get("muted", "#9AA4B4")
    if value is None:
        return base, muted
    value = float(value)
    if not math.isfinite(value):
        return base, muted


    if abs(value) < 0.05:
        return _blend_hex(base, theme.get("text", "#DFE5EE"), 0.04), muted
    accent = (
        theme.get("green", "#4DDFA4")
        if value > 0.0
        else theme.get("red", "#FF7A85")
    )


    strength = min(abs(value) / 3.0, 1.0)
    fill = 0.08 + 0.30 * strength
    return _blend_hex(base, accent, fill), accent


class LeadershipCellDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, owner: "LeadershipTimelineWidget"):
        super().__init__(owner.table)
        self.owner = owner

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        apply_text_render_hints(painter)
        rect = option.rect
        data = index.data(DETAIL_ROLE) or {}
        background = data.get("background", self.owner.theme.get("panel", "#12151B"))
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            background = self.owner.theme.get("active", "#1E2B3E")
        painter.fillRect(rect, QtGui.QColor(background))
        painter.setPen(QtGui.QColor(self.owner.theme.get("separator", self.owner.theme.get("border", "#232833"))))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        role = data.get("role", "text")
        symbol = str(index.data(Qt.ItemDataRole.UserRole) or "")
        if role == "symbol":
            icon_size = min(24, max(18, rect.height() - 12))
            icon_rect = QtCore.QRectF(rect.left() + 8, rect.center().y() - icon_size / 2, icon_size, icon_size)
            self.owner.paint_coin(painter, icon_rect, symbol)
            font = typography_font(TextRole.INSTRUMENT_SYMBOL, emphasized=True)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(self.owner.theme.get("text", "#DFE5EE")))
            painter.drawText(rect.adjusted(icon_size + 17, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             symbol.removesuffix("USDT"))
        elif role == "state":
            state = str(index.data() or "Waiting")
            color = QtGui.QColor(data.get("foreground", LEADER_COLORS.get(state, "#9AA4B4")))
            fill = QtGui.QColor(color)
            fill.setAlpha(30)
            state_font = typography_font(TextRole.UI_LABEL)
            metrics = QtGui.QFontMetrics(state_font)
            width = min(rect.width() - 10, max(58, metrics.horizontalAdvance(state) + 22))
            pill = QtCore.QRectF(rect.center().x() - width / 2, rect.center().y() - 11, width, 22)
            painter.setPen(QtGui.QPen(color, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(pill, 11, 11)
            painter.setFont(state_font)
            painter.setPen(color)
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, state)
        elif role == "spark":
            values = data.get("spark", [])
            valid = [v for v in values if v is not None and math.isfinite(v)]
            if len(valid) >= 2:
                low, high = min(valid), max(valid)
                span = high - low or max(abs(high), 1.0) * 0.02 or 1.0
                chart = rect.adjusted(8, 10, -8, -10)
                color = QtGui.QColor(data.get("foreground", "#4DDFA4"))
                path = QtGui.QPainterPath()
                connected = False
                for i, value in enumerate(values):
                    if value is None or not math.isfinite(value):
                        connected = False
                        continue
                    x = chart.left() + i * chart.width() / max(1, len(values) - 1)
                    y = chart.bottom() - (value - low) / span * chart.height()
                    if not connected:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)
                    connected = True
                painter.setPen(QtGui.QPen(color, 1.35))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
        else:
            numeric = bool(data.get("numeric"))
            font = typography_font(TextRole.TABLE_VALUE if numeric else TextRole.UI_BODY)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(data.get("foreground", self.owner.theme.get("text", "#DFE5EE"))))
            alignment = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if data.get("align") == "left" else Qt.AlignmentFlag.AlignRight if data.get("align") == "right" else Qt.AlignmentFlag.AlignHCenter)
            text = str(index.data() or "")
            text = QtGui.QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width() - 12))
            painter.drawText(rect.adjusted(6, 0, -6, 0), alignment, text)

        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(QtGui.QPen(QtGui.QColor(self.owner.theme.get("active_line", "#8AA9FF")), 1))
            painter.drawLine(rect.topLeft(), rect.topRight())
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


class LeadershipHistoryTable(QtWidgets.QTableWidget):
    def paintEvent(self, event):
        super().paintEvent(event)


class _LeadershipRankItem(QtWidgets.QTableWidgetItem):
    def __lt__(self, other):
        return int(self.text()) < int(other.text())


class LeadershipTransport(QtWidgets.QPushButton):
    def __init__(self, kind: str, theme: dict[str, str] | None = None, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.theme = dict(theme or {})
        self.setObjectName("leadershipTransport")
        self.setFixedSize(27, 25)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_theme(self, theme: dict[str, str]) -> None:
        self.theme = dict(theme)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ink = QtGui.QColor(self.theme.get("muted", self.theme.get("text", "#9AA4B4")))
        if self.isChecked():
            ink = QtGui.QColor(self.theme.get("text", ink.name()))
        painter.setPen(QtGui.QPen(ink, 1.2))
        painter.setBrush(ink if self.kind == "play" else Qt.BrushStyle.NoBrush)
        x, y = self.width() / 2, self.height() / 2
        if self.kind == "play" and self.isChecked():
            painter.drawRect(QtCore.QRectF(x - 4, y - 5, 2, 10))
            painter.drawRect(QtCore.QRectF(x + 2, y - 5, 2, 10))
        else:
            direction = -1 if self.kind == "back" else 1
            points = [QtCore.QPointF(x - 4 * direction, y - 5), QtCore.QPointF(x + 4 * direction, y),
                      QtCore.QPointF(x - 4 * direction, y + 5)]
            painter.drawPolygon(QtGui.QPolygonF(points))
            if self.kind != "play":
               painter.drawLine(QtCore.QPointF(x + 6 * direction, y - 5), QtCore.QPointF(x + 6 * direction, y + 5))

class LeadersPairCard(QtWidgets.QFrame):
    """Compatibility card kept for the existing detail/fact pipeline."""

    chosen = Signal(str)

    def __init__(self, owner, confirmation: bool = False):
        super().__init__()

        self.owner, self.symbol, self.confirmation = owner, "", confirmation
        self.setObjectName("leadersInspectorCard" if confirmation else "leadersCandidate")
        self.name = QtWidgets.QPushButton("Select a pair")
        self.name.setObjectName("leadersPairName")
        set_text_role(self.name, TextRole.INSTRUMENT_SYMBOL)
        self.name.clicked.connect(lambda: self.chosen.emit(self.symbol) if self.symbol else None)
        self.description = ElidedLabel()
        self.description.setObjectName("leadershipMuted")
        set_text_role(self.description, TextRole.UI_CAPTION)
        self.value = QtWidgets.QLabel("-")
        self.value.setObjectName("leadersBigPrice" if confirmation else "leadersCandidatePrice")
        set_text_role(self.value, TextRole.MARKET_VALUE_EMPHASIZED if confirmation else TextRole.MARKET_VALUE)
        self.change = QtWidgets.QLabel("-")
        self.change.setObjectName("leadersCandidateChange")
        set_text_role(self.change, TextRole.MARKET_VALUE)
        self.spark = LeadershipSparkline()
        self.watch = QtWidgets.QPushButton("?")
        self.watch.setCheckable(True)
        self.watch.clicked.connect(lambda: owner.toggle_watch(self.symbol) if self.symbol else None)
        self.open = QtWidgets.QPushButton("Open chart")
        self.open.clicked.connect(lambda: owner.symbol_selected.emit(self.symbol) if self.symbol else None)
        self.open.setIcon(owner.chart_icon())
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.name)
        row.addWidget(self.description, 1)
        row.addWidget(self.change)
        layout.addLayout(row)
        if confirmation:
            price_row = QtWidgets.QHBoxLayout()
            price_row.addWidget(self.value)
            self.state = QtWidgets.QLabel("Waiting")
            set_text_role(self.state, TextRole.UI_LABEL)
            price_row.addWidget(self.state)
            price_row.addStretch(1)
            price_row.addWidget(self.watch)
            layout.addLayout(price_row)
            self.spark_title = QtWidgets.QLabel("24H relative strength vs BTC")
            self.spark_title.setObjectName("leadershipMuted")
            set_text_role(self.spark_title, TextRole.UI_CAPTION)
            layout.addWidget(self.spark_title)
            self.spark.setFixedHeight(72)
            layout.addWidget(self.spark)
            self.metric_labels: dict[str, QtWidgets.QLabel] = {}
            metrics = QtWidgets.QHBoxLayout()
            for key, caption in (("rs4", "4H vs BTC"), ("volume_share", "Vol share"), ("rvol", "Vol baseline")):
                col = QtWidgets.QVBoxLayout()
                head = QtWidgets.QLabel(caption)
                head.setObjectName("leadershipMuted")
                set_text_role(head, TextRole.UI_LABEL)
                value = QtWidgets.QLabel("-")
                value.setObjectName("leadersMetric")
                set_text_role(value, TextRole.MARKET_VALUE)
                col.addWidget(head)
                col.addWidget(value)
                metrics.addLayout(col, 1)
                self.metric_labels[key] = value
            layout.addLayout(metrics)
            layout.addWidget(self.open)
        else:
            self.value.hide()
            self.watch.hide()
            self.open.hide()
            self.spark.hide()

    def set_market(self, symbol: str, metrics: dict[str, Any]) -> None:

        self.symbol = symbol
        name_text = symbol.removesuffix("USDT") if symbol else "Select a pair"
        if self.name.text() != name_text:
            self.name.setText(name_text)
        name, category = self.owner.identity(symbol)
        description_text = f"{name} · {category}" if symbol else ""
        if self.description.text() != description_text:
            self.description.setText(description_text)
        price_text = _price(metrics.get("price"))
        if self.value.text() != price_text:
            self.value.setText(price_text)
        change = metrics.get("usd1")
        change_text = _pct(change, 1)
        if self.change.text() != change_text:
            self.change.setText(change_text)
        change_style = "color: " + ("#FF7A85" if change is not None and change < 0 else "#4DDFA4") + ";"
        if self.change.styleSheet() != change_style:
            self.change.setStyleSheet(change_style)
        if self.confirmation:
            state = metrics.get("state", "Waiting")
            if self.state.text() != state:
                self.state.setText(state)
            state_style = f"color: {LEADER_COLORS.get(state, '#9AA4B4')};"
            if self.state.styleSheet() != state_style:
                self.state.setStyleSheet(state_style)
            self.spark.set_values(self.owner.price_path(symbol), "#4DDFA4" if (metrics.get("usd24") or 0) >= 0 else "#FF7A85")
            for key, label in self.metric_labels.items():
                value = metrics.get(key)
                label.setText("-" if value is None else _pct(value) if key == "rs4" else f"{value:.1f}%" if key == "volume_share" else f"{value:.2f}x")
        self.watch.setEnabled(bool(symbol))
        self.open.setEnabled(bool(symbol))
        self.sync_watch()

    def sync_watch(self) -> None:
        watched = bool(self.owner.watchlist and self.owner.watchlist.contains(self.symbol))
        self.watch.setChecked(watched)
        self.watch.setText("?" if watched else "?")


class DistributionBar(QtWidgets.QWidget):
    def __init__(self, theme: dict[str, str] | None = None, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme = dict(theme or {})
        self.setMinimumHeight(20)
        self.setMaximumHeight(20)
        self.counts: dict[str, int] = {}

    def set_theme(self, theme: dict[str, str]) -> None:
        self.theme = dict(theme)
        self.update()

    def set_counts(self, counts: dict[str, int]) -> None:
        next_counts = dict(counts)
        if next_counts == self.counts:
            return
        self.counts = next_counts
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 3.5, -0.5, -3.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(self.theme.get("panel2", self.theme.get("control", "#181C24"))))
        painter.drawRoundedRect(rect, 5, 5)
        total = sum(max(0, int(v)) for v in self.counts.values())
        if total <= 0:
            return
        x = rect.left()
        order = ("Leading", "Improving", "Cooling", "Lagging", "Flat", "Waiting")
        for state in order:
            count = max(0, int(self.counts.get(state, 0)))
            if not count:
                continue
            width = rect.width() * count / total
            seg = QtCore.QRectF(x, rect.top(), width, rect.height())
            painter.setBrush(QtGui.QColor(_leader_color(state, self.theme)))
            painter.drawRect(seg)
            x += width


class SectorBars(QtWidgets.QWidget):
    def __init__(self, theme: dict[str, str] | None = None, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme = dict(theme or {})
        self.values: list[tuple[str, float]] = []
        self.setMinimumHeight(108)

    def set_theme(self, theme: dict[str, str]) -> None:
        self.theme = dict(theme)
        self.update()

    def set_values(self, values: list[tuple[str, float]]) -> None:
        next_values = values[:6]
        if next_values == self.values:
            return
        self.values = next_values
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        apply_text_render_hints(painter)
        if not self.values:
            painter.setPen(QtGui.QColor(self.theme.get("muted", "#9AA4B4")))
            painter.setFont(typography_font(TextRole.UI_BODY))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for sector data")
            return
        rect = QtCore.QRectF(self.rect()).adjusted(4, 7, -4, -4)
        n = len(self.values)
        gap = 8.0
        width = max(12.0, (rect.width() - gap * (n - 1)) / n)
        max_abs = max(0.25, max(abs(v) for _, v in self.values))
        top_area = rect.height() - 28
        for i, (name, value) in enumerate(self.values):
            x = rect.left() + i * (width + gap)
            bar_h = max(3.0, top_area * min(1.0, abs(value) / max_abs))
            y = rect.top() + top_area - bar_h
            color = QtGui.QColor(self.theme.get("green", "#4DDFA4") if value >= 0 else self.theme.get("red", "#FF7A85"))
            gradient = QtGui.QLinearGradient(x, y, x, y + bar_h)
            top = QtGui.QColor(color); top.setAlpha(230)
            bottom = QtGui.QColor(color); bottom.setAlpha(75)
            gradient.setColorAt(0, top); gradient.setColorAt(1, bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(QtCore.QRectF(x, y, width, bar_h), 2, 2)
            name_rect = device_pixel_rect(
                self,
                QtCore.QRectF(x - 3, rect.top() + top_area + 3, width + 6, 12),
            )
            value_rect = device_pixel_rect(
                self,
                QtCore.QRectF(x - 3, rect.top() + top_area + 15, width + 6, 12),
            )
            painter.setPen(QtGui.QColor(self.theme.get("muted", "#9AA4B4")))
            painter.setFont(typography_font(TextRole.UI_LABEL))
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignCenter, name[:8])
            painter.setPen(color)
            painter.setFont(typography_font(TextRole.MARKET_VALUE_EMPHASIZED))
            painter.drawText(value_rect, Qt.AlignmentFlag.AlignCenter, f"{value:+.1f}%")


def _metric_block(parent_layout: QtWidgets.QHBoxLayout, title: str) -> tuple[QtWidgets.QLabel, QtWidgets.QLabel]:
    frame = QtWidgets.QFrame()
    frame.setObjectName("leadersMetricBlock")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(3)
    frame.setMinimumHeight(88)
    head = QtWidgets.QLabel(title)
    head.setObjectName("leadersMetricTitle")
    set_text_role(head, TextRole.UI_LABEL)
    value = QtWidgets.QLabel("-")
    value.setObjectName("leadersMetricValue")
    set_text_role(value, TextRole.MARKET_VALUE_LARGE)
    sub = ElidedLabel("")
    sub.setObjectName("leadersMetricSub")
    set_text_role(sub, TextRole.UI_BODY)
    layout.addWidget(head)
    layout.addWidget(value)
    layout.addWidget(sub)
    parent_layout.addWidget(frame, 1)
    return value, sub


def _rank_panel(title: str) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout, list[tuple[QtWidgets.QLabel, QtWidgets.QLabel, QtWidgets.QLabel]]]:
    panel = QtWidgets.QFrame()
    panel.setObjectName("leadersSidePanel")
    layout = QtWidgets.QVBoxLayout(panel)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(7)
    panel.setMinimumHeight(176)
    header = QtWidgets.QHBoxLayout()
    label = QtWidgets.QLabel(title)
    label.setObjectName("leadersSideTitle")
    set_text_role(label, TextRole.PANEL_TITLE)
    header.addWidget(label)
    header.addStretch(1)
    layout.addLayout(header)
    rows: list[tuple[QtWidgets.QLabel, QtWidgets.QLabel, QtWidgets.QLabel]] = []
    for i in range(5):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(7)
        rank = QtWidgets.QLabel(str(i + 1))
        rank.setObjectName("leadersRank")
        set_text_role(rank, TextRole.TABLE_VALUE)
        rank.setFixedWidth(14)
        symbol = QtWidgets.QLabel("-")
        symbol.setObjectName("leadersRankSymbol")
        set_text_role(symbol, TextRole.INSTRUMENT_SYMBOL)
        name = QtWidgets.QLabel("")
        name.setObjectName("leadersRankName")
        set_text_role(name, TextRole.UI_BODY)
        change = QtWidgets.QLabel("-")
        change.setObjectName("leadersRankChange")
        set_text_role(change, TextRole.MARKET_VALUE_EMPHASIZED)
        row.addWidget(rank)
        row.addWidget(symbol)
        row.addWidget(name, 1)
        row.addWidget(change)
        layout.addLayout(row)
        rows.append((symbol, name, change))
    return panel, layout, rows


class _SegmentedSelector(QtWidgets.QWidget):
    """Direct-choice replacement for small fixed QComboBox selectors."""

    currentIndexChanged = Signal(int)
    currentTextChanged = Signal(str)

    def __init__(self, button_object_name: str, parent: QtWidgets.QWidget | None = None, *, distribute: bool = True):
        super().__init__(parent)
        self._button_object_name = button_object_name
        self._distribute = bool(distribute)
        self._items: list[tuple[str, Any, QtWidgets.QPushButton]] = []
        self._index = -1
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def addItem(self, text: str, data: Any = None) -> None:
        button = QtWidgets.QPushButton(str(text), self)
        button.setObjectName(self._button_object_name)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setFixedHeight(24)
        if self._distribute:
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(max(30, button.fontMetrics().horizontalAdvance(str(text)) + 12))
        else:
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            button.setFixedWidth(max(38, button.fontMetrics().horizontalAdvance(str(text)) + 16))
        button.clicked.connect(lambda _checked=False, target=button: self._activate_button(target))
        self._items.append((str(text), data, button))
        self._layout.addWidget(button, 1 if self._distribute else 0)
        if not self._distribute:
            self._sync_content_width()
        if self._index < 0:
            self.setCurrentIndex(0)

    def addItems(self, texts: list[str]) -> None:
        for text in texts:
            self.addItem(text, text)

    def clear(self) -> None:
        self._index = -1
        for _text, _data, button in self._items:
            self._layout.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._items.clear()
        self.setMinimumWidth(0)

    def _sync_content_width(self) -> None:
        if not self._items:
            self.setMinimumWidth(0)
            return
        width = sum(button.width() for _text, _data, button in self._items)
        width += self._layout.spacing() * max(0, len(self._items) - 1)
        self.setMinimumWidth(width)

    def count(self) -> int:
        return len(self._items)

    def itemText(self, index: int) -> str:
        return self._items[index][0] if 0 <= index < len(self._items) else ""

    def currentText(self) -> str:
        return self.itemText(self._index)

    def currentData(self) -> Any:
        return self._items[self._index][1] if 0 <= self._index < len(self._items) else None

    def findData(self, data: Any) -> int:
        return next((index for index, (_text, value, _button) in enumerate(self._items) if value == data), -1)

    def findText(self, text: str) -> int:
        return next((index for index, (label, _value, _button) in enumerate(self._items) if label == text), -1)

    def setCurrentText(self, text: str) -> None:
        index = self.findText(text)
        if index >= 0:
            self.setCurrentIndex(index)

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        changed = index != self._index
        self._index = index
        button = self._items[index][2]
        blocker = QtCore.QSignalBlocker(button)
        button.setChecked(True)
        del blocker
        if changed:
            self.currentIndexChanged.emit(index)
            self.currentTextChanged.emit(self._items[index][0])

    def _activate_button(self, button: QtWidgets.QPushButton) -> None:
        index = next((i for i, (_text, _data, candidate) in enumerate(self._items) if candidate is button), -1)
        if index >= 0:
            self.setCurrentIndex(index)


def _set_state_filter(owner, state: str) -> None:
    owner.state_filter = state
    for key, button in owner.state_buttons.items():
        button.setChecked(key == state)
    _refresh_leaders_view(owner)


def _category_changed(owner, text: str) -> None:
    owner.category_filter_value = text or "All Sectors"
    _refresh_leaders_view(owner)


def _refresh_leaders_view(owner):
    """Filter/rank the current snapshot without recomputing candle metrics or events."""
    context = (owner.generation, owner.cursor_end(), tuple(owner.symbols), owner.span.currentData())
    prepared = getattr(owner, "_prepared_analysis", {})
    if (not prepared or context != getattr(owner, "_prepared_view_context", None)
            or not owner.active or owner.closing or owner._interaction_paused or owner._rendering):
        owner.render()
        return
    snapshot = dict(prepared)
    model = _LeaderAnalysis(dict(symbols=owner.symbols, metrics=prepared["metrics"],
        categories=owner.categories, query=owner.search.text(), sort_mode=owner.sort.currentData(),
        sort_descending=owner.sort_descending, state_filter=owner.state_filter,
        category_filter_value=owner.category_filter_value))
    snapshot["ordered"] = model._ordered()
    owner._rendering = True
    try:
        owner._render(snapshot)
    finally:
        owner._rendering = False


_HEADER_SORT = {1: "pair", 2: "name", 3: "price", 4: "usd1", 5: "usd4", 6: "usd24", 7: "score", 8: "state", 9: "sector"}

def _sort_changed(owner):
    owner.sort_descending = owner.sort.currentData() not in ("pair", "name", "sector")
    _update_sort(owner)

def _toggle_sort(owner):
    owner.sort_descending = not owner.sort_descending
    _update_sort(owner)

def _update_sort(owner):
    mode = owner.sort.currentData()
    text_mode = mode in ("pair", "name", "sector")
    owner.direction_button.setText(("Z → A" if owner.sort_descending else "A → Z") if text_mode else
                                  ("↓ Leaders first" if owner.sort_descending else "↑ Laggards first") if mode == "state" else
                                  ("↓ Highest first" if owner.sort_descending else "↑ Lowest first"))
    if hasattr(owner, "table"):
        column = next((col for col, key in _HEADER_SORT.items() if key == mode), 8)
        descending = owner.sort_descending if mode != "state" else not owner.sort_descending
        owner.table.horizontalHeader().setSortIndicator(column, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        owner.table.horizontalHeader().setSortIndicatorShown(True)
    _refresh_leaders_view(owner)

def _sort_header(owner, column):
    mode = _HEADER_SORT.get(column)
    if mode is None:
        return
    if owner.sort.currentData() == mode:
        _toggle_sort(owner)
    else:
        owner.sort.setCurrentIndex(owner.sort.findData(mode))

def _table_selection(owner):
    row = owner.table.currentRow()
    if 0 <= row < len(getattr(owner, "row_symbols", [])):
        owner.select_symbol(owner.row_symbols[row])


def _build_columns_menu(owner) -> None:
    menu = QtWidgets.QMenu(owner.columns_button)
    owner.column_actions = {}
    for column, label in ((4, "1H %"), (5, "4H %"), (6, "24H %"), (7, "RS Score"), (9, "Sector"), (10, "24H Trend")):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(True)
        action.toggled.connect(lambda checked, col=column: owner.table.setColumnHidden(col, not checked))
        owner.column_actions[column] = action
    owner.columns_button.setMenu(menu)
    owner.columns_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)

def build_leaders(owner) -> None:

    owner.sort_descending = True
    owner.state_filter = "All"
    owner.category_filter_value = "All Sectors"

    root = QtWidgets.QVBoxLayout(owner)
    root.setContentsMargins(0, 0, 0, 0)
    owner.page_scroll = QtWidgets.QScrollArea()
    owner.page_scroll.setObjectName("leadershipScroll")
    owner.page_scroll.setWidgetResizable(True)
    owner.page_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    body = QtWidgets.QWidget()
    body.setObjectName("leadershipBody")
    body.setMinimumWidth(940)
    owner.page_scroll.setWidget(body)
    root.addWidget(owner.page_scroll)

    outer = QtWidgets.QVBoxLayout(body)
    outer.setContentsMargins(14, 12, 14, 12)
    outer.setSpacing(10)


    header = QtWidgets.QWidget()
    header_layout = QtWidgets.QHBoxLayout(header)
    header_layout.setContentsMargins(2, 0, 2, 0)
    header_layout.setSpacing(12)
    title_box = QtWidgets.QVBoxLayout()
    title_box.setSpacing(1)
    title = QtWidgets.QLabel("Leaders")
    title.setObjectName("leadershipTitle")
    set_text_role(title, TextRole.WORKSPACE_TITLE)
    subtitle = QtWidgets.QLabel("Relative strength, momentum and market rotation · Completed hourly data")
    subtitle.setObjectName("leadershipSubtitle")
    set_text_role(subtitle, TextRole.UI_BODY)
    title_box.addWidget(title)
    title_box.addWidget(subtitle)
    header_layout.addLayout(title_box, 1)

    owner.coverage = ElidedLabel("Waiting for market data")
    owner.coverage.setObjectName("leadershipCoverage")
    owner.asof = QtWidgets.QLabel("Waiting for hourly history")
    owner.asof.setObjectName("leadershipMuted")
    header_layout.addWidget(owner.asof)
    outer.addWidget(header)
    outer.addWidget(owner.coverage)


    controls = QtWidgets.QFrame()
    controls.setObjectName("leadersControlsPanel")
    controls_layout = QtWidgets.QVBoxLayout(controls)
    controls_layout.setContentsMargins(12, 10, 12, 10)
    filters = QtWidgets.QHBoxLayout()
    filters.setSpacing(10)
    header_layout = filters
    coverage_label = QtWidgets.QLabel("History coverage")
    coverage_label.setObjectName("leadersControlLabel")
    filters.addWidget(coverage_label)
    owner.span = _SegmentedSelector("leadersSegmentButton")
    for hours, caption in ((1, "1H"), (4, "4H"), (6, "6H"), (12, "12H"), (24, "24H"), (72, "3D"), (168, "7D")):
        owner.span.addItem(caption, hours)
    owner.span.setCurrentIndex(4)
    owner.span.currentIndexChanged.connect(owner.render)
    header_layout.addWidget(owner.span)

    universe_label = QtWidgets.QLabel("Universe")
    universe_label.setObjectName("leadersControlLabel")
    header_layout.addWidget(universe_label)
    owner.limit = _SegmentedSelector("leadersSegmentButton")
    for n in (40, 80, 160, 0):
        owner.limit.addItem(str(n) if n else "All", n)
    owner.limit.setCurrentIndex(1)
    owner.limit.currentIndexChanged.connect(owner._filters_changed)
    header_layout.addWidget(owner.limit)

    liquidity_label = QtWidgets.QLabel("Liquidity")
    liquidity_label.setObjectName("leadersControlLabel")
    header_layout.addWidget(liquidity_label)
    owner.liquidity = _SegmentedSelector("leadersSegmentButton")
    for label, value in (("5M", 5_000_000), ("20M", 20_000_000), ("50M", 50_000_000), ("100M", 100_000_000)):
        owner.liquidity.addItem(label, value)
    owner.liquidity.setCurrentIndex(1)
    owner.liquidity.currentIndexChanged.connect(owner._filters_changed)
    header_layout.addWidget(owner.liquidity)

    controls_layout.addLayout(filters)
    header_layout.addStretch(1)
    header_layout = QtWidgets.QHBoxLayout()
    header_layout.setSpacing(10)
    controls_layout.addLayout(header_layout)
    outer.addWidget(controls)
    sort_label = QtWidgets.QLabel("Sort by")
    sort_label.setObjectName("leadersControlLabel")
    header_layout.addWidget(sort_label)
    owner.sort = QtWidgets.QComboBox()
    owner.sort.setObjectName("leadersCombo")
    owner.sort.setMinimumWidth(145)
    for caption, key in (("Leadership state", "state"), ("RS score", "score"), ("4H vs BTC", "rs4"), ("Symbol", "pair"), ("Name", "name"), ("Price", "price"), ("1H return", "usd1"), ("4H return", "usd4"), ("24H return", "usd24"), ("Sector", "sector")):
        owner.sort.addItem(caption, key)
    owner.sort.currentIndexChanged.connect(lambda _index: _sort_changed(owner))
    header_layout.addWidget(owner.sort)
    owner.direction_button = QtWidgets.QPushButton("↓ Highest first")
    owner.direction_button.setObjectName("leadersToolbarButton")
    owner.direction_button.clicked.connect(lambda: _toggle_sort(owner))
    header_layout.addWidget(owner.direction_button)

    sector_label = QtWidgets.QLabel("Sector")
    sector_label.setObjectName("leadersControlLabel")
    header_layout.addWidget(sector_label)
    owner.category_filter = QtWidgets.QComboBox()
    owner.category_filter.setObjectName("leadersCombo")
    owner.category_filter.setMinimumWidth(160)
    owner.category_filter.addItem("All Sectors", "All Sectors")
    owner.category_filter.currentTextChanged.connect(lambda text: _category_changed(owner, text))
    header_layout.addWidget(owner.category_filter)
    header_layout.addStretch(1)

    venue = QtWidgets.QLabel("Binance USD-M")
    venue.setObjectName("leadersVenueChip")
    header_layout.addWidget(venue, 0, Qt.AlignmentFlag.AlignVCenter)


    strip = QtWidgets.QFrame()
    strip.setObjectName("leadersContextStrip")
    strip_layout = QtWidgets.QHBoxLayout(strip)
    strip_layout.setContentsMargins(0, 0, 0, 0)
    strip_layout.setSpacing(0)
    owner.market_metrics: dict[str, tuple[QtWidgets.QLabel, QtWidgets.QLabel]] = {}
    for key, title in (
        ("trend", "MARKET TREND"),
        ("coverage", "LIQUID PAIRS"),
        ("volume", "24H PERP VOLUME"),
        ("btc_share", "BTC VOL SHARE"),
        ("eth_share", "ETH VOL SHARE"),
        ("rotation", "ROTATION"),
    ):
        owner.market_metrics[key] = _metric_block(strip_layout, title)
    outer.addWidget(strip)

    content = QtWidgets.QHBoxLayout()
    content.setSpacing(10)


    main_panel = QtWidgets.QFrame()
    main_panel.setObjectName("leadersMainPanel")
    main = QtWidgets.QVBoxLayout(main_panel)
    main.setContentsMargins(0, 0, 0, 0)
    main.setSpacing(0)

    toolbar = QtWidgets.QWidget()
    toolbar.setObjectName("leadersToolbar")
    toolbar_rows = QtWidgets.QVBoxLayout(toolbar)
    toolbar_rows.setContentsMargins(10, 9, 10, 8)
    toolbar_rows.setSpacing(7)
    toolbar_layout = QtWidgets.QHBoxLayout()
    toolbar_rows.addLayout(toolbar_layout)
    toolbar_layout.setSpacing(5)
    owner.state_buttons: dict[str, QtWidgets.QPushButton] = {}
    for state in ("All", "Leading", "Improving", "Cooling", "Lagging", "Flat", "Waiting"):
        button = QtWidgets.QPushButton(state)
        button.setObjectName("leadersStateTab")
        button.setCheckable(True)
        button.setChecked(state == "All")
        button.clicked.connect(lambda _checked=False, value=state: _set_state_filter(owner, value))
        owner.state_buttons[state] = button
        toolbar_layout.addWidget(button)
    toolbar_layout.addStretch(1)
    toolbar_layout = QtWidgets.QHBoxLayout()
    toolbar_rows.addLayout(toolbar_layout)
    toolbar_layout.addStretch(1)
    owner.search = QtWidgets.QLineEdit()
    owner.search.setObjectName("leadersSearch")
    owner.search.setPlaceholderText("Search symbol or name…")
    owner.search.setClearButtonEnabled(True)
    owner.search.setMinimumWidth(190)
    owner.search.setMaximumWidth(320)
    owner.search_timer = QtCore.QTimer(owner)
    owner.search_timer.setSingleShot(True)
    owner.search_timer.setInterval(180)
    owner.search_timer.timeout.connect(lambda: _refresh_leaders_view(owner))
    owner.search.textChanged.connect(lambda _text: owner.search_timer.start())
    toolbar_layout.addWidget(owner.search)
    owner.columns_button = QtWidgets.QToolButton()
    owner.columns_button.setObjectName("leadersToolbarButton")
    owner.columns_button.setText("Columns")
    toolbar_layout.addWidget(owner.columns_button)
    main.addWidget(toolbar)

    owner.sample_caption = QtWidgets.QLabel("Snapshot ranking · 1H / 4H / 24H completed returns · 24H trend")
    owner.sample_caption.setObjectName("leadersTableCaption")
    set_text_role(owner.sample_caption, TextRole.UI_LABEL)
    main.addWidget(owner.sample_caption)

    owner.table = LeadershipHistoryTable()
    owner.table.setObjectName("leadershipTable")
    owner.table.setShowGrid(False)
    owner.table.setWordWrap(False)
    owner.table.verticalHeader().hide()
    owner.table.verticalHeader().setDefaultSectionSize(42)
    owner.table.horizontalHeader().setFixedHeight(38)
    owner.table.setMinimumHeight(450)
    owner.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    owner.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    owner.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    owner.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    owner.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    owner.table.setItemDelegate(LeadershipCellDelegate(owner))
    owner.table.horizontalHeader().setSectionsClickable(True)
    owner.table.horizontalHeader().sectionClicked.connect(lambda column: _sort_header(owner, column))
    owner.table.itemSelectionChanged.connect(lambda: _table_selection(owner))
    owner.table.cellClicked.connect(owner._select_row)
    owner.table.cellDoubleClicked.connect(owner._open_row)
    main.addWidget(owner.table, 1)
    _build_columns_menu(owner)


    replay_bar = QtWidgets.QWidget()
    replay_bar.setObjectName("leadersReplayBar")
    replay = QtWidgets.QHBoxLayout(replay_bar)
    replay.setContentsMargins(10, 5, 10, 6)
    replay.setSpacing(5)
    replay_label = QtWidgets.QLabel("REPLAY")
    replay_label.setObjectName("leadersTinyLabel")
    set_text_role(replay_label, TextRole.UI_LABEL)
    replay.addWidget(replay_label)
    owner.back, owner.play, owner.forward = [LeadershipTransport(k, owner.theme) for k in ("back", "play", "forward")]
    owner.play.setCheckable(True)
    owner.back.clicked.connect(lambda: owner._seek(-1))
    owner.forward.clicked.connect(lambda: owner._seek(1))
    owner.play.toggled.connect(owner._toggle_play)
    for button in (owner.back, owner.play, owner.forward):
        replay.addWidget(button)
    owner.replay = QtWidgets.QSlider(Qt.Orientation.Horizontal)
    owner.replay.setObjectName("leadershipReplay")
    owner.replay.setRange(0, 24)
    owner.replay.setValue(24)
    owner.replay.valueChanged.connect(owner._cursor_changed)
    replay.addWidget(owner.replay, 1)
    owner.latest = QtWidgets.QPushButton("Latest")
    owner.latest.setObjectName("leadersLatestButton")
    owner.latest.clicked.connect(owner._latest)
    replay.addWidget(owner.latest)
    main.addWidget(replay_bar)
    content.addWidget(main_panel, 1)


    rail = QtWidgets.QVBoxLayout()
    rail.setSpacing(8)
    sector_panel = QtWidgets.QFrame()
    sector_panel.setObjectName("leadersSidePanel")
    sector_layout = QtWidgets.QVBoxLayout(sector_panel)
    sector_layout.setContentsMargins(12, 10, 12, 10)
    sector_title = QtWidgets.QLabel("Sector Rotation")
    sector_title.setObjectName("leadersSideTitle")
    set_text_role(sector_title, TextRole.PANEL_TITLE)
    sector_layout.addWidget(sector_title)
    owner.sector_bars = SectorBars(owner.theme)
    sector_layout.addWidget(owner.sector_bars)
    rail.addWidget(sector_panel, 1)

    gainers_panel, _g_layout, owner.gainer_rows = _rank_panel("Top Gainers (24h)")
    losers_panel, _l_layout, owner.loser_rows = _rank_panel("Top Losers (24h)")
    rail.addWidget(gainers_panel)
    rail.addWidget(losers_panel)

    distribution = QtWidgets.QFrame()
    distribution.setObjectName("leadersSidePanel")
    dist_layout = QtWidgets.QVBoxLayout(distribution)
    dist_layout.setContentsMargins(12, 10, 12, 10)
    dist_head = QtWidgets.QHBoxLayout()
    dist_title = QtWidgets.QLabel("Leaders Distribution")
    dist_title.setObjectName("leadersSideTitle")
    set_text_role(dist_title, TextRole.PANEL_TITLE)
    owner.distribution_total = QtWidgets.QLabel("Total: 0 coins")
    owner.distribution_total.setObjectName("leadershipMuted")
    set_text_role(owner.distribution_total, TextRole.UI_LABEL)
    dist_head.addWidget(dist_title)
    dist_head.addStretch(1)
    dist_head.addWidget(owner.distribution_total)
    dist_layout.addLayout(dist_head)
    owner.distribution_bar = DistributionBar(owner.theme)
    dist_layout.addWidget(owner.distribution_bar)
    owner.distribution_labels: dict[str, QtWidgets.QLabel] = {}
    dist_values = QtWidgets.QHBoxLayout()
    for state in ("Leading", "Improving", "Cooling", "Lagging"):
        label = QtWidgets.QLabel("0\n" + state)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName("leadersDistributionValue")
        set_text_role(label, TextRole.UI_LABEL)
        dist_values.addWidget(label, 1)
        owner.distribution_labels[state] = label
    dist_layout.addLayout(dist_values)
    distribution.setMinimumHeight(116)
    rail.addWidget(distribution)

    rail_widget = QtWidgets.QWidget()
    rail_widget.setObjectName("leadersRightRail")
    rail_widget.setMinimumWidth(280)
    rail_widget.setMaximumWidth(320)
    rail_widget.setLayout(rail)
    content.addWidget(rail_widget, 0)
    owner.context_button = QtWidgets.QPushButton("Market context")
    owner.context_button.setObjectName("leadersToolbarButton")
    owner.context_button.setCheckable(True)
    owner.context_button.setChecked(True)
    owner.context_button.setToolTip("Show or hide sector rotation and market rankings to give the table more room")
    owner.context_button.toggled.connect(rail_widget.setVisible)
    toolbar_layout.insertWidget(0, owner.context_button)
    outer.addLayout(content, 1)


    owner.refresh_button = QtWidgets.QPushButton("Refresh")
    owner.refresh_button.setObjectName("leadersToolbarButton")
    owner.refresh_button.clicked.connect(lambda _checked=False: owner.refresh(force=True))
    toolbar_layout.insertWidget(toolbar_layout.count() - 1, owner.refresh_button)
    owner.scope = QtWidgets.QLabel("Current liquid universe")
    owner.scope.setWordWrap(True)
    owner.scope.setObjectName("leadershipMuted")
    outer.addWidget(owner.scope)


    support = QtWidgets.QWidget(owner)
    support.hide()
    support_layout = QtWidgets.QVBoxLayout(support)
    owner.cards, owner.empty = {}, {}
    for state in ("Improving", "Cooling"):
        owner.cards[state] = []
        for _ in range(2):
            card = LeadersPairCard(owner)
            card.chosen.connect(owner.select_symbol)
            support_layout.addWidget(card)
            owner.cards[state].append(card)
        empty = QtWidgets.QLabel("Waiting for comparable history")
        support_layout.addWidget(empty)
        owner.empty[state] = empty
    owner.confirmation = LeadersPairCard(owner, confirmation=True)
    owner.confirmation.chosen.connect(owner.select_symbol)
    support_layout.addWidget(owner.confirmation)
    owner.selected_name = owner.confirmation.name
    owner.watch_selected, owner.open_selected = owner.confirmation.watch, owner.confirmation.open
    owner.facts, owner.fact_marks = {}, {}
    for key, caption in (("relative", "Relative strength positive"), ("share", "Volume share rising"),
                         ("baseline", "Above volume baseline"), ("range", "4H range high"),
                         ("spot", "Spot participation"), ("liquidity", "Liquidity"), ("funding", "Funding")):
        mark = QtWidgets.QLabel()
        set_text_role(mark, TextRole.UI_GLYPH)
        label = ElidedLabel(caption)
        support_layout.addWidget(mark)
        support_layout.addWidget(label)
        owner.facts[key], owner.fact_marks[key] = label, mark
    for key in ("volume", "oi"):
        owner.facts[key] = ElidedLabel("-", support)
        support_layout.addWidget(owner.facts[key])

    owner.changes = ElidedLabel("Recent changes will appear after history loads.")
    owner.changes.setObjectName("leadershipMuted")
    outer.addWidget(owner.changes)
    actions = QtWidgets.QHBoxLayout()
    owner.chart_button = QtWidgets.QPushButton("Open chart")
    owner.watch_button = QtWidgets.QPushButton("☆ Toggle watchlist")
    for button in (owner.chart_button, owner.watch_button):
        button.setObjectName("leadersToolbarButton")
        button.setEnabled(False)
        actions.addWidget(button)
    actions.addStretch(1)
    main.insertLayout(main.count() - 1, actions)
    owner.chart_button.clicked.connect(lambda: owner.symbol_selected.emit(owner.selected) if owner.selected else None)
    owner.watch_button.clicked.connect(lambda: owner.toggle_watch(owner.selected))
    owner.search_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), owner)
    owner.search_shortcut.activated.connect(owner.search.setFocus)
    owner.back.setToolTip("Previous completed hour")
    owner.play.setToolTip("Play / pause hourly replay")
    owner.forward.setToolTip("Next completed hour")
    owner.refresh_button.setToolTip("Reload market histories")
    for widget in owner.findChildren(QtWidgets.QAbstractButton):
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.setAccessibleName(widget.text() or widget.toolTip())

def _compact_money(value: float) -> str:
    if not math.isfinite(value):
        return "-"
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"${value / scale:.2f}{suffix}"
    return f"${value:,.0f}"


def _set_metric(owner, key: str, value: str, sub: str = "", direction: str = "") -> None:
    value_label, sub_label = owner.market_metrics[key]
    if value_label.text() != value:
        value_label.setText(value)
    if value_label.property("direction") != direction:
        value_label.setProperty("direction", direction)
        style = value_label.style()
        style.unpolish(value_label)
        style.polish(value_label)
    if sub_label.text() != sub:
        sub_label.setText(sub)


def _prepare_dashboard(model):
    blocks = []
    metrics = model.metrics
    valid = [symbol for symbol in model.symbols if metrics.get(symbol, {}).get("rs4") is not None]
    positive = sum(1 for symbol in valid if (metrics[symbol].get("rs4") or 0) > 0)
    breadth = 100.0 * positive / len(valid) if valid else 0.0
    if not valid:
        trend, trend_sub, trend_dir = "Waiting", "Comparable history loading", ""
    elif breadth >= 60:
        trend, trend_sub, trend_dir = "Risk On ↑", f"{breadth:.0f}% outperforming BTC (4H)", "positive"
    elif breadth <= 40:
        trend, trend_sub, trend_dir = "Risk Off ↓", f"{breadth:.0f}% outperforming BTC (4H)", "negative"
    else:
        trend, trend_sub, trend_dir = "Mixed", f"{breadth:.0f}% outperforming BTC (4H)", ""
    blocks.append(("trend", trend, trend_sub, trend_dir))
    blocks.append(("coverage", str(len(model.symbols)), f"{len(valid)} with comparable 4H history"))

    universe = model.valid_symbols or set(model.tickers)
    volume_by_symbol = {symbol: max(0.0, safe_float(model.tickers.get(symbol, {}).get("q"), 0.0)) for symbol in universe}
    total_volume = sum(volume_by_symbol.values())
    blocks.append(("volume", _compact_money(total_volume), "Current Binance 24H quote volume"))
    btc_share = 100.0 * volume_by_symbol.get("BTCUSDT", 0.0) / total_volume if total_volume > 0 else 0.0
    eth_share = 100.0 * volume_by_symbol.get("ETHUSDT", 0.0) / total_volume if total_volume > 0 else 0.0
    blocks.append(("btc_share", f"{btc_share:.1f}%" if total_volume else "-", "Share of tracked perp volume"))
    blocks.append(("eth_share", f"{eth_share:.1f}%" if total_volume else "-", "Share of tracked perp volume"))

    sector_values: dict[str, list[float]] = defaultdict(list)
    for symbol in valid:
        category = model.identity(symbol)[1]
        value = metrics[symbol].get("rs4")
        if category and category not in {"-", "—", "Benchmark"} and value is not None:
            sector_values[category].append(value)
    sectors = sorted(
        ((category, sum(values) / len(values)) for category, values in sector_values.items() if values),
        key=lambda pair: pair[1], reverse=True,
    )[:6]
    if sectors:
        top_name, top_value = sectors[0]
        blocks.append(("rotation", f"{top_name} Gaining ↑" if top_value > 0 else f"{top_name} Weak",
                    f"Avg 4H relative return {top_value:+.2f}%", "positive" if top_value > 0 else "negative"))
    else:
        blocks.append(("rotation", "Waiting", "Sector tags loading"))

    categories = [name for name, _value in sectors]
    categories.extend(sorted(name for name in sector_values if name not in categories))
    gainers = sorted((symbol for symbol in model.symbols if metrics.get(symbol, {}).get("usd24") is not None),
                     key=lambda symbol: metrics[symbol]["usd24"], reverse=True)
    return {"blocks": blocks, "sectors": sectors, "categories": categories,
            "gainers": [s for s in gainers if metrics[s]["usd24"] > 0][:5],
            "losers": [s for s in reversed(gainers) if metrics[s]["usd24"] < 0][:5],
            "counts": dict(Counter(metrics.get(symbol, {}).get("state", "Waiting") for symbol in model.symbols))}


def update_dashboard(owner, prepared) -> None:
    """Apply already-computed values; all cohort scans run in the worker."""
    metrics = owner.metrics
    for block in prepared["blocks"]:
        _set_metric(owner, *block)
    owner.sector_bars.set_values(prepared["sectors"])

    categories = prepared["categories"]
    current = owner.category_filter.currentText() or "All Sectors"
    desired = ["All Sectors", *categories]
    if [owner.category_filter.itemText(i) for i in range(owner.category_filter.count())] != desired:
        blocker = QtCore.QSignalBlocker(owner.category_filter)
        owner.category_filter.clear()
        owner.category_filter.addItems(desired)
        index = owner.category_filter.findText(current)
        owner.category_filter.setCurrentIndex(index if index >= 0 else 0)
        del blocker
        owner.category_filter_value = owner.category_filter.currentText()

    gainers, losers = prepared["gainers"], prepared["losers"]

    def fill(rows, symbols) -> None:
        for index, widgets in enumerate(rows):
            symbol_label, name_label, change_label = widgets
            if index < len(symbols):
                symbol = symbols[index]
                name, _category = owner.identity(symbol)
                change = metrics[symbol].get("usd24")
                symbol_text = symbol.removesuffix("USDT")
                if symbol_label.text() != symbol_text:
                    symbol_label.setText(symbol_text)
                if name_label.text() != name:
                    name_label.setText(name)
                change_text = "-" if change is None else f"{change:+.1f}%"
                if change_label.text() != change_text:
                    change_label.setText(change_text)
                color = owner.theme.get("green", "#4DDFA4") if change is not None and change >= 0 else owner.theme.get("red", "#FF7A85")
                change_style = f"color: {color};"
                if change_label.styleSheet() != change_style:
                    change_label.setStyleSheet(change_style)
                for label in widgets:
                    label.setToolTip(f"{symbol} · 24H completed-candle return")
            else:
                if symbol_label.text() != "-":
                    symbol_label.setText("-")
                if name_label.text():
                    name_label.setText("")
                if change_label.text() != "-":
                    change_label.setText("-")
                if change_label.styleSheet():
                    change_label.setStyleSheet("")

    fill(owner.gainer_rows, gainers[:5])
    fill(owner.loser_rows, losers[:5])

    counts = prepared["counts"]
    owner.distribution_bar.set_counts(dict(counts))
    total_text = f"Total: {len(owner.symbols)} coins"
    if owner.distribution_total.text() != total_text:
        owner.distribution_total.setText(total_text)
    for state, label in owner.distribution_labels.items():
        label_text = f"{counts.get(state, 0)}\n{state}"
        if label.text() != label_text:
            label.setText(label_text)
        stylesheet = f"color: {_leader_color(state, owner.theme)};"
        if label.styleSheet() != stylesheet:
            label.setStyleSheet(stylesheet)
    for state, button in owner.state_buttons.items():
        count = len(owner.symbols) if state == "All" else counts.get(state, 0)
        caption = "All Leaders" if state == "All" else state
        button_text = f"{caption} ({count})"
        if button.text() != button_text:
            button.setText(button_text)


def leaders_stylesheet(theme: dict[str, str] | None = None) -> str:
    """Neutral-black NEXUS-style chrome aligned to the shared Nightwatch theme."""

    theme = theme or {}
    bg = theme.get("bg", "#0B0D11")
    panel = theme.get("panel", "#12151B")
    panel2 = theme.get("panel2", "#181C24")
    header = theme.get("header", panel2)
    border = theme.get("border", "#2A313D")
    separator = theme.get("separator", border)
    control = theme.get("control", "#181C24")
    control_border = theme.get("control_border", border)
    hover = theme.get("control_hover", "#1E2B3E")
    text = theme.get("text", "#DFE5EE")
    muted = theme.get("muted", "#9AA4B4")
    active = theme.get("active", "#1E2B3E")
    active_line = theme.get("active_line", "#8AA9FF")
    green = theme.get("green", "#4DDFA4")
    red = theme.get("red", "#FF7A85")

    return f"""
        QWidget#leadershipTimeline, QWidget#leadershipBody, QScrollArea#leadershipScroll {{
            background: {bg}; color: {text}; border: 0;
        }}
        QWidget#leadershipTimeline QLabel {{ background: transparent; border: 0; color: {text}; }}
        QLabel#leadershipTitle {{ color: {text}; }}
        QLabel#leadershipSubtitle {{ color: {muted}; }}
        QLabel#leadershipCoverage, QLabel#leadershipMuted {{ color: {muted}; }}

        QFrame#leadersContextStrip, QFrame#leadersMainPanel, QFrame#leadersSidePanel, QFrame#leadersControlsPanel {{
            background: {panel}; border: 1px solid {border}; border-radius: 6px;
        }}
        QFrame#leadersMetricBlock {{
            background: transparent; border: 0; border-right: 1px solid {separator}; border-radius: 0;
        }}
        QLabel#leadersMetricTitle {{ color: {muted}; }}
        QLabel#leadersMetricValue {{ color: {text}; }}
        QLabel#leadersMetricValue[direction="positive"] {{ color: {green}; }}
        QLabel#leadersMetricValue[direction="negative"] {{ color: {red}; }}
        QLabel#leadersMetricSub {{ color: {muted}; }}

        QLabel#leadersControlLabel {{ color: {muted}; padding: 0 2px; }}
        QLabel#leadersVenueChip {{
            background: {control}; color: {muted}; border: 1px solid {border}; border-radius: 5px;
            min-height: 26px; padding: 2px 9px;
        }}
        QPushButton#leadersSegmentButton, QPushButton#leadersCategoryButton {{
            background: {control}; color: {muted}; border: 1px solid {control_border}; border-radius: 4px;
            min-height: 22px; padding: 0 6px;
        }}
        QPushButton#leadersSegmentButton:hover, QPushButton#leadersCategoryButton:hover {{
            background: {hover}; color: {text}; border-color: {control_border};
        }}
        QPushButton#leadersSegmentButton:checked, QPushButton#leadersCategoryButton:checked {{
            background: {active}; color: {text}; border-color: {active_line};
        }}
        QScrollArea#leadersCategoryScroll {{
            background: transparent; border: 0;
        }}
        QScrollArea#leadersCategoryScroll > QWidget > QWidget {{
            background: transparent;
        }}
        QScrollArea#leadersCategoryScroll QScrollBar:horizontal {{
            background: transparent; height: 4px; margin: 0;
        }}
        QScrollArea#leadersCategoryScroll QScrollBar::handle:horizontal {{
            background: {control_border}; min-width: 18px; border-radius: 2px;
        }}
        QScrollArea#leadersCategoryScroll QScrollBar::add-line:horizontal,
        QScrollArea#leadersCategoryScroll QScrollBar::sub-line:horizontal {{
            width: 0;
        }}

        QWidget#leadersToolbar {{ background: {panel}; border: 0; border-bottom: 1px solid {separator}; }}
        QPushButton#leadersStateTab {{
            background: {control}; color: {muted}; border: 1px solid {border}; border-radius: 5px;
            min-height: 27px; padding: 3px 6px;
        }}
        QPushButton#leadersStateTab:hover {{ color: {text}; background: {hover}; }}
        QPushButton#leadersStateTab:checked {{ background: {active}; color: {text}; border-color: {active_line}; }}
        QLineEdit#leadersSearch {{
            background: {control}; color: {text}; border: 1px solid {border}; border-radius: 5px;
            min-height: 27px; padding: 3px 8px;
        }}
        QPushButton#leadersToolbarButton, QToolButton#leadersToolbarButton {{
            background: {control}; color: {text}; border: 1px solid {border}; border-radius: 5px;
            min-height: 27px; padding: 3px 9px;
        }}
        QPushButton#leadersToolbarButton:hover, QToolButton#leadersToolbarButton:hover {{ background: {hover}; border-color: {active_line}; }}
        QLabel#leadersTableCaption {{ color: {muted}; padding: 5px 11px; border-bottom: 1px solid {separator}; }}

        QTableWidget#leadershipTable {{
            background: {panel}; color: {text}; border: 0; outline: 0; gridline-color: {separator};
            selection-background-color: transparent; alternate-background-color: {panel2};
        }}
        QTableWidget#leadershipTable QHeaderView::section {{
            background: {header}; color: {muted}; border: 0; border-bottom: 1px solid {separator};
            padding: 5px 6px;
        }}
        QTableWidget#leadershipTable QHeaderView::section:hover {{ color: {text}; background: {hover}; }}

        QWidget#leadersReplayBar {{ background: {panel2}; border: 0; border-top: 1px solid {separator}; }}
        QLabel#leadersTinyLabel {{ color: {muted}; }}
        QPushButton#leadershipTransport {{ background: transparent; border: 0; padding: 0; }}
        QPushButton#leadersLatestButton {{ background: {control}; color: {muted}; border: 1px solid {border}; border-radius: 4px; padding: 2px 7px; }}
        QSlider#leadershipReplay::groove:horizontal {{ background: {separator}; height: 3px; }}
        QSlider#leadershipReplay::handle:horizontal {{ background: {active_line}; width: 8px; margin: -4px 0; border-radius: 4px; }}

        QComboBox#leadersCombo {{ background: #000000; color: {text}; border: 1px solid {border}; border-radius: 5px; min-height: 30px; padding: 0 10px; }}
        QComboBox#leadersCombo QAbstractItemView {{ background: #000000; color: {text}; selection-background-color: #000000; selection-color: {active_line}; border: 1px solid {border}; }}
        QComboBox#leadersCombo:focus, QLineEdit#leadersSearch:focus {{ border-color: {active_line}; }}
        QPushButton#leadersToolbarButton:focus, QToolButton#leadersToolbarButton:focus, QPushButton#leadersStateTab:focus {{ border-color: {active_line}; }}
        QPushButton#leadersToolbarButton:disabled {{ color: {muted}; border-color: {separator}; }}
        QLabel#leadersSideTitle {{ color: {text}; }}
        QLabel#leadersRank {{ color: {muted}; }}
        QLabel#leadersRankSymbol {{ color: {text}; min-width: 52px; }}
        QLabel#leadersRankName {{ color: {muted}; }}
        QLabel#leadersRankChange {{ color: {text}; }}
        QLabel#leadersDistributionValue {{ color: {muted}; }}

        QMenu#leadershipFilters, QWidget#leadershipTimeline QMenu {{ background: {panel}; color: {text}; border: 1px solid {border}; }}
        QWidget#leadershipTimeline QMenu QPushButton {{
            background: {control}; color: {text}; border: 1px solid {border}; border-radius: 4px; min-height: 24px; padding: 3px 7px;
        }}
        QWidget#leadershipTimeline QScrollBar:vertical {{ background: {panel}; width: 7px; margin: 0; }}
        QWidget#leadershipTimeline QScrollBar::handle:vertical {{ background: {theme.get('control_border', '#3A4250')}; min-height: 24px; border-radius: 3px; }}
        QWidget#leadershipTimeline QScrollBar::add-line:vertical, QWidget#leadershipTimeline QScrollBar::sub-line:vertical {{ height: 0; }}
    """


_SECTOR_OVERVIEW_MINUTE = 60_000
_SECTOR_OVERVIEW_QUARTER = 15 * _SECTOR_OVERVIEW_MINUTE
_SECTOR_OVERVIEW_HOUR = 60 * _SECTOR_OVERVIEW_MINUTE
_SECTOR_OVERVIEW_DAY = 24 * _SECTOR_OVERVIEW_HOUR
_SECTOR_OVERVIEW_BENCHMARK = "BTCUSDT"
_SECTOR_OVERVIEW_SECTOR_REFRESH_MS = 90_000
_SECTOR_OVERVIEW_ACTIVE_BATCH_SIZE = 6
_SECTOR_OVERVIEW_ACTIVE_BATCH_DELAY_MS = 250

_SECTOR_OVERVIEW_TIMEFRAMES: OrderedDict[str, tuple[str, int, int]] = OrderedDict(
    (
        ("15m", ("15M", _SECTOR_OVERVIEW_QUARTER, 1)),
        ("1h", ("1H", _SECTOR_OVERVIEW_HOUR, 1)),
        ("4h", ("4H", _SECTOR_OVERVIEW_HOUR, 4)),
        ("1d", ("1D", _SECTOR_OVERVIEW_HOUR, 24)),
    )
)

_SECTOR_OVERVIEW_SECTOR_ORDER = ("AI", "DeFi", "L1", "Gaming", "Memes", "Infrastructure")
_SECTOR_OVERVIEW_SECTOR_COLORS = {
    "AI": "#E070D8",
    "DeFi": "#E8A64A",
    "L1": "#B08CFF",
    "Gaming": "#4CC9C0",
    "Memes": "#FF7AA0",
    "Infrastructure": "#909099",
}


def _sector_overview_sector_color(sector: str, theme: dict[str, str], *, for_text: bool = False) -> str:
    """Return categorical sector ink that remains readable on dark or light themes."""
    raw = QtGui.QColor(_SECTOR_OVERVIEW_SECTOR_COLORS.get(sector, theme.get("cyan", "#8AA9FF")))
    surface = QtGui.QColor(theme.get("panel", theme.get("bg", "#0B0D11")))


    if surface.lightnessF() > 0.62:
        raw = raw.darker(165 if for_text else 145)
    return raw.name()


_SECTOR_OVERVIEW_SECTOR_MEMBERSHIP: dict[str, str] = {

    "FET": "AI", "RENDER": "AI", "RNDR": "AI", "TAO": "AI", "WLD": "AI",
    "ARKM": "AI", "PHB": "AI", "NFP": "AI", "IO": "AI", "AIXBT": "AI",
    "KAITO": "AI", "VIRTUAL": "AI", "AI": "AI", "AGIX": "AI", "OCEAN": "AI",

    "AAVE": "DeFi", "UNI": "DeFi", "CRV": "DeFi", "MKR": "DeFi",
    "COMP": "DeFi", "SUSHI": "DeFi", "DYDX": "DeFi", "GMX": "DeFi",
    "SNX": "DeFi", "LDO": "DeFi", "JTO": "DeFi", "JUP": "DeFi",
    "ENA": "DeFi", "PENDLE": "DeFi", "RUNE": "DeFi", "INJ": "DeFi",
    "1INCH": "DeFi", "CAKE": "DeFi", "KAVA": "DeFi", "ZRX": "DeFi",

    "ETH": "L1", "SOL": "L1", "BNB": "L1", "ADA": "L1", "AVAX": "L1",
    "SUI": "L1", "SEI": "L1", "NEAR": "L1", "TON": "L1", "APT": "L1",
    "DOT": "L1", "ATOM": "L1", "ALGO": "L1", "TRX": "L1", "EGLD": "L1",
    "HBAR": "L1", "KAS": "L1", "ICP": "L1", "CELO": "L1",

    "IMX": "Gaming", "GALA": "Gaming", "SAND": "Gaming", "MANA": "Gaming",
    "AXS": "Gaming", "ENJ": "Gaming", "PIXEL": "Gaming", "PORTAL": "Gaming",
    "BEAMX": "Gaming", "BIGTIME": "Gaming", "YGG": "Gaming", "RONIN": "Gaming",
    "ACE": "Gaming", "XAI": "Gaming", "NOT": "Gaming",

    "DOGE": "Memes", "SHIB": "Memes", "PEPE": "Memes", "WIF": "Memes",
    "BONK": "Memes", "FLOKI": "Memes", "MEME": "Memes", "BOME": "Memes",
    "POPCAT": "Memes", "TURBO": "Memes", "NEIRO": "Memes", "1000SATS": "Memes",
    "1000RATS": "Memes", "BRETT": "Memes", "MOG": "Memes", "PENGU": "Memes",

    "LINK": "Infrastructure", "ARB": "Infrastructure", "OP": "Infrastructure",
    "STRK": "Infrastructure", "ZK": "Infrastructure", "MANTA": "Infrastructure",
    "TIA": "Infrastructure", "FIL": "Infrastructure", "GRT": "Infrastructure",
    "STX": "Infrastructure", "AR": "Infrastructure", "THETA": "Infrastructure",
    "IOTA": "Infrastructure", "POL": "Infrastructure", "MATIC": "Infrastructure",
    "W": "Infrastructure", "WORMHOLE": "Infrastructure", "ZRO": "Infrastructure",
    "PYTH": "Infrastructure", "LAYER": "Infrastructure", "ALT": "Infrastructure",
}

_SECTOR_OVERVIEW_TAG_SECTOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AI", ("artificial intelligence", "ai & big data", "ai innovation", "ai")),
    ("Memes", ("meme", "memecoin")),
    ("Gaming", ("gaming", "gamefi", "metaverse")),
    ("DeFi", ("defi", "decentralized finance", "dex", "liquid staking", "yield")),
    ("L1", ("layer 1", "layer-1", "layer1")),
    ("Infrastructure", ("layer 2", "layer-2", "layer2", "oracle", "storage", "infrastructure", "interoperability", "modular", "data availability")),
)


def _sector_overview_finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _sector_overview_pct(value: float | None, digits: int = 1) -> str:
    return "—" if not _sector_overview_finite(value) else f"{value:+.{digits}f}%"


def _sector_overview_share(value: float | None, digits: int = 1) -> str:
    return "—" if not _sector_overview_finite(value) else f"{value:.{digits}f}%"


def _sector_overview_series_window(
    series: dict[int, Candle],
    end: int,
    step: int,
    count: int,
) -> list[Candle]:
    if count <= 0:
        return []
    keys = range(end - (count - 1) * step, end + 1, step)
    rows = [series.get(key) for key in keys]
    if any(
        row is None
        or row.open <= 0
        or row.close <= 0
        or row.quote_volume < 0
        or not all(
            math.isfinite(value)
            for value in (row.open, row.close, row.high, row.low, row.quote_volume)
        )
        for row in rows
    ):
        return []
    return rows  # type: ignore[return-value]


def _sector_overview_return(series: dict[int, Candle], end: int, step: int, count: int) -> float | None:
    rows = _sector_overview_series_window(series, end, step, count)
    if not rows:
        return None
    return (rows[-1].close / rows[0].open - 1.0) * 100.0


def _sector_overview_relative(
    series: dict[int, Candle],
    btc: dict[int, Candle],
    end: int,
    step: int,
    count: int,
) -> float | None:
    rows = _sector_overview_series_window(series, end, step, count)
    base = _sector_overview_series_window(btc, end, step, count)
    if not rows or not base:
        return None
    return ((rows[-1].close / rows[0].open) / (base[-1].close / base[0].open) - 1.0) * 100.0


def _sector_overview_volume(series: dict[int, Candle], end: int, step: int, count: int) -> float | None:
    rows = _sector_overview_series_window(series, end, step, count)
    if not rows:
        return None
    return sum(row.quote_volume for row in rows)


def _sector_overview_median(values: list[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else None


def _sector_overview_map_candles(candles: list[Candle], step: int) -> dict[int, Candle]:
    result: dict[int, Candle] = {}
    for candle in candles:
        end = int(round(candle.time * 1000)) + step
        if candle.open > 0 and candle.close > 0 and math.isfinite(candle.close):
            result[end] = candle
    return result


def _sector_overview_classify(symbol: str, metadata: dict[str, Any] | None) -> str | None:
    if symbol == _SECTOR_OVERVIEW_BENCHMARK or not symbol.endswith("USDT"):
        return None
    base = coin_base_symbol(symbol)
    curated = _SECTOR_OVERVIEW_SECTOR_MEMBERSHIP.get(base) or _SECTOR_OVERVIEW_SECTOR_MEMBERSHIP.get(coin_remote_symbol(symbol))
    if curated:
        return curated
    tags: list[str] = []
    row = metadata or {}
    subtype = row.get("underlyingSubType", [])
    if isinstance(subtype, list):
        tags.extend(str(item).lower() for item in subtype)
    underlying = row.get("underlyingType")
    if underlying:
        tags.append(str(underlying).lower())
    joined = " | ".join(tags)
    if not joined:
        return None
    for sector, keywords in _SECTOR_OVERVIEW_TAG_SECTOR_RULES:
        if any(re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", joined) for keyword in keywords):
            return sector
    return None


def _sector_overview_closed_window_ready(
    rows: list[Candle],
    end_ms: int,
    step_ms: int,
    minimum_rows: int,
) -> bool:
    series = _sector_overview_map_candles(rows, step_ms)
    return bool(_sector_overview_series_window(series, end_ms, step_ms, minimum_rows))


def _sector_overview_tail_limit(
    rows: list[Candle],
    end_ms: int,
    step_ms: int,
    full_limit: int,
    minimum_rows: int,
) -> int:
    series = _sector_overview_map_candles(rows, step_ms)
    required = range(end_ms - (minimum_rows - 1) * step_ms, end_ms + 1, step_ms)
    missing = [stamp for stamp in required if not _sector_overview_series_window(series, stamp, step_ms, 1)]
    if not missing:
        return 2
    # Fetch from the oldest hole, including internal gaps, rather than merely
    # fetching the most recent tail and permanently preserving a cache hole.
    return min(full_limit, max(2, (end_ms - min(missing)) // step_ms + 1))


def _sector_overview_merge_candles(existing: list[Candle], fetched: list[Candle]) -> list[Candle]:
    merged = {int(round(row.time * 1000)): row for row in existing}
    merged.update({int(round(row.time * 1000)): row for row in fetched})
    return [merged[key] for key in sorted(merged)]


def _prepare_sector_batch_rows(output):
    result = {}
    for symbol, entry in output.items():
        prepared = dict(entry)
        for name, step in (("futures_15m", _SECTOR_OVERVIEW_QUARTER),
                           ("spot_15m", _SECTOR_OVERVIEW_QUARTER),
                           ("futures_1h", _SECTOR_OVERVIEW_HOUR),
                           ("spot_1h", _SECTOR_OVERVIEW_HOUR)):
            if prepared.get(name):
                prepared[name] = _sector_overview_map_candles(prepared[name], step)
        if prepared.get("daily"):
            prepared["daily"] = sorted(prepared["daily"], key=lambda candle: candle.time)[-32:]
        result[symbol] = prepared
    return result


def _sector_overview_load_sector_batch(
    rest: Any,
    db: Any,
    requests: dict[str, dict[str, bool]],
    end_15m: int,
    end_hour: int,
    cancel: threading.Event,
) -> dict[str, Any]:
    """Serve Sector history from SQLite first, then fetch only missing tails."""
    if cancel.is_set() or not requests:
        return {
            "data": {}, "requests": requests, "end_15m": end_15m,
            "end_hour": end_hour, "stats": {}, "cancelled": cancel.is_set(),
        }

    output: dict[str, dict[str, Any]] = {}
    network_requests: dict[str, dict[str, Any]] = {}
    stats = {
        "symbols": len(requests),
        "cache_hits": 0,
        "network_calls": 0,
        "spot_unavailable": 0,
    }
    day_end = int(end_hour // _SECTOR_OVERVIEW_DAY) * _SECTOR_OVERVIEW_DAY

    for symbol, requested in requests.items():
        if cancel.is_set():
            break
        spec: dict[str, Any] = dict(requested)
        entry: dict[str, Any] = {}

        if requested.get("futures_15m"):
            cached = db.load_candles(
                symbol, "15m", end_15m - 64 * _SECTOR_OVERVIEW_QUARTER, end_15m - 1
            )
            if cached:
                entry["futures_15m"] = cached
            if _sector_overview_closed_window_ready(cached, end_15m, _SECTOR_OVERVIEW_QUARTER, 24):
                spec["futures_15m"] = False
                stats["cache_hits"] += 1
            else:
                spec["futures_15m_limit"] = _sector_overview_tail_limit(
                    cached, end_15m, _SECTOR_OVERVIEW_QUARTER, 64, 24
                )

        if requested.get("futures_1h"):
            cached = db.load_candles(
                symbol, "1h", end_hour - 96 * _SECTOR_OVERVIEW_HOUR, end_hour - 1
            )
            if cached:
                entry["futures_1h"] = cached
            if _sector_overview_closed_window_ready(cached, end_hour, _SECTOR_OVERVIEW_HOUR, 56):
                spec["futures_1h"] = False
                stats["cache_hits"] += 1
            else:
                spec["futures_1h_limit"] = _sector_overview_tail_limit(
                    cached, end_hour, _SECTOR_OVERVIEW_HOUR, 96, 56
                )

        if requested.get("spot_15m"):
            cached = db.load_candles(
                symbol, "spot:15m", end_15m - 64 * _SECTOR_OVERVIEW_QUARTER, end_15m - 1
            )
            if cached:
                entry["spot_15m"] = cached
            if _sector_overview_closed_window_ready(cached, end_15m, _SECTOR_OVERVIEW_QUARTER, 24):
                spec["spot_15m"] = False
                stats["cache_hits"] += 1
            else:
                spec["spot_15m_limit"] = _sector_overview_tail_limit(
                    cached, end_15m, _SECTOR_OVERVIEW_QUARTER, 64, 24
                )

        if requested.get("spot_1h"):


            cached = db.load_candles(
                symbol, "spot:1h", end_hour - 96 * _SECTOR_OVERVIEW_HOUR, end_hour - 1
            )
            if cached:
                entry["spot_1h"] = cached
            if _sector_overview_closed_window_ready(cached, end_hour, _SECTOR_OVERVIEW_HOUR, 56):
                spec["spot_1h"] = False
                stats["cache_hits"] += 1
            else:
                spec["spot_1h_limit"] = _sector_overview_tail_limit(
                    cached, end_hour, _SECTOR_OVERVIEW_HOUR, 96, 56
                )

        if requested.get("daily"):
            cached = db.load_candles(
                symbol, "1d", day_end - 32 * _SECTOR_OVERVIEW_DAY, day_end - 1
            )
            cached = [
                candle for candle in cached
                if int(round(candle.time * 1000)) + _SECTOR_OVERVIEW_DAY <= day_end
            ]
            if cached:
                entry["daily"] = cached
            if _sector_overview_closed_window_ready(cached, day_end, _SECTOR_OVERVIEW_DAY, 20):
                spec["daily"] = False
                stats["cache_hits"] += 1
            else:
                spec["daily_limit"] = _sector_overview_tail_limit(
                    cached, day_end, _SECTOR_OVERVIEW_DAY, 25, 20
                )

        output[symbol] = entry
        if any(spec.get(key) for key in (
            "futures_15m", "futures_1h", "daily",
            "spot_15m", "spot_1h",
        )):
            network_requests[symbol] = spec

    if cancel.is_set() or not network_requests:
        return {
            "data": _prepare_sector_batch_rows(output), "requests": requests, "end_15m": end_15m,
            "end_hour": end_hour, "stats": stats, "cancelled": cancel.is_set(),
        }

    fetched = rest.sector_snapshot_batch(network_requests, end_15m, end_hour)
    for symbol, fetched_entry in fetched.items():
        if cancel.is_set():
            break
        target = output.setdefault(symbol, {})
        stats["network_calls"] += int(fetched_entry.get("_network_calls") or 0)
        unavailable = fetched_entry.get("_spot_unavailable") or ()
        target["_spot_unavailable"] = tuple(unavailable)
        stats["spot_unavailable"] += len(unavailable)

        for key in ("futures_15m", "futures_1h", "daily"):
            rows = fetched_entry.get(key) or []
            if rows:
                target[key] = _sector_overview_merge_candles(target.get(key) or [], rows)
                db.cache_candles(
                    symbol,
                    {"futures_15m": "15m", "futures_1h": "1h", "daily": "1d"}[key],
                    rows,
                )
        for key, interval in (("spot_15m", "spot:15m"), ("spot_1h", "spot:1h")):
            if key in fetched_entry:
                rows = fetched_entry.get(key) or []
                if rows:
                    target[key] = _sector_overview_merge_candles(target.get(key) or [], rows)
                    db.cache_candles(symbol, interval, rows)
                else:
                    target.setdefault(key, [])
        if fetched_entry.get("errors"):
            target["errors"] = dict(fetched_entry["errors"])

    return {
        "data": _prepare_sector_batch_rows(output), "requests": requests, "end_15m": end_15m,
        "end_hour": end_hour, "stats": stats, "cancelled": cancel.is_set(),
    }


class _SectorOverviewSparkline(QtWidgets.QWidget):
    def __init__(self, color: str = "#8AA9FF", parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.values: list[float | None] = []
        self.color = QtGui.QColor(color)
        self.setMinimumHeight(34)

    def set_values(self, values: list[float | None], color: str | None = None) -> None:
        next_values = list(values)
        next_color = QtGui.QColor(color) if color else self.color
        if self.values == next_values and self.color == next_color:
            return
        self.values = next_values
        self.color = next_color
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        valid = [(index, value) for index, value in enumerate(self.values) if _sector_overview_finite(value)]
        if len(valid) < 2:
            return
        values = [float(value) for _, value in valid]
        low, high = min(values), max(values)
        spread = max(high - low, 1e-9)
        rect = self.rect().adjusted(2, 3, -2, -3)
        path = QtGui.QPainterPath()
        started = False
        count = max(1, len(self.values) - 1)
        for index, value in enumerate(self.values):
            if not _sector_overview_finite(value):
                started = False
                continue
            x = rect.left() + rect.width() * index / count
            y = rect.bottom() - rect.height() * (float(value) - low) / spread
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
        painter.setPen(QtGui.QPen(self.color, 1.5))
        painter.drawPath(path)


class _SectorOverviewShareBar(QtWidgets.QWidget):
    def __init__(self, color: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.ratio = 0.0
        self.color = QtGui.QColor(color)
        self.track = QtGui.QColor("#1D222B")
        self.setFixedHeight(8)

    def set_color(self, color: str) -> None:
        next_color = QtGui.QColor(color)
        if self.color.rgba() == next_color.rgba():
            return
        self.color = next_color
        self.update()

    def set_value(self, ratio: float, track: str | None = None) -> None:
        next_ratio = max(0.0, min(1.0, safe_float(ratio)))
        next_track = QtGui.QColor(track) if track else self.track
        if (
            abs(next_ratio - self.ratio) < 1e-9
            and self.track.rgba() == next_track.rgba()
        ):
            return
        self.ratio = next_ratio
        self.track = next_track
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        rect = QtCore.QRectF(self.rect())
        painter.fillRect(rect, self.track)
        if self.ratio > 0:
            painter.fillRect(QtCore.QRectF(rect.left(), rect.top(), rect.width() * self.ratio, rect.height()), self.color)


class _SectorOverviewVolumeBars(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.values: list[float | None] = []
        self.color = QtGui.QColor("#3A4250")
        self.setMinimumHeight(42)

    def set_values(self, values: list[float | None], color: str | None = None) -> None:
        next_values = [max(0.0, float(value)) if _sector_overview_finite(value) else None for value in values]
        next_color = QtGui.QColor(self.color)
        if color:
            next_color = QtGui.QColor(color)
            next_color.setAlpha(85)
        if next_values == self.values and next_color.rgba() == self.color.rgba():
            return
        self.values = next_values
        self.color = next_color
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if not self.values:
            return
        maximum = max((value for value in self.values if value is not None), default=0.0) or 1.0
        painter = QtGui.QPainter(self)
        rect = self.rect().adjusted(1, 2, -1, -2)
        width = rect.width() / max(1, len(self.values))
        for index, value in enumerate(self.values):
            if value is None:
                painter.setPen(self.color)
                painter.drawText(QtCore.QRectF(rect.left() + index * width, rect.top(), width, rect.height()),
                                 Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, '—')
                continue
            height = max(1.0, rect.height() * value / maximum)
            painter.fillRect(
                QtCore.QRectF(rect.left() + index * width + 0.5, rect.bottom() - height, max(1.0, width - 1.0), height),
                self.color,
            )


class _SectorOverviewStatCard(QtWidgets.QFrame):
    def __init__(self, title: str, theme: dict[str, str], with_bar: bool = False):
        super().__init__()
        self.setObjectName("sectorStatCard")
        self.theme = dict(theme)
        self.setMinimumHeight(80)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 10)
        layout.setSpacing(4)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("sectorStatTitle")
        set_text_role(self.title, TextRole.UI_LABEL)
        self.value = QtWidgets.QLabel("—")
        self.value.setObjectName("sectorStatValue")
        set_text_role(self.value, TextRole.MARKET_VALUE_LARGE)
        self.sub = QtWidgets.QLabel("")
        self.sub.setObjectName("sectorMuted")
        set_text_role(self.sub, TextRole.UI_BODY)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.value)
        row.addWidget(self.sub, 1)
        layout.addWidget(self.title)
        layout.addLayout(row)
        self.bar = _SectorOverviewShareBar(theme.get("cyan", "#8AA9FF")) if with_bar else None
        if self.bar is not None:
            layout.addWidget(self.bar)

    def set_metric(
        self,
        value: str,
        sub: str = "",
        color: str | None = None,
        ratio: float | None = None,
        track: str | None = None,
    ) -> None:
        if self.value.text() != value:
            self.value.setText(value)
        if self.sub.text() != sub:
            self.sub.setText(sub)
        stylesheet = f"color: {color};" if color else ""
        if self.value.styleSheet() != stylesheet:
            self.value.setStyleSheet(stylesheet)
        if self.bar is not None and ratio is not None:
            self.bar.set_value(ratio, track)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = dict(theme)
        if self.bar is not None:
            self.bar.set_color(theme.get("cyan", "#8AA9FF"))


class _SectorOverviewSectorTile(QtWidgets.QFrame):
    clicked = Signal(str)

    def __init__(self, sector: str, theme: dict[str, str]):
        super().__init__()
        self.sector = sector
        self.theme = theme
        self.selected = False
        self.setObjectName("sectorTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(13, 11, 13, 10)
        layout.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        self.marker = QtWidgets.QFrame()
        self.marker.setFixedSize(5, 30)
        self.marker.setStyleSheet(f"background: {_sector_overview_sector_color(sector, theme)}; border: 0;")
        self.name = QtWidgets.QLabel(sector)
        self.name.setObjectName("sectorTileName")
        set_text_role(self.name, TextRole.PANEL_TITLE)
        top.addWidget(self.marker)
        top.addWidget(self.name)
        top.addStretch(1)
        layout.addLayout(top)
        self.performance = QtWidgets.QLabel("—")
        self.performance.setObjectName("sectorTilePerformance")
        set_text_role(self.performance, TextRole.MARKET_VALUE_HERO)
        layout.addWidget(self.performance)
        self.bars = _SectorOverviewVolumeBars()
        layout.addWidget(self.bars, 1)
        self.share = QtWidgets.QLabel("—")
        self.share.setObjectName("sectorTileShare")
        set_text_role(self.share, TextRole.MARKET_VALUE_EMPHASIZED)
        layout.addWidget(self.share)
        self.members = QtWidgets.QLabel("")
        self.members.setObjectName("sectorMuted")
        set_text_role(self.members, TextRole.UI_LABEL)
        layout.addWidget(self.members)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = dict(theme)
        self.marker.setStyleSheet(f"background: {_sector_overview_sector_color(self.sector, theme)}; border: 0;")
        self.update()

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected)
        if selected == self.selected and self.property("selected") == selected:
            return
        self.selected = selected
        self.setProperty("selected", selected)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def update_data(self, metrics: dict[str, Any], timeframe_label: str) -> None:
        performance = metrics.get("performance")
        color = self.theme.get("green", "#4DDFA4") if not _sector_overview_finite(performance) or performance >= 0 else self.theme.get("red", "#FF7A85")
        performance_text = _sector_overview_pct(performance, 1)
        if self.performance.text() != performance_text:
            self.performance.setText(performance_text)
        performance_style = f"color: {color};"
        if self.performance.styleSheet() != performance_style:
            self.performance.setStyleSheet(performance_style)
        share = metrics.get("volume_share")
        share_text = f"{_sector_overview_share(share)} of {timeframe_label} volume"
        if self.share.text() != share_text:
            self.share.setText(share_text)
        members_text = f"{metrics.get('outperformers', 0)} / {metrics.get('covered', 0)} outperform BTC"
        if self.members.text() != members_text:
            self.members.setText(members_text)
        self.bars.set_values(metrics.get("volume_bars", []), _sector_overview_sector_color(self.sector, self.theme))
        self.bars.setToolTip(f"Turnover history · fixed cohort {metrics.get('volume_history_covered', 0)}/{metrics.get('members', 0)} pairs · — means unavailable")
        self.spark.setToolTip(f"Median vs BTC history · fixed cohort {metrics.get('trend_covered', 0)}/{metrics.get('members', 0)} pairs")

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.sector)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _SectorOverviewSectorMetricCard(QtWidgets.QFrame):
    """Large, glanceable metric used inside the selected-sector detail panel."""

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sectorMetricCard")
        self.setMinimumHeight(56)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("sectorMetricLabel")
        set_text_role(self.title, TextRole.UI_LABEL)
        self.value = QtWidgets.QLabel("—")
        self.value.setObjectName("sectorDetailMetric")
        set_text_role(self.value, TextRole.MARKET_VALUE_LARGE)
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_value(self, text: str, color: str | None = None) -> None:
        if self.value.text() != text:
            self.value.setText(text)
        stylesheet = f"color: {color};" if color else ""
        if self.value.styleSheet() != stylesheet:
            self.value.setStyleSheet(stylesheet)


class _SectorOverviewSectorPeerCard(QtWidgets.QFrame):
    """Compact ranked peer list with visible strength bars instead of tiny prose."""

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sectorPeerCard")
        self.setMinimumHeight(122)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(5)
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("sectorPeerTitle")
        set_text_role(heading, TextRole.PANEL_TITLE)
        layout.addWidget(heading)
        self.rows: list[tuple[QtWidgets.QWidget, QtWidgets.QLabel, QtWidgets.QLabel, QtWidgets.QProgressBar]] = []
        for index in range(3):
            row_host = QtWidgets.QWidget()
            row_host.setObjectName("sectorPeerRow")
            row = QtWidgets.QGridLayout(row_host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setHorizontalSpacing(7)
            row.setVerticalSpacing(2)
            rank = QtWidgets.QLabel(str(index + 1))
            rank.setObjectName("sectorPeerRank")
            set_text_role(rank, TextRole.UI_LABEL)
            rank.setFixedWidth(18)
            symbol = QtWidgets.QLabel("—")
            symbol.setObjectName("sectorPeerSymbol")
            set_text_role(symbol, TextRole.INSTRUMENT_SYMBOL)
            value = QtWidgets.QLabel("—")
            value.setObjectName("sectorPeerValue")
            set_text_role(value, TextRole.MARKET_VALUE_EMPHASIZED)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bar = QtWidgets.QProgressBar()
            bar.setObjectName("sectorPeerBar")
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setFixedHeight(5)
            row.addWidget(rank, 0, 0, 2, 1)
            row.addWidget(symbol, 0, 1)
            row.addWidget(value, 0, 2)
            row.addWidget(bar, 1, 1, 1, 2)
            layout.addWidget(row_host)
            self.rows.append((row_host, symbol, value, bar))

    def update_rows(self, items: list[tuple[str, float]]) -> None:
        clean = [(symbol, float(value)) for symbol, value in items[:3] if _sector_overview_finite(value)]
        maximum = max((abs(value) for _symbol, value in clean), default=1.0) or 1.0
        for index, (host, symbol_label, value_label, bar) in enumerate(self.rows):
            if index >= len(clean):
                if index == 0 and not clean:
                    host.setVisible(True)
                    symbol_label.setText("Waiting for comparable pairs")
                    value_label.setText("")
                    bar.setValue(0)
                else:
                    host.setVisible(False)
                continue
            host.setVisible(True)
            symbol, value = clean[index]
            symbol_label.setText(symbol.removesuffix("USDT"))
            value_label.setText(_sector_overview_pct(value, 1))
            direction = "positive" if value >= 0 else "negative"
            if value_label.property("direction") != direction:
                value_label.setProperty("direction", direction)
                style = value_label.style()
                style.unpolish(value_label)
                style.polish(value_label)
            if bar.property("direction") != direction:
                bar.setProperty("direction", direction)
                style = bar.style()
                style.unpolish(bar)
                style.polish(bar)
            target_value = max(20, round(1000 * abs(value) / maximum))
            if bar.value() != target_value:
                bar.setValue(target_value)


class _SectorOverviewSectorDetail(QtWidgets.QFrame):
    def __init__(self, theme: dict[str, str]):
        super().__init__()
        self.theme = theme
        self.setObjectName("sectorPanel")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        self.title = QtWidgets.QLabel("AI")
        self.title.setObjectName("sectorPanelTitle")
        set_text_role(self.title, TextRole.UI_HEADING)
        layout.addWidget(self.title)
        self.caption = QtWidgets.QLabel("Relative performance vs BTC")
        self.caption.setObjectName("sectorMuted")
        set_text_role(self.caption, TextRole.UI_LABEL)
        layout.addWidget(self.caption)
        self.spark = _SectorOverviewSparkline()
        self.spark.setMinimumHeight(72)
        layout.addWidget(self.spark)

        self.metric_cards: dict[str, _SectorOverviewSectorMetricCard] = {}
        metrics_grid = QtWidgets.QGridLayout()
        metrics_grid.setHorizontalSpacing(7)
        metrics_grid.setVerticalSpacing(7)
        for index, (key, label) in enumerate((
            ("share", "Volume share"),
            ("performance", "Median vs BTC"),
            ("participation", "Breadth"),
            ("spot", "Spot participation"),
        )):
            card = _SectorOverviewSectorMetricCard(label)
            metrics_grid.addWidget(card, index // 2, index % 2)
            self.metric_cards[key] = card
        layout.addLayout(metrics_grid)

        peers = QtWidgets.QHBoxLayout()
        peers.setSpacing(7)
        self.leaders = _SectorOverviewSectorPeerCard("Relative leaders")
        self.improving = _SectorOverviewSectorPeerCard("Improving peers")
        peers.addWidget(self.leaders, 1)
        peers.addWidget(self.improving, 1)
        layout.addLayout(peers)
        layout.addStretch(1)

    def update_data(self, sector: str, metrics: dict[str, Any], timeframe_label: str) -> None:
        self.title.setText(f"{sector}  ·  {metrics.get('outperformers', 0)} of {metrics.get('covered', 0)} outperform BTC")
        self.caption.setText(f"Median vs BTC · {timeframe_label} · trend {metrics.get('trend_covered', 0)}/{metrics.get('members', 0)} comparable pairs")
        self.spark.set_values(metrics.get("trend", []), _sector_overview_sector_color(sector, self.theme))
        current_share = metrics.get("volume_share")
        previous_share = metrics.get("previous_volume_share")
        self.metric_cards["share"].set_value(
            f"{_sector_overview_share(previous_share)} → {_sector_overview_share(current_share)}"
            if _sector_overview_finite(current_share) and _sector_overview_finite(previous_share)
            else "—"
        )
        performance = metrics.get("performance")
        performance_color = self.theme.get("green", "#4DDFA4") if not _sector_overview_finite(performance) or performance >= 0 else self.theme.get("red", "#FF7A85")
        self.metric_cards["performance"].set_value(_sector_overview_pct(performance, 1), performance_color)
        self.metric_cards["participation"].set_value(f"{metrics.get('outperformers', 0)} / {metrics.get('covered', 0)}")
        self.metric_cards["spot"].set_value(_sector_overview_share(metrics.get("spot_participation")))
        self.leaders.update_rows(metrics.get("leaders", []))
        self.improving.update_rows(metrics.get("improving", []))


class _SectorAnalysis:
    """Data-only analytics; instances never contain a QObject or live GUI map."""

    def __init__(self, state):
        self.__dict__.update(state)

    def _dataset(self, symbol: str, timeframe: str) -> tuple[dict[int, Candle], int, int, int]:
        label, step, count = _SECTOR_OVERVIEW_TIMEFRAMES[timeframe]
        del label
        if timeframe == "15m":
            return self.futures_15m.get(symbol, {}), self.end_15m, step, count
        return self.hourly.get(symbol, {}), self.end_hour, step, count

    def _relative_value(self, symbol: str, timeframe: str, end: int | None = None) -> float | None:
        series, default_end, step, count = self._dataset(symbol, timeframe)
        btc, _btc_end, _step, _count = self._dataset(_SECTOR_OVERVIEW_BENCHMARK, timeframe)
        return _sector_overview_relative(series, btc, default_end if end is None else end, step, count)

    def _volume_value(self, symbol: str, timeframe: str, end: int | None = None) -> float | None:
        series, default_end, step, count = self._dataset(symbol, timeframe)
        return _sector_overview_volume(series, default_end if end is None else end, step, count)

    def _sector_members(self, sector: str) -> list[str]:
        return [symbol for symbol in self.symbols if self.sectors.get(symbol) == sector]

    def _fixed_history_cohort(self, sector: str, timeframe: str, points: int, *, relative: bool) -> tuple[list[float | None], int]:
        _series, end, _step, _count = self._dataset(_SECTOR_OVERVIEW_BENCHMARK, timeframe)
        sample_step = _SECTOR_OVERVIEW_QUARTER if timeframe == "15m" else _SECTOR_OVERVIEW_HOUR
        metric = self._relative_value if relative else self._volume_value
        histories = [[metric(symbol, timeframe, end - index * sample_step)
                      for index in range(points - 1, -1, -1)]
                     for symbol in self._sector_members(sector)]
        cohort = [history for history in histories if all(value is not None for value in history)]
        if not cohort:
            return [None] * points, 0
        reduce = _sector_overview_median if relative else sum
        return [reduce([history[index] for history in cohort]) for index in range(points)], len(cohort)

    def _spot_participation(self, members: list[str], timeframe: str) -> float | None:
        if timeframe == "15m":
            end, step, count = self.end_15m, _SECTOR_OVERVIEW_QUARTER, 1
            spot_source, future_source = self.spot_15m, self.futures_15m
        else:
            count = {"1h": 1, "4h": 4, "1d": 24}[timeframe]
            end, step = self.end_hour, _SECTOR_OVERVIEW_HOUR
            spot_source, future_source = self.spot_hourly, self.hourly
        spot_total = 0.0
        future_total = 0.0
        covered = 0
        for symbol in members:
            spot = _sector_overview_volume(spot_source.get(symbol, {}), end, step, count)
            future = _sector_overview_volume(future_source.get(symbol, {}), end, step, count)
            if spot is None or future is None:
                continue
            spot_total += spot
            future_total += future
            covered += 1
        if not covered or spot_total + future_total <= 0:
            return None
        return spot_total / (spot_total + future_total) * 100.0

    def _all_sector_metrics(self, timeframe: str) -> dict[str, dict[str, Any]]:
        _series, end, step, count = self._dataset(_SECTOR_OVERVIEW_BENCHMARK, timeframe)
        previous_end = end - step * count
        current_volumes: dict[str, float] = {}
        previous_volumes: dict[str, float] = {}
        comparable: set[str] = set()
        for symbol in self.symbols:
            current = self._volume_value(symbol, timeframe, end)
            previous = self._volume_value(symbol, timeframe, previous_end)
            if current is not None and previous is not None:
                current_volumes[symbol] = current
                previous_volumes[symbol] = previous
                comparable.add(symbol)
        current_total = sum(current_volumes.values())
        previous_total = sum(previous_volumes.values())

        output: dict[str, dict[str, Any]] = {}
        for sector in _SECTOR_OVERVIEW_SECTOR_ORDER:
            members = self._sector_members(sector)
            relative_values: list[tuple[str, float]] = []
            improving: list[tuple[str, float]] = []
            for symbol in members:
                value = self._relative_value(symbol, timeframe, end)
                if value is None:
                    continue
                relative_values.append((symbol, value))
                prior = self._relative_value(symbol, timeframe, previous_end)
                if prior is not None and value > prior:
                    improving.append((symbol, value - prior))
            performance = _sector_overview_median([value for _symbol, value in relative_values])
            outperf = sum(value > 0 for _symbol, value in relative_values)
            covered = len(relative_values)
            sector_current = sum(current_volumes.get(symbol, 0.0) for symbol in members if symbol in comparable)
            sector_previous = sum(previous_volumes.get(symbol, 0.0) for symbol in members if symbol in comparable)
            volume_covered = sum(symbol in comparable for symbol in members)
            current_share = sector_current / current_total * 100.0 if volume_covered and current_total > 0 else None
            previous_share = sector_previous / previous_total * 100.0 if volume_covered and previous_total > 0 else None
            trend, trend_covered = self._fixed_history_cohort(sector, timeframe, 32 if timeframe == '1d' else 24, relative=True)
            volume_bars, volume_history_covered = self._fixed_history_cohort(sector, timeframe, 12, relative=False)
            output[sector] = {
                "performance": performance,
                "outperformers": outperf,
                "covered": covered,
                "members": len(members),
                "volume_share": current_share,
                "previous_volume_share": previous_share,
                "share_delta": (
                    current_share - previous_share
                    if _sector_overview_finite(current_share) and _sector_overview_finite(previous_share)
                    else None
                ),
                "trend": trend, "trend_covered": trend_covered,
                "volume_bars": volume_bars, "volume_history_covered": volume_history_covered,
                "volume_covered": volume_covered,
                "leaders": sorted(relative_values, key=lambda item: (-item[1], item[0]))[:3],
                "improving": sorted(improving, key=lambda item: (-item[1], item[0]))[:3],
                "spot_participation": self._spot_participation(members, timeframe),
            }
        return output

    def _sector_performances(self, timeframe: str) -> dict[str, float | None]:
        return {
            sector: _sector_overview_median([
                value
                for symbol in self._sector_members(sector)
                if (value := self._relative_value(symbol, timeframe)) is not None
            ])
            for sector in _SECTOR_OVERVIEW_SECTOR_ORDER
        }

    def _daily_means(self) -> dict[str, float]:
        end = self.end_hour // _SECTOR_OVERVIEW_DAY * _SECTOR_OVERVIEW_DAY
        return {symbol: statistics.mean(row.close for row in window)
                for symbol in self.symbols
                if (window := _sector_overview_series_window(
                    _sector_overview_map_candles(self.daily.get(symbol, []), _SECTOR_OVERVIEW_DAY),
                    end, _SECTOR_OVERVIEW_DAY, 20))}

    def _above_20d(self, means: dict[str, float]) -> tuple[int, int]:
        above = 0
        covered = 0
        for symbol, mean in means.items():
            current = safe_float(self.tickers.get(symbol, {}).get("c"))
            if current <= 0:
                continue
            covered += 1
            if current > mean:
                above += 1
        return above, covered


def _prepare_sectors(state, cached=None):
    model = _SectorAnalysis(state)
    if cached is not None:
        return {**cached, "above": model._above_20d(cached["ma20"])}
    metrics = model._all_sector_metrics(model.timeframe)
    performances = {frame: model._sector_performances(frame)
                    for frame in _SECTOR_OVERVIEW_TIMEFRAMES}
    btc, end, step, count = model._dataset(_SECTOR_OVERVIEW_BENCHMARK, model.timeframe)
    values = [value for symbol in model.symbols
              if (value := model._relative_value(symbol, model.timeframe)) is not None]
    means = model._daily_means()
    return {
        "metrics": metrics, "performances": performances,
        "btc_trend": _sector_overview_return(btc, end, step, count),
        "alt_pct": sum(value > 0 for value in values) / len(values) * 100.0 if values else None,
        "above": model._above_20d(means), "ma20": means,
        "loaded15": sum(bool(_sector_overview_series_window(model.futures_15m.get(symbol, {}), model.end_15m, _SECTOR_OVERVIEW_QUARTER, 1)) for symbol in model.symbols),
        "loaded1h": sum(bool(_sector_overview_series_window(model.hourly.get(symbol, {}), model.end_hour, _SECTOR_OVERVIEW_HOUR, 1)) for symbol in model.symbols),
    }


class SectorOverviewWidget(QtWidgets.QWidget):
    symbol_selected = Signal(str)

    def __init__(
        self,
        _ignored_theme: dict[str, str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ):


        super().__init__(parent)
        self.setObjectName("sectorOverview")
        self.theme = dict(SECTORS_PALETTE)
        self.rest = self.db = self.leadership = None
        self.can_load = lambda: True
        self.valid_symbols: set[str] = set()
        self.metadata: dict[str, dict[str, Any]] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.sectors: dict[str, str] = {}
        self.symbols: list[str] = []
        self.active = False
        self.closing = False
        self._interaction_paused = False
        self._refresh_pending = False
        self._render_dirty = True


        self.timeframe = "4h"
        self.minimum_volume = 0.0
        self.selected_sector = "AI"

        self.hourly: dict[str, dict[int, Candle]] = {}
        self.spot_hourly: dict[str, dict[int, Candle]] = {}
        self.futures_15m: dict[str, dict[int, Candle]] = {}
        self.spot_15m: dict[str, dict[int, Candle]] = {}
        self.daily: dict[str, list[Candle]] = {}
        self.end_hour = 0
        self.end_15m = 0
        self._component_attempts: dict[tuple[str, str], int] = {}
        self._component_retries: dict[tuple[str, str], tuple[int, float]] = {}
        self.task: ApiTask | None = None
        self.cancel = threading.Event()
        self._analysis_serial = 0
        self._analysis_job = LatestJob(QtCore.QThreadPool.globalInstance(), self, priority=-1)
        self._history_transport = WorkspaceHistory()
        self._analysis_job.ready.connect(self._analysis_ready)
        self._analysis_job.failed.connect(self._analysis_failed)
        self._prepared_analysis = {}
        self._build_ui()

        self.load_timer = QtCore.QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(self._next_batch)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(_SECTOR_OVERVIEW_SECTOR_REFRESH_MS)
        self.refresh_timer.timeout.connect(self.refresh)
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.render_timer.setInterval(display_frame_interval_ms(self))
        self.render_timer.timeout.connect(self.render)
        self._apply_fixed_palette()

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("sectorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        body = QtWidgets.QWidget()
        body.setObjectName("sectorBody")
        body.setMinimumWidth(1040)
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(1)
        title = QtWidgets.QLabel("Sector overview")
        title.setObjectName("sectorTitle")
        set_text_role(title, TextRole.WORKSPACE_TITLE)
        subtitle = QtWidgets.QLabel("Find where participation is broadening")
        subtitle.setObjectName("sectorMuted")
        set_text_role(subtitle, TextRole.UI_BODY)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.timeframe_group = QtWidgets.QButtonGroup(self)
        self.timeframe_group.setExclusive(True)
        self.timeframe_buttons: dict[str, QtWidgets.QPushButton] = {}
        for key, (label, _step, _count) in _SECTOR_OVERVIEW_TIMEFRAMES.items():
            button = QtWidgets.QPushButton(label)
            button.setObjectName("sectorTimeframe")
            set_text_role(button, TextRole.UI_CONTROL_COMPACT)
            button.setCheckable(True)
            button.setFixedSize(50, 29)
            button.setChecked(key == self.timeframe)
            button.clicked.connect(lambda _checked=False, value=key: self._set_timeframe(value))
            self.timeframe_group.addButton(button)
            self.timeframe_buttons[key] = button
            header.addWidget(button)
        benchmark_label = QtWidgets.QLabel("Benchmark")
        benchmark_label.setObjectName("sectorControlLabel")
        set_text_role(benchmark_label, TextRole.UI_LABEL)
        header.addWidget(benchmark_label)
        benchmark = QtWidgets.QLabel("BTC")
        benchmark.setObjectName("sectorBenchmarkChip")
        benchmark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        benchmark.setFixedHeight(29)
        benchmark.setMinimumWidth(54)
        header.addWidget(benchmark)
        liquidity_label = QtWidgets.QLabel("Liquidity")
        liquidity_label.setObjectName("sectorControlLabel")
        set_text_role(liquidity_label, TextRole.UI_LABEL)
        header.addWidget(liquidity_label)
        self.liquidity = _SegmentedSelector("sectorSegmentButton")
        for label, value in (("All", 0), ("$20M+", 20_000_000), ("$50M+", 50_000_000), ("$100M+", 100_000_000)):
            self.liquidity.addItem(label, value)
        self.liquidity.currentIndexChanged.connect(self._filters_changed)
        header.addWidget(self.liquidity)
        layout.addLayout(header)

        stats = QtWidgets.QHBoxLayout()
        stats.setSpacing(8)
        self.stat_btc = _SectorOverviewStatCard("BTC trend", self.theme)
        self.stat_alts = _SectorOverviewStatCard("Alts beating BTC", self.theme, with_bar=True)
        self.stat_ma = _SectorOverviewStatCard("Above 20D average", self.theme, with_bar=True)
        self.stat_spot = _SectorOverviewStatCard("Spot participation", self.theme, with_bar=True)
        for card in (self.stat_btc, self.stat_alts, self.stat_ma, self.stat_spot):
            stats.addWidget(card, 1)
        layout.addLayout(stats)

        middle = QtWidgets.QHBoxLayout()
        middle.setSpacing(10)
        self.participation_panel = QtWidgets.QFrame()
        self.participation_panel.setObjectName("sectorPanel")
        participation_layout = QtWidgets.QVBoxLayout(self.participation_panel)
        participation_layout.setContentsMargins(12, 10, 12, 10)
        participation_layout.setSpacing(7)
        panel_title = QtWidgets.QLabel("Sector participation")
        panel_title.setObjectName("sectorPanelTitle")
        set_text_role(panel_title, TextRole.PANEL_TITLE)
        participation_layout.addWidget(panel_title)
        self.tile_host = QtWidgets.QWidget()
        self.tile_layout = QtWidgets.QHBoxLayout(self.tile_host)
        self.tile_layout.setContentsMargins(0, 0, 0, 0)
        self.tile_layout.setSpacing(6)
        participation_layout.addWidget(self.tile_host, 1)
        self.tiles: dict[str, _SectorOverviewSectorTile] = {}
        for sector in _SECTOR_OVERVIEW_SECTOR_ORDER:
            tile = _SectorOverviewSectorTile(sector, self.theme)
            tile.clicked.connect(self._select_sector)
            self.tiles[sector] = tile
        self._arrange_tiles()
        self.area_caption = QtWidgets.QLabel("Area: selected-window traded volume · Label: performance vs BTC")
        self.area_caption.setObjectName("sectorMuted")
        set_text_role(self.area_caption, TextRole.UI_LABEL)
        participation_layout.addWidget(self.area_caption)
        middle.addWidget(self.participation_panel, 2)

        self.detail = _SectorOverviewSectorDetail(self.theme)
        self.detail.setMinimumWidth(360)
        middle.addWidget(self.detail, 2)
        middle.setStretch(0, 5)
        middle.setStretch(1, 2)
        layout.addLayout(middle, 1)

        leadership_panel = QtWidgets.QFrame()
        leadership_panel.setObjectName("sectorPanel")
        bottom = QtWidgets.QVBoxLayout(leadership_panel)
        bottom.setContentsMargins(12, 10, 12, 10)
        bottom.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("Sector leadership")
        label.setObjectName("sectorPanelTitle")
        set_text_role(label, TextRole.PANEL_TITLE)
        self.coverage = QtWidgets.QLabel("Waiting for histories")
        self.coverage.setObjectName("sectorMuted")
        set_text_role(self.coverage, TextRole.UI_LABEL)
        head.addWidget(label)
        head.addStretch(1)
        head.addWidget(self.coverage)
        bottom.addLayout(head)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setObjectName("sectorTable")
        set_text_role(self.table, TextRole.TABLE_TEXT)
        self.table.setHorizontalHeaderLabels((
            "SECTOR", "15M VS BTC", "1H VS BTC", "4H VS BTC", "1D VS BTC",
            "VOLUME SHARE CHANGE", "OUTPERFORMERS", "TREND",
        ))
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setMinimumHeight(225)
        header_view = self.table.horizontalHeader()
        header_view.setFixedHeight(38)
        header_view.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in range(1, 7):
            header_view.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(7, QtWidgets.QHeaderView.ResizeMode.Stretch)
        bottom.addWidget(self.table)
        layout.addWidget(leadership_panel)

        scroll.setWidget(body)
        outer.addWidget(scroll)

    def bind_sources(self, rest: Any, db: Any, leadership: Any, can_load: Any) -> None:
        previous = getattr(self, "leadership", None)
        if previous is leadership and self.rest is rest and self.db is db:
            self.can_load = can_load
            return
        if previous is not None and hasattr(previous, "data_changed"):
            try:
                previous.data_changed.disconnect(self._leaders_changed)
            except (RuntimeError, TypeError):
                pass
        self.rest, self.db, self.leadership, self.can_load = rest, db, leadership, can_load
        if hasattr(leadership, "data_changed"):
            leadership.data_changed.connect(self._leaders_changed)
        if self.active and not self.refresh_timer.isActive():
            self.refresh_timer.start()
        self.refresh()

    def set_universe(self, symbols: set[str], metadata: list[dict[str, Any]]) -> None:
        self.valid_symbols = set(symbols)
        self.metadata = {
            str(row.get("symbol")): row
            for row in metadata
            if row.get("symbol") in self.valid_symbols
        }
        self.sectors = {
            symbol: sector
            for symbol in self.valid_symbols
            if (sector := _sector_overview_classify(symbol, self.metadata.get(symbol))) is not None
        }
        self.refresh()

    def set_tickers(self, tickers: dict[str, dict[str, Any]]) -> None:


        self.tickers = tickers


        if not self.symbols:
            self.refresh()
        self._refresh_ticker_stat()

    def update_tickers(self, updates: list[dict[str, Any]]) -> None:
        for ticker in updates:
            symbol = str(ticker.get("s") or "")
            if symbol:
                current = self.tickers.get(symbol)
                if current is None:
                    self.tickers[symbol] = dict(ticker)
                elif current is not ticker:
                    current.update(ticker)
        self._refresh_ticker_stat()

    def _refresh_ticker_stat(self) -> None:
        if not self.active or self.closing or self._interaction_paused:
            return
        means = getattr(self, "_ma20", {})
        current = [(safe_float(self.tickers.get(symbol, {}).get("c")), mean) for symbol, mean in means.items()]
        current = [(price, mean) for price, mean in current if price > 0]
        covered = len(current)
        above = sum(price > mean for price, mean in current)
        value = above / covered * 100 if covered else None
        self.stat_ma.set_metric(_sector_overview_share(value, 0), f"/ {covered} covered",
                                self.theme.get("green", "#4DDFA4"), (value or 0) / 100,
                                self.theme.get("grid", "#1D222B"))

    def set_active(self, active: bool) -> None:
        was_active = self.active
        self.active = bool(active) and not self.closing
        if self.active:
            if not was_active:
                self.render()
                self.refresh_timer.start()
                self.refresh()
            return
        self.refresh_timer.stop()
        self.load_timer.stop()
        self.cancel.set()
        self.render_timer.stop()

    def set_interaction_priority(self, active: bool) -> None:
        self._interaction_paused = bool(active)
        if active:
            self.load_timer.stop()
            self.render_timer.stop()
            self.cancel.set()
        elif not self.closing and self.active:
            if self._refresh_pending:
                self.refresh()
            elif self.task is None:
                self.load_timer.start(0)
            if self.active and self._render_dirty:
                self.render_timer.start(0)

    def shutdown(self) -> None:
        self._analysis_job.close()
        self.closing = True
        self.active = False
        self.refresh_timer.stop()
        self.load_timer.stop()
        self.render_timer.stop()
        self.cancel.set()

    def _set_timeframe(self, timeframe: str) -> None:
        if timeframe not in _SECTOR_OVERVIEW_TIMEFRAMES or timeframe == self.timeframe:
            return
        self.timeframe = timeframe
        self.render()

    def _filters_changed(self) -> None:
        self.minimum_volume = safe_float(self.liquidity.currentData())
        self.refresh()

    def _leaders_changed(self) -> None:


        self.refresh()

    def _sync_leader_histories(self) -> None:
        if self.leadership is None:
            return
        snapshot = self.leadership.sector_hourly_snapshot() if hasattr(self.leadership, "sector_hourly_snapshot") else None
        if not snapshot:
            return
        self.end_hour = max(self.end_hour, int(snapshot.get("end") or 0))
        for symbol, rows in snapshot.get("series", {}).items():
            if rows:
                self.hourly[symbol] = rows
        for symbol, rows in snapshot.get("spot", {}).items():
            if rows:
                self.spot_hourly[symbol] = rows

    @profile_callback("workspace.sectors.refresh_ms")
    def refresh(self) -> None:
        if self.closing:
            return
        if not self.active or self._interaction_paused:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        if self.rest is None or self.db is None or not self.can_load():
            return
        self._sync_leader_histories()
        minimum = safe_float(self.liquidity.currentData()) if hasattr(self, "liquidity") else self.minimum_volume
        self.minimum_volume = minimum
        candidates = [
            symbol
            for symbol, sector in self.sectors.items()
            if symbol in self.valid_symbols
            and sector in _SECTOR_OVERVIEW_SECTOR_ORDER
            and safe_float(self.tickers.get(symbol, {}).get("q")) >= minimum
        ]
        candidates.sort(key=lambda symbol: (-safe_float(self.tickers.get(symbol, {}).get("q")), symbol))
        self.symbols = candidates

        offset = safe_float(getattr(self.leadership, "clock_offset", 0.0)) if self.leadership is not None else 0.0
        now_ms = time.time() * 1000 + offset - 2000
        new_end_15m = int(now_ms // _SECTOR_OVERVIEW_QUARTER) * _SECTOR_OVERVIEW_QUARTER
        new_end_hour = int(now_ms // _SECTOR_OVERVIEW_HOUR) * _SECTOR_OVERVIEW_HOUR
        if new_end_15m != self.end_15m:
            self.end_15m = new_end_15m
        if new_end_hour != self.end_hour:
            self.end_hour = new_end_hour

        self.render()
        if self.task is None:
            self.load_timer.start(0)

    def _request_spec(self, symbol: str) -> dict[str, bool]:
        hourly_ready = bool(_sector_overview_series_window(self.hourly.get(symbol, {}), self.end_hour, _SECTOR_OVERVIEW_HOUR, 56))
        spot_hourly_ready = bool(_sector_overview_series_window(self.spot_hourly.get(symbol, {}), self.end_hour, _SECTOR_OVERVIEW_HOUR, 56))
        future15_ready = bool(_sector_overview_series_window(self.futures_15m.get(symbol, {}), self.end_15m, _SECTOR_OVERVIEW_QUARTER, 24))
        spot15_ready = bool(_sector_overview_series_window(self.spot_15m.get(symbol, {}), self.end_15m, _SECTOR_OVERVIEW_QUARTER, 24))
        daily_rows = self.daily.get(symbol, [])
        day_stamp = int(self.end_hour // _SECTOR_OVERVIEW_DAY) * _SECTOR_OVERVIEW_DAY
        daily_ready = _sector_overview_closed_window_ready(daily_rows, day_stamp, _SECTOR_OVERVIEW_DAY, 20)

        def attempted(component: str, stamp: int) -> bool:
            key = (symbol, component)
            retry = self._component_retries.get(key)
            return self._component_attempts.get(key) == stamp or bool(retry and time.monotonic() < retry[1])

        leader_expected = bool(
            self.leadership is not None
            and (symbol == _SECTOR_OVERVIEW_BENCHMARK or symbol in getattr(self.leadership, "symbols", ()))
        )
        leader_failed = bool(
            self.leadership is not None
            and symbol in getattr(self.leadership, "errors", {})
        )
        leader_pending = bool(
            leader_expected
            and not leader_failed
            and (getattr(self.leadership, "_load_pending", False)
                 or getattr(self.leadership, "task", None) is not None
                 or getattr(self.leadership, "_cache_loading", False))
            and getattr(self.leadership, "fetched_for", {}).get(symbol)
            != getattr(self.leadership, "end", 0)
        )
        return {
            "futures_15m": not future15_ready and not attempted("futures_15m", self.end_15m),
            "spot_15m": symbol != _SECTOR_OVERVIEW_BENCHMARK and not spot15_ready and not attempted("spot_15m", self.end_15m),


            "futures_1h": not hourly_ready and not leader_pending and not attempted("futures_1h", self.end_hour),
            "spot_1h": symbol != _SECTOR_OVERVIEW_BENCHMARK and not spot_hourly_ready and not leader_pending and not attempted("spot_1h", self.end_hour),
            "daily": symbol != _SECTOR_OVERVIEW_BENCHMARK and not daily_ready and not attempted("daily", day_stamp),
        }

    @profile_callback("workspace.sectors.next_batch_ms")
    def _next_batch(self) -> None:
        if self.task is not None or not self.active or self.closing or self._interaction_paused or self.rest is None or self.db is None or not self.can_load():
            return
        wanted = [_SECTOR_OVERVIEW_BENCHMARK, *self.symbols]
        pending: list[str] = []
        specs: dict[str, dict[str, bool]] = {}
        for symbol in wanted:
            spec = self._request_spec(symbol)
            if any(spec.values()):
                pending.append(symbol)
                specs[symbol] = spec
                if len(pending) >= _SECTOR_OVERVIEW_ACTIVE_BATCH_SIZE:
                    break
        if not pending:
            deadlines = [deadline for _count, deadline in self._component_retries.values() if deadline > time.monotonic()]
            if deadlines:
                self.load_timer.start(max(50, int((min(deadlines) - time.monotonic()) * 1000)))
            return
        batch_symbols = pending[:_SECTOR_OVERVIEW_ACTIVE_BATCH_SIZE]
        batch_specs = {symbol: specs[symbol] for symbol in batch_symbols}
        rest, db = self.rest, self.db
        end15, endhour = self.end_15m, self.end_hour
        self.cancel = threading.Event()
        cancel = self.cancel
        self.task = ApiTask(lambda: _sector_overview_load_sector_batch(rest, db, batch_specs, end15, endhour, cancel))
        self.task.signals.finished.connect(self._batch_finished)
        self.task.signals.failed.connect(self._batch_failed)
        QtCore.QThreadPool.globalInstance().start(self.task)

    @profile_callback("workspace.sectors.batch_finished_ms")
    def _batch_finished(self, result: dict[str, Any]) -> None:
        self.task = None
        if self.closing:
            return
        result_hour = int(result.get("end_hour", self.end_hour))
        result_quarter = int(result.get("end_15m", self.end_15m))
        day_stamp = int(result_hour // _SECTOR_OVERVIEW_DAY) * _SECTOR_OVERVIEW_DAY
        attempted = {} if result.get("cancelled") else result.get("requests", {})
        for symbol, spec in attempted.items():
            for component, requested in spec.items():
                if not requested:
                    continue
                stamp = (
                    result_quarter
                    if component in {"futures_15m", "spot_15m"}
                    else day_stamp
                    if component == "daily"
                    else result_hour
                )
                key = (symbol, component)
                entry = result.get("data", {}).get(symbol, {})
                error = entry.get("errors", {}).get(component)
                rows = entry.get(component) or ([] if component == 'daily' else {})
                unavailable = component in entry.get("_spot_unavailable", ())
                step = _SECTOR_OVERVIEW_QUARTER if component.endswith("15m") else _SECTOR_OVERVIEW_DAY if component == "daily" else _SECTOR_OVERVIEW_HOUR
                minimum = 24 if component.endswith("15m") else 20 if component == "daily" else 56
                ready = _sector_overview_closed_window_ready(rows, stamp, step, minimum) if component == "daily" else bool(_sector_overview_series_window(rows, stamp, step, minimum))
                if unavailable or (not error and ready):
                    self._component_attempts[key] = stamp
                    self._component_retries.pop(key, None)
                else:
                    count = self._component_retries.get(key, (0, 0.0))[0] + 1
                    self._component_retries[key] = (count, time.monotonic() + min(60.0, 1.5 * 2 ** min(count - 1, 6)))
        for symbol, entry in result.get("data", {}).items():
            rows = entry.get("futures_15m") or []
            if rows:
                self.futures_15m[symbol] = rows
            rows = entry.get("futures_1h") or []
            if rows:
                self.hourly[symbol] = rows
            rows = entry.get("spot_15m") or []
            if rows:
                self.spot_15m[symbol] = rows
            rows = entry.get("spot_1h") or []
            if rows:
                self.spot_hourly[symbol] = rows
            rows = entry.get("daily") or []
            if rows:
                self.daily[symbol] = rows
        self.render()
        if self.active and not self._interaction_paused:
            self.load_timer.start(_SECTOR_OVERVIEW_ACTIVE_BATCH_DELAY_MS)


    def _batch_failed(self, _message: str) -> None:
        self.task = None
        if self.active and not self.closing and not self._interaction_paused:
            self.load_timer.start(1500)

    def _analysis_view_key(self):
        return (self.timeframe, tuple(self.symbols), self.end_hour, self.end_15m)

    @profile_callback("workspace.sectors.render_ms")
    def render(self) -> None:
        self._render_dirty = True
        if not self.active or self.closing or self._interaction_paused:
            return
        if not self.symbols:
            self._analysis_job.invalidate()
            self.coverage.setText("Waiting for classified market histories")
            return
        self._analysis_serial += 1
        state = {name: dict(getattr(self, name)) for name in
                 ("hourly", "spot_hourly", "futures_15m", "spot_15m", "daily", "sectors")}
        state.update(symbols=tuple(self.symbols), timeframe=self.timeframe,
                     end_hour=self.end_hour, end_15m=self.end_15m,
                     tickers={symbol: {"c": row.get("c")} for symbol, row in self.tickers.items()})
        self._analysis_job.submit(
            (self._analysis_view_key(), self._analysis_serial),
            _workspace_analysis, _prepare_sectors, state, self._history_transport,
        )

    @QtCore.Slot(object, object)
    def _analysis_ready(self, key, prepared) -> None:
        if (self.closing or not self.active or self._interaction_paused
                or key[0] != self._analysis_view_key()):
            return
        self._prepared_analysis = prepared
        self._ma20 = prepared['ma20']
        self._render_dirty = False
        timeframe_label = _SECTOR_OVERVIEW_TIMEFRAMES[self.timeframe][0]
        metrics, performances = prepared["metrics"], prepared["performances"]
        available = [sector for sector in _SECTOR_OVERVIEW_SECTOR_ORDER if metrics[sector].get("members")]
        if self.selected_sector not in available and available:
            self.selected_sector = available[0]
            self._arrange_tiles()

        for sector, tile in self.tiles.items():
            tile.theme = self.theme
            tile.set_selected(sector == self.selected_sector)
            tile.update_data(metrics[sector], timeframe_label)
            tile.setVisible(bool(metrics[sector].get("members")))

        selected = metrics.get(self.selected_sector, {})
        self.detail.theme = self.theme
        self.detail.update_data(self.selected_sector, selected, timeframe_label)

        btc_trend = prepared["btc_trend"]
        btc_color = self.theme.get("green", "#4DDFA4") if not _sector_overview_finite(btc_trend) or btc_trend >= 0 else self.theme.get("red", "#FF7A85")
        self.stat_btc.set_metric(_sector_overview_pct(btc_trend, 1), f"/ {timeframe_label}", btc_color)

        alt_pct = prepared["alt_pct"]
        self.stat_alts.set_metric(
            _sector_overview_share(alt_pct, 0),
            f"/ {timeframe_label}",
            self.theme.get("green", "#4DDFA4"),
            (alt_pct or 0.0) / 100.0,
            self.theme.get("grid", "#1D222B"),
        )

        above, daily_covered = prepared["above"]
        above_pct = above / daily_covered * 100.0 if daily_covered else None
        self.stat_ma.set_metric(
            _sector_overview_share(above_pct, 0),
            f"/ {daily_covered} covered",
            self.theme.get("green", "#4DDFA4"),
            (above_pct or 0.0) / 100.0,
            self.theme.get("grid", "#1D222B"),
        )

        spot = selected.get("spot_participation")
        self.stat_spot.set_metric(
            _sector_overview_share(spot, 0),
            f"/ {self.selected_sector}",
            self.theme.get("amber", "#E3B45E"),
            (spot or 0.0) / 100.0,
            self.theme.get("grid", "#1D222B"),
        )

        self._render_table(metrics, performances)
        loaded15, loaded1h = prepared["loaded15"], prepared["loaded1h"]
        self.coverage.setText(
            f"{len(self.symbols)} classified pairs · 15M {loaded15}/{len(self.symbols)} · 1H {loaded1h}/{len(self.symbols)} · 20D {daily_covered}/{len(self.symbols)}"
        )

    @QtCore.Slot(object, str)
    def _analysis_failed(self, _key, message) -> None:
        self._render_dirty = True
        log.error("Sectors analysis failed: %s", message)

    def _render_table(
        self,
        metrics: dict[str, dict[str, Any]],
        performances: dict[str, dict[str, float | None]],
    ) -> None:
        def performance_key(sector: str) -> tuple[float, str]:
            value = metrics[sector].get("performance")
            return (-(float(value) if _sector_overview_finite(value) else -1e9), sector)

        ordered = sorted(
            (sector for sector in _SECTOR_OVERVIEW_SECTOR_ORDER if metrics[sector].get("members")),
            key=performance_key,
        )
        self.table.setRowCount(len(ordered))
        value_font = typography_font(TextRole.TABLE_VALUE)

        def cell(row, column, text, color=None):
            item = self.table.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                if column:
                    item.setFont(value_font)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
            if item.text() != text:
                item.setText(text)
            ink = QtGui.QColor(color or self.theme.get("text", "#DFE5EE"))
            if item.foreground().color() != ink:
                item.setForeground(ink)

        for row, sector in enumerate(ordered):
            sector_metrics = metrics[sector]
            cell(row, 0, sector, _sector_overview_sector_color(sector, self.theme, for_text=True))
            for column, timeframe in enumerate(("15m", "1h", "4h", "1d"), 1):
                value = performances[timeframe].get(sector)
                color = (self.theme.get("green", "#4DDFA4") if value >= 0 else self.theme.get("red", "#FF7A85")) if _sector_overview_finite(value) else None
                cell(row, column, _sector_overview_pct(value, 1), color)
            current = sector_metrics.get("volume_share")
            previous = sector_metrics.get("previous_volume_share")
            share_text = (
                f"{_sector_overview_share(previous)} → {_sector_overview_share(current)}"
                if _sector_overview_finite(current) and _sector_overview_finite(previous)
                else "—"
            )
            cell(row, 5, share_text)
            cell(row, 6, f"{sector_metrics.get('outperformers', 0)} / {sector_metrics.get('covered', 0)}")
            spark = self.table.cellWidget(row, 7)
            if spark is None:
                spark = _SectorOverviewSparkline(_SECTOR_OVERVIEW_SECTOR_COLORS[sector])
                spark.setMinimumHeight(28)
                self.table.setCellWidget(row, 7, spark)
                self.table.setRowHeight(row, 40)
            spark.set_values(sector_metrics.get("trend", []), _SECTOR_OVERVIEW_SECTOR_COLORS[sector])

    def _select_sector(self, sector: str) -> None:
        if sector == self.selected_sector:
            return
        self.selected_sector = sector
        self._arrange_tiles()
        self.render()

    def _arrange_tiles(self) -> None:


        for tile in self.tiles.values():
            tile.setParent(self.tile_host)
        while self.tile_layout.count():
            item = self.tile_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget not in self.tiles.values():
                widget.deleteLater()

        selected = self.tiles[self.selected_sector]
        selected.setMinimumWidth(255)
        selected.setMinimumHeight(258)
        self.tile_layout.addWidget(selected, 1)

        right = QtWidgets.QWidget(self.tile_host)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        remaining = [sector for sector in _SECTOR_OVERVIEW_SECTOR_ORDER if sector != self.selected_sector]
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(6)
        for index, sector in enumerate(remaining):
            tile = self.tiles[sector]
            tile.setMinimumWidth(150)
            tile.setMinimumHeight(124)
            (top if index < 2 else bottom).addWidget(tile, 1)
        right_layout.addLayout(top, 1)
        right_layout.addLayout(bottom, 1)
        self.tile_layout.addWidget(right, 2)

    def _apply_fixed_palette(self) -> None:
        palette = dict(SECTORS_PALETTE)
        self.theme = palette
        for tile in getattr(self, "tiles", {}).values():
            tile.apply_theme(palette)
        for card_name in ("stat_btc", "stat_alts", "stat_ma", "stat_spot"):
            card = getattr(self, card_name, None)
            if card is not None:
                card.apply_theme(palette)
        if hasattr(self, "detail"):
            self.detail.theme = palette
        green = palette.get("green", "#4DDFA4")
        red = palette.get("red", "#FF7A85")
        self.setStyleSheet(f"""
            QWidget#sectorOverview, QScrollArea#sectorScroll, QWidget#sectorBody {{ background: {palette['bg']}; color: {palette['text']}; border: 0; }}
            QFrame#sectorPanel, QFrame#sectorStatCard, QFrame#sectorTile {{ background: {palette['panel']}; border: 1px solid {palette['control_border']}; border-radius: 6px; }}
            QFrame#sectorTile[selected="true"] {{ border: 2px solid {palette.get("cyan", "#8AA9FF")}; }}
            QLabel {{ background: transparent; border: 0; color: {palette['text']}; }}
            QLabel#sectorTitle {{ }}
            QLabel#sectorPanelTitle {{ }}
            QLabel#sectorMuted, QLabel#sectorStatTitle, QLabel#sectorControlLabel {{ color: {palette['muted']}; }}
            QLabel#sectorStatValue {{ color: {palette['text']}; }}
            QLabel#sectorTileName {{ color: {palette['text']}; }}
            QLabel#sectorTilePerformance {{ }}
            QLabel#sectorTileShare {{ color: {palette['text']}; }}
            QFrame#sectorMetricCard, QFrame#sectorPeerCard {{
                background: {palette['panel2']}; border: 1px solid {palette['border']}; border-radius: 4px;
            }}
            QLabel#sectorMetricLabel, QLabel#sectorPeerRank {{ color: {palette['muted']}; }}
            QLabel#sectorDetailMetric, QLabel#sectorPeerSymbol {{ color: {palette['text']}; }}
            QLabel#sectorPeerValue {{ color: {palette['text']}; }}
            QLabel#sectorPeerValue[direction="positive"] {{ color: {green}; }}
            QLabel#sectorPeerValue[direction="negative"] {{ color: {red}; }}
            QProgressBar#sectorPeerBar {{ background: {palette['border']}; border: 0; border-radius: 2px; }}
            QProgressBar#sectorPeerBar::chunk {{ background: {green}; border: 0; border-radius: 2px; }}
            QProgressBar#sectorPeerBar[direction="negative"]::chunk {{ background: {red}; }}
            QPushButton#sectorTimeframe, QPushButton#sectorSegmentButton, QLabel#sectorBenchmarkChip {{
                background: {palette['control']}; color: {palette['muted']}; border: 1px solid {palette['control_border']};
                border-radius: 5px; padding: 3px 7px;
            }}
            QPushButton#sectorTimeframe:hover, QPushButton#sectorSegmentButton:hover {{
                background: {palette['control_hover']}; color: {palette['text']};
            }}
            QPushButton#sectorTimeframe:checked, QPushButton#sectorSegmentButton:checked {{
                background: {palette['active']}; border: 1px solid {palette.get('active_line', palette.get('cyan', '#8AA9FF'))}; color: {palette['text']};
            }}
            QLabel#sectorBenchmarkChip {{ color: {palette['text']}; }}
            QFrame#sectorDivider {{ color: {palette['border']}; background: {palette['border']}; max-height: 1px; }}
            QTableWidget#sectorTable {{ background: {palette['panel']}; alternate-background-color: {palette['panel2']}; color: {palette['text']}; border: 0; }}
            QTableWidget#sectorTable::item {{ border-bottom: 1px solid {palette['border']}; padding: 4px 8px; }}
            QHeaderView::section {{ background: {palette['panel2']}; color: {palette['muted']}; border: 0; border-bottom: 1px solid {palette['border']}; padding: 7px 8px; }}
        """)
        if hasattr(self, "render_timer"):
            self.render()

    def restore_ui_state(self, settings: QtCore.QSettings) -> None:
        timeframe = settings.value("markets/sectors/timeframe_v1", "4h", str)
        if timeframe in _SECTOR_OVERVIEW_TIMEFRAMES:
            self.timeframe = timeframe
            self.timeframe_buttons[timeframe].setChecked(True)
        minimum = settings.value("markets/sectors/minimum_volume_v1", 0, int)
        index = self.liquidity.findData(minimum)
        if index >= 0:
            blocker = QtCore.QSignalBlocker(self.liquidity)
            self.liquidity.setCurrentIndex(index)
            del blocker
            self.minimum_volume = float(minimum)
        selected = settings.value("markets/sectors/selected_v1", "AI", str)
        if selected in _SECTOR_OVERVIEW_SECTOR_ORDER:
            self.selected_sector = selected
            self._arrange_tiles()

    def save_ui_state(self, settings: QtCore.QSettings) -> None:
        settings.setValue("markets/sectors/timeframe_v1", self.timeframe)
        settings.setValue("markets/sectors/minimum_volume_v1", int(self.liquidity.currentData() or 0))
        settings.setValue("markets/sectors/selected_v1", self.selected_sector)


import math


def encode_histories(histories, end):
    return {
        symbol: [
            [row.time, row.open, row.high, row.low, row.close, row.volume, row.quote_volume]
            for stamp, row in sorted(rows.items())
            if end - HISTORY_HOURS * HOUR < stamp <= end
        ]
        for symbol, rows in histories.items()
    }


def _decode_histories(histories, end):
    if not isinstance(histories, dict):
        raise ValueError("Invalid Leaders histories")
    result = {}
    for symbol, rows in histories.items():
        if not isinstance(symbol, str) or not isinstance(rows, list) or len(rows) > HISTORY_HOURS:
            raise ValueError("Invalid Leaders series")
        restored = {}
        for values in rows:
            if not isinstance(values, list) or len(values) != 7:
                raise ValueError("Invalid cached candle")
            values = [float(value) for value in values]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Non-finite cached candle")
            candle = Candle(*values)
            stamp = int(round(candle.time * 1000)) + HOUR
            if (stamp % HOUR or not end - HISTORY_HOURS * HOUR < stamp <= end
                    or min(values[1:5]) <= 0 or min(values[5:]) < 0
                    or candle.low > min(candle.open, candle.close)
                    or candle.high < max(candle.open, candle.close)):
                raise ValueError("Invalid completed-hour candle")
            restored[stamp] = candle
        result[symbol] = restored
    return result


def load_leadership_state(db, name):
    """Run on a worker; return a fully decoded model for one GUI adoption."""
    payload = db.load_workspace_snapshot(name)
    if not payload or payload.get("version") != 1:
        return None
    end = int(payload["end"])
    offset = float(payload.get("clock_offset", 0))
    if end <= 0 or end % HOUR or not math.isfinite(offset):
        raise ValueError("Invalid Leaders clock")
    symbols = payload["symbols"]
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError("Invalid Leaders universe")
    series = _decode_histories(payload["series"], end)
    spot = _decode_histories(payload.get("spot", {}), end)
    fetched_for = {
        symbol: int(stamp) for symbol, stamp in payload.get("fetched_for", {}).items()
        if int(stamp) == end and end in series.get(symbol, {})
    }
    categories = payload.get("categories", {})
    if not isinstance(categories, dict):
        raise ValueError("Invalid Leaders categories")
    return dict(end=end, clock_offset=offset, symbols=list(dict.fromkeys(symbols)),
                series=series, spot_series=spot, fetched_for=fetched_for,
                categories={str(s): str(v) for s, v in categories.items()},
                eligible_count=max(len(symbols), int(payload.get("eligible_count", 0))))


# Rotation consumes the existing Leaders dataset; it owns no backend loader.
ROTATION_PALETTE = dict(LEADERS_PALETTE, bg="#000000", panel="#000000",
                        panel2="#000000", header="#000000", control="#000000",
                        control_hover="#000000", active="#000000")
QUADRANT_COLORS = {"Improving": "#35C4B2", "Leading": "#00C56A",
                   "Lagging": "#F0143E", "Weakening": "#C89A47", "Neutral": "#8E8E96"}
# Stable coin colors make crossing a quadrant easy to follow. Position determines
# the state; color identifies the instrument, as in the scanner reference.
_BUBBLE_COLORS = ("#42C9E8", "#53DD7B", "#AA82DD", "#7DB6F3", "#F57579", "#E7B955")
_REFERENCE_COIN_COLORS = dict(TAO="#42C9E8", RENDER="#53DD7B", RNDR="#53DD7B",
                             FET="#AA82DD", SOL="#7DB6F3", ARB="#F57579", WIF="#E7B955")

def _bubble_color(point):
    base = point["symbol"].removesuffix("USDT")
    return point.get("color") or _REFERENCE_COIN_COLORS.get(base) or _BUBBLE_COLORS[zlib.crc32(base.encode("utf-8")) % len(_BUBBLE_COLORS)]

_ROTATION_SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 11
_ROTATION_SYMBOL_ROLE = int(Qt.ItemDataRole.UserRole)


def _rotation_finite(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


def _rotation_quadrant(x, y):
    if x == 0 or y == 0:
        return "Neutral"
    return ("Leading" if x > 0 else "Improving") if y > 0 else ("Weakening" if x > 0 else "Lagging")


def _prepare_rotation(state):
    """Worker-only calculation; no widgets or QPixmaps cross the thread boundary."""
    end, hours = state["cursor"], state["hours"]
    histories, btc = state["series"], state["series"].get(BENCHMARK, {})
    metrics, points, charts = {}, [], {}
    for symbol in state["symbols"]:
        series = histories.get(symbol, {})
        item = _metrics(series, btc, end)
        x = _relative(series, btc, end, hours)
        current, prior = _relative(series, btc, end), _relative(series, btc, end - HOUR)
        y = current - prior if current is not None and prior is not None else None
        share, delta = _spot_share(state["spot_series"].get(symbol, {}), series, end)
        item.update(x=x, y=y, spot_share=share, spot_delta=delta,
                    spot_confirmed=delta is not None and delta > 0)
        rows = _window(series, end, hours)
        item["turnover"] = sum(row.quote_volume for row in rows) if rows else None
        item["quadrant"] = _rotation_quadrant(x, y) if _rotation_finite(x) and _rotation_finite(y) else "Waiting"
        metrics[symbol] = item
        if _rotation_finite(x) and _rotation_finite(y):
            trail = []
            for at in range(end - 3 * HOUR, end + 1, HOUR):
                tx = _relative(series, btc, at, hours)
                r1, r0 = _relative(series, btc, at), _relative(series, btc, at - HOUR)
                ty = r1 - r0 if r1 is not None and r0 is not None else None
                trail.append((tx, ty) if _rotation_finite(tx) and _rotation_finite(ty) else None)
            points.append(dict(symbol=symbol, x=x, y=y, trail=trail,
                               volume=item["turnover"] or 0, quadrant=item["quadrant"]))
        # Hourly samples with explicit gaps, suitable for an inspector without more requests.
        at_values = range(end - max(hours, 4) * HOUR, end + 1, HOUR)
        base = next((at for at in at_values if at in series and at in btc), None)
        ratio = series[base].close / btc[base].close if base is not None and btc[base].close > 0 else None
        relative = []
        for at in at_values:
            coin, benchmark = series.get(at), btc.get(at)
            relative.append((coin.close / benchmark.close / ratio - 1) * 100
                            if ratio and coin and benchmark and benchmark.close > 0 else None)
        volumes = [series[at].quote_volume if at in series else None
                   for at in range(end - 23 * HOUR, end + 1, HOUR)]
        charts[symbol] = dict(relative=relative, volume=volumes)
    btc_rows = _window(btc, end, hours)
    btc_return = (btc_rows[-1].close / btc_rows[0].open - 1) * 100 if btc_rows else None
    covered = sum(_rotation_finite(m.get("x")) for m in metrics.values())
    beating = sum(m.get("x", 0) > 0 for m in metrics.values() if _rotation_finite(m.get("x")))
    return dict(end=end, hours=hours, metrics=metrics, points=points, charts=charts,
                btc_return=btc_return, covered=covered, beating=beating)


def _rotation_detail_numbers(result, end, live):
    """Only expose observed, sufficiently fresh details; missing values stay absent."""
    out = dict(oi=None, funding=None, spread=None, depth=None)
    oi = sorted((r for r in result.get("oi", []) if 0 < int(r.get("timestamp", 0)) <= end),
                key=lambda r: int(r["timestamp"]))
    if len(oi) >= 2:
        a, b = oi[-2:]
        before = safe_float(a.get("sumOpenInterest"))
        after = safe_float(b.get("sumOpenInterest"), float("nan"))
        at, prior = int(b["timestamp"]), int(a["timestamp"])
        if before > 0 and _rotation_finite(after) and after >= 0 and abs(at - prior - HOUR) <= 5000 and end - at <= HOUR:
            out["oi"] = (after / before - 1) * 100
    funding = sorted((r for r in result.get("funding", []) if 0 < int(r.get("fundingTime", 0)) <= end),
                     key=lambda r: int(r["fundingTime"]))
    if len(funding) >= 2:
        at = int(funding[-1]["fundingTime"])
        interval = (at - int(funding[-2]["fundingTime"])) / HOUR
        rate = safe_float(funding[-1].get("fundingRate"), float("nan"))
        if .5 <= interval <= 24 and end - at <= interval * HOUR and _rotation_finite(rate):
            out["funding"] = rate * 100 * 8 / interval
    age = time.monotonic() - result.get("book_at", result.get("at", 0))
    book = result.get("book", {}) if live and 0 <= age < 90 else {}
    bids = [(safe_float(r[0]), safe_float(r[1])) for r in book.get("bids", []) if len(r) >= 2]
    asks = [(safe_float(r[0]), safe_float(r[1])) for r in book.get("asks", []) if len(r) >= 2]
    bids = [(p, q) for p, q in bids if p > 0 and q > 0]
    asks = [(p, q) for p, q in asks if p > 0 and q > 0]
    if bids and asks:
        bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
        if ask > bid:
            mid = (ask + bid) / 2
            out["spread"] = (ask - bid) / mid * 100
            out["depth"] = sum(p * q for p, q in bids if p >= mid * .995) + sum(p * q for p, q in asks if p <= mid * 1.005)
    return out


class RotationBubbleChart(QtWidgets.QWidget):
    """Symmetric linear axes keep zero at the center and all four planes intact."""
    chosen = Signal(str)
    opened = Signal(str)
    MAX_BUBBLES = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.selected = ""
        self.hours = 4
        self._hits = []
        self._geometry = []
        self._labels = []
        self._plot = QtCore.QRectF()
        self.x_extent, self.y_extent = 6.0, 3.0
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Rotation map: Improving top left, Leading top right, Lagging bottom left, Weakening bottom right")

    def sizeHint(self):
        return QtCore.QSize(850, 430)

    @classmethod
    def _major_points(cls, points):
        """Shortlist the strongest moves without changing the candidate dataset."""
        candidates = [
            point for point in points
            if _rotation_finite(point.get("x")) and _rotation_finite(point.get("y"))
            and _rotation_quadrant(point["x"], point["y"]) != "Neutral"
            # Match Leaders' meaningful RS / momentum thresholds; omit flat noise.
            and (abs(point["x"]) >= .5 or abs(point["y"]) >= .2)
        ]
        if not candidates:
            return []
        # Normalize axes independently so RS and acceleration both affect rank.
        x_scale = max(.5, statistics.median(abs(p["x"]) for p in candidates))
        y_scale = max(.2, statistics.median(abs(p["y"]) for p in candidates))

        def rank(point):
            volume = point.get("volume", 0)
            return (-math.hypot(point["x"] / x_scale, point["y"] / y_scale),
                    -volume if _rotation_finite(volume) and volume > 0 else 0,
                    point["symbol"])

        ranked = sorted(candidates, key=rank)
        leaders = {}
        for point in ranked:
            leaders.setdefault(_rotation_quadrant(point["x"], point["y"]), point)
        # One major mover per populated quadrant, then the strongest remaining
        # moves. A table selection never adds an ordinary coin to the plot.
        shortlist = list(leaders.values())
        symbols = {p["symbol"] for p in shortlist}
        for point in ranked:
            if len(shortlist) >= cls.MAX_BUBBLES:
                break
            if point["symbol"] not in symbols:
                shortlist.append(point)
                symbols.add(point["symbol"])
        return sorted(shortlist, key=rank)

    def set_points(self, points, selected="", hours=4):
        self.points = self._major_points(points)
        self.selected, self.hours = selected, hours
        coordinates = [(p["x"], p["y"]) for p in self.points]
        # Historical outliers cannot stretch the axes and hide current leaders.
        # Latest positions keep their exact values; trail spacing is normalized.
        def extent(values, minimum, step):
            maximum = max((abs(v) for v in values), default=0)
            return max(minimum, math.ceil(maximum * 1.22 / step) * step)
        self.x_extent = extent((v[0] for v in coordinates), 6, 3)
        self.y_extent = extent((v[1] for v in coordinates), 3, 1.5)
        self._hits = []
        self._geometry = []
        self._labels = []
        self.update()

    def plot_rect(self):
        return QtCore.QRectF(20, 20, max(1, self.width() - 40), max(1, self.height() - 40))

    def map_point(self, x, y):
        r = self.plot_rect()
        return QtCore.QPointF(r.center().x() + x / self.x_extent * r.width() / 2,
                             r.center().y() - y / self.y_extent * r.height() / 2)

    def _font(self, role, pixels=None):
        family = getattr(self, "_graph_font_family", None)
        if family is None:
            available = set(QtGui.QFontDatabase.families())
            preferred = (
                "Inter", "Geist", "Manrope", "DM Sans",
                "IBM Plex Sans", "Segoe UI", "Arial",
            )
            family = next((name for name in preferred if name in available), "")
            self._graph_font_family = family
        font = QtGui.QFont(family)
        font.setStyleHint(QtGui.QFont.StyleHint.SansSerif)
        font.setPixelSize(pixels or 12)
        font.setKerning(True)
        font.setHintingPreference(
            QtGui.QFont.HintingPreference.PreferVerticalHinting
        )
        if (pixels or 12) >= 14 or role == TextRole.INSTRUMENT_SYMBOL:
            font.setWeight(QtGui.QFont.Weight.DemiBold)
        return font

    def _trail_geometry(self, point, radius):
        """Readable chronological trail; hover retains observed percentages."""
        history = list(point.get("trail", [])[:-1])[-3:]
        samples = [None] * (3 - len(history)) + history
        samples.append((point["x"], point["y"]))
        samples = [
            value if isinstance(value, (tuple, list))
            and len(value) == 2
            and all(_rotation_finite(n) for n in value)
            else None
            for value in samples
        ]
        raw = [
            self.map_point(*value) if value is not None else None
            for value in samples
        ]

        # Each circle has exactly half the diameter of its successor.
        radii = [radius / 8, radius / 4, radius / 2, radius]
        visible_gap = 8.0
        distances = [0.0] * 4
        for i in range(2, -1, -1):
            distances[i] = (
                distances[i + 1] + radii[i] + radii[i + 1] + visible_gap
            )

        # Preserve the latest movement direction while regularizing spacing.
        heading = math.atan2(.65, -1)
        for earlier in reversed(raw[:-1]):
            if earlier is None:
                continue
            delta = earlier - raw[-1]
            if math.hypot(delta.x(), delta.y()) > .001:
                heading = math.atan2(delta.y(), delta.x())
                break

        plot = self.plot_rect()
        cx, cy = plot.center().x(), plot.center().y()
        right, top = point["x"] > 0, point["y"] > 0
        area = QtCore.QRectF(
            cx if right else plot.left(),
            plot.top() if top else cy,
            plot.width() / 2,
            plot.height() / 2,
        ).adjusted(18, 34 if top else 14, -18, -14 if top else -34)

        rotations = [0.0]
        for k in (1, 2, 3, 4, 5, 6, 9, 12):
            rotations.extend((k * math.pi / 12, -k * math.pi / 12))

        best = None
        valid = [i for i, value in enumerate(raw) if value is not None]
        for turn in rotations:
            angle = heading + turn
            unit = QtCore.QPointF(math.cos(angle), math.sin(angle))
            offsets = [unit * distance for distance in distances]
            low_x = area.left() - min(
                offsets[i].x() - radii[i] for i in valid
            )
            high_x = area.right() - max(
                offsets[i].x() + radii[i] for i in valid
            )
            low_y = area.top() - min(
                offsets[i].y() - radii[i] for i in valid
            )
            high_y = area.bottom() - max(
                offsets[i].y() + radii[i] for i in valid
            )
            if low_x > high_x or low_y > high_y:
                continue
            head = QtCore.QPointF(
                min(high_x, max(low_x, raw[-1].x())),
                min(high_y, max(low_y, raw[-1].y())),
            )
            delta = head - raw[-1]
            cost = delta.x() ** 2 + delta.y() ** 2 + (turn * 16) ** 2
            positions = [
                head + offsets[i] if raw[i] is not None else None
                for i in range(4)
            ]
            if best is None or cost < best[0]:
                best = cost, positions

        if best is None:
            raise RuntimeError("Rotation plot is too small for its bubbles")

        return dict(
            point=point, samples=samples, positions=best[1],
            radii=radii, normalized=True, visible_gap=visible_gap,
        )

    @staticmethod
    def _label_clear(box, occupied, circles, segments):
        if any(box.intersects(other) for other in occupied):
            return False
        for at, radius in circles:
            near_x = max(box.left(), min(at.x(), box.right()))
            near_y = max(box.top(), min(at.y(), box.bottom()))
            if math.hypot(at.x() - near_x, at.y() - near_y) <= radius + 3:
                return False
        # Keep text clear of connector lines as well as the circles themselves.
        for start, stop in segments:
            if box.contains(start) or box.contains(stop):
                return False
            line = QtCore.QLineF(start, stop)
            edges = ((box.topLeft(), box.topRight()), (box.topRight(), box.bottomRight()),
                     (box.bottomRight(), box.bottomLeft()), (box.bottomLeft(), box.topLeft()))
            if any(line.intersects(QtCore.QLineF(a, b))[0] == QtCore.QLineF.IntersectionType.BoundedIntersection
                   for a, b in edges):
                return False
        return True

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        apply_text_render_hints(p)
        p.fillRect(self.rect(), QtGui.QColor("#000000"))
        r = self.plot_rect()
        self._plot = r
        center_color = QtGui.QColor("#FFFFFF")
        center_color.setAlpha(24)
        pen = QtGui.QPen(center_color, .65)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(r.center().x(), r.top()), QtCore.QPointF(r.center().x(), r.bottom()))
        p.drawLine(QtCore.QPointF(r.left(), r.center().y()), QtCore.QPointF(r.right(), r.center().y()))
        quadrant_font = self._font(TextRole.UI_LABEL, 15)
        quadrant_font.setWeight(QtGui.QFont.Weight.DemiBold)
        p.setFont(quadrant_font)
        headings = []
        for label, where in (("Improving", "tl"), ("Leading", "tr"), ("Lagging", "bl"), ("Weakening", "br")):
            p.setPen(QtGui.QColor({"Improving": "#42C9E8", "Leading": QUADRANT_COLORS["Leading"], "Lagging": QUADRANT_COLORS["Lagging"], "Weakening": "#E7B955"}[label]))
            box = QtCore.QRectF(r.left() + 14, r.top() + 10 if where[0] == "t" else r.bottom() - 30, r.width() - 28, 20)
            alignment = Qt.AlignmentFlag.AlignLeft if where[1] == "l" else Qt.AlignmentFlag.AlignRight
            p.drawText(box, alignment | Qt.AlignmentFlag.AlignVCenter, label.upper())
            width = p.fontMetrics().horizontalAdvance(label.upper())
            headings.append(QtCore.QRectF(box.left() if where[1] == "l" else box.right() - width,
                                         box.top(), width, box.height()))
        self._hits = []
        self._geometry = []
        self._labels = []
        if not self.points:
            return
        max_volume = max((v["volume"] for v in self.points), default=1) or 1
        # Large bubbles first; the selected bubble and its label always draw last.
        ordered = sorted(self.points, key=lambda v: (v["symbol"] == self.selected, -v["volume"], v["symbol"]))
        self._geometry = [self._trail_geometry(point, max(10, 12 * math.sqrt(max(0, point["volume"]) / max_volume)))
                          for point in ordered]
        circles, segments = [], []
        p.save()
        p.setClipRect(r.adjusted(1, 1, -1, -1))
        # Thin connectors go behind every bead, and never bridge missing hours.
        for group in self._geometry:
            color = QtGui.QColor(_bubble_color(group["point"]))
            color.setAlpha(145)
            pen = QtGui.QPen(color, .7)
            pen.setCosmetic(True)
            p.setPen(pen)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            for i, (start, stop) in enumerate(zip(group["positions"], group["positions"][1:])):
                if start is None or stop is None:
                    continue
                delta = stop - start
                length = math.hypot(delta.x(), delta.y())
                if length <= 0:
                    continue
                unit = delta / length
                # Draw only the visible section outside both circle outlines.
                edge_start = start + unit * group["radii"][i]
                edge_stop = stop - unit * group["radii"][i + 1]
                p.drawLine(edge_start, edge_stop)
                segments.append((edge_start, edge_stop))
        for group in self._geometry:
            point = group["point"]
            color = QtGui.QColor(_bubble_color(point))
            for i, (at, radius) in enumerate(zip(group["positions"], group["radii"])):
                if at is None:
                    continue
                latest = i == 3
                selected = latest and point["symbol"] == self.selected
                stroke = 1.7 if selected else min(1.2, max(.45, radius * .2))
                rim = QtGui.QColor(color)
                rim.setAlpha((170, 195, 225, 255)[i])
                fill = QtGui.QColor(color)
                fill.setAlpha((18, 24, 30, 38)[i])
                p.setPen(QtGui.QPen(rim, stroke))
                p.setBrush(fill)
                # Stroke remains inside the nominal circle boundary.
                drawn_radius = max(.1, radius - stroke / 2)
                p.drawEllipse(at, drawn_radius, drawn_radius)
                circles.append((at, radius + (2 if latest else .5)))
                hit = dict(point, _sample_index=i, _sample_value=group["samples"][i],
                           _trail_normalized=group["normalized"])
                self._hits.append((hit, at, max(4, radius + 3)))
        p.restore()
        p.setFont(self._font(TextRole.INSTRUMENT_SYMBOL, 13 if r.width() >= 600 else 11))
        occupied = list(headings)
        # Text has no background and must clear every group's circles and lines.
        for group in sorted(self._geometry, key=lambda g: (g["point"]["symbol"] != self.selected,
                                                           -g["point"]["volume"], g["point"]["symbol"])):
            point, at, radius = group["point"], group["positions"][-1], group["radii"][-1]
            text = point["symbol"].removesuffix("USDT")
            width = p.fontMetrics().horizontalAdvance(text) + 4
            height = p.fontMetrics().height() + 4
            offsets = [(radius + 7, -height / 2 - 4), (-radius - width - 7, -height / 2 - 4),
                       (radius + 7, 7), (-radius - width - 7, 7),
                       (-width / 2, -radius - height - 7), (-width / 2, radius + 7)]
            # A small bounded search keeps all four names visible in crowded or
            # narrow plots, while leaving the bubble coordinates untouched.
            def label_offsets():
                yield from offsets
                alternatives = [(dx, dy) for dx in range(-160, 161, 16) for dy in range(-96, 97, 16)]
                yield from sorted(alternatives, key=lambda v: (v[0] * v[0] + v[1] * v[1], -v[0], v[1]))

            for dx, dy in label_offsets():
                box = QtCore.QRectF(at.x() + dx, at.y() + dy, width, height)
                if r.contains(box) and self._label_clear(box, occupied, circles, segments):
                    occupied.append(box)
                    self._labels.append((point, box))
                    p.setPen(QtGui.QColor("#FFFFFF" if point["symbol"] == self.selected else ROTATION_PALETTE["text"]))
                    p.drawText(box, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
                    break

    def _hit(self, pos):
        nearest = None
        distance = float("inf")
        for point, at, radius in reversed(self._hits):
            measured = math.hypot(pos.x() - at.x(), pos.y() - at.y())
            if measured <= radius and measured < distance:
                nearest, distance = point, measured
        return nearest

    def mousePressEvent(self, event):
        point = self._hit(event.position())
        if point and event.button() == Qt.MouseButton.LeftButton:
            self.chosen.emit(point["symbol"])
            self.setFocus()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        point = self._hit(event.position())
        if point and event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit(point["symbol"])
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        point = self._hit(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if point else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        symbols = sorted(p["symbol"] for p in self.points)
        if symbols and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            index = symbols.index(self.selected) if self.selected in symbols else -1
            step = 1 if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down) else -1
            self.chosen.emit(symbols[(index + step) % len(symbols)])
        elif self.selected and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.opened.emit(self.selected)
        else:
            super().keyPressEvent(event)


class _rotation_MiniChart(QtWidgets.QWidget):
    def __init__(self, *, bars=False, compact=False, color="#35C4B2", parent=None):
        super().__init__(parent)
        self.values = []
        self.bars, self.compact, self.color = bars, compact, color
        self.setFixedHeight(44 if compact else 66 if bars else 90)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Completed hourly volume" if bars else "Relative return versus BTC")

    def set_values(self, values, color=None):
        self.values = list(values)
        if color:
            self.color = color
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        apply_text_render_hints(p)
        p.fillRect(self.rect(), QtGui.QColor("#000000"))
        r = QtCore.QRectF(1, 2, max(1, self.width() - (2 if self.compact else 47)), max(1, self.height() - (4 if self.compact else 19)))
        p.setPen(QtGui.QColor(ROTATION_PALETTE["border"]))
        if not self.compact:
            p.drawRect(r)
        for i in (() if self.compact else range(1, 4)):
            p.setPen(QtGui.QColor("#16181B"))
            p.drawLine(QtCore.QPointF(r.left() + r.width() * i / 4, r.top()), QtCore.QPointF(r.left() + r.width() * i / 4, r.bottom()))
            p.drawLine(QtCore.QPointF(r.left(), r.top() + r.height() * i / 4), QtCore.QPointF(r.right(), r.top() + r.height() * i / 4))
        values = [v for v in self.values if _rotation_finite(v)]
        p.setFont(typography_font(TextRole.CHART_AXIS))
        p.setPen(QtGui.QColor(ROTATION_PALETTE["muted"]))
        if not values:
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Waiting for history")
            return
        low, high = (0, max(values)) if self.bars else (min(values), max(values))
        if high <= low:
            high = low + max(abs(low) * .02, .1)
        for i in (() if self.compact else (0, 1, 2)):
            v = high - i / 2 * (high - low)
            text = _amount(v).removeprefix("$") if self.bars else f"{v:+.1f}%"
            p.drawText(QtCore.QRectF(r.right() + 4, r.top() + r.height() * i / 2 - 8, 40, 16), Qt.AlignmentFlag.AlignVCenter, text)
        if self.bars:
            width = r.width() / max(1, len(self.values))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(self.color))
            for i, v in enumerate(self.values):
                if _rotation_finite(v):
                    height = (v - low) / (high - low) * r.height()
                    p.drawRect(QtCore.QRectF(r.left() + i * width + 1, r.bottom() - height, max(1, width - 2), height))
        else:
            path = QtGui.QPainterPath()
            connected = False
            for i, v in enumerate(self.values):
                if not _rotation_finite(v):
                    connected = False
                    continue
                at = QtCore.QPointF(r.left() + i / max(1, len(self.values) - 1) * r.width(), r.bottom() - (v - low) / (high - low) * r.height())
                if connected:
                    path.lineTo(at)
                else:
                    path.moveTo(at)
                connected = True
            p.setPen(QtGui.QPen(QtGui.QColor(self.color), 1.7))
            p.drawPath(path)
        if not self.compact:
            p.setPen(QtGui.QColor(ROTATION_PALETTE["muted"]))
            caption = "24 completed hours" if self.bars else "Hourly closes · rebased vs BTC"
            caption = p.fontMetrics().elidedText(caption, Qt.TextElideMode.ElideRight, int(r.width()))
            p.drawText(QtCore.QRectF(r.left(), r.bottom() + 2, r.width(), 15), Qt.AlignmentFlag.AlignLeft, caption)


class _rotation_NumericItem(QtWidgets.QTableWidgetItem):
    def __lt__(self, other):
        a, b = self.data(_ROTATION_SORT_ROLE), other.data(_ROTATION_SORT_ROLE)
        order = self.tableWidget().horizontalHeader().sortIndicatorOrder()
        # Qt reverses comparisons for descending; keep unknown entries last in both directions.
        if a is None or b is None:
            if a is None and b is None:
                return self.data(_ROTATION_SYMBOL_ROLE) < other.data(_ROTATION_SYMBOL_ROLE)
            return a is None if order == Qt.SortOrder.DescendingOrder else b is None
        return (a, self.data(_ROTATION_SYMBOL_ROLE)) < (b, other.data(_ROTATION_SYMBOL_ROLE))


def _rotation_label(text, role=TextRole.UI_BODY, name="", parent=None):
    widget = QtWidgets.QLabel(text, parent)
    set_text_role(widget, role)
    if name:
        widget.setObjectName(name)
    return widget


def _rotation_button(text, name="rotationButton"):
    button = QtWidgets.QPushButton(text)
    button.setObjectName(name)
    button.setMinimumHeight(34)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAccessibleName(text)
    return button


def _rotation_combo(items):
    combo = QtWidgets.QComboBox()
    combo.setMinimumHeight(36)
    for caption, data in items:
        combo.addItem(caption, data)
    return combo


class RotationScannerWidget(LeadershipTimelineWidget):
    """Reference-layout rotation workspace, with Leadership-compatible host interfaces."""

    def __init__(self, _ignored_theme=None, parent=None):
        self.leadership = None
        self._rotation_revision = None
        self._filtered_points = []
        self._sort_column = 2
        self._sort_order = Qt.SortOrder.DescendingOrder
        super().__init__(_ignored_theme, parent)
        self.detail_freshness_timer = QtCore.QTimer(self)
        self.detail_freshness_timer.setInterval(30_000)
        self.detail_freshness_timer.timeout.connect(self._refresh_detail_view)
        self.setObjectName("rotationScanner")
        self._apply_fixed_palette()

    def _build_ui(self):
        self.setObjectName("rotationScanner")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # The host owns Nightwatch's top navigation; this module supplies the Markets page.
        strip = QtWidgets.QFrame()
        strip.setObjectName("rotationSummary")
        summary = QtWidgets.QHBoxLayout(strip)
        summary.setContentsMargins(20, 12, 20, 12)
        summary.setSpacing(16)
        self.summary_btc = _rotation_label("BTC 4H  —", TextRole.UI_BODY)
        self.summary_breadth = _rotation_label("Alts beating BTC  —", TextRole.UI_BODY)
        self.summary_count = _rotation_label("Candidates  —", TextRole.UI_BODY)
        for i, widget in enumerate((self.summary_btc, self.summary_breadth, self.summary_count)):
            if i:
                divider = _rotation_label("|", name="rotationMuted")
                summary.addWidget(divider)
            summary.addWidget(widget)
        summary.addStretch(1)
        self.refresh_button = _rotation_button("↻  Refresh")
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        summary.addWidget(self.refresh_button)
        root.addWidget(strip)

        self.page_scroll = QtWidgets.QScrollArea()
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        body = QtWidgets.QWidget()
        body.setMinimumWidth(1140)
        self.page_scroll.setWidget(body)
        root.addWidget(self.page_scroll, 1)
        columns = QtWidgets.QHBoxLayout(body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)

        self.filter_panel = QtWidgets.QFrame()
        self.filter_panel.setObjectName("rotationFilters")
        self.filter_panel.setFixedWidth(238)
        filters = QtWidgets.QVBoxLayout(self.filter_panel)
        filters.setContentsMargins(18, 22, 18, 20)
        filters.setSpacing(12)
        filters.addWidget(_rotation_label("UNIVERSE", TextRole.PANEL_TITLE))
        self.limit = _rotation_combo([("Liquid USDT perps", 0), ("Top 80 liquid pairs", 80), ("Top 40 liquid pairs", 40), ("Top 160 liquid pairs", 160)])
        self.limit.setToolTip("Ranked by the current Binance 24H USDT turnover. BTC is the benchmark and is excluded.")
        filters.addWidget(self.limit)
        self.category_filter = _rotation_combo([("All sectors", "All sectors")])
        filters.addWidget(self.category_filter)
        filters.addSpacing(4)
        filters.addWidget(_rotation_label("Relative window", name="rotationMuted"))
        self.span = _rotation_combo([("1H", 1), ("4H", 4), ("12H", 12), ("24H", 24)])
        self.span.setCurrentIndex(1)
        self.span.setToolTip("X-axis return window vs BTC. The Y-axis always shows the hour-on-hour change in 1H relative return.")
        filters.addWidget(self.span)
        filters.addSpacing(4)
        filters.addWidget(_rotation_label("Minimum volume", name="rotationMuted"))
        self.liquidity = _rotation_combo([("$5M / 24H", 5_000_000), ("$20M / 24H", 20_000_000), ("$50M / 24H", 50_000_000), ("$100M / 24H", 100_000_000)])
        self.liquidity.setCurrentIndex(1)
        filters.addWidget(self.liquidity)
        self.spot_confirmation = QtWidgets.QCheckBox("Spot confirmation")
        self.spot_confirmation.setToolTip("Only include coins with rising spot share over two consecutive 4H windows. Unavailable spot history does not pass.")
        self.hide_thin = QtWidgets.QCheckBox("Hide verified thin books")
        self.hide_thin.setToolTip("Hide coins with a verified live spread above 0.15% or combined ±0.5% depth below $25K.\nBooks are checked on selection. Unknown or stale books stay visible; no historical books are inferred.")
        self.watched_only = QtWidgets.QCheckBox("Watchlist only")
        filters.addSpacing(4)
        filters.addWidget(self.spot_confirmation)
        filters.addWidget(self.hide_thin)
        filters.addWidget(self.watched_only)
        filters.addSpacing(12)
        self.reset_button = _rotation_button("↺  Reset filters")
        filters.addWidget(self.reset_button)
        self.reset_button.clicked.connect(self._reset_filters)
        self.coverage = _rotation_label("Waiting for history", name="rotationMuted")
        self.coverage.setWordWrap(False)
        filters.addSpacing(12)
        filters.addWidget(self.coverage)
        filters.addStretch(1)
        columns.addWidget(self.filter_panel)

        center = QtWidgets.QWidget()
        center_layout = QtWidgets.QVBoxLayout(center)
        center_layout.setContentsMargins(18, 20, 18, 16)
        center_layout.setSpacing(12)
        chart_panel = QtWidgets.QFrame()
        chart_panel.setObjectName("rotationMapPanel")
        chart_layout = QtWidgets.QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(0)
        self.bubbles = RotationBubbleChart()
        self.bubbles.chosen.connect(self.select_symbol)
        self.bubbles.opened.connect(self.symbol_selected.emit)
        chart_layout.addWidget(self.bubbles, 1)
        center_layout.addWidget(chart_panel, 3)

        table_panel = QtWidgets.QFrame()
        table_panel.setObjectName("rotationPanel")
        table_layout = QtWidgets.QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 10, 0, 0)
        table_layout.setSpacing(6)
        table_head = QtWidgets.QHBoxLayout()
        table_head.setContentsMargins(10, 0, 10, 0)
        self.candidates_title = _rotation_label("Improving candidates", TextRole.PANEL_TITLE)
        table_head.addWidget(self.candidates_title)
        table_head.addStretch(1)
        self.candidate_mode = _rotation_combo([("Improving momentum", "improving"), ("All plotted coins", "all")])
        self.candidate_mode.setMinimumHeight(28)
        self.candidate_mode.setMaximumWidth(180)
        table_head.addWidget(self.candidate_mode)
        table_layout.addLayout(table_head)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search symbol or name…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(30)
        table_layout.addWidget(self.search)
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setObjectName("rotationCandidates")
        self.table.setHorizontalHeaderLabels(["#", "Pair", "1H vs BTC", "Volume / baseline", "OI 1H", "Spread", "Status"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(166)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicator(2, Qt.SortOrder.DescendingOrder)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        for col, width in enumerate((34, 92, 84, 112, 76, 70, 155)):
            self.table.setColumnWidth(col, width + (18 if col in (2, 3) else 0))
            self.table.horizontalHeader().setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.Stretch if col == 6 else QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_candidates)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellDoubleClicked.connect(self._open_row)
        table_layout.addWidget(self.table, 1)
        self.empty_candidates = _rotation_label("Waiting for comparable histories", TextRole.UI_CAPTION, "rotationMuted")
        self.empty_candidates.setContentsMargins(10, 0, 10, 6)
        table_layout.addWidget(self.empty_candidates)
        center_layout.addWidget(table_panel, 2)
        columns.addWidget(center, 1)

        self.inspector = QtWidgets.QFrame()
        self.inspector.setObjectName("rotationInspector")
        self.inspector.setFixedWidth(350)
        info = QtWidgets.QVBoxLayout(self.inspector)
        info.setContentsMargins(16, 20, 16, 16)
        info.setSpacing(8)
        instrument_row = QtWidgets.QHBoxLayout()
        self.selected_symbol = _rotation_label("Select a coin", TextRole.PANEL_TITLE)
        self.watch_selected = _rotation_button("☆  Watch")
        self.watch_selected.setCheckable(True)
        self.watch_selected.setMinimumHeight(30)
        self.watch_selected.clicked.connect(lambda: self.toggle_watch(self.selected))
        instrument_row.addWidget(self.selected_symbol, 1)
        instrument_row.addWidget(self.watch_selected)
        info.addLayout(instrument_row)
        self.selected_name = _rotation_label("Click a bubble or candidate", TextRole.UI_CAPTION, "rotationMuted")
        self.selected_name.setWordWrap(False)
        info.addWidget(self.selected_name)
        prices = QtWidgets.QHBoxLayout()
        self.selected_price = _rotation_label("—", TextRole.MARKET_VALUE_HERO)
        self.selected_change = _rotation_label("—", TextRole.TABLE_VALUE)
        prices.addWidget(self.selected_price)
        prices.addWidget(self.selected_change)
        prices.addStretch(1)
        info.addLayout(prices)
        self.relative_value = _rotation_label("vs BTC  —", TextRole.TABLE_VALUE)
        info.addWidget(self.relative_value)
        self.price_caption = _rotation_label("Relative price (4H)", name="rotationMuted")
        info.addWidget(self.price_caption)
        self.relative_chart = _rotation_MiniChart()
        info.addWidget(self.relative_chart)
        info.addWidget(_rotation_label("Volume (USDT) · 24H", name="rotationMuted"))
        self.volume_chart = _rotation_MiniChart(bars=True, color="#7C838C")
        info.addWidget(self.volume_chart)
        info.addWidget(_rotation_label("Key facts", TextRole.PANEL_TITLE))
        self.fact_values = {}
        for key, caption in (("spot", "Spot volume share rising"), ("funding", "Funding / 8H"),
                             ("range", "Distance to 4H high"), ("oi", "Perp OI / 1H"), ("book", "Live spread")):
            frame = QtWidgets.QFrame()
            frame.setObjectName("rotationFact")
            row = QtWidgets.QHBoxLayout(frame)
            row.setContentsMargins(0, 5, 0, 5)
            row.addWidget(_rotation_label(caption, TextRole.UI_CAPTION, "rotationMuted"), 1)
            value = _rotation_label("—", TextRole.TABLE_VALUE)
            row.addWidget(value)
            self.fact_values[key] = value
            info.addWidget(frame)
        self.compare_title = _rotation_label("Compare with a leader", TextRole.PANEL_TITLE)
        info.addWidget(self.compare_title)
        self.compare_selected = _rotation_label("Selected vs BTC", TextRole.UI_CAPTION, "rotationMuted")
        info.addWidget(self.compare_selected)
        self.compare_chart = _rotation_MiniChart(compact=True)
        info.addWidget(self.compare_chart)
        self.compare_other = _rotation_label("Leader vs BTC", TextRole.UI_CAPTION, "rotationMuted")
        info.addWidget(self.compare_other)
        self.compare_other_chart = _rotation_MiniChart(compact=True, color=ROTATION_PALETTE["active_line"])
        info.addWidget(self.compare_other_chart)
        info.addStretch(1)
        self.open_selected = _rotation_button("↗  OPEN CHART", "rotationPrimary")
        self.open_selected.clicked.connect(lambda: self.symbol_selected.emit(self.selected) if self.selected else None)
        info.addWidget(self.open_selected)
        columns.addWidget(self.inspector)

        footer = QtWidgets.QFrame()
        footer.setObjectName("rotationFooter")
        foot = QtWidgets.QHBoxLayout(footer)
        foot.setContentsMargins(18, 8, 18, 8)
        self.asof = _rotation_label("Waiting for completed hourly data", TextRole.UI_CAPTION, "rotationMuted")
        foot.addWidget(self.asof)
        foot.addStretch(1)
        foot.addWidget(_rotation_label("BINANCE USDT PERPETUALS", TextRole.UI_CAPTION, "rotationMuted"))
        root.addWidget(footer)
        # Hidden lifecycle controls preserve Leadership's replay/detail interfaces.
        self.replay = QtWidgets.QSlider(Qt.Orientation.Horizontal, self)
        self.replay.setRange(0, 24)
        self.replay.setValue(24)
        self.replay.hide()
        self.play = QtWidgets.QPushButton(self)
        self.play.setCheckable(True)
        self.play.hide()
        self.replay.valueChanged.connect(self._cursor_changed)
        self.search_timer = QtCore.QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._view_changed)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        for combo in (self.category_filter, self.candidate_mode):
            combo.currentIndexChanged.connect(self._view_changed)
        self.span.currentIndexChanged.connect(self.render)
        self.limit.currentIndexChanged.connect(self._filters_changed)
        self.liquidity.currentIndexChanged.connect(self._filters_changed)
        for checkbox in (self.spot_confirmation, self.hide_thin, self.watched_only):
            checkbox.toggled.connect(self._view_changed)
        self.search_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), self)
        self.search_shortcut.activated.connect(self.search.setFocus)
        self._sync_watch()

    def _apply_fixed_palette(self):
        self.theme = dict(ROTATION_PALETTE)
        self.setStyleSheet(rotation_stylesheet())
        if hasattr(self, "bubbles"):
            self.bubbles.update()

    def _analysis_view_key(self):
        return (self.generation, self.cursor_end(), tuple(self.symbols), self.span.currentData())

    def render(self):
        self._render_dirty = True
        if (not self.active or self.closing or self._interaction_paused or self._cache_loading or self._rendering):
            return
        self._analysis_serial += 1
        state = dict(symbols=tuple(self.symbols), series=dict(self.series),
                     spot_series=dict(self.spot_series), cursor=self.cursor_end(), hours=self.span.currentData() or 4)
        self._analysis_job.submit((self._analysis_view_key(), self._analysis_serial),
                                  _workspace_analysis, _prepare_rotation, state, self._history_transport)

    def _render(self, prepared):
        self.metrics = prepared["metrics"]
        self._rotation_revision = (self.generation, prepared["end"], tuple(self.symbols), prepared["hours"])
        self._update_categories()
        self._render_view(prepared)

    def _update_categories(self):
        desired = ["All sectors", *sorted({self.identity(s)[1] for s in self.symbols if self.identity(s)[1] != "—"})]
        current = self.category_filter.currentText()
        if [self.category_filter.itemText(i) for i in range(self.category_filter.count())] != desired:
            blocker = QtCore.QSignalBlocker(self.category_filter)
            self.category_filter.clear()
            for value in desired:
                self.category_filter.addItem(value, value)
            self.category_filter.setCurrentIndex(max(0, self.category_filter.findText(current)))
            del blocker

    def _view_changed(self, *_):
        if self.closing:
            return
        context = (self.generation, self.cursor_end(), tuple(self.symbols), self.span.currentData())
        if self._prepared_analysis and context == self._rotation_revision and self.active and not self._interaction_paused:
            was_rendering = self._rendering
            self._rendering = True
            try:
                self._render_view(self._prepared_analysis)
            finally:
                self._rendering = was_rendering
        else:
            self.render()

    def _visible(self, point):
        symbol = point["symbol"]
        name, category = self.identity(symbol)
        query = self.search.text().strip().casefold().replace("/", "").removesuffix(".p")
        if query and query not in symbol.casefold() and query not in name.casefold():
            return False
        wanted = self.category_filter.currentText()
        if wanted != "All sectors" and category != wanted:
            return False
        if self.spot_confirmation.isChecked() and not self.metrics[symbol].get("spot_confirmed"):
            return False
        if self.watched_only.isChecked() and not (self.watchlist and self.watchlist.contains(symbol)):
            return False
        if self.hide_thin.isChecked():
            detail = self._numbers(symbol)
            if detail["spread"] is not None and (detail["spread"] > .15 or detail["depth"] < 25_000):
                return False
        return True

    def _render_view(self, prepared):
        self._filtered_points = [p for p in prepared["points"] if self._visible(p)]
        visible = {p["symbol"] for p in self._filtered_points}
        if self.selected not in visible:
            self.selected = next((p["symbol"] for p in sorted(self._filtered_points, key=lambda p: (-p["y"], p["symbol"]))), "")
        self.bubbles.set_points(self._filtered_points, self.selected, prepared["hours"])
        self.summary_btc.setText(f"BTC {prepared['hours']}H  {_pct(prepared['btc_return'], 1)}")
        covered = prepared["covered"]
        self.summary_breadth.setText(f"Alts beating BTC  {prepared['beating'] / covered * 100:.0f}%" if covered else "Alts beating BTC  —")
        self.summary_count.setText(f"Candidates  {len(self._filtered_points)} / {len(self.symbols)}")
        self.coverage.setText(f"{len(self.symbols)} liquid pairs\n{len(self._filtered_points)} showing · {covered} comparable")
        self.coverage.setToolTip("\n".join(f"{s}: {e}" for s, e in self.errors.items()) or "Only comparable completed hourly candles are plotted.")
        self.asof.setText(("Latest completed hour · " if self.replay.value() == 24 else "Replay · ") + (_stamp(prepared["end"], True) if prepared["end"] else "Waiting for history"))
        self._populate_candidates()
        self._render_facts()
        self._schedule_details()

    def _numbers(self, symbol):
        key = (symbol, self.cursor_end(), self.replay.value() == 24)
        return _rotation_detail_numbers(self.details.get(key, {}), key[1], key[2])

    def _populate_candidates(self):
        points = [p for p in self._filtered_points if self.candidate_mode.currentData() == "all" or p["y"] > 0]
        points.sort(key=lambda p: (-self.metrics[p["symbol"]].get("rs1", 0), p["symbol"]))
        self.candidates_title.setText("Improving candidates" if self.candidate_mode.currentData() == "improving" else "Rotation candidates")
        table = self.table
        blocker = QtCore.QSignalBlocker(table)
        scroll = table.verticalScrollBar().value()
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        try:
            table.setRowCount(len(points))
            for row, point in enumerate(points):
                symbol = point["symbol"]
                metrics, details = self.metrics[symbol], self._numbers(symbol)
                status = "Near range high" if point["y"] > 0 and _rotation_finite(metrics.get("high_distance")) and metrics["high_distance"] <= 1 else point["quadrant"]
                rvol = metrics.get("rvol")
                values = [(str(row + 1), row + 1), (symbol.removesuffix("USDT"), symbol),
                          (_pct(metrics.get("rs1"), 1), metrics.get("rs1")),
                          (f"{rvol:.1f}×" if rvol is not None else "—", rvol),
                          (_pct(details["oi"], 1), details["oi"]),
                          (f"{details['spread']:.2f}%" if details["spread"] is not None else "—", details["spread"]),
                          (status, status)]
                for col, (text, raw) in enumerate(values):
                    item = table.item(row, col)
                    if item is None:
                        item = _rotation_NumericItem()
                        table.setItem(row, col, item)
                    item.setText(text)
                    item.setData(_ROTATION_SYMBOL_ROLE, symbol)
                    item.setData(_ROTATION_SORT_ROLE, raw if not isinstance(raw, (int, float)) or _rotation_finite(raw) else None)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if col in (1, 6) else Qt.AlignmentFlag.AlignRight))
                    color = self.theme["text"]
                    if col in (2, 4) and _rotation_finite(raw):
                        color = QUADRANT_COLORS["Leading" if raw >= 0 else "Lagging"]
                    elif col == 3 and rvol is not None and rvol >= 1:
                        color = QUADRANT_COLORS["Leading"]
                    item.setForeground(QtGui.QColor(color))
                    tooltip = f"{symbol} · {self.identity(symbol)[0]}\n{self.span.currentText()} vs BTC: {_pct(point['x'])}\n1H change in RS: {point['y']:+.2f} pp\nDouble-click to open chart"
                    if col == 3:
                        tooltip += "\nLatest completed hourly turnover / median of the prior 24 completed hours."
                    elif col in (4, 5):
                        tooltip += "\nLoaded on selection; unavailable data stays blank. Spread is live and expires after 90 seconds."
                    elif col == 6:
                        tooltip += "\nNear range high: within 1% of the prior four-hour high, with positive RS acceleration."
                    item.setToolTip(tooltip)
            table.setSortingEnabled(True)
            table.sortItems(self._sort_column, self._sort_order)
            # The rank reflects the current order, while row actions use symbol roles, never row indices.
            for row in range(table.rowCount()):
                table.item(row, 0).setText(str(row + 1))
                if table.item(row, 1).data(_ROTATION_SYMBOL_ROLE) == self.selected:
                    table.selectRow(row)
            if self.selected not in {p["symbol"] for p in points}:
                table.clearSelection()
            self.empty_candidates.setText("No improving candidates. Switch to All plotted coins or relax the filters." if not points else "")
            self.empty_candidates.setVisible(not points)
        finally:
            table.setUpdatesEnabled(True)
            table.verticalScrollBar().setValue(scroll)
            del blocker

    def _sort_candidates(self, column):
        if column == 0:
            self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
            self._populate_candidates()
            return
        if column == self._sort_column:
            self._sort_order = Qt.SortOrder.AscendingOrder if self._sort_order == Qt.SortOrder.DescendingOrder else Qt.SortOrder.DescendingOrder
        else:
            self._sort_column = column
            self._sort_order = Qt.SortOrder.AscendingOrder if column in (1, 6) else Qt.SortOrder.DescendingOrder
        self.table.horizontalHeader().setSortIndicator(column, self._sort_order)
        self._populate_candidates()

    def _table_selected(self):
        row = self.table.currentRow()
        if row >= 0 and self.table.item(row, 1):
            self.select_symbol(self.table.item(row, 1).data(_ROTATION_SYMBOL_ROLE))

    def _open_row(self, row, _column):
        if self.table.item(row, 1):
            self.symbol_selected.emit(self.table.item(row, 1).data(_ROTATION_SYMBOL_ROLE))

    def select_symbol(self, symbol):
        if symbol not in {p["symbol"] for p in self._filtered_points}:
            return
        self.selected = symbol
        self.bubbles.selected = symbol
        self.bubbles.update()
        blocker = QtCore.QSignalBlocker(self.table)
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 1).data(_ROTATION_SYMBOL_ROLE) == symbol:
                self.table.selectRow(row)
                break
        del blocker
        self._render_facts()
        self._schedule_details()

    def _sync_watch(self, *_):
        if not hasattr(self, "watch_selected"):
            return
        watched = bool(self.watchlist and self.selected and self.watchlist.contains(self.selected))
        blocker = QtCore.QSignalBlocker(self.watch_selected)
        self.watch_selected.setChecked(watched)
        self.watch_selected.setText("★  Watched" if watched else "☆  Watch")
        self.watch_selected.setEnabled(bool(self.selected) and self.watchlist is not None and self.selected in self.valid_symbols)
        self.watch_selected.setToolTip("Remove from watchlist" if watched else "Add to watchlist")
        del blocker
        signature = tuple(s for s in self.symbols if self.watchlist and self.watchlist.contains(s))
        changed = signature != getattr(self, "_watched_signature", ())
        self._watched_signature = signature
        if changed and hasattr(self, "watched_only") and self.watched_only.isChecked() and self._prepared_analysis and not self._rendering:
            self._view_changed()

    def _render_facts(self):
        symbol = self.selected
        metrics = self.metrics.get(symbol, {})
        charts = self._prepared_analysis.get("charts", {})
        values = charts.get(symbol, {})
        self.selected_symbol.setText(symbol or "Select a coin")
        name, category = self.identity(symbol) if symbol else ("Click a bubble or candidate", "")
        self.selected_name.setText(f"{name} / TetherUS · USDT Perp · {category}" if symbol else name)
        live = self.replay.value() == 24
        ticker_price = safe_float(self.tickers.get(symbol, {}).get("c")) if live else 0
        self.selected_price.setText(_price(ticker_price if ticker_price > 0 else metrics.get("price")))
        self.selected_price.setToolTip("Live ticker price" if ticker_price > 0 else "Selected completed hourly close")
        self.selected_change.setText(_pct(metrics.get("usd4"), 1) + " · 4H")
        self.selected_change.setToolTip("4H completed-candle return in USD")
        self.selected_change.setStyleSheet("color: " + (QUADRANT_COLORS["Leading"] if (metrics.get("usd4") or 0) >= 0 else QUADRANT_COLORS["Lagging"]))
        self.relative_value.setText(f"vs BTC  {_pct(metrics.get('x'), 1)} · {metrics.get('quadrant', 'Waiting')}")
        self.price_caption.setText(f"Relative price ({max(4, self.span.currentData())}H) · vs BTC")
        self.relative_chart.set_values(values.get("relative", []))
        self.volume_chart.set_values(values.get("volume", []))
        numbers = self._numbers(symbol)
        spot = metrics.get("spot_delta")
        distance = metrics.get("high_distance")
        facts = {"spot": ("Yes" if spot > 0 else "No") if spot is not None else "—",
                 "funding": _pct(numbers["funding"], 3),
                 "range": f"{distance:.1f}%" if distance is not None else "—",
                 "oi": _pct(numbers["oi"], 1),
                 "book": f"{numbers['spread']:.2f}%" if numbers["spread"] is not None else "—"}
        for key, label in self.fact_values.items():
            label.setText(facts[key])
            label.setStyleSheet("color: " + (QUADRANT_COLORS["Leading"] if key == "spot" and spot is not None and spot > 0 or key == "oi" and (numbers["oi"] or 0) > 0 else self.theme["text"]))
        self.fact_values["spot"].setToolTip("Rising spot share: spot / (spot + perpetual) 4H turnover, compared with the preceding 4H window.")
        self.fact_values["funding"].setToolTip("Most recent settled funding rate normalized to 8H using the observed settlement interval. Missing/stale intervals stay unavailable.")
        self.fact_values["range"].setToolTip("Distance from the selected completed close to the prior four-hour high. Negative values indicate a breakout above that high.")
        self.fact_values["book"].setToolTip("Observed live best bid/ask spread, younger than 90 seconds. No historical book is substituted during replay.")
        errors = self.details.get((symbol, self.cursor_end(), live), {}).get("errors", {})
        self.fact_values["oi"].setToolTip(errors.get("oi", "Change in open-interest quantity over consecutive hourly observations. Reuses details already loaded by Leaders."))
        leaders = [p for p in self._filtered_points if p["symbol"] != symbol and p["quadrant"] == "Leading"]
        if not leaders:
            leaders = [p for p in self._filtered_points if p["symbol"] != symbol]
        other = max(leaders, key=lambda p: (p["x"], p["symbol"]))["symbol"] if leaders else ""
        self.compare_title.setText("Compare with " + other.removesuffix("USDT") if other else "Compare with a leader")
        self.compare_selected.setText(f"{symbol.removesuffix('USDT') or 'Selected'} vs BTC   {_pct(metrics.get('x'), 1)}")
        self.compare_other.setText(f"{other.removesuffix('USDT') or 'No comparison'} vs BTC   {_pct(self.metrics.get(other, {}).get('x'), 1)}")
        self.compare_chart.set_values(values.get("relative", []))
        self.compare_other_chart.set_values(charts.get(other, {}).get("relative", []))
        self.open_selected.setEnabled(bool(symbol))
        self._sync_watch()

    def _refresh_detail_view(self):
        if self.active and not self._interaction_paused and self._prepared_analysis:
            self._view_changed()

    def set_active(self, active):
        super().set_active(active)
        if self.active:
            self.detail_freshness_timer.start()
        else:
            self.detail_freshness_timer.stop()

    def _details_finished(self, result):
        super()._details_finished(result)
        if self.active and self._prepared_analysis:
            self._view_changed()

    def _details_failed(self, error):
        super()._details_failed(error)
        if self.active and self._prepared_analysis:
            self._populate_candidates()

    def _refresh_live_prices(self):
        # Rotation's candidate table has RS/RVol columns; price is in its inspector.
        pass

    def set_tickers(self, tickers):
        super().set_tickers(tickers)
        self.update_tickers(())

    def update_tickers(self, updates):
        super().update_tickers(updates)
        if self.active and not self._interaction_paused and self.selected:
            if not self.detail_timer.isActive():
                self.detail_timer.start()

    def _reset_filters(self):
        for widget, data in ((self.limit, 0), (self.liquidity, 20_000_000), (self.span, 4), (self.category_filter, "All sectors"), (self.candidate_mode, "improving")):
            blocker = QtCore.QSignalBlocker(widget)
            widget.setCurrentIndex(max(0, widget.findData(data)))
            del blocker
        for widget in (self.spot_confirmation, self.hide_thin, self.watched_only, self.search):
            blocker = QtCore.QSignalBlocker(widget)
            widget.clear() if widget is self.search else widget.setChecked(False)
            del blocker
        self._filters_changed()
        self.render()

    def bind_leadership(self, leadership):
        """Bind to the existing Leaders loader; Rotation never issues network requests."""
        if self.leadership is leadership:
            return
        if self.leadership is not None:
            try:
                self.leadership.data_changed.disconnect(self._leaders_changed)
                self.leadership.details_changed.disconnect(self._refresh_detail_view)
            except (RuntimeError, TypeError):
                pass
        self.load_timer.stop()
        self.cancel.set()
        self._load_pending = False
        self.generation += 1
        self.leadership = leadership
        if leadership is not None:
            leadership.data_changed.connect(self._leaders_changed)
            leadership.details_changed.connect(self._refresh_detail_view)
            self._adopt_shared_bindings()
        if self.active:
            self.refresh(force=True)

    def _adopt_shared_bindings(self):
        source = self.leadership
        if source is None:
            return
        if self.watchlist is not source.watchlist:
            if self.watchlist is not None:
                try:
                    self.watchlist.symbols_changed.disconnect(self._sync_watch)
                except (RuntimeError, TypeError):
                    pass
            self.watchlist = source.watchlist
            if self.watchlist is not None:
                self.watchlist.symbols_changed.connect(self._sync_watch)
        # Only data and watchlist bindings are shared, never REST/database clients.
        self.rest = self.db = None
        self.can_load = source.can_load
        self.valid_symbols = set(source.valid_symbols)
        self.tickers = source.tickers
        self.details = source.details

    def _leaders_changed(self):
        if self.leadership is None or self.closing:
            return
        self._adopt_shared_bindings()
        snapshot = self.leadership.sector_hourly_snapshot()
        self.end, self.clock_offset = snapshot["end"], snapshot["clock_offset"]
        self.series, self.spot_series = snapshot["series"], snapshot["spot"]
        self.categories = dict(snapshot["categories"])
        minimum = self.liquidity.currentData()
        symbols = [s for s in snapshot["symbols"] if safe_float(self.tickers.get(s, {}).get("q")) >= minimum]
        symbols.sort(key=lambda s: (-safe_float(self.tickers.get(s, {}).get("q")), s))
        count = self.limit.currentData()
        self.symbols = symbols[:count] if count else symbols
        self.eligible_count = len(symbols)
        self.generation += 1
        self._render_dirty = True
        if self.active:
            self.render()

    def bind_sources(self, rest, db, watchlist, can_load):
        """Compatibility binding only: no cache restore, loader, or REST jobs."""
        self.can_load = can_load
        if self.leadership is not None:
            self._adopt_shared_bindings()

    def refresh(self, *, force=False):
        if self.closing:
            return
        if self._interaction_paused:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        self._leaders_changed()

    def _filters_changed(self):
        self.refresh(force=True)

    def _next_batch(self):
        # Histories are owned exclusively by Leaders.
        return

    def _save_cached_state(self):
        return

    def _schedule_details(self):
        # Selection must never start a new REST request.
        return

    def _request_details(self):
        # Coalesce ticker presentation; this timer never starts backend work.
        if self.active and not self._interaction_paused:
            self._render_facts()

    def shutdown(self):
        if self.leadership is not None:
            try:
                self.leadership.data_changed.disconnect(self._leaders_changed)
                self.leadership.details_changed.disconnect(self._refresh_detail_view)
            except (RuntimeError, TypeError):
                pass
        self.search_timer.stop()
        self.detail_freshness_timer.stop()
        super().shutdown()

    def restore_ui_state(self, settings):
        for key, widget, default in (("window", self.span, 4), ("liquidity", self.liquidity, 20_000_000), ("limit", self.limit, 0), ("candidates", self.candidate_mode, "improving")):
            value = settings.value(f"markets/rotation/{key}", default, str if isinstance(default, str) else int)
            blocker = QtCore.QSignalBlocker(widget)
            widget.setCurrentIndex(max(0, widget.findData(value)))
            del blocker
        self.selected = settings.value("markets/rotation/selected", "", str)
        category = settings.value("markets/rotation/category", "All sectors", str)
        if self.category_filter.findText(category) < 0:
            self.category_filter.addItem(category, category)
        self.category_filter.setCurrentText(category)
        self.search.setText(settings.value("markets/rotation/search", "", str))
        for key, widget in (("spot", self.spot_confirmation), ("thin", self.hide_thin), ("watched", self.watched_only)):
            blocker = QtCore.QSignalBlocker(widget)
            widget.setChecked(settings.value(f"markets/rotation/{key}", False, bool))
            del blocker
        self._sort_column = max(1, min(6, settings.value("markets/rotation/sort_column", 2, int)))
        descending = settings.value("markets/rotation/sort_descending", True, bool)
        self._sort_order = Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
        self._filters_changed()
        self.render()

    def save_ui_state(self, settings):
        values = dict(window=self.span.currentData(), liquidity=self.liquidity.currentData(), limit=self.limit.currentData(),
                      candidates=self.candidate_mode.currentData(), selected=self.selected,
                      category=self.category_filter.currentText(), search=self.search.text(),
                      spot=self.spot_confirmation.isChecked(), thin=self.hide_thin.isChecked(), watched=self.watched_only.isChecked(),
                      sort_column=self._sort_column, sort_descending=self._sort_order == Qt.SortOrder.DescendingOrder)
        for key, value in values.items():
            settings.setValue(f"markets/rotation/{key}", value)


def rotation_stylesheet():
    p = ROTATION_PALETTE
    return f"""
    QWidget#rotationScanner, QWidget#rotationScanner QWidget {{ background: #000000; color: {p['text']}; }}
    QWidget#rotationScanner QLabel {{ border: 0; background: #000000; }}
    QLabel#rotationMuted {{ color: {p['muted']}; }}
    QFrame#rotationSummary {{ border-bottom: 1px solid {p['border']}; }}
    QFrame#rotationFooter {{ border-top: 1px solid {p['border']}; }}
    QFrame#rotationFilters {{ border-right: 1px solid {p['border']}; }}
    QFrame#rotationInspector {{ border-left: 1px solid {p['border']}; }}
    QFrame#rotationPanel {{ border: 1px solid {p['border']}; border-radius: 5px; }}
    QFrame#rotationMapPanel {{ border: 0; }}
    QFrame#rotationFact {{ border-bottom: 1px solid {p['separator']}; }}
    QWidget#rotationScanner QScrollArea {{ border: 0; background: #000000; }}
    QWidget#rotationScanner QComboBox, QWidget#rotationScanner QLineEdit {{
        background: #000000; color: {p['text']}; border: 1px solid {p['control_border']}; border-radius: 4px; padding: 4px 8px;
    }}
    QWidget#rotationScanner QComboBox:hover, QWidget#rotationScanner QLineEdit:hover {{ border-color: {p['muted']}; }}
    QWidget#rotationScanner QComboBox:focus, QWidget#rotationScanner QLineEdit:focus {{ border-color: {p['active_line']}; }}
    QWidget#rotationScanner QComboBox QAbstractItemView {{ background: #000000; selection-background-color: #000000; selection-color: {p['active_line']}; border: 1px solid {p['border']}; }}
    QPushButton#rotationButton, QPushButton#rotationPrimary {{
        background: #000000; color: {p['text']}; border: 1px solid {p['control_border']}; border-radius: 4px; padding: 2px 10px;
    }}
    QPushButton#rotationButton:hover, QPushButton#rotationButton:focus {{ border-color: {p['active_line']}; color: {p['active_line']}; }}
    QPushButton#rotationButton:checked {{ border-color: {p['active_line']}; color: {p['active_line']}; }}
    QPushButton#rotationPrimary {{ border-color: {p['active_line']}; color: {p['active_line']}; }}
    QPushButton#rotationPrimary:hover, QPushButton#rotationPrimary:focus {{ border-color: #FFFFFF; }}
    QPushButton#rotationButton:disabled, QPushButton#rotationPrimary:disabled {{ color: {p['muted']}; border-color: {p['separator']}; }}
    QWidget#rotationScanner QCheckBox {{ spacing: 8px; padding: 4px 0; }}
    QWidget#rotationScanner QCheckBox::indicator {{ width: 17px; height: 17px; background: #000000; border: 1px solid {p['control_border']}; border-radius: 3px; }}
    QWidget#rotationScanner QCheckBox::indicator:checked {{ background: {p['active_line']}; border-color: {p['active_line']}; }}
    QWidget#rotationScanner QCheckBox::indicator:focus {{ border-color: #FFFFFF; }}
    QTableWidget#rotationCandidates {{ border: 0; background: #000000; gridline-color: {p['separator']}; selection-background-color: #000000; selection-color: {p['text']}; outline: 0; }}
    QTableWidget#rotationCandidates::item {{ border-bottom: 1px solid {p['separator']}; padding: 5px; }}
    QTableWidget#rotationCandidates::item:selected {{ border-top: 1px solid {p['active_line']}; border-bottom: 1px solid {p['active_line']}; }}
    QTableWidget#rotationCandidates QHeaderView::section {{ background: #000000; color: {p['muted']}; border: 0; border-bottom: 1px solid {p['border']}; padding: 4px; }}
    QTableWidget#rotationCandidates QHeaderView::section:hover {{ color: {p['text']}; }}
    QWidget#rotationScanner QScrollBar:vertical {{ background: #000000; width: 7px; margin: 0; }}
    QWidget#rotationScanner QScrollBar:horizontal {{ background: #000000; height: 7px; margin: 0; }}
    QWidget#rotationScanner QScrollBar::handle {{ background: {p['control_border']}; min-width: 24px; min-height: 24px; border-radius: 3px; }}
    QWidget#rotationScanner QScrollBar::add-line, QWidget#rotationScanner QScrollBar::sub-line {{ width: 0; height: 0; }}
    QWidget#rotationScanner QScrollBar::add-page, QWidget#rotationScanner QScrollBar::sub-page {{ background: #000000; }}
    """


# Host projects may use either naming convention; all expose the identical widget.
