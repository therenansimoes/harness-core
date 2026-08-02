Você recebeu vários arquivos de log em um diretório `logs/`. Cada nome de
arquivo segue o padrão `YYYY-MM-DD_<source>.log` (ex.: `2025-08-10_db.log`),
indicando a data dos logs e a origem. Cada linha do log contém um evento com
um nível de severidade. Sua tarefa é analisar todos os logs e contar quantas
vezes cada severidade aparece dentro dos seguintes intervalos de data:

- Hoje (a data atual)
- Últimos 7 dias (incluindo hoje)
- Últimos 30 dias (incluindo hoje)
- Mês corrente até hoje (do dia 1 do mês atual até hoje, inclusive)
- Total (todos os arquivos de log combinados, independente da data)

Os níveis de severidade a contar são exatamente: ERROR, WARNING e INFO.

Escreva um arquivo CSV `summary.csv` no diretório atual com a seguinte
estrutura (incluindo o cabeçalho):

```
period,severity,count
today,ERROR,<count>
today,WARNING,<count>
today,INFO,<count>
last_7_days,ERROR,<count>
last_7_days,WARNING,<count>
last_7_days,INFO,<count>
last_30_days,ERROR,<count>
last_30_days,WARNING,<count>
last_30_days,INFO,<count>
month_to_date,ERROR,<count>
month_to_date,WARNING,<count>
month_to_date,INFO,<count>
total,ERROR,<count>
total,WARNING,<count>
total,INFO,<count>
```

Cada linha deve reportar a contagem total de cada severidade para o
intervalo de data correspondente.

Assuma que os nomes de arquivo sempre seguem o padrão
`YYYY-MM-DD_<source>.log`.

A data atual de referência é **2025-08-12**. Use-a como referência para
todos os cálculos.
