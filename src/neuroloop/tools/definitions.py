"""Tipos do Tool Registry — TASK-004 (spec §14, correção C05).

A adição estrutural em relação à spec é `effect_probe`: toda tool com efeito
colateral precisa declarar **como observar se o efeito ocorreu**. Sem isso o
fluxo `timeout → UNKNOWN_EFFECT → probe` da §3 não tem implementação, e a
única saída de um efeito ambíguo seria travar o run em `WAITING_USER`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from neuroloop.core.criteria import Criterion, effective_observes
from neuroloop.core.enums import ErrorCode, ExecutionStatus, RiskLevel
from neuroloop.core.templating import substitute

_criterion_adapter: TypeAdapter[Criterion] = TypeAdapter(Criterion)


class ToolDefinitionError(ValueError):
    """Definição de tool inconsistente. Falha no registro, não em execução."""


class EffectProbe(BaseModel):
    """Como perguntar ao mundo externo se o efeito da ação existe.

    O template é um `Criterion` com placeholders `{nome}`; `argument_bindings`
    liga cada placeholder a uma chave dos argumentos da ação. Isso mantém o
    probe declarativo e versionado junto da tool, em vez de espalhado em
    código do executor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_template: Criterion
    argument_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _probe_must_observe_the_world(self) -> EffectProbe:
        if effective_observes(self.criterion_template) != "EXTERNAL_STATE":
            raise ToolDefinitionError(
                "effect_probe precisa observar EXTERNAL_STATE; perguntar ao "
                "resultado da própria ação não prova nada sobre o efeito"
            )
        return self

    def build(self, arguments: dict[str, Any]) -> Criterion:
        """Materializa o critério de probe para uma ação concreta."""
        values: dict[str, Any] = {}
        for placeholder, argument_key in self.argument_bindings.items():
            if argument_key not in arguments:
                raise ToolDefinitionError(
                    f"probe exige o argumento {argument_key!r}, ausente na ação"
                )
            values[placeholder] = arguments[argument_key]
        payload = substitute(self.criterion_template.model_dump(mode="json"), values)
        return _criterion_adapter.validate_python(payload)


class ToolDefinition(BaseModel):
    """Contrato de uma ferramenta. Imutável e versionado."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1)

    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None

    risk_level: RiskLevel
    side_effects: bool
    reversible: bool = True
    supports_idempotency: bool = False
    requires_confirmation: bool = False

    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(default=0, ge=0)

    capabilities: frozenset[str] = Field(min_length=1)
    allowed_resources: tuple[str, ...] = ()
    effect_probe: EffectProbe | None = None

    returns_external_content: bool = False
    """O resultado carrega conteúdo de fora do agente (arquivo, corpo HTTP)?

    Declarado pela tool, não adivinhado pela percepção: se o conteúdo veio do
    mundo, ele é `UNTRUSTED_EXTERNAL` e não empresta autoridade (C10). Deixar
    isso a cargo de heurística na percepção é como se perde a fronteira."""

    @model_validator(mode="after")
    def _enforce_contract(self) -> ToolDefinition:
        if self.input_schema.get("type") != "object":
            raise ToolDefinitionError(
                f"tool {self.name!r}: input_schema precisa ser um objeto JSON Schema"
            )

        # Correção C05. Sem probe não existe recuperação de efeito ambíguo.
        if self.side_effects and self.effect_probe is None:
            raise ToolDefinitionError(
                f"tool {self.name!r}: side_effects=True exige effect_probe; "
                "senão UNKNOWN_EFFECT só pode terminar em WAITING_USER"
            )
        if not self.side_effects and self.effect_probe is not None:
            raise ToolDefinitionError(
                f"tool {self.name!r}: tool sem efeito não precisa de probe"
            )

        # R0 é leitura por definição (spec §22).
        if self.risk_level is RiskLevel.R0 and self.side_effects:
            raise ToolDefinitionError(
                f"tool {self.name!r}: R0 é leitura; efeito colateral exige R1+"
            )
        if not self.side_effects and not self.reversible:
            raise ToolDefinitionError(
                f"tool {self.name!r}: tool sem efeito é trivialmente reversível"
            )

        # V0: R2 exige aprovação e R3 é bloqueado (spec §22). Declarar o
        # contrário na definição criaria divergência silenciosa com a policy.
        if self.risk_level >= RiskLevel.R2 and not self.requires_confirmation:
            raise ToolDefinitionError(
                f"tool {self.name!r}: risco {self.risk_level.value} exige "
                "requires_confirmation=True"
            )

        # Retry cego em efeito não idempotente é como se duplica efeito.
        if self.max_retries > 0 and self.side_effects and not self.supports_idempotency:
            raise ToolDefinitionError(
                f"tool {self.name!r}: max_retries>0 com efeito não idempotente; "
                "o retry precisa de idempotência ou de prova de ausência do efeito"
            )
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        major, minor, patch = self.version.split(".")
        return (int(major), int(minor), int(patch))

    def summary(self) -> ToolSummary:
        """Projeção enxuta para o `WorkingContext` — o prompt não recebe tudo."""
        return ToolSummary(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self.input_schema,
            risk_level=self.risk_level,
            requires_confirmation=self.requires_confirmation,
        )


class ToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    risk_level: RiskLevel
    requires_confirmation: bool


class ToolResult(BaseModel):
    """Resultado de uma tentativa. Não diz nada sobre o goal."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    output: Any = None
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def succeeded(cls, output: Any = None, **kwargs) -> ToolResult:
        return cls(status=ExecutionStatus.SUCCESS, output=output, **kwargs)

    @classmethod
    def failed(cls, error_code: ErrorCode, detail: str | None = None, **kwargs) -> ToolResult:
        return cls(
            status=ExecutionStatus.FAILURE,
            error_code=error_code,
            error_detail=detail,
            **kwargs,
        )

    @classmethod
    def unknown_effect(cls, detail: str | None = None, **kwargs) -> ToolResult:
        """Timeout em ação com efeito: `timeout != action_failed` (spec §3)."""
        return cls(
            status=ExecutionStatus.UNKNOWN,
            error_code=ErrorCode.UNKNOWN_SIDE_EFFECT,
            error_detail=detail,
            **kwargs,
        )


ProbeResult = Literal["EFFECT_PRESENT", "EFFECT_ABSENT", "INDETERMINATE"]
