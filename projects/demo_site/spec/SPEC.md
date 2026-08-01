+++
version = "1.0"
updated = "2026-08-01"
ui = true
+++

# demo_site — landing page da Oficina Volt

Site institucional de uma oficina fictícia de eletrônica ("Oficina Volt"),
especializada em conserto de placas, retrofit de equipamentos e consultoria
de hardware. É um projeto fixture: existe para exercitar a camada de entrega
do harness (checks de regression e acceptance), não para vender nada de
verdade.

## O que existe hoje

- `site/index.html` — página inicial com título, apresentação da oficina e
  um resumo dos serviços.
- `site/style.css` — folha de estilo linkada pelo index.

## Requisitos permanentes (regression)

Estes requisitos não podem regredir em nenhuma entrega futura:

1. `site/index.html` existe e não está vazio.
2. O HTML é bem formado — toda tag aberta é fechada, na ordem certa (sem tags
   soltas ou aninhamento quebrado).
3. O index tem `<title>` e pelo menos um `<h1>`.
4. O index linka `style.css` via `<link>` e o arquivo existe em disco.

## Critérios de UI (avaliação humana)

Estes critérios não são checáveis por script — servem de guia para quem
revisar a UI manualmente ou julgar screenshots:

1. **Hierarquia visual** — título principal se destaca claramente do resto
   do conteúdo (tamanho, peso ou cor).
2. **Contraste legível** — texto sobre fundo com contraste suficiente para
   leitura confortável, sem texto claro sobre fundo claro.
3. **Espaçamento consistente** — margens e paddings seguem um ritmo, sem
   elementos colados uns nos outros.
4. **Responsivo em telas estreitas** — o conteúdo não estoura a viewport
   nem exige rolagem horizontal em larguras de celular (~360px).
5. **Consistência de estilo** — botões, links e blocos de texto usam a
   mesma linguagem visual em toda a página.

## Fora de escopo (por enquanto)

- Página de preços (`site/precos.html`) — pedida pela sessão `s001`, ainda
  não entregue. Ver `acceptance/s001/`.
