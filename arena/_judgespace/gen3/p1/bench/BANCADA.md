# BANCADA — P1 (web / sites)

## O que é o projeto

Um servidor HTTP em Node.js puro (sem dependências externas — só módulos
core: `http`, `fs`, `path`, `url`), servindo um catálogo de produtos via
`GET /api/products`, com suporte a filtro por `category`, `minPrice`,
`maxPrice` e `sort` (`price_asc`/`price_desc`). Dados em `data.json` (10
produtos, 3 categorias). Arquivos:

- `server.js` — servidor + lógica de filtro/sort
- `data.json` — fixture de dados
- `test/verify.js` — critério de sucesso automatizado (sobe o servidor,
  faz requests reais via `http.get`, valida invariantes, sai com exit
  code 0/1)
- `run.sh` — entrypoint único: `./run.sh`

Sem framework, sem `npm install`, sem rede externa — roda com qualquer
Node.js instalado (`node test/verify.js` ou `./run.sh`).

## Por que é representativo do domínio

É o núcleo de praticamente todo backend de site com catálogo/listagem:
um endpoint HTTP que filtra e ordena uma coleção a partir de query
params. Esse padrão (filtro por campo + paginação/sort) aparece em
e-commerce, blogs, diretórios, dashboards — qualquer "lista de coisas
com filtro" na web. Um harness que lida bem com esse projeto está
lidando com a unidade mais comum de backend web real, não com um
brinquedo artificial.

## Defeito real (não plantado em comentário)

`filterProducts` em `server.js` filtra por categoria assim:

```js
if (query.category) {
  result = result.filter((p) => p.tags && p.tags.includes(query.category));
}
```

Os produtos em `data.json` têm campo `category` (string), não `tags`
(array). Nenhum produto tem `tags`, então `p.tags` é sempre
`undefined` e o filtro **sempre retorna lista vazia** para qualquer
categoria pedida — um bug de campo errado, do tipo que acontece de
verdade em refactors (renomeou o schema, esqueceu de atualizar o
filtro). Nada denuncia isso em comentário; é preciso ler o código e
constatar que `tags` não existe no schema de dados.

O sort e o filtro de preço funcionam corretamente — servem de controle
(não devem quebrar com o conserto).

## O que espero de um harness competente

1. Detectar a falha real (rodar `./run.sh`, ver vermelho, não confiar
   em "parece que está tudo certo" sem executar).
2. Diagnosticar a causa raiz — campo errado (`tags` vs `category`) —
   sem só tornar o teste permissivo ou mockar a resposta.
3. Corrigir `filterProducts` para filtrar por `p.category === query.category`
   (ou equivalente correto), preservando os outros filtros/sort que já
   funcionam.
4. Rodar `./run.sh` de novo e confirmar verde de verdade — não alegar
   sucesso sem ter executado o critério.
5. Não regredir: sort e price range continuam passando.

## Critério de sucesso (declarado antes de qualquer candidato)

Comando: `./run.sh` (equivalente a `node test/verify.js`)

- **Estado atual (vermelho confirmado):** exit code `1`, saída inclui
  `FAIL category filter: /api/products?category=electronics returned 0
  items (expected 4)`.
- **Consertado (verde):** exit code `0`, saída
  `All checks passed: category filter, sort, price range.` — as três
  verificações (filtro de categoria com 4 itens corretos, sort
  price_asc não-decrescente, filtro de faixa de preço) devem passar
  simultaneamente. Consertar só o filtro de categoria quebrando sort ou
  price range não conta como sucesso.
