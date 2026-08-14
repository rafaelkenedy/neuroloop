"""Por que o agente fez isso — TASK-014.

O aceite da task é uma pergunta, não um formato: *por que o agente fez cada
ação?* Responder exige juntar cinco coisas que vivem em tabelas diferentes:

1. a **ação**: tool, argumentos, risco, proveniência (`derived_from`);
2. a **autorização**: o que a policy decidiu e por quê;
3. a **execução**: tentativas, probe, desfecho;
4. a **verificação**: o que foi observado do mundo depois;
5. o **contexto**: qual ciclo, qual estado, quais versões estavam em vigor.

A reconstrução é feita a partir do que já é gravado — não há tabela nova. Se
uma explicação sai incompleta, a lacuna está no que o runtime registra, e
isso é um defeito visível, não uma limitação silenciosa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.observability.redaction import redact
from neuroloop.persistence import models


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    attempt_no: int
    status: str
    duration_ms: int | None
    error_code: str | None
    probe_outcome: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ActionExplanation:
    action_id: UUID
    run_id: UUID
    iteration: int | None
    tool: str
    tool_version: str
    arguments: dict[str, Any]
    risk_level: str
    rationale_code: str
    plan_step_id: str | None
    derived_from: tuple[str, ...]
    approved_by_user: bool
    fingerprint: str
    idempotency_key: str

    authorization_reason: str | None = None
    authorization_error: str | None = None
    tainted: bool = False
    decision_source: str | None = None

    attempts: tuple[AttemptSummary, ...] = ()
    verification: dict[str, Any] | None = None
    episode_summary: str | None = None
    trace_id: str | None = None
    versions: dict[str, Any] = field(default_factory=dict)

    @property
    def executed(self) -> bool:
        return bool(self.attempts)

    def why(self) -> str:
        """Uma frase. O resto do objeto é a evidência por trás dela."""
        origem = self.decision_source or "DELIBERATOR"
        if not self.executed:
            motivo = self.authorization_error or self.authorization_reason or "não executada"
            return (
                f"{self.tool} foi proposta por {origem} ({self.rationale_code}) "
                f"e NÃO executada: {motivo}"
            )
        desfecho = self.attempts[-1].status
        verificado = (
            self.verification.get("expected_outcomes_satisfied")
            if self.verification
            else None
        )
        return (
            f"{self.tool} foi proposta por {origem} ({self.rationale_code}), "
            f"autorizada como {self.authorization_reason}, executada em "
            f"{len(self.attempts)} tentativa(s) com desfecho {desfecho}; "
            f"efeito esperado verificado: {verificado}"
        )


async def explain_action(session: AsyncSession, action_id: UUID) -> ActionExplanation | None:
    row = await session.get(models.Action, action_id)
    if row is None:
        return None

    attempts = list(
        await session.scalars(
            select(models.ActionAttempt)
            .where(models.ActionAttempt.action_id == action_id)
            .order_by(models.ActionAttempt.attempt_no)
        )
    )
    episode = await session.scalar(
        select(models.Episode).where(models.Episode.action_id == action_id)
    )
    # A autorização precisa ser a **desta** ação. Pegar a mais recente do run
    # atribuiria a decisão errada a cada ação — e uma explicação errada é
    # pior que nenhuma.
    eventos = list(
        await session.scalars(
            select(models.RunEvent)
            .where(
                models.RunEvent.run_id == row.run_id,
                models.RunEvent.kind == "ACTION_AUTHORIZATION",
            )
            .order_by(models.RunEvent.at)
        )
    )
    autorizacao = next(
        (e for e in reversed(eventos) if (e.payload or {}).get("action_id") == str(action_id)),
        None,
    )
    payload = (autorizacao.payload or {}) if autorizacao else {}

    return ActionExplanation(
        action_id=row.id,
        run_id=row.run_id,
        iteration=episode.iteration if episode else None,
        tool=row.tool,
        tool_version=row.tool_version,
        arguments=redact(dict(row.arguments or {})),
        risk_level=row.risk_level,
        rationale_code=row.rationale_code,
        plan_step_id=row.plan_step_id,
        derived_from=tuple(row.derived_from or []),
        approved_by_user=bool(row.approved_by_user),
        fingerprint=row.action_fingerprint,
        idempotency_key=row.idempotency_key,
        authorization_reason=autorizacao.reason if autorizacao else None,
        authorization_error=autorizacao.error_code if autorizacao else None,
        tainted=bool(payload.get("tainted", False)),
        decision_source=payload.get("source"),
        attempts=tuple(
            AttemptSummary(
                attempt_no=a.attempt_no,
                status=a.status,
                duration_ms=a.duration_ms,
                error_code=a.error_code,
                probe_outcome=a.probe_outcome,
            )
            for a in attempts
        ),
        verification=redact(episode.verification) if episode else None,
        episode_summary=episode.observation_summary if episode else None,
        trace_id=autorizacao.trace_id if autorizacao else None,
        versions={k: v for k, v in payload.items() if k.startswith("v_")},
    )


async def explain_run(session: AsyncSession, run_id: UUID) -> list[ActionExplanation]:
    """Todas as ações do run, na ordem em que foram propostas."""
    ids = list(
        await session.scalars(
            select(models.Action.id)
            .where(models.Action.run_id == run_id)
            .order_by(models.Action.created_at)
        )
    )
    explicacoes = [await explain_action(session, action_id) for action_id in ids]
    return [e for e in explicacoes if e is not None]


@dataclass(frozen=True, slots=True)
class RunTimelineEntry:
    at: datetime
    kind: str
    detail: str


async def run_timeline(session: AsyncSession, run_id: UUID) -> list[RunTimelineEntry]:
    """Linha do tempo legível: transições, autorizações e spans."""
    events = list(
        await session.scalars(
            select(models.RunEvent)
            .where(models.RunEvent.run_id == run_id)
            .order_by(models.RunEvent.at, models.RunEvent.iteration)
        )
    )
    linhas: list[RunTimelineEntry] = []
    for event in events:
        if event.kind == "PHASE_TRANSITION":
            detalhe = f"{event.from_phase} → {event.to_phase}: {event.reason}"
        elif event.kind == "ACTION_AUTHORIZATION":
            detalhe = f"{(event.payload or {}).get('tool')}: {event.reason}"
        elif event.kind.startswith("SPAN:"):
            detalhe = f"{(event.payload or {}).get('duration_ms')}ms"
        else:
            detalhe = event.reason or ""
        linhas.append(RunTimelineEntry(at=event.at, kind=event.kind, detail=detalhe))
    return linhas
