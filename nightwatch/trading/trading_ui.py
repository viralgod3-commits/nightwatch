"""Manual order entry, protection plans, and account/trading workspaces."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal

from ..constants import (
    CONDITIONAL_ORDER_TYPES,
    DEFAULT_QUICK_TRADING_PRESET,
    DEFAULT_SYMBOL,
    DEFAULT_TRADING_HOTKEYS,
    LEVERAGE_PRESETS,
)
from ..models import SymbolRules
from ..models import TradingGatewayPort
from ..utilities import ElidedLabel, line_icon
from ..utilities import TextRole, set_text_role, typography_font
from ..models import format_price, human_number, quantize_step, safe_float, validate_step
from .orders import (
    is_shift_letter_shortcut,
    is_smart_exit_shortcut,
)




_ACCOUNT_POLL_MINIMUM_INTERVAL = 8.0


def _exchange_step_text(value: Any) -> str:
    """Render Binance decimal step strings without changing their numeric contract."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if not number.is_finite() or number <= 0:
        return "—"
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _confirm_cancel_symbol_orders(parent: QtWidgets.QWidget, symbol: str) -> bool:
    return QtWidgets.QMessageBox.warning(
        parent,
        f"Cancel all {symbol} orders?",
        f"Cancel every working order for {symbol}, including entries, stop-losses, "
        "take-profits and conditional orders? Open positions will remain open and may lose protection.",
        QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
        QtWidgets.QMessageBox.StandardButton.Cancel,
    ) == QtWidgets.QMessageBox.StandardButton.Yes


class CredentialsDialog(QtWidgets.QDialog):
    def __init__(self, api_key: str, api_secret: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Binance API credentials")
        self.setMinimumWidth(500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("BINANCE API CREDENTIALS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Keys are kept only in memory for this session. Use a Binance API key with USD-M Futures trading permission and withdrawals disabled."
        )
        note.setWordWrap(True)
        note.setObjectName("subtleLabel")
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        self.key_edit = QtWidgets.QLineEdit(api_key)
        self.secret_edit = QtWidgets.QLineEdit(api_secret)
        self.secret_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("API key", self.key_edit)
        form.addRow("API secret", self.secret_edit)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def credentials(self) -> tuple[str, str]:
        return self.key_edit.text().strip(), self.secret_edit.text().strip()


class TradingHotkeysDialog(QtWidgets.QDialog):
    ACTIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("place_buy", "Place Buy / Long"),
        ("place_sell", "Place Sell / Short"),
        ("close_1", "Close preset 1"),
        ("close_2", "Close preset 2"),
        ("close_3", "Close preset 3"),
        ("cancel_all", "Cancel all symbol orders"),
        ("open_trading", "Open Trading workspace"),
        ("refresh_account", "Refresh account"),
        ("kill_session", "Cancel all + lock quick orders"),
    )

    def __init__(
        self,
        shortcuts: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
        reserved_shortcuts: set[str] | None = None,
    ):
        super().__init__(parent)
        self.reserved_shortcuts = {str(value) for value in (reserved_shortcuts or set()) if str(value)}
        self.setWindowTitle("Trading hotkeys")
        self.setMinimumWidth(500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        note = QtWidgets.QLabel(
            "Quick-order lock: Ctrl+Shift+A toggles the fixed B/S workflow. B/S + one digit uses 10%-90% collateral; BB/SS uses 100%; Ctrl+Shift+X creates a passive Smart Exit. Exact Shift+letter shortcuts are reserved for symbol search."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(7)
        self.editors: dict[str, QtWidgets.QKeySequenceEdit] = {}
        for key, label in self.ACTIONS:
            editor = QtWidgets.QKeySequenceEdit(
                QtGui.QKeySequence(shortcuts.get(key, DEFAULT_TRADING_HOTKEYS[key]))
            )
            if hasattr(editor, "setMaximumSequenceLength"):
                editor.setMaximumSequenceLength(1)
            self.editors[key] = editor
            form.addRow(label, editor)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Reset
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset).clicked.connect(self._reset)
        layout.addWidget(buttons)

    def _reset(self) -> None:
        for key, editor in self.editors.items():
            editor.setKeySequence(QtGui.QKeySequence(DEFAULT_TRADING_HOTKEYS[key]))

    def _accept_validated(self) -> None:
        values = [
            editor.keySequence().toString(QtGui.QKeySequence.SequenceFormat.PortableText)
            for editor in self.editors.values()
        ]
        active = [value for value in values if value]
        if len(active) != len(set(active)):
            QtWidgets.QMessageBox.warning(self, "Duplicate hotkey", "Each active trading action needs a unique shortcut.")
            return
        if any(value in set("0123456789") for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Number key reserved",
                "Unmodified number keys are reserved for chart indicators. Add Shift, Ctrl or Alt to the trading hotkey.",
            )
            return
        if any(len(value) == 1 and value.isalpha() for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Letter key reserved",
                "Bare letters are reserved for the quick-order B/S grammar and symbol search. Add Ctrl or Alt to the trading hotkey.",
            )
            return
        if any(is_shift_letter_shortcut(value) for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Shift + letter reserved",
                "Shift + letter opens symbol search while quick orders are unlocked.",
            )
            return
        if any(is_smart_exit_shortcut(value) for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Smart Exit shortcut reserved",
                "Ctrl+Shift+X is reserved for Smart Exit.",
            )
            return
        conflicts = sorted(set(active) & self.reserved_shortcuts)
        if conflicts:
            QtWidgets.QMessageBox.warning(
                self,
                "Shortcut already in use",
                "These shortcuts are already reserved by Nightwatch: " + ", ".join(conflicts),
            )
            return
        self.accept()

    def shortcuts(self) -> dict[str, str]:
        return {
            key: editor.keySequence().toString(QtGui.QKeySequence.SequenceFormat.PortableText)
            for key, editor in self.editors.items()
        }


class QuickTradingSettingsDialog(QtWidgets.QDialog):
    """Edit the one-click order preset and its shortcuts together."""

    def __init__(
        self,
        preset: dict[str, Any],
        shortcuts: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
        reserved_shortcuts: set[str] | None = None,
    ):
        super().__init__(parent)
        self.reserved_shortcuts = {str(value) for value in (reserved_shortcuts or set()) if str(value)}
        self.setWindowTitle("Quick trading settings")
        self.setMinimumSize(650, 650)
        merged = {**DEFAULT_QUICK_TRADING_PRESET, **preset}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        note = QtWidgets.QLabel(
            "Ctrl+Shift+A toggles the quick-order lock. Manual ticket orders and explicit risk-reduction actions remain available while quick orders are locked. Fixed fast keys use the execution ticket's current leverage; configurable Buy/Sell hotkeys use the saved custom-order preset."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        fixed_page = QtWidgets.QWidget()
        fixed_layout = QtWidgets.QVBoxLayout(fixed_page)
        fixed_layout.setContentsMargins(10, 10, 10, 10)
        fixed_layout.setSpacing(10)
        fixed_intro = QtWidgets.QLabel(
            "FIXED QUICK-ORDER WORKFLOW · NOT REMAPPABLE"
        )
        fixed_intro.setObjectName("controlSectionTitle")
        fixed_layout.addWidget(fixed_intro)
        fixed_grid = QtWidgets.QGridLayout()
        fixed_grid.setHorizontalSpacing(18)
        fixed_grid.setVerticalSpacing(10)
        fixed_rows = (
            (
                "B1 / S1",
                "Market BUY/SELL using 10% of available collateral at the leverage currently selected in the execution ticket.",
            ),
            (
                "B2–B9 / S2–S9",
                "Immediate collateral sizing from 20% through 90%. Zero and multi-digit percentages are not accepted.",
            ),
            (
                "BB / SS",
                "Market BUY/SELL using 100% of available collateral at the currently selected leverage.",
            ),
            (
                "1 SECOND",
                "The digit or second side letter must arrive within one second. An expired sequence is ignored and reported in the bottom-left status bar.",
            ),
            (
                "SHIFT + LETTER",
                "Open symbol search while quick orders are unlocked. Bare letters are consumed so they cannot interrupt fast order entry.",
            ),
            (
                "CTRL + SHIFT + X",
                "Create one to three visible post-only Smart Exit orders near very recent highs for a long or lows for a short. It never adds a stop, market-closes, or reprices later.",
            ),
        )
        for row, (command, description) in enumerate(fixed_rows):
            command_label = QtWidgets.QLabel(command)
            command_label.setObjectName("tradingSectionLabel")
            command_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            description_label = QtWidgets.QLabel(description)
            description_label.setObjectName("subtleLabel")
            description_label.setWordWrap(True)
            command_label.setToolTip(description)
            description_label.setToolTip(description)
            fixed_grid.addWidget(command_label, row, 0)
            fixed_grid.addWidget(description_label, row, 1)
        fixed_grid.setColumnStretch(1, 1)
        fixed_layout.addLayout(fixed_grid)
        fixed_layout.addStretch(1)
        tabs.addTab(fixed_page, "FAST KEYS")

        order_page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(order_page)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.collateral_percent = QtWidgets.QDoubleSpinBox()
        self.collateral_percent.setRange(0.1, 100.0)
        self.collateral_percent.setDecimals(1)
        self.collateral_percent.setSuffix("%")
        self.collateral_percent.setValue(safe_float(merged["collateral_percent"]))
        self.collateral_percent.setToolTip(
            "Share of currently available margin allocated before leverage. Example: 10% at 5× targets order notional equal to 50% of available margin."
        )
        self.leverage = QtWidgets.QSpinBox()
        self.leverage.setRange(1, 125)
        self.leverage.setSuffix("×")
        self.leverage.setValue(int(safe_float(merged["leverage"])))
        self.leverage.setToolTip(
            "Cross leverage required before an OPEN shortcut can run. Saving attempts to apply it to the active symbol immediately."
        )
        leverage_box = QtWidgets.QWidget()
        leverage_layout = QtWidgets.QHBoxLayout(leverage_box)
        leverage_layout.setContentsMargins(0, 0, 0, 0)
        leverage_layout.setSpacing(5)
        leverage_layout.addWidget(self.leverage)
        self.quick_leverage_buttons: list[QtWidgets.QPushButton] = []
        for value in LEVERAGE_PRESETS:
            button = QtWidgets.QPushButton(f"{value}×", self)
            button.setObjectName("leveragePresetButton")
            button.setCheckable(True)
            button.setProperty("leverageValue", value)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(f"Use {value}× leverage for OPEN trading shortcuts.")
            button.clicked.connect(
                lambda _checked=False, target=value: self.leverage.setValue(target)
            )
            self.quick_leverage_buttons.append(button)
            leverage_layout.addWidget(button)
        self.order_mode = QtWidgets.QComboBox()
        self.order_mode.addItems(("MARKET", "LIMIT"))
        self.order_mode.setCurrentText(str(merged["order_mode"]).upper())
        self.order_mode.setToolTip(
            "MARKET executes immediately. LIMIT posts at the current best same-side price and may remain unfilled."
        )
        self.time_in_force = QtWidgets.QComboBox()
        self.time_in_force.addItem("IOC", "IOC")
        self.time_in_force.addItem("GTC", "GTC")
        self.time_in_force.addItem("FOK", "FOK")
        self.time_in_force.addItem("GTX · POST-ONLY", "GTX")
        self.time_in_force.setCurrentIndex(
            max(0, self.time_in_force.findData(str(merged["time_in_force"]).upper()))
        )
        self.time_in_force.setToolTip(
            "GTC stays active; IOC fills available size then cancels; FOK requires a complete immediate fill; GTX is post-only. Used only by limit-style shortcuts."
        )
        self.slippage_enabled = QtWidgets.QCheckBox("Cap market slippage")
        self.slippage_enabled.setChecked(bool(merged["slippage_enabled"]))
        self.slippage_enabled.setToolTip(
            "Turns a MARKET shortcut into an aggressive IOC/FOK/GTC limit whose price cannot exceed the selected percentage from the live book."
        )
        self.max_slippage = QtWidgets.QDoubleSpinBox()
        self.max_slippage.setRange(0.01, 10.0)
        self.max_slippage.setDecimals(2)
        self.max_slippage.setSuffix("%")
        self.max_slippage.setValue(safe_float(merged["max_slippage_percent"]))
        self.max_slippage.setToolTip(
            "Maximum distance from the current best ask for BUY or best bid for SELL. Smaller caps reduce bad fills but increase non-fills."
        )
        slippage_box = QtWidgets.QWidget()
        slippage_layout = QtWidgets.QHBoxLayout(slippage_box)
        slippage_layout.setContentsMargins(0, 0, 0, 0)
        slippage_layout.addWidget(self.slippage_enabled)
        slippage_layout.addWidget(self.max_slippage)
        self.reduce_only = QtWidgets.QCheckBox("Reduce existing position only")
        self.reduce_only.setChecked(bool(merged["reduce_only"]))
        self.reduce_only.setToolTip(
            "One-way mode only: BUY reduces a short and SELL reduces a long. It cannot open or reverse a position. Close hotkeys are safer in hedge mode."
        )

        def add_row(label_text: str, field: QtWidgets.QWidget, tooltip: str) -> None:
            label = QtWidgets.QLabel(label_text)
            label.setToolTip(tooltip)
            field.setToolTip(field.toolTip() or tooltip)
            form.addRow(label, field)

        add_row("Margin allocation", self.collateral_percent, self.collateral_percent.toolTip())
        add_row("Leverage", leverage_box, self.leverage.toolTip())
        add_row("Execution", self.order_mode, self.order_mode.toolTip())
        add_row("Time in force", self.time_in_force, self.time_in_force.toolTip())
        add_row("Slippage cap", slippage_box, self.slippage_enabled.toolTip())
        add_row("Intent", self.reduce_only, self.reduce_only.toolTip())

        self.take_profit_enabled = QtWidgets.QCheckBox("Enabled")
        self.take_profit_enabled.setChecked(bool(merged["take_profit_enabled"]))
        self.take_profit_enabled.setToolTip("After an OPEN shortcut fills, place a reduce-only take-profit at the configured move.")
        self.take_profit_percent = QtWidgets.QDoubleSpinBox()
        self.take_profit_percent.setRange(0.01, 100.0)
        self.take_profit_percent.setDecimals(2)
        self.take_profit_percent.setSuffix("% move")
        self.take_profit_percent.setValue(safe_float(merged["take_profit_percent"]))
        self.take_profit_percent.setToolTip("Favorable percentage move from the shortcut entry reference.")
        self.take_profit_close = QtWidgets.QSpinBox()
        self.take_profit_close.setRange(1, 100)
        self.take_profit_close.setSuffix("% close")
        self.take_profit_close.setValue(int(safe_float(merged["take_profit_close_percent"])))
        self.take_profit_close.setToolTip("Percentage of the filled entry quantity closed at the take-profit.")
        tp_box = QtWidgets.QWidget()
        tp_layout = QtWidgets.QHBoxLayout(tp_box)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.addWidget(self.take_profit_enabled)
        tp_layout.addWidget(self.take_profit_percent)
        tp_layout.addWidget(self.take_profit_close)
        add_row("Take profit", tp_box, "Optional reduce-only take-profit placed after an OPEN shortcut fills.")

        self.stop_loss_enabled = QtWidgets.QCheckBox("Enabled")
        self.stop_loss_enabled.setChecked(bool(merged["stop_loss_enabled"]))
        self.stop_loss_enabled.setToolTip("After an OPEN shortcut fills, place a reduce-only stop-loss at the configured adverse move.")
        self.stop_loss_percent = QtWidgets.QDoubleSpinBox()
        self.stop_loss_percent.setRange(0.01, 100.0)
        self.stop_loss_percent.setDecimals(2)
        self.stop_loss_percent.setSuffix("% move")
        self.stop_loss_percent.setValue(safe_float(merged["stop_loss_percent"]))
        self.stop_loss_percent.setToolTip("Adverse percentage move from the shortcut entry reference.")
        self.stop_loss_close = QtWidgets.QSpinBox()
        self.stop_loss_close.setRange(1, 100)
        self.stop_loss_close.setSuffix("% close")
        self.stop_loss_close.setValue(int(safe_float(merged["stop_loss_close_percent"])))
        self.stop_loss_close.setToolTip("Percentage of the filled entry quantity closed at the stop-loss.")
        sl_box = QtWidgets.QWidget()
        sl_layout = QtWidgets.QHBoxLayout(sl_box)
        sl_layout.setContentsMargins(0, 0, 0, 0)
        sl_layout.addWidget(self.stop_loss_enabled)
        sl_layout.addWidget(self.stop_loss_percent)
        sl_layout.addWidget(self.stop_loss_close)
        add_row("Stop loss", sl_box, "Optional reduce-only stop-loss placed after an OPEN shortcut fills.")

        close_box = QtWidgets.QWidget()
        close_layout = QtWidgets.QHBoxLayout(close_box)
        close_layout.setContentsMargins(0, 0, 0, 0)
        self.close_presets: list[QtWidgets.QSpinBox] = []
        for key in ("close_1_percent", "close_2_percent", "close_3_percent"):
            editor = QtWidgets.QSpinBox()
            editor.setRange(1, 100)
            editor.setSuffix("%")
            editor.setValue(int(safe_float(merged[key])))
            editor.setToolTip("Position percentage closed by the matching close hotkey.")
            self.close_presets.append(editor)
            close_layout.addWidget(editor)
        add_row("Close hotkeys", close_box, "Independent position-close presets; ticket sizing buttons remain 25/50/75/100%.")
        self.reduce_protection_note = QtWidgets.QLabel(
            "Reduce Only turns off attached TP/SL; those targets apply only to opening entries."
        )
        self.reduce_protection_note.setObjectName("subtleLabel")
        self.reduce_protection_note.setWordWrap(True)
        form.addRow(self.reduce_protection_note)
        tabs.addTab(order_page, "CUSTOM ORDER")

        hotkey_page = QtWidgets.QWidget()
        hotkey_form = QtWidgets.QFormLayout(hotkey_page)
        hotkey_form.setHorizontalSpacing(14)
        hotkey_form.setVerticalSpacing(7)
        hotkey_note = QtWidgets.QLabel(
            "These remappable actions are separate from the fixed FAST KEYS workflow. Shift+letter remains reserved for symbol search while quick orders are unlocked; Ctrl+Shift+X remains reserved for Smart Exit."
        )
        hotkey_note.setObjectName("subtleLabel")
        hotkey_note.setWordWrap(True)
        hotkey_form.addRow(hotkey_note)
        self.hotkey_editors: dict[str, QtWidgets.QKeySequenceEdit] = {}
        hotkey_help = {
            "place_buy": "Submit BUY using the saved shortcut order preset. In one-way reduce mode this reduces a short.",
            "place_sell": "Submit SELL using the saved shortcut order preset. In one-way reduce mode this reduces a long.",
            "close_1": "Close the selected position using close percentage preset 1.",
            "close_2": "Close the selected position using close percentage preset 2.",
            "close_3": "Close the selected position using close percentage preset 3.",
            "cancel_all": "Cancel every active order for the current symbol.",
            "open_trading": "Open the chart workspace with the trading sidebar visible.",
            "refresh_account": "Refresh positions, orders, fills and balances.",
            "kill_session": "Cancel current-symbol orders and immediately lock quick-order hotkeys.",
        }
        for key, label in TradingHotkeysDialog.ACTIONS:
            editor = QtWidgets.QKeySequenceEdit(
                QtGui.QKeySequence(shortcuts.get(key, DEFAULT_TRADING_HOTKEYS[key]))
            )
            if hasattr(editor, "setMaximumSequenceLength"):
                editor.setMaximumSequenceLength(1)
            editor.setToolTip(hotkey_help[key])
            label_widget = QtWidgets.QLabel(label)
            label_widget.setToolTip(hotkey_help[key])
            self.hotkey_editors[key] = editor
            hotkey_form.addRow(label_widget, editor)
        tabs.addTab(hotkey_page, "HOTKEYS")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.order_mode.currentTextChanged.connect(self._sync_quick_controls)
        self.slippage_enabled.toggled.connect(self._sync_quick_controls)
        self.reduce_only.toggled.connect(self._sync_quick_controls)
        self.take_profit_enabled.toggled.connect(self._sync_quick_controls)
        self.stop_loss_enabled.toggled.connect(self._sync_quick_controls)
        self.leverage.valueChanged.connect(self._sync_quick_controls)
        self._sync_quick_controls()

    def _sync_quick_controls(self, _value: object = None) -> None:
        market_order = self.order_mode.currentText() == "MARKET"
        if not market_order and self.slippage_enabled.isChecked():
            blocker = QtCore.QSignalBlocker(self.slippage_enabled)
            self.slippage_enabled.setChecked(False)
            del blocker
        self.slippage_enabled.setEnabled(market_order)
        capped_market = market_order and self.slippage_enabled.isChecked()
        self.max_slippage.setEnabled(capped_market)
        self.time_in_force.setEnabled(not market_order or capped_market)
        gtx_index = self.time_in_force.findData("GTX")
        gtx_item = self.time_in_force.model().item(gtx_index)
        if gtx_item is not None:
            gtx_item.setEnabled(not capped_market)
        if capped_market and self.time_in_force.currentData() == "GTX":
            self.time_in_force.setCurrentIndex(self.time_in_force.findData("IOC"))
        reducing = self.reduce_only.isChecked()
        self.reduce_protection_note.setVisible(reducing)
        if reducing:
            for checkbox in (self.take_profit_enabled, self.stop_loss_enabled):
                blocker = QtCore.QSignalBlocker(checkbox)
                checkbox.setChecked(False)
                del blocker
        self.take_profit_enabled.setEnabled(not reducing)
        self.stop_loss_enabled.setEnabled(not reducing)
        self.take_profit_percent.setEnabled(
            not reducing and self.take_profit_enabled.isChecked()
        )
        self.take_profit_close.setEnabled(
            not reducing and self.take_profit_enabled.isChecked()
        )
        self.stop_loss_percent.setEnabled(
            not reducing and self.stop_loss_enabled.isChecked()
        )
        self.stop_loss_close.setEnabled(
            not reducing and self.stop_loss_enabled.isChecked()
        )
        for button in self.quick_leverage_buttons:
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(
                int(button.property("leverageValue")) == self.leverage.value()
            )
            del blocker

    def _accept_validated(self) -> None:
        if self.slippage_enabled.isChecked() and self.time_in_force.currentData() == "GTX":
            QtWidgets.QMessageBox.warning(self, "Invalid execution preset",
                                          "Slippage-capped orders cannot use GTX (post-only).")
            return
        active = [
            editor.keySequence().toString(QtGui.QKeySequence.SequenceFormat.PortableText)
            for editor in self.hotkey_editors.values()
            if not editor.keySequence().isEmpty()
        ]
        if len(active) != len(set(active)):
            QtWidgets.QMessageBox.warning(
                self, "Duplicate hotkey", "Each active trading action needs a unique shortcut."
            )
            return
        if any(value in set("0123456789") for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Number key reserved",
                "Unmodified number keys are reserved for chart indicators. Add Shift, Ctrl or Alt to the trading hotkey.",
            )
            return
        if any(len(value) == 1 and value.isalpha() for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Letter key reserved",
                "Bare letters are reserved for the quick-order B/S grammar and symbol search. Add Ctrl or Alt to the trading hotkey.",
            )
            return
        if any(is_shift_letter_shortcut(value) for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Shift + letter reserved",
                "Shift + letter opens symbol search while quick orders are unlocked.",
            )
            return
        if any(is_smart_exit_shortcut(value) for value in active):
            QtWidgets.QMessageBox.warning(
                self,
                "Smart Exit shortcut reserved",
                "Ctrl+Shift+X is reserved for Smart Exit.",
            )
            return
        conflicts = sorted(set(active) & self.reserved_shortcuts)
        if conflicts:
            QtWidgets.QMessageBox.warning(
                self,
                "Shortcut already in use",
                "These shortcuts are already reserved by Nightwatch: " + ", ".join(conflicts),
            )
            return
        self.accept()

    def values(self) -> tuple[dict[str, Any], dict[str, str]]:
        preset = {
            "collateral_percent": self.collateral_percent.value(),
            "leverage": self.leverage.value(),
            "order_mode": self.order_mode.currentText(),
            "time_in_force": str(self.time_in_force.currentData() or "GTC"),
            "slippage_enabled": self.slippage_enabled.isChecked(),
            "max_slippage_percent": self.max_slippage.value(),
            "reduce_only": self.reduce_only.isChecked(),
            "take_profit_enabled": self.take_profit_enabled.isChecked(),
            "take_profit_percent": self.take_profit_percent.value(),
            "take_profit_close_percent": self.take_profit_close.value(),
            "stop_loss_enabled": self.stop_loss_enabled.isChecked(),
            "stop_loss_percent": self.stop_loss_percent.value(),
            "stop_loss_close_percent": self.stop_loss_close.value(),
            "close_1_percent": self.close_presets[0].value(),
            "close_2_percent": self.close_presets[1].value(),
            "close_3_percent": self.close_presets[2].value(),
        }
        shortcuts = {
            key: editor.keySequence().toString(QtGui.QKeySequence.SequenceFormat.PortableText)
            for key, editor in self.hotkey_editors.items()
        }
        return preset, shortcuts


class ProtectionEditorDialog(QtWidgets.QDialog):
    """Small multi-target editor: up to four take-profits and four stop-losses."""

    def __init__(
        self,
        plans: dict[str, list[dict[str, float]]] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Multiple take-profit and stop-loss targets")
        self.setMinimumWidth(570)
        self.rows: dict[str, list[tuple[QtWidgets.QLineEdit, QtWidgets.QSpinBox]]] = {
            "tp": [],
            "sl": [],
        }
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        note = QtWidgets.QLabel(
            "Targets are submitted only after the entry fills. Each side can distribute up to 100% of the filled quantity."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(12)
        current = plans or {}
        for key, title in (("tp", "TAKE PROFIT"), ("sl", "STOP LOSS")):
            group = QtWidgets.QGroupBox(title)
            grid = QtWidgets.QGridLayout(group)
            grid.setContentsMargins(9, 9, 9, 9)
            grid.setHorizontalSpacing(7)
            grid.setVerticalSpacing(6)
            grid.addWidget(QtWidgets.QLabel("TRIGGER PRICE"), 0, 0)
            grid.addWidget(QtWidgets.QLabel("CLOSE %"), 0, 1)
            saved = list(current.get(key, []))
            for index in range(4):
                price = QtWidgets.QLineEdit()
                price.setPlaceholderText(f"Target {index + 1}")
                set_text_role(price, TextRole.MARKET_VALUE)
                percent = QtWidgets.QSpinBox()
                percent.setRange(0, 100)
                percent.setSuffix("%")
                percent.setValue(0)
                if index < len(saved):
                    price.setText(str(saved[index].get("price", "")))
                    percent.setValue(int(saved[index].get("percent", 0)))
                grid.addWidget(price, index + 1, 0)
                grid.addWidget(percent, index + 1, 1)
                self.rows[key].append((price, percent))
            columns.addWidget(group)
        layout.addLayout(columns)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        try:
            self.plans()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid protection targets", str(exc))
            return
        self.accept()

    def plans(self) -> dict[str, list[dict[str, float]]]:
        output: dict[str, list[dict[str, float]]] = {"tp": [], "sl": []}
        for key, rows in self.rows.items():
            total = 0
            for price_edit, percent_edit in rows:
                price_text = price_edit.text().replace(",", "").strip()
                percent = int(percent_edit.value())
                if not price_text and percent == 0:
                    continue
                price = safe_float(price_text)
                if price <= 0 or percent <= 0:
                    raise ValueError("Every used target needs a positive trigger price and close percentage.")
                total += percent
                output[key].append({"price": price, "percent": float(percent)})
            if total > 100:
                label = "take-profit" if key == "tp" else "stop-loss"
                raise ValueError(f"The {label} percentages total {total}%; the maximum is 100%.")
        return output


class CompactTradeComboBox(QtWidgets.QComboBox):
    """Compact ticket combo whose single arrow is owned by the stylesheet."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)




        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )



        self.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents
        )


class LeverageComboBox(CompactTradeComboBox):
    """Common leverage presets with a transient typed custom-value popup."""

    valueChanged = Signal(int)
    COMMON_VALUES: ClassVar[tuple[int, ...]] = (1, 3, 5, 10, 20, 50)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("leverageDropdown")
        self.setEditable(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._value = 5
        self._popup_open = False
        self._popup_restore_value: int | None = None
        self._custom_item_value: int | None = None
        self._custom_editor: QtWidgets.QLineEdit | None = None
        for leverage in self.COMMON_VALUES:
            self.addItem(f"{leverage}×", leverage)
        self.view().installEventFilter(self)
        self.view().viewport().installEventFilter(self)
        self.currentIndexChanged.connect(self._selection_changed)
        self.setValue(5)

    @staticmethod
    def minimum() -> int:
        return 1

    @staticmethod
    def maximum() -> int:
        return 125

    def value(self) -> int:
        return max(self.minimum(), min(self.maximum(), int(self._value)))

    def _remove_custom_item(self) -> None:
        if self._custom_item_value is None:
            return
        index = self.findData(self._custom_item_value)
        if index >= 0 and self._custom_item_value not in self.COMMON_VALUES:
            self.removeItem(index)
        self._custom_item_value = None

    def _show_value(self, value: int) -> None:
        blocker = QtCore.QSignalBlocker(self)
        self._remove_custom_item()
        index = self.findData(value)
        if index < 0:
            self.addItem(f"{value}×", value)
            self._custom_item_value = value
            index = self.count() - 1
        self.setCurrentIndex(index)
        del blocker

    def setValue(self, value: int) -> None:
        value = max(self.minimum(), min(self.maximum(), int(value)))
        changed = value != self._value
        self._value = value
        self._show_value(value)
        if changed:
            self.valueChanged.emit(value)

    def showPopup(self) -> None:
        self._popup_open = True
        self._popup_restore_value = self._value



        if self._custom_item_value is not None:
            blocker = QtCore.QSignalBlocker(self)
            self._remove_custom_item()
            self.setCurrentIndex(-1)
            del blocker
        super().showPopup()

    def hidePopup(self) -> None:
        self._popup_open = False
        super().hidePopup()
        if self.currentIndex() < 0:
            self._show_value(self._value)
        self._popup_restore_value = None

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        text = event.text()
        if self._popup_open and text and text.isdigit():
            self._begin_custom_entry(text)
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if self._custom_editor is not None and watched is self._custom_editor:
            if event.type() == QtCore.QEvent.Type.KeyPress:
                key_event = event
                if key_event.key() == Qt.Key.Key_Escape:
                    self._custom_editor.hide()
                    self._show_value(self._value)
                    return True
            return super().eventFilter(watched, event)
        if self._popup_open and event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event
            text = key_event.text()
            if text and text.isdigit():
                self._begin_custom_entry(text)
                return True
        return super().eventFilter(watched, event)

    def _begin_custom_entry(self, seed: str) -> None:
        super().hidePopup()
        self._popup_open = False
        editor = self._custom_editor
        if editor is None:
            editor = QtWidgets.QLineEdit(self)
            editor.setObjectName("leverageCustomPopup")
            editor.setWindowFlags(
                Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
            )
            editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
            editor.setPlaceholderText("1–125×")
            editor.setMaxLength(3)
            editor.returnPressed.connect(self._commit_custom_text)
            editor.editingFinished.connect(self._commit_custom_text)
            editor.installEventFilter(self)
            self._custom_editor = editor
        editor.setText(seed)
        editor.setFixedSize(max(78, self.width()), max(28, self.height()))
        editor.move(self.mapToGlobal(QtCore.QPoint(0, self.height() + 2)))
        editor.show()
        editor.raise_()
        editor.activateWindow()
        editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        editor.setCursorPosition(len(seed))

    def _selection_changed(self, index: int) -> None:
        if index < 0:
            return
        value = int(safe_float(self.itemData(index), self._value))
        self._popup_restore_value = None
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)

    def _commit_custom_text(self) -> None:
        editor = self._custom_editor
        if editor is None or not editor.isVisible():
            return
        raw = editor.text().replace("×", "").replace("x", "").strip()
        editor.hide()
        try:
            value = int(raw)
        except ValueError:
            value = self._value
        self.setValue(max(self.minimum(), min(self.maximum(), value)))



class OrderPanel(QtWidgets.QWidget):
    MARK_STALE_SECONDS = 5.0

    order_requested = Signal(object)
    open_workspace_requested = Signal()
    quick_settings_requested = Signal()
    minimum_content_height_changed = Signal(int)

    ORDER_TYPES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("LIMIT", "LIMIT"),
        ("MARKET", "MARKET"),
        ("STOP LIMIT", "STOP"),
        ("STOP MARKET", "STOP_MARKET"),
        ("TRAILING STOP", "TRAILING_STOP_MARKET"),
    )
    OPEN_SIZE_MODES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("QUANTITY", "CONTRACTS"),
        ("USDT VALUE", "QUOTE NOTIONAL"),
        ("AVAILABLE %", "BALANCE %"),
        ("RISK %", "RISK %"),
    )
    REDUCE_SIZE_MODES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("QUANTITY", "CONTRACTS"),
        ("POSITION %", "POSITION %"),
    )

    def __init__(
        self,
        gateway: TradingGatewayPort,
        compact: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.gateway = gateway
        self.testnet = gateway.testnet
        self.compact = compact
        self.symbol = DEFAULT_SYMBOL
        self.rules = SymbolRules()
        self.mark_price = 0.0
        self._last_mark_mono = 0.0
        self._market_live = False
        self._book_valid = False
        self._market_reason = "STARTING"
        self._gateway_state_text = ""
        self._submission_state = "READY"
        self._submission_detail = ""
        self._submission_request_id = ""
        self._submission_generation = 0
        self._protection_lifecycle = ""
        self._available_margin = 0.0
        self._account_mode_text = "MODE …"
        self._last_confirmed_leverage = 5
        self.protection_plans: dict[str, list[dict[str, float]]] = {"tp": [], "sl": []}
        self.hedge_mode: bool | None = None
        self.setObjectName("responsiveOrderTicket")
        self.setProperty("allowTradingTooltips", True)
        self._published_compact_minimum_height = 0


        self.setMinimumWidth(0 if compact else 350)
        self.setMinimumHeight(0 if compact else 430)
        layout = QtWidgets.QVBoxLayout(self)


        layout.setContentsMargins(7, 7, 7, 7)


        layout.setSpacing(0)
        group_gap = 12

        self.credentials_button = QtWidgets.QPushButton("API")
        self.credentials_button.setToolTip("Set session-only Binance API credentials")
        self.credentials_button.setVisible(False)

        account_row_widget = QtWidgets.QWidget(self)
        account_row_widget.setObjectName("tradingAccountContextRow")
        account_row = QtWidgets.QHBoxLayout(account_row_widget)
        account_row.setContentsMargins(5, 2, 5, 2)
        account_row.setSpacing(8)
        self.account_summary = ElidedLabel("AVAIL — USDT · MODE …")
        self.account_summary.setObjectName("tradeAvailableSummary")
        set_text_role(self.account_summary, TextRole.TRADING_TICKET_VALUE)
        self.account_summary.setToolTip(
            "Available cross collateral and account position mode"
        )
        self.quick_settings_button = QtWidgets.QToolButton()
        self.quick_settings_button.setObjectName("tradeSettingsMini")
        self.quick_settings_button.setFixedSize(20, 20)
        self.quick_settings_button.setIconSize(QtCore.QSize(12, 12))
        self.quick_settings_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.quick_settings_button.setAccessibleName("Trading settings")
        self.quick_settings_button.setToolTip(
            "Fast-order keys, Smart Exit, custom hotkeys, leverage, TP/SL, and close presets"
        )
        account_row.addWidget(self.account_summary, 1)
        account_row.addWidget(self.quick_settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(account_row_widget)
        layout.addSpacing(group_gap)

        side_row = QtWidgets.QHBoxLayout()
        side_row.setContentsMargins(5, 0, 5, 0)
        side_row.setSpacing(8)
        self.buy_button = QtWidgets.QPushButton("BUY / LONG")
        self.sell_button = QtWidgets.QPushButton("SELL / SHORT")
        self.buy_button.setObjectName("buySideButton")
        self.sell_button.setObjectName("sellSideButton")
        self.buy_button.setCheckable(True)
        self.sell_button.setCheckable(True)
        self.buy_button.setChecked(True)
        self.buy_button.setToolTip(
            "Send a BUY order. With Reduce Only, BUY reduces a short position."
        )
        self.sell_button.setToolTip(
            "Send a SELL order. With Reduce Only, SELL reduces a long position."
        )
        side_group = QtWidgets.QButtonGroup(self)
        side_group.setExclusive(True)
        side_group.addButton(self.buy_button)
        side_group.addButton(self.sell_button)
        for button in (self.buy_button, self.sell_button):
            set_text_role(button, TextRole.TRADING_TICKET)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setMinimumWidth(0)
            button.setMinimumHeight(34)
            button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            side_row.addWidget(button, 1)

        order_card = QtWidgets.QFrame()
        order_card.setObjectName("tradingPanelCard")
        order_card.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        order_card_layout = QtWidgets.QVBoxLayout(order_card)
        order_card_layout.setContentsMargins(5, 5, 5, 4)
        order_card_layout.setSpacing(7)
        self._order_card = order_card
        self._order_card_layout = order_card_layout
        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)
        form.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.type_combo = CompactTradeComboBox()
        self.type_combo.setObjectName("tradeTicketPrimaryCombo")
        set_text_role(self.type_combo, TextRole.TRADING_TICKET)
        for label, value in self.ORDER_TYPES:
            self.type_combo.addItem(label, value)
        self.type_combo.setToolTip(
            "Binance USD-M order type. Stop orders trigger from the selected price source."
        )
        self.type_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.type_combo.setMinimumContentsLength(6)
        self.size_mode = CompactTradeComboBox()
        self.size_mode.setObjectName("tradeTicketPrimaryCombo")
        set_text_role(self.size_mode, TextRole.TRADING_TICKET)
        for label, value in self.OPEN_SIZE_MODES:
            self.size_mode.addItem(label, value)
        self.size_mode.setToolTip(
            "Quantity: base asset · USDT Value: quote notional · Available %: collateral allocation at selected leverage · Risk %: collateral risk to the first stop."
        )
        self.size_mode.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.size_mode.setMinimumContentsLength(8)
        self.quantity_edit = QtWidgets.QLineEdit()
        self.quantity_edit.setPlaceholderText("Amount")
        self.price_edit = QtWidgets.QLineEdit()
        self.price_edit.setPlaceholderText("Price")
        self.trigger_edit = QtWidgets.QLineEdit()
        self.trigger_edit.setPlaceholderText("Trigger price")
        self.activation_edit = QtWidgets.QLineEdit()
        self.activation_edit.setPlaceholderText("Optional activation")
        for numeric_edit in (
            self.quantity_edit,
            self.price_edit,
            self.trigger_edit,
            self.activation_edit,
        ):
            set_text_role(numeric_edit, TextRole.TRADING_TICKET_VALUE)
        self.callback_rate = QtWidgets.QDoubleSpinBox()
        self.callback_rate.setRange(0.1, 10.0)
        self.callback_rate.setDecimals(1)
        self.callback_rate.setSingleStep(0.1)
        self.callback_rate.setValue(0.5)
        self.callback_rate.setSuffix(" %")
        self.working_type = QtWidgets.QComboBox()
        self.working_type.addItem("MARK PRICE", "MARK_PRICE")
        self.working_type.addItem("LAST PRICE", "CONTRACT_PRICE")
        self.working_type.setToolTip("Price source used to trigger conditional orders")

        def inline_mark_field(
            edit: QtWidgets.QLineEdit,
        ) -> tuple[QtWidgets.QWidget, QtWidgets.QToolButton]:
            widget = QtWidgets.QWidget()
            widget.setObjectName("tradingInlineField")
            row = QtWidgets.QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            mark = QtWidgets.QToolButton()
            mark.setText("MARK")
            mark.setObjectName("tradeMarkButton")
            mark.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            mark.setFixedWidth(50)
            set_text_role(mark, TextRole.TRADING_TICKET)
            mark.setToolTip("Use current mark price")
            row.addWidget(edit, 1)
            row.addWidget(mark)
            return widget, mark

        price_widget, self.price_mark_button = inline_mark_field(self.price_edit)
        trigger_widget, self.trigger_mark_button = inline_mark_field(self.trigger_edit)
        activation_widget, self.activation_mark_button = inline_mark_field(
            self.activation_edit
        )

        self.time_in_force_values = ("GTC", "IOC", "FOK", "GTX")
        self.time_in_force = CompactTradeComboBox()
        self.time_in_force.setObjectName("timeInForceCycle")
        set_text_role(self.time_in_force, TextRole.TRADING_TICKET)
        self.time_in_force.addItem("GTC", "GTC")
        self.time_in_force.addItem("IOC", "IOC")
        self.time_in_force.addItem("FOK", "FOK")
        self.time_in_force.addItem("GTX", "GTX")
        self.time_in_force.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.time_in_force.setToolTip(
            "Time in force. GTX is Binance post-only and must rest as maker liquidity."
        )



        self.position_side = QtWidgets.QComboBox(self)
        self.position_side.addItems(("BOTH", "LONG", "SHORT"))
        self.position_side.setToolTip(
            "BOTH for one-way mode; LONG or SHORT for Binance hedge mode"
        )
        self.position_side.setVisible(False)
        self.reduce_only = QtWidgets.QCheckBox("REDUCE ONLY")
        self.reduce_only.setObjectName("reduceOnlyCheck")
        self.reduce_only.setToolTip(
            "Prevent the order from increasing exposure. BUY reduces SHORT; SELL reduces LONG."
        )
        self.reduce_only.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_text_role(self.reduce_only, TextRole.TRADING_TICKET)
        self.protection_button = QtWidgets.QPushButton("TP / SL")
        self.protection_button.setObjectName("protectionButton")
        self.protection_button.setProperty("active", False)
        self.protection_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_text_role(self.protection_button, TextRole.TRADING_TICKET)
        self.protection_button.setToolTip(
            "Attach up to four take-profits and four stop-losses after entry fill"
        )

        self._order_form = form
        self._form_controls = {
            "type": self.type_combo,
            "size": self.size_mode,
            "amount": self.quantity_edit,
            "price": price_widget,
            "trigger": trigger_widget,
            "activation": activation_widget,
            "callback": self.callback_rate,
            "source": self.working_type,
        }
        accessible_names = {
            "type": "Order type",
            "size": "Amount mode",
            "amount": "Amount",
            "price": "Price",
            "trigger": "Trigger price",
            "activation": "Activation price",
            "callback": "Callback rate",
            "source": "Trigger source",
        }
        for name, control in self._form_controls.items():
            if not control.accessibleName():
                control.setAccessibleName(accessible_names[name])
        self.time_in_force.setAccessibleName("Time in force")
        for column in range(4):
            form.setColumnStretch(column, 1)
        order_card_layout.addLayout(form)


        self.margin_bar = QtWidgets.QFrame()
        self.margin_bar.setObjectName("tradingMarginRow")
        margin_row = QtWidgets.QHBoxLayout(self.margin_bar)
        margin_row.setContentsMargins(5, 2, 5, 2)
        margin_row.setSpacing(9)
        self.size_presets: list[QtWidgets.QPushButton] = []
        for percent in (25, 50, 75, 100):
            button = QtWidgets.QPushButton(f"{percent}%")
            button.setObjectName("sizePresetButton")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            set_text_role(button, TextRole.TRADING_TICKET)
            button.setToolTip(
                f"Use {percent}% of available margin for an opening order, "
                f"or {percent}% of the target position when Reduce Only is enabled."
            )
            button.clicked.connect(
                lambda _checked=False, value=percent: self._apply_size_preset(value)
            )
            self.size_presets.append(button)
            margin_row.addWidget(button, 1)

        self.leverage = LeverageComboBox()
        self.leverage.setValue(5)
        self.leverage.setToolTip(
            "Requested cross leverage. Choose a common value, or open the list and start typing a custom 1–125× value."
        )
        set_text_role(self.leverage, TextRole.TRADING_TICKET)

        self.options_bar = QtWidgets.QFrame()
        self.options_bar.setObjectName("tradingControlRow")
        options_row = QtWidgets.QHBoxLayout(self.options_bar)
        options_row.setContentsMargins(5, 2, 5, 2)
        options_row.setSpacing(10)
        options_row.addWidget(self.reduce_only)
        options_row.addWidget(self.protection_button)
        options_row.addStretch(1)
        options_row.addWidget(self.time_in_force)
        options_row.addWidget(self.leverage)

        controls_cluster = QtWidgets.QVBoxLayout()
        controls_cluster.setContentsMargins(0, 0, 0, 0)
        controls_cluster.setSpacing(8)
        controls_cluster.addWidget(order_card)
        controls_cluster.addWidget(self.options_bar)
        controls_cluster.addWidget(self.margin_bar)
        layout.addLayout(controls_cluster)
        layout.addSpacing(group_gap)

        feedback_cluster = QtWidgets.QVBoxLayout()
        feedback_cluster.setContentsMargins(0, 0, 0, 0)
        feedback_cluster.setSpacing(3)
        self.execution_state_label = QtWidgets.QLabel("")
        self.execution_state_label.setObjectName("tradeExecutionState")
        self.execution_state_label.setWordWrap(True)
        set_text_role(self.execution_state_label, TextRole.TRADING_TICKET)
        feedback_cluster.addWidget(self.execution_state_label)
        self.reconcile_button = QtWidgets.QPushButton("RECONCILE UNKNOWN ORDERS")
        self.reconcile_button.setToolTip("Query existing client order IDs; this does not resend placements.")
        self.reconcile_button.clicked.connect(self.gateway.reconcile_unknown_orders)
        self.reconcile_button.hide()
        feedback_cluster.addWidget(self.reconcile_button)
        self.risk_size_hint = QtWidgets.QLabel("")
        self.risk_size_hint.setObjectName("subtleLabel")
        self.risk_size_hint.setWordWrap(True)
        set_text_role(self.risk_size_hint, TextRole.TRADING_TICKET)
        self.risk_size_hint.hide()
        feedback_cluster.addWidget(self.risk_size_hint)
        self.protection_summary = ElidedLabel("")
        self.protection_summary.setObjectName("tradeAccountSummary")
        set_text_role(self.protection_summary, TextRole.TRADING_TICKET_VALUE)
        self.protection_summary.hide()
        feedback_cluster.addWidget(self.protection_summary)
        self.validation_label = ElidedLabel("")
        self.validation_label.setObjectName("tradeValidation")
        set_text_role(self.validation_label, TextRole.TRADING_TICKET)
        self.validation_label.hide()
        feedback_cluster.addWidget(self.validation_label)
        layout.addLayout(feedback_cluster)
        layout.addSpacing(group_gap)



        layout.addLayout(side_row)
        layout.addStretch(1)
        self.credentials_button.clicked.connect(self.edit_credentials)
        self.quick_settings_button.clicked.connect(self.quick_settings_requested)
        self.type_combo.currentIndexChanged.connect(
            lambda _index: self._type_changed(self.current_order_type())
        )
        self.position_side.currentTextChanged.connect(self._position_side_changed)
        self.size_mode.currentIndexChanged.connect(
            lambda _index: self._size_mode_changed(str(self.size_mode.currentData()))
        )
        self.price_mark_button.clicked.connect(
            lambda: self._copy_mark_to(self.price_edit)
        )
        self.trigger_mark_button.clicked.connect(
            lambda: self._copy_mark_to(self.trigger_edit)
        )
        self.activation_mark_button.clicked.connect(
            lambda: self._copy_mark_to(self.activation_edit)
        )
        self.time_in_force.currentIndexChanged.connect(lambda _index: self._update_order_summary())
        self.protection_button.clicked.connect(self.edit_protections)
        self.reduce_only.toggled.connect(self._intent_changed)
        self._leverage_apply_timer = QTimer(self)
        self._leverage_apply_timer.setSingleShot(True)
        self._leverage_apply_timer.setInterval(220)
        self._leverage_apply_timer.timeout.connect(
            lambda: self._request_leverage(self.leverage.value())
        )
        self.leverage.valueChanged.connect(self._leverage_value_changed)
        self.buy_button.toggled.connect(lambda _checked: self._sync_auto_position_side())
        self.sell_button.toggled.connect(lambda _checked: self._sync_auto_position_side())
        self.buy_button.clicked.connect(
            lambda _checked=False: self._submit_from_side(True)
        )
        self.sell_button.clicked.connect(
            lambda _checked=False: self._submit_from_side(False)
        )
        for editor in (self.quantity_edit, self.price_edit, self.trigger_edit, self.activation_edit):
            editor.textChanged.connect(lambda _text: self._update_order_summary())
        self.callback_rate.valueChanged.connect(lambda _value: self._update_order_summary())
        self.working_type.currentIndexChanged.connect(lambda _index: self._update_order_summary())
        self.gateway.state_changed.connect(self._gateway_state)
        self.gateway.request_succeeded.connect(self._execution_request_changed)
        self.gateway.request_failed.connect(self._execution_request_changed)
        self.gateway.armed_changed.connect(self._armed_changed)
        self.gateway.credentials_changed.connect(self._prepare_manual_trading)
        self.gateway.snapshot_ready.connect(self.apply_account_snapshot)
        self.gateway.leverage_changing.connect(self._leverage_changing)
        self.gateway.leverage_changed.connect(self._leverage_changed)
        self._readiness_fresh_state = self._mark_is_fresh()
        self._readiness_timer = QTimer(self)
        self._readiness_timer.setInterval(1000)
        self._readiness_timer.timeout.connect(self._refresh_execution_freshness)
        self._readiness_timer.start()
        self._type_changed(self.current_order_type())
        self._size_mode_changed(str(self.size_mode.currentData()))
        self._position_cache: list[dict[str, Any]] = []
        self._update_submit_text()
        self._update_execution_state()
        self._update_order_summary()

    def compact_required_height(self) -> int:
        """Return the ticket's current unsqueezed content height.

        The right rail owns splitter geometry; this value is only a feature
        usability floor so an adjacent panel cannot compress the ticket until
        its fixed row rhythm or equal top/bottom inset is lost.
        """
        if not self.compact:
            return max(0, self.minimumSizeHint().height())
        self.ensurePolished()
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        return max(
            1,
            layout.minimumSize().height(),
            self.minimumSizeHint().height(),
        )

    def _publish_compact_minimum_height(self) -> None:
        if not self.compact:
            return
        required = self.compact_required_height()
        if required == self._published_compact_minimum_height:
            return
        self._published_compact_minimum_height = required
        self.minimum_content_height_changed.emit(required)

    def sync_compact_fixed_height(self) -> int:
        """Compatibility shim: measure the current ticket without fixing geometry."""
        return self.compact_required_height() if self.compact else self.height()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if self.compact and event.type() in {
            QtCore.QEvent.Type.StyleChange,
            QtCore.QEvent.Type.FontChange,
        }:
            QTimer.singleShot(0, self._publish_compact_minimum_height)

    def set_symbol(self, symbol: str, rules: SymbolRules) -> None:
        changed = symbol != self.symbol
        self.symbol = symbol
        self.rules = rules
        if changed:
            self.mark_price = 0.0
            self._last_mark_mono = 0.0
            self._position_cache = []
            self.quantity_edit.clear()
            self.price_edit.clear()
            self.trigger_edit.clear()
            self.activation_edit.clear()
            self.protection_plans = {"tp": [], "sl": []}
            self._protection_lifecycle = ""
            self._update_protection_label()
        price_rule = f"Tick {_exchange_step_text(rules.tick_size)} · range {format_price(rules.min_price)}–{format_price(rules.max_price)}"
        qty_rule = f"Lot {_exchange_step_text(rules.lot_step)} · market lot {_exchange_step_text(rules.market_step)}"
        self.price_edit.setToolTip(price_rule)
        self.trigger_edit.setToolTip(price_rule)
        self.activation_edit.setToolTip(price_rule)
        self.quantity_edit.setToolTip(qty_rule)
        self.setToolTip(
            f"{symbol} · quote {rules.quote_asset} · margin {rules.margin_asset} · cross margin only"
        )
        self._prepare_manual_trading()
        self._update_execution_state()
        self._update_order_summary()

    def _prepare_manual_trading(self, *_args) -> None:
        if self.gateway.has_credentials() and not self.gateway.stopping:
            self.gateway.ensure_cross(self.symbol)
            if not self.gateway.account_loaded:
                self.gateway.refresh_account(self.symbol)

    def set_mark_price(self, price: float) -> None:
        self.mark_price = price
        if price > 0:
            self._last_mark_mono = time.monotonic()
        self._readiness_fresh_state = self._mark_is_fresh()
        for row in self._position_cache:
            _update_position_mark(row, price)
        self._refresh_account_summary()
        order_type = self.current_order_type()
        self._sync_mark_controls()
        self._update_execution_state()
        self._update_order_summary()

    def set_dense(self, dense: bool) -> None:
        """Compatibility hook; the embedded ticket now has one stable layout."""
        _ = dense

    def _apply_leverage_now(self) -> None:
        self._leverage_apply_timer.stop()
        self._request_leverage(self.leverage.value())

    def _request_leverage(self, leverage: int) -> None:
        leverage = max(self.leverage.minimum(), min(self.leverage.maximum(), int(leverage)))
        blocker = QtCore.QSignalBlocker(self.leverage)
        self.leverage.setValue(leverage)
        del blocker
        if not self.gateway.has_credentials():
            self.gateway.problem.emit("Add Binance API credentials before changing leverage.")
            return
        current = self.gateway.current_leverage(self.symbol)
        if current == leverage and self.symbol in self.gateway.cross_ready:
            self._update_execution_state()
            return
        self.gateway.apply_cross_leverage(self.symbol, leverage)

    def _leverage_value_changed(self, leverage: int) -> None:
        self._leverage_apply_timer.start()

    def _leverage_changing(self, symbol: str, leverage: int) -> None:
        if symbol == self.symbol:
            self._update_execution_state()

    def _leverage_changed(
        self, symbol: str, leverage: int, succeeded: bool, message: str
    ) -> None:
        if symbol != self.symbol:
            return
        if succeeded:
            self._last_confirmed_leverage = leverage
            blocker = QtCore.QSignalBlocker(self.leverage)
            self.leverage.setValue(leverage)
            del blocker
            self._update_execution_state()
        elif message:
            confirmed = self.gateway.current_leverage(symbol) or self._last_confirmed_leverage
            blocker = QtCore.QSignalBlocker(self.leverage)
            self.leverage.setValue(confirmed)
            del blocker
            self._gateway_state_text = message
            self._update_execution_state()

    def _copy_mark_to(self, target: QtWidgets.QLineEdit) -> None:
        if not self._mark_is_fresh():
            self._set_validation_message(
                "MARK PRICE UNAVAILABLE" if self.mark_price <= 0 else "MARK PRICE STALE"
            )
            return
        try:
            text = quantize_step(str(self.mark_price), self.rules.tick_size)
        except ValueError:
            text = format_price(self.mark_price).replace(",", "")
        target.setText(text)
        self._set_validation_message("")
        self._update_order_summary()

    def current_time_in_force(self) -> str:
        value = self.time_in_force.currentData()
        return str(value or self.time_in_force.currentText()).split(" ·", 1)[0]


    def set_orders_drawer_open(self, opened: bool) -> None:


        _ = bool(opened)

    def set_settings_icon(self, color: str) -> None:
        self.quick_settings_button.setIcon(
            line_icon("gear", color, max(1.0, self.devicePixelRatioF()))
        )

    def current_order_type(self) -> str:
        return str(self.type_combo.currentData() or self.type_combo.currentText())

    def edit_credentials(self) -> None:
        dialog = CredentialsDialog(self.gateway.api_key, self.gateway.api_secret, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.gateway.set_credentials(*dialog.credentials())

    def _armed_changed(self, _armed: bool) -> None:


        self._update_execution_state()

    def _gateway_state(self, state: str) -> None:
        self._gateway_state_text = str(state or "")
        self._update_execution_state()

    def _type_changed(self, order_type: str) -> None:
        limit_price = order_type in {"LIMIT", "STOP"}
        conditional = order_type in CONDITIONAL_ORDER_TYPES
        trailing = order_type == "TRAILING_STOP_MARKET"
        self.time_in_force.setVisible(limit_price)
        self._sync_mark_controls(order_type)
        active_fields = ["type", "size", "amount"]
        if limit_price:
            active_fields.append("tif")
        if trailing:
            active_fields.extend(("activation", "callback", "source"))
        else:
            if limit_price:
                active_fields.append("price")
            if conditional:
                active_fields.extend(("trigger", "source"))
        self._reflow_order_fields(active_fields)
        self._update_order_summary()
        self._update_execution_state()

    def _reflow_order_fields(self, active_fields: list[str]) -> None:
        active = set(active_fields)
        form = self._order_form
        for control in self._form_controls.values():
            form.removeWidget(control)
            control.hide()




        self.type_combo.show()
        self.size_mode.show()
        form.addWidget(self.type_combo, 0, 0, 1, 2)
        form.addWidget(self.size_mode, 0, 2, 1, 2)
        self.time_in_force.setVisible("tif" in active)

        row = 1
        self.quantity_edit.show()
        if "price" in active:
            form.addWidget(self.quantity_edit, row, 0, 1, 2)
            form.addWidget(self._form_controls["price"], row, 2, 1, 2)
            self._form_controls["price"].show()
            row += 1
        else:
            form.addWidget(self.quantity_edit, row, 0, 1, 4)
            row += 1

        if "trigger" in active:
            form.addWidget(self._form_controls["trigger"], row, 0, 1, 2)
            form.addWidget(self.working_type, row, 2, 1, 2)
            self._form_controls["trigger"].show()
            self.working_type.show()
            row += 1
        elif "activation" in active:
            form.addWidget(self._form_controls["activation"], row, 0, 1, 2)
            form.addWidget(self.callback_rate, row, 2)
            form.addWidget(self.working_type, row, 3)
            self._form_controls["activation"].show()
            self.callback_rate.show()
            self.working_type.show()
            row += 1

        form.invalidate()
        form.activate()
        self._order_card_layout.invalidate()
        self._order_card_layout.activate()
        target_height = max(
            self._order_card_layout.sizeHint().height(),
            self._order_card_layout.minimumSize().height(),
        )





        self._order_card.setMinimumHeight(max(36, target_height + 2))
        self._order_card.updateGeometry()
        if self.compact:
            QTimer.singleShot(0, self._publish_compact_minimum_height)

    def _intent_changed(self, reducing: bool) -> None:
        current_type = self.current_order_type()
        values = self.ORDER_TYPES
        allowed = {value for _label, value in self.ORDER_TYPES}
        if current_type not in allowed:
            current_type = "MARKET"
        blocker = QtCore.QSignalBlocker(self.type_combo)
        self.type_combo.clear()
        for label, value in values:
            self.type_combo.addItem(label, value)
        index = self.type_combo.findData(current_type)
        self.type_combo.setCurrentIndex(max(0, index))
        del blocker
        current_mode = str(self.size_mode.currentData() or "CONTRACTS")
        size_values = self.REDUCE_SIZE_MODES if reducing else self.OPEN_SIZE_MODES
        allowed_modes = {value for _label, value in size_values}
        if current_mode not in allowed_modes:
            current_mode = "CONTRACTS"
        blocker = QtCore.QSignalBlocker(self.size_mode)
        self.size_mode.clear()
        for label, value in size_values:
            self.size_mode.addItem(label, value)
        index = self.size_mode.findData(current_mode)
        self.size_mode.setCurrentIndex(max(0, index))
        del blocker


        self.protection_button.setVisible(not reducing)
        self.protection_button.setEnabled(not reducing)
        self._type_changed(self.current_order_type())
        self._size_mode_changed(str(self.size_mode.currentData()))
        self._sync_auto_position_side()
        self._update_submit_text()
        self._update_order_summary()
        self._update_execution_state()

    def _update_submit_text(self) -> None:
        """Keep the two bottom execution actions aligned with OPEN/REDUCE intent."""
        if self.reduce_only.isChecked():
            self.buy_button.setText("CLOSE SHORT")
            self.sell_button.setText("CLOSE LONG")
        else:
            self.buy_button.setText("BUY / LONG")
            self.sell_button.setText("SELL / SHORT")

    def _submit_from_side(self, buying: bool) -> None:
        """Select the requested side and submit from the final execution row."""
        target = self.buy_button if buying else self.sell_button
        if not target.isChecked():
            target.setChecked(True)
        self._sync_auto_position_side()
        self.prepare_order()

    def _apply_size_preset(self, percent: int) -> None:
        """Apply one-click margin/position sizing without changing exchange semantics."""
        percent = max(1, min(100, int(percent)))
        target_mode = "POSITION %" if self.reduce_only.isChecked() else "BALANCE %"
        index = self.size_mode.findData(target_mode)
        if index >= 0:
            self.size_mode.setCurrentIndex(index)
        self.quantity_edit.setText(str(percent))
        self.quantity_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.quantity_edit.selectAll()

    def _position_side_changed(self, position_side: str) -> None:
        _ = position_side
        self.reduce_only.setEnabled(True)

    def _sync_auto_position_side(self) -> None:
        if self.hedge_mode:
            if self.reduce_only.isChecked():
                value = "SHORT" if self.buy_button.isChecked() else "LONG"
            else:
                value = "LONG" if self.buy_button.isChecked() else "SHORT"
        else:
            value = "BOTH"
        blocker = QtCore.QSignalBlocker(self.position_side)
        self.position_side.setCurrentText(value)
        del blocker
        self._position_side_changed(value)
        self._update_submit_text()
        self._update_order_summary()

    def apply_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        mode = snapshot.get("positionMode") or {}
        if "dualSidePosition" in mode:
            value = mode.get("dualSidePosition")
            self.hedge_mode = value if isinstance(value, bool) else str(value).lower() == "true"
        self._sync_auto_position_side()
        account = snapshot.get("account") or {}
        available = max(0.0, safe_float(account.get("availableBalance")))
        if available <= 0:
            margin_row = next(
                (
                    row
                    for row in account.get("assets", [])
                    if str(row.get("asset") or "") == self.rules.margin_asset
                ),
                {},
            )
            available = max(0.0, safe_float(margin_row.get("availableBalance")))
        self._available_margin = available
        self._position_cache = []
        active_leverage = 0
        for row in account.get("positions", []):
            if str(row.get("symbol")) != self.symbol:
                continue
            if abs(safe_float(row.get("positionAmt"))) > 0:
                self._position_cache.append(dict(row))
            active_leverage = max(active_leverage, int(safe_float(row.get("leverage"))))
        if active_leverage:
            self._last_confirmed_leverage = active_leverage
            blocker = QtCore.QSignalBlocker(self.leverage)
            self.leverage.setValue(active_leverage)
            del blocker
        self._account_mode_text = (
            "HEDGE"
            if self.hedge_mode
            else "ONE-WAY"
            if self.hedge_mode is False
            else "MODE UNKNOWN"
        )
        self._refresh_account_summary()
        self._update_execution_state()
        self._update_order_summary()

    def _refresh_account_summary(self) -> None:
        margin_asset = str(getattr(self.rules, "margin_asset", "") or "USDT").strip() or "USDT"
        _set_text_if_changed(
            self.account_summary,
            f"AVAIL {human_number(self._available_margin, money=True)} {margin_asset} · "
            f"{self._account_mode_text}",
        )

    def _size_mode_changed(self, mode: str) -> None:
        previous = getattr(self, "_quantity_unit", mode)
        self._quantity_unit = mode
        if previous != mode:
            self.quantity_edit.clear()
        self.quantity_edit.setPlaceholderText("Amount")
        self._update_order_summary()

    def edit_protections(self) -> None:
        dialog = ProtectionEditorDialog(self.protection_plans, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.protection_plans = dialog.plans()
            self._update_protection_label()

    def _update_protection_label(self) -> None:
        tp = list(self.protection_plans.get("tp", []))
        sl = list(self.protection_plans.get("sl", []))
        tp_count, sl_count = len(tp), len(sl)
        _set_text_if_changed(
            self.protection_button,
            f"TP {tp_count} · SL {sl_count}" if tp_count or sl_count else "TP / SL",
        )
        _set_repolished_property(
            self.protection_button, "active", bool(tp_count or sl_count)
        )
        details: list[str] = []
        for label, targets in (("TP", tp), ("SL", sl)):
            for target in targets[:4]:
                price = safe_float(target.get("price"))
                percent = int(round(safe_float(target.get("percent"))))
                if price > 0 and percent > 0:
                    details.append(f"{label} {format_price(price)} · {percent}%")
        text = "AFTER ENTRY FILLS · " + "  |  ".join(details) if details else ""
        if self._protection_lifecycle:
            text = (text + "  |  " if text else "") + self._protection_lifecycle
        _set_text_if_changed(self.protection_summary, text)
        if self.protection_summary.toolTip() != text:
            self.protection_summary.setToolTip(text)
        if self.protection_summary.isVisible() != bool(text):
            self.protection_summary.setVisible(bool(text))
        self._update_order_summary()

    def set_execution_market_state(self, live: bool, book_valid: bool, reason: str = "") -> None:
        self._market_live = bool(live)
        self._book_valid = bool(book_valid)
        self._market_reason = str(reason or ("READY" if book_valid else "SYNCING"))
        self._update_execution_state()
        self._update_order_summary()

    def set_submission_state(self, state: str, detail: str = "", request_id: str = "") -> None:
        self._submission_state = str(state or "READY").upper()
        self._submission_detail = str(detail or "")
        self._submission_request_id = str(request_id or "")
        self._submission_generation += 1
        generation = self._submission_generation
        self._update_execution_state()
        self._update_submit_text()



        persistent = {"SENDING", "OUTCOME UNKNOWN"}
        if self._submission_state not in {"READY", "IDLE", *persistent}:
            delay = 3500
            def clear_if_current() -> None:
                if self._submission_generation != generation:
                    return
                self._submission_state = "READY"
                self._submission_detail = ""
                self._submission_request_id = ""
                self._submission_generation += 1
                self._update_execution_state()
                self._update_submit_text()
            QTimer.singleShot(delay, clear_if_current)

    def set_protection_lifecycle(self, text: str) -> None:
        self._protection_lifecycle = str(text or "").upper()
        self._update_protection_label()

    def _set_validation_message(self, text: str, *, blocked: bool = True) -> None:
        text = str(text or "")
        _set_repolished_property(self.validation_label, "blocked", bool(text) and blocked)
        _set_text_if_changed(self.validation_label, text)
        if self.validation_label.toolTip() != text:
            self.validation_label.setToolTip(text)
        if self.validation_label.isVisible() != bool(text):
            self.validation_label.setVisible(bool(text))

    def _mark_is_fresh(self) -> bool:
        return bool(
            self.mark_price > 0
            and self._last_mark_mono > 0
            and time.monotonic() - self._last_mark_mono <= self.MARK_STALE_SECONDS
        )

    def _refresh_execution_freshness(self) -> None:
        fresh = self._mark_is_fresh()
        if fresh == self._readiness_fresh_state:
            return
        self._readiness_fresh_state = fresh
        self._sync_mark_controls()
        self._update_execution_state()
        self._update_order_summary()

    def _sync_mark_controls(self, order_type: str | None = None) -> None:
        order_type = str(order_type or self.current_order_type())
        fresh = self._mark_is_fresh()
        limit_price = order_type in {"LIMIT", "STOP"}
        conditional = order_type in CONDITIONAL_ORDER_TYPES
        trailing = order_type == "TRAILING_STOP_MARKET"
        self.price_mark_button.setEnabled(fresh and limit_price)
        self.trigger_mark_button.setEnabled(fresh and conditional and not trailing)
        self.activation_mark_button.setEnabled(fresh and trailing)

    def _leverage_state_text(self) -> str:
        requested = int(self.leverage.value())
        confirmed = int(self.gateway.current_leverage(self.symbol))
        if self._leverage_apply_timer.isActive() or self.symbol in self.gateway.cross_pending:
            return f"CROSS {requested}× APPLYING"
        if confirmed <= 0:
            return "LEVERAGE UNKNOWN"
        if requested != confirmed:
            return f"CROSS {confirmed}× · REQUEST {requested}×"
        return f"CROSS {confirmed}× CONFIRMED"

    def _execution_request_changed(self, *_args: object) -> None:
        self._update_execution_state()

    def _update_execution_state(self) -> None:
        venue = "TESTNET" if self.testnet else "LIVE"
        if not self._market_live:
            market = "MARKET OFFLINE"
        elif not self._book_valid:
            market = "BOOK SYNCING"
        elif self.mark_price <= 0:
            market = "MARK UNAVAILABLE"
        elif not self._mark_is_fresh():
            market = "MARK STALE"
        else:
            market = "MARKET LIVE"
        used, capacity, unknown = self.gateway.placement_status()
        submission = self._submission_state if self._submission_state not in {"", "IDLE"} else "READY"
        if unknown:
            submission = "OUTCOME UNKNOWN"
        elif used and submission == "READY":
            submission = "SENDING"
        parts = [submission, f"PLACEMENTS {used}/{capacity}", f"UNKNOWN {unknown}"]
        parts.extend((market, self._leverage_state_text()))
        detail = self._market_reason
        if self._submission_detail:
            detail = (detail + " · " if detail else "") + self._submission_detail
        if self._submission_request_id:
            detail = (detail + " · " if detail else "") + self._submission_request_id
        quick = "QUICK ORDERS UNLOCKED" if self.gateway.armed else "QUICK ORDERS LOCKED"
        gateway_detail = self._gateway_state_text.strip()
        tooltip = f"{venue} · {quick} · {' · '.join(parts)}"
        if detail:
            tooltip += f" · {detail}"
        if gateway_detail:
            tooltip += f" · {gateway_detail}"
        visible = f"{submission} · PLACEMENTS {used}/{capacity}"
        if unknown:
            visible += f" · {unknown} UNKNOWN"
        elif self._submission_state == "SENDING" and self._submission_detail:
            visible += f"\n{self._submission_detail}"
        if not self.gateway.has_credentials():
            visible += "\nAPI CREDENTIALS REQUIRED"
        visible += f"\n{venue} · {market} · {quick}\n{self._leverage_state_text()}"
        state_changed = _set_text_if_changed(self.execution_state_label, visible)
        if self.execution_state_label.toolTip() != tooltip:
            self.execution_state_label.setToolTip(tooltip)
        uncertain = submission == "OUTCOME UNKNOWN"
        _set_repolished_property(self.execution_state_label, "attention",
                                uncertain or self._submission_state in {"REJECTED", "FAILED"})
        reconcile_changed = self.reconcile_button.isHidden() == uncertain
        if reconcile_changed:
            self.reconcile_button.setVisible(uncertain)
        if self.compact and (state_changed or reconcile_changed):
            QTimer.singleShot(0, self._publish_compact_minimum_height)
        self.reconcile_button.setEnabled(self.gateway.has_credentials() and not self.gateway.stopping)
        if self.quick_settings_button.toolTip() != tooltip:
            self.quick_settings_button.setToolTip(tooltip)

    def _manual_market_block_reason(self, order_type: str, reducing: bool) -> str:
        if reducing:
            return ""
        if order_type == "MARKET":
            if not self._market_live:
                return "OPEN MARKET ORDER BLOCKED · MARKET DATA OFFLINE"
            if not self._book_valid:
                return "OPEN MARKET ORDER BLOCKED · ORDER BOOK SYNCING"
            if not self._mark_is_fresh():
                return "OPEN MARKET ORDER BLOCKED · MARK PRICE STALE"
        if order_type in CONDITIONAL_ORDER_TYPES:
            if not self._market_live:
                return "CONDITIONAL ORDER BLOCKED · MARKET DATA OFFLINE"
            if not self._mark_is_fresh():
                return "CONDITIONAL ORDER BLOCKED · MARK PRICE STALE"
        return ""

    def _update_order_summary(self) -> None:
        risk_mode = self.size_mode.currentData() == "RISK %"
        stops = self.protection_plans.get("sl", [])
        risk_text = (f"RISK % USES FIRST STOP · {format_price(safe_float(stops[0].get('price')))}"
                     if stops else "RISK % REQUIRES A FIRST STOP-LOSS TARGET")
        _set_text_if_changed(self.risk_size_hint, risk_text if risk_mode else "")
        self.risk_size_hint.setVisible(risk_mode)
        reducing = self.reduce_only.isChecked()
        buying = self.buy_button.isChecked()
        order_type = self.current_order_type()
        preview_error = ""
        try:
            if order_type in {"LIMIT", "STOP"} and self.price_edit.text().strip():
                self._validated_price(self.price_edit.text().replace(",", "").strip(), "Order price")
            if order_type in CONDITIONAL_ORDER_TYPES and order_type != "TRAILING_STOP_MARKET" and self.trigger_edit.text().strip():
                self._validated_price(self.trigger_edit.text().replace(",", "").strip(), "Trigger price")
            if order_type == "TRAILING_STOP_MARKET" and self.activation_edit.text().strip():
                self._validated_price(self.activation_edit.text().replace(",", "").strip(), "Activation price")
            if self.quantity_edit.text().strip():
                self._quantity(order_type)
            elif reducing:
                available = self._reduced_position_size()
                if available <= 0 and getattr(self.gateway, "account_loaded", False):
                    target = "SHORT" if buying else "LONG"
                    preview_error = f"NO {target} POSITION AVAILABLE TO REDUCE"
        except (ValueError, TypeError) as exc:
            preview_error = str(exc)
        block = self._manual_market_block_reason(order_type, reducing)
        if block:
            self._set_validation_message(block)
        elif preview_error:
            self._set_validation_message(preview_error)
        elif order_type == "LIMIT" and not reducing and not self._mark_is_fresh():
            self._set_validation_message("LIMIT READY TO DRAFT · MARK STALE · PRICE BAND NOT VERIFIED", blocked=False)
        elif self.validation_label.text():
            self._set_validation_message("")

    def apply_external_price_prefill(self, price: float) -> tuple[bool, str]:
        order_type = self.current_order_type()
        if order_type == "TRAILING_STOP_MARKET":
            target, label = self.activation_edit, "ACTIVATION"
        elif order_type in CONDITIONAL_ORDER_TYPES:
            target, label = self.trigger_edit, "TRIGGER"
        else:
            target, label = self.price_edit, "PRICE"
        if target.text().strip() and not target.hasFocus():
            return False, label
        try:
            text = quantize_step(str(float(price)), self.rules.tick_size)
        except (ValueError, TypeError):
            text = format_price(price).replace(",", "")
        target.setText(text)
        if target.hasFocus():
            target.selectAll()
        self._set_validation_message("")
        self._update_order_summary()
        return True, label

    def _entry_reference(self, order_type: str) -> float:
        if order_type in {"LIMIT", "STOP"}:
            return safe_float(self.price_edit.text().replace(",", ""))
        if order_type == "STOP_MARKET":
            return safe_float(self.trigger_edit.text().replace(",", ""))
        if order_type == "TRAILING_STOP_MARKET":
            activation = safe_float(self.activation_edit.text().replace(",", ""))
            if activation > 0:
                return activation
        return self.mark_price

    def _reduced_position_size(self) -> float:
        direction = "SHORT" if self.buy_button.isChecked() else "LONG"
        total = 0.0
        for row in self._position_cache:
            amount = safe_float(row.get("positionAmt"))
            position_side = str(row.get("positionSide") or "BOTH")
            if position_side == direction or (
                position_side == "BOTH"
                and ((direction == "LONG" and amount > 0) or (direction == "SHORT" and amount < 0))
            ):
                total += abs(amount)
        return total

    def _quantity(self, order_type: str) -> str:
        raw = safe_float(self.quantity_edit.text().replace(",", ""))
        if raw <= 0:
            raise ValueError("Enter a positive order size.")
        reference = self._entry_reference(order_type)
        mode = str(self.size_mode.currentData())
        if mode in {"BALANCE %", "RISK %", "POSITION %"} and raw > 100:
            raise ValueError("Percentage sizing cannot exceed 100%.")
        if mode == "CONTRACTS":
            quantity_value = raw
        elif mode == "POSITION %":
            direction = "SHORT" if self.buy_button.isChecked() else "LONG"
            available_position = self._reduced_position_size()
            if available_position <= 0:
                raise ValueError(f"There is no {direction.lower()} position to reduce.")
            quantity_value = available_position * raw / 100.0
        elif mode == "QUOTE NOTIONAL":
            if reference <= 0:
                raise ValueError("A current or limit price is required for notional sizing.")
            quantity_value = raw / reference
        else:
            balance = self.gateway.available_balance(self.rules.margin_asset)
            if balance <= 0:
                raise ValueError("Load account balances before using balance- or risk-based sizing.")
            if mode == "BALANCE %":
                if reference <= 0:
                    raise ValueError("A current or limit price is required for balance sizing.")
                leverage = self.gateway.current_leverage(self.symbol)
                if leverage <= 0:
                    raise ValueError("Load the confirmed Binance leverage before using Available %.")
                quantity_value = balance * raw / 100.0 * leverage / reference
            else:
                stops = self.protection_plans.get("sl", [])
                if not stops or reference <= 0:
                    raise ValueError("Risk sizing needs an entry price and at least one stop-loss target.")
                distance = abs(reference - safe_float(stops[0].get("price")))
                if distance <= 0:
                    raise ValueError("The first stop-loss must differ from the entry price.")
                quantity_value = balance * raw / 100.0 / distance
        if self.reduce_only.isChecked():
            direction = "SHORT" if self.buy_button.isChecked() else "LONG"
            available_position = self._reduced_position_size()
            if available_position <= 0:
                raise ValueError(f"There is no {direction.lower()} position to reduce.")
            if quantity_value > available_position + 1e-12:
                raise ValueError(
                    f"Reduce size exceeds the active {direction.lower()} position "
                    f"({human_number(available_position)})."
                )
        market_quantity = order_type in {
            "MARKET", "STOP_MARKET", "TRAILING_STOP_MARKET"
        }
        step = (
            self.rules.market_step
            if market_quantity
            else self.rules.lot_step
        )
        quantity = (
            validate_step(
                self.quantity_edit.text().replace(",", "").strip(),
                step,
                "Quantity",
            )
            if mode == "CONTRACTS"
            else quantize_step(str(quantity_value), step)
        )
        value = safe_float(quantity)
        minimum_qty = self.rules.min_market_qty if market_quantity else self.rules.min_qty
        maximum_qty = self.rules.max_market_qty if market_quantity else self.rules.max_qty
        if not (minimum_qty <= value <= maximum_qty):
            raise ValueError(
                f"Resulting quantity must be between {minimum_qty:g} and {maximum_qty:g}."
            )
        if self.rules.min_notional and reference > 0 and value * reference < self.rules.min_notional:
            raise ValueError(
                f"Order value must be at least {self.rules.min_notional:g} {self.rules.quote_asset}."
            )
        return quantity

    def _validated_price(self, value: str, label: str) -> str:
        price = validate_step(value, self.rules.tick_size, label)
        number = safe_float(price)
        if not (self.rules.min_price <= number <= self.rules.max_price):
            raise ValueError(
                f"{label} must be between {format_price(self.rules.min_price)} and "
                f"{format_price(self.rules.max_price)}."
            )
        if self._mark_is_fresh():
            if self.rules.price_multiplier_up and number > self.mark_price * self.rules.price_multiplier_up:
                raise ValueError(f"{label} is above Binance's current mark-price band.")
            if self.rules.price_multiplier_down and number < self.mark_price * self.rules.price_multiplier_down:
                raise ValueError(f"{label} is below Binance's current mark-price band.")
        return price

    def prepare_order(self) -> None:
        if not self.gateway.has_credentials():
            self.edit_credentials()
            if not self.gateway.has_credentials():
                return
        try:
            confirmed_leverage = self.gateway.current_leverage(self.symbol)
            self._sync_auto_position_side()
            order_type = self.current_order_type()
            reducing = self.reduce_only.isChecked()
            block_reason = self._manual_market_block_reason(order_type, reducing)
            if block_reason:
                raise ValueError(block_reason)
            if not reducing and (
                self._leverage_apply_timer.isActive()
                or self.symbol in self.gateway.cross_pending
                or confirmed_leverage <= 0
                or self.leverage.value() != confirmed_leverage
            ):
                self._apply_leverage_now()
                raise ValueError(
                    "Opening order blocked until CROSS leverage is confirmed by Binance."
                )
            active_protections = (
                {"tp": [], "sl": []}
                if reducing
                else json.loads(json.dumps(self.protection_plans))
            )
            side = "BUY" if self.buy_button.isChecked() else "SELL"
            order: dict[str, Any] = {
                "symbol": self.symbol,
                "side": side,
                "type": order_type,
                "quantity": self._quantity(order_type),
                "positionSide": self.position_side.currentText(),
            }
            if order_type in {"LIMIT", "STOP"}:
                order["price"] = self._validated_price(
                    self.price_edit.text().replace(",", "").strip(), "Order price"
                )
                order["timeInForce"] = self.current_time_in_force()
            if order_type in CONDITIONAL_ORDER_TYPES:
                order["workingType"] = str(self.working_type.currentData())
                order["priceProtect"] = True
                if order_type == "TRAILING_STOP_MARKET":
                    order["callbackRate"] = f"{self.callback_rate.value():.1f}"
                    activation = self.activation_edit.text().replace(",", "").strip()
                    if activation:
                        order["activatePrice"] = self._validated_price(
                            activation, "Activation price"
                        )
                else:
                    order["triggerPrice"] = self._validated_price(
                        self.trigger_edit.text().replace(",", "").strip(), "Trigger price"
                    )
                    trigger = safe_float(order["triggerPrice"])
                    if self._mark_is_fresh() and order_type in {"STOP", "STOP_MARKET"}:
                        expected_above = side == "BUY"
                        if (expected_above and trigger <= self.mark_price) or (
                            not expected_above and trigger >= self.mark_price
                        ):
                            relation = "above" if expected_above else "below"
                            raise ValueError(
                                f"A {side} Stop order must trigger {relation} "
                                f"the current mark price ({format_price(self.mark_price)})."
                            )
                if order_type == "TRAILING_STOP_MARKET":
                    activation = safe_float(order.get("activatePrice"))
                    if activation > 0 and self._mark_is_fresh():
                        invalid = (
                            side == "BUY" and activation >= self.mark_price
                        ) or (
                            side == "SELL" and activation <= self.mark_price
                        )
                        if invalid:
                            relation = "below" if side == "BUY" else "above"
                            raise ValueError(
                                f"A {side} Trailing Stop activation price must be {relation} "
                                f"the current mark price ({format_price(self.mark_price)})."
                            )
            if reducing and order["positionSide"] == "BOTH":
                order["reduceOnly"] = True
            if active_protections.get("tp") or active_protections.get("sl"):
                reference = self._entry_reference(order_type)
                if reference <= 0:
                    raise ValueError("A valid entry or trigger price is required before attaching TP/SL targets.")
                for target in active_protections.get("tp", []):
                    trigger = safe_float(target.get("price"))
                    self._validated_price(str(target.get("price", "")), "Take-profit trigger")
                    if (side == "BUY" and trigger <= reference) or (
                        side == "SELL" and trigger >= reference
                    ):
                        direction = "above" if side == "BUY" else "below"
                        raise ValueError(f"Take-profit prices for this {side} entry must be {direction} {format_price(reference)}.")
                for target in active_protections.get("sl", []):
                    trigger = safe_float(target.get("price"))
                    self._validated_price(str(target.get("price", "")), "Stop-loss trigger")
                    if (side == "BUY" and trigger >= reference) or (
                        side == "SELL" and trigger <= reference
                    ):
                        direction = "below" if side == "BUY" else "above"
                        raise ValueError(f"Stop-loss prices for this {side} entry must be {direction} {format_price(reference)}.")
        except ValueError as exc:
            self._set_validation_message(str(exc))
            self._update_execution_state()
            return
        self._set_validation_message("")
        self.order_requested.emit(
            {
                "order": order,
                "protections": active_protections,
                "rules": self.rules,
                "position_intent": "REDUCE" if reducing else "OPEN",
            }
        )


class BatchOrderDialog(QtWidgets.QDialog):
    def __init__(
        self,
        symbol: str,
        rules: SymbolRules,
        order_side: str,
        position_side: str,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.symbol = symbol
        self.rules = rules
        self.order_side = order_side
        self.position_side = position_side
        self.setWindowTitle(f"Batch limit orders · {symbol}")
        self.setMinimumWidth(650)
        self.rows: list[tuple[QtWidgets.QComboBox, QtWidgets.QLineEdit, QtWidgets.QLineEdit]] = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        direction = (
            position_side
            if position_side in {"LONG", "SHORT"}
            else ("LONG" if order_side == "BUY" else "SHORT")
        )
        intent = QtWidgets.QLabel(f"OPEN {direction} BATCH · {symbol} · LIMIT / GTC")
        intent.setObjectName("controlSectionTitle")
        layout.addWidget(intent)
        note = QtWidgets.QLabel(
            "Submit up to five standard limit entry orders in one native Binance batch request. "
            "Blank rows are ignored; side and hedge-position intent are locked to the current ticket."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        rule_note = QtWidgets.QLabel(
            f"PRICE TICK {_exchange_step_text(rules.tick_size)} · SIZE STEP {_exchange_step_text(rules.lot_step)}"
        )
        rule_note.setObjectName("subtleLabel")
        layout.addWidget(rule_note)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        for column, title in enumerate(("SIDE", "PRICE", "SIZE")):
            grid.addWidget(QtWidgets.QLabel(title), 0, column)
        for index in range(5):
            side = QtWidgets.QComboBox()
            side.addItem(self.order_side)
            side.setToolTip(
                f"Locked entry side · position intent {direction}"
            )
            price = QtWidgets.QLineEdit()
            price.setPlaceholderText(f"Limit {index + 1}")
            price.setToolTip(f"Binance price tick: {_exchange_step_text(rules.tick_size)}")
            set_text_role(price, TextRole.MARKET_VALUE)
            quantity = QtWidgets.QLineEdit()
            quantity.setPlaceholderText("Quantity")
            quantity.setToolTip(f"Binance quantity step: {_exchange_step_text(rules.lot_step)}")
            set_text_role(quantity, TextRole.MARKET_VALUE)
            price.textChanged.connect(self._validate_live)
            quantity.textChanged.connect(self._validate_live)
            grid.addWidget(side, index + 1, 0)
            grid.addWidget(price, index + 1, 1)
            grid.addWidget(quantity, index + 1, 2)
            self.rows.append((side, price, quantity))
        layout.addLayout(grid)
        self.validation = QtWidgets.QLabel("")
        self.validation.setObjectName("subtleLabel")
        self.validation.setWordWrap(True)
        self.validation.hide()
        layout.addWidget(self.validation)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("SUBMIT BATCH")
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_validation(self, message: str) -> None:
        message = str(message or "")
        self.validation.setText(message)
        self.validation.setVisible(bool(message))

    def _validate_live(self, _text: str = "") -> None:
        for index, (_side, price, quantity) in enumerate(self.rows, start=1):
            price_text = price.text().replace(",", "").strip()
            quantity_text = quantity.text().replace(",", "").strip()
            if not price_text and not quantity_text:
                continue
            if not price_text or not quantity_text:
                self._set_validation(f"ROW {index} · ENTER BOTH PRICE AND SIZE")
                return
            try:
                validate_step(price_text, self.rules.tick_size, f"Row {index} limit price")
                validate_step(quantity_text, self.rules.lot_step, f"Row {index} quantity")
            except ValueError as exc:
                self._set_validation(str(exc))
                return
        self._set_validation("")

    def _accept_validated(self) -> None:
        try:
            self.orders()
        except ValueError as exc:
            self._set_validation(str(exc))
            return
        self._set_validation("")
        self.accept()

    def orders(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for index, (side, price, quantity) in enumerate(self.rows, start=1):
            price_text = price.text().replace(",", "").strip()
            quantity_text = quantity.text().replace(",", "").strip()
            if not price_text and not quantity_text:
                continue
            if not price_text or not quantity_text:
                raise ValueError(f"Row {index} needs both price and quantity.")
            item = {
                "symbol": self.symbol,
                "side": side.currentText(),
                "type": "LIMIT",
                "price": validate_step(price_text, self.rules.tick_size, "Limit price"),
                "quantity": validate_step(quantity_text, self.rules.lot_step, "Quantity"),
                "timeInForce": "GTC",
                "positionSide": self.position_side,
            }
            output.append(item)
        if not output:
            raise ValueError("Enter at least one complete limit order.")
        return output


class ModifyOrderDialog(QtWidgets.QDialog):
    def __init__(
        self,
        order: dict[str, Any],
        rules: SymbolRules,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.order = dict(order)
        self.rules = rules
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "").upper()
        order_type = str(order.get("type") or order.get("orderType") or "LIMIT").upper()
        original = safe_float(order.get("origQty") or order.get("quantity"))
        executed = safe_float(order.get("executedQty") or order.get("cumQty"))
        remaining = max(0.0, original - executed)
        self.setWindowTitle(f"Modify working order · {symbol}")
        self.setMinimumWidth(440)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)
        context = QtWidgets.QLabel(
            f"{symbol} · {side} · {order_type} · REMAINING {human_number(remaining)}"
        )
        context.setObjectName("controlSectionTitle")
        root.addWidget(context)
        rules_label = QtWidgets.QLabel(
            f"PRICE TICK {_exchange_step_text(rules.tick_size)} · SIZE STEP {_exchange_step_text(rules.lot_step)}"
        )
        rules_label.setObjectName("subtleLabel")
        root.addWidget(rules_label)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.quantity = QtWidgets.QLineEdit(str(order.get("origQty") or order.get("quantity") or ""))
        self.price = QtWidgets.QLineEdit(str(order.get("price") or ""))
        self.price.setToolTip(f"Binance price tick: {_exchange_step_text(rules.tick_size)}")
        set_text_role(self.quantity, TextRole.MARKET_VALUE)
        set_text_role(self.price, TextRole.MARKET_VALUE)
        self.quantity.setToolTip(
            f"New TOTAL order quantity, including fills. Already filled: {human_number(executed)}. "
            f"Step: {_exchange_step_text(rules.lot_step)}. Remaining = total minus filled."
        )
        form.addRow("New total quantity", self.quantity)
        form.addRow("Price", self.price)
        root.addLayout(form)
        self.validation = QtWidgets.QLabel("")
        self.validation.setObjectName("subtleLabel")
        self.validation.setWordWrap(True)
        self.validation.hide()
        root.addWidget(self.validation)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.quantity.textChanged.connect(self._validate_live)
        self.price.textChanged.connect(self._validate_live)

    def _validated_changes(self) -> dict[str, Any]:
        quantity = validate_step(self.quantity.text(), self.rules.lot_step, "New total quantity")
        try:
            executed = Decimal(str(self.order.get("executedQty") or self.order.get("cumQty") or "0"))
        except InvalidOperation as exc:
            raise ValueError("Filled quantity is unavailable; refresh the order before editing.") from exc
        if not executed.is_finite() or executed < 0:
            raise ValueError("Filled quantity is unavailable; refresh the order before editing.")
        if Decimal(quantity) <= executed:
            raise ValueError("New total quantity must exceed the already filled amount. Use Cancel to remove the remainder.")
        return {
            "symbol": str(self.order.get("symbol") or ""),
            "orderId": self.order.get("orderId"),
            "side": self.order.get("side"),
            "quantity": quantity,
            "_minimumExecutedQty": str(executed),
            "price": validate_step(self.price.text(), self.rules.tick_size, "Price"),
        }

    def _validate_live(self, _text: str = "") -> None:
        try:
            self._validated_changes()
        except ValueError as exc:
            self.validation.setText(str(exc))
            self.validation.show()
            return
        self.validation.clear()
        self.validation.hide()

    def _accept_validated(self) -> None:
        try:
            self._validated_changes()
        except ValueError as exc:
            self.validation.setText(str(exc))
            self.validation.show()
            return
        self.accept()

    def changes(self) -> dict[str, Any]:
        return self._validated_changes()


def _signed_money(value: float) -> str:
    value = safe_float(value)
    if abs(value) < 1e-12:
        return human_number(0.0, money=True)
    sign = "+" if value > 0 else "-"
    return f"{sign}{human_number(abs(value), money=True)}"


def _pnl_state(value: float) -> str:
    value = safe_float(value)
    return "positive" if value > 1e-12 else "negative" if value < -1e-12 else "flat"


def _set_text_if_changed(widget: QtWidgets.QLabel | QtWidgets.QAbstractButton, text: str) -> bool:
    text = str(text)
    if widget.text() == text:
        return False
    widget.setText(text)
    return True


def _set_repolished_property(widget: QtWidgets.QWidget, name: str, value: Any) -> bool:
    if widget.property(name) == value:
        return False
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    return True


def _position_direction(payload: dict[str, Any]) -> str:
    amount = safe_float(payload.get("positionAmt"))
    side = str(payload.get("positionSide") or "BOTH").upper()
    if side == "LONG" or (side == "BOTH" and amount > 0):
        return "LONG"
    if side == "SHORT" or (side == "BOTH" and amount < 0):
        return "SHORT"
    return side or "—"


def _position_close_side(payload: dict[str, Any]) -> str:
    return "BUY" if _position_direction(payload) == "SHORT" else "SELL"


def _position_notional(payload: dict[str, Any]) -> float:
    explicit = abs(safe_float(payload.get("notional")))
    if explicit > 0:
        return explicit
    amount = abs(safe_float(payload.get("positionAmt")))
    mark = safe_float(payload.get("markPrice"))
    return amount * mark if amount > 0 and mark > 0 else 0.0


def _update_position_mark(payload: dict[str, Any], mark: float) -> float:
    """Update USD-M linear position values from a live mark-price tick."""
    mark = safe_float(mark)
    amount = abs(safe_float(payload.get("positionAmt")))
    entry = safe_float(payload.get("entryPrice"))
    direction = _position_direction(payload)
    if mark <= 0 or amount <= 0:
        return safe_float(payload.get("unrealizedProfit"))
    signed_amount = -amount if direction == "SHORT" else amount
    pnl = (mark - entry) * signed_amount if entry > 0 else 0.0
    payload["markPrice"] = mark
    payload["unrealizedProfit"] = pnl
    payload["notional"] = signed_amount * mark
    return pnl


class CloseLimitDialog(QtWidgets.QDialog):
    """Collect the price for a percentage-based reduce-only limit close."""

    def __init__(
        self,
        position: dict[str, Any],
        percent: int,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        symbol = str(position.get("symbol") or "POSITION")
        direction = _position_direction(position)
        self.setWindowTitle(f"Close {symbol} with limit order")
        self.setMinimumWidth(360)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        context = QtWidgets.QLabel(
            f"{symbol} · {direction} · CLOSE {max(1, min(100, int(percent)))}%"
        )
        context.setObjectName("tradingDeskStatus")
        layout.addWidget(context)
        self.price_edit = QtWidgets.QLineEdit()
        set_text_role(self.price_edit, TextRole.MARKET_VALUE)
        mark = safe_float(position.get("markPrice"))
        self.price_edit.setPlaceholderText("Limit price")
        if mark > 0:
            self.price_edit.setText(format_price(mark).replace(",", ""))
        self.price_edit.selectAll()
        layout.addWidget(self.price_edit)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        confirm = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        confirm.setText("PLACE CLOSE LIMIT")
        confirm.setObjectName("dangerButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def price_text(self) -> str:
        return self.price_edit.text().replace(",", "").strip()


class PositionAccountCard(QtWidgets.QFrame):
    MINIMUM_HEIGHT = 138

    selected_requested = Signal()
    close_requested = Signal(object, int)
    close_limit_requested = Signal(object, int)

    def __init__(
        self,
        payload: dict[str, Any],
        theme: dict[str, str],
        close_percentages: list[int],
        current_symbol: str,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.payload = dict(payload)
        self.theme = theme
        self.close_percentages = list(close_percentages)
        direction = _position_direction(self.payload)
        self.setObjectName("positionAccountCard")
        self.setProperty("direction", direction.casefold())
        self.setProperty(
            "current",
            str(self.payload.get("symbol") or "") == str(current_symbol),
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(self.MINIMUM_HEIGHT)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.symbol_label = ElidedLabel(str(self.payload.get("symbol") or "—"))
        self.symbol_label.setObjectName("accountCardSymbol")
        set_text_role(self.symbol_label, TextRole.INSTRUMENT_SYMBOL)
        leverage = str(self.payload.get("leverage") or "—")
        self.side_label = QtWidgets.QLabel(f"{direction} · {leverage}×")
        self.side_label.setObjectName("accountCardSide")
        set_text_role(self.side_label, TextRole.UI_LABEL)
        self.side_label.setProperty("direction", direction.casefold())
        self.pnl_label = QtWidgets.QLabel()
        self.pnl_label.setObjectName("accountCardPnl")
        set_text_role(self.pnl_label, TextRole.MARKET_VALUE_EMPHASIZED)
        self.pnl_label.setProperty("pnl", "flat")
        self.pnl_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.symbol_label, 1)
        header.addWidget(self.side_label)
        header.addWidget(self.pnl_label)
        layout.addLayout(header)




        metrics = QtWidgets.QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(8)
        metrics.setVerticalSpacing(1)
        self.entry_value_label = QtWidgets.QLabel("—")
        self.mark_value_label = QtWidgets.QLabel("—")
        self.size_value_label = QtWidgets.QLabel("—")
        self.notional_value_label = QtWidgets.QLabel("—")
        for column, (caption, value_label) in enumerate((
            ("ENTRY", self.entry_value_label),
            ("MARK", self.mark_value_label),
            ("SIZE", self.size_value_label),
            ("VALUE", self.notional_value_label),
        )):
            label = QtWidgets.QLabel(caption)
            label.setObjectName("accountCardDetail")
            set_text_role(label, TextRole.UI_CAPTION)
            set_text_role(value_label, TextRole.MARKET_VALUE)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            metrics.addWidget(label, 0, column)
            metrics.addWidget(value_label, 1, column)
            metrics.setColumnStretch(column, 1)
        layout.addLayout(metrics)

        risk = QtWidgets.QGridLayout()
        risk.setContentsMargins(0, 0, 0, 0)
        risk.setHorizontalSpacing(8)
        risk.setVerticalSpacing(1)
        self.liquidation_value_label = QtWidgets.QLabel("—")
        self.distance_value_label = QtWidgets.QLabel("—")
        self.margin_value_label = QtWidgets.QLabel("—")
        for column, (caption, value_label, object_name) in enumerate((
            ("LIQ", self.liquidation_value_label, "accountCardRisk"),
            ("DIST", self.distance_value_label, "accountCardRisk"),
            ("MARGIN", self.margin_value_label, "accountCardDetail"),
        )):
            label = QtWidgets.QLabel(caption)
            label.setObjectName("accountCardDetail")
            set_text_role(label, TextRole.UI_CAPTION)
            value_label.setObjectName(object_name)
            set_text_role(value_label, TextRole.MARKET_VALUE)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            risk.addWidget(label, 0, column)
            risk.addWidget(value_label, 1, column)
            risk.setColumnStretch(column, 1)
        layout.addLayout(risk)

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 1, 0, 0)
        actions.setSpacing(4)
        self.close_size = QtWidgets.QComboBox()
        self.close_size.setObjectName("positionCloseSize")
        self.close_size.setToolTip("Percentage of this position to close")
        self.close_size.setMinimumContentsLength(4)
        self.close_size.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.close_size.setMinimumWidth(92)
        self.close_size.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.close_market = QtWidgets.QPushButton("CLOSE MARKET")
        self.close_market.setObjectName("accountCardCloseMarket")
        self.close_limit = QtWidgets.QPushButton("CLOSE LIMIT")
        self.close_limit.setObjectName("accountCardCloseLimit")
        set_text_role(self.close_size, TextRole.UI_CONTROL_COMPACT)
        for button in (self.close_market, self.close_limit):
            set_text_role(button, TextRole.UI_CONTROL_COMPACT)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoDefault(False)
        self.close_market.setToolTip(
            "Immediate reduce-only market close for the selected percentage. No confirmation dialog is inserted so risk reduction remains fast."
        )
        self.close_limit.setToolTip(
            "Open a price dialog for a reduce-only limit close of the selected percentage."
        )
        self.close_market.clicked.connect(
            lambda: self.close_requested.emit(dict(self.payload), self.close_percent())
        )
        self.close_limit.clicked.connect(
            lambda: self.close_limit_requested.emit(
                dict(self.payload), self.close_percent()
            )
        )
        actions.addWidget(self.close_size)
        actions.addWidget(self.close_market, 1)
        actions.addWidget(self.close_limit, 1)
        layout.addLayout(actions)

        self.set_close_percentages(self.close_percentages)
        self._refresh_values()

        for label in self.findChildren(QtWidgets.QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def update_payload(self, payload: dict[str, Any], current_symbol: str) -> None:
        self.payload = dict(payload)
        direction = _position_direction(self.payload)
        _set_repolished_property(self, "direction", direction.casefold())
        _set_repolished_property(
            self,
            "current",
            str(self.payload.get("symbol") or "") == str(current_symbol),
        )
        _set_text_if_changed(self.symbol_label, str(self.payload.get("symbol") or "—"))
        leverage = str(self.payload.get("leverage") or "—")
        _set_text_if_changed(self.side_label, f"{direction} · {leverage}×")
        _set_repolished_property(self.side_label, "direction", direction.casefold())
        self._refresh_values()

    def set_close_percentages(self, values: list[int]) -> None:
        self.close_percentages = list(values)
        current = self.close_size.currentData()
        blocker = QtCore.QSignalBlocker(self.close_size)
        self.close_size.clear()
        for percent in self.close_percentages:
            self.close_size.addItem(f"{percent}%", int(percent))
        index = self.close_size.findData(current)
        self.close_size.setCurrentIndex(index if index >= 0 else self.close_size.count() - 1)
        del blocker

    def close_percent(self) -> int:
        return max(1, min(100, int(safe_float(self.close_size.currentData(), 100))))

    def update_mark_price(self, mark: float) -> float:
        pnl = _update_position_mark(self.payload, mark)
        self._refresh_values()
        return pnl

    def _refresh_values(self) -> None:
        amount = abs(safe_float(self.payload.get("positionAmt")))
        entry = safe_float(self.payload.get("entryPrice"))
        mark = safe_float(self.payload.get("markPrice"))
        pnl = safe_float(self.payload.get("unrealizedProfit"))
        margin = safe_float(self.payload.get("positionInitialMargin"))
        if margin <= 0:
            margin = safe_float(self.payload.get("initialMargin"))
        roe_text = f" · {pnl / margin * 100:+.1f}% ROE" if margin > 0 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.entry_value_label, format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.mark_value_label, format_price(mark) if mark > 0 else "—"
        )
        _set_text_if_changed(self.size_value_label, human_number(amount))
        _set_text_if_changed(
            self.notional_value_label,
            human_number(_position_notional(self.payload), money=True),
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 0 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)
            liquidation_text = format_price(liquidation)
            distance_text = f"{distance:.1f}%"
            risk_state = "critical" if distance <= 5.0 else "warning" if distance <= 12.0 else "normal"
        _set_text_if_changed(self.liquidation_value_label, liquidation_text)
        _set_text_if_changed(self.distance_value_label, distance_text)
        for label in (self.liquidation_value_label, self.distance_value_label):
            _set_repolished_property(label, "risk", risk_state)
        _set_text_if_changed(
            self.margin_value_label,
            human_number(margin, money=True) if margin > 0 else "—",
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


class OrderAccountCard(QtWidgets.QFrame):
    MINIMUM_HEIGHT = 102

    selected_requested = Signal()
    cancel_requested = Signal(object)

    def __init__(
        self,
        payload: dict[str, Any],
        theme: dict[str, str],
        current_symbol: str,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.payload = dict(payload)
        self.theme = theme
        self.current_symbol = str(current_symbol or "")
        self.setObjectName("orderAccountCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(self.MINIMUM_HEIGHT)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.symbol_label = ElidedLabel("—")
        self.symbol_label.setObjectName("accountCardSymbol")
        set_text_role(self.symbol_label, TextRole.INSTRUMENT_SYMBOL)
        self.side_label = QtWidgets.QLabel("—")
        self.side_label.setObjectName("accountCardSide")
        set_text_role(self.side_label, TextRole.UI_LABEL)
        self.type_label = ElidedLabel("ORDER")
        self.type_label.setObjectName("accountCardDetail")
        set_text_role(self.type_label, TextRole.UI_CAPTION)
        self.status_label = QtWidgets.QLabel("NEW")
        self.status_label.setObjectName("accountOrderStatus")
        set_text_role(self.status_label, TextRole.UI_CAPTION)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.symbol_label, 1)
        header.addWidget(self.side_label)
        header.addWidget(self.type_label)
        header.addWidget(self.status_label)
        layout.addLayout(header)

        metrics = QtWidgets.QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(8)
        metrics.setVerticalSpacing(1)
        self.metric_value_labels: dict[str, QtWidgets.QLabel] = {}
        metric_roles = (
            ("SIZE", TextRole.MARKET_VALUE),
            ("PRICE", TextRole.MARKET_VALUE),
            ("TRIGGER", TextRole.MARKET_VALUE),
            ("TIF", TextRole.UI_LABEL),
        )
        for column, (caption, role) in enumerate(metric_roles):
            caption_label = QtWidgets.QLabel(caption)
            caption_label.setObjectName("accountCardDetail")
            set_text_role(caption_label, TextRole.UI_CAPTION)
            value_label = QtWidgets.QLabel("—")
            value_label.setObjectName("accountCardDetail")
            set_text_role(value_label, role)
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            metrics.addWidget(caption_label, 0, column)
            metrics.addWidget(value_label, 1, column)
            metrics.setColumnStretch(column, 1)
            self.metric_value_labels[caption] = value_label
        layout.addLayout(metrics)

        bottom = QtWidgets.QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(6)
        self.context_label = QtWidgets.QLabel("WORKING ORDER")
        self.context_label.setObjectName("accountCardDetail")
        set_text_role(self.context_label, TextRole.UI_CAPTION)
        self.cancel_button = QtWidgets.QPushButton("CANCEL")
        self.cancel_button.setObjectName("accountCardCancel")
        set_text_role(self.cancel_button, TextRole.UI_CONTROL_COMPACT)
        self.cancel_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cancel_button.clicked.connect(
            lambda: self.cancel_requested.emit(dict(self.payload))
        )
        bottom.addWidget(self.context_label, 1)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)

        for label in self.findChildren(QtWidgets.QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.update_payload(self.payload)

    def update_payload(self, payload: dict[str, Any]) -> None:
        """Refresh an existing card in place from the latest order payload."""
        self.payload = dict(payload)
        side = str(self.payload.get("side") or "—").upper()
        status = str(
            self.payload.get("status")
            or self.payload.get("algoStatus")
            or "NEW"
        ).upper()
        order_type = str(
            self.payload.get("type")
            or self.payload.get("orderType")
            or "ORDER"
        ).replace("_", " ")
        quantity = str(
            self.payload.get("origQty")
            or self.payload.get("quantity")
            or self.payload.get("totalQty")
            or "—"
        )
        price = safe_float(
            self.payload.get("price") or self.payload.get("actualPrice")
        )
        trigger = safe_float(
            self.payload.get("triggerPrice") or self.payload.get("stopPrice")
        )
        time_in_force = str(self.payload.get("timeInForce") or "").upper()

        _set_repolished_property(self, "side", side.casefold())
        _set_repolished_property(
            self,
            "current",
            str(self.payload.get("symbol") or "") == self.current_symbol,
        )
        _set_text_if_changed(self.symbol_label, str(self.payload.get("symbol") or "—"))
        _set_text_if_changed(self.side_label, side)
        _set_repolished_property(
            self.side_label,
            "direction",
            "long" if side == "BUY" else "short",
        )
        _set_text_if_changed(self.type_label, order_type)
        _set_text_if_changed(self.status_label, status)
        _set_repolished_property(
            self.status_label,
            "state",
            "partial" if status == "PARTIALLY_FILLED" else "open",
        )
        _set_text_if_changed(self.metric_value_labels["SIZE"], quantity)
        _set_text_if_changed(
            self.metric_value_labels["PRICE"],
            format_price(price) if price > 0 else "MARKET",
        )
        _set_text_if_changed(
            self.metric_value_labels["TRIGGER"],
            format_price(trigger) if trigger > 0 else "—",
        )
        _set_text_if_changed(
            self.metric_value_labels["TIF"], time_in_force or "—"
        )
        context = (
            "REDUCE ONLY"
            if str(self.payload.get("reduceOnly", False)).lower() == "true"
            else "WORKING ORDER"
        )
        _set_text_if_changed(self.context_label, context)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


class CompactPositionActivityCard(QtWidgets.QFrame):
    """Low-height current-position summary for spare right-rail space."""

    MINIMUM_HEIGHT = 78
    selected_requested = Signal()

    def __init__(
        self,
        payload: dict[str, Any],
        current_symbol: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.payload = dict(payload)
        self.current_symbol = str(current_symbol or "")
        self.setObjectName("compactPositionActivityCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(self.MINIMUM_HEIGHT)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(3)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.symbol_label = ElidedLabel("—")
        self.symbol_label.setObjectName("accountCardSymbol")
        set_text_role(self.symbol_label, TextRole.INSTRUMENT_SYMBOL)
        self.side_label = QtWidgets.QLabel("—")
        self.side_label.setObjectName("accountCardSide")
        set_text_role(self.side_label, TextRole.UI_CAPTION)
        self.pnl_label = QtWidgets.QLabel("—")
        self.pnl_label.setObjectName("accountCardPnl")
        set_text_role(self.pnl_label, TextRole.MARKET_VALUE)
        self.pnl_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.symbol_label, 1)
        header.addWidget(self.side_label)
        header.addWidget(self.pnl_label)
        layout.addLayout(header)

        metrics = QtWidgets.QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(7)
        metrics.setVerticalSpacing(0)
        self.metric_labels: dict[str, QtWidgets.QLabel] = {}
        for column, caption in enumerate(("ENTRY", "MARK", "LIQ", "DIST")):
            caption_label = QtWidgets.QLabel(caption)
            caption_label.setObjectName("accountCardDetail")
            set_text_role(caption_label, TextRole.UI_CAPTION)
            value_label = QtWidgets.QLabel("—")
            value_label.setObjectName(
                "accountCardRisk" if caption in {"LIQ", "DIST"} else "accountCardDetail"
            )
            set_text_role(value_label, TextRole.MARKET_VALUE)
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            metrics.addWidget(caption_label, 0, column)
            metrics.addWidget(value_label, 1, column)
            metrics.setColumnStretch(column, 1)
            self.metric_labels[caption] = value_label
        layout.addLayout(metrics)

        note = QtWidgets.QLabel("CLICK FOR POSITION CONTROLS")
        note.setObjectName("accountCardDetail")
        set_text_role(note, TextRole.UI_CAPTION)
        note.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(note)
        for label in self.findChildren(QtWidgets.QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.update_payload(self.payload, self.current_symbol)

    def update_payload(self, payload: dict[str, Any], current_symbol: str) -> None:
        self.payload = dict(payload)
        self.current_symbol = str(current_symbol or "")
        direction = _position_direction(self.payload)
        _set_repolished_property(self, "direction", direction.casefold())
        _set_repolished_property(
            self,
            "current",
            str(self.payload.get("symbol") or "") == self.current_symbol,
        )
        _set_text_if_changed(self.symbol_label, str(self.payload.get("symbol") or "—"))
        leverage = str(self.payload.get("leverage") or "—")
        _set_text_if_changed(self.side_label, f"{direction} · {leverage}×")
        _set_repolished_property(self.side_label, "direction", direction.casefold())
        self._refresh_values()

    def update_mark_price(self, mark: float) -> float:
        pnl = _update_position_mark(self.payload, mark)
        self._refresh_values()
        return pnl

    def _refresh_values(self) -> None:
        entry = safe_float(self.payload.get("entryPrice"))
        mark = safe_float(self.payload.get("markPrice"))
        pnl = safe_float(self.payload.get("unrealizedProfit"))
        margin = safe_float(self.payload.get("positionInitialMargin"))
        if margin <= 0:
            margin = safe_float(self.payload.get("initialMargin"))
        roe_text = f" · {pnl / margin * 100:+.1f}%" if margin > 0 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.metric_labels["ENTRY"], format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.metric_labels["MARK"], format_price(mark) if mark > 0 else "—"
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 0 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)
            liquidation_text = format_price(liquidation)
            distance_text = f"{distance:.1f}%"
            risk_state = (
                "critical" if distance <= 5.0 else "warning" if distance <= 12.0 else "normal"
            )
        _set_text_if_changed(self.metric_labels["LIQ"], liquidation_text)
        _set_text_if_changed(self.metric_labels["DIST"], distance_text)
        for key in ("LIQ", "DIST"):
            _set_repolished_property(self.metric_labels[key], "risk", risk_state)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


class CompactOrderActivityCard(QtWidgets.QFrame):
    """Low-height current-symbol working-order summary for spare rail space."""

    MINIMUM_HEIGHT = 68
    selected_requested = Signal()

    def __init__(
        self,
        payload: dict[str, Any],
        current_symbol: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.payload = dict(payload)
        self.current_symbol = str(current_symbol or "")
        self.setObjectName("compactOrderActivityCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(self.MINIMUM_HEIGHT)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(3)
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.symbol_label = ElidedLabel("—")
        self.symbol_label.setObjectName("accountCardSymbol")
        set_text_role(self.symbol_label, TextRole.INSTRUMENT_SYMBOL)
        self.side_label = QtWidgets.QLabel("—")
        self.side_label.setObjectName("accountCardSide")
        set_text_role(self.side_label, TextRole.UI_CAPTION)
        self.type_label = ElidedLabel("ORDER")
        self.type_label.setObjectName("accountCardDetail")
        set_text_role(self.type_label, TextRole.UI_CAPTION)
        self.status_label = QtWidgets.QLabel("NEW")
        self.status_label.setObjectName("accountOrderStatus")
        set_text_role(self.status_label, TextRole.UI_CAPTION)
        header.addWidget(self.symbol_label, 1)
        header.addWidget(self.side_label)
        header.addWidget(self.type_label)
        header.addWidget(self.status_label)
        layout.addLayout(header)

        metrics = QtWidgets.QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(7)
        metrics.setVerticalSpacing(0)
        self.metric_labels: dict[str, QtWidgets.QLabel] = {}
        for column, caption in enumerate(("SIZE", "PRICE", "TRIGGER", "TIF")):
            caption_label = QtWidgets.QLabel(caption)
            caption_label.setObjectName("accountCardDetail")
            set_text_role(caption_label, TextRole.UI_CAPTION)
            value_label = QtWidgets.QLabel("—")
            value_label.setObjectName("accountCardDetail")
            set_text_role(
                value_label,
                TextRole.UI_CAPTION if caption == "TIF" else TextRole.MARKET_VALUE,
            )
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            metrics.addWidget(caption_label, 0, column)
            metrics.addWidget(value_label, 1, column)
            metrics.setColumnStretch(column, 1)
            self.metric_labels[caption] = value_label
        layout.addLayout(metrics)
        for label in self.findChildren(QtWidgets.QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.update_payload(self.payload)

    def update_payload(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        side = str(self.payload.get("side") or "—").upper()
        status = str(
            self.payload.get("status") or self.payload.get("algoStatus") or "NEW"
        ).upper()
        order_type = str(
            self.payload.get("type") or self.payload.get("orderType") or "ORDER"
        ).replace("_", " ")
        quantity = str(
            self.payload.get("origQty")
            or self.payload.get("quantity")
            or self.payload.get("totalQty")
            or "—"
        )
        price = safe_float(self.payload.get("price") or self.payload.get("actualPrice"))
        trigger = safe_float(
            self.payload.get("triggerPrice") or self.payload.get("stopPrice")
        )
        tif = str(self.payload.get("timeInForce") or "").upper()
        _set_repolished_property(self, "side", side.casefold())
        _set_repolished_property(
            self,
            "current",
            str(self.payload.get("symbol") or "") == self.current_symbol,
        )
        _set_text_if_changed(self.symbol_label, str(self.payload.get("symbol") or "—"))
        _set_text_if_changed(self.side_label, side)
        _set_repolished_property(
            self.side_label,
            "direction",
            "long" if side == "BUY" else "short",
        )
        _set_text_if_changed(self.type_label, order_type)
        _set_text_if_changed(self.status_label, status)
        _set_repolished_property(
            self.status_label,
            "state",
            "partial" if status == "PARTIALLY_FILLED" else "open",
        )
        _set_text_if_changed(self.metric_labels["SIZE"], quantity)
        _set_text_if_changed(
            self.metric_labels["PRICE"], format_price(price) if price > 0 else "MARKET"
        )
        _set_text_if_changed(
            self.metric_labels["TRIGGER"], format_price(trigger) if trigger > 0 else "—"
        )
        _set_text_if_changed(self.metric_labels["TIF"], tif or "—")

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


def _new_account_card_list() -> QtWidgets.QListWidget:
    view = QtWidgets.QListWidget()
    view.setObjectName("tradingCardList")
    view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setSpacing(4)
    view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    return view


def _account_key(payload):
    return (
        str(payload.get("symbol", "")),
        str(payload.get("positionSide", "")),
        str(payload.get("_source", "")),
        str(
            payload.get("algoId")
            or payload.get("orderId")
            or payload.get("clientAlgoId")
            or payload.get("clientOrderId")
            or ""
        ),
    )


def _order_card_render_fingerprint(payloads, current_symbol):
    return (
        str(current_symbol),
        tuple(
            (
                _account_key(payload),
                str(payload.get("side") or ""),
                str(payload.get("status") or payload.get("algoStatus") or ""),
                str(payload.get("type") or payload.get("orderType") or payload.get("algoType") or ""),
                str(payload.get("origQty") or payload.get("quantity") or payload.get("totalQty") or ""),
                str(payload.get("executedQty") or payload.get("cumQty") or ""),
                str(payload.get("price") or ""),
                str(payload.get("actualPrice") or ""),
                str(payload.get("triggerPrice") or payload.get("stopPrice") or ""),
                str(payload.get("activatePrice") or payload.get("activationPrice") or ""),
                str(payload.get("timeInForce") or ""),
                str(payload.get("reduceOnly") or ""),
                str(payload.get("closePosition") or ""),
            )
            for payload in payloads
        ),
    )


def _set_tab_text_if_changed(tabs, index, text):
    if tabs.tabText(index) != text:
        tabs.setTabText(index, text)


class AccountDataTable(QtWidgets.QTableWidget):
    """Visible empty state that never masquerades as an account data row."""

    def __init__(self, headers):
        super().__init__(0, len(headers))
        self.empty = QtWidgets.QLabel("No records", self.viewport())
        self.empty.setObjectName("accountEmptyState")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty.setGeometry(self.viewport().rect().adjusted(8, 8, -8, -8))

    def setRowCount(self, count):
        super().setRowCount(count)
        self.empty.setVisible(count == 0)
        self.empty.setGeometry(self.viewport().rect().adjusted(8, 8, -8, -8))


def _populate_account_cards(
    view, payloads, factory, empty_text, *, fingerprint=None, update_existing=None
):
    payloads = [dict(payload) for payload in payloads]
    structure = (
        str(empty_text),
        fingerprint if fingerprint is not None
        else tuple(_account_key(payload) for payload in payloads),
    )
    if getattr(view, "_cards_fingerprint", None) == structure:
        if payloads and update_existing is not None and view.count() == len(payloads):
            for index, payload in enumerate(payloads):
                item = view.item(index)
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                card = view.itemWidget(item)
                update_existing(card, dict(payload))
        return False

    previous = view.currentItem()
    previous_payload = previous.data(Qt.ItemDataRole.UserRole) if previous else None
    selected_key = _account_key(previous_payload) if isinstance(previous_payload, dict) else None
    scroll = view.verticalScrollBar().value()
    view.setUpdatesEnabled(False)
    try:
        view.clear()
        if not payloads:
            item = QtWidgets.QListWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            card = QtWidgets.QFrame()
            card.setObjectName("accountEmptyState")
            layout = QtWidgets.QVBoxLayout(card)
            layout.setContentsMargins(12, 16, 12, 16)
            label = QtWidgets.QLabel(empty_text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            layout.addWidget(label)
            item.setSizeHint(QtCore.QSize(0, max(80, card.sizeHint().height())))
            view.addItem(item)
            view.setItemWidget(item, card)
            view._cards_fingerprint = structure
            return True
        selection = None
        for payload in payloads:
            item = QtWidgets.QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dict(payload))
            card = factory(dict(payload))
            item.setSizeHint(QtCore.QSize(0, card.sizeHint().height() + 2))
            view.addItem(item)
            view.setItemWidget(item, card)
            if hasattr(card, "selected_requested"):
                card.selected_requested.connect(lambda item=item: view.setCurrentItem(item))
            if selected_key == _account_key(payload):
                selection = item
        view.setCurrentItem(selection or view.item(0))
        view.verticalScrollBar().setValue(scroll)
        view._cards_fingerprint = structure
        return True
    finally:
        view.setUpdatesEnabled(True)

def _account_fill_rows(snapshot, symbol):
    result = []
    for row in sorted(snapshot.get("fills", []), key=lambda r: safe_float(r.get("time")), reverse=True):
        if str(row.get("symbol", "")) != symbol:
            continue
        timestamp = safe_float(row.get("time")) / 1000.0
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %H:%M:%S") if timestamp > 0 else "—"
        result.append(((stamp, str(row.get("side", "")), str(row.get("price", "")),
                        str(row.get("qty", "")), _signed_money(safe_float(row.get("realizedPnl"))),
                        f"{safe_float(row.get('commission')):.7f} {row.get('commissionAsset', '')}"), dict(row)))
    return result


class TradingWorkspace(QtWidgets.QWidget):
    order_requested = Signal(object)
    batch_orders_requested = Signal(object)
    orders_drawer_toggled = Signal(bool)
    rail_minimum_height_changed = Signal()

    def __init__(
        self,
        theme: dict[str, str],
        gateway: TradingGatewayPort,
        parent: QtWidgets.QWidget | None = None,
        symbol_rules: dict[str, SymbolRules] | None = None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.gateway = gateway
        self.symbol_rules = symbol_rules if symbol_rules is not None else {}
        self.symbol = DEFAULT_SYMBOL
        self.rules = SymbolRules()
        self._orders_drawer_open = False
        self._open_orders: list[dict[str, Any]] = []
        self._position_payloads: list[dict[str, Any]] = []
        self._last_account_snapshot_mono = 0.0
        self._account_status_base = "ACCOUNT DATA · ADD API CREDENTIALS"
        self.account_refresh_timer = QTimer(self)
        self.account_refresh_timer.setSingleShot(True)
        self.account_refresh_timer.setInterval(8_000)
        self.account_refresh_timer.timeout.connect(self._poll_account_if_open)
        self.account_poll_timer = QTimer(self)
        self.account_poll_timer.setInterval(20_000)
        self.account_poll_timer.timeout.connect(self._poll_account_if_open)
        self.account_poll_timer.start()
        self.account_age_timer = QTimer(self)
        self.account_age_timer.setInterval(5_000)
        self.account_age_timer.timeout.connect(self._refresh_account_freshness)
        self.close_percentages = [25, 50, 100]
        self.setMinimumSize(0, 0)
        layout = QtWidgets.QVBoxLayout(self)



        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.ticket = OrderPanel(gateway, compact=True)
        self.ticket.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.ticket.order_requested.connect(self.order_requested)
        self.ticket.minimum_content_height_changed.connect(
            self._sync_rail_minimum_height
        )
        layout.addWidget(self.ticket, 0)






        self._bottom_panel = False
        self._adaptive_activity_threshold = 200
        self.adaptive_activity_frame = QtWidgets.QFrame(self)
        self.adaptive_activity_frame.setObjectName("tradingAdaptiveActivity")
        self.adaptive_activity_frame.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._adaptive_activity_layout = QtWidgets.QVBoxLayout(
            self.adaptive_activity_frame
        )
        self._adaptive_activity_layout.setContentsMargins(0, 8, 0, 0)
        self._adaptive_activity_layout.setSpacing(0)
        self._adaptive_activity_card: CompactPositionActivityCard | CompactOrderActivityCard | None = None
        self._adaptive_activity_key: tuple[Any, ...] | None = None
        self.adaptive_activity_frame.hide()
        layout.addWidget(self.adaptive_activity_frame, 0)
        layout.addStretch(1)
        self._sync_rail_minimum_height(self.ticket.compact_required_height())

        self.account_frame = QtWidgets.QFrame(self)
        self.account_frame.setObjectName("tradingAccountFrame")
        account_layout = QtWidgets.QVBoxLayout(self.account_frame)
        account_layout.setContentsMargins(8, 8, 8, 8)
        account_layout.setSpacing(6)
        account_header = QtWidgets.QHBoxLayout()
        account_header.setContentsMargins(2, 0, 2, 0)
        account_header.setSpacing(8)
        self.status = ElidedLabel("ACCOUNT DATA · ADD API CREDENTIALS")
        self.status.setObjectName("tradingDeskStatus")
        self.total_pnl = QtWidgets.QLabel("UPNL —")
        self.total_pnl.setObjectName("accountTotalPnl")


        set_text_role(self.total_pnl, TextRole.MARKET_VALUE_LARGE)
        self.total_pnl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        account_header.addWidget(self.status, 1)
        account_header.addWidget(self.total_pnl)
        account_layout.addLayout(account_header)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("tradingAccountTabs")
        self.tabs.setDocumentMode(True)
        set_text_role(self.tabs.tabBar(), TextRole.UI_CONTROL_COMPACT)
        self.positions = _new_account_card_list()
        self.orders = _new_account_card_list()
        self.fills = self._table(("TIME", "SIDE", "PRICE", "SIZE", "PNL", "FEE"))
        self.balances = self._table(("ASSET", "WALLET", "AVAILABLE", "UPNL"))
        self._position_count = 0
        self._order_count = 0

        def account_page(content: QtWidgets.QWidget) -> QtWidgets.QWidget:
            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(0, 4, 0, 4)
            page_layout.setSpacing(0)
            page_layout.addWidget(content)
            return page

        _populate_account_cards(self.positions, [], None, "Connect API credentials to view positions")
        _populate_account_cards(self.orders, [], None, "Connect API credentials to view orders")
        self.fills.empty.setText("Connect API credentials to view fills")
        self.balances.empty.setText("Connect API credentials to view balances")
        self.tabs.addTab(account_page(self.positions), "POSITIONS 0")
        self.tabs.addTab(account_page(self.fills), "FILLS")
        self.tabs.addTab(account_page(self.orders), "ORDERS 0")
        self.tabs.addTab(account_page(self.balances), "BALANCES")
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(True)
        account_layout.addWidget(self.tabs, 1)

        self.order_actions = QtWidgets.QWidget()
        order_row = QtWidgets.QHBoxLayout(self.order_actions)
        order_row.setContentsMargins(0, 0, 0, 0)
        order_row.setSpacing(4)
        self.batch_button = QtWidgets.QPushButton("NEW LIMIT BATCH")
        self.modify_button = QtWidgets.QPushButton("EDIT SELECTED")
        self.cancel_all_button = QtWidgets.QPushButton("CANCEL ALL SYMBOL ORDERS")
        self.cancel_all_button.setObjectName("dangerButton")
        order_row.addWidget(self.batch_button, 1)
        order_row.addWidget(self.modify_button, 1)
        order_row.addWidget(self.cancel_all_button, 1)
        account_layout.addWidget(self.order_actions)

        self.account_frame.hide()
        self.batch_button.clicked.connect(self.open_batch_orders)
        self.modify_button.clicked.connect(self.modify_selected)
        self.ticket.reduce_only.toggled.connect(lambda _checked: self._sync_batch_action())
        self.cancel_all_button.clicked.connect(self.cancel_selected_symbol_orders)
        self.tabs.currentChanged.connect(self._account_tab_changed)
        self.gateway.snapshot_ready.connect(self.apply_snapshot)
        self.gateway.credentials_changed.connect(self._clear_account_view)
        self.gateway.account_event.connect(self._account_event)
        self.gateway.state_changed.connect(self.status.setText)
        self.gateway.request_succeeded.connect(
            lambda _request, _result: self.account_refresh_timer.start()
        )
        self._sync_account_actions(self.tabs.currentIndex())
        self.apply_theme(theme)

    def _sync_rail_minimum_height(self, height: int) -> None:
        required = max(0, int(height))


        self.ticket.setMaximumHeight(max(1, required))
        if int(self.property("rightRailMinimumHeight") or 0) != required:
            self.setProperty("rightRailMinimumHeight", required)
            self.updateGeometry()
            self.rail_minimum_height_changed.emit()
        QTimer.singleShot(0, self._refresh_adaptive_activity)

    def set_bottom_panel(self, bottom: bool) -> None:
        bottom = bool(bottom)
        if bottom == self._bottom_panel:
            return
        self._bottom_panel = bottom
        self._refresh_adaptive_activity()

    def _adaptive_activity_candidate(
        self,
    ) -> tuple[str, dict[str, Any]] | None:
        symbol = self.symbol.upper()
        for payload in self._position_payloads:
            if str(payload.get("symbol") or "").upper() == symbol:
                return "position", dict(payload)
        for payload in self._open_orders:
            if str(payload.get("symbol") or "").upper() == symbol:
                return "order", dict(payload)
        return None

    def _clear_adaptive_activity(self) -> None:
        card = self._adaptive_activity_card
        self._adaptive_activity_card = None
        self._adaptive_activity_key = None
        if card is not None:
            self._adaptive_activity_layout.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.adaptive_activity_frame.hide()

    def _refresh_adaptive_activity(self) -> None:
        candidate = self._adaptive_activity_candidate()
        ticket_height = max(1, self.ticket.compact_required_height())
        spare_height = max(0, self.height() - ticket_height)
        if (
            candidate is None
            or not self.isVisible()
            or not self._bottom_panel
            or self._orders_drawer_open
            or spare_height < self._adaptive_activity_threshold
        ):
            self._clear_adaptive_activity()
            return

        kind, payload = candidate
        key = (
            kind,
            _account_key(payload)
            if kind == "order"
            else (
                str(payload.get("symbol") or ""),
                str(payload.get("positionSide") or "BOTH"),
            ),
        )
        card = self._adaptive_activity_card
        if key == self._adaptive_activity_key and card is not None:
            if kind == "position" and isinstance(card, CompactPositionActivityCard):
                card.update_payload(payload, self.symbol)
            elif kind == "order" and isinstance(card, CompactOrderActivityCard):
                card.update_payload(payload)
            else:
                self._clear_adaptive_activity()
                card = None
        if card is None or key != self._adaptive_activity_key:
            self._clear_adaptive_activity()
            if kind == "position":
                card = CompactPositionActivityCard(payload, self.symbol, self)
                card.selected_requested.connect(
                    lambda: self._open_adaptive_account_tab(0)
                )
            else:
                card = CompactOrderActivityCard(payload, self.symbol, self)
                card.selected_requested.connect(
                    lambda: self._open_adaptive_account_tab(2)
                )
            self._adaptive_activity_card = card
            self._adaptive_activity_key = key
            self._adaptive_activity_layout.addWidget(card)

        card_height = max(
            int(card.minimumHeight()),
            int(card.minimumSizeHint().height()),
            int(card.sizeHint().height()),
        )
        margins = self._adaptive_activity_layout.contentsMargins()
        required = card_height + margins.top() + margins.bottom()
        if spare_height < max(self._adaptive_activity_threshold, required):
            self.adaptive_activity_frame.hide()
            return
        self.adaptive_activity_frame.setFixedHeight(required)
        self.adaptive_activity_frame.show()

    def _open_adaptive_account_tab(self, tab_index: int) -> None:
        self.tabs.setCurrentIndex(int(tab_index))
        self.set_orders_drawer_open(True)

    def sync_embedded_ticket_height(self) -> int:
        """Compatibility shim: report content height without fixing panel geometry."""
        self.ensurePolished()
        self.layout().invalidate()
        self.layout().activate()
        return max(
            self.minimumSizeHint().height(),
            self.sizeHint().height(),
        )

    def set_top_aligned(self, top_aligned: bool) -> None:


        _ = top_aligned

    @staticmethod
    def _table(headers: tuple[str, ...]) -> QtWidgets.QTableWidget:
        table = AccountDataTable(headers)
        table.setObjectName("tradingDataTable")
        set_text_role(table, TextRole.TABLE_TEXT)
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setMinimumSectionSize(56)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        return table

    def set_symbol(self, symbol: str, rules: SymbolRules) -> None:
        self.symbol = symbol
        self.rules = rules
        self.ticket.set_symbol(symbol, rules)
        self.fills.setRowCount(0)
        self.fills.empty.setText(f"Loading fills for {symbol}…" if self.gateway.has_credentials() else "Connect API credentials to view fills")
        self.status.setText(f"{symbol} · ACCOUNT DATA")
        self._refresh_adaptive_activity()
        self._poll_account_if_open()

    def set_mark_price(self, price: float, symbol: str | None = None) -> None:
        symbol = str(symbol or self.symbol).upper()
        if symbol == self.symbol:
            self.ticket.set_mark_price(price)
        changed = False
        for row in self._position_payloads:
            if str(row.get("symbol") or "").upper() == symbol:
                _update_position_mark(row, price)
                changed = True
        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if (
                isinstance(card, PositionAccountCard)
                and str(card.payload.get("symbol") or "").upper() == symbol
            ):
                card.update_mark_price(price)
        adaptive = self._adaptive_activity_card
        if (
            isinstance(adaptive, CompactPositionActivityCard)
            and adaptive.isVisible()
            and str(adaptive.payload.get("symbol") or "").upper() == symbol
        ):
            adaptive.update_mark_price(price)
        if changed:
            self._refresh_total_pnl()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._orders_drawer_open and not self.account_age_timer.isActive():
            self.account_age_timer.start()
            self._refresh_account_freshness()
        QTimer.singleShot(0, self._refresh_adaptive_activity)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.account_age_timer.stop()
        self.adaptive_activity_frame.hide()
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_adaptive_activity()

    def set_page(self, index: int) -> None:
        self.set_orders_drawer_open(int(index) == 1)

    def set_orders_drawer_open(self, opened: bool) -> None:
        opened = bool(opened)
        if opened == self._orders_drawer_open:
            self.ticket.set_orders_drawer_open(opened)
            return
        self._orders_drawer_open = opened
        self.ticket.set_orders_drawer_open(opened)
        self._refresh_adaptive_activity()
        if opened:
            if not self.account_age_timer.isActive():
                self.account_age_timer.start()
            if self.gateway.has_credentials():
                self.gateway.refresh_account(
                    self.symbol,
                    True,
                    poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                    follow_up=False,
                )
        else:
            self.account_age_timer.stop()
        self.orders_drawer_toggled.emit(opened)

    def current_page(self) -> int:
        return 1 if self._orders_drawer_open else 0

    def _poll_account_if_open(self) -> None:
        if self._orders_drawer_open and self.gateway.has_credentials():
            self.gateway.refresh_account(
                self.symbol,
                True,
                poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                follow_up=False,
            )

    def _refresh_account_freshness(self) -> None:
        if not self._orders_drawer_open:
            return
        if not self.gateway.has_credentials():
            self.status.setText("ACCOUNT DATA · ADD API CREDENTIALS")
            return
        if self._last_account_snapshot_mono <= 0.0:
            self.status.setText("ACCOUNT DATA · WAITING FOR SNAPSHOT")
            return
        age = max(0.0, time.monotonic() - self._last_account_snapshot_mono)
        freshness = (
            f"STALE · {int(age)}s" if age >= 30.0
            else f"UPDATED {int(age)}s AGO"
        )
        self.status.setText(f"{self._account_status_base} · {freshness}")

    def _account_tab_changed(self, tab_index: int) -> None:
        self._sync_account_actions(tab_index)
        if self._orders_drawer_open and self.gateway.has_credentials():
            self.gateway.refresh_account(
                self.symbol,
                True,
                poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                follow_up=False,
            )

    def _sync_account_actions(self, tab_index: int) -> None:
        self.order_actions.setVisible(tab_index == 2)
        self._sync_batch_action()

    def _sync_batch_action(self) -> None:
        reducing = bool(self.ticket.reduce_only.isChecked())
        self.batch_button.setEnabled(not reducing)
        self.batch_button.setToolTip(
            "Batch limits are entry orders; switch the execution ticket to OPEN POSITION."
            if reducing
            else "Create up to five standard LIMIT / GTC entry orders for the current side."
        )

    def set_close_presets(self, preset: dict[str, Any]) -> None:
        self.close_percentages = [
            max(1, min(100, int(safe_float(preset.get(key), fallback))))
            for key, fallback in (
                ("close_1_percent", 25),
                ("close_2_percent", 50),
                ("close_3_percent", 100),
            )
        ]
        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if isinstance(card, PositionAccountCard):
                card.set_close_percentages(self.close_percentages)

    @staticmethod
    def _fill_table(
        table: QtWidgets.QTableWidget,
        rows: list[tuple[tuple[str, ...], dict[str, Any]]],
    ) -> None:
        fingerprint = tuple(values for values, _payload in rows)
        if (
            getattr(table, "_rows_fingerprint", None) == fingerprint
            and table.rowCount() == len(rows)
        ):
            for row_index, (_values, payload) in enumerate(rows):
                item = table.item(row_index, 0)
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, payload)
            return
        previous_row = table.currentRow()
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, (values, payload) in enumerate(rows):
            for column, value in enumerate(values):
                item = table.item(row_index, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    header_item = table.horizontalHeaderItem(column)
                    header = header_item.text().upper() if header_item is not None else ""
                    numeric = header in {
                        "PRICE", "SIZE", "PNL", "FEE", "WALLET", "AVAILABLE", "UPNL"
                    }
                    item.setFont(typography_font(
                        TextRole.TABLE_VALUE if numeric else TextRole.TABLE_TEXT
                    ))
                    if numeric:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    table.setItem(row_index, column, item)
                if item.text() != value:
                    item.setText(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, payload)
                if item.toolTip() != value:
                    item.setToolTip(value)
            if table.rowHeight(row_index) != 25:
                table.setRowHeight(row_index, 25)
        table.setSortingEnabled(True)
        table._rows_fingerprint = fingerprint
        if rows:
            table.selectRow(min(max(previous_row, 0), len(rows) - 1))


    def _clear_account_view(self, _key):
        self._open_orders = []
        self._last_account_snapshot_mono = 0.0
        self.apply_snapshot(
            {"account": {}, "ordersScope": "ALL", "orders": [], "algoOrders": [], "fills": []},
            mark_fresh=False,
        )

    def apply_snapshot(self, snapshot: dict[str, Any], *, mark_fresh: bool = True) -> None:
        if mark_fresh and self.gateway.has_credentials():
            self._last_account_snapshot_mono = time.monotonic()
        account = snapshot.get("account") or {}

        position_payloads: list[dict[str, Any]] = []
        for row in account.get("positions", []):
            amount = safe_float(row.get("positionAmt"))
            if abs(amount) <= 0:
                continue
            position_payloads.append(dict(row))
        position_payloads.sort(
            key=lambda row: (
                0 if str(row.get("symbol") or "") == self.symbol else 1,
                -_position_notional(row),
                -abs(safe_float(row.get("unrealizedProfit"))),
            )
        )
        self._position_payloads = [dict(row) for row in position_payloads]
        self._position_count = len(position_payloads)
        _populate_account_cards(
            self.positions,
            position_payloads,
            lambda payload: self._position_card(payload),
            "NO OPEN POSITIONS",
            update_existing=lambda card, payload: card.update_payload(payload, self.symbol)
            if isinstance(card, PositionAccountCard)
            else None,
        )
        _set_tab_text_if_changed(self.tabs, 0, f"POSITIONS {self._position_count}")

        if str(snapshot.get("ordersScope", "")).upper() == "ALL":
            combined_orders: list[dict[str, Any]] = []
            for source, source_rows in (
                ("STANDARD", snapshot.get("orders", [])),
                ("ALGO", snapshot.get("algoOrders", [])),
            ):
                for row in source_rows:
                    payload = dict(row)
                    payload["_source"] = source
                    status = str(
                        payload.get("status")
                        or payload.get("algoStatus")
                        or "NEW"
                    ).upper()
                    if status in {"NEW", "PARTIALLY_FILLED"}:
                        combined_orders.append(payload)

            combined_orders.sort(
                key=lambda row: (
                    0 if str(row.get("symbol") or "") == self.symbol else 1,
                    str(row.get("symbol") or ""),
                    str(row.get("type") or row.get("orderType") or ""),
                )
            )
            self._open_orders = [dict(row) for row in combined_orders]
            self._order_count = len(combined_orders)
            _populate_account_cards(
                self.orders,
                combined_orders,
                lambda payload: self._order_card(payload),
                "NO OPEN ORDERS",
                fingerprint=_order_card_render_fingerprint(combined_orders, self.symbol),
                update_existing=lambda card, payload: card.update_payload(payload)
                if isinstance(card, OrderAccountCard)
                else None,
            )
            _set_tab_text_if_changed(self.tabs, 2, f"ORDERS {self._order_count}")

        if snapshot.get("fillsSymbol", self.symbol) == self.symbol:
            self._fill_table(self.fills, _account_fill_rows(snapshot, self.symbol))
            self.fills.empty.setText(f"No fills for {self.symbol}" if self.gateway.has_credentials() else "Connect API credentials to view fills")

        balances: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        for row in account.get("assets", []):
            wallet = safe_float(row.get("walletBalance"))
            if abs(wallet) <= 0 and str(row.get("asset")) not in {
                "USDT", "USDC", "BFUSD"
            }:
                continue
            values = (
                str(row.get("asset", "")),
                human_number(wallet),
                human_number(safe_float(row.get("availableBalance"))),
                human_number(safe_float(row.get("unrealizedProfit")), money=True),
            )
            balances.append((values, dict(row)))
        self._fill_table(self.balances, balances)

        self._refresh_total_pnl()
        self._account_status_base = (
            f"ACCOUNT · {self._position_count} POSITION"
            f"{'S' if self._position_count != 1 else ''} · "
            f"{self._order_count} OPEN ORDER"
            f"{'S' if self._order_count != 1 else ''}"
        )
        self._refresh_adaptive_activity()
        if mark_fresh and self.gateway.has_credentials():
            self._refresh_account_freshness()
        elif not self.gateway.has_credentials():
            self.status.setText("ACCOUNT DATA · ADD API CREDENTIALS")

    def _refresh_total_pnl(self) -> None:
        total_pnl = sum(
            safe_float(row.get("unrealizedProfit"))
            for row in self._position_payloads
        )
        _set_text_if_changed(self.total_pnl, f"UPNL {_signed_money(total_pnl)}")
        _set_repolished_property(self.total_pnl, "pnl", _pnl_state(total_pnl))

    def _position_card(self, payload: dict[str, Any]) -> PositionAccountCard:
        card = PositionAccountCard(
            payload,
            self.theme,
            self.close_percentages,
            self.symbol,
        )
        card.close_requested.connect(self._close_position_payload)
        card.close_limit_requested.connect(self._close_position_limit_payload)
        return card

    def _order_card(self, payload: dict[str, Any]) -> OrderAccountCard:
        card = OrderAccountCard(payload, self.theme, self.symbol)
        card.cancel_requested.connect(self._cancel_order_payload)
        return card

    def _selected_payload(
        self,
        view: QtWidgets.QTableWidget | QtWidgets.QListWidget,
    ) -> dict[str, Any] | None:
        if isinstance(view, QtWidgets.QListWidget):
            item = view.currentItem()
            payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        else:
            row = view.currentRow()
            item = view.item(row, 0) if row >= 0 else None
            payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return dict(payload) if isinstance(payload, dict) else None

    def _cancel_order_payload(self, order: dict[str, Any]) -> None:
        if not order:
            return
        request = {"symbol": order.get("symbol")}
        algo = order.get("_source") == "ALGO"
        if algo:
            request["algoId"] = order.get("algoId") or order.get("orderId")
        else:
            request["orderId"] = order.get("orderId")
        self.gateway.submit_cancel(request, algo)

    def cancel_selected(self) -> None:
        order = self._selected_payload(self.orders)
        if not order:
            self.status.setText("SELECT AN ORDER TO CANCEL")
            return
        self._cancel_order_payload(order)

    def cancel_selected_symbol_orders(self) -> None:
        order = self._selected_payload(self.orders)
        symbol = str((order or {}).get("symbol") or self.symbol)
        if _confirm_cancel_symbol_orders(self, symbol):
            self.gateway.cancel_all(symbol)

    def open_batch_orders(self) -> None:
        if self.ticket.reduce_only.isChecked():
            self.status.setText("BATCH LIMITS ARE ENTRY ORDERS · SWITCH EXECUTION TO OPEN POSITION")
            return
        order_side = "BUY" if self.ticket.buy_button.isChecked() else "SELL"
        dialog = BatchOrderDialog(
            self.symbol,
            self.rules,
            order_side,
            self.ticket.position_side.currentText(),
            self,
        )
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.batch_orders_requested.emit(
                {
                    "orders": dialog.orders(),
                    "rules": self.rules,
                    "position_intent": "OPEN",
                }
            )

    def modify_selected(self) -> None:
        order = self._selected_payload(self.orders)
        if not order:
            self.status.setText("SELECT A STANDARD LIMIT ORDER TO MODIFY")
            return
        if order.get("_source") == "ALGO":
            self.status.setText("UNTRIGGERED CONDITIONAL ALGO ORDERS MUST BE CANCELED AND REPLACED")
            return
        symbol = str(order.get("symbol") or "")
        rules = self.symbol_rules.get(symbol)
        if rules is None and symbol == self.symbol:
            rules = self.rules
        if rules is None:
            self.status.setText(f"OPEN {symbol} ON THE CHART BEFORE MODIFYING THIS ORDER")
            return
        dialog = ModifyOrderDialog(order, rules, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.gateway.submit_modify(dialog.changes(), rules)

    def selected_position(self) -> dict[str, Any] | None:
        return self._selected_payload(self.positions)

    def existing_exit_quantity(self, position: dict[str, Any]) -> float:
        amount = safe_float(position.get("positionAmt"))
        symbol = str(position.get("symbol") or "")
        position_side = str(position.get("positionSide") or "BOTH").upper()
        exit_side = (
            "SELL"
            if position_side == "LONG" or (position_side == "BOTH" and amount > 0)
            else "BUY"
        )
        total = 0.0
        for order in self._open_orders:
            if str(order.get("symbol") or "") != symbol:
                continue
            if str(order.get("side", "")).upper() != exit_side:
                continue
            order_position_side = str(order.get("positionSide") or "BOTH").upper()
            reduce_only = order.get("reduceOnly") is True or str(
                order.get("reduceOnly", "")
            ).casefold() == "true"
            if position_side == "BOTH":
                if order_position_side != "BOTH" or not reduce_only:
                    continue
            elif order_position_side != position_side:
                continue
            original = safe_float(order.get("origQty") or order.get("quantity"))
            executed = safe_float(order.get("executedQty") or order.get("cumQty"))
            total += max(0.0, original - executed)
        return total

    def _close_position_payload(
        self,
        position: dict[str, Any],
        percent: int = 100,
    ) -> None:
        amount = safe_float(position.get("positionAmt"))
        if amount == 0:
            return
        symbol = str(position.get("symbol") or "")
        rules = self.symbol_rules.get(symbol)
        if rules is None and symbol == self.symbol:
            rules = self.rules
        if rules is None:
            self.status.setText(
                f"OPEN {symbol} ON THE CHART BEFORE CLOSING THIS POSITION"
            )
            return
        percent = max(1, min(100, int(percent)))
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), rules.market_step
            )
        except ValueError as exc:
            self.status.setText(f"CLOSE NOT SENT · {exc}")
            return
        quantity_value = safe_float(quantity)
        if not (rules.min_market_qty <= quantity_value <= rules.max_market_qty):
            self.status.setText("CLOSE SIZE IS OUTSIDE THE EXCHANGE MARKET-ORDER RANGE")
            return
        mark = safe_float(position.get("markPrice"))
        if rules.min_notional and mark > 0 and quantity_value * mark < rules.min_notional:
            self.status.setText("CLOSE VALUE IS BELOW THE EXCHANGE MINIMUM")
            return
        position_side = str(position.get("positionSide", "BOTH"))
        order: dict[str, Any] = {
            "symbol": position.get("symbol"),
            "side": _position_close_side(position),
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side,
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        self.order_requested.emit(
            {
                "order": order,
                "protections": {},
                "rules": rules,
                "position_intent": "REDUCE",
            }
        )

    def _close_position_limit_payload(
        self,
        position: dict[str, Any],
        percent: int = 100,
    ) -> None:
        amount = safe_float(position.get("positionAmt"))
        if amount == 0:
            return
        symbol = str(position.get("symbol") or "")
        rules = self.symbol_rules.get(symbol)
        if rules is None and symbol == self.symbol:
            rules = self.rules
        if rules is None:
            self.status.setText(f"NO EXCHANGE RULES AVAILABLE FOR {symbol}")
            return
        percent = max(1, min(100, int(percent)))
        dialog = CloseLimitDialog(position, percent, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), rules.lot_step
            )
            quantity_value = safe_float(quantity)
            if not (rules.min_qty <= quantity_value <= rules.max_qty):
                raise ValueError(
                    f"Close quantity must be between {rules.min_qty:g} and {rules.max_qty:g}."
                )
            price = validate_step(dialog.price_text(), rules.tick_size, "Limit price")
            price_value = safe_float(price)
            if not (rules.min_price <= price_value <= rules.max_price):
                raise ValueError(
                    f"Limit price must be between {format_price(rules.min_price)} "
                    f"and {format_price(rules.max_price)}."
                )
            mark = safe_float(position.get("markPrice"))
            if mark > 0:
                if rules.price_multiplier_up and price_value > mark * rules.price_multiplier_up:
                    raise ValueError("Limit price is above Binance's current price band.")
                if rules.price_multiplier_down and price_value < mark * rules.price_multiplier_down:
                    raise ValueError("Limit price is below Binance's current price band.")
            if rules.min_notional and quantity_value * price_value < rules.min_notional:
                raise ValueError(
                    f"Close value must be at least {rules.min_notional:g} {rules.quote_asset}."
                )
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid close limit", str(exc))
            return
        position_side = str(position.get("positionSide") or "BOTH")
        order: dict[str, Any] = {
            "symbol": symbol,
            "side": _position_close_side(position),
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
            "positionSide": position_side,
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        self.order_requested.emit(
            {
                "order": order,
                "protections": {},
                "rules": rules,
                "position_intent": "REDUCE",
            }
        )

    def close_selected_position(self, percent: int = 100) -> None:
        position = self._selected_payload(self.positions)
        if not position:
            self.status.setText("SELECT A POSITION TO CLOSE")
            return
        self._close_position_payload(position, percent)

    def kill_session(self) -> None:
        self.gateway.cancel_all(self.symbol)
        self.gateway.disarm()

    def _account_event(self, _event: dict[str, Any]) -> None:
        self.account_refresh_timer.start()

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.ticket.set_settings_icon(theme["muted"])




class CompactOrdersWidget(QtWidgets.QWidget):
    count_changed = Signal(int)

    def __init__(self, gateway, parent=None, theme=None, account_owner=None):
        super().__init__(parent)
        self.gateway = gateway
        self.theme = theme or {}
        self.account_owner = account_owner
        self.symbol = DEFAULT_SYMBOL
        self.rules = SymbolRules()
        self._snapshot = {}
        self._open_orders = []
        self._last_snapshot_mono = 0.0
        self.setObjectName("accountActivityPanel")
        self.setMinimumSize(0, 0)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("tradingAccountTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.positions = _new_account_card_list()
        self.list = _new_account_card_list()
        self.fills = TradingWorkspace._table(("TIME UTC", "SIDE", "PRICE", "SIZE", "PNL", "FEE"))
        self.tabs.addTab(self.positions, "POSITIONS")
        self.tabs.addTab(self.fills, "FILLS")
        self.tabs.addTab(self.list, "ORDERS")
        layout.addWidget(self.tabs, 1)
        self.status = ElidedLabel("Connect API credentials to view account activity")
        self.status.setObjectName("tradeAccountSummary")
        layout.addWidget(self.status)
        self.cancel_all = QtWidgets.QPushButton("CANCEL ALL SYMBOL ORDERS")
        self.cancel_all.setObjectName("dangerButton")
        self.cancel_all.clicked.connect(self._cancel_symbol_orders)
        layout.addWidget(self.cancel_all)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(20_000)
        self.refresh_timer.timeout.connect(self._poll_if_visible)
        self.refresh_timer.start()
        self.age_timer = QTimer(self)
        self.age_timer.setInterval(5_000)
        self.age_timer.timeout.connect(self._sync_scope)
        self.event_refresh_timer = QTimer(self)
        self.event_refresh_timer.setSingleShot(True)
        self.event_refresh_timer.setInterval(8_000)
        self.event_refresh_timer.timeout.connect(self._poll_if_visible)
        self.gateway.snapshot_ready.connect(self.apply_snapshot)
        self.gateway.credentials_changed.connect(self._clear_account_view)
        self.gateway.account_event.connect(self._account_event)
        self.gateway.request_succeeded.connect(self._request_succeeded)
        self.apply_snapshot({}, mark_fresh=False)

    def _cancel_symbol_orders(self) -> None:
        symbol = self.symbol
        if _confirm_cancel_symbol_orders(self, symbol):
            self.gateway.cancel_all(symbol)

    def set_symbol(self, symbol, rules):
        self.symbol, self.rules = symbol, rules
        self.fills.setRowCount(0)
        self.fills.empty.setText(f"Loading fills for {symbol}…" if self.gateway.has_credentials() else "Connect API credentials to view fills")
        self.apply_snapshot(self._snapshot, mark_fresh=False)
        self._poll_if_visible()

    def _poll_if_visible(self):
        if self.isVisible() and self.gateway.has_credentials():
            self.gateway.refresh_account(
                self.symbol,
                True,
                poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                follow_up=False,
            )

    def _tab_changed(self, _index):
        self._sync_scope()
        self._poll_if_visible()

    def _sync_scope(self):
        self.cancel_all.setVisible(self.tabs.currentIndex() == 2)
        current_symbol = str(self.symbol).upper()
        self.cancel_all.setEnabled(
            self.gateway.has_credentials()
            and any(
                str(row.get("symbol") or "").upper() == current_symbol
                for row in self._open_orders
            )
        )
        self.cancel_all.setToolTip(f"Cancel open orders for {self.symbol} only")
        if not self.gateway.has_credentials():
            self.status.setText("Connect API credentials to view account activity")
            return
        if self.tabs.currentIndex() == 1:
            base = f"{self.symbol} · FILLS · UTC"
        else:
            base = "ALL SYMBOLS · CURRENT PAIR FIRST"
        if self._last_snapshot_mono <= 0.0:
            freshness = "WAITING FOR SNAPSHOT"
        else:
            age = max(0.0, time.monotonic() - self._last_snapshot_mono)
            freshness = f"STALE · {int(age)}s" if age >= 30.0 else f"UPDATED {int(age)}s AGO"
        self.status.setText(f"{base} · {freshness}")

    def _account_event(self, _event):
        if self.isVisible() and not self.event_refresh_timer.isActive():
            self.event_refresh_timer.start()

    def _request_succeeded(self, _request, _result):
        self._account_event({})

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if not self.age_timer.isActive():
            self.age_timer.start()
        self._sync_scope()
        QTimer.singleShot(0, self._poll_if_visible)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.age_timer.stop()
        super().hideEvent(event)

    def _position_card(self, payload):
        if self.account_owner is not None:
            return self.account_owner._position_card(payload)
        card = PositionAccountCard(payload, self.theme, [25, 50, 100], self.symbol)
        for button in card.findChildren(QtWidgets.QAbstractButton):
            button.setEnabled(False)
        return card

    def _card(self, payload):
        card = OrderAccountCard(payload, self.theme, self.symbol)
        card.cancel_requested.connect(self._cancel_payload)
        return card

    def _clear_account_view(self, _key):
        self._open_orders = []
        self._last_snapshot_mono = 0.0
        self.apply_snapshot(
            {"account": {}, "ordersScope": "ALL", "orders": [], "algoOrders": [], "fills": []},
            mark_fresh=False,
        )

    def apply_snapshot(self, snapshot, *, mark_fresh=True):
        self._snapshot = dict(snapshot)
        if mark_fresh and self.gateway.has_credentials():
            self._last_snapshot_mono = time.monotonic()
        connected = self.gateway.has_credentials()
        positions = [dict(row) for row in (snapshot.get("account") or {}).get("positions", [])
                     if abs(safe_float(row.get("positionAmt"))) > 0]
        positions.sort(key=lambda row: (row.get("symbol") != self.symbol, -_position_notional(row)))
        _populate_account_cards(
            self.positions,
            positions,
            self._position_card,
            "No open positions" if connected else "Connect API credentials to view positions",
            update_existing=lambda card, payload: card.update_payload(payload, self.symbol)
            if isinstance(card, PositionAccountCard)
            else None,
        )
        if str(snapshot.get("ordersScope", "")).upper() == "ALL":
            orders = []
            for source, rows in (("STANDARD", snapshot.get("orders", [])), ("ALGO", snapshot.get("algoOrders", []))):
                for row in rows:
                    if str(row.get("status") or row.get("algoStatus") or "NEW").upper() in {"NEW", "PARTIALLY_FILLED"}:
                        orders.append({**row, "_source": source})
            orders.sort(key=lambda row: (row.get("symbol") != self.symbol, str(row.get("symbol", ""))))
            self._open_orders = orders
        _populate_account_cards(
            self.list,
            self._open_orders,
            self._card,
            "No open orders" if connected else "Connect API credentials to view orders",
            fingerprint=_order_card_render_fingerprint(self._open_orders, self.symbol),
            update_existing=lambda card, payload: card.update_payload(payload)
            if isinstance(card, OrderAccountCard)
            else None,
        )
        if snapshot.get("fillsSymbol", self.symbol) == self.symbol:
            TradingWorkspace._fill_table(self.fills, _account_fill_rows(snapshot, self.symbol))
            self.fills.empty.setText(f"No fills for {self.symbol}" if connected else "Connect API credentials to view fills")
        _set_tab_text_if_changed(self.tabs, 0, f"POSITIONS {len(positions)}")
        _set_tab_text_if_changed(self.tabs, 2, f"ORDERS {len(self._open_orders)}")
        order_count = len(self._open_orders)
        if getattr(self, "_published_order_count", None) != order_count:
            self._published_order_count = order_count
            self.count_changed.emit(order_count)
        self._sync_scope()

    def set_mark_price(self, price, symbol=None):
        symbol = symbol or self.symbol
        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if isinstance(card, PositionAccountCard) and card.payload.get("symbol") == symbol:
                card.update_mark_price(price)

    def _cancel_payload(self, payload):
        if not payload:
            return
        algo = payload.get("_source") == "ALGO"
        symbol = str(payload.get("symbol") or "").upper()
        identifier_key = "algoId" if algo else "orderId"
        identifier = payload.get(identifier_key)
        if not symbol or identifier in (None, ""):
            self.status.setText("ORDER CANNOT BE CANCELED · MISSING EXCHANGE ID")
            return
        request = {"symbol": symbol, identifier_key: identifier}
        self.gateway.submit_cancel(request, algo)

    def cancel_selected(self):
        item = self.list.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(payload, dict):
            self._cancel_payload(payload)

    def apply_theme(self, theme):
        self.theme = theme

        self.apply_snapshot(self._snapshot, mark_fresh=False)

