# AGENTS.md — constituição para qualquer agente de IA neste repo

Vale para Claude Code, Cursor, o autopilot do próprio harness e humanos apressados. Fonte de verdade de estado: `STATUS.md`. Fonte de verdade do genoma: `config/genome.toml`.

## O que o repo é

Harness auto-evolutivo sobre LangGraph: pacote `harness/` (núcleo provider-agnostic; executor padrão deepagents, backend claude_code opcional) roda unidades de trabalho num grafo (provision → plan → route → execute → verify → measure → gate → record), mede KPIs contra baseline congelada e evolui a si mesmo por 7 ações registradas (research, codegen, synthesize, topology, evolve, skill_prune, prompt) — sempre dentro das zonas mutáveis do genoma, julgado por uma régua que ele não pode tocar. Ledger em `data/runs.sqlite`; legado congelado em `legacy/` (read-only, fora do pytest e do genoma).

## Zonas do genoma — NUNCA editar sem humano

Espelho de `config/genome.toml` (que também é fail-closed em runtime):

- `harness/ruler/**` — quem mede e decide não se muda: mutação que reescreve a régua aprova a si mesma.
- `harness/genome/**` — quem define o que pode mudar também não se muda.
- `harness/routing/**` — quem escolhe o modelo não se muda: senão a proposta se dá o tier caro e falseia o A/B.
- `harness/graph/**` — a topologia é o processo; o loop calibra os toml, não os nós.
- `uv.lock` — deps pinadas: trocar versão por baixo invalida qualquer comparação.
- `benchmarks/sealed/**` — exame selado: se o loop reescreve a prova, a nota não vale nada.

Mutável (onde agente pode operar): `config/*.toml`, `prompts/**`, `skills/**`, `plugins/**`, `benchmarks/quarantine/**`. Selar exame (quarantine → sealed) é ato humano: `harness seal <name> --yes`.

## Comandos canônicos

```sh
uv run --extra deepagents pytest -q                 # suíte (verde atual: 499 passed, 2 deselected)
uv run harness doctor                                # 10 checks de sanidade
uv run harness lineage --file --db --limit 20        # árvore de mutações + verdicts
uv run harness run --unit tests/fixtures/tiny_fix --backend mock   # E2E mock, custo $0
```

Setup: `uv sync --extra deepagents`. E2E real barato: `--backend deepagents --model ollama:qwen2.5:3b`.

## Estilo

- PT-BR, terso. Docs e commits idem.
- Comentário só para restrição não-óbvia (o *porquê*); nunca parafrasear o código.
- Fail-closed por padrão em tudo que toca genoma/régua; fail-open só onde já documentado (policy de graph, MCP).
- Skill = `skills/<nome>.md`: `---`, TOML (`name`/`kinds`/`description`), `---`, corpo markdown; `kinds ⊆ {code, content, config, refactor, infra}`.

## Armadilhas conhecidas

- **LangSmith VETADO.** Tracing off no bootstrap; `LANGGRAPH_STRICT_MSGPACK=true`. Não reintroduzir.
- **Nota humana é humana.** Agente NUNCA escreve nota humana; ≥3 notas para valer como KPI.
- **Histórico legacy não entra no prior.** `legacy/results.tsv` sem colunas backend/kind envenenaria o Wilson novo.
- **Verify = exit code.** O veredito é o exit code do comando de verify, nunca a palavra do agente ("passou" sem rodar não existe).
- **Escritas em zona mutável são atômicas** e passam pelo genome check ANTES de tocar disco — seguir o padrão de `improve/research.py`.
