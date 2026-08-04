"""Memória de casos: escalação respondida por humano vira precedente.

A ponta de escrita é o resume do `harness improve` (o único ponto em que a
pergunta do loop e a resposta do humano coexistem); a ponta de leitura é a
evidência da escalação seguinte. Sem estes testes o banco podia ficar vazio para
sempre — ou pior, ficar cheio e nunca ser lido.
"""

import json

import pytest

from harness import cli
from harness.improve import escalate as esc
from harness.memory import decisions

CTX = '{"history": 0, "catalog": 3}'
ANSWER = '{"action": "continue", "rule_id": "floor_up"}'


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Namespace GLOBAL da memória: é `HARNESS_DATA_DIR` que decide de qual
    história o precedente sai (mesma fixture do test_episodic)."""
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def test_record_and_recall_by_kind_and_reason(data_dir):
    assert decisions.record_decision("code", esc.NO_GRADIENT, CTX, ANSWER) is True

    cases = decisions.recall_decisions("code", esc.NO_GRADIENT)

    assert cases and ANSWER in cases[0]
    # O motivo e o contexto viajam com a resposta: quem lê está fora do caso.
    assert esc.NO_GRADIENT in cases[0] and "catalog" in cases[0]
    # Outro kind não contamina quem não pediu, e outro motivo tampouco.
    assert decisions.recall_decisions("prose", esc.NO_GRADIENT) == []
    assert decisions.recall_decisions("code", esc.DEADLINE) == []


def test_new_escalation_carries_prior_decision(data_dir):
    """O caso fecha aqui: gravado no resume, lido na parada seguinte."""
    decisions.record_decision("code", esc.NO_GRADIENT, CTX, ANSWER)

    payload = esc.payload(
        esc.NO_GRADIENT, unit="u1", evidence={"history": 0}, kind="code"
    )

    prior = payload["evidence"]["prior_decisions"]
    assert "humano disse antes (não é ordem)" in prior
    assert "floor_up" in prior
    # `kind` na evidência é o que deixa o lado da resposta gravar na célula
    # certa sem reabrir o checkpoint procurando as unidades.
    assert payload["evidence"]["kind"] == "code"
    # Escalação sem kind não inventa precedente (nem chave para procurá-lo).
    sem_kind = esc.payload(esc.NO_GRADIENT, unit="u1", evidence={"history": 0})
    assert "prior_decisions" not in sem_kind["evidence"]


def test_cli_resume_records_the_case(data_dir):
    """Ponta de escrita do `--resume`: evidência da escalação + JSON da resposta.

    `prior_decisions` NÃO entra no contexto gravado — precedente regravado como
    contexto faria cada caso carregar o anterior inteiro.
    """
    pending = {
        "reason": esc.DEADLINE,
        "unit": ["u1"],
        "mutation": None,
        "evidence": {
            "node": "run_arms",
            "kind": "code",
            "prior_decisions": "## humano disse antes (não é ordem)\n- lixo",
        },
    }

    cli._record_human_decision(pending, {"action": "abort"})

    cases = decisions.recall_decisions("code", esc.DEADLINE)
    assert cases and json.dumps({"action": "abort"}) in cases[0]
    assert "run_arms" in cases[0]
    assert "humano disse antes" not in cases[0]


def test_fail_open_sem_fts5(data_dir, monkeypatch):
    """Sqlite sem FTS5 (ou banco quebrado): grava não grava, lê não lê, e a
    escalação sai normal — memória que derruba o loop vale menos que nenhuma."""
    def boom(*a, **kw):
        raise RuntimeError("no such module: fts5")

    monkeypatch.setattr(decisions, "_connect", boom)

    assert decisions.record_decision("code", esc.NO_GRADIENT, CTX, ANSWER) is False
    assert decisions.recall_decisions("code", esc.NO_GRADIENT) == []

    payload = esc.payload(esc.NO_GRADIENT, unit="u1", evidence={"n": 1}, kind="code")
    assert payload["reason"] == esc.NO_GRADIENT
    assert payload["evidence"]["n"] == 1
    assert "prior_decisions" not in payload["evidence"]


def test_kill_switch_nao_grava_nem_le(data_dir, monkeypatch):
    monkeypatch.setenv(decisions.ENV_ENABLED, "0")
    assert decisions.record_decision("code", esc.NO_GRADIENT, CTX, ANSWER) is False

    monkeypatch.delenv(decisions.ENV_ENABLED)
    decisions.record_decision("code", esc.NO_GRADIENT, CTX, ANSWER)
    monkeypatch.setenv(decisions.ENV_ENABLED, "0")

    assert decisions.recall_decisions("code", esc.NO_GRADIENT) == []
    assert "prior_decisions" not in esc.payload(
        esc.NO_GRADIENT, unit="u1", kind="code"
    )["evidence"]
    # Bloco: mesmo switch para os caminhos de exame/screening.
    monkeypatch.delenv(decisions.ENV_ENABLED)
    with decisions.disabled():
        assert decisions.recall_decisions("code", esc.NO_GRADIENT) == []
    assert decisions.recall_decisions("code", esc.NO_GRADIENT) != []
