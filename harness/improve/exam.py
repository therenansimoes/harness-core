"""Exame selado: roda TODAS as unidades de `benchmarks/sealed/*/unit.toml`.

Fail-closed: sealed sem unidades → False (sem exame não se aprova nada);
qualquer exceção → False. `True` só quando toda unidade termina em `accept`.
O loop nunca escreve em `benchmarks/sealed/` — seedar/promover é ato humano.
"""

from __future__ import annotations

import sys
import tomllib
import uuid
from pathlib import Path

# Mesma convenção cwd-relativa de synthesize.SEALED_DIR.
SEALED_DIR = Path("benchmarks/sealed")

# Qual executor faz a prova. O knob vive em `config/ruler.toml` porque esse
# arquivo é guardado pelo meta-exame (harness/improve/meta.py): trocar o
# backend do exame é mexer na própria régua, então exige exame + ack humano.
# Ausente/malformado => mock, exatamente o comportamento histórico (custo $0).
EXAM_CONFIG = Path(__file__).resolve().parents[2] / "config" / "ruler.toml"
MOCK_BACKEND = "mock"
DEFAULT_MODEL = ""

# Chave opcional de `unit.toml`: unidade que só um executor de verdade resolve
# (o mock só escreve mock_output.txt). Sob o exame mock ela fica FORA do
# relatório em vez de reprovar tudo — o exame mock é smoke, não a prova real.
REQUIRES_REAL_KEY = "requires_real_backend"


def _section_backend(data: dict, name: str) -> tuple[str, str] | None:
    """(backend, model) da seção `name`; `None` quando a seção não decide nada
    (ausente, não-tabela, ou `backend` ausente/vazio/não-string)."""
    section = data.get(name)
    if not isinstance(section, dict):
        return None
    backend, model = section.get("backend"), section.get("model")
    if not isinstance(backend, str) or not backend:
        return None
    return backend, model if isinstance(model, str) else DEFAULT_MODEL


def _load_config(config_path: Path | None) -> dict:
    """Toml de `config/ruler.toml`; qualquer falha de leitura vira dict vazio."""
    path = EXAM_CONFIG if config_path is None else config_path
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def exam_backend(config_path: Path | None = None) -> tuple[str, str]:
    """(backend, model) da seção `[exam]`; qualquer falha devolve o mock.

    `config_path` só existe para teste; produção lê `config/ruler.toml`.
    """
    return _section_backend(_load_config(config_path), "exam") or (
        MOCK_BACKEND,
        DEFAULT_MODEL,
    )


def frontier_backend(config_path: Path | None = None) -> tuple[str, str]:
    """(backend, model) do screening da fronteira (harness/improve/coevolve.py).

    `[frontier]` quando existe, senão `[exam]`, senão mock. O fallback é de
    propósito: screenar a quarentena com um executor mais fraco que o do exame
    marcaria como "na fronteira" candidato que a prova real passa.
    """
    data = _load_config(config_path)
    return (
        _section_backend(data, "frontier")
        or _section_backend(data, "exam")
        or (MOCK_BACKEND, DEFAULT_MODEL)
    )


def _discover(sealed_dir: Path) -> list[Path]:
    """Dirs de unidade = subdirs com `unit.toml`. Ordenado = determinístico."""
    return sorted(p.parent for p in sealed_dir.glob("*/unit.toml"))


def _requires_real_backend(unit_dir: Path) -> bool:
    """Lê `requires_real_backend` do `unit.toml`. Toml torto => False.

    False no erro é de propósito: unidade ilegível entra no exame e reprova
    via `run_unit` (fail-closed), em vez de sair de fininho do relatório.
    """
    try:
        data = tomllib.loads((unit_dir / "unit.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return bool(data.get(REQUIRES_REAL_KEY))


def exam_report(
    backend: str | None = None,
    model: str | None = None,
    sealed_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    config_path: Path | None = None,
) -> list[dict]:
    """Uma linha por unidade: {"id": str, "passed": bool} — pro humano ver.

    `backend`/`model` explícitos vencem o config; `None` lê `[exam]` do
    `config/ruler.toml` (default: mock).
    Exceção numa unidade vira `passed=False` daquela unidade; exceção de
    descoberta sobe (o chamador `run_sealed_exam` traduz em False).
    """
    # Import tardio: espelha run_graph↔cli e mantém o módulo leve de importar.
    from harness.graph.run_graph import run_unit
    from harness.ledger.store import data_dir as default_data_dir
    from harness.memory import episodic

    cfg_backend, cfg_model = exam_backend(config_path)
    backend = backend or cfg_backend
    model = model or cfg_model

    sealed = Path(sealed_dir) if sealed_dir is not None else SEALED_DIR
    data = Path(data_dir) if data_dir is not None else default_data_dir()

    report: list[dict] = []
    # Exame não grava nem lê memória episódica — o verificador selado imprime o
    # gabarito ("esperado=..."), e a memória global vazaria isso pro executor em
    # prompts futuros do mesmo kind. O juiz não alimenta a memória do avaliado.
    with episodic.disabled():
        for unit_dir in _discover(sealed):
            unit_id = unit_dir.name
            if backend == MOCK_BACKEND and _requires_real_backend(unit_dir):
                print(f"exam: {unit_id} exige backend real, fora do exame mock", file=sys.stderr)
                continue
            # thread_id único: exame nunca retoma run velho por engano.
            thread_id = f"exam-{unit_id}-{uuid.uuid4().hex[:8]}"
            try:
                state = run_unit(unit_dir, backend, model or None, data, thread_id)
                decision = state.get("decision")
                passed = bool(decision is not None and decision.action == "accept")
            except Exception:
                passed = False
            report.append({"id": unit_id, "passed": passed})
    return report


def run_sealed_exam(
    backend: str | None = None,
    model: str | None = None,
    sealed_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    config_path: Path | None = None,
) -> bool:
    """True SÓ se todas as unidades seladas aceitam. Vazio ou erro → False."""
    try:
        report = exam_report(
            backend=backend,
            model=model,
            sealed_dir=sealed_dir,
            data_dir=data_dir,
            config_path=config_path,
        )
    except Exception as exc:
        print(f"exam: erro no exame selado, reprovando (fail-closed): {exc}", file=sys.stderr)
        return False
    if not report:
        print("exam: benchmarks/sealed sem unidades — fail-closed, nada aprovado", file=sys.stderr)
        return False
    return all(r["passed"] for r in report)
