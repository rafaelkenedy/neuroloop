"""TASK-004. Aceite: schema/risk/capabilities obrigatórios; C05: probe obrigatório."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroloop.core import ErrorCode, FileExists, JsonPathEquals, RiskLevel
from neuroloop.tools import (
    DuplicateToolError,
    EffectProbe,
    ToolArgumentError,
    ToolDefinition,
    ToolDefinitionError,
    ToolNotFoundError,
    ToolRegistry,
)

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}

WRITE_PROBE = EffectProbe(
    criterion_template=FileExists(path="{path}"),
    argument_bindings={"path": "path"},
)


def tool(**overrides) -> ToolDefinition:
    defaults = dict(
        name="filesystem.read",
        version="1.0.0",
        description="lê arquivo",
        input_schema=OBJECT_SCHEMA,
        risk_level=RiskLevel.R0,
        side_effects=False,
        timeout_seconds=5.0,
        capabilities=frozenset({"fs:read"}),
    )
    defaults.update(overrides)
    return ToolDefinition(**defaults)


async def _noop(arguments):
    return arguments


class TestContratoDaDefinicao:
    def test_definicao_minima_valida(self):
        assert tool().key == ("filesystem.read", "1.0.0")

    def test_capabilities_obrigatorias(self):
        with pytest.raises(ValidationError):
            tool(capabilities=frozenset())

    def test_versao_precisa_ser_semver(self):
        with pytest.raises(ValidationError):
            tool(version="latest")

    def test_input_schema_precisa_ser_objeto(self):
        with pytest.raises(ValidationError, match="input_schema"):
            tool(input_schema={"type": "array"})

    def test_timeout_precisa_ser_positivo(self):
        with pytest.raises(ValidationError):
            tool(timeout_seconds=0)

    def test_definicao_e_imutavel(self):
        with pytest.raises(ValidationError):
            tool().risk_level = RiskLevel.R3


class TestProbeObrigatorio:
    """Correção C05: sem probe, efeito ambíguo só termina em WAITING_USER."""

    def test_side_effect_sem_probe_e_rejeitado(self):
        with pytest.raises(ValidationError, match="effect_probe"):
            tool(
                name="filesystem.write",
                risk_level=RiskLevel.R1,
                side_effects=True,
                reversible=False,
            )

    def test_side_effect_com_probe_e_aceito(self):
        definition = tool(
            name="filesystem.write",
            risk_level=RiskLevel.R1,
            side_effects=True,
            reversible=False,
            effect_probe=WRITE_PROBE,
        )
        assert definition.effect_probe is not None

    def test_tool_sem_efeito_nao_declara_probe(self):
        with pytest.raises(ValidationError, match="não precisa de probe"):
            tool(effect_probe=WRITE_PROBE)

    def test_probe_precisa_observar_o_mundo(self):
        """Perguntar ao resultado da própria ação não prova nada sobre o efeito."""
        with pytest.raises(ValidationError, match="EXTERNAL_STATE"):
            EffectProbe(
                criterion_template=JsonPathEquals(
                    source="ACTION_RESULT", json_path="$.ok", expected=True
                )
            )


class TestCoerenciaDeRisco:
    def test_r0_nao_pode_ter_efeito(self):
        with pytest.raises(ValidationError, match="R0 é leitura"):
            tool(risk_level=RiskLevel.R0, side_effects=True, effect_probe=WRITE_PROBE)

    def test_r2_exige_confirmacao(self):
        with pytest.raises(ValidationError, match="requires_confirmation"):
            tool(
                name="http.request",
                risk_level=RiskLevel.R2,
                side_effects=True,
                effect_probe=WRITE_PROBE,
                supports_idempotency=True,
            )

    def test_r2_com_confirmacao_e_aceito(self):
        definition = tool(
            name="http.request",
            risk_level=RiskLevel.R2,
            side_effects=True,
            requires_confirmation=True,
            supports_idempotency=True,
            effect_probe=WRITE_PROBE,
        )
        assert definition.requires_confirmation is True

    def test_retry_exige_idempotencia_quando_ha_efeito(self):
        """Retry cego em efeito não idempotente é como se duplica efeito."""
        with pytest.raises(ValidationError, match="idempot"):
            tool(
                name="http.request",
                risk_level=RiskLevel.R2,
                side_effects=True,
                requires_confirmation=True,
                supports_idempotency=False,
                max_retries=2,
                effect_probe=WRITE_PROBE,
            )

    def test_ordenacao_de_risco(self):
        assert RiskLevel.R3 > RiskLevel.R2 > RiskLevel.R1 > RiskLevel.R0
        assert RiskLevel.R1 <= RiskLevel.R1


class TestMaterializacaoDoProbe:
    def test_placeholder_vira_argumento_real(self):
        criterion = WRITE_PROBE.build({"path": "/workspace/out.json", "content": "x"})
        assert isinstance(criterion, FileExists)
        assert criterion.path == "/workspace/out.json"

    def test_argumento_ausente_e_erro(self):
        with pytest.raises(ToolDefinitionError, match="ausente"):
            WRITE_PROBE.build({"content": "x"})

    def test_template_sem_binding_passa_intacto(self):
        probe = EffectProbe(criterion_template=FileExists(path="/fixo.json"))
        assert probe.build({}).path == "/fixo.json"


class TestRegistry:
    def test_registro_e_busca(self):
        registry = ToolRegistry()
        registry.register(tool(), _noop)
        assert len(registry) == 1
        assert registry.get("filesystem.read").definition.version == "1.0.0"
        assert registry.has("filesystem.read")

    def test_duplicata_e_rejeitada(self):
        registry = ToolRegistry()
        registry.register(tool(), _noop)
        with pytest.raises(DuplicateToolError):
            registry.register(tool(), _noop)

    def test_tool_desconhecida(self):
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError) as exc:
            registry.get("nao.existe")
        assert exc.value.error_code is ErrorCode.TOOL_SELECTION_ERROR

    def test_versao_desconhecida_e_distinta_de_tool_desconhecida(self):
        registry = ToolRegistry()
        registry.register(tool(), _noop)
        with pytest.raises(ToolNotFoundError, match="2.0.0"):
            registry.get("filesystem.read", "2.0.0")

    def test_sem_versao_devolve_a_mais_recente(self):
        registry = ToolRegistry()
        registry.register(tool(version="1.0.0"), _noop)
        registry.register(tool(version="1.10.0"), _noop)
        registry.register(tool(version="1.9.0"), _noop)
        # comparação semântica, não lexicográfica
        assert registry.get("filesystem.read").definition.version == "1.10.0"
        assert registry.versions("filesystem.read") == ("1.0.0", "1.9.0", "1.10.0")

    def test_versao_explicita_e_respeitada(self):
        registry = ToolRegistry()
        registry.register(tool(version="1.0.0"), _noop)
        registry.register(tool(version="2.0.0"), _noop)
        assert registry.get("filesystem.read", "1.0.0").definition.version == "1.0.0"

    def test_summaries_nao_vazam_o_catalogo_inteiro(self):
        """O prompt recebe projeção, não a definição completa."""
        registry = ToolRegistry()
        registry.register(tool(), _noop)
        summary = registry.summaries()[0]
        assert summary.name == "filesystem.read"
        assert not hasattr(summary, "effect_probe")
        assert not hasattr(summary, "capabilities")


class TestValidacaoDeArgumentos:
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(tool(), _noop)
        return registry

    def test_argumentos_validos_passam(self):
        self._registry().validate_arguments("filesystem.read", {"path": "/a.json"})

    def test_campo_obrigatorio_ausente(self):
        with pytest.raises(ToolArgumentError) as exc:
            self._registry().validate_arguments("filesystem.read", {})
        assert exc.value.error_code is ErrorCode.TOOL_VALIDATION_ERROR

    def test_tipo_errado(self):
        with pytest.raises(ToolArgumentError, match="path"):
            self._registry().validate_arguments("filesystem.read", {"path": 42})

    def test_campo_inventado_pelo_llm_e_barrado(self):
        with pytest.raises(ToolArgumentError):
            self._registry().validate_arguments(
                "filesystem.read", {"path": "/a", "sudo": True}
            )
