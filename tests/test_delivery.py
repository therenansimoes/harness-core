#!/usr/bin/env python3
"""Testa o eixo de ENTREGA: camadas de verify, governança, post-work, resume.

Roda sobre uma CÓPIA de projects/demo_site em /tmp — o projeto real nunca é
tocado. DB do graph é temporário. Nada aqui chama API nem rede.

    python3 tests/test_delivery.py    # exit 0 = passou
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="delivery_test_"))

os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
os.environ["HARNESS_CONFIG_HOME"] = str(TMP / "noconfig")
sys.path.insert(0, str(REPO))

import delivery  # noqa: E402
import graph  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def fresh(ui_enabled: bool = False) -> Path:
    """Cópia limpa do demo_site, com MANIFEST recém-aprovado.

    UI desligada por default: a camada Playwright é lenta e tem cobertura
    própria em tests/test_ui_gate.py. Aqui o foco é regression/acceptance e
    governança.
    """
    dst = TMP / f"proj_{len(list(TMP.glob('proj_*')))}"
    shutil.copytree(REPO / "projects" / "demo_site", dst)
    if not ui_enabled:
        cfg = dst / ".harness" / "config.toml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[ui]\nenabled = false\n",
                       encoding="utf-8")
    delivery.write_manifest(dst, actor="teste", detail="baseline", record=False)
    delivery.new_session(dst, "s001")
    return dst


# ---------------------------------------------------------------------- testes


def test_camadas_separadas():
    p = fresh()
    v = delivery.verify(p, "s001")
    check(v["regression_total"] == 3, f"esperava 3 regression, veio {v['regression_total']}")
    check(v["regression_passed"] == 3, f"regression deveria passar: {v['regression']}")
    check(v["acceptance_total"] == 2, f"esperava 2 acceptance, veio {v['acceptance_total']}")
    check(v["acceptance_passed"] == 0, "acceptance deveria falhar (delta não entregue)")
    check(v["delivery_success"] == 0, "delivery_success deveria ser 0 com acceptance falhando")


def test_acceptance_passa_quando_delta_entregue():
    p = fresh()
    (p / "site" / "precos.html").write_text(
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
        "<title>Preços</title></head><body><h1>Planos</h1>"
        '<div class="plano">A</div><div class="plano">B</div><div class="plano">C</div>'
        "</body></html>",
        encoding="utf-8",
    )
    v = delivery.verify(p, "s001")
    check(v["acceptance_passed"] == 2, f"acceptance deveria passar: {v['acceptance']}")
    check(v["delivery_success"] == 1, "com tudo verde, delivery_success deveria ser 1")


def test_governanca_bloqueia_delecao():
    """O caminho mais fácil para ficar verde seria apagar o check. Não pode ser."""
    p = fresh()
    (p / "site" / "precos.html").write_text(
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
        "<title>P</title></head><body><h1>Planos</h1>"
        '<div class="plano">A</div><div class="plano">B</div><div class="plano">C</div>'
        "</body></html>",
        encoding="utf-8",
    )
    alvo = p / "regression" / "check_index_existe.py"
    alvo.unlink()  # o "worker" apaga um invariante

    v = delivery.verify(p, "s001")
    check(bool(v["governance_violations"]), "apagar regression deveria ser violação")
    check(any("APAGADO" in x for x in v["governance_violations"]),
          f"violação deveria dizer APAGADO: {v['governance_violations']}")
    check(v["delivery_success"] == 0,
          "delivery NÃO pode ser sucesso com regression apagada, mesmo com o resto verde")


def test_governanca_bloqueia_edicao():
    p = fresh()
    alvo = p / "regression" / "check_index_existe.py"
    alvo.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")  # neutraliza o check
    v = delivery.verify(p, "s001")
    check(any("MODIFICADO" in x for x in v["governance_violations"]),
          f"editar regression deveria ser violação: {v['governance_violations']}")
    check(v["delivery_success"] == 0, "check neutralizado não pode virar sucesso")


def test_adicionar_check_e_permitido():
    p = fresh()
    novo = p / "regression" / "check_extra.py"
    novo.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    v = delivery.verify(p, "s001")
    check(not v["governance_violations"], f"adicionar não é violação: {v['governance_violations']}")
    check("check_extra.py" in v["new_unregistered_checks"], "check novo deveria aparecer como novo")
    check(v["regression_total"] == 4, "check novo deveria ser executado")


def test_governance_approve_limpa_violacao():
    p = fresh()
    (p / "regression" / "check_index_existe.py").unlink()
    check(bool(delivery.verify(p, "s001")["governance_violations"]), "deveria violar antes")
    delivery.write_manifest(p, actor="renan", detail="remoção aprovada")
    check(not delivery.verify(p, "s001")["governance_violations"],
          "após aprovação explícita a violação deveria sumir")
    gov = graph.recent_governance(5)
    check(any(g["actor"] == "renan" for g in gov), f"aprovação deveria virar evento: {gov}")


def test_post_work_grava_tudo():
    p = fresh()
    r = delivery.post_work(p, "s001", actor="teste")
    rep = p / "sessions" / "s001" / "delivery_report.md"
    check(rep.exists(), "delivery_report.md não foi escrito")
    txt = rep.read_text(encoding="utf-8")
    check("Regression (3/3)" in txt, "report não traz o placar de regression")
    check("Acceptance (0/2)" in txt, "report não traz o placar de acceptance")
    check(r["next_action"] == "continue_delivery",
          f"com acceptance falhando, next_action deveria ser continue_delivery, veio {r['next_action']}")

    st = json.loads((p / "sessions" / "s001" / "state.json").read_text(encoding="utf-8"))
    check(st["next_action"] == "continue_delivery", "state.json não foi atualizado")
    check(st["scores"]["delivery_success"] == 0, "state não tem delivery_success")
    check(st["scores"]["needs_human_ui_review"] is True,
          "ui declarada e não verificável deveria pedir review humano")
    check(any("acceptance:" in i for i in st["open_issues"]), f"open_issues vazio: {st['open_issues']}")

    hist = graph.delivery_history(p.name)
    check(len(hist) >= 1, "post_work não gravou delivery_event no graph")
    check(hist[0]["next_action"] == "continue_delivery", "graph não tem o next_action")


def test_ui_declarada_sem_suite_falha_fechado():
    """`ui = true` mas suite indisponível: não fecha sozinho — falha fechado.

    O gate automático de UI vive em test_ui_gate.py. Aqui o que se prova é o
    contrário: quando a máquina NÃO consegue verificar a UI, ela não finge que
    está tudo bem — devolve para o humano.
    """
    p = fresh(ui_enabled=False)
    (p / "site" / "precos.html").write_text(
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
        "<title>P</title></head><body><h1>Planos</h1>"
        '<div class="plano">A</div><div class="plano">B</div><div class="plano">C</div>'
        "</body></html>",
        encoding="utf-8",
    )
    # brief sem itens abertos, tudo verde, mas o projeto é UI
    (p / "sessions" / "s001" / "brief.md").write_text("# s001\n\n## Aceite\n\n- [x] pronto\n",
                                                      encoding="utf-8")
    r = delivery.post_work(p, "s001", actor="teste")
    check(r["delivery_success"] == 1, "tudo verde deveria dar delivery_success 1")
    check(r["next_action"] == "await_renan",
          f"UI não verificável não pode fechar sozinha, veio {r['next_action']}")
    check(any("needs_human_ui_review" in i for i in r["open_issues"]),
          "faltou marcar needs_human_ui_review quando a UI não é verificável")


def test_promote_sobe_a_barra():
    p = fresh()
    movidos = delivery.promote_checks(p, "s001", actor="renan")
    check(len(movidos) == 2, f"deveria promover 2 checks, moveu {movidos}")
    check(delivery.acceptance_checks(p, "s001") == [], "acceptance deveria ficar vazia")
    check(len(delivery.regression_checks(p)) == 5, "regression deveria crescer para 5")
    v = delivery.verify(p, "s001")
    check(not v["governance_violations"],
          f"promoção reescreve o manifest, não pode violar: {v['governance_violations']}")


def test_eixos_nao_se_misturam():
    """Entrega e laboratório vivem em tabelas diferentes. Este é o ponto."""
    p = fresh()
    graph.record_run(task_id="task_01", harness_version="v0.2", suite="fixed", success=1,
                     seconds=10.0, tokens=100, cost_usd=0.01)
    delivery.post_work(p, "s001", actor="teste")
    runs = graph.runs_for_version("v0.2")
    check(all("delivery_success" not in r for r in runs), "run de lab contaminada com entrega")
    hist = graph.delivery_history(p.name)
    check(all("harness_version" not in h for h in hist), "entrega contaminada com métrica de lab")
    check(len(hist) >= 1 and len(runs) >= 1, "os dois eixos deveriam ter registro próprio")


def test_resume_le_o_state():
    p = fresh()
    delivery.post_work(p, "s001", actor="teste")
    txt = delivery.resume(p, "s001")
    for esperado in ("s001", "next_action", "open issues", "brief"):
        check(esperado in txt, f"resume não mostra {esperado!r}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as e:  # noqa: BLE001
            FAILS.append(f"{t.__name__} estourou: {type(e).__name__}: {e}")
        if len(FAILS) == before:
            print(f"OK {t.__name__}")
    if FAILS:
        print("\nFALHOU:\n  - " + "\n  - ".join(FAILS))
        return 1
    print(f"\n{len(tests)} testes de entrega verdes.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
