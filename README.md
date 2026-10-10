# Nightwatch

Native Qt terminal for Binance USD-M perpetuals, with trading charts, order flow,
Leaders, Sectors, and a Rotation scanner.

## Run

Use standard CPython and an isolated environment. The primary entry point
supports Windows process spawning.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python nightwatch_futures.py --testnet --symbol BTCUSDT
```

On Linux, activate with `source .venv/bin/activate`. Omit `--testnet` to use
mainnet market data. Configure matching Binance Futures credentials in the app
or through `BINANCE_API_KEY` and `BINANCE_API_SECRET`. Public research does not
require trading credentials. Trading starts disarmed.

Ubuntu/Debian headless checks also require Qt's native EGL/OpenGL runtime:

```bash
sudo apt-get install -y libegl1 libopengl0 libxkbcommon0
```

A native Linux desktop also needs its Qt platform/graphics dependencies; see
[Qt for Linux requirements](https://doc.qt.io/qt-6/linux-requirements.html).

Optional `fonts/` and `assets/` directories belong beside
`nightwatch_futures.py`, at the project root. Resource lookup is independent of
the working directory. A checkout without bundled fonts uses system UI and
fixed-width numeric fonts.

Set `COINGECKO_API_KEY` before launch to authenticate optional CoinGecko icon
lookups. Credentials are read from the environment rather than bundled in source.

Native numerical libraries use one thread per process by default to keep
analytics processes from oversubscribing the CPU. Set
`NIGHTWATCH_NUMERIC_THREADS` before launch to override this after measuring your
workload. Windows high process priority is opt-in through
`NIGHTWATCH_HIGH_PRIORITY=1`.

Spawned workers share a CPU budget based on process affinity, physical cores and
Linux cgroup quotas. On hosts with at least four available physical cores and
CPU capacity, workers exclude one physical core, including its SMT siblings, so
the GUI can use it without competing with child processes. Live order-flow and
DOM workers keep their inherited priority; analysis workers use lower priority.
The analysis pool reserves capacity for the GUI and both live workers, with a
minimum of one analysis worker. CPU topology or affinity restrictions fall back
to inherited placement. Trading transport stays in the main process.

Chart preparation workers build an immutable multiresolution candle index for
wide zooms and live aggregate updates (about 2 MB for 250,000 contiguous candles).
Older history is fetched and adopted during held pans. The order-book rendering
process retains shared pixel-buffer capacity across panel resizes, growing it
only when necessary while keeping the displayed buffer leased until painting.

During chart gestures, panel geometry and secondary updates join the active
chart's presentation clock before chart preparation. Moving between chart panes
hands that clock over to the newly manipulated pane. Panel-only resizes retain
display pacing without forcing an unchanged chart to repaint. Cached resize
previews isolate control input until the live layout and focus are restored.

Measure chart pan/zoom and watchlist resizing with all four panels, ten pairs,
and no indicators using `python tools/benchmark_interactions.py`. The offline
benchmark records actual paint/composition cadence, slowest 1% frame intervals,
and input latency. See [benchmark instructions](tools/benchmark_interactions.md)
for matching before/after runs and target GPU/display validation.

## Local state and recovery

Application data uses Qt's platform-specific application data directory.
Mainnet and testnet caches use separate databases: `nightwatch-mainnet.sqlite3`
and `nightwatch-testnet.sqlite3`. The old `nightwatch.sqlite3` is preserved and
is not automatically imported, because it may contain history from both venues.

Accepted entries retain their protection intent in the order journal. Startup
queries saved client IDs and restores monitoring before allowing new entries.
An unknown or absent protection outcome keeps new entries blocked; use
**Reconcile** after inspecting the matching account's orders and positions.
Recovery does not resend missing orders. Plans from versions that never saved
accepted protection intent cannot be reconstructed automatically.

Collateral-percentage shortcuts use fresh available balance for the confirmed
account mode. Single-Asset Mode uses the contract's margin-asset balance;
Multi-Assets Mode uses the shared USD collateral pool and Binance's current
asset conversion rates, including pending orders across margin assets.
The account summary displays the balance in its currency even when collateral
sizing is waiting for an account update or conversion rate.
Connected trading sessions update account data in the background, including
while the Trade tab is open or the panel is hidden. Use the update icon in the
trading panel to retry after a connection failure; updating never submits an
order. Manual orders validate account data and confirmed leverage before sending.
A failed order-history request does not hide a successfully loaded balance. Position
reductions do not require fresh collateral for a new entry.

The trading panel separates **Open** and **Reduce**. Choose the order type and
size, then use **Buy / Long** or **Sell / Short**. **Ctrl+Enter** submits the
current side. Arrow keys switch the selected side without submitting an order.
The form scrolls in short panels and switches to columns in wide panels.
Reduce mode sizes against the selected position. API credentials are configured
from the Trading menu; account data can be updated with the circular arrow icon.
