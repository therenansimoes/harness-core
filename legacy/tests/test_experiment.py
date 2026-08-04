#!/usr/bin/env python3
"""Testa experiment.py (runner automático de A/B) sem gastar API/rede.

`subprocess.run` é o único ponto de saída para o mundo real dentro de
experiment.py (git worktree add/remove, setup.sh, run_task.py) — os testes
substituem esse ÚNICO ponto por um fake determinístico (FakeRun). Nenhum
teste aqui chama `claude`, agent.py ou faz uma run real.

    python3 -m pytest tests/test_experiment.py -q
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import experiment  # noqa: E402

EXP_TOML = """
name = "{name}"
task = "benchmarks/judge/task_j_b2b"
n_per_arm = {n}
budget_usd = {budget}

[mutation]
file = "agent.py"
old = "MAX_TURNS = 30"
new = "MAX_TURNS = 25"

[decision_rule]
min_diff_successes = {min_diff}
"""

OLD_AGENT_TEXT = "MAX_TURNS = 30\n"
NEW_MARKER = "MAX_TURNS = 25"


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _write_result_row(worktree: Path, success: int, cost: float) -> None:
    header = "\t".join(experiment.RESULTS_HEADER)
    row = "\t".join(
        str(v)
        for v in [
            "2026-08-02T00:00:00+00:00",
            "vtest",
            "cli",
            "m",
            "fixed",
            "task_j_b2b",
            success,
            "1.0",
            "100",
            f"{cost:.4f}",
            "1",
            "",
        ]
    )
    path = worktree / "results.tsv"
    if not path.exists():
        path.write_text(header + "\n")
    with path.open("a") as fh:
        fh.write(row + "\n")


class FakeRun:
    """Substitui subprocess.run inteiro dentro de experiment.py.

    - `git worktree add`: cria um diretório real (não um worktree git de
      verdade) com um agent.py sintético — o suficiente para apply_mutation
      operar em cima de arquivo real.
    - `git worktree remove`: apaga o diretório e registra a remoção.
    - `run_task.py <task>`: NUNCA chama agent/claude. Consome um item de
      `script` ([(success, cost), ...], na ordem A,B,A,B,...) e grava a linha
      em <worktree>/results.tsv, do jeito que run_task.py real grava.
    - `bash setup.sh`: no-op ok.
    """

    def __init__(self, script):
        self.script = list(script)
        self.created: list[Path] = []
        self.removed: list[Path] = []
        self.call_order: list[tuple] = []  # (arm, success) na ordem de chamada

    def __call__(self, cmd, cwd=None, check=False, capture_output=False, text=False, timeout=None):
        cwd = Path(cwd) if cwd else None
        if cmd[0] == "git" and cmd[1:3] == ["worktree", "add"]:
            dest = Path(cmd[4])
            dest.mkdir(parents=True)
            (dest / "agent.py").write_text(OLD_AGENT_TEXT)
            self.created.append(dest)
            return _ok()
        if cmd[0] == "git" and cmd[1:3] == ["worktree", "remove"]:
            dest = Path(cmd[-1])
            self.removed.append(dest)
            shutil.rmtree(dest, ignore_errors=True)
            return _ok()
        if len(cmd) >= 2 and str(cmd[1]).endswith("run_task.py"):
            worktree = cwd
            mutated = NEW_MARKER in (worktree / "agent.py").read_text()
            arm = "B" if mutated else "A"
            if not self.script:
                raise AssertionError("run_task.py chamado além do script combinado no teste")
            success, cost = self.script.pop(0)
            self.call_order.append((arm, success))
            _write_result_row(worktree, success, cost)
            return _ok()
        if cmd[0] == "bash":
            return _ok()
        raise AssertionError(f"comando inesperado em subprocess.run: {cmd}")


def _write_cfg(tmp_path: Path, name: str, n: int, budget: float, min_diff: int = 2) -> Path:
    p = tmp_path / f"{name}.toml"
    p.write_text(EXP_TOML.format(name=name, n=n, budget=budget, min_diff=min_diff))
    return p


# --------------------------------------------------------------- parsing


def test_parse_experiment_campos_obrigatorios(tmp_path):
    cfg_path = _write_cfg(tmp_path, "exp1", n=3, budget=5.0)
    cfg = experiment.parse_experiment(cfg_path)
    assert cfg["name"] == "exp1"
    assert cfg["n_per_arm"] == 3
    assert cfg["mutation"]["file"] == "agent.py"
    assert cfg["decision_rule"]["min_diff_successes"] == 2


def test_parse_experiment_falta_campo(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text(
        'name = "sem_task"\nn_per_arm = 1\nbudget_usd = 1.0\n[mutation]\nold="a"\nnew="b"\n'
    )
    with pytest.raises(experiment.ExperimentError):
        experiment.parse_experiment(p)


# ------------------------------------------------------ intercalação + mutação


def test_intercalacao_ab_e_mutacao_so_em_b(tmp_path, monkeypatch):
    cfg = experiment.parse_experiment(_write_cfg(tmp_path, "interc", n=3, budget=100.0))
    fake = FakeRun(script=[(1, 0.01)] * 6)  # 3 pares, tudo sucesso, custo baixo
    monkeypatch.setattr(subprocess, "run", fake)

    result = experiment.run_experiment(cfg)

    assert [r["arm"] for r in result["runs"]] == ["A", "B", "A", "B", "A", "B"]
    assert fake.call_order == [("A", 1), ("B", 1)] * 3
    assert result["stopped_early"] is False
    assert result["aggregates"]["A"]["successes"] == 3
    assert result["aggregates"]["B"]["successes"] == 3


# ------------------------------------------------------------------ budget


def test_para_no_budget_no_par_completo(tmp_path, monkeypatch):
    cfg = experiment.parse_experiment(_write_cfg(tmp_path, "budget", n=5, budget=1.0))
    # cada par (A+B) custa 0.6 -> após o 2º par, custo acumulado 1.2 >= 1.0, para.
    fake = FakeRun(script=[(1, 0.3)] * 10)
    monkeypatch.setattr(subprocess, "run", fake)

    result = experiment.run_experiment(cfg)

    assert result["stopped_early"] is True
    assert result["stopped_at_pair"] == 1
    assert len(result["runs"]) == 4  # 2 pares completos, nada além
    assert result["cost_total_usd"] == pytest.approx(1.2)


# ------------------------------------------------------------- draft/regra


def _agg(successes_a, n_a, successes_b, n_b) -> dict:
    def arm(successes, n):
        return {
            "n": n,
            "successes": successes,
            "success_rate": successes / n if n else 0.0,
            "cost_usd_total": 0.0,
            "cost_usd_avg": 0.0,
            "tokens_avg": 0.0,
            "turns_avg": 0.0,
        }

    return {"A": arm(successes_a, n_a), "B": arm(successes_b, n_b)}


@pytest.mark.parametrize(
    "successes_a, n_a, successes_b, n_b, expected, verdict",
    [
        (0, 6, 6, 6, "promover", "KEEP"),
        (6, 6, 0, 6, "rejeitar", "DISCARD"),
        # ruído: 4/6 vs 5/6 não distingue nada, e N=5 nem chega ao mínimo
        (4, 6, 5, 6, "inconclusivo", "INCONCLUSIVE"),
        (1, 5, 4, 5, "inconclusivo", "INCONCLUSIVE"),
    ],
)
def test_decide_usa_regua_de_wilson(successes_a, n_a, successes_b, n_b, expected, verdict):
    decision = experiment.decide(_agg(successes_a, n_a, successes_b, n_b), {})
    assert decision["outcome"] == expected
    assert decision["verdict"] == verdict
    assert decision["rule"] == "wilson"


def test_draft_e_json_tres_cenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "EXPERIMENTS_DIR", tmp_path / "experiments")
    # cada item é (sucesso_A, sucesso_B) de um par; script flat = [A,B,A,B,...] na ordem de consumo.
    # N=6 por braço é o mínimo que a régua de Wilson aceita para opinar.
    cenarios = [
        ([(0, 1)] * 6, "promover"),  # A=0/6, B=6/6
        ([(1, 0)] * 6, "rejeitar"),  # A=6/6, B=0/6
        (
            [(1, 1), (1, 1), (0, 1), (0, 1), (1, 0), (0, 0)],  # A=3/6, B=4/6
            "inconclusivo",
        ),
    ]
    for i, (pairs, expected) in enumerate(cenarios):
        script = [item for a, b in pairs for item in ((a, 0.0), (b, 0.0))]
        cfg = experiment.parse_experiment(
            _write_cfg(tmp_path, f"cenario{i}", n=len(pairs), budget=100.0)
        )
        fake = FakeRun(script=script)
        monkeypatch.setattr(subprocess, "run", fake)
        result = experiment.run_experiment(cfg)
        assert result["decision"]["outcome"] == expected

        json_path, draft_path = experiment.write_outputs(result)
        assert json_path.exists()
        assert draft_path.exists()
        assert expected.upper() in draft_path.read_text()
        assert '"outcome": "' + expected + '"' in json_path.read_text()


# --------------------------------------------------------------- worktrees


def test_worktrees_sempre_removidos_no_caminho_feliz(tmp_path, monkeypatch):
    cfg = experiment.parse_experiment(_write_cfg(tmp_path, "cleanup_ok", n=1, budget=100.0))
    fake = FakeRun(script=[(1, 0.0), (1, 0.0)])
    monkeypatch.setattr(subprocess, "run", fake)

    experiment.run_experiment(cfg)

    assert len(fake.created) == 2
    assert set(fake.created) == set(fake.removed)
    for d in fake.created:
        assert not d.exists()


def test_worktrees_removidos_mesmo_com_excecao(tmp_path, monkeypatch):
    cfg = experiment.parse_experiment(_write_cfg(tmp_path, "cleanup_exc", n=3, budget=100.0))
    fake = FakeRun(script=[(1, 0.0)])  # só 1 item: 2ª chamada de run_task.py estoura

    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(AssertionError):
        experiment.run_experiment(cfg)

    assert len(fake.created) == 2
    assert set(fake.created) == set(fake.removed)
    for d in fake.created:
        assert not d.exists()


# ------------------------------------------------------------------- CLI


# -------------------------------------------------------------- parallel


PARALLEL_TOML = """
name = "{name}"
task = "benchmarks/judge/task_j_b2b"
n_per_arm = {n}
budget_usd = {budget}
parallel = true
est_cost_per_run = {est}

[mutation]
file = "agent.py"
old = "MAX_TURNS = 30"
new = "MAX_TURNS = 25"
"""


def _write_parallel_cfg(
    tmp_path: Path, name: str, n: int, budget: float, est: float = 0.35
) -> Path:
    p = tmp_path / f"{name}.toml"
    p.write_text(PARALLEL_TOML.format(name=name, n=n, budget=budget, est=est))
    return p


def _make_fake_popen(script):
    """Substitui subprocess.Popen usado por run_task_launch no modo parallel.

    Cada Popen "roda" a run de forma síncrona no construtor: grava a linha de
    resultado no arquivo apontado por env["HARNESS_RESULTS"] (o mesmo
    contrato que run_task.py real segue) — .wait() só devolve o returncode.
    `script`: lista de (success, cost) consumida na ordem em que os
    Popen são CRIADOS, ou seja, a ordem de disparo do laço em
    _run_experiment_parallel (A0, B0, A1, B1, ...).
    """
    calls: list[dict] = []

    class FakePopen:
        def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None):
            calls.append({"cmd": cmd, "cwd": Path(cwd), "run_id": env["HARNESS_RUN_ID"]})
            if not script:
                raise AssertionError("Popen chamado além do script combinado no teste")
            success, cost = script.pop(0)
            results_path = Path(env["HARNESS_RESULTS"])
            header = "\t".join(experiment.RESULTS_HEADER)
            row = "\t".join(
                str(v)
                for v in [
                    "2026-08-02T00:00:00+00:00",
                    "vtest",
                    "cli",
                    "m",
                    "fixed",
                    "task_j_b2b",
                    success,
                    "1.0",
                    "100",
                    f"{cost:.4f}",
                    "1",
                    "",
                ]
            )
            results_path.write_text(header + "\n" + row + "\n")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    FakePopen.calls = calls
    return FakePopen


def _make_concurrency_popen(script):
    """Como _make_fake_popen, mas a run só "termina" no 2º poll() — assim dá
    pra medir quantos Popen ficam vivos ao mesmo tempo (pico de concorrência)."""
    calls: list[dict] = []
    state = {"alive": 0, "peak": 0}

    class FakePopen:
        def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None):
            calls.append({"cmd": cmd, "cwd": Path(cwd), "run_id": env["HARNESS_RUN_ID"]})
            if not script:
                raise AssertionError("Popen chamado além do script combinado no teste")
            success, cost = script.pop(0)
            results_path = Path(env["HARNESS_RESULTS"])
            header = "\t".join(experiment.RESULTS_HEADER)
            row = "\t".join(
                str(v)
                for v in [
                    "2026-08-02T00:00:00+00:00",
                    "vtest",
                    "cli",
                    "m",
                    "fixed",
                    "task_j_b2b",
                    success,
                    "1.0",
                    "100",
                    f"{cost:.4f}",
                    "1",
                    "",
                ]
            )
            results_path.write_text(header + "\n" + row + "\n")
            self.returncode = None
            self._polls = 0
            state["alive"] += 1
            state["peak"] = max(state["peak"], state["alive"])

        def poll(self):
            self._polls += 1
            if self.returncode is None and self._polls >= 2:
                self.returncode = 0
                state["alive"] -= 1
            return self.returncode

        def wait(self, timeout=None):
            while self.poll() is None:
                pass
            return self.returncode

    FakePopen.calls = calls
    FakePopen.state = state
    return FakePopen


def test_parallel_dispara_tudo_e_coleta(tmp_path, monkeypatch):
    cfg = experiment.parse_experiment(_write_parallel_cfg(tmp_path, "par1", n=3, budget=100.0))
    fake_run = FakeRun(
        script=[]
    )  # só git worktree add/remove + bash setup.sh — sem run_task.py aqui
    fake_popen = _make_fake_popen(script=[(1, 0.1)] * 6)  # 3 pares, tudo sucesso
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = experiment.run_experiment(cfg)

    assert result["mode"] == "parallel"
    assert len(fake_popen.calls) == 6  # TODOS disparados, nenhum esperado antes do próximo
    assert [c["run_id"] for c in fake_popen.calls] == [
        "par1_A_0_" + result["timestamp"],
        "par1_B_0_" + result["timestamp"],
        "par1_A_1_" + result["timestamp"],
        "par1_B_1_" + result["timestamp"],
        "par1_A_2_" + result["timestamp"],
        "par1_B_2_" + result["timestamp"],
    ]
    assert result["n_per_arm_effective"] == 3
    assert result["budget_capped"] is False
    assert result["aggregates"]["A"]["successes"] == 3
    assert result["aggregates"]["B"]["successes"] == 3
    assert len(result["runs"]) == 6
    assert set(fake_run.created) == set(fake_run.removed)


def test_parallel_respeita_max_workers(tmp_path, monkeypatch):
    # 6 runs (3 pares) com teto de 2 simultâneas: nunca mais de 2 Popen vivos.
    cfg = experiment.parse_experiment(_write_parallel_cfg(tmp_path, "parmw", n=3, budget=100.0))
    assert cfg["max_workers"] == 6  # default do parse
    cfg["max_workers"] = 2
    fake_run = FakeRun(script=[])
    fake_popen = _make_concurrency_popen(script=[(1, 0.1)] * 6)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(experiment.time, "sleep", lambda s: None)

    result = experiment.run_experiment(cfg)

    assert fake_popen.state["peak"] == 2  # teto respeitado, e o pool foi usado de fato
    assert len(fake_popen.calls) == 6
    assert [c["run_id"] for c in fake_popen.calls] == [
        "parmw_A_0_" + result["timestamp"],
        "parmw_B_0_" + result["timestamp"],
        "parmw_A_1_" + result["timestamp"],
        "parmw_B_1_" + result["timestamp"],
        "parmw_A_2_" + result["timestamp"],
        "parmw_B_2_" + result["timestamp"],
    ]
    assert len(result["runs"]) == 6
    assert result["aggregates"]["A"]["successes"] == 3
    assert result["aggregates"]["B"]["successes"] == 3


def test_parallel_reduz_n_pelo_budget_antes_de_disparar(tmp_path, monkeypatch):
    # n_per_arm=10, est_cost_per_run=0.4 -> nominal = 10*2*0.4 = 8.0 > budget 1.0
    # n_efetivo = floor(1.0 / (2*0.4)) = 1
    cfg = experiment.parse_experiment(
        _write_parallel_cfg(tmp_path, "par2", n=10, budget=1.0, est=0.4)
    )
    fake_run = FakeRun(script=[])
    fake_popen = _make_fake_popen(script=[(1, 0.1)] * 2)  # só 1 par -> 2 Popen, nunca 20
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = experiment.run_experiment(cfg)

    assert result["n_per_arm_requested"] == 10
    assert result["n_per_arm_effective"] == 1
    assert result["budget_capped"] is True
    assert len(fake_popen.calls) == 2
    assert len(result["runs"]) == 2


def test_parallel_budget_insuficiente_para_1_par_da_erro(tmp_path, monkeypatch):
    # est_cost_per_run=1.0 -> mínimo 1 par custa 2.0, budget é 0.5
    cfg = experiment.parse_experiment(
        _write_parallel_cfg(tmp_path, "par3", n=5, budget=0.5, est=1.0)
    )
    fake_run = FakeRun(script=[])
    fake_popen = _make_fake_popen(script=[])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(experiment.ExperimentError):
        experiment.run_experiment(cfg)


def test_cli_parallel_flag_forca_modo_mesmo_sem_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "EXPERIMENTS_DIR", tmp_path / "experiments")
    cfg_path = _write_cfg(tmp_path, "cli_par", n=2, budget=100.0)  # TOML sem `parallel = true`
    fake_run = FakeRun(script=[])
    fake_popen = _make_fake_popen(script=[(1, 0.05)] * 4)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["experiment.py", "run", str(cfg_path), "--parallel"])

    rc = experiment.main()

    assert rc == 0
    assert len(fake_popen.calls) == 4


def test_main_run_produz_json_e_draft(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(experiment, "EXPERIMENTS_DIR", tmp_path / "experiments")
    cfg_path = _write_cfg(tmp_path, "cli_smoke", n=2, budget=100.0)
    fake = FakeRun(script=[(1, 0.05)] * 4)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["experiment.py", "run", str(cfg_path)])

    rc = experiment.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "cli_smoke" in out
    produced = list((tmp_path / "experiments").glob("cli_smoke_*.json"))
    assert len(produced) == 1
    draft = list((tmp_path / "experiments").glob("cli_smoke_*-draft.md"))
    assert len(draft) == 1
