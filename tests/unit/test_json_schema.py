"""Critério de aceite da TASK-001: JSON Schema utilizável como structured output."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, TypeAdapter

from neuroloop.core import (
    ActionProposal,
    Criterion,
    Decision,
    Goal,
    Observation,
    Plan,
    PlanStep,
    RunCheckpoint,
    VerificationResult,
)

MODELS: list[type[BaseModel]] = [
    ActionProposal,
    Goal,
    Observation,
    Plan,
    PlanStep,
    RunCheckpoint,
    VerificationResult,
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_modelo_gera_json_schema_serializavel(model: type[BaseModel]):
    schema = model.model_json_schema()
    assert schema["type"] == "object"
    json.dumps(schema)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_modelo_proibe_campos_extras(model: type[BaseModel]):
    """Structured output não pode aceitar campo que o runtime ignoraria."""
    assert model.model_config.get("extra") == "forbid"


def test_uniao_de_decisao_expoe_discriminador():
    schema = TypeAdapter(Decision).json_schema()
    assert "discriminator" in schema
    assert schema["discriminator"]["propertyName"] == "type"


def test_uniao_de_criterio_expoe_discriminador():
    schema = TypeAdapter(Criterion).json_schema()
    assert schema["discriminator"]["propertyName"] == "kind"


def test_roundtrip_preserva_arvore_de_criterios():
    from neuroloop.core import AllOf, AnyOf, FileExists, JsonPathCount

    original = AllOf(
        criteria=(
            FileExists(path="/workspace/eligible.json"),
            AnyOf(
                criteria=(
                    JsonPathCount(
                        source="FILE",
                        path="/workspace/eligible.json",
                        json_path="$[*]",
                        expected_count=3,
                    ),
                )
            ),
        )
    )
    adapter: TypeAdapter[Criterion] = TypeAdapter(Criterion)
    restored = adapter.validate_json(adapter.dump_json(original))
    assert restored == original
