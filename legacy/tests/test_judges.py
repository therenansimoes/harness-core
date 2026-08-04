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
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "attic" / "judges"))

os.environ["PERSONA_MOCK"] = "1"

import persona  # noqa: E402
import run_judge  # noqa: E402

REGISTRY = REPO / "attic" / "judges" / "registry.tsv"
SEALED = REPO / "attic" / "judges" / "_sealed" / "j_b2b" / "test_checksum.py"
TASK_DIR = REPO / "benchmarks" / "judge" / "task_j_b2b"

SEALED_WEB = REPO / "attic" / "judges" / "_sealed" / "j_web" / "index.test.ts"
TASK_DIR_WEB = REPO / "benchmarks" / "judge" / "task_j_web"

SEALED_HW = REPO / "attic" / "judges" / "_sealed" / "j_hw" / "tests.c"
TASK_DIR_HW = REPO / "benchmarks" / "judge" / "task_j_hw"


# ------------------------------------------------------------------ registry


def _read_registry_rows() -> list[dict]:
    lines = REGISTRY.read_text().splitlines()
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"), strict=False)) for line in lines[1:] if line.strip()]


def test_registry_parseavel():
    rows = _read_registry_rows()
    assert rows, "registry.tsv vazio"
    cols = {
        "judge_id",
        "upstream_url",
        "base_sha",
        "fix_sha",
        "sealed_sha256",
        "rubric_version",
        "license",
    }
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
        shutil.copy(
            TASK_DIR / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py"
        )

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
        shutil.copy(
            TASK_DIR_WEB / "verify.py",
            fake_repo / "benchmarks" / "judge" / "task_j_web" / "verify.py",
        )

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
        shutil.copy(
            TASK_DIR_HW / "verify.py",
            fake_repo / "benchmarks" / "judge" / "task_j_hw" / "verify.py",
        )

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
        shutil.copy(
            TASK_DIR / "verify.py", fake_repo / "benchmarks" / "judge" / "task_j_b2b" / "verify.py"
        )

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
    import verify as task_verify

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


def test_extract_ficha_json_ignora_cot_antes_do_json():
    """Prompt novo pede raciocínio (evidência por critério) antes do JSON
    final — result não é mais JSON puro de ponta a ponta."""
    result_text = (
        "Evidência P1: germany.py:1 — usa reconcile do domínio.\n"
        "Evidência P2: trace.jsonl:1 — bate com o DONE.\n"
        '{"P1": {"score": 12, "citation": "germany.py:1", "quote": "x"}, '
        '"P2": {"score": 8, "citation": "trace.jsonl:1", "quote": "y"}}'
    )
    ficha = persona.extract_ficha_json(result_text)
    assert ficha["P1"]["score"] == 12
    assert ficha["P2"]["citation"] == "trace.jsonl:1"


def test_extract_ficha_json_com_json_citado_na_evidencia():
    """JSON citado como evidência antes da ficha final deve ser ignorado;
    deve extrair a ficha final mesmo quando houver múltiplos '{' no texto."""
    result_text = (
        "Evidência P1: germany.py:1 — usa reconcile do domínio.\n"
        'A resposta anterior foi: {"status": 200, "body": '
        '{"checksum": "ABC123", "expected": "ABC124"}} — isso está errado.\n'
        "Evidência P2: trace.jsonl:1 — bate com o DONE.\n"
        '{"P1": {"score": 12, "citation": "germany.py:1", "quote": "x"}, '
        '"P2": {"score": 8, "citation": "trace.jsonl:1", "quote": "y"}}'
    )
    ficha = persona.extract_ficha_json(result_text)
    assert ficha["P1"]["score"] == 12
    assert ficha["P2"]["citation"] == "trace.jsonl:1"


def test_extract_ficha_json_sem_json_valido_levanta_erro():
    """Quando nenhum '{' no texto é decodificável como ficha válida,
    deve levantar JSONDecodeError."""
    result_text = "Evidência P1: germany.py:1 — usa reconcile do domínio.\nNenhum JSON válido aqui."
    with pytest.raises(json.JSONDecodeError):
        persona.extract_ficha_json(result_text)


def _patch_subprocess_run(monkeypatch, returncode: int, stdout: str, stderr: str = ""):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(persona.subprocess, "run", fake_run)


def test_call_persona_com_exit_nao_zero_mas_json_valido_nao_descarta(monkeypatch):
    """Mesmo padrão de agent.py/_run_cli: `claude -p` pode sair com exit
    != 0 e mesmo assim ter emitido um result com ficha válida. returncode
    != 0 não pode descartar um JSON parseável."""
    monkeypatch.setenv("PERSONA_MOCK", "0")
    stream_result = {
        "result": '{"P1": {"score": 10, "citation": "a.py:1", "quote": "q1"}, '
        '"P2": {"score": 5, "citation": "trace.jsonl:1", "quote": "q2"}}'
    }
    _patch_subprocess_run(
        monkeypatch, returncode=1, stdout=json.dumps(stream_result), stderr="algum aviso"
    )

    ficha = persona.call_persona({}, "diff", "trace", "test output")
    assert ficha["P1"]["score"] == 10
    assert ficha["P2"]["citation"] == "trace.jsonl:1"


def test_call_persona_exit_nao_zero_sem_json_levanta_erro(monkeypatch):
    """returncode != 0 SEM nada aproveitável no stdout continua sendo
    erro de infra de verdade."""
    monkeypatch.setenv("PERSONA_MOCK", "0")
    _patch_subprocess_run(monkeypatch, returncode=1, stdout="", stderr="crash de infra")

    try:
        persona.call_persona({}, "diff", "trace", "test output")
        assert False, "deveria ter levantado"
    except RuntimeError as exc:
        assert "exit 1" in str(exc)


# ------------------------------------------------------------ citação/veto


def test_criterio_sem_citacao_e_descartado():
    ficha = {
        "P1": {"score": 12, "citation": "", "quote": ""},
        "P2": {"score": 8, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, veto, _reason = run_judge.validate_and_score_persona(
        ficha, diff="algo", trace="trace.jsonl:1 DONE: ok"
    )
    assert discarded == ["P1"]
    assert not veto
    assert "P1" not in scored
    assert "P2" in scored


def test_citacao_invalida_vira_veto_de_persona():
    """Citação inválida veta a persona (P1+P2 descartados), NÃO zera a
    ficha inteira — esse é o veto de candidato (D2), outra severidade."""
    ficha = {
        "P1": {"score": 15, "citation": "germany.py:1", "quote": "isso não existe no diff"},
        "P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, persona_vetoed, reason = run_judge.validate_and_score_persona(
        ficha, diff="+ return super().reconcile(checksum)", trace="trace.jsonl:1 DONE: ok"
    )
    assert persona_vetoed
    assert reason
    assert scored == {}
    assert discarded == ["P1", "P2"]


def test_citacao_com_quebra_de_linha_escapada_nao_veta():
    """Regressão: trace.jsonl guarda a linha bruta do stream, com `\\n`
    (2 chars) dentro do campo `text`. A citação da persona, depois de
    json.loads(), tem quebra de linha REAL nesse mesmo trecho — os dois
    lados precisam ser normalizados pra bater. Fixture = trace real de
    runs/harness_task_j_b2b_gp04m5hx/trace.jsonl linha 160 (o caso do
    v0.4 verdict: P2 citou trace.jsonl:160 e foi vetado por engano)."""
    trace_path = REPO / "runs" / "harness_task_j_b2b_gp04m5hx" / "trace.jsonl"
    lines = trace_path.read_text().splitlines()
    trace = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=1))

    # trecho real da linha 160, que no arquivo bruto atravessa um `\n`
    # escapado (entre "<task-notification>" e "Agent ... completed.").
    quote_com_newline_real = "<task-notification>\nAgent a4938732c46f34fea completed."

    ficha = {
        "P1": {"score": 0, "citation": "", "quote": ""},
        "P2": {"score": 8, "citation": "trace.jsonl:160", "quote": quote_com_newline_real},
    }
    scored, discarded, persona_vetoed, reason = run_judge.validate_and_score_persona(
        ficha, diff="", trace=trace
    )
    assert not persona_vetoed, reason
    assert "P2" in scored
    assert discarded == ["P1"]


def test_citacao_valida_nao_veta():
    ficha = {
        "P1": {
            "score": 15,
            "citation": "germany.py:1",
            "quote": "return super().reconcile(checksum)",
        },
        "P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "DONE: ok"},
    }
    scored, discarded, veto, _reason = run_judge.validate_and_score_persona(
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
        "judge_id",
        "harness_version",
        "rubric_version",
        "base_sha",
        "sealed_sha256",
        "deterministic",
        "persona",
        "discarded",
        "veto_reason",
        "judge_score",
        "cost_usd",
        "ts",
    }
    assert expected_top <= set(loaded)
    assert loaded["judge_id"] == "j_b2b"
    assert set(loaded["deterministic"]) >= {"D1", "D2", "D3", "D4", "veto", "evidence"}
    assert isinstance(loaded["judge_score"], int)
    assert 0 <= loaded["judge_score"] <= 100
    assert loaded["veto_reason"] == ""
    assert loaded["discarded"] == []


def test_veto_de_candidato_zera_judge_score():
    """D2 (deterministic['veto']=True, trapaça do candidato) é o único
    veto que zera a ficha inteira."""
    reg = run_judge.read_registry_row("j_b2b")
    deterministic = run_judge.synthetic_deterministic()
    deterministic["veto"] = True
    verdict = run_judge.build_verdict(
        "j_b2b",
        reg,
        deterministic,
        persona_scored={},
        discarded=[],
        persona_vetoed=False,
        veto_reason="",
        cost_usd=0.1,
    )
    assert verdict["judge_score"] == 0
    assert verdict["veto_reason"] == "D2: tamper/segredo/escrita fora do workspace"
    assert verdict["persona_vetoed"] is False


def test_persona_vetada_mantem_score_deterministico():
    """Veto de persona (citação inválida) NÃO zera D1-D4 — só descarta
    P1/P2 do cálculo. Recalcula o caso real do v0.4: D1=25 D2=15 D3=10
    D4=2, sem P1/P2 -> 52/60 * 100 = 87."""
    reg = run_judge.read_registry_row("j_b2b")
    deterministic = {
        "D1": 25,
        "D2": 15,
        "D3": 10,
        "D4": 2,
        "veto": False,
        "evidence": {
            "target_test": "passed",
            "full_suite": "415 passed, 0 regressions / 415 total",
            "cost_usd": 0.5237,
            "turns": 1,
        },
    }
    verdict = run_judge.build_verdict(
        "j_b2b",
        reg,
        deterministic,
        persona_scored={},
        discarded=["P1", "P2"],
        persona_vetoed=True,
        veto_reason="persona vetada: citação inválida em P2: 'trace.jsonl:160' não sustentada pelo material",
        cost_usd=0.5237,
    )
    assert verdict["persona_vetoed"] is True
    assert verdict["judge_score"] == 87
    assert verdict["veto_reason"]


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
        "j_b2b",
        reg,
        deterministic,
        persona_scored={"P2": {"score": 10, "citation": "trace.jsonl:1", "quote": "x"}},
        discarded=["P1"],
        persona_vetoed=False,
        veto_reason="",
        cost_usd=0.1,
    )
    # numer = 60 + 10 = 70; denom = 60 + 10 (peso de P2) = 70 -> 100
    assert verdict["judge_score"] == 100


# --------------------------------------------------------------- generalização


def test_run_dry_default_e_j_b2b():
    """Sem --judge, o comportamento é o de antes (compatibilidade)."""
    verdict = run_judge.run_dry()
    assert verdict["judge_id"] == "j_b2b"


def test_run_dry_aceita_judge_id_por_parametro():
    verdict = run_judge.run_dry("j_web")
    assert verdict["judge_id"] == "j_web"
    reg_web = run_judge.read_registry_row("j_web")
    assert verdict["base_sha"] == reg_web["base_sha"]


def test_task_dir_for_segue_convencao():
    assert run_judge.task_dir_for("j_hw") == TASK_DIR_HW
    assert run_judge.task_dir_for("j_web") == TASK_DIR_WEB
    assert run_judge.task_dir_for("j_b2b") == TASK_DIR


def test_all_judge_ids_veem_do_registry():
    ids = run_judge.all_judge_ids()
    assert set(ids) == {"j_b2b", "j_web", "j_hw"}


def test_all_judges_dry_run_gera_3_verdicts_e_summary(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    summary = run_judge.run_all_judges(dry_run=True)

    assert set(summary["scores"]) == {"j_b2b", "j_web", "j_hw"}
    for judge_id in ("j_b2b", "j_web", "j_hw"):
        out = verdicts_dir / judge_id / f"{run_judge.harness_version()}.json"
        assert out.exists()
        loaded = json.loads(out.read_text())
        assert loaded["judge_id"] == judge_id
        assert loaded["judge_score"] == summary["scores"][judge_id]

    summary_path = verdicts_dir / f"summary_{run_judge.harness_version()}.json"
    assert summary_path.exists()
    loaded_summary = json.loads(summary_path.read_text())
    assert loaded_summary["scores"] == summary["scores"]

    values = list(summary["scores"].values())
    assert summary["median"] == statistics.median(values)
    assert summary["spread"] == max(values) - min(values)


def test_build_summary_mediana_e_spread():
    summary = run_judge.build_summary({"j_b2b": 90, "j_web": 80, "j_hw": 70})
    assert summary["median"] == 80
    assert summary["spread"] == 20
    assert summary["inconclusive"] is False


def test_build_summary_spread_maior_que_25_marca_inconclusive():
    summary = run_judge.build_summary({"j_b2b": 95, "j_web": 60, "j_hw": 90})
    assert summary["spread"] == 35
    assert summary["inconclusive"] is True


def test_build_summary_ignora_scores_none():
    """judge_score None (infra_error) não pode quebrar mediana/spread nem
    ser confundido com um score real."""
    summary = run_judge.build_summary({"j_b2b": 90, "j_web": None, "j_hw": 70})
    assert summary["median"] == 80
    assert summary["spread"] == 20
    assert summary["scores"]["j_web"] is None


# ------------------------------------------------------- repeats intra-juiz

# Scores determinísticos que synthetic_deterministic(i) produz, por índice:
# D1 perde 5 por repetição (satura em 25), sobre 60 pontos de D1-D4.
DRY_SCORES = [100, 92, 83, 75, 67, 58]


def test_deterministic_score_so_olha_d1_d4():
    """O que se repete em --repeats é o determinístico — a persona roda 1x,
    sobre a run mediana, então ela não pode entrar na amostra."""
    assert run_judge.deterministic_score(run_judge.synthetic_deterministic()) == 100
    assert run_judge.deterministic_score(run_judge.synthetic_deterministic(2)) == 83
    vetado = run_judge.synthetic_deterministic()
    vetado["veto"] = True
    assert run_judge.deterministic_score(vetado) == 0


def test_aggregate_repeats_mediana_e_spread_intra():
    agg = run_judge.aggregate_repeats([47, 81, 79])
    assert agg["repeats"] == 3
    assert agg["scores_runs"] == [47, 81, 79]
    assert agg["median"] == 79
    assert agg["spread_intra"] == 34
    assert agg["unstable"] is True


def test_aggregate_repeats_estavel_nao_marca_unstable():
    agg = run_judge.aggregate_repeats([79, 81, 80])
    assert agg["median"] == 80
    assert agg["spread_intra"] == 2
    assert agg["unstable"] is False


def test_aggregate_repeats_limiar_e_exclusivo():
    """spread_intra == 25 ainda é estável; unstable só a partir de > 25
    (mesma convenção do spread ENTRE juízes)."""
    assert run_judge.aggregate_repeats([60, 85])["unstable"] is False
    assert run_judge.aggregate_repeats([60, 86])["unstable"] is True


def test_median_run_index_com_n_impar_pega_a_run_da_mediana():
    assert run_judge.median_run_index([100, 83, 92], [0.4, 0.4, 0.4]) == 2


def test_median_run_index_com_n_par_desempata_pela_mais_barata():
    """Com N par a mediana cai entre duas runs e nenhuma tem aquele score —
    empate de distância vai pra mais barata."""
    scores = [100, 92, 83, 75]  # mediana 87.5, empate entre 92 e 83
    assert run_judge.median_run_index(scores, [0.40, 0.43, 0.42, 0.41]) == 2
    assert run_judge.median_run_index(scores, [0.40, 0.41, 0.42, 0.43]) == 1


def test_repeats_1_e_identico_ao_comportamento_atual():
    """N=1 (default) não muda nem score nem ficha — só declara o que sempre
    foi verdade (1 run, spread_intra 0)."""
    base = run_judge.run_dry()
    um = run_judge.run_dry(repeats=1)

    ignorar = {"ts", "repeats", "scores_runs", "spread_intra", "unstable"}
    assert {k: v for k, v in um.items() if k not in ignorar} == {
        k: v for k, v in base.items() if k not in ignorar
    }
    assert um["repeats"] == 1
    assert um["scores_runs"] == [DRY_SCORES[0]]
    assert um["spread_intra"] == 0
    assert um["unstable"] is False


def test_dry_run_com_repeats_agrega_por_mediana():
    """3 runs sintéticas distintas -> a ficha julgada é a da run MEDIANA
    (D1 = 20, a do meio), não a melhor nem a média."""
    verdict = run_judge.run_dry("j_b2b", repeats=3)

    assert verdict["repeats"] == 3
    assert verdict["scores_runs"] == DRY_SCORES[:3]
    assert verdict["spread_intra"] == 17
    assert verdict["unstable"] is False
    assert verdict["judge_score"] is not None
    assert verdict["deterministic"]["D1"] == 20  # run mediana (índice 1), não a 25 do índice 0
    assert verdict["persona"]  # persona rodou (1x), sobre a run mediana


def test_repeats_instavel_vira_abstencao_sem_chamar_persona():
    """spread_intra > 25: o juiz não repete, então não vota. judge_score
    None (não 0 — 0 seria "trabalho ruim") e persona nem é chamada."""
    verdict = run_judge.run_dry("j_b2b", repeats=6)

    assert verdict["unstable"] is True
    assert verdict["spread_intra"] == DRY_SCORES[0] - DRY_SCORES[5]
    assert verdict["judge_score"] is None
    assert verdict["persona"] == {}
    assert "se abstém" in verdict["unstable_reason"]
    assert verdict["deterministic"] is not None  # rastro do que foi medido


def test_summary_sem_intra_preserva_formato_antigo():
    """N=1 não pode mudar o summary de quem já consome ele."""
    summary = run_judge.build_summary({"j_b2b": 90, "j_web": 80, "j_hw": 70})
    assert set(summary) == {"scores", "median", "spread", "inconclusive", "ts"}


def test_summary_registra_juiz_unstable_em_vez_de_score():
    """Abstenção: o instável sai da mediana ENTRE juízes (score None) e
    aparece nominalmente em unstable_judges."""
    intra = {
        "j_b2b": {"repeats": 3, "scores_runs": [47, 81, 79], "spread_intra": 34, "unstable": True},
        "j_web": {"repeats": 3, "scores_runs": [80, 82, 81], "spread_intra": 2, "unstable": False},
        "j_hw": {"repeats": 3, "scores_runs": [70, 72, 71], "spread_intra": 2, "unstable": False},
    }
    summary = run_judge.build_summary({"j_b2b": None, "j_web": 81, "j_hw": 71}, intra)

    assert summary["median"] == 76  # só j_web e j_hw
    assert summary["unstable_judges"] == ["j_b2b"]
    assert summary["scores"]["j_b2b"] is None
    assert summary["inconclusive"] is True
    assert summary["inconclusive_reason"] == ["variance_intra"]
    assert summary["repeats"] == 3


def test_summary_separa_discordancia_de_variancia_intra():
    """As duas causas de dúvida viviam fundidas num `inconclusive` só."""
    intra = {
        j: {"repeats": 3, "scores_runs": [], "spread_intra": 2, "unstable": False}
        for j in ("j_b2b", "j_web", "j_hw")
    }
    summary = run_judge.build_summary({"j_b2b": 95, "j_web": 60, "j_hw": 90}, intra)
    assert summary["inconclusive"] is True
    assert summary["inconclusive_reason"] == ["disagreement"]
    assert summary["unstable_judges"] == []


def test_all_judges_com_repeats_grava_intra_no_summary(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    summary = run_judge.run_all_judges(dry_run=True, repeats=3)

    assert summary["repeats"] == 3
    assert summary["unstable_judges"] == []
    for judge_id in ("j_b2b", "j_web", "j_hw"):
        assert summary["intra"][judge_id]["scores_runs"] == DRY_SCORES[:3]
        loaded = json.loads(
            (verdicts_dir / judge_id / f"{run_judge.harness_version()}.json").read_text()
        )
        assert loaded["repeats"] == 3
        assert loaded["judge_score"] == summary["scores"][judge_id]

    loaded_summary = json.loads(
        (verdicts_dir / f"summary_{run_judge.harness_version()}.json").read_text()
    )
    assert loaded_summary["intra"] == summary["intra"]


def test_all_judges_sem_repeats_nao_muda_summary(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    summary = run_judge.run_all_judges(dry_run=True)
    assert "intra" not in summary and "repeats" not in summary


def test_cli_repeats_chega_no_verdict(tmp_path, monkeypatch):
    """Wiring do CLI. main() in-process (com VERDICTS_DIR redirecionado) em
    vez de subprocess: judges/verdicts/j_b2b/ é versionado, um subprocess
    sujaria o repo de verdade."""
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)
    monkeypatch.setattr(sys, "argv", ["run_judge.py", "--dry-run", "--repeats", "3"])

    assert run_judge.main() == 0
    loaded = json.loads(
        (verdicts_dir / "j_b2b" / f"{run_judge.harness_version()}.json").read_text()
    )
    assert loaded["repeats"] == 3
    assert loaded["scores_runs"] == DRY_SCORES[:3]


def test_cli_rejeita_repeats_invalido(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_judge.py", "--dry-run", "--repeats", "0"])
    with pytest.raises(SystemExit, match="--repeats precisa ser >= 1"):
        run_judge.main()


def test_cli_repeats_nao_existe_na_trilha_build(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["run_judge.py", "--dry-run", "--track", "build", "--repeats", "3"]
    )
    with pytest.raises(SystemExit, match="só existe na trilha result"):
        run_judge.main()


# ---------------------------------------------------------- verdicts timestamped


def test_write_verdict_grava_history_timestamped_e_compat(tmp_path, monkeypatch):
    """write_verdict grava um registro histórico timestamped (que nunca é
    sobrescrito) além da cópia `<versão>.json` de compatibilidade."""
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    verdict = run_judge.run_dry()
    out = run_judge.write_verdict(verdict)

    version = verdict["harness_version"]
    assert out == verdicts_dir / "j_b2b" / f"{version}.json"
    assert out.exists()

    history_dir = verdicts_dir / "j_b2b" / "history"
    hist_files = list(history_dir.glob(f"{version}_*.json"))
    assert len(hist_files) == 1
    assert json.loads(hist_files[0].read_text()) == json.loads(out.read_text())


def test_write_verdict_rerun_nao_apaga_history_anterior(tmp_path, monkeypatch):
    """Re-rodar o mesmo juiz/versão sobrescreve a cópia de compat mas
    acumula um novo registro em history/ (dívida do STATUS.md: antes o
    verdict.json era sobrescrito a cada re-run e o histórico se perdia)."""
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    v1 = run_judge.run_dry()
    v1["ts"] = "2026-01-01T00:00:00+00:00"
    run_judge.write_verdict(v1)

    v2 = run_judge.run_dry()
    v2["ts"] = "2026-01-01T00:00:05+00:00"
    out2 = run_judge.write_verdict(v2)

    history_dir = verdicts_dir / "j_b2b" / "history"
    hist_files = sorted(p.name for p in history_dir.glob(f"{v2['harness_version']}_*.json"))
    assert len(hist_files) == 2
    assert json.loads(out2.read_text())["ts"] == "2026-01-01T00:00:05+00:00"


def test_write_verdict_history_nao_duplica_no_ingest(tmp_path, monkeypatch):
    """graph.ingest_verdicts varre `*/*.json` — history/ fica um nível a
    mais fundo, então não bate no glob e não duplica linha na tabela
    `judgements` por causa da cópia de compat."""
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    verdict = run_judge.run_dry()
    run_judge.write_verdict(verdict)

    import graph

    db_path = tmp_path / "g.db"
    n = graph.ingest_verdicts(verdicts_dir=verdicts_dir, db_path=str(db_path))
    assert n == 1

    rows = graph.judge_history(n=100, db_path=str(db_path))
    assert len(rows) == 1
