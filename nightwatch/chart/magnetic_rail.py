"""Magnetic order-rail configuration, interaction policy and Qt presentation."""
from __future__ import annotations

from typing import Any
ORDER_RAIL_STYLE_PRESETS: dict[str, dict[str, Any]] = {'Flux Arc / Photon Sweep': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Pulse Lance': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Vector Stream': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Reactor Wave': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Prism Packets': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Scanline': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Quantum Dash': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Comet Trail': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Interference': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}, 'Flux Arc / Charge Relay': {'body_animation': 'ray', 'animation_enabled': True, 'frame_cap': 0, 'animation_speed': 1.1, 'glow_strength': 22, 'line_opacity': 92, 'line_width': 1.05, 'line_pattern': 'solid', 'canvas_bloom': 10, 'spark_count': 0, 'spark_size': 1.0, 'spark_spread': 8}}
_LEGACY_RAIL_STYLE_ALIASES = {'Photon Ray': 'Flux Arc / Photon Sweep', 'Pulse Lance': 'Flux Arc / Pulse Lance', 'Vector Stream': 'Flux Arc / Vector Stream', 'Reactor Wave': 'Flux Arc / Reactor Wave', 'Flux Arc': 'Flux Arc / Photon Sweep', 'Prism Surge': 'Flux Arc / Prism Packets', 'Scanline': 'Flux Arc / Scanline', 'Quantum Dash': 'Flux Arc / Quantum Dash', 'Neon Edge': 'Flux Arc / Photon Sweep', 'Glass Dock': 'Flux Arc / Scanline', 'Reactor': 'Flux Arc / Reactor Wave', 'Pulse Beam': 'Flux Arc / Pulse Lance', 'Stealth': 'Flux Arc / Scanline', 'Vector': 'Flux Arc / Vector Stream', 'Ion Storm': 'Flux Arc / Pulse Lance', 'Starforge': 'Flux Arc / Reactor Wave'}
ORDER_RAIL_USER_KEYS = ('style', 'armed_transition_enabled', 'cancel_implosion_enabled', 'active_line_style', 'active_animation', 'active_opacity', 'active_width')
_ORDER_RAIL_BASE_DEFAULTS: dict[str, Any] = {
    'active_line_style': 'solid',
    'active_animation': 'packets',
    'active_opacity': 28,
    'active_width': 0.8,
    'style': 'Flux Arc / Photon Sweep',
    'armed_transition_enabled': True,
    'cancel_implosion_enabled': True,
    'animation_enabled': True,
    'frame_cap': 0,
    'animation_speed': 1.1,
    'body_animation': 'ray',
    'glow_strength': 22,
    'panel_opacity': 100,
    'line_opacity': 94,
    'line_width': 1.15,
    'line_pattern': 'solid',
    'corner_radius': 8,
    'vertical_offset': 0,
    'drag_hitbox': 11,
    'release_flash': True,
    'live_drag_update': True,
    'canvas_bloom': 14,
    'spark_count': 0,
    'spark_size': 1.0,
    'spark_spread': 8,
}
ORDER_RAIL_LAB_DEFAULTS: dict[str, Any] = dict(_ORDER_RAIL_BASE_DEFAULTS)
ORDER_RAIL_LAB_DEFAULTS.update(ORDER_RAIL_STYLE_PRESETS['Flux Arc / Photon Sweep'])

def _rail_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'1', 'true', 'yes', 'on'}:
            return True
        if lowered in {'0', 'false', 'no', 'off'}:
            return False
    return bool(default)

def _rail_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    return max(low, min(high, number))

def _rail_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(low, min(high, number))
ORDER_RAIL_ORDER_PRESET_DEFAULTS: dict[str, dict[str, Any]] = {'Limit Entry': {'orderType': 'LIMIT', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 25, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'CONTRACT_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'inherit'}, 'Stop Market': {'orderType': 'STOP_MARKET', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 100, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'MARK_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'dash'}, 'Stop Limit': {'orderType': 'STOP', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 100, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'MARK_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'dash'}, 'Take Profit Market': {'orderType': 'TAKE_PROFIT_MARKET', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 100, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'MARK_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'dot'}, 'Take Profit Limit': {'orderType': 'TAKE_PROFIT', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 100, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'MARK_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'dash'}, 'Trailing Stop': {'orderType': 'TRAILING_STOP_MARKET', 'orderRole': 'ENTRY', 'reduceOnly': False, 'sizePercent': 100, 'leverage': 5, 'timeInForce': 'GTC', 'workingType': 'MARK_PRICE', 'priceProtect': True, 'callbackRate': 0.5, 'limitOffsetPercent': 0.0, 'linePattern': 'segmented'}}

def normalized_order_rail_order_preset(source: dict[str, Any] | None=None) -> dict[str, Any]:
    v = dict(ORDER_RAIL_ORDER_PRESET_DEFAULTS['Limit Entry'])
    if isinstance(source, dict):
        v.update(source)
    typ = str(v.get('orderType', 'LIMIT')).upper()
    allowed = {'LIMIT', 'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'TRAILING_STOP_MARKET'}
    v['orderType'] = typ if typ in allowed else 'LIMIT'
    legacy_role = str(v.get('orderRole', 'ENTRY')).upper()
    v['orderRole'] = 'ENTRY'
    v['reduceOnly'] = _rail_bool(v.get('reduceOnly'), False)
    if legacy_role in {'TP', 'SL'}:
        v['reduceOnly'] = False
    size = _rail_int(v.get('sizePercent'), 25, 1, 100)
    v['sizePercent'] = min((25, 50, 75, 100), key=lambda option: abs(option - size))
    v['leverage'] = _rail_int(v.get('leverage'), 5, 1, 125)
    tif = str(v.get('timeInForce', 'GTC')).upper()
    v['timeInForce'] = tif if tif in {'GTC', 'GTX', 'IOC', 'FOK'} else 'GTC'
    working = str(v.get('workingType', 'CONTRACT_PRICE')).upper()
    v['workingType'] = working if working in {'CONTRACT_PRICE', 'MARK_PRICE'} else 'CONTRACT_PRICE'
    v['priceProtect'] = _rail_bool(v.get('priceProtect'), True)
    v['callbackRate'] = _rail_float(v.get('callbackRate'), 0.5, 0.1, 10.0)
    v['limitOffsetPercent'] = _rail_float(v.get('limitOffsetPercent'), 0.0, -5.0, 5.0)
    pattern = str(v.get('linePattern', 'inherit')).lower()
    v['linePattern'] = pattern if pattern in {'inherit', 'solid', 'dash', 'dot', 'segmented'} else 'inherit'
    return v

def normalized_order_rail_config(source: dict[str, Any] | None=None) -> dict[str, Any]:
    """Resolve the magnetic rail visual configuration.

    Legacy tooltip/tray settings are intentionally ignored: the rail now uses
    inline body controls only. Old settings remain loadable because unknown
    keys are discarded rather than migrated into presentation behavior.
    """
    source = source if isinstance(source, dict) else {}
    raw_style = str(source.get('style', ORDER_RAIL_LAB_DEFAULTS['style']))
    style = _LEGACY_RAIL_STYLE_ALIASES.get(raw_style, raw_style)
    if style not in ORDER_RAIL_STYLE_PRESETS:
        style = ORDER_RAIL_LAB_DEFAULTS['style']
    values = dict(_ORDER_RAIL_BASE_DEFAULTS)
    values['active_line_style'] = source.get('active_line_style', 'solid') if source.get('active_line_style', 'solid') in {'solid', 'dash', 'dot'} else 'solid'
    values['active_animation'] = source.get('active_animation', 'packets') if source.get('active_animation', 'packets') in {'packets', 'pulse', 'scan', 'off'} else 'packets'
    values['active_opacity'] = _rail_int(source.get('active_opacity', 28), 28, 10, 45)
    values['active_width'] = _rail_float(source.get('active_width', 0.8), 0.8, 0.5, 1.2)
    values['style'] = style
    values.update(ORDER_RAIL_STYLE_PRESETS[style])
    values['armed_transition_enabled'] = _rail_bool(source.get('armed_transition_enabled', ORDER_RAIL_LAB_DEFAULTS['armed_transition_enabled']), True)
    values['cancel_implosion_enabled'] = _rail_bool(source.get('cancel_implosion_enabled', ORDER_RAIL_LAB_DEFAULTS['cancel_implosion_enabled']), True)
    values['animation_enabled'] = _rail_bool(values['animation_enabled'], True)
    values['frame_cap'] = _rail_int(values['frame_cap'], 0, 0, 360)
    values['animation_speed'] = _rail_float(values['animation_speed'], 1.0, 0.1, 4.0)
    values['glow_strength'] = _rail_int(values['glow_strength'], 20, 0, 100)
    values['panel_opacity'] = _rail_int(values['panel_opacity'], 100, 70, 100)
    values['line_opacity'] = _rail_int(values['line_opacity'], 92, 10, 100)
    values['line_width'] = _rail_float(values['line_width'], 1.0, 0.5, 4.0)
    pattern = str(values.get('line_pattern', 'solid')).lower()
    values['line_pattern'] = pattern if pattern in {'solid', 'dash', 'dot', 'segmented'} else 'solid'
    values['corner_radius'] = _rail_int(values['corner_radius'], 8, 0, 18)
    values['canvas_bloom'] = _rail_int(values['canvas_bloom'], 8, 0, 100)
    values['spark_count'] = _rail_int(values['spark_count'], 0, 0, 24)
    values['spark_size'] = _rail_float(values['spark_size'], 1.0, 0.5, 4.0)
    values['spark_spread'] = _rail_int(values['spark_spread'], 8, 4, 48)
    values['vertical_offset'] = _rail_int(values['vertical_offset'], 0, -120, 120)
    values['drag_hitbox'] = _rail_int(values['drag_hitbox'], 11, 4, 30)
    values['release_flash'] = _rail_bool(values['release_flash'], True)
    values['live_drag_update'] = _rail_bool(values['live_drag_update'], True)
    return values

from enum import Enum
from typing import Any, Mapping

class RailIntent(str, Enum):
    CYCLE_SIZE = 'cycle_size'
    CYCLE_LEVERAGE = 'cycle_leverage'
    TOGGLE_REDUCE = 'toggle_reduce'


INTERACTIVE_KEYS = ('size_cycle', 'leverage_cycle', 'reduce')


def control_intent(key: str) -> RailIntent | None:
    if key == 'size_cycle':
        return RailIntent.CYCLE_SIZE
    if key == 'leverage_cycle':
        return RailIntent.CYCLE_LEVERAGE
    if key == 'reduce':
        return RailIntent.TOGGLE_REDUCE
    return None


def hit_control_key(point: Any, layout: Mapping[str, Any], *, blocked: bool) -> str:
    if blocked:
        return ''
    for key in INTERACTIVE_KEYS:
        rect = layout.get(key)
        if rect is not None and (not rect.isEmpty()) and rect.contains(point):
            return key
    return ''

import math
import time
from typing import Any
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QTimer, Qt, Signal
from ..models import format_price
from ..utilities import TextRole, apply_text_render_hints, typography_controller, typography_font

def _chart_axis_font() -> QtGui.QFont:
    return typography_font(TextRole.CHART_AXIS)

class CurrentPriceLineOverlay(QtWidgets.QWidget):
    """Persistent screen-space LTP locator independent of the graphics scene.

    The candle renderer may use native OpenGL inside the QGraphicsView viewport.
    Keeping this locator as a child of the stable QGraphicsView means scene
    clipping, partial viewport updates, native-paint ordering, and OpenGL/raster
    viewport replacement cannot silently drop the line during navigation.
    """
    LINE_COLOR = QtGui.QColor('#FFFFFF')
    LINE_ALPHA = 200
    LINE_WIDTH = 0.45

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.line_y = 0.0
        color = QtGui.QColor(self.LINE_COLOR)
        color.setAlpha(self.LINE_ALPHA)
        self._line_pen = QtGui.QPen(color)
        self._line_pen.setWidthF(self.LINE_WIDTH)
        self._line_pen.setCosmetic(True)
        self._line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._line_pen.setDashPattern([1.0, 3.0])
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def set_line_y(self, line_y: float) -> None:
        line_y = float(line_y)
        if abs(line_y - self.line_y) < 0.125:
            return
        self.line_y = line_y
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if self.width() <= 0 or not math.isfinite(self.line_y):
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._line_pen)
        painter.drawLine(QtCore.QLineF(0.0, self.line_y, float(self.width()), self.line_y))

class PriceAxisFocusOverlay(QtWidgets.QWidget):
    """Mouse-transparent axis layer for live-price focus and nearby-label de-emphasis.

    The native AxisItem remains authoritative and fully interactive underneath.
    This widget only masks competing tick text around active prices and paints the
    live-price value directly on the price axis. The magnetic rail HUD is always
    stacked above it when both occupy the same area.
    """
    FOCUS_RADIUS = 24.0
    BADGE_HEIGHT = 20.0

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.theme = theme
        self.current_y: float | None = None
        self.current_text = ''
        self.current_color = QtGui.QColor(theme.get('cyan', '#65b7d5'))
        self.current_tooltip = ''
        self.rail_y: float | None = None
        self.cursor_y: float | None = None
        self.cursor_text = ''
        self.cursor_color = QtGui.QColor(theme.get('text', '#d4dbe1'))
        self._current_color_key = self.current_color.name(QtGui.QColor.NameFormat.HexArgb)
        self._cursor_color_key = self.cursor_color.name(QtGui.QColor.NameFormat.HexArgb)
        self._bg_color = QtGui.QColor(theme.get('bg', '#050709'))
        self._border_color = QtGui.QColor(theme.get('border', '#28343d'))
        self._current_text_color = QtGui.QColor(theme.get('current_price_text', theme.get('text', '#d4dbe1')))
        self._cursor_text_color = QtGui.QColor(theme.get('text', '#d4dbe1'))
        self._accessible_description = ''
        self._axis_text_left = 0.0
        self._axis_text_width = 0.0
        self._axis_font = _chart_axis_font()
        self._axis_font_metrics = QtGui.QFontMetricsF(self._axis_font)
        self._current_text_width = 0.0
        self._cursor_text_width = 0.0
        typography_controller().changed.connect(self._refresh_typography_cache)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def _refresh_typography_cache(self) -> None:
        self._axis_font = _chart_axis_font()
        self._axis_font_metrics = QtGui.QFontMetricsF(self._axis_font)
        self._current_text_width = self._axis_font_metrics.horizontalAdvance(self.current_text) if self.current_text else 0.0
        self._cursor_text_width = self._axis_font_metrics.horizontalAdvance(self.cursor_text) if self.cursor_text else 0.0
        self.update()

    @staticmethod
    def _alpha(color: QtGui.QColor, alpha: int) -> QtGui.QColor:
        out = QtGui.QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self._bg_color = QtGui.QColor(theme.get('bg', '#050709'))
        self._border_color = QtGui.QColor(theme.get('border', '#28343d'))
        self._current_text_color = QtGui.QColor(theme.get('current_price_text', theme.get('text', '#d4dbe1')))
        self._cursor_text_color = QtGui.QColor(theme.get('text', '#d4dbe1'))
        self.update()

    def set_axis_text_band(self, local_left: float, width: float) -> None:
        """Track the native right-axis tick-text origin, not the axis box edge."""
        left = float(local_left)
        width = max(1.0, float(width))
        if abs(left - self._axis_text_left) < 0.25 and abs(width - self._axis_text_width) < 0.25:
            return
        self._axis_text_left = left
        self._axis_text_width = width
        self.update()

    def _sync_accessible_description(self) -> None:
        description = f'Current price {self.current_text}. {self.current_tooltip}'.strip()
        if description != self._accessible_description:
            self._accessible_description = description
            self.setAccessibleDescription(description)

    def set_current_tooltip(self, text: str) -> None:
        """Update countdown/accessibility text without repainting or remapping."""
        text = str(text or '')
        if text == self.current_tooltip:
            return
        self.current_tooltip = text
        self._sync_accessible_description()

    def set_focus_state(self, *, current_y: float | None, current_text: str, current_color: str | QtGui.QColor, current_tooltip: str='', rail_y: float | None=None, cursor_y: float | None=None, cursor_text: str='', cursor_color: str | QtGui.QColor | None=None) -> None:
        new_current_y = None if current_y is None else float(current_y)
        new_current_text = str(current_text or '')
        new_tooltip = str(current_tooltip or '')
        new_rail_y = None if rail_y is None else float(rail_y)
        new_cursor_y = None if cursor_y is None else float(cursor_y)
        new_cursor_text = str(cursor_text or '')
        current_key = current_color.name(QtGui.QColor.NameFormat.HexArgb) if isinstance(current_color, QtGui.QColor) else str(current_color)
        cursor_source = cursor_color if cursor_color is not None else self.theme.get('text', '#d4dbe1')
        cursor_key = cursor_source.name(QtGui.QColor.NameFormat.HexArgb) if isinstance(cursor_source, QtGui.QColor) else str(cursor_source)
        geometry_changed = new_current_y != self.current_y or new_rail_y != self.rail_y or new_cursor_y != self.cursor_y
        content_changed = new_current_text != self.current_text or new_tooltip != self.current_tooltip or new_cursor_text != self.cursor_text or (current_key != self._current_color_key) or (cursor_key != self._cursor_color_key)
        if not geometry_changed and (not content_changed):
            return
        self.current_y = new_current_y
        self.current_text = new_current_text
        self.current_tooltip = new_tooltip
        self.rail_y = new_rail_y
        self.cursor_y = new_cursor_y
        self.cursor_text = new_cursor_text
        if current_key != self._current_color_key:
            self.current_color = QtGui.QColor(current_color)
            self._current_color_key = current_key
        if cursor_key != self._cursor_color_key:
            self.cursor_color = QtGui.QColor(cursor_source)
            self._cursor_color_key = cursor_key
        if content_changed:
            self._current_text_width = self._axis_font_metrics.horizontalAdvance(self.current_text) if self.current_text else 0.0
            self._cursor_text_width = self._axis_font_metrics.horizontalAdvance(self.cursor_text) if self.cursor_text else 0.0
            self._sync_accessible_description()
        visible = self.current_y is not None or self.rail_y is not None or (self.cursor_y is not None and bool(self.cursor_text))
        if self.isVisible() != visible:
            self.setVisible(visible)
        if visible:
            self.update()


    def _draw_axis_value(self, painter: QtGui.QPainter, y_value: float, value: str, accent: QtGui.QColor, text_color: QtGui.QColor, text_width: float) -> None:
        y = max(self.BADGE_HEIGHT * 0.5, min(float(self.height()) - self.BADGE_HEIGHT * 0.5, y_value))
        text_left = max(0.0, min(float(self.width()) - 1.0, self._axis_text_left))
        text_right = max(text_left + 1.0, min(float(self.width()) - 1.0, text_left + self._axis_text_width))
        painter.setFont(self._axis_font)
        text_width = max(1.0, float(text_width))
        body_left = max(0.0, text_left - 5.0)
        body_right = min(text_right, text_left + text_width + 4.0)
        if body_right <= body_left:
            body_right = min(float(self.width()), body_left + 1.0)
        rect = QtCore.QRectF(body_left, y - self.BADGE_HEIGHT * 0.5, max(1.0, body_right - body_left), self.BADGE_HEIGHT)
        painter.setPen(QtGui.QPen(self._alpha(accent, 150), 1.0))
        painter.setBrush(self._alpha(self._bg_color, 248))
        painter.drawRoundedRect(rect, 3.0, 3.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(QtCore.QRectF(rect.left() + 1.0, rect.top() + 3.0, 2.0, max(1.0, rect.height() - 6.0)), 1.0, 1.0)
        painter.setPen(text_color)
        value_rect = QtCore.QRectF(text_left, rect.top(), max(1.0, rect.right() - text_left - 3.0), rect.height())
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(self._alpha(self._border_color, 74), 1.0))
        painter.drawRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0), 2.5, 2.5)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        apply_text_render_hints(painter)
        cursor_near_current = self.cursor_y is not None and self.cursor_text and (self.current_y is not None) and (abs(self.cursor_y - self.current_y) < self.BADGE_HEIGHT)
        if self.current_y is not None and self.current_text and (not cursor_near_current) and (not (self.rail_y is not None and abs(self.current_y - self.rail_y) < 27.0)):
            self._draw_axis_value(painter, self.current_y, self.current_text, self.current_color, self._current_text_color, self._current_text_width)
        if self.cursor_y is not None and self.cursor_text:
            self._draw_axis_value(painter, self.cursor_y, self.cursor_text, self.cursor_color, self._cursor_text_color, self._cursor_text_width)

class MagneticRailLineOverlay(QtWidgets.QWidget):
    """Small transparent rail band; never repaints the whole chart viewport."""
    ANIMATED_STYLES = set(ORDER_RAIL_STYLE_PRESETS)
    _FLUX_ARC_STEPS = 28
    _FLUX_ARC_SAMPLES = tuple(((index / 28.0, math.sin(math.pi * index / 28.0)) for index in range(29)))

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.theme: dict[str, str] = {}
        self._secondary_color = QtGui.QColor('#a970ff')
        self._tertiary_color = QtGui.QColor('#f0b85a')
        self._highlight_color = QtGui.QColor('#e7edf2')
        self._background_color = QtGui.QColor('#030507')
        self.config = normalized_order_rail_config()
        self.line_y = 0.0
        self.color = QtGui.QColor('#65b7d5')
        self._idle_color = QtGui.QColor(self.color)
        self.phase = 0.0
        self.dragging = False
        self.armed = False
        self.side = 'BUY'
        self.arm_progress = 0.0
        self.implosion_progress = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.hide()

    @staticmethod
    def _alpha(color: QtGui.QColor, alpha: int) -> QtGui.QColor:
        out = QtGui.QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def _refresh_theme_color_cache(self) -> None:
        self._secondary_color = QtGui.QColor(self.theme.get('rail_secondary', self.theme.get('purple', '#a970ff')))
        self._tertiary_color = QtGui.QColor(self.theme.get('rail_warning', self.theme.get('amber', '#f0b85a')))
        self._highlight_color = QtGui.QColor(self.theme.get('text', '#e7edf2'))
        self._background_color = QtGui.QColor(self.theme.get('bg', '#030507'))

    def set_state(self, *, theme: dict[str, str], config: dict[str, Any], line_y: float, color: QtGui.QColor, phase: float, dragging: bool, armed: bool=False, side: str='BUY', arm_progress: float=0.0, implosion_progress: float=0.0) -> None:
        arm_progress = max(0.0, min(1.0, float(arm_progress)))
        implosion_progress = max(0.0, min(1.0, float(implosion_progress)))
        normalized_side = 'SELL' if str(side).upper() == 'SELL' else 'BUY'
        theme_changed = self.theme is not theme
        changed = theme_changed or self.config != config or abs(self.line_y - float(line_y)) > 0.25 or (self.color.rgba() != color.rgba()) or (self.dragging != bool(dragging)) or (self.armed != bool(armed)) or (self.side != normalized_side) or (abs(self.arm_progress - arm_progress) > 0.001) or (abs(self.implosion_progress - implosion_progress) > 0.001)
        self.theme = theme
        if theme_changed:
            self._refresh_theme_color_cache()
        self.config = config
        self.line_y = float(line_y)
        if not armed and arm_progress <= 0.001:
            self._idle_color = QtGui.QColor(color)
        self.color = QtGui.QColor(color)
        self.phase = float(phase)
        self.dragging = bool(dragging)
        self.armed = bool(armed)
        self.side = normalized_side
        self.arm_progress = arm_progress
        self.implosion_progress = implosion_progress
        interactive = bool(armed and arm_progress >= 0.999 and implosion_progress <= 0)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not interactive)
        if interactive:
            self.setMask(QtGui.QRegion(0, int(self.line_y) - 5, self.width(), 11))


            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.clearMask()
        if changed:
            self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        hud = getattr(self, 'working_hud', None)
        if event.button() != Qt.MouseButton.LeftButton or hud is None or hud.submission_pending or hud.cancellation_pending:
            event.ignore()
            return
        self._rail_press = QtCore.QPointF(event.globalPosition())
        self._rail_drag = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocus()
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        origin = getattr(self, '_rail_press', None)
        hud = getattr(self, 'working_hud', None)
        if origin is None or hud is None:
            event.ignore()
            return
        delta = event.globalPosition() - origin
        if not self._rail_drag and abs(delta.x()) + abs(delta.y()) >= QtWidgets.QApplication.startDragDistance():
            self._rail_drag = True
            hud.body_drag_started.emit(origin)
        if self._rail_drag:
            hud.body_drag_moved.emit(event.globalPosition())
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and getattr(self, '_rail_press', None) is not None:
            hud = self.working_hud
            dragged = self._rail_drag
            self._rail_press = None
            self._rail_drag = False
            if dragged:
                hud.body_drag_finished.emit(True)
            else:
                hud._show_working_order_details()
            event.accept()
            return
        event.ignore()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and getattr(self, '_rail_press', None) is not None:
            self._rail_press = None
            if self._rail_drag:
                self._rail_drag = False
                self.working_hud.body_drag_finished.emit(False)
            self.releaseMouse()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event: QtCore.QEvent) -> bool:
        if event.type() in {QtCore.QEvent.Type.UngrabMouse, QtCore.QEvent.Type.WindowDeactivate, QtCore.QEvent.Type.Hide} and getattr(self, '_rail_press', None) is not None:
            self._rail_press = None
            if self._rail_drag:
                self._rail_drag = False
                self.working_hud.body_drag_finished.emit(False)
        return super().event(event)

    def animation_required(self) -> bool:
        return self.config['style'] in self.ANIMATED_STYLES

    def advance_animation(self, phase: float) -> None:
        phase = float(phase)
        if abs(phase - self.phase) < 1e-06:
            return
        self.phase = phase
        if self.isVisible() and self.animation_required():
            self.update()

    def _pen(self, color: QtGui.QColor, width: float, opacity: int, pattern: str | None=None) -> QtGui.QPen:
        pen = QtGui.QPen(self._alpha(color, opacity), float(width))
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pattern = str(pattern or self.config['line_pattern'])
        if pattern == 'dash':
            pen.setDashPattern([6.0, 4.0])
        elif pattern == 'dot':
            pen.setDashPattern([1.0, 3.0])
        elif pattern == 'segmented':
            pen.setDashPattern([10.0, 4.0, 2.0, 4.0])
        return pen

    def _flux_arc_geometry(self, x1: float) -> tuple[float, float]:
        span = min(180.0, max(96.0, self.width() * 0.22))
        return (span, max(0.0, x1 - span))

    def _draw_flux_arc_whirl(self, painter: QtGui.QPainter, *, y: float, x1: float, accent: QtGui.QColor, secondary: QtGui.QColor, highlight: QtGui.QColor) -> None:
        """Original Flux Arc whirl, shared unchanged by every body animation."""
        span = min(180.0, max(96.0, self.width() * 0.22))
        start_x = max(0.0, x1 - span)
        for lane, color, phase_offset in ((-1.0, secondary, 0.0), (1.0, accent, 0.5)):
            path = QtGui.QPainterPath()
            for index, (t, envelope) in enumerate(self._FLUX_ARC_SAMPLES):
                px = start_x + span * t
                py = y + lane * math.sin((t * 3.0 + self.phase + phase_offset) * math.tau) * 2.6 * envelope
                if index == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            painter.setPen(QtGui.QPen(self._alpha(color, 105), 0.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)
        head_x = start_x + self.phase * 1.25 % 1.0 * span
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._alpha(highlight, 225))
        painter.drawEllipse(QtCore.QPointF(head_x, y), 1.45, 1.45)

    def _draw_body_animation(self, painter: QtGui.QPainter, *, mode: str, x0: float, x1: float, y: float, width: float, alpha: int, accent: QtGui.QColor, secondary: QtGui.QColor, tertiary: QtGui.QColor, highlight: QtGui.QColor) -> None:
        if x1 <= x0 + 4.0:
            return
        length = x1 - x0
        body_line = QtCore.QLineF(x0, y, x1, y)
        if mode == 'ray':


            ray_span = max(72.0, min(168.0, length * 0.24))
            travel = (self.phase * 0.94) % 1.0
            head = x0 - ray_span + (length + ray_span * 2.0) * travel
            start = max(x0, head - ray_span)
            finish = min(x1, head)
            if finish > start:
                bloom = QtGui.QLinearGradient(start, y, finish, y)
                bloom.setColorAt(0.0, self._alpha(accent, 0))
                bloom.setColorAt(0.62, self._alpha(accent, 16))
                bloom.setColorAt(0.90, self._alpha(accent, 42))
                bloom.setColorAt(1.0, self._alpha(highlight, 78))
                bloom_pen = QtGui.QPen(QtGui.QBrush(bloom), width + 1.65)
                bloom_pen.setCosmetic(True)
                bloom_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(bloom_pen)
                painter.drawLine(QtCore.QLineF(start, y, finish, y))

                core = QtGui.QLinearGradient(start, y, finish, y)
                core.setColorAt(0.0, self._alpha(accent, 0))
                core.setColorAt(0.48, self._alpha(accent, 28))
                core.setColorAt(0.82, self._alpha(accent, 126))
                core.setColorAt(0.965, self._alpha(highlight, 232))
                core.setColorAt(1.0, self._alpha(highlight, 255))
                core_pen = QtGui.QPen(QtGui.QBrush(core), max(0.62, width * 0.88))
                core_pen.setCosmetic(True)
                core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(core_pen)
                painter.drawLine(QtCore.QLineF(start, y, finish, y))

                if x0 <= head <= x1:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(self._alpha(highlight, 235))
                    radius = max(0.72, width * 0.82)
                    painter.drawEllipse(QtCore.QPointF(head, y), radius, radius)
            return
        if mode == 'photon':
            span = max(44.0, length * 0.16)
            phase = self.phase * 0.82 % 1.0
            head = x0 - span + (length + span) * phase
            start, finish = (max(x0, head - span), min(x1, head))
            if finish > start:
                gradient = QtGui.QLinearGradient(start, y, finish, y)
                gradient.setColorAt(0.0, self._alpha(accent, 0))
                gradient.setColorAt(0.7, self._alpha(accent, 108))
                gradient.setColorAt(1.0, self._alpha(highlight, 245))
                pen = QtGui.QPen(QtGui.QBrush(gradient), width + 1.05)
                pen.setCosmetic(True)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QtCore.QLineF(start, y, finish, y))
            return
        if mode == 'pulse':
            span = max(62.0, length * 0.2)
            phase = self.phase * 1.38 % 1.0
            head = x0 - span + (length + span) * phase
            start, finish = (max(x0, head - span), min(x1, head))
            if finish > start:
                gradient = QtGui.QLinearGradient(start, y, finish, y)
                gradient.setColorAt(0.0, self._alpha(accent, 0))
                gradient.setColorAt(0.58, self._alpha(accent, 90))
                gradient.setColorAt(0.86, self._alpha(accent, 220))
                gradient.setColorAt(1.0, self._alpha(highlight, 255))
                pen = QtGui.QPen(QtGui.QBrush(gradient), width + 1.8)
                pen.setCosmetic(True)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QtCore.QLineF(start, y, finish, y))
            return
        if mode == 'vector':
            pen = self._pen(accent, width, min(240, alpha), 'segmented')
            pen.setDashOffset(-self.phase * 26.0)
            painter.setPen(pen)
            painter.drawLine(body_line)
            spacing = 34.0
            shift = self.phase * spacing % spacing


            painter.save()
            painter.setClipRect(QtCore.QRectF(x0, y - 5.0, max(0.0, x1 - x0), 10.0))
            x = x0 + shift - spacing
            painter.setPen(self._pen(accent, 1.0, 180, 'solid'))
            while x <= x1 + spacing:
                painter.drawLine(QtCore.QLineF(x - 4.0, y - 3.0, x, y))
                painter.drawLine(QtCore.QLineF(x, y, x - 4.0, y + 3.0))
                x += spacing
            painter.restore()
            return
        if mode == 'reactor':
            pulse = 0.5 + 0.5 * math.sin(self.phase * math.tau)
            side_alpha = int(45 + 105 * pulse)
            painter.setPen(self._pen(accent, 0.85, side_alpha, 'solid'))
            painter.drawLine(QtCore.QLineF(x0, y - 2.1, x1, y - 2.1))
            painter.drawLine(QtCore.QLineF(x0, y + 2.1, x1, y + 2.1))
            core = x0 + self.phase * 1.18 % 1.0 * length
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._alpha(highlight, int(165 + 80 * pulse)))
            painter.drawEllipse(QtCore.QPointF(core, y), 1.3 + 0.5 * pulse, 1.3 + 0.5 * pulse)
            return
        if mode == 'prism':
            packet_span = max(30.0, min(66.0, length * 0.12))
            for idx, color in enumerate((accent, secondary, tertiary)):
                travel = (self.phase * 0.96 + idx / 3.0) % 1.0
                head = x0 - packet_span + (length + packet_span) * travel
                start, finish = (max(x0, head - packet_span), min(x1, head))
                if finish <= start:
                    continue
                gradient = QtGui.QLinearGradient(start, y, finish, y)
                gradient.setColorAt(0.0, self._alpha(color, 0))
                gradient.setColorAt(0.68, self._alpha(color, 120))
                gradient.setColorAt(1.0, self._alpha(highlight, 235))
                pen = QtGui.QPen(QtGui.QBrush(gradient), width + 1.0)
                pen.setCosmetic(True)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QtCore.QLineF(start, y, finish, y))
            return
        if mode == 'scan':
            window = max(34.0, length * 0.11)
            center = x0 - window * 0.5 + (length + window) * (self.phase * 0.68 % 1.0)
            start, finish = (max(x0, center - window * 0.5), min(x1, center + window * 0.5))
            if finish > start:
                painter.setPen(self._pen(highlight, width + 0.85, 238, 'solid'))
                painter.drawLine(QtCore.QLineF(start, y, finish, y))
                painter.setPen(self._pen(accent, width + 2.4, 42, 'solid'))
                painter.drawLine(QtCore.QLineF(start, y, finish, y))
            return
        if mode == 'dash':
            pen = self._pen(accent, width, min(245, alpha), 'dash')
            pen.setDashPattern([4.0, 4.0])
            pen.setDashOffset(-self.phase * 20.0)
            painter.setPen(pen)
            painter.drawLine(body_line)
            return
        if mode == 'comet':
            comet_span = max(42.0, length * 0.13)
            for index in range(4):
                travel = (self.phase * 0.9 + index * 0.235) % 1.0
                head = x0 + travel * length
                tail = max(x0, head - comet_span * (1.0 - index * 0.1))
                if head <= x0 or tail >= x1:
                    continue
                finish = min(x1, head)
                gradient = QtGui.QLinearGradient(tail, y, finish, y)
                gradient.setColorAt(0.0, self._alpha(accent, 0))
                gradient.setColorAt(0.72, self._alpha(accent, 48 + index * 16))
                gradient.setColorAt(1.0, self._alpha(highlight, 205 - index * 24))
                pen = QtGui.QPen(QtGui.QBrush(gradient), max(0.7, width + 0.65 - index * 0.12))
                pen.setCosmetic(True)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QtCore.QLineF(tail, y, finish, y))
            return
        if mode == 'interference':
            segments = max(10, min(24, int(length / 28.0)))
            step = length / segments
            for index in range(segments):
                a = x0 + index * step
                b = min(x1, a + step * 0.74)
                wave = 0.5 + 0.5 * math.sin((index / 4.0 - self.phase * 4.0) * math.tau)
                color = secondary if index % 4 == 1 else accent
                painter.setPen(self._pen(color, width + 0.25 * wave, int(38 + 170 * wave), 'solid'))
                painter.drawLine(QtCore.QLineF(a, y, b, y))
            return
        nodes = max(5, min(12, int(length / 54.0)))
        for index in range(nodes):
            t = (index + 0.5) / nodes
            x = x0 + length * t
            cycle = (self.phase * nodes - index) % nodes
            distance = min(cycle, nodes - cycle)
            pulse = max(0.0, 1.0 - distance / 1.6)
            radius = 0.8 + 1.25 * pulse
            node_color = highlight if pulse > 0.55 else accent
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._alpha(node_color, int(55 + 190 * pulse)))
            painter.drawEllipse(QtCore.QPointF(x, y), radius, radius)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if self.width() <= 1 or not 0.0 <= self.line_y <= float(self.height()):
            return
        cfg = self.config
        secondary = self._secondary_color
        tertiary = self._tertiary_color
        highlight = self._highlight_color
        fade = max(0.0, 1.0 - self.implosion_progress)
        alpha = int(255 * cfg['line_opacity'] / 100.0 * fade)


        width = max(0.68, float(cfg['line_width']) * 0.85)
        y = self.line_y
        x1 = float(self.width())
        neutral = QtGui.QColor(self._idle_color)
        if self.side == 'SELL':
            directional = QtGui.QColor(self.theme.get('candle_down', self.theme.get('red', '#E60028')))
        else:
            directional = QtGui.QColor(self.theme.get('rail_buy', self.theme.get('green', '#13d88a')))
        x0 = 0.0
        if self.implosion_progress > 0.0:
            collapse = self.implosion_progress ** 0.72
            x0 = x0 + (x1 - x0) * collapse
        line = QtCore.QLineF(x0, y, x1, y)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if alpha <= 0 and self.implosion_progress <= 0.0:
            return
        if self.armed:


            mode = cfg.get('active_animation', 'packets')
            opacity = int(255 * cfg.get('active_opacity', 28) / 100.0 * fade)
            thin = float(cfg.get('active_width', 0.8))
            if mode == 'pulse':
                opacity = int(opacity * (0.65 + 0.35 * (0.5 + 0.5 * math.sin(self.phase * math.tau))))
            painter.setPen(self._pen(directional, thin, opacity, cfg.get('active_line_style', 'solid')))
            painter.drawLine(line)
            if mode == 'packets':
                pen = self._pen(directional, thin, min(110, opacity + 28), 'solid')
                pen.setDashPattern([3.0, 35.0])
                pen.setDashOffset(self.phase * 48.0)
                painter.setPen(pen)
                painter.drawLine(line)
            elif mode == 'scan':
                px = x1 - (self.phase % 1.0) * (x1 - x0)
                painter.setPen(self._pen(directional, thin, min(110, opacity + 28), 'solid'))
                painter.drawLine(QtCore.QLineF(max(x0, px - 28), y, px, y))
            return
        painter.save()
        painter.setClipRect(QtCore.QRectF(max(0.0, x0 - 1.0), 0.0, max(1.0, x1 - x0 + 2.0), float(self.height())))
        shadow = QtGui.QColor(self._background_color)
        shadow.setAlpha(int(180 * fade))
        painter.setPen(QtGui.QPen(shadow, width + 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(line)
        glow = float(cfg['glow_strength']) / 100.0

        def draw_rail_segment(start_x: float, end_x: float, color: QtGui.QColor) -> None:
            if end_x <= start_x + 0.25:
                return
            segment = QtCore.QLineF(start_x, y, end_x, y)
            if glow > 0.0 and alpha > 0:
                painter.setPen(self._pen(color, width + 5.0, int((18 + 34 * glow) * fade), 'solid'))
                painter.drawLine(segment)
                painter.setPen(self._pen(color, width + 2.2, int((28 + 38 * glow) * fade), 'solid'))
                painter.drawLine(segment)
            painter.setPen(self._pen(color, width, int(alpha * 0.88), 'solid'))
            painter.drawLine(segment)
        draw_rail_segment(x0, x1, neutral)
        _arc_span, arc_start = self._flux_arc_geometry(x1)
        if x1 > x0 + 1.0:


            body_end = min(x1, arc_start)
            body_mode = str(cfg.get('body_animation', 'ray'))


            animation_start = x0 if body_mode in {'vector', 'ray'} else x0 + max(0.0, body_end - x0) * 0.20
            if body_end > animation_start + 1.0:
                painter.save()
                painter.translate(animation_start + body_end, 0.0)
                painter.scale(-1.0, 1.0)
                self._draw_body_animation(painter, mode=body_mode, x0=animation_start, x1=body_end, y=y, width=width, alpha=alpha, accent=neutral, secondary=secondary, tertiary=tertiary, highlight=highlight)
                painter.restore()
        gate_accent = neutral
        self._draw_flux_arc_whirl(painter, y=y, x1=x1, accent=gate_accent, secondary=secondary, highlight=highlight)
        accent = gate_accent
        gate_x = max(x0 + 8.0, x1 - 9.0)
        pulse = 0.5 + 0.5 * math.sin(self.phase * math.tau)
        painter.setPen(QtGui.QPen(self._alpha(accent, int(150 * fade)), 1.0))
        painter.drawLine(QtCore.QLineF(gate_x - 7.0, y - 6.0, gate_x - 1.5, y - 6.0))
        painter.drawLine(QtCore.QLineF(gate_x - 7.0, y + 6.0, gate_x - 1.5, y + 6.0))
        painter.drawLine(QtCore.QLineF(gate_x - 7.0, y - 6.0, gate_x - 7.0, y - 2.0))
        painter.drawLine(QtCore.QLineF(gate_x - 7.0, y + 2.0, gate_x - 7.0, y + 6.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(self._alpha(accent, int((90 + 85 * pulse) * fade)), 1.0))
        radius = 4.2 + 1.2 * pulse
        painter.drawArc(QtCore.QRectF(gate_x - radius, y - radius, radius * 2, radius * 2), 38 * 16, 284 * 16)
        painter.setPen(QtGui.QPen(self._alpha(secondary, int((70 + 70 * (1.0 - pulse)) * fade)), 1.0))
        outer = radius + 2.8
        painter.drawArc(QtCore.QRectF(gate_x - outer, y - outer, outer * 2, outer * 2), 210 * 16, 118 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._alpha(accent, int(245 * fade)))
        painter.drawEllipse(QtCore.QPointF(gate_x, y), 1.8, 1.8)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, base_alpha in enumerate((110, 76, 46)):
            travel = (self.phase + index * 0.27) % 1.0
            px = gate_x - 12.0 - travel * 54.0
            radius = 1.25 - index * 0.18
            packet_color = secondary if index == 1 else accent
            painter.setBrush(self._alpha(packet_color, int(base_alpha * fade)))
            painter.drawEllipse(QtCore.QPointF(px, y), radius, radius)
        if self.dragging:
            painter.setPen(self._pen(accent, 1.8, int(255 * fade), 'solid'))
            painter.drawLine(QtCore.QLineF(max(x0, x1 - 28.0), y, x1, y))
            painter.setPen(QtGui.QPen(self._alpha(accent, int(210 * fade)), 1.0))
            painter.drawLine(QtCore.QLineF(gate_x, y - 9.0, gate_x, y + 9.0))
        painter.restore()
        if self.implosion_progress > 0.0:
            p = self.implosion_progress
            center = QtCore.QPointF(max(0.0, x1 - 9.0), y)
            for base_radius, base_alpha in ((14.0, 185), (8.0, 140)):
                r = max(0.8, base_radius * (1.0 - p) + 0.8)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QtGui.QPen(self._alpha(accent, int(base_alpha * (1.0 - p))), 1.0))
                painter.drawEllipse(center, r, r)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._alpha(highlight, int(220 * (1.0 - p))))
            painter.drawEllipse(center, max(0.7, 2.4 * (1.0 - p)), max(0.7, 2.4 * (1.0 - p)))

class MagneticOrderRailPanel(QtWidgets.QWidget):
    """Compact right-edge order rail with inline execution parameters.

    Price-axis geometry and the rail/body connection remain independent of these
    controls. The controls occupy only free space between the side dot and the
    canonical native-axis price text band.
    """


    COMPACT_WIDTH = 244
    COMPACT_HEIGHT = 51
    BODY_WIDTH = 242.2
    BODY_HEIGHT = 45.9


    CONTROL_BOX_HEIGHT = 24.0
    CONTROL_BOX_WIDTH = 44.5
    CONTROL_MIN_BOX_WIDTH = 40.0
    REDUCE_WIDTH = 26.5
    REDUCE_HEIGHT = 24.0
    DOT_CENTER_INSET = 18.7
    DOT_RADIUS = 4.1
    SIZE_LEFT_INSET = 48.8
    SIZE_LEVERAGE_GAP = 4.5
    LEVERAGE_REDUCE_GAP = 8.2
    BODY_DIVIDER_INSET = 192.7
    PRICE_CONTROL_MIN_CLEARANCE = 8.0
    ARM_DURATION_MS = 520
    DISARM_DURATION_MS = 160
    SIZE_VALUES = (25, 50, 75, 100)
    LEVERAGE_VALUES = (1, 3, 5, 10, 20, 25, 50)

    remove_requested = Signal()
    state_changed = Signal()
    geometry_changed = Signal()
    body_drag_started = Signal(object)
    body_drag_moved = Signal(object)
    body_drag_finished = Signal(bool)
    execution_requested = Signal(str)
    leverage_requested = Signal(int)

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.theme = theme
        self.config = normalized_order_rail_config()
        self.price = 0.0
        self.price_text = ''
        self._paint_price_text = '—'
        self.armed = False
        self.side = 'BUY'
        self.reduce_only = False
        self._entry_reduce_only = False
        self.size_percent = 25
        self.leverage = 5
        self.order_role = 'ENTRY'
        self.take_profit_enabled = False
        self.stop_loss_enabled = False
        self.order_type = 'LIMIT'
        self.time_in_force = 'GTC'
        self.working_type = 'CONTRACT_PRICE'
        self.price_protect = True
        self.callback_rate = 0.5
        self.limit_offset_percent = 0.0
        self.line_pattern_override = 'inherit'
        self.preset_name = 'Limit Entry'

        self._external_dragging = False
        self.line_anchor_y = 0.0
        self.offscreen_direction = 0
        self._axis_left: float | None = None
        self._axis_text_left: float | None = None
        self._axis_text_width = 0.0
        self.phase = 0.0
        self._hover_key = ''
        self._pressed_key = ''
        self._body_dragging = False
        self._body_click_pending = False
        self._body_press_global: QtCore.QPointF | None = None
        self._arm_visual_progress = 0.0


        self._arm_submission_preview = False
        self._implosion_progress = 0.0
        self.submission_pending = False
        self.cancellation_pending = False
        self._commit_flash = False
        self._commit_started = 0.0

        self._axis_font = _chart_axis_font()
        self._paint_text_color = QtGui.QColor()
        self._paint_muted_color = QtGui.QColor()
        self._paint_surface_color = QtGui.QColor()
        self._paint_green_color = QtGui.QColor()
        self._paint_red_color = QtGui.QColor()
        self._paint_cyan_color = QtGui.QColor()
        self._paint_opaque_color = QtGui.QColor()
        self._paint_panel_fill = QtGui.QColor()
        self._refresh_theme_color_cache()
        typography_controller().changed.connect(self._refresh_typography_cache)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)


        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()


        self._arm_animation_running = False
        self._arm_animation_started = 0.0
        self._arm_animation_from = 0.0
        self._arm_animation_to = 0.0
        self._arm_animation_duration_s = 0.0

    @staticmethod
    def _alpha(color: QtGui.QColor, alpha: int) -> QtGui.QColor:
        out = QtGui.QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def _refresh_typography_cache(self) -> None:
        self._axis_font = _chart_axis_font()
        self.update()

    def _refresh_theme_color_cache(self) -> None:
        t = self.theme
        self._paint_text_color = QtGui.QColor(t.get('text', '#d4dbe1'))
        self._paint_muted_color = QtGui.QColor(t.get('muted', '#7f8b95'))
        self._paint_surface_color = QtGui.QColor(t.get('control', '#0a1014'))
        self._paint_green_color = QtGui.QColor(t.get('rail_buy', t.get('green', '#13d88a')))
        self._paint_red_color = QtGui.QColor(t.get('candle_down', t.get('red', '#E60028')))
        self._paint_cyan_color = QtGui.QColor(t.get('rail_neutral', t.get('cyan', '#65b7d5')))
        self._paint_opaque_color = QtGui.QColor(t.get('panel2', '#070b0e'))
        self._paint_opaque_color.setAlpha(255)
        self._paint_panel_fill = QtGui.QColor(self._paint_opaque_color)
        self._paint_panel_fill.setAlpha(int(255 * self.config['panel_opacity'] / 100.0))

    def _refresh_paint_price_text(self) -> None:
        raw = self.price_text or (format_price(self.price) if self.price > 0 else '—')
        if self.offscreen_direction < 0:
            raw = '↑ ' + raw
        elif self.offscreen_direction > 0:
            raw = '↓ ' + raw
        self._paint_price_text = raw


    def set_config(self, config: dict[str, Any]) -> None:
        self.config = normalized_order_rail_config(config)
        self._refresh_theme_color_cache()
        if not self.config.get('armed_transition_enabled', True):
            self._arm_animation_running = False
            target = 1.0 if self.armed else 0.0
            if abs(self._arm_visual_progress - target) > 0.0001:
                self._arm_visual_progress = target
                self.geometry_changed.emit()
        self.update()

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self._refresh_theme_color_cache()
        self.update()

    def set_price(self, price: float, text: str='') -> None:
        price = float(price)
        text = str(text or '')
        if abs(price - self.price) <= max(1e-12, abs(price) * 1e-12) and text == self.price_text:
            return
        self.price = price
        self.price_text = text
        self._refresh_paint_price_text()
        self.update()

    def set_line_anchor(self, local_y: float, offscreen_direction: int=0) -> None:
        local_y = max(0.0, min(float(self.height()), float(local_y)))
        direction = -1 if offscreen_direction < 0 else 1 if offscreen_direction > 0 else 0
        if abs(local_y - self.line_anchor_y) < 0.25 and direction == self.offscreen_direction:
            return
        self.line_anchor_y = local_y
        self.offscreen_direction = direction
        self._refresh_paint_price_text()
        self.update()

    def set_axis_text_band(self, local_left: float | None, width: float=0.0, *, axis_left: float | None=None) -> None:
        left = None if local_left is None else float(local_left)
        axis_left_value = None if axis_left is None else float(axis_left)
        width = max(0.0, float(width))
        unchanged_text = (
            (left is None and self._axis_text_left is None)
            or (
                left is not None
                and self._axis_text_left is not None
                and abs(left - self._axis_text_left) < 0.25
                and abs(width - self._axis_text_width) < 0.25
            )
        )
        unchanged_axis = (
            (axis_left_value is None and self._axis_left is None)
            or (
                axis_left_value is not None
                and self._axis_left is not None
                and abs(axis_left_value - self._axis_left) < 0.25
            )
        )
        if unchanged_text and unchanged_axis:
            return
        self._axis_left = axis_left_value
        self._axis_text_left = left
        self._axis_text_width = width
        self.update()

    def apply_order_preset(self, preset: dict[str, Any], *, side: str, preset_name: str='') -> None:
        values = normalized_order_rail_order_preset(preset)
        self.side = 'SELL' if str(side).upper() == 'SELL' else 'BUY'
        self.order_type = str(values['orderType'])
        self.order_role = 'ENTRY'
        self.reduce_only = bool(values['reduceOnly'])
        self._entry_reduce_only = self.reduce_only
        self.take_profit_enabled = bool(values.get('takeProfitEnabled', False)) and (not self.reduce_only)
        self.stop_loss_enabled = bool(values.get('stopLossEnabled', False)) and (not self.reduce_only)
        self.size_percent = int(values['sizePercent'])
        self.leverage = int(values['leverage'])
        self.time_in_force = str(values['timeInForce'])
        self.working_type = str(values['workingType'])
        self.price_protect = bool(values['priceProtect'])
        self.callback_rate = float(values['callbackRate'])
        self.limit_offset_percent = float(values['limitOffsetPercent'])
        self.line_pattern_override = str(values['linePattern'])
        self.preset_name = str(preset_name or self.order_type)
        self.submission_pending = False
        self.cancellation_pending = False
        self.update()
        self.state_changed.emit()

    def set_submission_pending(self, pending: bool) -> None:
        pending = bool(pending)
        if pending == self.submission_pending:
            return
        self.submission_pending = pending
        self._hover_key = ''
        self._pressed_key = ''
        self.update()

    def set_cancellation_pending(self, pending: bool) -> None:
        pending = bool(pending)
        if pending == self.cancellation_pending:
            return
        self.cancellation_pending = pending
        self._hover_key = ''
        self._pressed_key = ''
        self.update()

    def set_arm_submission_preview(self, active: bool, *, animated: bool=True) -> None:
        """Run the armed contraction while Binance submission is unresolved.

        This is presentation-only. ``self.armed`` remains False until the normal
        accepted-working-order path calls :meth:`set_armed`.
        """
        active = bool(active)
        if self.armed:
            self._arm_submission_preview = False
            return
        target = 1.0 if active else 0.0
        if (
            active == self._arm_submission_preview
            and abs(self._arm_visual_progress - target) < 0.0001
        ):
            return
        self._arm_submission_preview = active
        self._hover_key = ''
        self._pressed_key = ''
        self._arm_animation_running = False
        animated = bool(animated and self.config.get('armed_transition_enabled', True))
        if animated:
            self._arm_animation_from = float(self._arm_visual_progress)
            self._arm_animation_to = target
            duration_ms = self.ARM_DURATION_MS if active else self.DISARM_DURATION_MS
            self._arm_animation_duration_s = max(0.001, duration_ms / 1000.0)
            self._arm_animation_started = time.monotonic()
            self._arm_animation_running = True
            self._advance_arm_animation(self._arm_animation_started)
        else:
            self._arm_visual_progress = target
            self.geometry_changed.emit()
            self.update()

    def arm_submission_preview_active(self) -> bool:
        return bool(self._arm_submission_preview)

    def set_armed(self, armed: bool, *, animated: bool=True) -> None:
        armed = bool(armed)
        target = 1.0 if armed else 0.0


        if armed and self._arm_submission_preview:
            self._arm_submission_preview = False
            self.armed = True
            self._hover_key = ''
            self._pressed_key = ''
            self.update()
            return

        if armed == self.armed and abs(self._arm_visual_progress - target) < 0.0001:
            self._arm_submission_preview = False
            return
        self._arm_submission_preview = False
        self.armed = armed
        self._hover_key = ''
        self._pressed_key = ''
        self._arm_animation_running = False
        animated = bool(animated and self.config.get('armed_transition_enabled', True))
        if animated:
            self._arm_animation_from = float(self._arm_visual_progress)
            self._arm_animation_to = target
            duration_ms = self.ARM_DURATION_MS if armed else self.DISARM_DURATION_MS
            self._arm_animation_duration_s = max(0.001, duration_ms / 1000.0)
            self._arm_animation_started = time.monotonic()
            self._arm_animation_running = True
            self._advance_arm_animation(self._arm_animation_started)
        else:
            self._arm_visual_progress = target
            self.geometry_changed.emit()
            self.update()

    @staticmethod
    def _arm_eased_progress(progress: float, *, arming: bool) -> float:
        progress = max(0.0, min(1.0, float(progress)))
        if arming:

            return 1.0 - (1.0 - progress) ** 3

        return progress ** 3

    def arm_transition_active(self) -> bool:
        return bool(self._arm_animation_running)

    def _advance_arm_animation(self, now: float | None = None) -> bool:
        if not self._arm_animation_running:
            return False
        duration = max(0.001, float(self._arm_animation_duration_s))
        current = time.monotonic() if now is None else float(now)
        elapsed = max(0.0, current - self._arm_animation_started)
        linear = min(1.0, elapsed / duration)
        eased = self._arm_eased_progress(
            linear,
            arming=self._arm_animation_to >= self._arm_animation_from,
        )
        progress = self._arm_animation_from + (
            self._arm_animation_to - self._arm_animation_from
        ) * eased
        if linear >= 1.0:
            self._arm_visual_progress = self._arm_animation_to
            self._arm_animation_running = False
        else:
            self._arm_visual_progress = max(0.0, min(1.0, progress))
        self.geometry_changed.emit()
        self.update()
        return self._arm_animation_running

    def advance_frame_animation(self, now: float) -> bool:
        """Advance arm/disarm interpolation from the chart presentation frame."""
        return self._advance_arm_animation(now)

    def arm_visual_progress(self) -> float:
        return max(0.0, min(1.0, float(self._arm_visual_progress)))

    def set_active_contract_target(self, enabled: bool, *, animated: bool=True) -> None:


        del enabled, animated


    def set_implosion_progress(self, progress: float) -> None:
        progress = max(0.0, min(1.0, float(progress)))
        if abs(progress - self._implosion_progress) < 0.001:
            return
        self._implosion_progress = progress
        self.update()

    def set_external_dragging(self, dragging: bool) -> None:
        dragging = bool(dragging)
        if dragging == self._external_dragging:
            return
        self._external_dragging = dragging
        self.update()

    def expansion_anchor(self) -> float:
        return 0.5

    def desired_size(self, available_width: int, available_height: int) -> QtCore.QSize:
        return QtCore.QSize(
            max(1, min(self.COMPACT_WIDTH, int(available_width))),
            max(1, min(self.COMPACT_HEIGHT, int(available_height))),
        )

    def _layout(self) -> dict[str, QtCore.QRectF]:
        full_rect = QtCore.QRectF(self.rect())
        price_h = min(self.BODY_HEIGHT, max(1.0, full_rect.height() - 6.0))
        base_price_w = min(self.BODY_WIDTH, max(1.0, full_rect.width() - 2.0))
        top = max(3.0, min(full_rect.bottom() - price_h - 3.0, self.line_anchor_y - price_h * 0.5))
        right = full_rect.right() - 0.5
        base_left = right - base_price_w
        arm_progress = self.arm_visual_progress()
        left = base_left
        if self._axis_left is not None and arm_progress > 0.0:
            axis_target_left = max(base_left, min(right - 1.0, self._axis_left))
            left += (axis_target_left - left) * arm_progress
        if self._implosion_progress > 0.0:
            left += (right - 1.0 - left) * self._implosion_progress
        if self._axis_text_left is not None and self._axis_text_width > 1.0:
            if self._implosion_progress <= 0.0:
                left = max(base_left, min(left, self._axis_text_left - 9.0))
            value = QtCore.QRectF(self._axis_text_left, self.line_anchor_y - 10.0, self._axis_text_width, 20.0)
        else:
            value_left = max(left + 12.0, right - 88.0)
            value = QtCore.QRectF(value_left, self.line_anchor_y - 10.0, max(1.0, right - value_left - 5.0), 20.0)
        price = QtCore.QRectF(left, top, max(1.0, right - left), price_h)


        empty = QtCore.QRectF()
        size_rect = leverage_rect = reduce_rect = empty
        controls_blocked = bool(
            self.armed
            or self.submission_pending
            or self.cancellation_pending
            or self._implosion_progress > 0.001
        )

        if self._axis_left is not None:
            divider_x = max(price.left() + 1.0, min(price.right() - 1.0, self._axis_left))
        else:
            divider_x = min(
                price.left() + self.BODY_DIVIDER_INSET,
                value.left() - self.PRICE_CONTROL_MIN_CLEARANCE,
            )
            divider_x = max(price.left() + 1.0, divider_x)

        if not controls_blocked:
            control_h = min(self.CONTROL_BOX_HEIGHT, max(18.0, price.height() - 12.0))
            box_w = min(self.CONTROL_BOX_WIDTH, max(self.CONTROL_MIN_BOX_WIDTH, self.CONTROL_BOX_WIDTH))
            reduce_h = min(self.REDUCE_HEIGHT, control_h)
            reduce_w = min(self.REDUCE_WIDTH, reduce_h + 2.5)
            control_top = self.line_anchor_y - control_h * 0.5

            x_size = price.left() + self.SIZE_LEFT_INSET
            x_leverage = x_size + box_w + self.SIZE_LEVERAGE_GAP
            x_reduce = x_leverage + box_w + self.LEVERAGE_REDUCE_GAP
            controls_end = x_reduce + reduce_w


            max_end = divider_x - 8.0
            if controls_end > max_end:
                shift = controls_end - max_end
                x_size -= shift
                x_leverage -= shift
                x_reduce -= shift

            min_start = price.left() + self.DOT_CENTER_INSET + self.DOT_RADIUS + 10.0
            if x_size >= min_start and x_reduce + reduce_w <= max_end:
                size_rect = QtCore.QRectF(x_size, control_top, box_w, control_h)
                leverage_rect = QtCore.QRectF(x_leverage, control_top, box_w, control_h)
                reduce_top = self.line_anchor_y - reduce_h * 0.5
                reduce_rect = QtCore.QRectF(x_reduce, reduce_top, reduce_w, reduce_h)

        return {
            'price': price,
            'price_value': value,
            'size_cycle': size_rect,
            'leverage_cycle': leverage_rect,
            'reduce': reduce_rect,
            'divider_x': divider_x,
        }

    def _shape_path(self, rect: QtCore.QRectF) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        if rect.isEmpty():
            return path
        radius = max(0.0, min(15.3, rect.height() * 0.5, rect.width() * 0.5))
        path.addRoundedRect(rect, radius, radius)
        return path

    def _interactive_surface_contains(self, point: QtCore.QPointF) -> bool:
        price = self._layout().get('price', QtCore.QRectF())
        return bool(not price.isEmpty() and self._shape_path(price).contains(point))

    def _hit_key(self, point: QtCore.QPointF) -> str:
        layout = self._layout()
        key = hit_control_key(
            point,
            layout,
            blocked=bool(self.armed or self.submission_pending or self.cancellation_pending),
        )
        if key == 'reduce':
            rect = layout['reduce']
            radius = min(rect.width(), rect.height()) * 0.5
            center = rect.center()
            dx = float(point.x() - center.x())
            dy = float(point.y() - center.y())
            if dx * dx + dy * dy > radius * radius:
                key = ''


        return '' if self.reduce_only and key == 'leverage_cycle' else key

    @staticmethod
    def _cycle_value(current: int, values: tuple[int, ...], step: int=1) -> int:
        current = int(current)
        try:
            index = values.index(current)
        except ValueError:
            index = min(range(len(values)), key=lambda i: abs(values[i] - current))
        return values[(index + step) % len(values)]

    def _activate(self, key: str, *, step: int=1) -> None:
        if self.armed or self.submission_pending or self.cancellation_pending:
            return
        intent = control_intent(key)
        if intent is None:
            return
        changed = False
        if intent is RailIntent.CYCLE_SIZE:
            value = self._cycle_value(self.size_percent, self.SIZE_VALUES, step)
            if value != self.size_percent:
                self.size_percent = value
                changed = True
        elif intent is RailIntent.CYCLE_LEVERAGE:
            value = self._cycle_value(self.leverage, self.LEVERAGE_VALUES, step)
            if value != self.leverage:
                self.leverage = value
                self.leverage_requested.emit(value)
                changed = True
        elif intent is RailIntent.TOGGLE_REDUCE:
            self._entry_reduce_only = not self._entry_reduce_only
            self.reduce_only = self._entry_reduce_only
            if self.reduce_only:
                self.take_profit_enabled = False
                self.stop_loss_enabled = False
            changed = True
        if changed:
            self.state_changed.emit()
            self.update()

    def _start_body_drag(self, current_global: QtCore.QPointF) -> None:
        if self._body_dragging or self.submission_pending or self.cancellation_pending:
            return
        self._body_click_pending = False
        self._body_dragging = True
        self.set_external_dragging(True)
        try:
            self.grabMouse()
        except RuntimeError:
            pass
        start_global = self._body_press_global or current_global
        self.body_drag_started.emit(start_global)
        self.body_drag_moved.emit(current_global)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._body_dragging:
            self.body_drag_moved.emit(event.globalPosition())
            event.accept()
            return
        if self._body_click_pending:
            origin = self._body_press_global or event.globalPosition()
            delta = event.globalPosition() - origin
            threshold = max(3, QtWidgets.QApplication.startDragDistance())
            if abs(delta.x()) + abs(delta.y()) >= threshold:
                self._start_body_drag(event.globalPosition())
                event.accept()
                return
        key = self._hit_key(event.position()) if self._interactive_surface_contains(event.position()) else ''
        if key != self._hover_key:
            self._hover_key = key
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        if not self._body_dragging and not self._body_click_pending:
            self._hover_key = ''
            self._pressed_key = ''
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        position = event.position()
        if not self._interactive_surface_contains(position):
            event.ignore()
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        key = '' if self.armed else self._hit_key(position)
        if key:
            self._pressed_key = key
            self.update()
            event.accept()
            return
        if self.submission_pending or self.cancellation_pending:
            event.accept()
            return
        self._pressed_key = ''
        self._body_click_pending = True
        self._body_press_global = QtCore.QPointF(event.globalPosition())
        try:
            self.grabMouse()
        except RuntimeError:
            pass
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if self._body_dragging:
            self._body_dragging = False
            try:
                if QtWidgets.QWidget.mouseGrabber() is self:
                    self.releaseMouse()
            except RuntimeError:
                pass
            self.set_external_dragging(False)
            self.body_drag_finished.emit(True)
            self._pressed_key = ''
            self._body_press_global = None
            self.update()
            event.accept()
            return
        if self._body_click_pending:
            if self.armed:
                self._show_working_order_details()
            self._body_click_pending = False
            self._body_press_global = None
            try:
                if QtWidgets.QWidget.mouseGrabber() is self:
                    self.releaseMouse()
            except RuntimeError:
                pass
            self.update()
            event.accept()
            return
        key, pressed = self._hit_key(event.position()), self._pressed_key
        self._pressed_key = ''
        if pressed and key == pressed:
            self._activate(key)
        self.update()
        event.accept()

    def _show_working_order_details(self) -> None:
        order = getattr(self, 'working_order', {})
        popup = QtWidgets.QMenu(self)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        color = '#23d99a' if self.side == 'BUY' else '#f35472'
        popup.setStyleSheet(f"QMenu {{ background:#09121c; color:#d9e9f4; border:1px solid {color}; padding:8px; }} QMenu::item:selected {{ background:#1a3040; }}")
        kind = order.get('type') or order.get('orderType') or 'ORDER'
        for label in (
            f"{order.get('symbol', '')}  ·  {self.side}  ·  {kind}",
            f"Price  {self.price_text}",
            f"Quantity  {order.get('origQty', order.get('quantity', 'Close position'))}",
            f"Filled  {order.get('executedQty', '0')}",
            f"Status  {order.get('status', order.get('algoStatus', 'ACTIVE'))}",
            f"Trigger  {order.get('workingType', 'CONTRACT_PRICE')}",
            f"Limit  {order.get('price', '—')}  ·  Stop  {order.get('triggerPrice', order.get('stopPrice', '—'))}",
            f"ID  {order.get('algoId', order.get('orderId', 'Pending'))}",
        ):
            action = popup.addAction(str(label))
            action.setEnabled(False)
        popup.addSeparator()
        cancel = popup.addAction('Cancel order')
        cancel.setEnabled(bool(order) and not self.cancellation_pending and not self.submission_pending)
        cancel.triggered.connect(self.remove_requested.emit)
        popup.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        key = self._hit_key(event.position())
        if key not in {'size_cycle', 'leverage_cycle'}:
            event.ignore()
            return
        delta = int(event.angleDelta().y())
        if delta == 0:
            event.ignore()
            return
        self._activate(key, step=1 if delta > 0 else -1)
        event.accept()

    def event(self, event: QtCore.QEvent) -> bool:
        if event.type() in {QtCore.QEvent.Type.UngrabMouse, QtCore.QEvent.Type.WindowDeactivate} and (self._body_dragging or self._body_click_pending):
            was_dragging = self._body_dragging
            self._body_dragging = False
            self._body_click_pending = False
            self._body_press_global = None
            self._pressed_key = ''
            self.set_external_dragging(False)
            if was_dragging:
                self.body_drag_finished.emit(False)
            self.update()
        return super().event(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        was_dragging = self._body_dragging
        self._body_dragging = False
        self._body_click_pending = False
        self._body_press_global = None
        self._pressed_key = ''
        try:
            if QtWidgets.QWidget.mouseGrabber() is self:
                self.releaseMouse()
        except RuntimeError:
            pass
        if was_dragging:
            self.body_drag_finished.emit(False)
        super().hideEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            if not self.cancellation_pending:
                self.remove_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def flash_commit(self) -> None:
        if not self.config['release_flash']:
            return
        self._commit_flash = True
        self._commit_started = time.monotonic()
        self.update()
        QTimer.singleShot(340, self._clear_commit_flash)

    def _clear_commit_flash(self) -> None:
        if self._commit_flash:
            self._commit_flash = False
            self.update()

    def animation_required(self) -> bool:
        return bool(self._commit_flash or self._arm_animation_running)

    def advance_animation(self, phase: float) -> None:
        phase = float(phase)
        if abs(phase - self.phase) < 1e-06:
            return
        self.phase = phase
        if self.isVisible() and self._commit_flash:
            self.update()

    def _draw_shell(self, painter: QtGui.QPainter, rect: QtCore.QRectF, accent: QtGui.QColor) -> None:

        path = self._shape_path(rect)
        cyan = self._paint_cyan_color
        visually_armed = bool(self.armed or self._arm_submission_preview)
        rim = (self._paint_green_color if self.side == 'BUY' else self._paint_red_color) if visually_armed else cyan
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._paint_panel_fill)
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)


        entrance = QtGui.QRadialGradient(
            QtCore.QPointF(rect.left() + 8.0, self.line_anchor_y),
            min(72.0, rect.width() * 0.30),
        )
        entrance.setColorAt(0.0, self._alpha(cyan, 72))
        entrance.setColorAt(0.24, self._alpha(cyan, 34))
        entrance.setColorAt(0.58, self._alpha(cyan, 9))
        entrance.setColorAt(1.0, self._alpha(cyan, 0))
        painter.fillRect(rect, entrance)


        top_glint = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
        top_glint.setColorAt(0.0, self._alpha(cyan, 155))
        top_glint.setColorAt(0.20, self._alpha(cyan, 95))
        top_glint.setColorAt(0.62, self._alpha(cyan, 32))
        top_glint.setColorAt(1.0, self._alpha(cyan, 12))
        painter.setPen(QtGui.QPen(QtGui.QBrush(top_glint), 0.85))
        painter.drawLine(QtCore.QLineF(rect.left() + 14.0, rect.top() + 0.8, rect.right() - 18.0, rect.top() + 0.8))
        painter.restore()


        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(self._alpha(rim, 22), 2.4))
        painter.drawPath(path)
        painter.setPen(QtGui.QPen(self._alpha(rim, 132), 0.82))
        painter.drawPath(path)
        inner = rect.adjusted(1.3, 1.3, -1.3, -1.3)
        painter.setPen(QtGui.QPen(self._alpha(rim, 92 if visually_armed else 66), 0.55))
        painter.drawRoundedRect(inner, 14.0, 14.0)

    def _draw_inline_control(self, painter: QtGui.QPainter, key: str, rect: QtCore.QRectF, label: str, *, active: bool=False, disabled: bool=False) -> None:
        if rect.isEmpty():
            return
        cyan = self._paint_cyan_color
        hovered = (self._hover_key == key) and not disabled
        pressed = (self._pressed_key == key) and not disabled

        painter.setFont(typography_font(TextRole.RAIL_CONTROL))

        if key == 'reduce':
            radius = min(7.0, rect.height() * 0.32)
            if active or hovered:
                glow_rect = rect.adjusted(-1.4, -1.4, 1.4, 1.4)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self._alpha(cyan, 36 if active else 18))
                painter.drawRoundedRect(glow_rect, radius + 1.4, radius + 1.4)
            fill = QtGui.QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
            fill.setColorAt(0.0, self._alpha(cyan, 22 if not pressed else 34))
            fill.setColorAt(1.0, self._alpha(self._paint_surface_color, 244))
            painter.setBrush(fill)
            painter.setPen(QtGui.QPen(self._alpha(cyan, 118 if not active else 205), 0.72 if not active else 0.95))
            painter.drawRoundedRect(rect, radius, radius)
            painter.setPen(cyan if active else self._paint_text_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
            return


        radius = rect.height() * 0.5
        fill = QtGui.QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        fill.setColorAt(0.0, self._alpha(cyan, 44 if hovered else 34))
        fill.setColorAt(0.48, self._alpha(cyan, 28 if pressed else 20))
        fill.setColorAt(1.0, self._alpha(self._paint_surface_color, 236))
        painter.setBrush(fill)
        painter.setPen(QtGui.QPen(self._alpha(cyan, 46 if not hovered else 82), 0.55))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(self._paint_muted_color if disabled else cyan)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _draw_commit_pulse(self, painter: QtGui.QPainter, rect: QtCore.QRectF, accent: QtGui.QColor) -> None:
        if not self._commit_flash or self._commit_started <= 0.0:
            return
        progress = max(0.0, min(1.0, (time.monotonic() - self._commit_started) / 0.34))
        alpha = int(220 * (1.0 - progress))
        expand = 0.85 + 4.25 * progress
        pulse_rect = rect.adjusted(-expand, -expand, expand, expand)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(self._alpha(accent, alpha), 1.02 + 0.68 * (1.0 - progress)))
        radius = max(4.25, float(self.config['corner_radius']) * 0.85 + expand)
        painter.drawRoundedRect(pulse_rect, radius, radius)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        apply_text_render_hints(painter)
        text = self._paint_text_color
        muted = self._paint_muted_color
        green = self._paint_green_color
        red = self._paint_red_color
        cyan = self._paint_cyan_color
        pending_color = QtGui.QColor(self.theme.get('rail_warning', self.theme.get('amber', '#f0b34a')))
        layout = self._layout()
        price_rect = layout['price']
        visually_armed = bool(self.armed or self._arm_submission_preview)
        compact_accent = pending_color if self.submission_pending else (green if self.side == 'BUY' else red) if visually_armed else cyan
        if self._implosion_progress > 0.0:
            painter.setOpacity(max(0.0, (1.0 - self._implosion_progress) ** 1.35))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._paint_opaque_color)
        painter.drawPath(self._shape_path(price_rect))
        self._draw_shell(painter, price_rect, compact_accent)


        if not visually_armed:
            side_color = green if self.side == 'BUY' else red
            dot_center = QtCore.QPointF(price_rect.left() + self.DOT_CENTER_INSET, self.line_anchor_y)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._alpha(side_color, 30))
            painter.drawEllipse(dot_center, self.DOT_RADIUS + 1.5, self.DOT_RADIUS + 1.5)
            painter.setBrush(side_color)
            painter.drawEllipse(dot_center, self.DOT_RADIUS, self.DOT_RADIUS)

        self._draw_inline_control(painter, 'size_cycle', layout['size_cycle'], f'{self.size_percent}%', active=False)
        self._draw_inline_control(painter, 'leverage_cycle', layout['leverage_cycle'], f'{self.leverage}x', active=False, disabled=self.reduce_only)
        self._draw_inline_control(painter, 'reduce', layout['reduce'], 'R', active=self.reduce_only)
        if not layout['reduce'].isEmpty():
            divider_x = float(layout.get('divider_x', layout['price_value'].left() - 4.25))
            painter.setPen(QtGui.QPen(self._alpha(cyan, 92), 0.72))
            painter.drawLine(QtCore.QLineF(divider_x, self.line_anchor_y - 11.0, divider_x, self.line_anchor_y + 11.0))


        raw_price = self.price_text or (format_price(self.price) if self.price > 0 else '—') if self._axis_text_left is not None else self._paint_price_text
        painter.setFont(self._axis_font)
        value_rect = layout.get('price_value', price_rect)
        if self._axis_text_left is not None and self.offscreen_direction:
            arrow = '↑' if self.offscreen_direction < 0 else '↓'
            arrow_width = max(8.0, float(QtGui.QFontMetricsF(self._axis_font).horizontalAdvance(arrow)) + 3.0)
            arrow_rect = QtCore.QRectF(max(price_rect.left() + 2.0, value_rect.left() - arrow_width), value_rect.top(), arrow_width - 2.0, value_rect.height())
            painter.setPen(muted)
            painter.drawText(arrow_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, arrow)
        painter.setPen(text)
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if self._axis_text_left is not None else Qt.AlignmentFlag.AlignRight), raw_price)
        self._draw_commit_pulse(painter, price_rect, compact_accent)
        painter.setOpacity(1.0)
