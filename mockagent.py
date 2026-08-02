#!/usr/bin/env python3
"""mockagent.py — o agente sintético, num lugar só. stdlib only.

Existe para que TODO aceite do harness rode a $0: `HARNESS_MOCK_AGENT=1` troca
`agent.run_agent` (e `project._call_agent`) por `run()` aqui. Nunca há chamada
de rede, API ou subprocess de CLI.

As diretivas são lidas linha a linha do prompt. Um agente real veria só texto
comum; o mock as trata como contrato:

    MOCK_TAMPER: <path abs>   apenda uma linha nesse arquivo (simula o agente
                              escapando do ws e editando o verificador)
    MOCK_FAIL: 1              devolve ok=False
    MOCK_SLEEP: <segundos>    dorme antes de responder (testa corrida de lock)
    MOCK_NOTES: <str>         força o campo `notes` do AgentResult — é assim
                              que um aceite forja `error_max_turns`, `timeout`
                              ou `tamper:` sem gastar um token.

Sempre escreve `ws/AGENT_OUTPUT.txt`: é o artefato que os verify.py de teste
checam por padrão.
"""

from __future__ import annotations

import time
from pathlib import Path


def run(prompt: str, workspace: Path):
    from agent import AgentResult

    t0 = time.time()
    fail = False
    notes = ""
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line.startswith("MOCK_TAMPER:"):
            target = Path(line.split(":", 1)[1].strip())
            with target.open("a") as fh:
                fh.write("\n# tampered-by-mock-agent\n")
        elif line.startswith("MOCK_FAIL:"):
            fail = line.split(":", 1)[1].strip() == "1"
        elif line.startswith("MOCK_SLEEP:"):
            time.sleep(float(line.split(":", 1)[1].strip()))
        elif line.startswith("MOCK_NOTES:"):
            notes = line.split(":", 1)[1].strip()

    ws = Path(workspace)
    try:
        (ws / "AGENT_OUTPUT.txt").write_text("mock:done\n")
    except OSError:
        # ws pode não existir em chamada de teste direta — o artefato é
        # conveniência, não a resposta.
        pass

    if not notes:
        notes = "mock_fail" if fail else ""
    return AgentResult(
        ok=not fail, seconds=time.time() - t0, tokens=0, cost_usd=0.0,
        turns=1, text="DONE: mock", notes=notes,
    )
