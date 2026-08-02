# SPEC — rebuild in-place do harness-core (LangGraph + deepagents, MIT, provider-agnostic)

> Gerada pelo architect em 2026-08-02. Decisões confirmadas: evolução in-place, licença MIT, open source, provider-agnostic. Fonte dos requisitos: memory `handoff-recomeco-deepagents`.

**Decisão:** núcleo novo em `harness/` (pacote Python), legado root movido para `legacy/` congelado; LangGraph = orquestração, deepagents = executor default via um contrato `Backend` de 3 métodos, régua/genoma/router portados como código NOVO (semântica preservada, vendor removido).

**Por quê:** o valor provado do repo velho está na *régua* (Wilson/KPI/tamper/nota humana), não no encanamento — encanamento é o que LangGraph/deepagents fazem melhor. Trade-off aceito: perde-se o histórico numérico do `results.tsv` (linhas presas a um único modelo/vendor) em troca de um ledger com `backend`+`kind` desde a linha 1.

**Prior art (levantado, não suposto):** REUSAR LangGraph (StateGraph, `SqliteSaver`, `interrupt()`, `Send` para fan-out), deepagents (loop de execução, planning/subagent/filesystem tools), LangChain (`init_chat_model`, `langchain-ollama`). ROUBAR PADRÃO: archive de variantes do DGM, gate held-in/held-out do Self-Harness, SkillScan + sandbox-por-run do deer-flow. CRIAR (nada existente serve): régua Wilson+KPI com revert, genoma mutable/immutable + tamper, router determinístico por custo, replay/atribuição. VETADO: LangSmith, GraphRAG no núcleo, Mem0/vector DB até dor medida.

---

## 1. Estrutura de diretórios

```
harness-core/
  pyproject.toml            # NASCE: deps pinadas + [project.entry-points."harness.backends"]
  uv.lock                   # NASCE: lock imutável no genoma
  LICENSE                   # MIT
  README.md STATUS.md
  harness/                  # NASCE: núcleo, zero menção a vendor
    cli.py
    types.py                # UnitSpec, ExecRequest, ExecResult, Selection, Verdict
    backends/  base.py registry.py mock.py deepagents_backend.py claude_code.py auth/base.py
    graph/     state.py run_graph.py autopilot_graph.py checkpoint.py
    ruler/     wilson.py kpi.py verify.py note.py gate.py
    genome/    genome.py tamper.py
    routing/   router.py kinds.py
    workspace/ provision.py         # git worktree, mata o copytree
    ledger/    store.py             # SQLite + export TSV
    improve/   target.py mutate.py replay.py escalate.py
  config/      models.toml kinds.toml tools.toml catalog.toml genome.toml   # calibráveis pelo loop
  benchmarks/  held_in/ sealed/
  data/        runs.sqlite results.tsv checkpoints.sqlite   # gitignored
  projects/    # projetos privados do operador — gitignored, fora da história pública
  tests/
  legacy/      # REFERÊNCIA read-only, fora do pytest, fora do genoma
```

**Morre (delete, git history é o arquivo):** `whatsapp.py`, `channel/`, `delivery.py`, `assist.py`, `harness_cli.py`, `graph_query.py`, `arena/`, `attic/`, `.harness_build_ws/`.

**Vai pra `legacy/` (referência de porte):** `score.py`, `router.py`, `note.py`, `kpi.py`, `autopilot.py`, `project.py`, `run_task.py`, `runplan.py`, `graph.py`, `safety.py`, `evolution/`, `results.tsv`.

## 2. Módulos do núcleo (interfaces)

```python
# harness/ruler/wilson.py   — régua. NUNCA no genoma mutável.
def wilson_interval(succ:int, n:int, z:float=1.96) -> tuple[float,float]
def decide_ab(a:Arm, b:Arm, min_n:int=6) -> Literal["KEEP","DISCARD","INCONCLUSIVE"]

# harness/ruler/kpi.py
def load_kpis(repo:Path) -> dict[str, KpiSpec]           # nome -> cmd + direction
def collect(repo:Path, timeout_s:float) -> dict[str,float]
def regressed(before:dict, after:dict, specs:dict) -> list[str]   # [] => sem regressão

# harness/ruler/verify.py
def run_verify(unit:UnitSpec, ws:Path) -> Verdict        # passed, exit_code, log_path, sec

# harness/ruler/note.py    — nota humana; agente NUNCA escreve
def add(project:str, unit_id:str, score:int, tags:str, why:str) -> Path
def kpi_value(project:str, window:int=20, min_notes:int=3) -> float|None

# harness/ruler/gate.py    — combinador único usado pelo grafo
def gate(verdict:Verdict, kpi_before:dict, kpi_after:dict, tamper:list[str]) -> Decision
   # Decision ∈ accept | retry | revert | escalate_human, com `reason` textual

# harness/genome/genome.py
def load(path:Path=Path("config/genome.toml")) -> Genome  # falha se path casa mutable e immutable
def check_patch(g:Genome, changed:Iterable[str]) -> list[str]   # violações
# harness/genome/tamper.py
def fingerprint(g:Genome, root:Path) -> str
def detect(root:Path, before:str, changed:Iterable[str]) -> list[str]  # ["tamper:genome_violation", ...]

# harness/routing/kinds.py — rótulo ORTOGONAL ao custo
Kind = Literal["code","content","config","refactor","infra"]
def classify_kind(unit:UnitSpec, cfg:dict) -> tuple[Kind, list[str]]   # determinístico: extensões + keywords

# harness/routing/router.py — tier = CLASSE DE CUSTO
def select(unit:UnitSpec, history:Sequence[RunRow], attempt:int=0, cfg:dict|None=None) -> Selection
def should_escalate(sel:Selection, verdict:Verdict, attempt:int, cfg:dict) -> bool
# Selection = (backend:str, model:str, tier:str, kind:Kind, max_turns:int, reasons:list[str])

# harness/workspace/provision.py — ataca o throughput
def provision(repo:Path, run_id:str, mode:Literal["worktree","tmpdir"]="worktree") -> Workspace
   # git worktree add --detach + symlink de caches (node_modules, .venv, .cache) via config/tools.toml
def dispose(ws:Workspace, keep:bool) -> None

# harness/ledger/store.py
def record_run(row:RunRow) -> int                # SQLite; TSV é export, não fonte
def history(project:str|None, kind:Kind|None, backend:str|None, limit:int=500) -> list[RunRow]

# harness/improve/target.py
def pick_target(history:Sequence[RunRow], catalog:list[Rule]) -> Target|None
   # ganho esperado = freq(erro) * custo_medio(erro) * prior_de_sucesso(regra); None => nada vale a pena
# harness/improve/replay.py
def attribute(mutation_id:str, before:Sequence[RunRow], after:Sequence[RunRow]) -> Attribution
```

## 3. Estado e grafos LangGraph

```python
# harness/graph/state.py
class RunState(TypedDict):
    run_id: str; unit: UnitSpec; attempt: int
    selection: Selection | None
    workspace: str | None
    exec: ExecResult | None
    verdict: Verdict | None
    kpi_before: dict[str,float]; kpi_after: dict[str,float]
    tamper: list[str]
    decision: Decision | None
    budget: Budget            # spent_usd, deadline_ts, max_attempts
    events: Annotated[list[Event], operator.add]   # append-only, é o trace
```

**run_graph** (`harness/graph/run_graph.py`):
`plan → route → provision → execute → verify → measure → gate → [accept: commit | retry: route | escalate: human | revert: rollback] → record → END`

- `human` = `interrupt({"reason":..., "unit":...})` (stop rule nativo do LangGraph); retomar = `invoke(Command(resume=...), config={"configurable":{"thread_id":run_id}})`.
- Checkpointer: `SqliteSaver` em `data/checkpoints.sqlite`, `thread_id = run_id`. Nós idempotentes: `provision` reusa worktree existente; `execute` só reexecuta se `exec is None`.
- Topologia é **immutable** no genoma. O loop só calibra `config/*.toml`.

**autopilot_graph**: `pick_target → propose → genome_check → apply → fanout_ab (Send: N unidades × braço A/B, paralelo) → score → [KEEP: commit | DISCARD/INCONCLUSIVE: revert] → attribute → record`. Fan-out via `Send` é o segundo ataque estrutural ao throughput serial.

## 4. Contrato do backend-adapter

```python
# harness/backends/base.py
@dataclass(frozen=True)
class Capabilities:
    resumable: bool; reports_cost: bool; model_selectable: bool
    tools: frozenset[str]; streaming: bool

@dataclass(frozen=True)
class ExecRequest:
    prompt: str; workspace: Path; tools: tuple[str,...]
    model: str|None; max_turns: int; timeout_s: float
    env: Mapping[str,str]; session_id: str|None; trace_path: Path

@dataclass(frozen=True)
class ExecResult:
    ok: bool; exit_reason: str        # done|max_turns|timeout|error|blocked
    turns: int; cost_usd: float|None; tokens_in: int|None; tokens_out: int|None
    files_changed: tuple[str,...]; session_id: str|None; trace_path: Path

class Backend(Protocol):
    name: ClassVar[str]
    def capabilities(self) -> Capabilities: ...
    def preflight(self) -> Preflight: ...          # (ok, reason) — determinístico, ZERO chamada de LLM
    def execute(self, req: ExecRequest) -> ExecResult: ...
```

Registro por entry point `harness.backends` (plugin de terceiros sem tocar no núcleo). Auth plugável: entry point `harness.auth`, `class AuthAdapter(Protocol): def env(self)->Mapping[str,str]; def check(self)->Preflight`. **Nenhum adapter de OAuth-de-assinatura é especificado nem shippado** — só o slot; quem quiser publica fora do repo.

Implementações no repo: `mock` (determinístico, para teste), `deepagents` (default; modelo via `init_chat_model`, roda Ollama grátis), `claude_code` (subprocess do CLI oficial; `resumable=True` via `--resume`).

## 5. Velho → novo

| Padrão validado | Renasce como |
|---|---|
| Router tier de custo + Wilson + escalation | `routing/router.py`. **Corrige o bug de chave**: o velho fazia `history_prior(rows, task_class, task_class)` — classe e tier colapsados, impossível aprender "tier X é ruim pro kind Y". Novo prior é keyed em `(kind, tier, backend)`. `prior_floor` continua knob em `config/models.toml`. |
| Tamper + genoma mutable/immutable | `genome/` + `config/genome.toml`. Immutable: `harness/ruler/**`, `harness/genome/**`, `harness/routing/**`, `harness/graph/**` (topologia), `uv.lock`, `benchmarks/sealed/**`. Mutable: `config/*.toml`, `prompts/**`. |
| Verify + KPI com gate e revert | `ruler/gate.py` chamado pelo nó `gate`; revert = `git worktree` descartado + `ledger` marca `reverted`. |
| Nota humana ≥3 notas | `ruler/note.py`; `projects/**/notes.tsv` immutable; agente sem tool de escrita nela (allowlist de tools do `ExecRequest`). |
| architect→builder→verifier→review→commit | Vira topologia: `route`(architect) → `execute`(builder) → `verify`+`measure`(verifier) → `gate`(review) → `commit`. Papéis = subagents do deepagents, definidos em `config/tools.toml`. |
| `results.tsv` fonte de verdade | `data/runs.sqlite` é fonte; TSV vira export (`harness export`). Colunas novas obrigatórias: `backend`, `kind`, `tier`, `sec_total`, `sec_provision`, `intervention`. |

## 6. Escada de PRs

| PR | Escopo | Aceite executável |
|---|---|---|
| **PR-0** | `pyproject.toml`, `harness/` esqueleto, `types.py`, `backends/base.py` + `mock`, `ledger/store.py`, `cli.py`, LICENSE MIT, `legacy/` movido | `uv run pytest -q` verde; `uv run harness run --unit tests/fixtures/echo --backend mock` sai 0 e insere 1 linha em `data/runs.sqlite` |
| **PR-1** | Backend `deepagents` + `init_chat_model` + Ollama; `preflight` | `uv run harness run --unit tests/fixtures/tiny_fix --backend deepagents --model ollama:qwen3:4b` altera o arquivo alvo, custo $0 |
| **PR-2** | `graph/run_graph.py` + `SqliteSaver`; nós idempotentes | `tests/test_resume.py`: `kill -9` durante `execute`, re-invoke mesmo `thread_id` → run completa e `execute` roda **uma** vez (assert no ledger) |
| **PR-3** | `workspace/provision.py` (worktree + symlink de cache) | p50 de `sec_provision` ≤ 2s em 10 runs; `harness bench provision` imprime p50 |
| **PR-4** | `ruler/` completo (wilson, kpi, verify, note, gate) | 3 casos de aceite do D2 batem; `harness ab --a X --b Y` imprime KEEP/DISCARD/INCONCLUSIVE; regressão de KPI força `revert` |
| **PR-5** | `genome/` + tamper + `config/genome.toml` | patch em `harness/ruler/wilson.py` → `tamper:genome_violation`; patch em `config/models.toml` → passa |
| **PR-6** | `routing/` router + kinds ortogonais | 13 unidades do teste velho reproduzem o tier esperado; `test_prior_keyed`: histórico ruim de `(code, tier0)` não afeta `(content, tier0)` |
| **PR-7** | Backend `claude_code` + slot `harness.auth` | `harness backends` lista 3 com `preflight`; mesmo unit roda nos 3 e grava `backend` no ledger |
| **PR-8** | A/B de backend rodado pelo próprio harness | `harness ab --dim backend --n 6` emite veredito Wilson escolhendo executor por evidência |
| **PR-9** | `autopilot_graph` + `improve/target.py` + `escalate.py` (interrupt) + `intervention_rate` | 20min sem intervenção: ≥5 runs, ≥1 mutação avaliada, zero escrita fora do ROOT, `intervention_rate` no relatório |
| **PR-10** | `improve/replay.py` (atribuição por mutação) + docs + publish | `harness replay --mutation <id>` mostra delta atribuído com IC; README com quickstart Ollama |

## 7. Riscos e recomendação

1. **deepagents é jovem (API instável).** → Pinar exato no `uv.lock` (immutable) e **nunca** importar tipos dele fora de `backends/deepagents_backend.py`. O núcleo só conhece `Backend`.
2. **LangGraph puxa LangChain inteiro + telemetria.** → Setar `LANGCHAIN_TRACING_V2=false`/`LANGSMITH_TRACING=false` no bootstrap do `cli.py` e um teste que falha se qualquer var `LANGSMITH_*` estiver ligada.
3. **Perda do histórico do `results.tsv`.** → Aceitar. Rows antigas não têm `backend`/`kind`; misturar envenena o prior. `legacy/results.tsv` fica só para leitura humana.
4. **Nós não-idempotentes quebram o resume** (risco #1 do PR-2). → Toda escrita externa dos nós passa por `ledger` com chave `(run_id, node)`; `execute` consulta antes de chamar backend.
5. **Goodhart na régua.** → KPI calculado fora do genoma + `benchmarks/sealed/**` immutable + nota humana como KPI independente. Sem gradiente novo, o loop não roda: `pick_target` retorna `None` e escala pro humano.
6. **Paralelismo (Send) vs. budget.** → Semáforo por `Budget.max_parallel` em `config/models.toml` e `deadline_ts` checado no início de cada nó — kill por SIGTERM.
7. **Área cinzenta de ToS no auth.** → Só o slot entra no repo; nenhum adapter de OAuth de terceiro, nenhuma doc ensinando. Linha explícita no README.

**Verificação global do rebuild:** `uv run pytest -q && uv run harness doctor && uv run harness run --unit tests/fixtures/tiny_fix --backend deepagents` — `doctor` faz preflight de todos os backends + checa tamper + confirma tracing desligado.
