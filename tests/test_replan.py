"""`harness replan`: triagem da unidade travada (blocker + nota) e o repique.

O roteamento é puro (`decide`) e testado direto; o caminho do repique passa pelo
`propose_decompose` real com o backend mockado, porque o que importa nele é o
NOME dos sub-passos — sufixo alfabético que ordena entre `03_` e `04_`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.improve import decompose as dec
from harness.improve import replan as rp
from harness.ledger import store
from harness.types import RunRow

PLANO = [
    {
        "id_slug": "cria-css",
        "prompt_md": "# a\n\nCrie `style.css` com uma variável.",
        "verify_cmd": "test -f style.css || { echo falta; exit 1; }",
        "kind": "code",
        "files": ["style.css"],
        "deps": [],
        "checks": [{"name": "existe", "cmd": "test -s style.css", "weight": 1.0}],
    },
    {
        "id_slug": "usa-css",
        "prompt_md": "# b\n\nLigue `style.css` no `index.html`.",
        "verify_cmd": "grep -q style.css index.html || { echo falta; exit 1; }",
        "kind": "code",
        "files": ["index.html"],
        "deps": ["cria-css"],
        "checks": [{"name": "linkado", "cmd": "grep -q link index.html", "weight": 1.0}],
    },
]

UNIT_TOML = """\
id = "03_tema-escuro"
kind = "code"
project = "oficina"
prompt = "Deixe o site com tema escuro: CSS e o botao."
verify_cmd = "test -f style.css || { echo falta; exit 1; }"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# site\n", encoding="utf-8")
    (repo / "index.html").write_text("<h1>oi</h1>\n", encoding="utf-8")
    projects_file = tmp_path / "projects.toml"
    projects_file.write_text(f'[projects.oficina]\nrepo = "{repo}"\n')
    queue = tmp_path / "queue"
    travada = queue / "stuck" / "03_tema-escuro"
    travada.mkdir(parents=True)
    (travada / "unit.toml").write_text(UNIT_TOML, encoding="utf-8")

    def fake_call(prompt, backend, model, max_usd, adapter=None) -> str:
        return json.dumps(PLANO, ensure_ascii=False)

    monkeypatch.setattr(dec, "_call_planner", fake_call)

    class Env:
        pass

    e = Env()
    e.repo, e.projects_file, e.queue = repo, projects_file, queue
    e.db = tmp_path / "data" / store.DB_NAME
    return e


def _run(unit_id: str, run_id: str, scores: list[float], db: Path, blocker=None) -> None:
    store.record_run(
        RunRow(
            run_id=run_id,
            unit_id=unit_id,
            project="oficina",
            backend="mock",
            model=None,
            tier=None,
            kind="code",
            ok=False,
            exit_reason="blocker" if blocker else "verify_failed",
            sec_total=1.0,
            sec_provision=0.1,
            cost_usd=0.0,
            intervention=False,
            created_at=store.now_iso(),
        ),
        db,
    )
    for attempt, score in enumerate(scores):
        store.record_node(run_id, "verify", {"score": score}, db, attempt=attempt)
        store.record_node(
            run_id,
            "execute",
            {"ok": False, "exit_reason": "blocker", "blocker": blocker},
            db,
            attempt=attempt,
        )


# --- roteamento ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "scores", "rota"),
    [
        ("needs_user_input", [0.1, 0.1], rp.ROUTE_USER),  # nota baixa NÃO vence o humano
        ("external_wait", [0.1, 0.1], rp.ROUTE_WAIT),  # o plano está certo, o mundo não
        ("missing_evidence", [0.1, 0.2], rp.ROUTE_SPLIT),
        ("goal_not_met_yet", [0.1, 0.2, 0.0], rp.ROUTE_SPLIT),
        ("goal_not_met_yet", [0.6, 0.7], rp.ROUTE_RETRY),  # dentro da zona: ainda informa
        ("missing_evidence", [0.1], rp.ROUTE_RETRY),  # uma tentativa é ruído
    ],
)
def test_roteia_por_tipo_de_blocker_e_nota(tipo, scores, rota):
    assert rp.decide((tipo, "detalhe"), scores).route == rota


def test_sem_blocker_declarado_usa_so_a_nota():
    assert rp.decide(None, [0.1, 0.2]).route == rp.ROUTE_SPLIT
    assert rp.decide(None, []).route == rp.ROUTE_RETRY


# --- sufixo alfabético --------------------------------------------------------


def test_sufixo_alfabetico_ordena_entre_o_passo_e_o_proximo():
    names = rp.split_names("03_tema-escuro", ["cria-css", "usa-css"])

    assert names == ["03a_cria-css", "03b_usa-css"]
    assert sorted(["04_outro", "03_tema-escuro", *names]) == [
        "03_tema-escuro",
        "03a_cria-css",
        "03b_usa-css",
        "04_outro",
    ]


def test_sufixo_sem_prefixo_numerico_ainda_ordena_depois():
    names = rp.split_names("u4a_botao", ["um", "dois"])

    assert names == ["u4a_botao-a_um", "u4a_botao-b_dois"]
    assert sorted(["u4a_botao", *names])[0] == "u4a_botao"


# --- repique ------------------------------------------------------------------


def test_repique_grava_os_sub_passos_com_sufixo(env, capsys):
    _run("03_tema-escuro", "r1", [0.1, 0.2], env.db, blocker="goal_not_met_yet")

    rc = rp.replan(
        "oficina",
        "03_tema-escuro",
        queue_dir=env.queue,
        projects_file=env.projects_file,
        db=env.db,
    )

    assert rc == 0
    assert sorted(p.name for p in env.queue.iterdir() if p.is_dir() and p.name != "stuck") == [
        "03a_cria-css",
        "03b_usa-css",
    ]
    # a original continua em stuck/ — repique não mexe no que já foi julgado
    assert (env.queue / "stuck" / "03_tema-escuro" / "unit.toml").is_file()
    from harness.cli import load_unit

    sub = load_unit(env.queue / "03b_usa-css")
    assert sub.id == "03b_usa-css"
    assert sub.deps == ("03a_cria-css",)  # dep aponta para o NOME novo


def test_needs_user_input_nao_repica(env, capsys):
    _run("03_tema-escuro", "r1", [0.1, 0.1], env.db, blocker="needs_user_input")

    rc = rp.replan(
        "oficina",
        "03_tema-escuro",
        queue_dir=env.queue,
        projects_file=env.projects_file,
        db=env.db,
    )

    assert rc == 0
    assert [p.name for p in env.queue.iterdir()] == ["stuck"]
    assert "é sua vez" in capsys.readouterr().out


def test_duas_travadas_na_mesma_fila_mandam_escalar(env, capsys):
    outra = env.queue / "stuck" / "04_outro"
    outra.mkdir(parents=True)
    (outra / "unit.toml").write_text(UNIT_TOML.replace("03_tema-escuro", "04_outro"))
    _run("03_tema-escuro", "r1", [0.1, 0.2], env.db, blocker="goal_not_met_yet")

    rc = rp.replan(
        "oficina",
        "03_tema-escuro",
        queue_dir=env.queue,
        projects_file=env.projects_file,
        db=env.db,
    )

    assert rc == 1
    assert [p.name for p in env.queue.iterdir()] == ["stuck"]  # nada gravado
    assert "escale" in capsys.readouterr().err


def test_repique_reprovado_no_gate_manda_escalar(env, capsys, monkeypatch):
    """Sub-passo cuja régua já passa no HEAD: o gate reprova as duas vezes."""
    verde = [dict(PLANO[0], verify_cmd="test -f README.md || { echo falta; exit 1; }"), PLANO[1]]
    monkeypatch.setattr(
        dec, "_call_planner", lambda *a, **kw: json.dumps(verde, ensure_ascii=False)
    )
    _run("03_tema-escuro", "r1", [0.1, 0.2], env.db, blocker="missing_evidence")

    rc = rp.replan(
        "oficina",
        "03_tema-escuro",
        queue_dir=env.queue,
        projects_file=env.projects_file,
        db=env.db,
    )

    assert rc == 1
    assert [p.name for p in env.queue.iterdir()] == ["stuck"]
    assert "escale" in capsys.readouterr().err


def test_unidade_inexistente_e_erro(env, capsys):
    rc = rp.replan(
        "oficina", "99_nada", queue_dir=env.queue, projects_file=env.projects_file, db=env.db
    )

    assert rc == 1
    assert "não está em" in capsys.readouterr().err
