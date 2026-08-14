"""Catálogo tipado e versionado de ferramentas — TASK-004.

O registry é a fronteira entre "o que o agente pode querer fazer" e "o que o
sistema sabe fazer". Ele valida no **registro** (contrato da tool) e na
**chamada** (argumentos contra o JSON Schema), de modo que nenhum argumento
inventado pelo LLM chegue a um adapter.

O registry não autoriza nada: risco e permissões são do PolicyEngine
(TASK-005). Aqui só existe o catálogo e a validação estrutural.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from neuroloop.core.enums import ErrorCode
from neuroloop.tools.definitions import ToolDefinition, ToolSummary


class ToolError(RuntimeError):
    error_code: ErrorCode


class ToolNotFoundError(ToolError):
    error_code = ErrorCode.TOOL_SELECTION_ERROR

    def __init__(self, name: str, version: str | None = None) -> None:
        alvo = f"{name}@{version}" if version else name
        super().__init__(f"{ErrorCode.TOOL_SELECTION_ERROR.value}: tool {alvo} não registrada")


class DuplicateToolError(ToolError):
    error_code = ErrorCode.TOOL_SELECTION_ERROR

    def __init__(self, name: str, version: str) -> None:
        super().__init__(f"tool {name}@{version} já registrada")


class ToolArgumentError(ToolError):
    """Argumentos não batem com o `input_schema` declarado."""

    error_code = ErrorCode.TOOL_VALIDATION_ERROR

    def __init__(self, name: str, detail: str) -> None:
        self.detail = detail
        super().__init__(
            f"{ErrorCode.TOOL_VALIDATION_ERROR.value}: argumentos inválidos para "
            f"{name}: {detail}"
        )


@runtime_checkable
class ToolHandler(Protocol):
    """Execução crua. Não conhece run, goal, política nem verificação."""

    async def __call__(self, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}

    # ------------------------------------------------------------- registro

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> RegisteredTool:
        if definition.key in self._tools:
            raise DuplicateToolError(*definition.key)
        Draft202012Validator.check_schema(definition.input_schema)
        entry = RegisteredTool(definition=definition, handler=handler)
        self._tools[definition.key] = entry
        return entry

    # -------------------------------------------------------------- consulta

    def get(self, name: str, version: str | None = None) -> RegisteredTool:
        if version is not None:
            entry = self._tools.get((name, version))
            if entry is None:
                raise ToolNotFoundError(name, version)
            return entry

        candidates = [e for (n, _), e in self._tools.items() if n == name]
        if not candidates:
            raise ToolNotFoundError(name)
        return max(candidates, key=lambda e: e.definition.version_tuple)

    def has(self, name: str, version: str | None = None) -> bool:
        try:
            self.get(name, version)
        except ToolNotFoundError:
            return False
        return True

    def versions(self, name: str) -> tuple[str, ...]:
        found = [e.definition for (n, _), e in self._tools.items() if n == name]
        return tuple(d.version for d in sorted(found, key=lambda d: d.version_tuple))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted({n for n, _ in self._tools}))

    def summaries(self) -> tuple[ToolSummary, ...]:
        """Catálogo para o `WorkingContext`: última versão de cada tool."""
        return tuple(
            self.get(name).definition.summary() for name in self.names()
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())

    # ------------------------------------------------------------- validação

    def validate_arguments(
        self, name: str, arguments: dict[str, Any], *, version: str | None = None
    ) -> None:
        """Barra argumentos malformados antes de qualquer adapter.

        Levanta `ToolArgumentError`, que mapeia para `TOOL_VALIDATION_ERROR`
        — falha de proposta, não de execução: nada foi tentado no mundo.
        """
        definition = self.get(name, version).definition
        validator = Draft202012Validator(definition.input_schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            raise ToolArgumentError(name, _describe(errors[0]))


def _describe(error: JsonSchemaValidationError) -> str:
    location = "/".join(str(p) for p in error.path)
    return f"{location or '<raiz>'}: {error.message}"
