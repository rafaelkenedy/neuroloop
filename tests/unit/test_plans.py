"""Spec §13: horizonte curto, DAG válido, expected outcomes obrigatórios."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from neuroloop.core import (
    MAX_PLAN_STEPS,
    ErrorCode,
    FileExists,
    Plan,
    PlanStep,
    RiskLevel,
)


def step(sid: str, deps: tuple[str, ...] = (), **kwargs) -> PlanStep:
    defaults = dict(
        id=sid,
        description=f"step {sid}",
        dependencies=deps,
        expected_outcomes=(FileExists(path=f"/workspace/{sid}.json"),),
    )
    defaults.update(kwargs)
    return PlanStep(**defaults)


def plan(*steps: PlanStep, **kwargs) -> Plan:
    defaults = dict(
        id=uuid4(),
        version=1,
        objective="produzir eligible.json",
        steps=steps,
        completion_condition="arquivo existe com 3 registros",
    )
    defaults.update(kwargs)
    return Plan(**defaults)


class TestValidacaoEstrutural:
    def test_plano_linear_valido(self):
        p = plan(step("a"), step("b", ("a",)), step("c", ("b",)))
        assert p.topological_order() == ("a", "b", "c")

    def test_ciclo_rejeitado(self):
        with pytest.raises(ValidationError) as exc:
            plan(step("a", ("c",)), step("b", ("a",)), step("c", ("b",)))
        assert ErrorCode.INVALID_PLAN.value in str(exc.value)
        assert "ciclo" in str(exc.value)

    def test_auto_dependencia_rejeitada(self):
        with pytest.raises(ValidationError) as exc:
            plan(step("a", ("a",)))
        assert ErrorCode.INVALID_PLAN.value in str(exc.value)

    def test_dependencia_inexistente_rejeitada(self):
        with pytest.raises(ValidationError) as exc:
            plan(step("a"), step("b", ("fantasma",)))
        assert "inexistente" in str(exc.value)

    def test_ids_duplicados_rejeitados(self):
        with pytest.raises(ValidationError) as exc:
            plan(step("a"), step("a"))
        assert "duplicados" in str(exc.value)

    def test_expected_outcomes_vazio_rejeitado(self):
        """Step sem resultado esperado é step não verificável."""
        with pytest.raises(ValidationError):
            plan(step("a", expected_outcomes=()))

    def test_limite_de_steps(self):
        ok = plan(*[step(f"s{i}") for i in range(MAX_PLAN_STEPS)])
        assert len(ok.steps) == MAX_PLAN_STEPS
        with pytest.raises(ValidationError):
            plan(*[step(f"s{i}") for i in range(MAX_PLAN_STEPS + 1)])

    def test_plano_vazio_rejeitado(self):
        with pytest.raises(ValidationError):
            plan()

    def test_versao_minima(self):
        with pytest.raises(ValidationError):
            plan(step("a"), version=0)


class TestOrdenacao:
    def test_diamante_respeita_dependencias(self):
        p = plan(
            step("a"),
            step("b", ("a",)),
            step("c", ("a",)),
            step("d", ("b", "c")),
        )
        order = p.topological_order()
        assert order.index("a") < order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_ordem_estavel_por_declaracao(self):
        p = plan(step("x"), step("y"), step("z"))
        assert p.topological_order() == ("x", "y", "z")


class TestMaterializacao:
    def test_step_materializado_habilita_fast_path(self):
        s = step("a", preferred_tool="filesystem.read", arguments={"path": "/in.json"})
        assert s.is_materialized is True

    def test_step_sem_argumentos_nao_e_materializado(self):
        assert step("a", preferred_tool="filesystem.read").is_materialized is False
        assert step("a").is_materialized is False

    def test_risco_default_e_leitura(self):
        assert step("a").risk_hint is RiskLevel.R0
