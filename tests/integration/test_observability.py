"""TASK-014. Aceite: explicar por que o agente fez cada ação."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from factories import make_goal
from neuroloop.core import FileExists, RunPhase
from neuroloop.llm import FakeLLMClient, LlmActionProposal, LlmDecision, LlmFileExists
from neuroloop.observability import (
    REDACTED,
    InMemoryTracer,
    collect_run_metrics,
    explain_action,
    explain_run,
    fingerprint,
    rate,
    redact,
    run_timeline,
)
from neuroloop.persistence import build_session_factory, models
from neuroloop.persistence.repositories import AgentRepository, GoalRepository
from neuroloop.runtime import AgentRuntime
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


async def _rodar(engine, registry, sandbox, *, tracer=None, derived_ok=True):
    factory = build_session_factory(engine)
    async with factory() as session:
        agent_id = await AgentRepository(session).ensure(f"obs-{uuid4().hex[:6]}")
        goal = make_goal(
            agent_id=agent_id, success_criteria=(FileExists(path="eligible.json"),)
        )
        await GoalRepository(session).create(goal)
        await session.commit()

    llm = FakeLLMClient()
    runtime = AgentRuntime(
        session_factory=factory,
        registry=registry,
        sandbox=sandbox,
        llm=llm,
        tracer=tracer,
    )
    checkpoint = await runtime.start(goal)

    async with factory() as session:
        from sqlalchemy import select

        row = await session.scalar(
            select(models.Observation).where(
                models.Observation.run_id == checkpoint.run_id,
                models.Observation.kind == "goal",
            )
        )
    origem = str(row.id) if derived_ok else str(uuid4())

    llm.queue(
        LlmDecision(
            type="ACT",
            reason_code="WRITE_ARTIFACT",
            action=LlmActionProposal(
                tool="filesystem.write",
                arguments_json=json.dumps(
                    {"path": "eligible.json", "content": ELIGIBLE}
                ),
                expected_outcomes=[LlmFileExists(path="eligible.json")],
                rationale_code="WRITE",
                derived_from=[origem],
            ),
        )
    )
    result = await runtime.run_until_pause(checkpoint.run_id)
    return factory, result


class TestRedacao:
    """Spec §33: nem chain-of-thought, nem secrets."""

    def test_chave_de_segredo_e_redigida(self):
        assert redact({"api_key": "sk-abc123"})["api_key"] == REDACTED
        assert redact({"Authorization": "Bearer x"})["Authorization"] == REDACTED
        assert redact({"user_password": "hunter2"})["user_password"] == REDACTED

    def test_pensamento_nao_entra_no_trace(self):
        payload = {"thinking": "primeiro eu consideraria...", "decision": "ACT"}
        redigido = redact(payload)
        assert redigido["thinking"] == REDACTED
        assert redigido["decision"] == "ACT"

    def test_credencial_em_url_e_removida(self):
        redigido = redact({"url": "https://user:senha@api.exemplo.com/x"})
        assert "senha" not in redigido["url"]
        assert "api.exemplo.com" in redigido["url"]

    def test_token_no_meio_do_texto_e_removido(self):
        redigido = redact("use ghp_abcdefghijklmnopqrstuvwxyz01 para autenticar")
        assert "ghp_" not in redigido

    def test_texto_longo_e_truncado(self):
        redigido = redact("x" * 5000)
        assert len(redigido) < 5000
        assert "5000 chars" in redigido

    def test_redacao_e_recursiva(self):
        redigido = redact({"a": [{"secret": "x"}, {"ok": "y"}]})
        assert redigido["a"][0]["secret"] == REDACTED
        assert redigido["a"][1]["ok"] == "y"

    def test_estrutura_ciclica_nao_trava(self):
        profundo: dict = {"nivel": 0}
        atual = profundo
        for i in range(30):
            atual["filho"] = {"nivel": i + 1}
            atual = atual["filho"]
        assert redact(profundo) is not None


class TestSpans:
    async def test_arvore_de_spans_da_spec(self, engine, registry, sandbox):
        tracer = InMemoryTracer()
        await _rodar(engine, registry, sandbox, tracer=tracer)

        nomes = set(tracer.names())
        assert {
            "perception.collect",
            "controller.decide",
            "tool.execute",
            "verifier.evaluate",
            "memory.store",
        } <= nomes

    async def test_span_carrega_identidade_e_versoes(self, engine, registry, sandbox):
        tracer = InMemoryTracer()
        await _rodar(engine, registry, sandbox, tracer=tracer)

        trace, _ = tracer.spans[0]
        payload = trace.payload()
        assert payload["trace_id"]
        assert payload["cycle_id"]
        assert payload["state_version"] >= 0
        assert payload["v_tool_registry"]
        assert payload["v_model"] == "claude-opus-5"

    async def test_todos_os_spans_compartilham_o_trace_id(self, engine, registry, sandbox):
        tracer = InMemoryTracer()
        await _rodar(engine, registry, sandbox, tracer=tracer)
        assert len({t.context.trace_id for t, _ in tracer.spans}) == 1

    async def test_span_mede_duracao(self, engine, registry, sandbox):
        tracer = InMemoryTracer()
        await _rodar(engine, registry, sandbox, tracer=tracer)
        assert all(s.duration_ms >= 0 for _, s in tracer.spans)

    async def test_span_de_decisao_registra_o_veredito(self, engine, registry, sandbox):
        tracer = InMemoryTracer()
        await _rodar(engine, registry, sandbox, tracer=tracer)
        decide = tracer.by_name("controller.decide")[0]
        assert decide.payload["decision"] == "ACT"
        assert decide.payload["tokens"] > 0

    async def test_span_falho_nao_engole_a_excecao(self):
        """Observabilidade não pode virar mecanismo de perda de erro."""
        from neuroloop.observability import NullTracer, span
        from neuroloop.observability.context import (
            ComponentVersions,
            CycleTrace,
            TraceContext,
        )

        trace = CycleTrace(
            context=TraceContext(
                trace_id="t",
                run_id=uuid4(),
                goal_id=uuid4(),
                cycle_id="c",
                iteration=1,
                phase=RunPhase.EXECUTING,
                state_version=0,
            ),
            versions=ComponentVersions(),
        )
        with pytest.raises(ValueError, match="falhou"):
            async with span(NullTracer(), trace, "x"):
                raise ValueError("falhou")

    async def test_spans_persistidos_no_trace(self, engine, registry, sandbox):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            linhas = await run_timeline(session, result.run_id)
        assert any(linha.kind.startswith("SPAN:") for linha in linhas)


class TestExplicacao:
    """O aceite: por que o agente fez isso?"""

    async def test_explica_acao_executada(self, engine, registry, sandbox):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            explicacoes = await explain_run(session, result.run_id)

        assert len(explicacoes) == 1
        explicacao = explicacoes[0]
        assert explicacao.tool == "filesystem.write"
        assert explicacao.executed is True
        assert explicacao.decision_source == "DELIBERATOR"
        assert explicacao.authorization_reason == "AUTO_APPROVED:R1"
        assert explicacao.attempts[-1].status == "SUCCESS"
        assert explicacao.verification is not None
        assert "filesystem.write" in explicacao.why()
        assert "executada em 1 tentativa" in explicacao.why()

    async def test_explica_acao_barrada(self, engine, registry, sandbox):
        """A explicação da ação que NÃO rodou é a mais importante."""
        factory, result = await _rodar(engine, registry, sandbox, derived_ok=False)
        async with factory() as session:
            explicacoes = await explain_run(session, result.run_id)

        explicacao = explicacoes[0]
        assert explicacao.executed is False
        assert explicacao.tainted is True
        assert "NÃO executada" in explicacao.why()

    async def test_explicacao_carrega_proveniencia_e_identidade(
        self, engine, registry, sandbox
    ):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            explicacao = (await explain_run(session, result.run_id))[0]

        assert explicacao.derived_from
        assert explicacao.fingerprint.startswith("sha256:")
        assert explicacao.idempotency_key.startswith("sha256:")
        assert explicacao.trace_id
        assert explicacao.versions["v_tool_registry"]

    async def test_acao_inexistente_devolve_none(self, engine, registry, sandbox):
        factory, _ = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            assert await explain_action(session, uuid4()) is None

    async def test_linha_do_tempo_legivel(self, engine, registry, sandbox):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            linhas = await run_timeline(session, result.run_id)

        tipos = {linha.kind for linha in linhas}
        assert "PHASE_TRANSITION" in tipos
        assert "ACTION_AUTHORIZATION" in tipos
        transicao = next(l for l in linhas if l.kind == "PHASE_TRANSITION")
        assert "→" in transicao.detail


class TestMetricas:
    def test_denominador_pequeno_nao_vira_percentual(self):
        """C18: 100% sobre duas amostras não informa nada."""
        assert rate(2, 2) is None
        assert rate(20, 20) == 1.0
        assert rate(1, 2, minimum=1) == 0.5

    async def test_metricas_do_run(self, engine, registry, sandbox):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            metricas = await collect_run_metrics(session, result.run_id)

        assert metricas.phase is RunPhase.COMPLETED
        assert metricas.declared_complete is True
        assert metricas.actions_proposed == 1
        assert metricas.actions_executed == 1
        assert metricas.deliberations == 1
        assert metricas.duplicate_side_effects == 0
        assert metricas.dangling_attempts == 0
        assert metricas.tokens_used > 0

    async def test_proposta_recusada_conta_como_insegura(
        self, engine, registry, sandbox
    ):
        factory, result = await _rodar(engine, registry, sandbox, derived_ok=False)
        async with factory() as session:
            metricas = await collect_run_metrics(session, result.run_id)

        assert metricas.actions_proposed == 1
        assert metricas.actions_executed == 0
        assert metricas.declared_complete is False

    async def test_metricas_serializam(self, engine, registry, sandbox):
        factory, result = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            metricas = await collect_run_metrics(session, result.run_id)
        assert json.dumps(metricas.as_dict())

    async def test_run_inexistente_e_erro(self, engine, registry, sandbox):
        factory, _ = await _rodar(engine, registry, sandbox)
        async with factory() as session:
            with pytest.raises(LookupError):
                await collect_run_metrics(session, uuid4())


class TestFingerprints:
    def test_estavel_e_independente_de_ordem_de_chave(self):
        assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})

    def test_muda_com_o_conteudo(self):
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})
