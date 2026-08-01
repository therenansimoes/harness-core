# harness-core

Motor de evolução com verificação. Plano em [`PLAN.md`](PLAN.md), atalho em
[`FAST_START.md`](FAST_START.md).

**Done = `verify.py` sai 0.** O que o agent diz não conta.

## Rodar

```bash
python3 run_task.py tasks/task_01        # uma task, uma vez
python3 run_task.py --all                # a suite fixed inteira
python3 run_task.py --all --repeat 3     # baseline com ruído
python3 run_task.py --all --suite sealed # só para creditar generalização
python3 run_task.py tasks/task_02 --keep # não apaga o workspace (debug)
```

Cada run: workspace temporário → fixtures → agent → `verify.py` no workspace →
linha em `results.tsv` → workspace apagado.

## Backends

| Backend | Como | Custo | Quando |
|---------|------|-------|--------|
| `cli` (default) | subprocess `claude -p` | assinatura | dia a dia, budget baixo |
| `api` | SDK `anthropic` | tokens | A/B sério, tokens crus |

```bash
HARNESS_BACKEND=api HARNESS_MODEL=claude-sonnet-5 python3 run_task.py --all
```

Variáveis: `HARNESS_BACKEND`, `HARNESS_MODEL`, `HARNESS_TIMEOUT`.

O `cost_usd` do backend `cli` é o custo **nocional** que o CLI reporta — na
assinatura você não paga isso, mas serve como proxy comparável entre A e B.

## Onde mexer

`agent.py` é o harness inteiro. `SYSTEM_PROMPT`, `MAX_TURNS`, `ALLOWED_TOOLS`,
`MODEL` são o genoma. **Um A/B muda uma coisa só.**

## Tasks

| Task | O que exige | Verificador |
|------|-------------|-------------|
| `task_01` | README com seções obrigatórias | estrutura + blocos de código |
| `task_02` | script CSV → resumo | saída bate com golden recomputado |
| `task_03` | consertar bugs até os testes passarem | testes verdes + hash do teste intacto |

`task_03` tem anti-cheat: editar `test_estoque.py` muda o hash e invalida a run.

Toda task nova precisa de um `verify.py` que foi **testado nas duas direções** —
falha no estado errado, passa no estado certo. Verificador que só sabe passar
não mede nada.

## A/B (etapa 3)

1. Baseline de `results.tsv` na `harness_version` atual
2. Mudar UMA coisa em `agent.py`, bump `harness_version.txt`
3. `--all --repeat 3` de novo
4. Comparar success / seconds / cost por versão
5. Confirmar em `benchmarks/sealed/` antes de creditar
6. Registrar merge|discard + motivo em `evolution/decisions/`
