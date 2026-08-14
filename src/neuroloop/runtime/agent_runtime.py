"""Agent Runtime — TASK-012 (spec §7).

O loop completo, substituindo o walking skeleton:

    percepção → memória → contexto → gates → Fast Path ou Deliberator
              → policy → executor → verifier → episódio → próximo ciclo

O runtime **dirige**; ele não decide nada sozinho. Cada decisão pertence a
um componente com responsabilidade única, e a regra estrutural da spec é o
que este módulo existe para não violar:

    Executor não decide. Planner não executa.
    Verifier não planeja. LLM não autoriza.

As correções que só se manifestam aqui, no loop:

- **C04** retry contado por `logical_action_id`, incrementado e persistido
  *antes* da nova tentativa — senão um crash entre incremento e execução
  perde a conta e o retry vira infinito através de restarts.
- **C05** efeito não resolvido tem precedência sobre tudo: `RECOVERING`
  antes de qualquer nova deliberação.
- **C07** limite de replan checado *antes* de trocar o plano ativo.
- **C12** tokens e custo do LLM creditados no checkpoint a cada chamada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neuroloop.cognition.deliberator import DeliberationError, Deliberator
from neuroloop.cognition.fast_path import FastPath, FastPathMatch, FastPathSource
from neuroloop.cognition.planner import PlannerValidator, PlanValidationError
from neuroloop.cognition.skills import SkillRegistry
from neuroloop.context.workspace import ContextBudget, RecentError, WorkingContext, WorkspaceBuilder
from neuroloop.core.actions import ActionProposal
from neuroloop.core.criteria import CriterionOutcome
from neuroloop.core.decisions import (
    ActDecision,
    AskUserDecision,
    ImpossibleDecision,
    PlanDecision,
)
from neuroloop.core.enums import (
    ErrorCode,
    ExecutionStatus,
    NextAction,
    PlanStepStatus,
    RunPhase,
    TrustLevel,
)
from neuroloop.core.goals import Goal
from neuroloop.core.plans import Plan
from neuroloop.core.runs import ExecutionBudget, RunCheckpoint
from neuroloop.llm.client import LLMClient
from neuroloop.memory.episodes import EpisodeRecorder
from neuroloop.memory.plan_cache import PlanCache
from neuroloop.memory.retrieval import MemoryRetriever, query_from_context
from neuroloop.observability.context import (
    ComponentVersions,
    CycleTrace,
    TraceContext,
    fingerprint,
    new_trace_id,
    registry_fingerprint,
)
from neuroloop.observability.tracing import (
    SPAN_DECIDE,
    SPAN_EXECUTE,
    SPAN_MEMORY_STORE,
    SPAN_PERCEPTION,
    SPAN_VERIFY,
    RunEventTracer,
    Tracer,
    span,
)
from neuroloop.perception.normalizer import PerceptionNormalizer
from neuroloop.persistence.repositories import (
    ActionRepository,
    ObservationRepository,
    PlanRepository,
    RunEventRepository,
    RunRepository,
)
from neuroloop.runtime.executor import DurableExecutor, RetryPolicy
from neuroloop.runtime.state_machine import RunStateMachine, resume_phase_for
from neuroloop.security.policy import GateType, PolicyEngine, TaintContext, default_policy
from neuroloop.tools.registry import ToolRegistry
from neuroloop.tools.sandbox import Sandbox
from neuroloop.verification.evaluator import CriterionEvaluator, EvaluationContext
from neuroloop.verification.verifier import ExecutionReport, Verifier


@dataclass(slots=True)
class RunResult:
    run_id: UUID
    phase: RunPhase
    iteration: int
    error_code: ErrorCode | None = None
    waiting_reason: str | None = None
    goal_outcomes: list[CriterionOutcome] = field(default_factory=list)
    tokens_used: int = 0
    cost_used_usd: Decimal = Decimal("0")
    deliberations: int = 0
    fast_path_hits: dict[str, int] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.phase is RunPhase.COMPLETED


class _Terminated(Exception):  # noqa: N818 - controle de fluxo interno
    def __init__(self, result: RunResult) -> None:
        self.result = result
        super().__init__(result.phase.value)


class AgentRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
        sandbox: Sandbox,
        llm: LLMClient,
        policy: PolicyEngine | None = None,
        skills: SkillRegistry | None = None,
        evaluator: CriterionEvaluator | None = None,
        executor: DurableExecutor | None = None,
        verifier: Verifier | None = None,
        context_budget: ContextBudget | None = None,
        tracer: Tracer | None = None,
        http_prober=None,
        owner: str = "runtime",
        agent_name: str = "neuroloop",
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.sandbox = sandbox
        self.owner = owner
        self.agent_name = agent_name

        self.evaluator = evaluator or CriterionEvaluator()
        self.http_prober = http_prober
        self.policy = policy or default_policy(sandbox)
        self.executor = executor or DurableExecutor(
            session_factory=session_factory,
            registry=registry,
            sandbox=sandbox,
            evaluator=self.evaluator,
            http_prober=http_prober,
        )
        self.verifier = verifier or Verifier(self.evaluator)
        self.perception = PerceptionNormalizer()
        self.workspace = WorkspaceBuilder(context_budget or ContextBudget())
        self.fast_path = FastPath(
            registry=registry, skills=skills, evaluator=self.evaluator
        )
        self.deliberator = Deliberator(llm=llm, registry=registry)
        self.planner = PlannerValidator(registry)
        self.retry_policy = RetryPolicy()
        self.tracer = tracer or RunEventTracer(session_factory)
        # Versoes resolvidas uma vez: e o que torna uma decisao passada
        # reproduzivel (spec §33).
        self.versions = ComponentVersions(
            model=self.deliberator.model_profile.model,
            tool_registry=registry_fingerprint(
                {entry.definition.name: entry.definition.version for entry in registry}
            ),
            policy=fingerprint(sorted(self.policy.config.granted_capabilities)),
            skills=registry_fingerprint(
                {skill.id: skill.version for skill in self.fast_path.skills.all()}
            ),
        )

    # ---------------------------------------------------------------- API

    async def start(
        self, goal: Goal, *, budget: ExecutionBudget | None = None
    ) -> RunCheckpoint:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            checkpoint = await RunRepository(session).create(
                goal_id=goal.id, started_at=now, budget=budget or ExecutionBudget()
            )
            await ObservationRepository(session).append(
                self.perception.from_goal(goal, run_id=checkpoint.run_id, now=now)
            )
            await session.commit()
        return checkpoint

    async def cancel(self, run_id: UUID) -> None:
        async with self.session_factory() as session:
            await RunRepository(session).request_cancel(run_id)
            await session.commit()

    async def resume(self, run_id: UUID, *, message: str | None = None) -> RunResult:
        if message is not None:
            async with self.session_factory() as session:
                await ObservationRepository(session).append(
                    self.perception.from_user(
                        run_id=run_id, message=message, now=datetime.now(UTC)
                    )
                )
                await session.commit()
        return await self.run_until_pause(run_id)

    async def run_until_pause(self, run_id: UUID) -> RunResult:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            repo = RunRepository(session)
            checkpoint = await repo.load(run_id)
            resume_state = await repo.resume_state(run_id)
            lease = await repo.acquire_lease(run_id, owner=self.owner, now=now)
            goal = await _load_goal(session, checkpoint.goal_id)
            await session.commit()

        machine = RunStateMachine(phase=RunPhase.CREATED)
        state = _RunState(
            goal=goal, checkpoint=checkpoint, lease=lease, machine=machine
        )
        # C08: a fase de retomada vem do estado durável, não do checkpoint.
        state.resume_phase = resume_phase_for(resume_state)

        try:
            return await self._loop(state)
        except _Terminated as stop:
            return stop.result

    # --------------------------------------------------------------- loop

    def _trace(self, state: _RunState) -> CycleTrace:
        return CycleTrace(
            context=TraceContext(
                trace_id=state.trace_id,
                run_id=state.checkpoint.run_id,
                goal_id=state.goal.id,
                cycle_id=f"{state.checkpoint.run_id.hex[:8]}-{state.checkpoint.iteration}",
                iteration=state.checkpoint.iteration,
                phase=state.machine.phase,
                state_version=state.checkpoint.state_version,
            ),
            versions=self.versions,
        )

    async def _loop(self, state: _RunState) -> RunResult:
        while True:
            async with self.session_factory() as session:
                state.checkpoint = await RunRepository(session).load(
                    state.checkpoint.run_id
                )

            gate = self.policy.pre_decision(state.checkpoint, now=datetime.now(UTC))
            if gate.type is GateType.RECOVER:
                await self._recover(state)
                continue
            if not gate.proceeds:
                return await self._finish(
                    state,
                    _phase_for_gate(gate),
                    error_code=gate.error_code,
                    waiting_reason=(
                        None if gate.type is GateType.STOP else gate.reason_code
                    ),
                )

            self._transition(state, RunPhase.PERCEIVING, "topo do ciclo")

            if state.checkpoint.iteration == 0:
                await self._capture_baseline(state)
                continue

            async with span(self.tracer, self._trace(state), SPAN_PERCEPTION) as detalhe:
                context = await self._build_context(state)
                detalhe["observations"] = len(context.observations)
                detalhe["memories"] = len(context.memories)
                detalhe["dropped"] = len(context.dropped)

            verdict = await self.verifier.verify_goal(
                state.goal, state.checkpoint, self._evaluation(state)
            )
            state.goal_outcomes = list(verdict.outcomes)
            if verdict.conclusive:
                return await self._finish(state, RunPhase.COMPLETED)
            if verdict.pre_satisfied:
                return await self._finish(
                    state,
                    RunPhase.WAITING_USER,
                    error_code=ErrorCode.GOAL_PRE_SATISFIED,
                    waiting_reason="GOAL_PRE_SATISFIED",
                )

            self._transition(state, RunPhase.DELIBERATING, "selecionar ação")
            proposal, match = await self._choose_action(state, context)
            if proposal is None:
                continue

            await self._act(state, context, proposal, match)

    # ---------------------------------------------------------- percepção

    async def _build_context(self, state: _RunState) -> WorkingContext:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            observations = await ObservationRepository(session).pending(
                state.checkpoint.run_id
            )
            plan = await PlanRepository(session).active(state.checkpoint.run_id)
            memories = await MemoryRetriever(session).retrieve(
                query_from_context(
                    run_id=state.checkpoint.run_id,
                    tools=tuple(self.registry.names()),
                    resources=tuple(_goal_resources(state.goal)),
                ),
                now=now,
            )
            confianca = await ObservationRepository(session).trust_map(
                state.checkpoint.run_id
            )
            await ObservationRepository(session).mark_consumed(
                [o.id for o in observations], now=now
            )
            await session.commit()

        state.taint = TaintContext(
            trust_by_observation={
                oid: TrustLevel(trust) for oid, trust in confianca.items()
            }
        )
        state.observations = observations
        return self.workspace.build(
            goal=state.goal,
            checkpoint=state.checkpoint,
            now=now,
            plan=plan,
            observations=tuple(observations),
            memories=tuple(memories),
            errors=tuple(state.errors[-3:]),
            tools=self.registry.summaries(),
            seen_hashes=state.seen_hashes,
        )

    # ------------------------------------------------------------ decisão

    async def _choose_action(
        self, state: _RunState, context: WorkingContext
    ) -> tuple[ActionProposal | None, FastPathMatch | None]:
        match = await self.fast_path.match(
            context,
            evaluation=self._evaluation(state),
            inputs=state.skill_inputs,
            has_unresolved_failure=state.unresolved_failure,
        )
        if match is not None:
            state.fast_path_hits[match.source.value] = (
                state.fast_path_hits.get(match.source.value, 0) + 1
            )
            return match.action, match

        try:
            async with span(self.tracer, self._trace(state), SPAN_DECIDE) as detalhe:
                result = await self.deliberator.decide(context)
                detalhe["decision"] = result.decision.type
                detalhe["reason_code"] = result.decision.reason_code
                detalhe["tokens"] = result.usage.total_tokens
        except DeliberationError as error:
            await self._terminate(
                state, RunPhase.FAILED, error_code=error.error_code
            )
            return None, None

        state.deliberations += 1
        await self._charge_usage(state, result.usage)

        match result.decision:
            case ActDecision(action=action):
                return action, None
            case PlanDecision(plan=plan):
                await self._install_plan(state, plan)
                return None, None
            case AskUserDecision(request=request):
                await self._terminate(
                    state, RunPhase.WAITING_USER, waiting_reason=request.type
                )
            case ImpossibleDecision():
                await self._terminate(
                    state, RunPhase.FAILED, error_code=ErrorCode.IMPOSSIBLE_TASK
                )
        return None, None

    async def _install_plan(self, state: _RunState, plan: Plan) -> None:
        checkpoint = state.checkpoint
        is_replan = checkpoint.active_plan_id is not None
        # C07: o limite é checado ANTES de trocar o plano ativo.
        if is_replan and checkpoint.replan_count + 1 > checkpoint.budget.max_replans:
            await self._terminate(
                state, RunPhase.FAILED, error_code=ErrorCode.REPLAN_LIMIT
            )

        self._transition(state, RunPhase.PLANNING, "instalar plano")
        try:
            self.planner.validate(plan)
        except PlanValidationError as error:
            state.errors.append(
                RecentError(
                    error_code=ErrorCode.INVALID_PLAN,
                    detail=str(error),
                    at=datetime.now(UTC),
                )
            )
            await self._terminate(
                state, RunPhase.FAILED, error_code=ErrorCode.INVALID_PLAN
            )

        async with self.session_factory() as session:
            await PlanRepository(session).replace_active(
                checkpoint.run_id, plan, now=datetime.now(UTC)
            )
            state.checkpoint = await RunRepository(session).save(
                checkpoint.model_copy(
                    update={
                        "active_plan_id": plan.id,
                        "active_plan_version": plan.version,
                        "plan_generation_count": checkpoint.plan_generation_count + 1,
                        "replan_count": checkpoint.replan_count + (1 if is_replan else 0),
                    }
                ),
                lease=state.lease,
            )
            await session.commit()
        state.active_plan = plan
        self._transition(state, RunPhase.PERCEIVING, "plano instalado")

    # ------------------------------------------------------------ execução

    async def _act(
        self,
        state: _RunState,
        context: WorkingContext,
        proposal: ActionProposal,
        match: FastPathMatch | None,
    ) -> None:
        entry = self.registry.get(proposal.tool)
        # C04 + C09: o retry de um step reusa a MESMA ação lógica. Criar uma
        # ação nova a cada tentativa zeraria o contador de retry (o limite
        # nunca dispararia) e trocaria a chave de idempotência entre
        # tentativas — que é exatamente como se duplica um efeito externo.
        reused_id = state.step_actions.get(match.step_id) if match and match.step_id else None

        async with self.session_factory() as session:
            if reused_id is not None:
                action_id = reused_id
            else:
                # A ação é registrada ANTES de autorizar: proposta recusada é
                # dado de auditoria (`unsafe_action_proposal_rate`), e uma
                # aprovação humana precisa de um id concreto a que se vincular.
                action = await ActionRepository(session).create_logical_action(
                    run_id=state.checkpoint.run_id,
                    proposal=proposal,
                    tool_version=entry.definition.version,
                    risk_level=entry.definition.risk_level,
                    target_resource=proposal.arguments.get("path"),
                    plan_step_id=match.step_id if match else None,
                )
                action_id = action.id
                if match is not None and match.step_id:
                    state.step_actions[match.step_id] = action_id
            # C19: aprovação é o que o humano confirmou, não o que foi pedido.
            # Ler `pending_approval_fingerprint` como se fosse aprovação faria
            # o run se auto-autorizar ao retomar.
            aprovadas = await ActionRepository(session).approved_fingerprints(
                state.checkpoint.run_id
            )
            await session.commit()
        authorization = self.policy.authorize(
            proposal,
            entry.definition,
            taint=state.taint,
            approved_fingerprints=aprovadas,
            target_resource=proposal.arguments.get("path"),
        )

        async with self.session_factory() as session:
            await RunEventRepository(session).append(
                run_id=state.checkpoint.run_id,
                iteration=state.checkpoint.iteration,
                kind="ACTION_AUTHORIZATION",
                reason=authorization.reason_code,
                error_code=authorization.error_code,
                payload={
                    "action_id": str(action_id),
                    "tool": proposal.tool,
                    "risk": authorization.risk_level.value,
                    "tainted": authorization.tainted,
                    "source": match.source.value if match else "DELIBERATOR",
                    **self.versions.as_payload(),
                },
                trace_id=state.trace_id,
            )
            await session.commit()

        state.pending_action_id = action_id

        if not authorization.allowed:
            await self._terminate(
                state,
                RunPhase.FAILED,
                error_code=authorization.error_code or ErrorCode.PERMISSION_DENIED,
            )
        if authorization.requires_user_approval:
            await self._terminate(
                state,
                RunPhase.WAITING_USER,
                waiting_reason=authorization.reason_code,
                pending_fingerprint=authorization.action_fingerprint,
            )

        self._transition(state, RunPhase.EXECUTING, f"tool {proposal.tool}")
        async with span(
            self.tracer, self._trace(state), SPAN_EXECUTE, tool=proposal.tool
        ) as detalhe:
            outcome = await self.executor.execute(
                run_id=state.checkpoint.run_id,
                proposal=proposal,
                entry=entry,
                action_id=action_id,
            )
            detalhe["status"] = outcome.result.status.value
            detalhe["attempt_no"] = outcome.attempt_no
        result = outcome.result

        if outcome.effect_unknown:
            self._transition(state, RunPhase.RECOVERING, "efeito indeterminado")

        now = datetime.now(UTC)
        async with self.session_factory() as session:
            await ObservationRepository(session).append(
                self.perception.from_tool_result(
                    run_id=state.checkpoint.run_id,
                    definition=entry.definition,
                    arguments=proposal.arguments,
                    output=result.output,
                    action_id=outcome.action_id,
                    now=now,
                    succeeded=result.status is ExecutionStatus.SUCCESS,
                )
            )
            await session.commit()

        self._transition(state, RunPhase.VERIFYING, "conferir efeito")
        retry = self.retry_policy.decide(state.checkpoint, entry.definition, outcome)
        async with span(self.tracer, self._trace(state), SPAN_VERIFY) as detalhe:
            verification = await self._verify(state, proposal, outcome, retry, result)
            detalhe["next_action"] = verification.next_action.value
            detalhe["goal_satisfied"] = verification.goal_satisfied
            detalhe["confidence"] = verification.confidence

        if result.error_code is not None:
            state.errors.append(
                RecentError(
                    error_code=result.error_code,
                    detail=result.error_detail,
                    action_id=outcome.action_id,
                    at=now,
                )
            )

        self._transition(state, RunPhase.UPDATING_MEMORY, "registrar episódio")
        async with span(self.tracer, self._trace(state), SPAN_MEMORY_STORE):
            await self._remember(
                state, context, proposal, outcome, verification, entry, match
            )
        await self._advance(state, outcome, verification, retry, match)

    async def _verify(self, state, proposal, outcome, retry, result):
        return await self.verifier.evaluate(
            goal=state.goal,
            checkpoint=state.checkpoint,
            report=ExecutionReport(
                status=result.status,
                output=result.output,
                error_code=result.error_code,
                probe_result=outcome.probe_result,
                retry_available=retry.should_retry,
            ),
            expected_outcomes=proposal.expected_outcomes,
            ctx=self._evaluation(state, action_result=result.output),
        )

    async def _remember(
        self, state, context, proposal, outcome, verification, entry, match
    ) -> None:
        async with self.session_factory() as session:
            if match is not None and match.source is FastPathSource.STEP:
                await PlanRepository(session).mark_step(
                    state.checkpoint.run_id,
                    match.step_id,
                    PlanStepStatus.DONE
                    if verification.expected_outcomes_satisfied is True
                    else PlanStepStatus.FAILED,
                )
            await EpisodeRecorder(session).record(
                run_id=state.checkpoint.run_id,
                iteration=state.checkpoint.iteration,
                goal_summary=state.goal.description,
                observation_summary=f"{proposal.tool} via {match.source.value if match else 'DELIBERATOR'}",
                verification=verification,
                tool_name=proposal.tool,
                resource=proposal.arguments.get("path"),
                action_id=outcome.action_id,
                plan_step_id=match.step_id if match else None,
                risk_level=entry.definition.risk_level,
            )
            await session.commit()

    async def _advance(self, state, outcome, verification, retry, match) -> None:
        checkpoint = state.checkpoint
        retry_counts = dict(checkpoint.retry_counts)
        if verification.next_action is NextAction.RETRY and not retry.should_retry:
            # Limite alcançado ou retry não provado seguro: o run para em vez
            # de tentar para sempre.
            await self._terminate(
                state,
                RunPhase.WAITING_USER
                if retry.requires_user
                else RunPhase.FAILED,
                error_code=retry.error_code,
                waiting_reason=retry.reason_code,
            )
        if verification.next_action is NextAction.RETRY and retry.should_retry:
            # C04: incrementa e persiste ANTES de tentar de novo.
            retry_counts[outcome.logical_action_id] = (
                retry_counts.get(outcome.logical_action_id, 0) + 1
            )
        if match is not None and match.source is FastPathSource.STEP:
            if verification.next_action is NextAction.RETRY:
                await self._reopen_step(state, match.step_id)

        async with self.session_factory() as session:
            state.checkpoint = await RunRepository(session).save(
                checkpoint.model_copy(
                    update={
                        "iteration": checkpoint.iteration + 1,
                        "current_step_id": match.step_id if match else None,
                        "last_action_id": outcome.action_id,
                        "last_verified_action_id": outcome.action_id,
                        "retry_counts": retry_counts,
                        "unresolved_effect_action_id": (
                            outcome.action_id if outcome.effect_unknown else None
                        ),
                    }
                ),
                lease=state.lease,
            )
            await session.commit()

        state.unresolved_failure = verification.next_action in (
            NextAction.ASK_USER,
            NextAction.STOP_FAILURE,
        )

        match verification.next_action:
            case NextAction.GOAL_COMPLETED:
                await self._terminate(state, RunPhase.COMPLETED)
            case NextAction.STOP_FAILURE:
                await self._terminate(
                    state, RunPhase.FAILED, error_code=verification.error_code
                )
            case NextAction.ASK_USER:
                await self._terminate(
                    state,
                    RunPhase.WAITING_USER,
                    error_code=verification.error_code,
                    waiting_reason=(
                        verification.error_code.value if verification.error_code else None
                    ),
                )
            case NextAction.REPLAN:
                async with self.session_factory() as session:
                    await PlanRepository(session).invalidate_active(
                        state.checkpoint.run_id, now=datetime.now(UTC)
                    )
                    await session.commit()
                state.active_plan = None
            case _:
                pass
        self._transition(state, RunPhase.PERCEIVING, "próximo ciclo")

    async def _reopen_step(self, state: _RunState, step_id: str) -> None:
        async with self.session_factory() as session:
            await PlanRepository(session).mark_step(
                state.checkpoint.run_id, step_id, PlanStepStatus.PENDING
            )
            await session.commit()

    # --------------------------------------------------------- recuperação

    async def _recover(self, state: _RunState) -> None:
        """C05: resolver o efeito antes de qualquer nova decisão."""
        self._transition(state, RunPhase.RECOVERING, "efeito não resolvido")
        outcomes = await self.executor.recover_in_flight(state.checkpoint.run_id)
        now = datetime.now(UTC)

        indeterminado = any(o.probe_result == "INDETERMINATE" for o in outcomes)
        async with self.session_factory() as session:
            for outcome in outcomes:
                await ObservationRepository(session).append(
                    self.perception.from_probe(
                        run_id=state.checkpoint.run_id,
                        action_id=outcome.action_id,
                        outcome={"probe": outcome.probe_result},
                        now=now,
                    )
                )
            state.checkpoint = await RunRepository(session).save(
                state.checkpoint.model_copy(update={"unresolved_effect_action_id": None}),
                lease=state.lease,
            )
            await session.commit()

        if indeterminado:
            await self._terminate(
                state,
                RunPhase.WAITING_USER,
                error_code=ErrorCode.UNKNOWN_SIDE_EFFECT,
                waiting_reason="AMBIGUOUS_EFFECT",
            )

    # -------------------------------------------------------------- apoio

    async def _capture_baseline(self, state: _RunState) -> None:
        """C02: fotografa o estado externo antes de qualquer ação."""
        outcomes = await self.evaluator.evaluate_all(
            state.goal.external_success_criteria, self._evaluation(state)
        )
        async with self.session_factory() as session:
            state.checkpoint = await RunRepository(session).save(
                state.checkpoint.model_copy(
                    update={"baseline_outcomes": tuple(outcomes), "iteration": 1}
                ),
                lease=state.lease,
            )
            await session.commit()

    async def _charge_usage(self, state: _RunState, usage) -> None:
        """C12: sem isto o budget do run nunca se move."""
        async with self.session_factory() as session:
            state.checkpoint = await RunRepository(session).save(
                state.checkpoint.model_copy(
                    update={
                        "tokens_used": state.checkpoint.tokens_used + usage.total_tokens,
                        "cost_used_usd": state.checkpoint.cost_used_usd + usage.cost_usd,
                    }
                ),
                lease=state.lease,
            )
            await session.commit()

    def _evaluation(self, state: _RunState, action_result=None) -> EvaluationContext:
        return EvaluationContext(
            sandbox=self.sandbox,
            action_result=action_result,
            http_prober=self.http_prober,
            now=datetime.now(UTC),
        )

    def _transition(self, state: _RunState, phase: RunPhase, reason: str) -> None:
        if state.machine.phase is phase:
            return
        if not state.machine.can(phase):
            # Retomada entra no meio do ciclo; realinhar sem inventar aresta.
            state.machine = RunStateMachine(phase=phase)
            return
        state.machine.transition(phase, reason=reason)

    async def _terminate(self, state: _RunState, phase: RunPhase, **kwargs) -> None:
        raise _Terminated(await self._finish(state, phase, **kwargs))

    async def _finish(
        self,
        state: _RunState,
        phase: RunPhase,
        *,
        error_code: ErrorCode | None = None,
        waiting_reason: str | None = None,
        pending_fingerprint: str | None = None,
    ) -> RunResult:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            state.checkpoint = await RunRepository(session).save(
                state.checkpoint.model_copy(
                    update={
                        "phase": phase,
                        "waiting_reason": waiting_reason,
                        "pending_approval_fingerprint": pending_fingerprint,
                        "pending_approval_action_id": (
                            state.pending_action_id if pending_fingerprint else None
                        ),
                    }
                ),
                lease=state.lease,
            )
            for record in state.machine.history:
                await RunEventRepository(session).record_transition(
                    run_id=state.checkpoint.run_id,
                    iteration=state.checkpoint.iteration,
                    from_phase=record.from_phase.value,
                    to_phase=record.to_phase.value,
                    reason=record.reason,
                    error_code=record.error_code,
                    at=record.at,
                )
            plan = await PlanRepository(session).active(state.checkpoint.run_id)
            if plan is not None and phase in (RunPhase.COMPLETED, RunPhase.FAILED):
                # C16: o cache aprende com os dois desfechos.
                await PlanCache(session).record(
                    state.goal,
                    plan,
                    frozenset(self.registry.names()),
                    succeeded=phase is RunPhase.COMPLETED,
                    now=now,
                )
            await RunRepository(session).release_lease(state.lease)
            await session.commit()

        return RunResult(
            run_id=state.checkpoint.run_id,
            phase=phase,
            iteration=state.checkpoint.iteration,
            error_code=error_code,
            waiting_reason=waiting_reason,
            goal_outcomes=state.goal_outcomes,
            tokens_used=state.checkpoint.tokens_used,
            cost_used_usd=state.checkpoint.cost_used_usd,
            deliberations=state.deliberations,
            fast_path_hits=dict(state.fast_path_hits),
        )


@dataclass(slots=True)
class _RunState:
    goal: Goal
    checkpoint: RunCheckpoint
    lease: object
    machine: RunStateMachine
    resume_phase: RunPhase | None = None
    active_plan: Plan | None = None
    observations: list = field(default_factory=list)
    errors: list[RecentError] = field(default_factory=list)
    goal_outcomes: list[CriterionOutcome] = field(default_factory=list)
    taint: TaintContext = field(default_factory=TaintContext)
    skill_inputs: dict = field(default_factory=dict)
    pending_action_id: UUID | None = None
    step_actions: dict[str, UUID] = field(default_factory=dict)
    unresolved_failure: bool = False
    """Falha que o Verifier NAO encaminhou para retry/replan (spec §12).

    Distinta de `errors`, que e historico para o contexto. Uma falha em
    retry esta sendo tratada: desligar o Fast Path por causa dela faria
    justamente o passo conhecido voltar ao LLM a cada tentativa."""
    trace_id: str = field(default_factory=new_trace_id)
    seen_hashes: frozenset[str] = frozenset()
    deliberations: int = 0
    fast_path_hits: dict[str, int] = field(default_factory=dict)


def _phase_for_gate(gate) -> RunPhase:
    if gate.type is GateType.STOP:
        return (
            RunPhase.CANCELLED
            if gate.error_code is ErrorCode.CANCELLED
            else RunPhase.FAILED
        )
    return RunPhase.WAITING_USER


def _goal_resources(goal: Goal) -> list[str]:
    found: list[str] = []
    for criterion in goal.success_criteria:
        for attr in ("path", "url"):
            value = getattr(criterion, attr, None)
            if isinstance(value, str):
                found.append(value)
    return found


async def _load_goal(session: AsyncSession, goal_id: UUID) -> Goal:
    from neuroloop.persistence.repositories import GoalRepository

    return await GoalRepository(session).get(goal_id)


__all__ = ["AgentRuntime", "RunResult"]
