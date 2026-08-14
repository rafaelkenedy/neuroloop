"""Persistência de observações, episódios e eventos de trace.

O ranking de retrieval (correção C14) é TASK-008; aqui existe apenas o
armazenamento e uma leitura por recência/importância, suficiente para o
walking skeleton.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.enums import ErrorCode
from neuroloop.core.observations import Observation
from neuroloop.persistence import models


class ObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, observation: Observation) -> UUID:
        row = models.Observation(
            id=observation.id,
            run_id=observation.run_id,
            source=observation.source.value,
            source_ref=observation.source_ref,
            kind=observation.kind,
            content={"value": observation.content},
            content_hash=observation.content_hash,
            trust=observation.trust.value,
            confidence=observation.confidence,
            tags=list(observation.tags),
            occurred_at=observation.occurred_at,
            received_at=observation.received_at,
            meta=observation.metadata,
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def pending(self, run_id: UUID, *, limit: int = 50) -> list[Observation]:
        """Observações ainda não consumidas por um ciclo, em ordem de chegada."""
        rows = await self.session.scalars(
            select(models.Observation)
            .where(
                models.Observation.run_id == run_id,
                models.Observation.consumed_at.is_(None),
            )
            .order_by(models.Observation.received_at)
            .limit(limit)
        )
        return [_to_observation(r) for r in rows]

    async def trust_map(self, run_id: UUID) -> dict[UUID, str]:
        """Confiança de **todas** as observações do run, consumidas ou não.

        Confiança não expira quando a observação é consumida: uma ação pode
        legitimamente citar o objetivo, visto no primeiro ciclo. Restringir o
        mapa às pendentes faria proveniência válida virar "desconhecida" —
        e, sendo desconhecida tratada como não confiável, o agente seria
        bloqueado por lembrar corretamente de onde veio o dado.
        """
        rows = await self.session.execute(
            select(models.Observation.id, models.Observation.trust).where(
                models.Observation.run_id == run_id
            )
        )
        return {row[0]: row[1] for row in rows}

    async def mark_consumed(self, ids: list[UUID], *, now: datetime) -> None:
        if not ids:
            return
        await self.session.execute(
            update(models.Observation)
            .where(models.Observation.id.in_(ids))
            .values(consumed_at=now)
        )


class EpisodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        run_id: UUID,
        iteration: int,
        goal_summary: str,
        observation_summary: str,
        decision_type: str,
        result_summary: str,
        verification: dict[str, Any],
        importance: float,
        reward: float = 0.0,
        tool_name: str | None = None,
        action_id: UUID | None = None,
        plan_step_id: str | None = None,
        error_code: ErrorCode | None = None,
        tags: list[str] | None = None,
    ) -> UUID:
        row = models.Episode(
            id=uuid4(),
            run_id=run_id,
            iteration=iteration,
            goal_summary=goal_summary,
            observation_summary=observation_summary,
            decision_type=decision_type,
            plan_step_id=plan_step_id,
            action_id=action_id,
            tool_name=tool_name,
            result_summary=result_summary,
            verification=verification,
            error_code=error_code.value if error_code else None,
            reward=reward,
            importance=importance,
            tags=tags or [],
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def by_run(self, run_id: UUID) -> list[models.Episode]:
        rows = await self.session.scalars(
            select(models.Episode)
            .where(models.Episode.run_id == run_id)
            .order_by(models.Episode.iteration)
        )
        return list(rows)

    async def top_by_importance(self, *, limit: int = 5) -> list[models.Episode]:
        rows = await self.session.scalars(
            select(models.Episode)
            .order_by(models.Episode.importance.desc(), models.Episode.created_at.desc())
            .limit(limit)
        )
        return list(rows)


class RunEventRepository:
    """Trace e auditoria. Nada é reconstruído a partir daqui."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        run_id: UUID,
        iteration: int,
        kind: str,
        reason: str | None = None,
        from_phase: str | None = None,
        to_phase: str | None = None,
        error_code: ErrorCode | None = None,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
        at: datetime | None = None,
    ) -> UUID:
        row = models.RunEvent(
            id=uuid4(),
            run_id=run_id,
            iteration=iteration,
            kind=kind,
            from_phase=from_phase,
            to_phase=to_phase,
            reason=reason,
            error_code=error_code.value if error_code else None,
            payload=payload or {},
            trace_id=trace_id,
            **({"at": at} if at is not None else {}),
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def record_transition(
        self,
        *,
        run_id: UUID,
        iteration: int,
        from_phase: str,
        to_phase: str,
        reason: str,
        error_code: ErrorCode | None = None,
        at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID:
        """Recebe primitivos, não o registro do runtime.

        Persistência não conhece o runtime: a dependência inversa criava um
        ciclo de import e, pior, amarrava a camada de dados ao formato de um
        componente cognitivo.
        """
        return await self.append(
            run_id=run_id,
            iteration=iteration,
            kind="PHASE_TRANSITION",
            reason=reason,
            from_phase=from_phase,
            to_phase=to_phase,
            error_code=error_code,
            trace_id=trace_id,
            at=at,
        )

    async def by_run(self, run_id: UUID) -> list[models.RunEvent]:
        rows = await self.session.scalars(
            select(models.RunEvent)
            .where(models.RunEvent.run_id == run_id)
            .order_by(models.RunEvent.at, models.RunEvent.iteration)
        )
        return list(rows)


def _to_observation(row: models.Observation) -> Observation:
    return Observation.model_validate(
        {
            "id": row.id,
            "run_id": row.run_id,
            "source": row.source,
            "source_ref": row.source_ref,
            "kind": row.kind,
            "content": (row.content or {}).get("value"),
            "content_hash": row.content_hash,
            "trust": row.trust,
            "confidence": row.confidence,
            "tags": row.tags or [],
            "occurred_at": row.occurred_at,
            "received_at": row.received_at,
            "metadata": row.meta or {},
        }
    )
