Tarefa: renomear uma função e arrumar quem chama ela.

No diretório de trabalho atual existem três arquivos:

- `util.py` define a função `calcula_total`.
- `pedido.py` chama `calcula_total`.
- `relatorio.py` chama `calcula_total`.

Renomeie a função de `calcula_total` para `soma_total`, nos três arquivos.

Depois da mudança:

- `util.py` tem `def soma_total(...)` e a palavra `calcula_total` não aparece
  mais em nenhum dos três arquivos (nem na definição, nem nos `import`, nem nas
  chamadas).
- `pedido.py` e `relatorio.py` continuam funcionando: `pedido.total_pedido` e
  `relatorio.linha_relatorio` seguem existindo, com o mesmo comportamento de
  antes.

Regras:

- Só o nome muda. Não altere o corpo das funções, não mude o que elas devolvem,
  não crie arquivo novo, não apague arquivo.
- Confira os `import` no topo de `pedido.py` e `relatorio.py`: eles também citam
  o nome antigo e precisam ser atualizados.
- Assim que os três arquivos estiverem atualizados, a tarefa está pronta: pare.
