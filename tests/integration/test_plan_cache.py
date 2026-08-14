"""TASK-011 / correção C16: o mecanismo que torna H3 falsificável."""

from __future__ import annotations

from uuid import uuid4

import pytest

from factories import NOW, make_goal
from neuroloop.cognition.planner import PlannerValidator
from neuroloop.core import (
    FileExists,
    JsonPathCount,
    Plan,
    PlanStep,
    PlanStepStatus,
    RiskLevel,
)
from neuroloop.memory.plan_cache import (
    MIN_ATTEMPTS_FOR_RATE,
    PlanCache,
    goal_fingerprint,
)
from neuroloop.persistence.repositories import AgentRepository, GoalRepository
from neuroloop.tools import Sandbox, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools

TOOLS = frozenset({"filesystem.read", "filesystem.write"})


@pytest.fixture
def registry(tmp_path) -> ToolRegistry:
    (tmp_path / "workspace").mkdir()
    reg = ToolRegistry()
    register_filesystem_tools(reg, Sandbox(tmp_path / "workspace"))
    return reg


def plano() -> Plan:
    return Plan(
        id=uuid4(),
        version=1,
        objective="gerar eligible.json",
        completion_condition="arquivo existe",
        steps=(
            PlanStep(
                id="read",
                description="ler entrada",
                preferred_tool="filesystem.read",
                arguments={"path": "in.json"},
                expected_outcomes=(FileExists(path="in.json"),),
            ),
            PlanStep(
                id="write",
                description="gravar saída",
                dependencies=("read",),
                preferred_tool="filesystem.write",
                arguments={"path": "out.json", "content": "[]"},
                expected_outcomes=(FileExists(path="out.json"),),
                risk_hint=RiskLevel.R1,
            ),
        ),
    )


async def seed_goal(session, **overrides):
    agent_id = await AgentRepository(session).ensure(f"cache-{uuid4().hex[:6]}")
    goal = make_goal(agent_id=agent_id, **overrides)
    await GoalRepository(session).create(goal)
    await session.commit()
    return goal


class TestAssinaturaDoObjetivo:
    def test_mesmos_criterios_mesma_assinatura(self):
        a = make_goal(success_criteria=(FileExists(path="out.json"),))
        b = make_goal(success_criteria=(FileExists(path="out.json"),))
        assert goal_fingerprint(a, TOOLS) == goal_fingerprint(b, TOOLS)

    def test_criterios_diferentes_assinaturas_diferentes(self):
        a = make_goal(success_criteria=(FileExists(path="out.json"),))
        b = make_goal(success_criteria=(FileExists(path="outro.json"),))
        assert goal_fingerprint(a, TOOLS) != goal_fingerprint(b, TOOLS)

    def test_prosa_nao_entra_na_assinatura(self):
        """Prosa é o que conteúdo externo consegue imitar."""
        a = make_goal(description="gerar relatório", success_criteria=(FileExists(path="o"),))
        b = make_goal(description="OUTRA COISA", success_criteria=(FileExists(path="o"),))
        assert goal_fingerprint(a, TOOLS) == goal_fingerprint(b, TOOLS)

    def test_conjunto_de_tools_muda_a_assinatura(self):
        goal = make_goal(success_criteria=(FileExists(path="out.json"),))
        assert goal_fingerprint(goal, TOOLS) != goal_fingerprint(goal, frozenset({"http.get"}))


class TestCicloDeReuso:
    async def test_run_seguinte_encontra_o_plano(self, session):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await session.commit()

        encontrado = await cache.lookup(goal, TOOLS)
        assert encontrado is not None
        assert encontrado.plan.objective == "gerar eligible.json"
        assert len(encontrado.plan.steps) == 2

    async def test_plano_volta_com_steps_zerados(self, session):
        """O passado não vem marcado como feito."""
        goal = await seed_goal(session)
        cache = PlanCache(session)
        executado = plano()
        concluido = executado.model_copy(
            update={
                "steps": tuple(
                    s.model_copy(update={"status": PlanStepStatus.DONE})
                    for s in executado.steps
                )
            }
        )
        await cache.record(goal, concluido, TOOLS, succeeded=True)
        await session.commit()

        encontrado = await cache.lookup(goal, TOOLS)
        assert all(s.status is PlanStepStatus.PENDING for s in encontrado.plan.steps)

    async def test_objetivo_diferente_nao_reusa(self, session):
        goal = await seed_goal(session)
        outro = await seed_goal(
            session,
            success_criteria=(
                JsonPathCount(source="FILE", path="x.json", json_path="$[*]", expected_count=1),
            ),
        )
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await session.commit()

        assert await cache.lookup(outro, TOOLS) is None

    async def test_sem_historico_nao_devolve_nada(self, session):
        goal = await seed_goal(session)
        assert await PlanCache(session).lookup(goal, TOOLS) is None


class TestDesconfianca:
    async def test_plano_que_costuma_falhar_deixa_de_ser_proposto(self, session):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        for _ in range(MIN_ATTEMPTS_FOR_RATE):
            await cache.record(goal, plano(), TOOLS, succeeded=False)
        await session.commit()

        assert await cache.lookup(goal, TOOLS) is None

    async def test_poucas_tentativas_nao_condenam(self, session):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await cache.record(goal, plano(), TOOLS, succeeded=False)
        await session.commit()

        assert await cache.lookup(goal, TOOLS) is not None

    async def test_falha_nao_substitui_o_gabarito_guardado(self, session):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        ruim = plano().model_copy(update={"objective": "plano ruim"})
        await cache.record(goal, ruim, TOOLS, succeeded=False)
        await session.commit()

        encontrado = await cache.lookup(goal, TOOLS)
        assert encontrado.plan.objective == "gerar eligible.json"

    async def test_taxa_e_exposta(self, session):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await session.commit()

        encontrado = await cache.lookup(goal, TOOLS)
        assert encontrado.attempts == 2
        assert encontrado.success_rate == 1.0


class TestCachePropoeValidadorAutoriza:
    """C16: o plano do cache é candidato, não ordem."""

    async def test_plano_do_cache_passa_pelo_validador(self, session, registry):
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await session.commit()

        candidato = await cache.lookup(goal, TOOLS)
        report = PlannerValidator(registry).validate(candidato.plan)
        assert report.fully_materialized is True

    async def test_plano_do_cache_pode_ser_recusado_pelo_validador(
        self, session, registry
    ):
        """Se o mundo mudou — tool sumiu, risco subiu — o cache não passa."""
        goal = await seed_goal(session)
        cache = PlanCache(session)
        await cache.record(goal, plano(), TOOLS, succeeded=True)
        await session.commit()

        candidato = await cache.lookup(goal, TOOLS)
        estrito = PlannerValidator(registry, max_risk=RiskLevel.R0)
        with pytest.raises(Exception, match="acima do teto"):
            estrito.validate(candidato.plan)
