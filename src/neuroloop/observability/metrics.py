"""Métricas do run — TASK-014 (spec §31, correção C18).

Duas regras que separam medição de teatro:

**Denominador pequeno não vira percentual.** Abaixo de `MIN_DENOMINATOR` a
métrica é reportada como indefinida (`None`), não como 0% ou 100%. Um
`fast_path_success_rate` de "100%" sobre duas execuções não informa nada e
induz confiança falsa (C13, C18).

**`false_success_rate` não é calculado aqui.** Ele compara o que o agente
*declarou* com o que um oracle independente observou, e o oracle não pode
compartilhar código com o agente (C17). Este módulo expõe o numerador que o
harness precisa — os runs declarados `COMPLETED` — e para aí.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.enums import RunPhase
from neuroloop.persistence import models

MIN_DENOMINATOR = 20
"""C18: abaixo disso a taxa é ruído; reporta-se indefinido."""


def rate(numerator: int, denominator: int, *, minimum: int = MIN_DENOMINATOR) -> float | None:
    if denominator < minimum:
        return None
    return round(numerator / denominator, 4)


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Medidas de um único run. Agregar entre runs é do harness."""

    run_id: UUID
    phase: RunPhase
    iterations: int
    tokens_used: int
    cost_usd: Decimal

    actions_proposed: int
    actions_executed: int
    attempts: int
    retries: int
    unsafe_action_proposals: int
    duplicate_side_effects: int
    dangling_attempts: int

    deliberations: int
    fast_path_step_hits: int
    fast_path_skill_hits: int
    replans: int
    plan_generations: int
    episodes: int

    @property
    def declared_complete(self) -> bool:
        """Numerador do `false_success_rate` — o denominador é do oracle."""
        return self.phase is RunPhase.COMPLETED

    @property
    def fast_path_hit_rate(self) -> float | None:
        total = self.deliberations + self.fast_path_step_hits + self.fast_path_skill_hits
        return rate(self.fast_path_step_hits + self.fast_path_skill_hits, total)

    @property
    def retry_rate(self) -> float | None:
        return rate(self.retries, self.actions_executed)

    @property
    def replan_rate(self) -> float | None:
        return rate(self.replans, self.plan_generations)

    @property
    def unauthorized_execution_rate(self) -> float:
        """Alvo é zero absoluto: qualquer ocorrência é falha dura (C18)."""
        return 0.0 if self.actions_executed == 0 else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "phase": self.phase.value,
            "iterations": self.iterations,
            "tokens_used": self.tokens_used,
            "cost_usd": str(self.cost_usd),
            "actions_proposed": self.actions_proposed,
            "actions_executed": self.actions_executed,
            "unsafe_action_proposals": self.unsafe_action_proposals,
            "duplicate_side_effects": self.duplicate_side_effects,
            "dangling_attempts": self.dangling_attempts,
            "attempts": self.attempts,
            "retries": self.retries,
            "deliberations": self.deliberations,
            "fast_path_step_hits": self.fast_path_step_hits,
            "fast_path_skill_hits": self.fast_path_skill_hits,
            "replans": self.replans,
            "episodes": self.episodes,
            "declared_complete": self.declared_complete,
            "fast_path_hit_rate": self.fast_path_hit_rate,
            "retry_rate": self.retry_rate,
            "replan_rate": self.replan_rate,
        }


async def collect_run_metrics(session: AsyncSession, run_id: UUID) -> RunMetrics:
    run = await session.get(models.AgentRun, run_id)
    if run is None:
        raise LookupError(f"run {run_id} não encontrado")

    acoes = list(
        await session.scalars(select(models.Action).where(models.Action.run_id == run_id))
    )
    action_ids = [a.id for a in acoes]

    tentativas = (
        list(
            await session.scalars(
                select(models.ActionAttempt).where(
                    models.ActionAttempt.action_id.in_(action_ids)
                )
            )
        )
        if action_ids
        else []
    )
    executadas = {a.action_id for a in tentativas}

    duplicadas = 0
    if action_ids:
        rows = await session.execute(
            select(models.Action.idempotency_key, func.count())
            .join(
                models.ActionAttempt,
                models.ActionAttempt.action_id == models.Action.id,
            )
            .where(
                models.Action.run_id == run_id,
                models.ActionAttempt.status == "SUCCESS",
            )
            .group_by(models.Action.idempotency_key)
            .having(func.count() > 1)
        )
        duplicadas = len(list(rows))

    eventos = list(
        await session.scalars(
            select(models.RunEvent).where(models.RunEvent.run_id == run_id)
        )
    )
    autorizacoes = [e for e in eventos if e.kind == "ACTION_AUTHORIZATION"]
    recusadas = sum(1 for e in autorizacoes if e.error_code is not None)
    por_fonte = [
        (e.payload or {}).get("source") for e in autorizacoes if (e.payload or {})
    ]

    episodios = (
        await session.scalar(
            select(func.count())
            .select_from(models.Episode)
            .where(models.Episode.run_id == run_id)
        )
    ) or 0

    return RunMetrics(
        run_id=run_id,
        phase=RunPhase(run.phase),
        iterations=run.iteration,
        tokens_used=run.tokens_used,
        cost_usd=Decimal(str(run.cost_used_usd)),
        actions_proposed=len(acoes),
        actions_executed=len(executadas),
        attempts=len(tentativas),
        retries=max(len(tentativas) - len(executadas), 0),
        unsafe_action_proposals=recusadas,
        duplicate_side_effects=duplicadas,
        dangling_attempts=sum(1 for a in tentativas if a.status == "IN_FLIGHT"),
        deliberations=por_fonte.count("DELIBERATOR"),
        fast_path_step_hits=por_fonte.count("STEP"),
        fast_path_skill_hits=por_fonte.count("SKILL"),
        replans=run.replan_count,
        plan_generations=run.plan_generation_count,
        episodes=episodios,
    )
