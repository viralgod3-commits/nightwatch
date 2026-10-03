"""Render the real terminal and native text surfaces at desktop DPI settings.

Run this file directly with --probe OUTPUT to retain full-resolution captures.
Windows probes use the Windows QPA/font backend; Linux probes use offscreen Qt.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets, QtTest

from nightwatch import utilities as typography

ROOT = Path(__file__).resolve().parents[1]


def settle(app):
    for _ in range(6):
        app.processEvents()
    QtTest.QTest.qWait(30)
    app.processEvents()


def pixels(image):
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()].copy()


@pytest.fixture(scope='module')
def bundled_fonts(qapp):
    typography.configure_typography(notify=False)
    typography.load_app_fonts(qapp, str(ROOT / 'nightwatch'))
    return qapp


@pytest.mark.parametrize('role', list(typography.TYPOGRAPHY_DEFAULTS))
def test_roles_resolve_to_the_bundled_static_outlines(bundled_fonts, role):
    profile = typography.TYPOGRAPHY_DEFAULTS[role]
    numeric = profile['family'] == 'numeric'
    weight = profile['weight']
    width = profile.get('numeric_width', 'normal')
    if numeric and width == 'extended':
        key = 'numeric_extended_medium' if weight == 500 else 'numeric_extended_semibold'
    else:
        key = ('numeric' if numeric else 'ui') + '_' + {
            400: 'regular', 500: 'medium', 600: 'semibold', 700: 'bold'}[weight]
    expected = QtGui.QRawFont(str(ROOT / 'fonts' / typography.TYPOGRAPHY_FONT_FILES[key]), 14.0)
    font = typography.typography_font(role)
    actual = QtGui.QRawFont.fromFont(font)
    assert actual.isValid() and expected.isValid()
    assert actual.fontTable('name') == expected.fontTable('name'), (role, actual.styleName())
    assert not (font.styleStrategy() & QtGui.QFont.StyleStrategy.NoAntialias)
    if numeric:
        metrics = QtGui.QFontMetricsF(font)
        widths = [metrics.horizontalAdvance(digit) for digit in '0123456789']
        assert max(widths) - min(widths) < .02


def test_extended_faces_are_wider_without_synthetic_stretch(bundled_fonts):
    controller = typography.TypographyController()
    controller.configure({typography.TextRole.TOP_MARKET_VALUE: {'numeric_width': 'normal'}}, notify=False)
    narrow = controller.font(typography.TextRole.TOP_MARKET_VALUE)
    wide = typography.typography_font(typography.TextRole.TOP_MARKET_VALUE)
    assert wide.stretch() == narrow.stretch() == 100
    assert QtGui.QFontMetricsF(wide).horizontalAdvance('0123456789') > (
        QtGui.QFontMetricsF(narrow).horizontalAdvance('0123456789') * 1.1)
    assert 'Extended' in QtGui.QRawFont.fromFont(wide).styleName()


@pytest.mark.parametrize('mode,expected', [
    ('auto', 'PassThrough'), ('invalid', 'PassThrough'),
    ('passthrough', 'PassThrough'), ('round', 'Round'),
    ('round_prefer_floor', 'RoundPreferFloor'), ('floor', 'Floor'), ('ceil', 'Ceil'),
])
def test_dpi_policy_preserves_native_scaling_and_explicit_overrides(mode, expected):
    assert typography.typography_dpi_rounding_policy(mode).name == expected


@pytest.mark.parametrize('dpr', [1.0, 1.25, 1.5, 2.0])
def test_dom_surface_uses_exact_dpr_and_canvas_font_metrics(bundled_fonts, dpr):
    from nightwatch.orderbook.orderbook_ui import _DomRasterProcess
    worker = _DomRasterProcess({'theme': {}, 'dpi': 96})
    try:
        worker.canvas.resize(361, 137)
        worker.canvas._raster_dpr = dpr
        # Own the inspection copy so no test reference leases shared memory
        # when the real raster worker shuts down.
        image = worker._surface(None)[1].copy()
        assert image.size() == QtCore.QSize(math.ceil(361 * dpr), math.ceil(137 * dpr))
        assert image.devicePixelRatioF() == dpr
        assert image.logicalDpiY() == worker.canvas.logicalDpiY()
        font = typography.typography_font(typography.TextRole.UI_LABEL)
        image_metrics = QtGui.QFontMetricsF(font, image)
        canvas_metrics = QtGui.QFontMetricsF(font, worker.canvas)
        assert image_metrics.horizontalAdvance('OPEN INTEREST') == canvas_metrics.horizontalAdvance('OPEN INTEREST')
        assert image_metrics.height() == canvas_metrics.height()
    finally:
        worker.close()


@pytest.mark.parametrize('scale', ['1', '1.25', '1.5', '2'])
def test_desktop_ui_at_1080p_and_1440p(scale, tmp_path):
    env = dict(os.environ, QT_SCALE_FACTOR=scale, QT_FONT_DPI='96',
               QT_SCALE_FACTOR_ROUNDING_POLICY='PassThrough',
               QT_QPA_PLATFORM='windows' if sys.platform == 'win32' else 'offscreen')
    env['QT_SCREEN_SCALE_FACTORS'] = '1'
    env['PYTHONPATH'] = str(ROOT)
    result = subprocess.run([sys.executable, str(Path(__file__)), '--probe', str(tmp_path)],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['dpr'] == float(scale)
    assert len(report['screens']) == 2
    assert report['dom_pixels_unchanged']


def probe(output):
    """Offline application run; no credentials, exchange calls or render worker."""
    from nightwatch.constants import ORG_NAME, APP_NAME
    from nightwatch.models import OrderFlowSnapshot, OrderFlowTradePrint
    from nightwatch.networking.binance import BinanceRest
    from nightwatch.orderbook import backend, orderbook_ui
    from nightwatch.trading.gateway import TradingGateway

    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        typography.typography_dpi_rounding_policy())
    app = QtWidgets.QApplication([])
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                            QtCore.QSettings.Scope.UserScope, tempfile.mkdtemp())
    QtCore.QStandardPaths.setTestModeEnabled(True)
    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    settings.clear()
    settings.setValue('testing/chart_opengl_v2', False)
    typography.load_app_fonts(app, str(ROOT / 'nightwatch'))
    BinanceRest._ensure_time_sync_loop = lambda self: None
    TradingGateway._restore_placement_journal = lambda self: None
    backend._OrderBookProcessLink.enable = lambda *args: None
    from nightwatch.app.main_window import MainWindow
    MainWindow._run_deferred_startup_stage = lambda self: None
    MainWindow._on_order_flow_runtime_snapshot = lambda *args: None
    window = MainWindow(testnet=True)
    window.start_fullscreen = window.start_maximized = False
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'screens': []}
    try:
        window.show()
        settle(app)
        dpr = window.devicePixelRatioF()
        report['dpr'] = dpr
        assert QtGui.QGuiApplication.highDpiScaleFactorRoundingPolicy() == QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        window.stats.update_ticker({'c': '65432.1', 'q': '24600000000', 'v': '123456.789', 'h': '66789.2', 'l': '64123.4'})
        window.stats.update_interest({'openInterest': '123456.7'}, 65432.1)
        window.stats.update_long_short([{'timestamp': 1700000000000,
            'longShortRatio': '1.1', 'longAccount': '.5238', 'shortAccount': '.4762'}])
        window.stats.update_mark({'p': '65432.1', 'r': '.0001', 'E': 1700000000000, 'T': 1700014400000})
        window.chart.price_plot.setYRange(64000, 67000, padding=0)
        window.chart.price_plot.setXRange(1700000000, 1700007200, padding=0)
        tape = window.large_trades
        tape.model.decimals = 1
        prints = [
            OrderFlowTradePrint(sequence=i, event_time_ms=1700000000000 + i * 1000,
                received_monotonic=i / 10, price=65432.1 + i / 10, quantity=.00125 * i,
                notional=81.79 * i, aggressor_side='BUY' if i % 2 else 'SELL',
                normal_notional=100, rpi_notional=0, relative_size=1, salience_class=1)
            for i in range(12, 0, -1)]
        tape.set_mode('ALL', emit=False)
        tape.set_panel_active(True)
        tape.set_order_flow_snapshot(OrderFlowSnapshot(
            symbol=tape.symbol, sequence=1, generated_monotonic=1,
            ready=True, live=True, bbo_source='depth', depth_age_seconds=0,
            bbo_age_seconds=0, trade_age_seconds=0, recent_prints=tuple(prints)))
        tape._refresh_table()
        window.order_panel.price_edit.setText('65432.1')
        for width, height in [(1920, 1080), (2560, 1440)]:
            logical = QtCore.QSize(round(width / dpr), round(height / dpr))
            window.resize(logical)
            settle(app)
            assert tape.model.rowCount() == len(prints)
            # At high desktop zoom the existing minimum width can require a
            # wider window; text must retain its font size and fit every card.
            assert window.height() == logical.height()
            for key in ('volume', 'oi', 'long_short', 'funding'):
                card = window.stats.cards[key]
                if not card.isVisible():
                    continue  # Existing responsive policy hides overflow cards.
                for label in (card.title, card.value):
                    assert '…' not in label.text(), (key, label.text())
                    metrics = QtGui.QFontMetricsF(label.font(), label)
                    assert metrics.horizontalAdvance(label.text()) <= label.contentsRect().width() + 1, (key, label.text(), label.size())
                    assert metrics.height() <= label.contentsRect().height() + 1, (key, label.text(), label.size(), metrics.height())
            for label in window.instrument_bar.findChildren(QtWidgets.QLabel):
                assert QtGui.QRawFont.fromFont(label.font()).familyName().startswith(('Inter', 'Iosevka'))
            pixmap = window.grab()
            assert pixmap.devicePixelRatioF() == dpr
            assert pixmap.width() >= width - 1 and pixmap.height() >= height - 1
            assert pixmap.save(str(output / f'terminal-{width}x{height}.png'))
            assert window.instrument_bar.grab().save(str(output / f'market-{width}.png'))
            report['screens'].append({'requested': [width, height],
                                      'rendered': [pixmap.width(), pixmap.height()]})

        # Check every market card independently of the shell's overflow policy.
        # Use the real card class, current strings, stylesheet and font roles.
        from nightwatch.ui.market_widgets import MetricCard
        strip = QtWidgets.QWidget()
        strip.setStyleSheet(window.styleSheet())
        row = QtWidgets.QHBoxLayout(strip)
        for key in ('volume', 'oi', 'long_short', 'funding'):
            original = window.stats.cards[key]
            card = MetricCard(original.title.text(), original.value.text(), compact=True)
            card.setFixedWidth(card.metric_width())
            row.addWidget(card)
        strip.show()
        settle(app)
        for card in strip.findChildren(MetricCard):
            card.setFixedWidth(card.metric_width())
        strip.adjustSize()
        settle(app)
        for card in strip.findChildren(MetricCard):
            for label in (card.title, card.value):
                metrics = QtGui.QFontMetricsF(label.font(), label)
                assert metrics.horizontalAdvance(label.text()) <= label.contentsRect().width() + 1, (label.text(), label.size())
                assert metrics.height() <= label.contentsRect().height() + 1, (label.text(), label.size(), metrics.height())
        assert strip.grab().save(str(output / 'all-market-metrics.png'))
        strip.close()

        # Exercise the real DOM presenter's odd-sized fractional-DPI image.
        # Alternating high-contrast pixels expose even slight resampling.
        canvas = window.orderbook.canvas
        canvas.setParent(None)
        canvas.resize(361, 137)
        canvas.show()
        settle(app)
        physical = QtCore.QSize(math.ceil(canvas.width() * dpr), math.ceil(canvas.height() * dpr))
        source = QtGui.QImage(physical, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        source.setDevicePixelRatio(dpr)
        data = np.frombuffer(source.bits(), np.uint8).reshape(source.height(), source.bytesPerLine())
        data[:] = 255
        data[:, 0::8] = data[:, 1::8] = data[:, 2::8] = 0
        canvas._display_frame = {'frame': ('probe', 0, source.width(), source.height(), dpr),
                                 'pixels': SimpleNamespace(images=[source])}
        target = QtGui.QImage(physical, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        target.setDevicePixelRatio(dpr)
        target.fill(QtCore.Qt.GlobalColor.transparent)
        canvas.render(target)
        # Qt may clip the last ceil-rounded row/column outside the logical rect.
        interior = (slice(0, math.floor(canvas.height() * dpr)), slice(0, math.floor(canvas.width() * dpr)))
        assert np.array_equal(pixels(source)[interior], pixels(target)[interior]), 'DOM glyph image was resampled'
        report['dom_pixels_unchanged'] = True
        canvas.setParent(window.orderbook)
        canvas.hide()
        (output / 'report.json').write_text(json.dumps(report, indent=2))
    finally:
        window.close()
        deadline = QtCore.QDeadlineTimer(5000)
        while window._order_flow_runtime_thread.isRunning() and not deadline.hasExpired():
            app.processEvents()
            QtTest.QTest.qWait(10)
        assert not window._order_flow_runtime_thread.isRunning()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        probe(sys.argv[2])
