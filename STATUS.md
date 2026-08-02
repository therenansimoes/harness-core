# STATUS — fonte de verdade do harness-core

**Atualizado:** 2026-08-02 (rebuild in-place executado). Norte técnico: `docs/SPEC-rebuild.md`. API real das libs: `docs/RESEARCH-deepagents-api.md`. Este arquivo substitui a versão anterior (pivô LangGraph+LangChain com executor Claude CLI) — o rebuild foi além: núcleo provider-agnostic, executor padrão deepagents, licença MIT, open source.

## O que o repo é agora

Pacote `harness/` (núcleo, zero menção a vendor) sobre LangGraph; legado congelado em `legacy/` (referência read-only, fora do pytest e do genoma). Fonte de verdade de runs: `data/runs.sqlite` (TSV vira export). `projects/website-faz-rogers/` segue CONGELADO.

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
| PR-9 | autopilot_graph + improve/ (target, escalate via interrupt, intervention_rate) | andaime commitado; aceite NÃO cumprido — C1 (canal causal) é PR-9b bloqueante |
| PR-10 | improve/replay (atribuição por mutação) + docs + publish | — |

Pendência de fiação (não é PR novo): o nó `route` do run_graph ainda é stub — ligar `routing/router.select()` quando o autopilot (PR-9) consumir o grafo.

## Verificação global (última: 2026-08-02, 8/8 PASS)

`uv sync --extra deepagents && uv run --extra deepagents pytest -q` → 255 passed. Resume kill-9 real passa. `harness bench provision --n 10` p50=0.092s. `harness ab --a 5/6 --b 6/6` → INCONCLUSIVE com intervalos. E2E `harness run --unit tests/fixtures/tiny_fix --backend deepagents --model ollama:qwen2.5:3b` → accept, custo $0. Genome bloqueia patch em `harness/ruler/**`.

## Regras que não mudam

- Régua nunca no genoma mutável; loop calibra só `config/*.toml`.
- LangSmith VETADO (tracing off no bootstrap; `LANGGRAPH_STRICT_MSGPACK=true`).
- Nota humana: agente NUNCA escreve; ≥3 notas pra valer como KPI.
- Adotar > reinventar: capacidade nova só com dor medida no ledger.
- Histórico velho (`legacy/results.tsv`) não entra no prior novo — envenenaria (sem colunas backend/kind).
