# MISSÃO

Atue como uma combinação de:

- Principal AI Systems Architect
- Staff Software Engineer
- Agentic Systems Engineer
- LLM Engineer
- Distributed Systems Engineer
- Product Architect
- Security Engineer
- SRE
- Research Engineer especializado em agentes autônomos

Sua tarefa é **projetar o MVP implementável de um sistema de agente cognitivo autônomo baseado na arquitetura conceitual descrita abaixo**.

Não quero apenas ideias, analogias ou uma arquitetura conceitual.

Quero chegar a uma especificação suficientemente concreta para um engenheiro começar a implementar o sistema.

O objetivo é construir **a menor versão funcional capaz de demonstrar o loop cognitivo completo do agente**:

**perceber → selecionar contexto → decidir → planejar ou executar → usar ferramentas → verificar resultado → atualizar memória → continuar.**

---

# 1. ARQUITETURA CONCEITUAL ATUAL

O sistema proposto possui os seguintes componentes:

```
                           ┌──────────────────────────┐
                           │   OBJETIVOS / DRIVES     │
                           │ prioridade • risco       │
                           │ urgência • recompensa    │
                           └────────────┬─────────────┘
                                        │
                                        ▼
┌────────────┐     ┌──────────────┐   ┌─────────────────────┐
│ Ambiente   │────▶│  Percepção   │──▶│ GLOBAL WORKSPACE    │
│ usuário    │     │ normalização │   │ estado consciente   │
│ APIs       │     └──────────────┘   │ atual do agente     │
│ filesystem │                         └─────────┬───────────┘
└─────▲──────┘                                   │
      │                                          ▼
      │                              ┌───────────────────────┐
      │                              │ EXECUTIVE CONTROLLER  │
      │                              │ "pré-frontal"         │
      │                              └───────┬───────────────┘
      │                                      │
      │                         ┌────────────┴────────────┐
      │                         │                         │
      │                         ▼                         ▼
      │                 ┌───────────────┐         ┌───────────────┐
      │                 │ FAST PATH     │         │ SLOW PATH     │
      │                 │ hábitos       │         │ planejamento  │
      │                 │ skills        │         │ simulação     │
      │                 └──────┬────────┘         └──────┬────────┘
      │                        │                         │
      │                        └────────────┬────────────┘
      │                                     ▼
      │                              ┌─────────────┐
      │                              │ EXECUTOR    │
      │                              │ ferramentas │
      │                              │ subagentes  │
      │                              └──────┬──────┘
      │                                     │
      │                                     ▼
      │                              ┌─────────────┐
      └──────────────────────────────│ VERIFIER    │
                                     │ consequência│
                                     │ erro/reward │
                                     └──────┬──────┘
                                            │
                ┌───────────────────────────┼────────────────────┐
                ▼                           ▼                    ▼
         ┌─────────────┐             ┌──────────────┐     ┌──────────────┐
         │ EPISÓDICA   │             │ SEMÂNTICA    │     │ PROCEDURAL   │
         │ o que houve │             │ o que sabemos│     │ como fazer   │
         └──────┬──────┘             └──────┬───────┘     └──────┬───────┘
                └───────────────────────────┼────────────────────┘
                                            ▼
                                  ┌──────────────────┐
                                  │ CONSOLIDAÇÃO     │
                                  │ "sono/replay"    │
                                  └──────────────────┘

```

---

# 2. OBJETIVO DO MVP

O MVP NÃO precisa reproduzir cognição humana.

O objetivo é demonstrar que esta arquitetura consegue produzir um agente de software que:

1. recebe um objetivo;
2. percebe informações externas;
3. mantém um estado operacional coerente;
4. decide o que merece atenção;
5. escolhe entre execução direta e planejamento;
6. executa ferramentas;
7. observa consequências;
8. detecta sucesso ou erro;
9. ajusta sua estratégia;
10. registra experiências;
11. recupera memória relevante;
12. reutiliza procedimentos bem-sucedidos;
13. mantém continuidade durante tarefas compostas;
14. encerra quando o objetivo for atingido ou quando não for seguro continuar.

O MVP deve provar o **loop cognitivo**, e não tentar implementar todas as ideias avançadas possíveis.

---

# 3. REGRA FUNDAMENTAL

Não trate nomes como:

- Global Workspace;
- Executive Controller;
- Fast Path;
- Slow Path;
- Episodic Memory;
- Semantic Memory;
- Procedural Memory;
- Drives;
- Consolidation;

como especificações suficientes.

Eles são apenas conceitos.

Para cada conceito, traduza-o para:

- responsabilidade concreta;
- inputs;
- outputs;
- estado;
- interfaces;
- algoritmos;
- schemas;
- persistência;
- critérios de decisão;
- limites;
- erros;
- observabilidade.

Se dois componentes puderem ser unidos no MVP sem perder a hipótese que queremos testar, recomende a simplificação.

---

# 4. NÃO ANTROPOMORFIZE SEM NECESSIDADE

Analise criticamente as analogias cerebrais.

Por exemplo:

"Global Workspace" precisa virar alguma estrutura concreta como:

```
AgentState
WorkingContext
Blackboard
TaskContext

```

"Drives" precisam virar variáveis computáveis.

"Consolidação durante sono" precisa virar jobs ou processos concretos.

"Memória episódica" precisa virar eventos persistidos e recuperáveis.

"Memória procedural" precisa virar skills, policies, recipes ou workflows reutilizáveis.

Sempre diferencie:

**metáfora cognitiva**

de

**mecanismo de software**.

---

# 5. PRIMEIRO: ENCONTRE OS GAPS

Antes de desenhar a solução, faça uma revisão crítica da arquitetura.

Crie:

# GAPS DA ARQUITETURA ATUAL

Procure especificamente respostas ausentes para questões como:

### Agent loop

- O que inicia um ciclo?
- O agente é event-driven ou possui loop contínuo?
- Quando um ciclo termina?
- Existe tick?
- Existe scheduler?
- O agente pode ficar ocioso?

### Objetivos

- Como objetivos são representados?
- Existe objetivo principal?
- Existem subobjetivos?
- Como prioridades competem?
- Como detectar conflito de objetivos?
- Um objetivo pode expirar?
- Como detectar conclusão?

### Drives

Defina matematicamente ou proceduralmente:

- prioridade;
- urgência;
- risco;
- recompensa;
- custo;
- confiança.

Como estes valores influenciam decisões?

### Global Workspace

- Qual exatamente é seu schema?
- O que entra nele?
- O que fica fora?
- Existe limite de tamanho?
- Como informações são removidas?
- Como atenção funciona?
- Como memória é recuperada para dentro dele?

### Executive Controller

- É código determinístico?
- É LLM?
- É híbrido?
- Que decisões ele toma?
- O que ele NÃO pode decidir?

### Fast Path

- O que qualifica uma tarefa para fast path?
- É rule engine?
- Skill?
- Workflow?
- Tool call direta?
- LLM pequeno?
- Cache de decisões?

### Slow Path

- O que dispara planejamento?
- Como planos são representados?
- Existe replanning?
- Existe simulação?
- Quantas etapas um plano pode ter?
- Quando abandonar um plano?

### Executor

- Como tools são registradas?
- Como argumentos são validados?
- Como permissões são aplicadas?
- Como efeitos destrutivos são tratados?
- Como evitar execução duplicada?

### Verifier

Este é um dos componentes mais importantes.

Determine:

- como sabe o resultado esperado;
- como verifica sucesso;
- como distingue tool success de task success;
- como calcula erro;
- como calcula reward;
- quando pede nova tentativa;
- quando replana;
- quando desiste.

### Memória

- quando escrever;
- o que escrever;
- o que não escrever;
- como recuperar;
- como rankear;
- como esquecer;
- como atualizar informação incorreta;
- como lidar com contradições.

### Consolidação

- o que exatamente consolida;
- quando roda;
- como transforma episódios em conhecimento;
- como cria procedures;
- como evita aprender um padrão errado.

Não prossiga sem destacar os principais gaps.

---

# 6. DEFINA O LOOP CENTRAL

Proponha um algoritmo explícito para o agente.

Algo conceitualmente parecido com:

```
while agent_active:

    events = perceive(environment)

    state = update_workspace(
        events,
        goals,
        relevant_memory
    )

    decision = executive_controller(state)

    if decision.mode == FAST:
        action = fast_path(decision)
    else:
        plan = slow_path(decision)
        action = select_next_action(plan)

    result = executor.execute(action)

    verification = verifier.evaluate(
        expected=action.expected_result,
        observed=result
    )

    update_state(verification)

    store_episode(...)

    if verification.requires_replan:
        ...

    if goal_completed:
        stop

```

Não copie isso cegamente.

Melhore-o.

Entregue pseudocódigo suficientemente preciso para virar código.

Inclua:

- estados;
- transições;
- retry;
- timeout;
- tool error;
- replanning;
- human intervention;
- objective completion;
- impossible task;
- cancellation.

---

# 7. DEFINA O AGENT STATE

Projete o schema central do agente.

Por exemplo:

```
interface AgentState {
  agentId: string

  currentGoal: Goal
  subgoals: Goal[]

  workspace: WorkspaceItem[]

  activePlan?: Plan

  currentStep?: PlanStep

  pendingActions: Action[]

  observations: Observation[]

  recentEpisodes: Episode[]

  retrievedMemories: Memory[]

  confidence: number

  riskLevel: number

  status: AgentStatus

  iteration: number

  tokenBudget?: number
  costBudget?: number

  createdAt: Date
  updatedAt: Date
}

```

Não considere este schema correto.

Analise e proponha o melhor schema para o MVP.

Diferencie claramente:

- estado volátil;
- estado persistente;
- estado derivado;
- estado que vai para o prompt do LLM.

---

# 8. OBJETIVOS E SUBOBJETIVOS

Projete uma representação concreta de objetivos.

Cada Goal deve considerar, se necessário:

```
id
description
parentGoal
priority
urgency
risk
expectedReward
status
constraints
successCriteria
failureCriteria
deadline
dependencies

```

Defina uma máquina de estados.

Por exemplo:

```
pending
active
blocked
completed
failed
cancelled

```

Explique como:

- objetivos são criados;
- objetivos são priorizados;
- objetivos competem;
- objetivos são concluídos;
- subobjetivos surgem;
- objetivos são cancelados.

---

# 9. GLOBAL WORKSPACE

Projete o Global Workspace concretamente.

Responda:

### O que é?

Uma estrutura em memória?

Uma tabela?

Um contexto serializado?

Um blackboard?

Um subset do AgentState?

### O que pode entrar?

- objetivo atual;
- observações;
- hipóteses;
- memória recuperada;
- plano;
- resultado recente;
- erros;
- riscos;
- contexto do usuário.

### Atenção

Crie um mecanismo mínimo para decidir o que ganha atenção.

Considere algo como:

```
salience =
  goal_relevance
  + urgency
  + novelty
  + risk
  + recency

```

Mas questione e melhore essa ideia.

Não use ML desnecessariamente no MVP.

---

# 10. EXECUTIVE CONTROLLER

Determine exatamente o papel do controller.

Ele pode decidir coisas como:

```
ACT
PLAN
RETRIEVE_MEMORY
ASK_USER
RETRY
REPLAN
WAIT
DELEGATE
STOP

```

Proponha uma estrutura de decisão tipada.

Exemplo:

```
type ExecutiveDecision =
  | { type: "ACT"; action: Action }
  | { type: "PLAN"; reason: string }
  | { type: "RETRIEVE_MEMORY"; query: string }
  | { type: "ASK_USER"; question: string }
  | { type: "STOP"; reason: string }

```

Determine quais decisões devem ser:

- determinísticas;
- LLM-driven;
- híbridas.

Não coloque no LLM decisões que podem ser resolvidas com regras simples e seguras.

---

# 11. FAST PATH

Projete o Fast Path do MVP.

Hipótese:

Ele deve executar comportamentos já conhecidos sem realizar planejamento completo.

Pode incluir:

- skills;
- actions conhecidas;
- templates;
- workflows;
- tool call direta;
- procedimentos recuperados da memória procedural.

Determine critérios claros para selecionar Fast Path.

Exemplo conceitual:

```
skill existente
AND alta confiança
AND baixo risco
AND parâmetros completos
→ Fast Path

```

Defina fallback para Slow Path.

---

# 12. SLOW PATH

Projete o planejamento deliberativo.

Determine:

- formato do Plan;
- formato do PlanStep;
- dependências;
- preconditions;
- expected outcomes;
- tool desejada;
- custo;
- risco.

Exemplo:

```
interface PlanStep {
  id: string
  description: string
  tool?: string

  preconditions: Condition[]
  expectedOutcome: string

  status: StepStatus

  dependencies: string[]
}

```

Defina:

- geração do plano;
- validação do plano;
- execução incremental;
- replanning.

Evite planos gigantes.

---

# 13. TOOLS

Projete um Tool Registry.

Cada ferramenta deve possuir algo próximo de:

```
interface ToolDefinition {
  name: string
  description: string

  inputSchema: JSONSchema
  outputSchema?: JSONSchema

  riskLevel: RiskLevel

  sideEffects: boolean

  requiresConfirmation: boolean

  timeout: number

  idempotent: boolean
}

```

Defina:

- descoberta;
- seleção;
- validação;
- autorização;
- execução;
- timeout;
- retry;
- logs;
- auditoria.

Considere inicialmente:

- filesystem;
- shell restrito;
- HTTP/API;
- search;
- memória.

---

# 14. VERIFIER

Trate o Verifier como componente de primeira classe.

Não assuma:

```
tool retornou 200 → tarefa concluída

```

Separe:

### Execution verification

A ferramenta funcionou?

### State verification

O estado externo realmente mudou?

### Goal verification

Isso aproxima ou conclui o objetivo?

### Safety verification

O resultado gerou alguma condição perigosa?

Projete algo como:

```
interface VerificationResult {
  executionSuccess: boolean
  expectedOutcomeSatisfied: boolean

  confidence: number

  error?: string

  rewardSignal: number

  nextAction:
    | "CONTINUE"
    | "RETRY"
    | "REPLAN"
    | "ASK_USER"
    | "STOP"
}

```

Determine quando usar:

- código determinístico;
- consulta ao ambiente;
- LLM-as-judge;
- testes;
- schemas;
- comparação de estado.

Use LLM-as-judge apenas onde necessário.

---

# 15. MEMÓRIA EPISÓDICA

Defina episódios.

Um episódio pode conter:

```
objetivo
contexto
decisão
ação
tool
resultado
verificação
reward
erro
timestamp

```

Determine:

- granularidade;
- persistência;
- retenção;
- busca;
- embeddings ou não;
- resumo.

O MVP não precisa guardar todo token produzido pelo agente.

---

# 16. MEMÓRIA SEMÂNTICA

Defina conhecimento persistente.

Exemplos:

```
fatos aprendidos
preferências do usuário
entidades
relações
constraints
informações verificadas

```

Projete mecanismo para:

- provenance;
- confidence;
- timestamp;
- atualização;
- contradições;
- invalidação.

Evite transformar simplesmente todo histórico em embeddings.

---

# 17. MEMÓRIA PROCEDURAL

Defina como procedimentos reutilizáveis funcionam.

Um procedimento poderia ser:

```
interface Skill {
  id: string

  trigger: string

  preconditions: Condition[]

  steps: SkillStep[]

  successCriteria: Condition[]

  confidence: number

  usageCount: number

  successRate: number
}

```

Defina:

- como nasce;
- como é encontrado;
- como é executado;
- como é atualizado;
- quando deixa de ser confiável.

Para o MVP, considere se skills inicialmente devem ser cadastradas manualmente em vez de aprendidas automaticamente.

---

# 18. CONSOLIDAÇÃO

Traduza "sono/replay" em software.

Avalie uma implementação como background job que:

1. analisa episódios;
2. agrupa padrões;
3. identifica fatos;
4. produz resumos;
5. promove informação para memória semântica;
6. identifica procedimentos recorrentes;
7. atualiza skills.

Entretanto:

**não implemente aprendizagem autônoma complexa no MVP sem necessidade.**

Determine qual versão mínima da consolidação seria suficiente.

Pode ser apenas:

```
episódios → resumo de sessão

```

no MVP.

---

# 19. PERCEPÇÃO

Projete uma camada normalizadora.

Diferentes fontes:

```
mensagem do usuário
tool result
filesystem event
API result
timer
system event
subagent response

```

devem virar um tipo comum de evento.

Proponha schema:

```
interface Observation {
  id: string

  source: string
  type: string

  content: unknown

  timestamp: Date

  relevance?: number
  confidence?: number

  metadata?: Record<string, unknown>
}

```

Melhore-o se necessário.

---

# 20. EVENT MODEL

Defina os principais eventos internos.

Exemplos:

```
GoalCreated
GoalActivated

ObservationReceived

MemoryRetrieved

DecisionMade

PlanCreated
PlanChanged

ActionRequested
ActionStarted
ActionCompleted
ActionFailed

VerificationCompleted

GoalCompleted
GoalFailed

EpisodeStored

```

Determine se o MVP precisa realmente de event sourcing.

Por padrão:

**NÃO.**

Mas mantenha eventos estruturados para tracing.

---

# 21. SEGURANÇA

Este agente pode executar ações.

Portanto modele:

### Capability security

Cada tool deve declarar o que pode fazer.

### Permissions

Quais recursos o agente pode acessar?

### Risk tiers

Exemplo:

```
R0 → leitura
R1 → alteração reversível
R2 → alteração significativa
R3 → destrutivo / financeiro / publicação

```

### Human approval

Determine ações que exigem aprovação explícita.

### Prompt injection

Considere que:

```
websites
arquivos
APIs
documentos
tool outputs

```

podem conter instruções maliciosas.

Dados externos nunca devem automaticamente adquirir autoridade sobre o agente.

Projete separação entre:

- instructions;
- observations;
- tool outputs;
- memory.

---

# 22. PREVENÇÃO DE LOOPS

Projete mecanismos contra:

- agent loop infinito;
- retry infinito;
- replanning infinito;
- tool calls repetidas;
- ciclos entre subobjetivos;
- consumo excessivo de tokens;
- consumo excessivo de dinheiro.

Inclua:

```
max_iterations
max_retries
max_replans
token_budget
cost_budget
wall_clock_timeout
duplicate_action_detection

```

---

# 23. IDEMPOTÊNCIA

Um agente pode tentar novamente uma ação.

Defina como evitar:

- enviar mensagem duas vezes;
- pagar duas vezes;
- criar recurso duas vezes;
- excluir duas vezes;
- executar side effect duplicado.

Projete:

```
action_id
idempotency_key
execution_status

```

---

# 24. OBSERVABILIDADE

Preciso conseguir responder:

**Por que o agente fez isso?**

Crie tracing por ciclo.

Cada ciclo deve registrar aproximadamente:

```
observation
workspace
goal
retrieved memory
decision
reason
plan
action
tool
result
verification
next state
cost
latency

```

Mas diferencie:

### internal telemetry

de

### contexto enviado ao LLM.

Não envie tudo ao modelo indiscriminadamente.

---

# 25. LLM BOUNDARIES

Defina exatamente quais módulos precisam de LLM.

Para cada chamada, especifique:

```
propósito
input
output estruturado
modelo recomendado
failure mode
fallback

```

Tente minimizar número de chamadas.

Questione se precisamos de LLM para:

- percepção;
- ranking;
- controller;
- planejamento;
- tool selection;
- verification;
- memory extraction;
- consolidation.

Não assuma que tudo precisa de um LLM.

---

# 26. STRUCTURED OUTPUT

Toda decisão crítica de LLM deve utilizar saída estruturada.

Projete schemas JSON/Pydantic/Zod apropriados.

Não dependa de parsing de texto livre para controlar o sistema.

---

# 27. CONTEXT MANAGEMENT

Projete uma estratégia para controlar a janela de contexto.

Diferencie:

```
system instructions
goal
working state
recent observations
active plan
retrieved memory
tool definitions
recent tool results

```

Defina:

- prioridades;
- limites;
- truncation;
- summaries;
- retrieval.

O Global Workspace NÃO deve significar "jogar tudo no prompt".

---

# 28. SUBAGENTES

Questione se subagentes são necessários no MVP.

Default:

**não usar**, salvo se apresentarem uma vantagem concreta.

Se forem necessários, defina:

- criação;
- escopo;
- contexto;
- permissões;
- comunicação;
- resultado;
- cancelamento;
- máximo de subagentes.

Evite swarm architecture no MVP.

---

# 29. PERSISTÊNCIA

Projete o mínimo necessário.

Considere inicialmente:

```
PostgreSQL
+
pgvector somente se retrieval semântico realmente precisar

```

Defina tabelas mínimas.

Prováveis candidatos:

```
agents
goals
agent_runs
plans
plan_steps
actions
tool_executions
observations
episodes
semantic_memories
skills

```

Questione cada tabela.

Elimine o que puder ser JSON ou estado transitório no MVP.

---

# 30. ARQUITETURA FÍSICA

Transforme tudo em componentes deployáveis.

Priorize algo simples:

```
┌─────────────────┐
│ Client / API    │
└────────┬────────┘
         ↓
┌──────────────────────────┐
│ Agent Runtime            │
│                          │
│ Cognitive Loop           │
│ Controller               │
│ Planner                  │
│ Executor                 │
│ Verifier                 │
│ Memory Manager           │
└───────────┬──────────────┘
            │
      ┌─────┴─────┐
      ↓           ↓
 PostgreSQL     Tool adapters

```

Não crie microservices para cada módulo cognitivo.

Eles devem inicialmente ser **módulos do mesmo runtime**.

---

# 31. STACK DO MVP

Sugira uma stack concreta.

Avalie prioritariamente:

### Linguagem

Python versus TypeScript.

### API

FastAPI, se Python.

### Schemas

Pydantic.

### Database

PostgreSQL.

### ORM

SQLAlchemy / SQLModel ou alternativa.

### LLM abstraction

Avalie se realmente precisamos de framework.

Compare:

- SDK direto do provider;
- abstração mínima própria;
- LangGraph;
- outros frameworks.

Por padrão, prefira **menos abstração** no núcleo cognitivo para que o comportamento seja observável.

### Queue

Não adicionar até existir necessidade.

### Cache

Não adicionar até existir necessidade.

### Vector DB separado

Não adicionar até existir necessidade.

---

# 32. MVP EXATO

Depois de toda análise, responda:

# O MENOR SISTEMA QUE PROVA A HIPÓTESE

Quero explicitamente saber quais componentes implementar na V0.

Considere uma V0 aproximadamente composta por:

```
Goal Manager
Perception normalizer
Agent State / Workspace
Executive Controller
Simple Planner
Tool Registry
Executor
Verifier
Episodic memory
Basic memory retrieval
Trace/log

```

Questione se todos são necessários.

Depois determine:

## V0

absolutamente necessário.

## V0.5

logo após validação.

## V1

após provar o conceito.

---

# 33. O QUE NÃO IMPLEMENTAR AGORA

Crie uma seção explícita:

# NÃO CONSTRUIR NO MVP

Analise especialmente:

- multi-agent swarm;
- auto-evolução;
- automatic skill synthesis;
- reinforcement learning;
- knowledge graph;
- vector database separado;
- microservices;
- Kubernetes;
- distributed agents;
- complex event sourcing;
- continual fine-tuning;
- autonomous code modification;
- sophisticated simulations;
- long-term autonomous operation;
- biologically realistic cognitive architecture.

Se algum deles realmente for indispensável, justifique.

---

# 34. TESTE DE VALIDAÇÃO DO MVP

O MVP precisa demonstrar a arquitetura.

Proponha 3 a 5 benchmarks/tarefas.

Os testes devem exigir:

- múltiplas etapas;
- uso de ferramenta;
- memória;
- erro;
- replanning;
- verificação.

Exemplo conceitual:

```
Objetivo:
encontre determinado arquivo,
extraia informação,
consulte uma API,
produza um resultado,
salve um artefato.

Durante a execução:
uma ferramenta falha.

O agente deve:
detectar,
adaptar,
continuar,
verificar o resultado.

```

Crie benchmarks melhores e mensuráveis.

---

# 35. MÉTRICAS

Defina métricas como:

```
task_success_rate
goal_completion_rate

tool_failure_recovery_rate

unnecessary_tool_calls

average_iterations_per_goal

replan_rate

false_success_rate

memory_retrieval_precision

fast_path_success_rate

average_cost_per_goal

average_latency_per_goal

```

Destaque especialmente:

## False Success Rate

Quantas vezes o agente acredita que terminou quando na realidade não terminou?

Essa deve ser uma métrica central para avaliar o Verifier.

---

# 36. FAILURE TAXONOMY

Crie uma taxonomia de falhas.

Por exemplo:

```
PERCEPTION_ERROR
MEMORY_ERROR
REASONING_ERROR
PLANNING_ERROR
TOOL_SELECTION_ERROR
TOOL_EXECUTION_ERROR
VERIFICATION_ERROR
STATE_ERROR
PERMISSION_ERROR
TIMEOUT
BUDGET_EXCEEDED
LOOP_DETECTED

```

Determine como cada erro afeta a próxima decisão.

---

# 37. STATE MACHINE

Projete uma máquina de estados explícita.

Algo possivelmente como:

```
IDLE
 ↓
PERCEIVING
 ↓
DELIBERATING
 ↓
PLANNING / ACTING
 ↓
EXECUTING
 ↓
VERIFYING
 ↓
UPDATING_MEMORY
 ↓
DELIBERATING

```

Com saídas:

```
COMPLETED
FAILED
BLOCKED
WAITING_USER
CANCELLED

```

Produza:

1. state machine;
2. tabela de transições;
3. Mermaid diagram.

---

# 38. ESTRUTURA DO REPOSITÓRIO

Proponha uma organização de código concreta.

Algo como:

```
src/
  agent/
    runtime.py
    state.py
    controller.py

  cognition/
    fast_path.py
    planner.py
    verifier.py

  goals/
    models.py
    manager.py

  memory/
    episodic.py
    semantic.py
    procedural.py
    retrieval.py

  tools/
    registry.py
    executor.py
    adapters/

  perception/
    normalizer.py

  models/
    ...

  persistence/
    ...

  observability/
    ...

  api/
    ...

```

Não copie essa estrutura automaticamente.

Projete a melhor versão.

---

# 39. INTERFACES

Defina as interfaces centrais em pseudo-Python ou TypeScript.

No mínimo:

```
Perception
GoalManager
Workspace
ExecutiveController
FastPath
Planner
ToolRegistry
Executor
Verifier
MemoryStore
MemoryRetriever
AgentRuntime

```

Quero métodos, inputs e outputs.

---

# 40. DATABASE SCHEMA

Apresente um schema simplificado.

Para cada entidade:

```
primary key
foreign keys
campos
indexes
JSON fields
timestamps

```

Evite normalização excessiva.

---

# 41. SEQUÊNCIA COMPLETA

Mostre um exemplo real de uma execução.

Use uma tarefa composta.

Mostre etapa por etapa:

```
1. objetivo criado
2. percepção
3. workspace
4. controller
5. memory retrieval
6. decisão
7. plano
8. ação
9. tool call
10. resultado
11. verifier
12. memória
13. próxima decisão
...
14. objetivo concluído

```

Quero visualizar exatamente como os componentes interagem.

---

# 42. ANÁLISE DE GAPS FINAL

Depois de projetar tudo, faça novamente uma análise crítica.

Pergunte:

- Existe componente conceitual sem implementação concreta?
- Existe responsabilidade duplicada?
- Controller e Planner estão sobrepostos?
- Workspace está virando apenas um nome bonito para prompt?
- Verifier consegue realmente observar consequências?
- Reward possui algum uso concreto?
- Fast Path está realmente economizando raciocínio?
- Memória procedural é necessária na V0?
- Consolidação é necessária na V0?
- Como conhecimento errado é corrigido?
- Como impedir loops?
- Como impedir ações perigosas?
- Como saber quando o objetivo terminou?
- Como retomar uma execução interrompida?
- Como debugar uma decisão?
- Como reproduzir uma execução?
- Como testar cada módulo isoladamente?

Corrija a arquitetura antes da resposta final.

---

# 43. FORMATO OBRIGATÓRIO DA RESPOSTA

Entregue nesta ordem:

1. **Interpretação da arquitetura**
2. **Hipótese que o MVP precisa provar**
3. **Gaps encontrados**
4. **Simplificações recomendadas**
5. **Arquitetura final do MVP**
6. **Responsabilidade de cada componente**
7. **Agent loop**
8. **State machine**
9. **Global Workspace / Agent State**
10. **Goal system**
11. **Executive Controller**
12. **Fast Path**
13. **Slow Path / Planner**
14. **Tool system**
15. **Verifier**
16. **Memória episódica**
17. **Memória semântica**
18. **Memória procedural**
19. **Consolidação**
20. **Context management**
21. **LLM boundaries**
22. **Safety / permissions**
23. **Persistência**
24. **Schemas principais**
25. **Interfaces**
26. **Stack**
27. **Estrutura do repositório**
28. **Exemplo completo de execução**
29. **Testes**
30. **Benchmarks**
31. **Métricas**
32. **Failure taxonomy**
33. **Observabilidade**
34. **V0 / V0.5 / V1**
35. **O que NÃO construir**
36. **Backlog de implementação**
37. **Riscos**
38. **Gaps ainda não resolvidos**

---

# 44. BACKLOG IMPLEMENTÁVEL

No final transforme a V0 em tarefas.

Formato:

## TASK-001 — Agent State

**Objetivo**

Implementar o estado central do agente.

**Arquivos prováveis**

```
src/agent/state.py
...

```

**Implementação**

...

**Critérios de aceite**

-  ...
-  ...
-  ...

**Testes**

-  ...

**Dependências**

...

Faça isso para cada módulo da V0.

Ordene as tasks exatamente na sequência mais eficiente de implementação.

---

# 45. REGRAS PARA SUAS DECISÕES

Durante toda a análise:

### Prefira

- código simples;
- funções puras;
- state machines explícitas;
- schemas tipados;
- outputs estruturados;
- PostgreSQL;
- interfaces simples;
- logs estruturados;
- determinismo quando possível;
- LLM somente onde agrega valor;
- componentes testáveis isoladamente.

### Evite

- overengineering;
- frameworks mágicos;
- abstrações prematuras;
- agentes chamando agentes sem necessidade;
- decisões críticas escondidas em prompts;
- estado implícito;
- side effects não rastreados;
- prompts gigantes;
- memória ilimitada;
- execução sem verificação;
- "LLM decide tudo".

---

# PRINCÍPIO FINAL

O sistema deve ser projetado em torno desta ideia:

> **O LLM não é o agente. O LLM é um componente de raciocínio dentro de um runtime de agente controlado por estado, regras, ferramentas, memória, verificação e limites.**

Toda decisão arquitetural deve respeitar esse princípio.

Quando houver escolha entre:

**mais inteligência emergente**

e

**mais controle, observabilidade e previsibilidade**

prefira controle no MVP.

Quando houver escolha entre:

**arquitetura cognitivamente elegante**

e

**arquitetura implementável, testável e mensurável**

prefira a segunda.

O MVP terá sido bem projetado se conseguirmos implementar o loop completo, executar tarefas reais e medir objetivamente onde o agente falha.