"""Schemas centrais compartilhados — TASK-001.

Tipos usados por mais de um módulo cognitivo. Nada aqui faz I/O, chama LLM
ou depende de persistência: são dados validados e funções puras.
"""

from neuroloop.core.actions import ActionProposal, UserInputRequest
from neuroloop.core.criteria import (
    AllOf,
    AnyOf,
    CommandExitCodeEquals,
    Criterion,
    CriterionOutcome,
    FileExists,
    FileMatchesJsonSchema,
    HttpStatusEquals,
    JsonPathCount,
    JsonPathEquals,
    Observes,
    ValueEquals,
    apply_negate,
    combine_all,
    combine_any,
    effective_observes,
    is_conclusive_success,
    iter_criteria,
)
from neuroloop.core.decisions import (
    ActDecision,
    AskUserDecision,
    Decision,
    ImpossibleDecision,
    PlanDecision,
)
from neuroloop.core.enums import (
    AttemptStatus,
    ErrorCode,
    ExecutionStatus,
    GoalStatus,
    NextAction,
    ObservationSource,
    PlanStepStatus,
    RiskLevel,
    RunPhase,
    TrustLevel,
)
from neuroloop.core.goals import Constraint, Goal
from neuroloop.core.observations import Observation
from neuroloop.core.plans import MAX_PLAN_STEPS, Plan, PlanStep
from neuroloop.core.runs import ExecutionBudget, RunCheckpoint
from neuroloop.core.verification import (
    VerificationEvidence,
    VerificationLevel,
    VerificationResult,
)

__all__ = [
    "MAX_PLAN_STEPS",
    "ActDecision",
    "ActionProposal",
    "AllOf",
    "AnyOf",
    "AskUserDecision",
    "AttemptStatus",
    "CommandExitCodeEquals",
    "Constraint",
    "Criterion",
    "CriterionOutcome",
    "Decision",
    "ErrorCode",
    "ExecutionBudget",
    "ExecutionStatus",
    "FileExists",
    "FileMatchesJsonSchema",
    "Goal",
    "GoalStatus",
    "HttpStatusEquals",
    "ImpossibleDecision",
    "JsonPathCount",
    "JsonPathEquals",
    "NextAction",
    "Observation",
    "ObservationSource",
    "Observes",
    "Plan",
    "PlanDecision",
    "PlanStep",
    "PlanStepStatus",
    "RiskLevel",
    "RunCheckpoint",
    "RunPhase",
    "TrustLevel",
    "UserInputRequest",
    "ValueEquals",
    "VerificationEvidence",
    "VerificationLevel",
    "VerificationResult",
    "apply_negate",
    "combine_all",
    "combine_any",
    "effective_observes",
    "is_conclusive_success",
    "iter_criteria",
]
