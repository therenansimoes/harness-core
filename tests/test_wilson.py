import math

import pytest

from harness.ruler.wilson import Arm, decide_ab, wilson_interval


def _legacy_wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Fórmula do harness antigo, reescrita aqui como oráculo do porte."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - half), min(1.0, center + half)


# Valores conferidos à mão contra a fórmula do legado (z=1.96).
@pytest.mark.parametrize(
    "succ, n, lo, hi",
    [
        (6, 6, 0.6096569663469354, 0.9999999999999999),
        (5, 6, 0.43649056343635395, 0.9699474141282697),
        (0, 6, 0.0, 0.3903430336530645),
        (3, 10, 0.10778928748621183, 0.6032267800204347),
        (12, 20, 0.3865779423152061, 0.7811960325858074),
    ],
)
def test_valores_identicos_ao_legado(succ, n, lo, hi):
    assert wilson_interval(succ, n) == (lo, hi)


@pytest.mark.parametrize("n", range(0, 13))
def test_bate_com_o_oraculo_em_toda_a_grade(n):
    for succ in range(0, n + 1):
        assert wilson_interval(succ, n) == _legacy_wilson(succ, n)


def test_sem_amostra_e_ignorancia_inteira():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert wilson_interval(3, -1) == (0.0, 1.0)


def test_nao_degenera_com_p_colado_em_um():
    lo, hi = wilson_interval(6, 6)
    assert lo < 0.62 and hi > 0.99  # Wald daria (1.0, 1.0)


def test_z_maior_alarga_o_intervalo():
    lo_95, hi_95 = wilson_interval(5, 10)
    lo_99, hi_99 = wilson_interval(5, 10, z=2.576)
    assert lo_99 < lo_95 and hi_99 > hi_95


def test_ab_inconclusive_por_n_insuficiente():
    # B perfeito, A zerado: sem min_n a régua diria KEEP.
    assert decide_ab(Arm(0, 5), Arm(5, 5)) == "INCONCLUSIVE"
    assert decide_ab(Arm(0, 6), Arm(5, 5)) == "INCONCLUSIVE"
    assert decide_ab(Arm(0, 5), Arm(6, 6), min_n=5) == "KEEP"


def test_ab_keep_quando_b_e_melhor_e_nao_sobrepoe():
    assert decide_ab(Arm(0, 10), Arm(10, 10)) == "KEEP"


def test_ab_discard_quando_a_e_melhor_e_nao_sobrepoe():
    assert decide_ab(Arm(10, 10), Arm(0, 10)) == "DISCARD"


def test_ab_inconclusive_quando_intervalos_sobrepoem():
    assert decide_ab(Arm(5, 10), Arm(6, 10)) == "INCONCLUSIVE"
    assert decide_ab(Arm(6, 6), Arm(6, 6)) == "INCONCLUSIVE"
