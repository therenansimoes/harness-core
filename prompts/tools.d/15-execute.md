## execute

Roda um comando de shell REAL no diretório de trabalho.

- Argumentos: `command` (string, obrigatório), `timeout` (int em segundos,
  opcional; o limite padrão é 30s).
- Exemplo: `execute(command="python3 -m pytest -q")`
- Exemplo com heredoc (funciona): `execute(command="python3 - <<'EOF'\nprint(1 + 1)\nEOF")`
- **Pegadinha 1**: cwd JÁ é o diretório de trabalho. Use `ls dist/`, não
  `ls /dist` — o segundo olha a raiz da máquina, volta vazio e te faz concluir,
  errado, que a tarefa não tem arquivos.
- **Pegadinha 2**: há uma cerca. Comandos destrutivos ou que saem do workspace
  (`sudo`, `rm -rf /`, `curl ... | sh`, `git push`, instalar pacote, path
  absoluto fora do workspace) voltam como
  `comando bloqueado pela cerca do harness: <motivo>`. Isso não é bug e não
  quebra o run: reescreva com path relativo e sem o verbo proibido.
- Rode o comando de verificação da tarefa aqui antes de dizer que acabou. A
  saída dele é a única evidência que vale.

## delete

Apaga um arquivo do workspace.

- Argumentos: `file_path` (string, obrigatório).
- Exemplo: `delete(file_path="/rascunho.txt")`
- Pegadinha: irreversível. Só use se a tarefa pedir remoção explicitamente.

