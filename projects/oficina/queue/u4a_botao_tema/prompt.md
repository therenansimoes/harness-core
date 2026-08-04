# u4a — botão de tema em toda página

Esta unidade é só o botão e o arquivo de script. NÃO escreva CSS e NÃO escreva
lógica de tema aqui: isso vem nas unidades seguintes.

Trabalhe em TODAS as páginas HTML que existirem em `dist/` (hoje:
`index.html` e `inventario.html`; se houver `projetos.html`, ela também).

Em CADA página HTML de `dist/`:

1. Dentro do `<header>` da página, adicione exatamente esta linha:
   `<button id="tema">🌓</button>`
2. Antes de `</body>`, garanta que a página carrega o script novo:
   `<script src="tema.js"></script>`
   (se a página já carrega `app.js`, mantenha o `app.js` e some o
   `tema.js` — são dois `<script>`.)

No diretório `dist/`:

3. Crie o arquivo `dist/tema.js` com apenas um comentário dentro, por exemplo:
   `// tema: comportamento entra na unidade u4c`
   O arquivo precisa existir mesmo estando praticamente vazio.

NÃO altere `dist/style.css`. NÃO altere `dist/app.js`. NÃO use biblioteca
externa, CDN ou npm.
