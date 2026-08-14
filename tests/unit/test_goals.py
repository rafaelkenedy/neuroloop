"""Correção C02: goal precisa ser verificável por evidência externa."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factories import make_goal
from neuroloop.core import (
    AllOf,
    Constraint,
    ErrorCode,
    FileExists,
    JsonPathCount,
    JsonPathEquals,
)


class TestCriteriosDeSucesso:
    def test_goal_com_criterio_externo_e_valido(self):
        goal = make_goal(success_criteria=(FileExists(path="/workspace/out.json"),))
        assert len(goal.external_success_criteria) == 1

    def test_goal_apenas_com_auto_relato_e_rejeitado(self):
        """O caso central de falso sucesso: 'a tool disse que deu certo'."""
        with pytest.raises(ValidationError) as exc:
            make_goal(
                success_criteria=(
                    JsonPathEquals(source="ACTION_RESULT", json_path="$.ok", expected=True),
                )
            )
        assert ErrorCode.INVALID_GOAL_CRITERIA.value in str(exc.value)

    def test_goal_sem_criterios_e_rejeitado(self):
        with pytest.raises(ValidationError):
            make_goal(success_criteria=())

    def test_composto_misto_nao_habilita_o_goal(self):
        """AllOf com auto-relato dentro não conta como evidência externa."""
        misto = AllOf(
            criteria=(
                FileExists(path="/workspace/out.json"),
                JsonPathCount(source="ACTION_RESULT", json_path="$.items", expected_count=3),
            )
        )
        with pytest.raises(ValidationError) as exc:
            make_goal(success_criteria=(misto,))
        assert ErrorCode.INVALID_GOAL_CRITERIA.value in str(exc.value)

    def test_mistura_no_topo_e_aceita_e_filtrada(self):
        """Basta um critério externo; os demais seguem como sinal auxiliar."""
        goal = make_goal(
            success_criteria=(
                FileExists(path="/workspace/out.json"),
                JsonPathEquals(source="ACTION_RESULT", json_path="$.ok", expected=True),
            )
        )
        assert len(goal.success_criteria) == 2
        assert len(goal.external_success_criteria) == 1


class TestCamposDoGoal:
    def test_prioridade_fora_da_faixa_rejeitada(self):
        with pytest.raises(ValidationError):
            make_goal(priority=1.5)

    def test_descricao_vazia_rejeitada(self):
        with pytest.raises(ValidationError):
            make_goal(description="")

    def test_constraint_aceita_criterio_opcional(self):
        goal = make_goal(
            constraints=(
                Constraint(description="não escrever fora do sandbox"),
                Constraint(
                    description="arquivo precisa existir",
                    criterion=FileExists(path="/workspace/in.json"),
                ),
            )
        )
        assert goal.constraints[1].criterion is not None
