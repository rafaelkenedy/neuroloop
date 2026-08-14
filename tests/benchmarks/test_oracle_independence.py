"""Correção C17: o oracle não pode compartilhar código com o Verifier.

Este teste é a única coisa que impede a métrica principal de virar
tautologia. Se um dia alguém importar o avaliador de critérios dentro de
`oracles.py` para "reaproveitar", o `false_success_rate` passa a medir zero
por construção — e o B2 deixa de detectar qualquer coisa.
"""

from __future__ import annotations

import ast
from pathlib import Path

ORACLES = Path(__file__).parent / "oracles.py"


def _imported_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_oracles_nao_importam_o_agente():
    """Independência total, não apenas do módulo de verificação.

    A regra é binária de propósito: "não importa nada de `neuroloop`" é
    verificável e não abre espaço para discussão sobre qual import seria
    inofensivo.
    """
    proibidos = {m for m in _imported_modules(ORACLES) if m.split(".")[0] == "neuroloop"}
    assert proibidos == set(), (
        f"oracles.py importa código do agente: {sorted(proibidos)}. "
        "O oracle precisa ser implementação independente (C17)."
    )


def test_oracles_usam_apenas_stdlib_e_sqlalchemy():
    permitidos = {"__future__", "json", "dataclasses", "pathlib", "typing", "uuid", "sqlalchemy"}
    usados = {m.split(".")[0] for m in _imported_modules(ORACLES)}
    assert usados <= permitidos, f"dependências inesperadas: {sorted(usados - permitidos)}"


def test_oracle_de_falso_sucesso_existe():
    """Guarda contra remoção acidental do oracle mais importante."""
    tree = ast.parse(ORACLES.read_text(encoding="utf-8"))
    funcoes = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert {
        "b1_tool_failure_recovery",
        "b2_false_success_trap",
        "b3_crash_idempotency",
        "b4_prompt_injection",
        "b5_memory_reuse",
    } <= funcoes
