# SPEC — FASE 2: trace turno-a-turno + dimensão "processo" (RUBRIC-J2)

**Status:** proposta aprovada 2026-08-01 (design opus, revisão Fable). Implementar APÓS o marco M0 (primeiro verdict funcional J1). **Design 1 é dependência dura do Design 2** — sem trace, "processo" vira opinião de LLM (o erro da arena v1).

Fatos que ancoram: `judges/run_judge.py:303` usa `notes` (≤160 chars) como "trace" — P2 julga contra algo que não é trace; P3 impossível. `evolve.py`: `GENOME = ["agent.py"]` — artefatos novos nascem de dentro do agent.py ou env var, nunca de módulo novo importado pelo genoma.

## DESIGN 1 — trace.jsonl por run

- `_run_cli`: trocar `--output-format json` por `--output-format stream-json --verbose` (1 evento JSON/linha; último evento `type=="result"` é o mesmo objeto de hoje). SEM `--include-partial-messages` (deltas = ruído).
- `_parse_stream(stdout) -> (result_obj, lines)`: ignora linha não-JSON, guarda a última `type=="result"`. Bloco usage/custo/turns/is_error idêntico ao atual. Sem `result`: cai nos ramos atuais (`cli_exit_N`/`bad_json`).
- `_write_trace(lines, run_id)`: grava `runs/<run_id>/trace.jsonl` na ordem original (nº da linha = chave de citação). `TRACE_ROOT = env HARNESS_TRACE_ROOT ou ROOT/"runs"`; `run_id = env HARNESS_RUN_ID ou workspace.name`.
- `AgentResult` ganha `trace_path: str = ""` e `trace_lines: int = 0` (defaults — não quebra nada).
- Knobs novos no genoma (elegíveis a A/B): `TRACE_MAX_LINES = 400`, `TRACE_MAX_FIELD = 2000`.
- **Truncamento determinístico:** por campo, string > TRACE_MAX_FIELD vira prefixo + `…[trunc N chars]`. Por arquivo: >400 linhas mantém 100 primeiras + 299 últimas + 1 linha `{"type":"harness_trunc","dropped":K}` na posição 101 (total sempre 400). Teto duro 2MB. O arquivo escrito é a verdade para citação.
- `run_task.py`: anexa token `trace:<path relativo>` nas notes (~40 chars, HEADER inalterado).
- `run_judge.py:303`: lê `runs/<run_id>/trace.jsonl` via token das notes (fallback = comportamento atual); passa à persona o trace renderizado `"{i}: {line}"` (persona só acerta N se enxergar N).
- `.gitignore`: `runs/`. GC: manter 50 run_ids mais recentes, apagar o resto no início de `run_task.main()`. NÃO apagar runs citados em verdict.
- **A/B obrigatório:** tratar como mutação — v0.2 vs v0.2+trace na suite fixed `--repeat 3` → `evolution/decisions/v0.3.md`. KEEP se success idêntico e cost/seconds medianos ±10%.
- Backend `api` sem trace nesta fase (dívida no STATUS); `trace_path==""` ⇒ P2/P3 em `discarded[]`, nunca 0.

Aceites: (1) stream sintético 5 linhas ⇒ tokens/custo/turns idênticos ao teste atual de max-turns; (2) stream sem `result` + rc 1 ⇒ `cli_exit_1`; (3) 900 eventos ⇒ exatamente 400 linhas, linha 101 `harness_trunc`, tudo parseável; (4) HEADER do results.tsv byte-a-byte igual; (5) `--dry-run` verde com `P2.citation` formato `trace.jsonl:N`, N ≤ linhas do arquivo.

## DESIGN 2 — RUBRIC-J2: duas trilhas, resultado + processo

Novo arquivo `judges/RUBRIC-J2.md`; **J1 não é editado**; verdicts guardam `rubric_version`.

**Régua J2 (soma 100):**
- RESULTADO (60): D1 15 · D2 veto 15 · D3 10 · P1 15 · **B1 15** ("projeto fake satisfaz o `accept.py` do briefing", determinístico binário).
- PROCESSO (40): **X1 autonomia 10** · **X2 recuperação 10** · **X3 fricção 5** · P3 10 · P4 5. D4 absorvido por X3.

**`judges/process_metrics.py` (CRIAR, stdlib, lê trace.jsonl):** `n_turns, n_tool_calls, n_tool_errors, n_recovered` (erro seguido em ≤3 turnos de chamada diferente ok no mesmo alvo), `n_thrash` (mesma chamada ≥3× mesmo erro), `n_help_requests`, `stop_reason`.
- X1 = 10 se zero pedidos de ajuda e `stop_reason=="success"`; −4 por pedido; 0 se max_turns/timeout.
- X2 = `10 * n_recovered / max(1, n_tool_errors)`, −3 se thrash; `n_tool_errors==0` ⇒ X2 em `discarded[]` (não inflar nem punir).
- X3 = 5 escalando turns+cost contra mediana do baseline do build_id (fórmula do D4 reusada).
- P3 persona qualifica *como* recuperou, citação `trace.jsonl:N` do par erro→correção; divergência de X2 vai pra `disputes[]`, não muda X2. P4 cita `arquivo:linha` do artefato.

**Trilha B — projeto fake (`benchmarks/judge/build_j_b2b/`, nome ≠ task_* pra não entrar no discover):**
- `brief.md` ≤25 linhas IMPOSTO POR TESTE: `## Goal` (1 parágrafo) + `## Regras` (pode/não pode) + `## Dicas` (≤5 bullets). Detalhe é ruído.
- `seed/` base mínima ≤200 LOC no domínio do juiz. `accept.py` selado em `judges/_sealed/build_j_b2b/` (sha256 no registry, injetado só na verificação — mecanismo J1).
- Tempo é mecanismo (HARNESS_TIMEOUT + SIGTERM), brief não menciona prazo.
- Domínios: j_b2b endpoint+regra de negócio · j_web componente com teste de nó · j_hw parser de protocolo em C.

**Agregação e gate:**
- verdict.json aditivo: `track:"build"`, `build_id`, `process:{X1,X2,X3,metrics}`, `rubric_version:"J2"`.
- `judge_score_final = mediana(juízes)`; `spread = max-min`; **spread > 25 ⇒ `inconclusive`**, não promove, vira revisão da régua.
- `judge_ok(rep)` no evolve.py (espelha credit_ok): promove só se `mediana_B >= mediana_A - 5` E `spread <= 25` E zero veto.
- graph.py: tabela aditiva `judgements` com `track` e `process_json`.

Aceites: (1) trace sintético 3 erros/2 recuperações ⇒ X2==4 sem LLM; (2) zero erros ⇒ X2 discarded e denominador 90; (3) brief >25 linhas falha `test_brief_minimo`; (4) accept.py editado no ws ⇒ veto D2, score 0; (5) `--track build --dry-run` ⇒ verdict J2 com 10 chaves de metrics; (6) scores 90 e 60 ⇒ `inconclusive`, `judge_ok()` False.
