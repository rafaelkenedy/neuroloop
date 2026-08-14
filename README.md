# NeuroLoop

Runtime de agente cognitivo em que o LLM decide, mas não autoriza, não executa e não
declara conclusão. Voltado a quem precisa que um agente execute tarefas compostas com
efeitos auditáveis, verificação independente do resultado e limites explícitos.

Princípio central, aplicado em todo o código: **o LLM é um componente dentro de um
runtime controlado por estado, políticas, memória, ferramentas, verificação e limites.**

## Stack

| Camada | Tecnologia | Versão mínima |
|---|---|---|
| Linguagem | Python | 3.13 |
| Build | hatchling | — |
| Schemas e validação | Pydantic | 2.9 |
| Persistência | SQLAlchemy (async) | 2.0 |
| Migrations | Alembic | 1.13 |
| Driver de banco | psycopg (binary) | 3.2 |
| Banco | PostgreSQL | 17 (imagem do compose) |
| API HTTP | FastAPI | 0.115 |
| Validação de argumentos de tool | jsonschema | 4.20 |
| LLM (extra opcional `llm`) | anthropic | 0.116 |
| Testes | pytest, pytest-asyncio, aiosqlite, httpx | 8.0 / 0.24 / 0.20 / 0.27 |

Fonte: [`pyproject.toml`](pyproject.toml).

## Pré-requisitos

- Python 3.13 ou superior (`requires-python = ">=3.13"`).
- Docker e Docker Compose, para subir o PostgreSQL de desenvolvimento.
- A suíte de testes roda sem Docker: por padrão usa SQLite em arquivo temporário.

## Instalação

Crie o ambiente virtual:

```
python -m venv .venv
```

Instale o pacote em modo editável com as dependências de desenvolvimento:

```
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Em Linux ou macOS, o executável é `.venv/bin/python`.

Para usar o adapter real da Anthropic, instale também o extra `llm`:

```
.venv/Scripts/python.exe -m pip install -e ".[dev,llm]"
```

## Configuração

Todas as variáveis lidas pelo código, obtidas por busca de `os.environ` em `src/`,
`tests/` e `migrations/`:

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `NEUROLOOP_DATABASE_URL` | Não | `postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop` | URL do banco usada pelo runtime e pelas migrations. Lida em `persistence/session.py`. |
| `NEUROLOOP_TEST_DATABASE_URL` | Não | vazio (usa SQLite temporário) | Aponta a suíte de testes para um banco real. Lida apenas pelos testes. |
| `NEUROLOOP_BENCH_SEEDS` | Não | `3` | Número de execuções por benchmark B1–B5. |
| `ANTHROPIC_API_KEY` | Só com o extra `llm` | — | O código instancia `anthropic.AsyncAnthropic()` sem argumentos, então a credencial é resolvida pelo próprio SDK. Nenhum teste do repositório exercita esse caminho. |

O `docker-compose.yml` define as credenciais do PostgreSQL de desenvolvimento
(`neuroloop`/`neuroloop`/`neuroloop`). São valores locais, não segredos.

Copie o modelo antes de editar:

```
cp .env.example .env
```

## Como executar

### Banco de desenvolvimento

```
docker compose up -d postgres
```

Aplique as migrations:

```
NEUROLOOP_DATABASE_URL=postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop .venv/Scripts/python.exe -m alembic upgrade head
```

### API

`neuroloop.api.app.create_app` é uma **fábrica** que exige uma instância de
`AgentRuntime` e uma `session_factory`. Não existe módulo com um objeto `app` pronto,
nem entrada em `[project.scripts]`, nem referência a `uvicorn` no repositório. Não há,
portanto, comando de servidor documentável hoje.

> **TODO(verificar):** definir o processo de inicialização da API (montagem do
> `AgentRuntime`, escolha do `LLMClient`, registro de tools e sandbox) e expor um
> módulo servível por ASGI. Hoje a API só é executável em processo, como em
> `tests/integration/test_api.py`.

### Uso em processo

O caminho exercitado pelos testes é instanciar o runtime diretamente. Veja
`tests/integration/test_agent_runtime.py` para o fluxo `start` → `run_until_pause`, e
`tests/integration/test_api.py` para o mesmo fluxo atrás da API.

## Testes

Suíte completa (SQLite temporário, sem dependência externa):

```
.venv/Scripts/python.exe -m pytest
```

Contra PostgreSQL:

```
NEUROLOOP_TEST_DATABASE_URL=postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop .venv/Scripts/python.exe -m pytest
```

Um arquivo isolado:

```
.venv/Scripts/python.exe -m pytest tests/unit/test_verifier.py
```

Um teste isolado:

```
.venv/Scripts/python.exe -m pytest tests/unit/test_verifier.py::TestConclusaoDeGoal::test_delta_conclui
```

Benchmarks B1–B5 com amostragem completa (o padrão da suíte é `N=3`, por velocidade):

```
NEUROLOOP_BENCH_SEEDS=30 .venv/Scripts/python.exe -m pytest tests/benchmarks -m benchmark -s
```

Estado verificado na árvore atual: **598 testes passando em SQLite e em PostgreSQL**;
B1–B5 aprovados com `N=30`, sem falhas duras.

### Lint

O `pyproject.toml` configura `ruff` (`line-length = 100`, regras `E,F,I,UP,B,SIM`), mas
`ruff` não está declarado em nenhum grupo de dependências.

> **TODO(verificar):** adicionar `ruff` ao extra `dev` ou documentar instalação externa.
> Hoje `ruff check .` não roda a partir de uma instalação limpa do projeto.

## Estrutura do projeto

```
src/neuroloop/
  core/            schemas compartilhados; sem I/O, sem LLM, sem persistência
  perception/      entradas heterogêneas viram Observation com confiança atribuída
  context/         salience, WorkingContext e montagem do prompt
  cognition/       Deliberator, Fast Path, skills e validação de plano
  llm/             protocolo do provider, schemas de saída e adapter Anthropic
  tools/           registry versionado, sandbox e adapters de filesystem
  security/        gates, tiers de risco, taint e aprovação
  verification/    avaliação de Criterion e Verifier de quatro níveis
  memory/          episódios, retrieval, importância e plan cache
  runtime/         state machine, executor durável e o loop do agente
  persistence/     modelos, sessão async e repositórios
  observability/   trace, redação, explicação e métricas
  api/             rotas HTTP e contratos
migrations/        Alembic (0001_initial_schema, 0002_plan_cache)
tests/unit/        testes sem I/O
tests/integration/ testes com banco
tests/benchmarks/  oracles B1–B5, cenários e harness
```

Documentos do projeto:

| Arquivo | Conteúdo |
|---|---|
| [`00_prompt_original.md`](00_prompt_original.md) | Especificação original enviada ao projeto. |
| [`01_especificacao_mvp.md`](01_especificacao_mvp.md) | Especificação de engenharia do MVP. |
| [`02_correcoes_spec.md`](02_correcoes_spec.md) | 21 correções sobre a spec; lê-se junto com `01`, não no lugar dele. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Componentes, fluxo de dados e decisões técnicas. |
| [`docs/API.md`](docs/API.md) | Endpoints, payloads e erros. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Fluxo de trabalho e checklist de PR. |
| [`CHANGELOG.md`](CHANGELOG.md) | Histórico de versões. |

## Invariantes que o núcleo impõe

Estas regras vivem no schema ou no validador, não em comentário:

- Um `Goal` precisa de ao menos um critério de sucesso que observe **estado externo**.
  Objetivo verificável só pelo auto-relato da ferramenta é rejeitado na criação.
- `INDETERMINATE` (`satisfied=None`) nunca conta como sucesso, em nenhuma composição.
- `goal_satisfied=True` exige evidência de nível GOAL, integralmente observada,
  integralmente externa, com `safety_ok=True`.
- Goal só conclui com **delta**: satisfeito no baseline e satisfeito agora não é
  sucesso deste run.
- `timeout != action_failed`: com efeito colateral possível, timeout vira
  `UNKNOWN_EFFECT` e só o probe desempata.
- Retry exige idempotência **ou** prova de ausência do efeito.
- Argumento derivado de conteúdo `UNTRUSTED_EXTERNAL` não empresta autoridade: exige
  aprovação em R1 e é recusado em R2+.
- Aprovação humana vale para um `action_fingerprint`, não para a ação lógica.
- `WAITING_USER` vive no checkpoint: a espera sobrevive ao restart do processo.
- Estados terminais não reabrem; `EXECUTING` não alcança `COMPLETED` sem o Verifier.

## Deploy

Não há `Dockerfile`, workflow de CI nem definição de infraestrutura no repositório. O
`docker-compose.yml` provisiona apenas o PostgreSQL de desenvolvimento.

> **TODO(verificar):** definir alvo de deploy e empacotamento antes de documentar esta
> seção.

## Licença

Não há arquivo `LICENSE` no repositório e `pyproject.toml` não declara o campo
`license`.

> **TODO(verificar):** definir a licença do projeto.
