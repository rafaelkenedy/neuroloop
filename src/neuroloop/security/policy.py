"""Policy Engine — TASK-005 (spec §22, correções C10 e C19).

Duas responsabilidades, deliberadamente separadas:

`pre_decision`
    Gates determinísticos que rodam **antes** de qualquer LLM (spec §11).
    Cancelamento, budget, aprovação pendente e efeito não resolvido são
    decididos por regra, nunca por prompt.

`authorize`
    Autoriza uma ação concreta. Aqui mora a regra que a spec enunciava como
    princípio sem mecanismo: *conteúdo externo é sempre dado, nunca
    instrução*. Uma ação cujos argumentos vieram de conteúdo
    `UNTRUSTED_EXTERNAL` não adquire a autoridade que teria se viesse do
    usuário.

Cadeia de autoridade (spec §22):

    SYSTEM_POLICY > USER_GOAL > USER_APPROVAL > INTERNAL_STATE
                  > TOOL_OUTPUT > EXTERNAL_CONTENT

O LLM não aparece nessa cadeia: `LLM não autoriza`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from neuroloop.core.actions import ActionProposal
from neuroloop.core.enums import ErrorCode, RiskLevel, TrustLevel
from neuroloop.core.identity import make_action_fingerprint
from neuroloop.core.runs import RunCheckpoint
from neuroloop.tools.definitions import ToolDefinition
from neuroloop.tools.sandbox import Sandbox, SandboxViolation

_PATH_ARGUMENT_KEYS = ("path", "destination", "target", "file")
_URL_ARGUMENT_KEYS = ("url", "endpoint", "uri")


class GateType(str, Enum):
    PROCEED = "PROCEED"
    STOP = "STOP"
    WAIT_USER = "WAIT_USER"
    RECOVER = "RECOVER"


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: GateType
    reason_code: str
    error_code: ErrorCode | None = None

    @property
    def proceeds(self) -> bool:
        return self.type is GateType.PROCEED


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    requires_user_approval: bool = False
    risk_level: RiskLevel
    reason_code: str
    error_code: ErrorCode | None = None
    tainted: bool = False
    """Algum argumento derivou de conteúdo `UNTRUSTED_EXTERNAL`."""
    action_fingerprint: str | None = None
    """Vincula uma eventual aprovação a estes argumentos exatos (C19)."""

    @property
    def executable(self) -> bool:
        return self.allowed and not self.requires_user_approval


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Política da V0 (spec §22).

    R0 automático; R1 automático dentro do sandbox; R2 exige aprovação; R3
    bloqueado.
    """

    granted_capabilities: frozenset[str]
    sandbox: Sandbox | None = None
    auto_approve_max_risk: RiskLevel = RiskLevel.R1
    blocked_min_risk: RiskLevel = RiskLevel.R3
    tainted_blocked_min_risk: RiskLevel = RiskLevel.R2
    """Acima disso, argumento derivado de conteúdo externo é recusado."""
    tainted_approval_min_risk: RiskLevel = RiskLevel.R1
    """A partir daí, taint exige confirmação humana."""


@dataclass(slots=True)
class TaintContext:
    """Confiança conhecida de cada observação citada em `derived_from`."""

    trust_by_observation: dict[UUID, TrustLevel] = field(default_factory=dict)

    def worst_trust(self, observation_ids: tuple[UUID, ...]) -> TrustLevel | None:
        """Pior confiança entre as observações que originaram a ação.

        Uma observação citada e desconhecida é tratada como não confiável:
        proveniência que não se pode auditar não vale como garantia.
        """
        if not observation_ids:
            return None
        levels = [
            self.trust_by_observation.get(oid, TrustLevel.UNTRUSTED_EXTERNAL)
            for oid in observation_ids
        ]
        for level in (TrustLevel.UNTRUSTED_EXTERNAL, TrustLevel.USER):
            if level in levels:
                return level
        return TrustLevel.TRUSTED_INTERNAL


class PolicyEngine:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ gates

    def pre_decision(self, checkpoint: RunCheckpoint, *, now) -> Gate:
        """Ordem importa: o mais terminal primeiro (spec §11)."""
        if checkpoint.cancel_requested:
            return Gate(
                type=GateType.STOP, reason_code="CANCEL_REQUESTED",
                error_code=ErrorCode.CANCELLED,
            )
        if checkpoint.budget_exhausted(now):
            return Gate(
                type=GateType.STOP, reason_code="BUDGET_EXCEEDED",
                error_code=ErrorCode.BUDGET_EXCEEDED,
            )
        # Efeito não resolvido tem precedência sobre a aprovação pendente:
        # não se pede autorização para o próximo passo sem saber se o
        # anterior surtiu efeito.
        if checkpoint.unresolved_effect_action_id is not None:
            return Gate(
                type=GateType.RECOVER, reason_code="UNRESOLVED_SIDE_EFFECT",
                error_code=ErrorCode.UNKNOWN_SIDE_EFFECT,
            )
        if checkpoint.pending_approval_action_id is not None:
            return Gate(type=GateType.WAIT_USER, reason_code="PENDING_APPROVAL")
        return Gate(type=GateType.PROCEED, reason_code="OK")

    # ------------------------------------------------------- autorização

    def authorize(
        self,
        proposal: ActionProposal,
        definition: ToolDefinition,
        *,
        taint: TaintContext | None = None,
        approved_fingerprints: frozenset[str] = frozenset(),
        target_resource: str | None = None,
    ) -> Authorization:
        risk = definition.risk_level
        fingerprint = make_action_fingerprint(
            tool=definition.name,
            tool_version=definition.version,
            arguments=proposal.arguments,
            target_resource=target_resource,
        )

        def deny(reason: str, code: ErrorCode, *, tainted: bool = False) -> Authorization:
            return Authorization(
                allowed=False,
                risk_level=risk,
                reason_code=reason,
                error_code=code,
                tainted=tainted,
                action_fingerprint=fingerprint,
            )

        # 1. Capabilities: o agente precisa deter o que a tool exige.
        missing = definition.capabilities - self.config.granted_capabilities
        if missing:
            return deny(
                f"MISSING_CAPABILITIES:{','.join(sorted(missing))}",
                ErrorCode.PERMISSION_DENIED,
            )

        # 2. Teto de risco. R3 é bloqueado na V0, sem via de aprovação.
        if risk >= self.config.blocked_min_risk:
            return deny(f"RISK_BLOCKED:{risk.value}", ErrorCode.PERMISSION_DENIED)

        # 3. Recursos. Resolução canônica antes de qualquer decisão (C10):
        #    um caminho com `..` ou symlink precisa ser resolvido para ser
        #    comparado com a allowlist.
        violation = self._check_resources(proposal.arguments)
        if violation is not None:
            return deny(violation, ErrorCode.PERMISSION_DENIED)

        # 4. Taint. Conteúdo externo não empresta autoridade à ação.
        taint = taint or TaintContext()
        trust = taint.worst_trust(proposal.derived_from)
        tainted = trust is TrustLevel.UNTRUSTED_EXTERNAL

        if tainted and risk >= self.config.tainted_blocked_min_risk:
            return deny(
                f"UNTRUSTED_ORIGIN_RISK:{risk.value}",
                ErrorCode.PROMPT_INJECTION,
                tainted=True,
            )

        needs_approval = risk > self.config.auto_approve_max_risk or (
            tainted and risk >= self.config.tainted_approval_min_risk
        )

        # 5. Aprovação existente só vale para estes argumentos (C19).
        if needs_approval and fingerprint in approved_fingerprints:
            return Authorization(
                allowed=True,
                requires_user_approval=False,
                risk_level=risk,
                reason_code="USER_APPROVED",
                tainted=tainted,
                action_fingerprint=fingerprint,
            )

        return Authorization(
            allowed=True,
            requires_user_approval=needs_approval,
            risk_level=risk,
            reason_code=_approval_reason(needs_approval, tainted, risk),
            tainted=tainted,
            action_fingerprint=fingerprint,
        )

    # ---------------------------------------------------------- recursos

    def _check_resources(self, arguments: dict[str, Any]) -> str | None:
        sandbox = self.config.sandbox
        for key, value in arguments.items():
            if not isinstance(value, str):
                continue
            if key in _PATH_ARGUMENT_KEYS:
                if sandbox is None:
                    return f"NO_SANDBOX_FOR_PATH:{key}"
                try:
                    sandbox.resolve(value)
                except SandboxViolation:
                    return f"RESOURCE_OUT_OF_SANDBOX:{key}"
            elif key in _URL_ARGUMENT_KEYS and not value.startswith(("http://", "https://")):
                return f"UNSUPPORTED_URL_SCHEME:{key}"
        return None


def _approval_reason(needs_approval: bool, tainted: bool, risk: RiskLevel) -> str:
    if not needs_approval:
        return f"AUTO_APPROVED:{risk.value}"
    if tainted:
        return f"UNTRUSTED_ORIGIN_NEEDS_APPROVAL:{risk.value}"
    return f"RISK_NEEDS_APPROVAL:{risk.value}"


class PolicyDenied(RuntimeError):
    """Ação recusada pela política. Não é falha de execução: nada foi tentado."""

    def __init__(self, authorization: Authorization) -> None:
        self.authorization = authorization
        self.error_code = authorization.error_code or ErrorCode.PERMISSION_DENIED
        super().__init__(f"{self.error_code.value}: {authorization.reason_code}")


DEFAULT_CAPABILITIES: frozenset[str] = frozenset({"fs:read", "fs:write"})
"""Capacidades concedidas por padrão na V0. `http:*` e `shell:*` ficam fora."""


def default_policy(sandbox: Sandbox | None = None, **overrides) -> PolicyEngine:
    config = PolicyConfig(
        granted_capabilities=overrides.pop("granted_capabilities", DEFAULT_CAPABILITIES),
        sandbox=sandbox,
        **overrides,
    )
    return PolicyEngine(config)


__all__ = [
    "Authorization",
    "DEFAULT_CAPABILITIES",
    "Gate",
    "GateType",
    "PolicyConfig",
    "PolicyDenied",
    "PolicyEngine",
    "TaintContext",
    "default_policy",
]
