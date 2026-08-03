# u6 — página "Sobre" e navegação sem link quebrado

Crie `dist/sobre.html` e ajuste o `<nav>` de TODAS as páginas de `dist/`.

Crie `dist/sobre.html` com:

1. `<!doctype html>`, `<html lang="pt-BR">`, `<meta charset="utf-8">`,
   `<title>Sobre — Painel da Oficina</title>`.
2. `<link rel="stylesheet" href="style.css">` e
   `<script src="app.js"></script>` antes de `</body>`.
3. O mesmo `<header>` (com `<h1>` e o botão
   `<button id="tema" type="button">Tema</button>`) e o mesmo `<nav>` das
   outras páginas.
4. NO MÍNIMO 3 parágrafos `<p>` explicando: (a) o que é o Painel da Oficina —
   um painel estático para organizar a bancada de eletrônica; (b) como o
   inventário e as gavetas são organizados; (c) como as horas por projeto são
   registradas. O arquivo tem que passar de 1024 bytes — texto real, não
   placeholder.
5. Um `<footer>` igual ao das outras páginas.

Em CADA arquivo `.html` de `dist/` (index, inventario, projetos, sobre):

6. O `<nav>` deve conter os quatro links, com estes hrefs literais:
   `href="index.html"`, `href="inventario.html"`,
   `href="projetos.html"`, `href="sobre.html"`.
   (A própria página também pode se listar; o importante é que os quatro
   hrefs apareçam.)
7. Todo `href` e todo `src` relativo tem que apontar para um arquivo que
   existe dentro de `dist/`. Não deixe link para arquivo que você não criou.
8. Todas as páginas precisam de `<meta charset="utf-8">` e `<html lang="...">`.

NÃO use biblioteca externa, CDN ou npm.
