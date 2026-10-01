# Watchlist, order-overlay and GPU diagnostics patch

Originally prepared against GitHub `main` at
`369ef6d9164f23a4d113fbdf51a2bfce78c3e8b3`. Integrated onto
`6c10ba9fc4d877f2043cd98885a027c807b86445`, preserving the later market-header
spacing and independent trade-tape units, layout and highlighting changes.

## Implemented recommendations

**1. Watchlist.** Numeric sidebar columns retain Qt's natural content widths,
but use Fixed sizing during writes. Each dirty column gets one native sizing
pass at the end of the refresh. Shorter values can shrink columns; theme,
typography, style and DPI changes invalidate sizing. Selection, scrolling,
icons, groups and numeric formatting retain their existing behavior.

The five-minute move detector maintains bounded history and median caches for
active symbols. Appends and replacements reuse earlier calculations; pruning
repairs affected references. New or irregular histories rebuild safely. The
existing reference window, thresholds, exclusion of the newest historical
move, cooldown and signal lifetime remain unchanged. No new worker or IPC
overhead is introduced.

**2. Order overlays.** A scoped transaction shares plot, price-axis and layout
geometry across rails within one commit. The cache is discarded at commit end,
including exceptions, and invalidated when axis reservation changes. A bounded
price-text measurement cache follows font-metrics replacement. Price formatting
is reused within each placement. Qt's coordinate mapping and integer rounding
remain the source of every pixel anchor. Armed mouse-hit masks, transparency and
cursor properties are updated only when necessary, preserving the exact
eleven-pixel hit strip and all existing controls, animation and trading paths.

**4. GPU verification.** Persistent per-batch counters distinguish successful
native draws, requested-path failures, onscreen CPU fallback and intentionally
selected software rendering. Driver vendor/renderer/version and realized
context format are queried once per context lifetime. Context destruction,
recovery, setting changes and image exports have separate handling. Retiring an
older context cannot erase verification of a newer context. Metadata and failure
reason caches are bounded. The existing Developer diagnostics and F3 output
show requested versus observed backend, fallback reasons and each chart's
candle/volume batches. These counters measure batch draws, not display frames
or GPU utilization. Ordinary diagnostics reads do not query the driver or
request extra paints.

## Original archive measurements

Same interpreter and harness, sequential baseline/patched runs: Linux Python
3.12.14, Qt 6.11.2, offscreen software rendering and fallback system fonts.
Watchlists use 360 historical samples per coin and change all displayed numeric
values on every measured update. Charts have 5,800 candles; overlay timings
isolate the overlay commit, with camera mutation outside the timed section.

| Case | Samples | Before median | After median | Before p95 | After p95 |
|---|---:|---:|---:|---:|---:|
| 50-coin full refresh | 12 | 104.428 ms | 2.991 ms | 109.952 ms | 6.933 ms |
| 200-coin full refresh | 12 | 1600.988 ms | 12.284 ms | 1669.422 ms | 28.825 ms |
| 10-order overlay during pan | 500 | 1.685 ms | 1.037 ms | 2.541 ms | 1.338 ms |
| 20-order overlay during pan | 500 | 3.005 ms | 1.928 ms | 3.862 ms | 2.927 ms |

The 200-coin stress refresh is about 130 times faster; the 20-order pan overlay
uses about 36% less time. Full 200-coin updates and history pruning can still
exceed a 144 Hz frame budget of 6.94 ms on this host. These are callback timings,
not Windows FPS forecasts. Native GPU throughput, refresh cadence and actual
end-to-end latency require the target PC benchmark. No indicator-worker,
swap-interval, frame-clock or GPU-rendering geometry changes are included.

## Validation and reproduction

The archive records 167 passing tests, including existing trading, navigation
and shutdown regressions.
New tests cover detector boundaries, irregular histories, appends, replacements,
pruning and cooldowns; Qt natural sizing, shrink/grow and environment changes;
overlay anchors and hit regions; and mocked GPU context failures and recovery.
Five baseline/patched captures are pixel-identical: 50/200-coin sidebars and
linear, zoomed and logarithmic charts. All 60 rail geometry snapshots match.
Compilation and patch applicability are checked separately.

Integration on Python 3.14.6 / Qt 6.11.2 passed 93 focused UI and diagnostics
checks, plus startup-format and visible-chart F3-output checks. Sequential
before/after runs on the integration base reproduced five pixel-identical
captures and 60 identical rail geometries. Median callback timings were
107.392 to 3.051 ms for 50 coins, 1600.725 to 12.400 ms for 200 coins, and
2.964 to 1.704 ms for 20-order overlays during pan. These remain offscreen
software measurements; native Windows GPU performance is unmeasured.

Install the existing development requirements, then run:

```powershell
python -m pytest -q tests/test_watchlist_performance.py tests/test_overlay_performance.py tests/test_gpu_diagnostics.py tests/test_trade_tape.py tests/test_shell_smoke.py
python scripts/benchmark_ui_performance.py --output ui-benchmark.json
```

The offline benchmark clears Binance credentials and creates no gateway or live
orders. `NIGHTWATCH_BENCH_SOURCE` can select an earlier checkout for the same
harness. Raw measurements are in `ui-performance-benchmark-results.json`.

For your Windows benchmark, launch with `--diagnostics`, reproduce the same
1–2 charts, 2–3 indicators, watchlist and orders, and press F3 for the existing
30-second capture while panning and zooming. Keep `[F3]`, `[F3 timing]` and the
new `[F3 GPU]` lines. Check each visible chart's observed backend and native
draws, including fallback reasons. An unpainted or recreated context remains
unverified; a disabled native renderer is an intentional software selection.
The existing presentation metrics are paint cadence, not monitor scanout FPS.
