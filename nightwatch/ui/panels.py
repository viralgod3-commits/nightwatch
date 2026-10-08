"""Registered, movable panels with persistent topology and incremental Qt commits."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from copy import deepcopy
from uuid import uuid4
from typing import Any, Iterable, Mapping

from ..constants import (
    RIGHT_PANEL_DEFAULT_SIZES,
    RIGHT_PANEL_MIN_HEIGHTS,
    RIGHT_PANEL_NAMES,
    RIGHT_PANEL_ONE_COLUMN_ORDER,
    RIGHT_PANEL_SINGLE_MIN_WIDTH,
    RIGHT_PANEL_SPLITTER_HIT_WIDTH,
    RIGHT_PANEL_SPLITTER_VISUAL_WIDTH,
    RIGHT_PANEL_TWO_COLUMN_CELL_MIN_WIDTH,
    RIGHT_PANEL_TWO_COLUMN_FULL_WIDTH,
    RIGHT_PANEL_TWO_COLUMN_SECONDARY_ORDER,
    RIGHT_RAIL_CHART_MIN_WIDTH,
)

try:  # Keep the pure state model importable in non-Qt validation environments.
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import QTimer, Qt, Signal
    from ..presentation import display_frame_interval_ms
except ModuleNotFoundError:  # pragma: no cover - exercised by offline validation.
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    QTimer = Qt = Signal = None  # type: ignore[assignment]
    display_frame_interval_ms = None  # type: ignore[assignment]


STATE_KEY = "right_rail/state_v5"
LEGACY_STATE_KEY = "right_rail/state_v3"
STATE_VERSION = 5
DEFAULT_RAIL_WIDTH_ONE = 420
DEFAULT_RAIL_WIDTH_TWO = 760

_MIN_PAIR_RATIO = 0.02
_MAX_PAIR_RATIO = 0.98


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _safe_float(value: object, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return float(default)
    return numeric


def _bounded_pair_ratio(value: float) -> float:
    return min(_MAX_PAIR_RATIO, max(_MIN_PAIR_RATIO, float(value)))


def row_key(row: Iterable[str]) -> str:
    return "|".join(str(name) for name in row)


def layout_key(rows: Iterable[Iterable[str]]) -> str:
    return "||".join(row_key(row) for row in rows)


def row_weight_key(rows: Iterable[Iterable[str]], row: Iterable[str]) -> str:
    return f"{layout_key(rows)}::{row_key(row)}"


def _row_default_size(row: Iterable[str], default_sizes: Mapping[str, int]) -> float:
    values = [max(1.0, float(default_sizes.get(name, 1))) for name in row]
    # Side-by-side panels share height, so the tallest panel determines the row.
    return max(values, default=1.0)


# Display names are compatibility aliases only; saved topology uses stable IDs.
PANEL_IDS = {"Market depth": "depth", "Trading / positions": "trading",
             "Large trades": "trades", "Watchlist": "watchlist"}


@dataclass(frozen=True, slots=True)
class PanelSpec:
    """Feature registration; outer geometry belongs to the shared rail rules."""

    panel_id: str
    title: str
    factory: Any
    activity_changed: Any = None


@dataclass(frozen=True, slots=True)
class PanelNode:
    id: str


@dataclass(frozen=True, slots=True)
class SplitNode:
    id: str
    axis: str
    children: tuple
    weights: tuple[float, ...]


def panel_ids(node) -> list[str]:
    if node is None:
        return []
    if isinstance(node, PanelNode):
        return [node.id]
    return [name for child in node.children for name in panel_ids(child)]


def split_node(axis, children, weights=None, node_id=None):
    children = tuple(children)
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    values = tuple(weights) if weights is not None else (1.0,) * len(children)
    if len(values) != len(children) or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("Invalid split weights")
    total = sum(values)
    return SplitNode(node_id or "split-" + uuid4().hex, axis, children,
                     tuple(v / total for v in values))


def validate_tree(root) -> None:
    seen, count = set(), 0
    def visit(node, depth):
        nonlocal count
        if node is None:
            return
        count += 1
        if depth > 32 or count > 255 or not isinstance(node.id, str) or not node.id or len(node.id) > 128:
            raise ValueError("Layout exceeds supported bounds")
        if node.id in seen:
            raise ValueError("Duplicate layout ID")
        seen.add(node.id)
        if isinstance(node, PanelNode):
            return
        if not isinstance(node, SplitNode) or node.axis not in ("h", "v"):
            raise ValueError("Invalid split")
        if len(node.children) < 2 or len(node.children) != len(node.weights):
            raise ValueError("Invalid split children")
        if any(not math.isfinite(w) or w <= 0 for w in node.weights) or abs(sum(node.weights) - 1) > 1e-6:
            raise ValueError("Invalid split proportions")
        for child in node.children:
            if child is None:
                raise ValueError("Empty split child")
            visit(child, depth + 1)
    visit(root, 0)


def encode_tree(node):
    if node is None:
        return None
    if isinstance(node, PanelNode):
        return {"kind": "panel", "id": node.id}
    return {"kind": "split", "id": node.id, "axis": node.axis,
            "weights": list(node.weights), "children": [encode_tree(c) for c in node.children]}


def decode_tree(raw, depth=0):
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or depth > 32:
        raise ValueError("Invalid layout document")
    if raw.get("kind") == "panel":
        return PanelNode(raw.get("id"))
    children = raw.get("children")
    if raw.get("kind") != "split" or not isinstance(children, list) or len(children) > 128:
        raise ValueError("Invalid split document")
    weights = raw.get("weights")
    if not isinstance(weights, list):
        raise ValueError("Invalid split weights")
    return SplitNode(raw.get("id"), raw.get("axis"),
                     tuple(decode_tree(c, depth + 1) for c in children),
                     tuple(float(w) for w in weights))


def detach_panel(node, panel_id):
    if node is None or isinstance(node, PanelNode):
        return None if node is not None and node.id == panel_id else node
    children, weights = [], []
    changed = False
    for child, weight in zip(node.children, node.weights):
        result = detach_panel(child, panel_id)
        changed |= result is not child
        if result is not None:
            children.append(result)
            weights.append(weight)
    return split_node(node.axis, children, weights, node.id) if changed else node


def insert_panel(node, source, target, edge):
    axis = "h" if edge in ("left", "right") else "v"
    before = edge in ("left", "above")
    if node is None:
        return source
    if target is None and isinstance(node, SplitNode) and node.axis == axis:
        children = (source, *node.children) if before else (*node.children, source)
        weights = (.25, *[w * .75 for w in node.weights]) if before else (*[w * .75 for w in node.weights], .25)
        return split_node(axis, children, weights, node.id)
    if target is None or node.id == target:
        return split_node(axis, (source, node) if before else (node, source))
    if isinstance(node, PanelNode):
        return node
    # Insert directly into an existing matching split; preserve peer proportions.
    if node.axis == axis and target in [c.id for c in node.children]:
        index = next(i for i, c in enumerate(node.children) if c.id == target)
        children, weights = list(node.children), list(node.weights)
        weight = weights[index] / 2
        weights[index] = weight
        index += 0 if before else 1
        children.insert(index, source)
        weights.insert(index, weight)
        return split_node(node.axis, children, weights, node.id)
    children = tuple(insert_panel(c, source, target, edge) for c in node.children)
    return replace(node, children=children) if children != node.children else node


def replace_split_weights(node, split_id, values):
    if not isinstance(node, SplitNode):
        return node
    if node.id == split_id:
        return split_node(node.axis, node.children, values, node.id)
    children = tuple(replace_split_weights(c, split_id, values) for c in node.children)
    return replace(node, children=children) if children != node.children else node


def valid_panel_names(names, available):
    """Shared preset membership validation, including zero/one-panel layouts."""
    if not isinstance(names, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(n) for n in names if str(n) in available))


@dataclass(slots=True)
class PanelState:
    enabled: bool
    weight_one: float


@dataclass(slots=True)
class RightRailState:
    version: int = STATE_VERSION
    column_mode: int = 1
    active_preset: str = "Custom"
    rail_width_one: int = DEFAULT_RAIL_WIDTH_ONE
    rail_width_two: int = DEFAULT_RAIL_WIDTH_TWO
    panels: dict[str, PanelState] = field(default_factory=dict)
    root: Any = None
    layouts: dict[str, Any] = field(default_factory=dict)
    hidden: dict[str, dict] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=lambda: dict(PANEL_IDS))
    unresolved_panels: dict[str, dict] = field(default_factory=dict)

    def rail_width(self):
        return self.rail_width_two if self.column_mode == 2 else self.rail_width_one

    def set_rail_width(self, value):
        if self.column_mode == 2:
            self.rail_width_two = max(0, int(value))
        else:
            self.rail_width_one = max(0, int(value))

    def to_dict(self):
        return {"version": STATE_VERSION, "column_mode": self.column_mode,
                "active_preset": self.active_preset, "rail_width_one": self.rail_width_one,
                "rail_width_two": self.rail_width_two, "root": encode_tree(self.root),
                "layouts": {k: encode_tree(v) for k, v in self.layouts.items()},
                "hidden": deepcopy(self.hidden),
                "panels": {**deepcopy(self.unresolved_panels),
                           **{self.aliases.get(n, n): {"enabled": p.enabled, "weight_one": p.weight_one}
                              for n, p in self.panels.items()}}}

    @classmethod
    def from_dict(cls, payload, panel_names=RIGHT_PANEL_NAMES, default_sizes=RIGHT_PANEL_DEFAULT_SIZES, aliases=None):
        version = _safe_int(payload.get("version", 4), 4)
        if version > STATE_VERSION:
            raise ValueError("Newer layout schema")
        aliases = dict(aliases or {n: PANEL_IDS.get(n, n) for n in panel_names})
        state = cls(column_mode=2 if payload.get("column_mode") == 2 else 1,
                    active_preset=str(payload.get("active_preset", "Custom")), aliases=aliases)
        for key in ("rail_width_one", "rail_width_two"):
            setattr(state, key, max(0, _safe_int(payload.get(key), getattr(state, key))))
        raw_panels = payload.get("panels", {})
        if not isinstance(raw_panels, Mapping):
            raw_panels = {}
        for name in panel_names:
            raw = raw_panels.get(aliases[name] if version == 5 else name, {})
            raw = raw if isinstance(raw, Mapping) else {}
            state.panels[name] = PanelState(bool(raw.get("enabled", False)),
                max(1e-6, _safe_float(raw.get("weight_one"), default_sizes.get(name, 100))))
        if version == 5:
            state.unresolved_panels = {str(pid): {"enabled": bool(raw.get("enabled", False)),
                "weight_one": max(1e-6, _safe_float(raw.get("weight_one"), 100))}
                for pid, raw in list(raw_panels.items())[:128] if pid not in aliases.values() and isinstance(raw, Mapping)}
            state.root = decode_tree(payload.get("root"))
            validate_tree(state.root)
            layouts = payload.get("layouts", {})
            if isinstance(layouts, Mapping):
                for key in ("1", "2"):
                    if key in layouts:
                        tree = decode_tree(layouts[key]); validate_tree(tree)
                        state.layouts[key] = tree
            hidden = payload.get("hidden", {})
            if isinstance(hidden, Mapping):
                state.hidden = {str(k): dict(v) for k, v in list(hidden.items())[:128] if isinstance(v, Mapping)}
        if "Large trades" in state.panels:
            if version == 5:
                def migrate_alerts(node):
                    if isinstance(node, PanelNode):
                        return PanelNode("trades") if node.id == "alerts" else node
                    if isinstance(node, SplitNode):
                        return replace(node, children=tuple(migrate_alerts(c) for c in node.children))
                    return node
                if "trades" in panel_ids(state.root):
                    state.root = detach_panel(state.root, "alerts")
                else:
                    state.root = migrate_alerts(state.root)
                for key, tree in tuple(state.layouts.items()):
                    state.layouts[key] = (detach_panel(tree, "alerts") if "trades" in panel_ids(tree)
                                          else migrate_alerts(tree))
                state.unresolved_panels.pop("alerts", None)
                state.hidden.pop("alerts", None)
            elif "Alerts" in raw_panels:
                old = raw_panels["Alerts"]
                if isinstance(old, Mapping):
                    state.panels["Large trades"].enabled = bool(old.get("enabled", False))
        # Retire the known Orders panel while retaining unrelated plugin IDs.
        if "orders" not in aliases.values():
            state.root = detach_panel(state.root, "orders")
            state.layouts = {k: detach_panel(v, "orders") for k, v in state.layouts.items()}
            state.unresolved_panels.pop("orders", None)
            state.hidden.pop("orders", None)
        model = RightRailModel(state, panel_names, default_sizes)
        if version < 5:
            state.layouts = {str(mode): model.template(mode) for mode in (1, 2)}
            # Preserve legacy per-row and pair proportions in the two-column tree.
            rows = model.legacy_rows()
            raw_rows = payload.get("two_row_weights", {})
            raw_pairs = payload.get("two_pair_ratios", {})
            raw_rows = raw_rows if isinstance(raw_rows, Mapping) else {}
            raw_pairs = raw_pairs if isinstance(raw_pairs, Mapping) else {}
            children, weights = [], []
            for row in rows:
                ratio = _bounded_pair_ratio(_safe_float(raw_pairs.get(row_key(row)), .5))
                children.append(split_node("h", [PanelNode(aliases[n]) for n in row],
                                           (ratio, 1-ratio) if len(row) == 2 else None))
                weights.append(max(1e-6, _safe_float(raw_rows.get(row_weight_key(rows, row)),
                                                    _row_default_size(row, default_sizes))))
            state.layouts["2"] = split_node("v", children, weights)
            state.root = state.layouts[str(state.column_mode)]
        else:
            # Unknown IDs remain in the document, never instantiated from settings.
            visible = set(panel_ids(state.root))
            for name, panel in state.panels.items():
                panel.enabled = aliases[name] in visible
        return state


def default_state(panel_names=RIGHT_PANEL_NAMES, default_sizes=RIGHT_PANEL_DEFAULT_SIZES, *,
                  visible_names=(), active_preset="Custom", column_mode=1, aliases=None):
    names = tuple(panel_names)
    state = RightRailState(column_mode=2 if column_mode == 2 else 1, active_preset=active_preset,
                          aliases=dict(aliases or {n: PANEL_IDS.get(n, n) for n in names}))
    visible = set(visible_names)
    state.panels = {n: PanelState(n in visible, float(default_sizes.get(n, 100))) for n in names}
    model = RightRailModel(state, names, default_sizes)
    state.root = model.template(state.column_mode)
    return state


class RightRailModel:
    """Pure layout document and atomic transitions; no QWidget/service access."""
    def __init__(self, state, panel_names=RIGHT_PANEL_NAMES, default_sizes=RIGHT_PANEL_DEFAULT_SIZES):
        self.state = state
        self.panel_names = tuple(panel_names)
        self.default_sizes = dict(default_sizes)

    def name_for_id(self, panel_id):
        return next((n for n, i in self.state.aliases.items() if i == panel_id), panel_id)

    def resolve(self, name):
        return self.state.aliases.get(name, name)

    def visible_names(self, mode=None):
        return [self.name_for_id(i) for i in panel_ids(self.state.root)
                if self.name_for_id(i) in self.state.panels]

    def legacy_rows(self):
        visible = {n for n, p in self.state.panels.items() if p.enabled}
        rows = [(n,) for n in RIGHT_PANEL_TWO_COLUMN_FULL_WIDTH if n in visible]
        used = {n for row in rows for n in row}
        secondary = [n for n in RIGHT_PANEL_TWO_COLUMN_SECONDARY_ORDER if n in visible and n not in used]
        secondary.extend(n for n in self.panel_names if n in visible and n not in used and n not in secondary)
        return rows + [tuple(secondary[i:i+2]) for i in range(0, len(secondary), 2)]

    def template(self, mode):
        if mode == 2:
            visible = {n for n, p in self.state.panels.items() if p.enabled}
            market = [n for n in ("Market depth", "Large trades") if n in visible]
            utility = [n for n in ("Watchlist", "Trading / positions") if n in visible]
            utility.extend(n for n in self.panel_names if n in visible and n not in market + utility)
            columns = [column for column in (market, utility) if column]
            if len(columns) == 1 and len(columns[0]) > 1:
                columns = [[n] for n in columns[0]]
            return split_node("h", [split_node("v", [PanelNode(self.resolve(n)) for n in column],
                              [self.default_sizes.get(n, 100) for n in column]) for column in columns])
        names = [n for n in RIGHT_PANEL_ONE_COLUMN_ORDER if n in self.state.panels and self.state.panels[n].enabled]
        names.extend(n for n in self.panel_names if self.state.panels[n].enabled and n not in names)
        return split_node("v", [PanelNode(self.resolve(n)) for n in names],
                          [max(1e-6, self.state.panels[n].weight_one) for n in names])

    def set_panel_enabled(self, name, enabled):
        name = self.name_for_id(self.resolve(name))
        if name not in self.state.panels:
            raise KeyError(name)
        panel = self.state.panels[name]
        if panel.enabled == bool(enabled):
            return False
        pid = self.resolve(name)
        root = self.state.root
        if enabled:
            hint = self.state.hidden.get(pid, {})
            target = hint.get("target")
            edge = hint.get("edge", "below")
            if edge not in ("above", "below", "left", "right"):
                edge = "below"
            if target not in panel_ids(root):
                target = None
            root = insert_panel(root, PanelNode(pid), target, edge)
        else:
            def hint_for(node):
                if not isinstance(node, SplitNode):
                    return None
                for i, child in enumerate(node.children):
                    if isinstance(child, PanelNode) and child.id == pid:
                        peer = node.children[i-1 if i else 1]
                        return {"target": panel_ids(peer)[-1 if i else 0],
                                "edge": ("right" if i else "left") if node.axis == "h" else ("below" if i else "above")}
                    hint = hint_for(child)
                    if hint:
                        return hint
                return None
            self.state.hidden[pid] = hint_for(root) or {}
            root = detach_panel(root, pid)
        validate_tree(root)
        self.state.root = root
        panel.enabled = bool(enabled)
        self.state.active_preset = "Custom"
        return True

    def set_column_mode(self, mode):
        mode = 2 if mode == 2 else 1
        if mode == self.state.column_mode:
            return False
        self.state.layouts[str(self.state.column_mode)] = self.state.root
        candidate = self.state.layouts.get(str(mode))
        if set(panel_ids(candidate)) != set(panel_ids(self.state.root)):
            candidate = self.template(mode)
        self.state.column_mode = mode
        self.state.root = candidate
        self.state.active_preset = "Custom"
        return True

    def apply_preset(self, name, visible_names, section_sizes=None, *, reset_geometry=False, tree=None):
        visible = set(valid_panel_names(visible_names, self.panel_names))
        if tree is not None:
            validate_tree(tree)
            if set(panel_ids(tree)) != {self.resolve(n) for n in visible}:
                raise ValueError("Preset tree must contain exactly its visible panels")
        for n, panel in self.state.panels.items():
            panel.enabled = n in visible
            if reset_geometry:
                panel.weight_one = max(1, (section_sizes or {}).get(n, self.default_sizes.get(n, 100)))
        self.state.layouts.clear()
        self.state.root = tree if tree is not None else self.template(self.state.column_mode)
        self.state.active_preset = str(name)


if QtWidgets is not None:

    class PanelSplitterHandle(QtWidgets.QSplitterHandle):
        """Physical inter-panel gap; extended input is routed without painting."""

        def sizeHint(self) -> QtCore.QSize:
            hint = super().sizeHint()
            if self.orientation() == Qt.Orientation.Horizontal:
                hint.setWidth(self.splitter().handleWidth())
            else:
                hint.setHeight(self.splitter().handleWidth())
            return hint


    class _SplitterHitRouter(QtCore.QObject):
        """One input filter per window; no widget overlaps a chart viewport."""

        _pointer_events = frozenset({
            QtCore.QEvent.Type.MouseMove,
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonRelease,
            QtCore.QEvent.Type.MouseButtonDblClick,
        })

        def __init__(self, window: QtWidgets.QWidget) -> None:
            super().__init__(window)
            self.setObjectName("splitterHitRouter")
            self._window = window
            self._surfaces: list[SplitterGrabSurface] = []
            self._drag_surface: SplitterGrabSurface | None = None
            self._hover_widget: QtWidgets.QWidget | None = None
            self._hover_cursor: QtGui.QCursor | None = None
            self._hover_had_cursor = False
            QtWidgets.QApplication.instance().installEventFilter(self)

        def remove_surface(self, surface: "SplitterGrabSurface") -> None:
            if self._drag_surface is surface:
                self._drag_surface = None
            if surface in self._surfaces:
                self._surfaces.remove(surface)
            self.clear_hover()

        def clear_hover(self) -> None:
            widget, cursor = self._hover_widget, self._hover_cursor
            self._hover_widget = self._hover_cursor = None
            if widget is None:
                return
            try:
                if self._hover_had_cursor:
                    widget.setCursor(cursor)
                else:
                    widget.unsetCursor()
            except RuntimeError:  # The previous input recipient may be deleted.
                pass

        def hit_at(self, position: QtCore.QPoint) -> "SplitterGrabSurface | None":
            if not self._window.isVisible():
                return None
            local = self._window.mapFromGlobal(position)
            for surface in reversed(self._surfaces):
                if surface._enabled and surface.geometry().contains(local):
                    return surface
            return None

        def _track_hover(self, widget, surface) -> None:
            if surface is None:
                self.clear_hover()
                return
            if self._hover_widget is not widget:
                self.clear_hover()
                self._hover_widget = widget
                self._hover_cursor = QtGui.QCursor(widget.cursor())
                self._hover_had_cursor = widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor)
            if widget.cursor().shape() != surface.cursor().shape():
                widget.setCursor(surface.cursor())

        def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
            kind = event.type()
            if kind == QtCore.QEvent.Type.WindowDeactivate and watched is self._window:
                if self._drag_surface is not None:
                    self._drag_surface._cancel_drag()
                    self._drag_surface = None
                self.clear_hover()
            elif kind == QtCore.QEvent.Type.UngrabMouse:
                surface = self._drag_surface
                if surface is not None and watched is surface._mouse_grabber:
                    surface._cancel_drag()
                    self._drag_surface = None
            elif kind == QtCore.QEvent.Type.Leave and watched is self._hover_widget:
                self.clear_hover()
            if kind not in self._pointer_events:
                return False

            # Observe native motion for cursor feedback even over child widgets
            # without mouse tracking. Leave native dispatch/button state intact.
            if watched is self._window.windowHandle():
                if kind == QtCore.QEvent.Type.MouseMove and self._drag_surface is None:
                    if event.buttons() != Qt.MouseButton.NoButton or QtWidgets.QWidget.mouseGrabber() is not None:
                        self.clear_hover()
                        return False
                    widget = QtWidgets.QApplication.widgetAt(event.globalPosition().toPoint())
                    if widget is not None and widget.window() is self._window:
                        self._track_hover(widget, self.hit_at(event.globalPosition().toPoint()))
                    else:
                        self.clear_hover()
                return False
            if not isinstance(watched, QtWidgets.QWidget):
                return False

            surface = self._drag_surface
            if surface is not None:
                if kind == QtCore.QEvent.Type.MouseMove:
                    surface.mouseMoveEvent(event)
                elif kind == QtCore.QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                    surface.mouseReleaseEvent(event)
                    self._drag_surface = None
                return True
            if watched.window() is not self._window:
                self.clear_hover()
                return False
            # A chart/control drag that started elsewhere owns its motion, even
            # when it crosses the cached grip rectangle or leaves the window.
            if kind == QtCore.QEvent.Type.MouseMove and (
                event.buttons() != Qt.MouseButton.NoButton
                or QtWidgets.QWidget.mouseGrabber() is not None
            ):
                self.clear_hover()
                return False
            surface = self.hit_at(event.globalPosition().toPoint())
            if kind == QtCore.QEvent.Type.MouseMove:
                self._track_hover(watched, surface)
                return surface is not None
            if surface is not None and kind in {
                QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonDblClick,
            } and event.button() == Qt.MouseButton.LeftButton:
                self.clear_hover()
                self._drag_surface = surface
                surface.mousePressEvent(event)
                return True
            return False


    class SplitterGrabSurface(QtCore.QObject):
        """Cached oversized hit rectangle, deliberately absent from composition."""

        def __init__(self, splitter: "PanelSplitter", index: int):
            super().__init__(splitter.window())
            self.splitter = splitter
            self.index = int(index)
            self._start_global: QtCore.QPointF | None = None
            self._start_sizes: list[int] = []
            self._geometry = QtCore.QRect()
            self._enabled = False
            self._router: _SplitterHitRouter | None = None
            self._mouse_grabber: QtWidgets.QWidget | None = None
            self._cursor = QtGui.QCursor(
                Qt.CursorShape.SplitHCursor
                if splitter.orientation() == Qt.Orientation.Horizontal
                else Qt.CursorShape.SplitVCursor
            )
            splitter.destroyed.connect(self._splitter_destroyed)

        def parentWidget(self) -> QtWidgets.QWidget:
            return self.parent()

        def geometry(self) -> QtCore.QRect:
            return QtCore.QRect(self._geometry)

        def setGeometry(self, *args) -> None:
            self._geometry = QtCore.QRect(*args)

        def cursor(self) -> QtGui.QCursor:
            return self._cursor

        def set_hit_active(self, enabled: bool) -> None:
            self._enabled = bool(enabled)
            if not enabled and self._router is None:
                self._cancel_drag()
                return
            window = self.parentWidget()
            if self._router is None or self._router.parent() is not window:
                if self._router is not None:
                    self._cancel_drag()
                    self._router.remove_surface(self)
                router = window.findChild(_SplitterHitRouter, "splitterHitRouter", Qt.FindChildOption.FindDirectChildrenOnly)
                self._router = router if router is not None else _SplitterHitRouter(window)
                self._router._surfaces.append(self)
            if not enabled:
                self._cancel_drag()
                if self._router._drag_surface is self:
                    self._router._drag_surface = None
                self._router.clear_hover()

        def hide(self) -> None:
            self.set_hit_active(False)

        def _splitter_destroyed(self) -> None:
            self._enabled = False
            self._cancel_drag()
            if self._router is not None:
                self._router.remove_surface(self)
            super().deleteLater()

        def grabMouse(self) -> None:
            self._mouse_grabber = self.splitter.handle(self.index)
            if self._mouse_grabber is not None:
                self._mouse_grabber.grabMouse()

        def releaseMouse(self) -> None:
            grabber, self._mouse_grabber = self._mouse_grabber, None
            if grabber is not None:
                try:
                    if QtWidgets.QWidget.mouseGrabber() is grabber:
                        grabber.releaseMouse()
                except RuntimeError:
                    pass

        def deleteLater(self) -> None:
            self.hide()
            if self._router is not None:
                self._router.remove_surface(self)
            super().deleteLater()

        def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
            if event.button() != Qt.MouseButton.LeftButton:
                event.ignore()
                return
            self._start_global = event.globalPosition()
            self._start_sizes = list(self.splitter.sizes())
            self.splitter.begin_grab_drag()
            self.grabMouse()
            event.accept()

        def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
            if self._start_global is None:
                event.ignore()
                return
            current = event.globalPosition()
            delta = (
                current.x() - self._start_global.x()
                if self.splitter.orientation() == Qt.Orientation.Horizontal
                else current.y() - self._start_global.y()
            )
            self.splitter.resize_from_grab(
                self.index,
                self._start_sizes,
                int(round(delta)),
            )
            event.accept()

        def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
            if self._start_global is not None:
                current = event.globalPosition()
                delta = (
                    current.x() - self._start_global.x()
                    if self.splitter.orientation() == Qt.Orientation.Horizontal
                    else current.y() - self._start_global.y()
                )
                self.splitter.resize_from_grab(
                    self.index,
                    self._start_sizes,
                    int(round(delta)),
                )
                self._start_global = None
                self._start_sizes = []
                self.splitter.end_grab_drag()
                self.releaseMouse()
                event.accept()
                return
            event.ignore()

        def _cancel_drag(self) -> None:
            if self._start_global is None:
                return
            self._start_global = None
            self._start_sizes = []
            try:
                self.splitter.cancel_grab_drag()
            except RuntimeError:  # Splitter destruction also cancels capture.
                pass
            self.releaseMouse()


    class PanelSplitter(QtWidgets.QSplitter):
        """QSplitter with a fixed inter-panel gap and a larger grab target."""

        drag_started = Signal()
        drag_ended = Signal()

        def __init__(
            self,
            orientation: Qt.Orientation,
            parent: QtWidgets.QWidget | None = None,
            *,
            extended_hit_target: bool = True,
        ) -> None:
            self._commit_owner = None
            self._extended_hit_target = bool(extended_hit_target)
            self._grab_surfaces: dict[int, SplitterGrabSurface] = {}
            self._grab_surface_sync_pending = False
            self._pending_grab_resize: tuple[int, list[int]] | None = None
            self._grab_drag_active = False
            super().__init__(orientation, parent)
            self._grab_resize_timer = QTimer(self)
            self._grab_resize_timer.setSingleShot(True)
            self._grab_resize_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._grab_resize_timer.timeout.connect(self._flush_pending_grab_resize)
            self.setHandleWidth(RIGHT_PANEL_SPLITTER_VISUAL_WIDTH)
            self.setChildrenCollapsible(False)
            self.setOpaqueResize(False)
            self.splitterMoved.connect(lambda *_args: self.request_grab_surface_sync())

        def createHandle(self) -> QtWidgets.QSplitterHandle:
            return PanelSplitterHandle(self.orientation(), self)

        @staticmethod
        def _widget_minimum(widget: QtWidgets.QWidget, horizontal: bool) -> int:
            return max(
                0,
                int(widget.minimumWidth() if horizontal else widget.minimumHeight()),
            )

        @staticmethod
        def _widget_maximum(widget: QtWidgets.QWidget, horizontal: bool) -> int:
            return max(
                0,
                int(widget.maximumWidth() if horizontal else widget.maximumHeight()),
            )

        def begin_grab_drag(self) -> None:
            if self._grab_drag_active:
                return
            self._grab_drag_active = True
            self.drag_started.emit()

        def end_grab_drag(self) -> None:
            if self._grab_resize_timer.isActive():
                self._grab_resize_timer.stop()
            self._flush_pending_grab_resize()
            if not self._grab_drag_active:
                return
            self._grab_drag_active = False
            self.drag_ended.emit()

        def resize_from_grab(self, index: int, start_sizes: list[int], delta: int) -> None:
            before = int(index) - 1
            after = int(index)
            if before < 0 or after >= self.count() or len(start_sizes) != self.count():
                return
            sizes = [max(0, int(value)) for value in start_sizes]
            pair_total = sizes[before] + sizes[after]
            horizontal = self.orientation() == Qt.Orientation.Horizontal
            before_widget = self.widget(before)
            after_widget = self.widget(after)
            minimum_before = self._widget_minimum(before_widget, horizontal)
            minimum_after = self._widget_minimum(after_widget, horizontal)
            maximum_before = self._widget_maximum(before_widget, horizontal)
            maximum_after = self._widget_maximum(after_widget, horizontal)
            lower = max(minimum_before, pair_total - maximum_after)
            upper = min(maximum_before, pair_total - minimum_after)
            if upper < lower:
                return
            target_before = max(lower, min(upper, sizes[before] + int(delta)))
            sizes[before] = target_before
            sizes[after] = max(0, pair_total - target_before)
            current = [max(0, int(value)) for value in self.sizes()]
            if sizes == current:
                self._pending_grab_resize = None
                self._grab_resize_timer.stop()
                return
            pending = self._pending_grab_resize
            if pending is not None and pending == (int(index), sizes):
                return
            self._pending_grab_resize = (int(index), sizes)
            if self._commit_owner is not None:
                self._commit_owner.queue_splitter(self)
            elif not self._grab_resize_timer.isActive():
                self._grab_resize_timer.start(display_frame_interval_ms(self))

        def _flush_pending_grab_resize(self) -> None:
            pending = self._pending_grab_resize
            self._pending_grab_resize = None
            if pending is None:
                return
            index, sizes = pending
            count = self.count()
            if index <= 0 or index >= count or len(sizes) != count:
                return
            before_actual = [max(0, int(value)) for value in self.sizes()]
            if before_actual == sizes:
                return
            self.setSizes(sizes)
            after_actual = [max(0, int(value)) for value in self.sizes()]
            if after_actual == before_actual:
                return
            handle = self.handle(index)
            if handle is None:
                return
            horizontal = self.orientation() == Qt.Orientation.Horizontal
            position = handle.geometry().x() if horizontal else handle.geometry().y()
            self.splitterMoved.emit(int(position), int(index))

        def cancel_grab_drag(self) -> None:
            self._grab_resize_timer.stop()
            self._pending_grab_resize = None
            if not self._grab_drag_active:
                return
            self._grab_drag_active = False
            self.drag_ended.emit()

        def request_grab_surface_sync(self) -> None:
            """Coalesce geometry bursts into one next-turn overlay update."""
            if self._grab_surface_sync_pending:
                return
            self._grab_surface_sync_pending = True
            if self._commit_owner is not None:
                self._commit_owner.queue_splitter(self, surfaces=True)
            else:
                QTimer.singleShot(0, self._flush_grab_surface_sync)

        def _flush_grab_surface_sync(self) -> None:
            self._grab_surface_sync_pending = False
            self.sync_grab_surfaces()

        def dispose_grab_surfaces(self) -> None:
            self.cancel_grab_drag()
            for surface in self._grab_surfaces.values():
                surface.hide()
                surface.deleteLater()
            self._grab_surfaces.clear()

        def sync_grab_surfaces(self) -> None:
            self._grab_surface_sync_pending = False
            if not self._extended_hit_target:
                for surface in self._grab_surfaces.values():
                    surface.hide()
                return
            stale = [index for index in self._grab_surfaces if index >= self.count()]
            for index in stale:
                surface = self._grab_surfaces.pop(index)
                surface.hide()
                surface.deleteLater()

            horizontal = self.orientation() == Qt.Orientation.Horizontal
            half = RIGHT_PANEL_SPLITTER_HIT_WIDTH // 2
            for index in range(1, self.count()):
                handle = self.handle(index)
                if handle is None:
                    continue
                surface = self._grab_surfaces.get(index)
                if surface is None:
                    surface = SplitterGrabSurface(self, index)
                    self._grab_surfaces[index] = surface
                if surface.parentWidget() is not self.window():
                    surface.setParent(self.window())
                geometry = handle.geometry()
                if not self.isVisible() or not handle.isVisible() or geometry.isEmpty():
                    surface.hide()
                    continue
                overlay_parent = surface.parentWidget()
                if overlay_parent is None:
                    surface.hide()
                    continue
                splitter_origin = self.mapTo(overlay_parent, QtCore.QPoint(0, 0))
                handle_origin = handle.mapTo(overlay_parent, QtCore.QPoint(0, 0))
                if horizontal:
                    center = handle_origin.x() + max(1, geometry.width()) // 2
                    x = max(0, center - half)
                    width = min(
                        RIGHT_PANEL_SPLITTER_HIT_WIDTH,
                        max(1, overlay_parent.width() - x),
                    )
                    surface.setGeometry(
                        x,
                        splitter_origin.y(),
                        max(1, width),
                        self.height(),
                    )
                else:
                    center = handle_origin.y() + max(1, geometry.height()) // 2
                    y = max(0, center - half)
                    height = min(
                        RIGHT_PANEL_SPLITTER_HIT_WIDTH,
                        max(1, overlay_parent.height() - y),
                    )
                    surface.setGeometry(
                        splitter_origin.x(),
                        y,
                        self.width(),
                        max(1, height),
                    )
                owner = self._commit_owner
                if owner is not None and self is not owner._main_splitter:
                    viewport = owner._scroll.viewport()
                    clip = QtCore.QRect(viewport.mapTo(overlay_parent, QtCore.QPoint()), viewport.size())
                    surface.setGeometry(surface.geometry().intersected(clip))
                surface.set_hit_active(not surface.geometry().isEmpty())

        def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
            super().resizeEvent(event)
            self.request_grab_surface_sync()

        def moveEvent(self, event: QtGui.QMoveEvent) -> None:
            super().moveEvent(event)
            self.request_grab_surface_sync()

        def showEvent(self, event: QtGui.QShowEvent) -> None:
            super().showEvent(event)
            self.request_grab_surface_sync()

        def hideEvent(self, event: QtGui.QHideEvent) -> None:
            for surface in self._grab_surfaces.values():
                surface.hide()
            super().hideEvent(event)


    class RightPanelEdgeOverlay(QtWidgets.QWidget):
        """Paint a rounded perimeter with tiny corner caches and direct edges."""

        def __init__(self, parent: QtWidgets.QWidget) -> None:
            super().__init__(parent)
            self._radius = 0
            self._border_color = QtGui.QColor("#202020")
            self._gap_color = QtGui.QColor("#000000")
            self._corner_cache: tuple[QtGui.QPixmap, QtGui.QPixmap, QtGui.QPixmap, QtGui.QPixmap] = ()
            self._corner_cache_dpr = 0.0
            self._corner_extent = 0
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.hide()

        def set_edge_style(self, *, radius: int, border_color: str, gap_color: str = "#000000") -> None:
            radius = max(0, int(radius))
            border = QtGui.QColor(str(border_color))
            gap = QtGui.QColor(str(gap_color))
            if not border.isValid():
                border = QtGui.QColor("#202020")
            if not gap.isValid():
                gap = QtGui.QColor("#000000")
            changed = radius != self._radius or border != self._border_color or gap != self._gap_color
            self._radius = radius
            self._border_color = border
            self._gap_color = gap
            self.setVisible(radius > 0)
            if changed:
                self._rebuild_corner_cache()

        def _build_corner(self, ratio: float) -> QtGui.QPixmap:
            radius = float(self._radius)
            extent = self._corner_extent
            pixels = max(1, int(round(extent * ratio)))
            cache = QtGui.QPixmap(pixels, pixels)
            cache.setDevicePixelRatio(ratio)
            cache.fill(Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(cache)
            try:
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                tile = QtCore.QRectF(0.0, 0.0, float(extent), float(extent))
                virtual = QtCore.QRectF(0.5, 0.5, radius * 2.0, radius * 2.0)
                rounded = QtGui.QPainterPath()
                rounded.addRoundedRect(virtual, radius, radius)
                outside = QtGui.QPainterPath()
                outside.addRect(tile)
                painter.fillPath(outside.subtracted(rounded), self._gap_color)
                pen = QtGui.QPen(self._border_color)
                pen.setWidthF(1.0)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(rounded)
            finally:
                painter.end()
            return cache

        def _rebuild_corner_cache(self) -> None:
            if self._radius <= 0:
                self._corner_cache = ()
                self._corner_cache_dpr = 0.0
                self._corner_extent = 0
                self.update()
                return
            ratio = max(1.0, float(self.devicePixelRatioF()))
            self._corner_extent = max(2, int(math.ceil(self._radius + 1.5)))
            top_left = self._build_corner(ratio)
            h = QtGui.QTransform()
            h.scale(-1.0, 1.0)
            v = QtGui.QTransform()
            v.scale(1.0, -1.0)
            hv = QtGui.QTransform()
            hv.scale(-1.0, -1.0)
            self._corner_cache = (top_left, top_left.transformed(h), top_left.transformed(v), top_left.transformed(hv))
            self._corner_cache_dpr = ratio
            self.update()

        def event(self, event: QtCore.QEvent) -> bool:
            handled = super().event(event)
            dpr_change = getattr(QtCore.QEvent.Type, 'DevicePixelRatioChange', None)
            if dpr_change is not None and event.type() == dpr_change and self._radius > 0:
                self._rebuild_corner_cache()
            return handled

        def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
            super().resizeEvent(event)
            ratio = max(1.0, float(self.devicePixelRatioF()))
            if self._radius > 0 and abs(ratio - self._corner_cache_dpr) > 1e-6:
                self._rebuild_corner_cache()
            else:
                self.update()

        def paintEvent(self, event: QtGui.QPaintEvent) -> None:
            del event
            if self._radius <= 0 or len(self._corner_cache) != 4 or self.width() <= 1 or self.height() <= 1:
                return
            width = self.width()
            height = self.height()
            extent = min(self._corner_extent, width, height)
            painter = QtGui.QPainter(self)
            try:
                tl, tr, bl, br = self._corner_cache
                painter.drawPixmap(0, 0, tl)
                painter.drawPixmap(max(0, width - extent), 0, tr)
                painter.drawPixmap(0, max(0, height - extent), bl)
                painter.drawPixmap(max(0, width - extent), max(0, height - extent), br)
                pen = QtGui.QPen(self._border_color)
                pen.setWidthF(1.0)
                pen.setCosmetic(True)
                painter.setPen(pen)
                radius = min(float(self._radius), width * 0.5, height * 0.5)
                left, top = 0.5, 0.5
                right, bottom = max(left, width - 0.5), max(top, height - 0.5)
                painter.drawLine(QtCore.QPointF(left + radius, top), QtCore.QPointF(right - radius, top))
                painter.drawLine(QtCore.QPointF(left + radius, bottom), QtCore.QPointF(right - radius, bottom))
                painter.drawLine(QtCore.QPointF(left, top + radius), QtCore.QPointF(left, bottom - radius))
                painter.drawLine(QtCore.QPointF(right, top + radius), QtCore.QPointF(right, bottom - radius))
            finally:
                painter.end()


    class RightPanelShell(QtWidgets.QFrame):
        """Unified outer geometry contract for one right-rail feature."""

        def __init__(
            self,
            name: str,
            content: QtWidgets.QWidget,
            parent: QtWidgets.QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.panel_name = str(name)
            self.content = content
            self.setObjectName("section")
            self.setProperty("rightRail", True)
            self.setProperty("rightRailPanel", self.panel_name)
            self.setMinimumWidth(0)
            self.setMinimumHeight(
                max(0, int(RIGHT_PANEL_MIN_HEIGHTS.get(self.panel_name, 56)))
            )
            self.setMaximumSize(16777215, 16777215)
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
            content.setMinimumSize(0, 0)
            content.setMaximumSize(16777215, 16777215)
            content.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Ignored,
            )
            layout.addWidget(content, 1)
            self._edge_overlay = RightPanelEdgeOverlay(self)
            self._edge_overlay.setGeometry(self.rect())
            self._edge_overlay.raise_()

        def set_edge_style(
            self,
            *,
            radius: int,
            border_color: str,
            gap_color: str = "#000000",
        ) -> None:
            self._edge_overlay.set_edge_style(
                radius=radius,
                border_color=border_color,
                gap_color=gap_color,
            )
            self._edge_overlay.raise_()

        def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
            super().resizeEvent(event)
            self._edge_overlay.setGeometry(self.rect())
            self._edge_overlay.raise_()

        def showEvent(self, event: QtGui.QShowEvent) -> None:
            super().showEvent(event)
            self._edge_overlay.setGeometry(self.rect())
            self._edge_overlay.raise_()

        def minimumSizeHint(self) -> QtCore.QSize:
            return QtCore.QSize(0, self.minimumHeight())

        def sizeHint(self) -> QtCore.QSize:
            return QtCore.QSize(
                0,
                max(
                    self.minimumHeight(),
                    int(RIGHT_PANEL_DEFAULT_SIZES.get(self.panel_name, 160)),
                ),
            )

        def set_content_margin(self, margin: int) -> None:
            margin = max(0, int(margin))
            layout = self.layout()
            if layout is not None:
                layout.setContentsMargins(margin, margin, margin, margin)


    class RightRailController(QtCore.QObject):
        """Registry, layout transaction, activity and geometry boundary for the rail."""
        state_changed = Signal(object)
        composition_changed = Signal()
        geometry_changed = Signal()
        operation_rejected = Signal(str)
        activity_changed = Signal(str, bool)
        drag_started = Signal()
        drag_ended = Signal()
        registry_changed = Signal()

        def __init__(self, settings, contents, presets, *, initial_preset="Depth + Trading", parent=None, clock=None):
            super().__init__(parent)
            self.settings, self.presets = settings, dict(presets)
            self.initial_preset = initial_preset
            self.registry, self.sections, self._containers, self._panel_actions = {}, {}, {}, {}
            self._panel_action_slots = {}
            self._rendering, self._host_active, self._future_schema = False, True, False
            self._revision, self._active_ids = 0, set()
            self._main_splitter = self._chart_widget = None
            self._chart_minimum_width = RIGHT_RAIL_CHART_MIN_WIDTH
            self._clock = clock
            self._dirty_splitters, self._dirty_surfaces = set(), set()
            self._geometry_pending, self._outer_pending = False, False
            self._margin, self._edge_style = 0, None
            self.rail = QtWidgets.QFrame()
            self.rail.setObjectName("rightRailHost")
            layout = QtWidgets.QVBoxLayout(self.rail)
            # Keep the chart-facing edge flush and clear the other edges of
            # window/toolbar borders with the same black gutter.
            gap = RIGHT_PANEL_SPLITTER_VISUAL_WIDTH
            layout.setContentsMargins(0, gap, gap, gap); layout.setSpacing(0)
            self._scroll = QtWidgets.QScrollArea(self.rail)
            self._scroll.setObjectName("rightRailScroll")
            self._scroll.viewport().setObjectName("rightRailViewport")
            self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self._scroll.setWidgetResizable(True)
            self._canvas = QtWidgets.QWidget()
            self._canvas.setObjectName("rightRailCanvas")
            self._canvas_layout = QtWidgets.QVBoxLayout(self._canvas)
            self._canvas_layout.setContentsMargins(0, 0, 0, 0); self._canvas_layout.setSpacing(0)
            self._scroll.setWidget(self._canvas)
            layout.addWidget(self._scroll)
            self._parking = QtWidgets.QWidget(self.rail); self._parking.hide()
            for widget in (self.rail, self._scroll.viewport(), self._canvas):
                widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                widget.installEventFilter(self)
            self._scroll.horizontalScrollBar().valueChanged.connect(lambda _: self._queue_geometry())
            self._scroll.verticalScrollBar().valueChanged.connect(lambda _: self._queue_geometry())
            self._geometry_persist_timer = QTimer(self)
            self._geometry_persist_timer.setSingleShot(True); self._geometry_persist_timer.setInterval(180)
            self._geometry_persist_timer.timeout.connect(self.save_state)
            self._fallback_frame = QTimer(self)
            self._fallback_frame.setSingleShot(True)
            self._fallback_frame.timeout.connect(self._commit_frame)
            if clock is not None:
                clock.interaction_frame.connect(self._commit_frame)
            specs = contents if not isinstance(contents, Mapping) else [
                PanelSpec(PANEL_IDS.get(name, name), name, lambda widget=widget: widget)
                for name, widget in contents.items()]
            for spec in specs:
                self._register_spec(spec)
            names = tuple(s.title for s in self.registry.values())
            aliases = {s.title: s.panel_id for s in self.registry.values()}
            sizes = {s.title: RIGHT_PANEL_DEFAULT_SIZES.get(s.title, 160) for s in self.registry.values()}
            state = self._load_state(names, sizes, aliases)
            self.model = RightRailModel(state, names, sizes)
            self._render()
            self.save_state()

        @property
        def column_mode(self):
            return self.model.state.column_mode

        @property
        def revision(self):
            return self._revision

        @property
        def state(self):
            # Callers cannot mutate the authoritative document through a signal or property.
            return deepcopy(self.model.state)

        def _register_spec(self, spec):
            if not isinstance(spec, PanelSpec) or not spec.panel_id or len(spec.panel_id) > 128 or spec.panel_id.startswith("split-"):
                raise ValueError("Invalid panel registration")
            if not isinstance(spec.title, str) or not spec.title:
                raise ValueError("Invalid panel metadata")
            if spec.panel_id in self.registry or spec.title in self.sections:
                raise ValueError("Duplicate panel registration")
            content = spec.factory()
            if not isinstance(content, QtWidgets.QWidget):
                raise TypeError("Panel factory must return a QWidget")
            if any(shell.content is content for shell in self.sections.values()):
                raise ValueError("A widget can belong to only one panel")
            self.registry[spec.panel_id] = spec
            shell = RightPanelShell(spec.title, content, self._parking)
            shell.panel_id = spec.panel_id
            shell.set_content_margin(self._margin)
            if self._edge_style:
                shell.set_edge_style(**self._edge_style)
            shell.hide()
            self.sections[spec.title] = shell


        def _load_state(self, names, sizes, aliases):
            for key in (STATE_KEY, "right_rail/state_v4", LEGACY_STATE_KEY):
                raw = self.settings.value(key, "", str)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, Mapping):
                        continue
                    if _safe_int(payload.get("version"), 0) > STATE_VERSION:
                        self._future_schema = True
                        break
                    state = RightRailState.from_dict(payload, names, sizes, aliases)
                    if state.active_preset not in (*self.presets, "Custom"):
                        state.active_preset = "Custom"
                    return state
                except (ValueError, TypeError, OverflowError, RecursionError):
                    continue
            preset_name = self.settings.value("right_layout_preset", self.initial_preset, str)
            if preset_name not in (*self.presets, "Custom"):
                preset_name = (
                    self.initial_preset if self.initial_preset in self.presets
                    else next(iter(self.presets), "Custom")
                )
            preset = self.presets.get(preset_name, self.presets.get(self.initial_preset, {}))
            visible = valid_panel_names(preset.get("visible", ()), names)
            if any(self.settings.contains(f"panel/{n}") for n in (*names, "Alerts")):
                visible = tuple(n for n in names if self.settings.value(
                    f"panel/{n}", self.settings.value("panel/Alerts", False, bool) if n == "Large trades" else False, bool
                ))
            state = default_state(names, sizes, visible_names=visible, active_preset=preset_name,
                                  column_mode=_safe_int(preset.get("column_mode", self.settings.value("right_panel_columns_v1", 1)), 1), aliases=aliases)
            if "tree" in preset and set(visible) == set(preset.get("visible", ())):
                RightRailModel(state, names, sizes).apply_preset(preset_name, visible, tree=decode_tree(preset["tree"]))
                state.set_rail_width(_safe_int(preset.get("rail_width"), state.rail_width()))
            for mode, attr in ((1, "rail_width_one"), (2, "rail_width_two")):
                setattr(state, attr, max(0, _safe_int(self.settings.value(f"right_panel_rail_width_{mode}col_v1", getattr(state, attr)), getattr(state, attr))))
            return state

        def save_state(self):
            # Keep legacy payloads as rollback data; never overwrite a future schema.
            if self._future_schema:
                return
            self._geometry_persist_timer.stop()
            self.settings.setValue(STATE_KEY, json.dumps(self.model.state.to_dict(), separators=(",", ":"), allow_nan=False))

        def attach_main_splitter(self, splitter, chart_widget, *, chart_minimum_width=RIGHT_RAIL_CHART_MIN_WIDTH):
            self._main_splitter, self._chart_widget = splitter, chart_widget
            self._chart_minimum_width = max(1, int(chart_minimum_width))
            chart_widget.setMinimumWidth(self._chart_minimum_width)
            splitter.setHandleWidth(0)
            self._configure_splitter(splitter)
            splitter.installEventFilter(self)
            splitter.splitterMoved.connect(self._outer_splitter_moved)
            self._outer_pending = True
            self._queue_geometry()

        def _configure_splitter(self, splitter):
            if isinstance(splitter, PanelSplitter):
                splitter._commit_owner = self
                splitter.drag_started.connect(self.drag_started.emit)
                splitter.drag_ended.connect(self._finish_drag)

        def _finish_drag(self):
            self.capture_geometry()
            self._geometry_persist_timer.start()
            self.drag_ended.emit()

        def _cancel_interactions(self):
            for splitter in self.interaction_splitters():
                splitter.cancel_grab_drag()
            self._dirty_splitters.clear()

        def queue_splitter(self, splitter, *, surfaces=False):
            (self._dirty_surfaces if surfaces else self._dirty_splitters).add(splitter)
            self._request_frame()

        def _request_frame(self):
            if self._clock is not None:
                self._clock.request()
            elif not self._fallback_frame.isActive():
                self._fallback_frame.start(display_frame_interval_ms(self.rail))

        def _queue_geometry(self):
            self._geometry_pending = True
            self._request_frame()

        def _commit_frame(self, _timestamp=0):
            dirty, self._dirty_splitters = self._dirty_splitters, set()
            for splitter in dirty:
                splitter._flush_pending_grab_resize()
            if self._geometry_pending:
                self._geometry_pending = False
                if self._outer_pending:
                    self._outer_pending = False
                    self._apply_outer_width()
                # Resizing queues these same splitters through their own
                # resize/move events. Merge the sets so every grip is updated
                # once after the frame's geometry transaction.
                self._dirty_surfaces.update(self.interaction_splitters())
                if isinstance(self._main_splitter, PanelSplitter):
                    self._dirty_surfaces.add(self._main_splitter)
                self.geometry_changed.emit()
            surfaces, self._dirty_surfaces = self._dirty_surfaces, set()
            for splitter in surfaces:
                splitter.sync_grab_surfaces()

        def _resolved_tree(self, node):
            if node is None:
                return None
            if isinstance(node, PanelNode):
                return node if node.id in self.registry else None
            children, weights = [], []
            for child, weight in zip(node.children, node.weights):
                resolved = self._resolved_tree(child)
                if resolved is not None:
                    children.append(resolved); weights.append(weight)
            return split_node(node.axis, children, weights, node.id)

        def _render(self):
            self._rendering = True
            self._cancel_interactions()
            focus = QtWidgets.QApplication.focusWidget()
            used = set()
            tree = self._resolved_tree(self.model.state.root)
            visible = set(panel_ids(tree))
            try:
                def reconcile(node):
                    if isinstance(node, PanelNode):
                        return self.sections[self.registry[node.id].title]
                    splitter = self._containers.get(node.id)
                    fresh = splitter is None
                    if fresh:
                        splitter = PanelSplitter(Qt.Orientation.Horizontal if node.axis == "h" else Qt.Orientation.Vertical, self._parking)
                        splitter.setObjectName("rightRailSplit")
                        self._containers[node.id] = splitter
                        self._configure_splitter(splitter)
                        splitter.splitterMoved.connect(lambda _p, _i, key=node.id: self._splitter_moved(key))
                    used.add(node.id)
                    previous = getattr(splitter, "_layout_node", None)
                    children = [reconcile(child) for child in node.children]
                    for index, child in enumerate(children):
                        if child.parentWidget() is not splitter or splitter.indexOf(child) != index:
                            splitter.insertWidget(index, child)
                        child.show()
                    # Removed children must leave before proportions are applied.
                    for index in reversed(range(splitter.count())):
                        child = splitter.widget(index)
                        if child not in children:
                            child.hide(); child.setParent(self._parking)
                    if fresh or previous != node:
                        splitter.setSizes([max(1, round(w * 10000)) for w in node.weights])
                    splitter._layout_node = node
                    return splitter
                for pid, spec in self.registry.items():
                    shell = self.sections[spec.title]
                    if pid not in visible and shell.parentWidget() is not self._parking:
                        shell.hide(); shell.setParent(self._parking)
                root_widget = reconcile(tree) if tree is not None else None
                current = self._canvas_layout.itemAt(0)
                if current is not None and current.widget() is not root_widget:
                    old = current.widget(); self._canvas_layout.removeWidget(old)
                    # It may already be a descendant of the new root; never park it here.
                if root_widget is not None:
                    if self._canvas_layout.indexOf(root_widget) < 0:
                        self._canvas_layout.addWidget(root_widget)
                    root_widget.show()
                for key in set(self._containers) - used:
                    old = self._containers.pop(key)
                    self._dirty_splitters.discard(old); self._dirty_surfaces.discard(old)
                    old.dispose_grab_surfaces()
                    old.hide(); old.deleteLater()
                self._apply_minimum_constraints()
                desired_visible = self._host_active and bool(visible)
                if self.rail.isHidden() == desired_visible:
                    self.rail.setVisible(desired_visible)
                self._sync_actions()
                if focus is not None and focus.isVisible():
                    focus.setFocus(Qt.FocusReason.OtherFocusReason)
            finally:
                self._rendering = False
            self._sync_activity()
            self._queue_geometry()

        def _changed(self):
            self._revision += 1
            self._render()
            self._geometry_persist_timer.start()
            self.composition_changed.emit()
            self.state_changed.emit(self.state)

        def _sync_activity(self):
            active = set(panel_ids(self.model.state.root)) & set(self.registry) if self._host_active else set()
            previous, self._active_ids = self._active_ids, active
            for pid in sorted(active ^ previous):
                enabled = pid in active
                spec = self.registry.get(pid)
                if spec is not None and spec.activity_changed is not None:
                    spec.activity_changed(enabled)
                self.activity_changed.emit(pid, enabled)

        def set_host_active(self, active):
            active = bool(active)
            if active == self._host_active:
                return
            self.capture_geometry()
            self._host_active = active
            if not active:
                self._cancel_interactions()
            self.rail.setVisible(active and bool(self.visible_names()))
            self._sync_activity()
            self._queue_geometry()

        def panel_active(self, name):
            return self.model.resolve(name) in self._active_ids

        def panel_enabled(self, name):
            return self.model.resolve(name) in panel_ids(self.model.state.root)

        def visible_names(self):
            return self.model.visible_names()

        def set_panel_enabled(self, name, enabled):
            pid = self.model.resolve(name)
            if pid not in self.registry or self.panel_enabled(pid) == bool(enabled):
                return
            self.capture_geometry()
            if self.model.set_panel_enabled(self.registry[pid].title, enabled):
                self._changed()

        def ensure_panel(self, name):
            self.set_panel_enabled(name, True)


        def set_column_mode(self, columns):
            self.capture_geometry()
            if self.model.set_column_mode(columns):
                self._outer_pending = True
                self._changed()

        def apply_preset(self, name, preset, *, reset_geometry=False, column_mode=None):
            tree = decode_tree(preset.get("tree"))
            validate_tree(tree)
            visible = valid_panel_names(preset.get("visible", ()), self.model.panel_names)
            if tree is not None and set(panel_ids(tree)) != {self.model.resolve(n) for n in visible}:
                raise ValueError("Preset tree must contain exactly its visible panels")
            self.capture_geometry()
            if column_mode is None:
                column_mode = preset.get("column_mode")
            if column_mode is not None:
                self.model.state.column_mode = 2 if column_mode == 2 else 1
            sizes = dict(zip(self.model.panel_names, preset.get("sections", ())))
            self.model.apply_preset(name, visible, sizes, reset_geometry=reset_geometry, tree=tree)
            if "rail_width" in preset:
                self.model.state.set_rail_width(_safe_int(preset["rail_width"], 660))
            self._outer_pending = True
            self._changed()

        def reset(self, name, preset):
            self.apply_preset(name, preset, reset_geometry=True)

        def update_presets(self, presets):
            self.presets = dict(presets)
            if self.model.state.active_preset not in self.presets:
                self.model.state.active_preset = "Custom"
            self.state_changed.emit(self.state)

        def register_panel_action(self, name, action):
            name = self.model.name_for_id(self.model.resolve(name))
            previous, slot = self._panel_actions.get(name), self._panel_action_slots.get(name)
            if previous is action:
                return
            if previous is not None and slot is not None:
                previous.toggled.disconnect(slot)
            self._panel_actions[name] = action
            slot = lambda checked, name=name: self.set_panel_enabled(name, checked)
            self._panel_action_slots[name] = slot
            action.toggled.connect(slot)
            self._sync_actions()

        def _sync_actions(self):
            for name, action in self._panel_actions.items():
                blocker = QtCore.QSignalBlocker(action)
                action.setChecked(self.panel_enabled(name))
                del blocker

        def interaction_splitters(self):
            return tuple(self._containers.values())

        def capture_geometry(self):
            if self._rendering:
                return
            for key in self._containers:
                self._capture_split(key)
            if self._main_splitter is not None and not self.rail.isHidden():
                sizes = self._main_splitter.sizes()
                if len(sizes) == 2 and sizes[1] > 0:
                    self.model.state.set_rail_width(sizes[1])

        def _capture_split(self, key):
            splitter = self._containers.get(key)
            if splitter is None or splitter.isHidden():
                return
            sizes = splitter.sizes()
            if len(sizes) < 2 or min(sizes) <= 0:
                return
            node = getattr(splitter, "_layout_node", None)
            if node is None or len(node.children) != len(sizes):
                return
            if any(pid not in self.registry for pid in panel_ids(self.model.state.root)):
                return  # Retain unresolved saved branches until their panels return.
            self.model.state.root = replace_split_weights(self.model.state.root, key, sizes)
            # Cache actual weights so unrelated commits don't restore stale sizes.
            def find(node):
                if isinstance(node, SplitNode):
                    if node.id == key:
                        return node
                    return next((found for child in node.children if (found := find(child)) is not None), None)
                return None
            splitter._layout_node = find(self.model.state.root)

        def _splitter_moved(self, key):
            if not self._rendering:
                self._capture_split(key)
                self._geometry_persist_timer.start()
                self._queue_geometry()

        def _outer_splitter_moved(self, _position, _index):
            if not self._rendering:
                self.capture_geometry()
                self._geometry_persist_timer.start()
                self._queue_geometry()

        def _available_rail_width(self) -> int | None:
            if self._main_splitter is None:
                return None
            total = sum(max(0, int(value)) for value in self._main_splitter.sizes())
            if total <= 0:
                return None
            return max(0, total - self._chart_minimum_width)

        def _required_rail_width(self) -> int:
            if not self.visible_names():
                return 0
            tree = self._resolved_tree(self.model.state.root)
            margins = self.rail.layout().contentsMargins()
            return max(RIGHT_PANEL_SINGLE_MIN_WIDTH, int(self._tree_minimum(tree).width())) + margins.left() + margins.right()

        def _effective_rail_minimum_width(self) -> int:
            required = self._required_rail_width()
            available = self._available_rail_width()
            if available is not None:
                required = min(required, available)
            return max(0, required)

        def _apply_outer_width(self):
            if self._main_splitter is None or self.rail.isHidden():
                return
            sizes_now = self._main_splitter.sizes()
            total = sum(max(0, int(value)) for value in sizes_now)
            if total <= 0:
                return
            maximum = max(0, total - self._chart_minimum_width)
            minimum = min(self._effective_rail_minimum_width(), maximum)
            if self.rail.minimumWidth() != minimum:
                self.rail.setMinimumWidth(minimum)
            target = max(minimum, min(maximum, int(self.model.state.rail_width())))
            sizes = [max(0, total - target), target]
            if sizes != sizes_now:
                self._main_splitter.setSizes(sizes)

        def _panel_minimum_height(self, name):
            return max(0, int(RIGHT_PANEL_MIN_HEIGHTS.get(name, 56))) + 2 * self._margin

        def _tree_minimum(self, node):
            if node is None:
                return QtCore.QSize(0, 0)
            if isinstance(node, PanelNode):
                spec = self.registry[node.id]
                return QtCore.QSize(RIGHT_PANEL_TWO_COLUMN_CELL_MIN_WIDTH + 2 * self._margin, self._panel_minimum_height(spec.title))
            sizes = [self._tree_minimum(c) for c in node.children]
            gap = RIGHT_PANEL_SPLITTER_VISUAL_WIDTH * (len(sizes)-1)
            return QtCore.QSize(sum(s.width() for s in sizes) + gap, max(s.height() for s in sizes)) if node.axis == "h" else QtCore.QSize(max(s.width() for s in sizes), sum(s.height() for s in sizes)+gap)

        def _apply_minimum_constraints(self):
            for pid, spec in self.registry.items():
                shell = self.sections[spec.title]
                minimum = QtCore.QSize(RIGHT_PANEL_TWO_COLUMN_CELL_MIN_WIDTH + 2*self._margin, self._panel_minimum_height(spec.title))
                if shell.minimumSize() != minimum:
                    shell.setMinimumSize(minimum)
            minimum = self._tree_minimum(self._resolved_tree(self.model.state.root))
            if self._canvas.minimumSize() != minimum:
                self._canvas.setMinimumSize(minimum)
            rail_minimum = self._effective_rail_minimum_width()
            if self.rail.minimumWidth() != rail_minimum:
                self.rail.setMinimumWidth(rail_minimum)

        def refresh_geometry_constraints(self):
            before_canvas = self._canvas.minimumSize()
            before_rail = self.rail.minimumWidth()
            self._apply_minimum_constraints()
            if before_canvas != self._canvas.minimumSize() or before_rail != self.rail.minimumWidth():
                self._outer_pending = True
                self._queue_geometry()

        def set_panel_margin(self, margin):
            self._margin = max(0, int(margin))
            for shell in self.sections.values():
                shell.set_content_margin(self._margin)
            self.refresh_geometry_constraints()

        def set_panel_edge_style(self, **style):
            self._edge_style = style
            for shell in self.sections.values():
                shell.set_edge_style(**style)

        def sync_interaction_surfaces(self):
            splitters = list(self.interaction_splitters())
            if isinstance(self._main_splitter, PanelSplitter):
                splitters.append(self._main_splitter)
            for splitter in splitters:
                splitter.sync_grab_surfaces()

        def placement_context(self, name):
            pid = self.model.resolve(name)
            result = {"spans_host_width": True, "touches_host_bottom": True, "visible": self.panel_enabled(name)}
            def visit(node):
                if not isinstance(node, SplitNode):
                    return
                for index, child in enumerate(node.children):
                    if pid in panel_ids(child):
                        if node.axis == "h":
                            result["spans_host_width"] = False
                        elif index != len(node.children)-1:
                            result["touches_host_bottom"] = False
                        visit(child); return
            visit(self.model.state.root)
            return result

        def eventFilter(self, watched, event):
            kind = event.type()
            if watched is self._main_splitter and kind == QtCore.QEvent.Type.Resize:
                # Screen/DPI/window resizes can proportionally shrink the outer
                # splitter without emitting splitterMoved. Re-apply the stored
                # rail preference and the current tree minimum on the next frame.
                self._outer_pending = True
            if kind in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Show):
                self._queue_geometry()
            return super().eventFilter(watched, event)


else:  # pragma: no cover - lightweight placeholders for pure-state validation.

    class PanelSplitter:  # type: ignore[no-redef]
        pass

    class RightPanelShell:  # type: ignore[no-redef]
        pass

    class RightRailController:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("PySide6 is required for RightRailController")
