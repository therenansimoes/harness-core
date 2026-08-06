"""`harness.cursor_tools` — puro, sem servidor: quoting/round-trip do
`Shell`, filtragem de args por schema real do Cursor (fixture), detecção do
turno de resumo (`summary_target`) e parsing da saída do `harness do`.
"""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path

from harness import cursor_tools as ct

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cursor_request.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _shell_schema() -> dict:
    return ct.tools_index(_fixture())[ct.SHELL_FN]


def _todo_schema() -> dict:
    return ct.tools_index(_fixture())[ct.TODO_FN]


# --------------------------------------------------------------------------- tools_index / supports_tools


def test_tools_index_com_fixture_traz_shell_e_todowrite() -> None:
    index = ct.tools_index(_fixture())
    assert "command" in index[ct.SHELL_FN]["properties"]
    assert "todos" in index[ct.TODO_FN]["properties"]


def test_tools_index_entrada_malformada_nunca_levanta() -> None:
    body = {
        "tools": [
            "não é dict",
            {"type": "function"},  # sem function.name
            {"type": "function", "function": {"name": "X", "parameters": "não é objeto"}},
            {"type": "function", "function": {"name": "Ok", "parameters": {"properties": {}}}},
        ]
    }
    assert ct.tools_index(body) == {"Ok": {"properties": {}}}


def test_supports_tools_true_com_fixture() -> None:
    assert ct.supports_tools(_fixture()) is True


def test_supports_tools_false_tool_choice_none() -> None:
    body = {**_fixture(), "tool_choice": "none"}
    assert ct.supports_tools(body) is False


def test_supports_tools_false_sem_shell() -> None:
    body = {"tools": [t for t in _fixture()["tools"] if t["function"]["name"] != ct.SHELL_FN]}
    assert ct.supports_tools(body) is False


def test_supports_tools_false_required_desconhecido() -> None:
    schema = dict(_shell_schema())
    schema["required"] = ["command", "surpresa"]
    body = {"tools": [{"type": "function", "function": {"name": ct.SHELL_FN, "parameters": schema}}]}
    assert ct.supports_tools(body) is False


# --------------------------------------------------------------------------- build_command


def test_build_command_tokens_exatos() -> None:
    cmd = ct.build_command("/usr/bin/python3", "/cfg", "/data", "conserta o bug", 5.0, [])
    assert (
        cmd == "env HARNESS_CONFIG_DIR=/cfg HARNESS_DATA_DIR=/data /usr/bin/python3 "
        "-m harness.cli do 'conserta o bug' --no-apply --max-usd 5.00"
    )


def test_build_command_metacaracteres_fica_um_argumento_so() -> None:
    task = "x'; rm -rf ~ #"
    cmd = ct.build_command("/usr/bin/python3", "/cfg", "/data", task, 5.0, ["--backend", "deepagents"])
    tokens = shlex.split(cmd)
    esperado = [
        "env",
        "HARNESS_CONFIG_DIR=/cfg",
        "HARNESS_DATA_DIR=/data",
        "/usr/bin/python3",
        "-m",
        "harness.cli",
        "do",
        task,
        "--no-apply",
        "--max-usd",
        "5.00",
        "--backend",
        "deepagents",
    ]
    assert tokens == esperado


def test_build_command_sanitiza_control_chars_e_newline() -> None:
    task = "linha1\nlinha2\x07fim"
    cmd = ct.build_command("/usr/bin/python3", "/cfg", "/data", task, 5.0, [])
    tokens = shlex.split(cmd)
    assert tokens[tokens.index("do") + 1] == "linha1 linha2?fim"


# --------------------------------------------------------------------------- shell_call_args / todo_call_args


def test_shell_call_args_com_fixture_schema() -> None:
    args = ct.shell_call_args(_shell_schema(), "env FOO=bar cmd", "/ws", "roda harness do")
    assert args == {
        "command": "env FOO=bar cmd",
        "working_directory": "/ws",
        "description": "roda harness do",
        "block_until_ms": ct.SHELL_BLOCK_MS,
    }


def test_shell_call_args_required_desconhecido_none() -> None:
    schema = dict(_shell_schema())
    schema["required"] = ["command", "notify_on_output"]
    assert ct.shell_call_args(schema, "cmd", "/ws", "desc") is None


def test_shell_call_args_sem_command_declarado_none() -> None:
    schema = {"properties": {"working_directory": {"type": "string"}}, "required": []}
    assert ct.shell_call_args(schema, "cmd", "/ws", "desc") is None


def test_shell_call_args_so_inclui_declarado() -> None:
    schema = {"properties": {"command": {"type": "string"}}, "required": ["command"]}
    args = ct.shell_call_args(schema, "cmd", "/ws", "desc")
    assert args == {"command": "cmd"}


def test_todo_call_args_com_fixture_schema() -> None:
    items = (
        {"id": "1", "content": "passo 1", "status": "in_progress"},
        {"id": "2", "content": "passo 2", "status": "pending"},
    )
    args = ct.todo_call_args(_todo_schema(), items)
    assert args == {"todos": list(items), "merge": False}


def test_todo_call_args_filtra_keys_nao_declaradas() -> None:
    items = ({"id": "1", "content": "x", "status": "pending", "extra": "fora"},)
    args = ct.todo_call_args(_todo_schema(), items)
    assert args["todos"] == [{"id": "1", "content": "x", "status": "pending"}]


def test_todo_call_args_schema_alien_none() -> None:
    assert ct.todo_call_args({"properties": {}}, ({"id": "1"},)) is None
    assert ct.todo_call_args({"properties": {"todos": {"type": "string"}}}, ({"id": "1"},)) is None


# --------------------------------------------------------------------------- tool_call


def test_tool_call_shape_e_argumentos_sao_string_json() -> None:
    call = ct.tool_call(ct.SHELL_FN, {"command": "echo oi"}, 0, new_id=lambda: uuid.UUID(int=1))
    assert call["id"].startswith(ct.TOOL_ID_PREFIX)
    assert call["type"] == "function"
    assert call["index"] == 0
    assert call["function"]["name"] == ct.SHELL_FN
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"command": "echo oi"}


# --------------------------------------------------------------------------- tool_result_text


def test_tool_result_text_content_string() -> None:
    assert ct.tool_result_text({"role": "tool", "content": "saida crua"}) == "saida crua"


def test_tool_result_text_content_parts() -> None:
    msg = {"role": "tool", "content": [{"type": "text", "text": "saida em partes"}]}
    assert ct.tool_result_text(msg) == "saida em partes"


def test_tool_result_text_sem_name_ou_com_name() -> None:
    sem_name = {"role": "tool", "tool_call_id": "call_x", "content": "ok"}
    com_name = {"role": "tool", "name": "Shell", "tool_call_id": "call_x", "content": "ok"}
    assert ct.tool_result_text(sem_name) == ct.tool_result_text(com_name) == "ok"


def test_tool_result_text_json_dict_expande_e_normaliza_exit_code() -> None:
    payload = json.dumps({"stdout": "linha de saida", "exit_code": 1})
    msg = {"role": "tool", "content": payload}
    text = ct.tool_result_text(msg)
    assert "linha de saida" in text
    assert "Exit code: 1" in text


def test_follow_up_example_da_fixture_parseia() -> None:
    example = _fixture()["_follow_up_example"]["message"]
    text = ct.tool_result_text(example)
    assert "resultado  ACEITO" in text
    assert ct._extract_exit_code(text) == 0


# --------------------------------------------------------------------------- summary_target


def _assistant_com_shell(command: str, call_id: str | None = "call_hx_abc123") -> dict:
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": ct.SHELL_FN, "arguments": json.dumps({"command": command})},
    }
    if call_id is None:
        del call["id"]
    return {"role": "assistant", "content": None, "tool_calls": [call]}


def _tool_result(text: str, call_id: str | None = "call_hx_abc123") -> dict:
    msg = {"role": "tool", "content": text}
    if call_id is not None:
        msg["tool_call_id"] = call_id
    return msg


COMMAND = (
    "env HARNESS_CONFIG_DIR=/cfg HARNESS_DATA_DIR=/data /usr/bin/python3 "
    "-m harness.cli do 'conserta o bug' --no-apply --max-usd 5.00"
)


def test_summary_target_none_sem_shell_com_marker() -> None:
    assert ct.summary_target([{"role": "user", "content": "oi"}]) is None


def test_summary_target_none_sem_resultado_ainda() -> None:
    messages = [{"role": "user", "content": "faz"}, _assistant_com_shell(COMMAND)]
    assert ct.summary_target(messages) is None


def test_summary_target_ok_com_resultado() -> None:
    messages = [
        {"role": "user", "content": "faz"},
        _assistant_com_shell(COMMAND),
        _tool_result("Exit code: 0\n\nresultado  ACEITO em 1.0s"),
    ]
    target = ct.summary_target(messages)
    assert target is not None
    assert target["task"] == "conserta o bug"
    assert target["exit_code"] == 0
    assert "ACEITO" in target["results"][0]


def test_summary_target_tool_call_id_ausente_pega_todas_trailing() -> None:
    messages = [
        _assistant_com_shell(COMMAND, call_id=None),
        _tool_result("Exit code: 0\n\nresultado  ACEITO em 1.0s", call_id="qualquer"),
    ]
    assert ct.summary_target(messages) is not None


def test_summary_target_user_novo_depois_invalida() -> None:
    messages = [
        _assistant_com_shell(COMMAND),
        _tool_result("Exit code: 0\n\nresultado  ACEITO em 1.0s"),
        {"role": "user", "content": "pergunta completamente diferente"},
    ]
    assert ct.summary_target(messages) is None


def test_summary_target_regenerate_mesma_task_ainda_retorna() -> None:
    tail = "<timestamp>x</timestamp>\n<user_query>\nconserta o bug\n</user_query>"
    messages = [
        _assistant_com_shell(COMMAND),
        _tool_result("Exit code: 0\n\nresultado  ACEITO em 1.0s"),
        {"role": "user", "content": [{"type": "text", "text": tail}]},
    ]
    target = ct.summary_target(messages)
    assert target is not None
    assert target["task"] == "conserta o bug"


# --------------------------------------------------------------------------- parse_run_output


def test_parse_run_output_aceito() -> None:
    text = "algo\nresultado  ACEITO em 42.3s · 3 arquivo(s) · --no-apply: ficou em harness/do-abc\nledger  ok"
    outcome = ct.parse_run_output(text, 0)
    assert outcome.status == "accepted"
    assert outcome.branch == "harness/do-abc"
    assert outcome.ledger == "ok"


def test_parse_run_output_rejeitado() -> None:
    text = "resultado  NÃO ACEITO — motivo qualquer"
    outcome = ct.parse_run_output(text, 0)
    assert outcome.status == "rejected"


def test_parse_run_output_falhou() -> None:
    outcome = ct.parse_run_output("traceback qualquer\nsem linha de resultado", 1)
    assert outcome.status == "failed"


def test_parse_run_output_incompleto_sem_resultado_e_sem_exit_code() -> None:
    outcome = ct.parse_run_output("ainda rodando...", 0)
    assert outcome.status == "incomplete"


def test_parse_run_output_resultado_estranho_nao_vira_failed() -> None:
    outcome = ct.parse_run_output("resultado  ALGO_INESPERADO", 1)
    assert outcome.status == "incomplete"


def test_parse_run_output_tail_ultimas_linhas() -> None:
    text = "\n".join(f"linha{i}" for i in range(10))
    outcome = ct.parse_run_output(text, 0)
    assert outcome.tail == "\n".join(f"linha{i}" for i in range(4, 10))


# --------------------------------------------------------------------------- summary_md — contrato de estilo


_PROIBIDAS = ("tool_call", "Shell", "TodoWrite", "function call", "ferramenta")


def test_summary_md_aceito_bate_esqueleto() -> None:
    outcome = ct.parse_run_output(
        "resultado  ACEITO em 42.3s · 3 arquivo(s) · --no-apply: ficou em wip/unit-abc", 0
    )
    md = ct.summary_md(outcome, "task")
    assert "**resultado**: ACEITO em 42.3s · 3 arquivo(s)" in md
    assert "**branch de entrega**: `wip/unit-abc`" in md
    assert "`git merge wip/unit-abc`" in md
    assert "`harness report`" in md
    assert "\n\n" in md


def test_summary_md_incompleto_nao_menciona_status() -> None:
    outcome = ct.parse_run_output("ainda rodando", 0)
    md = ct.summary_md(outcome, "task")
    assert "/status" not in md
    assert "segundo plano" in md
    assert "`harness report`" in md


def test_summary_md_nunca_tem_substring_proibida() -> None:
    for text, exit_code in [
        ("resultado  ACEITO em 1s · ficou em wip/x", 0),
        ("resultado  NÃO ACEITO — recusado", 0),
        ("traceback", 1),
        ("rodando", 0),
    ]:
        outcome = ct.parse_run_output(text, exit_code)
        md = ct.summary_md(outcome, "task")
        for proibida in _PROIBIDAS:
            assert proibida not in md, (proibida, md)
