"""Camada de persistência — PostgreSQL via SQLAlchemy 2.x async."""

from neuroloop.persistence.errors import (
    LeaseLostError,
    LeaseUnavailableError,
    PersistenceError,
    RunNotFoundError,
    StateConflictError,
)
from neuroloop.persistence.models import Base
from neuroloop.persistence.session import (
    build_engine,
    build_session_factory,
    configure_event_loop,
    create_all,
    database_url,
    drop_all,
    session_scope,
)

__all__ = [
    "Base",
    "LeaseLostError",
    "LeaseUnavailableError",
    "PersistenceError",
    "RunNotFoundError",
    "StateConflictError",
    "build_engine",
    "build_session_factory",
    "configure_event_loop",
    "create_all",
    "database_url",
    "drop_all",
    "session_scope",
]
