"""TASK-013. Aceite: WAITING_USER sobrevive restart; aprovação vinculada (C19)."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest

from neuroloop.api.app import create_app
from neuroloop.core import FileExists, RiskLevel
from neuroloop.llm import FakeLLMClient, LlmActionProposal, LlmDecision, LlmFileExists
from neuroloop.persistence import build_session_factory
from neuroloop.persistence.repositories import ObservationRepository
from neuroloop.runtime import AgentRuntime
from neuroloop.tools import EffectProbe, Sandbox, ToolDefinition, ToolRegistry
from neuroloop.tools.adapters import register_filesystem_tools

ELIGIBLE = json.dumps([{"id": 1}, {"id": 2}, {"id": 3}])

# Tool R2: exige aprovação humana (spec §22).
PUBLICAR = ToolDefinition(
    name="artifact.publish",
    version="1.0.0",
    description="publica o artefato num destino externo",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    risk_level=RiskLevel.R2,
    side_effects=True,
    reversible=False,
    requires_confirmation=True,
    supports_idempotency=True,
    timeout_seconds=5.0,
    capabilities=frozenset({"fs:write"}),
    effect_probe=EffectProbe(
        criterion_template=FileExists(path="{path}"),
        argument_bindings={"path": "path"},
    ),
)


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "orders.json").write_text(ELIGIBLE, encoding="utf-8")
    return Sandbox(root)


@pytest.fixture
def registry(sandbox) -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg, sandbox)

    async def _publish(arguments):
        sandbox.resolve(arguments["path"]).write_text(
            arguments["content"], encoding="utf-8"
        )
        return {"published": True}

    reg.register(PUBLICAR, _publish)
    return reg


@pytest.fixture
def llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def client(engine, registry, sandbox, llm):
    factory = build_session_factory(engine)
    runtime = AgentRuntime(
        session_factory=factory, registry=registry, sandbox=sandbox, llm=llm
    )
    app = create_app(runtime=runtime, session_factory=factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


GOAL_PAYLOAD = {
    "description": "gerar eligible.json",
    "success_criteria": [{"kind": "FILE_EXISTS", "path": "eligible.json"}],
}


def acao(tool: str, derived_from: list[str]) -> LlmDecision:
    return LlmDecision(
        type="ACT",
        reason_code="WRITE",
        action=LlmActionProposal(
            tool=tool,
            arguments_json=json.dumps({"path": "eligible.json", "content": ELIGIBLE}),
            expected_outcomes=[LlmFileExists(path="eligible.json")],
            rationale_code="WRITE",
            derived_from=derived_from,
        ),
    )


async def _origem(engine, run_id) -> str:
    """Id da observação do goal — confiável, mesmo já consumida."""
    factory = build_session_factory(engine)
    async with factory() as session:
        from sqlalchemy import select

        from neuroloop.persistence import models

        row = await session.scalar(
            select(models.Observation).where(
                models.Observation.run_id == UUID(str(run_id)),
                models.Observation.kind == "goal",
            )
        )
    return str(row.id)


class TestCriacao:
    async def test_health(self, client):
        async with client as c:
            assert (await c.get("/health")).json() == {"status": "ok"}

    async def test_cria_goal_e_run(self, client):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            resposta = await c.post(f"/goals/{goal['goal_id']}/runs", json={})

        assert resposta.status_code == 201
        assert resposta.json()["phase"] == "CREATED"
        assert resposta.json()["iteration"] == 0

    async def test_goal_sem_evidencia_externa_e_recusado_na_porta(self, client):
        """C02 barrado na entrada, não três ciclos adiante."""
        async with client as c:
            resposta = await c.post(
                "/goals",
                json={
                    "description": "confiar na tool",
                    "success_criteria": [
                        {
                            "kind": "JSON_PATH_EQUALS",
                            "source": "ACTION_RESULT",
                            "json_path": "$.ok",
                            "expected": True,
                        }
                    ],
                },
            )
        assert resposta.status_code == 422
        assert "INVALID_GOAL_CRITERIA" in resposta.text

    async def test_run_inexistente_e_404(self, client):
        async with client as c:
            assert (await c.get(f"/runs/{uuid4()}")).status_code == 404

    async def test_budget_do_pedido_e_respeitado(self, client):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (
                await c.post(
                    f"/goals/{goal['goal_id']}/runs", json={"max_iterations": 1}
                )
            ).json()
            resultado = (await c.post(f"/runs/{run['run_id']}/execute")).json()

        assert resultado["error_code"] == "BUDGET_EXCEEDED"


class TestExecucao:
    async def test_run_completo_pela_api(self, client, engine, llm, sandbox):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()
            llm.queue(acao("filesystem.write", [await _origem(engine, run["run_id"])]))

            resultado = (await c.post(f"/runs/{run['run_id']}/execute")).json()

        assert resultado["phase"] == "COMPLETED"
        assert resultado["goal_satisfied"] is True
        assert (sandbox.root / "eligible.json").is_file()

    async def test_trace_explica_o_que_aconteceu(self, client, engine, llm):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()
            llm.queue(acao("filesystem.write", [await _origem(engine, run["run_id"])]))
            await c.post(f"/runs/{run['run_id']}/execute")

            trace = (await c.get(f"/runs/{run['run_id']}/trace")).json()
            episodios = (await c.get(f"/runs/{run['run_id']}/episodes")).json()

        assert any(e["kind"] == "ACTION_AUTHORIZATION" for e in trace)
        assert any(e["kind"] == "PHASE_TRANSITION" for e in trace)
        assert episodios[0]["tool_name"] == "filesystem.write"

    async def test_cancelamento_pela_api(self, client, engine, llm):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()

            assert (await c.post(f"/runs/{run['run_id']}/cancel")).status_code == 202
            resultado = (await c.post(f"/runs/{run['run_id']}/execute")).json()

        assert resultado["phase"] == "CANCELLED"


class TestAprovacaoHumana:
    """C19 pela API: a aprovação vale para argumentos, não para a ação lógica."""

    async def _ate_aprovacao(self, c, engine, llm):
        goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
        run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()
        llm.queue(acao("artifact.publish", [await _origem(engine, run["run_id"])]))
        resultado = (await c.post(f"/runs/{run['run_id']}/execute")).json()
        estado = (await c.get(f"/runs/{run['run_id']}")).json()
        return run["run_id"], resultado, estado

    async def test_r2_para_o_run_pedindo_aprovacao(self, client, engine, llm, sandbox):
        async with client as c:
            _, resultado, estado = await self._ate_aprovacao(c, engine, llm)

        assert resultado["phase"] == "WAITING_USER"
        assert estado["pending_approval_action_id"] is not None
        assert estado["pending_approval_fingerprint"].startswith("sha256:")
        assert not (sandbox.root / "eligible.json").exists()

    async def test_aprovacao_correta_libera_a_execucao(
        self, client, engine, llm, sandbox
    ):
        async with client as c:
            run_id, _, estado = await self._ate_aprovacao(c, engine, llm)
            llm.queue(acao("artifact.publish", [await _origem(engine, run_id)]))

            resposta = await c.post(
                f"/runs/{run_id}/approve",
                json={
                    "action_id": estado["pending_approval_action_id"],
                    "fingerprint": estado["pending_approval_fingerprint"],
                },
            )

        assert resposta.status_code == 200
        assert resposta.json()["phase"] == "COMPLETED"
        assert (sandbox.root / "eligible.json").is_file()

    async def test_fingerprint_divergente_e_recusado(self, client, engine, llm):
        """Aprovar `a.json` não pode autorizar `b.json`."""
        async with client as c:
            run_id, _, estado = await self._ate_aprovacao(c, engine, llm)

            resposta = await c.post(
                f"/runs/{run_id}/approve",
                json={
                    "action_id": estado["pending_approval_action_id"],
                    "fingerprint": "sha256:outro",
                },
            )

        assert resposta.status_code == 409
        assert "não corresponde" in resposta.json()["detail"]

    async def test_acao_divergente_e_recusada(self, client, engine, llm):
        async with client as c:
            run_id, _, estado = await self._ate_aprovacao(c, engine, llm)

            resposta = await c.post(
                f"/runs/{run_id}/approve",
                json={
                    "action_id": str(uuid4()),
                    "fingerprint": estado["pending_approval_fingerprint"],
                },
            )

        assert resposta.status_code == 409

    async def test_aprovar_run_que_nao_espera_e_conflito(self, client, engine, llm):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()

            resposta = await c.post(
                f"/runs/{run['run_id']}/approve",
                json={"action_id": str(uuid4()), "fingerprint": "sha256:x"},
            )

        assert resposta.status_code == 409

    async def test_acao_recusada_fica_no_registro_de_acoes(self, client, engine, llm):
        """Proposta que aguardou aprovação continua auditável."""
        async with client as c:
            run_id, _, _ = await self._ate_aprovacao(c, engine, llm)
            acoes = (await c.get(f"/runs/{run_id}/actions")).json()

        assert len(acoes) == 1
        assert acoes[0]["tool"] == "artifact.publish"
        assert acoes[0]["approved_by_user"] is False


class TestEsperaSobreviveAoRestart:
    """Aceite explícito da TASK-013."""

    async def test_estado_de_espera_persiste_em_outro_processo(
        self, engine, registry, sandbox, llm
    ):
        factory = build_session_factory(engine)
        runtime_a = AgentRuntime(
            session_factory=factory, registry=registry, sandbox=sandbox, llm=llm
        )
        app_a = create_app(runtime=runtime_a, session_factory=factory)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://a"
        ) as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()
            llm.queue(acao("artifact.publish", [await _origem(engine, run["run_id"])]))
            await c.post(f"/runs/{run['run_id']}/execute")

        # "Reinício": runtime e app novos, mesmo banco.
        llm_b = FakeLLMClient()
        runtime_b = AgentRuntime(
            session_factory=factory, registry=registry, sandbox=sandbox, llm=llm_b
        )
        app_b = create_app(runtime=runtime_b, session_factory=factory)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://b"
        ) as c:
            estado = (await c.get(f"/runs/{run['run_id']}")).json()
            assert estado["phase"] == "WAITING_USER"
            assert estado["pending_approval_action_id"] is not None

            llm_b.queue(acao("artifact.publish", [await _origem(engine, run["run_id"])]))
            resposta = await c.post(
                f"/runs/{run['run_id']}/approve",
                json={
                    "action_id": estado["pending_approval_action_id"],
                    "fingerprint": estado["pending_approval_fingerprint"],
                },
            )

        assert resposta.json()["phase"] == "COMPLETED"

    async def test_aprovar_sem_retomar_deixa_o_run_parado(self, client, engine, llm):
        async with client as c:
            goal = (await c.post("/goals", json=GOAL_PAYLOAD)).json()
            run = (await c.post(f"/goals/{goal['goal_id']}/runs", json={})).json()
            llm.queue(acao("artifact.publish", [await _origem(engine, run["run_id"])]))
            await c.post(f"/runs/{run['run_id']}/execute")
            estado = (await c.get(f"/runs/{run['run_id']}")).json()

            resposta = await c.post(
                f"/runs/{run['run_id']}/approve",
                json={
                    "action_id": estado["pending_approval_action_id"],
                    "fingerprint": estado["pending_approval_fingerprint"],
                    "resume": False,
                },
            )
            depois = (await c.get(f"/runs/{run['run_id']}")).json()

        assert resposta.json()["waiting_reason"] == "APPROVED_NOT_RESUMED"
        assert depois["pending_approval_action_id"] is None
