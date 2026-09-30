"""Isolated, offline Qt shell/navigation/shutdown smoke check."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def main():
    from PySide6 import QtCore, QtWidgets
    from nightwatch.constants import APP_NAME, ORG_NAME
    from nightwatch.networking.binance import BinanceRest
    from nightwatch.trading.gateway import TradingGateway
    from nightwatch.orderbook.backend import _OrderBookProcessLink
    from nightwatch.utilities import load_app_fonts

    root = tempfile.mkdtemp(prefix='nightwatch-smoke-')
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, root)
    QtCore.QStandardPaths.setTestModeEnabled(True)
    app = QtWidgets.QApplication([])
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    settings.clear()
    settings.setValue('testing/chart_opengl_v2', False)
    load_app_fonts(app, str(Path(__file__).resolve().parents[1] / 'nightwatch'))
    BinanceRest._ensure_time_sync_loop = lambda self: None
    TradingGateway._restore_placement_journal = lambda self: None
    _OrderBookProcessLink.enable = lambda *args: None

    from nightwatch.app.main_window import MainWindow
    MainWindow._run_deferred_startup_stage = lambda self: None
    window = MainWindow(testnet=True)
    window.start_fullscreen = False
    window.start_maximized = False
    window.resize(1280, 720)
    window.show()
    for index in range(window.workspace_stack.count()):
        window.workspace_stack.setCurrentIndex(index)
        for _ in range(5):
            app.processEvents()
            time.sleep(.01)
        capture = os.environ.get('NIGHTWATCH_SMOKE_CAPTURE')
        if capture:
            target = Path(capture)
            window.grab().save(str(target.with_name(target.stem + f'-{index}' + target.suffix)))
    capture = os.environ.get('NIGHTWATCH_SMOKE_CAPTURE')
    if capture:
        window.grab().save(capture)
    size = [window.width(), window.height()]
    available = window.screen().availableGeometry()
    assert window.height() <= available.height(), 'Window exceeds virtual screen height'
    window.close()
    deadline = time.monotonic() + 5
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not window.isVisible(), 'Window did not finish cooperative shutdown'
    assert not window._order_flow_runtime_thread.isRunning(), 'Qt analysis relay remained running'
    print(json.dumps({'shell': 'passed', 'navigation': window.workspace_stack.count(), 'shutdown': 'passed', 'size': size}))


if __name__ == '__main__':
    main()
