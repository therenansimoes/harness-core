"""`harness skills` e `harness actions`: observabilidade read-only da evolução.

Tudo em tmp_path via env vars (mesma convenção do doctor): skills sintéticas
no HARNESS_ROOT, ledger sintético no HARNESS_DATA_DIR. Nenhum backend roda.
"""

import re

import pytest

from harness import cli
from harness.ledger import store
from harness.skills.attribution import record_usage
from harness.types import MutationRow, RunRow


def _skill_md(name: str, kinds: str = '["code"]', description: str = "ajuda") -> str:
    return f'---\nname = "{name}"\nkinds = {kinds}\ndescription = "{description}"\n---\ncorpo\n'


def _run_row(run_id: str, ok: bool) -> RunRow:
    return RunRow(
        run_id=run_id,
        unit_id="u1",
        project=None,
        backend="mock",
        model=None,
        tier=None,
        kind="code",
        ok=ok,
        exit_reason="ok" if ok else "verify_failed",
        sec_total=1.0,
        sec_provision=0.1,
        cost_usd=None,
        intervention=False,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _mutation(mid: str, verdict: str) -> MutationRow:
    return MutationRow(
        mutation_id=mid,
        rule_id="r1",
        verdict=verdict,
        arm_a="3/6",
        arm_b="5/6",
        applied_at="2026-01-01T00:00:00+00:00",
        reverted=(verdict == "DISCARD"),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


# --- harness skills ----------------------------------------------------------------


def test_skills_lista_nome_kinds_descricao(env, capsys):
    (env / "skills").mkdir()
    (env / "skills" / "a.md").write_text(
        _skill_md("corrige-python", description="fixes de python"), encoding="utf-8"
    )
    (env / "skills" / "b.md").write_text(
        _skill_md("geral", kinds="[]", description="vale pra tudo"), encoding="utf-8"
    )

    rc = cli.main(["skills"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "corrige-python" in out and "code" in out and "fixes de python" in out
    # kinds vazio = vale para todo kind, e a listagem diz isso
    assert "geral" in out and "*" in out and "vale pra tudo" in out
    assert "skills=2" in out


def test_skills_sem_dir_e_lista_vazia_rc0(env, capsys):
    rc = cli.main(["skills"])

    assert rc == 0
    assert "skills=0" in capsys.readouterr().out


def test_skills_lift_sem_dados_mostra_traco(env, capsys):
    (env / "skills").mkdir()
    (env / "skills" / "a.md").write_text(_skill_md("s1"), encoding="utf-8")

    rc = cli.main(["skills", "--lift"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "lift=-" in out
    assert "com=0/0" in out and "sem=0/0" in out


def test_skills_lift_com_dados_sinteticos(env, capsys):
    (env / "skills").mkdir()
    (env / "skills" / "a.md").write_text(_skill_md("s1"), encoding="utf-8")
    # 2 runs COM a skill (1 ok), 1 run SEM (ok): braços com amostra dos dois lados
    store.record_run(_run_row("r1", ok=True))
    store.record_run(_run_row("r2", ok=False))
    store.record_run(_run_row("r3", ok=True))
    record_usage("r1", ["s1"])
    record_usage("r2", ["s1"])

    rc = cli.main(["skills", "--lift"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "com=1/2" in out and "sem=1/1" in out
    # com amostra nos dois braços sai número, não traço
    assert re.search(r"lift=[+-]\d\.\d\d", out)


# --- harness actions ---------------------------------------------------------------


def test_actions_lista_registry_sem_mutacoes(env, capsys):
    rc = cli.main(["actions"])

    out = capsys.readouterr().out
    assert rc == 0
    for name in ("codegen", "research"):
        assert name in out
    assert "sem mutações no ledger" in out


def test_actions_conta_keep_discard_do_ledger(env, capsys):
    store.record_mutation(_mutation("m1", "KEEP"))
    store.record_mutation(_mutation("m2", "DISCARD"))
    store.record_mutation(_mutation("m3", "INCONCLUSIVE"))

    rc = cli.main(["actions"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "mutações=3" in out
    assert "KEEP=1" in out and "DISCARD=1" in out
