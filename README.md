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

The order book uses the Prism price ladder: exact prices on a central spine,
buy and sell size on opposing depth rails, a labeled last-trade/midpoint band,
and an explicit spread. Visible-depth totals compare only the rows on screen.
The bottom controls change tick grouping and base/quote size units; the display
menu offers compact rows, cumulative totals on wide panels, reduced motion and
an optional trade tape. Existing color presets and app theme overrides apply.

The new view consumes the same immutable order-flow snapshots and aggregation
as the market backend. Grouping rounds bids down and asks up. Selecting a live
row only prefills its displayed price; it never submits an order. Stale books,
market changes and pending geometry changes disable price selection. Prices and
sizes update immediately; only depth bars interpolate, for at most 100 ms.
Drawing, text caching and transitions run in the existing separate raster
process, with dirty-region updates and leased shared pixels on the GUI side.

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

Leaders loads completed Binance futures and exact-symbol spot histories only
while Leaders or Rotation is open. Rotation shares that loader and fills in as
completed histories arrive. Sectors loads its additional intervals only while
its tab is open and reuses cached hourly data. Returning to Chart cancels pending
research reads, rate-limit retries and queued analysis. An already running
analysis or shared HTTP response may finish; it cannot schedule another batch.

Research validates completed candles before use, requests missing history tails,
and retains valid cached bars through refresh failures. Missing data remains
unavailable in calculations; failed components retry while their page is active.
Verified research bars have separate cache entries from live chart bars and CSV
imports. Existing chart caches are preserved; research rebuilds its verified
cache on the first visit after this upgrade.
Spot availability is tracked separately from futures coverage. Prices and volumes
remain Binance-specific; sector labels use curated membership and exchange tags.
No extra API key or external data subscription is required.

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
sizing is waiting for an account refresh or conversion rate.
Connected trading sessions refresh account data in the background, including
while the Trade tab is open or the panel is hidden. Use **Refresh** in the
trading panel to retry after a connection failure; refreshing never submits an
order. Account and leverage errors remain visible until resolved, and a failed
order-history request does not hide a successfully loaded balance. Position
reductions do not require fresh collateral for a new entry.

The trading panel keeps order entry compact, with the available balance beside
leverage and sizing controls alongside the form in wide panels. Choose an order
type and size, then use **Buy / Long** or **Sell / Short** to submit. **Reduce
only** sizes against the selected position. The form scrolls in short panels.
Spare height below the ticket shows live unrealized P&L and active orders, with
position and order lists stacked in narrow panels or side by side in wide ones.
Current-pair activity appears first; click a position for its controls or use
an order's cancel button. The account view retains fills, balances, and full
position controls.
