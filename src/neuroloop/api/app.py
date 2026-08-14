"""API do runtime — TASK-013 (spec §25).

Superfície mínima para operar e intervir:

    criar goal → iniciar run → executar → consultar → aprovar / retomar /
    cancelar → ler o trace

O aceite explícito da task é `WAITING_USER` **sobreviver a restart**: a
espera não vive em memória do processo, vive no checkpoint. Um run parado
esperando humano continua parado depois de o processo morrer, e a aprovação
que chega depois encontra o mesmo estado.

A API não decide nada: valida entrada, delega ao runtime e devolve estado.
Em particular, `approve` **não** executa a ação — apenas registra que o
humano autorizou aqueles argumentos exatos; quem executa é o loop, na
próxima passada, depois de a policy reavaliar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neuroloop.api.schemas import (
    ApproveRequest,
    CreateGoalRequest,
    EpisodeView,
    GoalView,
    ResumeRequest,
    RunResultView,
    RunView,
    StartRunRequest,
    TraceEvent,
)
from neuroloop.core.enums import GoalStatus, RunPhase
from neuroloop.core.goals import Goal
from neuroloop.core.runs import ExecutionBudget
from neuroloop.persistence import models
from neuroloop.persistence.errors import RunNotFoundError
from neuroloop.persistence.repositories import (
    ActionRepository,
    AgentRepository,
    EpisodeRepository,
    GoalRepository,
    RunEventRepository,
    RunRepository,
)
from neuroloop.observability import collect_run_metrics, explain_run, run_timeline
from neuroloop.runtime.agent_runtime import AgentRuntime, RunResult


def create_app(
    *, runtime: AgentRuntime, session_factory: async_sessionmaker[AsyncSession]
) -> FastAPI:
    app = FastAPI(title="NeuroLoop", version="0.0.1")

    async def get_session():
        async with session_factory() as session:
            yield session

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------- goals

    @app.post("/goals", response_model=GoalView, status_code=status.HTTP_201_CREATED)
    async def create_goal(
        request: CreateGoalRequest, session: AsyncSession = Depends(get_session)
    ) -> GoalView:
        now = datetime.now(UTC)
        agent_id = await AgentRepository(session).ensure(request.agent_name)
        try:
            goal = Goal(
                id=uuid4(),
                agent_id=agent_id,
                description=request.description,
                priority=request.priority,
                deadline=request.deadline,
                success_criteria=request.success_criteria,
                failure_criteria=request.failure_criteria,
                status=GoalStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        except ValueError as error:
            # Goal sem evidência externa (C02) é recusado na porta de entrada,
            # não três ciclos adiante.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

        await GoalRepository(session).create(goal)
        await session.commit()
        return GoalView(
            goal_id=goal.id, description=goal.description, status=goal.status.value
        )

    # --------------------------------------------------------------- runs

    @app.post(
        "/goals/{goal_id}/runs", response_model=RunView, status_code=status.HTTP_201_CREATED
    )
    async def start_run(
        goal_id: UUID,
        request: StartRunRequest | None = None,
        session: AsyncSession = Depends(get_session),
    ) -> RunView:
        try:
            goal = await GoalRepository(session).get(goal_id)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error

        checkpoint = await runtime.start(goal, budget=_budget(request))
        return await _run_view(session, checkpoint.run_id)

    @app.get("/runs/{run_id}", response_model=RunView)
    async def get_run(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> RunView:
        return await _run_view(session, run_id)

    @app.post("/runs/{run_id}/execute", response_model=RunResultView)
    async def execute(run_id: UUID) -> RunResultView:
        return _result_view(await _guarded(runtime.run_until_pause(run_id)))

    @app.post("/runs/{run_id}/resume", response_model=RunResultView)
    async def resume(run_id: UUID, request: ResumeRequest | None = None) -> RunResultView:
        message = request.message if request else None
        return _result_view(await _guarded(runtime.resume(run_id, message=message)))

    @app.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel(run_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
        await _run_view(session, run_id)  # 404 se não existe
        await runtime.cancel(run_id)
        return {"cancel_requested": True}

    # ---------------------------------------------------------- aprovação

    @app.post("/runs/{run_id}/approve", response_model=RunResultView)
    async def approve(
        run_id: UUID,
        request: ApproveRequest,
        session: AsyncSession = Depends(get_session),
    ) -> RunResultView:
        checkpoint = await _load(session, run_id)
        if checkpoint.phase is not RunPhase.WAITING_USER:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"run está em {checkpoint.phase.value}, não aguardando aprovação",
            )
        if checkpoint.pending_approval_action_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "nenhuma aprovação pendente")

        # C19: aprovar é confirmar *estes* argumentos. Divergência invalida.
        if (
            request.action_id != checkpoint.pending_approval_action_id
            or request.fingerprint != checkpoint.pending_approval_fingerprint
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "aprovação não corresponde à ação pendente; um novo pedido é necessário",
            )

        acao = await ActionRepository(session).approve(request.action_id)
        if acao is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "ação não encontrada")

        await RunRepository(session).save(
            checkpoint.model_copy(
                update={
                    "pending_approval_action_id": None,
                    "pending_approval_fingerprint": None,
                    "waiting_reason": None,
                }
            )
        )
        await session.commit()

        if not request.resume:
            return _result_view(
                RunResult(
                    run_id=run_id,
                    phase=RunPhase.WAITING_USER,
                    iteration=checkpoint.iteration,
                    waiting_reason="APPROVED_NOT_RESUMED",
                )
            )
        return _result_view(await _guarded(runtime.run_until_pause(run_id)))

    # -------------------------------------------------------------- trace

    @app.get("/runs/{run_id}/trace", response_model=list[TraceEvent])
    async def trace(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> list[TraceEvent]:
        await _load(session, run_id)
        events = await RunEventRepository(session).by_run(run_id)
        return [
            TraceEvent(
                kind=e.kind,
                reason=e.reason,
                from_phase=e.from_phase,
                to_phase=e.to_phase,
                error_code=e.error_code,
                iteration=e.iteration,
                payload=e.payload or {},
                at=e.at,
            )
            for e in events
        ]

    @app.get("/runs/{run_id}/episodes", response_model=list[EpisodeView])
    async def episodes(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> list[EpisodeView]:
        await _load(session, run_id)
        rows = await EpisodeRepository(session).by_run(run_id)
        return [
            EpisodeView(
                iteration=e.iteration,
                decision_type=e.decision_type,
                tool_name=e.tool_name,
                result_summary=e.result_summary,
                error_code=e.error_code,
                importance=e.importance,
                reward=e.reward,
                tags=tuple(e.tags or []),
            )
            for e in rows
        ]

    @app.get("/runs/{run_id}/explain")
    async def explain(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> list[dict]:
        """Por que o agente fez cada ação — aceite da TASK-014."""
        await _load(session, run_id)
        return [
            {
                "action_id": str(e.action_id),
                "why": e.why(),
                "tool": e.tool,
                "risk_level": e.risk_level,
                "decision_source": e.decision_source,
                "authorization": e.authorization_reason,
                "tainted": e.tainted,
                "executed": e.executed,
                "attempts": [a.status for a in e.attempts],
                "arguments": e.arguments,
                "derived_from": list(e.derived_from),
                "fingerprint": e.fingerprint,
                "trace_id": e.trace_id,
                "versions": e.versions,
            }
            for e in await explain_run(session, run_id)
        ]

    @app.get("/runs/{run_id}/metrics")
    async def metrics(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> dict:
        await _load(session, run_id)
        return (await collect_run_metrics(session, run_id)).as_dict()

    @app.get("/runs/{run_id}/timeline")
    async def timeline(
        run_id: UUID, session: AsyncSession = Depends(get_session)
    ) -> list[dict]:
        await _load(session, run_id)
        return [
            {"at": linha.at.isoformat(), "kind": linha.kind, "detail": linha.detail}
            for linha in await run_timeline(session, run_id)
        ]

    @app.get("/runs/{run_id}/actions")
    async def actions(run_id: UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
        """Ações propostas, inclusive as recusadas — auditoria de segurança."""
        await _load(session, run_id)
        rows = await session.scalars(
            select(models.Action).where(models.Action.run_id == run_id)
        )
        return [
            {
                "action_id": str(row.id),
                "tool": row.tool,
                "risk_level": row.risk_level,
                "approved_by_user": row.approved_by_user,
                "fingerprint": row.action_fingerprint,
            }
            for row in rows
        ]

    return app


# ------------------------------------------------------------------ apoio


async def _load(session: AsyncSession, run_id: UUID):
    try:
        return await RunRepository(session).load(run_id)
    except RunNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error


async def _run_view(session: AsyncSession, run_id: UUID) -> RunView:
    checkpoint = await _load(session, run_id)
    return RunView(
        run_id=checkpoint.run_id,
        goal_id=checkpoint.goal_id,
        phase=checkpoint.phase,
        iteration=checkpoint.iteration,
        waiting_reason=checkpoint.waiting_reason,
        pending_approval_action_id=checkpoint.pending_approval_action_id,
        pending_approval_fingerprint=checkpoint.pending_approval_fingerprint,
        tokens_used=checkpoint.tokens_used,
        cost_used_usd=checkpoint.cost_used_usd,
        cancel_requested=checkpoint.cancel_requested,
        started_at=checkpoint.started_at,
        wall_clock_deadline=checkpoint.wall_clock_deadline,
    )


def _result_view(result: RunResult) -> RunResultView:
    return RunResultView(
        run_id=result.run_id,
        phase=result.phase,
        iteration=result.iteration,
        error_code=result.error_code,
        waiting_reason=result.waiting_reason,
        tokens_used=result.tokens_used,
        cost_used_usd=result.cost_used_usd,
        deliberations=result.deliberations,
        fast_path_hits=result.fast_path_hits,
        goal_satisfied=result.completed,
    )


def _budget(request: StartRunRequest | None) -> ExecutionBudget | None:
    if request is None:
        return None
    campos = {
        k: v
        for k, v in {
            "max_iterations": request.max_iterations,
            "token_budget": request.token_budget,
            "wall_clock_seconds": request.wall_clock_seconds,
        }.items()
        if v is not None
    }
    return ExecutionBudget(**campos) if campos else None


async def _guarded(coro):
    try:
        return await coro
    except RunNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
