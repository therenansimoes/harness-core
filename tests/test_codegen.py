"""Testes da ação codegen: zona de código mutável plugins/**."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.genome.genome import Genome
from harness.improve import codegen, mutate

GENOME = Genome(immutable=("harness/**", "uv.lock"), mutable=("plugins/**",))

SEED = "def collect(path):\n    return {}\n"
VALID = "def collect(path):\n    return {'files': 0, 'lines': 0}\n"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    plug = tmp_path / "plugins" / "kpi_lines.py"
    plug.parent.mkdir(parents=True)
    plug.write_text(SEED, encoding="utf-8")
    return tmp_path


def test_recusa_fora_de_plugins(root: Path) -> None:
    alvo = root / "harness" / "improve" / "x.py"
    with pytest.raises(mutate.GenomeViolation):
        codegen.propose_code_mutation(alvo, VALID, root=root, genome=GENOME)
    assert not alvo.exists()
    assert not (root / "data" / "lineage.jsonl").exists()


def test_recusa_sintaxe_invalida_sem_escrever(root: Path) -> None:
    alvo = root / "plugins" / "kpi_lines.py"
    antes = alvo.read_bytes()
    with pytest.raises(codegen.CodegenError):
        codegen.propose_code_mutation(alvo, "def x(:\n", root=root, genome=GENOME)
    assert alvo.read_bytes() == antes
    assert not (root / "data" / "lineage.jsonl").exists()


def test_keep_mantem_e_linhagem_com_parent(root: Path) -> None:
    alvo = root / "plugins" / "kpi_lines.py"
    m = codegen.propose_code_mutation(
        alvo, VALID, parent_id="abc123", root=root, genome=GENOME
    )
    veredito = codegen.judge_code_mutation(m, run_exam=lambda: True, root=root)
    assert veredito == codegen.KEEP
    assert alvo.read_text(encoding="utf-8") == VALID

    linhas = [
        json.loads(l)
        for l in (root / "data" / "lineage.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert linhas[0] == {
        "id": m.mutation_id,
        "parent_id": "abc123",
        "target": "plugins/kpi_lines.py",
        "ts": m.ts,
    }
    # evento de veredito: mesma id, chave 'verdict', sem 'target'
    assert linhas[1]["id"] == m.mutation_id
    assert linhas[1]["verdict"] == codegen.KEEP
    assert "target" not in linhas[1]


def test_discard_restaura_byte_a_byte(root: Path) -> None:
    alvo = root / "plugins" / "kpi_lines.py"
    antes = alvo.read_bytes()
    m = codegen.propose_code_mutation(alvo, VALID, root=root, genome=GENOME)
    assert alvo.read_bytes() != antes
    veredito = codegen.judge_code_mutation(m, run_exam=lambda: False, root=root)
    assert veredito == codegen.DISCARD
    assert alvo.read_bytes() == antes

    ultima = json.loads(
        (root / "data" / "lineage.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert ultima["id"] == m.mutation_id
    assert ultima["verdict"] == codegen.DISCARD
    assert "target" not in ultima


def test_discard_de_arquivo_novo_apaga(root: Path) -> None:
    alvo = root / "plugins" / "novo.py"
    m = codegen.propose_code_mutation(alvo, VALID, root=root, genome=GENOME)
    assert alvo.exists()
    assert codegen.judge_code_mutation(m, run_exam=lambda: False, root=root) == codegen.DISCARD
    assert not alvo.exists()


def test_action_exposta_sem_editar_target() -> None:
    a = codegen.action()
    assert a.name == codegen.ACTION == "codegen"
    assert a.propose is codegen.propose_code_mutation
    assert a.apply is codegen.judge_code_mutation
