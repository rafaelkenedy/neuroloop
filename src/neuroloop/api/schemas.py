"""Schemas da API — TASK-013."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from neuroloop.core.criteria import Criterion
from neuroloop.core.enums import ErrorCode, RunPhase


class CreateGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    success_criteria: tuple[Criterion, ...] = Field(min_length=1)
    failure_criteria: tuple[Criterion, ...] = ()
    agent_name: str = "neuroloop"
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    deadline: datetime | None = None


class GoalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: UUID
    description: str
    status: str


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int | None = Field(default=None, ge=1)
    token_budget: int | None = Field(default=None, ge=1)
    wall_clock_seconds: int | None = Field(default=None, ge=1)


class RunView(BaseModel):
    """Estado do run para quem está de fora.

    Inclui o que a intervenção humana precisa para agir: a fase, o motivo da
    espera e — quando há aprovação pendente — a ação e o fingerprint aos
    quais a aprovação vai se vincular (C19).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    goal_id: UUID
    phase: RunPhase
    iteration: int
    waiting_reason: str | None = None
    pending_approval_action_id: UUID | None = None
    pending_approval_fingerprint: str | None = None
    tokens_used: int
    cost_used_usd: Decimal
    cancel_requested: bool
    started_at: datetime
    wall_clock_deadline: datetime

    @property
    def awaiting_human(self) -> bool:
        return self.phase is RunPhase.WAITING_USER


class RunResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    phase: RunPhase
    iteration: int
    error_code: ErrorCode | None = None
    waiting_reason: str | None = None
    tokens_used: int = 0
    cost_used_usd: Decimal = Decimal("0")
    deliberations: int = 0
    fast_path_hits: dict[str, int] = Field(default_factory=dict)
    goal_satisfied: bool = False


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = None


class ApproveRequest(BaseModel):
    """Aprovação vinculada a argumentos exatos (correção C19).

    O cliente devolve o `action_id` e o `fingerprint` que recebeu em
    `RunView`. Se qualquer um mudou desde o pedido, a aprovação não vale —
    aprovar `a.json` não pode autorizar `b.json`.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: UUID
    fingerprint: str
    resume: bool = True


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    reason: str | None = None
    from_phase: str | None = None
    to_phase: str | None = None
    error_code: str | None = None
    iteration: int
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime


class EpisodeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int
    decision_type: str
    tool_name: str | None = None
    result_summary: str
    error_code: str | None = None
    importance: float
    reward: float
    tags: tuple[str, ...] = ()
