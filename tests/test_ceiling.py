"""`ceiling`: teto de gasto EXPLÍCITO (`--max-usd`), fail-closed, checado antes
de cada dispatch. Não confundir com `pressure.cost_cap_usd` (ambiente,
fail-open, pós-hoc), coberto em test_governor_pressure.py — os dois mecanismos
não se tocam, e este arquivo isola o segundo pra não vazar nos asserts do
primeiro."""

from pathlib import Path

import pytest

from harness.backends.mock import OUTPUT_FILE
from harness.governor import ceiling
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV

# --- pure functions: check / spent_for_run -----------------------------------


def test_check_inativo_ignora_prior_absurdo():
    cel = ceiling.Ceiling(limit_usd=0.0, prior_usd=999.0)
    assert ceiling.check(cel, "r1", Path("/nao/existe.sqlite"), 0, True) is ceiling.NO_BREACH


def test_check_limiar_igual_ao_check_cost(tmp_path):
    db = tmp_path / "runs.sqlite"
    folgado = ceiling.Ceiling(limit_usd=1.0, prior_usd=0.99)
    no_limiar = ceiling.Ceiling(limit_usd=1.0, prior_usd=1.0)

    assert ceiling.check(folgado, "r1", db, 0, True) is ceiling.NO_BREACH

    br = ceiling.check(no_limiar, "r1", db, 0, True)
    assert br.fired and br.reason == ceiling.BREACH_REASON and br.spent_usd == 1.0


def test_check_prior_desconhecido_e_blind(tmp_path):
    db = tmp_path / "runs.sqlite"
    cel = ceiling.Ceiling(limit_usd=1.0, prior_usd=None)

    br = ceiling.check(cel, "r1", db, 0, True)

    assert br.fired and br.reason == ceiling.BLIND_REASON


def test_check_backend_sem_custo_com_teto_ativo_barra(tmp_path):
    db = tmp_path / "runs.sqlite"
    cel = ceiling.Ceiling(limit_usd=1.0, prior_usd=0.0)

    br = ceiling.check(cel, "r1", db, 0, False)

    assert br.fired and br.reason == ceiling.NO_COST_REASON


def test_spent_for_run_propaga_excecao(tmp_path, monkeypatch):
    db = tmp_path / "runs.sqlite"
    monkeypatch.setattr(
        store, "get_node", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("banco sumiu"))
    )

    with pytest.raises(RuntimeError):
        ceiling.spent_for_run("r1", db, 0)


def test_check_converte_excecao_do_spent_for_run_em_blind(tmp_path, monkeypatch):
    db = tmp_path / "runs.sqlite"
    monkeypatch.setattr(
        store, "get_node", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("banco sumiu"))
    )
    cel = ceiling.Ceiling(limit_usd=1.0, prior_usd=0.0)

    # `attempt=1`: `through_attempt = attempt - 1 = 0` de fato bate no ledger
    # (com `attempt=0` o `through_attempt` seria -1, e `spent_for_run` nem
    # chamaria `store.get_node` — a exceção nunca aconteceria).
    br = ceiling.check(cel, "r1", db, 1, True)

    assert br.fired and br.reason == ceiling.BLIND_REASON


# --- enforcement no grafo -----------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def sem_pressao_ambiente(tmp_path, monkeypatch):
    """Isola `pressure.cost_cap_usd`: sem isto o `config/governor.toml` DESTE
    REPO (cost_cap_usd=5.0 — `./config` do cwd vence em `config_dir()`)
    vazaria pros testes do teto explícito, e os dois mecanismos, que não se
    tocam por design, se confundiriam nos asserts daqui."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "governor.toml").write_text("[pressure]\ncost_cap_usd = 0.0\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def _unit(tmp_path: Path, name: str, verify_cmd: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "x"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    return unit


@pytest.fixture
def caro(monkeypatch):
    """Mock cobrando $2.50 por tentativa: o mock real reporta 0.0 (grátis) e
    nenhum teto o alcançaria."""
    from dataclasses import replace as _replace

    from harness.backends.mock import MockBackend

    original = MockBackend.execute
    monkeypatch.setattr(
        MockBackend,
        "execute",
        lambda self, req: _replace(original(self, req), cost_usd=2.5),
    )


def test_teto_estoura_no_gate_depois_da_segunda_tentativa(
    caro, sem_pressao_ambiente, data_dir, tmp_path
):
    """DESVIO do spec original (harness-core-di9 §Testes #6): o teto pedido
    ($3.00) NÃO cabe na primeira tentativa sozinha ($2.50), então o `_execute`
    da segunda tentativa faz a MESMA soma que o `_gate` já fez depois da
    primeira (mesma janela `through_attempt`) — os dois checks são
    matematicamente equivalentes dentro da mesma corrida, e se o `_gate`
    deixou passar o retry o `_execute` seguinte também deixa. O teto só fecha
    quando a SEGUNDA tentativa empurra o acumulado ($5.00) pra cima do limite,
    e quem barra aí é o `_gate` olhando pra frente (attempt + 1), não o
    `_execute` recusando um dispatch. O caminho em que `_execute` barra de
    verdade — SEM dispatch nenhum — é `spent_before` alto ou gasto ilegível:
    ver `test_breach_no_attempt_0_nao_aceita_mesmo_com_verify_verde` e
    `test_spent_before_desconhecido_barra_o_dispatch` logo abaixo.
    """
    unit = _unit(tmp_path, "teto-gate", "test -f nao_existe.txt")
    final = run_unit(
        unit, "mock", None, data_dir, thread_id="t-teto-gate", max_attempts=5, max_usd=3.0
    )

    assert final["decision"].action == "escalate_human"
    assert ceiling.BREACH_REASON in final["decision"].reason
    assert final["attempt"] == 1  # rodaram 0 e 1; a terceira nunca aconteceu

    execs = [e for e in final["events"] if e["node"] == "execute"]
    assert len(execs) == 2
    assert all("ceiling" not in e for e in execs)  # nenhuma tentativa foi barrada

    row = store.history()[0]
    assert row.exit_reason == "verify_failed"  # o motivo real da última tentativa
    assert row.ok is False
    assert row.cost_usd == 2.5  # custo do ÚLTIMO exec, não a soma acumulada


def test_breach_no_attempt_0_nao_aceita_mesmo_com_verify_verde(
    sem_pressao_ambiente, data_dir, tmp_path
):
    """Teto já estourado ANTES da primeira tentativa (`spent_before`) barra o
    `_execute` sem despachar nada — e o `_gate` tem que sobrepor mesmo um
    verify VERDE: aceitar um attempt que nunca rodou é o fail-open que o teto
    existe pra evitar. Ledger: `exit_reason == "budget"`, `ok` falso,
    `cost_usd == 0.0` — o caso pinado no spec original."""
    unit = _unit(tmp_path, "breach-attempt0", "true")
    final = run_unit(
        unit,
        "mock",
        None,
        data_dir,
        thread_id="t-breach0",
        max_attempts=5,
        max_usd=1.0,
        spent_before=1.0,
    )

    assert not (Path(final["workspace"]) / OUTPUT_FILE).exists()  # zero dispatch
    assert final["decision"].action == "escalate_human"
    assert ceiling.BREACH_REASON in final["decision"].reason
    assert "accept" not in [e["node"] for e in final["events"]]
    assert final["attempt"] == 0

    row = store.history()[0]
    assert row.exit_reason == "budget"
    assert row.ok is False
    assert row.cost_usd == 0.0


def test_sem_teto_o_grafo_e_byte_a_byte_o_de_hoje(caro, sem_pressao_ambiente, data_dir, tmp_path):
    """Mesma unit cara, SEM `--max-usd`: decisão, tentativas e nós idênticos
    ao comportamento de sempre — `ceiling` não aparece em evento nenhum."""
    unit = _unit(tmp_path, "sem-teto", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-sem-teto", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert "acabaram as 2 tentativas" in final["decision"].reason
    assert final["attempt"] == 1
    assert all("ceiling" not in e for e in final["events"])


def test_spent_before_desconhecido_barra_o_dispatch(sem_pressao_ambiente, data_dir, tmp_path):
    """`spent_before=None` é gasto DESCONHECIDO, não zero — com teto ativo
    isso barra por precaução (BLIND_REASON) mesmo sem nenhum gasto real."""
    unit = _unit(tmp_path, "blind", "true")
    final = run_unit(
        unit,
        "mock",
        None,
        data_dir,
        thread_id="t-blind",
        max_attempts=5,
        max_usd=5.0,
        spent_before=None,
    )

    assert not (Path(final["workspace"]) / OUTPUT_FILE).exists()
    assert final["decision"].action == "escalate_human"
    assert ceiling.BLIND_REASON in final["decision"].reason
    assert final["attempt"] == 0
