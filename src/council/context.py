"""Build a CycleContext for the unattended runner, a dry run, or an offline stub run.

  live     — READ-only Agent Portfolio token from the login keychain, real data, real LLM, push.
  dry_run  — no broker (or the read token if present), real data and LLM, publishes to
             site-preview/ only (never pushes, never notifies).
  stub     — no network at all: canned LLM replies and caller-supplied data sources (tests).

The WRITE token is never loaded here; only council.operator.approve may load it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from council import paths
from council.policy import Policy
from council.runtime import CycleContext, Sources
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


def data_sources(policy: Policy, *, broker: Any | None = None) -> Sources:
    from council.data.calendar import load_events
    from council.data.credentials import secret
    from council.data.feeds import parse_news_feed
    from council.facts.market import gather_history, gather_macro

    token = secret("council-book.tiingo")
    fred_key = secret("council-book.fred")

    def history(slot):
        return gather_history(policy, now=slot, tiingo_token=token)

    def events(start, end):
        return load_events(policy, start, end, fred_key=fred_key)

    def macro(slot):
        return gather_macro(now=slot)

    news = None
    if broker is not None:
        def news(slot):  # type: ignore[no-redef]
            return parse_news_feed(broker.feeds_news(take=50), now=slot)

    return Sources(history=history, events=events, macro=macro, news=news, broker=broker)


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


def build_context(*, mode: Literal["live", "dry_run", "stub"], stub_llm: bool = False,
                  publish: PublishMode = "preview", sources: Sources | None = None,
                  state_dir: Path | None = None, settings: Settings | None = None) -> CycleContext:
    from council.invariants import check_policy
    from council.ledger.db import Ledger
    from council.llm.prompts import PromptRegistry

    settings = settings or Settings.from_env()
    policy = Policy.load()
    check_policy(policy)
    root = state_dir or paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    paths.assert_outside_repo(root)
    ledger = Ledger(root / "ledger.sqlite3")
    ledger.migrate()

    broker = None
    if sources is None:
        if mode == "live" or (mode == "dry_run" and settings.agent_portfolio_id):
            broker = read_broker(settings)
        sources = data_sources(policy, broker=broker)

    publisher = None
    if publish == "preview":
        from council.publish.gitops import Publisher

        preview = paths.REPO_ROOT / "site-preview"
        publisher = Publisher(root / "publisher-clone", push=False, dry_run_dir=preview)
    elif publish == "push":
        from council.publish.gitops import Publisher

        publisher = Publisher(root / "publisher-clone", push=True,
                              ssh_command=_ssh_command(root))

    notifier = None
    if mode == "live" and settings.ntfy_topic:
        from council.operator.notify import Notifier

        notifier = Notifier(settings.ntfy_topic)

    return CycleContext(policy=policy, settings=settings, ledger=ledger,
                        gateway=make_gateway(policy, settings, stub=stub_llm or mode == "stub"),
                        registry=PromptRegistry(), sources=sources, publisher=publisher,
                        notifier=notifier, state_dir=root,
                        run_single_agent=bool(policy.council["roles"]["single_agent_control"]["enabled"]))


def _ssh_command(root: Path) -> str | None:
    key = root / "deploy_key"
    if key.exists():
        return f"ssh -i {key} -o IdentitiesOnly=yes"
    return None
