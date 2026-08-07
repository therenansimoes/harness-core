---
name = "python-fixes"
kinds = ["code"]
description = "Correção de bug em Python com diff mínimo e verificação real"
paths = ["**/*.py"]
---
## Correção de bugs com diff mínimo

- Reproduza antes de mexer: rode o comando de verify da unit e leia a falha real.
- Diff mínimo: mude só as linhas que causam o bug — sem refactor, rename ou formatação junto.
- Não enfraqueça testes para "fazer passar"; o teste é o contrato.
- Depois do fix, rode o verify de novo e só declare pronto com saída verde. Falhou => reporte a falha real.
