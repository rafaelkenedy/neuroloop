"""Avaliação determinística de `Criterion` — TASK-007.

Regra que este módulo existe para honrar: critério que **não pôde ser
observado** devolve `satisfied=None` (INDETERMINATE), nunca `False`. Tratar
"não sei" como "não" produz replanejamento espúrio; tratar como "sim"
produz falso sucesso. São erros distintos e caros.

A avaliação é assíncrona porque observar o mundo é I/O: reler um arquivo,
consultar um endpoint, rodar um comando. Uma versão síncrona bloquearia o
loop do agente durante o probe — exatamente no momento em que ele mais
precisa continuar respondendo.

Sondas externas são **injetadas**, não importadas: o avaliador não decide
como falar HTTP nem como executar comando. Sem sonda, o critério é
INDETERMINATE — o que é honesto e, pela regra acima, nunca vira sucesso.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from neuroloop.core.criteria import (
    AllOf,
    AnyOf,
    CommandExitCodeEquals,
    Criterion,
    CriterionOutcome,
    FileExists,
    FileMatchesJsonSchema,
    HttpStatusEquals,
    JsonPathCount,
    JsonPathEquals,
    ValueEquals,
    apply_negate,
    combine_all,
    combine_any,
)
from neuroloop.tools.sandbox import Sandbox, SandboxViolation

_MISSING = object()

HttpProber = Callable[[str, str], Awaitable[int]]
"""(método, url) -> status HTTP observado."""

CommandRunner = Callable[[tuple[str, ...]], Awaitable[int]]
"""(comando) -> exit code observado."""


@dataclass(slots=True)
class EvaluationContext:
    """Tudo que a avaliação pode observar. Nada além disso."""

    sandbox: Sandbox | None = None
    action_result: Any = None
    run_values: dict[str, Any] = field(default_factory=dict)
    http_prober: HttpProber | None = None
    command_runner: CommandRunner | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


class CriterionEvaluator:
    async def evaluate(self, criterion: Criterion, ctx: EvaluationContext) -> CriterionOutcome:
        outcome = await self._dispatch(criterion, ctx)
        return outcome.model_copy(
            update={"satisfied": apply_negate(outcome.satisfied, criterion.negate)}
        )

    async def evaluate_all(
        self, criteria: tuple[Criterion, ...] | list[Criterion], ctx: EvaluationContext
    ) -> list[CriterionOutcome]:
        return [await self.evaluate(c, ctx) for c in criteria]

    # ------------------------------------------------------------- dispatch

    async def _dispatch(self, criterion: Criterion, ctx: EvaluationContext) -> CriterionOutcome:
        match criterion:
            case FileExists():
                return self._file_exists(criterion, ctx)
            case FileMatchesJsonSchema():
                return self._file_matches_schema(criterion, ctx)
            case JsonPathEquals():
                return self._json_path_equals(criterion, ctx)
            case JsonPathCount():
                return self._json_path_count(criterion, ctx)
            case ValueEquals():
                return self._value_equals(criterion, ctx)
            case HttpStatusEquals():
                return await self._http_status(criterion, ctx)
            case CommandExitCodeEquals():
                return await self._command_exit_code(criterion, ctx)
            case AllOf() | AnyOf():
                return await self._composite(criterion, ctx)
            case _:  # pragma: no cover - união fechada
                return self._indeterminate(criterion, ctx, "critério desconhecido")

    # --------------------------------------------------------- filesystem

    def _file_exists(self, criterion: FileExists, ctx: EvaluationContext) -> CriterionOutcome:
        path = self._resolve_path(criterion.path, ctx)
        if isinstance(path, str):
            return self._indeterminate(criterion, ctx, path)
        return self._outcome(criterion, ctx, satisfied=path.is_file(), observed=path.exists())

    def _file_matches_schema(
        self, criterion: FileMatchesJsonSchema, ctx: EvaluationContext
    ) -> CriterionOutcome:
        document = self._load_document("FILE", criterion.path, ctx)
        if isinstance(document, str):
            return self._indeterminate(criterion, ctx, document)
        validator = Draft202012Validator(criterion.json_schema)
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        return self._outcome(
            criterion,
            ctx,
            satisfied=not errors,
            observed=errors[0].message if errors else "conforme",
        )

    # ------------------------------------------------------------- json

    def _json_path_equals(
        self, criterion: JsonPathEquals, ctx: EvaluationContext
    ) -> CriterionOutcome:
        document = self._load_document(criterion.source, criterion.path, ctx)
        if isinstance(document, str):
            return self._indeterminate(criterion, ctx, document)
        found = _select(document, criterion.json_path)
        if found is _MISSING:
            return self._indeterminate(
                criterion, ctx, f"json_path {criterion.json_path!r} não resolveu"
            )
        return self._outcome(
            criterion,
            ctx,
            satisfied=found == criterion.expected,
            observed=found,
            expected=criterion.expected,
        )

    def _json_path_count(
        self, criterion: JsonPathCount, ctx: EvaluationContext
    ) -> CriterionOutcome:
        document = self._load_document(criterion.source, criterion.path, ctx)
        if isinstance(document, str):
            return self._indeterminate(criterion, ctx, document)
        found = _select(document, criterion.json_path)
        if found is _MISSING or not isinstance(found, list):
            return self._indeterminate(
                criterion, ctx, f"json_path {criterion.json_path!r} não devolveu coleção"
            )
        return self._outcome(
            criterion,
            ctx,
            satisfied=len(found) == criterion.expected_count,
            observed=len(found),
            expected=criterion.expected_count,
        )

    def _value_equals(self, criterion: ValueEquals, ctx: EvaluationContext) -> CriterionOutcome:
        found = ctx.run_values.get(criterion.ref, _MISSING)
        if found is _MISSING:
            return self._indeterminate(criterion, ctx, f"ref {criterion.ref!r} ausente")
        return self._outcome(
            criterion,
            ctx,
            satisfied=found == criterion.expected,
            observed=found,
            expected=criterion.expected,
        )

    # ---------------------------------------------------------- externos

    async def _http_status(
        self, criterion: HttpStatusEquals, ctx: EvaluationContext
    ) -> CriterionOutcome:
        if ctx.http_prober is None:
            return self._indeterminate(criterion, ctx, "sem sonda HTTP no contexto")
        try:
            status = await ctx.http_prober(criterion.method, criterion.url)
        except Exception as error:  # noqa: BLE001 - sonda externa é hostil por natureza
            return self._indeterminate(criterion, ctx, f"sonda HTTP falhou: {error}")
        return self._outcome(
            criterion,
            ctx,
            satisfied=status == criterion.expected_status,
            observed=status,
            expected=criterion.expected_status,
        )

    async def _command_exit_code(
        self, criterion: CommandExitCodeEquals, ctx: EvaluationContext
    ) -> CriterionOutcome:
        if ctx.command_runner is None:
            return self._indeterminate(criterion, ctx, "sem executor de comando no contexto")
        try:
            code = await ctx.command_runner(criterion.command)
        except Exception as error:  # noqa: BLE001
            return self._indeterminate(criterion, ctx, f"comando falhou: {error}")
        return self._outcome(
            criterion,
            ctx,
            satisfied=code == criterion.expected_exit_code,
            observed=code,
            expected=criterion.expected_exit_code,
        )

    # ------------------------------------------------------------ composto

    async def _composite(
        self, criterion: AllOf | AnyOf, ctx: EvaluationContext
    ) -> CriterionOutcome:
        children = [await self.evaluate(child, ctx) for child in criterion.criteria]
        values = [c.satisfied for c in children]
        combined = combine_all(values) if isinstance(criterion, AllOf) else combine_any(values)
        return self._outcome(
            criterion, ctx, satisfied=combined, observed=[c.satisfied for c in children]
        )

    # --------------------------------------------------------------- apoio

    def _resolve_path(self, path: str, ctx: EvaluationContext):
        if ctx.sandbox is None:
            return "sem sandbox no contexto de avaliação"
        try:
            return ctx.sandbox.resolve(path)
        except SandboxViolation:
            return f"caminho {path!r} fora do sandbox"

    def _load_document(self, source: str, path: str | None, ctx: EvaluationContext):
        if source == "ACTION_RESULT":
            if ctx.action_result is None:
                return "sem resultado de ação no contexto"
            return ctx.action_result
        if not path:
            return "critério sobre arquivo sem path"
        resolved = self._resolve_path(path, ctx)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"arquivo {path!r} não existe"
        try:
            return json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return f"arquivo {path!r} ilegível como JSON: {error}"

    def _outcome(
        self,
        criterion: Criterion,
        ctx: EvaluationContext,
        *,
        satisfied: bool | None,
        observed: Any = None,
        expected: Any = None,
    ) -> CriterionOutcome:
        return CriterionOutcome(
            criterion_kind=criterion.kind,
            satisfied=satisfied,
            observed=observed,
            expected=expected,
            observed_at=ctx.now,
        )

    def _indeterminate(
        self, criterion: Criterion, ctx: EvaluationContext, reason: str
    ) -> CriterionOutcome:
        return CriterionOutcome(
            criterion_kind=criterion.kind,
            satisfied=None,
            error=reason,
            observed_at=ctx.now,
        )


def _select(document: Any, json_path: str) -> Any:
    """Subconjunto de JSONPath suficiente para a V0.

    Suporta `$`, `$.chave`, `$.a.b`, `$[0]`, `$[*]` e `$.itens[*]`. Um
    dialeto completo entraria como dependência; até aqui não se paga por ele.
    """
    if not json_path.startswith("$"):
        return _MISSING
    current = document
    for token in _tokenize(json_path[1:]):
        if token == "*":
            if not isinstance(current, list):
                return _MISSING
            continue
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return _MISSING
            current = current[token]
            continue
        if not isinstance(current, dict) or token not in current:
            return _MISSING
        current = current[token]
    return current


def _tokenize(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    buffer = ""
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            if buffer:
                tokens.append(buffer)
                buffer = ""
        elif char == "[":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            close = path.index("]", index)
            inner = path[index + 1 : close]
            tokens.append("*" if inner == "*" else int(inner))
            index = close
        else:
            buffer += char
        index += 1
    if buffer:
        tokens.append(buffer)
    return tokens
