---
name = "langgraph-idioms"
kinds = ["code", "refactor"]
description = "Idiomas LangGraph do harness: idempotência por chave, configurable, interrupt, checkpointer"
---
## Idiomas LangGraph deste harness

- Nó com efeito externo é idempotente por chave natural `(run_id, node, attempt)`: no início, `store.get_node(...)` — se já existe, devolva o payload salvo com evento `reused=True` e NÃO refaça o efeito.
- `attempt` entra na chave só de nós por-tentativa (`execute`, `verify`, `measure`); nós únicos por run (`plan`, `provision`, `record`) ficam no attempt 0.
- Nós de decisão (`route`, `gate`) NÃO cacheiam por attempt de propósito: precisam recalcular a cada passagem, senão retry/escalação nunca mudam o desfecho.
- Infra que não é estado do run (data_dir, db, quem executa) viaja em `config["configurable"]`, lida via helper com default — nunca pelo estado/checkpoint. Estado é só o que descreve o run.
- Escalação humana é `interrupt()` no nó de escalate, não exception nem flag mágica no estado; o retorno do interrupt decide o próximo passo.
- Sempre rode com checkpointer e `thread_id` estável em `configurable`: mesmo `thread_id` + mesmo data_dir = retomada do ponto exato, incluindo processo morto no meio de um nó.
- Retomada: se há estado pendente no checkpointer, invoque com payload `None`; só passe `initial_state` em run novo.
- Cada nó retorna dict parcial de updates (incluindo lista `events`), nunca muta o estado recebido.
- Teste de idempotência obrigatório: rodar o nó duas vezes com a mesma chave produz um efeito externo só.
