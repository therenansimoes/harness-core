#!/usr/bin/env python3
"""Testa project.py (SPEC-MULTIPROJECT FASE 1) — os 7 critérios de aceite.

Roda inteiro sobre projects/, work_paths e um DB do graph temporários
(HARNESS_PROJECTS_ROOT / HARNESS_WS_ROOT / HARNESS_GRAPH) — nunca toca no
repo real. O agente é MOCKADO via HARNESS_MOCK_AGENT=1 (ver project._mock_agent):
zero chamada de API/rede/custo, inclusive nos testes de paralelismo real
(subprocess x2), que usam o mesmo env.

    python3 -m pytest tests/test_project.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="project_test_"))

# Ambiente isolado ANTES de importar project.py (lê env no import p/ definir
# PROJECTS_ROOT/WS_ROOT). O mesmo env é herdado pelos subprocessos que os
# testes de paralelismo real disparam (aceites 2, 3, 4) — sem isso os testes
# de dois-processos-simultâneos chamariam o backend real.
os.environ["HARNESS_PROJECTS_ROOT"] = str(TMP / "projects")
os.environ["HARNESS_WS_ROOT"] = str(TMP / "ws")
os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
os.environ["HARNESS_MOCK_AGENT"] = "1"
sys.path.insert(0, str(REPO))

import project  # noqa: E402

PROJECT_PY = str(REPO / "project.py")


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(TMP, ignore_errors=True)


def run_cli(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Sobe project.py como PROCESSO REAL (não chamada in-process) — é o que
    prova paralelismo/lock de verdade nos aceites 2/3/4. Herda o env do
    processo de teste (HARNESS_MOCK_AGENT=1 incluso)."""
    return subprocess.run(
        [sys.executable, PROJECT_PY, *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


DEFAULT_VERIFY = (
    "import pathlib, sys\nsys.exit(0 if pathlib.Path('AGENT_OUTPUT.txt').exists() else 1)\n"
)
ALWAYS_FAIL_VERIFY = "import sys\nsys.exit(1)\n"


def _mkworkpath(name: str, extra_files: dict[str, str] | None = None) -> Path:
    wp = TMP / "workpaths" / name
    wp.mkdir(parents=True, exist_ok=True)
    (wp / "original.txt").write_text("original\n")
    for fname, content in (extra_files or {}).items():
        (wp / fname).write_text(content)
    return wp


def _mkfile(name: str, content: str) -> Path:
    p = TMP / "srcs" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _add_project(name: str, priority: int = 1, extra_files=None) -> Path:
    wp = _mkworkpath(f"wp_{name}", extra_files)
    r = run_cli(["add", name, "--path", str(wp), "--priority", str(priority)])
    assert r.returncode == 0, r.stdout + r.stderr
    return wp


def _enqueue(
    name: str, title: str, prompt_text: str, verify_text: str = DEFAULT_VERIFY, priority: int = 1
) -> str:
    prompt_f = _mkfile(f"{name}_{title}_prompt.md", prompt_text)
    verify_f = _mkfile(f"{name}_{title}_verify.py", verify_text)
    r = run_cli(
        [
            "queue",
            name,
            "add",
            title,
            "--prompt",
            str(prompt_f),
            "--verify",
            str(verify_f),
            "--priority",
            str(priority),
        ]
    )
    assert r.returncode == 0, r.stdout + r.stderr
    rows = project.read_queue(project.PROJECTS_ROOT / name)
    return rows[-1]["id"]


# --------------------------------------------------------------------------- 1


def test_aceite1_add_e_list_mostra_projetos_zero_pendencias():
    _add_project("a1_p1", priority=5)
    _add_project("a1_p2", priority=1)

    r = run_cli(["list"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "a1_p1" in r.stdout
    assert "a1_p2" in r.stdout
    assert r.stdout.count("pending=0") >= 2


# --------------------------------------------------------------------------- 2


def test_aceite2_dois_run_once_em_paralelo_projetos_diferentes():
    _add_project("a2_p1")
    _add_project("a2_p2")
    _enqueue("a2_p1", "u1", "faça algo\n")
    _enqueue("a2_p2", "u1", "faça algo\n")

    proc1 = subprocess.Popen(
        [sys.executable, PROJECT_PY, "run", "--once", "--project", "a2_p1"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc2 = subprocess.Popen(
        [sys.executable, PROJECT_PY, "run", "--once", "--project", "a2_p2"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out1, err1 = proc1.communicate(timeout=30)
    out2, err2 = proc2.communicate(timeout=30)

    assert proc1.returncode == 0, out1 + err1
    assert proc2.returncode == 0, out2 + err2
    assert "[PASS]" in out1, out1
    assert "[PASS]" in out2, out2

    for name in ("a2_p1", "a2_p2"):
        results = (project.PROJECTS_ROOT / name / "results.tsv").read_text().splitlines()
        assert len(results) == 2  # header + 1 linha

    ws_root = project.WS_ROOT
    leftover = (
        [p for p in ws_root.iterdir() if p.name.startswith("a2_")] if ws_root.is_dir() else []
    )
    assert leftover == [], f"workspace remanescente: {leftover}"


# --------------------------------------------------------------------------- 3


def test_aceite3_dois_run_once_mesmo_projeto_so_um_executa():
    _add_project("a3_p1")
    _enqueue("a3_p1", "u1", "faça algo\nMOCK_SLEEP: 0.8\n")
    proj_dir = project.PROJECTS_ROOT / "a3_p1"

    proc1 = subprocess.Popen(
        [sys.executable, PROJECT_PY, "run", "--once", "--project", "a3_p1"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Espera proc1 ter de fato reivindicado a unidade (claimed_at preenchido)
    # antes de disparar o segundo processo — assim a corrida pelo lock é
    # determinística, não dependente de sorte de agendamento do SO.
    deadline = time.time() + 5
    claimed = False
    while time.time() < deadline:
        rows = project.read_queue(proj_dir)
        if rows and rows[0]["claimed_at"]:
            claimed = True
            break
        time.sleep(0.02)
    assert claimed, "proc1 nunca reivindicou a unidade — corrida não pôde ser testada"

    proc2 = subprocess.Popen(
        [sys.executable, PROJECT_PY, "run", "--once", "--project", "a3_p1"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out1, err1 = proc1.communicate(timeout=30)
    out2, err2 = proc2.communicate(timeout=30)

    assert proc1.returncode == 0, out1 + err1
    assert proc2.returncode == 0, out2 + err2

    outs = [out1, out2]
    ran = [o for o in outs if "[PASS]" in o or "[FAIL]" in o]
    locked = [o for o in outs if "[lock]" in o]
    assert len(ran) == 1, outs
    assert len(locked) == 1, outs

    results = (proj_dir / "results.tsv").read_text().splitlines()
    assert len(results) == 2  # header + exatamente 1 execução


# --------------------------------------------------------------------------- 4


def test_aceite4_run_loop_respeita_prioridade_ponderada():
    _add_project("a4_hi", priority=3)
    _add_project("a4_lo", priority=1)
    for i in range(3):
        _enqueue("a4_hi", f"u{i}", "faça algo\n", priority=3)
    _enqueue("a4_lo", "u0", "faça algo\n", priority=1)

    r = run_cli(["run", "--loop", "4"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "run: 4 unidade" in r.stdout, r.stdout

    hi_dir = project.PROJECTS_ROOT / "a4_hi"
    lo_dir = project.PROJECTS_ROOT / "a4_lo"
    hi_rows = project.read_queue(hi_dir)
    lo_rows = project.read_queue(lo_dir)
    assert all(row["state"] == "done" for row in hi_rows)
    assert all(row["state"] == "done" for row in lo_rows)

    hi_results = hi_dir.joinpath("results.tsv").read_text().splitlines()[1:]
    lo_results = lo_dir.joinpath("results.tsv").read_text().splitlines()[1:]
    assert len(hi_results) == 3
    assert len(lo_results) == 1

    # scheduler prioriza -priority: enquanto a4_hi (priority=3) tem pending,
    # a4_lo (priority=1) nunca é escolhido — logo o timestamp da única linha
    # de a4_lo é >= o timestamp de TODAS as linhas de a4_hi.
    hi_ts = sorted(line.split("\t")[0] for line in hi_results)
    lo_ts = lo_results[0].split("\t")[0]
    assert lo_ts >= hi_ts[-1], (hi_ts, lo_ts)


# --------------------------------------------------------------------------- 5


def test_aceite5_agente_edita_verify_gera_tamper():
    _add_project("a5_p1")
    proj_dir = project.PROJECTS_ROOT / "a5_p1"
    # o próximo id enfileirado neste projeto novo é sempre "0001" (1a unidade).
    verify_target = proj_dir / "verify" / "0001.py"
    prompt = f"faça algo\nMOCK_TAMPER: {verify_target}\n"
    unit_id = _enqueue("a5_p1", "u1", prompt)
    assert unit_id == "0001"

    r = run_cli(["run", "--once", "--project", "a5_p1"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[FAIL]" in r.stdout
    assert "tamper:" in r.stdout

    results = proj_dir.joinpath("results.tsv").read_text().splitlines()
    header, row = results[0].split("\t"), results[1].split("\t")
    row_map = dict(zip(header, row, strict=False))
    assert row_map["success"] == "0"
    assert "tamper:" in row_map["notes"]

    rows = project.read_queue(proj_dir)
    assert rows[0]["state"] == "failed"


# --------------------------------------------------------------------------- 6 (meta)

# O critério 6 ("pytest tests/test_project.py verde, sem rede/pagas, agente
# mockado") é o próprio arquivo: todo teste acima roda com HARNESS_MOCK_AGENT=1
# (setado no import, no topo do módulo) e nenhum teste chama agent.run_agent
# real nem toca projects/ do repo. `python3 -m pytest tests/test_project.py -q`
# passar é a prova.


# --------------------------------------------------------------------------- 7


def test_aceite7_work_path_intacto_quando_verify_falha():
    wp = _add_project("a7_p1")
    _enqueue("a7_p1", "u1", "faça algo\n", verify_text=ALWAYS_FAIL_VERIFY)

    before = sorted(p.name for p in wp.iterdir())

    r = run_cli(["run", "--once", "--project", "a7_p1"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[FAIL]" in r.stdout

    after = sorted(p.name for p in wp.iterdir())
    assert before == after, (before, after)
    # o artefato que o agente mock escreveu no ws (AGENT_OUTPUT.txt) NUNCA
    # deve ter sido copiado de volta pro work_path — só aplica se verify passa.
    assert not (wp / "AGENT_OUTPUT.txt").exists()

    # D7: falha de verify não é veredito enquanto sobrar tentativa e tier —
    # a unidade volta pra fila num rank acima (o que o aceite 7 garante é o
    # work_path intacto, acima, não o estado final da linha).
    rows = project.read_queue(project.PROJECTS_ROOT / "a7_p1")
    assert rows[0]["state"] == "pending"
    assert rows[0]["attempts"] == "1"


# ------------------------------------------------------------------- D7 router


def test_execute_grava_tier_e_class_nas_notes():
    _add_project("d7_notes")
    _enqueue("d7_notes", "u1", "faça algo\n", verify_text=ALWAYS_FAIL_VERIFY)

    r = run_cli(["run", "--once", "--project", "d7_notes"])
    assert r.returncode == 0, r.stdout + r.stderr

    results = (project.PROJECTS_ROOT / "d7_notes" / "results.tsv").read_text().splitlines()
    row_map = dict(zip(results[0].split("\t"), results[1].split("\t"), strict=False))
    assert "tier:" in row_map["notes"], row_map["notes"]
    assert "class:" in row_map["notes"], row_map["notes"]
    assert row_map["model"], row_map


def test_execute_requeue_em_falha_com_attempts():
    _add_project("d7_requeue")
    _enqueue("d7_requeue", "u1", "faça algo\n", verify_text=ALWAYS_FAIL_VERIFY)
    proj_dir = project.PROJECTS_ROOT / "d7_requeue"

    r = run_cli(["run", "--once", "--project", "d7_requeue"])
    assert r.returncode == 0, r.stdout + r.stderr

    row = project.read_queue(proj_dir)[0]
    assert row["state"] == "pending"
    assert row["attempts"] == "1"
    # prompt curto => haiku na 1a tentativa; a 2a já vem marcada um rank acima.
    assert row["tier"] == "sonnet", row


def test_execute_falha_definitiva_no_max_attempts():
    _add_project("d7_max")
    _enqueue("d7_max", "u1", "faça algo\n", verify_text=ALWAYS_FAIL_VERIFY)
    proj_dir = project.PROJECTS_ROOT / "d7_max"

    rows = project.read_queue(proj_dir)
    rows[0]["attempts"] = "2"  # max_attempts=3 inclui a 1a: esta é a última
    project.write_queue(proj_dir, rows)

    r = run_cli(["run", "--once", "--project", "d7_max"])
    assert r.returncode == 0, r.stdout + r.stderr

    row = project.read_queue(proj_dir)[0]
    assert row["state"] == "failed"
    assert row["attempts"] == "2"


def test_execute_tamper_nao_escala():
    _add_project("d7_tamper")
    proj_dir = project.PROJECTS_ROOT / "d7_tamper"
    verify_target = proj_dir / "verify" / "0001.py"
    _enqueue("d7_tamper", "u1", f"faça algo\nMOCK_TAMPER: {verify_target}\n")

    r = run_cli(["run", "--once", "--project", "d7_tamper"])
    assert r.returncode == 0, r.stdout + r.stderr

    row = project.read_queue(proj_dir)[0]
    assert row["state"] == "failed"
    assert row["attempts"] == "0"


def test_queue_tsv_legado_8_colunas_le():
    """queue.tsv gravado antes do D7 (8 colunas) continua carregando."""
    _add_project("d7_legado")
    _enqueue("d7_legado", "u1", "faça algo\n")
    proj_dir = project.PROJECTS_ROOT / "d7_legado"

    legacy_header = project.QUEUE_HEADER[:8]
    rows = project.read_queue(proj_dir)
    lines = ["\t".join(legacy_header)]
    lines += ["\t".join(r.get(c, "") for c in legacy_header) for r in rows]
    (proj_dir / "queue.tsv").write_text("\n".join(lines) + "\n")

    back = project.read_queue(proj_dir)
    assert back[0]["id"] == "0001"
    assert back[0]["attempts"] == ""
    assert back[0]["tier"] == ""
