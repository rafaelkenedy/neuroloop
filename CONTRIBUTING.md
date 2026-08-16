# Contribuindo

## Antes de tudo

O repositório foi inicializado com `git init` e **ainda não tem commits nem tags**. As
convenções abaixo são uma proposta, não uma dedução do histórico.

> **TODO(verificar):** confirmar convenção de branches e de mensagens de commit com o
> mantenedor. Assim que houver histórico, esta seção deve ser reescrita a partir dele.

## Ambiente

```
python -m venv .venv
```

```
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Banco de desenvolvimento, quando precisar rodar contra PostgreSQL:

```
docker compose up -d postgres
```

## Branches

Proposta:

| Tipo | Padrão | Exemplo |
|---|---|---|
| Funcionalidade | `feat/<escopo>` | `feat/semantic-memory` |
| Correção | `fix/<escopo>` | `fix/retry-counter` |
| Documentação | `docs/<escopo>` | `docs/api-reference` |
| Refatoração | `refactor/<escopo>` | `refactor/persistence-layer` |

`main` é a branch de integração. Não trabalhe direto nela.

## Mensagens de commit

Proposta: [Conventional Commits](https://www.conventionalcommits.org/), com escopo igual
ao módulo tocado.

```
feat(memory): plan cache indexado por assinatura de objetivo

Sem mecanismo determinístico de reuso, H3 não é falsificável: qualquer ganho
medido no B5 poderia ser variação do modelo. Referência: C16.
```

Prefixos: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`.

Escopos que correspondem a módulos reais: `core`, `perception`, `context`, `cognition`,
`llm`, `tools`, `security`, `verification`, `memory`, `runtime`, `persistence`,
`observability`, `api`, `bench`.

Explique **por que**, não o quê — o diff já mostra o quê.

## Testes

Suíte completa:

```
.venv/Scripts/python.exe -m pytest
```

Contra PostgreSQL, antes de mexer em persistência:

```
NEUROLOOP_TEST_DATABASE_URL=postgresql+psycopg://neuroloop:neuroloop@localhost:5432/neuroloop .venv/Scripts/python.exe -m pytest
```

Um teste isolado:

```
.venv/Scripts/python.exe -m pytest tests/unit/test_verifier.py::TestConclusaoDeGoal::test_delta_conclui
```

`filterwarnings = ["error"]` está ativo: um warning novo quebra a suíte. Isso é
intencional.

## Teste contra modelo real (opcional)

A suíte inteira usa `FakeLLMClient`. Isso é proposital: medir o runtime exige a
variável do modelo fixa. Mas significa que as portas de validação só veem saída
escrita por quem escreveu o teste.

`scripts/live_local_model.py` roda o runtime contra um servidor
OpenAI-compatível (LM Studio, Ollama, vLLM). Não entra na suíte: exige servidor
externo, é lento e não é determinístico.

```
.venv/Scripts/python.exe scripts/live_local_model.py --runs 6 --model "google/gemma-4-12b-qat"
```

O critério não é "o modelo acertou". É `falso_sucesso: 0` e `excecao_vazou: 0` —
um modelo fraco gera entrada adversarial de graça, e o runtime precisa recusar
em vez de executar lixo ou quebrar.

Três defeitos reais saíram daí (C23, C24, C26), nenhum detectável pela suíte:
todos viviam na costura entre instrução, renderização e validação, onde o
`FakeLLMClient` é cego por construção.

Medições, configuração e o que ajustar em hardware melhor estão em
[`docs/TESTE_MODELO_LOCAL.md`](docs/TESTE_MODELO_LOCAL.md).

## Lint e formatação

`pyproject.toml` configura `ruff` (linha 100, regras `E,F,I,UP,B,SIM`), mas a ferramenta
não está declarada em nenhum grupo de dependências.

> **TODO(verificar):** decidir entre adicionar `ruff` ao extra `dev` ou padronizar
> instalação via `pipx`/`uvx`. Enquanto isso não for resolvido, `ruff check .` depende
> de instalação manual.

Não há formatador configurado (`ruff format`, `black` ou equivalente).

> **TODO(verificar):** definir se o projeto adota formatador automático.

## Alterações em persistência

Modelos e migrations não podem divergir — há teste que falha o build se divergirem
(`tests/integration/test_migrations.py::test_migrations_nao_divergem_dos_modelos`).

Ao alterar `src/neuroloop/persistence/models.py`:

```
NEUROLOOP_DATABASE_URL=sqlite+aiosqlite:///./_tmp.db .venv/Scripts/python.exe -m alembic revision --autogenerate -m "descricao curta"
```

Revise a migration gerada. O autogenerate produz dois padrões que precisam de ajuste
manual, ambos já corrigidos nas migrations existentes:

- `sa.JSON().with_variant(postgresql.JSONB(...), 'postgresql')` deve virar `JsonType`,
  importado de `neuroloop.persistence.models`;
- `sa.text('(CURRENT_TIMESTAMP)')` deve virar `sa.func.now()`, para não fixar dialeto.

Depois apague o banco temporário e rode a suíte nos dois bancos.

## Regras que o código impõe e o PR precisa respeitar

Estas não são preferências de estilo. Cada uma tem teste que falha se for violada:

| Regra | Onde vive |
|---|---|
| `tests/benchmarks/oracles.py` não importa nada de `neuroloop`. | `test_oracle_independence.py` |
| Nenhum estado da state machine pode ficar órfão ou virar sumidouro. | `test_state_machine.py` |
| A tabela de transições no teste espelha a de `02_correcoes_spec.md`. | `test_state_machine.py` |
| Migrations não divergem dos modelos. | `test_migrations.py` |
| Todo schema de saída do LLM proíbe campos extras. | `test_json_schema.py` |

Se um PR precisa mudar uma dessas, a mudança é de arquitetura e merece justificativa no
corpo do PR.

## Checklist antes de abrir PR

- [ ] `pytest` passa em SQLite.
- [ ] `pytest` passa em PostgreSQL, se o PR toca persistência, runtime ou migrations.
- [ ] Alteração em modelo veio acompanhada de migration, e `alembic check` está limpo.
- [ ] Comportamento novo tem teste que falha sem a mudança.
- [ ] Nenhum segredo, token ou URL privada no diff.
- [ ] Documentação atualizada se contratos, variáveis de ambiente ou rotas mudaram.
- [ ] Se o PR desvia da especificação, o desvio está registrado em
      `02_correcoes_spec.md` com o motivo.

## Onde ler antes de mudar

| Você vai mexer em | Leia antes |
|---|---|
| Qualquer coisa | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Verificação, critérios, conclusão de goal | C01 e C02 em [`02_correcoes_spec.md`](02_correcoes_spec.md) |
| Executor, retry, idempotência | C04, C05, C08, C09 |
| Policy, taint, aprovação | C10, C19 |
| Memória e reuso | C14, C16 |
| Benchmarks e métricas | C17, C18 |
