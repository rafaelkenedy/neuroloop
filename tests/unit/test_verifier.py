"""TASK-007. Os quatro níveis e a regra que separa tool success de goal success."""

from __future__ import annotations

import json

import pytest

from factories import make_checkpoint, make_goal, make_outcome
from neuroloop.core import (
    ErrorCode,
    ExecutionStatus,
    FileExists,
    JsonPathCount,
    JsonPathEquals,
    NextAction,
)
from neuroloop.tools import Sandbox
from neuroloop.verification import EvaluationContext, ExecutionReport, Verifier


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    return Sandbox(root)


@pytest.fixture
def ctx(sandbox) -> EvaluationContext:
    return EvaluationContext(sandbox=sandbox)


@pytest.fixture
def verifier() -> Verifier:
    return Verifier()


def artefato(sandbox, registros: int = 3) -> None:
    (sandbox.root / "out.json").write_text(
        json.dumps([{"id": i} for i in range(registros)]), encoding="utf-8"
    )


def goal_de_arquivo(**overrides):
    defaults = dict(success_criteria=(FileExists(path="out.json"),))
    defaults.update(overrides)
    return make_goal(**defaults)


def checkpoint_com_baseline(satisfeito: bool | None = False, **overrides):
    return make_checkpoint(
        baseline_outcomes=(make_outcome(satisfeito),), iteration=1, **overrides
    )


def report(status=ExecutionStatus.SUCCESS, **overrides) -> ExecutionReport:
    return ExecutionReport(status=status, **overrides)


class TestNivelDeEvidencia:
    async def test_execucao_sempre_gera_evidencia(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(), checkpoint=checkpoint_com_baseline(), report=report(), ctx=ctx
        )
        execucao = [e for e in result.evidence if e.level == "EXECUTION"]
        assert len(execucao) == 1
        assert execucao[0].observes == "ACTION_RESULT"

    async def test_evidencia_de_goal_e_sempre_externa(self, verifier, ctx, sandbox):
        artefato(sandbox)
        result = await verifier.evaluate(
            goal=goal_de_arquivo(), checkpoint=checkpoint_com_baseline(), report=report(), ctx=ctx
        )
        goal_evidence = [e for e in result.evidence if e.level == "GOAL"]
        assert goal_evidence
        assert all(e.observes == "EXTERNAL_STATE" for e in goal_evidence)

    async def test_estado_nao_e_sondado_apos_falha_definitiva(self, verifier, ctx):
        """Sondar o mundo depois de falha permanente é custo sem informação."""
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(
                ExecutionStatus.FAILURE, error_code=ErrorCode.TOOL_PERMANENT_ERROR
            ),
            expected_outcomes=(FileExists(path="out.json"),),
            ctx=ctx,
        )
        assert [e for e in result.evidence if e.level == "STATE"] == []
        assert result.expected_outcomes_satisfied is None

    async def test_confianca_cai_com_evidencia_nao_observada(self, verifier, ctx):
        """Toda evidência é determinística; o que varia é quanto se coletou."""
        result = await verifier.evaluate(
            goal=goal_de_arquivo(
                success_criteria=(FileExists(path="../fora.json"),)
            ),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            ctx=ctx,
        )
        assert result.confidence < 1.0


class TestConclusaoDeGoal:
    async def test_delta_conclui(self, verifier, ctx, sandbox):
        artefato(sandbox)
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(satisfeito=False),
            report=report(),
            ctx=ctx,
        )
        assert result.goal_satisfied is True
        assert result.next_action is NextAction.GOAL_COMPLETED
        assert result.reward_signal == 1.0

    async def test_sem_delta_pede_confirmacao(self, verifier, ctx, sandbox):
        """C02: já estava satisfeito antes do run — não foi este run que fez."""
        artefato(sandbox)
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(satisfeito=True),
            report=report(),
            ctx=ctx,
        )
        assert result.goal_satisfied is False
        assert result.next_action is NextAction.ASK_USER
        assert result.error_code is ErrorCode.GOAL_PRE_SATISFIED

    async def test_criterio_nao_satisfeito_continua(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            ctx=ctx,
        )
        assert result.goal_satisfied is False
        assert result.next_action is NextAction.CONTINUE

    async def test_criterio_indeterminado_nao_conclui(self, verifier, ctx):
        """Fora do sandbox: não observável. INDETERMINATE nunca é sucesso."""
        result = await verifier.evaluate(
            goal=goal_de_arquivo(success_criteria=(FileExists(path="../fora.json"),)),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            ctx=ctx,
        )
        assert result.goal_satisfied is False
        assert result.next_action is not NextAction.GOAL_COMPLETED

    async def test_goal_composto_exige_todos_os_criterios(self, verifier, ctx, sandbox):
        artefato(sandbox, registros=2)
        goal = goal_de_arquivo(
            success_criteria=(
                FileExists(path="out.json"),
                JsonPathCount(
                    source="FILE", path="out.json", json_path="$[*]", expected_count=3
                ),
            )
        )
        checkpoint = make_checkpoint(
            baseline_outcomes=(make_outcome(False), make_outcome(False)), iteration=1
        )
        result = await verifier.evaluate(
            goal=goal, checkpoint=checkpoint, report=report(), ctx=ctx
        )
        assert result.goal_satisfied is False


class TestSafety:
    async def test_failure_criteria_satisfeito_para_o_run(self, verifier, ctx, sandbox):
        """`failure_criteria` ganha uso: condição de parada, não de sucesso."""
        artefato(sandbox)
        (sandbox.root / "erro.log").write_text("falhou", encoding="utf-8")
        goal = goal_de_arquivo(failure_criteria=(FileExists(path="erro.log"),))

        result = await verifier.evaluate(
            goal=goal,
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            ctx=ctx,
        )
        assert result.safety_ok is False
        assert result.next_action is NextAction.STOP_FAILURE
        assert result.reward_signal == -1.0

    async def test_safety_bloqueia_conclusao_mesmo_com_goal_satisfeito(
        self, verifier, ctx, sandbox
    ):
        artefato(sandbox)
        (sandbox.root / "erro.log").write_text("falhou", encoding="utf-8")
        goal = goal_de_arquivo(failure_criteria=(FileExists(path="erro.log"),))

        result = await verifier.evaluate(
            goal=goal, checkpoint=checkpoint_com_baseline(), report=report(), ctx=ctx
        )
        assert result.goal_satisfied is False

    async def test_safety_externa_tambem_bloqueia(self, verifier, ctx, sandbox):
        """A policy pode declarar insegurança sem critério de falha no goal."""
        artefato(sandbox)
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            ctx=ctx,
            safety_ok=False,
        )
        assert result.next_action is NextAction.STOP_FAILURE


class TestDecisaoAposFalha:
    async def test_falha_com_retry_seguro_repete(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(
                ExecutionStatus.FAILURE,
                error_code=ErrorCode.TOOL_TRANSIENT_ERROR,
                retry_available=True,
            ),
            ctx=ctx,
        )
        assert result.next_action is NextAction.RETRY

    async def test_falha_permanente_replaneja(self, verifier, ctx):
        """A tool não vai funcionar; o plano é que precisa mudar."""
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(
                ExecutionStatus.FAILURE, error_code=ErrorCode.TOOL_PERMANENT_ERROR
            ),
            ctx=ctx,
        )
        assert result.next_action is NextAction.REPLAN

    async def test_falha_sem_retry_seguro_pede_humano(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(
                ExecutionStatus.FAILURE, error_code=ErrorCode.TOOL_TRANSIENT_ERROR
            ),
            ctx=ctx,
        )
        assert result.next_action is NextAction.ASK_USER


class TestEfeitoDesconhecido:
    """C05: efeito indeterminado nunca avança sozinho."""

    async def test_unknown_sem_retry_pede_humano(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(ExecutionStatus.UNKNOWN, probe_result="INDETERMINATE"),
            ctx=ctx,
        )
        assert result.next_action is NextAction.ASK_USER
        assert result.error_code is ErrorCode.UNKNOWN_SIDE_EFFECT

    async def test_unknown_com_retry_provado_seguro_repete(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(ExecutionStatus.UNKNOWN, retry_available=True),
            ctx=ctx,
        )
        assert result.next_action is NextAction.RETRY

    async def test_unknown_nao_conclui_goal_mesmo_com_artefato(
        self, verifier, ctx, sandbox
    ):
        """O arquivo existe, mas o efeito é indeterminado: não se conclui."""
        artefato(sandbox)
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(ExecutionStatus.UNKNOWN),
            ctx=ctx,
        )
        assert result.next_action is NextAction.ASK_USER
        assert result.goal_satisfied is False


class TestVerificacaoDeStep:
    async def test_expected_outcome_nao_satisfeito_replaneja(self, verifier, ctx):
        """Tool funcionou e o efeito não apareceu: erro de plano, não de execução."""
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            expected_outcomes=(FileExists(path="out.json"),),
            ctx=ctx,
        )
        assert result.expected_outcomes_satisfied is False
        assert result.next_action is NextAction.REPLAN
        assert result.reward_signal == -0.5

    async def test_expected_outcome_satisfeito_continua(self, verifier, ctx, sandbox):
        (sandbox.root / "parcial.json").write_text("[]", encoding="utf-8")
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            expected_outcomes=(FileExists(path="parcial.json"),),
            ctx=ctx,
        )
        assert result.expected_outcomes_satisfied is True
        assert result.next_action is NextAction.CONTINUE
        assert result.reward_signal == 0.5

    async def test_expected_outcome_indeterminado_pede_humano(self, verifier, ctx):
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            expected_outcomes=(FileExists(path="../fora.json"),),
            ctx=ctx,
        )
        assert result.expected_outcomes_satisfied is None
        assert result.next_action is NextAction.ASK_USER
        assert result.error_code is ErrorCode.INDETERMINATE_VERIFICATION

    async def test_auto_relato_da_tool_nao_conclui_goal(self, verifier, ctx):
        """B2 em unidade: expected outcome sobre ACTION_RESULT é satisfeito,
        e mesmo assim o goal não conclui — falta evidência externa."""
        ctx.action_result = {"bytes_written": 42}
        result = await verifier.evaluate(
            goal=goal_de_arquivo(),
            checkpoint=checkpoint_com_baseline(),
            report=report(),
            expected_outcomes=(
                JsonPathEquals(
                    source="ACTION_RESULT", json_path="$.bytes_written", expected=42
                ),
            ),
            ctx=ctx,
        )
        assert result.expected_outcomes_satisfied is True
        assert result.goal_satisfied is False
        assert result.next_action is NextAction.CONTINUE
