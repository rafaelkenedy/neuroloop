"""Identidade de ações — correção C09.

Dois identificadores com propósitos disjuntos, que a spec original tratava
como um só:

``idempotency_key = f(run_id, logical_action_id)``
    Semântica *at-most-once* do efeito externo. **Constante entre tentativas
    da mesma ação lógica** — é exatamente isso que torna o retry seguro, e é
    o valor enviado ao serviço externo quando a tool suporta.

``action_fingerprint = f(tool, tool_version, arguments, target_resource)``
    Detecção de loop *dentro* do run. Não deduplica execução: escrever o
    mesmo arquivo em dois steps é legítimo. Alimenta `LOOP_DETECTED`.

Confundir os dois produz um de dois bugs: ou o retry cria efeito duplicado
(chave variando entre tentativas), ou ações legítimas repetidas são
silenciosamente suprimidas (fingerprint usado como chave de idempotência).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

_DIGEST_CHARS = 32
LOOP_REPEAT_THRESHOLD = 3
"""Ocorrências do mesmo fingerprint sem progresso verificado (correção C09)."""


def canonical_json(value: Any) -> str:
    """Serialização estável: chaves ordenadas, sem espaço, UTF-8 preservado."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()[:_DIGEST_CHARS]}"


def make_idempotency_key(run_id: UUID, logical_action_id: UUID) -> str:
    """Estável entre tentativas; único por ação lógica dentro do run."""
    return _digest("idem", str(run_id), str(logical_action_id))


def make_action_fingerprint(
    *,
    tool: str,
    tool_version: str,
    arguments: dict[str, Any],
    target_resource: str | None = None,
) -> str:
    """Identifica *a mesma ação*, não a mesma tentativa."""
    return _digest(
        "fp",
        tool,
        tool_version,
        canonical_json(arguments),
        target_resource or "",
    )


def is_loop(occurrences: int, *, progressed_since: bool) -> bool:
    """Repetição só é loop quando não houve progresso verificado no meio.

    Um agente que escreve o mesmo arquivo três vezes intercalando
    verificações bem-sucedidas está avançando, não travado.
    """
    if progressed_since:
        return False
    return occurrences >= LOOP_REPEAT_THRESHOLD
