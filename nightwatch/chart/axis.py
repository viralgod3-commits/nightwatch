"""Retain axis glyphs while the camera moves; Qt's GL engine caches the text."""
from __future__ import annotations

from collections import OrderedDict

from PySide6 import QtCore, QtGui


class _AxisMetrics:
    """Cache unchanged tick measurements without changing tick selection."""

    def __init__(self, painter, cache):
        self.painter = painter
        self.cache = cache

    def __getattr__(self, name):
        return getattr(self.painter, name)

    def boundingRect(self, rect, flags, text):
        key = (rect.x(), rect.y(), rect.width(), rect.height(), int(flags), text)
        bounds = self.cache.get(key)
        if bounds is None:
            bounds = self.painter.boundingRect(rect, flags, text)
            self.cache[key] = bounds
            if len(self.cache) > 512:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        # AxisItem adjusts the returned rectangle in place.
        return QtCore.QRectF(bounds)


class CachedAxisPainting:
    """AxisItem mixin retaining shaped text across range invalidations.

    The normal axis generator still chooses all ticks, labels and clipping.
    A small line-only QPicture follows its normal invalidation contract; text
    uses QStaticText so each pan does not record and replay text layout again.
    Font/DPI changes discard both bounded caches; pens remain live paint state.
    """

    def __init__(self, *args, **kwargs):
        self._axis_measurements = OrderedDict()
        self._axis_glyphs = OrderedDict()
        self._axis_text = ()
        self._axis_font = None
        self._axis_device = None
        self._axis_label_html = None
        recording = QtGui.QPicture()
        self._axis_recording_dpi = (recording.logicalDpiX(), recording.logicalDpiY())
        super().__init__(*args, **kwargs)

    def _updateLabel(self):
        label = self.labelString()
        if label != self._axis_label_html:
            self._axis_label_html = label
            super()._updateLabel()
        else:
            # AxisItem otherwise reparses identical HTML and invalidates Qt
            # layout on every camera change, even for an empty hidden label.
            self.picture = None
            self.update()

    def paint(self, painter, option, widget=None):
        font = self.style['tickFont'] or painter.font()
        device = painter.device()
        device_key = (device.logicalDpiX(), device.logicalDpiY(), device.devicePixelRatioF())
        if font != self._axis_font or device_key != self._axis_device:
            self._axis_font = QtGui.QFont(font)
            self._axis_device = device_key
            self._axis_measurements.clear()
            self._axis_glyphs.clear()
            self.picture = None
        painter.setFont(font)
        if device_key[:2] != self._axis_recording_dpi:
            # QPicture applies its own logical-DPI conversion on exports and
            # unusual paint devices. Preserve that path; normal per-monitor
            # scaling uses DPR and can retain native glyphs at 1x/1.5x/2x.
            return super().paint(painter, option, widget)
        if self.picture is None:
            specs = self.generateDrawSpecs(_AxisMetrics(painter, self._axis_measurements))
            picture = QtGui.QPicture()
            lines = QtGui.QPainter(picture)
            self._axis_text = ()
            try:
                lines.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
                if specs is not None:
                    axis, ticks, labels = specs
                    for pen, start, end in (axis, *ticks):
                        lines.setPen(pen)
                        lines.drawLine(start, end)
                    self._axis_text = self._prepare_axis_text(labels, font, device)
            finally:
                lines.end()
            self.picture = picture
        self.picture.play(painter)
        painter.setFont(font)
        painter.setPen(self.textPen())
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        painter.setClipRect(self.boundingRect().toAlignedRect())
        for position, text in self._axis_text:
            painter.drawStaticText(position, text)

    def _prepare_axis_text(self, labels, font, device):
        prepared = []
        height = QtGui.QFontMetricsF(font, device).height()
        align = QtCore.Qt.AlignmentFlag
        for rect, flags, value in labels:
            text = self._axis_glyphs.get(value)
            if text is None:
                text = QtGui.QStaticText(value)
                text.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                text.setPerformanceHint(QtGui.QStaticText.PerformanceHint.AggressiveCaching)
                text.prepare(QtGui.QTransform(), font)
                self._axis_glyphs[value] = text
                if len(self._axis_glyphs) > 512:
                    self._axis_glyphs.popitem(last=False)
            else:
                self._axis_glyphs.move_to_end(value)
            x, y = rect.x(), rect.y()
            if flags & align.AlignHCenter:
                x += (rect.width() - text.size().width()) * .5
            elif flags & align.AlignRight:
                x += rect.width() - text.size().width()
            if flags & align.AlignVCenter:
                y += (rect.height() - height) * .5
            elif flags & align.AlignBottom:
                y += rect.height() - height
            prepared.append((QtCore.QPointF(x, y), text))
        return tuple(prepared)
