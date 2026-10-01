"""Revisioned, worker-resident completed histories for research workspaces.

Callers replace completed history containers rather than mutating published
candles. Only changed components/bars cross IPC; the first request and recovery
after a worker restart seed the full dataset. Inputs never contain Qt objects.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
import uuid

from .chart.analysis import run_analysis

HISTORY_FIELDS = (
    "series", "spot_series", "hourly", "spot_hourly", "futures_15m", "spot_15m", "daily",
)
_MISSING = object()
_worker_histories = OrderedDict()
_MAX_WORKSPACES = 8


def history_update(histories, previous=None):
    """Build idempotent patches on a background thread, outside GUI callbacks."""
    full = previous is None
    previous = previous or {}
    updates = {}
    for field, components in histories.items():
        old_components = previous.get(field, {})
        replace, patches = {}, {}
        removed = tuple(symbol for symbol in old_components if symbol not in components)
        for symbol, rows in components.items():
            old = old_components.get(symbol, _MISSING)
            if rows is old:
                continue
            if isinstance(rows, dict) and isinstance(old, dict) and not full:
                changed = {stamp: candle for stamp, candle in rows.items()
                           if old.get(stamp, _MISSING) != candle}
                deleted = tuple(stamp for stamp in old if stamp not in rows)
                if changed or deleted:
                    patches[symbol] = (changed, deleted)
            elif full or rows != old:
                replace[symbol] = rows
        updates[field] = {"replace": replace, "patch": patches, "remove": removed}
    return {"full": full, "fields": updates}


def _copy_component(rows):
    return dict(rows) if isinstance(rows, dict) else list(rows)


def _feature_view(function, view):
    # Filtering/ranking and live quote labels do not change completed-bar facts.
    ignored = {
        "_prepare_leaders": {"query", "sort_mode", "sort_descending",
                             "category_filter_value", "state_filter", "tickers", "valid_symbols"},
        "_prepare_sectors": {"tickers"},
    }.get(function.__name__, set())
    return {key: value for key, value in view.items() if key not in ignored}


def resident_analysis(token, function, update, view):
    """Runs only in the affinity worker; a missing cache requests a full seed."""
    entry = _worker_histories.pop(token, None)
    if entry is None and not update["full"]:
        return {"__history_cache_miss__": True}
    if entry is None or update["full"]:
        entry = {"histories": {}, "result": None, "view": None, "function": None}
    histories = entry["histories"]
    changed = bool(update["full"])
    for field, patch in update["fields"].items():
        components = histories.setdefault(field, {})
        for symbol in patch["remove"]:
            components.pop(symbol, None)
        for symbol, rows in patch["replace"].items():
            components[symbol] = _copy_component(rows)
        for symbol, (bars, removed) in patch["patch"].items():
            rows = components[symbol]
            for stamp in removed:
                rows.pop(stamp, None)
            rows.update(bars)
        changed |= bool(patch["remove"] or patch["replace"] or patch["patch"])

    # Remember history even if feature preparation fails. Reapplying a patch is
    # safe; failed results never become the next feature-cache baseline.
    _worker_histories[token] = entry
    while len(_worker_histories) > _MAX_WORKSPACES:
        _worker_histories.popitem(last=False)
    state = {**view, **histories}
    context = _feature_view(function, view)
    reusable = (not changed and entry["result"] is not None
                and entry["function"] is function and context == entry["view"])
    if reusable and function.__name__ in {"_prepare_leaders", "_prepare_sectors"}:
        result = function(state, cached=entry["result"])
    elif reusable:
        result = entry["result"]
    else:
        # A partially applied patch must not reuse old features on retry.
        entry["result"] = None
        result = function(state)
    entry.update(result=result, view=context, function=function)
    return result


class WorkspaceHistory:
    """One workspace's acknowledged dataset; no GUI work or process handles."""

    def __init__(self):
        self.token = uuid.uuid4().hex
        self._previous = None
        self._lock = threading.Lock()

    def analyze(self, function, state):
        histories = {name: state[name] for name in HISTORY_FIELDS if name in state}
        view = {name: value for name, value in state.items() if name not in histories}
        with self._lock:
            previous = self._previous
            if previous is not None and previous.keys() != histories.keys():
                previous = None
            patch = history_update(histories, previous)
            result = run_analysis(resident_analysis, self.token, function, patch, view,
                                  cache_affinity="research")
            if result.get("__history_cache_miss__"):
                patch = history_update(histories)
                result = run_analysis(resident_analysis, self.token, function, patch, view,
                                      cache_affinity="research")
            self._previous = histories
            return result
