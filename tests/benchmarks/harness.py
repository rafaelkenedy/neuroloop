"""Harness de benchmark — TASK-015 (spec §30, §31, correção C18).

Roda um cenário N vezes e agrega. Três regras que separam medição de teatro:

**Intervalo de confiança, não ponto.** A spec pedia `false_success_rate < 1%`
com cinco benchmarks — impossível de medir. O harness reporta Wilson 95% e
os alvos são formulados sobre o **limite superior** do intervalo (C18).

**Falhas duras não são percentual.** Efeito duplicado e execução não
autorizada têm alvo zero absoluto: uma ocorrência reprova a suíte,
independentemente da taxa.

**Nada é truncado em silêncio.** Se o harness limita cobertura, ele diz —
uma suíte que esconde o que não mediu lê-se como "cobri tudo".

O harness pode importar `neuroloop` (ele dirige o agente). Quem **não** pode
é `oracles.py`, e isso é verificado por teste (C17).
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, *, z: float = Z_95) -> tuple[float, float]:
    """Intervalo de Wilson 95%.

    Escolhido em vez do normal por se comportar em amostra pequena e perto
    de 0 ou 1 — que é exatamente o regime destes benchmarks.
    """
    if trials == 0:
        return (0.0, 1.0)
    p = successes / trials
    denominador = 1 + z**2 / trials
    centro = (p + z**2 / (2 * trials)) / denominador
    margem = (
        z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2))
    ) / denominador
    return (max(centro - margem, 0.0), min(centro + margem, 1.0))


@dataclass(slots=True)
class SeedOutcome:
    """Resultado de uma execução do cenário."""

    seed: int
    passed: bool
    reasons: tuple[str, ...] = ()
    declared_complete: bool = False
    duplicate_side_effects: int = 0
    unauthorized_executions: int = 0
    dangling_attempts: int = 0
    iterations: int = 0
    tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    tool_calls: int = 0
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BenchmarkReport:
    name: str
    description: str
    outcomes: list[SeedOutcome]
    notes: tuple[str, ...] = ()

    @property
    def trials(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.trials if self.trials else 0.0

    @property
    def pass_interval(self) -> tuple[float, float]:
        return wilson_interval(self.passed, self.trials)

    @property
    def declared_complete(self) -> int:
        return sum(1 for o in self.outcomes if o.declared_complete)

    @property
    def false_successes(self) -> int:
        """Declarou COMPLETED e o oracle reprovou (spec §31).

        O numerador vem do agente; o denominador e o veredito, do oracle.
        """
        return sum(1 for o in self.outcomes if o.declared_complete and not o.passed)

    @property
    def false_success_rate(self) -> float | None:
        if not self.declared_complete:
            return None
        return self.false_successes / self.declared_complete

    @property
    def false_success_upper_bound(self) -> float | None:
        """O alvo é sobre o limite superior, não sobre o ponto (C18)."""
        if not self.declared_complete:
            return None
        return wilson_interval(self.false_successes, self.declared_complete)[1]

    @property
    def hard_failures(self) -> dict[str, int]:
        """Alvo zero absoluto: uma ocorrência reprova."""
        return {
            "duplicate_side_effects": sum(
                o.duplicate_side_effects for o in self.outcomes
            ),
            "unauthorized_executions": sum(
                o.unauthorized_executions for o in self.outcomes
            ),
            "dangling_attempts": sum(o.dangling_attempts for o in self.outcomes),
        }

    @property
    def clean(self) -> bool:
        return all(v == 0 for v in self.hard_failures.values())

    def mean(self, attribute: str) -> float:
        if not self.outcomes:
            return 0.0
        return sum(float(getattr(o, attribute)) for o in self.outcomes) / self.trials

    def failures(self) -> list[str]:
        return [
            f"seed {o.seed}: {'; '.join(o.reasons)}"
            for o in self.outcomes
            if not o.passed
        ]

    def summary(self) -> str:
        low, high = self.pass_interval
        linhas = [
            f"{self.name} - {self.description}",
            f"  aprovados: {self.passed}/{self.trials} "
            f"({self.pass_rate:.0%}, IC95 {low:.0%}-{high:.0%})",
            f"  falhas duras: {self.hard_failures}",
            f"  médias: {self.mean('iterations'):.1f} iterações, "
            f"{self.mean('tool_calls'):.1f} tool calls, "
            f"{self.mean('tokens'):.0f} tokens, "
            f"{self.mean('latency_ms'):.0f} ms",
        ]
        if self.false_success_rate is not None:
            linhas.append(
                f"  false_success_rate: {self.false_success_rate:.0%} "
                f"(limite superior IC95: {self.false_success_upper_bound:.0%}) "
                f"sobre {self.declared_complete} runs declarados COMPLETED"
            )
        for nota in self.notes:
            linhas.append(f"  nota: {nota}")
        for falha in self.failures():
            linhas.append(f"  FALHOU {falha}")
        return "\n".join(linhas)


ScenarioRunner = Callable[[int], Awaitable[SeedOutcome]]


async def run_benchmark(
    *,
    name: str,
    description: str,
    runner: ScenarioRunner,
    seeds: int,
    notes: tuple[str, ...] = (),
) -> BenchmarkReport:
    outcomes: list[SeedOutcome] = []
    for seed in range(seeds):
        started = time.perf_counter()
        outcome = await runner(seed)
        if not outcome.latency_ms:
            outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        outcomes.append(outcome)

    aviso = ()
    if seeds < 30:
        # C18: dizer o que não foi medido é parte da medição.
        aviso = (
            f"N={seeds} abaixo do padrão de 30 seeds; o intervalo é largo e o "
            "alvo de false_success_rate não é conclusivo",
        )
    return BenchmarkReport(
        name=name, description=description, outcomes=outcomes, notes=notes + aviso
    )
