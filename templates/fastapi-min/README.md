# fastapi-min

API mínima: `GET /healthz` (sonda) e `POST /echo` (corpo validado por pydantic).

```sh
uv run pytest                                  # 2 testes, em processo, sem porta
uv run uvicorn app.main:app --reload           # docs interativas em /docs
```

Entrada e saída têm modelos separados (`EchoIn`/`EchoOut`): o de saída é o que
congela o contrato da resposta. Rota nova segue o mesmo par, mais um teste em
`tests/test_main.py` — um caso feliz e um 422.

`package = false` no `pyproject.toml` diz que isto é aplicação, não biblioteca;
`pythonpath = ["."]` é o que faz `import app` funcionar sem instalar nada.
