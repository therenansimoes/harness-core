"""Gate do plano: o que o decompose propôs é EXECUTÁVEL, ou não sai daqui.

O `parse_plan` julga a FORMA de cada sub-unit (slug, kind, verify determinístico).
Ninguém julgava o PLANO: cinco specs impecáveis podem descrever uma fila que já
está pronta, que depende para a frente, ou cuja régua nunca fica vermelha — e a
fila só revela isso depois de gravada, quando o driver aceita o passo 1 sem o
agente escrever uma linha e o ledger registra progresso que não houve.

O check que importa é o RED-FIRST (e): cada `verify_cmd` roda num worktree
descartável do HEAD, ANTES de qualquer trabalho. Toda unidade tem que sair
VERMELHA. Unidade já verde é uma das duas doenças, e as duas são fatais para a
fila: ou o passo é no-op (não há o que fazer), ou a régua mede outra coisa que
não a entrega. Um verify que já passa é um accept grátis.

Os checks estruturais (a-d) rodam ANTES e curto-circuitam o (e): plano torto não
merece o custo de N worktrees, e rodar shell de um plano que já reprovou é
executar comando que ninguém validou.

`files` (c) é AVISO, não erro: a sobreposição declarada é sinal fraco (duas
unidades podem tocar o mesmo arquivo em regiões distintas) e reprovar por ela
mataria plano bom. Aviso volta na mesma lista, prefixado — quem chama filtra.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from harness import add
from harness.ruler.verify import run_verify
from harness.types import UnitSpec
from harness.workspace.provision import dispose, is_git_repo, provision

if TYPE_CHECKING:
    from harness.improve.decompose import DecomposeProposal, SubUnit

# Prefixo do que NÃO reprova. Quem chama decide o que fazer com o resto.
WARN = "aviso:"

# Mesmo teto que o prompt de planejamento promete ao modelo (regra 2). Prompt
# maior que isto estoura o contexto do executor local de 4B.
MAX_PROMPT_CHARS = 3000
MAX_FILES = 3
MIN_CHECKS = 1
# Teto por unidade no RED-FIRST. Verify de sub-tarefa atômica é barato por
# contrato (regra 4 do plano); o que passa disto já é régua cara demais para
# rodar a cada tentativa.
RED_TIMEOUT_S = 60.0
WS_PREFIX = "plangate"


def check_plan(proposal: DecomposeProposal, repo: Path | str) -> list[str]:
    """Lista de reprovações do plano. Vazia = aprovado.

    Item prefixado por `WARN` é aviso e NÃO reprova — o chamador filtra por
    `errors()`. A ordem é a do plano, para o relatório ler como a fila.
    """
    errors = _structural(proposal)
    warnings = _file_warnings(proposal)
    if errors:
        # Curto-circuito: plano estruturalmente torto não paga N worktrees, e
        # o shell de um plano reprovado não roda.
        return errors + warnings
    return _red_first(proposal, Path(repo)) + warnings


def errors(problems: list[str]) -> list[str]:
    """Só o que reprova — avisos fora."""
    return [p for p in problems if not p.startswith(WARN)]


def _structural(proposal: DecomposeProposal) -> list[str]:
    """(a) régua, (b) deps para trás, (d) prompt e kind. Tudo sem I/O."""
    out: list[str] = []
    slugs = [u.slug for u in proposal.units]
    for i, unit in enumerate(proposal.units):
        name = unit.name
        # (a) régua: sem verify_cmd não há veredito, sem check não há gradiente.
        if not str(unit.spec.get("verify_cmd") or "").strip():
            out.append(f"{name}: sem verify_cmd")
        if len(tuple(unit.spec.get("checks") or ())) < MIN_CHECKS:
            out.append(f"{name}: sem [checks] — a régua não gradua nada")
        # (b) deps: só para trás. A ordem da fila é a dependência, então dep
        # para a frente trava o passo 1 esperando o último.
        for dep in tuple(unit.spec.get("deps") or ()):
            dep = str(dep)
            if dep not in slugs:
                out.append(f"{name}: dep {dep!r} não existe no plano")
            elif slugs.index(dep) >= i:
                out.append(f"{name}: dep {dep!r} é posterior (ou ela mesma) na fila")
        # (d) prompt e kind.
        prompt = str(unit.spec.get("prompt_md") or "")
        if len(prompt) > MAX_PROMPT_CHARS:
            out.append(f"{name}: prompt com {len(prompt)} chars > teto {MAX_PROMPT_CHARS}")
        kind = unit.spec.get("kind")
        if kind not in add.KINDS:
            out.append(f"{name}: kind inválido: {kind!r}")
    return out


def _file_warnings(proposal: DecomposeProposal) -> list[str]:
    """(c) `files`: declarado, curto e disjunto entre unidades sem dep. AVISO."""
    out: list[str] = []
    files: list[set[str]] = []
    for unit in proposal.units:
        declared = {str(f) for f in (unit.spec.get("files") or ())}
        files.append(declared)
        if not declared:
            out.append(f"{WARN} {unit.name}: sem `files` declarados")
        elif len(declared) > MAX_FILES:
            out.append(f"{WARN} {unit.name}: {len(declared)} files > teto {MAX_FILES}")
    reach = _reachable(proposal)
    for i, unit in enumerate(proposal.units):
        for j in range(i + 1, len(proposal.units)):
            other = proposal.units[j]
            if other.slug in reach[i] or unit.slug in reach[j]:
                continue  # relação de dep: a sobreposição é a integração
            shared = sorted(files[i] & files[j])
            if shared:
                out.append(
                    f"{WARN} {unit.name} e {other.name} tocam {', '.join(shared)} "
                    f"sem relação de dep"
                )
    return out


def _reachable(proposal: DecomposeProposal) -> list[set[str]]:
    """Fecho transitivo de `deps` por unidade (slugs). Deps só apontam para
    trás, então uma passada em ordem já fecha — não há ciclo por construção."""
    by_slug: dict[str, set[str]] = {}
    out: list[set[str]] = []
    for unit in proposal.units:
        got: set[str] = set()
        for dep in tuple(unit.spec.get("deps") or ()):
            dep = str(dep)
            got.add(dep)
            got |= by_slug.get(dep, set())
        by_slug[unit.slug] = got
        out.append(got)
    return out


def _red_first(proposal: DecomposeProposal, repo: Path) -> list[str]:
    """(e) Toda unidade sai VERMELHA no HEAD, ou o plano não vale.

    Um worktree por unidade: verify que escreve no workspace (o normal é não
    escrever, mas o plano vem de um modelo) contaminaria a medição do próximo.
    Só o `verify_cmd` entra — os `[checks]` graduam progresso, e um check verde
    no HEAD é esperado (é para isso que serve o degrau fácil).
    """
    out: list[str] = []
    mode = "worktree" if is_git_repo(repo) else "tmpdir"
    for unit in proposal.units:
        try:
            ws = provision(repo, f"{WS_PREFIX}-{uuid.uuid4().hex[:8]}", mode=mode)
        except (RuntimeError, ValueError, OSError) as exc:
            # Fail-closed: sem workspace não dá para saber se a régua já está
            # verde, e aprovar sem essa medição é justamente o que o gate existe
            # para impedir. (Repo sem commit nenhum cai aqui.)
            out.append(f"{unit.name}: não deu para provisionar workspace do HEAD — {exc}")
            continue
        try:
            verdict = run_verify(_probe(unit, ws.path), ws.path, timeout_s=RED_TIMEOUT_S)
        finally:
            dispose(ws)
        if verdict.passed:
            out.append(
                f"{unit.name}: verify_cmd JÁ PASSA no HEAD (exit 0) — passo no-op ou "
                f"régua errada: {unit.spec['verify_cmd']}"
            )
    return out


def _probe(unit: SubUnit, ws: Path) -> UnitSpec:
    """A sub-unit como UnitSpec, sem `[checks]`: aqui o veredito é do verify."""
    return UnitSpec(
        id=unit.name,
        path=ws,
        prompt=str(unit.spec.get("prompt_md") or ""),
        verify_cmd=str(unit.spec["verify_cmd"]),
        checks=(),
    )
