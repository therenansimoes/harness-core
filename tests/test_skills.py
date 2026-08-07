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
    _write(
        root,
        "a_html.md",
        'name = "html-edit"\nkinds = ["code"]\ndescription = "d"',
        "Editar template HTML: mexa no markup do arquivo index.",
    )
    _write(
        root,
        "b_ledger.md",
        'name = "ledger-sqlite"\nkinds = ["code"]\ndescription = "d"',
        "Migração de schema sqlite, transação e índice no banco.",
    )
    _write(
        root,
        "c_markup.md",
        'name = "markup-css"\nkinds = []\ndescription = "markup e css do template"',
        "Classe utilitária no css.",
    )
    _write(
        root,
        "d_graph.md",
        'name = "langgraph-idioms"\nkinds = ["code"]\ndescription = "d"',
        "Nó do grafo, checkpoint e reducer de estado.",
    )


def test_select_ranks_by_query_overlap_and_caps(tmp_path):
    """Só as relevantes ao prompt entram, mais pontuada primeiro. Global sem
    overlap (nenhuma aqui) não passaria de graça — a que entra pontuou."""
    root = tmp_path / "skills"
    _four_skills(root)

    got = select_skills("code", root, query="editar o markup HTML do template index")
    # description-first: markup-css hits on description; html-edit is body-only
    # and is dropped while any description hit exists.
    assert [s.name for s in got] == ["markup-css"]


def test_select_without_query_keeps_kind_order_under_limit(tmp_path):
    root = tmp_path / "skills"
    _four_skills(root)

    assert [s.name for s in select_skills("code", root)] == ["html-edit", "ledger-sqlite", "markup-css"]
    # query sem token utilizável (< 3 chars) = sem query, não zera a seleção
    assert len(select_skills("code", root, query="x")) == 3
    assert [s.name for s in select_skills("code", root, query="css", limit=1)] == ["markup-css"]


# --- path-trigger ----------------------------------------------------------


def test_path_trigger_fura_o_ranking_fuzzy(tmp_path):
    """Glob casado é eixo determinístico: passa na frente da mais pontuada."""
    root = tmp_path / "skills"
    _write(
        root,
        "a_html.md",
        'name = "html-edit"\nkinds = ["code"]\ndescription = "d"',
        "Editar markup HTML do template.",
    )
    _write(
        root,
        "z_toml.md",
        'name = "toml-safety"\nkinds = ["code"]\ndescription = "d"\npaths = ["*.toml"]',
        "Nada a ver com o prompt.",
    )

    query = "editar o markup HTML do template index"
    # sem `files`: comportamento de antes, só o ranking fuzzy
    assert [s.name for s in select_skills("code", root, query=query)] == ["html-edit"]
    # com um arquivo que casa o glob: a skill do path vem primeiro
    got = select_skills("code", root, query=query, files=["config/agents.toml"])
    assert [s.name for s in got] == ["toml-safety", "html-edit"]
    # arquivo que não casa nenhum glob não dispara nada
    got = select_skills("code", root, query=query, files=["index.html"])
    assert [s.name for s in got] == ["html-edit"]


def test_path_trigger_respeita_o_limite(tmp_path):
    root = tmp_path / "skills"
    for n in ("a", "b", "c"):
        _write(
            root,
            f"{n}.md",
            f'name = "{n}"\nkinds = ["config"]\ndescription = "d"\npaths = ["*.toml"]',
        )
    # PATH_CAP=max(1,limit//3): at limit=2 -> max(1,0)=1, so 1 triggered slot
    # overflow triggered fall into resto and fill remaining slots
    got = select_skills("config", root, files=["genome.toml"], limit=2)
    assert [s.name for s in got] == ["a", "b"]
    # at limit=6 -> PATH_CAP=2, all 3 triggered included
    got6 = select_skills("config", root, files=["genome.toml"], limit=6)
    assert [s.name for s in got6] == ["a", "b", "c"]


def test_path_trigger_casa_basename_e_path_completo(tmp_path):
    """Glob com diretório casa o path; glob de extensão casa também o basename.

    O decoy alfabeticamente antes mostra QUEM disparou: sem gatilho a skill
    ainda entra pelo kind, só não fura a fila."""
    root = tmp_path / "skills"
    _write(root, "a_decoy.md", 'name = "decoy"\nkinds = []\ndescription = "d"')
    _write(
        root,
        "z_trig.md",
        'name = "trig"\nkinds = []\ndescription = "d"\npaths = ["harness/skills/*.py", "*.toml"]',
    )

    def primeiro(files):
        return select_skills(None, root, files=files)[0].name

    assert primeiro(["agents.toml"]) == "trig"  # basename bate `*.toml`
    assert primeiro(["config/agents.toml"]) == "trig"  # path completo também
    assert primeiro(["harness/skills/loader.py"]) == "trig"  # glob com diretório
    assert primeiro(["harness/improve.py"]) == "decoy"  # fora do diretório: sem gatilho
    assert primeiro(None) == "decoy"


def test_seed_toml_skill_dispara_por_glob():
    """A skill versionada declara o gatilho real: qualquer .toml da unidade."""
    got = select_skills("config", REPO_SKILLS, files=["config/genome.toml"])
    assert got and got[0].name == "toml-calibration-safety"
    assert got[0].paths == ("*.toml",)


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


def test_render_prompt_truncates_long_body(tmp_path):
    root = tmp_path / "skills"
    section1 = "word " * 520
    section2 = "## Second section\n" + "extra " * 20
    body = section1.strip() + "\n\n" + section2
    _write(root, "long.md", 'name = "long"\nkinds = []\ndescription = "d"', body)
    out = render_prompt(load_skills(root))
    assert "<!-- skill truncated -->" in out
    assert "Second section" not in out


def test_render_prompt_short_body_not_truncated(tmp_path):
    root = tmp_path / "skills"
    body = "short body with fewer than five hundred words word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word word "
    _write(root, "short.md", 'name = "short"\nkinds = []\ndescription = "d"', body)
    out = render_prompt(load_skills(root))
    assert "<!-- skill truncated -->" not in out
    assert "short body" in out


def test_render_prompt_no_section_boundary_hard_truncates(tmp_path):
    root = tmp_path / "skills"
    body = "word " * 600
    _write(root, "nosec.md", 'name = "nosec"\nkinds = []\ndescription = "d"', body)
    out = render_prompt(load_skills(root))
    assert "<!-- skill truncated -->" in out
    assert out.count("word") <= 501


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
    assert "## Skills disponíveis" in prompt
    assert "- python-fixes — d" in prompt  # índice no system; corpo é dado
    assert "### python-fixes\nDiff mínimo sempre." in (da._untrusted_block(req) or "")
    assert "diretório de trabalho" in prompt  # base preservada
    # MCP entra junto das file/web tools das ondas de tool engineering
    assert sentinel_tool in captured["tools"]


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
    # sem MCP ainda entram as file/web tools; o que este teste garante é que
    # nenhum sentinel de MCP vazou e que o prompt base ficou intacto
    mcp_names = [getattr(t, "name", "") for t in captured.get("tools", [])]
    assert "sentinel" not in mcp_names


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

    assert "- python-fixes —" in captured["system_prompt"]  # índice
    assert "config-calibration" not in captured["system_prompt"]  # kind errado
    # Teto (9B não lê 4 skills): contado onde os corpos moram agora, o bloco.
    assert (da._untrusted_block(req) or "").count("### ") <= 3
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


def test_backend_extrai_files_do_prompt_e_dispara_path_trigger(monkeypatch, tmp_path):
    """O prompt da unidade é a fonte barata de `files=`: citou o .toml, a skill
    de glob fura a fila. Sem isso o path-trigger dormia — o backend nunca
    passava `files`, e o único eixo vivo era o ranking fuzzy.

    A asserção é sobre a CHAMADA (e sobre a seleção com a fixture real do repo),
    não sobre onde o corpo da skill aparece: o lugar do texto no prompt é
    assunto de outro teste."""
    deepagents = pytest.importorskip("deepagents")
    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    seen: dict = {}

    def fake_select(kind, root=None, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(deepagents, "create_deep_agent", lambda *a, **kw: object())
    monkeypatch.setattr(da, "select_skills", fake_select)
    monkeypatch.setattr(da, "load_mcp_tools", lambda *a, **k: [])

    prompt = "Edite config/genome.toml para subir o piso e rode o verify"
    da._build_agent(
        ExecRequest(prompt=prompt, workspace=tmp_path / "ws", max_turns=2, kind="config")
    )
    assert seen["files"] == ["config/genome.toml"]
    assert seen["query"] == prompt  # o ranking continua recebendo o prompt

    # E com esse `files` a skill versionada de .toml ganha a fila de verdade.
    got = select_skills("config", REPO_SKILLS, query=prompt, files=seen["files"])
    assert got and got[0].name == "toml-calibration-safety"


def test_prompt_files_dedup_e_prompt_sem_arquivo():
    """Ordem de aparição, sem repetir, e lista vazia quando ninguém citou path —
    aí o eixo de path-trigger nem roda e o comportamento é o de antes."""
    pytest.importorskip("deepagents")
    from harness.backends import deepagents_backend as da

    prompt = "leia harness/x.py, edite harness/x.py e o config/agents.toml"
    assert da._prompt_files(prompt) == ["harness/x.py", "config/agents.toml"]
    assert da._prompt_files("suba o piso do bandit sem mexer em arquivo nenhum") == []


def test_methodology_skills_select_by_domain_keywords() -> None:
    """New Ralph methodology skills must win SELECT_LIMIT ranking on domain queries."""
    cases = [
        ("content", "marketing landing page conversion CTA offer", None, "marketing-methodology"),
        ("content", "inventory SKU reorder stock BOM audit", None, "inventory-methodology"),
        ("content", "ecommerce checkout cart pricing refund fulfill", None, "ecommerce-sales-methodology"),
        ("code", "Next.js app router server component page.tsx", ["app/page.tsx"], "nextjs-methodology"),
        ("code", "python pytest failure fix types uv run", None, "python-methodology"),
        ("code", "research sources claims counter-evidence before coding", None, "method-research"),
        ("code", "planning decompose implementation steps verify plan", None, "method-planning"),
    ]
    for kind, query, files, expect in cases:
        got = select_skills(kind, REPO_SKILLS, query=query, files=files)
        names = [s.name for s in got]
        assert expect in names, (kind, query, names)


def test_path_trigger_proc_skills() -> None:
    """Proc skills with paths= must be path-triggered for .py units (limit=10 to bypass SELECT_LIMIT cap)."""
    # All *.py proc skills compete; use limit=10 to confirm each is triggered.
    py_files_rename = ["pedido.py", "relatorio.py"]
    got = select_skills("code", REPO_SKILLS, query="rename function across files", files=py_files_rename, limit=10)
    names = [s.name for s in got]
    assert "proc-rename-via-write" in names, names

    got = select_skills("code", REPO_SKILLS, query="edit existing class method", files=["util.py"], limit=10)
    names = [s.name for s in got]
    assert "proc-surgical-edit-check" in names, names

    got = select_skills("code", REPO_SKILLS, query="create module and importer", files=["main.py"], limit=10)
    names = [s.name for s in got]
    assert "proc-two-module-create" in names, names

    got = select_skills("code", REPO_SKILLS, query="write new file from spec", files=["app.py"], limit=10)
    names = [s.name for s in got]
    assert "proc-exact-write-from-spec" in names, names

    got = select_skills("code", REPO_SKILLS, query="fix bug", files=["service.py"], limit=10)
    names = [s.name for s in got]
    assert "python-fixes" in names, names

    # Under SELECT_LIMIT=2, rename prompt wins over unrelated skills when files present
    got2 = select_skills("code", REPO_SKILLS, query="rename symbol across files", files=py_files_rename)
    names2 = [s.name for s in got2]
    assert "proc-rename-via-write" in names2, names2


def test_path_trigger_recovery_cards_no_paths() -> None:
    """Recovery cards have no paths=: fire on description match alone."""
    got = select_skills(None, REPO_SKILLS, query="empty_turn nudge fired do something")
    assert "proc-recover-after-empty-turn" in [s.name for s in got], [s.name for s in got]

    got = select_skills(None, REPO_SKILLS, query="completion_guard nudge fired missing files write")
    assert "proc-recover-missing-files" in [s.name for s in got], [s.name for s in got]


def test_path_cap_prevents_path_flood() -> None:
    """ym0.18 redesign: SELECT_LIMIT=3, PATH_CAP=max(1,limit//3)=1.
    One slot for best desc-ranked path-triggered skill; two free slots for ranking.
    """
    # Single-file: best path-triggered by desc score wins 1 slot;
    # python-methodology fills a ranking slot.
    got = select_skills("code", REPO_SKILLS, query="fix bug python service", files=["service.py"])
    names = [s.name for s in got]
    assert len(names) <= 3, f"exceeded limit: {names}"
    assert not ("proc-exact-write-from-spec" in names and "proc-rename-via-write" in names), (
        f"path flood: both alphabetically-first skills crowded in: {names}"
    )
    # python-methodology should surface via ranking (free slots)
    assert "python-methodology" in names, f"expected python-methodology in ranking slots, got {names}"

    # Pytest / test file: python-methodology wins desc score -> triggered slot.
    got2 = select_skills("code", REPO_SKILLS, query="python pytest typed uv run tests", files=["tests/test_foo.py"])
    names2 = [s.name for s in got2]
    assert "python-methodology" in names2, f"expected python-methodology, got {names2}"

    # Exact-write query: proc-exact-write-from-spec wins triggered slot.
    got3 = select_skills("code", REPO_SKILLS, query="write new file from spec exact content", files=["new_mod.py"])
    names3 = [s.name for s in got3]
    assert "proc-exact-write-from-spec" in names3, f"expected proc-exact-write-from-spec, got {names3}"

    # Multi-file rename: proc-rename-via-write wins triggered slot (rename in desc);
    # ranking fills remaining 2 slots — python-methodology or two-module-create can appear.
    got4 = select_skills("code", REPO_SKILLS, query="rename symbol across files", files=["pedido.py", "relatorio.py"])
    names4 = [s.name for s in got4]
    assert "proc-rename-via-write" in names4, f"expected proc-rename-via-write in triggered slot, got {names4}"
    # At SELECT_LIMIT=3, at least one methodology/proc should surface alongside rename.
    assert len(names4) >= 2, f"expected at least 2 skills for multi-file rename, got {names4}"
