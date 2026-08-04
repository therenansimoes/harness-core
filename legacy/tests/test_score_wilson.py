#!/usr/bin/env python3
"""Testa a régua de Wilson do score.py (D2) — decisão ternária sobre success.

Sem API, sem results.tsv: `decide_ab` é aritmética pura sobre (sucessos, N).
O que estes testes travam é o comportamento que o juiz-LLM tinha e errava —
declarar vencedor onde só existe ruído.

    python3 -m pytest tests/test_score_wilson.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import score  # noqa: E402

# ---------------------------------------------------------------- intervalo


def test_wilson_nao_degenera_em_p_extremo():
    """0/6 e 6/6 têm intervalo de largura real — Wald daria [0,0] e [1,1]."""
    lo0, hi0 = score.wilson_interval(0, 6)
    lo1, hi1 = score.wilson_interval(6, 6)
    assert lo0 == 0.0 and 0.3 < hi0 < 0.5
    assert hi1 == pytest.approx(1.0) and 0.5 < lo1 < 0.7


def test_wilson_encolhe_com_mais_amostra():
    largo = score.wilson_interval(3, 6)
    estreito = score.wilson_interval(30, 60)
    assert (estreito[1] - estreito[0]) < (largo[1] - largo[0])
    for lo, hi in (largo, estreito):
        assert 0.0 <= lo <= hi <= 1.0


def test_wilson_n_zero_nao_explode():
    assert score.wilson_interval(0, 0) == (0.0, 1.0)


# ------------------------------------------------------------------ veredito


@pytest.mark.parametrize(
    "succ_a, n_a, succ_b, n_b, verdict",
    [
        # ruído puro: 1 sucesso de diferença em 6 runs não distingue nada
        (4, 6, 5, 6, score.INCONCLUSIVE),
        # separação limpa: B ganha de A com folga
        (1, 6, 6, 6, score.KEEP),
        # N abaixo do mínimo: nem 3/3 vs 0/3 vira veredito
        (0, 3, 3, 3, score.INCONCLUSIVE),
        # sobreposição parcial: os intervalos se tocam, logo não decide
        (2, 8, 6, 8, score.INCONCLUSIVE),
        # espelho do KEEP: A ganha, candidata morre
        (6, 6, 1, 6, score.DISCARD),
    ],
)
def test_decide_ab_ternario(succ_a, n_a, succ_b, n_b, verdict):
    assert score.decide_ab(succ_a, n_a, succ_b, n_b)["verdict"] == verdict


def test_n_insuficiente_reporta_o_motivo_certo():
    d = score.decide_ab(0, 3, 3, 3)
    assert "N insuficiente" in d["reason"]
    assert d["min_n"] == score.MIN_N == 6


def test_sobreposicao_parcial_reporta_os_dois_intervalos():
    d = score.decide_ab(2, 8, 6, 8)
    lo_a, hi_a = d["ci_a"]
    lo_b, hi_b = d["ci_b"]
    assert lo_b < hi_a and lo_a < hi_b  # de fato se sobrepõem
    assert "sobrepõem" in d["reason"]


def test_empate_perfeito_nunca_promove():
    """6/6 vs 6/6: intervalos idênticos. Empate não é promoção."""
    assert score.decide_ab(6, 6, 6, 6)["verdict"] == score.INCONCLUSIVE
