"""`council doctor --ready`: the readiness report (m5-readiness §6, work package M5-H).

Every gate is a pure function over injected probes (`Probes`) and returns a state, a code and a
short detail. Each gate carries an `owner`:

- `agent`: code in this repository; the loop may build it;
- `user`: the machine, accounts, keys, loaded jobs, the state dir; the loop only reports it;
- `token`: needs the Agent Portfolio. Before the token these gates show `wait` ("awaiting token");
  with `--post-token` they are evaluated from the readiness records and must be green.

States: `green`; `amber` (accepted unless the gate says otherwise, for example E2 without the
`power-ok` attestation); `red`; `wait`. A gate whose work package has not landed shows
`red — not built: M5-x`. A package counts as landed when every test node id the ACCEPTANCE
registry names for it exists in the checkout (CI job `m5-acceptance` runs exactly those ids).

Exit codes: 0 when every gate is green or accepted amber and the token gates wait (or are green with
`--post-token`); 1 on any red or unaccepted amber; 2 on an internal error.

Read-only. It never reads a secret value: keychain presence uses `security find-generic-password`
without `-w` (the runner refuses `-w` and `-g`). It never imports or calls the broker or an LLM
client. It reads codes from `state_dir/readiness/*.json`, the ledger (opened read-only), the release
checkout and the repository files, and with `--network` `git ls-remote` and the CI check runs of the
checkout's HEAD through `gh`. It never reads `calls/`, `transcripts/`, `licensed/` or `backups/`.

Records (`state_dir/readiness/<name>.json`, 0600) hold codes, booleans, timestamps and shas, never an
amount or an identifier: `{"at": iso, "head": sha, "gates": {"K7": {"state": "green", "code":
"rates_ok"}}, "attested": {"power-ok": {"value": true, "at": iso}}}`. Only guarded operator
commands write them (`write_record` asserts the operator context and the installed release), except
`soak.json`, which the launchd rehearsal watch writes as the runner.
"""

from __future__ import annotations

import ast
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from council import paths

SCHEMA_VERSION = 1
OWNERS = ("agent", "user", "token")
STATES = ("green", "amber", "red", "wait")
RECORD_STATES = ("green", "amber", "red")
TRACKS = ("core", "stocks")
TRACK_LABEL = {"core": "Track C", "stocks": "Track S"}

REPO_SLUG = "fbzz/council-book"
ORIGIN_URL = f"https://github.com/{REPO_SLUG}.git"
CI_ACCEPTANCE_JOB = "m5-acceptance"
RELEASE_TAG = re.compile(r"^council-spec-v\d+(?:\.\d+)*$")
SECURITY = "/usr/bin/security"
LEDGER_FILE = "ledger.sqlite3"          # council.ledger.db.LEDGER_FILE (not imported: keep this path light)
LAUNCHD_LABEL_PREFIX = "com.fbzz.council."
REHEARSAL_LABEL_PREFIX = "com.fbzz.council.rehearsal."
LIVE_JOBS = ("com.fbzz.council.cycle", "com.fbzz.council.watch")
FILLED_LEG_STATES = ("filled", "partially_filled", "rejected_partial")

# Ledger runtime keys the ops hooks write (M5-E1 backup, M5-E2 ping): codes, booleans, timestamps.
RUNTIME_HEALTHCHECK = "ops.healthcheck"   # {"at": iso, "ok": bool}
RUNTIME_BACKUP = "ops.backup"             # {"at": iso, "licensed_free": bool}

GIB = 1024**3
DISK_GREEN_GIB = 10.0
DISK_RED_GIB = 3.0
PING_MAX_AGE = timedelta(minutes=30)
BACKUP_MAX_AGE = timedelta(hours=26)
WRITE_KEYCHAIN_MAX_LOCK_S = 60

PROXY_OR_CERT_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                     "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
BASE_URL_ENV = "COUNCIL_ETORO_BASE_URL"
# plist variables that must never be set: secrets, URLs, proxies, certificate overrides
_PLIST_FORBIDDEN_NAME = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|_KEY$|_URL$|PROXY|SSL_CERT|CA_BUNDLE|CERT)",
                                   re.IGNORECASE)

# Keychain items (presence only). E4: required per track; `fred` is optional (info).
KEYCHAIN_CORE = ("council-book.tiingo", "council-book.sec-user-agent", "council-book.soak-probe")
KEYCHAIN_STOCKS = ("council-book.alpaca-key-id", "council-book.alpaca-secret")
KEYCHAIN_OPTIONAL = ("council-book.fred",)
NTFY_ITEM = "council-book.ntfy-topic"
HEALTHCHECK_ITEM = "council-book.healthcheck-url"
WRITE_SERVICE = "council-book.etoro.write"            # council.operator.keychain.WRITE_SERVICE
WRITE_KEYCHAIN_NAME = "council-write.keychain-db"     # council.operator.keychain.WRITE_KEYCHAIN_NAME

ATTEST_ITEMS = ("mirror-copied", "mirror-sl-equal", "fee-charged-on", "copy-stop-loss", "ntfy-received",
                "tiingo-dedicated", "power-ok", "w8ben-na", "token-scopes", "terms-version", "etoro-licence")

_SHA = re.compile(r"^[0-9a-f]{40}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z0-9_-]+)?$")
_LONG_DIGITS = re.compile(r"\d{4,}")


class ReadinessError(RuntimeError):
    """A readiness record was refused (wrong writer, bad schema). Never carries a value."""


# ------------------------------------------------------------------------------------ registry
@dataclass(frozen=True)
class Package:
    """A work package's named acceptance tests. `acceptance`: listed by R6 (and run by CI job
    `m5-acceptance`); `track`: "core" packages are required on both tracks."""

    what: str
    tests: tuple[str, ...]
    track: str = "core"
    acceptance: bool = True


# Node ids per package. A package has landed when every one of its node ids exists in the checkout.
# Entries for packages that have not landed name the files their design section names, or a proposed
# file: the landing package confirms or renames its entry here, in the same change.
ACCEPTANCE: dict[str, Package] = {
    "M5-0": Package("keys store-read CLI test", (
        "tests/cli/test_keys_cli.py::test_store_read_exits_0_and_stores_both_read_items_in_the_default_keychain",)),
    "M5-A": Package("broker-failure containment, base-URL pin, transport", (
        "tests/boundaries/test_broker_transport.py", "tests/integration/test_broker_containment.py")),
    "M5-B": Package("operator commands, operator context, release pinning", (
        "tests/cli/test_operator_commands.py", "tests/execution/test_operator_recovery.py",
        "tests/boundaries/test_deny_rules.py")),
    "M5-C": Package("onboarding commands", (
        "tests/operator/test_onboarding.py", "tests/contract/test_private_fixtures.py")),
    "M5-D1": Package("capability gates", ("tests/operator/test_capabilities.py",)),
    "M5-D2": Package("smoke tickets", ("tests/operator/test_smoke.py",)),
    "M5-E1": Package("ops wiring: keychain fallback, backup, notify test", (
        "tests/operator/test_backup.py", "tests/unit/test_settings_keychain_fallback.py",
        "tests/cli/test_ops_wiring_cli.py", "tests/operator/test_ops_canary.py")),
    "M5-E2": Package("ops hooks: healthcheck ping, daily backup", (
        "tests/operator/test_ops_hooks.py", "tests/operator/test_healthcheck.py")),
    "M5-F": Package("launchd install and soak", ("tests/boundaries/test_install_script.py",)),
    "M5-G": Package("onboarding rehearsal (L1)", ("tests/rehearsal/test_onboarding_rehearsal.py",)),
    "M5-H": Package("council doctor --ready", ("tests/operator/test_readiness.py",)),
    "M5-I": Package("CI supply chain", (
        "tests/boundaries/test_ci_config.py::test_every_action_is_pinned_to_a_full_commit_sha",
        "tests/boundaries/test_ci_config.py::test_dependabot_bumps_the_pinned_actions_weekly",
        "tests/boundaries/test_ci_config.py::test_gitleaks_tarball_is_verified_before_it_is_unpacked")),
    "M5-J": Package("runbook v2 and docs", ("tests/boundaries/test_runbook.py",)),
    "M5-K": Package("operator why screen", ("tests/operator/test_show_trail.py",)),
    "M5-M": Package("licensed-content controls", ("tests/operator/test_licensed.py",)),
    "M5-N": Package("NAV side channels", ("tests/publish/test_nav_invariance.py",)),
    "T1": Package("private input capture", ("tests/council/test_input_capture.py",)),
    "T3": Package("public-domain news", ("tests/data/test_gov_news.py", "tests/integration/test_news_wiring.py")),
    "T5a": Package("why trails", ("tests/operator/test_why.py",)),
    # Track S
    "M5-L": Package("Track S rehearsal variant", ("tests/rehearsal/test_onboarding_rehearsal_stocks.py",),
                    track="stocks"),
    "WP-J": Package("reference sleeve live", ("tests/reference/test_sleeve_live.py",),
                    track="stocks", acceptance=False),
    "WP-K": Package("council authority", ("tests/risk/test_council_authority.py",), track="stocks", acceptance=False),
    "WP-L": Package("stock prompts and roles", ("tests/council/test_stock_roles.py",),
                    track="stocks", acceptance=False),
    "WP-M": Package("stock public models and site", ("tests/publish/test_nav_invariance_sleeve.py",),
                    track="stocks", acceptance=False),
    "T2": Package("public inputs", ("tests/publish/test_public_inputs.py",), track="stocks", acceptance=False),
    "T6": Package("transparency site pages", ("tests/site/test_transparency_pages.py",),
                  track="stocks", acceptance=False),
}


@dataclass(frozen=True)
class RecordSpec:
    writer: str
    gates: tuple[str, ...]
    attestations: tuple[str, ...] = ()
    runner: bool = False            # written by the launchd rehearsal watch, not by an operator command


RECORDS: dict[str, RecordSpec] = {
    "soak": RecordSpec("M5-F: the launchd rehearsal watch (runner)", ("O3", "T1", "T2", "T3"), runner=True),
    "dress": RecordSpec("M5-G: council ops record-dress", ("O5",)),
    "keys": RecordSpec("M5-C: council keys verify", ("K1", "K2", "K3", "K4")),
    "live-read": RecordSpec("M5-C: council doctor --live-read",
                            ("K5", "K6", "K7", "K8", "K9", "K10", "K11", "K12", "K13", "K17", "K19", "P2")),
    "smoke": RecordSpec("M5-D2: council smoke verify / status", ("K14", "K20", "S3")),
    "attest": RecordSpec("M5-D1: council ops attest", ("K15",), attestations=ATTEST_ITEMS),
    "stocks": RecordSpec("Track S: council stocks onboard", ("S4",)),
}

# §9.1 operator commands -> release-pinned. A conditional variant ("doctor --live-read") is checked
# when its base command and option exist in cli.py. `inputs *` register from their own module.
REQUIRED_OPERATOR: dict[str, bool] = {
    "inbox": False, "show": False, "approve": True, "reject": False,
    "resume-exec": True, "ops resolve": True, "ops review": True, "resume": True,
    "keys init-write-keychain": True, "keys store-read": True, "keys store-write": True,
    "keys verify": True, "keys store": False,
    "doctor --live-read": True, "doctor --record-fixtures": True, "instruments resolve": True,
    "account set-mirror": False,
    "smoke propose": True, "smoke verify": True, "smoke status": True,
    "ops attest": True, "ops capabilities": True, "ops record-dress": True,
    "purge-licensed": True,
    "stocks rank": True, "stocks onboard": True, "stocks adopt": True, "stocks status": True,
    "stocks prune": True,
}
AGENT_RULE_FILES = ("CLAUDE.md", "AGENTS.md")
AGENT_RULE_TOKENS = (
    "inbox", "show", "inputs", "approve", "reject", "resume-exec", "ops resolve", "ops review", "resume",
    "init-write-keychain", "store-read", "store-write", "verify", "--live-read", "--record-fixtures",
    "instruments resolve", "set-mirror", "smoke", "attest", "capabilities", "record-dress",
    "purge-licensed", "onboard", "adopt", "prune", "stocks rank",
    "ops/install.sh", "ops/uninstall.sh", "ops/rehearse-onboarding.sh",
    # state dir: no writes under these ...
    "readiness", "account", "salts", "releases",
    # ... and no reads of these
    "calls/", "transcripts/", "licensed/", "backups/",
)
GUARDED_SCRIPTS = ("ops/install.sh", "ops/uninstall.sh", "ops/rehearse-onboarding.sh")


# ------------------------------------------------------------------------------------- results
@dataclass(frozen=True)
class Result:
    state: str
    code: str
    detail: str = ""
    accepted: bool = True           # meaningful for amber only

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")


def green(code: str, detail: str = "") -> Result:
    return Result("green", code, detail)


def amber(code: str, detail: str = "", *, accepted: bool = True) -> Result:
    return Result("amber", code, detail, accepted)


def red(code: str, detail: str = "") -> Result:
    return Result("red", code, detail)


@dataclass(frozen=True)
class Gate:
    id: str
    owner: str
    title: str
    check: Callable[[Probes], Result]
    wp: tuple[str, ...] = ("M5-H",)
    track: str = "core"             # "core" gates run on both tracks
    hint: str = ""                  # the token-day step, shown while the gate waits

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ValueError(f"gate {self.id}: unknown owner {self.owner!r}")


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    result: Result

    @property
    def unaccepted(self) -> bool:
        return self.result.state == "amber" and not self.result.accepted

    def as_json(self) -> dict[str, Any]:
        return {"id": self.gate.id, "owner": self.gate.owner, "state": self.result.state,
                "code": self.result.code, "wp": "+".join(self.gate.wp), "accepted": self.result.accepted,
                "title": self.gate.title, "detail": self.result.detail}


# -------------------------------------------------------------------------------------- probes
@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., Completed]


def default_run(argv: Sequence[str], *, timeout: float = 20.0, env: Mapping[str, str] | None = None) -> Completed:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False,
                              env=dict(env) if env is not None else None)
    except FileNotFoundError:
        return Completed(127, "", "not found")
    except subprocess.TimeoutExpired:
        return Completed(124, "", "timeout")
    except OSError as exc:
        return Completed(126, "", type(exc).__name__)
    return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class ReleaseInfo:
    exists: bool
    git_ok: bool = False
    dirty: bool = False
    head: str = ""
    tags: tuple[str, ...] = ()
    recorded: str | None = None


@dataclass(frozen=True)
class PlistInfo:
    name: str
    env: Mapping[str, str] = field(default_factory=dict)
    program: tuple[str, ...] = ()
    workdir: str = ""
    error: bool = False


@dataclass(frozen=True)
class WriteKeychainInfo:
    exists: bool
    on_search_list: bool = False
    lock_timeout_s: int | None = None


@dataclass(frozen=True)
class PublisherInfo:
    exists: bool
    clean: bool = False
    origin: str = ""
    deploy_key: bool = False
    ls_remote_ok: bool | None = None


@dataclass(frozen=True)
class LedgerSummary:
    exists: bool
    readable: bool = True
    decisions: int = 0
    nav_state: bool = False
    fills: int = 0
    published: int = 0              # non-smoke decisions with a published commit (K18)


INVALID = object()                  # a record that exists but fails the schema


class Probes:
    """Everything the gates read. Each method is a seam: tests override it; the defaults read the
    machine through `run` (subprocess), the file system and the ledger (read-only)."""

    def __init__(self, *, state_dir: Path, repo: Path, home: Path, env: Mapping[str, str], now: datetime,
                 network: bool = False, run: Runner | None = None) -> None:
        self.state_dir = state_dir
        self.repo = repo
        self.home = home
        self.env = dict(env)
        self.now = now
        self.network = network
        self._run = run if run is not None else default_run
        self._cache: dict[str, Any] = {}

    @classmethod
    def default(cls, *, network: bool = False) -> Probes:
        return cls(state_dir=paths.state_dir(), repo=paths.REPO_ROOT, home=Path.home(), env=dict(os.environ),
                   now=datetime.now(UTC), network=network)

    # -- the runner: every subprocess goes through here
    def run(self, argv: Sequence[str], *, timeout: float = 20.0) -> Completed:
        argv = [str(a) for a in argv]
        # getopt also takes clustered flags (`-gw`, `-wa`): refuse any short-option cluster with w or g
        if argv and Path(argv[0]).name == "security" and any(
                re.fullmatch(r"-[A-Za-z]+", a) and ("w" in a or "g" in a) for a in argv[1:]):
            raise ReadinessError("readiness never reads a keychain value")
        return self._run(argv, timeout=timeout)

    def git(self, root: Path, *args: str, timeout: float = 20.0) -> Completed:
        return self.run(["git", "-C", str(root), *args], timeout=timeout)

    def _memo(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # -- places
    @property
    def release_dir(self) -> Path:
        return self.state_dir / "releases" / "current"

    @property
    def real_state_dir(self) -> Path:
        return self.home / "Library" / "Application Support" / "council-book"

    def is_sandbox(self) -> bool:
        marker = self.state_dir / "REHEARSAL"
        try:
            return marker.is_file() and self.state_dir.resolve() != self.real_state_dir.resolve()
        except OSError:
            return False

    def on_release(self) -> bool:
        try:
            return self.repo.resolve() == self.release_dir.resolve()
        except OSError:
            return False

    # -- repository
    def node_exists(self, node: str) -> bool:
        def get() -> bool:
            file, _, name = node.partition("::")
            path = self.repo / file
            if not path.is_file():
                return False
            if not name:
                return True
            try:
                text = path.read_text()
            except OSError:
                return False
            return re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", text, re.MULTILINE) is not None
        return self._memo(f"node:{node}", get)

    def landed(self, wp: str) -> bool:
        package = ACCEPTANCE.get(wp)
        if package is None:
            return True
        return all(self.node_exists(node) for node in package.tests)

    def head(self) -> str:
        def get() -> str:
            out = self.git(self.repo, "rev-parse", "HEAD")
            sha = out.stdout.strip().lower() if out.returncode == 0 else ""
            return sha if _SHA.match(sha) else ""
        return self._memo("head", get)

    def workflow_uses(self) -> list[str]:
        refs: list[str] = []
        wf = self.repo / ".github" / "workflows"
        for path in sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")]):
            for line in path.read_text().splitlines():
                m = re.match(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)", line)
                if m:
                    refs.append(m.group(1))
        return refs

    def policy_prompts(self) -> Result:
        # council.llm.prompts is the prompt FILE registry (jinja2 + hashes), not an LLM client
        from council.invariants import check_policy
        from council.llm.prompts import PromptRegistry
        from council.policy import Policy

        try:
            policy = Policy.load(self.repo / "policy", include_sleeve=False)
            check_policy(policy)
        except Exception as exc:
            return red("policy_invariant_failed", type(exc).__name__)
        reg = PromptRegistry(self.repo / "prompts")
        try:
            on_disk = json.loads((self.repo / "prompts" / "manifest.json").read_text())
        except (OSError, ValueError):
            return red("prompts_manifest_missing")
        if reg.manifest() != on_disk:
            return red("prompts_manifest_stale", "run `council prompts` and commit prompts/manifest.json")
        return green("policy_prompts_ok", f"policy {policy.sha256[:12]} · prompts {reg.manifest_sha()[:12]}")

    def agent_rules(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for name in AGENT_RULE_FILES:
            try:
                out[name] = (self.repo / name).read_text()
            except OSError:
                out[name] = None
        return out

    def operator_registry(self) -> tuple[set[str], dict[str, bool]]:
        return operator_registry_from_source((self.repo / "src" / "council" / "cli.py").read_text())

    def deny_rules(self) -> tuple[list[str] | None, list[str] | None]:
        def load(path: Path) -> Any:
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None
        rules = load(self.repo / "ops" / "claude" / "deny-rules.json")
        settings = load(self.repo / ".claude" / "settings.json")
        wanted = rules.get("deny") if isinstance(rules, dict) else None
        perms = settings.get("permissions") if isinstance(settings, dict) else None
        have = perms.get("deny") if isinstance(perms, dict) else None
        return (list(wanted) if isinstance(wanted, list) else None, list(have) if isinstance(have, list) else None)

    def scripts(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for rel in GUARDED_SCRIPTS:
            try:
                out[rel] = (self.repo / rel).read_text()
            except OSError:
                out[rel] = None
        return out

    def broker_feed_on(self) -> bool:
        from council import invariants
        from council.policy import Policy

        if not bool(getattr(invariants, "BROKER_FEED_ENABLED", False)):
            return False
        policy = Policy.load(self.repo / "policy", include_sleeve=False)
        council = policy.council if isinstance(policy.council, dict) else {}
        section = council.get("news")
        return section is None or (isinstance(section, dict) and section.get("broker_feed", True) is True)

    def head_snapshot(self) -> Result:
        from council.runtime import head_policy_snapshot  # writes only into a throwaway dir

        with tempfile.TemporaryDirectory(prefix="council-ready-") as tmp:
            try:
                snap = head_policy_snapshot(Path(tmp), repo=self.repo)
            except Exception as exc:
                return red("head_snapshot_failed", type(exc).__name__)
        return green("head_snapshot_ok", snap.commit[:12])

    # -- the installed release
    def release_state(self) -> ReleaseInfo:
        cur = self.release_dir
        if not (cur / ".git").exists():
            return ReleaseInfo(False)
        status = self.git(cur, "status", "--porcelain", "--untracked-files=normal")
        if status.returncode != 0:
            return ReleaseInfo(True, git_ok=False)
        head = self.git(cur, "rev-parse", "HEAD").stdout.strip().lower()
        tags = tuple(sorted(t for t in self.git(cur, "tag", "--points-at", "HEAD").stdout.split()
                            if RELEASE_TAG.match(t)))
        from council.operator.release import recorded_commit

        return ReleaseInfo(True, git_ok=True, dirty=bool(status.stdout.strip()), head=head, tags=tags,
                           recorded=recorded_commit(self.state_dir))

    def origin_tag_commit(self, tag: str) -> str | None:
        out = self.run(["git", "ls-remote", ORIGIN_URL, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], timeout=30)
        if out.returncode != 0:
            return None
        plain, peeled = "", ""
        for line in out.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                peeled = sha.strip().lower()
            elif ref:
                plain = sha.strip().lower()
        return peeled or plain or ""

    def release_tags(self, pattern: str) -> list[str]:
        out = self.git(self.release_dir, "tag", "--merged", "HEAD", "-l", pattern)
        return out.stdout.split() if out.returncode == 0 else []

    def release_sleeve_live(self) -> bool:
        try:
            text = (self.release_dir / "src" / "council" / "invariants.py").read_text()
        except OSError:
            return False
        return re.search(r"^STOCK_SLEEVE_LIVE\s*=\s*True\b", text, re.MULTILINE) is not None

    # -- CI
    def ci_runs(self, sha: str) -> list[CheckRun] | None:
        def get() -> list[CheckRun] | None:
            if not _SHA.match(sha):
                return None
            out = self.run(["gh", "api", "-H", "Accept: application/vnd.github+json",
                            f"repos/{REPO_SLUG}/commits/{sha}/check-runs?per_page=100"], timeout=30)
            if out.returncode != 0:
                return None
            try:
                data = json.loads(out.stdout)
                return [CheckRun(str(r.get("name", "")), str(r.get("status", "")), r.get("conclusion"))
                        for r in data.get("check_runs", [])]
            except (ValueError, AttributeError, TypeError):
                return None
        return self._memo(f"ci:{sha}", get)

    # -- machine
    def plists(self) -> list[PlistInfo]:
        out: list[PlistInfo] = []
        agents = self.home / "Library" / "LaunchAgents"
        for path in sorted(agents.glob(f"{LAUNCHD_LABEL_PREFIX}*.plist")):
            try:
                data = plistlib.loads(path.read_bytes())
                env = data.get("EnvironmentVariables") or {}
                program = tuple(str(a) for a in (data.get("ProgramArguments") or []))
                out.append(PlistInfo(path.name, {str(k): str(v) for k, v in env.items()}, program,
                                     str(data.get("WorkingDirectory", ""))))
            except Exception:
                out.append(PlistInfo(path.name, error=True))
        return out

    def security_has(self, service: str) -> bool:
        return self._memo(f"sec:{service}", lambda: self.run(
            [SECURITY, "find-generic-password", "-s", service]).returncode == 0)

    def write_keychain(self) -> WriteKeychainInfo:
        path = self.state_dir / WRITE_KEYCHAIN_NAME
        if not path.exists():
            return WriteKeychainInfo(False)
        listing = self.run([SECURITY, "list-keychains", "-d", "user"])
        target = path.resolve().as_posix()
        on_list = any(Path(line.strip().strip('"')).resolve().as_posix() == target
                      for line in listing.stdout.splitlines() if line.strip())
        info = self.run([SECURITY, "show-keychain-info", str(path)])
        text = f"{info.stdout}\n{info.stderr}"
        m = re.search(r"timeout=(\d+)s", text)
        timeout = int(m.group(1)) if info.returncode == 0 and m and "no-timeout" not in text else None
        return WriteKeychainInfo(True, on_list, timeout)

    def disk_free_gib(self) -> float:
        probe = self.state_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free / GIB

    def ac_sleep(self) -> int | None:
        out = self.run(["pmset", "-g", "custom"])
        if out.returncode != 0:
            return None
        section = ""
        for line in out.stdout.splitlines():
            if line.strip().endswith(":") and not line.startswith(" "):
                section = line.strip().rstrip(":")
                continue
            m = re.match(r"^\s*sleep\s+(\d+)", line)
            if m and section.lower().startswith("ac"):
                return int(m.group(1))
        return None

    def ollama_model(self) -> tuple[str, bool | None]:
        from council.settings import Settings

        model = self.env.get("COUNCIL_MODEL") or Settings.ollama_model
        out = self.run(["ollama", "list"])
        if out.returncode != 0:
            return model, None
        return model, any(line.split()[:1] == [model] for line in out.stdout.splitlines())

    def publisher(self) -> PublisherInfo:
        clone = self.state_dir / "publisher-clone"
        if not (clone / ".git").exists():
            return PublisherInfo(False)
        status = self.git(clone, "status", "--porcelain")
        origin = self.git(clone, "remote", "get-url", "origin").stdout.strip()
        key = self.state_dir / "deploy_key"
        ls_ok: bool | None = None
        if self.network:
            ls = self.git(clone, "ls-remote", "--heads", "origin", "main", timeout=30)
            ls_ok = ls.returncode == 0
        return PublisherInfo(True, status.returncode == 0 and not status.stdout.strip(), origin, key.exists(), ls_ok)

    def launchd_loaded(self) -> set[str]:
        out = self.run(["launchctl", "list"])      # read-only listing
        return {line.split()[-1] for line in out.stdout.splitlines()
                if line.split() and line.split()[-1].startswith(LAUNCHD_LABEL_PREFIX)}

    # -- ledger (read-only)
    def _ledger(self) -> sqlite3.Connection | None:
        path = self.state_dir / LEDGER_FILE
        if not path.is_file():
            return None
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)

    def ledger_summary(self) -> LedgerSummary:
        try:
            con = self._ledger()
        except sqlite3.Error:
            return LedgerSummary(True, readable=False)
        if con is None:
            return LedgerSummary(False)
        try:
            decisions = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            published = con.execute("SELECT COUNT(*) FROM decisions WHERE kind != 'smoke' "
                                    "AND published_commit IS NOT NULL").fetchone()[0]
            nav = con.execute("SELECT COUNT(*) FROM runtime_state WHERE key = 'nav_state'").fetchone()[0]
            marks = ",".join("?" for _ in FILLED_LEG_STATES)
            fills = con.execute(f"SELECT COUNT(*) FROM legs WHERE state IN ({marks})", FILLED_LEG_STATES).fetchone()[0]
        except sqlite3.Error:
            return LedgerSummary(True, readable=False)
        finally:
            con.close()
        return LedgerSummary(True, True, int(decisions), bool(nav), int(fills), int(published))

    def runtime(self, key: str) -> dict[str, Any] | None:
        try:
            con = self._ledger()
            if con is None:
                return None
            try:
                row = con.execute("SELECT value_json FROM runtime_state WHERE key = ?", (key,)).fetchone()
            finally:
                con.close()
            value = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def rate_limit_trips(self, since: datetime) -> list[datetime]:
        """Cycles since `since` that met a provider 429 (flag `history_rate_limited:` / `history_breaker:`)."""
        trips: list[datetime] = []
        try:
            con = self._ledger()
            if con is None:
                return []
            try:
                rows = con.execute("SELECT created_at, record_json FROM cycles").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []
        for created, record in rows:
            at = _parse_time(created)
            if at is None or at < since:
                continue
            try:
                flags = json.loads(record).get("flags") or []
            except (ValueError, AttributeError):
                continue
            if any(str(f).startswith(("history_rate_limited:", "history_breaker:")) for f in flags):
                trips.append(at)
        return trips

    # -- readiness records
    def record(self, name: str) -> Any:
        def get() -> Any:
            path = self.state_dir / "readiness" / f"{name}.json"
            if not path.is_file():
                return None
            try:
                data = json.loads(path.read_text())
                validate_record(name, data)
            except (OSError, ValueError, ReadinessError):
                return INVALID
            return data
        return self._memo(f"record:{name}", get)

    def attested(self, item: str) -> bool:
        rec = self.record("attest")
        if not isinstance(rec, dict):
            return False
        entry = (rec.get("attested") or {}).get(item)
        return isinstance(entry, dict) and entry.get("value") is True


# ---------------------------------------------------------------------------- source analysis
def operator_registry_from_source(source: str) -> tuple[set[str], dict[str, bool]]:
    """From cli.py source (never imported here): the defined command paths and the operator registry
    {path: pinned} (`@operator_command(...)`, the OPERATOR_COMMANDS literal, `require_operator(...)`).
    A conditional variant ("doctor --live-read") counts as defined when its option string appears."""
    tree = ast.parse(source)
    prefix: dict[str, str] = {"app": ""}
    literal: dict[str, bool] = {}
    registry: dict[str, bool] = {}
    defined: set[str] = set()

    def const(node: ast.AST | None) -> Any:
        return node.value if isinstance(node, ast.Constant) else None

    def pinned_of(call: ast.Call) -> bool:
        for kw in call.keywords:
            if kw.arg == "pinned":
                if isinstance(kw.value, ast.Constant):
                    return bool(kw.value.value)
                if isinstance(kw.value, ast.Subscript):          # OPERATOR_COMMANDS["path"]
                    return literal.get(const(kw.value.slice), False)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_typer" \
                and node.args and isinstance(node.args[0], ast.Name):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(const(kw.value), str):
                    prefix[node.args[0].id] = const(kw.value)
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "OPERATOR_COMMANDS" and isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values, strict=True):
                if isinstance(const(k), str):
                    literal[const(k)] = bool(const(v))
    registry.update(literal)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "command" and isinstance(func.value, ast.Name) \
                        and func.value.id in prefix:
                    name = const(dec.args[0]) if dec.args else None
                    name = name if isinstance(name, str) else node.name.replace("_", "-")
                    defined.add(f"{prefix[func.value.id]} {name}".strip())
                elif isinstance(func, ast.Name) and func.id == "operator_command" and dec.args:
                    if isinstance(const(dec.args[0]), str):
                        registry[const(dec.args[0])] = pinned_of(dec)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require_operator" \
                and node.args and isinstance(const(node.args[0]), str):
            registry[const(node.args[0])] = pinned_of(node)
    for path in REQUIRED_OPERATOR:
        base, _, option = path.partition(" --")
        if option and base in defined and f'"--{option}"' in source:
            defined.add(path)
    return defined, registry


# ------------------------------------------------------------------------------------ helpers
def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _ci_job(p: Probes, job: str, ok_code: str) -> Result:
    """A CI job on the checkout's HEAD: amber without --network (accepted, as §6 says)."""
    if not p.network:
        return amber("ci_unknown", f"CI job {job}: run with --network to read it")
    runs = p.ci_runs(p.head())
    if runs is None:
        return amber("ci_unreachable", "gh could not read the check runs", accepted=False)
    mine = [r for r in runs if r.name == job]
    if not mine:
        return red("ci_job_absent", f"no {job} job on {p.head()[:12]}")
    if any(r.status != "completed" for r in mine):
        return amber("ci_pending", f"{job} still running", accepted=False)
    if all(r.conclusion == "success" for r in mine):
        return green(ok_code, f"{job} passed on {p.head()[:12]}")
    return red("ci_failed", f"{job} did not pass on {p.head()[:12]}")


def _record_gate(p: Probes, name: str, gate_id: str) -> Result:
    rec = p.record(name)
    if rec is None:
        return red(f"no_record:{name}", f"readiness/{name}.json missing ({RECORDS[name].writer})")
    if rec is INVALID:
        return red(f"record_invalid:{name}", f"readiness/{name}.json fails the schema")
    entry = (rec.get("gates") or {}).get(gate_id)
    if not isinstance(entry, dict):
        return red("not_recorded", f"readiness/{name}.json has no {gate_id} ({RECORDS[name].writer})")
    return Result(entry["state"], entry["code"], f"recorded {str(rec.get('at', ''))[:16]}")


def _attest_gate(p: Probes, item: str, ok_code: str) -> Result:
    if p.attested(item):
        return green(ok_code, f"{item} attested")
    return red(f"unattested:{item}", f"council-op ops attest {item}")


def _present(p: Probes, env_name: str, item: str) -> bool:
    """The Keychain item only: the launchd runner never sees this shell's environment (B3 refuses a
    URL or secret in a plist), so a value exported here alone would leave the runner without it."""
    return p.security_has(item)


def _shell_only(p: Probes, env_name: str) -> str:
    return f" ({env_name} is set in this shell, but the launchd runner reads only the Keychain)" \
        if p.env.get(env_name) else ""


# --------------------------------------------------------------------------------------- gates
def r1_release(p: Probes) -> Result:
    info = p.release_state()
    if not info.exists:
        return red("release_missing", "no installed release (state_dir/releases/current)")
    if not info.git_ok:
        return red("release_not_git", "releases/current is not a git checkout")
    if info.dirty:
        return red("release_dirty", "the installed release has local changes")
    if not info.tags:
        return red("release_untagged", "HEAD is not a council-spec-v* tag")
    if info.recorded != info.head:
        return red("release_record_mismatch", "HEAD differs from the commit recorded at install")
    tag = info.tags[-1]
    if p.network:
        origin = p.origin_tag_commit(tag)
        if origin is None:
            return amber("origin_unreachable", "git ls-remote origin failed", accepted=False)
        if origin != info.head:
            return red("tag_not_on_origin", f"{tag} on origin is not the installed commit")
        return green("release_ok", f"{tag} {info.head[:12]} = origin")
    return green("release_ok", f"{tag} {info.head[:12]} (origin checked at install; --network re-checks)")


def r2_ci(p: Probes) -> Result:
    if not p.network:
        return amber("ci_unknown", "run with --network to read CI")
    runs = p.ci_runs(p.head())
    if runs is None:
        return amber("ci_unreachable", "gh could not read the check runs", accepted=False)
    if not runs:
        return amber("ci_pending", f"no check runs on {p.head()[:12]}", accepted=False)
    failed = sorted({r.name for r in runs if r.status == "completed"
                     and r.conclusion not in ("success", "skipped", "neutral")})
    if failed:
        return red("ci_failed", "failed: " + ", ".join(failed))
    if any(r.status != "completed" for r in runs):
        return amber("ci_pending", "CI still running", accepted=False)
    return green("ci_ok", f"{len(runs)} check runs passed on {p.head()[:12]}")


def r3_rehearsal(p: Probes) -> Result:
    return _ci_job(p, CI_ACCEPTANCE_JOB, "rehearsal_ci_ok")


def r4_policy(p: Probes) -> Result:
    return p.policy_prompts()


def r5_pinned(p: Probes) -> Result:
    refs = [r for r in p.workflow_uses() if not r.startswith(("./", "docker://"))]
    loose = sorted({r for r in refs if not _SHA.match(r.rpartition("@")[2])})
    if loose:
        return red("action_not_pinned", "tag refs: " + ", ".join(loose))
    return green("actions_pinned", f"{len(refs)} uses: pinned to a commit")


def r6_acceptance(p: Probes, track: str) -> Result:
    missing = [f"{wp} ({pkg.what})" for wp, pkg in ACCEPTANCE.items()
               if pkg.acceptance and (pkg.track == "core" or track == "stocks") and not p.landed(wp)]
    if missing:
        return red("not_built", "not built: " + ", ".join(missing))
    return _ci_job(p, CI_ACCEPTANCE_JOB, "acceptance_ok")


def b1_base_url(p: Probes) -> Result:
    if p.is_sandbox():
        return green("sandbox", "rehearsal sandbox: a base URL is allowed")
    if p.env.get(BASE_URL_ENV):
        return red("base_url_in_shell", f"{BASE_URL_ENV} is set in this shell")
    hits = [pl.name for pl in p.plists() if BASE_URL_ENV in pl.env]
    if hits:
        return red("base_url_in_plist", "set in " + ", ".join(hits))
    return green("base_url_pinned")


def b2_write_isolation(p: Probes) -> Result:
    if p.security_has(WRITE_SERVICE):
        return red("write_token_on_search_list", f"{WRITE_SERVICE} is reachable from the default keychains")
    kc = p.write_keychain()
    if not kc.exists:
        return amber("write_keychain_absent", "not created yet (council-op keys init-write-keychain)")
    if kc.on_search_list:
        return red("write_keychain_on_search_list", "remove it from the user search list")
    if kc.lock_timeout_s is None:
        return red("write_keychain_no_timeout", "lock timeout unknown or disabled")
    if kc.lock_timeout_s > WRITE_KEYCHAIN_MAX_LOCK_S:
        return red("write_keychain_lock_timeout", f"locks after {kc.lock_timeout_s} s (> 60 s)")
    return green("write_token_isolated", f"separate keychain, locks after {kc.lock_timeout_s} s")


def b3_plists(p: Probes) -> Result:
    plists = p.plists()
    if not plists:
        return amber("plists_not_rendered", "no com.fbzz.council.* plist in ~/Library/LaunchAgents")
    release = str(p.release_dir)
    bad: list[str] = []
    for pl in plists:
        if pl.error:
            bad.append(f"{pl.name}: unreadable")
            continue
        if pl.env.get("COUNCIL_ROLE") != "runner":
            bad.append(f"{pl.name}: role")
        if pl.env.get("COUNCIL_AGENT_CONTEXT") != "1":
            bad.append(f"{pl.name}: agent context")
        # the live jobs run `<release>/.venv/bin/council`; the soak jobs `<release>/.venv/bin/python -m
        # council.operator.soak` (M5-F): either executable must live in the installed release
        council = [a for a in pl.program if a.endswith(("/council", "/python"))]
        if not council or not all(a.startswith(release + "/") for a in council):
            bad.append(f"{pl.name}: not the release path")
        for name, value in pl.env.items():
            if _PLIST_FORBIDDEN_NAME.search(name) or value.lower().startswith(("http://", "https://")):
                bad.append(f"{pl.name}: {name}")
    if bad:
        return red("plist_bad", "; ".join(bad))
    return green("plists_ok", f"{len(plists)} plist(s)")


def b4_marker(p: Probes) -> Result:
    # a FILE, as in release.is_marked_sandbox: on a case-insensitive volume (APFS default) the
    # `rehearsal/` directory of `cycle --rehearsal` also answers to the name REHEARSAL
    if (p.real_state_dir / "REHEARSAL").is_file():
        return red("rehearsal_marker_in_state_dir", "remove REHEARSAL from the real state dir")
    return green("no_marker")


def b5_agent_rules(p: Probes) -> Result:
    _, registry = p.operator_registry()
    tokens = list(AGENT_RULE_TOKENS) + sorted({path.split()[-1] for path in registry})
    missing: list[str] = []
    for name, text in p.agent_rules().items():
        if text is None:
            missing.append(f"{name}: file")
            continue
        missing += [f"{name}: {t}" for t in tokens if t not in text]
    if missing:
        return red("agent_rules_incomplete", "; ".join(missing[:8]))
    return green("agent_rules_ok", " and ".join(AGENT_RULE_FILES))


def b6_operator_guard(p: Probes) -> Result:
    defined, registry = p.operator_registry()
    unguarded = sorted(path for path in REQUIRED_OPERATOR if path in defined and path not in registry)
    unpinned = sorted(path for path, pinned in REQUIRED_OPERATOR.items()
                      if pinned and path in registry and not registry[path])
    if unguarded or unpinned:
        detail = "; ".join(filter(None, [
            "unguarded: " + ", ".join(unguarded) if unguarded else "",
            "not pinned: " + ", ".join(unpinned) if unpinned else ""]))
        return red("operator_command_unguarded", detail)
    return green("operator_commands_guarded", f"{len(registry)} guarded, "
                 f"{sum(1 for v in registry.values() if v)} release-pinned")


def b7_transport(p: Probes) -> Result:
    hits = sorted(name for name in PROXY_OR_CERT_ENV if p.env.get(name))
    if hits:
        return red("proxy_or_cert_env", "set in this shell: " + ", ".join(hits))
    return green("transport_ok", "clients pinned (AST test); no proxy or cert variable")


def b8_deny_rules(p: Probes) -> Result:
    wanted, have = p.deny_rules()
    if wanted is None:
        return red("deny_rules_source_missing", "ops/claude/deny-rules.json unreadable")
    if have is None:
        return red("deny_settings_missing", ".claude/settings.json has no permissions.deny")
    missing = [rule for rule in wanted if rule not in set(have)]
    if missing:
        return red("deny_rules_missing", f"{len(missing)} rule(s) missing from .claude/settings.json")
    return green("deny_rules_present", f"{len(wanted)} rules in the project settings")


def b9_scripts(p: Probes) -> Result:
    bad: list[str] = []
    for rel, text in p.scripts().items():
        if text is None:
            bad.append(f"{rel}: missing")
            continue
        if "ops assert-operator" not in text:
            bad.append(f"{rel}: no ops assert-operator")
        if "/dev/tty" not in text:
            bad.append(f"{rel}: no /dev/tty")
    if bad:
        return red("script_unguarded", "; ".join(bad))
    return green("scripts_guarded")


def e1_disk(p: Probes) -> Result:
    free = p.disk_free_gib()
    if free >= DISK_GREEN_GIB:
        return green("disk_ok", f"{free:.1f} GiB free")
    if free >= DISK_RED_GIB:
        return amber("disk_low", f"{free:.1f} GiB free (< 10)")
    return red("disk_full", f"{free:.1f} GiB free (< 3)")


def e2_power(p: Probes) -> Result:
    sleep = p.ac_sleep()
    if sleep == 0:
        return green("ac_sleep_off")
    if p.attested("power-ok"):
        return amber("power_ok_attested", "AC sleep is on; accepted by attestation")
    if sleep is None:
        return amber("pmset_unreadable", "pmset -g custom gave no AC sleep value", accepted=False)
    return amber("ac_sleep_on", f"AC sleep {sleep} min: set 0, or council-op ops attest power-ok", accepted=False)


def e3_ollama(p: Probes) -> Result:
    model, listed = p.ollama_model()
    if listed is None:
        return red("ollama_unavailable", "ollama list failed")
    if not listed:
        return red("model_not_listed", model)
    return green("model_listed", model)


def e4_keychain(p: Probes, track: str) -> Result:
    required = KEYCHAIN_CORE + (KEYCHAIN_STOCKS if track == "stocks" else ())
    missing = [s for s in required if not p.security_has(s)]
    info = [f"{s} absent (optional)" for s in KEYCHAIN_OPTIONAL if not p.security_has(s)]
    if missing:
        return red("keychain_missing", "missing: " + ", ".join(missing))
    return green("keychain_present", "; ".join([f"{len(required)} present", *info]))


def e5_tiingo(p: Probes) -> Result:
    trips = p.rate_limit_trips(p.now - timedelta(days=30))
    recent = [t for t in trips if t >= p.now - timedelta(days=7)]
    if recent:
        return red("rate_limit_trip_7d", f"{len(recent)} cycle(s) met a 429 in the last 7 days")
    if not p.attested("tiingo-dedicated"):
        return amber("tiingo_unattested", "council-op ops attest tiingo-dedicated")
    if trips:
        return amber("rate_limit_trip_30d", "a 429 in the last 30 days")
    return green("tiingo_dedicated", "attested; no 429 in 30 days")


def e6_publisher(p: Probes) -> Result:
    pub = p.publisher()
    if not pub.exists:
        return red("publisher_missing", "state_dir/publisher-clone missing")
    if not pub.clean:
        return red("publisher_dirty")
    if REPO_SLUG not in pub.origin:
        return red("publisher_wrong_origin", "origin is not fbzz/council-book")
    https = pub.origin.startswith("https://")
    if pub.ls_remote_ok is False:
        return red("publisher_unreachable", "git ls-remote origin failed")
    if https and pub.deploy_key:
        return red("deploy_key_with_https", "a deploy key exists but the remote is HTTPS")
    if https:
        return amber("publisher_https", "HTTPS remote (pushes with your user credential)")
    if not pub.deploy_key:
        return red("deploy_key_missing", "SSH remote without state_dir/deploy_key")
    return green("publisher_ok", "SSH remote + deploy key" + (" + ls-remote" if pub.ls_remote_ok else ""))


def e7_head_policy(p: Probes) -> Result:
    return p.head_snapshot()


def e8_ledger(p: Probes, post_token: bool) -> Result:
    if post_token:
        return green("ledger_in_use", "token day passed: smoke decisions are expected")
    led = p.ledger_summary()
    if not led.exists:
        return green("ledger_pristine", "no live ledger yet")
    if not led.readable:
        return red("ledger_unreadable")
    if led.nav_state:
        return red("ledger_has_nav_state", "a nav_state from a non-live run (agents never delete it)")
    if led.decisions or led.fills:
        return red("ledger_not_pristine", "decisions or fills before token day")
    return green("ledger_pristine")


def o1_ntfy(p: Probes) -> Result:
    if not _present(p, "COUNCIL_NTFY_TOPIC", NTFY_ITEM):
        return red("ntfy_absent", f"council-op keys store ntfy-topic ({NTFY_ITEM})"
                   + _shell_only(p, "COUNCIL_NTFY_TOPIC"))
    if not p.attested("ntfy-received"):
        return red("ntfy_unattested", "council notify test, then council-op ops attest ntfy-received")
    return green("ntfy_ok")


def o2_deadman(p: Probes) -> Result:
    if not _present(p, "COUNCIL_HEALTHCHECK_URL", HEALTHCHECK_ITEM):
        return amber("healthcheck_absent", "no external dead-man switch"
                     + _shell_only(p, "COUNCIL_HEALTHCHECK_URL"))
    if not (set(LIVE_JOBS) & p.launchd_loaded()):
        return green("healthcheck_present", "jobs not loaded yet")
    ping = p.runtime(RUNTIME_HEALTHCHECK) or {}
    at = _parse_time(ping.get("at"))
    if at is None or ping.get("ok") is not True or p.now - at > PING_MAX_AGE:
        return red("pings_failing", "no successful ping in the last 30 min")
    return green("pings_ok")


def o3_soak(p: Probes) -> Result:
    result = _record_gate(p, "soak", "O3")
    if result.state == "green":
        rec = p.record("soak")
        installed = p.release_state()
        if installed.exists and rec.get("head") != installed.head:
            return amber("soak_older_release", "soak ran another release commit")
    return result


def o4_backup(p: Probes) -> Result:
    backup = p.runtime(RUNTIME_BACKUP) or {}
    at = _parse_time(backup.get("at"))
    if at is None:
        return red("backup_missing", "no ledger backup recorded")
    if backup.get("licensed_free") is not True:
        return red("backup_has_licensed", "the last backup did not exclude licensed content")
    if p.now - at > BACKUP_MAX_AGE:
        return red("backup_stale", "last backup older than 26 h")
    return green("backup_ok")


def o5_dress(p: Probes) -> Result:
    rec = p.record("dress")
    if rec is None:
        return red("dress_missing", "council-op ops record-dress after ops/rehearse-onboarding.sh")
    result = _record_gate(p, "dress", "O5")
    if result.state != "green":
        return result
    installed = p.release_state()
    expected = installed.head if installed.exists else p.head()
    if rec.get("head") != expected:
        return amber("dress_older_commit", "dress rehearsal ran an older commit")
    return green("dress_ok", f"release {expected[:12]}")


def lc1_feed(p: Probes) -> Result:
    if not p.broker_feed_on():
        return amber("feed_off", "broker feed off — news from public sources only")
    if p.attested("etoro-licence"):
        return green("feed_on_licensed", "feed on; etoro-licence attested (personal use, never published)")
    return red("feed_on_unattested", "feed on: council-op ops attest etoro-licence --ref <ticket>, or turn it off")


def lc2_prompts(p: Probes) -> Result:
    return _ci_job(p, CI_ACCEPTANCE_JOB, "licensed_prompt_tests_ok")


def lc3_retention(p: Probes) -> Result:
    return _ci_job(p, CI_ACCEPTANCE_JOB, "retention_tests_ok")


def p1_nav(p: Probes) -> Result:
    return _ci_job(p, CI_ACCEPTANCE_JOB, "nav_invariance_ok")


def k18_first_cycle(p: Probes) -> Result:
    led = p.ledger_summary()
    if led.published:
        return green("first_live_cycle_published")
    return red("no_live_cycle_published", "approve and publish the first manual live cycle")


def s1_stock_packages(p: Probes) -> Result:
    return green("stock_packages_landed")


def s2_alpaca(p: Probes) -> Result:
    missing = [s for s in KEYCHAIN_STOCKS if not p.security_has(s)]
    if missing:
        return red("alpaca_keys_missing", "missing: " + ", ".join(missing))
    return green("alpaca_keys_present")


def s4_sleeve_tagged(p: Probes) -> Result:
    if not p.release_tags("stocks-*"):
        return red("sleeve_untagged", "no stocks-<quarter> tag in the installed release")
    return _record_gate(p, "stocks", "S4")


def s5_sleeve_live(p: Probes) -> Result:
    if p.release_sleeve_live():
        return green("sleeve_live")
    return red("sleeve_not_live", "STOCK_SLEEVE_LIVE is False in the installed release")


def _rec(name: str, gate_id: str) -> Callable[[Probes], Result]:
    return lambda p: _record_gate(p, name, gate_id)


def _att(item: str, code: str) -> Callable[[Probes], Result]:
    return lambda p: _attest_gate(p, item, code)


_LIVE = "council-op doctor --live-read"


def gates_for(track: str, post_token: bool = False) -> list[Gate]:
    """The registry, in report order. Track `stocks` adds the S gates and the stock keychain items."""
    core = [
        Gate("R1", "user", "Installed release", r1_release, ("M5-F",)),
        Gate("R2", "agent", "CI on the release commit", r2_ci),
        Gate("R3", "agent", "L1 rehearsal in CI", r3_rehearsal, ("M5-G",)),
        Gate("R4", "agent", "Policy invariants + prompts manifest", r4_policy),
        Gate("R5", "agent", "CI actions SHA-pinned", r5_pinned, ("M5-I",)),
        Gate("R6", "agent", "Named acceptance tests", lambda p: r6_acceptance(p, track)),
        Gate("B1", "user", "Broker base URL pinned", b1_base_url, ("M5-A",)),
        Gate("B2", "user", "WRITE token isolation", b2_write_isolation),
        Gate("B3", "user", "Plists", b3_plists, ("M5-F",)),
        Gate("B4", "user", "No REHEARSAL marker in the real state dir", b4_marker, ("M5-B",)),
        Gate("B5", "agent", "CLAUDE.md / AGENTS.md operator rules", b5_agent_rules, ("M5-B",)),
        Gate("B6", "agent", "Operator commands guarded and pinned", b6_operator_guard, ("M5-B",)),
        Gate("B7", "agent", "Broker transport (+ no proxy/cert env)", b7_transport, ("M5-A",)),
        Gate("B8", "user", "Claude Code deny rules (project settings)", b8_deny_rules, ("M5-B",)),
        Gate("B9", "agent", "Ops scripts assert operator + /dev/tty", b9_scripts, ("M5-F", "M5-G")),
        Gate("E1", "user", "Free disk", e1_disk),
        Gate("E2", "user", "Power: AC sleep 0", e2_power),
        Gate("E3", "user", "Ollama model listed", e3_ollama),
        Gate("E4", "user", "Keychain presence", lambda p: e4_keychain(p, track)),
        Gate("E5", "user", "Dedicated Tiingo token", e5_tiingo),
        Gate("E6", "user", "Publisher clone", e6_publisher),
        Gate("E7", "agent", "HEAD policy snapshot loads", e7_head_policy),
        Gate("E8", "user", "Live ledger pristine before token day", lambda p: e8_ledger(p, post_token)),
        Gate("O1", "user", "ntfy", o1_ntfy, ("M5-E1",)),
        Gate("O2", "user", "External dead-man switch", o2_deadman, ("M5-E1", "M5-E2")),
        Gate("O3", "user", "launchd soak (48 h)", o3_soak, ("M5-F",)),
        Gate("O4", "token", "Ledger backup (post-load)", o4_backup, ("M5-E1", "M5-E2", "M5-M"),
             hint="first backup after the live jobs load"),
        Gate("O5", "user", "Human dress rehearsal (L2)", o5_dress, ("M5-G",)),
        Gate("LC1", "user", "Broker feed state", lc1_feed),
        Gate("LC2", "agent", "No eToro data in prompts while off (CI)", lc2_prompts, ("M5-M",)),
        Gate("LC3", "agent", "Licensed retention, 7-day sweep (CI)", lc3_retention, ("M5-M",)),
        Gate("P1", "agent", "Core NAV-invariance tests (CI)", p1_nav, ("M5-N",)),
        Gate("P2", "token", "Size floor within the deadband share", _rec("live-read", "P2"), ("M5-C",), hint=_LIVE),
        Gate("T1", "agent", "Private capture of the last rehearsal cycle", _rec("soak", "T1"), ("T1", "M5-F")),
        Gate("T2", "agent", "why trail for every line (council show)", _rec("soak", "T2"),
             ("T5a", "M5-K", "M5-F")),
        Gate("T3", "agent", "Public-domain news path", _rec("soak", "T3"), ("T3", "M5-F")),
        Gate("K1", "token", "One Agent Portfolio; council-read/-write belong to it", _rec("keys", "K1"),
             ("M5-C",), hint="council-op keys verify"),
        Gate("K2", "token", "Token scopes", _rec("keys", "K2"), ("M5-C",), hint="council-op keys verify"),
        Gate("K3", "token", "Token expiry", _rec("keys", "K3"), ("M5-C",), hint="council-op keys verify"),
        Gate("K4", "token", "IP whitelist", _rec("keys", "K4"), ("M5-C",), hint="council-op keys verify"),
        Gate("K5", "token", "Identity: equity = virtual balance, 0 positions", _rec("live-read", "K5"),
             ("M5-C",), hint=_LIVE),
        Gate("K6", "token", "pnl read", _rec("live-read", "K6"), ("M5-C",), hint=_LIVE),
        Gate("K7", "token", "rates entitlement", _rec("live-read", "K7"), ("M5-C",), hint=_LIVE),
        Gate("K8", "token", "feed (skipped: licence while LC1 is off)", _rec("live-read", "K8"), ("M5-C",),
             hint=_LIVE),
        Gate("K9", "token", "costs within floors", _rec("live-read", "K9"), ("M5-C",), hint=_LIVE),
        Gate("K10", "token", "minimum within copy floor and smoke caps", _rec("live-read", "K10"), ("M5-C",),
             hint=_LIVE),
        Gate("K11", "token", "Every line has a resolved vehicle", _rec("live-read", "K11"), ("M5-C",),
             hint="council-op instruments resolve"),
        Gate("K12", "token", "Mirror ratio set from the broker", _rec("live-read", "K12"), ("M5-C",),
             hint="council-op account set-mirror --from-broker"),
        Gate("K13", "token", "Private fixtures recorded; parsers pass", _rec("live-read", "K13"), ("M5-C",),
             hint="council-op doctor --record-fixtures"),
        Gate("K14", "token", "Smoke S1–S6 done; capabilities cross-checked", _rec("smoke", "K14"), ("M5-D2",),
             hint="council-op smoke propose / verify"),
        Gate("K15", "token", "Fee location attested = costs.yaml", _rec("attest", "K15"), ("M5-D1",),
             hint="council-op ops attest fee-charged-on=<levels>"),
        Gate("K16", "token", "Copy Stop Loss attested", _att("copy-stop-loss", "copy_sl_attested"), ("M5-D1",),
             hint="council-op ops attest copy-stop-loss"),
        Gate("K17", "token", "Cancel route", _rec("live-read", "K17"), ("M5-C",), hint=_LIVE),
        Gate("K18", "token", "First manual live cycle published", k18_first_cycle, hint="approve the first cycle"),
        Gate("K19", "token", "Currency, price unit, whole units, SL bounds", _rec("live-read", "K19"), ("M5-C",),
             hint="council-op instruments resolve"),
        Gate("K20", "token", "No open smoke position or pending smoke decision", _rec("smoke", "K20"),
             ("M5-D2",), hint="council-op smoke status"),
        Gate("K21", "token", "Terms version attested", _att("terms-version", "terms_version_attested"),
             ("M5-D1",), hint="council-op ops attest terms-version"),
    ]
    if track != "stocks":
        return core
    stock_wps = ("WP-J", "WP-K", "WP-L", "WP-M", "T2", "T6", "M5-L")
    return core + [
        Gate("S1", "agent", "Track S packages landed", s1_stock_packages, stock_wps, "stocks"),
        Gate("S2", "user", "Alpaca keys", s2_alpaca, track="stocks"),
        Gate("S3", "token", "S7 + stock_fractional capability", _rec("smoke", "S3"), ("M5-D2",), "stocks",
             hint="council-op smoke propose (S7)"),
        Gate("S4", "user", "Sleeve tagged + onboard ok", s4_sleeve_tagged, ("M5-L",), "stocks"),
        Gate("S5", "user", "STOCK_SLEEVE_LIVE in the installed release", s5_sleeve_live, track="stocks"),
        Gate("S6", "token", "W-8BEN n/a attested", _att("w8ben-na", "w8ben_na_attested"), ("M5-D1",), "stocks",
             hint="council-op ops attest w8ben-na"),
    ]


# ------------------------------------------------------------------------------------- report
@dataclass
class Report:
    track: str
    post_token: bool
    network: bool
    results: list[GateResult]
    internal_error: bool = False
    checkout: str = ""
    not_built: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        if self.internal_error:
            return 2
        if any(r.result.state == "red" or r.unaccepted for r in self.results):
            return 1
        return 0

    @property
    def ready(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> dict[str, Any]:
        reds = [r for r in self.results if r.result.state == "red"]
        return {
            "red": len(reds),
            "red_by_owner": {o: sum(1 for r in reds if r.gate.owner == o) for o in OWNERS},
            "amber": sum(1 for r in self.results if r.result.state == "amber"),
            "amber_unaccepted": sum(1 for r in self.results if r.unaccepted),
            "wait": sum(1 for r in self.results if r.result.state == "wait"),
            "green": sum(1 for r in self.results if r.result.state == "green"),
            "not_built": list(self.not_built),
        }

    def as_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "ready": self.ready, "exit": self.exit_code, "track": self.track,
                "post_token": self.post_token, "network": self.network, "checkout": self.checkout,
                "summary": self.summary(), "gates": [r.as_json() for r in self.results]}

    def lines(self) -> list[str]:
        s = self.summary()
        label = TRACK_LABEL[self.track] + (", post-token" if self.post_token else "")
        by_owner = ", ".join(f"{o}: {n}" for o, n in s["red_by_owner"].items() if n)
        head = (f"{'READY' if self.ready else 'NOT READY'} ({label}): {s['red']} red"
                + (f" ({by_owner})" if by_owner else "")
                + f", {s['amber']} amber" + (f" ({s['amber_unaccepted']} not accepted)" if s["amber_unaccepted"] else "")
                + f", {s['wait']} awaiting token, {s['green']} green")
        out = [head]
        if self.internal_error:
            out.append("internal error in at least one gate (exit 2)")
        if self.checkout:
            out.append(f"checkout: {self.checkout}")
        order = {"red": 0, "amber": 1, "wait": 2, "green": 3}
        for r in sorted(self.results, key=lambda r: (order[r.result.state], not r.unaccepted)):
            state = "amber!" if r.unaccepted else r.result.state
            text = r.gate.title + (f" — {r.result.detail}" if r.result.detail else "")
            out.append(f"  {r.gate.id:<4} {state:<6} {r.gate.owner:<5}  {text}")
        return out


def evaluate(probes: Probes, *, track: str = "core", post_token: bool = False) -> Report:
    if track not in TRACKS:
        raise ValueError(f"unknown track {track!r}")
    results: list[GateResult] = []
    internal = False
    for gate in gates_for(track, post_token):
        missing = [wp for wp in gate.wp if not probes.landed(wp)]
        if missing:
            result = red("not_built", "not built: " + ", ".join(missing))
        elif gate.owner == "token" and not post_token:
            result = Result("wait", "awaiting_token", "awaiting token" + (f" ({gate.hint})" if gate.hint else ""))
        else:
            try:
                result = gate.check(probes)
            except Exception as exc:        # a broken gate is an internal error, never a pass
                internal = True
                result = red("internal_error", type(exc).__name__)
        results.append(GateResult(gate, result))
    if probes.on_release():
        checkout = "installed release"
    else:
        checkout = f"development tree (not the installed release): {probes.repo}"
    not_built = tuple(wp for wp, pkg in ACCEPTANCE.items()
                      if (pkg.track == "core" or track == "stocks") and not probes.landed(wp))
    return Report(track, post_token, probes.network, results, internal, checkout, not_built)


def run_ready(*, track: str = "core", post_token: bool = False, as_json: bool = False, network: bool = False,
              echo: Callable[[str], Any] = print, probes: Probes | None = None) -> int:
    """`council doctor --ready`: print the report and return the exit code (2 on an internal error)."""
    try:
        report = evaluate(probes or Probes.default(network=network), track=track, post_token=post_token)
    except Exception as exc:
        echo(json.dumps({"schema": SCHEMA_VERSION, "exit": 2, "error": type(exc).__name__}) if as_json
             else f"internal error: {type(exc).__name__}")
        return 2
    if as_json:
        echo(json.dumps(report.as_json(), indent=2))
    else:
        for line in report.lines():
            echo(line)
    return report.exit_code


# ------------------------------------------------------------------------------------ records
def validate_record(name: str, data: Any) -> None:
    """Codes, booleans, timestamps and shas only; the record's gates and attestations must be the
    ones RECORDS allows for `name`. Raises ReadinessError."""
    spec = RECORDS.get(name)
    if spec is None:
        raise ReadinessError(f"unknown readiness record {name!r}")
    if not isinstance(data, dict) or not set(data) <= {"at", "head", "gates", "attested"}:
        raise ReadinessError("record keys must be at, head, gates, attested")
    if _parse_time(data.get("at")) is None:
        raise ReadinessError("record 'at' must be an ISO timestamp")
    if not isinstance(data.get("head"), str) or not _SHA.match(data["head"]):
        raise ReadinessError("record 'head' must be a 40-hex commit")
    gates = data.get("gates", {})
    if not isinstance(gates, dict):
        raise ReadinessError("record 'gates' must be a mapping")
    for gate_id, entry in gates.items():
        if gate_id not in spec.gates:
            raise ReadinessError(f"{name}.json may not carry gate {gate_id!r}")
        if not isinstance(entry, dict) or not set(entry) <= {"state", "code", "at"} or \
                entry.get("state") not in RECORD_STATES or not _valid_code(entry.get("code")):
            raise ReadinessError(f"gate {gate_id}: needs a state and a code (no values)")
        if "at" in entry and _parse_time(entry["at"]) is None:
            raise ReadinessError(f"gate {gate_id}: 'at' must be an ISO timestamp")
    attested = data.get("attested", {})
    if not isinstance(attested, dict):
        raise ReadinessError("record 'attested' must be a mapping")
    for item, entry in attested.items():
        if item not in spec.attestations:
            raise ReadinessError(f"{name}.json may not carry attestation {item!r}")
        if not isinstance(entry, dict) or set(entry) != {"value", "at"} or not isinstance(entry["value"], bool) \
                or _parse_time(entry["at"]) is None:
            raise ReadinessError(f"attestation {item}: needs value (bool) and at (ISO)")


def _valid_code(code: Any) -> bool:
    return isinstance(code, str) and len(code) <= 64 and bool(_CODE.match(code)) and not _LONG_DIGITS.search(code)


def _assert_rehearsal_runner(env: Mapping[str, str]) -> None:
    """soak.json: only the launchd rehearsal watch, as the runner; never an agent or CI."""
    problems = []
    if env.get("COUNCIL_ROLE") != "runner":
        problems.append("COUNCIL_ROLE is not 'runner'")
    if not env.get("XPC_SERVICE_NAME", "").startswith(REHEARSAL_LABEL_PREFIX):
        problems.append("not under a com.fbzz.council.rehearsal.* launchd job")
    for name in ("CLAUDECODE", "CI", "GITHUB_ACTIONS"):
        if name in env:
            problems.append(f"{name} is set")
    problems += [f"{n} is set" for n in sorted(env) if n.startswith("CLAUDE_CODE_")]
    if problems:
        raise ReadinessError("soak record refused: " + "; ".join(problems))


def write_record(
    name: str,
    *,
    head: str,
    gates: Mapping[str, Mapping[str, str]] | None = None,
    attested: Mapping[str, bool] | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    assert_operator: Callable[[], None] | None = None,
    assert_release: Callable[[], None] | None = None,
) -> Path:
    """Merge `gates` / `attested` into `state_dir/readiness/<name>.json` (0600, outside the repo).

    Every record except `soak` is written only from the operator's terminal on the installed release
    (`guards.assert_current_process_is_operator`, `release.assert_release_code`; a marked rehearsal
    sandbox passes the latter); `soak` only by the launchd rehearsal watch as the runner. Refusals
    raise ReadinessError (the guard's own error for the operator checks)."""
    spec = RECORDS.get(name)
    if spec is None:
        raise ReadinessError(f"unknown readiness record {name!r}")
    root = state_dir if state_dir is not None else paths.state_dir()
    if spec.runner:
        _assert_rehearsal_runner(env if env is not None else os.environ)
    else:
        if assert_operator is None:
            from council.operator import guards

            assert_operator = guards.assert_current_process_is_operator
        assert_operator()
        if assert_release is None:
            from council.operator.release import assert_release_code

            def assert_release() -> None:
                assert_release_code(state_dir=root, argv=["(readiness record)"])
        assert_release()
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    directory = root / "readiness"
    path = directory / f"{name}.json"
    paths.assert_outside_repo(path)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
            validate_record(name, loaded)
            existing = loaded
        except (OSError, ValueError, ReadinessError):
            existing = {}
    data: dict[str, Any] = {"at": at, "head": head, "gates": dict(existing.get("gates", {}))}
    for gate_id, entry in (gates or {}).items():
        data["gates"][gate_id] = {"state": entry.get("state"), "code": entry.get("code")}
    merged_attest = dict(existing.get("attested", {}))
    for item, value in (attested or {}).items():
        merged_attest[item] = {"value": value, "at": at}
    if merged_attest:
        data["attested"] = merged_attest
    validate_record(name, data)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = directory / f".{name}.json.tmp"
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------------------ CI node ids
def acceptance_ids(repo: Path | None = None, *, include_missing: bool = False,
                   packages: Iterable[str] | None = None) -> list[str]:
    """The registry's node ids that exist in `repo` (all of them with `include_missing`), for CI job
    `m5-acceptance`. Missing ones are reported by R6, not by CI."""
    probes = Probes(state_dir=Path(tempfile.gettempdir()), repo=repo or paths.REPO_ROOT, home=Path.home(),
                    env={}, now=datetime.now(UTC))
    out: list[str] = []
    for wp, package in ACCEPTANCE.items():
        if packages is not None and wp not in set(packages):
            continue
        if not package.acceptance:
            continue
        out += [n for n in package.tests if include_missing or probes.node_exists(n)]
    return list(dict.fromkeys(out))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI for CI
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["acceptance-ids"]:
        for node in acceptance_ids(include_missing="--all" in args):
            print(node)
        return 0
    print("usage: python -m council.operator.readiness acceptance-ids [--all]", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
