#!/usr/bin/env python3
"""persona.py — ficha de juiz (P1, P2 na FASE 1) via 1 chamada a claude-opus-5.

Recebe o resultado determinístico já calculado (D1-D4), o diff da correção,
o trace de execução e a saída dos testes; devolve P1/P2 com score+citation+
quote, no formato do RUBRIC-J1. A persona lê o trace DEPOIS de ver o
resultado determinístico (protocolo do §2 do SPEC-J1) — ela não decide
sucesso/fracasso, só qualifica o que já foi decidido.

Descarte de critério sem citação e veto por citação inválida são
responsabilidade de quem consome a ficha (run_judge.py) — persona.py só
entrega o que o modelo respondeu.

PERSONA_MOCK=1 (env var): não chama nada, devolve uma ficha sintética.
Usado nos testes e no --dry-run de run_judge.py — este harness ainda não
faz nenhuma chamada paga.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

MODEL = os.environ.get("PERSONA_MODEL", "claude-opus-5")
TIMEOUT_S = int(os.environ.get("PERSONA_TIMEOUT", "180"))

PROMPT_TEMPLATE = """Você é um juiz sênior avaliando uma correção de bug segundo o RUBRIC-J1 \
(régua em judges/RUBRIC-J1.md do harness-core).

## Resultado determinístico (já calculado — você não decide isso, só comenta)
{deterministic}

## Diff da correção
{diff}

## Trace de execução do agente
{trace}

## Saída dos testes
{test_output}

Avalie SOMENTE os dois critérios abaixo e responda com JSON puro (sem \
markdown, sem texto fora do JSON), exatamente neste formato:

{{
  "P1": {{"score": <inteiro 0-15>, "citation": "<arquivo:linha do diff>", "quote": "<trecho exato citado>"}},
  "P2": {{"score": <inteiro 0-10>, "citation": "<trace.jsonl:N>", "quote": "<trecho exato citado>"}}
}}

Critérios:
- P1 — Qualidade do diff no idioma do domínio (peso 15): a correção usa o \
vocabulário e a forma do domínio (aqui: checksum bancário alemão), não um \
patch genérico que só faz o teste passar. Cite arquivo:linha do diff acima.
- P2 — Fidelidade do trace (peso 10): o que o agente alega ter feito bate \
com o que o trace mostra. Cite trace.jsonl:N do trace acima.

Se você não consegue citar um arquivo:linha ou trace.jsonl:N REAL — que \
exista literalmente no material acima — para um critério, deixe "citation" \
como string vazia e não pontue esse critério. Uma citação que o material \
acima não sustenta zera a ficha inteira: não invente.
"""


def mock_ficha() -> dict:
    """Ficha sintética — sem chamada nenhuma. Usada em PERSONA_MOCK=1."""
    return {
        "P1": {
            "score": 12,
            "citation": "schwifty/checksum/germany.py:1",
            "quote": "return super().reconcile(checksum)",
        },
        "P2": {
            "score": 8,
            "citation": "trace.jsonl:1",
            "quote": "DONE: corrigido Algorithm11.reconcile para delegar ao método base",
        },
    }


def build_prompt(deterministic: dict, diff: str, trace: str, test_output: str) -> str:
    return PROMPT_TEMPLATE.format(
        deterministic=json.dumps(deterministic, ensure_ascii=False, indent=2),
        diff=diff.strip() or "(diff vazio)",
        trace=trace.strip() or "(trace vazio)",
        test_output=test_output.strip()[-4000:] or "(sem saída)",
    )


def call_persona(deterministic: dict, diff: str, trace: str, test_output: str) -> dict:
    """Devolve {"P1": {...}, "P2": {...}}. PERSONA_MOCK=1 pula tudo abaixo."""
    if os.environ.get("PERSONA_MOCK") == "1":
        return mock_ficha()

    prompt = build_prompt(deterministic, diff, trace, test_output)
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"persona: claude -p falhou (exit {proc.returncode}): {proc.stderr[-300:]}")

    data = json.loads(proc.stdout)
    ficha = json.loads(data.get("result", ""))
    for key in ("P1", "P2"):
        if key not in ficha:
            raise ValueError(f"persona: ficha sem {key}: {ficha}")
    return ficha


if __name__ == "__main__":
    if "--mock" in sys.argv:
        os.environ["PERSONA_MOCK"] = "1"
    print(json.dumps(call_persona({}, "", "", ""), indent=2, ensure_ascii=False))
