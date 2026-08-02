#!/usr/bin/env python3
"""whatsapp.py — o GATE de outbound. Nenhuma mensagem sai do harness sem passar aqui.

Invariante do módulo, em uma frase:

    `confirm_send()` é a ÚNICA função deste repo que chama o transporte de envio.

Tudo o mais — `request_send`, o assist, o evolve, um comando de CLI — no máximo
cria um `pending` no graph. Um pending não é uma mensagem: é um pedido de
permissão. Sem uma confirmação explícita e registrada, ele expira como pending e
nada é enviado.

Camadas de defesa (redundantes de propósito):
    1. allowlist verificada ao CRIAR o pedido
    2. allowlist verificada de novo ao CONFIRMAR (config pode ter mudado no meio)
    3. máquina de estados no graph: só `confirmed` transiciona para `sent`
    4. o serviço Node valida a allowlist uma terceira vez, e só escuta em 127.0.0.1

Se qualquer camada sozinha falhar, as outras ainda seguram. Isso é intencional:
o custo de uma mensagem indevida saindo para um terceiro é alto e irreversível.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import config as _config  # noqa: E402
import graph  # noqa: E402


class NotAllowed(Exception):
    """Destino fora da allowlist. Falha fechada — nunca vira warning."""


class ServiceError(Exception):
    """O serviço de WhatsApp não respondeu ou recusou. Não é veredito de gate."""


def cfg() -> dict:
    return _config.load()["whatsapp"]


def check_allowed(to_addr: str, c: dict | None = None) -> None:
    c = c or cfg()
    allow = c.get("allowlist") or []
    if not allow:
        raise NotAllowed(
            "allowlist vazia — o harness recusa todo envio. "
            "Configure whatsapp.allowlist em ~/.config/harness-core/config.toml"
        )
    if to_addr not in allow:
        raise NotAllowed(f"destino {to_addr!r} não está na allowlist")


# ------------------------------------------------------------------ transporte


def _http_send(to_addr: str, body: str, c: dict) -> str:
    """Fala com o serviço Node. Chamado SOMENTE por confirm_send()."""
    url = c["service_url"].rstrip("/") + "/send"
    data = json.dumps({"to": to_addr, "body": body}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=c.get("timeout_s", 20)) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ServiceError(f"HTTP {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise ServiceError(f"serviço indisponível em {url}: {e.reason}")
    mid = payload.get("message_id")
    if not mid:
        raise ServiceError(f"resposta sem message_id: {payload}")
    return mid


def service_status() -> dict:
    c = cfg()
    url = c["service_url"].rstrip("/") + "/status"
    try:
        with urllib.request.urlopen(url, timeout=c.get("timeout_s", 20)) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "jid": None, "qr": None, "last_error": str(e)}


# ----------------------------------------------------------------------- gate


def request_send(to_addr: str, body: str, requested_by: str, context: str = "") -> int:
    """Registra a INTENÇÃO de enviar. Não envia. Devolve o id do pending.

    Todo caminho do harness que "quer mandar uma mensagem" termina aqui.
    """
    check_allowed(to_addr)
    if not body.strip():
        raise ValueError("body vazio")
    return graph.record_outbound_request(
        to_addr=to_addr, body=body, requested_by=requested_by, context=context
    )


def confirm_send(outbound_id: int, actor: str, source: str = "cli", send_fn=None) -> dict:
    """A ÚNICA porta de saída. Confirma e envia, nesta ordem, uma vez só.

    `send_fn` existe para os testes injetarem um transporte falso. Em produção
    fica None e o transporte real é o `_http_send`.
    """
    row = graph.get_outbound(outbound_id)
    if row is None:
        raise ValueError(f"outbound {outbound_id} não existe")

    # Segunda checagem de allowlist: o pedido pode ter sido criado ontem, com
    # outra config. Quem envia é o agora, não o passado.
    c = cfg()
    check_allowed(row["to_addr"], c)

    # Levanta ValueError se não estiver 'pending' — impede confirmar duas vezes
    # e, com isso, impede enviar duas vezes.
    row = graph.confirm_outbound(outbound_id, actor=actor, source=source)

    sender = send_fn or (lambda to, b: _http_send(to, b, c))
    try:
        message_id = sender(row["to_addr"], row["body"])
    except Exception as e:  # noqa: BLE001
        graph.mark_outbound_failed(outbound_id, error=str(e)[:500])
        raise ServiceError(str(e)) from e
    return graph.mark_outbound_sent(outbound_id, message_id=str(message_id))


def cancel_send(outbound_id: int, actor: str, source: str = "cli", note: str = "") -> dict:
    return graph.cancel_outbound(outbound_id, actor=actor, source=source, note=note)


def pending() -> list[dict]:
    return graph.pending_outbound()


# -------------------------------------------------------------------- inbound


def read_inbox(since_ts: str | None = None) -> list[dict]:
    """Lê o inbox.jsonl que o serviço Node alimenta. Só leitura, sem consumir."""
    p = Path(cfg()["inbox_path"])
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_ts and msg.get("ts", "") <= since_ts:
            continue
        out.append(msg)
    return out


if __name__ == "__main__":
    c = cfg()
    print(f"service:   {c['service_url']}")
    print(f"owner:     {c['owner'] or '(não configurado)'}")
    print(f"allowlist: {c['allowlist'] or '(VAZIA — todo envio é recusado)'}")
    print(f"auto-reply ao dono: {c['allow_auto_reply_to_owner']}")
    print(f"status:    {service_status()}")
    print(f"pendentes: {len(pending())}")
