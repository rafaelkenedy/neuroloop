"""Cliente falso — a base dos testes de integração (spec §29).

Devolve saídas pré-definidas e registra o que recebeu, para que os testes
possam afirmar sobre o *prompt* tanto quanto sobre a decisão. É assim que se
verifica, por exemplo, que conteúdo não confiável chegou envelopado.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel

from neuroloop.llm.client import (
    LLMError,
    LLMResponse,
    LLMUsage,
    Message,
    ModelProfile,
    compute_cost,
)

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class RecordedCall:
    messages: list[Message]
    system: str | None
    output_schema: type[BaseModel]
    model_profile: ModelProfile

    @property
    def prompt(self) -> str:
        return "\n\n".join([self.system or "", *(m.content for m in self.messages)])


@dataclass(slots=True)
class FakeLLMClient:
    """Fila de saídas. Esgotar a fila é erro, não repetição silenciosa."""

    outputs: list[BaseModel] = field(default_factory=list)
    input_tokens: int = 1200
    output_tokens: int = 300
    calls: list[RecordedCall] = field(default_factory=list)

    def queue(self, *outputs: BaseModel) -> None:
        self.outputs.extend(outputs)

    async def structured(
        self,
        *,
        messages: Sequence[Message],
        output_schema: type[T],
        model_profile: ModelProfile,
        system: str | None = None,
    ) -> LLMResponse[T]:
        self.calls.append(
            RecordedCall(
                messages=list(messages),
                system=system,
                output_schema=output_schema,
                model_profile=model_profile,
            )
        )
        if not self.outputs:
            raise LLMError("FakeLLMClient sem saídas na fila")
        output = self.outputs.pop(0)
        if not isinstance(output, output_schema):
            raise LLMError(
                f"saída enfileirada é {type(output).__name__}, "
                f"esperava {output_schema.__name__}"
            )
        return LLMResponse[T](
            output=output,
            usage=LLMUsage(
                model=model_profile.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cost_usd=compute_cost(
                    model=model_profile.model,
                    input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens,
                ),
            ),
            stop_reason="end_turn",
        )

    @property
    def last_prompt(self) -> str:
        if not self.calls:
            raise AssertionError("nenhuma chamada registrada")
        return self.calls[-1].prompt


@dataclass(slots=True)
class RefusingLLMClient:
    """Provider que recusa. Existe para o runtime ter esse caminho testado."""

    async def structured(self, **_: object) -> LLMResponse:
        raise LLMError("provider recusou a requisição (stop_reason=refusal)")


def zero_usage(model: str = "fake") -> LLMUsage:
    return LLMUsage(model=model, input_tokens=0, output_tokens=0, cost_usd=Decimal("0"))
