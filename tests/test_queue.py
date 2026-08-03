"""Driver da fila (`harness queue`): fila progressiva + buckets done/stuck."""

import json
import subprocess
from pathlib import Path

import pytest

from harness import queue as queue_mod
from harness.cli import build_parser
from harness.projects import QUEUE_DONE, QUEUE_STUCK, UNIT_FILE, default_branch
from harness.routing import CONFIG_DIR_ENV
from harness.ruler.gate import Decision


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _entrega(repo: Path, unit: str, arquivo: str, conteudo: str, base: str = "HEAD"):
    """Simula o que `deliver` deixa no repo: branch `harness/<unit>` com um commit
    feito num worktree, sem tocar a working tree principal."""
    ws = repo / "data" / f"ws-{unit}"
    _git(repo, "worktree", "add", "-q", "-B", f"harness/{unit}", str(ws), base)
    (ws / arquivo).write_text(conteudo, encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", f"harness: {unit}")
    _git(repo, "worktree", "remove", "--force", str(ws))


@pytest.fixture
def fila(tmp_path, monkeypatch) -> Path:
    """3 unidades numa fila fake + registro de projeto apontando pra ela.

    O repo é git de verdade (com um commit) porque o accept agora integra a
    entrega no branch default — fila em diretório qualquer não representa nada.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    _git(tmp_path, "init", "-q")
    (tmp_path / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    queue = tmp_path / "queue"
    for name in ("01-um", "02-dois", "03-tres"):
        d = queue / name
        d.mkdir(parents=True)
        (d / UNIT_FILE).write_text(f'id = "{name}"\n', encoding="utf-8")
    (cfg / "projects.toml").write_text(
        f'[projects.t]\nrepo = {json.dumps(str(tmp_path))}\n'
        f'queue_dir = {json.dumps(str(queue))}\n',
        encoding="utf-8",
    )
    return queue


def _fake_run_unit(acoes: dict[str, str], visto: list[str]):
    def run_unit(unit_dir, backend, model, data_dir, thread_id, max_attempts=None):
        visto.append(f"{unit_dir.name}:{thread_id}:{backend}:{model}:{max_attempts}")
        acao = acoes.get(unit_dir.name, "accept")
        return {"decision": Decision(action=acao, reason="fake")}

    return run_unit


def test_para_na_primeira_nao_aceita_e_move(fila, monkeypatch, capsys):
    visto: list[str] = []
    monkeypatch.setattr(
        queue_mod, "run_unit", _fake_run_unit({"02-dois": "escalate"}, visto)
    )

    assert queue_mod.run_queue("t", backend="mock", model="m", attempts=2) == 0

    # A terceira nunca rodou: fila progressiva para na que travou.
    assert [v.split(":")[0] for v in visto] == ["01-um", "02-dois"]
    assert (fila / QUEUE_DONE / "01-um" / UNIT_FILE).is_file()
    assert (fila / QUEUE_STUCK / "02-dois" / UNIT_FILE).is_file()
    assert (fila / "03-tres" / UNIT_FILE).is_file()
    # thread_id único por invocação, e os argumentos chegam inteiros no grafo.
    assert visto[0].endswith(":mock:m:2")
    threads = {v.split(":")[1] for v in visto}
    assert len(threads) == 2 and all(t.startswith("t-") for t in threads)
    assert "travou" in capsys.readouterr().out


def test_no_move_e_ensaio(fila, monkeypatch):
    visto: list[str] = []
    monkeypatch.setattr(
        queue_mod, "run_unit", _fake_run_unit({"02-dois": "escalate"}, visto)
    )

    assert queue_mod.run_queue("t", move=False) == 0

    # Ensaio: roda a fila toda e não cria bucket nenhum.
    assert len(visto) == 3
    assert not (fila / QUEUE_DONE).exists() and not (fila / QUEUE_STUCK).exists()


def test_cli_queue_defaults():
    args = build_parser().parse_args(["queue", "--project", "t", "--no-move"])
    assert (args.project, args.move, args.backend) == (
        "t", False, queue_mod.DEFAULT_BACKEND
    )
    assert args.deadline_s == queue_mod.DEFAULT_DEADLINE_S and args.attempts is None
    # Integração é o default: sem ela a fila progressiva não compõe.
    assert args.integrate is True
    assert build_parser().parse_args(["queue", "--no-integrate"]).integrate is False


def test_entregas_aceitas_compoem_no_branch_default(fila, monkeypatch, capsys):
    """O ponto da integração: a unidade 2 sai de um HEAD que já tem a 1, e no fim
    o branch default tem os arquivos das DUAS entregas (as branches ficam)."""
    repo = fila.parent
    visto: list[str] = []
    base = _fake_run_unit({"03-tres": "escalate"}, visto)

    def run_unit(unit_dir, *a, **kw):
        state = base(unit_dir, *a, **kw)
        if state["decision"].action == "accept":
            _entrega(repo, unit_dir.name, f"{unit_dir.name}.txt", unit_dir.name)
        return state

    monkeypatch.setattr(queue_mod, "run_unit", run_unit)

    assert queue_mod.run_queue("t") == 0

    assert (repo / "01-um.txt").read_text(encoding="utf-8") == "01-um"
    assert (repo / "02-dois.txt").read_text(encoding="utf-8") == "02-dois"
    branches = set(
        _git(repo, "branch", "--format=%(refname:short)").split()
    )
    assert {"harness/01-um", "harness/02-dois"} <= branches
    assert default_branch(repo) in branches
    assert (fila / QUEUE_DONE / "02-dois" / UNIT_FILE).is_file()
    # Cada entrega é um merge --no-ff próprio na história do branch default.
    log = _git(repo, "log", "--format=%s", default_branch(repo))
    assert "harness: integrate 01-um" in log and "harness: integrate 02-dois" in log
    assert "integrate: harness/02-dois ->" in capsys.readouterr().out


def test_conflito_de_merge_para_a_fila_e_aborta(fila, monkeypatch, capsys):
    """Conflito não é resolvido por ninguém aqui: merge abortado, repo limpo,
    unidade em stuck/ e a fila para (a próxima nem roda)."""
    repo = fila.parent
    visto: list[str] = []
    base = _fake_run_unit({}, visto)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    def run_unit(unit_dir, *a, **kw):
        state = base(unit_dir, *a, **kw)
        # As duas entregas mexem na mesma linha, ambas a partir do commit seed:
        # a segunda não tem como fazer fast-forward nem merge limpo.
        _entrega(repo, unit_dir.name, "app.txt", f"{unit_dir.name}\n", base=seed)
        return state

    monkeypatch.setattr(queue_mod, "run_unit", run_unit)

    assert queue_mod.run_queue("t") == 0

    assert [v.split(":")[0] for v in visto] == ["01-um", "02-dois"]
    assert (fila / QUEUE_DONE / "01-um" / UNIT_FILE).is_file()
    assert (fila / QUEUE_STUCK / "02-dois" / UNIT_FILE).is_file()
    assert (fila / "03-tres" / UNIT_FILE).is_file()
    # Merge abortado: nada em conflito na working tree e HEAD é o merge da 1ª.
    assert _git(repo, "status", "--porcelain", "-uno").strip() == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "log", "-1", "--format=%s").strip() == "harness: integrate 01-um"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "01-um\n"
    out = capsys.readouterr().out
    assert "integração falhou" in out and "abortado" in out
