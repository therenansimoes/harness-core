# u2 — página de inventário

Crie o arquivo `dist/inventario.html`.

Requisitos obrigatórios:

1. `<!doctype html>`, `<html lang="pt-BR">`, `<meta charset="utf-8">`,
   `<title>Inventário — Painel da Oficina</title>`.
2. Carregue o CSS existente: `<link rel="stylesheet" href="style.css">`.
3. Repita o mesmo `<nav>` do `dist/index.html`, incluindo um link
   `<a href="index.html">Início</a>`.
4. Uma `<table>` com `<thead>` contendo UMA `<tr>` e QUATRO `<th>`, nesta
   ordem e com estes textos exatos:
   `<th>Nome</th>`, `<th>Categoria</th>`, `<th>Qtd</th>`, `<th>Gaveta</th>`.
5. Um `<tbody>` com NO MÍNIMO 15 linhas `<tr>`, cada uma com exatamente 4
   `<td>` (nome, categoria, quantidade, gaveta).
6. Os dados devem ser componentes reais de eletrônica, plausíveis. Use por
   exemplo: Resistor 10kΩ 1/4W, Resistor 220Ω 1/4W, Capacitor 100nF cerâmico,
   Capacitor eletrolítico 470µF 25V, LED 5mm vermelho, LED 5mm verde, Diodo
   1N4007, Transistor BC547, Transistor IRF540N, Regulador LM7805, CI NE555,
   CI LM358, Arduino Nano, ESP32 DevKit v1, Push button 6mm, Header macho
   2,54mm, Jumper macho-macho 20cm.
   - Categoria: uma de "Passivo", "Semicondutor", "CI", "Módulo",
     "Conector", "Mecânico".
   - Qtd: um número inteiro plausível (ex.: 3 a 200).
   - Gaveta: um código curto como "A1", "A2", "B3", "C4".
7. Ao final, um `<footer>` igual ao do index.

NÃO invente colunas extras. NÃO use biblioteca externa, CDN ou npm.
