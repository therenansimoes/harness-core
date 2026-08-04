# static-site

Página estática sem build: abra `index.html` no browser, ou sirva com `python -m http.server`.
`tokens.css` é a fonte de verdade de cor/espaço/tipografia — mude `--hue` e a identidade inteira muda.
`reset.css` vem depois dos tokens e cobre só o que vale para todo elemento; estilo de componente é seu.
Tema escuro segue o sistema; `<html data-theme="dark|light">` força um lado.
`app.js` está vazio de propósito: a página funciona sem JavaScript.
