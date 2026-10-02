"""Fast, lossless multiprocessing transport for immutable order-flow records."""
from __future__ import annotations

from dataclasses import fields
from multiprocessing.reduction import ForkingPickler
from operator import attrgetter

from ..models import (
    OrderFlowDisplayLevel,
    OrderFlowPresentationFrame,
    OrderFlowSnapshot,
    OrderFlowTradePrint,
)


def _constructor_reducer(model):
    schema = fields(model)
    if any(not field.init or field.kw_only for field in schema):
        raise TypeError(f'{model.__name__} requires a positional IPC constructor')
    values = attrgetter(*(field.name for field in schema))

    def reduce(record):
        # The C-level getter reads the fixed schema once per record. Restore
        # through the generated constructor, bypassing dataclasses' repeated
        # fields() scans and generic frozen-slot __setstate__ loop.
        return model, values(record)

    return reduce


def install_snapshot_reducers() -> None:
    """Optimize Connection.send without changing model or ordinary pickle APIs.

    These four immutable wire records have positional generated constructors
    and no post-init side effects. Registration is exact-class, so subclasses
    retain their normal serialization. Every send still has a fresh memo:
    reused mutable command containers must never acquire stale cached state.
    """
    for model in (
        OrderFlowTradePrint,
        OrderFlowDisplayLevel,
        OrderFlowSnapshot,
        OrderFlowPresentationFrame,
    ):
        ForkingPickler.register(model, _constructor_reducer(model))
