"""Construção do contexto que vai ao LLM. Limitado, priorizado, auditável."""

from neuroloop.context.rendering import (
    CLOSE_TAG,
    OPEN_TAG,
    PROTECTED_SECTIONS,
    SYSTEM_POLICY,
    PromptSection,
    render_prompt,
    render_sections,
    sanitize_untrusted,
    wrap_untrusted,
)
from neuroloop.context.salience import (
    SalienceInputs,
    measure,
    salience,
    score_observation,
)
from neuroloop.context.workspace import (
    AttentionItem,
    BudgetView,
    ContextBudget,
    GoalView,
    RecentError,
    SafetyContext,
    WorkingContext,
    WorkspaceBuilder,
)

__all__ = [
    "CLOSE_TAG",
    "OPEN_TAG",
    "PROTECTED_SECTIONS",
    "SYSTEM_POLICY",
    "AttentionItem",
    "BudgetView",
    "ContextBudget",
    "GoalView",
    "PromptSection",
    "RecentError",
    "SafetyContext",
    "SalienceInputs",
    "WorkingContext",
    "WorkspaceBuilder",
    "measure",
    "render_prompt",
    "render_sections",
    "salience",
    "sanitize_untrusted",
    "score_observation",
    "wrap_untrusted",
]
