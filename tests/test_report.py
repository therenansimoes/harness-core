"""`harness report`: o loop presta contas, e presta mesmo sem dado nenhum.

Dois casos importam. Com ledger populado, cada seção tem de aparecer com o que
está na janela (e só o que está: a run velha fica fora). Com banco vazio, o
report ainda sai, todas as seções em "(sem dados)" e exit 0 — relatório que
quebra por falta de arquivo não é lido nunca.
"""

import pytest

from harness import cli, report
from harness.ledger import store
from harness.skills.attribution import record_usage
from harness.types import MutationRow, RunRow

NOW = "2026-08-03T12:00:00+00:00"
RECENT = "2026-08-03T09:00:00+00:00"  # dentro de 24h
OLD = "2026-07-01T09:00:00+00:00"  # fora de qualquer janela curta

SECTIONS = ("Runs", "Mutações por ação", "Skills por lift", "Linhagem (últimas)", "Escalações")


def _run(run_id: str, ok: bool, created_at: str, **over) -> RunRow:
    base = dict(
        run_id=run_id,
        unit_id="u1",
        project="p",
        backend="mock",
        model=None,
        tier="local",
        kind="codegen",
        ok=ok,
        exit_reason="done",
        sec_total=1.0,
        sec_provision=0.1,
        cost_usd=0.01,
        intervention=False,
        created_at=created_at,
    )
    return RunRow(**{**base, **over})


def _mut(mid: str, verdict: str, applied_at: str, **over) -> MutationRow:
    base = dict(
        mutation_id=mid,
        rule_id="r1",
        verdict=verdict,
        arm_a="3/6",
        arm_b="5/6",
        applied_at=applied_at,
        reverted=(verdict != "KEEP"),
    )
    return MutationRow(**{**base, **over})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_report_populado(env, capsys):
    db = env / "data" / "runs.sqlite"
    store.record_run(_run("r-ok", True, RECENT), path=db)
    store.record_run(_run("r-bad", False, RECENT), path=db)
    store.record_run(_run("r-velha", True, OLD), path=db)
    store.record_mutation(_mut("m1", "KEEP", RECENT, action="tune_toml"), path=db)
    store.record_mutation(_mut("m2", "DISCARD", RECENT, action="tune_toml"), path=db)
    store.record_mutation(
        _mut("m3", "ABORTED", RECENT, note="deadline", action="tune_toml"), path=db
    )
    record_usage("r-ok", ["skill_a"], db_path=db)
    lineage = env / "lineage.jsonl"
    lineage.write_text(
        '{"id": "abc12345deadbeef", "parent_id": null, '
        f'"target": "harness/x.py", "ts": "{RECENT}", "verdict": "KEEP"}}\n',
        encoding="utf-8",
    )

    text = report.build_report(since_hours=24, db_path=db, lineage_file=lineage, now=NOW)

    for name in SECTIONS:
        assert f"## {name}" in text
    assert report.NO_DATA not in text  # tudo tem dado neste caso
    assert "runs=2 accept=1/2 (50%)" in text  # a run velha ficou fora da janela
    assert "usd=0.0200" in text
    assert "| mock | codegen | 2 |" in text
    assert "| tune_toml | 1 | 1 | 1 |" in text
    assert "| skill_a |" in text
    assert "abc12345 harness/x.py [KEEP]" in text
    assert "motivo=deadline" in text

    # E o mesmo relatório pelo CLI, com --out gravando o arquivo.
    out = env / "sub" / "report.md"
    assert (
        cli.main(
            ["report", "--since", "24", "--db", str(db), "--file", str(lineage), "--out", str(out)]
        )
        == 0
    )
    assert "## Runs" in out.read_text(encoding="utf-8")
    assert str(out) in capsys.readouterr().out


def test_runs_mostra_token_agregado_quando_existe(env):
    """`tok=in/out` soma a janela e IGNORA quem não reportou usage. Sem nenhum
    token na janela a métrica não aparece: "tok=0/0" leria como "não gastou"."""
    db = env / "data" / "runs.sqlite"
    store.record_run(_run("r1", True, RECENT, tokens_in=1000, tokens_out=250), path=db)
    store.record_run(_run("r2", True, RECENT, tokens_in=500, tokens_out=100), path=db)
    store.record_run(_run("r3", False, RECENT), path=db)  # backend sem usage
    store.record_run(_run("r-velha", True, OLD, tokens_in=999_999), path=db)

    text = report.build_report(since_hours=24, db_path=db, now=NOW)
    assert "tok=1500/350" in text

    sem_db = env / "data" / "sem-token.sqlite"
    store.record_run(_run("r1", True, RECENT), path=sem_db)
    assert "tok=" not in report.build_report(since_hours=24, db_path=sem_db, now=NOW)


def test_report_vazio_nunca_quebra(env, capsys):
    """Sem banco, sem jsonl: todas as seções em "(sem dados)" e exit 0.

    `--file` explícito porque o default de `lineage.LINEAGE_FILE` é relativo ao
    cwd (`data/lineage.jsonl`), e não ao HARNESS_DATA_DIR: sem isso o teste
    passaria a ler a linhagem REAL do repo quando ela existir.
    """
    assert cli.main(["report", "--file", str(env / "sem-linhagem.jsonl")]) == 0
    text = capsys.readouterr().out
    for name in SECTIONS:
        assert f"## {name}\n\n{report.NO_DATA}" in text
