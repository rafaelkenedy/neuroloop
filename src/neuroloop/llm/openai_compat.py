"""Adapter para servidor OpenAI-compatível — LM Studio, Ollama, vLLM.

Existe para exercitar o runtime contra um modelo **real** sem depender de API
paga. O valor do teste não é a qualidade do modelo local: é que um modelo fraco
gera entrada adversarial de graça. As portas de validação do `Deliberator`, o
registry de tools e o `PlannerValidator` nunca foram exercitados contra uma
saída que eu não escrevi.

Três diferenças em relação ao adapter Anthropic, todas medidas contra o LM
Studio antes de escrever este módulo:

**O envelope é `response_format`, não `output_config`.** Mesma ideia, chave
diferente. O JSON Schema em si é idêntico — os dois lados exigem
`additionalProperties: false` e `required` completo, então `strict_json_schema`
é reaproveitado sem alteração.

**Não há equivalente a `effort`.** `ModelProfile.effort` é ignorado aqui. O
esforço de raciocínio de um modelo local é decidido no carregamento, não na
requisição.

**Pensamento e resposta dividem `max_tokens`, e o estouro é silencioso.** Com
teto apertado o servidor devolve HTTP 200, `finish_reason="length"` e
`content` **vazio** — não um erro. Medido nos três modelos carregados: 297 de
300 tokens gastos em `reasoning_tokens`, zero de conteúdo. Um adapter ingênuo
trataria isso como "JSON inválido"; aqui vira mensagem específica.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from neuroloop.llm.anthropic_client import strict_json_schema
from neuroloop.llm.client import (
    LLMError,
    LLMResponse,
    LLMUsage,
    Message,
    ModelProfile,
    compute_cost,
)

T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "http://localhost:1234/v1"
"""Padrão do LM Studio. Ollama usa 11434, vLLM usa 8000."""

DEFAULT_TIMEOUT = 1800.0
"""Teto de leitura, em segundos.

Dimensionado por medição, não por hábito. `google/gemma-4-12b-qat` gera a
~9 tok/s; o teto de 4096 tokens do perfil local dá ~440s só de geração, antes
do processamento do prompt. Com 600s, três baterias falharam sempre na
**segunda** deliberação — a que já carrega o conteúdo do arquivo lido — com
`httpx.ReadTimeout`.

Teto largo não custa nada quando a chamada funciona: só adia o erro quando ela
não funciona. Inferência local não é cobrada por segundo.
"""

LOCAL_DELIBERATION = ModelProfile(
    name="LOCAL_DELIBERATION",
    model="google/gemma-4-e4b",
    max_tokens=8192,
    effort=None,
)
"""Perfil de deliberação para modelo local.

`max_tokens` é o parâmetro que mais importa aqui, e 4096 foi medido como
insuficiente: na **segunda** deliberação — a que já carrega o conteúdo do
arquivo lido no ciclo anterior — `gemma-4-12b-qat` gastava os 4096 inteiros
pensando e devolvia conteúdo vazio. O prompt maior induz mais raciocínio, e
pensamento e resposta dividem o mesmo orçamento.

Continua abaixo dos 16000 de `DELIBERATION` porque a conta aqui é de relógio,
não de dinheiro: a ~9 tok/s, 16000 tokens são ~29 minutos numa única chamada.

`effort=None` porque o campo não tem destino nesta API.
"""

_THINK_BLOCK = re.compile(r"^\s*<(think|thinking|reasoning)>.*?</\1>\s*", re.DOTALL)


class OpenAICompatLLMClient:
    """Implementa `LLMClient` sobre `/v1/chat/completions`.

    `httpx` é importado na construção, não no topo: o núcleo não deve ganhar
    dependência de rede por causa de um adapter opcional.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "lm-studio",
        timeout: float = DEFAULT_TIMEOUT,
        model_override: str | None = None,
        client: Any | None = None,
    ) -> None:
        """`model_override` vence `ModelProfile.model`.

        Não é gambiarra de teste: um servidor local serve o modelo que está
        carregado. O perfil continua mandando em `max_tokens`, que é o
        parâmetro que de fato muda o comportamento aqui.
        """
        self._model_override = model_override
        if client is None:
            try:
                import httpx
            except ImportError as error:  # pragma: no cover - depende do ambiente
                raise LLMError(
                    "pacote `httpx` não instalado; necessário para o adapter local"
                ) from error
            client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def structured(
        self,
        *,
        messages: list[Message],
        output_schema: type[T],
        model_profile: ModelProfile,
        system: str | None = None,
    ) -> LLMResponse[T]:
        wire: list[dict[str, str]] = []
        if system is not None:
            wire.append({"role": "system", "content": system})
        wire.extend({"role": m.role, "content": m.content} for m in messages)

        modelo = self._model_override or model_profile.model
        request = {
            "model": modelo,
            "max_tokens": model_profile.max_tokens,
            "messages": wire,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(output_schema),
                },
            },
        }

        try:
            response = await self._client.post("/chat/completions", json=request)
        except Exception as error:  # noqa: BLE001 - traduz falha de transporte
            # O tipo entra na mensagem porque as exceções de timeout do httpx
            # têm `str()` vazio: sem ele a falha chega como "chamada falhou:"
            # e não distingue timeout de conexão recusada.
            raise LLMError(
                f"chamada ao servidor local falhou: {type(error).__name__}: {error}"
            ) from error

        if response.status_code != 200:
            raise LLMError(
                f"servidor local respondeu {response.status_code}: "
                f"{response.text[:300]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise LLMError("resposta sem `choices`")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content") or ""

        # Ordem importa: `length` com conteúdo vazio é o modo de falha comum
        # de modelo de raciocínio, e a mensagem precisa dizer o que fazer.
        if finish_reason == "length" and not text.strip():
            raise LLMError(
                "modelo gastou todo o `max_tokens` em raciocínio e não emitiu "
                f"resposta (perfil {model_profile.name}, teto "
                f"{model_profile.max_tokens}); aumente o teto"
            )
        if finish_reason == "length":
            raise LLMError("resposta truncada por max_tokens")
        if not text.strip():
            raise LLMError(f"resposta vazia (finish_reason={finish_reason})")

        # Alguns modelos vazam o bloco de pensamento no `content` em vez de
        # devolvê-lo em campo separado. Remove só o prefixo bem formado; não
        # sai garimpando JSON no meio de texto livre.
        text = _THINK_BLOCK.sub("", text)

        try:
            output = output_schema.model_validate(json.loads(text))
        except (ValueError, ValidationError) as error:
            raise LLMError(f"saída não bate com o schema: {error}") from error

        return LLMResponse[T](
            output=output,
            usage=_usage(modelo, body.get("usage") or {}),
            stop_reason=finish_reason,
        )


def _usage(model: str, raw: dict[str, Any]) -> LLMUsage:
    """Normaliza o `usage` OpenAI, que usa nomes diferentes do Anthropic.

    `cost_usd` sai zero porque um modelo local não está em `PRICING` — e essa
    é a resposta certa, não um buraco: a inferência é gratuita. Contar tokens
    continua valendo, porque o budget do run também limita tokens.
    """
    entrada = int(raw.get("prompt_tokens") or 0)
    saida = int(raw.get("completion_tokens") or 0)
    custo = compute_cost(model=model, input_tokens=entrada, output_tokens=saida)
    return LLMUsage(
        model=model,
        input_tokens=entrada,
        output_tokens=saida,
        cost_usd=custo if custo else Decimal("0"),
    )
