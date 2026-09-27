"""T3b end to end, offline (stub LLM, recorded public items, the fake broker): the news role reads
public-domain `P:` items in every cycle and the broker's feed whenever an Agent Portfolio is
connected and the switch is on (the user's decision of 2026-09-26); the switch off, or no broker,
means zero feed requests; a 403 from the feed leaves the news role running on the public items; no
item at all skips the news call; `N:` ids are keyed by the private install key; the earnings keep
the SEC estimate when the feed is off. No network: the public fetch is a fake, the broker a
FakeEtoro behind the real READ client."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council import context, invariants
from council.broker.etoro_read import EtoroReadClient
from council.cycle import run_cycle
from council.data.gov_news import NewsFetch, SourceReport
from council.deliberation.capture import load_inputs
from council.models.facts import NewsItem, broker_news_id, public_news_id
from council.publish import install_key
from council.runtime import Sources, window
from tests.integration.test_end_to_end import (
    API_KEY,
    NOW,
    READ_KEY,
    SLOT,
    VEHICLES,
    WRITE_KEY,
    _ctx,
    _history,
    _no_events,
)

FEED_PATH = "/api/v1/feeds/news"


@pytest.fixture
def fake_etoro(tmp_path):
    """The connected account of test_end_to_end (instruments, rates, the instrument map)."""
    from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
    from council.broker.instruments import InstrumentMap

    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        if settlement == "real":
            configs = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
        else:
            configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005,
                            row=eligibility_row(sym, iid, configs=configs))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state / "instruments.json").merged({s: v[0] for s, v in VEHICLES.items()}, NOW).save()
    return fake, fclock
FED_LINK = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260930a.htm"


def public_items(n: int = 3) -> list[NewsItem]:
    out = []
    for i in range(n):
        at = SLOT - timedelta(hours=2 + i)
        out.append(NewsItem(id=public_news_id("fed_board", f"guid-{i}"), title=f"Board release number {i}",
                            summary="The Board announced a routine item.", symbols=[], published_at=at,
                            available_at=at, source="fed_board", link=FED_LINK))
    return out


class FakePublic:
    """Stands in for `gov_news.gather_public_news`: records its calls, returns fixed items."""

    def __init__(self, items: list[NewsItem] | None = None, flags: list[str] | None = None) -> None:
        self.items = public_items() if items is None else items
        self.flags = flags or []
        self.calls: list[dict] = []

    def __call__(self, policy, now, state_dir=None, *, slot=None, **kw) -> NewsFetch:
        self.calls.append({"now": now, "slot": slot, "state_dir": state_dir})
        return NewsFetch(items=list(self.items), flags=list(self.flags),
                         sources={"fed_board": SourceReport(source="fed_board", feeds_ok=4, kept=len(self.items))})


def feed_entry(post_id: str, title: str, at) -> dict:
    return {"id": post_id, "post": {"title": title, "summary": "A feed summary for the test.",
                                    "created": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "tags": [{"market": {"symbolName": "NSDQ100"}}]}}


def _cycle_ctx(tmp_path, *, broker=None, public=None, policy=None):
    ctx = _ctx(tmp_path, broker=broker)
    if policy is not None:
        ctx.policy = policy
    news, _ = context.news_sources(ctx.policy, broker=broker, state_dir=ctx.state_dir,
                                   public=public or FakePublic(), clock=lambda: NOW)
    ctx.sources = Sources(history=_history, events=_no_events, news=news, broker=broker)
    return ctx


def _news_calls(record) -> list[dict]:
    return [c for c in record["calls"] if c["role"] == "news"]


def _read_client(fake, fclock) -> EtoroReadClient:
    return EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)


# ------------------------------------------------------------------------------ no broker
def test_without_a_broker_the_news_role_reads_the_public_items(tmp_path):
    public = FakePublic()
    ctx = _cycle_ctx(tmp_path, public=public)
    out = run_cycle(ctx)
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert public.calls and public.calls[0]["slot"] == SLOT
    assert not any(f.startswith("news_broker_feed") for f in record["flags"])
    (call,) = _news_calls(record)
    assert call["status"] == "ok"
    ids = [item.id for item in public.items]
    fetch = record["extras"]["news_fetch"]
    assert [row["id"] for row in fetch["public_items"]] == ids and fetch["broker_items"] == 0
    assert all(row["link"] == FED_LINK for row in fetch["public_items"])
    inputs = load_inputs(ctx.state_dir, out.cycle_id)       # what the news analyst read, privately
    detail = inputs.sections["news_detail"]
    assert {it.ref for it in detail.items if it.field == "id"} == set(ids)


def test_no_news_items_skips_the_news_call(tmp_path):
    ctx = _cycle_ctx(tmp_path, public=FakePublic(items=[]))
    out = run_cycle(ctx)
    record = ctx.ledger.get_cycle(out.cycle_id)
    (call,) = _news_calls(record)
    assert call["status"] == "skipped" and call["error"] == "no_news_items"
    assert all(c.role != "news" for c in load_inputs(ctx.state_dir, out.cycle_id).calls)


# ------------------------------------------------------------------------------ with a broker
def test_the_feed_is_read_by_default_and_never_without_the_switch(tmp_path, fake_etoro, monkeypatch):
    fake, fclock = fake_etoro
    fake.news = [feed_entry("post-1", "Index futures edge higher before the open", SLOT - timedelta(hours=1))]
    ctx = _cycle_ctx(tmp_path, broker=_read_client(fake, fclock))
    out = run_cycle(ctx)
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert fake.count("GET", FEED_PATH) == 1
    key = install_key.load(ctx.state_dir)
    n_id = broker_news_id("post-1", key)
    assert record["extras"]["news_fetch"]["broker_items"] == 1
    assert all(row["id"] != n_id for row in record["extras"]["news_fetch"]["public_items"])
    detail = load_inputs(ctx.state_dir, out.cycle_id).sections["news_detail"]
    assert n_id in {it.ref for it in detail.items}
    assert "news_broker_feed:off" not in record["flags"]

    monkeypatch.setattr(invariants, "BROKER_FEED_ENABLED", False)      # the code ceiling off
    later = _cycle_ctx(tmp_path, broker=_read_client(fake, fclock))
    later.clock = lambda: NOW + timedelta(hours=4)
    later.ledger.clock = later.clock
    out2 = run_cycle(later)
    record2 = later.ledger.get_cycle(out2.cycle_id)
    assert fake.count("GET", FEED_PATH) == 1                             # zero new feed requests
    assert "news_broker_feed:off" in record2["flags"]
    (call,) = _news_calls(record2)
    assert call["status"] == "ok"                                        # the public items still read


def test_the_policy_can_only_turn_the_feed_off(policy, monkeypatch):
    broker = object()
    assert context.broker_feed_enabled(policy, broker)
    assert not context.broker_feed_enabled(policy, None)
    off = policy.model_copy(update={"council": {**policy.council,
                                                "news": {**policy.council["news"], "broker_feed": False}}})
    assert not context.broker_feed_enabled(off, broker)
    monkeypatch.setattr(invariants, "BROKER_FEED_ENABLED", False)
    on = policy.model_copy(update={"council": {**policy.council,
                                               "news": {**policy.council["news"], "broker_feed": True}}})
    assert not context.broker_feed_enabled(on, broker)                   # never above the ceiling


def test_a_403_from_the_feed_leaves_the_news_role_on_the_public_items(tmp_path, fake_etoro):
    fake, fclock = fake_etoro
    fake.news = [feed_entry("post-9", "Never read: the feed refuses", SLOT - timedelta(hours=1))]
    fake.inject("GET", FEED_PATH, status=403, times=5)
    public = FakePublic()
    ctx = _cycle_ctx(tmp_path, broker=_read_client(fake, fclock), public=public)
    out = run_cycle(ctx)
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert fake.count("GET", FEED_PATH) == 1                             # asked once, refused, not retried
    assert "news_source_error:broker_feed:auth" in record["flags"]
    (call,) = _news_calls(record)
    assert call["status"] == "ok"
    detail = load_inputs(ctx.state_dir, out.cycle_id).sections["news_detail"]
    refs = {it.ref for it in detail.items if it.field == "id"}
    assert refs == {item.id for item in public.items}                    # P: items only


def test_n_ids_are_keyed_by_the_install_key(tmp_path, policy):
    class Feed:
        def __init__(self) -> None:
            self.calls = 0

        def feeds_news(self, take: int = 50):
            self.calls += 1
            return {"discussions": [feed_entry("post-7", "Oil slips as supply worries ease",
                                               SLOT - timedelta(hours=3))]}

    def ids(state_dir, slot=SLOT):
        news, _ = context.news_sources(policy, broker=Feed(), state_dir=state_dir,
                                       public=FakePublic(items=[]), clock=lambda: NOW)
        return [item.id for item in news(slot).items]

    first, again = ids(tmp_path / "a"), ids(tmp_path / "a", SLOT + timedelta(hours=4))
    other = ids(tmp_path / "b")
    assert first == again == [broker_news_id("post-7", install_key.load(tmp_path / "a"))]
    assert other == [broker_news_id("post-7", install_key.load(tmp_path / "b"))] and other != first


def test_a_failing_public_fetch_is_a_flag_and_the_feed_still_counts(tmp_path, policy):
    class Feed:
        def feeds_news(self, take: int = 50):
            return {"discussions": [feed_entry("post-3", "Dollar steadies ahead of data",
                                               SLOT - timedelta(hours=1))]}

    def broken(*args, **kw):
        raise RuntimeError("bug")

    news, _ = context.news_sources(policy, broker=Feed(), state_dir=tmp_path, public=broken, clock=lambda: NOW)
    fetch = news(SLOT)
    assert "news_source_error:public:RuntimeError" in fetch.flags
    assert [item.source for item in fetch.items] == ["etoro_feed"]
    assert fetch.sources["broker_feed"].kept == 1


# ------------------------------------------------------------------------------ earnings (SEC)
def test_earnings_keep_the_sec_estimate_when_the_feed_is_off(sleeve_policy, tmp_path, monkeypatch):
    from council.stocks import earnings as EA

    seen: list[dict] = []

    def recorder(policy, **kw):
        seen.append(kw)
        return [], []

    class Feed:
        calls = 0

        def feeds_news(self, take: int = 50):
            Feed.calls += 1
            return {"discussions": []}

    monkeypatch.setattr(EA, "gather_earnings", recorder)
    monkeypatch.setattr(invariants, "BROKER_FEED_ENABLED", False)
    sources = context.data_sources(sleeve_policy, broker=Feed(), state_dir=tmp_path, public_news=FakePublic())
    sources.events(*window(SLOT))
    fetch = sources.news(SLOT)
    assert Feed.calls == 0 and seen and seen[0]["news"] == []          # the SEC estimate alone
    assert "news_broker_feed:off" in fetch.flags


@pytest.mark.parametrize("enabled", [True, False])
def test_zero_feed_requests_without_a_broker(tmp_path, policy, monkeypatch, enabled):
    monkeypatch.setattr(invariants, "BROKER_FEED_ENABLED", enabled)
    news, broker_feed = context.news_sources(policy, broker=None, state_dir=tmp_path,
                                             public=FakePublic(), clock=lambda: NOW)
    assert broker_feed is None
    fetch = news(SLOT)
    assert not any("broker" in f for f in fetch.flags) and all(i.source == "fed_board" for i in fetch.items)


def test_a_failing_feed_is_asked_once_per_slot(sleeve_policy, tmp_path, monkeypatch):
    from council.broker.http import BrokerAuthError
    from council.stocks import earnings as EA

    class Refusing:
        calls = 0

        def feeds_news(self, take: int = 50):
            Refusing.calls += 1
            raise BrokerAuthError(403)

    monkeypatch.setattr(EA, "gather_earnings", lambda policy, **kw: ([], []))
    sources = context.data_sources(sleeve_policy, broker=Refusing(), state_dir=tmp_path, public_news=FakePublic())
    _, event_flags = sources.events(*window(SLOT))
    fetch = sources.news(SLOT)
    assert Refusing.calls == 1                       # the earnings and the news role share the refusal
    assert "earnings_feed_failed:BrokerAuthError" in event_flags
    assert "news_source_error:broker_feed:auth" in fetch.flags and fetch.items
