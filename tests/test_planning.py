"""ONDA B: planejamento como estado vivo.

Nenhum teste aqui chama modelo nem rede — o `create_deep_agent` e o `invoke` do
agente são espionados/fakes. O que se verifica é o encanamento: o middleware
entra no stack, os `todos` sobrevivem no trace, e o gate do nó `plan` carimba a
ordem no prompt só quando a tarefa é grande.
"""

import json
from pathlib import Path

import pytest

from harness.backends import deepagents_backend as da
from harness.graph import run_graph as rg
from harness.types import ExecRequest, UnitSpec

REPO = Path(__file__).resolve().parent.parent


def _unit(prompt: str) -> UnitSpec:
    return UnitSpec(id="u1", prompt=prompt, verify_cmd="true", path=REPO)


def test_todo_middleware_entra_no_stack_com_prompt_em_pt_br(tmp_path, monkeypatch):
    """Sem o middleware o plano é parágrafo perdido na conversa; com ele é
    estado (`state["todos"]`) reinjetado a cada turno. O `system_prompt=`
    substitui o bloco default da lib (inglês, longo)."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado.update(kw)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))

    nomes = [type(m).__name__ for m in capturado["middleware"]]
    assert "TodoListMiddleware" in nomes
    todo = next(m for m in capturado["middleware"] if type(m).__name__ == "TodoListMiddleware")
    assert todo.system_prompt == da.TODO_PROMPT
    assert "write_todos" in da.TODO_PROMPT and "in_progress" in da.TODO_PROMPT
    assert "You have access" not in todo.system_prompt  # o default da lib saiu


def test_manual_de_planning_chega_no_system_prompt(tmp_path, monkeypatch):
    """O fragmento prompts/tools.d/20-planning.md (renderizado em tools.md por
    scripts/build_prompts.py) é o que diz COMO chamar a tool."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, str] = {}

    def spy(*a, **kw):
        capturado["system_prompt"] = kw["system_prompt"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    prompt = capturado["system_prompt"]
    assert "Manual das tools" in prompt  # o base continua lá
    assert "## write_todos" in prompt
    assert "## Protocolo" in prompt  # executor.md manda planejar antes de editar


def test_fragmento_de_planning_esta_no_manual_gerado():
    """Guarda contra `tools.md` gerado sem o fragmento (esquecer de rodar o
    scripts/build_prompts.py deixa o manual sem a tool)."""
    manual = (REPO / "prompts" / "tools.md").read_text(encoding="utf-8")
    assert "## write_todos" in manual
    assert (REPO / "prompts" / "tools.d" / "20-planning.md").read_text(
        encoding="utf-8"
    ).strip() in manual


def test_todos_do_estado_viram_linha_no_trace(tmp_path, monkeypatch):
    """O plano que o agente seguiu tem que sobrar depois do run."""
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)

    class _Agent:
        def invoke(self, payload, config):
            return {
                "messages": [da._TraceMsg("ai", "feito")],
                "todos": [
                    {"content": "editar /app.py", "status": "completed"},
                    {"content": "rodar pytest", "status": "in_progress"},
                ],
            }

    monkeypatch.setattr(da, "_build_agent", lambda req: (_Agent(), object()))

    ws = tmp_path / "ws"
    ws.mkdir()
    trace = tmp_path / "trace.jsonl"
    res = da.DeepagentsBackend().execute(
        ExecRequest(prompt="x", workspace=ws, trace_path=trace, max_turns=3)
    )
    assert res.exit_reason == "done"

    linhas = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    (rec,) = [x for x in linhas if x.get("type") == "todos"]
    assert rec["todos"] == [
        {"content": "editar /app.py", "status": "completed"},
        {"content": "rodar pytest", "status": "in_progress"},
    ]
    # A linha é ADITIVA: as mensagens continuam no mesmo formato.
    assert [x["content"] for x in linhas if x.get("type") == "ai"] == ["feito"]


def test_sem_todos_o_trace_nao_muda(tmp_path):
    trace = tmp_path / "trace.jsonl"
    da._write_trace(trace, [da._TraceMsg("ai", "oi")], todos=None)
    linhas = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    assert linhas == [{"type": "ai", "content": "oi"}]


def test_gate_do_plano_pega_multi_arquivo_e_ignora_tarefa_curta():
    assert rg._needs_plan("implemente o parser em /parser.py") is True
    assert rg._needs_plan("refatore a função soma") is True
    assert rg._needs_plan("mude os arquivos a.py e b.py para usar Path") is True
    assert rg._needs_plan("x" * (rg.PLAN_PROMPT_CHARS + 1)) is True
    # Um arquivo, um passo: plano aqui é só turno gasto.
    assert rg._needs_plan("no arquivo /soma.py troque o - por + na função soma") is False
    assert rg._needs_plan("corrija o typo em app.py") is False


def test_needs_plan_prepende_a_ordem_no_prompt():
    unit = _unit("refatore o modulo")
    assert rg._prompt({"unit": unit, "needs_plan": True}).startswith(rg.PLAN_ORDER)
    assert "task(subagent_type='planner')" in rg.PLAN_ORDER
    assert "write_todos" in rg.PLAN_ORDER
    # Sem o flag (e em checkpoint antigo, que não tem a chave) nada muda.
    assert rg._prompt({"unit": unit}) == unit.prompt


def test_ordem_do_plano_vem_antes_do_hint_do_reflect():
    """A ordem é a PRIMEIRA linha: modelo pequeno obedece o começo do prompt."""
    from harness.graph.reflect import HINT_HEADER

    out = rg._prompt({"unit": _unit("refatore o modulo"), "needs_plan": True, "reflect_hint": "h"})
    assert out.startswith(rg.PLAN_ORDER)
    assert out.endswith(f"{HINT_HEADER}\nh")


def test_no_carimba_o_flag_e_o_evento(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    out = rg._plan({"unit": _unit("implemente o parser"), "attempt": 0})
    assert out["needs_plan"] is True
    assert out["events"][0]["needs_plan"] is True

    curto = rg._plan({"unit": _unit("troque - por + em /soma.py"), "attempt": 0})
    assert curto["needs_plan"] is False
