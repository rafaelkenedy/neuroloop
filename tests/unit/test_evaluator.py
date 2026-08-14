"""Avaliação de critérios: o que é observável, o que é INDETERMINATE."""

from __future__ import annotations

import json

import pytest

from neuroloop.core import (
    AllOf,
    AnyOf,
    CommandExitCodeEquals,
    FileExists,
    FileMatchesJsonSchema,
    HttpStatusEquals,
    JsonPathCount,
    JsonPathEquals,
    ValueEquals,
)
from neuroloop.tools import Sandbox
from neuroloop.verification import CriterionEvaluator, EvaluationContext

SCHEMA_LISTA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "active": {"type": "boolean"}},
        "required": ["id", "active"],
    },
}


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "eligible.json").write_text(
        json.dumps([{"id": 1, "active": True}, {"id": 2, "active": True}]),
        encoding="utf-8",
    )
    (root / "quebrado.json").write_text("{isso não é json", encoding="utf-8")
    (root / "fora_do_schema.json").write_text(json.dumps([{"id": "um"}]), encoding="utf-8")
    return Sandbox(root)


@pytest.fixture
def ctx(sandbox) -> EvaluationContext:
    return EvaluationContext(sandbox=sandbox)


@pytest.fixture
def evaluator() -> CriterionEvaluator:
    return CriterionEvaluator()


class TestArquivo:
    async def test_arquivo_existente(self, evaluator, ctx):
        outcome = await evaluator.evaluate(FileExists(path="eligible.json"), ctx)
        assert outcome.satisfied is True

    async def test_arquivo_ausente_e_falso_nao_indeterminado(self, evaluator, ctx):
        """Ausência é observável: é `False`, não 'não sei'."""
        outcome = await evaluator.evaluate(FileExists(path="sumiu.json"), ctx)
        assert outcome.satisfied is False
        assert outcome.error is None

    async def test_fora_do_sandbox_e_indeterminado(self, evaluator, ctx):
        outcome = await evaluator.evaluate(FileExists(path="../fora.json"), ctx)
        assert outcome.satisfied is None
        assert "sandbox" in outcome.error

    async def test_sem_sandbox_e_indeterminado(self, evaluator):
        outcome = await evaluator.evaluate(FileExists(path="a.json"), EvaluationContext())
        assert outcome.satisfied is None


class TestSchemaDeArquivo:
    async def test_conforme_ao_schema(self, evaluator, ctx):
        criterion = FileMatchesJsonSchema(path="eligible.json", json_schema=SCHEMA_LISTA)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_fora_do_schema_e_falso(self, evaluator, ctx):
        criterion = FileMatchesJsonSchema(path="fora_do_schema.json", json_schema=SCHEMA_LISTA)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is False
        assert outcome.observed  # traz a primeira violação

    async def test_arquivo_ilegivel_e_indeterminado(self, evaluator, ctx):
        criterion = FileMatchesJsonSchema(path="quebrado.json", json_schema=SCHEMA_LISTA)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None


class TestJsonPath:
    async def test_contagem_bate(self, evaluator, ctx):
        criterion = JsonPathCount(
            source="FILE", path="eligible.json", json_path="$[*]", expected_count=2
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True
        assert outcome.observed == 2

    async def test_contagem_nao_bate(self, evaluator, ctx):
        criterion = JsonPathCount(
            source="FILE", path="eligible.json", json_path="$[*]", expected_count=3
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is False
        assert (outcome.observed, outcome.expected) == (2, 3)

    async def test_campo_aninhado(self, evaluator, ctx):
        criterion = JsonPathEquals(
            source="FILE", path="eligible.json", json_path="$[0].id", expected=1
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_json_invalido_e_indeterminado(self, evaluator, ctx):
        """Arquivo ilegível não refuta o critério — não foi possível observar."""
        criterion = JsonPathCount(
            source="FILE", path="quebrado.json", json_path="$[*]", expected_count=1
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None
        assert "JSON" in outcome.error

    async def test_arquivo_ausente_e_indeterminado_para_json_path(self, evaluator, ctx):
        criterion = JsonPathEquals(
            source="FILE", path="sumiu.json", json_path="$.a", expected=1
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None

    async def test_path_que_nao_resolve(self, evaluator, ctx):
        criterion = JsonPathEquals(
            source="FILE", path="eligible.json", json_path="$.inexistente", expected=1
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None

    async def test_resultado_da_acao(self, evaluator, sandbox):
        ctx = EvaluationContext(sandbox=sandbox, action_result={"bytes_written": 42})
        criterion = JsonPathEquals(
            source="ACTION_RESULT", json_path="$.bytes_written", expected=42
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_sem_resultado_de_acao_e_indeterminado(self, evaluator, ctx):
        criterion = JsonPathEquals(source="ACTION_RESULT", json_path="$.ok", expected=True)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None


class TestValorDoRun:
    async def test_ref_presente(self, evaluator, sandbox):
        ctx = EvaluationContext(sandbox=sandbox, run_values={"run.iteration": 3})
        outcome = await evaluator.evaluate(ValueEquals(ref="run.iteration", expected=3), ctx)
        assert outcome.satisfied is True

    async def test_ref_ausente_e_indeterminado(self, evaluator, ctx):
        outcome = await evaluator.evaluate(ValueEquals(ref="run.nada", expected=1), ctx)
        assert outcome.satisfied is None


class TestSondaHttp:
    """A sonda é injetada: o avaliador não decide como falar HTTP."""

    async def test_status_esperado(self, evaluator, sandbox):
        async def prober(method, url):
            assert (method, url) == ("GET", "http://api/orders/1")
            return 200

        ctx = EvaluationContext(sandbox=sandbox, http_prober=prober)
        criterion = HttpStatusEquals(url="http://api/orders/1", expected_status=200)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_status_diferente_e_falso(self, evaluator, sandbox):
        async def prober(method, url):
            return 404

        ctx = EvaluationContext(sandbox=sandbox, http_prober=prober)
        criterion = HttpStatusEquals(url="http://api/orders/1", expected_status=200)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is False
        assert outcome.observed == 404

    async def test_sem_sonda_e_indeterminado(self, evaluator, ctx):
        criterion = HttpStatusEquals(url="http://api", expected_status=200)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None
        assert "sonda HTTP" in outcome.error

    async def test_sonda_que_explode_e_indeterminado(self, evaluator, sandbox):
        """Rede indisponível não refuta o critério: não se observou nada."""

        async def prober(method, url):
            raise ConnectionError("rede fora")

        ctx = EvaluationContext(sandbox=sandbox, http_prober=prober)
        criterion = HttpStatusEquals(url="http://api", expected_status=200)
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None
        assert "rede fora" in outcome.error


class TestExecutorDeComando:
    async def test_exit_code_esperado(self, evaluator, sandbox):
        async def runner(command):
            assert command == ("pytest", "-q")
            return 0

        ctx = EvaluationContext(sandbox=sandbox, command_runner=runner)
        criterion = CommandExitCodeEquals(command=("pytest", "-q"))
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_exit_code_diferente(self, evaluator, sandbox):
        async def runner(command):
            return 1

        ctx = EvaluationContext(sandbox=sandbox, command_runner=runner)
        criterion = CommandExitCodeEquals(command=("pytest", "-q"))
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is False
        assert outcome.observed == 1

    async def test_sem_executor_e_indeterminado(self, evaluator, ctx):
        criterion = CommandExitCodeEquals(command=("echo", "oi"))
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None
        assert "executor de comando" in outcome.error


class TestNegacao:
    async def test_negacao_inverte(self, evaluator, ctx):
        outcome = await evaluator.evaluate(FileExists(path="sumiu.json", negate=True), ctx)
        assert outcome.satisfied is True

    async def test_negacao_preserva_indeterminado(self, evaluator, ctx):
        outcome = await evaluator.evaluate(FileExists(path="../fora.json", negate=True), ctx)
        assert outcome.satisfied is None


class TestCompostos:
    async def test_all_of_satisfeito(self, evaluator, ctx):
        criterion = AllOf(
            criteria=(
                FileExists(path="eligible.json"),
                JsonPathCount(
                    source="FILE", path="eligible.json", json_path="$[*]", expected_count=2
                ),
            )
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_all_of_com_indeterminado_nao_conclui(self, evaluator, ctx):
        """A regra que impede falso sucesso em composição."""
        criterion = AllOf(
            criteria=(
                FileExists(path="eligible.json"),
                JsonPathCount(
                    source="FILE", path="quebrado.json", json_path="$[*]", expected_count=1
                ),
            )
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is None

    async def test_all_of_com_falso_e_falso(self, evaluator, ctx):
        """False domina INDETERMINATE: já se sabe que não está satisfeito."""
        criterion = AllOf(
            criteria=(
                FileExists(path="sumiu.json"),
                JsonPathCount(
                    source="FILE", path="quebrado.json", json_path="$[*]", expected_count=1
                ),
            )
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is False

    async def test_any_of_com_verdadeiro_e_verdadeiro(self, evaluator, ctx):
        criterion = AnyOf(
            criteria=(FileExists(path="eligible.json"), FileExists(path="../fora.json"))
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True

    async def test_composto_mistura_arquivo_e_sonda(self, evaluator, sandbox):
        async def prober(method, url):
            return 200

        ctx = EvaluationContext(sandbox=sandbox, http_prober=prober)
        criterion = AllOf(
            criteria=(
                FileExists(path="eligible.json"),
                HttpStatusEquals(url="http://api", expected_status=200),
            )
        )
        outcome = await evaluator.evaluate(criterion, ctx)
        assert outcome.satisfied is True
