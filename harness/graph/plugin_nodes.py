"""Whitelist que CRESCE: nós de plugin em `plugins/nodes/`, com gate humano.

`topology.NODE_IMPLS` é a whitelist do que a spec pode citar, e ela ser fechada
é o que impede a spec de virar execução arbitrária. Aqui ela ganha uma porta —
com tranca dupla, porque um nó é código nosso rodando dentro do grafo, não
config: só entra módulo que passa no guard de AST **e** cujo sha256 exato foi
aprovado por um humano (`data/node_approvals.jsonl`, escrito pela ação `node`).
Arquivo editado depois da aprovação muda o hash e volta a ser recusado.

Fail-OPEN por desenho, ao contrário do `topology._validate`: nó de plugin é
enfeite, não espinha. Recusa (ou dir ausente, ou módulo torto) tira aquele nó e
o grafo roda igual — `register_all` nunca levanta, e a spec que citar um nó não
registrado morre no fail-closed da topologia, que é onde essa decisão mora.

`HARNESS_PLUGIN_NODES=0|off|false` é o kill switch: nenhum nó de plugin carrega,
nem aprovado.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
from pathlib import Path

from harness.improve import root_dir
from harness.ledger import store

NODES_SUBDIR = Path("plugins") / "nodes"
APPROVALS_NAME = "node_approvals.jsonl"
APPROVAL_VERDICT = "KEEP"

KILL_SWITCH = "HARNESS_PLUGIN_NODES"
KILL_VALUES = frozenset({"0", "off", "false"})

# Nome do arquivo É o nome do nó na spec: slug estrito barra traversal, dunder e
# nome de uma letra que colidiria com qualquer coisa.
NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,31}\Z")

NODE_FUNC = "node"
NODE_PARAMS = ("state", "config")
EVENTS_KEY = "events"

# Import destes é subprocesso/rede/FFI por outro nome; um nó não precisa de
# nenhum deles para calcular um evento, e quem precisa não é um nó.
FORBIDDEN_MODULES = frozenset({"subprocess", "socket", "ctypes", "multiprocessing"})
# `eval`/`exec`/`__import__` fariam o guard de AST inútil: o código proibido
# entraria como string.
FORBIDDEN_NAMES = frozenset({"eval", "exec", "__import__"})

# Nome -> sha256 do que ESTE processo já registrou. Sem isso a segunda chamada
# veria o próprio registro em NODE_IMPLS e recusaria como "builtin".
_REGISTERED: dict[str, str] = {}


def disabled() -> bool:
    return os.environ.get(KILL_SWITCH, "").strip().lower() in KILL_VALUES


def nodes_dir(root: Path | str | None = None) -> Path:
    return root_dir(root) / NODES_SUBDIR


def approvals_path(data_dir: Path | str | None = None) -> Path:
    """`store.data_dir()` em call-time: teste em tmpdir troca a env, não o módulo."""
    base = Path(data_dir) if data_dir is not None else store.data_dir()
    return base / APPROVALS_NAME


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_approvals(data_dir: Path | str | None = None) -> dict[str, set[str]]:
    """nome -> hashes aprovados. Arquivo ausente/linha torta = sem aprovação:
    ler mal um ack não pode virar ack."""
    out: dict[str, set[str]] = {}
    path = approvals_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("verdict") != APPROVAL_VERDICT:
            continue
        name, digest = row.get("name"), row.get("sha256")
        if isinstance(name, str) and isinstance(digest, str) and digest:
            out.setdefault(name, set()).add(digest)
    return out


def record_approval(
    name: str, digest: str, data_dir: Path | str | None = None
) -> Path:
    """Appenda o ack. Só a ação `node` (com ack humano) chama isto."""
    path = approvals_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "name": name,
        "sha256": digest,
        "verdict": APPROVAL_VERDICT,
        "ts": store.now_iso(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def register_all(
    root: Path | str | None = None, data_dir: Path | str | None = None
) -> dict[str, str]:
    """Varre `plugins/nodes/*.py` e devolve nome -> "registered" | motivo.

    Idempotente e sem exceção: chamada duas vezes registra uma. A ordem das
    checagens é a ordem do custo e do risco — nome, colisão com builtin e AST
    antes de qualquer `exec_module`, porque importar já é executar.
    """
    result: dict[str, str] = {}
    try:
        if disabled():
            return result
        folder = nodes_dir(root)
        if not folder.is_dir():
            return result

        from harness.graph import topology

        approvals = load_approvals(data_dir)
        for path in sorted(folder.glob("*.py")):
            name = path.stem
            if name in _REGISTERED:
                result[name] = "registered"
                continue
            reason = _try_register(path, name, approvals, topology)
            result[name] = reason or "registered"
    except Exception as exc:  # fail-open: registry torto não derruba o grafo
        print(
            f"plugin_nodes: varredura abortada, seguindo sem nós de plugin: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return result


def _try_register(path: Path, name: str, approvals: dict[str, set[str]], topology) -> str | None:
    """None = registrado; string = motivo da recusa (vai para o doctor)."""
    if not NAME_RE.fullmatch(name):
        return f"nome inválido (esperado {NAME_RE.pattern})"
    if name in topology.NODE_IMPLS:
        return "builtin"

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"ilegível: {type(exc).__name__}: {exc}"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"sintaxe inválida: {exc}"
    guard = _guard(tree)
    if guard:
        return guard

    approved = approvals.get(name, set())
    if not approved:
        return f"sem aprovação humana em {approvals_path().name}"
    digest = file_sha256(path)
    if digest not in approved:
        return f"hash divergente do aprovado (arquivo={digest[:12]})"

    try:
        mod = _import(path, name)
    except Exception as exc:
        # Import é execução: módulo que explode ao carregar fica fora, e o
        # processo segue.
        return f"import falhou: {type(exc).__name__}: {exc}"

    fn = getattr(mod, NODE_FUNC, None)
    if not callable(fn):
        return f"módulo importa mas não expõe {NODE_FUNC}() chamável"
    sig = _signature_error(fn)
    if sig:
        return sig

    topology.NODE_IMPLS.setdefault(name, _wrap(name, fn))
    _REGISTERED[name] = digest
    return None


def _guard(tree: ast.Module) -> str | None:
    """Estático e conservador: o que não dá para provar aqui, recusa."""
    if not any(
        isinstance(n, ast.FunctionDef) and n.name == NODE_FUNC for n in tree.body
    ):
        return f"não define {NODE_FUNC}() no topo do módulo"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in FORBIDDEN_MODULES:
                    return f"import proibido: {mod}"
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                return f"import proibido: {mod}"
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                return f"nome proibido: {node.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES:
                return f"nome proibido: .{node.attr}"
    return None


def _signature_error(fn) -> str | None:
    try:
        params = [
            p
            for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError) as exc:
        return f"assinatura ilegível: {exc}"
    got = tuple(p.name for p in params)
    if got != NODE_PARAMS:
        return f"assinatura {got} != {NODE_PARAMS}"
    return None


def _import(path: Path, name: str):
    """Fora do `sys.modules` global: nó de plugin não sombreia módulo nosso."""
    spec = importlib.util.spec_from_file_location(f"harness_plugin_node_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"spec de import vazia para {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wrap(name: str, fn):
    """Só `events` atravessa. O retorno de um nó é escrita no RunState; deixar
    um plugin escrever `decision` ou `verdict` seria deixá-lo votar no gate.
    """

    def _node(state, config=None) -> dict:
        out = fn(state, config)
        if not isinstance(out, dict):
            print(
                f"plugin_nodes: nó {name} devolveu {type(out).__name__}, "
                f"esperado dict — ignorado",
                file=sys.stderr,
            )
            return {}
        extra = sorted(k for k in out if k != EVENTS_KEY)
        if extra:
            print(
                f"plugin_nodes: nó {name} tentou escrever {extra} — descartado, "
                f"só {EVENTS_KEY!r} passa",
                file=sys.stderr,
            )
        return {EVENTS_KEY: out[EVENTS_KEY]} if EVENTS_KEY in out else {}

    return _node
