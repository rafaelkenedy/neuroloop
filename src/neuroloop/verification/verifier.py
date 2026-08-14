"""Verifier — TASK-007 (spec §15, correção C02).

Quatro níveis, com autoridade crescente e custo crescente:

1. **Execução** — a ferramenta funcionou? Barato e insuficiente.
2. **Estado** — o efeito esperado do passo realmente ocorreu?
3. **Goal** — isso satisfaz os critérios do objetivo? Só evidência externa
   conta, e só com delta em relação ao baseline.
4. **Safety** — o resultado violou algum critério de falha?

A separação existe porque `tool retornou 200` não é `objetivo cumprido`. É
essa distinção que a métrica `false_success_rate` mede, e é por isso que o
nível 1 nunca conclui um goal sozinho.

Ordem de preferência das fontes (spec §15): schema/teste → probe externo →
comparação de estado → regra. LLM-as-judge não existe na V0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuroloop.core.criteria import Criterion, CriterionOutcome, effective_observes
from neuroloop.core.enums import ErrorCode, ExecutionStatus, NextAction
from neuroloop.core.goals import Goal
from neuroloop.core.runs import RunCheckpoint
from neuroloop.core.verification import VerificationEvidence, VerificationResult
from neuroloop.verification.evaluator import CriterionEvaluator, EvaluationContext


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """O que o executor observou, traduzido para o vocabulário do Verifier.

    Deliberadamente um tipo próprio: o Verifier não importa o executor, para
    que não passe a depender de como a execução é feita.
    """

    status: ExecutionStatus
    output: Any = None
    error_code: ErrorCode | None = None
    probe_result: str | None = None
    retry_available: bool = False
    """O runtime já sabe se um retry seria provadamente seguro."""


@dataclass(frozen=True, slots=True)
class GoalVerdict:
    satisfied: bool
    pre_satisfied: bool
    outcomes: tuple[CriterionOutcome, ...]

    @property
    def conclusive(self) -> bool:
        return self.satisfied and not self.pre_satisfied


class Verifier:
    def __init__(self, evaluator: CriterionEvaluator | None = None) -> None:
        self.evaluator = evaluator or CriterionEvaluator()

    # ------------------------------------------------------- nível 2: estado

    async def verify_state(
        self, expected_outcomes: tuple[Criterion, ...], ctx: EvaluationContext
    ) -> list[CriterionOutcome]:
        return await self.evaluator.evaluate_all(expected_outcomes, ctx)

    # -------------------------------------------------------- nível 3: goal

    async def verify_goal(
        self, goal: Goal, checkpoint: RunCheckpoint, ctx: EvaluationContext
    ) -> GoalVerdict:
        """Correção C02: satisfeito **e** com progresso atribuível ao run.

        Um goal que já estava satisfeito no baseline não foi cumprido por
        este run. Declarar `COMPLETED` nesse caso é falso sucesso puro — é o
        caso que a spec original produzia ao verificar antes de agir.
        """
        outcomes = await self.evaluator.evaluate_all(goal.external_success_criteria, ctx)
        satisfied = bool(outcomes) and all(o.satisfied is True for o in outcomes)

        baseline = list(checkpoint.baseline_outcomes)
        progressed = any(
            before.satisfied is not True and after.satisfied is True
            for before, after in zip(baseline, outcomes, strict=False)
        )
        return GoalVerdict(
            satisfied=satisfied,
            pre_satisfied=satisfied and not progressed,
            outcomes=tuple(outcomes),
        )

    # ------------------------------------------------------ nível 4: safety

    async def verify_safety(
        self, goal: Goal, ctx: EvaluationContext
    ) -> list[CriterionOutcome]:
        """`failure_criteria` satisfeito é condição de parada, não de sucesso."""
        return await self.evaluator.evaluate_all(goal.failure_criteria, ctx)

    # ----------------------------------------------------------- composição

    async def evaluate(
        self,
        *,
        goal: Goal,
        checkpoint: RunCheckpoint,
        report: ExecutionReport,
        expected_outcomes: tuple[Criterion, ...] = (),
        ctx: EvaluationContext,
        safety_ok: bool = True,
    ) -> VerificationResult:
        evidence: list[VerificationEvidence] = []

        # Nível 1 — execução. Auto-relato da tool: o mais fraco que existe.
        evidence.append(
            VerificationEvidence(
                level="EXECUTION",
                observes="ACTION_RESULT",
                outcome=CriterionOutcome(
                    criterion_kind="EXECUTION_STATUS",
                    satisfied=_execution_satisfied(report.status),
                    observed=report.status.value,
                    expected=ExecutionStatus.SUCCESS.value,
                    error=report.error_code.value if report.error_code else None,
                    observed_at=ctx.now,
                ),
                source=f"executor:{report.probe_result or 'sem probe'}",
            )
        )

        # Nível 2 — estado. Só faz sentido se a execução não falhou de forma
        # definitiva; sondar o mundo depois de uma falha permanente é custo
        # sem informação nova.
        state_outcomes: list[CriterionOutcome] = []
        if report.status is not ExecutionStatus.FAILURE:
            state_outcomes = await self.verify_state(expected_outcomes, ctx)
            evidence.extend(
                VerificationEvidence(
                    level="STATE",
                    observes=effective_observes(criterion),
                    outcome=outcome,
                    source="releitura do estado",
                )
                for criterion, outcome in zip(expected_outcomes, state_outcomes, strict=True)
            )
        expected_satisfied = (
            None if not state_outcomes else _combine(state_outcomes)
        )

        # Nível 4 — safety antes de goal: efeito perigoso invalida conclusão.
        failure_outcomes = await self.verify_safety(goal, ctx)
        evidence.extend(
            VerificationEvidence(
                level="SAFETY",
                observes=effective_observes(criterion),
                outcome=outcome,
                source="failure_criteria do goal",
            )
            for criterion, outcome in zip(goal.failure_criteria, failure_outcomes, strict=True)
        )
        tripped_failure = any(o.satisfied is True for o in failure_outcomes)
        safety_ok = safety_ok and not tripped_failure

        # Nível 3 — goal. Só evidência externa, só com delta.
        verdict = await self.verify_goal(goal, checkpoint, ctx)
        evidence.extend(
            VerificationEvidence(
                level="GOAL",
                observes=effective_observes(criterion),
                outcome=outcome,
                source="critério externo do goal",
            )
            for criterion, outcome in zip(
                goal.external_success_criteria, verdict.outcomes, strict=True
            )
        )

        # Efeito indeterminado não produz afirmação conclusiva sobre o goal,
        # mesmo que o estado externo pareça satisfeito agora: pode ser efeito
        # de outra coisa, ou o efeito pendente ainda pode chegar. É a mesma
        # regra do INDETERMINATE, aplicada um nível acima.
        goal_satisfied = (
            verdict.conclusive
            and safety_ok
            and report.status is not ExecutionStatus.UNKNOWN
        )
        next_action, error_code = self._decide(
            report=report,
            expected_satisfied=expected_satisfied,
            had_expectations=bool(expected_outcomes),
            verdict=verdict,
            safety_ok=safety_ok,
            goal_satisfied=goal_satisfied,
        )

        return VerificationResult(
            execution_status=report.status,
            expected_outcomes_satisfied=expected_satisfied,
            goal_satisfied=goal_satisfied,
            safety_ok=safety_ok,
            confidence=_confidence(evidence),
            evidence=tuple(evidence),
            reward_signal=_reward(goal_satisfied, expected_satisfied, safety_ok),
            error_code=error_code,
            next_action=next_action,
        )

    def _decide(
        self,
        *,
        report: ExecutionReport,
        expected_satisfied: bool | None,
        had_expectations: bool,
        verdict: GoalVerdict,
        safety_ok: bool,
        goal_satisfied: bool,
    ) -> tuple[NextAction, ErrorCode | None]:
        """Ordem de precedência: segurança, efeito, conclusão, progresso."""
        if not safety_ok:
            return NextAction.STOP_FAILURE, ErrorCode.VERIFICATION_ERROR

        if report.status is ExecutionStatus.UNKNOWN:
            # C05: efeito indeterminado nunca avança sozinho.
            if report.retry_available:
                return NextAction.RETRY, ErrorCode.UNKNOWN_SIDE_EFFECT
            return NextAction.ASK_USER, ErrorCode.UNKNOWN_SIDE_EFFECT

        if goal_satisfied:
            return NextAction.GOAL_COMPLETED, None

        if verdict.pre_satisfied:
            # C02: satisfeito sem que este run tenha causado nada.
            return NextAction.ASK_USER, ErrorCode.GOAL_PRE_SATISFIED

        if report.status is ExecutionStatus.FAILURE:
            if report.retry_available:
                return NextAction.RETRY, report.error_code
            if report.error_code is ErrorCode.TOOL_PERMANENT_ERROR:
                return NextAction.REPLAN, report.error_code
            return NextAction.ASK_USER, report.error_code

        if expected_satisfied is False:
            # A tool funcionou e o efeito esperado não apareceu: o plano é
            # que está errado, não a execução.
            return NextAction.REPLAN, ErrorCode.GOAL_NOT_SATISFIED
        if had_expectations and expected_satisfied is None:
            # Havia o que checar e não foi possível observar. Distinto de
            # "não havia expectativa declarada", que segue o fluxo normal.
            return NextAction.ASK_USER, ErrorCode.INDETERMINATE_VERIFICATION

        return NextAction.CONTINUE, None


def _execution_satisfied(status: ExecutionStatus) -> bool | None:
    match status:
        case ExecutionStatus.SUCCESS:
            return True
        case ExecutionStatus.FAILURE:
            return False
        case _:
            return None  # UNKNOWN é indeterminado, não falha


def _combine(outcomes: list[CriterionOutcome]) -> bool | None:
    if any(o.satisfied is False for o in outcomes):
        return False
    if any(o.satisfied is None for o in outcomes):
        return None
    return True


def _confidence(evidence: list[VerificationEvidence]) -> float:
    """Confiança cai com evidência não observada, não com "achismo".

    Toda evidência aqui é determinística; o que varia é quanto dela foi
    possível coletar.
    """
    if not evidence:
        return 0.0
    observed = sum(1 for e in evidence if e.outcome.satisfied is not None)
    return round(observed / len(evidence), 4)


def _reward(goal_satisfied: bool, expected_satisfied: bool | None, safety_ok: bool) -> float:
    """Telemetria da V0, não mecanismo de treinamento (spec §10)."""
    if not safety_ok:
        return -1.0
    if goal_satisfied:
        return 1.0
    if expected_satisfied is True:
        return 0.5
    if expected_satisfied is False:
        return -0.5
    return 0.0
