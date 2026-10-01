"""Offline spawned-worker parity and disposable-worker restart acceptance."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

from concurrent.futures.process import BrokenProcessPool
from nightwatch.models import Candle
from nightwatch.leadership import HOUR, _prepare_leaders
from nightwatch.research import WorkspaceHistory
from nightwatch.chart import analysis


def main():
    end = 200 * HOUR
    histories = {symbol: {stamp: Candle((stamp - HOUR) / 1000, 100, 102, 99, 101, 10, 1000)
                          for stamp in range(HOUR, end + 1, HOUR)}
                 for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}
    state = dict(symbols=("ETHUSDT", "SOLUSDT"), series=histories, spot_series={},
                 cursor=end, hours=24, query="", sort_mode="state", categories={},
                 tickers={}, valid_symbols=frozenset())
    client = WorkspaceHistory()
    try:
        assert client.analyze(_prepare_leaders, state) == _prepare_leaders(state)
        filtered = {**state, "query": "ETH", "sort_mode": "pair"}
        assert client.analyze(_prepare_leaders, filtered) == _prepare_leaders(filtered)
        # Killing a disposable analytics owner must not strand its cached lane.
        executor, = analysis._executors.values()
        for process in tuple(executor._processes.values()):
            process.terminate()
            process.join(5)
            assert not process.is_alive()
        try:
            client.analyze(_prepare_leaders, state)
        except BrokenProcessPool:
            pass
        else:
            raise AssertionError("Killed worker did not report a broken lane")
        assert not analysis._executors
        # Parent retains its acknowledged revision; the new worker requests a
        # full seed instead of computing from absent/stale histories.
        assert client.analyze(_prepare_leaders, filtered) == _prepare_leaders(filtered)
        print("Spawned research parity, compact reuse, and restart recovery passed")
    finally:
        analysis.shutdown_analysis()


if __name__ == "__main__":
    main()
