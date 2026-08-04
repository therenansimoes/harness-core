"""Prova selada: o verificador não existe no workspace enquanto o agente roda.

O que estava vazando: o seed copiava o `verify.py` da unidade antes do execute,
então o agente podia ler o verificador e recomputar o golden.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from harness import cli
from harness.backends import registry
from harness.graph.run_graph import run_unit
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

# verify.py que exige o arquivo do agente e nada mais: o assert do teste é sobre
# visibilidade, não sobre a dificuldade da tarefa.
VERIFY_PY = """\
import sys
from pathlib import Path
sys.exit(0 if Path("resposta.txt").read_text().strip() == "42" else 1)
"""


class PeekBackend:
    """Anota o que o agente vê no workspace e entrega a resposta certa.

    O peek é o teste: se `verify.py` aparecer nessa lista, o exame está aberto.
    """

    name: ClassVar[str] = "peek"

    def __init__(self, seen: list[list[str]]) -> None:
        self.seen = seen

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        ws = req.workspace
        ws.mkdir(parents=True, exist_ok=True)
        self.seen.append(sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()))
        (ws / "resposta.txt").write_text("42\n", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=("resposta.txt",),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def peek(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    seen: list[list[str]] = []
    registry.register("peek", lambda: PeekBackend(seen))
    yield seen
    registry.unregister("peek")


def _unit(tmp_path: Path) -> Path:
    """Unidade com verificador e fixture: `fixtures/` é insumo do agente."""
    unit = tmp_path / "u_sealed"
    (unit / "fixtures").mkdir(parents=True)
    (unit / "unit.toml").write_text(
        'id = "u_sealed"\nprompt = "x"\n'
        'verify_cmd = "cp -f fixtures/dado.txt dado.txt && python3 verify.py"\n',
        encoding="utf-8",
    )
    (unit / "verify.py").write_text(VERIFY_PY, encoding="utf-8")
    (unit / "fixtures" / "dado.txt").write_text("42\n", encoding="utf-8")
    return unit


def test_agente_nao_ve_verify_py_mas_o_verify_passa(tmp_path, peek):
    unit = _unit(tmp_path)

    final = run_unit(unit, "peek", None, tmp_path / "data", thread_id="t-sealed")

    assert peek == [["fixtures/dado.txt"]]  # o agente viu a fixture, não a prova
    assert final["verdict"].passed is True  # e o verify ainda rodou `python3 verify.py`
    # Nem depois: o retry da tentativa seguinte também rodaria às cegas.
    assert not (Path(final["workspace"]) / "verify.py").exists()


def test_seed_workspace_retem_o_verificador(tmp_path):
    unit = cli.load_unit(_unit(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()

    assert cli.seed_workspace(unit, ws) == ["fixtures/dado.txt"]
    assert not (ws / "verify.py").exists()
