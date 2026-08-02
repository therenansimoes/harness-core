# FEEDBACK-GEN4 — dados brutos do post-mortem (todos reprovados no gate)

Colheita mecânica. Custo = phase1.json + result.json (fases separadas; result.json é a fase 2).

| Cand | Rank atacado | Alvo do diagnóstico | Custo US$ (p1+p2) | Turnos (p1+p2) | subtype p1 / p2 | Gate pytest | Arquivos tocados (top 5) |
|---|---|---|---|---|---|---|---|
| v1 | #1 | Gate de verde flaky/não-hermético (`run_ui_suite` devolve `ran=True, passed=0, total=0`) | 1.8918 (0.7595+1.1323) | 25 (12+13) | success / **error_during_execution** | 1 failed, 141 passed | `delivery.py` (28 l.), `run.sh` (novo) |
| v2 | #2 | Juízo de run única: sem repetição intra-juiz, decisão sobre ruído bimodal | 2.0547 (0.7898+1.2649) | 31 (14+17) | success / success | 2 failed, 150 passed | `judges/run_judge.py` (93 l.), `tests/test_judge_repeats.py` (novo), `run.sh` (novo), verdicts j_b2b/j_hw/j_web + `history/` |
| v3 | #3 | Taxonomia de desfecho `ok`/`task_failed`/`infra_error` em `results.tsv`, agregador ignora infra | 2.4013 (0.7127+1.6886) | 37 (11+26) | **error_during_execution** / success | 1 failed, 147 passed | `score.py` (45 l.), `tests/test_outcome_taxonomy.py` (novo), `run.sh` (novo) |
| v4 | #4 | `task_fail` vs `infra_fail` na origem; gate do A/B não credita/desconta run de infra | 2.3986 (0.7719+1.6267) | 41 (15+26) | **error_during_execution** / **error_during_execution** | 4 failed, 143 passed | `score.py` (55 l.), `tests/test_infra_vs_task_failure.py` (novo), `run.sh` (novo) |

Testes que falharam no gate:

- v1: `tests/test_tb_tasks.py::test_tb_task_green_com_solucao_referencia[task_tb_cancel_async]`
- v2: `test_ui_gate.py::test_review_subjective_forca_humano`, `::test_baseline_atualizada_volta_a_passar`
- v3: `tests/test_tb_tasks.py::test_tb_task_green_com_solucao_referencia[task_tb_cancel_async]`
- v4: os dois acima somados — `test_tb_..._cancel_async` + `test_tudo_verde_fecha_sem_humano` + `test_review_subjective_forca_humano` + `test_baseline_atualizada_volta_a_passar`

## Ganho previsto (hipótese) e o que a própria NOTES concluiu

- **v1** — X: hermetizar gate + tratar suíte com 0 testes coletados como FALHA. A→B: verde em 2/4 execuções → 5/5. **NOTES não tem seção de fase 2** (termina em "Estado ao fim da fase 1"); `result.json` = `error_during_execution`. Sem veredito de hipótese. `.gate_run.log`: run 1 VERMELHO, run 2 VERMELHO.
- **v2** — X: `--repeats N` + mediana intra-juiz + `spread_intra`. A→B: flip de decisão ≥30% (N=1) → 0% (N=3). Veredito próprio: **parcialmente falsificada** — base media 41% de flip; N=3 dá 30%, N=5 dá 22%; só `unstable` (spread_intra > 25, abstenção) leva a 5.9% mantendo 43% conclusivas.
- **v3** — X: taxonomia de desfecho + agregador descarta `infra_error`. A→B: decisão errada sobre lote contaminado ~100% → 0%. Veredito próprio: trava **preventiva** — reclassificando as linhas históricas, os 3 `infra_error` **não mudariam nenhuma decisão** registrada. Ganho não demonstrado no dado real. Registra `test_ui_gate.py` como flaky pré-existente, não tocado.
- **v4** — X: classificar `task_fail`/`infra_fail` na origem e excluir infra do A/B. A→B: decisão do gate sensível a infra → insensível; causa registrada 0% → 100%. Veredito próprio: **confirmada** (success cru 100%→71%→45% com contaminação; `rate_task` fica 100% nos três, decisão idêntica ao par limpo). Mas `.gate_run.log` fecha com `RESULTADO: VERMELHO (base=1 demo=0 guarda=0)`.

## Os quatro "diagnóstico #1" lado a lado

| Cand | #1 declarado |
|---|---|
| v1 | "O gate de verde é flaky, e ele é o sinal que TODOS os outros mecanismos consomem." Base intermitente: 4 execuções → 2 verdes, 2 vermelhas; `run_ui_suite` retorna `ran=True, passed=0, total=0`. |
| v2 | "Vacuous pass: gate que não roda nada conta como verde." `delivery.run_ui_suite` devolve `passed=0, total=0, ran=False` e o consumidor lê "nenhuma falha" como "tudo certo". Falha de **classe**, não bug de UI. |
| v3 | "A medição do próprio gate não é hermética." Predicado "suite verde ⇒ mutação aceita" depende de ordem de execução; contamina `evolve.py`, `results.tsv`, `evolution/decisions/`. |
| v4 | "O loop de auto-melhoria não tem gerador, só juiz." `evolve.py:56 parse_proposal()` lê proposta markdown escrita à mão; o resto é gate. O "auto" hoje é humano. |

**Convergiram ou divergiram?** Convergência forte de 3 em 4: v1, v2 e v3 apontam o **mesmo** #1 por caminhos independentes — o gate de verde não é hermético e degrada para silêncio/ruído em vez de vermelho, envenenando tudo a jusante. v2 nomeia o mecanismo com mais precisão (`ran=False` ⇒ ninguém reprova, padrão replicável em qualquer verificador opcional); v3 generaliza para o predicado do loop; v1 traz a única medição repetida (2/4 verdes).

v4 é o **outlier**: descarta esse eixo e aponta a ausência de gerador de mutações. Notar que v4 declara ter lido `STATUS.md` e removido do rank duas dívidas que julgou já fechadas no código recebido — ou seja, partiu de um mapa diferente dos outros três.

Convergência secundária: **N=1 sobre j_b2b bimodal 47–81** aparece como #2 em v1, v2 e v3 e como #2 em v4 (embutido em "largura de benchmark insuficiente"). Os quatro citam a mesma evidência do STATUS.

## Fora da spec, encontrado no caminho (reportado, não resolvido)

1. Não existe `phase2.json` em nenhum candidato; a fase 2 está em `result.json`. Assumido isso ao somar.
2. `error_during_execution` em 5 das 8 fases (v1-p2, v3-p1, v4-p1, v4-p2). O custo/turnos dessas fases foi somado assim mesmo — pode não representar trabalho concluído.
3. `tests/test_tb_tasks.py::test_tb_task_green_com_solucao_referencia[task_tb_cancel_async]` falha em v1, v3 e v4 — e **não** aparece nas falhas de base que v1 mediu no seed (lá eram testes de `test_ui_gate.py`). Não determinei se é regressão dos candidatos ou mais flakiness.
4. v4 descreve mudanças em `agent.py` e `evolve.py` na NOTES, mas o `diff -rq` contra `../seed` só acusa `score.py` + o teste novo. Divergência entre relato e diff, não investigada.
5. Todos os 4 criaram `run.sh` (inexistente no seed) e todos gravaram lixo de execução no workspace (`.pytest_cache`, `.ui_test_tmp/`, `test-results/`), o que polui qualquer diff futuro.
