"""Fixtures globais. Construtores ficam em `factories.py`.

As fixtures de banco vivem aqui, e não em `integration/conftest.py`, porque
os benchmarks também precisam delas.

Por padrão a suíte roda contra um SQLite efêmero, o que a mantém executável
sem servidor. Para rodar exatamente os mesmos testes contra o alvo real:

    docker compose up -d postgres
    NEUROLOOP_TEST_DATABASE_URL=postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop \
        python -m pytest
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from factories import NOW
from neuroloop.persistence import (
    build_engine,
    build_session_factory,
    configure_event_loop,
    create_all,
    drop_all,
)

# psycopg async exige SelectorEventLoop no Windows; sem isto a suíte contra
# PostgreSQL falha na conexão, antes de qualquer teste.
configure_event_loop()


def database_url_for_tests(tmp_path) -> str:
    configured = os.environ.get("NEUROLOOP_TEST_DATABASE_URL")
    if configured:
        return configured
    return f"sqlite+aiosqlite:///{tmp_path / 'neuroloop-test.db'}"


@pytest.fixture
def is_postgres() -> bool:
    return "postgresql" in os.environ.get("NEUROLOOP_TEST_DATABASE_URL", "")


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    eng = build_engine(database_url_for_tests(tmp_path))
    await drop_all(eng)
    await create_all(eng)
    try:
        yield eng
    finally:
        await drop_all(eng)
        await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = build_session_factory(engine)
    async with factory() as s:
        yield s
        await s.commit()


@pytest.fixture
def session_factory(engine: AsyncEngine):
    """Para testes que precisam de duas conexões simultâneas."""
    return build_session_factory(engine)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def action_id() -> UUID:
    return uuid4()
