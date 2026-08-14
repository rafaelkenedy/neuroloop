"""TASK-008. Retrieval top-k por SQL + ranking determinístico, sem embeddings."""

from __future__ import annotations

from datetime import timedelta

import pytest

from factories import NOW, make_goal
from neuroloop.core import ErrorCode, ExecutionStatus, NextAction, RiskLevel
from neuroloop.core.verification import VerificationResult
from neuroloop.memory import (
    EpisodeRecorder,
    MemoryRetriever,
    RetrievalQuery,
    build_episode_tags,
    query_from_context,
)
from neuroloop.persistence import models
from neuroloop.persistence.repositories import (
    AgentRepository,
    GoalRepository,
    RunRepository,
)


def verificacao(
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    error: ErrorCode | None = None,
    outcomes_ok: bool | None = True,
    next_action: NextAction = NextAction.CONTINUE,
) -> VerificationResult:
    return VerificationResult(
        execution_status=status,
        expected_outcomes_satisfied=outcomes_ok,
        confidence=1.0,
        reward_signal=0.5 if outcomes_ok else -0.5,
        error_code=error,
        next_action=next_action,
    )


async def novo_run(session, nome: str = "mem"):
    agent_id = await AgentRepository(session).ensure(f"{nome}-{id(session) % 9999}")
    goal = make_goal(agent_id=agent_id)
    await GoalRepository(session).create(goal)
    checkpoint = await RunRepository(session).create(goal_id=goal.id, started_at=NOW)
    await session.commit()
    return checkpoint


async def gravar(
    session,
    run_id,
    *,
    iteration: int = 1,
    tool: str = "filesystem.write",
    resource: str | None = "out.json",
    error: ErrorCode | None = None,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    risk: RiskLevel = RiskLevel.R1,
    created_at=None,
):
    episode_id = await EpisodeRecorder(session).record(
        run_id=run_id,
        iteration=iteration,
        goal_summary="gerar artefato",
        observation_summary=f"step via {tool}",
        verification=verificacao(
            status=status,
            error=error,
            outcomes_ok=None if error else True,
            next_action=NextAction.ASK_USER if error else NextAction.CONTINUE,
        ),
        tool_name=tool,
        resource=resource,
        risk_level=risk,
    )
    if created_at is not None:
        row = await session.get(models.Episode, episode_id)
        row.created_at = created_at
    await session.commit()
    return episode_id


class TestTags:
    def test_vocabulario_e_prefixado(self):
        tags = build_episode_tags(
            tool_name="filesystem.write",
            resource="out.json",
            error_code=ErrorCode.TOOL_TIMEOUT,
            succeeded=False,
        )
        assert "tool:filesystem.write" in tags
        assert "resource:out.json" in tags
        assert "error:TOOL_TIMEOUT" in tags
        assert "outcome:FAILURE" in tags
        assert "decision:ACT" in tags

    def test_sem_erro_marca_sucesso(self):
        tags = build_episode_tags(tool_name="filesystem.read", succeeded=True)
        assert "outcome:SUCCESS" in tags
        assert not any(t.startswith("error:") for t in tags)


class TestGravacao:
    async def test_importancia_vem_da_verificacao(self, session):
        run = await novo_run(session)
        sucesso = await gravar(session, run.run_id, iteration=1, risk=RiskLevel.R0)
        falha = await gravar(
            session,
            run.run_id,
            iteration=2,
            error=ErrorCode.TOOL_TIMEOUT,
            status=ExecutionStatus.UNKNOWN,
            risk=RiskLevel.R2,
        )

        bom = await session.get(models.Episode, sucesso)
        ruim = await session.get(models.Episode, falha)
        # Episódio de falha carrega mais informação reutilizável (C14).
        assert ruim.importance > bom.importance

    async def test_reward_da_verificacao_e_persistido(self, session):
        run = await novo_run(session)
        episode_id = await gravar(session, run.run_id)
        row = await session.get(models.Episode, episode_id)
        assert row.reward == 0.5  # o recorder persiste o reward do Verifier


class TestRetrieval:
    async def test_query_vazia_nao_devolve_nada(self, session):
        """Sem sinal de relevância, top-k viraria 'os mais recentes'."""
        run = await novo_run(session)
        await gravar(session, run.run_id)

        assert await MemoryRetriever(session).retrieve(RetrievalQuery()) == []

    async def test_exclui_o_run_corrente(self, session):
        """Reler os próprios episódios não é reuso."""
        run = await novo_run(session)
        await gravar(session, run.run_id)

        query = query_from_context(run_id=run.run_id, tools=("filesystem.write",))
        assert await MemoryRetriever(session).retrieve(query) == []

    async def test_recupera_episodio_de_outro_run(self, session):
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(session, anterior.run_id, tool="filesystem.write", resource="out.json")

        query = query_from_context(
            run_id=atual.run_id, tools=("filesystem.write",), resources=("out.json",)
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)

        assert len(memories) == 1
        assert memories[0].tool_name == "filesystem.write"
        assert memories[0].score > 0.5
        assert any("tool:" in r for r in memories[0].match_reasons)

    async def test_episodio_irrelevante_e_cortado(self, session):
        """Abaixo do piso é ruído: entra no prompt sem ajudar."""
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(session, anterior.run_id, tool="http.get", resource="clientes")

        query = query_from_context(
            run_id=atual.run_id, tools=("filesystem.write",), resources=("out.json",)
        )
        assert await MemoryRetriever(session).retrieve(query, now=NOW) == []

    async def test_match_por_codigo_de_erro(self, session):
        """Ao repetir um erro, o episódio da falha anterior é o mais útil."""
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(
            session,
            anterior.run_id,
            iteration=1,
            tool="http.get",
            error=ErrorCode.TOOL_TRANSIENT_ERROR,
            status=ExecutionStatus.FAILURE,
        )
        await gravar(session, anterior.run_id, iteration=2, tool="http.get")

        query = query_from_context(
            run_id=atual.run_id,
            tools=("http.get",),
            error_codes=(ErrorCode.TOOL_TRANSIENT_ERROR,),
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)

        assert memories[0].error_code == ErrorCode.TOOL_TRANSIENT_ERROR.value

    async def test_respeita_o_limite_top_k(self, session):
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        for i in range(8):
            await gravar(session, anterior.run_id, iteration=i + 1)

        query = query_from_context(
            run_id=atual.run_id, tools=("filesystem.write",), resources=("out.json",)
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)
        assert len(memories) == 5  # spec §16: top-k = 5

    async def test_recencia_desempata(self, session):
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        antigo = await gravar(
            session, anterior.run_id, iteration=1, created_at=NOW - timedelta(days=30)
        )
        recente = await gravar(
            session, anterior.run_id, iteration=2, created_at=NOW - timedelta(hours=1)
        )

        query = query_from_context(
            run_id=atual.run_id, tools=("filesystem.write",), resources=("out.json",)
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)

        ids = [m.episode_id for m in memories]
        assert ids.index(recente) < ids.index(antigo)

    async def test_episodio_antigo_nao_e_descartado(self, session):
        """Decaimento é exponencial, não corte: o velho pesa menos, não some."""
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(
            session, anterior.run_id, created_at=NOW - timedelta(days=30)
        )

        query = query_from_context(
            run_id=atual.run_id, tools=("filesystem.write",), resources=("out.json",)
        )
        assert len(await MemoryRetriever(session).retrieve(query, now=NOW)) == 1

    async def test_score_e_ordenado_desc(self, session):
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(session, anterior.run_id, iteration=1, tool="filesystem.write")
        await gravar(session, anterior.run_id, iteration=2, tool="filesystem.read")

        query = query_from_context(
            run_id=atual.run_id,
            tools=("filesystem.write", "filesystem.read"),
            resources=("out.json",),
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)
        scores = [m.score for m in memories]
        assert scores == sorted(scores, reverse=True)


class TestNormalizacaoDoMatch:
    """Query sem dimensão X não pode tetar o score de todo mundo."""

    async def test_query_so_com_tool_atinge_score_alto(self, session):
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(session, anterior.run_id, tool="filesystem.write")

        query = RetrievalQuery(
            tools=frozenset({"filesystem.write"}), exclude_run_id=atual.run_id
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)

        assert memories
        # match estrutural pleno: a única dimensão perguntada bateu
        assert memories[0].score >= 0.5 + 0.3 * memories[0].importance

    async def test_episodio_de_falha_compete_com_episodio_comum(self, session):
        """Sem normalização, episódios de erro nunca apareceriam."""
        anterior = await novo_run(session, "a")
        atual = await novo_run(session, "b")
        await gravar(
            session,
            anterior.run_id,
            iteration=1,
            error=ErrorCode.TOOL_TIMEOUT,
            status=ExecutionStatus.FAILURE,
        )

        query = RetrievalQuery(
            tools=frozenset({"filesystem.write"}), exclude_run_id=atual.run_id
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)
        assert memories and memories[0].error_code == ErrorCode.TOOL_TIMEOUT.value


class TestReusoEntreRuns:
    """Base do H3/B5: run B encontra o que o run A aprendeu."""

    async def test_run_semelhante_encontra_episodios_do_anterior(self, session):
        run_a = await novo_run(session, "a")
        for i, tool in enumerate(("filesystem.read", "filesystem.write")):
            await gravar(
                session, run_a.run_id, iteration=i + 1, tool=tool, resource="pedidos.json"
            )

        run_b = await novo_run(session, "b")
        query = query_from_context(
            run_id=run_b.run_id,
            tools=("filesystem.read", "filesystem.write"),
            resources=("pedidos.json",),
        )
        memories = await MemoryRetriever(session).retrieve(query, now=NOW)

        assert len(memories) == 2
        assert {m.run_id for m in memories} == {run_a.run_id}
        assert all(m.score > 0.5 for m in memories)
