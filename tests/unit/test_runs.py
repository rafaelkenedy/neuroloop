"""Correção C03: checkpoint com os limites que o loop realmente lê."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factories import NOW, make_checkpoint, make_outcome
from neuroloop.core import ExecutionBudget, RunPhase


class TestExecutionBudget:
    def test_defaults_da_spec(self):
        b = ExecutionBudget()
        assert b.max_iterations == 30
        assert b.max_replans == 3
        assert b.max_retries_per_action == 2
        assert b.token_budget == 100_000
        assert b.wall_clock_seconds == 900
        assert b.cost_budget_usd is None

    def test_budget_e_imutavel(self):
        with pytest.raises(ValidationError):
            ExecutionBudget().max_iterations = 999

    def test_valores_invalidos_rejeitados(self):
        with pytest.raises(ValidationError):
            ExecutionBudget(max_iterations=0)
        with pytest.raises(ValidationError):
            ExecutionBudget(cost_budget_usd=Decimal("0"))


class TestCheckpoint:
    def test_loop_encontra_os_limites_no_checkpoint(self):
        """A spec original lia run.max_iterations sem que o campo existisse."""
        cp = make_checkpoint()
        assert cp.budget.max_iterations == 30
        assert cp.cancel_requested is False

    def test_retry_e_contado_por_acao(self):
        a, b = uuid4(), uuid4()
        cp = make_checkpoint(retry_counts={a: 2, b: 0})
        assert cp.retry_counts[a] == 2

    def test_versao_de_plano_exige_plano(self):
        with pytest.raises(ValidationError):
            make_checkpoint(active_plan_version=2)

    def test_fingerprint_de_aprovacao_exige_acao(self):
        with pytest.raises(ValidationError):
            make_checkpoint(pending_approval_fingerprint="sha256:abc")

    def test_replan_nao_excede_geracoes_de_plano(self):
        with pytest.raises(ValidationError):
            make_checkpoint(plan_generation_count=1, replan_count=2)

    def test_fase_terminal(self):
        assert RunPhase.COMPLETED.is_terminal
        assert RunPhase.FAILED.is_terminal
        assert RunPhase.CANCELLED.is_terminal
        assert not RunPhase.EXECUTING.is_terminal


class TestBaselineDeGoal:
    """Correção C02: goal já satisfeito antes do run não é sucesso do run."""

    def test_baseline_totalmente_satisfeito_marca_pre_satisfied(self):
        cp = make_checkpoint(baseline_outcomes=(make_outcome(True), make_outcome(True)))
        assert cp.pre_satisfied is True

    def test_baseline_parcial_nao_marca(self):
        cp = make_checkpoint(baseline_outcomes=(make_outcome(True), make_outcome(False)))
        assert cp.pre_satisfied is False

    def test_baseline_indeterminado_nao_marca(self):
        cp = make_checkpoint(baseline_outcomes=(make_outcome(None),))
        assert cp.pre_satisfied is False

    def test_sem_baseline_nao_marca(self):
        assert make_checkpoint().pre_satisfied is False


class TestLimites:
    def test_iteracoes_esgotam_budget(self):
        cp = make_checkpoint(iteration=30)
        assert cp.budget_exhausted(NOW) is True

    def test_tokens_esgotam_budget(self):
        cp = make_checkpoint(tokens_used=100_000)
        assert cp.budget_exhausted(NOW) is True

    def test_deadline_esgota_budget(self):
        cp = make_checkpoint()
        assert cp.budget_exhausted(NOW + timedelta(seconds=901)) is True
        assert cp.budget_exhausted(NOW) is False

    def test_custo_esgota_budget(self):
        cp = make_checkpoint(
            budget=ExecutionBudget(cost_budget_usd=Decimal("1.00")),
            cost_used_usd=Decimal("1.00"),
        )
        assert cp.budget_exhausted(NOW) is True

    def test_cost_pressure_usa_o_maior_fator(self):
        cp = make_checkpoint(iteration=3, tokens_used=50_000)
        # tokens: 0.5; iterações: 0.1; tempo: 0.0
        assert cp.cost_pressure(NOW) == pytest.approx(0.5)

    def test_cost_pressure_satura_em_um(self):
        cp = make_checkpoint(tokens_used=500_000)
        assert cp.cost_pressure(NOW) == 1.0
