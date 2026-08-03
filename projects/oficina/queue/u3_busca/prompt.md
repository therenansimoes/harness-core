# u3 — busca e filtro no inventário

Edite `dist/inventario.html` e crie `dist/app.js`.

Em `dist/inventario.html`:

1. Logo acima da `<table>`, adicione:
   `<input id="busca" type="search" placeholder="Filtrar por nome ou categoria">`
2. Antes de `</body>`, adicione: `<script src="app.js"></script>`
3. Dê um id à tabela: `<table id="tabela-inventario">`.
4. NÃO remova nem altere as linhas de dados que já existem.

Crie `dist/app.js` com JavaScript puro (vanilla, sem framework, sem import):

1. Pegue o input com `document.getElementById('busca')`.
2. Pegue as linhas com
   `document.querySelectorAll('#tabela-inventario tbody tr')`.
3. Registre `input.addEventListener('input', ...)`.
4. No handler: pegue `input.value.toLowerCase().trim()`; para cada linha,
   monte o texto da 1ª célula (nome) e da 2ª célula (categoria) em minúsculas
   e verifique se alguma contém o termo digitado.
5. Se combinar (ou se o termo estiver vazio), mostre a linha com
   `linha.style.display = ''`; se não combinar, esconda com
   `linha.style.display = 'none'`.

A comparação tem que ser case-insensitive (`toLowerCase` nos dois lados).
NÃO use biblioteca externa, CDN ou npm. NÃO crie outros arquivos.
