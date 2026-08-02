from pathlib import Path

import pytest

from harness.genome.genome import DEFAULT_PATH, load
from harness.genome.tamper import GENOME_VIOLATION, detect, fingerprint, immutable_files

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "config" / "genome.toml"

# Árvore mínima com um arquivo de cada grupo do genoma.
TREE = {
    "config/genome.toml": None,  # cópia do shipped
    "config/models.toml": "tier0 = 'ollama'\n",
    "harness/ruler/wilson.py": "def wilson_interval(): ...\n",
    "harness/routing/router.py": "def select(): ...\n",
    "harness/cli.py": "def main(): ...\n",
    "prompts/build.md": "faça\n",
    "uv.lock": "version = 1\n",
    "benchmarks/sealed/task_s01/verify.py": "assert True\n",
    "benchmarks/held_in/task_h/verify.py": "assert True\n",
}


@pytest.fixture
def repo(tmp_path):
    """Repo de mentira, com o genoma de verdade dentro."""
    for rel, content in TREE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SHIPPED.read_text(encoding="utf-8") if content is None else content,
                     encoding="utf-8")
    return tmp_path


# ------------------------------------------------------- immutable_files


def test_immutable_files_lists_only_the_blocklist(repo):
    assert immutable_files(load(SHIPPED), repo) == [
        "benchmarks/sealed/task_s01/verify.py",
        "harness/routing/router.py",
        "harness/ruler/wilson.py",
        "uv.lock",
    ]


def test_immutable_files_skips_pycache(repo):
    cache = repo / "harness" / "ruler" / "__pycache__"
    cache.mkdir()
    (cache / "wilson.cpython-311.pyc").write_bytes(b"\x00")
    assert "harness/ruler/__pycache__/wilson.cpython-311.pyc" not in immutable_files(
        load(SHIPPED), repo
    )


# ---------------------------------------------------------- fingerprint


def test_fingerprint_is_stable(repo):
    g = load(SHIPPED)
    assert fingerprint(g, repo) == fingerprint(g, repo)


def test_fingerprint_changes_when_immutable_changes(repo):
    g = load(SHIPPED)
    before = fingerprint(g, repo)
    (repo / "harness" / "ruler" / "wilson.py").write_text("def wilson_interval(): return 1\n")
    assert fingerprint(g, repo) != before


def test_fingerprint_ignores_mutable_changes(repo):
    g = load(SHIPPED)
    before = fingerprint(g, repo)
    (repo / "config" / "models.toml").write_text("tier0 = 'outro'\n")
    (repo / "harness" / "cli.py").write_text("def main(): return 0\n")
    assert fingerprint(g, repo) == before


def test_fingerprint_sees_added_and_removed_immutable(repo):
    g = load(SHIPPED)
    before = fingerprint(g, repo)
    (repo / "harness" / "ruler" / "kpi.py").write_text("def collect(): ...\n")
    added = fingerprint(g, repo)
    assert added != before
    (repo / "harness" / "ruler" / "kpi.py").unlink()
    assert fingerprint(g, repo) == before


def test_fingerprint_of_real_repo_is_stable():
    g = load(SHIPPED)
    assert fingerprint(g, REPO) == fingerprint(g, REPO)
    assert "uv.lock" in immutable_files(g, REPO)


# --------------------------------------------------------------- detect


def test_patch_in_ruler_is_tamper(repo):
    before = fingerprint(load(SHIPPED), repo)
    assert detect(repo, before, ["harness/ruler/wilson.py"]) == [
        "tamper:genome_violation:harness/ruler/wilson.py"
    ]


def test_patch_in_config_is_clean(repo):
    before = fingerprint(load(SHIPPED), repo)
    assert detect(repo, before, ["config/models.toml"]) == []


def test_patch_in_ordinary_code_is_clean(repo):
    before = fingerprint(load(SHIPPED), repo)
    assert detect(repo, before, ["harness/cli.py", "benchmarks/held_in/task_h/verify.py"]) == []


def test_silent_edit_of_immutable_is_caught(repo):
    """Ninguém declarou nada — quem pega é o fingerprint."""
    before = fingerprint(load(SHIPPED), repo)
    (repo / "harness" / "routing" / "router.py").write_text("def select(): return 'caro'\n")
    assert detect(repo, before, []) == ["tamper:immutable_changed"]


def test_both_signals_when_declared_and_changed(repo):
    before = fingerprint(load(SHIPPED), repo)
    (repo / "harness" / "ruler" / "wilson.py").write_text("z = 0\n")
    assert detect(repo, before, ["harness/ruler/wilson.py"]) == [
        "tamper:genome_violation:harness/ruler/wilson.py",
        "tamper:immutable_changed",
    ]


def test_detect_loads_genome_from_root_by_default(repo):
    (repo / DEFAULT_PATH).unlink()
    with pytest.raises(FileNotFoundError):
        detect(repo, "", [])


def test_caller_genome_beats_the_sandbox_copy(repo):
    """A cópia do genoma dentro da sandbox é o que está sob suspeita.

    Com o genoma esvaziado lá dentro, o default não vê a violação declarada —
    só o fingerprint contra a baseline canônica denuncia (por isso o caller
    passa o genoma do repo).
    """
    canonical = load(SHIPPED)
    before = fingerprint(canonical, repo)
    (repo / DEFAULT_PATH).write_text('immutable = ["nada/**"]\nmutable = []\n')

    sandbox_view = detect(repo, before, ["harness/ruler/wilson.py"])
    assert not [v for v in sandbox_view if v.startswith(GENOME_VIOLATION)]
    assert detect(repo, before, ["harness/ruler/wilson.py"], genome=canonical) == [
        "tamper:genome_violation:harness/ruler/wilson.py"
    ]


def test_escape_is_reported_as_violation(repo):
    before = fingerprint(load(SHIPPED), repo)
    assert detect(repo, before, ["../fora.py"]) == ["tamper:genome_violation:../fora.py"]


def test_empty_baseline_fails_closed(repo):
    assert detect(repo, "", []) == ["tamper:immutable_changed"]


def test_genome_toml_itself_is_mutable(repo):
    """Registro do buraco conhecido: `config/*.toml` cobre o próprio genoma.

    Fechar isso muda a lista do `config/genome.toml`, que é decisão de spec —
    aqui só se documenta o comportamento atual para ele não mudar sem querer.
    """
    g = load(SHIPPED)
    before = fingerprint(g, repo)
    assert detect(repo, before, ["config/genome.toml"], genome=g) == []
