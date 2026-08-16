"""Roda o runtime contra um modelo real servido pelo LM Studio.

Não faz parte da suíte: exige servidor externo, é lento e não é determinístico.
Os benchmarks B1–B5 continuam com deliberador roteirizado, porque medir o
runtime exige que a variável do modelo fique fixa.

O que este script mede é outra coisa: como o runtime se comporta quando a
saída do LLM **não** foi escrita por mim. Um modelo local fraco é um gerador
barato de entrada adversarial — tool inventada, proveniência não auditável,
argumento malformado. O critério de aprovação não é "o modelo acertou": é
"nenhuma execução terminou em falso sucesso e nenhuma exceção vazou".

Uso:

    .venv/Scripts/python.exe scripts/live_local_model.py --runs 5

Com `--approve` o harness simula o humano aprovando ações R1 pendentes, para
exercitar execução e verificação. Sem a flag, um run que leia conteúdo externo
para em `WAITING_USER` — que é C10 funcionando, não falha.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tests"))
sys.path.insert(0, str(RAIZ / "tests" / "benchmarks"))

from scenarios import CONTEUDO_CORRETO, build_world, seed_goal  # noqa: E402

from neuroloop.cognition.deliberator import (  # noqa: E402
    DeliberationError,
    Deliberator,
)
from neuroloop.core.criteria import FileExists, JsonPathCount  # noqa: E402
from neuroloop.core.enums import RunPhase  # noqa: E402
from neuroloop.core.runs import ExecutionBudget  # noqa: E402
from neuroloop.llm.client import LLMError  # noqa: E402
from neuroloop.llm.openai_compat import (  # noqa: E402
    DEFAULT_BASE_URL,
    LOCAL_DELIBERATION,
    OpenAICompatLLMClient,
)
from neuroloop.persistence.repositories import (  # noqa: E402
    ActionRepository,
    RunRepository,
)
from neuroloop.persistence.session import configure_event_loop  # noqa: E402
from neuroloop.runtime.agent_runtime import AgentRuntime  # noqa: E402


class _ClienteQueLembra:
    """Delega ao cliente real e guarda a última falha.

    `RunResult` carrega só o `ErrorCode`. `REASONING_ERROR` cobre desde
    timeout de transporte até resposta vazia por orçamento de raciocínio, e
    sem a mensagem original não dá para distinguir uma da outra.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.ultima_falha: str | None = None

    async def structured(self, **kwargs):
        try:
            return await self._real.structured(**kwargs)
        except LLMError as error:
            self.ultima_falha = str(error)
            raise

    async def aclose(self) -> None:
        await self._real.aclose()


class _DeliberadorQueLembra:
    """Envolve o Deliberator e guarda a última falha de deliberação.

    `LLMError` já é capturado no cliente, mas `PLANNING_ERROR` e
    `TOOL_*_ERROR` nascem na tradução e na validação, depois da chamada. O
    runtime os registra no histórico do run; o `RunResult` devolve só o
    `ErrorCode`, que não diz qual campo reprovou.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.ultima_falha: str | None = None

    def __getattr__(self, nome):  # model_profile, registry, etc.
        return getattr(self._real, nome)

    async def decide(self, *args, **kwargs):
        try:
            return await self._real.decide(*args, **kwargs)
        except DeliberationError as error:
            self.ultima_falha = str(error)
            raise


async def _aprovar_e_seguir(world, runtime, run_id, resultado, *, teto: int = 4):
    """Simula o humano aprovando, uma ação por vez, e retoma.

    `teto` existe para o caso de o modelo entrar em ciclo pedindo aprovação
    sem nunca avançar: sem limite o harness rodaria para sempre.
    """
    aprovacoes = 0
    while resultado.phase is RunPhase.WAITING_USER and aprovacoes < teto:
        async with world.factory() as session:
            cp = await RunRepository(session).load(run_id)
            if cp.pending_approval_action_id is None:
                break  # espera por outro motivo; não é aprovação
            await ActionRepository(session).approve(cp.pending_approval_action_id)
            await RunRepository(session).save(
                cp.model_copy(
                    update={
                        "pending_approval_action_id": None,
                        "pending_approval_fingerprint": None,
                        "waiting_reason": None,
                    }
                )
            )
            await session.commit()
        aprovacoes += 1
        resultado = await runtime.run_until_pause(run_id)
    return resultado, aprovacoes


async def uma_execucao(tmp: Path, seed: int, modelo: str, aprovar: bool) -> dict:
    """Uma execução completa. Devolve fatos, não julgamento."""
    world = await build_world(tmp, seed)
    cliente = OpenAICompatLLMClient(
        base_url=os.environ.get("NEUROLOOP_LOCAL_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.environ.get("NEUROLOOP_LOCAL_API_KEY", "lm-studio"),
        model_override=modelo or LOCAL_DELIBERATION.model,
    )
    cliente = _ClienteQueLembra(cliente)
    world.llm = cliente

    fato: dict = {"seed": seed}
    try:
        # Os dois critérios são declarados de propósito. `FileExists` sozinho
        # deixaria o agente vencer criando um arquivo vazio — e ele estaria
        # certo, porque teria satisfeito o que foi pedido. Falso sucesso só
        # significa alguma coisa quando o critério declarado cobre o efeito
        # que o teste depois confere.
        goal = await seed_goal(
            world,
            criteria=(
                FileExists(path="eligible.json"),
                JsonPathCount(
                    source="FILE",
                    path="eligible.json",
                    json_path="$",
                    expected_count=3,
                ),
            ),
            description="gravar eligible.json com os 3 registros de orders.json",
        )
        # Perfil próprio: DELIBERATION usa max_tokens=16000, que num modelo
        # local a ~9 tok/s significa quase meia hora por chamada — e o reparo
        # dobra isso. LOCAL_DELIBERATION troca teto por relógio.
        deliberador = _DeliberadorQueLembra(
            Deliberator(
                llm=world.llm,
                registry=world.registry,
                model_profile=LOCAL_DELIBERATION,
            )
        )
        runtime = AgentRuntime(
            session_factory=world.factory,
            registry=world.registry,
            sandbox=world.sandbox,
            llm=world.llm,
            deliberator=deliberador,
        )
        # Orçamento declarado, não herdado. O padrão de 100k tokens supõe um
        # modelo que gasta alguns milhares por deliberação; este gasta os 8192
        # inteiros em raciocínio a cada chamada, o que dá ~10 deliberações
        # antes de BUDGET_EXCEEDED. O relógio também sobe: a ~9 tok/s, 900s
        # não cobrem duas chamadas.
        #
        # Afrouxar limite para um teste passar costuma esconder problema. Aqui
        # não esconde: o que se mede é falso sucesso e exceção vazada, e ambos
        # continuam sendo zero ou não. O limite de iterações fica no padrão,
        # que é o que impede laço infinito.
        checkpoint = await runtime.start(
            goal,
            budget=ExecutionBudget(
                token_budget=1_000_000,
                wall_clock_seconds=7200,
            ),
        )
        resultado = await runtime.run_until_pause(checkpoint.run_id)

        # Ler um arquivo do sandbox produz conteúdo UNTRUSTED_EXTERNAL, então
        # gravar um artefato derivado dele é R1 com origem não confiável e
        # para em WAITING_USER — C10 funcionando. Sem simular o humano, o
        # harness nunca exercita execução nem verificação.
        #
        # Fica atrás de flag e aprova uma ação por vez, pelo fingerprint
        # exato que está pendente (C19). Aprovar em bloco transformaria a
        # medição de falso sucesso numa medição de nada.
        if aprovar:
            resultado, fato["aprovacoes"] = await _aprovar_e_seguir(
                world, runtime, checkpoint.run_id, resultado
            )

        fato["estado"] = resultado.phase.name
        fato["reason"] = resultado.error_code.name if resultado.error_code else ""
        fato["waiting"] = resultado.waiting_reason or ""
        fato["iteracoes"] = resultado.iteration
        fato["deliberacoes"] = resultado.deliberations
        fato["tokens"] = resultado.tokens_used

        artefato = world.sandbox.root / "eligible.json"
        fato["arquivo_existe"] = artefato.exists()
        fato["conteudo"] = (
            artefato.read_text(encoding="utf-8")[:200] if artefato.exists() else None
        )
        fato["conteudo_ok"] = (
            artefato.exists()
            and artefato.read_text(encoding="utf-8").strip() == CONTEUDO_CORRETO.strip()
        )
        # Falso sucesso: o agente declarou vitória sem o efeito no mundo.
        # É a única falha que o teste trata como dura.
        fato["falso_sucesso"] = resultado.completed and not fato["conteudo_ok"]
        fato["excecao"] = None
        fato["llm_falha"] = cliente.ultima_falha
        fato["delib_falha"] = deliberador.ultima_falha
    except Exception as error:  # noqa: BLE001 - queremos registrar, não abortar
        fato["estado"] = "EXCECAO_VAZOU"
        fato["excecao"] = f"{type(error).__name__}: {error}"
        fato["falso_sucesso"] = False
        fato["conteudo_ok"] = False
    finally:
        await cliente.aclose()
        await world.close()
    return fato


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default=None, help="id no LM Studio")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="simula o humano aprovando ações R1 pendentes, para exercitar "
        "execução e verificação; desligado por padrão",
    )
    args = parser.parse_args()

    configure_event_loop()
    estados: Counter[str] = Counter()
    fatos: list[dict] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for seed in range(args.runs):
            fato = await uma_execucao(tmp, seed, args.model, args.approve)
            fatos.append(fato)
            estados[fato["estado"]] += 1
            marca = "!!" if fato["falso_sucesso"] else "  "
            print(
                f"{marca} seed={seed} fase={fato['estado']} "
                f"erro={fato.get('reason', '')} "
                f"espera={fato.get('waiting', '')} "
                f"delib={fato.get('deliberacoes', '-')} "
                f"aprov={fato.get('aprovacoes', 0)} "
                f"conteudo_ok={fato['conteudo_ok']}"
            )
            if fato["excecao"]:
                print(f"     excecao: {fato['excecao'][:300]}")
            if fato.get("llm_falha"):
                print(f"     llm: {fato['llm_falha'][:250]}")
            if fato.get("delib_falha"):
                print(f"     delib: {fato['delib_falha'][:300]}")

    print("\n--- resumo ---")
    for estado, n in estados.most_common():
        print(f"  {estado}: {n}")
    falsos = sum(1 for f in fatos if f["falso_sucesso"])
    vazadas = sum(1 for f in fatos if f["excecao"])
    print(f"  falso_sucesso: {falsos}  (alvo: 0)")
    print(f"  excecao_vazou: {vazadas}  (alvo: 0)")
    Path("live_local_report.json").write_text(
        json.dumps(fatos, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 1 if (falsos or vazadas) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
