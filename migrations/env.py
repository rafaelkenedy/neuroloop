"""Ambiente do Alembic.

A URL vem de `NEUROLOOP_DATABASE_URL` para que o mesmo conjunto de migrations
rode contra o Postgres do compose e contra o banco efêmero da suíte de
testes.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from neuroloop.persistence.models import Base
from neuroloop.persistence.session import database_url, select_selector_loop

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # loop_factory (3.12+) em vez de mexer na event loop policy global: o
    # psycopg async exige SelectorEventLoop no Windows.
    asyncio.run(run_migrations_online(), loop_factory=select_selector_loop)
