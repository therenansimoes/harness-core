"""Reflect como checker do retry (padrão worker/checker).

O contrato testado: (1) verify vermelho + gate em retry => o hint cita o arquivo
que o `verify_cmd` cobra e o worker não tocou, e ele chega no prompt da tentativa
SEGUINTE (não na primeira); (2) fail-open — estado sem material devolve "" sem
exceção; (3) a topologia default do repo põe reflect na perna retry->route.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.graph import reflect, topology
from harness.graph.run_graph import run_unit
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

MISSING = "exigido.txt"


class RecorderBackend:
    """Nunca cria o arquivo que a régua cobra: as duas tentativas reprovam. O
    que interessa é o prompt de cada chamada — é ali que o hint tem de aparecer."""

    name: ClassVar[str] = "recorder"

    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.prompts.append(req.prompt)
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / "outro.txt").write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=("outro.txt",),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def recorder():
    prompts: list[str] = []
    registry.register("recorder", lambda: RecorderBackend(prompts))
    yield prompts
    registry.unregister("recorder")


def _unit(tmp_path: Path, name: str, verify_cmd: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "faça a coisa"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    return unit


def test_hint_cita_arquivo_exigido_e_entra_no_prompt_da_2a_tentativa(data_dir, tmp_path, recorder):
    unit = _unit(tmp_path, "skeptic", f"grep -q pronto {MISSING}")
    final = run_unit(unit, "recorder", None, data_dir, thread_id="t-reflect", max_attempts=2)

    assert final["attempt"] == 1
    hint = final["reflect_hint"]
    # Exit code, não o texto do log: 2 é o grep sem o arquivo.
    assert "A régua reprovou (exit 2)" in hint
    assert "Você alterou: outro.txt" in hint
    assert f"Arquivos exigidos e NÃO alterados: {MISSING}" in hint
    assert "Padrões exigidos no conteúdo: pronto" not in hint  # grep sem quotes

    assert len(recorder) == 2, "duas tentativas, dois prompts"
    assert recorder[0] == "faça a coisa", "tentativa 0 não tem feedback de nada"
    # O hint é dado (texto do checker), não ordem: entra no bloco não confiável
    # ANTES da tarefa, que segue rotulada como única fonte de instrução.
    from harness.trust_boundary import TASK_HEADER

    bloco, sep, tarefa = recorder[1].partition(f"{TASK_HEADER}\n")
    assert sep
    assert tarefa == "faça a coisa"
    assert reflect.HINT_HEADER.lstrip("# ") in bloco
    assert MISSING in bloco

    nodes = [e["node"] for e in final["events"]]
    assert nodes[nodes.index("retry") : nodes.index("retry") + 3] == [
        "retry",
        "reflect",
        "route",
    ]
    assert [e["hint"] for e in final["events"] if e["node"] == "reflect"] == [True]


def test_hint_nao_repete_nenhuma_palavra_do_log_do_verify():
    """O log é do verificador selado e pode conter o gabarito; o hint vai pro
    prompt da próxima tentativa. Nada do tail pode atravessar."""
    unit = SimpleNamespace(verify_cmd="python verify.py")
    hint = reflect.build_hint(
        {
            "unit": unit,
            "exec": SimpleNamespace(files_changed=("outro.txt",)),
            "events": [
                {
                    "node": "verify",
                    "exit_code": 1,
                    "tail": "esperado=GABARITO_SECRETO\nassert falhou\n",
                }
            ],
        }
    )
    assert "GABARITO_SECRETO" not in hint
    assert "assert falhou" not in hint
    assert "A régua reprovou (exit 1)" in hint
    assert "verify.py" in hint  # verify_cmd é público: pode citar


def test_build_hint_fail_open_sem_exec_e_sem_tail():
    assert reflect.build_hint({}) == ""
    assert reflect.build_hint({"unit": None, "exec": None, "events": []}) == ""
    # hydrate de estado torto também não levanta (attempt sem run_id no ledger).
    assert reflect.build_hint(reflect.hydrate({"attempt": 1})) == ""


def test_topologia_default_do_repo_tem_reflect_na_perna_do_retry():
    spec = topology.load_spec()
    edges = [tuple(e) for e in spec["edges"]]
    assert "reflect" in spec["nodes"]
    assert ("retry", "reflect") in edges and ("reflect", "route") in edges
    assert ("retry", "route") not in edges
    topology.build(spec)  # compila: default do repo é spec válida
