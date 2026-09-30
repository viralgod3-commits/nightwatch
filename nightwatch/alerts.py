"""Alert delivery, price/zone monitoring, and alert presentation."""

from __future__ import annotations

import os
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Signal

from .constants import DEFAULT_INTERVAL, DEFAULT_SYMBOL
from .models import PriceAlert, Zone
from .networking.binance import ApiTask, http_json, launch_task
from .utilities import TextRole, set_text_role
from .models import format_price, utc_stamp


class PriceAlertDialog(QtWidgets.QDialog):
    def __init__(self, current_price: float, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add price alert")
        screen = parent.screen() if parent is not None else QtGui.QGuiApplication.primaryScreen()
        available_width = screen.availableGeometry().width() if screen is not None else 460
        self.setMinimumWidth(max(300, min(420, available_width - 40)))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("PRICE ALERT")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel("Trigger once when the live price crosses the selected level.")
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        self.price = QtWidgets.QDoubleSpinBox()
        self.price.setDecimals(10)
        self.price.setRange(0.00000001, 1_000_000_000)
        self.price.setValue(max(current_price, 0.00000001))


        self.price.setMinimumWidth(0)
        self.price.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.price.setToolTip("The exact price level monitored against the live last-traded price.")
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(("Any crossing", "Crossing up", "Crossing down"))
        self.direction.setToolTip(
            "Any crossing triggers from either side. Crossing up only triggers from below; crossing down only from above."
        )
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow("Price", self.price)
        form.addRow("Trigger", self.direction)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)


class AlertSettingsDialog(QtWidgets.QDialog):
    def __init__(self, center: AlertCenter, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.center = center
        self.setWindowTitle("Alert delivery")
        self.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("ALERT DELIVERY")
        heading.setObjectName("dialogHeading")
        destinations = QtWidgets.QGroupBox("Destinations")
        destination_layout = QtWidgets.QHBoxLayout(destinations)
        self.checks: dict[str, QtWidgets.QCheckBox] = {}
        for key, label in (
            ("in_app", "In app"),
            ("sound", "Sound"),
            ("telegram", "Telegram"),
        ):
            checkbox = QtWidgets.QCheckBox(label)
            checkbox.setChecked(center.destinations[key])
            self.checks[key] = checkbox
            destination_layout.addWidget(checkbox)
        self.checks["sound"].setToolTip("Play the Windows notification sound when an alert fires.")
        self.checks["telegram"].setToolTip(
            "Send the alert through the Telegram bot token and chat ID entered below."
        )
        test_sound = QtWidgets.QPushButton("TEST SOUND")
        test_sound.setToolTip("Play the same notification sound used by alerts.")
        test_sound.clicked.connect(QtWidgets.QApplication.beep)
        destination_layout.addStretch(1)
        destination_layout.addWidget(test_sound)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        self.token = QtWidgets.QLineEdit(center.telegram_token)
        self.token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.token.setToolTip("Session-only Telegram bot token. It is not written to disk.")
        self.chat_id = QtWidgets.QLineEdit(center.telegram_chat_id)
        self.chat_id.setToolTip("Telegram chat or channel ID that should receive alerts.")
        form.addRow("Telegram token", self.token)
        form.addRow("Telegram chat ID", self.chat_id)
        note = QtWidgets.QLabel(
            "Delivery choices apply to custom price alerts and automatic chart alerts for this session. Telegram tokens are never saved to disk."
        )
        note.setWordWrap(True)
        note.setObjectName("subtleLabel")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(destinations)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        if self.checks["telegram"].isChecked() and (
            not self.token.text().strip() or not self.chat_id.text().strip()
        ):
            QtWidgets.QMessageBox.warning(
                self,
                "Telegram details required",
                "Enter both the Telegram bot token and chat ID, or disable Telegram delivery.",
            )
            return
        self.accept()

    def apply(self) -> None:
        self.center.destinations = {key: box.isChecked() for key, box in self.checks.items()}
        self.center.telegram_token = self.token.text().strip()
        self.center.telegram_chat_id = self.chat_id.text().strip()


class AlertsPanel(QtWidgets.QWidget):
    cancel_all_requested = Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(6)
        self.add_button = QtWidgets.QPushButton("+ ALERT")
        self.add_button.setToolTip("Create a one-time alert when the active symbol crosses a price.")
        self.settings_button = QtWidgets.QPushButton("DELIVERY")
        self.settings_button.setToolTip("Choose in-app, sound and Telegram alert delivery.")
        self.cancel_button = QtWidgets.QPushButton("CANCEL ALL")
        self.cancel_button.setToolTip("Cancel every active custom price alert.")
        self.clear_button = QtWidgets.QToolButton()
        self.clear_button.setText("CLEAR")
        self.clear_button.setToolTip("Clear fired and status messages without cancelling active alerts.")
        self.active_count = QtWidgets.QLabel("ARMED 0")
        self.active_count.setObjectName("tradeFieldLabel")
        set_text_role(self.active_count, TextRole.UI_LABEL)
        controls.addWidget(self.add_button)
        controls.addWidget(self.settings_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch()
        controls.addWidget(self.active_count)
        controls.addWidget(self.clear_button)
        self.list = QtWidgets.QListWidget()
        set_text_role(self.list, TextRole.ALERT_TEXT)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.list.setWordWrap(True)
        self.list.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.list.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.list.setSpacing(2)
        self.list.setMinimumHeight(0)
        self.list.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        self.setMinimumHeight(0)
        self.clear_button.clicked.connect(self.list.clear)
        self.cancel_button.clicked.connect(self.cancel_all_requested.emit)
        layout.addLayout(controls)
        layout.addWidget(self.list)
        self.set_active_count(0)

    def set_active_count(self, count: int) -> None:
        count = max(0, int(count))
        self.active_count.setText(f"ARMED {count}")
        self.cancel_button.setEnabled(count > 0)

    def append_alert(self, title: str, detail: str) -> None:
        item = QtWidgets.QListWidgetItem(
            f"{datetime.now().astimezone().strftime('%H:%M:%S')}  {title}\n{detail}"
        )
        item.setToolTip(detail)
        self._size_item(item)
        self.list.insertItem(0, item)
        while self.list.count() > 100:
            self.list.takeItem(self.list.count() - 1)

    def _size_item(self, item: QtWidgets.QListWidgetItem) -> None:
        width = max(180, self.list.viewport().width() - 18)
        height = self.list.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, width, 1_000),
            QtCore.Qt.TextFlag.TextWordWrap,
            item.text(),
        ).height()
        item.setSizeHint(QtCore.QSize(0, max(42, height + 10)))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        for row in range(self.list.count()):
            self._size_item(self.list.item(row))


class AlertCenter(QtCore.QObject):
    in_app = Signal(str, str)
    manual_alerts_changed = Signal(int)

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.symbol = DEFAULT_SYMBOL
        self.interval = DEFAULT_INTERVAL
        self.last_price: float | None = None
        self.manual: list[PriceAlert] = []
        self.zones: list[Zone] = []
        self.profile_levels: list[float] = []
        self.zone_states: dict[str, tuple[bool, bool]] = {}
        self.last_fired: OrderedDict[str, float] = OrderedDict()
        self.telegram_token = os.getenv("CHART_TELEGRAM_TOKEN", "") or os.getenv("TOKEN_PUMP", "")
        self.telegram_chat_id = os.getenv("CHART_TELEGRAM_CHAT_ID", "")
        self.destinations = {
            "in_app": True,
            "sound": False,
            "telegram": bool(self.telegram_token and self.telegram_chat_id),
        }
        self.tasks: set[ApiTask] = set()

    def set_market(self, symbol: str, interval: str) -> None:
        self.symbol = symbol
        self.interval = interval
        self.reset_crossing_state()

    def reset_crossing_state(self) -> None:
        """The first price after a feed gap establishes a new crossing baseline."""
        self.last_price = None
        self.zone_states.clear()

    def add_price_alert(self, level: float, direction: str) -> None:
        self.manual.append(PriceAlert(self.symbol, level, direction))
        self.manual_alerts_changed.emit(self.active_manual_count())
        self.in_app.emit("PRICE ALERT ARMED", f"{self.symbol} · {format_price(level)} · {direction}")

    def active_manual_count(self) -> int:
        return sum(1 for rule in self.manual if rule.active)

    def clear_manual_alerts(self) -> None:
        count = self.active_manual_count()
        for rule in self.manual:
            rule.active = False
        self.manual_alerts_changed.emit(0)
        if count:
            self.in_app.emit("PRICE ALERTS CANCELLED", f"{count} active custom alert{'s' if count != 1 else ''} cancelled")

    def set_analysis(self, zones: list[Zone], levels: list[float]) -> None:
        self.zones = list(zones)
        self.profile_levels = list(levels)
        keys = {f"zone:{zone.kind}:{zone.timeframe}:{zone.low:.10g}:{zone.high:.10g}" for zone in self.zones}
        self.zone_states = {key: value for key, value in self.zone_states.items() if key in keys}

    def check_price(self, price: float) -> None:
        previous = self.last_price
        self.last_price = price
        if previous is None or price <= 0:
            return
        for rule in self.manual:
            if not rule.active or rule.symbol != self.symbol:
                continue
            crossed_up = previous < rule.level <= price
            crossed_down = previous > rule.level >= price
            matches = (
                (rule.direction == "Any crossing" and (crossed_up or crossed_down))
                or (rule.direction == "Crossing up" and crossed_up)
                or (rule.direction == "Crossing down" and crossed_down)
            )
            if matches:
                rule.active = False
                self.manual_alerts_changed.emit(self.active_manual_count())
                self.dispatch("PRICE LEVEL CROSSED", price, f"Level {format_price(rule.level)}")
        self._check_zones(previous, price)
        self._check_profiles(previous, price)

    def _check_zones(self, previous: float, price: float) -> None:
        for zone in self.zones:
            key = f"zone:{zone.kind}:{zone.timeframe}:{zone.low:.10g}:{zone.high:.10g}"
            inside = zone.low <= price <= zone.high
            broken = price < zone.low if zone.kind == "support" else price > zone.high
            previous_broken = previous < zone.low if zone.kind == "support" else previous > zone.high
            old_inside, old_broken = self.zone_states.get(
                key,
                (zone.low <= previous <= zone.high, previous_broken),
            )
            if inside and not old_inside:
                self.dispatch(
                    f"{zone.kind.upper()} LEVEL TOUCHED",
                    price,
                    f"{zone.timeframe.upper()} · {format_price(zone.low)}–{format_price(zone.high)}",
                    key + ":touch",
                    900,
                )
            crossed_boundary = (
                zone.kind == "support" and previous >= zone.low
            ) or (
                zone.kind == "resistance" and previous <= zone.high
            )
            if broken and not old_broken and crossed_boundary:
                self.dispatch(
                    f"{zone.kind.upper()} LEVEL BROKEN",
                    price,
                    f"{zone.timeframe.upper()} · {'below' if zone.kind == 'support' else 'above'} "
                    f"{format_price(zone.low if zone.kind == 'support' else zone.high)}",
                    key + ":break",
                    900,
                )
            self.zone_states[key] = (inside, broken)

    def _check_profiles(self, previous: float, price: float) -> None:
        for level in self.profile_levels:
            crossed = (previous - level) * (price - level) <= 0
            touched = abs(price - level) / max(level, 1e-12) <= 0.0006
            if crossed or touched:
                self.dispatch(
                    "VOLUME PROFILE LEVEL",
                    price,
                    f"High-volume node {format_price(level)}",
                    f"profile:{level:.10g}",
                    1200,
                )

    def dispatch(
        self,
        title: str,
        price: float,
        detail: str,
        key: str | None = None,
        cooldown: int = 0,
    ) -> None:
        self.dispatch_market(
            self.symbol,
            self.interval,
            title,
            price,
            detail,
            key,
            cooldown,
        )

    def dispatch_market(
        self,
        symbol: str,
        interval: str,
        title: str,
        price: float,
        detail: str,
        key: str | None = None,
        cooldown: int = 0,
        destinations: dict[str, bool] | None = None,
    ) -> None:
        key = key or f"manual:{title}:{price}"
        now = time.time()
        if cooldown and now - self.last_fired.get(key, 0.0) < cooldown:
            return
        self.last_fired[key] = now
        self.last_fired.move_to_end(key)
        while len(self.last_fired) > 4096:
            self.last_fired.popitem(last=False)
        delivery = destinations or self.destinations
        compact = f"{symbol} · {format_price(price)} · {detail}"
        if delivery.get("in_app", False):
            self.in_app.emit(title, compact)
        if delivery.get("sound", False):
            QtWidgets.QApplication.beep()
        if delivery.get("telegram", False) and self.telegram_token and self.telegram_chat_id:
            self._send_telegram(title, price, detail, symbol, interval)

    def _send_telegram(
        self,
        title: str,
        price: float,
        detail: str,
        symbol: str | None = None,
        interval: str | None = None,
    ) -> None:
        token = self.telegram_token
        chat_id = self.telegram_chat_id
        symbol = symbol or self.symbol
        interval = interval or self.interval
        text = (
            f"🔔 <b>{symbol} · {title}</b>\n"
            f"Price: <code>{format_price(price).replace(',', '')}</code>\n"
            f"{detail}\n"
            f"{interval} · Binance USD-M\n"
            f"🕒 {utc_stamp()}\n\n"
            f'<a href="https://app.binance.com/futures/{symbol}">TRADE NOW</a>'
        )

        def send() -> Any:
            body = urllib.parse.urlencode(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                }
            ).encode()
            return http_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body,
            )

        task: ApiTask

        def done(_result: Any) -> None:
            self.tasks.discard(task)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.in_app.emit("TELEGRAM DELIVERY FAILED", message)

        task = launch_task(send, done, failed)
        self.tasks.add(task)
