"""Construtores compartilhados pelos testes.

Fica num módulo próprio, e não em `conftest.py`, porque existem dois
conftests (raiz e `integration/`) e ambos seriam importáveis como `conftest`
— a última importação venceria.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from neuroloop.core import (
    CriterionOutcome,
    ExecutionBudget,
    FileExists,
    Goal,
    GoalStatus,
    RunCheckpoint,
    RunPhase,
)

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def make_outcome(
    satisfied: bool | None, kind: str = "FILE_EXISTS", **kwargs
) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_kind=kind, satisfied=satisfied, observed_at=NOW, **kwargs
    )


def make_goal(**overrides) -> Goal:
    defaults = dict(
        id=uuid4(),
        agent_id=uuid4(),
        description="gerar /workspace/eligible.json com clientes ativos",
        success_criteria=(FileExists(path="/workspace/eligible.json"),),
        status=GoalStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return Goal(**defaults)


def make_checkpoint(**overrides) -> RunCheckpoint:
    defaults = dict(
        run_id=uuid4(),
        goal_id=uuid4(),
        phase=RunPhase.PERCEIVING,
        started_at=NOW,
        wall_clock_deadline=NOW + timedelta(seconds=900),
        budget=ExecutionBudget(),
    )
    defaults.update(overrides)
    return RunCheckpoint(**defaults)


def sync_url(url: str) -> str:
    """URL do driver síncrono equivalente.

    A inspeção de schema é síncrona; o runtime é assíncrono. Só o teste
    precisa das duas formas.

    `+psycopg` permanece: no psycopg 3 o mesmo dialeto atende sync e async.
    Removê-lo faria o SQLAlchemy cair no psycopg2, que não está instalado.
    """
    return url.replace("+aiosqlite", "")
