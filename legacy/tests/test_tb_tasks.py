#!/usr/bin/env python3
"""Testa a suite `tb` (benchmarks/tb/task_tb_*) — tasks portadas do
Terminal-Bench 2.0 (item 6 do NIGHT-PLAN.md).

Para cada task tb: verify.py deve falhar (exit != 0) contra a fixture crua e
passar (exit 0) depois de aplicar a solução de referência do dataset original
(tests/fixtures/tb_solutions/<task_id>/). Tudo offline, sem chamada a agente.

    python3 -m pytest tests/test_tb_tasks.py -q
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TB_ROOT = REPO / "benchmarks" / "tb"
SOLUTIONS_ROOT = Path(__file__).resolve().parent / "fixtures" / "tb_solutions"

TASK_IDS = sorted(p.name for p in TB_ROOT.glob("task_tb_*") if (p / "prompt.md").exists())


def _make_workspace(task_id: str, tmp_path: Path) -> Path:
    ws = tmp_path / task_id
    ws.mkdir()
    fixtures = TB_ROOT / task_id / "fixtures"
    if fixtures.is_dir():
        shutil.copytree(fixtures, ws, dirs_exist_ok=True)
    return ws


def _run_verify(task_id: str, ws: Path) -> subprocess.CompletedProcess:
    verify = TB_ROOT / task_id / "verify.py"
    return subprocess.run(
        [sys.executable, str(verify)], cwd=ws, capture_output=True, text=True, timeout=60
    )


def _apply_solution(task_id: str, ws: Path) -> None:
    """Aplica a solução de referência de tests/fixtures/tb_solutions/<task_id>/
    no workspace. solve.py é executado (produz o artefato via lógica própria);
    qualquer outro arquivo é copiado direto (ex.: regex.txt, run.py prontos)."""
    sol_dir = SOLUTIONS_ROOT / task_id
    assert sol_dir.is_dir(), f"sem solução de referência para {task_id}: {sol_dir}"
    solve_script = sol_dir / "solve.py"
    if solve_script.exists():
        r = subprocess.run(
            [sys.executable, str(solve_script)], cwd=ws, capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, f"solve.py de {task_id} falhou: {r.stdout}\n{r.stderr}"
    for f in sol_dir.iterdir():
        if f.name == "solve.py":
            continue
        shutil.copy(f, ws / f.name)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_tb_task_red_sem_solucao(task_id, tmp_path):
    """verify.py falha contra a fixture crua (sem a task resolvida)."""
    ws = _make_workspace(task_id, tmp_path)
    r = _run_verify(task_id, ws)
    assert r.returncode != 0, f"{task_id}: verify passou sem solução (deveria falhar)"


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_tb_task_green_com_solucao_referencia(task_id, tmp_path):
    """verify.py passa depois de aplicar a solução de referência do dataset original."""
    ws = _make_workspace(task_id, tmp_path)
    _apply_solution(task_id, ws)
    r = _run_verify(task_id, ws)
    assert r.returncode == 0, (
        f"{task_id}: verify falhou com a solução de referência: {r.stdout}\n{r.stderr}"
    )


def test_tb_suite_tem_tasks():
    """Sanidade: a suite tb não está vazia (evita passar 'verde' com 0 tasks coletadas)."""
    assert TASK_IDS, "nenhuma task_tb_* encontrada em benchmarks/tb"
