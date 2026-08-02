## Goal

Complete o endpoint de cotação de pedidos B2B em `pricing.py`: dado um
carrinho de itens, calcular subtotal, aplicar desconto por volume e frete,
e validar entradas inválidas — hoje as funções são só esqueleto
(`NotImplementedError`).

## Regras

- Pode: editar `pricing.py` livremente; usar só a stdlib do Python.
- Pode: adicionar funções/helpers privados dentro do próprio arquivo.
- Não pode: adicionar dependências externas (pip, requirements.txt).
- Não pode: mudar a assinatura de `handle_quote_request(payload)`.
- Não pode: editar/apagar arquivos de teste.

## Dicas

- Desconto e frete têm limiares por quantidade/valor — releia os
  comentários `# regra:` no arquivo.
- `qty` ou `unit_price_cents` <=0 em qualquer item invalidam o pedido inteiro.
- Carrinho vazio é erro, não subtotal zero.
- Resposta de erro usa `status=400` com `error` legível.
- Valores monetários são sempre inteiros (centavos), nunca float.
