Escreva um script `wc_lite.py` (Python 3, apenas biblioteca padrão) que recebe
um único argumento de linha de comando: o caminho de um arquivo de texto.

Uso: `python3 wc_lite.py <arquivo>`

O script deve tratar os seguintes casos, nesta ordem de prioridade:

1. Se o número de argumentos passados não for exatamente 1 (zero ou mais de
   um): não imprima nada no stdout, imprima exatamente a linha
   `ERRO: uso: wc_lite.py <arquivo>` no stderr, e termine com código de saída
   `2`.
2. Se o argumento for passado mas o arquivo não existir: não imprima nada no
   stdout, imprima exatamente a linha `ERRO: arquivo nao encontrado: <caminho>`
   no stderr (onde `<caminho>` é exatamente o texto do argumento recebido,
   sem alterações), e termine com código de saída `3`.
3. Se o arquivo existir mas estiver vazio (0 bytes): não imprima nada no
   stdout, imprima exatamente a linha `ERRO: arquivo vazio` no stderr, e
   termine com código de saída `4`.
4. Caso contrário (o arquivo existe e tem conteúdo): leia o arquivo como texto
   (encoding utf-8) e imprima no stdout, EXATAMENTE, estas três linhas, nesta
   ordem, e nada no stderr, terminando com código de saída `0`:

```
LINHAS: <número de linhas>
PALAVRAS: <número de palavras>
CARACTERES: <número de caracteres>
```

Definições exatas a usar (com `texto` sendo o conteúdo lido do arquivo):
- linhas = `len(texto.splitlines())`
- palavras = `len(texto.split())`
- caracteres = `len(texto)`

Regras estritas:
- As mensagens de erro devem ser exatamente como especificado acima (sem
  acentos, sem pontuação extra).
- Nada além do especificado deve ir para stdout ou stderr em cada caso.
