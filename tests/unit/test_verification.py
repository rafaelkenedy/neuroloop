"""Invariantes que impedem falso sucesso (spec §15, correções C01/C02/C05)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factories import make_outcome
from neuroloop.core import (
    ErrorCode,
    ExecutionStatus,
    NextAction,
    VerificationEvidence,
    VerificationResult,
)


def evidence(
    satisfied: bool | None = True,
    level: str = "GOAL",
    observes: str = "EXTERNAL_STATE",
) -> VerificationEvidence:
    return VerificationEvidence(
        level=level,
        observes=observes,
        outcome=make_outcome(satisfied),
        source="releitura do artefato",
    )


def result(**overrides) -> VerificationResult:
    defaults = dict(
        execution_status=ExecutionStatus.SUCCESS,
        confidence=0.9,
        next_action=NextAction.CONTINUE,
    )
    defaults.update(overrides)
    return VerificationResult(**defaults)


class TestConclusaoDeGoal:
    def test_conclusao_valida(self):
        r = result(
            goal_satisfied=True,
            next_action=NextAction.GOAL_COMPLETED,
            evidence=(evidence(),),
        )
        assert r.goal_satisfied is True

    def test_conclusao_sem_evidencia_de_goal_rejeitada(self):
        with pytest.raises(ValidationError) as exc:
            result(goal_satisfied=True, next_action=NextAction.GOAL_COMPLETED)
        assert ErrorCode.VERIFICATION_ERROR.value in str(exc.value)

    def test_conclusao_por_auto_relato_rejeitada(self):
        """B2: a tool reportar sucesso não conclui o goal."""
        with pytest.raises(ValidationError) as exc:
            result(
                goal_satisfied=True,
                next_action=NextAction.GOAL_COMPLETED,
                evidence=(evidence(observes="ACTION_RESULT"),),
            )
        assert "EXTERNAL_STATE" in str(exc.value)

    def test_conclusao_com_criterio_indeterminado_rejeitada(self):
        with pytest.raises(ValidationError) as exc:
            result(
                goal_satisfied=True,
                next_action=NextAction.GOAL_COMPLETED,
                evidence=(evidence(satisfied=True), evidence(satisfied=None)),
            )
        assert ErrorCode.INDETERMINATE_VERIFICATION.value in str(exc.value)

    def test_conclusao_com_criterio_refutado_rejeitada(self):
        with pytest.raises(ValidationError):
            result(
                goal_satisfied=True,
                next_action=NextAction.GOAL_COMPLETED,
                evidence=(evidence(satisfied=False),),
            )

    def test_next_action_e_goal_satisfied_precisam_concordar(self):
        with pytest.raises(ValidationError):
            result(goal_satisfied=True, next_action=NextAction.CONTINUE, evidence=(evidence(),))
        with pytest.raises(ValidationError):
            result(goal_satisfied=False, next_action=NextAction.GOAL_COMPLETED)


class TestSeguranca:
    def test_falha_de_safety_bloqueia_conclusao(self):
        with pytest.raises(ValidationError):
            result(
                goal_satisfied=True,
                safety_ok=False,
                next_action=NextAction.GOAL_COMPLETED,
                evidence=(evidence(),),
            )

    def test_falha_de_safety_bloqueia_continue(self):
        with pytest.raises(ValidationError) as exc:
            result(safety_ok=False, next_action=NextAction.CONTINUE)
        assert ErrorCode.VERIFICATION_ERROR.value in str(exc.value)

    def test_falha_de_safety_permite_parar(self):
        r = result(
            safety_ok=False,
            next_action=NextAction.STOP_FAILURE,
            error_code=ErrorCode.PROMPT_INJECTION,
        )
        assert r.next_action is NextAction.STOP_FAILURE


class TestEfeitoDesconhecido:
    """Correção C05: UNKNOWN_EFFECT não pode seguir adiante sem resolver."""

    @pytest.mark.parametrize(
        "proibido", [NextAction.CONTINUE, NextAction.GOAL_COMPLETED]
    )
    def test_unknown_nao_avanca(self, proibido):
        kwargs = {"execution_status": ExecutionStatus.UNKNOWN, "next_action": proibido}
        if proibido is NextAction.GOAL_COMPLETED:
            kwargs |= {"goal_satisfied": True, "evidence": (evidence(),)}
        with pytest.raises(ValidationError):
            result(**kwargs)

    @pytest.mark.parametrize(
        "permitido",
        [NextAction.ASK_USER, NextAction.RETRY, NextAction.REPLAN, NextAction.STOP_FAILURE],
    )
    def test_unknown_permite_resolucao(self, permitido):
        r = result(execution_status=ExecutionStatus.UNKNOWN, next_action=permitido)
        assert r.execution_status is ExecutionStatus.UNKNOWN


class TestFaixas:
    def test_confianca_fora_da_faixa(self):
        with pytest.raises(ValidationError):
            result(confidence=1.2)

    def test_reward_fora_da_faixa(self):
        with pytest.raises(ValidationError):
            result(reward_signal=2.0)

    def test_reward_negativo_valido(self):
        assert result(reward_signal=-1.0).reward_signal == -1.0
