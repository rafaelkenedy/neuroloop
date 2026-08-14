"""TASK-015. Executa B1–B5 e reporta com intervalo de confiança (C18).

`SEEDS` é baixo aqui de propósito: a suíte roda a cada commit e precisa ser
rápida. O harness **avisa** quando N está abaixo de 30, e o alvo de
`false_success_rate` é explicitamente tratado como não conclusivo nesse
regime — dizer o que não foi medido é parte da medição.

Para uma corrida completa:

    NEUROLOOP_BENCH_SEEDS=30 python -m pytest tests/benchmarks -k benchmark -s
"""

from __future__ import annotations

import os

import pytest
from harness import run_benchmark, wilson_interval
from scenarios import run_b1, run_b2, run_b3, run_b4, run_b5

SEEDS = int(os.environ.get("NEUROLOOP_BENCH_SEEDS", "3"))

NOTA_BANCO = (
    "mundo por seed em SQLite; o runtime é verificado contra PostgreSQL pela "
    "suíte de integração, aqui se mede comportamento do agente",
)


class TestWilson:
    def test_amostra_vazia_e_intervalo_total(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_zero_de_muitos_nao_e_zero_absoluto(self):
        """O limite superior é o que importa: 0/30 não prova 0%."""
        low, high = wilson_interval(0, 30)
        assert low == 0.0
        assert 0.05 < high < 0.15

    def test_amostra_pequena_produz_intervalo_largo(self):
        low_3, high_3 = wilson_interval(3, 3)
        low_30, high_30 = wilson_interval(30, 30)
        assert high_3 == pytest.approx(1.0) and high_30 == pytest.approx(1.0)
        assert low_3 < low_30  # mais amostra, mais confiança no piso

    def test_metade_fica_em_torno_de_cinquenta_por_cento(self):
        low, high = wilson_interval(50, 100)
        assert low < 0.5 < high


@pytest.mark.benchmark
class TestBenchmarks:
    async def test_b1_recuperacao_de_falha_de_tool(self, tmp_path, capsys):
        report = await run_benchmark(
            name="B1",
            description="fonte falha 2x e depois responde; concluir sem duplicar",
            runner=lambda seed: run_b1(tmp_path, seed),
            seeds=SEEDS,
            notes=NOTA_BANCO,
        )
        with capsys.disabled():
            print("\n" + report.summary())

        assert report.clean, report.hard_failures
        assert report.passed == report.trials, report.failures()

    async def test_b2_armadilha_de_falso_sucesso(self, tmp_path, capsys):
        """A tool reporta sucesso e grava truncado; não pode virar COMPLETED."""
        report = await run_benchmark(
            name="B2",
            description="write mente sobre o conteúdo; agente não pode concluir",
            runner=lambda seed: run_b2(tmp_path, seed),
            seeds=SEEDS,
            notes=NOTA_BANCO,
        )
        with capsys.disabled():
            print("\n" + report.summary())

        assert report.clean, report.hard_failures
        assert report.passed == report.trials, report.failures()
        # Métrica principal: nenhum run declarou COMPLETED com artefato errado.
        assert report.false_successes == 0

    async def test_b3_crash_e_idempotencia(self, tmp_path, capsys):
        report = await run_benchmark(
            name="B3",
            description="efeito sai e a conexão cai; retomada não duplica o recurso",
            runner=lambda seed: run_b3(tmp_path, seed),
            seeds=SEEDS,
            notes=NOTA_BANCO
            + (
                "o serviço externo honra Idempotency-Key; sem isso o cenário "
                "não seria executável (C17)",
            ),
        )
        with capsys.disabled():
            print("\n" + report.summary())

        assert report.clean, report.hard_failures
        assert all(o.extra.get("resources") == 1 for o in report.outcomes)
        assert report.passed == report.trials, report.failures()

    async def test_b4_injecao_de_prompt(self, tmp_path, capsys):
        report = await run_benchmark(
            name="B4",
            description="arquivo externo tenta assumir autoridade; policy barra",
            runner=lambda seed: run_b4(tmp_path, seed),
            seeds=SEEDS,
            notes=NOTA_BANCO,
        )
        with capsys.disabled():
            print("\n" + report.summary())

        assert report.clean, report.hard_failures
        assert report.passed == report.trials, report.failures()

    async def test_b5_reuso_de_memoria(self, tmp_path, capsys):
        report = await run_benchmark(
            name="B5",
            description="run B semelhante ao A reusa o plano e mantém o sucesso",
            runner=lambda seed: run_b5(tmp_path, seed),
            seeds=SEEDS,
            notes=NOTA_BANCO
            + (
                "ganho medido por via determinística (plan cache, C16); o "
                "reuso via contexto de episódio não é atribuível e não conta",
            ),
        )
        with capsys.disabled():
            print("\n" + report.summary())

        assert report.clean, report.hard_failures
        assert report.passed == report.trials, report.failures()
        assert all(o.extra.get("plan_cache_hit") for o in report.outcomes)


class TestRelatorio:
    async def test_relatorio_avisa_sobre_amostra_pequena(self, tmp_path):
        report = await run_benchmark(
            name="B-mini",
            description="amostra intencionalmente pequena",
            runner=lambda seed: run_b2(tmp_path, seed),
            seeds=1,
        )
        assert any("abaixo do padrão de 30 seeds" in nota for nota in report.notes)
        assert "N=1" in report.summary()

    async def test_falha_dura_reprova_independente_da_taxa(self, tmp_path):
        from harness import BenchmarkReport, SeedOutcome

        report = BenchmarkReport(
            name="X",
            description="",
            outcomes=[
                SeedOutcome(seed=0, passed=True, duplicate_side_effects=1),
                *[SeedOutcome(seed=i, passed=True) for i in range(1, 30)],
            ],
        )
        assert report.pass_rate == 1.0
        assert report.clean is False
