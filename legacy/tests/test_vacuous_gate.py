#!/usr/bin/env python3
"""Vacuous pass: gate de UI ligado que não julga nada NÃO pode contar como verde.

Casos sintéticos — `run_ui_suite` é monkeypatchado, nada de Playwright aqui.
A suite real (com Playwright de verdade) vive em tests/test_ui_gate.py; o que
se prova neste arquivo é a regra de leitura do resultado, não a suite.

    python3 -m pytest tests/test_vacuous_gate.py -q
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="vacuous_test_"))

os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
os.environ["HARNESS_CONFIG_HOME"] = str(TMP / "noconfig")
sys.path.insert(0, str(REPO))

import delivery  # noqa: E402
import harness_cli  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(TMP, ignore_errors=True)


def fresh(entregue: bool = True) -> Path:
    """Cópia do demo_site com a delta da sessão s001 já entregue."""
    dst = TMP / f"proj_{len(list(TMP.glob('proj_*')))}"
    shutil.copytree(REPO / "projects" / "demo_site", dst)
    shutil.rmtree(dst / "sessions", ignore_errors=True)
    delivery.write_manifest(dst, actor="teste", detail="baseline", record=False)
    delivery.new_session(dst, "s001", brief="# s001\n\n## Aceite\n\n- [x] entregue\n")
    if entregue:
        (dst / "site" / "precos.html").write_text(
            '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
            "<title>Preços</title></head><body><h1>Planos</h1>"
            '<div class="plano">A</div><div class="plano">B</div><div class="plano">C</div>'
            "</body></html>",
            encoding="utf-8",
        )
    return dst


def fake_suite(monkeypatch, resultado: dict) -> None:
    """Força o placar da UI e garante que o gate está LIGADO (suite existe)."""
    monkeypatch.setattr(delivery, "has_ui_suite", lambda _p: True)
    monkeypatch.setattr(delivery, "run_ui_suite", lambda _p, **_k: resultado)


VERDE = {
    "ran": True,
    "passed": 3,
    "total": 3,
    "tests": [{"name": f"t{i}", "ok": True, "reason": ""} for i in range(3)],
    "reason": "",
}
NAO_RODOU = {
    "ran": False,
    "tests": [],
    "passed": 0,
    "total": 0,
    "reason": "npx não encontrado — Node/Playwright não instalado",
}
ZERO_TESTES = {"ran": True, "tests": [], "passed": 0, "total": 0, "reason": ""}


# --------------------------------------------------------------- verify/post_work


def test_gate_ligado_que_nao_rodou_reprova(monkeypatch):
    """ran=False com UI ligada: sem veredito não há entrega bem-sucedida."""
    p = fresh()
    fake_suite(monkeypatch, NAO_RODOU)
    v = delivery.verify(p, "s001")
    assert v["ui_vacuous"], "gate ligado que não rodou deveria ser vacuous"
    assert v["delivery_success"] == 0, "ausência de falha não pode virar verde"
    assert "npx" in v["ui_vacuous_reason"], (
        f"motivo deveria citar a causa: {v['ui_vacuous_reason']}"
    )


def test_gate_ligado_com_zero_testes_reprova(monkeypatch):
    """passed=0/total=0: 0==0 passaria como verde na leitura ingênua."""
    p = fresh()
    fake_suite(monkeypatch, ZERO_TESTES)
    v = delivery.verify(p, "s001")
    assert v["ui_vacuous"], "suite que coletou 0 testes deveria ser vacuous"
    assert v["delivery_success"] == 0, "0/0 não é verde"
    assert not v["ui_ok"], "0/0 não pode marcar ui_ok"


def test_vacuous_pede_humano_e_nao_fecha(monkeypatch):
    p = fresh()
    fake_suite(monkeypatch, ZERO_TESTES)
    r = delivery.post_work(p, "s001", actor="teste")
    assert r["needs_human_ui_review"], "gate sem veredito deveria chamar o dono"
    assert r["next_action"] == "await_renan", f"não pode fechar sozinho, veio {r['next_action']}"
    assert any(i.startswith("ui_gate:vacuous") for i in r["open_issues"]), (
        f"faltou a nota ui_gate:vacuous: {r['open_issues']}"
    )
    report = (p / "sessions" / "s001" / "delivery_report.md").read_text(encoding="utf-8")
    assert "ui_gate:vacuous" in report, "o report precisa dizer por que não fechou"


def test_suite_com_testes_de_verdade_fecha(monkeypatch):
    """Controle: com checks reais e verdes, nada muda — ainda fecha sozinho."""
    p = fresh()
    fake_suite(monkeypatch, VERDE)
    r = delivery.post_work(p, "s001", actor="teste")
    assert not r["ui_vacuous"], "suite com 3 checks não é vacuous"
    assert r["ui_ok"] and r["delivery_success"] == 1, f"deveria fechar verde: {r['open_issues']}"
    assert not r["needs_human_ui_review"], f"não deveria pedir humano: {r['open_issues']}"
    assert r["next_action"] == "done", f"veio {r['next_action']}"


def test_ui_desligada_nao_e_vacuous():
    """Gate desligado não é gate falhando: sem suite, a regra não se aplica."""
    p = fresh()
    cfg = p / ".harness" / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[ui]\nenabled = false\n", encoding="utf-8")
    v = delivery.verify(p, "s001")
    assert not v["ui_vacuous"], "ui desligada não pode ser reprovação de gate"
    assert v["delivery_success"] == 1, f"resto verde deveria fechar: {v}"


def test_decide_next_action_vacuous_vai_para_humano():
    v = {
        "governance_violations": [],
        "regression_passed": 1,
        "regression_total": 1,
        "acceptance_passed": 1,
        "acceptance_total": 1,
        "ui_failed": False,
        "ui_vacuous": True,
        "ui_vacuous_reason": "suite de UI rodou e coletou 0 testes",
    }
    assert delivery.decide_next_action(v, [], False, []) == "await_renan"
    v["ui_vacuous"] = False
    assert delivery.decide_next_action(v, [], False, []) == "done"


# ------------------------------------------------------------------------- cli


def _cli_ui(monkeypatch, resultado: dict, cmd) -> int:
    p = fresh()
    monkeypatch.setattr(delivery, "resolve_project", lambda _n: p)
    monkeypatch.setattr(delivery, "run_ui_suite", lambda _p, **_k: resultado)
    return cmd(argparse.Namespace(project=str(p), note=None))


def test_cli_ui_test_reprova_zero_testes(monkeypatch, capsys):
    rc = _cli_ui(monkeypatch, ZERO_TESTES, harness_cli.cmd_ui_test)
    assert rc == 2, "0/0 não pode sair 0 (verde)"
    assert "ui_gate:vacuous" in capsys.readouterr().out


def test_cli_ui_test_verde_continua_zero(monkeypatch):
    assert _cli_ui(monkeypatch, VERDE, harness_cli.cmd_ui_test) == 0


def test_cli_ui_baseline_nao_registra_zero_testes(monkeypatch, capsys):
    chamadas = []
    monkeypatch.setattr(
        harness_cli.graph, "record_governance_event", lambda **kw: chamadas.append(kw)
    )
    rc = _cli_ui(monkeypatch, ZERO_TESTES, harness_cli.cmd_ui_baseline)
    assert rc == 2, "baseline vazia não pode sair 0"
    assert not chamadas, "não pode registrar governança de baseline que não cobre nada"
    assert "ui_gate:vacuous" in capsys.readouterr().out


def test_cli_ui_baseline_com_testes_registra(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        harness_cli.graph, "record_governance_event", lambda **kw: chamadas.append(kw)
    )
    assert _cli_ui(monkeypatch, VERDE, harness_cli.cmd_ui_baseline) == 0
    assert len(chamadas) == 1, "baseline real continua sendo ato registrado"
