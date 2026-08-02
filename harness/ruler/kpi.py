"""KPI por projeto: lê `kpis.toml` do repo-alvo e coleta números.

Formato (`<repo>/kpis.toml`):

    [kpi.linhas]
    cmd = "wc -l < src/main.py"
    direction = "lower"     # opcional, default "higher"
    timeout_s = 120         # opcional, default 60

`direction` não muda a coleta — só `regressed` sabe o que é "melhor". "higher"
= maior é melhor (testes verdes, cobertura); "lower" = menor é melhor (linhas,
tempo de build, warnings). Valor desconhecido cai em "higher" com aviso: chutar
sentido em silêncio vira gate invertido.

Contrato do comando: roda com o repo como cwd e imprime UM número (float
parseável) na ÚLTIMA linha do stdout. exit != 0, timeout ou parse falho => NaN.
NaN é "não medido" — nunca 0, que seria um KPI legítimo.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from math import isnan, nan
from pathlib import Path
from typing import Literal, Mapping

KPI_FILE = "kpis.toml"
DEFAULT_TIMEOUT_S = 60.0

Direction = Literal["higher", "lower"]
HIGHER, LOWER = "higher", "lower"
DEFAULT_DIRECTION = HIGHER


@dataclass(frozen=True)
class KpiSpec:
    """Um KPI declarado pelo alvo: como medir e para que lado é melhor."""

    name: str
    cmd: str
    direction: Direction = DEFAULT_DIRECTION
    timeout_s: float = DEFAULT_TIMEOUT_S


def load_kpis(repo: Path) -> dict[str, KpiSpec]:
    """Lê `<repo>/kpis.toml` -> {nome: KpiSpec}.

    Ausente, ilegível ou sem tabela `[kpi.*]` => {} — KPI é opcional e um alvo
    sem `kpis.toml` não pode quebrar a run. Entrada sem `cmd` é ignorada com
    aviso: silêncio aqui viraria KPI que "sumiu" sem explicação.
    """
    path = Path(repo) / KPI_FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"kpi: {path} inválido, ignorando — {exc}", file=sys.stderr)
        return {}

    out: dict[str, KpiSpec] = {}
    for name, spec in (data.get("kpi") or {}).items():
        if not isinstance(spec, dict) or not str(spec.get("cmd", "")).strip():
            print(f"kpi: [kpi.{name}] sem 'cmd', ignorado ({path})", file=sys.stderr)
            continue
        try:
            timeout_s = float(spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        direction = str(spec.get("direction", DEFAULT_DIRECTION)).strip().lower()
        if direction not in (HIGHER, LOWER):
            print(f"kpi: [kpi.{name}] direction={spec.get('direction')!r} desconhecido, "
                  f"usando {DEFAULT_DIRECTION!r} ({path})", file=sys.stderr)
            direction = DEFAULT_DIRECTION
        out[str(name)] = KpiSpec(
            name=str(name), cmd=str(spec["cmd"]), direction=direction, timeout_s=timeout_s
        )
    return out


def parse_value(stdout: str) -> float:
    """Última linha não-vazia do stdout como float; qualquer outra coisa = NaN."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return nan
    try:
        return float(lines[-1].strip())
    except ValueError:
        return nan


def run_kpi(spec: KpiSpec, repo: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> float:
    """Roda UM comando de KPI e devolve o número (ou NaN, com o motivo no stderr).

    `shell=True` é deliberado: o contrato do `kpis.toml` é um comando de shell
    (pipe/redirect é o caso comum). Não há superfície nova — quem escreve o
    `kpis.toml` já controla o código do alvo, e `verify_cmd` roda igual.
    `timeout_s` do chamador é teto global; o do spec é o limite daquele KPI.
    """
    limit = min(float(spec.timeout_s), float(timeout_s))
    try:
        proc = subprocess.run(
            spec.cmd, cwd=str(repo), shell=True, capture_output=True, text=True, timeout=limit
        )
    except subprocess.TimeoutExpired:
        print(f"kpi: {spec.name} estourou {limit}s, NaN", file=sys.stderr)
        return nan
    except OSError as exc:
        print(f"kpi: {spec.name} não executou ({exc}), NaN", file=sys.stderr)
        return nan
    if proc.returncode != 0:
        print(f"kpi: {spec.name} saiu {proc.returncode}, NaN", file=sys.stderr)
        return nan
    value = parse_value(proc.stdout)
    if isnan(value):
        print(f"kpi: {spec.name} não imprimiu número na última linha, NaN", file=sys.stderr)
    return value


def collect(repo: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, float]:
    """{nome: valor} para todo KPI declarado em `<repo>/kpis.toml`."""
    return {
        name: run_kpi(spec, repo, timeout_s) for name, spec in load_kpis(repo).items()
    }


def regressed(
    before: Mapping[str, float],
    after: Mapping[str, float],
    specs: Mapping[str, KpiSpec] | None = None,
) -> list[str]:
    """Nomes dos KPIs que pioraram de `before` para `after`. `[]` = sem regressão.

    - nome ausente em `before` (ou em `after`): ignorado — não há linha de base,
      e tratar ausência como zero fabricaria regressão;
    - `before` NaN: ignorado, a medição de base falhou;
    - `after` NaN com `before` medido: REGRESSÃO. NaN nunca conta como melhora,
      e perder a medição depois da mudança é o jeito mais barato de burlar o
      gate — quem quebra o comando do KPI não passa;
    - sem entrada em `specs`: direction default "higher".

    Comparação é estrita (igual não regride) e sem limiar de ruído: aqui os dois
    lados vêm do mesmo workspace, medidos pelo mesmo comando.
    """
    specs = specs or {}
    out: list[str] = []
    for name in sorted(set(before) & set(after)):
        old, new = float(before[name]), float(after[name])
        if isnan(old):
            continue
        if isnan(new):
            out.append(name)
            continue
        spec = specs.get(name)
        direction = spec.direction if spec is not None else DEFAULT_DIRECTION
        worse = new < old if direction == HIGHER else new > old
        if worse:
            out.append(name)
    return out
