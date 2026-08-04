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


# --- ranking por relevância ------------------------------------------------


def _four_skills(root: Path) -> None:
    _write(root, "a_html.md", 'name = "html-edit"\nkinds = ["code"]\ndescription = "d"', "Editar template HTML: mexa no markup do arquivo index.")
    _write(root, "b_ledger.md", 'name = "ledger-sqlite"\nkinds = ["code"]\ndescription = "d"', "Migração de schema sqlite, transação e índice no banco.")
    _write(root, "c_markup.md", 'name = "markup-css"\nkinds = []\ndescription = "markup e css do template"', "Classe utilitária no css.")
    _write(root, "d_graph.md", 'name = "langgraph-idioms"\nkinds = ["code"]\ndescription = "d"', "Nó do grafo, checkpoint e reducer de estado.")


def test_select_ranks_by_query_overlap_and_caps(tmp_path):
    """Só as relevantes ao prompt entram, mais pontuada primeiro. Global sem
    overlap (nenhuma aqui) não passaria de graça — a que entra pontuou."""
    root = tmp_path / "skills"
    _four_skills(root)

    got = select_skills("code", root, query="editar o markup HTML do template index")
    assert [s.name for s in got] == ["html-edit", "markup-css"]


def test_select_without_query_keeps_kind_order_under_limit(tmp_path):
    root = tmp_path / "skills"
    _four_skills(root)

    assert [s.name for s in select_skills("code", root)] == ["html-edit", "ledger-sqlite"]
    # query sem token utilizável (< 3 chars) = sem query, não zera a seleção
    assert len(select_skills("code", root, query="x")) == 2
    assert [s.name for s in select_skills("code", root, query="css", limit=1)] == ["markup-css"]


def test_select_kind_without_skills_is_empty(tmp_path):
    root = tmp_path / "skills"
    _write(root, "code.md", 'name = "so-code"\nkinds = ["code"]\ndescription = "d"', "corpo")
    assert select_skills("infra", root, query="qualquer coisa de infra") == []


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
    # com o teto de 2, "roteia" agora quer dizer "ganha o ranking da tarefa"
    bug = "Corrigir bug em Python com diff mínimo e rodar o verify"
    assert "python-fixes" in {s.name for s in select_skills("code", REPO_SKILLS, query=bug)}
    assert "config-calibration" in {s.name for s in select_skills("config", REPO_SKILLS)}


# --- root default: cwd não pode decidir se a skill existe ------------------


def test_default_root_finds_repo_skills_from_foreign_cwd(monkeypatch, tmp_path):
    """Regressão: cwd no workspace (sem skills/) carregava zero skill."""
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "skills").exists()
    names = {s.name for s in load_skills()}
    assert {"python-fixes", "config-calibration"} <= names
    query = "Corrigir bug em Python com diff mínimo e rodar o verify"
    assert "python-fixes" in {s.name for s in select_skills("code", query=query)}


def test_default_root_honours_harness_root(monkeypatch, tmp_path):
    """Env setado manda — inclusive vazio, senão isolamento de teste vazaria."""
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert load_skills() == []
    _write(tmp_path / "skills", "x.md", 'name = "x"\nkinds = ["code"]\ndescription = "d"')
    assert [s.name for s in select_skills("code")] == ["x"]


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

    def fake_select(kind, root=None, **kw):
        seen["kind"] = kind
        seen.update(kw)
        return [Skill("python-fixes", ("code",), "d", "Diff mínimo sempre.", Path("x.md"))]

    sentinel_tool = object()
    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create)
    monkeypatch.setattr(da, "select_skills", fake_select)
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [sentinel_tool])

    req = ExecRequest(prompt="x", workspace=tmp_path, max_turns=2)
    da._build_agent(req)

    assert seen["kind"] is None  # ExecRequest sem kind => None, não explode
    assert seen["query"] == "x"  # prompt da unidade vai pro ranking
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
    monkeypatch.setattr(da, "select_skills", lambda kind, root=None, **kw: [])
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [])

    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, max_turns=2))

    assert "## Skills" not in captured["system_prompt"]
    assert "tools" not in captured  # lista vazia não é passada adiante


def test_backend_injects_real_skills_from_foreign_cwd_and_records_usage(monkeypatch, tmp_path):
    """O caminho real: nada de fake_select. cwd fora do repo, kind vindo do
    request, skills do repo no prompt e a atribuição gravada no ledger."""
    deepagents = pytest.importorskip("deepagents")
    from harness.backends import deepagents_backend as da
    from harness.skills import attribution
    from harness.types import ExecRequest

    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)

    captured: dict = {}
    monkeypatch.setattr(
        deepagents, "create_deep_agent", lambda *a, **kw: captured.update(kw) or object()
    )
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [])

    req = ExecRequest(
        # o prompt é a query do ranking: sem ele, quem entra é ordem de arquivo
        prompt="Corrigir bug em Python com diff mínimo e rodar o verify",
        workspace=tmp_path / "ws",
        max_turns=2,
        run_id="run-skill-1",
        kind="code",
    )
    da._build_agent(req)

    assert "### python-fixes" in captured["system_prompt"]
    assert "### config-calibration" not in captured["system_prompt"]  # kind errado
    assert captured["system_prompt"].count("### ") <= 2  # teto: 9B não lê 4 skills
    rows = attribution.lift("python-fixes")
    assert rows["with"] == (0, 0)  # sem linha em `runs`: só o usage foi gravado
    with attribution._connect() as conn:
        used = [
            r["skill"]
            for r in conn.execute(
                "SELECT skill FROM skill_usage WHERE run_id = ?", ("run-skill-1",)
            )
        ]
    assert "python-fixes" in used
