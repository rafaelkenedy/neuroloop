"""Persistência de planos, ações e tentativas.

O ponto crítico deste módulo é `start_attempt`: a linha `IN_FLIGHT` precisa
estar **commitada antes** da chamada externa (correção C08). Sem isso, um
crash durante a chamada deixa o sistema sem qualquer registro de que um
efeito pode ter saído — e o retry vira duplicação silenciosa.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.actions import ActionProposal
from neuroloop.core.enums import AttemptStatus, ErrorCode, PlanStepStatus, RiskLevel
from neuroloop.core.identity import make_action_fingerprint, make_idempotency_key
from neuroloop.core.plans import Plan
from neuroloop.persistence import models


class PlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_active(self, run_id: UUID, plan: Plan, *, now: datetime) -> UUID:
        """Novo plano ativo; o anterior é invalidado, não apagado."""
        await self.invalidate_active(run_id, now=now)
        row = models.Plan(
            id=plan.id,
            run_id=run_id,
            version=plan.version,
            objective=plan.objective,
            steps=[s.model_dump(mode="json") for s in plan.steps],
            assumptions=list(plan.assumptions),
            completion_condition=plan.completion_condition,
            is_active=True,
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def invalidate_active(self, run_id: UUID, *, now: datetime) -> None:
        await self.session.execute(
            update(models.Plan)
            .where(models.Plan.run_id == run_id, models.Plan.is_active.is_(True))
            .values(is_active=False, invalidated_at=now)
        )

    async def mark_step(self, run_id: UUID, step_id: str, status: PlanStepStatus) -> None:
        """Atualiza o status de um step dentro do JSON do plano ativo."""
        row = await self.session.scalar(
            select(models.Plan).where(
                models.Plan.run_id == run_id, models.Plan.is_active.is_(True)
            )
        )
        if row is None:
            raise LookupError(f"run {run_id} não tem plano ativo")
        row.steps = [
            {**step, "status": status.value} if step["id"] == step_id else step
            for step in row.steps
        ]
        await self.session.flush()

    async def active(self, run_id: UUID) -> Plan | None:
        row = await self.session.scalar(
            select(models.Plan).where(
                models.Plan.run_id == run_id, models.Plan.is_active.is_(True)
            )
        )
        if row is None:
            return None
        return Plan.model_validate(
            {
                "id": row.id,
                "version": row.version,
                "objective": row.objective,
                "steps": row.steps,
                "assumptions": row.assumptions,
                "completion_condition": row.completion_condition,
            }
        )


class ActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_logical_action(
        self,
        *,
        run_id: UUID,
        proposal: ActionProposal,
        tool_version: str,
        risk_level: RiskLevel,
        target_resource: str | None = None,
        plan_step_id: str | None = None,
        approved_by_user: bool = False,
        logical_action_id: UUID | None = None,
    ) -> models.Action:
        logical_action_id = logical_action_id or uuid4()
        row = models.Action(
            id=uuid4(),
            run_id=run_id,
            logical_action_id=logical_action_id,
            tool=proposal.tool,
            tool_version=tool_version,
            arguments=proposal.arguments,
            expected_outcomes=[c.model_dump(mode="json") for c in proposal.expected_outcomes],
            rationale_code=proposal.rationale_code,
            plan_step_id=plan_step_id,
            idempotency_key=make_idempotency_key(run_id, logical_action_id),
            action_fingerprint=make_action_fingerprint(
                tool=proposal.tool,
                tool_version=tool_version,
                arguments=proposal.arguments,
                target_resource=target_resource,
            ),
            derived_from=[str(o) for o in proposal.derived_from],
            risk_level=risk_level.value,
            approved_by_user=approved_by_user,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, action_id: UUID) -> models.Action | None:
        return await self.session.get(models.Action, action_id)

    async def find_by_idempotency_key(self, key: str) -> models.Action | None:
        return await self.session.scalar(
            select(models.Action).where(models.Action.idempotency_key == key)
        )

    async def approved_fingerprints(self, run_id: UUID) -> frozenset[str]:
        """Fingerprints que um humano de fato aprovou neste run (C19)."""
        rows = await self.session.scalars(
            select(models.Action.action_fingerprint).where(
                models.Action.run_id == run_id,
                models.Action.approved_by_user.is_(True),
            )
        )
        return frozenset(rows)

    async def approve(self, action_id: UUID) -> models.Action | None:
        row = await self.session.get(models.Action, action_id)
        if row is None:
            return None
        row.approved_by_user = True
        await self.session.flush()
        return row

    async def count_fingerprint(self, run_id: UUID, fingerprint: str) -> int:
        """Ocorrências da mesma ação no run — insumo de `LOOP_DETECTED`."""
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(models.Action)
                .where(
                    models.Action.run_id == run_id,
                    models.Action.action_fingerprint == fingerprint,
                )
            )
        ) or 0

    # ------------------------------------------------------------- tentativas

    async def start_attempt(self, action_id: UUID, *, now: datetime) -> models.ActionAttempt:
        """Grava a tentativa como `IN_FLIGHT`.

        O chamador **precisa** commitar antes de disparar a chamada externa.
        `ExecutorRepository.start_attempt` não commita sozinho porque a
        sessão pertence ao chamador, mas a ordem é obrigatória: commit →
        chamada externa → segunda transação com o desfecho.
        """
        last = await self.session.scalar(
            select(func.max(models.ActionAttempt.attempt_no)).where(
                models.ActionAttempt.action_id == action_id
            )
        )
        row = models.ActionAttempt(
            id=uuid4(),
            action_id=action_id,
            attempt_no=(last or 0) + 1,
            status=AttemptStatus.IN_FLIGHT.value,
            started_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def finish_attempt(
        self,
        attempt_id: UUID,
        *,
        status: AttemptStatus,
        now: datetime,
        result: dict[str, Any] | None = None,
        error_code: ErrorCode | None = None,
        error_detail: str | None = None,
        probe_outcome: dict[str, Any] | None = None,
    ) -> None:
        row = await self.session.get(models.ActionAttempt, attempt_id)
        if row is None:
            raise LookupError(f"attempt {attempt_id} não encontrada")
        row.status = status.value
        row.finished_at = now
        row.duration_ms = int((now - row.started_at).total_seconds() * 1000)
        row.result = result
        row.error_code = error_code.value if error_code else None
        row.error_detail = error_detail
        row.probe_outcome = probe_outcome
        await self.session.flush()

    async def in_flight_attempts(self, run_id: UUID) -> list[models.ActionAttempt]:
        rows = await self.session.scalars(
            select(models.ActionAttempt)
            .join(models.Action, models.Action.id == models.ActionAttempt.action_id)
            .where(
                models.Action.run_id == run_id,
                models.ActionAttempt.status == AttemptStatus.IN_FLIGHT.value,
            )
        )
        return list(rows)

    async def attempt_count(self, action_id: UUID) -> int:
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(models.ActionAttempt)
                .where(models.ActionAttempt.action_id == action_id)
            )
        ) or 0
