"""The fact card of one proposed swing ticker (design swing-book.md rev 2, §1.4 step 3; SW-1).

Two layers:
- COMPLETED-BAR layer: Alpaca daily SIP bars through the last completed session (the caller passes
  ~400 sessions; this module re-applies the availability rule at the slot, so bars after the slot
  can never change a value, and the card hash with them). Stale bars (the newest is not the last
  session whose bar is available at the slot) -> `no_facts`.
- LIVE layer (private): the broker's rate at the slot, by the resolved instrument id. Shown to the
  agents, never published.

Reaction since the news (the core of the card). With t0 = the earliest cited catalyst's
`available_at`, `pre` = the last session whose close is at or before t0, and `last` = the last
completed session:
- `news_age_sessions`: completed sessions after t0 (session close in (t0, slot]).
- `gap_pct`: the first session opening after t0, open vs the prior close (once it completed).
- `move_since_news_close_pct` = close[last] / close[pre] - 1; `..._sigma` divides by
  sigma_pre x sqrt(news_age_sessions), sigma_pre = the 20-day realised vol ending at `pre` (the
  reaction never inflates its own yardstick).
- `vol_ratio_last`: last session's volume / the 20-session median before it; `vol_ratio_since`:
  mean volume after t0 / the 20-session median ending at `pre`. SIP over SIP only.
- `sector_move_since_pct`, `spx_move_since_pct`, `ndx_move_since_pct`: the same window on the sector
  ETF (FF12 -> SPDR), SPY and QQQ completed bars; `rel_move_since_pct` = stock move minus
  beta (60 sessions to `pre`, vs the sector ETF, else SPY) x the sector (else SPX) move.
Context: 52-week distance, SMA50/200 trend, sigma_daily (20d), ATR14 %, beta_60d vs SPY, 5/20/60-day
returns, earnings (next date, confirmed only from the broker feed; sessions since the last 2.02),
FINRA short interest (% of shares outstanding: a lower bound of % of float, basis labelled; absent ->
`crowding: unknown`, never `low`), SEC fundamentals, the max 60-day return correlation with open
swing trades and core lines, and the code-attached catalyst metadata.

Public vs private (`public_view`): the live layer, dollar volume (public only as a bucket), short
interest, feed catalyst text and every cost are private. Alpaca-derived percentages stay withheld
until the data-rights row is widened (Q-S10, `alpaca_public=False`). SEC-derived fields (public
domain) and code fields are public. Percent values only: no price, no volume, no NAV.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from council.clock import session_hours
from council.data.bars import available_only
from council.data.finra import ShortInterest
from council.facts.market import expected_day
from council.models.facts import NewsItem
from council.swing import series
from council.swing.intake import catalyst_item
from council.swing.screen import SECTOR_ETF

SOURCE = "alpaca"
MIN_SESSIONS = 61                 # 60 returns for beta/correlation, 20 for sigma
SMA_SHORT, SMA_LONG = 50, 200
WEEKS_52 = 252
ATR_N = 14
CORR_N = 60
ADV_BUCKETS = ((50e6, "<50M"), (200e6, "50-200M"))
SI_ELEVATED, SI_HIGH = 10.0, 20.0
# Always private (never in the public record).
PRIVATE_FIELDS = frozenset({
    "adv_usd_20d", "move_since_news_live_pct", "move_since_news_live_sigma", "move_today_live_pct",
    "move_today_live_sigma", "short_interest_pct_float", "short_interest_basis", "days_to_cover",
    "short_interest_settlement", "catalyst_items_feed", "corr_60d_with",
})
# Derived from Alpaca bars: public only once the data-rights row is widened (Q-S10).
ALPACA_FIELDS = frozenset({
    "px_ge_10", "gap_pct", "move_since_news_close_pct", "move_since_news_close_sigma", "vol_ratio_last",
    "vol_ratio_since", "sector_move_since_pct", "spx_move_since_pct", "ndx_move_since_pct",
    "rel_move_since_pct", "dist_52w_high_pct", "dist_52w_low_pct", "trend", "sigma_daily", "atr14_pct",
    "beta_60d", "ret_5d", "ret_20d", "ret_60d", "corr_60d_max", "adv_bucket",
})


@dataclass
class FactCard:
    line_id: str
    side: str
    slot: str
    ok: bool
    reason: str | None = None                          # `no_facts` / `no_catalyst_id`
    fields: dict[str, Any] = field(default_factory=dict)
    catalyst_items: list[dict[str, Any]] = field(default_factory=list)     # SEC metadata (public)
    flags: list[str] = field(default_factory=list)

    def fact_ids(self) -> list[str]:
        return [f"X:{self.line_id}:{k}" for k in sorted(self.fields)]

    def content_hash(self) -> str:
        body = {"line_id": self.line_id, "side": self.side, "slot": self.slot, "ok": self.ok,
                "reason": self.reason, "fields": self.fields, "catalyst_items": self.catalyst_items}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def public_view(self, *, alpaca_public: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.fields.items():
            if k in PRIVATE_FIELDS or (k in ALPACA_FIELDS and not alpaca_public):
                continue
            out[k] = v
        return {"line_id": self.line_id, "side": self.side, "ok": self.ok, "reason": self.reason,
                "fields": out, "catalyst_items": list(self.catalyst_items)}


# --------------------------------------------------------------------------------- helpers


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


def session_close(day: date) -> datetime | None:
    hours = session_hours("us", day, closing=True)
    return hours[1] if hours else None


def session_open(day: date) -> datetime | None:
    hours = session_hours("us", day, closing=True)
    return hours[0] if hours else None


def _days(bars: pd.DataFrame) -> list[date]:
    return [pd.Timestamp(t).tz_convert("UTC").date() for t in bars.index]


def completed(bars: pd.DataFrame | None, slot: datetime) -> pd.DataFrame:
    """The bars available at `slot` (the lookahead guard)."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return available_only(bars, source=SOURCE, interval="1d", now=slot)


def _move_between(bars: pd.DataFrame, d0: date, d1: date) -> float | None:
    """Close-to-close % from the last close on/before d0 to the last close on/before d1."""
    if bars.empty:
        return None
    idx = pd.DatetimeIndex(bars.index)
    p0 = series.position_on_or_before(idx, pd.Timestamp(d0).tz_localize("UTC"))
    p1 = series.position_on_or_before(idx, pd.Timestamp(d1).tz_localize("UTC"))
    if p0 is None or p1 is None:
        return None
    c = series.closes(bars)
    return series.pct(c[p1], c[p0])


def _returns(bars: pd.DataFrame) -> pd.Series:
    return bars["close"].astype(float).pct_change().dropna()


def _aligned(a: pd.DataFrame, b: pd.DataFrame, end: date, n: int) -> tuple[np.ndarray, np.ndarray] | None:
    ra, rb = _returns(a), _returns(b)
    cut = pd.Timestamp(end).tz_localize("UTC")
    both = pd.concat([ra[ra.index <= cut], rb[rb.index <= cut]], axis=1, join="inner").dropna().tail(n)
    if len(both) < n:
        return None
    return both.iloc[:, 0].to_numpy(), both.iloc[:, 1].to_numpy()


def adv_bucket(adv: float | None) -> str | None:
    if adv is None:
        return None
    for limit, name in ADV_BUCKETS:
        if adv < limit:
            return name
    return ">200M"


def crowding(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    return "high" if pct > SI_HIGH else "elevated" if pct > SI_ELEVATED else "low"


def sessions_between(t0: datetime, t1: datetime) -> list[date]:
    """US sessions whose close is in (t0, t1]."""
    out: list[date] = []
    day = t0.astimezone(UTC).date()
    end = t1.astimezone(UTC).date()
    while day <= end:
        close = session_close(day)
        if close is not None and t0 < close <= t1:
            out.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return out


# --------------------------------------------------------------------------------- the card


def build_card(
    line_id: str,
    side: str,
    *,
    slot: datetime,
    bars: pd.DataFrame | None,
    catalysts: Sequence[NewsItem],
    sector: str | None = None,
    sector_bars: pd.DataFrame | None = None,
    spx_bars: pd.DataFrame | None = None,
    ndx_bars: pd.DataFrame | None = None,
    live_price: float | None = None,
    earnings_next: date | None = None,
    earnings_confirmed: bool = False,
    last_release_at: datetime | None = None,
    short_interest: ShortInterest | None = None,
    shares_outstanding: float | None = None,
    fundamentals: Mapping[str, float | None] | None = None,
    corr_bars: Mapping[str, pd.DataFrame] | None = None,
) -> FactCard:
    """The card (module rules). Never raises for a data problem: a missing input is a missing
    field and a flag; missing or stale bars, or no admitted catalyst, make the card not ok."""
    card = FactCard(line_id=line_id, side=side, slot=slot.astimezone(UTC).isoformat(), ok=True)
    admitted = [c for c in catalysts if c.available_at < slot]
    if not admitted:
        card.ok, card.reason = False, "no_catalyst_id"
        return card
    b = completed(bars, slot)
    days = _days(b)
    if len(b) < MIN_SESSIONS or not days or days[-1] != expected_day(SOURCE, slot):
        card.ok, card.reason = False, "no_facts"
        return card
    f = card.fields
    close = series.closes(b)
    high = b["high"].to_numpy(dtype=float)
    low = b["low"].to_numpy(dtype=float)
    vol = b["volume"].to_numpy(dtype=float)
    last = len(close) - 1

    # Liquidity and context.
    adv = float(np.median((close * vol)[last - 19:last + 1]))
    f["adv_usd_20d"] = round(adv, 0)
    f["adv_bucket"] = adv_bucket(adv)
    f["px_ge_10"] = bool(close[last] >= 10.0)
    sigma = series.sigma_daily(close, end=last)
    f["sigma_daily"] = _r(sigma * 100.0 if sigma else None)
    tr = np.maximum(high[1:], close[:-1]) - np.minimum(low[1:], close[:-1])
    f["atr14_pct"] = _r(float(np.mean(tr[-ATR_N:])) / close[last] * 100.0)
    for n in (5, 20, 60):
        f[f"ret_{n}d"] = _r(series.pct(close[last], close[last - n]))
    window = close[max(0, last - WEEKS_52 + 1):last + 1]
    f["dist_52w_high_pct"] = _r(series.pct(close[last], float(window.max())))
    f["dist_52w_low_pct"] = _r(series.pct(close[last], float(window.min())))
    above50 = close[last] > float(np.mean(close[-SMA_SHORT:]))
    above200 = close[last] > float(np.mean(close[-SMA_LONG:])) if len(close) >= SMA_LONG else None
    f["trend"] = ("unknown" if above200 is None else "up" if above50 and above200
                  else "down" if not above50 and not above200 else "mixed")
    spx = completed(spx_bars, slot)
    pair = _aligned(b, spx, days[-1], CORR_N) if not spx.empty else None
    f["beta_60d"] = _r(series.beta(*pair) if pair else None)

    # Reaction since the news.
    t0 = min(c.available_at for c in admitted).astimezone(UTC)
    after = sessions_between(t0, slot)
    completed_after = [d for d in after if d <= days[-1]]
    f["news_age_sessions"] = len(completed_after)
    pre_days = [d for d in days if (session_close(d) or datetime.max.replace(tzinfo=UTC)) <= t0]
    if not pre_days:
        card.flags.append("pre_news_close_missing")
    else:
        pre = pre_days[-1]
        p_pre = days.index(pre)
        sessions = max(len(completed_after), 1)
        move = series.pct(close[last], close[p_pre])
        sig_pre = series.sigma_daily(close, end=p_pre)
        f["move_since_news_close_pct"] = _r(move)
        f["move_since_news_close_sigma"] = _r(series.sigma_move(move, sig_pre, sessions) if sig_pre else None)
        first_open = next((d for d in days[p_pre + 1:] if (session_open(d) or t0) > t0), None)
        if first_open is not None:
            p = days.index(first_open)
            f["gap_pct"] = _r(series.pct(float(b["open"].iloc[p]), close[p - 1]))
        med_pre = series.volume_median(vol, end=p_pre)
        since = vol[p_pre + 1:last + 1]
        f["vol_ratio_since"] = _r(float(np.mean(since)) / med_pre if med_pre and len(since) else None)
        etf = SECTOR_ETF.get(sector or "")
        sec_b = completed(sector_bars, slot)
        f["sector_etf"] = etf
        sector_move = _move_between(sec_b, pre, days[-1]) if etf and not sec_b.empty else None
        f["sector_move_since_pct"] = _r(sector_move)
        spx_move = _move_between(spx, pre, days[-1]) if not spx.empty else None
        f["spx_move_since_pct"] = _r(spx_move)
        ndx = completed(ndx_bars, slot)
        f["ndx_move_since_pct"] = _r(_move_between(ndx, pre, days[-1]) if not ndx.empty else None)
        ref_bars, ref_move = (sec_b, sector_move) if sector_move is not None else (spx, spx_move)
        ref_pair = _aligned(b, ref_bars, pre, CORR_N) if ref_move is not None and not ref_bars.empty else None
        beta_ref = series.beta(*ref_pair) if ref_pair else None
        f["rel_move_since_pct"] = _r(move - beta_ref * ref_move if beta_ref is not None and ref_move is not None
                                     else None)
        if live_price is not None and live_price > 0:
            live_move = series.pct(live_price, close[p_pre])
            f["move_since_news_live_pct"] = _r(live_move)
            # The live window spans the completed sessions after the news plus today's partial one.
            live_sessions = len(completed_after) + 1
            f["move_since_news_live_sigma"] = _r(series.sigma_move(live_move, sig_pre, live_sessions)
                                                 if sig_pre else None)
    med_last = series.volume_median(vol, end=last - 1)
    f["vol_ratio_last"] = _r(float(vol[last]) / med_last if med_last else None)
    if live_price is not None and live_price > 0:
        today = series.pct(live_price, close[last])
        f["move_today_live_pct"] = _r(today)
        f["move_today_live_sigma"] = _r(series.sigma_move(today, sigma, 1) if sigma else None)
    elif live_price is not None:
        card.flags.append("live_price_invalid")

    # Earnings, short interest, fundamentals.
    f["earnings_next"] = earnings_next.isoformat() if earnings_next else None
    f["earnings_confirmed"] = bool(earnings_confirmed and earnings_next)
    f["earnings_last_sessions_ago"] = (len(sessions_between(last_release_at, slot))
                                       if last_release_at is not None and last_release_at < slot else None)
    si_pct = short_interest.pct_of_shares(shares_outstanding) if short_interest is not None else None
    f["short_interest_pct_float"] = _r(si_pct)
    f["short_interest_basis"] = "shares_outstanding" if si_pct is not None else None
    f["days_to_cover"] = _r(short_interest.days_to_cover) if short_interest is not None else None
    f["short_interest_settlement"] = (short_interest.settlement_date.isoformat()
                                      if short_interest is not None else None)
    f["crowding"] = crowding(si_pct)
    for key in ("rev_yoy", "rev_accel", "gm_chg", "om_chg", "filing_age_d"):
        value = (fundamentals or {}).get(key)
        f[key] = _r(float(value)) if value is not None else None

    # Correlation with open swing trades and the core lines.
    best: tuple[float, str] | None = None
    for name, other in sorted((corr_bars or {}).items()):
        o = completed(other, slot)
        pr = _aligned(b, o, days[-1], CORR_N) if not o.empty else None
        if pr is None:
            continue
        rho = float(np.corrcoef(pr[0], pr[1])[0, 1])
        if math.isfinite(rho) and (best is None or rho > best[0]):
            best = (rho, name)
    f["corr_60d_max"] = _r(best[0]) if best else None
    f["corr_60d_with"] = best[1] if best else None

    # Code-attached catalyst metadata (never model text).
    feed: list[dict[str, Any]] = []
    for c in sorted(admitted, key=lambda c: (c.available_at, c.id)):
        if c.id.startswith("P:") and c.source == "sec":
            meta = catalyst_item(c)
            card.catalyst_items.append({"id": meta.id, "form": meta.form, "items": list(meta.items),
                                        "titles": list(meta.titles)})
        elif c.id.startswith("N:"):
            feed.append({"id": c.id, "title": c.title})
    f["catalyst_items_feed"] = feed or None
    return card


def sec_fundamentals(cik: int, companyfacts: Mapping[str, Any] | None, *, slot: datetime) -> dict[str, float | None]:
    """rev_yoy, rev_accel, gm_chg, om_chg (percent / percentage points) and filing_age_d from SEC
    companyfacts through the live rank's own computation (filings dated before the slot's UTC
    date), or {} without companyfacts."""
    if not companyfacts:
        return {}
    from council.stocks import pit
    from council.stocks.fundamentals import visibility_day
    from council.stocks.rank import features as rank_features
    from council.stocks.universe import Candidate, RankInputs

    rows, _ = pit.fundamentals_comparable(str(cik), str(cik), dict(companyfacts))
    cand = Candidate(key=str(cik), symbol=str(cik), cik=int(cik))
    day = visibility_day(slot)
    feats = rank_features(cand, RankInputs(candidates=(cand,), fundamentals={int(cik): rows}), day)
    names = dict(zip(pit.FUNDAMENTAL_COLUMNS, ("rev_yoy", "rev_accel", "gm_chg", "om_chg"), strict=True))
    out: dict[str, float | None] = {}
    for col, key in names.items():
        v = feats.get(col)
        out[key] = float(v) * 100.0 if v is not None and pd.notna(v) and math.isfinite(float(v)) else None
    latest = feats.get("latest_available_at")
    out["filing_age_d"] = (float((day - pd.Timestamp(latest)).days)
                           if latest is not None and pd.notna(latest) else None)
    return out
