#!/usr/bin/env python3
"""Testa a camada de juízes FASE 1: registry, sealed test, verify.py,
montagem do verdict.json (--dry-run) e a regra de citação/veto da persona.

Sem chamada de API/subprocess `claude` — persona roda em PERSONA_MOCK=1.

    python3 -m pytest tests/test_judges.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "judges"))

os.environ["PERSONA_MOCK"] = "1"

import persona  # noqa: E402
import run_judge  # noqa: E402

REGISTRY = REPO / "judges" / "registry.tsv"
SEALED = REPO / "judges" / "_sealed" / "j_b2b" / "test_checksum.py"
TASK_DIR = REPO / "benchmarks" / "judge" / "task_j_b2b"

SEALED_WEB = REPO / "judges" / "_sealed" / "j_web" / "index.test.ts"
TASK_DIR_WEB = REPO / "benchmarks" / "judge" / "task_j_web"

SEALED_HW = REPO / "judges" / "_sealed" / "j_hw" / "tests.c"
TASK_DIR_HW = REPO / "benchmarks" / "judge" / "task_j_hw"


# ------------------------------------------------------------------ registry


def _read_registry_rows() -> list[dict]:
    lines = REGISTRY.read_text().splitlines()
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line.strip()]


def test_registry_parseavel():
    rows = _read_registry_rows()
    assert rows, "registry.tsv vazio"
    cols = {"judge_id", "upstream_url", "base_sha", "fix_sha", "sealed_sha256", "rubric_version", "license"}
    for row in rows:
        assert cols <= set(row), f"linha do registry sem todas as colunas: {row}"
        assert len(row["base_sha"]) == 40, "base_sha deveria ser um sha1 de 40 chars"
        assert len(row["fix_sha"]) == 40, "fix_sha deveria ser um sha1 de 40 chars"
        assert len(row["sealed_sha256"]) == 64, "sealed_sha256 deveria ser um sha256 de 64 chars"


def test_registry_tem_j_b2b():
    rows = _read_registry_rows()
    ids = {r["judge_id"] for r in rows}
    assert "j_b2b" in ids


def test_registry_tem_j_web():
    rows = _read_registry_rows()
    ids = {r["judge_id"] for r in rows}
    assert "j_web" in ids


def test_registry_tem_j_hw():
    rows = _read_registry_rows()
    ids = {r["judge_id"] for r in rows}
    assert "j_hw" in ids


# --------------------------------------------------------------- sealed sha256


def test_sealed_sha256_bate_com_registry():
    rows = _read_registry_rows()
    row = next(r for r in rows if r["judge_id"] == "j_b2b")
    assert SEALED.exists(), f"sealed test ausente: {SEALED}"
    actual = hashlib.sha256(SEALED.read_bytes()).hexdigest()
    assert actual == row["sealed_sha256"]


def test_sealed_sha256_bate_com_registry_j_web():
    rows = _read_registry_rows()
    row = next(r for r in rows if r["judge_id"] == "j_web")
    assert SEALED_WEB.exists(), f"sealed test ausente: {SEALED_WEB}"
    actual = hashlib.sha256(SEALED_WEB.read_bytes()).hexdigest()
    assert actual == row["sealed_sha256"]


def test_sealed_sha256_bate_com_registry_j_hw():
    rows = _read_registry_rows()
    row = next(r for r in rows if r["judge_id"] == "j_hw")
    assert SEALED_HW.exists(), f"sealed test ausente: {SEALED_HW}"
    actual = hashlib.sha256(SEALED_HW.read_bytes()).hexdigest()
    assert actual == row["sealed_sha256"]


# ------------------------------------------------------------------- verify.py


def test_verify_detecta_sealed_adulterado():
    """Se alguém adulterar o arquivo selado (fora do registry), verify.py
    tem que recusar antes de rodar qualquer teste."""
    ws = Path(tempfile.mkdtemp(prefix="judge_verify_tamper_"))
    fake_repo = Path(tempfile.mkdtemp(prefix="judge_fake_repo_"))
    try:
        (fake_repo / "judges" / "_sealed" / "j_b2b").mkdir(parents=True)
        (fake_repo / "judges").joinpath("registry.tsv").write_text(REGISTRY.read_text())
        # adultera o conteúdo do "selado" sem atualizar o hash no registry
        (fake_repo / "judges" / "_sealed" / "j_b2b" / "test_checksum.py").write_text(
            "def test_fake():\n    assert True\n"
        )
        (fake_repo / "benchmarks" / "judge" / "task_j_b2b").mkdir(parents=True)
        shutil.copy(TASK_DIR / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py")

        proc = subprocess.run(
            [sys.executable, str(fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py")],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0, "verify.py deveria recusar um sealed adulterado"
        assert "sealed_sha256" in (proc.stdout + proc.stderr)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(fake_repo, ignore_errors=True)


def test_verify_j_web_detecta_sealed_adulterado():
    """Mesma checagem do j_b2b, aplicada ao verify.py do j_web (sealed é
    listen-keys/index.test.ts, não tests/test_checksum.py)."""
    ws = Path(tempfile.mkdtemp(prefix="judge_verify_web_tamper_"))
    fake_repo = Path(tempfile.mkdtemp(prefix="judge_fake_repo_web_"))
    try:
        (fake_repo / "judges" / "_sealed" / "j_web").mkdir(parents=True)
        (fake_repo / "judges").joinpath("registry.tsv").write_text(REGISTRY.read_text())
        # adultera o conteúdo do "selado" sem atualizar o hash no registry
        (fake_repo / "judges" / "_sealed" / "j_web" / "index.test.ts").write_text(
            "test('fake', () => {})\n"
        )
        (fake_repo / "benchmarks" / "judge" / "task_j_web").mkdir(parents=True)
        shutil.copy(TASK_DIR_WEB / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_web" / "verify.py")

        proc = subprocess.run(
            [sys.executable, str(fake_repo / "benchmarks" / "judge" / "task_j_web" / "verify.py")],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0, "verify.py deveria recusar um sealed adulterado"
        assert "sealed_sha256" in (proc.stdout + proc.stderr)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(fake_repo, ignore_errors=True)


def test_verify_j_hw_detecta_sealed_adulterado():
    """Mesma checagem do j_b2b, aplicada ao verify.py do j_hw (sealed é
    test/tests.c, o gabarito em C)."""
    ws = Path(tempfile.mkdtemp(prefix="judge_verify_hw_tamper_"))
    fake_repo = Path(tempfile.mkdtemp(prefix="judge_fake_repo_hw_"))
    try:
        (fake_repo / "judges" / "_sealed" / "j_hw").mkdir(parents=True)
        (fake_repo / "judges").joinpath("registry.tsv").write_text(REGISTRY.read_text())
        # adultera o conteúdo do "selado" sem atualizar o hash no registry
        (fake_repo / "judges" / "_sealed" / "j_hw" / "tests.c").write_text(
            "int main(void) { return 0; }\n"
        )
        (fake_repo / "benchmarks" / "judge" / "task_j_hw").mkdir(parents=True)
        shutil.copy(TASK_DIR_HW / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_hw" / "verify.py")

        proc = subprocess.run(
            [sys.executable, str(fake_repo / "benchmarks" / "judge" / "task_j_hw" / "verify.py")],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0, "verify.py deveria recusar um sealed adulterado"
        assert "sealed_sha256" in (proc.stdout + proc.stderr)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(fake_repo, ignore_errors=True)


def test_verify_acusa_arquivo_selado_ausente():
    """Sem registro nenhum de j_b2b no registry do fake_repo, verify.py
    tem que falhar limpo em vez de estourar traceback."""
    ws = Path(tempfile.mkdtemp(prefix="judge_verify_missing_"))
    fake_repo = Path(tempfile.mkdtemp(prefix="judge_fake_repo2_"))
    try:
        (fake_repo / "judges").mkdir(parents=True)
        (fake_repo / "judges" / "registry.tsv").write_text(
            "judge_id\tupstream_url\tbase_sha\tfix_sha\tsealed_sha256\trubric_version\tlicense\n"
        )
        (fake_repo / "benchmarks" / "judge" / "task_j_b2b").mkdir(parents=True)
        shutil.copy(TASK_DIR / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py")

        proc = subprocess.run(
            [sys.executable, str(fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py")],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(fake_repo, ignore_errors=True)


def test_parse_pytest_counts():
    sys.path.insert(0, str(TASK_DIR))
    import verify as task_verify  # noqa: E402

    out = "........F..\n=== 1 failed, 10 passed, 2 errors in 0.5s ==="
    counts = task_verify.parse_pytest_counts(out)
    assert counts == {"passed": 10, "failed": 1, "errors": 2, "total": 13}


def _load_verify_module(task_dir: Path, module_name: str):
    """Carrega verify.py de um task_dir sob um nome de módulo próprio, pra
    não colidir no sys.modules com o `verify` de outro juiz (todos os
    task_j_*/verify.py se chamam igual)."""
    spec = importlib.util.spec_from_file_location(module_name, task_dir / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_bnt_counts_j_web():
    task_verify = _load_verify_module(TASK_DIR_WEB, "task_j_web_verify")

    out = "✔ some test (1ms)\nℹ tests 5\nℹ pass 4\nℹ fail 1"
    counts = task_verify.parse_bnt_counts(out)
    assert counts == {"passed": 4, "failed": 1, "total": 5}


def test_parse_test_counts_j_hw():
    task_verify = _load_verify_module(TASK_DIR_HW, "task_j_hw_verify")

    out = "FAILED: test for unmatched brackets (at line 363)\n\nPASSED: 14\nFAILED: 1"
    counts = task_verify.parse_test_counts(out)
    assert counts == {"passed": 14, "failed": 1, "total": 15}


# --------------------------------------------------------------------- persona


def test_persona_mock_devolve_ficha_no_schema():
    ficha = persona.call_persona({}, "diff", "trace", "test output")
    assert set(ficha) == {"P1", "P2"}
    for key, max_score in (("P1", 15), ("P2", 10)):
        entry = ficha[key]
        assert set(entry) == {"score", "citation", "quote"}
        assert 0 <= entry["score"] <= max_score
        assert entry["citation"]
        assert entry["quote"]


# ------------------------------------------------------------ citação/veto


def test_criterio_sem_citacao_e_descartado():
    ficha = {
        "P1": {"score": 12, "citation": "", "quote": ""},
        "P2": {"score": 8, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, veto, reason = run_judge.validate_and_score_persona(
        ficha, diff="algo", trace="trace.jsonl:1 DONE: ok"
    )
    assert discarded == ["P1"]
    assert not veto
    assert "P1" not in scored
    assert "P2" in scored


def test_citacao_invalida_vira_veto():
    ficha = {
        "P1": {"score": 15, "citation": "germany.py:1", "quote": "isso não existe no diff"},
        "P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, veto, reason = run_judge.validate_and_score_persona(
        ficha, diff="+ return super().reconcile(checksum)", trace="trace.jsonl:1 DONE: ok"
    )
    assert veto
    assert reason
    assert scored == {}


def test_citacao_valida_nao_veta():
    ficha = {
        "P1": {"score": 15, "citation": "germany.py:1", "quote": "return super().reconcile(checksum)"},
        "P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, veto, reason = run_judge.validate_and_score_persona(
        ficha,
        diff="+ return super().reconcile(checksum)",
        trace="trace.jsonl:1 DONE: ok",
    )
    assert not veto
    assert discarded == []
    assert set(scored) == {"P1", "P2"}


# ------------------------------------------------------------------- verdict


def test_dry_run_produz_verdict_json_valido(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    verdict = run_judge.run_dry()
    out = run_judge.write_verdict(verdict)

    assert out.exists()
    loaded = json.loads(out.read_text())

    expected_top = {
        "judge_id", "harness_version", "rubric_version", "base_sha", "sealed_sha256",
        "deterministic", "persona", "discarded", "veto_reason", "judge_score", "cost_usd", "ts",
    }
    assert expected_top <= set(loaded)
    assert loaded["judge_id"] == "j_b2b"
    assert set(loaded["deterministic"]) >= {"D1", "D2", "D3", "D4", "veto", "evidence"}
    assert isinstance(loaded["judge_score"], int)
    assert 0 <= loaded["judge_score"] <= 100
    assert loaded["veto_reason"] == ""
    assert loaded["discarded"] == []


def test_veto_zera_judge_score():
    reg = run_judge.read_registry_row("j_b2b")
    deterministic = run_judge.synthetic_deterministic()
    verdict = run_judge.build_verdict(
        "j_b2b", reg, deterministic, persona_scored={}, discarded=[], veto=True,
        veto_reason="citação inválida em P1", cost_usd=0.1,
    )
    assert verdict["judge_score"] == 0
    assert verdict["veto_reason"] == "citação inválida em P1"


def test_infra_error_verdict_nao_pontua_e_nao_chama_persona():
    """0 tokens = agente nunca trabalhou (infra quebrada, ex. cli saiu
    antes de emitir JSON) — curto-circuito não pode virar um judge_score=0
    (isso significaria "trabalho ruim", não "infra quebrada")."""
    reg = run_judge.read_registry_row("j_b2b")
    row = {"tokens": "0", "cost_usd": "0.0000", "turns": "0", "notes": "cli_exit_1:"}
    verdict = run_judge.build_infra_error_verdict("j_b2b", reg, row)

    assert verdict["infra_error"] is True
    assert verdict["judge_score"] is None
    assert verdict["deterministic"] is None
    assert verdict["persona"] == {}
    assert "0 tokens" in verdict["infra_error_reason"]


def test_criterio_descartado_recalcula_denominador():
    reg = run_judge.read_registry_row("j_b2b")
    deterministic = run_judge.synthetic_deterministic()  # D1..D4 = 25+15+10+10 = 60/60 (cheio)
    # só P2 pontuado, cheio (10/10); P1 descartado -> não entra em nem numerador nem denominador
    verdict = run_judge.build_verdict(
        "j_b2b", reg, deterministic,
        persona_scored={"P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "x"}},
        discarded=["P1"], veto=False, veto_reason="", cost_usd=0.1,
    )
    # numer = 60 + 10 = 70; denom = 60 + 10 (peso de P2) = 70 -> 100
    assert verdict["judge_score"] == 100
