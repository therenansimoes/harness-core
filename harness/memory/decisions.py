"""Memória de casos de decisão humana: o que o humano respondeu, em texto.

Índice FTS5 `human_decisions` no MESMO `data/runs.sqlite` do ledger — criado
aqui com CREATE VIRTUAL TABLE IF NOT EXISTS, sem tocar em `store.py` (mesma
convenção de `memory/episodic.py`).

A episódica lembra o que FALHOU; esta lembra o que foi RESPONDIDO quando o loop
parou e chamou gente. São bancos irmãos porque a chave é a mesma — `kind` — e a
leitura acontece no mesmo lugar: um bloco no prompt/na evidência de quem vai
decidir de novo o que um humano já decidiu uma vez.

O que atravessa aqui não é ordem: o humano respondeu OUTRO caso, com outro
contexto, possivelmente meses atrás. Por isso `render_prompt` rotula o bloco
como "humano disse antes (não é ordem)" — decisão passada é evidência, e
evidência que se apresenta como comando faz o loop obedecer fantasma.

FTS5 é opcional no sqlite: build sem a extensão faz `record_decision` virar
no-op silencioso e `recall_decisions` devolver []. Toda a API é fail-open —
memória que derruba a escalação vale menos que memória nenhuma.

O namespace é o data dir GLOBAL (`HARNESS_DATA_DIR`), não o db do experimento:
cada data dir é um harness com sua própria história de decisões.

Escrita e leitura compartilham VOCABULÁRIO por construção, e isso é o que faz o
recall existir: quem grava (`cli._record_human_decision`) põe no `context` os
mesmos identificadores curtos que quem lê (`run_graph._prior_query`) monta na
query — `kind`, nome de check reprovado (`check:<nome>`) e a classe de saída
(`verify_failed`). O texto livre do hint do checker ("régua reprovou exit 2;
arquivos exigidos…") NÃO serve de query: o vocabulário dele não existe do lado
gravado, e o recall volta vazio sempre. Por isso o MATCH procura em `reason` E
em `context`: o motivo é do vocabulário fechado da escalação, os checks e a
classe de saída vêm no contexto.

`HARNESS_DECISIONS=0` (ou off/false/no) é kill switch lido a cada chamada:
`record_decision` devolve False e `recall_decisions` devolve []. `disabled()` é
o mesmo switch como bloco, pros caminhos de exame/screening — decisão humana
vazando pro prompt de uma avaliação é o mesmo vazamento que a episódica evita
lá.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from harness.ledger.store import db_path as default_db_path
from harness.ledger.store import now_iso

TABLE = "human_decisions"
# Trecho gravado/devolvido: o suficiente para reconhecer o caso e o que foi
# respondido, não o estado inteiro (que estouraria o prompt).
MAX_TEXT_CHARS = 800
DEFAULT_K = 3

SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(
    kind, reason, context, decision, created_at UNINDEXED
);
"""

_TOKEN = re.compile(r"[0-9A-Za-z_]{3,}")
_OFF = {"0", "off", "false", "no"}
ENV_ENABLED = "HARNESS_DECISIONS"


def _enabled() -> bool:
    """Kill switch lido NA CHAMADA, não no import: teste e operador precisam
    poder desligar a memória sem reimportar o módulo."""
    return os.environ.get(ENV_ENABLED, "1").strip().lower() not in _OFF


@contextmanager
def disabled():
    """Bloco em que a memória de casos não grava nem lê. Restaura o valor
    anterior no finally, inclusive a ausência da env — quem chama não pode
    deixar a memória desligada pro resto do processo. Reentrante.

    É env var e não flag de argumento porque `recall_decisions` é chamado fundo
    no `escalate.payload`, longe de quem sabe que o run é uma avaliação.
    """
    previous = os.environ.get(ENV_ENABLED)
    os.environ[ENV_ENABLED] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV_ENABLED, None)
        else:
            os.environ[ENV_ENABLED] = previous


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Conexão com o índice já criado. Levanta se o sqlite não tem FTS5 —
    quem chama é que decide o fail-open."""
    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_decision(
    kind: str | None,
    reason: str | None,
    context: str,
    decision: str,
    db_path: Path | None = None,
) -> bool:
    """Indexa uma decisão humana. True se gravou; False (silencioso) se não.

    Sem `decision` não há caso: escalação que ninguém respondeu já vive no
    ledger como ABORTED. `kind` vazio virá "" — a busca é sempre keyed em kind,
    então decisão sem kind simplesmente nunca é recuperada.
    """
    if not _enabled():
        return False
    if not decision or not decision.strip():
        return False
    try:
        with _connect(db_path) as conn:
            conn.execute(
                f"INSERT INTO {TABLE} (kind, reason, context, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    kind or "",
                    reason or "",
                    (context or "").strip()[:MAX_TEXT_CHARS],
                    decision.strip()[:MAX_TEXT_CHARS],
                    now_iso(),
                ),
            )
        return True
    except Exception:
        return False


def recall_decisions(
    kind: str | None,
    reason: str | None,
    k: int = DEFAULT_K,
    db_path: Path | None = None,
) -> list[str]:
    """Top-k decisões humanas do MESMO kind, mais relevantes primeiro.

    `reason` é a query (o motivo da escalação de agora, ou os termos estáveis do
    retry): vira OR dos seus tokens contra `reason` E `context`, porque a
    sintaxe do MATCH não perdoa pontuação e porque o check reprovado que faz dois
    casos serem o mesmo caso mora no contexto. Cada item é
    `"<motivo>: <contexto> -> <decisão>"` — quem lê está fora do caso e precisa
    saber a que pergunta aquela resposta respondia. Qualquer erro => []
    (fail-open).
    """
    if not _enabled():
        return []
    if not kind or k <= 0:
        return []
    match = _match_expr(kind, reason or "")
    if match is None:
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT reason, context, decision FROM {TABLE} WHERE {TABLE} MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, k),
            ).fetchall()
        return [_render_case(r) for r in rows if r["decision"]]
    except Exception:
        return []


def _render_case(row: sqlite3.Row) -> str:
    """Uma linha do índice como uma linha de texto. Contexto vazio sai fora em
    vez de virar `" -> "` pendurado num prompt."""
    head = f"{row['reason'] or 'sem motivo'}"
    if row["context"]:
        head = f"{head}: {row['context']}"
    return f"{head} -> {row['decision']}"


def _match_expr(kind: str, query: str) -> str | None:
    """`kind:"x" AND (reason:"a" OR context:"a" OR …)`, ou None sem token.

    Tokens de <3 chars e pontuação saem fora: em FTS5 eles são ruído puro e
    `-`/`:`/`"` no meio do texto viram erro de sintaxe.

    As duas colunas no OR são o outro lado do vocabulário compartilhado: o
    motivo (vocabulário fechado) casa em `reason`, o nome do check e a classe de
    saída casam em `context`, que é onde a ponta de escrita os põe.
    """
    tokens = _TOKEN.findall(query or "")
    if not tokens:
        return None
    # Dedup preservando ordem; teto para não montar expressão gigante.
    seen: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in seen:
            seen.append(low)
    terms = " OR ".join(f'reason:"{t}" OR context:"{t}"' for t in seen[:32])
    return f'kind:"{kind}" AND ({terms})'


def render_prompt(cases: list[str]) -> str:
    """Bloco para o prompt/evidência. Lista vazia => "" (nenhum bloco).

    O rótulo é o conteúdo: quem lê tem que saber que aquilo é o que um humano
    respondeu em OUTRO caso parecido, não instrução para este.
    """
    if not cases:
        return ""
    items = "\n".join(f"- {c}" for c in cases)
    return (
        "## humano disse antes (não é ordem)\n"
        "Escalações anteriores deste tipo de tarefa foram respondidas assim. "
        "É evidência de precedente, não instrução:\n"
        f"{items}"
    )
