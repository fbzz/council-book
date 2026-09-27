"""LOOKAHEAD and quotas for the news the pack admits (transparency-v2 §3.3, T3b).

- Per-source quotas (`policy/council.yaml` `news.quotas`) are applied AFTER the time filter: an
  item available at or after the slot never takes a place, so rewriting every such item (text,
  source, count, id) leaves the pack hash unchanged.
- Quotas are deterministic: the input order does not matter, each source keeps its newest items up
  to its quota, unused quota is not reassigned, and the whole list is capped at `max_items`.
The "not yet knowable" rule is written independently of the production code: an item is knowable
at slot T only if its available_at is strictly before T.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from council.facts.pack import admissible_news, build_fact_pack
from council.models.facts import MarketState, NewsItem, broker_news_id, public_news_id

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
KEY = b"k" * 32
PUBLIC = ("sec", "fed_board", "bls", "bea", "treasury", "eia")


def item(source: str, n: int, at: datetime, *, title: str | None = None) -> NewsItem:
    if source == "etoro_feed":
        return NewsItem(id=broker_news_id(f"post-{n}", KEY), title=title or f"feed item {n}",
                        published_at=at, available_at=at)
    extra = {"form": "8-K", "items": ["8.01"]} if source == "sec" else {}
    return NewsItem(id=public_news_id(source, f"{source}-{n}"), title=title or f"{source} release {n}",
                    summary="" if source == "treasury" else "A summary.", published_at=at, available_at=at,
                    source=source, **extra)  # type: ignore[arg-type]


def world(before: int = 30, after: int = 10) -> list[NewsItem]:
    out: list[NewsItem] = []
    for source in ("etoro_feed", *PUBLIC):
        for n in range(before):
            out.append(item(source, n, SLOT - timedelta(minutes=7 * n + 1)))
        for n in range(after):
            out.append(item(source, 1000 + n, SLOT + timedelta(minutes=n)))      # at or after the slot
    return out


def pack_of(policy, news):
    states = {ln.symbol: MarketState(symbol=ln.symbol, asset_class=ln.asset_class)
              for ln in policy.universe.lines}
    return build_fact_pack(cycle_id="2026-10-01T1440Z", slot=SLOT, now=SLOT + timedelta(minutes=3),
                           policy=policy, states=states, news=news)


def test_rewriting_every_item_at_or_after_the_slot_leaves_the_pack_hash(policy):
    news = world()
    base = pack_of(policy, news)
    knowable = [n for n in news if n.available_at < SLOT]
    rewritten = [
        *knowable,
        *(item(random.Random(i).choice(PUBLIC), 5000 + i, SLOT + timedelta(minutes=i % 50),
               title=f"breaking after T {i}") for i in range(200)),
        item("etoro_feed", 9999, SLOT, title="stamped at the slot instant"),
    ]
    assert pack_of(policy, rewritten).input_hash == base.input_hash
    assert pack_of(policy, knowable).input_hash == base.input_hash
    assert all(n.available_at < SLOT for n in base.news)


def test_a_new_knowable_item_moves_the_hash(policy):
    base = pack_of(policy, world())
    fresher = item("fed_board", 777, SLOT - timedelta(seconds=30))
    assert pack_of(policy, [*world(), fresher]).input_hash != base.input_hash       # negative control


def test_quotas_are_deterministic_and_never_reassigned(policy):
    quotas = policy.council["news"]["quotas"]
    max_items = policy.council["news"]["max_items"]
    news = world()
    kept = admissible_news(news, SLOT, policy)
    shuffled = list(news)
    random.Random(5).shuffle(shuffled)
    assert [n.id for n in admissible_news(shuffled, SLOT, policy)] == [n.id for n in kept]
    counts: dict[str, int] = {}
    for n in kept:
        key = "broker_feed" if n.source == "etoro_feed" else n.source
        counts[key] = counts.get(key, 0) + 1
    assert len(kept) == max_items
    assert all(counts[k] <= quotas[k] for k in counts)
    assert [n.published_at for n in kept] == sorted((n.published_at for n in kept), reverse=True)

    public_only = [n for n in news if n.source != "etoro_feed"]
    public_kept = admissible_news(public_only, SLOT, policy)
    assert len(public_kept) == sum(quotas[s] for s in PUBLIC) == 21        # the broker quota stays unused
    no_sec = [n for n in public_only if n.source != "sec"]
    assert len(admissible_news(no_sec, SLOT, policy)) == 21 - quotas["sec"]     # not handed to others


def test_each_source_keeps_its_newest_items(policy):
    quotas = policy.council["news"]["quotas"]
    news = world()
    kept = {n.id for n in admissible_news([n for n in news if n.source == "bls"], SLOT, policy)}
    newest = [n for n in news if n.source == "bls" and n.available_at < SLOT][: quotas["bls"]]
    assert kept == {n.id for n in newest}


def test_a_source_without_a_quota_keeps_nothing(policy):
    council = {**policy.council, "news": {**policy.council["news"],
                                          "quotas": {k: v for k, v in policy.council["news"]["quotas"].items()
                                                     if k != "eia"}}}
    trimmed = policy.model_copy(update={"council": council})
    kept = admissible_news(world(), SLOT, trimmed)
    assert kept and all(n.source != "eia" for n in kept)
