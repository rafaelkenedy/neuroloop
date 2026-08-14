"""`WorkingContext` e seu construtor — TASK-009 (spec §9, §20).

O Global Workspace da arquitetura conceitual vira aqui uma **projeção
derivada e limitada** do estado. Duas propriedades definem o que ele é:

- **Nunca é fonte canônica.** É reconstruído a cada ciclo a partir do
  checkpoint, do plano e das observações. Perder o workspace não perde
  estado.
- **Não significa "jogar tudo no prompt"** (spec §27). O que entra é
  decidido por salience e por um orçamento explícito; o que fica de fora
  fica registrado, para que a omissão seja auditável em vez de silenciosa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from neuroloop.context.salience import score_observation
from neuroloop.core.criteria import Criterion
from neuroloop.core.enums import ErrorCode, RiskLevel, TrustLevel
from neuroloop.core.goals import Constraint, Goal
from neuroloop.core.observations import Observation
from neuroloop.core.plans import Plan, PlanStep
from neuroloop.core.runs import RunCheckpoint
from neuroloop.memory.retrieval import EpisodeMemory
from neuroloop.tools.definitions import ToolSummary


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Limites do que cabe no ciclo. Explícitos para poderem ser medidos."""

    max_observations: int = 8
    max_memories: int = 5
    max_errors: int = 3
    max_observation_chars: int = 2000


class GoalView(BaseModel):
    """Projeção do goal. Nunca truncada (spec §20)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    description: str
    success_criteria: tuple[Criterion, ...]
    constraints: tuple[Constraint, ...] = ()
    deadline: datetime | None = None


class BudgetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int
    max_iterations: int
    tokens_used: int
    token_budget: int
    cost_pressure: float = Field(ge=0.0, le=1.0)
    deadline: datetime


class SafetyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_auto_risk: RiskLevel = RiskLevel.R1
    untrusted_observation_ids: tuple[UUID, ...] = ()
    pending_approval_action_id: UUID | None = None

    @property
    def has_untrusted_content(self) -> bool:
        return bool(self.untrusted_observation_ids)


class RecentError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: ErrorCode
    detail: str | None = None
    action_id: UUID | None = None
    at: datetime


class AttentionItem(BaseModel):
    """Por que este item está no contexto. Alimenta o trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    ref: str
    salience: float = Field(ge=0.0, le=1.0)
    trust: TrustLevel | None = None


class WorkingContext(BaseModel):
    """Volátil e derivado. Reconstruído por ciclo, nunca persistido."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: GoalView
    current_plan: Plan | None = None
    current_step: PlanStep | None = None
    observations: tuple[Observation, ...] = ()
    memories: tuple[EpisodeMemory, ...] = ()
    errors: tuple[RecentError, ...] = ()
    available_tools: tuple[ToolSummary, ...] = ()
    budget: BudgetView
    safety: SafetyContext
    attention: tuple[AttentionItem, ...] = ()
    dropped: tuple[str, ...] = ()
    """O que ficou de fora por orçamento. Omissão registrada, não silenciosa."""

    @property
    def has_untrusted_content(self) -> bool:
        return self.safety.has_untrusted_content


@dataclass(slots=True)
class WorkspaceBuilder:
    budget: ContextBudget = field(default_factory=ContextBudget)

    def build(
        self,
        *,
        goal: Goal,
        checkpoint: RunCheckpoint,
        now: datetime,
        plan: Plan | None = None,
        observations: tuple[Observation, ...] = (),
        memories: tuple[EpisodeMemory, ...] = (),
        errors: tuple[RecentError, ...] = (),
        tools: tuple[ToolSummary, ...] = (),
        seen_hashes: frozenset[str] = frozenset(),
    ) -> WorkingContext:
        goal_tags = _goal_tags(goal)
        ranked = sorted(
            (
                (
                    score_observation(
                        obs, goal_tags=goal_tags, now=now, seen_hashes=seen_hashes
                    ),
                    obs,
                )
                for obs in observations
            ),
            key=lambda pair: (-pair[0], pair[1].received_at),
        )

        kept = ranked[: self.budget.max_observations]
        dropped = [
            f"observation:{obs.id} (salience={score:.2f})" for score, obs in ranked[self.budget.max_observations :]
        ]

        kept_memories = memories[: self.budget.max_memories]
        if len(memories) > self.budget.max_memories:
            dropped.extend(
                f"memory:{m.episode_id} (score={m.score:.2f})"
                for m in memories[self.budget.max_memories :]
            )

        kept_errors = errors[: self.budget.max_errors]

        attention = tuple(
            AttentionItem(
                kind="observation",
                ref=str(obs.id),
                salience=score,
                trust=obs.trust,
            )
            for score, obs in kept
        ) + tuple(
            AttentionItem(kind="memory", ref=str(m.episode_id), salience=min(m.score, 1.0))
            for m in kept_memories
        )

        return WorkingContext(
            goal=GoalView(
                id=goal.id,
                description=goal.description,
                success_criteria=goal.success_criteria,
                constraints=goal.constraints,
                deadline=goal.deadline,
            ),
            current_plan=plan,
            current_step=_current_step(plan, checkpoint),
            observations=tuple(obs for _, obs in kept),
            memories=tuple(kept_memories),
            errors=tuple(kept_errors),
            available_tools=tuple(tools),
            budget=BudgetView(
                iteration=checkpoint.iteration,
                max_iterations=checkpoint.budget.max_iterations,
                tokens_used=checkpoint.tokens_used,
                token_budget=checkpoint.budget.token_budget,
                cost_pressure=checkpoint.cost_pressure(now),
                deadline=checkpoint.wall_clock_deadline,
            ),
            safety=SafetyContext(
                untrusted_observation_ids=tuple(
                    obs.id for _, obs in kept if obs.is_untrusted
                ),
                pending_approval_action_id=checkpoint.pending_approval_action_id,
            ),
            attention=attention,
            dropped=tuple(dropped),
        )


def _current_step(plan: Plan | None, checkpoint: RunCheckpoint) -> PlanStep | None:
    if plan is None or checkpoint.current_step_id is None:
        return None
    return next((s for s in plan.steps if s.id == checkpoint.current_step_id), None)


def _goal_tags(goal: Goal) -> frozenset[str]:
    """Tags derivadas dos critérios, não da descrição.

    Usar o texto do goal faria o `goal_relevance` depender de prosa — e
    prosa é justamente o que conteúdo externo consegue imitar.
    """
    tags: set[str] = set()
    for criterion in goal.success_criteria:
        path = getattr(criterion, "path", None)
        if isinstance(path, str):
            tags.add(f"resource:{path}")
        url = getattr(criterion, "url", None)
        if isinstance(url, str):
            tags.add(f"resource:{url}")
    return frozenset(tags)
