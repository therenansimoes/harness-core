<!--
Fragmento do manual das tools: auto-crítica do próprio diff. Some do prompt
quando a tool de review não está montada.
-->

## diff_review

Mostra o que VOCÊ mudou no workspace, arquivo por arquivo.

- Sem argumentos: `diff_review()`.
- A saída traz a contagem por arquivo (`+linhas/-linhas`), o histograma do
  `--stat`, as primeiras 40 linhas do diff de cada arquivo e a lista de arquivos
  novos (untracked) com tamanho. A ordem é por linhas mudadas, do maior para o
  menor.
- **Chame isto antes de declarar pronto, sempre.** Verify_cmd verde não prova que
  você mudou só o que a tarefa pede: reformatação de arquivo vizinho, `print` de
  debug, `.bak` esquecido e arquivo criado por engano passam todos no teste. O
  que essa tool responde é a única pergunta que o teste não responde.
- Leia procurando o que NÃO deveria estar lá. Achou? Desfaça antes de seguir —
  reportar como ressalva não é o mesmo que consertar.
- `nenhuma mudança detectada` é resposta possível e é grave se você acha que
  editou algo: ou você escreveu fora do workspace, ou não escreveu.
- Binário sai só como `(binário, N bytes)`: o conteúdo não é despejado. Se o
  tamanho de um asset está absurdo, o problema é ele.
- O teto é de 4000 chars no total; com muito arquivo mexido a saída avisa quantos
  ficaram de fora. Nesse caso o `--stat` continua completo o suficiente para você
  ver os paths — use `read_file` nos que ficaram sem diff.
- Diff grande demais para revisar é sinal de que a tarefa cresceu além do pedido,
  não motivo para pular a leitura.
