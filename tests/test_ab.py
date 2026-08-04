"""A/B rodado pelo harness: ordem alternada, veredito da régua, ledger cheio."""

from pathlib import Path

import pytest

from harness import cli
from harness.ab import ArmSpec, run_ab
from harness.backends import registry
from harness.backends.mock import MockBackend
from harness.ledger import store
from harness.types import Capabilities, ExecResult, Preflight, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "echo"


class NoopBackend:
    """Diz que terminou e não escreve nada: o verify da unit `echo` reprova.

    É o braço perdedor determinístico — falha pela régua, não pelo executor,
    que é exatamente o que o A/B mede.
    """

    name = "noop"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset(),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="noop sempre disponível")

    def execute(self, req) -> ExecResult:
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(),
            session_id=req.session_id,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def arena(data_dir):
    """`win` (sempre passa) e `lose` (sempre falha) registrados no registry.

    Devolve a lista de execuções na ordem em que aconteceram — é como o teste
    de alternância vê a ordem sem depender de timestamp.
    """
    calls: list[str] = []

    def spy(name, cls):
        def factory():
            backend = cls()
            inner = backend.execute

            def traced(req):
                calls.append(name)
                return inner(req)

            backend.execute = traced
            return backend

        registry.register(name, factory)

    spy("win", MockBackend)
    spy("lose", NoopBackend)
    try:
        yield calls
    finally:
        registry.unregister("win")
        registry.unregister("lose")


def test_run_ab_alternates(arena):
    run_ab(FIXTURE, ArmSpec("win"), ArmSpec("lose"), n=3)
    assert arena == ["win", "lose", "win", "lose", "win", "lose"]


@pytest.mark.parametrize(
    "a, b, verdict",
    [
        ("win", "lose", "DISCARD"),  # baseline ganha: candidata não entra
        ("lose", "win", "KEEP"),
    ],
)
def test_run_ab_verdicts(arena, a, b, verdict):
    report = run_ab(FIXTURE, ArmSpec(a), ArmSpec(b), n=6)

    assert report.verdict == verdict
    esperado = {"win": (6, 6), "lose": (0, 6)}
    assert (report.arm_a.succ, report.arm_a.n) == esperado[a]
    assert (report.arm_b.succ, report.arm_b.n) == esperado[b]
    assert report.sec_total > 0


def test_ab_grava_ledger(arena):
    report = run_ab(FIXTURE, ArmSpec("win", model="m1"), ArmSpec("lose", model="m2"), n=2)

    rows = store.history()  # mais recente primeiro: b2, a2, b1, a1
    assert len(rows) == 4
    assert [(r.backend, r.model) for r in rows] == [("lose", "m2"), ("win", "m1")] * 2
    assert all(r.kind == "code" for r in rows)  # chave do prior do router
    assert {r.run_id for r in report.rows_a} == {r.run_id for r in rows if r.ok}
    assert {r.run_id for r in report.rows_b} == {r.run_id for r in rows if not r.ok}


def test_ab_grava_no_data_dir_pedido(arena, tmp_path):
    outro = tmp_path / "outro"
    run_ab(FIXTURE, ArmSpec("win"), ArmSpec("lose"), n=1, data_dir=outro)

    assert len(store.history(path=outro / store.DB_NAME)) == 2
    assert not (tmp_path / "data" / store.DB_NAME).exists()  # o env não foi usado


def test_run_ab_aceita_selection_do_router(arena):
    sel = Selection(backend="win", model="m", tier="tier0", kind="code", max_turns=3)
    report = run_ab(FIXTURE, sel, ArmSpec("lose"), n=1)

    assert report.rows_a[0].tier == "tier0"  # sem tier a linha não vira prior
    assert report.rows_a[0].model == "m"


def test_run_ab_recusa_n_nao_positivo(arena):
    with pytest.raises(ValueError, match="positivo"):
        run_ab(FIXTURE, ArmSpec("win"), ArmSpec("lose"), n=0)
    assert arena == []


def test_cli_ab_dim_backend(data_dir, capsys):
    rc = cli.main(
        [
            "ab",
            "--dim",
            "backend",
            "--unit",
            str(FIXTURE),
            "--a-backend",
            "mock",
            "--b-backend",
            "mock",
            "--n",
            "2",
        ]
    )
    assert rc == 0

    linhas = capsys.readouterr().out.strip().splitlines()
    assert len(linhas) == 5  # 2 runs por braço + veredito
    assert linhas[0].startswith("a1 mock ok done")
    assert linhas[1].startswith("b1 mock ok done")
    assert linhas[-1].startswith("INCONCLUSIVE a=2/2 [")  # n=2 < min_n: não opina
    assert "b=2/2 [" in linhas[-1]
    assert len(store.history()) == 4


def test_cli_ab_dim_backend_veredito_com_braco_perdedor(arena, capsys):
    assert (
        cli.main(
            [
                "ab",
                "--dim",
                "backend",
                "--unit",
                str(FIXTURE),
                "--a-backend",
                "win",
                "--b-backend",
                "lose",
                "--n",
                "6",
            ]
        )
        == 0
    )
    assert (
        capsys.readouterr()
        .out.strip()
        .splitlines()[-1]
        .startswith("DISCARD a=6/6 [0.61,1.00] b=0/6 [0.00,0.39]")
    )


def test_cli_ab_estatistico_continua(data_dir, capsys):
    assert cli.main(["ab", "--a", "1/6", "--b", "6/6"]) == 0

    out = capsys.readouterr().out.strip()
    assert out.startswith("KEEP") and "a=1/6" in out and "b=6/6" in out
    assert store.history() == []  # modo estatístico não roda nada, não grava nada


@pytest.mark.parametrize(
    "argv",
    [
        ["ab"],  # nenhum modo
        ["ab", "--a", "1/6"],  # metade do estatístico
        ["ab", "--dim", "backend", "--a-backend", "mock", "--b-backend", "mock"],  # sem --unit
        [
            "ab",
            "--dim",
            "backend",
            "--unit",
            str(FIXTURE),
            "--a-backend",
            "mock",
        ],  # sem --b-backend
        [
            "ab",
            "--dim",
            "backend",
            "--unit",
            str(FIXTURE),
            "--a-backend",
            "mock",
            "--b-backend",
            "mock",
            "--a",
            "1/6",
        ],  # modos misturados
        [
            "ab",
            "--dim",
            "backend",
            "--unit",
            str(FIXTURE),
            "--a-backend",
            "mock",
            "--b-backend",
            "mock",
            "--n",
            "0",
        ],  # n sem sentido
    ],
)
def test_cli_ab_recusa_combinacao_invalida(data_dir, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
