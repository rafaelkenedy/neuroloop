"""Segurança: policy, permissões e limites de autoridade."""

from neuroloop.security.policy import (
    DEFAULT_CAPABILITIES,
    Authorization,
    Gate,
    GateType,
    PolicyConfig,
    PolicyDenied,
    PolicyEngine,
    TaintContext,
    default_policy,
)

__all__ = [
    "DEFAULT_CAPABILITIES",
    "Authorization",
    "Gate",
    "GateType",
    "PolicyConfig",
    "PolicyDenied",
    "PolicyEngine",
    "TaintContext",
    "default_policy",
]
