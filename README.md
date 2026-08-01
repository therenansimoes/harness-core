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

## Núcleo self-evolutive

Um ciclo = proposta → sandbox → suite → juiz → decisão → merge|discard → graph.
O baseline **não é tocado** até todos os gates passarem.

```bash
cp evolution/proposals/_template.md evolution/proposals/minha_ideia.md
$EDITOR evolution/proposals/minha_ideia.md      # hipótese + [change] old/new
python3 evolve.py --proposal evolution/proposals/minha_ideia.md --repeat 3
```

Exit **0 = merge** (genome promovido, versão bumpada) · **1 = discard** (baseline
intacto) · **2 = erro de infra** (não é veredito).

Quem julga é o `score.py --ab`, os mesmos gates normalizados de sempre — o
`evolve.py` não tem score próprio. A decisão sai em `evolution/decisions/<id>.md`
e tudo fica ligado no graph (proposta → runs da candidata → decisão).

```bash
python3 graph_query.py decisions          # histórico + placar merge/discard
python3 graph_query.py runs v0.2          # runs de uma versão
python3 graph_query.py ab v0.2 v0.3       # dois lados no graph
python3 tests/test_evolve_paths.py        # merge e discard sem gastar API
```
