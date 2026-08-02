#!/usr/bin/env python3
"""kpi.py — KPI por projeto: lê `.harness/kpi.toml` do alvo e coleta números.

Formato (`.harness/kpi.toml`, no repo-alvo):

    [kpi.linhas]
    cmd = "wc -l < src/main.py"

    [kpi.testes_verdes]
    cmd = "python3 -m pytest -q 2>&1 | tail -1 | grep -oE '[0-9]+ passed' | cut -d' ' -f1"
    timeout_s = 120        # opcional, default 60

Contrato do comando: roda com o workspace do alvo como cwd e imprime UM número
(float parseável) na ÚLTIMA linha do stdout. exit != 0, timeout ou parse falho
=> valor `nan`. nan significa "não medido" — nunca 0, que seria um KPI legítimo.

O resultado vai para o `results.tsv` como UMA coluna `kpis` com JSON compacto
({"nome": valor}), para o schema do TSV não crescer a cada KPI novo.

CLI:
    python3 kpi.py <path>      # imprime o JSON coletado naquele diretório
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from math import nan
from pathlib import Path

DEFAULT_TIMEOUT_S = 60


def load_kpis(repo_path) -> dict[str, dict]:
    """Lê `<repo_path>/.harness/kpi.toml` -> {nome: {cmd, timeout_s}}.

    Ausente, ilegível ou sem tabela `[kpi.*]` => {} (KPI é opcional; um alvo
    sem kpi.toml não pode quebrar a run). Entrada sem `cmd` é ignorada com
    aviso no stderr — silêncio aqui viraria KPI que "sumiu" sem explicação."""
    path = Path(repo_path) / ".harness" / "kpi.toml"
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"kpi: {path} inválido, ignorando — {e}", file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    for name, spec in (data.get("kpi") or {}).items():
        if not isinstance(spec, dict) or not str(spec.get("cmd", "")).strip():
            print(f"kpi: [kpi.{name}] sem 'cmd', ignorado ({path})", file=sys.stderr)
            continue
        try:
            timeout_s = float(spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = float(DEFAULT_TIMEOUT_S)
        out[str(name)] = {"cmd": str(spec["cmd"]), "timeout_s": timeout_s}
    return out


def parse_value(stdout: str) -> float:
    """Última linha não-vazia do stdout como float; qualquer outra coisa = nan."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return nan
    try:
        return float(lines[-1].strip())
    except ValueError:
        return nan


def run_kpi(cmd: str, cwd, timeout_s: float = DEFAULT_TIMEOUT_S) -> float:
    """Roda UM comando de KPI e devolve o número (ou nan).

    shell=True é deliberado: o contrato do kpi.toml é um comando de shell
    (pipe/redirect é o caso comum, ver o cabeçalho). O nível de confiança é o
    mesmo do `verify.py` do alvo, que run_task.py já executa direto — não há
    superfície nova: quem escreve o kpi.toml já controla o código do alvo.
    Por isso o allowlist de safety.py (que gate*ia argv de lista, nunca shell)
    não se aplica aqui."""
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return nan
    if p.returncode != 0:
        return nan
    return parse_value(p.stdout)


def collect(repo_path) -> dict[str, float]:
    """{nome: valor} para todo KPI declarado em `<repo_path>/.harness/kpi.toml`."""
    return {
        name: run_kpi(spec["cmd"], repo_path, spec["timeout_s"])
        for name, spec in load_kpis(repo_path).items()
    }


def to_json(values: dict[str, float]) -> str:
    """JSON compacto para a coluna `kpis` do results.tsv. Sem espaço e sem
    quebra de linha — a coluna é um campo TSV. nan sai como o literal `NaN`
    (json.loads do Python lê de volta como float('nan'))."""
    return json.dumps(values, separators=(",", ":"), sort_keys=True)


def main(argv: list[str]) -> int:
    path = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    if not path.is_dir():
        print(f"kpi: {path} não é um diretório", file=sys.stderr)
        return 2
    print(to_json(collect(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
