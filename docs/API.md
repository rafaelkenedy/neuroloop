# API HTTP

Rotas extraídas de [`src/neuroloop/api/app.py`](../src/neuroloop/api/app.py); os
contratos, de [`src/neuroloop/api/schemas.py`](../src/neuroloop/api/schemas.py).

A API valida entrada, delega ao runtime e devolve estado. Ela não decide nada. Em
particular, `approve` **não executa** a ação: registra que o humano autorizou aqueles
argumentos exatos, e a execução acontece no ciclo seguinte, com a policy reavaliando.

> **TODO(verificar):** não há servidor documentável. `create_app(runtime, session_factory)`
> é uma fábrica e o repositório não expõe um objeto ASGI nem entrada em
> `[project.scripts]`. Os exemplos abaixo assumem a aplicação montada em processo, como
> em `tests/integration/test_api.py`.

Não há autenticação implementada em nenhuma rota.

## Resumo

| Método | Rota | Sucesso | Erros |
|---|---|---|---|
| `GET` | `/health` | 200 | — |
| `POST` | `/goals` | 201 | 422 |
| `POST` | `/goals/{goal_id}/runs` | 201 | 404 |
| `GET` | `/runs/{run_id}` | 200 | 404 |
| `POST` | `/runs/{run_id}/execute` | 200 | 404 |
| `POST` | `/runs/{run_id}/resume` | 200 | 404 |
| `POST` | `/runs/{run_id}/cancel` | 202 | 404 |
| `POST` | `/runs/{run_id}/approve` | 200 | 404, 409 |
| `GET` | `/runs/{run_id}/trace` | 200 | 404 |
| `GET` | `/runs/{run_id}/episodes` | 200 | 404 |
| `GET` | `/runs/{run_id}/explain` | 200 | 404 |
| `GET` | `/runs/{run_id}/metrics` | 200 | 404 |
| `GET` | `/runs/{run_id}/timeline` | 200 | 404 |
| `GET` | `/runs/{run_id}/actions` | 200 | 404 |

O FastAPI também devolve `422` para corpo malformado em qualquer rota com payload.

## Ciclo de vida típico

```
POST /goals                       cria o objetivo
POST /goals/{goal_id}/runs        cria o run (fase CREATED)
POST /runs/{run_id}/execute       roda até pausar ou concluir
GET  /runs/{run_id}               consulta o estado
POST /runs/{run_id}/approve       se parou em WAITING_USER por aprovação
GET  /runs/{run_id}/explain       por que cada ação aconteceu
```

---

## `GET /health`

Sem parâmetros.

```json
{ "status": "ok" }
```

---

## `POST /goals`

Cria um objetivo. O `agent` é criado sob demanda pelo nome informado.

### Corpo

| Campo | Tipo | Obrigatório | Padrão | Observação |
|---|---|---|---|---|
| `description` | string | sim | — | Mínimo 1 caractere. |
| `success_criteria` | array de `Criterion` | sim | — | Mínimo 1 item. |
| `failure_criteria` | array de `Criterion` | não | `[]` | Condição de parada, nunca de sucesso. |
| `agent_name` | string | não | `"neuroloop"` | — |
| `priority` | number | não | `0.5` | Entre 0.0 e 1.0. |
| `deadline` | datetime | não | `null` | — |

### `Criterion`

União discriminada por `kind`. Membros e campos, de
[`core/criteria.py`](../src/neuroloop/core/criteria.py):

| `kind` | Campos | Observa |
|---|---|---|
| `FILE_EXISTS` | `path` | estado externo |
| `FILE_MATCHES_JSON_SCHEMA` | `path`, `json_schema` | estado externo |
| `JSON_PATH_EQUALS` | `source` (`FILE`\|`ACTION_RESULT`), `json_path`, `expected`, `path` | externo se `source=FILE` |
| `JSON_PATH_COUNT` | `source`, `json_path`, `expected_count`, `path` | externo se `source=FILE` |
| `VALUE_EQUALS` | `ref`, `expected` | estado do run |
| `HTTP_STATUS_EQUALS` | `method` (`GET`\|`HEAD`), `url`, `expected_status` | estado externo |
| `COMMAND_EXIT_CODE_EQUALS` | `command`, `expected_exit_code` | estado externo |
| `ALL_OF` / `ANY_OF` | `criteria` | o mais fraco entre os filhos |

Todos aceitam `negate` (padrão `false`).

### Exemplo

```json
{
  "description": "gerar eligible.json com exatamente 3 registros",
  "success_criteria": [
    {
      "kind": "ALL_OF",
      "criteria": [
        { "kind": "FILE_EXISTS", "path": "eligible.json" },
        {
          "kind": "JSON_PATH_COUNT",
          "source": "FILE",
          "path": "eligible.json",
          "json_path": "$[*]",
          "expected_count": 3
        }
      ]
    }
  ]
}
```

### Resposta `201`

```json
{
  "goal_id": "3f1a2b4c-0000-4000-8000-000000000001",
  "description": "gerar eligible.json com exatamente 3 registros",
  "status": "ACTIVE"
}
```

### `422` — objetivo não verificável

Devolvido quando nenhum `success_criteria` observa estado externo. É a regra C02
aplicada na entrada: objetivo aferível só pelo auto-relato da ferramenta é rejeitado.

```json
{
  "detail": "INVALID_GOAL_CRITERIA: pelo menos um success_criteria precisa observar EXTERNAL_STATE; auto-relato de tool não conclui goal"
}
```

---

## `POST /goals/{goal_id}/runs`

Cria o run e grava o objetivo como primeira observação. Não executa nada.

### Corpo (opcional)

| Campo | Tipo | Padrão do runtime |
|---|---|---|
| `max_iterations` | integer ≥ 1 | 30 |
| `token_budget` | integer ≥ 1 | 100000 |
| `wall_clock_seconds` | integer ≥ 1 | 900 |

Campos omitidos usam o `ExecutionBudget` padrão.

### Resposta `201` — `RunView`

```json
{
  "run_id": "9c2d7e10-0000-4000-8000-000000000002",
  "goal_id": "3f1a2b4c-0000-4000-8000-000000000001",
  "phase": "CREATED",
  "iteration": 0,
  "waiting_reason": null,
  "pending_approval_action_id": null,
  "pending_approval_fingerprint": null,
  "tokens_used": 0,
  "cost_used_usd": "0",
  "cancel_requested": false,
  "started_at": "2026-08-14T12:00:00Z",
  "wall_clock_deadline": "2026-08-14T12:15:00Z"
}
```

`404` se o `goal_id` não existe.

---

## `GET /runs/{run_id}`

Devolve o `RunView` acima. É por aqui que o cliente descobre que há aprovação pendente:
`pending_approval_action_id` e `pending_approval_fingerprint` são os valores a devolver
em `approve`.

Fases possíveis: `CREATED`, `PERCEIVING`, `DELIBERATING`, `PLANNING`, `EXECUTING`,
`VERIFYING`, `RECOVERING`, `UPDATING_MEMORY`, `WAITING_USER`, `COMPLETED`, `FAILED`,
`CANCELLED`.

---

## `POST /runs/{run_id}/execute`

Roda o loop até concluir, falhar, ser cancelado ou parar aguardando humano. Chamada
síncrona: retorna quando o run pausa.

### Resposta `200` — `RunResultView`

```json
{
  "run_id": "9c2d7e10-0000-4000-8000-000000000002",
  "phase": "COMPLETED",
  "iteration": 3,
  "error_code": null,
  "waiting_reason": null,
  "tokens_used": 1500,
  "cost_used_usd": "0.013500",
  "deliberations": 1,
  "fast_path_hits": { "STEP": 2 },
  "goal_satisfied": true
}
```

`error_code` usa a taxonomia de [`core/enums.py`](../src/neuroloop/core/enums.py).
Valores observados nos testes: `BUDGET_EXCEEDED`, `CANCELLED`, `PERMISSION_DENIED`,
`PROMPT_INJECTION`, `IMPOSSIBLE_TASK`, `REASONING_ERROR`, `INVALID_PLAN`,
`GOAL_PRE_SATISFIED`, `UNKNOWN_SIDE_EFFECT`, `RETRY_LIMIT`, `REPLAN_LIMIT`.

---

## `POST /runs/{run_id}/resume`

Igual a `execute`, com a opção de anexar uma mensagem do usuário como observação antes
de retomar.

```json
{ "message": "use o arquivo pedidos_v2.json" }
```

Corpo opcional; `message` pode ser `null`.

---

## `POST /runs/{run_id}/cancel`

Marca `cancel_requested`. **Não interrompe** execução em andamento: o pedido é honrado
no topo do próximo ciclo. Cancelar no meio de um efeito externo deixaria o efeito
indeterminado.

Resposta `202`:

```json
{ "cancel_requested": true }
```

---

## `POST /runs/{run_id}/approve`

Registra aprovação humana para uma ação pendente.

### Corpo

| Campo | Tipo | Obrigatório | Padrão |
|---|---|---|---|
| `action_id` | UUID | sim | — |
| `fingerprint` | string | sim | — |
| `resume` | boolean | não | `true` |

Ambos devem ser idênticos aos valores em `RunView`. Aprovar `a.json` não autoriza
`b.json`: se qualquer um divergir, a aprovação não vale e um novo pedido é necessário.

Com `resume: false`, a aprovação é gravada e o run permanece parado; a resposta traz
`waiting_reason: "APPROVED_NOT_RESUMED"`.

### Erros

| Código | Situação |
|---|---|
| `409` | Run não está em `WAITING_USER`. |
| `409` | Não há aprovação pendente. |
| `409` | `action_id` ou `fingerprint` divergem do pendente. |
| `404` | A ação informada não existe. |

```json
{ "detail": "aprovação não corresponde à ação pendente; um novo pedido é necessário" }
```

---

## `GET /runs/{run_id}/trace`

Eventos brutos de auditoria e tracing.

```json
[
  {
    "kind": "PHASE_TRANSITION",
    "reason": "topo do ciclo",
    "from_phase": "CREATED",
    "to_phase": "PERCEIVING",
    "error_code": null,
    "iteration": 1,
    "payload": {},
    "at": "2026-08-14T12:00:01Z"
  }
]
```

`kind` observados: `PHASE_TRANSITION`, `ACTION_AUTHORIZATION` e `SPAN:<nome>`, com
nomes de span `perception.collect`, `controller.decide`, `tool.execute`,
`verifier.evaluate`, `memory.store`.

---

## `GET /runs/{run_id}/episodes`

Memória episódica do run, em ordem de iteração.

```json
[
  {
    "iteration": 1,
    "decision_type": "ACT",
    "tool_name": "filesystem.write",
    "result_summary": "SUCCESS",
    "error_code": null,
    "importance": 0.1,
    "reward": 0.5,
    "tags": ["decision:ACT", "tool:filesystem.write", "outcome:SUCCESS"]
  }
]
```

---

## `GET /runs/{run_id}/explain`

Por que cada ação aconteceu. Inclui as ações **recusadas**, que costumam ser as mais
informativas depois de um incidente.

```json
[
  {
    "action_id": "b1c2d3e4-0000-4000-8000-000000000003",
    "why": "filesystem.write foi proposta por DELIBERATOR (WRITE), autorizada como AUTO_APPROVED:R1, executada em 1 tentativa(s) com desfecho SUCCESS; efeito esperado verificado: True",
    "tool": "filesystem.write",
    "risk_level": "R1",
    "decision_source": "DELIBERATOR",
    "authorization": "AUTO_APPROVED:R1",
    "tainted": false,
    "executed": true,
    "attempts": ["SUCCESS"],
    "arguments": { "path": "eligible.json", "content": "[]" },
    "derived_from": ["7a8b9c0d-0000-4000-8000-000000000004"],
    "fingerprint": "sha256:0123456789abcdef0123456789abcdef",
    "trace_id": "0123456789abcdef0123456789abcdef",
    "versions": { "v_model": "claude-opus-5", "v_tool_registry": "0123456789abcdef" }
  }
]
```

`arguments` passa pela redação: chaves com aparência de segredo e texto com formato de
credencial são substituídos por `«redigido»`.

---

## `GET /runs/{run_id}/metrics`

```json
{
  "run_id": "9c2d7e10-0000-4000-8000-000000000002",
  "phase": "COMPLETED",
  "iterations": 3,
  "tokens_used": 1500,
  "cost_usd": "0.013500",
  "actions_proposed": 2,
  "actions_executed": 2,
  "unsafe_action_proposals": 0,
  "duplicate_side_effects": 0,
  "dangling_attempts": 0,
  "attempts": 2,
  "retries": 0,
  "deliberations": 1,
  "fast_path_step_hits": 2,
  "fast_path_skill_hits": 0,
  "replans": 0,
  "episodes": 2,
  "declared_complete": true,
  "fast_path_hit_rate": null,
  "retry_rate": null,
  "replan_rate": null
}
```

Taxas vêm `null` quando o denominador é menor que 20: uma taxa sobre amostra pequena
não informa e induz confiança falsa.

`declared_complete` é o **numerador** do `false_success_rate`. O denominador e o
veredito vêm de um oracle independente — o cálculo não acontece aqui.

---

## `GET /runs/{run_id}/timeline`

Versão legível do trace.

```json
[
  { "at": "2026-08-14T12:00:01Z", "kind": "PHASE_TRANSITION", "detail": "CREATED → PERCEIVING: topo do ciclo" },
  { "at": "2026-08-14T12:00:02Z", "kind": "ACTION_AUTHORIZATION", "detail": "filesystem.write: AUTO_APPROVED:R1" }
]
```

---

## `GET /runs/{run_id}/actions`

Todas as ações do run, inclusive as que nunca executaram. Superfície de auditoria de
segurança.

```json
[
  {
    "action_id": "b1c2d3e4-0000-4000-8000-000000000003",
    "tool": "filesystem.write",
    "risk_level": "R1",
    "approved_by_user": false,
    "fingerprint": "sha256:0123456789abcdef0123456789abcdef"
  }
]
```
