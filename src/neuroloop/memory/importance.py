"""Importância de episódio — correção C14.

A spec usava `importance` no ranking de retrieval sem nunca defini-la.
Determinística e sem LLM, por hipótese: episódios de falha e de decisão
custosa carregam mais informação reutilizável que sucessos triviais.
"""

from __future__ import annotations

from neuroloop.core.enums import ErrorCode, ExecutionStatus, RiskLevel

_RISK_WEIGHT = {RiskLevel.R0: 0.0, RiskLevel.R1: 0.34, RiskLevel.R2: 0.67, RiskLevel.R3: 1.0}


def compute_importance(
    *,
    execution_status: ExecutionStatus,
    goal_satisfied: bool,
    expected_outcomes_satisfied: bool | None,
    risk_level: RiskLevel = RiskLevel.R0,
    decision_type: str = "ACT",
    error_code: ErrorCode | None = None,
    closed_the_goal: bool = False,
) -> float:
    failed = (
        execution_status is not ExecutionStatus.SUCCESS
        or expected_outcomes_satisfied is not True
    )
    score = (
        0.35 * float(failed)
        + 0.25 * _RISK_WEIGHT[risk_level]
        + 0.20 * float(decision_type in {"PLAN", "ASK_USER"})
        + 0.10 * float(error_code is not None)
        + 0.10 * float(closed_the_goal or goal_satisfied)
    )
    return round(min(max(score, 0.0), 1.0), 4)
