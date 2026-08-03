# Quarentena

Exames sintetizados de falhas reais do harness (`synthesize_from_failures`): cada dir tem um `unit.toml` com a origem (run_id, exit_reason).
Ficam aqui, e não em `sealed/`, porque exame auto-gerado ainda não foi julgado — o loop de melhoria não pode se auto-avaliar com prova que ele mesmo escreveu.
Selar é ato humano: revise o `unit.toml` e rode `harness seal <name> --yes` para mover o dir para `benchmarks/sealed/`.
Sem `--yes` o comando recusa; nada além da CLI (humana) escreve em `sealed/`.
