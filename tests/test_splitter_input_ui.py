"""Behavior of wide splitter grips without a widget over the chart."""

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from nightwatch.constants import RIGHT_PANEL_SPLITTER_HIT_WIDTH
from nightwatch.ui.panels import PanelSplitter


class InputPane(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.presses = 0
        self.paints = 0
        self.moves = []
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        self.presses += 1
        event.accept()

    def paintEvent(self, event):
        self.paints += 1
        super().paintEvent(event)

    def mouseMoveEvent(self, event):
        self.moves.append(event.position().toPoint())
        event.accept()


def settle(app):
    for _ in range(4):
        app.processEvents()


@pytest.fixture(params=[QtCore.Qt.Orientation.Horizontal, QtCore.Qt.Orientation.Vertical])
def desk(qapp, request):
    window = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    splitter = PanelSplitter(request.param)
    panes = [InputPane(), InputPane()]
    for pane in panes:
        pane.setMinimumSize(40, 40)
        splitter.addWidget(pane)
    layout.addWidget(splitter)
    window.resize(600, 400)
    window.show()
    settle(qapp)
    splitter.setSizes([200, 200])
    splitter.sync_grab_surfaces()
    settle(qapp)
    yield window, splitter, panes
    splitter.dispose_grab_surfaces()
    window.close()
    window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    settle(qapp)


def grip_point(window, splitter, pane):
    rect = splitter._grab_surfaces[1].geometry()
    point = rect.center()
    if splitter.orientation() == QtCore.Qt.Orientation.Horizontal:
        point.setX(rect.left() + 1)
    else:
        point.setY(rect.top() + 1)
    return pane.mapFromGlobal(window.mapToGlobal(point))


def drag_end(splitter, start, distance=60):
    delta = QtCore.QPoint(distance, 0) if splitter.orientation() == QtCore.Qt.Orientation.Horizontal else QtCore.QPoint(0, distance)
    return start + delta


def test_wide_grip_drag_from_neighbor_preserves_signals_and_final_resize(desk, qapp):
    window, splitter, panes = desk
    started, ended = [], []
    splitter.drag_started.connect(lambda: started.append(True))
    splitter.drag_ended.connect(lambda: ended.append(True))
    start_sizes = splitter.sizes()
    start = grip_point(window, splitter, panes[0])
    end = drag_end(splitter, start)
    QtTest.QTest.mousePress(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=start)
    QtTest.QTest.mouseMove(panes[0], end)
    QtTest.QTest.mouseRelease(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=end)
    settle(qapp)
    assert splitter.sizes()[0] == start_sizes[0] + 60
    assert sum(splitter.sizes()) == sum(start_sizes)
    assert panes[0].presses == 0
    assert started == [True] and ended == [True]
    assert not splitter._grab_drag_active
    assert QtWidgets.QWidget.mouseGrabber() is None


def test_repaint_neighbor_does_not_create_a_painting_hit_widget(desk, qapp):
    _, splitter, panes = desk
    surface = splitter._grab_surfaces[1]
    rect = surface.geometry()
    extent = rect.width() if splitter.orientation() == QtCore.Qt.Orientation.Horizontal else rect.height()
    assert extent == RIGHT_PANEL_SPLITTER_HIT_WIDTH
    assert surface._enabled
    assert not isinstance(surface, QtWidgets.QWidget)
    paints_before = panes[0].paints
    for _ in range(8):
        panes[0].update()
        settle(qapp)
    assert panes[0].paints > paints_before
    assert surface._enabled and surface.geometry() == rect


def test_click_away_from_grip_reaches_pane(desk, qapp):
    _, splitter, panes = desk
    sizes = splitter.sizes()
    QtTest.QTest.mouseClick(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(10, 10))
    settle(qapp)
    assert panes[0].presses == 1
    assert splitter.sizes() == sizes
    assert not splitter._grab_drag_active


@pytest.mark.parametrize("explicit_cursor", [False, True])
def test_hover_restores_inherited_or_explicit_cursor(desk, qapp, explicit_cursor):
    window, splitter, panes = desk
    pane = panes[0]
    if explicit_cursor:
        pane.setCursor(QtCore.Qt.CursorShape.CrossCursor)
    else:
        pane.unsetCursor()
    original = pane.cursor().shape()
    QtTest.QTest.mouseMove(pane, grip_point(window, splitter, pane))
    assert pane.cursor().shape() == splitter._grab_surfaces[1].cursor().shape()
    QtTest.QTest.mouseMove(pane, QtCore.QPoint(10, 10))
    assert pane.cursor().shape() == original
    assert pane.testAttribute(QtCore.Qt.WidgetAttribute.WA_SetCursor) == explicit_cursor


@pytest.mark.parametrize("cancel", ["hide", "deactivate", "ungrab", "dispose"])
def test_drag_cancellation_drops_pending_resize_and_capture(desk, qapp, cancel):
    window, splitter, panes = desk
    ended = []
    splitter.drag_ended.connect(lambda: ended.append(True))
    sizes = splitter.sizes()
    point = grip_point(window, splitter, panes[0])
    QtTest.QTest.mousePress(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=point)
    splitter.resize_from_grab(1, sizes, 50)
    if cancel == "hide":
        splitter.hide()
    elif cancel == "deactivate":
        QtCore.QCoreApplication.sendEvent(window, QtCore.QEvent(QtCore.QEvent.Type.WindowDeactivate))
    elif cancel == "ungrab":
        QtCore.QCoreApplication.sendEvent(splitter.handle(1), QtCore.QEvent(QtCore.QEvent.Type.UngrabMouse))
    else:
        splitter.dispose_grab_surfaces()
    settle(qapp)
    assert ended == [True]
    assert not splitter._grab_drag_active
    assert splitter._pending_grab_resize is None
    assert splitter.sizes() == sizes
    assert QtWidgets.QWidget.mouseGrabber() is None


def test_drag_honors_neighbor_minimum_sizes(desk, qapp):
    window, splitter, panes = desk
    start = grip_point(window, splitter, panes[0])
    end = drag_end(splitter, start, 2000)
    QtTest.QTest.mousePress(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=start)
    QtTest.QTest.mouseRelease(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=end)
    settle(qapp)
    assert splitter.sizes()[1] == 40


def test_right_click_does_not_begin_resize(desk, qapp):
    window, splitter, panes = desk
    sizes = splitter.sizes()
    QtTest.QTest.mouseClick(panes[0], QtCore.Qt.MouseButton.RightButton,
                           pos=grip_point(window, splitter, panes[0]))
    settle(qapp)
    assert not splitter._grab_drag_active
    assert splitter.sizes() == sizes


def test_second_button_release_does_not_end_left_drag(desk, qapp):
    window, splitter, panes = desk
    point = grip_point(window, splitter, panes[0])
    QtTest.QTest.mousePress(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=point)
    QtTest.QTest.mouseRelease(panes[0], QtCore.Qt.MouseButton.RightButton, pos=point)
    assert splitter._grab_drag_active
    QtTest.QTest.mouseRelease(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=point)
    assert not splitter._grab_drag_active


def test_deleted_splitter_leaves_no_active_input_target(desk, qapp):
    _, splitter, _ = desk
    router = splitter._grab_surfaces[1]._router
    splitter.dispose_grab_surfaces()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    settle(qapp)
    assert router._surfaces == []
    assert router._drag_surface is None


@pytest.mark.parametrize("explicit_capture", [False, True])
def test_existing_pane_drag_crossing_grip_keeps_its_mouse_motion(desk, qapp, explicit_capture):
    window, splitter, panes = desk
    pane = panes[0]
    point = grip_point(window, splitter, pane)
    sizes = splitter.sizes()
    if explicit_capture:
        pane.grabMouse()
    else:
        QtTest.QTest.mousePress(pane, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(10, 10))
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove, QtCore.QPointF(point),
        QtCore.QPointF(pane.mapToGlobal(point)), QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.NoButton if explicit_capture else QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtCore.QCoreApplication.sendEvent(pane, event)
    assert pane.moves[-1] == point
    assert not splitter._grab_drag_active and splitter.sizes() == sizes
    if explicit_capture:
        pane.releaseMouse()
    else:
        QtTest.QTest.mouseRelease(pane, QtCore.Qt.MouseButton.LeftButton, pos=point)


def test_reparented_splitter_routes_only_in_new_window(desk, qapp):
    old_window, splitter, panes = desk
    old_router = splitter._grab_surfaces[1]._router
    new_window = QtWidgets.QWidget()
    new_layout = QtWidgets.QVBoxLayout(new_window)
    new_layout.addWidget(splitter)
    new_window.resize(600, 400)
    new_window.show()
    settle(qapp)
    splitter.sync_grab_surfaces()
    surface = splitter._grab_surfaces[1]
    assert old_router._surfaces == []
    assert surface._router.parent() is new_window
    sizes = splitter.sizes()
    point = grip_point(new_window, splitter, panes[0])
    end = drag_end(splitter, point, 30)
    QtTest.QTest.mousePress(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=point)
    QtTest.QTest.mouseRelease(panes[0], QtCore.Qt.MouseButton.LeftButton, pos=end)
    settle(qapp)
    assert splitter.sizes()[0] == sizes[0] + 30
    old_window.layout().addWidget(splitter)
    old_window.show()
    settle(qapp)
    new_window.close()
    new_window.deleteLater()
