"""Candlestick and volume rendering for the chart feature.

This module owns bar geometry and CPU/GPU rendering. It
depends only on the chart-rendering contract plus shared model helpers;
it does not know about ChartWorkspace, MainWindow, market data, or theme services.
"""
from __future__ import annotations

import math
import time
import logging
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from ..theme import CANDLE_STYLES
from .preparation import PreparedBars, prepare_bars
from ..presentation import (
    performance_profile_active,
    record_performance_count,
    record_performance_sum,
    record_performance_timing,
)

try:
    from PySide6 import QtOpenGL
except ImportError:
    QtOpenGL = None


def _qt_enum_member(owner: Any, group: str, member: str) -> Any:
    """Resolve scoped and legacy PySide6 enum spellings."""
    scoped = getattr(owner, group, None)
    if scoped is not None and hasattr(scoped, member):
        return getattr(scoped, member)
    return getattr(owner, member)


def _pixel_bar_width(slot_pixels: float, width_ratio: float) -> int:
    """Exact physical-pixel body-width policy shared by candles and volume."""
    slot_pixels = max(0.0, float(slot_pixels))
    ratio = max(0.0, float(width_ratio))
    width = int(math.floor(slot_pixels * ratio * 0.5)) * 2 + 1
    width = max(1, width)
    maximum = max(1, int(math.floor(slot_pixels)) - 1)
    constrained = maximum if maximum & 1 else max(1, maximum - 1)
    return constrained if width > maximum else width


def _pixel_vertical_visibility(
    mapped_y: np.ndarray,
    screen_top: float,
    screen_bottom: float,
    *,
    margin: float = 0.0,
) -> np.ndarray:
    """Return bars whose transformed vertical extent can affect the viewport.

    ``mapped_y`` contains transformed open/close/low/high coordinates.  The
    helper deliberately uses row minima/maxima rather than semantic OHLC order
    so it remains correct for inverted Qt Y transforms, logarithmic values, and
    volume batches.  ``margin`` reserves pixels for bloom/glow and one-pixel
    raster expansion before culling.
    """
    values = np.asarray(mapped_y, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] < 1:
        return np.zeros(len(values), dtype=bool)
    low = min(float(screen_top), float(screen_bottom))
    high = max(float(screen_top), float(screen_bottom))
    pad = max(0.0, float(margin))
    finite = np.isfinite(values).all(axis=1)
    row_top = np.min(values, axis=1)
    row_bottom = np.max(values, axis=1)
    return finite & (row_bottom + pad >= low) & (row_top - pad <= high)


class _NativeBarGL:
    """Instanced OpenGL candle/volume renderer used inside a QOpenGLWidget viewport.

    History geometry is uploaded only when PixelBarBatch.set_data() changes.
    Pan and zoom are uniforms, so interaction does not rebuild candle geometry
    or cross the Python/C++ boundary once per bar.
    """

    GL_FLOAT = 0x1406
    GL_TRIANGLES = 0x0004
    GL_BLEND = 0x0BE2
    GL_SRC_ALPHA = 0x0302
    GL_ONE_MINUS_SRC_ALPHA = 0x0303
    GL_DEPTH_TEST = 0x0B71
    GL_CULL_FACE = 0x0B44
    GL_SCISSOR_TEST = 0x0C11

    DESKTOP_VERTEX = """#version 330 core
layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec4 a_data0;
layout(location = 2) in vec4 a_data1;

uniform vec4 u_map;          // sx, sy, tx, ty: data -> device pixels
uniform vec2 u_screen;       // framebuffer size in device pixels
uniform float u_body_width;
uniform float u_wick_width;
uniform float u_outline;
uniform float u_expand;
uniform int u_geometry;      // 0 body, 1 body-inner, 2 wick, 3 close tick
uniform vec4 u_up_color;
uniform vec4 u_down_color;
uniform int u_volume_overlay;
uniform float u_volume_max;
uniform float u_volume_height_fraction;
uniform float u_clip_top;
uniform float u_clip_bottom;

out vec4 v_color;
flat out float v_valid;

vec2 to_ndc(vec2 p) {
    return vec2(
        (p.x / u_screen.x) * 2.0 - 1.0,
        1.0 - (p.y / u_screen.y) * 2.0
    );
}

void main() {
    float x = a_data0.x;
    float open_v = a_data0.y;
    float close_v = a_data0.z;
    float low_v = a_data0.w;
    float high_v = a_data1.x;
    float rising = a_data1.y;
    float slot = a_data1.z;

    float pixel_left = floor(x * u_map.x + u_map.z);
    float center = pixel_left + 0.5;
    float open_px = open_v * u_map.y + u_map.w;
    float close_px = close_v * u_map.y + u_map.w;
    float low_px = low_v * u_map.y + u_map.w;
    float high_px = high_v * u_map.y + u_map.w;

    float slot_px = abs(u_map.x) * slot;
    float width_ratio = u_body_width;
    if (width_ratio < 0.20 || width_ratio > 0.98) {
        width_ratio = 0.72;
    }
    float desired = slot_px * width_ratio;
    float width_px = floor(desired * 0.5) * 2.0 + 1.0;
    width_px = max(width_px, 1.0);
    float max_width = max(1.0, floor(slot_px) - 1.0);
    if (width_px > max_width) {
        float odd_max = mod(max_width, 2.0) >= 1.0
            ? max_width
            : max(1.0, max_width - 1.0);
        width_px = odd_max;
    }

    float body_top;
    float body_bottom;
    if (u_volume_overlay != 0) {
        float available = max(1.0, u_clip_bottom - u_clip_top);
        float max_height = max(1.0, available * u_volume_height_fraction);
        float ratio = u_volume_max > 0.0
            ? clamp(close_v / u_volume_max, 0.0, 1.0)
            : 0.0;
        body_bottom = floor(u_clip_bottom);
        body_top = floor(body_bottom - max(1.0, ratio * max_height));
    } else {
        body_top = floor(min(open_px, close_px));
        body_bottom = floor(max(open_px, close_px));
        body_bottom = max(body_bottom, body_top + 1.0);
    }
    float body_left = center - width_px * 0.5;
    float body_right = center + width_px * 0.5;

    float left = body_left;
    float right = body_right;
    float top = body_top;
    float bottom = body_bottom;

    if (u_geometry == 1) {
        float inset = max(1.0, u_outline);
        left += inset;
        right -= inset;
        top += inset;
        bottom -= inset;
    } else if (u_geometry == 2) {
        float wick_top = floor(min(low_px, high_px));
        float wick_bottom = floor(max(low_px, high_px)) + 1.0;
        float half_wick = max(0.5, u_wick_width * 0.5);
        left = center - half_wick;
        right = center + half_wick;
        top = wick_top;
        bottom = wick_bottom;
    } else if (u_geometry == 3) {
        float length_px = max(1.0, floor(width_px * 0.48));
        float edge_y = clamp(floor(close_px), body_top, body_bottom - 1.0);
        left = rising >= 0.5 ? center : center - length_px;
        right = rising >= 0.5 ? center + length_px : center;
        top = edge_y;
        bottom = edge_y + 1.0;
    }

    left -= u_expand;
    right += u_expand;
    top -= u_expand;
    bottom += u_expand;

    v_valid = (right > left && bottom > top) ? 1.0 : 0.0;
    vec2 pixel = vec2(
        mix(left, right, a_corner.x),
        mix(top, bottom, a_corner.y)
    );
    gl_Position = vec4(to_ndc(pixel), 0.0, 1.0);
    v_color = rising >= 0.5 ? u_up_color : u_down_color;
}
"""

    DESKTOP_FRAGMENT = """#version 330 core
in vec4 v_color;
flat in float v_valid;
out vec4 frag_color;
void main() {
    if (v_valid < 0.5 || v_color.a <= 0.0) {
        discard;
    }
    frag_color = v_color;
}
"""

    ES_VERTEX = """#version 300 es
precision highp float;
layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec4 a_data0;
layout(location = 2) in vec4 a_data1;

uniform vec4 u_map;
uniform vec2 u_screen;
uniform float u_body_width;
uniform float u_wick_width;
uniform float u_outline;
uniform float u_expand;
uniform int u_geometry;
uniform vec4 u_up_color;
uniform vec4 u_down_color;
uniform int u_volume_overlay;
uniform float u_volume_max;
uniform float u_volume_height_fraction;
uniform float u_clip_top;
uniform float u_clip_bottom;

out vec4 v_color;
flat out float v_valid;

vec2 to_ndc(vec2 p) {
    return vec2(
        (p.x / u_screen.x) * 2.0 - 1.0,
        1.0 - (p.y / u_screen.y) * 2.0
    );
}

void main() {
    float x = a_data0.x;
    float open_v = a_data0.y;
    float close_v = a_data0.z;
    float low_v = a_data0.w;
    float high_v = a_data1.x;
    float rising = a_data1.y;
    float slot = a_data1.z;

    float pixel_left = floor(x * u_map.x + u_map.z);
    float center = pixel_left + 0.5;
    float open_px = open_v * u_map.y + u_map.w;
    float close_px = close_v * u_map.y + u_map.w;
    float low_px = low_v * u_map.y + u_map.w;
    float high_px = high_v * u_map.y + u_map.w;

    float slot_px = abs(u_map.x) * slot;
    float width_ratio = u_body_width;
    if (width_ratio < 0.20 || width_ratio > 0.98) {
        width_ratio = 0.72;
    }
    float desired = slot_px * width_ratio;
    float width_px = floor(desired * 0.5) * 2.0 + 1.0;
    width_px = max(width_px, 1.0);
    float max_width = max(1.0, floor(slot_px) - 1.0);
    if (width_px > max_width) {
        float odd_max = mod(max_width, 2.0) >= 1.0
            ? max_width
            : max(1.0, max_width - 1.0);
        width_px = odd_max;
    }

    float body_top;
    float body_bottom;
    if (u_volume_overlay != 0) {
        float available = max(1.0, u_clip_bottom - u_clip_top);
        float max_height = max(1.0, available * u_volume_height_fraction);
        float ratio = u_volume_max > 0.0
            ? clamp(close_v / u_volume_max, 0.0, 1.0)
            : 0.0;
        body_bottom = floor(u_clip_bottom);
        body_top = floor(body_bottom - max(1.0, ratio * max_height));
    } else {
        body_top = floor(min(open_px, close_px));
        body_bottom = floor(max(open_px, close_px));
        body_bottom = max(body_bottom, body_top + 1.0);
    }
    float body_left = center - width_px * 0.5;
    float body_right = center + width_px * 0.5;

    float left = body_left;
    float right = body_right;
    float top = body_top;
    float bottom = body_bottom;

    if (u_geometry == 1) {
        float inset = max(1.0, u_outline);
        left += inset;
        right -= inset;
        top += inset;
        bottom -= inset;
    } else if (u_geometry == 2) {
        float wick_top = floor(min(low_px, high_px));
        float wick_bottom = floor(max(low_px, high_px)) + 1.0;
        float half_wick = max(0.5, u_wick_width * 0.5);
        left = center - half_wick;
        right = center + half_wick;
        top = wick_top;
        bottom = wick_bottom;
    } else if (u_geometry == 3) {
        float length_px = max(1.0, floor(width_px * 0.48));
        float edge_y = clamp(floor(close_px), body_top, body_bottom - 1.0);
        left = rising >= 0.5 ? center : center - length_px;
        right = rising >= 0.5 ? center + length_px : center;
        top = edge_y;
        bottom = edge_y + 1.0;
    }

    left -= u_expand;
    right += u_expand;
    top -= u_expand;
    bottom += u_expand;

    v_valid = (right > left && bottom > top) ? 1.0 : 0.0;
    vec2 pixel = vec2(
        mix(left, right, a_corner.x),
        mix(top, bottom, a_corner.y)
    );
    gl_Position = vec4(to_ndc(pixel), 0.0, 1.0);
    v_color = rising >= 0.5 ? u_up_color : u_down_color;
}
"""

    ES_FRAGMENT = """#version 300 es
precision highp float;
in vec4 v_color;
flat in float v_valid;
out vec4 frag_color;
void main() {
    if (v_valid < 0.5 || v_color.a <= 0.0) {
        discard;
    }
    frag_color = v_color;
}
"""


    @staticmethod
    def _preblended_body(
        background: str,
        color: str | QtGui.QColor,
        alpha: int,
    ) -> QtGui.QColor:
        source = QtGui.QColor(color)
        alpha = max(0, min(255, int(alpha)))
        if alpha >= 255:
            source.setAlpha(255)
            return source
        surface = QtGui.QColor(background)
        mix = alpha / 255.0
        return QtGui.QColor(
            round(surface.red() * (1.0 - mix) + source.red() * mix),
            round(surface.green() * (1.0 - mix) + source.green() * mix),
            round(surface.blue() * (1.0 - mix) + source.blue() * mix),
            255,
        )

    @classmethod
    def create_resources(cls, context: Any) -> dict[str, Any] | None:
        if QtOpenGL is None or context is None:
            return None
        try:
            fmt = context.format()
            major = int(fmt.majorVersion())
            minor = int(fmt.minorVersion())
            if context.isOpenGLES():
                if major < 3:
                    return None
                vertex_source = cls.ES_VERTEX
                fragment_source = cls.ES_FRAGMENT
            else:
                if (major, minor) < (3, 3):
                    return None
                vertex_source = cls.DESKTOP_VERTEX
                fragment_source = cls.DESKTOP_FRAGMENT

            program = QtOpenGL.QOpenGLShaderProgram()
            vertex_type = _qt_enum_member(
                QtOpenGL.QOpenGLShader,
                "ShaderTypeBit",
                "Vertex",
            )
            fragment_type = _qt_enum_member(
                QtOpenGL.QOpenGLShader,
                "ShaderTypeBit",
                "Fragment",
            )
            add_shader = getattr(
                program,
                "addCacheableShaderFromSourceCode",
                program.addShaderFromSourceCode,
            )
            if not add_shader(vertex_type, vertex_source):
                return None
            if not add_shader(fragment_type, fragment_source):
                return None
            if not program.link():
                return None

            vao = QtOpenGL.QOpenGLVertexArrayObject()
            if not vao.create():
                return None

            buffer_type = _qt_enum_member(
                QtOpenGL.QOpenGLBuffer,
                "Type",
                "VertexBuffer",
            )
            corner_vbo = QtOpenGL.QOpenGLBuffer(buffer_type)
            instance_vbo = QtOpenGL.QOpenGLBuffer(buffer_type)
            if not corner_vbo.create() or not instance_vbo.create():
                return None

            usage = _qt_enum_member(
                QtOpenGL.QOpenGLBuffer,
                "UsagePattern",
                "DynamicDraw",
            )
            instance_vbo.setUsagePattern(usage)

            corners = np.asarray(
                (
                    (0.0, 0.0),
                    (1.0, 0.0),
                    (0.0, 1.0),
                    (0.0, 1.0),
                    (1.0, 0.0),
                    (1.0, 1.0),
                ),
                dtype=np.float32,
            )

            vao.bind()
            program.bind()

            corner_vbo.bind()
            corner_vbo.allocate(corners.tobytes(), corners.nbytes)
            program.enableAttributeArray(0)
            program.setAttributeBuffer(0, cls.GL_FLOAT, 0, 2, 8)

            instance_vbo.bind()
            placeholder = np.zeros((1, 8), dtype=np.float32)
            instance_vbo.allocate(placeholder.tobytes(), placeholder.nbytes)
            program.enableAttributeArray(1)
            program.setAttributeBuffer(1, cls.GL_FLOAT, 0, 4, 32)
            program.enableAttributeArray(2)
            program.setAttributeBuffer(2, cls.GL_FLOAT, 16, 4, 32)

            functions = context.extraFunctions()
            functions.glVertexAttribDivisor(0, 0)
            functions.glVertexAttribDivisor(1, 1)
            functions.glVertexAttribDivisor(2, 1)

            instance_vbo.release()
            corner_vbo.release()
            program.release()
            vao.release()

            uniforms = {
                name: program.uniformLocation(name)
                for name in (
                    "u_map",
                    "u_screen",
                    "u_body_width",
                    "u_wick_width",
                    "u_outline",
                    "u_expand",
                    "u_geometry",
                    "u_up_color",
                    "u_down_color",
                    "u_volume_overlay",
                    "u_volume_max",
                    "u_volume_height_fraction",
                    "u_clip_top",
                    "u_clip_bottom",
                )
            }

            for slot_pixels in (2.0, 3.5, 6.0, 9.25, 14.0):
                for ratio in (0.60, 0.62, 0.73):
                    expected = _pixel_bar_width(slot_pixels, ratio)
                    actual = int(math.floor(slot_pixels * ratio * 0.5)) * 2 + 1
                    actual = max(1, actual)
                    maximum = max(1, int(math.floor(slot_pixels)) - 1)
                    constrained = (
                        maximum if maximum & 1 else max(1, maximum - 1)
                    )
                    if actual > maximum:
                        actual = constrained
                    if actual != expected:
                        return None

            return {
                "program": program,
                "vao": vao,
                "corner_vbo": corner_vbo,
                "instance_vbo": instance_vbo,
                "context": context,
                "uniforms": uniforms,
                "revision": -1,
                "count": 0,
            }
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None

    @classmethod
    def upload(
        cls,
        resources: dict[str, Any],
        data: np.ndarray,
        revision: int,
    ) -> bool:
        if resources.get("revision") == revision:
            return True
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        profile_name = str(resources.get("profile_name") or "bars")
        try:
            prepared = resources["prepared"]
            payload = prepared.payload
            required = len(payload)
            x_origin, y_origin = prepared.origin
            vbo = resources["instance_vbo"]
            if not vbo.bind():
                return False



            capacity = max(int(resources.get("capacity", 0)), 1 << max(5, (required-1).bit_length()))
            vbo.allocate(capacity)
            vbo.write(0, payload, required)
            vbo.release()
            resources["capacity"] = capacity
            resources["revision"] = revision
            resources["count"] = len(data)
            resources["x_origin"] = x_origin
            resources["y_origin"] = y_origin
            if profile_started:
                record_performance_timing(
                    f"gl.upload.{profile_name}_ms",
                    (time.perf_counter() - profile_started) * 1000.0,
                )
                record_performance_count(f"gl.upload.{profile_name}_count")
                record_performance_sum(f"gl.upload.{profile_name}_rows", len(data))
            return True
        except (AttributeError, RuntimeError, TypeError, ValueError, MemoryError):
            if profile_started:
                record_performance_count(f"gl.upload.{profile_name}_failures")
                record_performance_timing(
                    f"gl.upload.{profile_name}_ms",
                    (time.perf_counter() - profile_started) * 1000.0,
                )
            return False

    @classmethod
    def _style_state(
        cls,
        resources: dict[str, Any],
        style: dict[str, Any],
        style_key: object,
        background: str,
        up: str,
        down: str,
    ) -> dict[str, Any]:
        key = (
            style_key,
            background,
            up,
            down,
        )
        if resources.get("style_key") == key:
            cached = resources.get("style_state")
            if isinstance(cached, dict):
                return cached

        up_color = QtGui.QColor(style.get("up_color", up))
        down_color = QtGui.QColor(style.get("down_color", down))
        body_alpha = int(style.get("body_alpha", 255))
        up_body = cls._preblended_body(
            background,
            style.get("up_body_color", up_color),
            body_alpha,
        )
        down_body = cls._preblended_body(
            background,
            style.get("down_body_color", down_color),
            body_alpha,
        )
        if style.get("hollow") or style.get("hollow_up"):
            up_body = QtGui.QColor(background)
        if style.get("hollow") or style.get("hollow_down"):
            down_body = QtGui.QColor(background)

        bloom_up = style.get("up_bloom_color")
        bloom_down = style.get("down_bloom_color")
        bloom_radius = max(
            int(style.get("up_bloom_radius", 0)),
            int(style.get("down_bloom_radius", 0)),
        )
        if bloom_up or bloom_down:
            glow_up = (
                QtGui.QColor(bloom_up)
                if bloom_up
                else QtGui.QColor(0, 0, 0, 0)
            )
            glow_down = (
                QtGui.QColor(bloom_down)
                if bloom_down
                else QtGui.QColor(0, 0, 0, 0)
            )
            glow_expand = float(max(1, bloom_radius))
        else:
            glow_alpha = max(
                0,
                min(255, int(style.get("glow_alpha", 0))),
            )
            if glow_alpha:
                glow_up = QtGui.QColor(up_color)
                glow_down = QtGui.QColor(down_color)
                glow_up.setAlpha(glow_alpha)
                glow_down.setAlpha(glow_alpha)
                glow_expand = max(
                    1.0,
                    float(style.get("glow_width", 2.0)) * 0.5,
                )
            else:
                glow_up = None
                glow_down = None
                glow_expand = 0.0

        if style.get("close_tick"):
            tick_up = QtGui.QColor(up_color).lighter(132)
            tick_down = QtGui.QColor(down_color).lighter(132)
            tick_up.setAlpha(225)
            tick_down.setAlpha(225)
        else:
            tick_up = None
            tick_down = None

        body_width = float(style.get("width", 0.72))
        if not math.isfinite(body_width) or body_width < 0.20 or body_width > 0.98:
            body_width = 0.72
        state = {
            "body_width": body_width,
            "wick_width": max(0.6, float(style.get("wick", 1.0))),
            "outline": max(0.6, float(style.get("outline", 1.0))),
            "up": up_color,
            "down": down_color,
            "up_body": up_body,
            "down_body": down_body,
            "glow_up": glow_up,
            "glow_down": glow_down,
            "glow_expand": glow_expand,
            "tick_up": tick_up,
            "tick_down": tick_down,
        }
        resources["style_key"] = key
        resources["style_state"] = state
        return state

    @classmethod
    def draw(
        cls,
        resources: dict[str, Any],
        context: Any,
        data: np.ndarray,
        revision: int,
        transform: QtGui.QTransform,
        framebuffer_size: tuple[int, int],
        clip_screen: QtCore.QRectF,
        style: dict[str, Any],
        style_key: object,
        background: str,
        up: str,
        down: str,
        *,
        volume: bool,
        volume_overlay_max: float = 0.0,
        volume_overlay_fraction: float = 0.0,
        volume_overlay_screen: QtCore.QRectF | None = None,
    ) -> bool:
        if not len(data) or not cls.upload(resources, data, revision):
            return False

        width, height = framebuffer_size
        if width <= 0 or height <= 0:
            return False





        if (
            not transform.isAffine()
            or abs(float(transform.m12())) > 1e-12
            or abs(float(transform.m21())) > 1e-12
        ):
            return False

        sx = float(transform.m11())
        sy = float(transform.m22())
        tx = float(transform.dx())
        ty = float(transform.dy())
        if (
            not all(math.isfinite(value) for value in (sx, sy, tx, ty))
            or abs(sx) < 1e-18
            or abs(sy) < 1e-18
        ):
            return False

        try:
            program = resources["program"]
            vao = resources["vao"]
            functions = context.extraFunctions()

            left = max(0, int(math.floor(clip_screen.left())))
            top = max(0, int(math.floor(clip_screen.top())))
            right = min(width, int(math.ceil(clip_screen.right())))
            bottom = min(height, int(math.ceil(clip_screen.bottom())))
            if right <= left or bottom <= top:
                return True

            overlay_enabled = bool(
                volume
                and math.isfinite(float(volume_overlay_max))
                and float(volume_overlay_max) > 0.0
                and math.isfinite(float(volume_overlay_fraction))
                and float(volume_overlay_fraction) > 0.0
            )
            overlay_screen = (
                QtCore.QRectF(volume_overlay_screen).normalized()
                if overlay_enabled and volume_overlay_screen is not None
                else QtCore.QRectF(clip_screen).normalized()
            )

            functions.glViewport(0, 0, width, height)
            functions.glEnable(cls.GL_BLEND)
            functions.glBlendFunc(cls.GL_SRC_ALPHA, cls.GL_ONE_MINUS_SRC_ALPHA)
            functions.glDisable(cls.GL_DEPTH_TEST)
            functions.glDisable(cls.GL_CULL_FACE)
            functions.glEnable(cls.GL_SCISSOR_TEST)
            functions.glScissor(
                left,
                max(0, height - bottom),
                right - left,
                bottom - top,
            )

            vao.bind()
            program.bind()

            x_origin = float(resources.get("x_origin", 0.0))
            y_origin = float(resources.get("y_origin", 0.0))
            mapped_origin = transform.map(
                QtCore.QPointF(x_origin, 0.0 if overlay_enabled else y_origin)
            )
            uniforms = resources["uniforms"]
            functions.glUniform4f(
                int(uniforms["u_map"]),
                float(sx),
                1.0 if overlay_enabled else float(sy),
                float(mapped_origin.x()),
                0.0 if overlay_enabled else float(mapped_origin.y()),
            )
            functions.glUniform2f(
                int(uniforms["u_screen"]),
                float(width),
                float(height),
            )
            functions.glUniform1i(
                int(uniforms["u_volume_overlay"]),
                1 if overlay_enabled else 0,
            )
            functions.glUniform1f(
                int(uniforms["u_volume_max"]),
                float(volume_overlay_max) if overlay_enabled else 0.0,
            )
            functions.glUniform1f(
                int(uniforms["u_volume_height_fraction"]),
                max(0.0, min(1.0, float(volume_overlay_fraction)))
                if overlay_enabled
                else 0.0,
            )
            functions.glUniform1f(
                int(uniforms["u_clip_top"]),
                float(overlay_screen.top()) if overlay_enabled else float(top),
            )
            functions.glUniform1f(
                int(uniforms["u_clip_bottom"]),
                float(overlay_screen.bottom()) if overlay_enabled else float(bottom),
            )
            state = cls._style_state(
                resources,
                style,
                style_key,
                background,
                up,
                down,
            )
            functions.glUniform1f(
                int(uniforms["u_body_width"]),
                float(state["body_width"]),
            )
            functions.glUniform1f(
                int(uniforms["u_wick_width"]),
                float(state["wick_width"]),
            )
            functions.glUniform1f(
                int(uniforms["u_outline"]),
                float(state["outline"]),
            )

            up_color = state["up"]
            down_color = state["down"]



            up_body = state["up_body"]
            down_body = state["down_body"]
            count = int(resources.get("count", 0))

            def draw_pass(
                geometry: int,
                up_pass: QtGui.QColor,
                down_pass: QtGui.QColor,
                *,
                expand: float = 0.0,
            ) -> None:
                functions.glUniform1i(
                    int(uniforms["u_geometry"]),
                    int(geometry),
                )
                functions.glUniform1f(
                    int(uniforms["u_expand"]),
                    float(expand),
                )
                functions.glUniform4f(
                    int(uniforms["u_up_color"]),
                    float(up_pass.redF()),
                    float(up_pass.greenF()),
                    float(up_pass.blueF()),
                    float(up_pass.alphaF()),
                )
                functions.glUniform4f(
                    int(uniforms["u_down_color"]),
                    float(down_pass.redF()),
                    float(down_pass.greenF()),
                    float(down_pass.blueF()),
                    float(down_pass.alphaF()),
                )
                functions.glDrawArraysInstanced(
                    cls.GL_TRIANGLES,
                    0,
                    6,
                    count,
                )



            up_glow = state["glow_up"]
            down_glow = state["glow_down"]
            expand = state["glow_expand"]
            if (
                not overlay_enabled
                and up_glow is not None
                and down_glow is not None
                and expand > 0.0
            ):
                if not volume:
                    draw_pass(2, up_glow, down_glow, expand=expand)
                draw_pass(0, up_glow, down_glow, expand=expand)

            if not volume:
                draw_pass(2, up_color, down_color)

            if volume:
                draw_pass(0, up_color, down_color)
                hollow_up = bool(style.get("hollow") or style.get("hollow_up"))
                hollow_down = bool(style.get("hollow") or style.get("hollow_down"))
                if hollow_up or hollow_down:
                    transparent = QtGui.QColor(0, 0, 0, 0)
                    draw_pass(
                        1,
                        QtGui.QColor(background) if hollow_up else transparent,
                        QtGui.QColor(background) if hollow_down else transparent,
                    )
            else:


                draw_pass(0, up_color, down_color)
                draw_pass(1, up_body, down_body)

            up_tick = state["tick_up"]
            down_tick = state["tick_down"]
            if not volume and up_tick is not None and down_tick is not None:
                draw_pass(3, up_tick, down_tick)

            program.release()
            vao.release()
            functions.glDisable(cls.GL_SCISSOR_TEST)
            return True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            try:
                resources["program"].release()
                resources["vao"].release()
            except (AttributeError, RuntimeError):
                pass
            return False


class PixelBarBatch:
    """Cache a whole series as physical-pixel rectangles, never one item per bar."""

    def __init__(self):
        self.data = np.empty((0, 7))
        self.profile_name = "bars"
        self.cache_key = None
        self.batches = []
        self._rect_batch_source = None
        self._rect_batches = []
        self.cache_origin = (0.0, 0.0)
        self.cache_screen = QtCore.QRectF()
        self.gpu_enabled = False
        self._data_revision = 0
        self._gl_resources: dict[int, dict[str, Any]] = {}
        self._gl_disabled_contexts: set[int] = set()
        self._reported_native_failures: set[str] = set()
        self._gl_cleanup_callbacks: dict[int, Any] = {}
        self._gl_orphaned_resources: list[dict[str, Any]] = []
        self.prepared = prepare_bars([])

    def set_data(self, candles, slots, logarithmic=False, volume=False):
        prepared = candles if isinstance(candles, PreparedBars) else prepare_bars(
            candles, slots, logarithmic, volume
        )
        if prepared is self.prepared:
            return QtCore.QRectF(*prepared.bounds)
        self.prepared = prepared
        self.data = prepared.data
        self.cache_key = None
        self.batches = []
        self._data_revision += 1
        return QtCore.QRectF(*prepared.bounds)

    def _draw_cpu_batches(self, painter):




        if self._rect_batch_source is not self.batches:
            self._rect_batch_source = self.batches
            self._rect_batches = []
            for brush, path in self.batches:
                rects = None
                disjoint = False
                if brush.style() == Qt.BrushStyle.SolidPattern:
                    polygons = path.toSubpathPolygons()
                    if all(len(poly) == 5 and poly[0] == poly[-1] for poly in polygons):
                        rects = [poly.boundingRect() for poly in polygons]
                        ordered = sorted(rects, key=lambda rect: rect.left())
                        disjoint = all(a.right() <= b.left() for a,b in zip(ordered, ordered[1:]))
                self._rect_batches.append((brush, path, rects, disjoint))
        for brush, path, rects, disjoint in self._rect_batches:
            painter.setBrush(brush)
            if rects is not None and (disjoint or (brush.isOpaque() and painter.opacity() == 1.0)):
                painter.drawRects(rects)
            else:
                painter.drawPath(path)

    @staticmethod
    def _append_rectangles(
        target: QtGui.QPainterPath,
        x: np.ndarray,
        y: np.ndarray,
        width: np.ndarray,
        height: np.ndarray,
        clip: QtCore.QRectF,
        *,
        expand: float = 0.0,
    ) -> None:
        """Clip rectangle arrays once and append them directly to ``target``.

        PySide6 exposes ``QPainterPath.addRects`` on supported Qt builds.  Use
        that bulk boundary when available so a rebuild does not perform one
        Python->Qt call per rectangle.  The scalar fallback keeps compatibility
        with older bindings without allocating QRectF wrappers unnecessarily.
        """
        if not len(x):
            return

        expand = float(expand)
        left = np.asarray(x, dtype=np.float64) - expand
        top = np.asarray(y, dtype=np.float64) - expand
        right = np.asarray(x, dtype=np.float64) + np.asarray(width, dtype=np.float64) + expand
        bottom = np.asarray(y, dtype=np.float64) + np.asarray(height, dtype=np.float64) + expand

        clip_left = float(clip.left())
        clip_top = float(clip.top())
        clip_right = float(clip.right())
        clip_bottom = float(clip.bottom())

        np.maximum(left, clip_left, out=left)
        np.maximum(top, clip_top, out=top)
        np.minimum(right, clip_right, out=right)
        np.minimum(bottom, clip_bottom, out=bottom)

        widths = right - left
        heights = bottom - top
        valid = (
            np.isfinite(left)
            & np.isfinite(top)
            & np.isfinite(widths)
            & np.isfinite(heights)
            & (widths > 0.0)
            & (heights > 0.0)
        )
        if not np.any(valid):
            return

        valid_left = left[valid]
        valid_top = top[valid]
        valid_width = widths[valid]
        valid_height = heights[valid]
        add_rects = getattr(target, "addRects", None)
        if callable(add_rects):


            chunk_size = 1024
            count = len(valid_left)
            for first in range(0, count, chunk_size):
                last = min(count, first + chunk_size)
                rects = [
                    QtCore.QRectF(float(rx), float(ry), float(rw), float(rh))
                    for rx, ry, rw, rh in zip(
                        valid_left[first:last],
                        valid_top[first:last],
                        valid_width[first:last],
                        valid_height[first:last],
                    )
                ]
                try:
                    add_rects(rects)
                except (TypeError, AttributeError):



                    add_rect = target.addRect
                    for rect in rects:
                        add_rect(rect)
                    for rx, ry, rw, rh in zip(
                        valid_left[last:],
                        valid_top[last:],
                        valid_width[last:],
                        valid_height[last:],
                    ):
                        add_rect(float(rx), float(ry), float(rw), float(rh))
                    return
            return

        add_rect = target.addRect
        for rx, ry, rw, rh in zip(
            valid_left, valid_top, valid_width, valid_height
        ):
            add_rect(float(rx), float(ry), float(rw), float(rh))

    @staticmethod
    def _rect_path(
        x: np.ndarray,
        y: np.ndarray,
        width: np.ndarray,
        height: np.ndarray,
        clip: QtCore.QRectF,
        *,
        expand: float = 0.0,
    ) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        PixelBarBatch._append_rectangles(
            path, x, y, width, height, clip, expand=expand
        )
        return path

    @staticmethod
    def _append_rect_path(
        target: QtGui.QPainterPath,
        x: np.ndarray,
        y: np.ndarray,
        width: np.ndarray,
        height: np.ndarray,
        clip: QtCore.QRectF,
        *,
        expand: float = 0.0,
    ) -> None:
        PixelBarBatch._append_rectangles(
            target, x, y, width, height, clip, expand=expand
        )

    def set_gpu_enabled(self, enabled: bool) -> None:
        self.gpu_enabled = bool(enabled)

    @staticmethod
    def _framebuffer_size(
        painter: QtGui.QPainter,
        dpr: float,
    ) -> tuple[int, int]:
        device = painter.device()
        return (
            max(1, int(round(float(device.width()) * dpr))),
            max(1, int(round(float(device.height()) * dpr))),
        )

    @staticmethod
    def _clip_screen_rect(
        painter: QtGui.QPainter,
        transform: QtGui.QTransform,
        framebuffer_size: tuple[int, int],
    ) -> QtCore.QRectF:
        width, height = framebuffer_size
        device_rect = QtCore.QRectF(0.0, 0.0, float(width), float(height))
        if not painter.hasClipping():
            return device_rect
        clip = painter.clipBoundingRect()
        if clip.isEmpty():
            return device_rect
        mapped = transform.mapRect(clip).normalized()
        clipped = mapped.intersected(device_rect)
        return clipped

    @staticmethod
    def _is_opengl_painter(painter: QtGui.QPainter) -> bool:
        engine = painter.paintEngine()
        if engine is None:
            return False
        engine_type = engine.type()
        enum_type = getattr(QtGui.QPaintEngine, "Type", QtGui.QPaintEngine)
        supported = {
            getattr(enum_type, "OpenGL", None),
            getattr(enum_type, "OpenGL2", None),
        }
        return engine_type in supported

    def _destroy_gl_resources(
        self,
        context_key: int,
        context: Any,
    ) -> None:
        resources = self._gl_resources.pop(context_key, None)
        self._gl_disabled_contexts.discard(context_key)
        self._gl_cleanup_callbacks.pop(context_key, None)
        if resources is None:
            return

        made_current = False
        try:
            if QtGui.QOpenGLContext.currentContext() is not context:
                surface = context.surface()
                if surface is None or not context.makeCurrent(surface):


                    self._gl_orphaned_resources.append(resources)
                    return
                made_current = True

            resources["instance_vbo"].destroy()
            resources["corner_vbo"].destroy()
            resources["vao"].destroy()
            program = resources.get("program")
            if program is not None:
                try:
                    program.release()
                    program.removeAllShaders()
                except (RuntimeError, AttributeError):
                    pass
                resources["program"] = None
                del program
            resources["context"] = None
        except (RuntimeError, AttributeError, TypeError):
            self._gl_orphaned_resources.append(resources)
        finally:
            if made_current:
                try:
                    context.doneCurrent()
                except (RuntimeError, AttributeError):
                    pass

    def _register_gl_context(
        self,
        context_key: int,
        context: Any,
    ) -> None:
        if context_key in self._gl_cleanup_callbacks:
            return

        def cleanup() -> None:
            self._destroy_gl_resources(context_key, context)

        self._gl_cleanup_callbacks[context_key] = cleanup
        try:
            direct_connection = _qt_enum_member(
                QtCore.Qt,
                "ConnectionType",
                "DirectConnection",
            )
            context.aboutToBeDestroyed.connect(
                cleanup,
                direct_connection,
            )
        except (RuntimeError, AttributeError, TypeError):


            pass

    def _record_native_failure(self, reason: str, *, warning: bool = False) -> bool:
        record_performance_count("gl.fallback." + reason.replace(" ", "_"))
        if warning and reason not in self._reported_native_failures:
            self._reported_native_failures.add(reason)
            logging.getLogger(__name__).warning(
                "Native chart rendering fell back to CPU (%s): %s", self.profile_name, reason
            )
        return False

    def _paint_native_gl(
        self,
        painter: QtGui.QPainter,
        transform: QtGui.QTransform,
        screen: QtCore.QRectF,
        framebuffer_size: tuple[int, int],
        style: dict[str, Any],
        style_key: object,
        background: str,
        up: str,
        down: str,
        *,
        volume: bool,
        volume_overlay_max: float = 0.0,
        volume_overlay_fraction: float = 0.0,
        volume_overlay_screen: QtCore.QRectF | None = None,
        native_painting_active: bool = False,
    ) -> bool:
        if not self.gpu_enabled:
            return False
        if QtOpenGL is None:
            return self._record_native_failure("QtOpenGL unavailable", warning=True)
        if not self._is_opengl_painter(painter):


            return self._record_native_failure("non-OpenGL paint engine")

        context = QtGui.QOpenGLContext.currentContext()
        if context is None or not context.isValid():
            return self._record_native_failure("invalid OpenGL context", warning=True)
        context_key = id(context)
        if context_key in self._gl_disabled_contexts:
            return self._record_native_failure("OpenGL context disabled after resource failure")

        resources = self._gl_resources.get(context_key)
        if resources is not None and resources.get("context") is not context:

            self._gl_orphaned_resources.append(resources)
            self._gl_resources.pop(context_key, None)
            resources = None

        if resources is None:
            resources = _NativeBarGL.create_resources(context)
            if resources is None:
                self._gl_disabled_contexts.add(context_key)
                return self._record_native_failure("OpenGL resource creation failed", warning=True)
            self._gl_resources[context_key] = resources
            self._register_gl_context(context_key, context)
        resources["profile_name"] = self.profile_name
        resources["prepared"] = self.prepared
        try:
            if not native_painting_active:
                painter.beginNativePainting()
            draw_started = time.perf_counter() if performance_profile_active() else 0.0
            drawn = _NativeBarGL.draw(
                resources,
                context,
                self.data,
                self._data_revision,
                transform,
                framebuffer_size,
                screen,
                style,
                style_key,
                background,
                up,
                down,
                volume=volume,
                volume_overlay_max=volume_overlay_max,
                volume_overlay_fraction=volume_overlay_fraction,
                volume_overlay_screen=volume_overlay_screen,
            )
            if draw_started:
                record_performance_timing(
                    f"gl.draw.{self.profile_name}_ms",
                    (time.perf_counter() - draw_started) * 1000.0,
                )
                record_performance_count(f"gl.draw.{self.profile_name}_count")
            if not native_painting_active:
                painter.endNativePainting()
        except (RuntimeError, TypeError, ValueError, AttributeError):
            if not native_painting_active:
                try:
                    painter.endNativePainting()
                except (RuntimeError, TypeError):
                    pass
            return self._record_native_failure("native OpenGL draw exception", warning=True)
        if not drawn:
            return self._record_native_failure("native OpenGL draw rejected", warning=True)
        return True

    def _paint_volume_overlay_cpu(
        self,
        painter: QtGui.QPainter,
        transform: QtGui.QTransform,
        clip_screen: QtCore.QRectF,
        overlay_screen: QtCore.QRectF,
        style: dict[str, Any],
        style_key: object,
        up: str,
        down: str,
        volume_max: float,
        height_fraction: float,
    ) -> None:
        """Render bottom-anchored volume in screen space without a volume Y axis.

        X remains chart/data space, so pan and zoom stay synchronized with the
        candles. Y is deliberately derived from the price ViewBox's physical
        pixels; price/log transforms therefore cannot move or rescale the bars.
        """
        if (
            not self.data.size
            or volume_max <= 0.0
            or height_fraction <= 0.0
            or overlay_screen.isEmpty()
        ):
            return

        dpr = max(1.0, float(painter.device().devicePixelRatioF()))
        fraction = max(0.01, min(0.80, float(height_fraction)))
        key = (
            "volume-overlay",
            transform.m11(),
            dpr,
            overlay_screen.getRect(),
            style_key,
            up,
            down,
            round(float(volume_max), 12),
            round(fraction, 6),
        )
        dx = transform.dx() - self.cache_origin[0]
        pan_dx = round(dx)

        if (
            key != self.cache_key
            or not self.cache_screen.translated(pan_dx, 0.0).contains(clip_screen)
        ):
            self.cache_key = key
            self.cache_origin = (transform.dx(), 0.0)
            dx = 0.0
            self.cache_screen = overlay_screen.adjusted(
                -overlay_screen.width(),
                0.0,
                overlay_screen.width(),
                0.0,
            )
            build_screen = self.cache_screen
            self.batches = []

            data = self.data
            x_ref = float(data[0, 0])
            origin_x = transform.map(QtCore.QPointF(x_ref, 0.0)).x()
            xs = (data[:, 0] - x_ref) * transform.m11() + origin_x
            slots = abs(transform.m11()) * data[:, 6]
            visible = (
                (xs + slots >= build_screen.left())
                & (xs - slots <= build_screen.right())
            )
            if not np.any(visible):
                return

            max_height = max(1.0, overlay_screen.height() * fraction)
            bottom = float(math.floor(overlay_screen.bottom()))
            width_ratio = float(style.get("width", 0.72))
            if (
                not math.isfinite(width_ratio)
                or width_ratio < 0.20
                or width_ratio > 0.98
            ):
                width_ratio = 0.72

            for rising, fallback in ((True, up), (False, down)):
                indices = np.flatnonzero((data[:, 5] == rising) & visible)
                if not len(indices):
                    continue
                selected_slots = slots[indices]
                desired = selected_slots * width_ratio
                widths = np.floor(desired * 0.5).astype(np.int64) * 2 + 1
                np.maximum(widths, 1, out=widths)
                max_widths = np.floor(selected_slots).astype(np.int64) - 1
                np.maximum(max_widths, 1, out=max_widths)
                constrained = np.where(
                    max_widths & 1,
                    max_widths,
                    np.maximum(1, max_widths - 1),
                )
                widths = np.where(widths > max_widths, constrained, widths)
                centers = np.floor(xs[indices]).astype(np.int64)
                lefts = centers - widths // 2

                volumes = np.maximum(0.0, data[indices, 2])
                heights = np.maximum(
                    1.0,
                    np.floor(np.minimum(1.0, volumes / volume_max) * max_height),
                )
                tops = bottom - heights
                path = self._rect_path(
                    lefts.astype(np.float64, copy=False),
                    tops.astype(np.float64, copy=False),
                    widths.astype(np.float64, copy=False),
                    heights.astype(np.float64, copy=False),
                    build_screen,
                )
                if not path.isEmpty():
                    color = style.get(
                        "up_color" if rising else "down_color",
                        fallback,
                    )
                    self.batches.append((QtGui.QBrush(QtGui.QColor(color)), path))

        painter.save()
        painter.resetTransform()
        painter.setWorldTransform(painter.deviceTransform().inverted()[0])
        painter.translate(round(dx), 0.0)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QtGui.QPen(Qt.PenStyle.NoPen))
        self._draw_cpu_batches(painter)
        painter.restore()

    def paint(
        self,
        painter,
        style,
        background,
        up,
        down,
        volume=False,
        exposed_rect=None,
        *,
        style_key: object | None = None,
        volume_overlay_max: float = 0.0,
        volume_overlay_fraction: float = 0.0,
        volume_overlay_screen: QtCore.QRectF | None = None,
        native_painting_active: bool = False,
    ):
        if not self.data.size:
            return
        transform = painter.deviceTransform()
        if not transform.isInvertible():
            return

        dpr = max(1.0, float(painter.device().devicePixelRatioF()))
        framebuffer_size = self._framebuffer_size(painter, dpr)
        gl_screen = self._clip_screen_rect(
            painter,
            transform,
            framebuffer_size,
        )
        if exposed_rect is not None:
            exposed_screen = transform.mapRect(
                QtCore.QRectF(exposed_rect)
            ).normalized()
            clipped = gl_screen.intersected(exposed_screen)
            gl_screen = clipped
        if gl_screen.isEmpty():
            return True





        stable_style_key = style_key if style_key is not None else id(style)



        if self._paint_native_gl(
            painter,
            transform,
            gl_screen,
            framebuffer_size,
            style,
            stable_style_key,
            background,
            up,
            down,
            volume=volume,
            volume_overlay_max=volume_overlay_max,
            volume_overlay_fraction=volume_overlay_fraction,
            volume_overlay_screen=volume_overlay_screen,
            native_painting_active=native_painting_active,
        ):
            return True
        if native_painting_active:
            return False

        if (
            volume
            and volume_overlay_max > 0.0
            and volume_overlay_fraction > 0.0
            and volume_overlay_screen is not None
        ):
            self._paint_volume_overlay_cpu(
                painter,
                transform,
                gl_screen,
                QtCore.QRectF(volume_overlay_screen).normalized(),
                style,
                stable_style_key,
                up,
                down,
                float(volume_overlay_max),
                float(volume_overlay_fraction),
            )
            return


        dpr = painter.device().devicePixelRatioF()
        painter.save()
        painter.resetTransform()


        screen = painter.deviceTransform().mapRect(
            QtCore.QRectF(painter.viewport())
        )
        painter.restore()

        key = (
            transform.m11(),
            transform.m22(),
            dpr,
            screen.getRect(),
            stable_style_key,
            background,
            up,
            down,
            volume,
        )
        dx = transform.dx() - self.cache_origin[0]
        dy = transform.dy() - self.cache_origin[1]
        pan_dx, pan_dy = round(dx), round(dy)

        if (
            key != self.cache_key
            or not self.cache_screen.translated(pan_dx, pan_dy).contains(screen)
        ):
            self.cache_key = key
            self.cache_origin = (transform.dx(), transform.dy())
            dx = dy = 0.0
            self.cache_screen = screen.adjusted(
                -screen.width(),
                -screen.height(),
                screen.width(),
                screen.height(),
            )
            build_screen = self.cache_screen
            self.batches = []

            data = self.data
            x_ref, y_ref = data[0, :2]
            origin = transform.map(QtCore.QPointF(x_ref, y_ref))
            xs = (data[:, 0] - x_ref) * transform.m11() + origin.x()
            ys = (data[:, 1:5] - y_ref) * transform.m22() + origin.y()
            slots = abs(transform.m11()) * data[:, 6]

            visible = (
                (xs + slots >= build_screen.left())
                & (xs - slots <= build_screen.right())
            )
            bloom_margin = max(
                0,
                int(style.get("up_bloom_radius", 0) or 0),
                int(style.get("down_bloom_radius", 0) or 0),
            )
            glow_margin = 0
            if int(style.get("glow_alpha", 0) or 0) > 0:
                glow_margin = max(
                    1,
                    int(math.ceil(float(style.get("glow_width", 2.0)) * 0.5)),
                )
            vertical_margin = float(max(bloom_margin, glow_margin) + 1)
            visible &= _pixel_vertical_visibility(
                ys,
                build_screen.top(),
                build_screen.bottom(),
                margin=vertical_margin,
            )

            for rising, fallback in ((True, up), (False, down)):
                mask = (data[:, 5] == rising) & visible
                indices = np.flatnonzero(mask)
                if not len(indices):
                    continue

                color = style.get(
                    "up_color" if rising else "down_color",
                    fallback,
                )
                hollow = bool(
                    style.get("hollow")
                    or style.get(
                        "hollow_up" if rising else "hollow_down"
                    )
                )
                body_color = style.get(
                    "up_body_color" if rising else "down_body_color",
                    color,
                )
                if volume:
                    fill = QtGui.QColor(background) if hollow else QtGui.QColor(color)
                elif hollow:
                    fill = QtGui.QColor(background)
                else:
                    fill = QtGui.QColor(body_color)
                    body_alpha = max(
                        0,
                        min(255, int(style.get("body_alpha", 255))),
                    )
                    if body_alpha < 255:
                        surface = QtGui.QColor(background)
                        mix = body_alpha / 255.0
                        fill = QtGui.QColor(
                            round(surface.red() * (1.0 - mix) + fill.red() * mix),
                            round(surface.green() * (1.0 - mix) + fill.green() * mix),
                            round(surface.blue() * (1.0 - mix) + fill.blue() * mix),
                        )

                selected_slots = slots[indices]
                width_ratio = float(style.get("width", 0.72))
                if (
                    not math.isfinite(width_ratio)
                    or width_ratio < 0.20
                    or width_ratio > 0.98
                ):
                    width_ratio = 0.72
                desired = selected_slots * width_ratio
                widths = (
                    np.floor(desired * 0.5).astype(np.int64) * 2 + 1
                )
                np.maximum(widths, 1, out=widths)

                max_widths = (
                    np.floor(selected_slots).astype(np.int64) - 1
                )
                np.maximum(max_widths, 1, out=max_widths)
                constrained = np.where(
                    max_widths & 1,
                    max_widths,
                    np.maximum(1, max_widths - 1),
                )
                widths = np.where(
                    widths > max_widths,
                    constrained,
                    widths,
                )

                centers = np.floor(xs[indices]).astype(np.int64)
                lefts = centers - widths // 2

                selected_y = ys[indices]
                opens = selected_y[:, 0]
                closes = selected_y[:, 1]
                lows = selected_y[:, 2]
                highs = selected_y[:, 3]

                tops = np.floor(np.minimum(opens, closes)).astype(np.int64)
                bottoms = np.floor(np.maximum(opens, closes)).astype(np.int64)

                if volume:
                    bottoms = np.floor(opens).astype(np.int64)
                    close_pixels = np.floor(closes).astype(np.int64)
                    tops = np.minimum(close_pixels, bottoms - 1)
                    wick_x = np.empty(0, dtype=np.float64)
                    wick_y = np.empty(0, dtype=np.float64)
                    wick_w = np.empty(0, dtype=np.float64)
                    wick_h = np.empty(0, dtype=np.float64)
                else:
                    bottoms = np.maximum(bottoms, tops + 1)
                    wick_tops = np.floor(
                        np.minimum(lows, highs)
                    ).astype(np.int64)
                    wick_bottoms = np.floor(
                        np.maximum(lows, highs)
                    ).astype(np.int64)
                    wick_x = centers.astype(np.float64, copy=False)
                    wick_y = wick_tops.astype(np.float64, copy=False)
                    wick_w = np.ones(len(indices), dtype=np.float64)
                    wick_h = (
                        np.maximum(1, wick_bottoms - wick_tops + 1)
                        .astype(np.float64, copy=False)
                    )

                heights = np.maximum(1, bottoms - tops)

                body_x = lefts.astype(np.float64, copy=False)
                body_y = tops.astype(np.float64, copy=False)
                body_w = widths.astype(np.float64, copy=False)
                body_h = heights.astype(np.float64, copy=False)

                wick_path = self._rect_path(
                    wick_x,
                    wick_y,
                    wick_w,
                    wick_h,
                    build_screen,
                )
                body_path = self._rect_path(
                    body_x,
                    body_y,
                    body_w,
                    body_h,
                    build_screen,
                )

                edge_path = QtGui.QPainterPath()
                edge_path.setFillRule(Qt.FillRule.WindingFill)
                has_edges = bool(hollow or fill != color)
                if has_edges:
                    one = np.ones(len(indices), dtype=np.float64)

                    self._append_rect_path(
                        edge_path,
                        np.concatenate((body_x, body_x, body_x, body_x + body_w - 1.0)),
                        np.concatenate((body_y, body_y + body_h - 1.0, body_y, body_y)),
                        np.concatenate((body_w, body_w, one, one)),
                        np.concatenate((one, one, body_h, body_h)),
                        build_screen,
                    )

                close_tick_path = QtGui.QPainterPath()
                close_tick_path.setFillRule(Qt.FillRule.WindingFill)
                close_tick_brush: QtGui.QBrush | None = None
                if not volume and style.get("close_tick"):
                    tick_length = np.maximum(
                        1.0,
                        np.floor(body_w * 0.48),
                    )
                    close_pixels = np.floor(closes)
                    tick_y = np.minimum(
                        body_y + body_h - 1.0,
                        np.maximum(body_y, close_pixels),
                    )
                    tick_x = np.where(
                        rising,
                        centers.astype(np.float64, copy=False),
                        centers.astype(np.float64, copy=False) - tick_length,
                    )
                    self._append_rect_path(
                        close_tick_path,
                        tick_x,
                        tick_y,
                        tick_length,
                        np.ones(len(indices), dtype=np.float64),
                        build_screen,
                    )
                    accent = QtGui.QColor(color).lighter(132)
                    accent.setAlpha(225)
                    close_tick_brush = QtGui.QBrush(accent)

                bloom_color = style.get(
                    "up_bloom_color" if rising else "down_bloom_color"
                )
                bloom_radius = max(
                    0,
                    int(
                        style.get(
                            "up_bloom_radius"
                            if rising
                            else "down_bloom_radius",
                            0,
                        )
                    ),
                )
                bloom_brush: QtGui.QBrush | None = None
                if bloom_color and bloom_radius:
                    bloom_brush = QtGui.QBrush(QtGui.QColor(bloom_color))
                else:
                    glow_alpha = max(
                        0,
                        min(255, int(style.get("glow_alpha", 0))),
                    )
                    if glow_alpha:
                        glow = QtGui.QColor(color)
                        glow.setAlpha(glow_alpha)
                        bloom_brush = QtGui.QBrush(glow)
                        bloom_radius = max(
                            1,
                            int(math.ceil(float(style.get("glow_width", 2.0)) * 0.5)),
                        )

                bloom_path = QtGui.QPainterPath()
                bloom_path.setFillRule(Qt.FillRule.WindingFill)
                if bloom_brush is not None and bloom_radius:



                    one = np.ones(len(indices), dtype=np.float64)
                    self._append_rect_path(
                        bloom_path,
                        np.concatenate((wick_x, body_x, body_x, body_x, body_x + body_w - 1.0)),
                        np.concatenate((wick_y, body_y, body_y + body_h - 1.0, body_y, body_y)),
                        np.concatenate((wick_w, body_w, body_w, one, one)),
                        np.concatenate((wick_h, one, one, body_h, body_h)),
                        build_screen,
                        expand=bloom_radius,
                    )



                if bloom_brush is not None and not bloom_path.isEmpty():
                    self.batches.append((bloom_brush, bloom_path))
                if color and not wick_path.isEmpty():
                    self.batches.append(
                        (QtGui.QBrush(QtGui.QColor(color)), wick_path)
                    )
                if not body_path.isEmpty():
                    self.batches.append((QtGui.QBrush(fill), body_path))
                if color and not edge_path.isEmpty():
                    self.batches.append(
                        (QtGui.QBrush(QtGui.QColor(color)), edge_path)
                    )
                if close_tick_brush is not None and not close_tick_path.isEmpty():
                    self.batches.append((close_tick_brush, close_tick_path))

        painter.save()


        painter.resetTransform()
        painter.setWorldTransform(painter.deviceTransform().inverted()[0])
        painter.translate(round(dx), round(dy))
        painter.setRenderHint(
            QtGui.QPainter.RenderHint.Antialiasing,
            False,
        )
        painter.setPen(QtGui.QPen(Qt.PenStyle.NoPen))

        self._draw_cpu_batches(painter)

        painter.restore()


class CandlestickItem(pg.GraphicsObject):
    def __init__(
        self,
        interval_seconds: int,
        up: str,
        down: str,
        background: str,
        style_name: str = "Hollow",
    ):
        super().__init__()
        self.interval_seconds = interval_seconds
        self.up = up
        self.down = down
        self.background = background
        self.style_name = style_name if style_name in CANDLE_STYLES else "Inked"
        self._style_key = tuple(CANDLE_STYLES[self.style_name].items())
        self.logarithmic = False
        self.bounds = QtCore.QRectF()
        self.pixel_batch = PixelBarBatch()
        self.painted_once = False
        self._native_composite: NativeBarCompositeItem | None = None


    def set_native_composite(self, composite: "NativeBarCompositeItem | None") -> None:
        self._native_composite = composite

    def set_colors(self, up: str, down: str, background: str) -> None:
        self.up = up
        self.down = down
        self.background = background
        if self._native_composite is not None:
            self._native_composite.update()

    def set_style(self, style_name: str) -> None:
        self.style_name = style_name if style_name in CANDLE_STYLES else "Inked"



        self._style_key = tuple(CANDLE_STYLES[self.style_name].items())
        if self._native_composite is not None:
            self._native_composite.update()

    def set_logarithmic(self, enabled: bool) -> None:
        self.logarithmic = enabled

    def set_gpu_enabled(self, enabled: bool) -> None:
        self.pixel_batch.set_gpu_enabled(enabled)

    def _body_color(self, color: QtGui.QColor, alpha: int) -> QtGui.QColor:
        """Blend into the chart surface so no grid or wick can bleed through a body."""
        surface = QtGui.QColor(self.background)
        mix = max(0.0, min(1.0, alpha / 255.0))
        return QtGui.QColor(
            round(surface.red() * (1.0 - mix) + color.red() * mix),
            round(surface.green() * (1.0 - mix) + color.green() * mix),
            round(surface.blue() * (1.0 - mix) + color.blue() * mix),
            255,
        )

    def set_data(self, candles, previous_close: float | None = None, slots=None) -> None:
        del previous_close
        previous_bounds = QtCore.QRectF(self.bounds)
        updated_bounds = self.pixel_batch.set_data(
            candles,
            slots if slots is not None else self.interval_seconds,
            self.logarithmic,
        )
        geometry_changed = updated_bounds != self.bounds
        if performance_profile_active():
            record_performance_count(
                "bars.geometry_changed" if geometry_changed else "bars.geometry_unchanged"
            )
        if geometry_changed:
            self.prepareGeometryChange()
            self.bounds = updated_bounds
        if not self.pixel_batch.data.size:
            self.painted_once = False
        if self._native_composite is not None:
            if previous_bounds.isEmpty():
                dirty_bounds = QtCore.QRectF(updated_bounds)
            elif updated_bounds.isEmpty():
                dirty_bounds = previous_bounds
            else:
                dirty_bounds = previous_bounds.united(updated_bounds)
            self._native_composite.refresh_bounds(dirty_rect=dirty_bounds)
        else:
            self.update()

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionGraphicsItem,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        del widget
        if self._native_composite is not None:
            return
        if not self.pixel_batch.data.size:
            return
        self.pixel_batch.paint(
            painter, CANDLE_STYLES[self.style_name], self.background, self.up, self.down,
            exposed_rect=option.exposedRect, style_key=self._style_key,
        )
        self.painted_once = True

    def boundingRect(self) -> QtCore.QRectF:
        return self.bounds


class VolumeOverlayItem(pg.GraphicsObject):
    """TradingView-style volume underlay anchored to the price ViewBox bottom.

    The item deliberately owns no Y data scale. History and live volume retain
    independent VBOs so a live tick uploads only one instance, while both batches
    share one visible-range maximum and one screen-space height fraction.
    """

    _BOUND_Y = 1.0e15


    _BAR_ALPHA = 112

    @classmethod
    def _volume_color(cls, color: str) -> str:
        value = QtGui.QColor(color)
        value.setAlpha(cls._BAR_ALPHA)
        return value.name(QtGui.QColor.NameFormat.HexArgb)

    def __init__(
        self,
        interval_seconds: int,
        up: str,
        down: str,
        background: str,
        style_name: str = "Hollow",
        *,
        height_percent: float = 30.0,
    ):
        super().__init__()
        self.history_interval_seconds = max(1, int(interval_seconds))
        self.live_interval_seconds = max(1, int(interval_seconds))
        self.up = self._volume_color(up)
        self.down = self._volume_color(down)
        self.background = background
        self.style_name = style_name if style_name in CANDLE_STYLES else "Inked"
        self._style_key = tuple(CANDLE_STYLES[self.style_name].items())
        self._height_fraction = 0.30
        self.set_height_percent(height_percent, repaint=False)
        self.history_batch = PixelBarBatch()
        self.live_batch = PixelBarBatch()
        self._history_bounds = QtCore.QRectF()
        self._live_bounds = QtCore.QRectF()
        self._bounds = QtCore.QRectF()
        self._visible_scale_key: tuple[Any, ...] | None = None
        self._visible_scale_max = 0.0
        self.painted_once = False
        self._native_composite: NativeBarCompositeItem | None = None
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)

    @property
    def height_percent(self) -> int:
        return int(round(self._height_fraction * 100.0))

    def set_native_composite(self, composite: "NativeBarCompositeItem | None") -> None:
        self._native_composite = composite

    def set_height_percent(self, value: float | int, *, repaint: bool = True) -> None:
        fraction = max(0.05, min(0.45, float(value) / 100.0))
        if abs(fraction - self._height_fraction) <= 1e-9:
            return
        self._height_fraction = fraction
        if repaint:
            self.history_batch.cache_key = None
            self.live_batch.cache_key = None
            if self._native_composite is not None:
                self._native_composite.update()
            else:
                self.update()

    def set_colors(self, up: str, down: str, background: str) -> None:
        self.up = self._volume_color(up)
        self.down = self._volume_color(down)
        self.background = background
        self.history_batch.cache_key = None
        self.live_batch.cache_key = None
        if self._native_composite is not None:
            self._native_composite.update()
        else:
            self.update()

    def set_style(self, style_name: str) -> None:
        self.style_name = style_name if style_name in CANDLE_STYLES else "Inked"
        self._style_key = tuple(CANDLE_STYLES[self.style_name].items())
        self.history_batch.cache_key = None
        self.live_batch.cache_key = None
        if self._native_composite is not None:
            self._native_composite.update()
        else:
            self.update()

    def set_gpu_enabled(self, enabled: bool) -> None:
        self.history_batch.set_gpu_enabled(enabled)
        self.live_batch.set_gpu_enabled(enabled)

    @staticmethod
    def _x_bounds(batch: PixelBarBatch) -> QtCore.QRectF:
        data = batch.data
        if not data.size:
            return QtCore.QRectF()
        left, _low, width, _height = batch.prepared.bounds
        return QtCore.QRectF(left, -VolumeOverlayItem._BOUND_Y, width, VolumeOverlayItem._BOUND_Y * 2.0)

    def _update_bounds(self) -> None:
        candidates = [rect for rect in (self._history_bounds, self._live_bounds) if not rect.isEmpty()]
        if not candidates:
            updated = QtCore.QRectF()
        else:
            left = min(rect.left() for rect in candidates)
            right = max(rect.right() for rect in candidates)
            updated = QtCore.QRectF(
                left,
                -self._BOUND_Y,
                max(1e-12, right - left),
                self._BOUND_Y * 2.0,
            )
        if updated != self._bounds:
            self.prepareGeometryChange()
            self._bounds = updated

    def set_history_data(self, candles, *, interval_seconds: int, slots=None) -> None:
        previous_bounds = QtCore.QRectF(self._history_bounds)
        self.history_interval_seconds = max(1, int(interval_seconds))
        self.history_batch.set_data(
            candles,
            slots if slots is not None else self.history_interval_seconds,
            volume=True,
        )
        self._history_bounds = self._x_bounds(self.history_batch)
        self._visible_scale_key = None
        self._update_bounds()
        if self._native_composite is not None:
            if previous_bounds.isEmpty():
                dirty_bounds = QtCore.QRectF(self._history_bounds)
            elif self._history_bounds.isEmpty():
                dirty_bounds = previous_bounds
            else:
                dirty_bounds = previous_bounds.united(self._history_bounds)
            self._native_composite.refresh_bounds(dirty_rect=dirty_bounds)
        else:
            self.update()

    def set_live_data(self, candles, *, interval_seconds: int, slots=None) -> None:
        previous_bounds = QtCore.QRectF(self._live_bounds)
        self.live_interval_seconds = max(1, int(interval_seconds))
        self.live_batch.set_data(
            candles,
            slots if slots is not None else self.live_interval_seconds,
            volume=True,
        )
        self._live_bounds = self._x_bounds(self.live_batch)
        self._visible_scale_key = None
        self._update_bounds()
        if self._native_composite is not None:
            if previous_bounds.isEmpty():
                dirty_bounds = QtCore.QRectF(self._live_bounds)
            elif self._live_bounds.isEmpty():
                dirty_bounds = previous_bounds
            else:
                dirty_bounds = previous_bounds.united(self._live_bounds)
            self._native_composite.refresh_bounds(dirty_rect=dirty_bounds)
        else:
            self.update()

    def clear(self) -> None:
        self.set_history_data([], interval_seconds=self.history_interval_seconds)
        self.set_live_data([], interval_seconds=self.live_interval_seconds)
        self.painted_once = False

    @staticmethod
    def _batch_visible_max(batch: PixelBarBatch, x0: float, x1: float) -> float:
        data = batch.data
        if not data.size:
            return 0.0
        xs = data[:, 0]
        low, high = sorted((float(x0), float(x1)))
        left = max(0, int(np.searchsorted(xs, low, side="left")) - 1)
        right = min(len(xs), int(np.searchsorted(xs, high, side="right")) + 1)



        if left < right and data[left, 0] + data[left, 6] * .5 < low:
            left += 1
        if left < right and data[right - 1, 0] - data[right - 1, 6] * .5 > high:
            right -= 1
        if right <= left:
            return 0.0



        _low, maximum = batch.prepared.extrema.query(left, right)
        return max(0.0, maximum)

    def _visible_max(self, x0: float, x1: float) -> float:
        key = (
            float(x0),
            float(x1),
            self.history_batch._data_revision,
            self.live_batch._data_revision,
        )
        if key == self._visible_scale_key:
            return self._visible_scale_max
        maximum = max(
            self._batch_visible_max(self.history_batch, x0, x1),
            self._batch_visible_max(self.live_batch, x0, x1),
        )
        self._visible_scale_key = key
        self._visible_scale_max = maximum
        return maximum

    def _paint_context(
        self, painter: QtGui.QPainter
    ) -> tuple[dict[str, Any], list[PixelBarBatch], float, QtCore.QRectF] | None:
        view_box = self.getViewBox()
        if view_box is None:
            return None
        x_range, y_range = view_box.viewRange()
        if len(x_range) < 2 or len(y_range) < 2:
            return None
        maximum = self._visible_max(float(x_range[0]), float(x_range[1]))
        if maximum <= 0.0:
            return None

        transform = painter.deviceTransform()
        view_data_rect = QtCore.QRectF(
            float(x_range[0]),
            float(y_range[0]),
            float(x_range[1] - x_range[0]),
            float(y_range[1] - y_range[0]),
        ).normalized()
        overlay_screen = transform.mapRect(view_data_rect).normalized()
        if overlay_screen.isEmpty():
            return None

        batches = [
            batch
            for batch in (self.history_batch, self.live_batch)
            if batch.data.size
        ]
        if not batches:
            return None
        return CANDLE_STYLES[self.style_name], batches, maximum, overlay_screen

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionGraphicsItem,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        del widget
        if self._native_composite is not None:
            return
        context = self._paint_context(painter)
        if context is None:
            return
        style, batches, maximum, overlay_screen = context
        shared_native = bool(
            len(batches) > 1
            and all(batch.gpu_enabled for batch in batches)
            and PixelBarBatch._is_opengl_painter(painter)
        )
        native_results: list[bool] | None = None
        if shared_native:
            try:
                painter.beginNativePainting()
                native_results = [
                    bool(
                        batch.paint(
                            painter,
                            style,
                            self.background,
                            self.up,
                            self.down,
                            volume=True,
                            exposed_rect=option.exposedRect,
                            style_key=self._style_key,
                            volume_overlay_max=maximum,
                            volume_overlay_fraction=self._height_fraction,
                            volume_overlay_screen=overlay_screen,
                            native_painting_active=True,
                        )
                    )
                    for batch in batches
                ]
                painter.endNativePainting()
            except (RuntimeError, TypeError, ValueError, AttributeError):
                try:
                    painter.endNativePainting()
                except (RuntimeError, TypeError):
                    pass
                native_results = None

        for index, batch in enumerate(batches):
            if native_results is not None and native_results[index]:
                continue
            batch.paint(
                painter,
                style,
                self.background,
                self.up,
                self.down,
                volume=True,
                exposed_rect=option.exposedRect,
                style_key=self._style_key,
                volume_overlay_max=maximum,
                volume_overlay_fraction=self._height_fraction,
                volume_overlay_screen=overlay_screen,
            )
        self.painted_once = bool(batches)

    def boundingRect(self) -> QtCore.QRectF:
        return self._bounds


class NativeBarCompositeItem(pg.GraphicsObject):
    """One native GL transaction for volume + historical/live candle batches.

    Source items stay in the scene as data/bounds owners but delegate painting to
    this item. Their VBOs and revisions remain independent; only the Qt/native
    painting boundary is shared.
    """

    _BOUND_Y = VolumeOverlayItem._BOUND_Y

    def __init__(
        self,
        history_candles: CandlestickItem,
        live_candle: CandlestickItem,
        volume_overlay: VolumeOverlayItem,
    ) -> None:
        super().__init__()

        self.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption,
            True,
        )
        self.history_candles = history_candles
        self.live_candle = live_candle
        self.volume_overlay = volume_overlay
        self._bounds = QtCore.QRectF()
        for item in (history_candles, live_candle, volume_overlay):
            item.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemHasNoContents, True)
        history_candles.set_native_composite(self)
        live_candle.set_native_composite(self)
        volume_overlay.set_native_composite(self)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.refresh_bounds()

    def refresh_bounds(self, dirty_rect: QtCore.QRectF | None = None) -> None:
        rects = [
            rect
            for rect in (
                self.history_candles.bounds,
                self.live_candle.bounds,
                self.volume_overlay.boundingRect(),
            )
            if not rect.isEmpty()
        ]
        if not rects:
            updated = QtCore.QRectF()
        else:
            left = min(rect.left() for rect in rects)
            right = max(rect.right() for rect in rects)
            updated = QtCore.QRectF(
                left,
                -self._BOUND_Y,
                max(1e-12, right - left),
                self._BOUND_Y * 2.0,
            )
        if updated != self._bounds:
            self.prepareGeometryChange()
            self._bounds = updated




        if dirty_rect is not None and not dirty_rect.isEmpty():
            view = self.getViewBox()
            if view is not None:
                x0, x1 = view.viewRange()[0]
                margin = (x1 - x0) * 16 / max(1.0, view.width())
                if dirty_rect.right() < x0 - margin or dirty_rect.left() > x1 + margin:




                    return
            dirty = QtCore.QRectF(
                float(dirty_rect.left()),
                -self._BOUND_Y,
                max(1e-12, float(dirty_rect.width())),
                self._BOUND_Y * 2.0,
            )
            self.update(dirty)
        else:
            self.update()

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionGraphicsItem,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        del widget
        profile_started = time.perf_counter() if performance_profile_active() else 0.0
        if profile_started:
            record_performance_count("gl.composite_paints")
        exposed = QtCore.QRectF(option.exposedRect).normalized()

        def x_exposed(rect: QtCore.QRectF) -> bool:
            if rect.isEmpty() or exposed.isEmpty():
                return exposed.isEmpty() and not rect.isEmpty()


            return float(rect.right()) > float(exposed.left()) and float(rect.left()) < float(exposed.right())

        candle_items = [
            item
            for item in (self.history_candles, self.live_candle)
            if item.pixel_batch.data.size and x_exposed(item.bounds)
        ]
        volume_context = self.volume_overlay._paint_context(painter)
        volume_batches: list[PixelBarBatch] = []
        if volume_context is not None:
            _style, _all_volume_batches, _maximum, _screen = volume_context
            for batch, bounds in (
                (self.volume_overlay.history_batch, self.volume_overlay._history_bounds),
                (self.volume_overlay.live_batch, self.volume_overlay._live_bounds),
            ):
                if batch.data.size and x_exposed(bounds):
                    volume_batches.append(batch)

        candidates: list[tuple[str, object, PixelBarBatch]] = []
        if volume_context is not None:
            candidates.extend(("volume", self.volume_overlay, batch) for batch in volume_batches)
        candidates.extend(("candle", item, item.pixel_batch) for item in candle_items)
        if not candidates:
            if profile_started:
                record_performance_timing(
                    "gl.composite_paint_ms",
                    (time.perf_counter() - profile_started) * 1000.0,
                )
            return

        shared_native = bool(
            all(batch.gpu_enabled for _kind, _owner, batch in candidates)
            and PixelBarBatch._is_opengl_painter(painter)
        )
        native_results: list[bool] | None = None
        if shared_native:
            native_results = []
            native_started = time.perf_counter() if profile_started else 0.0
            if profile_started:
                record_performance_count("gl.native_blocks")
            try:
                painter.beginNativePainting()
                if volume_context is not None:
                    style, _all_volume_batches, maximum, overlay_screen = volume_context
                    for batch in volume_batches:
                        native_results.append(
                            bool(
                                batch.paint(
                                    painter,
                                    style,
                                    self.volume_overlay.background,
                                    self.volume_overlay.up,
                                    self.volume_overlay.down,
                                    volume=True,
                                    exposed_rect=option.exposedRect,
                                    style_key=self.volume_overlay._style_key,
                                    volume_overlay_max=maximum,
                                    volume_overlay_fraction=self.volume_overlay._height_fraction,
                                    volume_overlay_screen=overlay_screen,
                                    native_painting_active=True,
                                )
                            )
                        )
                for item in candle_items:
                    native_results.append(
                        bool(
                            item.pixel_batch.paint(
                                painter,
                                CANDLE_STYLES[item.style_name],
                                item.background,
                                item.up,
                                item.down,
                                exposed_rect=option.exposedRect,
                                style_key=item._style_key,
                                native_painting_active=True,
                            )
                        )
                    )
                painter.endNativePainting()
                if native_started:
                    record_performance_timing(
                        "gl.native_block_ms",
                        (time.perf_counter() - native_started) * 1000.0,
                    )
            except (RuntimeError, TypeError, ValueError, AttributeError):
                try:
                    painter.endNativePainting()
                except (RuntimeError, TypeError):
                    pass
                if native_started:
                    record_performance_timing(
                        "gl.native_block_ms",
                        (time.perf_counter() - native_started) * 1000.0,
                    )
                    record_performance_count("gl.native_block_failures")
                native_results = None

        result_index = 0
        if volume_context is not None:
            style, _all_volume_batches, maximum, overlay_screen = volume_context
            for batch in volume_batches:
                ok = native_results is not None and native_results[result_index]
                result_index += 1
                if not ok:
                    if profile_started:
                        record_performance_count("gl.cpu_fallback_batches")
                    batch.paint(
                        painter,
                        style,
                        self.volume_overlay.background,
                        self.volume_overlay.up,
                        self.volume_overlay.down,
                        volume=True,
                        exposed_rect=option.exposedRect,
                        style_key=self.volume_overlay._style_key,
                        volume_overlay_max=maximum,
                        volume_overlay_fraction=self.volume_overlay._height_fraction,
                        volume_overlay_screen=overlay_screen,
                    )
            self.volume_overlay.painted_once = bool(volume_batches)

        for item in candle_items:
            ok = native_results is not None and native_results[result_index]
            result_index += 1
            if not ok:
                if profile_started:
                    record_performance_count("gl.cpu_fallback_batches")
                item.pixel_batch.paint(
                    painter,
                    CANDLE_STYLES[item.style_name],
                    item.background,
                    item.up,
                    item.down,
                    exposed_rect=option.exposedRect,
                    style_key=item._style_key,
                )
            item.painted_once = True

        if profile_started:
            record_performance_timing(
                "gl.composite_paint_ms",
                (time.perf_counter() - profile_started) * 1000.0,
            )

    def boundingRect(self) -> QtCore.QRectF:
        return self._bounds


from PySide6 import QtCore, QtGui, QtWidgets


from ..presentation import record_frame_request


class ChartGraphicsView(pg.GraphicsLayoutWidget):
    frame_needed = QtCore.Signal()
    frame_presented = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        self._paint_pending = False
        self._painting = False
        self._scene_dirty = False
        self._pending_pointer = None
        self._pointer_received_at = 0.0
        self._pointer_paint_sample_at = 0.0
        self.presentation_clock = None
        super().__init__(*args, **kwargs)


        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.NoViewportUpdate)
        self.scene().changed.connect(self._scene_changed)

    def _sync_viewport_opacity(self):
        viewport = self.viewport()
        if viewport is not None:
            brush = self.backgroundBrush()
            opaque = (brush.style() == QtCore.Qt.BrushStyle.SolidPattern
                      and brush.color().alpha() == 255)


            viewport.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, opaque)

    def setBackground(self, background):
        super().setBackground(background)
        self._sync_viewport_opacity()

    def setViewport(self, viewport):
        super().setViewport(viewport)
        self._sync_viewport_opacity()

    def mouseMoveEvent(self, event):




        record_performance_count("input.pointer_received")
        already_pending = self._pending_pointer is not None
        if already_pending:
            record_performance_count("input.pointer_coalesced")
        self._pending_pointer = QtGui.QMouseEvent(event)
        self._pointer_received_at = time.monotonic()
        event.accept()
        if not already_pending:
            record_frame_request(self.viewport(), self._pointer_received_at)
            self.frame_needed.emit()

    def flush_pointer_motion(self):
        event, self._pending_pointer = self._pending_pointer, None
        if event is None:
            return
        started = time.perf_counter() if performance_profile_active() else 0.0
        if started:
            record_performance_timing("input.pointer_queue_ms", (time.monotonic() - self._pointer_received_at) * 1000)
        self._pointer_paint_sample_at = self._pointer_received_at
        super().mouseMoveEvent(event)
        record_performance_count("input.pointer_dispatched")
        if started:
            record_performance_timing("input.scene_dispatch_ms", (time.perf_counter() - started) * 1000)

    def mousePressEvent(self, event):
        self.flush_pointer_motion()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):


        self.flush_pointer_motion()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.flush_pointer_motion()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._pending_pointer = None
        self._paint_pending = False
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._scene_dirty = False
        self.request_redraw()

    def _scene_changed(self, _rects):
        self.request_redraw()

    def request_redraw(self):
        already_dirty = self._scene_dirty
        self._scene_dirty = True
        if not already_dirty and not self._paint_pending and not self._painting:
            self.frame_needed.emit()

    def present(self):
        clock = self.presentation_clock


        required = clock is not None and clock.presentation_required()
        if not required and not self._scene_dirty and not self._paint_pending:
            return False
        viewport = self.viewport()
        if self._painting or not self.isVisible() or not viewport.isVisible() or self.window().isMinimized():
            return False
        if clock is not None and clock.frame_pending():
            return False
        if clock is None and self._paint_pending:
            return False


        self._paint_pending = True
        record_frame_request(viewport)
        if clock is not None:
            clock.frame_submitted(viewport)
        viewport.update()
        return True

    def paintEvent(self, event):
        self._paint_pending = False
        self._painting = True
        clock = self.presentation_clock
        viewport = self.viewport()
        pointer_received_at, self._pointer_paint_sample_at = self._pointer_paint_sample_at, 0.0
        started = 0.0
        try:


            if clock is not None:
                clock.begin_frame(viewport, input_received_at=pointer_received_at)
            self._scene_dirty = False
            started = time.perf_counter() if performance_profile_active() else 0.0


            self.scene().prepareForPaint()
            prepared = time.perf_counter() if started else 0.0
            QtWidgets.QGraphicsView.paintEvent(self, event)
            if started:
                record_performance_timing("qt.scene_prepare_ms", (prepared - started) * 1000)
                record_performance_timing("qt.scene_draw_ms", (time.perf_counter() - prepared) * 1000)
        finally:
            if started:
                record_performance_timing("qt.chart_paint_ms", (time.perf_counter() - started) * 1000)
                if pointer_received_at:
                    record_performance_timing("input.pointer_to_paint_ms", (time.monotonic() - pointer_received_at) * 1000)
            self._painting = False
            if clock is not None:
                clock.end_frame(viewport)
        self.frame_presented.emit()
        if self._scene_dirty:
            self.frame_needed.emit()
