"""Gatilhos: acordar o harness por EVENTO, não só comando ou cron.

Três portas: inbox de arquivos JSON (universal — qualquer coisa que escreve
arquivo acorda o loop), webhook HTTP que deposita no inbox, e vigia do
ledger que dispara quando falhas recentes cruzam o threshold.
"""

from harness.triggers.inbox import (
    Handler,
    default_handlers,
    process_inbox,
    serve_webhook,
)
from harness.triggers.watch import watch_inbox, watch_ledger

__all__ = [
    "Handler",
    "default_handlers",
    "process_inbox",
    "serve_webhook",
    "watch_inbox",
    "watch_ledger",
]
