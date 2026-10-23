#!/usr/bin/env python3
"""Offline Qt checks for the replacement indicator catalog and settings migration."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from nightwatch.constants import (
    APP_NAME, ORG_NAME, DEFAULT_INDICATOR_SHORTCUTS,
    INDICATOR_GROUPS, INDICATOR_KEYS, INDICATOR_SETTING_DEFAULTS,
)
from nightwatch.ui.dialogs import IndicatorSettingsDialog, IndicatorShortcutsDialog


class IndicatorSettingsChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="nightwatch-indicator-settings-")
        cls.previous_paths = {
            name: os.environ.get(name)
            for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
        }
        for name in cls.previous_paths:
            os.environ[name] = cls.scratch.name
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
        QtCore.QSettings.setPath(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            cls.scratch.name,
        )
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle("Fusion")
        cls.app.setApplicationName(APP_NAME)
        cls.app.setOrganizationName(ORG_NAME)
        cls.app.setProperty("nightwatchChartOpenGLRequestedAtStartup", False)
        cls.qt_errors = []
        cls.previous_excepthook = sys.excepthook
        sys.excepthook = lambda kind, value, trace: cls.qt_errors.append(value)

    @classmethod
    def tearDownClass(cls):
        analysis = sys.modules.get("nightwatch.chart.analysis")
        if analysis is not None:
            analysis.shutdown_analysis()
        sys.excepthook = cls.previous_excepthook
        for name, value in cls.previous_paths.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cls.scratch.cleanup()

    def setUp(self):
        self.qt_errors.clear()
        self.widgets = []
        self.settings = QtCore.QSettings(ORG_NAME, APP_NAME)
        self.settings.clear()
        self.settings.setValue("testing/chart_opengl_v2", False)
        self.settings.sync()

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def tearDown(self):
        for widget in reversed(self.widgets):
            if hasattr(widget, "_order_flow_runtime"):
                self.close_window(widget)
            else:
                widget.close()
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.settle()
        self.assertFalse(self.qt_errors, f"Unhandled Qt callback failures: {self.qt_errors}")

    def close_window(self, window):
        if getattr(window, "_indicator_check_closed", False):
            return
        window._indicator_check_closed = True
        from nightwatch.orderbook.backend import _OrderBookProcessLink
        runtime = window._order_flow_runtime
        links = list(dict.fromkeys(
            window.findChildren(_OrderBookProcessLink)
            + runtime.findChildren(_OrderBookProcessLink)
        ))
        window.close()
        for link in links:
            link.close()
        deadline = time.monotonic() + 5
        while window._order_flow_runtime_thread.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(window._order_flow_runtime_thread.isRunning())
        window.close()
        self.settle()

    def test_catalog_parameters_and_shortcuts_round_trip(self):
        expected = {
            "EMA Trend", "VWAP", "Supertrend", "Bollinger Bands",
            "Visible Volume Profile", "Liquidations", "RSI", "ADX / DI",
            "Relative Volume", "Chaikin Money Flow", "Open Interest",
            "Funding Rate History",
        }
        self.assertEqual(set(INDICATOR_KEYS), expected)
        self.assertEqual(len(INDICATOR_KEYS), 12)
        dialog = IndicatorSettingsDialog(deepcopy(INDICATOR_SETTING_DEFAULTS))
        self.widgets.append(dialog)
        self.assertEqual(set(dialog.editors), expected | {"Auto Fibonacci"})
        self.assertEqual(dialog.values(), INDICATOR_SETTING_DEFAULTS)
        dialog.editors["Supertrend"]["period"].setValue(17)
        dialog.editors["Supertrend"]["multiplier"].setValue(4.2)
        dialog.editors["ADX / DI"]["threshold"].setValue(31)
        dialog.editors["Relative Volume"]["period"].setValue(37)
        dialog.editors["Chaikin Money Flow"]["period"].setValue(29)
        anchor = dialog.choice_editors["VWAP"]["anchor"]
        anchor.setCurrentIndex(anchor.findData("week"))
        dialog._accept_validated()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        changed = dialog.values()
        reopened = IndicatorSettingsDialog(changed, initial_indicator="Relative Volume")
        self.widgets.append(reopened)
        self.assertEqual(reopened.values(), changed)
        reopened._reset_selected()
        self.assertEqual(reopened.values()["Relative Volume"], {"period": 20})
        self.assertEqual(reopened.values()["Supertrend"], changed["Supertrend"])
        shortcuts = IndicatorShortcutsDialog(dict(DEFAULT_INDICATOR_SHORTCUTS))
        self.widgets.append(shortcuts)
        self.assertEqual(set(shortcuts.editors), expected)
        self.assertEqual(shortcuts.shortcuts(), DEFAULT_INDICATOR_SHORTCUTS)

    def test_dialog_rejects_invalid_cross_field_values(self):
        dialog = IndicatorSettingsDialog(deepcopy(INDICATOR_SETTING_DEFAULTS))
        self.widgets.append(dialog)
        dialog.editors["EMA Trend"]["fast"].setValue(100)
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog._accept_validated()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.names.currentItem().text(), "EMA Trend")
        dialog._reset_selected()
        dialog.editors["RSI"]["lower"].setValue(50)
        dialog.editors["RSI"]["upper"].setValue(50)
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog._accept_validated()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.names.currentItem().text(), "RSI")

    def test_first_launch_migration_actions_and_restart(self):
        from nightwatch.app import main_window
        from nightwatch.entrypoint import AppComposition
        from nightwatch.networking import binance
        from nightwatch.trading.gateway import TradingGateway

        class OfflineGateway(TradingGateway):
            def arm(self, *args, **kwargs):
                raise AssertionError("Trading is disabled in indicator checks")

            def _open_trade_socket(self, *args, **kwargs):
                raise AssertionError("Network is disabled in indicator checks")

            def _resume_user_stream(self, *args, **kwargs):
                raise AssertionError("Network is disabled in indicator checks")

        class OfflineComposition(AppComposition):
            def create_trading_gateway(self):
                return OfflineGateway(self.testnet, self.parent)

            def create_database(self):
                raise AssertionError("Recorder is disabled in indicator checks")

            def create_market_data_hub(self, db):
                raise AssertionError("Network is disabled in indicator checks")

            def create_chart_market_data_hub(self, parent):
                raise AssertionError("Network is disabled in indicator checks")

        class OfflineWindow(main_window.MainWindow):
            def _run_deferred_startup_stage(self):
                self._configure_crisp_ui()

            def _refresh_watchlist_hour_changes(self, *args, **kwargs):
                pass

            def _request_coin_icon(self, *args, **kwargs):
                pass

            def _request_older_chart_history(self, *args, **kwargs):
                pass

            def _record_market_event(self, *args, **kwargs):
                pass

            def _fit_normal_window_to_work_area(self):
                pass

        requests = []

        async def reject_network(*args, **kwargs):
            requests.append((args, kwargs))
            raise AssertionError("Network is disabled in indicator checks")

        fibonacci = deepcopy(INDICATOR_SETTING_DEFAULTS["Auto Fibonacci"])
        fibonacci.update(maximum_candidates=8, label_position="left", show_badge=False)
        old_settings = {
            "Auto Fibonacci": fibonacci,
            "RSI": {"period": 42},
            "VWAP": {"anchor": "month"},
            "Donchian Channels": {"period": 99},
            "Major Price Levels": {"maximum_levels": 7},
        }
        self.settings.setValue("chart/indicator_settings_v1", json.dumps(old_settings))
        self.settings.setValue("chart/indicator_shortcuts_v1", json.dumps({"RSI": "Ctrl+0"}))
        self.settings.setValue("chart/study_heights_v1", json.dumps({"RSI": 350, "ATR": 240}))
        self.settings.setValue("chart/active_indicators_v1", json.dumps({"RSI": True}))
        self.settings.setValue("chart/rsi_period", 99)
        self.settings.setValue("trading/hotkeys_v1", json.dumps({"place_buy": "Ctrl+7"}))
        self.settings.setValue("trading/quick_preset_v1", json.dumps({"leverage": 7}))
        self.settings.setValue("chart/volume_bar_height_pct_v1", 22)
        self.settings.sync()

        with patch.object(main_window, "AppComposition", OfflineComposition), \
                patch.object(binance._AsyncHttpRuntime, "request_json", reject_network), \
                patch.object(binance._AsyncHttpRuntime, "request_bytes", reject_network):
            window = OfflineWindow(symbol="BTCUSDT", interval="15m", testnet=True)
            self.widgets.append(window)
            self.settle()
            self.assertEqual(set(window.indicator_actions), set(INDICATOR_KEYS))
            self.assertFalse(any(action.isChecked() for action in window.indicator_actions.values()))
            self.assertFalse(any(window.chart.indicators.values()))
            for name in INDICATOR_KEYS:
                self.assertEqual(window.indicator_settings[name], INDICATOR_SETTING_DEFAULTS[name])
            self.assertEqual(window.indicator_settings["Auto Fibonacci"], fibonacci)
            self.assertEqual(window.saved_study_heights, {})
            self.assertEqual(window.volume_bar_height_percent, 22)
            self.assertEqual(window.trading_hotkeys["place_buy"], "Ctrl+7")
            self.assertEqual(window.quick_trading_preset["leverage"], 7)
            self.assertEqual(window.indicator_shortcuts["Chaikin Money Flow"], "")
            self.assertEqual(window.indicator_shortcuts["RSI"], DEFAULT_INDICATOR_SHORTCUTS["RSI"])
            for key in ("chart/indicator_settings_v1", "chart/indicator_shortcuts_v1",
                        "chart/study_heights_v1", "chart/active_indicators_v1", "chart/rsi_period"):
                self.assertFalse(self.settings.contains(key), key)
            menu_groups = {
                action.menu().title(): action.menu()
                for action in window.indicator_menu.actions() if action.menu() is not None
            }
            self.assertEqual(set(menu_groups), set(INDICATOR_GROUPS))
            for group, names in INDICATOR_GROUPS.items():
                self.assertEqual(menu_groups[group].actions(), [window.indicator_actions[n] for n in names])
            for name, action in window.indicator_actions.items():
                action.trigger()
                self.assertTrue(window.chart.indicators[name], name)
                action.trigger()
                self.assertFalse(window.chart.indicators[name], name)

            def save_parameters(dialog):
                dialog.editors["Supertrend"]["period"].setValue(18)
                dialog.editors["Relative Volume"]["period"].setValue(33)
                dialog.editors["Chaikin Money Flow"]["period"].setValue(26)
                dialog._accept_validated()
                return dialog.result()

            with patch.object(IndicatorSettingsDialog, "exec", save_parameters):
                window.edit_indicator_settings("Supertrend")
            self.assertEqual(window.chart.indicator_settings["Supertrend"]["period"], 18)
            window.chart.set_study_heights({"Relative Volume": 188})
            window.indicator_actions["RSI"].setChecked(True)
            self.close_window(window)
            saved = json.loads(self.settings.value("chart/indicator_settings_v2", "", str))
            self.assertEqual(saved["Relative Volume"], {"period": 33})
            self.assertEqual(saved["Auto Fibonacci"], fibonacci)
            self.assertEqual(set(saved), set(INDICATOR_KEYS) | {"Auto Fibonacci"})

            reopened = OfflineWindow(symbol="BTCUSDT", interval="15m", testnet=True)
            self.widgets.append(reopened)
            self.settle()
            self.assertEqual(reopened.chart.indicator_settings["Supertrend"]["period"], 18)
            self.assertEqual(reopened.chart.indicator_settings["Chaikin Money Flow"]["period"], 26)
            self.assertEqual(reopened.chart.study_heights["Relative Volume"], 188)
            self.assertEqual(reopened.chart.indicator_settings["Auto Fibonacci"], fibonacci)
            self.assertFalse(any(reopened.chart.indicators.values()))
            self.assertEqual(reopened.trading_hotkeys["place_buy"], "Ctrl+7")
            self.assertEqual(reopened.quick_trading_preset["leverage"], 7)
            self.close_window(reopened)
        self.assertEqual(requests, [], "UI checks attempted network I/O")


if __name__ == "__main__":
    unittest.main(verbosity=2)
