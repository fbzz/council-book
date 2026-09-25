"""Fixtures for the council area (builders live in factories.py)."""

from __future__ import annotations

from datetime import datetime

import pytest

from council.llm.prompts import PromptRegistry
from council.models.facts import FactPack
from council.models.reference import ReferenceBook
from council.models.risk import Band

from .factories import REF_LEVELS, SLOT, build_bands, build_pack, build_ref


@pytest.fixture
def pack() -> FactPack:
    return build_pack()


@pytest.fixture
def ref() -> ReferenceBook:
    return build_ref()


@pytest.fixture
def bands() -> dict[str, Band]:
    return build_bands()


@pytest.fixture
def current() -> dict[str, float]:
    return {**REF_LEVELS, "OIL": -0.25}


@pytest.fixture
def lines(policy):
    return list(policy.universe.lines)


@pytest.fixture
def reg() -> PromptRegistry:
    return PromptRegistry()


@pytest.fixture
def now() -> datetime:
    return SLOT
