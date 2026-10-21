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
        QWidget#responsiveOrderTicket, QWidget#tradingWorkspace, QFrame#ticketBody,
        QWidget#accountActivityPanel, QWidget#positionDeskBody,
        QTabWidget#tradingDestinations, QTabWidget#tradingAccountTabs,
        QTabWidget#tradingAccountTabs > QStackedWidget,
        QTabWidget#tradingAccountTabs QTabBar,
        QFrame#positionDeskView, QScrollArea#positionDeskScroll,
        QScrollArea#deskTicketScroll {{ background: {t['panel']}; border: 0; }}
        QToolButton#ticketConditionalType[active="true"] {{ color: {t['cyan']}; border-color: {t['cyan']}; }}
        QSlider#ticketAllocation {{ background: {t['panel']}; border: 0; }}
        QScrollArea#deskTicketScroll > QWidget, QScrollArea#positionDeskScroll > QWidget {{ background: {t['panel']}; border: 0; }}
        QFrame#ticketFeedback {{ background: {tint('amber', 15)}; border: 1px solid {tint('amber', 65)}; border-radius: 6px; }}
        QLabel {{ background: transparent; border: 0; padding: 0; color: {t['text']}; }}
        QLabel#accountCardDetail, QLabel#ticketFieldCaption, QLabel#ticketContext {{ color: {t['muted']}; }}
        QWidget#tradingInlineField, QFrame#ticketField, QFrame#tradingPanelCard,
        QFrame#tradingMarginRow {{ background: transparent; border: 0; }}
        QLineEdit, QComboBox, QDoubleSpinBox {{
            background: {t['control']}; color: {t['text']}; border: 1px solid {t['control_border']};
            border-radius: 5px; min-width: 0; min-height: 0; padding: 5px 10px;
            selection-background-color: {t['control_hover']};
        }}
        QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover {{ border-color: {t['muted']}; }}
        QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border-color: {t['cyan']}; background: {t['panel2']}; }}
        QLineEdit:disabled, QComboBox:disabled {{ color: {t['muted']}; border-color: {t['border']}; }}
        QFrame#ticketInputGroup {{ background: {t['control']}; border: 1px solid {t['control_border']}; border-radius: 5px; }}
        QFrame#ticketInputGroup:hover {{ border-color: {t['muted']}; }}
        QFrame#ticketInputGroup[focused="true"] {{ border-color: {t['cyan']}; }}
        QFrame#ticketInputGroup QLineEdit, QFrame#ticketInputGroup QComboBox {{ background: transparent; border: 0; border-radius: 0; }}
        QFrame#ticketInputGroup QComboBox {{ color: {t['muted']}; border-left: 1px solid {t['control_border']}; }}
        QToolButton#tradeMarkButton {{ background: transparent; color: {t['muted']}; border: 0; border-left: 1px solid {t['control_border']}; border-radius: 0; padding: 0 6px; }}
        QToolButton#tradeMarkButton:hover, QToolButton#tradeMarkButton:focus {{ color: {t['text']}; background: {t['control_hover']}; }}
        QToolButton#tradeMarkButton:disabled {{ color: {t['muted']}; background: transparent; }}
        QComboBox {{ padding-left: 10px; padding-right: 26px; }}
        QWidget#responsiveOrderTicket QComboBox {{ padding-right: 26px; }}
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
        QToolButton#tradeSettingsMini {{ border: 0; background: transparent; padding: 0; margin: 0; min-height: 0; }}
        QToolButton#tradeSettingsMini:hover, QToolButton#tradeSettingsMini:focus {{ background: {t['control_hover']}; }}
        QComboBox#leverageDropdown, QComboBox#timeInForceCycle, QPushButton#protectionButton {{
            background: transparent; color: {t['muted']}; border: 1px solid {t['border']};
            border-radius: 4px; padding: 3px 8px;
        }}
        QComboBox#leverageDropdown {{ color: {t['text']}; }}
        QComboBox#leverageDropdown:hover, QComboBox#timeInForceCycle:hover,
        QPushButton#protectionButton:hover {{ background: {t['control_hover']}; color: {t['text']}; }}
        QLabel#ticketAvailableBalance, QLabel#ticketEstimateValue {{ color: {t['text']}; }}
        QPushButton#buySideButton:disabled, QPushButton#sellSideButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; background: {t['control']}; }}
        QPushButton#protectionButton[active="true"] {{ color: {t['cyan']}; border-color: {t['cyan']}; }}
        QPushButton#portfolioSyncButton {{ background: transparent; border: 0; padding: 0; min-width: 28px; max-width: 28px; min-height: 26px; }}
        QPushButton#portfolioSyncButton:hover, QPushButton#portfolioSyncButton:focus {{ background: {t['control_hover']}; }}
        QCheckBox#ticketReduceOnly {{ background: transparent; color: {t['muted']}; spacing: 7px; padding: 3px 0; }}
        QCheckBox#ticketReduceOnly:checked {{ color: {t['text']}; }}
        QCheckBox#ticketReduceOnly::indicator {{ width: 13px; height: 13px; background: {t['control']}; border: 1px solid {t['control_border']}; border-radius: 3px; }}
        QCheckBox#ticketReduceOnly::indicator:checked {{ background: {t['cyan']}; border-color: {t['cyan']}; }}
        QCheckBox#ticketReduceOnly::indicator:focus {{ border-color: {t['text']}; }}
        QTabWidget#tradingAccountTabs QTabBar::tab {{
            background: transparent; color: {t['muted']}; border: 0;
            border-bottom: 2px solid transparent; padding: 7px 8px;
        }}
        QTabWidget#tradingAccountTabs QTabBar::tab:!selected {{ border: 0; border-bottom: 2px solid transparent; }}
        QTabWidget#tradingAccountTabs QTabBar::tab:selected {{ color: {t['text']}; border: 0; border-bottom: 2px solid {t['cyan']}; }}
        QTabBar::tab:hover {{ color: {t['text']}; }}
        QTabWidget#tradingDestinations QTabBar QToolButton,
        QTabWidget#tradingAccountTabs QTabBar QToolButton {{
            padding: 0; min-width: 0; min-height: 0; border-radius: 0;
        }}
        QTabWidget#tradingAccountTabs::pane {{ border: 0; background: {t['panel']}; }}
        QTabWidget#tradingDestinations QTabBar {{ background: {t['panel']}; border: 0; }}
        QTabWidget#tradingDestinations QTabBar::tab {{
            background: transparent; color: {t['muted']}; border: 0;
            border-bottom: 2px solid transparent; border-radius: 0; margin: 0;
            padding: 8px 13px; min-width: 20px; min-height: 18px;
        }}
        QTabWidget#tradingDestinations QTabBar::tab:selected {{
            color: {t['text']}; background: transparent;
            border: 0; border-bottom: 2px solid {t['cyan']};
        }}
        QTabWidget#tradingDestinations QTabBar::tab:hover {{ background: {t['control_hover']}; }}
        QTabWidget#tradingDestinations::pane {{ border: 0; border-top: 1px solid {t['border']}; background: {t['panel']}; }}
        QWidget#tradingDestinations > QStackedWidget {{ background: {t['panel']}; border: 0; }}
        QSlider#ticketAllocation::groove:horizontal {{ height: 3px; background: {t['border']}; border-radius: 1px; }}
        QSlider#ticketAllocation::sub-page:horizontal {{ background: {t['cyan']}; border-radius: 2px; }}
        QSlider#ticketAllocation::handle:horizontal {{ width: 10px; height: 10px; margin: -5px 0; background: {t['text']}; border: 2px solid {t['panel']}; border-radius: 6px; }}
        QSlider#ticketAllocation:focus::handle:horizontal {{ border-color: {t['cyan']}; }}
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
        QFrame#positionDeskRow[selected="true"], QFrame#workingOrderRow[selected="true"] {{ background: {t['panel2']}; border-color: {t['muted']}; }}
        QFrame#positionDeskDetails {{ background: transparent; border: 0; border-top: 1px solid {t['border']}; border-radius: 0; }}
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
        QWidget#ticketTypeStrip {{ background: {t['control']}; border: 1px solid {t['border']}; border-radius: 5px; }}
        QPushButton#ticketOrderType, QToolButton#ticketConditionalType {{
            color: {t['muted']}; background: transparent; border: 0;
            border-bottom: 2px solid transparent; border-radius: 3px; padding: 4px 8px;
        }}
        QToolButton#ticketConditionalType {{ padding-right: 24px; }}
        QPushButton#ticketOrderType:checked, QToolButton#ticketConditionalType[active="true"] {{
            color: {t['text']}; background: {t['control_hover']}; border: 0; border-bottom: 2px solid {t['cyan']};
        }}
        QPushButton#ticketOrderType:hover, QToolButton#ticketConditionalType:hover {{ color: {t['text']}; background: {t['control_hover']}; }}
        QPushButton#ticketOrderType:focus, QToolButton#ticketConditionalType:focus {{ border-bottom-color: {t['cyan']}; color: {t['text']}; }}
        QPushButton#ticketAllocationPreset {{ background: {t['control']}; color: {t['muted']}; padding: 2px 4px; border: 1px solid {t['border']}; }}
        QPushButton#ticketAllocationPreset:hover, QPushButton#ticketAllocationPreset:focus {{ color: {t['text']}; border-color: {t['cyan']}; }}
        QPushButton#ticketAllocationPreset:checked {{ color: {t['cyan']}; border-color: {t['cyan']}; background: {t['control_hover']}; }}
        QFrame#ticketReview {{ background: transparent; border: 0; }}
        QFrame#ticketEstimateBox {{ background: transparent; border: 0; border-top: 1px solid {t['border']}; border-radius: 0; }}
        QFrame#ticketExecutionOptions {{ background: transparent; border: 0; }}
        QWidget#ticketSubmitFooter {{ background: {t['panel']}; border: 0; }}
        QPushButton#buySideButton {{ color: {t['green_text']}; border-color: {tint('green', 75)}; background: {tint('green', 42)}; border-radius: 7px; }}
        QPushButton#sellSideButton {{ color: {t['red_text']}; border-color: {tint('red', 75)}; background: {tint('red', 42)}; border-radius: 7px; }}
        QPushButton#buySideButton:hover, QPushButton#buySideButton:pressed {{ color: {t['text']}; background: {tint('green', 72)}; }}
        QPushButton#sellSideButton:hover, QPushButton#sellSideButton:pressed {{ color: {t['text']}; background: {tint('red', 72)}; }}
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


class _TradingTooltipFilter(QtCore.QObject):
    def attach(self, root: QtCore.QObject) -> None:
        root.installEventFilter(self)
        if isinstance(root, QtWidgets.QWidget) and root.toolTip():
            root.setToolTip("")
        for child in root.children():
            self.attach(child)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        kind = event.type()
        if kind == QtCore.QEvent.Type.ToolTip:
            event.accept()
            return True
        if kind == QtCore.QEvent.Type.ToolTipChange:
            if isinstance(watched, QtWidgets.QWidget) and watched.toolTip():
                watched.setToolTip("")
        elif kind == QtCore.QEvent.Type.ChildAdded:
            self.attach(event.child())
        return False


def _disable_trading_tooltips(root: QtWidgets.QWidget) -> None:
    root._trading_tooltip_filter = _TradingTooltipFilter(root)
    root._trading_tooltip_filter.attach(root)


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
        _disable_trading_tooltips(self)
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
        _disable_trading_tooltips(self)
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
        _disable_trading_tooltips(self)
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
        self.leverage = QtWidgets.QSpinBox()
        self.leverage.setRange(1, 125)
        self.leverage.setSuffix("×")
        self.leverage.setValue(int(safe_float(merged["leverage"])))
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
            button.clicked.connect(self._sync_quick_controls)
            self.quick_leverage_buttons.append(button)
            leverage_layout.addWidget(button)
        self.order_mode = QtWidgets.QComboBox()
        self.order_mode.addItems(("MARKET", "LIMIT"))
        self.order_mode.setCurrentText(str(merged["order_mode"]).upper())
        self.time_in_force = QtWidgets.QComboBox()
        self.time_in_force.addItem("IOC", "IOC")
        self.time_in_force.addItem("GTC", "GTC")
        self.time_in_force.addItem("FOK", "FOK")
        self.time_in_force.addItem("GTX · POST-ONLY", "GTX")
        self.time_in_force.setCurrentIndex(
            max(0, self.time_in_force.findData(str(merged["time_in_force"]).upper()))
        )
        self.slippage_enabled = QtWidgets.QCheckBox("Cap market slippage")
        self.slippage_enabled.setChecked(bool(merged["slippage_enabled"]))
        self.max_slippage = QtWidgets.QDoubleSpinBox()
        self.max_slippage.setRange(0.01, 10.0)
        self.max_slippage.setDecimals(2)
        self.max_slippage.setSuffix("%")
        self.max_slippage.setValue(safe_float(merged["max_slippage_percent"]))
        slippage_box = QtWidgets.QWidget()
        slippage_layout = QtWidgets.QHBoxLayout(slippage_box)
        slippage_layout.setContentsMargins(0, 0, 0, 0)
        slippage_layout.addWidget(self.slippage_enabled)
        slippage_layout.addWidget(self.max_slippage)
        self.reduce_only = QtWidgets.QCheckBox("Reduce existing position only")
        self.reduce_only.setChecked(bool(merged["reduce_only"]))

        def add_row(label_text: str, field: QtWidgets.QWidget, tooltip: str) -> None:
            label = QtWidgets.QLabel(label_text)
            form.addRow(label, field)

        add_row("Margin allocation", self.collateral_percent, self.collateral_percent.toolTip())
        add_row("Leverage", leverage_box, self.leverage.toolTip())
        add_row("Execution", self.order_mode, self.order_mode.toolTip())
        add_row("Time in force", self.time_in_force, self.time_in_force.toolTip())
        add_row("Slippage cap", slippage_box, self.slippage_enabled.toolTip())
        add_row("Intent", self.reduce_only, self.reduce_only.toolTip())

        self.take_profit_enabled = QtWidgets.QCheckBox("Enabled")
        self.take_profit_enabled.setChecked(bool(merged["take_profit_enabled"]))
        self.take_profit_percent = QtWidgets.QDoubleSpinBox()
        self.take_profit_percent.setRange(0.01, 100.0)
        self.take_profit_percent.setDecimals(2)
        self.take_profit_percent.setSuffix("% move")
        self.take_profit_percent.setValue(safe_float(merged["take_profit_percent"]))
        self.take_profit_close = QtWidgets.QSpinBox()
        self.take_profit_close.setRange(1, 100)
        self.take_profit_close.setSuffix("% close")
        self.take_profit_close.setValue(int(safe_float(merged["take_profit_close_percent"])))
        tp_box = QtWidgets.QWidget()
        tp_layout = QtWidgets.QHBoxLayout(tp_box)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.addWidget(self.take_profit_enabled)
        tp_layout.addWidget(self.take_profit_percent)
        tp_layout.addWidget(self.take_profit_close)
        add_row("Take profit", tp_box, "Optional reduce-only take-profit placed after an OPEN shortcut fills.")

        self.stop_loss_enabled = QtWidgets.QCheckBox("Enabled")
        self.stop_loss_enabled.setChecked(bool(merged["stop_loss_enabled"]))
        self.stop_loss_percent = QtWidgets.QDoubleSpinBox()
        self.stop_loss_percent.setRange(0.01, 100.0)
        self.stop_loss_percent.setDecimals(2)
        self.stop_loss_percent.setSuffix("% move")
        self.stop_loss_percent.setValue(safe_float(merged["stop_loss_percent"]))
        self.stop_loss_close = QtWidgets.QSpinBox()
        self.stop_loss_close.setRange(1, 100)
        self.stop_loss_close.setSuffix("% close")
        self.stop_loss_close.setValue(int(safe_float(merged["stop_loss_close_percent"])))
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
        _disable_trading_tooltips(self)
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
    """Keep the quote currency visible without mixing it into the price value."""

    def __init__(self, label: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setAccessibleName(label)
        self.setPlaceholderText("0.00")
        self.unit_label = QtWidgets.QLabel("USDT", self)
        self.unit_label.setObjectName("ticketFieldCaption")
        for decoration in (self.unit_label,):
            decoration.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            set_text_role(decoration, TextRole.UI_CAPTION)
            decoration.installEventFilter(self)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._sync_unit()

    def set_quote_asset(self, asset: str) -> None:
        self.unit_label.setText(asset)
        self._sync_unit()

    def _sync_unit(self) -> None:
        metrics = self.unit_label.fontMetrics()
        width = metrics.horizontalAdvance(self.unit_label.text())
        self.setTextMargins(0, 0, width + 8, 0)
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
        if watched is self.unit_label and event.type() == QtCore.QEvent.Type.FontChange:
            QTimer.singleShot(0, self, self._sync_unit)
        return super().eventFilter(watched, event)


class TicketInputGroup(QtWidgets.QFrame):
    """One input surface for an editor and its unit or price shortcut."""

    def __init__(self, editor: QtWidgets.QWidget, accessory: QtWidgets.QWidget):
        super().__init__()
        self.editor = editor
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
    """Direct pointer tracking; clicking the track must not also page-step."""

    def _position_value(self, position: QtCore.QPointF) -> int:
        option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                       QtWidgets.QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                       QtWidgets.QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        pixel = round(position.x()) - groove.left() - handle.width() // 2
        return QtWidgets.QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pixel, span, option.upsideDown)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.setSliderDown(True)
            self.setSliderPosition(self._position_value(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.isSliderDown():
            self.setSliderPosition(self._position_value(event.position()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderPosition(self._position_value(event.position()))
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class TicketAllocationControl(QtWidgets.QFrame):
    """Continuous allocation with exact, keyboard-accessible percentage shortcuts."""

    percentage_changed = Signal(int)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        row = QtWidgets.QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.slider = TicketPercentageSlider(Qt.Orientation.Horizontal, self)
        self.slider.setObjectName("ticketAllocation")
        self.slider.setRange(0, 100)
        self.slider.setPageStep(10)
        self.slider.setTracking(True)
        self.slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.slider.setAccessibleName("Available margin percentage")
        self.slider.valueChanged.connect(self.percentage_changed)
        row.addWidget(self.slider)
        shortcuts = QtWidgets.QHBoxLayout()
        shortcuts.setSpacing(6)
        self.preset_buttons = []
        for percent in (25, 50, 75, 100):
            button = QtWidgets.QPushButton(f"{percent}%")
            button.setObjectName("ticketAllocationPreset")
            button.setAutoDefault(False)
            button.setCheckable(True)
            button.setAccessibleName(f"Allocate {percent} percent")
            set_text_role(button, TextRole.UI_CAPTION)
            # Emit even when the slider already has this value: a manual amount
            # edit may have taken over since the previous allocation.
            button.clicked.connect(lambda _checked=False, value=percent:
                                   self.percentage_changed.emit(value))
            shortcuts.addWidget(button, 1)
            self.preset_buttons.append(button)
        row.addLayout(shortcuts)
        self.sync_geometry()

    def sync_geometry(self) -> None:
        self.slider.setFixedHeight(max(18, self.slider.fontMetrics().height() + 2))
        for button in self.preset_buttons:
            button.setFixedHeight(max(22, button.fontMetrics().height() + 6))


class TicketScrollArea(QtWidgets.QScrollArea):
    """Follow the form's natural height; give up space only in short panels."""

    def sizeHint(self) -> QtCore.QSize:
        body = self.widget()
        return body.layout().sizeHint() if body is not None else QtCore.QSize(0, 24)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 24)


class TicketOrderTypeButton(QtWidgets.QToolButton):
    """Keep the conditional-order menu discoverable under the shell stylesheet."""

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        _paint_trade_arrow(self, painter,
                           QtCore.QRect(self.width() - 16, (self.height() - 8) // 2, 8, 8))


class CompactTradeComboBox(QtWidgets.QComboBox):
    """Keyboard-friendly selectors with a native dropdown affordance."""

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

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        _paint_trade_arrow(self, painter,
                           QtCore.QRect(self.width() - 17, (self.height() - 8) // 2, 8, 8))

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

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
        _disable_trading_tooltips(self)
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
        """A single compact form; only its input area scrolls in short docks."""
        def label(text, name="ticketFieldCaption", *, elided=False):
            widget = ElidedLabel(text) if elided else QtWidgets.QLabel(text)
            widget.setObjectName(name)
            widget.setTextFormat(Qt.TextFormat.PlainText)
            set_text_role(widget, TextRole.UI_CAPTION)
            return widget

        def combo(items, name="tradeTicketPrimaryCombo"):
            widget = CompactTradeComboBox()
            widget.setObjectName(name)
            widget.setMinimumWidth(0)
            widget.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            widget.setMinimumContentsLength(0)
            set_text_role(widget, TextRole.UI_CONTROL)
            for title, value in items:
                widget.addItem(title, value)
            return widget

        def frame(name, margins=(0, 0, 0, 0), spacing=8):
            widget = QtWidgets.QFrame()
            widget.setObjectName(name)
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Preferred)
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
        self.ticket_scroll.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                         QtWidgets.QSizePolicy.Policy.Preferred)
        self.ticket_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.ticket_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ticket_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ticket_scroll.setMinimumSize(0, 24)
        self._ticket_body, self._desk_layout = frame("ticketBody", (12, 10, 12, 10))
        content = self._desk_layout
        self.ticket_scroll.setWidget(self._ticket_body)
        root.addWidget(self.ticket_scroll)

        self._ticket_header = QtWidgets.QWidget()
        self._ticket_header.setObjectName("tradingInlineField")
        header = QtWidgets.QHBoxLayout(self._ticket_header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.account_summary = label("Avail. — USDT", "ticketAvailableBalance", elided=True)
        self.account_summary.setAccessibleName("Available margin")
        header.addWidget(self.account_summary, 1)
        self.leverage_field = QtWidgets.QWidget()
        self.leverage_field.setObjectName("tradingInlineField")
        leverage_row = QtWidgets.QHBoxLayout(self.leverage_field)
        leverage_row.setContentsMargins(0, 0, 0, 0)
        self.leverage = LeverageComboBox()
        self.leverage.setAccessibleName("Cross margin leverage")
        set_text_role(self.leverage, TextRole.UI_CONTROL)
        leverage_row.addWidget(self.leverage)
        header.addWidget(self.leverage_field)
        self.quick_settings_button = QtWidgets.QToolButton()
        self.quick_settings_button.setObjectName("tradeSettingsMini")
        self.quick_settings_button.setFixedSize(28, 28)
        self.quick_settings_button.setIconSize(QtCore.QSize(16, 16))
        self.quick_settings_button.setAccessibleName("Trading settings")
        header.addWidget(self.quick_settings_button)
        content.addWidget(self._ticket_header)

        # Keep the public selectors used by hotkeys and chart entry. Their
        # visible counterparts share exactly the same state and order builder.
        self.position_side = combo(((side, side) for side in ("BOTH", "LONG", "SHORT")))
        self.position_side.setParent(self)
        self.position_side.hide()
        self.type_combo = combo(self.ORDER_TYPES)
        self.type_combo.setCurrentIndex(self.type_combo.findData("MARKET"))
        self.type_combo.setParent(self)
        self.type_combo.hide()
        self.order_type_row = QtWidgets.QWidget()
        self.order_type_row.setObjectName("ticketTypeStrip")
        types = QtWidgets.QGridLayout(self.order_type_row)
        types.setContentsMargins(2, 2, 2, 2)
        types.setSpacing(2)
        self.order_type_group = QtWidgets.QButtonGroup(self)
        self.order_type_buttons = {}
        for column, (title, kind) in enumerate((("Market", "MARKET"), ("Limit", "LIMIT"))):
            button = QtWidgets.QPushButton(title)
            button.setObjectName("ticketOrderType")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setAccessibleName(f"{title} order")
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            set_text_role(button, TextRole.UI_CONTROL)
            button.clicked.connect(lambda _checked=False, value=kind:
                                   self.type_combo.setCurrentIndex(self.type_combo.findData(value)))
            self.order_type_group.addButton(button)
            self.order_type_buttons[kind] = button
            types.addWidget(button, 0, column)
        self.conditional_button = TicketOrderTypeButton()
        self.conditional_button.setObjectName("ticketConditionalType")
        self.conditional_button.setText("Stop")
        self.conditional_button.setCheckable(True)
        self.conditional_button.setAccessibleName("Stop order type")
        self.conditional_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.conditional_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                              QtWidgets.QSizePolicy.Policy.Fixed)
        set_text_role(self.conditional_button, TextRole.UI_CONTROL)
        self.order_type_group.addButton(self.conditional_button)
        conditional_menu = QtWidgets.QMenu(self.conditional_button)
        self.conditional_actions = {}
        for title, kind in self.ORDER_TYPES:
            if kind in CONDITIONAL_ORDER_TYPES:
                action = conditional_menu.addAction(title)
                action.setCheckable(True)
                action.triggered.connect(lambda _checked=False, value=kind:
                                         self.type_combo.setCurrentIndex(self.type_combo.findData(value)))
                self.conditional_actions[kind] = action
        self.conditional_button.setMenu(conditional_menu)
        types.addWidget(self.conditional_button, 0, 2)
        content.addWidget(self.order_type_row)

        self._order_card, order_layout = frame("tradingPanelCard", spacing=0)
        self._order_form = QtWidgets.QGridLayout()
        self._order_form.setContentsMargins(0, 0, 0, 0)
        self._order_form.setHorizontalSpacing(10)
        self._order_form.setVerticalSpacing(8)
        order_layout.addLayout(self._order_form)
        content.addWidget(self._order_card)
        self.size_mode = combo(self.OPEN_SIZE_MODES)
        self.size_mode.setAccessibleName("Amount unit or sizing mode")
        self.quantity_edit = QtWidgets.QLineEdit()
        self.quantity_edit.setPlaceholderText("0")
        self.price_edit = TicketPriceEdit("Limit price")
        self.trigger_edit = TicketPriceEdit("Trigger price")
        self.activation_edit = TicketPriceEdit("Activation price")
        self.activation_edit.setPlaceholderText("Optional")
        for editor in (self.quantity_edit, self.price_edit, self.trigger_edit, self.activation_edit):
            set_text_role(editor, TextRole.TRADING_DESK_VALUE)
            editor.setMinimumWidth(0)
            editor.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.callback_rate = TradingRateSpinBox()
        self.callback_rate.setRange(0.1, 10)
        self.callback_rate.setDecimals(1)
        self.callback_rate.setSingleStep(0.1)
        self.callback_rate.setValue(0.5)
        self.callback_rate.setSuffix(" %")
        self.callback_rate.setAccessibleName("Trailing callback rate")
        set_text_role(self.callback_rate, TextRole.TRADING_DESK_VALUE)
        self.working_type = combo((("Mark price", "MARK_PRICE"), ("Last price", "CONTRACT_PRICE")))
        self.working_type.setAccessibleName("Trigger source")
        self.working_type.setParent(self._ticket_body)
        self.working_type.hide()

        def mark_field(edit):
            mark = QtWidgets.QToolButton()
            mark.setText("Mark")
            mark.setObjectName("tradeMarkButton")
            mark.setAccessibleName(f"Use mark price for {edit.accessibleName().lower()}")
            set_text_role(mark, TextRole.UI_CAPTION)
            return TicketInputGroup(edit, mark), mark

        price, self.price_mark_button = mark_field(self.price_edit)
        trigger, self.trigger_mark_button = mark_field(self.trigger_edit)
        activation, self.activation_mark_button = mark_field(self.activation_edit)
        amount = TicketInputGroup(self.quantity_edit, self.size_mode)
        self._field_rows = {}
        self._field_headers = {}
        for name, caption, control in (
            ("trigger", "Trigger price", trigger), ("price", "Limit price", price),
            ("activation", "Activation price", activation),
            ("callback", "Callback rate", self.callback_rate), ("amount", "Amount", amount),
        ):
            field, box = frame("ticketField", spacing=4)
            field.setParent(self._ticket_body)
            heading = QtWidgets.QGridLayout()
            heading.setContentsMargins(0, 0, 0, 0)
            heading.setHorizontalSpacing(8)
            heading.setVerticalSpacing(4)
            caption_label = label(caption)
            caption_label.setBuddy(control.editor if isinstance(control, TicketInputGroup) else control)
            heading.addWidget(caption_label, 0, 0)
            heading.setColumnStretch(0, 1)
            box.addLayout(heading)
            box.addWidget(control)
            self._field_headers[name] = heading
            self._field_rows[name] = field
            if name == "amount":
                self._amount_caption = caption_label
        content.addWidget(self._field_rows["amount"])
        self.margin_bar = TicketAllocationControl()
        self.margin_bar.setObjectName("tradingMarginRow")
        self.allocation_slider = self.margin_bar.slider
        self.margin_bar.percentage_changed.connect(
            lambda value: self._apply_size_preset(value, focus_amount=False))
        content.addWidget(self.margin_bar)

        self.options_bar = QtWidgets.QFrame()
        self.options_bar.setObjectName("ticketExecutionOptions")
        options = QtWidgets.QGridLayout(self.options_bar)
        options.setContentsMargins(0, 0, 0, 0)
        options.setHorizontalSpacing(6)
        options.setVerticalSpacing(6)
        self.reduce_only = QtWidgets.QCheckBox("Reduce only")
        self.reduce_only.setObjectName("ticketReduceOnly")
        set_text_role(self.reduce_only, TextRole.UI_CONTROL)
        self.time_in_force_field, tif = frame("ticketField", spacing=0)
        self.time_in_force = combo(((key, key) for key in _TIME_IN_FORCE_HINTS), "timeInForceCycle")
        self.time_in_force.setAccessibleName("Time in force")
        tif.addWidget(self.time_in_force)
        self.protection_field, protection = frame("ticketField", spacing=0)
        self.protection_button = QtWidgets.QPushButton("TP / SL")
        self.protection_button.setObjectName("protectionButton")
        self.protection_button.setProperty("active", False)
        self.protection_button.setAutoDefault(False)
        self.protection_button.setAccessibleName("Edit take-profit and stop-loss targets")
        set_text_role(self.protection_button, TextRole.UI_CONTROL)
        protection.addWidget(self.protection_button)
        for column, widget in enumerate((self.reduce_only, self.time_in_force_field, self.protection_field)):
            options.addWidget(widget, 0, column)
        content.addWidget(self.options_bar)

        self._estimate_box, estimates = frame("ticketEstimateBox", (0, 8, 0, 0), 0)
        estimate_grid = QtWidgets.QGridLayout()
        estimate_grid.setContentsMargins(0, 0, 0, 0)
        estimate_grid.setHorizontalSpacing(12)
        estimate_grid.setVerticalSpacing(4)
        self.order_value = label("—", "ticketEstimateValue", elided=True)
        self.order_value.setAccessibleName("Estimated order value")
        self.order_cost = label("—", "ticketEstimateValue", elided=True)
        self.order_cost.setAccessibleName("Estimated order margin")
        for row, (caption, value) in enumerate((("Order value", self.order_value), ("Est. margin", self.order_cost))):
            estimate_grid.addWidget(label(caption), row, 0)
            set_text_role(value, TextRole.TABLE_VALUE)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            estimate_grid.addWidget(value, row, 1)
        estimate_grid.setColumnStretch(1, 1)
        estimates.addLayout(estimate_grid)
        content.addWidget(self._estimate_box)
        self.feedback_box, feedback = frame("ticketFeedback", (8, 6, 8, 6), 6)
        self.account_problem_label = label("", "ticketAccountProblem")
        self.account_problem_label.setAccessibleName("Account or leverage issue")
        self.protection_summary = label("", "tradeAccountSummary")
        self.execution_feedback = label("", "ticketExecutionFeedback")
        self.execution_feedback.setAccessibleName("Order submission status")
        self.reconcile_button = QtWidgets.QPushButton("Reconcile orders")
        self.reconcile_button.setAccessibleName("Reconcile unknown orders")
        self.reconcile_button.clicked.connect(self.gateway.reconcile_unknown_orders)
        for widget in (self.account_problem_label, self.protection_summary,
                       self.execution_feedback, self.reconcile_button):
            if isinstance(widget, QtWidgets.QLabel):
                widget.setWordWrap(True)
                widget.setMinimumWidth(0)
            widget.hide()
            feedback.addWidget(widget)
        content.addWidget(self.feedback_box)

        self._submit_footer = QtWidgets.QWidget()
        self._submit_footer.setObjectName("ticketSubmitFooter")
        footer = QtWidgets.QVBoxLayout(self._submit_footer)
        footer.setContentsMargins(12, 0, 12, 12)
        self.side_selector = QtWidgets.QWidget()
        self.side_selector.setObjectName("tradingInlineField")
        side = QtWidgets.QGridLayout(self.side_selector)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(8)
        self.buy_button = QtWidgets.QPushButton("Buy / Long")
        self.sell_button = QtWidgets.QPushButton("Sell / Short")
        self.buy_button.setObjectName("buySideButton")
        self.sell_button.setObjectName("sellSideButton")
        side_group = QtWidgets.QButtonGroup(self)
        for column, button in enumerate((self.buy_button, self.sell_button)):
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setMinimumWidth(0)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            set_text_role(button, TextRole.UI_CONTROL)
            side_group.addButton(button)
            side.addWidget(button, 0, column)
        self.buy_button.setChecked(True)
        footer.addWidget(self.side_selector)
        root.addWidget(self._submit_footer)
        root.addStretch(1)
        self._layout_sync_timer = QTimer(self)
        self._layout_sync_timer.setSingleShot(True)
        self._layout_sync_timer.timeout.connect(self._sync_scroll_height)
        self._ticket_body.installEventFilter(self)
        self.ticket_scroll.viewport().installEventFilter(self)
        focus_order = (self.leverage, self.quick_settings_button,
                       *self.order_type_buttons.values(), self.conditional_button,
                       self.working_type, self.trigger_edit, self.trigger_mark_button,
                       self.price_edit, self.price_mark_button, self.activation_edit,
                       self.activation_mark_button, self.callback_rate,
                       self.quantity_edit, self.size_mode,
                       self.allocation_slider, *self.margin_bar.preset_buttons, self.reduce_only,
                       self.time_in_force, self.protection_button, self.reconcile_button,
                       self.buy_button, self.sell_button)
        for previous, following in zip(focus_order, focus_order[1:]):
            QtWidgets.QWidget.setTabOrder(previous, following)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.set_settings_icon(ui_palette(THEMES[DEFAULT_THEME_NAME])["muted"])

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
            self.leverage_field.setEnabled(not reducing)
            self.protection_field.setEnabled(not reducing)
            self.allocation_slider.setAccessibleName(
                "Position close percentage" if reducing else
                "Account risk percentage" if self.size_mode.currentData() == "RISK %"
                else "Available margin percentage")
            usable = not self.gateway.stopping
            self.buy_button.setEnabled(usable)
            self.sell_button.setEnabled(usable)
            self.feedback_box.setVisible(any(not widget.isHidden() for widget in
                                             (self.account_problem_label, self.protection_summary,
                                              self.execution_feedback, self.reconcile_button)))
            mode = str(self.size_mode.currentData())
            raw = self.quantity_edit.text().strip()
            if quantity is None:
                quantity = getattr(self, "_desk_preview_quantity", "")
            allocation = 0.0
            if not reducing and self._allocation_percent is not None:
                allocation = self._allocation_percent
            elif mode in {"POSITION %", "RISK %"}:
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
            blocker = QtCore.QSignalBlocker(self.allocation_slider)
            self.allocation_slider.setValue(round(max(0, min(100, allocation))))
            del blocker
            for percent, button in zip((25, 50, 75, 100), self.margin_bar.preset_buttons):
                button.setChecked(abs(allocation - percent) < 0.001)
            # Allocation is an intent: it can be selected before the price or
            # account is ready. The order builder checks fresh inputs on submit.
            self.margin_bar.setEnabled(usable)
            intent = "position" if reducing else "risk" if mode == "RISK %" else "margin"
            if intent != getattr(self, "_allocation_accessibility_intent", None):
                self._allocation_accessibility_intent = intent
                self.margin_bar.setAccessibleName(
                    "Position close percentage" if reducing else
                    "Account risk percentage" if mode == "RISK %" else "Available margin allocation")
                for percent, button in zip((25, 50, 75, 100), self.margin_bar.preset_buttons):
                    button.setAccessibleName(
                        f"Close {percent} percent of position" if reducing else
                        f"Risk {percent} percent of account" if mode == "RISK %" else
                        f"Allocate {percent} percent of available margin")
        finally:
            self._desk_syncing = False

    def _ticket_content_height(self) -> int:
        width = self.ticket_scroll.viewport().width()
        layout = self._desk_layout
        return max(layout.minimumSize().height(), layout.heightForWidth(width)
                   if layout.hasHeightForWidth() else layout.sizeHint().height())

    def compact_required_height(self) -> int:
        """Use wrapped content height, independent of the available dock height."""
        return self._ticket_content_height() + self._submit_footer.sizeHint().height()

    def _publish_compact_minimum_height(self) -> None:
        if not self.compact:
            return
        required = self.compact_required_height()
        if required != self._published_compact_minimum_height:
            self._published_compact_minimum_height = required
            self.minimum_content_height_changed.emit(required)

    def _sync_scroll_height(self) -> None:
        self._sync_field_widths()
        self._reflow_order_fields(self._active_order_fields)
        self._desk_layout.activate()
        height = max(24, self._ticket_content_height())
        # Word-wrapped errors must grow the scrollable body, not disappear
        # beneath the footer. Both execution actions stay outside that viewport.
        if self._ticket_body.minimumHeight() != height:
            self._ticket_body.setMinimumHeight(height)
        if self.ticket_scroll.maximumHeight() != height:
            self.ticket_scroll.setMaximumHeight(height)
        self.ticket_scroll.updateGeometry()
        self._publish_compact_minimum_height()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if hasattr(self, "_layout_sync_timer"):
            resized = (watched is self.ticket_scroll.viewport()
                       and event.type() == QtCore.QEvent.Type.Resize
                       and event.size().width() != event.oldSize().width())
            relayout = (watched is self._ticket_body
                        and event.type() == QtCore.QEvent.Type.LayoutRequest)
            if resized or relayout:
                self._layout_sync_timer.start(0)
        return super().eventFilter(watched, event)

    def _sync_control_heights(self) -> None:
        inputs = (self.quantity_edit, self.price_edit, self.trigger_edit,
                  self.activation_edit, self.size_mode, self.callback_rate,
                  self.price_mark_button, self.trigger_mark_button, self.activation_mark_button)
        height = max(34, max(widget.fontMetrics().height() for widget in inputs) + 14)
        for widget in inputs:
            widget.setFixedHeight(height - 2 if isinstance(widget.parentWidget(), TicketInputGroup) else height)
        self.callback_rate.setMinimumWidth(0)
        for button in (self.price_mark_button, self.trigger_mark_button, self.activation_mark_button):
            button.setFixedWidth(button.fontMetrics().horizontalAdvance("Mark") + 16)
        for button in (self.buy_button, self.sell_button):
            button.setFixedHeight(max(38, button.fontMetrics().height() + 18))
        for button in (*self.order_type_buttons.values(), self.conditional_button):
            button.setFixedHeight(max(30, button.fontMetrics().height() + 12))
        for control in (self.leverage, self.time_in_force, self.protection_button, self.working_type):
            control.setFixedHeight(max(26, control.fontMetrics().height() + 10))
        self.margin_bar.sync_geometry()
        self._order_form_signature = None
        self._ticket_chrome_signature = None
        self._sync_field_widths()
        self._layout_sync_timer.start(0)

    def _sync_field_widths(self) -> None:
        """Measure text once per layout change; never relayout on market ticks."""
        available = max(1, self.ticket_scroll.viewport().width() - 24)
        self.working_type.setFixedWidth(self.working_type.fontMetrics().horizontalAdvance("Mark price") + 34)
        modes = [self.size_mode.itemText(index) for index in range(self.size_mode.count())]
        desired = max((self.size_mode.fontMetrics().horizontalAdvance(text) for text in modes), default=0) + 34
        self.size_mode.setFixedWidth(min(desired, max(60, available // 2)))
        self.leverage.setFixedWidth(self.leverage.fontMetrics().horizontalAdvance("125×") + 34)
        self.time_in_force.setFixedWidth(self.time_in_force.fontMetrics().horizontalAdvance("GTX") + 34)
        self.protection_button.setFixedWidth(self.protection_button.fontMetrics().horizontalAdvance(self.protection_button.text()) + 20)

        type_buttons = (*self.order_type_buttons.values(), self.conditional_button)
        type_widths = tuple(button.fontMetrics().horizontalAdvance(button.text())
                            + (36 if button is self.conditional_button else 20)
                            for button in type_buttons)
        for button, width in zip(type_buttons, type_widths):
            button.setMinimumWidth(width)
        type_wrap = sum(type_widths) + 8 > available
        tif_visible = not self.time_in_force_field.isHidden()
        option_widgets = (self.reduce_only, self.time_in_force_field, self.protection_field)
        option_widths = (self.reduce_only.sizeHint().width(),
                         self.time_in_force.width() if tif_visible else 0,
                         self.protection_button.width())
        options_wrap = sum(option_widths) + (12 if tif_visible else 6) > available
        options_stack = tif_visible and sum(option_widths[1:]) + 6 > available
        action_width = max(button.fontMetrics().horizontalAdvance(button.text()) + 24
                           for button in (self.buy_button, self.sell_button))
        actions_stack = action_width * 2 + 8 > max(1, self.width() - 24)
        signature = (type_wrap, type_widths, tif_visible, options_wrap, options_stack,
                     option_widths, actions_stack)
        if signature == getattr(self, "_ticket_chrome_signature", None):
            return
        self._ticket_chrome_signature = signature
        types = self.order_type_row.layout()
        for button in type_buttons:
            types.removeWidget(button)
        for column in range(3):
            types.setColumnStretch(column, 1 if not type_wrap or column < 2 else 0)
        types.addWidget(type_buttons[0], 0, 0)
        types.addWidget(type_buttons[1], 0, 1)
        types.addWidget(type_buttons[2], 1 if type_wrap else 0, 0 if type_wrap else 2,
                        1, 2 if type_wrap else 1)

        options = self.options_bar.layout()
        for widget in option_widgets:
            options.removeWidget(widget)
        for column in range(3):
            options.setColumnStretch(column, 1 if column == 0 else 0)
        options.addWidget(self.reduce_only, 0, 0, 1, 3 if options_wrap else 1,
                          Qt.AlignmentFlag.AlignLeft)
        if options_wrap:
            options.addWidget(self.time_in_force_field, 1, 0, Qt.AlignmentFlag.AlignLeft)
            options.addWidget(self.protection_field, 2 if options_stack else 1,
                              0 if options_stack else 1, Qt.AlignmentFlag.AlignLeft)
        else:
            options.addWidget(self.time_in_force_field, 0, 1)
            options.addWidget(self.protection_field, 0, 2)

        actions = self.side_selector.layout()
        actions.removeWidget(self.buy_button)
        actions.removeWidget(self.sell_button)
        actions.setColumnStretch(0, 1)
        actions.setColumnStretch(1, 0 if actions_stack else 1)
        actions.addWidget(self.buy_button, 0, 0)
        actions.addWidget(self.sell_button, 1 if actions_stack else 0, 0 if actions_stack else 1)

    def _typography_changed(self) -> None:
        self._sync_control_heights()
        if self.compact:
            QTimer.singleShot(0, self, self._publish_compact_minimum_height)


    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if hasattr(self, "_layout_sync_timer") and event.type() in {
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
        # The cached setting may still match while another value is in flight.
        # Queue this selection so the earlier acknowledgement cannot replace it.
        if (current == leverage and self.symbol in self.gateway.cross_ready
                and self.symbol not in self.gateway.cross_pending
                and self.symbol not in self.gateway.queued_leverage):
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

    def apply_theme(self, theme: dict[str, str], *, inherited: bool = False) -> None:
        palette = ui_palette(theme)
        stylesheet = "" if inherited else _trading_stylesheet(palette)
        if self.styleSheet() != stylesheet:
            self.setStyleSheet(stylesheet)
        self.set_settings_icon(palette['muted'])

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
        (self.conditional_button if conditional else self.order_type_buttons[order_type]).setChecked(True)
        _set_repolished_property(self.conditional_button, "active", conditional)
        self.conditional_button.setText(dict((value, title) for title, value in self.ORDER_TYPES)[order_type]
                                        if conditional else "Stop")
        for kind, action in self.conditional_actions.items():
            action.setChecked(kind == order_type)
        self.time_in_force.setVisible(limit_price)
        self.time_in_force_field.setVisible(limit_price)
        self._sync_mark_controls(order_type)
        active_fields = ["amount"]
        if limit_price:
            active_fields.append("tif")
        if trailing:
            active_fields.extend(("activation", "callback", "source"))
        else:
            if limit_price:
                active_fields.append("price")
            if conditional:
                active_fields.extend(("trigger", "source"))
        self._sync_field_widths()
        self._reflow_order_fields(active_fields)
        self._update_submit_text()
        self._update_order_summary()
        self._update_execution_state()

    def _reflow_order_fields(self, active_fields: list[str]) -> None:
        self._active_order_fields = list(active_fields)
        fields = [name for name in ("trigger", "price", "activation", "callback") if name in active_fields]
        available = max(1, self.ticket_scroll.viewport().width() - 24)
        # Pair conditional prices only when both retain a useful editing area.
        column_minimum = max(240, self.price_edit.fontMetrics().horizontalAdvance("00000000.00000000") + 100)
        columns = 2 if len(fields) > 1 and available >= column_minimum * 2 + 10 else 1
        field_width = (available - 10 * (columns - 1)) // columns
        source_anchor = "trigger" if "trigger" in fields else "activation"
        source_wrap = False
        if "source" in active_fields:
            caption = self._field_headers[source_anchor].itemAtPosition(0, 0).widget()
            source_wrap = caption.sizeHint().width() + self.working_type.width() + 8 > field_width
        signature = (tuple(fields), columns, source_wrap)
        if signature == getattr(self, "_order_form_signature", None):
            return
        self._order_form_signature = signature
        for heading in self._field_headers.values():
            heading.removeWidget(self.working_type)
        self.working_type.hide()
        for name, container in self._field_rows.items():
            if name != "amount":
                self._order_form.removeWidget(container)
                container.hide()
        if "source" in active_fields:
            self._field_headers[source_anchor].addWidget(
                self.working_type, 1 if source_wrap else 0, 0 if source_wrap else 1,
                1, 2 if source_wrap else 1, Qt.AlignmentFlag.AlignLeft if source_wrap else Qt.AlignmentFlag.AlignRight)
            self.working_type.show()
        for index, name in enumerate(fields):
            self._order_form.addWidget(self._field_rows[name], index // columns, index % columns)
            self._field_rows[name].show()
        self._order_form.setColumnStretch(0, 1)
        self._order_form.setColumnStretch(1, 1 if columns == 2 else 0)
        self._order_card.setVisible(bool(fields))
        self._desk_layout.invalidate()
        self._layout_sync_timer.start(0)

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
        self._layout_sync_timer.start(0)

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
        mode = str(self.size_mode.currentData())
        if self.reduce_only.isChecked() or mode == "RISK %":
            self._allocation_percent = None
            if mode in {"POSITION %", "RISK %"}:
                self.quantity_edit.setText(str(percent))
            else:
                market = self.current_order_type() in {"MARKET", "STOP_MARKET", "TRAILING_STOP_MARKET"}
                step = self.rules.market_step if market else self.rules.lot_step
                minimum = self.rules.min_market_qty if market else self.rules.min_qty
                amount = Decimal(str(self._reduced_position_size())) * Decimal(percent) / 100
                quantity = (quantize_step(str(amount), step, offset=str(minimum))
                            if amount > 0 and amount >= Decimal(str(minimum)) else "0")
                self.quantity_edit.setText(quantity)
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
        self._amount_caption.setText({"CONTRACTS": "Amount", "QUOTE NOTIONAL": "Order value",
                                      "RISK %": "Account risk", "POSITION %": "Position size"}.get(mode, "Amount"))
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
        state = self._submission_state
        message = self._submission_detail or state.replace("_", " ").capitalize()
        _set_text_if_changed(self.execution_feedback, message)
        self.execution_feedback.setVisible(state not in {"READY", "IDLE"})
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
        displayed = quantity
        if self._allocation_percent is not None:
            if self._allocation_percent == 0:
                displayed = "0"
            elif quantity and self.size_mode.currentData() == "QUOTE NOTIONAL":
                displayed = format((Decimal(quantity) * Decimal(str(self._entry_reference(order_type)))).normalize(), "f")
        if self._allocation_percent is not None and self.quantity_edit.text() != displayed:
            # Show the derived amount in the selected unit while the slider retains the
            # percentage. Internal preview refreshes must not become manual edits.
            selected = self.quantity_edit.selectedText()
            selection_start = self.quantity_edit.selectionStart()
            cursor = self.quantity_edit.cursorPosition()
            all_selected = bool(selected) and len(selected) == len(self.quantity_edit.text())
            blocker = QtCore.QSignalBlocker(self.quantity_edit)
            self.quantity_edit.setText(displayed)
            if all_selected:
                self.quantity_edit.selectAll()
            elif selection_start >= 0:
                self.quantity_edit.setSelection(selection_start, len(selected))
            else:
                self.quantity_edit.setCursorPosition(min(cursor, len(displayed)))
            del blocker
        self._update_ticket_estimates(quantity, order_type)

    def _update_ticket_estimates(self, quantity: str, order_type: str) -> None:
        self._desk_preview_quantity = quantity
        reference = self._entry_reference(order_type)
        notional = safe_float(quantity) * reference
        leverage = self.gateway.current_leverage(self.symbol)
        value = (f"≈ {human_number(notional)} {self.rules.quote_asset}"
                 if quantity and reference > 0 else "—")
        cost = (f"≈ {human_number(notional / leverage)} {self.rules.margin_asset}"
                if quantity and reference > 0 and leverage > 0 and not self.reduce_only.isChecked()
                else "—")
        _set_text_if_changed(self.order_value, value)
        _set_text_if_changed(self.order_cost, cost)
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
        _disable_trading_tooltips(self)
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
            price = QtWidgets.QLineEdit()
            price.setPlaceholderText(f"Limit {index + 1}")
            set_text_role(price, TextRole.MARKET_VALUE)
            quantity = QtWidgets.QLineEdit()
            quantity.setPlaceholderText("Quantity")
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
        _disable_trading_tooltips(self)
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
        set_text_role(self.quantity, TextRole.MARKET_VALUE)
        set_text_role(self.price, TextRole.MARKET_VALUE)
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
        available = max(0, self.viewport().width() - 2 * self.spacing())
        for index in range(self.count()):
            item = self.item(index)
            card = self.itemWidget(item)
            if card is None:
                continue
            if card.maximumWidth() != available:
                card.setMaximumWidth(available)
            if card.width() != available:
                card.resize(available, card.height())
            if hasattr(card, "set_presentation_width"):
                card.set_presentation_width(available)
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
    """One position summary with its own on-demand details; no duplicate card."""
    selected_requested = Signal()
    trade_requested = Signal(object)
    close_requested = Signal(object, int)

    def __init__(self, payload, current_symbol):
        super().__init__()
        self.details = None
        self._expanded = False
        self._rules = None
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
        if self._expanded:
            self._update_details()

    def set_expanded(self, expanded, rules=None):
        self._rules = rules
        expanded = bool(expanded)
        if expanded and self.details is None:
            self.details = PositionDeskDetails()
            self.details.trade_requested.connect(self.trade_requested)
            self.details.close_requested.connect(self.close_requested)
            self.layout().addWidget(self.details, 2, 0, 1, 2)
        changed = self._expanded != expanded
        self._expanded = expanded
        if self.details is not None:
            self.details.setVisible(expanded)
            if expanded:
                self._update_details()
        return changed

    def _update_details(self):
        display = dict(self.payload)
        symbol = str(display.get("symbol") or "")
        quote = self._rules.quote_asset if self._rules else "USDC" if symbol.endswith("USDC") else "USDT"
        display["_quote_asset"] = quote
        display["_margin_asset"] = self._rules.margin_asset if self._rules else quote
        self.details.set_payload(display)

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

    def __init__(self):
        super().__init__()
        self.payload = {}
        self.setObjectName("positionDeskDetails")
        self.setMinimumWidth(0)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.metrics = QtWidgets.QGridLayout()
        self.metrics.setHorizontalSpacing(12)
        self.metrics.setVerticalSpacing(6)
        self.values = {}
        self.captions = {}
        self.risk_captions = {}
        for key, text in (("entry", "Entry price"), ("mark", "Mark price"), ("roe", "ROE"), ("value", "Notional")):
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
            cap = QtWidgets.QLabel(text)
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
        self.trade_button = QtWidgets.QPushButton("Reduce…")
        self.trade_button.setAccessibleName("Prepare a reduce-only order for this position")
        self.trade_button.setObjectName("deskTradePosition")
        set_text_role(self.trade_button, TextRole.UI_CONTROL)
        self.trade_button.clicked.connect(lambda: self.trade_requested.emit(dict(self.payload)) if self.payload else None)
        self.close_button = QtWidgets.QPushButton("Market close")
        set_text_role(self.close_button, TextRole.UI_CONTROL)
        self.close_button.clicked.connect(
            lambda: self.close_requested.emit(dict(self.payload), 100) if self.payload else None
        )
        self.close_button.setObjectName("dangerButton")
        actions.addWidget(self.trade_button, 1)
        actions.addWidget(self.close_button, 1)
        layout.addWidget(self._detail_actions)
        self._wide = None
        self.set_presentation_width(500)
        self.set_payload(None)

    def set_presentation_width(self, width):
        columns = 2 if width >= 520 else 1
        if columns == self._wide:
            return
        self._wide = columns
        for key in self.captions:
            self.metrics.removeWidget(self.captions[key])
            self.metrics.removeWidget(self.values[key])
        for index, key in enumerate(("entry", "mark", "roe", "value")):
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
        pnl = safe_float(payload.get("unrealizedProfit"))
        mark = safe_float(payload.get("markPrice"))
        margin = safe_float(payload.get("positionInitialMargin") or payload.get("initialMargin"))
        liquidation = safe_float(payload.get("liquidationPrice"))
        distance = abs(mark - liquidation) / mark * 100 if mark > 0 and liquidation > 0 else None
        values = {
            "entry": format_price(safe_float(payload.get("entryPrice"))),
            "mark": format_price(mark) if mark > 0 else "—",
            "roe": f"{pnl / margin * 100:+.1f}%" if margin > 0 else "—",
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
    edit_requested = Signal(object)
    selected_requested = Signal()

    def __init__(self, payload, theme, current_symbol):
        super().__init__()
        self.setObjectName("workingOrderRow")
        self.setMinimumWidth(0)
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(6, 9, 6, 9)
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
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(dict(self.payload)))
        self.edit_button = QtWidgets.QPushButton("Edit")
        self.edit_button.setObjectName("deskOrderEdit")
        self.edit_button.setMinimumWidth(0)
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(dict(self.payload)))
        self.action_buttons = QtWidgets.QWidget()
        self.action_buttons.setObjectName("tradingInlineField")
        actions = QtWidgets.QHBoxLayout(self.action_buttons)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addStretch(1)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.cancel_button)
        for widget, role in ((self.symbol_label, TextRole.INSTRUMENT_SYMBOL),
                             (self.side_label, TextRole.UI_CONTROL),
                             (self.type_label, TextRole.UI_CAPTION),
                             (self.price_label, TextRole.TABLE_VALUE),
                             (self.quantity_label, TextRole.TABLE_VALUE),
                             (self.status_label, TextRole.UI_CAPTION),
                             (self.cancel_button, TextRole.UI_CONTROL),
                             (self.edit_button, TextRole.UI_CONTROL)):
            set_text_role(widget, role)
        self._wide = None
        self.update_payload(payload, current_symbol)
        self.set_presentation_width(400)

    def set_presentation_width(self, width):
        action_width = (self.edit_button.sizeHint().width() + self.cancel_button.sizeHint().width()
                        + self.action_buttons.layout().spacing())
        self.action_buttons.setFixedWidth(action_width)
        required = max(520, self.price_label.fontMetrics().horizontalAdvance("00,000.00") * 3 +
                       self.symbol_label.fontMetrics().horizontalAdvance("BTCUSDT") +
                       action_width + 135)
        wide = width >= required
        if wide == self._wide:
            return
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
            self.grid.addWidget(self.action_buttons, 0, 5, 2, 1)
            for column, stretch in ((0, 3), (1, 2), (2, 4), (3, 3), (4, 4)):
                self.grid.setColumnStretch(column, stretch)
        else:
            self.grid.addWidget(self.symbol_label, 0, 0, 1, 3)
            self.grid.addWidget(self.action_buttons, 0, 3)
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
        self.price_label.setAlignment((Qt.AlignmentFlag.AlignLeft if wide else Qt.AlignmentFlag.AlignRight)
                                     | Qt.AlignmentFlag.AlignVCenter)
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
        self.edit_button.setVisible(payload.get("_source") == "STANDARD" and payload.get("type") == "LIMIT")
        self.edit_button.setAccessibleName(f"Edit {payload.get('symbol', '')} limit order {identifier or ''}")

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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("tradingAccountTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                QtWidgets.QSizePolicy.Policy.Expanding)
        set_text_role(self.tabs.tabBar(), TextRole.UI_CONTROL)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.positions = _new_account_card_list()
        self.positions.setSpacing(6)
        self.tabs.addTab(self.positions, "Positions")
        self.orders = _new_account_card_list()
        self.orders.setSpacing(6)
        self.fills = FillAccountView()
        self.fills.setSpacing(0)
        order_page = self.order_page = QtWidgets.QWidget()
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
            label.setAlignment((Qt.AlignmentFlag.AlignRight if text == "Quantity" else Qt.AlignmentFlag.AlignLeft)
                               | Qt.AlignmentFlag.AlignVCenter)
            headers.addWidget(label, stretch)
        self.order_headers.hide()
        order_layout.addWidget(self.order_headers)
        order_layout.addWidget(self.orders, 1)
        self.tabs.addTab(order_page, "Orders")
        self.orders.presentation_changed.connect(self._sync_order_headers)
        self.tabs.addTab(self.fills, "Fills")
        root.addWidget(self.tabs, 1)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(6)
        self.actions.setContentsMargins(12, 0, 12, 10)
        self.cancel_button = QtWidgets.QPushButton("Cancel symbol orders")
        self.cancel_button.setObjectName("deskCancelSymbolOrders")
        set_text_role(self.cancel_button, TextRole.UI_CONTROL)
        self.cancel_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum,
                                         QtWidgets.QSizePolicy.Policy.Fixed)
        self.more_button = QtWidgets.QToolButton()
        self.more_button.setText("Batch…")
        set_text_role(self.more_button, TextRole.UI_CONTROL)
        self.more_button.hide()
        self.status = ElidedLabel("All symbols")
        self.status.setObjectName("accountCardDetail")
        set_text_role(self.status, TextRole.UI_CAPTION)
        self.actions.addWidget(self.status, 1)
        self.actions.addWidget(self.cancel_button)
        self.actions.addWidget(self.more_button)
        self.footer = QtWidgets.QWidget()
        self.footer.setObjectName("tradingInlineField")
        self.footer.setLayout(self.actions)
        root.addWidget(self.footer)
        self.positions.currentItemChanged.connect(self._selection_changed)
        self.orders.currentItemChanged.connect(self._sync_selected_rows)
        self._position_factory, self._order_factory = position_factory, order_factory
        self.tabs.currentChanged.connect(lambda _index: self._sync_selected_rows())

    def _sync_order_headers(self):
        first = self.orders.item(0)
        row = self.orders.itemWidget(first) if first else None
        self.order_headers.setVisible(isinstance(row, WorkingOrderRow) and bool(row._wide))
        if isinstance(row, WorkingOrderRow) and row._wide:
            row.grid.activate()
            width = self.orders.viewport().width()
            self.order_headers.setFixedWidth(width)
            cells = [row.grid.cellRect(0, column) for column in range(6)]
            self.order_headers.layout().setContentsMargins(
                row.x() + cells[0].left(), 5,
                max(0, width - row.x() - cells[-1].right() - 1), 5)
            for column in range(6):
                label = self.order_headers.layout().itemAt(column).widget()
                required = cells[column].width()
                if required > 0 and label.width() != required:
                    label.setFixedWidth(required)

    def _selection_changed(self, *_args):
        item = self.positions.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._sync_selected_rows()
        if isinstance(payload, dict):
            self.position_selected.emit(dict(payload))

    def _sync_selected_rows(self, *_args):
        for view in (self.positions, self.orders):
            current = view.currentItem()
            for index in range(view.count()):
                item = view.item(index)
                card = view.itemWidget(item)
                if card is not None:
                    _set_repolished_property(card, "selected", item is current)
                    if isinstance(card, PositionDeskRow):
                        expanded = item is current and self.tabs.currentWidget() is self.positions
                        if card.set_expanded(expanded, self.symbol_rules.get(card.payload.get("symbol"))):
                            view._schedule_card_heights()

    def set_positions(self, payloads, symbol, connected):
        blocker = QtCore.QSignalBlocker(self.positions)
        _populate_account_cards(self.positions, payloads, self._position_factory,
                                "No open positions" if connected else "Connect API credentials to view positions",
                                update_existing=lambda card, payload: card.update_payload(payload, symbol),
                                update_context=symbol)
        del blocker
        self.positions._schedule_card_heights()
        self.tabs.setTabText(self.tabs.indexOf(self.positions), f"Positions {len(payloads)}")
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
        self.tabs.setTabText(self.tabs.indexOf(self.order_page), f"Orders {len(payloads)}")
        _set_text_if_changed(self.cancel_button, f"Cancel {symbol}")

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
        *,
        inherited_theme_stylesheet: bool = False,
    ):
        super().__init__(parent)
        _disable_trading_tooltips(self)
        self._inherited_theme_stylesheet = bool(inherited_theme_stylesheet)
        self.theme = ui_palette(theme)
        self.gateway = gateway
        self.symbol_rules = symbol_rules if symbol_rules is not None else {}
        self.symbol = DEFAULT_SYMBOL
        self.rules = SymbolRules()
        self._account_view_open = False
        self._open_orders: list[dict[str, Any]] = []
        self._position_payloads: list[dict[str, Any]] = []
        self._last_account_snapshot_mono = 0.0
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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setObjectName("tradingWorkspace")
        self.ticket = OrderPanel(gateway, compact=True)
        self.ticket.order_requested.connect(self.order_requested)
        self.account_frame = PositionDeskView(self._position_card, self._order_card)
        self.account_frame.symbol_rules = self.symbol_rules
        self.main_tabs = QtWidgets.QTabWidget()
        self.main_tabs.setObjectName("tradingDestinations")
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.setIconSize(QtCore.QSize(18, 18))
        self.main_tabs.tabBar().setAccessibleName("Trade and Positions")
        self.main_tabs.tabBar().setExpanding(False)
        self.main_tabs.tabBar().setDrawBase(False)
        set_text_role(self.main_tabs.tabBar(), TextRole.UI_CONTROL)
        self.main_tabs.addTab(self.ticket, "")
        self.main_tabs.addTab(self.account_frame, "")
        self.main_tabs.tabBar().setAccessibleTabName(0, "Trade")
        self.main_tabs.tabBar().setAccessibleTabName(1, "Positions")
        self.ticket._ticket_header.layout().removeWidget(self.ticket.quick_settings_button)
        tab_actions = QtWidgets.QWidget()
        tab_actions.setObjectName("tradingInlineField")
        tab_action_layout = QtWidgets.QHBoxLayout(tab_actions)
        tab_action_layout.setContentsMargins(0, 4, 8, 4)
        tab_action_layout.setSpacing(0)
        tab_action_layout.addWidget(self.ticket.quick_settings_button)
        self.main_tabs.setCornerWidget(tab_actions, Qt.Corner.TopRightCorner)
        layout.addWidget(self.main_tabs)

        self.refresh_button = QtWidgets.QPushButton()
        self.refresh_button.setObjectName("portfolioSyncButton")
        self.refresh_button.setIconSize(QtCore.QSize(16, 16))
        self.refresh_button.setFixedSize(28, 28)
        self.refresh_button.setAccessibleName("Refresh account")
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.clicked.connect(lambda: self.gateway.refresh_account(self.symbol, True))
        self.refresh_button.setEnabled(self.gateway.has_credentials())
        self.gateway.credentials_changed.connect(
            lambda _key: self.refresh_button.setEnabled(self.gateway.has_credentials()))
        self.account_frame.actions.insertWidget(0, self.refresh_button)
        self.positions = self.account_frame.positions
        self.orders = self.account_frame.orders
        self.fills = self.account_frame.fills
        self.tabs = self.account_frame.tabs
        self.status = self.account_frame.status
        self.balances = self._table(("ASSET", "WALLET", "AVAILABLE", "UPNL"))
        self.tabs.addTab(self.balances, "Balances")
        self.tabs.tabBar().setExpanding(False)

        self.cancel_all_button = self.account_frame.cancel_button
        self.order_actions = self.account_frame.more_button
        self.batch_button = self.order_actions
        self.batch_button.clicked.connect(self.open_batch_orders)
        self.ticket.reduce_only.toggled.connect(lambda _checked: self._sync_batch_action())
        self.cancel_all_button.clicked.connect(self.cancel_selected_symbol_orders)
        self.tabs.currentChanged.connect(self._account_tab_changed)
        self.main_tabs.currentChanged.connect(self._main_tab_changed)
        self.orders.currentItemChanged.connect(lambda *_args: self._sync_account_actions(self.tabs.currentIndex()))
        self.account_frame.position_selected.connect(self._position_selected)
        self.ticket.position_target_selected.connect(self.select_position)
        _populate_account_cards(self.positions, [], None, "Connect API credentials to view positions")
        _populate_account_cards(self.orders, [], None, "Connect API credentials to view orders")
        _populate_fill_cards(self.fills, {}, self.symbol, connected=False)
        self.gateway.snapshot_ready.connect(self.apply_snapshot)
        self.gateway.credentials_changed.connect(self._clear_account_view)
        self.gateway.account_event.connect(self._account_event)
        self.gateway.state_changed.connect(self.status.setText)
        self.gateway.request_succeeded.connect(
            lambda _request, _result: self.account_refresh_timer.start()
        )
        self._sync_account_actions(self.tabs.currentIndex())
        self.apply_theme(theme)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(0, 96)

    def set_bottom_panel(self, _bottom: bool) -> None:
        # The ticket reflows from its own viewport; account pages use all space.
        pass

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
        self.symbol_rules[symbol] = rules
        self._position_payloads.sort(key=lambda row: (row.get("symbol") != symbol, -_position_notional(row)))
        self._open_orders.sort(key=lambda row: (row.get("symbol") != symbol, str(row.get("symbol", ""))))
        self.account_frame.set_positions(self._position_payloads, symbol, self.gateway.has_credentials())
        self.account_frame.set_orders(self._open_orders, symbol, self.gateway.has_credentials())
        self._sync_account_actions(self.tabs.currentIndex())
        if changed:
            self.fills.set_payloads([], f"Loading fills for {symbol}…"
                                    if self.gateway.has_credentials() else "Connect API credentials to view fills")
        self._refresh_account_freshness()
        self._poll_account_if_open()

    def set_mark_price(self, price: float, symbol: str | None = None) -> None:
        symbol = str(symbol or self.symbol).upper()
        if symbol == self.symbol:
            self.ticket.set_mark_price(price)
        for row in self._position_payloads:
            if str(row.get("symbol") or "").upper() == symbol:
                _update_position_mark(row, price)
        self.account_frame.set_mark_price(price, symbol)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._account_view_open:
            self.account_age_timer.start()
            self._refresh_account_freshness()
        QTimer.singleShot(0, self, self._poll_account_if_open)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.account_age_timer.stop()
        super().hideEvent(event)

    def set_page(self, index: int) -> None:
        self.set_account_view(int(index) == 1)

    def set_account_view(self, opened: bool) -> None:
        self.main_tabs.setCurrentIndex(1 if opened else 0)

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
        if not self._account_view_open or not self.isVisible():
            return
        if not self.gateway.has_credentials():
            self.status.setText("ACCOUNT DATA · ADD API CREDENTIALS")
            return
        if self._last_account_snapshot_mono <= 0.0:
            self.status.setText("ACCOUNT DATA · WAITING FOR SNAPSHOT")
            return
        age = max(0.0, time.monotonic() - self._last_account_snapshot_mono)
        scope = f"{self.symbol} · Local time" if self.tabs.currentWidget() is self.fills else "All symbols"
        _set_text_if_changed(self.status, f"{scope} · Stale {int(age)}s" if age >= 30.0 else scope)

    def _account_tab_changed(self, tab_index: int) -> None:
        self._sync_account_actions(tab_index)
        self._refresh_account_freshness()
        self._poll_account_if_open()

    def _main_tab_changed(self, index: int) -> None:
        opened = index == 1
        changed = opened != self._account_view_open
        self._account_view_open = opened
        if opened and self.isVisible():
            self.account_age_timer.start()
        else:
            self.account_age_timer.stop()
        if changed:
            self.view_changed.emit(1 if opened else 0)
        tab_index = self.tabs.currentIndex()
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
        orders_open = self.tabs.widget(tab_index) is self.account_frame.order_page
        self.account_frame.footer.setVisible(True)
        self.order_actions.setVisible(orders_open)
        self.cancel_all_button.setVisible(orders_open)
        connected = self.gateway.has_credentials()
        self.cancel_all_button.setEnabled(connected and any(str(row.get("symbol") or "").upper() == self.symbol.upper() for row in self._open_orders))
        self._sync_batch_action()

    def _sync_batch_action(self) -> None:
        reducing = bool(self.ticket.reduce_only.isChecked())
        self.batch_button.setEnabled(not reducing and self.gateway.has_credentials())

    def set_close_presets(self, preset: dict[str, Any]) -> None:
        self.close_percentages = [
            max(1, min(100, int(safe_float(preset.get(key), fallback))))
            for key, fallback in (
                ("close_1_percent", 25),
                ("close_2_percent", 50),
                ("close_3_percent", 100),
            )
        ]
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

        self._sync_account_actions(self.tabs.currentIndex())
        if mark_fresh and self.gateway.has_credentials():
            self._refresh_account_freshness()
        elif not self.gateway.has_credentials():
            self.status.setText("ACCOUNT DATA · ADD API CREDENTIALS")

    def _position_card(self, payload):
        card = PositionDeskRow(payload, self.symbol)
        card.trade_requested.connect(self.position_trade_requested)
        card.close_requested.connect(self._close_position_payload)
        return card

    def _order_card(self, payload):
        card = WorkingOrderRow(payload, self.theme, self.symbol)
        card.cancel_requested.connect(self._cancel_order_payload)
        card.edit_requested.connect(self._modify_order_payload)
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

    def _modify_order_payload(self, order: dict[str, Any]) -> None:
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

    def close_selected_position(self, percent: int = 100) -> None:
        position = self._selected_payload(self.positions)
        if not position:
            self.status.setText("SELECT A POSITION TO CLOSE")
            return
        self._close_position_payload(position, percent)


    def _account_event(self, _event: dict[str, Any]) -> None:
        self.account_refresh_timer.start()

    @staticmethod
    def scoped_theme_stylesheet(theme: dict[str, str]) -> str:
        """Embed trading rules in the shell's single scoped recolor pass."""
        rules = []
        root = 'QWidget#tradingWorkspace'
        for rule in _trading_stylesheet(theme).split('}'):
            if '{' not in rule:
                continue
            selectors, body = rule.split('{', 1)
            selectors = ','.join(selector if selector.startswith(root) else root + ' ' + selector
                                 for selector in (part.strip() for part in selectors.split(',')))
            rules.append(selectors + '{' + body + '}')
        return ''.join(rules)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = ui_palette(theme)
        if not self._inherited_theme_stylesheet:
            stylesheet = _trading_stylesheet(self.theme)
            if self.styleSheet() != stylesheet:
                self.setStyleSheet(stylesheet)
        self.ticket.apply_theme(self.theme, inherited=True)
        self.fills.apply_theme(self.theme)
        ratio = self.devicePixelRatioF()
        self.refresh_button.setIcon(line_icon('refresh', self.theme['muted'], ratio))
        self.main_tabs.setTabIcon(0, asset_svg_icon("trading-account.svg", self.theme["text"], ratio))
        self.main_tabs.setTabIcon(1, asset_svg_icon("trading-open-orders.svg", self.theme["text"], ratio))
        self.main_tabs.setTabText(0, "Trade")
        self.main_tabs.setTabText(1, "Account")



# Performance is deliberately hosted here with the other trading workspaces.
# Nothing below constructs widgets, starts workers or reads history at import time.
_PERFORMANCE_STYLES = (
    dict(name='Aurora', bg='#090e1c', card='#111a2c', raised='#18253b', border='#24324b',
         text='#edf4ff', muted='#91a3bd', accent='#7de3fa', green='#66e5b6', red='#ff637e', radius=14),
    dict(name='Journal', bg='#151413', card='#201e1b', raised='#2c2925', border='#3c3730',
         text='#f5eee2', muted='#b3a79a', accent='#ecc294', green='#b7d88b', red='#f47c69', radius=9),
    dict(name='Vector', bg='#090d0c', card='#101714', raised='#19231d', border='#2b3b30',
         text='#edf7ed', muted='#91a998', accent='#d5f781', green='#9ce6a3', red='#ff6c75', radius=3),
)


def _performance_number(value, *, signed=False, decimals=2) -> str:
    value = float(value)
    if abs(value) < 0.5 * 10 ** -decimals:
        value = 0.0
    return f'{value:+,.{decimals}f}' if signed else f'{value:,.{decimals}f}'


def _performance_demo() -> list[dict]:
    from datetime import datetime, timedelta, timezone
    import random
    rng = random.Random(731)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    markets = [('BTCUSDT', 68000), ('ETHUSDT', 3200), ('SOLUSDT', 154), ('SUIUSDT', 2.1), ('XRPUSDT', 0.6)]
    rows = []
    for index in range(176):
        symbol, price = markets[rng.randrange(len(markets))]
        opened = today - timedelta(days=89 - index // 2, hours=-rng.randrange(5, 21), minutes=-rng.randrange(60))
        quantity = Decimal(str(round(rng.uniform(500, 6500) / price, 5)))
        win = rng.random() < .62
        pnl = Decimal(str(round(rng.uniform(12, 310) if win else -rng.uniform(15, 220), 2)))
        buy = rng.random() < .56
        entry = Decimal(str(price))
        exit_price = (entry + pnl / quantity * (1 if buy else -1)).quantize(Decimal('.00001'))
        for leg in range(2):
            at = opened + (timedelta(minutes=rng.randrange(8, 280)) if leg else timedelta())
            fill_price = exit_price if leg else entry
            fee = (quantity * fill_price * Decimal('.0004')).quantize(Decimal('.00000001'))
            rows.append(dict(symbol=symbol, trade_id=str(index * 2 + leg), order_id=str(index * 2 + leg),
                             client_id=f'demo-{index}-{leg}', time_ms=int(at.timestamp() * 1000),
                             side='BUY' if buy != bool(leg) else 'SELL', position_side='LONG' if buy else 'SHORT',
                             price=str(fill_price), qty=str(quantity), realized=str(pnl if leg else 0),
                             fee=str(fee), fee_asset='USDT', settlement='USDT', maker=int(rng.random() < .35),
                             closing=leg))
    return sorted(rows, key=lambda row: row['time_ms'])


def _performance_summary(source: list[dict], days: int, market: str, side: str, currency: str) -> dict:
    """Worker-only aggregation. Decimal accounting; floats only at the drawing edge."""
    from datetime import datetime, timezone
    from collections import defaultdict
    cutoff = int(time.time() * 1000) - days * 86_400_000 if days else 0
    # A one-way exit at the entry price has zero exchange realized P&L. Use
    # the full recorded inventory before filtering so it still counts as an
    # exit, including when the opening fill falls outside the chosen period.
    positions = defaultdict(Decimal)
    classified = []
    for original in sorted(source, key=lambda row: (row['time_ms'], int(row['trade_id']))):
        row = dict(original)
        key = (row['symbol'], row['position_side'])
        signed = Decimal(row['qty']) * (1 if row['side'] == 'BUY' else -1)
        held = positions[key]
        closes_recorded = held * signed < 0
        row['closing'] = bool(row['closing'] or closes_recorded)
        if not row['closing'] or closes_recorded:
            positions[key] += signed
        classified.append(row)
    rows = [row for row in classified if row['time_ms'] >= cutoff
            and row['settlement'] == currency and (not market or row['symbol'] == market)
            and (not side or row['side'] == side)]
    rows.sort(key=lambda row: (row['time_ms'], int(row['trade_id'])))
    zero = Decimal(0)
    net = gross = fees = volume = peak = drawdown = zero
    days_map, markets_map, fees_map, exits = (defaultdict(lambda: zero) for _ in range(4))
    day_counts = defaultdict(int)
    exit_orders = set()
    hours = [[0.0] * 24 for _ in range(7)]
    hour_counts = [[0] * 24 for _ in range(7)]
    curve, dips = [], []
    if rows:
        curve.append((rows[0]['time_ms'] - 1, 0.0))
        dips.append((rows[0]['time_ms'] - 1, 0.0))
    makers = 0
    for row in rows:
        realized, fee = Decimal(row['realized']), Decimal(row['fee'])
        paid = fee if row['fee_asset'] == currency else zero
        result = realized - paid
        net += result
        gross += realized
        fees += paid
        volume += Decimal(row['qty']) * Decimal(row['price'])
        fees_map[row['fee_asset'] or 'Unknown'] += fee
        peak = max(peak, net)
        drawdown = max(drawdown, peak - net)
        curve.append((row['time_ms'], float(net)))
        dips.append((row['time_ms'], float(net - peak)))
        stamp = datetime.fromtimestamp(row['time_ms'] / 1000, timezone.utc)
        day = stamp.date().isoformat()
        days_map[day] += result
        day_counts[day] += 1
        markets_map[row['symbol']] += result
        hours[stamp.weekday()][stamp.hour] += float(result)
        hour_counts[stamp.weekday()][stamp.hour] += 1
        order_key = (row['symbol'], row['order_id'])
        exits[order_key] += result
        if row['closing']:
            exit_orders.add(order_key)
        makers += bool(row['maker'])
        row.update(net=float(result), day=day, date_text=stamp.strftime('%d %b %Y  %H:%M:%S'))
    exits = {key: value for key, value in exits.items() if key in exit_orders}
    wins = sum(value > 0 for value in exits.values())
    losses = sum(value < 0 for value in exits.values())
    gain = sum((v for v in exits.values() if v > 0), zero)
    loss = -sum((v for v in exits.values() if v < 0), zero)

    def decimate(points):
        if len(points) <= 800:
            return points
        step = max(1, len(points) // 350)
        reduced = [points[0]]
        for start in range(1, len(points) - 1, step):
            bucket = points[start:min(start + step, len(points) - 1)]
            if bucket:
                extremes = {min(range(len(bucket)), key=lambda i: bucket[i][1]),
                            max(range(len(bucket)), key=lambda i: bucket[i][1])}
                reduced.extend(bucket[i] for i in sorted(extremes))
        return [*reduced, points[-1]]

    return dict(rows=list(reversed(rows)), net=float(net), gross=float(gross), fees=float(fees),
                fee_assets={key: float(value) for key, value in fees_map.items() if value},
                volume=float(volume), drawdown=float(drawdown), curve=decimate(curve), dips=decimate(dips),
                days={key: float(value) for key, value in sorted(days_map.items())}, day_counts=dict(day_counts),
                markets=sorted(((key, float(value)) for key, value in markets_map.items()), key=lambda p: p[1], reverse=True),
                hours=hours, hour_counts=hour_counts, wins=wins, losses=losses, exits=len(exits),
                win_rate=wins / len(exits) * 100 if exits else None,
                factor=float(gain / loss) if loss else (float('inf') if gain else None),
                average=float(sum(exits.values(), zero) / len(exits)) if exits else None,
                maker=makers / len(rows) * 100 if rows else 0, currency=currency,
                all_markets=sorted({row['symbol'] for row in source if row['settlement'] == currency}))


class _PerformanceTableModel(QtCore.QAbstractTableModel):
    HEADERS = ('Executed · UTC', 'Market', 'Action', 'Price', 'Quantity', 'Realized', 'Fee', 'Net')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.colors = _PERFORMANCE_STYLES[0]
        self.currency = 'USDT'

    def set_rows(self, rows, colors, currency):
        self.beginResetModel()
        self.rows, self.colors, self.currency = rows, colors, currency
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] + (f' · {self.currency}' if section in (5, 7) else '')
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            def precise(value):
                return format(Decimal(value).normalize(), 'f')
            return (row['date_text'], row['symbol'], row['side'].title() + (' / Exit' if row['closing'] else ' / Entry'),
                    precise(row['price']), precise(row['qty']), _performance_number(row['realized'], signed=True),
                    f"{precise(row['fee'])} {row['fee_asset']}",
                    _performance_number(row['net'], signed=True))[col]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if col >= 3 else Qt.AlignmentFlag.AlignLeft))
        if role == Qt.ItemDataRole.ForegroundRole:
            value = float(row['realized']) if col == 5 else row['net']
            color = self.colors['muted'] if col in (0, 3, 4, 6) else self.colors['text']
            if col in (5, 7) and value:
                color = self.colors['green' if value > 0 else 'red']
            return QtGui.QColor(color)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Order {row['order_id']} · Fill {row['trade_id']}\n{row['position_side']} · {'Maker' if row['maker'] else 'Taker'}"
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        fields = ('time_ms', 'symbol', 'side', 'price', 'qty', 'realized', 'fee', 'net')
        self.layoutAboutToBeChanged.emit()
        self.rows.sort(key=lambda row: Decimal(str(row[fields[column]])) if column >= 3 else row[fields[column]],
                       reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


class _PerformancePlot(QtWidgets.QWidget):
    day_selected = Signal(str)

    def __init__(self, kind, title, parent=None):
        super().__init__(parent)
        self.kind, self.title = kind, title
        self.summary = {}
        self.colors = _PERFORMANCE_STYLES[0]
        self.hover_index = -1
        self.hit_days = []
        self.hover_regions = []
        self._hover_text = ''
        self.plot_rect = QtCore.QRectF()
        self.month = QtCore.QDate.currentDate()
        self.metric_values = ('', '', '')
        self.setMouseTracking(kind != 'metric')
        self.setProperty('informationalToolTip', True)
        self.setMinimumWidth(200)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def configure(self, summary, colors):
        self.summary, self.colors = summary, colors
        self.hover_index = -1
        self.hover_regions = []
        if self.kind == 'metric' and not summary:
            self.metric_values = ('—', '', '')
        self.setAccessibleName(self.title)
        self.update()

    def _text(self, p, rect, value, size=12, color=None, *, bold=False, align=None, mono=False):
        font = QtGui.QFont('Iosevka' if mono or self.colors['name'] == 'Vector' else 'Inter')
        font.setPixelSize(size)
        font.setWeight(QtGui.QFont.Weight.DemiBold if bold else QtGui.QFont.Weight.Normal)
        p.setFont(font)
        p.setPen(QtGui.QColor(color or self.colors['text']))
        p.drawText(QtCore.QRectF(rect), align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), str(value))

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        c, w, h = self.colors, self.width(), self.height()
        p.setPen(QtGui.QPen(QtGui.QColor(c['border']), 1))
        p.setBrush(QtGui.QColor(c['card']))
        p.drawRoundedRect(QtCore.QRectF(.5, .5, w - 1, h - 1), c['radius'], c['radius'])
        self._text(p, QtCore.QRectF(20, 14, w - 40, 22), self.title.upper(), 11, c['muted'], bold=True)
        if self.kind == 'metric':
            value, detail, color = self.metric_values
            self._text(p, QtCore.QRectF(20, 39, w - 40, 40), value, 29, color or c['text'], bold=True)
            self._text(p, QtCore.QRectF(20, 82, w - 40, 20), detail, 11, c['muted'])
        elif self.kind in {'equity', 'drawdown'}:
            self._line(p)
        elif self.kind == 'ring':
            self._ring(p)
        elif self.kind == 'calendar':
            self._calendar(p)
        elif self.kind == 'markets':
            self._markets(p)
        elif self.kind == 'daily':
            self._daily(p)
        elif self.kind == 'hours':
            self._hours(p)
        p.end()

    def _line(self, p):
        from datetime import datetime, timezone
        s, c = self.summary, self.colors
        equity = self.kind == 'equity'
        points = s.get('curve' if equity else 'dips', [])
        top = 112 if equity else 52
        rect = QtCore.QRectF(20, top, max(1, self.width() - 100), max(1, self.height() - top - 34))
        self.plot_rect = rect
        value = s.get('net' if equity else 'drawdown', 0)
        if equity:
            self._text(p, QtCore.QRectF(20, 40, self.width() - 40, 45),
                       _performance_number(value, signed=True), 36, c['green' if value >= 0 else 'red'], bold=True)
            self._text(p, QtCore.QRectF(22, 83, self.width() - 44, 18),
                       f"{s.get('currency', 'USDT')} · realized P&L less settlement-currency fees", 11, c['muted'])
        low = min([v for _, v in points] + [0])
        high = max([v for _, v in points] + [0])
        pad = max((high - low) * .12, .01)
        low, high = low - pad, high + pad
        t0, t1 = (points[0][0], max(points[-1][0], points[0][0] + 1)) if points else (0, 1)

        def location(stamp, amount):
            return QtCore.QPointF(rect.left() + (stamp - t0) / (t1 - t0) * rect.width(),
                                 rect.bottom() - (amount - low) / (high - low) * rect.height())

        for index in range(4):
            amount = high - (high - low) * index / 3
            y = location(t0, amount).y()
            p.setPen(QtGui.QPen(QtGui.QColor(c['border']), .7))
            p.drawLine(QtCore.QPointF(rect.left(), y), QtCore.QPointF(rect.right(), y))
            self._text(p, QtCore.QRectF(rect.right() + 10, y - 9, 67, 18), _performance_number(amount, decimals=0), 10, c['muted'], mono=True)
        if not points:
            self._text(p, rect, 'Your first recorded fills start the curve.', 13, c['muted'], align=Qt.AlignmentFlag.AlignCenter)
            return
        path = QtGui.QPainterPath(location(*points[0]))
        for point in points[1:]:
            path.lineTo(location(*point))
        color = QtGui.QColor(c['accent'] if equity else c['red'])
        area = QtGui.QPainterPath(path)
        area.lineTo(rect.right(), rect.bottom())
        area.lineTo(rect.left(), rect.bottom())
        area.closeSubpath()
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
        color.setAlpha(65)
        gradient.setColorAt(0, color)
        color.setAlpha(0)
        gradient.setColorAt(1, color)
        p.fillPath(area, gradient)
        p.setPen(QtGui.QPen(QtGui.QColor(c['accent'] if equity else c['red']), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for stamp, alignment, x in [(t0, Qt.AlignmentFlag.AlignLeft, rect.left()),
                                     (t1, Qt.AlignmentFlag.AlignRight, rect.right() - 100)]:
            self._text(p, QtCore.QRectF(x, rect.bottom() + 7, 100, 20),
                       datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime('%d %b %Y'),
                       10, c['muted'], align=alignment | Qt.AlignmentFlag.AlignVCenter)
        if 0 <= self.hover_index < len(points):
            stamp, amount = points[self.hover_index]
            point = location(stamp, amount)
            p.setPen(QtGui.QPen(QtGui.QColor(c['muted']), 1, Qt.PenStyle.DashLine))
            p.drawLine(QtCore.QPointF(point.x(), rect.top()), QtCore.QPointF(point.x(), rect.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(c['accent']))
            p.drawEllipse(point, 4, 4)
            label = f"{datetime.fromtimestamp(stamp / 1000, timezone.utc):%d %b %H:%M} UTC   {_performance_number(amount, signed=True)}"
            box = QtCore.QRectF(max(rect.left(), min(point.x() - 125, rect.right() - 250)), rect.top() + 6, 250, 28)
            p.setBrush(QtGui.QColor(c['raised']))
            p.drawRoundedRect(box, 4, 4)
            self._text(p, box.adjusted(9, 0, -9, 0), label, 11)

    def _ring(self, p):
        c, s = self.colors, self.summary
        side = min(self.width() - 72, self.height() - 120, 190)
        side = max(60, side)
        r = QtCore.QRectF((self.width() - side) / 2, 55, side, side)
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QtGui.QPen(QtGui.QColor(c['border']), 13)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawEllipse(r)
        rate = s.get('win_rate')
        if rate is not None:
            pen.setColor(QtGui.QColor(c['green']))
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -int(rate / 100 * 360 * 16))
        self._text(p, r.adjusted(0, -9, 0, -9), f'{rate:.1f}%' if rate is not None else '—', 32,
                   bold=True, align=Qt.AlignmentFlag.AlignCenter)
        self._text(p, r.adjusted(0, 48, 0, 0), 'exit win rate', 11, c['muted'], align=Qt.AlignmentFlag.AlignCenter)
        self._text(p, QtCore.QRectF(20, r.bottom() + 24, self.width() - 40, 22),
                   f"{s.get('wins', 0)} wins   /   {s.get('losses', 0)} losses", 12, align=Qt.AlignmentFlag.AlignCenter)
        self._text(p, QtCore.QRectF(20, r.bottom() + 46, self.width() - 40, 20),
                   f"{s.get('exits', 0)} executed exit orders", 11, c['muted'], align=Qt.AlignmentFlag.AlignCenter)

    def _daily(self, p):
        c, s = self.colors, self.summary
        data = list(s.get('days', {}).items())[-42:]
        rect = QtCore.QRectF(20, 60, self.width() - 40, self.height() - 95)
        magnitude = max([abs(v) for _, v in data] + [.01])
        center = rect.center().y()
        p.setPen(QtGui.QPen(QtGui.QColor(c['border']), 1))
        p.drawLine(QtCore.QPointF(rect.left(), center), QtCore.QPointF(rect.right(), center))
        count = max(1, len(data))
        width = rect.width() / count
        self.hover_regions = []
        for index, (day, value) in enumerate(data):
            size = abs(value) / magnitude * (rect.height() / 2 - 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(c['green' if value >= 0 else 'red']))
            p.drawRoundedRect(QtCore.QRectF(rect.left() + index * width + 1, center - size if value >= 0 else center,
                                           max(1, width - 3), max(1, size)), 2, 2)
            self.hover_regions.append((QtCore.QRectF(rect.left() + index * width, rect.top(), width, rect.height()),
                                       f'{day} UTC · {_performance_number(value, signed=True)} {s.get("currency", "")}' ))
        caption = f"{data[0][0]}  —  {data[-1][0]} · last {len(data)} active days" if data else 'No executions in this period'
        self._text(p, QtCore.QRectF(20, self.height() - 29, self.width() - 40, 18), caption, 10, c['muted'])

    def _markets(self, p):
        c = self.colors
        rows = self.summary.get('markets', [])[:5]
        maximum = max([abs(v) for _, v in rows] + [.01])
        slot = max(28, min(40, (self.height() - 55) / max(1, len(rows))))
        for index, (symbol, value) in enumerate(rows):
            y = 48 + index * slot
            self._text(p, QtCore.QRectF(20, y, 105, 20), symbol, 11, bold=True)
            self._text(p, QtCore.QRectF(self.width() - 125, y, 105, 20), _performance_number(value, signed=True),
                       12, c['green' if value >= 0 else 'red'], align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, mono=True)
            r = QtCore.QRectF(134, y + 6, max(1, self.width() - 273), 7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(c['raised']))
            p.drawRoundedRect(r, 3, 3)
            r.setWidth(r.width() * abs(value) / maximum)
            p.setBrush(QtGui.QColor(c['green' if value >= 0 else 'red']))
            p.drawRoundedRect(r, 3, 3)
        if not rows:
            self._text(p, QtCore.QRectF(20, 65, self.width() - 40, 40), 'Market results appear after your first fill.', 12, c['muted'])

    def _calendar(self, p):
        import calendar
        c, s = self.colors, self.summary
        year, month = self.month.year(), self.month.month()
        first, count = calendar.monthrange(year, month)
        gap = 7
        width = (self.width() - 40 - gap * 6) / 7
        height = (self.height() - 88 - gap * 5) / 6
        magnitude = max([abs(v) for day, v in s.get('days', {}).items() if day.startswith(f'{year:04}-{month:02}')] + [.01])
        self.hit_days = []
        self.hover_regions = []
        for i, label in enumerate(('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')):
            self._text(p, QtCore.QRectF(20 + i * (width + gap), 42, width, 22), label, 10, c['muted'])
        for day in range(1, count + 1):
            slot = first + day - 1
            rect = QtCore.QRectF(20 + (slot % 7) * (width + gap), 72 + (slot // 7) * (height + gap), width, height)
            key = f'{year:04}-{month:02}-{day:02}'
            value = s.get('days', {}).get(key)
            background = QtGui.QColor(c['raised'])
            if value is not None:
                background = QtGui.QColor(c['green' if value >= 0 else 'red'])
                background.setAlpha(25 + int(abs(value) / magnitude * 60))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(background)
            p.drawRoundedRect(rect, 5, 5)
            self._text(p, rect.adjusted(9, 4, -9, 4 - height + 20), day, 10, c['muted'])
            if value is not None:
                self.hit_days.append((rect, key))
                self.hover_regions.append((rect, f'{key} UTC · {_performance_number(value, signed=True)} '
                                           f'{s.get("currency", "")} · {s.get("day_counts", {}).get(key, 0)} fills'))
                self._text(p, QtCore.QRectF(rect.x() + 9, rect.y() + height * .35, width - 18, 23),
                           _performance_number(value, signed=True, decimals=0), 16,
                           c['green' if value >= 0 else 'red'], bold=True)
                if height >= 65:
                    self._text(p, QtCore.QRectF(rect.x() + 9, rect.bottom() - 22, width - 18, 17),
                               f"{s.get('day_counts', {}).get(key, 0)} fills", 10, c['muted'])

    def _hours(self, p):
        c, s = self.colors, self.summary
        grid = s.get('hours', [[0.] * 24 for _ in range(7)])
        counts = s.get('hour_counts', [[0] * 24 for _ in range(7)])
        width = (self.width() - 62) / 24
        height = max(8, (self.height() - 95) / 7)
        maximum = max([abs(v) for row in grid for v in row] + [.01])
        self.hover_regions = []
        for day in range(7):
            self._text(p, QtCore.QRectF(16, 53 + day * height, 28, height), 'MTWTFSS'[day], 10, c['muted'])
            for hour in range(24):
                value = grid[day][hour]
                color = QtGui.QColor(c['raised'])
                if counts[day][hour]:
                    color = QtGui.QColor(c['green' if value >= 0 else 'red'])
                    color.setAlpha(40 + int(abs(value) / maximum * 215))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                rect = QtCore.QRectF(44 + hour * width, 53 + day * height, width - 3, height - 3)
                p.drawRoundedRect(rect, 2, 2)
                self.hover_regions.append((rect, f'{("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[day]} '
                                           f'{hour:02}:00–{hour:02}:59 UTC · {_performance_number(value, signed=True)} '
                                           f'{s.get("currency", "")} · {counts[day][hour]} fills'))
        for hour in (0, 6, 12, 18):
            self._text(p, QtCore.QRectF(44 + hour * width, self.height() - 32, 50, 20), f'{hour:02}:00', 10, c['muted'])

    def mouseMoveEvent(self, event):
        if self.kind in {'equity', 'drawdown'}:
            from bisect import bisect_left
            points = self.summary.get('curve' if self.kind == 'equity' else 'dips', [])
            index = -1
            if points and self.plot_rect.contains(event.position()):
                target = points[0][0] + (event.position().x() - self.plot_rect.x()) / self.plot_rect.width() * (points[-1][0] - points[0][0])
                index = min(len(points) - 1, bisect_left(points, target, key=lambda point: point[0]))
            if self.hover_index != index:
                self.hover_index = index
                self.update()
        elif self.kind == 'calendar':
            hit = next((day for rect, day in self.hit_days if rect.contains(event.position())), '')
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)
        detail = next((text for rect, text in self.hover_regions if rect.contains(event.position())), '')
        if detail != self._hover_text:
            self._hover_text = detail
            if detail:
                QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), detail, self)
            else:
                QtWidgets.QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self.kind == 'calendar' and event.button() == Qt.MouseButton.LeftButton:
            day = next((day for rect, day in self.hit_days if rect.contains(event.position())), '')
            if day:
                self.day_selected.emit(day)
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        self._hover_text = ''
        QtWidgets.QToolTip.hideText()
        if self.hover_index >= 0:
            self.hover_index = -1
            self.update()
        super().leaveEvent(event)


class TradingPerformanceWorkspace(QtWidgets.QWidget):
    """Lazy, event-driven performance workspace; no hidden polling or animation."""

    def __init__(self, gateway, settings=None, parent=None):
        super().__init__(parent)
        from datetime import datetime, timezone
        self.gateway, self.settings = gateway, settings
        self.setObjectName('tradingPerformance')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._active = False
        self._task = None
        self._cancelled = None
        self._generation = 0
        self._scope = ''
        self._rows = []
        self._summary = {}
        self._demo_rows = None
        self._again = False
        self._want_network = False
        self._selected_day = ''
        self._warning = ''
        self._variant = max(0, min(2, settings.value('performance/variant_v1', 0, int))) if settings else 0
        now = datetime.now(timezone.utc)
        self._month = QtCore.QDate(now.year, now.month, 1)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pool.setExpiryTimeout(1000)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(180)
        self._refresh_timer.timeout.connect(self._reload)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName('performanceScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        root.addWidget(scroll)
        body = QtWidgets.QWidget()
        body.setObjectName('performanceBody')
        scroll.setWidget(body)
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(24, 22, 24, 24)
        layout.setSpacing(16)
        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel('Performance')
        self.title.setObjectName('performanceTitle')
        header.addWidget(self.title)
        header.addStretch()
        self.demo_button = QtWidgets.QPushButton('Dummy data')
        self.demo_button.setCheckable(True)
        self.demo_button.setObjectName('performanceDemo')
        self.demo_button.setAccessibleName('Toggle dummy performance data')
        self.demo_button.toggled.connect(self._toggle_demo)
        self.variant_button = QtWidgets.QPushButton()
        self.variant_button.setAccessibleName('Switch performance layout variant')
        self.variant_button.clicked.connect(self._cycle_variant)
        header.addWidget(self.demo_button)
        header.addWidget(self.variant_button)
        layout.addLayout(header)
        filters = QtWidgets.QHBoxLayout()
        filters.setSpacing(8)
        self.period = QtWidgets.QComboBox()
        for label, days in [('All time', 0), ('Last 7 days', 7), ('Last 30 days', 30), ('Last 90 days', 90)]:
            self.period.addItem(label, days)
        self.period.setAccessibleName('Performance date range')
        self.market = QtWidgets.QComboBox()
        self.market.addItem('All markets', '')
        self.market.setAccessibleName('Performance market filter')
        self.side = QtWidgets.QComboBox()
        for label, value in [('Both sides', ''), ('Buy executions', 'BUY'), ('Sell executions', 'SELL')]:
            self.side.addItem(label, value)
        self.side.setAccessibleName('Performance execution side')
        self.currency = QtWidgets.QComboBox()
        self.currency.addItems(['USDT', 'USDC'])
        self.currency.setAccessibleName('Performance settlement currency')
        for widget in (self.period, self.market, self.side, self.currency):
            widget.currentIndexChanged.connect(self._filter_changed)
            filters.addWidget(widget)
        filters.addStretch()
        self.source_label = QtWidgets.QLabel()
        self.source_label.setObjectName('performanceMuted')
        filters.addWidget(self.source_label)
        self.refresh_button = QtWidgets.QPushButton('Refresh')
        self.refresh_button.clicked.connect(lambda: self._schedule(reconcile=True, immediate=True))
        filters.addWidget(self.refresh_button)
        layout.addLayout(filters)
        self.notice = QtWidgets.QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName('performanceNotice')
        layout.addWidget(self.notice)
        self.visual_host = QtWidgets.QWidget()
        self.visual_layout = QtWidgets.QGridLayout(self.visual_host)
        self.visual_layout.setContentsMargins(0, 0, 0, 0)
        self.visual_layout.setSpacing(12)
        layout.addWidget(self.visual_host)
        table_header = QtWidgets.QHBoxLayout()
        self.history_title = QtWidgets.QLabel('Execution history')
        self.history_title.setObjectName('performanceSection')
        table_header.addWidget(self.history_title)
        table_header.addStretch()
        self.day_button = QtWidgets.QPushButton('Clear day filter')
        self.day_button.clicked.connect(lambda: self._select_day(''))
        self.day_button.hide()
        table_header.addWidget(self.day_button)
        self.history_count = QtWidgets.QLabel()
        self.history_count.setObjectName('performanceMuted')
        table_header.addWidget(self.history_count)
        layout.addLayout(table_header)
        self.table = QtWidgets.QTableView()
        self.table.setObjectName('performanceTable')
        self.table.setMinimumHeight(290)
        self.table.setMaximumHeight(430)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.model = _PerformanceTableModel(self.table)
        self.table.setModel(self.model)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        header_view.setStretchLastSection(True)
        for col, width in enumerate((195, 115, 115, 120, 105, 135, 155, 140)):
            self.table.setColumnWidth(col, width)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        layout.addWidget(self.table)
        self.details = QtWidgets.QLabel()
        self.details.setObjectName('performanceMuted')
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self._plots = []
        self._metrics = {}
        self._build_variant()
        gateway.performance_changed.connect(self._history_changed)
        gateway.performance_problem.connect(self._history_problem)
        gateway.credentials_changed.connect(self._account_changed)
        QtWidgets.QApplication.instance().aboutToQuit.connect(lambda: self.set_active(False))

    def _build_variant(self):
        while self.visual_layout.count():
            widget = self.visual_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for index in range(6):
            self.visual_layout.setColumnStretch(index, 0)
            self.visual_layout.setRowStretch(index, 0)
        self._plots, self._metrics = [], {}
        self.calendar = None
        c = _PERFORMANCE_STYLES[self._variant]
        self.variant_button.setText(f'Layout {self._variant + 1} / 3 · {c["name"]}  ↻')
        self.setStyleSheet(f'''
            QWidget#tradingPerformance, QWidget#performanceBody, QScrollArea#performanceScroll {{ background: {c['bg']}; color: {c['text']}; border: 0; }}
            QWidget#tradingPerformance QLabel {{ background: transparent; color: {c['text']}; border: 0; padding: 0; font: 12px Inter; }}
            QWidget#tradingPerformance QLabel#performanceTitle {{ font: 600 27px Inter; }}
            QWidget#tradingPerformance QLabel#performanceSection {{ font: 600 15px Inter; }}
            QWidget#tradingPerformance QLabel#performanceMuted {{ color: {c['muted']}; font: 11px Inter; }}
            QWidget#tradingPerformance QLabel#performanceNotice {{ color: {c['accent']}; padding: 8px 12px; background: {c['raised']}; border-radius: 5px; }}
            QWidget#tradingPerformance QPushButton, QWidget#tradingPerformance QComboBox {{
                color: {c['text']}; background: {c['card']}; border: 1px solid {c['border']}; border-radius: 5px;
                font: 12px Inter; padding: 8px 12px; min-height: 17px;
            }}
            QWidget#tradingPerformance QPushButton:hover, QWidget#tradingPerformance QComboBox:hover {{ border-color: {c['accent']}; background: {c['raised']}; }}
            QWidget#tradingPerformance QPushButton:focus, QWidget#tradingPerformance QComboBox:focus {{ border-color: {c['accent']}; }}
            QWidget#tradingPerformance QPushButton:checked {{ color: {c['bg']}; background: {c['accent']}; border-color: {c['accent']}; }}
            QWidget#tradingPerformance QPushButton:disabled {{ color: {c['muted']}; }}
            QWidget#tradingPerformance QComboBox::drop-down {{ width: 0; border: 0; }}
            QWidget#tradingPerformance QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
            QWidget#tradingPerformance QComboBox QAbstractItemView {{ color: {c['text']}; background: {c['card']}; selection-background-color: {c['raised']}; border: 1px solid {c['border']}; padding: 5px; }}
            QTableView#performanceTable {{ color: {c['text']}; background: {c['card']}; alternate-background-color: {c['bg']}; border: 1px solid {c['border']}; border-radius: 5px; selection-background-color: {c['raised']}; font: 12px Iosevka; }}
            QTableView#performanceTable::item {{ padding: 0 10px; border: 0; }}
            QTableView#performanceTable QHeaderView::section {{ background: {c['card']}; color: {c['muted']}; border: 0; border-bottom: 1px solid {c['border']}; padding: 12px 10px; font: 11px Inter; }}
            QWidget#tradingPerformance QScrollBar:vertical {{ background: {c['bg']}; width: 8px; border: 0; }}
            QWidget#tradingPerformance QScrollBar::handle:vertical {{ background: {c['border']}; min-height: 24px; border-radius: 4px; }}
            QWidget#tradingPerformance QScrollBar::add-line:vertical, QWidget#tradingPerformance QScrollBar::sub-line:vertical {{ height: 0; }}
        ''')

        def plot(kind, title, row, col, rows=1, cols=1, height=220):
            widget = _PerformancePlot(kind, title)
            widget.setMinimumHeight(height)
            self.visual_layout.addWidget(widget, row, col, rows, cols)
            self._plots.append(widget)
            return widget

        def metric(key, title, row, col, cols=1):
            widget = plot('metric', title, row, col, cols=cols, height=116)
            widget.setMaximumHeight(128)
            self._metrics[key] = widget
            return widget

        if self._variant == 0:
            for col, (key, title) in enumerate((('average', 'Average exit'), ('factor', 'Exit profit factor'),
                                               ('drawdown', 'Peak drawdown'), ('volume', 'Executed volume'))):
                metric(key, title, 0, col)
                self.visual_layout.setColumnStretch(col, 1)
            plot('equity', 'Cumulative P&L', 1, 0, cols=3, height=350)
            plot('ring', 'Outcome distribution', 1, 3, height=350)
            plot('daily', 'Daily performance', 2, 0, cols=2, height=230)
            plot('markets', 'Market contribution · top 5', 2, 2, cols=2, height=230)
        elif self._variant == 1:
            metric('net', 'Net realized · selected period', 0, 0)
            metric('win_rate', 'Exit win rate', 1, 0)
            metric('average', 'Average exit', 2, 0)
            metric('fees', 'Trading fees', 3, 0)
            container = QtWidgets.QWidget()
            column = QtWidgets.QVBoxLayout(container)
            column.setContentsMargins(0, 0, 0, 0)
            nav = QtWidgets.QHBoxLayout()
            previous = QtWidgets.QPushButton('‹')
            previous.setAccessibleName('Previous performance month')
            previous.clicked.connect(lambda: self._change_month(-1))
            following = QtWidgets.QPushButton('›')
            following.setAccessibleName('Next performance month')
            following.clicked.connect(lambda: self._change_month(1))
            self.month_label = QtWidgets.QLabel()
            self.month_label.setObjectName('performanceSection')
            nav.addWidget(self.month_label)
            nav.addStretch()
            nav.addWidget(previous)
            nav.addWidget(following)
            column.addLayout(nav)
            self.calendar = _PerformancePlot('calendar', 'Daily net P&L · click a day to inspect fills')
            self.calendar.setMinimumHeight(460)
            self.calendar.day_selected.connect(self._select_day)
            self._plots.append(self.calendar)
            column.addWidget(self.calendar, 1)
            self.visual_layout.addWidget(container, 0, 1, 4, 3)
            self._change_month(0)
            self.visual_layout.setColumnStretch(0, 1)
            for col in (1, 2, 3):
                self.visual_layout.setColumnStretch(col, 1)
            plot('equity', 'Cumulative P&L', 4, 0, cols=4, height=270)
        else:
            plot('equity', 'P&L / execution curve', 0, 0, rows=2, cols=2, height=270)
            metric('factor', 'Exit profit factor', 0, 2)
            metric('win_rate', 'Exit win rate', 0, 3)
            metric('drawdown', 'Peak drawdown', 1, 2)
            metric('maker', 'Maker executions', 1, 3)
            plot('hours', 'Execution P&L / weekday × hour · UTC', 2, 0, cols=2, height=248)
            plot('markets', 'Market attribution · top 5', 2, 2, cols=2, height=248)
            plot('drawdown', 'Drawdown from P&L high', 3, 0, cols=4, height=175)
            for col in range(4):
                self.visual_layout.setColumnStretch(col, 1)
        if self._summary:
            self._present(self._summary)
        else:
            for widget in self._plots:
                widget.configure({}, c)

    def _change_month(self, delta):
        self._month = self._month.addMonths(delta)
        if self.calendar is not None:
            self.calendar.month = self._month
            self.month_label.setText(self._month.toString('MMMM yyyy'))
            self.calendar.update()

    def _cycle_variant(self):
        self._variant = (self._variant + 1) % len(_PERFORMANCE_STYLES)
        if self.settings:
            self.settings.setValue('performance/variant_v1', self._variant)
        self._build_variant()

    def _filters(self):
        return (int(self.period.currentData() or 0), str(self.market.currentData() or ''),
                str(self.side.currentData() or ''), self.currency.currentText())

    def _filter_changed(self):
        self._selected_day = ''
        self._schedule(immediate=True)

    def _toggle_demo(self):
        self._cancel_work()
        self._selected_day = ''
        self._warning = ''
        self._summary = {}
        self.model.set_rows([], _PERFORMANCE_STYLES[self._variant], self.currency.currentText())
        for widget in self._plots:
            widget.configure({}, _PERFORMANCE_STYLES[self._variant])
        self.notice.setText('Preparing dummy data…' if self.demo_button.isChecked() else 'Loading recorded execution history…')
        self.notice.show()
        self._schedule(reconcile=not self.demo_button.isChecked(), immediate=True)

    def _account_changed(self, *_args):
        self._cancel_work()
        self._rows, self._summary, self._scope = [], {}, ''
        self.model.set_rows([], _PERFORMANCE_STYLES[self._variant], self.currency.currentText())
        for widget in self._plots:
            widget.configure({}, _PERFORMANCE_STYLES[self._variant])
        self._schedule(reconcile=True, immediate=True)

    def _history_changed(self):
        if not self.demo_button.isChecked():
            self._schedule(reconcile=True)

    def _history_problem(self, message):
        self._warning = message
        if self._active and not self.demo_button.isChecked():
            self.notice.setText(message)
            self.notice.show()

    def _schedule(self, *, reconcile=False, immediate=False):
        if not self._active:
            return
        self._want_network |= reconcile
        if self._task is not None:
            self._again = True
        elif not self._refresh_timer.isActive() or immediate:
            self._refresh_timer.start(0 if immediate else 180)

    def _cancel_work(self):
        self._refresh_timer.stop()
        self._generation += 1
        if self._cancelled is not None:
            self._cancelled.set()
        if self._task is not None:
            self._task.cancel()
        self._task = None
        self._again = self._want_network = False
        self.refresh_button.setEnabled(True)

    def set_active(self, active):
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if active:
            self._schedule(reconcile=True, immediate=True)
        else:
            self._cancel_work()

    def hideEvent(self, event):
        self.set_active(False)
        super().hideEvent(event)

    def showEvent(self, event):
        self.set_active(True)
        super().showEvent(event)

    def _reload(self):
        if not self._active or self._task is not None:
            return
        import threading
        from ..networking.binance import launch_task
        self._generation += 1
        generation = self._generation
        cancelled = self._cancelled = threading.Event()
        filters, demo = self._filters(), self.demo_button.isChecked()
        reconcile, self._want_network = self._want_network, False
        journal = getattr(self.gateway, '_journal', None)
        key, secret = self.gateway.api_key, self.gateway.api_secret
        rest = self.gateway.rest
        self.refresh_button.setEnabled(False)
        self.source_label.setText('Loading history…' if not demo else 'Preparing demo…')
        demo_rows = self._demo_rows

        def read():
            warnings = []
            if demo:
                rows = demo_rows if demo_rows is not None else _performance_demo()
                scope = 'demo'
            elif journal is not None:
                if reconcile and key and secret and not cancelled.is_set():
                    warnings = journal.reconcile_performance(rest, key, secret, cancelled)
                rows = journal.performance_snapshot()['fills'] if not cancelled.is_set() else []
                scope = journal.scope
            else:
                rows, scope = [], ''
            if cancelled.is_set():
                raise RuntimeError('Performance read cancelled.')
            return rows, scope, _performance_summary(rows, *filters), warnings

        def done(result):
            if generation != self._generation or not self._active:
                return
            self._task = None
            self.refresh_button.setEnabled(True)
            rows, scope, summary, warnings = result
            if demo:
                self._demo_rows = rows
            else:
                self._rows, self._scope = rows, scope
            self._warning = '; '.join(warnings[:3])
            if filters == self._filters() and demo == self.demo_button.isChecked():
                self._present(summary)
            else:
                self._again = True
            if self._again:
                self._again = False
                self._schedule(immediate=True)

        def failed(message):
            if generation != self._generation or not self._active:
                return
            self._task = None
            self.refresh_button.setEnabled(True)
            self._warning = f'History could not be refreshed: {message}'
            self.notice.setText(self._warning)
            self.notice.show()
            self.source_label.setText('History unavailable' if not self._summary else 'Showing previous history')

        self._task = launch_task(read, done, failed, self._pool, defer_rate_waits=True,
                                 source='trading performance', valid=lambda: self._active and generation == self._generation)

    def _present(self, summary):
        self._summary = summary
        c = _PERFORMANCE_STYLES[self._variant]
        unit = summary['currency']
        available = summary['all_markets']
        selected = self.market.currentData() or ''
        if available != [self.market.itemData(i) for i in range(1, self.market.count())]:
            blocker = QtCore.QSignalBlocker(self.market)
            self.market.clear()
            self.market.addItem('All markets', '')
            for market in available:
                self.market.addItem(market, market)
            self.market.setCurrentIndex(max(0, self.market.findData(selected)))
            del blocker
            if selected and selected not in available and available:
                self._schedule(immediate=True)
        factor = summary['factor']
        definitions = {
            'net': (_performance_number(summary['net'], signed=True), unit, c['green' if summary['net'] >= 0 else 'red']),
            'win_rate': (f"{summary['win_rate']:.1f}%" if summary['win_rate'] is not None else '—',
                         f"{summary['wins']} wins / {summary['exits']} exit orders", ''),
            'factor': ('—' if factor is None else ('∞' if factor == float('inf') else f'{factor:.2f}'),
                       'Exit gains / exit losses', c['accent']),
            'drawdown': (_performance_number(summary['drawdown']), f'{unit} · peak to trough', c['red']),
            'volume': (human_number(summary['volume']), f'{unit} · filled notional', ''),
            'average': (_performance_number(summary['average'], signed=True) if summary['average'] is not None else '—',
                        f'{unit} · per exit order', ''),
            'fees': (_performance_number(summary['fees']), f'{unit} · paid on recorded fills', ''),
            'maker': (f"{summary['maker']:.1f}%", 'Share of recorded fills', c['accent']),
        }
        for key, widget in self._metrics.items():
            widget.metric_values = definitions[key]
        for widget in self._plots:
            widget.configure(summary, c)
        demo = self.demo_button.isChecked()
        self.source_label.setText('DEMO / synthetic executions' if demo else
                                  f"Nightwatch orders / {'Testnet' if self.gateway.testnet else 'Mainnet'} / UTC")
        warning = self._warning or (getattr(self.gateway, 'performance_error', '') if not demo else '')
        if demo:
            notice = 'Dummy data is on. These synthetic trades are separate from your recorded history.'
        elif warning:
            notice = f'Saved executions are shown. History recovery is incomplete: {warning}'
        elif not summary['rows']:
            notice = ('No recorded fills match these filters.' if self._rows else
                      'No recorded executions yet. Trades placed through Nightwatch will appear here; use Dummy data to explore the layouts.')
        else:
            notice = ''
        self.notice.setText(notice)
        self.notice.setVisible(bool(notice))
        fees = ' · '.join(f"{_performance_number(value, decimals=8).rstrip('0').rstrip('.')} {asset}"
                          for asset, value in sorted(summary['fee_assets'].items())) or '0'
        foreign = any(asset != unit and value for asset, value in summary['fee_assets'].items())
        self.details.setText(f'Fees paid: {fees}.  '
                             + (f'Non-{unit} fees are listed separately and are not converted into P&L.  ' if foreign else '')
                             + 'P&L excludes funding and unrealized positions. Exit statistics combine partial fills by exit order, '
                               'after its settlement-currency fees. History covers orders recorded by Nightwatch from this version onward.')
        self._select_day(self._selected_day)

    def _select_day(self, day):
        self._selected_day = day
        rows = self._summary.get('rows', [])
        if day:
            rows = [row for row in rows if row['day'] == day]
        self.model.set_rows(list(rows), _PERFORMANCE_STYLES[self._variant], self.currency.currentText())
        self.model.sort(self.table.horizontalHeader().sortIndicatorSection(), self.table.horizontalHeader().sortIndicatorOrder())
        self.day_button.setVisible(bool(day))
        self.history_title.setText('Execution history' + (f' / {day}' if day else ''))
        self.history_count.setText(f'{len(rows):,} fills')
