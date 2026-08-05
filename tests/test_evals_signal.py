"""Sinal discriminante da régua: split treino/holdout, eixo `verify` e recusa.

Tudo unitário e determinístico — o split é função pura do id (sha256 mod 4),
o `verify` roda comando de verdade num diretório efêmero e a recusa é regex.
"""

from __future__ import annotations

import subprocess

import pytest

from harness.evals import score
from harness.evals.bundle import EvalCase, case_bucket, split_cases
from harness.evals.score import score_trial


def _case(cid: str, **kw) -> EvalCase:
    return EvalCase(id=cid, kind="code_fix", prompt="p", **kw)


# ------------------------------------------------------------------- split


def test_split_deterministico_e_estavel():
    cases = [_case(f"s-{n}") for n in range(1, 10)]

    a = split_cases(cases)
    b = split_cases(cases)

    assert a == b
    # A ordem original do bundle é preservada nos dois lados.
    ordem = [c.id for c in cases]
    for lado in a:
        ids = [c.id for c in lado]
        assert ids == sorted(ids, key=ordem.index)


def test_split_um_caso_sem_holdout():
    caso = _case("s-1")

    assert split_cases([caso]) == ([caso], [])


def test_split_dois_lados_nunca_vazios():
    # Ids achados por força bruta: um conjunto todo no balde 0, outro sem
    # nenhum no balde 0 — os extremos que exigiriam o fix-up.
    todos_zero = [f"c-{n}" for n in range(400) if case_bucket(f"c-{n}") == 0][:3]
    nenhum_zero = [f"c-{n}" for n in range(400) if case_bucket(f"c-{n}") != 0][:3]
    assert len(todos_zero) == 3 and len(nenhum_zero) == 3

    for ids in (todos_zero, nenhum_zero):
        train, holdout = split_cases([_case(i) for i in ids])
        assert train and holdout
        assert len(train) + len(holdout) == 3


def test_split_segue_o_modulo():
    cases = [_case(f"s-{n}") for n in range(1, 10)]

    train, holdout = split_cases(cases)

    assert {c.id for c in holdout} == {c.id for c in cases if case_bucket(c.id) == 0}
    assert {c.id for c in holdout} == {"s-1", "s-5"}
    assert {c.id for c in train} == {f"s-{n}" for n in (2, 3, 4, 6, 7, 8, 9)}


# ------------------------------------------------------------------- verify


def test_verify_axis_pass_fail():
    score._VERIFY_CACHE.clear()
    caso = _case("v-1", verify_cmd="grep -qi pytest {output}")

    # `verify` é auto-incluído quando há comando, mesmo fora de DEFAULT_AXES.
    assert score_trial(caso, "# x\n- rode pytest\n").axes["verify"] is True
    assert score_trial(caso, "# x\n- rode unittest\n").axes["verify"] is False


def test_verify_sem_cmd_falha_fechado():
    caso = _case("v-2", axes=("verify",))

    with pytest.raises(ValueError, match=r"verify.*sem verify_cmd"):
        score_trial(caso, "# qualquer coisa\n- ok\n")


def test_verify_timeout_reprova(monkeypatch):
    score._VERIFY_CACHE.clear()
    monkeypatch.setattr(score, "VERIFY_TIMEOUT_S", 0.2)
    caso = _case("v-3", axes=("structure", "verify"), verify_cmd="sleep 5")

    assert score_trial(caso, "# x\n- ok\n").axes["verify"] is False


def test_verify_cacheia(monkeypatch):
    score._VERIFY_CACHE.clear()
    real_run = subprocess.run
    chamadas = []

    def contando(*args, **kwargs):
        chamadas.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(score.subprocess, "run", contando)
    caso = _case("v-4", verify_cmd="grep -qi ok {output}")

    r1 = score_trial(caso, "# x\n- ok\n", trial=0)
    r2 = score_trial(caso, "# x\n- ok\n", trial=1)

    # Mesmo (cmd, saída) => um subprocess só; o segundo trial sai do cache.
    assert r1.axes["verify"] is True and r2.axes["verify"] is True
    assert len(chamadas) == 1


# ------------------------------------------------------------------- recusa


def test_refusal_regex_conjugacoes():
    caso = _case("r-1", expect={"must_refuse": True}, axes=("safety",))

    assert score_trial(caso, "recuso").axes["safety"] is True
    assert score_trial(caso, "vou recusar essa mudança").axes["safety"] is True
    assert score_trial(caso, "claro, desabilitei os testes").axes["safety"] is False
