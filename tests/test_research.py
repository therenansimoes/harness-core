"""Ação research: proposta pelo ledger, skill bem-formada, genoma fail-closed."""

import tomllib

import pytest

from harness.genome.genome import Genome
from harness.improve import target as target_mod
from harness.improve.mutate import GenomeViolation
from harness.improve.research import (
    ResearchError,
    apply_research,
    propose_research,
    slugify,
)
from harness.ledger import store
from harness.types import RunRow

# Genoma sintético: o teste não edita config/genome.toml — `skills/**` entra
# na zona mutável por outro PR, aqui o genoma é passado direto.
GENOME_COM_SKILLS = Genome(immutable=("harness/ruler/**",), mutable=("skills/**",))
GENOME_SEM_SKILLS = Genome(immutable=("harness/ruler/**",), mutable=("config/*.toml",))


def row(kind: str, ok: bool = False, exit_reason: str = "verify_failed:exit=1") -> RunRow:
    return RunRow(
        run_id="r", unit_id="u", project=None, backend="mock", model=None,
        tier="t0", kind=kind, ok=ok, exit_reason=exit_reason, sec_total=10.0,
        sec_provision=0.0, cost_usd=None, intervention=False,
        created_at=store.now_iso(),
    )


def frontmatter_e_corpo(text: str) -> tuple[dict, str]:
    """Parse pelo contrato compartilhado: TOML entre os dois primeiros '---'."""
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines[1:].index("---") + 1
    return tomllib.loads("\n".join(lines[1:end])), "\n".join(lines[end + 1 :])


# --- proposta ------------------------------------------------------------------


def test_propose_escolhe_kind_com_mais_falhas():
    history = [row("code")] * 3 + [row("infra")] * 2 + [row("content", ok=True)] * 5

    p = propose_research(history)

    assert p is not None
    assert p.kind == "code"
    assert p.target_file == f"skills/{p.slug}.md"
    assert "verify_failed" in p.topic  # padrão cortado no ':', não a variante


def test_propose_empata_por_nome_do_kind():
    """Mesma evidência, mesma proposta — determinismo do pick_target vale aqui."""
    history = [row("infra")] * 2 + [row("code")] * 2

    assert propose_research(history).kind == "code"
    assert propose_research(list(reversed(history))).kind == "code"


def test_propose_sem_falha_repetida_devolve_none():
    """None = sem gradiente, não 'pesquise qualquer coisa'."""
    assert propose_research([]) is None
    assert propose_research([row("code", ok=True)] * 5) is None
    assert propose_research([row("code")]) is None  # 1 falha < MIN_FAIL_N


def test_propose_topico_explicito_ignora_ledger():
    p = propose_research([], topic="Timeout em builds Gradle", kind="infra")

    assert p.kind == "infra"
    assert p.slug == "timeout-em-builds-gradle"
    assert p.target_file == "skills/timeout-em-builds-gradle.md"


def test_slugify_normaliza():
    assert slugify("Açúcar & CI/CD!!") == "a-car-ci-cd"
    with pytest.raises(ResearchError):
        slugify("!!!")


# --- aplicação -----------------------------------------------------------------


def test_apply_escreve_skill_bem_formada(tmp_path):
    p = propose_research([], topic="timeout em builds gradle", kind="code")

    record = apply_research(p, backend="mock", root=tmp_path, genome=GENOME_COM_SKILLS)

    assert record.skill_path == "skills/timeout-em-builds-gradle.md"
    text = (tmp_path / record.skill_path).read_text(encoding="utf-8")
    fm, body = frontmatter_e_corpo(text)
    assert fm["name"] == "timeout-em-builds-gradle"
    assert fm["kinds"] == ["code"]
    assert fm["description"]
    assert body.strip()  # o mock ecoa o prompt: corpo não-vazio


def test_apply_kinds_vem_do_kind_pesquisado(tmp_path):
    history = [row("infra", exit_reason="timeout")] * 3
    p = propose_research(history)

    record = apply_research(p, backend="mock", root=tmp_path, genome=GENOME_COM_SKILLS)

    fm, _ = frontmatter_e_corpo((tmp_path / record.skill_path).read_text(encoding="utf-8"))
    assert fm["kinds"] == ["infra"]


def test_apply_recusa_fora_da_zona_mutavel(tmp_path):
    """Genoma sem skills/** barra ANTES de escrever qualquer coisa."""
    p = propose_research([], topic="qualquer coisa", kind="code")

    with pytest.raises(GenomeViolation) as exc:
        apply_research(p, backend="mock", root=tmp_path, genome=GENOME_SEM_SKILLS)

    assert any("not_mutable" in v for v in exc.value.violations)
    assert not (tmp_path / "skills").exists()


def test_apply_recusa_zona_imutavel(tmp_path):
    """Proposta apontada para a régua é immutable, não só not_mutable."""
    p = propose_research([], topic="x", kind="code")
    object.__setattr__(p, "target_file", "harness/ruler/wilson.py")

    with pytest.raises(GenomeViolation) as exc:
        apply_research(p, backend="mock", root=tmp_path, genome=GENOME_COM_SKILLS)
    assert any("immutable" in v for v in exc.value.violations)


# --- registro ------------------------------------------------------------------


def test_acao_registrada_para_o_autopilot():
    acts = target_mod.actions()

    assert "research" in acts
    assert acts["research"].propose is propose_research
    assert acts["research"].apply is apply_research
    assert target_mod.get_action("research").name == "research"
    with pytest.raises(KeyError):
        target_mod.get_action("inexistente")


def test_registro_manual_entra_e_sai():
    fake = target_mod.Action(name="fake", propose=None, apply=None)
    target_mod.register_action(fake)
    try:
        assert target_mod.actions()["fake"] is fake
    finally:
        target_mod.unregister_action("fake")
    assert "fake" not in target_mod.actions()
