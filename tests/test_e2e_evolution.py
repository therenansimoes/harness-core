"""Prova de vida: ciclo COMPLETO de auto-evolução, só com o que já existe.

Quatro fios, todos com backend mock e árvore em tmp_path:
1. research: ledger com falha repetida -> proposta -> skill escrita ->
   selecionada por kind -> injetada no prompt renderizado.
2. codegen: mutação em plugins/** julgada por exame injetado — KEEP com
   linhagem encadeada por parent, DISCARD restaura byte a byte.
3. guardrails: harness/ruler/** barrado pelo genoma REAL do repo; falha vira
   exame de quarentena que o mesmo loader do `harness run` carrega.
4. topologia declarada: nó `reflect` inserido por dado aparece na run mock.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from harness import cli
from harness.genome.genome import load as load_genome
from harness.graph.run_graph import run_unit
from harness.improve import codegen, mutate
from harness.improve.research import apply_research, propose_research
from harness.improve.synthesize import synthesize_from_failures
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.skills import render_prompt, select_skills
from harness.types import RunRow

REPO = Path(__file__).parent.parent
ECHO_FIXTURE = Path(__file__).parent / "fixtures" / "echo"

# Genoma REAL do repo: o teste prova que skills/** e plugins/** são mutáveis
# e harness/ruler/** é imutável na configuração de verdade, não numa sintética.
REAL_GENOME = load_genome(REPO / "config" / "genome.toml")

SEED = "def collect(path):\n    return {}\n"
V1 = "def collect(path):\n    return {'files': 1}\n"
V2 = "def collect(path):\n    return {'files': 2}\n"


def _row(
    kind: str,
    ok: bool = False,
    exit_reason: str = "verify_failed:exit=1",
    unit_id: str = "u",
    run_id: str = "r",
) -> RunRow:
    return RunRow(
        run_id=run_id,
        unit_id=unit_id,
        project=None,
        backend="mock",
        model=None,
        tier="t0",
        kind=kind,
        ok=ok,
        exit_reason=exit_reason,
        sec_total=10.0,
        sec_provision=0.0,
        cost_usd=None,
        intervention=False,
        created_at=store.now_iso(),
    )


# --- 1. ciclo research: falha repetida -> skill -> prompt -----------------------


def test_ciclo_research_falha_vira_skill_no_prompt(tmp_path):
    # Ledger fake: infra falha repetido, code vai bem — gradiente aponta infra.
    history = [_row("infra", exit_reason="timeout:600s")] * 3 + [_row("code", ok=True)] * 4

    proposta = propose_research(history)
    assert proposta is not None
    assert proposta.kind == "infra"

    record = apply_research(proposta, backend="mock", root=tmp_path, genome=REAL_GENOME)
    assert record.skill_path == proposta.target_file
    assert (tmp_path / record.skill_path).is_file()

    # A skill recém-escrita é selecionável pelo kind que falhava…
    skills_root = tmp_path / "skills"
    selecionadas = select_skills("infra", skills_root)
    assert proposta.slug in {s.name for s in selecionadas}
    # …e NÃO vaza para outro kind.
    assert proposta.slug not in {s.name for s in select_skills("code", skills_root)}

    # render_prompt injeta nome e corpo (mock ecoou o prompt de pesquisa).
    prompt = render_prompt(selecionadas)
    assert f"### {proposta.slug}" in prompt
    assert "timeout" in prompt


def test_research_sem_gradiente_nao_escreve_nada(tmp_path):
    assert propose_research([_row("infra", ok=True)] * 5) is None
    assert not (tmp_path / "skills").exists()


# --- 2. ciclo codegen: KEEP com linhagem, DISCARD restaura ----------------------


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    plug = tmp_path / "plugins" / "kpi_lines.py"
    plug.parent.mkdir(parents=True)
    plug.write_text(SEED, encoding="utf-8")
    return tmp_path


def test_ciclo_codegen_keep_depois_discard(root: Path):
    alvo = root / "plugins" / "kpi_lines.py"

    # 1a mutação: exame passa -> KEEP, código novo fica.
    m1 = codegen.propose_code_mutation(alvo, V1, root=root, genome=REAL_GENOME)
    assert codegen.judge_code_mutation(m1, run_exam=lambda: True, root=root) == codegen.KEEP
    assert alvo.read_text(encoding="utf-8") == V1

    # 2a mutação, filha da 1a: exame falha -> DISCARD restaura a geração KEEP.
    m2 = codegen.propose_code_mutation(
        alvo, V2, parent_id=m1.mutation_id, root=root, genome=REAL_GENOME
    )
    assert alvo.read_text(encoding="utf-8") == V2
    assert codegen.judge_code_mutation(m2, run_exam=lambda: False, root=root) == codegen.DISCARD
    assert alvo.read_text(encoding="utf-8") == V1

    # Linhagem: duas propostas (a segunda aponta a primeira como parent)
    # e um evento de veredito por julgamento.
    linhas = [
        json.loads(ln)
        for ln in (root / "data" / "lineage.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    propostas = [ev for ev in linhas if "target" in ev]
    vereditos = [ev for ev in linhas if "verdict" in ev]
    assert [ev["id"] for ev in propostas] == [m1.mutation_id, m2.mutation_id]
    assert propostas[0]["parent_id"] is None
    assert propostas[1]["parent_id"] == m1.mutation_id
    assert {ev["target"] for ev in propostas} == {"plugins/kpi_lines.py"}
    assert {(v["id"], v["verdict"]) for v in vereditos} == {
        (m1.mutation_id, codegen.KEEP),
        (m2.mutation_id, codegen.DISCARD),
    }


# --- 3. guardrails de ponta a ponta ---------------------------------------------


def test_mutacao_na_regua_barrada_pelo_genoma_real(tmp_path):
    alvo = tmp_path / "harness" / "ruler" / "wilson.py"

    with pytest.raises(mutate.GenomeViolation) as exc:
        codegen.propose_code_mutation(alvo, V1, root=tmp_path, genome=REAL_GENOME)

    assert any("immutable" in v for v in exc.value.violations)
    assert not alvo.exists()
    assert not (tmp_path / "data" / "lineage.jsonl").exists()


def test_falha_vira_exame_de_quarentena_carregavel(tmp_path):
    units = tmp_path / "held_in"
    (units / "u-falho").mkdir(parents=True)
    (units / "u-falho" / "unit.toml").write_text(
        'id = "u-falho"\nkind = "code"\nprompt = "Conserte o u-falho."\nverify_cmd = "true"\n',
        encoding="utf-8",
    )
    out = tmp_path / "benchmarks" / "quarantine"

    created = synthesize_from_failures(
        [_row("code", unit_id="u-falho", run_id="run-9", exit_reason="verify_failed")],
        out_dir=out,
        units_dir=units,
    )

    assert created == [out / "u-falho"]
    data = tomllib.loads((out / "u-falho" / "unit.toml").read_text(encoding="utf-8"))
    assert data["origin"]["run_id"] == "run-9"
    # o exame sintetizado carrega pelo MESMO loader do `harness run`.
    unit = cli.load_unit(out / "u-falho")
    assert unit.id == "u-falho"


# --- 4. topologia declarada com nó reflect --------------------------------------

REFLECT_TOML = """\
nodes = ["plan","reflect","route","provision","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
edges = [
  ["START","plan"], ["plan","reflect"], ["reflect","route"],
  ["route","provision"], ["provision","execute"], ["execute","verify"],
  ["verify","measure"], ["measure","gate"], ["retry","route"],
  ["accept","record"], ["escalate","record"], ["revert","record"],
  ["record","END"],
]
"""


def test_topologia_com_reflect_roda_na_run_mock(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data_dir))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "topology.toml").write_text(REFLECT_TOML, encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    final = run_unit(ECHO_FIXTURE, "mock", None, data_dir, thread_id="t-e2e-reflect")

    nodes = [e["node"] for e in final["events"]]
    assert final["decision"].action == "accept"
    assert "reflect" in nodes
    assert nodes.index("plan") < nodes.index("reflect") < nodes.index("route")


# --- sanidade cruzada: o genoma real cobre tudo que os ciclos acima usaram ------


def test_genoma_real_declara_as_zonas_dos_ciclos(tmp_path):
    class _T:
        def __init__(self, f: str):
            self.target_file = f

    assert mutate.check(_T("skills/x.md"), root=tmp_path, genome=REAL_GENOME) == []
    assert mutate.check(_T("plugins/x.py"), root=tmp_path, genome=REAL_GENOME) == []
    assert (
        mutate.check(_T("benchmarks/quarantine/u/unit.toml"), root=tmp_path, genome=REAL_GENOME)
        == []
    )
    assert mutate.check(_T("harness/ruler/wilson.py"), root=tmp_path, genome=REAL_GENOME) != []
