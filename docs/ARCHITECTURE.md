# ARCHITECTURE — o que é permanente

> O que muda a cada PR mora em `STATUS.md`; a história da decisão mora em
> `docs/SPEC-rebuild.md` (executada). Aqui fica só o que um contribuidor
> precisa saber para não brigar com a estrutura: contratos, estado, zonas.
> Onde este arquivo e o código divergirem, o código está certo — cada seção
> aponta para o arquivo que manda.

## Estrutura

```
harness/          núcleo, zero menção a vendor
  cli.py            run · ab · backends · improve · bench
  types.py          UnitSpec ExecRequest ExecResult Selection Verdict RunRow MutationRow
  backends/         base.py (Protocol) registry.py mock.py deepagents_backend.py
                    claude_code.py auth/base.py
  graph/            state.py run_graph.py autopilot_graph.py checkpoint.py
  ruler/            wilson.py kpi.py verify.py note.py gate.py
  genome/           genome.py tamper.py
  routing/          kinds.py router.py
  workspace/        provision.py            git worktree + symlink de cache
  ledger/           store.py                SQLite; TSV é export, não fonte
  improve/          target.py mutate.py escalate.py
config/           models.toml kinds.toml tools.toml catalog.toml genome.toml
benchmarks/       held_in/ sealed/ judge/ tb/
data/             runs.sqlite checkpoints.sqlite ws/       (gitignored)
projects/         website-faz-rogers/  CONGELADO, vira benchmark
tests/
legacy/           referência read-only: fora do pytest, fora do genoma
```

Raízes vêm de env var, sempre — é o que deixa o teste rodar num tmpdir:
`$HARNESS_DATA_DIR` (default `data/`, ledger + checkpoints + `ws/`),
`$HARNESS_CONFIG_DIR` (default `config/`), `$HARNESS_ROOT` (default `.`, a
árvore que o `improve` muta), `$HARNESS_PROJECTS_ROOT` (default `projects/`).

## Unidade de trabalho

Um diretório com `unit.toml`: `id`, `prompt`, `verify_cmd`, e `kind` opcional
(`code|content|config|refactor|infra`). Sucesso é `verify_cmd` sair 0 — o que o
agente diz nunca conta. Exemplos: `tests/fixtures/echo`,
`tests/fixtures/tiny_fix`.

## Contrato `Backend`

`harness/backends/base.py`. Três métodos, nada mais — o núcleo não conhece
LangChain, CLI, HTTP nem nome de provider:

```python
class Backend(Protocol):
    name: ClassVar[str]

    def capabilities(
        self,
    ) -> Capabilities: ...  # resumable, reports_cost, model_selectable, tools, streaming
    def preflight(self) -> Preflight: ...  # (ok, reason) — determinístico, ZERO chamada de LLM
    def execute(self, req: ExecRequest) -> ExecResult: ...
```

`ExecRequest` e `ExecResult` são dataclasses frozen em `harness/types.py`.
`ExecResult.exit_reason` é vocabulário fechado: `done | max_turns | timeout |
error | blocked`. Preflight que chama LLM não é preflight — é run.

Descoberta em `harness/backends/registry.py`: entry point `harness.backends`,
com fallback embutido para os três do repo e `register()` manual para teste e
plugin não instalado. Backend de terceiro não toca no núcleo.

## Contrato `AuthAdapter`

`harness/backends/auth/base.py`, entry point `harness.auth`:

```python
class AuthAdapter(Protocol):
    name: ClassVar[str]

    def env(self) -> Mapping[str, str]: ...  # injetado no processo do backend, nunca persistido
    def check(self) -> Preflight: ...  # checagem local, ZERO chamada de LLM
```

O repo shippa só `NullAuth` (autenticação nativa da ferramenta). Adapter de
OAuth de assinatura em cliente de terceiro é área cinzenta de ToS: fica fora
deste repo, por decisão, não por falta de tempo.

## Estado do grafo

`harness/graph/state.py` — `RunState` é um `TypedDict`; `events` é
`Annotated[list[Event], operator.add]` (append-only: a lista de eventos *é* o
trace). Campos: `run_id`, `unit`, `attempt`, `selection`, `workspace`, `exec`,
`verdict`, `kpi_before`, `kpi_after`, `tamper`, `decision`, `budget`, `events`.

**run_graph** (`harness/graph/run_graph.py`, entrada `run_unit()`):

```
plan → route → provision → execute → verify → measure → gate
        ↑                                                 ├→ accept  ─┐
        └──────────────── retry ←─────────────────────────┤           │
                                                          ├→ escalate ┼→ record → END
                                                          └→ revert  ─┘
```

Checkpointer `SqliteSaver` em `$HARNESS_DATA_DIR/checkpoints.sqlite`,
`thread_id = run_id`. Nós idempotentes por construção: `provision` reusa o
worktree existente, `execute` consulta o ledger por `(run_id, node, attempt)`
antes de chamar o backend. É isso que faz o resume pós-`kill -9` completar sem
executar duas vezes (`tests/test_resume.py`, com processo filho de verdade).

**No grafo, `measure` e `gate` ainda são stubs** (é o que os próprios
docstrings de `_measure`/`_gate` dizem): `measure` devolve `kpi_before` e
`kpi_after` vazios e `gate` decide só pelo veredito do verify — `accept` se
passou, senão `retry` até `max_attempts` e depois `escalate_human`. Ou seja,
`run_unit()` **não** chama `ruler/gate.py`, não compara KPI antes/depois e não
checa tamper; unidade que passa no verify e regride KPI sai como `accept` e
entra no ledger com `ok=1`. A régua com KPI roda no caminho da CLI (`run_once`,
abaixo). Ligar `ruler.gate` no grafo é trabalho pendente, não detalhe de
implementação.

**autopilot_graph** (`harness/graph/autopilot_graph.py`, entrada
`run_autopilot()`):

```
pick_target → propose → genome_check → apply → fanout_ab → score
                                                            ├→ KEEP: commit_cfg ─┐
                                                            └→ DISCARD/INCONCL.: │
                                                               revert_cfg ───────┴→ attribute → record → (próximo ciclo | END)
```

Qualquer nó desvia para `escalate` = `interrupt()`. `fanout_ab` é sequencial de
propósito: os braços diferem por uma mutação em `config/*.toml`, que é estado
global do processo — dois braços em paralelo leriam o mesmo arquivo e mediriam
a mesma coisa. `Budget.max_parallel` fica em 1 até a config ser injetável por
run.

**A CLI não passa pelo grafo.** `harness run` chama `harness/cli.py:run_once()`,
sequência linear sem checkpoint nem retry; `harness ab` e `harness improve`
também executam por `run_once` (via `harness/ab.py`). O grafo é o caminho
programático (`run_unit`) e o que os testes de resume exercitam.

Os dois caminhos não são o mesmo run com e sem checkpoint — cada um tem menos
que o outro em algum eixo. `run_once` é o único que roda a régua de verdade:
`ruler/kpi.py` coleta antes com as specs do ANTES, coleta depois e entrega tudo
a `ruler/gate.py`, que faz o `revert` do worktree em regressão de KPI; em troca,
não tem checkpoint, retry nem escalação de tier por attempt. O grafo tem
checkpoint, retry e router, e decide por um gate stub. **Nenhum dos dois liga o
tamper**: os dois passam lista de tamper vazia para o gate (o grafo literalmente
`"tamper": []`, `run_once` um `[]` no argumento). Antes de "consertar" um dos
dois, leia os dois.

## Zonas: quem pode mudar o quê

`config/genome.toml` é a fonte; `harness/genome/genome.py` carrega (falha
fechado se um path casar `mutable` e `immutable`) e `harness/genome/tamper.py`
sabe responder as duas perguntas — `check_patch` (o patch declarado tocou o
proibido?) e `fingerprint` (o conteúdo do imutável mudou?). Uma violação de
tamper que chegue ao gate vira `revert`, mas **nada no caminho de run chama
`fingerprint` hoje**: quem barra é o `genome_check` do autopilot, que roda
`improve/mutate.py:check` (via `check_patch`) ANTES de escrever e recusa
fail-closed. Isso cobre a mutação que o próprio loop propõe; não cobre um
backend que escreva em `harness/ruler/**` por conta própria durante a run.

| zona | por quê |
|---|---|
| `harness/ruler/**` immutable | quem mede e quem decide não se muda: a mutação aprovaria a si mesma |
| `harness/genome/**` immutable | quem define o que pode mudar também não se muda |
| `harness/routing/**` immutable | senão a proposta se dá o tier caro e falseia o próprio A/B |
| `harness/graph/**` immutable | a topologia é o processo; o loop calibra TOML, não nós |
| `uv.lock` immutable | trocar versão de dep por baixo invalida qualquer comparação |
| `benchmarks/sealed/**` immutable | se o loop reescreve a prova, a nota não vale nada |
| `config/*.toml`, `prompts/**` mutable | a zona calibrável — e `catalog.toml` é vedado ao próprio loop (`genome:self_edit`) |

## Ledger

`harness/ledger/store.py`, SQLite em `$HARNESS_DATA_DIR/runs.sqlite`. `RunRow`
tem `backend`, `model`, `tier`, `kind`, `sec_total`, `sec_provision`,
`cost_usd`, `intervention` **desde a linha 1** — é o que permite prior keyed em
`(kind, tier, backend)` e medição de autonomia. `MutationRow` guarda o veredito
por mutação. TSV é export, nunca fonte.

## Velho → novo

O legado está em `legacy/` (read-only). O que renasceu e o que mudou:

| legado | virou | o que mudou |
|---|---|---|
| `router.py` (tier + Wilson + escalation) | `harness/routing/` | prior era keyed em `(task_class, task_class)` — classe e tier colapsados, impossível aprender "tier X é ruim pro kind Y". Agora `(kind, tier, backend)`, com `kind` ortogonal ao custo |
| tamper + genoma | `harness/genome/` + `config/genome.toml` | zonas declaradas em config, não hardcoded; falha fechado em contradição |
| verify + KPI + gate + revert | `harness/ruler/` | a decisão virou um combinador só (`ruler/gate.py`), em vez de espalhada pelo executor; specs de KPI lidas ANTES da mudança. Ressalva: hoje quem chama `ruler.gate` é `cli.run_once` — o nó `gate` do run_graph ainda decide sozinho (stub, ver "Estado do grafo") |
| nota humana | `harness/ruler/note.py` | append-only, `HARNESS_PROJECTS_ROOT`, sem tool de escrita para o agente |
| `run_task.py` + copytree de workspace | `harness/workspace/provision.py` | `git worktree` + symlink de cache; `harness bench provision --n 10` mede p50 (0,069s na máquina onde esta doc foi escrita) |
| `results.tsv` como fonte | `harness/ledger/store.py` | SQLite é fonte, TSV é export; linhas velhas NÃO migram (sem `backend`/`kind`, envenenariam o prior) |
| `agent.py` (loop próprio, vendor no meio) | `harness/backends/` | o loop de execução é do deepagents; o repo só define o contrato de 3 métodos |
| pipeline architect→builder→verifier→review | topologia do `run_graph` | `route` → `execute` → `verify`+`measure` → `gate` → `accept` |

## Riscos que a estrutura endereça

1. **API jovem do deepagents** → pins exatos no `uv.lock` (immutable) e nenhum
   import de `deepagents`/`langchain` fora de `backends/deepagents_backend.py`.
2. **LangSmith / telemetria** → vetada; `cli._bootstrap()` seta
   `LANGCHAIN_TRACING_V2=false`, `LANGSMITH_TRACING=false` e
   `LANGGRAPH_STRICT_MSGPACK=true` (checkpoint 3.x executa código na
   desserialização msgpack sem isso).
3. **Nó não-idempotente quebra o resume** → toda escrita externa passa pelo
   ledger com chave `(run_id, node)`.
4. **Goodhart** → KPI fora do genoma, `benchmarks/sealed/**` immutable, nota
   humana como KPI independente. Sem gradiente novo, `pick_target` devolve
   `None` e o loop escala pro humano em vez de improvisar.
5. **Budget vs. paralelismo** → `deadline_ts` checado na entrada de cada nó E
   antes de cada run do A/B; estourou, o experimento é abortado inteiro (braço
   com N diferente é amostra envenenada, não amostra pequena).
