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

Chart preparation workers build an immutable multiresolution candle index for
wide zooms and live aggregate updates (about 2 MB for 250,000 contiguous candles).
Older history is fetched and adopted during held pans. The order-book rendering
process retains shared pixel-buffer capacity across panel resizes, growing it
only when necessary while keeping the displayed buffer leased until painting.

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

Collateral-percentage shortcuts require fresh, asset-specific available balance
in confirmed Single-Asset Mode. Multi-Assets Mode requires manual quantity
sizing; account-wide USD-equivalent collateral is not a specific asset balance.
