"""Estado do run: budget imutável e checkpoint persistente (correção C03).

A spec original lia ``run.max_iterations``, ``run.max_replans`` e
``run.cancel_requested`` no loop sem que nenhum deles existisse no schema.
Aqui `ExecutionBudget` é embutido e imutável, e os contadores que o loop
consulta são campos de primeira classe do checkpoint.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.criteria import CriterionOutcome
from neuroloop.core.enums import RunPhase


class ExecutionBudget(BaseModel):
    """Limites do run. Imutável após a criação (spec §22, §24)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=30, ge=1)
    max_replans: int = Field(default=3, ge=0)
    """C07: conta apenas substituições; o plano inicial não é replan."""
    max_retries_per_action: int = Field(default=2, ge=0)
    token_budget: int = Field(default=100_000, ge=1)
    cost_budget_usd: Decimal | None = Field(default=None, gt=0)
    wall_clock_seconds: int = Field(default=900, ge=1)


class RunCheckpoint(BaseModel):
    """Estado persistente do run. Fonte canônica.

    `WorkingContext` (TASK-009) é derivado e reconstruído a cada ciclo;
    nunca substitui este schema.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    goal_id: UUID
    phase: RunPhase
    iteration: int = Field(default=0, ge=0)

    active_plan_id: UUID | None = None
    active_plan_version: int | None = Field(default=None, ge=1)
    current_step_id: str | None = None

    replan_count: int = Field(default=0, ge=0)
    plan_generation_count: int = Field(default=0, ge=0)
    retry_counts: dict[UUID, int] = Field(default_factory=dict)
    """C04: retry é contado por logical_action_id, não por run."""

    waiting_reason: str | None = None
    pending_approval_action_id: UUID | None = None
    pending_approval_fingerprint: str | None = None
    """C19: aprovação vale para argumentos específicos, não para a ação lógica."""

    last_action_id: UUID | None = None
    last_verified_action_id: UUID | None = None
    unresolved_effect_action_id: UUID | None = None
    """C05: se preenchido, o próximo ciclo entra em RECOVERING antes de decidir."""

    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    tokens_used: int = Field(default=0, ge=0)
    cost_used_usd: Decimal = Field(default=Decimal("0"), ge=0)

    baseline_outcomes: tuple[CriterionOutcome, ...] = ()
    """C02: estado dos success_criteria antes de qualquer ação do run."""

    cancel_requested: bool = False

    started_at: datetime
    wall_clock_deadline: datetime
    state_version: int = Field(default=0, ge=0)
    lease_epoch: int = Field(default=0, ge=0)
    """C11: fencing token. Escrita com epoch desatualizado é STATE_CONFLICT."""

    @model_validator(mode="after")
    def _check_counters(self) -> RunCheckpoint:
        if self.plan_generation_count < self.replan_count:
            raise ValueError(
                "plan_generation_count não pode ser menor que replan_count"
            )
        if self.active_plan_id is None and self.active_plan_version is not None:
            raise ValueError("active_plan_version exige active_plan_id")
        if self.pending_approval_fingerprint and not self.pending_approval_action_id:
            raise ValueError(
                "pending_approval_fingerprint exige pending_approval_action_id"
            )
        return self

    @property
    def pre_satisfied(self) -> bool:
        """True se o goal já estava satisfeito antes do run começar (C02).

        Nesse caso a conclusão exige confirmação humana — declarar
        COMPLETED sem ter agido é falso sucesso.
        """
        return bool(self.baseline_outcomes) and all(
            o.satisfied is True for o in self.baseline_outcomes
        )

    def budget_exhausted(self, now: datetime) -> bool:
        if self.iteration >= self.budget.max_iterations:
            return True
        if self.tokens_used >= self.budget.token_budget:
            return True
        if (
            self.budget.cost_budget_usd is not None
            and self.cost_used_usd >= self.budget.cost_budget_usd
        ):
            return True
        return now >= self.wall_clock_deadline

    def cost_pressure(self, now: datetime) -> float:
        """Spec §10. Máximo entre as três pressões, saturado em 1.0."""
        elapsed = (now - self.started_at).total_seconds()
        window = max((self.wall_clock_deadline - self.started_at).total_seconds(), 1e-9)
        pressures = [
            self.tokens_used / self.budget.token_budget,
            elapsed / window,
            self.iteration / self.budget.max_iterations,
        ]
        if self.budget.cost_budget_usd is not None:
            pressures.append(float(self.cost_used_usd / self.budget.cost_budget_usd))
        return min(max(pressures), 1.0)
