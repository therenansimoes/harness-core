Crie um arquivo `run.py` no diretório atual implementando:

```python
async def run_tasks(tasks: list[Callable[[], Awaitable[None]]], max_concurrent: int) -> None
```

onde cada item de `tasks` é uma corrotina (job assíncrono) a ser executada e
`max_concurrent` é o número máximo de tasks que podem rodar concorrentemente.

A função precisa poder ser importada como `from run import run_tasks`.

Requisitos:
- Respeitar o limite `max_concurrent` (nunca mais que N tasks simultâneas).
- Se o processo receber um KeyboardInterrupt (Ctrl+C / SIGINT), o código de
  limpeza (bloco finally) de cada task já iniciada deve rodar mesmo assim.
- Use apenas a biblioteca padrão (asyncio). Pode instalar pacotes se achar
  necessário, mas não é preciso.
