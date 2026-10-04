# Order-flow ingress and IPC optimization

Task 5 retains two measured IPC improvements. JSON classification and local
depth reconstruction remain in their existing threads: the sampled parser
process increased latency even when trade messages were batched.

The baseline is `7dfe2da7b26c11bb834d71c1c867808b4d26fadd`, including tasks 1–4.
Profiling used Linux, CPython 3.12.14 and PySide6/Qt 6.11.2. These are offline
CPU and transport measurements, not Windows desktop FPS measurements. No test
suites were run or added.

## Retained changes

* Restore the exact `OrderFlowDisplayLevel` and `OrderFlowTradePrint` frozen
  dataclasses through cached, bound slot writers. The generated restoration
  function is compiled once from the declared internal schema and published
  under a stable module name so spawned workers can resolve it. Pickle
  arguments stay flat. Ordinary construction and ordinary pickle are unchanged.
* For broad updates affecting a few fields, compare the hinted fields through
  one C-level attribute getter and reuse their fixed field mask. This removes
  temporary changed-value lists and per-field Python lookups for each row.
  Every other field is still compared; an outlier uses the complete schema
  scan. A hinted field may be sent unchanged alongside changed fields.

The fast restoration path requires generated positional initialization,
frozen dataclasses, the expected slot descriptors, the default allocator, and
no post-init hook. Other layouts fall back to the model constructor. Field
names cannot collide with restoration locals because generated arguments use
indexed names. Exact record types, collection bounds, identities, field masks,
snapshot generations and atomic receiver commits retain their existing checks.
Socket validation, depth sequencing, subscriptions and trading behavior are
unchanged.

## Measured benefit

The paired profile alternates baseline and modified codecs on the same frames,
with 32 measured updates per case and two IPC hops. Each frame contains 2,048
recent prints. Full nested field comparisons run outside the timed intervals,
including fields excluded from dataclass equality.

| Levels per side | Update | Before median | After median | Reduction |
|---:|---|---:|---:|---:|
| 120 | Age/revision refresh | 4.28 ms | 3.54 ms | 17.2% |
| 120 | Dense level changes | 4.10 ms | 3.26 ms | 20.3% |
| 120 | Refresh with an outlier | 4.23 ms | 3.43 ms | 18.8% |
| 1,000 | Age/revision refresh | 62.96 ms | 50.57 ms | 19.7% |
| 1,000 | Dense level changes | 53.35 ms | 44.70 ms | 16.2% |
| 1,000 | Refresh with an outlier | 40.29 ms | 32.12 ms | 20.3% |

Independent runs also reduced the 1,000-level dense median from 34.88 to
26.84 ms. Absolute times vary with the profiling host and retained heap. These
results support faster dense IPC processing, not a universal speedup for
every update shape. BBO-only updates, sparse corrections, membership changes
and reseeding showed small or inconsistent changes; the original paired
membership result at maximum depth was 3.02 versus 3.31 ms. The raw report
retains those results as well as the focused follow-up measurements. The
96-update follow-up did not reproduce that slowdown: membership at maximum
depth was 3.36 versus 3.21 ms. Small workloads have no consistent measured
improvement and are not counted in the claimed benefit.

## Parser process decision

The candidate uses an actual spawned process, sends raw messages over a pipe
and returns the complete classified packets. Medians from the baseline run:

| Input | Direct parse/classification | Spawned process round trip |
|---|---:|---:|
| One aggregate trade | 0.0041 ms | 0.0498 ms |
| 32 aggregate trades | 0.1087 ms | 0.2188 ms |
| One book ticker | 0.0026 ms | 0.0454 ms |
| 900 mini tickers | 1.0430 ms | 2.2567 ms |
| 900 mark prices, 50 tracked | 0.7292 ms | 1.0697 ms |

Startup took about 446 ms. The candidate measures sequential latency, not
GUI contention under sustained exchange traffic. It did not establish a
benefit and is confined to the profiling script.

For five changed bid and ask rows, the existing depth worker's complete
parse/reconstruct/publish median was 0.048 ms at 120 levels and 0.238 ms at
1,000 levels. Raw depth serialization round trips were 0.024 and 0.207 ms.
There is no measured justification here for a production parser process.

## Verification and reproduction

All eight IPC shapes at both depths preserved every field: BBO-only, sparse
corrections, rolling prints, age refresh, dense levels, sampled-shape outliers,
membership changes and reseeding. JSON packet digests matched the baseline,
and the reconstructed depth books retained every expected row.

The existing offscreen Qt profiles also exercised real spawned analysis and
raster workers, including hidden views, mode/unit changes, scrolling, expired
recent prints, missing-base recovery, same-symbol reset, worker restart and
symbol changes. Tape history and counters were preserved, raster output
reached the latest source revision, and worker sessions reported no errors.

Run the new offline profile from the repository root:

```bash
python scripts/benchmark_orderflow_ingress.py \
  --paired-ref 7dfe2da7b26c11bb834d71c1c867808b4d26fadd \
  --samples 32 --process-samples 32 --output /tmp/ingress-profile.json
```

`NIGHTWATCH_BENCH_SOURCE` selects a separate baseline checkout for independent
runs. `NIGHTWATCH_QT_SITE_PACKAGES` can append an installed Qt package directory
when profiling outside the application's environment. The script does not
connect to an exchange or create orders.

The complete measurements and Qt session records are in
[`orderflow-ingress-benchmark-results.json`](orderflow-ingress-benchmark-results.json).
