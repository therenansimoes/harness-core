# Medicao deterministica — geracao 2

## Violacao de escopo (escrita fora de arena/)
nenhuma. OK.

## v1
- custo_usd: 1.2456999500000001
- turnos: 29
- duracao_ms: 299228
- subtype: error_during_execution | is_error: True
- tokens_in: 48 | tokens_out: 28221
- wall_clock_s: 301
- exit_code: 124  (124 = morto pelo prazo)
- arquivos: 10 | linhas: 727
- NOTES.md: AUSENTE
- arvore:
```
INHERITED.md
fixture_broken/task.py
harness.py
repair.py
safety.py
self_improve.py
self_improve_log.jsonl
trace.jsonl
verify.py
workspace/task.py
workspace/test_task.py
```

## v2
- custo_usd: 1.2599996
- turnos: 33
- duracao_ms: 299189
- subtype: error_during_execution | is_error: True
- tokens_in: 54 | tokens_out: 25010
- wall_clock_s: 301
- exit_code: 124  (124 = morto pelo prazo)
- arquivos: 8 | linhas: 519
- NOTES.md: AUSENTE
- arvore:
```
INHERITED.md
evolve.py
harness.py
policy.py
safety.py
task_fixture/ops.py
task_fixture/test_ops.py
tracelib.py
verify.py
```

## v3
- result.json vazio ou ausente (morto pelo prazo antes de emitir JSON)
- wall_clock_s: 252
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 17 | linhas: 689
- NOTES.md: presente
- arvore:
```
.pytest_cache/.gitignore
.pytest_cache/CACHEDIR.TAG
.pytest_cache/README.md
.pytest_cache/v/cache/lastfailed
.pytest_cache/v/cache/nodeids
INHERITED.md
NOTES.md
config.json
evolve.py
evolve_log.jsonl
fixture/task/stats.py
fixture/task/test_stats.py
harness.py
policy.py
run.sh
safety.py
trace.jsonl
verify.py
```

## v4
- custo_usd: 1.29258425
- turnos: 33
- duracao_ms: 299255
- subtype: error_during_execution | is_error: True
- tokens_in: 62 | tokens_out: 25550
- wall_clock_s: 301
- exit_code: 124  (124 = morto pelo prazo)
- arquivos: 7 | linhas: 369
- NOTES.md: AUSENTE
- arvore:
```
INHERITED.md
external_project/strutils.py
external_project/test_strutils.py
harness.py
strategy.json
trace.jsonl
workspace/buggy_calc.py
workspace/test_calc.py
```

## v5
- custo_usd: 1.25105475
- turnos: 35
- duracao_ms: 299245
- subtype: error_during_execution | is_error: True
- tokens_in: 66 | tokens_out: 23968
- wall_clock_s: 301
- exit_code: 124  (124 = morto pelo prazo)
- arquivos: 11 | linhas: 469
- NOTES.md: AUSENTE
- arvore:
```
INHERITED.md
harness/agent.py
harness/fixture/pkg/__init__.py
harness/fixture/pkg/calc.py
harness/fixture/test_calc.py
harness/fixture_seed.py
harness/policy.json
harness/sandbox.sb.tmpl
harness/self_improve.py
harness/trace.jsonl
harness/trace.jsonl.baseline
harness/verifier.py
```

