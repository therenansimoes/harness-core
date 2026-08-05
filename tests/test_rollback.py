"""Rollback e audit: o desfazer auditável de uma promoção de tune.

Mesma árvore isolada do `test_tune` (env vars para tmp_path + seams de LLM
trocados por stub): o rollback restaura bytes e grava ledger, então rodar
contra o checkout real reescreveria artefato e banco de verdade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import cli, paths
from harness.evals import freeze
from harness.evals.bundle import bundle_dir
from harness.genome.genome import load as load_genome
from harness.improve import ROOT_ENV, tune
from harness.improve import rollback as rollback_mod
from harness.improve.rollback import RollbackError
from harness.ledger import store
from harness.types import MutationRow

REPO = Path(__file__).resolve().parents[1]
REAL_GENOME = load_genome(REPO / "config" / "genome.toml")

SKILL = "skills/x.md"

SKILL_CASES = (
    '{"id":"s-1","kind":"code_fix","prompt":"como corrigir o import quebrado",'
    '"expect":{"must_mention":["alfa","beta"]},"axes":["grounding","coverage"],'
    '"weight":1.0,"trials":1}\n'
)


def _skill(*termos: str) -> str:
    corpo = "\n".join(f"- passo {t}" for t in termos) or "- passo nenhum"
    return (
        '---\nname = "x"\nkinds = ["code"]\n'
        'description = "orientação destilada: x"\n---\n\n'
        f"# guia\n{corpo}\n"
    )


V1, V2, V3 = _skill("alfa"), _skill("alfa", "beta"), _skill("gama")


def _row(
    mid: str,
    rule_id: str,
    verdict: str = "promoted",
    arms: tuple[str, str] = ("v1", "v2"),
    action: str = "tune",
    ts: str = "2026-08-05T00:00:00+00:00",
) -> MutationRow:
    return MutationRow(
        mutation_id=mid,
        rule_id=rule_id,
        verdict=verdict,
        arm_a=arms[0],
        arm_b=arms[1],
        applied_at=ts,
        reverted=False,
        note="",
        action=action,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Árvore isolada + os dois seams de LLM trocados por stub (vide test_tune)."""
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))

    def runner(prompt, *, model=None, max_usd=0.0):
        runner.prompts.append(prompt)
        return prompt

    def rewriter(prompt, *, model=None, max_usd=0.0):
        rewriter.prompts.append(prompt)
        return rewriter.payload.pop(0)

    runner.prompts, rewriter.prompts, rewriter.payload = [], [], []
    monkeypatch.setattr(tune, "_call_runner", runner)
    monkeypatch.setattr(tune, "_call_rewriter", rewriter)

    class Env:
        pass

    e = Env()
    e.root, e.runner, e.rewriter = tmp_path, runner, rewriter
    return e


def _skill_tree(env, texto: str = V1) -> Path:
    art = env.root / SKILL
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(texto, encoding="utf-8")
    d = bundle_dir(SKILL)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(SKILL_CASES, encoding="utf-8")
    freeze(SKILL)
    return d


def _promote(env) -> str:
    """Promove v2 pelo caminho real (propose + apply) e devolve o mutation_id."""
    _skill_tree(env)
    env.rewriter.payload = [V2, V3]
    proposal = tune.propose_tune(SKILL, trials=1)
    tune.apply_tune(proposal, root=env.root, genome=REAL_GENOME)
    return store.mutations(rule_id=f"tune:{SKILL}")[0].mutation_id


def test_rollback_restaura_versao_anterior(env):
    mid = _promote(env)

    rec = rollback_mod.rollback(mid, why="teste", root=env.root)

    d = tune.chain_dir(SKILL)
    assert (env.root / SKILL).read_text(encoding="utf-8") == V1
    assert (env.root / SKILL).read_text(encoding="utf-8") == (d / "v1.txt").read_text(
        encoding="utf-8"
    )
    assert rec.from_version == 2 and rec.to_version == 1
    winner = json.loads((d / rollback_mod.WINNER_FILE).read_text(encoding="utf-8"))
    assert winner["winner"] == 1 and winner["undoes"] == mid
    assert store.get_mutation(mid).reverted is True
    ev = store.get_mutation(rec.event_id)
    assert ev.verdict == "rolled_back" and ev.action == "rollback"
    assert (ev.arm_a, ev.arm_b) == ("v2", "v1")
    assert "why=teste" in ev.note and f"undoes={mid}" in ev.note
    # Histórico append-only intacto: cadeia e versões não são tocadas.
    assert len(json.loads((d / "chain.json").read_text(encoding="utf-8"))) == 3
    assert (d / "v2.txt").read_text(encoding="utf-8") == V2


def test_rollback_id_desconhecido(env):
    with pytest.raises(RollbackError, match="desconhecido"):
        rollback_mod.rollback("tune:x@nada")


def test_rollback_recusa_held_e_nao_tune(env):
    store.record_mutation(_row("tune:skills/x.md@t1", "tune:skills/x.md", verdict="held"))
    store.record_mutation(
        _row("imp:r@t2", "regra-x", action="improve", ts="2026-08-05T00:00:01+00:00")
    )

    with pytest.raises(RollbackError, match="nada a desfazer"):
        rollback_mod.rollback("tune:skills/x.md@t1")
    with pytest.raises(RollbackError, match="só desfaz"):
        rollback_mod.rollback("imp:r@t2")


def test_rollback_recusa_duas_vezes(env):
    mid = _promote(env)
    rollback_mod.rollback(mid, root=env.root)

    with pytest.raises(RollbackError, match="já revertida"):
        rollback_mod.rollback(mid, root=env.root)


def test_rollback_lifo(env):
    rule = f"tune:{SKILL}"
    store.record_mutation(_row(f"{rule}@t1", rule, ts="2026-08-05T00:00:00+00:00"))
    store.record_mutation(
        _row(f"{rule}@t2", rule, arms=("v2", "v3"), ts="2026-08-05T00:00:01+00:00")
    )

    with pytest.raises(RollbackError, match="LIFO"):
        rollback_mod.rollback(f"{rule}@t1")

    # A mais nova pode: cadeia fabricada com v2 válida para restaurar.
    d = tune.chain_dir(SKILL)
    d.mkdir(parents=True, exist_ok=True)
    (d / "v1.txt").write_text(V1, encoding="utf-8")
    (d / "v2.txt").write_text(V2, encoding="utf-8")
    (d / "chain.json").write_text(
        json.dumps(
            [
                {"version": 1, "overall": 0.1, "reason": "v1", "valid": True},
                {"version": 2, "overall": 0.2, "reason": "v2", "valid": True},
            ]
        ),
        encoding="utf-8",
    )
    (env.root / SKILL).parent.mkdir(parents=True, exist_ok=True)
    (env.root / SKILL).write_text(V3, encoding="utf-8")

    rec = rollback_mod.rollback(f"{rule}@t2", root=env.root)

    assert rec.to_version == 2
    assert (env.root / SKILL).read_text(encoding="utf-8") == V2


def test_rollback_cadeia_sem_arquivo(env):
    rule = f"tune:{SKILL}"
    store.record_mutation(_row(f"{rule}@t1", rule))

    with pytest.raises(RollbackError, match="cadeia sem"):
        rollback_mod.rollback(f"{rule}@t1")


def test_audit_lista_cadeia_e_eventos(env, monkeypatch):
    mid = _promote(env)

    # Antes do rollback: vencedor implícito, derivado da cadeia.
    rep = rollback_mod.audit(SKILL)
    assert [e.version for e in rep.chain] == [1, 2, 3]
    assert rep.winner == 2 and rep.winner_source == "chain.json"
    assert rep.chain_dir == str(tune.chain_dir(SKILL))

    # Relógio adiantado: promoção e rollback no mesmo segundo empatariam no
    # sort por applied_at e a ordem viraria loteria de inserção.
    monkeypatch.setattr(store, "now_iso", lambda: "2099-01-01T00:00:00+00:00")
    rec = rollback_mod.rollback(mid, root=env.root)

    rep = rollback_mod.audit(SKILL)
    assert [e.version for e in rep.chain] == [1, 2, 3]
    assert rep.winner == 1 and rep.winner_source == "winner.json"
    assert rep.events[0].mutation_id == rec.event_id
    tune_rows = [m for m in rep.events if m.mutation_id == mid]
    assert tune_rows and tune_rows[0].reverted is True


def test_audit_sem_historico(env):
    rep = rollback_mod.audit("skills/nunca.md")

    assert rep.chain == [] and rep.events == []
    assert rep.winner is None and rep.winner_source == "" and rep.chain_dir == ""


def test_cli_rollback_e_audit(env, capsys):
    mid = _promote(env)

    assert cli.main(["audit", SKILL]) == 0
    assert mid in capsys.readouterr().out

    assert cli.main(["rollback", mid, "--why", "cli", "--root", str(env.root)]) == 0
    out = capsys.readouterr().out
    assert "revertido: skills/x.md v2 -> v1" in out
    assert (env.root / SKILL).read_text(encoding="utf-8") == V1

    assert cli.main(["rollback", "tune:x@nada"]) == 2
    assert "rollback recusado" in capsys.readouterr().err

    assert cli.main(["audit", "skills/nunca.md"]) == 0
    assert "sem histórico" in capsys.readouterr().out
