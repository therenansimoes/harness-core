"""Atribuição de skills: record/lift/prune + wiring no backend deepagents."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.ledger import store
from harness.skills import attribution
from harness.types import ExecRequest


def _db(tmp_path: Path) -> Path:
    return tmp_path / "runs.sqlite"


def _insert_run(db: Path, run_id: str, ok: bool) -> None:
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, unit_id, backend, ok, exit_reason, "
            "sec_total, sec_provision, intervention, created_at) "
            "VALUES (?, 'u', 'mock', ?, 'done', 1.0, 0.0, 0, 't')",
            (run_id, int(ok)),
        )


def _seed(db: Path, skill: str, with_oks: list[bool], without_oks: list[bool]) -> None:
    for i, ok in enumerate(with_oks):
        rid = f"{skill}-with-{i}"
        _insert_run(db, rid, ok)
        attribution.record_usage(rid, [skill], db)
    for i, ok in enumerate(without_oks):
        _insert_run(db, f"{skill}-wo-{i}", ok)


# --------------------------------------------------------------------------- record + lift


def test_record_usage_is_idempotent(tmp_path):
    db = _db(tmp_path)
    assert attribution.record_usage("r1", ["a", "b"], db) == 2
    assert attribution.record_usage("r1", ["a", "b"], db) == 0
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
    assert n == 2


def test_lift_counts_and_wilson_match_hand_calc(tmp_path):
    db = _db(tmp_path)
    # com a skill: 3/4; sem: 1/4
    _seed(db, "helper", [True, True, True, False], [True, False, False, False])
    stats = attribution.lift("helper", db)
    assert stats["with"] == (3, 4)
    assert stats["without"] == (1, 4)
    # Wilson-low na mão (z=1.96): p=.75 -> 0.3006; p=.25 -> 0.0456
    assert stats["wilson_low_with"] == pytest.approx(0.3006, abs=1e-3)
    assert stats["wilson_low_without"] == pytest.approx(0.0456, abs=1e-3)
    assert stats["wilson_low_with"] > stats["wilson_low_without"]


def test_lift_empty_db_is_all_zero(tmp_path):
    stats = attribution.lift("ghost", _db(tmp_path))
    assert stats["with"] == (0, 0)
    assert stats["without"] == (0, 0)
    assert stats["wilson_low_with"] == 0.0
    assert stats["wilson_low_without"] == 0.0


# --------------------------------------------------------------------------- prune


def test_prune_candidates_negative_lift_with_sample(tmp_path):
    db = _db(tmp_path)
    # "bad": 1/5 com, 4/5 sem — lift negativo, amostra ok
    _seed(db, "bad", [True] + [False] * 4, [True] * 4 + [False])
    # "good": 4/5 com, 1/5 sem — lift positivo
    _seed(db, "good", [True] * 4 + [False], [True] + [False] * 4)
    assert attribution.prune_candidates(db, min_trials=5) == ["bad"]


def test_prune_candidates_respects_min_trials(tmp_path):
    db = _db(tmp_path)
    _seed(db, "bad", [False, False], [True, True])  # só 2 trials por braço
    assert attribution.prune_candidates(db, min_trials=5) == []
    assert attribution.prune_candidates(db, min_trials=2) == ["bad"]


# --------------------------------------------------------------------------- action


def test_action_shape_and_apply_moves_to_attic(tmp_path):
    act = attribution.action()
    assert act.name == "skill_prune"
    assert callable(act.propose) and callable(act.apply)

    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "bad.md").write_text("corpo", encoding="utf-8")
    moved = act.apply(["bad", "inexistente"], root=tmp_path)
    assert moved == ["skills/attic/bad.md"]
    assert not (skills / "bad.md").exists()
    assert (skills / "attic" / "bad.md").read_text(encoding="utf-8") == "corpo"


def test_propose_reads_ledger(tmp_path):
    db = _db(tmp_path)
    _seed(db, "bad", [False] * 5, [True] * 5)
    assert attribution.action().propose(db_path=db, min_trials=5) == ["bad"]


# --------------------------------------------------------------------------- backend wiring


SKILL_MD = '---\nname = "helper"\nkinds = []\ndescription = "d"\n---\n\nCorpo da skill.\n'


class _FakeAgent:
    def invoke(self, payload, config):
        return {"messages": []}


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    pytest.importorskip("deepagents")
    import deepagents

    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return captured


def _req(tmp_path: Path, session_id: str | None = "sess-1") -> ExecRequest:
    return ExecRequest(
        prompt="oi",
        workspace=tmp_path / "ws",
        session_id=session_id,
        trace_path=tmp_path / "trace.jsonl",
    )


def test_backend_records_usage_and_prepends_executor_md(fake_agent, tmp_path):
    from harness.backends.deepagents_backend import DeepagentsBackend

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "helper.md").write_text(SKILL_MD, encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "executor.md").write_text("BASE DO EXECUTOR\n", encoding="utf-8")

    result = DeepagentsBackend().execute(_req(tmp_path))
    assert result.ok

    prompt = fake_agent["system_prompt"]
    assert prompt.startswith("BASE DO EXECUTOR")
    assert "Seu diretório de trabalho" in prompt
    assert "Corpo da skill." in prompt

    with sqlite3.connect(tmp_path / "data" / "runs.sqlite") as conn:
        rows = conn.execute("SELECT run_id, skill FROM skill_usage").fetchall()
    assert rows == [("sess-1", "helper")]


def test_backend_without_executor_md_keeps_current_prompt(fake_agent, tmp_path):
    from harness.backends.deepagents_backend import DeepagentsBackend

    result = DeepagentsBackend().execute(_req(tmp_path, session_id=None))
    assert result.ok
    assert fake_agent["system_prompt"].startswith("Seu diretório de trabalho")
    # sem skills e sem id => nenhum banco de atribuição criado
    assert not (tmp_path / "data" / "runs.sqlite").exists()
