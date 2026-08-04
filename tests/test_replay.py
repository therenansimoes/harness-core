"""Replay/atribuição sobre um ledger sintético — zero backend, zero modelo.

O ledger é escrito à mão porque o que está sob teste é a LEITURA dele: como as
janelas são cortadas, o que fica de fora, e o que a atribuição admite não
conseguir separar. Rodar um A/B de verdade só encareceria a mesma asserção.

Linha do tempo de todos os testes (minutos do mesmo dia, UTC):

    12:00–12:05  janela ANTES        6 runs, 2 ok
    12:10        mutação aplicada
    12:10–12:21  o experimento       12 runs (arms 1/6 e 4/6) — fora das janelas
    12:30–12:35  janela DEPOIS       6 runs, 5 ok
"""

from pathlib import Path

import pytest

from harness.improve.replay import (
    Attribution,
    ReplayError,
    arm_n,
    attribute,
    replay,
    signature,
    split,
)
from harness.ledger import store
from harness.ruler.wilson import wilson_interval
from harness.types import MutationRow, RunRow

MID = "aaaaaaaaaaaa"
RULE = "floor_up"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def db(data_dir: Path) -> Path:
    return data_dir / store.DB_NAME


def ts(minute: int) -> str:
    return f"2026-08-02T12:{minute:02d}:00+00:00"


def run(minute: int, ok: bool, **over) -> RunRow:
    base = dict(
        run_id=f"r{minute}",
        unit_id="echo",
        project=None,
        backend="mock",
        model=None,
        tier="t0",
        kind="code",
        ok=ok,
        exit_reason="done" if ok else "verify_failed",
        sec_total=1.0,
        sec_provision=0.0,
        cost_usd=0.0,
        intervention=False,
        created_at=ts(minute),
    )
    base.update(over)
    return RunRow(**base)


def mutation(**over) -> MutationRow:
    base = dict(
        mutation_id=MID,
        rule_id=RULE,
        verdict="KEEP",
        arm_a="1/6",
        arm_b="4/6",
        applied_at=ts(10),
        reverted=False,
        note=None,
    )
    base.update(over)
    return MutationRow(**base)


def seed(data_dir: Path, *, after_ok: int = 5, **over) -> MutationRow:
    """Escreve a linha do tempo do docstring e devolve a mutação gravada."""
    path = db(data_dir)
    for minute in range(0, 6):  # antes: 2 de 6
        store.record_run(run(minute, minute < 2), path=path)
    for minute in range(10, 22):  # o experimento: 12 runs
        store.record_run(run(minute, minute % 3 == 0), path=path)
    for i, minute in enumerate(range(30, 36)):  # depois: 5 de 6
        store.record_run(run(minute, i < after_ok), path=path)
    row = mutation(**over)
    store.record_mutation(row, path=path)
    return row


# --- janelas ---------------------------------------------------------------------


def test_replay_delta_com_ic(data_dir):
    """Antes 2/6, depois 5/6: delta +0.50, cada janela com o IC dela."""
    seed(data_dir)

    att = replay(MID)

    assert (att.succ_before, att.n_before) == (2, 6)
    assert (att.succ_after, att.n_after) == (5, 6)
    assert att.delta == pytest.approx(5 / 6 - 2 / 6)
    assert att.ci_before == wilson_interval(2, 6)
    assert att.ci_after == wilson_interval(5, 6)
    # 6 contra 6 não separa nada: o ponto é o delta, a leitura é o IC.
    assert att.separated is False
    assert (att.rule_id, att.verdict, att.reverted) == (RULE, "KEEP", False)


def test_amostra_do_experimento_fica_fora_das_janelas(data_dir):
    """As 12 runs do A/B não são "depois": metade rodou com a mutação DESLIGADA.

    Sem o corte, a janela de depois teria 18 runs e mediria uma média de
    com-e-sem a mutação, com cara de medida limpa.
    """
    seed(data_dir)

    att = replay(MID)

    assert att.n_experiment == 12
    assert att.n_after == 6


def test_janela_filtra_pela_chave_do_experimento(data_dir):
    """Run de outro kind não entra: seria delta de mistura de trabalho."""
    seed(data_dir)
    store.record_run(run(33, False, run_id="outro", kind="content"), path=db(data_dir))
    store.record_run(run(3, False, run_id="outro2", backend="knob"), path=db(data_dir))

    att = replay(MID)

    assert att.key == ("code", "t0", "mock")
    assert (att.n_before, att.n_after) == (6, 6)


def test_sem_janela_depois_o_delta_e_none(data_dir):
    """Mutação recém-aplicada: zero de zero não é 0.0, é ausência de amostra."""
    seed(data_dir, applied_at=ts(40))

    att = replay(MID)

    # tudo que existe é passado: as 24 runs caíram antes do `applied_at`
    assert (att.n_before, att.n_after) == (24, 0)
    assert att.delta is None
    assert att.ci_after == (0.0, 1.0)  # a ignorância inteira


def test_mutacao_desconhecida(data_dir):
    seed(data_dir)

    with pytest.raises(ReplayError, match="desconhecida"):
        replay("nao-existe")


# --- confounders -----------------------------------------------------------------


def test_confounder_e_a_segunda_keep_no_meio(data_dir):
    """Outra KEEP entre as janelas está DENTRO do delta e não dá para separar."""
    seed(data_dir)
    store.record_mutation(
        MutationRow("bbbbbbbbbbbb", "turns_up", "KEEP", "2/6", "5/6", ts(25), False),
        path=db(data_dir),
    )

    att = replay(MID)

    assert [(c.mutation_id, c.rule_id) for c in att.confounders] == [("bbbbbbbbbbbb", "turns_up")]


def test_nao_e_confounder_o_que_nao_sobreviveu_nem_o_que_ficou_fora(data_dir):
    """DISCARD voltou byte a byte; KEEP de ontem é baseline dos dois lados;
    KEEP de depois da última run não tocou em janela nenhuma."""
    seed(data_dir)
    for mid, verdict, at in (
        ("cccccccccccc", "DISCARD", ts(25)),
        ("dddddddddddd", "INCONCLUSIVE", ts(25)),
        ("eeeeeeeeeeee", "KEEP", "2026-08-01T09:00:00+00:00"),
        ("ffffffffffff", "KEEP", ts(59)),
    ):
        store.record_mutation(
            MutationRow(mid, "outra", verdict, "3/6", "3/6", at, verdict != "KEEP"),
            path=db(data_dir),
        )

    assert replay(MID).confounders == ()


# --- peças puras -----------------------------------------------------------------


def test_arm_n_de_lixo_e_zero():
    """Contagem que não parseia não pode excluir run nenhuma da janela."""
    assert (arm_n("4/6"), arm_n("0/0"), arm_n(""), arm_n("a/b")) == (6, 0, 0, 0)


def test_signature_sem_unanimidade_nao_tem_chave():
    rows = [run(0, True), run(1, True, tier="t1")]

    assert signature(rows) == ("code", None, "mock")
    assert signature([]) == (None, None, None)


def test_split_ordena_por_created_at_e_nao_pela_ordem_de_insercao():
    """O ledger é lido em qualquer ordem; a linha do tempo é o `created_at`."""
    rows = [run(31, True), run(2, False), run(1, True)]

    span = split(mutation(arm_a="0/0", arm_b="0/0"), rows)

    assert [r.run_id for r in span.before] == ["r1", "r2"]
    assert [r.run_id for r in span.after] == ["r31"]


def test_attribute_recusa_mutacao_de_outro_id():
    with pytest.raises(ReplayError, match="não é"):
        attribute("outro", [], [], mutation=mutation())


def test_attribute_sem_mutacao_e_so_a_conta():
    """Sem a linha do ledger não há rótulo — e a conta continua de pé."""
    att = attribute(MID, [run(0, True), run(1, False)], [run(30, True)])

    assert isinstance(att, Attribution)
    assert (att.rule_id, att.verdict) == ("", "")
    assert att.delta == pytest.approx(0.5)


# --- CLI ---------------------------------------------------------------------------


def test_cli_replay_imprime_delta_ic_e_confounders(data_dir, capsys):
    from harness import cli

    seed(data_dir)
    store.record_mutation(
        MutationRow("bbbbbbbbbbbb", "turns_up", "KEEP", "2/6", "5/6", ts(25), False),
        path=db(data_dir),
    )

    assert cli.main(["replay", "--mutation", MID]) == 0

    linhas = capsys.readouterr().out.strip().splitlines()
    assert linhas[0] == (f"mut {MID} {RULE} KEEP mantida exp=12 kind=code tier=t0 backend=mock")
    assert linhas[1] == (
        "antes 2/6 [0.10,0.70] depois 5/6 [0.44,0.97] delta=+0.50 intervalos=sobrepostos"
    )
    assert linhas[2] == f"confounders=1 bbbbbbbbbbbb:turns_up@{ts(25)}"


def test_cli_replay_list(data_dir, capsys):
    from harness import cli

    seed(data_dir)

    assert cli.main(["replay", "--list"]) == 0

    out = capsys.readouterr().out
    assert f"{MID} {ts(10)} {RULE} KEEP a=1/6 b=4/6 mantida" in out
    assert "mutações=1" in out


def test_cli_replay_limit_vale_no_mutation_tambem(data_dir, capsys):
    """A flag chega no `replay()`. Ignorá-la ali fazia o `--list --limit N`
    mostrar id que o `--mutation --limit N` jurava não existir: teto de 200 na
    lista, 2000 na atribuição, e o help anunciando um número que não valia."""
    from harness import cli

    seed(data_dir)
    store.record_mutation(
        mutation(mutation_id="bbbbbbbbbbbb", applied_at=ts(25)), path=db(data_dir)
    )

    assert cli.main(["replay", "--mutation", MID, "--limit", "1"]) == 1
    assert "desconhecida" in capsys.readouterr().err
    assert cli.main(["replay", "--mutation", MID, "--limit", "2"]) == 0
    assert capsys.readouterr().out.startswith(f"mut {MID} ")


def test_cli_replay_list_avisa_que_truncou(data_dir, capsys):
    """`mutações=1` calado seria lido como "o ledger tem uma"."""
    from harness import cli

    seed(data_dir)
    store.record_mutation(
        mutation(mutation_id="bbbbbbbbbbbb", applied_at=ts(25)), path=db(data_dir)
    )

    assert cli.main(["replay", "--list", "--limit", "1"]) == 0

    assert "mutações=1 (teto do --limit; pode haver mais)" in capsys.readouterr().out


def test_cli_replay_id_inexistente_sai_1(data_dir, capsys):
    from harness import cli

    seed(data_dir)

    assert cli.main(["replay", "--mutation", "zzz"]) == 1
    assert "desconhecida" in capsys.readouterr().err


def test_cli_replay_sem_alvo_e_erro_de_uso(data_dir, capsys):
    from harness import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["replay"])

    assert exc.value.code == 2
    assert "--mutation" in capsys.readouterr().err
