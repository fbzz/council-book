"""Stock-sleeve study, PRE-REGISTERED in docs/stock-sleeve-spec.md and
policy/variants/stock-sleeve-variants-v1.yaml (tag `stock-sleeve-spec`). This file implements exactly
that spec; editing it after the tag voids the study, and `run` refuses real data when it (or the spec,
the variants file, a rule module or a frozen dependency) differs from the tagged version.

The rule itself lives in the modules live trading imports, so the study tests the code that will run:
`council.stocks.pit` (point-in-time fundamentals and the four features), `council.stocks.score` (the
score and the name selection), `council.stocks.sectors` (SIC to Fama-French 12) and
`council.reference.sleeve` (equal weight, overlay level, deadband, never borrow). The universe filters
(`Universe.at`) stay in this script: the live rank reproduces them and a test pins it to this code.
A real run executes from a clean worktree checked out at the tag, with its own environment.

Subcommands
    prepare   read the lab's local sources and write the point-in-time input bundle to
              <state dir>/backtests/stock-sleeve/inputs/ (reads parquet: `uv run --with pyarrow`);
              refused once the tag exists
    coverage  counts only, no returns: the eligibility funnel and the unmapped members per date
    seal      write the private cost parameters and print their commitment (pre-registration step)
    power     the gate's power on the seeded synthetic fixture with injected alpha (no real data)
    freeze    write the sha256 of the input bundle, its lab sources and the code into the variants
              file (the last step before the commit and the tag)
    run       the study; real data only after the tag is pushed, or `--synthetic` for the fixture

    uv run --with pyarrow python scripts/stock_sleeve_study.py prepare
    uv run python scripts/stock_sleeve_study.py coverage
    uv run python scripts/stock_sleeve_study.py power
    uv run python scripts/stock_sleeve_study.py freeze
    COUNCIL_MODE=dry_run uv run python scripts/stock_sleeve_study.py run          # after tag + push
    uv run python scripts/stock_sleeve_study.py run --synthetic --draws 20

Every output goes to <state dir>/backtests/stock-sleeve/ (synthetic runs to .../synthetic/, the
power study to .../power/), never into the repository. A real run writes to its own
results/run-<UTC time>/ folder and refuses to start when a completed real run exists (a run folder
holding gate.json or result.json), when another run holds results/LOCK, or when COUNCIL_STATE_DIR is
set. The outputs are OPERATOR-ONLY: the summary markdown is percent-only and passes
`assert_public_safe`, but a book's fee drag together with its leg count discloses the sealed funding.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import pwd
import re
import secrets
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from council import paths  # noqa: E402
from council.policy import LineSpec, Policy  # noqa: E402
from council.reference import sleeve as sleeve_rule  # noqa: E402
from council.reference.backtest import (  # noqa: E402
    BacktestConfig,
    BookPlan,
    CostModel,
    Panel,
    SimResult,
    aligned_returns,
    asof_align,
    build_panel,
    control_cost_model,
    cost_model,
    equity_calendar,
    fixed_mix_plan,
    reference_plan,
    run_backtest,
    simulate,
)
from council.reference.metrics import performance, soft_kill_drill  # noqa: E402
from council.reference.report import assert_public_safe  # noqa: E402
from council.reference.signals import line_signals  # noqa: E402
from council.reference.synthetic import synthetic_closes  # noqa: E402
from council.stocks import pit  # noqa: E402
from council.stocks.score import FEATURES, add_scores, sector_quotas, select_rule  # noqa: E402
from council.stocks.sectors import ff12  # noqa: E402

SPEC_PATH = REPO / "policy" / "variants" / "stock-sleeve-variants-v1.yaml"
DOC_PATH = REPO / "docs" / "stock-sleeve-spec.md"
# The rule the study tests, in the modules the live rank and the live reference book import.
RULE_MODULES = (
    "src/council/stocks/pit.py",
    "src/council/stocks/score.py",
    "src/council/stocks/sectors.py",
    "src/council/reference/sleeve.py",
)
FROZEN_FILES = (
    "docs/stock-sleeve-spec.md",
    "policy/variants/stock-sleeve-variants-v1.yaml",
    "scripts/stock_sleeve_study.py",
    *RULE_MODULES,
)
# Code, policy and locked library versions the numbers depend on: they must also equal their tagged
# versions. A real run also requires HEAD to be the tagged commit and every tracked file to equal its
# tagged blob (`frozen_check`), so the code these do not list is pinned as well.
FROZEN_DEPENDENCIES = (
    "src/council/stocks/__init__.py",
    "pyproject.toml",
    "uv.lock",
    "policy/universe.yaml",
    "policy/reference.yaml",
    "policy/risk.yaml",
    "policy/costs.yaml",
    "scripts/backtest_reference.py",
    "src/council/policy.py",
    "src/council/reference/backtest.py",
    "src/council/reference/book.py",
    "src/council/reference/metrics.py",
    "src/council/reference/signals.py",
    "src/council/reference/synthetic.py",
)
# Hashed into the bundle's manifest and the variants file's `frozen_inputs.code_sha256` by `freeze`.
CODE_FILES = ("scripts/stock_sleeve_study.py", *RULE_MODULES)
_EPS = 1e-12
PROXY = "SLEEVE_PROXY"
LABEL = ("Mechanical, in-sample backtest of a pre-registered rule; the universe and prices carry "
         "the survivorship limits listed in the spec. Not evidence for the council.")
SENSITIVITIES = ("costs_x2", "no_hold_buffer", "lab_resolver", "ai_list", "overlay_standalone",
                 "zero_fixed_fee", "nav_x5", "stock_stops", "execution_lag", "index_sleeve_spy_only")
DIAGNOSTICS = ("gc_overlap", "concentration", "fee_arithmetic", "fee_feasibility", "survivorship",
               "r8_realised_vol")
GATE_CHECKS = ("G1_pool", "G2_random", "G3_regimes", "B1_book", "B2_index_sleeve")

# ------------------------------------------------------------------------------------ spec


# Spec values this code implements in only one way: a changed value must fail loudly, never be ignored.
_FIXED_BY_CODE: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("window", "end"), "last_stock_session"),
    (("rebalance", "filings_visible"), "filed_before_D"),
    (("rebalance", "decision"), "close_D"),
    (("universe", "dedupe"), "cik"),
    (("universe", "sector"), "ff12_from_sic"),
    (("universe", "require_all_features"), True),
    (("universe", "fundamentals_over"), "cik_chain"),
    (("rule", "score"), "rank_average"),
    (("rule", "peer_group"), "ff12"),
    (("rule", "data_layer"), "comparable_year_ago"),
    (("sleeve", "budget"), "trim_held_above_target"),
    (("overlay", "signal"), "SPY"),
    (("costs", "fixed_fee_per_leg"), "sealed"),
    (("costs", "core_commission"), "sealed"),
    (("costs", "slippage_scope"), "every_leg"),
    (("baselines", "ew_universe"), {"rebalance": "each_D", "fixed_fee": False}),
    (("baselines", "random", "common_random_numbers"), True),
    (("baselines", "index_sleeve", "weights"), "base_weight"),
    (("selection", "candidates"), "selectable"),
    (("selection", "metric"), "net_sharpe"),
    (("selection", "tie_break"), "lower_total_cost_drag"),
    (("gate", "G1_pool", "fixed_fee"), "none"),
    (("gate", "G2_random", "null_model"), "crn_max_percentile_over_selectable"),
    (("gate", "G3_regimes", "fixed_fee"), "none"),
    (("gate", "B2_index_sleeve", "vs"), "index_sleeve_book"),
    (("whole_book", "compare_with"), "current_reference"),
    (("stress", "sleeve_proxy"), "beta_scaled_equal_weight_index"),
)


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    spec = yaml.safe_load(path.read_text())
    for key in ("data", "frozen_inputs", "window", "rebalance", "universe", "rule", "variants", "n_names",
                "sleeve", "overlay", "costs", "baselines", "selection", "gate", "reported", "stress",
                "whole_book", "stops", "execution_lag_sessions", "sensitivities", "diagnostics", "power"):
        if key not in spec:
            raise ValueError(f"spec is missing '{key}'")
    for keys, expected in _FIXED_BY_CODE:
        node: Any = spec
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        if node != expected:
            raise ValueError(f"spec {'.'.join(keys)} = {node!r}; this code implements only {expected!r}")
    if tuple(spec["rule"]["features"]) != tuple(pit.FUNDAMENTAL_COLUMNS):
        raise ValueError("spec rule.features differ from the ported rule")
    if set(spec["gate"]) != set(GATE_CHECKS):
        raise ValueError(f"spec gate checks {tuple(spec['gate'])} differ from the implemented {GATE_CHECKS}")
    for kind, known in (("sensitivities", SENSITIVITIES), ("diagnostics", DIAGNOSTICS)):
        unknown = [s for s in spec[kind] if s not in known]
        missing = [s for s in known if s not in spec[kind]]
        if unknown or missing:
            raise ValueError(f"spec {kind}: unknown {unknown}, not listed {missing}")
    if not any(v.get("selectable") for v in spec["variants"].values()):
        raise ValueError("spec has no selectable variant")
    return spec


def out_root(synthetic: bool = False) -> Path:
    root = paths.state_dir() / "backtests" / "stock-sleeve"
    return root / "synthetic" if synthetic else root


def lab_root(spec: Mapping[str, Any]) -> Path:
    env = spec["data"].get("lab_root_env", "COUNCIL_LAB_ROOT")
    return Path(os.environ.get(env) or (Path.home() / "Desktop" / "trading")).expanduser()


def cells_of(spec: Mapping[str, Any], *, selectable: bool | None = None) -> dict[str, tuple[dict[str, Any], int]]:
    """{cell name: (variant, N)}; `selectable` filters on the variant's flag."""
    out = {}
    for vname, variant in spec["variants"].items():
        if selectable is not None and bool(variant.get("selectable")) != selectable:
            continue
        for n in spec["n_names"]:
            out[f"{vname}-{n}"] = (variant, int(n))
    return out


# ------------------------------------------------------------------------------------ sealed costs


def canonical_json(doc: Mapping[str, Any]) -> bytes:
    """Same canonical form as council.publish.commit_reveal: sorted keys, no whitespace."""
    return json.dumps(dict(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def params_commitment(doc: Mapping[str, Any], salt_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt_hex) + canonical_json(doc)).hexdigest()


def seal_params(path: Path, *, real_nav_usd: float, fixed_fee_usd: float) -> str:
    paths.assert_outside_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SystemExit(f"{path.name} already exists; a sealed parameter is never overwritten")
    doc = {"fixed_fee_usd": float(fixed_fee_usd), "real_nav_usd": float(real_nav_usd)}
    salt = secrets.token_hex(32)
    path.write_text(json.dumps({"doc": doc, "salt": salt}, indent=2))
    path.chmod(0o600)
    return params_commitment(doc, salt)


def load_sealed_params(spec: Mapping[str, Any], policy: Policy, root: Path) -> dict[str, float]:
    """The private fee and funding, verified against the commitment in the variants file."""
    expected = str(spec["costs"]["private_params_sha256"])
    path = root / spec["costs"]["private_params_file"]
    if not path.exists():
        raise SystemExit(f"sealed parameters missing: {path.name} (run `seal` before the tag)")
    blob = json.loads(path.read_text())
    got = params_commitment(blob["doc"], blob["salt"])
    if got != expected:
        raise SystemExit("sealed parameters do not match the commitment in the variants file")
    doc = blob["doc"]
    if float(doc["fixed_fee_usd"]) != float(policy.costs["fixed_commission_usd"]["real"]):
        raise SystemExit("sealed fee differs from policy/costs.yaml fixed_commission_usd.real")
    return {"fixed_fee_usd": float(doc["fixed_fee_usd"]), "real_nav_usd": float(doc["real_nav_usd"])}


# ------------------------------------------------------------------------------------ frozen check


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def tag_exists(tag: str) -> bool:
    return _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode == 0


def checkout_check() -> None:
    """The code this process executes must be this checkout's: a run from a worktree at the tag must
    import `council` (the rule modules, the reference book, the policy loader and so the policy
    files) from that worktree, never from another checkout's environment."""
    if paths.REPO_ROOT.resolve() != REPO.resolve():
        raise SystemExit("the council package is imported from another checkout: run `uv run` inside the "
                         "checkout (or worktree) the study script belongs to")


def git_blob_sha1(data: bytes) -> str:
    """Git's object id of a blob with these bytes (what `git hash-object --no-filters` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tagged_tree(tag: str) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob id)} of every file in the tagged commit's tree."""
    listed = _git("ls-tree", "-r", "-z", "--full-tree", f"refs/tags/{tag}^{{commit}}")
    if listed.returncode != 0:
        raise SystemExit(f"cannot list the tree of tag {tag}")
    out: dict[str, tuple[str, str]] = {}
    for entry in filter(None, listed.stdout.split("\0")):
        meta, rel = entry.split("\t", 1)
        mode, _kind, oid = meta.split()
        out[rel] = (mode, oid)
    return out


def _worktree_blob(rel: str, mode: str) -> str | None:
    """The blob id of the checked-out file at `rel`, hashed from its bytes (never from git's index, so
    `assume-unchanged` and `skip-worktree` flags cannot hide an edit); None when it is missing."""
    path = REPO / rel
    if mode == "120000":
        return git_blob_sha1(os.readlink(path).encode()) if path.is_symlink() else None
    if path.is_symlink() or not path.is_file():
        return None
    return git_blob_sha1(path.read_bytes())


def frozen_check(tag: str) -> str:
    """Refuse a real run unless `tag` exists, HEAD is the tagged commit and the checkout is the tagged
    tree: the frozen files and every other tracked file equal their tagged blobs (hashed from the
    files' bytes), and nothing untracked sits beside them. Returns the tagged commit."""
    rev = _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}")
    if rev.returncode != 0:
        raise SystemExit(f"tag {tag} does not exist: commit and tag the pre-registration first")
    commit = rev.stdout.strip()
    worktree_hint = f"run from a clean worktree at the tag: `git worktree add ../council-book-{tag} {tag}`"
    head = _git("rev-parse", "-q", "--verify", "HEAD^{commit}")
    if head.returncode != 0 or head.stdout.strip() != commit:
        raise SystemExit(f"HEAD is not the commit tagged {tag}; {worktree_hint}")
    tree = tagged_tree(tag)
    frozen = (*FROZEN_FILES, *FROZEN_DEPENDENCIES)
    for rel in frozen:
        if rel not in tree:
            raise SystemExit(f"{rel} is not part of tag {tag}")
    changed = [rel for rel in frozen if _worktree_blob(rel, tree[rel][0]) != tree[rel][1]]
    if changed:
        raise SystemExit(f"frozen files differ from tag {tag} ({', '.join(changed)}); the pre-registration "
                         f"would be void. If later policy or code edits landed, {worktree_hint}")
    other = [rel for rel, (mode, oid) in sorted(tree.items())
             if mode != "160000" and _worktree_blob(rel, mode) != oid]
    listed = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise SystemExit("cannot list the checkout's files")
    extra = sorted({rel for rel in listed.stdout.split("\0") if rel} - set(tree))
    if other or extra:
        shown = ", ".join([*other, *extra][:8])
        raise SystemExit(f"the checkout is not the tagged tree ({len(other)} changed or missing, {len(extra)} "
                         f"not in the tag: {shown}); {worktree_hint}")
    return commit


def remote_tag_check(tag: str, remote: str) -> str:
    """The tag must be on the remote (an external timestamp) and point where the local tag points."""
    local = _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    listed = _git("ls-remote", remote, f"refs/tags/{tag}")
    if listed.returncode != 0 or not listed.stdout.strip():
        raise SystemExit(f"tag {tag} is not on {remote}: `git push {remote} {tag}` before the run")
    remote_sha = listed.stdout.split()[0]
    if local.returncode != 0 or remote_sha != local.stdout.strip():
        raise SystemExit(f"tag {tag} on {remote} differs from the local tag")
    return remote_sha


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_hashes() -> dict[str, str]:
    return {rel: sha256_file(REPO / rel) for rel in CODE_FILES}


def inputs_check(spec: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """The bundle `run` reads must be the one frozen in the variants file, built by the tagged code."""
    fz = spec["frozen_inputs"]
    if not fz or fz.get("inputs_sha256") in (None, "", "pending"):
        raise SystemExit("the variants file carries no frozen inputs: run `freeze` before the tag")
    bundle = root / "inputs" / "inputs.pkl"
    if not bundle.exists():
        raise SystemExit("inputs/inputs.pkl is missing")
    if sha256_file(bundle) != fz["inputs_sha256"]:
        raise SystemExit("inputs/inputs.pkl differs from the frozen sha256")
    manifest = json.loads((root / "inputs" / "manifest.json").read_text())
    got_sources = {k: v.get("sha256") for k, v in manifest.get("sources", {}).items()}
    if got_sources != dict(fz.get("sources") or {}):
        raise SystemExit("the bundle's source hashes differ from the frozen ones")
    if manifest.get("code_sha256") != dict(fz.get("code_sha256") or {}) or manifest.get("code_sha256") != code_hashes():
        raise SystemExit("the bundle was built by code other than the frozen (tagged) code")
    return manifest


def render_frozen_block(inputs_sha: str, code: Mapping[str, str], sources: Mapping[str, str]) -> str:
    lines = ["frozen_inputs:                          # written by `freeze` just before the tag; `run` refuses on any mismatch",
             f"  inputs_sha256: {inputs_sha}",
             "  code_sha256:"]
    lines += [f"    {k}: {v}" for k, v in sorted(code.items())]
    lines.append("  sources:")
    lines += [f"    {k}: {v}" for k, v in sorted(sources.items())]
    return "\n".join(lines) + "\n"


def freeze(spec: Mapping[str, Any], root: Path, spec_path: Path = SPEC_PATH) -> str:
    """Write the bundle's sha256, its sources' sha256 and the code's sha256 into the variants file."""
    if tag_exists(str(spec["tag"])):
        raise SystemExit("the tag exists: the frozen inputs can no longer change")
    bundle = root / "inputs" / "inputs.pkl"
    manifest = json.loads((root / "inputs" / "manifest.json").read_text())
    if manifest.get("code_sha256") != code_hashes():
        raise SystemExit("the bundle was built by older code: run `prepare` again, then `freeze`")
    sources = {k: v["sha256"] for k, v in manifest["sources"].items()}
    block = render_frozen_block(sha256_file(bundle), manifest["code_sha256"], sources)
    text = spec_path.read_text()
    new, count = re.subn(r"^frozen_inputs:.*?(?=^\S)", block + "\n", text, count=1, flags=re.M | re.S)
    if count != 1:
        raise SystemExit("no frozen_inputs block in the variants file")
    spec_path.write_text(new)
    return block


# ------------------------------------------------------------------------------------ inputs


@dataclass
class Inputs:
    """Point-in-time inputs. Keys: `security_id` for prices, CIK (int) for issuers."""

    members: pd.DataFrame          # index, symbol, opt_in, opt_out (NaT = still a member)
    symbol_changes: pd.DataFrame   # old_symbol, new_symbol, event_date (membership rows use the newest symbol)
    symbols: pd.DataFrame          # security_id, symbol, data_symbol, valid_from, valid_to
    issuer_by_date: pd.DataFrame   # security_id, cik, valid_from, valid_to (predecessor CIKs included)
    securities: pd.DataFrame       # index security_id: security_type, is_adr, cik
    sic: dict[int, Any]            # CIK -> SEC SIC code (current)
    closes: pd.DataFrame           # adjusted closes, sessions x security_id (the headline price panel)
    dollar_volume: pd.DataFrame    # raw close x volume, sessions x security_id
    fundamentals: pd.DataFrame     # PIT rows, changes (a), (b), (c): pit.COMPARABLE_COLUMNS; `ticker` = str(CIK)
    taxonomy: dict[int, str]       # CIK -> companyfacts taxonomy ("us-gaap", "ifrs-full", "none")
    raw_close: pd.DataFrame | None = None  # unadjusted closes (float32): the fee-feasibility diagnostic
    adj_open: pd.DataFrame | None = None   # adjusted opens (float32): the stop sensitivity
    adj_low: pd.DataFrame | None = None    # adjusted lows (float32): the stop sensitivity
    ai: pd.DataFrame | None = None  # symbol, cik, security_id (sensitivity only)
    ai_closes: pd.DataFrame | None = None  # adjusted closes of AI names outside the panel ("AI:<ticker>")
    fundamentals_lab: pd.DataFrame | None = None  # the lab resolver's rows, verbatim (sensitivity)
    manifest: dict[str, Any] = field(default_factory=dict)

    _fund_by_cik: dict[str, dict[int, pd.DataFrame]] = field(default_factory=dict)
    _first_bar: pd.Series | None = None
    _all_closes: pd.DataFrame | None = None

    BLOB_FIELDS = ("members", "symbol_changes", "symbols", "issuer_by_date", "securities", "sic", "closes",
                   "dollar_volume", "fundamentals", "taxonomy", "raw_close", "adj_open", "adj_low", "ai",
                   "ai_closes", "fundamentals_lab")

    @property
    def all_closes(self) -> pd.DataFrame:
        """The panel plus the AI-only closes; the panel alone fixes the window and the listing rule."""
        if self._all_closes is None:
            extra = self.ai_closes
            self._all_closes = self.closes if extra is None or extra.empty else self.closes.join(
                extra, how="left")
        return self._all_closes

    def fund(self, cik: int, source: str = "headline") -> pd.DataFrame:
        if source not in self._fund_by_cik:
            base = self.fundamentals if source == "headline" else self.fundamentals_lab
            if base is None:
                raise ValueError(f"no fundamentals for source {source}")
            f = base.copy()
            f["period_end"] = pd.to_datetime(f["period_end"])
            f["available_at"] = pd.to_datetime(f["available_at"])
            self._fund_by_cik[source] = {int(k): g for k, g in f.groupby(f["ticker"].astype(int))}
        base = self.fundamentals if source == "headline" else self.fundamentals_lab
        return self._fund_by_cik[source].get(int(cik), base.iloc[0:0])  # type: ignore[union-attr]

    def first_bar(self) -> pd.Series:
        if self._first_bar is None:
            first = self.closes.apply(lambda s: s.first_valid_index())
            if self.ai_closes is not None and not self.ai_closes.empty:
                first = pd.concat([first, self.ai_closes.apply(lambda s: s.first_valid_index())])
            self._first_bar = first
        return self._first_bar

    def save(self, directory: Path) -> None:
        paths.assert_outside_repo(directory)
        directory.mkdir(parents=True, exist_ok=True)
        blob = {k: _plain(getattr(self, k)) for k in self.BLOB_FIELDS}
        pd.to_pickle(blob, directory / "inputs.pkl")
        (directory / "manifest.json").write_text(json.dumps(self.manifest, indent=2, default=str))

    @classmethod
    def load(cls, directory: Path) -> Inputs:
        blob = pd.read_pickle(directory / "inputs.pkl")
        missing = [k for k in cls.BLOB_FIELDS if k not in blob]
        if missing:
            raise SystemExit(f"the input bundle predates this code (no {missing}): run `prepare` again")
        manifest = json.loads((directory / "manifest.json").read_text())
        return cls(**{k: blob[k] for k in cls.BLOB_FIELDS}, manifest=manifest)


def _plain(obj: Any) -> Any:
    """Object dtype for every string column and label, so the bundle unpickles without pyarrow."""
    if not isinstance(obj, pd.DataFrame):
        return obj
    out = obj.copy()
    for c in out.columns:
        if pd.api.types.is_string_dtype(out[c].dtype) and out[c].dtype != object:
            out[c] = out[c].astype(object)
    if pd.api.types.is_string_dtype(out.columns.dtype):
        out.columns = pd.Index(list(out.columns), dtype=object)
    if pd.api.types.is_string_dtype(out.index.dtype):
        out.index = pd.Index(list(out.index), dtype=object)
    return out


def _read_parquet(path: Path, **kw: Any) -> pd.DataFrame:
    try:
        return pd.read_parquet(path, **kw)
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit("reading parquet needs pyarrow: `uv run --with pyarrow python ...`") from exc


def _cik_of(issuer_id: Any) -> int | None:
    text = str(issuer_id or "")
    return int(text[1:]) if text.startswith("I") and text[1:].isdigit() else None


def _file_facts(lab: Path, rel: str) -> dict[str, Any]:
    st = (lab / rel).stat()
    return {"path": rel, "bytes": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            "sha256": sha256_file(lab / rel)}


def source_files(spec: Mapping[str, Any]) -> dict[str, str]:
    """Every lab file `prepare` reads, by name."""
    d = spec["data"]
    out = {"sp500": d["membership"]["sp500"], "nasdaq100": d["membership"]["nasdaq100"],
           "membership_events": d["membership_events"], "prices": d["prices"], "companyfacts": d["companyfacts"],
           "ff12_definition": d["ff12_definition"]}
    out.update({k: v for k, v in d["identity"].items()})
    out.update({f"ai_{k}": v for k, v in d["ai_sensitivity"].items()})
    return out


def prepare(spec: Mapping[str, Any], *, include_ai: bool = True, verbose: bool = True) -> Inputs:
    """Read the lab sources named in the spec into one point-in-time bundle."""
    lab = lab_root(spec)
    d = spec["data"]
    t0 = time.time()
    members = []
    for index, rel in d["membership"].items():
        df = pd.read_pickle(lab / rel)
        members.append(pd.DataFrame({
            "index": index, "symbol": df["symbol"].astype(str),
            "opt_in": pd.to_datetime(df["opt-in"]), "opt_out": pd.to_datetime(df["opt-out"]),
        }))
    members_df = pd.concat(members, ignore_index=True)
    ev = pd.read_pickle(lab / d["membership_events"])
    ev = ev[ev["event_type"] == "ticker_change"]
    symbol_changes = pd.DataFrame({"old_symbol": ev["old_symbol"].astype(str), "new_symbol": ev["new_symbol"].astype(str),
                                   "event_date": pd.to_datetime(ev["event_date"])}).reset_index(drop=True)
    ident = d["identity"]
    sym = _read_parquet(lab / ident["symbol_intervals"])
    symbols = pd.DataFrame({
        "security_id": sym["security_id"].astype(str), "symbol": sym["symbol"].astype(str),
        "data_symbol": sym["data_symbol"].astype(str),
        "valid_from": pd.to_datetime(sym["valid_from"]), "valid_to": pd.to_datetime(sym["valid_to"]),
    })
    iss = _read_parquet(lab / ident["issuer_intervals"])
    issuer_by_date = pd.DataFrame({
        "security_id": iss["security_id"].astype(str), "cik": iss["issuer_id"].map(_cik_of),
        "valid_from": pd.to_datetime(iss["valid_from"]), "valid_to": pd.to_datetime(iss["valid_to"]),
    }).dropna(subset=["cik"])
    issuer_by_date["cik"] = issuer_by_date["cik"].astype(int)
    sec = _read_parquet(lab / ident["securities"])
    securities = pd.DataFrame({
        "security_type": sec["security_type"].astype(str).to_numpy(),
        "is_adr": sec["is_adr"].map(lambda v: bool(v) if v is not None and v == v else False).to_numpy(),
        "cik": sec["issuer_id"].map(_cik_of).to_numpy(),
    }, index=sec["security_id"].astype(str))
    issuers = _read_parquet(lab / ident["issuers"])
    sic = {int(c): s for c, s in zip(issuers["cik"], issuers["sic"], strict=True)}
    if verbose:
        print(f"identity loaded ({time.time() - t0:.0f}s)")
    bars = _read_parquet(lab / d["prices"], columns=["security_id", "session_date", "open", "low", "close", "volume",
                                                      "adj_close"])
    bars["session_date"] = pd.to_datetime(bars["session_date"])
    factor = bars["adj_close"] / bars["close"].where(bars["close"] > 0)

    keys = bars[["security_id", "session_date"]]

    def pivot(values: pd.Series, dtype: str = "float64") -> pd.DataFrame:
        frame = pd.concat([keys, values.rename("v")], axis=1)
        return frame.pivot(index="session_date", columns="security_id", values="v").sort_index().astype(dtype)

    closes = pivot(bars["adj_close"])
    dollar_volume = pivot(bars["close"] * bars["volume"])
    raw_close = pivot(bars["close"], "float32")
    adj_open = pivot(bars["open"] * factor, "float32")
    adj_low = pivot(bars["low"] * factor, "float32")
    del bars
    if verbose:
        print(f"prices loaded: {closes.shape} ({time.time() - t0:.0f}s)")

    ai = None
    extra_closes: dict[str, pd.Series] = {}
    if include_ai:
        ai, extra_closes = _prepare_ai(spec, lab, securities)
        for cik, s in zip(ai["cik"], ai["sic"], strict=True):
            if cik is not None and int(cik) not in sic and s is not None:
                sic[int(cik)] = s
    ai_closes = pd.DataFrame(extra_closes).sort_index() if extra_closes else None
    ciks = set(issuer_by_date["cik"].astype(int)) | {int(c) for c in securities["cik"].dropna()}
    if ai is not None:
        ciks |= {int(c) for c in ai["cik"].dropna()}
    fundamentals, fundamentals_lab, taxonomy = _prepare_fundamentals(lab / d["companyfacts"], sorted(ciks),
                                                                     verbose=verbose)
    manifest = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "code_sha256": code_hashes(),
        "sources": {k: _file_facts(lab, v) for k, v in source_files(spec).items()},
        "members_rows": len(members_df),
        "price_panel": {"sessions": int(closes.shape[0]), "securities": int(closes.shape[1]),
                        "first": str(closes.index.min().date()), "last": str(closes.index.max().date())},
        "ai_names": 0 if ai is None else len(ai), "ai_names_priced_outside_panel": len(extra_closes),
        "fundamental_rows": len(fundamentals), "ciks_with_facts": int(fundamentals["ticker"].nunique()),
        "ciks_requested": len(ciks),
        "taxonomy_counts": pd.Series(taxonomy).value_counts().to_dict(),
    }
    return Inputs(members=members_df, symbol_changes=symbol_changes, symbols=symbols, issuer_by_date=issuer_by_date,
                  securities=securities, sic=sic, closes=closes, dollar_volume=dollar_volume,
                  fundamentals=fundamentals, taxonomy=taxonomy, raw_close=raw_close, adj_open=adj_open,
                  adj_low=adj_low, ai=ai, ai_closes=ai_closes, fundamentals_lab=fundamentals_lab, manifest=manifest)


def _prepare_fundamentals(zpath: Path, ciks: Sequence[int], *,
                          verbose: bool) -> tuple[pd.DataFrame, pd.DataFrame, dict[int, str]]:
    """Both builds from one read: changes (a)+(b)+(c) (headline) and the lab resolver (verbatim)."""
    frames: list[pd.DataFrame] = []
    lab_frames: list[pd.DataFrame] = []
    taxonomy: dict[int, str] = {}
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        for i, cik in enumerate(ciks, 1):
            name = f"CIK{int(cik):010d}.json"
            if name not in names:
                taxonomy[int(cik)] = "no_companyfacts"
                continue
            cf = json.loads(z.read(name))
            df, stats = pit.fundamentals_comparable(str(int(cik)), str(int(cik)), cf)
            df_lab, _ = pit.fundamentals_for_company(str(int(cik)), str(int(cik)), cf)
            taxonomy[int(cik)] = stats["taxonomy"]
            if not df.empty:
                frames.append(df)
            if not df_lab.empty:
                lab_frames.append(df_lab)
            if verbose and i % 200 == 0:
                print(f"  companyfacts {i}/{len(ciks)}")
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=pit.COMPARABLE_COLUMNS)
    cols = pit.FUNDAMENTALS_COLUMNS
    out_lab = pd.concat(lab_frames, ignore_index=True) if lab_frames else pd.DataFrame(columns=cols)
    return out[pit.COMPARABLE_COLUMNS], out_lab[cols], taxonomy


class _TickerSafeLoader(yaml.SafeLoader):
    """YAML loader that keeps tickers such as ON as strings. Its resolver table is a COPY: the base
    SafeLoader (used by Policy.load and the spec) keeps its booleans."""

    yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


def _prepare_ai(spec: Mapping[str, Any], lab: Path, securities: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    """The lab's AI-adjacent tickers (hindsight list): CIK and SIC from the lab's EDGAR table; prices
    from the finetune panel when the CIK is there, else from the lab's own price file."""
    a = spec["data"]["ai_sensitivity"]
    cfg = yaml.load((lab / a["universe"]).read_text(), Loader=_TickerSafeLoader)
    tickers: list[str] = []
    for group in (cfg.get("peer_groups") or cfg.get("groups") or {}).values():
        members = group.get("tickers", group) if isinstance(group, dict) else group
        tickers += [str(t) for t in members]
    tickers = sorted(set(tickers))
    comp = _read_parquet(lab / a["companies"])
    comp_by = {str(t): r for t, r in zip(comp["ticker"], comp.to_dict("records"), strict=True)}
    prices = _read_parquet(lab / a["prices"], columns=["ticker", "date", "adj_close"])
    prices["date"] = pd.to_datetime(prices["date"])
    by_cik_sec = {int(c): s for s, c in securities["cik"].dropna().items()}
    rows, extra = [], {}
    for t in tickers:
        info = comp_by.get(t)
        cik = int(info["cik"]) if info is not None and info.get("cik") not in (None, "") else None
        sec_id = by_cik_sec.get(cik) if cik is not None else None
        if sec_id is None:
            p = prices[prices["ticker"] == t]
            if not p.empty:
                sec_id = f"AI:{t}"
                extra[sec_id] = p.set_index("date")["adj_close"].sort_index()
        rows.append({"symbol": t, "cik": cik, "security_id": sec_id,
                     "sic": info.get("sic") if info is not None else None})
    return pd.DataFrame(rows), extra


# ------------------------------------------------------------------------------------ calendar


def rebalance_dates(sessions: pd.DatetimeIndex, spec: Mapping[str, Any], *, start: pd.Timestamp,
                    end: pd.Timestamp) -> list[pd.Timestamp]:
    """First session on or after each anchor (MM-DD), from the first decision anchor to `end`."""
    out: list[pd.Timestamp] = []
    for year in range(start.year, end.year + 1):
        for anchor in spec["rebalance"]["anchors"]:
            a = pd.Timestamp(f"{year}-{anchor}")
            if a < start or a > end:
                continue
            i = int(sessions.searchsorted(a))
            if i < len(sessions) and sessions[i] <= end:
                out.append(pd.Timestamp(sessions[i]))
    return sorted(set(out))


# ------------------------------------------------------------------------------------ universe


FUNNEL = ("member", "mapped", "common", "priced", "listed", "one_per_cik", "sector_known", "not_money",
          "companyfacts", "us_gaap", "visible", "domestic", "fresh", "all_features", "revenue_floor", "plausible")


def _valid(frame: pd.DataFrame, when: pd.Timestamp) -> pd.DataFrame:
    return frame[(frame["valid_from"] <= when) & (frame["valid_to"] >= when)]


class Universe:
    """Eligible names at each decision date, with the four features (cached per (CIK chain, date))."""

    def __init__(self, inp: Inputs, spec: Mapping[str, Any], *, source: str = "headline"):
        self.inp = inp
        self.spec = spec
        self.source = source
        self.u = spec["universe"]
        self._feat_cache: dict[tuple[tuple[int, ...], pd.Timestamp], dict[str, Any]] = {}
        self._sym_by_symbol = {s: g for s, g in inp.symbols.groupby("symbol")}
        self._sym_by_data = {s: g for s, g in inp.symbols.groupby("data_symbol")}
        self._iss_by_sec = {s: g for s, g in inp.issuer_by_date.groupby("security_id")}
        self.panel_start = inp.closes.index.min()

    def live_members(self, when: pd.Timestamp, indexes: Iterable[str] | None = None) -> list[str]:
        m = self.inp.members
        live = m[(m["opt_in"] <= when) & (m["opt_out"].isna() | (m["opt_out"] > when))]
        live = live[live["index"].isin(list(indexes or self.u["indexes"]))]
        return sorted(set(live["symbol"]))

    def symbols_at(self, symbol: str, when: pd.Timestamp) -> list[str]:
        """The membership symbol, then the symbols it replaced after `when` (ticker changes, chained)."""
        out, frontier = [symbol], [symbol]
        ch = self.inp.symbol_changes
        while frontier:
            s = frontier.pop()
            for old in ch[(ch["new_symbol"] == s) & (ch["event_date"] > when)]["old_symbol"]:
                if old not in out:
                    out.append(old)
                    frontier.append(old)
        return out

    def _security(self, symbol: str, when: pd.Timestamp) -> str | None:
        for candidate in self.symbols_at(symbol, when):
            for table in (self._sym_by_symbol, self._sym_by_data):
                g = table.get(candidate)
                if g is not None:
                    hit = _valid(g, when)
                    if not hit.empty:
                        return str(sorted(hit["security_id"])[0])
        return None

    def unmapped(self, when: pd.Timestamp) -> list[tuple[str, str]]:
        """Members with no security at `when`: `no_identity` (the symbol, or a symbol it replaced, is
        not in the identity table at all) or `identity_later` (its identity history starts after D)."""
        out = []
        for s in self.live_members(when):
            if self._security(s, when) is not None:
                continue
            known = any(c in self._sym_by_symbol or c in self._sym_by_data for c in self.symbols_at(s, when))
            out.append((s, "identity_later" if known else "no_identity"))
        return out

    def mapped_members(self, when: pd.Timestamp, index: str) -> list[str]:
        """Members of one index mapped to a security with a bar within the age limit (any type)."""
        window = self.inp.closes.loc[when - pd.Timedelta(days=int(self.u["max_bar_age_days"])): when]
        priced = set(window.columns[window.notna().any().to_numpy()])
        keys = {self._security(s, when) for s in self.live_members(when, [index])}
        return sorted(k for k in keys if k is not None and k in priced)

    def _cik(self, security_id: str, when: pd.Timestamp) -> int | None:
        g = self._iss_by_sec.get(security_id)
        if g is not None:
            hit = _valid(g, when)
            if not hit.empty:
                return int(sorted(hit["cik"])[0])
        if security_id in self.inp.securities.index:
            c = self.inp.securities.at[security_id, "cik"]
            return int(c) if c is not None and c == c else None
        return None

    def cik_chain(self, cik: int, when: pd.Timestamp, key: str | None = None) -> tuple[int, ...]:
        """The CIK valid at `when` plus every CIK the security had before (holding-company
        reorganisations): fundamentals are read across all of them, first report per period."""
        ciks = {int(cik)}
        g = self._iss_by_sec.get(key) if key is not None else None
        if g is not None:
            ciks |= {int(c) for c in g.loc[g["valid_from"] <= when, "cik"]}
        return tuple(sorted(ciks))

    def features(self, cik: int, when: pd.Timestamp, key: str | None = None) -> dict[str, Any]:
        chain = self.cik_chain(cik, when, key)
        ck = (chain, when)
        if ck not in self._feat_cache:
            parts = [self.inp.fund(c, self.source) for c in chain]
            parts = [p for p in parts if not p.empty]
            if parts:
                f = pd.concat(parts, ignore_index=True).assign(ticker="X")
            else:
                f = self.inp.fund(cik, self.source).assign(ticker="X")
            visible = f[f["available_at"] < when]           # strict: filed before the decision date
            if self.source == "headline":
                feats = pit.comparable_features(visible, "X", when)
            else:                                           # the lab's layer, verbatim (no guards)
                feats = pit.fundamental_features(visible, "X", when)
                feats.update(pit.rule_quarters(visible, "X", when))
                feats["plausible"] = True
            feats["n_ciks"] = len(chain)
            self._feat_cache[ck] = feats
        return self._feat_cache[ck]

    def at(self, when: pd.Timestamp, *, include_ai: bool = False) -> tuple[pd.DataFrame, dict[str, int]]:
        """(eligible frame, funnel counts). Eligible columns: key, symbol, cik, sector, the four
        features, revenue_L..PY, latest_available_at."""
        inp, u = self.inp, self.u
        funnel = dict.fromkeys(FUNNEL, 0)
        rows: list[dict[str, Any]] = [{"symbol": s, "key": None, "cik": None} for s in self.live_members(when)]
        funnel["member"] = len(rows)
        for r in rows:
            r["key"] = self._security(r["symbol"], when)
        if include_ai and inp.ai is not None:
            known = {r["symbol"] for r in rows}
            for a in inp.ai.itertuples(index=False):
                if a.symbol not in known and a.security_id is not None:
                    rows.append({"symbol": a.symbol, "key": a.security_id,
                                 "cik": int(a.cik) if a.cik is not None and a.cik == a.cik else None, "ai": True})
            funnel["member"] = len(rows)
        rows = [r for r in rows if r["key"] is not None]
        funnel["mapped"] = len(rows)

        def common(r: dict[str, Any]) -> bool:
            if str(r["key"]).startswith("AI:"):
                return True
            if r["key"] not in inp.securities.index:
                return False
            s = inp.securities.loc[r["key"]]
            ok_type = s["security_type"] in u["security_types"]
            return bool(ok_type and not (u.get("exclude_adr", True) and bool(s["is_adr"])))

        rows = [r for r in rows if common(r)]
        funnel["common"] = len(rows)
        window = inp.all_closes.loc[when - pd.Timedelta(days=int(u["max_bar_age_days"])): when]
        priced = set(window.columns[window.notna().any().to_numpy()])
        rows = [r for r in rows if r["key"] in priced]
        funnel["priced"] = len(rows)
        first = inp.first_bar()
        min_age = pd.Timedelta(days=int(u["min_listing_days"]))
        rows = [r for r in rows if first.get(r["key"]) is not None
                and (first[r["key"]] <= when - min_age or first[r["key"]] == self.panel_start)]
        funnel["listed"] = len(rows)
        for r in rows:
            if r.get("cik") is None:
                r["cik"] = self._cik(r["key"], when)
        rows = [r for r in rows if r["cik"] is not None]
        rows = self._dedupe(rows, when)
        funnel["one_per_cik"] = len(rows)
        for r in rows:
            r["sector"] = ff12(inp.sic.get(int(r["cik"])))
        rows = [r for r in rows if r["sector"] is not None]
        funnel["sector_known"] = len(rows)
        rows = [r for r in rows if r["sector"] not in u["exclude_sectors"]]
        funnel["not_money"] = len(rows)
        rows = [r for r in rows if inp.taxonomy.get(int(r["cik"]), "no_companyfacts") != "no_companyfacts"]
        funnel["companyfacts"] = len(rows)
        rows = [r for r in rows if inp.taxonomy.get(int(r["cik"])) == u["taxonomy"]]
        funnel["us_gaap"] = len(rows)
        for r in rows:
            r.update(self.features(int(r["cik"]), when, None if str(r["key"]).startswith("AI:") else r["key"]))
        rows = [r for r in rows if r.get("quarters_of_history", 0) > 0]
        funnel["visible"] = len(rows)
        rows = [r for r in rows if r.get("form_L") in set(u["domestic_forms"])]
        funnel["domestic"] = len(rows)
        max_age = pd.Timedelta(days=int(u["max_filing_age_days"]))
        rows = [r for r in rows if pd.notna(r["latest_available_at"]) and when - r["latest_available_at"] <= max_age]
        funnel["fresh"] = len(rows)
        rows = [r for r in rows if all(np.isfinite(float(r[c])) for c in FEATURES)]
        funnel["all_features"] = len(rows)
        floor = float(u["min_quarter_revenue_usd"])
        rows = [r for r in rows if all(float(r[c]) >= floor for c in ("revenue_L", "revenue_P", "revenue_Y", "revenue_PY"))]
        funnel["revenue_floor"] = len(rows)
        rows = [r for r in rows if bool(r.get("plausible"))]
        funnel["plausible"] = len(rows)
        cols = ["key", "symbol", "cik", "sector", *FEATURES, "revenue_L", "revenue_P", "revenue_Y",
                "revenue_PY", "latest_available_at", "latest_period_end"]
        frame = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)
        return frame.set_index("key", drop=False), funnel

    def _dedupe(self, rows: list[dict[str, Any]], when: pd.Timestamp) -> list[dict[str, Any]]:
        """One security per CIK: the higher median dollar volume over the last N sessions wins."""
        by: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by.setdefault(int(r["cik"]), []).append(r)
        n = int(self.u["dedupe_volume_sessions"])
        dv = self.inp.dollar_volume.loc[:when].tail(n)
        out = []
        for group in by.values():
            if len(group) == 1:
                out.append(group[0])
                continue

            def liquidity(r: dict[str, Any]) -> float:
                if r["key"] in dv.columns:
                    v = dv[r["key"]].median()
                    return float(v) if v == v else -1.0
                return -1.0

            out.append(sorted(group, key=lambda r: (-liquidity(r), str(r["key"])))[0])
        return out


# ------------------------------------------------------------------------------------ selection
#
# The score and the rule's selection (peer_groups, add_scores, ordered, sector_quotas, select_rule) are
# imported from council.stocks.score, the module the live rank uses. The random null stays here.


def crn_orders(eligible: Mapping[pd.Timestamp, pd.DataFrame], dates: Sequence[pd.Timestamp], seed: int,
               draw: int) -> dict[pd.Timestamp, list[str]]:
    """Common random numbers: ONE random order of the eligible set per decision date for each draw
    index, shared by every cell, so the null's cells are correlated the way the rule's cells are."""
    rng = np.random.default_rng([int(seed), int(draw)])
    out = {}
    for d in dates:
        keys = sorted(eligible[d].index)
        out[d] = [keys[i] for i in rng.permutation(len(keys))]
    return out


def select_random(elig: pd.DataFrame, held: Iterable[str], variant: Mapping[str, Any], n: int,
                  kept_target: int, order: Sequence[str]) -> list[str]:
    """Turnover-matched random null under a draw's order: keep the first `kept_target` eligible held
    names of the order (the rule's kept count at this date), then fill from the order, under the
    variant's sector constraint; the constraint is relaxed only when it leaves slots empty."""
    if elig.empty:
        return []
    cap = int(variant["cap"])
    sector = elig["sector"].to_dict()
    held_set = {h for h in held if h in elig.index}
    order = [k for k in order if k in elig.index]
    limit = sector_quotas(elig["sector"].value_counts().to_dict(), n, cap) if variant["constraint"] == "quota" else None
    counts: dict[str, int] = {}

    def room(k: str) -> bool:
        s = sector[k]
        return counts.get(s, 0) < (limit.get(s, 0) if limit is not None else cap)

    chosen: list[str] = []
    for k in order:
        if len(chosen) >= min(kept_target, n):
            break
        if k in held_set and room(k):
            chosen.append(k)
            counts[sector[k]] = counts.get(sector[k], 0) + 1
    for k in order:
        if len(chosen) >= n:
            break
        if k not in held_set and k not in chosen and room(k):
            chosen.append(k)
            counts[sector[k]] = counts.get(sector[k], 0) + 1
    for k in order:                                   # only when the constraint left slots empty
        if len(chosen) >= n:
            break
        if k not in held_set and k not in chosen:
            chosen.append(k)
    return chosen


# ------------------------------------------------------------------------------------ simulation


def simulate_budgeted(plan: BookPlan, returns: pd.DataFrame, costs: CostModel, *,
                      sleeve_cols: Sequence[str] = (), sleeve_budget: float = math.inf) -> SimResult:
    """council.reference.backtest.simulate plus one rule: when the orders decided at a close would
    lift the sleeve columns above `sleeve_budget`, every sleeve column held above its target is
    also ordered back to its target (a rebalance never borrows). The order decision at each close
    is council.reference.sleeve.pending_trades, the live rule. With no sleeve columns this is
    exactly `simulate` (a test pins it)."""
    idx = plan.targets.index
    syms = list(plan.targets.columns)
    n, m = len(idx), len(syms)
    rets = returns.reindex(index=idx, columns=syms).to_numpy(dtype=float)
    rets = np.where(np.isfinite(rets), rets, 0.0)
    tgt = np.nan_to_num(plan.targets.to_numpy(dtype=float), nan=0.0)
    lvl = np.nan_to_num(plan.levels.to_numpy(dtype=float), nan=0.0)
    thr = np.nan_to_num(plan.thresholds.to_numpy(dtype=float), nan=np.inf)
    per_side = np.array([costs.per_side[s] for s in syms], dtype=float)
    fixed = np.array([costs.fixed[s] for s in syms], dtype=float)
    sleeve = np.array([s in set(sleeve_cols) for s in syms], dtype=bool)

    held = np.zeros(m)
    held_level = np.zeros(m)
    nav = 1.0
    pending = np.zeros(m, dtype=bool)
    pend_target = np.zeros(m)
    pend_level = np.zeros(m)
    nav_out = np.empty(n)
    w_out = np.empty((n, m))
    dw_out = np.zeros((n, m))
    cost_out = np.zeros(n)
    orders_out = np.full((n, m), np.nan)
    for i in range(n):
        if i > 0:
            growth = 1.0 + float(held @ rets[i])
            if growth <= 0.0:
                raise ValueError(f"{plan.name}: book wiped out on {idx[i]}")
            held = held * (1.0 + rets[i]) / growth
            nav *= growth
        if pending.any():
            new = held.copy()
            new[pending] = pend_target[pending]
            dw = new - held
            traded = np.abs(dw) > _EPS
            cost = float(np.abs(dw) @ per_side + fixed[traded].sum())
            held = new
            held_level[pending] = pend_level[pending]
            nav *= 1.0 - cost
            dw_out[i], cost_out[i] = dw, cost
        pending = sleeve_rule.pending_trades(held, held_level, tgt[i], lvl[i], thr[i], budget_mask=sleeve,
                                             budget=sleeve_budget)
        pend_target, pend_level = tgt[i].copy(), lvl[i].copy()
        orders_out[i, pending] = pend_target[pending]
        nav_out[i], w_out[i] = nav, held
    nav_s = pd.Series(nav_out, index=idx, name=plan.name)
    return SimResult(
        name=plan.name, nav=nav_s, returns=nav_s.pct_change().fillna(0.0),
        weights=pd.DataFrame(w_out, index=idx, columns=syms),
        trades=pd.DataFrame(dw_out, index=idx, columns=syms),
        costs=pd.Series(cost_out, index=idx, name="cost"),
        orders=pd.DataFrame(orders_out, index=idx, columns=syms), cost_model=costs,
    )


@dataclass(frozen=True)
class StopRule:
    """Catastrophe stop for the stop sensitivity: distance = max(min, k x daily sigma x sqrt(h)),
    capped at max; triggered on the daily low; filled at min(open, stop); re-entry after N sessions."""

    min_distance: float
    sigma_multiple: float
    horizon_days: int
    max_distance: float
    sigma_sessions: int
    reentry_sessions: int

    def distance(self, sigma_daily: np.ndarray) -> np.ndarray:
        raw = self.sigma_multiple * np.nan_to_num(sigma_daily, nan=0.0) * math.sqrt(self.horizon_days)
        return np.minimum(np.maximum(self.min_distance, raw), self.max_distance)


def simulate_with_stops(plan: BookPlan, returns: pd.DataFrame, open_rel: pd.DataFrame, low_rel: pd.DataFrame,
                        sigma: pd.DataFrame, costs: CostModel, rule: StopRule, *,
                        sleeve_budget: float) -> tuple[SimResult, int]:
    """`simulate_budgeted` for a stand-alone sleeve with a catastrophe stop on every opened position.

    `open_rel` / `low_rel` are the session's open and low relative to the previous close. A stop is
    set when a position opens (entry = that execution close) and kept on top-ups; it triggers when
    the low reaches it, the day's return is the fill (the open when it gaps through, else the stop),
    the position is sold (a leg) and the name waits `reentry_sessions` before its target applies
    again. Returns the path and the number of stop hits."""
    idx = plan.targets.index
    syms = list(plan.targets.columns)
    n, m = len(idx), len(syms)

    def arr(frame: pd.DataFrame) -> np.ndarray:
        return frame.reindex(index=idx, columns=syms).to_numpy(dtype=float)

    rets = np.where(np.isfinite(arr(returns)), arr(returns), 0.0)
    orel, lrel, sig = arr(open_rel), arr(low_rel), arr(sigma)
    tgt = np.nan_to_num(plan.targets.to_numpy(dtype=float), nan=0.0)
    lvl = np.nan_to_num(plan.levels.to_numpy(dtype=float), nan=0.0)
    thr = np.nan_to_num(plan.thresholds.to_numpy(dtype=float), nan=np.inf)
    per_side = np.array([costs.per_side[s] for s in syms], dtype=float)
    fixed = np.array([costs.fixed[s] for s in syms], dtype=float)
    every = np.ones(m, dtype=bool)                    # the stand-alone sleeve: every column is budgeted
    held, held_level = np.zeros(m), np.zeros(m)
    dist, ratio = np.full(m, np.nan), np.ones(m)
    cool_until = np.full(m, -1)
    nav, hits = 1.0, 0
    pending = np.zeros(m, dtype=bool)
    pend_target, pend_level = np.zeros(m), np.zeros(m)
    nav_out, w_out = np.empty(n), np.empty((n, m))
    dw_out, cost_out = np.zeros((n, m)), np.zeros(n)
    orders_out = np.full((n, m), np.nan)
    for i in range(n):
        stopped = np.zeros(m, dtype=bool)
        if i > 0:
            r = rets[i].copy()
            stop_rel = (1.0 - dist) / ratio - 1.0
            armed = (held > _EPS) & np.isfinite(dist) & np.isfinite(lrel[i])
            stopped = armed & (lrel[i] <= stop_rel)
            if stopped.any():
                gap_open = np.where(np.isfinite(orel[i]), orel[i], stop_rel)
                r[stopped] = np.minimum(gap_open, stop_rel)[stopped]
            growth = 1.0 + float(held @ r)
            if growth <= 0.0:
                raise ValueError(f"{plan.name}: book wiped out on {idx[i]}")
            held = held * (1.0 + r) / growth
            nav *= growth
            ratio = ratio * (1.0 + r)
        if stopped.any():
            dw = np.where(stopped, -held, 0.0)
            cost = float(np.abs(dw) @ per_side + fixed[stopped].sum())
            held = np.where(stopped, 0.0, held)
            held_level[stopped] = 0.0
            nav *= 1.0 - cost
            dw_out[i] += dw
            cost_out[i] += cost
            cool_until[stopped] = i + rule.reentry_sessions
            dist[stopped] = np.nan
            pending = pending & ~stopped
            hits += int(stopped.sum())
        if pending.any():
            new = held.copy()
            new[pending] = pend_target[pending]
            dw = new - held
            traded = np.abs(dw) > _EPS
            cost = float(np.abs(dw) @ per_side + fixed[traded].sum())
            opened = (held <= _EPS) & (new > _EPS)
            closed = new <= _EPS
            held = new
            held_level[pending] = pend_level[pending]
            nav *= 1.0 - cost
            dw_out[i] += dw
            cost_out[i] += cost
            dist[opened] = rule.distance(sig[i])[opened]
            ratio[opened] = 1.0
            dist[closed] = np.nan
        t, lv = tgt[i].copy(), lvl[i].copy()
        cooling = cool_until >= i
        t[cooling], lv[cooling] = 0.0, 0.0
        pending = sleeve_rule.pending_trades(held, held_level, t, lv, thr[i], budget_mask=every,
                                             budget=sleeve_budget)
        pend_target, pend_level = t, lv
        orders_out[i, pending] = pend_target[pending]
        nav_out[i], w_out[i] = nav, held
    nav_s = pd.Series(nav_out, index=idx, name=plan.name)
    sim = SimResult(name=plan.name, nav=nav_s, returns=nav_s.pct_change().fillna(0.0),
                    weights=pd.DataFrame(w_out, index=idx, columns=syms),
                    trades=pd.DataFrame(dw_out, index=idx, columns=syms),
                    costs=pd.Series(cost_out, index=idx, name="cost"),
                    orders=pd.DataFrame(orders_out, index=idx, columns=syms), cost_model=costs)
    return sim, hits


def stock_returns(inp: Inputs, keys: Iterable[str], calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Close-to-close returns of adjusted closes on the calendar. After a security's last bar the
    close is carried forward (zero return: a delisted holding sits as cash until it is sold)."""
    keys = sorted(set(keys))
    raw = inp.all_closes.reindex(columns=keys)
    full = raw.reindex(raw.index.union(calendar)).sort_index().ffill()
    return full.reindex(calendar).pct_change(fill_method=None)


def sleeve_plan(rows: pd.DatetimeIndex, decisions: Mapping[pd.Timestamp, Sequence[str]], *, share: float,
                n: int, level: pd.Series | None, deadband_level: float, min_share: float,
                name: str) -> BookPlan:
    """Equal-weight sleeve: from each decision close D the selected names target unit x level
    (council.reference.sleeve: unit = share / n); level changes force trades (R11), drift trades
    past max(deadband x unit, min share)."""
    names = sorted({k for sel in decisions.values() for k in sel})
    col = {k: j for j, k in enumerate(names)}
    lv = np.ones(len(rows)) if level is None else level.reindex(rows).fillna(1.0).to_numpy(dtype=float)
    targets = np.zeros((len(rows), len(names)))
    levels = np.zeros_like(targets)
    dates = sorted(decisions)
    unit = sleeve_rule.unit_weight(share, n)
    for a, d in enumerate(dates):
        i0 = int(rows.searchsorted(d))
        i1 = int(rows.searchsorted(dates[a + 1])) if a + 1 < len(dates) else len(rows)
        js = [col[k] for k in decisions[d]]
        if not js or i0 >= i1:
            continue
        targets[i0:i1, js] = unit * lv[i0:i1, None]
        levels[i0:i1, js] = lv[i0:i1, None]
    th = np.full_like(targets, sleeve_rule.drift_threshold(unit, deadband_level, min_share))
    return BookPlan(name=name, targets=pd.DataFrame(targets, index=rows, columns=names),
                    levels=pd.DataFrame(levels, index=rows, columns=names),
                    thresholds=pd.DataFrame(th, index=rows, columns=names))


def ew_plan(rows: pd.DatetimeIndex, members: Mapping[pd.Timestamp, Sequence[str]], name: str) -> BookPlan:
    """Equal weight over the whole eligible set, fully rebalanced at each decision close."""
    names = sorted({k for sel in members.values() for k in sel})
    col = {k: j for j, k in enumerate(names)}
    targets = np.zeros((len(rows), len(names)))
    levels = np.zeros_like(targets)
    th = np.full_like(targets, np.inf)
    dates = sorted(members)
    for a, d in enumerate(dates):
        i0 = int(rows.searchsorted(d))
        i1 = int(rows.searchsorted(dates[a + 1])) if a + 1 < len(dates) else len(rows)
        js = [col[k] for k in members[d]]
        if not js or i0 >= i1:
            continue
        targets[i0:i1, js] = 1.0 / len(js)
        levels[i0:i1, js] = 1.0
        th[i0, :] = 0.0
    return BookPlan(name=name, targets=pd.DataFrame(targets, index=rows, columns=names),
                    levels=pd.DataFrame(levels, index=rows, columns=names),
                    thresholds=pd.DataFrame(th, index=rows, columns=names))


def flat_costs(names: Iterable[str], per_side: float, fixed: float, cls: str) -> CostModel:
    names = list(names)
    return CostModel(per_side=dict.fromkeys(names, per_side), fixed=dict.fromkeys(names, fixed),
                     classes=dict.fromkeys(names, cls))


def scale_costs(model: CostModel, *, var_mult: float = 1.0, fee_mult: float = 1.0) -> CostModel:
    return CostModel(per_side={k: v * var_mult for k, v in model.per_side.items()},
                     fixed={k: v * fee_mult for k, v in model.fixed.items()}, classes=dict(model.classes))


def merge_costs(*models: CostModel) -> CostModel:
    per, fix, cls = {}, {}, {}
    for mdl in models:
        per.update(mdl.per_side)
        fix.update(mdl.fixed)
        cls.update(mdl.classes)
    return CostModel(per_side=per, fixed=fix, classes=cls)


def concat_plans(name: str, *plans: BookPlan) -> BookPlan:
    rows = plans[0].targets.index
    for p in plans[1:]:
        if not p.targets.index.equals(rows):
            raise ValueError("plans must share rows")
    return BookPlan(name=name,
                    targets=pd.concat([p.targets for p in plans], axis=1),
                    levels=pd.concat([p.levels for p in plans], axis=1),
                    thresholds=pd.concat([p.thresholds for p in plans], axis=1))


def shift_decisions(rows: pd.DatetimeIndex, decisions: Mapping[pd.Timestamp, Sequence[str]],
                    sessions: int) -> dict[pd.Timestamp, Sequence[str]]:
    """Decisions taken `sessions` sessions later (the execution-lag sensitivity)."""
    out = {}
    for d, sel in decisions.items():
        i = min(int(rows.searchsorted(d)) + sessions, len(rows) - 1)
        out[rows[i]] = sel
    return out


# ------------------------------------------------------------------------------------ metrics


def slice_sim(sim: SimResult, start: pd.Timestamp | None, end: pd.Timestamp | None) -> SimResult | None:
    """The book over [start, end): NAV rebased at the last close before `start`."""
    idx = sim.nav.index
    lo = 0 if start is None else max(0, int(idx.searchsorted(start)) - 1)
    hi = len(idx) if end is None else int(idx.searchsorted(end))
    if hi - lo < 3:
        return None
    sl = slice(lo, hi)
    nav = sim.nav.iloc[sl] / sim.nav.iloc[lo]
    return SimResult(name=sim.name, nav=nav, returns=nav.pct_change().fillna(0.0),
                     weights=sim.weights.iloc[sl], trades=sim.trades.iloc[sl].copy().where(
                         pd.Series(np.arange(hi - lo) > 0, index=nav.index), 0.0, axis=0),
                     costs=sim.costs.iloc[sl].where(pd.Series(np.arange(hi - lo) > 0, index=nav.index), 0.0),
                     orders=sim.orders.iloc[sl], cost_model=sim.cost_model)


def metrics(sim: SimResult | None) -> dict[str, Any]:
    if sim is None:
        return {}
    m = performance(sim)
    years = m["years"] or 0.0
    fixed = pd.Series(sim.cost_model.fixed).reindex(sim.trades.columns).fillna(0.0).to_numpy()
    traded = (sim.trades.abs() > _EPS).to_numpy()
    fees = float((traded * fixed).sum())
    legs = int(traded.sum())
    m["fee_drag_per_year"] = fees / years if years > 0 else None
    m["variable_cost_drag_per_year"] = (float(sim.costs.sum()) - fees) / years if years > 0 else None
    m["legs_per_year"] = legs / years if years > 0 else None
    return m


def sharpe_of(returns: pd.Series) -> float | None:
    r = returns.iloc[1:] if len(returns) > 1 else returns
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    return float(r.mean()) / sd * math.sqrt(252) if sd > 0 else None


def concentration(sim: SimResult, stock_rets: pd.DataFrame) -> dict[str, Any]:
    """Each name's arithmetic contribution (weight at the previous close x the day's return), the top
    contributor's share of the total, and the Sharpe with the top contributor's slot held as cash."""
    w_prev = sim.weights.shift(1).fillna(0.0)
    r = stock_rets.reindex(index=sim.weights.index, columns=sim.weights.columns).fillna(0.0)
    contrib = (w_prev * r).sum()
    if contrib.empty:
        return {"top": None, "top_share": None, "sharpe_ex_top": sharpe_of(sim.returns)}
    top = str(contrib.idxmax())
    total = float(contrib.sum())
    ex = sim.returns - (w_prev[top] * r[top])
    return {"top": top, "top_share": float(contrib[top]) / total if total > 0 else None,
            "sharpe_ex_top": sharpe_of(ex), "contributions": contrib.sort_values(ascending=False)}


def per_year(sim: SimResult) -> dict[int, float]:
    nav = sim.nav
    out = {}
    prev = float(nav.iloc[0])
    for year, g in nav.groupby(nav.index.year):
        out[int(year)] = float(g.iloc[-1]) / prev - 1.0
        prev = float(g.iloc[-1])
    return out


def percentile(value: float | None, null: Sequence[float | None]) -> float | None:
    """Share of the null strictly below `value`, ties counting half. A missing value or null entry
    (no Sharpe) ranks lowest."""
    arr = np.array([-np.inf if x is None or not np.isfinite(x) else x for x in null], dtype=float)
    if arr.size == 0:
        return None
    v = -np.inf if value is None or not np.isfinite(value) else float(value)
    return float(((arr < v).sum() + 0.5 * (arr == v).sum()) / arr.size)


def max_percentile_null(null: pd.DataFrame, cells: Sequence[str], column: str = "sharpe") -> list[float]:
    """For each draw index, the highest percentile any of `cells` reaches within its own null. The
    rule's own-null percentile is ranked against this distribution (Westfall-Young max-T on
    percentiles), which corrects for picking the best of several correlated, unequal cells."""
    per_cell = []
    for c in cells:
        s = null[null["cell"] == c].set_index("draw")[column].astype(float).fillna(-np.inf)
        per_cell.append((s.rank(method="average") - 0.5) / len(s))   # = `percentile` of each draw in its own null
    if not per_cell:
        return []
    return list(pd.concat(per_cell, axis=1).dropna().max(axis=1))


# ------------------------------------------------------------------------------------ market data


@dataclass
class Market:
    """Index and ETF history: `core` by line symbol (the main window), `controls` by ticker (main
    window), `long` by ticker from the stress history start (equity only)."""

    core: dict[str, pd.Series]
    controls: dict[str, pd.Series]
    long: dict[str, pd.Series]


def required_tickers(spec: Mapping[str, Any], policy: Policy) -> tuple[list[str], list[str]]:
    """(equity tickers, crypto tickers) the study needs; `run` fails if any is missing."""
    lines = core_lines(policy)
    eq = {ln.signal.ticker for ln in lines if ln.signal.source != "binance"}
    eq |= set(spec["reported"]["G4_index"]["vs"]) | {spec["overlay"]["signal"]}
    eq |= set(index_mix(spec, policy)) | {spec["stress"]["proxy"]["equal_weight"], spec["stress"]["proxy"]["before"]}
    eq |= set(spec["data"]["survivorship"].values())
    crypto = {ln.signal.ticker for ln in lines if ln.signal.source == "binance"}
    return sorted(eq), sorted(crypto)


def market_from(by_ticker: Mapping[str, pd.Series], spec: Mapping[str, Any], policy: Policy, *,
                main_start: date) -> Market:
    eq, crypto = required_tickers(spec, policy)
    missing = [t for t in (*eq, *crypto) if t not in by_ticker or by_ticker[t].dropna().empty]
    if missing:
        raise SystemExit(f"history missing for {missing}: the study needs every pre-registered series")
    start = pd.Timestamp(main_start)
    main = {t: s[s.index >= start] for t, s in by_ticker.items()}
    lines = core_lines(policy)
    core = {ln.symbol: main[ln.signal.ticker] for ln in lines}
    controls = {t: main[t] for t in eq}
    long = {t: by_ticker[t] for t in eq}
    return Market(core=core, controls=controls, long=long)


def real_market(spec: Mapping[str, Any], policy: Policy, *, main_start: date, end: date) -> Market:
    """Tiingo (equity ETFs, from the stress history start) and Binance (crypto) through the data layer."""
    br = importlib.import_module("backtest_reference")
    tiingo = importlib.import_module("council.data.tiingo")
    binance = importlib.import_module("council.data.binance")
    credentials = importlib.import_module("council.data.credentials")
    token = credentials.secret(br.TIINGO_SECRET)
    if not token:
        raise SystemExit("no Tiingo token (run with COUNCIL_MODE=dry_run)")
    eq, crypto = required_tickers(spec, policy)
    long_start = date.fromisoformat(str(spec["stress"]["history_start"]))
    out: dict[str, pd.Series] = {}
    for t in eq:
        raw = br.call_flexible(tiingo.fetch_daily, ticker=t, start=long_start, end=end, token=token)
        out[t] = br.to_close_series(raw, t)
    for t in crypto:
        if br._accepts(binance.fetch_klines, "start"):
            raw = br.call_flexible(binance.fetch_klines, ticker=t, interval="1d", start=main_start, end=end)
        else:
            raw = br.fetch_binance_daily_history(t, start=main_start, end=end)
        out[t] = br.to_close_series(raw, t)
    out = {t: s[s.index <= pd.Timestamp(end)] for t, s in out.items()}
    return market_from(out, spec, policy, main_start=main_start)


def market_hashes(market: Market) -> dict[str, str]:
    """sha256 of every fetched market series: each equity ticker from the stress-history start, and
    each core line's series in the main window as "core:<line>" (the crypto lines appear only there)."""
    def digest(s: pd.Series) -> str:
        return hashlib.sha256(pd.util.hash_pandas_object(s).to_numpy().tobytes()).hexdigest()

    out = {k: digest(v) for k, v in sorted(market.long.items())}
    out.update({f"core:{k}": digest(v) for k, v in sorted(market.core.items())})
    return out


def synthetic_market(spec: Mapping[str, Any], policy: Policy, *, main_start: date, end: date, seed: int) -> Market:
    eq, crypto = required_tickers(spec, policy)
    long_start = date.fromisoformat(str(spec["stress"]["history_start"]))
    by = synthetic_closes(eq, start=long_start, end=end, crypto_tickers=crypto,
                          crypto_start=max(main_start, date(2017, 8, 17)), seed=seed)
    return market_from(by, spec, policy, main_start=main_start)


# ------------------------------------------------------------------------------------ study


@dataclass
class Context:
    spec: dict[str, Any]
    inp: Inputs
    policy: Policy
    calendar: pd.DatetimeIndex
    rows: pd.DatetimeIndex
    dates: list[pd.Timestamp]
    eligible: dict[pd.Timestamp, pd.DataFrame]
    funnels: dict[pd.Timestamp, dict[str, int]]
    market: Market
    real_nav: float
    fee_usd: float
    draws: int
    synthetic: bool
    _returns: pd.DataFrame | None = None
    _core: dict[str, tuple[Panel, BookPlan, list[LineSpec], Policy]] = field(default_factory=dict)

    @property
    def core_closes(self) -> dict[str, pd.Series]:
        return self.market.core

    @property
    def control_closes(self) -> dict[str, pd.Series]:
        return self.market.controls

    @property
    def slippage_bps(self) -> float:
        return float(self.spec["costs"]["slippage_bps"])

    def returns_for(self, keys: Iterable[str]) -> pd.DataFrame:
        """Stock returns on the calendar for every name ever eligible, computed once."""
        if self._returns is None:
            universe = {k for e in self.eligible.values() for k in e.index}
            self._returns = stock_returns(self.inp, universe, self.calendar)
        keys = list(keys)
        missing = [k for k in keys if k not in self._returns.columns]
        if missing:
            self._returns = pd.concat([self._returns, stock_returns(self.inp, missing, self.calendar)], axis=1)
        return self._returns[keys]

    @property
    def split(self) -> pd.Timestamp:
        return pd.Timestamp(self.spec["window"]["split"])

    def stock_costs(self, names: Iterable[str], *, share: float, var_mult: float = 1.0,
                    fee_mult: float = 1.0) -> CostModel:
        c = self.spec["costs"]
        per_side = var_mult * (float(c["stock_spread_floor_bps"]) + float(c["slippage_bps"])) / 1e4
        fixed = fee_mult * self.fee_usd / (self.real_nav * share)   # fee per leg, as a share of the simulated book
        return flat_costs(names, per_side, fixed, "stock_real")


def decisions_for(ctx: Context, variant: Mapping[str, Any], n: int, *, buffer_mult: float,
                  eligible: Mapping[pd.Timestamp, pd.DataFrame] | None = None) -> tuple[dict, dict]:
    elig_by = eligible or ctx.eligible
    held: list[str] = []
    out, kept = {}, {}
    for d in ctx.dates:
        sel = select_rule(elig_by[d], held, variant, n, buffer_mult)
        kept[d] = len(set(sel) & set(held))
        out[d], held = sel, sel
    return out, kept


def random_decisions(ctx: Context, variant: Mapping[str, Any], n: int, kept: Mapping[pd.Timestamp, int],
                     orders: Mapping[pd.Timestamp, Sequence[str]]) -> dict:
    held: list[str] = []
    out = {}
    for d in ctx.dates:
        sel = select_random(ctx.eligible[d], held, variant, n, kept[d], orders[d])
        out[d], held = sel, sel
    return out


def run_sleeve(ctx: Context, decisions: Mapping, n: int, *, share: float = 1.0, level: pd.Series | None = None,
               var_mult: float = 1.0, fee_mult: float = 1.0, lag: int = 1, name: str = "sleeve") -> SimResult:
    """Stand-alone sleeve: `share` = 1 is the sleeve's own capital (the book's 50%). `lag` = sessions
    from the decision to the execution (1 = the next close, the headline)."""
    sl = ctx.spec["sleeve"]
    book_share = float(sl["share_of_nav"])
    if lag != 1:
        decisions = shift_decisions(ctx.rows, decisions, lag - 1)
    plan = sleeve_plan(ctx.rows, decisions, share=share, n=n, level=level,
                       deadband_level=float(sl["deadband"]["level"]),
                       min_share=float(sl["deadband"]["min_nav_share"]) / book_share, name=name)
    rets = ctx.returns_for(plan.targets.columns)
    costs = ctx.stock_costs(plan.targets.columns, share=book_share, var_mult=var_mult, fee_mult=fee_mult)
    return simulate_budgeted(plan, rets, costs, sleeve_cols=list(plan.targets.columns), sleeve_budget=share)


def run_sleeve_stops(ctx: Context, decisions: Mapping, n: int, *, name: str = "stops") -> tuple[SimResult, int]:
    sl = ctx.spec["sleeve"]
    book_share = float(sl["share_of_nav"])
    plan = sleeve_plan(ctx.rows, decisions, share=1.0, n=n, level=None, deadband_level=float(sl["deadband"]["level"]),
                       min_share=float(sl["deadband"]["min_nav_share"]) / book_share, name=name)
    cols = list(plan.targets.columns)
    rets = ctx.returns_for(cols)
    prev_close = ctx.inp.all_closes.reindex(columns=cols)
    prev_close = prev_close.reindex(prev_close.index.union(ctx.calendar)).sort_index().ffill().reindex(ctx.calendar).shift(1)

    def rel(frame: pd.DataFrame | None) -> pd.DataFrame:
        if frame is None:
            return pd.DataFrame(np.nan, index=ctx.calendar, columns=cols)
        return frame.reindex(index=ctx.calendar, columns=cols).astype(float) / prev_close - 1.0

    s = ctx.spec["stops"]
    rule = StopRule(min_distance=float(s["min_distance"]), sigma_multiple=float(s["sigma_multiple"]),
                    horizon_days=int(s["horizon_days"]), max_distance=float(s["max_distance"]),
                    sigma_sessions=int(s["sigma_sessions"]), reentry_sessions=int(s["reentry_sessions"]))
    sigma = rets.rolling(rule.sigma_sessions, min_periods=rule.sigma_sessions // 2).std()
    costs = ctx.stock_costs(cols, share=book_share)
    return simulate_with_stops(plan, rets, rel(ctx.inp.adj_open), rel(ctx.inp.adj_low), sigma, costs, rule,
                               sleeve_budget=1.0)


def run_ew(ctx: Context, *, var_mult: float = 1.0, members: Mapping | None = None, cost: bool = True,
           name: str = "ew_universe") -> SimResult:
    members = members or {d: list(ctx.eligible[d].index) for d in ctx.dates}
    plan = ew_plan(ctx.rows, members, name)
    rets = ctx.returns_for(plan.targets.columns)
    c = ctx.spec["costs"]
    per_side = var_mult * (float(c["stock_spread_floor_bps"]) + float(c["slippage_bps"])) / 1e4 if cost else 0.0
    return simulate(plan, rets, flat_costs(plan.targets.columns, per_side, 0.0, "stock_real"))


def rebased_policy(policy: Policy, core_share: float) -> Policy:
    ref = [ln for ln in policy.universe.lines if ln.in_reference]
    total = sum(ln.base_weight for ln in ref)
    k = core_share / total
    lines = [ln.model_copy(update={"base_weight": ln.base_weight * k}) if ln.in_reference else ln
             for ln in policy.universe.lines]
    universe = policy.universe.model_copy(update={"lines": lines, "reference_gross_max": core_share})
    return policy.model_copy(update={"universe": universe})


def index_mix(spec: Mapping[str, Any], policy: Policy) -> dict[str, float]:
    """The index-sleeve counterfactual: the named lines' signal ETFs in proportion to base weights."""
    by = policy.universe.by_symbol()
    lines = [by[s] for s in spec["baselines"]["index_sleeve"]["lines"]]
    total = sum(ln.base_weight for ln in lines)
    return {ln.signal.ticker: ln.base_weight / total for ln in lines}


def level_from(spy: pd.Series, calendar: pd.DatetimeIndex, table: Mapping[str, float], policy: Policy) -> pd.Series:
    sig = line_signals(spy, asset_class="index", policy=policy)
    trend = asof_align(sig["trend"], calendar)
    return trend.map(lambda s: sleeve_rule.overlay_level(table, s)).astype(float)


def overlay_level(ctx: Context, option: str) -> pd.Series:
    """Sleeve level from the SPX line's trend state (SPY closes, V3 trend parameters)."""
    table = ctx.spec["overlay"]["options"][option]
    spy = ctx.control_closes.get(ctx.spec["overlay"]["signal"])
    if spy is None:
        raise ValueError("overlay needs SPY closes")
    return level_from(spy, ctx.calendar, table, ctx.policy).reindex(ctx.rows).astype(float)


def core_lines(policy: Policy) -> list[LineSpec]:
    return [ln for ln in policy.universe.lines if ln.in_reference]


def rebased_core(ctx: Context, *, stress: bool = False) -> tuple[Panel, BookPlan, list[LineSpec], Policy]:
    """The re-based core's panel and plan (independent of the sleeve; computed once per context)."""
    key = "stress" if stress else "main"
    if key not in ctx._core:
        pol = rebased_policy(ctx.policy, float(ctx.spec["whole_book"]["core_share"]))
        lines = core_lines(pol)
        if stress:
            long = ctx.market.long
            closes = {ln.symbol: long[ln.signal.ticker] for ln in lines if ln.signal.ticker in long}
            windows = ctx.spec["stress"]["windows"]
            first = min(pd.Timestamp(w[0]) for w in windows.values())
            last = max(pd.Timestamp(w[1]) for w in windows.values())
            calendar = equity_calendar(closes, lines, last.date())
            start = first
        else:
            closes = {ln.symbol: ctx.core_closes[ln.symbol] for ln in lines if ln.symbol in ctx.core_closes}
            calendar, start = ctx.calendar, ctx.rows[0]
        panel = build_panel(closes, lines, pol, calendar)
        plan, _ = reference_plan(panel, lines, pol, start=start, name="core")
        ctx._core[key] = (panel, plan, lines, pol)
    return ctx._core[key]


def core_costs(ctx: Context, lines: Sequence[LineSpec], pol: Policy, *, var_mult: float = 1.0,
               fee_mult: float = 1.0) -> CostModel:
    base = cost_model(lines, pol, mode="listed", commission_nav=ctx.real_nav, slippage_bps=ctx.slippage_bps)
    return scale_costs(base, var_mult=var_mult, fee_mult=fee_mult)


def run_current_reference(ctx: Context) -> SimResult:
    lines = core_lines(ctx.policy)
    closes = {ln.symbol: ctx.core_closes[ln.symbol] for ln in lines if ln.symbol in ctx.core_closes}
    cfg = BacktestConfig(start=ctx.rows[0].date(), end=ctx.rows[-1].date(), commission_nav=ctx.real_nav,
                         slippage_bps=ctx.slippage_bps)
    run = run_backtest(closes, ctx.control_closes, ctx.policy, cfg, lines=lines)
    return run.books["reference"]


def _book(ctx: Context, name: str, sleeve: BookPlan, sleeve_rets: pd.DataFrame, sleeve_costs: CostModel, *,
          var_mult: float, fee_mult: float) -> SimResult:
    panel, core_plan, lines, pol = rebased_core(ctx)
    core = BookPlan(name="core", targets=core_plan.targets.reindex(ctx.rows), levels=core_plan.levels.reindex(ctx.rows),
                    thresholds=core_plan.thresholds.reindex(ctx.rows))
    plan = concat_plans(name, core, sleeve)
    rets = pd.concat([panel.returns.reindex(ctx.rows), sleeve_rets.reindex(ctx.rows)], axis=1)
    costs = merge_costs(core_costs(ctx, lines, pol, var_mult=var_mult, fee_mult=fee_mult), sleeve_costs)
    return simulate_budgeted(plan, rets, costs, sleeve_cols=list(sleeve.targets.columns),
                             sleeve_budget=float(ctx.spec["whole_book"]["sleeve_share"]))


def run_whole_book(ctx: Context, decisions: Mapping, n: int, option: str, *, var_mult: float = 1.0,
                   fee_mult: float = 1.0) -> SimResult:
    share = float(ctx.spec["whole_book"]["sleeve_share"])
    sl = ctx.spec["sleeve"]
    level = None if option == "none" else overlay_level(ctx, option)
    splan = sleeve_plan(ctx.rows, decisions, share=share, n=n, level=level,
                        deadband_level=float(sl["deadband"]["level"]),
                        min_share=float(sl["deadband"]["min_nav_share"]), name="sleeve")
    costs = ctx.stock_costs(splan.targets.columns, share=1.0, var_mult=var_mult, fee_mult=fee_mult)
    return _book(ctx, f"book_{option}", splan, ctx.returns_for(splan.targets.columns), costs,
                 var_mult=var_mult, fee_mult=fee_mult)


def run_index_book(ctx: Context, option: str, *, mix: Mapping[str, float] | None = None, var_mult: float = 1.0,
                   fee_mult: float = 1.0, name: str | None = None) -> SimResult:
    """The same re-based book with the sleeve's share held in index ETFs (the B2 counterfactual):
    same overlay option, same deadband rule, the ETF cost class, the sealed fee per leg."""
    mix = dict(mix or index_mix(ctx.spec, ctx.policy))
    share = float(ctx.spec["whole_book"]["sleeve_share"])
    db = ctx.spec["sleeve"]["deadband"]
    lv = np.ones(len(ctx.rows)) if option == "none" else overlay_level(ctx, option).fillna(1.0).to_numpy()
    cols = list(mix)
    w = np.array([share * mix[c] for c in cols])
    targets = pd.DataFrame(lv[:, None] * w[None, :], index=ctx.rows, columns=cols)
    levels = pd.DataFrame(np.repeat(lv[:, None], len(cols), axis=1), index=ctx.rows, columns=cols)
    th_line = np.array([sleeve_rule.drift_threshold(x, float(db["level"]), float(db["min_nav_share"])) for x in w])
    th = pd.DataFrame(np.broadcast_to(th_line, targets.shape).copy(), index=ctx.rows, columns=cols)
    iplan = BookPlan(name="index_sleeve", targets=targets, levels=levels, thresholds=th)
    rets = aligned_returns({c: ctx.control_closes[c] for c in cols}, ctx.calendar)
    costs = scale_costs(control_cost_model(cols, ctx.policy, mode="listed", commission_nav=ctx.real_nav,
                                           slippage_bps=ctx.slippage_bps), var_mult=var_mult, fee_mult=fee_mult)
    return _book(ctx, name or f"index_book_{option}", iplan, rets, costs, var_mult=var_mult, fee_mult=fee_mult)


def proxy_returns(ctx: Context) -> pd.Series:
    """The equal-weight index where it exists, the cap-weighted index before (long history)."""
    p = ctx.spec["stress"]["proxy"]
    ew = ctx.market.long[p["equal_weight"]].astype(float).dropna().pct_change()
    before = ctx.market.long[p["before"]].astype(float).dropna().pct_change()
    start = ew.dropna().index.min()
    return pd.concat([before[before.index < start], ew[ew.index >= start]]).sort_index()


def sleeve_beta(ctx: Context, sim: SimResult) -> float:
    """Beta of the stand-alone sleeve's daily return on the proxy's, over the study rows."""
    proxy = proxy_returns(ctx).reindex(sim.returns.index)
    frame = pd.concat([sim.returns.rename("s"), proxy.rename("p")], axis=1).iloc[1:].dropna()
    var = float(frame["p"].var())
    return float(frame["s"].cov(frame["p"]) / var) if var > 0 else 1.0


def run_stress_book(ctx: Context, option: str, beta: float, n: int) -> SimResult:
    """The re-based core on the long history plus the sleeve as a beta-scaled proxy column, with the
    overlay option's level from the long SPY history. Only level changes trade the proxy."""
    panel, core_plan, lines, pol = rebased_core(ctx, stress=True)
    rows = core_plan.targets.index
    share = float(ctx.spec["whole_book"]["sleeve_share"])
    table = ctx.spec["overlay"]["options"][option]
    lv = level_from(ctx.market.long[ctx.spec["overlay"]["signal"]], panel.calendar, table, ctx.policy)
    lv = lv.reindex(rows).fillna(1.0).to_numpy()
    sleeve = BookPlan(name="proxy", targets=pd.DataFrame({PROXY: share * lv}, index=rows),
                      levels=pd.DataFrame({PROXY: lv}, index=rows),
                      thresholds=pd.DataFrame({PROXY: np.inf}, index=rows))
    plan = concat_plans(f"stress_{option}", core_plan, sleeve)
    rets = pd.concat([panel.returns.reindex(rows),
                      (beta * proxy_returns(ctx)).reindex(rows).rename(PROXY).to_frame()], axis=1)
    c = ctx.spec["costs"]
    per_side = (float(c["stock_spread_floor_bps"]) + float(c["slippage_bps"])) / 1e4
    costs = merge_costs(core_costs(ctx, lines, pol), flat_costs([PROXY], per_side, n * ctx.fee_usd / ctx.real_nav,
                                                                "stock_real"))
    return simulate_budgeted(plan, rets, costs, sleeve_cols=[PROXY], sleeve_budget=share)


def stress_drawdowns(ctx: Context, sim: SimResult) -> dict[str, float | None]:
    out = {}
    for name, (a, b) in ctx.spec["stress"]["windows"].items():
        part = slice_sim(sim, pd.Timestamp(a), pd.Timestamp(b) + pd.Timedelta(days=1))
        out[name] = None if part is None else float(performance(part)["max_drawdown"])
    return out


def run_controls(ctx: Context) -> dict[str, SimResult]:
    tickers = list(ctx.spec["reported"]["G4_index"]["vs"])
    rets = aligned_returns({t: ctx.control_closes[t] for t in tickers}, ctx.calendar).reindex(ctx.rows)
    costs = control_cost_model(tickers, ctx.policy, mode="listed", commission_nav=ctx.real_nav,
                               slippage_bps=ctx.slippage_bps)
    out = {}
    for t in tickers:
        p = fixed_mix_plan(rets, {t: 1.0}, start=ctx.rows[0], name=f"{t.lower()}_bh", rebalance="never")
        out[p.name] = simulate(p, rets, costs)
    return out


def choose_cell(cells: Mapping[str, dict[str, Any]], tie: float) -> tuple[str, str]:
    """Selection rule: highest net Sharpe; within `tie` of the best, the lowest total cost drag."""
    ok = {k: c for k, c in cells.items() if c["sharpe"] is not None}
    if not ok:
        raise ValueError("no cell has a Sharpe ratio")
    best = max(c["sharpe"] for c in ok.values())
    tied = [k for k, c in ok.items() if best - c["sharpe"] <= tie]
    pick = min(tied, key=lambda k: (ok[k]["cost_drag_per_year"], k))
    return pick, f"highest net Sharpe among {len(ok)} selectable cells ({len(tied)} within {tie} of the best; lowest cost drag wins)"


def worst_drawdown(b: Mapping[str, Any]) -> float:
    dds = [b["max_drawdown"], b.get("max_drawdown_x2", b["max_drawdown"]),
           *[v for v in (b.get("stress") or {}).values()]]
    return min(-1.0 if v is None else float(v) for v in dds)


def choose_overlay(books: Mapping[str, dict[str, Any]], halt_dd: float) -> tuple[str, bool]:
    """Overlay rule: an option qualifies when its re-based book stays shallower than the HALT line in
    sample at base costs, at doubled costs and in every proxy stress window. Among those, the highest
    CAGR; within 0.25 pp by Sharpe, then by the fewest legs. None qualifying -> the option with the
    shallowest worst drawdown, and B1 fails."""
    ok = {k: b for k, b in books.items() if worst_drawdown(b) > halt_dd and b["cagr"] is not None}
    if not ok:
        return max(books, key=lambda k: worst_drawdown(books[k])), False
    top = max(b["cagr"] for b in ok.values())
    tied = [k for k, b in ok.items() if top - b["cagr"] <= 0.0025]
    order = {"none": 0, "down_only": 1, "reference": 2}
    pick = sorted(tied, key=lambda k: (-(ok[k]["sharpe"] or -9), ok[k]["legs_per_year"] or 0, order.get(k, 9)))[0]
    return pick, True


def evaluate_gate(*, g1: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]], adj_pct: float | None,
                  sub_sel: Mapping[str, Mapping[str, Any]], sub_pool: Mapping[str, Mapping[str, Any]], book_ok: bool,
                  book: Mapping[str, Any], index_book: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """G1: the fee-free sleeve vs the pool, CAGR and Sharpe, at base and doubled variable costs
    (`g1` = {"base": (sleeve, pool), "x2": (sleeve, pool)}). G2: the rule's max-percentile-adjusted
    percentile. G3: fee-free sleeve Sharpe above the pool's in each sub-period. B1: the overlay rule
    qualified an option. B2: the book with the sleeve vs the book with the index sleeve."""
    def ge(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and a >= b

    def gt(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and a > b

    g = spec["gate"]
    b2 = g["B2_index_sleeve"]
    checks = {
        "G1_pool": all(ge(s.get("cagr"), p.get("cagr")) and ge(s.get("sharpe"), p.get("sharpe")) for s, p in g1.values())
        and set(g1) == {"base", "x2"},
        "G2_random": adj_pct is not None and adj_pct >= float(g["G2_random"]["min_percentile"]),
        "G3_regimes": all(gt(sub_sel.get(p, {}).get("sharpe"), sub_pool.get(p, {}).get("sharpe"))
                          for p in g["G3_regimes"]["periods"]),
        "B1_book": bool(book_ok),
        "B2_index_sleeve": ge(book.get("sharpe"), None if index_book.get("sharpe") is None
                              else index_book["sharpe"] + float(b2["min_sharpe_diff"]))
        and ge(book.get("max_drawdown"), None if index_book.get("max_drawdown") is None
               else index_book["max_drawdown"] - float(b2["max_drawdown_worse_by_at_most"])),
    }
    return {"checks": checks, "adopt": all(checks.values())}


def build_context(spec: dict[str, Any], inp: Inputs, policy: Policy, *, market: Market, real_nav: float,
                  fee_usd: float, draws: int, synthetic: bool, verbose: bool = True) -> Context:
    lines = core_lines(policy)
    calendar = equity_calendar(market.core, lines, None)
    end = min(inp.closes.index.max(), calendar.max())
    calendar = calendar[calendar <= end]
    start = pd.Timestamp(spec["window"]["first_decision_anchor"])
    dates = rebalance_dates(calendar, spec, start=start, end=end)
    if not dates:
        raise ValueError("no rebalance date inside the window")
    rows = calendar[calendar >= dates[0]]
    uni = Universe(inp, spec)
    eligible, funnels = {}, {}
    min_peer = int(spec["rule"]["min_peer_group"])
    for d in dates:
        e, f = uni.at(d)
        eligible[d], funnels[d] = add_scores(e, min_peer), f
        if verbose:
            print(f"  {d.date()}: {f['member']} members -> {f['plausible']} eligible")
    return Context(spec=spec, inp=inp, policy=policy, calendar=calendar, rows=rows, dates=dates,
                   eligible=eligible, funnels=funnels, market=market, real_nav=real_nav, fee_usd=fee_usd,
                   draws=draws, synthetic=synthetic)


def run_null(ctx: Context, cells: Mapping[str, tuple[Mapping[str, Any], int]], kept_by: Mapping[str, Mapping],
             *, fee_mult: float = 1.0, nav_mult: float = 1.0, concentration_for: str | None = None,
             verbose: bool = False, label: str = "null") -> pd.DataFrame:
    """The common-random-numbers null: per draw index one order per date, shared by every cell."""
    seed = int(ctx.spec["baselines"]["random"]["seed"])
    rows = []
    t0 = time.time()
    for draw in range(ctx.draws):
        orders = crn_orders(ctx.eligible, ctx.dates, seed, draw)
        for cell, (variant, n) in cells.items():
            dec = random_decisions(ctx, variant, n, kept_by[cell], orders)
            sim = run_sleeve(ctx, dec, n, fee_mult=fee_mult / nav_mult, name=f"{cell}-r{draw}")
            m = metrics(sim)
            row = {"cell": cell, "draw": draw, "sharpe": m["sharpe"], "cagr": m["cagr"],
                   "max_drawdown": m["max_drawdown"], "cost_drag_per_year": m["cost_drag_per_year"],
                   "fee_drag_per_year": m["fee_drag_per_year"], "turnover_per_year": m["turnover_per_year"],
                   "legs_per_year": m["legs_per_year"]}
            if cell == concentration_for:
                con = concentration(sim, ctx.returns_for(sim.weights.columns))
                row["sharpe_ex_top"] = con["sharpe_ex_top"]
                row["top_share"] = con["top_share"]
            rows.append(row)
        if verbose and (draw + 1) % max(1, ctx.draws // 10) == 0:
            print(f"  {label}: {draw + 1}/{ctx.draws} draws [{time.time() - t0:.0f}s]")
    return pd.DataFrame(rows)


def stage_gate(ctx: Context, *, verbose: bool = True) -> dict[str, Any]:
    """Selection, the random null, G1-G3, the overlay rule with B1, and B2: everything the gate needs.
    Returned objects are reused by the reported sections of `run_study`. It prints progress only:
    `run_study` shows the selection and the verdict after it has written gate.json."""
    spec = ctx.spec
    buffer_mult = float(spec["hold_buffer_multiple"])
    all_cells = cells_of(spec)
    selectable = list(cells_of(spec, selectable=True))
    cells: dict[str, dict[str, Any]] = {}
    sims: dict[str, SimResult] = {}
    decisions: dict[str, dict] = {}
    kept_by: dict[str, dict] = {}
    for cell, (variant, n) in all_cells.items():
        dec, kept = decisions_for(ctx, variant, n, buffer_mult=buffer_mult)
        sim = run_sleeve(ctx, dec, n, name=cell)
        decisions[cell], kept_by[cell], sims[cell] = dec, kept, sim
        cells[cell] = {**metrics(sim), "selectable": cell in selectable}
    winner, why = choose_cell({k: cells[k] for k in selectable}, float(spec["selection"]["tie_sharpe"]))
    variant, n = all_cells[winner]

    null = run_null(ctx, all_cells, kept_by, concentration_for=winner, verbose=verbose)
    w_null = max_percentile_null(null, selectable) if not null.empty else []
    for cell, c in cells.items():
        z = null[null["cell"] == cell] if not null.empty else null
        c["null_pct_sharpe"] = percentile(c["sharpe"], list(z["sharpe"])) if not z.empty else None
        c["null_pct_cagr"] = percentile(c["cagr"], list(z["cagr"])) if not z.empty else None
        c["null_median_sharpe"] = float(z["sharpe"].median()) if not z.empty else None
        c["null_p95_sharpe"] = float(z["sharpe"].quantile(0.95)) if not z.empty else None
        c["null_median_cagr"] = float(z["cagr"].median()) if not z.empty else None
        c["null_pct_adjusted"] = percentile(c["null_pct_sharpe"], w_null) if c["null_pct_sharpe"] is not None else None
    adj_pct = cells[winner]["null_pct_adjusted"]

    x2 = float(spec["costs"]["sensitivity_multiplier"])
    ew, ew_x2 = run_ew(ctx), run_ew(ctx, var_mult=x2)
    free = run_sleeve(ctx, decisions[winner], n, fee_mult=0.0, name=f"{winner}-nofee")
    free_x2 = run_sleeve(ctx, decisions[winner], n, fee_mult=0.0, var_mult=x2, name=f"{winner}-nofee-x2")
    split = ctx.split
    periods = {"before_split": (None, split), "from_split": (split, None)}
    sub_free = {p: metrics(slice_sim(free, a, b)) for p, (a, b) in periods.items()}
    sub_pool = {p: metrics(slice_sim(ew, a, b)) for p, (a, b) in periods.items()}

    halt_dd = float(spec["whole_book"]["halt_at"]) - 1.0
    beta = sleeve_beta(ctx, sims[winner])
    books: dict[str, SimResult] = {}
    options: dict[str, dict[str, Any]] = {}
    for option in spec["overlay"]["options"]:
        b = run_whole_book(ctx, decisions[winner], n, option)
        b2 = run_whole_book(ctx, decisions[winner], n, option, var_mult=x2, fee_mult=x2)
        st = run_stress_book(ctx, option, beta, n)
        books[f"rebased_{option}"] = b
        options[option] = {**metrics(b), "max_drawdown_x2": metrics(b2)["max_drawdown"],
                           "stress": stress_drawdowns(ctx, st)}
    overlay, book_ok = choose_overlay(options, halt_dd)
    index_book = run_index_book(ctx, overlay)
    gate = evaluate_gate(
        g1={"base": (metrics(free), metrics(ew)), "x2": (metrics(free_x2), metrics(ew_x2))}, adj_pct=adj_pct,
        sub_sel=sub_free, sub_pool=sub_pool, book_ok=book_ok, book=metrics(books[f"rebased_{overlay}"]),
        index_book=metrics(index_book), spec=spec)
    return {"cells": cells, "sims": sims, "decisions": decisions, "kept_by": kept_by, "winner": winner, "why": why,
            "n": n, "variant": variant, "null": null, "w_null": w_null, "adj_pct": adj_pct, "ew": ew, "ew_x2": ew_x2,
            "free": free, "free_x2": free_x2, "sub_free": sub_free, "sub_pool": sub_pool, "beta": beta,
            "books": books, "options": options, "overlay": overlay, "book_ok": book_ok, "index_book": index_book,
            "gate": gate, "halt_dd": halt_dd}


def run_study(ctx: Context, out: Path, *, tag_commit: str | None, verbose: bool = True,
              extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = ctx.spec
    paths.assert_outside_repo(out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    st = stage_gate(ctx, verbose=verbose)
    winner, n, variant, overlay = st["winner"], st["n"], st["variant"], st["overlay"]
    decisions, sims, cells = st["decisions"], st["sims"], st["cells"]
    gate_values = {"G2_adjusted_percentile": st["adj_pct"], "G2_own_percentile": cells[winner]["null_pct_sharpe"]}
    # Written before the verdict is shown: a real run folder holding gate.json counts as completed, so a
    # crash or an interrupt after this point cannot buy a second run.
    (out / "gate.json").write_text(json.dumps({"gate": st["gate"], "selected_cell": winner, "overlay": overlay,
                                               "values": gate_values}, indent=2, default=_json_default))
    if verbose:
        print(f"selection: {winner} ({st['why']})")
        print(f"gate: {'ADOPT' if st['gate']['adopt'] else 'NOT ADOPTED'} {st['gate']['checks']} "
              f"[{time.time() - t0:.0f}s]")
    x2 = float(spec["costs"]["sensitivity_multiplier"])
    warn_dd = float(spec["whole_book"]["warn_at"]) - 1.0
    halt_dd = st["halt_dd"]

    # whole books, reported
    books: dict[str, SimResult] = {"current_reference": run_current_reference(ctx), **st["books"],
                                   f"index_book_{overlay}": st["index_book"]}
    book_m = {k: metrics(v) for k, v in books.items()}
    for m in book_m.values():
        m["halt_headroom"] = m["max_drawdown"] - halt_dd if m.get("max_drawdown") is not None else None
    drills = {k: soft_kill_drill(v, warn_at=float(spec["whole_book"]["warn_at"]),
                                 halt_at=float(spec["whole_book"]["halt_at"])) for k, v in books.items()}
    controls = run_controls(ctx)
    idx = {k: metrics(v) for k, v in controls.items()}
    reported = {"G4_index": {"sleeve_sharpe": cells[winner]["sharpe"], **{k: v["sharpe"] for k, v in idx.items()},
                             "pass": all(cells[winner]["sharpe"] is not None and v["sharpe"] is not None
                                         and cells[winner]["sharpe"] >= v["sharpe"] for v in idx.values())},
                "G1_net_of_fee": {"sleeve": {k: cells[winner][k] for k in ("cagr", "sharpe")},
                                  "pool": {k: metrics(st["ew"])[k] for k in ("cagr", "sharpe")}}}

    # sensitivities (reported, never used for selection or the gate), exactly the listed ones
    sens: dict[str, dict[str, Any]] = {}
    min_peer = int(spec["rule"]["min_peer_group"])
    buffer_mult = float(spec["hold_buffer_multiple"])
    for name in spec["sensitivities"]:
        if name == "costs_x2":
            sens[name] = metrics(run_sleeve(ctx, decisions[winner], n, var_mult=x2, fee_mult=x2, name=f"{winner}-costx2"))
        elif name == "no_hold_buffer":
            dec_nb, _ = decisions_for(ctx, variant, n, buffer_mult=1.0)
            sens[name] = metrics(run_sleeve(ctx, dec_nb, n, name=f"{winner}-nobuffer"))
        elif name == "lab_resolver":
            if ctx.inp.fundamentals_lab is None:
                raise SystemExit("the lab-resolver rows are missing from the bundle")
            uni_lab = Universe(ctx.inp, spec, source="lab")
            elig_lab = {d: add_scores(uni_lab.at(d)[0], min_peer) for d in ctx.dates}
            dec_lab, _ = decisions_for(ctx, variant, n, buffer_mult=buffer_mult, eligible=elig_lab)
            sens[name] = metrics(run_sleeve(ctx, dec_lab, n, name=f"{winner}-labresolver"))
        elif name == "ai_list":
            if ctx.inp.ai is None or ctx.inp.ai.empty:
                raise SystemExit("the AI-list inputs are missing from the bundle")
            uni = Universe(ctx.inp, spec)
            elig_ai = {d: add_scores(uni.at(d, include_ai=True)[0], min_peer) for d in ctx.dates}
            dec_ai, _ = decisions_for(ctx, variant, n, buffer_mult=buffer_mult, eligible=elig_ai)
            sens[name] = metrics(run_sleeve(ctx, dec_ai, n, name=f"{winner}-ai"))
        elif name == "overlay_standalone":
            sens[name] = ({"note": "the chosen overlay is none"} if overlay == "none" else metrics(
                run_sleeve(ctx, decisions[winner], n, level=overlay_level(ctx, overlay), name=f"{winner}-{overlay}")))
        elif name in ("zero_fixed_fee", "nav_x5"):
            fee_mult, nav_mult = (0.0, 1.0) if name == "zero_fixed_fee" else (1.0, 5.0)
            sim = st["free"] if name == "zero_fixed_fee" else run_sleeve(ctx, decisions[winner], n, fee_mult=1.0 / nav_mult,
                                                                          name=f"{winner}-navx5")
            z = run_null(ctx, {winner: (variant, n)}, st["kept_by"], fee_mult=fee_mult, nav_mult=nav_mult,
                         verbose=verbose, label=name)
            m = metrics(sim)
            sens[name] = {**m, "null_pct_sharpe": percentile(m["sharpe"], list(z["sharpe"])),
                          "null_median_sharpe": float(z["sharpe"].median())}
        elif name == "stock_stops":
            sim, hits = run_sleeve_stops(ctx, decisions[winner], n, name=f"{winner}-stops")
            m = metrics(sim)
            sens[name] = {**m, "stop_hits_per_year": hits / m["years"] if m.get("years") else None}
        elif name == "execution_lag":
            lag = int(spec["execution_lag_sessions"])
            sens[name] = metrics(run_sleeve(ctx, decisions[winner], n, lag=lag, name=f"{winner}-lag{lag}"))
        elif name == "index_sleeve_spy_only":
            spy = spec["overlay"]["signal"]
            sens[name] = metrics(run_index_book(ctx, overlay, mix={spy: 1.0}, name=f"index_book_{overlay}_spy"))

    # diagnostics (reported)
    diag: dict[str, Any] = {}
    for name in spec["diagnostics"]:
        if name == "gc_overlap":
            out_ov = {}
            for nn in spec["n_names"]:
                sc, gc = decisions.get(f"SC-{nn}"), decisions.get(f"GC-{nn}")
                if sc and gc:
                    shares = [len(set(sc[d]) & set(gc[d])) / max(1, len(sc[d])) for d in ctx.dates]
                    out_ov[f"N={nn}"] = {"mean": float(np.mean(shares)), "min": float(np.min(shares)),
                                         "per_date": {str(d.date()): v for d, v in zip(ctx.dates, shares, strict=True)}}
            diag[name] = out_ov
        elif name == "concentration":
            con = concentration(sims[winner], ctx.returns_for(sims[winner].weights.columns))
            z = st["null"][st["null"]["cell"] == winner]
            symbol_of = {k: str(e.at[k, "symbol"]) for d in ctx.dates for e in [ctx.eligible[d]] for k in e.index}
            top_sym = symbol_of.get(con["top"]) if con["top"] is not None else None
            diag[name] = {"top_symbol": top_sym, "top_share": con["top_share"], "sharpe_ex_top": con["sharpe_ex_top"],
                          "contributions": {symbol_of.get(k, k): float(v) for k, v in con.get("contributions", {}).items()},
                          "null_median_sharpe_ex_top": float(z["sharpe_ex_top"].median()) if "sharpe_ex_top" in z else None,
                          "null_pct_sharpe_ex_top": percentile(con["sharpe_ex_top"], list(z.get("sharpe_ex_top", [])))}
        elif name == "fee_arithmetic":
            m = cells[winner]
            per_leg = ctx.fee_usd / (ctx.real_nav * float(spec["sleeve"]["share_of_nav"]))
            diag[name] = {"legs_per_year": m["legs_per_year"], "fee_per_leg_share_of_sleeve": per_leg,
                          "expected_fee_drag": (m["legs_per_year"] or 0.0) * per_leg,
                          "measured_fee_drag": m["fee_drag_per_year"]}
        elif name == "fee_feasibility":
            per_name = ctx.real_nav * float(spec["sleeve"]["share_of_nav"]) / n
            raw = ctx.inp.raw_close
            above = total = 0
            for d in ctx.dates:
                for k in decisions[winner][d]:
                    if raw is None or k not in raw.columns:
                        continue
                    px = raw[k].loc[:d].dropna()
                    if px.empty:
                        continue
                    total += 1
                    above += int(float(px.iloc[-1]) > per_name)
            diag[name] = {"name_quarters": total, "share_priced_above_one_name": above / total if total else None}
        elif name == "survivorship":
            uni = Universe(ctx.inp, spec)
            res = {}
            for index, etf in spec["data"]["survivorship"].items():
                mem = {d: uni.mapped_members(d, index) for d in ctx.dates}
                ew_m = metrics(run_ew(ctx, members=mem, cost=False, name=f"ew_mapped_{index}"))
                rets = aligned_returns({etf: ctx.control_closes[etf]}, ctx.calendar).reindex(ctx.rows)
                bh = metrics(simulate(fixed_mix_plan(rets, {etf: 1.0}, start=ctx.rows[0], name=etf, rebalance="never"),
                                      rets, flat_costs([etf], 0.0, 0.0, "etf")))
                res[index] = {"etf": etf, "panel_cagr": ew_m["cagr"], "etf_cagr": bh["cagr"],
                              "panel_sharpe": ew_m["sharpe"], "etf_sharpe": bh["sharpe"],
                              "cagr_gap": None if ew_m["cagr"] is None or bh["cagr"] is None else ew_m["cagr"] - bh["cagr"]}
            diag[name] = res
        elif name == "r8_realised_vol":
            cap = float(ctx.policy.risk["ex_ante_vol_hard"])
            diag[name] = {k: float((v.returns.rolling(63).std() * math.sqrt(252) > cap).mean())
                          for k, v in books.items() if k in ("current_reference", f"rebased_{overlay}")}

    split = ctx.split
    periods = {"before_split": (None, split), "from_split": (split, None)}
    named: dict[str, SimResult] = {winner: sims[winner], f"{winner} (no fixed fee)": st["free"], "ew_universe": st["ew"],
                                   **controls, **{f"book_{k}": v for k, v in books.items()}}
    sub = {name: {p: metrics(slice_sim(s, a, b)) for p, (a, b) in periods.items()} for name, s in named.items()}
    result = {
        "study": "stock-sleeve", "synthetic": ctx.synthetic, "tag_commit": tag_commit,
        "policy_sha256": ctx.policy.sha256,
        "spec_sha256": hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest(),
        "inputs_manifest": ctx.inp.manifest,
        "market_sha256": market_hashes(ctx.market),
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {"first_decision": str(ctx.dates[0].date()), "end": str(ctx.rows[-1].date()),
                   "rebalances": len(ctx.dates), "split": str(split.date())},
        "selected_cell": winner, "selection_reason": st["why"], "overlay": overlay, "sleeve_beta": st["beta"],
        "cells": cells, "ew_universe": metrics(st["ew"]), "ew_universe_costx2": metrics(st["ew_x2"]),
        "selected_no_fee": metrics(st["free"]), "selected_no_fee_costx2": metrics(st["free_x2"]),
        "controls": idx, "whole_book": book_m, "overlay_options": st["options"], "halt_drawdown": halt_dd,
        "warn_drawdown": warn_dd,
        "kill_drills": {k: [{**e, "date": str(e["date"])} for e in v] for k, v in drills.items()},
        "subperiods": sub, "sensitivities": sens, "diagnostics": diag, "reported": reported, "gate": st["gate"],
        "gate_values": gate_values,
        "per_year": {name: per_year(s) for name, s in named.items()},
        "eligible_counts": {str(d.date()): int(len(ctx.eligible[d])) for d in ctx.dates},
        **(dict(extra) if extra else {}),
    }
    sel_rows = [{"cell": cell, "date": d.date(), "kept": st["kept_by"][cell][d],
                 "names": " ".join(str(ctx.eligible[d].at[k, "symbol"]) for k in decisions[cell][d])}
                for cell in decisions for d in ctx.dates]
    _write_outputs(ctx, out, result, named, pd.DataFrame(sel_rows), st["null"])
    if verbose:
        print(f"done [{time.time() - t0:.0f}s]")
    return result


def _write_outputs(ctx: Context, out: Path, result: dict[str, Any], named: Mapping[str, SimResult],
                   selections: pd.DataFrame, null: pd.DataFrame) -> None:
    """Everything after gate.json (written by `run_study` as soon as the gate is known)."""
    pd.DataFrame({k: v.nav for k, v in named.items()}).to_csv(out / "nav.csv")
    selections.to_csv(out / "selections.csv", index=False)
    null.to_csv(out / "random_null.csv", index=False)
    pd.DataFrame(result["cells"]).T.to_csv(out / "cells.csv")
    pd.DataFrame(ctx.funnels).T.to_csv(out / "eligibility_funnel.csv")
    pd.DataFrame(result["per_year"]).to_csv(out / "per_year.csv")
    text = render_summary(result)
    assert_public_safe(text)
    (out / "summary.md").write_text(text)
    (out / "result.json").write_text(json.dumps(result, indent=2, default=_json_default))   # last: marks completion


def _json_default(x: Any) -> Any:
    if isinstance(x, (pd.Timestamp, date)):
        return str(x)
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, pd.Series):
        return {str(k): float(v) for k, v in x.items()}
    raise TypeError(type(x).__name__)


def _p(x: Any, digits: int = 1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{100 * x:.{digits}f}%"


def _r(x: Any) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _n(x: Any) -> str:
    return "n/a" if x is None else f"{x:.0f}"


def render_summary(res: Mapping[str, Any]) -> str:
    """Percent-only summary for the OPERATOR ONLY. It passes `assert_public_safe` (no amounts), but it
    is not publishable as it stands: a book's fee drag divided by its legs a year is the fee over the
    sealed funding, so the fee drag and the leg count together disclose the funding (spec section 7).
    A public write-up is a separate document that never shows both for the same book."""
    g = res["gate"]
    gv = res["gate_values"]
    sel = res["selected_cell"]
    c = res["cells"][sel]
    free, pool = res["selected_no_fee"], res["ew_universe"]
    ob = res["whole_book"].get(f"rebased_{res['overlay']}", {})
    ib = res["whole_book"].get(f"index_book_{res['overlay']}", {})
    values = {
        "G1_pool": f"no-fee sleeve {_p(free['cagr'])} / {_r(free['sharpe'])} vs pool {_p(pool['cagr'])} / "
                   f"{_r(pool['sharpe'])}; doubled variable costs {_p(res['selected_no_fee_costx2']['cagr'])} / "
                   f"{_r(res['selected_no_fee_costx2']['sharpe'])} vs {_p(res['ew_universe_costx2']['cagr'])} / "
                   f"{_r(res['ew_universe_costx2']['sharpe'])}",
        "G2_random": f"adjusted percentile {_p(gv['G2_adjusted_percentile'], 0)} (own null {_p(gv['G2_own_percentile'], 0)})",
        "G3_regimes": " ; ".join(f"{p}: {_r(res['subperiods'][f'{sel} (no fixed fee)'][p].get('sharpe'))} vs "
                                 f"{_r(res['subperiods']['ew_universe'][p].get('sharpe'))}" for p in ("before_split", "from_split")),
        "B1_book": f"overlay {res['overlay']}: worst drawdown {_p(worst_drawdown(res['overlay_options'][res['overlay']]))}",
        "B2_index_sleeve": f"Sharpe {_r(ob.get('sharpe'))} vs {_r(ib.get('sharpe'))}; max DD {_p(ob.get('max_drawdown'))} "
                           f"vs {_p(ib.get('max_drawdown'))}",
    }
    lines = [
        "# Stock-sleeve study (pre-registered)" + (" — SYNTHETIC FIXTURE" if res["synthetic"] else ""), "",
        f"> **{LABEL}** The spec and the selection rule were committed and tagged `stock-sleeve-spec` "
        "before this run.", "",
        f"- Window: {res['window']['first_decision']} to {res['window']['end']}, "
        f"{res['window']['rebalances']} rebalances; sub-periods split at {res['window']['split']}.",
        f"- Selected cell: **{sel}** — {res['selection_reason']}.",
        f"- Overlay chosen on the whole book: **{res['overlay']}**.",
        f"- Fee drag of the selected cell: {_p(c['fee_drag_per_year'], 2)} of the sleeve a year "
        f"({_n(c['legs_per_year'])} legs a year).",
        f"- Gate: **{'ADOPT' if g['adopt'] else 'NOT ADOPTED — reported to the user, who decides'}**.", "",
        "| Check | Result | Values |", "|---|---|---|",
        *[f"| {k} | {'pass' if v else 'FAIL'} | {values.get(k, '')} |" for k, v in g["checks"].items()], "",
        "## Sleeve cells (stand-alone, no overlay, net of costs)", "",
        "| Cell | Selectable | CAGR | Vol | Sharpe | Max DD | Turnover/yr | Fee drag/yr | Other cost/yr | Legs/yr | Null pct (own) | Null pct (adjusted) | Null median / 95th Sharpe |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cell, m in res["cells"].items():
        mark = " **(selected)**" if cell == sel else ""
        lines.append(f"| {cell}{mark} | {'yes' if m['selectable'] else 'control'} | {_p(m['cagr'])} | {_p(m['ann_vol'])} | "
                     f"{_r(m['sharpe'])} | {_p(m['max_drawdown'])} | {_p(m['turnover_per_year'], 0)} | "
                     f"{_p(m['fee_drag_per_year'], 2)} | {_p(m['variable_cost_drag_per_year'], 2)} | {_n(m['legs_per_year'])} | "
                     f"{_p(m['null_pct_sharpe'], 0)} | {_p(m['null_pct_adjusted'], 0)} | "
                     f"{_r(m['null_median_sharpe'])} / {_r(m['null_p95_sharpe'])} |")
    ctrl = res["controls"]
    lines += ["", "## Baselines and reported comparisons (full window)", "", "| Book | CAGR | Vol | Sharpe | Max DD |",
              "|---|---:|---:|---:|---:|",
              f"| Selected cell without the fixed fee | {_p(free['cagr'])} | {_p(free['ann_vol'])} | {_r(free['sharpe'])} | {_p(free['max_drawdown'])} |",
              f"| Equal-weight eligible pool (no fixed fee) | {_p(pool['cagr'])} | {_p(pool['ann_vol'])} | {_r(pool['sharpe'])} | {_p(pool['max_drawdown'])} |"]
    for k, m in ctrl.items():
        lines.append(f"| {k.upper().replace('_BH', ' buy and hold')} | {_p(m['cagr'])} | {_p(m['ann_vol'])} | {_r(m['sharpe'])} | {_p(m['max_drawdown'])} |")
    lines.append(f"\nReported index comparison (was G4, not gating): {'pass' if res['reported']['G4_index']['pass'] else 'fail'}.")
    lines += ["", "## Sub-periods (net Sharpe / CAGR)", "", "| Book | Before split | From split |", "|---|---:|---:|"]
    for name, per in res["subperiods"].items():
        b, f = per.get("before_split", {}), per.get("from_split", {})
        lines.append(f"| {name} | {_r(b.get('sharpe'))} / {_p(b.get('cagr'))} | {_r(f.get('sharpe'))} / {_p(f.get('cagr'))} |")
    lines += ["", "## Overlay options (re-based book; HALT line at " + _p(res["halt_drawdown"], 0) + ")", "",
              "| Option | CAGR | Sharpe | Max DD | Max DD, doubled costs | " +
              " | ".join(f"Stress {k}" for k in next(iter(res["overlay_options"].values()))["stress"]) + " |",
              "|---|---:|---:|---:|---:|" + "---:|" * len(next(iter(res["overlay_options"].values()))["stress"])]
    for k, m in res["overlay_options"].items():
        lines.append(f"| {k} | {_p(m['cagr'])} | {_r(m['sharpe'])} | {_p(m['max_drawdown'])} | {_p(m['max_drawdown_x2'])} | "
                     + " | ".join(_p(v) for v in m["stress"].values()) + " |")
    lines += ["", "## Whole books", "",
              "| Book | CAGR | Vol | Sharpe | Max DD | Headroom to HALT | Turnover/yr | Cost drag/yr | Fee drag/yr | 12-month windows with a -25% drawdown | WARN episodes | HALT episodes |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, m in res["whole_book"].items():
        events = res["kill_drills"].get(k, [])
        warn = sum(1 for e in events if e["kind"] == "WARN")
        halt = sum(1 for e in events if e["kind"] == "HALT")
        lines.append(f"| {k} | {_p(m['cagr'])} | {_p(m['ann_vol'])} | {_r(m['sharpe'])} | {_p(m['max_drawdown'])} | "
                     f"{_p(m['halt_headroom'])} | {_p(m['turnover_per_year'], 0)} | {_p(m['cost_drag_per_year'], 2)} | "
                     f"{_p(m['fee_drag_per_year'], 2)} | {_p(m['dd25_window_share'])} | {warn} | {halt} |")
    lines += ["", "## Sensitivities (reported only)", "",
              "| Sensitivity | CAGR | Sharpe | Max DD | Cost drag/yr | Fee drag/yr | Null pct | Other |", "|---|---:|---:|---:|---:|---:|---:|---|"]
    for k, m in res["sensitivities"].items():
        if "cagr" in m:
            other = f"stop hits/yr {_r(m['stop_hits_per_year'])}" if "stop_hits_per_year" in m else ""
            lines.append(f"| {k} | {_p(m['cagr'])} | {_r(m['sharpe'])} | {_p(m['max_drawdown'])} | {_p(m['cost_drag_per_year'], 2)} | "
                         f"{_p(m['fee_drag_per_year'], 2)} | {_p(m.get('null_pct_sharpe'), 0)} | {other} |")
        else:
            lines.append(f"| {k} | {m.get('note', 'n/a')} | | | | | | |")
    d = res["diagnostics"]
    lines += ["", "## Diagnostics (reported only)", ""]
    if "gc_overlap" in d:
        lines.append("- Overlap of the SC and GC picks: " + "; ".join(
            f"{k} mean {_p(v['mean'], 0)}, min {_p(v['min'], 0)}" for k, v in d["gc_overlap"].items()) + ".")
    if "concentration" in d:
        cc = d["concentration"]
        lines.append(f"- Concentration: the top contributor ({cc['top_symbol']}) made {_p(cc['top_share'], 0)} of the "
                     f"arithmetic return; Sharpe with its slot as cash {_r(cc['sharpe_ex_top'])} "
                     f"(null median {_r(cc['null_median_sharpe_ex_top'])}, percentile {_p(cc['null_pct_sharpe_ex_top'], 0)}).")
    if "fee_arithmetic" in d:
        fa = d["fee_arithmetic"]
        lines.append(f"- Fee arithmetic: the leg count predicts a fee drag of {_p(fa['expected_fee_drag'], 2)} of the "
                     f"sleeve a year (measured {_p(fa['measured_fee_drag'], 2)}).")
    if "fee_feasibility" in d:
        ff = d["fee_feasibility"]
        lines.append(f"- Fee feasibility: {_p(ff['share_priced_above_one_name'], 0)} of the selected name-quarters had a "
                     "share price above one name's real amount (these need fractional copies).")
    if "survivorship" in d:
        for k, v in d["survivorship"].items():
            lines.append(f"- Panel bias, {k}: equal weight of the mapped members {_p(v['panel_cagr'])} a year vs "
                         f"{v['etf']} {_p(v['etf_cagr'])} (gap {_p(v['cagr_gap'])}).")
    if "r8_realised_vol" in d:
        lines.append("- Share of days with 63-day realised volatility above the R8 line: " + "; ".join(
            f"{k} {_p(v)}" for k, v in d["r8_realised_vol"].items()) + ".")
    years = sorted({y for v in res["per_year"].values() for y in v})
    names = list(res["per_year"])
    lines += ["", "## Calendar-year returns (first and last years partial)", "",
              "| Year | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for y in years:
        lines.append(f"| {y} | " + " | ".join(_p(res["per_year"][nm].get(y)) for nm in names) + " |")
    if not g["adopt"]:
        lines += ["", "## Not adopted: options for the user", "",
                  "1. Adopt the rule sleeve anyway, as a recorded user override of this gate.",
                  "2. Keep the stock sleeve but let the council pick from the ranked shortlist, with no "
                  "claim of mechanical evidence.",
                  "3. Do not add single stocks: keep the equity share in the existing index lines.",
                  "4. If only B1 failed: a smaller sleeve share or an overlay, pre-registered anew.",
                  "5. If G1-G3 pass and only B2 fails on fees: more funding, fewer legs or a slower refresh, "
                  "pre-registered anew."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------ synthetic


def synthetic_inputs(seed: int = 11, *, start: str = "2014-01-01", end: str = "2024-06-28",
                     n_names: int = 60, market: pd.Series | None = None,
                     beta_range: tuple[float, float] = (0.8, 1.2),
                     idio_range: tuple[float, float] = (0.20, 0.35),
                     sics: Sequence[int] | None = None) -> Inputs:
    """A seeded fixture that exercises every filter: a financial, a foreign (IFRS) filer, an ADR,
    a REIT, a missing gross profit, a pre-revenue name, a stale filer, a delisting, a late listing,
    a second share class of one CIK, a renamed member, a holding-company reorganisation (a new CIK
    with no filing yet), an implausible gross margin, index entries and exits, and three AI-list
    names outside the indexes (two in the panel, one priced only outside it). Numbers mean nothing.

    With `market` (daily returns), each name's return is beta x market + idiosyncratic noise with no
    drift of its own (the power study); without it, the original independent walks."""
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, end)
    sics = list(sics or [3674, 7372, 2834, 3841, 5311, 2080, 1311, 4911, 4813, 3711, 3560, 2821, 4512, 7011, 3663])
    rows_m, rows_sym, rows_iss, sec_rows, sic, closes, dvol, fund = [], [], [], {}, {}, {}, {}, []
    taxonomy: dict[int, str] = {}
    n_ai = 3
    ai_closes: dict[str, pd.Series] = {}
    mkt = None if market is None else market.reindex(sessions).fillna(0.0).to_numpy()
    reorg_cik = 9000                                  # security S1012-1 moves to a new holding CIK in 2019
    for i in range(n_names + n_ai):
        cik = 1000 + i
        outside_panel = i == n_names + n_ai - 1       # an AI-list name priced only outside the panel
        sym = f"S{i:02d}"
        sid = f"S{cik}-1"
        s_sic = sics[i % len(sics)]
        if i == 1:
            s_sic = 6021                              # a bank -> Money
        sic[cik] = s_sic
        kind = "common"
        if i == 3:
            kind = "reit"
        if not outside_panel:
            sec_rows[sid] = {"security_type": kind, "is_adr": i == 4, "cik": cik}
        listed = sessions[0] if i != 7 else pd.Timestamp("2019-06-03")
        delist = None if i != 8 else pd.Timestamp("2020-09-30")
        if i == 20:                                   # renamed on 2019-01-02; membership uses the new symbol
            rows_sym.append({"security_id": sid, "symbol": "S20OLD", "data_symbol": "S20OLD", "valid_from": listed,
                             "valid_to": pd.Timestamp("2019-01-01")})
            listed_sym = pd.Timestamp("2019-01-02")
        else:
            listed_sym = listed
        if not outside_panel:
            rows_sym.append({"security_id": sid, "symbol": sym, "data_symbol": sym, "valid_from": listed_sym,
                             "valid_to": delist or sessions[-1]})
            if i == 12:                               # holding-company reorganisation on 2019-04-01
                rows_iss.append({"security_id": sid, "cik": cik, "valid_from": listed,
                                 "valid_to": pd.Timestamp("2019-03-31")})
                rows_iss.append({"security_id": sid, "cik": reorg_cik, "valid_from": pd.Timestamp("2019-04-01"),
                                 "valid_to": sessions[-1]})
                sic[reorg_cik] = s_sic
                taxonomy[reorg_cik] = "us-gaap"
            else:
                rows_iss.append({"security_id": sid, "cik": cik, "valid_from": listed, "valid_to": delist or sessions[-1]})
        opt_in = pd.Timestamp("2010-01-01") if i % 9 else pd.Timestamp("2018-03-01")
        opt_out = pd.Timestamp("2021-06-01") if i % 11 == 5 else pd.NaT
        if i < n_names:
            rows_m.append({"index": "sp500" if i % 3 else "nasdaq100", "symbol": sym, "opt_in": opt_in,
                           "opt_out": opt_out})
        growth = rng.normal(0.02, 0.03)
        idx = sessions[(sessions >= listed) & ((sessions <= delist) if delist is not None else True)]
        if mkt is None:
            vol = rng.uniform(0.2, 0.45) / np.sqrt(252)
            drift = 0.10 / 252 + 0.5 * growth / 63
            r = rng.normal(drift, vol, len(idx))
        else:
            beta = rng.uniform(*beta_range)
            idio = rng.uniform(*idio_range) / np.sqrt(252)
            m_i = mkt[sessions.get_indexer(idx)]
            r = np.log1p(beta * m_i + rng.normal(0.0, idio, len(idx)))    # log returns of the simple returns
        if outside_panel:
            ai_closes["AI:AIX"] = pd.Series(50 * np.exp(np.cumsum(r)), index=idx)
        else:
            closes[sid] = pd.Series(50 * np.exp(np.cumsum(r)), index=idx)
            dvol[sid] = pd.Series(rng.uniform(1e7, 5e8), index=idx)
        taxonomy[cik] = "ifrs-full" if i == 5 else "us-gaap"
        rev = (1e6 if i == 6 else 5e8) * rng.uniform(0.5, 2.0)
        gm, om = rng.uniform(0.3, 0.7), rng.uniform(0.05, 0.3)
        last_q = pd.Timestamp("2019-12-31") if i == 9 else pd.Timestamp(end)
        for pe in pd.date_range("2009-03-31", last_q, freq="QE"):
            g = growth + rng.normal(0, 0.02)
            rev *= 1 + g
            gm = float(np.clip(gm + rng.normal(0, 0.01), 0.05, 0.9))
            om = float(np.clip(om + rng.normal(0, 0.01), -0.2, 0.5))
            q4 = pe.month == 12
            filer = reorg_cik if (i == 12 and pe >= pd.Timestamp("2019-03-31")) else cik
            gp = np.nan if i == 2 else rev * gm
            if i == 13 and pe.year == 2020:
                gp = 1.5 * rev                        # implausible: gross profit above revenue
            fund.append({"ticker": str(filer), "cik": str(filer), "period_end": pe, "fy": pe.year,
                         "fp": "Q4" if q4 else f"Q{pe.quarter}", "form": "10-K" if q4 else "10-Q",
                         "accession": f"{filer}-{pe.date()}", "available_at": pe + pd.Timedelta(days=60 if q4 else 40),
                         "revenue": rev, "gross_profit": gp,
                         "operating_income": rev * om, "net_income": rev * om * 0.8, "cfo": np.nan, "capex": np.nan,
                         "cash": np.nan, "debt_total": np.nan, "shares_outstanding": np.nan, "is_derived_q4": q4})
    # a second share class of CIK 1010 (less liquid) that must be deduplicated
    rows_sym.append({"security_id": "S1010-2", "symbol": "S10B", "data_symbol": "S10B", "valid_from": sessions[0],
                     "valid_to": sessions[-1]})
    rows_iss.append({"security_id": "S1010-2", "cik": 1010, "valid_from": sessions[0], "valid_to": sessions[-1]})
    sec_rows["S1010-2"] = {"security_type": "common", "is_adr": False, "cik": 1010}
    rows_m.append({"index": "sp500", "symbol": "S10B", "opt_in": pd.Timestamp("2010-01-01"), "opt_out": pd.NaT})
    closes["S1010-2"] = closes["S1010-1"] * 0.98
    dvol["S1010-2"] = pd.Series(1e5, index=sessions)
    raw_fund = pd.DataFrame(fund)[pit.FUNDAMENTALS_COLUMNS]
    # the year-ago values are read per SECURITY (a reorganised filer prints its predecessor's quarters)
    group = raw_fund["ticker"].replace({str(reorg_cik): "1012"})
    fundamentals = pit.year_ago_from_rows(raw_fund, by=group)
    changes = pd.DataFrame({"old_symbol": ["S20OLD"], "new_symbol": ["S20"],
                            "event_date": [pd.Timestamp("2019-01-02")]})
    last = n_names + n_ai - 1
    ai = pd.DataFrame([
        {"symbol": "S00", "cik": 1000, "security_id": "S1000-1", "sic": sic[1000]},          # also a member
        *[{"symbol": f"S{i:02d}", "cik": 1000 + i, "security_id": f"S{1000 + i}-1", "sic": sic[1000 + i]}
          for i in range(n_names, last)],
        {"symbol": "AIX", "cik": 1000 + last, "security_id": "AI:AIX", "sic": sic[1000 + last]},
    ])
    close_df = pd.DataFrame(closes).sort_index()
    wiggle = pd.DataFrame(rng.uniform(0.0, 0.02, close_df.shape), index=close_df.index, columns=close_df.columns)
    return Inputs(
        members=pd.DataFrame(rows_m), symbol_changes=changes, symbols=pd.DataFrame(rows_sym),
        issuer_by_date=pd.DataFrame(rows_iss), securities=pd.DataFrame.from_dict(sec_rows, orient="index"), sic=sic,
        closes=close_df, dollar_volume=pd.DataFrame(dvol).sort_index(),
        fundamentals=fundamentals, taxonomy=taxonomy, raw_close=close_df.astype("float32"),
        adj_open=(close_df.shift(1) * (1 + wiggle - 0.01)).astype("float32"),
        adj_low=(close_df * (1 - 2 * wiggle)).astype("float32"),
        ai=ai, ai_closes=pd.DataFrame(ai_closes).sort_index(),
        fundamentals_lab=fundamentals[pit.FUNDAMENTALS_COLUMNS].copy(),
        manifest={"synthetic": True, "seed": seed},
    )


def synthetic_context(spec: dict[str, Any], policy: Policy, *, seed: int, draws: int, n_names: int = 60,
                      market_factor: bool = False, end: str = "2024-06-28", sics: Sequence[int] | None = None,
                      real_nav: float = 10_000.0, verbose: bool = True) -> Context:
    start = pd.Timestamp(spec["window"]["first_decision_anchor"]).date() - timedelta(
        days=int(spec["window"]["core_warmup_days"]))
    market = synthetic_market(spec, policy, main_start=start, end=pd.Timestamp(end).date(), seed=seed)
    factor = None
    if market_factor:
        factor = market.long[spec["overlay"]["signal"]].astype(float).pct_change()
    inp = synthetic_inputs(seed=seed, n_names=n_names, market=factor, end=end, sics=sics)
    return build_context(spec, inp, policy, market=market, real_nav=real_nav,
                         fee_usd=float(policy.costs["fixed_commission_usd"]["real"]), draws=draws, synthetic=True,
                         verbose=verbose)


# ------------------------------------------------------------------------------------ power


# FF12 sector mix of the eligible set on 2026-08-20 (section 15 of the spec), one SIC per sector.
POWER_SICS = ([3674] * 78 + [5311] * 31 + [8711] * 30 + [2834] * 29 + [3560] * 29 + [2080] * 20 + [2821] * 13
              + [4911] * 5 + [3711] * 4 + [1311] * 3)


def inject_alpha(ctx: Context, alpha: float, top_share: float, base: pd.DataFrame) -> None:
    """Add `alpha` a year to the names in the top `top_share` of the sector score at each decision
    date, from the next session to the next decision (the power study's planted edge)."""
    rets = base.copy()
    daily = alpha / 252.0
    for a, d in enumerate(ctx.dates):
        e = ctx.eligible[d]
        if e.empty:
            continue
        k = max(1, int(round(top_share * len(e))))
        top = list(e.sort_values(["s_score", "g_score"], ascending=False).index[:k])
        i0 = int(ctx.calendar.searchsorted(d)) + 1
        i1 = int(ctx.calendar.searchsorted(ctx.dates[a + 1])) + 1 if a + 1 < len(ctx.dates) else len(ctx.calendar)
        rets.iloc[i0:i1, rets.columns.get_indexer(top)] += daily
    ctx._returns = rets


def _power_seed(args: tuple[int, dict[str, Any], list[float], int, float, float]) -> list[dict[str, Any]]:
    seed, spec, alphas, draws, top_share, real_nav = args
    policy = Policy.load()
    p = spec["power"]
    ctx = synthetic_context(spec, policy, seed=seed, draws=draws, n_names=int(p["names"]), market_factor=True,
                            end=str(p["end"]), sics=POWER_SICS, real_nav=real_nav, verbose=False)
    base = ctx.returns_for(sorted({k for e in ctx.eligible.values() for k in e.index})).copy()
    out = []
    for alpha in alphas:
        inject_alpha(ctx, alpha, top_share, base)
        st = stage_gate(ctx, verbose=False)
        out.append({"seed": seed, "alpha": alpha, "selected": st["winner"], "overlay": st["overlay"],
                    "adjusted_percentile": st["adj_pct"], **st["gate"]["checks"], "adopt": st["gate"]["adopt"],
                    "fee_drag": st["cells"][st["winner"]]["fee_drag_per_year"],
                    "legs_per_year": st["cells"][st["winner"]]["legs_per_year"]})
    return out


def power_study(spec: dict[str, Any], *, replications: int, draws: int, workers: int, real_nav: float,
                verbose: bool = True) -> pd.DataFrame:
    """Pass rates of the gate on synthetic worlds with a planted edge (spec section 14)."""
    p = spec["power"]
    alphas = [float(a) for a in p["alphas"]]
    tasks = [(int(p["seed"]) + r, spec, alphas, draws, float(p["top_share"]), real_nav) for r in range(replications)]
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    if workers <= 1:
        for t in tasks:
            rows += _power_seed(t)
            if verbose:
                print(f"  power seed {t[0]} done [{time.time() - t0:.0f}s]")
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, part in enumerate(ex.map(_power_seed, tasks), 1):
                rows += part
                if verbose:
                    print(f"  power {i}/{len(tasks)} seeds [{time.time() - t0:.0f}s]")
    return pd.DataFrame(rows)


def power_table(frame: pd.DataFrame) -> pd.DataFrame:
    g = frame.groupby("alpha")
    sleeve = frame[["G1_pool", "G2_random", "G3_regimes"]].all(axis=1)
    return pd.DataFrame({
        "replications": g.size(),
        "G2_pass": g["G2_random"].mean(),
        "G1_G3_pass": sleeve.groupby(frame["alpha"]).mean(),
        "whole_gate_pass": g["adopt"].mean(),
        "B1_pass": g["B1_book"].mean(),
        "B2_pass": g["B2_index_sleeve"].mean(),
        "median_adjusted_percentile": g["adjusted_percentile"].median(),
        "median_fee_drag": g["fee_drag"].median(),
        "median_legs_per_year": g["legs_per_year"].median(),
    })


# ------------------------------------------------------------------------------------ once only


def state_dir_check() -> None:
    """A real run uses the operator's default private state folder, where the frozen bundle, the sealed
    parameters and every earlier attempt live. An overridden or copied state folder would escape the
    once-only rule, so COUNCIL_STATE_DIR must be unset and HOME must be this account's home."""
    if os.environ.get("COUNCIL_STATE_DIR"):
        raise SystemExit("COUNCIL_STATE_DIR is set: a real run uses the default private state folder only")
    if Path.home().resolve() != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve():
        raise SystemExit("HOME is not this account's home folder: a real run uses the default private state "
                         "folder only")


def completed_runs(results: Path) -> list[str]:
    """Real run folders whose gate is known (gate.json is written before the verdict is shown) or that
    finished (result.json). Any of them means the study has run."""
    if not results.exists():
        return []
    return sorted({p.parent.name for pattern in ("run-*/gate.json", "run-*/result.json")
                   for p in results.glob(pattern)})


@contextmanager
def run_lock(results: Path) -> Iterator[Path]:
    """One real run at a time: results/LOCK is created exclusively (O_EXCL) and removed when the run
    ends, normally or by an exception or an interrupt. A killed process leaves it behind."""
    paths.assert_outside_repo(results)
    results.mkdir(parents=True, exist_ok=True)
    lock = results / "LOCK"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit("another real run holds results/LOCK. If none is running (a killed run leaves the "
                         "file), check that no run folder holds gate.json, then remove results/LOCK") from None
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "at": datetime.now(UTC).isoformat()}))
        yield lock
    finally:
        lock.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------ CLI


def coverage(spec: Mapping[str, Any], inp: Inputs, *, out: Path | None = None, verbose: bool = True) -> pd.DataFrame:
    """Counts only (no returns): the eligibility funnel at every rebalance date, the sector mix and
    the unmapped members with their reason."""
    sessions = inp.closes.index
    start = pd.Timestamp(spec["window"]["first_decision_anchor"])
    dates = rebalance_dates(sessions, spec, start=start, end=sessions.max())
    uni = Universe(inp, spec)
    rows = []
    for d in dates:
        e, f = uni.at(d)
        sectors = e["sector"].value_counts().to_dict() if not e.empty else {}
        unm = uni.unmapped(d)
        rows.append({"date": d.date(), **f, "sectors": len(sectors),
                     "largest_sector": max(sectors.values()) if sectors else 0,
                     "sector_mix": " ".join(f"{k}:{v}" for k, v in sorted(sectors.items())),
                     "unmapped": " ".join(f"{s}:{why}" for s, why in unm)})
        if verbose:
            print(d.date(), " ".join(f"{k}={v}" for k, v in f.items()))
    frame = pd.DataFrame(rows)
    if out is not None:
        paths.assert_outside_repo(out)
        out.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out / "coverage.csv", index=False)
    return frame


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pre-registered stock-sleeve study (see docs/stock-sleeve-spec.md).")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prepare", help="build the point-in-time input bundle from the lab sources")
    pp.add_argument("--no-ai", action="store_true", help="skip the AI-list sensitivity inputs")
    sub.add_parser("coverage", help="eligibility funnel per rebalance date (counts only)")
    ps = sub.add_parser("seal", help="write the private cost parameters and print the commitment")
    ps.add_argument("--real-nav-usd", type=float, required=True)
    pw = sub.add_parser("power", help="the gate's power on the synthetic fixture (no real data)")
    pw.add_argument("--replications", type=int, default=None)
    pw.add_argument("--draws", type=int, default=None)
    pw.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    sub.add_parser("freeze", help="write the input, source and code sha256 into the variants file")
    pr = sub.add_parser("run", help="run the study (real data only after the tag is pushed)")
    pr.add_argument("--synthetic", action="store_true", help="seeded synthetic fixture, no real data")
    pr.add_argument("--draws", type=int, default=None, help="random-null draws (synthetic runs only)")
    pr.add_argument("--seed", type=int, default=11, help="synthetic seed")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    spec = load_spec()
    root = out_root()
    tag = str(spec["tag"])
    if args.cmd == "prepare":
        if tag_exists(tag):
            raise SystemExit(f"tag {tag} exists: the input bundle is frozen and is never rebuilt")
        inp = prepare(spec, include_ai=not args.no_ai)
        inp.save(root / "inputs")
        print(json.dumps(inp.manifest, indent=2, default=str))
        return 0
    if args.cmd == "coverage":
        inp = Inputs.load(root / "inputs")
        frame = coverage(spec, inp, out=root)
        print(f"wrote coverage for {len(frame)} rebalance dates to the private study directory")
        return 0
    if args.cmd == "seal":
        if tag_exists(tag):
            raise SystemExit(f"tag {tag} exists: the sealed parameters are fixed")
        policy = Policy.load()
        digest = seal_params(root / spec["costs"]["private_params_file"], real_nav_usd=args.real_nav_usd,
                             fixed_fee_usd=float(policy.costs["fixed_commission_usd"]["real"]))
        print(f"private_params_sha256: {digest}")
        return 0
    if args.cmd == "power":
        p = spec["power"]
        policy = Policy.load()
        try:                                  # the sealed funding when it is there (fees drive B2)
            real_nav, nav_source = load_sealed_params(spec, policy, root)["real_nav_usd"], "sealed"
        except SystemExit:
            real_nav, nav_source = float(p["fallback_nav_fee_units"]), "fallback"
        real_nav *= float(policy.costs["fixed_commission_usd"]["real"]) if nav_source == "fallback" else 1.0
        print(f"power at the {nav_source} funding")
        frame = power_study(spec, replications=args.replications or int(p["replications"]),
                            draws=args.draws or int(p["draws"]), workers=args.workers, real_nav=real_nav)
        table = power_table(frame)
        out = root / "power"
        paths.assert_outside_repo(out)
        out.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out / "power_runs.csv", index=False)
        table.to_csv(out / "power.csv")
        print(table.to_string(float_format=lambda x: f"{x:.3f}"))
        return 0
    if args.cmd == "freeze":
        print(freeze(spec, root))
        return 0
    policy = Policy.load()
    if args.synthetic:
        draws = args.draws if args.draws is not None else 20
        ctx = synthetic_context(spec, policy, seed=args.seed, draws=draws)
        run_study(ctx, out_root(synthetic=True), tag_commit=None)
        return 0
    if args.draws is not None:
        raise SystemExit("--draws is fixed by the spec for real runs")
    checkout_check()
    state_dir_check()
    commit = frozen_check(tag)
    remote_tag_check(tag, str(spec.get("remote", "origin")))
    inputs_check(spec, root)
    params = load_sealed_params(spec, policy, root)
    results = root / "results"
    with run_lock(results):
        done = completed_runs(results)
        if done:
            raise SystemExit(f"a completed real run exists ({done[0]}): a pre-registered study runs once")
        started = sorted(p.name for p in results.glob("run-*"))
        inp = Inputs.load(root / "inputs")
        start = pd.Timestamp(spec["window"]["first_decision_anchor"]).date() - timedelta(
            days=int(spec["window"]["core_warmup_days"]))
        # Fetched before the run folder exists: a network or token failure is not an attempt.
        market = real_market(spec, policy, main_start=start, end=inp.closes.index.max().date())
        out = results / f"run-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        paths.assert_outside_repo(out)
        out.mkdir(parents=True, exist_ok=False)
        (out / "STARTED").write_text(json.dumps({"tag_commit": commit, "at": datetime.now(UTC).isoformat(),
                                                 "market_sha256": market_hashes(market)}, indent=2))
        ctx = build_context(spec, inp, policy, market=market, real_nav=params["real_nav_usd"],
                            fee_usd=params["fixed_fee_usd"], draws=int(spec["baselines"]["random"]["draws"]),
                            synthetic=False)
        run_study(ctx, out, tag_commit=commit, extra={"incomplete_earlier_runs": started})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
