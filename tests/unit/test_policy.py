"""TASK-005. Aceite: R0/R1/R2/R3 e sandbox enforced; C10: taint; C19: fingerprint."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from factories import NOW, make_checkpoint
from neuroloop.core import (
    ActionProposal,
    ErrorCode,
    FileExists,
    RiskLevel,
    TrustLevel,
)
from neuroloop.core.identity import make_action_fingerprint
from neuroloop.security import (
    GateType,
    PolicyConfig,
    PolicyEngine,
    TaintContext,
    default_policy,
)
from neuroloop.tools import EffectProbe, Sandbox, ToolDefinition

PROBE = EffectProbe(
    criterion_template=FileExists(path="{path}"),
    argument_bindings={"path": "path"},
)


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    (tmp_path / "workspace").mkdir()
    return Sandbox(tmp_path / "workspace")


@pytest.fixture
def policy(sandbox) -> PolicyEngine:
    return default_policy(sandbox)


def tool(risk: RiskLevel, **overrides) -> ToolDefinition:
    side_effects = overrides.pop("side_effects", risk >= RiskLevel.R1)
    defaults = dict(
        name=f"tool.{risk.value.lower()}",
        version="1.0.0",
        description="tool de teste",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk_level=risk,
        side_effects=side_effects,
        reversible=not side_effects,
        requires_confirmation=risk >= RiskLevel.R2,
        supports_idempotency=side_effects,
        timeout_seconds=5.0,
        capabilities=frozenset({"fs:write"} if side_effects else {"fs:read"}),
        effect_probe=PROBE if side_effects else None,
    )
    defaults.update(overrides)
    return ToolDefinition(**defaults)


def proposal(**overrides) -> ActionProposal:
    defaults = dict(
        tool="tool.r1",
        arguments={"path": "out.json"},
        expected_outcomes=(FileExists(path="out.json"),),
        rationale_code="WRITE",
    )
    defaults.update(overrides)
    return ActionProposal(**defaults)


class TestGates:
    """Decididos por regra, antes de qualquer LLM (spec §11)."""

    def test_run_saudavel_prossegue(self, policy):
        gate = policy.pre_decision(make_checkpoint(), now=NOW)
        assert gate.type is GateType.PROCEED
        assert gate.proceeds is True

    def test_cancelamento_para_o_run(self, policy):
        gate = policy.pre_decision(make_checkpoint(cancel_requested=True), now=NOW)
        assert gate.type is GateType.STOP
        assert gate.error_code is ErrorCode.CANCELLED

    def test_budget_estourado_para_o_run(self, policy):
        gate = policy.pre_decision(make_checkpoint(iteration=30), now=NOW)
        assert gate.type is GateType.STOP
        assert gate.error_code is ErrorCode.BUDGET_EXCEEDED

    def test_deadline_estourado_para_o_run(self, policy):
        gate = policy.pre_decision(make_checkpoint(), now=NOW + timedelta(seconds=901))
        assert gate.error_code is ErrorCode.BUDGET_EXCEEDED

    def test_efeito_nao_resolvido_manda_recuperar(self, policy):
        gate = policy.pre_decision(
            make_checkpoint(unresolved_effect_action_id=uuid4()), now=NOW
        )
        assert gate.type is GateType.RECOVER

    def test_aprovacao_pendente_espera_usuario(self, policy):
        gate = policy.pre_decision(
            make_checkpoint(pending_approval_action_id=uuid4()), now=NOW
        )
        assert gate.type is GateType.WAIT_USER

    def test_efeito_nao_resolvido_tem_precedencia_sobre_aprovacao(self, policy):
        """Não se pede autorização do próximo passo sem saber do anterior."""
        gate = policy.pre_decision(
            make_checkpoint(
                unresolved_effect_action_id=uuid4(),
                pending_approval_action_id=uuid4(),
            ),
            now=NOW,
        )
        assert gate.type is GateType.RECOVER

    def test_cancelamento_tem_precedencia_sobre_tudo(self, policy):
        gate = policy.pre_decision(
            make_checkpoint(cancel_requested=True, unresolved_effect_action_id=uuid4()),
            now=NOW,
        )
        assert gate.type is GateType.STOP


class TestTiersDeRisco:
    """V0: R0 automático, R1 automático em sandbox, R2 aprovação, R3 bloqueado."""

    def test_r0_e_automatico(self, policy):
        auth = policy.authorize(proposal(), tool(RiskLevel.R0))
        assert auth.executable is True
        assert auth.reason_code == "AUTO_APPROVED:R0"

    def test_r1_e_automatico_no_sandbox(self, policy):
        auth = policy.authorize(proposal(), tool(RiskLevel.R1))
        assert auth.executable is True

    def test_r2_exige_aprovacao(self, policy):
        auth = policy.authorize(proposal(), tool(RiskLevel.R2))
        assert auth.allowed is True
        assert auth.requires_user_approval is True
        assert auth.executable is False
        assert auth.reason_code == "RISK_NEEDS_APPROVAL:R2"

    def test_r3_e_bloqueado_sem_via_de_aprovacao(self, policy):
        auth = policy.authorize(proposal(), tool(RiskLevel.R3))
        assert auth.allowed is False
        assert auth.requires_user_approval is False
        assert auth.error_code is ErrorCode.PERMISSION_DENIED


class TestSandbox:
    def test_caminho_fora_do_sandbox_e_recusado(self, policy, tmp_path):
        auth = policy.authorize(
            proposal(arguments={"path": str(tmp_path / "fora.json")}), tool(RiskLevel.R1)
        )
        assert auth.allowed is False
        assert auth.reason_code == "RESOURCE_OUT_OF_SANDBOX:path"

    def test_traversal_e_recusado(self, policy):
        auth = policy.authorize(
            proposal(arguments={"path": "../../etc/passwd"}), tool(RiskLevel.R1)
        )
        assert auth.allowed is False
        assert auth.error_code is ErrorCode.PERMISSION_DENIED

    def test_sem_sandbox_caminho_e_recusado(self):
        """Ausência de sandbox nega, não libera."""
        engine = default_policy(None)
        auth = engine.authorize(proposal(), tool(RiskLevel.R1))
        assert auth.allowed is False
        assert auth.reason_code == "NO_SANDBOX_FOR_PATH:path"

    def test_esquema_de_url_nao_suportado(self, policy):
        auth = policy.authorize(
            proposal(arguments={"url": "file:///etc/passwd"}),
            tool(RiskLevel.R1, capabilities=frozenset({"fs:write"})),
        )
        assert auth.allowed is False
        assert auth.reason_code == "UNSUPPORTED_URL_SCHEME:url"


class TestCapabilities:
    def test_capability_ausente_e_recusada(self, sandbox):
        engine = default_policy(sandbox, granted_capabilities=frozenset({"fs:read"}))
        auth = engine.authorize(proposal(), tool(RiskLevel.R1))
        assert auth.allowed is False
        assert auth.reason_code == "MISSING_CAPABILITIES:fs:write"

    def test_capability_concedida_passa(self, policy):
        assert policy.authorize(proposal(), tool(RiskLevel.R1)).allowed is True

    def test_capability_e_checada_antes_do_risco(self, sandbox):
        """Falta de capacidade é o motivo mais específico; reporta esse."""
        engine = default_policy(sandbox, granted_capabilities=frozenset())
        auth = engine.authorize(proposal(), tool(RiskLevel.R3))
        assert auth.reason_code.startswith("MISSING_CAPABILITIES")


class TestTaint:
    """Correção C10 — o mecanismo por trás do benchmark B4."""

    def _taint(self, trust: TrustLevel):
        observation_id = uuid4()
        return observation_id, TaintContext(trust_by_observation={observation_id: trust})

    def test_origem_confiavel_nao_altera_nada(self, policy):
        oid, taint = self._taint(TrustLevel.USER)
        auth = policy.authorize(
            proposal(derived_from=(oid,)), tool(RiskLevel.R1), taint=taint
        )
        assert auth.tainted is False
        assert auth.executable is True

    def test_conteudo_externo_em_r1_exige_aprovacao(self, policy):
        """Instrução vinda de arquivo não vale como autorização do usuário."""
        oid, taint = self._taint(TrustLevel.UNTRUSTED_EXTERNAL)
        auth = policy.authorize(
            proposal(derived_from=(oid,)), tool(RiskLevel.R1), taint=taint
        )
        assert auth.tainted is True
        assert auth.requires_user_approval is True
        assert auth.reason_code == "UNTRUSTED_ORIGIN_NEEDS_APPROVAL:R1"

    def test_conteudo_externo_em_r2_e_bloqueado(self, policy):
        oid, taint = self._taint(TrustLevel.UNTRUSTED_EXTERNAL)
        auth = policy.authorize(
            proposal(derived_from=(oid,)), tool(RiskLevel.R2), taint=taint
        )
        assert auth.allowed is False
        assert auth.error_code is ErrorCode.PROMPT_INJECTION

    def test_conteudo_externo_em_r0_segue_automatico(self, policy):
        """Leitura não muda o mundo; taint aqui não muda a decisão."""
        oid, taint = self._taint(TrustLevel.UNTRUSTED_EXTERNAL)
        auth = policy.authorize(
            proposal(derived_from=(oid,)), tool(RiskLevel.R0), taint=taint
        )
        assert auth.executable is True

    def test_proveniencia_desconhecida_e_tratada_como_nao_confiavel(self, policy):
        """Proveniência que não se pode auditar não vale como garantia."""
        auth = policy.authorize(
            proposal(derived_from=(uuid4(),)), tool(RiskLevel.R1), taint=TaintContext()
        )
        assert auth.tainted is True
        assert auth.requires_user_approval is True

    def test_pior_confianca_domina(self, policy):
        confiavel, externa = uuid4(), uuid4()
        taint = TaintContext(
            trust_by_observation={
                confiavel: TrustLevel.TRUSTED_INTERNAL,
                externa: TrustLevel.UNTRUSTED_EXTERNAL,
            }
        )
        auth = policy.authorize(
            proposal(derived_from=(confiavel, externa)), tool(RiskLevel.R1), taint=taint
        )
        assert auth.tainted is True


class TestAprovacaoVinculada:
    """Correção C19 — aprovação vale para argumentos, não para a ação lógica."""

    def _fingerprint(self, definition, arguments) -> str:
        return make_action_fingerprint(
            tool=definition.name,
            tool_version=definition.version,
            arguments=arguments,
            target_resource=None,
        )

    def test_aprovacao_do_mesmo_fingerprint_libera(self, policy):
        definition = tool(RiskLevel.R2)
        args = {"path": "out.json"}
        approved = self._fingerprint(definition, args)

        auth = policy.authorize(
            proposal(arguments=args), definition, approved_fingerprints=frozenset({approved})
        )
        assert auth.executable is True
        assert auth.reason_code == "USER_APPROVED"

    def test_argumentos_trocados_invalidam_a_aprovacao(self, policy):
        """Confused deputy: aprovar 'a.json' não aprova 'b.json'."""
        definition = tool(RiskLevel.R2)
        approved = self._fingerprint(definition, {"path": "a.json"})

        auth = policy.authorize(
            proposal(arguments={"path": "b.json"}), definition, approved_fingerprints=frozenset({approved})
        )
        assert auth.requires_user_approval is True
        assert auth.reason_code != "USER_APPROVED"

    def test_fingerprint_e_devolvido_para_registro(self, policy):
        auth = policy.authorize(proposal(), tool(RiskLevel.R2))
        assert auth.action_fingerprint is not None
        assert auth.action_fingerprint.startswith("sha256:")

    def test_aprovacao_nao_ressuscita_acao_bloqueada(self, policy):
        """R3 não tem via de aprovação: nem o usuário destrava."""
        definition = tool(RiskLevel.R3)
        approved = self._fingerprint(definition, {"path": "out.json"})
        auth = policy.authorize(proposal(), definition, approved_fingerprints=frozenset({approved}))
        assert auth.allowed is False


class TestConfiguracao:
    def test_politica_pode_ser_endurecida(self, sandbox):
        """Um ambiente mais estrito bloqueia a partir de R2."""
        engine = PolicyEngine(
            PolicyConfig(
                granted_capabilities=frozenset({"fs:read", "fs:write"}),
                sandbox=sandbox,
                blocked_min_risk=RiskLevel.R2,
            )
        )
        assert engine.authorize(proposal(), tool(RiskLevel.R2)).allowed is False
        assert engine.authorize(proposal(), tool(RiskLevel.R1)).executable is True
