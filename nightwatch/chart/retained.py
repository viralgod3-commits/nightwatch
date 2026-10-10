"""Keep disabled graphics out of the live ViewBox without discarding their data."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

import pyqtgraph as pg
import shiboken6
from PySide6 import QtCore, QtWidgets


@dataclass
class _RetainedGraphic:
    ignore_bounds: bool
    parked: bool = False


class RetainedChartItems(QtCore.QObject):
    """Detach explicitly hidden graphics and restore them on their next show.

    PlotItem registration remains intact: adding through PlotItem again would
    overwrite each curve's clipping/downsampling options. ViewBox removal also
    disconnects the graphics' children from range and transform notifications;
    reparenting back refreshes their view caches against the current camera.
    """

    def __init__(
        self,
        view: pg.ViewBox,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent or view)
        self._view: pg.ViewBox | None = view
        self._items: dict[QtWidgets.QGraphicsObject, _RetainedGraphic] = {}
        self._changing: set[QtWidgets.QGraphicsObject] = set()
        view.destroyed.connect(self._view_destroyed)

    def register(self, item: QtWidgets.QGraphicsObject) -> None:
        """Retain an existing direct child, including its original bounds policy."""
        if item in self._items:
            return
        view = self._view
        if view is None or not shiboken6.isValid(view):
            return
        if not isinstance(item, QtWidgets.QGraphicsObject):
            raise TypeError("Retained chart graphics must provide visibleChanged")
        if item.parentItem() is not view.childGroup:
            raise ValueError("Retained chart graphics must belong directly to the ViewBox")
        self._items[item] = _RetainedGraphic(item not in view.addedItems)
        if item.parent() is None:
            # Scene removal relinquishes Qt ownership. The helper owns parked
            # objects until the chart closes, including their child graphics.
            item.setParent(self)
        item.visibleChanged.connect(partial(self._visibility_changed, item))
        item.destroyed.connect(partial(self._item_destroyed, item))
        self._visibility_changed(item)

    def _visibility_changed(self, item: QtWidgets.QGraphicsObject) -> None:
        if item in self._changing:
            return
        view = self._view
        state = self._items.get(item)
        if (
            state is None
            or view is None
            or not shiboken6.isValid(view)
            or not shiboken6.isValid(item)
        ):
            return
        # Ancestor visibility changes must not turn an explicitly shown item
        # into a parked item which can no longer hear its ancestor's next show.
        visible = item.isVisibleTo(None if state.parked else view.childGroup)
        if visible == (not state.parked):
            return
        self._changing.add(item)
        try:
            if visible:
                # Adopt the final parent before ViewBox.addItem's scene step.
                # An unparented PlotDataItem briefly discovers GraphicsView
                # instead of ViewBox, which breaks clip-to-view restoration.
                item.setParentItem(view.childGroup)
                view.addItem(item, ignoreBounds=state.ignore_bounds)
                state.parked = False
            else:
                self._detach_paint_callbacks(item)
                view.removeItem(item)
                state.parked = True
        finally:
            self._changing.discard(item)

    @staticmethod
    def _detach_paint_callbacks(item: QtWidgets.QGraphicsObject) -> None:
        pending = [item]
        while pending:
            graphic = pending.pop()
            pending.extend(graphic.childItems())
            if not isinstance(graphic, pg.TextItem):
                continue
            scene = graphic._lastScene
            if scene is None:
                continue
            if shiboken6.isValid(scene):
                try:
                    scene.sigPrepareForPaint.disconnect(graphic.updateTransform)
                except (TypeError, RuntimeError):
                    pass
            # TextItem reconnects on its first paint in the restored scene.
            graphic._lastScene = None

    def _item_destroyed(self, item: QtWidgets.QGraphicsObject, *_args) -> None:
        self._items.pop(item, None)
        self._changing.discard(item)

    def _view_destroyed(self, *_args) -> None:
        self._view = None
        self._items.clear()
        self._changing.clear()
