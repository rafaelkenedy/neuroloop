"""Erros de persistência mapeados na taxonomia de falhas."""

from __future__ import annotations

from uuid import UUID

from neuroloop.core.enums import ErrorCode


class PersistenceError(RuntimeError):
    error_code: ErrorCode


class StateConflictError(PersistenceError):
    """Optimistic lock perdido: outro escritor avançou `state_version`."""

    error_code = ErrorCode.STATE_CONFLICT

    def __init__(self, run_id: UUID, expected: int, actual: int | None) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{ErrorCode.STATE_CONFLICT.value}: run {run_id} esperava state_version="
            f"{expected}, encontrou {actual}"
        )


class LeaseLostError(PersistenceError):
    """Fencing token desatualizado — correção C11.

    O caso que isso protege: processo pausado longo o bastante para a lease
    expirar, outro runner assume, e o primeiro volta tentando escrever. Sem
    fencing ele sobrescreveria o trabalho do sucessor.
    """

    error_code = ErrorCode.LEASE_LOST

    def __init__(self, run_id: UUID, epoch: int, actual: int | None) -> None:
        self.run_id = run_id
        self.epoch = epoch
        self.actual = actual
        super().__init__(
            f"{ErrorCode.LEASE_LOST.value}: run {run_id} escrevia com epoch={epoch}, "
            f"dono atual está em {actual}"
        )


class LeaseUnavailableError(PersistenceError):
    """Outro runner detém a lease e ela ainda não expirou."""

    error_code = ErrorCode.STATE_CONFLICT

    def __init__(self, run_id: UUID, owner: str | None) -> None:
        self.run_id = run_id
        self.owner = owner
        super().__init__(f"run {run_id} já está sendo executado por {owner!r}")


class RunNotFoundError(PersistenceError):
    error_code = ErrorCode.STATE_CONFLICT

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"run {run_id} não encontrado")
