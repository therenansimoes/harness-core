#!/usr/bin/env python3
"""Testa o genoma 360° do evolve.py: o que pode mudar, e o que nunca pode.

Sem API e sem suite: o gate de genoma é anterior a qualquer run — proposta que
toca a régua não chega a ser julgada pelo mérito. Quem julga não se muda.

    python3 -m pytest tests/test_genome.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
os.environ["HARNESS_MOCK_AGENT"] = "1"

PROPOSAL = """+++
id = "{pid}"
from_version = "vA"
to_version = "vB"
hypothesis = "Candidata sintética do teste de genoma {pid}."

{changes}
+++

Proposta sintética do teste.
"""

CHANGE = """[[change]]
file = "{file}"
old = '''{old}'''
new = '''{new}'''
"""

HEADER = (
    "timestamp\tharness_version\tbackend\tmodel\tsuite\ttask_id\tsuccess\t"
    "seconds\ttokens\tcost_usd\tturns\tnotes"
)


def make_repo(pid: str, changes: list[tuple[str, str, str]]) -> tuple[Path, Path]:
    """Cópia mínima do repo em tmp + a proposta escrita. Devolve (tmp, proposal)."""
    tmp = Path(tempfile.mkdtemp(prefix=f"genome_{pid}_"))
    for f in ("agent.py", "run_task.py", "score.py", "graph.py", "evolve.py"):
        shutil.copy2(REPO / f, tmp / f)
    shutil.copytree(REPO / "tasks", tmp / "tasks")
    (tmp / "harness_version.txt").write_text("vA\n")
    (tmp / "evolution" / "proposals").mkdir(parents=True)
    (tmp / "evolution" / "decisions").mkdir(parents=True)
    shutil.copy2(REPO / "evolution" / "genome.toml", tmp / "evolution" / "genome.toml")
    (tmp / "results.tsv").write_text(HEADER + "\n")

    prop = tmp / "evolution" / "proposals" / f"{pid}.md"
    prop.write_text(PROPOSAL.format(
        pid=pid,
        changes="\n".join(CHANGE.format(file=f, old=o, new=n) for f, o, n in changes),
    ))
    return tmp, prop


def load_evolve(tmp: Path):
    os.environ["HARNESS_GRAPH"] = str(tmp / "critique.db")
    sys.path.insert(0, str(tmp))
    for m in ("evolve", "score", "graph"):
        sys.modules.pop(m, None)
    import evolve  # noqa: E402
    return evolve


def unload(tmp: Path):
    if str(tmp) in sys.path:
        sys.path.remove(str(tmp))
    for m in ("evolve", "score", "graph"):
        sys.modules.pop(m, None)


def run_cycle(pid: str, changes: list[tuple[str, str, str]], suite_hook=None):
    """Roda um ciclo com a suite stubada. Devolve (rc, tmp, evolve, chamadas)."""
    tmp, prop = make_repo(pid, changes)
    evolve = load_evolve(tmp)
    calls = []

    def fake_suite(sandbox, suite, repeat):
        calls.append(suite)
        if suite_hook:
            suite_hook(tmp)
        return 0

    evolve.run_suite = fake_suite
    rc = evolve.cycle(prop, repeat=1, suite="fixed", force=False)
    return rc, tmp, evolve, calls


def jsonl_last(tmp: Path) -> dict:
    lines = (tmp / "evolution" / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


# ------------------------------------------------------- validação do toml


def test_load_genome_le_as_duas_listas():
    genome = None
    tmp = Path(tempfile.mkdtemp(prefix="genome_load_"))
    try:
        (tmp / "evolution").mkdir()
        shutil.copy2(REPO / "evolution" / "genome.toml", tmp / "evolution" / "genome.toml")
        shutil.copy2(REPO / "evolve.py", tmp / "evolve.py")
        for f in ("score.py", "graph.py"):
            shutil.copy2(REPO / f, tmp / f)
        evolve = load_evolve(tmp)
        genome = evolve.load_genome()
        assert "agent.py" in genome["mutable"]
        assert "score.py" in genome["immutable"]
        assert "evolution/genome.toml" in genome["immutable"]
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    assert genome is not None


def test_item_nas_duas_listas_e_erro_de_validacao():
    """mutable ∩ immutable não é ambiguidade a resolver — é config quebrada."""
    tmp = Path(tempfile.mkdtemp(prefix="genome_overlap_"))
    try:
        for f in ("evolve.py", "score.py", "graph.py"):
            shutil.copy2(REPO / f, tmp / f)
        (tmp / "evolution").mkdir()
        bad = tmp / "evolution" / "genome.toml"
        bad.write_text('mutable = ["agent.py", "score.py"]\nimmutable = ["score.py"]\n')
        evolve = load_evolve(tmp)
        with pytest.raises(evolve.InfraError) as e:
            evolve.load_genome()
        assert "score.py" in str(e.value)
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_genome_toml_ausente_e_fail_closed():
    tmp = Path(tempfile.mkdtemp(prefix="genome_missing_"))
    try:
        for f in ("evolve.py", "score.py", "graph.py"):
            shutil.copy2(REPO / f, tmp / f)
        evolve = load_evolve(tmp)
        with pytest.raises(evolve.InfraError):
            evolve.load_genome()
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ matching puro


VIOLA = [
    ("score.py", True),                        # blocklist: o juiz
    ("evolve.py", True),                       # blocklist: o loop
    ("safety.py", True),
    ("kpi.py", True),
    ("results.tsv", True),
    ("evolution/genome.toml", True),           # a própria régua do genoma
    ("benchmarks/sealed/task_s01/verify.py", True),   # glob de held-out
    ("benchmarks/held_in/task_h01/verify.py", True),
    ("tests/test_genome.py", True),
    (".harness/state.json", True),
    ("delivery.py", True),                     # nem blocklist nem mutable
    ("../fora.py", True),                      # escapa da raiz
    ("/etc/passwd", True),
    ("agent.py", False),
    ("prompts/x.md", False),
    ("prompts/sub/y.md", False),
    ("profile.py", False),
]


@pytest.mark.parametrize("path,viola", VIOLA, ids=[p for p, _ in VIOLA])
def test_genome_violations(path, viola):
    tmp = Path(tempfile.mkdtemp(prefix="genome_match_"))
    try:
        for f in ("evolve.py", "score.py", "graph.py"):
            shutil.copy2(REPO / f, tmp / f)
        (tmp / "evolution").mkdir()
        shutil.copy2(REPO / "evolution" / "genome.toml", tmp / "evolution" / "genome.toml")
        evolve = load_evolve(tmp)
        bad = evolve.genome_violations([path])
        assert bool(bad) is viola, f"{path}: violations={bad}"
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_blocklist_vence_mutable():
    """Mesmo que alguém amplie mutable, a blocklist decide."""
    tmp = Path(tempfile.mkdtemp(prefix="genome_wins_"))
    try:
        for f in ("evolve.py", "score.py", "graph.py"):
            shutil.copy2(REPO / f, tmp / f)
        (tmp / "evolution").mkdir()
        (tmp / "evolution" / "genome.toml").write_text(
            'mutable = ["*.py"]\nimmutable = ["score.py", "tests/**"]\n'
        )
        evolve = load_evolve(tmp)
        assert evolve.genome_violations(["agent.py"]) == []
        assert evolve.genome_violations(["score.py"]) == ["score.py"]
        assert evolve.genome_violations(["tests/test_x.py"]) == ["tests/test_x.py"]
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ gate no ciclo


def _assert_rejeitada(rc, tmp, evolve, calls, esperado_em_razao):
    assert rc == 1, "violação de genoma tem que virar DISCARD (exit 1)"
    assert calls == [], f"suite rodou numa proposta bloqueada: {calls}"
    entry = jsonl_last(tmp)
    assert entry["accepted"] is False
    assert entry["gates_failed"] == [evolve.GENOME_VIOLATION]
    assert entry["reason"].startswith(evolve.GENOME_VIOLATION)
    for frag in esperado_em_razao:
        assert frag in entry["reason"], entry["reason"]
    assert (tmp / "harness_version.txt").read_text().strip() == "vA"
    import graph
    decs = graph.recent_decisions(5)
    assert decs and decs[0]["outcome"] == "discard"
    assert evolve.GENOME_VIOLATION in decs[0]["reason"]


def test_proposta_em_score_py_e_rejeitada():
    rc, tmp, evolve, calls = run_cycle(
        "g_score", [("score.py", "MIN_N", "MIN_N2")]
    )
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, ["score.py"])
        md = (tmp / "evolution" / "decisions" / "g_score.md").read_text()
        assert "DISCARD" in md and evolve.GENOME_VIOLATION in md
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_proposta_mista_agent_mais_score_e_rejeitada():
    """Uma mudança legítima não carrega uma ilegítima de carona."""
    rc, tmp, evolve, calls = run_cycle(
        "g_misto",
        [("agent.py", "MAX_TURNS = 30", "MAX_TURNS = 25"), ("score.py", "MIN_N", "MIN_N2")],
    )
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, ["score.py"])
        assert "MAX_TURNS = 25" not in (tmp / "agent.py").read_text()
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_proposta_em_sealed_e_rejeitada():
    rc, tmp, evolve, calls = run_cycle(
        "g_sealed", [("benchmarks/sealed/task_s01/verify.py", "assert", "pass  #")]
    )
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, ["benchmarks/sealed/task_s01/verify.py"])
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_proposta_no_proprio_genome_toml_e_rejeitada():
    rc, tmp, evolve, calls = run_cycle(
        "g_toml", [("evolution/genome.toml", '"score.py",', '# "score.py",')]
    )
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, ["evolution/genome.toml"])
        assert '"score.py",' in (tmp / "evolution" / "genome.toml").read_text()
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_models_toml_e_router_sao_immutable():
    """D7: quem escolhe o modelo está fora do alcance de quem é escolhido —
    senão uma proposta se dá o tier caro e falseia o próprio A/B."""
    for pid, (f, old, new) in (
        ("g_router", ("router.py", "min_n", "min_n2")),
        ("g_models", ("evolution/models.toml", "prior_floor", "# prior_floor")),
    ):
        rc, tmp, evolve, calls = run_cycle(pid, [(f, old, new)])
        try:
            _assert_rejeitada(rc, tmp, evolve, calls, [f])
        finally:
            unload(tmp)
            shutil.rmtree(tmp, ignore_errors=True)


def test_note_py_is_immutable():
    """D8.1: quem MEDE a qualidade não se muda — uma proposta que reescreve o
    note.py se dá a própria nota."""
    rc, tmp, evolve, calls = run_cycle("g_note", [("note.py", "MIN_NOTES = 3", "MIN_NOTES = 1")])
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, ["note.py"])
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_project_notes_tsv_is_immutable():
    """A nota é do humano: o histórico dela não é editável por proposta."""
    f = "projects/website-faz-rogers/notes.tsv"
    rc, tmp, evolve, calls = run_cycle("g_notes_tsv", [(f, "0", "5")])
    try:
        _assert_rejeitada(rc, tmp, evolve, calls, [f])
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_genome_toml_editado_durante_a_run_e_tamper():
    """Tamper check: reescrever a régua no meio do exame também é violação."""
    def tamper(tmp):
        p = tmp / "evolution" / "genome.toml"
        p.write_text(p.read_text().replace('"score.py",', ""))

    rc, tmp, evolve, calls = run_cycle(
        "g_tamper", [("agent.py", "MAX_TURNS = 30", "MAX_TURNS = 25")], suite_hook=tamper
    )
    try:
        assert rc == 1
        assert calls == ["fixed"], "a suite deveria ter rodado antes do tamper ser pego"
        entry = jsonl_last(tmp)
        assert entry["accepted"] is False
        assert entry["gates_failed"] == [evolve.GENOME_VIOLATION]
        assert "evolution/genome.toml" in entry["reason"]
        assert (tmp / "harness_version.txt").read_text().strip() == "vA"
        assert "MAX_TURNS = 25" not in (tmp / "agent.py").read_text()
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_runtime_files_traz_o_que_o_genoma_nao_traz():
    """A sandbox precisa importar `safety`/`kpi`/`score` — genoma não é runtime."""
    tmp = Path(tempfile.mkdtemp(prefix="genome_runtime_"))
    try:
        for f in ("evolve.py", "score.py", "graph.py", "safety.py", "kpi.py", "agent.py"):
            shutil.copy2(REPO / f, tmp / f)
        (tmp / "evolution").mkdir()
        shutil.copy2(REPO / "evolution" / "genome.toml", tmp / "evolution" / "genome.toml")
        (tmp / "prompts").mkdir()
        (tmp / "prompts" / "x.md").write_text("texto\n")
        (tmp / "__pycache__").mkdir()
        (tmp / "__pycache__" / "lixo.py").write_text("# nada\n")
        evolve = load_evolve(tmp)
        files = evolve.runtime_files()
        for f in ("safety.py", "kpi.py", "score.py", "agent.py", "prompts/x.md",
                  "evolution/genome.toml"):
            assert f in files, f"{f} fora do runtime: {files}"
        assert not any("__pycache__" in f for f in files), files
        # o genoma continua sendo um subconjunto — runtime não é licença
        assert "safety.py" not in evolve.genome_files()
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_sandbox_tamper_pega_imutavel_e_ignora_genoma():
    tmp, prop = make_repo("g_tamper_unit", [("agent.py", "MAX_TURNS = 30", "MAX_TURNS = 25")])
    try:
        shutil.copy2(REPO / "safety.py", tmp / "safety.py")
        evolve = load_evolve(tmp)
        meta = evolve.parse_proposal(prop)
        sandbox, _ = evolve.build_sandbox("g_tamper_unit", "vB", meta["changes"])
        # a sandbox nasce limpa: o patch em agent.py é genoma, não tamper
        assert evolve.sandbox_tamper(sandbox) == []
        (sandbox / "safety.py").write_text(
            (sandbox / "safety.py").read_text() + "\n# editado pela candidata\n"
        )
        assert evolve.sandbox_tamper(sandbox) == ["safety.py"]
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_sandbox_suja_vira_discard_de_tamper():
    """Editar imutável na sandbox é DISCARD, não InfraError: veredito, não erro."""
    def sujar(tmp):
        p = tmp / "evolution" / "sandboxes" / "g_sandbox" / "safety.py"
        p.write_text(p.read_text() + "\n# a candidata mexeu no que a julga\n")

    tmp, prop = make_repo("g_sandbox", [("agent.py", "MAX_TURNS = 30", "MAX_TURNS = 25")])
    try:
        shutil.copy2(REPO / "safety.py", tmp / "safety.py")
        evolve = load_evolve(tmp)
        calls = []

        def fake_suite(sandbox, suite, repeat):
            calls.append(suite)
            sujar(tmp)
            return 0

        evolve.run_suite = fake_suite
        rc = evolve.cycle(prop, repeat=1, suite="fixed", force=False)

        assert rc == 1, "sandbox suja é DISCARD (exit 1), nunca exit 2"
        assert calls == ["fixed"], f"tamper tem que ser pego DEPOIS da suite: {calls}"
        entry = jsonl_last(tmp)
        assert entry["accepted"] is False
        assert entry["gates_failed"] == [evolve.SANDBOX_TAMPER]
        assert entry["reason"].startswith(evolve.SANDBOX_TAMPER)
        assert "safety.py" in entry["reason"]
        assert (tmp / "harness_version.txt").read_text().strip() == "vA"
        assert "MAX_TURNS = 25" not in (tmp / "agent.py").read_text()
        md = (tmp / "evolution" / "decisions" / "g_sandbox.md").read_text()
        assert "DISCARD" in md and evolve.SANDBOX_TAMPER in md
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_proposta_em_prompts_passa_o_gate_de_genoma():
    """`prompts/**` é genoma: o gate deixa passar (o mérito quem julga é o score)."""
    tmp, prop = make_repo("g_prompt", [("prompts/x.md", "velho", "novo")])
    try:
        (tmp / "prompts").mkdir()
        (tmp / "prompts" / "x.md").write_text("texto velho\n")
        evolve = load_evolve(tmp)
        meta = evolve.parse_proposal(prop)
        assert evolve.genome_violations(evolve.proposal_files(meta)) == []
        # e o arquivo entra na sandbox como genoma de verdade
        assert "prompts/x.md" in evolve.genome_files()
        sandbox, diff = evolve.build_sandbox("g_prompt", "vB", meta["changes"])
        assert (sandbox / "prompts" / "x.md").read_text() == "texto novo\n"
        assert "prompts/x.md" in diff
    finally:
        unload(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
