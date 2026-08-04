"""Reflect: o checker do caminho de retry (padrão worker/checker).

O executor é o worker; este módulo é o cético que olha o cadáver da tentativa
reprovada e escreve, em texto literal, o que a régua cobrou e o worker não
entregou. Determinístico e $0 — nada de LLM aqui: o hint vai para o prompt da
tentativa seguinte e quem o lê é um modelo pequeno, que precisa de lista de
arquivos, não de prosa.

Fail-open em tudo: material insuficiente => hint vazio => retry como antes.

O hint é ESTRUTURAL por segurança: nada do texto do log do verify entra nele.
Ver `build_hint`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.ledger import store

# Cabeçalho do bloco injetado no prompt (run_graph._prompt). Fixo porque é
# contrato de leitura do executor, não formatação.
HINT_HEADER = "## Feedback da tentativa anterior"

# Token com extensão é o que a régua cobra na prática (`test -f ok.txt`,
# `grep -q css dist/app.css`). Sem esta extração o hint devolveria o comando
# cru, que o worker já tinha e não usou.
_PATHISH = re.compile(r"[\w./*@-]*\w\.[A-Za-z]\w*")
_GREP = re.compile(r"grep\s+(?:-\S+\s+)*(?:'([^']*)'|\"([^\"]*)\")")
# Teto de itens por linha: hint longo é hint ignorado.
MAX_ITEMS = 6


@dataclass(frozen=True)
class _ExecView:
    """Só o que o hint usa do ExecResult da tentativa anterior."""

    files_changed: tuple[str, ...]


def _verify_fail(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Evento do verify que reprovou, ou None. `retry` zera o `verdict`, mas a
    lista de eventos é aditiva no estado — o evento sobrevive à fronteira da
    tentativa.

    O `tail` dele serve de SINAL ("houve reprovação com log"), nunca de texto:
    quem escreveu aquele log foi o verificador selado, que pode imprimir o
    gabarito. Só o `exit_code` sai daqui como conteúdo.
    """
    for ev in reversed(list(state.get("events") or ())):
        if isinstance(ev, Mapping) and ev.get("node") == "verify" and ev.get("tail"):
            return ev
    return None


def _exit_code(ev: Mapping[str, Any] | None) -> int | None:
    try:
        return int(ev["exit_code"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def _failed_checks(ev: Mapping[str, Any] | None) -> tuple[list[str], float] | None:
    """`(nomes reprovados, score)` da régua graduada, ou None quando não há.

    SÓ NOMES entram no hint: o nome do check vem do `unit.toml`, que o agente já
    lê, mas a saída dele foi para o mesmo log selado do verify — citar o texto
    reintroduziria o vazamento do gabarito pelo prompt (ver `build_hint`).
    """
    if not isinstance(ev, Mapping):
        return None
    names = [str(n) for n in (ev.get("failed") or ())][:MAX_ITEMS]
    if not names:
        return None
    try:
        score = float(ev.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return names, score


def _changed(state: Mapping[str, Any]) -> list[str]:
    return [str(f) for f in (getattr(state.get("exec"), "files_changed", None) or ())]


def _required(verify_cmd: str) -> tuple[list[str], list[str]]:
    """(arquivos, padrões de grep) citados pelo `verify_cmd`, sem duplicata."""
    files = list(dict.fromkeys(_PATHISH.findall(verify_cmd)))[:MAX_ITEMS]
    pats = [a or b for a, b in _GREP.findall(verify_cmd)]
    return files, list(dict.fromkeys(p for p in pats if p))[:MAX_ITEMS]


def _touched(path: str, changed: list[str]) -> bool:
    """`files_changed` vem relativo ao workspace e o `verify_cmd` pode citar o
    mesmo arquivo por outro prefixo — comparar também pelo nome evita acusar
    de "não alterado" um arquivo que o worker escreveu."""
    base = path.rsplit("/", 1)[-1]
    return any(c == path or c.endswith("/" + path) or c.rsplit("/", 1)[-1] == base for c in changed)


def build_hint(state: Mapping[str, Any]) -> str:
    """Hint direcionado para a próxima tentativa, ou "" se não há o que dizer.

    ESTRUTURAL por segurança: nenhuma linha do log do verify é citada. O log é
    saída do verificador SELADO, que pode imprimir o gabarito
    (`esperado=<golden>`); como o hint é colado no prompt da tentativa seguinte,
    citar o tail reintroduziria pelo prompt o vazamento que tirar o log do
    workspace fechou. O `verify_cmd` é outra história: ele vive no `unit.toml`
    que o agente já lê, então arquivos/padrões extraídos dele não são segredo.

    Material usado: `exit_code` do verify, `files_changed` do exec e o
    `verify_cmd` da unidade. Nenhuma exceção sai daqui.
    """
    try:
        verify_cmd = str(getattr(state.get("unit"), "verify_cmd", "") or "")
        fail = _verify_fail(state)
        changed = _changed(state)
        if fail is None and not changed:
            return ""
        files, pats = _required(verify_cmd)
        lines = []
        code = _exit_code(fail)
        if code is not None:
            lines.append(f"A régua reprovou (exit {code}); o log não é seu.")
        lines.append("Você alterou: " + (", ".join(changed[:MAX_ITEMS]) if changed else "nada"))
        graded = _failed_checks(fail)
        if graded:
            names, score = graded
            lines.append(f"checks reprovados: {', '.join(names)} (score {score:.2f})")
        if files:
            lines.append("Checks do verify_cmd referenciam: " + ", ".join(files))
        if pats:
            lines.append("Padrões exigidos no conteúdo: " + ", ".join(pats))
        missing = [f for f in files if not _touched(f, changed)]
        if missing:
            lines.append("Arquivos exigidos e NÃO alterados: " + ", ".join(missing))
        return "\n".join(lines)
    except Exception:
        return ""


def hydrate(state: Mapping[str, Any], db: Path | None = None) -> Mapping[str, Any]:
    """Vista do estado com o material da tentativa que reprovou.

    `retry` zera `exec`/`verdict` antes de reflect rodar (é o que impede um nó
    de confundir tentativa velha com corrente), então o que falta vem do
    ledger — a fonte que sobrevive a SIGKILL e a retomada. Nunca levanta.
    """
    view = dict(state)
    prev = int(view.get("attempt") or 0) - 1
    if prev < 0:
        return view
    run_id = str(view.get("run_id") or "")
    try:
        if view.get("exec") is None:
            saved = store.get_node(run_id, "execute", db, attempt=prev)
            if saved:
                view["exec"] = _ExecView(tuple(saved.get("files_changed") or ()))
        if _verify_fail(view) is None:
            saved = store.get_node(run_id, "verify", db, attempt=prev)
            if saved and saved.get("tail"):
                view["events"] = [
                    *list(view.get("events") or ()),
                    {
                        "node": "verify",
                        "tail": saved["tail"],
                        "exit_code": saved.get("exit_code"),
                        "score": saved.get("score"),
                        "failed": saved.get("failed") or (),
                    },
                ]
    except Exception:
        return dict(state)
    return view
