#!/usr/bin/env bash
# Vigia em foreground: processa data/inbox/*.json e observa data/runs.sqlite (falhas → aviso).
# Deixar rodando: crontab `@reboot /caminho/scripts/watch.sh >> watch.log 2>&1` ou `tmux new -d -s watch ./scripts/watch.sh`.
# Acordar: echo '{"type":"improve"}' > data/inbox/improve.json  — ou via webhook (serve_webhook): curl -sX POST localhost:8787 -d '{"type":"research","topic":"timeouts"}'.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -c "
import threading
from pathlib import Path
from harness.triggers import default_handlers, watch_inbox, watch_ledger

inbox = Path('data/inbox')
inbox.mkdir(parents=True, exist_ok=True)
db = Path('data/runs.sqlite')
threading.Thread(
    target=watch_ledger,
    args=(db, lambda s: print('[ledger] falhas acima do threshold:', s, flush=True)),
    daemon=True,
).start()
print(f'[watch] inbox={inbox} ledger={db}', flush=True)
watch_inbox(inbox, default_handlers(), poll_s=10)
"
