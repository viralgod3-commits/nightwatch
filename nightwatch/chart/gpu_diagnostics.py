"""Small, persistent diagnostics for the chart's native OpenGL batches.

Frame-profile timings remain in :mod:`nightwatch.presentation`; this module
tracks only which backend painted and why a requested native path fell back.
Context strings are copied while the context is current and cached for its
lifetime, so normal diagnostics refreshes never call into the driver.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from PySide6 import QtCore, QtGui


_GL_VENDOR = 0x1F00
_GL_RENDERER = 0x1F01
_GL_VERSION = 0x1F02
_MAX_CONTEXTS = 16
_MAX_FAILURE_REASONS = 8
_MAX_REASON_LENGTH = 96
_MAX_CONTEXT_STRING_LENGTH = 160


def _context_key(context: Any) -> int:
    """Use the native QObject address when PySide exposes it, with a safe fallback."""
    try:
        import shiboken6

        pointer = shiboken6.getCppPointer(context)
        if pointer:
            return int(pointer[0])
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return id(context)


def gl_context_key(context: Any) -> int:
    """Stable cache key for a QOpenGLContext, including across PySide wrappers."""
    return _context_key(context)


def same_gl_context(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if left is None or right is None:
        return False
    try:
        return _context_key(left) == _context_key(right)
    except (RuntimeError, TypeError, ValueError):
        return False


def _decode_gl_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        decoded = value
    else:
        try:
            raw = memoryview(value)[:_MAX_CONTEXT_STRING_LENGTH].tobytes()
            decoded = raw.decode("utf-8", errors="replace")
        except (TypeError, ValueError, BufferError):
            data = getattr(value, "data", None)
            if callable(data):
                try:
                    raw = data()
                    if isinstance(raw, (bytes, bytearray, memoryview)):
                        decoded = bytes(raw[:_MAX_CONTEXT_STRING_LENGTH]).decode("utf-8", errors="replace")
                    else:
                        decoded = str(raw)
                except (RuntimeError, TypeError, ValueError):
                    decoded = str(value)
            else:
                decoded = str(value)
    return decoded.replace("\x00", "").strip()[:_MAX_CONTEXT_STRING_LENGTH]


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    return str(value)


def query_context_gpu_info(context: Any) -> dict[str, Any]:
    """Read driver identity and the realized context format once, while current."""
    if context is None:
        return {"available": False, "error": "no_context"}
    try:
        if not context.isValid():
            return {"available": False, "error": "invalid_context"}
        current_context = QtGui.QOpenGLContext.currentContext()
        if current_context is None or _context_key(current_context) != _context_key(context):
            return {"available": False, "error": "context_not_current"}
    except (AttributeError, RuntimeError, TypeError):
        return {"available": False, "error": "context_unavailable"}

    info: dict[str, Any] = {"available": False}
    try:
        functions = context.functions()
        get_string = getattr(functions, "glGetString", None)
        if not callable(get_string):
            info["error"] = "glGetString_unavailable"
        else:
            info["vendor"] = _decode_gl_string(get_string(_GL_VENDOR))
            info["renderer"] = _decode_gl_string(get_string(_GL_RENDERER))
            info["version"] = _decode_gl_string(get_string(_GL_VERSION))
            info["available"] = bool(info["vendor"] or info["renderer"] or info["version"])
            if not info["available"]:
                info["error"] = "driver_strings_unavailable"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        info["error"] = "driver_query_failed"

    try:
        surface_format = context.format()
        major = int(surface_format.majorVersion())
        minor = int(surface_format.minorVersion())
        renderable = surface_format.renderableType()
        open_gles = getattr(QtGui.QSurfaceFormat.RenderableType, "OpenGLES", object())
        api = "OpenGL ES" if renderable == open_gles else "OpenGL"
        profile = _enum_name(surface_format.profile())
        info.update(
            {
                "api": api,
                "context_format": f"{api} {major}.{minor} {profile}".strip(),
                "major": major,
                "minor": minor,
                "profile": profile,
            }
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        info.setdefault("context_format", "")
    return info


class GLContextInfoCache:
    """Bounded context-lifetime cache; stored values contain no Qt objects."""

    def __init__(self, maximum_contexts: int = _MAX_CONTEXTS):
        self._maximum_contexts = max(1, int(maximum_contexts))
        self._entries: OrderedDict[int, tuple[Any, dict[str, Any]]] = OrderedDict()
        self._cleanup_callbacks: dict[int, tuple[Any, Callable[[], None]]] = {}

    @staticmethod
    def _same_context(left: Any, right: Any) -> bool:
        if left is right:
            return True
        try:
            return _context_key(left) == _context_key(right)
        except (RuntimeError, TypeError, ValueError):
            return False

    def _disconnect_cleanup(self, key: int) -> None:
        saved = self._cleanup_callbacks.pop(key, None)
        if saved is None:
            return
        context, callback = saved
        try:
            context.aboutToBeDestroyed.disconnect(callback)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def _remove(self, key: int, context: Any | None = None, *, disconnect: bool = True) -> None:
        entry = self._entries.get(key)
        if entry is not None and (context is None or self._same_context(entry[0], context)):
            self._entries.pop(key, None)
        if disconnect:
            self._disconnect_cleanup(key)
        else:
            self._cleanup_callbacks.pop(key, None)

    def _register_cleanup(self, key: int, context: Any) -> None:
        signal = getattr(context, "aboutToBeDestroyed", None)
        if signal is None or not callable(getattr(signal, "connect", None)):
            return

        def cleanup() -> None:
            self._remove(key, context)

        try:
            direct = QtCore.Qt.ConnectionType.DirectConnection
            signal.connect(cleanup, direct)
        except (AttributeError, RuntimeError, TypeError):
            try:
                signal.connect(cleanup)
            except (AttributeError, RuntimeError, TypeError):
                return
        self._cleanup_callbacks[key] = (context, cleanup)

    def info_for(
        self,
        context: Any,
        query: Callable[[Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if context is None:
            return {"available": False, "error": "no_context"}
        key = _context_key(context)
        entry = self._entries.get(key)
        if entry is not None and self._same_context(entry[0], context):
            self._entries.move_to_end(key)
            return dict(entry[1])
        if entry is not None:
            self._remove(key)

        try:
            info = dict((query or query_context_gpu_info)(context))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            info = {"available": False, "error": "driver_query_failed"}
        # Do not cache transient observations made before/after context lifetime.
        if info.get("error") not in {"context_not_current", "invalid_context", "context_unavailable"}:
            self._entries[key] = (context, info)
            self._entries.move_to_end(key)
            self._register_cleanup(key, context)
            while len(self._entries) > self._maximum_contexts:
                oldest_key, _ = self._entries.popitem(last=False)
                self._disconnect_cleanup(oldest_key)
        return dict(info)

    def clear(self) -> None:
        for key in tuple(self._entries):
            self._remove(key)
        for key in tuple(self._cleanup_callbacks):
            self._disconnect_cleanup(key)


_GL_CONTEXT_INFO_CACHE = GLContextInfoCache()


def context_gpu_info(context: Any) -> dict[str, Any]:
    """Return a copied, cached GPU identity snapshot for a current context."""
    return _GL_CONTEXT_INFO_CACHE.info_for(context)


def clear_context_gpu_info_cache() -> None:
    """Clear cached Qt context references; primarily useful at shutdown/tests."""
    _GL_CONTEXT_INFO_CACHE.clear()


class GPUPathDiagnostics:
    """Bounded cumulative counters plus the latest meaningful paint result."""

    def __init__(self) -> None:
        self.native_draws = 0
        self.native_failures = 0
        self.fallback_frames = 0
        self.cpu_paints = 0
        self.intentional_cpu_paints = 0
        self.fallback_reasons: dict[str, int] = {}
        self.last_native_failure = ""
        self.last_path = "unverified"
        self.native_verified = False
        self.last_context: dict[str, Any] = {}
        self._current_failure_reason = ""

    def reset_current(self, native_requested: bool) -> None:
        self._current_failure_reason = ""
        self.last_path = "unverified" if native_requested else "software"
        self.native_verified = False

    def native_success(self, context_info: dict[str, Any]) -> None:
        self.native_draws += 1
        self.native_verified = True
        self.last_path = "native"
        self._current_failure_reason = ""
        self.last_context = dict(context_info)
        self.last_context["current"] = True

    def native_failure(self, reason: str, context_info: dict[str, Any] | None = None) -> None:
        normalized = str(reason or "unknown native failure")[:_MAX_REASON_LENGTH]
        self.native_failures += 1
        self.last_native_failure = normalized
        self._current_failure_reason = normalized
        self.last_path = "fallback"
        self.native_verified = False
        if normalized in self.fallback_reasons:
            self.fallback_reasons[normalized] += 1
        elif normalized != "other" and len(self.fallback_reasons) < _MAX_FAILURE_REASONS - 1:
            self.fallback_reasons[normalized] = 1
        else:
            self.fallback_reasons["other"] = self.fallback_reasons.get("other", 0) + 1
        if context_info:
            self.last_context = dict(context_info)
            self.last_context["current"] = True

    def repeat_failure(self, reason: str, context_info: dict[str, Any] | None = None) -> None:
        """Mark a known-disabled context as falling back without recounting its cause."""
        normalized = str(reason or self.last_native_failure or "native path disabled")[:_MAX_REASON_LENGTH]
        self._current_failure_reason = normalized
        self.last_path = "fallback"
        self.native_verified = False
        if context_info:
            self.last_context = dict(context_info)
            self.last_context["current"] = True

    def begin_native_attempt(self) -> None:
        self._current_failure_reason = ""

    def context_destroyed(self, native_requested: bool) -> None:
        if self.last_context:
            self.last_context["current"] = False
        if native_requested:
            self.last_path = "unverified"
            self.native_verified = False
        else:
            self.last_path = "software"
            self.native_verified = False

    def cpu_paint(self, native_requested: bool, *, offline_target: bool = False) -> None:
        self.cpu_paints += 1
        if not native_requested:
            self.intentional_cpu_paints += 1
            self.last_path = "software"
        elif offline_target:
            # QImage/QPixmap paints used by export and offscreen tests do not
            # exercise the chart viewport and therefore cannot verify or fail GL.
            if self.last_path == "unverified":
                self.last_path = "unverified"
        else:
            self.fallback_frames += 1
            self.last_path = "fallback"
            self.native_verified = False

    def note_offline_target(self) -> None:
        self._current_failure_reason = ""

    @property
    def current_failure_reason(self) -> str:
        return self._current_failure_reason

    def snapshot(self, profile: str, native_requested: bool) -> dict[str, Any]:
        return {
            "profile": str(profile),
            "requested_native": bool(native_requested),
            "observed_path": self.last_path if native_requested else "software",
            "native_verified": bool(self.native_verified and native_requested),
            "native_draws": int(self.native_draws),
            "native_failures": int(self.native_failures),
            "fallback_frames": int(self.fallback_frames),
            "cpu_paints": int(self.cpu_paints),
            "intentional_cpu_paints": int(self.intentional_cpu_paints),
            "fallback_reasons": dict(self.fallback_reasons),
            "last_native_failure": self.last_native_failure,
            "last_context": dict(self.last_context),
        }
