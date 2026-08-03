# plugins/ — zona de código mutável

Única zona onde o loop de auto-melhoria pode reescrever CÓDIGO (estilo Darwin
Gödel Machine). Todo o resto do harness — régua, roteador, grafo, exames
selados, lock de deps — continua imutável pelo genoma (`config/genome.toml`).

Como funciona (`harness/improve/codegen.py`):

1. `propose_code_mutation` passa pelo MESMO genome check fail-closed de
   `mutate.check`: alvo fora de `plugins/**` recusa antes de tocar o disco.
2. Sintaxe inválida (`ast.parse`) também recusa sem escrever.
3. Escrita atômica guardando o fonte anterior; linhagem em
   `data/lineage.jsonl` (`{id, parent_id, target, ts}`).
4. `judge_code_mutation` julga por exame INJETADO (na vida real,
   `benchmarks/sealed/`): KEEP mantém, DISCARD restaura byte a byte.

A régua e o exame ficam FORA desta zona de propósito: o código mutado nunca
julga a si mesmo.

`kpi_lines.py` é o seed: `collect(path) -> dict` com contagem de arquivos e
linhas.
