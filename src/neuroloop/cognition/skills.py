"""Memória procedural — TASK-011 (spec §18).

V0: skills são **cadastradas à mão** e versionadas. Não há síntese
automática — isso é V1, e a spec é explícita sobre não implementar
aprendizagem autônoma antes de medir o resto.

O que existe aqui é o mecanismo de desconfiança: uma skill deixa de ser
elegível sozinha quando a taxa de sucesso cai, quando houve falha recente,
ou quando a versão da tool que ela cita mudou. Skill obsoleta que continua
disparando é o modo de falha do Fast Path (spec §37).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neuroloop.core.actions import ActionProposal
from neuroloop.core.criteria import Criterion
from neuroloop.core.templating import MissingPlaceholder, placeholders, substitute
from neuroloop.tools.registry import ToolNotFoundError, ToolRegistry

MIN_SUCCESS_RATE = 0.8
"""Abaixo disso a skill sai do Fast Path e volta a passar pelo Deliberator."""

MIN_SAMPLES_FOR_RATE = 5
"""Com poucas execuções a taxa é ruído; só se desconfia com amostra."""


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1)

    trigger_tags: frozenset[str] = Field(min_length=1)
    required_inputs: tuple[str, ...] = ()
    preconditions: tuple[Criterion, ...] = ()
    action_template: ActionProposal
    success_criteria: tuple[Criterion, ...] = Field(min_length=1)
    tool_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    """Versão da tool contra a qual a skill foi escrita e validada."""

    enabled: bool = True
    usage_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _template_matches_inputs(self) -> SkillDefinition:
        if self.success_count > self.usage_count:
            raise ValueError("success_count não pode exceder usage_count")
        citados = placeholders(self.action_template.arguments)
        faltando = citados - set(self.required_inputs)
        if faltando:
            raise ValueError(
                f"skill {self.id!r}: gabarito cita {sorted(faltando)} "
                "fora de required_inputs"
            )
        return self

    @property
    def success_rate(self) -> float:
        if self.usage_count == 0:
            return 1.0
        return self.success_count / self.usage_count

    @property
    def has_enough_samples(self) -> bool:
        return self.usage_count >= MIN_SAMPLES_FOR_RATE

    def usability(self, registry: ToolRegistry) -> tuple[bool, str]:
        """Elegibilidade estrutural, antes de qualquer score.

        Devolve `(usável, motivo)`; o motivo entra no trace mesmo quando a
        skill é aceita, para responder "por que o Fast Path disparou".
        """
        if not self.enabled:
            return False, self.disabled_reason or "SKILL_DISABLED"
        if self.consecutive_failures > 0:
            return False, "RECENT_FAILURE"
        if self.has_enough_samples and self.success_rate < MIN_SUCCESS_RATE:
            return False, f"LOW_SUCCESS_RATE:{self.success_rate:.2f}"
        try:
            definition = registry.get(self.action_template.tool)
        except ToolNotFoundError:
            return False, "TOOL_MISSING"
        if definition.definition.version != self.tool_version:
            # Skill obsoleta: o contrato mudou embaixo dela.
            return False, (
                f"TOOL_VERSION_DRIFT:{self.tool_version}->{definition.definition.version}"
            )
        return True, "USABLE"

    def materialize(self, inputs: dict[str, object]) -> ActionProposal:
        faltando = [name for name in self.required_inputs if name not in inputs]
        if faltando:
            raise MissingPlaceholder(faltando[0])
        return self.action_template.model_copy(
            update={
                "arguments": substitute(self.action_template.arguments, dict(inputs)),
                "rationale_code": f"SKILL:{self.id}@{self.version}",
            }
        )

    def with_outcome(self, *, succeeded: bool) -> SkillDefinition:
        """Estatística atualizada, com auto-desabilitação quando cabe."""
        usage = self.usage_count + 1
        success = self.success_count + (1 if succeeded else 0)
        failures = 0 if succeeded else self.consecutive_failures + 1
        rate = success / usage
        desabilitar = usage >= MIN_SAMPLES_FOR_RATE and rate < MIN_SUCCESS_RATE
        return self.model_copy(
            update={
                "usage_count": usage,
                "success_count": success,
                "consecutive_failures": failures,
                "enabled": self.enabled and not desabilitar,
                "disabled_reason": (
                    f"LOW_SUCCESS_RATE:{rate:.2f}" if desabilitar else self.disabled_reason
                ),
            }
        )


class SkillRegistry:
    """Catálogo em memória. A persistência é a tabela `skills` (TASK-003)."""

    def __init__(self) -> None:
        self._skills: dict[tuple[str, str], SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        key = (skill.id, skill.version)
        if key in self._skills:
            raise ValueError(f"skill {skill.id}@{skill.version} já registrada")
        self._skills[key] = skill

    def replace(self, skill: SkillDefinition) -> None:
        self._skills[(skill.id, skill.version)] = skill

    def get(self, skill_id: str, version: str) -> SkillDefinition:
        return self._skills[(skill_id, version)]

    def all(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills.values())

    def usable(self, registry: ToolRegistry) -> tuple[SkillDefinition, ...]:
        return tuple(s for s in self._skills.values() if s.usability(registry)[0])

    def __len__(self) -> int:
        return len(self._skills)
