## write_todos

Escreve a sua lista de tarefas do run. A lista é ESTADO: ela volta para você a
cada turno, então é ela que lembra onde você parou.

- Argumentos: `todos` (lista de objetos, obrigatório), cada um com `content`
  (string — o passo, com o path do arquivo) e `status` (`pending`,
  `in_progress` ou `completed`).
- Exemplo: `write_todos(todos=[{"content": "editar /app.py: trocar - por +", "status": "in_progress"}, {"content": "rodar pytest -q", "status": "pending"}])`
- **Pegadinha crítica**: a chamada SUBSTITUI a lista inteira. Reenvie todos os
  itens sempre, com o status atualizado de cada um — item omitido desaparece.
- No máximo 7 itens. Lista longa é plano que você não vai seguir.
- Exatamente UM item em `in_progress` por vez. Marque `completed` na hora que
  terminar o passo, não em lote no fim.
- Quando chamar: a tarefa toca mais de um arquivo, ou pede refactor/implementar.
  Um passo só não precisa de lista — faça e reporte.
- Não é resposta: depois do último `write_todos`, ainda falta a frase de status
  com o que mudou e a saída real do comando de verificação.
