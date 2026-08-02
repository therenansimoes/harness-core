# STATUS — fonte de verdade do harness-core

**Atualizado:** 2026-08-01. Este arquivo substitui PLAN.md, FAST_START.md e generative-project.md como norte. Eles ficam como referência histórica — não seguir mais.

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

- Verdicts em `judges/verdicts/<juiz>/<versão>.json` são SOBRESCRITOS a cada re-run da mesma versão — viola o "nunca reescreva verdict" da régua; o histórico sobrevive só no graph (`judgements`, dedupe por ts). Fix: nome com timestamp. (2026-08-02)
- j_b2b tem ~75% de sucesso por run (variância real do haiku em 21+ turns) — avaliação de juiz precisa de repeats N≥3 com mediana, como a SPEC-J1 §8 já previa; run única gera spread falso. (2026-08-02)

- Só testado com `claude-haiku-4-5`; backend `api` nunca exercitado.
- 2 runs com `cli_exit_1` silencioso no results.tsv (2026-08-01 15:57/58) — causa não investigada.
- Gate do evolve não tem tamper check nem safety allowlist (mecanismos existem provados na gen3).

## Próximos passos (ordem)

1. ~~Port gen3 → core~~ **FEITO** (commit 10c64d2): safety allowlist, tamper check, gate em JSONL, testes honestos (34 passed reais).
2. **Camada de juízes — FASE 1** (spec aprovada em `judges/SPEC-J1.md`): projetos-juiz derivados de OSS real (decisão do Renan: repo novo, nunca os dele — têm segredos), 1 juiz `j_b2b` primeiro, nota 60/40 determinístico/persona, gate manual. Em execução: pesquisa do upstream OSS validado (teste do mantenedor vermelho no base).
3. FASE 2 dos juízes (j_web, j_hw, gate automático) + segundo modelo/backend no A/B.

## Pesquisa (evidência colhida, não opinião)

- **Open source vs. do zero (respondido 2026-08-01):** manter o core do zero — nenhum framework maduro (OpenHands/SWE-agent/Aider) faz meta-loop de auto-melhoria com gate determinístico próprio. Reusar: tasks do Terminal-Bench 2.0/Harbor como fonte de benchmark. Roubar padrão: gate held-in/held-out do Self-Harness (arXiv 2606.09498) e archive de variantes do Darwin Gödel Machine (jennyzzt/dgm).

- **Multi-projeto / memória / modularidade (respondido 2026-08-01):** tudo viável em stdlib, nada de dependência nova. Ordem de adoção decidida:
  1. Hierarquia de memória em markdown (global → projeto; episódico = `results.tsv`/runs, semântico = STATUS/CLAUDE.md) — pode já.
  2. Nota por módulo = coluna `module` em `proposals`/`runs` do `graph.py` (extensão de schema, não camada) — só APÓS provar valor em código de terceiro. Credit assignment por módulo é pesquisa aberta; manter "muda 1 coisa por A/B".
  3. Flags on/off por projeto (dict em config.py) — só quando houver 2+ módulos com histórico isolado.
  4. Isolamento multi-projeto (padrão event-sourcing do OpenHands V1 / sessões independentes do Agent SDK) — só quando existir 2º projeto real.
  5. Mem0/Letta/vector DB — ignorar; reavaliar só se markdown+SQLite não escalar.

## Método generativo v2 (princípios do Renan, 2026-08-01)

A arena volta como método de construção QUANDO a camada de juízes provar que mede honesto (a v1 morreu por medição quebrada, não pelo conceito). Regras do método:

- **Briefing mínimo:** goal + base de código a menor possível + regras do que pode/não pode + bullets curtos de dicas. Detalhe é ruído; pedido não é mecanismo.
- **Recursos máximos, tempo mínimo:** candidatos recebem todas as tools, time (subagentes) e recursos — mas o TEMPO é o gargalo, imposto por mecanismo (SIGTERM), nunca por instrução.
- **Juiz avalia escopo por escopo, resultado E processo:** cada juiz usa o harness candidato pra construir um projeto fake no domínio dele, e pontua (a) o resultado entregue e (b) o processo de desenvolvimento com aquele harness (fricção, autonomia, recuperação de erro). Mesma lista de critérios pra todos.
- **Alvo do artefato:** harness super, auto-melhorável, autônomo, auto-organizável — capaz de organizar, planejar, melhorar continuamente e desenvolver projetos de forma autônoma.

Sequência que protege o investimento: (1) juiz j_b2b rodando honesto ← estamos aqui; (2) rubrica ganha a dimensão "processo" (P3/P4 + uso end-to-end); (3) só então reabrir gerações de candidatos sobre a base atual.

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
