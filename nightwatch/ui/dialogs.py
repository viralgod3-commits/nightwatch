"""Shell configuration dialogs and news ribbon."""
from __future__ import annotations


from collections import deque
from pathlib import Path
import math
import time

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal

from ..utilities import TextRole, apply_text_render_hints, set_text_role
from ..utilities import device_pixel_value


class MicrostructureNewsCard(QtWidgets.QFrame):
    clicked = Signal()


    _IDLE_LOGO_HEIGHT = 67
    _TICKER_SIDE_INSET = 0.0
    _TICKER_PASSES = 20
    _FALLBACK_REFRESH_HZ = 60.0

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0.0
        self._text_width = 0
        self._passes_remaining = 0
        self._copies_started = 0
        self._color = QtGui.QColor("#f0a020")
        self._pending: deque[tuple[str, str]] = deque(maxlen=4)
        self._hover_html = ""
        self._idle_logo = QtGui.QPixmap(
            str(Path(__file__).resolve().parent.parent / "media" / "logo.png")
        )
        self._idle_logo_scaled = QtGui.QPixmap()
        if not self._idle_logo.isNull():
            self._idle_logo_scaled = self._idle_logo.scaledToHeight(
                self._IDLE_LOGO_HEIGHT,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._idle_wrapped = False
        self._scroll_speed = 45.0
        self._last_frame = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._update_frame_interval()
        self._timer.timeout.connect(self._advance)
        set_text_role(self, TextRole.NEWS_TEXT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Current-symbol microstructure signals")
        self._restart_idle_logo()

    def _update_frame_interval(self) -> None:
        """Run ticker updates at the active monitor refresh rate or faster."""
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        refresh_hz = float(screen.refreshRate()) if screen is not None else self._FALLBACK_REFRESH_HZ
        if not math.isfinite(refresh_hz) or refresh_hz <= 0.0:
            refresh_hz = self._FALLBACK_REFRESH_HZ


        interval_ms = max(1, int(1000.0 / refresh_hz))
        self._timer.setInterval(interval_ms)

    def _restart_idle_logo(self) -> None:
        """Run logo.png through the same right-to-left track used by ticker text."""
        self._text = ""
        self._text_width = (
            self._idle_logo_scaled.width() if not self._idle_logo_scaled.isNull() else 0
        )
        _left, right = self._ticker_bounds()
        self._offset = right
        self._idle_wrapped = False
        self._last_frame = time.monotonic()
        if self._text_width > 0 and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self._update_frame_interval()
        self._last_frame = time.monotonic()
        if not self._text:

            self._restart_idle_logo()
        elif self._text_width > 0:
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:


        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        old_width = max(1.0, float(event.oldSize().width()))
        old_left = self._TICKER_SIDE_INSET
        old_right = max(old_left, old_width - self._TICKER_SIDE_INSET)
        old_span = max(1.0, old_right - old_left)
        phase = (self._offset - old_left) / old_span
        super().resizeEvent(event)
        left, right = self._ticker_bounds()
        new_span = max(1.0, right - left)
        self._offset = left + phase * new_span
        self.update()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QtCore.QEvent.Type.FontChange,
            QtCore.QEvent.Type.ApplicationFontChange,
        ) and self._text:
            self._text_width = self.fontMetrics().horizontalAdvance(self._text)
            self.update()


    def show_signal(self, text: str, color: str) -> None:
        text = str(text)
        if self._text:
            if text != self._text and all(text != item[0] for item in self._pending):
                self._pending.append((text, color))
            return
        self._start_signal(text, color)

    def _ticker_bounds(self) -> tuple[float, float]:
        """Return the exact painted marquee track, matching the text clip rect."""
        left = self._TICKER_SIDE_INSET
        right = max(left, float(self.width()) - self._TICKER_SIDE_INSET)
        return left, right

    def _ticker_repeat_span(self) -> float:
        """Return the exact visible ticker-track width.

        The wrap phase is tied only to the painted left/right edges, never to the
        message width. The instant the leading edge starts crossing the left edge,
        the same message starts entering from the right edge. This keeps short and
        long alerts on the same deterministic phase with no width-dependent delay.
        """
        left, right = self._ticker_bounds()
        return max(1.0, right - left)

    def _start_signal(self, text: str, color: str) -> None:
        self._text = text
        self._color = QtGui.QColor(color)
        self._text_width = self.fontMetrics().horizontalAdvance(self._text)
        _left, right = self._ticker_bounds()
        self._offset = right
        self._passes_remaining = self._TICKER_PASSES
        self._copies_started = 1
        self._last_frame = time.monotonic()
        if self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def clear_signal(self) -> None:
        self._pending.clear()
        self._stop_signal()

    def discard_pending(self) -> None:
        """Drop queued signals without disturbing the item currently scrolling."""
        self._pending.clear()

    def set_detail_html(self, detail: str) -> None:
        self._hover_html = str(detail)

    def enterEvent(self, event: QtCore.QEvent) -> None:
        super().enterEvent(event)
        if (
            self._hover_html
            and not bool(self.window().property("chartTooltipsSuppressed"))
        ):
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), self._hover_html, self)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)

    def _stop_signal(self) -> None:
        self._passes_remaining = 0
        self._copies_started = 0
        self._restart_idle_logo()

    def _advance(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_frame)
        self._last_frame = now

        if not self._text:


            self._offset -= self._scroll_speed * elapsed
            left, _right = self._ticker_bounds()
            span = self._ticker_repeat_span()
            while self._offset <= left:
                self._offset += span
                self._idle_wrapped = True
            self.update()
            return


        self._offset -= self._scroll_speed * elapsed
        left, _right = self._ticker_bounds()
        span = self._ticker_repeat_span()


        while self._passes_remaining > 1 and self._offset <= left:
            self._offset += span
            self._passes_remaining -= 1
            self._copies_started += 1

        if self._passes_remaining == 1 and self._offset + self._text_width < left:
            if self._pending:
                self._start_signal(*self._pending.popleft())
            else:
                self._stop_signal()
            return

        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)

        if not self._text:
            if self._idle_logo_scaled.isNull():
                return
            left, right = self._ticker_bounds()
            painter.setClipRect(
                QtCore.QRectF(left, 0.0, max(0.0, right - left), float(self.height()))
            )
            span = self._ticker_repeat_span()
            y = (self.height() - self._idle_logo_scaled.height()) / 2.0
            positions = [self._offset]
            if self._idle_wrapped:
                positions.append(self._offset - span)
            for x in positions:
                if x > right or x + self._idle_logo_scaled.width() < left:
                    continue
                painter.drawPixmap(QtCore.QPointF(x, y), self._idle_logo_scaled)
            return

        apply_text_render_hints(painter)
        painter.setFont(self.font())
        clip = QtCore.QRectF(self.rect()).adjusted(
            self._TICKER_SIDE_INSET, 0.0, -self._TICKER_SIDE_INSET, 0.0
        )
        painter.setClipRect(clip)
        painter.setPen(self._color)
        metrics = painter.fontMetrics()
        baseline = device_pixel_value(
            self,
            (self.height() + metrics.ascent() - metrics.descent()) / 2.0,
        )

        span = self._ticker_repeat_span()
        positions = [self._offset]


        if self._copies_started > 1:
            positions.append(self._offset - span)

        left = float(clip.left())
        right = float(clip.right())
        for x in positions:
            if x > right or x + self._text_width < left:
                continue
            painter.drawText(QtCore.QPointF(x, baseline), self._text)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


from typing import Any

from ..constants import DEFAULT_INDICATOR_SHORTCUTS, INDICATOR_SETTING_DEFAULTS
from ..theme import RIGHT_LAYOUT_PRESETS
from ..models import SettingsHostPort
from .developer_tools import (
    UiTunerDialog, MagneticRailLabDialog, DeveloperDialog,
)
from ..constants import (
    RIGHT_PANEL_NAMES,
    RIGHT_PANEL_LABELS,
    DIRECTIONAL_COLOR_MODE_OPTIONS,
)
from ..utilities import TextRole


class IndicatorSettingsDialog(QtWidgets.QDialog):
    history_requested = Signal(str)

    SPECS = {
        "EMA Trend": (
            ("fast", "Fast EMA", 2, 500, 1, 0, " candles", "Short-term trend and pullbacks."),
            ("medium", "Medium EMA", 2, 500, 1, 0, " candles", "Intermediate trend support."),
            ("slow", "Slow EMA", 2, 500, 1, 0, " candles", "Longer-term trend context."),
        ),
        "VWAP": (),
        "Donchian Channels": (
            ("period", "Lookback", 2, 500, 1, 0, " candles", "Highest high / lowest low of previous candles; excludes the live candle."),
        ),
        "Auto Fibonacci": (
            ("maximum_candidates", "Candidate interpretations", 1, 10, 1, 0, "", "How many high-confidence swing interpretations Alt+F can cycle through."),
        ),
        "Bollinger Bands": (
            ("period", "Period", 2, 200, 1, 0, " candles", "Rolling mean and deviation window."),
            ("deviations", "Deviation width", 0.5, 5.0, 0.1, 2, " x", "Standard deviations above and below the mean."),
        ),
        "ATR": (
            ("period", "Period", 2, 200, 1, 0, " candles", "Wilder ATR calculation window."),
        ),
        "Open Interest": (
            ("smoothing", "Smoothing", 1, 50, 1, 0, " samples", "1 shows raw OI; higher values apply a rolling mean."),
        ),
        "Liquidations": (
            ("minimum_notional", "Minimum value", 10_000, 1_000_000_000, 25_000, 0, " USD", "Show only forced orders at or above this USD value."),
            ("maximum_markers", "Maximum markers", 20, 250, 10, 0, "", "Maximum qualifying arrows retained on the chart."),
        ),
        "Visible Volume Profile": (
            ("bins", "Price rows", 12, 160, 4, 0, "", "Number of price buckets in the visible-range profile."),
            ("levels", "Alert levels", 1, 8, 1, 0, "", "High-volume nodes exposed to alerts."),
            ("width_pct", "Maximum width", 5, 35, 1, 1, "%", "Maximum horizontal width inside the chart."),
        ),
        "Session Volume Profile": (
            ("bins", "Price rows", 12, 160, 4, 0, "", "Number of price buckets in the session profile."),
            ("levels", "Alert levels", 1, 8, 1, 0, "", "High-volume nodes exposed to alerts."),
            ("width_pct", "Maximum width", 5, 35, 1, 1, "%", "Maximum horizontal width inside the chart."),
        ),
        "Major Price Levels": (
            ("minimum_score", "Minimum confidence", 30, 90, 1, 0, "/99", "Higher values show fewer, stronger price-action zones."),
            ("maximum_levels", "Maximum levels", 1, 8, 1, 0, "", "Maximum support and resistance zones shown together."),
        ),
        "Funding Rate History": (
            (
                "smoothing",
                "EMA smoothing",
                1,
                24,
                1,
                0,
                " settlements",
                "1 shows the raw settlement path; higher values smooth the funding-rate line with an EMA.",
            ),
        ),
        "RSI": (
            ("period", "Period", 2, 200, 1, 0, " candles", "Wilder RSI calculation window."),
            ("upper", "Overbought", 50, 99, 1, 1, "", "Upper reference line."),
            ("lower", "Oversold", 1, 50, 1, 1, "", "Lower reference line."),
        ),
    }

    def __init__(
        self,
        values: dict[str, dict[str, Any]],
        parent: QtWidgets.QWidget | None = None,
        initial_indicator: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Indicator settings")
        self.resize(690, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("INDICATOR SETTINGS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Parameters apply immediately after saving and are retained between launches."
        )
        note.setObjectName("subtleLabel")
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        self.names = QtWidgets.QListWidget()
        self.names.setFixedWidth(225)
        self.pages = QtWidgets.QStackedWidget()
        self.editors: dict[str, dict[str, QtWidgets.QDoubleSpinBox]] = {}
        self.option_editors: dict[str, dict[str, QtWidgets.QCheckBox]] = {}
        self.choice_editors: dict[str, dict[str, QtWidgets.QComboBox]] = {}
        self.level_editors: dict[str, dict[float, QtWidgets.QCheckBox]] = {}
        for indicator, fields in self.SPECS.items():
            self.names.addItem(indicator)
            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QFormLayout(page)
            page_layout.setContentsMargins(12, 8, 12, 8)
            page_layout.setHorizontalSpacing(14)
            page_layout.setVerticalSpacing(12)
            editors: dict[str, QtWidgets.QDoubleSpinBox] = {}
            current = values.get(indicator, {})
            for key, label, minimum, maximum, step, decimals, suffix, tooltip in fields:
                editor = QtWidgets.QDoubleSpinBox()
                editor.setRange(float(minimum), float(maximum))
                editor.setSingleStep(float(step))
                editor.setDecimals(int(decimals))
                editor.setSuffix(str(suffix))
                editor.setValue(float(current.get(key, INDICATOR_SETTING_DEFAULTS[indicator][key])))
                editor.setToolTip(tooltip)
                page_layout.addRow(label, editor)
                editors[key] = editor
            options: dict[str, QtWidgets.QCheckBox] = {}
            choices: dict[str, QtWidgets.QComboBox] = {}
            levels: dict[float, QtWidgets.QCheckBox] = {}
            if indicator == "VWAP":
                anchor = QtWidgets.QComboBox()
                for label, value in (("Day · UTC", "day"), ("Week · Monday UTC", "week"), ("Month · UTC", "month")):
                    anchor.addItem(label, value)
                anchor.setCurrentIndex(max(0, anchor.findData(current.get("anchor", "week"))))
                page_layout.addRow("Reset", anchor)
                choices["anchor"] = anchor
            if indicator == "Auto Fibonacci":
                label_position = QtWidgets.QComboBox()
                for label, value in (("Right", "right"), ("Center", "center"), ("Left", "left")):
                    label_position.addItem(label, value)
                label_position.setCurrentIndex(max(0, label_position.findData(current.get("label_position", "right"))))
                label_position.setToolTip("Place every Fibonacci ratio/price label at the left, center, or right of its line.")
                page_layout.addRow("Value position", label_position)
                choices["label_position"] = label_position

                line_extent = QtWidgets.QComboBox()
                for label, value in (("Swing to right edge", "right_edge"), ("Between anchors", "anchors"), ("Full visible chart", "full")):
                    line_extent.addItem(label, value)
                line_extent.setCurrentIndex(max(0, line_extent.findData(current.get("line_extent", "right_edge"))))
                line_extent.setToolTip("Choose how far each automatic Fibonacci level extends horizontally.")
                page_layout.addRow("Line extent", line_extent)
                choices["line_extent"] = line_extent

                for key, text, tooltip in (
                    ("show_ratios", "Show Fibonacci ratios", "Show values such as 0.618 in each level label."),
                    ("show_prices", "Show level prices", "Show the calculated market price in each level label."),
                    ("show_anchor", "Show swing anchor and points", "Show the dotted impulse line and its two endpoints."),
                    ("show_badge", "Show Auto Fib badge", "Show the active interpretation and timeframe above the chart."),
                ):
                    option = QtWidgets.QCheckBox(text)
                    option.setChecked(bool(current.get(key, INDICATOR_SETTING_DEFAULTS[indicator][key])))
                    option.setToolTip(tooltip)
                    page_layout.addRow("Display", option)
                    options[key] = option

                level_box = QtWidgets.QWidget()
                level_grid = QtWidgets.QGridLayout(level_box)
                level_grid.setContentsMargins(0, 0, 0, 0)
                level_grid.setHorizontalSpacing(12)
                level_grid.setVerticalSpacing(5)
                selected_levels = {
                    round(float(value), 6)
                    for value in current.get("levels", INDICATOR_SETTING_DEFAULTS[indicator]["levels"])
                }
                for index, ratio in enumerate(INDICATOR_SETTING_DEFAULTS[indicator]["levels"]):
                    check = QtWidgets.QCheckBox(f"{float(ratio):.3f}")
                    check.setChecked(round(float(ratio), 6) in selected_levels)
                    check.setToolTip("Negative values are extensions above the impulse high in the existing 0-at-high / 1-at-low convention.")
                    level_grid.addWidget(check, index // 3, index % 3)
                    levels[float(ratio)] = check
                page_layout.addRow("Levels", level_box)
            elif indicator == "RSI":
                thresholds = QtWidgets.QCheckBox(
                    "Show overbought / oversold reference lines"
                )
                thresholds.setChecked(bool(current.get("show_thresholds", False)))
                thresholds.setToolTip(
                    "Draw dashed lines at the configured overbought and oversold levels"
                )
                page_layout.addRow("Reference lines", thresholds)
                options["show_thresholds"] = thresholds
            if indicator == "Liquidations":
                history_button = QtWidgets.QPushButton("LOAD RECORDED HISTORY")
                history_button.setToolTip(
                    "Load liquidation snapshots previously captured by Nightwatch for the active pair and visible chart range."
                )
                history_button.clicked.connect(
                    lambda _checked=False: self.history_requested.emit("Liquidations")
                )
                page_layout.addRow("Local data", history_button)
            elif indicator == "Funding Rate History":
                history_note = QtWidgets.QLabel(
                    "The active pair's current chart range downloads automatically when this indicator is enabled."
                )
                history_note.setObjectName("subtleLabel")
                history_note.setWordWrap(True)
                page_layout.addRow("History", history_note)
            explanations = {
                "EMA Trend": "21 / 50 / 200 by default. Rising, ordered averages help describe trend strength and pullbacks. Periods use the selected timeframe; they are not fixed daily averages.",
                "VWAP": "Volume-weighted price since the selected UTC boundary. Uses quote/base traded volume; OHLC typical-price approximation only when quote volume is missing. The first incomplete anchor stays blank until more history is loaded.",
                "Donchian Channels": "Breakout context from completed prior candles. A 20-candle channel reacts faster; 55 gives a broader trend range. The current candle cannot move its own breakout threshold.",
                "RSI": "Momentum, not an automatic sell signal. RSI can remain above 70 during a strong bull trend. Flat prices produce 50.",
                "ATR": "Displayed as ATR / close × 100, allowing comparison across differently priced pairs. Measures volatility, not direction.",
                "Visible Volume Profile": "Estimated from OHLCV candle ranges, not exact trades at each price. Changes with the selected visible range by design.",
                "Session Volume Profile": "Estimated volume distribution for the current UTC day. Not an exact trade-by-price histogram.",
                "Open Interest": "USD notional OI: changes reflect both positioning and price. Rising OI alone does not tell you whether longs or shorts are entering.",
                "Liquidations": "Observed forced-order snapshots only. Binance can omit events within its snapshot interval; this is not total exchange liquidation volume.",
                "Funding Rate History": "Raw rates are per settlement, not annualized. Positive rates mean longs pay shorts. The optional EMA uses actual settlements without clipping spikes.",
                "Major Price Levels": "Ranked support/resistance zones, not probabilities. Confirmed pivots require later candles, so new zones appear with a delay.",
                "Auto Fibonacci": "Ranks bullish swing interpretations using current structure. Candidates can change after new price action; this is a drawing aid, not a fixed historical signal.",
            }
            if indicator in explanations:
                explanation = QtWidgets.QLabel(explanations[indicator])
                explanation.setObjectName("subtleLabel")
                explanation.setWordWrap(True)
                page_layout.addRow(explanation)
            page_layout.addRow(QtWidgets.QLabel(""))
            self.editors[indicator] = editors
            self.option_editors[indicator] = options
            self.choice_editors[indicator] = choices
            self.level_editors[indicator] = levels
            self.pages.addWidget(page)
        self.names.currentRowChanged.connect(self.pages.setCurrentIndex)
        selected_row = (
            tuple(self.SPECS).index(initial_indicator)
            if initial_indicator in self.SPECS
            else 0
        )
        self.names.setCurrentRow(selected_row)
        body.addWidget(self.names)
        body.addWidget(self.pages, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
            | QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).setText(
            "RESET SELECTED"
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._reset_selected
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addLayout(body, 1)
        layout.addWidget(buttons)

    def _reset_selected(self) -> None:
        indicator = self.names.currentItem().text()
        for key, editor in self.editors[indicator].items():
            editor.setValue(float(INDICATOR_SETTING_DEFAULTS[indicator][key]))
        for key, editor in self.option_editors[indicator].items():
            editor.setChecked(bool(INDICATOR_SETTING_DEFAULTS[indicator][key]))
        for key, editor in self.choice_editors[indicator].items():
            index = editor.findData(INDICATOR_SETTING_DEFAULTS[indicator][key])
            editor.setCurrentIndex(max(0, index))
        defaults = {round(float(value), 6) for value in INDICATOR_SETTING_DEFAULTS[indicator].get("levels", [])}
        for ratio, editor in self.level_editors[indicator].items():
            editor.setChecked(round(ratio, 6) in defaults)

    def _accept_validated(self) -> None:
        auto_options = self.option_editors["Auto Fibonacci"]
        if not (auto_options["show_ratios"].isChecked() or auto_options["show_prices"].isChecked()):
            QtWidgets.QMessageBox.warning(
                self,
                "Auto Fibonacci labels",
                "Show at least the Fibonacci ratio or the level price.",
            )
            self.names.setCurrentRow(tuple(self.SPECS).index("Auto Fibonacci"))
            return
        if not any(editor.isChecked() for editor in self.level_editors["Auto Fibonacci"].values()):
            QtWidgets.QMessageBox.warning(
                self,
                "Auto Fibonacci levels",
                "Select at least one Fibonacci level.",
            )
            self.names.setCurrentRow(tuple(self.SPECS).index("Auto Fibonacci"))
            return
        ema = self.editors["EMA Trend"]
        if not ema["fast"].value() < ema["medium"].value() < ema["slow"].value():
            QtWidgets.QMessageBox.warning(self, "EMA periods", "Use fast < medium < slow.")
            self.names.setCurrentRow(tuple(self.SPECS).index("EMA Trend"))
            return
        rsi = self.editors["RSI"]
        if rsi["lower"].value() >= rsi["upper"].value():
            QtWidgets.QMessageBox.warning(
                self,
                "Invalid RSI levels",
                "Oversold must be below overbought.",
            )
            self.names.setCurrentRow(tuple(self.SPECS).index("RSI"))
            return
        self.accept()

    def values(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for indicator, editors in self.editors.items():
            integer_fields = {
                field[0] for field in self.SPECS[indicator] if int(field[5]) == 0
            }
            output[indicator] = {
                key: int(round(editor.value())) if key in integer_fields else editor.value()
                for key, editor in editors.items()
            }
            output[indicator].update(
                {
                    key: editor.isChecked()
                    for key, editor in self.option_editors[indicator].items()
                }
            )
            output[indicator].update(
                {key: editor.currentData() for key, editor in self.choice_editors[indicator].items()}
            )
            if self.level_editors[indicator]:
                output[indicator]["levels"] = [
                    ratio for ratio, editor in self.level_editors[indicator].items()
                    if editor.isChecked()
                ]
        return output


class IndicatorShortcutsDialog(QtWidgets.QDialog):
    def __init__(
        self,
        shortcuts: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Indicator shortcuts")
        self.setMinimumWidth(470)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("INDICATOR SHORTCUTS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Assign Ctrl shortcuts to chart indicators. Number keys 1–9 select timeframes."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        rows = QtWidgets.QGridLayout()
        rows.setContentsMargins(0, 4, 0, 4)
        rows.setHorizontalSpacing(14)
        rows.setVerticalSpacing(6)
        self.editors: dict[str, QtWidgets.QComboBox] = {}
        for row, indicator in enumerate(DEFAULT_INDICATOR_SHORTCUTS):
            label = QtWidgets.QLabel(indicator)
            editor = QtWidgets.QComboBox()
            editor.setFixedWidth(110)
            editor.addItem("—", "")
            for key in ("1", "2", "3", "Q", "W", "E", "A", "S", "D", "4", "5", "6", "7", "8", "9", "0"):
                editor.addItem("Ctrl+" + key, "Ctrl+" + key)
            selected = str(shortcuts.get(indicator, DEFAULT_INDICATOR_SHORTCUTS[indicator]))
            editor.setCurrentIndex(max(0, editor.findData(selected)))
            editor.setToolTip(
                f"Shortcut that toggles {indicator} while the chart workspace is active."
            )
            label.setToolTip(editor.toolTip())
            rows.addWidget(label, row, 0)
            rows.addWidget(editor, row, 1)
            self.editors[indicator] = editor
        rows.setColumnStretch(0, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
            | QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).setText(
            "RESET ALL"
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._reset
        )
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addLayout(rows)
        layout.addWidget(buttons)

    def _reset(self) -> None:
        for indicator, editor in self.editors.items():
            editor.setCurrentIndex(
                max(0, editor.findData(DEFAULT_INDICATOR_SHORTCUTS[indicator]))
            )

    def _accept_validated(self) -> None:
        active = [
            str(editor.currentData())
            for editor in self.editors.values()
            if str(editor.currentData())
        ]
        if len(active) != len(set(active)):
            QtWidgets.QMessageBox.warning(
                self,
                "Duplicate indicator shortcut",
                "Each number key can toggle only one indicator.",
            )
            return
        self.accept()

    def shortcuts(self) -> dict[str, str]:
        return {
            indicator: str(editor.currentData() or "")
            for indicator, editor in self.editors.items()
        }


class RightPanelPresetsDialog(QtWidgets.QDialog):
    """Edit the ordered, expandable list of right-panel layout presets."""

    def __init__(
        self,
        presets: dict[str, dict[str, Any]],
        parent: QtWidgets.QWidget | None = None,
        *, panel_names=None,
    ):
        super().__init__(parent)
        self.panel_names = tuple(panel_names or RIGHT_PANEL_NAMES)
        self.setWindowTitle("Panel presets")
        self.setMinimumWidth(620)
        self.resize(680, 390)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("PANEL PRESETS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Choose the panels to show. Column buttons apply layout templates; "
            "drag a panel grip or use its Move menu to arrange panels freely. "
            "Layouts smaller than their content can be scrolled."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(note)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(content)
        self.grid.setContentsMargins(0, 0, 6, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(7)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid.addWidget(QtWidgets.QLabel("NAME"), 0, 0)
        self.grid.setColumnStretch(0, 1)
        for column, panel_name in enumerate(self.panel_names, start=1):
            label = QtWidgets.QLabel(RIGHT_PANEL_LABELS.get(panel_name, panel_name))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("subtleLabel")
            self.grid.addWidget(label, 0, column)
        self.rows: list[tuple[QtWidgets.QLineEdit, dict[str, QtWidgets.QCheckBox]]] = []
        self.remove_buttons: dict[QtWidgets.QLineEdit, QtWidgets.QPushButton] = {}
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        for name, preset in presets.items():
            self._add_row(name, preset.get("visible", ()))
        add = QtWidgets.QPushButton("ADD PRESET")
        add.setAutoDefault(False)
        add.clicked.connect(self._add_preset)
        layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        restore = buttons.addButton(
            "RESTORE DEFAULTS", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole
        )
        restore.clicked.connect(self._restore_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, name: str, visible: tuple[str, ...]) -> None:
        name_edit = QtWidgets.QLineEdit(name)
        name_edit.setMaxLength(32)
        from .panels import valid_panel_names
        allowed = set(valid_panel_names(visible, self.panel_names))
        checks = {}
        for panel_name in self.panel_names:
            check = QtWidgets.QCheckBox()
            check.setChecked(panel_name in allowed)
            check.setAccessibleName(f"Include {RIGHT_PANEL_LABELS.get(panel_name, panel_name)}")
            checks[panel_name] = check
        remove = QtWidgets.QPushButton("−")
        remove.setAccessibleName("Remove preset")
        remove.setAutoDefault(False)
        remove.clicked.connect(lambda _checked=False, edit=name_edit: self._remove_row(edit))
        self.rows.append((name_edit, checks))
        self.remove_buttons[name_edit] = remove
        self._arrange_rows()

    def _arrange_rows(self) -> None:
        for row, (name_edit, checks) in enumerate(self.rows, start=1):
            widgets = [name_edit, *checks.values(), self.remove_buttons[name_edit]]
            for widget in widgets:
                self.grid.removeWidget(widget)
            name_edit.setPlaceholderText(f"Preset {row}")
            self.grid.addWidget(name_edit, row, 0)
            for column, panel_name in enumerate(self.panel_names, start=1):
                self.grid.addWidget(checks[panel_name], row, column, Qt.AlignmentFlag.AlignCenter)
            remove = self.remove_buttons[name_edit]
            remove.setEnabled(len(self.rows) > 1)
            self.grid.addWidget(remove, row, len(self.panel_names) + 1)

    def _add_preset(self) -> None:
        names = {edit.text().strip().casefold() for edit, _checks in self.rows}
        index = len(self.rows) + 1
        while f"preset {index}" in names:
            index += 1
        self._add_row(f"Preset {index}", ("Market depth", "Trading / positions"))
        edit = self.rows[-1][0]
        edit.setFocus(Qt.FocusReason.OtherFocusReason)
        edit.selectAll()
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(edit))

    def _remove_row(self, name_edit: QtWidgets.QLineEdit) -> None:
        if len(self.rows) <= 1:
            return
        checks = next(checks for edit, checks in self.rows if edit is name_edit)
        self.rows = [(edit, checks) for edit, checks in self.rows if edit is not name_edit]
        for widget in (name_edit, *checks.values(), self.remove_buttons.pop(name_edit)):
            self.grid.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self._arrange_rows()

    def _restore_defaults(self) -> None:
        for name_edit, checks in self.rows:
            for widget in (name_edit, *checks.values(), self.remove_buttons[name_edit]):
                self.grid.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        self.rows.clear()
        self.remove_buttons.clear()
        for name, preset in RIGHT_LAYOUT_PRESETS.items():
            self._add_row(name, preset["visible"])

    def accept(self) -> None:
        names = [name_edit.text().strip() for name_edit, _checks in self.rows]
        if any(not name for name in names):
            QtWidgets.QMessageBox.warning(
                self,
                "Preset name required",
                "Every preset needs a name.",
            )
            return
        if len({name.casefold() for name in names}) != len(names) or any(
            name.casefold() == "custom" for name in names
        ):
            QtWidgets.QMessageBox.warning(
                self,
                "Preset names",
                "Preset names must be unique, and Custom is reserved.",
            )
            return
        super().accept()

    def values(self) -> dict[str, tuple[str, ...]]:
        return {
            name_edit.text().strip(): tuple(
                panel_name
                for panel_name in self.panel_names
                if checks[panel_name].isChecked()
            )
            for name_edit, checks in self.rows
        }


class NightwatchSettingsDialog(QtWidgets.QDialog):
    """Single large settings surface for configuration and developer tooling.

    The dialog reuses the host's existing QAction/state graph instead of
    duplicating application state. Developer diagnostics, UI tuning and magnetic
    rail design are embedded here so Settings remains the one operational window.
    """

    CATEGORIES = (
        "Appearance",
        "Chart & Indicators",
        "Workspace",
        "Trading",
        "Data & Alerts",
        "Advanced",
    )

    def __init__(
        self,
        host: SettingsHostPort,
        parent: QtWidgets.QWidget | None = None,
    ):
        dialog_parent = parent
        if dialog_parent is None and isinstance(host, QtWidgets.QWidget):
            dialog_parent = host
        super().__init__(dialog_parent)
        self.host = host
        self.setObjectName("nightwatchSettingsDialog")
        self.setWindowTitle("Nightwatch settings")
        self.setModal(False)
        self._preferred_size = QtCore.QSize(1240, 820)
        self._preferred_minimum_size = QtCore.QSize(1040, 680)
        self.resize(self._preferred_size)
        self.setMinimumSize(self._preferred_minimum_size)
        self._action_widgets: dict[QtGui.QAction, QtWidgets.QWidget] = {}
        self._action_widget_labels: dict[QtGui.QAction, str] = {}
        self._panel_checks: dict[str, QtWidgets.QCheckBox] = {}
        self._settings_search: QtWidgets.QLineEdit | None = None
        self.learning_mode_check: QtWidgets.QCheckBox | None = None
        self._directional_mode_combos: dict[str, QtWidgets.QComboBox] = {}
        self._mode_buttons: dict[int, QtWidgets.QRadioButton] = {}
        self.volume_height_spin: QtWidgets.QSpinBox | None = None
        self.frame_benchmark_button: QtWidgets.QPushButton | None = None
        self.frame_benchmark_label: QtWidgets.QLabel | None = None
        self.developer_tabs: QtWidgets.QTabWidget | None = None
        self.embedded_ui_tuner: UiTunerDialog | None = None
        self.embedded_diagnostics: DeveloperDialog | None = None
        self.embedded_rail_design: MagneticRailLabDialog | None = None
        self._syncing = False
        self._page_builders = (
            self._build_appearance_page,
            self._build_chart_indicators_page,
            self._build_workspace_page,
            self._build_trading_page,
            self._build_data_alerts_page,
            self._build_advanced_page,
        )
        self._built_pages: set[int] = set()
        self._prewarm_running = False
        self._developer_tab_keys: list[str] = []
        self._developer_placeholders: dict[str, QtWidgets.QWidget] = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("settingsHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(24, 16, 24, 16)
        header_layout.setSpacing(18)

        heading_block = QtWidgets.QWidget()
        heading_block.setObjectName("settingsHeaderText")
        heading_layout = QtWidgets.QVBoxLayout(heading_block)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(3)
        title = QtWidgets.QLabel("SETTINGS")
        title.setObjectName("settingsHeading")
        note = QtWidgets.QLabel(
            "Configure Nightwatch. Changes apply immediately unless a control says otherwise."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        heading_layout.addWidget(title)
        heading_layout.addWidget(note)
        header_layout.addWidget(heading_block, 1)

        self._settings_search = QtWidgets.QLineEdit()
        self._settings_search.setObjectName("settingsSearch")
        self._settings_search.setPlaceholderText("Search settings…")
        self._settings_search.setClearButtonEnabled(True)
        self._settings_search.setAccessibleName("Search settings")
        self._settings_search.setToolTip("Find a setting by name, description, or option. Ctrl+F")
        header_layout.addWidget(self._settings_search, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        body = QtWidgets.QWidget()
        body_layout = QtWidgets.QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.categories = QtWidgets.QListWidget()
        self.categories.setObjectName("settingsCategories")
        self.categories.setMinimumWidth(156)
        self.categories.setMaximumWidth(196)
        self.categories.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.categories.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for name in self.CATEGORIES:
            self.categories.addItem(name)
        body_layout.addWidget(self.categories)

        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("settingsPages")
        self.pages.setMinimumSize(0, 0)
        self.pages.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        body_layout.addWidget(self.pages, 1)
        root.addWidget(body, 1)

        for name in self.CATEGORIES:
            placeholder = QtWidgets.QWidget()
            placeholder.setObjectName("settingsPagePlaceholder")
            placeholder_layout = QtWidgets.QVBoxLayout(placeholder)
            placeholder_layout.setContentsMargins(24, 24, 24, 24)
            placeholder_layout.addStretch(1)
            loading = QtWidgets.QLabel(f"{name.upper()} · PREPARING")
            loading.setObjectName("subtleLabel")
            loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder_layout.addWidget(loading)
            placeholder_layout.addStretch(1)
            self.pages.addWidget(placeholder)

        footer = QtWidgets.QFrame()
        footer.setObjectName("settingsFooter")
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        footer_layout.setSpacing(8)
        footer_hint = QtWidgets.QLabel("Ctrl+F · Search   Esc · Close")
        footer_hint.setObjectName("subtleLabel")
        footer_layout.addWidget(footer_hint)
        footer_layout.addStretch(1)
        close = QtWidgets.QPushButton("CLOSE")
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        footer_layout.addWidget(close)
        root.addWidget(footer)

        self.categories.currentRowChanged.connect(self._on_category_changed)
        self._settings_search.textChanged.connect(self._apply_settings_search)
        find_shortcut = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Find, self)
        find_shortcut.activated.connect(self._settings_search.setFocus)
        escape_shortcut = QtGui.QShortcut(QtGui.QKeySequence(Qt.Key.Key_Escape), self)
        escape_shortcut.activated.connect(self._settings_escape)
        self.categories.setCurrentRow(0)
        self._fit_to_available_screen(prefer_default=True)

    def _ensure_page_built(self, index: int) -> QtWidgets.QWidget | None:
        index = int(index)
        if index < 0 or index >= len(self._page_builders):
            return None
        if index in self._built_pages:
            return self.pages.widget(index)
        old = self.pages.widget(index)
        current = self.pages.currentIndex()
        page = self._page_builders[index]()
        self.pages.removeWidget(old)
        old.deleteLater()
        self.pages.insertWidget(index, page)
        self._built_pages.add(index)
        if current == index:
            self.pages.setCurrentIndex(index)
        return page

    def _on_category_changed(self, index: int) -> None:
        index = int(index)
        self._ensure_page_built(index)
        self.pages.setCurrentIndex(index)
        self.sync_from_owner()
        if index == self.CATEGORIES.index("Advanced") and self.isVisible():
            QtCore.QTimer.singleShot(0, self._ensure_current_developer_tool)

    def prewarm_remaining_pages(self) -> None:
        if self._prewarm_running:
            return
        self._prewarm_running = True
        QtCore.QTimer.singleShot(0, self._prewarm_next_page)

    def _prewarm_next_page(self) -> None:
        if not self._prewarm_running:
            return
        for index in range(len(self._page_builders)):
            if index not in self._built_pages:
                self._ensure_page_built(index)
                QtCore.QTimer.singleShot(16, self._prewarm_next_page)
                return
        self._prewarm_running = False

    def _available_screen_geometry(self) -> QtCore.QRect:
        parent = self.parentWidget()
        screen = None
        if isinstance(parent, QtWidgets.QWidget):
            try:
                screen = parent.screen()
            except RuntimeError:
                screen = None
        if screen is None:
            try:
                screen = self.screen()
            except RuntimeError:
                screen = None
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QtCore.QRect()

    def _fit_to_available_screen(self, *, prefer_default: bool = False) -> None:
        available = self._available_screen_geometry()
        if not available.isValid() or available.isEmpty():
            return
        margin = 24
        max_width = max(1, int(available.width()) - margin * 2)
        max_height = max(1, int(available.height()) - margin * 2)


        self.setMinimumSize(
            min(int(self._preferred_minimum_size.width()), max_width),
            min(int(self._preferred_minimum_size.height()), max_height),
        )

        current = self.size()
        target_width = min(
            int(self._preferred_size.width()) if prefer_default else int(current.width()),
            max_width,
        )
        target_height = min(
            int(self._preferred_size.height()) if prefer_default else int(current.height()),
            max_height,
        )
        target_width = max(int(self.minimumWidth()), target_width)
        target_height = max(int(self.minimumHeight()), target_height)
        if self.size() != QtCore.QSize(target_width, target_height):
            self.resize(target_width, target_height)


        frame = self.frameGeometry()
        x = min(max(frame.x(), available.left()), available.right() - target_width + 1)
        y = min(max(frame.y(), available.top()), available.bottom() - target_height + 1)
        if prefer_default or not available.contains(frame):
            if prefer_default:
                x = available.left() + max(0, (available.width() - target_width) // 2)
                y = available.top() + max(0, (available.height() - target_height) // 2)
            self.move(x, y)

    def _scroll_page(self, title: str, note: str = "") -> tuple[QtWidgets.QScrollArea, QtWidgets.QVBoxLayout]:
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        content.setObjectName("settingsPage")
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)
        heading = QtWidgets.QLabel(title.upper())
        heading.setObjectName("settingsPageHeading")
        layout.addWidget(heading)
        if note:
            detail = QtWidgets.QLabel(note)
            detail.setObjectName("subtleLabel")
            detail.setWordWrap(True)
            layout.addWidget(detail)
        scroll.setWidget(content)
        return scroll, layout

    @staticmethod
    def _group(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QVBoxLayout]:
        box = QtWidgets.QGroupBox(title)
        box.setObjectName("settingsGroup")
        box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        return box, layout

    @staticmethod
    def _settings_grid(layout: QtWidgets.QVBoxLayout) -> QtWidgets.QGridLayout:
        host = QtWidgets.QWidget()
        host.setObjectName("settingsGridHost")
        grid = QtWidgets.QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(host)
        layout.addStretch(1)
        return grid


    @staticmethod
    def _searchable_text(widget: QtWidgets.QWidget) -> str:
        parts: list[str] = [widget.objectName(), widget.toolTip()]
        if isinstance(widget, QtWidgets.QGroupBox):
            parts.append(widget.title())
        if isinstance(widget, QtWidgets.QLabel):
            parts.append(widget.text())
        if isinstance(widget, QtWidgets.QAbstractButton):
            parts.append(widget.text())
        if isinstance(widget, QtWidgets.QComboBox):
            parts.extend(widget.itemText(index) for index in range(widget.count()))
        for child in widget.findChildren(QtWidgets.QWidget):
            parts.extend((child.objectName(), child.toolTip()))
            if isinstance(child, QtWidgets.QGroupBox):
                parts.append(child.title())
            if isinstance(child, QtWidgets.QLabel):
                parts.append(child.text())
            if isinstance(child, QtWidgets.QAbstractButton):
                parts.append(child.text())
            if isinstance(child, QtWidgets.QComboBox):
                parts.extend(child.itemText(index) for index in range(child.count()))
        return " ".join(str(part) for part in parts if part).casefold()

    def _apply_settings_search(self, text: str) -> None:
        query = str(text or "").strip().casefold()
        if query:
            for index in range(len(self._page_builders)):
                self._ensure_page_built(index)
            self.sync_from_owner()
        first_match = -1
        current = self.categories.currentRow()
        current_visible = False
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            category = self.CATEGORIES[index].casefold() if index < len(self.CATEGORIES) else ""
            category_match = bool(query and query in category)
            page_match = not query or category_match or query in self._searchable_text(page)
            item = self.categories.item(index)
            if item is not None:
                item.setHidden(not page_match)
            if page_match and first_match < 0:
                first_match = index
            if page_match and index == current:
                current_visible = True


            for group in page.findChildren(QtWidgets.QGroupBox):
                if group.objectName() != "settingsGroup":
                    continue
                group.setVisible(not query or category_match or query in self._searchable_text(group))

        if query and not current_visible and first_match >= 0:
            self.categories.setCurrentRow(first_match)
        elif not query and current < 0 and self.categories.count():
            self.categories.setCurrentRow(0)

    def _settings_escape(self) -> None:
        if self._settings_search is not None and self._settings_search.text():
            self._settings_search.clear()
            self._settings_search.setFocus()
            return
        self.close()

    def _bind_action(
        self,
        layout: QtWidgets.QVBoxLayout,
        action: QtGui.QAction,
        *,
        text: str | None = None,
    ) -> QtWidgets.QWidget | None:
        if action.isSeparator():
            line = QtWidgets.QFrame()
            line.setObjectName("settingsSeparator")
            line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            layout.addWidget(line)
            return line
        label = str(text or action.text()).replace("&", "")
        if action.isCheckable():
            widget = QtWidgets.QCheckBox(label)
            widget.setChecked(action.isChecked())
            widget.setEnabled(action.isEnabled())
            widget.setToolTip(action.toolTip())
            widget.toggled.connect(
                lambda checked, a=action: (
                    a.trigger() if a.isChecked() != checked else None
                )
            )
        else:
            widget = QtWidgets.QPushButton(label)
            widget.setAutoDefault(False)
            widget.setEnabled(action.isEnabled())
            widget.setToolTip(action.toolTip())
            widget.clicked.connect(lambda _checked=False, a=action: a.trigger())
        self._action_widgets[action] = widget
        self._action_widget_labels[action] = label
        layout.addWidget(widget)
        action.changed.connect(lambda a=action: self._sync_action_widget(a))
        return widget

    def _sync_action_widget(self, action: QtGui.QAction) -> None:
        widget = self._action_widgets.get(action)
        if widget is None:
            return
        widget.setEnabled(action.isEnabled())
        widget.setVisible(action.isVisible())
        widget.setToolTip(action.toolTip())
        text = self._action_widget_labels.get(
            action, str(action.text()).replace("&", "")
        )
        if isinstance(widget, QtWidgets.QCheckBox):
            blocker = QtCore.QSignalBlocker(widget)
            widget.setText(text)
            widget.setChecked(action.isChecked())
            del blocker
        elif isinstance(widget, QtWidgets.QPushButton):
            widget.setText(text)

    def _radio_group(
        self,
        layout: QtWidgets.QVBoxLayout,
        actions: dict[Any, QtGui.QAction],
    ) -> None:
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for _key, action in actions.items():
            radio = QtWidgets.QRadioButton(str(action.text()).replace("&", ""))
            radio.setChecked(action.isChecked())
            radio.setEnabled(action.isEnabled())
            radio.setToolTip(action.toolTip())
            radio.toggled.connect(
                lambda checked, a=action: (
                    a.trigger() if checked and not a.isChecked() else None
                )
            )
            action.changed.connect(
                lambda a=action, r=radio: self._sync_radio(a, r)
            )
            group.addButton(radio)
            layout.addWidget(radio)

    @staticmethod
    def _sync_radio(action: QtGui.QAction, radio: QtWidgets.QRadioButton) -> None:
        blocker = QtCore.QSignalBlocker(radio)
        radio.setText(str(action.text()).replace("&", ""))
        radio.setChecked(action.isChecked())
        radio.setEnabled(action.isEnabled())
        radio.setVisible(action.isVisible())
        radio.setToolTip(action.toolTip())
        del blocker

    def _build_appearance_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page(
            "Appearance",
            "Theme, directional trading colors, and candle rendering. The common choices are kept on one screen.",
        )
        grid = self._settings_grid(layout)

        theme_box, theme_layout = self._group("Theme")
        theme_note = QtWidgets.QLabel(
            "Choose the base visual identity. Component colors continue to follow the selected theme unless you explicitly choose classic trading colors below."
        )
        theme_note.setObjectName("subtleLabel")
        theme_note.setWordWrap(True)
        theme_layout.addWidget(theme_note)
        self._radio_group(theme_layout, self.host.theme_actions)
        grid.addWidget(theme_box, 0, 0)

        directional_box, directional_layout = self._group("Buy / sell colors")
        directional_note = QtWidgets.QLabel(
            "Use each theme's own BUY/SELL colors, or switch only the selected trading surface to familiar high-contrast green and red."
        )
        directional_note.setObjectName("subtleLabel")
        directional_note.setWordWrap(True)
        directional_layout.addWidget(directional_note)
        directional_form_host = QtWidgets.QWidget()
        directional_form = QtWidgets.QFormLayout(directional_form_host)
        directional_form.setContentsMargins(0, 0, 0, 0)
        directional_form.setHorizontalSpacing(14)
        directional_form.setVerticalSpacing(9)
        for surface, label in (("candles", "Candles"), ("orderbook", "Order book")):
            combo = QtWidgets.QComboBox()
            for mode, mode_label in DIRECTIONAL_COLOR_MODE_OPTIONS:
                combo.addItem(mode_label, mode)
            combo.setToolTip(
                "Theme colors preserve the selected theme. Classic green / red changes this surface only."
            )
            combo.activated.connect(
                lambda index, target=surface, field=combo: self._directional_mode_selected(
                    target, field.itemData(index)
                )
            )
            directional_form.addRow(label, combo)
            self._directional_mode_combos[surface] = combo
        directional_layout.addWidget(directional_form_host)
        grid.addWidget(directional_box, 0, 1)

        candle_box, candle_layout = self._group("Candle appearance")
        candle_note = QtWidgets.QLabel(
            "Change candle geometry and rendering style without changing your selected BUY/SELL color mode."
        )
        candle_note.setObjectName("subtleLabel")
        candle_note.setWordWrap(True)
        candle_layout.addWidget(candle_note)
        self._radio_group(candle_layout, self.host.candle_style_actions)
        grid.addWidget(candle_box, 1, 0, 1, 2)
        return page

    def _directional_mode_selected(self, surface: str, mode: object) -> None:
        if self._syncing:
            return
        self.host.set_directional_color_mode(surface, str(mode or "theme"))

    def _volume_height_changed(self, value: int) -> None:
        if self._syncing:
            return
        self.host.set_volume_bar_height_percent(int(value))

    def _sync_directional_modes(self) -> None:
        for surface, combo in self._directional_mode_combos.items():
            mode = self.host.directional_color_mode(surface)
            index = combo.findData(mode)
            blocker = QtCore.QSignalBlocker(combo)
            combo.setCurrentIndex(max(0, index))
            del blocker

    def _build_chart_indicators_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page(
            "Chart & Indicators",
            "Chart behavior, visible layers, drawing tools, and studies are grouped here so the complete chart setup is one click away.",
        )
        grid = self._settings_grid(layout)

        benchmark_box, benchmark_layout = self._group("30-second chart benchmark")
        benchmark_note = QtWidgets.QLabel(
            "Click Start, then continuously pan/zoom the chart for 30 seconds. "
            "The capture uses Nightwatch's existing presentation clock and reports FPS and frame-time percentiles."
        )
        benchmark_note.setObjectName("subtleLabel")
        benchmark_note.setWordWrap(True)
        benchmark_layout.addWidget(benchmark_note)
        benchmark_row = QtWidgets.QHBoxLayout()
        benchmark_row.setContentsMargins(0, 0, 0, 0)
        benchmark_row.setSpacing(12)
        self.frame_benchmark_button = QtWidgets.QPushButton("START 30S BENCHMARK")
        self.frame_benchmark_button.setAutoDefault(False)
        self.frame_benchmark_button.setToolTip(
            "Record 30 seconds of chart presentation timing; click the chart and pan/zoom while it runs."
        )
        self.frame_benchmark_button.clicked.connect(self._start_frame_benchmark)
        benchmark_row.addWidget(self.frame_benchmark_button, 0, Qt.AlignmentFlag.AlignTop)
        self.frame_benchmark_label = QtWidgets.QLabel("Not captured yet.")
        self.frame_benchmark_label.setWordWrap(True)
        self.frame_benchmark_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        benchmark_row.addWidget(self.frame_benchmark_label, 1)
        benchmark_layout.addLayout(benchmark_row)
        grid.addWidget(benchmark_box, 0, 0, 1, 2)

        workspace_box, workspace_layout = self._group("Chart workspace")
        self._radio_group(workspace_layout, self.host.chart_layout_actions)
        grid.addWidget(workspace_box, 1, 0)

        scale_box, scale_layout = self._group("Price scale")
        self._bind_action(scale_layout, self.host.auto_scale_action, text="Auto-scale price")
        self._bind_action(scale_layout, self.host.logarithmic_action, text="Logarithmic scale")
        self._bind_action(scale_layout, self.host.fit_chart_action, text="Fit chart to visible data")
        grid.addWidget(scale_box, 1, 1)

        layers_box, layers_layout = self._group("Visible chart layers")
        self._radio_group(layers_layout, self.host.chart_visibility_actions)
        grid.addWidget(layers_box, 2, 0)

        drawing_box, drawing_layout = self._group("Drawing tools")
        for action, label in (
            (self.host.ruler_action, "Measure / ruler"),
            (self.host.fibonacci_action, "Fibonacci"),
            (self.host.horizontal_action, "Horizontal level"),
        ):
            self._bind_action(drawing_layout, action, text=label)
        self._bind_action(drawing_layout, self.host.clear_drawings_action, text="Clear chart drawings")
        grid.addWidget(drawing_box, 2, 1)

        volume_box, volume_layout = self._group("Volume overlay")
        volume_note = QtWidgets.QLabel(
            "Volume is drawn directly inside the price pane with no background or separate scale. "
            "Bars stay anchored to the bottom and normalize to the visible chart range."
        )
        volume_note.setObjectName("subtleLabel")
        volume_note.setWordWrap(True)
        volume_layout.addWidget(volume_note)
        volume_row = QtWidgets.QHBoxLayout()
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.setSpacing(10)
        volume_row.addWidget(QtWidgets.QLabel("Maximum bar height"))
        self.volume_height_spin = QtWidgets.QSpinBox()
        self.volume_height_spin.setRange(5, 45)
        self.volume_height_spin.setSingleStep(1)
        self.volume_height_spin.setSuffix(" %")
        self.volume_height_spin.setToolTip(
            "Maximum volume-bar height as a percentage of the main price viewport."
        )
        self.volume_height_spin.valueChanged.connect(self._volume_height_changed)
        volume_row.addWidget(self.volume_height_spin)
        volume_row.addStretch(1)
        volume_layout.addLayout(volume_row)
        grid.addWidget(volume_box, 3, 0, 1, 2)

        indicators_box, indicators_layout = self._group("Indicators")
        indicators_note = QtWidgets.QLabel(
            "Enable studies directly. Detailed parameters and shortcuts remain available below."
        )
        indicators_note.setObjectName("subtleLabel")
        indicators_note.setWordWrap(True)
        indicators_layout.addWidget(indicators_note)
        indicator_grid_host = QtWidgets.QWidget()
        indicator_grid = QtWidgets.QGridLayout(indicator_grid_host)
        indicator_grid.setContentsMargins(0, 0, 0, 0)
        indicator_grid.setHorizontalSpacing(18)
        indicator_grid.setVerticalSpacing(4)
        actions = list(self.host.indicator_actions.values())
        split = (len(actions) + 1) // 2
        for index, action in enumerate(actions):
            column = 0 if index < split else 1
            row = index if index < split else index - split
            holder = QtWidgets.QVBoxLayout()
            holder.setContentsMargins(0, 0, 0, 0)
            widget = self._bind_action(holder, action)
            if widget is not None:
                indicator_grid.addWidget(widget, row, column)
        config_row = QtWidgets.QHBoxLayout()
        config_row.setContentsMargins(0, 6, 0, 0)
        for action, label in (
            (self.host.indicator_settings_action, "Indicator settings…"),
            (self.host.indicator_shortcuts_action, "Shortcuts…"),
            (self.host.auto_fibonacci_action, "Auto Fibonacci"),
        ):
            button = QtWidgets.QPushButton(label)
            button.setAutoDefault(False)
            button.setEnabled(action.isEnabled())
            button.setToolTip(action.toolTip())
            button.clicked.connect(lambda _checked=False, a=action: a.trigger())
            self._action_widgets[action] = button
            self._action_widget_labels[action] = label
            action.changed.connect(lambda a=action: self._sync_action_widget(a))
            config_row.addWidget(button)
        config_row.addStretch(1)
        indicators_layout.addWidget(indicator_grid_host)
        indicators_layout.addLayout(config_row)
        grid.addWidget(indicators_box, 4, 0, 1, 2)
        self._refresh_frame_benchmark()
        return page

    def _start_frame_benchmark(self) -> None:
        clock = getattr(self.host, "presentation_clock", None)
        starter = getattr(clock, "start_frame_profile", None)
        if not callable(starter):
            if self.frame_benchmark_label is not None:
                self.frame_benchmark_label.setText("Benchmark unavailable: presentation profiling is not installed.")
            if self.frame_benchmark_button is not None:
                self.frame_benchmark_button.setEnabled(False)
            return
        starter(30.0)
        self._refresh_frame_benchmark()


        QtCore.QTimer.singleShot(30_100, self._refresh_frame_benchmark)

    def _refresh_frame_benchmark(self) -> None:
        button = self.frame_benchmark_button
        label = self.frame_benchmark_label
        if button is None or label is None:
            return
        clock = getattr(self.host, "presentation_clock", None)
        getter = getattr(clock, "frame_profile", None)
        if not callable(getter):
            button.setEnabled(False)
            label.setText("Benchmark unavailable: presentation profiling is not installed.")
            return
        profile = getter()
        active = bool(profile.get("active"))
        completed = bool(profile.get("completed"))
        button.setEnabled(not active)
        button.setText("BENCHMARK RUNNING…" if active else "START 30S BENCHMARK")
        if active:
            label.setText(
                f"Capturing… {float(profile.get('remaining_s', 0.0)):.0f}s remaining. "
                "Pan/zoom the chart continuously, then reopen the gear to view results."
            )
            return
        if not completed:
            label.setText("Not captured yet.")
            return
        label.setText(
            f"AVG {float(profile.get('avg_fps', 0.0)):.1f} FPS · "
            f"1% LOW {float(profile.get('one_percent_low_fps', 0.0)):.1f} FPS · "
            f"FRAME AVG {float(profile.get('frame_avg_ms', 0.0)):.2f} ms · "
            f"P50 {float(profile.get('frame_p50_ms', 0.0)):.2f} · "
            f"P95 {float(profile.get('frame_p95_ms', 0.0)):.2f} · "
            f"P99 {float(profile.get('frame_p99_ms', 0.0)):.2f} · "
            f"MAX {float(profile.get('frame_max_ms', 0.0)):.2f} ms · "
            f"{int(profile.get('frame_count', 0))} frames · "
            f"{float(profile.get('target_fps', 0.0)):.0f} Hz display"
        )

    def _refresh_panel_choices(self) -> None:
        grid = getattr(self, "_panel_grid", None)
        if grid is None:
            return
        names = tuple(self.host.panel_sections)
        for name in set(self._panel_checks) - set(names):
            check = self._panel_checks.pop(name)
            grid.removeWidget(check)
            check.deleteLater()
        for index, name in enumerate(names):
            check = self._panel_checks.get(name)
            if check is None:
                check = QtWidgets.QCheckBox(RIGHT_PANEL_LABELS.get(name, name).title())
                check.toggled.connect(
                    lambda checked, panel=name: self.host.right_rail_controller.set_panel_enabled(panel, checked)
                )
                self._panel_checks[name] = check
            if grid.indexOf(check) != index:
                grid.removeWidget(check)
                grid.addWidget(check, index // 2, index % 2)

    def _build_workspace_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page(
            "Workspace",
            "Arrange workspace panels and panel geometry. Order-book display controls live on the Market Depth panel itself.",
        )
        grid = self._settings_grid(layout)

        panels_box, panels_layout = self._group("Visible panels")
        panel_grid = QtWidgets.QGridLayout()
        panel_grid.setContentsMargins(0, 0, 0, 0)
        panel_grid.setHorizontalSpacing(14)
        panel_grid.setVerticalSpacing(4)
        self._panel_grid = panel_grid
        self._refresh_panel_choices()
        panels_layout.addLayout(panel_grid)
        grid.addWidget(panels_box, 0, 0)

        layout_box, layout_controls = self._group("Panel layout")
        mode_group = QtWidgets.QButtonGroup(self)
        mode_group.setExclusive(True)
        for mode, label in ((1, "Single column"), (2, "Two-column template")):
            radio = QtWidgets.QRadioButton(label)
            radio.toggled.connect(
                lambda checked, value=mode: (
                    self.host._set_right_panel_columns(value) if checked and not self._syncing else None
                )
            )
            mode_group.addButton(radio)
            self._mode_buttons[mode] = radio
            layout_controls.addWidget(radio)
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("Preset"))
        self.panel_preset = QtWidgets.QComboBox()
        self.panel_preset.activated.connect(self._panel_preset_selected)
        preset_row.addWidget(self.panel_preset, 1)
        layout_controls.addLayout(preset_row)
        button_row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QPushButton("Edit presets…")
        edit.setAutoDefault(False)
        edit.clicked.connect(self._edit_panel_presets)
        reset = QtWidgets.QPushButton("Reset layout")
        reset.setAutoDefault(False)
        reset.clicked.connect(self._reset_panels)
        button_row.addWidget(edit)
        button_row.addWidget(reset)
        button_row.addStretch(1)
        layout_controls.addLayout(button_row)
        grid.addWidget(layout_box, 0, 1)

        return page

    def _build_trading_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page(
            "Trading",
            "Execution behavior and safeguards. Manual ticket orders submit explicitly; quick-entry hotkeys require ARM; cancel/close remain risk-reduction controls; magnetic-rail BUY/SELL is an explicit chart-side submit routed through the normal shell and gateway safety path."
        )
        grid = self._settings_grid(layout)

        model_box, model_layout = self._group("Execution model")
        model = QtWidgets.QLabel(
            "Manual ticket — explicit submit\n"
            "Quick entry — ARM required\n"
            "Cancel / close — risk reduction\n"
            "Magnetic rail — hover controls + explicit BUY/SELL submit"
        )
        model.setObjectName("subtleLabel")
        model.setWordWrap(True)
        model_layout.addWidget(model)
        grid.addWidget(model_box, 0, 0)

        tools_box, tools_layout = self._group("Execution controls")
        for action in self.host.trading_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                sublabel = QtWidgets.QLabel(str(action.text()).replace("&", ""))
                sublabel.setObjectName("settingsSubheading")
                tools_layout.addWidget(sublabel)
                for child in submenu.actions():
                    self._bind_action(tools_layout, child)
            else:
                self._bind_action(tools_layout, action)
        grid.addWidget(tools_box, 0, 1)
        return page

    def _build_data_alerts_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page(
            "Data & Alerts",
            "Market-data behavior and trader notifications are kept together because they both control what Nightwatch monitors and surfaces.",
        )
        grid = self._settings_grid(layout)

        alerts_box, alerts_layout = self._group("Alerts")
        for action in self.host.alert_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                sublabel = QtWidgets.QLabel(str(action.text()).replace("&", ""))
                sublabel.setObjectName("settingsSubheading")
                alerts_layout.addWidget(sublabel)
                for child in submenu.actions():
                    self._bind_action(alerts_layout, child)
            else:
                self._bind_action(alerts_layout, action)
        grid.addWidget(alerts_box, 0, 0)

        data_box, data_layout = self._group("Market data")
        for action in self.host.data_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                sublabel = QtWidgets.QLabel(str(action.text()).replace("&", ""))
                sublabel.setObjectName("settingsSubheading")
                data_layout.addWidget(sublabel)
                for child in submenu.actions():
                    self._bind_action(data_layout, child)
            else:
                self._bind_action(data_layout, action)
        grid.addWidget(data_box, 0, 1)
        return page

    def _build_advanced_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )

        page = QtWidgets.QWidget()
        page.setObjectName("settingsPage")
        page.setMinimumSize(0, 0)
        page.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        heading = QtWidgets.QLabel("ADVANCED")
        heading.setObjectName("settingsPageHeading")
        root.addWidget(heading)
        note = QtWidgets.QLabel(
            "Guidance and advanced tools. Normal trading configuration should rarely require this page."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        root.addWidget(note)

        top_host = QtWidgets.QWidget()
        top_host.setObjectName("settingsGridHost")
        top_grid = QtWidgets.QGridLayout(top_host)
        top_grid.setContentsMargins(0, 0, 0, 0)
        top_grid.setHorizontalSpacing(14)
        top_grid.setVerticalSpacing(14)
        top_grid.setColumnStretch(0, 1)
        top_grid.setColumnStretch(1, 1)

        guidance_box, guidance_layout = self._group("Guidance")
        self.learning_mode_check = QtWidgets.QCheckBox("Show instructional tooltips")
        self.learning_mode_check.setChecked(bool(self.host.learning_mode))
        self.learning_mode_check.toggled.connect(self.host.set_learning_mode)
        guidance_layout.addWidget(self.learning_mode_check)
        guide = QtWidgets.QPushButton("Open order book guide…")
        guide.setAutoDefault(False)
        guide.clicked.connect(self._open_orderbook_guide)
        guidance_layout.addWidget(guide)
        top_grid.addWidget(guidance_box, 0, 0)

        help_box, help_layout = self._group("Application help")
        for action in self.host.help_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                for child in submenu.actions():
                    self._bind_action(help_layout, child)
            else:
                self._bind_action(help_layout, action)
        top_grid.addWidget(help_box, 0, 1)
        root.addWidget(top_host)

        developer_label = QtWidgets.QLabel("DEVELOPER TOOLS")
        developer_label.setObjectName("settingsSubheading")
        root.addWidget(developer_label)
        self.developer_tabs = QtWidgets.QTabWidget()
        self.developer_tabs.setObjectName("settingsDeveloperTabs")
        self.developer_tabs.setDocumentMode(True)
        self._developer_tab_keys = ["ui_tuner"]
        app = QtWidgets.QApplication.instance()
        diagnostics_enabled = bool(
            app is not None and app.property("nightwatchDiagnosticsEnabled")
        )
        if diagnostics_enabled:
            self._developer_tab_keys.append("diagnostics")
        self._developer_tab_keys.append("magnetic_rail")
        labels = {
            "ui_tuner": "UI TUNER",
            "diagnostics": "DIAGNOSTICS",
            "magnetic_rail": "MAGNETIC RAIL",
        }
        self._developer_placeholders.clear()
        for key in self._developer_tab_keys:
            placeholder = QtWidgets.QWidget()
            placeholder.setObjectName("settingsDeveloperPlaceholder")
            layout = QtWidgets.QVBoxLayout(placeholder)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.addStretch(1)
            note = QtWidgets.QLabel(f"{labels[key]} · loads when selected")
            note.setObjectName("subtleLabel")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(note)
            layout.addStretch(1)
            self._developer_placeholders[key] = placeholder
            self.developer_tabs.addTab(placeholder, labels[key])
        self.developer_tabs.setMinimumHeight(360)
        self.developer_tabs.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.developer_tabs.currentChanged.connect(self._on_developer_tab_changed)
        root.addWidget(self.developer_tabs, 1)

        self.host.ui_tuner_dialog = None
        self.host.developer_dialog = None
        self.host.magnetic_rail_lab_dialog = None
        scroll.setWidget(page)
        return scroll

    def _developer_key_for_index(self, index: int) -> str | None:
        if index < 0 or index >= len(self._developer_tab_keys):
            return None
        return self._developer_tab_keys[index]

    def _ensure_developer_tool(self, key: str) -> QtWidgets.QWidget | None:
        if self.developer_tabs is None or key not in self._developer_tab_keys:
            return None
        if key == "ui_tuner" and self.embedded_ui_tuner is not None:
            return self.embedded_ui_tuner
        if key == "diagnostics" and self.embedded_diagnostics is not None:
            return self.embedded_diagnostics
        if key == "magnetic_rail" and self.embedded_rail_design is not None:
            return self.embedded_rail_design

        index = self._developer_tab_keys.index(key)
        label = self.developer_tabs.tabText(index)
        current = self.developer_tabs.currentIndex()
        old = self.developer_tabs.widget(index)
        if key == "ui_tuner":
            widget = UiTunerDialog(self.host)
            self.embedded_ui_tuner = widget
            self.host.ui_tuner_dialog = widget
        elif key == "diagnostics":
            widget = DeveloperDialog(self.host)
            self.embedded_diagnostics = widget
            self.host.developer_dialog = widget
        else:
            widget = MagneticRailLabDialog(self.host)
            self.embedded_rail_design = widget
            self.host.magnetic_rail_lab_dialog = widget
        self.developer_tabs.removeTab(index)
        old.deleteLater()
        self.developer_tabs.insertTab(index, widget, label)
        self._developer_placeholders.pop(key, None)
        if current == index:
            self.developer_tabs.setCurrentIndex(index)
        return widget

    def _ensure_current_developer_tool(self) -> None:
        if self.developer_tabs is None:
            return
        key = self._developer_key_for_index(self.developer_tabs.currentIndex())
        if key is not None:
            self._ensure_developer_tool(key)

    def _on_developer_tab_changed(self, index: int) -> None:
        if not self.isVisible():
            return
        key = self._developer_key_for_index(int(index))
        if key is not None:
            QtCore.QTimer.singleShot(0, lambda k=key: self._ensure_developer_tool(k))


    def _open_orderbook_guide(self) -> None:
        guide_path = Path(__file__).resolve().parent.parent / "orderbook_guide.html"
        if not guide_path.is_file():
            QtWidgets.QMessageBox.warning(
                self,
                "Order book guide not found",
                "Place orderbook_guide.html in the Nightwatch package folder.",
            )
            return
        opened = QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(guide_path))
        )
        if not opened:
            QtWidgets.QMessageBox.warning(
                self,
                "Unable to open order book guide",
                f"Nightwatch could not open:\n{guide_path}",
            )

    def _panel_preset_selected(self, index: int) -> None:
        if self._syncing or index < 0:
            return
        name = str(self.panel_preset.itemData(index) or self.panel_preset.itemText(index))
        if name in self.host.right_layout_presets:
            self.host._apply_right_layout_preset(name)

    def _edit_panel_presets(self) -> None:
        self.host.edit_right_panel_presets()
        self.sync_from_owner()

    def _reset_panels(self) -> None:
        self.host._reset_right_panel_layout()
        self.sync_from_owner()

    def sync_from_owner(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for action in tuple(self._action_widgets):
                self._sync_action_widget(action)
            self._sync_directional_modes()

            if self.volume_height_spin is not None:
                blocker = QtCore.QSignalBlocker(self.volume_height_spin)
                self.volume_height_spin.setValue(
                    int(self.host.volume_bar_height_setting())
                )
                del blocker

            if self.learning_mode_check is not None:
                blocker = QtCore.QSignalBlocker(self.learning_mode_check)
                self.learning_mode_check.setChecked(bool(self.host.learning_mode))
                del blocker

            controller = self.host.right_rail_controller
            self._refresh_panel_choices()
            for name, check in self._panel_checks.items():
                blocker = QtCore.QSignalBlocker(check)
                check.setChecked(controller.panel_enabled(name))
                del blocker
            for mode, radio in self._mode_buttons.items():
                blocker = QtCore.QSignalBlocker(radio)
                radio.setChecked(controller.column_mode == mode)
                del blocker

            panel_preset = getattr(self, "panel_preset", None)
            if panel_preset is not None:
                names = list(self.host.right_layout_presets)
                current_names = [
                    str(panel_preset.itemData(index) or panel_preset.itemText(index))
                    for index in range(panel_preset.count())
                ]
                if current_names != names:
                    blocker = QtCore.QSignalBlocker(panel_preset)
                    panel_preset.clear()
                    for name in names:
                        panel_preset.addItem(name, name)
                    del blocker
                active = controller.state.active_preset
                index = panel_preset.findData(active)
                if index < 0:
                    panel_preset.setCurrentIndex(-1)
                    panel_preset.setPlaceholderText("Custom")
                else:
                    blocker = QtCore.QSignalBlocker(panel_preset)
                    panel_preset.setCurrentIndex(index)
                    del blocker

            if self.embedded_ui_tuner is not None:
                self.embedded_ui_tuner.sync_from_owner()
            if self.embedded_rail_design is not None:
                self.embedded_rail_design.sync_from_owner()
            if self.embedded_diagnostics is not None:
                self.embedded_diagnostics.sync_testing_state()
        finally:
            self._syncing = False

    def showEvent(self, event: QtGui.QShowEvent) -> None:


        self._prewarm_running = False
        self.sync_from_owner()
        self._refresh_frame_benchmark()
        self._fit_to_available_screen(prefer_default=False)
        super().showEvent(event)
        if self.categories.currentRow() == self.CATEGORIES.index("Advanced"):
            QtCore.QTimer.singleShot(0, self._ensure_current_developer_tool)

