O diretório atual contém `pedidos.json`: uma lista de pedidos. Cada pedido tem
os campos `id` (inteiro), `cliente` (string) e `itens` (lista de objetos com
`produto`, `quantidade` e `preco`; a lista pode estar vazia).

Escreva um script `transform.py` (Python 3, apenas biblioteca padrão) que leia
`pedidos.json` do diretório atual e escreva um arquivo `resumo.txt` no
diretório atual, com um total de linhas igual ao número de pedidos.

Para cada pedido, calcule o total como a soma de `quantidade * preco` de todos
os seus itens (pedido sem itens tem total 0.00). Cada linha de `resumo.txt`
deve ter EXATAMENTE este formato:

```
<id>|<cliente>|<total>
```

Regras estritas:
- `<total>` com ponto decimal e exatamente 2 casas.
- As linhas devem estar ordenadas do pedido de MAIOR total para o de MENOR
  total.
- Em caso de empate no total, desempate pelo `id` em ordem crescente.
- Não inclua cabeçalho, linha em branco extra, nem nenhum outro texto no
  arquivo além dessas linhas (uma por pedido).
- O script deve rodar com `python3 transform.py` sem argumentos e sair com
  código 0.
