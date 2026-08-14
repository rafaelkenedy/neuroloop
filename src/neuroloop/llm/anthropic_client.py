"""Adapter do provider — TASK-010.

Único módulo que conhece o SDK da Anthropic. O import é local à construção
para que o núcleo (e a suíte de testes) não dependa do pacote.

Duas decisões de implementação que vale registrar:

**Usa `messages.create` com `output_config.format`, não `messages.parse`.**
`parse` monta o `output_config` internamente a partir de `output_format`, e
precisamos do mesmo objeto para passar `effort`. Com `create` a requisição é
explícita e o JSON é decodificado e validado aqui.

**Endurece o JSON Schema antes de enviar.** Structured outputs exigem
`additionalProperties: false` e `required` completo em todo objeto; o schema
que o Pydantic gera marca como obrigatório apenas o que não tem default.
`strict_json_schema` normaliza isso.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from neuroloop.llm.client import (
    LLMError,
    LLMResponse,
    Message,
    ModelProfile,
    usage_from_provider,
)

T = TypeVar("T", bound=BaseModel)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema aceito por structured outputs.

    Tornar todo campo obrigatório é seguro porque campos opcionais do
    Pydantic viram `anyOf: [..., {"type": "null"}]` — o modelo pode
    responder `null`, mas não pode simplesmente omitir a chave.
    """
    return _harden(model.model_json_schema())


def _harden(node: Any) -> Any:
    if isinstance(node, list):
        return [_harden(item) for item in node]
    if not isinstance(node, dict):
        return node

    hardened = {key: _harden(value) for key, value in node.items()}
    if hardened.get("type") == "object" or "properties" in hardened:
        properties = hardened.get("properties", {})
        hardened["additionalProperties"] = False
        hardened["required"] = sorted(properties)
    return hardened


class AnthropicLLMClient:
    """Implementa `LLMClient` sobre o SDK oficial."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as error:  # pragma: no cover - depende do ambiente
                raise LLMError(
                    "pacote `anthropic` não instalado; use o extra [llm] "
                    "ou injete um cliente"
                ) from error
            client = anthropic.AsyncAnthropic()
        self._client = client

    async def structured(
        self,
        *,
        messages: list[Message],
        output_schema: type[T],
        model_profile: ModelProfile,
        system: str | None = None,
    ) -> LLMResponse[T]:
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": strict_json_schema(output_schema),
            }
        }
        if model_profile.effort:
            output_config["effort"] = model_profile.effort

        request: dict[str, Any] = {
            "model": model_profile.model,
            "max_tokens": model_profile.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "output_config": output_config,
        }
        if system is not None:
            request["system"] = system

        try:
            response = await self._client.messages.create(**request)
        except Exception as error:  # noqa: BLE001 - traduz falha do provider
            raise LLMError(f"chamada ao provider falhou: {error}") from error

        # Checar antes de ler `content`: numa recusa o conteúdo vem vazio
        # (pré-saída) ou parcial (meio de stream).
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise LLMError("provider recusou a requisição (stop_reason=refusal)")
        if stop_reason == "max_tokens":
            raise LLMError(
                "resposta truncada por max_tokens; aumente o teto do perfil "
                "(pensamento e resposta dividem o mesmo orçamento)"
            )

        text = _first_text_block(response)
        try:
            output = output_schema.model_validate(json.loads(text))
        except (ValueError, ValidationError) as error:
            raise LLMError(f"saída não bate com o schema: {error}") from error

        return LLMResponse[T](
            output=output,
            usage=usage_from_provider(
                model=getattr(response, "model", model_profile.model),
                raw=getattr(response, "usage", None),
            ),
            stop_reason=stop_reason,
        )


def _first_text_block(response: Any) -> str:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise LLMError("resposta sem bloco de texto")
