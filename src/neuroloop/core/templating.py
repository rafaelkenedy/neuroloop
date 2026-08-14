"""Substituição de placeholders em estruturas declarativas.

Usado por `EffectProbe` (tools) e por `SkillDefinition` (memória
procedural): ambos guardam um gabarito versionado com `{nome}` no lugar dos
valores concretos.

Regra que importa: `"{path}"` sozinho **preserva o tipo** do valor; um
placeholder interpolado no meio de um texto vira string. Sem isso, um
argumento numérico viraria `"3"` ao passar pelo gabarito.
"""

from __future__ import annotations

from typing import Any


class MissingPlaceholder(KeyError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"placeholder {name!r} sem valor")


def substitute(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute(item, values) for item in node]
    if isinstance(node, tuple):
        return tuple(substitute(item, values) for item in node)
    if isinstance(node, str):
        exact = node.strip()
        if exact.startswith("{") and exact.endswith("}") and exact[1:-1] in values:
            return values[exact[1:-1]]
        for placeholder, value in values.items():
            node = node.replace("{" + placeholder + "}", str(value))
        return node
    return node


def placeholders(node: Any) -> set[str]:
    """Nomes citados na estrutura, para validar um gabarito no registro."""
    found: set[str] = set()
    _collect(node, found)
    return found


def _collect(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _collect(value, found)
    elif isinstance(node, list | tuple):
        for item in node:
            _collect(item, found)
    elif isinstance(node, str):
        rest = node
        while "{" in rest and "}" in rest:
            start = rest.index("{")
            end = rest.index("}", start)
            name = rest[start + 1 : end]
            if name and " " not in name:
                found.add(name)
            rest = rest[end + 1 :]
