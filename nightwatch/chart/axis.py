"""Retain axis glyphs while the camera moves; Qt's GL engine caches the text."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy

from PySide6 import QtCore, QtGui


class CalendarTickCache:
    """Reuse a calendar tick set until either end crosses a step boundary.

    Keep the upstream calendar/DST generator authoritative. Its raw step
    boundaries tell us when the same result is still valid; a wider time tile
    would change the phase of skipped month ticks and mishandle DST edges.
    """

    def __init__(self):
        self.key = None
        self.windows = ()
        self.values = ()

    def tick_values(self, level, minimum, maximum, spacing):
        key = (level, level.utcOffset, spacing)
        windows = []
        hit = key == self.key
        for index, spec in enumerate(level.tickSpecs):
            skip = spec.skipFactor(spacing)
            lower, upper = level.extendTimeRangeForSpacing(spec.spacing, minimum, maximum)
            first = spec.step(lower, skip, first=True)
            signature = (spec.spacing, spec.step, spec.format, skip, first)
            old = self.windows[index] if hit and index < len(self.windows) else None
            if old is None or signature != old[0] or not old[1] <= upper < old[2]:
                hit = False
            windows.append((signature, upper))
            if skip > 1:
                break
        if hit and len(windows) == len(self.windows):
            return self.values
        values = level.tickValues(minimum, maximum, minSpc=spacing)
        limits = []
        for signature, upper in windows:
            _spacing, step, _format, skip, first = signature
            last, following = float('-inf'), first
            while following <= upper:
                last, following = following, step(following, skip, first=False)
            limits.append((signature, last, following))
        self.key, self.windows, self.values = key, limits, values
        return values


class PanningAxisLayout:
    """Translate an unchanged horizontal tick layout between boundary crossings.

    Tick selection, text density and edge visibility still come from AxisItem.
    Rebuild when any of those can change; ordinary camera motion only projects
    the retained specs. Nonstandard axes use the upstream generator unchanged.
    """

    def generateDrawSpecs(self, painter):
        if (self.orientation != 'bottom' or self.grid is not False or self.logMode
                or self._tickLevels is not None or any(self.style['stopAxisAtTick'])):
            self._pan_layout = None
            return super().generateDrawSpecs(painter)
        self.fontMetrics = painter.fontMetrics()
        bounds = self.mapRectFromParent(self.geometry())
        left = self.mapToDevice(bounds.topLeft() + QtCore.QPointF(-1, 1))
        right = self.mapToDevice(bounds.topRight() + QtCore.QPointF(1, 1))
        span = self.range[1] - self.range[0]
        if left is None or right is None or span <= 0:
            return super().generateDrawSpecs(painter)
        length = QtCore.QLineF(left, right).length()
        if not length:
            return super().generateDrawSpecs(painter)
        levels = self.tickValues(*self.range, length)
        key = (span, bounds, length, self.boundingRect(), self._axis_device,
               self._axis_font, self.scale, self.autoSIPrefixScale, self.utcOffset,
               tuple((spacing, tuple(ticks)) for spacing, ticks in levels))
        previous = getattr(self, '_pan_layout', None)
        scale = bounds.width() / span
        offset = self.range[0] * scale
        if previous is not None:
            old_key, style, pens, old_offset, limits, specs = previous
            shift = old_offset - offset
            if (key == old_key and self.style == style
                    and pens == (self.pen(), self.tickPen(), self.textPen())
                    and limits[0] + 1e-5 < shift < limits[1] - 1e-5):
                axis, ticks, labels = specs
                delta = QtCore.QPointF(shift, 0)
                return (axis, [(pen, start + delta, end + delta) for pen, start, end in ticks],
                        [(rect.translated(delta), flags, text) for rect, flags, text in labels])
        specs = super().generateDrawSpecs(painter)
        self._pan_layout = None
        if specs is None:
            return specs
        # Bound reuse by every tick and label, including labels currently
        # outside the clip. Otherwise a label entering at an edge would lag.
        clip = self.boundingRect()
        lower, upper = float('-inf'), float('inf')

        def constrain(start, stop, minimum, maximum):
            nonlocal lower, upper
            enter, leave = minimum - start, maximum - stop
            if enter <= 0 <= leave:
                lower, upper = max(lower, enter), min(upper, leave)
            elif enter > 0:
                upper = min(upper, enter)
            elif leave < 0:
                lower = max(lower, leave)

        minimum = self.range[0] * scale - offset
        maximum = self.range[1] * scale - offset
        for spacing, values in levels:
            strings = self.tickStrings(values, self.autoSIPrefixScale * self.scale, spacing)
            for value, text in zip(values, strings):
                x = value * scale - offset
                constrain(x, x, minimum, maximum)
                rect = painter.boundingRect(QtCore.QRectF(0, 0, 100, 100),
                                            QtCore.Qt.AlignmentFlag.AlignCenter, text)
                constrain(x - rect.width() / 2, x + rect.width() / 2,
                          clip.left(), clip.right())
        # _updateMaxTextSize may have changed the clipping rectangle.
        key = (*key[:3], clip, *key[4:])
        # AxisItem mutates tickTextOffset in place. Copy container values,
        # but retain Qt values (QFont does not support Python deepcopy).
        style = {name: deepcopy(value) if isinstance(value, (list, tuple)) else value
                 for name, value in self.style.items()}
        self._pan_layout = (key, style,
                            (self.pen(), self.tickPen(), self.textPen()), offset,
                            (lower, upper), specs)
        return specs


class _AxisFontMetrics:
    """DateAxis repeatedly measures the same calendar format examples."""

    def __init__(self, metrics):
        self.metrics = metrics
        self.bounds = {}

    def __getattr__(self, name):
        return getattr(self.metrics, name)

    def boundingRect(self, text):
        bounds = self.bounds.get(text)
        if bounds is None:
            bounds = self.metrics.boundingRect(text)
            if len(self.bounds) >= 32:
                self.bounds.clear()
            self.bounds[text] = bounds
        return QtCore.QRect(bounds)


class _AxisMetrics:
    """Cache unchanged tick measurements without changing tick selection."""

    def __init__(self, painter, cache, font_metrics):
        self.painter = painter
        self.cache = cache
        self.font_metrics = font_metrics

    def fontMetrics(self):
        return self.font_metrics

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
    Lines are retained as pen batches and text uses QStaticText. This avoids
    recording/replaying QPicture commands and shaping unchanged calendar text
    on each pan. Font/DPI changes invalidate all device-dependent caches.
    """

    def __init__(self, *args, **kwargs):
        self._axis_measurements = OrderedDict()
        self._axis_glyphs = OrderedDict()
        self._axis_text = ()
        self._axis_lines = ()
        self._axis_font = None
        self._axis_font_metrics = None
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
            self._axis_font_metrics = _AxisFontMetrics(QtGui.QFontMetrics(font, device))
            self.picture = None
        painter.setFont(font)
        if device_key[:2] != self._axis_recording_dpi:
            # QPicture applies its own logical-DPI conversion on exports and
            # unusual paint devices. Preserve that path; normal per-monitor
            # scaling uses DPR and can retain native glyphs at 1x/1.5x/2x.
            return super().paint(painter, option, widget)
        if self.picture is None:
            specs = self.generateDrawSpecs(_AxisMetrics(
                painter, self._axis_measurements, self._axis_font_metrics))
            batches = []
            self._axis_text = ()
            if specs is not None:
                axis, ticks, labels = specs
                for pen, start, end in (axis, *ticks):
                    if not batches or pen != batches[-1][0]:
                        batches.append((pen, []))
                    batches[-1][1].append(QtCore.QLineF(start, end))
                self._axis_text = self._prepare_axis_text(labels, font, device)
            self._axis_lines = batches
            # AxisItem owns invalidation through this existing sentinel. The
            # QPicture fallback above still handles unusual logical-DPI exports.
            self.picture = QtGui.QPicture()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        for pen, lines in self._axis_lines:
            painter.setPen(pen)
            painter.drawLines(lines)
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
