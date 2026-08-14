"""Enums compartilhados do núcleo.

Referências: spec §8, §10, §22, §32; correções C06 (state machine) e
"Adições à taxonomia de falhas" em 02_correcoes_spec.md.
"""

from __future__ import annotations

from enum import Enum


class RiskLevel(str, Enum):
    """Tiers de risco (spec §22).

    R0 leitura; R1 alteração local/reversível; R2 alteração externa
    significativa; R3 destrutivo/financeiro/publicação/credenciais.

    Ordenável: ``RiskLevel.R2 >= RiskLevel.R1``.
    """

    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"

    @property
    def level(self) -> int:
        return int(self.value[1])

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.level < other.level

    def __le__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.level <= other.level

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.level > other.level

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.level >= other.level


class RunPhase(str, Enum):
    """Fases do run na V0 (correção C06).

    ``WAITING_EXTERNAL`` e ``BLOCKED`` foram removidos: sem scheduler na V0
    nada sairia desses estados. Retornam na V0.5.
    """

    CREATED = "CREATED"
    PERCEIVING = "PERCEIVING"
    DELIBERATING = "DELIBERATING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    UPDATING_MEMORY = "UPDATING_MEMORY"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PHASES


_TERMINAL_PHASES = frozenset(
    {RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED}
)


class GoalStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PlanStepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class TrustLevel(str, Enum):
    """Autoridade de uma observação (spec §22).

    Conteúdo externo é sempre dado, nunca instrução. Consumido pelo
    PolicyEngine via propagação de taint (correção C10).
    """

    TRUSTED_INTERNAL = "TRUSTED_INTERNAL"
    USER = "USER"
    UNTRUSTED_EXTERNAL = "UNTRUSTED_EXTERNAL"


class ObservationSource(str, Enum):
    USER = "USER"
    TOOL = "TOOL"
    SYSTEM = "SYSTEM"
    RECOVERY = "RECOVERY"


class ExecutionStatus(str, Enum):
    """Resultado da execução da tool, não do goal."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"


class AttemptStatus(str, Enum):
    """Estado durável de uma tentativa (correção C08).

    ``IN_FLIGHT`` é gravado e commitado antes da chamada externa; é o
    marcador de recuperação após crash.
    """

    IN_FLIGHT = "IN_FLIGHT"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class NextAction(str, Enum):
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    REPLAN = "REPLAN"
    ASK_USER = "ASK_USER"
    GOAL_COMPLETED = "GOAL_COMPLETED"
    STOP_FAILURE = "STOP_FAILURE"


class ErrorCode(str, Enum):
    """Taxonomia de falhas (spec §32 + adições de 02_correcoes_spec.md)."""

    PERCEPTION_ERROR = "PERCEPTION_ERROR"
    MEMORY_RETRIEVAL_ERROR = "MEMORY_RETRIEVAL_ERROR"
    MEMORY_CONTRADICTION = "MEMORY_CONTRADICTION"
    REASONING_ERROR = "REASONING_ERROR"
    PLANNING_ERROR = "PLANNING_ERROR"
    INVALID_PLAN = "INVALID_PLAN"
    TOOL_SELECTION_ERROR = "TOOL_SELECTION_ERROR"
    TOOL_VALIDATION_ERROR = "TOOL_VALIDATION_ERROR"
    TOOL_TRANSIENT_ERROR = "TOOL_TRANSIENT_ERROR"
    TOOL_PERMANENT_ERROR = "TOOL_PERMANENT_ERROR"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"
    VERIFICATION_ERROR = "VERIFICATION_ERROR"
    GOAL_NOT_SATISFIED = "GOAL_NOT_SATISFIED"
    IMPOSSIBLE_TASK = "IMPOSSIBLE_TASK"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    STATE_CONFLICT = "STATE_CONFLICT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REPLAN_LIMIT = "REPLAN_LIMIT"
    RETRY_LIMIT = "RETRY_LIMIT"
    LOOP_DETECTED = "LOOP_DETECTED"
    CANCELLED = "CANCELLED"

    # Adições de 02_correcoes_spec.md
    GOAL_PRE_SATISFIED = "GOAL_PRE_SATISFIED"
    INVALID_GOAL_CRITERIA = "INVALID_GOAL_CRITERIA"
    INDETERMINATE_VERIFICATION = "INDETERMINATE_VERIFICATION"
    LEASE_LOST = "LEASE_LOST"
