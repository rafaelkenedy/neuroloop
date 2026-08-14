"""State machine do run — TASK-002.

Implementa a tabela de transições da correção C06. A spec original tinha três
defeitos que este módulo fecha por construção:

- `WAITING_EXTERNAL` e `BLOCKED` não tinham aresta alguma — estados mortos.
  Foram removidos do enum na V0.
- Nenhuma aresta levava a `CANCELLED`, apesar de `cancel_requested` poder
  disparar a qualquer ciclo.
- `transition()` era mutação em memória e a fase persistida não era confiável
  após crash. Aqui a fase de retomada é **derivada** do estado durável
  (`derive_resume_phase`), nunca lida diretamente do checkpoint.

Estados terminais são selados: qualquer tentativa de sair deles é
`STATE_CONFLICT`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from neuroloop.core.enums import ErrorCode, NextAction, RunPhase

P = RunPhase

ALLOWED_TRANSITIONS: Mapping[RunPhase, frozenset[RunPhase]] = {
    P.CREATED: frozenset({P.PERCEIVING, P.CANCELLED}),
    P.PERCEIVING: frozenset(
        {
            P.DELIBERATING,
            P.RECOVERING,
            P.COMPLETED,
            P.WAITING_USER,
            P.FAILED,
            P.CANCELLED,
        }
    ),
    P.DELIBERATING: frozenset({P.PLANNING, P.EXECUTING, P.WAITING_USER, P.FAILED}),
    P.PLANNING: frozenset({P.PERCEIVING, P.FAILED}),
    P.EXECUTING: frozenset({P.VERIFYING, P.RECOVERING}),
    P.RECOVERING: frozenset({P.VERIFYING, P.WAITING_USER}),
    P.VERIFYING: frozenset({P.UPDATING_MEMORY}),
    P.UPDATING_MEMORY: frozenset({P.PERCEIVING, P.COMPLETED, P.FAILED, P.WAITING_USER}),
    P.WAITING_USER: frozenset({P.PERCEIVING, P.CANCELLED}),
    P.COMPLETED: frozenset(),
    P.FAILED: frozenset(),
    P.CANCELLED: frozenset(),
}

CANCELLABLE_PHASES: frozenset[RunPhase] = frozenset(
    {P.CREATED, P.PERCEIVING, P.WAITING_USER}
)
"""Fases em que o cancelamento é honrado imediatamente.

Fora delas o pedido não é perdido: `cancel_requested` fica no checkpoint e é
honrado no topo do próximo ciclo (`PERCEIVING`). Cancelar no meio de uma
execução externa não é possível sem deixar efeito indeterminado — a decisão
arquitetural é esperar o efeito se resolver antes de encerrar.
"""

_NEXT_ACTION_TARGET: Mapping[NextAction, RunPhase] = {
    NextAction.CONTINUE: P.PERCEIVING,
    NextAction.RETRY: P.PERCEIVING,
    NextAction.REPLAN: P.PERCEIVING,
    NextAction.ASK_USER: P.WAITING_USER,
    NextAction.GOAL_COMPLETED: P.COMPLETED,
    NextAction.STOP_FAILURE: P.FAILED,
}


class TransitionError(RuntimeError):
    """Transição não permitida pela tabela. Mapeia para `STATE_CONFLICT`."""

    error_code = ErrorCode.STATE_CONFLICT

    def __init__(self, source: RunPhase, target: RunPhase, detail: str = "") -> None:
        self.source = source
        self.target = target
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"{ErrorCode.STATE_CONFLICT.value}: transição {source.value} → "
            f"{target.value} não permitida{suffix}"
        )


class TransitionRecord(BaseModel):
    """Registro para `run_events` — tracing e auditoria, não event sourcing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_phase: RunPhase
    to_phase: RunPhase
    reason: str
    error_code: ErrorCode | None = None
    at: datetime


def can_transition(source: RunPhase, target: RunPhase) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


def assert_transition(source: RunPhase, target: RunPhase) -> None:
    if source.is_terminal:
        raise TransitionError(source, target, "estado terminal não reabre")
    if not can_transition(source, target):
        raise TransitionError(source, target)


def phase_for_next_action(next_action: NextAction) -> RunPhase:
    """Alvo a partir de `UPDATING_MEMORY`, dado o veredito do Verifier.

    `RETRY` e `REPLAN` voltam a `PERCEIVING` como `CONTINUE`: o que muda é o
    estado do run (contador de retry, plano invalidado), não a fase. Isso
    mantém um único ponto de entrada de ciclo e evita caminhos paralelos que
    escapariam dos gates de budget e cancelamento.
    """
    return _NEXT_ACTION_TARGET[next_action]


def derive_resume_phase(
    persisted_phase: RunPhase,
    *,
    has_in_flight_attempt: bool,
    unresolved_effect: bool,
    has_unverified_action: bool,
) -> RunPhase:
    """Fase de retomada após restart — correção C08.

    A fase persistida no checkpoint é uma dica, não a verdade: o crash pode
    ter acontecido entre a mutação em memória e o `checkpoint()`. A verdade
    durável são as linhas de `action_attempts` e os ponteiros
    `last_action_id` / `last_verified_action_id`.

    Precedência, do mais conservador ao mais barato:

    1. terminal permanece terminal;
    2. attempt `IN_FLIGHT`, efeito não resolvido, ou fase `EXECUTING` →
       `RECOVERING`. O caso `EXECUTING` é conservador de propósito: nada
       prova que a chamada não saiu, então exige-se probe;
    3. ação executada e não verificada → `VERIFYING`. Verificação é
       read-only e idempotente, logo repeti-la é seguro;
    4. `WAITING_USER` é preservado — precisa sobreviver a restart;
    5. qualquer outra fase reinicia o ciclo em `PERCEIVING`.
    """
    if persisted_phase.is_terminal:
        return persisted_phase
    if has_in_flight_attempt or unresolved_effect or persisted_phase is P.EXECUTING:
        return P.RECOVERING
    if has_unverified_action:
        return P.VERIFYING
    if persisted_phase is P.WAITING_USER:
        return P.WAITING_USER
    return P.PERCEIVING


def resume_phase_for(state) -> RunPhase:
    """`derive_resume_phase` a partir dos sinais que o repositório coletou."""
    return derive_resume_phase(
        state.persisted_phase,
        has_in_flight_attempt=state.has_in_flight_attempt,
        unresolved_effect=state.unresolved_effect,
        has_unverified_action=state.has_unverified_action,
    )


def reachable_phases(start: RunPhase = P.CREATED) -> frozenset[RunPhase]:
    """Fecho transitivo a partir de `start`.

    Usado em teste para provar que não existe estado morto — precisamente o
    defeito de `WAITING_EXTERNAL` e `BLOCKED` na spec original.
    """
    seen: set[RunPhase] = {start}
    frontier: list[RunPhase] = [start]
    while frontier:
        current = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)


@dataclass(slots=True)
class RunStateMachine:
    """Fase corrente de um run mais o histórico de transições do processo.

    Não persiste nada: quem persiste é o checkpoint. O histórico existe para
    alimentar `run_events` e responder "por que o agente fez isso".
    """

    phase: RunPhase = P.CREATED
    history: list[TransitionRecord] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    def can(self, target: RunPhase) -> bool:
        return not self.phase.is_terminal and can_transition(self.phase, target)

    def transition(
        self,
        target: RunPhase,
        *,
        reason: str,
        error_code: ErrorCode | None = None,
        at: datetime | None = None,
    ) -> TransitionRecord:
        assert_transition(self.phase, target)
        record = TransitionRecord(
            from_phase=self.phase,
            to_phase=target,
            reason=reason,
            error_code=error_code,
            at=at or datetime.now(UTC),
        )
        self.phase = target
        self.history.append(record)
        return record

    def request_cancel(
        self, *, reason: str = "cancel_requested", at: datetime | None = None
    ) -> bool:
        """Tenta encerrar o run por cancelamento.

        Devolve `True` se o run foi para `CANCELLED` agora, `False` se o
        pedido precisa ser honrado no topo do próximo ciclo. Nunca levanta:
        pedir cancelamento é sempre legítimo, o que varia é quando surte
        efeito.
        """
        if self.phase.is_terminal:
            return False
        if self.phase not in CANCELLABLE_PHASES:
            return False
        self.transition(P.CANCELLED, reason=reason, error_code=ErrorCode.CANCELLED, at=at)
        return True

    def path(self) -> tuple[RunPhase, ...]:
        """Sequência de fases percorridas, para trace e debug."""
        if not self.history:
            return (self.phase,)
        return (self.history[0].from_phase, *(r.to_phase for r in self.history))


def validate_transition_table(table: Mapping[RunPhase, Iterable[RunPhase]]) -> None:
    """Sanidade estrutural da tabela. Chamada em teste, não em runtime."""
    for phase in RunPhase:
        if phase not in table:
            raise ValueError(f"fase sem entrada na tabela: {phase.value}")
    for source, targets in table.items():
        if source.is_terminal and set(targets):
            raise ValueError(f"estado terminal com saída: {source.value}")
        if source in set(targets):
            raise ValueError(f"auto-transição declarada: {source.value}")
