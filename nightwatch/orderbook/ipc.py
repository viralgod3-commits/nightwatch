"""Fast, lossless multiprocessing transport for immutable order-flow records."""
from __future__ import annotations

from dataclasses import dataclass, fields
from multiprocessing.reduction import ForkingPickler
from operator import attrgetter
from types import MemberDescriptorType
from typing import Any

from ..models import (
    OrderFlowComponentRevisions,
    OrderFlowDisplayLevel,
    OrderFlowPresentationFrame,
    OrderFlowSnapshot,
    OrderFlowTradePrint,
)
from .tape import TapeFrame, TapePatch, TapeState, TapeStateDelta


class SnapshotSeedRequired(RuntimeError):
    """A derived snapshot cannot be reconstructed from this receiver's base."""


@dataclass(frozen=True, slots=True)
class _FieldDelta:
    mask: int
    # Group rows sharing a changed-field mask. Each row is (key, *values).
    # Age/revision refreshes need one wire object, rather than one per level.
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class _CollectionDelta:
    # Only membership/order changes need an ordering vector. Record corrections
    # retain the receiver's existing order and immutable unaffected objects.
    order: tuple[int | float, ...] | None
    removed: tuple[int | float, ...]
    updates: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class SnapshotDelta:
    """Private pipe representation; public consumers still receive full models."""

    context: tuple[int, str]
    base_sequence: int | None
    scalars: tuple[Any, ...]
    collections: tuple[_CollectionDelta | None, ...]
    frame_values: tuple[Any, ...] | None
    timing: dict[str, Any] | None


_COLLECTIONS = (
    ("bid_levels", OrderFlowDisplayLevel, "price", 1000),
    ("ask_levels", OrderFlowDisplayLevel, "price", 1000),
    ("recent_prints", OrderFlowTradePrint, "sequence", 2048),
)
_COLLECTION_NAMES = frozenset(spec[0] for spec in _COLLECTIONS)
_SCALAR_NAMES = tuple(field.name for field in fields(OrderFlowSnapshot)
                      if field.name not in _COLLECTION_NAMES)
_FRAME_NAMES = tuple(field.name for field in fields(OrderFlowPresentationFrame)
                     if field.name != "snapshot")
_RECORD_NAMES = {
    model: tuple(field.name for field in fields(model))
    for model in (OrderFlowDisplayLevel, OrderFlowTradePrint)
}
_RECORD_VALUES = {model: attrgetter(*names) for model, names in _RECORD_NAMES.items()}


def _wire_constructor(model):
    """Restore the two hot frozen-slot records without repeated name lookup.

    The schema comes exclusively from our declared dataclasses, never a packet.
    Bound slot writers bypass the frozen setter exactly as the generated init
    does. Normal construction remains unchanged. A custom init/post-init or a
    changed slot layout falls back to the public constructor.
    """
    schema = fields(model)
    init = getattr(model.__init__, '__code__', None)
    if (type(model) is not type or model.__new__ is not object.__new__
            or not model.__dataclass_params__.frozen
            or init is None or init.co_filename != '<string>'
            or init.co_argcount != len(schema) + 1 or hasattr(model, '__post_init__')
            or any(not field.init or field.kw_only for field in schema)):
        return model
    names = tuple(field.name for field in schema)
    slots = tuple(vars(model).get(name) for name in names)
    if not all(isinstance(slot, MemberDescriptorType) for slot in slots):
        return model
    name = '_restore_' + model.__name__
    namespace = {'__name__': __name__, '_model': model, '_new': object.__new__,
                 '_writers': tuple(slot.__set__ for slot in slots)}
    arguments = ', '.join(f'_v{index}' for index in range(len(names)))
    source = (f'def {name}({arguments}):\n'
              '    record = _new(_model)\n'
              '    write = _writers\n'
              + ''.join(f'    write[{index}](record, _v{index})\n' for index in range(len(names)))
              + '    return record\n')
    exec(compile(source, '<nightwatch wire constructor>', 'exec'), namespace)
    constructor = namespace[name]
    # Multiprocessing pickle resolves module-level functions by this stable
    # name in both independently imported processes. Arguments stay flat.
    globals()[name] = constructor
    return constructor


_WIRE_CONSTRUCTORS = {model: _wire_constructor(model) for model in _RECORD_NAMES}
_SCALAR_VALUES = attrgetter(*_SCALAR_NAMES)
_FRAME_VALUES = attrgetter(*_FRAME_NAMES)
_SYMBOL_INDEX = _SCALAR_NAMES.index("symbol")
_SEQUENCE_INDEX = _SCALAR_NAMES.index("sequence")


class _CollectionSender:
    __slots__ = ("model", "key_name", "limit", "source", "rows", "order")

    def __init__(self, model, key_name, limit):
        self.model, self.key_name, self.limit = model, key_name, limit
        self.source = None
        self.rows = {}
        self.order = ()

    def staged(self):
        staged = _CollectionSender(self.model, self.key_name, self.limit)
        staged.source, staged.rows, staged.order = self.source, self.rows, self.order
        return staged

    def _change_shape(self, records):
        if len(records) < 64:
            return False, None
        # A bounded, evenly spaced sample selects a transport representation,
        # never which data to retain. Broad book churn uses complete records;
        # broad age/revision refreshes still use compact field corrections.
        values_for = _RECORD_VALUES[self.model]
        names = _RECORD_NAMES[self.model]
        changed, indices = 0, set()
        for index in range(8):
            record = records[index * (len(records) - 1) // 7]
            if type(record) is not self.model:
                return False, None
            previous = self.rows.get(getattr(record, self.key_name))
            if previous is None:
                changed += 1
            elif record is not previous:
                current_values, old_values = values_for(record), values_for(previous)
                if current_values != old_values:
                    changed += 1
                    indices.update(index for index, (value, old)
                                   in enumerate(zip(current_values, old_values)) if value != old)
        if changed < 4:
            return False, None
        if len(indices) >= max(4, len(names) // 5):
            return True, None
        if not indices:
            return False, None
        remaining_values = attrgetter(*(name for index, name in enumerate(names)
                                        if index not in indices))
        candidate_values = attrgetter(*(names[index] for index in sorted(indices)))
        return False, (sum(1 << index for index in indices), candidate_values,
                       remaining_values, len(indices) == 1)

    def encode(self, records, *, seed):
        if not seed and records is self.source:
            return None
        if not isinstance(records, tuple) or len(records) > self.limit:
            raise ValueError("Order-flow collection exceeds its transport contract")
        rows, updates, corrections = {}, [], {}
        values_for = _RECORD_VALUES[self.model]
        dense, shape = (False, None) if seed else self._change_shape(records)
        for record in records:
            if type(record) is not self.model:
                raise TypeError("Unexpected order-flow wire record type")
            key = getattr(record, self.key_name)
            if key in rows:
                raise ValueError("Duplicate order-flow wire record identity")
            rows[key] = record
            previous = None if seed else self.rows.get(key)
            if previous is None:
                updates.append(record)
            elif record is not previous:
                if dense:
                    updates.append(record)
                    continue
                if shape is not None:
                    mask, candidate_values, remaining_values, single = shape
                    # The sample is only a hint. Compare every other field in
                    # C before applying it; outliers use the complete scan.
                    if remaining_values(record) == remaining_values(previous):
                        current = candidate_values(record)
                        if current != candidate_values(previous):
                            # One fixed mask avoids a temporary changed-values
                            # list and per-field Python lookups for every row.
                            # An unchanged hinted field can travel with the
                            # changed ones; its exact value is still preserved.
                            correction = ((key, current) if single
                                          else (key, *current))
                            corrections.setdefault(mask, []).append(correction)
                        continue
                # Dataclass equality ignores age_seconds and analysis_revision.
                # Compare the complete fixed schema so every public field survives.
                current_values, old_values = values_for(record), values_for(previous)
                if current_values == old_values:
                    continue
                mask, changed = 0, []
                for index, (value, old) in enumerate(zip(current_values, old_values)):
                    if value != old:
                        mask |= 1 << index
                        changed.append(value)
                        # Dense changes are cheaper as the original immutable
                        # record; stop inspecting fields once that is certain.
                        if len(changed) >= len(current_values) // 2:
                            break
                if mask:
                    # Sparse corrections avoid retransmitting an entire level
                    # merely because its age, revision or outcome changed.
                    if len(changed) < len(current_values) // 2:
                        corrections.setdefault(mask, []).append((key, *changed))
                    else:
                        updates.append(record)
        updates.extend(_FieldDelta(mask, tuple(values)) for mask, values in corrections.items())
        order = tuple(rows)
        removed = () if seed else tuple(key for key in self.rows if key not in rows)
        order_patch = order if seed or order != self.order else None
        self.source, self.rows, self.order = records, rows, order
        if not seed and not updates and not removed and order_patch is None:
            return None
        return _CollectionDelta(order_patch, removed, tuple(updates))


class _CollectionReceiver:
    __slots__ = ("model", "key_name", "limit", "rows", "order", "records")

    def __init__(self, model, key_name, limit, *, rows=None, order=(), records=()):
        self.model, self.key_name, self.limit = model, key_name, limit
        self.rows = {} if rows is None else rows
        self.order, self.records = order, records

    def apply(self, patch, *, seed):
        if patch is None:
            if seed:
                raise SnapshotSeedRequired("Snapshot seed omitted a collection")
            return self
        if not isinstance(patch, _CollectionDelta):
            raise SnapshotSeedRequired("Invalid snapshot collection patch")
        if seed and patch.order is None:
            raise SnapshotSeedRequired("Snapshot seed omitted collection ordering")
        if len(patch.updates) > self.limit or len(patch.removed) > self.limit:
            raise SnapshotSeedRequired("Snapshot collection patch exceeds its bound")
        rows = {} if seed else self.rows.copy()
        for key in patch.removed:
            if key not in rows:
                raise SnapshotSeedRequired("Snapshot removal has no resident base")
            del rows[key]
        names = _RECORD_NAMES[self.model]
        values_for = _RECORD_VALUES[self.model]
        construct = _WIRE_CONSTRUCTORS[self.model]
        updated_keys = set()
        update_count = 0
        for update in patch.updates:
            if isinstance(update, _FieldDelta):
                update_count += len(update.rows)
                if not 0 < update.mask < (1 << len(names)) or update_count > self.limit:
                    raise SnapshotSeedRequired("Snapshot correction exceeds its schema/bound")
                mask, indices = update.mask, []
                while mask:
                    bit = mask & -mask
                    indices.append(bit.bit_length() - 1)
                    mask ^= bit
                for correction in update.rows:
                    if len(correction) != len(indices) + 1:
                        raise SnapshotSeedRequired("Snapshot correction does not cover its field mask")
                    key = correction[0]
                    if key not in rows or key in updated_keys:
                        raise SnapshotSeedRequired("Snapshot correction has no unique resident base")
                    values = list(values_for(rows[key]))
                    for index, value in zip(indices, correction[1:]):
                        values[index] = value
                    record = construct(*values)
                    if getattr(record, self.key_name) != key:
                        raise SnapshotSeedRequired("Snapshot correction changed a record identity")
                    updated_keys.add(key)
                    rows[key] = record
                continue
            if type(update) is self.model:
                update_count += 1
                if update_count > self.limit:
                    raise SnapshotSeedRequired("Snapshot correction exceeds its bound")
                record = update
                key = getattr(record, self.key_name)
            else:
                raise SnapshotSeedRequired("Invalid snapshot correction type")
            if key in updated_keys:
                raise SnapshotSeedRequired("Snapshot corrected an identity twice")
            updated_keys.add(key)
            rows[key] = record
        if len(rows) > self.limit:
            raise SnapshotSeedRequired("Resident snapshot collection exceeds its bound")
        order = self.order if patch.order is None else patch.order
        if not isinstance(order, tuple) or len(order) > self.limit:
            raise SnapshotSeedRequired("Snapshot ordering exceeds its bound")
        keys = set(order)
        if len(keys) != len(order) or keys != rows.keys():
            raise SnapshotSeedRequired("Snapshot ordering does not cover its resident records")
        records = tuple(rows[key] for key in order)
        return _CollectionReceiver(self.model, self.key_name, self.limit,
                                   rows=rows, order=order, records=records)


def _wire_counts(delta):
    counts = dict(seeds=int(delta.base_sequence is None),
                  deltas=int(delta.base_sequence is not None),
                  level_records=0, print_records=0, field_values=0, removed_records=0)
    for spec, patch in zip(_COLLECTIONS, delta.collections):
        if patch is None:
            continue
        target = "print_records" if spec[0] == "recent_prints" else "level_records"
        counts["removed_records"] += len(patch.removed)
        for update in patch.updates:
            if isinstance(update, _FieldDelta):
                counts[target] += len(update.rows)
                counts["field_values"] += update.mask.bit_count() * len(update.rows)
            else:
                counts[target] += 1
                counts["field_values"] += len(_RECORD_NAMES[spec[1]])
    return counts


class SnapshotEncoder:
    """One bounded source base, owned exclusively by its pipe's sending thread.

    Call only after latest-value queues have selected the payload to send. A
    replaced GUI frame must never advance this base. Raw exchange inputs do not
    use this codec and retain their existing ordered transport.
    """

    def __init__(self):
        self._context = self._sequence = None
        self._collections = [_CollectionSender(*spec[1:]) for spec in _COLLECTIONS]
        self._counts = dict(seeds=0, deltas=0, level_records=0, print_records=0,
                            field_values=0, removed_records=0)

    def encode(self, payload, generation, *, force_seed=False):
        timing = None
        frame = payload
        if (isinstance(payload, tuple) and len(payload) == 2
                and isinstance(payload[0], OrderFlowPresentationFrame)
                and isinstance(payload[1], dict)):
            frame, timing = payload[0], dict(payload[1])
        snapshot = frame.snapshot if isinstance(frame, OrderFlowPresentationFrame) else frame
        if not isinstance(snapshot, OrderFlowSnapshot):
            return payload
        context = (int(generation), snapshot.symbol)
        seed = (force_seed or context != self._context or self._sequence is None
                or snapshot.sequence <= self._sequence)
        # Stage all collections so a rejected third collection cannot silently
        # advance the sender's first two bases without sending their changes.
        senders = [sender.staged() for sender in self._collections]
        patches = tuple(sender.encode(getattr(snapshot, spec[0]), seed=seed)
                        for spec, sender in zip(_COLLECTIONS, senders))
        delta = SnapshotDelta(context, None if seed else self._sequence,
                              _SCALAR_VALUES(snapshot), patches,
                              _FRAME_VALUES(frame) if isinstance(frame, OrderFlowPresentationFrame) else None,
                              timing)
        self._context, self._sequence, self._collections = context, snapshot.sequence, senders
        for key, value in _wire_counts(delta).items():
            self._counts[key] += value
        return delta

    def diagnostic_state(self):
        return dict(self._counts)


class SnapshotDecoder:
    """Reconstruct before presentation coalescing; never expose partial models.

    Applying a message is atomic. Unchanged collections retain tuple identity
    and unchanged records retain object identity across consecutive messages.
    Previous frames are immutable and remain safe for queued GUI consumers.
    """

    def __init__(self):
        self._context = self._sequence = None
        self._collections = [_CollectionReceiver(*spec[1:]) for spec in _COLLECTIONS]
        self._counts = dict(seeds=0, deltas=0, level_records=0, print_records=0,
                            field_values=0, removed_records=0)

    def decode(self, payload, generation):
        if not isinstance(payload, SnapshotDelta):
            return payload
        if (len(payload.scalars) != len(_SCALAR_NAMES)
                or len(payload.collections) != len(_COLLECTIONS)
                or (payload.timing is not None and not isinstance(payload.timing, dict))
                or payload.context != (int(generation), payload.scalars[_SYMBOL_INDEX])):
            raise SnapshotSeedRequired("Snapshot header does not match its stream")
        seed = payload.base_sequence is None
        sequence = payload.scalars[_SEQUENCE_INDEX]
        if not seed and (payload.context != self._context
                         or payload.base_sequence != self._sequence
                         or sequence <= self._sequence):
            raise SnapshotSeedRequired("Snapshot delta does not match the last received frame")
        collections = [receiver.apply(patch, seed=seed)
                       for receiver, patch in zip(self._collections, payload.collections)]
        scalars = dict(zip(_SCALAR_NAMES, payload.scalars))
        scalars.update((spec[0], receiver.records)
                       for spec, receiver in zip(_COLLECTIONS, collections))
        snapshot = OrderFlowSnapshot(**scalars)
        if payload.frame_values is None:
            if payload.timing is not None:
                raise SnapshotSeedRequired("Snapshot timing omitted its presentation frame")
            restored = snapshot
        else:
            if len(payload.frame_values) != len(_FRAME_NAMES):
                raise SnapshotSeedRequired("Snapshot presentation schema does not match")
            frame = OrderFlowPresentationFrame(snapshot, *payload.frame_values)
            restored = frame if payload.timing is None else (frame, payload.timing)
        self._context, self._sequence, self._collections = payload.context, sequence, collections
        for key, value in _wire_counts(payload).items():
            self._counts[key] += value
        return restored

    def diagnostic_state(self):
        return dict(self._counts)


def _constructor_reducer(model):
    schema = fields(model)
    if any(not field.init or field.kw_only for field in schema):
        raise TypeError(f'{model.__name__} requires a positional IPC constructor')
    values = attrgetter(*(field.name for field in schema))
    construct = _WIRE_CONSTRUCTORS.get(model, model)

    def reduce(record):
        # The C-level getter reads the fixed schema once per record. Restore
        # through a fixed-schema constructor. The hot records use bound slot
        # writers; all other immutable models retain their generated init.
        return construct, values(record)

    return reduce


def install_snapshot_reducers() -> None:
    """Optimize Connection.send without changing model or ordinary pickle APIs.

    These immutable wire records have positional generated constructors
    and no post-init side effects. Registration is exact-class, so subclasses
    retain their normal serialization. Every send still has a fresh memo:
    reused mutable command containers must never acquire stale cached state.
    """
    for model in (
        TapeFrame,
        TapePatch,
        TapeState,
        TapeStateDelta,
        OrderFlowComponentRevisions,
        OrderFlowTradePrint,
        OrderFlowDisplayLevel,
        OrderFlowSnapshot,
        OrderFlowPresentationFrame,
        _FieldDelta,
        _CollectionDelta,
        SnapshotDelta,
    ):
        ForkingPickler.register(model, _constructor_reducer(model))
