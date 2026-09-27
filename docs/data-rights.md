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
| Broker news feed (eToro Licensed Content; agent-portfolio read token) | News evidence cards, earnings dates | Yes, whenever an Agent Portfolio is connected: the feed serves the operator's personal use with their own account (the user's decision of 2026-09-26). The switch is on by default: `invariants.BROKER_FEED_ENABLED` is the code ceiling, and `policy/council.yaml` `news.broker_feed: false` turns it off. | **Never the feed's text.** Only item ids (`N:` + 8 hex characters, an HMAC under a private install key, so an id cannot be matched back to a post), counts, times, instruments and the agents' own paraphrase, labelled "broker feed item, not republished". Any text that shares an 8-word sequence with a feed item, or contains a whole short feed title (4 to 7 words), is withheld automatically, and the final leak scan before every publish checks the cycle's feed texts again. Private copies of feed text (the input capture, transcripts, recorded fixtures) are kept at most 7 days: `council purge-licensed` removes them, and the first cycle of each UTC day runs it. |
| SEC filing metadata (Form 8-K, 8-K/A and 6-K) | News role: that a held or shortlisted stock's company filed a current report, and which items it lists | Yes: form, item codes with the official item titles, acceptance time, company name. Never the filing's content, so a card resting on filing metadata alone can never unlock a cut. | Form, item codes with their official titles, acceptance time, company name and a link keyed by ticker. Never filing text. Attribution: "Source: U.S. Securities and Exchange Commission". Public domain; licence page archived with SHA-256 `78e8a278bf5192fba70d11dd0abd93411e3213ac7cdb06fabca827021f4664af`. |
| Federal Reserve Board (press releases, speeches, testimony) | News role | Yes | Title, summary, link and time, after money and level cleaning. Attribution: "Source: Board of Governors of the Federal Reserve System". Public domain (the Board's own material; regional Reserve Banks and third-party material excluded); archived with SHA-256 `05d877288b49240619fd1f2fdc8fd06a0e3e805e86aa866a0900815bf256230e`. |
| BLS (release feeds) | News role | Yes | Title, summary, link and time, after money and level cleaning. Attribution: "Source: U.S. Bureau of Labor Statistics". Public domain (previously copyrighted photographs and illustrations excluded); archived with SHA-256 `d57e23a7035bf75a49ef570fd5e6dd411eff19f09246c627c3689b4fd667afc9`. |
| BEA (news releases) | News role | Yes | Title, summary, link and time, after money and level cleaning. Attribution: "Source: U.S. Bureau of Economic Analysis". Public domain; archived with SHA-256 `02c2bde5b0334dce8282ed921a8b4f86e4ba5fbc63dcb0cbb114ba26ed57d1d8`. |
| U.S. Treasury (TreasuryDirect auction announcements and results) | News role | Yes: title and time only (no summary is kept, not even for the model) | Title, link and time only. Attribution: "Source: U.S. Department of the Treasury". A federal work, but no explicit site statement was found (`federal_work_unverified`); archived with SHA-256 `05dd5de868c8ea5432f318bec85510e8c6804a9a36c926e1c1b8717cb6894d01`. |
| EIA (press releases, Today in Energy) | News role; oil-related items are tagged to the oil line | Yes | Title, summary, link and time, after money and level cleaning. Attribution: "Source: U.S. Energy Information Administration (<release date>)". Public domain (third-party material excluded); archived with SHA-256 `470f5eeef4ec9a10061e650af00741ab37c08a8188ae1fba96507624aaf6648a`. |
| Broker cost what-if and eligibility | Cost desk, planner, vehicle choice | Yes | Costs in basis points of NAV only. Never amounts, units, instrument or position identifiers. |
| Broker portfolio (open positions of the Agent Portfolio) | Book weights, kill switch, planner | No (they see levels and weights) | Per line: weight (x NAV), direction, the settlement type (real / CFD) and leverage of the line's largest open position, and the book's own P/L since open in % of the amount invested. Never amounts, units, open or current rates, stop rates or identifiers. |
| Cost policy floors (`policy/costs.yaml`) | Cost desk before the broker what-if | Yes | Yes — it is public. The facts table shows the per-side cost in bps of the traded amount only when it came from these floors. |
| FRED (Federal Reserve Economic Data) | Macro context, release dates | Yes | Values only for series marked publishable (US government data such as policy rates, Treasury yields, the broad dollar index, CPI, payrolls). Third-party series hosted on FRED (for example volatility indices or credit spreads owned by index providers) are cited by series name only, with no value. |
| Tiingo end-of-day history | Signal history for the equity, gold, oil and FX lines; the mechanical reference backtest | Yes | Derived percentages only (returns, distances from averages, volatility ratios). No price series. |
| Binance public market data | BTC and ETH signal history | Yes | Derived percentages only. No price series. |
| Alpaca market data, free plan (personal use; the operator's own keys) | Daily history (split- and dividend-adjusted) for the stock lines only, from the stock-sleeve go-live; never for the core lines | Yes, as derived percentages | Derived percentages only (returns, distances from averages, volatility ratios). No prices, no volumes, no price series. Until the publish layer lists Alpaca as a source, facts from it are withheld (`unknown_source`) and no daily change is shown for a stock line. |
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
| Scheduled events, filing sentences | Id and label only; never text | — |
| Broker feed news items (`N:`) | Id and label only; never text | — |
| Public-domain news items (`P:`) | Id and publisher. Title, summary (not Treasury) and link only after money and level cleaning, when the reading list publishes them; SEC items are filing metadata only | `licence_mismatch` when the prefix, the source and the licence do not all say public domain |

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
| Site | Map of the book | Tile areas are the lines' published `|weight_x|` (and `cash_x`); each asset-class box is the sum of its lines' tiles, and its header shows that sum in %. Nothing else is drawn. |
| Site | A line's weight across runs | From each published run: `reference[line].weight_ref_x`, the council's `risk.raw_x`, and the executed book, only where the record knows it: for `completed`, `execution.achieved_x` else `risk.final_x`; for `completed_partial`, `achieved_x`, else the unchanged `risk.base_x` when the plan had a leg on the line, else `final_x`; for `rejected`, `expired`, `superseded` and `reviewed_no_action`, the unchanged `risk.base_x` (nothing traded); for every other state (blocked, execution unknown, approved, executing, proposed, sealed) `achieved_x` if an execution record has the line, else nothing (the outcome is not known yet); nothing for a rehearsal. All in % of the portfolio. |

## Never published, whatever the source

- Money: dollar or euro amounts, account equity, cash, balances, P&L in currency.
- Sizes: units, notional amounts, margin amounts, prices, stop-loss rates.
- Identifiers: account, portfolio, position, order and request identifiers; customer numbers;
  instrument identifiers.
- Infrastructure: IP addresses, local file paths, e-mail addresses, tokens and keys, notification
  topics, private links.
- Raw transcripts, raw broker responses and the private ledger (they stay in private state, outside
  the repository).
- The private input capture (every model call's exact input, raw replies including the first reply
  of a corrected call, and the correction turn), its salts, and the private install key. The
  operator reads the capture in the operator terminal only (`council inputs <cycle>`); the command
  refuses any agent context, because it can show broker feed text.

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

## News the agents read

Every cycle, in rehearsal and live, the news role reads public-domain items from the sources above
(`src/council/data/gov_news.py`, `src/council/stocks/sec_news.py`; register and archived licence
pages in `docs/news-sources.md`) and, whenever an Agent Portfolio is connected and the switch is on,
the broker's feed. Each source keeps at most its quota of the newest items available before the
slot (`policy/council.yaml` `news`: broker feed 25, SEC 8, Federal Reserve Board 5, BLS, BEA,
Treasury and EIA 2 each; at most 40 in all). With no item at all the news role makes no call.
Public-domain titles and summaries are cleaned (money becomes "[amount removed]", large counts
"[level removed]") and leak-scanned when they are fetched; an item that trips the scan is dropped.

## Licences

Code is Apache-2.0. The journal, these docs and the site text are CC BY 4.0. Neither licence
covers third-party material. The public record republishes only U.S. federal public-domain material
(titles, summaries and links from the SEC, the Federal Reserve Board, BLS, BEA, Treasury and EIA),
each carrying its source attribution; that material is not relicensed. No other third-party material
is republished.
