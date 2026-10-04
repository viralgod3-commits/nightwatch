# Order-flow tasks 1–4: performance verification

178 focused tests passed in 32.61 seconds, including 80 new parametrized cases. Three historical comparison rounds passed all 10 performance gates. The measurements confirm less processing and GUI work for the workloads these changes target.

## Measured results

Times below are the median of three round medians. Tasks 1–3 use milliseconds per measured update. Task 4 uses total GUI milliseconds across 300 frames and two tables.

| Task | Workload | Before | After | Time reduction |
| --- | --- | ---: | ---: | ---: |
| 1: incremental IPC | BBO only, 1,000 levels per side, two hops | 39.4747 | 0.1815 | 99.54% |
| 1: incremental IPC | Sparse corrections, same depth | 39.5342 | 3.1259 | 92.09% |
| 1: incremental IPC | Rolling prints, same depth | 40.9878 | 2.0651 | 94.96% |
| 1: incremental IPC | Age refresh, same depth | 41.5822 | 35.2300 | 15.28% |
| 1: incremental IPC | Dense level changes, same depth | 39.8435 | 34.0350 | 14.58% |
| 2: revisions/cache | Age-only amount-width work | 0.9817 | 0.0066 | 99.33% |
| 2: revisions/cache | Age-only cache work | 11.5078 | 10.3801 | 9.80% |
| 2: revisions/cache | Reseeded cache work | 8.1109 | 2.3073 | 71.55% |
| 2: revisions/cache | Reseeded amount-width work | 0.9519 | 0.0071 | 99.25% |
| 3: regional raster | Moving-row buffer synchronization at 2x DPR | 1.28221 | 0.27087 | 78.87% |
| 3: regional raster | Moving-row native GUI blit at 2x DPR | 2.21777 | 0.09756 | 95.60% |
| 4: trade tape | Appending trades | 93.4129 | 42.2813 | 54.74% |
| 4: trade tape | Outcome corrections | 77.2062 | 19.3302 | 74.96% |
| 4: trade tape | BBO-only frames | 1.3845 | 0 | 100% |
| 4: trade tape | Hidden views | 77.0926 | 0 | 100% |

BBO and sparse IPC cases transferred 99.93% and 99.87% fewer bytes. The moving-row raster case copied and submitted 97.71% fewer bytes/pixels at 2x DPR. After the initial tape seed, append cases formatted 500 numeric rows per table; outcome-only and hidden cases formatted none. Hidden views received no tape frames.

## Correctness checks

The new tests verify all immutable fields through both IPC hops; coalescing; unchanged-object reuse; unsampled sparse outliers; atomic rejection; missing-base and generation recovery; cache invalidation for amounts, age, outcomes, signed flow and sessions; and metadata-free compatibility.

Pixel checks cover 54 damage/lease cases across DPR 1, 1.1, 1.25, 1.5, 1.75 and 2, plus four unknown/stale lease cases. They compare every resulting pixel with the full native blit, verify held buffers remain immutable, and check bounded damage regions.

Tape checks cover accepted trades beyond the rolling 2,048-print source window, corrections after eviction, stalled and hidden views, checkpoints and actual spawned workers. The tape subprocess completes all 14 recovery phases.

Tests also found and fixed two regressions:

- Changing units without a new frame could display a retained tiny quantity as zero. The model now measures retained rows once on a unit switch. The regression failed against the original task 4 commit and passed with the fix.
- A valid head removal or reorder could incorrectly request a new tape seed. The model now reconstructs the affected prefix from retained rows; invalid patches still fail validation.

The focused suite includes IPC, mailbox, tape and market-panel coverage. Existing tape tests now wait for acknowledged asynchronous delivery and use the seed/delta API; raster assertions use revision-bearing leases.

Run the added correctness checks from the repository root with the project's dependencies installed:

```sh
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_orderflow_optimizations.py
```

The existing benchmark helpers used by the verification include `scripts/benchmark_orderflow_ipc.py`, `scripts/benchmark_orderflow_regions.py`, `scripts/profile_orderflow_tape.py` and `scripts/profile_orderflow_regions.py`.

## Comparison method and limits

Comparisons used the exact historical task commits, three sequential rounds, 32 measured updates per case, and alternating before/after process order. Task 1 alternated full and incremental serialization within each frame after eight warmups. Timing ran on Linux, CPython 3.12.14, PySide6/Qt 6.11.2 and NumPy 2.3.5 with offscreen Qt. Historical references and selected round medians are in [the results summary](orderflow-tasks-1-4-verification-results.json).

The improvements are workload-specific. Task 2 BBO and outcome cache work rose from 1.283 to 1.3882 ms and from 1.5721 to 1.7338 ms; near-amount cache work rose about 2%. Full-churn GUI blits remain full blits and did not consistently improve. These results measure component processing and native GUI blits, not total application CPU or desktop FPS.

The full repository suite is not certified passing. Protection-test failures and fake-task teardown errors were reproduced on the pre-task-1 baseline. A broader GUI run stalled in Qt typography; the isolated ticket-layout case passed on both baseline and current source, but the suite-wide stall remains unresolved. Windows 10 with CPython 3.14.6 was not benchmarked.

The execution workspace disconnected after verification. This publication preserves the tested changes and selected results recovered from completed tool output. The JSON summary is explicitly not the full raw report, which remains in that workspace.
