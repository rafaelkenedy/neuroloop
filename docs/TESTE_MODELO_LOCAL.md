# Teste contra modelo local

Registro do que foi medido ao exercitar o runtime contra modelos servidos pelo
LM Studio, e o que precisa mudar para repetir o teste em hardware melhor.

Nada aqui substitui os benchmarks B1–B5, que continuam com deliberador
roteirizado: medir o runtime exige a variável do modelo fixa. Este teste responde
outra pergunta — **como o runtime se comporta quando a saída do LLM não foi
escrita por quem escreveu o teste.**

## Por que vale a pena

Três defeitos reais apareceram por esta via, nenhum detectável pela suíte:

| | Defeito | Sintoma |
|---|---|---|
| [C23](../02_correcoes_spec.md) | Ids de observação ausentes do prompt | Modelo citava `'USER'` em `derived_from` — não havia UUID no prompt para citar |
| C24 | Nome de tool com versão colada | Modelo copiava `filesystem.write@1.0.0` da seção TOOLS; o registry resolve pelo nome puro |
| C26 | Classificação de erro descartada na tradução | Falha de proveniência reportada como `PLANNING_ERROR` |

Os três vivem na costura entre instrução, renderização e validação. Cada
componente passava nos próprios testes; o conjunto discordava.

O `FakeLLMClient` não podia expô-los, e o motivo é estrutural: quem escreve a
saída do fake já conhece o formato aceito e a classificação esperada. As saídas
de teste trazem `tool="filesystem.write"` e `derived_from=[str(uuid4())]` — os
valores certos, que o prompt nunca ofereceu. **Um fake não testa o contrato entre
o que o prompt entrega e o que a validação exige.**

## Como rodar

```
.venv/Scripts/python.exe scripts/live_local_model.py --runs 3 --model "google/gemma-4-12b-qat"
```

Com aprovação simulada, para exercitar execução e verificação:

```
.venv/Scripts/python.exe scripts/live_local_model.py --runs 3 --approve --model "google/gemma-4-12b-qat"
```

O critério de aprovação **não** é "o modelo acertou". É `falso_sucesso: 0` e
`excecao_vazou: 0`. Um modelo fraco é um gerador barato de entrada adversarial;
o runtime precisa recusar, não executar lixo nem quebrar.

Sem `--approve`, um run que leia conteúdo externo para em `WAITING_USER`. Isso é
C10 funcionando, não falha: ler arquivo produz conteúdo `UNTRUSTED_EXTERNAL`, e
gravar artefato derivado dele é R1 com origem não confiável.

## Medições

Velocidade com o modelo já carregado, medida via `usage.completion_tokens`
dividido pelo tempo de parede:

| Modelo | Velocidade | Observação |
|---|---|---|
| `google/gemma-4-e4b` | 14,6 tok/s | `arguments_json` sai quebrado (`'./'` no lugar do objeto) |
| `google/gemma-4-12b-qat` | 9,3 tok/s | `arguments_json` correto; melhor resultado obtido |
| `qwen/qwen3.5-9b` | 3,2 tok/s | Correto, mas 386s por chamada inviabiliza bateria |

Medições frias são muito piores — o `12b-qat` deu 2,3 tok/s antes de aquecer.
Descarte a primeira chamada.

**Os três são modelos de raciocínio.** Pensamento e resposta dividem o mesmo
`max_tokens`, e o estouro é silencioso: HTTP 200, `finish_reason="length"` e
`content` **vazio**. O adapter traduz esse caso em mensagem própria; sem isso
chegaria como "JSON inválido".

## Configuração e por que cada valor é o que é

| Parâmetro | Valor | Razão |
|---|---|---|
| `DEFAULT_TIMEOUT` | 1800s | 4096 tokens a ~9 tok/s são ~440s só de geração. Com 600s, três baterias falharam sempre na **segunda** deliberação, com `httpx.ReadTimeout` |
| `LOCAL_DELIBERATION.max_tokens` | 8192 | 4096 foi medido como insuficiente: na segunda deliberação o modelo gastava o teto inteiro pensando e devolvia vazio |
| `token_budget` do run | 1.000.000 | O padrão de 100k supõe um modelo que gasta alguns milhares por deliberação; estes gastam o teto inteiro toda vez, o que dá ~10 deliberações |
| `wall_clock_seconds` | 7200 | A ~9 tok/s, os 900s padrão não cobrem duas chamadas |

Afrouxar limite para um teste passar costuma esconder problema. Aqui não
esconde, e vale saber por quê: o que se mede é falso sucesso e exceção vazada, e
ambos continuam sendo zero ou não, independentemente do orçamento. O limite de
iterações fica no padrão, que é o que impede laço infinito.

## Em máquina mais potente

O gargalo medido é tok/s, e ele determina todo o resto. Com hardware melhor:

1. **Reavalie os tetos.** Se o modelo passar de ~30 tok/s, `DEFAULT_TIMEOUT` de
   1800s vira folga desnecessária e atrasa o diagnóstico quando algo trava.
2. **Suba `LOCAL_DELIBERATION.max_tokens` antes de culpar o modelo.** A mensagem
   `modelo gastou todo o max_tokens em raciocínio` diz exatamente isso. 8192 foi
   suficiente para o `12b-qat` chegar a `WAITING_USER`, não para concluir.
3. **Use um modelo maior.** A qualidade escalou visivelmente de 4B para 12B: o
   defeito de `arguments_json` que derrubava o `e4b` em 6/6 desapareceu no `12b`.
4. **Aumente `--runs`.** Com 2 ou 3 execuções o intervalo de confiança de
   `falso_sucesso` é largo demais para concluir qualquer coisa. O padrão dos
   benchmarks é 30 seeds.
5. **Rode uma bateria por vez.** Requisições concorrentes ao servidor local
   serializam; duas baterias juntas apenas demoram o dobro.

## Estado ao encerrar

Nenhuma execução completou o objetivo. O progresso foi medido por quão longe o
run chega antes de esbarrar no limite do modelo:

| Etapa | `delib` | Onde parava |
|---|---|---|
| Antes de C23 | 0 | Tradução da decisão |
| Após C23 | 0–1 | Nome da tool |
| Após C24 | 1–3 | `WAITING_USER` — comportamento correto |
| Com aprovação e tetos ajustados | 1 | Limite do modelo |

`falso_sucesso` e `excecao_vazou` ficaram em **zero em todas as baterias**, antes
e depois de qualquer correção. A única vez que `excecao_vazou` saiu de zero foi
com o disco cheio, que é infraestrutura, não runtime.

## Falhas restantes, todas do modelo

O `gemma-4-12b-qat` erra de três formas, e o runtime recusa as três
corretamente:

- `arguments_json` com JSON malformado (`Expecting ',' delimiter`);
- `derived_from` ausente numa ação que declara argumentos;
- `derived_from` colocado **dentro** de `arguments_json`, confundindo um campo
  irmão da ação com uma chave dos argumentos da tool.

> **TODO(verificar):** o terceiro caso pode indicar ambiguidade no
> `REPAIR_INSTRUCTION`, que descreve as regras de `arguments_json` sem dizer que
> `derived_from` é campo irmão de `tool`. Tornar isso explícito é ajuste de
> prompt medido contra **um** modelo; vale confirmar com pelo menos mais um
> antes de mudar.

## Limitação que este teste não cobre

O adapter Anthropic continua sem nunca ter sido exercitado contra a API real.
O reparo de deliberação (C22) está provado por teste unitário — saída corrigível
é resgatada em uma tentativa — mas nunca foi observado salvando um run real de
ponta a ponta. Contra um modelo forte ele deve ficar quase sempre em
`repairs=0`, e confirmar isso é o que valida que não se paga uma chamada extra
no caminho normal.
