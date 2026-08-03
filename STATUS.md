# STATUS — fonte de verdade do harness-core

**Atualizado:** 2026-08-02 (rebuild in-place executado). Norte técnico: `docs/SPEC-rebuild.md`. API real das libs: `docs/RESEARCH-deepagents-api.md`. Este arquivo substitui a versão anterior (pivô LangGraph+LangChain com executor Claude CLI) — o rebuild foi além: núcleo provider-agnostic, executor padrão deepagents, licença MIT, open source.

## O que o repo é agora

Pacote `harness/` (núcleo, zero menção a vendor) sobre LangGraph; legado congelado em `legacy/` (referência read-only, fora do pytest e do genoma). Fonte de verdade de runs: `data/runs.sqlite` (TSV vira export). Projetos privados vivem em `projects/` mas ficam fora do git (gitignored) — o repo público carrega só fixtures e benchmarks sintéticos.

## Escada de PRs (docs/SPEC-rebuild.md §6)

| PR | O quê | Estado |
|---|---|---|
| PR-0 | Esqueleto `harness/`: types, backend mock, registry por entry point, ledger SQLite, cli | **FEITO** `2684cb9` |
| PR-1 | Backend deepagents (LangChain isolado em 1 arquivo) + Ollama E2E grátis | **FEITO** `ad2a279` |
| PR-2 | run_graph LangGraph + SqliteSaver + nós idempotentes (resume pós-kill-9 é teste) | **FEITO** merge `fa2c65c` |
| PR-3 | workspace/provision via git worktree (p50 medido: 0.092s vs 50-200s do legado) | **FEITO** merge `a98bea1` |
| PR-4 | ruler/ completo: wilson, kpi (specs do ANTES anti-Goodhart), verify, note, gate | **FEITO** merge `38b2caf` |
| PR-5 | genome/ mutable-immutable + tamper + config/genome.toml | **FEITO** merge `71c2fb7` |
| PR-6 | routing/: kinds ortogonais + prior Wilson keyed (kind,tier,backend) — bug de chave do legado corrigido | **FEITO** merge `cb3f86c` |
| PR-7 | Backend claude_code (CLI oficial) + slot harness.auth plugável | **FEITO** `a8e6717` |
| PR-8 | A/B de backend rodado pelo próprio harness (`harness ab --dim backend`) | **FEITO** `bc0714e` |
| PR-9 | autopilot_graph + improve/ (target, escalate via interrupt, intervention_rate) | **FEITO** `1760378` + PR-9b `af1787c` (canal causal provado: KEEP/DISCARD reais) |
| PR-10 | improve/replay (atribuição c/ IC e confounders) + harness doctor + README/ARCHITECTURE/CONTRIBUTING | **FEITO** merges `ca19215`+`5fe035a` |

**ESCADA COMPLETA (2026-08-02).** 402 testes; `harness doctor` 10 checks 0 falhas. Próximo: publicação (repo novo, commit único — plano na memory).

## Sprint auto-evolução (2026-08-03)

- **run_graph de-stubbed:** `provision` congela baseline no ledger (specs+valores de KPI do ANTES + fingerprint de tamper do genoma do workspace, padrões congelados — a run não redefine a própria régua); `measure` coleta KPIs do DEPOIS com as specs congeladas; `gate` chama o `ruler/gate` real (tamper→revert, verify vermelho→retry, KPI regrediu→revert, senão accept), retry vira `escalate_human` no teto de attempts; `record` carrega os motivos de revert como `exit_reason`. Política auto-evoluível em `config/graph.toml` (`max_attempts`, `verify_timeout_s`, toggles `[nodes]`), fail-open por campo; toggles off reproduzem o stub antigo. `run_unit(max_attempts=None)` lê da policy. Testes: `tests/test_graph_policy.py`.
- **skills/**: pacote `harness.skills` (load/select/render, formato `---`/TOML/`---`); injetado no system_prompt do backend deepagents por `kind`. Seeds: `skills/python-fixes.md`, `skills/config-calibration.md`. `skills/**` agora é mutável no genoma. Testes: `tests/test_skills.py`.
- **MCP:** `harness/backends/mcp_tools.py` lê `config/mcp.toml` (stdio + streamable_http, só `enabled=true`) via langchain-mcp-adapters (import lazy, dep opcional); qualquer falha → `[]`, nunca quebra. Testes: 6 em verde.
- **research:** ação de auto-evolução (`harness/improve/research.py`): `propose_research` acha o kind com mais falhas repetidas no ledger (determinístico; sem gradiente → None), `apply_research` passa pelo genome check fail-closed ANTES do backend e escreve `skills/<slug>.md` atomicamente. Registro via Action registry em `improve/target.py`. Testes: `tests/test_research.py`.

Suite completa pós-sprint: 428 passed, 2 deselected.

## Escada evolutiva (sprint 2) — 2026-08-03

- **topology:** topologia do run_graph vira dado — `harness/graph/topology.py` (whitelist `NODE_IMPLS` c/ nó novo `reflect` pass-through, `load_spec`/`compile_spec` fail-closed via `TopologyError`); `build_run_graph` tenta `config/topology.toml`, qualquer falha → 1 linha no stderr + topologia embutida inalterada. Toml shipado reproduz a topologia atual. 26 passed.
- **evolve:** `harness/evolve` — population PBT (`mutate_config`/`crossover`/`run_population`, seleção por Wilson lower bound, elitismo 25%) + archive MAP-Elites em sqlite próprio (`data/archive.sqlite`, nichos `(kind, cost_bucket)`). Não registrado no Action registry ainda. 7 passed.
- **codegen:** zona de código mutável `plugins/` (seed `kpi_lines.py`) + `harness/improve/codegen.py`: `propose_code_mutation` (genome check fail-closed, `ast.parse`, escrita atômica, linhagem em `data/lineage.jsonl`) e `judge_code_mutation` (exame injetado; DISCARD restaura/apaga). `plugins/**` agora mutável no genoma. 39 passed.
- **meta-ruler:** knobs do juiz em `config/ruler.toml` (`[gate].kpi_regression_tolerance`, default congelado 0.0 em qualquer falha de leitura); `meta.py` c/ `meta_check` (allowed/quarantined/blocked — mudar o juiz exige exame selado + ack humano). 64 passed.
- **synthesize:** `synthesize_from_failures` gera exames de quarentena (`benchmarks/quarantine/`) a partir de runs falhas/revertidas do ledger; `harness seal <name> --yes` move quarantine → sealed (selar é ato humano). `benchmarks/quarantine/**` agora mutável no genoma; `sealed/**` segue imutável. 16 passed.

## Ciclo fechado (sprint 3) — 2026-08-03

- **wiring:** `harness/improve/actions.py` — 3 adapters no padrão research/codegen: `synthesize` (propose deduplica falhas do history; apply delega a `synthesize_from_failures`), `topology` (propose insere nó `reflect` da whitelist numa aresta linear, determinístico; apply = genome_check → `compile_spec` → escrita atômica, spec inválida recusa sem tocar disco), `evolve` (propose = `population.mutate_config` sobre `config/models.toml` com rng seedado de sha256(thread_id, cycle); apply = genome_check → dump TOML c/ roundtrip-check → escrita atômica). `target.actions()` registra os 3 + tentativas guardadas (ImportError/AttributeError) de `skills.attribution` e `improve.prompt_evolve`. Autopilot `_apply` roda `meta.meta_check(human_ack=False)` antes de qualquer escrita; exame selado injetável via `configurable[CFG_SEALED_EXAM]` (default fail-closed `lambda: False`); quarantined/blocked → stop/escalate c/ `evidence.meta`. 77 passed + 54 vizinhas.
- **e2e:** `tests/test_e2e_evolution.py` — 7 testes, 4 fios: ciclo research (ledger→skill→injeção por kind, sem vazar pra outro kind), ciclo codegen (genoma REAL, KEEP/DISCARD, lineage encadeada), guardrails (GenomeViolation em `harness/ruler/**` sem efeito colateral; falha → unit de quarentena carregável), topologia declarada (`reflect` aparece entre plan e route). Suite inteira: 499 passed, 2 deselected. Nota documentada: DISCARD também entra em lineage.jsonl (append antes do veredito).
- **attribution:** `harness/skills/attribution.py` — tabela `skill_usage` no `data/runs.sqlite` (store.py intocado), `record_usage` idempotente, `lift()` com Wilson lower bound nos dois braços, `prune_candidates` (min_trials nos dois braços), action `skill_prune` (apply move pra `skills/attic/`, nunca deleta). Backend deepagents grava uso pós-`select_skills` e prependa `prompts/executor.md` ao system_prompt quando existe. Limitação: join usa session_id quando run_id falta — id divergente dilui o lift, nunca infla. 31 passed.
- **prompt-evolve:** ação `prompt` (PromptBreeder-lite) em `improve/prompt_evolve.py` — 4 operadores determinísticos, propose fail-closed (não escreve), apply atômico com before_text guardado, revert byte a byte. `prompts/executor.md` = prompt-base evoluível do executor. Corte anotado: sem lineage pra prompts (não pedido). 11 passed.
- **lineage:** `harness/improve/lineage.py` + subcomando `harness lineage --file --db --limit` — load tolerante a linha torta, árvore com órfão virando raiz, enrich com verdict da tabela mutations (não cria DB ausente), render ASCII. 40 passed (lineage + cli run/ab/gate).

Aceites rodados de verdade pelos builders; suite completa pós-sprint: 499 passed, 2 deselected.

## Sprint final (2026-08-03)

- **exam:** exame selado real — `harness/improve/exam.py`: `run_sealed_exam(backend='mock', model='', sealed_dir=None, data_dir=None) -> bool` e `exam_report(...) -> list[{id, passed}]`. Descoberta via `benchmarks/sealed/*/unit.toml`; cada unidade roda por `run_unit` com thread_id único; passed = gate `accept`. Fail-closed: sem unidades descobríveis → False + 1 linha stderr; exceção (unidade ou descoberta) → False. Seeds `sealed_s01`/`sealed_s02` criadas (determinísticas no mock); `task_s01`/`s02` intactos, fora do exame (sem `unit.toml` — entrar exige ato humano). `tests/test_exam.py`: 7 verdes; regressão de 103 testes vizinhos verde.
- **policy:** `harness/improve/policy.py` — bandit Wilson+UCB sobre KEEP-rate por ação, determinístico (rng seedado por `thread_id:cycle`), ação sem amostra nunca órfã. Plugado no autopilot: `_pick_target` escolhe via policy quando o chamador não fixa (param `action`/`CFG_ACTION`); `_record` grava `action=<nome>` no note da mutação (schema intocado) fechando o feedback — só em veredito concluído; rota forçada fica fora do bandit. Default do `CFG_SEALED_EXAM` agora é o exame real (`_default_sealed_exam` lazy, amarrado à raiz do ciclo; ImportError → False). 44+99 testes verdes. Pendente anotado: roteamento de backend real do exame default (fica `mock`).
- **surface:** doctor +7 checks de evolução (skills, topology, actions, ruler, mcp, lineage, executor) → 17 checks, 0 falhas/avisos no repo. CLI: `harness skills [--lift]` (nome/kinds/descrição; lift com com=/sem=/lift=) e `harness actions` (nomes + placar global KEEP/DISCARD). Corte anotado: sem mapeamento mutação→ação no placar (MutationRow só carrega rule_id). `tests/test_doctor.py` +8; 36 verdes.

Suite completa pós-sprint: 558 passed, 2 deselected.

Fechamento: o harness agora escolhe sozinho qual das 7 evoluções tentar (bandit por KEEP-rate), julga com exame selado real fail-closed, e expõe tudo em doctor/skills/actions/lineage.

Fiação feita (não é PR novo): o nó `route` do run_graph consome `routing/router.select()` no modo `auto` (`run_unit(route="auto")`, `harness run --route auto`), com escalação de tier por attempt; o modo `manual` — default, backend fixado pelo chamador — continua como era.

## Verificação global (última: 2026-08-02, 8/8 PASS)

`uv sync --extra deepagents && uv run --extra deepagents pytest -q` → 255 passed. Resume kill-9 real passa. `harness bench provision --n 10` p50=0.092s. `harness ab --a 5/6 --b 6/6` → INCONCLUSIVE com intervalos. E2E `harness run --unit tests/fixtures/tiny_fix --backend deepagents --model ollama:qwen2.5:3b` → accept, custo $0. Genome bloqueia patch em `harness/ruler/**`.

## Regras que não mudam

- Régua nunca no genoma mutável; loop calibra só `config/*.toml`.
- LangSmith VETADO (tracing off no bootstrap; `LANGGRAPH_STRICT_MSGPACK=true`).
- Nota humana: agente NUNCA escreve; ≥3 notas pra valer como KPI.
- Adotar > reinventar: capacidade nova só com dor medida no ledger.
- Histórico velho (`legacy/results.tsv`) não entra no prior novo — envenenaria (sem colunas backend/kind).
