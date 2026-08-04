"""`diff_review`: a tool que deixa o modelo LER o que ele acabou de escrever.

O buraco medido é o oposto do da visão: o modelo sabe que os testes passaram e
sabe que a tarefa pedia uma coisa, mas não sabe o que ele mudou. Ele reformatou
um arquivo vizinho no meio do caminho, deixou um `print` de debug, criou um
`.bak` — e nada disso aparece no verify_cmd, que fica verde.

Aqui a fonte é o git do workspace, não a memória do modelo. `--numstat` dá o
tamanho da mudança por arquivo, e o que sai é ordenado por linhas mudadas: o
arquivo que mais mexeu é o que tem mais chance de ter mexido no que não devia.

Duas economias deliberadas. Diff inteiro de 50 arquivos não cabe em contexto e
não ajuda: entram as primeiras 40 linhas por arquivo, com teto de 4000 chars no
total. E binário nunca é despejado — vale o tamanho, o resto é lixo que queima
tokens e ainda quebra o parse do modelo.

`ws` sem git ou sem mudança nenhuma NÃO é erro: é sinal, e o sinal é "você
declarou pronto sem ter mudado nada".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

MAX_CHARS = 4000
MAX_LINHAS_ARQUIVO = 40
MAX_LINHAS_STAT = 12
GIT_TIMEOUT = 30.0
NADA = "nenhuma mudança detectada"


class Mudanca(NamedTuple):
    """Uma linha do `--numstat`. `binario` é o `-` que o git põe no lugar da contagem."""

    path: str
    add: int
    rem: int
    binario: bool

    @property
    def churn(self) -> int:
        return self.add + self.rem


def diff_review(ws: str | Path) -> str:
    """O que mudou no workspace, por arquivo, do maior para o menor.

    Erro é string de retorno, nunca exceção — igual ao resto dos backends.
    """
    base = Path(ws)
    if _git(base, ["rev-parse", "--is-inside-work-tree"])[0] != 0:
        return NADA  # sem git não há o que revisar; o modelo segue sem travar

    rev = ["HEAD"] if _git(base, ["rev-parse", "--verify", "HEAD"])[0] == 0 else []
    mudancas = _numstat(base, rev)
    novos = _untracked(base)
    if not mudancas and not novos:
        return NADA

    return _montar(base, rev, mudancas, novos)


def _montar(
    ws: Path,
    rev: list[str],
    mudancas: list[Mudanca],
    novos: list[tuple[str, int]],
) -> str:
    """Cabeçalho + `--stat` + diff por arquivo, gastando o orçamento de cima para baixo."""
    add = sum(m.add for m in mudancas)
    rem = sum(m.rem for m in mudancas)
    extra = f", {len(novos)} novo(s)" if novos else ""
    blocos = [
        f"diff_review: {len(mudancas)} arquivo(s) mudado(s) +{add}/-{rem}{extra}",
        _stat(ws, rev),
    ]
    if novos:
        blocos.append("novos (untracked): " + ", ".join(f"{p} ({_tam(n)})" for p, n in novos))

    gasto = sum(len(b) + 2 for b in blocos)
    mostrados = 0
    for mudanca in sorted(mudancas, key=lambda m: (-m.churn, m.path)):
        bloco = _bloco(ws, rev, mudanca)
        if gasto + len(bloco) + 2 > MAX_CHARS:
            break
        blocos.append(bloco)
        gasto += len(bloco) + 2
        mostrados += 1

    faltam = len(mudancas) - mostrados
    if faltam:
        blocos.append(
            f"[truncado no teto de {MAX_CHARS} chars: {faltam} arquivo(s) sem diff aqui — "
            "leia-os com read_file se o --stat acima parecer errado]"
        )
    return "\n\n".join(b for b in blocos if b)


def _bloco(ws: Path, rev: list[str], mudanca: Mudanca) -> str:
    """`--- path (+a/-r)` e as primeiras linhas do diff daquele arquivo."""
    if mudanca.binario:
        return f"--- {mudanca.path} (binário, {_bytes(ws / mudanca.path)} bytes)"
    cabeca = f"--- {mudanca.path} (+{mudanca.add}/-{mudanca.rem})"
    codigo, saida = _git(ws, ["diff", *rev, "--", mudanca.path])
    if codigo != 0 or not saida.strip():
        return cabeca
    linhas = saida.splitlines()[:MAX_LINHAS_ARQUIVO]
    corte = "" if len(saida.splitlines()) <= MAX_LINHAS_ARQUIVO else "\n[...]"
    return f"{cabeca}\n" + "\n".join(linhas) + corte


def _stat(ws: Path, rev: list[str]) -> str:
    """O histograma do `--stat`, cortado: 50 linhas dele comem o orçamento inteiro."""
    codigo, saida = _git(ws, ["diff", *rev, "--stat"])
    if codigo != 0 or not saida.strip():
        return ""
    linhas = [linha for linha in saida.splitlines() if linha.strip()]
    if len(linhas) <= MAX_LINHAS_STAT + 1:
        return "\n".join(linhas)
    # A última linha do --stat é o resumo; ela vale mais que o meio da lista.
    return "\n".join(
        [
            *linhas[:MAX_LINHAS_STAT],
            f"[... {len(linhas) - 1 - MAX_LINHAS_STAT} arquivo(s)]",
            linhas[-1],
        ]
    )


def _numstat(ws: Path, rev: list[str]) -> list[Mudanca]:
    codigo, saida = _git(ws, ["diff", *rev, "--numstat"])
    if codigo != 0:
        return []
    mudancas = []
    for linha in saida.splitlines():
        campos = linha.split("\t")
        if len(campos) < 3 or not campos[2]:
            continue
        add, rem = campos[0], campos[1]
        binario = add == "-" or rem == "-"
        mudancas.append(
            Mudanca(
                path=campos[2],
                add=0 if binario else int(add),
                rem=0 if binario else int(rem),
                binario=binario,
            )
        )
    return mudancas


def _untracked(ws: Path) -> list[tuple[str, int]]:
    """Arquivo novo com tamanho. `-uall` porque `?? pasta/` não diz nada ao modelo."""
    codigo, saida = _git(ws, ["status", "--porcelain", "-uall"])
    if codigo != 0:
        return []
    paths = [linha[3:].strip('"') for linha in saida.splitlines() if linha.startswith("??")]
    return sorted((p, _bytes(ws / p)) for p in paths)


def _git(ws: Path, args: list[str]) -> tuple[int, str]:
    """argv é DESTE módulo (git read-only, ws fixado), então não passa pela cerca do shell."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ws), *args],
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout


def _bytes(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _tam(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f}kb"


def make_review_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado."""
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def _diff_review() -> str:
        """Mostra o que você mudou no workspace, arquivo por arquivo."""
        try:
            return diff_review(base)
        except Exception as exc:  # tool node não pode receber exceção
            return f"diff_review falhou: {type(exc).__name__}: {exc}"

    return [
        StructuredTool.from_function(
            func=_diff_review,
            name="diff_review",
            description=(
                "Mostra o que VOCÊ mudou no workspace: contagem por arquivo e as "
                f"primeiras {MAX_LINHAS_ARQUIVO} linhas do diff de cada um, do arquivo "
                "que mais mudou para o que menos mudou. Sem argumentos. Chame ANTES de "
                "declarar pronto e confira que só mudou o que a tarefa pede: verify_cmd "
                "verde não prova que você não reformatou um vizinho, não deixou debug e "
                "não criou arquivo sobrando. Binário sai só como tamanho e arquivo novo "
                f"aparece na lista de untracked. '{NADA}' significa que você não mudou nada."
            ),
        ),
    ]


__all__ = ["MAX_CHARS", "MAX_LINHAS_ARQUIVO", "NADA", "diff_review", "make_review_tools"]
