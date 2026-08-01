#!/usr/bin/env python3
"""Prova que nenhuma mensagem sai sem confirmação. Sem rede, sem WhatsApp real.

O transporte é injetado como espião (`send_fn`), então "não enviou" aqui não é
uma opinião: é o espião não ter sido chamado. Roda contra um DB temporário.

    python3 tests/test_outbound_gate.py    # exit 0 = gate íntegro
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

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


FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def test_request_nao_envia():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "oi", requested_by="teste")
    row = graph.get_outbound(oid)
    check(row["status"] == "pending", f"request deveria criar pending, criou {row['status']}")
    check(spy.calls == [], "request_send NÃO pode tocar no transporte")
    check(any(p["id"] == oid for p in whatsapp.pending()), "pending não aparece na listagem")


def test_confirm_envia_uma_vez():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "mensagem real", requested_by="teste")
    row = whatsapp.confirm_send(oid, actor="cli", send_fn=spy)
    check(len(spy.calls) == 1, f"confirm deveria enviar 1x, enviou {len(spy.calls)}x")
    check(spy.calls[0] == (OWNER, "mensagem real"), f"payload errado: {spy.calls}")
    check(row["status"] == "sent", f"status deveria ser sent, é {row['status']}")
    check(row["message_id"] == "MID1", "message_id não foi gravado")

    # Confirmar de novo não pode enviar de novo.
    check(raises(ValueError, whatsapp.confirm_send, oid, actor="cli", send_fn=spy),
          "confirmar duas vezes deveria levantar ValueError")
    check(len(spy.calls) == 1, f"reconfirmação enviou de novo: {len(spy.calls)} chamadas")


def test_cancel_nao_envia():
    spy = Spy()
    oid = whatsapp.request_send(OWNER, "nao mandar", requested_by="teste")
    whatsapp.cancel_send(oid, actor="cli")
    check(graph.get_outbound(oid)["status"] == "cancelled", "cancel não mudou o status")
    check(raises(ValueError, whatsapp.confirm_send, oid, actor="cli", send_fn=spy),
          "confirmar cancelado deveria levantar ValueError")
    check(spy.calls == [], "cancelado NÃO pode ser enviado")


def test_gate_no_graph():
    """A camada de baixo sozinha já impede pending -> sent."""
    oid = whatsapp.request_send(OWNER, "pulando a fila", requested_by="teste")
    check(raises(ValueError, graph.mark_outbound_sent, oid, "MID_FALSO"),
          "graph deveria recusar pending -> sent sem passar por confirmed")
    check(graph.get_outbound(oid)["status"] == "pending", "status mudou apesar do erro")


def test_allowlist():
    spy = Spy()
    check(raises(whatsapp.NotAllowed, whatsapp.request_send, STRANGER, "oi", "teste"),
          "destino fora da allowlist deveria ser recusado na criação")

    # Pedido legítimo criado; allowlist esvaziada depois. Confirmar tem que falhar.
    oid = whatsapp.request_send(OWNER, "config vai mudar", requested_by="teste")
    antes = os.environ["HARNESS_WA_ALLOWLIST"]
    os.environ["HARNESS_WA_ALLOWLIST"] = ""
    os.environ["HARNESS_WA_OWNER"] = ""
    try:
        check(raises(whatsapp.NotAllowed, whatsapp.confirm_send, oid, actor="cli", send_fn=spy),
              "allowlist vazia deveria recusar o envio na confirmação")
        check(spy.calls == [], "enviou com allowlist vazia")
    finally:
        os.environ["HARNESS_WA_ALLOWLIST"] = antes
        os.environ["HARNESS_WA_OWNER"] = OWNER


def test_assist_so_obedece_o_dono():
    inbox = Path(os.environ["HARNESS_WA_INBOX"])
    inbox.write_text(
        '{"ts":"2026-08-01T10:00:00+00:00","from":"%s","body":"status","is_group":false}\n'
        '{"ts":"2026-08-01T10:00:01+00:00","from":"%s","body":"status","is_group":true}\n'
        % (STRANGER, OWNER),
        encoding="utf-8",
    )
    assist.STATE = TMP / "cursor"
    antes = len(graph.pending_outbound(limit=999))
    n = assist.process_once(verbose=False)
    depois = len(graph.pending_outbound(limit=999))
    check(n == 0, f"assist obedeceu quem não devia ({n} comandos)")
    check(depois == antes, "assist criou outbound para estranho/grupo")


def test_assist_reply_fica_pendente():
    inbox = Path(os.environ["HARNESS_WA_INBOX"])
    inbox.write_text(
        '{"ts":"2026-08-01T11:00:00+00:00","from":"%s","body":"pendentes","is_group":false}\n'
        % OWNER,
        encoding="utf-8",
    )
    assist.STATE = TMP / "cursor2"
    n = assist.process_once(verbose=False)
    check(n == 1, f"assist deveria processar 1 comando do dono, processou {n}")
    novos = [p for p in graph.pending_outbound(limit=999) if p["requested_by"] == "assist"]
    check(len(novos) >= 1, "resposta do assist deveria virar pending")
    check(all(p["status"] == "pending" for p in novos),
          "com auto-reply desligado, resposta ao dono NÃO pode sair sozinha")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as e:  # noqa: BLE001
            FAILS.append(f"{t.__name__} estourou: {type(e).__name__}: {e}")
        if len(FAILS) == before:
            print(f"OK {t.__name__}")
    if FAILS:
        print("\nFALHOU:\n  - " + "\n  - ".join(FAILS))
        return 1
    print(f"\n{len(tests)} testes de gate verdes — nada sai sem confirmação.")
    return 0


if __name__ == "__main__":
    import shutil

    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
