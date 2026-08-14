"""Sandbox de filesystem.

Resolução canônica antes de qualquer checagem — correção C10. Um caminho
vindo de conteúdo não confiável pode conter `..`, symlink ou caminho UNC; a
comparação precisa acontecer depois de resolver, nunca sobre a string crua.
"""

from __future__ import annotations

from pathlib import Path

from neuroloop.core.enums import ErrorCode


class SandboxViolation(RuntimeError):
    error_code = ErrorCode.PERMISSION_DENIED

    def __init__(self, path: str, root: Path) -> None:
        self.path = path
        self.root = root
        super().__init__(
            f"{ErrorCode.PERMISSION_DENIED.value}: {path!r} está fora do sandbox {root}"
        )


class Sandbox:
    """Raiz única de escrita e leitura permitida na V0."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Resolve e prova que o resultado está dentro da raiz.

        `strict=False` porque o alvo pode ainda não existir (é o caso normal
        de uma escrita); a resolução de symlinks dos componentes existentes
        continua acontecendo.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxViolation(path, self.root)
        return resolved

    def contains(self, path: str) -> bool:
        try:
            self.resolve(path)
        except SandboxViolation:
            return False
        return True
