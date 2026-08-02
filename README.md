# harness-core

Harness de agente **provider-agnostic** que se auto-melhora com prova. O núcleo
(`harness/`) não conhece vendor: quem executa é um **backend** plugável, e o
executor default é [deepagents](https://github.com/langchain-ai/deepagents)
sobre [LangGraph](https://github.com/langchain-ai/langgraph). Roda de graça com
Ollama local; a mesma unidade roda em qualquer backend registrado.

O que o harness acrescenta a um loop de agente comum é a **régua**: verify
determinístico, KPI medido antes/depois, um gate que concentra a decisão,
veredito de Wilson no A/B e um genoma que declara o que o próprio loop **não**
pode mudar. Sem isso, "o agente melhorou" é opinião.

Licença MIT (`LICENSE`). Python ≥ 3.11.

## Arquitetura

```
harness/
  cli.py        run · ab · backends · improve · bench
  types.py      UnitSpec ExecRequest ExecResult Selection Verdict RunRow MutationRow
  backends/     base(Protocol) registry(entry point) mock deepagents claude_code auth/
  graph/        run_graph (topologia de 1 run)  autopilot_graph (1 ciclo de melhoria)
  ruler/        wilson kpi verify note gate      ← quem mede e quem decide
  genome/       genome tamper                    ← o que pode mudar
  routing/      kinds (o QUE é)  router (QUANTO custa)
  workspace/    provision (git worktree + symlink de cache)
  improve/      target mutate escalate           ← o que vale a pena mudar
  ledger/       store (SQLite; TSV é export)     ← fonte de verdade das runs
config/*.toml   models kinds tools catalog genome   ← a zona calibrável pelo loop
```

**run_graph** (`harness/graph/run_graph.py`) é a topologia de uma run:
`plan → route → provision → execute → verify → measure → gate →
[accept | retry → route | escalate | revert] → record → END`. Checkpoint em
`SqliteSaver` com `thread_id = run_id` e nós idempotentes: matar o processo no
meio do `execute` e reinvocar a mesma thread completa a run sem executar duas
vezes (`tests/test_resume.py` faz isso com `kill -9` de verdade). A topologia é
imutável no genoma — o loop calibra TOML, não nós. Ressalva: nesse grafo,
`measure` e `gate` ainda são stubs — decidem pelo veredito do verify, sem KPI
antes/depois e sem tamper. Quem roda a régua com KPI é `harness run`
(`cli.run_once`); detalhes em `docs/ARCHITECTURE.md`.

**ruler** (`harness/ruler/`) é a régua, e é uma peça só de propósito.
`verify.py` roda o `verify_cmd` da unidade (sucesso = exit 0, nunca o que o
agente diz); `kpi.py` coleta os KPIs do projeto com as specs lidas **antes** da
mudança; `wilson.py` dá o intervalo e o veredito KEEP/DISCARD/INCONCLUSIVE;
`note.py` guarda a nota humana 1–5, o único KPI que o harness não sabe medir
sozinho; `gate.py` combina tudo num lugar só: tamper → `revert`, verify
vermelho → `retry`, KPI regrediu → `revert`, senão `accept`. Quem chama esse
gate é `cli.run_once` — o nó `gate` do run_graph é o stub citado acima e não
passa por aqui.

**genome** (`harness/genome/` + `config/genome.toml`) separa o mutável do
imutável. Imutável: `harness/ruler/**`, `harness/genome/**`, `harness/routing/**`,
`harness/graph/**`, `uv.lock`, `benchmarks/sealed/**`. Mutável: `config/*.toml`
e `prompts/**`. `tamper.py` sabe tirar fingerprint antes e comparar depois, e
violação que chegue ao gate vira `revert` — mas o que barra escrita em zona
imutável hoje é o `genome_check` do autopilot, fail-closed ANTES de escrever;
nenhum dos dois caminhos de run liga a checagem de fingerprint ainda.

**router** (`harness/routing/`) separa duas perguntas que o harness velho
misturava: `kinds.py` classifica **o que** a unidade é (`code`, `content`,
`config`, `refactor`, `infra`) de forma determinística, e `router.py` escolhe
**quanto** ela pode custar (tier `t0`/`t1`/`t2` de `config/models.toml`). O
prior de sucesso é keyed em `(kind, tier, backend)` — histórico ruim de
`(code, t0)` não condena `(content, t0)` — e Wilson abaixo do `prior_floor`
sobe um tier, assim como falha repetida escala por attempt.

**autopilot** (`harness/graph/autopilot_graph.py` + `harness/improve/`) é o
loop de melhoria: `pick_target → propose → genome_check → apply → fanout_ab →
score → [KEEP: commit_cfg | DISCARD/INCONCLUSIVE: revert_cfg] → attribute →
record`. Qualquer nó pode desviar para `escalate`, que é `interrupt()` do
LangGraph: o grafo para e espera um humano em vez de improvisar.

## Quickstart (Ollama, custo $0)

```bash
uv sync --extra deepagents
ollama pull qwen2.5:3b      # laptop de 18GB: modelo local ≤ 8B (30B trava a máquina)
```

Backends registrados e preflight (determinístico, zero chamada de LLM):

```console
$ uv run harness backends
claude_code      ok             2.1.220 (Claude Code)
deepagents       ok             deepagents importável
mock             ok             mock sempre disponível
```

A terceira coluna depende da máquina: sem o extra `deepagents` instalado a
linha vira `indisponível`, e sem o CLI oficial a do `claude_code` também.

Uma unidade é um diretório com `unit.toml` (`id`, `prompt`, `verify_cmd`,
`kind` opcional). Rodando a fixture `tiny_fix` com o modelo local:

```bash
uv run harness run --unit tests/fixtures/tiny_fix \
  --backend deepagents --model ollama:qwen2.5:3b
```

Modelo `ollama:*` custa $0 na tabela `[pricing]` de `config/models.toml`, então
a linha do ledger sai com `cost_usd = 0.0`. A saída dessa run depende do modelo
que você tem instalado e por isso não está colada aqui; o formato é o mesmo do
exemplo com `mock` abaixo.

Deixando o router escolher:

```bash
uv run harness run --unit tests/fixtures/tiny_fix --route auto
```

`--route auto` imprime uma linha a mais antes da run
(`route auto <unit> kind=… tier=… <backend> <model> [motivos]`) e é exclusivo
com `--backend`/`--model` — quem escolhe é o router. **Atenção ao custo:** na
config que vem no repo, `[router.kind] code = "t1"`, e o tier `t1` é
`claude_code` (pago). Para um setup 100% local, aponte `code` para `t0` em
`config/models.toml` ou fixe `--backend deepagents` na mão.

Tudo que não precisa de modelo roda com o backend `mock`, que é determinístico:

```console
$ uv run harness run --unit tests/fixtures/echo --backend mock
a10bc523d66d echo mock accept verify ok, sem regressão de KPI 0.01s ledger#1

$ uv run harness run --unit tests/fixtures/tiny_fix --backend mock
c103bdb4c96a tiny_fix mock retry verify_failed:exit=1 0.03s ledger#2
```

(o `mock` só escreve o prompt num arquivo: `echo` passa, `tiny_fix` reprova no
verify — é assim que o gate se prova sem gastar API).

Exit code do `harness run` é 0 quando o gate deu `accept`, 1 no resto. Os dados
ficam em `$HARNESS_DATA_DIR` (default `data/`, gitignored):
`runs.sqlite` (ledger), `checkpoints.sqlite`, `ws/` (worktrees das runs).

Outros comandos:

```console
$ uv run harness ab --a 5/6 --b 6/6
INCONCLUSIVE a=5/6 [0.44,0.97] b=6/6 [0.61,1.00]

$ uv run harness bench provision --n 10
provision n=10 p50=0.069s p95=0.072s
```

## Backends

Um backend implementa três métodos (`harness/backends/base.py`):
`capabilities()`, `preflight()` — determinístico, **zero chamada de LLM** — e
`execute(ExecRequest) -> ExecResult`. O núcleo não conhece nada além disso.

| backend | o que é | notas |
|---|---|---|
| `mock` | determinístico, escreve o prompt num arquivo | é o backend dos testes; não toca rede |
| `deepagents` | default; modelo por `init_chat_model` (`ollama:…`, ou qualquer provider do LangChain) | único arquivo do repo que importa LangChain, e o import é lazy |
| `claude_code` | subprocess do CLI oficial; `resumable=True` via `--resume` | exige o CLI instalado e autenticado; gasta dinheiro |

Backend de terceiro não precisa tocar no núcleo: publique um pacote que se
anuncie no entry point `harness.backends`.

```toml
# pyproject.toml do SEU pacote
[project.entry-points."harness.backends"]
meu_backend = "meu_pacote.backend:MeuBackend"
```

`harness.backends.registry` funde os entry points instalados com os embutidos
(e com `registry.register(nome, factory)`, que é o caminho dos testes). Depois
do `pip install`, `harness backends` já lista o seu.

Auth segue o mesmo padrão, no entry point `harness.auth`
(`AuthAdapter`: `env()` + `check()`). O repo shippa só o `NullAuth`.

> Adapter de OAuth de assinatura em cliente de terceiro é **área cinzenta de
> ToS e está fora deste repo** — existe só o slot `harness.auth` para quem
> quiser publicar o próprio, por conta e risco próprios.

## Auto-melhoria

O loop não inventa mudança: ele escolhe de um catálogo declarado
(`config/catalog.toml`). Cada `[[rule]]` é uma hipótese falsificável sobre um
knob — arquivo alvo, chave, `from`, `to`, e o `fails_on` que amarra a regra a
`exit_reason` reais do ledger. `improve/target.py` ordena por ganho esperado
(`freq(falha) × custo_médio × prior`) e o `improve/mutate.py` aplica.

```console
$ uv run harness improve --cycles 1 --backend mock --unit tests/fixtures/echo
ciclo0 max_attempts_3_to_4 router.max_attempts 3->4 INCONCLUSIVE a=6/6 b=6/6 delta=+0.00 revertida mut=f4dff79cc02c
improve ciclos=1 mutações=1 intervenções=0 intervention_rate=0.00 (n=15)
```

A mutação é medida em A/B com os braços **alternados** (A,B,A,B — ambiente que
degrada no meio pune os dois igual) e o veredito é o de Wilson: `KEEP` commita
a config, `DISCARD`/`INCONCLUSIVE` revertem na hora. Sucesso do braço é a
decisão do gate, não o "terminei" do executor.

Sem gradiente, o loop **para e chama gente** — em vez de mutar qualquer coisa
para parecer produtivo (mesmo comando, outro ledger: sem padrão de falha que
sustente uma regra, não há mutação que valha o experimento):

```console
$ uv run harness improve --cycles 1 --backend mock --unit tests/fixtures/echo
escalate no_gradient thread=improve-80f6a036afbe evidence={'history': 14, 'catalog': 3, 'applicable': 3}
improve ciclos=0 mutações=0 intervenções=0 intervention_rate=0.00 (n=14)
```

São quatro motivos de escalação (`harness/improve/escalate.py`):
`no_gradient`, `genome_violation`, `deadline`, `error`. A parada é
`interrupt()` do LangGraph, então ela sobrevive ao fim do processo: o
`thread=` da linha é o que se responde depois.

```bash
uv run harness improve --unit tests/fixtures/echo --backend mock \
  --resume improve-80f6a036afbe --answer '{"action":"continue"}'
```

O default do `--answer` é `{"action":"abort"}`: retomar um loop sem dizer o que
fazer nunca pode significar "continua sozinho". Run retomada por humano entra
no ledger com `intervention=1`, que é o que alimenta o `intervention_rate` —
a métrica de autonomia mora fora do que ela mede.

`harness improve` sem `--unit` usa `benchmarks/held_in/*/unit.toml`; enquanto
esse diretório não tiver unidades no formato novo, passe `--unit` (repetível).

`harness replay --list` mostra as mutações julgadas; `harness replay
--mutation <id>` imprime o delta atribuído com intervalo de Wilson por janela
e nomeia os *confounders* (outras mutações KEEP no meio — atribuição honesta
diz o que não consegue separar). `harness doctor` roda o diagnóstico completo:
preflight de todos os backends, genoma/tamper, tracing desligado, configs
parseáveis.

## Régua e genoma

A régua e o genoma existem pelo mesmo motivo: **um loop que pode reescrever o
próprio critério não mede nada**. Uma mutação com acesso a `ruler/` aprovaria a
si mesma baixando a barra; com acesso a `routing/` se daria o tier caro e
falsearia o próprio A/B; com acesso a `benchmarks/sealed/**` reescreveria a
prova. Por isso essas zonas são `immutable` em `config/genome.toml`, o
`uv.lock` também é (trocar versão de dep por baixo invalida qualquer
comparação), e o que sobra para o loop calibrar é `config/*.toml` + `prompts/**`.

Três detalhes que fazem parte da mesma defesa:

- as specs de KPI são lidas **antes** da mudança — a mudança avaliada não
  redefine a direção da régua reescrevendo o `kpis.toml`;
- o `catalog.toml` é mutável (para o humano calibrar) mas o loop não pode
  apontar uma regra para ele: isso é `genome:self_edit`;
- a nota humana é append-only em `projects/<projeto>/notes.tsv` e nenhuma
  função de escrita dela é exposta a backend ou agente. Abaixo de 3 notas na
  janela, ela vale `None` ("não medido") — uma opinião solta não reverte uma
  versão. Hoje ela é API (`harness.ruler.note.add`), ainda sem subcomando na
  CLI.

## Estado

`uv run --extra deepagents pytest -q` → **364 passed, 2 deselected**. Os 2
deselecionados são os testes que exigem máquina de verdade: marker `ollama`
(servidor local) e `claude_cli` (gasta dinheiro). Ver `CONTRIBUTING.md`.

O histórico numérico do harness antigo **não** foi migrado: aquelas linhas não
têm `backend`/`kind` e envenenariam o prior. Ele fica em `legacy/` como
referência read-only, fora do pytest e fora do genoma.

Docs: `docs/ARCHITECTURE.md` (contratos e estado), `STATUS.md` (o que está
feito e verificado), `docs/SPEC-rebuild.md` (a spec do rebuild, já executada),
`CONTRIBUTING.md`.
