"""Persistência de agent, goal e run/checkpoint — núcleo da TASK-003.

Aqui vivem os dois mecanismos de concorrência da correção C11, que têm
papéis distintos e não são redundantes:

- **lease** (`lease_owner`, `lease_expires_at`, `lease_epoch`): impede que
  dois runners entrem no mesmo run. Tem TTL, então um processo morto não
  trava o run para sempre. Não é advisory lock em conexão — a conexão ficaria
  presa durante os até 900s de execução.
- **optimistic lock** (`state_version`): detecta escrita conflitante caso
  algo escape à lease.

O `lease_epoch` é o fencing token: uma escrita carregando epoch antigo é
rejeitada mesmo que o `state_version` bata, o que cobre o processo pausado
que volta depois de a lease ter sido tomada por outro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.criteria import CriterionOutcome
from neuroloop.core.enums import GoalStatus, RunPhase
from neuroloop.core.goals import Goal
from neuroloop.core.runs import ExecutionBudget, RunCheckpoint
from neuroloop.persistence import models
from neuroloop.persistence.errors import (
    LeaseLostError,
    LeaseUnavailableError,
    RunNotFoundError,
    StateConflictError,
)

DEFAULT_LEASE_TTL_SECONDS = 60
"""TTL curto com heartbeat a cada terço do período (correção C11)."""


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: UUID
    owner: str
    epoch: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResumeState:
    """Sinais duráveis que decidem onde um run retomado reentra (C08).

    Só os fatos. Traduzi-los em fase é do runtime — persistência que decide
    semântica cognitiva inverte a dependência e cria ciclo de import.
    """

    persisted_phase: RunPhase
    has_in_flight_attempt: bool
    unresolved_effect: bool
    has_unverified_action: bool
    cancel_requested: bool


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure(self, name: str) -> UUID:
        found = await self.session.scalar(
            select(models.Agent).where(models.Agent.name == name)
        )
        if found is not None:
            return found.id
        agent = models.Agent(id=uuid4(), name=name)
        self.session.add(agent)
        await self.session.flush()
        return agent.id


class GoalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, goal: Goal) -> UUID:
        row = models.Goal(
            id=goal.id,
            agent_id=goal.agent_id,
            description=goal.description,
            priority=goal.priority,
            deadline=goal.deadline,
            status=goal.status.value,
            success_criteria=[c.model_dump(mode="json") for c in goal.success_criteria],
            failure_criteria=[c.model_dump(mode="json") for c in goal.failure_criteria],
            constraints=[c.model_dump(mode="json") for c in goal.constraints],
            created_at=goal.created_at,
            updated_at=goal.updated_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def get(self, goal_id: UUID) -> Goal:
        row = await self.session.get(models.Goal, goal_id)
        if row is None:
            raise LookupError(f"goal {goal_id} não encontrado")
        return Goal.model_validate(
            {
                "id": row.id,
                "agent_id": row.agent_id,
                "description": row.description,
                "priority": row.priority,
                "deadline": row.deadline,
                "status": row.status,
                "success_criteria": row.success_criteria,
                "failure_criteria": row.failure_criteria,
                "constraints": row.constraints,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )

    async def set_status(self, goal_id: UUID, status: GoalStatus, now: datetime) -> None:
        await self.session.execute(
            update(models.Goal)
            .where(models.Goal.id == goal_id)
            .values(status=status.value, updated_at=now)
        )


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- criação

    async def create(
        self,
        *,
        goal_id: UUID,
        started_at: datetime,
        budget: ExecutionBudget | None = None,
        baseline_outcomes: tuple[CriterionOutcome, ...] = (),
        run_id: UUID | None = None,
    ) -> RunCheckpoint:
        budget = budget or ExecutionBudget()
        checkpoint = RunCheckpoint(
            run_id=run_id or uuid4(),
            goal_id=goal_id,
            phase=RunPhase.CREATED,
            budget=budget,
            baseline_outcomes=baseline_outcomes,
            started_at=started_at,
            wall_clock_deadline=started_at + timedelta(seconds=budget.wall_clock_seconds),
        )
        self.session.add(models.AgentRun(**_to_row(checkpoint)))
        await self.session.flush()
        return checkpoint

    # ----------------------------------------------------------------- leitura

    async def load(self, run_id: UUID) -> RunCheckpoint:
        row = await self.session.get(models.AgentRun, run_id)
        if row is None:
            raise RunNotFoundError(run_id)
        return _to_checkpoint(row)

    async def resume_state(self, run_id: UUID) -> ResumeState:
        """Coleta os sinais duráveis que determinam a fase de retomada.

        A fase gravada é dica; `action_attempts` e os ponteiros de ação são a
        verdade.
        """
        row = await self.session.get(models.AgentRun, run_id)
        if row is None:
            raise RunNotFoundError(run_id)

        in_flight = await self.session.scalar(
            select(models.ActionAttempt.id)
            .join(models.Action, models.Action.id == models.ActionAttempt.action_id)
            .where(
                models.Action.run_id == run_id,
                models.ActionAttempt.status == "IN_FLIGHT",
            )
            .limit(1)
        )
        return ResumeState(
            persisted_phase=RunPhase(row.phase),
            has_in_flight_attempt=in_flight is not None,
            unresolved_effect=row.unresolved_effect_action_id is not None,
            has_unverified_action=(
                row.last_action_id is not None
                and row.last_action_id != row.last_verified_action_id
            ),
            cancel_requested=row.cancel_requested,
        )

    # ------------------------------------------------------------------ escrita

    async def save(self, checkpoint: RunCheckpoint, *, lease: Lease | None = None) -> RunCheckpoint:
        """Grava o checkpoint sob optimistic lock e fencing token.

        Devolve o checkpoint com `state_version` já incrementado. Levanta
        `LeaseLostError` se o epoch não bate e `StateConflictError` se a
        versão não bate — a distinção importa porque a primeira significa
        "outro runner assumiu" e a segunda, "escrita concorrente".
        """
        values = _to_row(checkpoint)
        values.pop("id")
        values.pop("lease_owner", None)
        values.pop("lease_expires_at", None)
        values.pop("lease_epoch", None)
        values["state_version"] = checkpoint.state_version + 1

        stmt = (
            update(models.AgentRun)
            .where(
                models.AgentRun.id == checkpoint.run_id,
                models.AgentRun.state_version == checkpoint.state_version,
            )
            .values(**values)
        )
        if lease is not None:
            stmt = stmt.where(models.AgentRun.lease_epoch == lease.epoch)

        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            await self._raise_write_conflict(checkpoint, lease)
        await self.session.flush()
        return checkpoint.model_copy(update={"state_version": checkpoint.state_version + 1})

    async def _raise_write_conflict(
        self, checkpoint: RunCheckpoint, lease: Lease | None
    ) -> None:
        row = await self.session.get(models.AgentRun, checkpoint.run_id)
        if row is None:
            raise RunNotFoundError(checkpoint.run_id)
        if lease is not None and row.lease_epoch != lease.epoch:
            raise LeaseLostError(checkpoint.run_id, lease.epoch, row.lease_epoch)
        raise StateConflictError(
            checkpoint.run_id, checkpoint.state_version, row.state_version
        )

    async def request_cancel(self, run_id: UUID) -> None:
        """Sinaliza cancelamento sem tocar em `state_version`.

        Pedir cancelamento é sempre legítimo e não pode competir com o
        runner pela versão do estado; quem honra o pedido é o loop, no topo
        do próximo ciclo (ver `RunStateMachine.request_cancel`).
        """
        await self.session.execute(
            update(models.AgentRun)
            .where(models.AgentRun.id == run_id)
            .values(cancel_requested=True)
        )

    # -------------------------------------------------------------------- lease

    async def acquire_lease(
        self,
        run_id: UUID,
        *,
        owner: str,
        now: datetime,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> Lease:
        expires_at = now + timedelta(seconds=ttl_seconds)
        result = await self.session.execute(
            update(models.AgentRun)
            .where(
                models.AgentRun.id == run_id,
                # livre, expirada, ou já minha (restart do mesmo worker)
                (models.AgentRun.lease_owner.is_(None))
                | (models.AgentRun.lease_expires_at < now)
                | (models.AgentRun.lease_owner == owner),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=expires_at,
                lease_epoch=models.AgentRun.lease_epoch + 1,
            )
            .returning(models.AgentRun.lease_epoch)
        )
        epoch = result.scalar_one_or_none()
        if epoch is None:
            row = await self.session.get(models.AgentRun, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            raise LeaseUnavailableError(run_id, row.lease_owner)
        await self.session.flush()
        return Lease(run_id=run_id, owner=owner, epoch=epoch, expires_at=expires_at)

    async def renew_lease(
        self,
        lease: Lease,
        *,
        now: datetime,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> Lease:
        """Heartbeat. Falha se outro runner já assumiu o run."""
        expires_at = now + timedelta(seconds=ttl_seconds)
        result = await self.session.execute(
            update(models.AgentRun)
            .where(
                models.AgentRun.id == lease.run_id,
                models.AgentRun.lease_owner == lease.owner,
                models.AgentRun.lease_epoch == lease.epoch,
            )
            .values(lease_expires_at=expires_at)
        )
        if result.rowcount == 0:
            row = await self.session.get(models.AgentRun, lease.run_id)
            raise LeaseLostError(
                lease.run_id, lease.epoch, row.lease_epoch if row else None
            )
        await self.session.flush()
        return Lease(
            run_id=lease.run_id, owner=lease.owner, epoch=lease.epoch, expires_at=expires_at
        )

    async def release_lease(self, lease: Lease) -> None:
        await self.session.execute(
            update(models.AgentRun)
            .where(
                models.AgentRun.id == lease.run_id,
                models.AgentRun.lease_owner == lease.owner,
                models.AgentRun.lease_epoch == lease.epoch,
            )
            .values(lease_owner=None, lease_expires_at=None)
        )


# ------------------------------------------------------------------ mapeamento


def _to_row(checkpoint: RunCheckpoint) -> dict[str, Any]:
    return {
        "id": checkpoint.run_id,
        "goal_id": checkpoint.goal_id,
        "phase": checkpoint.phase.value,
        "iteration": checkpoint.iteration,
        "active_plan_id": checkpoint.active_plan_id,
        "active_plan_version": checkpoint.active_plan_version,
        "current_step_id": checkpoint.current_step_id,
        "replan_count": checkpoint.replan_count,
        "plan_generation_count": checkpoint.plan_generation_count,
        "retry_counts": {str(k): v for k, v in checkpoint.retry_counts.items()},
        "waiting_reason": checkpoint.waiting_reason,
        "pending_approval_action_id": checkpoint.pending_approval_action_id,
        "pending_approval_fingerprint": checkpoint.pending_approval_fingerprint,
        "last_action_id": checkpoint.last_action_id,
        "last_verified_action_id": checkpoint.last_verified_action_id,
        "unresolved_effect_action_id": checkpoint.unresolved_effect_action_id,
        "budget": checkpoint.budget.model_dump(mode="json"),
        "tokens_used": checkpoint.tokens_used,
        "cost_used_usd": checkpoint.cost_used_usd,
        "baseline_outcomes": [o.model_dump(mode="json") for o in checkpoint.baseline_outcomes],
        "cancel_requested": checkpoint.cancel_requested,
        "started_at": checkpoint.started_at,
        "wall_clock_deadline": checkpoint.wall_clock_deadline,
        "state_version": checkpoint.state_version,
        "lease_owner": None,
        "lease_expires_at": None,
        "lease_epoch": checkpoint.lease_epoch,
    }


def _to_checkpoint(row: models.AgentRun) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=row.id,
        goal_id=row.goal_id,
        phase=RunPhase(row.phase),
        iteration=row.iteration,
        active_plan_id=row.active_plan_id,
        active_plan_version=row.active_plan_version,
        current_step_id=row.current_step_id,
        replan_count=row.replan_count,
        plan_generation_count=row.plan_generation_count,
        retry_counts={UUID(k): v for k, v in (row.retry_counts or {}).items()},
        waiting_reason=row.waiting_reason,
        pending_approval_action_id=row.pending_approval_action_id,
        pending_approval_fingerprint=row.pending_approval_fingerprint,
        last_action_id=row.last_action_id,
        last_verified_action_id=row.last_verified_action_id,
        unresolved_effect_action_id=row.unresolved_effect_action_id,
        budget=ExecutionBudget.model_validate(row.budget),
        tokens_used=row.tokens_used,
        cost_used_usd=Decimal(str(row.cost_used_usd)),
        baseline_outcomes=tuple(
            CriterionOutcome.model_validate(o) for o in (row.baseline_outcomes or [])
        ),
        cancel_requested=row.cancel_requested,
        started_at=row.started_at,
        wall_clock_deadline=row.wall_clock_deadline,
        state_version=row.state_version,
        lease_epoch=row.lease_epoch,
    )
