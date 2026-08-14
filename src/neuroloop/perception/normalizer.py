"""Percepção — TASK-012 (spec §19).

Toda entrada vira uma `Observation`: mensagem do usuário, resultado de tool,
evento de sistema, resposta de probe. O ponto do módulo não é o formato
comum — é **atribuir confiança na entrada**, uma vez, no lugar certo.

A regra de confiança não é heurística: a tool **declara** se o resultado
carrega conteúdo de fora (`returns_external_content`). Ler um arquivo traz
conteúdo do mundo e é `UNTRUSTED_EXTERNAL`; `bytes_written` é metadado que a
própria ferramenta produziu e é `TRUSTED_INTERNAL`. Deixar isso a cargo de
adivinhação é como se perde a fronteira de C10 sem ninguém notar.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from neuroloop.core.enums import ObservationSource, TrustLevel
from neuroloop.core.goals import Goal
from neuroloop.core.identity import canonical_json
from neuroloop.core.observations import Observation
from neuroloop.tools.definitions import ToolDefinition


def content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()[:32]}"


class PerceptionNormalizer:
    """Converte entradas heterogêneas em `Observation`."""

    def from_goal(self, goal: Goal, *, run_id: UUID, now: datetime) -> Observation:
        """O objetivo é a primeira observação do run (spec §28, passo 2)."""
        payload = {
            "description": goal.description,
            "success_criteria": [c.model_dump(mode="json") for c in goal.success_criteria],
        }
        return Observation(
            id=uuid4(),
            run_id=run_id,
            source=ObservationSource.USER,
            source_ref=str(goal.id),
            kind="goal",
            content=payload,
            content_hash=content_hash(payload),
            trust=TrustLevel.USER,
            tags=tuple(_resource_tags(goal)),
            occurred_at=goal.created_at,
            received_at=now,
        )

    def from_tool_result(
        self,
        *,
        run_id: UUID,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        output: Any,
        action_id: UUID,
        now: datetime,
        succeeded: bool = True,
    ) -> Observation:
        trust = (
            TrustLevel.UNTRUSTED_EXTERNAL
            if definition.returns_external_content and succeeded
            else TrustLevel.TRUSTED_INTERNAL
        )
        target = arguments.get("path") or arguments.get("url")
        tags = [f"tool:{definition.name}", f"outcome:{'SUCCESS' if succeeded else 'FAILURE'}"]
        if isinstance(target, str):
            tags.append(f"resource:{target}")

        return Observation(
            id=uuid4(),
            run_id=run_id,
            source=ObservationSource.TOOL,
            source_ref=str(target or definition.name),
            kind="tool_result" if succeeded else "tool_error",
            content=output,
            content_hash=content_hash(output),
            trust=trust,
            confidence=1.0 if succeeded else 0.5,
            tags=tuple(tags),
            occurred_at=now,
            received_at=now,
            metadata={"action_id": str(action_id), "tool_version": definition.version},
        )

    def from_probe(
        self, *, run_id: UUID, action_id: UUID, outcome: Any, now: datetime
    ) -> Observation:
        """Resultado de probe é observação de recuperação (spec §24)."""
        payload = outcome if isinstance(outcome, dict) else {"result": outcome}
        return Observation(
            id=uuid4(),
            run_id=run_id,
            source=ObservationSource.RECOVERY,
            source_ref=str(action_id),
            kind="recovery_probe",
            content=payload,
            content_hash=content_hash(payload),
            trust=TrustLevel.TRUSTED_INTERNAL,
            tags=("recovery",),
            occurred_at=now,
            received_at=now,
        )

    def from_user(
        self, *, run_id: UUID, message: str, now: datetime, kind: str = "user_message"
    ) -> Observation:
        return Observation(
            id=uuid4(),
            run_id=run_id,
            source=ObservationSource.USER,
            kind=kind,
            content=message,
            content_hash=content_hash(message),
            trust=TrustLevel.USER,
            occurred_at=now,
            received_at=now,
        )


def _resource_tags(goal: Goal) -> list[str]:
    tags: list[str] = []
    for criterion in goal.success_criteria:
        for attr in ("path", "url"):
            value = getattr(criterion, attr, None)
            if isinstance(value, str):
                tags.append(f"resource:{value}")
    return tags
