"""Spans do ciclo — TASK-014 (spec §33).

A árvore de spans da spec:

    agent.cycle
    ├── perception.collect
    ├── memory.retrieve
    ├── workspace.build
    ├── controller.decide
    │   └── llm.call
    ├── action.authorize
    ├── tool.execute
    ├── verifier.evaluate
    └── memory.store

`Tracer` é protocolo. O padrão grava em `run_events` — o mesmo lugar da
auditoria, para que "o que aconteceu" e "quanto demorou" não vivam em
sistemas separados. Um adapter OpenTelemetry entra sem tocar no runtime:
basta outra implementação do protocolo.

Todo payload passa por `redact` antes de sair daqui. Redigir na fronteira
de saída, e não em cada call site, é o que evita que um campo novo vaze por
esquecimento.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neuroloop.core.enums import ErrorCode
from neuroloop.observability.context import CycleTrace
from neuroloop.observability.redaction import redact
from neuroloop.persistence.repositories import RunEventRepository

SPAN_CYCLE = "agent.cycle"
SPAN_PERCEPTION = "perception.collect"
SPAN_MEMORY_RETRIEVE = "memory.retrieve"
SPAN_WORKSPACE = "workspace.build"
SPAN_DECIDE = "controller.decide"
SPAN_LLM = "llm.call"
SPAN_AUTHORIZE = "action.authorize"
SPAN_EXECUTE = "tool.execute"
SPAN_VERIFY = "verifier.evaluate"
SPAN_MEMORY_STORE = "memory.store"


@dataclass(slots=True)
class SpanRecord:
    name: str
    duration_ms: int
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: ErrorCode | None = None


@runtime_checkable
class Tracer(Protocol):
    async def record(self, trace: CycleTrace, span: SpanRecord) -> None: ...


@dataclass(slots=True)
class NullTracer:
    """Descarta. Útil em teste de unidade e quando o trace é ruído."""

    async def record(self, trace: CycleTrace, span: SpanRecord) -> None:
        return None


@dataclass(slots=True)
class InMemoryTracer:
    """Coleta em memória — o que os testes inspecionam."""

    spans: list[tuple[CycleTrace, SpanRecord]] = field(default_factory=list)

    async def record(self, trace: CycleTrace, span: SpanRecord) -> None:
        self.spans.append((trace, span))

    def names(self) -> list[str]:
        return [span.name for _, span in self.spans]

    def by_name(self, name: str) -> list[SpanRecord]:
        return [span for _, span in self.spans if span.name == name]


@dataclass(slots=True)
class RunEventTracer:
    """Grava spans como `run_events`, junto da auditoria."""

    session_factory: async_sessionmaker[AsyncSession]

    async def record(self, trace: CycleTrace, span: SpanRecord) -> None:
        async with self.session_factory() as session:
            await RunEventRepository(session).append(
                run_id=trace.context.run_id,
                iteration=trace.context.iteration,
                kind=f"SPAN:{span.name}",
                reason=None if span.ok else "SPAN_FAILED",
                error_code=span.error_code,
                trace_id=trace.context.trace_id,
                payload=redact(
                    trace.payload({"duration_ms": span.duration_ms, "ok": span.ok, **span.payload})
                ),
            )
            await session.commit()


@asynccontextmanager
async def span(
    tracer: Tracer,
    trace: CycleTrace,
    name: str,
    **payload: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Mede e registra. O bloco pode enriquecer o payload durante a execução.

    A falha é registrada e **re-levantada**: um span que engole exceção
    transforma observabilidade em mecanismo de perda de erro.
    """
    started = time.perf_counter()
    extra: dict[str, Any] = dict(payload)
    try:
        yield extra
    except Exception as error:  # noqa: BLE001 - registra e repropaga
        await tracer.record(
            trace,
            SpanRecord(
                name=name,
                duration_ms=_elapsed_ms(started),
                ok=False,
                payload={**extra, "error": type(error).__name__},
                error_code=getattr(error, "error_code", None),
            ),
        )
        raise
    else:
        await tracer.record(
            trace,
            SpanRecord(
                name=name, duration_ms=_elapsed_ms(started), ok=True, payload=extra
            ),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
