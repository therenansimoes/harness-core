Tarefa: criar DOIS arquivos Python, onde o segundo usa o primeiro.

No diretório de trabalho atual:

1. Crie `dados.py` com uma única variável, uma lista de três nomes nesta ordem:

```python
NOMES = ["ana", "bruno", "carla"]
```

2. Crie `usa_dados.py` que importa `NOMES` de `dados.py` e imprime só a
   quantidade de nomes da lista. Exemplo do que ele deve fazer:

```python
from dados import NOMES

print(len(NOMES))
```

Regras:

- Rodar `python3 usa_dados.py` tem que imprimir exatamente `3` e nada mais.
  Sem texto antes, sem texto depois, sem "Total:".
- `usa_dados.py` NÃO pode repetir a lista de nomes: ele tem que importar de
  `dados.py`.
- Não crie nenhum outro arquivo e não use bibliotecas externas.
- Assim que os dois arquivos estiverem escritos, a tarefa está pronta: pare.
