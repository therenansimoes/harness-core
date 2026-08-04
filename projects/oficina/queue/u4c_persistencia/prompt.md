# u4c — persistência do tema

Esta unidade é só JavaScript. Escreva o conteúdo de `dist/tema.js` (o arquivo
já existe, com um comentário dentro, e já é carregado por todas as páginas).
NÃO toque em HTML, NÃO toque em `dist/style.css` e NÃO toque em `dist/app.js`.

O CSS já tem `:root` (tema claro) e `[data-theme="dark"]` (tema escuro); basta
pôr ou tirar o atributo `data-theme="dark"` no elemento `<html>`.

Em `dist/tema.js`, escreva nesta ordem:

1. Ao carregar o arquivo, leia a escolha salva:
   `const salvo = localStorage.getItem('tema');`
2. Se `salvo` for `'dark'`, aplique o tema escuro:
   `document.documentElement.setAttribute('data-theme', 'dark');`
3. Pegue o botão e só siga se ele existir:
   `const botao = document.getElementById('tema');`
   `if (botao) { ... }`
4. Dentro do `if`, registre o clique:
   `botao.addEventListener('click', function () { ... });`
5. Dentro do clique, alterne:
   - se `document.documentElement.getAttribute('data-theme') === 'dark'`:
     `document.documentElement.removeAttribute('data-theme');`
     `localStorage.setItem('tema', 'light');`
   - senão:
     `document.documentElement.setAttribute('data-theme', 'dark');`
     `localStorage.setItem('tema', 'dark');`

NÃO use biblioteca externa, CDN ou npm.
