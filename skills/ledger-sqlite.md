---
name = "ledger-sqlite"
kinds = ["code", "infra"]
description = "Padrões do ledger SQLite: schema idempotente, escrita por chave natural, JSON em payload"
---
## Ledger SQLite: padrões obrigatórios

- Schema inteiro em um script `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`, executado toda vez que o banco abre. Abrir = garantir diretório + schema; nunca assuma que o arquivo já existe.
- NUNCA migre schema em runtime: sem `ALTER TABLE`, sem versão de schema, sem "se coluna não existe, adiciona". Mudou o schema, é mudança de código com banco novo/rebuild — não patch no banco vivo.
- Escrita idempotente por chave natural: `UNIQUE` na chave (`run_id, node, attempt` etc.) + `INSERT OR IGNORE`. Reexecutar a mesma escrita é no-op, não erro nem duplicata.
- Antes de inserir algo caro, faça SELECT pela chave natural; achou => devolva o registro salvo e sinalize reuso.
- Dado de forma variável vai em UMA coluna `payload TEXT` com `json.dumps(..., default=str)`; leitura com `json.loads(row["payload"])`. Não crie coluna nova por campo novo.
- Colunas reais só para o que é filtrado/indexado (chaves, kind, tier, timestamp); o resto é payload.
- Timestamps como ISO string gerada num helper único (`now_iso()`), não `datetime.now()` espalhado.
- Conexão via context manager com commit no sucesso; uma transação por operação lógica.
- Teste: gravar duas vezes a mesma chave => 1 linha; reabrir o banco => schema ok sem erro.
