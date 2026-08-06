"""`harness do --max-usd`: o gate de CLI, antes de qualquer coisa disparar.
Semântica do teto em si (fail-closed pré-dispatch) é `test_ceiling.py`; aqui só
o contorno da CLI — flag inválida sai 2 sem tocar em repo nem grafo."""

import pytest

from harness import cli


@pytest.fixture
def pasta_vazia(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_max_usd_zero_sai_2_sem_dispatch(pasta_vazia, capsys):
    rc = cli.main(["do", "faz algo", "--max-usd", "0"])

    assert rc == 2
    assert "--max-usd tem que ser maior que zero" in capsys.readouterr().err
    # Recusado antes de tocar o cwd: nenhum repo git foi criado pra unidade.
    assert not (pasta_vazia / ".git").exists()


def test_max_usd_negativo_sai_2_sem_dispatch(pasta_vazia, capsys):
    rc = cli.main(["do", "faz algo", "--max-usd", "-1"])

    assert rc == 2
    assert "--max-usd tem que ser maior que zero" in capsys.readouterr().err
    assert not (pasta_vazia / ".git").exists()


def test_max_usd_positivo_passa_do_gate(pasta_vazia, capsys):
    """Controle negativo: um valor válido não é recusado por este check (o
    resto do comando pode falhar por outro motivo — mock não instalado,
    verify sem régua — mas não por este print/exit 2)."""
    rc = cli.main(["do", "faz algo", "--max-usd", "1", "--dry-run"])

    assert rc == 0
    assert "--max-usd tem que ser maior que zero" not in capsys.readouterr().err
