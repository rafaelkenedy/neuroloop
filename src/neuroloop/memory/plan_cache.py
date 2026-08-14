"""Plan cache — TASK-011 (correção C16).

A spec deixava H3 (reutilização) sem mecanismo determinístico: Fast Path não
aprende, skills são manuais e memória semântica é V0.5, então a única via de
reuso seria o LLM ler episódios e planejar melhor. Isso é alta variância e —
pior — não é atribuível: qualquer ganho medido no B5 poderia ser sorte do
modelo.

Este cache dá a H3 um mecanismo inspecionável. Ao concluir um run com
sucesso, o plano executado é gravado sob a **assinatura do objetivo**. Num
run parecido, o plano é **proposto** ao `PlannerValidator` — nunca executado
às cegas. A divisão de responsabilidades continua de pé:

    cache propõe → validador autoriza → Verifier conclui

A assinatura usa os `success_criteria` e o conjunto de tools, não a
descrição em prosa: dois goals com o mesmo texto e critérios diferentes são
objetivos diferentes, e prosa é o que conteúdo externo consegue imitar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.goals import Goal
from neuroloop.core.identity import canonical_json, _digest  # noqa: PLC2701
from neuroloop.core.plans import Plan, PlanStep
from neuroloop.persistence import models

MIN_SUCCESS_RATE = 0.8
"""C16: abaixo disso o plano não é proposto de novo."""

MIN_ATTEMPTS_FOR_RATE = 3
"""Com poucas tentativas a taxa é ruído; até lá o plano segue elegível."""


def goal_fingerprint(goal: Goal, tools: frozenset[str]) -> str:
    """Assinatura estrutural do objetivo: critérios + tools disponíveis."""
    return _digest(
        "goalfp",
        canonical_json([c.model_dump(mode="json") for c in goal.success_criteria]),
        canonical_json(sorted(tools)),
    )


@dataclass(frozen=True, slots=True)
class CachedPlan:
    plan: Plan
    attempts: int
    successes: int
    fingerprint: str

    @property
    def success_rate(self) -> float:
        if self.attempts == 0:
            return 1.0
        return self.successes / self.attempts


class PlanCache:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lookup(
        self, goal: Goal, tools: frozenset[str], *, now: datetime | None = None
    ) -> CachedPlan | None:
        """Candidato para revalidação, ou None.

        Devolve `None` — em vez de um plano fraco — quando a taxa histórica
        não sustenta a proposta. Reusar um plano que costuma falhar é pior
        que deliberar de novo.
        """
        fingerprint = goal_fingerprint(goal, tools)
        row = await self.session.scalar(
            select(models.PlanCacheEntry).where(
                models.PlanCacheEntry.goal_fingerprint == fingerprint
            )
        )
        if row is None:
            return None

        rate = row.successes / row.attempts if row.attempts else 1.0
        if row.attempts >= MIN_ATTEMPTS_FOR_RATE and rate < MIN_SUCCESS_RATE:
            return None

        if now is not None:
            row.last_used_at = now
            await self.session.flush()

        return CachedPlan(
            plan=_to_plan(row),
            attempts=row.attempts,
            successes=row.successes,
            fingerprint=fingerprint,
        )

    async def record(
        self,
        goal: Goal,
        plan: Plan,
        tools: frozenset[str],
        *,
        succeeded: bool,
        now: datetime | None = None,
    ) -> str:
        """Registra o desfecho de um plano sob a assinatura do objetivo.

        Fracasso também é gravado: é o que faz a taxa cair e o plano parar de
        ser proposto. Guardar só sucesso transformaria o cache numa memória
        que nunca aprende a desconfiar.
        """
        fingerprint = goal_fingerprint(goal, tools)
        row = await self.session.scalar(
            select(models.PlanCacheEntry).where(
                models.PlanCacheEntry.goal_fingerprint == fingerprint
            )
        )
        if row is None:
            row = models.PlanCacheEntry(
                id=uuid4(),
                goal_fingerprint=fingerprint,
                objective=plan.objective,
                completion_condition=plan.completion_condition,
                steps=[s.model_dump(mode="json") for s in plan.steps],
                assumptions=list(plan.assumptions),
                attempts=0,
                successes=0,
            )
            self.session.add(row)
        elif succeeded:
            # Só um plano que funcionou substitui o gabarito guardado.
            row.objective = plan.objective
            row.completion_condition = plan.completion_condition
            row.steps = [s.model_dump(mode="json") for s in plan.steps]
            row.assumptions = list(plan.assumptions)

        row.attempts += 1
        row.successes += 1 if succeeded else 0
        row.last_used_at = now
        await self.session.flush()
        return fingerprint


def _to_plan(row: models.PlanCacheEntry) -> Plan:
    """Reconstrói com os steps zerados: o passado não vem marcado como feito."""
    steps = tuple(
        PlanStep.model_validate({**step, "status": "PENDING"}) for step in row.steps
    )
    return Plan(
        id=uuid4(),
        version=1,
        objective=row.objective,
        steps=steps,
        assumptions=tuple(row.assumptions or []),
        completion_condition=row.completion_condition,
    )
