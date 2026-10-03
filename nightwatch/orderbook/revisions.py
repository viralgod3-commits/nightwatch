"""Session-scoped content revisions for immutable order-flow components."""
from __future__ import annotations

from dataclasses import fields
from operator import attrgetter
from uuid import uuid4

from ..models import (
    OrderFlowComponentRevisions, OrderFlowDisplayLevel, OrderFlowSnapshot,
    OrderFlowTradePrint,
)


_LEVEL_VALUES = attrgetter(*(field.name for field in fields(OrderFlowDisplayLevel)))
_PRINT_VALUES = attrgetter(*(field.name for field in fields(OrderFlowTradePrint)))
_AMOUNT_VALUES = attrgetter('price', 'quantity', 'notional')


def _records_equal(current, previous, values):
    # Dataclass equality excludes level age/revision; these getters include the
    # complete schema. Immutable local objects can short-circuit the fallback;
    # consumers never require identity to survive a process boundary.
    if current is previous:
        return True
    return len(current) == len(previous) and all(
        left is right or values(left) == values(right)
        for left, right in zip(current, previous)
    )


def levels_equal(current, previous):
    """Lossless fallback for direct producers without revision metadata."""
    return _records_equal(current, previous, _LEVEL_VALUES)


def prints_equal(current, previous):
    return _records_equal(current, previous, _PRINT_VALUES)


def amount_inputs(snapshot: OrderFlowSnapshot, limit: int | None = None):
    bids, asks = snapshot.bid_levels, snapshot.ask_levels
    if limit is not None:
        bids, asks = bids[:limit], asks[:limit]
    return tuple(_AMOUNT_VALUES(level) for level in bids), tuple(
        _AMOUNT_VALUES(level) for level in asks)


def level_revision_key(snapshot: OrderFlowSnapshot):
    versions = snapshot.component_revisions
    if versions is None:
        return None
    return versions.stream, versions.view, versions.bid_levels, versions.ask_levels


def print_revision_key(snapshot: OrderFlowSnapshot):
    versions = snapshot.component_revisions
    return None if versions is None else (versions.stream, versions.recent_prints)


def amount_revision_key(snapshot: OrderFlowSnapshot):
    versions = snapshot.component_revisions
    return None if versions is None else (versions.stream, versions.view, versions.amounts)


def levels_unchanged(current: OrderFlowSnapshot, previous: OrderFlowSnapshot):
    if current.symbol != previous.symbol:
        return False
    current_key, previous_key = level_revision_key(current), level_revision_key(previous)
    if current_key is not None and previous_key is not None:
        return current_key == previous_key
    return (levels_equal(current.bid_levels, previous.bid_levels)
            and levels_equal(current.ask_levels, previous.ask_levels))


class OrderFlowRevisionTracker:
    """One bounded producer base; update only after final snapshot preparation.

    Versions describe delivered contents, not ingress revisions: timed print
    expiry, outcome corrections, age and normalization changes all participate.
    Reset rotates the stream so counters may never alias another market/session.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._versions = OrderFlowComponentRevisions(uuid4().hex)
        self._bids = self._asks = self._prints = ()
        self._amounts = ((), ())

    def update(self, bids, asks, prints):
        old = self._versions
        # Producer-owned tuples are immutable and rebuilt for every change.
        # Minting a version here is O(1); downstream caches use its explicit
        # session/counter key, never the identity of a deserialized tuple.
        # Equivalent rebuilt tuples may conservatively advance a version.
        bid_changed = bids is not self._bids
        ask_changed = asks is not self._asks
        prints_changed = prints is not self._prints
        amounts_changed = False
        if bid_changed or ask_changed:
            amounts = (tuple(_AMOUNT_VALUES(level) for level in bids),
                       tuple(_AMOUNT_VALUES(level) for level in asks))
            amounts_changed = amounts != self._amounts
            self._amounts = amounts
        if bid_changed or ask_changed or prints_changed:
            self._versions = OrderFlowComponentRevisions(
                old.stream, old.bid_levels + bid_changed, old.ask_levels + ask_changed,
                old.recent_prints + prints_changed, old.amounts + amounts_changed,
            )
        self._bids, self._asks, self._prints = bids, asks, prints
        return self._versions
