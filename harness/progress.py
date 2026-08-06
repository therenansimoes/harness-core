"""Sinal de vida do `harness do`.

Sem isto o `harness do` fica ~200s calado enquanto o agente trabalha — o
leigo que testou achou que tinha travado. Este módulo imprime em stderr (o
stdout fica limpo para o relatório final): um heartbeat periódico e uma linha
por etapa concluída do grafo.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time

HEARTBEAT_S = 15.0

# nó do grafo (topology.NODE_IMPLS) -> etapa concluída, em português.
STAGE_LABELS: dict[str, str] = {
    "plan": "pedido lido",
    "route": "executor escolhido",
    "provision": "cópia isolada do repo pronta",
    "execute": "o agente terminou de trabalhar",
    "verify": "régua rodada",
    "measure": "resultado medido",
    "gate": "decisão tomada",
    "accept": "entrega gravada na branch",
    "retry": "nova tentativa",
    "escalate": "escalado para revisão",
    "revert": "revertido",
    "record": "gravado no histórico",
    "reflect": "reflexão sobre a tentativa anterior",
}


class Progress:
    """Heartbeat + linhas de etapa concluída, em thread separada."""

    def __init__(self, out=None, every_s: float = HEARTBEAT_S, clock=time.monotonic):
        # `out` fica None até a hora de imprimir: default fixado em sys.stderr
        # aqui capturaria o stderr de import time, não o do capsys do teste.
        self._out = out
        self._every_s = every_s
        self._clock = clock
        self._t0 = clock()
        self._ultima = "começando"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Progress:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def stage(self, node: str) -> None:
        label = STAGE_LABELS.get(node, node)
        with self._lock:
            self._ultima = label
        self._print(f"  · {label} ({self._segundos()}s)")

    def _loop(self) -> None:
        while not self._stop.wait(self._every_s):
            with self._lock:
                ultima = self._ultima
            self._print(f"  … {self._segundos()}s de execução — última etapa: {ultima}")

    def _segundos(self) -> int:
        return int(self._clock() - self._t0)

    def _print(self, texto: str) -> None:
        # Progresso nunca pode derrubar um run — se o stream já fechou (fim de
        # teste, pipe quebrado), engole o erro.
        with self._lock, contextlib.suppress(ValueError, OSError):
            print(texto, file=self._out or sys.stderr, flush=True)
