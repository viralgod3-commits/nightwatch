# Technical review patch status

Prepared against upstream source commit
`47a58b2f79155317b3e00130a9932d5b00f0c29e`, fetched on 30 September 2026,
then integrated with the newer font-only commit
`4229e6c52761290c5586c6c5e9d2911545f2b1c7`. All upstream font files are preserved.
The newer Rotation scanner, typography changes, and other upstream work are
preserved. This patch applies concrete correctness, recovery and responsiveness
fixes from the technical review to that newer source.

**The architecture rules are ignored as requested.** The existing checker is
unchanged and is not a CI gate. This exemption does not imply that execution
and data services have already been separated from the GUI.

“Patched” below describes the specific defect covered by this change. “Partial”
identifies both the implemented improvement and remaining scope. Offline checks
do not establish live-exchange reliability or a GPU frame-rate target.

## Trading and account state

| Finding | Status | Implementation and remaining scope |
| --- | --- | --- |
| T01 — Accepted protection intent lost on restart | Patched, with recovery limitations below | Adds venue/API-key-scoped protection intents alongside unresolved placements. Acceptance resolves placement uncertainty without deleting the protection plan. Complete fill tranches, progress and every leg ID commit together before transmission. Startup queries IDs, restores monitoring and blocks new entries on unresolved recovery. Legacy accepted entries never journaled remain unrecoverable. |
| T02 — REST overwrites newer account events | Patched for reproduced race | Buffers user-stream events from request start and replays them into the baseline before publishing to either gateway or UI. Rejects an overflowed baseline. Wallet, position and matching order rows honor known exchange timestamps. Separate exchange endpoints still do not form an atomic snapshot; this is not a globally linearizable account ledger. |
| T03 — V3 configuration missing | Patched | Reads explicit account/symbol configuration, hydrates permission, leverage, margin mode, Multi-Assets Mode and position mode, and budgets extra request weights. Permission/configuration uncertainty blocks new entries. |
| T04 — Stale high leverage wins | Patched | Uses latest authoritative leverage rather than maximum cache value, updates position rows from config events, and preserves buffered reductions across REST completion. |
| T05 — Wrong collateral pool returned | Patched for supported sizing | Selects only the requested asset's fresh available balance in confirmed Single-Asset Mode. Wallet updates invalidate free-collateral estimates until refreshed. Full Multi-Assets/Portfolio Margin sizing is not implemented. |
| T06 — SELL rounding exceeds slippage cap | Patched | BUY prices round down and SELL prices round up. Sizing uses the worse of entry reference and rounded limit price. Decimal validation rejects nonfinite/nonpositive inputs. |
| T07 — Protection depends on GUI | Partial | Separate protective placement budget and REST pool; one immediate durable fail-safe close attempt replaces tick/timer-based rejection delay. Protection decisions and account/order WebSockets still depend on GUI scheduling. Independent execution remains necessary for progress during a frozen UI. |
| T08 — Partial-fill capacity/small tranches | Partial | Accumulates sub-minimum partial fills, reserves cumulative allocations before sending, validates full-entry allocation, journals full tranches and uses unique leg idempotency IDs. Batch members count individually. Terminal dust is explicit and attempts fail-safe closure. Exchange minimum lots, disk failures and unknown outcomes can prevent confirmed protection/closure. |
| T09 — Pending entries reuse collateral | Partial | Quick/rail sizing metadata enables gateway collateral reservations before admission. Uncertain outcomes retain them; acceptance releases them only after a REST read begun after acceptance. Manual/batch quantity orders lack a full portfolio reservation model; fees/funding/margin risk are not modeled. |

### Protection lifecycle

1. Unsigned intent commits before transmission. Accepted protected entries keep
   their durable plan after placement completion.
2. A cumulative fill allocates only its unreserved delta. One SQLite transaction
   saves the tranche's progress and all intended leg IDs.
3. Restart restores saved rules/progress and queries Binance by client ID.
   Unknown or absent legs are held for reconciliation, never silently resent.
4. A newly observed terminal entry with a flat cached position requires a fresh
   account read before extension or retirement. A REST read begun before the
   terminal status cannot retire the newer plan. Closed entries do not create
   fresh protection orders.
5. Plans retire only when the matching snapshot scope has no position and no
   related live order. Account replacement is blocked while saved plans remain.

A rejected protection leg requests one risk-reducing close for the whole new
fill tranche. That close uses the durable gateway and is not guaranteed to
execute: validation, exchange rules, connectivity, journal failure or an
uncertain outcome can prevent confirmation. Small nonterminal fills remain
temporarily unprotected while accumulating a valid lot. Existing exchange-side
legs remain live while additional tranches are processed.

Tests cover selected crash windows and cumulative-fill invariants, not an
exhaustive kill/network-fault matrix. Hedge-mode allocation after independent
position changes and every exchange-side leg lifecycle still need testnet fault
validation.

## UI and navigation

| Finding | Status | Implementation and remaining scope |
| --- | --- | --- |
| U01 — Incomplete Leaders history freezes painting | Patched | Handles missing spark endpoints; `finally` restores updates even after another render error. Tested with real widgets. |
| U02 — Leaders prices become stale | Patched | Ticker publications update live price cells by symbol without rerunning historical analysis. Replay retains historical prices; Rotation keeps its separate schema. |
| U03 — Sorted account rows get wrong payload | Patched | Stable identity matching for updates/rebuilds, identity-based selection preservation and guaranteed sorting/update restoration. |
| U04 — Numeric columns sort as text | Patched | Raw numeric/time sort roles are separate from formatted display strings. UTC times sort chronologically. |
| U05 — Small/high-DPI layouts | Partial | Removes mandatory 760-pixel height and caps initial geometry to the screen. Dense controls, floating panels, DPI scaling and multi-monitor behavior still need target-desktop walkthroughs/layout work. |
| U06 — Workflow navigation | Preserved; redesign deferred | Keeps latest Chart, Leaders, Sectors and Rotation. History import no longer locks these workflows. Larger navigation redesign is not mixed into correctness fixes. |
| U07 — Interaction defers visible quotes | Patched for cheap quote paths | Lightweight last-price/watchlist presentation continues during chart interaction/resize. Expensive ranking/research retains deferral; all surfaces need sustained-interaction latency measurements. |
| U08 — Bulk research freezes live operation | Patched | Removes feed suspension and workspace deactivation. Downloader is modeless and uses existing background request priority. Pause/close waits for a checkpointable page/archive. |

## Research and insight

| Finding | Status | Implementation and remaining scope |
| --- | --- | --- |
| D01 — Mainnet/testnet cache mixing | Patched | Separate database filenames partition all cached datasets by venue. Ambiguous legacy database is preserved without automatic migration; new caches start cold. Order journals remain scoped by venue and hashed API-key identity. |
| D02 — Partial failures suppress retries | Patched | Completes a component only after its required valid window or explicit unsupported status. Transient/short responses retry with per-component backoff and deadlines. Spot-universe failures are retryable, not permanent unsupported markets. Whole-batch errors retain existing retry handling. |
| D03 — Readiness accepts internal gaps | Patched | Readiness/backfill share contiguous finite closed-candle checks. Fetches extend to the oldest missing/invalid required bar, including internal gaps. |
| D04 — Indicator lookbacks incomplete | Patched | Required windows cover 56 hourly, 24 quarter-hour and 20 completed daily bars. Daily mean excludes forming candles/gaps. |
| D05 — Taxonomy alias/substring errors | Patched for reviewed cases | Shared curated classification; multiplier aliases such as 1000PEPE/1000BONK; genuine numeric token names such as 1INCH; token-boundary metadata matching. Curated membership still needs ongoing maintenance. |
| D06 — 3D/7D controls lack history | Patched | 200-hour Leaders history covers seven days, replay offset and warmup. Coverage uses the actual span rather than a 24-hour cap. |
| D07 — Methodology/provenance | Partial | Positive-only improving deltas, shared classification, fixed comparable cohorts across sector history charts, cohort counts, and missing turnover distinct from zero. Uncovered sector share is not shown as measured zero. Full glossary, source-age badges, beta/residual context and point-in-time universe/taxonomy versions remain future work. Replay still uses today's universe. |
| D08 — Recorder loss/memory/shutdown | Partial | Bounded buffer with explicit overload gaps, whole failed batches, dedicated serialized writes and asynchronous closing drain. Failed closing batches use atomic fsynced spooling, replayed before the next successful database commit. Disk/spool failure is visible and prevents completing that save. Sustained overload records a gap rather than claiming losslessness. |

Daily means come from analytics worker results. Ticker updates compare current
prices with cached means instead of triggering full sector analysis. Historical
curves use one fixed covered cohort per curve; that cohort can differ from the
latest summary's covered set and is identified in the UI.

## Runtime and performance

| Finding | Status | Implementation and remaining scope |
| --- | --- | --- |
| P01 — GUI-owned market sockets | Partial | All chart hubs, including auxiliary chart-only feeds, parse through workers. Socket frame/message and parser-byte bounds limit input. Socket ownership/initial dispatch remain on the GUI; GUI stalls can still delay intake. |
| P02 — Unbounded parser drain | Patched | Both parsers yield after at most 32 messages or a four-millisecond drain budget with one scheduled continuation. Count/byte bounds trigger validity boundaries and resync. One unusually expensive individual parse can exceed the drain budget. |
| P03 — Analytics failure needs app restart | Patched for order flow | Supervised restarts/backoff, discarded failed-generation inputs, restored config and a fresh depth seed. Ordered queue has count, estimated-byte and age bounds. DOM raster-process supervision and long failure/burst soak remain separate work. |
| P04 — Publication/display cadence mismatch | Deferred to measurement | Existing cadence retained. High-refresh adaptive publication needs target traces of computation, IPC and paint cost; faster publication alone does not establish smooth presentation. |
| P05 — Pools lack global fairness | Partial | Dedicated process-future wait, protection and recorder pools; native numerical threads bounded before import; Windows high priority opt-in. Unified resource/admission scheduling remains unimplemented. |
| P06 — Redundant analysis/widget churn | Partial | Live leader prices and cached sector daily stats bypass historical analysis. Daily means move off GUI. Research-table/sparkline rebuilds on history/filter changes and repeated chart computations remain profiling candidates. |
| P07 — Latency-sensitive I/O | Partial | Rate-log file output off caller thread; streamed assets with byte ceiling; bounded archive download/expansion; project-root resources; redundant candle index removed. Schema initialization, first-use icons and archive CSV materialization still need ownership/streaming changes. |
| P08 — Blocking shutdown/uncancellable work | Partial | Cancels reads/retries/async waits, suppresses late callbacks, saves recorder asynchronously and replaces unbounded GUI analysis-relay wait with cooperative close. Transport drain has a deadline. Remaining bounded parser waits, native/process work and downloader checkpoints need real fault testing. Admitted placements retain journals. |
| P09 — Instrumentation insufficient | Partial | Per-surface frame counts/timing tails/request-to-paint latency displayed in developer tools and covered by tests. Exchange-age, input-to-visible latency, CPU/GPU/presentation and long-soak traces still need target hardware. Qt paints do not prove monitor presentation. |

Queue byte counts are conservative estimates, not complete resident-memory
accounting. Overload invalidates inference and reseeds depth; dropped trades
cannot all be reconstructed. Independent execution and worker-owned sockets
remain necessary for progress during a frozen GUI.

## Build and architecture

| Finding | Status | Implementation and remaining scope |
| --- | --- | --- |
| A01 — UI owns too many services | Partial | Qt-independent account replay and worker-only recorder helpers with explicit worker budgets. Large MainWindow/research ownership remains. |
| A02 — Architecture checker fails | Exempt at user's request | Checker/rules unchanged and excluded from validation and CI. |
| A03 — Build/resource reproducibility | Partial | Setup docs, bounded dependencies, portable font/arrow lookup in GUI and DOM process, system-font fallback, bundled control arrows, Windows freeze support and offline shell verification. Latest upstream fonts are preserved. Frozen desktop build and dependency lock/hashes are not supplied. |
| A04 — Missing regression protection | Partial | 60 focused tests and Windows/Linux × Python 3.12/3.14 Actions matrix. Reviewed regressions and selected recovery/overload cases are covered; full live fault/replay/performance acceptance suites and dependency lock remain outstanding. |
| A05 — Duplicate/dead implementations | Deferred | Legacy classes/development patch tools preserved pending separate call-path analysis. |
| A06 — Identity/ownership/freshness contracts | Partial | Stable table identities/raw sort values, request-start snapshot age, known row timestamps, durable client IDs and account/venue scopes. Immutable revisioned DTOs and complete ownership contracts remain future work. |

Qt/pyqtgraph/NumPy remain in place. JSON parser, kernel/JIT, IPC and renderer
migrations should follow measurements of the corresponding path on
representative workloads.

The publishing check also exposed a hardcoded CoinGecko demo credential in the
upstream MainWindow. This patch removes the literal and reads
`COINGECKO_API_KEY` from the environment. An absent key omits the authentication
header. The old credential remains in earlier public Git history; its owner
should revoke/replace it separately.

## Validation and limits

Local checks used Linux, CPython 3.12.14, PySide6 6.11.2, pyqtgraph 0.14.0,
NumPy 2.5.3 and HTTPX 0.28.1 with SOCKS support installed.

- `python -m pytest -q`: **60 passed**, using mocked exchange responses,
  temporary SQLite journals and real offscreen Qt widgets.
- `python -m compileall -q nightwatch nightwatch_futures.py tests`: passed.
- `git diff --check`: passed.
- Isolated shell check opens all four workspaces and completes cooperative
  shutdown with networking and GPU rendering disabled. Geometry is constrained
  by Qt's virtual screen; this is not a 720p/high-DPI Windows desktop pass.
- The Windows/Linux × Python 3.12/3.14 CI matrix is configured. Check the actual
  run on the published commit; configuration alone is not a passing result.

No live orders or real API credentials were used. There is no measured
maximum-FPS, zero-hitch, native-GPU, high-DPI or live-network reliability claim.

Next priorities: independent execution/socket ownership, testnet
crash/reconnect/partial-fill validation, Windows high-DPI/GPU frame-time baseline,
and scheduling guided by those measurements.
