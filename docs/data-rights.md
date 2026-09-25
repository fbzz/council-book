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
| FRED (Federal Reserve Economic Data) | Macro context, release dates | Yes | Values only for series marked publishable (US government data such as policy rates, Treasury yields, the broad dollar index, CPI, payrolls). Third-party series hosted on FRED (for example volatility indices or credit spreads owned by index providers) are cited by series name only, with no value. |
| Tiingo end-of-day history | Signal history for the equity, gold, oil and FX lines; the mechanical reference backtest | Yes | Derived percentages only (returns, distances from averages, volatility ratios). No price series. |
| Binance public market data | BTC and ETH signal history | Yes | Derived percentages only. No price series. |
| FOMC calendar (`policy/calendar-2026.yaml`) | Event officer | Yes | Yes — it is public. |
| Language-model output (cards, debate, PM decisions) | The council itself | — | Yes, after cleaning: control characters, links, handles, e-mail addresses, paths, money amounts and long numbers are removed before publication. |

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

Approval times are rounded down to the four-hour slot in which they happened.

## Licences

Code is Apache-2.0. The journal, these docs and the site text are CC BY 4.0. Neither licence
covers third-party material, which the public record does not republish.
