# scripts

`evolve.sh` — roda 1 ciclo de `harness improve` (auto-melhoria: muta config e testa em A/B) e apenda stdout+stderr com timestamp em `data/evolve.log`. Exit code do ciclo é preservado. Variáveis: `BACKEND` (default `mock`), `MODEL` (default vazio), `MAX_CYCLES` (default 1).

Crontab (1x/dia, 03h):

    0 3 * * * /caminho/para/harness-core-archive/scripts/evolve.sh

Aviso: com backend real (`BACKEND=claude` etc.) cada ciclo custa dinheiro, e o `escalate` para em interrupt esperando resposta humana (`harness improve --resume <thread>`) — não é fire-and-forget cego. Cheque `data/evolve.log` depois de cada rodada.
