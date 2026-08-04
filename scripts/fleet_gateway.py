#!/usr/bin/env python3
"""Proxy OpenAI-compatible que expõe a frota LoRA como modelos nomeados.

Qualquer app de chat que fale OpenAI aponta a base_url pra cá e vê
`fleet/sql`, `fleet/judge`, `fleet/base-qwen`… como se fossem modelos
separados. Quem sabe que um LoRA só existe colado num base, que o peso vai
no corpo do request e que cada card tem sampling próprio é este arquivo — o
cliente só escolhe um nome.

O registro é `config/adapters.toml` via `harness.routing.adapters`, lido UMA
vez no start: a frota é config, não estado, e recarregar a cada request só
esconderia um toml torto atrás de um 500 intermitente.

Sampling do card (temperature/top_p/max_tokens/repeat_penalty) entra só
quando o cliente NÃO mandou o seu: o adapter foi medido com aquele valor,
mas quem digitou um número na UI quis aquele número.

stdlib apenas — este script roda ao lado do mlx_lm.server, num venv que não
é o do repo, e uma dependência nova aqui seria uma dependência nova lá.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.routing.adapters import Adapter, load_adapters  # noqa: E402

DEFAULT_PORT = 1237
DEFAULT_MLX_BASE_URL = "http://127.0.0.1:1235/v1"
DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"

RUNTIME_MLX = "mlx"

# Prefixo do namespace: sem ele um cliente que lista modelos de dois servidores
# não distingue `sql` (peso local) de `sql` (qualquer outra coisa).
PREFIX = "fleet/"

# Bases puros = a mesma frota sem peso por cima. O `served_model` sai do
# registro em vez de constante: se o toml migrar de base, o alias migra junto
# e não vira um nome que aponta pra um modelo que ninguém mais serve.
BASE_ALIASES = {"base-qwen": "qwen", "base-llama": "llama"}

# Upstream lê o corpo inteiro antes de responder; um adapter frio custa reload
# (~1,2s) e um prompt longo com thinking passa fácil do minuto.
UPSTREAM_TIMEOUT = 600

# SSE em pedaços pequenos: o ganho do streaming é o primeiro token chegar
# cedo, e buffer grande devolve isso.
CHUNK = 1024


def mlx_base_url() -> str:
    return os.environ.get("HARNESS_MLX_BASE_URL", DEFAULT_MLX_BASE_URL).rstrip("/")


def lmstudio_base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL", DEFAULT_LMSTUDIO_BASE_URL).rstrip("/")


class Entry:
    """Um nome publicado. `adapter=None` é base puro (nome sem peso)."""

    def __init__(self, name: str, runtime: str, served_model: str, adapter: Adapter | None):
        self.name = name
        self.runtime = runtime
        self.served_model = served_model
        self.adapter = adapter


def build_registry(path: Path | str | None = None) -> dict[str, Entry]:
    """`{nome publicado: Entry}` — a frota inteira mais os bases puros.

    Alias de base sem nenhum adapter `mlx` que o use não é publicado: nome que
    o upstream não sabe carregar é pior que nome ausente, porque só falha no
    primeiro request de quem já escolheu ele.
    """
    fleet = load_adapters(path)
    registry: dict[str, Entry] = {}
    for ad in fleet:
        name = PREFIX + ad.id
        registry[name] = Entry(name, ad.runtime, ad.served_model, ad)
    for alias, token in BASE_ALIASES.items():
        served = _base_served_model(fleet, token)
        if served is None:
            continue
        name = PREFIX + alias
        registry[name] = Entry(name, RUNTIME_MLX, served, None)
    return registry


def _base_served_model(fleet: list[Adapter], token: str) -> str | None:
    """Primeiro `served_model` mlx cujo nome contém `token`, na ordem do toml."""
    for ad in fleet:
        if ad.runtime == RUNTIME_MLX and token in ad.served_model.lower():
            return ad.served_model
    return None


def resolve(model: str | None, registry: dict[str, Entry]) -> Entry | None:
    """Nome do cliente → Entry. Aceita sem o prefixo por tolerância."""
    if not model:
        return None
    hit = registry.get(model)
    if hit is not None:
        return hit
    return registry.get(PREFIX + model)


def rewrite(body: dict[str, Any], entry: Entry) -> dict[str, Any]:
    """Corpo do cliente → corpo do upstream, com o card por baixo.

    O `model` do cliente é o nome publicado, que o upstream não conhece: quem
    nomeia o modelo lá é o `served_model` do base. O peso vai em `adapters`
    (só o mlx_lm.server aceita peso por request; no LM Studio o modelo do card
    JÁ é o fine-tune inteiro).
    """
    out = dict(body)
    out["model"] = entry.served_model
    ad = entry.adapter
    if ad is None:
        return out
    if entry.runtime == RUNTIME_MLX:
        out["adapters"] = ad.ref
    kwargs = dict(out.get("chat_template_kwargs") or {})
    kwargs.setdefault("enable_thinking", bool(ad.enable_thinking))
    out["chat_template_kwargs"] = kwargs
    if ad.temperature is not None:
        out.setdefault("temperature", ad.temperature)
    if ad.top_p is not None:
        out.setdefault("top_p", ad.top_p)
    if ad.max_tokens is not None:
        out.setdefault("max_tokens", ad.max_tokens)
    if ad.repeat_penalty is not None:
        out.setdefault("repetition_penalty", ad.repeat_penalty)
    if ad.system:
        out["messages"] = _with_system(out.get("messages") or [], ad.system)
    return out


def _with_system(messages: list[Any], system: str) -> list[Any]:
    """`system` do card na frente de tudo, inclusive do system do cliente.

    É o texto com que o peso foi treinado; concatena em vez de substituir
    porque o cliente pediu o que pediu e derrubar a instrução dele calado
    seria uma resposta certa pra outra pergunta.
    """
    out = list(messages)
    for i, msg in enumerate(out):
        if isinstance(msg, dict) and msg.get("role") == "system":
            merged = dict(msg)
            merged["content"] = f"{system}\n\n{msg.get('content', '')}".rstrip()
            out[i] = merged
            return out
    return [{"role": "system", "content": system}, *out]


class Handler(BaseHTTPRequestHandler):
    """Rotas do gateway. `registry` e `verbose` vêm do servidor."""

    protocol_version = "HTTP/1.1"
    server_version = "fleet-gateway"

    @property
    def registry(self) -> dict[str, Entry]:
        return self.server.registry  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(200, self._models())
            return
        if path in ("", "/health", "/v1/health"):
            self._json(200, {"status": "ok", "models": len(self.registry)})
            return
        self._error(404, f"rota desconhecida: {self.path}")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, f"rota desconhecida: {self.path}")
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw or b"{}")
        except (ValueError, OSError) as exc:
            self._error(400, f"corpo inválido: {exc}")
            return
        if not isinstance(body, dict):
            self._error(400, "corpo precisa ser um objeto JSON")
            return
        entry = resolve(body.get("model"), self.registry)
        if entry is None:
            self._error(404, f"modelo desconhecido: {body.get('model')!r}")
            return
        base = mlx_base_url() if entry.runtime == RUNTIME_MLX else lmstudio_base_url()
        self._relay(f"{base}/chat/completions", rewrite(body, entry))

    def _models(self) -> dict[str, Any]:
        data = [
            {"id": name, "object": "model", "created": 0, "owned_by": "fleet"}
            for name in sorted(self.registry)
        ]
        return {"object": "list", "data": data}

    def _relay(self, url: str, payload: dict[str, Any]) -> None:
        """Manda pro upstream e devolve a resposta como ela veio.

        Streaming é pass-through do corpo: SSE é texto opaco pra este proxy e
        reserializar evento por evento só criaria uma segunda gramática pra
        manter em dia com o upstream.
        """
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = urlopen(req, timeout=UPSTREAM_TIMEOUT)
        except HTTPError as exc:
            # Erro do upstream é diagnóstico do usuário (modelo que não carrega,
            # adapter incompatível): passa status e corpo, não um 502 genérico.
            self._raw(exc.code, exc.headers.get("Content-Type", "application/json"), exc.read())
            return
        except (URLError, HTTPException, OSError) as exc:
            # `URLError` não cobre tudo: upstream que morre no meio do request
            # devolve `RemoteDisconnected`, e sem este braço a exceção sobe,
            # mata a thread do handler e o cliente vê a conexão cair sem status
            # nenhum — o pior diagnóstico possível pra quem só trocou de modelo.
            reason = getattr(exc, "reason", exc)
            self._error(502, f"upstream {url} falhou: {reason}")
            return
        with resp:
            ctype = resp.headers.get("Content-Type", "application/json")
            if payload.get("stream"):
                self._stream(resp, ctype)
            else:
                self._raw(resp.status, ctype, resp.read())

    def _stream(self, resp: Any, ctype: str) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        # Sem Content-Length no SSE: o tamanho só é conhecido no fim, e é
        # exatamente o fim que o cliente não pode esperar.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Cliente fechou a aba no meio da geração: não é erro deste proxy.
            pass

    def _raw(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._raw(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "fleet_gateway"}})

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def serve(port: int, registry: dict[str, Entry], verbose: bool = False) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.registry = registry  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    print(f"fleet gateway em http://127.0.0.1:{port}/v1 — {len(registry)} modelos", flush=True)
    print(f"  mlx      -> {mlx_base_url()}", flush=True)
    print(f"  lmstudio -> {lmstudio_base_url()}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet_gateway.py",
        description="Proxy OpenAI-compatible: a frota LoRA como modelos `fleet/<id>`.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FLEET_GATEWAY_PORT", DEFAULT_PORT)),
        help=f"porta de escuta (env FLEET_GATEWAY_PORT, default {DEFAULT_PORT})",
    )
    parser.add_argument("--adapters", default=None, help="caminho do adapters.toml")
    parser.add_argument("--list", action="store_true", help="lista os modelos e sai")
    parser.add_argument("-v", "--verbose", action="store_true", help="loga cada request")
    args = parser.parse_args(argv)

    registry = build_registry(args.adapters)
    if args.list:
        for name in sorted(registry):
            entry = registry[name]
            print(f"{name}\t{entry.runtime}\t{entry.served_model}")
        return 0
    serve(args.port, registry, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
