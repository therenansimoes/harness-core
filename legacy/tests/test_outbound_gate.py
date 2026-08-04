#!/usr/bin/env python3
"""Prova que nenhuma mensagem sai sem confirmação. Sem rede, sem WhatsApp real.

O transporte é injetado como espião (`send_fn`), então "não enviou" aqui não é
uma opinião: é o espião não ter sido chamado. Roda contra um DB temporário.

    python3 -m pytest tests/test_outbound_gate.py -q
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="gate_test_"))

OWNER = "5511900000000@s.whatsapp.net"
STRANGER = "5511911111111@s.whatsapp.net"

# Ambiente isolado ANTES de importar os módulos (eles leem env no import).
os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
os.environ["HARNESS_WA_OWNER"] = OWNER
os.environ["HARNESS_WA_ALLOWLIST"] = OWNER
os.environ["HARNESS_WA_INBOX"] = str(TMP / "inbox.jsonl")
os.environ["HARNESS_CONFIG_HOME"] = str(TMP / "noconfig")  # ignora config da máquina
sys.path.insert(0, str(REPO))

import assist  # noqa: E402
import graph  # noqa: E402
import whatsapp  # noqa: E402


class Spy:
    """Transporte falso. Conta chamadas — é o que prova 'não enviou'."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, to, body):
        self.calls.append((to, body))
        return f"MID{len(self.calls)}"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(TMP, ignore_errors=True)


def raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


def test_request_nao_envia():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "oi", requested_by="teste")
    row = graph.get_outbound(oid)
    assert row["status"] == "pending", f"request deveria criar pending, criou {row['status']}"
    assert spy.calls == [], "request_send NÃO pode tocar no transporte"
    assert any(p["id"] == oid for p in whatsapp.pending()), "pending não aparece na listagem"


def test_confirm_envia_uma_vez():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "mensagem real", requested_by="teste")
    row = whatsapp.confirm_send(oid, actor="cli", send_fn=spy)
    assert len(spy.calls) == 1, f"confirm deveria enviar 1x, enviou {len(spy.calls)}x"
    assert spy.calls[0] == (OWNER, "mensagem real"), f"payload errado: {spy.calls}"
    assert row["status"] == "sent", f"status deveria ser sent, é {row['status']}"
    assert row["message_id"] == "MID1", "message_id não foi gravado"

    # Confirmar de novo não pode enviar de novo.
    assert raises(ValueError, whatsapp.confirm_send, oid, actor="cli", send_fn=spy), (
        "confirmar duas vezes deveria levantar ValueError"
    )
    assert len(spy.calls) == 1, f"reconfirmação enviou de novo: {len(spy.calls)} chamadas"


def test_cancel_nao_envia():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "nao mandar", requested_by="teste")
    whatsapp.cancel_send(oid, actor="cli")
    assert graph.get_outbound(oid)["status"] == "cancelled", "cancel não mudou o status"
    assert raises(ValueError, whatsapp.confirm_send, oid, actor="cli", send_fn=spy), (
        "confirmar cancelado deveria levantar ValueError"
    )
    assert spy.calls == [], "cancelado NÃO pode ser enviado"


def test_gate_no_graph():
    """A camada de baixo sozinha já impede pending -> sent."""
    oid = whatsapp.request_send(OWNER, "pulando a fila", requested_by="teste")
    assert raises(ValueError, graph.mark_outbound_sent, oid, "MID_FALSO"), (
        "graph deveria recusar pending -> sent sem passar por confirmed"
    )
    assert graph.get_outbound(oid)["status"] == "pending", "status mudou apesar do erro"


def test_allowlist():
    spy = Spy()
    assert raises(whatsapp.NotAllowed, whatsapp.request_send, STRANGER, "oi", "teste"), (
        "destino fora da allowlist deveria ser recusado na criação"
    )

    # Pedido legítimo criado; allowlist esvaziada depois. Confirmar tem que falhar.
    oid = whatsapp.request_send(OWNER, "config vai mudar", requested_by="teste")
    antes = os.environ["HARNESS_WA_ALLOWLIST"]
    os.environ["HARNESS_WA_ALLOWLIST"] = ""
    os.environ["HARNESS_WA_OWNER"] = ""
    try:
        assert raises(whatsapp.NotAllowed, whatsapp.confirm_send, oid, actor="cli", send_fn=spy), (
            "allowlist vazia deveria recusar o envio na confirmação"
        )
        assert spy.calls == [], "enviou com allowlist vazia"
    finally:
        os.environ["HARNESS_WA_ALLOWLIST"] = antes
        os.environ["HARNESS_WA_OWNER"] = OWNER


def test_assist_so_obedece_o_dono():
    inbox = Path(os.environ["HARNESS_WA_INBOX"])
    inbox.write_text(
        f'{{"ts":"2026-08-01T10:00:00+00:00","from":"{STRANGER}","body":"status","is_group":false}}\n'
        f'{{"ts":"2026-08-01T10:00:01+00:00","from":"{OWNER}","body":"status","is_group":true}}\n',
        encoding="utf-8",
    )
    assist.STATE = TMP / "cursor"
    antes = len(graph.pending_outbound(limit=999))
    n = assist.process_once(verbose=False)
    depois = len(graph.pending_outbound(limit=999))
    assert n == 0, f"assist obedeceu quem não devia ({n} comandos)"
    assert depois == antes, "assist criou outbound para estranho/grupo"


def test_assist_reply_fica_pendente():
    inbox = Path(os.environ["HARNESS_WA_INBOX"])
    inbox.write_text(
        f'{{"ts":"2026-08-01T11:00:00+00:00","from":"{OWNER}","body":"pendentes","is_group":false}}\n',
        encoding="utf-8",
    )
    assist.STATE = TMP / "cursor2"
    n = assist.process_once(verbose=False)
    assert n == 1, f"assist deveria processar 1 comando do dono, processou {n}"
    novos = [p for p in graph.pending_outbound(limit=999) if p["requested_by"] == "assist"]
    assert len(novos) >= 1, "resposta do assist deveria virar pending"
    assert all(p["status"] == "pending" for p in novos), (
        "com auto-reply desligado, resposta ao dono NÃO pode sair sozinha"
    )
