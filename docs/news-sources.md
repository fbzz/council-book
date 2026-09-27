# Public-domain news sources

This page lists the news sources that anyone may check, and their licence status. The broker's news
feed is not one of them: it is eToro Licensed Content, and `docs/data-rights.md` sets its rules.

**Status (2026-09-26): wired.** Every cycle, in rehearsal and live, fetches these sources
(`src/council/data/gov_news.py`, `src/council/stocks/sec_news.py`, called from
`council.context.news_sources`) and the news analyst reads what the pack admits, within the
per-source quotas of `policy/council.yaml` `news` (a policy change in the CHANGELOG).
`docs/data-rights.md` has a row for each source. Until the public reading list ships, the public
record shows each cited item's id and publisher; the operator sees every item read, and what
happened to it, with `council inputs <cycle>`. This page is the register of what was checked.

## Sources and licence status

Each licence statement was read on 2026-09-26. A dated copy of the page it came from is archived
under `tests/fixtures/news/licences/` and listed with its SHA-256 in `manifest.json` there. A test
checks each hash, and checks that each quoted statement appears in its archived page. The same facts
are in code (`council.data.gov_news.SOURCES`), and a test checks that this table matches them.

| Source | What is read | Licence status | Statement (archived page) | Fields a public record may use | Attribution carried by each item | Archive SHA-256 |
|---|---|---|---|---|---|---|
| SEC (Form 8-K and 6-K metadata) | The current-filings Atom feed for 8-K and 6-K, filtered to the stock lines' companies, plus a daily backfill from each company's submissions document | `public_domain` | "Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC's permission." (sec.gov/about/privacy-information) | form, item codes with the official item titles, acceptance time, company name, a link keyed by ticker | Source: U.S. Securities and Exchange Commission | `78e8a278bf5192fba70d11dd0abd93411e3213ac7cdb06fabca827021f4664af` |
| Federal Reserve Board | Press releases (monetary policy and other), speeches and testimony feeds | `public_domain` | "Unless otherwise indicated, information on Board's website is in the public domain and may be copied and distributed without permission." (federalreserve.gov/disclaimer.htm) | title, summary, link, time | Source: Board of Governors of the Federal Reserve System | `05d877288b49240619fd1f2fdc8fd06a0e3e805e86aa866a0900815bf256230e` |
| BLS | Release feeds: the employment situation, CPI, PPI, JOLTS and the employment cost index | `public_domain` | "everything that we publish, both in hard copy and electronically, is in the public domain, except for previously copyrighted photographs and illustrations" (bls.gov/opub/copyright-information.htm) | title, summary, link, time | Source: U.S. Bureau of Labor Statistics | `d57e23a7035bf75a49ef570fd5e6dd411eff19f09246c627c3689b4fd667afc9` |
| BEA | The news-release feed | `public_domain` | "Unless stated otherwise, the information posted on this web site is in the public domain and may be used or reproduced without specific permission." (bea.gov/help/faq/147) | title, summary, link, time | Source: U.S. Bureau of Economic Analysis | `02c2bde5b0334dce8282ed921a8b4f86e4ba5fbc63dcb0cbb114ba26ed57d1d8` |
| U.S. Treasury | TreasuryDirect auction announcements and results | `federal_work_unverified` | None found. The site policies page (home.treasury.gov) is archived to show that. The material is a federal work (17 U.S.C. §105). | title, link, time. No summary is kept, not even for the model. | Source: U.S. Department of the Treasury | `05dd5de868c8ea5432f318bec85510e8c6804a9a36c926e1c1b8717cb6894d01` |
| EIA | Press releases and Today in Energy; an item is tagged to the oil line only when its title or summary is about oil (crude, petroleum, gasoline, diesel, refining and similar words), otherwise it is market-wide | `public_domain` | "U.S. government publications are in the public domain and are not subject to copyright protection. You may use and/or distribute any of our data, files, databases, reports, graphs, charts, and other information products" (eia.gov/about/copyrights_reuse.php) | title, summary, link, time | Source: U.S. Energy Information Administration (release date) | `470f5eeef4ec9a10061e650af00741ab37c08a8188ae1fba96507624aaf6648a` |

**Exclusions.**
- **Third-party material.** The Federal Reserve Board and EIA exclude third-party material, and BLS
  excludes previously copyrighted photographs and illustrations. Only each source's own release
  titles and summaries are read. Nothing is taken from embedded or linked content.
- **SEC filings.** A filing's text is written by the company, so only SEC metadata is read. The
  title is built from the form and the official Form 8-K item titles, for example
  "8-K: Item 2.02 Results of Operations and Financial Condition". A 6-K reads "6-K: report of a
  foreign private issuer". The filer's own description is never used. "EDGAR" is an SEC registered
  mark, so it is not used as the attribution.
- **Companies covered.** SEC items cover only the stock lines that are held or shortlisted. There is
  no watch list of index heavyweights; that is a separate decision. While the stock sleeve is not
  live, the policy has no stock lines, so no SEC request is made.
- **Not in this version:** the regional Reserve Banks (the Board's statement does not cover them),
  the ECB, the Bank of England and news wires.

**What the checks found.**
- No home.treasury.gov press-release feed was found.
- EIA's This Week in Petroleum feed stopped in October 2025, and the Weekly Petroleum Status Report
  has no feed.
- EDGAR's feed labels differ from the official Form 8-K item titles for items 2.05, 3.03, 5.02 and
  5.08, so the titles come from the form's own text.

## How items are fetched and admitted

- **Ids.** Each item's id is `P:` plus the first 8 hex characters of SHA-256 over the source and a
  stable key. The key is the SEC accession number or the feed's guid, otherwise the feed name, link,
  time and title. The inputs are public, so anyone can recompute an id. Broker feed items keep `N:`
  ids, which are keyed hashes under a private install key, so they cannot be matched back to a post.
- **Lookahead.** An item may be used only if it became available strictly before the run's slot, and
  at most 48 hours before it. It becomes available at its publish or acceptance time, or at its
  update time when that is later. An item dated more than 5 minutes after the fetch is dropped as
  clock skew. A date without a time of day counts from the next 00:00 UTC. An item with no readable
  time is dropped, because its availability cannot be shown.
- **Cleaning.** Each title and summary is cleaned when it is fetched: markup, links, e-mail addresses
  and handles are removed, money becomes "[amount removed]" and large counts become "[level
  removed]". Titles are capped at 220 characters and summaries at 320. The full text is at the link.
- **Leak scan.** The cleaned text then goes through the same value patterns as the public leak scan.
  An item that trips it is dropped, with the flag `news_item_dropped:<source>:leak`, so a false
  positive can never reach an agent or a public page.
- **Links.** A link is a typed field, never free text. It must use https and one of these hosts:
  www.sec.gov, www.federalreserve.gov, www.bls.gov, www.bea.gov, apps.bea.gov, home.treasury.gov,
  www.treasurydirect.gov, www.eia.gov. It must not hold a run of 7 or more digits. A link that fails
  is dropped and the item kept.
- **Time limits.** Each request has a 5-second connect timeout and a 10-second read timeout, with at
  most one retry. The sources are fetched in parallel, and the whole fetch has a 30-second budget. A
  source that is still running at the deadline is skipped with `news_source_error:<source>:budget`.
  A source that fails is skipped with `news_source_error:<source>:<type>`. One source's failure
  never costs another's items, and the run always continues.
- **Fair access.** SEC requests share one rate limiter with every other SEC request of the run: at
  most 7 a second (SEC's published limit is 10), with the declared contact user agent. BLS also asks
  for a declared user agent and gets the same one. The user agent is read from the Keychain and sent
  only in the request header, never in a log, a flag or an error. Without it, the SEC and BLS sources
  are skipped and the others still run.
- **Hosts.** A request, or a redirect, to any host outside the list above is refused.
