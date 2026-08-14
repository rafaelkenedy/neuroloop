"""Plan / PlanStep — horizonte curto e validável (spec §13).

O validador estrutural vive no schema porque a spec exige rejeitar plano com
DAG cíclico, tool inexistente, expected outcomes vazio ou steps demais. A
checagem de existência de tool depende do ToolRegistry e fica no
`PlannerValidator` (TASK-011); tudo que é verificável sem I/O está aqui.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.criteria import Criterion
from neuroloop.core.enums import ErrorCode, PlanStepStatus, RiskLevel

MAX_PLAN_STEPS = 5
"""Spec §13. Horizonte curto é decisão arquitetural, não limitação técnica."""


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    preferred_tool: str | None = None
    arguments: dict[str, Any] | None = None
    preconditions: tuple[Criterion, ...] = ()
    expected_outcomes: tuple[Criterion, ...] = Field(min_length=1)
    risk_hint: RiskLevel = RiskLevel.R0
    status: PlanStepStatus = PlanStepStatus.PENDING

    @property
    def is_materialized(self) -> bool:
        """Step pronto para Fast Path sem nova chamada de LLM (correção C13)."""
        return self.preferred_tool is not None and self.arguments is not None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    version: int = Field(ge=1)
    objective: str = Field(min_length=1)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    assumptions: tuple[str, ...] = ()
    completion_condition: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_dag(self) -> Plan:
        ids = [s.id for s in self.steps]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(
                f"{ErrorCode.INVALID_PLAN.value}: ids de step duplicados: {sorted(duplicates)}"
            )

        known = set(ids)
        for step in self.steps:
            unknown = set(step.dependencies) - known
            if unknown:
                raise ValueError(
                    f"{ErrorCode.INVALID_PLAN.value}: step {step.id!r} depende de "
                    f"step inexistente: {sorted(unknown)}"
                )
            if step.id in step.dependencies:
                raise ValueError(
                    f"{ErrorCode.INVALID_PLAN.value}: step {step.id!r} depende de si mesmo"
                )

        cycle = _find_cycle({s.id: set(s.dependencies) for s in self.steps})
        if cycle is not None:
            raise ValueError(f"{ErrorCode.INVALID_PLAN.value}: ciclo no DAG: {cycle}")
        return self

    def topological_order(self) -> tuple[str, ...]:
        """Ordem de execução respeitando dependências, estável por declaração."""
        pending = {s.id: set(s.dependencies) for s in self.steps}
        declared = [s.id for s in self.steps]
        ordered: list[str] = []
        while pending:
            ready = [sid for sid in declared if sid in pending and not pending[sid]]
            if not ready:  # pragma: no cover - impedido pelo validador de ciclo
                raise ValueError(f"{ErrorCode.INVALID_PLAN.value}: ciclo no DAG")
            for sid in ready:
                ordered.append(sid)
                del pending[sid]
            for deps in pending.values():
                deps.difference_update(ready)
        return tuple(ordered)


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """DFS com marcação tricolor; devolve um ciclo concreto para o erro."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for dep in sorted(graph[node]):
            if color[dep] == GREY:
                return stack[stack.index(dep) :] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None
