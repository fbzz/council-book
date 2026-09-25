from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel, ValidationError

from council.publish import leakscan
from council.publish.public_models import PUBLIC_MODELS, PublicCommitment, PublicStatus


def _models(model: type[BaseModel], seen: set) -> set[type[BaseModel]]:
    if model in seen:
        return seen
    seen.add(model)
    for field in model.model_fields.values():
        stack = [field.annotation]
        while stack:
            tp = stack.pop()
            if isinstance(tp, type) and issubclass(tp, BaseModel):
                _models(tp, seen)
            stack.extend(typing.get_args(tp))
    return seen


ALL = sorted(set().union(*(_models(m, set()) for m in PUBLIC_MODELS)), key=lambda m: m.__name__)


@pytest.mark.parametrize("model", ALL, ids=lambda m: m.__name__)
def test_no_public_field_name_is_on_the_denylist(model):
    assert [f for f in model.model_fields if leakscan.key_denied(f)] == []


@pytest.mark.parametrize("model", ALL, ids=lambda m: m.__name__)
def test_every_public_model_forbids_extra_fields_and_is_frozen(model):
    assert model.model_config.get("extra") == "forbid" and model.model_config.get("frozen") is True


def test_status_defaults_to_awaiting_account():
    assert PublicStatus().state == "AWAITING_ACCOUNT"
    with pytest.raises(ValidationError):
        PublicStatus(state="TRADING")


def test_commitment_requires_a_real_digest_and_aware_time():
    from datetime import datetime

    with pytest.raises(ValidationError):
        PublicCommitment(cycle_id="2026-10-01T1440Z", commitment_sha256="xyz", sealed_at=datetime(2026, 10, 1))
