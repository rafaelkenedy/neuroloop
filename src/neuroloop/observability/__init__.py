"""Observabilidade: identidade de trace, spans, redação, explicação e métricas."""

from neuroloop.observability.context import (
    ComponentVersions,
    CycleTrace,
    TraceContext,
    fingerprint,
    new_trace_id,
    registry_fingerprint,
)
from neuroloop.observability.explain import (
    ActionExplanation,
    AttemptSummary,
    RunTimelineEntry,
    explain_action,
    explain_run,
    run_timeline,
)
from neuroloop.observability.metrics import (
    MIN_DENOMINATOR,
    RunMetrics,
    collect_run_metrics,
    rate,
)
from neuroloop.observability.redaction import REDACTED, redact
from neuroloop.observability.tracing import (
    InMemoryTracer,
    NullTracer,
    RunEventTracer,
    SpanRecord,
    Tracer,
    span,
)

__all__ = [
    "MIN_DENOMINATOR",
    "REDACTED",
    "ActionExplanation",
    "AttemptSummary",
    "ComponentVersions",
    "CycleTrace",
    "InMemoryTracer",
    "NullTracer",
    "RunEventTracer",
    "RunMetrics",
    "RunTimelineEntry",
    "SpanRecord",
    "TraceContext",
    "Tracer",
    "collect_run_metrics",
    "explain_action",
    "explain_run",
    "fingerprint",
    "new_trace_id",
    "rate",
    "redact",
    "registry_fingerprint",
    "run_timeline",
    "span",
]
