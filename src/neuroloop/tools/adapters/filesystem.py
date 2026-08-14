"""Adapters de filesystem, restritos ao sandbox.

Registry inicial da spec §14: `filesystem.list` e `filesystem.read` são R0;
`filesystem.write` é R1 e só dentro do sandbox.
"""

from __future__ import annotations

from typing import Any

from neuroloop.core.criteria import FileExists
from neuroloop.core.enums import ErrorCode, RiskLevel
from neuroloop.tools.definitions import EffectProbe, ToolDefinition
from neuroloop.tools.registry import ToolRegistry
from neuroloop.tools.sandbox import Sandbox

_PATH_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}


class FilesystemToolError(RuntimeError):
    def __init__(self, error_code: ErrorCode, detail: str) -> None:
        self.error_code = error_code
        super().__init__(f"{error_code.value}: {detail}")


LIST = ToolDefinition(
    name="filesystem.list",
    version="1.0.0",
    description="Lista entradas de um diretório dentro do sandbox.",
    input_schema=_PATH_SCHEMA,
    risk_level=RiskLevel.R0,
    side_effects=False,
    timeout_seconds=5.0,
    max_retries=2,
    capabilities=frozenset({"fs:read"}),
    returns_external_content=True,
)

READ = ToolDefinition(
    name="filesystem.read",
    version="1.0.0",
    description="Lê o conteúdo de um arquivo de texto dentro do sandbox.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    risk_level=RiskLevel.R0,
    side_effects=False,
    timeout_seconds=5.0,
    max_retries=2,
    capabilities=frozenset({"fs:read"}),
    returns_external_content=True,
)

WRITE = ToolDefinition(
    name="filesystem.write",
    version="1.0.0",
    description="Escreve um arquivo de texto dentro do sandbox.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    risk_level=RiskLevel.R1,
    side_effects=True,
    reversible=False,
    supports_idempotency=True,
    timeout_seconds=10.0,
    max_retries=2,
    capabilities=frozenset({"fs:write"}),
    # O probe responde "o efeito saiu?", não "o conteúdo está correto?".
    # Correção de conteúdo é `expected_outcomes`, avaliado pelo Verifier.
    effect_probe=EffectProbe(
        criterion_template=FileExists(path="{path}"),
        argument_bindings={"path": "path"},
    ),
)


def register_filesystem_tools(registry: ToolRegistry, sandbox: Sandbox) -> None:
    """Registra as três tools ligadas a um sandbox concreto."""

    async def _list(arguments: dict[str, Any]) -> Any:
        target = sandbox.resolve(arguments["path"])
        if not target.is_dir():
            raise FilesystemToolError(
                ErrorCode.TOOL_PERMANENT_ERROR, f"{target} não é um diretório"
            )
        return {"entries": sorted(p.name for p in target.iterdir())}

    async def _read(arguments: dict[str, Any]) -> Any:
        target = sandbox.resolve(arguments["path"])
        if not target.is_file():
            raise FilesystemToolError(
                ErrorCode.TOOL_PERMANENT_ERROR, f"{target} não existe ou não é arquivo"
            )
        content = target.read_text(encoding=arguments.get("encoding", "utf-8"))
        return {"content": content, "bytes": len(content.encode("utf-8"))}

    async def _write(arguments: dict[str, Any]) -> Any:
        target = sandbox.resolve(arguments["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        content = arguments["content"]
        target.write_text(content, encoding=arguments.get("encoding", "utf-8"))
        return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}

    registry.register(LIST, _list)
    registry.register(READ, _read)
    registry.register(WRITE, _write)
