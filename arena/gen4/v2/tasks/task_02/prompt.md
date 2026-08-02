O diretório atual contém `vendas.csv` com as colunas:
`data,regiao,produto,quantidade,valor`.

Escreva um script `summarize.py` (Python 3, apenas biblioteca padrão) que leia
`vendas.csv` do diretório atual e imprima EXATAMENTE este formato no stdout:

```
LINHAS: <número total de linhas de dados>
TOTAL: <soma da coluna valor, 2 casas decimais>
REGIOES: <lista de regiões distintas, ordem alfabética, separadas por vírgula sem espaço>
TOP_PRODUTO: <produto com maior soma de valor>
MEDIA_QTD: <média da coluna quantidade, 2 casas decimais>
```

Regras estritas:
- Cinco linhas, nesta ordem, nada mais no stdout.
- Sem espaço antes do valor além do único espaço após os dois-pontos.
- Números decimais com ponto e exatamente 2 casas.
- O script deve rodar com `python3 summarize.py` sem argumentos e sair com código 0.
