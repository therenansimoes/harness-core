"""Testes da ação prompt: PromptBreeder-lite sobre prompts/**."""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest

from harness.genome.genome import Genome
from harness.improve import mutate, prompt_evolve

GENOME = Genome(immutable=("harness/**", "uv.lock"), mutable=("prompts/**",))

SEED_TEXT = (
    "# Executor\n"
    "\n"
    "Intro curta.\n"
    "\n"
    "## Regras\n"
    "\n"
    "- Diff mínimo.\n"
    "- Rode o verify_cmd antes de declarar pronto.\n"
    "\n"
    "## Diretivas\n"
    "\n"
    "- Prefira editar arquivo existente a reescrever do zero.\n"
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    p = tmp_path / "prompts" / "executor.md"
    p.parent.mkdir(parents=True)
    p.write_text(SEED_TEXT, encoding="utf-8")
    return tmp_path


def test_operadores_deterministicos_com_rng_seedado() -> None:
    assert len(prompt_evolve.OPERATORS) >= 3
    for nome, fn in prompt_evolve.OPERATORS.items():
        a = fn(SEED_TEXT, Random(7))
        b = fn(SEED_TEXT, Random(7))
        assert a == b, f"operador {nome} não é determinístico com mesmo seed"


def test_add_directive_adiciona_do_banco() -> None:
    out = prompt_evolve.OPERATORS["add_directive"](SEED_TEXT, Random(3))
    novas = [d for d in prompt_evolve.DIRECTIVES if d in out and d not in SEED_TEXT]
    assert len(novas) == 1


def test_drop_directive_remove_do_banco() -> None:
    out = prompt_evolve.OPERATORS["drop_directive"](SEED_TEXT, Random(3))
    assert "- Prefira editar arquivo existente a reescrever do zero." not in out


def test_drop_shortest_section_encurta() -> None:
    out = prompt_evolve.OPERATORS["drop_shortest_section"](SEED_TEXT, Random(0))
    assert len(out) < len(SEED_TEXT)
    assert out.startswith("# Executor\n")


def test_reorder_preserva_conteudo() -> None:
    out = prompt_evolve.OPERATORS["reorder_sections"](SEED_TEXT, Random(1))
    assert sorted(out.splitlines()) == sorted(SEED_TEXT.splitlines())


def test_genoma_recusa_fora_de_prompts(root: Path) -> None:
    alvo = root / "config" / "x.md"
    alvo.parent.mkdir(parents=True)
    alvo.write_text("oi\n", encoding="utf-8")
    with pytest.raises(mutate.GenomeViolation):
        prompt_evolve.propose_prompt_mutation(
            alvo, "add_directive", Random(0), root=root, genome=GENOME
        )
    assert alvo.read_text(encoding="utf-8") == "oi\n"


def test_operador_desconhecido(root: Path) -> None:
    alvo = root / "prompts" / "executor.md"
    with pytest.raises(KeyError):
        prompt_evolve.propose_prompt_mutation(
            alvo, "nao_existe", Random(0), root=root, genome=GENOME
        )


def test_propose_nao_escreve(root: Path) -> None:
    alvo = root / "prompts" / "executor.md"
    antes = alvo.read_bytes()
    m = prompt_evolve.propose_prompt_mutation(
        alvo, "add_directive", Random(0), root=root, genome=GENOME
    )
    assert alvo.read_bytes() == antes
    assert m.after_text != m.before_text


def test_apply_e_revert_byte_a_byte(root: Path) -> None:
    alvo = root / "prompts" / "executor.md"
    original = alvo.read_bytes()
    m = prompt_evolve.propose_prompt_mutation(
        alvo, "add_directive", Random(0), root=root, genome=GENOME
    )
    prompt_evolve.apply_prompt_mutation(m, root=root)
    assert alvo.read_text(encoding="utf-8") == m.after_text
    assert alvo.read_bytes() != original

    prompt_evolve.revert_prompt_mutation(m, root=root)
    assert alvo.read_bytes() == original


def test_action_expoe_par() -> None:
    a = prompt_evolve.action()
    assert a.name == "prompt"
    assert a.propose is prompt_evolve.propose_prompt_mutation
    assert a.apply is prompt_evolve.apply_prompt_mutation


def test_executor_md_do_repo_parseia() -> None:
    repo = Path(__file__).resolve().parents[1]
    texto = (repo / "prompts" / "executor.md").read_text(encoding="utf-8")
    assert texto.strip()
    assert not texto.lstrip().startswith("---"), "executor.md é markdown puro, sem frontmatter"
    assert "verify_cmd" in texto
    assert "prompt_evolve" in texto  # comentário de zona mutável no topo
