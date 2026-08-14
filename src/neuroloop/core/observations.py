"""Observation — saída normalizada da camada de percepção (spec §24)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from neuroloop.core.enums import ObservationSource, TrustLevel


class Observation(BaseModel):
    """Toda entrada no sistema — mensagem de usuário, resultado de tool,
    evento de sistema, resultado de probe — vira uma Observation.

    ``trust`` não é decorativo: alimenta a propagação de taint no
    PolicyEngine (correção C10) e penaliza salience no WorkspaceBuilder.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    source: ObservationSource
    source_ref: str | None = None
    kind: str
    content: Any = None
    content_hash: str
    trust: TrustLevel
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: tuple[str, ...] = ()
    occurred_at: datetime
    received_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_untrusted(self) -> bool:
        return self.trust is TrustLevel.UNTRUSTED_EXTERNAL
