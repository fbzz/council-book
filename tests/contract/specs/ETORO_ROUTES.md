# eToro Public API — routes used by council-book (condensed from the official route specs, v1.379)

Base URL: `https://public-api.etoro.com` (configurable: `COUNCIL_ETORO_BASE_URL`).

## Auth and headers (every route)
- EITHER `x-api-key: <application key>` + `x-user-key: <user token>` OR `Authorization: Bearer <token>` alone.
  Never both (422). council-book uses the key pair: `x-api-key` = developer app key, `x-user-key` =
  the Agent Portfolio user token (READ token in the runner, WRITE token only in the operator terminal).
- `x-request-id: <uuid>` is ALWAYS required. For open orders it is the idempotency key and is echoed
  back as `referenceId` (the only handle if the 202 response is lost).
- 429 carries `Retry-After` (seconds). Rate-limit headers `RateLimit-Remaining`, `RateLimit-Reset` when present.
- Clean 401/403 on a write = definite rejection (not ambiguous).

## Rate-limit pools
| Pool | Limit | Routes |
|---|---|---|
| execution | 20 / 60 s shared | POST v3 orders, POST v2 orders, POST v1 market-open (by-amount/units), POST v1 market-close, POST v1 limit-orders, all DELETE order routes |
| order info | 60 / 60 s shared | GET v2 orders:lookup, GET v1 real/orders/{id}, GET v1 real/close-orders/{id} |
| portfolio | 60 / 60 s shared | GET v1 trading/info/portfolio, GET v1 real/pnl, GET v2 instrument-breakdown |
| costs | 20 / 60 s dedicated | POST v2 trading/info/costs |
| eligibility | 20 / 60 s dedicated | POST v2 trading/info/eligibility |
| market data | 120 / 60 s shared | candles, rates, instruments, search |
| feeds | 60 / 60 s shared | GET v1 feeds/news, GET v1 feeds/markets/{marketId}, ... |
| default | 60 / 60 s shared | everything else incl. PATCH v2 positions, agent-portfolios |

## Agent Portfolios
- `GET /api/v1/agent-portfolios` → `{"agentPortfolios":[{"agentPortfolioId":uuid,"agentPortfolioName":"MyPort1","agentPortfolioGcid":int,"agentPortfolioVirtualBalance":10000,"mirrorId":int,"createdAt":iso,"userTokens":[{"userTokenId":uuid,"userTokenName":str,"clientId":uuid,"ipsWhitelist":[ipv4],"expiresAt":iso,"scopeNames":["etoro-public:trade.real:read", ...],"createdAt":iso}]}]}`
- `POST /api/v2/agent-portfolios/{agentPortfolioId}/user-tokens` body `{"userTokenName":str,"scopeNames":[...],"ipsWhitelist":[ipv4]?,"expiresAt":iso?}` — scopes available: `etoro-public:trade.real:read`, `trade.real:write`, `trade.demo:read`, `trade.demo:write`. The token secret is returned ONCE at creation.
- The agent portfolio trades a fixed virtual balance; the owner's real account copies it proportionally (`mirrorId`).

## Portfolio / PnL (read)
`GET /api/v1/trading/info/real/pnl` →
```json
{"clientPortfolio": {
  "credit": 10000.5, "unrealizedPnL": 251.0, "bonusCredit": 0.0, "accountCurrencyId": 1,
  "positions": [{
    "positionID": 9001, "CID": 123, "openDateTime": "2024-01-01T09:00:00Z", "openRate": 1.2345,
    "instrumentID": 101, "isBuy": true, "takeProfitRate": 1.5, "stopLossRate": 1.2, "mirrorID": 0,
    "amount": 1000.0, "leverage": 2, "orderID": 5001, "orderType": 1, "units": 10.5,
    "totalFees": 2.5, "initialAmountInDollars": 1000.0, "isTslEnabled": false,
    "settlementTypeID": 0, "isNoStopLoss": false, "isNoTakeProfit": false,
    "unrealizedPnL": {"pnL": 100.25, "exposureInAccountCurrency": 2100.0, "marginInAccountCurrency": 1000.0,
                      "closeRate": 1.25, "closeConversionRate": 1.0, "timestamp": "2024-01-01T12:00:00Z"}
  }],
  "mirrors": [], "orders": [], "ordersForOpen": [], "ordersForClose": [], "ordersForCloseMultiple": []
}}
```
- `amount` = USD margin allocated (initial investment + added margin). Exposure = `unrealizedPnL.exposureInAccountCurrency`
  when present, else `units × closeRate × closeConversionRate`.
- `settlementTypeID`: 0 CFD, 1 Real asset, 2 SWAP, 3 Crypto MarginTrade, 4 Future contract.
- Equity = credit + Σ amount + unrealizedPnL (port of the lab's `calculate_components`).
- Note key casing differs between routes (`positionID`/`instrumentID` here vs `positionId` elsewhere) — parse both.

## Eligibility (read, POST with body)
`POST /api/v2/trading/info/eligibility` body `{"instrumentIds":[1001]?, "symbols":["AAPL"]?, "currency":"USD"}` (≤100 total) →
```json
{"currency":"USD","eligibilities":[{
  "instrumentId":1001,"symbol":"AAPL","minPositionExposure":50.0,"maxUnitsPerOrder":10000.0,
  "allowOpenPosition":true,"allowClosePosition":true,"allowPartialClosePosition":true,
  "allowMitOrders":true,"allowEntryOrders":false,"allowExitOrders":false,"allowTrailingStopLoss":true,
  "requiresW8Ben":null,"unitsQuantityType":"FractionalUnits","orderFillBehaviorType":"BestEffort",
  "allowedOrderQuantityType":"Both","tradeUnitType":"Units",
  "leverageConfigs":[{"settlementType":"CFD","direction":"LONG","leverageValues":[1,2,5],"isPotential":false,
    "minPositionAmount":50.0,"allowEditStopLoss":true,"minStopLossPercentage":5.0,"maxStopLossPercentage":50.0,
    "defaultStopLossPercentage":50.0,"allowEditTakeProfit":true,"minTakeProfitPercentage":5.0,
    "maxTakeProfitPercentage":1000.0,"defaultTakeProfitPercentage":1000.0,"allowStopLossTakeProfit":true}]
}],"notFoundInstrumentIds":[],"notFoundSymbols":[]}
```
- `settlementType`/`direction` casing varies (`CFD`/`cfd`, `LONG`/`long`) — normalise to lower case.
- SL percentages are **% of margin**: for leverage L and price stop distance d, SL% = d × L × 100.
- `isPotential=true` means a questionnaire is needed → treat as unavailable.

## Costs what-if (read, POST with body)
`POST /api/v2/trading/info/costs` body (open) `{"action":"open","transaction":"buy","instrumentId":101,"settlementType":"cfd","orderType":"mkt","leverage":2,"amount":1000.0,"orderCurrency":"usd"}`
or (close) `{"action":"close","transaction":"sell","positionIds":[13902598]}` →
`{"instrumentId":101,"symbol":"AAPL","costs":[{"costType":"markup","amount":0.15,"currency":"USD"},{"costType":"marketSpread","amount":0.03,...},{"costType":"transactionFee","amount":1.0,...},{"costType":"overnightFee","amount":0.25,...},{"costType":"overWeekendFee","amount":0.75,...},{"costType":"sdrt","amount":0.5,...}],"lastUpdated":"2026-05-25T08:30:00Z"}`
- Amounts are USD for the hypothetical order. The old account got 0 for every component on real crypto → floors are mandatory.

## Open order (WRITE)
`POST /api/v3/trading/execution/orders` → **202** `{"token":uuid,"orderId":13902598,"referenceId":"<x-request-id>"}` (accepted, NOT executed)
```json
{"action":"open","transaction":"buy","instrumentId":101,"settlementType":"cfd","orderType":"mkt",
 "leverage":2,"amount":1000.0,"orderCurrency":"usd","stopLossRate":1.2,"stopLossType":"fixed"}
```
- `transaction`: only `buy` (long) and `sellShort` (short) supported; `action`: only `open`.
- Exactly one of `symbol`/`instrumentId`; exactly one of `amount`/`units`/`contracts`.
- `settlementType` mandatory (non-MIT) and must match an eligible `leverageConfigs` row, else rejected AFTER acceptance.
- `stopLossRate` REQUIRED when leverage > 1 or `sellShort`. council-book sets it on EVERY open.
- `amount` semantics (margin vs exposure) must be confirmed in the M6 smoke test.

## Order outcome (read)
`GET /api/v2/trading/info/orders:lookup?referenceId=<x-request-id>` (or `orderId=`) →
```json
{"orderId":13902598,"action":"open","transaction":"buy","type":"mkt",
 "status":{"id":3,"name":"Filled","errorCode":0,"errorMessage":null},
 "asset":{"symbol":"AAPL","instrumentId":101,"currency":"USD","settlementType":"cfd","leverage":2,"side":"long"},
 "requestedAmount":1000.0,"frozenAmount":1002.5,"openStopLossRate":1.2,"stopLossType":"fixed","totalCosts":2.5,
 "positionExecutions":[{"positionId":9001,"state":"open","investedAmountCurrency":1000,"initialExposureAccountCurrency":1000.0,
   "marginAccountCurrency":1000.0,"remainingUnits":10.5,"stopLossRate":1.2,
   "openingData":{"executionTime":"2024-01-01T09:00:01Z","units":10.5,"avgPrice":95.238095,"marketSpread":0.0002,"markup":0.0,"fees":2.5,"taxes":0.0}}],
 "requestTime":"2024-01-01T09:00:00Z","lastUpdate":"2024-01-01T09:00:01Z","requestType":"byAmount"}
```
Status ids: 1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled, 6 PendingCancel, 7 Canceled,
8 Expired, 9 CanceledPartiallyFilled, 10 RejectedPartiallyFilled, 11 WaitingForMarket, 12 PendingTriggeredRate.
Terminal-success: 3 (5 after its poll window). Terminal-failure: 4, 7, 8, 9, 10. In flight: 1, 2, 6, 11, 12. 404 = not found (yet).

## Close position (WRITE)
`POST /api/v1/trading/execution/market-close-orders/positions/{positionId}` body `{"InstrumentId":1111,"UnitsToDeduct":2.0}` (null/omitted = full) →
**200** `{"orderForClose":{"positionID":2150941015,"instrumentID":1111,"unitsToDeduct":2,"orderID":13904638,"orderType":19,"statusID":1,"CID":7765437,"openDateTime":iso,"lastUpdate":iso},"token":uuid}` (submitted, not yet closed)

## Close outcome (read)
`GET /api/v1/trading/info/real/close-orders/{orderId}` →
`{"orderID":13904638,"CID":1,"statusID":int,"referenceID":str|null,"orderType":19,"operationType":int,"errorCode":int|null,"errorMessage":str|null,"instrumentID":1111,"requestOccurred":iso,"proceeds":decimal,"externalOperationType":0,"positions":[{"positionID":2150941015,"occurred":iso|null,"rate":decimal|null,"units":decimal|null,"conversionRate":decimal|null,"amount":decimal|null}]}`
- A close is confirmed when the position appears in `positions` with `occurred` set and it is gone from the portfolio read.

## Modify SL/TP (WRITE)
`PATCH /api/v2/trading/positions/{positionId}` body `{"stopLossRate":145.25,"stopLossType":"fixed"}` (also `takeProfitRate`, `clearStopLoss`, `clearTakeProfit`) →
**202** `{"operationId":uuid,"positionId":13902598,"referenceId":uuid}`; 409 = position closed. Confirm by re-reading the position's `stopLossRate`.

## Market data (read)
- Candles: `GET /api/v1/market-data/instruments/{instrumentId}/history/candles/{asc|desc}/{OneMinute|FiveMinutes|TenMinutes|FifteenMinutes|ThirtyMinutes|OneHour|FourHours|OneDay|OneWeek}/{count≤1000}` →
  `{"interval":"OneDay","candles":[{"instrumentId":12,"candles":[{"instrumentID":12,"fromDate":"2025-03-05T10:34:00Z","open":1.70227,"high":1.70277,"low":1.70221,"close":1.70253,"volume":0.0}],"rangeOpen":...,"rangeClose":...}]}`
  `fromDate` is the candle START; a candle is complete only when fromDate + interval ≤ now.
- Rates: `GET /api/v2/market-data/rates?instrumentIds=1,2` (bid/ask per instrument).
- Search: `GET /api/v2/market-data/instruments/search?...` (entitlement may be blocked; eligibility-by-symbol is the primary resolver).

## Feeds (read)
- `GET /api/v1/feeds/news?take=20&offset=0` → `{"discussions":[{"id":..,"post":{"message":{"text":..},"title":..,"summary":..,"aiSummary":..,"created":iso,"tags":[{"market":{"symbolName":"AAPL","internalId":..}}],"marketEvent":{"earningsDate":iso,"isBeforeMarketOpen":bool,"estimatedEps":..}}}],"paging":{...}}`
  (field nesting varies; parse defensively and keep only: stable id, title, summary/aiSummary, created, symbols, earnings fields).
- `GET /api/v1/feeds/markets/{marketId}?take=20` → instrument discussion posts, same envelope.
- Licensed content: agents may read it; council-book never republishes its text (see docs/data-rights.md).
