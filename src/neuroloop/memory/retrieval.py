"""Retrieval episódico top-k — TASK-008 (spec §16, correção C14).

Sem embeddings, por decisão da V0. A hipótese a testar é se match
estrutural + importância + recência já resolve; o gap §38.1 continua aberto
e pgvector só entra se o benchmark justificar.

**Onde cada coisa acontece.** O pré-filtro é SQL — recorte por run, tool,
código de erro e uma janela de recência, tudo sobre colunas indexadas. O
ranking final é Python, porque comparar tags exigiria containment em JSON e
não existe forma portátil disso entre PostgreSQL (`jsonb ?|` com GIN) e
SQLite (`json_each`). Trazer uma janela limitada e ordenar em memória custa
pouco e mantém o mesmo comportamento nos dois bancos.

Fórmula (C14):

    score = 0.5 * match_estrutural + 0.3 * importance + 0.2 * recency_decay
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.enums import ErrorCode
from neuroloop.persistence import models

DEFAULT_LIMIT = 5
"""Spec §16: top-k = 5. Contexto é recurso escasso, não repositório."""

CANDIDATE_WINDOW = 200
"""Quantos episódios recentes entram no ranking. Corta o full scan."""

RECENCY_HALF_LIFE_HOURS = 72.0

WEIGHT_STRUCTURAL = 0.5
WEIGHT_IMPORTANCE = 0.3
WEIGHT_RECENCY = 0.2

MIN_SCORE = 0.2
"""Abaixo disso o episódio é ruído: entra no prompt sem ajudar."""


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """O que torna um episódio passado relevante para o ciclo atual."""

    tags: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    error_codes: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    exclude_run_id: UUID | None = None
    """O run corrente: reler os próprios episódios não é reuso."""
    limit: int = DEFAULT_LIMIT
    min_score: float = MIN_SCORE

    @property
    def is_empty(self) -> bool:
        return not (self.tags or self.tools or self.error_codes or self.resources)


@dataclass(frozen=True, slots=True)
class EpisodeMemory:
    """Projeção de episódio pronta para o `WorkingContext`."""

    episode_id: UUID
    run_id: UUID
    iteration: int
    goal_summary: str
    observation_summary: str
    result_summary: str
    decision_type: str
    tool_name: str | None
    error_code: str | None
    importance: float
    tags: tuple[str, ...]
    created_at: datetime
    score: float = 0.0
    structural_match: float = 0.0
    match_reasons: tuple[str, ...] = field(default=())


class MemoryRetriever:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def retrieve(
        self, query: RetrievalQuery, *, now: datetime | None = None
    ) -> list[EpisodeMemory]:
        if query.is_empty:
            # Sem sinal de relevância, o top-k viraria "os mais recentes",
            # que é ruído com aparência de memória.
            return []

        now = now or datetime.now(UTC)
        candidates = await self._candidates(query)
        scored = [
            memory
            for memory in (self._score(row, query, now) for row in candidates)
            # Afinidade estrutural é porta, não apenas peso: sem nenhuma
            # dimensão em comum, importância e recência sozinhas fariam um
            # episódio irrelevante entrar no contexto só por ser recente.
            if memory.structural_match > 0.0 and memory.score >= query.min_score
        ]
        scored.sort(key=lambda m: (-m.score, -m.importance, m.created_at))
        return scored[: query.limit]

    async def _candidates(self, query: RetrievalQuery) -> list[models.Episode]:
        stmt = select(models.Episode)
        if query.exclude_run_id is not None:
            stmt = stmt.where(models.Episode.run_id != query.exclude_run_id)
        stmt = stmt.order_by(models.Episode.created_at.desc()).limit(CANDIDATE_WINDOW)
        return list(await self.session.scalars(stmt))

    def _score(
        self, row: models.Episode, query: RetrievalQuery, now: datetime
    ) -> EpisodeMemory:
        tags = tuple(row.tags or [])
        structural, reasons = _structural_match(row, tags, query)
        recency = _recency_decay(row.created_at, now)
        score = (
            WEIGHT_STRUCTURAL * structural
            + WEIGHT_IMPORTANCE * row.importance
            + WEIGHT_RECENCY * recency
        )
        return EpisodeMemory(
            episode_id=row.id,
            run_id=row.run_id,
            iteration=row.iteration,
            goal_summary=row.goal_summary,
            observation_summary=row.observation_summary,
            result_summary=row.result_summary,
            decision_type=row.decision_type,
            tool_name=row.tool_name,
            error_code=row.error_code,
            importance=row.importance,
            tags=tags,
            created_at=row.created_at,
            score=round(score, 4),
            structural_match=round(structural, 4),
            match_reasons=reasons,
        )


def _structural_match(
    row: models.Episode, tags: tuple[str, ...], query: RetrievalQuery
) -> tuple[float, tuple[str, ...]]:
    """Média ponderada apenas sobre as dimensões que a query especificou.

    Normalizar pelo que foi perguntado evita que uma query sem `error_code`
    teto o score de todo mundo — o que faria episódios de falha nunca
    competirem com episódios comuns.
    """
    parts: list[tuple[float, float]] = []
    reasons: list[str] = []

    if query.tags:
        overlap = _jaccard(frozenset(tags), query.tags)
        parts.append((0.5, overlap))
        if overlap:
            reasons.append(f"tags:{overlap:.2f}")

    if query.tools:
        hit = float(row.tool_name in query.tools)
        parts.append((0.2, hit))
        if hit:
            reasons.append(f"tool:{row.tool_name}")

    if query.error_codes:
        hit = float(row.error_code in query.error_codes)
        parts.append((0.2, hit))
        if hit:
            reasons.append(f"error:{row.error_code}")

    if query.resources:
        recursos = {t.split(":", 1)[1] for t in tags if t.startswith("resource:")}
        overlap = _jaccard(frozenset(recursos), query.resources)
        parts.append((0.1, overlap))
        if overlap:
            reasons.append(f"resource:{overlap:.2f}")

    if not parts:
        return 0.0, ()
    total_weight = sum(weight for weight, _ in parts)
    return sum(weight * value for weight, value in parts) / total_weight, tuple(reasons)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _recency_decay(created_at: datetime, now: datetime) -> float:
    """Decaimento exponencial: episódio velho não deixa de valer, só pesa menos."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_hours = max((now - created_at).total_seconds() / 3600.0, 0.0)
    return math.pow(0.5, age_hours / RECENCY_HALF_LIFE_HOURS)


def query_from_context(
    *,
    run_id: UUID | None = None,
    tools: tuple[str, ...] = (),
    error_codes: tuple[ErrorCode, ...] = (),
    resources: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    limit: int = DEFAULT_LIMIT,
) -> RetrievalQuery:
    """Monta a query a partir do que o ciclo atual sabe.

    Recursos entram também como tag, porque é assim que estão gravados no
    episódio — manter as duas formas evita que o chamador precise conhecer o
    vocabulário interno.
    """
    resource_tags = tuple(f"resource:{r}" for r in resources)
    tool_tags = tuple(f"tool:{t}" for t in tools)
    return RetrievalQuery(
        tags=frozenset(tags + resource_tags + tool_tags),
        tools=frozenset(tools),
        error_codes=frozenset(e.value for e in error_codes),
        resources=frozenset(resources),
        exclude_run_id=run_id,
        limit=limit,
    )
