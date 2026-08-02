import pytest

from harness import cli


def _run(capsys, *argv):
    assert cli.main(list(argv)) == 0
    return capsys.readouterr().out.strip()


def test_ab_inconclusive_quando_os_intervalos_sobrepoem(capsys):
    out = _run(capsys, "ab", "--a", "5/6", "--b", "6/6")
    assert out.startswith("INCONCLUSIVE")
    assert "a=5/6" in out and "b=6/6" in out


def test_ab_keep_quando_b_e_melhor_sem_sobreposicao(capsys):
    assert _run(capsys, "ab", "--a", "1/10", "--b", "10/10").startswith("KEEP")


def test_ab_discard_quando_a_e_melhor_sem_sobreposicao(capsys):
    assert _run(capsys, "ab", "--a", "10/10", "--b", "1/10").startswith("DISCARD")


def test_ab_amostra_pequena_nao_opina(capsys):
    assert _run(capsys, "ab", "--a", "0/4", "--b", "4/4").startswith("INCONCLUSIVE")


def test_ab_min_n_e_configuravel(capsys):
    assert _run(capsys, "ab", "--a", "0/4", "--b", "4/4", "--min-n", "4").startswith("KEEP")


@pytest.mark.parametrize(
    "a, b, verdict",
    [
        ("4/6", "5/6", "INCONCLUSIVE"),   # ruído: 1 sucesso de diferença em 6
        ("1/6", "6/6", "KEEP"),           # separação limpa
        ("0/3", "3/3", "INCONCLUSIVE"),   # N abaixo do mínimo não vira veredito
    ],
)
def test_os_tres_casos_de_aceite_do_d2(capsys, a, b, verdict):
    assert _run(capsys, "ab", "--a", a, "--b", b).startswith(verdict)


def test_ab_imprime_o_intervalo_de_cada_braco(capsys):
    out = _run(capsys, "ab", "--a", "6/6", "--b", "6/6")
    assert out == "INCONCLUSIVE a=6/6 [0.61,1.00] b=6/6 [0.61,1.00]"


@pytest.mark.parametrize("bad", ["5", "a/6", "7/6", "5/0", "-1/6"])
def test_ab_recusa_braco_malformado(bad):
    with pytest.raises(SystemExit) as exc:
        cli.main(["ab", "--a", bad, "--b", "6/6"])
    assert exc.value.code == 2
