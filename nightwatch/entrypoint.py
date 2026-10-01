"""Command-line parsing and Qt application bootstrap."""

from __future__ import annotations

import argparse
import os
import sys
import time

# Keep native numerical pools bounded before importing pyqtgraph/NumPy.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = os.environ.get("NIGHTWATCH_NUMERIC_THREADS", "1")

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt

from .constants import APP_NAME, ORG_NAME, TIMEFRAMES
from .theme import DEFAULT_THEME_NAME, THEMES, chart_palette, ui_palette
from .utilities import load_app_fonts
from .utilities import ensure_frameless_close_button, position_frameless_close_button




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Binance USD-M futures charting terminal")
    parser.add_argument("--symbol", default=None, help="USD-M perpetual, e.g. BTCUSDT")
    parser.add_argument("--timeframe", choices=TIMEFRAMES, default=None)
    parser.add_argument("--theme", choices=tuple(THEMES), default=None)
    parser.add_argument("--testnet", action="store_true", help="Use Binance USD-M testnet endpoints")
    parser.add_argument(
        "--diagnostics", action="store_true",
        help="Enable Nightwatch diagnostics instrumentation and Diagnostics UI for this run",
    )
    return parser.parse_args()


def _configure_chart_surface_format() -> dict[str, object]:
    """Request the native GPU chart context and return the request for diagnostics."""
    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    requested = bool(settings.value("testing/chart_opengl_v2", True, bool))
    if requested:
        surface_format = QtGui.QSurfaceFormat.defaultFormat()
        renderable = surface_format.renderableType()
        if renderable == QtGui.QSurfaceFormat.RenderableType.OpenGLES:
            if (
                surface_format.majorVersion(),
                surface_format.minorVersion(),
            ) < (3, 0):
                surface_format.setVersion(3, 0)
        elif (
            surface_format.majorVersion(),
            surface_format.minorVersion(),
        ) < (3, 3):
            surface_format.setVersion(3, 3)
            surface_format.setProfile(
                QtGui.QSurfaceFormat.OpenGLContextProfile.NoProfile
            )
        if surface_format.alphaBufferSize() < 8:
            surface_format.setAlphaBufferSize(8)
        QtGui.QSurfaceFormat.setDefaultFormat(surface_format)
    else:
        surface_format = QtGui.QSurfaceFormat.defaultFormat()

    renderable = surface_format.renderableType()
    api = (
        "OpenGL ES"
        if renderable == QtGui.QSurfaceFormat.RenderableType.OpenGLES
        else "OpenGL"
    )
    requested_format = (
        f"{api} {surface_format.majorVersion()}.{surface_format.minorVersion()} "
        f"{getattr(surface_format.profile(), 'name', surface_format.profile())}"
    )
    return {
        "requested": requested,
        "format": requested_format if requested else "disabled",
    }


class _BorderlessWindowFilter(QtCore.QObject):
    """Apply one close/Escape/move contract to every frameless Nightwatch window.

    Nightwatch deliberately removes native title bars from dialogs.  That must not
    also remove the operating-system move affordance, so the top chrome strip of
    every frameless window behaves like a normal title bar while interactive
    controls inside that strip keep their own mouse handling.
    """

    DRAG_STRIP_HEIGHT = 52
    _DRAG_BLOCKERS = (
        QtWidgets.QAbstractButton,
        QtWidgets.QAbstractItemView,
        QtWidgets.QAbstractSlider,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QComboBox,
        QtWidgets.QLineEdit,
        QtWidgets.QMenuBar,
        QtWidgets.QPlainTextEdit,
        QtWidgets.QSizeGrip,
        QtWidgets.QTabBar,
        QtWidgets.QTextEdit,
    )

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._drag_window: QtWidgets.QWidget | None = None
        self._drag_offset = QtCore.QPoint()
        self._primary_window: QtWidgets.QWidget | None = None

    def set_primary_window(self, window: QtWidgets.QWidget) -> None:
        """Identify the one top-level window Escape must never close directly."""
        self._primary_window = window

    @staticmethod
    def _frameless_window(widget: QtCore.QObject | None) -> QtWidgets.QWidget | None:
        if not isinstance(widget, QtWidgets.QWidget):
            return None
        window = widget.window()
        if (
            isinstance(window, QtWidgets.QWidget)
            and window.isWindow()
            and bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        ):
            return window
        return None

    def _drag_allowed(
        self,
        watched: QtCore.QObject,
        window: QtWidgets.QWidget,
        global_pos: QtCore.QPoint,
    ) -> bool:
        local = window.mapFromGlobal(global_pos)
        if not (0 <= local.x() < window.width() and 0 <= local.y() < min(window.height(), self.DRAG_STRIP_HEIGHT)):
            return False
        widget = watched if isinstance(watched, QtWidgets.QWidget) else None
        while widget is not None and widget is not window:
            if isinstance(widget, self._DRAG_BLOCKERS):
                return False
            widget = widget.parentWidget()
        return True

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        event_type = event.type()
        if (
            event_type == QtCore.QEvent.Type.Polish
            and isinstance(watched, QtWidgets.QDialog)
            and watched.property("nightwatchFrameless") is True
        ):
            if not (watched.windowFlags() & Qt.WindowType.FramelessWindowHint):
                watched.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            ensure_frameless_close_button(watched)

        if isinstance(watched, QtWidgets.QWidget) and watched.isWindow():
            window = self._frameless_window(watched)
            if window is not None:
                if event_type in (
                    QtCore.QEvent.Type.Show,
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.WindowStateChange,
                ):
                    if window.property("nightwatchFrameless") is True:
                        ensure_frameless_close_button(window)
                    position_frameless_close_button(window)

        if event_type == QtCore.QEvent.Type.MouseButtonPress:
            window = self._frameless_window(watched)
            if (
                window is not None
                and getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton
            ):
                global_pos = event.globalPosition().toPoint()
                if self._drag_allowed(watched, window, global_pos):
                    handle = window.windowHandle()
                    if handle is not None:
                        try:
                            if handle.startSystemMove():
                                event.accept()
                                return True
                        except (AttributeError, RuntimeError):
                            pass


                    self._drag_window = window
                    self._drag_offset = global_pos - window.frameGeometry().topLeft()
                    event.accept()
                    return True

        if event_type == QtCore.QEvent.Type.MouseMove and self._drag_window is not None:
            buttons = getattr(event, "buttons", lambda: Qt.MouseButton.NoButton)()
            if not (buttons & Qt.MouseButton.LeftButton):


                self._drag_window = None
            else:
                try:
                    self._drag_window.move(
                        event.globalPosition().toPoint() - self._drag_offset
                    )
                except RuntimeError:
                    self._drag_window = None
                event.accept()
                return True

        if (
            event_type == QtCore.QEvent.Type.MouseButtonRelease
            and self._drag_window is not None
            and getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton
        ):
            self._drag_window = None
            event.accept()
            return True

        if event_type in (QtCore.QEvent.Type.ShortcutOverride, QtCore.QEvent.Type.KeyPress):
            key = getattr(event, "key", lambda: None)()
            modifiers = getattr(event, "modifiers", lambda: Qt.KeyboardModifier.NoModifier)()





            if (
                key == Qt.Key.Key_F11
                and modifiers == Qt.KeyboardModifier.NoModifier
                and self._primary_window is not None
                and self._primary_window._owns_keyboard()
            ):
                if event_type == QtCore.QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if getattr(event, "isAutoRepeat", lambda: False)():
                    event.accept()
                    return True
                action = getattr(self._primary_window, "fullscreen_action", None)
                if action is not None:
                    action.setChecked(not action.isChecked())
                    event.accept()
                    return True

            if key == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:



                if QtWidgets.QApplication.activePopupWidget() is not None:
                    return super().eventFilter(watched, event)





                window = QtWidgets.QApplication.activeWindow()
                if (
                    isinstance(window, QtWidgets.QWidget)
                    and window.isWindow()
                    and window is not self._primary_window
                ):
                    if event_type == QtCore.QEvent.Type.ShortcutOverride:
                        event.accept()
                        return True


                    window.close()
                    event.accept()
                    return True
        return super().eventFilter(watched, event)


def _install_exception_diagnostics(diagnostics) -> None:
    if diagnostics is None:
        return
    original_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, traceback) -> None:
        diagnostics.error(
            "APP",
            f"Uncaught {getattr(exc_type, '__name__', 'exception')}: {exc_value}",
        )
        original_hook(exc_type, exc_value, traceback)

    sys.excepthook = handle_exception


def main() -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    bootstrap_started = time.monotonic()
    arguments = parse_args()
    diagnostics = None
    if arguments.diagnostics:
        from .utilities import get_diagnostics
        diagnostics = get_diagnostics()

    def startup_mark(label: str) -> None:
        elapsed = (time.monotonic() - bootstrap_started) * 1000.0
        line = f"{label} · +{elapsed:.0f} ms"
        print(f"[Nightwatch startup] {line}")
        if diagnostics is not None:
            diagnostics.verbose("STARTUP", line)

    _install_exception_diagnostics(diagnostics)
    chart_gl_request = _configure_chart_surface_format()



    bootstrap_settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    bootstrap_theme_name = str(
        arguments.theme
        or bootstrap_settings.value("theme", DEFAULT_THEME_NAME, str)
        or DEFAULT_THEME_NAME
    )
    if bootstrap_theme_name not in THEMES:
        bootstrap_theme_name = DEFAULT_THEME_NAME
    bootstrap_theme = chart_palette(ui_palette(THEMES[bootstrap_theme_name]))
    pg.setConfigOptions(
        antialias=True,
        background=bootstrap_theme["bg"],
        foreground=bootstrap_theme["text"],
    )


    dpi_mode = bootstrap_settings.value("developer/typography_dpi_rounding_v1", "auto", str)
    policy_map = {
        "round": Qt.HighDpiScaleFactorRoundingPolicy.Round,
        "passthrough": Qt.HighDpiScaleFactorRoundingPolicy.PassThrough,
        "round_prefer_floor": Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor,
        "floor": Qt.HighDpiScaleFactorRoundingPolicy.Floor,
        "ceil": Qt.HighDpiScaleFactorRoundingPolicy.Ceil,
    }
    if dpi_mode == "auto" or dpi_mode not in policy_map:
        dpi_rounding_policy = (
            Qt.HighDpiScaleFactorRoundingPolicy.Round
            if sys.platform.startswith("win")
            else Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    else:
        dpi_rounding_policy = policy_map[dpi_mode]
    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(dpi_rounding_policy)
    QtWidgets.QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_DontUseNativeDialogs,
        True,
    )
    dont_native_siblings = getattr(
        Qt.ApplicationAttribute,
        "AA_DontCreateNativeWidgetSiblings",
        None,
    )
    if dont_native_siblings is not None:
        QtWidgets.QApplication.setAttribute(
            dont_native_siblings,
            True,
        )
    application = QtWidgets.QApplication(sys.argv)
    from .chart.analysis import shutdown_analysis
    application.aboutToQuit.connect(shutdown_analysis)
    application.setProperty("nightwatchStartupStartedMono", bootstrap_started)
    application.setProperty("nightwatchDiagnosticsEnabled", bool(arguments.diagnostics))
    application.setProperty(
        "nightwatchChartOpenGLRequestedAtStartup",
        bool(chart_gl_request["requested"]),
    )
    application.setProperty(
        "nightwatchChartOpenGLRequestedFormat",
        str(chart_gl_request["format"]),
    )
    borderless_window_filter = _BorderlessWindowFilter(application)
    application.installEventFilter(borderless_window_filter)
    application.setApplicationName(APP_NAME)
    application.setOrganizationName(ORG_NAME)
    application.setStyle("Fusion")
    startup_mark("QApplication ready")
    package_root = os.path.dirname(os.path.abspath(__file__))
    try:
        load_app_fonts(application, package_root)
    except Exception as exc:
        QtWidgets.QMessageBox.critical(None, "Nightwatch · Fonts", str(exc))
        return 1
    startup_mark("fonts registered")



    from .app.main_window import MainWindow
    startup_mark("modules imported")

    window_started = time.perf_counter()
    window = MainWindow(
        symbol=arguments.symbol,
        interval=arguments.timeframe,
        theme_name=arguments.theme,
        testnet=arguments.testnet,
        diagnostics=diagnostics,
    )
    if diagnostics is not None:
        diagnostics.observe_ms(
            "startup.main_window_init_ms",
            (time.perf_counter() - window_started) * 1000.0,
        )
    borderless_window_filter.set_primary_window(window)
    startup_mark("MainWindow constructed")
    def start_market_data_when_chart_ready(attempt: int = 0) -> None:



        if attempt > 0 and attempt % 4 == 0:
            window.chart.prime_render_surface()
        if window.chart.render_surface_ready():
            window.start_market_data()
            return
        if attempt >= 120:


            window.start_market_data()
            return
        QTimer.singleShot(
            25,
            lambda next_attempt=attempt + 1: start_market_data_when_chart_ready(
                next_attempt
            ),
        )

    if window.start_fullscreen:




        window.show()

        def enter_fullscreen_then_start() -> None:
            window.fullscreen_action.setChecked(True)
            QTimer.singleShot(0, start_market_data_when_chart_ready)

        QTimer.singleShot(0, enter_fullscreen_then_start)
    elif window.start_maximized:
        window.showMaximized()
        QTimer.singleShot(0, start_market_data_when_chart_ready)
    else:
        window.show()
        QTimer.singleShot(0, start_market_data_when_chart_ready)
    startup_mark("window show requested")
    return application.exec()


import os
from dataclasses import dataclass
from typing import Any

from .models import DiagnosticsPort, MarketDataHubPort, TradingGatewayPort
from .database import AppDatabase
from .utilities import application_data_directory


@dataclass(slots=True)
class AppComposition:
    parent: Any
    testnet: bool
    diagnostics: DiagnosticsPort | None = None

    def create_database(self) -> AppDatabase:
        return AppDatabase(
            os.path.join(application_data_directory(), "nightwatch-testnet.sqlite3" if self.testnet else "nightwatch-mainnet.sqlite3")
        )

    def create_trading_gateway(self) -> TradingGatewayPort:
        from .trading.gateway import TradingGateway

        return TradingGateway(self.testnet, self.parent, diagnostics=self.diagnostics)

    def create_market_data_hub(self, db: AppDatabase) -> MarketDataHubPort:
        from .market.data import MarketDataHub

        return MarketDataHub(self.testnet, self.parent, db=db, diagnostics=self.diagnostics)

    def create_chart_market_data_hub(self, parent: Any) -> MarketDataHubPort:
        """Create an isolated chart-only feed behind the chart data port."""
        from .market.data import MarketDataHub

        return MarketDataHub(self.testnet, parent, chart_only=True, diagnostics=self.diagnostics)
