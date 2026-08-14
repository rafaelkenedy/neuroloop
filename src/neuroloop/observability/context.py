"""Identidade de trace e versões de componente — TASK-014 (spec §33).

Dois blocos que a spec exige e que só valem juntos:

**Identidade.** Todo evento carrega `trace_id`, `run_id`, `cycle_id`,
`goal_id`, `iteration`, `phase` e `state_version`. Sem `cycle_id` não dá
para separar duas passadas do mesmo run; sem `state_version` não dá para
saber contra qual estado a decisão foi tomada.

**Versões.** Guardar o que estava em vigor — modelo, template de prompt,
schema de saída, registry de tools, policy, skills — é o que torna uma
decisão passada *reproduzível*. "O agente fez X" sem saber qual registry
estava carregado é uma observação, não uma explicação.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from uuid import UUID, uuid4

from neuroloop.core.enums import RunPhase
from neuroloop.core.identity import canonical_json


def new_trace_id() -> str:
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Bloco de identidade de um ciclo (spec §33)."""

    trace_id: str
    run_id: UUID
    goal_id: UUID
    cycle_id: str
    iteration: int
    phase: RunPhase
    state_version: int

    def with_phase(self, phase: RunPhase) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            run_id=self.run_id,
            goal_id=self.goal_id,
            cycle_id=self.cycle_id,
            iteration=self.iteration,
            phase=phase,
            state_version=self.state_version,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "cycle_id": self.cycle_id,
            "iteration": self.iteration,
            "phase": self.phase.value,
            "state_version": self.state_version,
        }


@dataclass(frozen=True, slots=True)
class ComponentVersions:
    """O que estava em vigor quando a decisão foi tomada.

    Fingerprints em vez de conteúdo: guardar o prompt inteiro em todo evento
    inflaria o trace e arriscaria vazar dado do usuário. O hash responde
    "mudou?", que é a pergunta que importa ao investigar uma regressão.
    """

    model: str | None = None
    prompt_template: str | None = None
    output_schema: str | None = None
    tool_registry: str | None = None
    policy: str | None = None
    skills: str | None = None
    pricing: str | None = None

    def as_payload(self) -> dict[str, object]:
        # `slots=True` remove `__dict__`; `fields` é a forma correta de
        # enumerar um dataclass com slots.
        return {
            f"v_{f.name}": getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }


def fingerprint(value: object) -> str:
    """Hash curto e estável de qualquer estrutura serializável."""
    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def registry_fingerprint(names_and_versions: dict[str, str]) -> str:
    return fingerprint(sorted(names_and_versions.items()))


@dataclass(slots=True)
class CycleTrace:
    """Contexto vivo de um ciclo, com as versões já resolvidas."""

    context: TraceContext
    versions: ComponentVersions = field(default_factory=ComponentVersions)

    def payload(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        return {**self.context.as_payload(), **self.versions.as_payload(), **(extra or {})}
