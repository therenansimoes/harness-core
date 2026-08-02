# harness-core

Harness de agente **provider-agnostic**. Você descreve uma unidade de trabalho
(`unit.toml`: um prompt e um comando de verificação), o harness executa, e
**verifica de forma determinística** — nunca confia no que o agente diz. Cada
run vira uma linha num ledger SQLite com backend, kind, tier, tempo e custo.

Em cima desse ledger roda um loop de auto-melhoria: ele lê os padrões de falha,
escolhe uma mutação de config no catálogo, testa a mutação contra o baseline em
**A/B com régua de Wilson** e mantém ou reverte por evidência. Nenhum knob muda
por opinião.

**Done = o `verify_cmd` sai 0.** O que o agente diz não conta.

Núcleo sobre [LangGraph](https://github.com/langchain-ai/langgraph) (grafo +
checkpointer SQLite). Executor default sobre
[deepagents](https://github.com/langchain-ai/deepagents)/LangChain, que roda
**modelo local via Ollama, custo zero**. Nenhum vendor no núcleo: backend é
plugin por entry point (`harness.backends`), e trocar de provedor não toca em
uma linha de `harness/`.

## Instalar

Requisitos: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), `git`. Para o
quickstart de custo zero, [Ollama](https://ollama.com) local.

```bash
uv sync --extra deepagents      # o extra é o EXECUTOR; o núcleo não precisa dele
uv run harness doctor
```

`doctor` é a verificação global em forma de comando — genoma, tracing, config,
catálogo, dados, ledger, preflight de todo backend registrado:

```
ok    genome               config/genome.toml fingerprint=79857c110726 imutáveis=24 padrões=6+2
ok    tracing              LANGSMITH_*/LANGCHAIN_TRACING_* desligados
ok    msgpack              LANGGRAPH_STRICT_MSGPACK=true
ok    config               5 toml: catalog.toml, genome.toml, kinds.toml, models.toml, tools.toml
ok    catalog              3 regra(s), n_per_arm=6 window=200
ok    data                 data ainda não existe; . é gravável
ok    ledger               data/runs.sqlite ainda não existe (nasce no primeiro run)
ok    backend:claude_code  2.1.220 (Claude Code)
ok    backend:deepagents   deepagents importável
ok    backend:mock         mock sempre disponível
doctor checks=10 falhas=0 avisos=0
```

**FALHA** (exit 1) é coisa nossa quebrada. Backend indisponível é **aviso**: um
doctor que sai 1 porque o Ollama está desligado vira ruído que ninguém lê.

## Quickstart: Ollama, custo zero

```bash
ollama serve &
ollama pull qwen2.5:3b

uv run harness run --unit tests/fixtures/tiny_fix \
  --backend deepagents --model ollama:qwen2.5:3b
```

Saída real desta máquina (M3 Pro, 18GB):

```
e224de09e26c tiny_fix deepagents accept verify ok, sem regressão de KPI 7.82s ledger#2
```

O que aconteceu: workspace descartável → o modelo edita `target.py` → o harness
roda `python3 -c 'from target import add; assert add(2,3)==5'` → o gate compara
os KPIs de antes e depois → linha no `data/runs.sqlite`. `accept` saiu do
verify, não do modelo.

Sem Ollama, o mesmo caminho com o backend determinístico de teste:

```bash
uv run harness run --unit tests/fixtures/echo --backend mock
# 99d6909de5c3 echo mock accept verify ok, sem regressão de KPI 0.01s ledger#1
```

Uma unidade é só isto (`tests/fixtures/tiny_fix/unit.toml`, sem os comentários):

```toml
id = "tiny_fix"
kind = "code"
prompt = """No seu diretório de trabalho existe o arquivo target.py com uma função
add(a, b) que está errada: retorna a - b. Edite target.py trocando
"return a - b" por "return a + b". Não faça mais nada depois disso."""
verify_cmd = "python3 -c 'from target import add; assert add(2,3)==5'"
```

## Comandos

| Comando | O que faz |
|---|---|
| `harness run --unit DIR --backend NOME [--model M]` | executa uma unidade e grava no ledger |
| `harness run --unit DIR --route auto` | quem escolhe tier/backend/model é o router, pelo kind da unidade e pelo histórico |
| `harness ab --a 5/6 --b 6/6` | veredito de Wilson entre dois braços já contados |
| `harness ab --dim backend --unit DIR --a-backend X --b-backend Y --n 6` | o harness roda o experimento e decide o executor por evidência |
| `harness improve [--cycles N] [--deadline-s S]` | um ciclo do loop: mutação → A/B → KEEP/DISCARD → registro |
| `harness replay --list` / `--mutation ID` | atribuição: quanto do delta do histórico a mutação sustenta |
| `harness doctor` | diagnóstico local, zero rede, zero LLM |
| `harness backends` | backends registrados + preflight |
| `harness bench provision --n 10` | custo de provisionar workspace (p50) |

```bash
uv run harness ab --a 5/6 --b 6/6
# INCONCLUSIVE a=5/6 [0.44,0.97] b=6/6 [0.61,1.00]

uv run harness backends
# claude_code      ok             2.1.220 (Claude Code)
# deepagents       ok             deepagents importável
# mock             ok             mock sempre disponível
```

## O loop de melhoria

O loop **não inventa mutação**: ele escolhe de `config/catalog.toml`, onde cada
regra é uma hipótese falsificável sobre um knob ("trocar esta chave deste valor
para aquele reduz este padrão de falha"), e ordena por ganho esperado
`freq(padrão) × custo_médio(padrão) × prior(regra)`. Sem gradiente — catálogo
esgotado ou ganho abaixo do piso — ele **para e chama o humano** via
`interrupt()` do LangGraph, em vez de improvisar:

```bash
uv run harness improve --unit tests/fixtures/echo --backend mock
# escalate no_gradient thread=improve-642faf7ac988 evidence={'history': 2, 'catalog': 3, 'applicable': 3}
# improve ciclos=0 mutações=0 intervenções=0 intervention_rate=0.00 (n=2)
```

Escalação pendente se responde por `--resume <thread> --answer '<json>'`; a
thread continua de onde parou, pelo checkpoint. `intervention_rate` sai em todo
relatório porque o alvo do projeto é essa taxa, não o número de runs.

Com veredito, `harness replay` faz a pergunta que o A/B não responde: o
histórico DEPOIS da mutação é melhor que o de ANTES? Três fatias (antes /
experimento / depois), IC de Wilson em cada janela, e os **confounders
nomeados** — as outras mutações KEEP que entraram no meio. Exemplo (ledger
sintético de `tests/test_replay.py`):

```
mut aaaaaaaaaaaa floor_up KEEP mantida exp=12 kind=code tier=t0 backend=mock
antes 2/6 [0.10,0.70] depois 5/6 [0.44,0.97] delta=+0.50 intervalos=sobrepostos
confounders=1 bbbbbbbbbbbb:turns_up@2026-08-02T12:25:00+00:00
```

`intervalos=sobrepostos` é o ponto: +0.50 com IC que se cruza não é ganho
provado. Atribuição honesta nomeia o que não consegue separar em vez de
publicar um número limpo que não é.

## Arquitetura

```
harness/
  types.py            # ExecRequest/ExecResult/RunRow/Selection — o contrato entre camadas
  cli.py              # run / ab / improve / replay / doctor / backends / bench
  backends/           # mock, deepagents (LangChain isolado em 1 arquivo), claude_code
                      #   registro por entry point; slot harness.auth plugável
  ledger/store.py     # SQLite: runs, mutations, node_events (idempotência dos nós)
  ruler/              # wilson, kpi, verify, note, gate — a RÉGUA, imutável no genoma
  routing/            # kinds ortogonais a tier + prior Wilson keyed (kind, tier, backend)
  genome/             # mutable/immutable + tamper: o que o loop pode e não pode editar
  graph/              # run_graph (um run) e autopilot_graph (um ciclo de melhoria)
  improve/            # target (escolha da mutação), mutate, escalate, replay
  workspace/          # provision por git worktree
config/               # models.toml, kinds.toml, catalog.toml, tools.toml, genome.toml
benchmarks/           # held_in (hill-climb), sealed (held-out), judge, tb
legacy/               # o harness anterior, congelado (read-only, fora do pytest)
```

## Princípios que o código impõe

- **Mecanismo, não instrução.** Sandbox, tamper check e teto de turnos são
  código que o agente não contorna escrevendo texto diferente — nunca uma frase
  no prompt pedindo para não trapacear.
- **A régua não é mutável.** `harness/ruler/**`, `harness/genome/**`,
  `harness/routing/**`, `harness/graph/**`, `uv.lock` e `benchmarks/sealed/**`
  são immutable no genoma; o loop calibra só `config/*.toml`. Patch em
  `harness/ruler/wilson.py` sai como `tamper:genome_violation`.
- **Wilson com N mínimo.** Abaixo do piso de amostra a régua diz
  `INCONCLUSIVE`, não "melhorou". Experimento interrompido no meio (deadline)
  é descartado inteiro: braço com N diferente do outro é amostra envenenada,
  não amostra pequena.
- **Braços contemporâneos.** O A/B alterna A,B,A,B na mesma janela; comparar
  dias diferentes mede ruído de dia.
- **Telemetria de terceiro é opt-in.** O bootstrap da CLI desliga
  `LANGSMITH_*`/`LANGCHAIN_TRACING_*` e liga `LANGGRAPH_STRICT_MSGPACK`; há
  teste que falha se alguma dessas voltar ligada.
- **Auth: só o slot.** O entry point `harness.auth` existe, e o único adapter
  shippado é o nulo. Adapter de OAuth de assinatura em cliente de terceiro é
  área cinzenta de ToS e não entra neste repo — nem o adapter, nem doc
  ensinando.
- **Nota humana é KPI, e KPI tem N mínimo.** A nota vive em
  `projects/<projeto>/notes.tsv`, append-only, escrita por humano. Abaixo de 3
  notas na janela o KPI é "não medido" — não é zero, e não vira gradiente.

## Testes

```bash
uv run --extra deepagents pytest -q
# 402 passed, 2 deselected
```

Os 2 deselecionados são os marcadores `ollama` e `claude_cli`: um exige
servidor local no ar, o outro gasta dinheiro. Rodar a suíte inteira custa zero
e não toca a rede.

## Estado honesto

O projeto anda por uma escada de PRs definida em `docs/SPEC-rebuild.md` §6, cada
degrau com aceite executável: PR-0 a PR-8 fechados, PR-9 (loop dirigido) fechado
com o canal causal do PR-9b, PR-10 (replay + docs) é este.

O que **não** está pronto, e é bom saber antes de clonar:

- **A CLI ainda não roda pelo grafo.** `graph/run_graph.py` tem os nós
  idempotentes, o checkpointer e o provision por `git worktree`, com teste de
  resume depois de `kill -9` de verdade — mas `harness run`/`ab`/`improve`
  executam pelo caminho direto (`run_once`, workspace em tmpdir ou `--repo`).
  Fiar a CLI no grafo é dívida aberta.
- **Fan-out do A/B é sequencial de propósito.** Os braços diferem por um
  arquivo de config, que é estado global do processo: dois braços em paralelo
  leriam o mesmo arquivo e mediriam a mesma coisa. Paralelismo real depende de
  injetar config por run.
- **Exercitado a fundo em modelo pequeno local** (`ollama:qwen2.5:3b`) e no
  backend `mock`. O backend `claude_code` roda e grava no ledger, mas não tem
  A/B sério contra os outros.
- O histórico do harness antigo (`legacy/results.tsv`) **não** entra no prior:
  aquelas linhas não têm `backend`/`kind` e envenenariam a chave.

## De onde veio

`legacy/` é o harness anterior — stdlib puro, sem framework de agente —
congelado como referência read-only, fora do pytest e do genoma. Foi ele que
consertou um bug real do [schwifty](https://github.com/mdomke/schwifty) com os
415 testes do upstream verdes, e que produziu as decisões A/B documentadas em
`legacy/evolution/decisions/`. **Aqueles números são dele, não deste núcleo**;
estão preservados porque foram medidos, e o que sobreviveu deles renasceu aqui
como mecanismo (router com prior Wilson, tamper, gate com revert). O caminho de
volta está em `docs/SPEC-rebuild.md` §5.

## Licença

MIT (`LICENSE`).
