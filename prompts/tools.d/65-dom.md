<!--
Fragmento do manual das tools: ler o DOM. Some do prompt quando as tools de DOM
não estão montadas — descrever tool que não existe gasta turno do modelo
tentando chamá-la.
-->

## inspect_dom

Mostra UM elemento do DOM que o navegador montou.

- Argumentos: `selector` (string) e `port` (int, do `start_server` desta run).
- Exemplos: `inspect_dom(selector="#app", port=54231)` ·
  `inspect_dom(selector="button.primary", port=54231)`
- Seletor aceita **só forma simples**: `tag`, `#id`, `.class`, `tag.class`. Qualquer
  outra coisa (`div > p`, `ul li`, `a:hover`, `#a .b`) é recusada com
  `seletor não suportado` — não insista, escolha um alvo simples.
- É o DOM **depois** do navegador, não o arquivo do `read_file`: nó injetado por
  script aparece aqui e não aparece lá. É por isso que a tool existe.
- `existe: não` é resposta útil: o elemento que você acha que escreveu não chegou
  no DOM. Antes de mexer em CSS, descubra por que o nó não está lá.
- `computed` é **parcial e honesto**: só `display`, `color` e `font-size`, e só
  quando o valor está literal no `style=` ou numa regra literal do `<style>`.
  CSS externo, `var()` e herança não entram. `(nada declarado literalmente)`
  significa "não sei", não "não tem estilo".
- `bbox: indisponível (requer CDP)` é permanente. Não existe posição nem tamanho
  em pixel por aqui: para alinhamento, use `view_render` e olhe a tela.

## a11y_audit

Audita acessibilidade e devolve cada achado com o conserto ao lado.

- Argumentos: `port` (int) OU `dist_path` (string) — exatamente um.
- Exemplos: `a11y_audit(port=54231)` · `a11y_audit(dist_path="dist")`
- Verifica: `img` sem `alt`; `input` sem label associado (`<label for>`, `<label>`
  em volta ou `aria-label`); heading fora de ordem (`h1` → `h3` pulando o `h2`);
  link vazio ou só de ícone sem `aria-label`; contraste WCAG AA (4.5:1).
- Cada linha sai como `achado: arquivo/elemento — como corrigir`, e a última
  linha é a contagem. Conserte pela lista, sem reescrever a página inteira.
- `dist_path` varre os `.html` do diretório e o achado vem com o **nome do
  arquivo** — é o arquivo para abrir. `port` audita o DOM montado e o achado vem
  com `port-<porta>`.
- **Contraste tem duas respostas, e `não avaliável` não é reprovação.** A razão só
  sai quando texto E fundo são literais no `style=`/`<style>`. Cor de CSS externo
  ou de `var()` cai em `não avaliável` e **não conta como achado**: não invente que
  está errado nem que está certo.
- Zero achados não é certificado de acessibilidade: aqui não roda lighthouse nem
  leitor de tela. É o piso, não o teto.
