#!/usr/bin/env python3
"""Offline MainWindow interaction benchmark; run on the target desktop for GPU claims.

Counts chart paint completions and optional QOpenGLWidget composition completions,
never presentation scheduler ticks. See --help and tools/benchmark_interactions.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=ROOT, help="App source directory, allows identical harness against an immutable original checkout")
    parser.add_argument("--output", type=Path, default=Path("work/interactions.json"))
    parser.add_argument("--duration", type=float, default=4.0, help="Seconds per gesture")
    parser.add_argument("--input-hz", type=float, default=240.0)
    parser.add_argument("--history", type=int, default=100_000)
    parser.add_argument("--size", default="1920x1080", help="Desktop window width x height")
    parser.add_argument("--refresh-hz", type=float, help="Explicit target Hz, screen Hz remains recorded")
    parser.add_argument("--raster", action="store_true", help="Disable GL; software smoke/comparison only")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SHIBUSDT", help="Representative price scales")
    parser.add_argument("--cases", default="pan,zoom,deep_pan,wide_pan,watchlist_resize,rail_resize")
    parser.add_argument("--market-hz", type=float, default=20.0, help="Offline watchlist/depth/trade updates per second")
    parser.add_argument("--live-candle-hz", type=float, default=0.0, help="Optional production mutable-candle updates per second; zero preserves static-candle comparisons")
    parser.add_argument("--label", default="", help="Machine/run identity for before/after comparisons")
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.duration, args.input_hz)):
        parser.error("duration and input-hz must be finite and positive")
    if args.history < 2000 or not math.isfinite(args.market_hz) or args.market_hz < 0:
        parser.error("history must be >= 2000 and market-hz finite and nonnegative")
    if not math.isfinite(args.live_candle_hz) or args.live_candle_hz < 0:
        parser.error("live-candle-hz must be finite and nonnegative")
    if args.refresh_hz is not None and (not math.isfinite(args.refresh_hz) or args.refresh_hz <= 0):
        parser.error("refresh-hz must be finite and positive")
    try:
        args.window_width, args.window_height = (int(value) for value in args.size.lower().split("x"))
    except ValueError:
        parser.error("size must be WIDTHxHEIGHT with positive integer dimensions")
    if min(args.window_width, args.window_height) <= 0:
        parser.error("size dimensions must be positive")
    return args


def interval_summary(stamps, start, end, target_hz):
    intervals = [(b-a)*1000.0 for a, b in zip(stamps, stamps[1:])]
    ordered = sorted(intervals)
    if not ordered:
        return {"frame_count": len(stamps), "average_fps": len(stamps)/(end-start), "interval_rate_fps": 0.0, "one_percent_low_fps": 0.0,
                "p99_frame_ms": None, "max_frame_ms": None, "intervals_ms": []}
    slowest = ordered[-max(1, math.ceil(len(ordered)*0.01)):]
    budget = 1000.0/target_hz
    return {"frame_count": len(stamps), "interval_count": len(intervals),
            "average_fps": len(stamps)/(end-start),
            "interval_rate_fps": 1000.0/(sum(intervals)/len(intervals)),
            "paint_count_per_gesture_second": len(stamps)/(end-start),
            "one_percent_low_fps": 1000.0/(sum(slowest)/len(slowest)),
            "p99_frame_ms": ordered[min(len(ordered)-1, math.ceil(len(ordered)*0.99)-1)],
            "max_frame_ms": ordered[-1], "frame_budget_ms": budget,
            "intervals_over_budget": sum(t > budget*1.05 for t in intervals),
            "missed_refresh_slots": sum(max(0, math.floor(t/budget+0.5)-1) for t in intervals),
            "missed_refresh_slots_method": "Nearest refresh-period estimate; not verified physical scanout misses.",
            "first_frame_delay_ms": (stamps[0]-start)*1000.0,
            "last_frame_to_gesture_end_ms": (end-stamps[-1])*1000.0,
            "intervals_ms": intervals}


def cgroup_cpu_stat():
    path = Path("/sys/fs/cgroup/cpu.stat")
    try:
        return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def counter_delta(start, end):
    return {key: end[key]-value for key, value in start.items() if key in end}


def main():
    args = arguments()
    app_root = args.app_root.resolve()
    if not (app_root / "nightwatch" / "app" / "main_window.py").is_file():
        raise ValueError("--app-root must contain the nightwatch application package")
    sys.path.insert(0, str(app_root))
    # Mirror the primary launcher before any NumPy/pyqtgraph import.
    numeric_threads = os.environ.get("NIGHTWATCH_NUMERIC_THREADS", "1")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = numeric_threads
    # Never read or modify the user's settings, recorder DB, journals or credentials.
    scratch = tempfile.TemporaryDirectory(prefix="nightwatch-benchmark-")
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "COINGECKO_API_KEY"):
        os.environ.pop(name, None)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        os.environ[name] = scratch.name
    from PySide6 import QtCore, QtGui, QtWidgets, QtTest
    import PySide6
    import numpy as np
    import pyqtgraph as pg
    from nightwatch.constants import APP_NAME, ORG_NAME
    from nightwatch.entrypoint import _configure_chart_surface_format, AppComposition
    from nightwatch.utilities import load_app_fonts
    from nightwatch.models import Candle, SymbolRules
    from nightwatch.app import main_window
    from nightwatch.trading.gateway import TradingGateway
    from nightwatch.ui.panels import panel_ids
    from nightwatch.chart.analysis import shutdown_analysis
    from nightwatch import presentation

    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, scratch.name)
    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    settings.setValue("testing/chart_opengl_v2", not args.raster)
    settings.setValue("right_layout_preset", "Balanced")
    request = _configure_chart_surface_format()
    QtWidgets.QApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings, True)
    app = QtWidgets.QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setProperty("nightwatchChartOpenGLRequestedAtStartup", request["requested"])
    app.setProperty("nightwatchChartOpenGLRequestedFormat", request["format"])
    load_app_fonts(app, str(app_root / "nightwatch"))
    pg.setConfigOptions(antialias=True)

    class OfflineTradingGateway(TradingGateway):
        def arm(self, *a, **kw):
            raise RuntimeError("Trading is disabled in the interaction benchmark")
        def _open_trade_socket(self, *a, **kw):
            raise RuntimeError("Network is disabled in the interaction benchmark")
        def _resume_user_stream(self, *a, **kw):
            raise RuntimeError("Network is disabled in the interaction benchmark")

    class OfflineComposition(AppComposition):
        def create_trading_gateway(self):
            return OfflineTradingGateway(self.testnet, self.parent)
        def create_database(self):
            raise RuntimeError("Database is disabled in the interaction benchmark")
        def create_market_data_hub(self, db):
            raise RuntimeError("Network is disabled in the interaction benchmark")
        def create_chart_market_data_hub(self, parent):
            raise RuntimeError("Auxiliary feeds are disabled in the interaction benchmark")

    # Composition substitution retains the actual widget tree and production render paths.
    main_window.AppComposition = OfflineComposition

    class OfflineWindow(main_window.MainWindow):
        def _run_deferred_startup_stage(self):
            self._configure_crisp_ui()
        def _refresh_watchlist_hour_changes(self, *a, **kw):
            pass
        def _request_coin_icon(self, *a, **kw):
            pass
        def _request_older_chart_history(self, *a, **kw):
            pass
        def _record_market_event(self, *a, **kw):
            pass
        def _fit_normal_window_to_work_area(self):
            pass

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cases = [s.strip() for s in args.cases.split(",") if s.strip()]
    allowed_cases = {"pan", "zoom", "deep_pan", "wide_pan", "history_traverse", "watchlist_resize", "rail_resize"}
    if not symbols or set(cases)-allowed_cases:
        raise ValueError("Supply symbols and valid case names")
    window = OfflineWindow(symbol=symbols[0], interval="1m", testnet=True)
    window.start_fullscreen = False
    window.start_maximized = False
    window.resize(args.window_width, args.window_height)
    window.chart.set_indicators_enabled(False)
    window.watchlist.set_icon_requests_enabled(False)
    pairs = ["BTCUSDT", "ETHUSDT", "SHIBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"]
    window.watchlist.set_symbols(pairs, emit=False)
    window.watchlist_sidebar.refresh()
    window.right_rail_controller.apply_preset("Balanced", window.right_layout_presets["Balanced"], reset_geometry=True)
    window.show()
    window.activateWindow()
    chart = window.chart
    viewport = chart.graphics.viewport()
    screen = window.screen()
    target_hz = args.refresh_hz or float(screen.refreshRate() or 60.0)
    results = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "label": args.label,
        "measurement": "Primary native FPS uses valid QOpenGLWidget frameSwapped; raster uses chart paint completions or watchlist paint entries. Qt composition completion is not verified physical scanout.",
        "configuration": {"symbols": symbols, "cases": cases, "history": args.history, "duration_s": args.duration,
            "app_source_root": str(app_root), "loaded_main_window": str(Path(main_window.__file__).resolve()),
            "numeric_threads": numeric_threads, "input_hz_requested": args.input_hz, "market_hz": args.market_hz, "live_candle_hz": args.live_candle_hz, "watchlist_pairs": pairs,
            "panel_ids": list(panel_ids(window.right_rail_controller.state.root)),
            "indicators_enabled": False, "offline": True, "data_workload": "Synthetic tickers/BBO at 20 Hz by default; 120 depth levels per side with 1k–10k quote notional per level; four 10k–100k quote trades per update; one display-only position; no network or order execution."},
        "environment": {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
            "python": platform.python_version(), "qt": QtCore.qVersion(), "pyside": PySide6.__version__,
            "numpy": np.__version__, "pyqtgraph": pg.__version__, "qt_platform": app.platformName(),
            "qt_screen": screen.name(), "screen_geometry": [screen.geometry().width(), screen.geometry().height()],
            "window_geometry": [window.width(), window.height()], "device_pixel_ratio": viewport.devicePixelRatioF(),
            "screen_refresh_hz": screen.refreshRate(), "target_refresh_hz": target_hz, "gl_request": request,
            "headless": app.platformName() in {"offscreen", "minimal"}, "target_hardware_validated": False,
            "screen_physical_mm": [screen.physicalSize().width(),screen.physicalSize().height()],
            "screen_logical_dpi": screen.logicalDotsPerInch(), "screen_physical_dpi": screen.physicalDotsPerInch(),
            "hardware_qualification": "Unverified: repeat on the target GPU/display; software/offscreen results cannot establish scanout performance."},
        "cases": []}
    if Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                results["environment"]["cpu"] = line.partition(":")[2].strip()
                break

    state = {"case": None, "paint_stamps": [], "swap_stamps": [], "widget_stamps": [],
             "input_stamps": [], "input_latencies_ms": [], "paint_start": None, "paint_durations_ms": [],
             "pending_inputs": [], "symbol_index": 0, "case_index": 0, "feed_index": 0,
             "gesture_start": None, "point": None, "target": None, "live_rows_max": 0,
             "snapshots_received": 0, "order_flow_failures": [], "last_received_snapshot": {},
             "capture_cpu_start": None, "live_fixture": None, "live_updates": 0, "live_stamp": 0}

    class PaintProbe(QtCore.QObject):
        def eventFilter(self, obj, event):
            if state["case"] and event.type() == QtCore.QEvent.Type.Paint:
                now = time.monotonic()
                if obj is viewport:
                    state["paint_start"] = now
                if obj is window.watchlist_sidebar.table.viewport():
                    state["widget_stamps"].append(now)
                    state["content_revision"] += 1
            return False
    def snapshot_received(payload):
        state["snapshots_received"] += 1
        frame = payload[0] if isinstance(payload, tuple) else payload
        snapshot = getattr(frame, "snapshot", frame)
        state["last_received_snapshot"] = {"symbol": snapshot.symbol, "sequence": snapshot.sequence,
            "ready": snapshot.ready, "bid_levels": len(snapshot.bid_levels), "ask_levels": len(snapshot.ask_levels)}
    window.order_flow_snapshot_ready.connect(snapshot_received)
    window._order_flow_runtime.failed.connect(lambda generation, message: state["order_flow_failures"].append(
        {"generation": generation, "message": message}))
    probe = PaintProbe(app)
    viewport.installEventFilter(probe)
    window.watchlist_sidebar.table.viewport().installEventFilter(probe)
    state["content_revision"] = 0
    state["composed_revision"] = 0

    def painted():
        if not state["case"]:
            return
        now = time.monotonic()
        state["paint_stamps"].append(now)
        rendered_window = chart.rendered_window
        if rendered_window != state["last_rendered_window"]:
            state["rendered_window_commits"].append({"at_gesture_ms": (now-state["gesture_start"])*1000.0,
                "window": list(rendered_window) if rendered_window is not None else None})
            state["last_rendered_window"] = rendered_window
        state["content_revision"] += 1
        if state["paint_start"] is not None:
            state["paint_durations_ms"].append((now-state["paint_start"])*1000.0)
            state["paint_start"] = None
        state["input_latencies_ms"].extend((now-t)*1000.0 for t in state["pending_inputs"])
        state["pending_inputs"].clear()
        state["live_rows_max"] = max(state["live_rows_max"], len(chart.live_candle.pixel_batch.data))
    chart.graphics.frame_presented.connect(painted)
    def swapped():
        if state["case"] and state["content_revision"] > state["composed_revision"]:
            state["swap_stamps"].append(time.monotonic())
            state["composed_revision"] = state["content_revision"]
    swap_signal = getattr(viewport, "frameSwapped", None)
    if swap_signal is not None:
        swap_signal.connect(swapped)
    results["environment"]["swap_signal_available"] = swap_signal is not None

    def bind_viewport(new_viewport):
        nonlocal viewport, swap_signal
        if viewport is new_viewport:
            return
        try:
            viewport.removeEventFilter(probe)
            if swap_signal is not None:
                swap_signal.disconnect(swapped)
        except RuntimeError:
            pass
        viewport = new_viewport
        viewport.installEventFilter(probe)
        swap_signal = getattr(viewport,"frameSwapped",None)
        if swap_signal is not None:
            swap_signal.connect(swapped)
        results["environment"]["swap_signal_available"] = swap_signal is not None
    chart.render_surface_changed.connect(bind_viewport)

    def price_for(symbol):
        return {"BTCUSDT": 65000.0, "ETHUSDT": 3200.0, "SHIBUSDT": 0.000017}.get(symbol, 12.5)

    def tick_for(symbol):
        # Match the representative exchange price grids, including low-price
        # symbols; arbitrary relative ticks can fall below backend precision.
        return {"BTCUSDT": 0.10, "ETHUSDT": 0.01, "SHIBUSDT": 0.00000001}.get(symbol, 0.001)

    def market_update():
        state["feed_index"] += 1
        i = state["feed_index"]
        window.watchlist.update_tickers([{"s": s, "c": price_for(s)*(1+0.0001*math.sin(i*.17+j)),
            "P": 1.1+j*.15+0.01*math.cos(i*.1), "q": 10000000+j*1250000} for j,s in enumerate(pairs)])
        price = price_for(window.current_symbol)
        tick = tick_for(window.current_symbol)
        bids = tuple((price-j*tick,(1000+1000*((i+j)%10))/(price-j*tick)) for j in range(1,121))
        asks = tuple((price+j*tick,(1000+1000*((i+2*j)%10))/(price+j*tick)) for j in range(1,121))
        window._order_flow_depth_requested.emit(window._order_flow_generation, bids, asks, i, None)
        window._order_flow_book_ticker_requested.emit(window._order_flow_generation,{"s":window.current_symbol,"u":i,
            "b":bids[0][0],"a":asks[0][0],"B":bids[0][1],"A":asks[0][1]})
        trades = tuple({"s": window.current_symbol, "p": price+(j%3-1)*tick,
            "q": (100000 if j == 3 else 10000+2500*((i+j)%3))/(price+(j%3-1)*tick),
            "m": bool((i+j)%2), "a": i*4+j, "T": 1800000000000+i*50+j} for j in range(4))
        window._order_flow_trade_batch_requested.emit(window._order_flow_generation, trades)
    feed = QtCore.QTimer(app)
    feed.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
    feed.timeout.connect(market_update)
    if args.market_hz:
        feed.start(max(1, round(1000/args.market_hz)))
    market_update()

    def live_candle_update():
        fixture = state["live_fixture"]
        if fixture is None or chart._snapshot_inflight or chart._matrix_waiting or not chart.candles:
            return
        fixture["index"] += 1
        i = fixture["index"]
        base = fixture["base"]
        close = base.close*(1+0.0008*math.sin(i*.23))
        excursion = base.close*0.0003*abs(math.sin(i*.17))
        fixture["high"] = max(fixture["high"],base.high+excursion,base.open,close)
        fixture["low"] = min(fixture["low"],base.low-excursion,base.open,close)
        volume = base.volume+min(20.0,i*.2)
        quote_volume = base.quote_volume+(volume-base.volume)*base.close
        state["live_stamp"] = max(state["live_stamp"]+max(1,round(1000/args.live_candle_hz)),
            int(base.time*1000)+i, int(chart._last_kline_event)+1)
        before_count, before_revision = len(chart.candles), chart._live_data_revision
        _, closed = chart.update_kline({"e":"kline", "s":window.current_symbol, "E":state["live_stamp"],
            "k":{"s":window.current_symbol, "i":"1m", "t":int(base.time*1000), "x":False,
                "o":str(base.open), "h":str(fixture["high"]), "l":str(fixture["low"]),
                "c":str(close), "v":str(volume), "q":str(quote_volume)}})
        if closed or len(chart.candles) != before_count or chart.candles[-1].time != base.time:
            raise RuntimeError("Offline live-candle fixture closed or appended a row")
        if chart._live_data_revision <= before_revision:
            raise RuntimeError("Offline mutable-candle update was not adopted")
        state["live_updates"] += 1
    live_feed = QtCore.QTimer(app)
    live_feed.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
    live_feed.timeout.connect(live_candle_update)
    if args.live_candle_hz:
        live_feed.start(max(1,round(1000/args.live_candle_hz)))

    def mouse(kind, obj, point, held=False):
        if kind == QtCore.QEvent.Type.MouseButtonPress:
            QtTest.QTest.mousePress(obj,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier,point,0)
            return
        if kind == QtCore.QEvent.Type.MouseButtonRelease:
            QtTest.QTest.mouseRelease(obj,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier,point,0)
            return
        button = QtCore.Qt.MouseButton.NoButton
        buttons = QtCore.Qt.MouseButton.LeftButton if held else QtCore.Qt.MouseButton.NoButton
        pos = QtCore.QPointF(point)
        event = QtGui.QMouseEvent(kind, pos, QtCore.QPointF(obj.mapToGlobal(point)), button, buttons,
            QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(obj, event)

    def tick():
        now = time.monotonic()
        elapsed = now-state["gesture_start"]
        if elapsed >= args.duration:
            finish_case()
            return
        case = state["case"]
        obj = state["target"]
        point = state["point"]
        state["input_stamps"].append(now)
        state["pending_inputs"].append(now)
        if case == "zoom":
            phase = int(elapsed/0.75)%2
            event = QtGui.QWheelEvent(QtCore.QPointF(point), QtCore.QPointF(obj.mapToGlobal(point)),
                QtCore.QPoint(), QtCore.QPoint(0, 8 if phase == 0 else -8),
                QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                QtCore.Qt.ScrollPhase.NoScrollPhase, False)
            QtWidgets.QApplication.sendEvent(obj, event)
        elif case == "history_traverse":
            cycle = int(elapsed/0.4)
            if cycle != state["swipe_index"]:
                # Complete the previous real drag, release, reposition the
                # unheld cursor and press again. Only input moves the view.
                finish_global = state["start_global"]+QtCore.QPoint(state["amplitude"],0)
                mouse(QtCore.QEvent.Type.MouseMove,obj,obj.mapFromGlobal(finish_global),held=True)
                mouse(QtCore.QEvent.Type.MouseButtonRelease,obj,obj.mapFromGlobal(finish_global),held=False)
                mouse(QtCore.QEvent.Type.MouseMove,obj,obj.mapFromGlobal(state["start_global"]),held=False)
                chart.graphics.flush_pointer_motion()
                mouse(QtCore.QEvent.Type.MouseButtonPress,obj,obj.mapFromGlobal(state["start_global"]),held=True)
                state["swipe_index"] = cycle
                state["swipes_started"] += 1
            offset = (elapsed/0.4-cycle)*state["amplitude"]
            moved_global = state["start_global"]+QtCore.QPoint(round(offset),0)
            state["latest_global"] = moved_global
            mouse(QtCore.QEvent.Type.MouseMove,obj,obj.mapFromGlobal(moved_global),held=True)
        else:
            # Triangular continuous motion avoids discontinuous cursor jumps.
            fraction = (elapsed/1.2)%2.0
            offset = (fraction if fraction <= 1 else 2-fraction)*state["amplitude"]
            moved_global = state["start_global"]+QtCore.QPoint(round(offset) if state["horizontal"] else 0,
                                      0 if state["horizontal"] else round(offset))
            moved = obj.mapFromGlobal(moved_global)
            state["latest_global"] = moved_global
            state["latest_point"] = moved
            mouse(QtCore.QEvent.Type.MouseMove, obj, moved, held=True)

    gesture = QtCore.QTimer(app)
    gesture.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
    gesture.timeout.connect(tick)
    gesture.setInterval(max(1, round(1000/args.input_hz)))

    def finish_case():
        gesture.stop()
        case = state["case"]
        obj = state["target"]
        # A real release flushes any coalesced final pointer sample.
        if case != "zoom":
            mouse(QtCore.QEvent.Type.MouseButtonRelease, obj, obj.mapFromGlobal(state["latest_global"]), held=False)
        state["gesture_ended_at"] = time.monotonic()
        state["live_updates_end"] = state["live_updates"]
        state["case_cpu_end"] = cgroup_cpu_stat()
        QtCore.QTimer.singleShot(250, finalize_case)

    def depth_observations():
        canvas = window.orderbook.canvas
        source = getattr(window.orderbook, "_latest_snapshot", None)
        frame = getattr(canvas, "_display_frame", None)
        if isinstance(frame, dict):
            # The process renderer owns the actual snapshot. The GUI stores
            # the adopted image's visible prices, sequence and geometry.
            asks, bids = frame.get("prices", ((), ()))
            displayed_ready = bool(asks and bids and frame.get("sequence", 0) > 0)
            displayed_sequence = frame.get("sequence")
            observation_source = "adopted_process_raster_visible_prices"
        else:
            snapshot = getattr(canvas, "snapshot", None)
            bids = getattr(snapshot, "bid_levels", ())
            asks = getattr(snapshot, "ask_levels", ())
            displayed_ready = bool(getattr(snapshot, "ready", False))
            displayed_sequence = getattr(snapshot, "sequence", None)
            observation_source = "direct_canvas_snapshot"
        return {"depth_observation_source": observation_source,
            "depth_bid_levels": len(bids), "depth_ask_levels": len(asks),
            "depth_ready": displayed_ready, "displayed_depth_sequence": displayed_sequence,
            "source_depth_bid_levels": len(getattr(source, "bid_levels", ())),
            "source_depth_ask_levels": len(getattr(source, "ask_levels", ())),
            "source_depth_ready": bool(getattr(source, "ready", False)),
            "dom_tick": canvas.price_tick_size}

    def finalize_case():
        case = state["case"]
        end = state["gesture_ended_at"]
        stamps = [t for t in state["paint_stamps"] if t <= end]
        tail_stamps = [t for t in state["paint_stamps"] if t > end]
        latency = sorted(state["input_latencies_ms"])
        profile = presentation._performance_diagnostic_summary()
        item = {"symbol": window.current_symbol, "interaction": case, "elapsed_s": end-state["gesture_start"],
            "inputs_sent": len(state["input_stamps"]), "actual_input_hz": len(state["input_stamps"])/(end-state["gesture_start"]),
            "live_candle_updates_during_gesture": state["live_updates_end"]-state["live_updates_start"],
            "candle_count_start": state["candle_count_start"], "candle_count_end": len(chart.candles),
            "cgroup_cpu_stat_delta": counter_delta(state["case_cpu_start"],state["case_cpu_end"]),
            "chart_paints": interval_summary(stamps,state["gesture_start"],end,target_hz),
            "qt_compositions": interval_summary([t for t in state["swap_stamps"] if t <= end],state["gesture_start"],end,target_hz),
            "watchlist_paint_entries": interval_summary([t for t in state["widget_stamps"] if t <= end],state["gesture_start"],end,target_hz),
            "input_to_next_chart_paint_complete_ms": {"sample_count": len(latency), "coalesced_inputs_included": True,
                "unpainted_input_count": len(state["pending_inputs"]), "p99": latency[min(len(latency)-1,math.ceil(len(latency)*.99)-1)] if latency else None,
                "max": latency[-1] if latency else None, "average": sum(latency)/len(latency) if latency else None},
            "live_batch_max_rows": state["live_rows_max"], "visible_x_range": chart.price_plot.viewRange()[0],
            "rendered_window_start": state["rendered_window_start"],
            "rendered_window_end": chart.rendered_window,
            "rendered_window_commits": state["rendered_window_commits"],
            "history_preparation_commits_during_gesture": sum(entry["at_gesture_ms"] <= (end-state["gesture_start"])*1000.0 for entry in state["rendered_window_commits"]),
            "swipes_started": state["swipes_started"] if case == "history_traverse" else None,
            "view_range_start": state["view_range_start"], "view_range_changed": chart.price_plot.viewRange() != state["view_range_start"],
            "resize_mode": "forced_opaque_content_resize" if "resize" in case else None,
            "watchlist_geometry_start": state["watchlist_geometry_start"], "watchlist_geometry_end": [window.watchlist_sidebar.width(),window.watchlist_sidebar.height()],
            "paint_durations_ms": state["paint_durations_ms"],
            "diagnostics_capture_includes_release_tail_ms": 250, "diagnostics": profile,
            "renderer_state": chart.diagnostic_state(),
            "visible_panels": {pid: window.right_rail_controller.panel_active(pid) for pid in ("depth","trades","watchlist","trading")},
            "watchlist_row_count": window.watchlist_sidebar.table.rowCount(),
            "fixture_observations": {"market_updates": state["feed_index"]-state["feed_index_start"],
                "order_flow_snapshots_received": state["snapshots_received"]-state["snapshots_received_start"],
                **depth_observations(),
                "tape_visible_rows": window.large_trades.model.rowCount(),
                "tape_mode": window.large_trades.mode(),
                "display_position_count": window.trading_workspace._position_count,
                "trading_armed": window.trading_gateway.armed,
                "last_received_snapshot": state["last_received_snapshot"],
                "orderbook_symbol": window.orderbook.symbol, "canvas_symbol": window.orderbook.canvas.symbol,
                "canvas_latest_received_sequence": window.orderbook.canvas._latest_received_sequence,
                "order_flow_failures": state["order_flow_failures"]},
            "release_first_chart_paint_ms": (tail_stamps[0]-end)*1000 if tail_stamps else None,
            "release_chart_paints": len(tail_stamps),
            "release_watchlist_paints": sum(t > end for t in state["widget_stamps"])}
        if not all(item["visible_panels"].values()) or item["watchlist_row_count"] != 10:
            raise RuntimeError("Benchmark configuration lost one of four panels or ten pairs")
        if case in {"pan","deep_pan","wide_pan","history_traverse"} and abs(item["visible_x_range"][0]-item["view_range_start"][0][0]) < 1:
            raise RuntimeError("Synthetic pan did not move the chart range")
        if case == "history_traverse" and item["history_preparation_commits_during_gesture"] <= 0:
            raise RuntimeError("History traversal did not paint a newly prepared residency window during the gesture; increase duration")
        if args.live_candle_hz and (item["live_candle_updates_during_gesture"] <= 0 or item["candle_count_start"] != item["candle_count_end"]):
            raise RuntimeError("Mutable-candle fixture did not update inside the gesture without appending rows")
        if "resize" in case and item["watchlist_geometry_start"] == item["watchlist_geometry_end"]:
            raise RuntimeError("Synthetic content resize did not change visible geometry")
        fixtures = item["fixture_observations"]
        if args.market_hz > 0 and (fixtures["order_flow_snapshots_received"] <= 0 or not fixtures["depth_ready"]
                or min(fixtures["depth_bid_levels"],fixtures["depth_ask_levels"],fixtures["tape_visible_rows"],fixtures["display_position_count"]) <= 0
                or fixtures["trading_armed"]):
            raise RuntimeError(f"Populated offline fixture did not reach visible widgets: {fixtures}")
        valid_gl_viewport = swap_signal is not None and callable(getattr(viewport, "isValid", None)) and viewport.isValid()
        item["primary_surface"] = "qt_compositions" if valid_gl_viewport else "watchlist_paint_entries" if case.startswith("watchlist") else "chart_paints"
        results["cases"].append(item)
        state["case"] = None
        primary = item[item["primary_surface"]]
        print(f"{item['symbol']:10} {case:18} surface={item['primary_surface']:23} frames={primary['frame_count']:5} avg={primary['average_fps']:7.1f} low1%={primary['one_percent_low_fps']:7.1f}", flush=True)
        state["case_index"] += 1
        QtCore.QTimer.singleShot(350, prepare_case)

    def begin_case():
        case = cases[state["case_index"]]
        for key in ("paint_stamps","swap_stamps","widget_stamps","input_stamps","input_latencies_ms","paint_durations_ms","pending_inputs"):
            state[key] = []
        state["live_rows_max"] = 0
        state["content_revision"] = 0
        state["composed_revision"] = 0
        state["paint_start"] = None
        state["rendered_window_commits"] = []
        state["rendered_window_start"] = chart.rendered_window
        state["last_rendered_window"] = chart.rendered_window
        state["swipe_index"] = 0
        state["swipes_started"] = 1
        obj = viewport
        scene = chart.price_plot.getViewBox().sceneBoundingRect().center()
        point = chart.graphics.mapFromScene(scene)
        horizontal = True
        amplitude = max(60,min(230,viewport.width()//3))
        if case == "rail_resize":
            obj = window.main_splitter.handle(1)
            point = obj.rect().center()
            amplitude = -100
        elif case == "watchlist_resize":
            splitters = list(window.right_rail_controller.interaction_splitters())
            vertical = [s for s in splitters if s.orientation() == QtCore.Qt.Orientation.Vertical and s.count() >= 2
                        and s.isAncestorOf(window.watchlist_sidebar)]
            if not vertical:
                raise RuntimeError("Visible watchlist splitter not found")
            obj = vertical[0].handle(1)
            point = obj.rect().center()
            horizontal = False
            amplitude = 80
        if "resize" in case:
            obj.splitter().setOpaqueResize(True)
        state["feed_index_start"] = state["feed_index"]
        state["live_updates_start"] = state["live_updates"]
        state["candle_count_start"] = len(chart.candles)
        state["snapshots_received_start"] = state["snapshots_received"]
        state["view_range_start"] = chart.price_plot.viewRange()
        state["watchlist_geometry_start"] = [window.watchlist_sidebar.width(),window.watchlist_sidebar.height()]
        state["latest_point"] = point
        state["start_global"] = obj.mapToGlobal(point)
        state["latest_global"] = state["start_global"]
        state["case_cpu_start"] = cgroup_cpu_stat()
        if state["capture_cpu_start"] is None:
            state["capture_cpu_start"] = state["case_cpu_start"]
        state.update(case=case, target=obj, point=point, horizontal=horizontal, amplitude=amplitude,
                     gesture_start=time.monotonic())
        chart._presentation_clock.start_frame_profile(args.duration+0.5)
        if case != "zoom":
            mouse(QtCore.QEvent.Type.MouseMove,obj,point,held=False)
            chart.graphics.flush_pointer_motion()
            mouse(QtCore.QEvent.Type.MouseButtonPress,obj,point,held=True)
        gesture.start()

    def prepare_case():
        if state["case_index"] >= len(cases):
            state["symbol_index"] += 1
            state["case_index"] = 0
            load_symbol()
            return
        case = cases[state["case_index"]]
        candles = chart.candles
        end_index = args.history//2 if case in {"deep_pan","history_traverse"} else len(candles)-1
        visible = 1000 if case == "wide_pan" else 170
        chart.price_plot.setXRange(candles[end_index-visible].time,candles[end_index].time+600,padding=0)
        if chart.auto_scale:
            chart._fit_y_to_visible(*chart.price_plot.viewRange()[0])
        window.right_rail_controller.apply_preset("Balanced",window.right_layout_presets["Balanced"],reset_geometry=True)
        QtCore.QTimer.singleShot(500,begin_case)

    def loaded(*_):
        base = chart.candles[-1]
        state["live_fixture"] = {"base":base, "high":base.high, "low":base.low, "index":0}
        QtCore.QTimer.singleShot(750,prepare_case)
    chart.snapshot_committed.connect(loaded)

    def load_symbol():
        if state["symbol_index"] >= len(symbols):
            capture_cpu_end = cgroup_cpu_stat()
            cpu_max_path = Path("/sys/fs/cgroup/cpu.max")
            results["environment"]["cgroup_cpu_stat"] = {
                "path": "/sys/fs/cgroup/cpu.stat", "cpu_max": cpu_max_path.read_text().strip() if cpu_max_path.is_file() else None,
                "start": state["capture_cpu_start"], "end": capture_cpu_end,
                "delta": counter_delta(state["capture_cpu_start"] or {},capture_cpu_end)}
            loaded_files = {}
            for name, module in tuple(sys.modules.items()):
                filename = getattr(module, "__file__", None)
                if name == "nightwatch" or name.startswith("nightwatch."):
                    if filename is not None:
                        path = Path(filename).resolve()
                        if not path.is_relative_to(app_root):
                            raise RuntimeError(f"Application source escaped --app-root: {name}: {path}")
                        loaded_files[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            results["configuration"]["loaded_application_files"] = loaded_files
            results["environment"]["renderer_state"] = chart.diagnostic_state()
            results["environment"]["chart_viewport_type"] = type(chart.graphics.viewport()).__name__
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(results,indent=2)+"\n")
            print(f"Saved {args.output}",flush=True)
            begin_shutdown()
            return
        symbol = symbols[state["symbol_index"]]
        state["live_fixture"] = None
        window.current_symbol = symbol
        chart.prepare_market("1m",reset_analysis=True)
        chart.set_order_rail_market_symbol(symbol)
        price = price_for(symbol)
        tick = tick_for(symbol)
        rules = SymbolRules(tick_size=str(tick))
        chart.set_symbol_rules(rules)
        window.orderbook.set_symbol(symbol)
        window.orderbook.set_symbol_rules(rules)
        window.large_trades.set_market(symbol,tick_size=tick)
        window._reset_order_flow_runtime(tick_size=tick,quote_volume=1_000_000_000.0)
        window.trading_workspace.apply_snapshot({"account":{"availableBalance":"10000", "positions":[
            {"symbol":symbol,"positionAmt":"0.5","entryPrice":str(price*.99),"markPrice":str(price),
             "unrealizedProfit":str(price*.005),"leverage":"5","positionSide":"BOTH","marginType":"cross","liquidationPrice":"0"}]},
            "orders":[],"ordersScope":"ALL"},mark_fresh=False)
        rows = []
        for i in range(args.history):
            opened = price*(1+0.015*math.sin(i*.031)+0.007*math.cos(i*.11))
            closed = opened*(1+0.0015*math.sin(i*.53))
            high = max(opened,closed)+price*.0011
            low = min(opened,closed)-price*.0011
            volume = 10.0+(i*17)%103
            rows.append(Candle(1800000000+i*60,opened,high,low,closed,volume,volume*closed))
        chart.set_snapshot({"interval":"1m","candles":rows,"_history_exhausted":True})

    def begin_shutdown(exit_status=0):
        # Keep servicing deferred MainWindow close and IPC cleanup. A fixed
        # app.quit() delay can race transport finalizers or process atexit.
        from nightwatch.orderbook.backend import _OrderBookProcessLink
        feed.stop()
        live_feed.stop()
        gesture.stop()
        app.setQuitOnLastWindowClosed(False)
        runtime = window._order_flow_runtime
        links = window.findChildren(_OrderBookProcessLink) + runtime.findChildren(_OrderBookProcessLink)
        links = list(dict.fromkeys(links))
        window.close()
        for link in links:
            link.close()
        deadline = time.monotonic()+5.0

        def poll_shutdown():
            runtime_running = window._order_flow_runtime_thread.isRunning()
            transports_running = any(getattr(link, "_thread", None) is not None
                and link._thread.is_alive() for link in links)
            if not runtime_running and not window.isVisible() and not transports_running:
                app.exit(exit_status)
            elif time.monotonic() >= deadline:
                print("Benchmark cleanup deadline reached.",flush=True)
                app.exit(exit_status)
            else:
                QtCore.QTimer.singleShot(25,poll_shutdown)
        QtCore.QTimer.singleShot(0,poll_shutdown)

    def failed(exc_type, value, traceback):
        import traceback as tb
        tb.print_exception(exc_type,value,traceback)
        state["error"] = str(value)
        begin_shutdown(1)
    sys.excepthook = failed
    QtCore.QTimer.singleShot(750,load_symbol)
    status = app.exec()
    shutdown_analysis()
    scratch.cleanup()
    return 1 if state.get("error") else status


if __name__ == "__main__":
    raise SystemExit(main())
