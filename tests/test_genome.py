from pathlib import Path

import pytest

from harness.genome.genome import Genome, check_patch, load, matches, violation_path

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "config" / "genome.toml"


def _toml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "genome.toml"
    p.write_text(body, encoding="utf-8")
    return p


# ------------------------------------------------------------------- load


def test_load_shipped_config():
    g = load(SHIPPED)
    assert "harness/ruler/**" in g.immutable
    assert "uv.lock" in g.immutable
    assert "benchmarks/sealed/**" in g.immutable
    assert g.mutable == (
        "config/*.toml",
        "config/workflows/*.toml",
        "prompts/**",
        "skills/**",
        "plugins/**",
        "benchmarks/quarantine/**",
    )


def test_load_normalizes_patterns(tmp_path):
    g = load(_toml(tmp_path, 'immutable = ["./a/b/**", "c/"]\nmutable = ["d/*.toml"]\n'))
    assert g.immutable == ("a/b/**", "c")


def test_load_missing_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nao-existe.toml")


def test_load_rejects_duplicated_pattern(tmp_path):
    path = _toml(
        tmp_path,
        'immutable = ["harness/ruler/**", "config/*.toml"]\n'
        'mutable = ["config/*.toml", "prompts/**"]\n',
    )
    with pytest.raises(ValueError, match="config/\\*.toml"):
        load(path)


def test_load_rejects_empty_immutable(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        load(_toml(tmp_path, 'immutable = []\nmutable = ["config/*.toml"]\n'))


def test_load_rejects_wrong_type(tmp_path):
    with pytest.raises(ValueError):
        load(_toml(tmp_path, 'immutable = "harness/ruler/**"\n'))


# --------------------------------------------------------------- matching


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("harness/ruler/wilson.py", True),
        ("harness/ruler/sub/dir/deep.py", True),  # ** atravessa segmentos
        ("harness/ruler", False),  # o diretório não é arquivo mudado
        ("harness/rulerx.py", False),
        ("harness/cli.py", False),
        ("uv.lock", True),
        ("benchmarks/sealed/task_s01/verify.py", True),
        ("benchmarks/held_in/task/verify.py", False),
    ],
)
def test_shipped_immutable_matching(rel, expected):
    assert matches(rel, load(SHIPPED).immutable) is expected


def test_star_does_not_cross_separator():
    g = Genome(immutable=("config/*.toml",))
    assert matches("config/models.toml", g.immutable)
    assert not matches("config/sub/models.toml", g.immutable)


def test_question_mark_and_char_class():
    g = Genome(immutable=("data/v?.tsv", "logs/[0-9].log", "src/[!x]*.py"))
    assert matches("data/v1.tsv", g.immutable)
    assert not matches("data/v10.tsv", g.immutable)
    assert matches("logs/7.log", g.immutable)
    assert not matches("logs/a.log", g.immutable)
    assert matches("src/a1.py", g.immutable)
    assert not matches("src/x1.py", g.immutable)


def test_double_star_in_the_middle():
    g = Genome(immutable=("projects/**/notes.tsv",))
    assert matches("projects/site/notes.tsv", g.immutable)
    assert matches("projects/a/b/notes.tsv", g.immutable)
    assert matches("projects/notes.tsv", g.immutable)  # zero segmentos
    assert not matches("notes.tsv", g.immutable)


# ------------------------------------------------------------ check_patch


def test_patch_in_ruler_is_violation():
    g = load(SHIPPED)
    assert check_patch(g, ["harness/ruler/wilson.py"]) == [
        "genome:immutable:harness/ruler/wilson.py"
    ]


def test_patch_in_config_is_clean():
    g = load(SHIPPED)
    assert check_patch(g, ["config/models.toml", "prompts/build.md"]) == []


def test_patch_outside_both_groups_is_clean():
    # código comum é trabalho normal do harness, não violação.
    assert check_patch(load(SHIPPED), ["harness/cli.py", "README.md"]) == []


def test_runtime_collision_is_conflict():
    # `load()` só barra o padrão IDÊNTICO nos dois grupos; padrões diferentes
    # cobrindo o mesmo path só colidem aqui, e a colisão falha fechado.
    g = Genome(immutable=("config/genome.toml",), mutable=("config/*.toml",))
    assert check_patch(g, ["config/genome.toml"]) == ["genome:conflict:config/genome.toml"]
    assert check_patch(g, ["config/models.toml"]) == []


def test_escape_is_violation():
    g = load(SHIPPED)
    assert check_patch(g, ["../fora.py"]) == ["genome:escape:../fora.py"]
    assert check_patch(g, ["/etc/passwd"]) == ["genome:escape:/etc/passwd"]
    assert check_patch(g, ["a/../../fora.py"]) == ["genome:escape:a/../../fora.py"]


def test_relative_path_is_normalized_against_root(tmp_path):
    g = Genome(immutable=("harness/ruler/**",))
    (tmp_path / "harness" / "ruler").mkdir(parents=True)
    assert check_patch(g, ["./harness/ruler/wilson.py"], root=tmp_path) == [
        "genome:immutable:harness/ruler/wilson.py"
    ]
    assert check_patch(g, [str(tmp_path / "harness/ruler/wilson.py")], root=tmp_path) == [
        "genome:immutable:harness/ruler/wilson.py"
    ]


def test_symlink_out_of_root_is_escape(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (tmp_path / "fora").mkdir()
    (root / "config" / "link.toml").symlink_to(tmp_path / "fora" / "x.toml")
    g = Genome(immutable=("config/*.toml",))
    # sem root, é só um path relativo inocente; com root, o link é visto.
    assert check_patch(g, ["config/link.toml"]) == ["genome:immutable:config/link.toml"]
    assert check_patch(g, ["config/link.toml"], root=root) == [
        "genome:escape:config/link.toml"
    ]


def test_violations_are_deduped_and_ordered():
    g = load(SHIPPED)
    changed = ["config/models.toml", "uv.lock", "harness/graph/state.py", "uv.lock"]
    assert check_patch(g, changed) == [
        "genome:immutable:uv.lock",
        "genome:immutable:harness/graph/state.py",
    ]


def test_violation_path_roundtrip():
    assert violation_path("genome:immutable:a/b.py") == "a/b.py"
    assert violation_path("tamper:genome_violation:a/b.py") == "a/b.py"
    assert violation_path("genome:escape:/etc/x:y") == "/etc/x:y"
