"""Research deltas must preserve completed-bar results through reuse/recovery."""
import pickle
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6 import QtCore

from nightwatch import research
from nightwatch.models import Candle
from nightwatch.leadership import (
    HOUR, LeadershipTimelineWidget, SectorOverviewWidget, DETAIL_ROLE,
    _prepare_leaders, _prepare_rotation, _prepare_sectors,
    _SECTOR_OVERVIEW_SECTOR_ORDER, _SECTOR_OVERVIEW_SECTOR_COLORS,
)


def history(end, count=200, step=HOUR, price=100):
    return {stamp: Candle((stamp - step) / 1000, price, price + 2, price - 1,
                          price + (index % 7) / 10, 10, 1000 + index)
            for index, stamp in enumerate(range(end - (count - 1) * step, end + 1, step))}


def leader_state(count=6):
    end = 600 * HOUR
    symbols = [f"COIN{index}USDT" for index in range(count)]
    return dict(symbols=tuple(symbols), series={symbol: history(end, price=100 + index)
                for index, symbol in enumerate(["BTCUSDT", *symbols])},
                spot_series={}, cursor=end, hours=24, categories={},
                query="", sort_mode="state", sort_descending=True,
                valid_symbols=frozenset(symbols), tickers={})


def _summary(state):
    return {symbol: sum(candle.close for candle in rows.values())
            for symbol, rows in state["series"].items()}


@pytest.fixture(autouse=True)
def clear_worker_cache():
    research._worker_histories.clear()
    yield
    research._worker_histories.clear()


def test_bar_patch_is_compact_and_does_not_modify_published_histories():
    state = leader_state(100)
    previous = {"series": state["series"]}
    changed = dict(state["series"])
    rows = dict(changed["COIN0USDT"])
    removed = min(rows)
    del rows[removed]
    stamp = state["cursor"] + HOUR
    rows[stamp] = Candle((stamp - HOUR) / 1000, 100, 102, 99, 101, 10, 1000)
    changed["COIN0USDT"] = rows
    patch = research.history_update({"series": changed}, previous)
    full = research.history_update({"series": changed})
    assert patch["fields"]["series"]["replace"] == {}
    assert patch["fields"]["series"]["patch"] == {"COIN0USDT": ({stamp: rows[stamp]}, (removed,))}
    assert len(pickle.dumps(patch)) < len(pickle.dumps(full)) / 100

    research.resident_analysis("bars", _summary, research.history_update(previous), {})
    assert research.resident_analysis("bars", _summary, patch, {}) == _summary({"series": changed})
    assert removed in previous["series"]["COIN0USDT"]
    assert stamp not in previous["series"]["COIN0USDT"]


def test_leaders_filters_reuse_facts_and_match_fresh_analysis(monkeypatch):
    state = leader_state()
    client = research.WorkspaceHistory()
    monkeypatch.setattr(research, "run_analysis", lambda fn, *args, **kw: fn(*args))
    initial = client.analyze(_prepare_leaders, state)
    assert initial == _prepare_leaders(state)
    filtered = {**state, "query": "COIN1", "sort_mode": "pair", "sort_descending": False,
                "tickers": {"BTCUSDT": {"q": "900000"}, "COIN1USDT": {"q": "600000"}}}
    expected = _prepare_leaders(filtered)
    import nightwatch.leadership as leadership
    with monkeypatch.context() as check:
        check.setattr(leadership, "_metrics", lambda *a, **k: pytest.fail("Filters recomputed completed-bar metrics"))
        assert client.analyze(_prepare_leaders, filtered) == expected


@pytest.mark.parametrize("prepare", [_prepare_leaders, _prepare_rotation])
def test_replay_span_benchmark_revision_gaps_and_removal_match_uncached(prepare, monkeypatch):
    state = leader_state()
    client = research.WorkspaceHistory()
    monkeypatch.setattr(research, "run_analysis", lambda fn, *args, **kw: fn(*args))
    assert client.analyze(prepare, state) == prepare(state)
    revised = {**state, "series": dict(state["series"]), "cursor": state["cursor"] - HOUR, "hours": 4}
    btc = dict(revised["series"]["BTCUSDT"])
    stamp = revised["cursor"]
    candle = btc[stamp]
    btc[stamp] = Candle(candle.time, 100, 104, 99, 103, 10, 2000)
    revised["series"]["BTCUSDT"] = btc
    missing = dict(revised["series"]["COIN0USDT"])
    missing.pop(stamp - HOUR)
    revised["series"]["COIN0USDT"] = missing
    revised["series"].pop("COIN5USDT")
    revised["symbols"] = revised["symbols"][:-1]
    revised["categories"] = {"COIN1USDT": "AI"}
    assert client.analyze(prepare, revised) == prepare(revised)


def test_sector_live_prices_reuse_history_facts(monkeypatch):
    end = 600 * HOUR
    symbols = ("ETHUSDT",)
    state = dict(symbols=symbols, timeframe="4h", end_hour=end, end_15m=end,
                 hourly={symbol: history(end) for symbol in ("BTCUSDT", *symbols)},
                 futures_15m={}, spot_15m={}, spot_hourly={}, sectors={"ETHUSDT": "AI"},
                 daily={"ETHUSDT": list(history(25 * 24 * HOUR, 25, 24 * HOUR).values())},
                 tickers={"ETHUSDT": {"c": "90"}})
    client = research.WorkspaceHistory()
    monkeypatch.setattr(research, "run_analysis", lambda fn, *args, **kw: fn(*args))
    assert client.analyze(_prepare_sectors, state) == _prepare_sectors(state)
    changed = {**state, "tickers": {"ETHUSDT": {"c": "120"}}}
    expected = _prepare_sectors(changed)
    import nightwatch.leadership as leadership
    monkeypatch.setattr(leadership._SectorAnalysis, "_all_sector_metrics",
                        lambda *a: pytest.fail("Live quotes recomputed sector histories"))
    actual = client.analyze(_prepare_sectors, changed)
    assert actual == expected
    assert actual["above"] == (1, 1)


def test_worker_cache_miss_recovers_with_full_seed_and_client_scopes(monkeypatch):
    calls = []
    def run(fn, *args, **kwargs):
        calls.append(args[2]["full"])
        return fn(*args)
    monkeypatch.setattr(research, "run_analysis", run)
    state = leader_state(2)
    first, second = research.WorkspaceHistory(), research.WorkspaceHistory()
    assert first.analyze(_summary, state) == _summary(state)
    second_state = {**state, "series": {"OTHER": history(state["cursor"], price=500)}}
    assert second.analyze(_summary, second_state) == _summary(second_state)
    assert first.analyze(_summary, state) == _summary(state)
    research._worker_histories.clear()  # A fresh process/eviction has no local dataset.
    assert first.analyze(_summary, state) == _summary(state)
    assert calls == [True, True, False, False, True]


def test_failed_feature_preparation_does_not_commit_an_acknowledgement(monkeypatch):
    state = leader_state(2)
    client = research.WorkspaceHistory()
    def fail(*args, **kwargs):
        raise RuntimeError("worker failed")
    monkeypatch.setattr(research, "run_analysis", fail)
    with pytest.raises(RuntimeError):
        client.analyze(_summary, state)
    assert client._previous is None
    monkeypatch.setattr(research, "run_analysis", lambda fn, *args, **kw: fn(*args))
    assert client.analyze(_summary, state) == _summary(state)


def test_spawned_research_worker_recovers_after_disposable_process_failure():
    script = Path(__file__).with_name("research_spawn.py")
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "restart recovery passed" in result.stdout


@pytest.mark.parametrize("count", [1, 2, 3, 6])
def test_analysis_affinity_lanes_respect_total_worker_budget(monkeypatch, count):
    from nightwatch.chart import analysis
    class Future:
        def result(self):
            return "ok"
    class Executor:
        def __init__(self, *, max_workers, **kw):
            self.workers = max_workers
        def submit(self, *args):
            return Future()
    monkeypatch.setattr(analysis, "_executors", {})
    monkeypatch.setattr(analysis, "_closed", False)
    monkeypatch.setattr(analysis, "analysis_worker_count", lambda: count)
    monkeypatch.setattr(analysis, "ProcessPoolExecutor", Executor)
    for affinity in (False, True, "research"):
        assert analysis.run_analysis(_summary, {}, cache_affinity=affinity) == "ok"
    assert sum(executor.workers for executor in analysis._executors.values()) <= count


def test_leaders_reorder_retains_items_selection_and_delegate_data(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    state = leader_state(12)
    widget.symbols = list(state["symbols"])
    for name in ("_render_facts", "_render_changes", "_schedule_details"):
        monkeypatch.setattr(widget, name, lambda *a: None)
    prepared = _prepare_leaders(state)
    widget._render(prepared)
    selected = prepared["ordered"][4]
    widget.selected = selected
    items = {symbol: widget.table.item(row, 1) for row, symbol in enumerate(prepared["ordered"])}
    reversed_order = list(reversed(prepared["ordered"]))
    widget._render({**prepared, "ordered": reversed_order})
    assert widget.row_symbols == reversed_order
    for row, symbol in enumerate(reversed_order):
        assert widget.table.item(row, 1) is items[symbol]
        assert widget.table.item(row, 0).text() == str(row + 1)
        assert widget.table.item(row, 10).data(DETAIL_ROLE)["spark"] == prepared["sparks"][symbol]
    assert widget.table.item(widget.table.currentRow(), 1).data(QtCore.Qt.ItemDataRole.UserRole) == selected
    assert not widget.grab().isNull()
    widget.shutdown()
    widget.deleteLater()


def test_leaders_incremental_rows_update_metrics_prices_and_membership(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    state = leader_state(3)
    widget.symbols = list(state["symbols"])
    for name in ("_render_facts", "_render_changes", "_schedule_details"):
        monkeypatch.setattr(widget, name, lambda *a: None)
    prepared = _prepare_leaders(state)
    widget._render(prepared)
    symbol = "COIN0USDT"
    index = prepared["ordered"].index(symbol)
    item = widget.table.item(index, 1)
    prepared["metrics"][symbol]["usd1"] = 9.0
    prepared["metrics"][symbol]["state"] = "Leading"
    widget.tickers = {symbol: {"c": "456"}}
    smaller = [symbol, "COIN1USDT"]
    widget._render({**prepared, "ordered": smaller})
    assert widget.table.rowCount() == 2
    assert widget.table.item(0, 1) is item
    assert widget.table.item(0, 3).text() == "456.00"
    assert widget.table.item(0, 4).text() == "+9.0%"
    assert widget.table.item(0, 8).text() == "Leading"
    assert widget.table.item(0, 4).data(DETAIL_ROLE)["foreground"] == widget.theme["green"]
    widget.shutdown()
    widget.deleteLater()


def test_sector_table_reuses_cells_and_sparklines_when_rank_changes(qapp):
    widget = SectorOverviewWidget({})
    chosen = _SECTOR_OVERVIEW_SECTOR_ORDER[:2]
    metrics = {sector: {"members": int(sector in chosen), "performance": index,
                        "trend": [1, None, 3], "outperformers": 1, "covered": 2}
               for index, sector in enumerate(_SECTOR_OVERVIEW_SECTOR_ORDER)}
    performances = {frame: {sector: 1 for sector in metrics} for frame in ("15m", "1h", "4h", "1d")}
    widget._render_table(metrics, performances)
    cells = [widget.table.item(row, 0) for row in range(2)]
    sparks = [widget.table.cellWidget(row, 7) for row in range(2)]
    metrics[chosen[0]]["performance"] = 100
    performances["1h"][chosen[0]] = -2
    widget._render_table(metrics, performances)
    for row in range(2):
        sector = widget.table.item(row, 0).text()
        assert widget.table.item(row, 0) is cells[row]
        assert widget.table.cellWidget(row, 7) is sparks[row]
        assert sparks[row].values == [1, None, 3]
        assert sparks[row].color.name() == _SECTOR_OVERVIEW_SECTOR_COLORS[sector].lower()
    assert widget.table.item(0, 2).text() == "-2.0%"
    assert not widget.grab().isNull()
    widget.shutdown()
    widget.deleteLater()
