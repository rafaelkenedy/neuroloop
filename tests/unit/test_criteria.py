"""Correção C01: união tipada, origem derivada e lógica ternária."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from factories import make_outcome
from neuroloop.core import (
    AllOf,
    AnyOf,
    CommandExitCodeEquals,
    Criterion,
    FileExists,
    HttpStatusEquals,
    JsonPathCount,
    JsonPathEquals,
    ValueEquals,
    apply_negate,
    combine_all,
    combine_any,
    effective_observes,
    is_conclusive_success,
    iter_criteria,
)

criterion_adapter: TypeAdapter[Criterion] = TypeAdapter(Criterion)


class TestUniaoDiscriminada:
    def test_desserializa_pelo_kind(self):
        parsed = criterion_adapter.validate_python(
            {"kind": "FILE_EXISTS", "path": "/workspace/out.json"}
        )
        assert isinstance(parsed, FileExists)
        assert parsed.path == "/workspace/out.json"

    def test_kind_desconhecido_rejeitado(self):
        with pytest.raises(ValidationError):
            criterion_adapter.validate_python({"kind": "LLM_ACHA_QUE_SIM"})

    def test_campo_extra_rejeitado(self):
        with pytest.raises(ValidationError):
            criterion_adapter.validate_python(
                {"kind": "FILE_EXISTS", "path": "/a", "confianca": 0.9}
            )

    def test_criterio_e_imutavel(self):
        criterion = FileExists(path="/a")
        with pytest.raises(ValidationError):
            criterion.path = "/b"

    def test_composto_exige_ao_menos_um_filho(self):
        with pytest.raises(ValidationError):
            AllOf(criteria=())

    def test_status_http_fora_da_faixa_rejeitado(self):
        with pytest.raises(ValidationError):
            HttpStatusEquals(url="http://x", expected_status=99)

    def test_comando_vazio_rejeitado(self):
        with pytest.raises(ValidationError):
            CommandExitCodeEquals(command=())

    def test_aninhamento_recursivo(self):
        nested = AllOf(
            criteria=(
                FileExists(path="/a"),
                AnyOf(criteria=(FileExists(path="/b"), FileExists(path="/c"))),
            )
        )
        assert len(list(iter_criteria(nested))) == 5


class TestOrigemDaEvidencia:
    """A origem é derivada para que não exista estado inconsistente."""

    def test_arquivo_e_estado_externo(self):
        assert effective_observes(FileExists(path="/a")) == "EXTERNAL_STATE"

    def test_valor_do_run_e_run_state(self):
        assert effective_observes(ValueEquals(ref="run.iteration", expected=1)) == "RUN_STATE"

    def test_json_path_segue_a_source(self):
        do_arquivo = JsonPathEquals(source="FILE", path="/a.json", json_path="$.n", expected=3)
        do_resultado = JsonPathEquals(source="ACTION_RESULT", json_path="$.n", expected=3)
        assert effective_observes(do_arquivo) == "EXTERNAL_STATE"
        assert effective_observes(do_resultado) == "ACTION_RESULT"

    def test_composto_assume_a_evidencia_mais_fraca(self):
        """Misturar estado externo com auto-relato não produz prova externa."""
        misto = AllOf(
            criteria=(
                FileExists(path="/a"),
                JsonPathCount(source="ACTION_RESULT", json_path="$.items", expected_count=3),
            )
        )
        assert effective_observes(misto) == "ACTION_RESULT"

    def test_composto_totalmente_externo_permanece_externo(self):
        externo = AllOf(
            criteria=(
                FileExists(path="/a"),
                JsonPathCount(source="FILE", path="/a", json_path="$.items", expected_count=3),
            )
        )
        assert effective_observes(externo) == "EXTERNAL_STATE"


class TestLogicaTernaria:
    """INDETERMINATE nunca vira sucesso."""

    @pytest.mark.parametrize(
        ("valores", "esperado"),
        [
            ([True, True], True),
            ([True, False], False),
            ([True, None], None),
            ([False, None], False),
            ([None, None], None),
        ],
    )
    def test_conjuncao(self, valores, esperado):
        assert combine_all(valores) is esperado

    @pytest.mark.parametrize(
        ("valores", "esperado"),
        [
            ([False, False], False),
            ([True, False], True),
            ([True, None], True),
            ([False, None], None),
            ([None, None], None),
        ],
    )
    def test_disjuncao(self, valores, esperado):
        assert combine_any(valores) is esperado

    def test_negacao_preserva_indeterminado(self):
        assert apply_negate(None, negate=True) is None
        assert apply_negate(True, negate=True) is False
        assert apply_negate(False, negate=False) is False

    def test_lista_vazia_e_erro_nao_sucesso(self):
        with pytest.raises(ValueError):
            combine_all([])
        with pytest.raises(ValueError):
            combine_any([])


class TestConclusaoDeSucesso:
    def test_todos_satisfeitos(self):
        assert is_conclusive_success([make_outcome(True), make_outcome(True)]) is True

    def test_um_indeterminado_derruba(self):
        assert is_conclusive_success([make_outcome(True), make_outcome(None)]) is False

    def test_ausencia_de_evidencia_nao_e_sucesso(self):
        assert is_conclusive_success([]) is False
