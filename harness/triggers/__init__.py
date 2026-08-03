"""Gatilhos: acordar o harness por EVENTO, não só comando ou cron.

Três portas: inbox de arquivos JSON (universal — qualquer coisa que escreve
arquivo acorda o loop), webhook HTTP que deposita no inbox, e vigia do
ledger que dispara quando falhas recentes cruzam o threshold.

O inbox confia em quem escreve arquivo (quem tem o disco já tem a máquina);
o webhook não confia em ninguém: token, rate-limit e teto de corpo em
`triggers/webhook.py`, fechado por default.
"""

from harness.triggers.inbox import (
    Handler,
    default_handlers,
    process_inbox,
)
from harness.triggers.watch import watch_inbox, watch_ledger
from harness.triggers.webhook import (
    TOKEN_HEADER,
    WEBHOOK_TOKEN_ENV,
    RateLimiter,
    WebhookConfig,
    load_webhook_config,
    screen_request,
    serve_webhook,
    token_ok,
)

__all__ = [
    "TOKEN_HEADER",
    "WEBHOOK_TOKEN_ENV",
    "Handler",
    "RateLimiter",
    "WebhookConfig",
    "default_handlers",
    "load_webhook_config",
    "process_inbox",
    "screen_request",
    "serve_webhook",
    "token_ok",
    "watch_inbox",
    "watch_ledger",
]
