# RESEARCH — API atual deepagents/langgraph (2026-08-02)

> Levantado pelo researcher (fontes: PyPI JSON, source no GitHub, docs oficiais). Insumo do PR-1.

## Pins verificados

```
deepagents==0.7.1
langchain==1.3.14
langgraph==1.2.10
langgraph-checkpoint-sqlite==3.1.1
langchain-ollama==1.1.0
```

- Python >=3.11 (deepagents é o mais restritivo).
- **ATENÇÃO**: deepagents 0.7.1 puxa `langchain-anthropic` e `langchain-google-genai` como deps OBRIGATÓRIAS — instalação não é 100% provider-agnostic (runtime é).
- langchain 1.3.14 → langgraph>=1.2.5,<1.3 ✔; langgraph 1.2.10 ↔ checkpoint-sqlite 3.1.1 ✔.

## deepagents 0.7.1

Factory: `create_deep_agent(model, tools, *, system_prompt, middleware, subagents, skills, memory, permissions, backend, interrupt_on, response_format, state_schema, context_schema, checkpointer, store, debug, name, cache) -> CompiledStateGraph`.

- **Modelo**: aceita string `init_chat_model` (`"ollama:qwen3:4b"` funciona) ou `BaseChatModel` (`ChatOllama(model="qwen3:4b")`).
- **`tools=` é ADITIVO, não allowlist.** Built-ins default: `ls, read_file, write_file, edit_file, delete, glob, grep, execute` (+ `task` com subagents). Restringir de verdade:
  - (a) substituir `FilesystemMiddleware(backend=..., tools=["ls","read_file","grep"])` via `middleware=` (merge por `.name`; tools fora da lista não são construídas). Inferido do source — smoke test antes de confiar.
  - (b) `register_harness_profile("ollama:qwen3:4b", HarnessProfile(excluded_tools=frozenset({"execute"})))`.
- **Workspace**: `backend=FilesystemBackend(root_dir="/abs/ws", virtual_mode=True)`. `virtual_mode=False` (default) NÃO dá segurança nenhuma — path traversal só é bloqueado com `virtual_mode=True`. Permissões declarativas via `permissions=[FilesystemPermission(...)]`.
- **Limite de turnos** — usar as duas camadas:
  ```python
  from langchain.agents.middleware import ModelCallLimitMiddleware
  agent = create_deep_agent(..., middleware=[ModelCallLimitMiddleware(thread_limit=10, run_limit=5, exit_behavior="end")])
  agent.invoke({...}, config={"recursion_limit": 50, "configurable": {"thread_id": "t1"}})
  ```
  `recursion_limit` não propaga a subagents (bug aberto deepagents#1698).

## Turnos / tokens / custo

- Nenhum campo de custo/turnos no retorno. Tokens via callback provider-agnostic:
  ```python
  from langchain_core.callbacks import UsageMetadataCallbackHandler
  cb = UsageMetadataCallbackHandler()
  res = agent.invoke({"messages": [...]}, config={"callbacks": [cb]})
  cb.usage_metadata  # {"qwen3:4b": {"input_tokens":..,"output_tokens":..}}
  turns = sum(1 for m in res["messages"] if m.type == "ai")
  ```
- **Custo em $ ninguém calcula client-side** → manter tabela preço/1M tokens por model_name em `config/models.toml` e multiplicar. Ollama = $0.

## langgraph-checkpoint-sqlite 3.1.1

Import path inalterado:
```python
from langgraph.checkpoint.sqlite import SqliteSaver
with SqliteSaver.from_conn_string("/abs/state.sqlite") as cp:
    graph = builder.compile(checkpointer=cp)
    cfg = {"configurable": {"thread_id": "run-42"}}
    graph.invoke(inp, cfg)   # mesmo thread_id = resume
```
Em deepagents: `create_deep_agent(..., checkpointer=cp)`.

**Segurança 3.x**: setar `LANGGRAPH_STRICT_MSGPACK=true` (ou `allowed_msgpack_modules`) — sem isso, DB comprometido pode executar código na desserialização. Entrar no bootstrap do cli junto com o veto de LangSmith.

## Pendências (smoke test no PR-1)

1. `FilesystemMiddleware(tools=[...])` via `middleware=` realmente substitui (inferido do source).
2. `ModelCallLimitMiddleware` sem colisão de nome no merge de middleware.
