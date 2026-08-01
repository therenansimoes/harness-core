#!/usr/bin/env python3
"""delivery.py — o eixo de ENTREGA: projetos reais, não tasks de laboratório.

O harness tem dois eixos que nunca se misturam:

    HARNESS   tasks/ e benchmarks/  -> "o motor melhorou?"   (results.tsv, score.py)
    ENTREGA   projects/<nome>/      -> "ficou bom pro Renan?" (delivery_events)

Um `verify.py` congelado no dia 1 não escala para um projeto que cresce. Aqui a
verificação é em CAMADAS:

    regression/   invariantes que NUNCA podem quebrar. Só crescem.
    acceptance/<session>/  o aceite da DELTA desta sessão. Efêmero, depois promovido.

E tem governança: o worker pode ADICIONAR check (só fortalece), mas não pode
APAGAR nem EDITAR um check de regression — isso exige aprovação explícita do
dono. O mecanismo é um MANIFEST com sha256; sumiço ou alteração silenciosa vira
violação, não warning.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import graph  # noqa: E402

PROJECTS = ROOT / "projects"
MANIFEST = "regression/MANIFEST.json"
CHECK_TIMEOUT = 120


class GovernanceViolation(Exception):
    """Regression foi apagada ou alterada sem aprovação. Nunca é warning."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_project(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.is_dir():
        return p.resolve()
    cand = PROJECTS / name_or_path
    if cand.is_dir():
        return cand.resolve()
    raise SystemExit(f"projeto não encontrado: {name_or_path}")


# ------------------------------------------------------------------- estrutura


def init_project(path: Path, name: str, ui: bool = False) -> Path:
    for d in ("spec", "regression", "acceptance", "sessions", ".harness"):
        (path / d).mkdir(parents=True, exist_ok=True)
    spec = path / "spec" / "SPEC.md"
    if not spec.exists():
        spec.write_text(
            f'+++\nversion = "0.1"\nupdated = "{now()[:10]}"\nui = {str(ui).lower()}\n+++\n\n'
            f"# {name}\n\n## O que é\n\n(descreva o projeto)\n\n"
            "## Requisitos permanentes\n\n- (viram checks em regression/)\n\n"
            "## Critérios de UI\n\n- (avaliados por humano até existir rubrica automática)\n",
            encoding="utf-8",
        )
    chg = path / "spec" / "CHANGELOG_SPEC.md"
    if not chg.exists():
        chg.write_text(f"# Changelog da SPEC\n\n## 0.1 — {now()[:10]}\n\nSpec inicial.\n",
                       encoding="utf-8")
    cfg = path / ".harness" / "config.toml"
    if not cfg.exists():
        cfg.write_text(f'[project]\nname = "{name}"\nui = {str(ui).lower()}\n', encoding="utf-8")
    write_manifest(path, actor="init", detail="manifest inicial", record=False)
    return path


def project_cfg(project: Path) -> dict:
    f = project / ".harness" / "config.toml"
    if not f.exists():
        return {"project": {"name": project.name, "ui": False}}
    try:
        return tomllib.loads(f.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"config inválido em {f}: {e}")


def spec_meta(project: Path) -> dict:
    f = project / "spec" / "SPEC.md"
    if not f.exists():
        return {}
    text = f.read_text(encoding="utf-8")
    if not text.startswith("+++"):
        return {}
    try:
        _, fm, _ = text.split("+++", 2)
        return tomllib.loads(fm)
    except (ValueError, tomllib.TOMLDecodeError):
        return {}


def new_session(project: Path, session_id: str, brief: str = "") -> Path:
    d = project / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    b = d / "brief.md"
    if not b.exists():
        b.write_text(
            brief or f"# Sessão {session_id}\n\n## Pedido\n\n(o que o Renan pediu)\n\n"
                     "## Aceite\n\n- [ ] (item verificável)\n",
            encoding="utf-8",
        )
    st = d / "state.json"
    if not st.exists():
        save_state(project, session_id, {
            "session_id": session_id, "project": project.name, "status": "open",
            "open_issues": [], "scores": {}, "next_action": "continue_delivery",
            "created": now(), "updated": now(),
        })
    (project / "acceptance" / session_id).mkdir(parents=True, exist_ok=True)
    graph.record_session(session_id=session_id, project=project.name,
                         brief_path=str(b), status="open")
    return d


def load_state(project: Path, session_id: str) -> dict:
    f = project / "sessions" / session_id / "state.json"
    if not f.exists():
        raise SystemExit(f"sessão não encontrada: {session_id} em {project.name}")
    return json.loads(f.read_text(encoding="utf-8"))


def save_state(project: Path, session_id: str, state: dict) -> None:
    state["updated"] = now()
    f = project / "sessions" / session_id / "state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ----------------------------------------------------------------- governança


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def regression_checks(project: Path) -> list[Path]:
    d = project / "regression"
    return sorted(p for p in d.glob("*.py") if not p.name.startswith("_")) if d.is_dir() else []


def acceptance_checks(project: Path, session_id: str) -> list[Path]:
    d = project / "acceptance" / session_id
    return sorted(p for p in d.glob("*.py") if not p.name.startswith("_")) if d.is_dir() else []


def write_manifest(project: Path, actor: str, detail: str, record: bool = True) -> dict:
    """Refaz o MANIFEST a partir do estado atual. Este é o ATO DE GOVERNANÇA."""
    entries = {p.name: _sha(p) for p in regression_checks(project)}
    man = {"updated": now(), "actor": actor, "checks": entries}
    f = project / MANIFEST
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    if record:
        graph.record_governance_event(
            project=project.name, action="approve_manifest", actor=actor,
            detail=f"{detail} ({len(entries)} checks)",
        )
    return man


def check_governance(project: Path) -> tuple[list[str], list[str]]:
    """Compara disco vs MANIFEST.

    Devolve (violacoes, novos). ADICIONAR check é livre — só fortalece a barra.
    APAGAR ou EDITAR é violação: exige aprovação explícita do dono.
    """
    f = project / MANIFEST
    atual = {p.name: _sha(p) for p in regression_checks(project)}
    if not f.exists():
        return [], sorted(atual)
    man = json.loads(f.read_text(encoding="utf-8")).get("checks", {})
    violacoes = []
    for nome, sha in man.items():
        if nome not in atual:
            violacoes.append(f"regression/{nome} foi APAGADO")
        elif atual[nome] != sha:
            violacoes.append(f"regression/{nome} foi MODIFICADO")
    novos = sorted(set(atual) - set(man))
    return violacoes, novos


# --------------------------------------------------------------------- verify


def run_check(project: Path, check: Path) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, str(check)], cwd=project, capture_output=True,
            text=True, timeout=CHECK_TIMEOUT,
        )
        out = (proc.stdout + proc.stderr).strip()
        return {
            "name": check.name, "ok": proc.returncode == 0,
            "reason": "" if proc.returncode == 0 else (out.splitlines() or [""])[0][:200],
        }
    except subprocess.TimeoutExpired:
        return {"name": check.name, "ok": False, "reason": "timeout"}


def verify(project: Path, session_id: str) -> dict:
    """Roda as duas camadas. Governança é avaliada ANTES e pode invalidar tudo."""
    violacoes, novos = check_governance(project)
    reg = [run_check(project, c) for c in regression_checks(project)]
    acc = [run_check(project, c) for c in acceptance_checks(project, session_id)]

    reg_ok = sum(1 for r in reg if r["ok"])
    acc_ok = sum(1 for r in acc if r["ok"])
    return {
        "project": project.name,
        "session_id": session_id,
        "regression": reg,
        "acceptance": acc,
        "regression_passed": reg_ok,
        "regression_total": len(reg),
        "acceptance_passed": acc_ok,
        "acceptance_total": len(acc),
        "checks_passed": reg_ok + acc_ok,
        "checks_total": len(reg) + len(acc),
        "governance_violations": violacoes,
        "new_unregistered_checks": novos,
        # Entrega só é sucesso com TUDO verde e governança limpa. Um check de
        # regression apagado é falha mesmo que todo o resto passe — senão o
        # caminho mais fácil para "ficar verde" seria apagar o check.
        "delivery_success": int(
            not violacoes and reg_ok == len(reg) and acc_ok == len(acc) and (len(reg) + len(acc)) > 0
        ),
    }


def promote_checks(project: Path, session_id: str, actor: str) -> list[str]:
    """Aceite da sessão vira regression permanente. É assim que a barra sobe."""
    movidos = []
    for c in acceptance_checks(project, session_id):
        dest = project / "regression" / f"{session_id}_{c.name}"
        if dest.exists():
            continue
        dest.write_bytes(c.read_bytes())
        c.unlink()
        movidos.append(dest.name)
    if movidos:
        write_manifest(project, actor=actor, detail=f"promoveu {len(movidos)} de {session_id}")
    return movidos


# ------------------------------------------------------------------ post-work


def brief_open_items(project: Path, session_id: str) -> list[str]:
    b = project / "sessions" / session_id / "brief.md"
    if not b.exists():
        return []
    return [
        ln.strip()[6:].strip()
        for ln in b.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("- [ ]")
    ]


def decide_next_action(v: dict, abertos: list[str], ui: bool, recorrentes: list[str]) -> str:
    if v["governance_violations"]:
        return "await_renan"
    if recorrentes:
        return "evolve_harness"
    if v["regression_passed"] < v["regression_total"]:
        return "continue_delivery"
    if v["acceptance_passed"] < v["acceptance_total"]:
        return "continue_delivery"
    if abertos or ui:
        return "await_renan"
    return "done"


def failure_patterns(project: Path, session_id: str, v: dict) -> list[str]:
    """Falha que se repete em sessões diferentes é sintoma do MOTOR, não da entrega.

    É o gatilho para `evolve_harness`: o mesmo check falhando várias vezes indica
    que o worker não consegue satisfazê-lo, não que o critério esteja errado.
    """
    falhando = {r["name"] for r in v["regression"] + v["acceptance"] if not r["ok"]}
    if not falhando:
        return []
    hist = graph.delivery_history(project.name, n=20)
    anteriores = [h for h in hist if h.get("session_id") != session_id]
    if len(anteriores) < 2:
        return []
    repetidas = [h for h in anteriores if h.get("delivery_success") == 0]
    return sorted(falhando) if len(repetidas) >= 2 else []


def proposal_stub(project: Path, session_id: str, motivos: list[str]) -> Path:
    """Worker NÃO muda gate de score. No máximo deixa uma proposta para o dono."""
    pid = f"gov_{project.name}_{session_id}"
    p = ROOT / "evolution" / "proposals" / f"{pid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"""+++
id = "{pid}"
from_version = "MANUAL"
to_version = "MANUAL"
hypothesis = "Falhas recorrentes na entrega de {project.name} sugerem limite do motor, não da spec."

[change]
file = "agent.py"
old = '''(preencher — esta proposta é um STUB gerado automaticamente)'''
new = '''(preencher)'''
+++

# Proposta gerada pelo post-work de `{project.name}` / `{session_id}`

**Isto não é uma mudança aprovada.** É um sinal: os mesmos checks falharam em
sessões diferentes, o que aponta para limitação do harness e não para critério
mal escrito.

Checks reincidentes:

{chr(10).join(f"- `{m}`" for m in motivos)}

## O que o dono precisa decidir

1. É limitação do motor (prompt, tools, turns) ou a spec está pedindo o impossível?
2. Se for motor: preencha `[change]` com UMA mudança e rode
   `python3 evolve.py --proposal evolution/proposals/{pid}.md`.
3. Se for spec: ajuste `spec/SPEC.md` e registre em `spec/CHANGELOG_SPEC.md`.

O worker não pode fazer nem uma nem outra sozinho — mudar o critério de avaliação
para ficar verde é exatamente o que este harness existe para impedir.
""",
        encoding="utf-8",
    )
    return p


UI_RUBRICA = [
    "hierarquia visual clara (o olho sabe onde olhar primeiro)",
    "contraste legível em texto corrido",
    "espaçamento consistente entre blocos",
    "usável em tela estreita (~375px)",
    "estados interativos visíveis (hover/focus)",
]


def post_work(project: Path, session_id: str, actor: str = "cli") -> dict:
    v = verify(project, session_id)
    st = load_state(project, session_id)
    cfg = project_cfg(project)
    meta = spec_meta(project)
    ui = bool(cfg.get("project", {}).get("ui") or meta.get("ui"))

    abertos = brief_open_items(project, session_id)
    recorrentes = failure_patterns(project, session_id, v)
    next_action = decide_next_action(v, abertos, ui, recorrentes)

    issues = [f"governança: {x}" for x in v["governance_violations"]]
    issues += [f"regression: {r['name']} — {r['reason']}" for r in v["regression"] if not r["ok"]]
    issues += [f"acceptance: {r['name']} — {r['reason']}" for r in v["acceptance"] if not r["ok"]]
    issues += [f"brief não marcado: {b}" for b in abertos]
    if ui:
        issues.append("needs_human_ui_review: rubrica visual não é automatizável ainda")

    stub = proposal_stub(project, session_id, recorrentes) if recorrentes else None

    st.update({
        "status": next_action,
        "open_issues": issues,
        "scores": {
            "delivery_success": v["delivery_success"],
            "regression": f"{v['regression_passed']}/{v['regression_total']}",
            "acceptance": f"{v['acceptance_passed']}/{v['acceptance_total']}",
            "needs_human_ui_review": ui,
        },
        "next_action": next_action,
    })
    save_state(project, session_id, st)

    report = write_report(project, session_id, v, abertos, ui, next_action, stub)
    graph.update_session_status(session_id, next_action)
    graph.record_delivery_event(
        session_id=session_id, project=project.name, kind="post_work",
        delivery_success=v["delivery_success"],
        checks_total=v["checks_total"], checks_passed=v["checks_passed"],
        regression_passed=v["regression_passed"], regression_total=v["regression_total"],
        acceptance_passed=v["acceptance_passed"], acceptance_total=v["acceptance_total"],
        next_action=next_action,
        notes="; ".join(issues)[:500],
        report_path=str(report),
    )
    v["next_action"] = next_action
    v["report"] = str(report)
    v["open_issues"] = issues
    v["proposal_stub"] = str(stub) if stub else ""
    return v


def write_report(project: Path, session_id: str, v: dict, abertos: list[str],
                 ui: bool, next_action: str, stub: Path | None) -> Path:
    def tabela(rows):
        if not rows:
            return "_(nenhum)_"
        return "\n".join(
            f"| {'PASS' if r['ok'] else 'FAIL'} | `{r['name']}` | {r['reason'] or '—'} |"
            for r in rows
        )

    gov = v["governance_violations"]
    gov_md = (
        "Nenhuma violação.\n" if not gov else
        "**VIOLAÇÃO** — verificação invalidada até o dono aprovar:\n\n"
        + "\n".join(f"- {x}" for x in gov)
        + "\n\nRodar `harness_cli.py governance-approve` se a mudança for legítima.\n"
    )
    novos = v["new_unregistered_checks"]
    novos_md = (
        "" if not novos else
        f"\nChecks novos ainda não registrados no MANIFEST (adicionar é permitido): "
        + ", ".join(f"`{n}`" for n in novos) + "\n"
    )
    acao = {
        "done": "Entrega fechada: tudo verde, nada pendente.",
        "continue_delivery": "Falta trabalho de ENTREGA — os checks dizem o quê.",
        "await_renan": "Precisa de decisão humana (UI, itens do brief ou governança).",
        "evolve_harness": "Padrão de falha recorrente: o gargalo parece ser o MOTOR.",
    }[next_action]

    doc = f"""# Delivery report — {project.name} / {session_id}

**Gerado:** {now()} · **delivery_success:** {v['delivery_success']}
**next_action:** `{next_action}` — {acao}

Este é o eixo de ENTREGA. Não se mistura com o score de laboratório do harness
(`results.tsv`): aqui a pergunta é se o trabalho serviu, não se o motor melhorou.

## Regression ({v['regression_passed']}/{v['regression_total']}) — não pode regredir

| | check | motivo |
|---|---|---|
{tabela(v['regression'])}

## Acceptance ({v['acceptance_passed']}/{v['acceptance_total']}) — a delta desta sessão

| | check | motivo |
|---|---|---|
{tabela(v['acceptance'])}

## Governança

{gov_md}{novos_md}
## Alinhamento ao brief

{("Todos os itens do brief estão marcados." if not abertos else chr(10).join(f"- [ ] {b}" for b in abertos))}

## UI

{("Projeto marcado como UI. **needs_human_ui_review** — a rubrica abaixo não é automatizável ainda:" + chr(10) + chr(10) + chr(10).join(f"- [ ] {r}" for r in UI_RUBRICA)) if ui else "Projeto sem UI declarada — sem rubrica visual."}

{f"## Proposta aberta{chr(10)}{chr(10)}Falhas recorrentes geraram um stub de governança em `{stub.relative_to(ROOT) if stub else ''}`. O worker não pode alterar critério de avaliação — só sinalizar." if stub else ""}
"""
    out = project / "sessions" / session_id / "delivery_report.md"
    out.write_text(doc, encoding="utf-8")
    return out


def resume(project: Path, session_id: str) -> str:
    st = load_state(project, session_id)
    b = project / "sessions" / session_id / "brief.md"
    brief = b.read_text(encoding="utf-8").strip() if b.exists() else "(sem brief)"
    issues = st.get("open_issues") or []
    ev = graph.session_state(session_id) or {}
    linhas = [
        f"projeto:     {st.get('project')}",
        f"sessão:      {session_id}   status: {st.get('status')}",
        f"atualizado:  {st.get('updated')}",
        "",
        "--- brief ---",
        brief[:800],
        "",
        f"--- scores de entrega --- {json.dumps(st.get('scores', {}), ensure_ascii=False)}",
        f"--- open issues ({len(issues)}) ---",
        *[f"  - {i}" for i in issues[:15]],
        "",
        f"next_action: {st.get('next_action')}",
    ]
    if ev:
        linhas.append(f"(graph: última entrega registrada em {ev.get('updated') or ev.get('ts')})")
    return "\n".join(linhas)
