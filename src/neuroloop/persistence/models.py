"""Tabelas do MVP — TASK-003, conjunto da correção C15.

A spec original listava 8 tabelas e omitia `observations` (que
`perception.collect_pending` pressupõe) e `skills` (cadastradas manualmente
na V0, mas ainda assim precisam morar em algum lugar).

Decisões:

- `plan_steps` não existe: steps são JSON dentro de `plans`. Na V0 nunca se
  consulta step isoladamente, e o plano inteiro é substituído a cada replan.
- `run_events` é tracing e auditoria, não event sourcing: nada é
  reconstruído a partir dele.
- Campos JSON usam `JSONB` no PostgreSQL e `JSON` nos demais dialetos, para
  que a suíte rode sem um servidor no ambiente de desenvolvimento.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """Datetime sempre tz-aware em UTC, na ida e na volta.

    Sem isso, dialetos que não guardam offset (SQLite) devolvem datetimes
    *naive* e qualquer comparação com `datetime.now(UTC)` explode em runtime
    — foi assim que o walking skeleton quebrou ao comparar
    `wall_clock_deadline`. Normalizar no tipo resolve para todo repositório
    de uma vez, em vez de espalhar `astimezone` pelos call sites.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JsonType,
        list[Any]: JsonType,
    }


def _pk() -> Mapped[UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid4)


def _ts(**kwargs) -> Mapped[datetime]:
    return mapped_column(UtcDateTime(), **kwargs)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[UUID] = _pk()
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[float]
    deadline: Mapped[datetime | None] = _ts(nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)

    success_criteria: Mapped[list[Any]] = mapped_column(JsonType)
    failure_criteria: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    constraints: Mapped[list[Any]] = mapped_column(JsonType, default=list)

    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())

    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 1", name="ck_goals_priority"),
        Index("ix_goals_agent_status", "agent_id", "status"),
    )


class AgentRun(Base):
    """Run + checkpoint na mesma linha.

    Separar checkpoint em tabela própria só faria sentido com histórico de
    checkpoints, que a V0 não usa: o histórico auditável vive em
    `run_events`. Uma linha por run mantém o optimistic locking trivial.
    """

    __tablename__ = "agent_runs"

    id: Mapped[UUID] = _pk()
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)

    phase: Mapped[str] = mapped_column(String(20), index=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)

    active_plan_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    active_plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_step_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    replan_count: Mapped[int] = mapped_column(Integer, default=0)
    plan_generation_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_counts: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    waiting_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pending_approval_action_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    pending_approval_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)

    last_action_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    last_verified_action_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    unresolved_effect_action_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)

    budget: Mapped[dict[str, Any]] = mapped_column(JsonType)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_used_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))

    baseline_outcomes: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    started_at: Mapped[datetime] = _ts(server_default=func.now())
    wall_clock_deadline: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True)

    # Concorrência — correção C11.
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = _ts(nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)

    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        CheckConstraint("iteration >= 0", name="ck_runs_iteration"),
        CheckConstraint("state_version >= 0", name="ck_runs_state_version"),
        CheckConstraint(
            "plan_generation_count >= replan_count", name="ck_runs_plan_counters"
        ),
        Index("ix_runs_lease", "lease_expires_at"),
    )


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[UUID] = _pk()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))

    source: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(80))
    content: Mapped[dict[str, Any]] = mapped_column(JsonType)
    content_hash: Mapped[str] = mapped_column(String(80))
    trust: Mapped[str] = mapped_column(String(20), index=True)
    confidence: Mapped[float]
    tags: Mapped[list[Any]] = mapped_column(JsonType, default=list)

    occurred_at: Mapped[datetime] = _ts()
    received_at: Mapped[datetime] = _ts(server_default=func.now())
    consumed_at: Mapped[datetime | None] = _ts(nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    __table_args__ = (
        # Percepção busca o que ainda não foi consumido neste run.
        Index("ix_observations_pending", "run_id", "consumed_at"),
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[UUID] = _pk()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(JsonType)
    assumptions: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    completion_condition: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    invalidated_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(server_default=func.now())

    __table_args__ = (Index("ix_plans_active", "run_id", "is_active"),)


class Action(Base):
    """Ação lógica. Uma linha por intenção, N tentativas em `action_attempts`."""

    __tablename__ = "actions"

    id: Mapped[UUID] = _pk()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    logical_action_id: Mapped[UUID] = mapped_column(Uuid, index=True)

    tool: Mapped[str] = mapped_column(String(100))
    tool_version: Mapped[str] = mapped_column(String(40))
    arguments: Mapped[dict[str, Any]] = mapped_column(JsonType)
    expected_outcomes: Mapped[list[Any]] = mapped_column(JsonType)
    rationale_code: Mapped[str] = mapped_column(String(80))
    plan_step_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Correção C09: propósitos disjuntos.
    idempotency_key: Mapped[str] = mapped_column(String(80))
    action_fingerprint: Mapped[str] = mapped_column(String(80))

    derived_from: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    risk_level: Mapped[str] = mapped_column(String(4))
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = _ts(server_default=func.now())

    attempts: Mapped[list[ActionAttempt]] = relationship(
        back_populates="action", cascade="all, delete-orphan", order_by="ActionAttempt.attempt_no"
    )

    __table_args__ = (
        # at-most-once do efeito externo por ação lógica.
        Index("uq_actions_idempotency", "idempotency_key", unique=True),
        # detecção de loop dentro do run.
        Index("ix_actions_fingerprint", "run_id", "action_fingerprint"),
    )


class ActionAttempt(Base):
    """Marcador durável de execução — correção C08.

    A linha é gravada e commitada com `IN_FLIGHT` **antes** da chamada
    externa. É ela, não a fase do checkpoint, que responde "o efeito pode ter
    saído?" depois de um crash.
    """

    __tablename__ = "action_attempts"

    id: Mapped[UUID] = _pk()
    action_id: Mapped[UUID] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = _ts(server_default=func.now())
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    probe_outcome: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)

    action: Mapped[Action] = relationship(back_populates="attempts")

    __table_args__ = (
        Index("uq_attempt_no", "action_id", "attempt_no", unique=True),
        Index("ix_attempts_in_flight", "status"),
    )


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[UUID] = _pk()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    iteration: Mapped[int] = mapped_column(Integer)

    goal_summary: Mapped[str] = mapped_column(Text)
    observation_summary: Mapped[str] = mapped_column(Text)
    decision_type: Mapped[str] = mapped_column(String(20))
    plan_step_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    result_summary: Mapped[str] = mapped_column(Text)
    verification: Mapped[dict[str, Any]] = mapped_column(JsonType)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    reward: Mapped[float] = mapped_column(default=0.0)
    importance: Mapped[float] = mapped_column(default=0.0, index=True)
    tags: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = _ts(server_default=func.now())

    __table_args__ = (Index("ix_episodes_run_iteration", "run_id", "iteration", unique=True),)


class Skill(Base):
    """Memória procedural. V0: cadastro manual e versionado."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(40), primary_key=True)

    description: Mapped[str] = mapped_column(Text)
    trigger_tags: Mapped[list[Any]] = mapped_column(JsonType)
    required_inputs: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    preconditions: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    action_template: Mapped[dict[str, Any]] = mapped_column(JsonType)
    success_criteria: Mapped[list[Any]] = mapped_column(JsonType)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class RunEvent(Base):
    """Tracing e auditoria. Não é event sourcing: nada é reconstruído daqui."""

    __tablename__ = "run_events"

    id: Mapped[UUID] = _pk()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    iteration: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    from_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    at: Mapped[datetime] = _ts(server_default=func.now())

    __table_args__ = (Index("ix_run_events_run_at", "run_id", "at"),)


class PlanCacheEntry(Base):
    """Plano que já funcionou, indexado por assinatura do objetivo.

    Correção C16. Sem isto, o único mecanismo de reuso na V0 é injetar
    episódios no prompt e torcer para o LLM planejar melhor — alta variância
    e não atribuível. O cache **propõe**; o PlannerValidator autoriza.
    """

    __tablename__ = "plan_cache"

    id: Mapped[UUID] = _pk()
    goal_fingerprint: Mapped[str] = mapped_column(String(80), unique=True)

    objective: Mapped[str] = mapped_column(Text)
    completion_condition: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(JsonType)
    assumptions: Mapped[list[Any]] = mapped_column(JsonType, default=list)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    successes: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = _ts(server_default=func.now())
    last_used_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        CheckConstraint("successes <= attempts", name="ck_plan_cache_counters"),
    )
