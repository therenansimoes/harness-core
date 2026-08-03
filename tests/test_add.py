"""`harness add`: autoria de unit com o backend claude_code MOCKADO.

Nenhum teste aqui gasta chamada paga — `_call_author` (a única fronteira com o
backend) é trocado por monkeypatch retornando JSON fixo.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from harness import add as add_mod
from harness.add import AddError, add
from harness.cli import load_unit, main

FIXED = {
    "id_slug": "ajusta-titulo",
    "prompt_md": "# Tarefa\n\nEdite `index.html`: troque o título por \"Fazenda\".",
    "verify_cmd": "grep -q 'Fazenda' index.html",
    "kind": "content",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Site\nSite da fazenda do Rogers.\n")
    (repo / "index.html").write_text("<h1>oi</h1>\n")
    projects_file = tmp_path / "projects.toml"
    projects_file.write_text(f'[projects.faz]\nrepo = "{repo}"\n')
    out_dir = tmp_path / "quarantine"

    calls: list[tuple[str, str, float]] = []

    def fake_call(prompt: str, model: str, max_usd: float) -> str:
        calls.append((prompt, model, max_usd))
        return json.dumps(fake_call.payload)

    fake_call.payload = dict(FIXED)
    monkeypatch.setattr(add_mod, "_call_author", fake_call)

    class Env:
        pass

    e = Env()
    e.repo, e.projects_file, e.out_dir, e.calls, e.fake = (
        repo, projects_file, out_dir, calls, fake_call
    )
    return e


def _add(env, task="tarefa", project="faz", **kw):
    return add(task, project, projects_file=env.projects_file,
               out_dir=env.out_dir, **kw)


def test_add_writes_loadable_unit(env):
    unit_dir = _add(env, "põe o nome da fazenda no título")

    assert unit_dir == env.out_dir / "ajusta-titulo"
    data = tomllib.loads((unit_dir / "unit.toml").read_text())
    assert data["kind"] == "content"
    assert data["origin"]["project"] == "faz"
    assert data["origin"]["task"] == "põe o nome da fazenda no título"
    assert (unit_dir / "prompt.md").read_text().strip() == FIXED["prompt_md"]

    unit = load_unit(unit_dir)
    assert unit.id == "ajusta-titulo"
    assert unit.prompt == FIXED["prompt_md"]
    assert unit.verify_cmd == FIXED["verify_cmd"]

    # a chamada de autoria recebeu a tarefa e o contexto REAL do repo
    prompt, model, max_usd = env.calls[0]
    assert "nome da fazenda" in prompt
    assert "Site da fazenda do Rogers." in prompt
    assert "index.html" in prompt
    assert model == "haiku" and max_usd == 0.25


def test_default_destination_is_quarantine(env, monkeypatch, tmp_path):
    """Unit autorada por LLM nasce em quarentena, nunca em sealed."""
    monkeypatch.chdir(tmp_path)
    unit_dir = add("tarefa", "faz", projects_file=env.projects_file)
    assert unit_dir == Path("benchmarks/quarantine/ajusta-titulo")
    assert (tmp_path / unit_dir / "unit.toml").is_file()
    assert not (tmp_path / "benchmarks" / "sealed").exists()


def test_dry_shows_without_writing(env, capsys):
    result = _add(env, dry=True)
    assert result is None
    assert not env.out_dir.exists()
    shown = capsys.readouterr().out
    assert 'id = "ajusta-titulo"' in shown
    assert FIXED["verify_cmd"] in shown


@pytest.mark.parametrize("bad", ["", "   ", "true", "review manually the page",
                                 "npm build && confira se ficou bom"])
def test_bad_verify_cmd_rejected_nothing_written(env, bad):
    env.fake.payload = {**FIXED, "verify_cmd": bad}
    with pytest.raises(AddError):
        _add(env)
    assert not env.out_dir.exists()


def test_parse_failure_is_clear_error(env, monkeypatch):
    monkeypatch.setattr(
        add_mod, "_call_author", lambda prompt, model, max_usd: "desculpa, não consegui"
    )
    with pytest.raises(AddError, match="JSON"):
        _add(env)
    assert not env.out_dir.exists()


def test_invalid_fields_rejected(env):
    for bad in (
        {**FIXED, "id_slug": "../fuga"},
        {**FIXED, "kind": "vibes"},
        {**FIXED, "prompt_md": "  "},
    ):
        env.fake.payload = bad
        with pytest.raises(AddError):
            _add(env)
    assert not env.out_dir.exists()


def test_unknown_project_is_clear_error(env):
    with pytest.raises(AddError, match="registrados"):
        _add(env, project="inexistente")


def test_duplicate_slug_refused(env):
    _add(env)
    with pytest.raises(AddError, match="já existe"):
        _add(env)


def test_cli_add_dry(env, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["add", "põe o título", "--project", "faz", "--dry",
               "--projects", str(env.projects_file)])
    assert rc == 0
    assert 'id = "ajusta-titulo"' in capsys.readouterr().out
    assert not (tmp_path / "benchmarks").exists()


def test_cli_add_error_exit_1(env, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    env.fake.payload = {**FIXED, "verify_cmd": ""}
    rc = main(["add", "tarefa", "--project", "faz",
               "--projects", str(env.projects_file)])
    assert rc == 1
    assert "add falhou" in capsys.readouterr().err
