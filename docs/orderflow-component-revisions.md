# Order-flow component revisions

Priority 2 replaces process-sensitive tuple-identity cache keys with explicit
component revisions. Prepared on `main` at
`a4aa2affc9b3dc88521837d1e90e2d961b2c1843`, retaining the incremental transport
and latest typography changes.

The analyzer assigns a session identifier and independent bid, ask, print and
amount versions after preparing the final snapshot components. Every reset,
including a reset of the same symbol, starts a new session. Immutable producer
tuples allow constant-time level/print version assignment; equivalent rebuilt
tuples may conservatively advance a version. Receivers use explicit session and
counter keys rather than requiring identities to survive serialization.

Versions accompany the existing immutable public models through both worker
pipes. Snapshot producers that omit the optional metadata use content checks;
a missing revision is never treated as version zero. Those checks include every
level/print field, including age and analysis revision. The tracker retains one
bounded producer base and one ordered price/quantity/notional projection.

## Reuse and invalidation

- Aggregation and prepared-row reuse compare level versions. Aggregation's
  view key includes its tick/grouping transformation; context and readiness
  changes invalidate reuse. Visible-row content checks remain available when
  changed components leave the visible subset unchanged.
- Tape ingestion compares print versions. New prints, history expiry and
  outcome changes advance the version after the analyzer constructs its current
  print window. Threshold/status handling remains independent of that key.
  Session changes preserve existing tape history under a new epoch, preventing
  old and new sequence numbers from aliasing.
- Width sizing compares amount versions independently of age and analytical
  changes. When that version changes, it checks the first 64 displayed rows on
  each side before formatting/measuring them. Far-depth changes and aggregation
  changes with identical measured amounts can therefore reuse the width. Font,
  units, symbol and reset invalidation remain explicit.

BBO, scalar metrics, feed health, state latches, hover metadata and market anchors
continue updating. No market input, price or execution state is interpolated or
discarded. Cache counters are included in the existing DOM diagnostics and
order-book performance state.

The review also found an existing bucket-cache omission: signed trade notional
was used by aggregation but absent from its source signature. A signed-flow-only
update could retain the old sum. Signed flow now invalidates that cache.

## Offline measurements

Baseline and patched runs used the same harness, sequentially, on Linux CPython
3.12.14 / Qt 6.11.2, with 24 updates per workload, 2,048 prints, five-tick
aggregation and two independent tape widgets. Both incremental transport codecs
run before the measured cache work. The table measures tape ingestion plus DOM
aggregation/preparation; amount-width time is also captured separately.

| Workload | Levels/side | Cache work before | Cache work after | Width before | Width after |
|---|---:|---:|---:|---:|---:|
| BBO only | 120 | 0.948 ms | 0.876 ms | 0.002 ms | 0.003 ms |
| Age/revision refresh | 120 | 2.033 ms | 1.729 ms | 0.321 ms | 0.003 ms |
| Near amount changes | 120 | 2.287 ms | 2.325 ms | 0.319 ms | 0.345 ms |
| Far amount changes | 120 | 1.772 ms | 1.672 ms | 0.333 ms | 0.325 ms |
| Print outcomes | 120 | 1.047 ms | 1.081 ms | 0.002 ms | 0.004 ms |
| Complete-state reseeding | 120 | 1.992 ms | 0.961 ms | 0.336 ms | 0.004 ms |
| Signed-flow-only changes | 120 | 1.499 ms | 1.674 ms | 0.302 ms | 0.003 ms |
| BBO only | 1000 | 0.936 ms | 0.922 ms | 0.001 ms | 0.003 ms |
| Age/revision refresh | 1000 | 10.822 ms | 7.636 ms | 0.840 ms | 0.004 ms |
| Near amount changes | 1000 | 11.756 ms | 6.390 ms | 0.913 ms | 0.810 ms |
| Far amount changes | 1000 | 6.698 ms | 3.927 ms | 1.045 ms | 0.024 ms |
| Print outcomes | 1000 | 1.039 ms | 1.065 ms | 0.001 ms | 0.003 ms |
| Complete-state reseeding | 1000 | 6.291 ms | 1.280 ms | 0.849 ms | 0.003 ms |
| Signed-flow-only changes | 1000 | 4.977 ms | 4.266 ms | 0.831 ms | 0.003 ms |

Ordinary BBO and outcome-only updates already benefited from priority 1's
resident tuple reuse. The revision checks add small bookkeeping costs there;
the strongest savings are redundant width work and complete-state recovery.
Actual amount changes still require measurement and changed levels still require
analysis. Raw medians, p95/p99 timings, formatting counts and producer-version
costs are in `orderflow-revisions-benchmark-results.json`.

The recovery workload deliberately reseeds both pipes on every update to expose
cache behavior after identity changes; it is a stress scenario. Every patched
display field matched uncached aggregation in every workload. The baseline
failed that comparison for signed-flow-only updates, reproducing the fixed bug.

An additional offline session used the actual spawned Qt analysis/raster workers,
BBO/depth/trade inputs and a market reset. It reported no worker errors. Each
tape retained all 20 supplied trades across the two sessions; diagnostics showed
revision reuse and only two width measurements in the sampled renderer frames.

These figures exclude painting, kernel pipe timing, actual display FPS and Windows
GPU behavior. The synthetic harness times revision assignment separately; its
extra snapshot copy for attaching metadata is outside that timing. Production
constructs the annotated snapshot once. No test suite was run and no exchange
connection, gateway or live order was created.

Reproduce with the existing application dependencies:

```sh
python scripts/benchmark_orderflow_revisions.py --levels 120 --samples 24 --output revisions-120.json
python scripts/benchmark_orderflow_revisions.py --levels 1000 --samples 24 --output revisions-1000.json
```

Set `NIGHTWATCH_BENCH_SOURCE` to an earlier checkout for a baseline run with the
same harness.
