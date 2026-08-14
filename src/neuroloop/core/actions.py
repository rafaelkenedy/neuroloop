"""ActionProposal e pedidos de intervenção humana (spec §24)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from neuroloop.core.criteria import Criterion


class ActionProposal(BaseModel):
    """Ação proposta, ainda não autorizada nem executada.

    ``derived_from`` carrega a proveniência dos argumentos (correção C10):
    são os ids das Observations que os originaram. O PolicyEngine usa isso
    para propagar taint — argumento derivado de conteúdo
    ``UNTRUSTED_EXTERNAL`` não pode disparar ação de risco elevado sem
    aprovação.

    O campo é opcional aqui porque templates de skill são escritos à mão e
    só ganham proveniência ao serem materializados. Para ações vindas do
    LLM a exigência é imposta em `ActDecision`.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_outcomes: tuple[Criterion, ...] = Field(min_length=1)
    rationale_code: str = Field(min_length=1)
    """Telemetria, não chain-of-thought (spec §11)."""
    timeout_seconds: float | None = Field(default=None, gt=0)
    derived_from: tuple[UUID, ...] = ()


class UserInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["MISSING_INFORMATION", "APPROVAL", "AMBIGUOUS_EFFECT"]
    message: str = Field(min_length=1)
    required_fields: tuple[str, ...] = ()
    action_id: UUID | None = None
    action_fingerprint: str | None = None
    """Vincula a aprovação a argumentos específicos (correção C19)."""
