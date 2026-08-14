"""Fronteira com o LLM — TASK-010 (spec §21, correção C12).

O núcleo não conhece SDK de provider. Ele conhece este protocolo:

    structured(messages, output_schema, model_profile) -> LLMResponse

`LLMResponse` carrega `usage` porque sem isso o budget do run é ficção: a
spec lia `tokens_used` e `cost_used_usd` do checkpoint sem que nada os
escrevesse (C12). O cálculo de custo vive aqui, na camada adapter, a partir
de uma tabela de preços versionada — o core nunca multiplica dólares.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"
"""Padrão do projeto. Trocar exige revisar `PRICING` e o perfil abaixo."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD por milhão de tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_multiplier: Decimal = Decimal("0.1")
    cache_write_multiplier: Decimal = Decimal("1.25")


PRICING_VERSION = date(2026, 6, 24)
"""Data da tabela. Preço muda; sem versão o custo histórico vira chute."""

PRICING: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": ModelPricing(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": ModelPricing(Decimal("1.00"), Decimal("5.00")),
    "claude-fable-5": ModelPricing(Decimal("10.00"), Decimal("50.00")),
}


class LLMUsage(BaseModel):
    """Consumo de uma chamada. Alimenta o budget do run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    pricing_version: date = PRICING_VERSION

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


class LLMResponse[T: BaseModel](BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    output: T
    usage: LLMUsage
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Perfil de chamada. Nome lógico → configuração concreta.

    O runtime pede `DELIBERATION`, não `claude-opus-5 com effort high`.
    Trocar modelo é trocar o perfil, não caçar strings pelo código.
    """

    name: str
    model: str = DEFAULT_MODEL
    max_tokens: int = 8192
    effort: str | None = "high"
    """`low` | `medium` | `high` | `xhigh` | `max`. None deixa o default do provider."""

    def pricing(self) -> ModelPricing | None:
        return PRICING.get(self.model)


DELIBERATION = ModelProfile(name="DELIBERATION", max_tokens=16000, effort="high")
"""Decidir ACT/PLAN/ASK_USER/IMPOSSIBLE é a chamada mais cara do ciclo.

`max_tokens` folgado de propósito: no Opus 5 o pensamento é ligado por
padrão e `max_tokens` limita **pensamento + resposta** juntos. Um teto
dimensionado só para o JSON de saída trunca a resposta no meio.
"""

SUMMARIZATION = ModelProfile(
    name="SUMMARIZATION", model="claude-haiku-4-5", max_tokens=1024, effort=None
)
"""Resumo de episódio é trabalho barato; não gasta Opus nisso."""


def compute_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Decimal:
    """Custo em USD. Devolve 0 para modelo fora da tabela, sem inventar preço."""
    pricing = PRICING.get(model)
    if pricing is None:
        return Decimal("0")
    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens) * pricing.input_per_mtok
        + Decimal(output_tokens) * pricing.output_per_mtok
        + Decimal(cache_read_input_tokens)
        * pricing.input_per_mtok
        * pricing.cache_read_multiplier
        + Decimal(cache_creation_input_tokens)
        * pricing.input_per_mtok
        * pricing.cache_write_multiplier
    ) / million
    return cost.quantize(Decimal("0.000001"))


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    content: str


class LLMError(RuntimeError):
    """Falha na chamada ao provider. O runtime trata como REASONING_ERROR."""


@runtime_checkable
class LLMClient(Protocol):
    """Toda decisão crítica é structured output (spec §26).

    Não existe método que devolva texto livre: se o núcleo não pode validar
    a resposta contra um schema, ela não entra no sistema.
    """

    async def structured(
        self,
        *,
        messages: list[Message],
        output_schema: type[T],
        model_profile: ModelProfile,
        system: str | None = None,
    ) -> LLMResponse[T]: ...


def usage_from_provider(
    *, model: str, raw: Any, pricing_version: date = PRICING_VERSION
) -> LLMUsage:
    """Normaliza o objeto `usage` do SDK, tolerando campos ausentes."""
    def _field(name: str) -> int:
        value = getattr(raw, name, 0)
        return int(value or 0)

    input_tokens = _field("input_tokens")
    output_tokens = _field("output_tokens")
    cache_read = _field("cache_read_input_tokens")
    cache_write = _field("cache_creation_input_tokens")
    return LLMUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        cost_usd=compute_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        pricing_version=pricing_version,
    )
