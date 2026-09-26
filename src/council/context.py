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
overridden by the broker's news feed when a broker is connected; the feed is fetched once per slot
for both the news role and the earnings), computed at the slot the cycle's event window belongs to.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from council import paths
from council.policy import Policy, default_policy, install_default_policy
from council.runtime import (
    POLICY_SNAPSHOTS,
    CycleContext,
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


def data_sources(policy: Policy, *, broker: Any | None = None, state_dir: Path | None = None) -> Sources:
    from council.data.cache import FileCache
    from council.data.calendar import load_events
    from council.data.credentials import secret
    from council.data.feeds import parse_news_feed
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

    news = None
    if broker is not None:
        feed_memo: dict[datetime, list[Any]] = {}

        def news(slot):  # type: ignore[no-redef]
            # one feed request per slot: the earnings override (called first) and the news role share it
            if slot not in feed_memo:
                items = parse_news_feed(broker.feeds_news(take=50), now=slot)
                feed_memo.clear()
                feed_memo[slot] = items
            return feed_memo[slot]

    earnings = None
    if has_stocks:
        def earnings(start, end):  # type: ignore[no-redef]
            from council.stocks.earnings import gather_earnings

            slot = events_slot(start, end)
            items: list[Any] = []
            feed_flags: list[str] = []
            if news is not None:
                try:
                    items = news(slot)
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

    return EtoroReadClient(api_key, token, base_url=settings.etoro_base_url)


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
    `sleeve_policy_untagged` blocker."""
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
                        policy_blockers=snapshot.blockers if snapshot is not None else ())


def _ssh_command(root: Path) -> str | None:
    key = root / "deploy_key"
    if key.exists():
        return f"ssh -i {key} -o IdentitiesOnly=yes"
    return None
