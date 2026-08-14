"""Gravação de episódios — TASK-008.

Um episódio é o registro estruturado de *um ciclo*: o que foi decidido, o
que foi executado, o que se verificou. Não é o log do que o LLM disse — a
spec (§15) é explícita em não guardar todo token produzido.

As tags são o que torna o retrieval possível sem embeddings. Elas são
derivadas de forma determinística de fatos do ciclo (tool, recurso, código
de erro, tipo de decisão), e não de texto livre: um vocabulário estável é o
que permite comparar episódios por estrutura.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from neuroloop.core.enums import ErrorCode, ExecutionStatus, RiskLevel
from neuroloop.core.verification import VerificationResult
from neuroloop.memory.importance import compute_importance
from neuroloop.persistence.repositories import EpisodeRepository

TAG_TOOL = "tool"
TAG_RESOURCE = "resource"
TAG_ERROR = "error"
TAG_DECISION = "decision"
TAG_OUTCOME = "outcome"


def build_episode_tags(
    *,
    tool_name: str | None = None,
    resource: str | None = None,
    error_code: ErrorCode | None = None,
    decision_type: str = "ACT",
    succeeded: bool = False,
    extra: tuple[str, ...] = (),
) -> list[str]:
    """Vocabulário fechado e prefixado, para o match não depender de prosa."""
    tags = [f"{TAG_DECISION}:{decision_type}"]
    if tool_name:
        tags.append(f"{TAG_TOOL}:{tool_name}")
    if resource:
        tags.append(f"{TAG_RESOURCE}:{resource}")
    if error_code:
        tags.append(f"{TAG_ERROR}:{error_code.value}")
    tags.append(f"{TAG_OUTCOME}:{'SUCCESS' if succeeded else 'FAILURE'}")
    tags.extend(extra)
    return tags


class EpisodeRecorder:
    """Fachada de gravação: calcula importância e tags, delega a persistência.

    Existe para que o runtime não precise conhecer a fórmula de importância
    (C14) nem o vocabulário de tags — ele só entrega os fatos do ciclo.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.repository = EpisodeRepository(session)

    async def record(
        self,
        *,
        run_id: UUID,
        iteration: int,
        goal_summary: str,
        observation_summary: str,
        verification: VerificationResult,
        decision_type: str = "ACT",
        tool_name: str | None = None,
        resource: str | None = None,
        action_id: UUID | None = None,
        plan_step_id: str | None = None,
        risk_level: RiskLevel = RiskLevel.R0,
        result_summary: str | None = None,
        extra_tags: tuple[str, ...] = (),
    ) -> UUID:
        succeeded = (
            verification.execution_status is ExecutionStatus.SUCCESS
            and verification.expected_outcomes_satisfied is not False
        )
        importance = compute_importance(
            execution_status=verification.execution_status,
            goal_satisfied=verification.goal_satisfied,
            expected_outcomes_satisfied=verification.expected_outcomes_satisfied,
            risk_level=risk_level,
            decision_type=decision_type,
            error_code=verification.error_code,
            closed_the_goal=verification.goal_satisfied,
        )
        return await self.repository.record(
            run_id=run_id,
            iteration=iteration,
            goal_summary=goal_summary,
            observation_summary=observation_summary,
            decision_type=decision_type,
            result_summary=result_summary or verification.execution_status.value,
            verification=verification.model_dump(mode="json"),
            importance=importance,
            reward=verification.reward_signal,
            tool_name=tool_name,
            action_id=action_id,
            plan_step_id=plan_step_id,
            error_code=verification.error_code,
            tags=build_episode_tags(
                tool_name=tool_name,
                resource=resource,
                error_code=verification.error_code,
                decision_type=decision_type,
                succeeded=succeeded,
                extra=extra_tags,
            ),
        )
