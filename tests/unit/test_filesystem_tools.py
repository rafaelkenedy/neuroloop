"""Sandbox e adapters de filesystem (spec §14, correção C10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neuroloop.core import ErrorCode
from neuroloop.tools import Sandbox, SandboxViolation, ToolArgumentError, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools
from neuroloop.tools.adapters.filesystem import WRITE, FilesystemToolError


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    (tmp_path / "workspace").mkdir()
    return Sandbox(tmp_path / "workspace")


@pytest.fixture
def registry(sandbox) -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg, sandbox)
    return reg


class TestSandbox:
    def test_caminho_relativo_ancora_na_raiz(self, sandbox):
        assert sandbox.resolve("a.json") == sandbox.root / "a.json"

    def test_traversal_e_bloqueado(self, sandbox):
        """O caminho pode vir de conteúdo não confiável; resolve antes de checar."""
        with pytest.raises(SandboxViolation) as exc:
            sandbox.resolve("../../etc/passwd")
        assert exc.value.error_code is ErrorCode.PERMISSION_DENIED

    def test_traversal_disfarcado_e_bloqueado(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.resolve("sub/../../fora.txt")

    def test_absoluto_fora_da_raiz_e_bloqueado(self, tmp_path, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.resolve(str(tmp_path / "fora.txt"))

    def test_alvo_inexistente_e_permitido_dentro_da_raiz(self, sandbox):
        """Escrita precisa resolver caminho que ainda não existe."""
        assert sandbox.resolve("novo/dir/out.json").name == "out.json"

    def test_contains(self, sandbox):
        assert sandbox.contains("a.json") is True
        assert sandbox.contains("../a.json") is False


class TestAdapters:
    async def test_read_devolve_conteudo(self, registry, sandbox):
        (sandbox.root / "in.json").write_text('{"n": 1}', encoding="utf-8")
        handler = registry.get("filesystem.read").handler
        result = await handler({"path": "in.json"})
        assert json.loads(result["content"]) == {"n": 1}
        assert result["bytes"] == 8

    async def test_read_de_arquivo_ausente(self, registry):
        handler = registry.get("filesystem.read").handler
        with pytest.raises(FilesystemToolError) as exc:
            await handler({"path": "sumiu.json"})
        assert exc.value.error_code is ErrorCode.TOOL_PERMANENT_ERROR

    async def test_write_cria_diretorios(self, registry, sandbox):
        handler = registry.get("filesystem.write").handler
        result = await handler({"path": "sub/dir/out.json", "content": "[]"})
        assert (sandbox.root / "sub/dir/out.json").read_text(encoding="utf-8") == "[]"
        assert result["bytes_written"] == 2

    async def test_write_fora_do_sandbox_e_bloqueado(self, registry, tmp_path):
        handler = registry.get("filesystem.write").handler
        with pytest.raises(SandboxViolation):
            await handler({"path": str(tmp_path / "escapou.json"), "content": "x"})

    async def test_list_ordena(self, registry, sandbox):
        for name in ("c.txt", "a.txt", "b.txt"):
            (sandbox.root / name).write_text("", encoding="utf-8")
        result = await registry.get("filesystem.list").handler({"path": "."})
        assert result["entries"] == ["a.txt", "b.txt", "c.txt"]


class TestContratoDoRegistroInicial:
    def test_tres_tools_registradas(self, registry):
        assert registry.names() == (
            "filesystem.list",
            "filesystem.read",
            "filesystem.write",
        )

    def test_riscos_seguem_a_spec(self, registry):
        assert registry.get("filesystem.read").definition.risk_level.value == "R0"
        assert registry.get("filesystem.write").definition.risk_level.value == "R1"

    def test_write_declara_probe_utilizavel(self, registry):
        definition = registry.get("filesystem.write").definition
        criterion = definition.effect_probe.build(
            {"path": "/workspace/out.json", "content": "[]"}
        )
        assert criterion.path == "/workspace/out.json"

    def test_write_e_idempotente_para_permitir_retry(self, registry):
        definition = registry.get("filesystem.write").definition
        assert definition.supports_idempotency is True
        assert definition.max_retries > 0

    def test_argumentos_sao_validados_pelo_registry(self, registry):
        with pytest.raises(ToolArgumentError):
            registry.validate_arguments("filesystem.write", {"path": "a.json"})

    def test_definicao_do_write_e_reutilizavel(self):
        """A definição é dado; o handler é que precisa de sandbox."""
        assert WRITE.name == "filesystem.write"
        assert Path is not None
