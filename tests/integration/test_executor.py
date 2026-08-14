"""TASK-006. Aceite: attempt antes da chamada, UNKNOWN_EFFECT, duplicate detection.

Cobre também C04 (retry por ação lógica) e C05 (ciclo do probe).
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from factories import NOW, make_checkpoint, make_goal
from neuroloop.core import (
    ActionProposal,
    AttemptStatus,
    ErrorCode,
    ExecutionBudget,
    ExecutionStatus,
    FileExists,
    RiskLevel,
)
from neuroloop.persistence import build_session_factory, models
from neuroloop.persistence.repositories import (
    ActionRepository,
    AgentRepository,
    GoalRepository,
    RunRepository,
)
from neuroloop.runtime import DurableExecutor, RetryPolicy
from neuroloop.tools import EffectProbe, Sandbox, ToolDefinition, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools

PROBE = EffectProbe(
    criterion_template=FileExists(path="{path}"),
    argument_bindings={"path": "path"},
)

SLOW_WRITE = ToolDefinition(
    name="slow.write",
    version="1.0.0",
    description="grava e depois trava — simula timeout com efeito já aplicado",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    risk_level=RiskLevel.R1,
    side_effects=True,
    reversible=False,
    supports_idempotency=True,
    timeout_seconds=0.05,
    capabilities=frozenset({"fs:write"}),
    effect_probe=PROBE,
)

SLOW_NOOP = SLOW_WRITE.model_copy(update={"name": "slow.noop"})


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    return Sandbox(root)


@pytest.fixture
def registry(sandbox) -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg, sandbox)

    async def _slow_write(arguments):
        sandbox.resolve(arguments["path"]).write_text(
            arguments["content"], encoding="utf-8"
        )
        await asyncio.sleep(5)  # trava depois de aplicar o efeito

    async def _slow_noop(arguments):
        await asyncio.sleep(5)  # trava sem aplicar efeito nenhum

    reg.register(SLOW_WRITE, _slow_write)
    reg.register(SLOW_NOOP, _slow_noop)
    return reg


@pytest.fixture
def executor(engine, registry, sandbox) -> DurableExecutor:
    return DurableExecutor(
        session_factory=build_session_factory(engine), registry=registry, sandbox=sandbox
    )


async def seed_run(engine):
    factory = build_session_factory(engine)
    async with factory() as session:
        agent_id = await AgentRepository(session).ensure("exec")
        goal = make_goal(agent_id=agent_id)
        await GoalRepository(session).create(goal)
        checkpoint = await RunRepository(session).create(goal_id=goal.id, started_at=NOW)
        await session.commit()
    return checkpoint


def write_proposal(path: str = "out.json", content: str = "[]", tool: str = "filesystem.write"):
    return ActionProposal(
        tool=tool,
        arguments={"path": path, "content": content},
        expected_outcomes=(FileExists(path=path),),
        rationale_code="WRITE",
    )


class TestAttemptAntesDaChamada:
    """Aceite explícito: o marcador durável precede o efeito."""

    async def test_attempt_in_flight_visivel_durante_a_chamada(
        self, engine, sandbox, registry
    ):
        """O handler consulta o banco e encontra a própria tentativa aberta.

        Prova direta do commit intermediário: se a gravação acontecesse
        junto com o desfecho, não haveria nada para o handler enxergar.
        """
        factory = build_session_factory(engine)
        checkpoint = await seed_run(engine)
        visto: list[str] = []

        async def _spy(arguments):
            async with factory() as session:
                rows = await session.scalars(
                    select(models.ActionAttempt).where(
                        models.ActionAttempt.status == AttemptStatus.IN_FLIGHT.value
                    )
                )
                visto.extend(r.status for r in rows)
            return {"ok": True}

        registry.register(
            SLOW_WRITE.model_copy(update={"name": "spy.write", "timeout_seconds": 5.0}),
            _spy,
        )
        executor = DurableExecutor(
            session_factory=factory, registry=registry, sandbox=sandbox
        )

        outcome = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(tool="spy.write")
        )

        assert visto == [AttemptStatus.IN_FLIGHT.value]
        assert outcome.succeeded is True

    async def test_desfecho_fecha_o_attempt(self, engine, executor, sandbox):
        checkpoint = await seed_run(engine)
        outcome = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal()
        )

        factory = build_session_factory(engine)
        async with factory() as session:
            assert await ActionRepository(session).in_flight_attempts(checkpoint.run_id) == []
        assert outcome.attempt_no == 1
        assert (sandbox.root / "out.json").is_file()


class TestEfeitoDesconhecido:
    """C05: `timeout → UNKNOWN_EFFECT → probe → confirmado/ausente/?`."""

    async def test_timeout_com_efeito_aplicado_vira_sucesso(
        self, engine, executor, sandbox
    ):
        checkpoint = await seed_run(engine)
        outcome = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(tool="slow.write")
        )

        assert outcome.probe_result == "EFFECT_PRESENT"
        assert outcome.result.status is ExecutionStatus.SUCCESS
        assert (sandbox.root / "out.json").is_file()

    async def test_timeout_sem_efeito_vira_falha_transitoria(self, engine, executor, sandbox):
        checkpoint = await seed_run(engine)
        outcome = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(tool="slow.noop")
        )

        assert outcome.probe_result == "EFFECT_ABSENT"
        assert outcome.result.error_code is ErrorCode.TOOL_TRANSIENT_ERROR
        assert not (sandbox.root / "out.json").exists()

    async def test_probe_e_persistido_na_tentativa(self, engine, executor):
        checkpoint = await seed_run(engine)
        outcome = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(tool="slow.noop")
        )

        factory = build_session_factory(engine)
        async with factory() as session:
            attempt = await session.scalar(
                select(models.ActionAttempt).where(
                    models.ActionAttempt.action_id == outcome.action_id
                )
            )
        assert attempt.probe_outcome["satisfied"] is False

    async def test_timeout_sem_efeito_colateral_e_falha_limpa(
        self, engine, registry, sandbox
    ):
        """Sem efeito possível, timeout não precisa de probe."""

        async def _hang(arguments):
            await asyncio.sleep(5)

        registry.register(
            ToolDefinition(
                name="slow.read",
                version="1.0.0",
                description="leitura que trava",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                risk_level=RiskLevel.R0,
                side_effects=False,
                timeout_seconds=0.05,
                capabilities=frozenset({"fs:read"}),
            ),
            _hang,
        )
        checkpoint = await seed_run(engine)
        executor = DurableExecutor(
            session_factory=build_session_factory(engine), registry=registry, sandbox=sandbox
        )

        outcome = await executor.execute(
            run_id=checkpoint.run_id,
            proposal=ActionProposal(
                tool="slow.read",
                arguments={"path": "a.json"},
                expected_outcomes=(FileExists(path="a.json"),),
                rationale_code="READ",
            ),
        )
        assert outcome.result.status is ExecutionStatus.FAILURE
        assert outcome.result.error_code is ErrorCode.TOOL_TIMEOUT
        assert outcome.probe_result is None


class TestDuplicateDetection:
    """C09: at-most-once por ação lógica."""

    async def test_segunda_execucao_da_mesma_acao_e_suprimida(
        self, engine, executor, sandbox
    ):
        checkpoint = await seed_run(engine)
        primeira = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(content="original")
        )
        (sandbox.root / "out.json").write_text("mexido por fora", encoding="utf-8")

        segunda = await executor.execute(
            run_id=checkpoint.run_id,
            proposal=write_proposal(content="original"),
            action_id=primeira.action_id,
        )

        assert segunda.suppressed_duplicate is True
        # o efeito não foi reaplicado
        assert (sandbox.root / "out.json").read_text(encoding="utf-8") == "mexido por fora"

    async def test_acao_logica_diferente_executa_normalmente(
        self, engine, executor, sandbox
    ):
        """Escrever o mesmo arquivo em dois steps é legítimo, não duplicata."""
        checkpoint = await seed_run(engine)
        await executor.execute(run_id=checkpoint.run_id, proposal=write_proposal(content="a"))
        segunda = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal(content="b")
        )

        assert segunda.suppressed_duplicate is False
        assert (sandbox.root / "out.json").read_text(encoding="utf-8") == "b"

    async def test_supressao_nao_cria_nova_tentativa(self, engine, executor):
        checkpoint = await seed_run(engine)
        primeira = await executor.execute(
            run_id=checkpoint.run_id, proposal=write_proposal()
        )
        await executor.execute(
            run_id=checkpoint.run_id,
            proposal=write_proposal(),
            action_id=primeira.action_id,
        )

        factory = build_session_factory(engine)
        async with factory() as session:
            assert await ActionRepository(session).attempt_count(primeira.action_id) == 1


class TestRecuperacaoAposCrash:
    async def test_attempt_em_voo_e_fechado_pelo_probe(self, engine, executor, sandbox):
        """B3: retomada precisa fechar o que ficou aberto, sem reexecutar."""
        factory = build_session_factory(engine)
        checkpoint = await seed_run(engine)

        async with factory() as session:
            action = await ActionRepository(session).create_logical_action(
                run_id=checkpoint.run_id,
                proposal=write_proposal(),
                tool_version="1.0.0",
                risk_level=RiskLevel.R1,
            )
            await ActionRepository(session).start_attempt(action.id, now=NOW)
            await session.commit()

        # o efeito de fato saiu antes do crash
        (sandbox.root / "out.json").write_text("[]", encoding="utf-8")

        outcomes = await executor.recover_in_flight(checkpoint.run_id)

        assert len(outcomes) == 1
        assert outcomes[0].probe_result == "EFFECT_PRESENT"
        async with factory() as session:
            assert await ActionRepository(session).in_flight_attempts(checkpoint.run_id) == []

    async def test_efeito_ausente_apos_crash_e_marcado_transitorio(self, engine, executor):
        factory = build_session_factory(engine)
        checkpoint = await seed_run(engine)
        async with factory() as session:
            action = await ActionRepository(session).create_logical_action(
                run_id=checkpoint.run_id,
                proposal=write_proposal(),
                tool_version="1.0.0",
                risk_level=RiskLevel.R1,
            )
            await ActionRepository(session).start_attempt(action.id, now=NOW)
            await session.commit()

        outcomes = await executor.recover_in_flight(checkpoint.run_id)

        assert outcomes[0].probe_result == "EFFECT_ABSENT"
        assert outcomes[0].result.error_code is ErrorCode.TOOL_TRANSIENT_ERROR


class TestDeteccaoDeLoop:
    async def test_repeticao_sem_progresso_e_loop(self, engine, executor):
        checkpoint = await seed_run(engine)
        proposal = write_proposal()
        factory = build_session_factory(engine)

        fingerprint = None
        for _ in range(3):
            async with factory() as session:
                action = await ActionRepository(session).create_logical_action(
                    run_id=checkpoint.run_id,
                    proposal=proposal,
                    tool_version="1.0.0",
                    risk_level=RiskLevel.R1,
                )
                fingerprint = action.action_fingerprint
                await session.commit()

        assert await executor.detect_loop(
            checkpoint.run_id, fingerprint, progressed_since=False
        )

    async def test_repeticao_com_progresso_nao_e_loop(self, engine, executor):
        checkpoint = await seed_run(engine)
        factory = build_session_factory(engine)
        async with factory() as session:
            action = await ActionRepository(session).create_logical_action(
                run_id=checkpoint.run_id,
                proposal=write_proposal(),
                tool_version="1.0.0",
                risk_level=RiskLevel.R1,
            )
            await session.commit()

        assert not await executor.detect_loop(
            checkpoint.run_id, action.action_fingerprint, progressed_since=True
        )


class TestRetryPolicy:
    """C04: contagem por ação lógica, limite alcançável, retry provado seguro."""

    def _outcome(self, executor_result, logical_id=None, probe_result=None):
        from neuroloop.runtime import ExecutionOutcome

        return ExecutionOutcome(
            action_id=uuid4(),
            logical_action_id=logical_id or uuid4(),
            attempt_no=1,
            result=executor_result,
            probe_result=probe_result,
        )

    def _result(self, status, error_code=None):
        from neuroloop.tools import ToolResult

        if status is ExecutionStatus.SUCCESS:
            return ToolResult.succeeded()
        if status is ExecutionStatus.UNKNOWN:
            return ToolResult.unknown_effect()
        return ToolResult.failed(error_code or ErrorCode.TOOL_TRANSIENT_ERROR)

    def test_sucesso_nao_gera_retry(self):
        decision = RetryPolicy().decide(
            make_checkpoint(),
            SLOW_WRITE,
            self._outcome(self._result(ExecutionStatus.SUCCESS)),
        )
        assert decision.should_retry is False
        assert decision.reason_code == "NO_RETRY_NEEDED"

    def test_falha_transitoria_com_idempotencia_permite_retry(self):
        decision = RetryPolicy().decide(
            make_checkpoint(), SLOW_WRITE, self._outcome(self._result(ExecutionStatus.FAILURE))
        )
        assert decision.should_retry is True

    def test_limite_por_acao_logica(self):
        logical_id = uuid4()
        checkpoint = make_checkpoint(
            budget=ExecutionBudget(max_retries_per_action=2),
            retry_counts={logical_id: 2},
        )
        decision = RetryPolicy().decide(
            checkpoint,
            SLOW_WRITE,
            self._outcome(self._result(ExecutionStatus.FAILURE), logical_id),
        )
        assert decision.should_retry is False
        assert decision.error_code is ErrorCode.RETRY_LIMIT

    def test_limite_de_uma_acao_nao_afeta_outra(self):
        """O contador é por ação, não por run."""
        checkpoint = make_checkpoint(
            budget=ExecutionBudget(max_retries_per_action=2),
            retry_counts={uuid4(): 2},
        )
        decision = RetryPolicy().decide(
            checkpoint, SLOW_WRITE, self._outcome(self._result(ExecutionStatus.FAILURE))
        )
        assert decision.should_retry is True

    def test_falha_permanente_nao_gera_retry(self):
        decision = RetryPolicy().decide(
            make_checkpoint(),
            SLOW_WRITE,
            self._outcome(
                self._result(ExecutionStatus.FAILURE, ErrorCode.TOOL_PERMANENT_ERROR)
            ),
        )
        assert decision.should_retry is False
        assert decision.error_code is ErrorCode.TOOL_PERMANENT_ERROR

    def test_efeito_desconhecido_nunca_e_retry_cego(self):
        """Nem idempotência declarada libera retry com efeito indeterminado."""
        decision = RetryPolicy().decide(
            make_checkpoint(), SLOW_WRITE, self._outcome(self._result(ExecutionStatus.UNKNOWN))
        )
        assert decision.should_retry is False
        assert decision.requires_user is True
        assert decision.error_code is ErrorCode.UNKNOWN_SIDE_EFFECT

    def test_efeito_provado_ausente_libera_retry(self):
        naoidempotente = SLOW_WRITE.model_copy(
            update={"supports_idempotency": False, "max_retries": 0}
        )
        decision = RetryPolicy().decide(
            make_checkpoint(),
            naoidempotente,
            self._outcome(
                self._result(ExecutionStatus.FAILURE), probe_result="EFFECT_ABSENT"
            ),
        )
        assert decision.should_retry is True

    def test_sem_idempotencia_e_sem_prova_pede_humano(self):
        naoidempotente = SLOW_WRITE.model_copy(
            update={"supports_idempotency": False, "max_retries": 0}
        )
        decision = RetryPolicy().decide(
            make_checkpoint(), naoidempotente, self._outcome(self._result(ExecutionStatus.FAILURE))
        )
        assert decision.should_retry is False
        assert decision.requires_user is True
