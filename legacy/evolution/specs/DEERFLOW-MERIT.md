# deerflow-merit — spec do architect (2026-08-02)

Doutrina (Renan): autonomia inteligente + self-improvement DENTRO das áreas customizáveis do DeerFlow; PR de core só se muuuito necessário com ganho grande. 1º PR aberto: bytedance/deer-flow#4644.

## Princípio

**O loop de mérito roda FORA do run do DeerFlow, o resultado entra DENTRO.** DeerFlow não tem noção de "task com verify determinístico"; ele é o agente sob teste, não o experimentador. Nosso autopilot/experiment é o processo pai que dispara runs via API e decide.

## Mapa de encaixe (tudo superfície customizável)

| Peça nossa | Superfície DeerFlow | Fluxo |
|---|---|---|
| Gate Wilson (`score.decide_ab`) + `kpi_report` | MCP server stdio `merit` (`extensions_config.json → mcpServers`) + CLI | Ledger SQLite `.harness/merit.db` (run_id, variante, success do verify, kpi_*) + JOIN read-only com `.deer-flow/data/deerflow.db` (runs: status, token_usage, custo). Saída KEEP/DISCARD/INCONCLUSIVE aplicada em `skills/custom/<skill>/SKILL.md` + extensions_config. |
| Coleta por run | Middleware `MeritLedgerMiddleware` (item 31 da chain, `extensions.middlewares`) | `wrap_model_call`/`wrap_tool_call` gravam por thread_id/run_id: skills ativas, tools, erros (`deerflow_tool_meta.error_type` já carimbado), turnos. Success NÃO vem daqui — vem do verify do processo pai. |
| Catálogo determinístico de propostas | MCP tool `merit_propose` + skill `merit-evolve` (SKILL.md) | merit.db + tool_meta agregados → erro dominante → proposta de mutação de SKILL.md. Zero LLM na classificação. Agente executa via `skill_manage`. |
| Probation/revert | Processo pai + MCP tool `merit_probation_check`; scheduled task opcional pro disparo | Snapshot = git próprio de `skills/custom/` (`.harness/skills.git`). Revert = `git checkout` + `POST /api/skills/reload`. |
| Router de modelo | Default: `subagents.agents.<tipo>.model` no config (zero código). Middleware `MeritModelRouterMiddleware` só se decisão por-turno. | |
| Autopilot de fila | Processo nosso, cliente do Gateway (`POST /api/threads/{id}/runs`) | Scheduled task deles não serve (não-interativa, sem A/B/revert). Fila, budget, SIGTERM, probation ficam nossos. |

## Ganchos de core (ranqueados)

1. **`skill_evolution.write_policy: "module:callable"`** — chamado com `(name, updates, runtime)` antes da escrita de skill, allow/deny+reason (padrão GuardrailProvider que eles já usam). ~60 linhas + testes. **ÚNICO que passa na régua** — sem ele o agente escreve skill sem prova e só revertemos depois. É o PR estratégico.
2. Middleware com config/escopo (lead vs subagent) — contornável com env var. Não propor.
3. `context.external_outcome` persistido em runs — contornável com ledger por run_id. Não propor.
4. Hook de resultado por skill — contornável via audit event. Não propor.

## Produto

Pacote pip **`deerflow-merit`** (nosso repo): `middlewares/{ledger,router}.py`, `mcp_server.py` (stdio), `skills/merit-evolve/SKILL.md`, `cli.py` (`merit doctor|run-ab|probation`). Instala: `uv pip install -e` no venv do backend + entradas no `extensions_config.json`. Upstream só o PR do write_policy (nosso callable como exemplo na doc).

## Incremento vertical 1 (1-2 semanas, $0 além de Ollama)

"1 skill custom, 1 mutação, gate Wilson decide, revert automático":
- Ollama local (qwen2.5:3b) + sqlite; 3 tasks held-in nossas; 6 runs por variante (MIN_N=6) via `merit run-ab` no Gateway REST.
- A = `skills/custom/merit-demo/SKILL.md` v1; B = v2 escrita por `skill_manage` (skill_evolution on).
- Ledger grava; verify externo grava success; `decide_ab` decide; DISCARD → checkout + reload.
- **Aceite**: `scripts/accept_merit_v1.sh` → 12 linhas no merit.db, JSON com decision ∈ {KEEP,DISCARD,INCONCLUSIVE}, skills.git limpo pós-revert, `make test` do deer-flow verde, zero diff no clone fora de config/extensions (gitignored).

## Riscos/blindagem

- run_id pode não chegar no runtime do middleware → fallback thread_id (1 thread por task). VERIFICAR DIA 1 (researcher: hooks disponíveis do AgentMiddleware na versão pinada + run_id acessível?).
- skill_evolution default off + security_fail_closed → `merit doctor` falha alto, nunca degrada silencioso.
- 119 commits/semana → contato só por 5 contratos estáveis (extensions_config, MCP stdio, SKILL.md, REST /api, SQLite runs read-only); proibido importar `deerflow.*` interno fora de AgentMiddleware; clone pinado + `test_deerflow_contract.py` semanal contra upstream/main.
- write_policy recusado → fallback probation reativa (escreve, mede, reverte). Não travar roadmap esperando merge.
