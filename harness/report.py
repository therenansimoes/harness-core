"""Auto-relatório do loop pro humano: o que o harness fez desde ontem.

Accountability barata: nada aqui mede nada novo — só lê as fontes que já
existem (ledger `runs`/`mutations`, `skill_usage`, `data/lineage.jsonl`) e as
junta num markdown que caiba num olhar. Quem lê está fora do processo e quer
três respostas: rodou o quê, aprendeu o quê, e está esperando por mim?

Regra de ouro: o report NUNCA quebra. Fonte ausente, banco novo, jsonl torto,
tabela que não existe — cada seção é fail-open e vira "(sem dados)". Relatório
que falha por causa de um arquivo faltando não é lido nunca, e um loop que só
se explica quando tudo está no lugar não presta contas.

O `now` é injetável (ISO, como `store.now_iso`) porque janela de tempo sem
clock controlável não tem teste determinístico.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.ledger import store

DEFAULT_SINCE_HOURS = 24
NO_DATA = "(sem dados)"
TOP_SKILLS = 5
LINEAGE_TAIL = 10
# Vereditos que significam "o ciclo parou e chamou gente". Importar o
# vocabulário de `improve.escalate` aqui custaria um ciclo de import só para ler
# quatro strings; a lista de motivos vem do ledger, não deste módulo.
ABORTED = "ABORTED"


def _parse_ts(value: object) -> datetime | None:
    """ISO -> datetime aware, ou None quando não dá para ler.

    Naive (linha gravada por versão antiga) é assumido UTC: comparar aware com
    naive levanta, e derrubar o report por causa de um timestamp sem fuso seria
    exatamente o que a docstring do módulo promete não fazer.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _in_window(value: object, cutoff: datetime) -> bool:
    """Timestamp ilegível ENTRA na janela: omitir evidência é pior que mostrar
    uma linha velha demais, e linha sem ts legível é bug de quem gravou."""
    ts = _parse_ts(value)
    return True if ts is None else ts >= cutoff


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else NO_DATA
    return f"## {title}\n\n{body}"


# ------------------------------------------------------------------ seções


def _runs_section(cutoff: datetime, db: Path) -> list[str]:
    """Runs da janela: placar por backend e por kind, accept rate e custo."""
    if not db.is_file():
        return []
    try:
        rows = store.history(limit=100_000, path=db)
    except sqlite3.Error:
        return []
    rows = [r for r in rows if _in_window(r.created_at, cutoff)]
    if not rows:
        return []
    ok = sum(1 for r in rows if r.ok)
    usd = sum(r.cost_usd or 0.0 for r in rows)
    lines = [
        f"- runs={len(rows)} accept={ok}/{len(rows)} ({ok / len(rows):.0%}) "
        f"usd={usd:.4f}",
        "",
        "| backend | kind | runs | accept | usd |",
        "| --- | --- | --- | --- | --- |",
    ]
    tally: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        key = (r.backend, r.kind or "-")
        t = tally.setdefault(key, [0, 0, 0.0])
        t[0] += 1
        t[1] += int(bool(r.ok))
        t[2] += r.cost_usd or 0.0
    for (backend, kind), (n, acc, cost) in sorted(tally.items()):
        lines.append(
            f"| {backend} | {kind} | {n} | {acc}/{n} ({acc / n:.0%}) | {cost:.4f} |"
        )
    return lines


def _mutations_section(cutoff: datetime, db: Path) -> tuple[list[str], list]:
    """Placar KEEP/DISCARD por ação. Devolve também as mutações da janela para
    a seção de escalação não reabrir o banco."""
    if not db.is_file():
        return [], []
    try:
        muts = store.mutations(limit=None, path=db)
    except sqlite3.Error:
        return [], []
    muts = [m for m in muts if _in_window(m.applied_at, cutoff)]
    if not muts:
        return [], muts
    from harness.improve.policy import action_of

    tally: dict[str, list[int]] = {}
    for m in muts:
        # `action_of` cobre a era do note (`action=<nome>;...`) e a coluna nova;
        # mutação sem ação identificável cai em "(sem ação)" em vez de sumir.
        name = action_of(m) or "(sem ação)"
        t = tally.setdefault(name, [0, 0, 0])
        if m.verdict == "KEEP":
            t[0] += 1
        elif m.verdict == "DISCARD":
            t[1] += 1
        else:
            t[2] += 1
    lines = ["| ação | KEEP | DISCARD | outros |", "| --- | --- | --- | --- |"]
    for name in sorted(tally):
        keep, discard, other = tally[name]
        lines.append(f"| {name} | {keep} | {discard} | {other} |")
    return lines, muts


def _skills_section(db: Path) -> list[str]:
    """Top skills por lift (Wilson-low com menos sem). Sem janela de propósito:
    lift é evidência acumulada, e recortá-la em 24h mede ruído."""
    if not db.is_file():
        return []
    try:
        from harness.skills.attribution import lift

        with sqlite3.connect(db) as conn:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT skill FROM skill_usage ORDER BY skill"
                ).fetchall()
            ]
    except sqlite3.Error:  # banco sem `skill_usage`: ninguém injetou skill ainda
        return []
    scored: list[tuple[float, str, dict]] = []
    for name in names:
        try:
            d = lift(name, db_path=db)
        except sqlite3.Error:
            continue
        delta = d["wilson_low_with"] - d["wilson_low_without"]
        scored.append((delta, name, d))
    if not scored:
        return []
    scored.sort(key=lambda s: (-s[0], s[1]))
    lines = ["| skill | com | sem | lift |", "| --- | --- | --- | --- |"]
    for delta, name, d in scored[:TOP_SKILLS]:
        (w_s, w_t), (wo_s, wo_t) = d["with"], d["without"]
        # Braço vazio sai como traço: número sobre zero trial é invenção.
        shown = "-" if w_t == 0 or wo_t == 0 else f"{delta:+.2f}"
        lines.append(f"| {name} | {w_s}/{w_t} | {wo_s}/{wo_t} | {shown} |")
    return lines


def _lineage_section(db: Path, lineage_file: Path | str | None) -> list[str]:
    """Últimas mutações de código com o veredito anexado pelo ledger."""
    try:
        from harness.improve import lineage as lin

        entries = lin.load_lineage(lineage_file)
        if not entries:
            return []
        lin.enrich(entries, db_path=db)
    except (OSError, sqlite3.Error):
        return []
    lines = []
    for e in entries[-LINEAGE_TAIL:]:
        verdict = e.get("verdict") or "?"
        lines.append(f"- {e['id'][:8]} {e['target']} [{verdict}] {e['ts']}")
    return lines


def _escalations_section(muts: list) -> list[str]:
    """Escalações da janela: linha ABORTED do ledger com o motivo no `note`.

    O ledger não sabe se o humano já respondeu (a resposta vive no
    checkpointer do grafo), então o título é honesto: é o que PAROU esperando
    gente, não uma fila garantidamente aberta.
    """
    lines = [
        f"- {m.applied_at} {m.rule_id} motivo={m.note or '?'}"
        for m in muts
        if m.verdict == ABORTED
    ]
    return lines


# ------------------------------------------------------------------ público


def build_report(
    since_hours: float = DEFAULT_SINCE_HOURS,
    db_path: Path | str | None = None,
    lineage_file: Path | str | None = None,
    now: str | datetime | None = None,
) -> str:
    """Markdown do que o loop fez nas últimas `since_hours`. Nunca levanta."""
    ref = now if isinstance(now, datetime) else _parse_ts(now)
    if ref is None:
        ref = datetime.now(timezone.utc)
    cutoff = ref - timedelta(hours=since_hours)
    db = Path(db_path) if db_path is not None else store.db_path()

    mut_lines, muts = _mutations_section(cutoff, db)
    parts = [
        f"# harness report\n\n"
        f"- janela: últimas {since_hours:g}h (desde "
        f"{cutoff.isoformat(timespec='seconds')})\n"
        f"- gerado em: {ref.isoformat(timespec='seconds')}\n"
        f"- ledger: {db}",
        _section("Runs", _runs_section(cutoff, db)),
        _section("Mutações por ação", mut_lines),
        _section("Skills por lift", _skills_section(db)),
        _section("Linhagem (últimas)", _lineage_section(db, lineage_file)),
        _section("Escalações", _escalations_section(muts)),
    ]
    return "\n\n".join(parts) + "\n"
