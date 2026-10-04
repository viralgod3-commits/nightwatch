"""One bounded worker-owned tape history and acknowledged visible-view deltas."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import math
import uuid

from ..models import OrderFlowSnapshot, OrderFlowTradePrint
from .revisions import print_revision_key, prints_equal


TAPE_CAPACITY = 500
TAPE_INTERVAL_SECONDS = .1
MAX_TAPE_VIEWS = 32
TapeKey = tuple[int, int]
TapeEntries = tuple[tuple[TapeKey, OrderFlowTradePrint], ...]


@dataclass(frozen=True, slots=True)
class TapeState:
    symbol: str
    stream: str
    epoch: int
    revision: int
    threshold: float
    all_revision: int
    large_revision: int
    all_entries: TapeEntries
    large_entries: TapeEntries


@dataclass(frozen=True, slots=True)
class TapePatch:
    order: tuple[TapeKey, ...] | None
    removed: tuple[TapeKey, ...]
    upserts: TapeEntries


@dataclass(frozen=True, slots=True)
class TapeStateDelta:
    header: tuple
    base_revision: int | None
    all_patch: TapePatch
    large_patch: TapePatch


@dataclass(frozen=True, slots=True)
class TapeFrame:
    consumer: str
    token: int
    symbol: str
    mode: str
    stream: str
    revision: int
    base_revision: int | None
    threshold: float
    patch: TapePatch


class TapeSeedRequired(RuntimeError):
    pass


def make_patch(entries: TapeEntries, base: TapeEntries | None) -> TapePatch:
    if entries is base:
        return TapePatch(None, (), ())
    old = dict(base or ())
    current = dict(entries)
    order = tuple(current)
    return TapePatch(order if base is None or order != tuple(old) else None,
                     tuple(key for key in old if key not in current),
                     tuple((key, trade) for key, trade in entries if old.get(key) != trade))


def apply_patch(base: TapeEntries, patch: TapePatch, *, seed: bool) -> TapeEntries:
    if not isinstance(patch, TapePatch) or (seed and patch.order is None):
        raise TapeSeedRequired('Tape seed omitted its ordering')
    if not seed and patch.order is None and not patch.removed and not patch.upserts:
        return base
    rows = {} if seed else dict(base)
    for key in patch.removed:
        rows.pop(key, None)
    for key, trade in patch.upserts:
        if not isinstance(trade, OrderFlowTradePrint):
            raise TapeSeedRequired('Unexpected tape record')
        rows[key] = trade
    order = patch.order if patch.order is not None else tuple(rows)
    if len(order) > TAPE_CAPACITY or len(set(order)) != len(order) or set(order) != rows.keys():
        raise TapeSeedRequired('Tape patch has an incomplete or oversized history')
    return tuple((key, rows[key]) for key in order)


class TradeTapeHistory:
    """Mutable only in the ingestion worker; widgets never build histories."""

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.stream = uuid.uuid4().hex
        self.epoch = self.revision = self.all_revision = self.large_revision = 0
        self.threshold = 1000.0
        self._all = OrderedDict()
        self._large = OrderedDict()
        self._state = None
        self._all_entries = self._large_entries = ()
        self._entries_versions = (-1, -1)
        self._snapshot_key = None
        self._snapshot_prints = ()
        self._snapshot_sequence = self._last_print = 0

    def reset(self, symbol: str, *, preserve: bool = True):
        symbol = symbol.upper()
        if symbol != self.symbol or not preserve:
            self._all.clear()
            self._large.clear()
            self.stream = uuid.uuid4().hex
        self.symbol = symbol
        self.epoch += 1
        self.revision += 1
        self.all_revision += 1
        self.large_revision += 1
        self.threshold = 1000.0
        self._state = None
        self._snapshot_key = None
        self._snapshot_prints = ()
        self._snapshot_sequence = self._last_print = 0

    def clear(self):
        # Clearing presentation must not make old legacy snapshot prints new.
        self._all.clear()
        self._large.clear()
        self.stream = uuid.uuid4().hex
        self.revision += 1
        self.all_revision += 1
        self.large_revision += 1
        self._state = None

    def set_threshold(self, threshold: float):
        threshold = max(1.0, float(threshold or 1.0))
        if not math.isfinite(threshold):
            raise ValueError('Non-finite tape threshold')
        if threshold != self.threshold:
            self.threshold = threshold
            self.revision += 1
            self._state = None

    def add(self, trade: OrderFlowTradePrint):
        key = self.epoch, trade.sequence
        self._all[key] = trade
        if len(self._all) > TAPE_CAPACITY:
            self._all.popitem(last=False)
        self.all_revision += 1
        if trade.salience_class >= 1:
            self._large[key] = trade
            if len(self._large) > TAPE_CAPACITY:
                self._large.popitem(last=False)
            self.large_revision += 1
        self.revision += 1
        self._last_print = max(self._last_print, trade.sequence)
        self._state = None

    def correct(self, trade: OrderFlowTradePrint):
        key = self.epoch, trade.sequence
        changed = False
        for history, version in ((self._all, 'all_revision'), (self._large, 'large_revision')):
            if key in history and history[key] != trade:
                history[key] = trade
                setattr(self, version, getattr(self, version) + 1)
                changed = True
        if changed:
            self.revision += 1
            self._state = None

    def resolve(self, sequence: int, outcome: str, direction: int):
        key = self.epoch, sequence
        trade = self._all.get(key) or self._large.get(key)
        if trade is not None:
            self.correct(replace(trade, outcome=outcome, outcome_direction=direction))

    def ingest_snapshot(self, snapshot: OrderFlowSnapshot):
        """Compatibility ingestion, also worker-only, for standalone hosts."""
        prints, key = snapshot.recent_prints, print_revision_key(snapshot)
        if snapshot.symbol != self.symbol:
            self.reset(snapshot.symbol, preserve=False)
        elif (key is not None and self._snapshot_key is not None
              and key[0] == self._snapshot_key[0]
              and snapshot.sequence < self._snapshot_sequence):
            return  # An explicit stream distinguishes stale delivery from reset.
        elif ((key is not None and self._snapshot_key is not None and key[0] != self._snapshot_key[0])
              or snapshot.sequence < self._snapshot_sequence
              or (prints and prints[-1].sequence < self._last_print)):
            self.reset(snapshot.symbol)
        self.set_threshold(snapshot.large_trade_threshold)
        self._snapshot_sequence = snapshot.sequence
        unchanged = (key == self._snapshot_key if key is not None and self._snapshot_key is not None
                     else prints_equal(prints, self._snapshot_prints))
        self._snapshot_key = key
        if unchanged:
            return
        self._snapshot_prints = prints
        for trade in prints:
            if trade.sequence > self._last_print:
                self.add(trade)
            else:
                self.correct(trade)

    def state(self) -> TapeState:
        if self._state is None:
            if self._entries_versions[0] != self.all_revision:
                self._all_entries = tuple(reversed(self._all.items()))
            if self._entries_versions[1] != self.large_revision:
                self._large_entries = tuple(reversed(self._large.items()))
            self._entries_versions = self.all_revision, self.large_revision
            self._state = TapeState(self.symbol, self.stream, self.epoch, self.revision,
                                    self.threshold, self.all_revision, self.large_revision,
                                    self._all_entries, self._large_entries)
        return self._state

    def restore(self, state: TapeState):
        if state.symbol != self.symbol:
            return
        self.stream = state.stream
        self.epoch = state.epoch + 1  # The restarted analyzer's sequences begin anew.
        self.revision = state.revision + 1
        self.all_revision, self.large_revision = state.all_revision + 1, state.large_revision + 1
        self.threshold = state.threshold
        self._all = OrderedDict(reversed(state.all_entries))
        self._large = OrderedDict(reversed(state.large_entries))
        self._state = None
        self._entries_versions = (-1, -1)


class TapeStateEncoder:
    def __init__(self):
        self.state = None

    def encode(self, state: TapeState, *, seed: bool = False):
        base = self.state
        seed |= base is None or base.symbol != state.symbol or base.stream != state.stream
        if not seed and base.revision == state.revision:
            return None
        patch = TapeStateDelta(
            (state.symbol, state.stream, state.epoch, state.revision, state.threshold,
             state.all_revision, state.large_revision),
            None if seed else base.revision,
            make_patch(state.all_entries, None if seed else base.all_entries),
            make_patch(state.large_entries, None if seed else base.large_entries))
        self.state = state
        return patch


class TapeStateDecoder:
    def __init__(self):
        self.state = None

    def decode(self, patch: TapeStateDelta):
        seed = patch.base_revision is None
        base = self.state
        if not seed and (base is None or patch.header[:2] != (base.symbol, base.stream)
                         or patch.base_revision != base.revision):
            raise TapeSeedRequired('Tape checkpoint lost its base')
        all_entries = apply_patch(() if seed else base.all_entries, patch.all_patch, seed=seed)
        large_entries = apply_patch(() if seed else base.large_entries, patch.large_patch, seed=seed)
        state = TapeState(*patch.header, all_entries, large_entries)
        self.state = state
        return state


class _TapeView:
    def __init__(self, consumer, token, symbol, mode, active):
        self.consumer, self.token, self.symbol = consumer, token, symbol
        self.mode, self.active = mode, active
        self.base = self.pending = self.key = None
        self.revision = None
        self.last_sent = -math.inf


class TapePublisher:
    """At most one unacknowledged frame per view; ingestion never waits."""

    def __init__(self):
        self.views = {}
        self.frames = self.seeds = self.upserts = self.acks = 0

    def set_view(self, consumer, token, symbol, mode, active):
        if not active:
            self.views.pop(consumer, None)
            return
        if consumer not in self.views and len(self.views) >= MAX_TAPE_VIEWS:
            raise ValueError('Too many visible trade-tape consumers')
        previous = self.views.get(consumer)
        if previous is not None and (previous.token, previous.symbol, previous.mode) == (token, symbol, mode):
            return
        self.views[consumer] = _TapeView(consumer, token, symbol, mode, True)

    def reset(self):
        for view in self.views.values():
            view.base = view.pending = view.key = view.revision = None

    def ack(self, consumer, token, revision):
        view = self.views.get(consumer)
        if view is None or view.token != token or view.pending is None or view.pending[0] != revision:
            return
        view.revision, view.base, view.key = view.pending
        view.pending = None
        self.acks += 1

    @staticmethod
    def _key(state, view):
        version = state.all_revision if view.mode == 'ALL' else state.large_revision
        # All shows a row count. Only Large displays the adaptive threshold;
        # switching modes seeds its current value with the retained records.
        return state.stream, version, state.threshold if view.mode == 'LARGE' else None

    def publish(self, state: TapeState, now: float):
        frames = []
        for view in self.views.values():
            key = self._key(state, view)
            if (view.symbol != state.symbol or view.pending is not None or key == view.key
                    or now < view.last_sent + TAPE_INTERVAL_SECONDS):
                continue
            entries = state.all_entries if view.mode == 'ALL' else state.large_entries
            patch = make_patch(entries, view.base)
            frame = TapeFrame(view.consumer, view.token, state.symbol, view.mode, state.stream,
                              state.revision, view.revision, state.threshold, patch)
            view.pending = state.revision, entries, key
            view.last_sent = now
            frames.append(frame)
            self.frames += 1
            self.seeds += int(view.base is None)
            self.upserts += len(patch.upserts)
        return frames

    def next_at(self, state: TapeState):
        return min((view.last_sent + TAPE_INTERVAL_SECONDS for view in self.views.values()
                    if view.symbol == state.symbol and view.pending is None and self._key(state, view) != view.key),
                   default=math.inf)

    def diagnostic_state(self):
        return dict(tape_visible_views=len(self.views), tape_frames=self.frames,
                    tape_seeds=self.seeds, tape_upserts=self.upserts, tape_acks=self.acks)
