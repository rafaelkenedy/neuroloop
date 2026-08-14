"""Redação do trace — TASK-014 (spec §33).

A spec é explícita: **não guardar chain-of-thought nem secrets**. As duas
proibições têm motivos diferentes e ambas importam.

*Chain-of-thought* não é decisão: é rascunho. Guardá-lo cria a tentação de
auditar o raciocínio em vez do efeito, e é exatamente o tipo de evidência
que o Verifier existe para não aceitar. O `reason_code` é telemetria; o
texto do pensamento não entra.

*Secrets* vazam por acidente — um argumento de tool com token, uma URL com
credencial embutida. A redação é por **chave** e por **forma**, porque
nenhuma das duas sozinha pega tudo.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "«redigido»"

SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|authorization|credential|"
    r"private[_-]?key|access[_-]?key|bearer)",
    re.IGNORECASE,
)

THINKING_KEYS = frozenset(
    {"thinking", "reasoning", "chain_of_thought", "scratchpad", "deliberation_text"}
)

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{12,}")
_LONG_SECRET = re.compile(r"\b(sk|pk|ghp|gho|xox[abps])[-_][A-Za-z0-9._\-]{16,}\b")

MAX_STRING_CHARS = 2000
"""Trace não é armazenamento de conteúdo; artefato grande fica no mundo."""


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if _is_sensitive_key(key)
                else redact(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    return key.lower() in THINKING_KEYS or bool(SECRET_KEY_PATTERN.search(key))


def _redact_text(text: str) -> str:
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    text = _BEARER.sub(f"\\1 {REDACTED}", text)
    text = _LONG_SECRET.sub(REDACTED, text)
    if len(text) > MAX_STRING_CHARS:
        return f"{text[:MAX_STRING_CHARS]}…[{len(text)} chars]"
    return text


def contains_secret(value: Any) -> bool:
    """Usado em teste: a redação pegou tudo o que deveria?"""
    redacted = redact(value)
    return canonical(redacted) != canonical(redact(redacted))


def canonical(value: Any) -> str:
    from neuroloop.core.identity import canonical_json

    return canonical_json(value)
