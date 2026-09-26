# Data rights: what the agents read and what the public record may show

council-book reads several data sources. Reading a source and republishing it are different
rights, so each source has two columns below. The public record (`journal/`, this `docs/`
folder and the generated site) is built only from allow-listed public models
(`src/council/publish/public_models.py`). A leak scan checks every published file, and the site
build fails if the scan finds anything.

## Sources

| Source | Used for | Agents read it | The public record may show |
|---|---|---|---|
| Broker candles and live rates (agent-portfolio read token) | Trend and volatility states, prices for sizing | Yes, as derived percentages | Only the book's own percentages and coarse states (trend up / mixed / down, volatility ratio). No price levels, no charts of broker data. |
| Broker news and market feeds | News evidence cards, earnings dates | Yes | **Nothing verbatim.** A card cites a feed item by its id only (`N:` + 8 hex characters), labelled "broker feed item, not republished". The agent's own short paraphrase may appear. Any text that shares an 8-word sequence with a feed item is withheld automatically. |
| Broker cost what-if and eligibility | Cost desk, planner, vehicle choice | Yes | Costs in basis points of NAV only. Never amounts, units, instrument or position identifiers. |
| Broker portfolio (open positions of the Agent Portfolio) | Book weights, kill switch, planner | No (they see levels and weights) | Per line: weight (x NAV), direction, the settlement type (real / CFD) and leverage of the line's largest open position, and the book's own P/L since open in % of the amount invested. Never amounts, units, open or current rates, stop rates or identifiers. |
| Cost policy floors (`policy/costs.yaml`) | Cost desk before the broker what-if | Yes | Yes — it is public. The facts table shows the per-side cost in bps of the traded amount only when it came from these floors. |
| FRED (Federal Reserve Economic Data) | Macro context, release dates | Yes | Values only for series marked publishable (US government data such as policy rates, Treasury yields, the broad dollar index, CPI, payrolls). Third-party series hosted on FRED (for example volatility indices or credit spreads owned by index providers) are cited by series name only, with no value. |
| Tiingo end-of-day history | Signal history for the equity, gold, oil and FX lines; the mechanical reference backtest | Yes | Derived percentages only (returns, distances from averages, volatility ratios). No price series. |
| Binance public market data | BTC and ETH signal history | Yes | Derived percentages only. No price series. |
| FOMC calendar (`policy/calendar-2026.yaml`) | Event officer | Yes | Yes — it is public. |
| Language-model output (cards, debate, PM decisions) | The council itself | — | Yes, after cleaning: control characters, links, handles, e-mail addresses, paths, money amounts, long numbers and bare price levels (a number with thousands separators, 3+ digits with decimals, or 4+ digits, not followed by a unit such as %, x, bp or days) are removed before publication. |

## The evidence table (`facts` in each cycle)

Every cycle lists the facts of the pack the agents saw, looked up by evidence id: kind, plain
label, line, unit, source, and the time the value became available. The value itself follows the
table above:

| Fact | Value shown | Otherwise (`withheld`) |
|---|---|---|
| Market and volatility facts from Tiingo or Binance history, and the clock's market-open state | Yes, rounded | — |
| Market and volatility facts from broker candles | Only trend, market open and the two volatility ratios | `broker_data` |
| Cost facts | Only when set by the public cost policy floors | `broker_data` |
| FRED series | Only series marked publishable | `licensed_series` (licensed, e.g. VIX) or `not_publishable` (unregistered) |
| Fundamentals | Not yet | `not_publishable` |
| Anything from an unknown source | No | `unknown_source` |
| Scheduled events, broker news items, filing sentences | Id and label only; never text | — |

## Other public fields added for the site

| Document | Field | Meaning |
|---|---|---|
| Cycle | `reference[line].day_change_pct` | The line's last completed daily return, derived from its Tiingo / Binance history (absent for broker candles). |
| Cycle | `macro` | The macro analyst's regime, up to four short drivers (its own cleaned text and evidence refs), a −1 / 0 / +1 tilt per sleeve, and the ids of its cards. |
| Cycle | `calls[].error_kind` | A fixed code for why a model call did not simply succeed (`timeout`, `not_json`, `schema`, `corrected`, …). The error text is never published. |
| Cycle | debate and PM text | Model text is published whole up to its cap (arguments up to about 1,500 characters), after cleaning. |
| Book | `name`, `asset_class`, `session` | From the public policy. |
| Book | `settlement`, `leverage` | Of the line's largest open position. |
| Book | `pnl_since_open_pct` | The book's own P/L on the line's open positions in % of the amount invested: price return since open times leverage, weighted by invested amount (in the instrument's currency). |
| Book | `day_change_pct` | As in the cycle. |
| Site | Open P/L of the book | Computed on the site from the published fields only: each line's `pnl_since_open_pct` weighted by the amount invested in it (`|weight_x|` / `leverage`). Shown only for a live book; a target book (rehearsal or no account) has no P/L. |

## Never published, whatever the source

- Money: dollar or euro amounts, account equity, cash, balances, P&L in currency.
- Sizes: units, notional amounts, margin amounts, prices, stop-loss rates.
- Identifiers: account, portfolio, position, order and request identifiers; customer numbers;
  instrument identifiers.
- Infrastructure: IP addresses, local file paths, e-mail addresses, tokens and keys, notification
  topics, private links.
- Raw transcripts, raw broker responses and the private ledger (they stay in private state, outside
  the repository).

## Units used in public

| Suffix | Meaning | Rounding |
|---|---|---|
| `_x` | Multiple of NAV (0.35x = 35% of NAV) | 0.001 |
| `_pct` | Percent | 0.01 |
| `_bp` | Basis points of NAV | 0.1 |
| index | Time-weighted index, base 100 | 0.1 on the site |

Facts-table values are rounded by their own unit: percent and sigma 0.01, ratios and multiples
0.001, bps and hours 0.1, bps per day 0.01.

Approval times are rounded down to the four-hour slot in which they happened.

## Licences

Code is Apache-2.0. The journal, these docs and the site text are CC BY 4.0. Neither licence
covers third-party material, which the public record does not republish.
