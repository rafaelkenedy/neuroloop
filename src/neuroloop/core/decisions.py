"""Decisões do Deliberator — structured output do LLM (spec §11).

Nenhuma decisão crítica depende de parsing de texto livre. A união é
discriminada por ``type`` e validada antes de qualquer efeito.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.actions import ActionProposal, UserInputRequest
from neuroloop.core.enums import ErrorCode
from neuroloop.core.plans import Plan


class _DecisionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(min_length=1)
    """Telemetria. Não é chain-of-thought e não é persistido como raciocínio."""


class ActDecision(_DecisionBase):
    type: Literal["ACT"] = "ACT"
    source: Literal["FAST_PATH", "DELIBERATOR"] = "DELIBERATOR"
    action: ActionProposal

    @model_validator(mode="after")
    def _require_provenance(self) -> ActDecision:
        """Correção C10: ação com argumentos precisa declarar de onde vieram.

        Fast Path é isento: seus argumentos vêm de um template versionado ou
        de um PlanStep já materializado, cuja proveniência foi registrada no
        ciclo que gerou o plano.
        """
        if (
            self.source == "DELIBERATOR"
            and self.action.arguments
            and not self.action.derived_from
        ):
            raise ValueError(
                f"{ErrorCode.TOOL_VALIDATION_ERROR.value}: ação com argumentos precisa "
                "declarar derived_from (proveniência das observações)"
            )
        return self


class PlanDecision(_DecisionBase):
    type: Literal["PLAN"] = "PLAN"
    plan: Plan


class AskUserDecision(_DecisionBase):
    type: Literal["ASK_USER"] = "ASK_USER"
    request: UserInputRequest


class ImpossibleDecision(_DecisionBase):
    type: Literal["IMPOSSIBLE"] = "IMPOSSIBLE"
    evidence: tuple[str, ...] = Field(min_length=1)
    """Impossibilidade precisa ser justificada com evidência, não asserida."""


Decision = Annotated[
    ActDecision | PlanDecision | AskUserDecision | ImpossibleDecision,
    Field(discriminator="type"),
]
