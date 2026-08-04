## task

Delega um sub-pedaço isolado a um subagente, que devolve um resumo.

- Argumentos: `description` (string, obrigatório — a instrução completa e
  autossuficiente do subagente).
- Exemplo: `task(description="Liste os arquivos .py da raiz e resuma o que cada um faz")`
- Pegadinha: o subagente não vê a sua conversa. Instrução vaga volta lixo. Para
  tarefa pequena, fazer você mesmo é mais barato.
- Pedido com 2 ou mais frentes independentes (arquivos/assuntos que não se
  cruzam): mande ao `conductor`, que quebra e delega e devolve um plano só.
- Pedido de uma frente: `planner` direto — passar pelo `conductor` só gastaria
  um turno a mais.

## Fechamento obrigatório

Termine SEMPRE com uma frase de status do que você fez: quais arquivos mudaram
e qual foi a saída real do comando de verificação. Sem essa frase, o run conta
como desistência silenciosa, mesmo que o arquivo esteja certo.
