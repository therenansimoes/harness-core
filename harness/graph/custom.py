"""Biblioteca de workflows nomeados (config/workflows/*.toml).

Generaliza a topologia única: cada arquivo no diretório é um workflow com
nome, no MESMO formato do `config/topology.toml`, validado pelo MESMO
`topology.compile_spec` — whitelist de nós, nós obrigatórios, gate
condicional. O próprio loop pode criar/evoluir workflows (ação 'workflow'
em harness/improve/workflow_action.py); aqui só se lê e roda, fail-closed:
spec torta => TopologyError, nunca grafo meio-válido.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from harness.graph import run_graph as _rg
from harness.graph import topology
from harness.routing import config_dir

WORKFLOWS_SUBDIR = "workflows"


def workflows_dir() -> Path:
    return config_dir() / WORKFLOWS_SUBDIR


def list_workflows(dir: Path | str | None = None) -> list[str]:
    """Nomes dos workflows disponíveis. Diretório ausente => lista vazia —
    biblioteca vazia não é erro, é estado inicial."""
    d = Path(dir) if dir is not None else workflows_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def load_workflow(name: str, dir: Path | str | None = None) -> dict:
    """Spec do workflow, já validada por compile. Qualquer problema
    (arquivo ausente, toml torto, spec inválida) => TopologyError."""
    d = Path(dir) if dir is not None else workflows_dir()
    path = d / f"{name}.toml"
    if not path.is_file():
        raise topology.TopologyError(
            f"workflow desconhecido: {name!r} (disponíveis: {list_workflows(d)})"
        )
    try:
        spec = topology.load_spec(path)
    except tomllib.TOMLDecodeError as e:
        raise topology.TopologyError(f"{path}: toml torto: {e}") from e
    topology.compile_spec(spec)  # valida fail-closed; erro sobe como está
    return spec


def run_workflow(
    name: str,
    unit_dir: Path | str,
    backend: str = "mock",
    model: str | None = None,
    data_dir: Path | str | None = None,
    thread_id: str | None = None,
    max_attempts: int | None = None,
    dir: Path | str | None = None,
) -> dict:
    """Roda uma unidade pelo workflow nomeado. Espelho do `run_unit`:
    mesma forma de estado, config e checkpointer — só o grafo vem do spec
    nomeado em vez da topologia default."""
    # Import tardio pelo mesmo motivo do run_unit: o cli chama o grafo.
    from harness.cli import load_unit

    spec = load_workflow(name, dir=dir)
    if max_attempts is None:
        max_attempts = _rg.load_policy().max_attempts

    unit = load_unit(Path(unit_dir))
    if data_dir is None:
        data_dir = os.environ.get("HARNESS_DATA_DIR", "data")
    data_dir = Path(data_dir)
    thread_id = thread_id or f"wf-{name}-{unit.id}"

    with _rg.open_checkpointer(data_dir) as checkpointer:
        graph = topology.compile_spec(spec, checkpointer=checkpointer)
        config = {
            "configurable": {
                "thread_id": thread_id,
                _rg.CFG_DATA_DIR: str(data_dir),
                _rg.CFG_BACKEND: backend,
                _rg.CFG_MODEL: model,
                _rg.CFG_MAX_TURNS: _rg.DEFAULT_MAX_TURNS,
                _rg.CFG_ROUTE: _rg.ROUTE_MANUAL,
            },
            # Mesma conta do run_unit: ~8 supersteps por tentativa + folga.
            "recursion_limit": 12 * (max_attempts + 1),
        }
        # `next` não vazio = thread parou no meio: retoma sem reinjetar.
        pending = bool(graph.get_state(config).next)
        payload = None if pending else _rg.initial_state(unit, thread_id, max_attempts)
        return graph.invoke(payload, config)
