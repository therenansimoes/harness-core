#!/usr/bin/env python3
"""Testa os 3 mecanismos novos: allowlist de safety, tamper-check de
test_*.py em run_task.py e o log JSONL de decisões do evolve.py.

Sem chamada de API/subprocess `claude` — tudo local, síncrono.

    python3 -m pytest tests/test_safety_and_provenance.py -q
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import safety  # noqa: E402
from run_task import hash_test_files  # noqa: E402

# --------------------------------------------------------------- safety.py


def test_safe_run_bloqueia_python_dash_c():
    try:
        safety.safe_run(["python3", "-c", "print(1)"], ".")
    except safety.SafetyViolation:
        pass
    else:
        raise AssertionError("safe_run deveria bloquear 'python3 -c'")


def test_safe_run_bloqueia_binario_fora_da_allowlist():
    try:
        safety.safe_run(["rm", "-rf", "/"], ".")
    except safety.SafetyViolation:
        pass
    else:
        raise AssertionError("safe_run deveria bloquear binário fora da allowlist")


def test_safe_run_permite_pytest_exato():
    rc, _out, err = safety.safe_run(
        [sys.executable, "-m", "pytest", "--version"], str(REPO), timeout=20
    )
    assert rc == 0, f"pytest --version deveria rodar limpo: rc={rc} err={err}"


def test_claude_esta_na_allowlist():
    # não executa o binário (custaria API) — só confirma que a allowlist
    # inclui o backend usado por agent.py:_run_cli.
    assert "claude" in safety.ALLOWED_BINARIES


# ---------------------------------------------------------- tamper-check


def test_hash_test_files_detecta_edicao():
    task_dir = REPO / "tasks" / "task_03" / "fixtures"
    assert (task_dir / "test_estoque.py").exists(), "fixture do task_03 sumiu"

    ws = Path(tempfile.mkdtemp(prefix="tamper_test_"))
    try:
        shutil.copytree(task_dir, ws, dirs_exist_ok=True)

        before = hash_test_files(ws)
        assert "test_estoque.py" in before

        # simula o agent "passando" na prova editando o exame em vez de
        # resolver a tarefa.
        alvo = ws / "test_estoque.py"
        alvo.write_text(alvo.read_text() + "\n# tamper\n")

        after = hash_test_files(ws)
        assert after != before, "hash deveria mudar depois da edição do test_*.py"
        assert after["test_estoque.py"] != before["test_estoque.py"]

        # e o inverso: sem edição, hash é estável.
        stable = hash_test_files(ws)
        assert stable == after
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_hash_test_files_ignora_arquivos_nao_test():
    ws = Path(tempfile.mkdtemp(prefix="tamper_notest_"))
    try:
        (ws / "estoque.py").write_text("x = 1\n")
        (ws / "test_a.py").write_text("def test_a(): assert True\n")
        hashes = hash_test_files(ws)
        assert list(hashes) == ["test_a.py"]
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ------------------------------------------------------------- evolve.py


def _fake_agg(
    rate=1.0,
    rate_valid=1.0,
    trunc_rate=0.0,
    med_s=20.0,
    cost_run=0.05,
    tok_run=1500,
    n=5,
    n_valid=5,
    pass_=5,
):
    return {
        "rate": rate,
        "rate_valid": rate_valid,
        "trunc_rate": trunc_rate,
        "med_s": med_s,
        "cost_run": cost_run,
        "tok_run": tok_run,
        "n": n,
        "n_valid": n_valid,
        "pass": pass_,
    }


def _fake_rep(va="vA", vb="vB", merge=True):
    gates = [{"name": "success não caiu", "ok": merge}]
    return {
        "version_a": va,
        "version_b": vb,
        "a": _fake_agg(),
        "b": _fake_agg(rate=1.0 if merge else 0.4, pass_=5 if merge else 2),
        "d_med": -0.20 if merge else 0.30,
        "d_cost": -0.15 if merge else 0.25,
        "d_tok": -0.10,
        "gates": gates,
        "failed": [] if merge else ["success não caiu"],
        "imbalanced": False,
        "merge": merge,
    }


def _fake_meta(pid):
    return {
        "id": pid,
        "from_version": "vA",
        "to_version": "vB",
        "hypothesis": "teste sintético",
        "body": "",
        "path": f"{pid}.md",
    }


def test_write_decision_grava_jsonl_accept_e_reject():
    import evolve

    tmp = Path(tempfile.mkdtemp(prefix="evolve_jsonl_"))
    old_decisions, old_jsonl = evolve.DECISIONS, evolve.DECISIONS_JSONL
    try:
        evolve.DECISIONS = tmp / "decisions"
        evolve.DECISIONS_JSONL = tmp / "decisions.jsonl"
        evolve.DECISIONS.mkdir(parents=True)

        rep_accept = _fake_rep("vA", "vB", merge=True)
        evolve.write_decision(
            _fake_meta("pid_accept"),
            rep_accept,
            "merge",
            gid=1,
            diff_summary="MAX_TURNS 12->11",
            n_runs=5,
        )

        rep_reject = _fake_rep("vA", "vB", merge=False)
        evolve.write_decision(
            _fake_meta("pid_reject"),
            rep_reject,
            "discard",
            gid=2,
            diff_summary="MAX_TURNS 12->11",
            n_runs=5,
        )

        lines = evolve.DECISIONS_JSONL.read_text().strip().splitlines()
        assert len(lines) == 2, f"esperava 2 linhas, veio {len(lines)}"

        entry_accept = json.loads(lines[0])
        entry_reject = json.loads(lines[1])

        assert entry_accept["id"] == "pid_accept"
        assert entry_accept["accepted"] is True
        assert entry_accept["va"] == "vA" and entry_accept["vb"] == "vB"
        assert entry_accept["gates_failed"] == []
        assert isinstance(entry_accept["d_cost"], float)
        assert isinstance(entry_accept["d_med"], float)

        assert entry_reject["id"] == "pid_reject"
        assert entry_reject["accepted"] is False
        assert entry_reject["gates_failed"] == ["success não caiu"]
        assert entry_reject["reason"]
    finally:
        evolve.DECISIONS, evolve.DECISIONS_JSONL = old_decisions, old_jsonl
        shutil.rmtree(tmp, ignore_errors=True)
