Tarefa: ler um JSON e escrever outro JSON transformado.

O arquivo de entrada é `fixtures/in.json` (já existe, não mexa nele). Ele tem
uma lista `itens`, e cada item tem `nome`, `qtd` e `ativo`.

Escreva o arquivo `out.json` na RAIZ do diretório de trabalho (não dentro de
`fixtures/`) com exatamente duas chaves:

- `ativos`: lista com os `nome` dos itens que têm `ativo` igual a `true`, na
  mesma ordem em que aparecem em `fixtures/in.json`.
- `total_qtd`: a soma dos `qtd` desses itens ativos (só os ativos).

Formato esperado (os valores abaixo são só ilustração do formato):

```json
{"ativos": ["a", "b"], "total_qtd": 0}
```

Regras:

- Use só a biblioteca padrão do Python (`json`). Não instale nada.
- `out.json` tem que ser JSON válido, com essas duas chaves e mais nenhuma.
- Itens com `ativo` igual a `false` ficam fora da lista E fora da soma.
- Assim que `out.json` estiver escrito, a tarefa está pronta: pare.
