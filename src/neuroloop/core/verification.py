"""VerificationResult — quatro níveis de verificação (spec §15, correção C02).

Este módulo carrega as invariantes que definem o valor do sistema. Elas são
impostas no schema, não em comentário, para que nenhum caminho de código
consiga produzir um `COMPLETED` sem evidência externa observada.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.criteria import CriterionOutcome, Observes
from neuroloop.core.enums import ErrorCode, ExecutionStatus, NextAction

VerificationLevel = Literal["EXECUTION", "STATE", "GOAL", "SAFETY"]


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: VerificationLevel
    observes: Observes
    outcome: CriterionOutcome
    source: str = Field(min_length=1)
    """Como a evidência foi obtida: probe, releitura de arquivo, schema, etc."""


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_status: ExecutionStatus
    expected_outcomes_satisfied: bool | None = None
    goal_satisfied: bool = False
    safety_ok: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[VerificationEvidence, ...] = ()
    reward_signal: float = Field(default=0.0, ge=-1.0, le=1.0)
    """V0: telemetria, não mecanismo de treinamento (spec §10)."""
    error_code: ErrorCode | None = None
    next_action: NextAction

    @property
    def goal_evidence(self) -> tuple[VerificationEvidence, ...]:
        return tuple(e for e in self.evidence if e.level == "GOAL")

    @model_validator(mode="after")
    def _enforce_completion_invariants(self) -> VerificationResult:
        goal_ev = self.goal_evidence

        if self.goal_satisfied:
            # C02: só evidência externa observada conclui um goal.
            if not goal_ev:
                raise ValueError(
                    f"{ErrorCode.VERIFICATION_ERROR.value}: goal_satisfied exige ao menos "
                    "uma evidência de nível GOAL"
                )
            if any(e.observes != "EXTERNAL_STATE" for e in goal_ev):
                raise ValueError(
                    f"{ErrorCode.VERIFICATION_ERROR.value}: goal_satisfied exige evidência "
                    "EXTERNAL_STATE; auto-relato de tool não conclui goal"
                )
            # C01: INDETERMINATE nunca conta como sucesso.
            if any(e.outcome.satisfied is not True for e in goal_ev):
                raise ValueError(
                    f"{ErrorCode.INDETERMINATE_VERIFICATION.value}: goal_satisfied exige "
                    "todos os critérios de goal observados e satisfeitos"
                )
            if not self.safety_ok:
                raise ValueError(
                    f"{ErrorCode.VERIFICATION_ERROR.value}: goal_satisfied incompatível "
                    "com safety_ok=False"
                )

        if (self.next_action is NextAction.GOAL_COMPLETED) != self.goal_satisfied:
            raise ValueError(
                f"{ErrorCode.VERIFICATION_ERROR.value}: next_action=GOAL_COMPLETED e "
                "goal_satisfied precisam concordar"
            )

        if not self.safety_ok and self.next_action in _SAFE_ONLY_ACTIONS:
            raise ValueError(
                f"{ErrorCode.VERIFICATION_ERROR.value}: safety_ok=False não permite "
                f"next_action={self.next_action.value}"
            )

        # Efeito desconhecido não pode seguir adiante sem resolução (C05).
        if self.execution_status is ExecutionStatus.UNKNOWN and self.next_action in (
            NextAction.CONTINUE,
            NextAction.GOAL_COMPLETED,
        ):
            raise ValueError(
                f"{ErrorCode.UNKNOWN_SIDE_EFFECT.value}: execution_status=UNKNOWN exige "
                "recuperação, retry provado seguro ou intervenção humana"
            )

        return self


_SAFE_ONLY_ACTIONS = frozenset({NextAction.CONTINUE, NextAction.GOAL_COMPLETED})
