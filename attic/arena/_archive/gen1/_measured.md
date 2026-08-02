# Medicao deterministica — geracao 1

## Violacao de escopo (escrita fora de arena/)
nenhuma. OK.

## v1
- custo_usd: 0.8409468
- turnos: 22
- duracao_ms: 141195
- subtype: success | is_error: False
- tokens_in: 44 | tokens_out: 11915
- wall_clock_s: 143
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 18 | linhas: 458
- NOTES.md: presente
- arvore:
```
NOTES.md
README.md
config.json
demo_task/.pytest_cache/.gitignore
demo_task/.pytest_cache/CACHEDIR.TAG
demo_task/.pytest_cache/README.md
demo_task/.pytest_cache/v/cache/lastfailed
demo_task/.pytest_cache/v/cache/nodeids
demo_task/buggy.py
demo_task/test_buggy.py
demo_task/trace.jsonl
evolve.py
evolve_log.jsonl
harness.py
policy.py
safety.py
trace.py
verify.py
```

## v2
- custo_usd: 0.7312460999999999
- turnos: 17
- duracao_ms: 114021
- subtype: success | is_error: False
- tokens_in: 34 | tokens_out: 10016
- wall_clock_s: 115
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 9 | linhas: 354
- NOTES.md: presente
- arvore:
```
NOTES.md
README.md
harness.py
prompt_template.txt
trace.jsonl
workspace/buggy_add.py
workspace/buggy_even.py
workspace/test_add.py
workspace/test_even.py
```

## v3
- custo_usd: 0.8131907
- turnos: 21
- duracao_ms: 140411
- subtype: success | is_error: False
- tokens_in: 42 | tokens_out: 11773
- wall_clock_s: 142
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 10 | linhas: 443
- NOTES.md: presente
- arvore:
```
NOTES.md
README.md
harness.py
safety.py
self_improve.py
self_improve_log.jsonl
trace.jsonl
verify.py
workspace/task.py
workspace/test_task.py
```

## v4
- custo_usd: 0.5666028
- turnos: 12
- duracao_ms: 77006
- subtype: success | is_error: False
- tokens_in: 24 | tokens_out: 6715
- wall_clock_s: 78
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 7 | linhas: 302
- NOTES.md: presente
- arvore:
```
NOTES.md
README.md
harness.py
harness_config.json
sandbox/solution.py
sandbox/test_task.py
trace.jsonl
```

## v5
- custo_usd: 0.8624555
- turnos: 25
- duracao_ms: 134371
- subtype: success | is_error: False
- tokens_in: 50 | tokens_out: 10864
- wall_clock_s: 136
- exit_code: 0  (124 = morto pelo prazo)
- arquivos: 12 | linhas: 361
- NOTES.md: presente
- arvore:
```
NOTES.md
README.md
harness.py
self_improve.py
strategies.json
tasks/task_off_by_one/solution.py
tasks/task_off_by_one/test_solution.py
tasks/task_return_none/solution.py
tasks/task_return_none/test_solution.py
tasks/task_swap_op/solution.py
tasks/task_swap_op/test_solution.py
trace.jsonl
```

