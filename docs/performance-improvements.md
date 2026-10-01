# Performance improvements

Baseline: GitHub `main` at `e6411c658f3636dfce86be771374f2939fa3e6b0`.

This patch addresses the measured list-update, research-transfer and presentation
backlog bottlenecks from the architecture review. It preserves the current Qt
interface, research formulas, recent tape styling and order-execution behavior.

## Changes

- Account lists retain row widgets by unique order/fill or position identity.
  Native numeric-rank sorting and incremental insertion/removal preserve
  selection, signal wiring and embedded editor state. Unchanged rows skip
  updates only with an explicit matching immutable context. Ambiguous identities
  or incompatible factories retain the complete rebuild fallback.
- Leaders retain items by symbol and update changed cell data only. Native row
  sorting moves complete rows. Header setup and unchanged metric snapshots are
  reused. Tagless assets bypass unnecessary classification regular expressions.
- Sectors reuse table cells and sparkline widgets; unchanged sparkline inputs do
  not request another paint.
- Research histories stay resident in a single affinity worker. Replaced mapping
  components produce changed-bar/deletion patches; changed list components are
  replaced. Filters, sorts and live sector quotes reuse completed-bar features.
  GUI snapshots still support existing history/detail/loading workflows.
- Research and chart-indicator affinity lanes share the existing total worker
  budget. Research cannot occupy the dedicated indicator lane on larger hosts.
  An idle or running broken process pool is retired; a fresh worker requests a
  full history seed before using deltas.
- A GUI-affine mailbox bounds derived order-flow presentation wakeups. Snapshots,
  microstructure and diagnostics use latest-value slots. Ordered raw depth/trade
  ingress and failure notifications retain their semantics. Each drain admits
  at most three presentation values and 32 failures. Runtime destruction retires
  the mailbox on its GUI thread.

## Measured comparison

Same harness, synthetic completed-bar datasets, warm calls, 12 samples per case,
Linux CPython 3.12.14 / Qt 6.11.2, offscreen Qt and system-font fallback. Each
research case has 200 completed bars per instrument, including BTC.

| Case | Before median | After median |
|---|---:|---:|
| 200-coin unchanged Leaders GUI commit | 25.55 ms | 2.04 ms |
| 200-coin Leaders ranking reorder | 28.89 ms | 2.84 ms |
| 200-coin warm filter request through process IPC | 248.20 ms | 2.35 ms |
| 200-coin warm input serialization | 66.97 ms | 0.006 ms |
| 100 unchanged order rows reordered | 72.10 ms | 1.72 ms |
| 200 unchanged order rows reordered | 144.29 ms | 3.40 ms |
| 200 order rows reordered, ten changed payloads | 158.44 ms | 4.23 ms |
| 200 order rows reordered, every payload changed | 163.84 ms | 14.30 ms |
| Unchanged sector table | 0.66 ms | 0.14 ms |

The 200-coin warm input shrank from **3,301,097 bytes to 3,031 bytes**. A paused
GUI receiving 1,000 analytics bursts posts one initial mailbox wakeup and later
receives only the newest value in each of three presentation streams.

These timings exclude monitor presentation, selected-coin details and live
exchange traffic. The synthetic tagless symbols benefit from classification
short-circuiting; production gains depend on the actual universe and workload.
Initial seeding still transfers full histories. Large changes to every card
remain slower than a high-refresh frame budget. These results establish relative
improvement, not a maximum-FPS or latency guarantee.

[Raw measurements, sample values and maxima](performance-benchmark-results.json).

## Validation and reproduction

**132 tests passed** locally, including native row/editor/selection reuse,
research equivalence across filtering/replay/gaps/revisions, spawned-process
restart/reseed, bounded mailbox races and real QThread affinity/destruction.
Existing navigation/shutdown smoke coverage remains green. Compilation and
`git diff --check` passed. No exchange credentials or live orders were used.

Install `requirements-dev.txt`, then run:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
QT_QPA_PLATFORM=offscreen python scripts/benchmark_performance.py --output benchmark.json
```

On Windows, set `QT_QPA_PLATFORM=offscreen` in the environment before running
the Python commands. Set `NIGHTWATCH_BENCH_SOURCE` to a baseline checkout to run
the same harness against that checkout. The harness clears Binance credentials.

## Remaining review work

Independent execution ownership, canonical account reconciliation, validation
changes, execution feed demand, complete transport queue bounds, DOM hang/restart
supervision, startup/storage ownership and release packaging remain separate
migrations. This patch does not claim to complete those recommendations. Their
state/lifecycle changes require dedicated acceptance work beyond the measured
performance optimizations implemented here. Native GPU, target-monitor/DPI,
long-session and live-exchange performance still need measurement.
