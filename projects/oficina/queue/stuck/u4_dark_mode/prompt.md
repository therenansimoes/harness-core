# u4 — modo escuro persistido

Edite `dist/style.css`, `dist/app.js` e TODAS as páginas HTML que existirem em
`dist/` (hoje: `index.html` e `inventario.html`).

Em `dist/style.css`:

1. No topo, defina o tema claro em `:root`:
   `:root { --bg: #f7f7f8; --fg: #1b1b1f; --acc: #0b6; }`
2. Defina o tema escuro:
   `[data-theme="dark"] { --bg: #14161a; --fg: #e8e8ea; --acc: #3d9; }`
3. Troque as cores fixas por `var(--bg)`, `var(--fg)` e `var(--acc)` — no
   mínimo `body { background: var(--bg); color: var(--fg); }`.

Em CADA página HTML de `dist/`:

4. Dentro do `<header>` (ou do `<nav>`), adicione o botão:
   `<button id="tema" type="button">Tema</button>`
5. Garanta que a página carrega o script: `<script src="app.js"></script>`
   antes de `</body>` (o `index.html` também precisa).

Em `dist/app.js` (mantenha o filtro de busca que já está lá, apenas some
código novo; proteja o código do filtro para não quebrar em páginas sem a
tabela — teste se o elemento existe antes de usar):

6. Ao carregar: leia `localStorage.getItem('tema')`. Se o valor for `'dark'`,
   aplique `document.documentElement.setAttribute('data-theme', 'dark')`.
7. Registre `document.getElementById('tema').addEventListener('click', ...)`
   (só se o botão existir) para alternar: se o atributo `data-theme` for
   `'dark'`, remova-o e grave `localStorage.setItem('tema', 'light')`; senão
   ponha `data-theme="dark"` e grave `localStorage.setItem('tema', 'dark')`.

NÃO use biblioteca externa, CDN ou npm.
