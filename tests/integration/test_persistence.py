"""TASK-003. Aceite: restart/resume, optimistic locking, migrations.

Cobre também C11 (lease + fencing) e C08 (attempt como marcador durável).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from factories import NOW, make_goal, make_outcome
from neuroloop.core import (
    ActionProposal,
    AllOf,
    AttemptStatus,
    ErrorCode,
    ExecutionBudget,
    FileExists,
    JsonPathCount,
    Observation,
    ObservationSource,
    Plan,
    PlanStep,
    RiskLevel,
    RunPhase,
    TrustLevel,
)
from neuroloop.core.identity import (
    make_action_fingerprint,
    make_idempotency_key,
)
from neuroloop.persistence.errors import (
    LeaseLostError,
    LeaseUnavailableError,
    StateConflictError,
)
from neuroloop.persistence.repositories import (
    ActionRepository,
    AgentRepository,
    EpisodeRepository,
    GoalRepository,
    ObservationRepository,
    PlanRepository,
    RunEventRepository,
    RunRepository,
)
from neuroloop.runtime import RunStateMachine, resume_phase_for


async def _seed_run(session, **run_kwargs):
    agent_id = await AgentRepository(session).ensure("tester")
    goal = make_goal(agent_id=agent_id)
    await GoalRepository(session).create(goal)
    checkpoint = await RunRepository(session).create(
        goal_id=goal.id, started_at=NOW, **run_kwargs
    )
    await session.commit()
    return goal, checkpoint


class TestRoundtripDeGoal:
    async def test_arvore_de_criterios_sobrevive(self, session):
        agent_id = await AgentRepository(session).ensure("tester")
        criterion = AllOf(
            criteria=(
                FileExists(path="/workspace/eligible.json"),
                JsonPathCount(
                    source="FILE",
                    path="/workspace/eligible.json",
                    json_path="$[*]",
                    expected_count=3,
                ),
            )
        )
        goal = make_goal(agent_id=agent_id, success_criteria=(criterion,))
        repo = GoalRepository(session)
        await repo.create(goal)
        await session.commit()

        loaded = await repo.get(goal.id)
        assert loaded.success_criteria == goal.success_criteria
        assert loaded.description == goal.description


class TestRoundtripDeCheckpoint:
    async def test_campos_criticos_sobrevivem(self, session):
        action_id = uuid4()
        goal, checkpoint = await _seed_run(
            session,
            budget=ExecutionBudget(max_iterations=7, cost_budget_usd=Decimal("2.50")),
            baseline_outcomes=(make_outcome(False), make_outcome(None)),
        )
        repo = RunRepository(session)

        mutated = checkpoint.model_copy(
            update={
                "phase": RunPhase.EXECUTING,
                "iteration": 4,
                "retry_counts": {action_id: 2},
                "tokens_used": 1234,
                "cost_used_usd": Decimal("0.125000"),
                "unresolved_effect_action_id": action_id,
            }
        )
        await repo.save(mutated)
        await session.commit()

        loaded = await repo.load(checkpoint.run_id)
        assert loaded.phase is RunPhase.EXECUTING
        assert loaded.iteration == 4
        assert loaded.retry_counts == {action_id: 2}  # chave UUID sobrevive ao JSON
        assert loaded.budget.max_iterations == 7
        assert loaded.budget.cost_budget_usd == Decimal("2.50")
        assert loaded.cost_used_usd == Decimal("0.125000")
        assert loaded.unresolved_effect_action_id == action_id
        assert len(loaded.baseline_outcomes) == 2
        assert loaded.baseline_outcomes[1].satisfied is None  # INDETERMINATE preservado

    async def test_deadline_deriva_do_budget(self, session):
        _, checkpoint = await _seed_run(
            session, budget=ExecutionBudget(wall_clock_seconds=120)
        )
        assert checkpoint.wall_clock_deadline == NOW + timedelta(seconds=120)


class TestOptimisticLocking:
    async def test_save_incrementa_versao(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        assert checkpoint.state_version == 0

        saved = await repo.save(checkpoint.model_copy(update={"iteration": 1}))
        assert saved.state_version == 1

        saved2 = await repo.save(saved.model_copy(update={"iteration": 2}))
        assert saved2.state_version == 2

    async def test_escrita_com_versao_velha_e_rejeitada(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.save(checkpoint.model_copy(update={"iteration": 1}))
        await session.commit()

        # Segundo escritor ainda segurando o checkpoint original.
        with pytest.raises(StateConflictError) as exc:
            await repo.save(checkpoint.model_copy(update={"iteration": 99}))
        assert exc.value.error_code is ErrorCode.STATE_CONFLICT
        assert exc.value.expected == 0

    async def test_estado_perdedor_nao_e_gravado(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.save(checkpoint.model_copy(update={"iteration": 1}))
        await session.commit()
        with pytest.raises(StateConflictError):
            await repo.save(checkpoint.model_copy(update={"iteration": 99}))
        await session.rollback()

        assert (await repo.load(checkpoint.run_id)).iteration == 1


class TestLease:
    async def test_aquisicao_incrementa_epoch(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        lease = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        assert lease.epoch == 1
        assert lease.expires_at == NOW + timedelta(seconds=60)

    async def test_segundo_runner_e_bloqueado(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        await session.commit()

        with pytest.raises(LeaseUnavailableError):
            await repo.acquire_lease(checkpoint.run_id, owner="runner-b", now=NOW)

    async def test_lease_expirada_e_assumida(self, session):
        """Processo morto não trava o run para sempre."""
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        await session.commit()

        later = NOW + timedelta(seconds=61)
        lease_b = await repo.acquire_lease(checkpoint.run_id, owner="runner-b", now=later)
        assert lease_b.epoch == 2

    async def test_mesmo_runner_reassume(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        reacquired = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        assert reacquired.epoch == 2

    async def test_renovacao_apos_takeover_falha(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        lease_a = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        await session.commit()

        later = NOW + timedelta(seconds=61)
        await repo.acquire_lease(checkpoint.run_id, owner="runner-b", now=later)
        await session.commit()

        with pytest.raises(LeaseLostError) as exc:
            await repo.renew_lease(lease_a, now=later)
        assert exc.value.error_code is ErrorCode.LEASE_LOST

    async def test_fencing_bloqueia_escrita_do_dono_antigo(self, session):
        """O caso que o fencing token existe para cobrir.

        Runner A pausa, a lease expira, B assume. Quando A volta, sua escrita
        precisa falhar mesmo que ele carregue o `state_version` correto.
        """
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        lease_a = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        await session.commit()

        later = NOW + timedelta(seconds=61)
        await repo.acquire_lease(checkpoint.run_id, owner="runner-b", now=later)
        await session.commit()

        current = await repo.load(checkpoint.run_id)
        stale = current.model_copy(update={"iteration": 42})
        with pytest.raises(LeaseLostError):
            await repo.save(stale, lease=lease_a)

    async def test_dono_atual_escreve_normalmente(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        lease = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        current = await repo.load(checkpoint.run_id)
        saved = await repo.save(current.model_copy(update={"iteration": 3}), lease=lease)
        assert saved.state_version == current.state_version + 1

    async def test_release_libera_para_outro(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        lease = await repo.acquire_lease(checkpoint.run_id, owner="runner-a", now=NOW)
        await repo.release_lease(lease)
        await session.commit()

        lease_b = await repo.acquire_lease(checkpoint.run_id, owner="runner-b", now=NOW)
        assert lease_b.owner == "runner-b"


class TestCancelamento:
    async def test_cancel_nao_compete_por_state_version(self, session):
        """Pedir cancelamento não pode derrubar a escrita do runner."""
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.request_cancel(checkpoint.run_id)
        await session.commit()

        loaded = await repo.load(checkpoint.run_id)
        assert loaded.cancel_requested is True
        assert loaded.state_version == checkpoint.state_version

        saved = await repo.save(loaded.model_copy(update={"iteration": 1}))
        assert saved.state_version == 1


class TestRetomadaAposRestart:
    """Aceite de restart/resume, ligado à state machine da TASK-002."""

    async def _action(self, session, run_id):
        proposal = ActionProposal(
            tool="http.request",
            arguments={"method": "POST", "url": "http://api/orders"},
            expected_outcomes=(FileExists(path="/workspace/out.json"),),
            rationale_code="CREATE_ORDER",
            derived_from=(uuid4(),),
        )
        return await ActionRepository(session).create_logical_action(
            run_id=run_id,
            proposal=proposal,
            tool_version="1.0.0",
            risk_level=RiskLevel.R2,
        )

    async def test_run_limpo_reinicia_o_ciclo(self, session):
        _, checkpoint = await _seed_run(session)
        repo = RunRepository(session)
        await repo.save(checkpoint.model_copy(update={"phase": RunPhase.DELIBERATING}))
        await session.commit()

        state = await repo.resume_state(checkpoint.run_id)
        assert resume_phase_for(state) is RunPhase.PERCEIVING

    async def test_attempt_em_voo_forca_recovering(self, session):
        _, checkpoint = await _seed_run(session)
        action = await self._action(session, checkpoint.run_id)
        await ActionRepository(session).start_attempt(action.id, now=NOW)
        await RunRepository(session).save(
            checkpoint.model_copy(update={"phase": RunPhase.EXECUTING})
        )
        await session.commit()

        state = await RunRepository(session).resume_state(checkpoint.run_id)
        assert state.has_in_flight_attempt is True
        assert resume_phase_for(state) is RunPhase.RECOVERING

    async def test_acao_nao_verificada_forca_verifying(self, session):
        _, checkpoint = await _seed_run(session)
        action = await self._action(session, checkpoint.run_id)
        attempt = await ActionRepository(session).start_attempt(action.id, now=NOW)
        await ActionRepository(session).finish_attempt(
            attempt.id, status=AttemptStatus.SUCCESS, now=NOW + timedelta(seconds=1)
        )
        await RunRepository(session).save(
            checkpoint.model_copy(
                update={
                    "phase": RunPhase.UPDATING_MEMORY,
                    "last_action_id": action.id,
                    "last_verified_action_id": None,
                }
            )
        )
        await session.commit()

        state = await RunRepository(session).resume_state(checkpoint.run_id)
        assert state.has_in_flight_attempt is False
        assert resume_phase_for(state) is RunPhase.VERIFYING

    async def test_acao_verificada_reinicia_o_ciclo(self, session):
        _, checkpoint = await _seed_run(session)
        action = await self._action(session, checkpoint.run_id)
        await RunRepository(session).save(
            checkpoint.model_copy(
                update={
                    "phase": RunPhase.UPDATING_MEMORY,
                    "last_action_id": action.id,
                    "last_verified_action_id": action.id,
                }
            )
        )
        await session.commit()

        state = await RunRepository(session).resume_state(checkpoint.run_id)
        assert resume_phase_for(state) is RunPhase.PERCEIVING

    async def test_waiting_user_sobrevive_a_restart(self, session):
        """Aceite explícito da TASK-013, garantido já na persistência."""
        _, checkpoint = await _seed_run(session)
        action_id = uuid4()
        await RunRepository(session).save(
            checkpoint.model_copy(
                update={
                    "phase": RunPhase.WAITING_USER,
                    "waiting_reason": "APPROVAL",
                    "pending_approval_action_id": action_id,
                    "pending_approval_fingerprint": "sha256:abc",
                }
            )
        )
        await session.commit()

        loaded = await RunRepository(session).load(checkpoint.run_id)
        state = await RunRepository(session).resume_state(checkpoint.run_id)
        assert resume_phase_for(state) is RunPhase.WAITING_USER
        assert loaded.pending_approval_fingerprint == "sha256:abc"

    async def test_cancelamento_pendente_visivel_na_retomada(self, session):
        _, checkpoint = await _seed_run(session)
        await RunRepository(session).request_cancel(checkpoint.run_id)
        await session.commit()

        state = await RunRepository(session).resume_state(checkpoint.run_id)
        assert state.cancel_requested is True


class TestAcoesETentativas:
    async def _proposal(self, **kwargs):
        defaults = dict(
            tool="filesystem.write",
            arguments={"path": "/workspace/out.json", "content": "[]"},
            expected_outcomes=(FileExists(path="/workspace/out.json"),),
            rationale_code="WRITE",
            derived_from=(uuid4(),),
        )
        defaults.update(kwargs)
        return ActionProposal(**defaults)

    async def test_chave_de_idempotencia_e_estavel_entre_tentativas(self, session):
        """C09: é isso que torna o retry seguro."""
        _, checkpoint = await _seed_run(session)
        repo = ActionRepository(session)
        action = await repo.create_logical_action(
            run_id=checkpoint.run_id,
            proposal=await self._proposal(),
            tool_version="1.0.0",
            risk_level=RiskLevel.R1,
        )
        a1 = await repo.start_attempt(action.id, now=NOW)
        await repo.finish_attempt(
            a1.id, status=AttemptStatus.FAILED, now=NOW + timedelta(seconds=1)
        )
        a2 = await repo.start_attempt(action.id, now=NOW + timedelta(seconds=2))
        await session.commit()

        assert a1.attempt_no == 1
        assert a2.attempt_no == 2
        assert action.idempotency_key == make_idempotency_key(
            checkpoint.run_id, action.logical_action_id
        )
        assert await repo.attempt_count(action.id) == 2

    async def test_attempt_e_gravado_antes_da_chamada(self, session_factory):
        """C08: o marcador precisa sobreviver a um crash durante a chamada.

        Simula o crash descartando a sessão logo após o commit do
        `IN_FLIGHT`, sem nunca escrever o desfecho.
        """
        async with session_factory() as s1:
            _, checkpoint = await _seed_run(s1)
            action = await ActionRepository(s1).create_logical_action(
                run_id=checkpoint.run_id,
                proposal=await self._proposal(),
                tool_version="1.0.0",
                risk_level=RiskLevel.R2,
            )
            await ActionRepository(s1).start_attempt(action.id, now=NOW)
            await s1.commit()  # commit ANTES da chamada externa

        async with session_factory() as s2:
            in_flight = await ActionRepository(s2).in_flight_attempts(checkpoint.run_id)
            assert len(in_flight) == 1
            assert in_flight[0].status == AttemptStatus.IN_FLIGHT.value
            state = await RunRepository(s2).resume_state(checkpoint.run_id)
            assert resume_phase_for(state) is RunPhase.RECOVERING

    async def test_finish_registra_duracao_e_erro(self, session):
        _, checkpoint = await _seed_run(session)
        repo = ActionRepository(session)
        action = await repo.create_logical_action(
            run_id=checkpoint.run_id,
            proposal=await self._proposal(),
            tool_version="1.0.0",
            risk_level=RiskLevel.R1,
        )
        attempt = await repo.start_attempt(action.id, now=NOW)
        await repo.finish_attempt(
            attempt.id,
            status=AttemptStatus.UNKNOWN,
            now=NOW + timedelta(milliseconds=250),
            error_code=ErrorCode.TOOL_TIMEOUT,
            probe_outcome={"result": "EFFECT_ABSENT"},
        )
        await session.commit()

        assert attempt.duration_ms == 250
        assert attempt.error_code == ErrorCode.TOOL_TIMEOUT.value
        assert attempt.probe_outcome == {"result": "EFFECT_ABSENT"}
        assert await repo.in_flight_attempts(checkpoint.run_id) == []

    async def test_fingerprint_conta_repeticoes_para_loop(self, session):
        """C09: fingerprint detecta loop, não deduplica execução."""
        _, checkpoint = await _seed_run(session)
        repo = ActionRepository(session)
        proposal = await self._proposal()
        for _ in range(3):
            await repo.create_logical_action(
                run_id=checkpoint.run_id,
                proposal=proposal,
                tool_version="1.0.0",
                risk_level=RiskLevel.R1,
            )
        await session.commit()

        fingerprint = make_action_fingerprint(
            tool=proposal.tool,
            tool_version="1.0.0",
            arguments=proposal.arguments,
            target_resource=None,
        )
        assert await repo.count_fingerprint(checkpoint.run_id, fingerprint) == 3

    async def test_acoes_repetidas_tem_chaves_de_idempotencia_distintas(self, session):
        """Escrever o mesmo arquivo em dois steps é legítimo, não duplicata."""
        _, checkpoint = await _seed_run(session)
        repo = ActionRepository(session)
        proposal = await self._proposal()
        a = await repo.create_logical_action(
            run_id=checkpoint.run_id,
            proposal=proposal,
            tool_version="1.0.0",
            risk_level=RiskLevel.R1,
        )
        b = await repo.create_logical_action(
            run_id=checkpoint.run_id,
            proposal=proposal,
            tool_version="1.0.0",
            risk_level=RiskLevel.R1,
        )
        await session.commit()

        assert a.action_fingerprint == b.action_fingerprint
        assert a.idempotency_key != b.idempotency_key
        assert await repo.find_by_idempotency_key(a.idempotency_key) is not None


class TestPlanos:
    async def test_replace_active_invalida_o_anterior(self, session):
        _, checkpoint = await _seed_run(session)
        repo = PlanRepository(session)
        step = PlanStep(
            id="s1",
            description="ler entrada",
            expected_outcomes=(FileExists(path="/workspace/in.json"),),
        )
        v1 = Plan(
            id=uuid4(),
            version=1,
            objective="obj",
            steps=(step,),
            completion_condition="pronto",
        )
        v2 = v1.model_copy(update={"id": uuid4(), "version": 2})

        await repo.replace_active(checkpoint.run_id, v1, now=NOW)
        await repo.replace_active(checkpoint.run_id, v2, now=NOW)
        await session.commit()

        active = await repo.active(checkpoint.run_id)
        assert active is not None
        assert active.version == 2

    async def test_invalidate_deixa_run_sem_plano(self, session):
        _, checkpoint = await _seed_run(session)
        repo = PlanRepository(session)
        plan = Plan(
            id=uuid4(),
            version=1,
            objective="obj",
            steps=(
                PlanStep(
                    id="s1",
                    description="ler",
                    expected_outcomes=(FileExists(path="/a"),),
                ),
            ),
            completion_condition="pronto",
        )
        await repo.replace_active(checkpoint.run_id, plan, now=NOW)
        await repo.invalidate_active(checkpoint.run_id, now=NOW)
        await session.commit()

        assert await repo.active(checkpoint.run_id) is None


class TestObservacoesEEpisodios:
    async def test_pendentes_e_consumo(self, session):
        _, checkpoint = await _seed_run(session)
        repo = ObservationRepository(session)
        obs = Observation(
            id=uuid4(),
            run_id=checkpoint.run_id,
            source=ObservationSource.TOOL,
            kind="file_content",
            content={"itens": 3},
            content_hash="sha256:xyz",
            trust=TrustLevel.UNTRUSTED_EXTERNAL,
            tags=("filesystem",),
            occurred_at=NOW,
            received_at=NOW,
        )
        await repo.append(obs)
        await session.commit()

        pending = await repo.pending(checkpoint.run_id)
        assert len(pending) == 1
        assert pending[0].trust is TrustLevel.UNTRUSTED_EXTERNAL
        assert pending[0].content == {"itens": 3}
        assert pending[0].is_untrusted is True

        await repo.mark_consumed([obs.id], now=NOW)
        await session.commit()
        assert await repo.pending(checkpoint.run_id) == []

    async def test_episodio_e_unico_por_iteracao(self, session):
        _, checkpoint = await _seed_run(session)
        repo = EpisodeRepository(session)
        await repo.record(
            run_id=checkpoint.run_id,
            iteration=1,
            goal_summary="g",
            observation_summary="o",
            decision_type="ACT",
            result_summary="ok",
            verification={"goal_satisfied": False},
            importance=0.7,
            tool_name="filesystem.read",
        )
        await session.commit()

        episodes = await repo.by_run(checkpoint.run_id)
        assert len(episodes) == 1
        assert episodes[0].importance == 0.7

    async def test_transicao_vira_evento_de_trace(self, session):
        _, checkpoint = await _seed_run(session)
        machine = RunStateMachine()
        record = machine.transition(RunPhase.PERCEIVING, reason="run iniciado", at=NOW)

        repo = RunEventRepository(session)
        await repo.record_transition(
            run_id=checkpoint.run_id,
            iteration=0,
            from_phase=record.from_phase.value,
            to_phase=record.to_phase.value,
            reason=record.reason,
            at=record.at,
            trace_id="trace-1",
        )
        await session.commit()

        events = await repo.by_run(checkpoint.run_id)
        assert len(events) == 1
        assert events[0].from_phase == RunPhase.CREATED.value
        assert events[0].to_phase == RunPhase.PERCEIVING.value
        assert events[0].trace_id == "trace-1"
