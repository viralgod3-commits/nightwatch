from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import numpy as np
from PySide6 import QtGui

from nightwatch.chart import gpu_diagnostics
from nightwatch.chart import rendering
from nightwatch.theme import CANDLE_STYLES


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback, *_args):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self):
        for callback in tuple(self.callbacks):
            callback()


class _FakeFormat:
    def majorVersion(self):
        return 3

    def minorVersion(self):
        return 3

    def renderableType(self):
        return "desktop"

    def profile(self):
        return SimpleNamespace(name="CoreProfile")


class _FakeFunctions:
    VALUES = {
        gpu_diagnostics._GL_VENDOR: b"Example GPU Vendor",
        gpu_diagnostics._GL_RENDERER: bytearray(b"Example Renderer"),
        gpu_diagnostics._GL_VERSION: "4.6 Example Driver",
    }

    def __init__(self):
        self.calls = []

    def glGetString(self, name):
        self.calls.append(name)
        return self.VALUES[name]


class _FakeContext:
    def __init__(self):
        self.aboutToBeDestroyed = _Signal()
        self._functions = _FakeFunctions()

    def isValid(self):
        return True

    def functions(self):
        return self._functions

    def format(self):
        return _FakeFormat()


class _FakeQOpenGLContext:
    current = None

    @classmethod
    def currentContext(cls):
        return cls.current


def _patch_fake_context(monkeypatch, context):
    _FakeQOpenGLContext.current = context
    monkeypatch.setattr(
        gpu_diagnostics,
        "QtGui",
        SimpleNamespace(
            QOpenGLContext=_FakeQOpenGLContext,
            QSurfaceFormat=SimpleNamespace(
                RenderableType=SimpleNamespace(OpenGLES="gles")
            ),
        ),
    )
    monkeypatch.setattr(
        rendering,
        "QtGui",
        SimpleNamespace(QOpenGLContext=_FakeQOpenGLContext),
    )


def test_context_driver_strings_are_cached_until_context_destruction():
    context = _FakeContext()
    cache = gpu_diagnostics.GLContextInfoCache(maximum_contexts=2)
    # query_context_gpu_info uses the live Qt context class; use a direct fake
    # query here to isolate cache lifetime semantics from the string contract.
    queried = []
    result = cache.info_for(
        context,
        lambda ctx: queried.append(ctx) or {
            "available": True,
            "vendor": "Example GPU",
        },
    )
    assert result["vendor"] == "Example GPU"
    assert cache.info_for(context, lambda _ctx: pytest.fail("cache miss"))["available"]
    assert len(queried) == 1

    context.aboutToBeDestroyed.emit()
    assert not context.aboutToBeDestroyed.callbacks
    refreshed = cache.info_for(
        context,
        lambda _ctx: {"available": True, "vendor": "Recreated context"},
    )
    assert refreshed["vendor"] == "Recreated context"
    assert len(context.aboutToBeDestroyed.callbacks) == 1


def test_context_info_cache_is_bounded_and_disconnects_evicted_contexts():
    cache = gpu_diagnostics.GLContextInfoCache(maximum_contexts=2)
    contexts = [_FakeContext() for _ in range(3)]
    for index, context in enumerate(contexts):
        cache.info_for(context, lambda _ctx, n=index: {"available": True, "index": n})

    assert len(cache._entries) == 2
    assert not contexts[0].aboutToBeDestroyed.callbacks
    assert len(contexts[1].aboutToBeDestroyed.callbacks) == 1
    assert len(contexts[2].aboutToBeDestroyed.callbacks) == 1


def test_context_gpu_query_decodes_strings_and_reports_realized_format(monkeypatch):
    context = _FakeContext()
    _patch_fake_context(monkeypatch, context)

    info = gpu_diagnostics.query_context_gpu_info(context)

    assert info["available"] is True
    assert info["vendor"] == "Example GPU Vendor"
    assert info["renderer"] == "Example Renderer"
    assert info["version"] == "4.6 Example Driver"
    assert info["context_format"] == "OpenGL 3.3 CoreProfile"
    assert context._functions.calls == [
        gpu_diagnostics._GL_VENDOR,
        gpu_diagnostics._GL_RENDERER,
        gpu_diagnostics._GL_VERSION,
    ]


def test_path_state_distinguishes_native_recovery_and_selected_software():
    state = gpu_diagnostics.GPUPathDiagnostics()
    state.reset_current(True)
    state.cpu_paint(True, offline_target=True)
    assert state.snapshot("history", True)["observed_path"] == "unverified"
    assert state.native_failures == 0

    state.native_failure("resource creation failed", {"renderer": "Example GPU"})
    state.cpu_paint(True)
    fallback = state.snapshot("history", True)
    assert fallback["observed_path"] == "fallback"
    assert fallback["native_verified"] is False
    assert fallback["fallback_frames"] == 1
    assert fallback["fallback_reasons"] == {"resource creation failed": 1}

    state.native_success({"renderer": "Recovered GPU"})
    recovered = state.snapshot("history", True)
    assert recovered["observed_path"] == "native"
    assert recovered["native_verified"] is True
    assert recovered["native_failures"] == 1
    assert recovered["last_context"]["renderer"] == "Recovered GPU"

    for index in range(20):
        state.native_failure(f"failure-{index}")
    assert len(state.snapshot("history", True)["fallback_reasons"]) <= 8

    state.reset_current(False)
    software = state.snapshot("history", False)
    assert software["observed_path"] == "software"
    assert software["native_verified"] is False
    assert software["intentional_cpu_paints"] == 0


def test_resource_failure_is_counted_once_and_context_recreation_recovers(monkeypatch):
    gpu_diagnostics.clear_context_gpu_info_cache()
    first_context = _FakeContext()
    _patch_fake_context(monkeypatch, first_context)
    monkeypatch.setattr(rendering, "QtOpenGL", object())
    monkeypatch.setattr(rendering._NativeBarGL, "create_resources", lambda _ctx: None)
    monkeypatch.setattr(rendering._NativeBarGL, "draw", lambda *_args, **_kwargs: True)

    batch = rendering.PixelBarBatch()
    batch.profile_name = "history_candles"
    batch.set_gpu_enabled(True)
    monkeypatch.setattr(batch, "_is_opengl_painter", lambda _painter: True)
    paint_args = (
        None,
        None,
        None,
        (100, 80),
        {},
        "test-style",
        "#000000",
        "#00ff00",
        "#ff0000",
    )
    paint_kwargs = {"volume": False, "native_painting_active": True}

    assert not batch._paint_native_gl(*paint_args, **paint_kwargs)
    assert not batch._paint_native_gl(*paint_args, **paint_kwargs)
    failed = batch.gpu_diagnostic_state()
    assert failed["observed_path"] == "fallback"
    assert failed["native_failures"] == 1
    assert failed["last_native_failure"] == "OpenGL resource creation failed"
    assert failed["last_context"]["renderer"] == "Example Renderer"
    assert first_context._functions.calls == [
        gpu_diagnostics._GL_VENDOR,
        gpu_diagnostics._GL_RENDERER,
        gpu_diagnostics._GL_VERSION,
    ]

    first_context.aboutToBeDestroyed.emit()
    second_context = _FakeContext()
    _FakeQOpenGLContext.current = second_context
    monkeypatch.setattr(rendering._NativeBarGL, "create_resources", lambda ctx: {"context": ctx})
    assert batch._paint_native_gl(*paint_args, **paint_kwargs)
    recovered = batch.gpu_diagnostic_state()
    assert recovered["observed_path"] == "native"
    assert recovered["native_verified"] is True
    assert recovered["native_draws"] == 1
    assert recovered["native_failures"] == 1
    assert recovered["last_context"]["renderer"] == "Example Renderer"
    gpu_diagnostics.clear_context_gpu_info_cache()


def test_retiring_old_context_keeps_new_context_verification(monkeypatch):
    gpu_diagnostics.clear_context_gpu_info_cache()
    first = _FakeContext()
    _patch_fake_context(monkeypatch, first)
    monkeypatch.setattr(rendering, "QtOpenGL", object())
    destroyed = []

    def resources(context):
        result = {"context": context}
        for name in ("instance_vbo", "corner_vbo", "vao"):
            result[name] = SimpleNamespace(destroy=lambda key=name: destroyed.append(key))
        return result

    monkeypatch.setattr(rendering._NativeBarGL, "create_resources", resources)
    monkeypatch.setattr(rendering._NativeBarGL, "draw", lambda *_args, **_kwargs: True)
    batch = rendering.PixelBarBatch()
    batch.set_gpu_enabled(True)
    monkeypatch.setattr(batch, "_is_opengl_painter", lambda _painter: True)
    args = (None, None, None, (100, 80), {}, "style", "#000", "#0f0", "#f00")
    assert batch._paint_native_gl(*args, volume=False, native_painting_active=True)
    second = _FakeContext()
    _FakeQOpenGLContext.current = second
    assert batch._paint_native_gl(*args, volume=False, native_painting_active=True)
    first.aboutToBeDestroyed.emit()
    assert batch.gpu_diagnostic_state()["observed_path"] == "native"
    assert batch.gpu_diagnostic_state()["last_context"]["current"] is True
    second.aboutToBeDestroyed.emit()
    assert batch.gpu_diagnostic_state()["observed_path"] == "unverified"
    assert batch.gpu_diagnostic_state()["last_context"]["current"] is False
    assert destroyed == ["instance_vbo", "corner_vbo", "vao"]
    gpu_diagnostics.clear_context_gpu_info_cache()


def test_offscreen_image_paints_do_not_report_driver_failures(qapp, caplog):
    batch = rendering.PixelBarBatch()
    batch.set_data(np.array([[10, 10, 14, 8, 13, 2, 1]], dtype=np.float64), 1)
    batch.set_gpu_enabled(True)
    image = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor("#101010"))
    painter = QtGui.QPainter(image)
    with caplog.at_level(logging.WARNING, logger="nightwatch.chart.rendering"):
        batch.paint(painter, CANDLE_STYLES["Inked"], "#101010", "#00ff00", "#ff0000")
    painter.end()

    state = batch.gpu_diagnostic_state()
    assert state["observed_path"] == "unverified"
    assert state["native_failures"] == 0
    assert state["fallback_frames"] == 0
    assert state["cpu_paints"] == 1
    assert not caplog.records

    batch.set_gpu_enabled(False)
    painter = QtGui.QPainter(image)
    batch.paint(painter, CANDLE_STYLES["Inked"], "#101010", "#00ff00", "#ff0000")
    painter.end()
    software = batch.gpu_diagnostic_state()
    assert software["observed_path"] == "software"
    assert software["intentional_cpu_paints"] == 1
    assert software["native_failures"] == 0


def test_developer_summary_uses_persistent_gpu_counters_without_f3_timings():
    from nightwatch.ui.developer_tools import DeveloperDialog

    text = DeveloperDialog._chart_gpu_detail(
        {
            "requested_opengl": True,
            "requested_native": True,
            "actual_render_path": "fallback",
            "gpu_status": "CPU fallback",
            "actual_viewport": "QOpenGLWidget",
            "native_draws": 12,
            "native_failures": 2,
            "fallback_frames": 5,
            "cpu_paints": 5,
            "fallback_reasons": {"OpenGL resource creation failed": 2},
            "last_context": {
                "vendor": "Example Vendor",
                "renderer": "Example Renderer",
                "version": "4.6 Driver",
                "context_format": "OpenGL 3.3 CoreProfile",
            },
        },
        3.0,
    )

    assert "GL requested" in text
    assert "CPU fallback" in text
    assert "Example Renderer" in text
    assert "native 3/s (12 total)" in text
    assert "rebuild" not in text
    assert "GL submit" not in text
