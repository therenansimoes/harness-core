# D6 — `autopilot`. Spec do architect (2026-08-02, HEAD cdefb26)

## 1. Decisão
**Módulo novo `autopilot.py`**: um único loop, dois tipos de passo (`step_project` = fila TSV do `project.py`; `step_self` = proposta de catálogo → `evolve.cycle`), um único orçamento/deadline. Não estender `project.py` (não pode importar `evolve`, é executor de fila) nem `evolve.py` (é blocklist do genoma — crescer ali aumenta a superfície imutável).

## 2. Por quê
Os dois loops compartilham exatamente o que precisa de teto (tempo, $, revert) e nada mais; um orquestrador fino sobre APIs já prontas (`project.try_run_one`, `evolve.cycle`, `score.ab_report`, `kpi.collect`) é REUSAR — nenhum código de execução novo. Trade-off aceito: autopilot vira ponto único de falha e não paraleliza projetos (multi-projeto real está fora da escada até existir 2º projeto).

## 3. Spec por arquivo

**`mockagent.py` (novo)** — `run(prompt, workspace) -> AgentResult`, move a lógica de `project._mock_agent` (diretivas `MOCK_TAMPER/MOCK_FAIL/MOCK_SLEEP`, +`MOCK_NOTES: <str>` para forjar `error_max_turns` etc.). `agent.run_agent` ganha, na 1ª linha: `if os.environ.get("HARNESS_MOCK_AGENT")=="1": return mockagent.run(...)`. `project._call_agent` delega ao mesmo módulo. Sem isso não existe aceite sem API.

**`evolution/genome.toml`** — acrescentar a `immutable`: `"autopilot.py"`, `"mockagent.py"`, `"evolution/catalog.toml"`. (Quem propõe não se muda.)

**`config.py`** — em `DEFAULTS["harness"]`: `autopilot_wall_clock_s = 1200`, `autopilot_budget_usd = 1.00`, `autopilot_max_iterations = 30`, `autopilot_self_every = 3`, `autopilot_probation_runs = 3`, `autopilot_allow_external_work_path = False`. Overrides por env `HARNESS_AP_MINUTES`, `HARNESS_AP_BUDGET`.

**`agent.py`** — inserir sentinela logo após o fechamento de `SYSTEM_PROMPT`: linha literal `# --- autopilot:prompt_tail ---`. É a âncora única para o template `append_prompt`.

**`evolution/catalog.toml` (novo)** — `[[rule]]`, ordem = desempate determinístico. Campos: `code`, `match_notes` (regex), `trace_any` (lista de regex sobre linhas do `trace.jsonl`, opcional; só refina), `action` ∈ `propose|ignore`, `file`, `kind` ∈ `bump_int|append_prompt`, `anchor`, `factor`/`max_value`/`text`, `hypothesis`. Entradas iniciais (baseadas nas notes reais do `results.tsv`: `max_turns` dominante, `tamper:test_file_modified` 3×, `cli_exit_1` 3×):
1. `max_turns` — `match_notes="(error_)?max_turns"`, `propose`, `agent.py`, `bump_int` em `MAX_TURNS`, `factor=1.5`, `max_value=60`.
2. `tamper_tests` — `match_notes="tamper:"`, `propose`, `agent.py`, `append_prompt` com "NUNCA edite arquivos test_*.py / verify.py; se o teste parece errado, reporte em vez de alterar."
3. `verify_env` — `match_notes="verify:"` + `trace_any=["ModuleNotFoundError","command not found","No module named"]`, `propose`, `agent.py`, `append_prompt` "Antes de finalizar, execute o comando de teste detectado no bloco de perfil e cole a saída."
4. `agent_timeout` — `match_notes="^timeout$"`, `propose`, `agent.py`, `append_prompt` "Prefira Edit direto; não releia arquivo já lido."
5. `cli_exit` — `match_notes="cli_exit_\\d+"`, `action="ignore"` (falha de infra, não do genoma: gerar proposta aqui é queimar budget).

**`autopilot.py` (novo)** — funções:
- `classify(notes) -> str`: primeira regra do catálogo cujo `match_notes` casa; `""` se nenhuma.
- `trace_signals(trace_path, patterns) -> collections.Counter`: lê `runs/<id>/trace.jsonl` linha a linha, conta regex. Só leitura, sem LLM.
- `dominant_error(rows) -> tuple[str,int]`: sobre as N linhas mais recentes com `success=0` (janela = `2*probation_runs+4`), agrupa por `classify`, refina com `trace_signals` quando a regra tem `trace_any`, retorna `max` por contagem; empate → ordem do catálogo. Determinístico e testável sem rede.
- `render_proposal(rule, root) -> Path`: lê o valor atual da âncora do arquivo alvo, verifica unicidade do `old` (mesma regra do `evolve.apply_change`), grava `evolution/proposals/auto-<code>-<ts>.md` no formato `+++` de `_template.md` com `from_version` = `harness_version.txt` e `to_version` = `<atual>+auto<n>`.
- `snapshot_genome() -> Path` / `restore_genome(snap)`: `shutil.copy2` de `evolve.genome_files()` + `harness_version.txt` para `evolution/rollbacks/<session>/<ts>/`. **Não usar git** (repo pode estar sujo; git é escrita fora do workspace lógico e history rewrite não é reversível barato).
- `step_project(s)` → `project.try_run_one(name, keep=False)`.
- `step_self(s)`: `dominant_error` → regra `propose` → `render_proposal` → `evolve.cycle(path, repeat=cfg, suite="fixed", force=False)`. Exit 0 (merge) → `s.probation = {"snap": snapshot_tomado_antes, "left": autopilot_probation_runs, "code": code}`.
- `probation_check(s)`: após cada `step_project` durante probation, decrementa; ao zerar compara janela pós vs. pré com `score.ab_report`/`score.kpi_report` (D4b). **Reverte se**: `kpi_verdict == "WORSE"` **ou** `success` da janela pós == 0 com ≥1 sucesso na pré **ou** qualquer `tamper:` novo. Revert = `restore_genome(snap)` + `evolution/decisions/<pid>-revert.md` (motivo, números das duas janelas, paths) + `graph` + `code` entra em `s.blocked_codes` (não reproposto na sessão).
- Tetos (mecanismo, não pedido): `signal.setitimer(signal.ITIMER_REAL, wall_s)` → handler levanta `Deadline`; `SIGTERM`/`SIGINT` → `Stop`; antes de cada passo, `os.environ["HARNESS_TIMEOUT"] = str(int(min(TIMEOUT_S, remaining_s())))` para o filho não sobreviver ao pai; `spent_usd()` soma `cost_usd` das linhas novas (global + `projects/*/results.tsv`) e aborta quando `>= budget`. `finally` sempre escreve o resumo.
- Escrita confinada: no start, `TMPDIR=ROOT/.harness_tmp`, `HARNESS_WS_ROOT=ROOT/.harness_ws`; recusa iniciar (exit 2) se algum projeto enabled tem `work_path` fora de `ROOT` e `autopilot_allow_external_work_path` é False.
- Log: `evolution/autopilot/<session>.jsonl`, um evento por passo (`ts, kind, result, cost_usd, elapsed_s, code, decision`).
- CLI: `python3 autopilot.py [--minutes N] [--budget X] [--project NAME] [--self-every N] [--dry-run]`. `--dry-run` = `HARNESS_MOCK_AGENT=1`. Exit 0 = terminou por fila vazia/iterações; 3 = parou por deadline; 4 = parou por budget.

**`tests/test_autopilot.py`** — unit puro: `classify` para as 5 notes reais; `dominant_error` com empate; `render_proposal` gera front matter que `evolve.parse_proposal` aceita e `evolve.genome_violations` aprova; `restore_genome` restaura bytes idênticos.

**Dependência D4b (assumida pronta):** `score.kpi_report(...)`. Se a assinatura real divergir, adaptar só a chamada em `probation_check` — não reimplementar.

## 4. Verificação
```bash
python3 -m pytest tests/ -q                      # verdes
bash tests/acceptance/d6_autopilot.sh            # aceite oficial, $0
```
`tests/acceptance/d6_autopilot.sh` (novo, builder escreve): cria projeto `demo_ap` com `work_path` sob `ROOT/.harness_demo`, enfileira 6 unidades (2 com `MOCK_NOTES: error_max_turns`), toca sentinela t0, roda `HARNESS_MOCK_AGENT=1 timeout 1260 python3 autopilot.py --minutes 20 --budget 0 --self-every 3 --project demo_ap`, e assere: exit ∈ {0,3}; `wc -l projects/demo_ap/results.tsv` cresceu ≥5; `evolution/autopilot/*.jsonl` tem ≥1 evento `kind=self`; nenhum arquivo novo fora de ROOT; `git status --porcelain` só sob paths esperados. **Smoke real (pago, separado, 1×):** `python3 autopilot.py --minutes 6 --budget 0.15 --self-every 99` com haiku — custo ~$0.10. O critério "20min sem intervenção" é do loop de controle, não da qualidade do modelo: não gastar API para prová-lo.

## 5. Fora do D6
Sem proposta por LLM; sem git branch/commit; sem paralelismo entre projetos; sem Docker; sem dependência nova; sem mexer em sealed/; sem edição automática do catalog.toml; sem credit assignment; sem repos de terceiro (D7).
