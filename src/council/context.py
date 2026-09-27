"""Build a CycleContext for the unattended runner, a dry run, or an offline stub run.

  live     — READ-only Agent Portfolio token from the login keychain, real data, real LLM, push.
  dry_run  — no broker (or the read token if present), real data and LLM, publishes to
             site-preview/ only (never pushes, never notifies).
  stub     — no network at all: canned LLM replies and caller-supplied data sources (tests).

A live context loads policy from a snapshot of the committed `HEAD:policy/` (runtime.
head_policy_snapshot), never from the working tree. When git cannot provide it, a live context runs
on the last verified snapshot with a whole-book blocker and an URGENT alert, and refuses to start
only when there is none. A committed stock sleeve stays out of every context until
`invariants.STOCK_SLEEVE_LIVE`. The WRITE token is never loaded here; only council.operator.approve
may load it.

Data sources (`data_sources`): Tiingo (core lines), Binance, FRED and the calendar as before; when
the policy has stock lines, their history comes from Alpaca (keys `council-book.alpaca-key-id` and
`council-book.alpaca-secret`; absent: stock lines no_data, the core unaffected) and their
fundamentals facts from SEC EDGAR. The Alpaca Keychain items are read only when the policy has stock
lines. Budgets, breakers and caches live in the context's state directory. With stock lines, the
calendar also carries their earnings events (`council.stocks.earnings`: SEC 8-K Item 2.02 history,
overridden by the broker's news feed when the feed is on; the feed is fetched once per slot for both
the news role and the earnings), computed at the slot the cycle's event window belongs to.

News (`news_sources`, transparency-v2 §3; the user's decisions of 2026-09-26): every cycle reads the
public-domain sources (`council.data.gov_news`: Federal Reserve Board, BLS, BEA, TreasuryDirect,
EIA, and SEC 8-K / 6-K metadata for the held and shortlisted stock lines), in rehearsal and live.
The broker's feed (eToro Licensed Content) is added whenever an Agent Portfolio is connected and the
switch is on (`broker_feed_enabled`: `invariants.BROKER_FEED_ENABLED`, which is True, and
`policy/council.yaml` `news.broker_feed`, which can only turn it off). Its text may reach the news
role's prompt but is never published, and private copies are purged within 7 days. A broker that is
connected while the switch is off sets `news_broker_feed:off` and sends no feed request (the
earnings then keep the SEC estimate). `N:` ids are keyed by the private install key. A source that
fails becomes `news_source_error:<source>:<type>` (a 401/403 from the feed is `auth`) and never costs
another source's items: the news role still runs on what arrived.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from council import paths
from council.policy import Policy, default_policy, install_default_policy
from council.runtime import (
    POLICY_SNAPSHOTS,
    CycleContext,
    PolicyBlocker,
    PolicySnapshot,
    PolicySnapshotError,
    Sources,
    head_policy_snapshot,
    last_verified_snapshot,
    release_checkout,
)
from council.settings import Settings

PublishMode = Literal["none", "preview", "push"]
_ID = re.compile(r"\bF:[A-Z0-9_]+:[a-z0-9_]+\b")


def _first_fact_id(user: str) -> str:
    m = _ID.search(user)
    return m.group(0) if m else "F:NDX:trend"


def hold_reference_stub() -> dict[str, Any]:
    """Canned replies that keep every line at the reference: used by dry runs without a model
    and by tests. Every reply passes the real schemas and validation."""

    def advocate(user: str, _rep: int) -> dict[str, Any]:
        fid = _first_fact_id(user)
        return {"argument": "The reference already reflects the trend states in the pack.",
                "proposal": {}, "claims": [{"claim_id": "c1", "text": "Trend states are unchanged.",
                                             "evidence_ids": [fid]}],
                "strongest_opposing_fact_id": fid, "concessions": []}

    def bear(user: str, rep: int) -> dict[str, Any]:
        out = advocate(user, rep)
        out["rebuttals"] = []
        return out

    def pm(user: str, _rep: int) -> dict[str, Any]:
        return {"deviations": [], "decisive_fact": {"text": "No evidence justifies leaving the reference.",
                                                     "evidence_id": _first_fact_id(user)},
                "sided_with": "reference", "dismissed": [], "no_change_reason": "stub: hold reference"}

    return {
        "news": {"cards": []},
        "macro": {"regime": "neutral", "drivers": [], "sleeve_tilts": {}, "cards": []},
        "bull_open": advocate, "bear": bear, "bull_rebuttal": advocate,
        "pm": pm, "single_agent": pm,
    }


def make_gateway(policy: Policy, settings: Settings, *, stub: bool) -> Any:
    if stub:
        from council.llm.stub import StubGateway

        return StubGateway(hold_reference_stub())
    from council.llm.gateway import OllamaGateway

    c = policy.council
    return OllamaGateway(settings.ollama_host, settings.ollama_model,
                         calls_per_min=int(c["limiter"]["calls_per_min"]),
                         concurrency=int(c["limiter"]["concurrency"]),
                         timeouts=tuple(float(t) for t in c["timeouts_s"]),
                         num_ctx=int(c["num_ctx"]))


BROKER_FEED_SOURCE = "broker_feed"        # the source name in flags and fetch reports
FEED_TAKE = 50


def broker_feed_enabled(policy: Policy, broker: Any | None) -> bool:
    """May this cycle request the broker's news feed? Only with a connected broker, the code
    ceiling `invariants.BROKER_FEED_ENABLED`, and `news.broker_feed` not set false in the policy."""
    from council import invariants

    if broker is None or not bool(getattr(invariants, "BROKER_FEED_ENABLED", False)):
        return False
    council = policy.council if isinstance(policy.council, dict) else {}
    section = council.get("news")
    if section is None:
        return True
    return isinstance(section, dict) and section.get("broker_feed", True) is True


def feed_error_type(exc: BaseException) -> str:
    """A fixed code for a broker feed failure (never a message): `auth` for 401/403."""
    from council.broker.http import BrokerAuthError, BrokerHTTPError, BrokerUnavailable

    if isinstance(exc, BrokerAuthError):
        return "auth"
    status = getattr(exc, "status", None)
    if isinstance(exc, BrokerHTTPError) and isinstance(status, int):
        return "auth" if status in (401, 403) else f"http_{status}"
    if isinstance(exc, BrokerUnavailable):
        return "unavailable"
    return type(exc).__name__


def news_sources(policy: Policy, *, broker: Any | None = None, state_dir: Path | None = None,
                 public: Any | None = None, clock: Any | None = None) -> tuple[Any, Any | None]:
    """(news(slot) -> NewsFetch, broker_feed(slot) -> list[NewsItem] or None when the feed is off).

    `public(policy, now, state_dir, slot=slot)` fetches the public-domain items (default
    `gov_news.gather_public_news`; tests pass a fake); `clock()` is the fetch time for its skew rule
    (default: the wall clock). The broker feed is fetched at most once per slot and shared with the
    earnings override. `news` never raises: a failing source becomes a flag."""
    from council.data import gov_news
    from council.data.feeds import parse_news_feed, sort_news
    from council.data.gov_news import NewsFetch, SourceReport

    fetch_public = public or gov_news.gather_public_news
    wall = clock or (lambda: datetime.now(UTC))
    broker_feed = None
    if broker_feed_enabled(policy, broker):
        feed_memo: dict[datetime, list[Any] | Exception] = {}

        def broker_feed(slot: datetime) -> list[Any]:  # type: ignore[no-redef]
            # one feed request per slot, failure included: the earnings override (called first) and
            # the news role share it
            if slot not in feed_memo:
                from council.publish import install_key

                feed_memo.clear()
                try:
                    key = install_key.load_or_create(state_dir)
                    feed_memo[slot] = parse_news_feed(broker.feeds_news(take=FEED_TAKE), now=slot,
                                                      install_key=key)
                except Exception as exc:
                    feed_memo[slot] = exc
            held = feed_memo[slot]
            if isinstance(held, Exception):
                raise held
            return held

    def news(slot: datetime) -> NewsFetch:
        flags: list[str] = []
        reports: dict[str, SourceReport] = {}
        items: list[Any] = []
        try:
            fetched = fetch_public(policy, max(wall(), slot), state_dir, slot=slot)
            items += list(fetched.items)
            flags += list(fetched.flags)
            reports.update(fetched.sources)
        except Exception as exc:  # a public-news failure never stops the cycle
            flags.append(f"news_source_error:public:{type(exc).__name__}")
        if broker is not None and broker_feed is None:
            flags.append("news_broker_feed:off")
        elif broker_feed is not None:
            try:
                feed = broker_feed(slot)
                items += feed
                reports[BROKER_FEED_SOURCE] = SourceReport(source=BROKER_FEED_SOURCE, feeds_ok=1,
                                                           entries=len(feed), kept=len(feed))
            except Exception as exc:  # the news role still runs on the public items
                kind = feed_error_type(exc)
                flags.append(f"news_source_error:{BROKER_FEED_SOURCE}:{kind}")
                reports[BROKER_FEED_SOURCE] = SourceReport(source=BROKER_FEED_SOURCE, feeds_failed=1,
                                                           error=kind)
        unique: dict[str, Any] = {}
        for item in sort_news(items):
            unique.setdefault(item.id, item)
        return NewsFetch(items=list(unique.values()), flags=list(dict.fromkeys(flags)), sources=reports)

    return news, broker_feed


def data_sources(policy: Policy, *, broker: Any | None = None, state_dir: Path | None = None,
                 public_news: Any | None = None) -> Sources:
    from council.data.cache import FileCache
    from council.data.calendar import load_events
    from council.data.credentials import secret
    from council.facts.market import gather_history, gather_macro

    token = secret("council-book.tiingo")
    fred_key = secret("council-book.fred")
    has_stocks = bool(policy.universe.stock_lines())
    stock_keys = None
    if has_stocks:
        from council.data.alpaca import load_keys

        stock_keys = load_keys()

    def history(slot):
        cache = FileCache("history", root=state_dir / "cache") if state_dir is not None else None
        return gather_history(policy, now=slot, tiingo_token=token, alpaca_keys=stock_keys, cache=cache,
                              state_dir=state_dir)

    fundamentals = None
    if has_stocks:
        def fundamentals(slot):  # type: ignore[no-redef]
            from council.stocks.fundamentals import gather_fundamentals

            return gather_fundamentals(policy, now=slot, state_dir=state_dir)

    news, broker_feed = news_sources(policy, broker=broker, state_dir=state_dir, public=public_news)

    earnings = None
    if has_stocks:
        def earnings(start, end):  # type: ignore[no-redef]
            from council.stocks.earnings import gather_earnings

            slot = events_slot(start, end)
            items: list[Any] = []
            feed_flags: list[str] = []
            if broker_feed is not None:          # the feed off: the SEC estimate alone
                try:
                    items = broker_feed(slot)
                except Exception as exc:  # the override is optional: the SEC estimate still applies
                    feed_flags = [f"earnings_feed_failed:{type(exc).__name__}"]
            found, flags = gather_earnings(policy, slot=slot, start=start, end=end, news=items,
                                           state_dir=state_dir)
            return found, feed_flags + flags

    def events(start, end):
        return load_events(policy, start, end, fred_key=fred_key, earnings=earnings)

    def macro(slot):
        return gather_macro(now=slot)

    return Sources(history=history, events=events, macro=macro, news=news, broker=broker,
                   fundamentals=fundamentals)


def events_slot(start: datetime, end: datetime) -> datetime:
    """The slot whose event window (`runtime.window`) is [start, end]: what the stock earnings are
    computed at (their visibility cutoff). For any other window, `start`: an earlier cutoff sees
    fewer filings, so an earnings estimate stays in force longer, never shorter."""
    from council.runtime import window

    slot = start + (start - window(start)[0])
    return slot if window(slot) == (start, end) else start


ONBOARDED_REL = ("account", "onboarded.json")   # written by the operator's onboarding (M5-C)


def is_onboarded(state_dir: Path) -> bool:
    """True once the Agent Portfolio was onboarded (`<state dir>/account/onboarded.json`). From
    then on a missing broker is a failure (`skipped_broker` + URGENT `keychain_unavailable`), never
    AWAITING_ACCOUNT again."""
    try:
        return state_dir.joinpath(*ONBOARDED_REL).is_file()
    except OSError:
        return False


def broker_expected(ctx: Any) -> bool:
    """A live cycle/watch after onboarding must have a broker."""
    return getattr(ctx.settings, "mode", "") == "live" and is_onboarded(ctx.state_dir)


def read_broker(settings: Settings) -> Any | None:
    """The READ-only Agent Portfolio client, or None when the account is not connected yet."""
    from council.operator.keychain import API_KEY_SERVICE, READ_SERVICE, read_secret

    try:
        api_key = read_secret(API_KEY_SERVICE)
        token = read_secret(READ_SERVICE)
    except Exception:
        return None
    if not api_key or not token:
        return None
    from council.broker.etoro_read import EtoroReadClient
    from council.broker.http import BrokerConfigError

    try:
        return EtoroReadClient(api_key, token, base_url=settings.etoro_base_url)
    except BrokerConfigError:
        # the base URL is outside the pin: no client, no socket; every read fails with the same
        # fixed error so the cycle records `skipped_broker` and alerts (never AWAITING_ACCOUNT)
        return UnavailableBroker("config")
    finally:
        del api_key, token


class UnavailableBroker:
    """Stands in for a broker that must exist but cannot be built (e.g. a refused base URL).
    Every read raises `BrokerConfigError` without any network access."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __repr__(self) -> str:
        return f"UnavailableBroker({self.kind!r})"

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        from council.broker.http import BrokerConfigError

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise BrokerConfigError(f"broker unavailable ({self.kind})")

        return refuse


def load_policy(root: Path, *, from_head: bool, fallback: bool = False,
                repo: Path | None = None) -> tuple[Policy, PolicySnapshot | None]:
    """The policy a context runs on.

    `from_head`: the snapshot of `HEAD:policy/` in `repo` (default: this checkout), installed as
    this process's `default_policy()` so no module reads the working tree. If git fails and
    `fallback` is set, the last verified snapshot of the same checkout, carrying a whole-book
    `policy_snapshot_unavailable` blocker; with no verified snapshot the `PolicySnapshotError`
    propagates. Otherwise: `default_policy()`, the working-tree `policy/` loaded once per process
    (dry runs, rehearsal, stub, operator commands). A committed stock sleeve is merged only once
    `invariants.STOCK_SLEEVE_LIVE` is True."""
    from council import invariants

    snapshot = None
    if from_head:
        include = bool(invariants.STOCK_SLEEVE_LIVE)
        try:
            snapshot = head_policy_snapshot(root, repo=repo, include_sleeve=include)
        except PolicySnapshotError as exc:
            if not fallback:
                raise
            snapshot = last_verified_snapshot(root, repo=repo, include_sleeve=include, reason=str(exc))
            if snapshot is None:
                raise
    policy = snapshot.policy if snapshot is not None else default_policy()
    invariants.check_policy(policy)
    if not invariants.STOCK_SLEEVE_LIVE and policy.universe.stock_lines():
        raise invariants.InvariantViolation("stock lines reached a runtime policy before the go-live switch "
                                            "(invariants.STOCK_SLEEVE_LIVE is False)")
    if snapshot is not None:
        install_default_policy(policy)
    return policy, snapshot


# One URGENT alert per kind per window: the watch runs every 15 minutes and the cycle hourly.
POLICY_ALERT_EVERY = timedelta(hours=4)
_POLICY_ALERTS = "alerts.json"
_POLICY_ALERT_TEXT = {
    "held": ("Council: live policy snapshot unavailable",
             "git could not read the committed policy. Live runs continue on the last verified policy "
             "with every line held; the kill switch, stops and flatten proposals still run. Fix git on "
             "the runner (for example: xcode-select --install, or the Homebrew git)."),
    "stopped": ("Council: live runs stopped",
                "git could not read the committed policy and no verified policy snapshot exists. Live "
                "cycles and the watch refuse to start: no kill switch, no stop checks. Fix git on the "
                "runner now."),
}


def alert_policy_snapshot(root: Path, settings: Settings, kind: Literal["held", "stopped"], *,
                          now: datetime | None = None) -> bool:
    """URGENT ntfy when a live run cannot snapshot the committed policy (at most one per kind per
    `POLICY_ALERT_EVERY`). The message is fixed text: no path, commit or amount. Never raises: an
    alert failure must not mask the fallback or the original error. True when an alert went out."""
    from council.clock import utcnow

    if not settings.ntfy_topic:
        return False
    now = now or utcnow()
    record = root / POLICY_SNAPSHOTS / _POLICY_ALERTS
    try:
        sent = json.loads(record.read_text()) if record.is_file() else {}
        last = datetime.fromisoformat(sent[kind]) if isinstance(sent, dict) and kind in sent else None
    except (OSError, ValueError, TypeError):
        sent, last = {}, None
    if last is not None and now - last < POLICY_ALERT_EVERY:
        return False
    try:
        from council.operator.notify import Notifier

        title, body = _POLICY_ALERT_TEXT[kind]
        notifier = Notifier(settings.ntfy_topic, window={"tz": "UTC", "start": "00:00", "end": "00:00"})
        notifier.send(title, body, priority="urgent")
    except Exception as exc:          # reported, never raised (see the docstring)
        print(f"policy snapshot alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({**(sent if isinstance(sent, dict) else {}), kind: now.isoformat()}) + "\n")
    except OSError:
        pass
    return True


def eligibility_blockers(policy: Policy) -> tuple[PolicyBlocker, ...]:
    """The runtime refusal of stock lines never eligibility-checked (a rank run with
    `--no-eligibility` stamps `eligibility_checked_at: null`; design §4.3): one satellite-scoped
    `stock_eligibility_unchecked` blocker when any stock line is unchecked, so the stock sleeve is
    held and the core keeps running. The line ids go only into the private `detail`; the code is what
    reaches R20 and the public record (`cycle.engine_blockers`). Empty for a core-only policy."""
    from council.stocks.eligibility import UNCHECKED, preflight_errors

    unchecked = preflight_errors(policy)
    if not unchecked:
        return ()
    lines = ", ".join(u.rsplit(":", 1)[-1] for u in unchecked)
    return (PolicyBlocker(UNCHECKED, "satellite",
                          f"stock lines never eligibility-checked: {lines}; re-rank with the broker gate"),)


def build_context(*, mode: Literal["live", "dry_run", "stub"], stub_llm: bool = False,
                  publish: PublishMode = "preview", sources: Sources | None = None,
                  state_dir: Path | None = None, settings: Settings | None = None,
                  publisher_dir: Path | None = None, policy_from_head: bool | None = None,
                  policy_fallback: bool | None = None) -> CycleContext:
    """`policy_from_head` (default: `mode == "live"`) loads policy from the committed HEAD, never
    the working tree: the runner's own checkout in live mode; for the operator's approval path, the
    installed release (`<state dir>/releases/current`, what the launchd jobs run) when there is one.
    `policy_fallback` (default: `mode == "live"`) lets a git failure fall back to the last verified
    snapshot with a whole-book `policy_snapshot_unavailable` blocker in `CycleContext.
    policy_blockers`; a live context also sends an URGENT alert, and before refusing to start when
    no verified snapshot exists. Once the sleeve is live (`invariants.STOCK_SLEEVE_LIVE`), a sleeve
    that is not the blob at its `stocks-<quarter>` tag yields a satellite-scoped
    `sleeve_policy_untagged` blocker, and a stock line whose eligibility was never checked a
    satellite-scoped `stock_eligibility_unchecked` one (`eligibility_blockers`, every mode)."""
    from council.ledger.db import Ledger
    from council.llm.prompts import PromptRegistry

    settings = settings or Settings.from_env()
    root = state_dir or paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    paths.assert_outside_repo(root)
    from_head = mode == "live" if policy_from_head is None else policy_from_head
    fallback = mode == "live" if policy_fallback is None else policy_fallback
    repo = paths.REPO_ROOT if mode == "live" else (release_checkout(root) or paths.REPO_ROOT)
    try:
        policy, snapshot = load_policy(root, from_head=from_head, fallback=fallback, repo=repo)
    except PolicySnapshotError:
        if mode == "live":
            alert_policy_snapshot(root, settings, "stopped")
        raise
    if snapshot is not None and snapshot.fallback and mode == "live":
        alert_policy_snapshot(root, settings, "held")
    ledger = Ledger(root / "ledger.sqlite3")
    ledger.migrate()

    broker = None
    if sources is None:
        if mode == "live" or (mode == "dry_run" and settings.agent_portfolio_id):
            broker = read_broker(settings)
        sources = data_sources(policy, broker=broker, state_dir=root)

    publisher = None
    if publish == "preview":
        from council.publish.gitops import Publisher

        preview = paths.REPO_ROOT / "site-preview"
        publisher = Publisher(root / "publisher-clone", push=False, dry_run_dir=preview)
    elif publish == "push":
        from council.publish.gitops import Publisher

        clone = publisher_dir or (root / "publisher-clone")
        publisher = Publisher(clone, push=True, ssh_command=_ssh_command(clone.parent))
        warning = deploy_key_warning(clone)
        if warning:
            print(warning, file=sys.stderr)     # fixed text: never the URL (it may carry a token)

    notifier = None
    if mode == "live" and settings.ntfy_topic:
        from council.operator.notify import Notifier

        notifier = Notifier(settings.ntfy_topic)

    return CycleContext(policy=policy, settings=settings, ledger=ledger,
                        gateway=make_gateway(policy, settings, stub=stub_llm or mode == "stub"),
                        registry=PromptRegistry(), sources=sources, publisher=publisher,
                        notifier=notifier, state_dir=root,
                        run_single_agent=bool(policy.council["roles"]["single_agent_control"]["enabled"]),
                        policy_commit=snapshot.commit if snapshot is not None else "",
                        policy_blockers=(*(snapshot.blockers if snapshot is not None else ()),
                                         *eligibility_blockers(policy)))


def _ssh_command(root: Path) -> str | None:
    """GIT_SSH_COMMAND for the deploy key (G24: the path is shell-quoted, since git runs it through
    a shell and the state dir, `Application Support`, contains a space)."""
    import shlex

    key = root / "deploy_key"
    if key.exists():
        return f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes"
    return None


DEPLOY_KEY_HTTPS_WARNING = ("warning: a deploy key exists but the publisher remote is HTTPS, so the "
                            "key is unused; set the remote to the SSH URL (git@github.com:...)")


def deploy_key_warning(clone: Path) -> str | None:
    """G24: the fixed warning when a deploy key exists but the clone's `origin` is an HTTPS URL
    (the key would silently be ignored). None otherwise, or when the remote cannot be read."""
    import subprocess

    if not (clone.parent / "deploy_key").exists() or not (clone / ".git").exists():
        return None
    try:
        url = subprocess.run(["git", "-C", str(clone), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except Exception:  # noqa: BLE001 - a warning only
        return None
    return DEPLOY_KEY_HTTPS_WARNING if url.lower().startswith(("https://", "http://")) else None
