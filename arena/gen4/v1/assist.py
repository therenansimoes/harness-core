#!/usr/bin/env python3
"""assist.py — o harness respondendo ao dono pelo WhatsApp.

    python3 assist.py --once     # processa o que chegou e sai
    python3 assist.py --watch    # fica lendo o inbox (poll)

Política, explícita:

- Só o DONO é obedecido. Mensagem de qualquer outro JID é ignorada, mesmo que
  esteja na allowlist (allowlist autoriza *destino*; owner autoriza *comando*).
- Comandos executam apenas trabalho LOCAL e reversível: consultar status, listar
  pendentes, confirmar/cancelar um envio já registrado. Nada que gaste API,
  nada que mude o genome.
- Toda RESPOSTA é outbound e passa pelo mesmo gate: vira `pending`. Com
  `allow_auto_reply_to_owner = true` a resposta ao dono é auto-confirmada; o
  default é false, e nesse caso até a resposta fica pendente esperando `confirm`.

O caso mais interessante é `confirmar <id>`: é o dono usando o próprio WhatsApp
para destravar um envio. O evento fica no graph com source='whatsapp' e o JID
como actor — quem confirmou, quando, e o quê.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import config as _config  # noqa: E402
import graph  # noqa: E402
import whatsapp  # noqa: E402

STATE = ROOT / "evolution" / ".assist_cursor"
HELP = (
    "comandos: status · pendentes · confirmar <id> · cancelar <id> · "
    "decision · ajuda"
)


def _cursor() -> str | None:
    return STATE.read_text().strip() if STATE.exists() else None


def _save_cursor(ts: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(ts)


# ------------------------------------------------------------------ comandos


def cmd_status() -> str:
    version = (ROOT / "harness_version.txt").read_text().strip()
    try:
        out = subprocess.run(
            [sys.executable, str(ROOT / "score.py")],
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        ).stdout.strip().splitlines()
        placar = "\n".join(out[-3:])
    except Exception as e:  # noqa: BLE001
        placar = f"(score indisponível: {e})"
    pend = len(whatsapp.pending())
    return f"harness {version}\n{placar}\noutbound pendentes: {pend}"


def cmd_pendentes() -> str:
    rows = whatsapp.pending()
    if not rows:
        return "nenhum envio pendente."
    linhas = [
        f"#{r['id']} -> {r['to_addr']}: {r['body'][:60]}" + ("..." if len(r["body"]) > 60 else "")
        for r in rows[:10]
    ]
    return "pendentes:\n" + "\n".join(linhas) + "\n\nresponda: confirmar <id> | cancelar <id>"


def cmd_decision() -> str:
    decs = graph.recent_decisions(1)
    if not decs:
        return "nenhuma decision registrada ainda."
    d = decs[0]
    return (
        f"última decision: {d.get('proposal_id')} -> {d.get('outcome','?').upper()}\n"
        f"{d.get('ts','')}\n{str(d.get('reason',''))[:200]}"
    )


def cmd_confirmar(arg: str, actor: str) -> str:
    try:
        oid = int(arg)
    except ValueError:
        return f"id inválido: {arg!r}. use: confirmar <numero>"
    try:
        row = whatsapp.confirm_send(oid, actor=actor, source="whatsapp")
    except whatsapp.NotAllowed as e:
        return f"recusado: {e}"
    except whatsapp.ServiceError as e:
        return f"confirmado, mas o envio falhou: {e}"
    except ValueError as e:
        return f"não deu: {e}"
    return f"enviado #{oid} para {row['to_addr']} (message_id {row.get('message_id')})"


def cmd_cancelar(arg: str, actor: str) -> str:
    try:
        oid = int(arg)
    except ValueError:
        return f"id inválido: {arg!r}. use: cancelar <numero>"
    try:
        whatsapp.cancel_send(oid, actor=actor, source="whatsapp")
    except ValueError as e:
        return f"não deu: {e}"
    return f"cancelado #{oid}. nada foi enviado."


def handle(text: str, actor: str) -> str | None:
    """Interpreta um comando do dono. Devolve o texto da resposta, ou None."""
    parts = text.strip().split()
    if not parts:
        return None
    verbo, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if verbo in ("status", "st"):
        return cmd_status()
    if verbo in ("pendentes", "pending", "pendente"):
        return cmd_pendentes()
    if verbo in ("confirmar", "confirma", "sim", "ok"):
        return cmd_confirmar(arg, actor)
    if verbo in ("cancelar", "cancela", "nao", "não"):
        return cmd_cancelar(arg, actor)
    if verbo in ("decision", "decisao", "decisão"):
        return cmd_decision()
    if verbo in ("ajuda", "help", "?"):
        return HELP
    return None  # silêncio: o harness não conversa, só atende comando


# -------------------------------------------------------------------- resposta


def reply(to_addr: str, body: str, c: dict) -> str:
    """Resposta ao dono — pelo MESMO gate de qualquer outra mensagem."""
    oid = whatsapp.request_send(to_addr, body, requested_by="assist", context="reply")
    if not c.get("allow_auto_reply_to_owner"):
        return f"[pending #{oid}] resposta NÃO enviada (auto-reply desligado)"
    try:
        whatsapp.confirm_send(oid, actor="assist:auto_reply", source="assist")
        return f"[sent #{oid}]"
    except whatsapp.ServiceError as e:
        return f"[failed #{oid}] {e}"


def process_once(verbose: bool = True) -> int:
    c = _config.load()["whatsapp"]
    owner = c.get("owner")
    if not owner:
        print("owner não configurado — assist não obedece ninguém. Veja config.py.")
        return 0

    msgs = whatsapp.read_inbox(since_ts=_cursor())
    n = 0
    for m in msgs:
        _save_cursor(m.get("ts", ""))
        if m.get("is_group"):
            continue                      # grupo nunca comanda nada
        if m.get("from") != owner:
            if verbose:
                print(f"ignorado (não é o dono): {m.get('from')}")
            continue
        resp = handle(m.get("body", ""), actor=owner)
        if resp is None:
            continue
        n += 1
        status = reply(owner, resp, c)
        if verbose:
            print(f"<- {m.get('body','')[:50]!r}\n-> {status}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="assist por WhatsApp (só o dono comanda)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=5)
    a = ap.parse_args()

    if not (a.once or a.watch):
        ap.error("use --once ou --watch")
    if a.once:
        print(f"{process_once()} comando(s) processado(s)")
        return 0
    print("assist em watch. ctrl-c para sair.")
    while True:
        try:
            process_once()
            time.sleep(a.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
