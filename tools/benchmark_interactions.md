Run the interaction benchmark from the repository root on the desktop machine whose performance you want to measure:

```bash
python tools/benchmark_interactions.py --duration 30 --history 250000 --live-candle-hz 4 --output work/before.json --label before
# Apply the change, then rerun with the same display, size and arguments.
python tools/benchmark_interactions.py --duration 30 --history 250000 --live-candle-hz 4 --output work/after.json --label after
```

It constructs the production `MainWindow` and chart widgets with all four Balanced panels visible, ten watchlist pairs, and indicators off. No credentials, real market feeds, databases, journals, or orders are used. User settings and application storage are isolated in a temporary directory. The fixtures contain deterministic candles at BTC, ETH and SHIB price scales and representative exchange ticks (0.10, 0.01 and 0.00000001), ten updating tickers, 120 depth levels per side, four trades per update, and a display-only position. Depth and trade quantities are normalized to quote notional so the tape remains populated at every price scale. The default market update cadence is 20 Hz; this is a synthetic workload, not a recorded Binance session.

`--live-candle-hz` defaults to zero, preserving static-current-candle comparisons. Set it to four for target-machine qualification: a separate timer calls production `update_kline` after snapshot adoption with the same last-candle time, increasing event stamps, bounded OHLC/volume updates and `x=False`. It never appends or closes a row. Per-case counters assert that updates were adopted inside the gesture and row counts stayed fixed.

Each symbol runs a real held-mouse pan, wheel zoom, historical pan, ~1,000-visible-candle pan, watchlist height resize, and right-rail width resize. The default duration is four seconds per case, with 240 requested pointer ticks/sec and 100,000 candles. Use `--history 250000 --duration 3` to reproduce short cloud diagnostics. `--app-root` imports an immutable application snapshot while running this same harness; metadata records the resolved paths and SHA-256 hashes of loaded app files and asserts they all belong to that source root. Native numeric pools are bounded before NumPy is imported, like the primary launcher. Resize cases use opaque resizing on the actual splitters to measure repeated visible geometry changes. A 250 ms release observation captures the first committed chart paint and release paints separately. Watchlist columns are fixed/automatic; rail widening exercises expansion.

Optional `--cases history_traverse` repeatedly swipes the historical chart, releases, repositions the unheld cursor and presses again every 0.4 seconds. It never changes the chart range directly during measurement. Rendered-window transitions are timestamped at paint completion, and the case requires a newly prepared residency window to be painted inside the gesture. Six seconds suffices for the software workload; use the longer native qualification duration to cross the larger GPU residency window. Warm `deep_pan` usually stays inside an already prepared residency window, so it does not measure that boundary cost.

JSON includes actual chart paint completion timestamps summarized as FPS, chart paint durations, watchlist paint entries, Qt `frameSwapped` composition events following new content, input-to-next-chart-paint latency, renderer/Qt/CPU/display metadata, navigation-range and resize geometry changes, and existing preparation/upload diagnostics. Every case asserts four active panels and ten rows, an X-range delta above one second for pans, and changed visible watchlist geometry for content resizes. Fixture observations record actual received snapshots, depth levels, tape rows, position count and the disarmed gateway; the benchmark asserts a populated depth/tape/position fixture arrived. For the process-rendered DOM, visible depth is observed from the adopted raster frame's prices and sequence; the GUI's inherited snapshot fields are intentionally unused. Direct renderers are observed through their displayed snapshot.

`average_fps` is the primary surface completion count divided by gesture wall time. A valid GL viewport with `frameSwapped` uses `qt_compositions` as primary, including an honest zero when no swaps occur; paint sources remain diagnostics. Raster cases use chart paint completions, or watchlist paint entries for watchlist interactions; an unchanged chart can correctly have zero paints. `interval_rate_fps` reports reciprocal mean inter-frame time. `one_percent_low_fps` is 1000 divided by the mean of the slowest `ceil(1% * interval_count)` intervals. P99/max intervals and boundary gaps remain visible. Missed-slot estimates round to the nearest refresh period (`floor(interval/budget + 0.5) - 1`, bounded at zero), so small timer jitter does not imply a physical presentation miss. Chart input latency includes coalesced events and measures dispatch to paint completion, not physical mouse-to-display latency.

Keep the native display's refresh rate, compositor, pixel dimensions, scaling, GPU driver, power state and background load consistent between runs. `--refresh-hz` specifies an explicit acceptance target and separately records the actual Qt screen rate; it does not change the display. The benchmark records Qt composition completion, not verified physical scanout. GPU adequacy and uninterrupted refresh-rate presentation require a run on the actual target GPU/display and examination of the tails for every interaction.

Use at least 30 seconds per case for target-machine qualification. The four-second default and three-second cloud comparisons are short diagnostics: their 1% lows can be determined by very few slow frames, and are sensitive to scheduling variation. Preserve the same power state, resolution, refresh rate and compositor between longer before/after captures.

Linux captures also record cgroup CPU quota and `cpu.stat` start/end/deltas per run and per gesture. These counters cover the entire cgroup, so other container work can affect them. Use repeated interleaved before/after captures to investigate regressions and compare the existing diagnostic timing totals and P99 values alongside FPS. After measurement, the benchmark services deferred window close and transport cleanup for up to five seconds before leaving Qt.

On a machine without a native display/OpenGL context, use a software diagnostic run:

```bash
QT_QPA_PLATFORM=offscreen python tools/benchmark_interactions.py --raster \
  --history 250000 --duration 3 --output work/offscreen.json
```

Offscreen/raster results measure Qt software paint throughput and preparation costs. They do not validate a GTX 1080, hardware composition, display synchronization, or refresh-rate FPS. The JSON deliberately leaves `target_hardware_validated=false`; hardware qualification needs independent machine/display verification.

The 24-case software comparison used the immutable `b1cb948` main snapshot versus the rebased candidate, 1920×1080, 250,000 candles, six seconds per case, one numeric thread and a static current candle. The container has a two-CPU quota and no qualified target GPU/display. All cases across BTC, ETH and SHIB passed population, source-root and gesture assertions; neither run recorded CPU throttling. Every traversal painted three newly prepared residency windows during the gesture. Medians across the three symbols were:

| Interaction | Average FPS before → after | 1% low FPS before → after |
| --- | ---: | ---: |
| History traversal | 110.8 → 111.7 | 28.8 → 41.4 |
| Zoom | 74.3 → 76.0 | 37.9 → 43.0 |
| Watchlist height resize | 72.6 → 68.8 | 29.4 → 29.4 |
| Rail width resize | 31.7 → 30.2 | 22.3 → 18.6 |

Results remain mixed: traversal lows improved on all three symbols, while ETH rail low declined 24.8→15.2 FPS and SHIB zoom/rail lows declined 42.5→36.4 and 21.0→18.6 FPS. These short software captures do not establish a universal FPS improvement or the native-refresh/no-lows target. Raw paired results and timing totals/P99 values are in local `work/final-before.json`, `work/final-after.json` and `work/final-comparison.json`.

One additional six-second BTC traversal pair enabled `--live-candle-hz 4`: average/1% low changed from 96.6/21.8 to 112.7/40.3 FPS. Each version adopted 24 current-candle updates without changing the 250,000-row count and painted three residency commits during the gesture, with no CPU throttling. Maximum live-batch size changed 125,184→1. This is a single software pair, not target-hardware qualification; raw results are `work/live-before.json` and `work/live-after.json`.

Traversal maximum live-batch size fell from 125,183 rows to one. Separate numerical preparation measurements fell from 28.134 to 0.086 ms; these are preparation microbenchmarks, not displayed FPS. Separate ten-pair watchlist callback measurements reduced resize content scans from 2,398 to zero and mean callback time from 0.821 to 0.678 ms, while preserving column widths. The width and live-tail bounds are concrete work reductions; broader frame-rate qualification still requires the native display run above.

Earlier captures against `84c5fab` had noisy three-second tails, including an apparent ETH warm historical-pan loss. Six-second component isolation in forward and reverse order found full-candidate median average 127.4 FPS versus original 127.6 FPS, with mixed tails and component spread; it did not establish a chart regression. Those older captures predate upstream resize changes in `b1cb948` and are historical diagnostics rather than the final merged comparison.
