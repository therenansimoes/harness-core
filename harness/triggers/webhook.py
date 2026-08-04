"""Porta HTTP do harness: POST autenticado vira arquivo no inbox.

Ao contrário do resto da config (governor, genome, ruler), esta falha
FECHADA: sem token configurado o webhook recusa tudo com 403. Um trigger é
execução remota de trabalho — default aberto aqui não é "fail-open
generoso", é porta destrancada na rua.

Configuração em `config/triggers.toml`:

    [webhook]
    token = "..."            # obrigatório; vazio = webhook desligado
    rate_limit = 30          # requests por janela, por IP
    rate_window_s = 60       # tamanho da janela
    max_body_bytes = 65536   # corpo maior que isso → 413

Env `HARNESS_WEBHOOK_TOKEN` sobrepõe o token do toml (deploy sem escrever
segredo em arquivo). O porteiro é a função pura `screen_request` — o teste
decide status sem abrir socket, e o relógio do rate-limit é injetado pelo
mesmo motivo que `sleep_fn` nos vigias: relógio nunca é parte do contrato.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import time
import tomllib
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from harness.routing import config_dir

TRIGGERS_TOML = "triggers.toml"
WEBHOOK_TOKEN_ENV = "HARNESS_WEBHOOK_TOKEN"
TOKEN_HEADER = "X-Harness-Token"

ACCEPTED = 202
BAD_REQUEST = 400
FORBIDDEN = 403
TOO_LARGE = 413
TOO_MANY = 429

NO_TOKEN_HELP = (
    f"[webhook] SEM TOKEN: recusando todo POST com {FORBIDDEN}. Para ligar, "
    f"escreva `token` em [webhook] de config/{TRIGGERS_TOML} ou exporte "
    f"{WEBHOOK_TOKEN_ENV}=<segredo>; o cliente manda o mesmo valor no header "
    f"{TOKEN_HEADER}."
)


@dataclass(frozen=True)
class WebhookConfig:
    """Defaults de fábrica. `token=""` é o estado seguro: porta fechada."""

    token: str = ""
    rate_limit: int = 30
    rate_window_s: float = 60.0
    max_body_bytes: int = 65536


def _pos_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def _pos_float(raw: Any, default: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _token(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def load_webhook_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> WebhookConfig:
    """`config/triggers.toml` + env → WebhookConfig.

    Números tortos caem no default (como no governor), mas o token NÃO tem
    default: toml ausente, ilegível ou sem `[webhook].token` = porta fechada.
    """
    p = Path(path) if path is not None else config_dir() / TRIGGERS_TOML
    base = WebhookConfig()
    environ = os.environ if env is None else env
    data: dict = {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = {}
    sec = data.get("webhook")
    sec = sec if isinstance(sec, dict) else {}
    return WebhookConfig(
        token=_token(environ.get(WEBHOOK_TOKEN_ENV)) or _token(sec.get("token")),
        rate_limit=_pos_int(sec.get("rate_limit"), base.rate_limit),
        rate_window_s=_pos_float(sec.get("rate_window_s"), base.rate_window_s),
        max_body_bytes=_pos_int(sec.get("max_body_bytes"), base.max_body_bytes),
    )


class RateLimiter:
    """Janela deslizante em memória: `limit` acertos por `window_s`, por chave.

    Em memória de propósito: o webhook é um processo só, e limite que precisa
    de banco vira dependência nova para conter abuso — o processo reinicia
    zerado e isso é aceitável para um freio, não para uma trava.
    """

    SWEEP_AT = 1024  # chaves vivas antes de varrer as mortas

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str = "") -> bool:
        """Registra o acerto e diz se ele cabe na janela. `limit<=0` fecha."""
        if self.limit <= 0:
            return False
        now = self._clock()
        cutoff = now - self.window_s
        if len(self._hits) > self.SWEEP_AT:
            self._sweep(cutoff)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def _sweep(self, cutoff: float) -> None:
        """Chave sem acerto vivo sai da tabela: freio não vira vazamento."""
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]


def token_ok(cfg: WebhookConfig, presented: str | None) -> bool:
    """Comparação em tempo constante. Sem token configurado: sempre False."""
    if not cfg.token:
        return False
    return hmac.compare_digest(cfg.token.encode("utf-8"), (presented or "").encode("utf-8"))


def screen_request(
    cfg: WebhookConfig,
    limiter: RateLimiter,
    presented_token: str | None,
    content_length: int,
    client: str = "",
) -> int:
    """Porteiro puro: devolve o status HTTP. `202` = pode gravar no inbox.

    Ordem importa: rate-limit antes do token para que tentativa de adivinhar
    segredo caia no mesmo freio de 429; tamanho antes de ler corpo para não
    engolir 10MB de quem só quer ocupar memória.
    """
    if not limiter.allow(client):
        return TOO_MANY
    if content_length > cfg.max_body_bytes:
        return TOO_LARGE
    if not token_ok(cfg, presented_token):
        return FORBIDDEN
    return ACCEPTED


def serve_webhook(
    port: int,
    inbox_dir: Path,
    max_requests: int | None = None,
    on_bind: Callable[[int], None] | None = None,
    config: WebhookConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """HTTP stdlib: corpo do POST vira `web-<ns>.json` no inbox, responde 202.

    Não processa nada — só deposita; quem processa é o watcher do inbox.
    `port=0` + `on_bind` = porta efêmera p/ teste; `max_requests` p/ não
    servir para sempre. `config=None` lê o toml; sem token, sobe avisando e
    recusando tudo (subir recusando é melhor que não subir: o operador vê o
    aviso no log em vez de um serviço que morreu calado).
    """
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    cfg = load_webhook_config() if config is None else config
    limiter = RateLimiter(cfg.rate_limit, cfg.rate_window_s, clock=clock)
    if not cfg.token:
        print(NO_TOKEN_HELP, file=sys.stderr)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = _content_length(self.headers.get("Content-Length"))
            status = screen_request(
                cfg,
                limiter,
                self.headers.get(TOKEN_HEADER),
                n,
                self.client_address[0] if self.client_address else "",
            )
            if status != ACCEPTED:
                # Corpo recusado nunca é lido: encerra a conexão em vez de
                # drenar bytes que já foram julgados grandes ou não-autorizados.
                self.close_connection = True
                self._refuse(status)
                return
            body = self.rfile.read(n)
            try:
                json.loads(body.decode("utf-8"))
            except Exception:
                self._refuse(BAD_REQUEST)
                return
            (inbox / f"web-{time.time_ns()}.json").write_bytes(body)
            self.send_response(ACCEPTED)
            self.end_headers()

        def _refuse(self, status: int) -> None:
            print(f"[webhook] {status} de {self.client_address[0]}", file=sys.stderr)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass  # silêncio: stderr é do watcher

    server = HTTPServer(("127.0.0.1", port), _Handler)
    try:
        if on_bind is not None:
            on_bind(server.server_address[1])
        if max_requests is None:
            server.serve_forever()
        else:
            for _ in range(max_requests):
                server.handle_request()
    finally:
        server.server_close()


def _content_length(raw: str | None) -> int:
    """Header ausente ou torto conta como 0: quem mente no tamanho cai no
    parse de JSON depois, não passa por cima do teto."""
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(n, 0)
