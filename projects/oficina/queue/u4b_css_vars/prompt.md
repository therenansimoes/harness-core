# u4b — variáveis de cor no CSS

Esta unidade é só CSS. Edite apenas `dist/style.css`. NÃO toque em HTML e NÃO
toque em JavaScript: o botão `#tema` já existe e o comportamento vem na
unidade seguinte.

Em `dist/style.css`:

1. No topo do arquivo, defina o tema claro:
   `:root { --bg: #f5f5f5; --fg: #333; }`
2. Logo abaixo, defina o tema escuro:
   `[data-theme="dark"] { --bg: #14161a; --fg: #e8e8ea; }`
3. Na regra do `body`, troque a cor fixa do fundo e do texto pelas variáveis.
   O `body` precisa ficar assim (as outras propriedades dele continuam):
   `body { background-color: var(--bg); color: var(--fg); ... }`
4. No `body` não pode sobrar nenhum valor fixo de `color`,  `background` ou
   `background-color` (nada de `#333`, `#f5f5f5`, `white`): só `var(--bg)` e
   `var(--fg)`.

As demais regras (`header`, `nav`, `footer`, tabela) podem continuar com as
cores fixas que já têm — não é escopo desta unidade.

NÃO use biblioteca externa, CDN ou npm.
