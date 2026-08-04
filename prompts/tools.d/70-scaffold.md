<!--
Fragmento do manual das tools: scaffold de projeto e geração de asset SVG. Some
do prompt quando as tools de scaffold não estão montadas.
-->

## scaffold

Cria um projeto novo a partir de um template CURADO do harness.

- Argumentos: `kind` (string, obrigatório — um dos kinds do catálogo), `name`
  (string, obrigatório — o nome da pasta nova).
- Exemplo: `scaffold(kind="static-site", name="landing")`
- Kinds: `static-site` (página sem build), `vite-vanilla` (JS vanilla com Vite +
  vitest), `fastapi-min` (API Python com 2 testes que passam).
- **Chame isto antes de escrever `index.html`, `package.json` ou `pyproject.toml`
  na mão.** O que você escreveria em 6 turnos já vem pronto e melhor: landmarks
  semânticos, skip-link, meta viewport, tokens de cor/espaço/tipografia com tema
  escuro, e teste verde no primeiro comando. Escrever isso de novo é gastar
  orçamento para entregar pior.
- `name` é UMA pasta no workspace: sem `/`, sem `..`, sem path absoluto. Para
  criar na raiz do workspace, faça o scaffold numa pasta e mova o que precisa.
- Destino que já tem arquivo é RECUSADO e nada é escrito. Se você já começou o
  projeto na mão, não tente scaffold por cima: ou escolhe outro nome, ou segue
  editando o que está lá.
- A saída lista todos os arquivos criados e os comandos do próximo passo (ex.:
  `npm install`, `uv run pytest`). Rode-os na pasta criada — não invente outros.
- Depois do scaffold, EDITE: título, descrição e o texto de placeholder são
  honestos de propósito ("substitua este texto"), e entregar com eles é entregar
  incompleto.

## asset_gen

Desenha um SVG por geometria e grava em `assets/`.

- Argumentos: `kind` (string, obrigatório — `icon`, `placeholder` ou
  `logo-mark`), `spec` (string, obrigatório — o conteúdo, formato por kind).
- `kind="icon"`: `spec` é a forma. Disponíveis: `seta`, `check`, `x`,
  `engrenagem`, `casa`, `lupa`. Ex.: `asset_gen(kind="icon", spec="lupa")`.
  Forma fora dessa lista volta erro com a lista — não tente `spec="foguete"`.
- `kind="placeholder"`: `spec` é `LARGURAxALTURA` mais um rótulo. Ex.:
  `asset_gen(kind="placeholder", spec="1200x630 Hero")`. Sai um retângulo com o
  rótulo e as dimensões escritas dentro — é o que evita imagem quebrada na tela.
- `kind="logo-mark"`: `spec` é o nome, opcionalmente com `circulo` ou `hex`.
  Ex.: `asset_gen(kind="logo-mark", spec="Oficina Aruã hex")` → monograma "OA".
- A cor sai do `tokens.css` do workspace quando existe, então o asset combina
  com o tema do projeto sozinho. Não passe cor.
- **Não escreva SVG à mão e não peça `<path d="...">` para você mesmo.** Path
  inventado não fecha, o browser mostra nada, e "arrumar o path" queima turno
  atrás de turno. Aqui a geometria é código: o XML sempre parseia.
- A saída traz o path relativo e o `<img>` pronto. Arquivo existente não é
  sobrescrito: sai `-2`, `-3` no nome.
