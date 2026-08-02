#!/usr/bin/env python3
"""autopilot.py — o loop que roda sozinho. stdlib only.

Um loop, dois tipos de passo, um orçamento:

    step_project   consome 1 unidade da fila de um projeto (project.try_run_one)
    step_self      olha as falhas recentes, consulta evolution/catalog.toml,
                   gera UMA proposta e roda evolve.cycle nela

Não há execução nova aqui: tudo é orquestração sobre APIs prontas
(`project.try_run_one`, `evolve.cycle`, `score.kpi_report`, `graph`). O que é
novo — e é o ponto do módulo — são os tetos: parede, dinheiro, iterações, e o
probation que REVERTE um merge que piorou o projeto.

Tetos (mecanismo, não pedido educado ao processo):
    - `signal.setitimer` levanta `Deadline` na parede, esteja onde estiver;
    - `HARNESS_TIMEOUT` é reescrito ANTES de cada passo com o tempo que resta,
      para que o filho não sobreviva ao pai;
    - `spent_usd()` soma o `cost_usd` das linhas NOVAS de todos os results.tsv;
    - escrita confinada: TMPDIR e workspaces vão para dentro da raiz do repo, e
      o loop se recusa a iniciar (exit 2) se um projeto aponta work_path para
      fora dela.

Exit: 0 = fila vazia / iterações / sinal · 2 = recusa iniciar · 3 = deadline
· 4 = budget.

    python3 autopilot.py --minutes 20 --project demo --self-every 3 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import evolve  # noqa: E402
import graph  # noqa: E402
import score  # noqa: E402


def _project():
    """`project.py` lê PROJECTS_ROOT/WS_ROOT do ambiente NO IMPORT. Importar
    cedo congelaria o env de quem quer isolar por env (confine() aqui, e os
    testes que apontam para tmp) — por isso o import é tardio, não por estilo.
    """
    import project

    return project

CATALOG = ROOT / "evolution" / "catalog.toml"
DECISIONS = ROOT / "evolution" / "decisions"
LOG_DIR = ROOT / "evolution" / "autopilot"

# Repetições da suite por ciclo de auto-evolução. 1 porque o ciclo já roda
# dentro de um orçamento compartilhado com a fila: quem quer mais N roda
# evolve.py à mão, com o dinheiro na frente.
SELF_REPEAT = int(os.environ.get("HARNESS_AP_REPEAT", "1"))


class Deadline(Exception):
    """Parede estourou — não é erro, é o teto funcionando."""


class Stop(Exception):
    """SIGTERM/SIGINT: alguém pediu para parar. Termina o passo e sai limpo."""


class SkipProposal(Exception):
    """Não há proposta legítima a gerar (âncora sumiu, valor já no teto...)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------ catálogo


def load_catalog(path: Path | None = None) -> list[dict]:
    """As regras na ORDEM do arquivo — a ordem é o desempate, não decoração."""
    p = path or CATALOG
    if not p.exists():
        raise SkipProposal(f"catalog.toml não existe: {p}")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    rules = data.get("rule") or []
    if not isinstance(rules, list):
        raise SkipProposal(f"{p.name}: 'rule' precisa ser uma lista de tabelas")
    return rules


def classify(notes: str, catalog: list[dict] | None = None) -> str:
    """`code` da primeira regra cujo match_notes casa; "" se nenhuma."""
    rules = catalog if catalog is not None else load_catalog()
    text = notes or ""
    for r in rules:
        pat = r.get("match_notes")
        if pat and re.search(pat, text, re.M):
            return r.get("code", "")
    return ""


def trace_signals(trace_path, patterns: list[str]) -> Counter:
    """Conta ocorrências de cada regex nas linhas do trace.jsonl.

    Leitura pura: nenhum LLM, nenhuma rede. Trace ausente/ilegível vale zero —
    a ausência de evidência não pode virar evidência de sintoma."""
    counts: Counter = Counter()
    p = Path(trace_path)
    if not p.exists():
        return counts
    compiled = [(pat, re.compile(pat)) for pat in patterns]
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for pat, rx in compiled:
                    if rx.search(line):
                        counts[pat] += 1
    except OSError:
        return Counter()
    return counts


def _trace_path(row: dict, root: Path | None = None) -> Path | None:
    """`trace:<path>` sai na coluna notes (agent.py). Relativo à raiz."""
    m = re.search(r"trace:(\S+)", row.get("notes", "") or "")
    if not m:
        return None
    p = Path(m.group(1))
    return p if p.is_absolute() else (root or ROOT) / p


def _rule_by_code(code: str, catalog: list[dict]) -> dict | None:
    for r in catalog:
        if r.get("code") == code:
            return r
    return None


def error_counts(rows: list[dict], catalog: list[dict] | None = None,
                 root: Path | None = None, window: int | None = None) -> list[tuple[str, int]]:
    """[(code, n)] das falhas recentes, ordenado por contagem e, no empate,
    pela ordem do catálogo."""
    rules = catalog if catalog is not None else load_catalog()
    win = window if window is not None else default_window()
    fails = [r for r in rows if str(r.get("success", "")).strip() in ("0", "False", "false")]
    fails = fails[-win:]

    order = {r.get("code"): i for i, r in enumerate(rules)}
    counts: Counter = Counter()
    for row in fails:
        code = classify(row.get("notes", ""), rules)
        if not code:
            continue
        rule = _rule_by_code(code, rules)
        trace_any = (rule or {}).get("trace_any")
        if trace_any:
            # trace_any só REFINA: sem confirmação no trace, a run não conta
            # para essa regra (e não é reclassificada — a nota já casou).
            tp = _trace_path(row, root)
            if tp is None or not sum(trace_signals(tp, list(trace_any)).values()):
                continue
        counts[code] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 999)))


def dominant_error(rows: list[dict], catalog: list[dict] | None = None,
                   root: Path | None = None, window: int | None = None) -> tuple[str, int]:
    ranked = error_counts(rows, catalog, root, window)
    return ranked[0] if ranked else ("", 0)


def default_window() -> int:
    """2*probation_runs+4: grande o bastante para as duas janelas do probation
    caberem, pequeno o bastante para o sinal ser RECENTE."""
    runs = int(config.load()["harness"]["autopilot_probation_runs"])
    return 2 * runs + 4


# ------------------------------------------------------------------ proposta


def _unique(src: str, old: str, where: str) -> None:
    n = src.count(old)
    if n != 1:
        raise SkipProposal(f"âncora aparece {n}x em {where} — precisa ser única")


def _next_auto_n(proposals: Path) -> int:
    return sum(1 for p in proposals.glob("auto-*.md")) + 1 if proposals.is_dir() else 1


def render_proposal(rule: dict, root: Path | None = None) -> Path:
    """Gera `evolution/proposals/auto-<code>-<ts>.md` no formato do _template.

    Lê o valor ATUAL da âncora e exige `old` único — a mesma regra do
    `evolve.apply_change`, checada aqui para falhar antes de gastar uma suite.
    """
    base = root or ROOT
    code = rule["code"]
    target_rel = rule.get("file", "agent.py")
    target = base / target_rel
    if not target.exists():
        raise SkipProposal(f"alvo não existe: {target_rel}")
    src = target.read_text(encoding="utf-8")
    kind, anchor = rule["kind"], rule["anchor"]

    if kind == "bump_int":
        m = re.search(rf"^{re.escape(anchor)} = (\d+)$", src, re.M)
        if not m:
            raise SkipProposal(f"âncora {anchor} (int) não encontrada em {target_rel}")
        cur = int(m.group(1))
        new_val = min(int(round(cur * float(rule["factor"]))), int(rule["max_value"]))
        if new_val <= cur:
            raise SkipProposal(f"{anchor} já está em {cur} (teto {rule['max_value']})")
        old = f"{anchor} = {cur}\n"
        new = f"{anchor} = {new_val}\n"
        change_desc = f"{anchor} {cur} -> {new_val}"
    elif kind == "append_prompt":
        text = rule["text"]
        old = anchor + "\n"
        # Apêndice ao prompt sem editar o interior da string: uma linha logo
        # acima da sentinela. A sentinela continua única, então a próxima
        # proposta ainda encontra a âncora.
        new = f"SYSTEM_PROMPT += {json.dumps(text + chr(10))}\n" + old
        change_desc = f"append_prompt: {text[:60]}"
    else:
        raise SkipProposal(f"kind desconhecido no catálogo: {kind}")

    _unique(src, old, target_rel)
    for s in (old, new):
        if "'''" in s:
            raise SkipProposal("texto da mudança contém ''' — quebraria o front matter")

    proposals = base / "evolution" / "proposals"
    cur_version = (base / "harness_version.txt").read_text().strip()
    to_version = f"{cur_version}+auto{_next_auto_n(proposals)}"
    pid = f"auto-{code}-{_stamp()}"
    hypothesis = rule.get("hypothesis") or f"Mutação automática para {code}."

    doc = f"""+++
id = {json.dumps(pid)}

from_version = {json.dumps(cur_version)}
to_version = {json.dumps(to_version)}

hypothesis = {json.dumps(hypothesis)}

[change]
file = {json.dumps(target_rel)}
old = '''
{old}'''
new = '''
{new}'''
+++

# Proposta automática: {code}

## Por que

Gerada por `autopilot.py` a partir da regra `{code}` de `evolution/catalog.toml`:
o sintoma dominante nas falhas recentes do results.tsv casou `{rule.get('match_notes')}`.
Nenhum LLM participou desta escolha — a regra é uma tabela e a ordem dela é o
desempate.

## Predição

{hypothesis}

## Falsificação

Os gates de `score.ab_report` sobre a suite fixed. Se success cair, truncamento
subir ou não houver ganho normalizado, o ciclo termina em DISCARD e o baseline
`{cur_version}` continua intacto. Se merge passar, o autopilot ainda observa
`autopilot_probation_runs` runs de projeto e REVERTE se piorarem.

## Mudança

{change_desc}
"""
    proposals.mkdir(parents=True, exist_ok=True)
    out = proposals / f"{pid}.md"
    out.write_text(doc, encoding="utf-8")
    return out


# ------------------------------------------------------------------ snapshot


def snapshot_genome(session: str, root: Path | None = None) -> Path:
    """Cópia byte a byte do genoma + versão. NÃO usa git: o repo pode estar
    sujo, e escrever no índice de outro processo é pior do que copiar arquivo.
    """
    base = root or ROOT
    dest = base / "evolution" / "rollbacks" / session / _stamp()
    dest.mkdir(parents=True, exist_ok=True)
    for rel in evolve.genome_files(root=base) + ["harness_version.txt"]:
        src = base / rel
        if not src.exists():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
    return dest


def restore_genome(snap: Path, root: Path | None = None) -> list[str]:
    """Devolve o snapshot ao lugar. Retorna os paths relativos restaurados."""
    base = root or ROOT
    restored = []
    for p in sorted(Path(snap).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(snap).as_posix()
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, base / rel)
        restored.append(rel)
    return restored


# --------------------------------------------------------------------- state


@dataclass
class State:
    session: str
    project: str | None
    wall_s: int
    budget: float
    max_iterations: int
    self_every: int
    probation_runs: int
    started: float = field(default_factory=time.time)
    baseline_lines: dict = field(default_factory=dict)
    probation: dict | None = None
    blocked_codes: set = field(default_factory=set)
    iterations: int = 0
    steps_since_self: int = 0
    ran: int = 0
    selfs: int = 0
    reverts: int = 0
    stop_reason: str = ""

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.session}.jsonl"

    def elapsed(self) -> float:
        return time.time() - self.started

    def remaining(self) -> float:
        return max(0.0, self.wall_s - self.elapsed())


def results_files() -> list[Path]:
    project = _project()
    out = [ROOT / "results.tsv"]
    if project.PROJECTS_ROOT.is_dir():
        out += sorted(project.PROJECTS_ROOT.glob("*/results.tsv"))
    return out


def _lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    return [l for l in p.read_text(errors="replace").splitlines() if l.strip()]


def baseline_lines() -> dict:
    return {str(p): len(_lines(p)) for p in results_files()}


def spent_usd(s: State) -> float:
    """Custo das linhas NOVAS desde o start — o gasto DESTA sessão, não o
    histórico do arquivo."""
    total = 0.0
    for p in results_files():
        lines = _lines(p)
        if not lines:
            continue
        header = lines[0].split("\t")
        try:
            idx = header.index("cost_usd")
        except ValueError:
            continue
        start = max(1, s.baseline_lines.get(str(p), 1))
        for ln in lines[start:]:
            cells = ln.split("\t")
            if len(cells) > idx:
                try:
                    total += float(cells[idx] or 0)
                except ValueError:
                    pass
    return total


def project_rows(name: str) -> list[dict]:
    """As linhas do results.tsv DO PROJETO, na ordem do arquivo."""
    p = _project().PROJECTS_ROOT / name / "results.tsv"
    lines = _lines(p)
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = (ln.split("\t") + [""] * len(header))[: len(header)]
        rows.append(dict(zip(header, cells)))
    return rows


def log_event(s: State, **ev) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now(), "session": s.session, "iteration": s.iterations,
           "elapsed_s": round(s.elapsed(), 1), "cost_usd": round(spent_usd(s), 4)}
    row.update(ev)
    with s.log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------- passos


def step_project(s: State) -> str:
    project = _project()
    name = s.project or project.pick_project()
    if not name:
        return "empty"
    result = project.try_run_one(name, keep=False)
    if result == "ran":
        s.ran += 1
    log_event(s, kind="project", result=result, code="", decision=name,
              **{k: v for k, v in (project.LAST_RUN or {}).items()})
    return result


def _pick_rule(s: State, rows: list[dict], catalog: list[dict]) -> tuple[dict | None, int]:
    for code, n in error_counts(rows, catalog):
        if code in s.blocked_codes:
            continue
        rule = _rule_by_code(code, catalog)
        if not rule or rule.get("action") != "propose":
            continue
        return rule, n
    return None, 0


def step_self(s: State) -> str:
    name = s.project or _project().pick_project()
    rows = project_rows(name) if name else []
    try:
        catalog = load_catalog()
    except SkipProposal as e:
        log_event(s, kind="self", result="skip", code="", decision=str(e))
        return "skip"

    rule, n = _pick_rule(s, rows, catalog)
    if rule is None:
        log_event(s, kind="self", result="no_signal", code="", decision="nenhuma regra propose aplicável")
        return "no_signal"

    code = rule["code"]
    try:
        path = render_proposal(rule)
    except SkipProposal as e:
        log_event(s, kind="self", result="skip", code=code, decision=str(e))
        return "skip"

    snap = snapshot_genome(s.session)
    s.selfs += 1
    try:
        rc = evolve.cycle(path, repeat=SELF_REPEAT, suite="fixed", force=False)
    except (Deadline, Stop):
        raise
    except (evolve.InfraError, SystemExit, Exception) as e:  # noqa: BLE001
        # Um ciclo que explode é dado de infra, não veredito: o loop segue e o
        # baseline não foi tocado (promote só roda no fim de cycle).
        log_event(s, kind="self", result="infra_error", code=code, decision=str(e)[:200])
        return "error"

    outcome = {0: "merge", 1: "discard"}.get(rc, f"exit_{rc}")
    if rc == 0:
        s.probation = {
            "snap": str(snap), "left": s.probation_runs, "code": code,
            "pid": path.stem, "project": name,
            "idx": len(rows), "pre": rows[-s.probation_runs:],
            "n_signal": n,
        }
    log_event(s, kind="self", result=outcome, code=code, decision=path.name)
    return outcome


# ----------------------------------------------------------------- probation


def _succ(rows: list[dict]) -> int:
    return sum(1 for r in rows if str(r.get("success", "")).strip() == "1")


def _kpi_worse(pre: list[dict], post: list[dict], name: str | None) -> tuple[list[str], str]:
    """(KPIs WORSE, nota). D4b: ativar quando kpi_report mergear — sem ele o
    critério de KPI simplesmente não participa (e isso fica no log)."""
    kpi_report = getattr(score, "kpi_report", None)
    if kpi_report is None:
        return [], "kpi_report ausente (D4b) — critério de KPI pulado"
    directions = {}
    try:
        import kpi

        project = _project()
        cfg = project.read_config(project.PROJECTS_ROOT / name) if name else {}
        if cfg.get("work_path"):
            directions = kpi.load_directions(Path(cfg["work_path"]).expanduser())
    except Exception:  # noqa: BLE001
        directions = {}
    try:
        rep = kpi_report(pre, post, directions)
    except Exception as e:  # noqa: BLE001
        return [], f"kpi_report falhou: {e}"
    return list(rep.get("worse") or []), ""


def probation_check(s: State) -> str | None:
    """Chamado depois de CADA step_project durante o probation. Ao zerar a
    contagem, compara a janela pós-merge com a pré e reverte se piorou."""
    p = s.probation
    if not p:
        return None
    p["left"] -= 1
    if p["left"] > 0:
        return None
    s.probation = None

    rows = project_rows(p["project"]) if p["project"] else []
    pre, post = p["pre"], rows[p["idx"]:]
    kpi_worse, kpi_note = _kpi_worse(pre, post, p["project"])
    tampered = [r for r in post if "tamper:" in (r.get("notes") or "")]

    reasons = []
    if kpi_worse:
        reasons.append(f"KPI WORSE: {', '.join(kpi_worse)}")
    if post and _succ(post) == 0 and _succ(pre) >= 1:
        reasons.append(f"success zerou (pré {_succ(pre)}/{len(pre)} -> pós 0/{len(post)})")
    if tampered:
        reasons.append(f"tamper novo em {len(tampered)} run(s) pós-merge")

    if not reasons:
        log_event(s, kind="probation", result="keep", code=p["code"], decision=kpi_note
                  or f"pré {_succ(pre)}/{len(pre)} · pós {_succ(post)}/{len(post)}")
        return "keep"

    restored = restore_genome(Path(p["snap"]))
    s.reverts += 1
    s.blocked_codes.add(p["code"])
    doc_path = _write_revert(s, p, pre, post, reasons, restored, kpi_note)
    try:
        graph.record_governance_event(
            project=p["project"] or "", action="autopilot_revert", actor="autopilot",
            detail=json.dumps({"pid": p["pid"], "code": p["code"], "reasons": reasons,
                               "snapshot": p["snap"], "decision": str(doc_path)},
                              ensure_ascii=False),
        )
    except Exception as e:  # noqa: BLE001
        log_event(s, kind="probation", result="graph_error", code=p["code"], decision=str(e)[:200])
    log_event(s, kind="probation", result="revert", code=p["code"], decision="; ".join(reasons))
    return "revert"


def _write_revert(s: State, p: dict, pre: list[dict], post: list[dict],
                  reasons: list[str], restored: list[str], kpi_note: str) -> Path:
    DECISIONS.mkdir(parents=True, exist_ok=True)
    out = DECISIONS / f"{p['pid']}-revert.md"
    out.write_text(f"""# Revert {p['pid']} — probation reprovou

**sessão:** `{s.session}` · **code:** `{p['code']}` · **projeto:** `{p['project']}`
**gerado por:** `autopilot.py` em {_now()}

O merge passou os gates da suite fixed, mas as {len(post)} run(s) de projeto
seguintes reprovaram o probation. O genoma foi restaurado do snapshot — nenhum
git, cópia byte a byte.

## Janelas

| | pré-merge | pós-merge |
|---|---|---|
| runs | {len(pre)} | {len(post)} |
| success | {_succ(pre)}/{len(pre)} | {_succ(post)}/{len(post)} |
| notes | {'; '.join(sorted({(r.get('notes') or '')[:40] for r in pre}))} | {'; '.join(sorted({(r.get('notes') or '')[:40] for r in post}))} |

## Motivo

{chr(10).join('- ' + r for r in reasons)}

{kpi_note}

## Restauração

- snapshot: `{p['snap']}`
- arquivos: {', '.join(restored) or '(nenhum)'}

`{p['code']}` entra na blocklist DESTA sessão: não é reproposto até alguém
olhar o motivo.
""", encoding="utf-8")
    return out


# ------------------------------------------------------------------ execução


def confine() -> None:
    """Toda escrita efêmera para dentro da raiz. Vale para os filhos também
    (env é herdado): o loop não deixa lixo em /tmp nem em $HOME."""
    tmp, ws = ROOT / ".harness_tmp", ROOT / ".harness_ws"
    tmp.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    os.environ["HARNESS_WS_ROOT"] = str(ws)
    # o import de project acontece DEPOIS do env acima (por isso é tardio); o
    # reapontamento explícito cobre o caso de alguém já ter importado antes.
    _project().WS_ROOT = ws


def external_work_paths(only: str | None = None) -> list[str]:
    """work_path de projeto enabled que aponta para fora da raiz do repo."""
    project = _project()
    bad = []
    if not project.PROJECTS_ROOT.is_dir():
        return bad
    for proj_dir in sorted(project.PROJECTS_ROOT.iterdir()):
        if not (proj_dir / ".harness" / "config.toml").exists():
            continue
        if only and proj_dir.name != only:
            continue
        cfg = project.read_config(proj_dir)
        if not cfg.get("enabled", True) or not cfg.get("work_path"):
            continue
        wp = Path(cfg["work_path"]).expanduser().resolve()
        if not wp.is_relative_to(ROOT):
            bad.append(f"{proj_dir.name}={wp}")
    return bad


def clamp_child_timeout(s: State) -> None:
    import agent

    left = int(min(agent.TIMEOUT_S, s.remaining()))
    os.environ["HARNESS_TIMEOUT"] = str(max(1, left))


def install_signals(s: State) -> None:
    def on_alarm(_sig, _frm):
        raise Deadline()

    def on_stop(_sig, _frm):
        raise Stop()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.setitimer(signal.ITIMER_REAL, max(1.0, float(s.wall_s)))


def loop(s: State) -> int:
    code = 0
    try:
        while True:
            if s.iterations >= s.max_iterations:
                s.stop_reason = "max_iterations"
                break
            if s.remaining() <= 0:
                raise Deadline()
            if s.budget > 0 and spent_usd(s) >= s.budget:
                s.stop_reason = "budget"
                code = 4
                break
            clamp_child_timeout(s)

            if s.steps_since_self >= s.self_every and s.probation is None:
                s.steps_since_self = 0
                step_self(s)
            else:
                result = step_project(s)
                if result in ("empty", "missing"):
                    s.stop_reason = f"queue_{result}"
                    break
                if result == "locked":
                    # outro processo tem o lock: não é erro, mas não adianta
                    # girar em vazio a cada milissegundo.
                    time.sleep(1)
                else:
                    s.steps_since_self += 1
                    probation_check(s)
            s.iterations += 1
    except Deadline:
        s.stop_reason, code = "deadline", 3
    except Stop:
        s.stop_reason, code = "signal", 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        summary = {
            "kind": "summary", "result": s.stop_reason or "done", "code": "",
            "decision": (f"{s.iterations} iterações · {s.ran} runs de fila · "
                         f"{s.selfs} ciclos self · {s.reverts} reverts"),
            "exit": code,
        }
        try:
            log_event(s, **summary)
        except Exception:  # noqa: BLE001
            pass
        print(f"[autopilot] {s.stop_reason or 'done'} · {summary['decision']} · "
              f"${spent_usd(s):.4f} · {s.elapsed():.0f}s · log {s.log_path}")
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="loop autônomo: fila de projeto + auto-evolução")
    p.add_argument("--minutes", type=float, help="parede em minutos (default: config)")
    p.add_argument("--budget", type=float, help="teto em USD; 0 = sem teto de custo")
    p.add_argument("--project", help="rodar só este projeto (default: scheduler)")
    p.add_argument("--self-every", type=int, help="1 passo de auto-evolução a cada N de fila")
    p.add_argument("--max-iterations", type=int)
    p.add_argument("--dry-run", action="store_true", help="HARNESS_MOCK_AGENT=1: $0, sem rede")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.dry_run:
        os.environ["HARNESS_MOCK_AGENT"] = "1"

    h = config.load()["harness"]
    wall_s = int(a.minutes * 60) if a.minutes is not None else int(h["autopilot_wall_clock_s"])
    budget = a.budget if a.budget is not None else float(h["autopilot_budget_usd"])
    s = State(
        session=f"ap-{_stamp()}-{os.getpid()}",
        project=a.project,
        wall_s=wall_s,
        budget=float(budget),
        max_iterations=a.max_iterations or int(h["autopilot_max_iterations"]),
        self_every=a.self_every if a.self_every is not None else int(h["autopilot_self_every"]),
        probation_runs=int(h["autopilot_probation_runs"]),
    )

    confine()
    if not h.get("autopilot_allow_external_work_path", False):
        bad = external_work_paths(a.project)
        if bad:
            print(f"[autopilot] recusa iniciar: work_path fora de {ROOT}: {', '.join(bad)}",
                  file=sys.stderr)
            return 2

    s.baseline_lines = baseline_lines()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_event(s, kind="start", result="ok", code="",
              decision=f"wall={s.wall_s}s budget=${s.budget:.2f} self_every={s.self_every} "
                       f"project={s.project or '(scheduler)'} mock={os.environ.get('HARNESS_MOCK_AGENT', '0')}")
    install_signals(s)
    return loop(s)


if __name__ == "__main__":
    raise SystemExit(main())
