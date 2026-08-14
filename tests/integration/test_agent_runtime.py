"""TASK-012. O loop completo, com o LLM falso no lugar do Deliberator real.

Substitui os testes do walking skeleton: os mesmos casos rodam agora contra
o runtime de verdade, com percepção, memória, Fast Path e budget.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from factories import make_goal
from neuroloop.core import (
    AllOf,
    ErrorCode,
    ExecutionBudget,
    FileExists,
    JsonPathCount,
    RunPhase,
    TrustLevel,
)
from neuroloop.llm import FakeLLMClient, LlmActionProposal, LlmDecision, LlmFileExists
from neuroloop.llm.schemas import LlmPlan, LlmPlanStep
from neuroloop.persistence import build_session_factory
from neuroloop.persistence.repositories import (
    ActionRepository,
    AgentRepository,
    EpisodeRepository,
    GoalRepository,
    ObservationRepository,
    RunEventRepository,
    RunRepository,
)
from neuroloop.runtime import AgentRuntime
from neuroloop.security import default_policy
from neuroloop.tools import Sandbox, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools

ELIGIBLE = json.dumps([{"id": 1}, {"id": 2}, {"id": 3}])


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "orders.json").write_text(ELIGIBLE, encoding="utf-8")
    return Sandbox(root)


@pytest.fixture
def registry(sandbox) -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg, sandbox)
    return reg


def goal_com_tres_registros(agent_id):
    return make_goal(
        agent_id=agent_id,
        description="gerar eligible.json com exatamente 3 registros",
        success_criteria=(
            AllOf(
                criteria=(
                    FileExists(path="eligible.json"),
                    JsonPathCount(
                        source="FILE",
                        path="eligible.json",
                        json_path="$[*]",
                        expected_count=3,
                    ),
                )
            ),
        ),
    )


def plano_llm() -> LlmDecision:
    return LlmDecision(
        type="PLAN",
        reason_code="INITIAL_PLAN",
        plan=LlmPlan(
            objective="produzir eligible.json",
            completion_condition="eligible.json com 3 registros",
            steps=[
                LlmPlanStep(
                    id="read",
                    description="ler orders.json",
                    preferred_tool="filesystem.read",
                    arguments_json=json.dumps({"path": "orders.json"}),
                    expected_outcomes=[LlmFileExists(path="orders.json")],
                ),
                LlmPlanStep(
                    id="write",
                    description="gravar eligible.json",
                    dependencies=["read"],
                    preferred_tool="filesystem.write",
                    arguments_json=json.dumps(
                        {"path": "eligible.json", "content": ELIGIBLE}
                    ),
                    expected_outcomes=[LlmFileExists(path="eligible.json")],
                    risk_hint="R1",
                ),
            ],
        ),
    )


def acao_llm(*, derived_from: list[str] | None = None, **overrides) -> LlmDecision:
    defaults = dict(
        type="ACT",
        reason_code="WRITE",
        action=LlmActionProposal(
            tool="filesystem.write",
            arguments_json=json.dumps({"path": "eligible.json", "content": ELIGIBLE}),
            expected_outcomes=[LlmFileExists(path="eligible.json")],
            rationale_code="WRITE",
            derived_from=derived_from if derived_from is not None else [str(uuid4())],
        ),
    )
    defaults.update(overrides)
    return LlmDecision(**defaults)


async def _id_da_observacao_do_goal(engine, run_id):
    factory = build_session_factory(engine)
    async with factory() as session:
        pendentes = await ObservationRepository(session).pending(run_id)
    return next(o.id for o in pendentes if o.kind == "goal")


async def seed(engine, goal_factory=goal_com_tres_registros):
    factory = build_session_factory(engine)
    async with factory() as session:
        agent_id = await AgentRepository(session).ensure(f"rt-{uuid4().hex[:6]}")
        goal = goal_factory(agent_id)
        await GoalRepository(session).create(goal)
        await session.commit()
    return goal


def build_runtime(engine, registry, sandbox, llm, **overrides):
    return AgentRuntime(
        session_factory=build_session_factory(engine),
        registry=registry,
        sandbox=sandbox,
        llm=llm,
        **overrides,
    )


class TestCicloCompleto:
    async def test_run_conclui_com_evidencia_externa(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.COMPLETED
        assert (sandbox.root / "eligible.json").is_file()
        assert json.loads((sandbox.root / "eligible.json").read_text(encoding="utf-8")) == [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]

    async def test_plano_deliberado_uma_vez_e_executado_por_fast_path(
        self, engine, registry, sandbox
    ):
        """Steps materializados não voltam ao LLM."""
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.deliberations == 1
        assert result.fast_path_hits.get("STEP") == 2

    async def test_acao_direta_sem_plano(self, engine, registry, sandbox):
        """A ação cita a observação do goal — proveniência auditável."""
        goal = await seed(engine)
        llm = FakeLLMClient()
        runtime = build_runtime(engine, registry, sandbox, llm)
        checkpoint = await runtime.start(goal)

        origem = await _id_da_observacao_do_goal(engine, checkpoint.run_id)
        llm.queue(acao_llm(derived_from=[str(origem)]))
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.COMPLETED
        assert result.deliberations == 1

    async def test_execucao_fica_auditavel(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)
        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        factory = build_session_factory(engine)
        async with factory() as session:
            episodes = await EpisodeRepository(session).by_run(result.run_id)
            events = await RunEventRepository(session).by_run(result.run_id)
            assert await ActionRepository(session).in_flight_attempts(result.run_id) == []

        assert {e.tool_name for e in episodes} == {"filesystem.read", "filesystem.write"}
        assert any(e.kind == "ACTION_AUTHORIZATION" for e in events)
        assert any(e.kind == "PHASE_TRANSITION" for e in events)


class TestPercepcao:
    async def test_objetivo_vira_a_primeira_observacao(self, engine, registry, sandbox):
        goal = await seed(engine)
        runtime = build_runtime(engine, registry, sandbox, FakeLLMClient())
        checkpoint = await runtime.start(goal)

        factory = build_session_factory(engine)
        async with factory() as session:
            pendentes = await ObservationRepository(session).pending(checkpoint.run_id)

        assert len(pendentes) == 1
        assert pendentes[0].kind == "goal"
        assert pendentes[0].trust is TrustLevel.USER

    async def test_leitura_de_arquivo_vira_conteudo_nao_confiavel(
        self, engine, registry, sandbox
    ):
        """A tool declara que devolve conteúdo de fora; a percepção respeita."""
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)
        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        factory = build_session_factory(engine)
        async with factory() as session:
            from sqlalchemy import select

            from neuroloop.persistence import models

            rows = list(
                await session.scalars(
                    select(models.Observation).where(
                        models.Observation.run_id == result.run_id
                    )
                )
            )

        por_tool = {r.source_ref: r.trust for r in rows if r.kind == "tool_result"}
        assert por_tool["orders.json"] == TrustLevel.UNTRUSTED_EXTERNAL.value
        # `bytes_written` é metadado da própria ferramenta, não conteúdo do mundo
        assert por_tool["eligible.json"] == TrustLevel.TRUSTED_INTERNAL.value


class TestRegraDeDelta:
    """C02 verificada no runtime real."""

    async def test_goal_ja_satisfeito_pede_confirmacao(self, engine, registry, sandbox):
        (sandbox.root / "eligible.json").write_text(ELIGIBLE, encoding="utf-8")
        goal = await seed(engine)
        runtime = build_runtime(engine, registry, sandbox, FakeLLMClient())

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.WAITING_USER
        assert result.error_code is ErrorCode.GOAL_PRE_SATISFIED

    async def test_baseline_e_persistido(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)
        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        factory = build_session_factory(engine)
        async with factory() as session:
            gravado = await RunRepository(session).load(result.run_id)
        assert gravado.baseline_outcomes[0].satisfied is False


class TestBudget:
    """C12: o budget só se move porque o usage é creditado."""

    async def test_tokens_e_custo_sao_creditados(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()], input_tokens=2000, output_tokens=800)
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.tokens_used == 2800
        assert result.cost_used_usd > 0

    async def test_teto_de_iteracoes_encerra_o_run(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal, budget=ExecutionBudget(max_iterations=2))
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.FAILED
        assert result.error_code is ErrorCode.BUDGET_EXCEEDED

    async def test_teto_de_tokens_encerra_o_run(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()], input_tokens=5000, output_tokens=5000)
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal, budget=ExecutionBudget(token_budget=1000))
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.error_code is ErrorCode.BUDGET_EXCEEDED


class TestTaintNoLoop:
    """C10 no loop: proveniência decide a autoridade da ação."""

    async def test_proveniencia_nao_auditavel_exige_aprovacao(
        self, engine, registry, sandbox
    ):
        """Id de observação que o runtime não viu vale como não confiável."""
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[acao_llm(derived_from=[str(uuid4())])])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.WAITING_USER
        assert result.waiting_reason == "UNTRUSTED_ORIGIN_NEEDS_APPROVAL:R1"
        assert not (sandbox.root / "eligible.json").exists()

    async def test_proveniencia_confiavel_libera(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient()
        runtime = build_runtime(engine, registry, sandbox, llm)
        checkpoint = await runtime.start(goal)

        origem = await _id_da_observacao_do_goal(engine, checkpoint.run_id)
        llm.queue(acao_llm(derived_from=[str(origem)]))
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.COMPLETED


class TestPolicyNoLoop:
    async def test_capability_ausente_barra_o_run(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient()
        runtime = build_runtime(
            engine,
            registry,
            sandbox,
            llm,
            policy=default_policy(sandbox, granted_capabilities=frozenset({"fs:read"})),
        )

        checkpoint = await runtime.start(goal)
        llm.queue(
            acao_llm(
                derived_from=[str(await _id_da_observacao_do_goal(engine, checkpoint.run_id))]
            )
        )
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.FAILED
        assert result.error_code is ErrorCode.PERMISSION_DENIED
        assert not (sandbox.root / "eligible.json").exists()

    async def test_escrita_fora_do_sandbox_e_barrada(
        self, engine, registry, sandbox, tmp_path
    ):
        goal = await seed(engine)
        fora = tmp_path / "escapou.json"
        llm = FakeLLMClient(
            outputs=[
                acao_llm(
                    action=LlmActionProposal(
                        tool="filesystem.write",
                        arguments_json=json.dumps({"path": str(fora), "content": "x"}),
                        expected_outcomes=[LlmFileExists(path="eligible.json")],
                        rationale_code="WRITE",
                        derived_from=[str(uuid4())],
                    )
                )
            ]
        )
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.error_code is ErrorCode.PERMISSION_DENIED
        assert not fora.exists()


class TestDecisoesQueParamOLoop:
    async def test_ask_user_para_o_run(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(
            outputs=[LlmDecision(type="ASK_USER", reason_code="MISSING", question="qual?")]
        )
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.WAITING_USER

    async def test_impossible_falha_o_run(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(
            outputs=[
                LlmDecision(type="IMPOSSIBLE", reason_code="NO_TOOL", evidence=["sem tool"])
            ]
        )
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.FAILED
        assert result.error_code is ErrorCode.IMPOSSIBLE_TASK

    async def test_falha_do_llm_vira_reasoning_error(self, engine, registry, sandbox):
        goal = await seed(engine)
        runtime = build_runtime(engine, registry, sandbox, FakeLLMClient())

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.FAILED
        assert result.error_code is ErrorCode.REASONING_ERROR

    async def test_plano_invalido_e_recusado(self, engine, registry, sandbox):
        """Step sem evidência externa não passa pelo PlannerValidator."""
        goal = await seed(engine)
        from neuroloop.llm.schemas import LlmJsonPathEquals

        llm = FakeLLMClient(
            outputs=[
                LlmDecision(
                    type="PLAN",
                    reason_code="PLAN",
                    plan=LlmPlan(
                        objective="obj",
                        completion_condition="ok",
                        steps=[
                            LlmPlanStep(
                                id="a",
                                description="ler",
                                preferred_tool="filesystem.read",
                                arguments_json=json.dumps({"path": "orders.json"}),
                                expected_outcomes=[
                                    LlmJsonPathEquals(
                                        source="ACTION_RESULT",
                                        json_path="$.bytes",
                                        expected_json="1",
                                    )
                                ],
                            )
                        ],
                    ),
                )
            ]
        )
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.error_code is ErrorCode.INVALID_PLAN


class TestCancelamento:
    async def test_cancelamento_encerra_no_topo_do_ciclo(self, engine, registry, sandbox):
        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        await runtime.cancel(checkpoint.run_id)
        result = await runtime.run_until_pause(checkpoint.run_id)

        assert result.phase is RunPhase.CANCELLED
        assert result.error_code is ErrorCode.CANCELLED


class TestReusoEntreRuns:
    """C16 no loop: o segundo run encontra o plano do primeiro."""

    async def test_plano_e_gravado_no_cache_ao_concluir(self, engine, registry, sandbox):
        from neuroloop.memory import PlanCache

        goal = await seed(engine)
        llm = FakeLLMClient(outputs=[plano_llm()])
        runtime = build_runtime(engine, registry, sandbox, llm)

        checkpoint = await runtime.start(goal)
        await runtime.run_until_pause(checkpoint.run_id)

        factory = build_session_factory(engine)
        async with factory() as session:
            cached = await PlanCache(session).lookup(
                goal, frozenset(registry.names())
            )
        assert cached is not None
        assert cached.successes == 1
        assert len(cached.plan.steps) == 2
