"""Pixel anchors, mouse hit regions and cache invalidation for order overlays."""
import math

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from nightwatch.chart.magnetic_rail import MagneticRailLineOverlay
from nightwatch.chart.workspace import ChartWorkspace
from nightwatch.theme import DEFAULT_THEME_NAME, THEMES


@pytest.fixture
def chart(qapp):
    instance = ChartWorkspace(dict(THEMES[DEFAULT_THEME_NAME]), use_opengl=False)
    instance.resize(1280, 800)
    instance.show()
    qapp.processEvents()
    instance.price_plot.setRange(xRange=(0, 100), yRange=(95, 105), padding=0)
    instance.set_order_rail_market_symbol("BTCUSDT")
    instance.set_working_orders([
        {"symbol": "BTCUSDT", "orderId": index + 1, "_source": "STANDARD",
         "side": "BUY", "type": "LIMIT", "price": str(98 + index * .2),
         "origQty": "1", "executedQty": "0", "status": "NEW", "positionSide": "BOTH"}
        for index in range(20)
    ])
    qapp.processEvents()
    yield instance
    instance.set_presentation_active(False)
    instance._interaction_gc.set_active(id(instance), False)
    instance.close()
    instance.deleteLater()
    qapp.processEvents()


def test_shared_plot_geometry_is_measured_once_for_twenty_orders(chart, monkeypatch):
    view = chart.price_plot.getViewBox()
    original = view.sceneBoundingRect
    calls = []
    monkeypatch.setattr(view, "sceneBoundingRect", lambda: calls.append(1) or original())
    chart._position_interaction_overlays()
    assert len(chart._active_parked_order_rails()) == 20
    assert len(calls) == 1
    assert chart._overlay_frame_geometry is None


@pytest.mark.parametrize("logarithmic", [False, True])
def test_price_anchors_follow_native_qt_mapping_after_camera_and_resize(chart, qapp, logarithmic):
    chart.logarithmic = logarithmic
    chart.current_price = 100
    view = chart.price_plot.getViewBox()
    for size, x_range, price_range in (
        ((1280, 800), (0, 100), (95, 105)),
        ((960, 640), (35, 180), (99, 101)),
        ((1600, 900), (-150, 5), (97, 108)),
    ):
        chart.resize(*size)
        qapp.processEvents()
        y_range = tuple(math.log10(value) for value in price_range) if logarithmic else price_range
        view.setRange(xRange=x_range, yRange=y_range, padding=0)
        chart._position_interaction_overlays()
        for parked in chart._active_parked_order_rails():
            price = parked["price"]
            shown = math.log10(price) if logarithmic else price
            expected = chart.graphics.mapFromScene(view.mapViewToScene(QtCore.QPointF(x_range[0], shown))).y()
            actual = chart._order_rail_view_geometry(price)[1]
            assert actual == expected
            hud = parked["hud"]
            assert hud.price_text == chart._order_rail_price_text_for(price)
            assert hud.armed
        assert chart._overlay_frame_geometry is None


def test_overlay_transaction_clears_on_exception_and_axis_width_change(chart):
    with pytest.raises(RuntimeError):
        with chart._overlay_geometry_transaction():
            chart._overlay_price_view_geometry()
            assert chart._overlay_frame_geometry
            chart._apply_price_axis_width(130)
            assert chart._overlay_frame_geometry == {}
            raise RuntimeError("interrupted overlay commit")
    assert chart._overlay_frame_geometry is None
    chart._position_interaction_overlays()
    assert all(parked["hud"].isVisible() for parked in chart._active_parked_order_rails())


def test_axis_text_cache_is_bounded_and_font_changes_remeasure(chart):
    class Metrics:
        def __init__(self, advance):
            self.advance = advance
            self.calls = 0

        def horizontalAdvance(self, text):
            self.calls += 1
            return self.advance

    first = Metrics(90)
    chart._chart_axis_metrics = first
    chart._reserve_price_axis_for_text("100.00")
    chart._reserve_price_axis_for_text("101.00")
    chart._reserve_price_axis_for_text("100.00")
    assert first.calls == 2
    for index in range(400):
        chart._reserve_price_axis_for_text(str(index))
    assert len(chart._price_axis_text_widths) == 256
    second = Metrics(125)
    chart._chart_axis_metrics = second
    chart._reset_price_axis_width()
    chart._reserve_price_axis_for_text("100.00")
    assert second.calls == 1
    assert chart._effective_axis_width == 139


def test_armed_mouse_hit_regions_update_only_when_the_region_changes(qapp, monkeypatch):
    parent = QtWidgets.QWidget()
    parent.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    overlay = MagneticRailLineOverlay(parent)
    overlay.resize(400, 60)
    installed = []
    original = overlay.setMask
    monkeypatch.setattr(overlay, "setMask", lambda region: installed.append(region) or original(region))
    state = dict(theme=dict(THEMES[DEFAULT_THEME_NAME]), config=overlay.config,
                 color=QtGui.QColor("green"), phase=0, dragging=False, armed=True,
                 side="BUY", arm_progress=1, implosion_progress=0)
    for y in (20.1, 20.2, 20.9):
        overlay.set_state(line_y=y, **state)
        assert overlay.mask() == QtGui.QRegion(0, int(y) - 5, 400, 11)
        assert not overlay.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert len(installed) == 1
    parent.setCursor(QtCore.Qt.CursorShape.CrossCursor)
    assert overlay.cursor().shape() == QtCore.Qt.CursorShape.ArrowCursor
    overlay.set_state(line_y=21, **state)
    overlay.resize(450, 60)
    overlay.set_state(line_y=21, **state)
    assert overlay.mask() == QtGui.QRegion(0, 16, 450, 11)
    assert len(installed) == 3
    for transition in (dict(arm_progress=.998), dict(implosion_progress=.1), dict(armed=False)):
        overlay.set_state(line_y=21, **{**state, **transition})
        assert overlay.mask().isEmpty()
        assert overlay.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    parent.deleteLater()


def test_workspace_gpu_status_does_not_claim_unpainted_native_batches(chart):
    state = chart.diagnostic_state()
    assert state["actual_render_path"] == "software"
    chart._requested_opengl = True
    chart.use_opengl = True  # Test reporting only; the Qt viewport remains raster.
    for item in (chart.history_candles, chart.live_candle, chart.volume_overlay):
        item.set_gpu_enabled(True)
    state = chart.diagnostic_state()
    assert state["actual_render_path"] == "unverified"
    chart.history_candles.pixel_batch._gpu_diagnostics.native_success({"renderer": "test GPU"})
    assert chart.diagnostic_state()["actual_render_path"] == "native"
    chart.volume_overlay.history_batch._gpu_diagnostics.native_failure("test resource failure")
    state = chart.diagnostic_state()
    assert state["actual_render_path"] == "fallback"
    assert state["render_path"]["native_draws"] == 1
    assert state["render_path"]["native_failures"] == 1
    assert state["render_path"]["fallback_reasons"] == {"test resource failure": 1}
    chart._opengl_runtime_failed = True
    chart.use_opengl = False
    chart._opengl_runtime_failure_reason = "test invalid viewport"
    state = chart.diagnostic_state()
    assert state["actual_render_path"] == "fallback"
    assert state["requested_opengl"]
    assert state["runtime_fallback_reason"] == "test invalid viewport"
