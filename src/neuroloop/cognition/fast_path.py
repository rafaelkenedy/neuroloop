"""Fast Path — TASK-011 (spec §12, correção C13).

A spec pedia `skill_match.score >= 0.95` sem definir o scorer. Sem
embeddings e com match por `trigger_tags`, o score é overlap discreto de
conjuntos pequenos: 0.95 vira match exato, e o `fast_path_hit_rate` fica em
zero — com o alvo de 95% de sucesso medido sobre denominador vazio.

C13 separa **duas fontes**, com critérios diferentes:

`STEP` — PlanStep já materializado. Determinístico, sem score. É daqui que
vem a maior parte dos acertos na V0: o plano já decidiu tool e argumentos, e
repetir a deliberação para executá-lo seria pagar duas vezes pela mesma
decisão.

`SKILL` — gabarito versionado, com score explícito:

    score = 0.60 * jaccard(tags do ciclo, trigger_tags)
          + 0.40 * (inputs resolvidos / inputs exigidos)

O segundo termo é binário na prática (argumentos completos são
obrigatórios), então o limiar efetivo de 0.75 equivale a `jaccard >= 0.58` —
alcançável com conjuntos de tags pequenos.

As métricas são reportadas **por fonte**: misturar as duas esconderia que o
Fast Path por skill não está disparando.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from neuroloop.cognition.skills import SkillDefinition, SkillRegistry
from neuroloop.context.workspace import WorkingContext
from neuroloop.core.actions import ActionProposal
from neuroloop.core.criteria import is_conclusive_success
from neuroloop.core.enums import PlanStepStatus, RiskLevel
from neuroloop.core.plans import Plan, PlanStep
from neuroloop.core.templating import MissingPlaceholder
from neuroloop.tools.registry import ToolNotFoundError, ToolRegistry
from neuroloop.verification.evaluator import CriterionEvaluator, EvaluationContext

SKILL_SCORE_THRESHOLD = 0.75
WEIGHT_TAGS = 0.60
WEIGHT_INPUTS = 0.40

MAX_AUTO_RISK = RiskLevel.R1
"""Fast Path não decide sobre risco: acima de R1 volta para a deliberação."""


class FastPathSource(str, Enum):
    STEP = "STEP"
    SKILL = "SKILL"


@dataclass(frozen=True, slots=True)
class FastPathMatch:
    source: FastPathSource
    action: ActionProposal
    score: float
    reason_code: str
    step_id: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None

    @property
    def is_skill(self) -> bool:
        return self.source is FastPathSource.SKILL


@dataclass(frozen=True, slots=True)
class FastPathRejection:
    """Por que uma candidata não passou. Alimenta o trace, não o fluxo."""

    skill_id: str
    reason_code: str
    score: float = 0.0


class FastPath:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        skills: SkillRegistry | None = None,
        evaluator: CriterionEvaluator | None = None,
        max_auto_risk: RiskLevel = MAX_AUTO_RISK,
    ) -> None:
        self.registry = registry
        self.skills = skills or SkillRegistry()
        self.evaluator = evaluator or CriterionEvaluator()
        self.max_auto_risk = max_auto_risk
        self.rejections: list[FastPathRejection] = []

    async def match(
        self,
        context: WorkingContext,
        *,
        evaluation: EvaluationContext | None = None,
        inputs: dict[str, object] | None = None,
        has_unresolved_failure: bool = False,
    ) -> FastPathMatch | None:
        self.rejections = []
        if has_unresolved_failure:
            # Spec §12: falha não resolvida derruba o Fast Path inteiro. Repetir
            # um caminho conhecido depois de algo dar errado é como se insiste
            # no mesmo erro.
            return None

        step_match = self._match_step(context)
        if step_match is not None:
            return step_match

        return await self._match_skill(
            context,
            evaluation=evaluation or EvaluationContext(now=datetime.now(UTC)),
            inputs=inputs or {},
        )

    # ------------------------------------------------------------ fonte STEP

    def _match_step(self, context: WorkingContext) -> FastPathMatch | None:
        step = next_ready_step(context.current_plan)
        if step is None:
            return None
        try:
            definition = self.registry.get(step.preferred_tool).definition
        except ToolNotFoundError:
            return None
        if definition.risk_level > self.max_auto_risk:
            return None

        return FastPathMatch(
            source=FastPathSource.STEP,
            action=ActionProposal(
                tool=step.preferred_tool,
                arguments=dict(step.arguments or {}),
                expected_outcomes=step.expected_outcomes,
                rationale_code=f"STEP:{step.id}",
            ),
            score=1.0,
            reason_code="MATERIALIZED_STEP",
            step_id=step.id,
        )

    # ----------------------------------------------------------- fonte SKILL

    async def _match_skill(
        self,
        context: WorkingContext,
        *,
        evaluation: EvaluationContext,
        inputs: dict[str, object],
    ) -> FastPathMatch | None:
        cycle_tags = _cycle_tags(context)
        melhor: tuple[float, SkillDefinition] | None = None

        for skill in self.skills.all():
            usable, reason = skill.usability(self.registry)
            if not usable:
                self.rejections.append(FastPathRejection(skill.id, reason))
                continue

            resolved = sum(1 for name in skill.required_inputs if name in inputs)
            total = len(skill.required_inputs)
            if total and resolved != total:
                self.rejections.append(
                    FastPathRejection(skill.id, f"MISSING_INPUTS:{total - resolved}")
                )
                continue

            score = skill_score(cycle_tags, skill, resolved=resolved, required=total)
            if score < SKILL_SCORE_THRESHOLD:
                self.rejections.append(
                    FastPathRejection(skill.id, "BELOW_THRESHOLD", score)
                )
                continue

            definition = self.registry.get(skill.action_template.tool).definition
            if definition.risk_level > self.max_auto_risk:
                self.rejections.append(
                    FastPathRejection(skill.id, f"RISK_TOO_HIGH:{definition.risk_level.value}")
                )
                continue

            if melhor is None or score > melhor[0]:
                melhor = (score, skill)

        if melhor is None:
            return None

        score, skill = melhor
        if not await self._preconditions_hold(skill, evaluation):
            self.rejections.append(FastPathRejection(skill.id, "PRECONDITIONS_UNMET", score))
            return None

        try:
            action = skill.materialize(inputs)
        except MissingPlaceholder as error:
            self.rejections.append(
                FastPathRejection(skill.id, f"MISSING_INPUT:{error.name}", score)
            )
            return None

        return FastPathMatch(
            source=FastPathSource.SKILL,
            action=action,
            score=round(score, 4),
            reason_code="SKILL_MATCH",
            skill_id=skill.id,
            skill_version=skill.version,
        )

    async def _preconditions_hold(
        self, skill: SkillDefinition, evaluation: EvaluationContext
    ) -> bool:
        if not skill.preconditions:
            return True
        outcomes = await self.evaluator.evaluate_all(skill.preconditions, evaluation)
        # INDETERMINATE não libera Fast Path: na dúvida, delibera.
        return is_conclusive_success(outcomes)


# ---------------------------------------------------------------- funções


def skill_score(
    cycle_tags: frozenset[str],
    skill: SkillDefinition,
    *,
    resolved: int,
    required: int,
) -> float:
    tag_score = _jaccard(cycle_tags, skill.trigger_tags)
    input_score = 1.0 if required == 0 else resolved / required
    return WEIGHT_TAGS * tag_score + WEIGHT_INPUTS * input_score


def next_ready_step(plan: Plan | None) -> PlanStep | None:
    """Primeiro step materializado com dependências concluídas."""
    if plan is None:
        return None
    done = {s.id for s in plan.steps if s.status is PlanStepStatus.DONE}
    for step_id in plan.topological_order():
        step = next(s for s in plan.steps if s.id == step_id)
        if step.status is not PlanStepStatus.PENDING:
            continue
        if not set(step.dependencies) <= done:
            continue
        if step.is_materialized:
            return step
    return None


def _cycle_tags(context: WorkingContext) -> frozenset[str]:
    tags: set[str] = set()
    for observation in context.observations:
        tags.update(observation.tags)
    for criterion in context.goal.success_criteria:
        path = getattr(criterion, "path", None)
        if isinstance(path, str):
            tags.add(f"resource:{path}")
    return frozenset(tags)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
