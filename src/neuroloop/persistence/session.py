"""Engine e sessão assíncronas.

O runtime é async (`async def run_until_pause`), então a persistência também
é — evita bloquear o loop e evita uma migração dolorosa depois.

Regra da spec §7 que este módulo existe para tornar praticável: **nunca
manter transação aberta durante chamada externa**. Por isso as sessões são
curtas e criadas por operação, não por ciclo.
"""

from __future__ import annotations

import asyncio
import os
import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from neuroloop.persistence.models import Base

DEFAULT_DATABASE_URL = "postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop"


def configure_event_loop() -> None:
    """Habilita psycopg async no Windows.

    O event loop padrão do Windows é o `ProactorEventLoop`, e o psycopg
    assíncrono não roda sobre ele — a conexão falha com `InterfaceError`
    antes de qualquer query. Precisa ser chamado pelos **pontos de entrada**
    (migrations, servidor, suíte de testes), nunca no import: mexer na
    política de event loop como efeito colateral de importar um módulo é o
    tipo de coisa que quebra o processo de quem só queria os schemas.

    No-op fora do Windows.

    A partir do Python 3.14 a API de event loop policy (`get_event_loop_policy`,
    `set_event_loop_policy`, `WindowsSelectorEventLoopPolicy`) está deprecada,
    com remoção marcada para o 3.16. Continua sendo o único jeito de fazer o
    `asyncio.new_event_loop()` do Windows devolver um `SelectorEventLoop`, então
    seguimos usando-a — mas suprimimos o `DeprecationWarning` local, senão a
    suíte (que roda com `filterwarnings = ["error"]`) não coleta sob 3.14.
    Quando a policy for removida, este bloco vira no-op e o loop-factory
    explícito em `select_selector_loop` assume.
    """
    if sys.platform != "win32":
        return
    with warnings.catch_warnings():
        # Só acessar `asyncio.WindowsSelectorEventLoopPolicy` já dispara o
        # warning via `asyncio.__getattr__`, então o filtro tem que cobrir o
        # getattr também, não só a chamada.
        warnings.simplefilter("ignore", DeprecationWarning)
        politica = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if politica is None:
            return
        if not isinstance(asyncio.get_event_loop_policy(), politica):
            asyncio.set_event_loop_policy(politica())


def select_selector_loop() -> asyncio.AbstractEventLoop:
    """Cria um event loop compatível com o psycopg async.

    Forma não deprecada de obter o loop certo, para pontos de entrada que
    controlam o próprio loop (migrations, um futuro servidor): passe como
    `asyncio.run(coro, loop_factory=select_selector_loop)`. No Windows força
    o `SelectorEventLoop`; nos demais sistemas é o loop padrão.
    """
    if sys.platform == "win32":
        selector = getattr(asyncio, "SelectorEventLoop", None)
        if selector is not None:
            return selector()
    return asyncio.new_event_loop()


def database_url() -> str:
    return os.environ.get("NEUROLOOP_DATABASE_URL", DEFAULT_DATABASE_URL)


def build_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(url or database_url(), echo=echo, future=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transação curta: commit no sucesso, rollback em qualquer exceção."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine: AsyncEngine) -> None:
    """Cria o schema direto do metadata.

    Atalho para teste e desenvolvimento. O caminho de produção é Alembic —
    ver `migrations/`.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
