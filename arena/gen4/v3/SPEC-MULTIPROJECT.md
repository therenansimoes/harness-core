# SPEC — Multi-projeto FASE 1 (aprovada 2026-08-02)

Design opus, revisão Fable. Doutrina: dois eixos que não se misturam — HARNESS (tasks/, benchmarks/, results.tsv, score.py) mede o motor; ENTREGA (projects/<nome>/) mede o produto. Multi-projeto vive 100% no eixo ENTREGA. stdlib-only, sem daemon.

## Layout

Control-plane in-repo em `projects/<nome>/`; data-plane (código real do projeto) fora do repo via `work_path`:

```
projects/<nome>/
  .harness/config.toml   [project] name, work_path=<abs, fora do repo>, priority=1..9, enabled
  spec/SPEC.md           formato do demo_site (frontmatter)
  queue.tsv              id  state  priority  created  claimed_at  prompt_file  verify  notes
  queue/<id>.md          prompt da unidade de trabalho
  verify/<id>.py         verificador da unidade (exit 0 = done). Obrigatório.
  regression/            MANIFEST.json + checks (invariantes que só crescem)
  results.tsv            resultados DESTE projeto (append-only)
  MEMORY.md              memória semântica do projeto
  runs/<run_id>/trace.jsonl
```

Compartilhado (raiz): genoma agent.py, judges/, graph.py, results.tsv do eixo HARNESS, safety.py. `projects/demo_site/` aproveitado como forma (spec/regression), `delivery.py` segue congelado.

## Isolamento

- Workspace efêmero por unidade: copia work_path → `.harness_ws/<project>_<id>_<hex>/`; agente roda com cwd=ws; resultado aplicado de volta SÓ se verify passar; senão descarta ws, preserva trace.
- results.tsv POR projeto (sem coluna nova no global — header é congelado).
- Lock por projeto: `os.open(.harness/lock, O_CREAT|O_EXCL)` com pid+ts; libera no finally; lock de pid morto é roubado. Fila editada sob o lock.
- Env por run: HARNESS_RUN_ID + HARNESS_TRACE_ROOT=projects/<n>/runs (já suportados pelo agent.py).

## Scheduler mínimo (sem daemon)

`pick_next() -> (project, queue_row) | None`: (1) projetos enabled, sem lock vivo, com pending; (2) ordena por (-priority, last_activity_ts) — round-robin ponderado starvation-free; (3) dentro do projeto (-priority, created); empate → nome. Agente pode enfileirar (`queue add`) ao terminar — auditado no graph.

## Interface

`project.py` novo (~250 linhas) + subcomandos em harness_cli.py:
```
harness project add <nome> --path <abs> [--priority N]
harness project list
harness project queue <nome> add "<título>" --prompt f.md --verify v.py
harness project run [--project N] [--once|--loop K] [--keep]
harness project status [<nome>]
```
`run` usa agent.run_agent direto (run_task.py é hard-coded no eixo HARNESS); copia (não importa) hash_test_files e formato de linha; reusa safety.py.

## Criar vs reusar vs roubar

REUSAR: agent.run_agent, safety.safe_run, graph.record_session/record_delivery_event, esquema spec/regression do demo_site. ROUBAR: sandbox-por-run (run_judge.run_real_build), 1 processo = 1 projeto = 1 lock (Agent SDK), event-sourcing = TSVs append-only como log, estado derivado por leitura (OpenHands V1). CRIAR: pick_next + lock (~60 linhas). NÃO AGORA: flags por projeto, coluna module no graph, vector DB.

## FASE 1 vs FASE 2

FASE 1 (hoje, 2 projetos): layout, lock, fila TSV, pick_next, 5 comandos, results/trace por projeto, tamper check, testes sem rede.
FASE 2: 2 processos simultâneos reais, promoção acceptance→regression, MEMORY.md automático, priorização por taxa de falha, gate de juiz sobre projetos reais.

## Critérios de aceite

1. `add p1 + add p2` → `list` mostra 2 projetos, 0 pendências.
2. 1 unidade em cada fila: dois `run --once` em paralelo terminam limpos; 1 linha em cada results.tsv; nenhum ws remanescente.
3. Dois `run --once --project p1` simultâneos: exatamente 1 executa; o outro reporta lock e sai 0 (ou pula pra p2).
4. `run --loop 4` com prioridades 3 e 1 → ordem ponderada verificável nos timestamps.
5. Agente edita verify/<id>.py → success=0, notes com `tamper:`.
6. `pytest tests/test_project.py` verde, sem rede/pagas (agente mockado).
7. work_path intacto quando verify falha.
