"""The swing stage's inputs for a real or an offline run (SW-5c): `SwingSources` and its builders.

- `SwingSources` is what `cycle.run_swing` reads from `ctx.sources.swing` (the SW-1 data layer,
  injected; `cycle` re-exports it).
- `real_swing_sources`: production wiring from the SW-1 modules. News intake = the cycle's news
  fetch (gov + broker feed + the RSS market feeds, fetched once per slot and shared with the core)
  + the per-ticker RSS feed for the open trades, carried ideas and movers-screen names (`rss_tickers`,
  once per slot; ranked by `council.data.rss_news.rank_rss`; licensed text) + SEC's market-wide 8-K / 6-K
  (`swing.intake.sec_market_wide`), with public-domain filing text for a bounded number of filings
  per slot (`swing.sec_text`, cached; movers and screen-universe filers ranked first); the MARKET
  CONTEXT rows (the screen's ETF moves, the policy calendar's FOMC dates; the cycle adds its loaded
  CPI / NFP / PCE events); the after-close movers screen (`swing.screen`, Alpaca SIP daily
  bars, built by `prepare` once per session after 20:30 New York and cached); the code gate =
  `swing.resolve.resolve_ideas` (one bounded eligibility read with the READ broker) + the fact card
  (`swing.facts.build_card`: Alpaca bars, SEC submissions/companyfacts, FINRA short interest);
  paper settlement bars and the SQ-8 returns from Alpaca. Credentials only through the existing
  Keychain helpers (`data.alpaca.load_keys`, `data.credentials.sec_user_agent`); a missing one
  makes the builder FAIL CLOSED: `unavailable` carries `swing_source_unavailable:<source>` and the
  swing council does not run (settlement of existing trades still does). Nothing here ever raises
  into the cycle: a failing request becomes `swing_source_error:<source>:<type>` in `flags`.
- Without a broker (a paper run: dry run, no Agent Portfolio) the eligibility read cannot happen.
  With `allow_unverified` (never in live mode) an idea that SEC resolves passes the gate labelled
  `paper_unverified` (flag `swing_eligibility_unverified`), so the pipeline runs end to end on
  paper; it can never trade (no instrument id is saved, and live needs a broker and
  `invariants.SWING_BOOK_LIVE`).
- `fixture_swing_sources`: deterministic and offline (stub LLM, stub mode, the dress rehearsal):
  one fixture 8-K on the fixture ticker, a fixed fact card, a fixed reference price; pairs with the
  swing replies of `swing_stub_replies` / `fixture_skeptic_gateway`.

Private: fact cards, prices and short interest stay in the ledger / the prompt; nothing here
publishes. No broker writer import, no write call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

UNAVAILABLE = "swing_source_unavailable"
SOURCE_ERROR = "swing_source_error"
UNVERIFIED_LABEL = "paper_unverified"
CARD_HISTORY_DAYS = 600                 # calendar days of daily bars for a fact card (>= 400 sessions)
UNIVERSE_TTL = timedelta(days=7)
UNIVERSE_SECTOR_BUDGET_S = 120.0
RECENT_DAYS = 7                         # ~5 sessions of recent ideas / rejections for the Scout
SEC_TEXT_MAX_FETCHES = 15               # uncached filings read per slot (<= 3 EDGAR requests each)
EVENT_HORIZON = timedelta(days=45)      # scheduled macro events shown as market context


@dataclass
class SwingSources:
    """The swing stage's inputs (SW-1 data layer, injected; no network of its own in `cycle`).

    `inputs(slot, open_trades, code_exits)` -> `swing.council.SwingInputs`; `gate` the code gate
    (resolve + fact card); `reference_price(ticker)` the slot-time reference for paper tracking;
    `candidate_extras(idea)` what code knows beyond the card (sector, listing days, last report);
    `daily_bars(tickers, day)` completed daily bars for `paper.settle`; `benchmark_returns(day)`
    the SQ-8 names' and SPX's returns of that close; `matched_legs(day)` the open trades as
    `benchmark.sq8.MatchedLeg`s. `prepare(slot, now)` is a per-cycle code step (the after-close
    screen) returning flags. `canary_event(slot)` a qualifying `swing.canary.PastEvent` for the
    weekly Skeptic canary (None: no canary this week, flag `swing_canary_no_event`; the field None:
    no canary source). `unavailable`: fail-closed codes; the swing council does not run.
    `flags`: data flags collected by the callbacks, drained into the cycle record."""

    inputs: Any
    gate: Any
    skeptic_gateway: Any = None
    reference_price: Any = None
    candidate_extras: Any = None
    daily_bars: Any = None
    benchmark_returns: Any = None
    matched_legs: Any = None
    prepare: Any = None
    canary_event: Any = None
    unavailable: tuple[str, ...] = ()
    flags: list[str] = field(default_factory=list)

    def drain(self) -> list[str]:
        out = list(dict.fromkeys(self.flags))
        self.flags.clear()
        return out


def _err(source: str, exc: BaseException) -> str:
    return f"{SOURCE_ERROR}:{source}:{type(exc).__name__}"


def _dot(line_id: str) -> str:
    return line_id.replace("_", ".")


# ------------------------------------------------------------------------------------ real
def real_swing_sources(
    policy: Any,
    *,
    state_dir: Path,
    broker: Any | None = None,
    news: Callable[[datetime], Any] | None = None,
    ledger: Any | None = None,
    skeptic_gateway: Any = None,
    allow_unverified: bool = False,
    keys_loader: Callable[[], Any] | None = None,
    sec_user_agent: Callable[[], str] | None = None,
    sec_factory: Callable[[], Any] | None = None,
    fetch_bars: Callable[..., Mapping[str, Any]] | None = None,
    fetch_short_interest: Callable[..., Mapping[str, Any]] | None = None,
    wall: Callable[[], datetime] | None = None,
    sec_text_get: Callable[[str], str] | None = None,
    sec_fetch: Callable[[Any, str], list[Any]] | None = None,
    fetch_last: Callable[..., Mapping[str, float]] | None = None,
    rss_tickers: Callable[[Sequence[str], datetime, datetime], Any] | None = None,
) -> SwingSources:
    """The production `SwingSources` (module rules). Every network callable is injectable (tests
    pass stubs); the defaults are the SW-1 clients. Builds no client and sends no request here:
    only the two Keychain lookups run at build time."""
    from council.data import alpaca
    from council.data.credentials import MissingCredential
    from council.data.credentials import sec_user_agent as _ua

    wall = wall or (lambda: datetime.now(UTC))
    unavailable: list[str] = []
    keys = (keys_loader or alpaca.load_keys)()
    if keys is None:
        unavailable.append(f"{UNAVAILABLE}:alpaca")
    try:
        (sec_user_agent or _ua)()
    except (MissingCredential, Exception):  # noqa: BLE001 - any failure is "no SEC access"
        unavailable.append(f"{UNAVAILABLE}:sec")

    state = _RealState(policy=policy, state_dir=state_dir, broker=broker, news=news, ledger=ledger,
                       keys=keys, allow_unverified=allow_unverified, sec_factory=sec_factory,
                       fetch_bars=fetch_bars, fetch_si=fetch_short_interest, wall=wall,
                       sec_text_get=sec_text_get, sec_fetch=sec_fetch, fetch_last=fetch_last,
                       rss_tickers=rss_tickers)
    src = SwingSources(inputs=state.inputs, gate=state.gate, skeptic_gateway=skeptic_gateway,
                       reference_price=state.reference_price, candidate_extras=state.candidate_extras,
                       daily_bars=state.daily_bars if keys is not None else None,
                       benchmark_returns=state.benchmark_returns if keys is not None else None,
                       matched_legs=None, prepare=state.prepare, unavailable=tuple(unavailable))
    state.flags = src.flags
    from council.swing.canary_set import provider as canary_provider

    src.canary_event = canary_provider(state_dir, src.flags)     # SW-4b: the weekly Skeptic canary
    return src


@dataclass
class _RealState:
    policy: Any
    state_dir: Path
    broker: Any
    news: Any
    ledger: Any
    keys: Any
    allow_unverified: bool
    sec_factory: Any
    fetch_bars: Any
    fetch_si: Any
    wall: Any
    sec_text_get: Any = None
    sec_fetch: Any = None
    fetch_last: Any = None
    rss_tickers: Any = None                          # (line ids, now, slot) -> NewsFetch: the per-ticker RSS feeds
    flags: list[str] = field(default_factory=list)
    _now: datetime | None = None                     # the cycle's clock (set by `prepare`)
    _sec: Any = None
    _tickers: Any = None
    _slot: datetime | None = None
    _reading: dict[str, Any] = field(default_factory=dict)
    _extras: dict[str, dict[str, Any]] = field(default_factory=dict)
    _closes: dict[str, float] = field(default_factory=dict)
    _filings: list[Any] = field(default_factory=list)
    _rss_memo: dict[Any, Any] = field(default_factory=dict)

    # ---- clients
    def sec(self) -> Any:
        if self._sec is None:
            if self.sec_factory is not None:
                self._sec = self.sec_factory()
            else:
                from council.stocks.sec import SecClient

                self._sec = SecClient(cache_root=self.state_dir / "cache")
        return self._sec

    def company_tickers(self) -> list[Any]:
        if self._tickers is None:
            self._tickers = list(self.sec().company_tickers())
        return self._tickers

    def bars(self, line_ids: Sequence[str], start: date, now: datetime) -> dict[str, Any]:
        """{line id: completed daily bars} (empty on any failure, flagged)."""
        from council.data import alpaca

        if self.keys is None or not line_ids:
            return {}
        fetch = self.fetch_bars or alpaca.fetch_daily
        out: dict[str, Any] = {}
        ids = list(dict.fromkeys(line_ids))
        for i in range(0, len(ids), alpaca.SYMBOLS_PER_REQUEST):
            chunk = ids[i:i + alpaca.SYMBOLS_PER_REQUEST]
            try:
                got = fetch([_dot(x) for x in chunk], start, keys=self.keys, now=now)
            except Exception as exc:  # noqa: BLE001 - a data failure is a flag
                self.flags.append(_err("alpaca", exc))
                continue
            for sym, df in (got or {}).items():
                out[str(sym).replace(".", "_").replace("-", "_")] = df
        return out

    # ---- prepare: the after-close screen, once per session
    def prepare(self, slot: datetime, now: datetime) -> list[str]:
        from council.swing import screen as S

        self._now = now
        if self.keys is None:
            return []
        session = S.screen_session(now)
        if session is None or not S.is_ready(now, session) or S.load(self.state_dir, session.isoformat()):
            return []
        flags: list[str] = []
        try:
            universe = self.screen_universe(now)
            filings = self.sec_items(now, slot=now)[1]
            from council.swing.intake import catalyst_filings, cik_tickers

            cats = catalyst_filings(filings, cik_tickers(self.company_tickers()))
            scr = S.run_screen(universe, now=now, keys=self.keys, state_dir=self.state_dir, filings=cats)
            if scr.ready:
                S.save(scr, self.state_dir)
            flags += scr.flags
        except Exception as exc:  # noqa: BLE001 - the screen never stops a cycle
            flags.append(_err("screen", exc))
        return flags

    def screen_universe(self, now: datetime) -> list[Any]:
        """S&P 500 + Nasdaq-100 + the AI list with FF12 sectors (SEC SIC), cached UNIVERSE_TTL in
        `state_dir/swing/universe.json`. Sector lookups are time-bounded; the rest stay None."""
        from council.stocks import universe as U
        from council.swing.screen import build_universe

        path = self.state_dir / "swing" / "universe.json"
        try:
            cached = json.loads(path.read_text())
            if now - datetime.fromisoformat(cached["asof"]) < UNIVERSE_TTL:
                return build_universe(cached["tickers"], cached.get("sectors") or {})
        except (OSError, ValueError, KeyError, TypeError):
            pass
        tickers: list[str] = []
        for index in U.INDEXES:
            try:
                tickers += list(U.fetch_membership(index, cache_root=self.state_dir / "cache").symbols)
            except Exception as exc:  # noqa: BLE001
                self.flags.append(_err(f"membership_{index}", exc))
        try:
            tickers += list(U.load_ai_list())
        except FileNotFoundError:               # the AI list is optional (policy may not carry one)
            self.flags.append("swing_ai_list_absent")
        except Exception as exc:  # noqa: BLE001
            self.flags.append(_err("ai_list", exc))
        by_line = U.ticker_map(self.company_tickers())
        sectors: dict[str, str | None] = {}
        started = time.monotonic()
        for t in dict.fromkeys(tickers):
            lid = U.try_normalise_id(t)
            row = by_line.get(lid) if lid else None
            if lid is None or row is None or time.monotonic() - started > UNIVERSE_SECTOR_BUDGET_S:
                continue
            try:
                sectors[lid] = U.sector_of(self.sec().submissions(row.cik).get("sic"))
            except Exception:  # noqa: BLE001 - an unknown sector is None
                sectors[lid] = None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"asof": now.isoformat(), "tickers": tickers, "sectors": sectors}) + "\n")
        return build_universe(tickers, sectors)

    # ---- inputs
    def sec_items(self, now: datetime, *, slot: datetime, enrich: bool = False,
                  prefer: Mapping[str, int] | None = None) -> tuple[list[Any], list[Any]]:
        from council.stocks import sec_news
        from council.swing.intake import cik_tickers, sec_market_wide

        try:
            enricher = self.enricher() if enrich else None
            result, filings = sec_market_wide(self.sec(), cik_tickers(self.company_tickers()), now=now, slot=slot,
                                              fetch=self.sec_fetch or sec_news.fetch_current,
                                              enrich=enricher, prefer=prefer)
        except Exception as exc:  # noqa: BLE001
            self.flags.append(_err("sec", exc))
            return [], []
        self.flags += list(result.flags)
        return list(result.items), filings

    def enricher(self) -> Any:
        """The slot's bounded, cached SEC filing-text reader (`swing.sec_text`)."""
        from council.swing import sec_text

        get = self.sec_text_get or sec_text.client_get(self.sec())
        return sec_text.Enricher(get, cache_root=self.state_dir / "cache", max_fetches=SEC_TEXT_MAX_FETCHES,
                                 flags=self.flags)

    def screened(self) -> frozenset[str] | None:
        """The cached screen universe as line ids (None when it is not cached or unreadable)."""
        try:
            from council.stocks.universe import try_normalise_id

            cached = json.loads((self.state_dir / "swing" / "universe.json").read_text())
            ids = frozenset(lid for lid in (try_normalise_id(str(t)) for t in cached.get("tickers") or []) if lid)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return ids or None

    def preferred(self, scr: Any, universe: frozenset[str] | None = None) -> dict[str, int]:
        """{line id: tier} for the SEC ranking: movers-screen names 0, the cached screen universe 1."""
        out: dict[str, int] = dict.fromkeys(universe if universe is not None else (self.screened() or ()), 1)
        if scr is not None:
            for rows in (scr.lists or {}).values():
                for row in rows:
                    out[str(row.get("line_id"))] = 0
        return out

    def context_rows(self, scr: Any, session: date | None, slot: datetime) -> list[Any]:
        """Market context: D's broad and sector ETF moves (from the screen, else one Alpaca pass)
        and the policy calendar's FOMC decisions ahead. Facts only (percent, sigma, dates)."""
        from council.data.calendar import fomc_events
        from council.swing import screen as S
        from council.swing.council import ContextRow

        rows: list[Any] = []
        if session is not None:
            ctx = dict(scr.context) if scr is not None and scr.context else {}
            if not ctx and self.keys is not None:
                etfs = sorted(set(S.SECTOR_ETF.values()) | set(S.CONTEXT_ETFS))
                got = self.bars(etfs, session - timedelta(days=S.LOOKBACK_DAYS), slot)
                ctx = S.context_of({e: m for e in etfs if (m := S.day_metric(e, got.get(e), session)) is not None})
            rows += [ContextRow(i, t) for i, t in S.market_context(ctx, session.isoformat())]
        rows += event_rows(fomc_events(self.policy, slot, slot + EVENT_HORIZON), slot)
        return rows

    def inputs(self, slot: datetime, open_trades: Sequence[Any], code_exits: Sequence[str]) -> Any:
        from council.swing import screen as S
        from council.swing.council import SwingInputs
        from council.swing.intake import reading_list

        self._slot = slot
        self._reading.clear()
        self._extras.clear()
        self._closes.clear()
        gov: list[Any] = []
        feed: list[Any] = []
        sec: list[Any] = []
        rss: list[Any] = []
        if self.news is not None:
            try:
                fetched = self.news(slot)
                for item in getattr(fetched, "items", []) or []:
                    if getattr(item, "source", "") == "rss":
                        rss.append(item)
                    elif item.id.startswith("N:"):
                        feed.append(item)
                    elif getattr(item, "source", "") == "sec":
                        sec.append(item)
                    else:
                        gov.append(item)
            except Exception as exc:  # noqa: BLE001
                self.flags.append(_err("news", exc))
        rows: list[dict[str, Any]] = []
        available: datetime | None = None
        session = S.screen_session(slot)            # the latest session whose screen is ready at the slot
        scr = S.load(self.state_dir, session.isoformat()) if session is not None else None
        if scr is not None and scr.ready and scr.session == session.isoformat():
            # D's facts are available from D 20:30 New York (<= the slot by `screen_session`),
            # whenever the cache file was written (a screen built in this very cycle counts)
            rows = [row for k in S.LISTS for row in scr.lists.get(k, [])]
            available = S.ready_at(session)
        else:
            scr = None
            self.flags.append("swing_screen_missing")
        # admission and ages are relative to the slot (a replay never reads past it); the feed's
        # future-skew check is against the real fetch time, i.e. the later of the wall and the cycle clock
        now = max(self.wall(), self._now or slot, slot)
        universe = self.screened()
        prefer = self.preferred(scr, universe)
        wide, self._filings = self.sec_items(now, slot=slot, enrich=True, prefer=prefer)
        rss = self.rss_items(rss, slot=slot, now=now, open_trades=open_trades, prefer=prefer)
        reading = reading_list(sec + wide, feed, gov, slot=slot, rss=rss)
        self._reading = {i.id: i for i in reading}
        try:
            context = self.context_rows(scr, session, slot)
        except Exception as exc:  # noqa: BLE001 - no context is not a reason to stop
            self.flags.append(_err("context", exc))
            context = []
        recent, rejected = self.recent(slot)
        return SwingInputs(slot=slot, reading=reading, screen_rows=rows, screen_available_at=available,
                           open_trades=tuple(open_trades), recent_ideas=recent, recent_rejections=rejected,
                           code_exits=tuple(code_exits), context=context,
                           screened=universe)

    def rss_priority(self, open_trades: Sequence[Any], prefer: Mapping[str, int]) -> list[str]:
        """Line ids for the per-ticker RSS feed, in priority order: open swing trades, carried
        (pending) ideas, then movers-screen names."""
        from council.stocks.universe import try_normalise_id

        names = [str(getattr(t, "ticker", "")) for t in open_trades]
        if self.ledger is not None:
            try:
                names += [str(r["ticker"]) for r in self.ledger.swing_ideas(status="pending")]
            except Exception as exc:  # noqa: BLE001
                self.flags.append(_err("ledger", exc))
        names += [lid for lid, tier in prefer.items() if tier == 0]
        return list(dict.fromkeys(n for n in (try_normalise_id(x) for x in names) if n))

    def rss_items(self, market: Sequence[Any], *, slot: datetime, now: datetime, open_trades: Sequence[Any],
                  prefer: Mapping[str, int]) -> list[Any]:
        """The Scout's RSS selection: the shared fetch's market feeds + the per-ticker feed (fetched
        once per slot here), ranked and capped (`council.data.rss_news.rank_rss`)."""
        from council.data import rss_news

        try:
            cfg = rss_news.rss_config(self.policy)
        except ValueError as exc:
            self.flags.append(_err("rss", exc))
            return []
        if cfg is None:
            return []
        names = self.rss_priority(open_trades, prefer)
        items = list(market)
        if self.rss_tickers is not None and names:
            key = (slot, tuple(names[:cfg.yahoo_max_tickers]))
            if key not in self._rss_memo:            # one request per ticker per slot
                self._rss_memo.clear()
                try:
                    got = self.rss_tickers(list(key[1]), now, slot)
                    self._rss_memo[key] = (list(got.items), list(got.flags))
                except Exception as exc:  # noqa: BLE001 - a per-ticker failure costs those items only
                    self._rss_memo[key] = ([], [f"news_source_error:rss:{type(exc).__name__}"])
            got_items, got_flags = self._rss_memo[key]
            items += got_items
            self.flags += got_flags
        return rss_news.rank_rss(items, cfg, slot=slot, priority=names)

    def recent(self, slot: datetime) -> tuple[list[str], dict[str, datetime]]:
        """Code-written lines of the last RECENT_DAYS of paper-tracked ideas, and each ticker's
        latest Skeptic / PM rejection (H10)."""
        if self.ledger is None:
            return [], {}
        lines: list[str] = []
        rejected: dict[str, datetime] = {}
        try:
            for r in self.ledger.paper_trades():
                opened = datetime.fromisoformat(str(r["opened_at"]))
                if not slot - timedelta(days=RECENT_DAYS) <= opened < slot:
                    continue
                group = str((r.get("record") or {}).get("group") or "")
                lines.append(f"{r['ticker']} {r['side']}: {group or 'tracked'}")
                if group in ("skeptic_rejected", "pm_passed", "skeptic_wait_debated"):
                    key = str(r["ticker"]).replace(".", "_")
                    rejected[key] = max(rejected.get(key, opened), opened)
        except Exception as exc:  # noqa: BLE001
            self.flags.append(_err("ledger", exc))
        return lines[-20:], rejected

    # ---- the code gate
    async def gate(self, ideas: list[Any]) -> dict[str, Any]:
        return self.gate_sync(ideas)

    def gate_sync(self, ideas: list[Any]) -> dict[str, Any]:
        from council.swing.council import GateResult

        out: dict[str, Any] = {}
        slot = self._slot or self.wall()
        try:
            res = self.resolve(ideas, slot)
        except Exception as exc:  # noqa: BLE001 - no resolution: every idea fails closed
            self.flags.append(_err("resolve", exc))
            return {i.ref: GateResult(False, "eligibility_unavailable") for i in ideas}
        passing = [(i, r) for i, r in zip(ideas, res, strict=True) if r.ok or self._unverified_ok(r)]
        for i, r in zip(ideas, res, strict=True):
            if not (r.ok or self._unverified_ok(r)):
                out[i.ref] = GateResult(False, r.reason or "unresolved_symbol")
        if not passing:
            return out
        if any(not r.ok for _, r in passing):
            self.flags.append("swing_eligibility_unverified")
        cards = self.cards(passing, slot)
        for i, _r in passing:
            card = cards.get(i.ref)
            if card is None:
                out[i.ref] = GateResult(False, "no_facts")
            elif not card.ok:
                out[i.ref] = GateResult(False, card.reason or "no_facts", card)
            else:
                out[i.ref] = GateResult(True, None, card)
        return out

    def _unverified_ok(self, r: Any) -> bool:
        return (self.allow_unverified and self.broker is None and r.reason == "eligibility_unavailable"
                and r.line_id is not None and r.cik is not None)

    def resolve(self, ideas: Sequence[Any], slot: datetime) -> list[Any]:
        from council.broker.instruments import InstrumentMap
        from council.stocks import eligibility as gate
        from council.swing import resolve as R

        sp = self.policy.swing
        cfg = gate.gate_config(self.policy, unit_share=float(sp.size.target_nav))
        caps = None
        imap = None
        if self.broker is not None:
            from council.operator import capabilities

            caps = capabilities.load(self.state_dir)
            imap = InstrumentMap.load(self.state_dir / "instruments.json")
        res, new_map = R.resolve_ideas([R.IdeaRef(i.idea.ticker, i.idea.side) for i in ideas],
                                       sec_tickers=self.company_tickers(), cfg=cfg,
                                       stop_min=float(sp.stops.min_pct), stop_max_long=float(sp.stops.max_long_pct),
                                       stop_max_short=float(sp.stops.max_short_pct), now=slot,
                                       read=self.broker, capabilities=caps, instruments=imap)
        if new_map is not None:
            new_map.save()
        return res

    def cards(self, passing: Sequence[tuple[Any, Any]], slot: datetime) -> dict[str, Any]:
        """{idea ref: FactCard}. One Alpaca pass (ideas + sector ETFs + SPY + QQQ), one FINRA
        request, SEC submissions / companyfacts per name (cached by the SEC client)."""
        from council.stocks import earnings as E
        from council.stocks.universe import sector_of
        from council.swing.facts import build_card, sec_fundamentals
        from council.swing.screen import SECTOR_ETF

        subs: dict[str, dict[str, Any]] = {}
        facts: dict[str, Any] = {}
        sectors: dict[str, str | None] = {}
        for _i, r in passing:
            try:
                subs[r.line_id] = dict(self.sec().submissions(r.cik))
                sectors[r.line_id] = sector_of(subs[r.line_id].get("sic"))
            except Exception as exc:  # noqa: BLE001
                self.flags.append(_err("sec", exc))
                sectors[r.line_id] = None
            try:
                facts[r.line_id] = self.sec().companyfacts(r.cik)
            except Exception as exc:  # noqa: BLE001
                self.flags.append(_err("sec", exc))
        etfs = sorted({SECTOR_ETF[s] for s in sectors.values() if s in SECTOR_ETF})
        start = (slot - timedelta(days=CARD_HISTORY_DAYS)).date()
        bars = self.bars([r.line_id for _, r in passing] + etfs + ["SPY", "QQQ"], start, slot)
        si = self.short_interest([r.line_id for _, r in passing], slot)
        live = self.delayed_prices([r.line_id for _, r in passing], slot) if self.broker is None else {}
        out: dict[str, Any] = {}
        for i, r in passing:
            lid = r.line_id
            cats = [self._reading[c] for c in i.idea.catalyst_ids if c in self._reading]
            hist = E.filing_history(subs[lid]) if lid in subs else None
            releases = hist.visible(slot)[0] if hist is not None else ()
            est = E.estimate_next(releases) if releases else None
            last_release = releases[-1] if releases else None
            sector = sectors.get(lid)
            b = bars.get(lid)
            if b is not None and len(b):
                self._closes[i.idea.ticker] = float(b["close"].iloc[-1])
                first = b.index[0].date() if hasattr(b.index[0], "date") else None
                listing = (slot.date() - first).days if first is not None else None
            else:
                listing = None
            try:
                card = build_card(lid, i.idea.side, slot=slot, bars=b, catalysts=cats, sector=sector,
                                  sector_bars=bars.get(SECTOR_ETF.get(sector or "", "")),
                                  spx_bars=bars.get("SPY"), ndx_bars=bars.get("QQQ"),
                                  earnings_next=est[0].date() if est else None, earnings_confirmed=False,
                                  last_release_at=last_release, short_interest=si.get(lid),
                                  shares_outstanding=_shares_outstanding(facts.get(lid)),
                                  fundamentals=sec_fundamentals(r.cik, facts.get(lid), slot=slot),
                                  live_price=live.get(lid))
            except Exception as exc:  # noqa: BLE001 - a card failure drops the idea
                self.flags.append(_err("card", exc))
                continue
            if not r.ok:
                card.flags.append(UNVERIFIED_LABEL)
            out[i.ref] = card
            self._extras[lid] = {"sector": sector, "listing_days": listing, "last_report_at": last_release}
        return out

    def delayed_prices(self, line_ids: Sequence[str], slot: datetime) -> dict[str, float]:
        """A paper run has no broker rate: the intraday reaction since the news (a filing of the
        slot's own session has no completed bar after it, so the close-based move is 0 by
        construction) uses Alpaca's delayed 15-minute SIP bars instead (`alpaca.fetch_delayed_last`,
        never past the slot). Any failure leaves the live layer absent (flagged)."""
        from council.data import alpaca

        if self.keys is None or not line_ids:
            return {}
        fetch = self.fetch_last or alpaca.fetch_delayed_last
        ids = list(dict.fromkeys(line_ids))[:alpaca.SYMBOLS_PER_REQUEST]
        try:
            got = fetch([_dot(x) for x in ids], keys=self.keys, now=slot)
        except Exception as exc:  # noqa: BLE001 - no intraday price: the live layer stays absent
            self.flags.append(_err("alpaca_intraday", exc))
            return {}
        out = {str(k).replace(".", "_").replace("-", "_"): float(v) for k, v in (got or {}).items()}
        if out and "swing_reaction_delayed_alpaca" not in self.flags:
            self.flags.append("swing_reaction_delayed_alpaca")
        return out

    def short_interest(self, line_ids: Sequence[str], slot: datetime) -> dict[str, Any]:
        from council.data import finra

        fetch = self.fetch_si or finra.fetch_short_interest
        out: dict[str, Any] = {}
        ids = list(dict.fromkeys(line_ids))[:finra.MAX_SYMBOLS]
        if not ids:
            return out
        try:
            got = fetch([_dot(x) for x in ids], asof=slot)
        except Exception as exc:  # noqa: BLE001 - unknown short interest halves a short (S1/S13)
            self.flags.append(_err("finra", exc))
            return out
        for sym, v in (got or {}).items():
            out[str(sym).replace(".", "_")] = v
        return out

    def candidate_extras(self, idea: Any) -> dict[str, Any]:
        return {k: v for k, v in self._extras.get(idea.line_id, {}).items() if v is not None}

    def reference_price(self, ticker: str) -> float | None:
        """The paper reference: the last completed close (the private live rate is not read here;
        flag `paper_reference_last_close`). One fetch per missing ticker per slot."""
        if ticker not in self._closes and self._slot is not None:
            from council.stocks.universe import try_normalise_id

            lid = try_normalise_id(ticker)
            if lid is not None:
                got = self.bars([lid], (self._slot - timedelta(days=10)).date(), self._slot)
                b = got.get(lid)
                if b is not None and len(b):
                    self._closes[ticker] = float(b["close"].iloc[-1])
        price = self._closes.get(ticker)
        if price is not None:
            self.flags.append("paper_reference_last_close")
        return price

    # ---- daily measurement
    def daily_bars(self, tickers: Sequence[str], day: date) -> dict[str, Any]:
        got = self.bars([t.replace(".", "_") for t in tickers], day - timedelta(days=45), self.wall())
        return {t: got[t.replace(".", "_")] for t in tickers if t.replace(".", "_") in got}

    def benchmark_returns(self, day: date) -> dict[str, float] | None:
        """{name: return of `day`'s close} for the SQ-8 selection and held names, plus SPX (SPY)."""
        from council.benchmark import sq8

        names: set[str] = set()
        sel = sq8.latest_selection(self.state_dir, on_or_before=day.isoformat())
        if sel is not None:
            names |= set(sel[1])
        book = sq8.load_book(self.state_dir)
        if book is not None:
            names |= set(book.held)
        ids = {n: n.replace(".", "_").replace("-", "_") for n in names}
        got = self.bars([*ids.values(), "SPY"], day - timedelta(days=10), self.wall())
        out: dict[str, float] = {}
        for name, lid in [*ids.items(), ("SPX", "SPY")]:
            r = _day_return(got.get(lid), day)
            if r is not None:
                out[name] = r
        return out if "SPX" in out else None


def event_rows(events: Sequence[Any], slot: datetime, horizon: timedelta = EVENT_HORIZON) -> list[Any]:
    """Scheduled macro events (`EventItem`: FOMC from the policy calendar, CPI / NFP / PCE when the
    cycle loaded them) after the slot within `horizon`, as context rows (id `E:<kind>@<day>`)."""
    from council.swing.council import ContextRow

    names = {"fomc": "FOMC decision", "cpi": "CPI release", "nfp": "payrolls (NFP) release", "pce": "PCE release"}
    out: dict[str, Any] = {}
    for ev in sorted(events, key=lambda e: e.at_utc):
        if ev.kind not in names or ev.symbols or not slot <= ev.at_utc <= slot + horizon:
            continue
        days = (ev.at_utc.date() - slot.date()).days
        out.setdefault(ev.id, ContextRow(ev.id, f"{names[ev.kind]} scheduled {ev.at_utc:%Y-%m-%d %H:%M} UTC "
                                                f"({days} days after the slot)"))
    return list(out.values())


def _day_return(bars: Any, day: date) -> float | None:
    if bars is None or len(bars) < 2:
        return None
    days = [ts.date() for ts in bars.index]
    if day not in days:
        return None
    k = days.index(day)
    if k == 0:
        return None
    prev, cur = float(bars["close"].iloc[k - 1]), float(bars["close"].iloc[k])
    return cur / prev - 1.0 if prev > 0 else None


def _shares_outstanding(companyfacts: Mapping[str, Any] | None) -> float | None:
    """The latest `dei:EntityCommonStockSharesOutstanding` value, or None."""
    try:
        units = companyfacts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]  # type: ignore[index]
        latest = max(units, key=lambda u: str(u.get("end") or ""))
        v = float(latest["val"])
        return v if v > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------ offline
FIXTURE_TICKER = "ACME"
FIXTURE_NEWS_ID = "P:0a1b2c3d"
FIXTURE_RSS_ID = "N:0f1e2d3c"
FIXTURE_PRICE = 100.0


def fixture_card(slot: datetime, line: str = FIXTURE_TICKER, side: str = "long") -> Any:
    from council.swing.facts import FactCard

    fields: dict[str, Any] = {
        "news_age_sessions": 1, "move_since_news_close_pct": 1.2, "move_since_news_close_sigma": 0.8,
        "gap_pct": 0.4, "vol_ratio_since": 1.1, "vol_ratio_last": 1.0, "rel_move_since_pct": 0.3,
        "sector_move_since_pct": 0.5, "atr14_pct": 2.1, "dist_52w_high_pct": -9.5, "trend": "up",
        "adv_bucket": ">200M", "adv_usd_20d": 1e9, "px_ge_10": True, "sigma_daily": 2.5, "beta_60d": 1.1,
        "ret_20d": 3.0, "crowding": "unknown", "rev_yoy": 12.5, "earnings_next": None,
        "earnings_confirmed": False, "sector_etf": "XLK",
    }
    card = FactCard(line_id=line, side=side, slot=slot.astimezone(UTC).isoformat(), ok=True, fields=fields)
    card.catalyst_items.append({"id": FIXTURE_NEWS_ID, "form": "8-K", "items": ["2.02"],
                                "titles": ["Results of Operations and Financial Condition"]})
    return card


def fixture_swing_sources(*, skeptic_gateway: Any = None) -> SwingSources:
    """Deterministic, offline sources (module rules). No network, no Keychain, no clock."""
    from council.models.facts import NewsItem
    from council.swing.council import ContextRow, GateResult, SwingInputs

    seen: list[datetime] = []

    def inputs(slot: datetime, open_trades: Sequence[Any], code_exits: Sequence[str]) -> Any:
        seen[:] = [slot]
        t = slot - timedelta(hours=20)
        item = NewsItem(id=FIXTURE_NEWS_ID, title="8-K: Results of Operations and Financial Condition",
                        symbols=[FIXTURE_TICKER], published_at=t, available_at=t, source="sec", form="8-K",
                        items=["2.02"])
        r = slot - timedelta(hours=3)            # a synthetic RSS headline (licensed path, N: id)
        rss = NewsItem(id=FIXTURE_RSS_ID, title=f"Acme Corp. (NASDAQ: {FIXTURE_TICKER}) schedules an investor day",
                       summary="Fixture press release standing in for a third-party RSS headline.",
                       symbols=[FIXTURE_TICKER], published_at=r, available_at=r, source="rss",
                       licence="third_party_licensed", feed="prnewswire_all")
        return SwingInputs(slot=slot, reading=[item, rss], open_trades=tuple(open_trades), code_exits=tuple(code_exits),
                           context=[ContextRow("F:SPX:ret_5d", "S&P 500 5-day return 0.8%")],
                           core_summary="core book near its reference levels")

    async def gate(ideas: list[Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i in ideas:
            if i.line_id != FIXTURE_TICKER:
                out[i.ref] = GateResult(False, "unresolved_symbol")
            else:
                out[i.ref] = GateResult(True, None, fixture_card(seen[0] if seen else _EPOCH, i.line_id,
                                                                i.idea.side))
        return out

    return SwingSources(inputs=inputs, gate=gate, skeptic_gateway=skeptic_gateway,
                        reference_price=lambda ticker: FIXTURE_PRICE if ticker == FIXTURE_TICKER else None,
                        candidate_extras=lambda idea: {"sector": "BusEq", "listing_days": 900})


_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def swing_stub_replies() -> dict[str, Any]:
    """Canned swing-role replies matching `fixture_swing_sources` (they pass the real schemas)."""
    line = FIXTURE_TICKER
    idea = {"ticker": line, "side": "long", "setup": "post_earnings_drift", "catalyst_ids": [FIXTURE_NEWS_ID],
            "catalyst_claim": "8-K item 2.02 results filed", "thesis": "Fixture thesis: results beat.",
            "why_not_priced_in": "Fixture: the move since the filing is small.", "entry": "now",
            "stop_pct": 0.03, "target_pct": 0.115, "time_stop_days": 10,
            "invalidation": "A close back below the pre-filing level."}
    case = {"argument": "Fixture case.", "strongest_opposing_fact_id": f"X:{line}:dist_52w_high_pct",
            "claims": [{"claim_id": "c1", "ref": "idea:1", "text": "Small move since the filing.",
                        "evidence_ids": [f"X:{line}:move_since_news_close_sigma"]}]}
    bear = {**case, "rebuttals": [{"claim_id": "c1", "verdict": "concede", "text": "True.", "evidence_ids": []}]}
    pm = {"actions": [{"ref": "idea:1", "action": "enter", "evidence_ids": [f"X:{line}:rev_yoy"], "reason": "fixture",
                       "stop_pct": 0.03, "target_pct": 0.115, "time_stop_days": 10}],
          "decisive_fact": {"text": "fixture", "evidence_id": f"X:{line}:rev_yoy"}, "dismissed": []}
    return {"scout": {"ideas": [idea], "passed": []}, "swing_bull": case, "swing_bear": bear, "swing_pm": pm}


def fixture_skeptic_reply() -> dict[str, Any]:
    line = FIXTURE_TICKER
    return {"idea_ref": "idea:1", "catalyst_supports_claim": True, "claim_supports_side": True, "verdict": "pass",
            "priced_in": "partly", "news_status": "new", "regime": "neutral", "crowding": "unknown",
            "reasons": [{"text": "Revenue growth is a fact the move does not contain.",
                         "evidence_ids": [f"X:{line}:rev_yoy"]},
                        {"text": "Room to the 52-week high.", "evidence_ids": [f"X:{line}:dist_52w_high_pct"]}],
            "what_would_change_my_mind": "A restatement.", "second_order": None}


def fixture_skeptic_gateway(policy: Any) -> Any:
    from council.llm.stub import StubGateway

    return StubGateway(responses={"skeptic": fixture_skeptic_reply()}, model=str(policy.swing.llm.skeptic_model))
