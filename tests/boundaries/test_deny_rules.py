"""Claude Code deny rules for coding agents (m5-readiness §9.4, gate B8; M5-B).

`ops/claude/deny-rules.json` parses, every rule matches the permission-rule grammar
(`Tool(specifier)`: Bash command prefixes, Read/Edit/Write gitignore-style paths anchored at `~/`,
`//`, `/` or `./`), the §9.4 set is present, no rule carries a local user path (the repo is public),
and the project-level `.claude/settings.json` denies every rule (the user installs them at project
level only; user-level settings are never edited).
"""

from __future__ import annotations

import json
import re

from council.paths import REPO_ROOT

RULES_PATH = REPO_ROOT / "ops" / "claude" / "deny-rules.json"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
RULE = re.compile(r"^(?P<tool>Bash|Read|Edit|Write)\((?P<spec>[^()]+)\)$")
BASH_SPEC = re.compile(r"^[^\s:*][^:*]*?(:\*)?$")
PATH_SPEC = re.compile(r"^(~/|//|/|\./)[^\s*][^\n]*$")
STATE = "~/Library/Application Support/council-book"

REQUIRED = {                                   # the whole §9.4 set
    "Bash(osascript:*)", "Bash(open -a Terminal:*)", "Bash(open -a iTerm:*)", "Bash(open -b com.apple.Terminal:*)",
    *(f"Bash(launchctl {c}:*)" for c in ("submit", "bootstrap", "kickstart", "load", "asuser")),
    *(f"Bash(security {c}:*)" for c in (
        "find-generic-password", "add-generic-password", "delete-generic-password", "unlock-keychain",
        "set-keychain-settings", "list-keychains", "dump-keychain", "export", "-i")),
    *(f"Bash({p}ops/{s}.sh:*)" for p in ("", "./") for s in ("install", "uninstall", "rehearse-onboarding")),
    "Bash(git tag:*)", "Bash(git push origin council-spec:*)", "Bash(git push --tags:*)", "Bash(council-op:*)",
    "Bash(council approve:*)", "Bash(uv run council approve:*)",
    *(f"Read({STATE}/{d}/**)" for d in ("calls", "transcripts", "licensed", "backups")),
    *(f"{t}({STATE}/{d}/**)" for t in ("Edit", "Write") for d in ("readiness", "account", "salts", "releases")),
}


def _rules() -> list[str]:
    data = json.loads(RULES_PATH.read_text())
    assert isinstance(data, dict) and isinstance(data.get("deny"), list)
    return data["deny"]


def test_the_rule_file_parses_and_every_rule_matches_the_grammar():
    rules = _rules()
    assert rules and len(rules) == len(set(rules))
    for rule in rules:
        m = RULE.match(rule)
        assert m, rule
        spec = m.group("spec")
        if m.group("tool") == "Bash":
            assert BASH_SPEC.match(spec) and spec == spec.strip(), rule
        else:
            assert PATH_SPEC.match(spec) and ".." not in spec, rule


def test_the_design_set_is_present_and_carries_no_local_path():
    rules = set(_rules())
    assert rules >= REQUIRED, sorted(REQUIRED - rules)
    assert not any("/Users/" in r or "/home/" in r for r in rules)


def test_the_project_settings_deny_every_rule():
    settings = json.loads(SETTINGS_PATH.read_text())
    deny = settings.get("permissions", {}).get("deny", [])
    assert set(_rules()) == set(deny), sorted(set(_rules()) ^ set(deny))   # one source, no drift
    assert not settings.get("permissions", {}).get("allow")           # no allow rule rides along


def test_every_operator_command_is_denied_to_agents_at_project_level():
    """Every §9.1 operator command (the readiness registry) is refused to a coding agent's Bash tool,
    bare and through `uv run`, by a prefix rule in the project settings (the code guard stays the
    boundary; this narrows the accident surface)."""
    from council.operator.readiness import REQUIRED_OPERATOR

    deny = json.loads(SETTINGS_PATH.read_text())["permissions"]["deny"]
    prefixes = [m.group("spec").removesuffix(":*") for r in deny
                if (m := RULE.match(r)) and m.group("tool") == "Bash"]
    missing = []
    for command in REQUIRED_OPERATOR:
        for pre in ("council ", "uv run council "):
            line = pre + command
            if not any(line == p or line.startswith(p + " ") for p in prefixes):
                missing.append(line)
    assert not missing, missing
