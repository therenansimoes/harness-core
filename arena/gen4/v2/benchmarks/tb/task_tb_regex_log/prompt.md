Escreva uma expressão regular que capture datas no formato YYYY-MM-DD que
aparecem em linhas contendo um endereço IPv4.

Se houver mais de uma data na mesma linha, a regex deve capturar apenas a
última data daquela linha.

Considere que fevereiro pode ter até 29 dias em qualquer ano (não distinga
ano bissexto de não bissexto). Endereços IPv4 usam notação decimal normal,
sem zeros à esquerda em cada octeto.

Atenção: pode haver no log texto parecido com data ou IP mas que não é
(ex.: `usuário 1134-12-1234`). Para evitar falsos positivos, garanta que
datas e IPs válidos não estejam imediatamente precedidos ou seguidos por
caracteres alfanuméricos.

Salve a regex em um arquivo `regex.txt` no diretório atual.

A regex será lida do arquivo e aplicada ao conteúdo do log usando
`re.findall` do Python com a flag `re.MULTILINE`. Exemplo de uso:

```python
import re

with open("/app/regex.txt") as f:
    pattern = f.read().strip()

matches = re.findall(pattern, log_text, re.MULTILINE)
```
