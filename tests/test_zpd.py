"""Currículo ZPD: escolhe a unidade cuja nota histórica ainda informa."""

import json
from pathlib import Path

import pytest

from harness.improve import zpd
from harness.ledger import store
from harness.projects import UNIT_FILE
from harness.routing import CONFIG_DIR_ENV
from harness.ruler.gate import Decision
from harness.types import RunRow


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _run(unit_id: str, run_id: str, scores: list[float], db: Path) -> None:
    """Uma run no ledger + o payload do verify de cada tentativa dela."""
    store.record_run(
        RunRow(
            run_id=run_id, unit_id=unit_id, project=None, backend="mock", model=None,
            tier=None, kind="code", ok=False, exit_reason="verify_failed",
            sec_total=1.0, sec_provision=0.1, cost_usd=0.0, intervention=False,
            created_at=store.now_iso(),
        ),
        db,
    )
    for attempt, score in enumerate(scores):
        store.record_node(
            run_id,
            "verify",
            {"passed": False, "exit_code": 64, "log_path": "v.log", "sec": 0.1,
             "score": score, "failed": ["c1"]},
            db,
            attempt=attempt,
        )


def _units(tmp_path: Path, *names: str) -> list[Path]:
    out = []
    for name in names:
        d = tmp_path / "units" / name
        d.mkdir(parents=True)
        (d / UNIT_FILE).write_text(f'id = "{name}"\n', encoding="utf-8")
        out.append(d)
    return out


def test_escolhe_a_da_zona(tmp_path, data_dir):
    db = data_dir / store.DB_NAME
    _run("fechada", "r1", [0.95, 1.0], db)     # acima da zona: já resolvida
    _run("perdida", "r2", [0.05, 0.1], db)     # abaixo: sem gradiente para subir
    _run("na-zona", "r3", [0.5, 0.6], db)      # média 0.55
    units = _units(tmp_path, "fechada", "perdida", "na-zona")

    assert zpd.next_unit(units=units, db=db).name == "na-zona"


def test_mais_alta_da_zona_primeiro(tmp_path, data_dir):
    """Entre duas informativas ganha a mais perto de fechar."""
    db = data_dir / store.DB_NAME
    _run("quase", "r1", [0.75], db)
    _run("longe", "r2", [0.45], db)
    units = _units(tmp_path, "quase", "longe")

    assert zpd.next_unit(units=units, db=db).name == "quase"


def test_empate_e_estavel(tmp_path, data_dir):
    """Nota igual: desempate por nome — duas leituras dão a mesma resposta."""
    db = data_dir / store.DB_NAME
    _run("bbb", "r1", [0.5], db)
    _run("aaa", "r2", [0.5], db)
    units = _units(tmp_path, "bbb", "aaa")

    primeira = zpd.next_unit(units=units, db=db)
    assert primeira.name == "aaa"
    assert zpd.next_unit(units=list(reversed(units)), db=db).name == "aaa"


def test_sem_candidata_na_zona_devolve_none(tmp_path, data_dir):
    db = data_dir / store.DB_NAME
    _run("fechada", "r1", [1.0], db)
    _run("perdida", "r2", [0.0], db)
    units = _units(tmp_path, "fechada", "perdida")

    assert zpd.next_unit(units=units, db=db) is None
    # Sem histórico nenhum também é None: ZPD é acessório, não opinião default.
    assert zpd.next_unit(units=_units(tmp_path, "nova"), db=db) is None


def test_media_usa_as_ultimas_k_tentativas(tmp_path, data_dir):
    """Nota antiga não segura a unidade fora da zona: a janela é K=5."""
    db = data_dir / store.DB_NAME
    _run("u", "r-velho", [0.0, 0.0, 0.0], db)
    _run("u", "r-novo", [0.7, 0.7, 0.7], db)

    scores = zpd.unit_scores(db=db, k=5)
    # 3 notas novas + as 2 mais recentes da run velha; o resto caiu da janela.
    assert scores["u"] == pytest.approx((0.7 * 3 + 0.0 * 2) / 5)
    assert zpd.next_unit(units=_units(tmp_path, "u"), db=db).name == "u"


def test_payload_sem_score_nao_conta(tmp_path, data_dir):
    """Run anterior à régua graduada não tem nota — inventar 1.0 mentiria."""
    db = data_dir / store.DB_NAME
    store.record_run(
        RunRow(
            run_id="r-velho", unit_id="u", project=None, backend="mock", model=None,
            tier=None, kind=None, ok=False, exit_reason="verify_failed",
            sec_total=1.0, sec_provision=0.1, cost_usd=0.0, intervention=False,
            created_at=store.now_iso(),
        ),
        db,
    )
    store.record_node(
        "r-velho", "verify",
        {"passed": False, "exit_code": 1, "log_path": "v.log", "sec": 0.1}, db,
    )

    assert zpd.unit_scores(db=db) == {}


def test_order_poe_a_escolha_na_frente(tmp_path, data_dir):
    db = data_dir / store.DB_NAME
    _run("03-tres", "r1", [0.6], db)
    units = _units(tmp_path, "01-um", "02-dois", "03-tres")

    assert [p.name for p in zpd.order(units, db=db)] == [
        "03-tres", "01-um", "02-dois"
    ]
    # Sem candidata a ordem do chamador fica intacta.
    assert zpd.order(_units(tmp_path, "a", "b"), db=db)[0].name == "a"


# --- integração com o driver da fila ------------------------------------------


@pytest.fixture
def fila(tmp_path, monkeypatch) -> Path:
    """Fila fake de 3 unidades + registro de projeto (sem git: `move=False`)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    queue = tmp_path / "queue"
    for name in ("01-um", "02-dois", "03-tres"):
        d = queue / name
        d.mkdir(parents=True)
        (d / UNIT_FILE).write_text(f'id = "{name}"\n', encoding="utf-8")
    (cfg / "projects.toml").write_text(
        f'[projects.t]\nrepo = {json.dumps(str(tmp_path))}\n'
        f'queue_dir = {json.dumps(str(queue))}\n',
        encoding="utf-8",
    )
    return queue


def _fake_run_unit(visto: list[str]):
    def run_unit(unit_dir, backend, model, data_dir, thread_id, max_attempts=None):
        visto.append(unit_dir.name)
        return {"decision": Decision(action="accept", reason="fake")}

    return run_unit


def test_queue_zpd_reordena(fila, monkeypatch):
    from harness import queue as queue_mod

    db = store.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    _run("03-tres", "r1", [0.6], db)
    visto: list[str] = []
    monkeypatch.setattr(queue_mod, "run_unit", _fake_run_unit(visto))

    assert queue_mod.run_queue("t", move=False, use_zpd=True) == 0
    assert visto[0] == "03-tres"


def test_queue_default_mantem_ordem_de_nome(fila, monkeypatch):
    """Sem `--zpd` a ordem de nome (a dependência da fila) manda, com ou sem
    histórico na zona."""
    from harness import queue as queue_mod

    db = store.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    _run("03-tres", "r1", [0.6], db)
    visto: list[str] = []
    monkeypatch.setattr(queue_mod, "run_unit", _fake_run_unit(visto))

    assert queue_mod.run_queue("t", move=False) == 0
    assert visto == ["01-um", "02-dois", "03-tres"]
