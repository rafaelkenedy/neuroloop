# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Este projeto pretende seguir [Semantic Versioning](https://semver.org/lang/pt-BR/).

> **TODO(verificar):** o repositório não tem commits nem tags. A entrada abaixo foi
> montada a partir do estado da árvore de trabalho e da versão declarada em
> `pyproject.toml` (`0.0.1`), não de histórico Git. Confirmar a data de release e criar
> a tag correspondente.

## [Não lançado]

Nada ainda.

## [0.0.1] — não lançado

Primeira versão do runtime V0. Cobre o backlog de implementação de
[`02_correcoes_spec.md`](02_correcoes_spec.md), seção C21 (TASK-001 a TASK-015).

### Adicionado

**Núcleo e schemas**
- Tipos compartilhados com invariantes no schema: `Criterion` como união discriminada
  com lógica ternária, `Goal`, `Plan`, `ActionProposal`, `VerificationResult`,
  `RunCheckpoint`, `ExecutionBudget`.
- Identidade de ações separando `idempotency_key` (at-most-once do efeito) de
  `action_fingerprint` (detecção de loop).

**Runtime**
- State machine de 12 fases com estados terminais selados e retomada derivada do estado
  durável.
- `DurableExecutor` com attempt gravado e commitado antes da chamada externa, ciclo
  `UNKNOWN_EFFECT → probe`, detecção de duplicata e política de retry por ação lógica.
- `AgentRuntime`: loop completo de percepção, memória, contexto, gates, Fast Path ou
  deliberação, autorização, execução, verificação e episódio.

**Cognição**
- `Deliberator` unindo Controller e Planner numa chamada, com três portas de validação.
- `FastPath` com fontes `STEP` (determinística) e `SKILL` (com score explícito).
- `PlannerValidator` exigindo evidência externa por passo e recusando risco
  subdeclarado.
- Skills versionadas com auto-desconfiança por taxa de sucesso, falha recente e
  mudança de versão de tool.

**Verificação**
- Avaliador assíncrono de `Criterion` com sondas HTTP e de comando injetáveis.
- Verifier de quatro níveis, exigindo delta em relação ao baseline para concluir goal.

**Segurança**
- `PolicyEngine` com gates determinísticos, tiers R0–R3, sandbox com resolução canônica,
  propagação de taint e aprovação vinculada a fingerprint.

**Memória**
- Episódios com importância calculada e tags de vocabulário fechado.
- Retrieval top-k sem embeddings, com afinidade estrutural como porta.
- Plan cache indexado por assinatura de objetivo.

**LLM**
- Protocolo `LLMClient` com `usage` na resposta, tabela de preços versionada, adapter
  Anthropic e cliente falso.
- Schema de saída achatado, estritamente menor que o domínio.

**Persistência**
- Onze tabelas, migrations Alembic `0001_initial_schema` e `0002_plan_cache`.
- Lease com fencing token e optimistic locking.
- `UtcDateTime` normalizando datetimes para UTC tz-aware nos dois sentidos.

**Observabilidade**
- Identidade de trace e fingerprints das versões em vigor.
- Spans gravados como eventos de run, com redação de segredos e chain of thought.
- Reconstrução de "por que o agente fez isso" e métricas com piso de denominador.

**API**
- Rotas de goal, run, execução, retomada, aprovação, cancelamento, trace, episódios,
  explicação, métricas, timeline e ações.

**Testes e benchmarks**
- 598 testes passando em SQLite e em PostgreSQL.
- Oracles B1–B5 independentes do agente, com a independência verificada por AST.
- Harness com intervalo de Wilson e falhas duras de alvo zero.

### Segurança

- Conteúdo `UNTRUSTED_EXTERNAL` não empresta autoridade a uma ação: exige aprovação em
  R1 e é recusado em R2 ou acima.
- Conteúdo externo entra no prompt dentro de envelope sanitizado; marcação injetada é
  neutralizada.
- Trace não guarda chain of thought nem segredos.
- Caminhos de filesystem são resolvidos canonicamente antes da comparação com o
  sandbox.

### Limitações conhecidas

- Não há entrada de servidor ASGI: `create_app` é uma fábrica.
- O adapter Anthropic nunca foi exercitado contra a API real.
- Benchmarks rodam em SQLite; o runtime é verificado em PostgreSQL pela suíte de
  integração.
- Com N=30, o limite superior do `false_success_rate` é 11%; o alvo de 1% exigiria cerca
  de 300 execuções.
- Escopo V0: sem memória semântica, consolidação ou scheduler.
