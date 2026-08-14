"""PlannerValidator — TASK-011 (spec §13).

O plano vem do LLM; a autorização para executá-lo não. Este módulo é a
porta: rejeita plano com tool inexistente, risco mal declarado, argumentos
que não batem com o schema, passo não verificável ou repetição interna.

A regra menos óbvia — e a que mais protege a hipótese — é a de
**verificabilidade**: todo passo precisa de pelo menos um `expected_outcome`
que observe o **estado externo**. Um passo cujo sucesso só pode ser aferido
pelo relatório da própria ferramenta é um passo que produz falso sucesso
local, e um plano feito só desses passos conclui sem nunca olhar o mundo.
É a regra de C02 aplicada um nível abaixo, no passo.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuroloop.core.criteria import effective_observes
from neuroloop.core.enums import ErrorCode, RiskLevel
from neuroloop.core.identity import canonical_json
from neuroloop.core.plans import MAX_PLAN_STEPS, Plan, PlanStep
from neuroloop.tools.registry import ToolArgumentError, ToolNotFoundError, ToolRegistry


class PlanValidationError(ValueError):
    error_code = ErrorCode.INVALID_PLAN

    def __init__(self, detail: str) -> None:
        super().__init__(f"{ErrorCode.INVALID_PLAN.value}: {detail}")


@dataclass(frozen=True, slots=True)
class PlanReport:
    """Resultado da validação, com o que foi conferido."""

    plan: Plan
    materialized_steps: tuple[str, ...]
    max_risk: RiskLevel

    @property
    def fully_materialized(self) -> bool:
        return len(self.materialized_steps) == len(self.plan.steps)


class PlannerValidator:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_steps: int = MAX_PLAN_STEPS,
        max_risk: RiskLevel = RiskLevel.R2,
    ) -> None:
        self.registry = registry
        self.max_steps = max_steps
        self.max_risk = max_risk

    def validate(self, plan: Plan) -> PlanReport:
        if len(plan.steps) > self.max_steps:
            raise PlanValidationError(
                f"{len(plan.steps)} steps excedem o horizonte de {self.max_steps}"
            )

        materializados: list[str] = []
        risco_maximo = RiskLevel.R0
        fingerprints: dict[str, str] = {}

        for step in plan.steps:
            self._require_external_evidence(step)
            risco = self._check_tool(step)
            risco_maximo = max(risco_maximo, risco, key=lambda r: r.level)
            if step.is_materialized:
                materializados.append(step.id)
                self._check_duplicate(step, fingerprints)

        if risco_maximo > self.max_risk:
            raise PlanValidationError(
                f"plano exige risco {risco_maximo.value}, acima do teto "
                f"{self.max_risk.value}"
            )

        return PlanReport(
            plan=plan,
            materialized_steps=tuple(materializados),
            max_risk=risco_maximo,
        )

    # ------------------------------------------------------------- regras

    def _require_external_evidence(self, step: PlanStep) -> None:
        if not any(
            effective_observes(c) == "EXTERNAL_STATE" for c in step.expected_outcomes
        ):
            raise PlanValidationError(
                f"step {step.id!r} não é verificável: nenhum expected_outcome "
                "observa o estado externo"
            )

    def _check_tool(self, step: PlanStep) -> RiskLevel:
        if step.preferred_tool is None:
            # Step ainda não resolvido: o Deliberator decidirá a tool depois.
            return step.risk_hint
        try:
            definition = self.registry.get(step.preferred_tool).definition
        except ToolNotFoundError as error:
            raise PlanValidationError(
                f"step {step.id!r}: {error}"
            ) from error

        if step.risk_hint < definition.risk_level:
            # Subdeclarar risco faria o passo escapar dos gates da policy.
            raise PlanValidationError(
                f"step {step.id!r} declara {step.risk_hint.value} mas "
                f"{definition.name} é {definition.risk_level.value}"
            )

        if step.arguments is not None:
            try:
                self.registry.validate_arguments(step.preferred_tool, step.arguments)
            except ToolArgumentError as error:
                raise PlanValidationError(f"step {step.id!r}: {error}") from error

        return definition.risk_level

    def _check_duplicate(self, step: PlanStep, seen: dict[str, str]) -> None:
        fingerprint = canonical_json(
            {"tool": step.preferred_tool, "arguments": step.arguments}
        )
        anterior = seen.get(fingerprint)
        if anterior is not None:
            raise PlanValidationError(
                f"steps {anterior!r} e {step.id!r} são a mesma ação repetida"
            )
        seen[fingerprint] = step.id
