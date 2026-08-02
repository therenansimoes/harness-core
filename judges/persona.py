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

Avalie SOMENTE os dois critérios abaixo. Antes de pontuar, escreva o \
raciocínio em texto puro (sem markdown), uma linha por critério, no \
formato "Evidência P1: <arquivo:linha ou trace.jsonl:N> — <o que essa \
linha mostra>" / "Evidência P2: <trace.jsonl:N> — <o que essa linha \
mostra>" — só cite o que está literalmente no material acima. Depois \
desse raciocínio, na última parte da resposta, escreva SÓ o JSON puro \
(sem markdown, sem texto depois dele), exatamente neste formato:

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


def extract_ficha_json(result_text: str) -> dict:
    """O prompt agora pede raciocínio (CoT — evidência por critério) ANTES
    do JSON final, então `result` não é mais JSON puro de ponta a ponta.
    Acha o primeiro `{` e decodifica a partir dali, ignorando texto solto
    depois do objeto (raw_decode não exige que a string acabe no `}`)."""
    start = result_text.find("{")
    if start == -1:
        raise json.JSONDecodeError("nenhum '{' encontrado no result", result_text, 0)
    obj, _ = json.JSONDecoder().raw_decode(result_text, start)
    return obj


def call_persona(deterministic: dict, diff: str, trace: str, test_output: str) -> dict:
    """Devolve {"P1": {...}, "P2": {...}}. PERSONA_MOCK=1 pula tudo abaixo."""
    if os.environ.get("PERSONA_MOCK") == "1":
        return mock_ficha()

    prompt = build_prompt(deterministic, diff, trace, test_output)
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)

    # Mesmo padrão de agent.py/_run_cli: `claude -p` pode sair com exit != 0
    # (ex.: is_error=true em algum evento intermediário) e ainda assim ter
    # emitido um `result` com JSON válido. Tratar returncode!=0 como falha
    # de infra ANTES de tentar parsear descartaria uma ficha real. Tenta
    # parsear primeiro; só cai em erro de infra se não sobrou nada
    # aproveitável no stdout.
    try:
        data = json.loads(proc.stdout)
        ficha = extract_ficha_json(data.get("result", ""))
        if not all(key in ficha for key in ("P1", "P2")):
            raise ValueError(f"persona: ficha sem P1/P2: {ficha}")
    except (json.JSONDecodeError, ValueError) as exc:
        if proc.returncode != 0:
            raise RuntimeError(
                f"persona: claude -p falhou (exit {proc.returncode}): {proc.stderr[-300:]}"
            ) from exc
        raise

    return ficha


# ------------------------------------------------------- trilha build (J2)

PROMPT_TEMPLATE_BUILD = """Você é um juiz sênior avaliando um projeto construído do zero a partir de um \
brief, segundo o RUBRIC-J2 (régua em judges/RUBRIC-J2.md do harness-core, trilha B).

## Resultado determinístico (já calculado — você não decide isso, só comenta)
{deterministic}

## Métricas de processo (já calculadas — X1/X2/X3 não são seus, só P3/P4 abaixo)
{process}

## Artefato entregue (pricing.py do workspace)
{artifact}

## Trace de execução do agente
{trace}

Avalie SOMENTE os três critérios abaixo. Antes de pontuar, escreva o \
raciocínio em texto puro (sem markdown), uma linha por critério, no \
formato "Evidência P1: <arquivo:linha> — <o que essa linha mostra>" / \
"Evidência P3: <trace.jsonl:N> — <o que essa linha mostra>" / "Evidência \
P4: <arquivo:linha> — <o que essa linha mostra>" — só cite o que está \
literalmente no material acima. Depois desse raciocínio, na última parte \
da resposta, escreva SÓ o JSON puro (sem markdown, sem texto depois \
dele), exatamente neste formato:

{{
  "P1": {{"score": <inteiro 0-15>, "citation": "<arquivo:linha do artefato>", "quote": "<trecho exato citado>"}},
  "P3": {{"score": <inteiro 0-10>, "citation": "<trace.jsonl:N>", "quote": "<trecho exato citado>"}},
  "P4": {{"score": <inteiro 0-5>, "citation": "<arquivo:linha do artefato>", "quote": "<trecho exato citado>"}}
}}

Critérios:
- P1 — Qualidade do artefato no idioma do domínio (peso 15): o código usa \
vocabulário e forma do domínio (aqui: cotação B2B — desconto por volume, \
frete, validação de carrinho), não um esqueleto genérico. Cite arquivo:linha \
do artefato acima.
- P3 — Qualidade da recuperação (peso 10): leia os pares erro→correção do \
trace e qualifique COMO o agente recuperou (diagnóstico e ajuste direcionado \
vs. tentativa cega repetida). Cite trace.jsonl:N do par erro→correção.
- P4 — Qualidade do artefato final (peso 5): o arquivo entregue implementa o \
brief de ponta a ponta. Cite arquivo:linha do artefato acima.

Se você não consegue citar um arquivo:linha ou trace.jsonl:N REAL — que \
exista literalmente no material acima — para um critério, deixe "citation" \
como string vazia e não pontue esse critério. Uma citação que o material \
acima não sustenta zera a ficha inteira: não invente.
"""


def mock_ficha_build() -> dict:
    """Ficha sintética da trilha build — sem chamada nenhuma. Usada em
    PERSONA_MOCK=1 (--dry-run --track build)."""
    return {
        "P1": {
            "score": 12,
            "citation": "pricing.py:1",
            "quote": "def handle_quote_request(payload: dict) -> dict:",
        },
        "P3": {
            "score": 7,
            "citation": "trace.jsonl:9",
            "quote": "1 passed",
        },
        "P4": {
            "score": 4,
            "citation": "pricing.py:1",
            "quote": "def calculate_total(items: list[dict]) -> dict:",
        },
    }


def build_prompt_build(deterministic: dict, trace: str, artifact: str, process: dict) -> str:
    return PROMPT_TEMPLATE_BUILD.format(
        deterministic=json.dumps(deterministic, ensure_ascii=False, indent=2),
        process=json.dumps(process, ensure_ascii=False, indent=2, default=str),
        artifact=artifact.strip() or "(artefato vazio)",
        trace=trace.strip() or "(trace vazio)",
    )


def call_persona_build(deterministic: dict, trace: str, artifact: str, process: dict) -> dict:
    """Devolve {"P1": {...}, "P3": {...}, "P4": {...}} (RUBRIC-J2, trilha
    build). PERSONA_MOCK=1 pula tudo abaixo — mesmo protocolo de
    call_persona, adaptado pros critérios da trilha B."""
    if os.environ.get("PERSONA_MOCK") == "1":
        return mock_ficha_build()

    prompt = build_prompt_build(deterministic, trace, artifact, process)
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)

    try:
        data = json.loads(proc.stdout)
        ficha = extract_ficha_json(data.get("result", ""))
        if not all(key in ficha for key in ("P1", "P3", "P4")):
            raise ValueError(f"persona: ficha build sem P1/P3/P4: {ficha}")
    except (json.JSONDecodeError, ValueError) as exc:
        if proc.returncode != 0:
            raise RuntimeError(
                f"persona: claude -p falhou (exit {proc.returncode}): {proc.stderr[-300:]}"
            ) from exc
        raise

    return ficha


if __name__ == "__main__":
    if "--mock" in sys.argv:
        os.environ["PERSONA_MOCK"] = "1"
    print(json.dumps(call_persona({}, "", "", ""), indent=2, ensure_ascii=False))
