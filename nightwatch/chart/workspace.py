"""Chart rendering, interaction, magnetic rail and auxiliary chart composition."""

from __future__ import annotations
import gc
import math
import time
from copy import deepcopy
from contextlib import contextmanager
import numpy as np
import pyqtgraph as pg
from typing import Any
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal
from ..theme import CANDLE_STYLES
from .preparation import (
    candle_matrix_capacity,
    CandlePages,
    CandleLodIndex,
    PreparedBars,
    RangeExtrema,
    prepare_window,
    prepare_live_tail,
    prepare_snapshot,
    prepare_storage_recovery,
    prepare_event_history,
    parse_liquidation,
    prepare_study,
)
from .analysis import LatestJob, analysis_worker_count, run_analysis, indicator_analysis, profile_analysis, major_level_analysis, fibonacci_analysis
from .rendering import (
    ChartGraphicsView, CandlestickItem, VolumeOverlayItem, NativeBarCompositeItem,
    NativeAreaItem, NativeBandItem,
)
from .retained import RetainedChartItems
from ..utilities import alpha_color, TextRole, set_text_role, typography_controller, typography_font
from ..utilities import hide_hover_tooltip, show_hover_tooltip
from ..presentation import (
    PresentationClock,
    display_refresh_rate,
    performance_profile_active,
    record_frame_request,
    record_performance_count,
    record_performance_sum,
    record_performance_timing,
)
from ..models import (
    chart_y,
    format_price,
    human_number,
    raw_price,
    chart_y_array,
    _candle_matrix_from_objects,
    Candle,
    Zone,
    safe_float,
    perpetual_display_symbol,
    shift_candle_time,
)
from .magnetic_rail import (
    CurrentPriceLineOverlay,
    MagneticOrderRailPanel,
    MagneticRailLineOverlay,
    PriceAxisFocusOverlay,
    ORDER_RAIL_ORDER_PRESET_DEFAULTS,
    normalized_order_rail_config,
    normalized_order_rail_order_preset,
)
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from datetime import datetime, timezone
from ..constants import (
    DEFAULT_INTERVAL,
    INDICATOR_KEYS,
    INDICATOR_SETTING_DEFAULTS,
    INTERVAL_SECONDS,
    MAX_CHART_CANDLES,
    MAX_RENDER_CANDLES,
    DEFAULT_SYMBOL,
    TIMEFRAMES,
)
from ..indicators import AutoFibCandidate, vwap_supports_interval
from ..models import ChartMarketDataFactory, ChartMarketDataPort


def opaque_overlay_color(background: str, foreground: str, alpha: int) -> QtGui.QColor:
    """Preblend an overlay so the chart grid cannot bleed through its fill."""
    surface = QtGui.QColor(background)
    accent = QtGui.QColor(foreground)
    mix = max(0.0, min(1.0, alpha / 255.0))
    return QtGui.QColor(
        round(surface.red() * (1.0 - mix) + accent.red() * mix),
        round(surface.green() * (1.0 - mix) + accent.green() * mix),
        round(surface.blue() * (1.0 - mix) + accent.blue() * mix),
    )


def _fallback_price_decimals(value: float) -> int:
    """Return the decimal precision used by the generic chart price formatter."""
    value = abs(float(value))
    if value >= 10000:
        return 1
    if value >= 100:
        return 2
    if value >= 1:
        return 4
    if value >= 0.01:
        return 6
    return 8


def _tick_precision_decimals(tick_value: object) -> int | None:
    """Return exact display decimals implied by an exchange tick string/value."""
    text = str(tick_value or "").strip().lower()
    if not text:
        return None
    try:
        if "e" in text:
            mantissa, exponent_text = text.split("e", 1)
            exponent = int(exponent_text)
        else:
            mantissa, exponent = text, 0
        fraction = mantissa.partition(".")[2].rstrip("0")
        return max(0, min(16, len(fraction) - exponent))
    except (TypeError, ValueError):
        return None


def _format_price_with_tick_precision(value: float, tick_value: object = None) -> str:
    """Format price like the native axis, never with less precision than its tick."""
    value = float(value)
    fallback = _fallback_price_decimals(value)
    tick_decimals = _tick_precision_decimals(tick_value)
    decimals = fallback if tick_decimals is None else max(fallback, tick_decimals)
    if tick_decimals is None or decimals == fallback:
        return format_price(value)
    grouped = abs(value) >= 10000
    return f"{value:,.{decimals}f}" if grouped else f"{value:.{decimals}f}"


_PRICE_AXIS_TICK_DENSITY = 2.5
_PRICE_AXIS_NICE_STEPS = (1.0, 2.0, 2.5, 5.0, 10.0)


def _positive_price_tick(tick_value: object) -> float:
    try:
        tick = abs(float(str(tick_value or "").strip()))
    except (TypeError, ValueError):
        return 0.0
    return tick if tick > 0.0 and math.isfinite(tick) else 0.0


def _nice_price_step(required: float, minimum_move: float) -> float:
    """Smallest 1/2/2.5/5×10ⁿ interval that is valid on the market tick grid."""
    minimum_move = max(0.0, float(minimum_move))
    required = max(abs(float(required)), minimum_move, 1e-15)
    exponent = math.floor(math.log10(required))
    for power in range(exponent - 1, exponent + 4):
        scale = 10.0 ** power
        for mantissa in _PRICE_AXIS_NICE_STEPS:
            candidate = mantissa * scale
            if minimum_move > 0.0:
                units = max(1, math.ceil(candidate / minimum_move - 1e-12))
                candidate = units * minimum_move
            if candidate >= required * (1.0 - 1e-12):
                return candidate
    return required


def _log_price_axis_ticks(
    minimum_log: float,
    maximum_log: float,
    axis_pixels: float,
    mark_pixels: float,
    minimum_move: float,
) -> list[float]:
    """Return meaningful raw-price levels mapped to log10 chart coordinates.

    The chart stores logarithmic prices as log10(price).  Letting pyqtgraph tick
    that coordinate directly produces arbitrary labels such as 0.354813.  A
    trading price scale should choose round levels in real price space first,
    then transform those selected prices to their logarithmic screen positions.
    """
    low_log, high_log = sorted((float(minimum_log), float(maximum_log)))
    if not all(math.isfinite(value) for value in (low_log, high_log)) or high_log <= low_log:
        return []

    low_price = raw_price(low_log, True)
    high_price = raw_price(high_log, True)
    if (
        not all(math.isfinite(value) for value in (low_price, high_price))
        or low_price <= 0.0
        or high_price <= low_price
    ):
        return []

    axis_pixels = max(1.0, abs(float(axis_pixels)))
    mark_pixels = max(1.0, abs(float(mark_pixels)))
    minimum_move = max(0.0, float(minimum_move))
    log_span = high_log - low_log


    if high_price / low_price <= 10.0:
        required = (high_price - low_price) * mark_pixels / axis_pixels
        step = _nice_price_step(required, minimum_move)
        for _ in range(16):
            lower = high_price - step
            if lower <= 0.0:
                break
            top_gap = (
                math.log10(high_price / lower) * axis_pixels / log_span
            )
            if top_gap >= mark_pixels:
                break
            step = _nice_price_step(step * (1.0 + 1e-9), minimum_move)

        epsilon = step * 1e-10
        first = math.floor((high_price + epsilon) / step)
        last = math.ceil((low_price - epsilon) / step)
        if first < last or first - last > 512:
            return []
        return [
            chart_y(index * step, True)
            for index in range(first, last - 1, -1)
            if low_price - epsilon <= index * step <= high_price + epsilon
            and index * step > 0.0
        ]


    candidates: set[float] = set()
    minimum_power = math.floor(math.log10(low_price)) - 1
    maximum_power = math.ceil(math.log10(high_price)) + 1
    for power in range(minimum_power, maximum_power + 1):
        scale = 10.0 ** power
        for mantissa in (1.0, 2.0, 2.5, 5.0):
            price = mantissa * scale
            if minimum_move > 0.0:
                units = round(price / minimum_move)
                if units <= 0:
                    continue
                price = units * minimum_move
            if low_price <= price <= high_price and price > 0.0:
                candidates.add(price)

    ticks: list[float] = []
    previous_pixel: float | None = None
    for price in sorted(candidates, reverse=True):
        shown = chart_y(price, True)
        pixel = (high_log - shown) * axis_pixels / log_span
        if previous_pixel is None or abs(pixel - previous_pixel) >= mark_pixels:
            ticks.append(shown)
            previous_pixel = pixel
    return ticks


class PriceAxisItem(pg.AxisItem):
    manual_scale_requested = Signal()

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.logarithmic = False
        self._tick_precision_value: object = None
        self._drag_range: tuple[float, float] | None = None
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def set_logarithmic(self, enabled: bool) -> None:
        self.logarithmic = enabled
        self.picture = None
        self.update()

    def setRange(self, mn: float, mx: float) -> None:
        # A horizontal pane resize does not change this axis's ticks. Its own
        # resize/style handlers still invalidate the picture when needed.
        if getattr(self, "range", None) == [mn, mx] and not self.grid:
            return
        super().setRange(mn, mx)

    def set_tick_precision(self, tick_value: object) -> None:
        self._tick_precision_value = tick_value
        self.picture = None
        self.update()

    def tickValues(
        self, minVal: float, maxVal: float, size: float
    ) -> list[tuple[float | None, list[float]]]:


        if not self.logarithmic:
            return super().tickValues(minVal, maxVal, size)

        font = self.style.get("tickFont")
        if font is None:
            application = QtWidgets.QApplication.instance()
            font = application.font() if application is not None else QtGui.QFont()
        mark_pixels = max(
            1.0,
            math.ceil(QtGui.QFontMetricsF(font).height() * _PRICE_AXIS_TICK_DENSITY),
        )
        values = _log_price_axis_ticks(
            minVal,
            maxVal,
            size,
            mark_pixels,
            _positive_price_tick(self._tick_precision_value),
        )
        return [(None, values)] if values else super().tickValues(minVal, maxVal, size)

    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        return [
            _format_price_with_tick_precision(
                raw_price(value * scale, self.logarithmic),
                self._tick_precision_value,
            )
            for value in values
        ]

    def mouseDragEvent(self, event: Any) -> None:
        """Scale the chart vertically when the user drags the right price axis."""
        view = self.linkedView()
        if view is None or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if event.isStart():
            current = view.viewRange()[1]
            self._drag_range = (float(current[0]), float(current[1]))
            self.manual_scale_requested.emit()
        if self._drag_range is None:
            event.ignore()
            return
        delta = event.pos().y() - event.buttonDownPos().y()
        factor = math.exp(max(-3.0, min(3.0, delta / 190.0)))
        low, high = self._drag_range
        center = (low + high) * 0.5
        half_span = max((high - low) * factor * 0.5, 1e-12)
        view.setYRange(center - half_span, center + half_span, padding=0)
        event.accept()
        if event.isFinish():
            self._drag_range = None


class ScalableStudyAxisItem(pg.AxisItem):
    """Right-side study axis with TradingView-style vertical drag scaling."""

    manual_scale_requested = Signal()
    reset_scale_requested = Signal()

    def setRange(self, mn: float, mx: float) -> None:
        if getattr(self, "range", None) == [mn, mx] and not self.grid:
            return
        super().setRange(mn, mx)

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._drag_range: tuple[float, float] | None = None
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Drag vertically to scale this pane · double-click to reset auto scale")

    def mouseDragEvent(self, event: Any) -> None:
        view = self.linkedView()
        if view is None or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if event.isStart():
            current = view.viewRange()[1]
            self._drag_range = (float(current[0]), float(current[1]))
            self.manual_scale_requested.emit()
        if self._drag_range is None:
            event.ignore()
            return
        delta = float(event.pos().y() - event.buttonDownPos().y())
        factor = math.exp(max(-3.0, min(3.0, delta / 170.0)))
        low, high = self._drag_range
        center = (low + high) * 0.5
        half_span = max((high - low) * factor * 0.5, 1e-12)
        view.setYRange(center - half_span, center + half_span, padding=0)
        event.accept()
        if event.isFinish():
            self._drag_range = None

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_range = None
            self.reset_scale_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class HumanAxisItem(ScalableStudyAxisItem):
    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        return [human_number(value * scale) for value in values]


class PercentAxisItem(ScalableStudyAxisItem):
    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        return [
            f"{value * scale:.4f}%" if abs(value * scale) < 0.1 else f"{value * scale:.2f}%"
            for value in values
        ]


class StudyViewBox(pg.ViewBox):
    """Lower-study ViewBox with direct vertical panning and linked time navigation."""

    settings_requested = Signal()


    def wheelEvent(self, event: Any, axis: int | None = None) -> None:


        super().wheelEvent(event, axis=0 if axis is None else axis)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.settings_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class RsiAxisItem(ScalableStudyAxisItem):
    def tickStrings(self, values, scale, spacing):
        return [f"{value:g}" for value in values]


class StudyPaneHeader(QtWidgets.QGraphicsWidget):
    """TradingView-style in-pane legend with a thin draggable pane separator."""

    settings_requested = Signal()
    remove_requested = Signal()
    auto_scale_requested = Signal()
    resize_started = Signal()
    resize_requested = Signal(float)
    resize_reset = Signal()
    HANDLE_HEIGHT = 6.0
    BUTTON_WIDTH = 24.0
    SIDE_PADDING = 8.0

    def __init__(self, name, theme, parent=None):
        super().__init__(parent)
        self.name = name
        self.caption = name.upper()
        self.theme = theme
        self._hovered = ""
        self._pressed = ""
        self._drag_y = None
        self._overlay_height = 26.0
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setFont(typography_font(TextRole.UI_CAPTION))
        self.setZValue(80)
        self.sync_geometry()

    def setFont(self, font):
        super().setFont(font)
        self._overlay_height = max(
            24.0,
            float(math.ceil(QtGui.QFontMetricsF(font).height()) + 10),
        )
        self.sync_geometry()
        self.update()

    def sync_geometry(self) -> None:
        parent = self.parentWidget() or self.parentItem()
        if parent is None:
            return
        rect = parent.boundingRect()
        self.setGeometry(
            QtCore.QRectF(
                float(rect.left()),
                float(rect.top()),
                max(0.0, float(rect.width())),
                self._overlay_height,
            )
        )

    def set_caption(self, caption):
        if caption != self.caption:
            self.caption = caption
            self.update()

    def _title_rect(self) -> QtCore.QRectF:
        rect = self.rect()
        available = max(
            0.0,
            rect.width() - self.SIDE_PADDING * 2 - self.BUTTON_WIDTH * 3,
        )
        preferred = QtGui.QFontMetricsF(self.font()).horizontalAdvance(self.caption) + 4.0
        width = min(available, preferred)
        return QtCore.QRectF(
            self.SIDE_PADDING,
            self.HANDLE_HEIGHT,
            width,
            max(0.0, rect.height() - self.HANDLE_HEIGHT),
        )

    def button_rect(self, action):
        index = ("auto", "settings", "remove").index(action)
        title = self._title_rect()
        return QtCore.QRectF(
            title.right() + 2.0 + index * self.BUTTON_WIDTH,
            self.HANDLE_HEIGHT,
            self.BUTTON_WIDTH,
            max(0.0, self.rect().height() - self.HANDLE_HEIGHT),
        )

    def _legend_rect(self) -> QtCore.QRectF:
        title = self._title_rect()
        remove = self.button_rect("remove")
        return QtCore.QRectF(
            self.SIDE_PADDING,
            self.HANDLE_HEIGHT,
            max(0.0, remove.right() - self.SIDE_PADDING),
            max(0.0, self.rect().height() - self.HANDLE_HEIGHT),
        )

    def shape(self):
        path = QtGui.QPainterPath()
        rect = self.rect()
        path.addRect(QtCore.QRectF(0.0, 0.0, rect.width(), self.HANDLE_HEIGHT))
        path.addRect(self._legend_rect())
        return path

    def _hit(self, point):
        if point.y() < self.HANDLE_HEIGHT:
            return "resize"
        for action in ("auto", "settings", "remove"):
            if self.button_rect(action).contains(point):
                return action
        if self._title_rect().contains(point):
            return "title"
        return ""

    def paint(self, painter, option, widget=None):
        rect = self.rect()
        divider = (
            self.theme["text"]
            if self._hovered == "resize" or self._drag_y is not None
            else self.theme["muted"]
        )
        painter.setPen(pg.mkPen(divider, width=1, cosmetic=True))
        painter.drawLine(QtCore.QLineF(0.0, 0.5, rect.width(), 0.5))

        title_rect = self._title_rect()
        painter.setFont(self.font())
        painter.setPen(QtGui.QColor(self.theme["text"]))
        text = QtGui.QFontMetricsF(self.font()).elidedText(
            self.caption,
            Qt.TextElideMode.ElideRight,
            title_rect.width(),
        )
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )

        show_controls = bool(
            self.hasFocus()
            or (self._hovered and self._hovered != "resize")
        )
        if show_controls:
            for action in ("auto", "settings", "remove"):
                button = self.button_rect(action)
                if self._hovered == action:
                    painter.fillRect(
                        button.adjusted(2.0, 2.0, -2.0, -2.0),
                        QtGui.QColor(self.theme["control_hover"]),
                    )
                painter.setPen(
                    pg.mkPen(
                        self.theme["text"]
                        if self._hovered == action
                        else self.theme["muted"],
                        width=1,
                        cosmetic=True,
                    )
                )
                x, y = button.center().x(), button.center().y()
                if action == "auto":
                    painter.drawText(
                        button,
                        Qt.AlignmentFlag.AlignCenter,
                        "A",
                    )
                elif action == "remove":
                    painter.drawLine(
                        QtCore.QLineF(x - 4.0, y - 4.0, x + 4.0, y + 4.0)
                    )
                    painter.drawLine(
                        QtCore.QLineF(x - 4.0, y + 4.0, x + 4.0, y - 4.0)
                    )
                else:
                    for offset, knob in ((-4.0, -2.0), (0.0, 3.0), (4.0, -1.0)):
                        painter.drawLine(
                            QtCore.QLineF(
                                x - 6.0,
                                y + offset,
                                x + 6.0,
                                y + offset,
                            )
                        )
                        painter.drawLine(
                            QtCore.QLineF(
                                x + knob,
                                y + offset - 2.0,
                                x + knob,
                                y + offset + 2.0,
                            )
                        )

        if self.hasFocus():
            painter.setPen(
                pg.mkPen(
                    self.theme["text"],
                    width=1,
                    style=Qt.PenStyle.DotLine,
                )
            )
            painter.drawRect(self._legend_rect().adjusted(0.0, 1.0, 0.0, -1.0))

    def hoverMoveEvent(self, event):
        hovered = self._hit(event.pos())
        if hovered != self._hovered:
            self._hovered = hovered
            self.setCursor(
                Qt.CursorShape.SplitVCursor
                if hovered == "resize"
                else Qt.CursorShape.PointingHandCursor
            )
            tooltips = {
                "resize": "Drag separator to resize · double-click to reset height",
                "auto": "Reset automatic scale",
                "settings": f"{self.name} settings",
                "remove": f"Remove {self.name}",
                "title": (
                    f"{self.name} settings · Enter to open · Delete to remove"
                ),
            }
            self.setToolTip(tooltips.get(hovered, ""))
            self.update()
        event.accept()

    def hoverLeaveEvent(self, event):
        self._hovered = ""
        self.unsetCursor()
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._pressed = self._hit(event.pos())
        if not self._pressed:
            event.ignore()
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._pressed == "resize":
            self._drag_y = float(event.screenPos().y())
            self.resize_started.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_y is not None:
            self.resize_requested.emit(float(event.screenPos().y()) - self._drag_y)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pressed, self._pressed = self._pressed, ""
        dragging, self._drag_y = self._drag_y is not None, None
        if not dragging and pressed and pressed == self._hit(event.pos()):
            if pressed in {"settings", "title"}:
                self.settings_requested.emit()
            elif pressed == "auto":
                self.auto_scale_requested.emit()
            elif pressed == "remove":
                self.remove_requested.emit()
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self._hit(event.pos()) == "resize":
            self._drag_y = None
            self._pressed = ""
            self.resize_reset.emit()
        elif self._hit(event.pos()) in {"title", "settings"}:
            self._pressed = ""
            self.settings_requested.emit()
        else:
            event.ignore()
            return
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.settings_requested.emit()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_requested.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

class ProfileItem(NativeAreaItem):
    def __init__(self, background: str):
        super().__init__("volume_profile")
        self.background = background
        self.poc: float | None = None
        self._poc_line = None
        self._poc_color = QtGui.QColor()
        self._profile_key: tuple[Any, ...] | None = None
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_x_range(self, x_range) -> None:
        """Move cached profile bars with the viewport, independently of binning."""
        x0, x1 = x_range
        span = max(x1 - x0, 1.0)
        self.setTransform(QtGui.QTransform(span, 0, 0, 1, x1-span*0.008, 0))

    @staticmethod
    def _profile_fingerprint(values: np.ndarray) -> tuple[int, int]:
        array = np.ascontiguousarray(values, dtype=np.float64)
        return (len(array), hash(array.tobytes()))

    def set_profile(
        self,
        centers: np.ndarray,
        volumes: np.ndarray,
        x_range: tuple[float, float],
        color: str,
        maximum_width: float,
        opacity: int = 68,
        logarithmic: bool = False,
    ) -> None:


        self.set_x_range(x_range)
        centers = np.ascontiguousarray(centers, dtype=np.float64)
        volumes = np.ascontiguousarray(volumes, dtype=np.float64)
        profile_key = (
            self._profile_fingerprint(centers),
            self._profile_fingerprint(volumes),
            self.background,
            color,
            round(float(maximum_width), 8),
            int(opacity),
            bool(logarithmic),
        )
        if profile_key == self._profile_key:
            return
        self._profile_key = profile_key

        self.poc = None
        self._poc_line = None
        if len(centers) == 0 or len(volumes) == 0 or not np.any(volumes > 0):
            self._set_geometry(np.empty((0, 6)), QtCore.QRectF(), (0.0, 0.0))
            return

        width = maximum_width
        anchor = 0.0
        shown_centers = chart_y_array(centers, logarithmic)
        bin_height = (
            float(np.median(np.diff(shown_centers)))
            if len(shown_centers) > 1
            else max(abs(shown_centers[0]) * 0.001, 1e-8)
        )
        scale = float(volumes.max())
        brush_color = opaque_overlay_color(self.background, color, opacity)
        outline = QtGui.QColor(color)
        outline.setAlpha(min(180, opacity + 70))
        bar_widths = width * volumes / max(scale, 1e-12)
        rectangles = np.column_stack((anchor - bar_widths, shown_centers - bin_height * 0.46,
                                      bar_widths, np.full(len(centers), bin_height * 0.92)))
        self.poc = float(centers[int(np.argmax(volumes))])
        poc_y = chart_y(self.poc, logarithmic)
        self._poc_line = QtCore.QLineF(anchor - width, poc_y, anchor, poc_y)
        self._poc_color = QtGui.QColor(color)
        bounds = QtCore.QRectF(
            anchor - width,
            float(shown_centers.min() - bin_height),
            width,
            float(shown_centers.max() - shown_centers.min() + bin_height * 2),
        )
        self.set_rectangles(rectangles, bounds, brush_color, outline)

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionGraphicsItem,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        super().paint(painter, option, widget)
        if self._poc_line is not None:
            pen = QtGui.QPen(self._poc_color)
            pen.setWidthF(1.4)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(self._poc_line)


def _prepare_price_extrema(closed, seconds):
    return RangeExtrema(closed[:, 3], closed[:, 2]), closed[:, 0], CandleLodIndex(closed, seconds)


class _IndicatorAnalysisState:
    """Identity for the persistent cache in the isolated analysis process."""


class _IndicatorAnalysisWorkerSignals(QtCore.QObject):
    finished = Signal(object, object, object)


class _IndicatorAnalysisWorker(QtCore.QRunnable):
    """Compute studies and display-size their results entirely off the GUI thread."""

    def __init__(
        self,
        state: _IndicatorAnalysisState,
        key: tuple[Any, ...],
        source_update: tuple[Any, ...],
        transport_epoch: int,
        requests: tuple[tuple[str, dict[str, Any]], ...],
        first: int,
        last: int,
        pixel_width: float,
        logarithmic: bool,
    ) -> None:
        super().__init__()
        self.state = state
        self.key = key
        self.source_update = source_update
        self.transport_epoch = int(transport_epoch)
        self.requests = requests
        self.first = max(0, int(first))
        self.last = max(self.first, int(last))
        self.pixel_width = float(pixel_width)
        self.logarithmic = bool(logarithmic)
        self.signals = _IndicatorAnalysisWorkerSignals()

    @QtCore.Slot()
    def run(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        prepared: dict[str, Any] = {}
        error = None
        try:
            mode = str(self.source_update[0])
            generation = self.source_update[-1]
            if mode == "full":
                closed, live = self.source_update[1], self.source_update[2]
                matrix = np.concatenate((closed, live))
                process_update = ("full", matrix)
                transport_bytes = int(matrix.nbytes)
            else:
                tail = self.source_update[3]
                process_update = (
                    "delta",
                    int(self.source_update[1]),
                    self.source_update[2],
                    tail,
                )
                transport_bytes = int(getattr(tail, "nbytes", 0))
            if profile_started:
                record_performance_count(f"analysis.indicator_transport.{mode}")
                record_performance_sum(
                    "analysis.indicator_transport_bytes", transport_bytes
                )
            prepared = run_analysis(
                indicator_analysis,
                id(self.state),
                generation,
                process_update,
                self.requests,
                self.first,
                self.last,
                self.pixel_width,
                self.logarithmic,
                cache_affinity=True,
            )
            if isinstance(prepared, dict):
                prepared["__transport_epoch__"] = self.transport_epoch
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            error = repr(exc)
            prepared = {}
        if profile_started:
            record_performance_timing(
                "analysis.indicator_compute_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )
        self.signals.finished.emit(self.key, prepared, error)


class _ProfileAnalysisWorkerSignals(QtCore.QObject):
    finished = Signal(object, object, object, object, object, object)


class _ProfileAnalysisWorker(QtCore.QRunnable):
    """Compute exact profile bins and alert levels away from GUI presentation."""

    def __init__(
        self,
        kind: str,
        key: tuple[Any, ...],
        candles: list[Candle],
        bins: int,
        levels: int,
    ) -> None:
        super().__init__()
        self.kind = str(kind)
        self.key = key
        self.candles = candles
        self.bins = int(bins)
        self.levels = int(levels)
        self.signals = _ProfileAnalysisWorkerSignals()

    @QtCore.Slot()
    def run(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        try:
            if isinstance(self.candles, tuple) and len(self.candles) == 3 and isinstance(self.candles[0], np.ndarray):
                matrix = np.concatenate(self.candles[:2])
            else:
                matrix = _candle_matrix_from_objects(self.candles)
            centers, volumes, levels = run_analysis(profile_analysis, matrix, self.bins, self.levels)
            error = None
        except Exception as exc:  # pragma: no cover
            centers = np.array([], dtype=float)
            volumes = np.array([], dtype=float)
            levels = []
            error = repr(exc)
        if profile_started:
            record_performance_timing(
                f"analysis.profile.{self.kind}_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )
        self.signals.finished.emit(self.kind, self.key, centers, volumes, levels, error)


class _MajorLevelsWorkerSignals(QtCore.QObject):
    finished = Signal(object, object, object)


class _MajorLevelsWorker(QtCore.QRunnable):
    def __init__(
        self,
        key: tuple[Any, ...],
        frames: tuple[tuple[str, tuple[Candle, ...]], ...],
        price: float,
        minimum_score: float,
        maximum_levels: int,
    ) -> None:
        super().__init__()
        self.key = key
        self.frames = frames
        self.price = float(price)
        self.minimum_score = float(minimum_score)
        self.maximum_levels = int(maximum_levels)
        self.signals = _MajorLevelsWorkerSignals()

    @QtCore.Slot()
    def run(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        try:
            zones = run_analysis(major_level_analysis, self.frames, self.price, self.minimum_score, self.maximum_levels)
            error = None
        except Exception as exc:  # pragma: no cover
            zones = []
            error = repr(exc)
        if profile_started:
            record_performance_timing(
                "analysis.major_levels_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )
        self.signals.finished.emit(self.key, zones, error)


class _AutoFibWorkerSignals(QtCore.QObject):
    finished = Signal(object, object, object)


class _AutoFibWorker(QtCore.QRunnable):
    def __init__(
        self,
        key: tuple[Any, ...],
        frames: tuple[tuple[str, tuple[Candle, ...]], ...],
        current_interval: str,
        current_candles: tuple[Candle, ...],
        current_price: float,
        zones: tuple[Zone, ...],
        maximum: int,
    ) -> None:
        super().__init__()
        self.key = key
        self.frames = frames
        self.current_interval = str(current_interval)
        self.current_candles = current_candles
        self.current_price = float(current_price)
        self.zones = zones
        self.maximum = int(maximum)
        self.signals = _AutoFibWorkerSignals()

    @QtCore.Slot()
    def run(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        try:
            candidates = run_analysis(fibonacci_analysis, self.frames, self.current_interval,
                                      self.current_candles, self.current_price, self.zones, self.maximum)
            error = None
        except Exception as exc:  # pragma: no cover
            candidates = []
            error = repr(exc)
        if profile_started:
            record_performance_timing(
                "analysis.auto_fib_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )
        self.signals.finished.emit(self.key, candidates, error)


class _HistoryPrepareWorkerSignals(QtCore.QObject):
    finished = Signal(object, object, object, object, object, object, object)


class _HistoryPrepareWorker(QtCore.QRunnable):
    """Prepend new history while sharing published pages and copying numeric rows.

    Closed matrix rows are immutable and the live row is captured by the caller.
    Never walk all retained Candle objects to reconstruct an already-built array.
    """

    def __init__(
        self,
        key: tuple[Any, ...],
        incoming: tuple[Candle, ...],
        current: CandlePages,
        exhausted: bool,
        current_times: CandlePages,
        current_matrix: tuple[np.ndarray, np.ndarray],
    ) -> None:
        super().__init__()
        self.key = key
        self.incoming = incoming
        self.current = current
        self.exhausted = bool(exhausted)
        self.current_times = current_times
        self.current_matrix = current_matrix
        self.signals = _HistoryPrepareWorkerSignals()

    @QtCore.Slot()
    def run(self) -> None:
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        try:
            cutoff = self.current[0].time if self.current else float("inf")
            page = sorted(
                (candle for candle in self.incoming if candle.time < cutoff),
                key=lambda candle: candle.time,
            )
            compact: list[Candle] = []
            previous_time: float | None = None
            for candle in page:
                if candle.time == previous_time:
                    compact[-1] = candle
                else:
                    compact.append(candle)
                    previous_time = candle.time
            added = min(len(compact), max(0, MAX_CHART_CANDLES - len(self.current)))
            retained = compact[-added:] if added else []
            combined = self.current.prepended(retained)
            times = self.current_times.prepended(candle.time for candle in retained)
            matrix_size = len(combined)
            storage = np.empty((candle_matrix_capacity(matrix_size), 7), dtype=np.float64)
            if added:
                storage[:added] = _candle_matrix_from_objects(retained)
            closed, live = self.current_matrix
            if len(closed) + len(live) != len(self.current):
                raise ValueError("History pages and captured matrix must have equal length")
            storage[added:added+len(closed)] = closed
            storage[added+len(closed):matrix_size] = live
            extrema = RangeExtrema(storage[:max(0, matrix_size-1), 3], storage[:max(0, matrix_size-1), 2])
            lod_index = CandleLodIndex(storage[:max(0, matrix_size-1)], INTERVAL_SECONDS[self.key[1]])
            matrix = (storage, matrix_size, extrema, lod_index)
            error = None
        except Exception as exc:  # pragma: no cover
            added = 0
            combined = []
            matrix = (np.empty((2, 7), dtype=np.float64), 0)
            times = []
            error = repr(exc)
        if profile_started:
            record_performance_timing(
                "analysis.history_prepare_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )
        self.signals.finished.emit(
            self.key, combined, matrix, times, added,
            self.exhausted or (error is None and len(combined) >= MAX_CHART_CANDLES), error
        )


def _chart_axis_font() -> QtGui.QFont:
    """Return the single semantic font used by every chart time/price axis.

    Keep the font in point-size space and let the typography controller own
    family, weight, hinting and fixed-pitch behavior.  Converting the role to a
    derived pixel size made the bottom DateAxis and right-side axes render
    differently across DPI/platform configurations.
    """
    return typography_font(TextRole.CHART_AXIS)


class TimeAxisItem(pg.DateAxisItem):
    """Keep calendar ticks and formatting bounded by pixels on every zoom level."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # Upstream's month/day context ticks have no autoSkip. A yearly view
        # consequently builds and formats every month, even when labels overlap.
        # Copy the shared specs so these changes cannot affect other date axes.
        self.zoomLevels = deepcopy(self.zoomLevels)
        for level in self.zoomLevels.values():
            for spec in level.tickSpecs:
                if spec.autoSkip is None:
                    spec.autoSkip = [1, 2, 3, 6, 12]
        self._time_tick_strings: OrderedDict[tuple, str] = OrderedDict()

    def tickValues(self, minVal: float, maxVal: float, size: float):
        if (
            not all(math.isfinite(value) for value in (minVal, maxVal, size))
            or maxVal <= minVal
            or size <= 0.0
        ):
            return []
        span = maxVal - minVal
        self.setZoomLevelForDensity(span / size)
        # At most 32 ticks per level (two levels); no loop over empty bar slots.
        budget = max(2, min(32, int(size / 70.0)))
        spacing = max(self.minSpacing, span / budget)
        return self.zoomLevel.tickValues(minVal, maxVal, minSpc=spacing)

    def tickStrings(self, values, scale, spacing):
        prefix = (id(self.zoomLevel), self.utcOffset, spacing)
        keys = [(*prefix, float(value)) for value in values]
        missing = [value for value, key in zip(values, keys) if key not in self._time_tick_strings]
        if missing:
            strings = super().tickStrings(missing, scale, spacing)
            self._time_tick_strings.update(
                ((*prefix, float(value)), label) for value, label in zip(missing, strings)
            )
        result = [self._time_tick_strings[key] for key in keys]
        for key in keys:
            self._time_tick_strings.move_to_end(key)
        while len(self._time_tick_strings) > 256:
            self._time_tick_strings.popitem(last=False)
        return result

    def setRange(self, mn: float, mx: float) -> None:
        if getattr(self, "range", None) == [mn, mx]:
            return
        super().setRange(mn, mx)


def _scaled_timeframe_x_range(
    x_range: tuple[float, float],
    old_interval_seconds: float,
    new_interval_seconds: float,
    *,
    old_latest_time: float | None,
    new_latest_time: float | None,
    new_earliest_time: float | None = None,
) -> tuple[float, float]:
    """Preserve density when history supports it; fit sparse HTF snapshots."""
    x0, x1 = float(x_range[0]), float(x_range[1])
    old_seconds = float(old_interval_seconds)
    new_seconds = float(new_interval_seconds)
    span = x1 - x0
    if (
        not all(math.isfinite(value) for value in (x0, x1, old_seconds, new_seconds))
        or span <= 0.0
        or old_seconds <= 0.0
        or new_seconds <= 0.0
    ):
        return x0, x1


    visible_bars = span / old_seconds
    new_span = visible_bars * new_seconds

    if (
        new_seconds > old_seconds
        and new_earliest_time is not None
        and new_latest_time is not None
        and math.isfinite(float(new_earliest_time))
        and math.isfinite(float(new_latest_time))
        and float(new_latest_time) >= float(new_earliest_time)
    ):
        first, last = float(new_earliest_time), float(new_latest_time)
        available_bars = (last - first) / new_seconds + 1.0
        if visible_bars > available_bars:
            right_pad = new_seconds * max(4.0, min(16.0, available_bars * 0.10))
            return first - new_seconds * 2.0, max(last + right_pad, first + new_seconds * 6.0)

    if (
        old_latest_time is not None
        and new_latest_time is not None
        and math.isfinite(float(old_latest_time))
        and math.isfinite(float(new_latest_time))
        and x0 <= float(old_latest_time) <= x1
    ):


        anchor_ratio = (float(old_latest_time) - x0) / span
        anchor_time = float(new_latest_time)
    else:


        anchor_ratio = 0.5
        anchor_time = (x0 + x1) * 0.5

    new_x0 = anchor_time - anchor_ratio * new_span
    return new_x0, new_x0 + new_span


class _InteractionGarbageCollection(QtCore.QObject):
    """Keep automatic cyclic collection out of active navigation transactions.

    Reference counting remains enabled. The original automatic-collection state
    is restored after navigation settles, including when a chart is destroyed.
    One application-owned guard coordinates all chart panes.
    """

    def __init__(self, application):
        super().__init__(application)
        self._active = set()
        self._restore_enabled = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._restore)
        application.aboutToQuit.connect(self._shutdown)

    def set_active(self, token, active):
        if active:
            self._active.add(token)
            self._timer.stop()
            if gc.isenabled():
                self._restore_enabled = True
                gc.disable()
        else:
            self._active.discard(token)
            if not self._active and self._restore_enabled:
                self._timer.start()

    def _restore(self):
        if self._active or not self._restore_enabled:
            return
        self._restore_enabled = False
        gc.enable()

    def _shutdown(self):
        self._timer.stop()
        self._active.clear()
        self._restore()


class ChartWorkspace(QtWidgets.QWidget):
    analysis_changed = Signal(object, object)
    drawing_mode_changed = Signal(object)
    order_rail_placement_changed = Signal(bool)
    order_rail_execution_requested = Signal(object)
    order_rail_cancel_requested = Signal(object)
    order_rail_leverage_requested = Signal(int)
    auto_scale_changed = Signal(bool)
    working_order_moved = Signal(object)
    indicator_settings_requested = Signal(str)
    indicator_visibility_changed = Signal(str, bool)
    history_requested = Signal(float)
    interaction_priority_changed = Signal(bool)
    interaction_started = Signal()
    render_surface_changed = Signal(object)

    snapshot_committed = Signal(object)
    history_merged = Signal(int)

    def __init__(
        self,
        theme: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
        *,
        use_opengl: bool = False,
        isolate_gl_composition: bool = False,
        opengl_full_viewport: bool = False,
        native_bar_renderer: bool = True,
        lod_aggregation: bool = True,
        magnetic_order_rail_enabled: bool = True,
        presentation_clock: PresentationClock | None = None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.use_opengl = bool(use_opengl)
        self._isolate_gl_composition = bool(isolate_gl_composition)
        self._requested_opengl = self.use_opengl
        self._opengl_runtime_failure_reason = ""
        self.opengl_full_viewport = bool(opengl_full_viewport)
        self.native_bar_renderer_enabled = bool(native_bar_renderer)
        self.lod_aggregation_enabled = bool(lod_aggregation)
        self.magnetic_order_rail_enabled = bool(magnetic_order_rail_enabled)
        self._presentation_clock = presentation_clock or PresentationClock(self, self)
        application = QtWidgets.QApplication.instance()
        guard = getattr(application, '_nightwatch_interaction_gc', None)
        if guard is None:
            guard = _InteractionGarbageCollection(application)
            application._nightwatch_interaction_gc = guard
        self._interaction_gc = guard
        self.destroyed.connect(lambda _obj=None, token=id(self), guard=guard: guard.set_active(token, False))
        self._resize_gc_token = (id(self), "resize")
        self.destroyed.connect(lambda _obj=None, token=self._resize_gc_token, guard=guard: guard.set_active(token, False))


        self._analysis_pool = QtCore.QThreadPool(self)
        self._analysis_pool.setMaxThreadCount(analysis_worker_count())


        self._render_pool = QtCore.QThreadPool(self)
        self._render_pool.setMaxThreadCount(1)
        self._preparation_pool = QtCore.QThreadPool(self)
        self._preparation_pool.setMaxThreadCount(2)
        self._market_generation = 0
        self._history_generation = 0
        self._snapshot_serial = 0
        self._snapshot_inflight = False
        self._snapshot_recovery_active = False
        self._matrix_waiting = False
        self._snapshot_live_updates = {}
        self._snapshot_mailbox = None
        self._bar_mailbox = None
        self._committed_closed_time = None
        self._committed_history_stride = 1
        self._committed_history_owns_tail = False
        self._bar_request_window = None
        self._bar_request_key = None
        self._lod_stride_hint = 1
        self._price_extrema = None
        self._candle_lod_index = None
        self._price_extrema_times = np.empty(0)
        self._snapshot_job = LatestJob(self._preparation_pool, self)
        self._snapshot_job.ready.connect(self._snapshot_prepared)
        self._snapshot_job.failed.connect(self._snapshot_failed)
        self._bar_job = LatestJob(self._render_pool, self)
        self._bar_job.ready.connect(self._bars_prepared)
        self._index_job = LatestJob(self._preparation_pool, self)
        self._index_job.ready.connect(self._extrema_prepared)
        self._history_input_serial = 0
        self._liquidation_live_revision = 0
        self._history_batches = {"funding": [], "liquidations": []}
        self._history_input_jobs = {kind: LatestJob(self._preparation_pool, self)
                                    for kind in self._history_batches}
        for job in self._history_input_jobs.values():
            job.ready.connect(self._event_history_prepared)
            job.failed.connect(self._event_history_failed)
        self._study_mailboxes = {}
        self._study_committed = {}
        self._study_extents = {}
        self._study_jobs = {kind: LatestJob(self._analysis_pool, self) for kind in ("oi", "funding")}
        for job in self._study_jobs.values():
            job.ready.connect(self._study_prepared)
        self._navigation_frame_requested = False
        self.interval = DEFAULT_INTERVAL
        self.candle_style = "Hollow"
        self.logarithmic = False
        self.auto_scale = True
        self.indicators_enabled = True
        self.current_price = 0.0
        self.current_rising = True
        self._current_price_text = ""
        self._chart_axis_metrics = QtGui.QFontMetricsF(_chart_axis_font())
        self.candles = CandlePages()
        self._candle_matrix_storage = np.empty((candle_matrix_capacity(0), 7), dtype=np.float64)
        self._candle_matrix_start = 0
        self._candle_matrix_size = 0
        self.candle_matrix = self._candle_matrix_storage[:0]
        self.candle_times = CandlePages()
        self._last_reserved_price_axis_text = ""
        self._live_detail_revision = -1
        self._live_detail_view_key: tuple[float, float] | None = None
        self._profile_detail_revision = -1
        self._profile_detail_view_key: tuple[float, float] | None = None
        self._restore_view = None
        self._timeframe_restore: dict[str, Any] | None = None
        self._last_kline_event = 0.0
        self._last_price_event = 0.0
        self._snapshot_loaded = False
        self.rendered_window: tuple[int, int, int] | None = None
        self._history_request_oldest: float | None = None
        self._history_exhausted = False
        self.session_candles: list[Candle] = []
        self.session_profile_cache = (np.array([], dtype=float), np.array([], dtype=float))
        self._session_profile_cache_key: tuple[Any, ...] | None = None
        self._visible_profile_cache_key: tuple[Any, ...] | None = None
        self._visible_profile_cache = (np.array([], dtype=float), np.array([], dtype=float))


        self._profile_async_keys: dict[str, tuple[Any, ...] | None] = {
            "visible": None,
            "session": None,
        }
        self._profile_requested_keys: dict[str, tuple[Any, ...] | None] = {
            "visible": None,
            "session": None,
        }
        self._profile_async_pending: dict[
            str, tuple[tuple[Any, ...], list[Candle], int, int] | None
        ] = {"visible": None, "session": None}
        self._profile_workers: dict[str, _ProfileAnalysisWorker | None] = {
            "visible": None,
            "session": None,
        }
        self._profile_mailbox: dict[
            str, tuple[tuple[Any, ...], np.ndarray, np.ndarray, list[float]]
        ] = {}
        self._indicator_analysis_state = _IndicatorAnalysisState()
        self._indicator_transport_reset_required = True
        self._indicator_transport_epoch = 0
        self._indicator_committed_key = None
        self._indicator_display_async_key: tuple[Any, ...] | None = None
        self._indicator_display_requested_key: tuple[Any, ...] | None = None
        self._indicator_display_async_pending: tuple[
            tuple[Any, ...],
            tuple[tuple[str, dict[str, Any]], ...],
            int,
            int,
            float,
            bool,
        ] | None = None
        self._indicator_display_worker: _IndicatorAnalysisWorker | None = None
        self._indicator_display_mailbox: tuple[
            tuple[Any, ...], dict[str, dict[str, Any]]
        ] | None = None
        self._history_indicator_generation = 0
        self._history_prepare_async_key: tuple[Any, ...] | None = None
        self._history_prepare_requested: tuple[
            tuple[Candle, ...], bool
        ] | None = None
        self._history_prepare_pending: tuple[tuple[Candle, ...], bool] | None = None
        self._history_prepare_worker: _HistoryPrepareWorker | None = None
        self._history_prepared_mailbox: tuple[
            tuple[Any, ...], list[Candle], np.ndarray, int, list[float], int, bool
        ] | None = None
        self._major_levels_dirty = True
        self._major_levels_async_key: tuple[Any, ...] | None = None
        self._major_levels_requested_key: tuple[Any, ...] | None = None
        self._major_levels_async_pending: tuple[
            tuple[Any, ...], tuple[tuple[str, tuple[Candle, ...]], ...], float, float, int
        ] | None = None
        self._major_levels_worker: _MajorLevelsWorker | None = None
        self._major_levels_mailbox: tuple[tuple[Any, ...], list[Zone]] | None = None
        self.multi_frames: dict[str, list[Candle]] = {}
        self.zones: list[Zone] = []
        self.oi_history: list[dict[str, Any]] = []
        self.liquidations: deque[dict[str, Any]] = deque(maxlen=2000)
        self.funding_history: list[dict[str, Any]] = []
        self.last_live_profile = 0.0
        self.visible_levels: list[float] = []
        self.session_levels: list[float] = []
        self.zone_graphics: list[tuple[pg.LinearRegionItem, pg.TextItem, Zone]] = []
        self._zone_render_signature: tuple[Any, ...] | None = None
        self.drawing_mode: str | None = None
        self.drawing_start: QtCore.QPointF | None = None
        self.drawing_preview_active = False
        self.drawings_visible = True
        self.selected_drawing: tuple[str, int] | None = None
        self.ruler_definition: dict[str, float] | None = None
        self.fibonacci_definition: dict[str, float] | None = None
        self.ruler_graphics: list[Any] = []
        self.fibonacci_graphics: list[Any] = []
        self.auto_fib_graphics: list[Any] = []
        self.auto_fib_level_graphics: list[tuple[pg.PlotDataItem, pg.TextItem, float]] = []
        self.auto_fib_candidates: list[AutoFibCandidate] = []
        self.auto_fib_index = -1
        self.auto_fib_enabled = False
        self.auto_fib_dirty = True
        self._auto_fib_async_key: tuple[Any, ...] | None = None
        self._auto_fib_requested_key: tuple[Any, ...] | None = None
        self._auto_fib_async_pending: tuple[Any, ...] | None = None
        self._auto_fib_worker: _AutoFibWorker | None = None
        self._auto_fib_mailbox: tuple[tuple[Any, ...], list[AutoFibCandidate]] | None = None
        self._auto_fib_pending_cycle_step: int | None = None
        self.horizontal_graphics: list[tuple[Any, Any]] = []
        self.order_rail_placement_mode = False
        self.order_rail_market_symbol = ""
        self._active_order_rail_symbol = ""


        self.order_rail_value: float | None = None
        self.order_rail_tick_size = 0.0
        self.order_rail_tick_text = ""
        self.order_rail_min_price = 0.0
        self.order_rail_max_price = float("inf")
        self.order_rail_hud: MagneticOrderRailPanel | None = None
        self.order_rail_visual: MagneticRailLineOverlay | None = None


        self._parked_order_rails: list[dict[str, Any]] = []
        self._parked_order_rail_revision = 0
        self._parked_order_rail_cache_revision = -1
        self._parked_order_rail_cache_symbol = ""
        self._parked_order_rail_active_cache: tuple[dict[str, Any], ...] = ()
        self._parked_order_rail_serial = 0
        self._order_rail_draft_serial = 0
        self._active_order_rail_draft_id = 0
        self._active_order_rail_symbol = ""
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order: dict[str, Any] = {}
        self._active_order_rail_matched_once = False


        self._active_order_rail_amend_origin: float | None = None
        self._active_order_rail_amend_price: float | None = None
        self.price_axis_focus_overlay: PriceAxisFocusOverlay | None = None
        self._last_price_axis_text_band: tuple[float, float] | None = None
        self._current_price_badge_color = str(theme.get("cyan", "#65b7d5"))
        self._current_price_badge_tooltip = ""
        self.order_rail_config = normalized_order_rail_config()
        self.order_rail_order_preset_name = "Limit Entry"
        self.order_rail_order_preset = normalized_order_rail_order_preset(ORDER_RAIL_ORDER_PRESET_DEFAULTS["Limit Entry"])
        self._order_rail_dragging = False
        self._order_rail_drag_start_value: float | None = None
        self._order_rail_pending_drag_position: QtCore.QPointF | None = None
        self._order_rail_phase = 0.0
        self._order_rail_last_frame = time.monotonic()


        self._interaction_priority_active = False
        self._interaction_render_active = False
        self._overlay_frame_geometry = None
        self._price_axis_measurement_metrics = None
        self._price_axis_text_widths = OrderedDict()
        self._interaction_priority_release_timer = QTimer(self)
        self._interaction_priority_release_timer.setSingleShot(True)
        self._interaction_priority_release_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._interaction_priority_release_timer.setInterval(180)
        self._interaction_priority_release_timer.timeout.connect(
            self._release_interaction_priority
        )
        self.working_order_lines: dict[str, pg.InfiniteLine] = {}
        self._working_order_line_signatures: dict[str, tuple[Any, ...]] = {}
        self.working_order_payloads: list[dict[str, Any]] = []
        self.working_orders_visible = True
        self.indicators = {name: False for name in INDICATOR_KEYS}
        self.indicator_settings = {
            name: dict(values) for name, values in INDICATOR_SETTING_DEFAULTS.items()
        }
        self.rsi_period = int(self.indicator_settings["RSI"]["period"])
        self.rsi_upper = float(self.indicator_settings["RSI"]["upper"])
        self.rsi_lower = float(self.indicator_settings["RSI"]["lower"])
        self.overview_restore: tuple[tuple[float, float], tuple[float, float]] | None = None
        self.study_heights = {
            "Open Interest": 125.0,
            "ATR": 115.0,
            "Funding Rate History": 125.0,
            "RSI": 125.0,
        }
        self._default_study_heights = dict(self.study_heights)
        self._study_layout_signature = None
        self._study_resize_start = 0.0
        self.study_manual_scale = {
            "Open Interest": False,
            "ATR": False,
            "Funding Rate History": False,
            "RSI": False,
        }


        self.volume_bar_height_percent = 30
        self.subplot_visibility = {1: True, 2: True, 3: True, 4: True}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)


        pg.setConfigOption("mouseRateLimit", 0)
        pg.setConfigOption("useOpenGL", self.use_opengl)
        self.graphics = ChartGraphicsView()
        self.graphics.presentation_clock = self._presentation_clock
        self._presentation_clock.set_frame_presenter(self.graphics.present)
        self._pending_zoom_range = None


        self.graphics.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.graphics.setContentsMargins(0, 0, 0, 0)
        self.graphics.setViewportMargins(0, 0, 0, 0)
        self.graphics.ci.setContentsMargins(0, 0, 0, 0)
        self.graphics.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.graphics.ci.layout.setHorizontalSpacing(0)
        if self.use_opengl:


            use_gl = getattr(self.graphics, "useOpenGL", None)
            if callable(use_gl):
                try:
                    use_gl(True)
                except (RuntimeError, TypeError) as error:
                    self.use_opengl = False
                    self._opengl_runtime_failure_reason = f"viewport setup: {type(error).__name__}"

        self.graphics.setBackground(theme["bg"])


        self.graphics.setViewportUpdateMode(self._normal_viewport_update_mode())
        self.graphics.setOptimizationFlag(
            QtWidgets.QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing,
            True,
        )
        self.graphics.setOptimizationFlag(
            QtWidgets.QGraphicsView.OptimizationFlag.DontSavePainterState,
            False,
        )
        self.graphics.setCacheMode(QtWidgets.QGraphicsView.CacheModeFlag.CacheNone)
        self._resize_expensive_deferred = False
        self._snapshot_visibility_fit = False
        self._snapshot_visibility_rendered = False
        self._initial_snapshot_painted = False
        self._opengl_verify_attempts = 0
        self._opengl_runtime_failed = False
        layout.addWidget(self.graphics)


        self.price_axis = PriceAxisItem("right")
        self.price_plot = self.graphics.addPlot(
            row=0,
            col=0,
            axisItems={
                "bottom": TimeAxisItem("bottom"),
                "right": self.price_axis,
            },
        )
        self.graphics.scene().pan_view_box = self.price_plot.getViewBox()
        self.oi_plot = self.graphics.addPlot(
            row=1,
            col=0,
            viewBox=StudyViewBox(),
            axisItems={
                "bottom": TimeAxisItem("bottom"),
                "right": HumanAxisItem("right"),
            },
        )
        self.atr_plot = self.graphics.addPlot(
            row=2,
            col=0,
            viewBox=StudyViewBox(),
            axisItems={
                "bottom": TimeAxisItem("bottom"),
                "right": PercentAxisItem("right"),
            },
        )
        self.funding_plot = self.graphics.addPlot(
            row=3,
            col=0,
            viewBox=StudyViewBox(),
            axisItems={
                "bottom": TimeAxisItem("bottom"),
                "right": PercentAxisItem("right"),
            },
        )
        self.rsi_plot = self.graphics.addPlot(
            row=4,
            col=0,
            viewBox=StudyViewBox(),
            axisItems={
                "bottom": TimeAxisItem("bottom"),
                "right": RsiAxisItem("right"),
            },
        )
        self.graphics.ci.layout.setRowStretchFactor(0, 14)
        self.graphics.ci.layout.setRowStretchFactor(1, 2)
        self.graphics.ci.layout.setRowStretchFactor(2, 2)
        self.graphics.ci.layout.setRowStretchFactor(3, 2)
        self.graphics.ci.layout.setRowStretchFactor(4, 2)
        self.graphics.ci.layout.setVerticalSpacing(0)
        self.oi_plot.setMaximumHeight(self.study_heights["Open Interest"])
        self.atr_plot.setMaximumHeight(self.study_heights["ATR"])
        self.funding_plot.setMaximumHeight(self.study_heights["Funding Rate History"])
        self.rsi_plot.setMaximumHeight(self.study_heights["RSI"])


        chart_font = _chart_axis_font()
        self._axis_base_width = 66
        self._effective_axis_width = 66
        for plot in (
            self.price_plot,
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        ):


            plot.layout.setContentsMargins(0, 0, 0, 0)
            plot.layout.setHorizontalSpacing(0)
            plot.hideAxis("left")
            plot.showAxis("right")
            right_axis = plot.getAxis("right")
            right_axis.setWidth(self._effective_axis_width)
            right_axis.setTickFont(chart_font)
            right_axis.setStyle(hideOverlappingLabels=True)
            plot.getAxis("bottom").setTickFont(chart_font)
            plot.showGrid(x=False, y=False)
            plot.setMenuEnabled(False)
            plot.hideButtons()
            view_box = plot.getViewBox()
            view_box.setMouseMode(pg.ViewBox.PanMode)
            if plot is self.price_plot:
                view_box.setMouseEnabled(x=True, y=True)
                plot.setClipToView(True)
            else:


                view_box.setMouseEnabled(x=True, y=False)


                view_box.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
                plot.setClipToView(False)


        self.study_panes: dict[str, tuple[pg.PlotItem, StudyPaneHeader]] = {}
        for study_name, plot in (
            ("Open Interest", self.oi_plot),
            ("ATR", self.atr_plot),
            ("Funding Rate History", self.funding_plot),
            ("RSI", self.rsi_plot),
        ):
            plot.layout.removeItem(plot.titleLabel)
            header = StudyPaneHeader(study_name, theme, plot)
            plot.getAxis("right").setStyle(tickTextOffset=6)
            header.settings_requested.connect(lambda name=study_name: self.indicator_settings_requested.emit(name))
            header.remove_requested.connect(lambda name=study_name: self._remove_study(name))
            header.auto_scale_requested.connect(lambda name=study_name: self._reset_study_scale(name))
            header.resize_started.connect(lambda name=study_name: self._begin_study_resize(name))
            header.resize_requested.connect(lambda delta, name=study_name: self._resize_study(name, delta))
            header.resize_reset.connect(lambda name=study_name: self.set_study_heights({name: self._default_study_heights[name]}))
            self.study_panes[study_name] = (plot, header)

        for plot in (
            self.price_plot,
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        ):
            plot.hideAxis("bottom")
        for plot in (
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        ):

            plot.hideAxis("top")
        self.oi_plot.setXLink(self.price_plot)
        self.atr_plot.setXLink(self.price_plot)
        self.funding_plot.setXLink(self.price_plot)
        self.rsi_plot.setXLink(self.price_plot)
        self._sync_bottom_time_axis()

        for study, plot in (
            ("Open Interest", self.oi_plot),
            ("ATR", self.atr_plot),
            ("Funding Rate History", self.funding_plot),
            ("RSI", self.rsi_plot),
        ):
            axis = plot.getAxis("right")
            if isinstance(axis, ScalableStudyAxisItem):
                axis.manual_scale_requested.connect(
                    lambda value=study: self._set_study_manual_scale(value)
                )
                axis.reset_scale_requested.connect(
                    lambda value=study: self._reset_study_scale(value)
                )
            view_box = plot.getViewBox()
            if isinstance(view_box, StudyViewBox):
                view_box.settings_requested.connect(
                    lambda value=study: self.indicator_settings_requested.emit(value)
                )
                view_box.sigRangeChangedManually.connect(
                    lambda axes, value=study: self._set_study_manual_scale(value) if axes[1] else None
                )

        seconds = INTERVAL_SECONDS[self.interval]
        self.history_candles = CandlestickItem(
            seconds,
            theme.get("candle_up", theme["green"]),
            theme.get("candle_down", theme["red"]),
            theme["bg"],
            self.candle_style,
        )
        self.live_candle = CandlestickItem(
            seconds,
            theme.get("candle_up", theme["green"]),
            theme.get("candle_down", theme["red"]),
            theme["bg"],
            self.candle_style,
        )
        self.history_candles.pixel_batch.profile_name = "history_candles"
        self.live_candle.pixel_batch.profile_name = "live_candle"
        self.history_candles.set_gpu_enabled(self.use_opengl and self.native_bar_renderer_enabled)
        self.live_candle.set_gpu_enabled(self.use_opengl and self.native_bar_renderer_enabled)
        self.history_candles.setZValue(20)
        self.live_candle.setZValue(21)
        self.price_plot.addItem(self.history_candles)
        self.price_plot.addItem(self.live_candle)

        bb_pen = pg.mkPen(
            theme["cyan"],
            width=1.15,
            style=Qt.PenStyle.SolidLine,
        )
        self.bb_mid = self.price_plot.plot(pen=bb_pen, antialias=True)
        self.bb_upper = self.price_plot.plot(pen=bb_pen, antialias=True)
        self.bb_lower = self.price_plot.plot(pen=bb_pen, antialias=True)
        for curve in (self.bb_mid, self.bb_upper, self.bb_lower):
            curve.setZValue(4)
        self.bb_fill = NativeBandItem(
            self.bb_upper,
            self.bb_lower,
            brush=pg.mkBrush(opaque_overlay_color(theme["bg"], theme["cyan"], 24)),
        )
        self.bb_fill.setZValue(3)
        self.price_plot.addItem(self.bb_fill)
        for curve in (self.bb_mid, self.bb_upper, self.bb_lower):
            curve.setClipToView(False)
            curve.setDownsampling(auto=False)
        self.trend_curves = {}
        for name, colors in (("EMA Trend", ("cyan", "amber", "purple")),
                             ("VWAP", ("amber",)),
                             ("Donchian Channels", ("green", "muted", "red"))):
            curves = []
            for color in colors:
                curve = self.price_plot.plot(pen=pg.mkPen(theme[color], width=1.15))
                curve.setZValue(7)
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                curve.setVisible(False)
                curves.append(curve)
            self.trend_curves[name] = curves
        self.trend_label = pg.TextItem(anchor=(1, 0), color=theme["muted"])
        self.trend_label.setFont(typography_font(TextRole.UI_CAPTION))
        self.trend_label.setZValue(35)
        self.trend_label.setVisible(False)
        self.price_plot.addItem(self.trend_label, ignoreBounds=True)
        self.current_price_line_overlay = CurrentPriceLineOverlay(self.graphics)
        self._live_data_revision = 0
        self._committed_live_render_key: tuple[int, str, bool] | None = None
        self._committed_volume_live_render_key: tuple[int, str] | None = None


        self._presentation_active = True
        self._hidden_presentation_dirty = False
        self._deferred_analysis_snapshot: dict[str, Any] | None = None

        self.price_axis_focus_overlay = PriceAxisFocusOverlay(self.theme, self.graphics)
        self.price_countdown_timer = QTimer(self)
        self.price_countdown_timer.setInterval(1000)
        self.price_countdown_timer.timeout.connect(self._refresh_price_countdown_tick)
        self.price_countdown_timer.start()

        crosshair_pen = pg.mkPen(alpha_color(theme["muted"], 165), width=1, style=Qt.PenStyle.DashLine)
        self.crosshair_vertical = pg.InfiniteLine(angle=90, movable=False, pen=crosshair_pen)
        self.crosshair_horizontal = pg.InfiniteLine(angle=0, movable=False, pen=crosshair_pen)
        self.crosshair_vertical.setZValue(70)
        self.crosshair_horizontal.setZValue(70)
        self.price_plot.addItem(self.crosshair_vertical, ignoreBounds=True)
        self.price_plot.addItem(self.crosshair_horizontal, ignoreBounds=True)
        self.crosshair_study_verticals: list[pg.InfiniteLine] = []
        self.crosshair_study_plots = (
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        )
        for plot in self.crosshair_study_plots:
            line = pg.InfiniteLine(angle=90, movable=False, pen=crosshair_pen)
            line.setZValue(70)
            plot.addItem(line, ignoreBounds=True)
            self.crosshair_study_verticals.append(line)
        self.crosshair_time_label = pg.TextItem(
            "",
            color=theme["text"],
            anchor=(0.5, 1),
            fill=pg.mkBrush(theme["panel2"]),
        )
        self.crosshair_time_label.setFont(chart_font)
        self.crosshair_time_label.setZValue(82)
        self.price_plot.addItem(self.crosshair_time_label, ignoreBounds=True)

        self._crosshair_axis_text = ""
        self._crosshair_axis_y_value: float | None = None


        self._pointer_over_price_plot = False

        self.crosshair_visible: bool | None = None
        self._set_crosshair_visible(False)
        self.auto_fib_badge = pg.TextItem(
            "",
            color=theme["muted"],
            anchor=(0, 0),
            fill=pg.mkBrush(theme["bg"]),
        )
        self.auto_fib_badge.setFont(typography_font(TextRole.UI_CAPTION))
        self.auto_fib_badge.setZValue(66)
        self.auto_fib_badge.setVisible(False)
        self.auto_fib_badge.setToolTip(
            "Automatic Fibonacci · Alt+F next · Shift+Alt+F previous"
        )
        self.price_plot.addItem(self.auto_fib_badge, ignoreBounds=True)
        self.drawing_selection_graphic = pg.ScatterPlotItem(
            size=8,
            symbol="s",
            pen=pg.mkPen(theme["text"], width=1.1),
            brush=pg.mkBrush(theme["bg"]),
        )
        self.drawing_selection_graphic.setZValue(68)
        self.drawing_selection_graphic.setVisible(False)
        self.price_plot.addItem(self.drawing_selection_graphic, ignoreBounds=True)

        self.visible_profile = ProfileItem(theme["bg"])
        self.session_profile = ProfileItem(theme["bg"])
        self.visible_profile.setZValue(6)
        self.session_profile.setZValue(5)
        self.price_plot.addItem(self.session_profile, ignoreBounds=True)
        self.price_plot.addItem(self.visible_profile, ignoreBounds=True)

        self.liquidation_points = pg.ScatterPlotItem(hoverable=True, hoverSize=3)
        self.liquidation_points.setZValue(30)
        self.liquidation_points.setToolTip(
            "Red down arrow: a long position was force-sold · Green up arrow: a short position was force-bought · Larger arrow: larger notional"
        )
        self.price_plot.addItem(self.liquidation_points)
        self.liquidation_points.sigHovered.connect(self._liquidation_hovered)

        self.oi_curve = self.oi_plot.plot(pen=pg.mkPen(theme["purple"], width=1.4))
        self.atr_curve = self.atr_plot.plot(pen=pg.mkPen(theme["amber"], width=1.35))
        self.funding_curve = self.funding_plot.plot(
            pen=pg.mkPen(alpha_color(theme["muted"], 220), width=1.35)
        )
        self.funding_curve.setClipToView(True)
        self.funding_curve.setDownsampling(auto=True, method="peak")
        self.funding_positive_bars = pg.BarGraphItem(
            x=[], height=[], width=1,
            brush=opaque_overlay_color(theme["bg"], theme["red"], 68), pen=None
        )
        self.funding_negative_bars = pg.BarGraphItem(
            x=[], height=[], width=1,
            brush=opaque_overlay_color(theme["bg"], theme["green"], 68), pen=None
        )
        self.funding_zero_line = pg.InfiniteLine(
            pos=0.0,
            angle=0,
            movable=False,
            pen=pg.mkPen(alpha_color(theme["muted"], 110), width=0.8, style=Qt.PenStyle.DashLine),
        )
        self.funding_plot.addItem(self.funding_positive_bars)
        self.funding_plot.addItem(self.funding_negative_bars)
        self.funding_plot.addItem(self.funding_zero_line, ignoreBounds=True)
        self.funding_plot.setToolTip(
            "Historical Binance funding settlements · bars are exact raw rates · line is robust EMA-smoothed · "
            "drag inside the pane to move vertically · drag the right axis to scale · "
            "double-click the right axis to reset"
        )
        self.rsi_curve = self.rsi_plot.plot(pen=pg.mkPen(theme["cyan"], width=1.35))
        self.rsi_upper_line = pg.InfiniteLine(
            pos=self.rsi_upper,
            angle=0,
            movable=False,
            pen=pg.mkPen(alpha_color(theme["red"], 145), width=1, style=Qt.PenStyle.DashLine),
        )
        self.rsi_lower_line = pg.InfiniteLine(
            pos=self.rsi_lower,
            angle=0,
            movable=False,
            pen=pg.mkPen(alpha_color(theme["green"], 145), width=1, style=Qt.PenStyle.DashLine),
        )
        self.rsi_plot.addItem(self.rsi_upper_line, ignoreBounds=True)
        self.rsi_plot.addItem(self.rsi_lower_line, ignoreBounds=True)
        volume_up = theme.get("candle_up", theme["green"])
        volume_down = theme.get("candle_down", theme["red"])
        self.volume_overlay = VolumeOverlayItem(
            INTERVAL_SECONDS[self.interval],
            volume_up,
            volume_down,
            theme["bg"],
            self.candle_style,
            height_percent=self.volume_bar_height_percent,
        )
        self.volume_overlay.history_batch.profile_name = "history_volume"
        self.volume_overlay.live_batch.profile_name = "live_volume"
        self.volume_overlay.set_gpu_enabled(
            self.use_opengl and self.native_bar_renderer_enabled
        )
        self.oi_curve.setZValue(12)
        self.atr_curve.setZValue(12)
        self.funding_positive_bars.setZValue(11)
        self.funding_negative_bars.setZValue(11)
        self.funding_curve.setZValue(12)
        self.funding_zero_line.setZValue(10)
        self.rsi_curve.setZValue(12)


        self.volume_overlay.setZValue(2)
        self.price_plot.addItem(self.volume_overlay, ignoreBounds=True)
        self.native_bar_composite = NativeBarCompositeItem(
            self.history_candles,
            self.live_candle,
            self.volume_overlay,
        )


        self.native_bar_composite.setZValue(20)
        self.price_plot.addItem(self.native_bar_composite, ignoreBounds=True)


        self._navigation_pending_fit = False
        self._navigation_pending_viewport = False
        self._navigation_pending_overlay = False
        self._navigation_pending_y_overlay = False
        self._navigation_pending_price_overlay = False
        self._navigation_pending_live = False
        self._navigation_pending_liquidations = False
        self._navigation_pending_oi = False
        self._navigation_pending_funding = False
        self._navigation_pending_rail = False
        self._navigation_deferred_indicators = False
        self._navigation_deferred_major_levels = False
        self._navigation_deferred_auto_fib = False
        self._navigation_deferred_profiles = False
        self._navigation_deferred_study_scale = False
        self._navigation_deferred_history = False
        self._navigation_deferred_history_page = False
        self._navigation_deferred_crosshair = False
        self._auto_scale_y_change = False
        self._navigation_flushing = False
        self._navigation_last_activity = 0.0


        self._navigation_settle_seconds = 0.30
        if self._presentation_clock is not None:
            self._presentation_clock.frame.connect(self._flush_navigation_frame)


        self._navigation_settle_timer = QTimer(self)
        self._navigation_settle_timer.setSingleShot(True)
        self._navigation_settle_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._navigation_settle_timer.timeout.connect(self._navigation_settle_due)

        self.detail_timer = QTimer(self)
        self.detail_timer.setSingleShot(True)
        self.detail_timer.setInterval(650)
        self.detail_timer.timeout.connect(self._render_live_details)


        self._pending_crosshair_position: QtCore.QPointF | None = None
        self._last_crosshair_dispatch = 0.0
        self.price_plot.getViewBox().sigXRangeChanged.connect(self._range_changed)
        self.price_plot.getViewBox().sigYRangeChanged.connect(
            self._price_y_range_changed
        )
        for plot, _header in self.study_panes.values():
            view = plot.getViewBox()


            view.sigRangeChanged.connect(self._schedule_overlay_frame)
            view.sigResized.connect(self._schedule_overlay_frame)
        self.price_plot.getViewBox().sigResized.connect(self._schedule_overlay_frame)
        self.price_axis.manual_scale_requested.connect(
            lambda: self.set_auto_scale(False)
        )
        self.graphics.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.graphics.scene().sigMouseMoved.connect(self._queue_crosshair_move)
        self.graphics.frame_needed.connect(self._scene_frame_needed)
        self.graphics.frame_presented.connect(self._surface_presented)
        self._configure_chart_viewport(self.graphics.viewport())
        self._retained_indicators = RetainedChartItems(self.price_plot.getViewBox(), self)
        for item in (
            self.bb_mid, self.bb_upper, self.bb_lower, self.bb_fill,
            *(curve for curves in self.trend_curves.values() for curve in curves),
            self.trend_label, self.auto_fib_badge,
            self.visible_profile, self.session_profile, self.liquidation_points,
        ):
            self._retained_indicators.register(item)
        self.apply_theme(theme)
        typography_controller().changed.connect(self._apply_typography)
        self._apply_typography()

    def _apply_typography(self) -> None:
        chart_font = _chart_axis_font()
        self._chart_axis_metrics = QtGui.QFontMetricsF(chart_font)
        for plot in (
            self.price_plot, self.oi_plot, self.atr_plot, self.funding_plot,
            self.rsi_plot,
        ):
            plot.getAxis("right").setTickFont(chart_font)
            plot.getAxis("bottom").setTickFont(chart_font)
        self._sync_bottom_time_axis()
        for _plot, header in self.study_panes.values():
            header.setFont(typography_font(TextRole.UI_CAPTION))
        self._layout_study_panes()
        self.crosshair_time_label.setFont(typography_font(TextRole.CHART_OVERLAY))
        self.trend_label.setFont(typography_font(TextRole.UI_CAPTION))
        self.auto_fib_badge.setFont(typography_font(TextRole.UI_CAPTION))
        overlay_font = typography_font(TextRole.CHART_OVERLAY)
        for _region, label, _zone in self.zone_graphics:
            label.setFont(overlay_font)
        for _line, label in self.horizontal_graphics:
            label.setFont(overlay_font)
        for collection in (
            self.ruler_graphics, self.fibonacci_graphics, self.auto_fib_graphics,
        ):
            for item in collection:
                if isinstance(item, pg.TextItem):
                    item.setFont(overlay_font)
        if self.order_rail_hud is not None:
            self.order_rail_hud.update()
        if self.order_rail_visual is not None:
            self.order_rail_visual.update()
        self.graphics.request_redraw()

    def _configure_chart_viewport(self, viewport: QtWidgets.QWidget) -> None:
        self._presentation_clock.register_frame_source(viewport, pace=True)
        self.render_surface_changed.emit(viewport)
        viewport.setMouseTracking(True)


        viewport.setCursor(Qt.CursorShape.ArrowCursor)
        viewport.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        viewport.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        viewport.installEventFilter(self)


        for overlay in (
            getattr(self, "current_price_line_overlay", None),
            getattr(self, "order_rail_visual", None),
            getattr(self, "order_rail_hud", None),
        ):
            if overlay is not None:
                overlay.raise_()
        if getattr(self, "current_price", 0.0) > 0.0:
            QTimer.singleShot(0, self, self._position_interaction_overlays)
        elif getattr(self, "order_rail_value", None) is not None:
            QTimer.singleShot(0, self, self._position_order_rail_hud)


        self.graphics.request_redraw()
        self._presentation_clock.request()

    def _exclude_chart_pointer_for_widget(self, widget: QtWidgets.QWidget) -> None:
        """Make an overlaid control relinquish chart crosshair ownership."""
        widget.setProperty("nightwatchChartPointerExclusion", True)
        widget.setMouseTracking(True)

        widget.setCursor(Qt.CursorShape.ArrowCursor)
        widget.installEventFilter(self)

    def _scene_position_is_main_price_chart(
        self, position: QtCore.QPointF
    ) -> bool:
        """Return whether a scene point belongs to the main price ViewBox only.

        PlotItem.sceneBoundingRect() also covers axes/layout chrome, so it is not
        a valid cursor/crosshair ownership boundary. Keep every caller on this
        single ViewBox-based predicate.
        """
        try:
            view = self.price_plot.getViewBox()
            return bool(
                self.price_plot.isVisible()
                and view.isVisible()
                and view.sceneBoundingRect().contains(QtCore.QPointF(position))
            )
        except (AttributeError, RuntimeError, TypeError):
            return False

    def _sync_chart_cursor(
        self,
        *,
        viewport_position: QtCore.QPointF | QtCore.QPoint | None = None,
        scene_position: QtCore.QPointF | None = None,
    ) -> bool:
        """Own CrossCursor only inside the main price ViewBox."""
        viewport = self.graphics.viewport()
        if scene_position is None:
            if viewport_position is None:
                viewport_position = viewport.mapFromGlobal(QtGui.QCursor.pos())
            scene_position = self.graphics.mapToScene(
                QtCore.QPoint(int(viewport_position.x()), int(viewport_position.y()))
            )
        over_price_chart = self._scene_position_is_main_price_chart(scene_position)
        desired = (
            Qt.CursorShape.CrossCursor
            if over_price_chart
            else Qt.CursorShape.ArrowCursor
        )
        if viewport.cursor().shape() != desired:
            viewport.setCursor(desired)

        if over_price_chart:
            self._pointer_over_price_plot = True
        elif (
            self._pointer_over_price_plot
            or bool(self.crosshair_visible)
            or self._pending_crosshair_position is not None
            or self._navigation_deferred_crosshair
        ):
            self._clear_chart_pointer_locator()
        else:
            self._pointer_over_price_plot = False
        return over_price_chart

    def _clear_chart_pointer_locator(self) -> None:
        self._pointer_over_price_plot = False
        self._pending_crosshair_position = None
        self._navigation_deferred_crosshair = False
        self._set_crosshair_visible(False)

    def prime_render_surface(self) -> bool:
        """Request the first real viewport paint before startup readiness polling.

        This is intentionally data-independent. It initializes/exposes a
        QOpenGLWidget without allowing bootstrap data to race the surface.
        """
        viewport = self.graphics.viewport()
        if (
            not self.isVisible()
            or not self.graphics.isVisible()
            or not viewport.isVisible()
            or viewport.width() < 32
            or viewport.height() < 32
        ):
            return False
        try:
            self.graphics.request_redraw()
        except RuntimeError:
            return False
        return self.render_surface_ready()

    def render_surface_ready(self) -> bool:
        """Return True only when the chart can safely receive its first snapshot.

        Startup deliberately waits for this before starting MarketDataHub so an
        in-memory/SQLite bootstrap cannot arrive while the fullscreen/layout
        transition is still creating or sizing the GraphicsView/OpenGL surface.
        """
        viewport = self.graphics.viewport()
        if (
            not self.isVisible()
            or not self.graphics.isVisible()
            or not viewport.isVisible()
            or viewport.width() < 32
            or viewport.height() < 32
        ):
            return False


        view_rect = self.price_plot.getViewBox().sceneBoundingRect()
        if view_rect.width() < 32.0 or view_rect.height() < 32.0:
            return False

        if self.use_opengl and not self._opengl_runtime_failed:
            is_valid = getattr(viewport, "isValid", None)
            context_getter = getattr(viewport, "context", None)


            if not callable(is_valid) or not callable(context_getter):
                return False

            try:
                if not bool(is_valid()):
                    return False
                context = context_getter()
                if context is None or not context.isValid():
                    return False
            except (RuntimeError, AttributeError, TypeError):
                return False

        return True

    def _zoom_time_axis(
        self,
        viewport_position: QtCore.QPointF,
        span_factor: float,
    ) -> bool:
        """TradingView-style horizontal zoom anchored under the pointer."""
        if not math.isfinite(span_factor) or span_factor <= 0.0:
            return False
        if self.graphics._pending_pointer is not None:
            self._flush_camera_input()
        scene_position = self.graphics.mapToScene(viewport_position.toPoint())
        view = self.price_plot.getViewBox()
        x_range = self._pending_zoom_range or view.viewRange()[0]
        x0, x1 = float(x_range[0]), float(x_range[1])
        span = x1 - x0
        if not math.isfinite(span) or span <= 0.0:
            return False


        committed = view.viewRange()[0]
        committed_span = float(committed[1] - committed[0])
        if committed_span <= 0.0:
            return False
        pointer_x = float(view.mapSceneToView(scene_position).x())
        anchor_ratio = max(0.0, min(1.0, (pointer_x - committed[0]) / committed_span))
        anchor_x = x0 + anchor_ratio * span
        seconds = float(INTERVAL_SECONDS[self.interval])
        minimum_span = seconds * 8.0
        maximum_span = seconds * float(MAX_CHART_CANDLES) * 1.10
        new_span = max(minimum_span, min(maximum_span, span * span_factor))
        if abs(new_span - span) <= max(1e-9, span * 1e-9):
            return False
        new_x0 = anchor_x - anchor_ratio * new_span
        self._pending_zoom_range = (new_x0, new_x0 + new_span)
        self._start_navigation_scheduler()
        return True

    def _flush_camera_input(self) -> None:
        """Keep wheel/move/click ordering while coalescing homogeneous bursts."""
        if self._pending_zoom_range is not None:
            zoom, self._pending_zoom_range = self._pending_zoom_range, None
            self.price_plot.getViewBox().setXRange(*zoom, padding=0)
        self.graphics.flush_pointer_motion()

    def _touchpad_zoom(
        self,
        viewport_position: QtCore.QPointF,
        magnification: float,
    ) -> bool:
        if not math.isfinite(magnification) or abs(magnification) < 1e-6:
            return False
        span_factor = math.exp(max(-0.55, min(0.55, -magnification * 1.8)))
        return self._zoom_time_axis(viewport_position, span_factor)

    def _wheel_zoom(
        self,
        viewport_position: QtCore.QPointF,
        angle_delta: int,
        pixel_delta: int,
    ) -> bool:
        if angle_delta:
            steps = max(-4.0, min(4.0, float(angle_delta) / 120.0))
            factor = 1.12 ** (-steps)
        elif pixel_delta:
            factor = math.exp(max(-0.45, min(0.45, -float(pixel_delta) / 420.0)))
        else:
            return False
        return self._zoom_time_axis(viewport_position, factor)

    def _normal_viewport_update_mode(self):
        return QtWidgets.QGraphicsView.ViewportUpdateMode.NoViewportUpdate


    def _begin_interaction_priority(self) -> None:
        """Reserve GUI-frame priority for a real chart interaction."""
        self._interaction_gc.set_active(id(self), True)
        now = time.monotonic()
        self._navigation_last_activity = now
        self._interaction_render_active = True
        self._sync_frame_activity()
        if self._interaction_priority_release_timer.isActive():
            self._interaction_priority_release_timer.stop()
        if self._interaction_priority_active:
            self.interaction_started.emit()
            return
        self._interaction_priority_active = True


        if not self._order_rail_dragging:
            self._set_crosshair_visible(False)
        self.interaction_priority_changed.emit(True)
        self.interaction_started.emit()

    def _hold_interaction_priority(self) -> None:
        """Extend the navigation-active window while pointer motion continues."""
        self._navigation_last_activity = time.monotonic()
        self._interaction_render_active = True
        self._sync_frame_activity()
        if not self._interaction_priority_active:
            self._begin_interaction_priority()

    def _schedule_interaction_priority_release(self) -> None:


        self._interaction_render_active = False
        self._sync_frame_activity()
        if not self._interaction_priority_active:
            return
        self._navigation_last_activity = time.monotonic()
        self._interaction_priority_release_timer.start()

    def _release_interaction_priority(self, *, immediate: bool = False) -> None:
        self._interaction_render_active = False
        self._sync_frame_activity()
        if immediate and self._interaction_priority_release_timer.isActive():
            self._interaction_priority_release_timer.stop()
        if not self._interaction_priority_active:
            return
        if not immediate and (
            QtWidgets.QApplication.mouseButtons() & Qt.MouseButton.LeftButton
        ):
            self._interaction_priority_release_timer.start()
            return
        self._interaction_priority_active = False
        self._interaction_gc.set_active(id(self), False)
        self.interaction_priority_changed.emit(False)

        if self._navigation_work_pending():
            self._start_navigation_scheduler(immediate=True)


    def _sync_frame_activity(self) -> None:
        self._presentation_clock.set_continuous(bool(
            self._presentation_active and self.isVisible() and (
                self._interaction_render_active
                or self._pending_zoom_range is not None
                or self._navigation_pending_rail
            )
        ))

    def _scene_frame_needed(self) -> None:
        if self._presentation_active and not self._navigation_flushing:
            self._start_navigation_scheduler()

    def _navigation_work_pending(self, *, include_rail: bool = True) -> bool:
        return bool(
            self._snapshot_mailbox is not None
            or self._bar_mailbox is not None
            or self._pending_zoom_range is not None
            or self.graphics._pending_pointer is not None
            or self._navigation_pending_fit
            or self._navigation_pending_viewport
            or self._navigation_pending_overlay
            or self._navigation_pending_y_overlay
            or self._navigation_pending_price_overlay
            or self._navigation_pending_live
            or self._navigation_pending_liquidations
            or self._navigation_pending_oi
            or self._navigation_pending_funding
            or (include_rail and self._navigation_pending_rail)
            or self._navigation_deferred_indicators
            or self._navigation_deferred_major_levels
            or self._navigation_deferred_auto_fib
            or self._navigation_deferred_profiles
            or self._navigation_deferred_study_scale
            or self._navigation_deferred_history
            or self._navigation_deferred_history_page
            or self._navigation_deferred_crosshair
        )

    def _start_navigation_scheduler(self, immediate: bool = False) -> None:
        if not self._presentation_active:
            self._hidden_presentation_dirty = True
            return
        if self._navigation_flushing:
            return
        if not self._navigation_frame_requested:
            record_performance_count("nav.scheduler_requests")
        else:
            record_performance_count("nav.scheduler_coalesced")
            return
        self._navigation_frame_requested = True
        self._presentation_clock.request(immediate=immediate)

    def _schedule_render_work(
        self,
        *,
        viewport: bool = False,
        live: bool = False,
        liquidations: bool = False,
        oi: bool = False,
        funding: bool = False,
        rail: bool = False,
        indicators: bool = False,
        major_levels: bool = False,
        auto_fib: bool = False,
        profiles: bool = False,
        study_scale: bool = False,
        history: bool = False,
        immediate: bool = False,
    ) -> None:
        self._navigation_pending_viewport |= bool(viewport)
        self._navigation_pending_live |= bool(live)
        self._navigation_pending_liquidations |= bool(liquidations)
        self._navigation_pending_oi |= bool(oi)
        self._navigation_pending_funding |= bool(funding)
        self._navigation_pending_rail |= bool(rail)
        self._navigation_deferred_indicators |= bool(indicators)
        self._navigation_deferred_major_levels |= bool(
            major_levels
            and self.indicators_enabled
            and self.indicators["Major Price Levels"]
        )
        self._navigation_deferred_auto_fib |= bool(auto_fib and self.auto_fib_enabled)
        self._navigation_deferred_profiles |= bool(
            profiles
            and self.indicators_enabled
            and (
                self.indicators["Visible Volume Profile"]
                or self.indicators["Session Volume Profile"]
            )
        )
        self._navigation_deferred_study_scale |= bool(study_scale)
        self._navigation_deferred_history |= bool(history)
        self._start_navigation_scheduler(immediate=immediate)

    def _navigation_is_active(self, now: float | None = None) -> bool:
        if not self._navigation_last_activity:
            return False
        stamp = time.monotonic() if now is None else float(now)
        return stamp - self._navigation_last_activity < self._navigation_settle_seconds

    def _arm_navigation_settle_timer(self, now: float | None = None) -> None:
        if not self._presentation_active or not self._navigation_work_pending():
            self._navigation_settle_timer.stop()
            return
        stamp = time.monotonic() if now is None else float(now)
        if self._navigation_last_activity <= 0.0:
            delay_ms = 1
        else:
            remaining = max(0.0, self._navigation_settle_seconds - (stamp - self._navigation_last_activity))
            delay_ms = max(1, int(math.ceil(remaining * 1000.0)))
        self._navigation_settle_timer.start(delay_ms)

    def _navigation_settle_due(self) -> None:
        if not self._presentation_active or not self._navigation_work_pending():
            return
        now = time.monotonic()
        if self._navigation_is_active(now):
            self._arm_navigation_settle_timer(now)
            return
        self._start_navigation_scheduler(immediate=True)

    def _commit_ready_work(self, deadline: float | None = None) -> bool:
        """Dispatch pure work or adopt bounded prepared results under a frame budget.

        There is no post-stall cooldown or category timer. Each consumer clears
        its dirty bit before running and worker completion is the next producer.
        """
        if deadline is None:
            deadline = time.perf_counter() + min(0.003, 0.25 / display_refresh_rate(self))
        changed = False
        for flag, callback in (
            ("_navigation_deferred_indicators", self._render_indicators),
            ("_navigation_deferred_profiles", self._update_profiles),
            ("_navigation_pending_oi", self._render_oi),
            ("_navigation_pending_funding", self._render_funding),
            ("_navigation_deferred_major_levels", self._refresh_major_levels_if_needed),
            ("_navigation_deferred_auto_fib", self._restore_requested_auto_fibonacci),
            ("_navigation_deferred_study_scale", self._refresh_linked_study_ranges),
            ("_navigation_pending_liquidations", self._render_liquidations),
        ):
            if time.perf_counter() >= deadline:
                break
            if not getattr(self, flag):
                continue
            setattr(self, flag, False)
            started = time.perf_counter()
            callback()
            changed = True
            record_performance_timing("commit." + flag.removeprefix("_navigation_") + "_ms", (time.perf_counter()-started)*1000)
            if time.perf_counter() >= deadline:
                break
        return changed

    def _schedule_navigation_frame(self, *, fit: bool, tasks: bool) -> None:
        self._navigation_pending_fit |= bool(fit)
        self._navigation_pending_overlay = True
        if tasks:
            self._navigation_deferred_indicators |= self._has_active_rendered_indicators()
            self._navigation_pending_viewport = True
            if (
                self.indicators_enabled
                and self.indicators["Liquidations"]
                and self.liquidation_points.isVisible()
            ):
                self._navigation_pending_liquidations = True
            if (
                self.indicators_enabled
                and (
                    self.indicators["Visible Volume Profile"]
                    or self.indicators["Session Volume Profile"]
                )
            ):
                self._navigation_deferred_profiles = True
            self._navigation_deferred_study_scale = True
            self._navigation_deferred_history = True
        self._start_navigation_scheduler()

    def _schedule_overlay_frame(self, *_args: Any) -> None:
        self._navigation_pending_overlay = True
        self._start_navigation_scheduler()

    def _price_y_range_changed(self, *_args: Any) -> None:
        """Keep live auto-scale Y changes off the full-overlay hot path."""
        self.graphics.request_redraw()
        if self._auto_scale_y_change:
            self._navigation_pending_y_overlay = True
            self._start_navigation_scheduler()
            return
        self._schedule_overlay_frame()

    def _schedule_price_overlay_frame(self) -> None:
        """Schedule only the live-price line/axis badge, not all chart labels."""
        self._navigation_pending_price_overlay = True
        self._start_navigation_scheduler()

    def _commit_price_overlay(self) -> None:
        with self._overlay_geometry_transaction():
            self._position_current_price_line_overlay()
            self._position_price_axis_focus_overlay()

    def _commit_y_overlay(self) -> None:
        """Reposition only price-view overlays whose pixel anchors depend on Y."""
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        if profile_started:
            record_performance_count("overlay.y_relayouts")
        self._position_interaction_overlays()
        view = self.price_plot.getViewBox()
        scene_rect = view.sceneBoundingRect()
        if not scene_rect.isEmpty():
            if self.trend_label.isVisible():
                self.trend_label.setPos(
                    view.mapSceneToView(scene_rect.topRight() + QtCore.QPointF(-6, 4))
                )
            if self.auto_fib_badge.isVisible():
                self.auto_fib_badge.setPos(
                    view.mapSceneToView(scene_rect.topLeft() + QtCore.QPointF(4.0, 22.0))
                )
        if self.crosshair_vertical.isVisible():
            y_range = self.price_plot.viewRange()[1]
            self.crosshair_time_label.setPos(
                self.crosshair_vertical.value(), float(y_range[0])
            )
        if profile_started:
            record_performance_timing(
                "overlay.y_relayout_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )

    def _flush_navigation_frame(self, _frame_mono: float | None = None) -> None:
        if not self._presentation_active or self._navigation_flushing:
            return
        self._navigation_frame_requested = False
        started = time.perf_counter()
        self._navigation_flushing = True
        try:


            self._flush_camera_input()


            moving = (
                self._interaction_priority_active
                or self._navigation_is_active()
                or self._resize_expensive_deferred
            )
            pointer_busy = time.monotonic() - self.graphics._pointer_received_at < 0.05

            if self._snapshot_mailbox is not None:
                payload, self._snapshot_mailbox = self._snapshot_mailbox, None
                self._commit_snapshot(payload)
            # History is part of navigation, not idle-only analytics. Fetch and
            # preparation already run in workers; adoption only swaps prepared
            # storage/indexes and remaps the resident window. Waiting for mouse
            # release here leaves a held pan looking at empty history forever.
            if not self._resize_expensive_deferred and not self._snapshot_inflight and not self._matrix_waiting:
                if self._navigation_deferred_history_page:
                    self._navigation_deferred_history_page = False
                    history_started = time.perf_counter()
                    self._commit_prepared_history_page()
                    record_performance_timing(
                        "commit.deferred_history_page_ms",
                        (time.perf_counter() - history_started) * 1000,
                    )
                if self._navigation_deferred_history:
                    self._navigation_deferred_history = False
                    self._maybe_request_older_history()
            if self._navigation_pending_viewport or self._bar_mailbox is not None:
                self._navigation_pending_viewport = False
                self._render_viewport()
            if self._navigation_pending_live:
                self._navigation_pending_live = False
                self._render_live()
            if self._navigation_pending_fit:
                self._navigation_pending_fit = False
                if self.auto_scale and self.candles:
                    self._fit_y_to_visible(*self.price_plot.viewRange()[0])
            if self._navigation_pending_rail:
                self._navigation_pending_rail = False
                self._order_rail_animation_tick()
            if self._navigation_pending_overlay:
                self._navigation_pending_overlay = False
                self._navigation_pending_y_overlay = False
                self._navigation_pending_price_overlay = False
                if moving:
                    self._position_interaction_overlays()
                else:
                    self._position_overlay_labels(force_full=True)
            elif self._navigation_pending_y_overlay:
                self._navigation_pending_y_overlay = False
                self._navigation_pending_price_overlay = False
                self._commit_y_overlay()
            elif self._navigation_pending_price_overlay:
                self._navigation_pending_price_overlay = False
                self._commit_price_overlay()
            if self._navigation_deferred_crosshair and not moving:
                self._navigation_deferred_crosshair = False
                if self._pending_crosshair_position is not None:
                    self._flush_crosshair_move()
            if not moving and not pointer_busy:
                self._commit_ready_work(
                    deadline=started + min(0.003, 0.25 / display_refresh_rate(self))
                )
        finally:
            self._navigation_flushing = False


        self._sync_frame_activity()
        if self._navigation_work_pending(include_rail=False):
            if moving:
                self._arm_navigation_settle_timer()
            elif pointer_busy:
                remaining = self.graphics._pointer_received_at + 0.05 - time.monotonic()
                self._navigation_settle_timer.start(max(1, int(math.ceil(remaining * 1000))))
            else:
                self._start_navigation_scheduler()
        record_performance_timing("nav.flush_ms", (time.perf_counter()-started)*1000)

    def _queue_crosshair_move(self, position: QtCore.QPointF) -> None:


        if not self._scene_position_is_main_price_chart(position):
            self._pointer_over_price_plot = False
            self._pending_crosshair_position = None
            self._navigation_deferred_crosshair = False
            self._set_crosshair_visible(False)
            return
        self._pointer_over_price_plot = True
        self._pending_crosshair_position = QtCore.QPointF(position)
        self._navigation_deferred_crosshair = True
        if self._interaction_priority_active and not self._order_rail_dragging:
            return
        first_dispatch = self._last_crosshair_dispatch <= 0.0
        self._start_navigation_scheduler(immediate=first_dispatch)

    def _flush_crosshair_move(self) -> None:
        position = self._pending_crosshair_position
        self._pending_crosshair_position = None
        if position is None:
            return
        self._last_crosshair_dispatch = time.monotonic()
        self._mouse_moved(position)


    def prepare_market(self, interval: str, reset_analysis: bool) -> None:
        self._pending_zoom_range = None
        self.graphics._pending_pointer = None
        self._market_generation += 1
        self._snapshot_serial += 1
        self._snapshot_job.invalidate()
        self._snapshot_recovery_active = False
        for job in self._history_input_jobs.values():
            job.invalidate()
        if reset_analysis:
            for batches in self._history_batches.values():
                batches.clear()

        for job in self._study_jobs.values():
            job.invalidate()
        self._study_mailboxes.clear()
        self._study_committed.clear()
        self._study_extents.clear()
        self._bar_job.invalidate()
        self._index_job.invalidate()
        self._snapshot_mailbox = self._bar_mailbox = None
        self._committed_closed_time = None
        self._committed_history_stride = 1
        self._committed_history_owns_tail = False
        self._snapshot_inflight = False
        self._snapshot_live_updates.clear()
        self._matrix_waiting = False
        self._price_extrema = None
        self._candle_lod_index = None
        self._price_extrema_times = np.empty(0)
        previous_interval = self.interval
        if reset_analysis:
            self.clear_magnetic_order_rail()
            self._restore_view = None
            self._timeframe_restore = None
            self._last_price_event = 0.0
        elif self._restore_view is None and self._timeframe_restore is None:
            view_range = self.price_plot.viewRange()
            if interval != previous_interval:
                self._timeframe_restore = {
                    "old_interval": previous_interval,
                    "x_range": tuple(float(value) for value in view_range[0]),
                    "y_range": tuple(float(value) for value in view_range[1]),
                    "old_latest_time": (
                        float(self.candles[-1].time) if self.candles else None
                    ),
                }
            else:
                self._restore_view = tuple(tuple(axis) for axis in view_range)
        self._snapshot_loaded = False
        self._initial_snapshot_painted = False
        self._last_kline_event = 0.0
        preserve_auto_fib = self.auto_fib_enabled and not reset_analysis
        self.interval = interval
        seconds = INTERVAL_SECONDS[interval]
        self.history_candles.interval_seconds = seconds
        self.live_candle.interval_seconds = seconds
        self.candles.clear()
        self._reset_candle_matrix(np.empty((0, 7), dtype=np.float64))
        self._invalidate_live_render_commits()
        self._invalidate_lod_cache()
        self._indicator_committed_key = None
        self._invalidate_indicator_transport()
        self._indicator_display_requested_key = None
        self._indicator_display_async_pending = None
        self._indicator_display_mailbox = None
        self._profile_requested_keys = {"visible": None, "session": None}
        self._profile_async_pending = {"visible": None, "session": None}
        self._profile_mailbox.clear()
        self._visible_profile_cache_key = None
        self._session_profile_cache_key = None
        self._major_levels_requested_key = None
        self._major_levels_async_pending = None
        self._major_levels_mailbox = None
        self._auto_fib_requested_key = None
        self._auto_fib_async_pending = None
        self._auto_fib_mailbox = None
        self._auto_fib_pending_cycle_step = None
        self._history_prepare_requested = None
        self._history_prepare_pending = None
        self._history_prepared_mailbox = None
        for curves in self.trend_curves.values():
            for curve in curves:
                curve.setData([], [])
        self.candle_times.clear()
        self.rendered_window = None
        self._history_request_oldest = None
        self._history_exhausted = False
        self._navigation_deferred_history = False
        self._navigation_deferred_history_page = False
        self.overview_restore = None
        self.oi_history.clear()
        self._atr_scale_data = (np.array([]), np.array([]))
        self._atr_scale_index = None
        if reset_analysis:
            self.funding_history.clear()
        for study in self.study_manual_scale:
            self.study_manual_scale[study] = False
        self._navigation_settle_timer.stop()
        self._navigation_frame_requested = False
        self._release_interaction_priority(immediate=True)
        self._pending_crosshair_position = None
        self._navigation_pending_fit = False
        self._navigation_pending_viewport = False
        self._navigation_pending_overlay = False
        self._navigation_pending_y_overlay = False
        self._navigation_pending_price_overlay = False
        self._navigation_pending_live = False
        self._navigation_pending_liquidations = False
        self._navigation_pending_oi = False
        self._navigation_pending_funding = False
        self._navigation_pending_rail = False
        self._navigation_deferred_indicators = False
        self._navigation_deferred_major_levels = False
        self._navigation_deferred_auto_fib = False
        self._navigation_deferred_profiles = False
        self._navigation_deferred_study_scale = False
        self._navigation_deferred_history = False
        self._navigation_deferred_history_page = False
        self._navigation_deferred_crosshair = False
        self._navigation_last_activity = 0.0
        self.detail_timer.stop()
        self.history_candles.set_data([])
        self.live_candle.set_data([])
        self.volume_overlay.clear()
        self.oi_curve.setData([], [])
        self.atr_curve.setData([], [])
        self.funding_curve.setData([], [])
        self.funding_positive_bars.setOpts(x=[], height=[])
        self.funding_negative_bars.setOpts(x=[], height=[])
        self.funding_zero_line.setVisible(False)
        self.rsi_curve.setData([], [])
        for curve in (
            self.bb_mid,
            self.bb_upper,
            self.bb_lower,
        ):
            curve.setData([], [])
        self.clear_drawings(include_auto=not preserve_auto_fib)
        if preserve_auto_fib:
            self._clear_auto_fibonacci_graphics()
            self.auto_fib_index = -1
        self.auto_fib_candidates.clear()
        self.auto_fib_dirty = True
        self._major_levels_dirty = True
        self.set_working_orders([], authoritative=False)
        self._set_drawing_mode(None)
        self.visible_levels.clear()
        self._visible_profile_cache_key = None
        self._visible_profile_cache = (np.array([], dtype=float), np.array([], dtype=float))
        self.visible_profile.set_profile(
            np.array([]),
            np.array([]),
            (0.0, 1.0),
            self.theme["cyan"],
            0.19,
        )
        if reset_analysis:
            self.current_price = 0.0
            self._current_price_text = ""
            self._reset_price_axis_width()
            self.current_price_line_overlay.hide()
            if self.price_axis_focus_overlay is not None:
                self.price_axis_focus_overlay.hide()
        self._set_crosshair_visible(False)
        if reset_analysis:
            self.session_candles.clear()
            self.session_profile_cache = (np.array([], dtype=float), np.array([], dtype=float))
            self._session_profile_cache_key = None
            self.session_levels.clear()
            self.session_profile.set_profile(
                np.array([]),
                np.array([]),
                (0.0, 1.0),
                self.theme["purple"],
                0.12,
            )
            self.multi_frames.clear()
            self.zones.clear()
            self._zone_render_signature = None
            self.liquidations.clear()
            self._render_liquidations()
            self._render_zones()
        else:
            self.analysis_changed.emit(self.zones, self.profile_alert_levels())


        if not reset_analysis:
            self._rearm_order_rail_animation_after_transition()
        for kind, batches in self._history_batches.items():
            if batches:
                self._request_event_history(kind)

    def _matrix_snapshot(self, first=0, last=None):
        stop = len(self.candle_matrix) if last is None else min(last, len(self.candle_matrix))
        closed_stop = min(stop, max(0, len(self.candle_matrix)-1))
        closed = self.candle_matrix[first:max(first, closed_stop)]
        live = self.candle_matrix[max(first, closed_stop):stop].copy()
        return closed, live, (self._market_generation, self._history_generation)

    def _current_live_render_key(self) -> tuple[int, str, bool]:
        return (self._live_data_revision, self.interval, self.logarithmic)

    def _current_volume_live_render_key(self) -> tuple[int, str]:
        return (self._live_data_revision, self.interval)

    def _invalidate_live_render_commits(self) -> None:
        self._committed_live_render_key = None
        self._committed_volume_live_render_key = None

    def _reset_candle_matrix(self, matrix: np.ndarray) -> None:
        """Replace candle rows while preserving amortized O(1) live appends."""
        source = np.ascontiguousarray(matrix[:, :7], dtype=np.float64) if len(matrix) else np.empty((0, 7), dtype=np.float64)
        if len(source) > MAX_CHART_CANDLES:
            source = source[-MAX_CHART_CANDLES:]
        required = candle_matrix_capacity(len(source))
        self._candle_matrix_storage = np.empty((required, 7), dtype=np.float64)
        if len(source):
            self._candle_matrix_storage[: len(source)] = source
        self._candle_matrix_start = 0
        self._candle_matrix_size = len(source)
        self.candle_matrix = self._candle_matrix_storage[: self._candle_matrix_size]

    def _adopt_prepared_candle_matrix(
        self,
        storage: np.ndarray,
        size: int,
    ) -> None:
        """Adopt worker-prepared backing storage without an O(N) GUI-thread copy."""
        array = np.asarray(storage)
        count = max(0, min(int(size), len(array)))
        if (
            array.dtype != np.float64
            or array.ndim != 2
            or array.shape[1] < 7
            or not array.flags.c_contiguous
        ):
            raise ValueError("Prepared candle storage must be contiguous float64 OHLCV rows")
        self._candle_matrix_storage = array[:, :7]
        self._candle_matrix_start = 0
        self._candle_matrix_size = count
        self.candle_matrix = self._candle_matrix_storage[:count]

    def _append_candle_matrix_row(self, row: np.ndarray) -> bool:
        """Append in O(1); exhausted backing storage is rebuilt by the worker."""
        row = np.asarray(row, dtype=np.float64).reshape(7)
        rolled = self._candle_matrix_size >= MAX_CHART_CANDLES
        end = self._candle_matrix_start + self._candle_matrix_size
        if end >= len(self._candle_matrix_storage):


            self.set_snapshot({"interval": self.interval, "candles": self.candles.snapshot(),
                               "oi_history": self.oi_history, "_storage_roll": True})
            self._matrix_waiting = True
            return rolled
        if rolled:
            self._candle_matrix_start += 1
        else:
            self._candle_matrix_size += 1
        self._candle_matrix_storage[end] = row
        start = self._candle_matrix_start
        self.candle_matrix = self._candle_matrix_storage[start:start+self._candle_matrix_size]
        return rolled

    def set_snapshot(self, payload: dict[str, Any]) -> None:
        if payload.get("interval") != self.interval:
            return
        self._snapshot_serial += 1
        self._snapshot_inflight = True
        self._snapshot_recovery_active = False
        self._snapshot_live_updates.clear()
        key = (self._market_generation, self._snapshot_serial)
        tail = self.candles[-2:] if self._snapshot_loaded else ()
        self._snapshot_job.submit(key, prepare_snapshot, payload, tail)

    def _snapshot_prepared(self, key, payload) -> None:
        if key != (self._market_generation, self._snapshot_serial):
            return
        self._snapshot_mailbox = payload
        self._start_navigation_scheduler(immediate=True)

    def _snapshot_failed(self, key, message) -> None:
        if key != (self._market_generation, self._snapshot_serial):
            return
        import logging
        logging.getLogger(__name__).error("Chart snapshot preparation failed: %s", message)
        self._snapshot_inflight = False
        if not self._matrix_waiting:
            self._snapshot_live_updates.clear()
            return
        if self._snapshot_recovery_active:


            return
        self._snapshot_recovery_active = True
        self._snapshot_inflight = True
        self._snapshot_serial += 1
        payload = {"interval": self.interval, "candles": self.candles.snapshot(),
                   "oi_history": self.oi_history, "_storage_roll": True}
        self._snapshot_job.submit(
            (self._market_generation, self._snapshot_serial),
            prepare_storage_recovery, payload,
        )

    def _commit_snapshot(self, payload: dict[str, Any]) -> None:
        restore = self._restore_view
        timeframe_restore = self._timeframe_restore
        if restore is None and timeframe_restore is None and self._snapshot_loaded:
            restore = tuple(tuple(axis) for axis in self.price_plot.viewRange())
        self.interval = payload["interval"]
        self.overview_restore = None
        self.candles = payload["candles"]
        self._adopt_prepared_candle_matrix(payload["_storage"], len(self.candles))
        self.candle_times = payload["_times"]
        self._price_extrema = payload["_extrema"]
        self._candle_lod_index = payload["_lod_index"]
        self._price_extrema_times = self.candle_matrix[:-1, 0]
        self._invalidate_indicator_transport()
        self._snapshot_inflight = False
        self._snapshot_recovery_active = False
        self._matrix_waiting = False
        pending, self._snapshot_live_updates = self._snapshot_live_updates, {}
        last_event = self._last_kline_event
        self._last_kline_event = 0.0
        for event in sorted(pending.values(), key=lambda e: safe_float(e.get("E"))):
            self.update_kline(event)
        self._last_kline_event = max(self._last_kline_event, last_event)
        self._live_data_revision += 1
        self._invalidate_lod_cache()
        self.rendered_window = None
        self._history_request_oldest = None
        self._history_exhausted = bool(payload.get("_history_exhausted", self._history_exhausted))
        self.auto_fib_dirty = True
        self.oi_history = list(payload.get("oi_history", []))
        seconds = INTERVAL_SECONDS[self.interval]
        self.history_candles.interval_seconds = seconds
        self.live_candle.interval_seconds = seconds
        if timeframe_restore is not None and self.candles:
            previous_interval = str(timeframe_restore.get("old_interval") or "")
            previous_seconds = float(INTERVAL_SECONDS.get(previous_interval, seconds))
            previous_x = tuple(timeframe_restore.get("x_range", (0.0, 1.0)))
            new_x = _scaled_timeframe_x_range(
                (float(previous_x[0]), float(previous_x[1])),
                previous_seconds,
                float(seconds),
                old_latest_time=timeframe_restore.get("old_latest_time"),
                new_latest_time=float(self.candles[-1].time),
                new_earliest_time=float(self.candles[0].time),
            )
            previous_y = tuple(timeframe_restore.get("y_range", self.price_plot.viewRange()[1]))
            new_y = (float(previous_y[0]), float(previous_y[1]))
            if self.auto_scale:
                fitted_y = self._target_y_range(*new_x)
                if fitted_y is not None:
                    new_y = fitted_y
            restore = (new_x, new_y)
        self.history_candles.set_data([])
        self.live_candle.set_data([])
        price = self.candles[-1].close if self.candles else 0.0
        if price and self.current_price <= 0:
            rising = self.candles[-1].close >= self.candles[-1].open
            self._set_current_price(price, rising)
        if self.interval in self.multi_frames:
            self.multi_frames[self.interval] = list(self.candles[-800:])
            self._major_levels_dirty = True
        if self.candles and restore is None:
            visible = self.candles[-170:]
            start = visible[0].time - seconds * 3
            end = visible[-1].time + seconds * 18
            self.price_plot.setXRange(start, end, padding=0)
            low = min(c.low for c in visible)
            high = max(c.high for c in visible)
            low_y = chart_y(low, self.logarithmic)
            high_y = chart_y(high, self.logarithmic)
            pad = max((high_y - low_y) * 0.08, 1e-8)
            self.price_plot.setYRange(low_y - pad, high_y + pad, padding=0)
        if restore is not None:
            self.price_plot.getViewBox().setRange(xRange=restore[0], yRange=restore[1], padding=0)


        self._render_viewport(force=True)
        self._schedule_render_work(
            oi=bool(self.oi_history),
            indicators=self._has_active_rendered_indicators(),
            major_levels=True,
            auto_fib=True,
            profiles=True,
            study_scale=True,
        )


        self._rearm_order_rail_animation_after_transition(reposition=True)
        self._snapshot_loaded = True
        self._restore_view = None
        self._timeframe_restore = None
        self._initial_snapshot_painted = False
        self.history_candles.painted_once = False
        self.live_candle.painted_once = False
        self.graphics.request_redraw()


        self._snapshot_visibility_fit = restore is None
        self._snapshot_visibility_rendered = False
        QTimer.singleShot(0, self, self._ensure_snapshot_visible)
        self.snapshot_committed.emit(payload)
        if payload.get("_history_merge"):
            self.history_merged.emit(len(self.candles))

    def _maybe_request_older_history(self) -> None:
        if (
            self._resize_expensive_deferred
            or self._history_exhausted
            or not self.candles
        ):
            return
        oldest = float(self.candles[0].time)
        if self._history_request_oldest == oldest:
            return
        x0, x1 = self.price_plot.viewRange()[0]
        if not math.isfinite(x0) or not math.isfinite(x1) or x1 <= x0:
            return
        seconds = float(INTERVAL_SECONDS[self.interval])
        visible_span = x1 - x0


        prefetch = max(80.0 * seconds, min(visible_span * 0.35, 400.0 * seconds))
        if x0 > oldest + prefetch:
            return
        self._history_request_oldest = oldest
        self.history_requested.emit(oldest)

    def history_request_failed(self) -> None:
        """Allow a later pan/range change to retry a failed history page."""
        self._history_request_oldest = None

    def _history_merge_key(self) -> tuple[Any, ...]:
        candles = self.candles
        if not candles:
            return (self._market_generation, self.interval, 0)
        anchor = candles[-2] if len(candles) > 1 else candles[-1]
        return (
            self._market_generation,
            self.interval,
            len(candles),
            id(candles[0]),
            float(candles[0].time),
            id(anchor),
            float(anchor.time),
        )

    def _start_history_prepare_worker(
        self,
        incoming: tuple[Candle, ...],
        exhausted: bool,
    ) -> None:
        key = self._history_merge_key()
        self._history_prepare_async_key = key
        self._history_prepare_requested = (incoming, bool(exhausted))
        worker = _HistoryPrepareWorker(
            key,
            incoming,
            self.candles.snapshot(),
            exhausted,
            self.candle_times.snapshot(),
            self._matrix_snapshot()[:2],
        )
        self._history_prepare_worker = worker
        worker.signals.finished.connect(self._history_prepare_worker_finished)
        self._preparation_pool.start(worker)

    def _request_history_prepare(
        self,
        incoming: tuple[Candle, ...],
        exhausted: bool,
    ) -> None:
        if self._history_prepare_async_key is None:
            self._start_history_prepare_worker(incoming, exhausted)
            return
        self._history_prepare_pending = (incoming, bool(exhausted))

    @QtCore.Slot(object, object, object, object, object, object, object)
    def _history_prepare_worker_finished(
        self,
        key: object,
        combined: object,
        matrix: object,
        times: object,
        added: object,
        exhausted: object,
        error: object,
    ) -> None:
        finished_key = key if isinstance(key, tuple) else tuple(key)
        self._history_prepare_async_key = None
        self._history_prepare_worker = None
        if error is not None:
            self._history_prepare_requested = None
            self._history_prepare_pending = None
            self._history_request_oldest = None
            return
        if finished_key == self._history_merge_key():
            prepared_combined = combined if isinstance(combined, CandlePages) else CandlePages(combined or ())
            prepared_times = times if isinstance(times, CandlePages) else CandlePages(times or ())
            prepared_storage, prepared_size, extrema, lod_index = matrix
            self._history_prepared_mailbox = (
                finished_key,
                prepared_combined,
                prepared_storage,
                prepared_size,
                prepared_times,
                int(added),
                bool(exhausted),
                extrema,
                lod_index,
            )
            self._navigation_deferred_history_page = True
            self._start_navigation_scheduler()
        else:
            requested = self._history_prepare_requested
            if requested is not None and finished_key[0] == self._market_generation:


                self._history_prepare_pending = requested


        if self._history_prepared_mailbox is None:
            pending = self._history_prepare_pending
            self._history_prepare_pending = None
            if pending is not None:
                self._start_history_prepare_worker(*pending)

    def _commit_prepared_history_page(self) -> int:
        prepared = self._history_prepared_mailbox
        if prepared is None:
            return 0
        key, combined, storage, matrix_size, times, added, exhausted, extrema, lod_index = prepared
        if key != self._history_merge_key():
            self._history_prepared_mailbox = None
            requested = self._history_prepare_requested
            if requested is not None:
                self._request_history_prepare(*requested)
            return 0

        self._history_prepared_mailbox = None
        if added and combined:
            resident_times = None
            if self.rendered_window is not None:
                first, last, stride = self.rendered_window
                last = min(last, len(self.candle_times) - 1)
                if first < last:
                    resident_times = (self.candle_times[first], self.candle_times[last - 1], stride)


            if self.candles and matrix_size:
                combined[-1] = self.candles[-1]
                storage[matrix_size - 1] = self.candle_matrix[-1]
                if times:
                    times[-1] = float(self.candles[-1].time)
            self.candles = combined
            self._adopt_prepared_candle_matrix(storage, matrix_size)
            self.candle_times = times
            self._price_extrema = extrema
            self._candle_lod_index = lod_index
            self._price_extrema_times = self.candle_matrix[:-1, 0]
            self._invalidate_lod_cache()
            self._invalidate_indicator_transport()
            self._visible_profile_cache_key = None
            self._profile_mailbox.pop("visible", None)
            self.rendered_window = (
                (bisect_left(times, resident_times[0]), bisect_right(times, resident_times[1]), resident_times[2])
                if resident_times is not None else None
            )
            self.auto_fib_dirty = True
            self._major_levels_dirty = True


            self._render_viewport()
            self._navigation_pending_fit = self.auto_scale
            self._navigation_deferred_indicators = False
            self._schedule_history_indicator_refresh()

        self._history_exhausted = bool(exhausted)
        self._history_request_oldest = None
        if not self._history_exhausted and added:
            self._schedule_render_work(history=True)

        self._history_prepare_requested = None
        pending = self._history_prepare_pending
        self._history_prepare_pending = None
        if pending is not None:
            self._start_history_prepare_worker(*pending)
        return int(added)


    def prepend_history_page(
        self,
        candles: list[Candle],
        exhausted: bool = False,
    ) -> int:
        """Queue one older page for worker preparation; GUI commits prepared state."""
        if not candles:
            self._history_exhausted = bool(exhausted)
            self._history_request_oldest = None
            return 0
        incoming = tuple(candles)
        self._request_history_prepare(incoming, bool(exhausted))
        return len(candles)

    def _schedule_history_indicator_refresh(self) -> None:
        if not self._has_active_rendered_indicators():
            return
        self._history_indicator_generation += 1
        generation = self._history_indicator_generation

        def refresh() -> None:
            if generation != self._history_indicator_generation:
                return
            self._schedule_render_work(indicators=True)


        QTimer.singleShot(360, self, refresh)

    def merge_history(self, candles: list[Candle]) -> int:
        self._snapshot_serial += 1
        self._snapshot_inflight = True
        self._snapshot_recovery_active = False
        self._snapshot_live_updates.clear()
        key = (self._market_generation, self._snapshot_serial)
        payload = {"interval": self.interval, "candles": candles, "oi_history": self.oi_history, "_history_merge": True}
        self._snapshot_job.submit(key, prepare_snapshot, payload, self.candles.snapshot())
        return len(self.candles)

    def set_analysis_snapshot(self, payload: dict[str, Any]) -> None:
        if not self._presentation_active:
            self._deferred_analysis_snapshot = dict(payload)
            self._hidden_presentation_dirty = True
            return
        self._deferred_analysis_snapshot = None
        self.session_candles = list(payload.get("session", []))
        self.multi_frames = dict(payload.get("multi", {}))
        self.update_funding_history(list(payload.get("funding_history", [])))
        self.auto_fib_dirty = True
        if self.interval in self.multi_frames and self.candles:
            self.multi_frames[self.interval] = list(self.candles[-800:])
        self._session_profile_cache_key = None
        self._major_levels_dirty = True
        self._schedule_render_work(major_levels=True, auto_fib=True, profiles=True)

    def _apply_price_axis_width(self, width: int) -> bool:
        width = max(40, min(160, int(width)))
        if width == getattr(self, "_effective_axis_width", 66):
            return False
        self._effective_axis_width = width
        if self._overlay_frame_geometry is not None:
            self._overlay_frame_geometry.clear()
        for plot in (
            self.price_plot,
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        ):
            plot.getAxis("right").setWidth(width)
        self.graphics.ci.layout.invalidate()
        return True

    def _reserve_price_axis_for_text(self, text: str) -> bool:
        """Latch enough right-axis width for one exact native-axis value."""
        if not text:
            return False
        if text == self._last_reserved_price_axis_text:
            return False
        self._last_reserved_price_axis_text = text
        if self._price_axis_measurement_metrics is not self._chart_axis_metrics:
            self._price_axis_measurement_metrics = self._chart_axis_metrics
            self._price_axis_text_widths.clear()
        required = self._price_axis_text_widths.get(text)
        if required is None:
            required = int(math.ceil(self._chart_axis_metrics.horizontalAdvance(text) + 14.0))
            self._price_axis_text_widths[text] = required
            if len(self._price_axis_text_widths) > 256:
                self._price_axis_text_widths.popitem(last=False)
        else:
            self._price_axis_text_widths.move_to_end(text)
        target = max(int(getattr(self, "_axis_base_width", 66)), required)


        target = max(int(getattr(self, "_effective_axis_width", 66)), target)
        return self._apply_price_axis_width(target)

    def _reset_price_axis_width(self) -> None:
        self._last_reserved_price_axis_text = ""
        self._apply_price_axis_width(int(getattr(self, "_axis_base_width", 66)))

    def _set_current_price(self, price: float, rising: bool) -> None:
        self.current_price = price
        self.current_rising = rising
        self._current_price_text = format_price(price)
        if not self._presentation_active:
            self._hidden_presentation_dirty = True
            return
        axis_geometry_changed = self._reserve_price_axis_for_text(self._current_price_text)


        self._refresh_current_price_label(position_overlay=False)
        if axis_geometry_changed:


            self._schedule_overlay_frame()
        else:
            self._schedule_price_overlay_frame()

    def _refresh_price_countdown_tick(self) -> None:
        """Update countdown text without remapping/repositioning the price overlay."""
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        self._refresh_current_price_label(position_overlay=False)
        if profile_started:
            record_performance_timing(
                "overlay.price_countdown_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )

    def _refresh_current_price_label(self, *, position_overlay: bool = True) -> None:
        """Refresh only the live price badge and candle-close countdown."""
        if self.current_price <= 0 or not self.candles:
            return
        candle_end = shift_candle_time(self.candles[-1].time, self.interval)
        remaining = max(0, int(candle_end - time.time()))
        if remaining >= 3600:
            countdown = f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}:{remaining % 60:02d}"
        else:
            countdown = f"{remaining // 60:02d}:{remaining % 60:02d}"
        style = CANDLE_STYLES.get(self.candle_style, {})
        marker = style.get(
            "up_color" if self.current_rising else "down_color",
            self.theme["green"] if self.current_rising else self.theme["red"],
        )

        self._current_price_badge_color = str(marker)
        self._current_price_badge_tooltip = f"Candle closes in {countdown}"
        if self.price_axis_focus_overlay is not None:
            self.price_axis_focus_overlay.set_current_tooltip(
                self._current_price_badge_tooltip
            )
        if position_overlay:
            self._position_price_axis_focus_overlay()

    def set_last_price(self, price: float, event_time: float) -> bool:
        if not math.isfinite(price) or price <= 0 or event_time < self._last_price_event:
            return False
        self._last_price_event = event_time
        if price == self.current_price:


            return True
        self._set_current_price(price, price >= self.current_price)
        return True

    def update_kline(self, event: dict[str, Any]) -> tuple[float, bool]:
        row = event.get("k") or {}
        stamp = safe_float(event.get("E"))
        if row.get("i", self.interval) != self.interval or stamp < self._last_kline_event:
            return self.current_price, False
        if self._snapshot_inflight:
            self._snapshot_live_updates[row.get("t")] = event
        self._last_kline_event = stamp
        if self._matrix_waiting:
            price = safe_float(row.get("c"))
            self.set_last_price(price, stamp)
            return price, bool(row.get("x"))
        candle = Candle.from_stream(row)
        if self.candles and candle.time < self.candles[-1].time:
            return self.current_price, False
        closed = bool(row.get("x"))
        matrix_row = np.asarray(
            (
                candle.time,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.quote_volume,
            ),
            dtype=np.float64,
        )
        same_live_candle = bool(self.candles and self.candles[-1].time == candle.time)
        render_changed = not (
            same_live_candle
            and len(self.candle_matrix)
            and np.array_equal(self.candle_matrix[-1], matrix_row)
        )

        if not self.candles:
            self.candles.append(candle)
            self._reset_candle_matrix(matrix_row.reshape(1, 7))
            self._invalidate_lod_cache()
            self.candle_times.append(candle.time)
            self.rendered_window = None
        elif same_live_candle:


            self.candles[-1] = candle
            if render_changed:
                if len(self.candle_matrix):
                    self.candle_matrix[-1] = matrix_row
                else:
                    self._reset_candle_matrix(matrix_row.reshape(1, 7))
        elif candle.time > self.candles[-1].time:
            self.candles.append(candle)
            self.candle_times.append(candle.time)
            rolled_over = self._append_candle_matrix_row(matrix_row)
            if rolled_over:
                self._invalidate_lod_cache()
                self._request_price_extrema()
            else:
                self._extend_lod_cache_for_new_closed_candle()
            if len(self.candles) > MAX_CHART_CANDLES:
                overflow = len(self.candles) - MAX_CHART_CANDLES
                del self.candles[:overflow]
                del self.candle_times[:overflow]
            self.rendered_window = None
            if self._presentation_active:
                self._schedule_render_work(viewport=True, immediate=True)
            else:
                self._hidden_presentation_dirty = True

        if render_changed:
            self._live_data_revision += 1

        self.set_last_price(candle.close, stamp)
        if self._presentation_active:
            if render_changed:
                self._schedule_render_work(live=True)
            if closed:
                self.detail_timer.stop()
                self._render_live_details()
                self._refresh_analysis_after_close()
        elif closed or render_changed:
            self._hidden_presentation_dirty = True
        return candle.close, closed

    def _live_matrix_for_render(self):
        """Retain the mutable tail without materializing off-screen history.

        Only a resident window that reached the newest closed row can leave
        pending closed rows for the live VBO to bridge. A historical resident
        window has no ownership over the unrelated suffix after its right edge.
        """
        if not len(self.candle_matrix):
            return self.candle_matrix
        stride = max(1, int(self._committed_history_stride))
        if self._committed_history_owns_tail and self._committed_closed_time is not None:
            first = bisect_right(self.candle_times, self._committed_closed_time)
        elif stride > 1:


            seconds = float(INTERVAL_SECONDS[self.interval])
            bucket_seconds = stride * seconds
            bucket_start = math.floor(float(self.candle_matrix[-1, 0]) / bucket_seconds) * bucket_seconds
            first = int(np.searchsorted(self.candle_matrix[:, 0], bucket_start, side="left"))
        else:
            first = len(self.candle_matrix) - 1
        return self.candle_matrix[max(0, first):]

    def _prepare_live_render_batches(self) -> tuple[PreparedBars, PreparedBars]:
        """Build candle/volume tail geometry on the resident history LOD grid."""
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        source = self._live_matrix_for_render()
        batches = prepare_live_tail(
            source,
            max(1, int(self._committed_history_stride)),
            INTERVAL_SECONDS[self.interval],
            self.logarithmic,
            self._candle_lod_index,
        )
        if profile_started:
            record_performance_timing("live.prepare_ms", (time.perf_counter() - profile_started) * 1000.0)
            record_performance_count("live.prepare_count")
            record_performance_sum("live.source_rows", len(source))
            record_performance_sum("live.candle_instances", len(batches[0].data))
        return batches

    def _render_live(self) -> None:
        if self._matrix_waiting:
            return
        if not self._presentation_active:
            self._hidden_presentation_dirty = True
            return
        if not self.candles:
            return
        if self.auto_scale:
            self._fit_y_to_visible(*self.price_plot.viewRange()[0])

        live_key = self._current_live_render_key()
        if live_key == self._committed_live_render_key:
            if not self.detail_timer.isActive():
                self.detail_timer.start()
            return

        live_candles, live_volume = self._prepare_live_render_batches()
        self.live_candle.set_data(
            live_candles,
            self.candles[-2].close if len(self.candles) > 1 else None,
        )
        self._committed_live_render_key = live_key
        self._render_volume_live(prepared=live_volume)
        x0, x1 = self.price_plot.viewRange()[0]
        if x0 <= self.candles[-1].time <= x1:
            self.graphics.request_redraw()
        if not self.detail_timer.isActive():
            self.detail_timer.start()

    def _render_live_details(self) -> None:
        if not self._presentation_active:
            self._hidden_presentation_dirty = True
            return
        if self._resize_expensive_deferred:
            return
        if not self.candles:
            return
        x0, x1 = self.price_plot.viewRange()[0]
        if not (x0 <= self.candles[-1].time <= x1):
            return
        left = bisect_left(self.candle_times, x0)
        right = bisect_right(self.candle_times, x1)
        if right - left > MAX_RENDER_CANDLES:
            return
        view_key = (round(float(x0), 6), round(float(x1), 6))
        detail_changed = (
            self._live_detail_revision != self._live_data_revision
            or self._live_detail_view_key != view_key
        )
        if detail_changed and self._has_active_rendered_indicators():
            self._schedule_render_work(indicators=True)
        self._live_detail_revision = self._live_data_revision
        self._live_detail_view_key = view_key
        now = time.monotonic()
        profile_changed = (
            self._profile_detail_revision != self._live_data_revision
            or self._profile_detail_view_key != view_key
        )
        if profile_changed and now - self.last_live_profile >= 2.0:
            self.last_live_profile = now
            self._profile_detail_revision = self._live_data_revision
            self._profile_detail_view_key = view_key
            self._schedule_render_work(profiles=True)

    def _refresh_analysis_after_close(self) -> None:
        self.auto_fib_dirty = True
        self._major_levels_dirty = True
        if self.interval in self.multi_frames:
            self.multi_frames[self.interval] = list(self.candles[-800:])
        self._schedule_render_work(major_levels=True, auto_fib=True, profiles=True)

    def _major_levels_analysis_key(self) -> tuple[Any, ...]:
        settings = self.indicator_settings["Major Price Levels"]
        frame_keys = tuple(
            sorted(
                (timeframe, self._major_level_frame_key(candles))
                for timeframe, candles in self.multi_frames.items()
            )
        )
        return (
            frame_keys,
            float(settings["minimum_score"]),
            int(settings["maximum_levels"]),
        )

    def _start_major_levels_worker(
        self,
        payload: tuple[
            tuple[Any, ...],
            tuple[tuple[str, tuple[Candle, ...]], ...],
            float,
            float,
            int,
        ],
    ) -> None:
        key, frames, price, minimum_score, maximum_levels = payload
        self._major_levels_async_key = key
        worker = _MajorLevelsWorker(
            key, frames, price, minimum_score, maximum_levels
        )
        self._major_levels_worker = worker
        worker.signals.finished.connect(self._major_levels_worker_finished)
        self._analysis_pool.start(worker)

    def _request_major_levels_analysis(self) -> None:
        if not self.multi_frames:
            self._major_levels_mailbox = (self._major_levels_analysis_key(), [])
            self._navigation_deferred_major_levels = True
            return
        settings = self.indicator_settings["Major Price Levels"]
        key = self._major_levels_analysis_key()
        self._major_levels_requested_key = key
        price = self.candles[-1].close if self.candles else self.current_price
        frames = tuple(
            (timeframe, tuple(candles))
            for timeframe, candles in self.multi_frames.items()
        )
        payload = (
            key,
            frames,
            float(price),
            float(settings["minimum_score"]),
            int(settings["maximum_levels"]),
        )
        if self._major_levels_async_key is None:
            self._start_major_levels_worker(payload)
            return
        if self._major_levels_async_key == key:
            return
        self._major_levels_async_pending = payload

    @QtCore.Slot(object, object, object)
    def _major_levels_worker_finished(
        self,
        key: object,
        zones: object,
        error: object,
    ) -> None:
        finished_key = key if isinstance(key, tuple) else tuple(key)
        self._major_levels_async_key = None
        self._major_levels_worker = None
        if (
            error is None
            and finished_key == self._major_levels_requested_key
        ):
            self._major_levels_mailbox = (
                finished_key,
                list(zones) if isinstance(zones, (list, tuple)) else [],
            )
            self._navigation_deferred_major_levels = True
            self._start_navigation_scheduler()

        pending = self._major_levels_async_pending
        self._major_levels_async_pending = None
        if pending is not None and pending[0] != finished_key:
            self._start_major_levels_worker(pending)

    def _refresh_major_levels_if_needed(self) -> None:
        """Commit prepared level zones or request worker analysis; never analyze here."""
        if (
            not self._major_levels_dirty
            or not self.indicators_enabled
            or not self.indicators["Major Price Levels"]
        ):
            return
        key = self._major_levels_analysis_key()
        mailbox = self._major_levels_mailbox
        if mailbox is not None and mailbox[0] == key:
            self._major_levels_mailbox = None
            self.zones = mailbox[1]
            self._major_levels_dirty = False
            self._render_zones()


            if self.auto_fib_enabled and self.auto_fib_dirty:
                self._navigation_deferred_auto_fib = True
            return
        self._request_major_levels_analysis()

    @staticmethod
    def _major_level_frame_key(candles: list[Candle]) -> tuple[Any, ...]:
        """O(1) identity key for immutable/replaced multi-timeframe snapshots."""
        if not candles:
            return (0,)


        last = candles[-1]
        first = candles[0]
        return (
            id(candles),
            len(candles),
            id(first),
            float(first.time),
            id(last),
            float(last.time),
        )

    def _sync_crosshair_study_visibility(self) -> None:
        visible = bool(self.crosshair_visible)
        for line, plot in zip(
            self.crosshair_study_verticals,
            self.crosshair_study_plots,
        ):
            line.setVisible(visible and plot.isVisible())

    def _set_crosshair_visible(self, visible: bool) -> None:
        if visible == self.crosshair_visible:
            self._sync_crosshair_study_visibility()
            return
        self.crosshair_visible = visible
        for item in (
            self.crosshair_vertical,
            self.crosshair_horizontal,
            self.crosshair_time_label,
        ):
            item.setVisible(visible)


        if not visible:
            self._crosshair_axis_text = ""
            self._crosshair_axis_y_value = None
            self._position_price_axis_focus_overlay()
        self._sync_crosshair_study_visibility()

    def _nearest_candle_index(self, x_value: float) -> int | None:
        if not self.candle_times:
            return None
        index = bisect_left(self.candle_times, x_value)
        if index <= 0:
            nearest = 0
        elif index >= len(self.candle_times):
            nearest = len(self.candle_times) - 1
        else:
            previous = index - 1
            nearest = (
                previous
                if abs(x_value - self.candle_times[previous])
                <= abs(self.candle_times[index] - x_value)
                else index
            )


        half_slot = max(0.5, float(INTERVAL_SECONDS[self.interval]) * 0.5)
        if abs(float(x_value) - float(self.candle_times[nearest])) > half_slot:
            return None
        return nearest

    def _mouse_moved(self, position: QtCore.QPointF) -> None:
        if (
            not self._pointer_over_price_plot
            or not self._scene_position_is_main_price_chart(position)
        ):
            self._set_crosshair_visible(False)
            return

        point = self.price_plot.getViewBox().mapSceneToView(position)
        raw_x = point.x()
        y_value = point.y()
        price = raw_price(y_value, self.logarithmic)
        if not self.candle_times:
            self._set_crosshair_visible(False)
            return

        candle_index = self._nearest_candle_index(raw_x)
        _x_range, y_range = self.price_plot.viewRange()
        if candle_index is None:


            crosshair_x = float(raw_x)
        else:
            candle = self.candles[candle_index]
            crosshair_x = float(candle.time)


        self.crosshair_vertical.setValue(crosshair_x)
        for line, plot in zip(
            self.crosshair_study_verticals,
            self.crosshair_study_plots,
        ):
            if plot.isVisible():
                line.setValue(crosshair_x)
        self.crosshair_horizontal.setValue(y_value)
        self._crosshair_axis_text = format_price(price)
        self._crosshair_axis_y_value = float(y_value)

        timestamp = datetime.fromtimestamp(max(0.0, crosshair_x), timezone.utc)
        time_text = timestamp.strftime("%d %b %Y · %H:%M UTC")
        if time_text != getattr(self, "_crosshair_time_text", None):
            self._crosshair_time_text = time_text
            self.crosshair_time_label.setText(time_text)
        self.crosshair_time_label.setPos(crosshair_x, y_range[0])
        self._set_crosshair_visible(True)
        self._position_price_axis_focus_overlay()

        if self.drawing_mode and self.drawing_start is not None:
            self._render_drawing(
                self.drawing_start,
                QtCore.QPointF(raw_x, y_value),
            )

    def set_developer_layout_tuning(
        self,
        *,
        vertical_spacing: int = 0,
        axis_width: int = 66,
    ) -> None:
        """Apply non-render-path chart geometry overrides from Developer tools."""
        spacing = max(0, min(24, int(vertical_spacing)))
        width = max(40, min(160, int(axis_width)))

        grid = self.graphics.ci.layout
        grid.setVerticalSpacing(spacing)
        self._axis_base_width = width
        if self.current_price > 0.0:
            self._reserve_price_axis_for_text(self._current_price_text)
        else:
            self._apply_price_axis_width(width)
        grid.invalidate()


        self._position_price_axis_focus_overlay()
        QTimer.singleShot(0, self._position_price_axis_focus_overlay)

    def developer_geometry_report(self) -> str:
        """Return scene-space pane geometry for diagnosing unexplained dead strips."""
        def rect_text(name: str, item: Any) -> str:
            try:
                rect = item.sceneBoundingRect()
            except (AttributeError, RuntimeError):
                return f"{name}: unavailable"
            return (
                f"{name}: x={rect.x():.1f} y={rect.y():.1f} "
                f"w={rect.width():.1f} h={rect.height():.1f} "
                f"bottom={rect.bottom():.1f}"
            )

        price_plot_rect = self.price_plot.sceneBoundingRect()
        price_view_rect = self.price_plot.getViewBox().sceneBoundingRect()
        lines = [
            f"graphics: {self.graphics.width()}x{self.graphics.height()}",
            rect_text("price PlotItem", self.price_plot),
            rect_text("price ViewBox", self.price_plot.getViewBox()),
        ]
        first_visible_study = None
        for name, plot in (
            ("OI PlotItem", self.oi_plot),
            ("ATR PlotItem", self.atr_plot),
            ("Funding PlotItem", self.funding_plot),
            ("RSI PlotItem", self.rsi_plot),
        ):
            if plot.isVisible():
                lines.append(rect_text(name, plot))
                if first_visible_study is None:
                    first_visible_study = plot
        if first_visible_study is not None:
            study_rect = first_visible_study.sceneBoundingRect()
            lines.append(
                f"gap: price ViewBox→first study = "
                f"{study_rect.top() - price_view_rect.bottom():.1f}px"
            )
            lines.append(
                f"gap: price PlotItem→first study = "
                f"{study_rect.top() - price_plot_rect.bottom():.1f}px"
            )
        return "\n".join(lines)


    def set_presentation_active(self, active: bool) -> None:
        """Suspend/resume render-only work while preserving latest chart state."""
        active = bool(active)
        if active == self._presentation_active:
            return
        self._presentation_active = active
        if not active:
            self._resize_expensive_deferred = False
            self._set_bar_resize_active(False)
            self._interaction_gc.set_active(self._resize_gc_token, False)
            self._pending_zoom_range = None
            self._presentation_clock.cancel()
            for timer in (
                self.detail_timer,
                self.price_countdown_timer,
                self._interaction_priority_release_timer,
                self._navigation_settle_timer,
            ):
                timer.stop()
            self._pending_crosshair_position = None
            self._navigation_frame_requested = False
            self._navigation_pending_fit = False
            self._navigation_pending_viewport = False
            self._navigation_pending_overlay = False
            self._navigation_pending_y_overlay = False
            self._navigation_pending_price_overlay = False
            self._navigation_pending_live = False
            self._navigation_pending_liquidations = False
            self._navigation_pending_oi = False
            self._navigation_pending_funding = False
            self._navigation_pending_rail = False
            self._navigation_deferred_indicators = False
            self._navigation_deferred_profiles = False
            self._navigation_deferred_study_scale = False
            self._navigation_deferred_history = False
            self._navigation_deferred_crosshair = False
            self._release_interaction_priority(immediate=True)
            return

        self._sync_frame_activity()
        if not self.price_countdown_timer.isActive():
            self.price_countdown_timer.start()
        deferred_analysis = self._deferred_analysis_snapshot
        if deferred_analysis is not None:
            self.set_analysis_snapshot(deferred_analysis)
        if self.current_price > 0.0:
            self._reserve_price_axis_for_text(self._current_price_text)
            self._refresh_current_price_label(position_overlay=False)
        if self.candles:


            if self._hidden_presentation_dirty:
                self._refresh_analysis_after_close()
            self._render_viewport(force=True)
            self._render_oi()
            self._render_funding()
            self._render_liquidations()
            self._schedule_render_work(
                viewport=True,
                indicators=True,
                profiles=True,
                study_scale=True,
                immediate=True,
            )
        self._hidden_presentation_dirty = False
        self._position_interaction_overlays()
        if self._order_rail_animation_required():
            self._schedule_order_rail_animation_frame()
        self.graphics.request_redraw()
        if self._snapshot_mailbox is not None or self._bar_mailbox is not None:
            self._start_navigation_scheduler(immediate=True)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        if (self.use_opengl and self._isolate_gl_composition
                and not self.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)):
            # A native chart ancestor keeps raster sibling updates out of GL
            # composition. Establish it after parenting, before first exposure.
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        super().showEvent(event)
        self.set_presentation_active(True)
        self._sync_frame_activity()
        if self.order_rail_value is not None:
            QTimer.singleShot(0, self, self._position_order_rail_hud)
            QTimer.singleShot(0, self, self._schedule_order_rail_animation_frame)
        QTimer.singleShot(0, self, self.prime_render_surface)
        if self.use_opengl and not self._opengl_runtime_failed:
            self._opengl_verify_attempts = 0
            QTimer.singleShot(0, self, self._verify_opengl_viewport)
        if self._snapshot_loaded and self.candles:
            QTimer.singleShot(0, self, self._ensure_snapshot_visible)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.set_presentation_active(False)
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if (
            self._snapshot_loaded
            and self.candles
            and not self._initial_snapshot_painted
        ):
            QTimer.singleShot(0, self, self._ensure_snapshot_visible)

    def _verify_opengl_viewport(self) -> None:
        if not self.use_opengl or self._opengl_runtime_failed:
            return
        viewport = self.graphics.viewport()
        is_valid = getattr(viewport, "isValid", None)
        context_getter = getattr(viewport, "context", None)


        if not callable(is_valid) or not callable(context_getter):
            self._fallback_to_raster_viewport("viewport has no OpenGL context")
            return

        if not self.isVisible() or not viewport.isVisible():
            return

        valid = bool(is_valid())
        context = context_getter()
        if valid and context is not None and context.isValid():
            # Warm persistent area shaders before the first indicator toggle.
            # This runs outside painting while the surface is being initialized.
            viewport.makeCurrent()
            try:
                for area in (self.bb_fill, self.visible_profile, self.session_profile):
                    area.prime_gpu(context)
            finally:
                viewport.doneCurrent()
            return

        self._opengl_verify_attempts += 1
        if self._opengl_verify_attempts < 8:
            QTimer.singleShot(50, self, self._verify_opengl_viewport)
            return

        self._fallback_to_raster_viewport("OpenGL context invalid after startup verification")

    def _fallback_to_raster_viewport(self, reason: str = "OpenGL viewport unavailable") -> None:
        if self._opengl_runtime_failed:
            return
        self._opengl_runtime_failed = True
        self._opengl_runtime_failure_reason = str(reason)[:160]
        self.use_opengl = False

        for item in (
            self.history_candles,
            self.live_candle,
            self.volume_overlay,
        ):
            item.set_gpu_enabled(False)

        use_gl = getattr(self.graphics, "useOpenGL", None)
        if callable(use_gl):
            try:
                use_gl(False)
            except (RuntimeError, TypeError):
                pass

        pg.setConfigOption("useOpenGL", False)
        self.graphics.setViewportUpdateMode(
            self._normal_viewport_update_mode()
        )
        self._configure_chart_viewport(self.graphics.viewport())
        self.rendered_window = None
        if self._snapshot_loaded and self.candles:
            QTimer.singleShot(0, self, self._ensure_snapshot_visible)
        else:
            self.graphics.request_redraw()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        viewport = self.graphics.viewport()
        if (
            bool(watched.property("nightwatchChartPointerExclusion"))
            and event.type() in (QtCore.QEvent.Type.Enter, QtCore.QEvent.Type.MouseMove)
        ):
            self._clear_chart_pointer_locator()
        if watched is viewport and event.type() == QtCore.QEvent.Type.Resize:
            # The ViewBox receives its final geometry after this event. Move
            # overlays once at the frame boundary, using that settled geometry.
            self._layout_study_panes()
            self._schedule_overlay_frame()
        if watched is viewport:
            event_type = event.type()
            if event_type in (
                QtCore.QEvent.Type.MouseButtonPress,
                QtCore.QEvent.Type.MouseButtonRelease,
                QtCore.QEvent.Type.MouseButtonDblClick,
            ):
                self._flush_camera_input()
            if event_type == QtCore.QEvent.Type.Enter:


                self._sync_chart_cursor()
            elif event_type == QtCore.QEvent.Type.Leave:
                if viewport.cursor().shape() != Qt.CursorShape.ArrowCursor:
                    viewport.setCursor(Qt.CursorShape.ArrowCursor)
                self._clear_chart_pointer_locator()
            elif event_type == QtCore.QEvent.Type.MouseMove:
                self._sync_chart_cursor(viewport_position=event.position())
            if event_type == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._begin_interaction_priority()
            elif event_type == QtCore.QEvent.Type.MouseMove:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self._hold_interaction_priority()
            elif event_type == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._schedule_interaction_priority_release()
            elif event_type in (
                QtCore.QEvent.Type.Hide,
                QtCore.QEvent.Type.WindowDeactivate,
                QtCore.QEvent.Type.UngrabMouse,
            ):
                self._release_interaction_priority(immediate=True)
        if (
            watched is viewport
            and event.type() == QtCore.QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
            and self.drawing_mode is None
            and not self.order_rail_placement_mode
            and not self._order_rail_dragging
        ):


            scene_position = self.graphics.mapToScene(event.position().toPoint())
            view = self.price_plot.getViewBox()
            if view.sceneBoundingRect().contains(scene_position):
                point = view.mapSceneToView(scene_position)
                self._add_horizontal_line(float(point.y()))
                self._set_selected_drawing(
                    ("horizontal", len(self.horizontal_graphics) - 1)
                )
                event.accept()
                return True

        if watched is viewport and self._order_rail_dragging:
            if (
                event.type() == QtCore.QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
            ):
                self._cancel_order_rail_drag()
                event.accept()
                return True
            if (
                event.type() == QtCore.QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.RightButton
            ):
                self._cancel_order_rail_drag()
                event.accept()
                return True
            if event.type() == QtCore.QEvent.Type.MouseMove:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    viewport_position = QtCore.QPointF(event.position())
                    self._order_rail_pending_drag_position = viewport_position


                    self._schedule_order_rail_animation_frame()
                    event.accept()
                    return True
                self._finish_order_rail_drag()
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton:
                    viewport_position = QtCore.QPointF(event.position())
                    self._order_rail_pending_drag_position = None
                    self._move_order_rail_from_viewport(viewport_position)
                    self._finish_order_rail_drag()
                    event.accept()
                    return True
                if event.button() == Qt.MouseButton.RightButton:
                    self._cancel_order_rail_drag()
                    event.accept()
                    return True
            elif event.type() in (
                QtCore.QEvent.Type.Hide,
                QtCore.QEvent.Type.WindowDeactivate,
                QtCore.QEvent.Type.UngrabMouse,
            ):
                self._cancel_order_rail_drag()
        if watched is viewport and event.type() == QtCore.QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                viewport.setFocus(Qt.FocusReason.MouseFocusReason)
                self._begin_interaction_priority()
                if self._touchpad_zoom(event.position(), float(event.value())):
                    self._schedule_interaction_priority_release()
                    event.accept()
                    return True
                self._schedule_interaction_priority_release()
        if watched is viewport and event.type() == QtCore.QEvent.Type.Wheel:
            viewport.setFocus(Qt.FocusReason.MouseFocusReason)
            self._begin_interaction_priority()
            if self._wheel_zoom(
                event.position(),
                int(event.angleDelta().y()),
                int(event.pixelDelta().y()),
            ):
                self._schedule_interaction_priority_release()
                event.accept()
                return True
            self._schedule_interaction_priority_release()
        if (
            watched is viewport and self.drawing_mode is None and self.magnetic_order_rail_enabled
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() in {Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton}
        ):
            mods=event.modifiers(); exact_ctrl=bool(mods & Qt.KeyboardModifier.ControlModifier) and not bool(mods & (Qt.KeyboardModifier.ShiftModifier|Qt.KeyboardModifier.AltModifier|Qt.KeyboardModifier.MetaModifier))
            if exact_ctrl:
                scene_position=self.graphics.mapToScene(event.position().toPoint()); view=self.price_plot.getViewBox()
                if view.sceneBoundingRect().contains(scene_position):
                    point=view.mapSceneToView(scene_position); side="BUY" if event.button()==Qt.MouseButton.LeftButton else "SELL"
                    self._place_order_rail(point.y(), side=side, preset=self.order_rail_order_preset, preset_name=self.order_rail_order_preset_name); event.accept(); return True

        if (
            watched is viewport
            and self.order_rail_placement_mode
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
        ):
            if event.button() == Qt.MouseButton.RightButton:
                self.set_order_rail_placement(False)
                return True
            if event.button() == Qt.MouseButton.LeftButton:
                scene_position = self.graphics.mapToScene(event.position().toPoint())
                view = self.price_plot.getViewBox()
                if view.sceneBoundingRect().contains(scene_position):
                    point = view.mapSceneToView(scene_position)
                    self._place_order_rail(point.y(), side="BUY", preset=self.order_rail_order_preset, preset_name=self.order_rail_order_preset_name)
                    return True
        if (
            watched is viewport
            and self.drawing_mode is None
            and self.order_rail_value is not None
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            geometry = self._order_rail_view_geometry()
            if geometry is not None:
                plot_rect, line_y = geometry
                position = event.position()
                if (
                    plot_rect.contains(position)
                    and abs(float(position.y()) - line_y)
                    <= float(self.order_rail_config["drag_hitbox"])
                ):
                    self._order_rail_dragging = True
                    self._order_rail_drag_start_value = self.order_rail_value
                    self._order_rail_pending_drag_position = QtCore.QPointF(position)


                    self._set_crosshair_visible(False)
                    if self.order_rail_hud is not None:
                        self.order_rail_hud.set_external_dragging(True)
                    viewport.setFocus(Qt.FocusReason.MouseFocusReason)
                    try:
                        viewport.grabMouse()
                    except RuntimeError:
                        pass
                    self._order_rail_last_frame = time.monotonic()
                    self._schedule_order_rail_animation_frame()
                    event.accept()
                    return True
        if (
            watched is viewport
            and self.drawing_mode is not None
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
        ):
            button = event.button()
            if button == Qt.MouseButton.RightButton:
                self.cancel_drawing()
                return True
            if button == Qt.MouseButton.LeftButton:
                scene_position = self.graphics.mapToScene(event.position().toPoint())
                if self._drawing_click_at(scene_position):
                    return True
        return super().eventFilter(watched, event)

    def _mouse_clicked(self, event: Any) -> None:
        if self.order_rail_placement_mode:
            return
        if self.drawing_mode is None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._select_drawing_at(event.scenePos())
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.cancel_drawing()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._drawing_click_at(event.scenePos()):
            return
        event.accept()

    def _drawing_click_at(self, position: QtCore.QPointF) -> bool:
        view = self.price_plot.getViewBox()
        if not view.sceneBoundingRect().contains(position):
            return False
        point = view.mapSceneToView(position)
        anchor = QtCore.QPointF(point.x(), point.y())
        if self.drawing_mode == "horizontal":
            self._add_horizontal_line(anchor.y())
            self._set_drawing_mode(None)
            return True
        if self.drawing_start is None:
            if self.drawing_mode == "ruler":
                self.ruler_definition = None
            elif self.drawing_mode == "fibonacci":
                self.fibonacci_definition = None
            self._set_selected_drawing(None)
            self._clear_drawing_graphics(self.drawing_mode)
            self.drawing_start = anchor
            self.drawing_preview_active = True
            self._render_drawing(anchor, anchor)
        else:
            self._render_drawing(self.drawing_start, anchor)
            self._commit_manual_drawing(self.drawing_mode, self.drawing_start, anchor)
            self.drawing_start = None
            self.drawing_preview_active = False
            self._set_drawing_mode(None)
        return True

    def set_drawing_mode(self, mode: str | None) -> None:
        if mode not in {None, "ruler", "fibonacci", "horizontal"}:
            return
        if mode is not None and self.order_rail_placement_mode:
            self.set_order_rail_placement(False)
        if self.drawing_preview_active:
            self._clear_drawing_graphics(self.drawing_mode)
        self.drawing_start = None
        self.drawing_preview_active = False
        self._set_drawing_mode(mode)

    def _set_drawing_mode(self, mode: str | None) -> None:
        if mode == self.drawing_mode:
            return
        self.drawing_mode = mode
        unlocked = mode is None and not self.order_rail_placement_mode
        self.price_plot.getViewBox().setMouseEnabled(x=unlocked, y=unlocked)
        self.drawing_mode_changed.emit(mode)

    def cancel_drawing(self) -> None:
        if self.drawing_preview_active:
            self._clear_drawing_graphics(self.drawing_mode)
        self.drawing_start = None
        self.drawing_preview_active = False
        self._set_drawing_mode(None)

    def clear_drawings(self, include_auto: bool = True) -> None:
        self._clear_drawing_graphics("ruler")
        self._clear_drawing_graphics("fibonacci")
        self._clear_drawing_graphics("horizontal")
        self.ruler_definition = None
        self.fibonacci_definition = None
        self._set_selected_drawing(None)
        if include_auto:
            self.clear_auto_fibonacci(reset_candidates=False)

    def export_manual_drawings(self) -> list[dict[str, float | str]]:
        drawings: list[dict[str, float | str]] = []
        for line, _label in self.horizontal_graphics:
            price = raw_price(float(line.value()), self.logarithmic)
            if price > 0:
                drawings.append({"type": "horizontal", "price": price})
        if self.ruler_definition is not None:
            drawings.append({"type": "ruler", **self.ruler_definition})
        if self.fibonacci_definition is not None:
            drawings.append({"type": "fibonacci", **self.fibonacci_definition})
        return drawings

    def import_manual_drawings(self, drawings: list[dict[str, Any]]) -> None:
        self.clear_drawings(include_auto=False)
        for drawing in drawings[:500]:
            kind = str(drawing.get("type") or "")
            if kind == "horizontal":
                price = safe_float(drawing.get("price"))
                if price > 0:
                    self._add_horizontal_line(chart_y(price, self.logarithmic))
                continue
            if kind not in {"ruler", "fibonacci"}:
                continue
            definition = {
                "start_time": safe_float(drawing.get("start_time")),
                "start_price": safe_float(drawing.get("start_price")),
                "end_time": safe_float(drawing.get("end_time")),
                "end_price": safe_float(drawing.get("end_price")),
            }
            if min(definition.values()) <= 0:
                continue
            start = QtCore.QPointF(
                definition["start_time"],
                chart_y(definition["start_price"], self.logarithmic),
            )
            end = QtCore.QPointF(
                definition["end_time"],
                chart_y(definition["end_price"], self.logarithmic),
            )
            if kind == "ruler":
                self._render_ruler(start, end)
                self.ruler_definition = definition
            else:
                self._render_fibonacci(start, end)
                self.fibonacci_definition = definition
        self._apply_drawings_visibility()

    def set_drawings_visible(self, visible: bool) -> None:
        self.drawings_visible = bool(visible)
        self._apply_drawings_visibility()

    def _apply_drawings_visibility(self) -> None:
        for item in (*self.ruler_graphics, *self.fibonacci_graphics):
            item.setVisible(self.drawings_visible)
        for line, label in self.horizontal_graphics:
            line.setVisible(self.drawings_visible)
            label.setVisible(self.drawings_visible)
        self.drawing_selection_graphic.setVisible(
            self.drawings_visible and self.selected_drawing is not None
        )


    def delete_selected_drawing(self) -> bool:
        selected = self.selected_drawing
        if selected is None:
            return False
        kind, index = selected
        if kind == "horizontal" and 0 <= index < len(self.horizontal_graphics):
            line, label = self.horizontal_graphics.pop(index)
            self.price_plot.removeItem(line)
            self.price_plot.removeItem(label)
        elif kind == "ruler" and self.ruler_definition is not None:
            self._clear_drawing_graphics("ruler")
            self.ruler_definition = None
        elif kind == "fibonacci" and self.fibonacci_definition is not None:
            self._clear_drawing_graphics("fibonacci")
            self.fibonacci_definition = None
        else:
            self._set_selected_drawing(None)
            return False
        self._set_selected_drawing(None)
        return True

    def _commit_manual_drawing(
        self,
        kind: str | None,
        start: QtCore.QPointF,
        end: QtCore.QPointF,
    ) -> None:
        definition = {
            "start_time": float(start.x()),
            "start_price": raw_price(float(start.y()), self.logarithmic),
            "end_time": float(end.x()),
            "end_price": raw_price(float(end.y()), self.logarithmic),
        }
        if kind == "ruler":
            self.ruler_definition = definition
            self._set_selected_drawing(("ruler", 0))
        elif kind == "fibonacci":
            self.fibonacci_definition = definition
            self._set_selected_drawing(("fibonacci", 0))
        self._apply_drawings_visibility()

    @staticmethod
    def _point_segment_distance(
        point: QtCore.QPointF,
        start: QtCore.QPointF,
        end: QtCore.QPointF,
    ) -> float:
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-12:
            return math.hypot(point.x() - start.x(), point.y() - start.y())
        projection = (
            (point.x() - start.x()) * dx + (point.y() - start.y()) * dy
        ) / length_squared
        projection = max(0.0, min(1.0, projection))
        nearest = QtCore.QPointF(
            start.x() + projection * dx,
            start.y() + projection * dy,
        )
        return math.hypot(point.x() - nearest.x(), point.y() - nearest.y())

    def _select_drawing_at(self, scene_position: QtCore.QPointF) -> bool:
        if not self.drawings_visible:
            self._set_selected_drawing(None)
            return False
        view = self.price_plot.getViewBox()
        if not view.sceneBoundingRect().contains(scene_position):
            return False
        view_point = view.mapSceneToView(scene_position)
        candidates: list[tuple[float, tuple[str, int]]] = []
        for index, (line, _label) in enumerate(self.horizontal_graphics):
            line_scene = view.mapViewToScene(
                QtCore.QPointF(view_point.x(), float(line.value()))
            )
            candidates.append(
                (abs(line_scene.y() - scene_position.y()), ("horizontal", index))
            )

        for kind, definition in (
            ("ruler", self.ruler_definition),
            ("fibonacci", self.fibonacci_definition),
        ):
            if definition is None:
                continue
            start = view.mapViewToScene(
                QtCore.QPointF(
                    definition["start_time"],
                    chart_y(definition["start_price"], self.logarithmic),
                )
            )
            end = view.mapViewToScene(
                QtCore.QPointF(
                    definition["end_time"],
                    chart_y(definition["end_price"], self.logarithmic),
                )
            )
            candidates.append(
                (self._point_segment_distance(scene_position, start, end), (kind, 0))
            )
            if kind == "fibonacci":
                left = min(start.x(), end.x()) - 7.0
                right = max(start.x(), end.x()) + abs(end.x() - start.x()) * 0.12 + 7.0
                if left <= scene_position.x() <= right:
                    price_span = definition["end_price"] - definition["start_price"]
                    for ratio in (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0):
                        shown = chart_y(
                            definition["start_price"] + price_span * ratio,
                            self.logarithmic,
                        )
                        level_scene = view.mapViewToScene(
                            QtCore.QPointF(view_point.x(), shown)
                        )
                        candidates.append(
                            (abs(level_scene.y() - scene_position.y()), (kind, 0))
                        )

        nearest = min(candidates, default=(float("inf"), ("", 0)))
        selected = nearest[1] if nearest[0] <= 7.0 else None
        self._set_selected_drawing(selected)
        return selected is not None

    def _set_selected_drawing(
        self, selected: tuple[str, int] | None
    ) -> None:
        self.selected_drawing = selected
        self._update_drawing_selection_graphic()

    def _update_drawing_selection_graphic(self) -> None:
        selected = self.selected_drawing
        points: list[tuple[float, float]] = []
        if selected is not None and self.drawings_visible:
            kind, index = selected
            if kind == "horizontal" and 0 <= index < len(self.horizontal_graphics):
                line, _label = self.horizontal_graphics[index]
                x_range = self.price_plot.viewRange()[0]
                points.append(((x_range[0] + x_range[1]) * 0.5, float(line.value())))
            else:
                definition = (
                    self.ruler_definition
                    if kind == "ruler"
                    else self.fibonacci_definition
                )
                if definition is not None:
                    points.extend(
                        (
                            (
                                definition["start_time"],
                                chart_y(definition["start_price"], self.logarithmic),
                            ),
                            (
                                definition["end_time"],
                                chart_y(definition["end_price"], self.logarithmic),
                            ),
                        )
                    )
        self.drawing_selection_graphic.setData(
            pos=points,
            size=8,
            symbol="s",
            pen=pg.mkPen(self.theme["text"], width=1.1),
            brush=pg.mkBrush(self.theme["bg"]),
        )
        self.drawing_selection_graphic.setVisible(bool(points))

    def clear_auto_fibonacci(self, reset_candidates: bool = False) -> None:
        self._clear_auto_fibonacci_graphics()
        self.auto_fib_index = -1
        self.auto_fib_enabled = False
        if reset_candidates:
            self.auto_fib_candidates.clear()
            self.auto_fib_dirty = True

    def _clear_auto_fibonacci_graphics(self) -> None:
        for item in self.auto_fib_graphics:
            self.price_plot.removeItem(item)
        self.auto_fib_graphics.clear()
        self.auto_fib_level_graphics.clear()
        self.auto_fib_badge.setVisible(False)

    def _apply_auto_fibonacci_visibility(self) -> None:
        visible = self.indicators_enabled and self.auto_fib_enabled
        for item in self.auto_fib_graphics:
            item.setVisible(visible)
        show_badge = bool(
            self.indicator_settings.get("Auto Fibonacci", {}).get("show_badge", True)
        )
        self.auto_fib_badge.setVisible(
            visible and show_badge and bool(self.auto_fib_graphics)
        )


    def set_order_rail_placement(self, enabled: bool) -> None:
        enabled = bool(enabled) and self.magnetic_order_rail_enabled
        if enabled == self.order_rail_placement_mode:
            return
        if enabled and self.drawing_mode is not None:
            self.cancel_drawing()
        self.order_rail_placement_mode = enabled
        unlocked = self.drawing_mode is None and not enabled
        self.price_plot.getViewBox().setMouseEnabled(x=unlocked, y=unlocked)


        self._sync_chart_cursor()
        self.order_rail_placement_changed.emit(enabled)

    def set_order_rail_order_preset(self, preset: dict[str, Any], name: str = "") -> None:
        self.order_rail_order_preset=normalized_order_rail_order_preset(preset); self.order_rail_order_preset_name=str(name or self.order_rail_order_preset["orderType"])

    def set_order_rail_lab_config(self, config: dict[str, Any]) -> None:
        self.order_rail_config = normalized_order_rail_config(config)
        for parked in self._active_parked_order_rails():
            parked["hud"].set_config(self.order_rail_config)
        if self.order_rail_hud is not None:
            self.order_rail_hud.set_config(self.order_rail_config)
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def set_symbol_rules(self, rules: Any) -> None:
        """Compatibility/public symbol-rule entry point used by the shell.

        Chart rendering still owns only display precision/rail price constraints;
        order validation remains in the trading layer.
        """
        self.set_order_rail_symbol_rules(rules)

    def set_order_rail_symbol_rules(self, rules: Any) -> None:
        """Provide exchange price constraints without coupling chart rendering to trading."""
        tick_value = getattr(rules, "tick_size", 0.0)
        tick = safe_float(tick_value)
        self.order_rail_tick_size = tick if tick > 0 and math.isfinite(tick) else 0.0
        self.order_rail_tick_text = (
            str(tick_value).strip()
            if self.order_rail_tick_size > 0
            else ""
        )
        self.price_axis.set_tick_precision(self.order_rail_tick_text)
        self.order_rail_min_price = max(
            0.0, safe_float(getattr(rules, "min_price", 0.0))
        )
        maximum = safe_float(
            getattr(rules, "max_price", float("inf")),
            float("inf"),
        )
        self.order_rail_max_price = (
            maximum if maximum > 0 and math.isfinite(maximum)
            else float("inf")
        )
        if self.order_rail_value is not None:
            self.order_rail_value = self._snap_order_rail_price(
                self.order_rail_value
            )
            self._position_order_rail_hud()

    def _snap_order_rail_price(self, price: float) -> float:
        """Snap UI price to an exchange-valid tick using a cheap drag-time path."""
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            return 0.0
        if self.order_rail_min_price > 0:
            price = max(price, self.order_rail_min_price)
        if math.isfinite(self.order_rail_max_price):
            price = min(price, self.order_rail_max_price)
        tick = self.order_rail_tick_size
        if tick > 0 and math.isfinite(tick):
            units = math.floor(price / tick + 0.5 + 1e-12)
            price = max(tick, units * tick)
            if self.order_rail_min_price > 0:
                price = max(price, self.order_rail_min_price)
            if math.isfinite(self.order_rail_max_price):
                price = min(price, self.order_rail_max_price)
        return price


    def _order_rail_price_text_for(self, price: float) -> str:
        price = float(price)
        if price <= 0:
            return ""
        return _format_price_with_tick_precision(price, self.order_rail_tick_text)

    def order_rail_price_text(self) -> str:
        return self._order_rail_price_text_for(self.order_rail_raw_price())

    def magnetic_order_rail_state(self) -> dict[str, Any]:
        hud = self.order_rail_hud
        typ = (
            str(hud.order_type)
            if hud is not None
            else str(self.order_rail_order_preset.get("orderType", "LIMIT"))
        )
        reducing = bool(hud.reduce_only) if hud is not None else False
        rail = self.order_rail_raw_price()
        offset = float(hud.limit_offset_percent) if hud is not None else 0.0
        limit_price = (
            self._snap_order_rail_price(rail * (1.0 + offset / 100.0))
            if rail > 0
            else 0.0
        )
        state = {
            "railDraftId": int(self._active_order_rail_draft_id),
            "symbol": str(self._active_order_rail_symbol or self.order_rail_market_symbol),
            "railPrice": rail,
            "priceText": self.order_rail_price_text(),
            "tickSize": self.order_rail_tick_text,
            "orderType": typ,
            "presetName": (
                str(hud.preset_name)
                if hud is not None
                else self.order_rail_order_preset_name
            ),
            "armed": bool(hud.armed) if hud is not None else False,
            "submissionPending": bool(hud.submission_pending) if hud is not None else False,
            "cancellationPending": bool(hud.cancellation_pending) if hud is not None else False,
            "side": str(hud.side) if hud is not None else "BUY",
            "reduceOnly": reducing,
            "sizePercent": int(hud.size_percent) if hud is not None else 25,
            "leverage": (
                int(hud.leverage)
                if hud is not None
                else int(self.order_rail_order_preset.get("leverage", 5))
            ),
            "orderRole": "ENTRY",
            "takeProfitEnabled": bool(hud.take_profit_enabled) if hud is not None else False,
            "stopLossEnabled": bool(hud.stop_loss_enabled) if hud is not None else False,
            "positionIntent": "REDUCE" if reducing else "OPEN",
            "timeInForce": str(hud.time_in_force) if hud is not None else "GTC",
            "workingType": (
                str(hud.working_type) if hud is not None else "CONTRACT_PRICE"
            ),
            "priceProtect": bool(hud.price_protect) if hud is not None else True,
            "callbackRate": float(hud.callback_rate) if hud is not None else 0.5,
            "limitOffsetPercent": offset,
            "linePattern": (
                str(hud.line_pattern_override)
                if hud is not None
                else str(self.order_rail_order_preset.get("linePattern", "inherit"))
            ),
            "expanded": False,
            "style": self.order_rail_config["style"],
        }
        if self._active_order_rail_working_order:
            state["workingOrder"] = dict(self._active_order_rail_working_order)
        if typ == "LIMIT":
            state["price"] = rail
        elif typ in {"STOP", "TAKE_PROFIT"}:
            state["triggerPrice"] = rail
            state["price"] = limit_price
        elif typ in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            state["triggerPrice"] = rail
        elif typ == "TRAILING_STOP_MARKET":
            state["activatePrice"] = rail
        return state

    def order_rail_raw_price(self) -> float:
        return float(self.order_rail_value or 0.0)


    def _remove_active_order_rail(self) -> None:
        """Remove only the editable draft; keep already parked armed rails."""
        if self._order_rail_dragging:
            self._cancel_order_rail_drag(restore=False)
        self.order_rail_value = None
        self._active_order_rail_draft_id = 0
        self._active_order_rail_symbol = ""
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order = {}
        self._active_order_rail_matched_once = False
        self._active_order_rail_amend_origin = None
        self._active_order_rail_amend_price = None
        self._order_rail_pending_drag_position = None
        for name in ("order_rail_hud", "order_rail_visual"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.hide()
                widget.deleteLater()
                setattr(self, name, None)
        if self.order_rail_placement_mode:
            self.set_order_rail_placement(False)
        self._position_price_axis_focus_overlay()
        self._schedule_order_rail_animation_frame()

    def dismiss_unarmed_order_rail(self) -> bool:
        """Remove only a local, unsubmitted rail draft.

        Armed rails and any rail with submission/cancellation in flight are never
        removed by the global Delete/Backspace shortcut.
        """
        hud = self.order_rail_hud
        if (
            hud is None
            or self.order_rail_value is None
            or hud.armed
            or hud.submission_pending
            or hud.cancellation_pending
        ):
            return False
        self._remove_active_order_rail()
        return True

    def _request_active_order_rail_removal(self) -> None:
        hud = self.order_rail_hud
        if hud is None:
            return
        if hud.armed or hud.submission_pending or hud.cancellation_pending:
            if not hud.cancellation_pending:
                hud.set_cancellation_pending(True)
                self.order_rail_cancel_requested.emit(self.magnetic_order_rail_state())
            return
        self._remove_active_order_rail()

    def _parked_order_rail_state(self, parked: dict[str, Any]) -> dict[str, Any]:
        state = dict(parked.get("state") or {})
        hud = parked.get("hud")
        state["railDraftId"] = int(parked.get("draft_id") or state.get("railDraftId") or 0)
        state["symbol"] = str(parked.get("symbol") or state.get("symbol") or "")
        state["railPrice"] = safe_float(parked.get("price"))
        if isinstance(hud, MagneticOrderRailPanel):
            state.update({
                "armed": bool(hud.armed),
                "submissionPending": bool(hud.submission_pending),
                "cancellationPending": bool(hud.cancellation_pending),
                "side": str(hud.side),
                "reduceOnly": bool(hud.reduce_only),
                "sizePercent": int(hud.size_percent),
                "leverage": int(hud.leverage),
                "takeProfitEnabled": bool(hud.take_profit_enabled),
                "stopLossEnabled": bool(hud.stop_loss_enabled),
            })
        working = parked.get("working_order")
        if isinstance(working, dict) and working:
            state["workingOrder"] = dict(working)
        return state

    def _request_parked_order_rail_removal(self, draft_id: int) -> None:
        for parked in self._parked_order_rails:
            if int(parked.get("draft_id") or 0) != int(draft_id):
                continue
            hud = parked.get("hud")
            if not isinstance(hud, MagneticOrderRailPanel) or hud.cancellation_pending:
                return
            hud.set_cancellation_pending(True)
            self.order_rail_cancel_requested.emit(self._parked_order_rail_state(parked))
            return

    def order_rail_state_for_draft(self, draft_id: int) -> dict[str, Any] | None:
        if int(draft_id) == int(self._active_order_rail_draft_id) and self.order_rail_hud is not None:
            return self.magnetic_order_rail_state()
        for parked in self._parked_order_rails:
            if int(parked.get("draft_id") or 0) == int(draft_id):
                return self._parked_order_rail_state(parked)
        return None

    def set_order_rail_cancellation_pending(self, draft_id: int, pending: bool) -> None:
        if int(draft_id) == int(self._active_order_rail_draft_id) and self.order_rail_hud is not None:
            if self.order_rail_hud is not None:
                self.order_rail_hud.set_cancellation_pending(pending)
            return
        for parked in self._parked_order_rails:
            if int(parked.get("draft_id") or 0) == int(draft_id):
                hud = parked.get("hud")
                if isinstance(hud, MagneticOrderRailPanel):
                    hud.set_cancellation_pending(pending)
                return

    def resolve_order_rail_cancellation(self, draft_id: int, *, canceled: bool) -> None:
        if int(draft_id) == int(self._active_order_rail_draft_id) and self.order_rail_hud is not None:
            hud = self.order_rail_hud
            if hud is None:
                return
            hud.set_cancellation_pending(False)
            if canceled:
                self._start_active_order_rail_implosion()
            return
        for parked in self._active_parked_order_rails():
            if int(parked.get("draft_id") or 0) != int(draft_id):
                continue
            hud = parked.get("hud")
            if isinstance(hud, MagneticOrderRailPanel):
                hud.set_cancellation_pending(False)
            if canceled:
                self._start_parked_order_rail_implosion(parked)
            return

    def clear_magnetic_order_rail(self) -> None:
        if self._order_rail_dragging:
            self._cancel_order_rail_drag(restore=False)
        self.order_rail_value = None
        self._active_order_rail_draft_id = 0
        self._active_order_rail_symbol = ""
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order = {}
        self._active_order_rail_matched_once = False
        self._active_order_rail_amend_origin = None
        self._active_order_rail_amend_price = None
        self._order_rail_pending_drag_position = None
        self._navigation_pending_rail = False
        self._sync_frame_activity()
        for name in ("order_rail_hud", "order_rail_visual"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.hide()
                widget.deleteLater()
                setattr(self, name, None)
        for parked in self._parked_order_rails:
            for key in ("hud", "visual"):
                widget = parked.get(key)
                if isinstance(widget, QtWidgets.QWidget):
                    widget.hide()
                    widget.deleteLater()
        self._parked_order_rails.clear()
        self._mark_parked_order_rails_changed()
        if self.order_rail_placement_mode:
            self.set_order_rail_placement(False)
        self._position_price_axis_focus_overlay()

    def _order_rail_visual_color_for_hud(self, hud: MagneticOrderRailPanel | None) -> QtGui.QColor:
        if hud is None:
            return QtGui.QColor(self.theme.get("rail_neutral", self.theme.get("cyan", self.theme.get("text", "#65b7d5"))))
        if hud.armed or hud.arm_submission_preview_active():
            key = "rail_buy" if hud.side == "BUY" else "rail_sell"
            return QtGui.QColor(self.theme.get(key, self.theme.get("cyan", "#65b7d5")))
        return QtGui.QColor(self.theme.get("rail_neutral", self.theme.get("cyan", self.theme.get("text", "#65b7d5"))))


    def _apply_order_rail_theme(self) -> None:
        if self.order_rail_hud is not None:
            self.order_rail_hud.apply_theme(self.theme)
            self.order_rail_hud.set_config(self.order_rail_config)
        for parked in self._parked_order_rails:
            hud = parked.get("hud")
            if isinstance(hud, MagneticOrderRailPanel):
                hud.apply_theme(self.theme)
                hud.set_config(self.order_rail_config)
        self._position_order_rail_hud()

    def _order_rail_hud_state_changed(self) -> None:
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def _order_rail_execute_from_hud(self, side: str) -> None:
        hud = self.order_rail_hud
        if (
            hud is None
            or hud.armed
            or hud.submission_pending
            or self.order_rail_raw_price() <= 0.0
        ):
            return
        hud.side = "SELL" if str(side).upper() == "SELL" else "BUY"
        state = self.magnetic_order_rail_state()
        self.order_rail_execution_requested.emit(state)

    def set_order_rail_submission_pending(self, draft_id: int, pending: bool) -> None:
        if int(draft_id) != int(self._active_order_rail_draft_id):
            return
        hud = self.order_rail_hud
        if hud is None:
            return
        hud.set_submission_pending(bool(pending))
        if (
            not pending
            and not hud.armed
            and hud.arm_submission_preview_active()
        ):
            hud.set_arm_submission_preview(False, animated=True)
        self._schedule_order_rail_animation_frame()

    def set_order_rail_arm_submission_preview(
        self, draft_id: int, active: bool, *, animated: bool = True
    ) -> None:
        """Drive only the optimistic Ctrl+A arm presentation for this draft."""
        if int(draft_id) != int(self._active_order_rail_draft_id):
            return
        hud = self.order_rail_hud
        if hud is None or hud.armed:
            return
        hud.set_arm_submission_preview(bool(active), animated=animated)
        self._schedule_order_rail_animation_frame()

    def resolve_order_rail_submission(
        self,
        draft_id: int,
        *,
        accepted: bool,
        working: bool = True,
        working_order: dict[str, Any] | None = None,
    ) -> None:
        if int(draft_id) != int(self._active_order_rail_draft_id):
            return
        hud = self.order_rail_hud
        if hud is None:
            return
        hud.set_submission_pending(False)
        if not accepted:
            hud.set_arm_submission_preview(False, animated=True)
            self._schedule_order_rail_animation_frame()
            return
        if not working:


            self._remove_active_order_rail()
            return
        hud.set_active_contract_target(
            True,
            animated=False,
        )
        hud.set_armed(True, animated=True)
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order = dict(working_order or {})
        hud.working_order = dict(working_order or {})
        self._active_order_rail_matched_once = bool(self._active_order_rail_working_order)
        self._schedule_order_rail_animation_frame()
        if (
            self._active_order_rail_symbol
            and self.order_rail_market_symbol
            and self._active_order_rail_symbol != self.order_rail_market_symbol
        ):
            self._park_current_order_rail()
            self._position_order_rail_hud()

    def _mark_parked_order_rails_changed(self) -> None:
        self._parked_order_rail_revision += 1

    def _active_parked_order_rails(self) -> tuple[dict[str, Any], ...]:
        symbol = str(self.order_rail_market_symbol or "").upper()
        if (
            self._parked_order_rail_cache_revision != self._parked_order_rail_revision
            or self._parked_order_rail_cache_symbol != symbol
        ):
            self._parked_order_rail_active_cache = tuple(
                item
                for item in self._parked_order_rails
                if not symbol or str(item.get("symbol") or "").upper() == symbol
            )
            self._parked_order_rail_cache_revision = self._parked_order_rail_revision
            self._parked_order_rail_cache_symbol = symbol
        return self._parked_order_rail_active_cache

    def _order_rail_animation_required(self) -> bool:
        if not self.isVisible():
            return False
        has_active = (
            self.order_rail_value is not None
            and self.order_rail_hud is not None
            and self.order_rail_visual is not None
        )
        parked = self._active_parked_order_rails()
        has_parked_active = any(
            isinstance(item.get("hud"), MagneticOrderRailPanel)
            and item["hud"].isVisible()
            and bool(item["hud"].armed)
            and (
                not self.order_rail_market_symbol
                or str(item.get("symbol") or "").upper() == self.order_rail_market_symbol
            )
            for item in parked
        )
        has_imploding = any(
            bool(item.get("imploding"))
            and (
                not self.order_rail_market_symbol
                or str(item.get("symbol") or "").upper() == self.order_rail_market_symbol
            )
            for item in parked
        )
        active_hud_animation = bool(
            has_active
            and self.order_rail_hud is not None
            and self.order_rail_hud.animation_required()
        )
        parked_hud_animation = any(
            isinstance(item.get("hud"), MagneticOrderRailPanel)
            and item["hud"].isVisible()
            and item["hud"].animation_required()
            for item in parked
        )
        if not (
            has_active
            or has_parked_active
            or has_imploding
            or active_hud_animation
            or parked_hud_animation
        ):
            return False
        window = self.window()
        if isinstance(window, QtWidgets.QWidget) and window.isMinimized():
            return False
        if (
            self._order_rail_dragging
            or has_imploding
            or active_hud_animation
            or parked_hud_animation
        ):
            return True
        if not self.order_rail_config["animation_enabled"]:
            return False
        return (has_active or has_parked_active) and (
            self.order_rail_config["style"] in MagneticRailLineOverlay.ANIMATED_STYLES
        )

    def _active_order_rail_geometry_changed(self) -> None:
        self._position_order_rail_hud()
        hud = self.order_rail_hud
        if (
            isinstance(hud, MagneticOrderRailPanel)
            and hud.arm_transition_active()
            and not self._navigation_flushing
        ):
            self._schedule_order_rail_animation_frame()

    def _rearm_order_rail_animation_after_transition(self, *, reposition: bool = False) -> None:
        """Restore the single rail-animation producer after presentation resets.

        This never creates a second animation clock: the existing navigation
        frame flag remains the sole producer. Repositioning is requested only
        after a new snapshot/range has been adopted.
        """
        if reposition:
            has_active = (
                self.order_rail_value is not None
                and self.order_rail_hud is not None
                and self.order_rail_visual is not None
            )
            if has_active or self._active_parked_order_rails():
                self._position_order_rail_hud()
        if not self._order_rail_animation_required():
            self._navigation_pending_rail = False
            self._sync_frame_activity()
            return


        self._order_rail_last_frame = time.monotonic()
        self._schedule_order_rail_animation_frame()

    def _schedule_order_rail_animation_frame(self) -> None:
        self._navigation_pending_rail = self._order_rail_animation_required()
        self._sync_frame_activity()
        if not self._navigation_pending_rail:
            return
        if self._order_rail_last_frame <= 0.0:
            self._order_rail_last_frame = time.monotonic()
        if not self._navigation_flushing:
            self._start_navigation_scheduler()

    def _order_rail_animation_tick(self) -> None:
        if not self._order_rail_animation_required():
            return
        pending = self._order_rail_pending_drag_position
        if pending is not None and self._order_rail_dragging:
            self._order_rail_pending_drag_position = None
            self._move_order_rail_from_viewport(pending)
        now = time.monotonic()


        if (
            isinstance(self.order_rail_hud, MagneticOrderRailPanel)
            and self.order_rail_hud.isVisible()
            and self.order_rail_hud.arm_transition_active()
        ):
            self.order_rail_hud.advance_frame_animation(now)
        for parked in self._active_parked_order_rails():
            hud = parked.get("hud")
            if (
                isinstance(hud, MagneticOrderRailPanel)
                and hud.isVisible()
                and hud.arm_transition_active()
            ):
                hud.advance_frame_animation(now)

        configured_cap = max(0, int(self.order_rail_config.get("frame_cap", 0)))
        elapsed_raw = max(0.0, now - self._order_rail_last_frame)
        if configured_cap > 0 and not self._order_rail_dragging and elapsed_raw < (1.0 / configured_cap):
            self._schedule_order_rail_animation_frame()
            return
        elapsed = max(0.0, min(0.1, elapsed_raw))
        self._order_rail_last_frame = now
        if self.order_rail_config["animation_enabled"]:
            self._order_rail_phase += elapsed * float(self.order_rail_config["animation_speed"])
            if self.order_rail_visual is not None:
                self.order_rail_visual.advance_animation(self._order_rail_phase)
            if self.order_rail_hud is not None:
                self.order_rail_hud.advance_animation(self._order_rail_phase)
            for parked in self._active_parked_order_rails():
                if parked.get("imploding"):
                    continue
                hud = parked.get("hud")
                visual = parked.get("visual")
                visual_visible = isinstance(visual, MagneticRailLineOverlay) and visual.isVisible()
                hud_visible = isinstance(hud, MagneticOrderRailPanel) and hud.isVisible()
                if visual_visible:
                    visual.advance_animation(self._order_rail_phase)
                if hud_visible:
                    hud.advance_animation(self._order_rail_phase)

        finished: list[dict[str, Any]] = []
        for parked in self._active_parked_order_rails():
            if not parked.get("imploding"):
                continue
            started = float(parked.get("implode_started") or now)
            progress = max(0.0, min(1.0, (now - started) / 0.42))
            hud = parked.get("hud")
            visual = parked.get("visual")
            if isinstance(hud, MagneticOrderRailPanel):
                hud.set_implosion_progress(progress)
            if isinstance(visual, MagneticRailLineOverlay):
                visual.implosion_progress = progress
                visual.advance_animation(self._order_rail_phase)
                visual.update()
            parked["implode_progress"] = progress
            if progress >= 1.0:
                finished.append(parked)

        for parked in finished:
            if parked in self._parked_order_rails:
                self._parked_order_rails.remove(parked)
                self._mark_parked_order_rails_changed()
            for key in ("hud", "visual"):
                widget = parked.get(key)
                if isinstance(widget, QtWidgets.QWidget):
                    widget.hide()
                    widget.deleteLater()
        if finished:
            self._position_price_axis_focus_overlay()
        if self._order_rail_animation_required():
            record_performance_count("rail.animation_frames")
            self._schedule_order_rail_animation_frame()
        else:
            self._navigation_pending_rail = False

    @contextmanager
    def _overlay_geometry_transaction(self):
        """Share immutable geometry within one overlay commit, never across frames."""
        outermost = self._overlay_frame_geometry is None
        if outermost:
            self._overlay_frame_geometry = {}
        try:
            yield
        finally:
            if outermost:
                self._overlay_frame_geometry = None

    def _overlay_price_view_geometry(self):
        cache = self._overlay_frame_geometry
        if cache is not None and "price_view" in cache:
            return cache["price_view"]
        view = self.price_plot.getViewBox()
        scene_rect = view.sceneBoundingRect()
        if scene_rect.isEmpty():
            result = None
        else:
            top_left = self.graphics.mapFromScene(scene_rect.topLeft())
            bottom_right = self.graphics.mapFromScene(scene_rect.bottomRight())
            plot_rect = QtCore.QRectF(QtCore.QPointF(top_left), QtCore.QPointF(bottom_right)).normalized()
            result = (view, plot_rect, float(self.price_plot.viewRange()[0][0]))
        if cache is not None:
            cache["price_view"] = result
        return result

    def _order_rail_view_geometry(self, price: float | None = None) -> tuple[QtCore.QRectF, float] | None:
        """Return price-plot geometry in viewport logical pixels."""
        rail_price = self.order_rail_value if price is None else float(price)
        if rail_price is None or float(rail_price) <= 0.0:
            return None
        geometry = self._overlay_price_view_geometry()
        if geometry is None:
            return None
        view, plot_rect, x0 = geometry
        shown = chart_y(float(rail_price), self.logarithmic)
        scene_point = view.mapViewToScene(QtCore.QPointF(x0, shown))
        viewport_point = self.graphics.mapFromScene(scene_point)
        return plot_rect, float(viewport_point.y())

    def _order_rail_axis_view_rect(self) -> QtCore.QRectF | None:
        """Return the visible right price-axis strip in viewport logical pixels."""
        cache = self._overlay_frame_geometry
        if cache is not None and "axis_view" in cache:
            return cache["axis_view"]
        try:
            scene_rect = self.price_axis.sceneBoundingRect()
        except (AttributeError, RuntimeError):
            return None
        if scene_rect.isEmpty():
            return None
        top_left = self.graphics.mapFromScene(scene_rect.topLeft())
        bottom_right = self.graphics.mapFromScene(scene_rect.bottomRight())
        rect = QtCore.QRectF(QtCore.QPointF(top_left), QtCore.QPointF(bottom_right)).normalized()
        result = rect if not rect.isEmpty() else None
        if cache is not None:
            cache["axis_view"] = result
        return result

    def _native_price_axis_text_band(
        self,
        axis_rect: QtCore.QRectF | None = None,
    ) -> tuple[float, float] | None:
        """Return the canonical native right-axis text band in graphics coordinates.

        AxisItem local x == 0 is the price-axis plane. The native tick style then
        determines the text origin. Rail/current/cursor overlays all consume this
        one mapping so price-string width, axis reservation, DPI and panel layout
        changes cannot create independent horizontal anchors.
        """
        if axis_rect is None or axis_rect.isEmpty():
            axis_view = self._order_rail_axis_view_rect()
            if axis_view is not None and not axis_view.isEmpty():
                axis_rect = axis_view.translated(
                    QtCore.QPointF(self.graphics.viewport().pos())
                )
                axis_rect = axis_rect.intersected(QtCore.QRectF(self.graphics.rect()))

        if axis_rect is not None and not axis_rect.isEmpty():
            cache = self._overlay_frame_geometry
            cache_key = ("axis_text_band", axis_rect.x(), axis_rect.y(), axis_rect.width(), axis_rect.height())
            if cache is not None and cache_key in cache:
                return cache[cache_key]
            try:
                axis_line_scene = self.price_axis.mapToScene(QtCore.QPointF(0.0, 0.0))
                axis_line_view = self.graphics.mapFromScene(axis_line_scene)
                axis_line_x = float(
                    axis_line_view.x() + self.graphics.viewport().pos().x()
                )
                style = getattr(self.price_axis, "style", {})
                raw_offset = style.get("tickTextOffset", (5.0, 2.0))
                tick_text_offset = float(raw_offset[0])
                tick_length = float(style.get("tickLength", -5.0))
                text_left = (
                    axis_line_x
                    + max(0.0, tick_length)
                    + max(0.0, tick_text_offset)
                )
                text_left = max(
                    float(axis_rect.left()),
                    min(float(axis_rect.right()) - 1.0, text_left),
                )
                text_right = max(text_left + 1.0, float(axis_rect.right()) - 2.0)
                if all(math.isfinite(value) for value in (text_left, text_right)):
                    band = (text_left, text_right)
                    self._last_price_axis_text_band = band
                    if cache is not None:
                        cache[cache_key] = band
                    return band
            except (AttributeError, IndexError, RuntimeError, TypeError, ValueError):
                pass


        previous = self._last_price_axis_text_band
        if previous is not None:
            left, right = previous
            bounds = QtCore.QRectF(self.graphics.rect())
            if (
                math.isfinite(left)
                and math.isfinite(right)
                and right > left
                and right >= bounds.left()
                and left <= bounds.right()
            ):
                return previous
        return None

    def _overlay_order_rail_layout_geometry(self, plot_view: QtCore.QRectF):
        cache = self._overlay_frame_geometry
        if cache is not None and "rail_layout" in cache:
            return cache["rail_layout"]
        offset = QtCore.QPointF(self.graphics.viewport().pos())
        parent_bounds = QtCore.QRectF(self.graphics.rect())
        plot_rect = plot_view.translated(offset).intersected(parent_bounds)
        axis_view = self._order_rail_axis_view_rect()
        axis_rect = axis_view.translated(offset) if axis_view is not None else QtCore.QRectF()
        if not axis_rect.isEmpty():
            axis_rect = axis_rect.intersected(parent_bounds)
        if plot_rect.isEmpty():
            result = None
        else:
            axis_text_band = self._native_price_axis_text_band(
                axis_rect if not axis_rect.isEmpty() else None
            )
            axis_right = axis_rect.right() if not axis_rect.isEmpty() else (
                axis_text_band[1] if axis_text_band is not None else plot_rect.right()
            )
            hud_right = min(parent_bounds.right() - 1.0, max(plot_rect.right(), axis_right))
            result = (
                offset, parent_bounds, plot_rect, axis_rect, axis_text_band, hud_right,
                max(1, int(hud_right - plot_rect.left() - 3.0)),
                max(1, int(plot_rect.height()) - 6),
            )
        if cache is not None:
            cache["rail_layout"] = result
        return result


    def _position_order_rail_pair(
        self,
        price: float,
        hud: MagneticOrderRailPanel,
        visual: MagneticRailLineOverlay,
        *,
        dragging: bool = False,
        implosion_progress: float = 0.0,
    ) -> None:
        rail_price_text = self._order_rail_price_text_for(float(price))
        if self._reserve_price_axis_for_text(rail_price_text):


            QTimer.singleShot(0, self, self._position_order_rail_hud)
        geometry = self._order_rail_view_geometry(price)
        if geometry is None:
            visual.hide()
            hud.hide()
            return
        plot_view, line_y_view = geometry
        layout_geometry = self._overlay_order_rail_layout_geometry(plot_view)
        if layout_geometry is None:
            visual.hide()
            hud.hide()
            return
        offset, parent_bounds, plot_rect, axis_rect, axis_text_band, hud_right, available_width, available_height = layout_geometry
        line_y = line_y_view + offset.y()
        desired = hud.desired_size(available_width, available_height)
        if hud.size() != desired:
            hud.resize(desired)


        original_compact_width = 164.0
        connection_right = (
            plot_rect.right()
            - max(0.0, float(desired.width()) - original_compact_width)
        )
        visually_armed = bool(hud.armed or hud.arm_submission_preview_active())
        if visually_armed:
            connection_right = plot_rect.right()
        connection_right = max(
            plot_rect.left() + 1.0,
            min(plot_rect.right(), connection_right),
        )

        glow = (
            10.0
            + self.order_rail_config["glow_strength"] * 0.08
            + self.order_rail_config["canvas_bloom"] * 0.16
            + self.order_rail_config["spark_spread"] * 0.55
            + self.order_rail_config["line_width"] * 2.0
        )
        band_half = max(13.0, min(64.0, glow))
        band_top = max(plot_rect.top(), line_y - band_half)
        band_bottom = min(plot_rect.bottom(), line_y + band_half)
        if band_bottom > band_top and plot_rect.top() <= line_y <= plot_rect.bottom():
            band = QtCore.QRect(
                int(math.floor(plot_rect.left())),
                int(math.floor(band_top)),
                max(1, int(math.ceil(connection_right - plot_rect.left()))),
                max(1, int(math.ceil(band_bottom - band_top))),
            )
            if visual.geometry() != band:
                visual.setGeometry(band)
            visual_config = self.order_rail_config
            if hud.line_pattern_override != "inherit":
                visual_config = dict(self.order_rail_config)
                visual_config["line_pattern"] = hud.line_pattern_override
            visual.working_hud = hud
            visual.set_state(
                theme=self.theme,
                config=visual_config,
                line_y=line_y - band.top(),
                color=self._order_rail_visual_color_for_hud(hud),
                phase=self._order_rail_phase,
                dragging=dragging,
                armed=visually_armed,
                side=str(hud.side),
                arm_progress=hud.arm_visual_progress(),
                implosion_progress=implosion_progress,
            )
            if not visual.isVisible():
                visual.show()
            visual.raise_()
        else:
            visual.hide()

        x = int(round(hud_right - desired.width()))
        min_x = int(math.floor(plot_rect.left() + 3.0))
        max_x = max(min_x, int(math.floor(parent_bounds.right() - desired.width() - 1.0)))
        x = max(min_x, min(max_x, x))

        if axis_text_band is not None:
            axis_text_left, axis_text_right = axis_text_band
            hud.set_axis_text_band(
                axis_text_left - float(x),
                max(1.0, axis_text_right - axis_text_left),
                axis_left=(float(axis_rect.left()) - float(x)) if not axis_rect.isEmpty() else None,
            )
        else:
            hud.set_axis_text_band(None, axis_left=None)

        visible_anchor = max(plot_rect.top(), min(plot_rect.bottom(), line_y))
        offscreen = -1 if line_y < plot_rect.top() else 1 if line_y > plot_rect.bottom() else 0
        anchor = hud.expansion_anchor()
        target_top = (
            visible_anchor
            - desired.height() * anchor
            + int(self.order_rail_config["vertical_offset"])
        )
        min_top = int(math.ceil(plot_rect.top() + 3.0))
        max_top = max(min_top, int(math.floor(plot_rect.bottom() - desired.height() - 3.0)))
        y = max(min_top, min(max_top, int(round(target_top))))
        target = QtCore.QPoint(x, y)
        if hud.pos() != target:
            hud.move(target)
        hud.set_line_anchor(visible_anchor - y, offscreen)

        hud.set_price(float(price), rail_price_text)
        if not hud.isVisible():
            hud.show()
        hud.raise_()

    def _position_order_rail_hud(self) -> None:
        with self._overlay_geometry_transaction():
            self._position_order_rail_hud_commit()

    def _position_order_rail_hud_commit(self) -> None:
        active_visible = (self.working_orders_visible or not (self.order_rail_hud and self.order_rail_hud.armed)) and (
            not self._active_order_rail_symbol
            or not self.order_rail_market_symbol
            or self._active_order_rail_symbol == self.order_rail_market_symbol
        )
        if (
            active_visible
            and self.order_rail_value is not None
            and self.order_rail_hud is not None
            and self.order_rail_visual is not None
        ):
            self._position_order_rail_pair(
                float(self.order_rail_value),
                self.order_rail_hud,
                self.order_rail_visual,
                dragging=self._order_rail_dragging,
            )
        elif self.order_rail_hud is not None and self.order_rail_visual is not None:
            self.order_rail_hud.hide()
            self.order_rail_visual.hide()
        for parked in self._active_parked_order_rails():
            hud = parked.get("hud")
            visual = parked.get("visual")
            price = safe_float(parked.get("price"))
            parked_symbol = str(parked.get("symbol") or "")
            if not isinstance(hud, MagneticOrderRailPanel) or not isinstance(visual, MagneticRailLineOverlay) or price <= 0:
                continue
            if not self.working_orders_visible or (self.order_rail_market_symbol and parked_symbol != self.order_rail_market_symbol):
                hud.hide()
                visual.hide()
                continue
            self._position_order_rail_pair(
                price,
                hud,
                visual,
                dragging=False,
                implosion_progress=float(parked.get("implode_progress") or 0.0),
            )
        self._position_price_axis_focus_overlay()

    def _order_rail_hud_viewport_position(self, global_position: object) -> QtCore.QPointF:
        """Map a HUD mouse position into the chart viewport drag coordinate space."""
        viewport = self.graphics.viewport()
        try:
            point = global_position.toPoint()
        except AttributeError:
            point = QtCore.QPoint()
        return QtCore.QPointF(viewport.mapFromGlobal(point))

    def _order_rail_hud_drag_started(self, global_position: object) -> None:
        if self.order_rail_value is None or self._order_rail_dragging:
            return
        self._begin_interaction_priority()
        self._order_rail_dragging = True
        self._order_rail_drag_start_value = self.order_rail_value


        self._set_crosshair_visible(False)


        self._order_rail_pending_drag_position = None
        if self.order_rail_hud is not None:
            self.order_rail_hud.set_external_dragging(True)
        self.graphics.viewport().setFocus(Qt.FocusReason.MouseFocusReason)
        self._order_rail_last_frame = time.monotonic()
        self._schedule_order_rail_animation_frame()

    def _order_rail_hud_drag_moved(self, global_position: object) -> None:
        if not self._order_rail_dragging:
            return
        self._hold_interaction_priority()
        position = self._order_rail_hud_viewport_position(global_position)

        self._order_rail_pending_drag_position = QtCore.QPointF(position)
        self._schedule_order_rail_animation_frame()

    def _order_rail_hud_drag_finished(self, commit: bool) -> None:
        if not self._order_rail_dragging:
            return
        self._schedule_interaction_priority_release()
        if commit:
            pending = self._order_rail_pending_drag_position
            if pending is not None:
                self._order_rail_pending_drag_position = None
                self._move_order_rail_from_viewport(pending)
            if self.order_rail_hud is not None and self.order_rail_hud.armed:
                proposed = self.order_rail_value
                original = self._order_rail_drag_start_value
                order = dict(self._active_order_rail_working_order)
                if order and proposed is not None and original is not None and proposed != original:
                    self._active_order_rail_amend_origin = float(original)
                    self._active_order_rail_amend_price = float(proposed)
                    self._finish_order_rail_drag()
                    self.working_order_moved.emit({**order, "newPrice": proposed,
                        "_railChart": self, "_railDraftId": self._active_order_rail_draft_id})
                else:
                    self._cancel_order_rail_drag()
            else:
                self._finish_order_rail_drag()
        else:
            self._cancel_order_rail_drag()

    def _move_order_rail_from_viewport(self, position: QtCore.QPointF) -> bool:
        if self.order_rail_value is None:
            return False
        view = self.price_plot.getViewBox()
        scene_position = self.graphics.mapToScene(position.toPoint())
        view_scene = view.sceneBoundingRect()


        if not (view_scene.top() <= scene_position.y() <= view_scene.bottom()):
            return False
        mapped_scene = QtCore.QPointF(view_scene.center().x(), scene_position.y())
        point = view.mapSceneToView(mapped_scene)
        raw = self._snap_order_rail_price(
            raw_price(float(point.y()), self.logarithmic)
        )
        if raw <= 0:
            return False
        self.order_rail_value = raw
        self._position_order_rail_hud()
        return True

    def _release_order_rail_mouse(self) -> None:
        try:
            viewport = self.graphics.viewport()
            if QtWidgets.QWidget.mouseGrabber() is viewport:
                viewport.releaseMouse()
        except RuntimeError:
            pass

    def _finish_order_rail_drag(self) -> None:
        if not self._order_rail_dragging:
            return
        self._order_rail_dragging = False
        self._order_rail_pending_drag_position = None
        self._release_order_rail_mouse()
        if self.order_rail_hud is not None:
            self.order_rail_hud.set_external_dragging(False)
            self.order_rail_hud.flash_commit()
        self._order_rail_drag_start_value = None
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def _cancel_order_rail_drag(self, *, restore: bool = True) -> None:
        if not self._order_rail_dragging:
            return
        if restore and self._order_rail_drag_start_value is not None:
            self.order_rail_value = self._order_rail_drag_start_value
        self._order_rail_dragging = False
        self._order_rail_pending_drag_position = None
        self._order_rail_drag_start_value = None
        self._release_order_rail_mouse()
        if self.order_rail_hud is not None:
            self.order_rail_hud.set_external_dragging(False)
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def _park_current_order_rail(self, *, reconcile: bool = True) -> bool:
        hud = self.order_rail_hud
        visual = self.order_rail_visual
        price = self.order_rail_raw_price()
        if (
            hud is None
            or visual is None
            or price <= 0.0
            or not hud.armed
        ):
            return False
        if self._order_rail_dragging:
            self._finish_order_rail_drag()

        state = self.magnetic_order_rail_state()
        hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        hud.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        hud.set_implosion_progress(0.0)

        self._parked_order_rail_serial += 1
        parked = {
            "id": self._parked_order_rail_serial,
            "draft_id": int(self._active_order_rail_draft_id),
            "symbol": str(self._active_order_rail_symbol or self.order_rail_market_symbol),
            "price": float(price),
            "state": dict(state),
            "hud": hud,
            "visual": visual,
            "working_key": str(self._active_order_rail_working_key or ""),
            "working_order": dict(self._active_order_rail_working_order),
            "matched_once": bool(self._active_order_rail_matched_once),
            "amend_origin": self._active_order_rail_amend_origin,
            "amend_price": self._active_order_rail_amend_price,
            "imploding": False,
            "implode_started": 0.0,
            "implode_progress": 0.0,
        }
        self._parked_order_rails.append(parked)
        self._bind_working_rail_interaction(parked)
        self._mark_parked_order_rails_changed()
        parked_draft_id = int(parked["draft_id"])
        try:
            hud.remove_requested.disconnect()
        except (RuntimeError, TypeError):
            pass
        hud.remove_requested.connect(
            lambda expected=parked_draft_id: self._request_parked_order_rail_removal(expected)
        )
        self.order_rail_value = None
        self.order_rail_hud = None
        self.order_rail_visual = None
        self._active_order_rail_symbol = ""
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order = {}
        self._active_order_rail_matched_once = False
        self._active_order_rail_amend_origin = None
        self._active_order_rail_amend_price = None
        self._order_rail_pending_drag_position = None
        if reconcile:
            self._sync_parked_order_rails_with_working_orders(self.working_order_payloads)
        self._position_order_rail_hud()
        return True

    @staticmethod
    def _working_order_visual_key(order: dict[str, Any], index: int) -> str:
        source = str(order.get("_source") or "STANDARD").upper()
        if source == "ALGO":
            identifier = (
                order.get("algoId")
                or order.get("clientAlgoId")
                or order.get("orderId")
                or order.get("clientOrderId")
                or f"row-{index}"
            )
        else:
            identifier = (
                order.get("orderId")
                or order.get("clientOrderId")
                or order.get("algoId")
                or order.get("clientAlgoId")
                or f"row-{index}"
            )
        return f"{source}:{identifier}"

    @staticmethod
    def _working_order_visual_price(order: dict[str, Any]) -> float:
        kind = str(order.get("type") or order.get("orderType") or "").upper()
        keys = ("activatePrice", "activationPrice") if kind == "TRAILING_STOP_MARKET" else ("triggerPrice", "stopPrice", "price")
        for key in keys:
            value = safe_float(order.get(key))
            if value > 0:
                return value
        return 0.0

    def _parked_rail_matches_order(
        self, parked: dict[str, Any], order: dict[str, Any]
    ) -> bool:
        state = parked.get("state")
        if not isinstance(state, dict):
            return False
        side = str(order.get("side") or "").upper()
        if side and side != str(state.get("side") or "").upper():
            return False

        expected_type = str(state.get("orderType") or "").upper()
        actual_type = str(
            order.get("type")
            or order.get("orderType")
            or order.get("algoType")
            or ""
        ).upper()
        if expected_type and actual_type and expected_type != actual_type:
            return False

        expected_price = safe_float(
            state.get("triggerPrice")
            or state.get("activatePrice")
            or state.get("price")
            or state.get("railPrice")
        )
        actual_price = self._working_order_visual_price(order)
        if expected_price <= 0.0 or actual_price <= 0.0:
            return False
        tolerance = max(
            1e-9,
            abs(expected_price) * 1e-8,
            abs(float(self.order_rail_tick_size)) * 0.51,
        )
        return abs(expected_price - actual_price) <= tolerance

    @staticmethod
    def _order_rail_reference_matches_order(
        reference: dict[str, Any], order: dict[str, Any]
    ) -> bool:
        source = str(reference.get("_source") or "").upper()
        symbol = str(reference.get("symbol") or "").upper()
        if symbol and str(order.get("symbol") or "").upper() != symbol:
            return False
        if source and str(order.get("_source") or "").upper() != source:
            return False
        reference_ids = {
            str(value)
            for value in (
                reference.get("orderId"),
                reference.get("algoId"),
                reference.get("clientOrderId"),
                reference.get("clientAlgoId"),
                reference.get("origClientOrderId"),
            )
            if value not in (None, "")
        }
        if not reference_ids:
            return False
        order_ids = {
            str(value)
            for value in (
                order.get("orderId"),
                order.get("algoId"),
                order.get("clientOrderId"),
                order.get("clientAlgoId"),
                order.get("origClientOrderId"),
            )
            if value not in (None, "")
        }
        return bool(reference_ids & order_ids)

    @staticmethod
    def _order_rail_reference_has_identity(reference: dict[str, Any]) -> bool:
        return any(
            reference.get(key) not in (None, "")
            for key in (
                "orderId",
                "algoId",
                "clientOrderId",
                "clientAlgoId",
                "origClientOrderId",
            )
        )

    def _start_active_order_rail_implosion(self) -> None:
        """Detach the active live rail and run the canonical cancel implosion."""
        draft_id = int(self._active_order_rail_draft_id)
        if draft_id <= 0 or self.order_rail_hud is None:
            self._remove_active_order_rail()
            return
        if not self._park_current_order_rail(reconcile=False):
            self._remove_active_order_rail()
            return
        for parked in self._parked_order_rails:
            if int(parked.get("draft_id") or 0) == draft_id:
                self._start_parked_order_rail_implosion(parked)
                return

    def _start_parked_order_rail_implosion(self, parked: dict[str, Any]) -> None:
        if bool(parked.get("imploding")):
            return
        enabled = bool(self.order_rail_config.get("cancel_implosion_enabled", True))
        parked["imploding"] = True
        parked["implode_started"] = time.monotonic() if enabled else time.monotonic() - 0.42
        parked["implode_progress"] = 0.0 if enabled else 1.0
        hud = parked.get("hud")
        visual = parked.get("visual")
        if isinstance(hud, MagneticOrderRailPanel):
            hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            hud.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            hud.set_implosion_progress(0.0 if enabled else 1.0)
        if isinstance(visual, MagneticRailLineOverlay):
            visual.implosion_progress = 0.0 if enabled else 1.0
            visual.update()
        self._schedule_order_rail_animation_frame()

    @staticmethod
    def _is_magnetic_rail_working_order(order: dict[str, Any]) -> bool:
        """Every live exchange order is eligible, regardless of its origin."""
        status = str(order.get("status") or order.get("algoStatus") or "NEW").upper()
        return status not in {"FILLED", "FINISHED", "CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}

    def _create_parked_order_rail_from_working_order(
        self, order: dict[str, Any], working_key: str
    ) -> dict[str, Any] | None:
        """Rehydrate one armed rail from an authoritative Binance open order."""
        price = self._working_order_visual_price(order)
        symbol = str(order.get("symbol") or self.order_rail_market_symbol).upper()
        if price <= 0.0 or not symbol:
            return None
        self._order_rail_draft_serial += 1
        draft_id = self._order_rail_draft_serial
        self._parked_order_rail_serial += 1
        hud = MagneticOrderRailPanel(self.theme, self.graphics)
        self._exclude_chart_pointer_for_widget(hud)
        hud.set_config(self.order_rail_config)
        hud.set_active_contract_target(
            True, animated=False
        )
        preset = dict(self.order_rail_order_preset)
        preset["orderType"] = str(
            order.get("type") or order.get("orderType") or order.get("algoType") or "LIMIT"
        ).upper()
        preset["timeInForce"] = str(order.get("timeInForce") or "GTC").upper()
        preset["workingType"] = str(order.get("workingType") or "CONTRACT_PRICE").upper()
        preset["priceProtect"] = bool(order.get("priceProtect", True))
        hud.apply_order_preset(
            normalized_order_rail_order_preset(preset),
            side=str(order.get("side") or "BUY").upper(),
            preset_name="BINANCE RAIL",
        )
        hud.reduce_only = bool(order.get("reduceOnly") or order.get("closePosition"))
        hud.take_profit_enabled = False
        hud.stop_loss_enabled = False
        hud.set_price(float(price), self._order_rail_price_text_for(float(price)))
        hud.set_armed(True, animated=False)
        hud.set_active_contract_target(
            True, animated=False
        )
        visual = MagneticRailLineOverlay(self.graphics)
        self._exclude_chart_pointer_for_widget(visual)
        state = {
            "railDraftId": draft_id,
            "symbol": symbol,
            "railPrice": float(price),
            "priceText": self._order_rail_price_text_for(float(price)),
            "tickSize": self.order_rail_tick_text,
            "orderType": preset["orderType"],
            "presetName": "BINANCE RAIL",
            "armed": True,
            "side": hud.side,
            "reduceOnly": bool(hud.reduce_only),
            "sizePercent": 100,
            "leverage": int(hud.leverage),
            "orderRole": "ENTRY",
            "takeProfitEnabled": False,
            "stopLossEnabled": False,
            "positionIntent": "REDUCE" if hud.reduce_only else "OPEN",
            "timeInForce": hud.time_in_force,
            "workingType": hud.working_type,
            "priceProtect": hud.price_protect,
            "workingOrder": dict(order),
        }
        parked = {
            "id": self._parked_order_rail_serial,
            "draft_id": draft_id,
            "symbol": symbol,
            "price": float(price),
            "state": state,
            "hud": hud,
            "visual": visual,
            "working_key": str(working_key),
            "working_order": dict(order),
            "matched_once": True,
            "imploding": False,
            "implode_started": 0.0,
            "implode_progress": 0.0,
        }
        hud.remove_requested.connect(
            lambda expected=draft_id: self._request_parked_order_rail_removal(expected)
        )
        self._parked_order_rails.append(parked)
        self._bind_working_rail_interaction(parked)
        self._mark_parked_order_rails_changed()
        return parked

    def _update_working_rail(self, parked: dict[str, Any], order: dict[str, Any]) -> None:
        parked["working_order"] = dict(order)
        parked["state"]["workingOrder"] = dict(order)
        parked["hud"].working_order = dict(order)
        authoritative_price = self._working_order_visual_price(order)
        amend_price = parked.get("amend_price")
        if amend_price is not None:
            tolerance = max(
                1e-9,
                abs(float(amend_price)) * 1e-8,
                abs(float(self.order_rail_tick_size)) * 0.51,
            )
            if abs(authoritative_price - float(amend_price)) <= tolerance:
                parked.pop("amend_origin", None)
                parked.pop("amend_price", None)
                parked["price"] = authoritative_price
                parked["state"]["railPrice"] = authoritative_price
        elif "drag_origin" not in parked:
            parked["price"] = authoritative_price
            parked["state"]["railPrice"] = parked["price"]

    def _bind_working_rail_interaction(self, parked: dict[str, Any]) -> None:
        hud = parked["hud"]
        hud.working_order = dict(parked.get("working_order") or {})
        if hud is self.order_rail_hud:
            for signal in (hud.body_drag_started, hud.body_drag_moved, hud.body_drag_finished):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
        hud.body_drag_started.connect(lambda pos, row=parked: self._working_rail_drag_start(row, pos))
        hud.body_drag_moved.connect(lambda pos, row=parked: self._working_rail_drag_move(row, pos))
        hud.body_drag_finished.connect(lambda commit, row=parked: self._working_rail_drag_finish(row, commit))

    def _working_rail_drag_start(self, row: dict[str, Any], position: object) -> None:
        if row.get("imploding") or not row.get("working_order"):
            return
        row["drag_origin"] = row["price"]
        row["drag_start_global"] = QtCore.QPointF(position)
        self._begin_interaction_priority()

    def _working_rail_drag_move(self, row: dict[str, Any], position: object) -> None:
        if "drag_origin" not in row:
            return
        view = self.price_plot.getViewBox()
        start = self._order_rail_hud_viewport_position(row["drag_start_global"])
        current = self._order_rail_hud_viewport_position(position)
        start_scene = self.graphics.mapToScene(start.toPoint())
        current_scene = self.graphics.mapToScene(current.toPoint())
        delta = view.mapSceneToView(current_scene).y() - view.mapSceneToView(start_scene).y()
        proposed = self._snap_order_rail_price(raw_price(chart_y(row["drag_origin"], self.logarithmic) + delta, self.logarithmic))
        if math.isfinite(proposed) and proposed > 0:
            row["price"] = proposed
            self._position_order_rail_hud()

    def _working_rail_drag_finish(self, row: dict[str, Any], commit: bool) -> None:
        if "drag_origin" not in row:
            return
        proposed = row["price"]
        original = row.pop("drag_origin")
        row.pop("drag_start_global", None)
        self._schedule_interaction_priority_release()
        if commit and proposed != original and not row.get("imploding"):
            row["amend_origin"] = float(original)
            row["amend_price"] = float(proposed)
            row["state"]["railPrice"] = float(proposed)
            self._position_order_rail_hud()
            self.working_order_moved.emit({**row["working_order"], "newPrice": proposed,
                "_railChart": self, "_railDraftId": row["draft_id"]})
        else:
            row["price"] = float(original)
            row["state"]["railPrice"] = float(original)
            self._position_order_rail_hud()

    def resolve_rail_amendment(self, draft_id: int, *, accepted: bool) -> None:
        """Commit an optimistic drag or restore its exact pre-drag price."""
        draft_id = int(draft_id)
        if draft_id == int(self._active_order_rail_draft_id):
            origin = self._active_order_rail_amend_origin
            proposed = self._active_order_rail_amend_price
            if origin is not None and proposed is not None:
                if accepted:
                    self.order_rail_value = float(proposed)
                else:
                    self.order_rail_value = float(origin)
                    self._active_order_rail_amend_origin = None
                    self._active_order_rail_amend_price = None
                self._position_order_rail_hud()
            return
        for row in self._parked_order_rails:
            if int(row.get("draft_id") or 0) != draft_id:
                continue
            origin = row.get("amend_origin")
            proposed = row.get("amend_price")
            if origin is None or proposed is None:
                return
            if accepted:
                row["price"] = float(proposed)
                row["state"]["railPrice"] = float(proposed)
            else:
                row["price"] = float(origin)
                row["state"]["railPrice"] = float(origin)
                row.pop("amend_origin", None)
                row.pop("amend_price", None)
            self._position_order_rail_hud()
            return

    def set_rail_amend_pending(self, draft_id: int, pending: bool) -> None:
        hud = self.order_rail_hud if draft_id == self._active_order_rail_draft_id else None
        for row in self._active_parked_order_rails():
            if row["draft_id"] == draft_id:
                hud = row["hud"]
        if hud is not None:
            hud.set_submission_pending(pending)
            hud.setToolTip("Amendment pending — awaiting exchange" if pending else "Click for details · drag to amend")

    def _discard_parked_order_rail_presentations(self) -> None:
        """Drop off-market rail widgets without changing any exchange order."""
        for parked in self._parked_order_rails:
            for key in ("hud", "visual"):
                widget = parked.get(key)
                if isinstance(widget, QtWidgets.QWidget):
                    widget.hide()
                    widget.deleteLater()
        self._parked_order_rails.clear()
        self._mark_parked_order_rails_changed()

    def set_order_rail_market_symbol(self, symbol: str) -> None:
        """Switch rail presentation without deleting exchange-backed armed rails.

        Binance/account open-order state is authoritative persistence. Existing
        parked widgets are presentation caches only, so they are discarded when
        leaving a ticker and reconstructed from the cached/next snapshot if the
        corresponding `nwr_…` order is still open when the symbol is shown again.
        """
        symbol = str(symbol or "").upper().strip()
        if symbol == self.order_rail_market_symbol:
            return
        active_hud = self.order_rail_hud
        if active_hud is not None and active_hud.armed:
            self._park_current_order_rail()
        elif (
            active_hud is not None
            and not active_hud.submission_pending
            and not active_hud.cancellation_pending
        ):


            self._remove_active_order_rail()
        self._discard_parked_order_rail_presentations()
        self.order_rail_market_symbol = symbol
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def _sync_parked_order_rails_with_working_orders(
        self, orders: list[dict[str, Any]]
    ) -> None:
        current_symbol = str(self.order_rail_market_symbol or "").upper()
        keyed = {
            self._working_order_visual_key(order, index): dict(order)
            for index, order in enumerate(orders)
            if isinstance(order, dict)
            and self._is_magnetic_rail_working_order(order)
            and (not current_symbol or str(order.get("symbol") or "").upper() == current_symbol)
        }


        active_parked = self._active_parked_order_rails()
        known_identity_keys = {
            str(parked.get("working_key") or "")
            for parked in active_parked
        }
        if (
            self._active_order_rail_symbol == current_symbol
            and self._active_order_rail_working_key
        ):
            known_identity_keys.add(self._active_order_rail_working_key)

        reserved = {
            str(parked.get("working_key") or "")
            for parked in active_parked
            if parked.get("working_key")
        }
        if (
            self._active_order_rail_working_key
            and (not current_symbol or self._active_order_rail_symbol == current_symbol)
        ):
            reserved.add(self._active_order_rail_working_key)

        active_hud = self.order_rail_hud
        if (
            active_hud is not None
            and active_hud.armed
            and (not current_symbol or self._active_order_rail_symbol == current_symbol)
        ):
            if self._active_order_rail_working_key:
                if (
                    self._active_order_rail_matched_once
                    and self._active_order_rail_working_key not in keyed
                ):


                    if active_hud.cancellation_pending:
                        self._start_active_order_rail_implosion()
                    else:
                        self._remove_active_order_rail()
                    active_hud = None
            else:
                active_state = self.magnetic_order_rail_state()
                probe = {"state": active_state}
                reference = dict(self._active_order_rail_working_order)
                for key, order in keyed.items():
                    if key in reserved:
                        continue
                    has_identity = self._order_rail_reference_has_identity(reference)
                    if (
                        has_identity
                        and self._order_rail_reference_matches_order(reference, order)
                    ) or (
                        not has_identity
                        and self._parked_rail_matches_order(probe, order)
                    ):
                        self._active_order_rail_working_key = key
                        self._active_order_rail_working_order = dict(order)
                        self._active_order_rail_matched_once = True
                        reserved.add(key)
                        break

        if active_hud is not None and self._active_order_rail_working_key in keyed:
            current = keyed[self._active_order_rail_working_key]
            self._active_order_rail_working_order = dict(current)
            active_hud.working_order = dict(current)
            authoritative_price = self._working_order_visual_price(current)
            if self._active_order_rail_amend_price is not None:
                tolerance = max(
                    1e-9,
                    abs(float(self._active_order_rail_amend_price)) * 1e-8,
                    abs(float(self.order_rail_tick_size)) * 0.51,
                )
                if abs(authoritative_price - float(self._active_order_rail_amend_price)) <= tolerance:
                    self._active_order_rail_amend_origin = None
                    self._active_order_rail_amend_price = None
                    if not self._order_rail_dragging:
                        self.order_rail_value = authoritative_price
            elif not self._order_rail_dragging:
                self.order_rail_value = authoritative_price

        for parked in active_parked:
            if parked.get("imploding"):
                continue
            bound = str(parked.get("working_key") or "")
            if bound:
                if bound in keyed:
                    self._update_working_rail(parked, keyed[bound])
                if bound not in keyed:
                    self._start_parked_order_rail_implosion(parked)
                continue
            reference = parked.get("working_order")
            if not isinstance(reference, dict):
                reference = {}
            for key, order in keyed.items():
                if key in reserved:
                    continue
                has_identity = self._order_rail_reference_has_identity(reference)
                if (
                    has_identity
                    and self._order_rail_reference_matches_order(reference, order)
                ) or (
                    not has_identity
                    and self._parked_rail_matches_order(parked, order)
                ):
                    parked["working_key"] = key
                    parked["working_order"] = dict(order)
                    parked["matched_once"] = True
                    reserved.add(key)
                    break

        known_identity_keys = {str(p.get("working_key") or "") for p in self._active_parked_order_rails()}
        if self._active_order_rail_symbol == current_symbol and self._active_order_rail_working_key:
            known_identity_keys.add(self._active_order_rail_working_key)
        for key, order in keyed.items():
            if key in known_identity_keys or not self._is_magnetic_rail_working_order(order):
                continue
            parked = self._create_parked_order_rail_from_working_order(order, key)
            if parked is not None:
                known_identity_keys.add(key)
        self._position_order_rail_hud()
        self._schedule_order_rail_animation_frame()

    def _place_order_rail(self, shown_price: float, *, side: str | None = None, preset: dict[str, Any] | None = None, preset_name: str = "") -> None:
        shown_price=float(shown_price); price=self._snap_order_rail_price(raw_price(shown_price,self.logarithmic))
        if price<=0: return
        if self.order_rail_hud is not None and self.order_rail_hud.submission_pending:
            return
        if self.order_rail_hud is not None and self.order_rail_hud.armed:
            self._park_current_order_rail()
        self._order_rail_draft_serial += 1
        self._active_order_rail_draft_id = self._order_rail_draft_serial
        self._active_order_rail_symbol = str(self.order_rail_market_symbol)
        self._active_order_rail_working_key = ""
        self._active_order_rail_working_order = {}
        self._active_order_rail_matched_once = False
        self._active_order_rail_amend_origin = None
        self._active_order_rail_amend_price = None
        self.order_rail_value=price
        if self.order_rail_visual is None:
            self.order_rail_visual = MagneticRailLineOverlay(self.graphics)
            self._exclude_chart_pointer_for_widget(self.order_rail_visual)
        if self.order_rail_hud is None:
            self.order_rail_hud = MagneticOrderRailPanel(self.theme, self.graphics)
            self._exclude_chart_pointer_for_widget(self.order_rail_hud)
            self.order_rail_hud.set_config(self.order_rail_config)
            self.order_rail_hud.remove_requested.connect(self._request_active_order_rail_removal)
            self.order_rail_hud.state_changed.connect(self._order_rail_hud_state_changed)
            self.order_rail_hud.geometry_changed.connect(
                self._active_order_rail_geometry_changed
            )
            self.order_rail_hud.body_drag_started.connect(self._order_rail_hud_drag_started)
            self.order_rail_hud.body_drag_moved.connect(self._order_rail_hud_drag_moved)
            self.order_rail_hud.body_drag_finished.connect(self._order_rail_hud_drag_finished)
            self.order_rail_hud.execution_requested.connect(self._order_rail_execute_from_hud)
            self.order_rail_hud.leverage_requested.connect(self.order_rail_leverage_requested.emit)
        self.order_rail_hud.set_active_contract_target(
            True,
            animated=False,
        )
        ep=normalized_order_rail_order_preset(preset) if isinstance(preset,dict) else dict(self.order_rail_order_preset); es=str(side).upper() if side is not None else str(self.order_rail_hud.side); self.order_rail_hud.apply_order_preset(ep,side=es,preset_name=preset_name or self.order_rail_order_preset_name)
        if self.order_rail_hud.armed:
            self.order_rail_hud.set_armed(False, animated=False)
        self.order_rail_visual.raise_(); self.order_rail_hud.raise_(); self._position_order_rail_hud(); self._schedule_order_rail_animation_frame()
        if self.order_rail_placement_mode: self.set_order_rail_placement(False)

    def set_working_orders(
        self,
        orders: list[dict[str, Any]],
        *,
        visible: bool | None = None,
        authoritative: bool = True,
    ) -> None:
        """Track working orders and diff their standard chart-line presentation."""
        if visible is not None:
            self.working_orders_visible = bool(visible)
        self.working_order_payloads = [dict(order) for order in orders]
        if authoritative:
            self._sync_parked_order_rails_with_working_orders(self.working_order_payloads)

        magnetic_bound_keys = {
            str(parked.get("working_key") or "")
            for parked in self._active_parked_order_rails()
            if parked.get("working_key") and not parked.get("imploding")
        }
        if (
            self._active_order_rail_working_key
            and (
                not self.order_rail_market_symbol
                or self._active_order_rail_symbol == self.order_rail_market_symbol
            )
        ):
            magnetic_bound_keys.add(self._active_order_rail_working_key)

        desired: dict[str, tuple[dict[str, Any], tuple[Any, ...]]] = {}
        if self.working_orders_visible:
            for index, order in enumerate(self.working_order_payloads):
                visual_key = self._working_order_visual_key(order, index)
                if visual_key in magnetic_bound_keys:
                    continue
                price = self._working_order_visual_price(order)
                if price <= 0 or not str(order.get("symbol", "")):
                    continue
                side = str(order.get("side", "BUY"))
                source = str(order.get("_source", "STANDARD"))
                order_id = str(order.get("orderId") or order.get("algoId") or visual_key)
                movable = source == "STANDARD" and str(order.get("type")) == "LIMIT"
                color = self.theme["green"] if side == "BUY" else self.theme["red"]
                signature = (
                    float(price), side, source, str(order.get("type", "")),
                    bool(movable), str(color), bool(self.logarithmic), visual_key,
                )
                desired[order_id] = (dict(order), signature)

        for order_id in tuple(self.working_order_lines):
            if order_id in desired and self._working_order_line_signatures.get(order_id) == desired[order_id][1]:
                continue
            line = self.working_order_lines.pop(order_id)
            self._working_order_line_signatures.pop(order_id, None)
            self.price_plot.removeItem(line)

        if not self.working_orders_visible:
            return

        for order_id, (order, signature) in desired.items():
            if order_id in self.working_order_lines:
                continue
            price, side, source, order_type, movable, color, _logarithmic, _visual_key = signature
            line = pg.InfiniteLine(
                pos=chart_y(float(price), self.logarithmic),
                angle=0,
                movable=bool(movable),
                pen=pg.mkPen(color, width=1.2, style=Qt.PenStyle.DashLine),
                hoverPen=pg.mkPen(self.theme["cyan"], width=1.8),
            )
            line.setZValue(34)
            line.setToolTip(
                f"{source} {side} {order_type} · {format_price(float(price))}\n"
                + (
                    "Drag to modify price immediately while trading is armed."
                    if movable
                    else "Conditional Algo lines are display-only; cancel and replace to change them."
                )
            )
            if movable:
                line.sigPositionChangeFinished.connect(
                    lambda *_args, line_ref=line, payload=dict(order): self.working_order_moved.emit(
                        {**payload, "newPrice": raw_price(float(line_ref.value()), self.logarithmic)}
                    )
                )
            self.price_plot.addItem(line, ignoreBounds=True)
            self.working_order_lines[order_id] = line
            self._working_order_line_signatures[order_id] = signature

    def _clear_drawing_graphics(self, mode: str | None) -> None:
        if mode not in {"ruler", "fibonacci", "horizontal"}:
            return
        if mode == "horizontal":
            for line, label in self.horizontal_graphics:
                self.price_plot.removeItem(line)
                self.price_plot.removeItem(label)
            self.horizontal_graphics.clear()
            return
        items = self.ruler_graphics if mode == "ruler" else self.fibonacci_graphics
        for item in items:
            self.price_plot.removeItem(item)
        items.clear()

    def _render_drawing(self, start: QtCore.QPointF, end: QtCore.QPointF) -> None:
        if self.drawing_mode == "ruler":
            self._render_ruler(start, end)
        elif self.drawing_mode == "fibonacci":
            self._render_fibonacci(start, end)

    def _add_horizontal_line(self, shown_price: float) -> None:
        price = raw_price(shown_price, self.logarithmic)
        line = pg.InfiniteLine(
            pos=shown_price,
            angle=0,
            movable=True,
            pen=pg.mkPen(self.theme["cyan"], width=1.2),
            hoverPen=pg.mkPen(self.theme["text"], width=1.7),
        )
        label = pg.TextItem(
            format_price(price),
            color=self.theme["cyan"],
            anchor=(1, 0.5),
            fill=pg.mkBrush(self.theme["panel2"]),
        )
        label.setFont(typography_font(TextRole.CHART_OVERLAY))
        line.setZValue(58)
        label.setZValue(59)

        def moved() -> None:
            value = float(line.value())
            label.setText(format_price(raw_price(value, self.logarithmic)))
            label.setPos(self.price_plot.viewRange()[0][1], value)
            if self.selected_drawing is not None:
                self._update_drawing_selection_graphic()

        line.sigPositionChanged.connect(moved)
        self.price_plot.addItem(line, ignoreBounds=True)
        self.price_plot.addItem(label, ignoreBounds=True)
        self.horizontal_graphics.append((line, label))
        moved()
        self._apply_drawings_visibility()

    @staticmethod
    def _duration_label(seconds: float) -> str:
        seconds = abs(seconds)
        if seconds >= 86_400:
            return f"{seconds / 86_400:.1f}d"
        if seconds >= 3_600:
            return f"{seconds / 3_600:.1f}h"
        if seconds >= 60:
            return f"{seconds / 60:.1f}m"
        return f"{seconds:.0f}s"

    def _render_ruler(self, start: QtCore.QPointF, end: QtCore.QPointF) -> None:
        self._clear_drawing_graphics("ruler")
        x0, x1 = start.x(), end.x()
        y0, y1 = start.y(), end.y()
        price0 = max(raw_price(y0, self.logarithmic), 1e-300)
        price1 = max(raw_price(y1, self.logarithmic), 1e-300)
        positive = price1 >= price0
        color = self.theme["green"] if positive else self.theme["red"]
        pen = QtGui.QPen(QtGui.QColor(color))
        pen.setCosmetic(True)
        pen.setWidthF(1.3)
        rectangle = QtWidgets.QGraphicsRectItem(
            QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized()
        )
        rectangle.setPen(pen)
        rectangle.setBrush(
            QtGui.QBrush(opaque_overlay_color(self.theme["bg"], color, 27))
        )
        rectangle.setZValue(62)
        diagonal = pg.PlotDataItem([x0, x1], [y0, y1], pen=pg.mkPen(color, width=1.4))
        diagonal.setZValue(63)
        percent = (price1 / max(price0, 1e-300) - 1.0) * 100.0
        absolute = price1 - price0
        absolute_text = f"{'+' if absolute >= 0 else '-'}{format_price(abs(absolute))}"
        bars = abs(x1 - x0) / max(INTERVAL_SECONDS[self.interval], 1)
        text = (
            f"{percent:+.2f}%  ·  {absolute_text}"
            f"  ·  {bars:.1f} bars  ·  {self._duration_label(x1 - x0)}"
        )
        label = pg.TextItem(
            text,
            color=self.theme["text"],
            anchor=(0 if x1 >= x0 else 1, 1 if y1 >= y0 else 0),
            fill=pg.mkBrush(self.theme["panel2"]),
            border=pg.mkPen(color, width=1),
        )
        label.setFont(typography_font(TextRole.CHART_OVERLAY))
        label.setPos(x1, y1)
        label.setZValue(64)
        self.price_plot.addItem(rectangle, ignoreBounds=True)
        self.price_plot.addItem(diagonal, ignoreBounds=True)
        self.price_plot.addItem(label, ignoreBounds=True)
        self.ruler_graphics.extend((rectangle, diagonal, label))
        self._apply_drawings_visibility()

    def _render_fibonacci(self, start: QtCore.QPointF, end: QtCore.QPointF) -> None:
        self._clear_drawing_graphics("fibonacci")
        x0, x1 = start.x(), end.x()
        price0 = raw_price(start.y(), self.logarithmic)
        price1 = raw_price(end.y(), self.logarithmic)
        ratios = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
        span = max(abs(x1 - x0), INTERVAL_SECONDS[self.interval] * 2.0)
        line_start = min(x0, x1)
        line_end = max(x0, x1) + span * 0.12
        for index, ratio in enumerate(ratios):
            price = price0 + (price1 - price0) * ratio
            if price <= 0:
                continue
            shown = chart_y(price, self.logarithmic)
            color = self.theme["cyan"] if ratio in {0.0, 0.5, 1.0} else self.theme["purple"]
            line = pg.PlotDataItem(
                [line_start, line_end],
                [shown, shown],
                pen=pg.mkPen(color, width=1.2 if ratio in {0.0, 0.5, 1.0} else 0.9),
            )
            line.setZValue(61)
            label = pg.TextItem(
                f"{ratio:.3f}  {format_price(price)}",
                color=color,
                anchor=(1, 0.5),
                fill=pg.mkBrush(self.theme["panel2"]),
            )
            label.setFont(typography_font(TextRole.CHART_OVERLAY))
            label.setPos(line_end, shown)
            label.setZValue(62 + index * 0.01)
            self.price_plot.addItem(line, ignoreBounds=True)
            self.price_plot.addItem(label, ignoreBounds=True)
            self.fibonacci_graphics.extend((line, label))
        anchor = pg.PlotDataItem(
            [x0, x1],
            [start.y(), end.y()],
            pen=pg.mkPen(alpha_color(self.theme["muted"], 150), width=1, style=Qt.PenStyle.DashLine),
        )
        anchor.setZValue(60)
        self.price_plot.addItem(anchor, ignoreBounds=True)
        self.fibonacci_graphics.append(anchor)
        self._apply_drawings_visibility()

    def _auto_fib_analysis_key(self) -> tuple[Any, ...]:
        maximum = int(
            self.indicator_settings.get("Auto Fibonacci", {}).get(
                "maximum_candidates", 5
            )
        )
        frame_keys = tuple(
            sorted(
                (timeframe, self._major_level_frame_key(candles))
                for timeframe, candles in self.multi_frames.items()
            )
        )
        candles = self.candles
        closed_anchor = candles[-2] if len(candles) > 1 else (candles[-1] if candles else None)
        current_key = (
            len(candles),
            id(candles[0]) if candles else None,
            id(closed_anchor) if closed_anchor is not None else None,
            float(closed_anchor.time) if closed_anchor is not None else 0.0,
        )
        zone_key = tuple(
            (float(zone.low), float(zone.high), float(zone.score), str(zone.timeframe), str(zone.kind))
            for zone in self.zones
        )
        return (
            self.interval,
            current_key,
            frame_keys,
            zone_key,
            max(1, min(10, maximum)),
        )

    def _start_auto_fib_worker(self, payload: tuple[Any, ...]) -> None:
        (
            key,
            frames,
            current_interval,
            current_candles,
            current_price,
            zones,
            maximum,
        ) = payload
        self._auto_fib_async_key = key
        worker = _AutoFibWorker(
            key,
            frames,
            current_interval,
            current_candles,
            current_price,
            zones,
            maximum,
        )
        self._auto_fib_worker = worker
        worker.signals.finished.connect(self._auto_fib_worker_finished)
        self._analysis_pool.start(worker)

    def _request_auto_fib_analysis(self) -> None:
        if len(self.candles) < 35:
            return
        key = self._auto_fib_analysis_key()
        self._auto_fib_requested_key = key
        maximum = int(
            self.indicator_settings.get("Auto Fibonacci", {}).get(
                "maximum_candidates", 5
            )
        )

        frames = tuple(
            (timeframe, tuple(candles[-760:]))
            for timeframe, candles in self.multi_frames.items()
            if candles
        )
        payload = (
            key,
            frames,
            self.interval,
            tuple(self.candles[-760:]),
            float(self.current_price or self.candles[-1].close),
            tuple(self.zones),
            max(1, min(10, maximum)),
        )
        if self._auto_fib_async_key is None:
            self._start_auto_fib_worker(payload)
            return
        if self._auto_fib_async_key == key:
            return
        self._auto_fib_async_pending = payload

    @QtCore.Slot(object, object, object)
    def _auto_fib_worker_finished(
        self,
        key: object,
        candidates: object,
        error: object,
    ) -> None:
        finished_key = key if isinstance(key, tuple) else tuple(key)
        self._auto_fib_async_key = None
        self._auto_fib_worker = None
        if error is None and finished_key == self._auto_fib_requested_key:
            self._auto_fib_mailbox = (
                finished_key,
                list(candidates) if isinstance(candidates, (list, tuple)) else [],
            )
            self._navigation_deferred_auto_fib = True
            self._start_navigation_scheduler()
        pending = self._auto_fib_async_pending
        self._auto_fib_async_pending = None
        if pending is not None and pending[0] != finished_key:
            self._start_auto_fib_worker(pending)

    def _install_auto_fib_candidates(
        self,
        candidates: list[AutoFibCandidate],
    ) -> None:
        previous = (
            self.auto_fib_candidates[self.auto_fib_index]
            if 0 <= self.auto_fib_index < len(self.auto_fib_candidates)
            else None
        )
        previous_index = self.auto_fib_index
        self.auto_fib_candidates = candidates
        self.auto_fib_dirty = False
        if not candidates:
            self.auto_fib_index = -1
            self._auto_fib_pending_cycle_step = None
            return

        if previous is None:
            matched_index = 0 if self.auto_fib_enabled else -1
        else:
            def distance(candidate: AutoFibCandidate) -> float:
                time_scale = max(
                    abs(previous.end_time - previous.start_time),
                    abs(candidate.end_time - candidate.start_time),
                    1.0,
                )
                price_scale = max(
                    abs(previous.end_price - previous.start_price),
                    abs(candidate.end_price - candidate.start_price),
                    self.current_price * 0.002,
                    1e-12,
                )
                return (
                    abs(candidate.start_time - previous.start_time) / time_scale
                    + abs(candidate.end_time - previous.end_time) / time_scale
                    + abs(candidate.start_price - previous.start_price) / price_scale
                    + abs(candidate.end_price - previous.end_price) / price_scale
                )
            matched_index = min(
                range(len(candidates)),
                key=lambda index: distance(candidates[index]),
            )
        self.auto_fib_index = matched_index

        pending_step = self._auto_fib_pending_cycle_step
        self._auto_fib_pending_cycle_step = None
        if pending_step is not None:
            direction = -1 if pending_step < 0 else 1
            if previous_index < 0 or self.auto_fib_index < 0:
                self.auto_fib_index = len(candidates) - 1 if direction < 0 else 0
            else:
                next_index = self.auto_fib_index + direction
                if next_index < 0 or next_index >= len(candidates):
                    self.auto_fib_index = -1
                else:
                    self.auto_fib_index = next_index


    def _restore_requested_auto_fibonacci(self) -> None:
        """Commit prepared Auto Fib candidates or request analysis off-thread."""
        if not self.auto_fib_enabled or len(self.candles) < 35:
            return


        if (
            self.indicators_enabled
            and self.indicators.get("Major Price Levels", False)
            and self._major_levels_dirty
        ):


            self._navigation_deferred_major_levels = True
            return
        manual_cycle_commit = False
        if self.auto_fib_dirty or not self.auto_fib_candidates:
            key = self._auto_fib_analysis_key()
            mailbox = self._auto_fib_mailbox
            if mailbox is not None and mailbox[0] == key:
                self._auto_fib_mailbox = None
                manual_cycle_commit = self._auto_fib_pending_cycle_step is not None
                self._install_auto_fib_candidates(mailbox[1])
            else:
                self._request_auto_fib_analysis()
                return
        if self.auto_fib_candidates and self.auto_fib_index < 0 and not manual_cycle_commit:
            self.auto_fib_index = 0
        if 0 <= self.auto_fib_index < len(self.auto_fib_candidates):
            self._render_auto_fibonacci()
        else:
            self._clear_auto_fibonacci_graphics()

    def cycle_auto_fibonacci(self, step: int = 1) -> str:
        """Cycle candidates; expensive candidate detection never runs in this UI callback."""
        self.auto_fib_enabled = True
        if len(self.candles) < 35:
            self._clear_auto_fibonacci_graphics()
            self.auto_fib_index = -1
            return "AUTO FIB · WAITING FOR MORE CANDLES"

        if self.auto_fib_dirty or not self.auto_fib_candidates:
            self._auto_fib_pending_cycle_step = int(step)
            self._request_auto_fib_analysis()
            self._navigation_deferred_auto_fib = True
            self._start_navigation_scheduler()
            return "AUTO FIB · ANALYZING…"

        count = len(self.auto_fib_candidates)
        if count == 0:
            self._clear_auto_fibonacci_graphics()
            self.auto_fib_index = -1
            return "AUTO FIB · NO QUALIFYING SWINGS"

        direction = -1 if step < 0 else 1
        if self.auto_fib_index < 0:
            self.auto_fib_index = count - 1 if direction < 0 else 0
        else:
            next_index = self.auto_fib_index + direction
            if next_index < 0 or next_index >= count:
                self.clear_auto_fibonacci(reset_candidates=False)
                return "AUTO FIB · OFF"
            self.auto_fib_index = next_index

        self._render_auto_fibonacci()
        candidate = self.auto_fib_candidates[self.auto_fib_index]
        timeframe = "1D" if candidate.timeframe == "1d" else candidate.timeframe.upper()
        return (
            f"AUTO FIB {self.auto_fib_index + 1}/{count} · "
            f"{candidate.label} · {timeframe}"
        )

    def _render_auto_fibonacci(self) -> None:
        self._clear_auto_fibonacci_graphics()
        if not (0 <= self.auto_fib_index < len(self.auto_fib_candidates)):
            return

        candidate = self.auto_fib_candidates[self.auto_fib_index]
        x0 = candidate.start_time
        x1 = candidate.end_time
        price0 = candidate.start_price
        price1 = candidate.end_price
        if price0 <= 0 or price1 <= 0:
            return

        tooltip = (
            f"{candidate.label} · {candidate.timeframe.upper()}\n"
            f"{format_price(price0)} → {format_price(price1)}\n"
            "0.000 at impulse high · retracement support below\n"
            "Alt+F next · Shift+Alt+F previous"
        )

        settings = self.indicator_settings.get("Auto Fibonacci", {})
        configured = settings.get("levels", INDICATOR_SETTING_DEFAULTS["Auto Fibonacci"]["levels"])
        ratios = tuple(
            sorted(
                {
                    round(float(value), 6)
                    for value in configured
                    if isinstance(value, (int, float)) and math.isfinite(float(value))
                }
            )
        )
        if not ratios:
            ratios = tuple(INDICATOR_SETTING_DEFAULTS["Auto Fibonacci"]["levels"])
        show_ratios = bool(settings.get("show_ratios", True))
        show_prices = bool(settings.get("show_prices", True))
        impulse = price1 - price0
        for ratio in ratios:
            price = price1 - impulse * ratio
            if price <= 0:
                continue
            shown = chart_y(price, self.logarithmic)
            emphasized = ratio in {0.5, 0.618}
            boundary = ratio in {0.0, 1.0}
            extension = ratio < 0.0
            source_color = self.theme["text"] if emphasized else self.theme["muted"]
            line_color = QtGui.QColor(source_color)
            line_color.setAlpha(
                128 if emphasized else (104 if boundary else (72 if extension else 82))
            )
            label_color = QtGui.QColor(source_color)
            label_color.setAlpha(205 if emphasized else (154 if extension else 172))
            line = pg.PlotDataItem(
                [x0, x1],
                [shown, shown],
                pen=pg.mkPen(
                    line_color,
                    width=1.05 if emphasized else (0.85 if boundary else 0.72),
                ),
            )
            line.setZValue(59.5 if emphasized else 59.0)
            line.setToolTip(tooltip)
            label_parts = []
            if show_ratios:
                label_parts.append(f"{ratio:.3f}")
            if show_prices:
                label_parts.append(format_price(price))
            label = pg.TextItem(
                "  ".join(label_parts),
                color=label_color,
                anchor=(1, 0.5),
                fill=pg.mkBrush(self.theme["bg"]),
            )
            label.setFont(typography_font(TextRole.CHART_OVERLAY))
            label.setZValue(65)
            label.setToolTip(tooltip)
            self.price_plot.addItem(line, ignoreBounds=True)
            self.price_plot.addItem(label, ignoreBounds=True)
            self.auto_fib_graphics.extend((line, label))
            self.auto_fib_level_graphics.append((line, label, shown))

        if bool(settings.get("show_anchor", True)):
            anchor_color = QtGui.QColor(self.theme["muted"])
            anchor_color.setAlpha(96)
            anchor = pg.PlotDataItem(
                [x0, x1],
                [chart_y(price0, self.logarithmic), chart_y(price1, self.logarithmic)],
                pen=pg.mkPen(anchor_color, width=0.8, style=Qt.PenStyle.DotLine),
            )
            anchor.setZValue(58)
            anchor.setToolTip(tooltip)
            anchor_points = pg.ScatterPlotItem(
                x=[x0, x1],
                y=[chart_y(price0, self.logarithmic), chart_y(price1, self.logarithmic)],
                size=5,
                symbol="o",
                pen=pg.mkPen(alpha_color(self.theme["muted"], 155), width=0.8),
                brush=pg.mkBrush(self.theme["bg"]),
            )
            anchor_points.setZValue(60)
            anchor_points.setToolTip(tooltip)
            self.price_plot.addItem(anchor, ignoreBounds=True)
            self.price_plot.addItem(anchor_points, ignoreBounds=True)
            self.auto_fib_graphics.extend((anchor, anchor_points))

        timeframe = "1D" if candidate.timeframe == "1d" else candidate.timeframe.upper()
        direction = "LOW → HIGH" if candidate.direction > 0 else "HIGH → LOW"
        self.auto_fib_badge.setText(
            f"AUTO FIB {self.auto_fib_index + 1}/{len(self.auto_fib_candidates)}"
            f"  ·  {candidate.label}  ·  {timeframe}  ·  {direction}"
        )
        badge_color = QtGui.QColor(self.theme["muted"])
        badge_color.setAlpha(205)
        self.auto_fib_badge.setColor(badge_color)
        self.auto_fib_badge.fill = pg.mkBrush(self.theme["bg"])
        self.auto_fib_badge.update()
        self.auto_fib_badge.setToolTip(tooltip)
        self.auto_fib_badge.setVisible(bool(settings.get("show_badge", True)))
        self._position_auto_fibonacci_levels()
        self._position_overlay_labels()
        self._apply_auto_fibonacci_visibility()

    def _position_auto_fibonacci_levels(self) -> None:
        if not self.auto_fib_level_graphics:
            return
        if not (0 <= self.auto_fib_index < len(self.auto_fib_candidates)):
            return
        x_range = self.price_plot.viewRange()[0]
        width = max(float(x_range[1] - x_range[0]), 1.0)
        candidate = self.auto_fib_candidates[self.auto_fib_index]
        anchor_left = min(candidate.start_time, candidate.end_time)
        anchor_right = max(candidate.start_time, candidate.end_time)
        settings = self.indicator_settings.get("Auto Fibonacci", {})
        extent = str(settings.get("line_extent", "right_edge"))
        if extent == "anchors":
            line_start, line_end = anchor_left, anchor_right
        elif extent == "full":
            line_start = float(x_range[0]) + width * 0.012
            line_end = float(x_range[1]) - width * 0.012
        else:
            line_start = max(float(x_range[0]), anchor_left)
            line_end = float(x_range[1]) - width * 0.012
        if line_start >= line_end:
            line_start = float(x_range[0]) + width * 0.012
            line_end = float(x_range[1]) - width * 0.012
        position = str(settings.get("label_position", "right"))
        if position == "left":
            label_x, label_anchor = line_start, (0, 0.5)
        elif position == "center":
            label_x, label_anchor = (line_start + line_end) * 0.5, (0.5, 0.5)
        else:
            label_x, label_anchor = line_end, (1, 0.5)
        for line, label, shown in self.auto_fib_level_graphics:
            line.setData([line_start, line_end], [shown, shown])
            label.setAnchor(label_anchor)
            label.setPos(label_x, shown)

    def update_funding_history(self, payload: list[dict[str, Any]]) -> None:
        self._history_batches["funding"].append(payload)
        self._request_event_history("funding")

    def _request_event_history(self, kind):
        self._history_input_serial += 1
        minimum = float(self.indicator_settings["Liquidations"].get("minimum_notional", 100_000))
        current = tuple(self.funding_history if kind == "funding" else self.liquidations)
        key = (self._market_generation, kind, self._history_input_serial,
               self._liquidation_live_revision, minimum)
        self._history_input_jobs[kind].submit(
            key, prepare_event_history, kind, current,
            tuple(self._history_batches[kind]), minimum,
        )

    @QtCore.Slot(object, object)
    def _event_history_prepared(self, key, prepared):
        generation, kind, _serial, live_revision, minimum = key
        if generation != self._market_generation:
            return
        if kind == "liquidations" and (
            live_revision != self._liquidation_live_revision
            or minimum != float(self.indicator_settings["Liquidations"].get("minimum_notional", 100_000))
        ):
            self._request_event_history(kind)
            return
        self._history_batches[kind].clear()
        if kind == "funding":
            self.funding_history = prepared
        else:
            self.liquidations = prepared
        if self._presentation_active:
            self._schedule_render_work(**{kind: True})
        else:
            self._hidden_presentation_dirty = True

    @QtCore.Slot(object, str)
    def _event_history_failed(self, key, message):
        if key[0] == self._market_generation:
            import logging
            logging.getLogger(__name__).error("%s history preparation failed: %s", key[1], message)


    def _render_funding(self) -> None:
        self._render_study("funding")

    def update_oi_history(self, payload: list[dict[str, Any]]) -> None:
        self.oi_history = payload
        if self._presentation_active:
            self._schedule_render_work(oi=True)
        else:
            self._hidden_presentation_dirty = True

    def append_interest(self, payload: dict[str, Any], reference_price: float) -> None:
        value = safe_float(payload.get("openInterest")) * reference_price
        if value <= 0:
            return
        timestamp = int(safe_float(payload.get("time"), time.time() * 1000))
        row = {"timestamp": timestamp, "sumOpenInterestValue": value}
        if self.oi_history and timestamp - int(safe_float(self.oi_history[-1].get("timestamp"))) < 25_000:
            self.oi_history[-1] = row
        else:
            self.oi_history.append(row)
            self.oi_history = self.oi_history[-500:]
        if self._presentation_active:
            self._schedule_render_work(oi=True)
        else:
            self._hidden_presentation_dirty = True


    def _update_study_headers(self) -> None:
        for name, (_plot, header) in self.study_panes.items():
            settings = self.indicator_settings[name]
            if name == "Open Interest":
                caption = "OPEN INTEREST · USD"
                if int(settings.get("smoothing", 1)) > 1:
                    caption += f" · SMA {settings['smoothing']}"
            elif name == "Funding Rate History":
                caption = "FUNDING · %"
                if int(settings.get("smoothing", 1)) > 1:
                    caption += f" · EMA {settings['smoothing']}"
            else:
                caption = f"{name} · {settings['period']}" + (" · %" if name == "ATR" else "")
            header.set_caption(caption)

    def _price_to_graphics_y(self, price: float) -> float | None:
        if price <= 0.0:
            return None
        try:
            view = self.price_plot.getViewBox()
            x0 = float(view.viewRange()[0][0])
            scene_point = view.mapViewToScene(
                QtCore.QPointF(x0, chart_y(float(price), self.logarithmic))
            )
            viewport_point = self.graphics.mapFromScene(scene_point)
            return float(viewport_point.y() + self.graphics.viewport().pos().y())
        except (RuntimeError, TypeError, ValueError, IndexError):
            return None

    def _position_current_price_line_overlay(self) -> None:
        """Place the persistent LTP line in screen space over the price canvas."""
        overlay = self.current_price_line_overlay
        price = float(self.current_price)
        if not math.isfinite(price) or price <= 0.0:
            overlay.hide()
            return

        view_geometry = self._overlay_price_view_geometry()
        if view_geometry is None:
            overlay.hide()
            return
        view, plot_view, x0 = view_geometry

        viewport = self.graphics.viewport()
        offset = QtCore.QPointF(viewport.pos())
        plot_rect = plot_view.translated(offset).intersected(
            QtCore.QRectF(self.graphics.rect())
        )
        if plot_rect.isEmpty():
            overlay.hide()
            return

        try:
            shown = chart_y(price, self.logarithmic)
            if not math.isfinite(shown):
                overlay.hide()
                return
            scene_point = view.mapViewToScene(QtCore.QPointF(x0, shown))
            viewport_point = self.graphics.mapFromScene(scene_point)
            line_y = float(viewport_point.y() + offset.y())
        except (RuntimeError, TypeError, ValueError, IndexError):
            overlay.hide()
            return


        if (
            not math.isfinite(line_y)
            or line_y < plot_rect.top() - 0.75
            or line_y > plot_rect.bottom() + 0.75
        ):
            overlay.hide()
            return

        left = int(math.floor(plot_rect.left()))
        right = int(math.ceil(plot_rect.right()))
        top = int(math.floor(line_y - 2.0))
        bottom = int(math.ceil(line_y + 2.0))
        geometry = QtCore.QRect(
            left,
            top,
            max(1, right - left),
            max(3, bottom - top),
        )
        if overlay.geometry() != geometry:
            overlay.setGeometry(geometry)
        overlay.set_line_y(line_y - float(geometry.top()))
        if not overlay.isVisible():
            overlay.show()
        overlay.raise_()

    def _position_price_axis_focus_overlay(self) -> None:
        overlay = self.price_axis_focus_overlay
        if overlay is None:
            return
        axis_view = self._order_rail_axis_view_rect()
        if axis_view is None or axis_view.isEmpty():
            overlay.hide()
            return
        offset = QtCore.QPointF(self.graphics.viewport().pos())
        axis_rect = axis_view.translated(offset).intersected(QtCore.QRectF(self.graphics.rect()))
        if axis_rect.isEmpty():
            overlay.hide()
            return
        geometry = QtCore.QRect(
            int(math.floor(axis_rect.left())),
            int(math.floor(axis_rect.top())),
            max(1, int(math.ceil(axis_rect.width()))),
            max(1, int(math.ceil(axis_rect.height()))),
        )
        if overlay.geometry() != geometry:
            overlay.setGeometry(geometry)


        axis_text_band = self._native_price_axis_text_band(axis_rect)
        if axis_text_band is not None:
            text_left, text_right = axis_text_band
            overlay.set_axis_text_band(
                text_left - float(geometry.left()),
                max(1.0, text_right - text_left),
            )
        else:
            overlay.set_axis_text_band(
                5.0,
                max(1.0, float(geometry.width()) - 7.0),
            )

        current_y = self._price_to_graphics_y(self.current_price)
        if current_y is not None:
            current_y -= float(geometry.top())
            if (
                not math.isfinite(current_y)
                or current_y < -PriceAxisFocusOverlay.BADGE_HEIGHT
                or current_y > geometry.height() + PriceAxisFocusOverlay.BADGE_HEIGHT
            ):
                current_y = None

        rail_y: float | None = None
        if self.order_rail_value is not None:
            rail_graphics_y = self._price_to_graphics_y(float(self.order_rail_value))
            if rail_graphics_y is not None:
                rail_y = rail_graphics_y - float(geometry.top())
                if rail_y < -PriceAxisFocusOverlay.FOCUS_RADIUS or rail_y > geometry.height() + PriceAxisFocusOverlay.FOCUS_RADIUS:
                    rail_y = None

        cursor_y: float | None = None
        if self.crosshair_visible and self._crosshair_axis_y_value is not None and self._crosshair_axis_text:
            try:
                view = self.price_plot.getViewBox()
                x0 = float(view.viewRange()[0][0])
                scene_point = view.mapViewToScene(
                    QtCore.QPointF(x0, float(self._crosshair_axis_y_value))
                )
                viewport_point = self.graphics.mapFromScene(scene_point)
                cursor_y = (
                    float(viewport_point.y() + self.graphics.viewport().pos().y())
                    - float(geometry.top())
                )
                if cursor_y < -PriceAxisFocusOverlay.BADGE_HEIGHT or cursor_y > geometry.height() + PriceAxisFocusOverlay.BADGE_HEIGHT:
                    cursor_y = None
            except (RuntimeError, TypeError, ValueError, IndexError):
                cursor_y = None

        overlay.set_focus_state(
            current_y=current_y,
            current_text=self._current_price_text if current_y is not None and self.current_price > 0 else "",
            current_color=self._current_price_badge_color,
            current_tooltip=self._current_price_badge_tooltip,
            rail_y=rail_y,
            cursor_y=cursor_y,
            cursor_text=self._crosshair_axis_text if cursor_y is not None else "",
            cursor_color=self.theme.get("muted", self.theme.get("text", "#d4dbe1")),
        )
        if overlay.isVisible():
            overlay.raise_()


            for parked in self._active_parked_order_rails():
                hud = parked.get("hud")
                if isinstance(hud, MagneticOrderRailPanel) and hud.isVisible():
                    hud.raise_()
            if self.order_rail_hud is not None and self.order_rail_hud.isVisible():
                self.order_rail_hud.raise_()

    def _position_interaction_overlays(self) -> tuple[float, float]:
        """Move only overlays that must remain attached during camera motion."""
        x_range = self.price_plot.viewRange()[0]
        with self._overlay_geometry_transaction():
            self._position_current_price_line_overlay()
            self._position_order_rail_hud()
        return float(x_range[0]), float(x_range[1])

    def _position_overlay_labels(self, *, force_full: bool = False) -> None:


        if not force_full and self._navigation_is_active():
            self._position_interaction_overlays()
            self._navigation_pending_overlay = True
            self._start_navigation_scheduler()
            return

        profile_started = time.perf_counter() if (
            force_full and performance_profile_active()
        ) else 0.0
        if profile_started:
            record_performance_count("overlay.full_relayouts")


        view = self.price_plot.getViewBox()
        scene_rect = view.sceneBoundingRect()
        if scene_rect.isEmpty():
            if profile_started:
                record_performance_timing(
                    "overlay.full_relayout_ms",
                    (time.perf_counter() - profile_started) * 1000.0,
                )
            return
        if self.trend_label.isVisible():
            self.trend_label.setPos(
                view.mapSceneToView(scene_rect.topRight() + QtCore.QPointF(-6, 4))
            )
        x_range, y_range = self.price_plot.viewRange()
        for profile in (self.visible_profile, self.session_profile):
            if profile.isVisible():
                profile.set_x_range(x_range)
        if self.indicators_enabled and self.indicators.get("Major Price Levels", False):
            for _region, label, zone in self.zone_graphics:
                if label.isVisible():
                    label.setPos(
                        x_range[1] - (x_range[1] - x_range[0]) * 0.01,
                        chart_y(zone.high, self.logarithmic),
                    )
        if self.auto_fib_badge.isVisible():
            self.auto_fib_badge.setPos(
                view.mapSceneToView(scene_rect.topLeft() + QtCore.QPointF(4.0, 22.0))
            )
            self._position_auto_fibonacci_levels()
        self._position_order_rail_hud()
        self._position_price_axis_focus_overlay()
        if self.drawings_visible:
            for line, label in self.horizontal_graphics:
                if label.isVisible():
                    label.setPos(x_range[1], float(line.value()))
            if self.selected_drawing is not None:
                self._update_drawing_selection_graphic()
        if self.crosshair_vertical.isVisible():
            self.crosshair_time_label.setPos(self.crosshair_vertical.value(), y_range[0])
        if profile_started:
            record_performance_timing(
                "overlay.full_relayout_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )

    def add_liquidation(self, event: dict[str, Any]) -> None:
        parsed = self._parse_liquidation(event)
        if parsed is None:
            return
        minimum = float(
            self.indicator_settings["Liquidations"].get("minimum_notional", 100_000)
        )
        if parsed["notional"] < minimum:
            return
        self.liquidations.append(parsed)
        self._liquidation_live_revision += 1
        if not self._presentation_active:
            self._hidden_presentation_dirty = True
            return
        if (
            self.indicators_enabled
            and self.indicators["Liquidations"]
            and self.liquidation_points.isVisible()
        ):
            self._schedule_render_work(liquidations=True)

    @staticmethod
    def _parse_liquidation(event, fallback_time_ms=0):
        return parse_liquidation(event, fallback_time_ms)

    def update_liquidation_history(self, events: list[dict[str, Any]]) -> None:
        self._history_batches["liquidations"].append(events)
        self._request_event_history("liquidations")

    def _liquidation_hovered(
        self,
        _item: object,
        points: list[object],
        _event: object,
    ) -> None:
        if len(points) == 0:
            hide_hover_tooltip(self)
            return
        data = points[0].data()
        if not isinstance(data, dict):
            return
        stamp = datetime.fromtimestamp(
            safe_float(data.get("time")), timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        show_hover_tooltip(
            self,
            f"{data.get('liquidated_position', '')} LIQUIDATION\n"
            f"{human_number(safe_float(data.get('notional')), money=True)}\n"
            f"Price {format_price(safe_float(data.get('price')))}\n{stamp}",
            QtGui.QCursor.pos(),
        )

    def add_depth(self, event: dict[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        prepared_bids = event.get("_bids")
        prepared_asks = event.get("_asks")
        if isinstance(prepared_bids, list) and isinstance(prepared_asks, list):
            return prepared_bids, prepared_asks

        bids = sorted(
            (
                (safe_float(row[0]), safe_float(row[1]))
                for row in event.get("b", [])
                if len(row) >= 2 and safe_float(row[0]) > 0 and safe_float(row[1]) > 0
            ),
            key=lambda row: row[0],
            reverse=True,
        )[:20]
        asks = sorted(
            (
                (safe_float(row[0]), safe_float(row[1]))
                for row in event.get("a", [])
                if len(row) >= 2 and safe_float(row[0]) > 0 and safe_float(row[1]) > 0
            ),
            key=lambda row: row[0],
        )[:20]
        return bids, asks


    def _lod_stride(self, visible_count: int, pixel_width: float) -> int:

        if not self.lod_aggregation_enabled:
            self._lod_stride_hint = 1
            return 1

        raster_fallback = (not self.use_opengl) or self._opengl_runtime_failed
        physical_width = max(320.0, float(pixel_width))

        if raster_fallback:


            pixels_per_bar = 3.0
            visual_limit = max(
                160,
                min(
                    min(MAX_RENDER_CANDLES, 900),
                    int(physical_width / pixels_per_bar),
                ),
            )
        else:


            visual_limit = max(
                160,
                min(8192, int(physical_width)),
            )


        density = max(
            1.0 / max(1, visual_limit),
            float(visible_count) / float(visual_limit),
        )
        required = max(1, math.ceil(density))
        stride = 1 << (required - 1).bit_length()
        previous = max(1, int(self._lod_stride_hint))


        if (
            (stride > previous and density <= previous * 1.10)
            or (stride < previous and density >= previous * 0.45)
        ):
            stride = previous

        self._lod_stride_hint = stride
        return stride

    def _invalidate_lod_cache(self) -> None:
        self._history_generation += 1
        self._bar_request_window = None
        self._bar_request_key = None
        self._bar_mailbox = None
        self._bar_job.invalidate()

    def _extend_lod_cache_for_new_closed_candle(self) -> None:
        self._invalidate_lod_cache()
        self._request_price_extrema()

    def _request_price_extrema(self) -> None:
        closed = self.candle_matrix[:-1]
        key = (self._market_generation, self._history_generation)
        self._index_job.submit(key, _prepare_price_extrema, closed, INTERVAL_SECONDS[self.interval])

    def _extrema_prepared(self, key, result) -> None:
        if key == (self._market_generation, self._history_generation):
            self._price_extrema, self._price_extrema_times, self._candle_lod_index = result
            self._navigation_pending_fit = self.auto_scale
            self._start_navigation_scheduler()

    def _bar_data_key(self) -> tuple:
        return (self._market_generation, self._history_generation, self.logarithmic)

    def _bars_prepared(self, key, result) -> None:
        if result.key == self._bar_data_key():
            self._bar_request_key = self._bar_request_window = None


            self._bar_job.invalidate()
            self._bar_mailbox = result
            self._navigation_pending_viewport = True
            self._start_navigation_scheduler(immediate=True)

    def _has_active_rendered_indicators(self) -> bool:
        if not self.indicators_enabled:
            return False
        return any(
            self.indicators.get(name, False)
            for name in (
                "Bollinger Bands",
                "ATR",
                "RSI",
                "EMA Trend",
                "VWAP",
                "Donchian Channels",
            )
        )

    def _render_viewport(
        self,
        force: bool = False,
    ) -> None:
        if self._matrix_waiting:
            return
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        if not self.candles:
            self.history_candles.set_data([])
            self.live_candle.set_data([])
            self.volume_overlay.set_history_data(
                np.empty((0, 7), dtype=np.float64),
                interval_seconds=INTERVAL_SECONDS[self.interval],
            )
            self.volume_overlay.set_live_data(
                np.empty((0, 7), dtype=np.float64),
                interval_seconds=INTERVAL_SECONDS[self.interval],
            )
            self._invalidate_live_render_commits()
            return
        prepared, self._bar_mailbox = self._bar_mailbox, None
        x0, x1 = self.price_plot.viewRange()[0]
        pixel_width = max(320.0, self.price_plot.getViewBox().width() * self.graphics.viewport().devicePixelRatioF())
        visible_slots = max(1, math.ceil((x1 - x0) / INTERVAL_SECONDS[self.interval]))
        camera_key = (
            self._bar_data_key(), self.rendered_window, visible_slots, pixel_width,
            self.lod_aggregation_enabled, self.use_opengl, self._opengl_runtime_failed,
            self.native_bar_renderer_enabled, self._lod_stride_hint,
        )
        resident = getattr(self, "_resident_view_cache", None)
        if (
            not force and prepared is None and resident is not None
            and resident[0] == camera_key
            and resident[1] <= x0 <= x1 <= resident[2]
            and x1 >= self.candle_times[0] and x0 <= self.candle_times[-1]
        ):
            if profile_started:
                record_performance_count("lod.viewport_fast_hits")
                record_performance_timing("lod.viewport_cache_hit_ms", (time.perf_counter() - profile_started) * 1000.0)
            return
        left = bisect_left(self.candle_times, x0)
        right = bisect_right(self.candle_times, x1)
        if right <= left:


            return
        visible_count = max(1, right - left)


        history_stride = self._lod_stride(visible_slots, pixel_width)
        native_resident = bool(
            self.use_opengl
            and not self._opengl_runtime_failed
            and self.native_bar_renderer_enabled
        )
        history_count = max(0, len(self.candles) - 1)
        if prepared is not None and prepared.key == self._bar_data_key():
            start, end, stride = prepared.window
            if (stride == history_stride and start <= min(left, history_count)
                    and end >= min(right, history_count)):
                self.history_candles.set_data(prepared.candles)
                self.volume_overlay.set_history_data(prepared.volume, interval_seconds=INTERVAL_SECONDS[self.interval])
                self.rendered_window = prepared.window
                self._committed_closed_time = prepared.closed_time
                self._committed_history_stride = max(1, int(prepared.window[2]))
                self._committed_history_owns_tail = end >= history_count
                self._invalidate_live_render_commits()
                self._render_live()
                self.graphics.request_redraw()
                record_performance_count("bars.prepared_commits")
            else:


                record_performance_count("bars.obsolete_view_results")
        if profile_started and self.rendered_window is not None:
            if self.rendered_window[2] != history_stride:
                record_performance_count("lod.stride_changes")

        if native_resident:


            visible_instances = max(
                1,
                int(math.ceil(visible_count / max(1, history_stride))),
            )
            resident_instances = min(
                8192,
                max(4096, visible_instances * 16),
            )
            resident_span = resident_instances * history_stride
            # The GPU budget caps residency at 8192 instances. A four-view
            # margin can exceed that entire window, causing a worker rebuild
            # on every camera frame. Reserve only available resident headroom.
            guard = min(
                max(1024 * history_stride, visible_count * 4),
                max(0, (resident_span - visible_count) // 4),
            )
            if not force and self.rendered_window is not None:
                cached_start, cached_end, cached_stride = self.rendered_window
                if (
                    cached_stride == history_stride
                    and cached_start <= max(0, left - guard)
                    and cached_end >= min(history_count, right + guard)
                ):


                    safe_guard = guard
                    low_index = cached_start + safe_guard
                    high_index = cached_end - safe_guard - 1
                    low = float("-inf") if cached_start == 0 else self.candle_times[min(low_index, history_count - 1)]
                    high = float("inf") if cached_end == history_count else self.candle_times[max(0, high_index)]
                    self._resident_view_cache = (camera_key, low, high)
                    if profile_started:
                        record_performance_count("lod.resident_hits")
                        record_performance_timing(
                            "lod.viewport_cache_hit_ms",
                            (time.perf_counter() - profile_started) * 1000.0,
                        )
                    return
            center = (left + min(right, history_count)) // 2
            start = max(0, center - resident_span // 2)
            start -= start % history_stride
            end = min(history_count, start + resident_span)
            if end - start < resident_span:
                start = max(0, end - resident_span)
                start -= start % history_stride
            window = (start, end, history_stride)
        else:


            margin = max(96, int(visible_count * 1.5))
            guard = max(24, int(visible_count * 0.65))
            if not force and self.rendered_window is not None:
                cached_start, cached_end, cached_stride = self.rendered_window
                if (
                    cached_stride == history_stride
                    and cached_start <= max(0, left - guard)
                    and cached_end >= min(history_count, right + guard)
                ):
                    if profile_started:
                        record_performance_count("lod.resident_hits")
                        record_performance_timing(
                            "lod.viewport_cache_hit_ms",
                            (time.perf_counter() - profile_started) * 1000.0,
                        )
                    return
            window = (
                max(0, left - margin),
                min(history_count, right + margin),
                history_stride,
            )

        if not force and window == self.rendered_window:
            if profile_started:
                record_performance_count("lod.resident_hits")
                record_performance_timing(
                    "lod.viewport_cache_hit_ms",
                    (time.perf_counter() - profile_started) * 1000.0,
                )
            return
        if profile_started:
            record_performance_count("lod.window_rebuilds")
            if force:
                record_performance_count("lod.forced_rebuilds")
        key = self._bar_data_key()
        requested = self._bar_request_window
        if not force and self._bar_request_key == key and requested is not None:
            if requested[2] == history_stride and requested[0] <= max(0, left-guard) and requested[1] >= min(history_count, right+guard):
                self._render_live()
                return
        request_key = key + window
        self._bar_request_window = window
        self._bar_request_key = key
        self._bar_job.submit(request_key, prepare_window, key, self.candle_matrix[:-1], window,
                             INTERVAL_SECONDS[self.interval], self.logarithmic, self._candle_lod_index)
        self._render_live()
        if self._has_active_rendered_indicators():
            self._navigation_deferred_indicators = True

    @staticmethod
    def _indicator_settings_signature(
        requests: tuple[tuple[str, dict[str, Any]], ...],
    ) -> tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]:
        return tuple(
            (name, tuple(sorted(settings.items())))
            for name, settings in requests
        )

    def _invalidate_indicator_transport(self) -> None:
        """Force the next indicator request to republish a complete source."""
        self._indicator_transport_epoch += 1
        self._indicator_transport_reset_required = True

    def _indicator_source_update(self, *, force_full: bool) -> tuple[Any, ...]:
        """Build a worker payload: full reset rarely, two-row delta normally."""
        generation = (self._market_generation, self._history_generation)
        matrix = self.candle_matrix
        if force_full:
            closed_stop = max(0, len(matrix) - 1)
            closed = matrix[:closed_stop]
            live = matrix[closed_stop:].copy()
            return ("full", closed, live, generation)
        first_time = float(matrix[0, 0]) if len(matrix) else None
        tail = matrix[max(0, len(matrix) - 2):].copy()
        return ("delta", len(matrix), first_time, tail, generation)

    def _indicator_build_key(
        self,
        requests: tuple[tuple[str, dict[str, Any]], ...],
    ) -> tuple[Any, ...]:
        prefix_length = max(0, len(self.candles) - 1)
        if prefix_length <= 0:
            source_signature: tuple[Any, ...] = (id(self.candles), 0)
        else:
            source_signature = (
                id(self.candles),
                prefix_length,
                id(self.candles[0]),
                id(self.candles[1]) if prefix_length > 1 else None,
                id(self.candles[prefix_length - 1]),
                id(self.candles[prefix_length - 2]) if prefix_length > 1 else None,
                float(self.candles[prefix_length - 1].time),
            )
        return source_signature + (self._indicator_settings_signature(requests),)

    def _indicator_display_key(
        self,
        requests: tuple[tuple[str, dict[str, Any]], ...],
        first: int,
        last: int,
        pixel_width: float,
        display_stride: int,
    ) -> tuple[Any, ...]:
        return (
            self._indicator_build_key(requests),
            int(self._live_data_revision),
            int(first),
            int(last),
            int(round(pixel_width)),
            int(display_stride),
            bool(self.logarithmic),
        )

    @staticmethod
    def _indicator_display_compatible(prepared_key, requested_key) -> bool:
        # A newer tick must not starve an otherwise current result. Market,
        # closed history, settings, viewport and scale still have to match.
        return (
            prepared_key is not None
            and requested_key is not None
            and prepared_key[0] == requested_key[0]
            and prepared_key[2:] == requested_key[2:]
        )

    def _start_indicator_display_worker(
        self,
        key: tuple[Any, ...],
        requests: tuple[tuple[str, dict[str, Any]], ...],
        first: int,
        last: int,
        pixel_width: float,
        logarithmic: bool,
    ) -> None:
        self._indicator_display_async_key = key
        transport_epoch = int(self._indicator_transport_epoch)
        source_update = self._indicator_source_update(
            force_full=self._indicator_transport_reset_required
        )
        worker = _IndicatorAnalysisWorker(
            self._indicator_analysis_state,
            key,
            source_update,
            transport_epoch,
            requests,
            first,
            last,
            pixel_width,
            logarithmic,
        )
        self._indicator_display_worker = worker
        worker.signals.finished.connect(self._indicator_display_worker_finished)
        self._analysis_pool.start(worker)

    def _request_indicator_display_worker(
        self,
        key: tuple[Any, ...],
        requests: tuple[tuple[str, dict[str, Any]], ...],
        first: int,
        last: int,
        pixel_width: float,
        logarithmic: bool,
    ) -> None:
        self._indicator_display_requested_key = key
        payload = (
            key, requests, int(first), int(last),
            float(pixel_width), bool(logarithmic),
        )
        if self._indicator_display_async_key is None:
            self._start_indicator_display_worker(*payload)
            return
        if self._indicator_display_async_key == key:
            return


        self._indicator_display_async_pending = payload

    @QtCore.Slot(object, object, object)
    def _indicator_display_worker_finished(
        self,
        key: object,
        prepared: object,
        error: object,
    ) -> None:
        finished_key = key if isinstance(key, tuple) else tuple(key)
        self._indicator_display_async_key = None
        self._indicator_display_worker = None

        prepared_dict = dict(prepared) if isinstance(prepared, dict) else {}
        transport_epoch = prepared_dict.pop("__transport_epoch__", None)
        cache_miss = bool(prepared_dict.pop("__cache_miss__", False))
        prepared_dict.pop("__source_generation__", None)
        epoch_is_current = (
            transport_epoch is not None
            and int(transport_epoch) == int(self._indicator_transport_epoch)
        )
        if error is None and epoch_is_current:
            self._indicator_transport_reset_required = bool(cache_miss)
        if cache_miss:
            record_performance_count("analysis.indicator_transport.cache_miss")

        if (
            error is None
            and epoch_is_current
            and not cache_miss
            and self._indicator_display_compatible(finished_key, self._indicator_display_requested_key)
        ):
            self._indicator_display_mailbox = (finished_key, prepared_dict)
            self._navigation_deferred_indicators = True
            self._start_navigation_scheduler()
        elif (
            error is None
            and epoch_is_current
            and cache_miss
            and self._indicator_display_compatible(finished_key, self._indicator_display_requested_key)
        ):


            self._navigation_deferred_indicators = True
            self._start_navigation_scheduler(immediate=True)

        pending = self._indicator_display_async_pending
        self._indicator_display_async_pending = None
        if pending is not None:
            if pending[0] != finished_key:
                self._start_indicator_display_worker(*pending)

    def _commit_indicator_display(
        self,
        prepared: dict[str, dict[str, Any]],
    ) -> None:
        groups = {
            "Bollinger Bands": (self.bb_mid, self.bb_upper, self.bb_lower),
            "ATR": (self.atr_curve,),
            "RSI": (self.rsi_curve,),
            **self.trend_curves,
        }
        for name, payload in prepared.items():
            curves = groups.get(name)
            if curves is None:
                continue
            prepared_curves = payload.get("curves", ())
            for curve, series in zip(curves, prepared_curves):
                display_times, display_values, _predecimated = series
                # The worker already enforces a pixel budget and preserves gaps
                # and extrema. A second decimation can erase that information.
                curve.setDownsampling(ds=1, auto=False)
                curve.setData(display_times, display_values, connect="finite")
            if name == "Bollinger Bands":
                self.bb_fill.prepare_geometry()
            scale_data = payload.get("scale_data")
            if name == "ATR" and scale_data is not None:
                self._atr_scale_data = scale_data
                self._atr_scale_index = payload["scale_index"]
                self._fit_atr_to_visible()
            if name == "RSI" and not self.study_manual_scale["RSI"]:
                self.rsi_plot.setYRange(0.0, 100.0, padding=0)
        self._update_trend_label()

    def _render_indicators(self, candles: list[Candle] | None = None) -> None:
        """Request analysis/display preparation or commit an already-prepared frame."""
        del candles
        if not self._has_active_rendered_indicators():
            return
        source = self.candles
        if not source:
            return

        x0, x1 = self.price_plot.viewRange()[0]
        visible_first = bisect_left(self.candle_times, x0)
        visible_last = bisect_right(self.candle_times, x1)
        visible_count = max(1, visible_last - visible_first)
        pixel_width = max(320.0, float(self.price_plot.getViewBox().width()))
        indicator_margin = max(
            96,
            min(int(visible_count * 1.5), int(pixel_width * 2.0)),
        )
        first = max(0, visible_first - indicator_margin)
        last = min(len(source), visible_last + indicator_margin)
        raw_display_stride = max(
            1,
            math.ceil(visible_count / max(1.0, pixel_width)),
        )
        display_stride = 1 << (raw_display_stride - 1).bit_length()

        groups = {
            "Bollinger Bands": (self.bb_mid, self.bb_upper, self.bb_lower),
            "ATR": (self.atr_curve,),
            "RSI": (self.rsi_curve,),
            **self.trend_curves,
        }
        active_requests: list[tuple[str, dict[str, Any]]] = []
        for name in groups:
            if not self.indicators_enabled or not self.indicators.get(name, False):
                continue
            settings = dict(self.indicator_settings[name])
            if (
                name == "VWAP"
                and not vwap_supports_interval(self.interval, settings.get("anchor", "week"))
            ):
                for curve in groups[name]:
                    curve.setData([], [])
                continue
            active_requests.append((name, settings))
        if not active_requests:
            self._update_trend_label()
            return

        frozen_requests = tuple(active_requests)
        display_key = self._indicator_display_key(
            frozen_requests,
            first,
            last,
            pixel_width,
            display_stride,
        )
        if self._indicator_committed_key == display_key:
            return
        mailbox = self._indicator_display_mailbox
        if mailbox is not None and self._indicator_display_compatible(mailbox[0], display_key):
            self._indicator_display_mailbox = None
            self._commit_indicator_display(mailbox[1])
            self._indicator_committed_key = mailbox[0]
            if mailbox[0] == display_key:
                return


        self._request_indicator_display_worker(
            display_key,
            frozen_requests,
            first,
            last,
            pixel_width,
            self.logarithmic,
        )

    def _update_trend_label(self):
        rows = []
        for name, curves in self.trend_curves.items():
            active = self.indicators_enabled and self.indicators.get(name, False)
            for curve in curves:
                curve.setVisible(active)
            if not active:
                continue
            settings = self.indicator_settings[name]
            if name == "EMA Trend":
                text = " · ".join(f"<span style='color:{self.theme[color]}'>EMA {settings[key]}</span>"
                                  for key, color in (("fast", "cyan"), ("medium", "amber"), ("slow", "purple")))
            elif name == "VWAP":
                text = f"VWAP · {settings['anchor'].upper()} UTC"
                if not vwap_supports_interval(self.interval, settings['anchor']):
                    text += " · ANCHOR UNAVAILABLE ON THIS TIMEFRAME"
            else:
                text = f"DONCHIAN {settings['period']} · PRIOR BARS"
            rows.append(text)
        label_html = "<div style='color:" + self.theme["muted"] + "'>" + "<br>".join(rows) + "</div>"
        if label_html != getattr(self, "_trend_label_html", None):
            self._trend_label_html = label_html
            self.trend_label.setHtml(label_html)
        self.trend_label.setVisible(bool(rows))
        self._position_overlay_labels()

    def _render_volume_live(
        self,
        *,
        force: bool = False,
        prepared: PreparedBars | None = None,
    ) -> None:
        """Update live volume using the exact same resident LOD buckets as candles."""
        key = self._current_volume_live_render_key()
        if not force and key == self._committed_volume_live_render_key:
            return
        if prepared is None:
            _candles, prepared = self._prepare_live_render_batches()
        self.volume_overlay.set_live_data(
            prepared,
            interval_seconds=INTERVAL_SECONDS[self.interval],
        )
        self._committed_volume_live_render_key = key

    def _render_oi(self) -> None:
        self._render_study("oi")

    def _study_key(self, kind):
        name = "Open Interest" if kind == "oi" else "Funding Rate History"
        rows = self.oi_history if kind == "oi" else self.funding_history

        revision = (len(rows), id(rows[0]) if rows else 0, id(rows[-1]) if rows else 0)
        x0, x1 = self.price_plot.viewRange()[0]
        width = float(self.price_plot.getViewBox().width()) * self.graphics.viewport().devicePixelRatioF()
        return (kind, self._market_generation, revision,
                int(self.indicator_settings[name].get("smoothing", 1)), float(x0), float(x1), int(width))

    def _study_prepared(self, key, result):
        kind = key[0]
        if key != self._study_key(kind):
            return
        self._study_mailboxes[kind] = (key, result)
        self._schedule_render_work(**{kind: True})

    def _render_study(self, kind):
        name = "Open Interest" if kind == "oi" else "Funding Rate History"
        plot = self.oi_plot if kind == "oi" else self.funding_plot
        if not self.indicators_enabled or not self.indicators[name] or not plot.isVisible():
            return
        key = self._study_key(kind)
        if self._study_committed.get(kind) == key:
            return
        mailbox = self._study_mailboxes.pop(kind, None)
        if mailbox is None or mailbox[0] != key:
            rows = self.oi_history if kind == "oi" else self.funding_history
            self._study_jobs[kind].submit(key, prepare_study, kind, tuple(rows), *key[3:])
            return
        prepared = mailbox[1]
        self._study_extents[kind] = prepared["extent"]
        self._study_committed[kind] = key
        curve = self.oi_curve if kind == "oi" else self.funding_curve
        curve.setDownsampling(ds=1, auto=False)
        curve.setData(*prepared["curve"], connect="finite")
        if kind == "oi":
            self._fit_oi_to_visible()
        else:
            for side, item, color in (("positive", self.funding_positive_bars, "red"),
                                      ("negative", self.funding_negative_bars, "green")):
                xs, ys = prepared[side]
                item.setOpts(x=xs, height=ys, width=prepared["bar_width"],
                             brush=pg.mkBrush(opaque_overlay_color(self.theme["bg"], self.theme[color], 68)), pen=None)
            self.funding_zero_line.setVisible(bool(len(prepared["curve"][0])))
            self._fit_funding_to_visible()

    def _render_liquidations(self) -> None:
        if (
            not self.indicators_enabled
            or not self.indicators["Liquidations"]
            or not self.liquidation_points.isVisible()
        ):
            return
        up_arrow = getattr(self, "_liquidation_up_arrow", None)
        down_arrow = getattr(self, "_liquidation_down_arrow", None)
        if up_arrow is None or down_arrow is None:
            up_arrow = QtGui.QPainterPath()
            up_arrow.moveTo(0.0, -0.62)
            up_arrow.lineTo(0.56, 0.12)
            up_arrow.lineTo(0.20, 0.12)
            up_arrow.lineTo(0.20, 0.58)
            up_arrow.lineTo(-0.20, 0.58)
            up_arrow.lineTo(-0.20, 0.12)
            up_arrow.lineTo(-0.56, 0.12)
            up_arrow.closeSubpath()
            down_arrow = QtGui.QTransform().scale(1.0, -1.0).map(up_arrow)
            self._liquidation_up_arrow = up_arrow
            self._liquidation_down_arrow = down_arrow
        settings = self.indicator_settings["Liquidations"]
        minimum_notional = float(settings["minimum_notional"])
        maximum_markers = min(20, max(0, int(settings["maximum_markers"])))
        x0, x1 = (float(value) for value in self.price_plot.viewRange()[0])
        if x1 < x0:
            x0, x1 = x1, x0
        eligible = [
            item for item in self.liquidations
            if (
                x0 <= safe_float(item.get("time")) <= x1
                and safe_float(item.get("notional")) >= minimum_notional
            )
        ][-maximum_markers:] if maximum_markers > 0 else []
        logarithms = [math.log10(max(item["notional"], 1.0)) for item in eligible]
        low_log = min(logarithms, default=0.0)
        high_log = max(logarithms, default=low_log)
        spots: list[dict[str, Any]] = []
        long_brush = pg.mkBrush(self.theme["red"])
        short_brush = pg.mkBrush(self.theme["green"])
        marker_pen = pg.mkPen(self.theme["bg"], width=0.8)
        for item, logarithm in zip(eligible, logarithms):
            scale = (logarithm - low_log) / max(high_log - low_log, 1e-9)
            size = 10.0 + scale * 9.0
            is_long = item["side"] == "SELL"
            item["liquidated_position"] = "LONG" if is_long else "SHORT"
            spots.append(
                {
                    "pos": (item["time"], item["price"]),
                    "size": size,
                    "symbol": down_arrow if is_long else up_arrow,
                    "brush": long_brush if is_long else short_brush,
                    "pen": marker_pen,
                    "data": item,
                }
            )
        for spot in spots:
            x_value, price = spot["pos"]
            spot["pos"] = (x_value, chart_y(price, self.logarithmic))
        self.liquidation_points.setData(spots)

    def _render_zones(self) -> None:
        if not self.indicators_enabled or not self.indicators["Major Price Levels"]:


            self.analysis_changed.emit(self.zones, self.profile_alert_levels())
            return

        signature = (
            bool(self.logarithmic),
            self.theme["bg"],
            self.theme["panel2"],
            self.theme["green"],
            self.theme["red"],
            tuple(
                (
                    float(zone.low),
                    float(zone.high),
                    float(zone.score),
                    str(zone.timeframe),
                    int(zone.touches),
                    str(zone.kind),
                    str(zone.source),
                )
                for zone in self.zones
            ),
        )
        x_range = self.price_plot.viewRange()[0]
        if signature == self._zone_render_signature and len(self.zone_graphics) == len(self.zones):


            for _region, label, zone in self.zone_graphics:
                label.setPos(x_range[1], chart_y(zone.high, self.logarithmic))
            self.analysis_changed.emit(self.zones, self.profile_alert_levels())
            return

        for region, label, _zone in self.zone_graphics:
            self.price_plot.removeItem(region)
            self.price_plot.removeItem(label)
        self.zone_graphics.clear()
        for zone in self.zones:
            color = self.theme["green"] if zone.kind == "support" else self.theme["red"]
            region = pg.LinearRegionItem(
                values=(
                    chart_y(zone.low, self.logarithmic),
                    chart_y(zone.high, self.logarithmic),
                ),
                orientation=pg.LinearRegionItem.Horizontal,
                movable=False,
                brush=pg.mkBrush(opaque_overlay_color(self.theme["bg"], color, 36)),
                pen=pg.mkPen(alpha_color(color, 118)),
            )
            region.setZValue(4)
            for line in region.lines:
                line.setHoverPen(pg.mkPen(alpha_color(color, 160)))
            confidence = min(99, int(zone.score))
            label = pg.TextItem(
                zone.timeframe.upper(),
                color=color,
                anchor=(1, 1),
                fill=pg.mkBrush(self.theme["panel2"]),
            )
            label.setFont(typography_font(TextRole.CHART_OVERLAY))
            explanation = (
                f"{zone.timeframe.upper()} major {zone.kind}\n"
                f"Confluence score: {confidence}/99\n"
                f"Evidence: {zone.source}\n"
                f"Separated retests: {zone.touches}\n"
                "Price action carries most of the score; volume-at-price and timeframe add confirmation."
            )
            region.setToolTip(explanation)
            label.setToolTip(explanation)
            label.setZValue(24)
            label.setPos(x_range[1], chart_y(zone.high, self.logarithmic))
            self.price_plot.addItem(region)
            self.price_plot.addItem(label, ignoreBounds=True)
            self.zone_graphics.append((region, label, zone))
        self._zone_render_signature = signature
        self.analysis_changed.emit(self.zones, self.profile_alert_levels())

    def _range_changed(self) -> None:


        record_frame_request(self.graphics.viewport())
        self.graphics.request_redraw()
        self._schedule_navigation_frame(fit=True, tasks=True)

    def _refresh_linked_study_ranges(self) -> None:
        """Scale only visible linked studies without rebuilding their data."""
        if self._resize_expensive_deferred or not self.indicators_enabled:
            return
        if self.atr_plot.isVisible() and self.indicators["ATR"]:
            self._fit_atr_to_visible()
        if self.oi_plot.isVisible() and self.indicators["Open Interest"]:
            self._fit_oi_to_visible()
        if self.funding_plot.isVisible() and self.indicators["Funding Rate History"]:
            self._render_study("funding")
            self._fit_funding_to_visible()

    def _fit_atr_to_visible(self) -> None:
        if not self.indicators_enabled or not self.indicators["ATR"] or self.study_manual_scale["ATR"]:
            return
        index = getattr(self, "_atr_scale_index", None)
        if index is None:
            return
        times, _values = self._atr_scale_data
        x0, x1 = self.price_plot.viewRange()[0]
        low, high = index.query(np.searchsorted(times, x0), np.searchsorted(times, x1, side="right"))
        if math.isfinite(low) and math.isfinite(high):
            low = max(0., low)
            pad = max((high-low)*.12, high*.04, 1e-6)
            self.atr_plot.setYRange(max(0., low-pad), high+pad, padding=0)

    def _fit_oi_to_visible(self) -> None:
        if not self.indicators_enabled or not self.indicators["Open Interest"] or self.study_manual_scale["Open Interest"]:
            return
        if self._study_committed.get("oi") != self._study_key("oi"):
            self._render_study("oi")
            return
        extent = self._study_extents.get("oi")
        if extent is not None:
            low, high = extent
            pad = max((high-low)*.12, high*.0015, 1.)
            self.oi_plot.setYRange(max(0., low-pad), high+pad, padding=0)

    def _fit_funding_to_visible(self) -> None:
        if not self.indicators_enabled or not self.indicators["Funding Rate History"] or self.study_manual_scale["Funding Rate History"]:
            return
        if self._study_committed.get("funding") != self._study_key("funding"):
            self._render_study("funding")
            return
        extent = self._study_extents.get("funding")
        if extent is not None:
            magnitude = max(abs(extent[0]), abs(extent[1]), .0001)*1.12
            self.funding_plot.setYRange(-magnitude, magnitude, padding=0)

    @staticmethod
    def _profile_identity_key(
        candles: list[Candle],
        bins: int,
        levels: int,
        *,
        live_revision: int = 0,
    ) -> tuple[Any, ...]:
        """O(1) source identity for immutable/replaced Candle snapshots."""
        if not candles:
            return (int(bins), int(levels), 0, int(live_revision))
        first = candles[0]
        last = candles[-1]
        previous = candles[-2] if len(candles) > 1 else last
        return (
            int(bins),
            int(levels),
            len(candles),
            id(first),
            float(first.time),
            id(previous),
            float(previous.time),
            id(last),
            float(last.time),
            int(live_revision),
        )

    def _start_profile_worker(
        self,
        kind: str,
        key: tuple[Any, ...],
        candles: list[Candle],
        bins: int,
        levels: int,
    ) -> None:
        self._profile_async_keys[kind] = key
        worker = _ProfileAnalysisWorker(kind, key, candles, bins, levels)
        self._profile_workers[kind] = worker
        worker.signals.finished.connect(self._profile_worker_finished)
        self._analysis_pool.start(worker)

    def _request_profile_analysis(
        self,
        kind: str,
        key: tuple[Any, ...],
        candles: list[Candle],
        bins: int,
        levels: int,
    ) -> None:
        self._profile_requested_keys[kind] = key
        active = self._profile_async_keys.get(kind)
        payload = (key, candles, int(bins), int(levels))
        if active is None:
            self._start_profile_worker(kind, *payload)
            return
        if active == key:
            return

        self._profile_async_pending[kind] = payload

    @QtCore.Slot(object, object, object, object, object, object)
    def _profile_worker_finished(
        self,
        kind: object,
        key: object,
        centers: object,
        volumes: object,
        levels: object,
        error: object,
    ) -> None:
        profile_kind = str(kind)
        finished_key = key if isinstance(key, tuple) else tuple(key)
        self._profile_async_keys[profile_kind] = None
        self._profile_workers[profile_kind] = None
        if (
            error is None
            and finished_key == self._profile_requested_keys.get(profile_kind)
        ):
            self._profile_mailbox[profile_kind] = (
                finished_key,
                np.asarray(centers, dtype=float),
                np.asarray(volumes, dtype=float),
                [float(value) for value in levels],
            )


            self._navigation_deferred_profiles = True
            self._start_navigation_scheduler()

        pending = self._profile_async_pending.get(profile_kind)
        self._profile_async_pending[profile_kind] = None
        if pending is not None:
            pending_key, pending_candles, pending_bins, pending_levels = pending
            if pending_key != finished_key:
                self._start_profile_worker(
                    profile_kind,
                    pending_key,
                    pending_candles,
                    pending_bins,
                    pending_levels,
                )

    def _update_profiles(self, *, position_overlays: bool = True) -> None:
        """Commit prepared profile results; never compute volume profiles here."""
        if self._resize_expensive_deferred or not self.candles:
            return
        visible_active = bool(
            self.indicators_enabled and self.indicators["Visible Volume Profile"]
        )
        session_active = bool(
            self.indicators_enabled and self.indicators["Session Volume Profile"]
        )
        if not visible_active and not session_active:
            return

        x0, x1 = self.price_plot.viewRange()[0]
        visible_settings = self.indicator_settings["Visible Volume Profile"]
        session_settings = self.indicator_settings["Session Volume Profile"]
        committed = False

        if visible_active:
            left = bisect_left(self.candle_times, x0)
            right = bisect_right(self.candle_times, x1)
            fallback = right - left < 5
            if fallback:
                source_first = max(0, len(self.candles) - 120)
                source_last = len(self.candles)
            else:
                source_first, source_last = left, right
            visible_bins = int(visible_settings["bins"])
            visible_level_count = int(visible_settings["levels"])
            visible_key = (
                source_first,
                source_last,
                self._live_data_revision,
                visible_bins,
                visible_level_count,
            )
            mailbox = self._profile_mailbox.get("visible")
            if mailbox is not None and mailbox[0] == visible_key:
                _, centers, volumes, level_values = mailbox
                self._profile_mailbox.pop("visible", None)
                self._visible_profile_cache = (centers, volumes)
                self._visible_profile_cache_key = visible_key
                self.visible_levels = level_values
            elif visible_key != self._visible_profile_cache_key:
                snapshot = self._matrix_snapshot(source_first, source_last)
                self._request_profile_analysis(
                    "visible",
                    visible_key,
                    snapshot,
                    visible_bins,
                    visible_level_count,
                )

            if visible_key == self._visible_profile_cache_key:
                centers, volumes = self._visible_profile_cache
                self.visible_profile.set_profile(
                    centers,
                    volumes,
                    (x0, x1),
                    self.theme["cyan"],
                    float(visible_settings["width_pct"]) / 100.0,
                    72,
                    self.logarithmic,
                )
                committed = True

        if session_active:
            session_bins = int(session_settings["bins"])
            session_level_count = int(session_settings["levels"])
            if self.session_candles:
                session_source = self.session_candles
                session_live_revision = 0
            else:
                session_source = self.candles[-400:]
                session_live_revision = self._live_data_revision
            session_key = self._profile_identity_key(
                session_source,
                session_bins,
                session_level_count,
                live_revision=session_live_revision,
            )
            mailbox = self._profile_mailbox.get("session")
            if mailbox is not None and mailbox[0] == session_key:
                _, centers, volumes, level_values = mailbox
                self._profile_mailbox.pop("session", None)
                self.session_profile_cache = (centers, volumes)
                self._session_profile_cache_key = session_key
                self.session_levels = level_values
            elif session_key != self._session_profile_cache_key:
                self._request_profile_analysis(
                    "session",
                    session_key,
                    list(session_source),
                    session_bins,
                    session_level_count,
                )

            if session_key == self._session_profile_cache_key:
                session_centers, session_volumes = self.session_profile_cache
                self.session_profile.set_profile(
                    session_centers,
                    session_volumes,
                    (x0, x1),
                    self.theme["purple"],
                    float(session_settings["width_pct"]) / 100.0,
                    43,
                    self.logarithmic,
                )
                committed = True

        if committed:
            if position_overlays:
                self._position_overlay_labels()
            self.analysis_changed.emit(self.zones, self.profile_alert_levels())

    def _target_y_range(self, x0: float, x1: float) -> tuple[float, float] | None:
        if self._matrix_waiting:
            return None
        left = bisect_left(self.candle_times, x0)
        right = min(len(self.candle_matrix), bisect_right(self.candle_times, x1))
        if right <= left:
            return None
        index = self._price_extrema
        indexed_times = self._price_extrema_times
        minimum, maximum = math.inf, -math.inf
        if index is not None and len(indexed_times):
            a = np.searchsorted(indexed_times, max(x0, self.candle_times[0]))
            b = np.searchsorted(indexed_times, x1, side="right")
            minimum, maximum = index.query(a, b)
            tail_first = bisect_right(self.candle_times, float(indexed_times[-1]))
        else:
            tail_first = 0
        tail = self.candle_matrix[max(left, tail_first):right]
        if len(tail):
            minimum = min(minimum, float(np.min(tail[:, 3])))
            maximum = max(maximum, float(np.max(tail[:, 2])))
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            return None
        minimum_y, maximum_y = chart_y(minimum, self.logarithmic), chart_y(maximum, self.logarithmic)
        padding = max((maximum_y-minimum_y)*0.075, 1e-8)
        return minimum_y-padding, maximum_y+padding

    def _fit_y_to_visible(self, x0: float, x1: float) -> None:
        target = self._target_y_range(x0, x1)
        if target is None:
            return
        target_low, target_high = target
        current_low, current_high = self.price_plot.viewRange()[1]
        pixel_height = max(float(self.price_plot.getViewBox().height()), 1.0)
        tolerance = max((target_high - target_low) / pixel_height * 0.35, 1e-12)
        if (
            abs(float(current_low) - target_low) <= tolerance
            and abs(float(current_high) - target_high) <= tolerance
        ):
            return
        if performance_profile_active():
            record_performance_count("overlay.auto_scale_y_changes")
        self._auto_scale_y_change = True
        try:
            self.price_plot.setYRange(target_low, target_high, padding=0)
        finally:
            self._auto_scale_y_change = False

    def profile_alert_levels(self) -> list[float]:
        combined: list[float] = []
        for level in (*self.visible_levels, *self.session_levels):
            if all(abs(level - old) > max(level, old) * 0.0005 for old in combined):
                combined.append(level)
        return combined[:6]

    def toggle_indicator(self, name: str, enabled: bool) -> None:
        if self.indicators.get(name) == bool(enabled):
            return
        self.indicators[name] = bool(enabled)
        self._apply_indicator_visibility()
        if name in {"Bollinger Bands", "ATR", "RSI", "EMA Trend", "VWAP", "Donchian Channels"}:
            if enabled and self.indicators_enabled:
                self._schedule_render_work(indicators=True)
        elif name == "Open Interest":
            self._render_oi()
        elif name == "Funding Rate History":
            self._render_funding()
        if enabled and self.indicators_enabled:
            if name == "Liquidations":
                self._render_liquidations()
            if name == "Major Price Levels":
                self._major_levels_dirty = True
                self._schedule_render_work(major_levels=True)
            elif name in {"Visible Volume Profile", "Session Volume Profile"}:
                self._schedule_render_work(profiles=True)

    def _apply_indicator_visibility(self) -> None:
        active = self.indicators_enabled

        def set_visible(item: Any, visible: bool) -> None:
            visible = bool(visible)
            if bool(item.isVisible()) != visible:
                item.setVisible(visible)

        self._update_trend_label()
        bb_visible = active and self.indicators["Bollinger Bands"]
        for item in (self.bb_mid, self.bb_upper, self.bb_lower, self.bb_fill):
            set_visible(item, bb_visible)
        self._set_subplot_visible(
            self.atr_plot,
            row=2,
            visible=active and self.indicators["ATR"],
            maximum_height=self.study_heights["ATR"],
        )
        self._set_subplot_visible(
            self.oi_plot,
            row=1,
            visible=active and self.indicators["Open Interest"],
            maximum_height=self.study_heights["Open Interest"],
        )
        self._set_subplot_visible(
            self.funding_plot,
            row=3,
            visible=active and self.indicators["Funding Rate History"],
            maximum_height=self.study_heights["Funding Rate History"],
        )
        self._set_subplot_visible(
            self.rsi_plot,
            row=4,
            visible=active and self.indicators["RSI"],
            maximum_height=self.study_heights["RSI"],
        )
        set_visible(self.atr_curve, active and self.indicators["ATR"])
        set_visible(self.oi_curve, active and self.indicators["Open Interest"])
        funding_visible = active and self.indicators["Funding Rate History"]
        set_visible(self.funding_curve, funding_visible)
        set_visible(self.funding_positive_bars, funding_visible)
        set_visible(self.funding_negative_bars, funding_visible)
        set_visible(self.funding_zero_line, funding_visible and bool(self.funding_history))
        rsi_visible = active and self.indicators["RSI"]
        set_visible(self.rsi_curve, rsi_visible)
        show_rsi_thresholds = bool(
            self.indicator_settings["RSI"].get("show_thresholds", False)
        )
        set_visible(self.rsi_upper_line, rsi_visible and show_rsi_thresholds)
        set_visible(self.rsi_lower_line, rsi_visible and show_rsi_thresholds)
        set_visible(self.liquidation_points, active and self.indicators["Liquidations"])
        set_visible(self.visible_profile, active and self.indicators["Visible Volume Profile"])
        set_visible(self.session_profile, active and self.indicators["Session Volume Profile"])
        zones_visible = active and self.indicators["Major Price Levels"]
        for region, label, _zone in self.zone_graphics:
            set_visible(region, zones_visible)
            set_visible(label, zones_visible)
        self._apply_auto_fibonacci_visibility()
        self._update_study_headers()
        self._layout_study_panes()

    def _sync_bottom_time_axis(self) -> None:
        """Show one shared time axis on the lowest visible chart row."""
        plots = (
            self.price_plot,
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        )
        owner = self.price_plot
        for candidate in (
            self.rsi_plot,
            self.funding_plot,
            self.atr_plot,
            self.oi_plot,
        ):
            if candidate.isVisible():
                owner = candidate
                break
        chart_font = _chart_axis_font()
        for plot in plots:
            axis = plot.getAxis("bottom")
            axis.setTickFont(chart_font)
            if plot is owner:
                plot.showAxis("bottom")
            else:
                plot.hideAxis("bottom")

    def _set_subplot_visible(
        self,
        plot: pg.PlotItem,
        row: int,
        visible: bool,
        maximum_height: float,
    ) -> None:
        """Collapse hidden indicator rows without reserving invisible layout space."""
        if self.subplot_visibility.get(row) == visible:
            return
        self.subplot_visibility[row] = visible
        if visible:
            plot.setXLink(self.price_plot)
            plot.setMaximumHeight(maximum_height)
            plot.setVisible(True)
            if plot not in self.graphics.ci.items:
                self.graphics.ci.addItem(plot, row=row, col=0)
            plot.getViewBox().setMouseEnabled(x=True, y=True)
            QTimer.singleShot(0, self, self._restore_study_ranges)
        else:
            plot.setVisible(False)
            plot.setXLink(None)
            if plot in self.graphics.ci.items:
                self.graphics.ci.removeItem(plot)
                plot.setParentItem(self.graphics.ci)

    def _layout_study_panes(self) -> None:
        """Allocate all pane rows together, reserving a usable price canvas."""
        panes = getattr(self, "study_panes", None)
        if not panes:
            return
        viewport = self.graphics.viewport()
        width = max(1.0, float(viewport.width()))
        height = max(1.0, float(viewport.height()))
        visible = [(name, plot, header) for name, (plot, header) in panes.items() if plot.isVisible()]
        signature = (
            width,
            height,
            tuple((name, self.study_heights[name]) for name, _plot, _header in visible),
        )
        if signature == self._study_layout_signature:
            return
        self._study_layout_signature = signature
        self._sync_bottom_time_axis()
        # Pane legends overlay the plot like TradingView; they do not consume a row.
        axis_height = math.ceil(QtGui.QFontMetricsF(_chart_axis_font()).height()) + 10
        minimum = [
            52.0 + (axis_height if index == len(visible) - 1 else 0)
            for index, (_name, _plot, _header) in enumerate(visible)
        ]
        desired = [max(floor, self.study_heights[name]) for floor, (name, _plot, _header) in zip(minimum, visible)]
        available = max(sum(minimum), height - max(100.0, height * .35))
        extra = sum(wanted - floor for wanted, floor in zip(desired, minimum))
        factor = min(1.0, max(0.0, available - sum(minimum)) / extra) if extra else 0.0
        allocated = {name: floor + (wanted - floor) * factor
                     for (name, _plot, _header), floor, wanted in zip(visible, minimum, desired)}
        grid = self.graphics.ci.layout
        for row, (name, (plot, _header)) in enumerate(panes.items(), 1):
            pane_height = allocated.get(name, 0.0)
            if pane_height:
                plot.setMinimumHeight(pane_height)
                plot.setMaximumHeight(pane_height)
                plot.setPreferredHeight(pane_height)
            else:
                plot.setMinimumHeight(0)
            grid.setRowMinimumHeight(row, pane_height)
            grid.setRowPreferredHeight(row, pane_height)
            grid.setRowMaximumHeight(row, pane_height)
            grid.setRowStretchFactor(row, 0)
        grid.invalidate()
        grid.activate()
        for _name, _plot, header in visible:
            header.sync_geometry()
        self.graphics.updateGeometry()
        self._sync_crosshair_study_visibility()
        self.graphics.request_redraw()

    def _begin_study_resize(self, name: str) -> None:
        self._study_resize_start = self.study_panes[name][0].geometry().height()

    def _resize_study(self, name: str, delta: float) -> None:
        self.set_study_heights({name: self._study_resize_start - delta})

    def _remove_study(self, name: str) -> None:
        self.toggle_indicator(name, False)
        self.indicator_visibility_changed.emit(name, False)

    def _set_study_manual_scale(self, name: str) -> None:
        if name in self.study_manual_scale:
            self.study_manual_scale[name] = True

    def _reset_study_scale(self, name: str) -> None:
        if name not in self.study_manual_scale:
            return
        self.study_manual_scale[name] = False
        if name == "Open Interest":
            self._fit_oi_to_visible()
        elif name == "Funding Rate History":
            self._fit_funding_to_visible()
        elif name == "ATR":
            self._fit_atr_to_visible()
        elif name == "RSI":
            self.rsi_plot.setYRange(0.0, 100.0, padding=0)
        self.graphics.request_redraw()

    def set_study_heights(self, values: dict[str, float | int]) -> None:
        for name in self.study_heights:
            if name in values:
                value = safe_float(values[name], self._default_study_heights[name])
                self.study_heights[name] = max(82.0, min(400.0, value))
        self._layout_study_panes()

    def saved_study_heights(self) -> dict[str, int]:
        return {name: int(round(height)) for name, height in self.study_heights.items()}

    def set_volume_bar_height_percent(self, value: float | int) -> None:
        value = int(round(max(5.0, min(45.0, float(value)))))
        if value == self.volume_bar_height_percent:
            return
        self.volume_bar_height_percent = value
        self.volume_overlay.set_height_percent(value)
        self.graphics.request_redraw()

    def volume_bar_height_setting(self) -> int:
        return int(self.volume_bar_height_percent)

    def _restore_study_ranges(self) -> None:
        """Reassert lower-pane ranges after the graphics layout has settled."""
        if self.indicators_enabled and self.indicators["ATR"]:
            self._schedule_render_work(indicators=True)
        if self.indicators_enabled and self.indicators["Open Interest"]:
            self._render_oi()
        if self.indicators_enabled and self.indicators["Funding Rate History"]:
            self._render_funding()
        if self.indicators_enabled and self.indicators["RSI"]:
            self._schedule_render_work(indicators=True)
        self.graphics.request_redraw()

    def set_rsi_settings(
        self,
        period: int,
        upper: float,
        lower: float,
        show_thresholds: bool = False,
    ) -> None:
        period = max(2, min(200, int(period)))
        upper = max(50.0, min(99.0, float(upper)))
        lower = max(1.0, min(50.0, float(lower)))
        if lower >= upper:
            raise ValueError("RSI lower level must be below the upper level.")
        self.rsi_period = period
        self.rsi_upper = upper
        self.rsi_lower = lower
        self.indicator_settings["RSI"] = {
            "period": period,
            "upper": upper,
            "lower": lower,
            "show_thresholds": bool(show_thresholds),
        }
        self.rsi_upper_line.setValue(upper)
        self.rsi_lower_line.setValue(lower)
        visible = self.indicators_enabled and self.indicators["RSI"]
        self.rsi_upper_line.setVisible(visible and bool(show_thresholds))
        self.rsi_lower_line.setVisible(visible and bool(show_thresholds))
        if visible:
            self._schedule_render_work(indicators=True)

    def set_indicator_settings(
        self,
        settings: dict[str, dict[str, Any]],
    ) -> None:
        for indicator, defaults in INDICATOR_SETTING_DEFAULTS.items():
            incoming = settings.get(indicator, {})
            self.indicator_settings[indicator] = {
                key: incoming.get(key, default) for key, default in defaults.items()
            }
        rsi = self.indicator_settings["RSI"]
        self.set_rsi_settings(
            int(rsi["period"]),
            float(rsi["upper"]),
            float(rsi["lower"]),
            bool(rsi.get("show_thresholds", False)),
        )
        self._update_study_headers()
        self._session_profile_cache_key = None
        self._visible_profile_cache_key = None
        self._major_levels_dirty = True
        self._schedule_render_work(indicators=True)
        self._render_oi()
        self._render_funding()
        self._render_liquidations()
        self.auto_fib_dirty = True
        self._schedule_render_work(major_levels=True, auto_fib=True, profiles=True)

    def set_indicators_enabled(self, enabled: bool) -> None:
        self.indicators_enabled = enabled
        self._apply_indicator_visibility()
        if enabled:
            self._schedule_render_work(indicators=True)
            self._render_oi()
            self._render_funding()
            self._render_liquidations()
            if self.indicators["Major Price Levels"]:
                self._major_levels_dirty = True
            self._schedule_render_work(major_levels=True, profiles=True)

    def set_auto_scale(self, enabled: bool) -> None:
        changed = enabled != self.auto_scale
        self.auto_scale = enabled
        if changed:
            self.auto_scale_changed.emit(enabled)
        if enabled and self.candles:
            x0, x1 = self.price_plot.viewRange()[0]
            self._fit_y_to_visible(x0, x1)

    def set_native_bar_renderer_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.native_bar_renderer_enabled:
            return
        self.native_bar_renderer_enabled = enabled
        active = self.use_opengl and enabled and not self._opengl_runtime_failed
        for item in (self.history_candles, self.live_candle, self.volume_overlay):
            item.set_gpu_enabled(active)
        self.graphics.request_redraw()

    def diagnostic_state(self) -> dict[str, Any]:
        """Read observed render paths without querying GL or requesting a paint."""
        batches = {
            "history_candles": self.history_candles.pixel_batch,
            "live_candle": self.live_candle.pixel_batch,
            "volume_history": self.volume_overlay.history_batch,
            "volume_live": self.volume_overlay.live_batch,
        }
        states = {name: batch.gpu_diagnostic_state() for name, batch in batches.items()}
        counters = (
            "native_draws", "native_failures", "fallback_frames", "cpu_paints", "intentional_cpu_paints",
        )
        render_path = {key: sum(int(state.get(key, 0)) for state in states.values()) for key in counters}
        reasons: dict[str, int] = {}
        last_context: dict[str, Any] = {}
        last_failure = ""
        for state in states.values():
            for reason, count in state.get("fallback_reasons", {}).items():
                reasons[reason] = reasons.get(reason, 0) + int(count)
            if state.get("last_context"):
                last_context = state["last_context"]
            if state.get("last_native_failure"):
                last_failure = str(state["last_native_failure"])
        render_path.update(fallback_reasons=reasons, last_native_failure=last_failure, last_context=last_context)
        paths = {state.get("observed_path", "unverified") for state in states.values()}
        if self._requested_opengl and (self._opengl_runtime_failed or not self.use_opengl):
            actual_path = "fallback"
        elif not self.use_opengl or not self.native_bar_renderer_enabled:
            actual_path = "software"
        elif "fallback" in paths:
            actual_path = "fallback"
        elif "native" in paths:
            actual_path = "native"
        else:
            actual_path = "unverified"
        statuses = {"native": "native verified", "fallback": "CPU fallback", "software": "software selected", "unverified": "unverified"}
        viewport = self.graphics.viewport()
        application = QtWidgets.QApplication.instance()
        mode = self.graphics.viewportUpdateMode()
        return {
            "renderer": "OpenGL" if self.use_opengl else "Raster",
            "viewport": type(viewport).__name__,
            "viewport_update_mode": getattr(mode, "name", str(mode)),
            "composition_isolated": self.testAttribute(Qt.WidgetAttribute.WA_NativeWindow),
            "size": (int(viewport.width()), int(viewport.height())),
            "device_pixel_ratio": float(viewport.devicePixelRatioF()),
            "ready": bool(self._initial_snapshot_painted),
            "requested_opengl": self._requested_opengl,
            "requested_native": self.native_bar_renderer_enabled,
            "actual_render_path": actual_path,
            "gpu_status": statuses[actual_path],
            "runtime_fallback_reason": self._opengl_runtime_failure_reason,
            "requested_context_format": str(application.property("nightwatchChartOpenGLRequestedFormat") or "") if application is not None else "",
            "gpu_batches": states,
            "render_path": render_path,
        }

    def set_lod_aggregation_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.lod_aggregation_enabled:
            return
        self.lod_aggregation_enabled = enabled
        self._invalidate_lod_cache()
        self.rendered_window = None
        self._render_viewport(force=True)
        self._schedule_render_work(indicators=True, profiles=True, study_scale=True)

    def set_magnetic_order_rail_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.magnetic_order_rail_enabled:
            return
        self.magnetic_order_rail_enabled = enabled
        if not enabled:
            self.clear_magnetic_order_rail()

    def set_candle_style(self, style_name: str) -> None:
        if style_name not in CANDLE_STYLES:
            return
        self.candle_style = style_name
        for item in (self.history_candles, self.live_candle, self.volume_overlay):
            item.set_style(style_name)
        self._start_navigation_scheduler()

    def set_logarithmic(self, enabled: bool) -> None:
        if enabled == self.logarithmic:
            return
        manual_range = None
        if not self.auto_scale:
            low, high = self.price_plot.viewRange()[1]
            low, high = raw_price(float(low), self.logarithmic), raw_price(float(high), self.logarithmic)
            if 0 < low < high and math.isfinite(high):
                manual_range = (chart_y(low, enabled), chart_y(high, enabled))
        rail_price = self.order_rail_raw_price()
        manual_drawings = self.export_manual_drawings()
        auto_fib_active = self.auto_fib_enabled
        self.clear_drawings(include_auto=False)
        self._clear_auto_fibonacci_graphics()
        self.cancel_drawing()
        self.logarithmic = enabled
        self.price_axis.set_logarithmic(enabled)
        if manual_range is not None:
            self.price_plot.setYRange(*manual_range, padding=0)
        for item in (self.history_candles, self.live_candle):
            item.set_logarithmic(enabled)
        self.rendered_window = None
        self._render_viewport(force=True)
        self._render_liquidations()
        self._render_zones()
        if self.current_price > 0:
            self._set_current_price(self.current_price, self.current_rising)
        if self.working_order_payloads:
            self.set_working_orders(self.working_order_payloads)
        if rail_price > 0 or self._parked_order_rails:
            self._position_order_rail_hud()
        self._schedule_render_work(profiles=True)
        self.import_manual_drawings(manual_drawings)
        if auto_fib_active:
            self._render_auto_fibonacci()
        if self.candles and (self.auto_scale or manual_range is None):
            x0, x1 = self.price_plot.viewRange()[0]
            self._fit_y_to_visible(x0, x1)

    def _ensure_snapshot_visible(self) -> None:
        if not self._snapshot_loaded or not self.candles or not self.isVisible():
            return
        viewport = self.graphics.viewport()
        view_rect = self.price_plot.getViewBox().sceneBoundingRect()
        if viewport.width() < 32 or viewport.height() < 32 or view_rect.width() < 32:
            return
        if self._snapshot_visibility_fit:
            self._snapshot_visibility_fit = False
            self.fit_chart()
        if not self._snapshot_visibility_rendered:
            self._snapshot_visibility_rendered = True
            self._schedule_render_work(viewport=True, live=True, immediate=True)
        self.graphics.request_redraw()

    def _surface_presented(self) -> None:
        if self._snapshot_loaded and (self.history_candles.painted_once or self.live_candle.painted_once):
            self._initial_snapshot_painted = True

    def fit_chart(self) -> None:
        if not self.candles:
            return
        self.overview_restore = None
        seconds = INTERVAL_SECONDS[self.interval]
        visible = self.candles[-170:]
        data_x0, data_x1 = visible[0].time, visible[-1].time
        x_range = (data_x0 - seconds * 2, data_x1 + seconds * 16)
        y_range = self._target_y_range(data_x0, data_x1)
        view = self.price_plot.getViewBox()


        if y_range is None:
            view.setRange(xRange=x_range, padding=0)
        else:
            view.setRange(xRange=x_range, yRange=y_range, padding=0)
        self.rendered_window = None
        self._render_viewport(force=True)
        self._schedule_render_work(
            indicators=True, profiles=True, study_scale=True, history=True
        )
        self.graphics.request_redraw()

    def toggle_overview(self) -> bool:
        if not self.candles:
            return False
        if self.overview_restore is not None:
            x_range, y_range = self.overview_restore
            self.overview_restore = None
            self.price_plot.setXRange(*x_range, padding=0)
            self.price_plot.setYRange(*y_range, padding=0)
            return False
        x_range, y_range = self.price_plot.viewRange()
        self.overview_restore = (
            (float(x_range[0]), float(x_range[1])),
            (float(y_range[0]), float(y_range[1])),
        )
        seconds = INTERVAL_SECONDS[self.interval]
        visible = self.candles[-min(600, len(self.candles)):]
        self.price_plot.setXRange(
            visible[0].time - seconds * 4,
            visible[-1].time + seconds * 24,
            padding=0,
        )
        self._fit_y_to_visible(visible[0].time, visible[-1].time)
        return True

    def _set_bar_resize_active(self, active: bool) -> None:
        for batch in (self.history_candles.pixel_batch, self.live_candle.pixel_batch,
                      self.volume_overlay.history_batch, self.volume_overlay.live_batch):
            if batch.interactive_resize and not active:
                # The final resize paint restores pan margins before the next
                # gesture. Keep the transform origin so pixels do not shift.
                batch.cache_screen = QtCore.QRectF()
            batch.interactive_resize = active

    def begin_interactive_resize(self) -> None:
        """Keep cheap presentation live while deferring resize-expensive analysis."""
        if self._resize_expensive_deferred:
            return
        if not self._snapshot_loaded and not self.candles:
            return
        self._resize_expensive_deferred = True
        self._set_bar_resize_active(True)
        self._interaction_gc.set_active(self._resize_gc_token, True)
        self._sync_frame_activity()
        self.detail_timer.stop()


        self._schedule_render_work(viewport=True, live=True, immediate=False)

    def end_interactive_resize(self) -> None:
        if not self._resize_expensive_deferred:
            return
        self._resize_expensive_deferred = False
        self._set_bar_resize_active(False)
        self._interaction_gc.set_active(self._resize_gc_token, False)
        self._sync_frame_activity()
        self._position_overlay_labels()
        self._schedule_render_work(
            viewport=True,
            live=True,
            indicators=self._has_active_rendered_indicators(),
            profiles=True,
            study_scale=True,
            history=True,
            immediate=True,
        )
        self.graphics.request_redraw()
        if self._snapshot_loaded and self.candles:
            QTimer.singleShot(0, self, self._ensure_snapshot_visible)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        if self.price_axis_focus_overlay is not None:
            self.price_axis_focus_overlay.apply_theme(theme)
        self.visible_profile.background = theme["bg"]
        self.session_profile.background = theme["bg"]
        self.graphics.setBackground(theme["bg"])
        for plot in (
            self.price_plot,
            self.oi_plot,
            self.atr_plot,
            self.funding_plot,
            self.rsi_plot,
        ):
            for name in ("right", "bottom", "top"):
                axis = plot.getAxis(name)
                axis_pen = pg.mkPen(theme["border"])
                if name == "right":
                    color = axis_pen.color()
                    color.setAlphaF(color.alphaF() * 0.5)
                    axis_pen.setColor(color)
                axis.setPen(axis_pen)
                axis.setTextPen(pg.mkPen(theme["muted"]))
                axis.setGrid(False)
        candle_up = theme.get("candle_up", theme["green"])
        candle_down = theme.get("candle_down", theme["red"])
        self.history_candles.set_colors(candle_up, candle_down, theme["bg"])
        self.live_candle.set_colors(candle_up, candle_down, theme["bg"])
        self.volume_overlay.set_colors(candle_up, candle_down, theme["bg"])
        self._apply_order_rail_theme()
        self.rendered_window = None
        self._render_viewport(force=True)
        bb_pen = pg.mkPen(
            theme["cyan"],
            width=1.15,
            style=Qt.PenStyle.SolidLine,
        )
        for name, colors in (("EMA Trend", ("cyan", "amber", "purple")),
                             ("VWAP", ("amber",)),
                             ("Donchian Channels", ("green", "muted", "red"))):
            for curve, color in zip(self.trend_curves[name], colors):
                curve.setPen(pg.mkPen(theme[color], width=1.15))
        self._update_trend_label()
        self.bb_mid.setPen(bb_pen)
        self.bb_upper.setPen(bb_pen)
        self.bb_lower.setPen(bb_pen)
        self.bb_fill.setBrush(
            pg.mkBrush(opaque_overlay_color(theme["bg"], theme["cyan"], 24))
        )
        crosshair_pen = pg.mkPen(alpha_color(theme["muted"], 165), width=1, style=Qt.PenStyle.DashLine)
        self.crosshair_vertical.setPen(crosshair_pen)
        self.crosshair_horizontal.setPen(crosshair_pen)
        for line in self.crosshair_study_verticals:
            line.setPen(crosshair_pen)
        self.crosshair_time_label.setColor(theme["text"])
        self.crosshair_time_label.fill = pg.mkBrush(theme["panel2"])
        self.crosshair_time_label.update()
        for _plot, header in self.study_panes.values():
            header.theme = theme
            header.update()
        self.oi_curve.setPen(pg.mkPen(theme["purple"], width=1.4))
        self.atr_curve.setPen(pg.mkPen(theme["amber"], width=1.35))
        self.funding_curve.setPen(pg.mkPen(alpha_color(theme["muted"], 220), width=1.35))
        self.funding_positive_bars.setOpts(
            brush=pg.mkBrush(opaque_overlay_color(theme["bg"], theme["red"], 68))
        )
        self.funding_negative_bars.setOpts(
            brush=pg.mkBrush(opaque_overlay_color(theme["bg"], theme["green"], 68))
        )
        self.funding_zero_line.setPen(
            pg.mkPen(alpha_color(theme["muted"], 110), width=0.8, style=Qt.PenStyle.DashLine)
        )
        self.rsi_curve.setPen(pg.mkPen(theme["cyan"], width=1.35))
        self.rsi_upper_line.setPen(
            pg.mkPen(alpha_color(theme["red"], 145), width=1, style=Qt.PenStyle.DashLine)
        )
        self.rsi_lower_line.setPen(
            pg.mkPen(alpha_color(theme["green"], 145), width=1, style=Qt.PenStyle.DashLine)
        )
        self._render_liquidations()
        self._render_zones()
        self._schedule_render_work(profiles=True)
        self._apply_indicator_visibility()
        if self.working_order_payloads:
            self.set_working_orders(self.working_order_payloads)
        if self.current_price > 0:
            self._set_current_price(self.current_price, self.current_rising)
        if 0 <= self.auto_fib_index < len(self.auto_fib_candidates):
            self._render_auto_fibonacci()


CHART_LAYOUTS = (
    "Single",
    "2 Horizontal",
    "2 Vertical",
    "4 Grid",
)


class AuxiliaryChartPane(QtWidgets.QFrame):
    """A small, self-contained chart with an independent market stream."""

    market_changed = Signal(str, str)

    def __init__(
        self,
        theme: dict[str, str],
        market_data_factory: ChartMarketDataFactory,
        symbol: str,
        interval: str,
        parent: QtWidgets.QWidget | None = None,
        *,
        use_opengl: bool = False,
        isolate_gl_composition: bool = False,
        opengl_full_viewport: bool = False,
        native_bar_renderer: bool = True,
        lod_aggregation: bool = True,
        magnetic_order_rail_enabled: bool = True,
        presentation_clock: PresentationClock | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("auxChartPane")
        self.theme = theme
        self.symbols: tuple[str, ...] = ()
        self.symbol = self._normalize_symbol(symbol) or DEFAULT_SYMBOL
        self.interval = interval if interval in TIMEFRAMES else DEFAULT_INTERVAL
        self._controls_updating = False
        self._should_run = False
        self._start_attempt = 0

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QtWidgets.QFrame()
        header.setObjectName("auxChartHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(6, 2, 4, 2)
        header_layout.setSpacing(4)

        self.symbol_box = QtWidgets.QComboBox()
        self.symbol_box.setObjectName("auxChartSymbol")
        self.symbol_box.setEditable(True)
        self.symbol_box.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.symbol_box.setMinimumWidth(118)
        self.symbol_box.setMaximumWidth(190)
        self.symbol_box.setFixedHeight(24)
        self.symbol_box.addItem(perpetual_display_symbol(self.symbol), self.symbol)
        self.symbol_box.setCurrentIndex(0)
        set_text_role(self.symbol_box, TextRole.INSTRUMENT_SYMBOL)
        self.symbol_box.activated.connect(self._symbol_activated)
        line_edit = self.symbol_box.lineEdit()
        if line_edit is not None:
            set_text_role(line_edit, TextRole.INSTRUMENT_SYMBOL)
            line_edit.editingFinished.connect(self._symbol_entered)

        self.interval_box = QtWidgets.QComboBox()
        self.interval_box.setObjectName("auxChartInterval")
        self.interval_box.setFixedSize(62, 24)
        for timeframe in TIMEFRAMES:
            self.interval_box.addItem(
                timeframe.upper() if timeframe in {"1d", "1w"} else timeframe,
                timeframe,
            )
        self.interval_box.setCurrentIndex(TIMEFRAMES.index(self.interval))
        self.interval_box.currentIndexChanged.connect(self._interval_selected)

        self.status_label = QtWidgets.QLabel("OFF")
        self.status_label.setObjectName("auxChartStatus")
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header_layout.addWidget(self.symbol_box)
        header_layout.addWidget(self.interval_box)
        header_layout.addStretch(1)
        header_layout.addWidget(self.status_label)

        self.chart = ChartWorkspace(
            theme,
            use_opengl=use_opengl,
            isolate_gl_composition=isolate_gl_composition,
            opengl_full_viewport=opengl_full_viewport,
            native_bar_renderer=native_bar_renderer,
            lod_aggregation=lod_aggregation,
            magnetic_order_rail_enabled=magnetic_order_rail_enabled,
            presentation_clock=presentation_clock,
        )
        self.chart.setMinimumWidth(280)
        self.chart.set_indicators_enabled(False)
        self.chart.set_order_rail_market_symbol(self.symbol)
        layout.addWidget(header)
        layout.addWidget(self.chart, 1)

        self.hub: ChartMarketDataPort = market_data_factory(self)
        self.hub.bootstrap_ready.connect(self._on_bootstrap)
        self.hub.kline.connect(self._on_kline)
        self.hub.status.connect(self._on_status)
        self.hub.problem.connect(self._on_problem)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol).upper().strip().removesuffix(".P").replace("-", "")

    def set_symbols(self, symbols: list[str] | tuple[str, ...]) -> None:
        cleaned = tuple(dict.fromkeys(self._normalize_symbol(item) for item in symbols if item))
        self.symbols = cleaned
        current = self.symbol
        market_replaced = False
        blocker = QtCore.QSignalBlocker(self.symbol_box)
        self.symbol_box.clear()
        for symbol in cleaned:
            self.symbol_box.addItem(perpetual_display_symbol(symbol), symbol)
        if current not in cleaned and cleaned:
            current = DEFAULT_SYMBOL if DEFAULT_SYMBOL in cleaned else cleaned[0]
            self.symbol = current
            market_replaced = True
        index = self.symbol_box.findData(current)
        if index >= 0:
            self.symbol_box.setCurrentIndex(index)
        else:
            self.symbol_box.setEditText(perpetual_display_symbol(current))
        del blocker
        completer = self.symbol_box.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        if market_replaced:
            self.chart.set_order_rail_market_symbol(self.symbol)
        if market_replaced and self._should_run:
            self.chart.prepare_market(self.interval, reset_analysis=True)
            self.hub.switch_market(self.symbol, self.interval)
            self.market_changed.emit(self.symbol, self.interval)

    def set_market(self, symbol: str, interval: str) -> None:
        normalized = self._normalize_symbol(symbol)
        if self.symbols and normalized not in self.symbols:
            return
        if not normalized or interval not in TIMEFRAMES:
            return
        changed = normalized != self.symbol or interval != self.interval
        symbol_changed = normalized != self.symbol
        self.symbol = normalized
        self.interval = interval
        if symbol_changed:
            self.chart.set_order_rail_market_symbol(self.symbol)
        self._sync_controls()
        if changed and self._should_run:
            self.chart.prepare_market(interval, reset_analysis=symbol_changed)
            self.hub.switch_market(normalized, interval)
        if changed:
            self.market_changed.emit(normalized, interval)

    def _sync_controls(self) -> None:
        self._controls_updating = True
        try:
            index = self.symbol_box.findData(self.symbol)
            if index >= 0:
                self.symbol_box.setCurrentIndex(index)
            else:
                self.symbol_box.setEditText(perpetual_display_symbol(self.symbol))
            self.interval_box.setCurrentIndex(TIMEFRAMES.index(self.interval))
        finally:
            self._controls_updating = False

    def _symbol_activated(self, index: int) -> None:
        if self._controls_updating:
            return
        symbol = str(self.symbol_box.itemData(index) or self.symbol_box.itemText(index))
        self.set_market(symbol, self.interval)

    def _symbol_entered(self) -> None:
        if not self._controls_updating:
            self.set_market(self.symbol_box.currentText(), self.interval)
        self._sync_controls()

    def _interval_selected(self, index: int) -> None:
        if self._controls_updating or index < 0:
            return
        interval = str(self.interval_box.itemData(index) or "")
        self.set_market(self.symbol, interval)

    def set_active(self, active: bool) -> None:
        active = bool(active)
        self._should_run = active
        self.setVisible(active)
        if active:
            QTimer.singleShot(0, self, self._start_if_active)
        else:
            self._start_attempt = 0
            self.hub.stop()
            self.status_label.setText("OFF")

    def _start_if_active(self) -> None:
        if not self._should_run or self.hub.started:
            return
        if not self.chart.render_surface_ready():
            self.chart.prime_render_surface()
            self._start_attempt += 1
            if self._start_attempt == 120:
                self.status_label.setText("SURFACE PENDING")


            retry_ms = 25 if self._start_attempt < 120 else 250
            QTimer.singleShot(retry_ms, self, self._start_if_active)
            return
        self._start_attempt = 0
        self.chart.prepare_market(self.interval, reset_analysis=True)
        self.hub.start(self.symbol, self.interval)

    def _on_bootstrap(self, payload: dict[str, Any]) -> None:
        if (
            str(payload.get("symbol") or "").upper() == self.symbol
            and str(payload.get("interval") or "") == self.interval
        ):
            self.chart.set_snapshot(payload)

    def _on_kline(self, event: dict[str, Any]) -> None:
        row = event.get("k") or {}
        if (
            str(event.get("s") or "").upper() == self.symbol
            and str(row.get("i") or "") == self.interval
        ):
            self.chart.update_kline(event)

    def _on_status(self, message: str, live: bool) -> None:
        self.status_label.setText("LIVE" if live else str(message).upper())

    def _on_problem(self, _message: str) -> None:
        self.status_label.setText("OFFLINE")

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.chart.apply_theme(theme)

    def state(self) -> dict[str, str]:
        return {"symbol": self.symbol, "interval": self.interval}

    def stop(self) -> None:
        self._should_run = False
        self.hub.stop()


class MultiChartContainer(QtWidgets.QWidget):
    """Hosts the canonical chart plus up to three independent chart panes.

    Auxiliary panes are materialized lazily the first time a multi-chart
    layout is requested. The default Single layout therefore does not pay for
    three additional ChartWorkspace + MarketDataHub constructions at startup.
    State intended for those panes is retained and replayed on materialization.
    """

    auxiliary_working_order_moved = Signal(object)
    auxiliary_order_rail_execution_requested = Signal(str, object, object)
    auxiliary_order_rail_cancel_requested = Signal(str, object, object)
    auxiliary_order_rail_leverage_requested = Signal(str, int)
    auxiliary_market_changed = Signal(str, str, object)
    interaction_priority_changed = Signal(bool)
    interaction_driver_changed = Signal(object)
    chart_added = Signal(object)

    def __init__(
        self,
        primary_chart: ChartWorkspace,
        theme: dict[str, str],
        market_data_factory: ChartMarketDataFactory,
        parent: QtWidgets.QWidget | None = None,
        *,
        use_opengl: bool = False,
        isolate_gl_composition: bool = False,
        opengl_full_viewport: bool = False,
        native_bar_renderer: bool = True,
        lod_aggregation: bool = True,
        magnetic_order_rail_enabled: bool = True,
        presentation_clock: PresentationClock | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("multiChartContainer")
        self.primary_chart = primary_chart
        self._interacting_charts: set[ChartWorkspace] = set()
        self._interaction_order: dict[ChartWorkspace, None] = {}
        self._interaction_chart: ChartWorkspace | None = None
        primary_chart.interaction_priority_changed.connect(
            lambda active: self._chart_interaction(primary_chart, active)
        )
        primary_chart.interaction_started.connect(
            lambda: self._chart_interaction_started(primary_chart)
        )
        self.layout_mode = "Single"
        self.workspace_active = True
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(2)
        self._market_data_factory = market_data_factory
        self._use_opengl = bool(use_opengl)
        self._isolate_gl_composition = bool(isolate_gl_composition)
        self._opengl_full_viewport = bool(opengl_full_viewport)
        self._native_bar_renderer = bool(native_bar_renderer)
        self._lod_aggregation = bool(lod_aggregation)
        self._magnetic_order_rail_enabled = bool(magnetic_order_rail_enabled)
        self._presentation_clock = presentation_clock
        self._auxiliary_theme = dict(theme)
        self._volume_bar_height_percent = primary_chart.volume_bar_height_setting()
        self._auxiliary_defaults = (
            ("ETHUSDT", "15m"),
            ("SOLUSDT", "1h"),
            ("BNBUSDT", "4h"),
        )
        self.auxiliary: list[AuxiliaryChartPane] = []
        self._auxiliary_states: list[dict[str, str]] = [
            {"symbol": symbol, "interval": interval}
            for symbol, interval in self._auxiliary_defaults
        ]
        self._auxiliary_symbols: list[str] = []
        self._auxiliary_rail_config: dict[str, Any] | None = None
        self._auxiliary_rail_preset: tuple[dict[str, Any], str] | None = None
        self.set_layout_mode("Single")

    @staticmethod
    def _panes_required(mode: str) -> int:
        if mode == "4 Grid":
            return 3
        if mode in ("2 Horizontal", "2 Vertical"):
            return 1
        return 0

    def _chart_interaction(self, chart: ChartWorkspace, active: bool) -> None:
        previous = bool(self._interacting_charts)
        if active:
            self._interacting_charts.add(chart)
            self._interaction_order.pop(chart, None)
            self._interaction_order[chart] = None
        else:
            self._interacting_charts.discard(chart)
            self._interaction_order.pop(chart, None)
        current = bool(self._interacting_charts)
        self._refresh_interaction_chart()
        if previous != current:
            self.interaction_priority_changed.emit(current)

    def _chart_interaction_started(self, chart: ChartWorkspace) -> None:
        # Gesture priority has a release tail. A new gesture can return to a
        # pane before that tail ends, without another priority True transition.
        if chart is self._interaction_chart or chart not in self._interacting_charts:
            return
        self._interaction_order.pop(chart, None)
        self._interaction_order[chart] = None
        self._refresh_interaction_chart()

    def _refresh_interaction_chart(self) -> None:
        chart = next(reversed(self._interaction_order), None)
        if chart is self._interaction_chart:
            return
        self._interaction_chart = chart
        self.interaction_driver_changed.emit(chart)

    def interaction_chart(self) -> ChartWorkspace | None:
        """Most recently manipulated visible pane, independent of release tails."""
        return next(
            (chart for chart in reversed(self._interaction_order) if chart.isVisible()),
            None,
        )

    def _ensure_auxiliary(self, count: int) -> None:
        required = max(0, min(3, int(count)))
        while len(self.auxiliary) < required:
            index = len(self.auxiliary)
            symbol, interval = self._auxiliary_defaults[index]
            pane = AuxiliaryChartPane(
                self._auxiliary_theme,
                self._market_data_factory,
                symbol,
                interval,
                self,
                use_opengl=self._use_opengl,
                isolate_gl_composition=self._isolate_gl_composition,
                opengl_full_viewport=self._opengl_full_viewport,
                native_bar_renderer=self._native_bar_renderer,
                lod_aggregation=self._lod_aggregation,
                magnetic_order_rail_enabled=self._magnetic_order_rail_enabled,
                presentation_clock=self._presentation_clock,
            )
            if self._auxiliary_symbols:
                pane.set_symbols(self._auxiliary_symbols)
            state = self._auxiliary_states[index]
            pane.set_market(
                str(state.get("symbol") or pane.symbol),
                str(state.get("interval") or pane.interval),
            )
            pane.chart.set_volume_bar_height_percent(
                self._volume_bar_height_percent
            )
            if self._auxiliary_rail_config is not None:
                pane.chart.set_order_rail_lab_config(self._auxiliary_rail_config)
            if self._auxiliary_rail_preset is not None:
                preset, name = self._auxiliary_rail_preset
                pane.chart.set_order_rail_order_preset(preset, name)
            pane.chart.order_rail_execution_requested.connect(
                lambda state, pane_ref=pane: self.auxiliary_order_rail_execution_requested.emit(
                    pane_ref.symbol, state, pane_ref.chart
                )
            )
            pane.chart.working_order_moved.connect(self.auxiliary_working_order_moved.emit)
            pane.chart.order_rail_cancel_requested.connect(
                lambda state, pane_ref=pane: self.auxiliary_order_rail_cancel_requested.emit(
                    pane_ref.symbol, state, pane_ref.chart
                )
            )
            pane.chart.order_rail_leverage_requested.connect(
                lambda leverage, pane_ref=pane: self.auxiliary_order_rail_leverage_requested.emit(
                    pane_ref.symbol, int(leverage)
                )
            )
            pane.market_changed.connect(
                lambda symbol, interval, pane_ref=pane: self.auxiliary_market_changed.emit(
                    symbol, interval, pane_ref.chart
                )
            )
            self.auxiliary.append(pane)
            pane.chart.interaction_priority_changed.connect(
                lambda active, chart=pane.chart: self._chart_interaction(chart, active)
            )
            pane.chart.interaction_started.connect(
                lambda chart=pane.chart: self._chart_interaction_started(chart)
            )
            self.chart_added.emit(pane.chart)

    def set_layout_mode(self, mode: str) -> None:
        if mode not in CHART_LAYOUTS:
            mode = "Single"
        required_panes = self._panes_required(mode)
        if required_panes:


            self._ensure_auxiliary(required_panes)
        while self.grid.count():
            self.grid.takeAt(0)
        for row in range(2):
            self.grid.setRowStretch(row, 0)
        for column in range(2):
            self.grid.setColumnStretch(column, 0)
        for pane in self.auxiliary:
            pane.set_active(False)
        self.primary_chart.setMinimumWidth(280 if mode != "Single" else 680)
        if mode == "2 Horizontal":
            self.grid.addWidget(self.primary_chart, 0, 0)
            self.grid.addWidget(self.auxiliary[0], 0, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
            self.auxiliary[0].set_active(self.workspace_active)
        elif mode == "2 Vertical":
            self.grid.addWidget(self.primary_chart, 0, 0)
            self.grid.addWidget(self.auxiliary[0], 1, 0)
            self.grid.setRowStretch(0, 1)
            self.grid.setRowStretch(1, 1)
            self.auxiliary[0].set_active(self.workspace_active)
        elif mode == "4 Grid":
            self.grid.addWidget(self.primary_chart, 0, 0)
            for index, pane in enumerate(self.auxiliary, start=1):
                self.grid.addWidget(pane, index // 2, index % 2)
                pane.set_active(self.workspace_active)
            self.grid.setRowStretch(0, 1)
            self.grid.setRowStretch(1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
        else:
            self.grid.addWidget(self.primary_chart, 0, 0)
            self.grid.setRowStretch(0, 1)
            self.grid.setColumnStretch(0, 1)
        # Parent the primary surface before exposing it. A temporary top-level
        # GL surface would be destroyed and recreated as the grid adopts it.
        self.primary_chart.show()
        self.layout_mode = mode

    def set_workspace_active(self, active: bool) -> None:
        active = bool(active)
        if active == self.workspace_active:
            return
        self.workspace_active = active
        pane_count = 3 if self.layout_mode == "4 Grid" else (
            1 if self.layout_mode in ("2 Horizontal", "2 Vertical") else 0
        )
        for index, pane in enumerate(self.auxiliary):
            pane.set_active(active and index < pane_count)

    def set_symbols(self, symbols: list[str] | tuple[str, ...]) -> None:
        cleaned = list(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol)))
        self._auxiliary_symbols = cleaned
        for pane in self.auxiliary:
            pane.set_symbols(cleaned)

    def restore_auxiliary(self, states: list[dict[str, str]]) -> None:
        cleaned = [state for state in states if isinstance(state, dict)][:3]
        for index, state in enumerate(cleaned):
            self._auxiliary_states[index] = dict(state)
        if self.auxiliary:
            for pane, state in zip(self.auxiliary, self._auxiliary_states):
                pane.set_market(
                    str(state.get("symbol") or pane.symbol),
                    str(state.get("interval") or pane.interval),
                )

    def auxiliary_state(self) -> list[dict[str, str]]:
        if self.auxiliary:
            return [pane.state() for pane in self.auxiliary]
        return [dict(state) for state in self._auxiliary_states]

    def apply_theme(self, theme: dict[str, str]) -> None:
        self._auxiliary_theme = dict(theme)
        for pane in self.auxiliary:
            pane.apply_theme(theme)

    def begin_interactive_resize(self) -> None:
        for chart in (self.primary_chart, *(pane.chart for pane in self.auxiliary)):
            if chart.isVisible():
                chart.begin_interactive_resize()

    def end_interactive_resize(self) -> None:
        for chart in (self.primary_chart, *(pane.chart for pane in self.auxiliary)):
            chart.end_interactive_resize()

    def set_volume_bar_height_percent(self, value: int) -> None:
        self._volume_bar_height_percent = max(5, min(45, int(value)))
        self.primary_chart.set_volume_bar_height_percent(
            self._volume_bar_height_percent
        )
        for pane in self.auxiliary:
            pane.chart.set_volume_bar_height_percent(
                self._volume_bar_height_percent
            )

    def set_native_bar_renderer_enabled(self, enabled: bool) -> None:
        self._native_bar_renderer = bool(enabled)
        self.primary_chart.set_native_bar_renderer_enabled(enabled)
        for pane in self.auxiliary:
            pane.chart.set_native_bar_renderer_enabled(enabled)

    def set_lod_aggregation_enabled(self, enabled: bool) -> None:
        self._lod_aggregation = bool(enabled)
        self.primary_chart.set_lod_aggregation_enabled(enabled)
        for pane in self.auxiliary:
            pane.chart.set_lod_aggregation_enabled(enabled)

    def set_magnetic_order_rail_enabled(self, enabled: bool) -> None:
        self._magnetic_order_rail_enabled = bool(enabled)
        self.primary_chart.set_magnetic_order_rail_enabled(enabled)
        for pane in self.auxiliary:
            pane.chart.set_magnetic_order_rail_enabled(enabled)

    def set_order_rail_lab_config(self, config: dict[str, Any]) -> None:
        self._auxiliary_rail_config = dict(config) if isinstance(config, dict) else None
        for pane in self.auxiliary:
            pane.chart.set_order_rail_lab_config(config)

    def set_order_rail_order_preset(
        self, preset: dict[str, Any], name: str = ""
    ) -> None:
        self._auxiliary_rail_preset = (dict(preset), str(name))
        for pane in self.auxiliary:
            pane.chart.set_order_rail_order_preset(preset, name)

    def stop(self) -> None:
        for pane in self.auxiliary:
            pane.stop()
