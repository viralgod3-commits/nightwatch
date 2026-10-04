# Binance integration audit — 2026-10-02

Scope: Nightwatch's Binance USD-M Futures integration and public Spot comparison data, reviewed against the official documentation available on 2026-10-02. Starting commit: `510859a5d0c7e069e8fd89a01ba6c04c27cc2e69`.

**Status updated 2026-10-04: 326 automated checks passed on Python 3.14.6; authenticated exchange execution remains unverified.** The original October 2 audit below was a source/documentation review without executed tests. See the October 4 verification section for the executed scope and exchange-access blocker.

## Official contracts reviewed

The current catalog and downloadable OpenAPI schemas were used, including parameters, response fields, authentication, filters, limits, and stream events. Older individual endpoint links now redirect, so the catalog and schemas are the contract references.

- [Futures REST trading](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade), [account](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account), and [market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).
- [Futures REST schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/1.0.0/schema.yaml).
- [Futures WebSocket trading](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-api/trade) and [WebSocket API schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-api/1.0.0/schema.yaml).
- [Futures public streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public), [market streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market), and [stream schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/1.0.0/schema.yaml).
- [Futures general information and filters](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info), [account streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams), and [change log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log).
- [Spot general endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general), [Spot market data](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market-data), and [Spot REST schema](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/1.0.0/schema.yaml).

## REST endpoint coverage

The application calls 31 Futures paths through 37 method/path combinations, plus two public Spot paths. Weight-table entries without an application call are excluded. Spot endpoints are read-only; the application places USD-M Futures orders.

| Method(s) | Futures path | Application use and reviewed contract |
|---|---|---|
| GET | `/fapi/v1/time` | Server clock; signed-request timestamp and receive window |
| GET | `/fapi/v1/exchangeInfo` | Universe, symbol filters, rate-limit definitions |
| GET | `/fapi/v1/ticker/24hr` | Symbol and all-symbol market snapshots; differing weights |
| GET | `/fapi/v1/klines` | Candle/history pagination, interval, limits, open/close timestamps |
| GET | `/fapi/v1/depth` | Snapshot limit/weight and sequence alignment with depth updates |
| GET | `/fapi/v1/premiumIndex` | Mark price, funding data, symbol/all-symbol response shapes |
| GET | `/fapi/v1/fundingRate` | Time pagination, limit and shared funding-history quota |
| GET | `/fapi/v1/openInterest` | Current symbol open interest |
| GET | `/futures/data/openInterestHist` | Period, pagination/limit and available history |
| GET | `/futures/data/takerlongshortRatio` | Period, limit and ratio/volume fields |
| GET | `/futures/data/globalLongShortAccountRatio` | Period, limit and account ratio fields |
| GET | `/futures/data/topLongShortAccountRatio` | MARKET_DATA API-key header; ratio fields |
| GET | `/futures/data/topLongShortPositionRatio` | MARKET_DATA API-key header; position ratio fields |
| GET | `/fapi/v3/account` | Signed account snapshot; trading permissions and balances |
| GET | `/fapi/v2/account` | Compatibility fallback account snapshot |
| GET | `/fapi/v3/positionRisk` | Signed position snapshot, side and amount |
| GET | `/fapi/v2/positionRisk` | Compatibility fallback position snapshot |
| GET | `/fapi/v1/accountConfig` | Signed account configuration and permission fields |
| GET | `/fapi/v1/symbolConfig` | Signed symbol margin/leverage configuration |
| GET | `/fapi/v1/positionSide/dual` | Signed hedge/one-way mode |
| GET | `/fapi/v1/openOrders` | Signed standard orders; symbol/all-symbol scope |
| GET | `/fapi/v1/openAlgoOrders` | Signed Algo orders; symbol/all-symbol scope |
| GET | `/fapi/v1/userTrades` | Recent fills, symbol requirement, limits and retention |
| POST, GET, PUT, DELETE | `/fapi/v1/order` | Standard placement, query, LIMIT modification and cancellation |
| POST, GET, DELETE | `/fapi/v1/algoOrder` | Conditional placement, query and cancellation; parent/child IDs and fills |
| POST | `/fapi/v1/batchOrders` | Up to five standard orders; per-order results and partial rejection |
| DELETE | `/fapi/v1/allOpenOrders` | Standard order cancellation for a symbol |
| DELETE | `/fapi/v1/algoOpenOrders` | Algo order cancellation for a symbol |
| POST | `/fapi/v1/leverage` | Signed symbol leverage change and returned limits |
| POST | `/fapi/v1/marginType` | Signed cross-margin change; already-configured response |
| POST, PUT | `/fapi/v1/listenKey` | API-key account stream creation/renewal, expiry and returned key |

| Method | Public Spot path | Application use |
|---|---|---|
| GET | `/api/v3/exchangeInfo` | Spot universe at `data-api.binance.vision`; request weight 20 |
| GET | `/api/v3/klines` | Spot comparison/history at `data-api.binance.vision`; request weight 2 |

The app also downloads public historical archives from Binance's data archive services. These are file downloads, not authenticated trading endpoints.

## WebSocket coverage

| Interface | Calls or subscriptions reviewed |
|---|---|
| Trade API | `order.place`, `algoOrder.place`, `order.modify`, `order.cancel`, `algoOrder.cancel` |
| `/public/stream` | `<symbol>@depth@100ms`, `<symbol>@bookTicker` |
| `/market/stream` | `<symbol>@kline_<interval>`, `<symbol>@aggTrade`, `<symbol>@markPrice@1s`, `!forceOrder@arr` |
| Ticker market connection | `!miniTicker@arr`, `!markPrice@arr@1s` |
| Subscription changes | `SUBSCRIBE` / `UNSUBSCRIBE` for candle intervals and aggregate trades |
| `/private/ws/<listenKey>` | `ACCOUNT_UPDATE`, `ORDER_TRADE_UPDATE`, `ALGO_UPDATE`, `ACCOUNT_CONFIG_UPDATE`, `MARGIN_CALL`, `CONDITIONAL_ORDER_TRIGGER_REJECT`, `listenKeyExpired` |

Stream sequence handling, account snapshot replay, reconnects, 24-hour socket lifetime, and listen-key renewal were reviewed. REST and WebSocket request-weight buckets and the shared account order-rate bucket were compared with current Futures documentation.

## Repairs in this change

1. **Wire contracts.** WebSocket signatures use sorted, unescaped `key=value` pairs. Decimal/flag parameters are strings and documented integer parameters are JSON integers. Nested batch flags are encoded individually. Algo query/cancel sends its documented ID fields without the unsupported `symbol` parameter. Conditional time-in-force combinations unsupported by the current WebSocket schema use REST.
2. **Clock and unknown outcomes.** Signed REST calls require a fresh server clock. A failed pre-send synchronization is identified as not transmitted. HTTP 408 and ambiguous 5xx/backend failures remain uncertain; documented non-executed overload/service failures remain definitive. Placement, amendment and cancellation reconciliation performs reads without repeating the write.
3. **Validation.** Boolean flags are parsed explicitly; hedge-mode and close-all parameter exclusions are enforced. Close-all triggers are validated. Tick/quantity grids honor filter origins and disabled zero tick sizes. Market quantities also satisfy LOT_SIZE. Invalid order/batch payloads are rejected before margin changes. Batches require one rules-compatible symbol, at most five orders and unique client IDs. GTD deadlines are validated after Binance's seconds truncation. BUY/SELL price bands apply to limit prices rather than trigger/activation prices.
4. **Modify and replacement.** The original reduce-only flag is forwarded when modifying a reduce-only LIMIT, matching the September 15, 2026 contract update. Conditional replacement waits for an authoritative canceled-parent query and stops if the parent created a matching-engine child. Remaining quantity is re-read. Replacement protection monitoring and persistence are registered before transmission to handle immediate fills.
5. **Algo lifecycle and fills.** REST `algoStatus`/`actualQty`/`actualPrice` and stream `X`/`aq`/`ap`/`tp` fields share one normalization path. Repeated normalization preserves a child query's verified values. Matching-engine child IDs are saved and linked to their parent protection plans. `FINISHED` is reconciled against the child because it can mean filled or canceled. Zero-fill `FINISHED` does not prematurely discard a pending entry plan. Invalid/non-finite fill data requires reconciliation.
6. **Recovery and exit cleanup.** Verified cumulative fills are retained in the protection journal. Recovery restores previously allocated quantities before extending protection for new fills, even if another saved leg requires review. Fresh account confirmation prevents protection of already-closed offline exposure. Full-tranche exits cancel working sibling TP/SL legs; confirmed flat positions cancel stale siblings. Unconfirmed cleanup blocks new entries, and retired records release their cleanup state. Account snapshots detect missing orders or increased fills for tracked entries and legs, including matching-engine children. Expired or rejected protection is routed to the fail-safe close; its amount is bounded by the tranche quantity minus already verified exits, avoiding an oversized close after a partial TP fill.
7. **Session lifecycle.** Disconnected protection monitoring polls via REST and reconciles the reconnect gap. Credential changes reset stream task references and cleanup state. Keepalive replies are checked for missing/rotated listen keys. Shutdown cancels pending worker tasks while preserving unresolved durable placement intent. Journal serialization handles unbounded symbol rules without emitting non-finite JSON.
8. **Partial batches.** Successful rows are checked for matching client/order identity, fill validity and expected fields. Confirmed per-order rejections are reported with the accepted count and release the completed batch intent. Unknown failures retain reconciliation state. The complete batch is never automatically retried.
9. **Sizing.** Position percentage reductions and smart-exit remaining amounts use Decimal arithmetic to avoid losing a quantity step through intermediate floating-point multiplication.

## Source paths reviewed

The review followed standard and conditional order construction, manual/quick/rail/batch submission, one-way and hedge reductions, close-all, price-match, trailing stops, GTD, LIMIT modification, conditional cancel-and-replace, immediate/partial fills, trigger rejection, uncertain transport, disconnect/reconnect, offline journal restoration, fail-safe close, sibling cleanup and shutdown. Account snapshot, market-data and Spot comparison requests were compared with their endpoint schemas.

These are source-review coverage statements, not executed scenario results.

## Production acceptance still required

- No exchange order scenario was executed. Acceptance must establish actual behavior for supported order types/time-in-force options in one-way and hedge accounts, partial fills, price protection, margin/leverage limits, triggered child orders, and cancellation/modify races.
- Disconnect, lost acknowledgement, delayed event, process termination, restart, journal failure, rate-limit and backend-failure scenarios need execution validation before a production certification is possible.
- TP/SL legs are client-managed orders submitted after verified entry fills. They are not an atomic exchange bracket or native OCO. There is a fill-to-protection interval, minimum-lot constraints can leave dust, and partial sibling quantities are not proportionally resized across unrelated positions. Cancellation is confirmed before admitting new exposure after completed-tranche cleanup.
- An absent queried client ID is not proof that a write was never accepted. Unresolved IDs remain blocked for review rather than being resent. Binance history retention can eventually prevent automated recovery of old records.
- A rejected protection can invoke the existing risk-reducing close, but that close can also be rejected or have an unknown outcome. The application reports such failures and does not guarantee a fill or silently repeat the close.

The changes address confirmed contract and lifecycle defects found in this audit. Static inspection cannot establish that every order will execute exactly as expected under all exchange, account and network conditions.

## Follow-up review — 2026-10-03

Starting commit: `c240bbcceb8cf857ecd111050adf6526995886bf`. The current official Futures trading catalog, account-stream lifecycle documentation, and change log were re-read before continuing the source review. The exchange execution acceptance status above remains outstanding; no tests or exchange orders were run during this follow-up.

| Confirmed defect | Repair |
|---|---|
| In Hedge Mode, an open SHORT could keep a closed LONG's protection plan active, or the reverse. | Protection recovery, flat confirmation, retirement and recovered fail-safe checks use the saved `positionSide`. Symbol-wide queries still support callers that need both sides. |
| A queued protection leg could be transmitted after another leg completed its fill tranche or a fresh account read confirmed the position closed. | Every new leg receives a thread-safe cancellation token. Dispatch checks it before sending and REST checks it again after clock synchronization. An unsent obsolete leg becomes a known canceled outcome, without starting another fail-safe close. |
| An acceptance arriving after tranche completion could leave a sibling active; an early cancel of a not-yet-accepted leg could consume cleanup attempts. | Cleanup is deferred when no exchange identity has been observed. Every leg update rechecks tranche completion, including zero-fill acceptance updates. Once an exchange ID is known, a late accepted parent or matching-engine child can be canceled. |
| An empty account snapshot begun before a leg's acceptance could prematurely retire the entire plan. | Queued/unresolved placements keep the records alive. Retirement requires a read begun after every related status change, with a follow-up refresh when needed. A live exit child is canceled when the entry is terminal and its side is flat; it does not prevent cancellation of other siblings. |
| A stale recovery query could publish fewer fills than the journal had already observed. | The recovery result always includes the monotonic cumulative execution quantities retained by the record, including observations made before that query started. |
| A crash after saving a definitive non-accepted protection outcome but before deleting its placement row could restore a permanently unknown placement. | Journal restoration recognizes the saved definitive outcome and completes deletion locally. Actual uncertain writes continue to require read-only reconciliation. |
| A terminal entry with no fills could remain attached to another position in the same symbol/side. | A verified zero-fill terminal entry with no issued legs can retire independently. The UI also removes its pending plan. |
| Credentials could change after an amendment/cancel reconciliation failed, leaving the unresolved operation attached to the wrong account session. | Credential replacement is blocked while any operation still has an uncertain transport outcome. |

The changed Python files were parsed and the diff was checked for whitespace errors. Cancellation tokens reduce the pre-transmission race; once a request has reached Binance, cancellation and confirmation remain exchange operations and cannot be guaranteed by local inspection. No claim of atomic entry/TP/SL execution or production certification is made.

## Executed verification — 2026-10-04

Starting commit: `3b1817b53363f52fd3024176bc81c22dbadf57ae`, freshly cloned and pulled from the official repository. Runtime: CPython **3.14.6**, PySide6 **6.11.2**, pytest **9.1.1**, Linux with Qt's offscreen platform.

**Result: 326 passed, 0 failed, 0 skipped in 482.82 seconds.** Python compilation and `git diff --check` also passed. The application methods, SQLite journal transactions, HTTP response parsing and Qt controls were executed; exchange replies and worker/socket scheduling were injected. The trading fixture blocks outbound HTTP. This is offline contract and regression verification, not authenticated Binance acceptance.

The current official REST, WebSocket API and user-stream schemas were downloaded and reviewed, together with their general-information pages. Schema SHA-256 prefixes: REST `9c3f92d02d8b84f6`, WebSocket API `307cbf971319f564`, user streams `b81aab5381fa00f2`. WebSocket decimal values follow the general-information requirement to send strings; the downloaded OpenAPI numeric annotations do not override that wire-format instruction.

| Trading capability | Executed scenarios |
|---|---|
| Placement | All seven supported backend order types; all five manual-ticket types; one-way and LONG/SHORT opening/reduction combinations; close-all exclusions |
| Exchange constraints | Tick and lot grids, market LOT_SIZE compatibility, malformed/non-finite values, client IDs, all supported price-match modes, LIMIT time-in-force options, GTD truncation, trailing callback boundaries |
| Wire and transport | REST HMAC over exact encoded bytes for GET/POST/PUT/DELETE; sorted unescaped WebSocket signing; integer identifiers/timestamps, string decimal/flag values; conditional Algo routing and REST fallback for unsupported WebSocket TIF combinations |
| Outcomes and reconciliation | Definitive rejections, HTTP 408/500/503 distinctions, invalid JSON, clock failure before transmission, lost acknowledgements, unknown client IDs, duplicate acknowledgements, account-stream confirmation before the trade acknowledgement |
| Batches | Successful standard/conditional batches, explicit Algo client IDs, partial acceptance/rejection, nested flag serialization, stopping conditional submission after a failed member without sending the remainder |
| Amendments and cancellation | Reduce-only LIMIT modification with total quantity including fills; read-only reconciliation after uncertain modify/cancel; standard/Algo cancellation; cancel-all attempts both services; conditional replacement requires a confirmed canceled parent without a child and re-reads remaining quantity |
| Sizing and closes | Quick-order collateral/slippage limits and TP/SL directions; magnetic entry/TP/SL sizing; passive smart-exit ladders conserving unreserved quantity; market-close and manual-reduce controls in one-way/hedge modes |
| Protection lifecycle | Durable tranches before transmission, partial-fill accumulation, duplicate fills, terminal dust, monotonic fills/status, verified Algo child fills, completed-tranche sibling cleanup, canceling obsolete queued legs, one bounded fail-safe close per tranche, hedge-side retirement |
| Account and settings | V3 account/configuration merging, buffered-event replay, stale account updates, balance reservations, cross/leverage confirmation and queued setting changes, position-mode mismatch, stream creation/renewal/key rotation/expiry |
| Persistence and shutdown | Actual journal transactions and credential/venue isolation, failed journal writes prevent transmission, unresolved intent survives shutdown, restart queries the saved client ID without retransmission, credentials cannot change during unresolved operations |

### Defects reproduced and repaired

- Conditional batches now save and verify the same canonical `clientAlgoId` used on the wire. Both supplied client-ID fields are validated.
- Fixed-quantity orders with cumulative fills above their original quantity require reconciliation. Close-All parents retain their documented dynamic-quantity behavior.
- Older or decreasing-fill observations cannot reset a terminal protection status. Exchange timestamps from the stream envelope are retained; a verified matching-engine child can still replace its parent's `FINISHED` status with the child's working status.
- Cancel-all needs a documented success code from each service; malformed replies remain uncertain rather than reporting success.
- Leverage changes need the requested symbol/value in the reply, and cross-margin changes need a success code or the documented already-configured response.
- Existing protection tests now model cancellable worker tasks, side-aware position queries and the follow-up account read before retirement.

File-level statement coverage: account reconciliation **87%**, trading gateway **67%**, order builders/amendments **56%**, trading UI **70%**. These measurements include presentation and lifecycle branches and do not establish exhaustive coverage of every timing interleaving.

Reproduce with Python 3.14.6:

```sh
python -m pytest tests/test_trading_regressions.py tests/test_protection_lifecycle.py tests/test_trading_layout_ui.py tests/test_account_list_performance.py tests/test_runtime_error_regressions.py -q
```

### Exchange execution blocker

Direct public GET requests to Binance's demo `/fapi/v1/time` and `/fapi/v1/exchangeInfo` returned **HTTP 451** with Binance's restricted-location response from this execution environment. No test-account credentials were available. No authenticated account reads, demo orders, or live orders were placed. Actual authentication, matching-engine fills/triggering, account-specific margin/leverage eligibility and exchange-side races still need acceptance on a Binance demo account from an environment Binance permits.
