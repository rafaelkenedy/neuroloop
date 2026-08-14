"""Durable Executor — TASK-006 (spec §14, correções C04, C05, C08, C09).

O executor é o único componente que fala com o mundo externo, e a única
razão de ele existir como peça separada é a ordem das operações:

    grava attempt IN_FLIGHT → COMMIT → chama a tool → COMMIT do desfecho

Sem o commit no meio, um crash durante a chamada deixa o sistema sem
registro de que um efeito pode ter saído — e o retry seguinte vira
duplicação silenciosa.

Três regras da spec vivem aqui:

- `timeout != action_failed` (§3). Timeout em tool com efeito produz
  `UNKNOWN_EFFECT`, não falha; quem desempata é o probe.
- Retry exige idempotência **ou** prova de ausência do efeito. Nunca retry
  cego.
- `Executor não decide`: ele executa, observa e relata. Concluir goal é do
  Verifier; escolher a próxima ação é do Controller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neuroloop.core.actions import ActionProposal
from neuroloop.core.criteria import CriterionOutcome
from neuroloop.core.enums import AttemptStatus, ErrorCode, ExecutionStatus
from neuroloop.core.identity import is_loop
from neuroloop.core.runs import RunCheckpoint
from neuroloop.persistence import models
from neuroloop.persistence.repositories import ActionRepository
from neuroloop.tools.definitions import ProbeResult, ToolDefinition, ToolResult
from neuroloop.tools.registry import RegisteredTool, ToolRegistry
from neuroloop.tools.sandbox import Sandbox
from neuroloop.verification.evaluator import CriterionEvaluator, EvaluationContext


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    action_id: UUID
    logical_action_id: UUID
    attempt_no: int
    result: ToolResult
    probe: CriterionOutcome | None = None
    probe_result: ProbeResult | None = None
    suppressed_duplicate: bool = False
    """Efeito já confirmado por uma tentativa anterior desta ação lógica."""

    @property
    def succeeded(self) -> bool:
        return self.result.status is ExecutionStatus.SUCCESS

    @property
    def effect_unknown(self) -> bool:
        return self.result.status is ExecutionStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    reason_code: str
    error_code: ErrorCode | None = None
    requires_user: bool = False


class RetryPolicy:
    """Correção C04: retry contado por ação lógica, com limite alcançável."""

    def decide(
        self,
        checkpoint: RunCheckpoint,
        definition: ToolDefinition,
        outcome: ExecutionOutcome,
    ) -> RetryDecision:
        if outcome.succeeded:
            return RetryDecision(False, "NO_RETRY_NEEDED")

        attempts = checkpoint.retry_counts.get(outcome.logical_action_id, 0)
        if attempts >= checkpoint.budget.max_retries_per_action:
            return RetryDecision(False, "RETRY_LIMIT", ErrorCode.RETRY_LIMIT)

        if outcome.result.error_code is ErrorCode.TOOL_PERMANENT_ERROR:
            return RetryDecision(False, "PERMANENT_FAILURE", ErrorCode.TOOL_PERMANENT_ERROR)

        if not retry_is_safe(definition, outcome):
            # Nem idempotência declarada, nem prova de ausência do efeito.
            return RetryDecision(
                False,
                "RETRY_NOT_PROVABLY_SAFE",
                ErrorCode.UNKNOWN_SIDE_EFFECT,
                requires_user=True,
            )
        return RetryDecision(True, "RETRY_SAFE")


def retry_is_safe(definition: ToolDefinition, outcome: ExecutionOutcome) -> bool:
    """Repetir a ação não pode criar um segundo efeito.

    Três caminhos aceitáveis, nesta ordem de força: a tool não muda nada; o
    probe provou que o efeito não existe; a tool declara idempotência e a
    chave é constante entre tentativas.
    """
    if not definition.side_effects:
        return True
    if outcome.probe_result == "EFFECT_ABSENT":
        return True
    if outcome.effect_unknown:
        # Efeito indeterminado nunca é provavelmente seguro, mesmo com
        # idempotência declarada por uma tool que não temos como auditar.
        return False
    return definition.supports_idempotency


class DurableExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
        sandbox: Sandbox | None = None,
        evaluator: CriterionEvaluator | None = None,
        http_prober=None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.sandbox = sandbox
        self.evaluator = evaluator or CriterionEvaluator()
        self.http_prober = http_prober
        """Sonda externa do probe. Sem ela, um efeito HTTP é INDETERMINATE."""

    # ------------------------------------------------------------- execução

    async def execute(
        self,
        *,
        run_id: UUID,
        proposal: ActionProposal,
        entry: RegisteredTool | None = None,
        plan_step_id: str | None = None,
        logical_action_id: UUID | None = None,
        approved_by_user: bool = False,
        action_id: UUID | None = None,
    ) -> ExecutionOutcome:
        entry = entry or self.registry.get(proposal.tool)
        definition = entry.definition
        self.registry.validate_arguments(
            proposal.tool, proposal.arguments, version=definition.version
        )
        logical_action_id = logical_action_id or uuid4()

        async with self.session_factory() as session:
            repo = ActionRepository(session)
            action = (
                await repo.get(action_id)
                if action_id
                else await repo.create_logical_action(
                    run_id=run_id,
                    proposal=proposal,
                    tool_version=definition.version,
                    risk_level=definition.risk_level,
                    target_resource=proposal.arguments.get("path"),
                    plan_step_id=plan_step_id,
                    approved_by_user=approved_by_user,
                    logical_action_id=logical_action_id,
                )
            )
            if action is None:
                raise LookupError(f"action {action_id} não encontrada")
            logical_action_id = action.logical_action_id

            # Duplicate detection (C09): se uma tentativa desta MESMA ação
            # lógica já confirmou o efeito, repetir seria criar um segundo.
            previous = await self._successful_attempt(session, action.id)
            if previous is not None:
                await session.commit()
                return ExecutionOutcome(
                    action_id=action.id,
                    logical_action_id=logical_action_id,
                    attempt_no=previous.attempt_no,
                    result=ToolResult.succeeded((previous.result or {}).get("output")),
                    suppressed_duplicate=True,
                )

            attempt = await repo.start_attempt(action.id, now=datetime.now(UTC))
            attempt_id, attempt_no = attempt.id, attempt.attempt_no
            action_pk, arguments = action.id, dict(action.arguments)
            # C08: o marcador precisa estar durável ANTES da chamada.
            await session.commit()

        result = await self._invoke(entry, arguments)

        probe: CriterionOutcome | None = None
        probe_result: ProbeResult | None = None
        if result.status is ExecutionStatus.UNKNOWN:
            probe, probe_result, result = await self._resolve_effect(
                definition, arguments, result
            )

        async with self.session_factory() as session:
            await ActionRepository(session).finish_attempt(
                attempt_id,
                status=_attempt_status(result),
                now=datetime.now(UTC),
                result={"output": result.output} if result.output is not None else None,
                error_code=result.error_code,
                error_detail=result.error_detail,
                probe_outcome=probe.model_dump(mode="json") if probe else None,
            )
            await session.commit()

        return ExecutionOutcome(
            action_id=action_pk,
            logical_action_id=logical_action_id,
            attempt_no=attempt_no,
            result=result,
            probe=probe,
            probe_result=probe_result,
        )

    async def _invoke(self, entry: RegisteredTool, arguments: dict) -> ToolResult:
        started = datetime.now(UTC)
        try:
            output = await asyncio.wait_for(
                entry.handler(arguments), timeout=entry.definition.timeout_seconds
            )
        except TimeoutError:
            # spec §3: `timeout != action_failed`. Sem efeito colateral o
            # timeout é falha limpa; com efeito, o desfecho é desconhecido.
            if entry.definition.side_effects:
                return ToolResult.unknown_effect(
                    "timeout com efeito possível", started_at=started
                )
            return ToolResult.failed(
                ErrorCode.TOOL_TIMEOUT, "timeout sem efeito colateral", started_at=started
            )
        except Exception as error:  # noqa: BLE001 - adapter traduz para a taxonomia
            code = getattr(error, "error_code", ErrorCode.TOOL_PERMANENT_ERROR)
            return ToolResult.failed(code, str(error), started_at=started)
        return ToolResult.succeeded(
            output, started_at=started, finished_at=datetime.now(UTC)
        )

    # -------------------------------------------------------------- recovery

    async def _resolve_effect(
        self, definition: ToolDefinition, arguments: dict, result: ToolResult
    ) -> tuple[CriterionOutcome | None, ProbeResult | None, ToolResult]:
        """Correção C05: `UNKNOWN_EFFECT → probe → confirmado/ausente/?`."""
        if definition.effect_probe is None:
            return None, "INDETERMINATE", result

        criterion = definition.effect_probe.build(arguments)
        outcome = await self.evaluator.evaluate(
            criterion,
            EvaluationContext(
                sandbox=self.sandbox,
                http_prober=self.http_prober,
                now=datetime.now(UTC),
            ),
        )
        if outcome.satisfied is True:
            return outcome, "EFFECT_PRESENT", ToolResult.succeeded(
                result.output, started_at=result.started_at
            )
        if outcome.satisfied is False:
            return outcome, "EFFECT_ABSENT", ToolResult.failed(
                ErrorCode.TOOL_TRANSIENT_ERROR,
                "efeito ausente confirmado pelo probe",
                started_at=result.started_at,
            )
        # Probe indeterminado mantém UNKNOWN: só o humano desempata.
        return outcome, "INDETERMINATE", result

    async def recover_in_flight(self, run_id: UUID) -> list[ExecutionOutcome]:
        """Resolve tentativas deixadas em voo por um crash.

        Chamado ao reentrar em `RECOVERING`. Nenhuma ação nova é executada:
        apenas se pergunta ao mundo o que aconteceu e se fecha o attempt.
        """
        outcomes: list[ExecutionOutcome] = []
        async with self.session_factory() as session:
            dangling = await ActionRepository(session).in_flight_attempts(run_id)
            pending = [
                (a.id, a.attempt_no, a.action_id) for a in dangling
            ]
            actions = {
                aid: await ActionRepository(session).get(aid)
                for aid in {a.action_id for a in dangling}
            }

        for attempt_id, attempt_no, action_id in pending:
            action = actions[action_id]
            definition = self.registry.get(action.tool, action.tool_version).definition
            probe, probe_result, result = await self._resolve_effect(
                definition,
                dict(action.arguments),
                ToolResult.unknown_effect("attempt em voo após restart"),
            )
            async with self.session_factory() as session:
                await ActionRepository(session).finish_attempt(
                    attempt_id,
                    status=_attempt_status(result),
                    now=datetime.now(UTC),
                    error_code=result.error_code,
                    error_detail=result.error_detail,
                    probe_outcome=probe.model_dump(mode="json") if probe else None,
                )
                await session.commit()
            outcomes.append(
                ExecutionOutcome(
                    action_id=action_id,
                    logical_action_id=action.logical_action_id,
                    attempt_no=attempt_no,
                    result=result,
                    probe=probe,
                    probe_result=probe_result,
                )
            )
        return outcomes

    # ----------------------------------------------------------- loop check

    async def detect_loop(
        self, run_id: UUID, fingerprint: str, *, progressed_since: bool
    ) -> bool:
        """C09: repetição sem progresso verificado entre as ocorrências."""
        async with self.session_factory() as session:
            occurrences = await ActionRepository(session).count_fingerprint(
                run_id, fingerprint
            )
        return is_loop(occurrences, progressed_since=progressed_since)

    # --------------------------------------------------------------- apoio

    async def _successful_attempt(
        self, session: AsyncSession, action_id: UUID
    ) -> models.ActionAttempt | None:
        return await session.scalar(
            select(models.ActionAttempt)
            .where(
                models.ActionAttempt.action_id == action_id,
                models.ActionAttempt.status == AttemptStatus.SUCCESS.value,
            )
            .order_by(models.ActionAttempt.attempt_no)
            .limit(1)
        )


def _attempt_status(result: ToolResult) -> AttemptStatus:
    return {
        ExecutionStatus.SUCCESS: AttemptStatus.SUCCESS,
        ExecutionStatus.FAILURE: AttemptStatus.FAILED,
        ExecutionStatus.UNKNOWN: AttemptStatus.UNKNOWN,
    }[result.status]
