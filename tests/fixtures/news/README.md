# Public-domain news fixtures

Test data for `council.data.gov_news` and `council.stocks.sec_news` (transparency design §3). Nothing
here is loaded live. Every file was recorded on 2026-09-26 at 15:23 UTC by the opt-in recorder in
`tests/data/test_gov_news_live_canary.py`:

    COUNCIL_LIVE_CANARY=1 COUNCIL_RECORD_NEWS_FIXTURES=1 uv run pytest -m live \
        tests/data/test_gov_news_live_canary.py -k record

Re-recording rewrites both folders and the manifest, and the tests that pin recorded values then
need updating in the same change.

- `feeds/<source>-<feed>.xml`: each feed's response body, byte for byte. `sec-current-8-K.xml` and
  `sec-current-6-K.xml` are SEC's current-filings Atom pages (every filer). The SEC tests re-assign
  one entry of each to a synthetic sleeve line in memory.
- `licences/<source>-2026-09-26.html`: the page each source's licence status was read from. The
  pages are archived without their `script`, `style` and `noscript` elements. The statement is page
  text. The scripts carry per-request tokens and a third-party analytics key, so the served bytes
  differ on every request. `licences/manifest.json` records the archived file's SHA-256, which a
  test checks, and the served page's SHA-256 and size.

## Sources and licence status

The same facts are in code (`council.data.gov_news.SOURCES`). A test checks that each quoted
statement appears in its archived page.

| Source | Feeds | Licence | Statement (archived page) | Public fields | Attribution | Archive SHA-256 |
|---|---|---|---|---|---|---|
| SEC (8-K / 6-K metadata) | current-filings Atom (8-K, 6-K), filtered by CIK; submissions backfill | public domain | "Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC's permission." (sec.gov/about/privacy-information) | form, item codes and official titles, time, company, ticker-keyed link | Source: U.S. Securities and Exchange Commission | `78e8a278bf5192fba70d11dd0abd93411e3213ac7cdb06fabca827021f4664af` |
| Federal Reserve Board | press_monetary, press_other, speeches, testimony | public domain | "Unless otherwise indicated, information on Board's website is in the public domain and may be copied and distributed without permission." (disclaimer.htm) | title, summary, link, time | Source: Board of Governors of the Federal Reserve System | `05d877288b49240619fd1f2fdc8fd06a0e3e805e86aa866a0900815bf256230e` |
| BLS | empsit, cpi, ppi, jolts, eci | public domain | "everything that we publish, both in hard copy and electronically, is in the public domain, except for previously copyrighted photographs and illustrations" (opub/copyright-information.htm) | title, summary, link, time | Source: U.S. Bureau of Labor Statistics | `d57e23a7035bf75a49ef570fd5e6dd411eff19f09246c627c3689b4fd667afc9` |
| BEA | news releases (apps.bea.gov/rss/rss.xml) | public domain | "Unless stated otherwise, the information posted on this web site is in the public domain and may be used or reproduced without specific permission." (help/faq/147) | title, summary, link, time | Source: U.S. Bureau of Economic Analysis | `02c2bde5b0334dce8282ed921a8b4f86e4ba5fbc63dcb0cbb114ba26ed57d1d8` |
| U.S. Treasury | TreasuryDirect auction announcements and results | federal work, unverified | none found on the site policies page, which is archived to show that; a federal work (17 U.S.C. §105) | title, link, time (no summary, not even for the model) | Source: U.S. Department of the Treasury | `05dd5de868c8ea5432f318bec85510e8c6804a9a36c926e1c1b8717cb6894d01` |
| EIA | press releases, Today in Energy (tagged OIL) | public domain | "U.S. government publications are in the public domain and are not subject to copyright protection. You may use and/or distribute any of our data, files, databases, reports, graphs, charts, and other information products" (about/copyrights_reuse.php) | title, summary, link, time | Source: U.S. Energy Information Administration (release date) | `470f5eeef4ec9a10061e650af00741ab37c08a8188ae1fba96507624aaf6648a` |

## What the recording showed

- **Treasury.** No home.treasury.gov press-release feed was found. `/rss.xml` is the site's generic
  front-page feed, and its descriptions carry staff e-mail addresses. v1 therefore reads only the
  TreasuryDirect auction feeds, which answer only when the request sends an `Accept` header. In
  those feeds every item has the same link, and an auction's announcement and result can share a
  time. The id key therefore adds the feed label and the title (`gov_news.stable_key`).
- **EIA.** The This Week in Petroleum feed stopped in October 2025, and its dates are unparseable
  (`###...# EST`). The Weekly Petroleum Status Report has no feed.
- **BEA.** Links use the bare domain `bea.gov`, and one of them has no scheme. Both are read as
  `https://www.bea.gov/...`.
- **EDGAR's item labels.** The labels in EDGAR's feed differ from the Form 8-K item titles for
  items 2.05, 3.03, 5.02 and 5.08. For example, 5.08 reads "Shareholder Nominations Pursuant to
  Exchange Act Rule 14a-11", a rule that was vacated. Titles come from the form's own text
  (`sec_news.ITEM_TITLES`), never from the feed.
