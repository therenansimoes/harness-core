# u1 — esqueleto do Painel da Oficina

Crie o arquivo `dist/index.html` (crie o diretório `dist/` se não existir).

O arquivo `dist/index.html` deve ter, obrigatoriamente:

1. `<!doctype html>` na primeira linha e `<html lang="pt-BR">`.
2. Dentro de `<head>`: `<meta charset="utf-8">` e
   `<title>Painel da Oficina</title>`.
3. Um `<header>` com `<h1>Painel da Oficina</h1>` e um subtítulo curto
   descrevendo o painel (ex.: "bancada de eletrônica — inventário, projetos e
   horas").
4. Um `<nav>` com exatamente estes três links, com estes hrefs literais:
   - `<a href="inventario.html">Inventário</a>`
   - `<a href="projetos.html">Projetos</a>`
   - `<a href="sobre.html">Sobre</a>`
5. Um `<main>` com 3 blocos (`<section>`) de apresentação: "Inventário",
   "Projetos" e "Horas na bancada", cada um com um parágrafo de 2 a 3 frases
   sobre o que a seção mostra.
6. Um `<footer>` com o texto "Painel da Oficina — bancada de eletrônica".
7. Estilo: crie também `dist/style.css` e carregue com
   `<link rel="stylesheet" href="style.css">`. O CSS deve estilizar `body`,
   `header`, `nav a`, `main`, `section` e `footer` (fonte sem serifa, layout
   centralizado com largura máxima, espaçamento).

O arquivo `dist/index.html` tem que passar de 1024 bytes — escreva conteúdo
real nos parágrafos, não placeholders.

NÃO crie `inventario.html`, `projetos.html` nem `sobre.html` nesta unidade.
NÃO use nenhuma biblioteca externa, nenhum CDN, nenhum npm.
