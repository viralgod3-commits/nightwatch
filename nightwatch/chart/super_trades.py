"""Small screen-space execution badges that leave chart gestures untouched."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from ..models import chart_y, format_price, human_number
from ..orderbook.super_trades import SUPER_TRADE_CAPACITY, SuperTradeEvent, SuperTradeFrame
from ..theme import _ensure_directional_contrast
from ..utilities import TextRole, hide_hover_tooltip, show_hover_tooltip, typography_font


@dataclass(slots=True)
class _Marker:
    event: SuperTradeEvent
    anchor: QtCore.QPointF
    badge: QtCore.QRectF | None
    text: str


class SuperTradeOverlay(QtWidgets.QWidget):
    """One transparent canvas, bounded history, and cached pixel geometry."""

    MAX_VISIBLE = 128
    MAX_LABELS = 48

    def __init__(self, graphics, theme):
        super().__init__(graphics)
        self.graphics = graphics
        self.symbol = ""
        self._events = OrderedDict()
        self._versions = OrderedDict()
        self._revision = 0
        self._layout_key = None
        self._markers: list[_Marker] = []
        self._plot_rect = QtCore.QRectF()
        self._dirty_rect = QtCore.QRectF()
        self._hovered = None
        self._font = typography_font(TextRole.UI_CAPTION)
        self._label_cache = OrderedDict()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.apply_theme(theme)
        self.hide()

    def set_market(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol == self.symbol:
            return
        self.symbol = symbol
        self._events.clear()
        self._versions.clear()
        self._markers.clear()
        self._layout_key = None
        self._revision += 1
        self.clear_hover()
        self.hide()

    def set_frame(self, frame: SuperTradeFrame) -> bool:
        if frame.symbol != self.symbol or frame.revision <= self._versions.get(frame.stream, -1):
            return False
        self._versions[frame.stream] = frame.revision
        self._versions.move_to_end(frame.stream)
        while len(self._versions) > 16:
            self._versions.popitem(last=False)
        changed = False
        for event in frame.events:
            key = (frame.stream, event.sequence)
            if self._events.get(key) != event:
                self._events[key] = event
                self._events.move_to_end(key)
                changed = True
        while len(self._events) > SUPER_TRADE_CAPACITY:
            self._events.popitem(last=False)
        if changed:
            self._revision += 1
        return changed

    def apply_theme(self, theme) -> None:
        background = QtGui.QColor(theme["bg"])
        self._colors = {}
        for side, key, fallback in (("buy", "green", "#37D28A"), ("sell", "red", "#FF6176")):
            accent = QtGui.QColor(_ensure_directional_contrast(
                theme[key], theme["bg"], fallback=fallback, target_ratio=4.5))
            fill = QtGui.QColor(*(round(background_value * .90 + accent_value * .10)
                                 for background_value, accent_value in zip(
                                     (background.red(), background.green(), background.blue()),
                                     (accent.red(), accent.green(), accent.blue()))))
            edge = QtGui.QColor(accent)
            edge.setAlpha(100)
            stem = QtGui.QColor(accent)
            stem.setAlpha(75)
            self._colors[side] = (accent, fill, edge, stem)
        self._layout_key = None
        self.update()

    def refresh(self, view, logarithmic: bool) -> None:
        if not self._events:
            self.hide()
            return
        font = typography_font(TextRole.UI_CAPTION)
        x_range, y_range = view.viewRange()
        viewport_rect = self.graphics.viewport().geometry()
        scene_rect = view.sceneBoundingRect()
        key = (self._revision, tuple(x_range), tuple(y_range), logarithmic,
               viewport_rect.getRect(), scene_rect.getRect(), font.key())
        if key == self._layout_key:
            if self._markers:
                self.show()
            return
        self._layout_key = key
        self._font = font
        self.setGeometry(self.graphics.rect())
        transform = self.graphics.viewportTransform()
        offset = QtCore.QPointF(viewport_rect.topLeft())
        plot_rect = transform.mapRect(scene_rect).translated(offset).intersected(QtCore.QRectF(self.rect()))
        self._plot_rect = plot_rect.adjusted(2, 2, -2, -2)
        old_dirty = self._dirty_rect
        self._dirty_rect = QtCore.QRectF()
        self._markers = []
        metrics = QtGui.QFontMetricsF(font)
        height = math.ceil(metrics.height()) + 5
        bottom_left = transform.map(view.mapViewToScene(QtCore.QPointF(x_range[0], y_range[0]))) + offset
        top_right = transform.map(view.mapViewToScene(QtCore.QPointF(x_range[1], y_range[1]))) + offset
        x_scale = (top_right.x() - bottom_left.x()) / max(x_range[1] - x_range[0], 1e-30)
        y_scale = (top_right.y() - bottom_left.y()) / max(y_range[1] - y_range[0], 1e-30)
        candidates = []
        for event in self._events.values():
            x = event.event_time_ms / 1000.0
            y = chart_y(event.price, logarithmic)
            if x_range[0] <= x <= x_range[1] and y_range[0] <= y <= y_range[1]:
                candidates.append((event, x, y))
        candidates.sort(key=lambda row: row[0].notional, reverse=True)
        occupied = {}
        label_count = 0
        left_edge, top_edge = self._plot_rect.left(), self._plot_rect.top()
        right_edge, bottom_edge = self._plot_rect.right(), self._plot_rect.bottom()
        font_key = font.key()
        for event, x, y in candidates[:self.MAX_VISIBLE]:
            anchor = QtCore.QPointF(bottom_left.x() + (x - x_range[0]) * x_scale,
                                   bottom_left.y() + (y - y_range[0]) * y_scale)
            cache_key = (event.notional, font_key)
            label = self._label_cache.get(cache_key)
            if label is None:
                text = human_number(event.notional)
                label = (text, math.ceil(metrics.horizontalAdvance(text)) + 19)
                self._label_cache[cache_key] = label
                while len(self._label_cache) > SUPER_TRADE_CAPACITY:
                    self._label_cache.popitem(last=False)
            text, width = label
            badge = None
            if label_count < self.MAX_LABELS and width <= self._plot_rect.width():
                direction = 1 if event.aggressor_side == "buy" else -1
                left = max(left_edge, min(anchor.x() - width / 2, right_edge - width))
                for lane in range(3):
                    center = anchor.y() + direction * (17 + lane * (height + 3))
                    top = center - height / 2
                    if top < top_edge or top + height > bottom_edge:
                        continue
                    box = (left - 3, top - 2, left + width + 3, top + height + 2)
                    cells = tuple((column, row)
                                  for column in range(math.floor(box[0] / 80), math.floor(box[2] / 80) + 1)
                                  for row in range(math.floor(box[1] / 24), math.floor(box[3] / 24) + 1))
                    if any(box[0] < other[2] and box[2] > other[0]
                           and box[1] < other[3] and box[3] > other[1]
                           for cell in cells for other in occupied.get(cell, ())):
                        continue
                    badge = QtCore.QRectF(left, top, width, height)
                    for cell in cells:
                        occupied.setdefault(cell, []).append(box)
                    label_count += 1
                    break
            marker = _Marker(event, anchor, badge, text)
            self._markers.append(marker)
            bounds = QtCore.QRectF(anchor.x() - 5, anchor.y() - 5, 10, 10)
            if badge is not None:
                bounds = bounds.united(badge)
            self._dirty_rect = self._dirty_rect.united(bounds.adjusted(-2, -2, 2, 2))
        if not self._markers:
            self.clear_hover()
            self.hide()
            return
        self.show()
        self.update(old_dirty.united(self._dirty_rect).toAlignedRect())

    def paintEvent(self, event) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setClipRect(self._plot_rect)
        painter.setFont(self._font)
        for marker in self._markers:
            accent, fill, edge, stem = self._colors[marker.event.aggressor_side]
            painter.setPen(QtGui.QPen(stem, .8))
            if marker.badge is not None:
                endpoint = QtCore.QPointF(marker.badge.center().x(),
                                         marker.badge.top() if marker.event.aggressor_side == "buy"
                                         else marker.badge.bottom())
                painter.drawLine(QtCore.QLineF(marker.anchor, endpoint))
            painter.setPen(QtGui.QPen(edge, .8))
            painter.setBrush(accent)
            painter.drawEllipse(marker.anchor, 2, 2)
            if marker.event.print_count > 1:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(marker.anchor, 3.5, 3.5)
            if marker.badge is None:
                continue
            painter.setBrush(fill)
            painter.drawRoundedRect(marker.badge, 4, 4)
            painter.setPen(accent)
            direction = -1 if marker.event.aggressor_side == "buy" else 1
            center = QtCore.QPointF(marker.badge.left() + 7, marker.badge.center().y())
            chevron = QtGui.QPainterPath()
            chevron.moveTo(center.x() - 2, center.y() - direction)
            chevron.lineTo(center.x(), center.y() + 2 * direction)
            chevron.lineTo(center.x() + 2, center.y() - direction)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(chevron)
            painter.drawText(marker.badge.adjusted(13, 0, -5, 0),
                             Qt.AlignmentFlag.AlignCenter, marker.text)

    def hit_test(self, position: QtCore.QPointF) -> SuperTradeEvent | None:
        for marker in self._markers:
            if marker.badge is not None and marker.badge.contains(position):
                return marker.event
        for marker in self._markers:
            delta = marker.anchor - position
            if delta.x() ** 2 + delta.y() ** 2 <= 36:
                return marker.event
        return None

    def hover_at(self, position: QtCore.QPointF) -> None:
        event = self.hit_test(position) if self.isVisible() else None
        if event is None:
            self.clear_hover()
            return
        if event == self._hovered:
            return
        self._hovered = event
        show_hover_tooltip(self, self.tooltip_text(event), QtGui.QCursor.pos())

    def clear_hover(self) -> None:
        if self._hovered is not None:
            self._hovered = None
            hide_hover_tooltip(self)

    def tooltip_text(self, event: SuperTradeEvent) -> str:
        quote = "USDC" if self.symbol.endswith("USDC") else "USDT"
        base = self.symbol.removesuffix(quote)
        burst = event.print_count > 1
        title = f"{event.aggressor_side.upper()} {'BURST' if burst else 'LARGE TRADE'} · {self.symbol}"
        quantity = f"{event.quantity:,.8f}".rstrip("0").rstrip(".")
        stamp = datetime.fromtimestamp(event.event_time_ms / 1000.0, timezone.utc)
        lines = [title, f"Total {human_number(event.notional)} {quote} · {quantity} {base}",
                 f"{'Average price' if burst else 'Price'} {format_price(event.price)}"]
        if burst:
            lines.extend((f"{event.print_count} prints · {event.event_time_ms - event.start_time_ms} ms",
                          f"Range {format_price(event.low_price)} – {format_price(event.high_price)}"))
        lines.append(f"{event.notional / event.tape_threshold:.1f}× tape threshold "
                     f"({human_number(event.tape_threshold)} {quote} at detection)")
        lines.append(stamp.strftime("%d %b %Y · %H:%M:%S.") + f"{event.event_time_ms % 1000:03d} UTC")
        if burst:
            lines.append("Inferred same-side sweep; participant identity is unavailable.")
        return "\n".join(lines)
