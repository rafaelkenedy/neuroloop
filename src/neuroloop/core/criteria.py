"""`Criterion` — a unidade de verificação determinística.

Correção C01. `Criterion` é o tipo mais load-bearing do sistema: é ele que
sustenta a promessa de "verificação objetiva" e a separação entre tool
success e goal success. A spec original o referenciava em cinco lugares sem
nunca defini-lo.

Duas regras duras vivem aqui:

1.  **Lógica ternária.** ``satisfied=None`` significa INDETERMINATE — o
    critério não pôde ser observado. Nunca conta como satisfeito. Um
    ``AllOf`` com qualquer ``None`` e nenhum ``False`` resulta ``None``,
    não ``True``. Verificação indeterminada é motivo para RECOVER ou
    ASK_USER, jamais para GOAL_COMPLETED.

2.  **Origem da evidência.** ``effective_observes`` é *derivado*, não
    declarado, para que não exista estado inconsistente (ex.: um critério
    que lê o resultado auto-reportado da ação mas se declara observador do
    estado externo). Goal verification só aceita ``EXTERNAL_STATE``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Observes = Literal["EXTERNAL_STATE", "ACTION_RESULT", "RUN_STATE"]

# Força da evidência. ACTION_RESULT é o mais fraco: é o auto-relato da tool,
# exatamente aquilo que o Verifier existe para não tomar como verdade.
_OBSERVES_RANK: dict[str, int] = {
    "ACTION_RESULT": 0,
    "RUN_STATE": 1,
    "EXTERNAL_STATE": 2,
}


class _CriterionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    negate: bool = False


class FileExists(_CriterionBase):
    kind: Literal["FILE_EXISTS"] = "FILE_EXISTS"
    path: str


class FileMatchesJsonSchema(_CriterionBase):
    kind: Literal["FILE_MATCHES_JSON_SCHEMA"] = "FILE_MATCHES_JSON_SCHEMA"
    path: str
    json_schema: dict[str, Any]


class JsonPathEquals(_CriterionBase):
    kind: Literal["JSON_PATH_EQUALS"] = "JSON_PATH_EQUALS"
    source: Literal["FILE", "ACTION_RESULT"]
    json_path: str
    expected: Any = None
    path: str | None = None
    """Caminho do arquivo; obrigatório quando ``source == "FILE"``."""


class JsonPathCount(_CriterionBase):
    kind: Literal["JSON_PATH_COUNT"] = "JSON_PATH_COUNT"
    source: Literal["FILE", "ACTION_RESULT"]
    json_path: str
    expected_count: int = Field(ge=0)
    path: str | None = None


class ValueEquals(_CriterionBase):
    kind: Literal["VALUE_EQUALS"] = "VALUE_EQUALS"
    ref: str
    """Referência resolvível no contexto de avaliação (estado do run)."""
    expected: Any = None


class HttpStatusEquals(_CriterionBase):
    kind: Literal["HTTP_STATUS_EQUALS"] = "HTTP_STATUS_EQUALS"
    method: Literal["GET", "HEAD"] = "GET"
    url: str
    expected_status: int = Field(ge=100, le=599)


class CommandExitCodeEquals(_CriterionBase):
    kind: Literal["COMMAND_EXIT_CODE_EQUALS"] = "COMMAND_EXIT_CODE_EQUALS"
    command: tuple[str, ...] = Field(min_length=1)
    expected_exit_code: int = 0


class AllOf(_CriterionBase):
    kind: Literal["ALL_OF"] = "ALL_OF"
    criteria: tuple["Criterion", ...] = Field(min_length=1)


class AnyOf(_CriterionBase):
    kind: Literal["ANY_OF"] = "ANY_OF"
    criteria: tuple["Criterion", ...] = Field(min_length=1)


Criterion = Annotated[
    FileExists
    | FileMatchesJsonSchema
    | JsonPathEquals
    | JsonPathCount
    | ValueEquals
    | HttpStatusEquals
    | CommandExitCodeEquals
    | AllOf
    | AnyOf,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()


class CriterionOutcome(BaseModel):
    """Resultado de avaliar um `Criterion`.

    ``satisfied=None`` é INDETERMINATE e é semanticamente distinto de
    ``False``: significa que não foi possível observar, não que o critério
    foi refutado.
    """

    model_config = ConfigDict(extra="forbid")

    criterion_kind: str
    satisfied: bool | None
    observed: Any = None
    expected: Any = None
    error: str | None = None
    observed_at: datetime

    @property
    def is_indeterminate(self) -> bool:
        return self.satisfied is None


def effective_observes(criterion: Criterion) -> Observes:
    """Deriva a origem da evidência de um critério.

    Composto é ``EXTERNAL_STATE`` apenas se *todos* os filhos forem; caso
    contrário assume a evidência mais fraca da árvore. Isso impede que um
    ``AllOf`` misturando estado externo e auto-relato da tool seja aceito
    para concluir um goal.
    """
    match criterion:
        case FileExists() | FileMatchesJsonSchema() | HttpStatusEquals() | CommandExitCodeEquals():
            return "EXTERNAL_STATE"
        case ValueEquals():
            return "RUN_STATE"
        case JsonPathEquals() | JsonPathCount():
            return "EXTERNAL_STATE" if criterion.source == "FILE" else "ACTION_RESULT"
        case AllOf() | AnyOf():
            weakest = min(
                (_OBSERVES_RANK[effective_observes(child)] for child in criterion.criteria),
            )
            return _RANK_TO_OBSERVES[weakest]
        case _:  # pragma: no cover - união fechada
            raise TypeError(f"Criterion não suportado: {type(criterion)!r}")


_RANK_TO_OBSERVES: dict[int, Observes] = {v: k for k, v in _OBSERVES_RANK.items()}  # type: ignore[misc]


def iter_criteria(criterion: Criterion):
    """Percorre a árvore em pré-ordem, incluindo o próprio nó."""
    yield criterion
    if isinstance(criterion, AllOf | AnyOf):
        for child in criterion.criteria:
            yield from iter_criteria(child)


# --------------------------------------------------------------------------
# Lógica ternária (Kleene). Funções puras — a avaliação em si é TASK-007.
# --------------------------------------------------------------------------


def apply_negate(value: bool | None, negate: bool) -> bool | None:
    """INDETERMINATE negado continua INDETERMINATE."""
    if value is None or not negate:
        return value
    return not value


def combine_all(values: list[bool | None]) -> bool | None:
    """Conjunção ternária: False domina; na ausência dele, None domina."""
    if not values:
        raise ValueError("combine_all exige ao menos um valor")
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


def combine_any(values: list[bool | None]) -> bool | None:
    """Disjunção ternária: True domina; na ausência dele, None domina."""
    if not values:
        raise ValueError("combine_any exige ao menos um valor")
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


def is_conclusive_success(outcomes: list[CriterionOutcome]) -> bool:
    """True apenas se todos os critérios foram observados e satisfeitos.

    Qualquer INDETERMINATE derruba a conclusão. Lista vazia é False:
    ausência de evidência nunca é evidência de sucesso.
    """
    if not outcomes:
        return False
    return all(o.satisfied is True for o in outcomes)
