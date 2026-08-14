"""Atenção — TASK-009 (spec §20, correção C10).

A fórmula da spec ganhou um termo negativo: conteúdo `UNTRUSTED_EXTERNAL`
**perde** salience em vez de ganhar.

O motivo é adversarial. Sem a penalidade, um atacante controla dois dos
termos de maior peso: escreve texto que parece relevante ao objetivo
(`goal_relevance`) e que nunca foi visto antes (`novelty`). O resultado é
que o jeito mais barato de dominar o contexto do agente passa a ser plantar
um arquivo — e a atenção vira superfície de ataque.

    salience = 0.35*goal_relevance + 0.25*risk + 0.20*recency
             + 0.10*novelty + 0.10*unresolved
             - 0.15*(trust == UNTRUSTED_EXTERNAL)

A penalidade não exclui o dado: um arquivo lido é a informação que o agente
precisa. Ela só impede que ele suba a fila por conta própria.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from neuroloop.core.enums import TrustLevel
from neuroloop.core.observations import Observation

WEIGHT_GOAL_RELEVANCE = 0.35
WEIGHT_RISK = 0.25
WEIGHT_RECENCY = 0.20
WEIGHT_NOVELTY = 0.10
WEIGHT_UNRESOLVED = 0.10
PENALTY_UNTRUSTED = 0.15

RECENCY_HALF_LIFE_SECONDS = 300.0
"""Observação de cinco minutos atrás vale metade. Ciclo é curto."""

_RISK_KINDS = frozenset({"tool_error", "verification_failure", "recovery_probe"})


@dataclass(frozen=True, slots=True)
class SalienceInputs:
    """Sinais medidos pelo builder. Manter a fórmula pura a torna testável."""

    goal_relevance: float
    risk: float
    recency: float
    novelty: float
    unresolved: float
    untrusted: bool

    def __post_init__(self) -> None:
        for name in ("goal_relevance", "risk", "recency", "novelty", "unresolved"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} precisa estar em [0, 1], recebeu {value}")


def salience(inputs: SalienceInputs) -> float:
    """Saturado em [0, 1]: penalidade não produz score negativo."""
    score = (
        WEIGHT_GOAL_RELEVANCE * inputs.goal_relevance
        + WEIGHT_RISK * inputs.risk
        + WEIGHT_RECENCY * inputs.recency
        + WEIGHT_NOVELTY * inputs.novelty
        + WEIGHT_UNRESOLVED * inputs.unresolved
        - PENALTY_UNTRUSTED * float(inputs.untrusted)
    )
    return round(min(max(score, 0.0), 1.0), 4)


def measure(
    observation: Observation,
    *,
    goal_tags: frozenset[str],
    now: datetime,
    seen_hashes: frozenset[str] = frozenset(),
    unresolved_refs: frozenset[str] = frozenset(),
) -> SalienceInputs:
    """Traduz uma observação nos cinco sinais da fórmula."""
    tags = frozenset(observation.tags)
    return SalienceInputs(
        goal_relevance=_overlap(tags, goal_tags),
        risk=1.0 if observation.kind in _RISK_KINDS else 0.0,
        recency=_recency(observation.received_at, now),
        novelty=0.0 if observation.content_hash in seen_hashes else 1.0,
        unresolved=1.0 if (observation.source_ref or "") in unresolved_refs else 0.0,
        untrusted=observation.trust is TrustLevel.UNTRUSTED_EXTERNAL,
    )


def score_observation(
    observation: Observation,
    *,
    goal_tags: frozenset[str],
    now: datetime,
    seen_hashes: frozenset[str] = frozenset(),
    unresolved_refs: frozenset[str] = frozenset(),
) -> float:
    return salience(
        measure(
            observation,
            goal_tags=goal_tags,
            now=now,
            seen_hashes=seen_hashes,
            unresolved_refs=unresolved_refs,
        )
    )


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _recency(received_at: datetime, now: datetime) -> float:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    age = max((now - received_at).total_seconds(), 0.0)
    return math.pow(0.5, age / RECENCY_HALF_LIFE_SECONDS)
