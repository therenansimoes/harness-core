Tarefa: trocar UMA linha de um arquivo que já existe.

O arquivo `config.py` está no diretório de trabalho atual. Abra ele e troque o
valor de `TIMEOUT_SEGUNDOS` de `30` para `90`.

Depois da edição, a linha tem que ficar assim:

```
TIMEOUT_SEGUNDOS = 90
```

Regras:

- Mexa SÓ nessa linha. As outras linhas do arquivo (o comentário,
  `MAX_TENTATIVAS` e `FILA`) ficam exatamente como estão.
- Não adicione linha nova, não apague linha, não reordene nada.
- Não crie arquivo novo e não renomeie `config.py`.
- Assim que a linha estiver trocada, a tarefa está pronta: pare.
