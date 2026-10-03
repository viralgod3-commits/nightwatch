# Incremental order-flow snapshot transport

Task 1 replaces complete snapshot serialization at both process boundaries:
analysis worker to application relay, and application relay to DOM raster worker.
Prepared on `main` at `11b269e70a54acf8b7b53a91d31837661aaeed4e`, including the
latest native-DPI typography changes.

The first message seeds a bounded resident book and print history. Subsequent
messages carry current scalar metrics, inserted or changed records, removals,
and ordering changes. Sparse corrections transfer only changed fields; rows
sharing a field mask use one compact correction group. A bounded sample selects
complete changed records for broad multi-field churn, avoiding the cost of
diffing and reconstructing every field in that case. The sample never selects
which data to preserve: outliers to a sparse field pattern receive a complete
comparison. All fields, including `age_seconds`, `analysis_revision`, print
outcomes, outcome direction and timing metadata, retain their existing values.

Public consumers receive the same immutable `OrderFlowSnapshot` and
`OrderFlowPresentationFrame` models. Unchanged collections keep tuple identity;
unchanged levels and prints keep record identity. Each codec holds one bounded
base: at most 1,000 bid levels, 1,000 ask levels and 2,048 prints. No per-frame
history of bases accumulates. Encoding and reconstruction commit atomically.

## Coalescing and recovery

The analysis relay decodes every received snapshot before the GUI mailbox may
replace a derived frame. The DOM relay encodes only after its latest-value queue
selects the frame actually sent. Consequently, skipped GUI frames do not skip a
required wire base. Snapshot sequence numbers need not be consecutive; the
packet names the last transmitted base explicitly.

Market/generation changes, sequence resets and new workers seed a new base.
A receiver missing its base requests one bounded complete-state retry. The
analysis worker supplies its latest derived frame; the DOM relay resends its
latest transmitted derived frame. Neither path replays depth, BBO, trade or
execution commands. A failed retry enters the existing worker failure path.
The renderer never adopts a partially reconstructed snapshot. Pausing or hiding
the DOM does not advance its sender base for unsent frames.

Existing transport diagnostics expose seeds, deltas, changed level/print counts,
field values and removals. Raw market inputs retain the existing ordered path.

## Measurements

Linux CPython 3.12.14, 24 updates per workload, 2,048 retained prints. The
baseline uses the existing optimized constructor pickle reducers. Measurements
include both codecs, serialization, reconstruction and re-encoding. Initial
seeds are excluded from update timings and reported separately in the JSON.
Every reconstructed frame was compared field by field, including fields omitted
by dataclass equality. All workloads preserved every field.

| Workload | Levels per side | DOM receives | Full median | Incremental median | Bytes saved |
|---|---:|---|---:|---:|---:|
| BBO only | 120 | Every third frame | 5.919 ms | 0.220 ms | 99.786% |
| Sparse level and outcome corrections | 120 | Every third frame | 5.385 ms | 0.974 ms | 99.595% |
| Rolling print history | 120 | Every third frame | 5.794 ms | 0.850 ms | 95.705% |
| Every row's age/revision changes | 120 | Every third frame | 5.824 ms | 2.327 ms | 97.213% |
| Every level changes multiple fields | 120 | Every third frame | 5.734 ms | 1.910 ms | 72.876% |
| BBO only | 1,000 | Every frame | 33.668 ms | 0.553 ms | 99.929% |
| Sparse level and outcome corrections | 1,000 | Every frame | 29.345 ms | 2.569 ms | 99.866% |
| Rolling print history | 1,000 | Every frame | 30.506 ms | 1.635 ms | 98.698% |
| Every row's age/revision changes | 1,000 | Every frame | 31.365 ms | 28.805 ms | 93.020% |
| Every level changes multiple fields | 1,000 | Every frame | 35.327 ms | 28.627 ms | 27.914% |

An additional offline 3.2-second session used the actual spawned Qt analysis and
raster workers, BBO updates, depth input and a market reset. It produced 95
analysis snapshots and 89 DOM transfers, with two seeds on each hop and no
worker errors. The DOM's lower transfer count reflects presentation coalescing.

These are transport measurements, not Windows FPS forecasts. Dense maximum-depth
updates still require substantial reconstruction work. This patch does not move
ingress parsing, change aggregation/formatting, or replace tape ingestion.
No test suite was run and no exchange connection or order submission was made.

Reproduce the synthetic measurements:

```sh
python scripts/benchmark_orderflow_ipc.py --levels 120 --samples 24 --coalesce-every 3 --output ipc-120.json
python scripts/benchmark_orderflow_ipc.py --levels 1000 --samples 24 --coalesce-every 1 --output ipc-1000.json
```

Raw measurements and the process-session counters are in
`orderflow-ipc-benchmark-results.json`.
