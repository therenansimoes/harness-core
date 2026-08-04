"""Régua graduada (`[checks]` do unit.toml) e trace persistido.

O contrato testado: (1) unidade sem `[checks]` produz o MESMO Verdict de antes,
campo a campo — a régua graduada é aditiva ou não é nada; (2) o score é a fração
do peso que passou, contando o `verify_cmd` como check implícito de peso 1.0;
(3) todos os checks rodam, sempre, mesmo depois do primeiro vermelho (sem isso
"quanto passou" não existe); (4) o hint da tentativa seguinte cita NOMES de
check e nunca o texto do log selado; (5) o trace da tentativa sobrevive ao
workspace.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.cli import load_unit
from harness.graph import reflect
from harness.graph.run_graph import _save_trace, run_unit
from harness.ledger import store
from harness.ruler.verify import CHECKS_EXIT, run_extra_checks, run_verify
from harness.types import Capabilities, Check, ExecRequest, ExecResult, Preflight, UnitSpec, Verdict

TRACE_LINE = '{"turn": 1, "tool": "write"}\n'


class TraceBackend:
    """Escreve o trace que o backend real escreveria — é o que se persiste."""

    name: ClassVar[str] = "tracer"

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
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / "feito.txt").write_text("x", encoding="utf-8")
        req.trace_path.write_text(TRACE_LINE, encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=("feito.txt",),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def tracer():
    registry.register("tracer", TraceBackend)
    yield
    registry.unregister("tracer")


def _unit(cmd: str, *checks: Check) -> UnitSpec:
    return UnitSpec(id="u1", path=Path("."), prompt="faça", verify_cmd=cmd, checks=tuple(checks))


# --- (1) aditivo: sem [checks], nada muda -------------------------------------


@pytest.mark.parametrize("cmd, code", [("true", 0), ("exit 3", 3)])
def test_sem_checks_o_verdict_e_o_de_antes(tmp_path, cmd, code):
    v = run_verify(_unit(cmd), tmp_path)
    # `sec` é o único campo não determinístico; o resto tem que bater com o
    # Verdict de 4 campos que o resto do harness sempre construiu.
    assert replace(v, sec=0.0) == Verdict(
        passed=code == 0, exit_code=code, log_path=v.log_path, sec=0.0
    )
    assert v.score == 1.0
    assert v.failed == ()


def test_unit_toml_sem_checks_nao_tem_checks(tmp_path):
    (tmp_path / "unit.toml").write_text(
        'id = "u"\nprompt = "x"\nverify_cmd = "test -f a.txt"\n', encoding="utf-8"
    )
    assert load_unit(tmp_path).checks == ()


# --- (2) o score é o peso que passou ------------------------------------------


def test_score_pondera_pelo_peso(tmp_path):
    # verify_cmd (implícito, peso 1) passa; um check de peso 1 passa e o de peso
    # 2 falha => 2 de 4 => 0.5.
    v = run_verify(
        _unit(
            "true",
            Check(name="leve", cmd="exit 0", weight=1.0),
            Check(name="pesado", cmd="exit 1", weight=2.0),
        ),
        tmp_path,
    )
    assert v.score == pytest.approx(0.5)
    assert v.passed is False
    assert v.failed == ("pesado",)
    # `verify_cmd` verde derrubado por subcheck tem exit code próprio: sem isso o
    # gate leria 0 e aceitaria.
    assert v.exit_code == CHECKS_EXIT
    log = v.log_path.read_text(encoding="utf-8")
    assert "=== pesado exit=1 ===" in log


def test_todos_verdes_e_score_cheio(tmp_path):
    v = run_verify(_unit("true", Check(name="ok", cmd="exit 0", weight=3.0)), tmp_path)
    assert v.score == 1.0
    assert v.passed is True
    assert v.exit_code == 0


def test_verify_vermelho_conta_no_score(tmp_path):
    # O comando principal reprovou, mas os checks rodam: 1 de 2 é diagnóstico,
    # "reprovou" não é.
    v = run_verify(_unit("exit 2", Check(name="parcial", cmd="exit 0")), tmp_path)
    assert v.score == pytest.approx(0.5)
    assert v.exit_code == 2, "exit code do comando principal não é sobrescrito"
    assert v.failed == ()


# --- (3) roda TODOS, em ordem -------------------------------------------------


def test_primeiro_vermelho_nao_interrompe_os_demais(tmp_path):
    checks = (
        Check(name="a", cmd="touch sentinela_a; exit 1"),
        Check(name="b", cmd="touch sentinela_b; exit 1"),
        Check(name="c", cmd="touch sentinela_c"),
    )
    score, failed, log = run_extra_checks(checks, tmp_path, budget_s=30.0)
    for name in "abc":
        assert (tmp_path / f"sentinela_{name}").is_file(), f"check {name} não rodou"
    assert failed == ("a", "b")
    assert score == pytest.approx(1 / 3)
    assert log.index("=== a") < log.index("=== b") < log.index("=== c")


def test_orcamento_estourado_reprova_o_que_nao_rodou(tmp_path):
    checks = (
        Check(name="lento", cmd="sleep 1"),
        Check(name="nunca", cmd="touch sentinela_nunca"),
    )
    score, failed, log = run_extra_checks(checks, tmp_path, per_check_timeout_s=0.2, budget_s=0.2)
    assert not (tmp_path / "sentinela_nunca").is_file()
    assert failed == ("lento", "nunca")
    assert score == 0.0
    assert "orçamento estourado, 1 não rodaram" in log


# --- (4) hint: nomes sim, log selado nunca ------------------------------------


def test_hint_cita_nomes_e_nao_o_golden():
    golden = "esperado=GABARITO_SECRETO"
    state = {
        "unit": SimpleNamespace(verify_cmd="test -f exigido.txt", kind=None),
        "exec": SimpleNamespace(files_changed=("outro.txt",)),
        "events": [
            {
                "node": "verify",
                "tail": golden,
                "exit_code": CHECKS_EXIT,
                "score": 0.6,
                "failed": ["css_min", "a11y"],
            }
        ],
    }
    hint = reflect.build_hint(state)
    assert "checks reprovados: css_min, a11y (score 0.60)" in hint
    assert "GABARITO_SECRETO" not in hint


def test_hint_sem_checks_nao_ganha_linha():
    state = {
        "unit": SimpleNamespace(verify_cmd="test -f exigido.txt", kind=None),
        "exec": SimpleNamespace(files_changed=("outro.txt",)),
        "events": [{"node": "verify", "tail": "x", "exit_code": 1}],
    }
    assert "checks reprovados" not in reflect.build_hint(state)


# --- caminho do grafo ---------------------------------------------------------


def test_grafo_reprova_por_subcheck_com_verify_verde(data_dir, tmp_path, tracer):
    unit = tmp_path / "graded"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "graded"\nkind = "code"\nprompt = "x"\n'
        'verify_cmd = "test -f feito.txt"\n'
        "[checks.tem_conteudo]\n"
        'cmd = "grep -q ausente feito.txt"\n'
        "weight = 2.0\n",
        encoding="utf-8",
    )
    final = run_unit(unit, "tracer", None, data_dir, thread_id="t-graded", max_attempts=1)

    assert final["verdict"].passed is False
    assert final["verdict"].exit_code == CHECKS_EXIT
    assert final["verdict"].score == pytest.approx(1 / 3)
    assert final["verdict"].failed == ("tem_conteudo",)
    saved = store.get_node("t-graded", "verify", data_dir / store.DB_NAME)
    assert saved["score"] == pytest.approx(1 / 3)
    assert saved["failed"] == ["tem_conteudo"]


# --- (5) trace persistido -----------------------------------------------------


def test_trace_sobrevive_ao_workspace(data_dir, tmp_path, tracer):
    unit = tmp_path / "tr"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "tr"\nkind = "code"\nprompt = "x"\nverify_cmd = "test -f feito.txt"\n',
        encoding="utf-8",
    )
    final = run_unit(unit, "tracer", None, data_dir, thread_id="t-trace-save")
    shutil.rmtree(final["workspace"])  # o tmpdir do run vai embora; o trace, não

    saved = (data_dir / "logs" / "t-trace-save" / "trace.0.jsonl").resolve()
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == TRACE_LINE
    assert (
        store.get_node("t-trace-save", "execute", data_dir / store.DB_NAME)["trace_saved"] is True
    )


def test_trace_ausente_nao_levanta(data_dir, tmp_path):
    assert _save_trace(tmp_path / "sumiu" / "trace.jsonl", "r-x", 0, data_dir) is False
