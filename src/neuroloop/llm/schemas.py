"""Schema de saída do LLM e tradução para o domínio — TASK-010.

**Por que existe um schema separado do domínio.** Structured outputs não
aceitam schema recursivo, e `Criterion` é recursivo por causa de `AllOf` /
`AnyOf`. Também não aceitam objetos de forma livre (`additionalProperties`
precisa ser `false`), o que exclui `arguments: dict[str, Any]`.

Isso vira uma vantagem, não um contorno: a superfície que o LLM pode
produzir fica **estritamente menor** que o domínio. Ele não escreve árvores
de critérios aninhadas nem dicionários arbitrários; escreve uma lista de
critérios folha (a lista já é uma conjunção) e argumentos como JSON, que são
decodificados e validados aqui. O que não passa nesta fronteira nunca vira
`ActionProposal`.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.actions import ActionProposal, UserInputRequest
from neuroloop.core.criteria import (
    CommandExitCodeEquals,
    Criterion,
    FileExists,
    HttpStatusEquals,
    JsonPathCount,
    JsonPathEquals,
)
from neuroloop.core.decisions import (
    ActDecision,
    AskUserDecision,
    Decision,
    ImpossibleDecision,
    PlanDecision,
)
from neuroloop.core.enums import ErrorCode, RiskLevel
from neuroloop.core.plans import Plan, PlanStep


class DecisionTranslationError(ValueError):
    """A saída do LLM é sintaticamente válida mas não vira decisão do domínio."""

    def __init__(self, detail: str, error_code: ErrorCode = ErrorCode.REASONING_ERROR) -> None:
        self.error_code = error_code
        super().__init__(f"{error_code.value}: {detail}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------- critérios


class LlmFileExists(_Strict):
    kind: Literal["FILE_EXISTS"] = "FILE_EXISTS"
    path: str


class LlmJsonPathCount(_Strict):
    kind: Literal["JSON_PATH_COUNT"] = "JSON_PATH_COUNT"
    source: Literal["FILE", "ACTION_RESULT"]
    json_path: str
    expected_count: int
    path: str | None = None


class LlmJsonPathEquals(_Strict):
    kind: Literal["JSON_PATH_EQUALS"] = "JSON_PATH_EQUALS"
    source: Literal["FILE", "ACTION_RESULT"]
    json_path: str
    expected_json: str
    """Valor esperado codificado em JSON — `Any` não é expressável no schema."""
    path: str | None = None


class LlmHttpStatusEquals(_Strict):
    kind: Literal["HTTP_STATUS_EQUALS"] = "HTTP_STATUS_EQUALS"
    method: Literal["GET", "HEAD"] = "GET"
    url: str
    expected_status: int


class LlmCommandExitCodeEquals(_Strict):
    kind: Literal["COMMAND_EXIT_CODE_EQUALS"] = "COMMAND_EXIT_CODE_EQUALS"
    command: list[str]
    expected_exit_code: int = 0


LlmCriterion = Annotated[
    LlmFileExists
    | LlmJsonPathCount
    | LlmJsonPathEquals
    | LlmHttpStatusEquals
    | LlmCommandExitCodeEquals,
    Field(discriminator="kind"),
]
"""Somente critérios folha. Conjunção se expressa como lista."""


# --------------------------------------------------------------- decisão


class LlmActionProposal(_Strict):
    tool: str
    arguments_json: str = "{}"
    """Objeto JSON. Dicionário livre não é expressável em structured output."""
    expected_outcomes: list[LlmCriterion] = Field(min_length=1)
    rationale_code: str
    derived_from: list[str] = Field(default_factory=list)
    """Ids das observações que originaram os argumentos (correção C10)."""


class LlmPlanStep(_Strict):
    id: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    preferred_tool: str | None = None
    arguments_json: str | None = None
    expected_outcomes: list[LlmCriterion] = Field(min_length=1)
    risk_hint: Literal["R0", "R1", "R2", "R3"] = "R0"


class LlmPlan(_Strict):
    objective: str
    completion_condition: str
    steps: list[LlmPlanStep] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)


class LlmDecision(_Strict):
    """Saída única do Deliberator (spec §11).

    Campos opcionais em vez de união de objetos: mais simples de produzir e
    a coerência é imposta abaixo, não confiada ao modelo.
    """

    type: Literal["ACT", "PLAN", "ASK_USER", "IMPOSSIBLE"]
    reason_code: str
    action: LlmActionProposal | None = None
    plan: LlmPlan | None = None
    question: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _branch_must_match_type(self) -> LlmDecision:
        exigido = {
            "ACT": ("action", self.action),
            "PLAN": ("plan", self.plan),
            "ASK_USER": ("question", self.question),
            "IMPOSSIBLE": ("evidence", self.evidence or None),
        }[self.type]
        campo, valor = exigido
        if valor is None:
            raise ValueError(f"decisão {self.type} exige o campo {campo!r}")
        return self


# ------------------------------------------------------------- tradução


def _parse_json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise DecisionTranslationError(
            f"{field} não é JSON válido: {error}", ErrorCode.TOOL_VALIDATION_ERROR
        ) from error
    if not isinstance(parsed, dict):
        raise DecisionTranslationError(
            f"{field} precisa ser objeto JSON", ErrorCode.TOOL_VALIDATION_ERROR
        )
    return parsed


def to_criterion(raw: LlmCriterion) -> Criterion:
    match raw:
        case LlmFileExists():
            return FileExists(path=raw.path)
        case LlmJsonPathCount():
            return JsonPathCount(
                source=raw.source,
                path=raw.path,
                json_path=raw.json_path,
                expected_count=raw.expected_count,
            )
        case LlmJsonPathEquals():
            try:
                expected = json.loads(raw.expected_json)
            except ValueError as error:
                raise DecisionTranslationError(
                    f"expected_json inválido: {error}", ErrorCode.TOOL_VALIDATION_ERROR
                ) from error
            return JsonPathEquals(
                source=raw.source,
                path=raw.path,
                json_path=raw.json_path,
                expected=expected,
            )
        case LlmHttpStatusEquals():
            return HttpStatusEquals(
                method=raw.method, url=raw.url, expected_status=raw.expected_status
            )
        case LlmCommandExitCodeEquals():
            return CommandExitCodeEquals(
                command=tuple(raw.command), expected_exit_code=raw.expected_exit_code
            )
        case _:  # pragma: no cover - união fechada
            raise DecisionTranslationError("critério desconhecido")


def _to_uuids(values: list[str]) -> tuple[UUID, ...]:
    try:
        return tuple(UUID(v) for v in values)
    except ValueError as error:
        raise DecisionTranslationError(
            f"derived_from com id inválido: {error}", ErrorCode.TOOL_VALIDATION_ERROR
        ) from error


def to_action_proposal(raw: LlmActionProposal) -> ActionProposal:
    return ActionProposal(
        tool=raw.tool,
        arguments=_parse_json_object(raw.arguments_json, field="arguments_json"),
        expected_outcomes=tuple(to_criterion(c) for c in raw.expected_outcomes),
        rationale_code=raw.rationale_code,
        derived_from=_to_uuids(raw.derived_from),
    )


def to_plan(raw: LlmPlan, *, plan_id: UUID, version: int) -> Plan:
    steps = tuple(
        PlanStep(
            id=step.id,
            description=step.description,
            dependencies=tuple(step.dependencies),
            preferred_tool=step.preferred_tool,
            arguments=(
                _parse_json_object(step.arguments_json, field=f"step {step.id} arguments_json")
                if step.arguments_json is not None
                else None
            ),
            expected_outcomes=tuple(to_criterion(c) for c in step.expected_outcomes),
            risk_hint=RiskLevel(step.risk_hint),
        )
        for step in raw.steps
    )
    return Plan(
        id=plan_id,
        version=version,
        objective=raw.objective,
        steps=steps,
        assumptions=tuple(raw.assumptions),
        completion_condition=raw.completion_condition,
    )


def to_decision(raw: LlmDecision, *, plan_id: UUID, plan_version: int = 1) -> Decision:
    """Traduz e revalida. Erro aqui é do LLM, não do runtime.

    A validação do domínio (DAG, limite de steps, proveniência) roda de novo
    do lado de dentro — não se confia na saída do modelo por ela ter passado
    no schema.
    """
    try:
        match raw.type:
            case "ACT":
                return ActDecision(
                    action=to_action_proposal(raw.action),
                    reason_code=raw.reason_code,
                    source="DELIBERATOR",
                )
            case "PLAN":
                return PlanDecision(
                    plan=to_plan(raw.plan, plan_id=plan_id, version=plan_version),
                    reason_code=raw.reason_code,
                )
            case "ASK_USER":
                return AskUserDecision(
                    request=UserInputRequest(
                        type="MISSING_INFORMATION",
                        message=raw.question,
                        required_fields=tuple(raw.required_fields),
                    ),
                    reason_code=raw.reason_code,
                )
            case _:
                return ImpossibleDecision(
                    evidence=tuple(raw.evidence), reason_code=raw.reason_code
                )
    except DecisionTranslationError:
        raise
    except ValueError as error:
        raise DecisionTranslationError(
            str(error), _codigo_embutido(str(error))
        ) from error


def _codigo_embutido(mensagem: str) -> ErrorCode:
    """Recupera o código que o validador de domínio embutiu na mensagem.

    Pydantic exige que validador levante `ValueError`, o que apaga o tipo da
    exceção original. Sem isto, tudo que sobe de um validador vira
    `PLANNING_ERROR` — inclusive falha de proveniência, que não tem nada a ver
    com planejamento. Um modelo local que omitia `derived_from` era reportado
    como erro de planejamento em toda execução, e a taxonomia de falhas, cujo
    propósito é dizer o que deu errado, apontava para o lugar errado.

    Os validadores do domínio já prefixam a mensagem com o próprio código;
    aqui só se lê de volta o que eles escreveram.
    """
    for code in ErrorCode:
        if code.value in mensagem:
            return code
    return ErrorCode.PLANNING_ERROR
