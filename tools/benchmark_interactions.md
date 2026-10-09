Run the interaction benchmark from the repository root on the desktop machine whose performance you want to measure:

```bash
python tools/benchmark_interactions.py --duration 30 --history 250000 --live-candle-hz 4 --output work/before.json --label before
# Apply the change, then rerun with the same display, size and arguments.
python tools/benchmark_interactions.py --duration 30 --history 250000 --live-candle-hz 4 --output work/after.json --label after
```

It constructs the production `MainWindow` and chart widgets with all four Balanced panels visible, ten watchlist pairs, and indicators off. No credentials, real market feeds, databases, journals, or orders are used. User settings and application storage are isolated in a temporary directory. The fixtures contain deterministic candles at BTC, ETH and SHIB price scales and representative exchange ticks (0.10, 0.01 and 0.00000001), ten updating tickers, 120 depth levels per side, four trades per update, and a display-only position. Depth and trade quantities are normalized to quote notional so the tape remains populated at every price scale. The default market update cadence is 20 Hz; this is a synthetic workload, not a recorded Binance session.

The harness suppresses `BinanceRest`'s constructor-time server-clock synchronization and rejects shared HTTP transport requests. Any attempted HTTP request fails the run even if application error handling catches it. This keeps startup offline as well as the measured gestures.

`--live-candle-hz` defaults to zero, preserving static-current-candle comparisons. Set it to four for target-machine qualification: a separate timer calls production `update_kline` after snapshot adoption with the same last-candle time, increasing event stamps, bounded OHLC/volume updates and `x=False`. It never appends or closes a row. Per-case counters assert that updates were adopted inside the gesture and row counts stayed fixed.

Each symbol runs a real held-mouse pan, wheel zoom, historical pan, ~1,000-visible-candle pan, watchlist height resize, and right-rail width resize. The default duration is four seconds per case, with 240 requested pointer ticks/sec and 100,000 candles. Use `--history 250000 --duration 3` to reproduce short cloud diagnostics. `--app-root` imports an immutable application snapshot while running this same harness; metadata records the resolved paths and SHA-256 hashes of loaded app files and asserts they all belong to that source root. Native numeric pools are bounded before NumPy is imported, like the primary launcher. Resize cases use opaque resizing on the actual splitters to measure repeated visible geometry changes. Panel shells now paint scaled press-time snapshots while their live controls retain their original geometry; on release, controls adopt the final geometry and resume painting. Controls are never hidden or reparented for the preview, so market models, worker subscriptions and focus remain active. A 250 ms release observation captures the first committed chart paint and release paints separately. Watchlist columns are fixed/automatic; rail widening exercises expansion.

Optional `--cases history_traverse` repeatedly swipes the historical chart, releases, repositions the unheld cursor and presses again every 0.4 seconds. It never changes the chart range directly during measurement. Rendered-window transitions are timestamped at paint completion, and the case requires a newly prepared residency window to be painted inside the gesture. Six seconds suffices for the software workload; use the longer native qualification duration to cross the larger GPU residency window. Warm `deep_pan` usually stays inside an already prepared residency window, so it does not measure that boundary cost.

Schema 2 JSON includes actual chart paint completion timestamps summarized as FPS, chart paint durations, watchlist paint entries, visible watchlist surface paints (table entries or preview completions), per-panel preview completions, raw Qt swaps and Qt `frameSwapped` composition events following new chart content, input-to-next-chart-paint latency, renderer/Qt/CPU/display metadata, navigation-range and resize geometry changes, and existing preparation/upload diagnostics. Every case asserts four active panels and ten rows, an X-range delta above one second for pans, and changed visible watchlist geometry for content resizes. Fixture observations record actual received snapshots, depth levels, tape rows, position count and the disarmed gateway; the benchmark asserts a populated depth/tape/position fixture arrived. For the process-rendered DOM, visible depth is observed from the adopted raster frame's prices and sequence; the GUI's inherited snapshot fields are intentionally unused. Direct renderers are observed through their displayed snapshot.

`average_fps` is the primary surface completion count divided by gesture wall time. Chart interactions and rail resizing use `qt_compositions` as primary when a valid GL viewport exists, including an honest zero when no swaps occur; raster runs use chart paint completions. Watchlist resizing always uses `watchlist_surface_paints`, because an isolated chart swap does not present the raster watchlist. That field counts table paint entries on older app roots and preview paint completions while a preview is visible. It measures painting, not raster-window composition or scanout. Raw table paints caused by snapshot capture are excluded from the visible surface count. An unchanged chart can correctly have zero paints. `qt_swap_events` separately records all swaps, including swaps without fresh chart content. `interval_rate_fps` reports reciprocal mean inter-frame time. `one_percent_low_fps` is 1000 divided by the mean of the slowest `ceil(1% * interval_count)` intervals. P99/max intervals and boundary gaps remain visible. Missed-slot estimates round to the nearest refresh period (`floor(interval/budget + 0.5) - 1`, bounded at zero), so small timer jitter does not imply a physical presentation miss. Chart input latency includes coalesced events and measures dispatch to paint completion, not physical mouse-to-display latency.

The benchmark also records each panel's shell/content size before, during and after the drag, plus content resize/layout-request counts separated from release. Preview runs assert zero held-drag content resizes, final control geometry, restored updates/layouts, and continuing market snapshots. Release timing starts before dispatching the mouse release, so synchronous final layout is included. First-frame delay includes the one-time preview capture cost. Regular chart frames are requested by geometry or data changes; dragging an unrelated panel no longer makes the chart paint continuously.

GPU composition isolation is available as the **Isolated GPU chart composition (restart)** Developer testing option. It is off by default: native chart ancestors eliminate unrelated raster-sibling composition, but the additional native surface regressed navigation throughput under Mesa llvmpipe. Qualify it on the target GPU before enabling it. The benchmark isolates settings and accepts `--isolate-chart-gl` to select the option for a single run:

```bash
python tools/benchmark_interactions.py --duration 30 --history 250000 --live-candle-hz 4 --output work/shared-gl.json
python tools/benchmark_interactions.py --isolate-chart-gl --duration 30 --history 250000 --live-candle-hz 4 --output work/isolated-gl.json
```

Check `renderer_state.composition_isolated` and the observed driver in `renderer_state.render_path.last_context`. A requested flag alone does not establish an active native surface or a hardware renderer. `llvmpipe` is software OpenGL even when native shaders and `frameSwapped` work. The native chart is parented before first exposure; startup, layout changes and GL resource destruction are part of the validation, not just steady-state FPS.

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

The 2026-10-09 panel-preview comparison used the session's immutable starting checkout in `work/baseline` and the default candidate, with the same schema-2 harness, 1920×1080, 250,000 candles, four seconds per gesture, 20 Hz market fixtures and 4 Hz mutable-candle updates. All twelve cases per version passed the fixture/geometry assertions. Native composition isolation was off for this raster comparison. Medians across BTC, ETH and SHIB were:

| Interaction | Average paint FPS before → after | 1% low before → after |
| --- | ---: | ---: |
| pan | 101.7 → 94.8 | 47.6 → 33.5 |
| zoom | 59.1 → 51.8 | 33.4 → 32.4 |
| watchlist_resize | 63.5 → 59.4 | 28.3 → 32.8 |
| rail_resize | 27.5 → 48.4 | 18.8 → 21.5 |

Held-drag control resize events fell from 2,566 to 0; resized controls adopted final geometry on release, and market snapshots/current-candle updates continued. Rail resizing improved on all three symbols. Pan and zoom results were mixed, with lower median throughput in this short pair; these captures do not establish a general navigation improvement. Raw captures are `work/final-raster-before.json` and `work/final-raster-after.json`; their loaded-source hashes identify the measured application versions.

Separate OpenGL captures exercised native shaders using Mesa 25.0.7 llvmpipe (OpenGL 4.5 Core Profile), under Xvfb without a hardware GPU. A small idle-chart correctness check observed 50 raster sibling repaints causing 50 chart swaps with shared composition and zero chart swaps with a native chart ancestor. In the earlier three-second BTC application captures, enabling isolation plus the resize changes reduced pan throughput from 27.5 to 17.7 compositions/sec on llvmpipe (`work/gl-before.json`, `work/gl-ondemand.json`). These short runs are why isolation remains opt-in. They do not qualify target hardware. Subsequent non-benchmark checks covered primary/auxiliary native surfaces, layout switches, hide/show, resize, shaders, raster fallback and teardown. Panel checks covered 1×/2× scaling, live values, focus, cancellation and restoration of prior layout/update flags.

The subsequent chart-rendering change uses these existing captures as its reference; no new benchmark or checks were run for that change at the user's request. The saved BTC raster pan trace spent about 4.6 ms painting, 1.8 ms preparing navigation and 2.3 ms between submission and paint, exceeding a 144 Hz frame's 6.94 ms budget. The saved zoom trace spent about 9.1 ms rebuilding the bar cache. These are CPU/event-loop costs, not evidence of insufficient target-GPU throughput.

Rendering now searches an immutable contiguous timestamp index before processing bars. Native draws adjust instance-attribute offsets into the retained history VBO and submit only the visible rows, with conservative slot/wick/glow padding; scissoring alone previously still processed all 4,096–8,192 resident rows in every style pass. CPU geometry preparation uses the same bounded range. Raster image-cache allocation, combined bounds and overlap preparation are deferred until geometry is reused, avoiding an immediately discarded image during continuous zoom. Raster completion posts the next preparation directly without an intervening zero-delay timer. The harness's existing performance-counter collection records `gl.draw.<batch>_visible_rows` and `gl.draw.<batch>_resident_rows` for later comparison. These changes have no new measured FPS result or 144 Hz qualification yet.
