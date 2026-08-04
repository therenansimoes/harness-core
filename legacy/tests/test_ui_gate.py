#!/usr/bin/env python3
"""Prova que o gate de UI é automático: máquina decide, humano só no ambíguo.

Roda Playwright de verdade (headless, Chrome do sistema) sobre CÓPIAS do
demo_site. Nada de API do Claude. Se o Playwright não estiver disponível, o
teste SKIPA com mensagem explícita em vez de fingir que passou.

    python3 -m pytest tests/test_ui_gate.py -q    # skipa se Playwright indisponível
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# As cópias precisam ficar DENTRO do repo: o Node resolve `@playwright/test`
# subindo a árvore de diretórios até achar node_modules. Em /tmp isso falha com
# ERR_MODULE_NOT_FOUND — o mesmo motivo pelo qual um projeto fora do repo
# precisa do seu próprio `npm install` (documentado no README).
(REPO / ".ui_test_tmp").mkdir(exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="ui_gate_", dir=REPO / ".ui_test_tmp"))

os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
os.environ["HARNESS_CONFIG_HOME"] = str(TMP / "noconfig")
os.environ.pop("REVIEW_UI_SUBJECTIVE", None)
sys.path.insert(0, str(REPO))

import delivery  # noqa: E402

PORT = 4200


PRECOS = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Preços</title>
<link rel="stylesheet" href="style.css"></head>
<body><main><h1>Planos</h1>
<div class="plano">Básico</div><div class="plano">Pro</div><div class="plano">Full</div>
</main></body></html>
"""


def playwright_ok() -> bool:
    try:
        r = subprocess.run(
            ["npx", "playwright", "--version"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not playwright_ok(),
    reason="Playwright indisponível (npx playwright --version falhou). Instale com: npm install",
)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(TMP, ignore_errors=True)


def fresh(entregue: bool = True) -> Path:
    """Cópia do demo_site. entregue=True completa a delta da sessão s001."""
    global PORT
    PORT += 1
    os.environ["HARNESS_UI_PORT"] = str(PORT)
    dst = TMP / f"proj_{PORT}"
    shutil.copytree(REPO / "projects" / "demo_site", dst)
    shutil.rmtree(dst / "sessions", ignore_errors=True)
    delivery.write_manifest(dst, actor="teste", detail="baseline", record=False)
    delivery.new_session(dst, "s001", brief="# s001\n\n## Aceite\n\n- [x] entregue\n")
    if entregue:
        (dst / "site" / "precos.html").write_text(PRECOS, encoding="utf-8")
    return dst


# ---------------------------------------------------------------------- testes


def test_tudo_verde_fecha_sem_humano():
    """O ponto da rodada: projeto com UI pode chegar a `done` sozinho."""
    p = fresh(entregue=True)
    r = delivery.post_work(p, "s001", actor="teste")
    assert r["ui"]["ran"], f"suite de UI não rodou: {r['ui']['reason']}"
    assert r["ui_total"] >= 6, f"esperava >=6 checks de UI, veio {r['ui_total']}"
    assert r["ui_ok"], f"UI deveria passar: {[t for t in r['ui']['tests'] if not t['ok']]}"
    assert r["delivery_success"] == 1, "tudo verde deveria dar delivery_success=1"
    assert not r["needs_human_ui_review"], f"não deveria exigir humano: {r['open_issues']}"
    assert r["next_action"] == "done", f"next_action deveria ser done, veio {r['next_action']}"


def test_ui_quebrada_vira_await_renan():
    """Falha de UI é ambígua (bug real ou baseline velha) — aí sim, humano."""
    p = fresh(entregue=True)
    idx = p / "site" / "index.html"
    html = idx.read_text(encoding="utf-8")
    # Banner enorme: muda muito mais que os 2% de threshold do screenshot.
    idx.write_text(
        html.replace("<body>", '<body><div style="height:400px;background:#f00">REGRESSAO</div>'),
        encoding="utf-8",
    )
    r = delivery.post_work(p, "s001", actor="teste")
    assert r["ui_failed"], "mudança visual grande deveria quebrar o screenshot"
    assert r["delivery_success"] == 0, "UI quebrada não pode ser entrega bem-sucedida"
    assert r["needs_human_ui_review"], "falha ambígua de UI deveria pedir humano"
    assert r["next_action"] == "await_renan", (
        f"next_action deveria ser await_renan, veio {r['next_action']}"
    )


def test_review_subjective_forca_humano():
    p = fresh(entregue=True)
    os.environ["REVIEW_UI_SUBJECTIVE"] = "true"
    try:
        r = delivery.post_work(p, "s001", actor="teste")
    finally:
        os.environ.pop("REVIEW_UI_SUBJECTIVE", None)
    assert r["ui_ok"], "UI deveria continuar passando"
    assert r["delivery_success"] == 1, "checks verdes = delivery_success 1 mesmo com review pedido"
    assert r["needs_human_ui_review"], "review_subjective deveria forçar humano"
    assert r["next_action"] == "await_renan", f"veio {r['next_action']}"


def test_manual_ui_check_forca_humano():
    p = fresh(entregue=True)
    (p / "acceptance" / "s001" / "manual_ui_marca.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    r = delivery.post_work(p, "s001", actor="teste")
    assert r["needs_human_ui_review"], "check manual_ui* deveria forçar revisão humana"
    assert r["next_action"] == "await_renan", f"veio {r['next_action']}"


def test_baseline_atualizada_volta_a_passar():
    """Mudança visual intencional: aprovar baseline destrava, e é ato registrado."""
    p = fresh(entregue=True)
    idx = p / "site" / "index.html"
    idx.write_text(
        idx.read_text(encoding="utf-8").replace(
            "<body>", '<body><div style="height:400px;background:#0f0">NOVO BLOCO</div>'
        ),
        encoding="utf-8",
    )
    antes = delivery.run_ui_suite(p)
    assert antes["passed"] < antes["total"], (
        "screenshot deveria falhar antes de atualizar a baseline"
    )
    delivery.run_ui_suite(p, update_baseline=True)
    r2 = delivery.run_ui_suite(p)
    assert r2["passed"] == r2["total"], (
        f"após atualizar baseline deveria passar: {[t for t in r2['tests'] if not t['ok']]}"
    )
