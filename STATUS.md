# STATUS — fonte de verdade do harness-core

**Atualizado:** 2026-08-02 (noite — escada D0–D6 completa; desvio DeerFlow encerrado). Este arquivo substitui PLAN.md, FAST_START.md e generative-project.md como norte. Eles ficam como referência histórica — não seguir mais.

## Desvio DeerFlow — ENCERRADO (2026-08-02 noite)

Passamos parte do dia avaliando construir SOBRE o DeerFlow 2.0 (bytedance/deer-flow). Renan testou a UI e vetou: fraca, plataforma imatura apesar de 78.9k stars. **Decisão: DeerFlow NÃO é a base do nosso produto.** O que fica: (a) 2 PRs já abertos por visibilidade (deer-flow#4644 skillscan tests, #4645 fix teste config) — deixar rolar, custo zero; (b) 3 entregas prontas no GELO, não publicar sem querer holofote: fork llm-space `feat/custom-eval-methods` (eval Wilson/KPI, 569 testes), canal Baileys `deer-flow-wa/feat/whatsapp-baileys-channel`, form Add-MCP `deer-flow-mcp/feat/add-mcp-server-ui`. Clones em ~/projects/{deer-flow,llm-space} + worktrees deer-flow-wa/deer-flow-mcp — podem ser removidos. Lição: star count ≠ maturidade; nosso diferencial (meta-loop) NUNCA precisou deles. Detalhes em evolution/PR-CANDIDATES.md. **Voltar ao plano principal: meta-loop no nosso harness + site da fazenda.**

## Visão (destino, confirmada pelo Renan em 2026-08-01)

O harness mais incrível que existe: desenvolve coisas incríveis com pouca interação humana, gere múltiplos projetos em paralelo, de forma autônoma, e se auto-melhora com prova. Método em aberto (reusar open source vs. do zero) — decidir com evidência, não opinião. A visão dita o DESTINO; o trabalho de hoje segue a escada abaixo, um degrau por vez.

## Decisão de 2026-08-01 (definitiva)

O projeto patinou por meta-recursão: arena (agentes construindo harnesses concorrentes) + satélites (WhatsApp, delivery, UI gate) construídos no mesmo dia em que o plano mandava "core antes de satélite". Correção:

1. **Arena encerrada como método.** Preservada em `arena/` (commitada) como histórico e fonte de mecanismos provados. Não rodar novas gerações.
2. **Um harness só: o da raiz** (`agent.py`, `run_task.py`, `score.py`, `evolve.py`, `graph.py`) — é o único com histórico real (`results.tsv`, `evolution/decisions/`).
3. **Satélites CONGELADOS** até o core provar valor em código de terceiro: `whatsapp.py`, `channel/`, `delivery.py`, `assist.py`, UI gate Playwright. Não evoluir, não corrigir, não documentar.
4. **Desenvolvimento direto:** Fable planeja/orquestra, subagentes executam. Sem gerações competindo.

## O que funciona (verificado por execução)

- Loop evolve: 2 decisões reais — `evolution/decisions/v0.1.md` (DISCARD) e `v0.2.md` (KEEP, −17.8% custo).
- 3 tasks fixed + 2 sealed rodando com haiku; `results.tsv` como fonte de verdade.

## Dívidas conhecidas

- Dívidas da era dos juízes (verdicts sobrescritos, variância j_b2b) arquivadas com o attic/ — não se aplicam ao método atual.

- Só testado com `claude-haiku-4-5`; backend `api` nunca exercitado.
- 2 runs com `cli_exit_1` silencioso no results.tsv (2026-08-01 15:57/58) — causa não investigada.
- ~~Gate do evolve não tem tamper check nem safety allowlist~~ RESOLVIDO: tamper check (genome fingerprint + sandbox_tamper) e genome.toml no D5/fix-sandbox.
- Confinamento assimétrico no ciclo self do autopilot: `evolution/decisions.jsonl` é escrito na raiz REAL mesmo com `HARNESS_PROJECTS_ROOT` apontando pra demo (o `.md` da decisão vai pro root temporário). Gitignored, sem efeito no git, mas o log de máquina mistura sessões demo e reais. (2026-08-02)

## Escada de construção (spec do architect, 2026-08-02)

| # | Degrau | Aceite verificável | Executor |
|---|---|---|---|
| D0 | ~~Descartar gen5~~ FEITO (revert e1a2bb6, 174 testes verdes) | — | mechanic |
| D1 | ~~Congelar `judges/` e `arena/` em `attic/`; extrair verify do `task_j_b2b` → `benchmarks/held_in/task_oss_b2b/`; métricas X1/X2/X3 → `metrics/process.py`; `evolve.py` sem import de judges~~ FEITO (04d8090 + da8a0a8) | `rg -l "judges/" *.py` vazio; suite held_in grava em results.tsv | mechanic |
| D2 | ~~Régua: `score.py` com Wilson, `MIN_N=6`, KEEP/DISCARD/INCONCLUSIVE; `experiment.py` consome~~ FEITO (merge 5832e46, 3 casos de aceite batem) | 5/6 vs 4/6 → INCONCLUSIVE; 6/6 vs 1/6 → KEEP; 3/3 vs 0/3 → INCONCLUSIVE | builder |
| D3 | ~~`profile.py` (self-adaptive): detecta stack/comandos determinístico, lê CLAUDE.md do alvo, escreve `.harness/profile.toml`, injeta no prompt. Antes: researcher levanta prior art (aider repo-map, mise/devbox etc.)~~ FEITO (merge 9ad4554, detecta e executa test_cmd) | roda contra 2 repos; comando de teste detectado executa e sai 0 | builder |
| D4 | ~~KPI: `.harness/kpi.toml` + coleta pós-run → colunas `kpi_*` em results.tsv e graph~~ FEITO (D4a merge 99ef96f coleta; D4b merge cc29726 kpi_report + gate kpi_regression) | 3 runs demo com kpi_* preenchido; `score.py --ab` compara por KPI | builder |
| D5 | ~~Genoma 360: `evolution/genome.toml` (mutáveis: agent.py, prompts/, profile.py; blocklist: evolve.py, score.py, safety.py, sealed/)~~ FEITO (merge cdefb26, tamper:genome_violation) | proposta em score.py falha `tamper:genome_violation`; em prompts/ passa | builder |
| D6 | ~~`autopilot`: fila → run → KPI → revert se regride; propostas de catálogo determinístico do erro dominante~~ FEITO (merge c7d1911, aceite mock 20min: 7 runs fila + 2 self, zero escrita fora do ROOT) | 20min sem intervenção, ≥5 linhas em results.tsv, zero escrita fora do workspace | builder |
| D7 | Prova em código de terceiro: +3 tasks held-out de repos OSS distintos | 3 verifies red no base, green pós-run em cópia limpa | builder |

Pós-escada (2026-08-02): 1º projeto real registrado — `projects/website-faz-rogers` (site da fazenda, Astro 5, 13 tasks na fila, verify próprio + 5 KPIs). Pendências antes da 1ª rodada real: (a) sandbox do evolve não copia o fecho de runtime (safety.py etc.) — toda proposta morre em InfraError, fix em decisão no architect; (b) copytree do project.py sem ignore copia node_modules por task — idem. D7 (3 tasks held-out de OSS) continua aberto.

Riscos vigiados: KPI gameável (KPI calculado fora do genoma + held-out selado), genoma 360 se auto-quebrar (blocklist + worktree + revert por regressão), autonomia sem teto (budget $ e wall-clock por SIGTERM no config). NÃO construir: satélites, Docker, vector DB/GraphRAG/Mem0, credit assignment por módulo, multi-projeto real (só com 2º projeto), qualquer volta de arena.

## Pesquisa (evidência colhida, não opinião)

- **Open source vs. do zero (respondido 2026-08-01):** manter o core do zero — nenhum framework maduro (OpenHands/SWE-agent/Aider) faz meta-loop de auto-melhoria com gate determinístico próprio. Reusar: tasks do Terminal-Bench 2.0/Harbor como fonte de benchmark. Roubar padrão: gate held-in/held-out do Self-Harness (arXiv 2606.09498) e archive de variantes do Darwin Gödel Machine (jennyzzt/dgm).

- **Multi-projeto / memória / modularidade (respondido 2026-08-01):** tudo viável em stdlib, nada de dependência nova. Ordem de adoção decidida:
  1. Hierarquia de memória em markdown (global → projeto; episódico = `results.tsv`/runs, semântico = STATUS/CLAUDE.md) — pode já.
  2. Nota por módulo = coluna `module` em `proposals`/`runs` do `graph.py` (extensão de schema, não camada) — só APÓS provar valor em código de terceiro. Credit assignment por módulo é pesquisa aberta; manter "muda 1 coisa por A/B".
  3. Flags on/off por projeto (dict em config.py) — só quando houver 2+ módulos com histórico isolado.
  4. Isolamento multi-projeto (padrão event-sourcing do OpenHands V1 / sessões independentes do Agent SDK) — só quando existir 2º projeto real.
  5. Mem0/Letta/vector DB — ignorar; reavaliar só se markdown+SQLite não escalar.

## Decisão de 2026-08-02 (definitiva — substitui o "Método generativo v2")

Desenvolvimento por gerações + juízes está ABANDONADO como método de construção do harness. A arena não volta. O método geracional fica reservado para otimizar sistemas PRONTOS que já têm KPIs — os KPIs são o feedback natural de testes A/B/multivariáveis. Gen5 parcial descartado (revert e1a2bb6). O harness se constrói por desenvolvimento direto, com três eixos:

- **Autonomia:** desenvolver projetos com pouca interação humana.
- **Self-improvement 360°:** melhorar qualquer parte de si (código, prompts, memória, processo) com prova/gate — não só config.
- **Self-adaptive:** detectar stack, convenções e KPIs do projeto-alvo e ajustar comportamento por projeto.

### Loop de self-improvement sem juízes

Fitness = verificadores determinísticos com barreira estatística, em três anéis: (1) held-in (`benchmarks/held_in/`) para propor; (2) held-out selado (`benchmarks/sealed/`) só para creditar; (3) KPIs do projeto-alvo (`.harness/kpi.toml`, nome → comando → número) — regressão de KPI reverte a mutação. A régua substitui o juiz: `MIN_N=6`, decisão por intervalo de Wilson não-sobreposto, saída ternária KEEP/DISCARD/INCONCLUSIVE (inconclusive não promove). Persona LLM só como comentário em draft, nunca como número que promove. Quem julga não se muda: `evolve.py`, `score.py`, `safety.py` e `benchmarks/sealed/` fora do genoma, sempre.

## Doutrina de rodada rápida (Renan, 2026-08-02 — "com metodologia certa, grande parte da verificação vira um script")

Gates escalonados, do mais barato pro mais caro — reprovou, morre ali, $0 adiante:
1. **Roda?** (script, segundos): não roda/não compila/suite vermelha = descartado. Sem julgamento, sem piedade.
2. **Milestone técnico** (script, ≤2min): critérios básicos objetivos (milestone_gate.sh da geração; verify da task).
3. **Determinístico do juiz** (script, ≤5min): D*/B*/X* — só pra quem passou 1-2.
4. **Persona** (LLM, pago): só sobreviventes do 3.
5. **Fable decide** (promoção/descarte): lê draft, bate o martelo.

Timeboxes duros (mecanismo, não pedido): dev de candidato ≤12min (SIGTERM); experimento A/B em modo paralelo ≤5min de parede; rodada completa de geração (build+gate+julgamento) ≤30min. Fila esperando >1.5x o previsto = kill + takeover (regra kill-fast).

## Radar de tecnologia (nunca parar de estudar — veredito registrado antes de adotar)

- **deer-flow (ByteDance, MIT, 78k stars, ativo, 2026-08-01):** SuperAgent harness sobre LangGraph. ROUBAR padrão: sandbox isolado por run (versão subprocess simples, não Docker/K8s); SkillScan (scan determinístico antes de carregar capacidade); memória com escopo explícito (fazer com SQLite/markdown, depois do "saiu do lugar"). IGNORAR: LangGraph como runtime, multi-worker Redis, gateway de canais — contraria stdlib-only e a escada.

- **Terminal-Bench 2.0 import (2026-08-01): PARCIAL.** Maioria das 89 tasks exige Docker (ambiente embutido no Dockerfile). Pool portável pequeno (dígito único a baixa dezena); 1 task (`cancel-async-tasks`) adaptada e validada red/green no scratchpad, ~30-45min/task portável. Decisão: importar as portáveis quando precisarmos de largura de benchmark; suporte Docker fica na fila de longo prazo (junto do A/B sério).
- **Confiabilidade de juiz LLM (2026-08-01):** citação obrigatória + veto (já temos) é a mitigação nº1 da literatura. ADOTAR custo-zero: CoT estilo G-Eval antes do score na persona (mesma chamada). QUANDO ESCALAR: swap de posição 2× no pairwise de versões A/B (flip = disputa). IGNORAR: self-consistency N× (60% determinístico já blinda) e pairwise nas fichas J1 (perderia granularidade por critério; pointwise por critério está certo). Fontes: arXiv 2504.14716, 2606.13685, 2507.10535.

## Critério de "saiu do lugar"

Uma linha no `results.tsv` onde o harness melhorou código que ele não escreveu, com verify passando em cópia limpa.

**✅ ATINGIDO (2026-08-02, v0.4, commit 8e145d9):** `results.tsv` linha `v0.4 judge task_j_b2b success=1` — bug real do schwifty corrigido, 415 testes do upstream verdes, 0 regressões, verdict 58→**81**. Caminho: M0 (v0.2=58, D1=0) → v0.3 trace (KEEP, +0.9% custo) → v0.4 MAX_TURNS 12→30 (A/B: 0/6 vs 3/4). Defeito de régua registrado: D4 pune sucesso caro vs falha barata (X3 da J2 corrige). **M2 medido (2026-08-02):** j_web **93**, j_hw **98** (estáveis, citações válidas, zero veto), j_b2b **bimodal 47–81** (5/8 sucessos no dia; 2 tentativas de trapaça pegas pelo tamper check). Summary `inconclusive` por spread — correto: a sonda localizou a fraqueza real (consistência em tarefas ≥21 turnos). Próximo alvo de evolução: consistência do j_b2b (prompt do genoma / tier de modelo / decomposição — decidir por A/B). Próximo marco: M3 = repo publicável.

## Divisao de trabalho (Renan + evidencia, 2026-08-02)

**Nos (Fable orquestra + tiers) = motor de construcao** do harness — dev direto com A/B e juizes (aproveitamento ~100% hoje). **Geracional = sonda sobre a nossa base**, tres usos provados: diagnostico convergente (3/4 mesmo #1 na gen4), stress test da base, exploracao quando nao sabemos a resposta. Geracao nao constroi o produto; informa o motor. Colheita de geracao = diagnosticos e hipoteses medidas, nao codigo.
