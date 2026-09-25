"""Prompt registry: IDs, hashing, manifest, strict rendering."""

from __future__ import annotations

import json
import re
import shutil

import jinja2
import pytest

from council.deliberation.common import prompt_context
from council.llm.prompts import PromptError, PromptRegistry
from council.paths import PROMPTS_DIR

ROLES = ["bear", "bull_open", "bull_rebuttal", "macro", "news", "pm", "single_agent"]


def test_registry_lists_roles_and_ids(reg):
    assert reg.roles() == ROLES
    assert reg.names() == ["_desk_brief", *ROLES]
    for role in ROLES:
        assert reg.prompt_id(role) == f"council-{role}/v1"
    assert reg.manifest()["_desk_brief"]["id"] == "council-desk_brief/v1"


def test_sha_is_over_file_bytes_and_includes(tmp_path):
    work = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, work)
    base = PromptRegistry(work)
    before = {r: base.sha256(r) for r in base.names()}
    assert all(re.fullmatch(r"[0-9a-f]{64}", s) for s in before.values())

    (work / "pm.md").write_text((work / "pm.md").read_text() + "\nextra line\n")
    after_pm = PromptRegistry(work)
    assert after_pm.sha256("pm") != before["pm"]
    assert after_pm.sha256("bear") == before["bear"]

    (work / "_desk_brief.md").write_text((work / "_desk_brief.md").read_text() + "\nmore\n")
    after_brief = PromptRegistry(work)
    for role in ROLES:  # every role includes the brief, so every role hash moves
        assert after_brief.sha256(role) != before[role]
    assert after_brief.manifest_sha() != base.manifest_sha()


def test_header_must_match_file_name(tmp_path):
    (tmp_path / "pm.md").write_text("Prompt ID: council-bear/v1\nhello\n")
    with pytest.raises(PromptError):
        PromptRegistry(tmp_path)
    (tmp_path / "pm.md").write_text("no header\n")
    with pytest.raises(PromptError):
        PromptRegistry(tmp_path)


def test_render_is_strict_about_missing_variables(reg):
    with pytest.raises(jinja2.UndefinedError):
        reg.render("pm")
    with pytest.raises(PromptError):
        reg.render("nope")


def test_render_all_roles(reg, policy):
    ctx = prompt_context(policy)
    for role in ROLES:
        text = reg.render(role, **ctx)
        assert "Prompt ID:" not in text                  # header stripped, also from the include
        assert "THE BOOK" in text and "ONE JSON object" in text   # the shared brief is included
        assert "{%" not in text and "{{" not in text
        assert "JSON FIELDS" in text
    pm = reg.render("pm", **ctx)
    assert "At most 3 deviations" in pm or "at most 3 deviations" in pm
    assert "NO DEVIATION IS A VALID ANSWER" in pm and "Never split the difference" in pm
    assert '"sided_with"' in pm


def test_prompt_numbers_come_from_policy(reg, policy):
    risk = {**policy.risk, "authority": {**policy.risk["authority"], "max_deviations_per_cycle": 2}}
    ctx = prompt_context(policy.model_copy(update={"risk": risk}))
    assert "0 to 2 items" in reg.render("pm", **ctx)
    text = reg.render("pm", **prompt_context(policy))
    assert "20% below its lifetime peak" in text and "HALT at 25% below" in text


def test_examples_in_prompts_are_parseable_json(reg, policy):
    """Every EXAMPLE line must be a JSON object, so the model copies a valid shape."""
    ctx = prompt_context(policy)
    for role in ROLES:
        lines = reg.render(role, **ctx).splitlines()
        examples = [lines[i + 1] for i, ln in enumerate(lines) if ln.startswith("EXAMPLE")]
        assert examples, role
        for ex in examples:
            assert isinstance(json.loads(ex), dict), role


def test_examples_satisfy_the_schemas(reg, policy):
    from council.models.cards import MacroAnalystOutput, NewsAnalystOutput
    from council.models.debate import AdvocateCase, BearCase
    from council.models.pm import PMDecision

    schemas = {"news": NewsAnalystOutput, "macro": MacroAnalystOutput, "bull_open": AdvocateCase,
               "bear": BearCase, "bull_rebuttal": AdvocateCase, "pm": PMDecision,
               "single_agent": PMDecision}
    ctx = prompt_context(policy)
    for role, schema in schemas.items():
        lines = reg.render(role, **ctx).splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("EXAMPLE"):
                schema.model_validate(json.loads(lines[i + 1]), strict=True)


def test_checked_in_manifest_is_current(reg):
    on_disk = (PROMPTS_DIR / "manifest.json").read_text()
    assert on_disk == reg.manifest_json(), "run PromptRegistry().write_manifest() after editing prompts"


def test_write_manifest_is_deterministic(tmp_path, reg):
    a = reg.write_manifest(tmp_path / "a.json").read_bytes()
    b = reg.write_manifest(tmp_path / "b.json").read_bytes()
    assert a == b
    assert set(json.loads(a)) == {"_desk_brief", *ROLES}
    assert len(reg.manifest_sha()) == 64
