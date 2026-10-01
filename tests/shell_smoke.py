"""Isolated, offline Qt shell/navigation/shutdown smoke check."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def check_market_bar(window, app):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest
    from nightwatch.constants import DEFAULT_MARKET_BAR_TIMEFRAMES

    window.activateWindow()
    window.chart.setFocus()
    QTest.qWait(50)
    assert window.market_bar_timeframes == DEFAULT_MARKET_BAR_TIMEFRAMES
    assert window.instrument_bar.row.contentsMargins().left() == 8
    assert window.instrument_bar.row.contentsMargins().right() == 8
    assert window.instrument_bar.height() == 48
    assert window.stats.timeframe_selector.count() == 8
    QTest.keyClick(window.chart, QtCore.Qt.Key.Key_6)
    assert window.current_interval == '1d', 'Default key 6 must select 1D after removing 12h'

    custom = ('3m', '30m', '2h', '6h', '12h', '1d', '1w', '1M')
    window.set_market_bar_timeframes(custom)
    assert window.current_interval == '1d', 'Editing favorites must not change the chart'
    QTest.keyClick(window.chart, QtCore.Qt.Key.Key_1)
    assert window.current_interval == '3m', 'Number dispatch must follow the saved selection'
    QTest.keyClick(window.chart, QtCore.Qt.Key.Key_8)
    assert window.current_interval == '1M'

    window._show_settings_window('Chart & Indicators')
    app.processEvents()
    dialog = window.settings_dialog
    dialog._timeframe_presets['Default'].click()
    assert window.market_bar_timeframes == DEFAULT_MARKET_BAR_TIMEFRAMES
    dialog._timeframe_buttons['12h'].click()
    assert len(window.market_bar_timeframes) == 9
    assert not dialog._timeframe_buttons['3m'].isEnabled(), 'A tenth timeframe must not be selectable'
    assert window.market_bar_timeframes[5] == '12h'
    dialog._timeframe_buttons['12h'].click()
    window.set_market_bar_timeframes(('30m',))
    assert not dialog._timeframe_buttons['30m'].isEnabled(), 'The final selection must remain nonempty'
    assert window.current_interval == '1M'
    assert window.stats.timeframe_selector.currentData() == '1M', 'Current omitted interval must remain visible'
    dialog._timeframe_buttons['1m'].click()
    assert window.market_bar_timeframes == ('1m', '30m')
    assert tuple(json.loads(window.settings.value('ui/market_bar_timeframes_v1', '', str))) == ('1m', '30m')
    dialog._timeframe_presets['Default'].click()
    QTest.qWait(30)
    capture = os.environ.get('NIGHTWATCH_SMOKE_CAPTURE')
    if capture:
        target = Path(capture)
        dialog.grab().save(str(target.with_name(target.stem + '-settings' + target.suffix)))
        window.instrument_bar.grab().save(str(target.with_name(target.stem + '-market-bar' + target.suffix)))
    dialog.close()
    window.activateWindow()
    window.chart.setFocus()
    QTest.qWait(20)
    QTest.keyClick(window.chart, QtCore.Qt.Key.Key_6)
    assert window.current_interval == '1d'
    assert not window._informational_tooltip_allowed(QtWidgets.QLabel('Instruction', window))
    assert window._informational_tooltip_allowed(window.stats.cards['volume'])
    assert window._informational_tooltip_allowed(window.settings_button)


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
    try:
        check_market_bar(window, app)
        tape = window.large_trades
        tape.units_button.click()
        assert tape.value_mode() == 'base'
        assert window.settings.value('trades/value_mode_v1', '', str) == 'base'
        if window.orderbook._tape is not None:
            window.orderbook._tape.set_value_mode('base')
        window.orderbook.set_value_mode('quote', emit=False)
        assert tape.value_mode() == 'base'
        if window.orderbook._tape is not None:
            assert window.orderbook._tape.value_mode() == 'base'
        assert tape.table.horizontalHeader().isHidden()
        assert tape.model.columnCount() == 4

    except BaseException:
        window.close()
        deadline = time.monotonic() + 5
        while window._order_flow_runtime_thread.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        raise
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
    print(json.dumps({'shell': 'passed', 'market_bar': 'passed', 'navigation': window.workspace_stack.count(), 'shutdown': 'passed', 'size': size}))


if __name__ == '__main__':
    main()
