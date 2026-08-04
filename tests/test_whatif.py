"""Replay contrafactual: conta salvos, ajusta denominador e não suja nada.

O executor é o mock, então "a config conserta a unidade" é encenado pelo
`verify_cmd`: o mock escreve `mock_output.txt` e mais nada, logo unidade que
verifica esse arquivo aceita e unidade que pede outro reprova. É o mesmo truque
de `tests/test_exam.py` — o que está sob teste aqui é o seletor, o denominador e
o isolamento, não a inteligência do executor.
"""

from pathlib import Path

import pytest

from harness.improve import counterfactual
from harness.ledger import store
from harness.memory import episodic
from harness.types import RunRow

SALVA_TOML = """\
id = "{uid}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "test -f mock_output.txt"
"""

NAO_SALVA_TOML = """\
id = "{uid}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "test -f nao_existe.txt"
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Data dir REAL do teste: é o ledger que o whatif lê e não pode escrever."""
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def root(tmp_path):
    """Raiz com `benchmarks/whatif/` — onde as unidades são resolvidas."""
    bench = tmp_path / "root" / "benchmarks" / "whatif"
    bench.mkdir(parents=True)
    return tmp_path / "root"


def _unit(root: Path, name: str, template: str) -> None:
    unit = root / "benchmarks" / "whatif" / name
    unit.mkdir(parents=True)
    (unit / "unit.toml").write_text(template.format(uid=name), encoding="utf-8")


def _fail(unit_id: str, **over) -> RunRow:
    base = {
        "run_id": f"run-{unit_id}", "unit_id": unit_id, "project": None,
        "backend": "mock", "model": None, "tier": "tier0", "kind": "code",
        "ok": False, "exit_reason": "verify_failed", "sec_total": 1.0,
        "sec_provision": 0.1, "cost_usd": 0.0, "intervention": False,
        "created_at": store.now_iso(),
    }
    base.update(over)
    return RunRow(**base)


def _seed(*rows: RunRow) -> None:
    for row in rows:
        store.record_run(row)


def test_salvou_2_de_3(data_dir, root):
    """3 fracassos no ledger, config atual conserta 2."""
    _seed(_fail("u_a"), _fail("u_b"), _fail("u_c"))
    _unit(root, "u_a", SALVA_TOML)
    _unit(root, "u_b", SALVA_TOML)
    _unit(root, "u_c", NAO_SALVA_TOML)

    report = counterfactual.whatif(root=root)

    assert report.requested == 3
    assert len(report.eligible) == 3
    assert report.rescued == 2
    assert "salvou 2 de 3" in counterfactual.format_report(report)
    salvos = {c.unit_id for c in report.cases if c.saved}
    assert salvos == {"u_a", "u_b"}


def test_unidade_ausente_vira_skipped_e_sai_do_denominador(data_dir, root):
    _seed(_fail("u_a"), _fail("u_sumiu"), _fail("u_c"))
    _unit(root, "u_a", SALVA_TOML)
    _unit(root, "u_c", NAO_SALVA_TOML)

    report = counterfactual.whatif(root=root)
    text = counterfactual.format_report(report)

    assert [c.unit_id for c in report.skipped] == ["u_sumiu"]
    assert len(report.eligible) == 2
    assert report.rescued == 1
    # Denominador é o elegível (2), não o pedido (3).
    assert "salvou 1 de 2" in text
    assert "2 elegíveis de 3 pedidas" in text
    assert "skipped: ['u_sumiu']" in text
    assert next(c for c in report.cases if c.unit_id == "u_sumiu").saved is None


def test_ledger_real_inalterado(data_dir, root):
    """A avaliação não pode ganhar linha no ledger que ela mede."""
    _seed(_fail("u_a"), _fail("u_b"))
    _unit(root, "u_a", SALVA_TOML)
    _unit(root, "u_b", NAO_SALVA_TOML)

    antes = len(store.history(limit=1000))
    report = counterfactual.whatif(root=root)
    depois = len(store.history(limit=1000))

    assert report.rescued == 1          # rodou de verdade
    assert antes == depois == 2


def test_episodica_nao_ganha_registros(data_dir, root):
    """`u_b` falha no replay; a falha não vira episódio na memória global."""
    _seed(_fail("u_b"))
    _unit(root, "u_b", NAO_SALVA_TOML)

    antes = len(episodic.episodes())
    counterfactual.whatif(root=root)
    assert len(episodic.episodes()) == antes == 0


def test_env_data_dir_restaurada(data_dir, root, monkeypatch):
    """O tmpdir do isolamento não pode sobreviver na env de quem chamou."""
    _seed(_fail("u_a"))
    _unit(root, "u_a", SALVA_TOML)

    counterfactual.whatif(root=root)
    import os

    assert os.environ["HARNESS_DATA_DIR"] == str(data_dir)


def test_dedupe_por_unidade(data_dir, root):
    """Mesma unidade falha 3x: um caso, não três."""
    _seed(
        _fail("u_a", run_id="r1"), _fail("u_a", run_id="r2"), _fail("u_a", run_id="r3")
    )
    _unit(root, "u_a", SALVA_TOML)

    report = counterfactual.whatif(root=root)
    assert report.requested == 1
    assert "salvou 1 de 1" in counterfactual.format_report(report)


def test_ok_nao_entra(data_dir, root):
    _seed(_fail("u_a", ok=True))
    _unit(root, "u_a", SALVA_TOML)

    report = counterfactual.whatif(root=root)
    assert report.cases == ()
    assert "nenhum fracasso" in counterfactual.format_report(report)


def test_filtro_de_kind(data_dir, root):
    _seed(_fail("u_a", kind="code"), _fail("u_b", kind="content"))
    _unit(root, "u_a", SALVA_TOML)
    _unit(root, "u_b", SALVA_TOML)

    report = counterfactual.whatif(kind="content", root=root)
    assert [c.unit_id for c in report.cases] == ["u_b"]


def test_limit_corta(data_dir, root):
    _seed(_fail("u_a"), _fail("u_b"), _fail("u_c"))
    for name in ("u_a", "u_b", "u_c"):
        _unit(root, name, SALVA_TOML)

    report = counterfactual.whatif(limit=2, root=root)
    assert report.requested == 2


def test_frase_de_honestidade_com_repeat_1(data_dir, root):
    _seed(_fail("u_a"))
    _unit(root, "u_a", SALVA_TOML)

    text = counterfactual.format_report(counterfactual.whatif(root=root))
    assert "amostra única" in text
    assert "não substitui A/B" in text


def test_resolve_na_fila_do_projeto(data_dir, root):
    """Unidade em `projects/<p>/queue/done/<id>` é encontrada."""
    unit = root / "projects" / "p1" / "queue" / "done" / "u_q"
    unit.mkdir(parents=True)
    (unit / "unit.toml").write_text(SALVA_TOML.format(uid="u_q"), encoding="utf-8")

    assert counterfactual.resolve_unit(_fail("u_q", project="p1"), root) == unit
    # Sem `project` no ledger a varredura acha do mesmo jeito.
    assert counterfactual.resolve_unit(_fail("u_q"), root) == unit
    assert counterfactual.resolve_unit(_fail("u_nada"), root) is None


def test_excecao_no_run_conta_como_nao_salvo(data_dir, root, monkeypatch):
    _seed(_fail("u_a"))
    _unit(root, "u_a", SALVA_TOML)

    def boom(*a, **k):
        raise RuntimeError("explodiu")

    monkeypatch.setattr("harness.graph.run_graph.run_unit", boom)
    report = counterfactual.whatif(root=root)

    assert report.rescued == 0
    caso = report.cases[0]
    assert caso.saved is False
    assert "explodiu" in caso.error
    assert "explodiu" in counterfactual.format_report(report)
