"""Shell configuration dialogs and news ribbon."""
from __future__ import annotations


from collections import deque
from copy import deepcopy
import math
import time
from uuid import uuid4

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal

from ..utilities import TextRole, apply_text_render_hints, set_text_role
from ..utilities import device_pixel_value
from ..constants import (
    MARKET_BAR_TIMEFRAME_LIMIT, MARKET_BAR_TIMEFRAME_PRESETS, TIMEFRAMES,
    SHELL_RESERVED_SHORTCUTS,
)


class MicrostructureNewsCard(QtWidgets.QFrame):
    clicked = Signal()


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
        self._scroll_speed = 45.0
        self._last_frame = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._update_frame_interval()
        self._timer.timeout.connect(self._advance)
        set_text_role(self, TextRole.NEWS_TEXT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Current-symbol microstructure signals")
        self._reset_idle_state()

    def _update_frame_interval(self) -> None:
        """Run ticker updates at the active monitor refresh rate or faster."""
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        refresh_hz = float(screen.refreshRate()) if screen is not None else self._FALLBACK_REFRESH_HZ
        if not math.isfinite(refresh_hz) or refresh_hz <= 0.0:
            refresh_hz = self._FALLBACK_REFRESH_HZ


        interval_ms = max(1, int(1000.0 / refresh_hz))
        self._timer.setInterval(interval_ms)

    def _reset_idle_state(self) -> None:
        self._text = ""
        self._text_width = 0
        _left, right = self._ticker_bounds()
        self._offset = right
        self._last_frame = time.monotonic()
        self._timer.stop()
        self.update()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self._update_frame_interval()
        self._last_frame = time.monotonic()
        if not self._text:
            self._reset_idle_state()
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
        self.setToolTip(self._hover_html)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)

    def _stop_signal(self) -> None:
        self._passes_remaining = 0
        self._copies_started = 0
        self._reset_idle_state()

    def _advance(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_frame)
        self._last_frame = now

        if not self._text:
            self._timer.stop()
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
        if not self._text:
            return
        painter = QtGui.QPainter(self)
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
from ..theme import (
    RIGHT_LAYOUT_PRESETS, THEMES, ORDERBOOK_THEMES,
    candle_directional_palette, chart_palette,
)
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
from .panels import (
    PANEL_IDS, PanelNode, SplitNode, decode_tree, detach_panel, encode_tree,
    insert_panel, panel_ids, replace_split_weights, split_node, validate_tree,
    valid_panel_names,
)


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
        if self.level_editors[indicator]:
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
        *, reserved_shortcuts: set[str] | None = None,
    ):
        super().__init__(parent)
        self.reserved_shortcuts = set(SHELL_RESERVED_SHORTCUTS) | set(reserved_shortcuts or ())
        self.setWindowTitle("Indicator shortcuts")
        self.setMinimumWidth(470)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("INDICATOR SHORTCUTS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "Assign Ctrl shortcuts to chart indicators. Number keys follow the market bar's selected timeframes from left to right."
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
        conflicts = sorted(set(active) & self.reserved_shortcuts)
        if conflicts:
            QtWidgets.QMessageBox.warning(
                self, "Shortcut already in use",
                "These shortcuts are reserved by Nightwatch: " + ", ".join(conflicts),
            )
            return
        self.accept()

    def shortcuts(self) -> dict[str, str]:
        return {
            indicator: str(editor.currentData() or "")
            for indicator, editor in self.editors.items()
        }


class _WorkspacePanelSource(QtWidgets.QToolButton):
    """A panel in the library; click to add or drag into a docking target."""

    MIME = "application/x-nightwatch-workspace-panel"

    def __init__(self, name, panel_id, token, parent=None):
        super().__init__(parent)
        self.panel_id, self.token = panel_id, token
        self.setText(RIGHT_PANEL_LABELS.get(name, name).title() + "  +")
        self.setAccessibleName(f"Add {name}; drag to choose a position")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedHeight(30)
        self._press = None

    def mousePressEvent(self, event):
        self._press = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength()
                >= QtWidgets.QApplication.startDragDistance()):
            self._press = None
            self.setDown(False)
            mime = QtCore.QMimeData()
            mime.setData(self.MIME, f"{self.token}:{self.panel_id}".encode())
            drag = QtGui.QDrag(self)
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(QtCore.QPoint(self.width() // 2, self.height() // 2))
            drag.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)


class _WorkspacePresetCanvas(QtWidgets.QWidget):
    """Event-driven miniature workspace. No live panel widgets or data feeds."""

    changed = Signal(object)
    selection_changed = Signal(str)
    history_changed = Signal(bool, bool)

    def __init__(self, aliases, token, parent=None):
        super().__init__(parent)
        self.aliases, self.token = dict(aliases), token
        self.titles = {pid: name for name, pid in aliases.items()}
        self.root = None
        self.rail_width = None
        self.selected = ""
        self._undo, self._redo = [], []
        self._rects, self._handles = {}, []
        self._press = self._drag_panel = self._drop = self._resize = None
        self.accent = QtGui.QColor("#f0b34a")
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Workspace preview. Drag panels to dock, drag dividers to resize. Tab selects a panel; Delete removes it.")

    def set_tree(self, root):
        validate_tree(root)
        self.root = root
        self._undo.clear()
        self._redo.clear()
        self._press = self._drag_panel = self._drop = self._resize = None
        self._select("")
        self._geometry()
        self.history_changed.emit(False, False)
        self.update()

    def _select(self, pid):
        if self.selected != pid:
            self.selected = pid
            self.setAccessibleDescription(self.titles.get(pid, "No panel selected"))
            self.selection_changed.emit(pid)
            self.update()

    def _geometry(self):
        frame = QtCore.QRectF(self.rect()).adjusted(12, 12, -12, -12)
        chart_width = min(150.0, frame.width() * .22)
        self._chart_rect = QtCore.QRectF(frame.x(), frame.y(), chart_width, frame.height())
        self._rail_rect = frame.adjusted(chart_width + 8, 0, 0, 0)
        self._rects, self._handles = {}, []

        def minimum(node, axis):
            if isinstance(node, PanelNode):
                return 68 if axis == "h" else 48
            values = [minimum(child, axis) for child in node.children]
            return sum(values) + 6 * (len(values) - 1) if node.axis == axis else max(values)

        def visit(node, rect):
            if isinstance(node, PanelNode):
                self._rects[node.id] = rect
            elif isinstance(node, SplitNode):
                horizontal = node.axis == "h"
                usable = max(1.0, (rect.width() if horizontal else rect.height()) - 6 * (len(node.children) - 1))
                minima = [minimum(child, node.axis) for child in node.children]
                extents = [0.0] * len(node.children)
                if sum(minima) >= usable:
                    extents = [value * usable / sum(minima) for value in minima]
                else:
                    remaining, available = set(range(len(extents))), usable
                    while remaining:
                        mass = sum(node.weights[index] for index in remaining)
                        small = [index for index in remaining
                                 if available * node.weights[index] / mass < minima[index]]
                        if not small:
                            for index in remaining:
                                extents[index] = available * node.weights[index] / mass
                            break
                        for index in small:
                            extents[index] = minima[index]
                            available -= minima[index]
                            remaining.remove(index)
                # Clamp the preview only. Resizing starts from the displayed
                # proportions; loading a preset does not alter its saved tree.
                displayed = split_node(node.axis, node.children, extents, node.id)
                offset = 0.0
                for index, (child, extent) in enumerate(zip(node.children, extents)):
                    cell = (QtCore.QRectF(rect.x() + offset, rect.y(), extent, rect.height()) if horizontal
                            else QtCore.QRectF(rect.x(), rect.y() + offset, rect.width(), extent))
                    visit(child, cell)
                    offset += extent
                    if index < len(node.children) - 1:
                        handle = (QtCore.QRectF(rect.x() + offset, rect.y(), 6, rect.height()) if horizontal
                                  else QtCore.QRectF(rect.x(), rect.y() + offset, rect.width(), 6))
                        self._handles.append((displayed, index, handle, usable))
                        offset += 6
        visit(self.root, self._rail_rect)

    def resizeEvent(self, event):
        self._geometry()
        super().resizeEvent(event)

    def _commit(self, root, before=None):
        validate_tree(root)
        if not set(panel_ids(root)) <= set(self.titles):
            raise ValueError("Unknown workspace panel")
        previous = self.root if before is None else before
        if root == previous:
            return
        self._undo.append((previous, self.rail_width))
        self._undo = self._undo[-40:]
        self._redo.clear()
        self.root = root
        if self.selected not in panel_ids(root):
            self._select("")
        self._geometry()
        self.history_changed.emit(bool(self._undo), False)
        self.changed.emit(None)
        self.update()

    def undo(self):
        if self._undo:
            self._redo.append((self.root, self.rail_width))
            self.root, width = self._undo.pop()
            self._history_applied(width)

    def redo(self):
        if self._redo:
            self._undo.append((self.root, self.rail_width))
            self.root, width = self._redo.pop()
            self._history_applied(width)

    def _history_applied(self, width):
        self._select("")
        self._geometry()
        self.history_changed.emit(bool(self._undo), bool(self._redo))
        self.changed.emit(width)
        self.update()

    def add_panel(self, pid):
        if pid in self.titles and pid not in panel_ids(self.root):
            leaves = panel_ids(self.root)
            self._commit(insert_panel(self.root, PanelNode(pid), leaves[-1] if leaves else None, "below"))
            self._select(pid)

    def remove_panel(self, pid=None):
        pid = pid or self.selected
        if pid in panel_ids(self.root):
            self._commit(detach_panel(self.root, pid))

    def template(self, axis):
        self._commit(split_node(axis, [PanelNode(pid) for pid in panel_ids(self.root)]))

    def equalize(self):
        def visit(node):
            return (split_node(node.axis, [visit(c) for c in node.children], node_id=node.id)
                    if isinstance(node, SplitNode) else node)
        self._commit(visit(self.root))

    def _target(self, point, source):
        if not self._rail_rect.contains(point):
            return None
        if self.root is None:
            return (None, "below", self._rail_rect)
        target = next((pid for pid, rect in self._rects.items() if rect.contains(point)), None)
        if target is None or target == source:
            return None
        rect = self._rects[target]
        x, y = (point.x() - rect.x()) / rect.width(), (point.y() - rect.y()) / rect.height()
        distances = {"left": x, "right": 1 - x, "above": y, "below": 1 - y}
        edge = min(distances, key=distances.get)
        if min(distances.values()) > .27 and source in self._rects:
            return (target, "swap", rect)
        overlay = QtCore.QRectF(rect)
        if edge in ("left", "right"):
            overlay.setWidth(rect.width() / 2)
            if edge == "right":
                overlay.moveRight(rect.right())
        else:
            overlay.setHeight(rect.height() / 2)
            if edge == "below":
                overlay.moveBottom(rect.bottom())
        return (target, edge, overlay)

    def dock_panel(self, source, target, edge):
        if source not in self.titles or source == target or edge not in ("left", "right", "above", "below", "swap"):
            return
        leaves = panel_ids(self.root)
        if self.root is not None and target not in leaves:
            return
        if edge == "swap":
            if source not in leaves:
                return
            def swap(node):
                if isinstance(node, PanelNode):
                    return PanelNode(target if node.id == source else source if node.id == target else node.id)
                return split_node(node.axis, [swap(c) for c in node.children], node.weights, node.id)
            root = swap(self.root)
        else:
            root = insert_panel(detach_panel(self.root, source), PanelNode(source), target, edge)
        self._commit(root)
        self._select(source)

    def _mime_panel(self, mime):
        if not mime.hasFormat(_WorkspacePanelSource.MIME):
            return None
        try:
            token, pid = bytes(mime.data(_WorkspacePanelSource.MIME)).decode().split(":", 1)
        except (ValueError, UnicodeError):
            return None
        return pid if token == self.token and pid in self.titles and pid not in self._rects else None

    def dragEnterEvent(self, event):
        if self._mime_panel(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        pid = self._mime_panel(event.mimeData())
        self._drop = self._target(event.position(), pid) if pid else None
        if self._drop:
            event.acceptProposedAction()
        else:
            event.ignore()
        self.update()

    def dragLeaveEvent(self, event):
        self._drop = None
        self.update()
        event.accept()

    def dropEvent(self, event):
        pid = self._mime_panel(event.mimeData())
        target = self._target(event.position(), pid) if pid else None
        self._drop = None
        if target:
            self.dock_panel(pid, target[0], target[1])
            event.acceptProposedAction()
        else:
            event.ignore()
        self.update()

    @staticmethod
    def _close_rect(rect):
        return QtCore.QRectF(rect.right() - 24, rect.top() + 6, 18, 18)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        for node, index, rect, usable in reversed(self._handles):
            if rect.contains(point):
                self._resize = (node, index, usable, point, self.root)
                self.setCursor(Qt.CursorShape.SplitHCursor if node.axis == "h" else Qt.CursorShape.SplitVCursor)
                return
        pid = next((pid for pid, rect in self._rects.items() if rect.contains(point)), "")
        self._select(pid)
        if pid and self._close_rect(self._rects[pid]).contains(point):
            self.remove_panel(pid)
        elif pid:
            self._press = (pid, point)

    def mouseMoveEvent(self, event):
        point = event.position()
        if self._resize:
            node, index, usable, start, before = self._resize
            delta = point.x() - start.x() if node.axis == "h" else point.y() - start.y()
            weights = list(node.weights)
            pair = weights[index] + weights[index + 1]
            def minimum(child):
                if isinstance(child, PanelNode):
                    return 68 if node.axis == "h" else 48
                values = [minimum(c) for c in child.children]
                return sum(values) + 6 * (len(values) - 1) if child.axis == node.axis else max(values)
            lower = min(pair * .45, minimum(node.children[index]) / max(1, usable))
            upper = pair - min(pair * .45, minimum(node.children[index + 1]) / max(1, usable))
            first = max(lower, min(upper, weights[index] + delta / max(1, usable)))
            weights[index], weights[index + 1] = first, pair - first
            self.root = replace_split_weights(before, node.id, weights)
            self._geometry()
            self.update()
            return
        if self._press and event.buttons() & Qt.MouseButton.LeftButton:
            pid, start = self._press
            if (point - start).manhattanLength() >= QtWidgets.QApplication.startDragDistance():
                self._drag_panel = pid
                self._drop = self._target(point, pid)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
            return
        handle = next((h for h in reversed(self._handles) if h[2].contains(point)), None)
        cursor = (Qt.CursorShape.SplitHCursor if handle[0].axis == "h" else Qt.CursorShape.SplitVCursor) if handle else (
            Qt.CursorShape.OpenHandCursor if any(r.contains(point) for r in self._rects.values()) else Qt.CursorShape.ArrowCursor)
        self.setCursor(cursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self._resize:
            before = self._resize[4]
            root = self.root
            self.root = before
            self._commit(root)
        elif self._drag_panel:
            target = self._target(event.position(), self._drag_panel)
            if target:
                self.dock_panel(self._drag_panel, target[0], target[1])
        self._press = self._drag_panel = self._drop = self._resize = None
        self.unsetCursor()
        self.update()

    def cancel_gesture(self):
        if not (self._press or self._drag_panel or self._drop or self._resize):
            return False
        if self._resize:
            self.root = self._resize[4]
            self._geometry()
        self._press = self._drag_panel = self._drop = self._resize = None
        self.unsetCursor()
        self.update()
        return True

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_panel()
        elif event.key() == Qt.Key.Key_Escape:
            if not self.cancel_gesture():
                super().keyPressEvent(event)
        elif event.matches(QtGui.QKeySequence.StandardKey.Undo):
            self.undo()
        elif event.matches(QtGui.QKeySequence.StandardKey.Redo):
            self.redo()
        else:
            super().keyPressEvent(event)

    def focusNextPrevChild(self, next):
        leaves = panel_ids(self.root)
        if self.hasFocus() and leaves:
            index = leaves.index(self.selected) if self.selected in leaves else (-1 if next else len(leaves))
            index += 1 if next else -1
            if 0 <= index < len(leaves):
                self._select(leaves[index])
                return True
        return super().focusNextPrevChild(next)

    def contextMenuEvent(self, event):
        pid = next((pid for pid, rect in self._rects.items() if rect.contains(event.pos())), self.selected)
        if not pid:
            return
        self._select(pid)
        menu = QtWidgets.QMenu(self)
        for target in panel_ids(self.root):
            if target == pid:
                continue
            moves = menu.addMenu(f"Move relative to {self.titles[target]}")
            for edge, title in (("left", "Left"), ("right", "Right"), ("above", "Above"), ("below", "Below"), ("swap", "Swap")):
                moves.addAction(title, lambda checked=False, source=pid, target=target, edge=edge: self.dock_panel(source, target, edge))
        menu.addSeparator()
        menu.addAction("Remove panel", lambda: self.remove_panel(pid))
        menu.exec(event.globalPos())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        apply_text_render_hints(painter)
        painter.fillRect(self.rect(), QtGui.QColor("#000000"))
        font = QtGui.QFont(self.font())
        font.setPointSizeF(9)
        painter.setFont(font)
        chart = self._chart_rect
        painter.setPen(QtGui.QPen(QtGui.QColor("#222222"), 1))
        painter.setBrush(QtGui.QColor("#080808"))
        painter.drawRoundedRect(chart, 8, 8)
        painter.setPen(QtGui.QColor("#777777"))
        painter.drawText(chart.adjusted(12, 10, -8, -8), Qt.AlignmentFlag.AlignTop, "CHART")
        painter.setPen(QtGui.QPen(QtGui.QColor("#161616"), 1))
        for row in range(1, 6):
            y = chart.y() + chart.height() * row / 6
            painter.drawLine(QtCore.QPointF(chart.left() + 8, y), QtCore.QPointF(chart.right() - 8, y))
        painter.setPen(QtGui.QPen(QtGui.QColor("#484848"), 1.4))
        path = QtGui.QPainterPath()
        for i in range(24):
            point = QtCore.QPointF(chart.x() + 10 + (chart.width() - 20) * i / 23,
                                  chart.y() + chart.height() * (.64 - i * .01 + .1 * math.sin(i * .8)))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        painter.drawPath(path)
        if self.root is None:
            painter.setPen(QtGui.QPen(QtGui.QColor("#363636"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QtGui.QColor("#080808"))
            painter.drawRoundedRect(self._rail_rect.adjusted(1, 1, -1, -1), 8, 8)
            painter.setPen(QtGui.QColor("#999999"))
            painter.drawText(self._rail_rect, Qt.AlignmentFlag.AlignCenter, "Drag a panel here\nor click a panel below")
        for pid, rect in self._rects.items():
            active = pid == self.selected
            painter.setPen(QtGui.QPen(self.accent if active else QtGui.QColor("#333333"), 1.3 if active else 1))
            painter.setBrush(QtGui.QColor("#141414" if active else "#0d0d0d"))
            painter.drawRoundedRect(rect.adjusted(.7, .7, -.7, -.7), 7, 7)
            painter.save()
            painter.setClipRect(rect.adjusted(3, 3, -3, -3))
            painter.setPen(QtGui.QColor("#606060"))
            for y in (12, 17, 22):
                for x in (8, 12):
                    painter.drawPoint(QtCore.QPointF(rect.x() + x, rect.y() + y))
            painter.setPen(QtGui.QColor("#eeeeee"))
            label = RIGHT_PANEL_LABELS.get(self.titles[pid], self.titles[pid]).title()
            title_rect = rect.adjusted(21, 7, -28, 0)
            title_rect.setHeight(20)
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, max(0, int(title_rect.width()))))
            close = self._close_rect(rect)
            painter.setPen(QtGui.QColor("#888888"))
            painter.drawLine(close.topLeft() + QtCore.QPointF(6, 6), close.bottomRight() - QtCore.QPointF(6, 6))
            painter.drawLine(close.topRight() + QtCore.QPointF(-6, 6), close.bottomLeft() + QtCore.QPointF(6, -6))
            body = rect.adjusted(12, 36, -12, -12)
            if body.height() > 4 and body.width() > 4:
                painter.setPen(Qt.PenStyle.NoPen)
                for row in range(min(12, int(body.height() / 14))):
                    y = body.y() + row * 14
                    if pid == "depth":
                        width = body.width() * (.2 + .22 * (1 + math.sin(row * .7)))
                        painter.setBrush(QtGui.QColor("#27352c" if row > 5 else "#38282a"))
                        painter.drawRoundedRect(QtCore.QRectF(body.right() - width, y, width, 8), 2, 2)
                    painter.setBrush(QtGui.QColor("#303030"))
                    painter.drawRoundedRect(QtCore.QRectF(body.x(), y, body.width() * (.26 if pid != "trading" else .8), 4), 2, 2)
                    if pid != "trading":
                        painter.drawRoundedRect(QtCore.QRectF(body.x() + body.width() * .55, y, body.width() * .23, 4), 2, 2)
            painter.restore()
        painter.setPen(QtGui.QPen(QtGui.QColor("#4a4a4a"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for node, index, rect, usable in self._handles:
            center = rect.center()
            offset = QtCore.QPointF(0, 10) if node.axis == "h" else QtCore.QPointF(10, 0)
            painter.drawLine(center - offset, center + offset)
        if self._drop:
            target, edge, rect = self._drop
            color = QtGui.QColor(self.accent)
            color.setAlpha(45)
            painter.setBrush(color)
            painter.setPen(QtGui.QPen(self.accent, 1.5))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            painter.setPen(self.accent)
            label = "Swap panels" if edge == "swap" else "Place " + edge if target else "Add panel"
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class WorkspacePresetEditor(QtWidgets.QWidget):
    """Draft editor shared by Settings and the preset dialog."""

    apply_requested = Signal(object, str)

    def __init__(self, presets, parent=None, *, panel_names=None, aliases=None,
                 active_name=None, current_layout_provider=None):
        super().__init__(parent)
        self.setObjectName("workspacePresetEditor")
        self.panel_names = tuple(RIGHT_PANEL_NAMES if panel_names is None else panel_names)
        self.aliases = {name: (aliases or PANEL_IDS).get(name, name) for name in self.panel_names}
        self._current_layout_provider = current_layout_provider
        self._loading = False
        self.dirty = False
        self._source_presets = None
        self._token = uuid4().hex
        self.setStyleSheet("""
            QWidget#workspacePresetEditor { background: #000000; color: #eeeeee; }
            QWidget#workspacePresetEditor QLabel { background: transparent; border: 0; color: #dddddd; }
            QWidget#workspacePresetEditor QListWidget { background: #080808; border: 1px solid #272727; border-radius: 8px; padding: 5px; outline: 0; }
            QWidget#workspacePresetEditor QListWidget::item { border-radius: 5px; padding: 8px; margin: 2px 0; color: #bbbbbb; }
            QWidget#workspacePresetEditor QListWidget::item:selected { background: #24211a; color: #f0b34a; }
            QWidget#workspacePresetEditor QLineEdit, QWidget#workspacePresetEditor QSpinBox { background: #101010; border: 1px solid #333333; border-radius: 5px; padding: 5px 8px; color: #eeeeee; min-height: 0; }
            QWidget#workspacePresetEditor QPushButton, QWidget#workspacePresetEditor QToolButton { background: #141414; border: 1px solid #333333; border-radius: 5px; padding: 6px 10px; color: #dddddd; min-height: 0; }
            QWidget#workspacePresetEditor QPushButton:hover, QWidget#workspacePresetEditor QToolButton:hover { background: #222222; border-color: #777777; }
            QWidget#workspacePresetEditor QPushButton:disabled, QWidget#workspacePresetEditor QToolButton:disabled { color: #666666; border-color: #222222; }
            QWidget#workspacePresetEditor QPushButton#workspaceSave { background: #f0b34a; border-color: #f0b34a; color: #080808; font-weight: 600; }
            QWidget#workspacePresetEditor QLabel#workspaceHint { color: #888888; }
        """)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)
        sidebar = QtWidgets.QVBoxLayout()
        sidebar.setSpacing(8)
        sidebar.addWidget(QtWidgets.QLabel("PRESETS"))
        self.presets = QtWidgets.QListWidget()
        self.presets.setAccessibleName("Workspace presets; drag to reorder the layout cycle")
        self.presets.setMinimumWidth(155)
        self.presets.setMaximumWidth(200)
        self.presets.setIconSize(QtCore.QSize(44, 34))
        self.presets.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.presets.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.presets.setDefaultDropAction(Qt.DropAction.MoveAction)
        sidebar.addWidget(self.presets, 1)
        for title, callback in (("New preset", self._new), ("Duplicate", self._duplicate), ("Delete preset", self._delete)):
            button = self._button(title, callback)
            sidebar.addWidget(button)
            if title == "Delete preset":
                self.delete_button = button
        body.addLayout(sidebar)
        stage = QtWidgets.QVBoxLayout()
        stage.setSpacing(9)
        body.addLayout(stage, 1)
        name_row = QtWidgets.QHBoxLayout()
        self.name = QtWidgets.QLineEdit()
        self.name.setFixedHeight(34)
        self.name.setMaxLength(32)
        self.name.setPlaceholderText("Preset name")
        self.name.setAccessibleName("Preset name")
        name_row.addWidget(self.name, 1)
        name_row.addWidget(QtWidgets.QLabel("Width"))
        self.rail_width_spin = QtWidgets.QSpinBox()
        self.rail_width_spin.setFixedHeight(34)
        self.rail_width_spin.setRange(300, 2400)
        self.rail_width_spin.setSuffix(" px")
        self.rail_width_spin.setAccessibleName("Saved right panel width in pixels")
        name_row.addWidget(self.rail_width_spin)
        stage.addLayout(name_row)
        tools = QtWidgets.QHBoxLayout()
        for title, callback in (("Stack", lambda: self.canvas.template("v")),
                                ("Columns", lambda: self.canvas.template("h")),
                                ("Even sizes", lambda: self.canvas.equalize())):
            tools.addWidget(self._button(title, callback))
        tools.addStretch(1)
        self.undo_button = self._button("↶", lambda: self.canvas.undo())
        self.undo_button.setAccessibleName("Undo panel arrangement")
        self.redo_button = self._button("↷", lambda: self.canvas.redo())
        self.redo_button.setAccessibleName("Redo panel arrangement")
        tools.addWidget(self.undo_button)
        tools.addWidget(self.redo_button)
        stage.addLayout(tools)
        self.canvas = _WorkspacePresetCanvas(self.aliases, self._token)
        stage.addWidget(self.canvas, 1)
        panel_row = QtWidgets.QGridLayout()
        panel_row.setSpacing(6)
        self.sources = {}
        for index, name in enumerate(self.panel_names):
            pid = self.aliases[name]
            source = _WorkspacePanelSource(name, pid, self._token)
            source.clicked.connect(lambda checked=False, pid=pid: self.canvas.add_panel(pid))
            self.sources[pid] = source
            panel_row.addWidget(source, index // 2, index % 2)
        stage.addLayout(panel_row)
        hint = QtWidgets.QLabel("Drag to an edge to split · Drag to the center to swap · Drag dividers to resize")
        hint.setObjectName("workspaceHint")
        hint.setWordWrap(True)
        stage.addWidget(hint)
        footer = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel()
        self.status.setObjectName("workspaceHint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        footer.addWidget(self._button("Use current workspace", self._capture))
        options = self._button("More", lambda: None)
        menu = QtWidgets.QMenu(options)
        menu.addAction("Clear panels", lambda: self.canvas._commit(None))
        menu.addAction("Restore default presets", self._restore_defaults)
        menu.addAction("Revert unsaved edits", self.revert)
        options.setMenu(menu)
        footer.addWidget(options)
        footer.addStretch(1)
        self.save_button = self._button("Save && apply", self._save)
        self.save_button.setObjectName("workspaceSave")
        footer.addWidget(self.save_button)
        root.addLayout(footer)
        self.name.textEdited.connect(self._name_changed)
        self.rail_width_spin.valueChanged.connect(self._width_changed)
        self.presets.currentRowChanged.connect(self._load_selected)
        self.presets.model().rowsMoved.connect(self._mark_dirty)
        self.canvas.changed.connect(self._canvas_changed)
        self.canvas.history_changed.connect(self._history_changed)
        self.set_presets(presets, active_name, force=True)

    @staticmethod
    def _button(title, callback):
        button = QtWidgets.QPushButton(title)
        button.setFixedHeight(30)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        return button

    def _normalized(self, definition):
        result = deepcopy(definition)
        visible = valid_panel_names(result.get("visible", ()), self.panel_names)
        try:
            tree = decode_tree(result.get("tree"))
            validate_tree(tree)
        except (ValueError, TypeError, RecursionError):
            tree = None
        wanted = {self.aliases[name] for name in visible}
        for pid in panel_ids(tree):
            if pid not in wanted:
                tree = detach_panel(tree, pid)
        for name in visible:
            pid = self.aliases[name]
            if pid not in panel_ids(tree):
                leaves = panel_ids(tree)
                tree = insert_panel(tree, PanelNode(pid), leaves[-1] if leaves else None, "below")
        result["tree"] = encode_tree(tree)
        result["visible"] = tuple(self.canvas.titles[pid] for pid in panel_ids(tree))
        try:
            result["rail_width"] = max(300, min(2400, int(result.get("rail_width", 660))))
        except (TypeError, ValueError, OverflowError):
            result["rail_width"] = 660
        return result

    def set_presets(self, presets, active_name=None, *, force=False):
        if self.dirty and not force:
            return
        # The owner replaces this dictionary on save/reset. Ordinary state
        # synchronization therefore needs no recursive preset comparison.
        if not force and presets is self._source_presets:
            return
        self._source_presets = presets
        if not force and presets == self._saved_presets:
            return
        self._saved_presets = deepcopy(presets)
        self._saved_active = active_name
        self._loading = True
        self.presets.clear()
        for name, definition in presets.items():
            self._add_item(name, definition)
        if self.presets.count() == 0:
            self._add_item("My workspace", {"visible": (), "tree": None})
        names = [self.presets.item(i).data(Qt.ItemDataRole.UserRole)["name"] for i in range(self.presets.count())]
        self._loading = False
        self.presets.setCurrentRow(names.index(active_name) if active_name in names else 0)
        self._load_selected(self.presets.currentRow())
        self.dirty = False
        self.status.setText("Arrange your panels, then save to use this layout.")

    def _add_item(self, name, definition):
        item = QtWidgets.QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"name": name, "definition": self._normalized(definition)})
        item.setSizeHint(QtCore.QSize(155, 62))
        self.presets.addItem(item)
        self._refresh_item(item)
        return item

    def _refresh_item(self, item):
        record = item.data(Qt.ItemDataRole.UserRole)
        tree = decode_tree(record["definition"]["tree"])
        item.setText(record["name"] + "\n" + f"{len(panel_ids(tree))} panels")
        item.setToolTip(record["name"])
        pixmap = QtGui.QPixmap(44, 34)
        pixmap.fill(QtGui.QColor("#000000"))
        painter = QtGui.QPainter(pixmap)
        def draw(node, rect):
            if isinstance(node, PanelNode):
                painter.fillRect(rect.adjusted(1, 1, -1, -1), QtGui.QColor("#69645a"))
            elif isinstance(node, SplitNode):
                offset = 0.0
                for child, weight in zip(node.children, node.weights):
                    size = (rect.width() if node.axis == "h" else rect.height()) * weight
                    cell = (QtCore.QRectF(rect.x() + offset, rect.y(), size, rect.height()) if node.axis == "h"
                            else QtCore.QRectF(rect.x(), rect.y() + offset, rect.width(), size))
                    draw(child, cell)
                    offset += size
        draw(tree, QtCore.QRectF(0, 0, 44, 34))
        painter.end()
        icon = QtGui.QIcon(pixmap)
        icon.addPixmap(pixmap, QtGui.QIcon.Mode.Selected)
        item.setIcon(icon)

    def _record(self):
        item = self.presets.currentItem()
        return item, item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_selected(self, index):
        if self._loading or index < 0:
            return
        item, record = self._record()
        if record is None:
            return
        self._loading = True
        self.name.setText(record["name"])
        self.rail_width_spin.setValue(record["definition"]["rail_width"])
        self.canvas.rail_width = self.rail_width_spin.value()
        self.canvas.set_tree(decode_tree(record["definition"]["tree"]))
        self._refresh_sources()
        self.delete_button.setEnabled(self.presets.count() > 1)
        self._loading = False

    def _mark_dirty(self, *args):
        if not self._loading:
            self.dirty = True
            self.status.setText("Unsaved changes · Save & apply to use this workspace.")

    def _name_changed(self, text):
        item, record = self._record()
        if record is not None:
            record["name"] = text
            item.setData(Qt.ItemDataRole.UserRole, record)
            self._refresh_item(item)
            self._mark_dirty()

    def _width_changed(self, value):
        self.canvas.rail_width = value
        if self._loading:
            return
        item, record = self._record()
        record["definition"]["rail_width"] = value
        item.setData(Qt.ItemDataRole.UserRole, record)
        self._mark_dirty()

    def _canvas_changed(self, restored_width=None):
        item, record = self._record()
        definition = record["definition"]
        definition["tree"] = encode_tree(self.canvas.root)
        definition["visible"] = tuple(self.canvas.titles[pid] for pid in panel_ids(self.canvas.root))
        def horizontal(node):
            return isinstance(node, SplitNode) and (node.axis == "h" or any(horizontal(c) for c in node.children))
        definition["column_mode"] = 2 if horizontal(self.canvas.root) else 1
        item.setData(Qt.ItemDataRole.UserRole, record)
        def minimum_width(node):
            if isinstance(node, PanelNode):
                return 300
            if isinstance(node, SplitNode):
                widths = [minimum_width(c) for c in node.children]
                return sum(widths) + 4 * (len(widths) - 1) if node.axis == "h" else max(widths)
            return 300
        width = (restored_width if restored_width is not None
                 else max(self.rail_width_spin.value(), minimum_width(self.canvas.root)))
        self.rail_width_spin.setValue(width)
        self.canvas.rail_width = self.rail_width_spin.value()
        self._refresh_item(item)
        self._refresh_sources()
        self._mark_dirty()

    def _refresh_sources(self):
        leaves = panel_ids(self.canvas.root)
        for pid, source in self.sources.items():
            source.setEnabled(pid not in leaves)
            source.setText(RIGHT_PANEL_LABELS.get(self.canvas.titles[pid], self.canvas.titles[pid]).title()
                           + ("  ✓" if pid in leaves else "  +"))

    def _history_changed(self, undo, redo):
        self.undo_button.setEnabled(undo)
        self.redo_button.setEnabled(redo)

    def _unique_name(self, base):
        names = {self.presets.item(i).data(Qt.ItemDataRole.UserRole)["name"].strip().casefold()
                 for i in range(self.presets.count())}
        name, index = base[:32], 2
        while name.casefold() in names or name.casefold() == "custom":
            suffix = f" {index}"
            name, index = base[:32 - len(suffix)] + suffix, index + 1
        return name

    def _new(self):
        definition = self._current_layout_provider() if self._current_layout_provider else {"visible": (), "tree": None}
        item = self._add_item(self._unique_name("My workspace"), definition)
        self.presets.setCurrentItem(item)
        self._mark_dirty()
        self.name.setFocus()
        self.name.selectAll()

    def _duplicate(self):
        item, record = self._record()
        duplicate = self._add_item(self._unique_name(record["name"] + " copy"), record["definition"])
        self.presets.setCurrentItem(duplicate)
        self._mark_dirty()
        self.name.setFocus()
        self.name.selectAll()

    def _delete(self):
        if self.presets.count() > 1:
            row = self.presets.currentRow()
            self.presets.takeItem(row)
            self.presets.setCurrentRow(min(row, self.presets.count() - 1))
            self._load_selected(self.presets.currentRow())
            self._mark_dirty()

    def _capture(self):
        if self._current_layout_provider:
            item, record = self._record()
            record["definition"] = self._normalized(self._current_layout_provider())
            item.setData(Qt.ItemDataRole.UserRole, record)
            self._refresh_item(item)
            self._load_selected(self.presets.currentRow())
            self._mark_dirty()

    def _restore_defaults(self):
        saved, active = self._saved_presets, self._saved_active
        self.set_presets(RIGHT_LAYOUT_PRESETS, "Balanced", force=True)
        self._saved_presets, self._saved_active = saved, active
        self._mark_dirty()

    def revert(self):
        self.set_presets(self._saved_presets, self._saved_active, force=True)

    def definitions(self):
        definitions = {}
        for index in range(self.presets.count()):
            record = self.presets.item(index).data(Qt.ItemDataRole.UserRole)
            name = record["name"].strip()
            if not name or name.casefold() == "custom" or name.casefold() in {n.casefold() for n in definitions}:
                raise ValueError("Use a unique name for every preset. Custom is reserved.")
            definition = deepcopy(record["definition"])
            tree = decode_tree(definition["tree"])
            validate_tree(tree)
            if set(panel_ids(tree)) != {self.aliases[n] for n in definition["visible"]}:
                raise ValueError("Panel arrangement does not match the selected panels.")
            definitions[name] = definition
        return definitions

    def selected_name(self):
        item, record = self._record()
        return record["name"].strip()

    def _save(self):
        try:
            definitions = self.definitions()
        except (ValueError, TypeError, RecursionError) as error:
            self.status.setText(str(error))
            self.name.setFocus()
            return
        self.apply_requested.emit(definitions, self.selected_name())

    def mark_saved(self, definitions, active_name):
        for index in range(self.presets.count()):
            item = self.presets.item(index)
            record = item.data(Qt.ItemDataRole.UserRole)
            name = record["name"].strip()
            if name != record["name"]:
                record["name"] = name
                item.setData(Qt.ItemDataRole.UserRole, record)
                self._refresh_item(item)
        self.name.setText(self.selected_name())
        self._source_presets = definitions
        self._saved_presets, self._saved_active = deepcopy(definitions), active_name
        self.dirty = False
        self.status.setText(f"Saved · {active_name}")


class _SettingsPreviewChoice(QtWidgets.QAbstractButton):
    """A selectable, static preview. No timers, worker, or live market feed."""

    def __init__(self, title, preview, chrome, *, kind="app", style="Inked", parent=None):
        super().__init__(parent)
        self.setText(title)
        self.preview = dict(preview)
        self.chrome = dict(chrome)
        self.kind, self.candle_style = kind, style
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{title} {kind} appearance")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._base_height = 94 if kind == "candles" else 108
        self._refresh_metrics()
        self.toggled.connect(lambda _checked: self.update())

    def sizeHint(self):
        return QtCore.QSize(max(164, self.minimumSizeHint().width()), self.height())

    def minimumSizeHint(self):
        return QtCore.QSize(max(130, self.fontMetrics().horizontalAdvance(self.text()) + 38), self.height())

    def _refresh_metrics(self):
        self.setFixedHeight(self._base_height + max(0, self.fontMetrics().height() - 16))
        self.updateGeometry()
        parent = self.parentWidget()
        if isinstance(parent, _SettingsChoiceGrid):
            parent._reflow(parent.width())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.ApplicationFontChange):
            self._refresh_metrics()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        apply_text_render_hints(painter)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        c = self.chrome
        background = c["control_hover"] if self.underMouse() else c["panel2"]
        border = c["text"] if self.isChecked() or self.hasFocus() else c["border"]
        painter.setBrush(QtGui.QColor(background))
        painter.setPen(QtGui.QPen(QtGui.QColor(border), 1.0))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 7, 7)
        painter.setPen(QtGui.QColor(c["text"] if self.isEnabled() else c["muted"]))
        title = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, self.width() - 38))
        title_height = max(20, self.fontMetrics().height())
        painter.drawText(QtCore.QRectF(12, 9, self.width() - 38, title_height), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(c["text"] if self.isChecked() else c["border"]))
        painter.drawEllipse(QtCore.QPointF(self.width() - 16, 9 + title_height / 2), 3, 3)
        preview_top = max(35, title_height + 21)
        area = QtCore.QRectF(10, preview_top, max(1, self.width() - 20), self.height() - preview_top - 10)
        p = self.preview
        painter.fillRect(area, QtGui.QColor(p.get("bg", "#000000")))
        painter.save()
        painter.setClipRect(area)
        if self.kind == "orderbook":
            tiny = QtGui.QFont(self.font())
            tiny.setPixelSize(9)
            painter.setFont(tiny)
            row_height = area.height() / 7
            for index, strength in enumerate((.28, .8, .45, 0, .62, .92, .35)):
                y = area.top() + index * row_height
                if index == 3:
                    painter.setPen(QtGui.QPen(QtGui.QColor(p["price_line"]), 1))
                    painter.drawLine(QtCore.QPointF(area.left(), y + row_height / 2),
                                     QtCore.QPointF(area.right(), y + row_height / 2))
                    continue
                side = "ask" if index < 3 else "bid"
                bar_x = area.left() + area.width() * .37
                painter.fillRect(QtCore.QRectF(bar_x, y, area.width() * .6 * strength, row_height - 1),
                                 QtGui.QColor(p[f"{side}_fill_strong"]))
                low = QtGui.QColor(p[f"{side}_fill"])
                high = QtGui.QColor(p.get(f"{side}_heat_high", p[side]))
                heat = QtGui.QColor(*(round(a + (b - a) * strength)
                                     for a, b in zip(low.getRgb()[:3], high.getRgb()[:3])))
                painter.fillRect(QtCore.QRectF(bar_x - 8, y, 5, row_height - 1), heat)
                painter.setPen(QtGui.QColor(p["text"] if index in (1, 4) else p["dim_price"]))
                painter.drawText(QtCore.QRectF(area.left() + 3, y, area.width() * .3, row_height),
                                 Qt.AlignmentFlag.AlignVCenter, str(60520 - index * 10))
        else:
            plot = area.adjusted(5, 4, -5, -4)
            if self.kind == "app":
                plot.setRight(area.left() + area.width() * .71)
                for index in range(3):
                    tile = QtCore.QRectF(plot.right() + 5, area.top() + 4 + index * area.height() / 3,
                                        max(1, area.right() - plot.right() - 9), area.height() / 3 - 6)
                    painter.fillRect(tile, QtGui.QColor(p["panel2"]))
            for index, (position, up) in enumerate(((.6, True), (.45, True), (.3, True),
                                                    (.4, False), (.28, True), (.18, True), (.35, False))):
                x = plot.left() + (index + .5) * plot.width() / 7
                top = plot.top() + plot.height() * position
                bottom = top + plot.height() * .23
                color = QtGui.QColor(p.get("candle_up", p["green"]) if up else p.get("candle_down", p["red"]))
                painter.setPen(QtGui.QPen(color, 1))
                painter.drawLine(QtCore.QPointF(x, top - 4), QtCore.QPointF(x, bottom + 4))
                body = QtCore.QRectF(x - 3, top, 6, max(2, bottom - top))
                painter.setBrush(Qt.BrushStyle.NoBrush if up and self.candle_style == "Hollow" else color)
                if self.candle_style == "Luminous":
                    glow = QtGui.QColor(color)
                    glow.setAlpha(35)
                    painter.fillRect(body.adjusted(-2, -2, 2, 2), glow)
                painter.drawRect(body)
        painter.restore()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class _SettingsChoiceGrid(QtWidgets.QWidget):
    """Reflow a small preview gallery without imposing a wide dialog minimum."""

    def __init__(self, buttons, parent=None):
        super().__init__(parent)
        self.buttons = tuple(buttons)
        self.columns = 0
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        self.group = QtWidgets.QButtonGroup(self)
        for button in self.buttons:
            self.group.addButton(button)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        self._reflow(640)

    def minimumSizeHint(self):
        # The old column count must never prevent the viewport from shrinking;
        # the resize event will then choose a new count for the actual width.
        width = max((button.minimumSizeHint().width() for button in self.buttons), default=130)
        return QtCore.QSize(width, self.grid.minimumSize().height())

    def _reflow(self, width):
        required = max((button.minimumSizeHint().width() for button in self.buttons), default=150) + 8
        columns = max(1, min(len(self.buttons), (max(0, width) + 8) // required))
        if columns == self.columns:
            return
        for index in range(max(columns, self.columns)):
            self.grid.setColumnStretch(index, 1 if index < columns else 0)
        for index, button in enumerate(self.buttons):
            self.grid.removeWidget(button)
            self.grid.addWidget(button, index // columns, index % columns)
        self.columns = columns
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow(event.size().width())


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
    WORKSPACE_SEARCH_TERMS = (
        "workspace panels panel layout custom presets preset name width "
        "depth trading positions large trades watchlist stack columns even sizes "
        "undo redo new duplicate delete save apply current clear restore defaults revert"
    )
    # Index the controls on lazy pages without constructing their widgets.
    # Action labels/tooltips and option names are added from the live host below.
    PAGE_SEARCH_TERMS = (
        "appearance application theme order book theme ladder heatmap embedded tape candle geometry buy sell colors",
        "chart market bar timeframes shortcuts workspace price scale auto-scale logarithmic fit visible layers "
        "drawing tools measure ruler fibonacci horizontal level clear drawings volume overlay maximum bar height "
        "indicators parameters shortcuts auto fibonacci",
        WORKSPACE_SEARCH_TERMS,
        "trading connection order entry account actions execution live testnet credentials quick trading magnetic rail",
        "alerts delivery notifications market data history download candles CSV research database",
        "advanced guidance application help order book guide chart performance benchmark FPS frame timing capture "
        "developer tools UI tuner diagnostics magnetic rail",
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
        self._preferred_size = QtCore.QSize(1120, 760)
        self._preferred_minimum_size = QtCore.QSize(760, 540)
        self.resize(self._preferred_size)
        self.setMinimumSize(self._preferred_minimum_size)
        self._action_widgets: dict[QtGui.QAction, QtWidgets.QWidget] = {}
        self._action_widget_labels: dict[QtGui.QAction, str] = {}
        self._settings_search: QtWidgets.QLineEdit | None = None
        self._directional_mode_combos: dict[str, QtWidgets.QComboBox] = {}
        self._appearance_actions: dict[QtGui.QAction, _SettingsPreviewChoice] = {}
        self._orderbook_theme_buttons: dict[str, _SettingsPreviewChoice] = {}
        self.volume_height_spin: QtWidgets.QSpinBox | None = None
        self._timeframe_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._timeframe_presets: dict[str, QtWidgets.QPushButton] = {}
        self._timeframe_preview: QtWidgets.QLabel | None = None
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
        self._developer_tab_keys: list[str] = []
        self._developer_placeholders: dict[str, QtWidgets.QWidget] = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("settingsHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(18, 9, 18, 9)
        header_layout.setSpacing(18)

        heading_block = QtWidgets.QWidget()
        heading_block.setObjectName("settingsHeaderText")
        heading_layout = QtWidgets.QVBoxLayout(heading_block)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(3)
        title = QtWidgets.QLabel("SETTINGS")
        title.setObjectName("settingsHeading")
        heading_layout.addWidget(title)
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
        self.categories.setFixedWidth(158)
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

        self._no_results_page = QtWidgets.QLabel("No matching settings. Clear the search to see all options.")
        self._no_results_page.setObjectName("settingsNoResults")
        self._no_results_page.setWordWrap(True)
        self._no_results_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(self._no_results_page)

        footer = QtWidgets.QFrame()
        footer.setObjectName("settingsFooter")
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        footer_layout.setSpacing(8)
        footer_hint = QtWidgets.QLabel("Ctrl+F Search   ·   Esc Close")
        footer_hint.setObjectName("subtleLabel")
        footer_layout.addWidget(footer_hint)
        footer_layout.addStretch(1)
        close = QtWidgets.QPushButton("CLOSE")
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        footer_layout.addWidget(close)
        root.addWidget(footer)

        self.categories.currentRowChanged.connect(self._on_category_changed)
        self._settings_search_timer = QtCore.QTimer(self)
        self._settings_search_timer.setSingleShot(True)
        self._settings_search_timer.timeout.connect(self._continue_settings_search)
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
        if self._settings_search.text().strip() and not self._settings_search_timer.isActive():
            self._filter_settings_pages(self._settings_search.text().strip().casefold())
        if index == self.CATEGORIES.index("Advanced") and self.isVisible():
            QtCore.QTimer.singleShot(0, self._ensure_current_developer_tool)

    def select_category(self, category: str) -> None:
        if category not in self.CATEGORIES:
            return
        # Opening a category from elsewhere in the app must reveal its controls,
        # even when a search from a previous settings visit excluded the page.
        self._settings_search.clear()
        self.categories.setCurrentRow(self.CATEGORIES.index(category))

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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        content.setObjectName("settingsPage")
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)
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
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(7)
        return box, layout

    @staticmethod
    def _settings_grid(layout: QtWidgets.QVBoxLayout) -> QtWidgets.QGridLayout:
        host = QtWidgets.QWidget()
        host.setObjectName("settingsGridHost")
        grid = QtWidgets.QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
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
        self._settings_search_timer.stop()
        query = str(text or "").strip().casefold()
        if query:
            # Coalesce typing; only the selected search result may build a page.
            self._settings_search_timer.start(100)
        else:
            self._filter_settings_pages(query)

    def _continue_settings_search(self) -> None:
        if not self.isVisible():
            return
        query = self._settings_search.text().strip().casefold()
        self._filter_settings_pages(query)

    def _page_search_metadata(self, index: int) -> str:
        parts = [self.PAGE_SEARCH_TERMS[index]]
        actions: list[QtGui.QAction] = []
        if index == 0:
            actions.extend(self.host.theme_actions.values())
            actions.extend(self.host.candle_style_actions.values())
            parts.extend(label for _mode, label in DIRECTIONAL_COLOR_MODE_OPTIONS)
            parts.extend(ORDERBOOK_THEMES)
        elif index == 1:
            for mapping in (self.host.chart_layout_actions, self.host.chart_visibility_actions,
                            self.host.indicator_actions):
                actions.extend(mapping.values())
            actions.extend((self.host.auto_scale_action, self.host.logarithmic_action,
                            self.host.fit_chart_action, self.host.ruler_action,
                            self.host.fibonacci_action, self.host.horizontal_action,
                            self.host.clear_drawings_action, self.host.indicator_settings_action,
                            self.host.indicator_shortcuts_action, self.host.auto_fibonacci_action))
            parts.extend(TIMEFRAMES)
            parts.extend(MARKET_BAR_TIMEFRAME_PRESETS)
        elif index == 2:
            parts.extend(self.host.right_layout_presets)
            parts.extend(self.host.panel_sections)
        elif index == 3:
            actions.extend(self.host.trading_menu.actions())
        elif index == 4:
            actions.extend(self.host.alert_menu.actions())
            actions.extend(self.host.data_menu.actions())
        elif index == 5:
            parts.append("chart performance benchmark 30 seconds FPS frame timing capture")
            actions.extend(self.host.help_menu.actions())
            app = QtWidgets.QApplication.instance()
            if app is not None and app.property("nightwatchDiagnosticsEnabled"):
                parts.append("DIAGNOSTICS · loads when selected")
        for action in actions:
            parts.extend((action.text().replace("&", ""), action.toolTip()))
            submenu = action.menu()
            if submenu is not None:
                actions.extend(submenu.actions())
        return " ".join(parts).casefold()

    def _filter_settings_pages(self, query: str) -> None:
        first_match = -1
        current = self.categories.currentRow()
        current_visible = False
        for index in range(len(self._page_builders)):
            page = self.pages.widget(index)
            category = self.CATEGORIES[index].casefold() if index < len(self.CATEGORIES) else ""
            category_match = bool(query and query in category)
            if not query or category_match:
                page_match = True
            elif index == self.CATEGORIES.index("Workspace") or index not in self._built_pages:
                page_match = query in self._page_search_metadata(index)
            else:
                page_match = query in self._searchable_text(page)
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
        elif query and first_match < 0:
            self.pages.setCurrentWidget(self._no_results_page)
        elif current >= 0:
            self.pages.setCurrentIndex(current)
        elif not query and current < 0 and self.categories.count():
            self.categories.setCurrentRow(0)

    def _settings_escape(self) -> None:
        editor = getattr(self, "workspace_editor", None)
        if editor is not None and editor.canvas.cancel_gesture():
            return
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
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
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

    def _action_selector(
        self,
        layout: QtWidgets.QVBoxLayout,
        actions: dict[Any, QtGui.QAction],
    ) -> None:
        choices = tuple(actions.values())
        combo = QtWidgets.QComboBox()
        combo.setAccessibleName(layout.parentWidget().title())
        combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
        for action in choices:
            combo.addItem(action.text().replace("&", ""))
        def sync():
            with QtCore.QSignalBlocker(combo):
                for index, action in enumerate(choices):
                    combo.setItemText(index, action.text().replace("&", ""))
                    item = combo.model().item(index)
                    item.setEnabled(action.isEnabled() and action.isVisible())
                    item.setToolTip(action.toolTip())
                    if action.isChecked():
                        combo.setCurrentIndex(index)
        for action in choices:
            action.changed.connect(sync)
        combo.activated.connect(lambda index: choices[index].trigger() if not choices[index].isChecked() else None)
        sync()
        layout.addWidget(combo, 0, Qt.AlignmentFlag.AlignLeft)

    def _build_appearance_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page("Appearance")
        theme_box, theme_layout = self._group("Application theme")
        buttons = []
        for name, action in self.host.theme_actions.items():
            button = _SettingsPreviewChoice(name, THEMES[name], self.host.ui_theme)
            self._bind_appearance_action(action, button)
            buttons.append(button)
        theme_layout.addWidget(_SettingsChoiceGrid(buttons))
        layout.addWidget(theme_box)

        book_box, book_layout = self._group("Order book theme")
        note = QtWidgets.QLabel("Independent colors for the ladder, heatmap, and embedded tape.")
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        book_layout.addWidget(note)
        buttons = []
        for name, palette in ORDERBOOK_THEMES.items():
            button = _SettingsPreviewChoice(name, palette, self.host.ui_theme, kind="orderbook")
            button.clicked.connect(lambda _checked=False, value=name: self.host.set_orderbook_theme(value))
            self._orderbook_theme_buttons[name] = button
            buttons.append(button)
        book_layout.addWidget(_SettingsChoiceGrid(buttons))
        layout.addWidget(book_box)

        candle_box, candle_layout = self._group("Candles")
        color_row = QtWidgets.QHBoxLayout()
        color_row.addWidget(QtWidgets.QLabel("Buy / sell colors"))
        combo = QtWidgets.QComboBox()
        combo.setAccessibleName("Candle buy and sell colors")
        for mode, label in DIRECTIONAL_COLOR_MODE_OPTIONS:
            combo.addItem(label, mode)
        combo.activated.connect(lambda index: self._directional_mode_selected("candles", combo.itemData(index)))
        self._directional_mode_combos["candles"] = combo
        color_row.addWidget(combo)
        color_row.addStretch(1)
        candle_layout.addLayout(color_row)
        buttons = []
        palette = candle_directional_palette(chart_palette(self.host.ui_theme), self.host.directional_color_mode("candles"))
        for name, action in self.host.candle_style_actions.items():
            button = _SettingsPreviewChoice(name, palette, self.host.ui_theme, kind="candles", style=name)
            self._bind_appearance_action(action, button)
            buttons.append(button)
        candle_layout.addWidget(_SettingsChoiceGrid(buttons))
        layout.addWidget(candle_box)
        layout.addStretch(1)
        return page

    def _bind_appearance_action(self, action, button) -> None:
        self._appearance_actions[action] = button
        button.clicked.connect(lambda _checked=False: action.trigger() if not action.isChecked() else None)
        action.changed.connect(lambda: self._sync_appearance_action(action, button))
        self._sync_appearance_action(action, button)

    @staticmethod
    def _sync_appearance_action(action, button) -> None:
        with QtCore.QSignalBlocker(button):
            button.setChecked(action.isChecked())
        button.setEnabled(action.isEnabled())
        button.setVisible(action.isVisible())

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
        page, layout = self._scroll_page("Chart & Indicators")
        timeframe_box, timeframe_layout = self._group("Market bar timeframes")
        timeframe_note = QtWidgets.QLabel(
            "Choose up to nine buttons. Number shortcuts follow their order; your current interval stays available."
        )
        timeframe_note.setObjectName("subtleLabel")
        timeframe_note.setWordWrap(True)
        timeframe_layout.addWidget(timeframe_note)
        presets = QtWidgets.QHBoxLayout()
        for name, intervals in MARKET_BAR_TIMEFRAME_PRESETS.items():
            button = QtWidgets.QPushButton(name)
            button.setObjectName("marketTimeframePreset")
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, values=intervals: self._timeframe_selection_changed(values)
            )
            presets.addWidget(button)
            self._timeframe_presets[name] = button
        timeframe_layout.addLayout(presets)
        choices = QtWidgets.QGridLayout()
        choices.setHorizontalSpacing(6)
        choices.setVerticalSpacing(6)
        for index, interval in enumerate(TIMEFRAMES):
            label = interval.upper() if interval in {"1d", "1w"} else interval
            button = QtWidgets.QPushButton(label)
            button.setObjectName("marketTimeframeChoice")
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setAccessibleName(f"Show {label} in market bar")
            button.toggled.connect(self._timeframe_button_toggled)
            choices.addWidget(button, index // 7, index % 7)
            self._timeframe_buttons[interval] = button
        timeframe_layout.addLayout(choices)
        self._timeframe_preview = QtWidgets.QLabel()
        self._timeframe_preview.setObjectName("subtleLabel")
        self._timeframe_preview.setWordWrap(True)
        timeframe_layout.addWidget(self._timeframe_preview)
        layout.addWidget(timeframe_box)
        grid = self._settings_grid(layout)

        workspace_box, workspace_layout = self._group("Chart workspace")
        self._action_selector(workspace_layout, self.host.chart_layout_actions)
        grid.addWidget(workspace_box, 1, 0, alignment=Qt.AlignmentFlag.AlignTop)

        scale_box, scale_layout = self._group("Price scale")
        self._bind_action(scale_layout, self.host.auto_scale_action, text="Auto-scale price")
        self._bind_action(scale_layout, self.host.logarithmic_action, text="Logarithmic scale")
        self._bind_action(scale_layout, self.host.fit_chart_action, text="Fit chart to visible data")
        grid.addWidget(scale_box, 1, 1, alignment=Qt.AlignmentFlag.AlignTop)

        layers_box, layers_layout = self._group("Visible chart layers")
        self._action_selector(layers_layout, self.host.chart_visibility_actions)
        grid.addWidget(layers_box, 2, 0, alignment=Qt.AlignmentFlag.AlignTop)

        drawing_box, drawing_layout = self._group("Drawing tools")
        for action, label in (
            (self.host.ruler_action, "Measure / ruler"),
            (self.host.fibonacci_action, "Fibonacci"),
            (self.host.horizontal_action, "Horizontal level"),
        ):
            self._bind_action(drawing_layout, action, text=label)
        self._bind_action(drawing_layout, self.host.clear_drawings_action, text="Clear chart drawings")
        grid.addWidget(drawing_box, 2, 1, alignment=Qt.AlignmentFlag.AlignTop)

        volume_box, volume_layout = self._group("Volume overlay")
        volume_note = QtWidgets.QLabel(
            "Volume bars scale to visible data and stay at the bottom of the chart."
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
        grid.addWidget(volume_box, 3, 0, 1, 2, alignment=Qt.AlignmentFlag.AlignTop)

        indicators_box, indicators_layout = self._group("Indicators")
        indicators_note = QtWidgets.QLabel(
            "Enable studies here; use Parameters for detailed configuration."
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
            (self.host.indicator_settings_action, "Parameters…"),
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
        grid.addWidget(indicators_box, 4, 0, 1, 2, alignment=Qt.AlignmentFlag.AlignTop)
        self._refresh_frame_benchmark()
        return page

    def _timeframe_button_toggled(self, _checked: bool) -> None:
        if self._syncing:
            return
        selected = tuple(interval for interval, button in self._timeframe_buttons.items() if button.isChecked())
        if 1 <= len(selected) <= MARKET_BAR_TIMEFRAME_LIMIT:
            self._timeframe_selection_changed(selected)
        else:
            self._sync_timeframes()

    def _timeframe_selection_changed(self, intervals: tuple[str, ...]) -> None:
        if not self._syncing:
            self.host.set_market_bar_timeframes(intervals)
            self._sync_timeframes()

    def _sync_timeframes(self) -> None:
        selected = self.host.market_bar_timeframes
        for interval, button in self._timeframe_buttons.items():
            active = interval in selected
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(active)
            button.setEnabled((active and len(selected) > 1) or (not active and len(selected) < MARKET_BAR_TIMEFRAME_LIMIT))
            del blocker
        for name, button in self._timeframe_presets.items():
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(selected == MARKET_BAR_TIMEFRAME_PRESETS[name])
            del blocker
        if self._timeframe_preview is not None:
            keys = "   ·   ".join(
                f"{index} → {interval.upper() if interval in {'1d', '1w'} else interval}"
                for index, interval in enumerate(selected, 1)
            )
            self._timeframe_preview.setText(f"{len(selected)}/9 selected\n{keys}")

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
        button.setText("Capture running…" if active else "Start 30s capture")
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

    def _build_workspace_page(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        page = QtWidgets.QWidget()
        page.setObjectName("settingsPage")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        active_row = QtWidgets.QHBoxLayout()
        active_row.addWidget(QtWidgets.QLabel("Active layout"))
        self.panel_preset = QtWidgets.QComboBox()
        self.panel_preset.setAccessibleName("Active workspace preset")
        self.panel_preset.activated.connect(self._panel_preset_selected)
        active_row.addWidget(self.panel_preset, 1)
        reset = QtWidgets.QPushButton("Reset live layout")
        reset.setAutoDefault(False)
        reset.clicked.connect(self._reset_panels)
        active_row.addWidget(reset)
        layout.addLayout(active_row)
        controller = self.host.right_rail_controller
        self.workspace_editor = WorkspacePresetEditor(
            self.host.right_layout_presets, panel_names=tuple(self.host.panel_sections),
            aliases=controller.state.aliases, active_name=controller.state.active_preset,
            current_layout_provider=self.host._current_right_panel_definition,
        )
        self.workspace_editor.apply_requested.connect(self._save_workspace_presets)
        layout.addWidget(self.workspace_editor, 1)
        scroll.setWidget(page)
        return scroll

    def _save_workspace_presets(self, definitions, active_name) -> None:
        try:
            self.host._save_right_panel_presets(definitions, active_name)
        except (ValueError, TypeError, RecursionError) as error:
            self.workspace_editor.status.setText(str(error))
            return
        self.workspace_editor.mark_saved(self.host.right_layout_presets, active_name)
        self.sync_from_owner()

    def _build_trading_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page("Trading")
        grid = self._settings_grid(layout)
        groups = {}
        for title, row, column in (("Connection", 0, 0), ("Order entry", 0, 1), ("Account actions", 1, 0)):
            box, section = self._group(title)
            grid.addWidget(box, row, column, alignment=Qt.AlignmentFlag.AlignTop)
            groups[title] = section
        for action in self.host.trading_menu.actions():
            if action.isSeparator():
                continue
            label = action.text().replace("&", "")
            if label in {"Testnet mode", "Live Binance mode"}:
                mode = QtWidgets.QLabel(label)
                mode.setObjectName("subtleLabel")
                groups["Connection"].insertWidget(0, mode)
                continue
            title = ("Connection" if label in {"API credentials", "Test API connection"}
                     else "Order entry" if any(word in label.casefold() for word in ("quick trading", "rail", "open trading"))
                     else "Account actions")
            self._bind_action(groups[title], action)
        return page

    def _build_data_alerts_page(self) -> QtWidgets.QWidget:
        page, layout = self._scroll_page("Data & Alerts")
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
        grid.addWidget(alerts_box, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)

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
        grid.addWidget(data_box, 0, 1, alignment=Qt.AlignmentFlag.AlignTop)
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

        top_host = QtWidgets.QWidget()
        top_host.setObjectName("settingsGridHost")
        top_grid = QtWidgets.QGridLayout(top_host)
        top_grid.setContentsMargins(0, 0, 0, 0)
        top_grid.setHorizontalSpacing(14)
        top_grid.setVerticalSpacing(14)
        top_grid.setColumnStretch(0, 1)
        top_grid.setColumnStretch(1, 1)

        guidance_box, guidance_layout = self._group("Guidance")
        guide = QtWidgets.QPushButton("Open order book guide…")
        guide.setAutoDefault(False)
        guide.clicked.connect(self._open_orderbook_guide)
        guidance_layout.addWidget(guide)
        top_grid.addWidget(guidance_box, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)

        help_box, help_layout = self._group("Application help")
        for action in self.host.help_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                for child in submenu.actions():
                    self._bind_action(help_layout, child)
            else:
                self._bind_action(help_layout, action)
        top_grid.addWidget(help_box, 0, 1, alignment=Qt.AlignmentFlag.AlignTop)
        root.addWidget(top_host)

        benchmark_box, benchmark_layout = self._group("Chart performance")
        note = QtWidgets.QLabel("Pan or zoom for 30 seconds to capture FPS and frame timing.")
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        benchmark_layout.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.frame_benchmark_button = QtWidgets.QPushButton("Start 30s capture")
        self.frame_benchmark_button.setAutoDefault(False)
        self.frame_benchmark_button.clicked.connect(self._start_frame_benchmark)
        row.addWidget(self.frame_benchmark_button)
        self.frame_benchmark_label = QtWidgets.QLabel("Not captured yet.")
        self.frame_benchmark_label.setWordWrap(True)
        self.frame_benchmark_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.frame_benchmark_label, 1)
        benchmark_layout.addLayout(row)
        root.addWidget(benchmark_box)
        self._refresh_frame_benchmark()

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
        dialog = self.findChild(QtWidgets.QDialog, "orderbookGuideDialog")
        if dialog is None:
            dialog = QtWidgets.QDialog(self)
            dialog.setObjectName("orderbookGuideDialog")
            dialog.setWindowTitle("Order book guide")
            dialog.resize(620, 480)
            layout = QtWidgets.QVBoxLayout(dialog)
            guide = QtWidgets.QTextBrowser()
            guide.setHtml("""
                <h2>Order book controls</h2>
                <p><b>Heatmap / Ladder</b> changes the depth view. <b>Qty / Value</b>
                switches between base quantity and quote value.</p>
                <p>The <b>Grouping menu</b> selects a price step; <b>Auto</b> adapts
                grouping to the visible range. <b>Range</b> scales liquidity within
                the rulers; 100% includes the full visible depth.</p>
                <p>The <b>Display menu</b> controls row spacing, depth overlays, and the embedded trade tape.
                Use <b>Reset orderbook display</b> to restore the display defaults.</p>
                <p>Choose a visual theme in <b>Settings → Appearance → Order book theme</b>.</p>
                <h3>Prices and gestures</h3>
                <p>Click a price to prefill the current ticket. An existing unfocused
                price is preserved; focus that field to replace it. Drag the
                heatmap's range ruler to measure a price range. Escape cancels the gesture.</p>
                <p>Prices are selectable only from a current, synchronized book.
                A market switch or reconnect cancels an active gesture.</p>
            """)
            layout.addWidget(guide)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _panel_preset_selected(self, index: int) -> None:
        if self._syncing or index < 0:
            return
        name = str(self.panel_preset.itemData(index) or self.panel_preset.itemText(index))
        if name in self.host.right_layout_presets:
            self.host._apply_right_layout_preset(name)

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
            candle_palette = candle_directional_palette(
                chart_palette(self.host.ui_theme), self.host.directional_color_mode("candles")
            )
            for action, button in self._appearance_actions.items():
                self._sync_appearance_action(action, button)
                button.chrome = dict(self.host.ui_theme)
                if button.kind == "candles":
                    button.preview = dict(candle_palette)
                button.update()
            for name, button in self._orderbook_theme_buttons.items():
                with QtCore.QSignalBlocker(button):
                    button.setChecked(name == self.host.orderbook_theme_name)
                button.chrome = dict(self.host.ui_theme)
                button.update()
            self._sync_timeframes()

            if self.volume_height_spin is not None:
                blocker = QtCore.QSignalBlocker(self.volume_height_spin)
                self.volume_height_spin.setValue(
                    int(self.host.volume_bar_height_setting())
                )
                del blocker

            if self.pages.currentIndex() == self.CATEGORIES.index("Workspace"):
                controller = self.host.right_rail_controller
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

                editor = getattr(self, "workspace_editor", None)
                if editor is not None:
                    editor.set_presets(self.host.right_layout_presets, controller.state.active_preset)

            if self.embedded_ui_tuner is not None:
                self.embedded_ui_tuner.sync_from_owner()
            if self.embedded_rail_design is not None:
                self.embedded_rail_design.sync_from_owner()
            if self.embedded_diagnostics is not None:
                self.embedded_diagnostics.sync_testing_state()
        finally:
            self._syncing = False

    def showEvent(self, event: QtGui.QShowEvent) -> None:


        self.sync_from_owner()
        self._refresh_frame_benchmark()
        self._fit_to_available_screen(prefer_default=False)
        super().showEvent(event)
        if self._settings_search.text().strip():
            self._settings_search_timer.start(0)
        if self.categories.currentRow() == self.CATEGORIES.index("Advanced"):
            QtCore.QTimer.singleShot(0, self._ensure_current_developer_tool)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._settings_search_timer.stop()
        super().hideEvent(event)
