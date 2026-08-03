from pathlib import Path

import pytest

from harness.skills import Skill, load_skills, render_prompt, select_skills

REPO_SKILLS = Path(__file__).parent.parent / "skills"


def _write(root: Path, filename: str, front: str, body: str = "corpo da skill") -> Path:
    root.mkdir(exist_ok=True)
    path = root / filename
    path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")
    return path


# --- loader ----------------------------------------------------------------


def test_load_happy_path(tmp_path):
    p = _write(
        tmp_path / "skills",
        "alfa.md",
        'name = "alfa"\nkinds = ["code"]\ndescription = "desc alfa"',
        "Faça o mínimo.",
    )
    skills = load_skills(tmp_path / "skills")
    assert skills == [
        Skill(name="alfa", kinds=("code",), description="desc alfa", body="Faça o mínimo.", path=p)
    ]


def test_missing_dir_returns_empty(tmp_path):
    assert load_skills(tmp_path / "nao_existe") == []


def test_malformed_files_are_skipped(tmp_path):
    root = tmp_path / "skills"
    _write(root, "ok.md", 'name = "ok"\nkinds = []\ndescription = "d"')
    (root / "toml_ruim.md").write_text("---\nname === !!!\n---\ncorpo\n", encoding="utf-8")
    (root / "sem_fecho.md").write_text('---\nname = "x"\ncorpo sem segundo ---\n', encoding="utf-8")
    (root / "sem_frontmatter.md").write_text("só markdown\n", encoding="utf-8")
    (root / "sem_name.md").write_text('---\nkinds = ["code"]\n---\ncorpo\n', encoding="utf-8")
    assert [s.name for s in load_skills(root)] == ["ok"]


# --- seleção por kind ------------------------------------------------------


def test_select_by_kind_and_empty_kinds_match_all(tmp_path):
    root = tmp_path / "skills"
    _write(root, "code.md", 'name = "so-code"\nkinds = ["code"]\ndescription = "d"')
    _write(root, "cfg.md", 'name = "so-config"\nkinds = ["config"]\ndescription = "d"')
    _write(root, "geral.md", 'name = "geral"\nkinds = []\ndescription = "d"')

    assert [s.name for s in select_skills("code", root)] == ["so-code", "geral"]
    assert {s.name for s in select_skills("config", root)} == {"so-config", "geral"}
    # kind desconhecido/None: só as skills sem restrição
    assert [s.name for s in select_skills("infra", root)] == ["geral"]
    assert [s.name for s in select_skills(None, root)] == ["geral"]


# --- render ----------------------------------------------------------------


def test_render_prompt_output(tmp_path):
    root = tmp_path / "skills"
    _write(root, "a.md", 'name = "alfa"\nkinds = []\ndescription = "d"', "Regra alfa.")
    _write(root, "b.md", 'name = "beta"\nkinds = []\ndescription = "d"', "Regra beta.")
    out = render_prompt(load_skills(root))
    assert out.startswith("## Skills")
    assert "### alfa\nRegra alfa." in out
    assert "### beta\nRegra beta." in out


def test_render_prompt_empty_for_no_skills():
    assert render_prompt([]) == ""


# --- seeds do repo ---------------------------------------------------------


def test_seed_skills_parse_and_route():
    names = {s.name for s in load_skills(REPO_SKILLS)}
    assert {"python-fixes", "config-calibration"} <= names
    assert "python-fixes" in {s.name for s in select_skills("code", REPO_SKILLS)}
    assert "config-calibration" in {s.name for s in select_skills("config", REPO_SKILLS)}


# --- injeção no backend ----------------------------------------------------


def test_backend_injects_skills_and_mcp_tools(monkeypatch, tmp_path):
    deepagents = pytest.importorskip("deepagents")
    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    captured: dict = {}

    def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return object()

    seen: dict = {}

    def fake_select(kind, root=None):
        seen["kind"] = kind
        return [Skill("python-fixes", ("code",), "d", "Diff mínimo sempre.", Path("x.md"))]

    sentinel_tool = object()
    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create)
    monkeypatch.setattr(da, "select_skills", fake_select)
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [sentinel_tool])

    req = ExecRequest(prompt="x", workspace=tmp_path, max_turns=2)
    da._build_agent(req)

    assert seen["kind"] is None  # ExecRequest sem kind => None, não explode
    prompt = captured["system_prompt"]
    assert "## Skills" in prompt
    assert "### python-fixes\nDiff mínimo sempre." in prompt
    assert "diretório de trabalho" in prompt  # base preservada
    assert captured["tools"] == [sentinel_tool]


def test_backend_without_skills_or_tools_keeps_base_prompt(monkeypatch, tmp_path):
    deepagents = pytest.importorskip("deepagents")
    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    captured: dict = {}
    monkeypatch.setattr(
        deepagents, "create_deep_agent", lambda *a, **kw: captured.update(kw) or object()
    )
    monkeypatch.setattr(da, "select_skills", lambda kind, root=None: [])
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [])

    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, max_turns=2))

    assert "## Skills" not in captured["system_prompt"]
    assert "tools" not in captured  # lista vazia não é passada adiante
