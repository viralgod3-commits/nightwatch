# Shared worker trade tape

Priority 4, based on `main` at
`0ed9d9f28d299609fda4475dea8d77a4f721e2f2`. Priorities 1–3 remain in place.

The embedded and separate tapes previously maintained independent All/Large
histories on the GUI thread. Hidden tapes still ingested every snapshot. A print
that disappeared from the analyzer's 2,048-print snapshot before GUI delivery
could be missing from the persistent Large history, even though it had been
accepted by the analyzer.

## History and delivery

`TradeTapeHistory` now captures each accepted normalized execution directly in
the analysis process. All and Large each retain their latest 500 qualifying
prints. Classification, aggressor, quantities, RPI fields, timestamps, relative
size and outcome fields come from the existing analyzer. Its outcome resolver
also updates retained tape records after their snapshot-window eviction. The
8-second recent-print window no longer determines tape-history capture.

Both application tapes use one `SharedTradeTapeSource`. Widgets retain only
their displayed table rows. They do not scan snapshots or maintain additional
All/Large histories. Each view chooses its mode, quantity/value units and scroll
position independently.

The analysis worker publishes at most ten updates per second per visible view.
New subscriptions, mode changes and recovery may require immediate complete
seeds. A normal delta contains changed records and, when membership changes, a
bounded row ordering and removals. Outcome-only deltas update indexed rows and
the tag column without formatting prices, sizes or times again. Precision and
unit changes still reformat the retained display when required.
Threshold-only changes update the Large view; the All view shows a row count
and receives no update for that metadata.

Each view has a subscription token and at most one unacknowledged frame. Its
next delta is based on the rows actually acknowledged by that view. Raw ingestion
continues while a GUI frame waits for the presentation clock; subsequent changes
accumulate in the canonical history. The GUI mailbox keys tape frames by
consumer, so one tape cannot replace the other's frame. Generation, symbol,
mode and token checks reject obsolete queued deliveries.

Hiding or deactivating a view unsubscribes it and drops its worker delivery base.
It receives no history updates while hidden. Showing it seeds the current
retained rows, including outcome corrections made while hidden. This does not
change the application's existing policy of pausing market transport when both
order-flow panels are hidden.

## Reset and recovery

Same-symbol inference resets preserve shared history while advancing an epoch
in each row key. Restarted local sequences therefore cannot collide with older
rows. Both tapes now preserve the same history across reconnects. Changing
symbol clears the producer history; changing a widget's market clears its local
display without clearing trades already accepted for that new symbol.

The IPC relay retains one bounded checkpoint outside the GUI thread, updated
through incremental patches. Worker restart restores the latest received
checkpoint before subscriptions resume. A missing checkpoint or view base
requests a complete derived-state seed; market inputs are not replayed. This
preserves received history, not a batch lost before its worker reply arrived.

Standalone snapshot-based widget hosts use the same protocol with a bounded
Python history worker. They can retain only prints supplied by their host.
Explicit input-budget failure is surfaced instead of silently dropping history.
The main application uses the isolated analysis process.

## Offline measurements

`scripts/benchmark_orderflow_tape.py` compares the previous checkout with this
implementation using the same 300 source snapshots, equivalent to five seconds
at 60 snapshots/second. Two visible tables contain 500 rows each and update at
10/second. Initial seeds are excluded from these totals.

| Workload | GUI time before | GUI time after | Result |
|---|---:|---:|---|
| BBO-only snapshots | 1.95 ms | 0 ms | No tape deliveries |
| Ten new prints per table update | 113.71 ms | 37.04 ms | 67% less GUI work |
| Ten outcome corrections per update | 82.01 ms | 15.97 ms | 81% less GUI work |
| Hidden views with continuous trade bursts | 78.30 ms | 0 ms | No tape deliveries or formatting |

All complete visible trade records matched the expected histories. Each append
workload formatted only its 500 new rows per view; outcome-only updates formatted
zero numeric rows after the seed. Creating the initial two 500-row displays took
7.3–10.0 ms in the patched workloads. Seeds and explicit unit changes still have
bounded full-table cost.

History/publishing/encoding work now runs outside the GUI. The isolated patched
benchmark measured 70.7 ms for that work across the append workload, 67.3 ms for
outcome corrections and 235.5 ms for the more frequent hidden bursts. Those
figures include checkpoint and view encoding but exclude raw trade analysis,
relay decoding, kernel pipes and painting; they do not establish total CPU
savings. The GUI measurements exclude paint and desktop FPS as well.

`scripts/profile_orderflow_tape.py` also exercised an actual spawned analysis
process with the application's QThread relay and offscreen Qt tables. Fourteen
phases preserved complete records through hide/show, independent modes, base
precision, scrolling, stalled presentation, missing-view-base recovery,
same-symbol reset, relay restart and a delayed new-symbol view update. The
session stopped cleanly with no unexpected worker errors.

A separate 2,201-trade capture workload retained its early significant print
outside the 2,048-print snapshot window, corrected its outcome after eviction,
kept tape history after snapshot expiry, rejected duplicate aggregate IDs and
invalid inputs, and recovered a missing checkpoint base with a seed. The
existing analysis/raster session also retained all 40 trades in both views,
finished at the latest source sequence and left no raster acknowledgement pending.

These runs used Linux CPython 3.12.14 and Qt 6.11.2. They are scoped processing
measurements, not Windows desktop-FPS guarantees. Raw results are in
`orderflow-tape-benchmark-results.json`. No test suite was run or added.
