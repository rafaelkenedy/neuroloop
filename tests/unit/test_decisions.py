"""Correção C10: proveniência obrigatória em ação proposta pelo LLM."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from neuroloop.core import (
    ActDecision,
    ActionProposal,
    AskUserDecision,
    Decision,
    ErrorCode,
    FileExists,
    ImpossibleDecision,
    UserInputRequest,
)

decision_adapter: TypeAdapter[Decision] = TypeAdapter(Decision)


def proposal(**kwargs) -> ActionProposal:
    defaults = dict(
        tool="filesystem.write",
        arguments={"path": "/workspace/out.json", "content": "[]"},
        expected_outcomes=(FileExists(path="/workspace/out.json"),),
        rationale_code="WRITE_ARTIFACT",
    )
    defaults.update(kwargs)
    return ActionProposal(**defaults)


class TestProveniencia:
    def test_deliberator_sem_derived_from_e_rejeitado(self):
        with pytest.raises(ValidationError) as exc:
            ActDecision(action=proposal(), reason_code="STEP_READY")
        assert ErrorCode.TOOL_VALIDATION_ERROR.value in str(exc.value)

    def test_deliberator_com_derived_from_e_aceito(self):
        decision = ActDecision(
            action=proposal(derived_from=(uuid4(),)),
            reason_code="STEP_READY",
        )
        assert decision.action.derived_from

    def test_fast_path_e_isento(self):
        """Argumentos de skill/step versionado já têm proveniência registrada."""
        decision = ActDecision(
            source="FAST_PATH", action=proposal(), reason_code="SKILL_MATCH"
        )
        assert decision.source == "FAST_PATH"

    def test_acao_sem_argumentos_nao_exige_proveniencia(self):
        decision = ActDecision(
            action=proposal(tool="search.status", arguments={}),
            reason_code="PROBE",
        )
        assert decision.action.arguments == {}


class TestUniaoDeDecisoes:
    def test_discrimina_por_type(self):
        parsed = decision_adapter.validate_python(
            {
                "type": "ASK_USER",
                "reason_code": "MISSING_PATH",
                "request": {"type": "MISSING_INFORMATION", "message": "qual arquivo?"},
            }
        )
        assert isinstance(parsed, AskUserDecision)

    def test_type_desconhecido_rejeitado(self):
        with pytest.raises(ValidationError):
            decision_adapter.validate_python({"type": "TALVEZ", "reason_code": "x"})

    def test_impossivel_exige_evidencia(self):
        with pytest.raises(ValidationError):
            ImpossibleDecision(evidence=(), reason_code="NO_TOOL")
        assert ImpossibleDecision(
            evidence=("nenhuma tool acessa esse recurso",), reason_code="NO_TOOL"
        ).evidence

    def test_reason_code_obrigatorio(self):
        with pytest.raises(ValidationError):
            ActDecision(action=proposal(derived_from=(uuid4(),)), reason_code="")


class TestActionProposal:
    def test_expected_outcomes_obrigatorio(self):
        """Ação sem resultado esperado não é verificável."""
        with pytest.raises(ValidationError):
            proposal(expected_outcomes=())

    def test_timeout_precisa_ser_positivo(self):
        with pytest.raises(ValidationError):
            proposal(timeout_seconds=0)

    def test_pedido_de_aprovacao_carrega_fingerprint(self):
        req = UserInputRequest(
            type="APPROVAL",
            message="autorizar POST?",
            action_id=uuid4(),
            action_fingerprint="sha256:abc",
        )
        assert req.action_fingerprint == "sha256:abc"
