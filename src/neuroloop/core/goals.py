"""Goal — objetivo raiz de um run (spec §10, correção C02)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.criteria import Criterion, effective_observes
from neuroloop.core.enums import ErrorCode, GoalStatus


class Constraint(BaseModel):
    """Restrição sobre como o goal pode ser atingido.

    Diferente de `failure_criteria`: restrição limita o caminho, critério de
    falha descreve um estado terminal indesejado.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1)
    criterion: Criterion | None = None


class Goal(BaseModel):
    """V0: um root goal por run (spec §10).

    Invariante de C02: pelo menos um `success_criteria` deve observar
    ``EXTERNAL_STATE``. Um goal verificável apenas pelo auto-relato da
    ferramenta não é verificável — é exatamente o falso sucesso que o
    sistema existe para eliminar.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    agent_id: UUID
    description: str = Field(min_length=1)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    deadline: datetime | None = None
    success_criteria: tuple[Criterion, ...] = Field(min_length=1)
    failure_criteria: tuple[Criterion, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    status: GoalStatus = GoalStatus.PENDING
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _require_external_evidence(self) -> Goal:
        if not any(effective_observes(c) == "EXTERNAL_STATE" for c in self.success_criteria):
            raise ValueError(
                f"{ErrorCode.INVALID_GOAL_CRITERIA.value}: pelo menos um success_criteria "
                "precisa observar EXTERNAL_STATE; auto-relato de tool não conclui goal"
            )
        return self

    @property
    def external_success_criteria(self) -> tuple[Criterion, ...]:
        """Subconjunto elegível para goal verification (C02)."""
        return tuple(
            c for c in self.success_criteria if effective_observes(c) == "EXTERNAL_STATE"
        )
