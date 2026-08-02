# SPEC — Camada de avaliação por juízes (harness-core)

**Status:** proposta v1, 2026-08-01, aprovada por revisão. Régua versionada: `judges/RUBRIC-J1.md`.

## 1. Decisão do circuito fechado — derivar de OSS real, commit anterior ao repo

Escolha: **(a) OSS real com bug/issue conhecida**. Rejeitados: (b) geração selada por juiz-opus — mesmo modelo, mesma distribuição, o gabarito continua nosso, o hash só prova *quando* escrevemos, não *quem*; (c) crédito held-out do `benchmarks/sealed/` — mede generalização dentro das nossas tasks, não código de terceiro. Só (a) satisfaz o critério de "saiu do lugar" do STATUS.md.

Mecanismo por projeto-juiz: pin de `upstream_url` + `base_sha` **anterior a 2026-07-31** (primeiro commit do harness-core) + `fix_sha` da correção do upstream. O verify é o **teste que o mantenedor escreveu no fix_sha**, copiado para `judges/_sealed/<judge_id>/` (fora do workspace, sha256 registrado em `judges/registry.tsv`) e executado só depois do agente parar. Provas de terceiro: URL+sha auditáveis, gabarito de autoria alheia, fixture entregue vermelha por construção. FASE 1 exige licença permissiva, sem `.env`, e grep de segredos no `setup.sh` (nunca repos do Renan).

## 2. Ancoragem contra ruído de LLM — 60 determinístico / 40 persona

Determinístico: roda em cópia limpa, sem LLM, mesmo pipeline do `run_task.py` (tamper check já existe). Persona: `claude-opus-5`, **lê o trace depois de ver o resultado da execução** (protocolo RUBRIC-v2), nota por critério com **citação obrigatória** (`arquivo:linha` do diff ou `trace.jsonl:N`); critério sem citação = descartado (sai da média); citação que o log não sustenta = **veto, zera a ficha**. Persona nunca sobrepõe a parte determinística — discordância vai para `disputes[]` e vira revisão da régua, não nota.

## 3. Critérios — lista ÚNICA para os 3 juízes (v1, soma 100)

| # | Critério | Peso | Fonte | Fase |
|---|---|---|---|---|
| D1 | Defeito real reparado (teste do upstream verde) | 25 | determinístico | 1 |
| D2 | Sem trapaça (tamper, escrita fora do ws, segredo tocado) — **veto** | 15 | determinístico | 1 |
| D3 | Sem regressão colateral (suíte completa verde + tamanho do diff) | 10 | determinístico | 1 |
| D4 | Custo/turnos até o verde | 10 | determinístico (`cost_usd`,`turns`) | 1 |
| P1 | Qualidade do diff no idioma do domínio | 15 | persona + citação | 1 |
| P2 | Fidelidade do trace (alegação × log) | 10 | persona + citação | 1 |
| P3 | Recuperação de erro / autonomia quando quebrou | 10 | persona + citação | 2 |
| P4 | Adoção: o juiz rodaria isso no projeto real dele | 5 | persona + citação | 2 |

FASE 1 = D1–D4 + P1 + P2 (85 pts, normalizados para 100). Peso e redação vivem no `RUBRIC-J1.md`; mudança de peso ⇒ `J2`, e verdicts antigos guardam `rubric_version`.

## 4. Domínios e forma dos projetos-juiz (3 iniciais)

- **j_web** (site/frontend): repo JS/TS pequeno, ~2–5k LOC, teste de nó (vitest/jest) sem browser. Bug de render/estado com teste do upstream.
- **j_b2b** (plataforma B2B/CRM): repo Python FastAPI/Django-lite, ~3–8k LOC, pytest, bug de regra de negócio (cálculo, filtro, permissão).
- **j_hw** (hardware/firmware): repo C com testes host-side (Unity/CMocka), ~1–3k LOC, bug de parsing de protocolo ou aritmética de ponto fixo. Sem hardware no loop.

Cada um: 1 defeito, 1 comando de verify, ≤120s, ≤300MB clonado, `setup.sh` idempotente e offline após o primeiro clone.

## 5. Estrutura de diretórios

```
judges/RUBRIC-J1.md        # régua versionada
judges/registry.tsv        # judge_id upstream_url base_sha fix_sha sealed_sha256 rubric_version license
judges/_sealed/<judge_id>/ # testes do fix_sha (NUNCA no workspace)
judges/run_judge.py        # orquestra: setup -> run_task -> persona -> verdict
judges/persona.py          # 1 chamada opus-5, saída json_schema
judges/verdicts/<judge_id>/<harness_version>.json
benchmarks/judge/task_j_web/   # prompt.md, verify.py, setup.sh, fixtures/ (gitignored)
benchmarks/judge/task_j_b2b/
benchmarks/judge/task_j_hw/
```

`benchmarks/judge/task_*` cai direto no `discover()` de `run_task.py` — `python3 run_task.py --all --suite judge` roda sem tocar em código.

## 6. Fluxo de uma avaliação completa

1. `setup.sh` clona upstream em `fixtures/` no `base_sha`, remove `.git`, roda scan de segredo. Aborta se sha divergir do `registry.tsv`.
2. `run_task.py --all --suite judge --repeat 3` para versão A e B → linhas em `results.tsv` com `suite=judge`.
3. `verify.py` copia os testes de `judges/_sealed/<id>/` para o workspace **só na hora de verificar**, confere sha256, roda, e apaga. Diff, trace e saída de teste ficam em `verdicts/`.
4. `persona.py` recebe: resultado determinístico já calculado, diff, trace, saída dos testes; devolve P1–P4 com citação. Sem citação ⇒ critério descartado; citação inválida ⇒ veto.
5. `run_judge.py` grava `verdict.json` e imprime `judge_score` por juiz + mediana entre juízes + dispersão.

## 7. Schema de dados (sem quebrar o existente)

- **results.tsv:** zero mudança de HEADER. Apenas `suite=judge` novo valor.
- **verdict.json:** `{judge_id, harness_version, rubric_version, base_sha, sealed_sha256, deterministic:{D1..D4, veto, evidence}, persona:{P1..P4:{score, citation, quote}}, discarded[], veto_reason, judge_score, cost_usd, ts}`.
- **graph.py:** tabela aditiva `judgements` (`CREATE TABLE IF NOT EXISTS`) com `(id, ts, judge_id, harness_version, rubric_version, judge_score, deterministic_json, persona_json, veto)`. Nenhuma migração.
- **Gate:** `evolve.py` ganha `judge_ok(rep)` espelhando `credit_ok` — **piso, não ganho**: gates da suite judge menos o de ganho, mais `judge_score_B >= judge_score_A - 5`. Não bloqueia o A/B; bloqueia **promoção**. Decisão registrada em `evolution/decisions/` numa seção `## Juízes`.

## 8. Custo e cadência

Rodada = 3 juízes × 2 versões × 3 repeats = 18 runs determinísticas (haiku, ~$0.30–0.60/run) + 6 fichas persona opus (~$0.38 cada) ≈ **$9–14 por rodada**.
Cadência: NÃO a cada A/B. Roda em (i) toda promoção de versão, depois do sealed aprovar; (ii) toda mudança de RUBRIC-J* (re-julgar as 2 últimas versões para calibrar); (iii) semanal, se houver merge. Baseline de cada juiz cacheado (N≥3).

## 9. FASE 1 vs FASE 2

**FASE 1 (mínimo para a primeira avaliação real):** 1 juiz só — `j_b2b` + `registry.tsv` + `_sealed/` + `RUBRIC-J1.md` (D1–D4/P1/P2) + `run_judge.py` + `verdict.json` + linhas `suite=judge` no results.tsv. Gate ainda **manual** (Fable lê o verdict e decide). Isso já produz a linha "saiu do lugar".

**FASE 2:** j_web + j_hw; P3/P4; tabela `judgements`; `judge_ok` automático; mediana/dispersão entre juízes; relatório de defeitos do próprio julgamento; rotação de projeto-juiz saturado (todos passam ⇒ sonda parou de discriminar).
