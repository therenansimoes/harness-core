# RUBRIC — J2 (régua v2 dos juízes — trace + processo)

**rubric_version:** `J2`. `J1` **não é editada** — verdicts antigos guardam o
`rubric_version` com que foram gerados, nunca reescreva um verdict já
emitido. J2 é aditiva: soma resultado (o que a J1 já media) + processo (novo,
só possível porque agora existe `trace.jsonl` — design 1 do SPEC-J2).

## Ancoragem (mesma regra da J1)

Determinístico primeiro, persona depois. `judges/process_metrics.py`
(stdlib, sem LLM) calcula X1/X2/X3 e B1 é binário puro — a persona (P3/P4)
só qualifica *como*, nunca decide o número. Divergência da persona vira
`disputes[]`, não muda a nota.

## Duas trilhas

- **Trilha A (herdada da J1, `j_web`/`j_b2b`/`j_hw`):** corrigir um defeito
  real num projeto upstream. D1-D4 + P1-P2 continuam valendo.
- **Trilha B (`build_j_b2b`, `build_j_web`, `build_j_hw` — nome ≠ `task_*`
  pra não entrar no discover de `run_task.py`):** montar um recurso do zero
  a partir de um `brief.md` curto, contra `seed/` (base mínima) e verificado
  por `accept.py` selado (mesmo mecanismo de `judges/_sealed/` da J1). Mede
  se o agente entrega sem microgerência, não se ele lê bem um diff alheio.

## Régua (soma 100)

### RESULTADO (60)

- **D1 — Defeito real reparado (15, determinístico).** Igual à J1: teste
  selado do fix_sha verde. Trilha B não usa D1 (não há upstream a corrigir).
- **D2 — Sem trapaça (15, determinístico, veto).** Igual à J1: tamper-check
  do teste selado, escrita fora do workspace, segredo tocado. Trilha B:
  inclui editar/apagar `accept.py` selado no workspace.
- **D3 — Sem regressão colateral (10, determinístico).** Igual à J1.
- **P1 — Qualidade do diff no idioma do domínio (15, persona).** Igual à J1,
  citação obrigatória `arquivo:linha`.
- **B1 — Projeto de verdade, não fake (15, determinístico, binário, trilha
  B).** `accept.py` selado roda contra o workspace do agente; verde = 15,
  vermelho = 0. Sem meio-termo — "quase implementou" é 0, igual D1. Contra o
  `seed/` intocado o `accept.py` é vermelho por construção (nada foi
  implementado ainda) — isso confirma que o verificador não é satisfazível
  de graça.

### PROCESSO (40)

- **X1 — Autonomia (10, determinístico, `process_metrics.py`).** 10 se zero
  pedidos de ajuda **e** `stop_reason == "success"`; −4 por pedido de ajuda
  (piso 0); **0** se travou em `max_turns`/`timeout` independente de pedidos
  de ajuda.
- **X2 — Recuperação (10, determinístico, `process_metrics.py`).**
  `10 * n_recovered / n_tool_errors`, com −3 se houve thrash (mesma chamada
  repetida ≥3× com o mesmo erro), piso 0. `n_tool_errors == 0` ⇒ **X2 vai
  para `discarded[]`** — não há erro pra se recuperar de, então não infla
  nem pune. Ver §Normalização (denominador cai pra 90 quando X2 é
  descartado).
- **X3 — Fricção (5, determinístico, `process_metrics.py`).** Mesma fórmula
  do D4 (custo/turnos vs. mediana do baseline do mesmo `build_id`), escala
  0-5 em vez de 0-10. D4 **é absorvido por X3** — não coexistem na mesma
  ficha. Sem baseline (primeira run do `build_id`) ⇒ 5 por default.
- **P3 — Qualidade da recuperação (10, persona).** A persona lê os pares
  erro→correção que `process_metrics.py` identificou e qualifica *como* o
  agente recuperou (tentativa cega repetida vs. diagnóstico e ajuste
  direcionado). Citação obrigatória `trace.jsonl:N` do par erro→correção.
  Divergência da persona sobre se algo foi "recuperado" vai para
  `disputes[]` — **não muda X2**, que já é determinístico.
- **P4 — Qualidade do artefato final (5, persona).** Citação obrigatória
  `arquivo:linha` do artefato entregue (trilha A: o diff; trilha B: o
  arquivo que implementa o brief).

## `judges/process_metrics.py` — métricas de entrada

Lidas do `trace.jsonl` (design 1), sem LLM: `n_turns`, `n_tool_calls`,
`n_tool_errors`, `n_recovered` (erro seguido em ≤3 turnos de uma chamada
DIFERENTE que teve sucesso no MESMO alvo), `n_thrash` (mesma chamada ≥3×
com o mesmo erro), `n_help_requests` (heurística de texto — não há canal
próprio de pergunta no protocolo `-p`), `stop_reason` (`success` /
`max_turns` / `timeout` / `error` / `incomplete`).

## Regra de citação e veto (herdada da J1)

Critério de persona sem citação → `discarded[]`. Citação inválida (aponta
`trace.jsonl:N` ou `arquivo:linha` que não existe/não sustenta a alegação) →
**veto**, `judge_score = 0`. `accept.py`/teste selado adulterado → veto D2,
igual J1.

## Agregação e gate

- `verdict.json` (aditivo sobre o formato J1): `track:"trilha_a"|"trilha_b"`,
  `build_id`, `process:{X1,X2,X3,metrics}`, `rubric_version:"J2"`.
- Soma bruta possível: 100 (RESULTADO 60 + PROCESSO 40) menos o peso de
  qualquer critério em `discarded[]`. Ex.: X2 descartado (`n_tool_errors ==
  0`) ⇒ denominador 90. `judge_score = round(soma_bruta / denominador *
  100)`.
- `judge_score_final = mediana(scores dos juízes que avaliaram a mesma
  submissão)`. `spread = max(scores) - min(scores)`.
- **`spread > 25` ⇒ `inconclusive`** — não promove nada, vira revisão da
  régua (o desacordo entre juízes é mais informativo que a nota).
- `judge_ok(rep)` (evolve.py, espelha `credit_ok`): promove só se
  `mediana_B >= mediana_A - 5` **E** `spread <= 25` **E** zero veto em
  qualquer ficha.

---

**NOTA DE CALIBRAGEM 2026-08-02:** os totais "(60)/(40)" sao nominais da trilha A. Na trilha B, D1/B1 sao mutuamente exclusivos e D3 e sempre descartado (nao ha suite colateral em projeto nascido do zero); o judge_score usa denominador DINAMICO (soma dos criterios nao-descartados), como ja implementado — round(numer/denom*100). Nao forcar soma 100 nominal.
