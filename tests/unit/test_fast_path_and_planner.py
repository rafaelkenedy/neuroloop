"""TASK-011. Scoring de C13, auto-desconfiança de skill e portas do planner."""

from __future__ import annotations

from uuid import uuid4

import pytest

from factories import NOW, make_checkpoint, make_goal
from neuroloop.cognition.fast_path import (
    SKILL_SCORE_THRESHOLD,
    FastPath,
    FastPathSource,
    next_ready_step,
    skill_score,
)
from neuroloop.cognition.planner import PlannerValidator, PlanValidationError
from neuroloop.cognition.skills import SkillDefinition, SkillRegistry
from neuroloop.context import ContextBudget, WorkspaceBuilder
from neuroloop.core import (
    ActionProposal,
    FileExists,
    JsonPathEquals,
    Observation,
    ObservationSource,
    Plan,
    PlanStep,
    PlanStepStatus,
    RiskLevel,
    TrustLevel,
)
from neuroloop.tools import Sandbox, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools
from neuroloop.verification import EvaluationContext


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "in.json").write_text("[]", encoding="utf-8")
    return Sandbox(root)


@pytest.fixture
def registry(sandbox) -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg, sandbox)
    return reg


def skill(**overrides) -> SkillDefinition:
    defaults = dict(
        id="gravar-artefato",
        version="1.0.0",
        description="grava o artefato padrão",
        trigger_tags=frozenset({"resource:out.json", "tool:filesystem.write"}),
        required_inputs=("path", "content"),
        action_template=ActionProposal(
            tool="filesystem.write",
            arguments={"path": "{path}", "content": "{content}"},
            expected_outcomes=(FileExists(path="out.json"),),
            rationale_code="SKILL",
        ),
        success_criteria=(FileExists(path="out.json"),),
        tool_version="1.0.0",
    )
    defaults.update(overrides)
    return SkillDefinition(**defaults)


def observation(tags: tuple[str, ...]) -> Observation:
    return Observation(
        id=uuid4(),
        run_id=uuid4(),
        source=ObservationSource.TOOL,
        kind="file_content",
        content="x",
        content_hash=f"sha256:{uuid4().hex[:8]}",
        trust=TrustLevel.TRUSTED_INTERNAL,
        tags=tags,
        occurred_at=NOW,
        received_at=NOW,
    )


def context(**overrides):
    defaults = dict(
        goal=make_goal(success_criteria=(FileExists(path="out.json"),)),
        checkpoint=make_checkpoint(),
        now=NOW,
    )
    defaults.update(overrides)
    return WorkspaceBuilder(ContextBudget()).build(**defaults)


def plan(*steps: PlanStep) -> Plan:
    return Plan(
        id=uuid4(),
        version=1,
        objective="obj",
        completion_condition="ok",
        steps=steps,
    )


def step(sid: str, **overrides) -> PlanStep:
    defaults = dict(
        id=sid,
        description=f"step {sid}",
        preferred_tool="filesystem.read",
        arguments={"path": "in.json"},
        expected_outcomes=(FileExists(path="in.json"),),
    )
    defaults.update(overrides)
    return PlanStep(**defaults)


class TestScoringDeSkill:
    """C13: o limiar de 0.95 da spec era inalcançável sem embeddings."""

    def test_match_perfeito(self):
        s = skill()
        score = skill_score(s.trigger_tags, s, resolved=2, required=2)
        assert score == pytest.approx(1.0)

    def test_limiar_efetivo_e_alcancavel(self):
        """Com inputs completos, 0.75 equivale a jaccard >= 0.58."""
        s = skill()
        # 2 de 3 tags em comum → jaccard = 2/3 ≈ 0.667
        tags = frozenset({"resource:out.json", "tool:filesystem.write", "extra:coisa"})
        score = skill_score(tags, s, resolved=2, required=2)
        assert score >= SKILL_SCORE_THRESHOLD
        assert score == pytest.approx(0.60 * (2 / 3) + 0.40)

    def test_tags_distantes_ficam_abaixo_do_limiar(self):
        s = skill()
        score = skill_score(frozenset({"resource:outro.json"}), s, resolved=2, required=2)
        assert score < SKILL_SCORE_THRESHOLD

    def test_inputs_faltando_derrubam_o_score(self):
        s = skill()
        assert skill_score(s.trigger_tags, s, resolved=1, required=2) == pytest.approx(0.8)
        assert skill_score(s.trigger_tags, s, resolved=0, required=2) == pytest.approx(0.6)


class TestDesconfiancaDeSkill:
    """Spec §37: skill obsoleta que continua disparando é o modo de falha."""

    def test_skill_saudavel_e_usavel(self, registry):
        assert skill().usability(registry)[0] is True

    def test_versao_de_tool_diferente_desabilita(self, registry):
        usable, reason = skill(tool_version="0.9.0").usability(registry)
        assert usable is False
        assert "TOOL_VERSION_DRIFT" in reason

    def test_tool_ausente_desabilita(self, registry):
        s = skill(
            action_template=ActionProposal(
                tool="database.drop",
                arguments={},
                expected_outcomes=(FileExists(path="out.json"),),
                rationale_code="X",
            ),
            required_inputs=(),
        )
        assert s.usability(registry) == (False, "TOOL_MISSING")

    def test_falha_recente_desabilita(self, registry):
        assert skill(consecutive_failures=1).usability(registry) == (False, "RECENT_FAILURE")

    def test_taxa_baixa_com_amostra_desabilita(self, registry):
        usable, reason = skill(usage_count=10, success_count=5).usability(registry)
        assert usable is False
        assert "LOW_SUCCESS_RATE" in reason

    def test_taxa_baixa_sem_amostra_nao_desabilita(self, registry):
        """Com poucas execuções a taxa é ruído."""
        assert skill(usage_count=2, success_count=1).usability(registry)[0] is True

    def test_estatistica_desabilita_sozinha(self, registry):
        s = skill()
        for _ in range(5):
            s = s.with_outcome(succeeded=False)
        assert s.enabled is False
        assert "LOW_SUCCESS_RATE" in s.disabled_reason

    def test_sucesso_zera_falhas_consecutivas(self):
        s = skill(consecutive_failures=3).with_outcome(succeeded=True)
        assert s.consecutive_failures == 0

    def test_gabarito_nao_pode_citar_input_nao_declarado(self):
        with pytest.raises(ValueError, match="fora de required_inputs"):
            skill(required_inputs=("path",))

    def test_materializacao_preserva_tipo(self):
        s = skill(
            required_inputs=("path", "content"),
            action_template=ActionProposal(
                tool="filesystem.write",
                arguments={"path": "{path}", "content": "{content}"},
                expected_outcomes=(FileExists(path="out.json"),),
                rationale_code="SKILL",
            ),
        )
        action = s.materialize({"path": "out.json", "content": "[]"})
        assert action.arguments == {"path": "out.json", "content": "[]"}
        assert action.rationale_code == "SKILL:gravar-artefato@1.0.0"


class TestFastPathPorStep:
    """A fonte determinística — de onde vem a maioria dos acertos na V0."""

    async def test_step_materializado_dispara(self, registry):
        fp = FastPath(registry=registry)
        match = await fp.match(context(plan=plan(step("a"))))

        assert match is not None
        assert match.source is FastPathSource.STEP
        assert match.score == 1.0
        assert match.step_id == "a"

    async def test_step_sem_argumentos_nao_dispara(self, registry):
        fp = FastPath(registry=registry)
        assert await fp.match(context(plan=plan(step("a", arguments=None)))) is None

    async def test_dependencia_pendente_bloqueia(self, registry):
        p = plan(step("a"), step("b", dependencies=("a",)))
        assert next_ready_step(p).id == "a"

    async def test_step_concluido_libera_o_seguinte(self):
        p = plan(
            step("a", status=PlanStepStatus.DONE),
            step("b", dependencies=("a",)),
        )
        assert next_ready_step(p).id == "b"

    async def test_risco_acima_do_teto_nao_dispara(self, registry, sandbox):
        """Fast Path não decide sobre risco."""
        fp = FastPath(registry=registry, max_auto_risk=RiskLevel.R0)
        p = plan(
            step(
                "w",
                preferred_tool="filesystem.write",
                arguments={"path": "out.json", "content": "[]"},
                expected_outcomes=(FileExists(path="out.json"),),
            )
        )
        assert await fp.match(context(plan=p)) is None


class TestFastPathPorSkill:
    def _fast_path(self, registry, *skills) -> FastPath:
        catalogo = SkillRegistry()
        for s in skills:
            catalogo.register(s)
        return FastPath(registry=registry, skills=catalogo)

    async def test_skill_compativel_dispara(self, registry, sandbox):
        fp = self._fast_path(registry, skill())
        match = await fp.match(
            context(observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json", "content": "[]"},
        )

        assert match is not None
        assert match.source is FastPathSource.SKILL
        assert match.skill_id == "gravar-artefato"
        assert match.action.arguments["path"] == "out.json"

    async def test_inputs_ausentes_bloqueiam(self, registry, sandbox):
        fp = self._fast_path(registry, skill())
        match = await fp.match(
            context(observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json"},
        )
        assert match is None
        assert any("MISSING_INPUTS" in r.reason_code for r in fp.rejections)

    async def test_precondicao_indeterminada_nao_libera(self, registry, sandbox):
        """Na dúvida, delibera."""
        fp = self._fast_path(
            registry, skill(preconditions=(FileExists(path="../fora.json"),))
        )
        match = await fp.match(
            context(observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json", "content": "[]"},
        )
        assert match is None
        assert any(r.reason_code == "PRECONDITIONS_UNMET" for r in fp.rejections)

    async def test_precondicao_satisfeita_libera(self, registry, sandbox):
        fp = self._fast_path(registry, skill(preconditions=(FileExists(path="in.json"),)))
        match = await fp.match(
            context(observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json", "content": "[]"},
        )
        assert match is not None

    async def test_falha_nao_resolvida_derruba_o_fast_path_inteiro(self, registry, sandbox):
        fp = self._fast_path(registry, skill())
        match = await fp.match(
            context(plan=plan(step("a"))),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            has_unresolved_failure=True,
        )
        assert match is None

    async def test_step_tem_precedencia_sobre_skill(self, registry, sandbox):
        fp = self._fast_path(registry, skill())
        match = await fp.match(
            context(plan=plan(step("a")), observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json", "content": "[]"},
        )
        assert match.source is FastPathSource.STEP

    async def test_rejeicoes_ficam_auditaveis(self, registry, sandbox):
        fp = self._fast_path(registry, skill(tool_version="0.9.0"))
        await fp.match(
            context(observations=(observation(("tool:filesystem.write",)),)),
            evaluation=EvaluationContext(sandbox=sandbox, now=NOW),
            inputs={"path": "out.json", "content": "[]"},
        )
        assert fp.rejections
        assert "TOOL_VERSION_DRIFT" in fp.rejections[0].reason_code


class TestPlannerValidator:
    def test_plano_valido_passa(self, registry):
        report = PlannerValidator(registry).validate(plan(step("a")))
        assert report.materialized_steps == ("a",)
        assert report.fully_materialized is True

    def test_step_sem_evidencia_externa_e_recusado(self, registry):
        """A regra de C02 aplicada no passo: precisa olhar o mundo."""
        with pytest.raises(PlanValidationError, match="não é verificável"):
            PlannerValidator(registry).validate(
                plan(
                    step(
                        "a",
                        expected_outcomes=(
                            JsonPathEquals(
                                source="ACTION_RESULT", json_path="$.ok", expected=True
                            ),
                        ),
                    )
                )
            )

    def test_tool_inexistente_e_recusada(self, registry):
        with pytest.raises(PlanValidationError, match="TOOL_SELECTION_ERROR"):
            PlannerValidator(registry).validate(
                plan(step("a", preferred_tool="database.drop", arguments=None))
            )

    def test_risco_subdeclarado_e_recusado(self, registry):
        """Subdeclarar risco faria o passo escapar dos gates da policy."""
        with pytest.raises(PlanValidationError, match="declara R0"):
            PlannerValidator(registry).validate(
                plan(
                    step(
                        "a",
                        preferred_tool="filesystem.write",
                        arguments={"path": "out.json", "content": "[]"},
                        expected_outcomes=(FileExists(path="out.json"),),
                        risk_hint=RiskLevel.R0,
                    )
                )
            )

    def test_argumentos_fora_do_schema_sao_recusados(self, registry):
        with pytest.raises(PlanValidationError, match="TOOL_VALIDATION_ERROR"):
            PlannerValidator(registry).validate(
                plan(step("a", arguments={"caminho": "in.json"}))
            )

    def test_acao_repetida_no_mesmo_plano_e_recusada(self, registry):
        with pytest.raises(PlanValidationError, match="mesma ação repetida"):
            PlannerValidator(registry).validate(plan(step("a"), step("b")))

    def test_teto_de_risco_do_plano(self, registry):
        validator = PlannerValidator(registry, max_risk=RiskLevel.R0)
        with pytest.raises(PlanValidationError, match="acima do teto"):
            validator.validate(
                plan(
                    step(
                        "w",
                        preferred_tool="filesystem.write",
                        arguments={"path": "out.json", "content": "[]"},
                        expected_outcomes=(FileExists(path="out.json"),),
                        risk_hint=RiskLevel.R1,
                    )
                )
            )

    def test_step_sem_tool_ainda_e_valido(self, registry):
        """Step não resolvido: a tool entra quando o passo for detalhado."""
        report = PlannerValidator(registry).validate(
            plan(step("a", preferred_tool=None, arguments=None))
        )
        assert report.fully_materialized is False
