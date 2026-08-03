# u5 — página de projetos com gráfico SVG

Crie o arquivo `dist/projetos.html`.

Requisitos obrigatórios:

1. `<!doctype html>`, `<html lang="pt-BR">`, `<meta charset="utf-8">`,
   `<title>Projetos — Painel da Oficina</title>`.
2. `<link rel="stylesheet" href="style.css">` e, antes de `</body>`,
   `<script src="app.js"></script>`.
3. O mesmo `<header>` e `<nav>` das outras páginas, com
   `<a href="index.html">Início</a>` e o botão
   `<button id="tema" type="button">Tema</button>`.
4. Um `<h2>Horas por projeto</h2>` e, abaixo, um gráfico de barras em SVG
   INLINE (escrito à mão no HTML, sem JS e sem imagem externa):
   `<svg viewBox="0 0 460 260" width="460" height="260" role="img"
    aria-label="Horas por projeto">`
5. Dentro do SVG:
   - Um eixo X e um eixo Y com `<line>` (ex.: eixo Y de (50,20) a (50,220);
     eixo X de (50,220) a (440,220)).
   - SEIS barras `<rect>`, uma por projeto, todas com os atributos `x`, `y`,
     `width` e `height`. Barras verticais: largura fixa (ex.: 40) e altura
     proporcional às horas (1 hora = 4 px), com
     `y = 220 - altura`. Projetos e horas:
     Fonte ATX 18h, Reflow 32h, Bancada 24h, Osciloscópio 12h,
     Estação de solda 40h, Painel LED 28h.
   - Um `<text>` por barra com o nome do projeto abaixo do eixo X
     (`y="238"`, `font-size="11"`).
   - Um `<text>` com o valor de horas acima de cada barra.
   - Um `<text>` rotulando o eixo Y com a palavra "horas".
6. Abaixo do gráfico, uma `<ul>` com os 6 projetos e as horas em texto, para
   quem não vê o SVG.
7. Um `<footer>` igual ao das outras páginas.

NÃO use biblioteca de gráfico, CDN, imagem externa ou npm.
