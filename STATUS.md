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

## Radar de tecnologia (nunca parar de estudar — veredito registrado antes de adotar)

- **deer-flow (ByteDance, MIT, 78k stars, ativo, 2026-08-01):** SuperAgent harness sobre LangGraph. ROUBAR padrão: sandbox isolado por run (versão subprocess simples, não Docker/K8s); SkillScan (scan determinístico antes de carregar capacidade); memória com escopo explícito (fazer com SQLite/markdown, depois do "saiu do lugar"). IGNORAR: LangGraph como runtime, multi-worker Redis, gateway de canais — contraria stdlib-only e a escada.

## Critério de "saiu do lugar"

Uma linha no `results.tsv` onde o harness melhorou código que ele não escreveu, com verify passando em cópia limpa. Até lá, nada de camada nova.
