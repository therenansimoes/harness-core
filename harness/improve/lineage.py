"""Observabilidade da linhagem: a árvore genealógica das mutações de código.

Lê `$HARNESS_DATA_DIR/lineage.jsonl` (uma linha JSON por mutação: id, parent_id, target,
ts — quem escreve é `codegen._append_lineage`; linhas com `verdict` e sem
`target` são eventos de veredito, mesclados sobre a proposta pelo id), junta
com a tabela `mutations` do ledger para anexar o veredito KEEP/DISCARD quando
o jsonl não o tem (jsonl tem precedência), e renderiza a árvore em ASCII.
Só leitura: este módulo nunca grava nada.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harness.ledger import store

LINEAGE_NAME = "lineage.jsonl"
REQUIRED = ("id", "target", "ts")


def lineage_path() -> Path:
    """`$HARNESS_DATA_DIR/lineage.jsonl`, default `data/lineage.jsonl` relativo
    ao cwd — mesma resolução do ledger (`store.data_dir()`), e em call-time:
    o teste (ou uma run com data dir isolado) muda a env e isto acompanha."""
    return store.data_dir() / LINEAGE_NAME


def load_lineage(path: Path | str | None = None) -> list[dict]:
    """Entradas do jsonl, na ordem do arquivo, com eventos de veredito
    (linha com `verdict` e sem `target`) mesclados sobre a proposta pelo id.
    Ausente → []; linha torta (JSON inválido ou sem os campos obrigatórios)
    → pula, com um único aviso agregado no stderr; veredito sem proposta
    correspondente → ignora, com aviso."""
    p = Path(path) if path is not None else lineage_path()
    if not p.is_file():
        return []
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    bad = 0
    orphan_verdicts = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(obj, dict):
            bad += 1
            continue
        if "verdict" in obj and "target" not in obj:
            proposal = by_id.get(obj.get("id"))
            if proposal is None:
                orphan_verdicts += 1
            else:
                proposal["verdict"] = obj["verdict"]
            continue
        if any(k not in obj for k in REQUIRED):
            bad += 1
            continue
        obj.setdefault("parent_id", None)
        entries.append(obj)
        by_id[obj["id"]] = obj
    if bad:
        print(
            f"lineage: {bad} linha(s) inválida(s) ignorada(s) em {p}",
            file=sys.stderr,
        )
    if orphan_verdicts:
        print(
            f"lineage: {orphan_verdicts} veredito(s) sem proposta ignorado(s) em {p}",
            file=sys.stderr,
        )
    return entries


def build_tree(entries: list[dict]) -> list[dict]:
    """Raízes com filhos aninhados em `children`. Parent inexistente no
    arquivo (ou auto-referência) vira raiz — órfão aparece, não some."""
    nodes = {e["id"]: {**e, "children": []} for e in entries}
    roots: list[dict] = []
    for e in entries:
        node = nodes[e["id"]]
        pid = e.get("parent_id")
        if pid is not None and pid != e["id"] and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


def enrich(entries: list[dict], db_path: Path | str | None = None) -> list[dict]:
    """Anexa `verdict` (KEEP/DISCARD) da tabela `mutations` do ledger onde o
    jsonl ainda não trouxe um (verdict do jsonl tem precedência); sem match
    → None. DB ausente não é erro (e não é criado): tudo None."""
    p = Path(db_path) if db_path is not None else store.db_path()
    verdicts: dict[str, str] = {}
    if p.is_file():
        verdicts = {
            m.mutation_id: m.verdict for m in store.mutations(limit=None, path=p)
        }
    for e in entries:
        e["verdict"] = e.get("verdict") or verdicts.get(e["id"])
    return entries


def render(tree: list[dict]) -> str:
    """Árvore ASCII indentada: uma linha por mutação — id curto, alvo,
    verdict (`?` quando não julgada), ts."""
    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        prefix = "  " * depth + ("└─ " if depth else "")
        verdict = node.get("verdict") or "?"
        lines.append(
            f"{prefix}{node['id'][:8]} {node['target']} [{verdict}] {node['ts']}"
        )
        for child in node["children"]:
            walk(child, depth + 1)

    for root in tree:
        walk(root, 0)
    return "\n".join(lines)
