"""TASK-002. Aceite: só transições permitidas, terminal não reabre, cancelamento.

Os testes de alcançabilidade existem porque a spec original tinha estados
mortos (`WAITING_EXTERNAL`, `BLOCKED`) que passaram despercebidos por não
haver checagem estrutural do grafo.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest

from factories import NOW
from neuroloop.core import ErrorCode, NextAction, RunPhase
from neuroloop.runtime import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_PHASES,
    RunStateMachine,
    TransitionError,
    can_transition,
    derive_resume_phase,
    phase_for_next_action,
    reachable_phases,
)
from neuroloop.runtime.state_machine import assert_transition, validate_transition_table

P = RunPhase

# Tabela da correção C06, redigitada aqui de propósito: o teste é a spec.
# Divergência entre esta tabela e a do módulo é falha de build.
C06_EDGES = {
    (P.CREATED, P.PERCEIVING),
    (P.CREATED, P.CANCELLED),
    (P.PERCEIVING, P.DELIBERATING),
    (P.PERCEIVING, P.RECOVERING),
    (P.PERCEIVING, P.COMPLETED),
    (P.PERCEIVING, P.WAITING_USER),
    (P.PERCEIVING, P.FAILED),
    (P.PERCEIVING, P.CANCELLED),
    (P.DELIBERATING, P.PLANNING),
    (P.DELIBERATING, P.EXECUTING),
    (P.DELIBERATING, P.WAITING_USER),
    (P.DELIBERATING, P.FAILED),
    (P.PLANNING, P.PERCEIVING),
    (P.PLANNING, P.FAILED),
    (P.EXECUTING, P.VERIFYING),
    (P.EXECUTING, P.RECOVERING),
    (P.RECOVERING, P.VERIFYING),
    (P.RECOVERING, P.WAITING_USER),
    (P.VERIFYING, P.UPDATING_MEMORY),
    (P.UPDATING_MEMORY, P.PERCEIVING),
    (P.UPDATING_MEMORY, P.COMPLETED),
    (P.UPDATING_MEMORY, P.FAILED),
    (P.UPDATING_MEMORY, P.WAITING_USER),
    (P.WAITING_USER, P.PERCEIVING),
    (P.WAITING_USER, P.CANCELLED),
}


class TestTabela:
    def test_tabela_bate_com_a_correcao_c06(self):
        actual = {(src, dst) for src, dsts in ALLOWED_TRANSITIONS.items() for dst in dsts}
        assert actual == C06_EDGES

    def test_tabela_e_estruturalmente_sa(self):
        validate_transition_table(ALLOWED_TRANSITIONS)

    def test_estados_removidos_da_v0_nao_existem(self):
        """`WAITING_EXTERNAL` e `BLOCKED` voltam na V0.5 com o scheduler."""
        nomes = {p.value for p in RunPhase}
        assert "WAITING_EXTERNAL" not in nomes
        assert "BLOCKED" not in nomes


class TestAlcancabilidade:
    def test_nenhum_estado_morto(self):
        assert reachable_phases(P.CREATED) == set(RunPhase)

    @pytest.mark.parametrize("phase", list(RunPhase), ids=lambda p: p.value)
    def test_todo_estado_alcanca_um_terminal(self, phase):
        """Nenhum sumidouro: de qualquer fase existe caminho para encerrar."""
        assert any(p.is_terminal for p in reachable_phases(phase))

    def test_ciclo_principal_fecha(self):
        cycle = [
            P.PERCEIVING,
            P.DELIBERATING,
            P.EXECUTING,
            P.VERIFYING,
            P.UPDATING_MEMORY,
            P.PERCEIVING,
        ]
        assert all(can_transition(a, b) for a, b in pairwise(cycle))


class TestTransicoesProibidas:
    def test_pular_verificacao_e_proibido(self):
        """Executor não conclui: só o Verifier decide conclusão."""
        assert not can_transition(P.EXECUTING, P.COMPLETED)
        assert not can_transition(P.EXECUTING, P.UPDATING_MEMORY)

    def test_deliberar_nao_executa_sem_passar_por_autorizacao(self):
        assert not can_transition(P.PERCEIVING, P.EXECUTING)

    def test_planejar_nao_executa_direto(self):
        """PLANNING volta ao ciclo; quem decide agir é o Controller."""
        assert not can_transition(P.PLANNING, P.EXECUTING)

    def test_transicao_invalida_levanta_state_conflict(self):
        with pytest.raises(TransitionError) as exc:
            assert_transition(P.PERCEIVING, P.VERIFYING)
        assert exc.value.error_code is ErrorCode.STATE_CONFLICT
        assert ErrorCode.STATE_CONFLICT.value in str(exc.value)


class TestEstadosTerminais:
    @pytest.mark.parametrize(
        "terminal", [P.COMPLETED, P.FAILED, P.CANCELLED], ids=lambda p: p.value
    )
    def test_terminal_nao_tem_saida(self, terminal):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()

    @pytest.mark.parametrize(
        "terminal", [P.COMPLETED, P.FAILED, P.CANCELLED], ids=lambda p: p.value
    )
    def test_terminal_nao_reabre(self, terminal):
        machine = RunStateMachine(phase=terminal)
        assert machine.is_terminal
        with pytest.raises(TransitionError, match="terminal não reabre"):
            machine.transition(P.PERCEIVING, reason="tentativa de reabrir")

    def test_terminal_ignora_cancelamento(self):
        machine = RunStateMachine(phase=P.COMPLETED)
        assert machine.request_cancel() is False
        assert machine.phase is P.COMPLETED


class TestMaquina:
    def test_registra_historico_e_caminho(self):
        m = RunStateMachine()
        m.transition(P.PERCEIVING, reason="run iniciado", at=NOW)
        m.transition(P.DELIBERATING, reason="contexto pronto", at=NOW + timedelta(seconds=1))
        assert m.phase is P.DELIBERATING
        assert m.path() == (P.CREATED, P.PERCEIVING, P.DELIBERATING)
        assert m.history[0].from_phase is P.CREATED
        assert m.history[1].reason == "contexto pronto"

    def test_registro_e_imutavel(self):
        from pydantic import ValidationError

        m = RunStateMachine()
        record = m.transition(P.PERCEIVING, reason="run iniciado", at=NOW)
        with pytest.raises(ValidationError):
            record.to_phase = P.FAILED

    def test_can_respeita_terminal(self):
        assert RunStateMachine(phase=P.CREATED).can(P.PERCEIVING)
        assert not RunStateMachine(phase=P.CANCELLED).can(P.PERCEIVING)

    def test_falha_carrega_error_code(self):
        m = RunStateMachine(phase=P.PERCEIVING)
        record = m.transition(
            P.FAILED, reason="budget", error_code=ErrorCode.BUDGET_EXCEEDED, at=NOW
        )
        assert record.error_code is ErrorCode.BUDGET_EXCEEDED


class TestCancelamento:
    @pytest.mark.parametrize("phase", sorted(CANCELLABLE_PHASES, key=lambda p: p.value))
    def test_cancelamento_imediato_nas_fases_seguras(self, phase):
        m = RunStateMachine(phase=phase)
        assert m.request_cancel(at=NOW) is True
        assert m.phase is P.CANCELLED
        assert m.history[-1].error_code is ErrorCode.CANCELLED

    @pytest.mark.parametrize(
        "phase",
        [P.DELIBERATING, P.PLANNING, P.EXECUTING, P.VERIFYING, P.RECOVERING, P.UPDATING_MEMORY],
        ids=lambda p: p.value,
    )
    def test_cancelamento_diferido_no_meio_do_ciclo(self, phase):
        """Não se cancela no meio de um efeito externo: fica indeterminado.

        O pedido não se perde — `cancel_requested` persiste no checkpoint e é
        honrado no topo do próximo ciclo.
        """
        m = RunStateMachine(phase=phase)
        assert m.request_cancel(at=NOW) is False
        assert m.phase is phase

    def test_cancelamento_diferido_e_honrado_no_topo_do_ciclo(self):
        m = RunStateMachine(phase=P.EXECUTING)
        assert m.request_cancel(at=NOW) is False
        m.transition(P.VERIFYING, reason="efeito resolvido", at=NOW)
        m.transition(P.UPDATING_MEMORY, reason="verificado", at=NOW)
        m.transition(P.PERCEIVING, reason="CONTINUE", at=NOW)
        assert m.request_cancel(at=NOW) is True
        assert m.phase is P.CANCELLED

    def test_waiting_user_cancela_imediatamente(self):
        m = RunStateMachine(phase=P.WAITING_USER)
        assert m.request_cancel(at=NOW) is True


class TestVereditoDoVerifier:
    @pytest.mark.parametrize(
        ("next_action", "expected"),
        [
            (NextAction.CONTINUE, P.PERCEIVING),
            (NextAction.RETRY, P.PERCEIVING),
            (NextAction.REPLAN, P.PERCEIVING),
            (NextAction.ASK_USER, P.WAITING_USER),
            (NextAction.GOAL_COMPLETED, P.COMPLETED),
            (NextAction.STOP_FAILURE, P.FAILED),
        ],
    )
    def test_mapeamento(self, next_action, expected):
        assert phase_for_next_action(next_action) is expected

    @pytest.mark.parametrize("next_action", list(NextAction))
    def test_todo_veredito_e_transicao_valida_de_updating_memory(self, next_action):
        assert can_transition(P.UPDATING_MEMORY, phase_for_next_action(next_action))


class TestRetomadaAposCrash:
    """Correção C08 e os quatro cenários de recovery da spec §29."""

    def _resume(self, phase, **flags):
        defaults = {
            "has_in_flight_attempt": False,
            "unresolved_effect": False,
            "has_unverified_action": False,
        }
        return derive_resume_phase(phase, **(defaults | flags))

    def test_crash_antes_da_tool_com_fase_executing_exige_probe(self):
        """Conservador: nada prova que a chamada não saiu."""
        assert self._resume(P.EXECUTING) is P.RECOVERING

    def test_crash_durante_a_tool(self):
        assert self._resume(P.EXECUTING, has_in_flight_attempt=True) is P.RECOVERING

    def test_crash_apos_a_tool_antes_do_verifier(self):
        assert self._resume(P.VERIFYING, has_unverified_action=True) is P.VERIFYING

    def test_crash_apos_verifier_antes_do_checkpoint(self):
        """Checkpoint não gravado: reverifica, que é read-only e idempotente."""
        assert self._resume(P.UPDATING_MEMORY, has_unverified_action=True) is P.VERIFYING

    def test_efeito_pendente_tem_precedencia_sobre_verificacao(self):
        assert (
            self._resume(P.PERCEIVING, unresolved_effect=True, has_unverified_action=True)
            is P.RECOVERING
        )

    def test_waiting_user_sobrevive_a_restart(self):
        assert self._resume(P.WAITING_USER) is P.WAITING_USER

    @pytest.mark.parametrize(
        "phase", [P.CREATED, P.PERCEIVING, P.DELIBERATING, P.PLANNING], ids=lambda p: p.value
    )
    def test_fases_sem_efeito_pendente_reiniciam_o_ciclo(self, phase):
        assert self._resume(phase) is P.PERCEIVING

    @pytest.mark.parametrize(
        "terminal", [P.COMPLETED, P.FAILED, P.CANCELLED], ids=lambda p: p.value
    )
    def test_terminal_nao_e_retomado(self, terminal):
        assert self._resume(terminal, has_in_flight_attempt=True) is terminal

    @pytest.mark.parametrize("phase", list(RunPhase), ids=lambda p: p.value)
    def test_retomada_sempre_produz_fase_operavel(self, phase):
        """Toda combinação de flags precisa cair em fase válida do grafo."""
        for flags in [
            {},
            {"has_in_flight_attempt": True},
            {"unresolved_effect": True},
            {"has_unverified_action": True},
        ]:
            resumed = self._resume(phase, **flags)
            assert resumed.is_terminal or ALLOWED_TRANSITIONS[resumed]
