"""Alvo, mutação e escalação: as três peças do loop, sem grafo no meio."""

import shutil
from pathlib import Path

import pytest

from harness.improve import escalate, mutate
from harness.improve.target import (
    DEFAULTS,
    CatalogError,
    Rule,
    load_catalog,
    pick_target,
    with_ledger_priors,
)
from harness.ledger import store
from harness.types import MutationRow, RunRow

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"

SYNTHETIC_CATALOG = """
[improve]
sec_cost_usd = 0.0001
n_per_arm = 6

[[rule]]
id = "floor_up"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: escala mais cedo"

[[rule]]
id = "toca_a_regua"
target_file = "harness/ruler/wilson.py"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: proibida pelo genoma"
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Árvore isolada com o `config/` real do repo + um catálogo sintético."""
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "catalog.toml").write_text(SYNTHETIC_CATALOG, encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def row(ok: bool, exit_reason: str, sec: float = 10.0, cost: float | None = None) -> RunRow:
    return RunRow(
        run_id="r", unit_id="u", project=None, backend="mock", model=None,
        tier="t0", kind="code", ok=ok, exit_reason=exit_reason, sec_total=sec,
        sec_provision=0.0, cost_usd=cost, intervention=False,
        created_at=store.now_iso(),
    )


def rule(id_: str, fails_on: tuple[str, ...], **kw) -> Rule:
    return Rule(
        id=id_, target_file="config/models.toml", key="router.prior_floor",
        from_value=0.50, to_value=0.65, fails_on=fails_on, **kw
    )


# --- alvo ----------------------------------------------------------------------


def test_pick_target_escolhe_maior_ganho_esperado():
    """Falha cara e frequente ganha da falha barata, mesmo com prior igual."""
    history = [row(False, "timeout", sec=100.0)] * 2 + [row(False, "verify_failed", sec=1.0)] * 4
    catalog = [rule("cura_verify", ("verify_failed",)), rule("cura_timeout", ("timeout",))]

    target = pick_target(history, catalog)

    assert target is not None
    assert target.rule.id == "cura_timeout"
    assert target.pattern == "timeout"
    assert target.freq == pytest.approx(2 / 6)
    assert target.avg_cost == pytest.approx(100.0 * DEFAULTS["sec_cost_usd"])
    assert target.gain == pytest.approx(target.freq * target.avg_cost * 0.5)
    assert target.reasons[0] == "pattern:timeout(2/6)"


def test_pick_target_usa_prior_para_desempatar():
    """Mesma evidência, priors diferentes: quem já foi KEEP vai na frente."""
    history = [row(False, "verify_failed")] * 3
    catalog = [
        rule("nova", ("verify_failed",)),
        rule("provada", ("verify_failed",), prior_succ=3, prior_n=3),
    ]

    assert pick_target(history, catalog).rule.id == "provada"


def test_pick_target_empata_por_id():
    """Determinismo: dois ciclos com a mesma evidência escolhem a mesma regra."""
    history = [row(False, "verify_failed")] * 3
    catalog = [rule("bbb", ("verify_failed",)), rule("aaa", ("verify_failed",))]

    assert pick_target(history, catalog).rule.id == "aaa"
    assert pick_target(history, list(reversed(catalog))).rule.id == "aaa"


@pytest.mark.parametrize(
    "history, catalog, porque",
    [
        ([], [rule("r", ("verify_failed",))], "histórico vazio"),
        ([row(True, "done")] * 10, [rule("r", ("verify_failed",))], "ninguém falhou"),
        ([row(False, "timeout")] * 5, [rule("r", ("verify_failed",))], "regra não ataca"),
        ([row(False, "verify_failed")] * 5, [], "catálogo vazio"),
        ([row(False, "verify_failed")] * 5, [rule("r", ())], "regra sem fails_on"),
    ],
)
def test_pick_target_sem_gradiente_devolve_none(history, catalog, porque):
    """Risco 5 da SPEC: sem gradiente o loop NÃO inventa mutação."""
    assert pick_target(history, catalog) is None, porque


def test_pick_target_respeita_min_gain():
    history = [row(False, "verify_failed", sec=1.0)] * 3
    catalog = [rule("r", ("verify_failed",))]

    assert pick_target(history, catalog) is not None
    assert pick_target(history, catalog, {"min_gain": 1.0}) is None


def test_pick_target_conta_cost_usd_quando_existe():
    """Backend pago: o dólar reportado entra por cima do preço do relógio."""
    history = [row(False, "verify_failed", sec=1.0, cost=0.5)] * 4
    target = pick_target(history, [rule("r", ("verify_failed",))])

    assert target.avg_cost == pytest.approx(0.5 + 1.0 * DEFAULTS["sec_cost_usd"])


def test_with_ledger_priors_tira_regra_ja_reprovada():
    catalog = [rule("boa", ("verify_failed",)), rule("ruim", ("verify_failed",))]
    mutations = [
        MutationRow("m1", "ruim", "DISCARD", "6/6", "0/6", "t", True),
        MutationRow("m2", "boa", "KEEP", "0/6", "6/6", "t", False),
        MutationRow("m3", "boa", "INCONCLUSIVE", "3/6", "3/6", "t", True),
    ]

    out = with_ledger_priors(catalog, mutations)

    assert [r.id for r in out] == ["boa"]
    # INCONCLUSIVE conta como tentativa (prior decai), só não conta como acerto.
    assert (out[0].prior_succ, out[0].prior_n) == (1, 2)


# --- catálogo -------------------------------------------------------------------


def test_load_catalog_do_repo_e_valido():
    rules, cfg = load_catalog(root=REPO_CONFIG.parent)

    assert [r.id for r in rules]
    assert all(r.fails_on for r in rules), "regra sem fails_on nunca seria escolhida"
    assert cfg["n_per_arm"] >= 1


@pytest.mark.parametrize(
    "body, erro",
    [
        ("[[rule]]\nid='x'\n", "campos faltando"),
        ("[improve]\nnao_existe = 1\n", "não é um knob"),
        ("[[rule]]\nid='x'\ntarget_file='config/a.toml'\nkey='k'\nfrom=1\nto=1\n", "iguais"),
        (("[[rule]]\nid='x'\ntarget_file='c'\nkey='k'\nfrom=1\nto=2\n"
          "[[rule]]\nid='x'\ntarget_file='c'\nkey='k'\nfrom=1\nto=3\n"), "duplicado"),
    ],
)
def test_load_catalog_falha_fechado(tmp_path, body, erro):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "catalog.toml").write_text(body, encoding="utf-8")

    with pytest.raises(CatalogError, match=erro):
        load_catalog(root=tmp_path)


def test_load_catalog_sem_arquivo_nao_vira_catalogo_vazio(tmp_path):
    with pytest.raises(CatalogError, match="ilegível"):
        load_catalog(root=tmp_path)


# --- mutação --------------------------------------------------------------------


def test_mutate_respeita_genoma(sandbox):
    """Regra apontando pra régua é rejeitada ANTES de qualquer escrita."""
    rules, _ = load_catalog(root=sandbox)
    proibida = next(r for r in rules if r.id == "toca_a_regua")

    violations = mutate.check(proibida, root=sandbox)
    assert violations == ["genome:immutable:harness/ruler/wilson.py"]

    with pytest.raises(mutate.GenomeViolation) as exc:
        mutate.apply(proibida, "2026-08-02T00:00:00+00:00", root=sandbox)
    assert exc.value.violations == violations


@pytest.mark.parametrize(
    "target_file, esperado",
    [
        ("harness/graph/run_graph.py", "genome:immutable"),
        ("README.md", mutate.NOT_MUTABLE),
        ("config/catalog.toml", mutate.SELF_EDIT),
        ("../fora.toml", "genome:escape"),
    ],
)
def test_mutate_rejeita_alvo_fora_do_mutavel(sandbox, target_file, esperado):
    r = Rule(id="x", target_file=target_file, key="router.prior_floor",
             from_value=0.50, to_value=0.65)

    violations = mutate.check(r, root=sandbox)

    assert len(violations) == 1
    assert violations[0].startswith(esperado)


def test_mutate_roundtrip_byte_identico(sandbox):
    alvo = sandbox / "config" / "models.toml"
    antes = alvo.read_bytes()
    rules, _ = load_catalog(root=sandbox)
    r = next(x for x in rules if x.id == "floor_up")

    m = mutate.apply(r, "2026-08-02T00:00:00+00:00", root=sandbox)

    assert mutate.read_value(alvo, r.key) == 0.65
    assert (m.before_raw, m.after_raw) == ("0.50", "0.65")
    assert alvo.read_bytes() != antes
    # só a linha do knob mudou: comentário nenhum foi perdido no caminho
    assert len(alvo.read_text().splitlines()) == len(antes.decode().splitlines())

    mutate.revert(m, root=sandbox)

    assert alvo.read_bytes() == antes


def test_mutate_toggle_e_idempotente(sandbox):
    rules, _ = load_catalog(root=sandbox)
    r = next(x for x in rules if x.id == "floor_up")
    m = mutate.apply(r, "t", root=sandbox)

    assert mutate.toggle(m, root=sandbox, applied=True) is False   # já está ligada
    assert mutate.toggle(m, root=sandbox, applied=False) is True
    assert mutate.revert(m, root=sandbox) is False                 # já reverteu
    assert mutate.read_value(sandbox / "config/models.toml", r.key) == 0.50


def test_mutate_recusa_from_desatualizado(sandbox):
    r = Rule(id="x", target_file="config/models.toml", key="router.prior_floor",
             from_value=0.99, to_value=0.65)

    with pytest.raises(mutate.MutationError, match="catálogo desatualizado"):
        mutate.apply(r, "t", root=sandbox)


def test_mutate_recusa_chave_inexistente(sandbox):
    r = Rule(id="x", target_file="config/models.toml", key="router.nao_existe",
             from_value=1, to_value=2)

    with pytest.raises(mutate.MutationError, match="chave inexistente"):
        mutate.apply(r, "t", root=sandbox)


def test_mutate_edita_array_de_tabelas(sandbox):
    """`tier[0].max_turns` acerta o PRIMEIRO [[tier]], não o último."""
    alvo = sandbox / "config" / "models.toml"
    antes = alvo.read_bytes()
    r = Rule(id="turns", target_file="config/models.toml", key="tier[0].max_turns",
             from_value=12, to_value=8)

    m = mutate.apply(r, "t", root=sandbox)

    assert mutate.read_value(alvo, "tier[0].max_turns") == 8
    assert mutate.read_value(alvo, "tier[1].max_turns") == 24
    mutate.revert(m, root=sandbox)
    assert alvo.read_bytes() == antes


def test_mutate_preserva_comentario_da_linha(sandbox):
    alvo = sandbox / "config" / "models.toml"
    r = Rule(id="x", target_file="config/models.toml", key="router.prior_floor",
             from_value=0.50, to_value=0.65)

    mutate.apply(r, "t", root=sandbox)

    linha = next(ln for ln in alvo.read_text().splitlines() if ln.startswith("prior_floor"))
    assert linha.startswith("prior_floor = 0.65")
    assert "sobe um tier" in linha


def test_mutate_recusa_mudanca_de_terceiro(sandbox):
    """Revert não sobrescreve o que outro processo escreveu no meio."""
    alvo = sandbox / "config" / "models.toml"
    rules, _ = load_catalog(root=sandbox)
    m = mutate.apply(next(x for x in rules if x.id == "floor_up"), "t", root=sandbox)
    alvo.write_text(alvo.read_text().replace("prior_floor = 0.65", "prior_floor = 0.71"))

    with pytest.raises(mutate.MutationError, match="está '0.71'"):
        mutate.revert(m, root=sandbox)


def test_mutation_id_e_deterministico():
    assert mutate.mutation_id("r", "t") == mutate.mutation_id("r", "t")
    assert mutate.mutation_id("r", "t") != mutate.mutation_id("r", "t2")
    assert len(mutate.mutation_id("r", "t")) == 12


# --- escalação e ledger ----------------------------------------------------------


def test_intervention_rate():
    history = [row(True, "done") for _ in range(8)]
    assert escalate.intervention_rate(history) == 0.0

    from dataclasses import replace
    history[0] = replace(history[0], intervention=True)
    history[1] = replace(history[1], intervention=True)
    assert escalate.intervention_rate(history) == pytest.approx(0.25)
    # janela corta o histórico: 2 de 4 na janela, não 2 de 8
    assert escalate.intervention_rate(history, window=4) == pytest.approx(0.5)
    assert escalate.intervention_rate([]) == 0.0


def test_escalate_payload():
    p = escalate.payload(
        escalate.NO_GRADIENT, unit="tests/fixtures/echo", evidence={"history": 0}
    )

    assert p == {
        "reason": "no_gradient",
        "unit": ["tests/fixtures/echo"],
        "mutation": None,
        "evidence": {"history": 0},
    }
    with pytest.raises(ValueError, match="motivo desconhecido"):
        escalate.payload("porque_sim")


def test_mutations_ledger(sandbox):
    db = sandbox / "data" / store.DB_NAME
    linha = MutationRow(
        mutation_id="abc123", rule_id="floor_up", verdict="KEEP",
        arm_a="2/6", arm_b="6/6", applied_at=store.now_iso(), reverted=False,
        note=None,
    )

    assert store.record_mutation(linha, path=db) is True
    assert store.record_mutation(linha, path=db) is False   # veredito não se reescreve

    store.record_mutation(
        MutationRow("def456", "outra", "REJECTED", "0/0", "0/0", store.now_iso(),
                    False, "genome:immutable:harness/ruler/wilson.py"),
        path=db,
    )

    todas = store.mutations(path=db)
    assert [m.mutation_id for m in todas] == ["def456", "abc123"]
    assert todas[0].note.startswith("genome:immutable")
    assert todas[1].verdict == "KEEP" and todas[1].reverted is False
    assert [m.rule_id for m in store.mutations(rule_id="floor_up", path=db)] == ["floor_up"]
