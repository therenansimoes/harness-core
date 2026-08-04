#!/usr/bin/env python3
"""Testa autopilot.py (D6) — só a parte determinística, sem rede e sem API.

O loop em si é aceite de shell (tests/acceptance/d6_autopilot.sh). O que está
aqui é o que precisa ser verdade ANTES de o loop rodar sozinho a noite inteira:
classificação estável das notes REAIS do results.tsv, desempate determinístico,
proposta que o evolve.py aceita e que o gate de genoma aprova, e um snapshot
que restaura byte a byte.

    python3 -m pytest tests/test_autopilot.py -q
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import autopilot  # noqa: E402
import evolve  # noqa: E402

CATALOG = autopilot.load_catalog()


def row(notes: str, success: str = "0", **extra) -> dict:
    return {"success": success, "notes": notes, **extra}


# ------------------------------------------------------------------ classify


@pytest.mark.parametrize(
    "notes,expected",
    [
        ("error_max_turns", "max_turns"),
        ("max_turns", "max_turns"),
        ("tamper:test_file_modified", "tamper_tests"),
        ("tamper:verify_modified", "tamper_tests"),
        ("verify:ksum_success", "verify_env"),
        ("timeout", "agent_timeout"),
        ("cli_exit_1:", "cli_exit"),
        ("", ""),
        ("mock", ""),
    ],
)
def test_classify_notes_reais(notes, expected):
    assert autopilot.classify(notes, CATALOG) == expected


def test_classify_primeira_regra_vence():
    # nota composta: a ordem do catálogo decide, não o acaso do dict.
    assert autopilot.classify("error_max_turns; tamper:test_file_modified", CATALOG) == "max_turns"


def test_cli_exit_e_ignore_no_catalogo():
    rule = autopilot._rule_by_code("cli_exit", CATALOG)
    assert rule["action"] == "ignore"


# ------------------------------------------------------------- dominant_error


def test_dominant_error_conta_so_falhas_recentes():
    rows = [row("error_max_turns") for _ in range(3)] + [row("tamper:x")]
    rows.append(row("error_max_turns", success="1"))  # sucesso não conta
    code, n = autopilot.dominant_error(rows, CATALOG, window=10)
    assert (code, n) == ("max_turns", 3)


def test_dominant_error_janela_corta_o_antigo():
    rows = [row("tamper:x") for _ in range(5)] + [row("error_max_turns") for _ in range(2)]
    code, _ = autopilot.dominant_error(rows, CATALOG, window=2)
    assert code == "max_turns"


def test_dominant_error_empate_usa_ordem_do_catalogo():
    # max_turns (1ª regra) e tamper_tests (2ª) empatados em 2: vence a ordem.
    rows = [row("error_max_turns"), row("tamper:x"), row("error_max_turns"), row("tamper:x")]
    code, n = autopilot.dominant_error(rows, CATALOG, window=10)
    assert (code, n) == ("max_turns", 2)


def test_trace_any_refina_e_pode_zerar_a_regra(tmp_path):
    # verify: sem trace confirmando não conta — trace_any refina, não inventa.
    rows = [row("verify:ksum_success") for _ in range(3)]
    assert autopilot.error_counts(rows, CATALOG, root=tmp_path, window=10) == []

    trace = tmp_path / "runs" / "r1" / "trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"text": "ModuleNotFoundError: No module named pytest"}\n')
    rows = [row("verify:ksum_success; trace:runs/r1/trace.jsonl")]
    assert autopilot.error_counts(rows, CATALOG, root=tmp_path, window=10) == [("verify_env", 1)]


def test_trace_signals_conta_por_padrao(tmp_path):
    p = tmp_path / "trace.jsonl"
    p.write_text('{"a": "command not found"}\n{"b": "ModuleNotFoundError"}\n{"c": "ok"}\n')
    c = autopilot.trace_signals(p, ["command not found", "ModuleNotFoundError", "nada"])
    assert c["command not found"] == 1 and c["ModuleNotFoundError"] == 1
    assert "nada" not in c


def test_trace_signals_arquivo_ausente_vale_zero(tmp_path):
    assert autopilot.trace_signals(tmp_path / "nope.jsonl", ["x"]) == autopilot.Counter()


# -------------------------------------------------------------- render_proposal


@pytest.fixture()
def fake_root(tmp_path):
    """Raiz mínima: agent.py real (é o alvo das duas mutações) + versão."""
    shutil.copy2(REPO / "agent.py", tmp_path / "agent.py")
    (tmp_path / "harness_version.txt").write_text("v0.4\n")
    (tmp_path / "evolution").mkdir()
    return tmp_path


def _rule(code: str) -> dict:
    return autopilot._rule_by_code(code, CATALOG)


def test_render_proposal_bump_int_evolve_aceita(fake_root):
    path = autopilot.render_proposal(_rule("max_turns"), root=fake_root)
    meta = evolve.parse_proposal(path)
    assert meta["from_version"] == "v0.4"
    assert meta["to_version"].startswith("v0.4+auto")
    assert meta["change"]["file"] == "agent.py"
    assert meta["change"]["old"] == "MAX_TURNS = 30\n"
    assert meta["change"]["new"] == "MAX_TURNS = 45\n"  # 30 * 1.5, abaixo do teto 60
    assert not evolve.genome_violations(evolve.proposal_files(meta))


def test_render_proposal_bump_int_respeita_o_teto(fake_root):
    src = (fake_root / "agent.py").read_text().replace("MAX_TURNS = 30", "MAX_TURNS = 50")
    (fake_root / "agent.py").write_text(src)
    path = autopilot.render_proposal(_rule("max_turns"), root=fake_root)
    assert evolve.parse_proposal(path)["change"]["new"] == "MAX_TURNS = 60\n"

    src = src.replace("MAX_TURNS = 50", "MAX_TURNS = 60")
    (fake_root / "agent.py").write_text(src)
    with pytest.raises(autopilot.SkipProposal):
        autopilot.render_proposal(_rule("max_turns"), root=fake_root)


def test_render_proposal_append_prompt_aplica_na_sandbox(fake_root):
    path = autopilot.render_proposal(_rule("tamper_tests"), root=fake_root)
    meta = evolve.parse_proposal(path)
    change = meta["change"]
    assert change["old"] == "# --- autopilot:prompt_tail ---\n"
    assert not evolve.genome_violations(evolve.proposal_files(meta))

    # a mudança tem que aplicar de verdade (old único) e continuar sendo Python
    # válido com a sentinela intacta para a PRÓXIMA proposta.
    evolve.apply_change(fake_root, change)
    novo = (fake_root / "agent.py").read_text()
    assert novo.count("# --- autopilot:prompt_tail ---") == 1
    assert "SYSTEM_PROMPT += " in novo
    compile(novo, "agent_patched", "exec")  # sintaxe válida depois do patch


def test_render_proposal_ancora_ausente_e_skip(fake_root):
    (fake_root / "agent.py").write_text("# sem âncora nenhuma\n")
    with pytest.raises(autopilot.SkipProposal):
        autopilot.render_proposal(_rule("max_turns"), root=fake_root)
    with pytest.raises(autopilot.SkipProposal):
        autopilot.render_proposal(_rule("tamper_tests"), root=fake_root)


def test_sentinela_existe_e_e_unica_no_agent_real():
    src = (REPO / "agent.py").read_text()
    assert src.count("# --- autopilot:prompt_tail ---") == 1


# ---------------------------------------------------------------- snapshot


def test_snapshot_restaura_bytes_identicos(fake_root):
    (fake_root / "profile.py").write_text("# profile\n")
    original = (fake_root / "agent.py").read_bytes()
    snap = autopilot.snapshot_genome("sess-test", root=fake_root)

    (fake_root / "agent.py").write_bytes(b"# genoma corrompido\n")
    (fake_root / "harness_version.txt").write_text("v9.9\n")

    restored = autopilot.restore_genome(snap, root=fake_root)
    assert "agent.py" in restored and "harness_version.txt" in restored
    assert (fake_root / "agent.py").read_bytes() == original
    assert (fake_root / "harness_version.txt").read_text().strip() == "v0.4"


def test_snapshot_nao_usa_git(fake_root):
    snap = autopilot.snapshot_genome("sess-test", root=fake_root)
    assert not (snap / ".git").exists()
    assert (snap / "agent.py").exists()


# --------------------------------------------------------------------- custo


def test_spent_usd_conta_so_as_linhas_novas(tmp_path, monkeypatch):
    """O teto é do gasto DESTA sessão: o histórico do arquivo não pode
    consumir o budget antes do primeiro passo."""
    p = tmp_path / "results.tsv"
    p.write_text("timestamp\tcost_usd\tnotes\n" + "t\t0.5000\tvelho\n" * 4)
    monkeypatch.setattr(autopilot, "results_files", lambda: [p])

    s = autopilot.State(
        session="s",
        project=None,
        wall_s=60,
        budget=1.0,
        max_iterations=1,
        self_every=3,
        probation_runs=3,
    )
    s.baseline_lines = autopilot.baseline_lines()
    assert autopilot.spent_usd(s) == 0.0

    with p.open("a") as fh:
        fh.write("t\t0.2500\tnovo\nt\t0.1000\tnovo\n")
    assert autopilot.spent_usd(s) == pytest.approx(0.35)


# ----------------------------------------------------------------- probation


@pytest.fixture()
def probation(tmp_path, monkeypatch):
    """State + genoma falso + rows controlados: exercita o revert sem rodar
    suite nenhuma (é a decisão que está sob teste, não a execução)."""
    shutil.copy2(REPO / "agent.py", tmp_path / "agent.py")
    (tmp_path / "harness_version.txt").write_text("v0.4\n")
    snap = autopilot.snapshot_genome("sess-prob", root=tmp_path)

    monkeypatch.setattr(autopilot, "DECISIONS", tmp_path / "decisions")
    monkeypatch.setattr(autopilot, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(autopilot.graph, "record_governance_event", lambda **kw: kw and 1)
    real_restore = autopilot.restore_genome
    monkeypatch.setattr(
        autopilot, "restore_genome", lambda snap, root=None: real_restore(snap, tmp_path)
    )

    s = autopilot.State(
        session="sess-prob",
        project="demo",
        wall_s=60,
        budget=0,
        max_iterations=5,
        self_every=3,
        probation_runs=2,
    )
    return s, snap, tmp_path


def _arm(s, snap, pre, post_rows, monkeypatch):
    s.probation = {
        "snap": str(snap),
        "left": 1,
        "code": "max_turns",
        "pid": "auto-x",
        "project": "demo",
        "idx": len(pre),
        "pre": pre,
        "n_signal": 3,
    }
    monkeypatch.setattr(autopilot, "project_rows", lambda name: pre + post_rows)


def test_probation_mantem_quando_nada_piorou(probation, monkeypatch):
    s, snap, _ = probation
    pre = [row("", success="1"), row("erro")]
    _arm(s, snap, pre, [row("", success="1"), row("", success="1")], monkeypatch)
    assert autopilot.probation_check(s) == "keep"
    assert s.blocked_codes == set()


def test_probation_reverte_quando_success_zera(probation, monkeypatch):
    s, snap, _ = probation
    pre = [row("", success="1"), row("", success="1")]
    _arm(s, snap, pre, [row("erro"), row("erro")], monkeypatch)
    assert autopilot.probation_check(s) == "revert"
    assert "max_turns" in s.blocked_codes  # não é reproposto na sessão
    assert s.probation is None
    assert (autopilot.DECISIONS / "auto-x-revert.md").exists()


def test_probation_reverte_com_tamper_novo(probation, monkeypatch):
    s, snap, _ = probation
    pre = [row("", success="1")]
    # success continua bom: o tamper sozinho manda reverter.
    _arm(s, snap, pre, [row("tamper:test_file_modified", success="1")], monkeypatch)
    assert autopilot.probation_check(s) == "revert"
    doc = (autopilot.DECISIONS / "auto-x-revert.md").read_text()
    assert "tamper novo" in doc


def test_probation_so_julga_ao_zerar_a_contagem(probation, monkeypatch):
    s, snap, _ = probation
    pre = [row("", success="1")]
    _arm(s, snap, pre, [row("erro")], monkeypatch)
    s.probation["left"] = 2
    assert autopilot.probation_check(s) is None  # ainda observando
    assert s.probation["left"] == 1


def test_probation_reverte_por_kpi_worse(probation, monkeypatch):
    s, snap, _ = probation
    kpi_pre = [dict(row("", success="1"), kpis='{"pass_rate": 1.0}') for _ in range(3)]
    kpi_post = [dict(row("", success="1"), kpis='{"pass_rate": 0.2}') for _ in range(3)]
    _arm(s, snap, kpi_pre, kpi_post, monkeypatch)
    s.probation["idx"] = len(kpi_pre)
    assert autopilot.probation_check(s) == "revert"
    assert "pass_rate" in (autopilot.DECISIONS / "auto-x-revert.md").read_text()


# ------------------------------------------------------------------ genoma


def test_autopilot_e_imutavel_no_genoma():
    """Quem propõe não se muda: proposta que toca o loop/catálogo é violação."""
    bad = evolve.genome_violations(["autopilot.py", "mockagent.py", "evolution/catalog.toml"])
    assert sorted(bad) == ["autopilot.py", "evolution/catalog.toml", "mockagent.py"]


# ------------------------------------------------------------------- router


def test_step_project_loga_tier(tmp_path, monkeypatch):
    """O JSONL da sessão precisa dizer QUEM rodou a unidade — sem isso o custo
    por tier vira arqueologia de results.tsv."""
    monkeypatch.setattr(autopilot, "LOG_DIR", tmp_path / "log")

    class FakeProject:
        LAST_RUN = {
            "unit": "0001",
            "tier": "haiku",
            "class": "haiku",
            "score": 0,
            "attempt": 0,
            "success": False,
            "escalated": True,
        }

        @staticmethod
        def try_run_one(name, keep):
            return "ran"

    monkeypatch.setattr(autopilot, "_project", lambda: FakeProject)
    monkeypatch.setattr(autopilot, "spent_usd", lambda s: 0.0)

    s = autopilot.State(
        session="s-tier",
        project="demo",
        wall_s=60,
        budget=1.0,
        max_iterations=1,
        self_every=3,
        probation_runs=1,
    )
    assert autopilot.step_project(s) == "ran"

    ev = json.loads((tmp_path / "log" / "s-tier.jsonl").read_text().splitlines()[-1])
    assert ev["kind"] == "project"
    assert ev["tier"] == "haiku"
    assert ev["attempt"] == 0
    assert ev["escalated"] is True
