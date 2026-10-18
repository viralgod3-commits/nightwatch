#!/usr/bin/env python3
"""Offline Qt regression checks: python tools/check_trading_panel.py.

Uses an in-memory gateway. Never opens an exchange connection or places orders.
Set QT_SCALE_FACTOR before launch to exercise device scaling as well as the
font-size overrides checked here. Requires the normal application dependencies.
"""
from __future__ import annotations

from itertools import combinations
from decimal import Decimal
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets, QtTest
from nightwatch.models import SymbolRules
from nightwatch.theme import THEMES, build_shell_stylesheet, ui_palette
from nightwatch.trading.trading_ui import TradingWorkspace
from nightwatch.utilities import (
    TYPOGRAPHY_DEFAULTS, apply_typography, configure_typography, load_app_fonts,
)


class OfflineGateway(QtCore.QObject):
    state_changed = QtCore.Signal(str)
    request_succeeded = QtCore.Signal(str, object)
    request_failed = QtCore.Signal(str, str)
    armed_changed = QtCore.Signal(bool)
    account_event = QtCore.Signal(object)
    snapshot_ready = QtCore.Signal(object)
    account_status_changed = QtCore.Signal(str)
    credentials_changed = QtCore.Signal(str)
    leverage_changing = QtCore.Signal(str, int)
    leverage_changed = QtCore.Signal(str, int, bool, str)
    testnet = True
    stopping = False
    account_loaded = True
    hedge_mode = False

    def __init__(self):
        super().__init__()
        self.cross_pending = set()
        self.queued_leverage = {}
        self.position_cache = {}

    def has_credentials(self): return True
    def current_leverage(self, _symbol): return 5
    def available_balance(self, _asset): return 12500.0
    def account_balance(self, asset): return 12500.0, asset
    def placement_status(self): return 0, 10, 0
    def refresh_account(self, *_args, **_kwargs): pass
    def ensure_cross(self, _symbol): pass
    def reconcile_unknown_orders(self): pass
    def apply_cross_leverage(self, *_args): pass


class TradingPanelChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setStyle("Fusion")
        load_app_fonts(cls.app, str(ROOT / "nightwatch"))

    def settle(self):
        for _ in range(20):
            self.app.processEvents()

    def setUp(self):
        configure_typography({})
        self.gateway = OfflineGateway()
        self.workspace = TradingWorkspace(THEMES["Nightwatch"], self.gateway)
        self.app.setStyleSheet(build_shell_stylesheet("Nightwatch", ui_palette(THEMES["Nightwatch"])))
        apply_typography(self.workspace)
        self.workspace.resize(340, 720)
        self.workspace.show()
        self.ticket = self.workspace.ticket
        self.ticket.set_symbol("BTCUSDT", SymbolRules())
        self.ticket.set_execution_market_state(True, True)
        self.ticket.set_mark_price(65000)
        self.ticket.apply_account_snapshot({"positionMode": {"dualSidePosition": False},
                                           "account": {"availableBalance": "12500", "positions": []}})
        self.ticket.quantity_edit.setText("0.025")
        self.ticket.price_edit.setText("65000")
        self.ticket.trigger_edit.setText("66000")
        self.settle()

    def tearDown(self):
        self.workspace.close()
        self.workspace.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.settle()

    def choose_type(self, kind):
        if kind in self.ticket.order_type_buttons:
            self.ticket.order_type_buttons[kind].click()
        else:
            self.ticket.conditional_actions[kind].trigger()
        self.settle()

    def assert_ticket_geometry(self):
        t = self.ticket
        self.assertLessEqual(t._ticket_body.width(), t.ticket_scroll.viewport().width())
        for root in (t._ticket_body, t._submit_footer):
            for parent in (root, *root.findChildren(QtWidgets.QWidget)):
                children = [child for child in parent.findChildren(
                    QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly
                ) if child.isVisible() and not child.isWindow() and not child.size().isEmpty()]
                for child in children:
                    self.assertTrue(parent.rect().contains(child.geometry()),
                                    f"{child.objectName() or type(child).__name__} outside parent")
                    if isinstance(child, QtWidgets.QLabel) and child.wordWrap():
                        self.assertGreaterEqual(child.height(), child.heightForWidth(child.width()),
                                                "Wrapped feedback is clipped")
                for first, second in combinations(children, 2):
                    self.assertFalse(first.geometry().intersects(second.geometry()),
                                     f"Overlapping {type(first).__name__} and {type(second).__name__}")
        for button in (t.buy_button, t.sell_button, *t.order_type_buttons.values(), t.conditional_button):
            self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 16)

    def test_all_order_types_across_dock_widths(self):
        for width in (240, 280, 320, 340, 400, 560, 720):
            self.workspace.resize(width, 720)
            for _title, kind in self.ticket.ORDER_TYPES:
                with self.subTest(width=width, kind=kind):
                    self.choose_type(kind)
                    self.assertEqual(self.workspace.width(), width)
                    self.assert_ticket_geometry()
                    self.assertEqual(self.ticket.ticket_scroll.verticalScrollBar().maximum(), 0)

    def test_large_fonts_reflow_and_short_docks_scroll(self):
        for scale in (1.5, 2.0):
            configure_typography({role: {"size": profile["size"] * scale}
                                  for role, profile in TYPOGRAPHY_DEFAULTS.items()})
            for width in (240, 340, 720):
                for _title, kind in self.ticket.ORDER_TYPES:
                    with self.subTest(scale=scale, width=width, kind=kind):
                        self.workspace.resize(width, 360)
                        self.choose_type(kind)
                        self.assert_ticket_geometry()
                        self.assertTrue(self.ticket.rect().contains(self.ticket._submit_footer.geometry()))
                        self.assertFalse(self.ticket.ticket_scroll.geometry().intersects(
                            self.ticket._submit_footer.geometry()))

    def test_wrapped_feedback_and_recovery_stay_reachable(self):
        self.workspace.resize(280, 300)
        self.choose_type("TRAILING_STOP_MARKET")
        self.ticket.set_submission_state("OUTCOME UNKNOWN", "The exchange has not confirmed this order. "
                                         "Reconcile the order before trying again. " * 6)
        self.settle()
        self.assert_ticket_geometry()
        scroll = self.ticket.ticket_scroll
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.settle()
        button = self.ticket.reconcile_button
        rect = QtCore.QRect(button.mapTo(scroll.viewport(), QtCore.QPoint()), button.size())
        self.assertTrue(scroll.viewport().rect().contains(rect))
        self.ticket.set_submission_state("READY")
        self.workspace.resize(340, 720)
        self.settle()
        self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        self.assert_ticket_geometry()

    def test_every_application_theme(self):
        self.choose_type("STOP")
        for inherited in (False, True):
            self.workspace._inherited_theme_stylesheet = inherited
            if inherited:
                self.workspace.setStyleSheet("")
            for name, palette in THEMES.items():
                with self.subTest(theme=name, inherited=inherited):
                    stylesheet = build_shell_stylesheet(name, ui_palette(palette))
                    if inherited:
                        stylesheet += TradingWorkspace.scoped_theme_stylesheet(palette)
                    self.app.setStyleSheet(stylesheet)
                    self.workspace.apply_theme(palette)
                    self.settle()
                    self.assert_ticket_geometry()
                    self.assertFalse(self.workspace.grab().isNull())

    def test_order_controls_preserve_execution_payloads(self):
        requests = []
        self.ticket.order_requested.connect(requests.append)
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            for kind in ("MARKET", "LIMIT", "STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"):
                self.choose_type(kind)
                self.ticket.set_mark_price(65000)
                self.ticket.buy_button.click()
                self.assertEqual(requests[-1]["order"]["type"], kind)
                self.assertEqual(requests[-1]["order"]["side"], "BUY")
                self.assertEqual(requests[-1]["order"]["quantity"], "0.025")
                self.assertEqual(requests[-1]["position_intent"], "OPEN")
            warning.assert_not_called()
            self.choose_type("MARKET")
            self.ticket.sell_button.click()
            self.assertEqual(requests[-1]["order"]["side"], "SELL")
            count = len(requests)
            self.ticket._last_mark_mono = time.monotonic() - 10
            self.ticket.buy_button.click()
            self.assertEqual(len(requests), count)
            self.assertIn("STALE", warning.call_args.args[2])
            self.ticket.set_mark_price(65000)
            self.ticket.apply_account_snapshot({"positionMode": {"dualSidePosition": False}, "account": {
                "availableBalance": "12500", "positions": [{"symbol": "BTCUSDT", "positionSide": "BOTH",
                "positionAmt": "1", "leverage": "5", "entryPrice": "64000"}]}})
            self.ticket.reduce_only.setChecked(True)
            self.ticket.quantity_edit.setText("0.25")
            self.ticket.sell_button.click()
            self.assertTrue(requests[-1]["order"]["reduceOnly"])
            self.assertEqual(requests[-1]["position_intent"], "REDUCE")
            self.assertEqual(requests[-1]["protections"], {"tp": [], "sl": []})

    def test_presets_and_manual_entry(self):
        self.ticket.margin_bar.preset_buttons[1].click()
        self.assertEqual(self.ticket.allocation_slider.value(), 50)
        self.assertTrue(self.ticket.margin_bar.preset_buttons[1].isChecked())
        self.assertEqual(Decimal(self.ticket.quantity_edit.text()), Decimal("0.480"))
        self.ticket.quantity_edit.selectAll()
        QtTest.QTest.keyClicks(self.ticket.quantity_edit, "0.025")
        self.assertFalse(any(button.isChecked() for button in self.ticket.margin_bar.preset_buttons))
        self.assertIsNone(self.ticket._allocation_percent)
        self.ticket.size_mode.setCurrentIndex(self.ticket.size_mode.findData("QUOTE NOTIONAL"))
        self.assertEqual(self.ticket.quantity_edit.text(), "")
        self.ticket.quantity_edit.setText("650")
        self.assertEqual(Decimal(self.ticket._quantity("MARKET")), Decimal("0.010"))

    def test_unchanged_quotes_do_not_relayout(self):
        class Counter(QtCore.QObject):
            count = 0
            def eventFilter(self, _watched, event):
                if event.type() == QtCore.QEvent.Type.LayoutRequest:
                    self.count += 1
                return False
        counter = Counter()
        self.ticket._ticket_body.installEventFilter(counter)
        for _ in range(100):
            self.ticket.set_mark_price(65000)
            self.app.processEvents()
        self.assertEqual(counter.count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
