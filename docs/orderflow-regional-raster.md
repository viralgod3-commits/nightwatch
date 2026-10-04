# Order-flow regional raster presentation

Priority 3 preserves the ladder renderer's dirty regions through shared-buffer
synchronization and GUI painting. Based on `main` at
`44cbcab1e24bdf6fc9bfe01b4ab1c1cfd1c8ae75`, retaining priorities 1 and 2 and the
native-DPI typography changes.

Previously, every buffer swap copied the entire preceding image even when only
one row changed. Adopting any completed image then requested a full-widget GUI
paint. The renderer's existing row/chrome diff was therefore lost after raster
drawing.

## Buffer ownership and damage

- Two shared images remain the only pixel buffers. The GUI lease now identifies
  allocation, slot and completed-frame revision. The worker never writes the
  leased slot, including when its revision cannot be validated.
- Each slot retains a bounded union of all paints it missed. When a stale slot
  is reused, the worker synchronizes that debt from the latest complete image.
  Pixels guaranteed to be overwritten by the upcoming paint are excluded.
  Repeated updates to the same row can consequently require no synchronization.
- Logical damage converts outward to physical pixels. Guaranteed overwrite
  coverage converts inward, so fractional-DPI boundary pixels are never omitted
  based on an overestimated paint clip. Synchronization uses fresh zero-copy
  DPR=1 wrappers and source composition, without resampling or detaching images.
- A completed frame carries integer damage rectangles relative to the exact
  lease supplied with that request. Damage accumulates across unadopted frames;
  it is not merely the most recent paint's rectangle. Unknown or old revisions
  cause a full repaint. Allocation, size and DPI changes also require a full
  initial render.
- The renderer republishes any completed but unacknowledged image even when a
  subsequent request paints nothing. This fixes a recovery gap where the IPC
  relay discarded a seed-recovery reply containing the only new image.

Persistent slot and GUI damage regions contain at most 64 rectangles. More
fragmented regions collapse to their bounding rectangle; regions occupying at
least 75% of the panel become full regions. These conservative fallbacks copy or
paint extra pixels without dropping changed pixels. Storage is independent of
the number of accumulated frames.

## GUI presentation

An adoption can request a regional Qt update only when its base equals the
currently displayed allocation/slot/revision and its physical size/DPI matches
the widget. Otherwise it requests the whole panel. The GUI unions any still
unpainted damage before scheduling the update, preserving it across compressed
or partly occluded Qt paints.

Native images use a point blit through the paint event's region clip. Qt's raster
engine copies the clipped spans; glyphs retain their native pixels, including
ceil-rounded fractional-DPI image edges. Temporary resize feedback retains the
existing scaled-image behavior until the matching new surface arrives.

Acknowledgement still follows GUI paint/adoption. An interaction-paced incoming
image cannot be released by a paint of the preceding image. Hidden widgets can
acknowledge without painting and request a complete paint when shown again.
Occluded regions do not block ingestion or frame progression: the current image
stays leased, unpainted damage stays bounded, and later exposure uses the current
complete image.

Every raw depth/trade input and the existing analysis, row diff, geometry, text
and tape behavior remain upstream of this change. Only completed presentation
pixels are coalesced. Fresh GUI image wrappers continue preventing stale Qt
paint-engine cache keys.

## Offline measurements

`scripts/benchmark_orderflow_regions.py` profiles actual shared-buffer methods
and native image blits using synthetic dirty-region patterns. Baseline and
patched runs were sequential on Linux CPython 3.12.14 / Qt 6.11.2, with 48
measured updates per case on an 803 x 907 logical-pixel panel. Pixel comparisons
run outside the timed intervals. This isolates buffer/blit work; it does not
measure ladder preparation, raster paint, damage publication, serialization,
pipes, GPU uploads or desktop FPS.

| Workload | DPI scale | Sync before/after | GUI blit before/after | Copy bytes saved | Blit area saved |
|---|---:|---:|---:|---:|---:|
| BBO bands | 1x | 0.300 / 0.095 ms | 0.275 / 0.049 ms | 100% | 93.4% |
| Repeated row | 1x | 0.300 / 0.104 ms | 0.246 / 0.040 ms | 100% | 97.7% |
| Moving row | 1x | 0.294 / 0.161 ms | 0.227 / 0.040 ms | 97.7% | 97.7% |
| Six disjoint rows | 1x | 0.293 / 0.260 ms | 0.267 / 0.098 ms | 98.9% | 88.9% |
| BBO bands | 2x | 1.638 / 0.188 ms | 2.805 / 0.217 ms | 100% | 93.4% |
| Moving row | 2x | 1.337 / 0.284 ms | 2.664 / 0.100 ms | 97.7% | 97.7% |
| Full churn | 2x | 1.411 / 0.218 ms | 2.461 / 2.653 ms | 100% | 0% |

These are workload medians, not universal latency bounds. Full churn still needs
a full GUI blit and shows no regional blit saving. Holding one lease across six
renders adds bounded bookkeeping even on renders that reuse the same slot;
it avoids redundant copies and reduces the eventual repaint area by 87.5% in
the measured pattern. Dense and fragmented workloads retain conservative
fallbacks.

All 54 workload/DPI combinations preserved every complete buffer pixel, kept
the leased image immutable, and produced GUI pixels identical to a full native
blit. Scales: 1, 1.1, 1.25, 1.5, 1.75 and 2. Workloads include accumulated
unadopted frames, no-paint republishing, full churn, 100 disjoint rectangles,
resize, DPI changes and market reset. Four additional profiles with legacy
leases or stale revisions also preserved pixels at 1.1x and 2x.

`scripts/profile_orderflow_regions.py` additionally ran the actual spawned Qt
analysis/raster workers at 1.25x with ordered synthetic depth/BBO/trade inputs,
interaction pacing, hide/show, resize, same-symbol reset, aggregation, units and
partial occlusion/exposure. The session had no worker errors or pending final
acknowledgement. Both tape consumers retained all 40 trades; displayed and
source sequences matched at completion. It produced 129 partial and 20 full GUI
paints. Worker counters sampled on their normal diagnostic cadence reported
64.8 MB copied and 530.9 MB avoided, rather than full-image copying on every swap.
This offscreen session does not establish Windows desktop FPS.

Raw counters, medians and p95/p99 timings are in
`orderflow-regions-benchmark-results.json`. Existing performance diagnostics now
also expose copied/avoided bytes, full/partial/skipped synchronization, copy
timing, submitted GUI pixel area and full/partial GUI paint counts.

No test suite was run or added.
