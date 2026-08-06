"""Protocolo nativo de `tool_calls` do Cursor — puro, sem I/O, sem import de
`harness.serve` (a seta só pode apontar pra fora, mesma regra de
`harness/paths.py`) e stdlib apenas (mesma restrição de `uv.lock` imutável
documentada no docstring de `serve.py`).

Decisão (bead harness-core-z8n): quando o cliente declara `tools` utilizável,
o servidor NÃO roda mais `harness do` em segundo plano por conta própria —
ele responde o turno de ação com `content` (preâmbulo) + `tool_calls`
(`TodoWrite` opcional + `Shell` rodando o harness CLI NO TERMINAL DO CURSOR,
sob a aprovação dele) e, no turno seguinte (que traz os resultados
`role=tool`), responde com um resumo markdown e `finish_reason="stop"`. Zero
chamada de LLM nos dois turnos — as funções deste módulo são uma máquina de
estado sobre a conversa, não um agente.

Duas constantes de env (`HARNESS_CONFIG_DIR`/`HARNESS_DATA_DIR`) são
duplicadas aqui em vez de importadas de `harness.paths` — os valores são os
mesmos de `paths.CONFIG_DIR_ENV`/`paths.DATA_DIR_ENV`, mas a restrição
"stdlib apenas" deste módulo é literal: zero import de `harness.*`.

Contrato de estilo (§3 do spec) — todo texto que sai destas funções pro
cliente com tools: markdown; "\n\n" entre blocos; crase em paths, branches,
comandos e flags; `**label**:` pra campos de resultado; NUNCA nomear a tool
ou o protocolo (substrings proibidas: "tool_call", "Shell", "TodoWrite",
"function call", "ferramenta"); sem dois-pontos imediatamente antes de uma
cláusula de ação (usar travessão); no máximo ~6 linhas substantivas. Ver
`summary_md` pro esqueleto de referência.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

SHELL_FN = "Shell"
TODO_FN = "TodoWrite"
HARNESS_MARKER = "harness.cli"
TOOL_ID_PREFIX = "call_hx_"
SHELL_BLOCK_MS = 600_000  # expirar = Cursor move o comando pra background, não mata (fixture confirma)
TAIL_LINES = 6

# Mesmo raciocínio de `serve.ACTION_MAX_CHARS`: teto pra texto de PEDIDO, não
# despejo de arquivo. Duplicado (não importado) — ver docstring do módulo.
ACTION_MAX_CHARS = 2000

# Mesmos valores de `harness.paths.CONFIG_DIR_ENV`/`DATA_DIR_ENV` — duplicados
# de propósito, este módulo não importa `harness.*`.
_CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"
_DATA_DIR_ENV = "HARNESS_DATA_DIR"

# Defaults server-side que cobrem `required` do schema real do Cursor (ver
# fixture): todo nome fora deste conjunto não tem valor conhecido pra
# preencher sozinho — schema com `required` fora daqui é sinal de drift do
# protocolo do Cursor, e o caminho correto é degradar pro legado, não
# adivinhar um valor.
_SHELL_DEFAULT_NAMES = ("command", "working_directory", "description", "block_until_ms")
_TODO_DEFAULT_NAMES = ("todos", "merge")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL | re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"exit code:\s*(-?\d+)", re.IGNORECASE)
_RESULT_LINE_RE = re.compile(r"^\s*resultado\s{2,}(?P<rest>\S.*?)\s*$", re.MULTILINE)
_BRANCH_RE = re.compile(r"(?:ficou em|git merge)\s+(?P<branch>\S+)")
_LEDGER_RE = re.compile(r"^\s*ledger\s{2,}(?P<ledger>.+?)\s*$", re.MULTILINE)


def _content_text(msg: dict) -> str:
    """Equivalente a `serve.message_text`, reimplementado aqui pra este
    módulo não depender de `serve` — mesmo shape str-ou-partes."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _user_query_text(raw: str) -> str:
    """Equivalente a `serve.user_query_text` — desembrulha `<user_query>`,
    último bloco ganha; sem a tag, passthrough."""
    matches = _USER_QUERY_RE.findall(raw)
    return matches[-1].strip() if matches else raw


def _sanitize_task(task: str) -> str:
    """Task sanitizada ANTES de entrar no `command`: control chars/NUL viram
    "?", newlines viram espaço (não podem quebrar a linha do comando do
    shell), clip em `ACTION_MAX_CHARS`. Mesma função é usada em
    `build_command` e em `summary_target` (comparação de regenerate) — as
    duas precisam concordar byte a byte no mesmo texto de task."""
    task = task.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    task = _CONTROL_RE.sub("?", task)
    return task[:ACTION_MAX_CHARS]


def tools_index(body: dict) -> dict[str, dict]:
    """name -> parameters schema, a partir de `body["tools"]`. Entrada
    malformada (tool sem `function.name`, `parameters` que não é objeto) é
    pulada — nunca levanta, o resto do payload pode estar são."""
    out: dict[str, dict] = {}
    if not isinstance(body, dict):
        return out
    tools = body.get("tools")
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            continue
        out[name] = params
    return out


def supports_tools(body: dict) -> bool:
    """Gate único do caminho novo: `tool_choice` não pode ser `"none"`, o
    Shell precisa estar presente com a propriedade `command` declarada, e
    todo nome em `parameters.required` do Shell precisa ter default
    server-side conhecido (`_SHELL_DEFAULT_NAMES`) — um `required` com nome
    desconhecido é o Cursor pedindo algo que este módulo não sabe preencher
    sozinho, e a resposta correta é degradar pro legado inteiro, não tentar
    meio caminho."""
    if not isinstance(body, dict):
        return False
    if body.get("tool_choice") == "none":
        return False
    schema = tools_index(body).get(SHELL_FN)
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if not isinstance(props, dict) or "command" not in props:
        return False
    required = schema.get("required")
    if required is None:
        required = []
    if not isinstance(required, list):
        return False
    return all(name in _SHELL_DEFAULT_NAMES for name in required)


def build_command(
    python: str, config_dir: str, data_dir: str, task: str, max_usd: float, extra_argv: Sequence[str]
) -> str:
    """Linha de comando EXATA que o Cursor roda no terminal dele — os dois
    `env` são obrigatórios: sem eles, `paths.config_dir()`/`paths.data_dir()`
    (relativos ao checkout) resolveriam contra o `cwd` do comando (o
    workspace do Cursor, não o checkout do harness-core), ver docstring de
    `dispatch_do` em `serve.py`. `python` é `sys.executable` DO SERVIDOR — o
    mesmo interpretador do `dispatch_do`, sem depender de PATH no terminal do
    Cursor."""
    task = _sanitize_task(task)
    tokens = [
        "env",
        f"{_CONFIG_DIR_ENV}={config_dir}",
        f"{_DATA_DIR_ENV}={data_dir}",
        python,
        "-m",
        "harness.cli",
        "do",
        task,
        "--no-apply",
        "--max-usd",
        f"{max_usd:.2f}",
        *extra_argv,
    ]
    return " ".join(shlex.quote(tok) for tok in tokens)


def shell_call_args(schema: dict | None, command: str, cwd: str, description: str) -> dict | None:
    """Argumentos do `Shell` filtrados pelo schema DECLARADO pelo cliente:
    honra `required` primeiro (todo nome sem default conhecido em
    `_SHELL_DEFAULT_NAMES` ⇒ `None`, sinal pro chamador cair no legado),
    depois inclui, do mesmo conjunto de defaults, só o que o schema declara
    (obrigatório ou opcional) — `notify_on_output` nunca entra aqui (objeto
    complexo, sem default server-side; só bloqueia se vier em `required`).
    `block_until_ms` é sempre incluído quando declarado: a fixture confirma
    que expirar move o comando pra background em vez de matar, então emitir
    o valor é seguro."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    required = schema.get("required")
    if required is None:
        required = []
    if not isinstance(required, list):
        return None
    defaults = {
        "command": command,
        "working_directory": cwd,
        "description": description,
        "block_until_ms": SHELL_BLOCK_MS,
    }
    for name in required:
        if name not in defaults:
            return None
    args = {name: defaults[name] for name in _SHELL_DEFAULT_NAMES if name in props}
    if "command" not in args:
        return None
    return args


def todo_call_args(schema: dict | None, items: Sequence[dict]) -> dict | None:
    """Mesma regra de `shell_call_args`, aplicada ao shape do `TodoWrite`:
    `properties.todos.items.properties` declara as keys de cada item (`id`,
    `content`, `status` na fixture real) — cada item de `items` é filtrado
    por essas keys; `merge` (quase sempre `required` na fixture) recebe
    `False` (substituir, não mesclar — este módulo nunca lê o estado anterior
    de todos do Cursor). Schema sem `todos.items.properties` reconhecível
    (shape alienígena) ⇒ `None`, o chamador simplesmente não emite o
    `TodoWrite` (o `Shell` segue sozinho — ver `serve.plan_turn`)."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    todos_schema = props.get("todos")
    if not isinstance(todos_schema, dict):
        return None
    item_schema = todos_schema.get("items")
    if not isinstance(item_schema, dict):
        return None
    item_props = item_schema.get("properties")
    if not isinstance(item_props, dict):
        return None
    filtered_items = []
    for item in items:
        if not isinstance(item, dict):
            return None
        filtered_items.append({k: v for k, v in item.items() if k in item_props})
    required = schema.get("required")
    if required is None:
        required = []
    if not isinstance(required, list):
        return None
    defaults = {"todos": filtered_items, "merge": False}
    for name in required:
        if name not in defaults:
            return None
    args = {name: defaults[name] for name in _TODO_DEFAULT_NAMES if name in props}
    if "todos" not in args:
        return None
    return args


def tool_call(
    name: str, args: dict, index: int, new_id: Callable[[], uuid.UUID] = uuid.uuid4
) -> dict:
    """Shape de um `tool_calls[i]` do protocolo OpenAI/Cursor. `arguments` é
    SEMPRE string JSON, nunca objeto — cliente estrito espera exatamente
    isso. `new_id` injetável pro teste, mesmo idioma de `serve.new_id`."""
    call_id = TOOL_ID_PREFIX + new_id().hex[:8]
    return {
        "id": call_id,
        "type": "function",
        "index": index,
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def tool_result_text(msg: dict) -> str:
    """Texto de um `role=tool`, tolerante ao shape real do Cursor: `content`
    string pura OU lista de partes (`_content_text`, mesmo shape de
    `message_text`); `name` NÃO é exigido (o protocolo só exige `role`,
    `content`, `tool_call_id` — `name` pode vir como extra do Cursor, mas
    algum cliente pode omitir). Quando o texto extraído é, ele mesmo, um JSON
    de objeto (`{"stdout": ..., "exit_code": ...}` — shape alternativo, não o
    da fixture real, que já vem como texto puro "Exit code: N\\n\\n..."),
    concatena os valores de output/stdout/stderr/result/content na ordem e
    normaliza `exit_code` pra "Exit code: N" — MESMA forma literal que já sai
    do shape de texto puro, pra `summary_target` extrair com uma regex só."""
    text = _content_text(msg)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, dict):
        return text
    parts = [
        str(parsed[k]) for k in ("output", "stdout", "stderr", "result", "content") if k in parsed
    ]
    if "exit_code" in parsed:
        parts.append(f"Exit code: {parsed['exit_code']}")
    return "\n".join(parts) if parts else text


def _extract_task(command: str) -> str:
    """Token depois de `do` no `command` do `Shell` — mesma tokenização de
    `build_command` (shlex), então o round-trip é exato mesmo com task
    contendo metacaracteres de shell."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if "do" in tokens:
        i = tokens.index("do")
        if i + 1 < len(tokens):
            return tokens[i + 1]
    return ""


def _shell_command_with_marker(msg: dict) -> tuple[str, str | None] | None:
    """Primeiro `tool_call` do Shell nesta mensagem assistant cujo
    `arguments.command` contém `HARNESS_MARKER` → `(command, tool_call_id)`;
    `tool_call_id` pode ser `None` (chamada malformada, sem `id`)."""
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict) or fn.get("name") != SHELL_FN:
            continue
        raw_args = fn.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else None
        except (ValueError, TypeError):
            args = None
        if not isinstance(args, dict):
            continue
        command = args.get("command")
        if isinstance(command, str) and HARNESS_MARKER in command:
            return command, tc.get("id")
    return None


def _extract_exit_code(text: str) -> int:
    m = _EXIT_CODE_RE.search(text)
    return int(m.group(1)) if m else 0


def summary_target(messages: list[dict]) -> dict | None:
    """Anda do fim da conversa atrás da última mensagem assistant com um
    `tool_call` do Shell cujo `command` bate `HARNESS_MARKER` — esse é o
    dispatch que este servidor fez. A partir dali, coleta os `role=tool`
    seguintes que casam o `tool_call_id` (sem `id` na chamada original ⇒
    pega TODOS os `tool` trailing, degradação tolerante); sem nenhum
    resultado ainda, devolve `None` (ainda rodando — não há o que resumir).

    Nenhuma mensagem `user` pode vir DEPOIS do último resultado — EXCETO uma
    `user` cujo texto (desembrulhado de `<user_query>` e sanitizado com a
    MESMA função de `build_command`) é exatamente a task já rodada: isso é
    regenerate do Cursor no mesmo turno, não um pedido novo, e ainda devolve
    o alvo (`serve.plan_turn` responde o resumo de novo, sem redisparar)."""
    idx = None
    command = None
    tool_call_id = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        found = _shell_command_with_marker(msg)
        if found is not None:
            idx, (command, tool_call_id) = i, found
            break
    if idx is None or command is None:
        return None
    results: list[str] = []
    trailing_user: dict | None = None
    for msg in messages[idx + 1 :]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            mcid = msg.get("tool_call_id")
            if tool_call_id is None or mcid is None or mcid == tool_call_id:
                results.append(tool_result_text(msg))
        elif role == "user":
            trailing_user = msg
    if not results:
        return None
    task = _extract_task(command)
    if trailing_user is not None:
        redo = _sanitize_task(_user_query_text(_content_text(trailing_user)).strip())
        if redo != _sanitize_task(task.strip()):
            return None
    return {"task": task, "results": results, "exit_code": _extract_exit_code("\n\n".join(results))}


@dataclass(frozen=True)
class RunOutcome:
    status: str  # "accepted" | "rejected" | "failed" | "incomplete"
    resultado: str
    branch: str
    ledger: str
    tail: str


def parse_run_output(text: str, exit_code: int) -> RunOutcome:
    """`status` é um balde SÓ pra "rodando/negado/desconhecido" (`incomplete`)
    quando não dá pra afirmar nada com confiança — nunca adivinha "aceito"
    ou "falhou" sem a linha `resultado` de verdade. `failed` exige exit_code
    truthy E nenhuma linha `resultado` (com linha `resultado` presente mas
    que não bate nem ACEITO nem NÃO ACEITO, cai em `incomplete` — texto
    inesperado não vira falha por adivinhação)."""
    m = _RESULT_LINE_RE.search(text)
    resultado = m.group("rest").strip() if m else ""
    if m and resultado.startswith("NÃO ACEITO"):
        status = "rejected"
    elif m and resultado.startswith("ACEITO"):
        status = "accepted"
    elif m is None and exit_code:
        status = "failed"
    else:
        status = "incomplete"
    bm = _BRANCH_RE.search(text)
    branch = bm.group("branch") if bm else ""
    lm = _LEDGER_RE.search(text)
    ledger = lm.group("ledger").strip() if lm else ""
    lines = [line for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-TAIL_LINES:])
    return RunOutcome(status=status, resultado=resultado, branch=branch, ledger=ledger, tail=tail)


def summary_md(outcome: RunOutcome, task: str) -> str:
    """Contrato de estilo no docstring do módulo (§3) — `outcome.tail` NUNCA
    entra aqui: é saída crua do `harness do`, pode carregar qualquer palavra
    (inclusive as proibidas) e estouraria o teto de ~6 linhas; fica só como
    campo de diagnóstico de `RunOutcome`. `task` é mantido na assinatura por
    simetria com o spec — o resumo não repete a task (o Cursor já mostra o
    pedido original acima na conversa)."""
    del task
    if outcome.status in ("accepted", "rejected"):
        blocks = [f"**resultado**: {outcome.resultado}"]
        if outcome.branch:
            blocks.append(f"**branch de entrega**: `{outcome.branch}`")
            blocks.append(
                f"Nada foi aplicado sozinho — revise e junte com `git merge {outcome.branch}`."
            )
        if outcome.ledger:
            blocks.append(f"**ledger**: {outcome.ledger}")
        blocks.append("Detalhes: `harness report`.")
        return "\n\n".join(blocks)
    if outcome.status == "failed":
        return "\n\n".join(
            [
                "**resultado**: terminou com erro antes de fechar uma decisão",
                "Detalhes: `harness report`.",
            ]
        )
    # incomplete: nenhuma linha `resultado` — não inventa um /status que este
    # servidor não tem (o run é do terminal do Cursor, não um job daqui).
    return "\n\n".join(
        [
            "**resultado**: ainda sem uma linha de fechamento",
            "O terminal segue em segundo plano no Cursor — acompanhe por lá.",
            "Detalhes: `harness report`.",
        ]
    )
