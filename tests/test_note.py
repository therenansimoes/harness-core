from pathlib import Path

import pytest

from harness.ruler import note


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_PROJECTS_ROOT", str(tmp_path / "projects"))
    return tmp_path / "projects"


def test_add_cria_arquivo_com_header(projects):
    path = note.add("site", "site/0001", 4, "layout", "ficou bom")
    assert path == projects / "site" / "notes.tsv"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == list(note.HEADER)
    assert lines[1].split("\t")[1:] == ["site/0001", "4", "layout", "ficou bom"]


def test_add_e_append_only(projects):
    note.add("site", "u1", 3)
    note.add("site", "u2", 5, why="ótimo")
    rows = note.load_notes("site")
    assert [r["unit_id"] for r in rows] == ["u1", "u2"]
    assert [r["score"] for r in rows] == ["3", "5"]


def test_add_neutraliza_tab_e_quebra_de_linha(projects):
    note.add("site", "u1", 2, why="linha1\nlinha2\tcom tab")
    lines = note.notes_path("site").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert note.load_notes("site")[0]["why"] == "linha1 linha2 com tab"


@pytest.mark.parametrize("score", [0, 6, -1, 3.5, "4", None])
def test_add_recusa_score_invalido(projects, score):
    with pytest.raises(ValueError):
        note.add("site", "u1", score)
    assert not note.notes_path("site").exists()


def test_kpi_sem_notas(projects):
    assert note.kpi_value("site") is None


def test_kpi_abaixo_do_minimo(projects):
    note.add("site", "u1", 5)
    note.add("site", "u2", 5)
    assert note.kpi_value("site") is None


def test_kpi_media_a_partir_do_minimo(projects):
    for score in (5, 4, 3):
        note.add("site", "u", score)
    assert note.kpi_value("site") == 4.0


def test_kpi_respeita_a_janela(projects):
    for score in (1, 1, 1, 5, 5, 5):
        note.add("site", "u", score)
    assert note.kpi_value("site", window=3) == 5.0
    assert note.kpi_value("site") == 3.0


def test_kpi_min_notes_customizado(projects):
    note.add("site", "u1", 4)
    assert note.kpi_value("site", min_notes=1) == 4.0


def test_kpi_ignora_linha_corrompida(projects):
    for score in (4, 4, 4):
        note.add("site", "u", score)
    with note.notes_path("site").open("a", encoding="utf-8") as fh:
        fh.write("2026-01-01\tu\tnão-é-número\t\t\n")
        fh.write("2026-01-01\tu\t9\t\t\n")
    assert note.kpi_value("site") == 4.0


def test_backend_nao_alcanca_o_canal_de_nota():
    # `add` é do CLI humano: o executor avaliado não escreve a própria nota.
    import harness.backends

    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in Path(harness.backends.__file__).parent.rglob("*.py")
    )
    assert "ruler.note" not in src
    assert "ruler import note" not in src
