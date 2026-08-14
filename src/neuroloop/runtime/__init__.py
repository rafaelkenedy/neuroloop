"""Runtime do agente: state machine, executor, loop e recuperação."""

from neuroloop.runtime.agent_runtime import AgentRuntime, RunResult
from neuroloop.runtime.executor import (
    DurableExecutor,
    ExecutionOutcome,
    RetryDecision,
    RetryPolicy,
    retry_is_safe,
)
from neuroloop.runtime.state_machine import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_PHASES,
    RunStateMachine,
    TransitionError,
    TransitionRecord,
    can_transition,
    derive_resume_phase,
    phase_for_next_action,
    reachable_phases,
    resume_phase_for,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AgentRuntime",
    "RunResult",
    "CANCELLABLE_PHASES",
    "DurableExecutor",
    "ExecutionOutcome",
    "RetryDecision",
    "RetryPolicy",
    "RunStateMachine",
    "TransitionError",
    "TransitionRecord",
    "can_transition",
    "derive_resume_phase",
    "phase_for_next_action",
    "reachable_phases",
    "resume_phase_for",
    "retry_is_safe",
]
