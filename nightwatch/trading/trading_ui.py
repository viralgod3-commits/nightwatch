"""Manual order entry, protection plans, and account/trading workspaces."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
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
from ..theme import (
    DEFAULT_THEME_NAME, THEMES, ui_palette,
    _contrast_ratio_hex, _ensure_directional_contrast,
)
from ..utilities import ElidedLabel, asset_svg_icon, format_local_time, line_icon
from ..utilities import TextRole, set_text_role, typography_controller, typography_font
from ..models import format_price, human_number, quantize_step, safe_float, validate_step
from .orders import (
    is_shift_letter_shortcut,
    is_smart_exit_shortcut,
)
from .account_state import exchange_bool


_ACCOUNT_POLL_MINIMUM_INTERVAL = 8.0
_TIME_IN_FORCE_HINTS = {
    "GTC": "Stays open until filled or canceled.",
    "IOC": "Fills immediately; cancels any remainder.",
    "FOK": "Fills completely now or cancels the entire order.",
    "GTX": "Post only; cancels if it would fill immediately.",
}


def _trading_stylesheet(theme: dict[str, str]) -> str:
    """Theme-aware surfaces scoped to the trading workspace."""
    t = ui_palette(theme or THEMES[DEFAULT_THEME_NAME])
    # Directional fills can be deliberately dark; small trading text still
    # needs to remain readable on normal, raised and selected card surfaces.
    for key in ("green", "red", "amber"):
        background = min((t["panel"], t["panel2"], t["control_hover"]),
                         key=lambda surface: _contrast_ratio_hex(t[key], surface))
        t[f"{key}_text"] = _ensure_directional_contrast(
            t[key], background, fallback=t[key], target_ratio=4.5)
    def tint(key: str, alpha: int) -> str:
        color = QtGui.QColor(t[key])
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"

    return f"""
        QWidget#responsiveOrderTicket, QWidget#tradingWorkspace,
        QWidget#accountActivityPanel, QWidget#positionDeskBody,
        QStackedWidget#tradingPages, QTabWidget#tradingAccountTabs,
        QTabWidget#tradingAccountTabs > QStackedWidget,
        QTabWidget#tradingAccountTabs QTabBar,
        QFrame#positionDeskView, QScrollArea#positionDeskScroll,
        QScrollArea#deskTicketScroll {{ background: {t['panel']}; border: 0; }}
        QFrame#tradingWorkspaceHeader {{ background: {t['panel2']}; border: 0; border-bottom: 1px solid {t['border']}; }}
        QTabBar#ticketOrderTypes, QSlider#ticketAllocation,
        QWidget#tradingAdaptiveActivity {{ background: {t['panel']}; border: 0; }}
        QFrame#tradingAdaptiveActivity {{ border-top: 1px solid {t['border']}; }}
        QPushButton#activityHeading {{ background: transparent; border: 0; padding: 2px 0; text-align: left; }}
        QPushButton#activityHeading:hover {{ color: {t['cyan']}; }}
        QPushButton#activityHeading:focus {{ border: 1px solid {t['cyan']}; }}
        QPushButton#activityOrderCancel {{ padding: 0; min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px; }}
        QWidget#tradingActivityBody, QWidget#tradingActivitySection {{ background: {t['panel']}; border: 0; }}
        QTabBar#tradingViewSwitch {{ background: transparent; border: 0; }}
        QScrollArea#deskTicketScroll > QWidget, QScrollArea#positionDeskScroll > QWidget {{ background: {t['panel']}; border: 0; }}
        QFrame#ticketFeedback {{ background: {tint('amber', 15)}; border: 1px solid {tint('amber', 65)}; border-radius: 6px; }}
        QLabel {{ background: transparent; border: 0; padding: 0; color: {t['text']}; }}
        QLabel#accountCardDetail, QLabel#ticketFieldCaption, QLabel#ticketContext {{ color: {t['muted']}; }}
        QWidget#tradingInlineField, QFrame#ticketField, QFrame#tradingPanelCard,
        QFrame#tradingMarginRow {{ background: transparent; border: 0; }}
        QLineEdit, QComboBox, QDoubleSpinBox {{
            background: {t['control']}; color: {t['text']}; border: 1px solid {t['control_border']};
            border-radius: 4px; min-width: 0; min-height: 0; padding: 4px 8px;
            selection-background-color: {t['control_hover']};
        }}
        QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover {{ border-color: {t['muted']}; }}
        QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border-color: {t['cyan']}; background: {t['panel2']}; }}
        QLineEdit:disabled, QComboBox:disabled {{ color: {t['muted']}; border-color: {t['border']}; }}
        QFrame#ticketInputGroup {{ background: {t['control']}; border: 1px solid {t['control_border']}; border-radius: 4px; }}
        QFrame#ticketInputGroup:hover {{ border-color: {t['muted']}; }}
        QFrame#ticketInputGroup[focused="true"] {{ border-color: {t['cyan']}; }}
        QFrame#ticketInputGroup QLineEdit, QFrame#ticketInputGroup QComboBox {{ background: transparent; border: 0; border-radius: 0; }}
        QFrame#ticketInputGroup QComboBox {{ border-left: 1px solid {t['control_border']}; }}
        QToolButton#tradeMarkButton {{ background: transparent; color: {t['muted']}; border: 0; border-left: 1px solid {t['control_border']}; border-radius: 0; padding: 0 6px; }}
        QToolButton#tradeMarkButton:hover, QToolButton#tradeMarkButton:focus {{ color: {t['text']}; background: {t['control_hover']}; }}
        QToolButton#tradeMarkButton:disabled {{ color: {t['muted']}; background: transparent; }}
        QFrame#ticketExecutionOptions {{ background: transparent; border: 0; border-top: 1px solid {t['border']}; }}
        QComboBox {{ padding-left: 8px; padding-right: 24px; }}
        QWidget#responsiveOrderTicket QComboBox {{ padding-right: 24px; }}
        QComboBox::drop-down {{ border: 0; width: 0; }}
        QComboBox::down-arrow, QToolButton::menu-indicator, QPushButton::menu-indicator {{ image: none; width: 0; height: 0; }}
        QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {{ image: none; width: 0; height: 0; }}
        QComboBox QAbstractItemView, QMenu {{ background: {t['panel2']}; color: {t['text']}; selection-background-color: {t['control_hover']}; border: 1px solid {t['control_border']}; padding: 6px; }}
        QMenu::item:selected {{ background: {t['control_hover']}; }}
        QPushButton, QToolButton {{
            background: {t['control']}; color: {t['text']}; border: 1px solid {t['control_border']};
            border-radius: 4px; min-width: 0; min-height: 18px; padding: 4px 8px;
        }}
        QPushButton:hover, QToolButton:hover {{ background: {t['control_hover']}; border-color: {t['muted']}; }}
        QPushButton:pressed, QToolButton:pressed {{ background: {t['panel2']}; }}
        QPushButton:focus, QToolButton:focus {{ border-color: {t['cyan']}; }}
        QPushButton:disabled, QToolButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; background: {t['control']}; }}
        QToolButton#tradeSettingsMini {{ border: 0; background: transparent; padding: 0; min-height: 0; }}
        QPushButton#buySideButton {{ color: {t['text']}; border-color: {t['green']}; background: {tint('green', 36)}; }}
        QPushButton#sellSideButton {{ color: {t['text']}; border-color: {t['red']}; background: {tint('red', 36)}; }}
        QPushButton#buySideButton:hover {{ background: {tint('green', 65)}; }}
        QPushButton#sellSideButton:hover {{ background: {tint('red', 65)}; }}
        QPushButton#buySideButton:disabled, QPushButton#sellSideButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; background: {t['control']}; }}
        QPushButton#protectionButton[active="true"] {{ color: {t['cyan']}; border-color: {t['cyan']}; }}
        QTabBar#tradingViewSwitch::tab {{
            background: {t['control']}; color: {t['muted']}; border: 1px solid {t['border']};
            border-radius: 3px; padding: 5px; margin: 0; min-width: 16px; max-width: 16px; min-height: 16px; max-height: 16px;
        }}
        QTabBar#tradingViewSwitch::tab:selected {{
            color: {t['text']}; background: {t['control_hover']}; border-color: {t['muted']};
        }}
        QPushButton#portfolioSyncButton {{ background: transparent; border: 0; padding: 0; min-width: 28px; max-width: 28px; min-height: 26px; }}
        QPushButton#portfolioSyncButton:hover, QPushButton#portfolioSyncButton:focus {{ background: {t['control_hover']}; }}
        QCheckBox#ticketReduceOnly {{ background: transparent; color: {t['text']}; spacing: 8px; padding: 3px 0; }}
        QCheckBox#ticketReduceOnly::indicator {{ width: 15px; height: 15px; background: {t['control']}; border: 1px solid {t['control_border']}; border-radius: 3px; }}
        QCheckBox#ticketReduceOnly::indicator:checked {{ background: {t['cyan']}; border-color: {t['cyan']}; }}
        QCheckBox#ticketReduceOnly::indicator:focus {{ border-color: {t['text']}; }}
        QTabBar#ticketOrderTypes::tab, QTabWidget#tradingAccountTabs QTabBar::tab {{
            background: transparent; color: {t['muted']}; border: 0;
            border-bottom: 2px solid {t['border']}; padding: 6px 8px;
        }}
        QTabBar#ticketOrderTypes::tab:selected, QTabWidget#tradingAccountTabs QTabBar::tab:selected {{ color: {t['text']}; border-bottom-color: {t['cyan']}; }}
        QTabBar::tab:hover {{ color: {t['text']}; }}
        QTabWidget#tradingAccountTabs::pane {{ border: 0; background: {t['panel']}; }}
        QSlider#ticketAllocation::groove:horizontal {{ height: 4px; background: {t['border']}; border-radius: 2px; }}
        QSlider#ticketAllocation::sub-page:horizontal {{ background: {t['cyan']}; border-radius: 2px; }}
        QSlider#ticketAllocation::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0; background: {t['text']}; border: 2px solid {t['panel']}; border-radius: 7px; }}
        QSlider#ticketAllocation:focus::handle:horizontal {{ border-color: {t['cyan']}; }}
        QPushButton#sizePresetButton {{ color: {t['muted']}; background: transparent; border: 0; padding: 0; min-height: 0; }}
        QPushButton#sizePresetButton:hover, QPushButton#sizePresetButton:checked, QPushButton#sizePresetButton:focus {{ color: {t['text']}; background: {t['control_hover']}; }}
        QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0; border: 0; }}
        QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 0; border: 0; }}
        QScrollBar::handle:vertical {{ background: {t['control_border']}; min-height: 28px; border-radius: 3px; }}
        QScrollBar::handle:horizontal {{ background: {t['control_border']}; min-width: 28px; border-radius: 3px; }}
        QScrollBar::handle:hover {{ background: {t['muted']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QLabel#accountCardSide[direction="long"], QLabel[pnl="positive"] {{ color: {t['green_text']}; }}
        QLabel#accountCardSide[direction="short"], QLabel[pnl="negative"] {{ color: {t['red_text']}; }}
        QLabel#accountCardRisk[risk="warning"], QLabel#accountOrderStatus[state="partial"] {{ color: {t['amber_text']}; }}
        QLabel#accountCardRisk[risk="critical"] {{ color: {t['red_text']}; }}
        QFrame#positionDeskRow, QFrame#workingOrderRow {{ background: {t['panel2']}; border: 1px solid {t['border']}; border-radius: 6px; }}
        QFrame#positionDeskRow[selected="true"], QFrame#workingOrderRow[selected="true"] {{ background: {t['control_hover']}; border-color: {t['cyan']}; }}
        QWidget#tradingActivitySection QFrame#positionDeskRow {{ background: transparent; border: 0; border-bottom: 1px solid {t['border']}; border-radius: 0; }}
        QWidget#tradingActivitySection QFrame#positionDeskRow:hover {{ background: {t['control_hover']}; }}
        QFrame#positionDeskDetails {{ background: {t['panel2']}; border: 1px solid {t['border']}; border-radius: 8px; }}
        QFrame#deskRiskMetrics {{ background: transparent; border: 0; border-top: 1px solid {t['border']}; }}
        QListWidget#tradingCardList, QListView#tradingFillList {{ border: 0; background: {t['panel']}; padding: 0 6px 0 0; }}
        QListWidget#tradingCardList::item, QListView#tradingFillList::item {{ border: 0; padding: 0; background: transparent; }}
        QListWidget#tradingCardList::item:selected, QListView#tradingFillList::item:selected {{ background: transparent; }}
        QTableWidget#tradingDataTable {{ background: {t['panel']}; color: {t['text']}; border: 0; }}
        QTableWidget#tradingDataTable::item {{ padding: 5px; border-bottom: 1px solid {t['border']}; }}
        QTableWidget#tradingDataTable::item:selected {{ background: {t['control_hover']}; }}
        QTableWidget#tradingDataTable QHeaderView::section {{ background: {t['panel2']}; color: {t['muted']}; border: 0; padding: 7px 3px; }}
        QFrame#accountEmptyState {{ background: transparent; border: 0; }}
        QProgressBar#orderFillProgress {{ border: 0; background: {t['border']}; max-height: 3px; min-height: 3px; }}
        QProgressBar#orderFillProgress::chunk {{ background: {t['cyan']}; }}
        QPushButton#dangerButton {{ color: {t['red_text']}; }}
        QPushButton#dangerButton:hover {{ border-color: {t['red']}; }}
    """


def _paint_trade_arrow(widget: QtWidgets.QWidget, painter: QtGui.QPainter,
                       rect: QtCore.QRect, *, up: bool = False) -> None:
    """Use Qt's native arrow primitive even when the shell's SVG is absent."""
    application = QtWidgets.QApplication.instance()
    native = getattr(application, "_nightwatch_trade_arrow_style", None)
    if native is None:
        native = QtWidgets.QStyleFactory.create("Fusion")
        # Rail moves can dispose widget-owned styles while repolishing controls.
        # One application-owned primitive style survives every panel placement.
        native.setParent(application)
        application._nightwatch_trade_arrow_style = native
    option = QtWidgets.QStyleOption()
    option.initFrom(widget)
    option.rect = rect
    native.drawPrimitive(
        QtWidgets.QStyle.PrimitiveElement.PE_IndicatorArrowUp if up
        else QtWidgets.QStyle.PrimitiveElement.PE_IndicatorArrowDown,
        option, painter,
    )


class TradingRateSpinBox(QtWidgets.QDoubleSpinBox):
    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow "
                          "{ image: none; width: 0; height: 0; }")

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionSpinBox()
        self.initStyleOption(option)
        painter = QtGui.QPainter(self)
        for subcontrol, up in ((QtWidgets.QStyle.SubControl.SC_SpinBoxUp, True),
                               (QtWidgets.QStyle.SubControl.SC_SpinBoxDown, False)):
            rect = self.style().subControlRect(QtWidgets.QStyle.ComplexControl.CC_SpinBox,
                                               option, subcontrol, self)
            _paint_trade_arrow(self, painter, rect.adjusted(3, 1, -3, -1), up=up)


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
        ("refresh_account", "Update portfolio data"),
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
            'Share of available margin used before leverage.'
        )
        self.leverage = QtWidgets.QSpinBox()
        self.leverage.setRange(1, 125)
        self.leverage.setSuffix("×")
        self.leverage.setValue(int(safe_float(merged["leverage"])))
        self.leverage.setToolTip(
            'Saving applies this leverage to the active symbol before entry shortcuts can run.'
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
            button.clicked.connect(
                lambda _checked=False, target=value: self.leverage.setValue(target)
            )
            self.quick_leverage_buttons.append(button)
            leverage_layout.addWidget(button)
        self.order_mode = QtWidgets.QComboBox()
        self.order_mode.addItems(("MARKET", "LIMIT"))
        self.order_mode.setCurrentText(str(merged["order_mode"]).upper())
        self.order_mode.setToolTip(
            'Market buys or sells now. Limit uses the same-side best price and may not fill.'
        )
        self.time_in_force = QtWidgets.QComboBox()
        self.time_in_force.addItem("IOC", "IOC")
        self.time_in_force.addItem("GTC", "GTC")
        self.time_in_force.addItem("FOK", "FOK")
        self.time_in_force.addItem("GTX · POST-ONLY", "GTX")
        self.time_in_force.setCurrentIndex(
            max(0, self.time_in_force.findData(str(merged["time_in_force"]).upper()))
        )
        for index in range(self.time_in_force.count()):
            self.time_in_force.setItemData(index, _TIME_IN_FORCE_HINTS[self.time_in_force.itemData(index)], Qt.ItemDataRole.ToolTipRole)
        self.time_in_force.setToolTip(_TIME_IN_FORCE_HINTS[self.time_in_force.currentData()])
        self.time_in_force.currentIndexChanged.connect(lambda _index: self.time_in_force.setToolTip(_TIME_IN_FORCE_HINTS[self.time_in_force.currentData()]))
        self.slippage_enabled = QtWidgets.QCheckBox("Cap market slippage")
        self.slippage_enabled.setChecked(bool(merged["slippage_enabled"]))
        self.slippage_enabled.setToolTip(
            'Use a limit order to cap the execution price. A fill is not guaranteed.'
        )
        self.max_slippage = QtWidgets.QDoubleSpinBox()
        self.max_slippage.setRange(0.01, 10.0)
        self.max_slippage.setDecimals(2)
        self.max_slippage.setSuffix("%")
        self.max_slippage.setValue(safe_float(merged["max_slippage_percent"]))
        self.max_slippage.setToolTip(
            'Price limit from the best ask for buys or best bid for sells. A tighter cap may not fill.'
        )
        slippage_box = QtWidgets.QWidget()
        slippage_layout = QtWidgets.QHBoxLayout(slippage_box)
        slippage_layout.setContentsMargins(0, 0, 0, 0)
        slippage_layout.addWidget(self.slippage_enabled)
        slippage_layout.addWidget(self.max_slippage)
        self.reduce_only = QtWidgets.QCheckBox("Reduce existing position only")
        self.reduce_only.setChecked(bool(merged["reduce_only"]))
        self.reduce_only.setToolTip(
            'Only reduces a position; cannot open or reverse it. One-way mode only.'
        )

        def add_row(label_text: str, field: QtWidgets.QWidget, tooltip: str) -> None:
            label = QtWidgets.QLabel(label_text)
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
        self.take_profit_enabled.setToolTip('Place a take-profit after the entry fills.')
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
        self.take_profit_close.setToolTip('Share of the filled entry to close at take-profit.')
        tp_box = QtWidgets.QWidget()
        tp_layout = QtWidgets.QHBoxLayout(tp_box)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.addWidget(self.take_profit_enabled)
        tp_layout.addWidget(self.take_profit_percent)
        tp_layout.addWidget(self.take_profit_close)
        add_row("Take profit", tp_box, "Optional reduce-only take-profit placed after an OPEN shortcut fills.")

        self.stop_loss_enabled = QtWidgets.QCheckBox("Enabled")
        self.stop_loss_enabled.setChecked(bool(merged["stop_loss_enabled"]))
        self.stop_loss_enabled.setToolTip('Place a stop-loss after the entry fills.')
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
        self.stop_loss_close.setToolTip('Share of the filled entry to close at stop-loss.')
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
            "place_buy": 'Buy with the saved preset. Reduce only closes a short.',
            "place_sell": 'Sell with the saved preset. Reduce only closes a long.',
            "close_1": "Close the selected position using close percentage preset 1.",
            "close_2": "Close the selected position using close percentage preset 2.",
            "close_3": "Close the selected position using close percentage preset 3.",
            "cancel_all": "Cancel every active order for the current symbol.",
            "open_trading": "Open the chart workspace with the trading sidebar visible.",
            "refresh_account": "Update positions, orders, fills and balances.",
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


class TicketPriceEdit(QtWidgets.QLineEdit):
    """Keep the price's purpose and currency visible while editing its value."""

    def __init__(self, label: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setAccessibleName(label)
        self.caption_label = QtWidgets.QLabel(label.removesuffix(" price"), self)
        self.caption_label.setObjectName("ticketFieldCaption")
        self.unit_label = QtWidgets.QLabel("USDT", self)
        self.unit_label.setObjectName("ticketFieldCaption")
        for decoration in (self.caption_label, self.unit_label):
            decoration.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            set_text_role(decoration, TextRole.UI_CAPTION)
            decoration.installEventFilter(self)
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._sync_unit()

    def set_quote_asset(self, asset: str) -> None:
        self.unit_label.setText(asset)
        self._sync_unit()

    def _sync_unit(self) -> None:
        metrics = self.unit_label.fontMetrics()
        width = metrics.horizontalAdvance(self.unit_label.text())
        caption_metrics = self.caption_label.fontMetrics()
        caption_width = caption_metrics.horizontalAdvance(self.caption_label.text())
        self.setTextMargins(caption_width + 8, 0, width + 8, 0)
        self.caption_label.setGeometry(9, max(0, (self.height() - caption_metrics.height()) // 2),
                                       caption_width, caption_metrics.height())
        self.unit_label.setGeometry(max(0, self.width() - width - 9),
                                    max(0, (self.height() - metrics.height()) // 2),
                                    width, metrics.height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_unit()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if hasattr(self, "unit_label") and event.type() in {
            QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange,
        }:
            QTimer.singleShot(0, self, self._sync_unit)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched in (self.unit_label, self.caption_label) and event.type() == QtCore.QEvent.Type.FontChange:
            QTimer.singleShot(0, self, self._sync_unit)
        return super().eventFilter(watched, event)


class TicketInputGroup(QtWidgets.QFrame):
    """One input surface for an editor and its unit or price shortcut."""

    def __init__(self, editor: QtWidgets.QWidget, accessory: QtWidgets.QWidget):
        super().__init__()
        self.setObjectName("ticketInputGroup")
        self.setProperty("focused", False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(1, 1, 1, 1)
        row.setSpacing(0)
        row.addWidget(editor, 1)
        row.addWidget(accessory)
        for control in (editor, accessory):
            control.installEventFilter(self)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() in {QtCore.QEvent.Type.FocusIn, QtCore.QEvent.Type.FocusOut}:
            _set_repolished_property(self, "focused", event.type() == QtCore.QEvent.Type.FocusIn)
        return super().eventFilter(watched, event)


class TicketPercentageSlider(QtWidgets.QSlider):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class TicketAllocationControl(QtWidgets.QFrame):
    """Percentage slider with clickable, position-aligned quarter marks."""

    percentage_changed = Signal(int)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.slider = TicketPercentageSlider(Qt.Orientation.Horizontal, self)
        self.slider.setObjectName("ticketAllocation")
        self.slider.setRange(0, 100)
        self.slider.setPageStep(25)
        self.slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.slider.setAccessibleName("Available margin percentage")
        self.slider.installEventFilter(self)
        self.slider.valueChanged.connect(self.percentage_changed)
        self.buttons: list[QtWidgets.QPushButton] = []
        for percent in (25, 50, 75, 100):
            button = QtWidgets.QPushButton(f"{percent}%", self)
            button.setObjectName("sizePresetButton")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            set_text_role(button, TextRole.UI_CAPTION)
            self.buttons.append(button)

    def _slider_geometry(self):
        option = QtWidgets.QStyleOptionSlider()
        self.slider.initStyleOption(option)
        style = self.slider.style()
        groove = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                       QtWidgets.QStyle.SubControl.SC_SliderGroove, self.slider)
        handle = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                       QtWidgets.QStyle.SubControl.SC_SliderHandle, self.slider)
        return option, groove, handle

    def sync_geometry(self) -> None:
        if not self.buttons:
            return
        tick_height = max(button.fontMetrics().height() for button in self.buttons) + 4
        track_height = max(16, tick_height - 4)
        self.setFixedHeight(track_height + tick_height)
        self.slider.setGeometry(0, 0, self.width(), track_height)
        option, groove, handle = self._slider_geometry()
        span = max(0, groove.width() - handle.width())
        positions = []
        for percent, button in zip((25, 50, 75, 100), self.buttons):
            center = groove.left() + handle.width() // 2 + QtWidgets.QStyle.sliderPositionFromValue(
                0, 100, percent, span, option.upsideDown)
            positions.append((center, button))
        right_edge = self.width()
        for center, button in sorted(positions, key=lambda item: item[0], reverse=True):
            width = button.fontMetrics().horizontalAdvance(button.text()) + 6
            left = max(0, min(right_edge - width, center - width // 2))
            button.setGeometry(left, track_height, width, tick_height)
            right_edge = left - 4

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.sync_geometry()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self.slider:
            if (event.type() == QtCore.QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                option, groove, handle = self._slider_geometry()
                if not handle.contains(event.position().toPoint()):
                    span = max(1, groove.width() - handle.width())
                    pixel = round(event.position().x()) - groove.left() - handle.width() // 2
                    value = QtWidgets.QStyle.sliderValueFromPosition(0, 100, pixel, span, option.upsideDown)
                    self.slider.setValue(value)
        return super().eventFilter(watched, event)


class TicketScrollArea(QtWidgets.QScrollArea):
    """Follow the form's natural height; give up space only in short panels."""

    def sizeHint(self) -> QtCore.QSize:
        body = self.widget()
        return body.layout().sizeHint() if body is not None else QtCore.QSize(0, 24)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 24)


class CompactTradeComboBox(QtWidgets.QComboBox):
    """A compact native combo with the platform-rendered dropdown indicator."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )


        self.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents
        )

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        # The shell suppresses stylesheet arrows. Paint a native primitive so
        # leverage, units and trigger source still read as selectable controls.
        painter = QtGui.QPainter(self)
        _paint_trade_arrow(self, painter, QtCore.QRect(self.width() - 21,
                                                     (self.height() - 12) // 2, 12, 12))



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

    def is_editing(self) -> bool:
        return self._popup_open or (self._custom_editor is not None and self._custom_editor.isVisible())

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
    quick_settings_requested = Signal()
    minimum_content_height_changed = Signal(int)
    position_target_selected = Signal(object)

    ORDER_TYPES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Limit", "LIMIT"),
        ("Market", "MARKET"),
        ("Stop limit", "STOP"),
        ("Stop market", "STOP_MARKET"),
        ("Trailing stop", "TRAILING_STOP_MARKET"),
    )
    OPEN_SIZE_MODES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Quantity", "CONTRACTS"),
        ("USDT value", "QUOTE NOTIONAL"),
        ("Risk %", "RISK %"),
    )
    REDUCE_SIZE_MODES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Quantity", "CONTRACTS"),
        ("Position %", "POSITION %"),
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
        self._available_margin: float | None = None
        self._balance_asset = self.rules.margin_asset
        self._account_problem = ""
        self._leverage_problem = ""
        self._last_confirmed_leverage = 5
        self._allocation_percent: int | None = None
        self.protection_plans: dict[str, list[dict[str, float]]] = {"tp": [], "sl": []}
        self.hedge_mode: bool | None = None
        self.setObjectName("responsiveOrderTicket")
        self._published_compact_minimum_height = 0


        self.setMinimumWidth(0 if compact else 350)
        self.setMinimumHeight(0 if compact else 430)
        self.setStyleSheet(_trading_stylesheet({}))
        self._position_cache: list[dict[str, Any]] = []
        self._desk_syncing = False
        self._desk_selected_key = None
        self._build_ticket()
        self.quick_settings_button.clicked.connect(self.quick_settings_requested)
        self.type_combo.currentIndexChanged.connect(
            lambda _index: self._type_changed(self.current_order_type())
        )
        self.order_type_tabs.currentChanged.connect(self._select_order_type_tab)
        self.position_side.currentTextChanged.connect(self._position_side_changed)
        self.size_mode.currentIndexChanged.connect(
            lambda _index: self._size_mode_changed(str(self.size_mode.currentData()))
        )
        self.size_mode.activated.connect(self._clear_margin_allocation)
        self.quantity_edit.textEdited.connect(self._clear_margin_allocation)
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
        self.buy_button.clicked.connect(lambda: self._submit_from_side(True))
        self.sell_button.clicked.connect(lambda: self._submit_from_side(False))
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
        self.gateway.account_status_changed.connect(self._account_status_changed)
        self.gateway.leverage_changing.connect(self._leverage_changing)
        self.gateway.leverage_changed.connect(self._leverage_changed)
        self._readiness_fresh_state = self._mark_is_fresh()
        self._readiness_timer = QTimer(self)
        self._readiness_timer.setInterval(1000)
        self._readiness_timer.timeout.connect(self._refresh_execution_freshness)
        self._readiness_timer.start()
        typography_controller().changed.connect(self._typography_changed)
        self.gateway.credentials_changed.connect(self._clear_position_desk_account)
        self._sync_control_heights()
        self._type_changed(self.current_order_type())
        self._size_mode_changed(str(self.size_mode.currentData()))
        self._sync_position_desk()

    def _build_ticket(self) -> None:
        """Build one frontend tree; order construction remains in prepare_order."""
        def label(text, role=TextRole.UI_CAPTION, name="ticketFieldCaption"):
            widget = QtWidgets.QLabel(text)
            widget.setObjectName(name)
            set_text_role(widget, role)
            return widget

        def combo(items, name="tradeTicketPrimaryCombo"):
            widget = CompactTradeComboBox()
            widget.setObjectName(name)
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Fixed)
            widget.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            widget.setMinimumContentsLength(4)
            set_text_role(widget, TextRole.UI_CONTROL)
            for title, value in items:
                widget.addItem(title, value)
            return widget

        def frame(name, margins=(0, 0, 0, 0), spacing=6):
            widget = QtWidgets.QFrame()
            widget.setObjectName(name)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Maximum)
            box = QtWidgets.QVBoxLayout(widget)
            box.setContentsMargins(*margins)
            box.setSpacing(spacing)
            return widget, box

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.ticket_scroll = TicketScrollArea()
        self.ticket_scroll.setObjectName("deskTicketScroll")
        self.ticket_scroll.setWidgetResizable(True)
        self.ticket_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.ticket_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ticket_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.ticket_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ticket_scroll.setMinimumSize(0, 24)
        self._ticket_body, self._desk_layout = frame("responsiveOrderTicket", (12, 10, 12, 12), 8)
        content = self._desk_layout
        content.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.ticket_scroll.setWidget(self._ticket_body)
        root.addWidget(self.ticket_scroll, 1)

        self._ticket_header = QtWidgets.QWidget()
        self._ticket_header.setObjectName("tradingInlineField")
        header = QtWidgets.QHBoxLayout(self._ticket_header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.leverage_field = QtWidgets.QWidget()
        self.leverage_field.setObjectName("tradingInlineField")
        leverage_row = QtWidgets.QHBoxLayout(self.leverage_field)
        leverage_row.setContentsMargins(0, 0, 0, 0)
        leverage_row.setSpacing(6)
        self.leverage = LeverageComboBox()
        self.leverage.setAccessibleName("Cross margin leverage")
        self.leverage.setToolTip("Uses cross margin.")
        set_text_role(self.leverage, TextRole.UI_CONTROL)
        leverage_row.addWidget(self.leverage)
        header.addWidget(self.leverage_field, 0, Qt.AlignmentFlag.AlignLeft)
        self.account_summary = ElidedLabel("— USDT")
        self.account_summary.setObjectName("tradeAccountSummary")
        self.account_summary.setAccessibleName("Available margin")
        set_text_role(self.account_summary, TextRole.UI_CAPTION)
        self.account_summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.account_summary, 1)
        self.quick_settings_button = QtWidgets.QToolButton()
        self.quick_settings_button.setObjectName("tradeSettingsMini")
        self.quick_settings_button.setFixedSize(28, 28)
        self.quick_settings_button.setIconSize(QtCore.QSize(16, 16))
        self.quick_settings_button.setAccessibleName("Trading settings")
        self.quick_settings_button.setToolTip("Trading settings")
        header.addWidget(self.quick_settings_button)
        content.addWidget(self._ticket_header, 0, Qt.AlignmentFlag.AlignLeft)

        self.reduce_only = QtWidgets.QCheckBox("Reduce only", self)
        self.reduce_only.setObjectName("ticketReduceOnly")
        self.reduce_only.setAccessibleName("Reduce only")
        self.reduce_only.setToolTip("Can reduce or close a position; never opens or reverses it.")
        set_text_role(self.reduce_only, TextRole.UI_CONTROL)
        self.position_side = combo(((side, side) for side in ("BOTH", "LONG", "SHORT")))
        self.position_side.setParent(self)
        self.position_side.hide()

        self.side_selector = QtWidgets.QWidget()
        self.side_selector.setObjectName("tradingInlineField")
        side = QtWidgets.QHBoxLayout(self.side_selector)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(8)
        self.buy_button = QtWidgets.QPushButton("Buy / Long")
        self.sell_button = QtWidgets.QPushButton("Sell / Short")
        self.buy_button.setObjectName("buySideButton")
        self.sell_button.setObjectName("sellSideButton")
        side_group = QtWidgets.QButtonGroup(self)
        side_group.setExclusive(True)
        for button in (self.buy_button, self.sell_button):
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setMinimumWidth(0)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            set_text_role(button, TextRole.UI_CONTROL)
            side_group.addButton(button)
            side.addWidget(button, 1)
        self.buy_button.setChecked(True)

        self.order_type_tabs = QtWidgets.QTabBar()
        self.order_type_tabs.setObjectName("ticketOrderTypes")
        self.order_type_tabs.setDrawBase(False)
        self.order_type_tabs.setExpanding(True)
        self.order_type_tabs.setUsesScrollButtons(True)
        self.order_type_tabs.setAccessibleName("Order type")
        set_text_role(self.order_type_tabs, TextRole.UI_CONTROL)
        for title in ("Market", "Limit", "Conditional"):
            self.order_type_tabs.addTab(title)
        self._conditional_order_type = "STOP"
        content.addWidget(self.order_type_tabs, 0, Qt.AlignmentFlag.AlignLeft)
        self._sections_body = QtWidgets.QWidget()
        self._sections_body.setObjectName("tradingInlineField")
        self._sections_body.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                          QtWidgets.QSizePolicy.Policy.Maximum)
        self._ticket_sections = QtWidgets.QGridLayout(self._sections_body)
        self._ticket_sections.setContentsMargins(0, 0, 0, 0)
        self._ticket_sections.setHorizontalSpacing(16)
        self._ticket_sections.setVerticalSpacing(12)
        content.addWidget(self._sections_body, 0, Qt.AlignmentFlag.AlignLeft)
        self.type_combo = combo(self.ORDER_TYPES)
        self.type_combo.setCurrentIndex(self.type_combo.findData("MARKET"))
        self.type_combo.setAccessibleName("Conditional order type")
        self.size_mode = combo(self.OPEN_SIZE_MODES)
        self.size_mode.setAccessibleName("Amount mode")
        self.quantity_edit = QtWidgets.QLineEdit()
        self.quantity_edit.setPlaceholderText("0")
        self.price_edit = TicketPriceEdit("Limit price")
        self.price_edit.setPlaceholderText("0.00")
        self.price_edit.setToolTip("Maximum buy price or minimum sell price.")
        self.trigger_edit = TicketPriceEdit("Trigger price")
        self.trigger_edit.setPlaceholderText("0.00")
        self.trigger_edit.setToolTip("Starts the order when the selected price source reaches this level.")
        self.activation_edit = TicketPriceEdit("Activation price")
        self.activation_edit.setPlaceholderText("Optional")
        self.activation_edit.setToolTip("Leave blank to activate at the current price.")
        for editor in (self.quantity_edit, self.price_edit, self.trigger_edit, self.activation_edit):
            set_text_role(editor, TextRole.TRADING_DESK_VALUE)
            editor.setMinimumWidth(0)
        self.quantity_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.callback_rate = TradingRateSpinBox()
        self.callback_rate.setRange(0.1, 10)
        self.callback_rate.setDecimals(1)
        self.callback_rate.setSingleStep(0.1)
        self.callback_rate.setValue(0.5)
        self.callback_rate.setSuffix(" %")
        self.callback_rate.setPrefix("Trail ")
        self.callback_rate.setToolTip("Price reversal that triggers the trailing stop.")
        self.callback_rate.setAccessibleName("Trailing callback rate")
        set_text_role(self.callback_rate, TextRole.TRADING_DESK_VALUE)
        self.working_type = combo((("Mark price", "MARK_PRICE"), ("Last price", "CONTRACT_PRICE")))
        self.working_type.setAccessibleName("Trigger source")
        self.working_type.setToolTip("Price used to trigger conditional orders.")

        def mark_field(edit):
            mark = QtWidgets.QToolButton()
            mark.setText("Mark")
            mark.setObjectName("tradeMarkButton")
            mark.setAccessibleName(f"Use mark price for {edit.accessibleName().lower()}")
            mark.setToolTip("Copy the current mark price")
            set_text_role(mark, TextRole.UI_CAPTION)
            return TicketInputGroup(edit, mark), mark

        price, self.price_mark_button = mark_field(self.price_edit)
        trigger, self.trigger_mark_button = mark_field(self.trigger_edit)
        activation, self.activation_mark_button = mark_field(self.activation_edit)
        amount = TicketInputGroup(self.quantity_edit, self.size_mode)
        self._order_card, self._order_card_layout = frame("tradingPanelCard")
        self._ticket_sections.addWidget(self._order_card, 0, 0)
        self._order_form = QtWidgets.QGridLayout()
        self._order_form.setHorizontalSpacing(8)
        self._order_form.setVerticalSpacing(8)
        for column in range(4):
            self._order_form.setColumnStretch(column, 1)
        self._order_card_layout.addLayout(self._order_form)
        self._field_rows = {}
        for name, control in (
            ("type", self.type_combo), ("source", self.working_type),
            ("price", price), ("trigger", trigger), ("activation", activation),
            ("callback", self.callback_rate), ("amount", amount),
        ):
            field, box = frame("ticketField", spacing=3)
            field.setParent(self._order_card)
            if name == "amount":
                amount_heading = QtWidgets.QHBoxLayout()
                amount_heading.setContentsMargins(0, 0, 0, 0)
                amount_heading.setSpacing(8)
                self.order_cost = ElidedLabel("")
                self.order_cost.setObjectName("ticketFieldCaption")
                self.order_cost.setAccessibleName("Estimated order cost")
                self.order_cost.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                set_text_role(self.order_cost, TextRole.UI_CAPTION)
                amount_heading.addWidget(self.order_cost, 1)
                box.addLayout(amount_heading)
            box.addWidget(control)
            self._field_rows[name] = field
        self._sizing_card, sizing = frame("tradingPanelCard", spacing=8)
        self._ticket_sections.addWidget(self._sizing_card, 1, 0)
        sizing.addWidget(self._field_rows["amount"])

        self.preset_section, preset = frame("ticketField", spacing=3)
        self.margin_bar = TicketAllocationControl()
        self.margin_bar.setObjectName("tradingMarginRow")
        self.size_presets = self.margin_bar.buttons
        self.allocation_slider = self.margin_bar.slider
        self.margin_bar.percentage_changed.connect(lambda value: self._apply_size_preset(value, focus_amount=False))
        for percent, button in zip((25, 50, 75, 100), self.size_presets):
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.clicked.connect(lambda _checked=False, value=percent: self._apply_size_preset(value))
        preset.addWidget(self.margin_bar)
        sizing.addWidget(self.preset_section)

        self.options_bar = QtWidgets.QFrame()
        self.options_bar.setObjectName("ticketExecutionOptions")
        options = QtWidgets.QGridLayout(self.options_bar)
        options.setContentsMargins(0, 6, 0, 0)
        options.setHorizontalSpacing(8)
        options.setVerticalSpacing(6)
        self._execution_options = QtWidgets.QWidget()
        self._execution_options.setObjectName("tradingInlineField")
        execution = QtWidgets.QHBoxLayout(self._execution_options)
        execution.setContentsMargins(0, 0, 0, 0)
        execution.setSpacing(6)
        options.addWidget(self.reduce_only, 0, 0, Qt.AlignmentFlag.AlignLeft)
        options.addWidget(self._execution_options, 0, 1, Qt.AlignmentFlag.AlignRight)
        self.time_in_force_field, tif = frame("ticketField", spacing=5)
        tif_help = _TIME_IN_FORCE_HINTS
        self.time_in_force = combo(((key, key) for key in tif_help), "timeInForceCycle")
        self.time_in_force.setAccessibleName("Time in force")
        for index, description in enumerate(tif_help.values()):
            self.time_in_force.setItemData(index, description, Qt.ItemDataRole.ToolTipRole)
        self.time_in_force.setToolTip(tif_help[self.time_in_force.currentText()])
        self.time_in_force.currentTextChanged.connect(lambda key: self.time_in_force.setToolTip(tif_help.get(key, "")))
        tif.addWidget(self.time_in_force)
        self.protection_field, protection = frame("ticketField", spacing=5)
        self.protection_button = QtWidgets.QPushButton("TP / SL")
        self.protection_button.setObjectName("protectionButton")
        self.protection_button.setProperty("active", False)
        self.protection_button.setToolTip("Set take-profit and stop-loss targets.")
        set_text_role(self.protection_button, TextRole.UI_CONTROL)
        protection.addWidget(self.protection_button)
        execution.addWidget(self.time_in_force_field)
        execution.addWidget(self.protection_field)
        sizing.addWidget(self.options_bar)

        self.feedback_box, feedback = frame("ticketFeedback", (10, 8, 10, 8), 5)
        self.account_problem_label = label("", name="ticketAccountProblem")
        self.account_problem_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_problem_label.setAccessibleName("Account or leverage issue")
        self.protection_summary = label("", name="tradeAccountSummary")
        self.protection_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.reconcile_button = QtWidgets.QPushButton("Reconcile unknown orders")
        self.reconcile_button.clicked.connect(self.gateway.reconcile_unknown_orders)
        for widget in (self.account_problem_label, self.protection_summary, self.reconcile_button):
            if isinstance(widget, QtWidgets.QLabel):
                widget.setWordWrap(True)
            widget.hide()
            feedback.addWidget(widget)
        sizing.addWidget(self.feedback_box)

        sizing.addWidget(self.side_selector)
        self._ticket_body.installEventFilter(self)
        self._order_card.installEventFilter(self)
        self.ticket_scroll.viewport().installEventFilter(self)
        self._layout_sync_timer = QTimer(self)
        self._layout_sync_timer.setSingleShot(True)
        self._layout_sync_timer.timeout.connect(self._sync_scroll_height)
        focus_order = (self.leverage, self.quick_settings_button, self.order_type_tabs,
                       self.type_combo, self.working_type,
                       self.trigger_edit, self.trigger_mark_button, self.price_edit,
                       self.price_mark_button, self.activation_edit, self.activation_mark_button,
                       self.callback_rate, self.quantity_edit, self.size_mode,
                       self.allocation_slider, *self.size_presets, self.reduce_only,
                       self.time_in_force, self.protection_button, self.reconcile_button,
                       self.buy_button, self.sell_button)
        for previous, following in zip(focus_order, focus_order[1:]):
            QtWidgets.QWidget.setTabOrder(previous, following)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.set_settings_icon(ui_palette(THEMES[DEFAULT_THEME_NAME], {})["muted"])

    def _clear_position_desk_account(self, _key):
        self._allocation_percent = None
        self._leverage_apply_timer.stop()
        self._position_cache = []
        self._desk_selected_key = None
        self.hedge_mode = None
        self._available_margin = None
        self._balance_asset = self.rules.margin_asset
        self._account_problem = self._leverage_problem = ""
        self._update_account_problem()
        self._last_confirmed_leverage = 0
        self.quantity_edit.clear()
        self._refresh_account_summary()
        self._refresh_reduce_positions()
        self._update_execution_state()
        self._update_order_summary()

    def _refresh_reduce_positions(self):
        rows = self._position_cache
        previous_key = self._desk_selected_key
        row = next((row for row in rows if _account_key(row) == previous_key), None)
        if row is None:
            direction = "SHORT" if self.buy_button.isChecked() else "LONG"
            row = next((row for row in rows if _position_direction(row) == direction), rows[0] if rows else None)
        self._desk_selected_key = _account_key(row) if row else None
        if row and self.reduce_only.isChecked():
            target = self.buy_button if _position_close_side(row) == "BUY" else self.sell_button
            target.setChecked(True)
            self._sync_auto_position_side()
            if self._desk_selected_key != previous_key:
                self.position_target_selected.emit(dict(row))
        self._sync_position_desk()

    def focus_position(self, payload, *, enter_reduce=False):
        if str(payload.get("symbol") or "") != self.symbol:
            return
        self._desk_selected_key = _account_key(payload)
        if enter_reduce:
            self.reduce_only.setChecked(True)
            self.type_combo.setCurrentIndex(self.type_combo.findData("MARKET"))
        self._refresh_reduce_positions()
        if enter_reduce:
            self._apply_size_preset(50)

    def _sync_position_desk(self, quantity=None):
        if not hasattr(self, "reduce_only") or self._desk_syncing:
            return
        self._desk_syncing = True
        try:
            reducing = self.reduce_only.isChecked()
            self.leverage_field.setVisible(not reducing)
            self.protection_field.setVisible(not reducing)
            self.allocation_slider.setAccessibleName(
                "Position close percentage" if reducing else "Available margin percentage")
            usable = not self.gateway.stopping
            self.buy_button.setEnabled(usable)
            self.sell_button.setEnabled(usable)
            self.feedback_box.setVisible(any(not widget.isHidden() for widget in
                                             (self.account_problem_label, self.protection_summary,
                                              self.reconcile_button)))
            mode = str(self.size_mode.currentData())
            raw = self.quantity_edit.text().strip()
            if quantity is None:
                quantity = getattr(self, "_desk_preview_quantity", "")
            allocation = 0.0
            if not reducing and self._allocation_percent is not None:
                allocation = self._allocation_percent
            elif mode == "POSITION %":
                allocation = safe_float(raw)
            elif reducing:
                amount = self._reduced_position_size()
                if amount > 0:
                    allocation = safe_float(quantity) / amount * 100
            elif quantity:
                leverage = self.gateway.current_leverage(self.symbol)
                try:
                    available = self.gateway.available_balance(self.rules.margin_asset)
                except ValueError:
                    available = 0.0
                reference = self._entry_reference(self.current_order_type())
                if leverage > 0 and available > 0 and reference > 0:
                    allocation = (safe_float(quantity) * reference
                                  / leverage / available * 100)
            for button, percent in zip(self.size_presets, (25, 50, 75, 100)):
                matched_preset = (
                    safe_float(raw) == percent if reducing and mode == "POSITION %"
                    else round(allocation) == percent if not reducing else False
                )
                button.setChecked(matched_preset)
            blocker = QtCore.QSignalBlocker(self.allocation_slider)
            self.allocation_slider.setValue(round(max(0, min(100, allocation))))
            del blocker
            # Allocation is an intent: it can be selected before the price or
            # account is ready. The order builder checks fresh inputs on submit.
            self.margin_bar.setEnabled(usable)
            intent = "position" if reducing else "margin"
            if intent != getattr(self, "_allocation_accessibility_intent", None):
                self._allocation_accessibility_intent = intent
                self.margin_bar.setAccessibleName(
                    "Position close percentage" if reducing else "Available margin allocation")
                self.margin_bar.setToolTip(
                    "Size this order to use the selected share of available margin at confirmed leverage."
                    if not reducing else "Close the selected share of the active position."
                )
                for percent, button in zip((25, 50, 75, 100), self.size_presets):
                    if reducing:
                        button.setAccessibleName(f"Close {percent}% of the selected position")
                        button.setToolTip(f"Size the reduction to close {percent}% of the selected position.")
                    else:
                        button.setAccessibleName(f"Allocate {percent}% of available margin")
                        button.setToolTip(
                            f"Size the order to use {percent}% of available margin at confirmed leverage."
                        )
        finally:
            self._desk_syncing = False

    def compact_required_height(self) -> int:
        """Natural ticket height, independent of the scroll viewport's height."""
        return self._desk_layout.sizeHint().height()

    def _publish_compact_minimum_height(self) -> None:
        if not self.compact:
            return
        required = self.compact_required_height()
        if required == self._published_compact_minimum_height:
            return
        self._published_compact_minimum_height = required
        self.minimum_content_height_changed.emit(required)

    def _sync_scroll_height(self) -> None:
        self._sync_field_widths()
        self._desk_layout.activate()
        self._reflow_order_fields(self._active_order_fields)
        self._publish_compact_minimum_height()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if (hasattr(self, "_active_order_fields") and watched is self.ticket_scroll.viewport()
                and event.type() == QtCore.QEvent.Type.Resize
                and event.size().width() != event.oldSize().width()):
            self._reflow_order_fields(self._active_order_fields)
            self._update_submit_text()
        if (hasattr(self, "_layout_sync_timer")
                and ((watched is self._ticket_body and event.type() == QtCore.QEvent.Type.LayoutRequest)
                     or (watched is self._order_card and event.type() == QtCore.QEvent.Type.Resize))):
            self._layout_sync_timer.start(0)
        return super().eventFilter(watched, event)

    def _sync_control_heights(self) -> None:
        controls = (self.quantity_edit, self.price_edit, self.trigger_edit,
                    self.activation_edit, self.type_combo, self.size_mode,
                    self.working_type, self.callback_rate, self.time_in_force,
                    self.leverage, self.protection_button, self.price_mark_button,
                    self.trigger_mark_button, self.activation_mark_button)
        height = max(32, max(widget.fontMetrics().height() for widget in controls) + 12)
        for widget in controls:
            grouped = isinstance(widget.parentWidget(), TicketInputGroup)
            widget.setFixedHeight(height - 2 if grouped else height)
        self.callback_rate.setFixedWidth(self.callback_rate.fontMetrics().horizontalAdvance("Trail 10.0 %") + 40)
        for button in (self.price_mark_button, self.trigger_mark_button, self.activation_mark_button):
            button.setFixedWidth(button.fontMetrics().horizontalAdvance("Mark") + 16)
        for button in (self.buy_button, self.sell_button):
            button.setFixedHeight(height + 4)
        self.margin_bar.sync_geometry()
        self._sync_field_widths()
        self._update_submit_text()
        if hasattr(self, "_active_order_fields"):
            self._order_form_signature = None
            self._reflow_order_fields(self._active_order_fields)
        self._layout_sync_timer.start(0)

    def _sync_field_widths(self) -> None:
        available = self._sizing_card.contentsRect().width()
        desired = self.size_mode.fontMetrics().horizontalAdvance(self.size_mode.currentText()) + 44
        self.size_mode.setFixedWidth(max(60, min(desired, max(60, available // 2))))
        self.leverage.setFixedWidth(self.leverage.fontMetrics().horizontalAdvance("125×") + 44)
        self.time_in_force.setFixedWidth(self.time_in_force.fontMetrics().horizontalAdvance("GTX") + 44)
        self.protection_button.setFixedWidth(self.protection_button.fontMetrics().horizontalAdvance(self.protection_button.text()) + 20)
        options = self.options_bar.layout()
        required = self.reduce_only.sizeHint().width() + self._execution_options.sizeHint().width() + 8
        wrap = required > available
        if wrap != getattr(self, "_options_wrapped", None):
            self._options_wrapped = wrap
            options.removeWidget(self.reduce_only)
            options.removeWidget(self._execution_options)
            options.addWidget(self.reduce_only, 0, 0, 1, 2 if wrap else 1,
                              Qt.AlignmentFlag.AlignLeft)
            options.addWidget(self._execution_options, 1 if wrap else 0, 0 if wrap else 1,
                              1, 2 if wrap else 1,
                              Qt.AlignmentFlag.AlignLeft if wrap else Qt.AlignmentFlag.AlignRight)
            options.setColumnStretch(0, 1)
            options.setColumnStretch(1, 0)

    def _typography_changed(self) -> None:
        self._sync_control_heights()
        if self.compact:
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)


    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if self.compact and event.type() in {
            QtCore.QEvent.Type.StyleChange,
            QtCore.QEvent.Type.FontChange,
        }:
            QTimer.singleShot(0, self, self._sync_control_heights)
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)

    def set_symbol(self, symbol: str, rules: SymbolRules) -> None:
        changed = symbol != self.symbol
        self.symbol = symbol
        self.rules = rules
        for editor in (self.price_edit, self.trigger_edit, self.activation_edit):
            editor.set_quote_asset(rules.quote_asset)
        if changed:
            self._allocation_percent = None
            # A delayed edit belongs to the old market. Never let its timer
            # apply that leverage to a different symbol after navigation.
            self._leverage_apply_timer.stop()
            self._leverage_problem = ""
            self._update_account_problem()
            self.mark_price = 0.0
            self._last_mark_mono = 0.0
            self._position_cache = []
            self._desk_selected_key = None
            self.quantity_edit.clear()
            self.price_edit.clear()
            self.trigger_edit.clear()
            self.activation_edit.clear()
            self.protection_plans = {"tp": [], "sl": []}
            self._protection_lifecycle = ""
            self._update_protection_label()
            confirmed = self.gateway.current_leverage(symbol)
            self._last_confirmed_leverage = confirmed
            blocker = QtCore.QSignalBlocker(self.leverage)
            self.leverage.setValue(confirmed or 5)
            del blocker
            # The gateway already owns the reconciled account for all markets.
            # Restore the new ticket's positions immediately; opening Reduce
            # must not depend on first visiting Account to request another read.
            available, balance_asset = self.gateway.account_balance(rules.margin_asset)
            mode = self.gateway.hedge_mode
            self.apply_account_snapshot({
                "positionMode": {"dualSidePosition": mode} if isinstance(mode, bool) else {},
                "account": {"availableBalance": available, "_balance_asset": balance_asset,
                            "positions": list(self.gateway.position_cache.values())},
            })
            self._sync_mark_controls()
        self._prepare_manual_trading()
        self._refresh_reduce_positions()
        self._size_mode_changed(str(self.size_mode.currentData()))
        self._update_execution_state()
        self._update_order_summary()

    def _prepare_manual_trading(self, *_args) -> None:
        if self.gateway.has_credentials() and not self.gateway.stopping:
            if not self.gateway.account_loaded:
                self.gateway.refresh_account(self.symbol)
            else:
                self.gateway.ensure_cross(self.symbol)

    def set_mark_price(self, price: float) -> None:
        self.mark_price = price
        if price > 0:
            self._last_mark_mono = time.monotonic()
        self._readiness_fresh_state = self._mark_is_fresh()
        for row in self._position_cache:
            _update_position_mark(row, price)
        self._refresh_account_summary()
        self._sync_mark_controls()
        self._update_execution_state()
        self._update_order_summary()


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

    def _leverage_edit_pending(self) -> bool:
        return (self._leverage_apply_timer.isActive()
                or self.leverage.is_editing()
                or self.symbol in self.gateway.queued_leverage)

    def _leverage_changing(self, symbol: str, leverage: int) -> None:
        if symbol == self.symbol:
            self._leverage_problem = ""
            self._update_account_problem()
            self._update_execution_state()

    def _leverage_changed(
        self, symbol: str, leverage: int, succeeded: bool, message: str
    ) -> None:
        if symbol != self.symbol:
            return
        if succeeded:
            self._leverage_problem = ""
            self._update_account_problem()
            self._last_confirmed_leverage = leverage
            if not self._leverage_edit_pending():
                blocker = QtCore.QSignalBlocker(self.leverage)
                self.leverage.setValue(leverage)
                del blocker
            self._update_execution_state()
        elif message:
            self._leverage_problem = f"Leverage change failed: {message}"
            QtWidgets.QMessageBox.warning(self, "Leverage unavailable", self._leverage_problem)
            self._update_account_problem()
            confirmed = self.gateway.current_leverage(symbol) or self._last_confirmed_leverage
            if not self._leverage_edit_pending():
                blocker = QtCore.QSignalBlocker(self.leverage)
                self.leverage.setValue(confirmed)
                del blocker
            self._gateway_state_text = message
            self._update_execution_state()
        self._update_order_summary()

    def _copy_mark_to(self, target: QtWidgets.QLineEdit) -> None:
        if not self._mark_is_fresh():
            self._show_order_error(
                "MARK PRICE UNAVAILABLE" if self.mark_price <= 0 else "MARK PRICE STALE"
            )
            return
        try:
            text = quantize_step(str(self.mark_price), self.rules.tick_size, offset=str(self.rules.min_price))
        except ValueError:
            text = format_price(self.mark_price).replace(",", "")
        target.setText(text)
        self._update_order_summary()

    def current_time_in_force(self) -> str:
        value = self.time_in_force.currentData()
        return str(value or self.time_in_force.currentText()).split(" ·", 1)[0]


    def set_settings_icon(self, color: str) -> None:
        self.quick_settings_button.setIcon(
            line_icon("gear", color, max(1.0, self.devicePixelRatioF()))
        )

    def apply_theme(self, theme: dict[str, str]) -> None:
        palette = ui_palette(theme)
        self.setStyleSheet(_trading_stylesheet(palette))
        self.set_settings_icon(palette['muted'])

    def _select_order_type_tab(self, index: int) -> None:
        value = ("MARKET", "LIMIT", self._conditional_order_type)[max(0, min(2, index))]
        self.type_combo.setCurrentIndex(self.type_combo.findData(value))

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
        self.time_in_force_field.setVisible(limit_price)
        if conditional:
            self._conditional_order_type = order_type
        blocker = QtCore.QSignalBlocker(self.order_type_tabs)
        self.order_type_tabs.setCurrentIndex(2 if conditional else 0 if order_type == "MARKET" else 1)
        del blocker
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
        self._update_submit_text()
        self._update_order_summary()
        self._update_execution_state()

    def _reflow_order_fields(self, active_fields: list[str]) -> None:
        self._active_order_fields = list(active_fields)
        active = set(active_fields)
        active.discard("amount")  # Sizing and execution stay together in their own section.
        form = self._order_form
        if self.current_order_type() not in CONDITIONAL_ORDER_TYPES:
            active.discard("type")
        selector_width = sum(control.fontMetrics().horizontalAdvance(control.currentText()) + 44
                             for control in (self.type_combo, self.working_type)) + form.horizontalSpacing()
        margins = self._desk_layout.contentsMargins()
        available = self.ticket_scroll.viewport().width() - margins.left() - margins.right()
        detail_fields = active.intersection(self._field_rows)
        column_minimum = max(300, selector_width)
        section_maximum = max(420, column_minimum)
        maximum_width = section_maximum * 2 + self._ticket_sections.horizontalSpacing() if detail_fields else section_maximum
        # Keep wide dock controls at useful widths instead of stretching across the screen.
        self._sections_body.setFixedWidth(max(0, min(available, maximum_width)))
        header_width = self._sections_body.width()
        self._ticket_header.setFixedWidth(header_width)
        self.order_type_tabs.setFixedWidth(header_width)
        columns = bool(detail_fields) and available >= column_minimum * 2 + self._ticket_sections.horizontalSpacing()
        detail_width = (self._sections_body.width() - self._ticket_sections.horizontalSpacing()) // 2 if columns else self._sections_body.width()
        selectors_share_row = selector_width <= detail_width
        signature = (frozenset(active), selectors_share_row, columns)
        if signature == getattr(self, "_order_form_signature", None):
            self._sync_field_widths()
            return
        self._order_form_signature = signature
        for name, container in self._field_rows.items():
            if name == "amount":
                continue
            form.removeWidget(container)
        for name, container in self._field_rows.items():
            container.setVisible(name == "amount" or name in active)
        self.time_in_force.setVisible("tif" in active)
        self.time_in_force_field.setVisible("tif" in active)
        row = 0

        def add_row(names):
            nonlocal row
            names = [name for name in names if name in active]
            if not names:
                return
            span = 4 // len(names)
            for column, name in enumerate(names):
                self._field_rows[name].show()
                form.addWidget(self._field_rows[name], row, column * span, 1, span)
            row += 1

        # Preserve exchange order-entry sequence; only the conditional selectors
        # share a row when they fit.
        if selectors_share_row:
            add_row(("type", "source"))
        else:
            add_row(("type",))
            add_row(("source",))
        for name in ("trigger", "price", "activation", "callback"):
            add_row((name,))
        form.invalidate()
        form.activate()
        self._order_card_layout.invalidate()
        self._order_card_layout.activate()
        self._order_card.setVisible(row > 0)
        self._order_card.setMinimumHeight(0)
        self._order_card.updateGeometry()
        sections = self._ticket_sections
        sections.removeWidget(self._order_card)
        sections.removeWidget(self._sizing_card)
        sections.setColumnStretch(0, 1)
        sections.setColumnStretch(1, 1 if columns else 0)
        if row > 0:
            sections.addWidget(self._order_card, 0, 0, Qt.AlignmentFlag.AlignTop)
        sections.addWidget(self._sizing_card, 0 if columns or row == 0 else 1,
                           1 if columns else 0, Qt.AlignmentFlag.AlignTop)
        sections.invalidate()
        sections.activate()
        self._sync_field_widths()
        if self.compact:
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)

    def _intent_changed(self, reducing: bool) -> None:
        had_allocation = self._allocation_percent is not None
        self._allocation_percent = None
        if had_allocation:
            self.quantity_edit.clear()
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


        self._refresh_reduce_positions()
        self.protection_button.setVisible(not reducing)
        self.protection_button.setEnabled(not reducing)
        self._type_changed(self.current_order_type())
        self._size_mode_changed(str(self.size_mode.currentData()))
        self._sync_auto_position_side()
        self._update_submit_text()
        self._update_order_summary()
        self._update_execution_state()

    def _update_submit_text(self) -> None:
        """Keep the two execution actions concise and accessible."""
        kind = dict((value, title.lower()) for title, value in self.ORDER_TYPES)[self.current_order_type()]
        reducing = self.reduce_only.isChecked()
        for button, text, side in ((self.buy_button, "Close short" if reducing else "Buy / Long", "buy"),
                                   (self.sell_button, "Close long" if reducing else "Sell / Short", "sell")):
            _set_text_if_changed(button, text)
            button.setAccessibleName(f"Submit {kind} {side} {'reduction' if reducing else 'order'} for {self.symbol}")

    def _submit_from_side(self, buying: bool) -> None:
        """Submit either side through the canonical order builder."""
        target = self.buy_button if buying else self.sell_button
        if not target.isChecked():
            target.setChecked(True)
        self._sync_auto_position_side()
        self.prepare_order()

    def _apply_size_preset(self, percent: int, *, focus_amount: bool = True) -> None:
        """Retain allocation intent; derive its quantity from fresh trading inputs."""
        percent = max(0, min(100, int(percent)))
        target_mode = "POSITION %" if self.reduce_only.isChecked() else "CONTRACTS"
        index = self.size_mode.findData(target_mode)
        if index >= 0:
            self.size_mode.setCurrentIndex(index)
        if self.reduce_only.isChecked():
            self._allocation_percent = None
            self.quantity_edit.setText(str(percent))
        else:
            self._allocation_percent = percent
            self._update_order_summary()
        if focus_amount:
            editor = self.quantity_edit
            editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
            editor.selectAll()

    def _clear_margin_allocation(self, *_args: object) -> None:
        """An explicit amount or unit selection takes over from the slider."""
        if self._allocation_percent is not None:
            self._allocation_percent = None
            self._update_order_summary()

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
        multi = exchange_bool((snapshot.get('accountConfig') or {}).get('multiAssetsMargin', account.get('multiAssetsMargin')))
        self._balance_asset = str(account.get('_balance_asset') or ('USD' if multi is True else self.rules.margin_asset))
        if "assets" in account and multi is not True:
            margin_row = next(
                (
                    row
                    for row in account.get("assets", [])
                    if str(row.get("asset") or "") == self.rules.margin_asset
                ),
                {},
            )
            available = safe_float(margin_row.get("availableBalance"), -1.0)
        else:
            available = safe_float(account.get("availableBalance"), -1.0)
        self._available_margin = available if available >= 0 else None
        self._position_cache = []
        active_leverage = 0
        for row in account.get("positions", []):
            if str(row.get("symbol")) != self.symbol:
                continue
            if abs(safe_float(row.get("positionAmt"))) > 0:
                self._position_cache.append(dict(row))
            active_leverage = max(active_leverage, int(safe_float(row.get("leverage"))))
        active_leverage = self.gateway.current_leverage(self.symbol) or active_leverage
        if active_leverage:
            self._last_confirmed_leverage = active_leverage
            # Refreshes may complete between a user selection and the debounce
            # timer, or while Binance is applying it. Keep the requested value.
            if not self._leverage_edit_pending() and self.symbol not in self.gateway.cross_pending:
                blocker = QtCore.QSignalBlocker(self.leverage)
                self.leverage.setValue(active_leverage)
                del blocker
        self._refresh_account_summary()
        self._refresh_reduce_positions()
        self._update_execution_state()
        self._update_order_summary()

    def _refresh_account_summary(self) -> None:
        margin_asset = self._balance_asset
        amount = f"{self._available_margin:,.2f}" if self._available_margin is not None else "—"
        _set_text_if_changed(
            self.account_summary,
            f"Avail. {amount} {margin_asset}",
        )

    def _account_status_changed(self, message: str) -> None:
        self._account_problem = message
        self._update_account_problem()

    def _update_account_problem(self) -> None:
        message = "\n".join(part for part in (self._account_problem, self._leverage_problem) if part)
        _set_text_if_changed(self.account_problem_label, message)
        self.account_problem_label.setVisible(bool(message))
        self._sync_position_desk()

    def _size_mode_changed(self, mode: str) -> None:
        previous = getattr(self, "_quantity_unit", mode)
        self._quantity_unit = mode
        if previous != mode:
            self._allocation_percent = None
            self.quantity_edit.clear()
        base_asset = self.symbol.removesuffix(self.rules.quote_asset)
        label, placeholder = {
            "CONTRACTS": (f"Amount · {base_asset}", "0"),
            "QUOTE NOTIONAL": (f"Value · {self.rules.quote_asset}", "0"),
            "RISK %": ("Account risk · %", "1–100"),
            "POSITION %": ("Position size · %", "1–100"),
        }.get(mode, ("Amount", "0.00"))
        self.quantity_edit.setAccessibleName(label)
        units = {
            "CONTRACTS": base_asset,
            "QUOTE NOTIONAL": self.rules.quote_asset,
            "RISK %": "Risk %", "POSITION %": "Position %",
        }
        for index in range(self.size_mode.count()):
            self.size_mode.setItemText(index, units.get(self.size_mode.itemData(index), "Qty"))
        self._sync_field_widths()
        self.quantity_edit.setPlaceholderText(placeholder)
        self._update_order_summary()
        if self.compact:
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)

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
        _set_text_if_changed(self.protection_summary, self._protection_lifecycle)
        self.protection_summary.setVisible(bool(self._protection_lifecycle))
        self._sync_field_widths()
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
            QTimer.singleShot(delay, self, clear_if_current)

    def set_protection_lifecycle(self, text: str) -> None:
        self._protection_lifecycle = str(text or "").upper()
        self._update_protection_label()

    def _show_order_error(self, text: str) -> None:
        # Validation is actionable only after a user asks to place/copy an order.
        # Preview updates leave unavailable estimates blank instead of adding warnings.
        if text:
            QtWidgets.QMessageBox.warning(self, "Order unavailable", str(text))

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

    def _execution_request_changed(self, *_args: object) -> None:
        self._update_execution_state()

    def _update_execution_state(self) -> None:
        # An unknown outcome needs a recovery action; routine readiness belongs
        # to the gateway and is checked again by the canonical order builder.
        _used, _capacity, unknown = self.gateway.placement_status()
        uncertain = bool(unknown) or self._submission_state == "OUTCOME UNKNOWN"
        reconcile_changed = self.reconcile_button.isHidden() == uncertain
        self.reconcile_button.setVisible(uncertain)
        self.reconcile_button.setEnabled(self.gateway.has_credentials() and not self.gateway.stopping)
        if self.compact and reconcile_changed:
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)
        self._sync_position_desk()

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
        order_type = self.current_order_type()
        quantity = ""
        try:
            if order_type in {"LIMIT", "STOP"} and self.price_edit.text().strip():
                self._validated_price(self.price_edit.text().replace(",", "").strip(), "Order price")
            if order_type in CONDITIONAL_ORDER_TYPES and order_type != "TRAILING_STOP_MARKET" and self.trigger_edit.text().strip():
                self._validated_price(self.trigger_edit.text().replace(",", "").strip(), "Trigger price")
            if order_type == "TRAILING_STOP_MARKET" and self.activation_edit.text().strip():
                self._validated_price(self.activation_edit.text().replace(",", "").strip(), "Activation price")
            if self._allocation_percent is not None or self.quantity_edit.text().strip():
                quantity = self._quantity(order_type)
        except (ValueError, TypeError):
            pass
        if self._allocation_percent is not None and self.quantity_edit.text() != quantity:
            # Show the derived base quantity while the slider retains the
            # percentage. Internal preview refreshes must not become manual edits.
            selected = self.quantity_edit.selectedText()
            selection_start = self.quantity_edit.selectionStart()
            cursor = self.quantity_edit.cursorPosition()
            all_selected = bool(selected) and len(selected) == len(self.quantity_edit.text())
            blocker = QtCore.QSignalBlocker(self.quantity_edit)
            self.quantity_edit.setText(quantity)
            if all_selected:
                self.quantity_edit.selectAll()
            elif selection_start >= 0:
                self.quantity_edit.setSelection(selection_start, len(selected))
            else:
                self.quantity_edit.setCursorPosition(min(cursor, len(quantity)))
            del blocker
        self._update_ticket_estimates(quantity, order_type)

    def _update_ticket_estimates(self, quantity: str, order_type: str) -> None:
        self._desk_preview_quantity = quantity
        reference = self._entry_reference(order_type)
        notional = safe_float(quantity) * reference
        leverage = self.gateway.current_leverage(self.symbol)
        cost = (f"Cost ≈ {human_number(notional / leverage)} {self.rules.margin_asset}"
                if quantity and reference > 0 and leverage > 0 and not self.reduce_only.isChecked()
                else "" if self.reduce_only.isChecked() else "Cost —")
        _set_text_if_changed(self.order_cost, cost)
        self.order_cost.setToolTip("Estimated initial margin, excluding fees." if cost else "")
        self._sync_position_desk(quantity)

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
            text = quantize_step(str(float(price)), self.rules.tick_size, offset=str(self.rules.min_price))
        except (ValueError, TypeError):
            text = format_price(price).replace(",", "")
        target.setText(text)
        if order_type == "MARKET":
            # A selected book price is a limit draft. Expose that price instead
            # of silently filling the hidden Market ticket's price field.
            self.type_combo.setCurrentIndex(self.type_combo.findData("LIMIT"))
        if target.hasFocus():
            target.selectAll()
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
        allocating = self._allocation_percent is not None and not self.reduce_only.isChecked()
        raw = (self._allocation_percent if allocating
               else safe_float(self.quantity_edit.text().replace(",", "")))
        if raw <= 0:
            raise ValueError("Enter a positive order size.")
        reference = self._entry_reference(order_type)
        mode = str(self.size_mode.currentData())
        if (allocating or mode in {"RISK %", "POSITION %"}) and raw > 100:
            raise ValueError("Percentage sizing cannot exceed 100%.")
        if allocating:
            balance = self.gateway.available_balance(self.rules.margin_asset)
            if balance <= 0:
                raise ValueError("Load account balances before using margin allocation.")
            if reference <= 0:
                raise ValueError("A current or limit price is required for margin allocation.")
            leverage = self.gateway.current_leverage(self.symbol)
            if leverage <= 0:
                raise ValueError("Load the confirmed Binance leverage before using margin allocation.")
            quantity_value = balance * raw / 100.0 * leverage / reference
        elif mode == "CONTRACTS":
            quantity_value = raw
        elif mode == "POSITION %":
            direction = "SHORT" if self.buy_button.isChecked() else "LONG"
            available_position = self._reduced_position_size()
            if available_position <= 0:
                raise ValueError(f"There is no {direction.lower()} position to reduce.")
            quantity_value = Decimal(str(available_position)) * Decimal(str(raw)) / 100
        elif mode == "QUOTE NOTIONAL":
            if reference <= 0:
                raise ValueError("A current or limit price is required for notional sizing.")
            quantity_value = raw / reference
        else:
            balance = self.gateway.available_balance(self.rules.margin_asset)
            if balance <= 0:
                raise ValueError("Load account balances before using balance- or risk-based sizing.")
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
        minimum_qty = self.rules.min_market_qty if market_quantity else self.rules.min_qty
        quantity = (
            validate_step(
                self.quantity_edit.text().replace(",", "").strip(),
                step,
                "Quantity",
                offset=str(minimum_qty),
            )
            if mode == "CONTRACTS" and not allocating
            else quantize_step(str(quantity_value), step, offset=str(minimum_qty))
        )
        value = safe_float(quantity)
        if value <= 0:
            raise ValueError("Resulting quantity is below the exchange lot size.")
        maximum_qty = self.rules.max_market_qty if market_quantity else self.rules.max_qty
        if not (minimum_qty <= value <= maximum_qty):
            raise ValueError(
                f"Resulting quantity must be between {minimum_qty:g} and {maximum_qty:g}."
            )
        if (not self.reduce_only.isChecked() and self.rules.min_notional
                and reference > 0 and value * reference < self.rules.min_notional):
            raise ValueError(
                f"Order value must be at least {self.rules.min_notional:g} {self.rules.quote_asset}."
            )
        return quantity

    def _validated_price(self, value: str, label: str, *, limit_side: str = '') -> str:
        price = validate_step(value, self.rules.tick_size, label, offset=str(self.rules.min_price))
        number = safe_float(price)
        if number < self.rules.min_price or (self.rules.max_price > 0 and number > self.rules.max_price):
            raise ValueError(
                f"{label} must be between {format_price(self.rules.min_price)} and "
                f"{format_price(self.rules.max_price)}."
            )
        if limit_side and self._mark_is_fresh():
            if limit_side == 'BUY' and self.rules.price_multiplier_up and number > self.mark_price * self.rules.price_multiplier_up:
                raise ValueError(f"{label} is above Binance's current mark-price band.")
            if limit_side == 'SELL' and self.rules.price_multiplier_down and number < self.mark_price * self.rules.price_multiplier_down:
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
                    self.price_edit.text().replace(",", "").strip(), "Order price", limit_side=side
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
                    if (str(order['workingType']) == 'MARK_PRICE' and self._mark_is_fresh()
                            and order_type in {"STOP", "STOP_MARKET"}):
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
                    if activation > 0 and str(order['workingType']) == 'MARK_PRICE' and self._mark_is_fresh():
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
            self._show_order_error(str(exc))
            self._update_execution_state()
            return
        request = {
            "order": order,
            "protections": active_protections,
            "rules": self.rules,
            "position_intent": "REDUCE" if reducing else "OPEN",
        }
        if not reducing and (self._allocation_percent is not None
                             or self.size_mode.currentData() == 'RISK %'):
            request['collateral_asset'] = self.rules.margin_asset
            request['collateral_required'] = (safe_float(order['quantity'])
                                              * self._entry_reference(order_type) / confirmed_leverage)
        self.order_requested.emit(request)


def _validated_limit_price(value: str, rules: SymbolRules, label: str = "Limit price") -> str:
    price = validate_step(value, rules.tick_size, label, offset=str(rules.min_price))
    number = Decimal(price)
    if number < Decimal(str(rules.min_price)) or (
        rules.max_price > 0 and number > Decimal(str(rules.max_price))
    ):
        raise ValueError(f"{label} must be between {rules.min_price:g} and {rules.max_price:g}.")
    return price


def _validated_limit_quantity(value: str, rules: SymbolRules, label: str = "Quantity") -> str:
    quantity = validate_step(value, rules.lot_step, label, offset=str(rules.min_qty))
    if not Decimal(str(rules.min_qty)) <= Decimal(quantity) <= Decimal(str(rules.max_qty)):
        raise ValueError(f"{label} must be between {rules.min_qty:g} and {rules.max_qty:g}.")
    return quantity


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
        if not any(price.text().strip() or quantity.text().strip() for _side, price, quantity in self.rows):
            self._set_validation("")
            return
        try:
            self.orders()
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
            validated_price = _validated_limit_price(price_text, self.rules, f"Row {index} limit price")
            validated_quantity = _validated_limit_quantity(quantity_text, self.rules, f"Row {index} quantity")
            if self.rules.min_notional and Decimal(validated_price) * Decimal(validated_quantity) < Decimal(str(self.rules.min_notional)):
                raise ValueError(
                    f"Row {index} order value must be at least {self.rules.min_notional:g} {self.rules.quote_asset}."
                )
            item = {
                "symbol": self.symbol,
                "side": side.currentText(),
                "type": "LIMIT",
                "price": validated_price,
                "quantity": validated_quantity,
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
        original = order.get("origQty") or order.get("quantity") or "0"
        executed = order.get("executedQty") or order.get("cumQty") or "0"
        try:
            remaining = max(Decimal("0"), Decimal(str(original)) - Decimal(str(executed)))
        except (InvalidOperation, TypeError, ValueError):
            remaining = None
        self.setWindowTitle(f"Modify working order · {symbol}")
        self.setMinimumWidth(440)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)
        context = QtWidgets.QLabel(
            f"{symbol} · {side} · {order_type} · REMAINING {_quantity_text(remaining)}"
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
            f"Total size includes {_quantity_text(executed)} already filled. "
            f"Quantity step: {_exchange_step_text(rules.lot_step)}."
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
        quantity = _validated_limit_quantity(
            self.quantity.text().replace(",", "").strip(), self.rules, "New total quantity"
        )
        try:
            executed = Decimal(str(self.order.get("executedQty") or self.order.get("cumQty") or "0"))
        except InvalidOperation as exc:
            raise ValueError("Filled quantity is unavailable; refresh the order before editing.") from exc
        if not executed.is_finite() or executed < 0:
            raise ValueError("Filled quantity is unavailable; refresh the order before editing.")
        if Decimal(quantity) <= executed:
            raise ValueError("New total quantity must exceed the already filled amount. Use Cancel to remove the remainder.")
        changes = {
            "symbol": str(self.order.get("symbol") or ""),
            "orderId": self.order.get("orderId"),
            "side": self.order.get("side"),
            "quantity": quantity,
            "_minimumExecutedQty": str(executed),
            "price": _validated_limit_price(
                self.price.text().replace(",", "").strip(), self.rules, "Price"
            ),
        }
        if self.order.get('reduceOnly') is True or str(self.order.get('reduceOnly')).lower() == 'true':
            changes['reduceOnly'] = True
        return changes

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


def _quantity_text(value: Any) -> str:
    """Display small contract sizes and fees without rounding them to zero."""
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if not number.is_finite():
        return "—"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pnl_state(value: float) -> str:
    value = safe_float(value)
    return "positive" if value > 1e-12 else "negative" if value < -1e-12 else "flat"


def _set_text_if_changed(widget: QtWidgets.QLabel | QtWidgets.QAbstractButton, text: str) -> bool:
    text = str(text)
    current = widget.full_text() if isinstance(widget, ElidedLabel) else widget.text()
    if current == text:
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
        *,
        rules: SymbolRules | None = None,
    ):
        super().__init__(parent)
        self.position = dict(position)
        self.rules = rules
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
            price = str(position.get("markPrice"))
            if rules is not None:
                try:
                    price = quantize_step(price, rules.tick_size, offset=str(rules.min_price))
                except ValueError:
                    price = ""
            self.price_edit.setText(price)
        self.price_edit.selectAll()
        layout.addWidget(self.price_edit)
        self.validation = QtWidgets.QLabel("")
        self.validation.setObjectName("subtleLabel")
        self.validation.setWordWrap(True)
        self.validation.hide()
        layout.addWidget(self.validation)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        confirm = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        confirm.setText("PLACE CLOSE LIMIT")
        confirm.setObjectName("dangerButton")
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.price_edit.textChanged.connect(self._validate_live)

    def price_text(self) -> str:
        return self.price_edit.text().replace(",", "").strip()

    def validated_price(self) -> str:
        if self.rules is None:
            return validate_step(self.price_text(), "0", "Limit price")
        price = _validated_limit_price(self.price_text(), self.rules)
        mark = safe_float(self.position.get("markPrice"))
        if mark > 0:
            number = Decimal(price)
            close_side = _position_close_side(self.position)
            if close_side == "BUY" and self.rules.price_multiplier_up and number > Decimal(str(mark)) * Decimal(str(self.rules.price_multiplier_up)):
                raise ValueError("Limit price is above Binance's current price band.")
            if close_side == "SELL" and self.rules.price_multiplier_down and number < Decimal(str(mark)) * Decimal(str(self.rules.price_multiplier_down)):
                raise ValueError("Limit price is below Binance's current price band.")
        return price

    def _validate_live(self, _text: str = "") -> None:
        try:
            self.validated_price()
        except ValueError as exc:
            self.validation.setText(str(exc))
            self.validation.show()
            return
        self.validation.clear()
        self.validation.hide()

    def _accept_validated(self) -> None:
        self._validate_live()
        if not self.validation.text():
            self.accept()


class _FillAccountModel(QtCore.QAbstractListModel):
    """Keep exchange fills as data; format only rows requested by the view."""

    CardRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent):
        super().__init__(parent)
        self.rows = []
        self._row_by_key = {}
        self._display_cache = OrderedDict()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def index_for_key(self, key):
        row = self._row_by_key.get(key)
        return self.index(row, 0) if row is not None else QtCore.QModelIndex()

    def set_rows(self, rows):
        if rows == self.rows:
            return False
        # Own each snapshot; callers may reuse/update their exchange payloads.
        rows = [dict(row) for row in rows]
        keys = [_account_key(row) for row in rows]
        old_keys = [_account_key(row) for row in self.rows]
        if keys == old_keys:
            changed = [i for i, (old, new) in enumerate(zip(self.rows, rows)) if old != new]
            self.rows = rows
            if changed:
                self.dataChanged.emit(self.index(changed[0], 0), self.index(changed[-1], 0))
        else:
            self.beginResetModel()
            self.rows = rows
            self._row_by_key = {key: i for i, key in enumerate(keys)}
            self.endResetModel()
        return True

    def _card(self, row):
        payload = self.rows[row]
        key = _account_key(payload)
        cached = self._display_cache.get(key)
        if cached is not None and cached[0] == payload:
            self._display_cache.move_to_end(key)
            return cached[1]
        timestamp = safe_float(payload.get("time")) / 1000.0
        try:
            stamp = format_local_time(timestamp, "%d %b %Y · %H:%M:%S") if timestamp > 0 else "—"
        except (OverflowError, OSError, ValueError):
            stamp = "—"
        pnl = safe_float(payload.get("realizedPnl"))
        asset = str(payload.get("commissionAsset") or "").strip()
        card = (
            str(payload.get("symbol") or "—"), str(payload.get("side") or "—").upper(),
            _quantity_text(payload.get("price")), _quantity_text(payload.get("qty")),
            _signed_money(pnl), _quantity_text(payload.get("commission")),
            f"FEE · {asset}" if asset else "FEE", stamp, pnl,
        )
        self._display_cache[key] = (payload, card)
        self._display_cache.move_to_end(key)
        while len(self._display_cache) > 128:
            self._display_cache.popitem(last=False)
        return card

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return dict(self.rows[index.row()])
        if role not in (self.CardRole, Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.AccessibleTextRole):
            return None
        card = self._card(index.row())
        if role == self.CardRole:
            return card
        symbol, side, price, qty, pnl, fee, fee_caption, stamp, _ = card
        return f"{symbol} · {side}\nPRICE {price}\nSIZE {qty}\nREALIZED PNL {pnl}\n{fee_caption}: {fee}\n{stamp}"


class _FillAccountDelegate(QtWidgets.QStyledItemDelegate):
    """Draw the existing label grid without a widget/layout tree per fill."""

    def __init__(self, parent):
        super().__init__(parent)
        self.theme = ui_palette(THEMES[DEFAULT_THEME_NAME])
        self.refresh_typography()

    def refresh_typography(self):
        self.fonts = {name: typography_font(role) for name, role in (
            ("symbol", TextRole.INSTRUMENT_SYMBOL), ("side", TextRole.UI_LABEL),
            ("caption", TextRole.UI_CAPTION), ("value", TextRole.MARKET_VALUE),
        )}
        self.metrics = {name: QtGui.QFontMetrics(font) for name, font in self.fonts.items()}
        self.header_height = max(self.metrics["symbol"].height(), self.metrics["side"].height())
        self.caption_height = self.metrics["caption"].height()
        self.value_height = self.metrics["value"].height()
        self.row_height = 40 + self.header_height + 3*self.caption_height + 2*self.value_height

    def sizeHint(self, option, index):
        return QtCore.QSize(0, self.row_height)

    def paint(self, painter, option, index):
        card = index.data(_FillAccountModel.CardRole)
        if card is None:
            return
        symbol, side, price, qty, pnl_text, fee, fee_caption, stamp, pnl = card
        theme = self.theme
        painter.save()
        painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
        rect = option.rect.adjusted(10, 9, -10, -9)
        width = max(0, rect.width())

        def text(value, font, color, box, right=False):
            if box.width() <= 0:
                return
            painter.setFont(self.fonts[font])
            painter.setPen(QtGui.QColor(color))
            value = self.metrics[font].elidedText(value, Qt.TextElideMode.ElideRight, box.width())
            align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
            painter.drawText(box, align | Qt.AlignmentFlag.AlignVCenter, value)

        side_width = min(self.metrics["side"].horizontalAdvance(side) + 2, width // 2)
        y = rect.top()
        text(symbol, "symbol", theme["text"], QtCore.QRect(rect.left(), y, max(0,width-side_width-8), self.header_height))
        text(side, "side", theme["green"] if side == "BUY" else theme["red"],
             QtCore.QRect(rect.right()-side_width+1, y, side_width, self.header_height), True)
        y += self.header_height + 6
        column_width = max(0, (width-8)//2)
        pnl_color = theme["green"] if pnl > 0 else theme["red"] if pnl < 0 else theme["text"]
        fields = (("PRICE", price, theme["text"]), ("SIZE", qty, theme["text"]),
                  ("REALIZED PNL", pnl_text, pnl_color), (fee_caption, fee, theme["text"]))
        for i, (caption, value, color) in enumerate(fields):
            row, column = divmod(i, 2)
            left = rect.left() + column*(column_width+8)
            top = y + row*(self.caption_height+self.value_height+4)
            text(caption, "caption", theme["muted"], QtCore.QRect(left, top, column_width, self.caption_height))
            text(value, "value", color, QtCore.QRect(left, top+self.caption_height+2, column_width, self.value_height), True)
        y += 2*self.caption_height + 2*self.value_height + 12
        text(stamp, "caption", theme["muted"], QtCore.QRect(rect.left(), y, width, self.caption_height))
        painter.setPen(QtGui.QColor(theme["border"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()


class FillAccountView(QtWidgets.QListView):
    """Virtualized fill cards with stable selection and scroll anchoring."""

    def __init__(self):
        super().__init__()
        self.setObjectName("tradingFillList")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setWrapping(False)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self._fill_model = _FillAccountModel(self)
        self.setModel(self._fill_model)
        self._fill_delegate = _FillAccountDelegate(self)
        self.setItemDelegate(self._fill_delegate)
        self._empty = QtWidgets.QLabel(self.viewport())
        self._empty.setObjectName("accountCardDetail")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        set_text_role(self._empty, TextRole.UI_LABEL)
        typography_controller().changed.connect(self._refresh_typography)

    def _refresh_typography(self):
        self._fill_delegate.refresh_typography()
        self.doItemsLayout()
        self.viewport().update()

    def apply_theme(self, theme):
        self._fill_delegate.theme = ui_palette(theme)
        self.viewport().update()

    def set_payloads(self, payloads, empty_text):
        current = self.currentIndex().data(Qt.ItemDataRole.UserRole)
        selected_key = _account_key(current) if isinstance(current, dict) else None
        bar = self.verticalScrollBar()
        at_top = bar.value() == bar.minimum()
        top = self.indexAt(QtCore.QPoint(1, 1))
        top_payload = top.data(Qt.ItemDataRole.UserRole)
        anchor = _account_key(top_payload) if isinstance(top_payload, dict) else None
        offset = self.visualRect(top).top() if top.isValid() else 0
        self._empty.setText(empty_text)
        self._empty.setVisible(not payloads)
        if not self._fill_model.set_rows(payloads):
            return
        index = self._fill_model.index_for_key(selected_key)
        if not index.isValid() and payloads:
            index = self._fill_model.index(0, 0)
        self.selectionModel().setCurrentIndex(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if at_top:
            bar.setValue(bar.minimum())
        else:
            top = self._fill_model.index_for_key(anchor)
            if top.isValid():
                self.scrollTo(top, QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
                bar.setValue(bar.value()-offset)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._empty.setGeometry(12, 16, max(0,self.viewport().width()-24), 80)


class AccountCardList(QtWidgets.QListWidget):
    """Refresh row heights on presentation changes, without a polling timer."""
    presentation_changed = Signal()

    def __init__(self):
        super().__init__()
        self._height_refresh = QTimer(self)
        self._height_refresh.setSingleShot(True)
        self._height_refresh.setInterval(0)
        self._height_refresh.timeout.connect(self._sync_card_heights)
        typography_controller().changed.connect(self._schedule_card_heights)

    def _schedule_card_heights(self) -> None:
        self._height_refresh.start()

    @QtCore.Slot()
    def _select_requested_card(self) -> None:
        # A QObject receiver avoids retaining the list through a child signal
        # callback while Qt clears and defers deletion of the old row widgets.
        card = self.sender()
        for index in range(self.count()):
            item = self.item(index)
            if self.itemWidget(item) is card:
                self.setCurrentItem(item)
                return

    def _sync_card_heights(self) -> None:
        for index in range(self.count()):
            item = self.item(index)
            card = self.itemWidget(item)
            if card is None:
                continue
            if hasattr(card, "set_presentation_width"):
                card.set_presentation_width(self.viewport().width())
            required = max(card.minimumSizeHint().height(), card.sizeHint().height()) + 2
            if item.sizeHint().height() != required:
                item.setSizeHint(QtCore.QSize(0, required))
        rows = int(self.property("fitVisibleRows") or 0)
        if rows:
            count = min(rows, self.count())
            height = (sum(self.item(i).sizeHint().height() for i in range(count))
                      + 2 * self.spacing() * count + 2 * self.frameWidth())
            self.setFixedHeight(max(36, height))
        self.presentation_changed.emit()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._schedule_card_heights()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange}:
            timer = getattr(self, "_height_refresh", None)
            if timer is not None:
                timer.start()


def _new_account_card_list() -> QtWidgets.QListWidget:
    view = AccountCardList()
    view.setObjectName("tradingCardList")
    view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setSpacing(4)
    view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    return view


def _account_key(payload):
    if payload.get("_source") == "FILL":
        identifier = payload.get("id")
        if identifier is None:
            identifier = (payload.get("orderId"), payload.get("time"),
                          payload.get("price"), payload.get("qty"))
        return (str(payload.get("symbol", "")), "", "FILL", str(identifier))
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


def _account_key_is_stable(key):
    """Whether a key can safely identify one card across snapshots."""
    if not (
        isinstance(key, tuple)
        and len(key) == 4
        and all(isinstance(part, str) for part in key)
        and bool(key[0].strip())
    ):
        return False
    # Position rows have no order ID; symbol + positionSide (and source when
    # present) is their stable exchange identity.
    position_key = bool(key[1].strip()) and key[2].upper() in {"", "POSITION"}
    return bool(key[3].strip()) or position_key


def _same_account_card_factory(previous, current):
    if previous is current:
        return True
    # Bound methods are recreated on attribute access, but their target and
    # implementation still identify the same factory and signal wiring.
    previous_self = getattr(previous, "__self__", None)
    current_self = getattr(current, "__self__", None)
    previous_function = getattr(previous, "__func__", None)
    current_function = getattr(current, "__func__", None)
    return (
        previous_self is not None
        and previous_self is current_self
        and previous_function is not None
        and previous_function is current_function
    )


_ACCOUNT_CARD_CONTEXT_UNSET = object()


def _same_account_card_context(previous, current):
    if previous is _ACCOUNT_CARD_CONTEXT_UNSET or current is _ACCOUNT_CARD_CONTEXT_UNSET:
        return False
    if type(previous) is type(current) and isinstance(
        previous, (str, int, float, bool, bytes, type(None))
    ):
        return previous == current
    if isinstance(previous, tuple) and isinstance(current, tuple):
        return len(previous) == len(current) and all(
            _same_account_card_context(left, right)
            for left, right in zip(previous, current)
        )
    return False


_ACCOUNT_SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 51


class _AccountCardOrderItem(QtWidgets.QListWidgetItem):
    """List item that sorts by a numeric, per-refresh row rank."""

    def __init__(self, rank=0):
        super().__init__()
        self._account_order_rank = int(rank)

    def __lt__(self, other):
        if isinstance(other, _AccountCardOrderItem):
            return self._account_order_rank < other._account_order_rank
        return super().__lt__(other)


def _table_record_key(payload):
    if payload.get("asset"):
        return ("asset", str(payload["asset"]))
    if payload.get("id") is not None:
        return ("fill", str(payload.get("symbol", "")), str(payload["id"]))
    return (*_account_key(payload), str(payload.get("time", "")))


class _AccountTableItem(QtWidgets.QTableWidgetItem):
    def __lt__(self, other):
        left, right = self.data(_ACCOUNT_SORT_ROLE), other.data(_ACCOUNT_SORT_ROLE)
        if left is None or right is None:
            return left is not None and right is None
        if isinstance(left, (float, int)) and isinstance(right, (float, int)):
            return left < right
        return str(left).casefold() < str(right).casefold()


def _account_sort_value(header, text, payload):
    key = {"PRICE": "price", "SIZE": "qty", "PNL": "realizedPnl", "FEE": "commission",
           "WALLET": "walletBalance", "AVAILABLE": "availableBalance", "UPNL": "unrealizedProfit",
           "TIME": "time", "TIME LOCAL": "time"}.get(header)
    if key:
        value = payload.get(key)
        return safe_float(value) if value is not None else None
    return text


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
    view, payloads, factory, empty_text, *, fingerprint=None, update_existing=None,
    update_context=_ACCOUNT_CARD_CONTEXT_UNSET,
):
    payloads = [dict(payload) for payload in payloads]
    keys = [_account_key(payload) for payload in payloads]
    structure = (
        str(empty_text),
        tuple(keys),
    )
    # Live prices, partial fills and statuses change card content, not identity.
    # Keep selection, close-size choices, scroll position and signal connections.
    if (
        getattr(view, "_cards_fingerprint", None) == structure
        and _same_account_card_factory(getattr(view, "_cards_factory", None), factory)
    ):
        # Skip card work only for an explicitly supplied, unchanged context;
        # callbacks may depend on non-payload inputs such as the selected symbol.
        same_context = _same_account_card_context(
            getattr(view, "_cards_update_context", _ACCOUNT_CARD_CONTEXT_UNSET),
            update_context,
        )
        if payloads and update_existing is not None and view.count() == len(payloads):
            for index, payload in enumerate(payloads):
                item = view.item(index)
                previous_payload = item.data(Qt.ItemDataRole.UserRole)
                if same_context and previous_payload == payload:
                    continue
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                card = view.itemWidget(item)
                update_existing(card, dict(payload))
                required = max(card.minimumSizeHint().height(), card.sizeHint().height()) + 2
                if item.sizeHint().height() != required:
                    item.setSizeHint(QtCore.QSize(0, required))
        view._cards_update_context = update_context
        return False

    previous = view.currentItem()
    previous_payload = previous.data(Qt.ItemDataRole.UserRole) if previous else None
    selected_key = _account_key(previous_payload) if isinstance(previous_payload, dict) else None
    scroll = view.verticalScrollBar().value()

    # QListWidget's internal list model can move rows while preserving the
    # persistent index widgets installed by setItemWidget. Reuse is safe only
    # when both snapshots give every row a unique, usable identity and the
    # factory is unchanged; otherwise retain the original clear/rebuild path.
    old_rows = {}
    old_keys = []
    can_reconcile = (
        bool(payloads)
        and factory is not None
        and update_existing is not None
        and _same_account_card_factory(getattr(view, "_cards_factory", None), factory)
        and len(keys) == len(set(keys))
        and all(_account_key_is_stable(key) for key in keys)
        and view.count() > 0
    )
    if can_reconcile:
        for index in range(view.count()):
            item = view.item(index)
            payload = item.data(Qt.ItemDataRole.UserRole)
            card = view.itemWidget(item)
            if not isinstance(payload, dict) or card is None:
                can_reconcile = False
                break
            key = _account_key(payload)
            if not _account_key_is_stable(key) or key in old_rows:
                can_reconcile = False
                break
            old_rows[key] = (item, card)
            old_keys.append(key)
        if len(old_keys) != view.count() or not set(old_keys).intersection(keys):
            can_reconcile = False

    if can_reconcile:
        updates_enabled = view.updatesEnabled()
        view.setUpdatesEnabled(False)
        reconciled = False
        try:
            new_by_key = dict(zip(keys, payloads))
            same_context = _same_account_card_context(
                getattr(view, "_cards_update_context", _ACCOUNT_CARD_CONTEXT_UNSET),
                update_context,
            )
            # Update surviving rows before editing the model. If a factory or
            # updater is incompatible, the caller can still rebuild cleanly.
            for key in keys:
                existing = old_rows.get(key)
                if existing is None:
                    continue
                item, card = existing
                payload = new_by_key[key]
                if (
                    same_context
                    and item.data(Qt.ItemDataRole.UserRole) == payload
                ):
                    continue
                update_existing(card, dict(payload))
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                required = max(card.minimumSizeHint().height(), card.sizeHint().height()) + 2
                if item.sizeHint().height() != required:
                    item.setSizeHint(QtCore.QSize(0, required))

            model = view.model()
            model_parent = QtCore.QModelIndex()
            removed = [index for index, key in enumerate(old_keys) if key not in new_by_key]
            # Remove contiguous runs from the bottom so surviving row indexes
            # remain valid. QListWidget's model schedules removed index widgets
            # for deletion and reparents no surviving cards.
            end = len(removed)
            while end:
                start_pos = end - 1
                while start_pos > 0 and removed[start_pos - 1] == removed[start_pos] - 1:
                    start_pos -= 1
                first = removed[start_pos]
                count = end - start_pos
                if not model.removeRows(first, count, model_parent):
                    raise RuntimeError("QListWidget model refused row removal")
                end = start_pos

            live_rows = {key: row for key, row in old_rows.items() if key in new_by_key}
            desired_rank = {key: rank for rank, key in enumerate(keys)}
            for key, payload in zip(keys, payloads):
                if key in live_rows:
                    continue
                item = _AccountCardOrderItem(desired_rank[key])
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                card = factory(dict(payload))
                item.setSizeHint(
                    QtCore.QSize(0, max(card.minimumSizeHint().height(), card.sizeHint().height()) + 2)
                )
                view.addItem(item)
                view.setItemWidget(item, card)
                if hasattr(card, "selected_requested"):
                    card.selected_requested.connect(view._select_requested_card)
                live_rows[key] = (item, card)

            live_keys = [
                _account_key(view.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(view.count())
            ]
            if live_keys != keys:
                if all(isinstance(live_rows[key][0], _AccountCardOrderItem) for key in keys):
                    # QListWidget's native sort preserves persistent indexes and
                    # their index widgets. Unique integer ranks express the
                    # incoming order with one model sort instead of many row moves.
                    for rank, key in enumerate(keys):
                        live_rows[key][0]._account_order_rank = rank
                    view.sortItems(Qt.SortOrder.AscendingOrder)
                    live_keys = [
                        _account_key(view.item(index).data(Qt.ItemDataRole.UserRole))
                        for index in range(view.count())
                    ]

                if live_keys != keys:
                    for target, key in enumerate(keys):
                        source = live_keys.index(key, target)
                        if source == target:
                            continue
                        # destinationChild uses pre-move coordinates. Moving a
                        # later row to target places it directly at that index.
                        if not model.moveRows(model_parent, source, 1, model_parent, target):
                            raise RuntimeError("QListWidget model refused row move")
                        live_keys.insert(target, live_keys.pop(source))

            selected_item = live_rows.get(selected_key, (None, None))[0]
            if selected_item is None and view.count():
                selected_item = view.item(0)
            if selected_item is not None and view.currentItem() is not selected_item:
                view.setCurrentItem(selected_item)
            view.verticalScrollBar().setValue(scroll)
            view._cards_fingerprint = structure
            view._cards_factory = factory
            view._cards_update_context = update_context
            reconciled = True
        except Exception:
            # A Qt binding/model variation or a custom updater may not support
            # safe reuse. A full rebuild below remains the correctness fallback.
            reconciled = False
        finally:
            view.setUpdatesEnabled(updates_enabled)
        if reconciled:
            return True

    updates_enabled = view.updatesEnabled()
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
            view._cards_factory = factory
            view._cards_update_context = update_context
            return True
        selection = None
        for rank, payload in enumerate(payloads):
            item = _AccountCardOrderItem(rank)
            item.setData(Qt.ItemDataRole.UserRole, dict(payload))
            card = factory(dict(payload))
            item.setSizeHint(QtCore.QSize(0, max(card.minimumSizeHint().height(), card.sizeHint().height()) + 2))
            view.addItem(item)
            view.setItemWidget(item, card)
            if hasattr(card, "selected_requested"):
                card.selected_requested.connect(view._select_requested_card)
            if selected_key == _account_key(payload):
                selection = item
        view.setCurrentItem(selection or view.item(0))
        view.verticalScrollBar().setValue(scroll)
        view._cards_fingerprint = structure
        view._cards_factory = factory
        view._cards_update_context = update_context
        return True
    finally:
        view.setUpdatesEnabled(updates_enabled)


def _populate_fill_cards(view: FillAccountView, snapshot: dict[str, Any],
                         symbol: str, *, connected: bool) -> None:
    payloads = [{**row, "_source": "FILL"}
                for row in sorted(snapshot.get("fills", []),
                                  key=lambda item: safe_float(item.get("time")), reverse=True)
                if str(row.get("symbol", "")) == symbol]
    view.set_payloads(
        payloads,
        f"No fills for {symbol}" if connected else "Connect API credentials to view fills",
    )


class PositionDeskRow(QtWidgets.QFrame):
    """One selectable position, with details rendered once below the list."""
    selected_requested = Signal()

    def __init__(self, payload, current_symbol):
        super().__init__()
        self.setObjectName("positionDeskRow")
        self.setMinimumWidth(0)
        row = QtWidgets.QGridLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setHorizontalSpacing(10)
        row.setVerticalSpacing(4)
        self.symbol_label = ElidedLabel("")
        self.side_label = QtWidgets.QLabel("")
        self.side_label.setObjectName("accountCardSide")
        self.leverage_label = QtWidgets.QLabel("")
        self.leverage_label.setObjectName("accountCardDetail")
        self.quantity_label = ElidedLabel("")
        self.quantity_label.setObjectName("accountCardDetail")
        self.quantity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.pnl_label = ElidedLabel("")
        self.pnl_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for label, role in ((self.symbol_label, TextRole.INSTRUMENT_SYMBOL),
                            (self.side_label, TextRole.UI_CAPTION),
                            (self.leverage_label, TextRole.UI_CAPTION),
                            (self.quantity_label, TextRole.UI_CAPTION),
                            (self.pnl_label, TextRole.MARKET_VALUE)):
            set_text_role(label, role)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        metadata = QtWidgets.QHBoxLayout()
        metadata.setSpacing(6)
        metadata.addWidget(self.side_label)
        metadata.addWidget(self.leverage_label)
        metadata.addStretch(1)
        row.addWidget(self.symbol_label, 0, 0)
        row.addWidget(self.pnl_label, 0, 1)
        row.addLayout(metadata, 1, 0)
        row.addWidget(self.quantity_label, 1, 1)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
        self.update_payload(payload, current_symbol)

    def update_payload(self, payload, current_symbol):
        self.payload = dict(payload)
        direction = _position_direction(payload)
        _set_text_if_changed(self.symbol_label, str(payload.get("symbol") or "—"))
        _set_text_if_changed(self.side_label, direction)
        _set_text_if_changed(self.leverage_label, f"{payload.get('leverage') or '—'}×")
        _set_text_if_changed(self.quantity_label, f"Qty {_quantity_text(abs(safe_float(payload.get('positionAmt'))))}")
        _set_repolished_property(self.side_label, "direction", direction.lower())
        pnl = safe_float(payload.get("unrealizedProfit"))
        quote = str(payload.get('_quote_asset') or ('USDC' if str(payload.get('symbol', '')).endswith('USDC') else 'USDT'))
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl).replace('$', '')} {quote}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))

    def update_mark_price(self, mark):
        pnl = _update_position_mark(self.payload, mark)
        self.update_payload(self.payload, "")
        return pnl

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


class PositionDeskDetails(QtWidgets.QFrame):
    trade_requested = Signal(object)
    close_requested = Signal(object, int)
    close_limit_requested = Signal(object, int)

    def __init__(self):
        super().__init__()
        self.payload = {}
        self.setObjectName("positionDeskDetails")
        self.setMinimumWidth(0)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        heading = QtWidgets.QHBoxLayout()
        self._detail_layout = layout
        self._detail_heading = heading
        self.title = ElidedLabel("Select a position")
        set_text_role(self.title, TextRole.INSTRUMENT_SYMBOL)
        heading.addWidget(self.title, 1)
        layout.addLayout(heading)
        self.pnl_label = ElidedLabel("—")
        self.pnl_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_text_role(self.pnl_label, TextRole.MARKET_VALUE_LARGE)
        returns = QtWidgets.QHBoxLayout()
        returns.setSpacing(8)
        caption = ElidedLabel("Unrealized PnL")
        caption.setObjectName("accountCardDetail")
        set_text_role(caption, TextRole.UI_CAPTION)
        self.roe_label = QtWidgets.QLabel("—")
        self.roe_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_text_role(self.roe_label, TextRole.TABLE_VALUE)
        returns.addWidget(caption, 1)
        returns.addWidget(self.pnl_label, 1)
        heading.addWidget(self.roe_label)
        layout.addLayout(returns)
        self.metrics = QtWidgets.QGridLayout()
        self.metrics.setHorizontalSpacing(12)
        self.metrics.setVerticalSpacing(6)
        self.values = {}
        self.captions = {}
        self.risk_captions = {}
        for key, text in (("entry", "Entry price"), ("mark", "Mark price"), ("size", "Position size"), ("value", "Notional")):
            cap = QtWidgets.QLabel(text)
            cap.setObjectName("accountCardDetail")
            set_text_role(cap, TextRole.UI_CAPTION)
            value = ElidedLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            set_text_role(value, TextRole.TABLE_VALUE)
            self.captions[key], self.values[key] = cap, value
        layout.addLayout(self.metrics)
        risk = QtWidgets.QFrame()
        risk.setObjectName("deskRiskMetrics")
        risk_grid = QtWidgets.QGridLayout(risk)
        self.risk_grid = risk_grid
        risk_grid.setContentsMargins(0, 8, 0, 0)
        risk_grid.setHorizontalSpacing(10)
        risk_grid.setVerticalSpacing(6)
        for column, (key, text) in enumerate((("liquidation", "Liq. price"), ("distance", "Distance"), ("margin", "Initial margin"))):
            cap = ElidedLabel(text)
            cap.setObjectName("accountCardDetail")
            set_text_role(cap, TextRole.UI_CAPTION)
            value = ElidedLabel("—")
            set_text_role(value, TextRole.TABLE_VALUE)
            if key != "margin":
                value.setObjectName("accountCardRisk")
                value.setProperty("risk", "warning")
            risk_grid.addWidget(cap, 0, column)
            risk_grid.addWidget(value, 1, column)
            risk_grid.setColumnStretch(column, 1)
            self.values[key] = value
            self.risk_captions[key] = cap
        layout.addWidget(risk)
        self._detail_actions = QtWidgets.QWidget()
        self._detail_actions.setObjectName("tradingInlineField")
        actions = QtWidgets.QHBoxLayout(self._detail_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.trade_button = QtWidgets.QPushButton("Order ticket")
        self.trade_button.setAccessibleName("Prepare a reduce-only order for this position")
        self.trade_button.setToolTip('Open the ticket to reduce this position.')
        self.trade_button.setObjectName("deskTradePosition")
        set_text_role(self.trade_button, TextRole.UI_CONTROL)
        self.trade_button.clicked.connect(lambda: self.trade_requested.emit(dict(self.payload)) if self.payload else None)
        self.close_button = QtWidgets.QPushButton("Market close")
        self.close_button.setToolTip("Close 100% of this position at market")
        set_text_role(self.close_button, TextRole.UI_CONTROL)
        self.close_button.clicked.connect(
            lambda: self.close_requested.emit(dict(self.payload), 100) if self.payload else None
        )
        self.limit_close_button = QtWidgets.QToolButton()
        self.limit_close_button.setText("Limit close…")
        self.limit_close_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        set_text_role(self.limit_close_button, TextRole.UI_CONTROL)
        self.close_menu = QtWidgets.QMenu(self.limit_close_button)
        self.limit_close_button.setMenu(self.close_menu)
        self.set_close_percentages([25, 50, 100])
        actions.addWidget(self.trade_button)
        actions.addWidget(self.close_button)
        actions.addWidget(self.limit_close_button)
        actions.addStretch(1)
        layout.addWidget(self._detail_actions)
        self._wide = None
        self.set_presentation_width(500)
        self.set_payload(None)

    def set_close_percentages(self, values):
        self.close_menu.clear()
        for percent in values:
            action = self.close_menu.addAction(f"{percent}%")
            action.triggered.connect(lambda _checked=False, p=int(percent):
                                     self.close_limit_requested.emit(dict(self.payload), p) if self.payload else None)

    def set_presentation_width(self, width):
        columns = 2 if width >= 520 else 1
        if columns == self._wide:
            return
        self._wide = columns
        for key in self.captions:
            self.metrics.removeWidget(self.captions[key])
            self.metrics.removeWidget(self.values[key])
        for index, key in enumerate(("entry", "mark", "size", "value")):
            row, column = divmod(index, columns)
            self.metrics.addWidget(self.captions[key], row, column * 2)
            self.metrics.addWidget(self.values[key], row, column * 2 + 1)
            self.values[key].setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for column in range(4):
            self.metrics.setColumnStretch(column, (2 if column % 2 else 1) if column < columns * 2 else 0)
        for key, cap in self.risk_captions.items():
            self.risk_grid.removeWidget(cap)
            self.risk_grid.removeWidget(self.values[key])
        for index, key in enumerate(("liquidation", "distance", "margin")):
            row, column = divmod(index, columns)
            self.risk_grid.addWidget(self.risk_captions[key], row, column * 2)
            self.risk_grid.addWidget(self.values[key], row, column * 2 + 1)
            self.values[key].setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for column in range(4):
            self.risk_grid.setColumnStretch(column, (2 if column % 2 else 1) if column < columns * 2 else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_presentation_width(event.size().width())

    def set_payload(self, payload):
        self.payload = dict(payload or {})
        self.setVisible(bool(self.payload))
        if not self.payload:
            return
        symbol = str(payload.get("symbol") or "")
        quote = str(payload.get("_quote_asset") or "USDT")
        margin_asset = str(payload.get("_margin_asset") or quote)
        base = symbol.removesuffix(quote)
        direction = _position_direction(payload)
        pnl = safe_float(payload.get("unrealizedProfit"))
        mark = safe_float(payload.get("markPrice"))
        margin = safe_float(payload.get("positionInitialMargin") or payload.get("initialMargin"))
        liquidation = safe_float(payload.get("liquidationPrice"))
        _set_text_if_changed(self.title, f"{symbol} · {direction}")
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl).replace("$", "")} {quote}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(self.roe_label, f"ROE {pnl / margin * 100:+.1f}%" if margin > 0 else "ROE —")
        distance = abs(mark - liquidation) / mark * 100 if mark > 0 and liquidation > 0 else None
        values = {
            "entry": format_price(safe_float(payload.get("entryPrice"))),
            "mark": format_price(mark) if mark > 0 else "—",
            "size": f"{_quantity_text(abs(safe_float(payload.get('positionAmt'))))} {base}",
            "value": f"{human_number(_position_notional(payload), money=True).replace('$', '')} {quote}",
            "liquidation": format_price(liquidation) if liquidation > 0 else "—",
            "distance": f"{distance:.1f}%" if distance is not None else "—",
            "margin": f"{human_number(margin, money=True).replace('$', '')} {margin_asset}" if margin > 0 else "—",
        }
        for key, text in values.items():
            _set_text_if_changed(self.values[key], text)
        for key in ("liquidation", "distance"):
            _set_repolished_property(self.values[key], "risk", "critical" if distance is not None and distance < 5 else "warning")


class WorkingOrderRow(QtWidgets.QFrame):
    cancel_requested = Signal(object)
    selected_requested = Signal()

    def __init__(self, payload, theme, current_symbol, *, compact=False):
        super().__init__()
        self._compact = compact
        self.setObjectName("workingOrderRow")
        self.setMinimumWidth(0)
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(6, 4 if compact else 9, 6, 4 if compact else 9)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(4)
        self.symbol_label = ElidedLabel("")
        self.side_label = ElidedLabel("")
        self.side_label.setObjectName("accountCardSide")
        self.type_label = ElidedLabel("")
        self.type_label.setObjectName("accountCardDetail")
        self.price_label = ElidedLabel("")
        self.quantity_label = ElidedLabel("")
        self.status_label = ElidedLabel("")
        self.status_label.setObjectName("accountOrderStatus")
        self.fill_progress = QtWidgets.QProgressBar()
        self.fill_progress.setObjectName("orderFillProgress")
        self.fill_progress.setRange(0, 1000)
        self.fill_progress.setTextVisible(False)
        self.fill_progress.setFixedHeight(3)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setObjectName("deskOrderCancel")
        self.cancel_button.setMinimumWidth(0)
        if compact:
            self.cancel_button.setObjectName("activityOrderCancel")
            self.cancel_button.setText("×")
            self.cancel_button.setFixedSize(28, 28)
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(dict(self.payload)))
        for widget, role in ((self.symbol_label, TextRole.INSTRUMENT_SYMBOL),
                             (self.side_label, TextRole.UI_CONTROL),
                             (self.type_label, TextRole.UI_CAPTION),
                             (self.price_label, TextRole.TABLE_VALUE),
                             (self.quantity_label, TextRole.TABLE_VALUE),
                             (self.status_label, TextRole.UI_CAPTION),
                             (self.cancel_button, TextRole.UI_CONTROL)):
            set_text_role(widget, role)
        self._wide = None
        self.update_payload(payload, current_symbol)
        self.set_presentation_width(400)

    def set_presentation_width(self, width):
        required = max(520, self.price_label.fontMetrics().horizontalAdvance("00,000.00") * 3 +
                       self.symbol_label.fontMetrics().horizontalAdvance("BTCUSDT") +
                       self.cancel_button.sizeHint().width() + 135)
        wide = width >= required
        compact_stacked = self._compact and width < max(
            400, self.quantity_label.fontMetrics().horizontalAdvance("Qty 0.000000") * 3 + 90)
        signature = (wide, compact_stacked)
        if signature == getattr(self, "_layout_signature", None):
            return
        self._layout_signature = signature
        self._wide = wide
        while self.grid.count():
            self.grid.takeAt(0)
        for column in range(6):
            self.grid.setColumnStretch(column, 0)
        if wide:
            for widget, row, column, span in ((self.symbol_label, 0, 0, 1), (self.side_label, 0, 1, 1),
                                             (self.type_label, 0, 2, 1), (self.price_label, 1, 2, 1),
                                             (self.quantity_label, 0, 3, 1), (self.status_label, 0, 4, 1),
                                             (self.fill_progress, 1, 4, 1)):
                self.grid.addWidget(widget, row, column, 1, span)
            self.grid.addWidget(self.cancel_button, 0, 5, 2, 1)
            for column, stretch in ((0, 3), (1, 2), (2, 4), (3, 3), (4, 4)):
                self.grid.setColumnStretch(column, stretch)
        elif compact_stacked:
            # At rail widths, give price and quantity a full column instead of
            # squeezing three numeric/text columns beside the cancel control.
            for widget, row, column in ((self.symbol_label, 0, 0), (self.side_label, 0, 1),
                                        (self.type_label, 1, 0), (self.price_label, 1, 1),
                                        (self.status_label, 2, 0), (self.quantity_label, 2, 1)):
                self.grid.addWidget(widget, row, column)
            self.grid.addWidget(self.cancel_button, 0, 2, 3, 1)
            self.grid.addWidget(self.fill_progress, 3, 0, 1, 2)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
            self.side_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif self._compact:
            self.grid.addWidget(self.symbol_label, 0, 0)
            self.grid.addWidget(self.side_label, 0, 1)
            self.grid.addWidget(self.price_label, 0, 2)
            self.grid.addWidget(self.type_label, 1, 0)
            self.grid.addWidget(self.quantity_label, 1, 1)
            self.grid.addWidget(self.status_label, 1, 2)
            self.grid.addWidget(self.cancel_button, 0, 3, 2, 1)
            self.grid.addWidget(self.fill_progress, 2, 0, 1, 3)
            for column, stretch in ((0, 3), (1, 2), (2, 3)):
                self.grid.setColumnStretch(column, stretch)
            self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            self.grid.addWidget(self.symbol_label, 0, 0, 1, 3)
            self.grid.addWidget(self.cancel_button, 0, 3)
            self.grid.addWidget(self.side_label, 1, 0)
            self.grid.addWidget(self.type_label, 1, 1)
            self.grid.addWidget(self.price_label, 1, 2, 1, 2)
            self.grid.addWidget(self.status_label, 2, 0, 1, 2)
            self.grid.addWidget(self.quantity_label, 2, 2, 1, 2)
            self.grid.addWidget(self.fill_progress, 3, 0, 1, 4)
            self.grid.setColumnStretch(0, 2)
            self.grid.setColumnStretch(1, 3)
            self.grid.setColumnStretch(2, 3)
            self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.quantity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if not compact_stacked:
            self.side_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if hasattr(self, "_quantity_text"):
            _set_text_if_changed(self.quantity_label, self._quantity_text if wide else f"Qty {self._quantity_text}")

    def update_payload(self, payload, current_symbol=None):
        self.payload = dict(payload)
        side = str(payload.get("side") or "")
        kind = str(payload.get("type") or payload.get("orderType") or "").replace("_", " ").title()
        price = payload.get("price")
        if safe_float(price) <= 0:
            price = payload.get("triggerPrice") or payload.get("stopPrice") or payload.get("activatePrice")
        status = str(payload.get("status") or payload.get("algoStatus") or "NEW").upper()
        total = safe_float(payload.get("origQty") or payload.get("quantity"))
        done = safe_float(payload.get("executedQty") or payload.get("cumQty"))
        filled = max(0.0, min(100.0, done / total * 100)) if total > 0 else 0
        reducing = payload.get("reduceOnly") is True or str(payload.get("reduceOnly")).lower() == "true"
        _set_text_if_changed(self.symbol_label, str(payload.get("symbol") or "—"))
        _set_text_if_changed(self.side_label, side.title())
        _set_repolished_property(self.side_label, "direction", "long" if side == "BUY" else "short")
        _set_text_if_changed(self.type_label, kind)
        price_text = format_price(safe_float(price)) if safe_float(price) > 0 else "Market"
        if "Trailing" in kind:
            price_text = f"Trail {payload.get('callbackRate') or '—'}%"
        _set_text_if_changed(self.price_label, price_text)
        self._quantity_text = _quantity_text(payload.get("origQty") or payload.get("quantity") or "0")
        _set_text_if_changed(self.quantity_label, self._quantity_text if self._wide else f"Qty {self._quantity_text}")
        text = f"{filled:.0f}% filled" if filled > 0 else "Reduce only" if reducing else status.replace("_", " ").title()
        if reducing and filled > 0:
            text += " · Reduce only"
        _set_text_if_changed(self.status_label, text)
        self.fill_progress.setVisible(filled > 0)
        self.fill_progress.setValue(round(filled * 10))
        identifier = payload.get("algoId") if payload.get("_source") == "ALGO" else payload.get("orderId")
        self.cancel_button.setEnabled(identifier not in (None, ""))
        self.cancel_button.setAccessibleName(f"Cancel {payload.get('symbol', '')} {kind} order {identifier or ''}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected_requested.emit()
        super().mousePressEvent(event)


class PositionDeskView(QtWidgets.QFrame):
    """Tabbed account activity contained inside the Trading panel."""
    position_selected = Signal(object)
    trade_requested = Signal(object)

    def __init__(self, position_factory, order_factory):
        super().__init__()
        self.setObjectName("positionDeskView")
        self.setMinimumSize(0, 0)
        self.symbol_rules = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 10)
        root.setSpacing(8)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("tradingAccountTabs")
        self.tabs.setDocumentMode(True)
        set_text_role(self.tabs.tabBar(), TextRole.UI_CONTROL)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName("positionDeskScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.position_area = QtWidgets.QWidget()
        self.position_area.setObjectName("positionDeskBody")
        self.body_layout = QtWidgets.QVBoxLayout(self.position_area)
        self.body_layout.setContentsMargins(0, 4, 2, 0)
        self.body_layout.setSpacing(8)
        self.positions = _new_account_card_list()
        self.positions.setSpacing(6)
        self.positions.setProperty("fitVisibleRows", 3)
        self.body_layout.addWidget(self.positions)
        self.details = PositionDeskDetails()
        self.body_layout.addWidget(self.details)
        self.details.trade_requested.connect(self.trade_requested)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.position_area)
        self.tabs.addTab(self.scroll, "Positions")
        self.orders = _new_account_card_list()
        self.orders.setSpacing(6)
        self.fills = FillAccountView()
        self.fills.setSpacing(0)
        order_page = QtWidgets.QWidget()
        order_page.setObjectName("accountActivityPanel")
        order_layout = QtWidgets.QVBoxLayout(order_page)
        order_layout.setContentsMargins(0, 0, 0, 0)
        order_layout.setSpacing(0)
        self.order_headers = QtWidgets.QWidget()
        self.order_headers.setObjectName("tradingInlineField")
        headers = QtWidgets.QHBoxLayout(self.order_headers)
        headers.setContentsMargins(6, 5, 6, 5)
        headers.setSpacing(8)
        for text, stretch in (("Symbol", 3), ("Side", 2), ("Type / Price", 4), ("Quantity", 3), ("Status", 4), ("Action", 0)):
            label = ElidedLabel(text)
            label.setObjectName("accountCardDetail")
            set_text_role(label, TextRole.UI_CAPTION)
            if text == "Action":
                label.setMinimumWidth(54)
            headers.addWidget(label, stretch)
        self.order_headers.hide()
        order_layout.addWidget(self.order_headers)
        order_layout.addWidget(self.orders, 1)
        self.tabs.addTab(order_page, "Orders")
        self.orders.presentation_changed.connect(self._sync_order_headers)
        self.tabs.addTab(self.fills, "Fills")
        root.addWidget(self.tabs, 1)
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(6)
        self.cancel_button = QtWidgets.QPushButton("Cancel symbol orders")
        self.cancel_button.setObjectName("deskCancelSymbolOrders")
        set_text_role(self.cancel_button, TextRole.UI_CONTROL)
        self.cancel_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum,
                                         QtWidgets.QSizePolicy.Policy.Fixed)
        self.more_button = QtWidgets.QToolButton()
        self.more_button.setText("More")
        set_text_role(self.more_button, TextRole.UI_CONTROL)
        self.more_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QtWidgets.QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        self.more_button.hide()
        self.status = ElidedLabel("All symbols · Current pair first")
        self.status.setObjectName("accountCardDetail")
        set_text_role(self.status, TextRole.UI_CAPTION)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.more_button)
        root.addLayout(footer)
        self.positions.currentItemChanged.connect(self._selection_changed)
        self.orders.currentItemChanged.connect(self._sync_selected_rows)
        self._position_factory, self._order_factory = position_factory, order_factory
        typography_controller().changed.connect(self._refresh_details_typography)

    def _refresh_details_typography(self):
        self.details.set_presentation_width(self.details.width())

    def _sync_order_headers(self):
        first = self.orders.item(0)
        row = self.orders.itemWidget(first) if first else None
        self.order_headers.setVisible(isinstance(row, WorkingOrderRow) and bool(row._wide))
        if isinstance(row, WorkingOrderRow) and row._wide:
            row.grid.activate()
            width = self.orders.viewport().width()
            if self.order_headers.maximumWidth() != width:
                self.order_headers.setMaximumWidth(width)
            for column in range(6):
                label = self.order_headers.layout().itemAt(column).widget()
                required = row.grid.cellRect(0, column).width()
                if required > 0 and label.maximumWidth() != required:
                    label.setMaximumWidth(required)

    def _selection_changed(self, *_args):
        item = self.positions.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._show_position_details(payload)
        self._sync_selected_rows()
        if isinstance(payload, dict):
            self.position_selected.emit(dict(payload))

    def _show_position_details(self, payload):
        if not isinstance(payload, dict):
            self.details.set_payload(None)
            return
        display = dict(payload)
        symbol = str(payload.get("symbol") or "")
        rules = self.symbol_rules.get(symbol)
        quote = rules.quote_asset if rules is not None else "USDC" if symbol.endswith("USDC") else "USDT"
        display["_quote_asset"] = quote
        display["_margin_asset"] = rules.margin_asset if rules is not None else quote
        self.details.set_payload(display)

    def _sync_selected_rows(self, *_args):
        for view in (self.positions, self.orders):
            current = view.currentItem()
            for index in range(view.count()):
                item = view.item(index)
                card = view.itemWidget(item)
                if card is not None:
                    _set_repolished_property(card, "selected", item is current)

    def set_positions(self, payloads, symbol, connected):
        blocker = QtCore.QSignalBlocker(self.positions)
        _populate_account_cards(self.positions, payloads, self._position_factory,
                                "No open positions" if connected else "Connect API credentials to view positions",
                                update_existing=lambda card, payload: card.update_payload(payload, symbol),
                                update_context=symbol)
        del blocker
        self.positions._schedule_card_heights()
        self.tabs.setTabText(0, f"Positions {len(payloads)}")
        self._selection_changed()

    def select_position(self, payload):
        key = _account_key(payload)
        for index in range(self.positions.count()):
            item = self.positions.item(index)
            row = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(row, dict) and _account_key(row) == key:
                if self.positions.currentItem() is not item:
                    self.positions.setCurrentItem(item)
                return

    def set_orders(self, payloads, symbol, connected):
        _populate_account_cards(self.orders, payloads, self._order_factory,
                                "No working orders" if connected else "Connect API credentials to view orders",
                                update_existing=lambda card, payload: card.update_payload(payload, symbol),
                                update_context=symbol)
        self.orders._schedule_card_heights()
        self.tabs.setTabText(1, f"Orders {len(payloads)}")
        _set_text_if_changed(self.cancel_button, f"Cancel {symbol} orders")

    def set_mark_price(self, mark, symbol):
        for index in range(self.positions.count()):
            item = self.positions.item(index)
            card = self.positions.itemWidget(item)
            if isinstance(card, PositionDeskRow) and card.payload.get("symbol") == symbol:
                card.update_mark_price(mark)
                item.setData(Qt.ItemDataRole.UserRole, dict(card.payload))
        item = self.positions.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._show_position_details(payload)


class TradingActivityPanel(QtWidgets.QFrame):
    """Use spare ticket height for live positions and working orders together."""
    account_requested = Signal(int)
    position_requested = Signal(object)

    def __init__(self, order_factory):
        super().__init__()
        self.setObjectName("tradingAdaptiveActivity")
        self.setMinimumSize(0, 0)
        self._order_factory = order_factory
        self._wide = False
        self._position_count = self._order_count = 0
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)
        root.setSpacing(6)
        self.summary = QtWidgets.QWidget()
        self.summary.setObjectName("tradingInlineField")
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        summary = QtWidgets.QHBoxLayout(self.summary)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(8)
        self.positions_summary = self._heading("Positions 0", 0)
        self.orders_summary = self._heading("Orders 0", 1)
        summary.addWidget(self.positions_summary)
        summary.addWidget(self.orders_summary)
        self.total_pnl = ElidedLabel("UPNL —")
        self.total_pnl.setObjectName("accountTotalPnl")
        self.total_pnl.setAccessibleName("Total unrealized profit and loss")
        set_text_role(self.total_pnl, TextRole.UI_CAPTION)
        self.total_pnl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        summary.addWidget(self.total_pnl, 1)
        root.addWidget(self.summary)
        self.body = QtWidgets.QWidget()
        self.body.setObjectName("tradingActivityBody")
        self.columns = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom, self.body)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(6)
        self.positions_heading = self._heading("Positions / P&L", 0)
        self.orders_heading = self._heading("Active orders", 1)
        self.positions = _new_account_card_list()
        self.orders = _new_account_card_list()
        self.positions.setAccessibleName("Open positions")
        self.orders.setAccessibleName("Working orders")
        self.sections = []
        for heading, view in ((self.positions_heading, self.positions),
                              (self.orders_heading, self.orders)):
            section = QtWidgets.QWidget()
            section.setObjectName("tradingActivitySection")
            section.setMinimumSize(0, 0)
            box = QtWidgets.QVBoxLayout(section)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            box.addWidget(heading)
            view.setMinimumSize(0, 0)
            view.setSpacing(2)
            box.addWidget(view, 1)
            self.columns.addWidget(section, 1)
            self.sections.append(section)
        self.positions.itemActivated.connect(self._activate_position)
        root.addWidget(self.body, 1)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

    def _heading(self, text, tab):
        button = QtWidgets.QPushButton(text)
        button.setObjectName("activityHeading")
        button.setAutoDefault(False)
        button.setMinimumWidth(0)
        button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        set_text_role(button, TextRole.UI_CAPTION)
        button.clicked.connect(lambda: self.account_requested.emit(tab))
        return button

    def summary_height(self):
        margins = self.layout().contentsMargins()
        return self.summary.sizeHint().height() + margins.top() + margins.bottom()

    def set_available_size(self, width, height):
        wide = width >= max(680, self.fontMetrics().horizontalAdvance("0" * 80))
        self._wide = wide
        self.columns.setDirection(QtWidgets.QBoxLayout.Direction.LeftToRight if wide
                                  else QtWidgets.QBoxLayout.Direction.TopToBottom)
        divided = bool(self._position_count and self._order_count)
        row_heights = []
        for heading, view in ((self.positions_heading, self.positions),
                              (self.orders_heading, self.orders)):
            first = view.item(0)
            row_height = first.sizeHint().height() if first else 48
            heading.setVisible(divided)
            row_heights.append((heading.sizeHint().height() + 2 if divided else 0) + row_height + 4)
        room = height - self.summary_height() - self.layout().spacing()
        both = room >= (max(row_heights) if wide else sum(row_heights) + self.columns.spacing())
        show_positions = bool(self._position_count) and (both or not self._order_count)
        show_orders = bool(self._order_count) or not self._position_count
        if show_positions and room < row_heights[0]:
            show_positions = False
        if show_orders and room < row_heights[1]:
            show_orders = False
        self.sections[0].setVisible(show_positions)
        self.sections[1].setVisible(show_orders)
        self.body.setVisible(show_positions or show_orders)
        if wide:
            self.columns.setStretch(0, 2)
            self.columns.setStretch(1, 3)
        else:
            # Allocate rows by their content so one position doesn't take half
            # the panel while a longer working-order list has to scroll.
            for index, (view, row_height) in enumerate(zip((self.positions, self.orders), row_heights)):
                self.columns.setStretch(index, row_height * max(1, min(view.count(), 6)))

    def set_payloads(self, positions, orders, symbol, connected):
        self._position_count, self._order_count = len(positions), len(orders)
        for view, payloads, factory, empty in (
            (self.positions, positions, self._position_card, "No open positions"),
            (self.orders, orders, self._order_factory, "No active orders"),
        ):
            _populate_account_cards(
                view, payloads, factory, empty if connected else "Connect API to view account",
                update_existing=lambda card, payload: card.update_payload(payload, symbol),
                update_context=symbol,
            )
            view._schedule_card_heights()
        self.positions_heading.setText(f"Positions · {len(positions)}")
        self.orders_heading.setText(f"Active orders · {len(orders)}")
        self.positions_summary.setText(f"Positions {len(positions)}")
        self.orders_summary.setText(f"Orders {len(orders)}")
        self.positions_summary.setAccessibleName(f"View {len(positions)} open positions")
        self.orders_summary.setAccessibleName(f"View {len(orders)} active orders")

    def _position_card(self, payload):
        card = PositionDeskRow(payload, "")
        card.layout().setContentsMargins(0, 6, 6, 6)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.selected_requested.connect(self._select_position_card)
        return card

    @QtCore.Slot()
    def _select_position_card(self):
        card = self.sender()
        if isinstance(card, PositionDeskRow):
            self.position_requested.emit(dict(card.payload))

    def _activate_position(self, item):
        payload = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(payload, dict):
            self.position_requested.emit(dict(payload))

    def set_mark_price(self, mark, symbol):
        for index in range(self.positions.count()):
            item = self.positions.item(index)
            card = self.positions.itemWidget(item)
            if isinstance(card, PositionDeskRow) and card.payload.get("symbol") == symbol:
                card.update_mark_price(mark)
                item.setData(Qt.ItemDataRole.UserRole, dict(card.payload))


class TradingWorkspace(QtWidgets.QWidget):
    order_requested = Signal(object)
    batch_orders_requested = Signal(object)
    position_trade_requested = Signal(object)
    position_selection_changed = Signal(object)
    close_presets_changed = Signal(object)
    view_changed = Signal(int)

    def __init__(
        self,
        theme: dict[str, str],
        gateway: TradingGatewayPort,
        parent: QtWidgets.QWidget | None = None,
        symbol_rules: dict[str, SymbolRules] | None = None,
    ):
        super().__init__(parent)
        self.theme = ui_palette(theme)
        self.gateway = gateway
        self.symbol_rules = symbol_rules if symbol_rules is not None else {}
        self.symbol = DEFAULT_SYMBOL
        self.rules = SymbolRules()
        self._account_view_open = False
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
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setObjectName("tradingWorkspace")
        header = QtWidgets.QFrame()
        header.setObjectName("tradingWorkspaceHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(12, 4, 12, 4)
        header_layout.setSpacing(6)
        self.symbol_label = ElidedLabel(self.symbol)
        set_text_role(self.symbol_label, TextRole.INSTRUMENT_SYMBOL)
        header_layout.addWidget(self.symbol_label, 1)
        self.view_tabs = QtWidgets.QTabBar()
        self.view_tabs.setObjectName("tradingViewSwitch")
        self.view_tabs.setDrawBase(False)
        self.view_tabs.setExpanding(False)
        self.view_tabs.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.view_tabs.setAccessibleName("Trading panel view")
        set_text_role(self.view_tabs, TextRole.UI_CONTROL)
        self.view_tabs.setIconSize(QtCore.QSize(16, 16))
        self.view_tabs.addTab(asset_svg_icon("trading-account.svg", self.theme['muted'], self.devicePixelRatioF()), "")
        self.view_tabs.setTabToolTip(0, "Order entry")
        self.view_tabs.setTabWhatsThis(0, "Order entry and position controls")
        self.view_tabs.addTab(asset_svg_icon("trading-open-orders.svg", self.theme['muted'], self.devicePixelRatioF()), "")
        self.view_tabs.setTabToolTip(1, "Positions and balances")
        self.view_tabs.setTabWhatsThis(1, "Positions, working orders, fills and balances")
        self.refresh_button = QtWidgets.QPushButton()
        self.refresh_button.setObjectName("portfolioSyncButton")
        self.refresh_button.setIcon(line_icon("refresh", self.theme['muted'], self.devicePixelRatioF()))
        self.refresh_button.setIconSize(QtCore.QSize(18, 18))
        self.refresh_button.setFixedSize(28, 28)
        self.refresh_button.setAccessibleName("Update portfolio data")
        self.refresh_button.setToolTip("Update balances, positions and working orders")
        self.refresh_button.setAutoDefault(False)
        set_text_role(self.refresh_button, TextRole.UI_CONTROL)
        self.refresh_button.clicked.connect(
            lambda: self.gateway.refresh_account(self.symbol, True)
        )
        self.refresh_button.setEnabled(self.gateway.has_credentials())
        self.gateway.credentials_changed.connect(
            lambda _key: self.refresh_button.setEnabled(self.gateway.has_credentials())
        )
        header_layout.addWidget(self.refresh_button)
        header_layout.addWidget(self.view_tabs, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(header)
        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("tradingPages")
        layout.addWidget(self.pages, 1)
        self.ticket = OrderPanel(gateway, compact=True)
        self.ticket.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.ticket.order_requested.connect(self.order_requested)
        self.ticket.minimum_content_height_changed.connect(
            self._ticket_content_height_changed
        )
        self.pages.addWidget(self.ticket)


        self._workspace_header = header
        self._activity_dirty = True
        self._activity_layout_timer = QTimer(self)
        self._activity_layout_timer.setSingleShot(True)
        self._activity_layout_timer.timeout.connect(self._refresh_adaptive_activity)
        self.adaptive_activity_frame = TradingActivityPanel(self._activity_order_card)
        self.adaptive_activity_frame.account_requested.connect(self._open_adaptive_account_tab)
        self.adaptive_activity_frame.position_requested.connect(self._open_activity_position)
        self.adaptive_activity_frame.positions.presentation_changed.connect(
            lambda: self._activity_layout_timer.start(0))
        self.adaptive_activity_frame.orders.presentation_changed.connect(
            lambda: self._activity_layout_timer.start(0))
        self.adaptive_activity_frame.hide()
        layout.addWidget(self.adaptive_activity_frame, 1)

        self.account_frame = PositionDeskView(self._position_card, self._order_card)
        self.account_frame.tabs.setTabIcon(
            1, asset_svg_icon("trading-open-orders.svg", self.theme['muted'], self.devicePixelRatioF())
        )
        self.account_frame.symbol_rules = self.symbol_rules
        self.positions = self.account_frame.positions
        self.orders = self.account_frame.orders
        self.fills = self.account_frame.fills
        self.tabs = self.account_frame.tabs
        self.status = self.account_frame.status
        self.total_pnl = self.adaptive_activity_frame.total_pnl
        self.balances = self._table(("ASSET", "WALLET", "AVAILABLE", "UPNL"))
        self.tabs.addTab(self.balances, "Balances")
        self._position_count = self._order_count = 0
        self.cancel_all_button = self.account_frame.cancel_button
        self.order_actions = self.account_frame.more_button
        self.order_actions.show()
        self.batch_button = self.account_frame.more_menu.addAction("Limit batch…")
        self.modify_button = self.account_frame.more_menu.addAction("Edit selected limit…")
        self.batch_button.triggered.connect(self.open_batch_orders)
        self.modify_button.triggered.connect(self.modify_selected)
        self.ticket.reduce_only.toggled.connect(lambda _checked: self._sync_batch_action())
        self.cancel_all_button.clicked.connect(self.cancel_selected_symbol_orders)
        self.tabs.currentChanged.connect(self._account_tab_changed)
        self.orders.currentItemChanged.connect(lambda *_args: self._sync_account_actions(self.tabs.currentIndex()))
        self.account_frame.position_selected.connect(self._position_selected)
        self.ticket.position_target_selected.connect(self.select_position)
        self.account_frame.trade_requested.connect(self.position_trade_requested)
        self.account_frame.details.close_requested.connect(self._close_position_payload)
        self.account_frame.details.close_limit_requested.connect(self._close_position_limit_payload)
        _populate_account_cards(self.positions, [], None, "Connect API credentials to view positions")
        _populate_account_cards(self.orders, [], None, "Connect API credentials to view orders")
        _populate_fill_cards(self.fills, {}, self.symbol, connected=False)
        self.pages.addWidget(self.account_frame)
        self.view_tabs.currentChanged.connect(self.set_page)
        self.gateway.snapshot_ready.connect(self.apply_snapshot)
        self.gateway.credentials_changed.connect(self._clear_account_view)
        self.gateway.account_event.connect(self._account_event)
        self.gateway.state_changed.connect(self.status.setText)
        self.gateway.request_succeeded.connect(
            lambda _request, _result: self.account_refresh_timer.start()
        )
        self._sync_account_actions(self.tabs.currentIndex())
        typography_controller().changed.connect(self._sync_view_tabs_geometry)
        self.apply_theme(theme)

    def _sync_view_tabs_geometry(self) -> None:
        # The native scroll-button minimum can exceed the two tab widths,
        # leaving a blank tail that shifts Account away from the right inset.
        self.view_tabs.ensurePolished()
        self.view_tabs.setFixedSize(self.view_tabs.sizeHint())

    def minimumSizeHint(self) -> QtCore.QSize:
        # The visible activity area must never prevent the rail from shrinking.
        return QtCore.QSize(0, self._workspace_header.sizeHint().height() + 24)

    def _ticket_content_height_changed(self, _height: int) -> None:
        self._activity_layout_timer.start(0)

    def set_bottom_panel(self, _bottom: bool) -> None:
        # Activity follows available space in every rail placement.
        self._activity_layout_timer.start(0)

    def _refresh_adaptive_activity(self) -> None:
        if not self.isVisible():
            return
        if self._account_view_open:
            self.adaptive_activity_frame.hide()
            self.pages.setMinimumHeight(0)
            self.pages.setMaximumHeight(16_777_215)
            return
        self.ticket._desk_layout.activate()
        ticket_height = max(1, self.ticket.compact_required_height())
        available = max(0, self.contentsRect().height() - self._workspace_header.height())
        spare_height = max(0, available - ticket_height)
        activity = self.adaptive_activity_frame
        if spare_height < activity.summary_height():
            activity.hide()
            self.pages.setMinimumHeight(0)
            self.pages.setMaximumHeight(16_777_215)
            return
        if self._activity_dirty:
            activity.set_payloads(self._position_payloads, self._open_orders, self.symbol,
                                  self.gateway.has_credentials())
            self._activity_dirty = False
        activity.set_available_size(self.width(), spare_height)
        # Cap the stacked page, not the ticket: its hidden account page otherwise
        # keeps the larger size hint and leaves a gap above the activity panel.
        self.pages.setFixedHeight(ticket_height)
        activity.show()

    def _open_activity_position(self, payload) -> None:
        self.select_position(payload)
        self._open_adaptive_account_tab(0)

    def _open_adaptive_account_tab(self, tab_index: int) -> None:
        self.tabs.setCurrentIndex(tab_index)
        self.set_page(1)

    @staticmethod
    def _table(headers: tuple[str, ...]) -> QtWidgets.QTableWidget:
        table = AccountDataTable(headers)
        table.setObjectName("tradingDataTable")
        set_text_role(table, TextRole.TABLE_TEXT)
        table.setHorizontalHeaderLabels(headers)
        set_text_role(table.horizontalHeader(), TextRole.UI_CAPTION)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setMinimumSectionSize(40)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        return table

    def set_symbol(self, symbol: str, rules: SymbolRules) -> None:
        changed = symbol != self.symbol
        self.symbol = symbol
        self.rules = rules
        self.ticket.set_symbol(symbol, rules)
        self.symbol_label.setText(symbol)
        self.symbol_rules[symbol] = rules
        self._position_payloads.sort(key=lambda row: (row.get("symbol") != symbol, -_position_notional(row)))
        self._open_orders.sort(key=lambda row: (row.get("symbol") != symbol, str(row.get("symbol", ""))))
        self.account_frame.set_positions(self._position_payloads, symbol, self.gateway.has_credentials())
        self.account_frame.set_orders(self._open_orders, symbol, self.gateway.has_credentials())
        self._sync_account_actions(self.tabs.currentIndex())
        if changed:
            self.fills.set_payloads([], f"Loading fills for {symbol}…"
                                    if self.gateway.has_credentials() else "Connect API credentials to view fills")
        self.status.setText(f"{symbol} · ACCOUNT DATA")
        self._activity_dirty = True
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
        self.account_frame.set_mark_price(price, symbol)
        if self.adaptive_activity_frame.isVisible():
            self.adaptive_activity_frame.set_mark_price(price, symbol)
        elif changed:
            self._activity_dirty = True
        if changed:
            self._refresh_total_pnl()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._account_view_open and not self.account_age_timer.isActive():
            self.account_age_timer.start()
            self._refresh_account_freshness()
        QTimer.singleShot(0, self, self._poll_account_if_open)
        QTimer.singleShot(0, self, self._refresh_adaptive_activity)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.account_age_timer.stop()
        self.adaptive_activity_frame.hide()
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._activity_layout_timer.start(0)

    def set_page(self, index: int) -> None:
        self.set_account_view(int(index) == 1)

    def set_account_view(self, opened: bool) -> None:
        opened = bool(opened)
        changed = opened != self._account_view_open
        self._account_view_open = opened
        index = 1 if opened else 0
        blocker = QtCore.QSignalBlocker(self.view_tabs)
        self.view_tabs.setCurrentIndex(index)
        del blocker
        self.pages.setCurrentIndex(index)
        self._refresh_adaptive_activity()
        if opened and self.isVisible():
            self.account_age_timer.start()
            self._refresh_account_freshness()
            if changed:
                self._poll_account_if_open()
        else:
            self.account_age_timer.stop()
        if changed:
            self.view_changed.emit(index)

    def current_page(self) -> int:
        return 1 if self._account_view_open else 0

    def _poll_account_if_open(self) -> None:
        if self._account_view_open and self.isVisible() and self.gateway.has_credentials():
            self.gateway.refresh_account(
                self.symbol,
                True,
                poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                follow_up=False,
            )

    def _refresh_account_freshness(self) -> None:
        if not self._account_view_open:
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
        scope = f"{self.symbol} · FILLS · LOCAL TIME" if self.tabs.currentIndex() == 2 else "ALL SYMBOLS"
        self.status.setText(f"{scope} · {freshness}")

    def _account_tab_changed(self, tab_index: int) -> None:
        self._sync_account_actions(tab_index)
        self._refresh_account_freshness()
        if self._account_view_open and self.isVisible() and self.gateway.has_credentials():
            self.gateway.refresh_account(
                self.symbol,
                True,
                poll_minimum_interval=_ACCOUNT_POLL_MINIMUM_INTERVAL,
                follow_up=False,
            )

    def _sync_account_actions(self, tab_index: int) -> None:
        self.order_actions.setVisible(tab_index == 1)
        self.cancel_all_button.setVisible(tab_index == 1)
        connected = self.gateway.has_credentials()
        self.cancel_all_button.setEnabled(connected and any(str(row.get("symbol") or "").upper() == self.symbol.upper() for row in self._open_orders))
        self.cancel_all_button.setToolTip(f"Cancel all working orders for {self.symbol} only")
        selected = self._selected_payload(self.orders)
        self.modify_button.setEnabled(connected and bool(selected) and selected.get("_source") == "STANDARD" and selected.get("type") == "LIMIT")
        self._sync_batch_action()

    def _sync_batch_action(self) -> None:
        reducing = bool(self.ticket.reduce_only.isChecked())
        self.batch_button.setEnabled(not reducing and self.gateway.has_credentials())
        self.batch_button.setToolTip(
            'Turn off Reduce only to create batch entries.'
            if reducing
            else 'Create up to five Limit / GTC entries for this side.'
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
        self.account_frame.details.set_close_percentages(self.close_percentages)
        self.close_presets_changed.emit(list(self.close_percentages))

    @staticmethod
    def _fill_table(
        table: QtWidgets.QTableWidget,
        rows: list[tuple[tuple[str, ...], dict[str, Any]]],
    ) -> None:
        fingerprint = tuple((_table_record_key(payload), values) for values, payload in rows)
        if (
            getattr(table, "_rows_fingerprint", None) == fingerprint
            and table.rowCount() == len(rows)
        ):
            by_key = {_table_record_key(payload): payload for _values, payload in rows}
            for row_index in range(table.rowCount()):
                item = table.item(row_index, 0)
                if item is not None:
                    key = _table_record_key(item.data(Qt.ItemDataRole.UserRole) or {})
                    if key in by_key:
                        item.setData(Qt.ItemDataRole.UserRole, by_key[key])
            return
        previous = table.item(table.currentRow(), 0)
        selected_key = _table_record_key(previous.data(Qt.ItemDataRole.UserRole) or {}) if previous else None
        scroll = table.verticalScrollBar().value()
        updates_enabled = table.updatesEnabled()
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        try:
            table.setRowCount(len(rows))
            for row_index, (values, payload) in enumerate(rows):
                for column, value in enumerate(values):
                    item = table.item(row_index, column)
                    header_item = table.horizontalHeaderItem(column)
                    header = header_item.text().upper() if header_item is not None else ""
                    if item is None:
                        item = _AccountTableItem()
                        numeric = header in {"PRICE", "SIZE", "PNL", "FEE", "WALLET", "AVAILABLE", "UPNL"}
                        item.setFont(typography_font(TextRole.TABLE_VALUE if numeric else TextRole.TABLE_TEXT))
                        if numeric:
                            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        table.setItem(row_index, column, item)
                    item.setText(value)
                    item.setData(_ACCOUNT_SORT_ROLE, _account_sort_value(header, value, payload))
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                    item.setToolTip(value)
                table.setRowHeight(row_index, 25)
            table._rows_fingerprint = fingerprint
        finally:
            table.setSortingEnabled(True)
            table.setUpdatesEnabled(updates_enabled)
        for row_index in range(table.rowCount()):
            if _table_record_key(table.item(row_index, 0).data(Qt.ItemDataRole.UserRole) or {}) == selected_key:
                table.selectRow(row_index)
                break
        table.verticalScrollBar().setValue(scroll)


    def _clear_account_view(self, _key):
        self._open_orders = []
        self._last_account_snapshot_mono = 0.0
        self.apply_snapshot(
            {"account": {}, "ordersScope": "ALL", "orders": [], "algoOrders": [], "fills": []},
            mark_fresh=False,
        )

    def apply_snapshot(self, snapshot: dict[str, Any], *, mark_fresh: bool = True) -> None:
        if mark_fresh and self.gateway.has_credentials():
            self._last_account_snapshot_mono = safe_float(snapshot.get('_read_started_mono'), time.monotonic())
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
        self.account_frame.set_positions(position_payloads, self.symbol, self.gateway.has_credentials())

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
            self.account_frame.set_orders(combined_orders, self.symbol, self.gateway.has_credentials())

        if "fills" in snapshot and snapshot.get("fillsSymbol", self.symbol) == self.symbol:
            _populate_fill_cards(self.fills, snapshot, self.symbol, connected=self.gateway.has_credentials())

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
        self._sync_account_actions(self.tabs.currentIndex())
        self._account_status_base = (
            f"ACCOUNT · {self._position_count} POSITION"
            f"{'S' if self._position_count != 1 else ''} · "
            f"{self._order_count} OPEN ORDER"
            f"{'S' if self._order_count != 1 else ''}"
        )
        self._activity_dirty = True
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
        connected = self.gateway.has_credentials() and self._last_account_snapshot_mono > 0
        _set_text_if_changed(self.total_pnl, f"UPNL {_signed_money(total_pnl)}" if connected else "UPNL —")
        _set_repolished_property(self.total_pnl, "pnl", _pnl_state(total_pnl))

    def _position_card(self, payload):
        return PositionDeskRow(payload, self.symbol)

    def _order_card(self, payload):
        card = WorkingOrderRow(payload, self.theme, self.symbol)
        card.cancel_requested.connect(self._cancel_order_payload)
        return card

    def _activity_order_card(self, payload):
        card = WorkingOrderRow(payload, self.theme, self.symbol, compact=True)
        card.cancel_requested.connect(self._cancel_order_payload)
        return card

    def _position_selected(self, payload):
        if isinstance(payload, dict):
            self.ticket.focus_position(payload)
            self.position_selection_changed.emit(dict(payload))

    def select_position(self, payload):
        self.account_frame.select_position(payload)

    def _selected_payload(
        self,
        view: QtWidgets.QTableWidget | QtWidgets.QListView,
    ) -> dict[str, Any] | None:
        if isinstance(view, QtWidgets.QListView):
            payload = view.currentIndex().data(Qt.ItemDataRole.UserRole)
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


    def cancel_selected_symbol_orders(self) -> None:
        symbol = self.symbol
        if _confirm_cancel_symbol_orders(self, symbol):
            self.gateway.cancel_all(symbol)

    def open_batch_orders(self) -> None:
        if self.ticket.reduce_only.isChecked():
            self.status.setText("BATCH LIMITS ARE ENTRY ORDERS · UNCHECK REDUCE ONLY")
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
                str(abs(Decimal(str(position.get('positionAmt')))) * Decimal(percent) / 100), rules.market_step, offset=str(rules.min_market_qty)
            )
        except ValueError as exc:
            self.status.setText(f"CLOSE NOT SENT · {exc}")
            return
        quantity_value = safe_float(quantity)
        if not (rules.min_market_qty <= quantity_value <= rules.max_market_qty):
            self.status.setText("CLOSE SIZE IS OUTSIDE THE EXCHANGE MARKET-ORDER RANGE")
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
        try:
            quantity = quantize_step(
                str(abs(Decimal(str(position.get('positionAmt')))) * Decimal(percent) / 100), rules.lot_step, offset=str(rules.min_qty)
            )
            quantity = _validated_limit_quantity(quantity, rules, "Close quantity")
        except ValueError as exc:
            self.status.setText(f"CLOSE NOT SENT · {exc}")
            return
        dialog = CloseLimitDialog(position, percent, self, rules=rules)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            price = dialog.validated_price()
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


    def _account_event(self, _event: dict[str, Any]) -> None:
        self.account_refresh_timer.start()

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = ui_palette(theme)
        self.setStyleSheet(_trading_stylesheet(self.theme))
        self.ticket.apply_theme(self.theme)
        self.fills.apply_theme(self.theme)
        ratio = self.devicePixelRatioF()
        self.refresh_button.setIcon(line_icon('refresh', self.theme['muted'], ratio))
        self.view_tabs.setTabIcon(
            0, asset_svg_icon("trading-account.svg", self.theme['muted'], ratio)
        )
        self.view_tabs.setTabIcon(
            1, asset_svg_icon("trading-open-orders.svg", self.theme['muted'], ratio)
        )
        self.tabs.setTabIcon(
            1, asset_svg_icon("trading-open-orders.svg", self.theme['muted'], ratio)
        )
        QTimer.singleShot(0, self, self._sync_view_tabs_geometry)

