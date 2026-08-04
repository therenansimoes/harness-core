"""API mínima: uma sonda de saúde e uma rota com corpo validado.

Padrão que vale copiar daqui: modelo de ENTRADA e modelo de SAÍDA separados
(`EchoIn`/`EchoOut`). O de saída é o que fixa o contrato — sem ele o FastAPI
serializa o que sobrar no dict e a resposta muda sem ninguém notar.
"""

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="fastapi-min", version="0.1.0")


class EchoIn(BaseModel):
    """Corpo do POST /echo. `Field` aqui não é enfeite: min_length devolve 422
    automático em vez de deixar string vazia entrar na regra de negócio."""

    message: str = Field(min_length=1, max_length=280)
    shout: bool = False


class EchoOut(BaseModel):
    message: str
    length: int


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Sonda de vida. Sem dependência nenhuma de propósito: se esta rota falha,
    o problema é o processo, não o banco."""
    return {"status": "ok"}


@app.post("/echo", response_model=EchoOut)
def echo(payload: EchoIn) -> EchoOut:
    """Devolve a mensagem (opcionalmente em maiúsculas) e o tamanho dela."""
    message = payload.message.upper() if payload.shout else payload.message
    return EchoOut(message=message, length=len(message))
