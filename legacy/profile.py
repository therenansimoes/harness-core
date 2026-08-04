#!/usr/bin/env python3
"""profile.py — detecção determinística de stack e comandos do projeto-alvo.

Degrau D3 (eixo self-adaptive): o harness olha o repo ANTES de falar com o
modelo e injeta "como se testa aqui" no system prompt, em vez de deixar o
agente descobrir por tentativa e erro gastando turns.

A heurística — tabela ordenada, primeiro-match, task runner e workspace antes
de linguagem — é vendorizada de `universal-test-runner` (xavdid, MIT):
`commands.py` + `context.py`. Reimplementada, não copiada; nenhuma dependência
nova. O upstream resolve só `test`; as colunas lint/build são extensão nossa
(confiança média, ver pesquisa). Prior art e pegadinhas:
`evolution/research/stack-detection-prior-art.md`.

Regra de ouro das pegadinhas: marcador existir não é o mesmo que marcador
valer. Checar CONTEÚDO (alvo `test:` no Makefile, script que não é o stub do
`npm init`, lockfile junto do script) e sempre devolver `debug_line` dizendo
por que aquele comando saiu.

stdlib only, Python 3.11+ (tomllib).

    python3 profile.py <path>     # imprime o profile em TOML no stdout
"""

from __future__ import annotations

import json
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

CACHE_TTL_S = 24 * 3600
CONVENTIONS_MAX = 2000
CONVENTION_FILES = ("CLAUDE.md", "AGENTS.md")

# stub do `npm init`: "echo \"Error: no test specified\" && exit 1" existe como
# script e passaria em qualquer check de existência.
STUB_TEST_RE = re.compile(r"exit\s+1|no test specified", re.I)
# alvo de Makefile / receita de justfile no começo da linha (`:=` não conta)
MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_./-]+)\s*:(?!=)", re.M)
JUST_RECIPE_RE = re.compile(r"^@?([A-Za-z0-9_-]+)[^:\n]*:(?!=)", re.M)


@dataclass
class Profile:
    stack: str = "unknown"
    test_cmd: str | None = None
    lint_cmd: str | None = None
    build_cmd: str | None = None
    matched_marker: str = ""
    debug_line: str = "nenhum marcador reconhecido"
    conventions: str = ""


# ---------------------------------------------------------------- utilitários


def _text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _toml(p: Path) -> dict:
    try:
        return tomllib.loads(_text(p)) if p.is_file() else {}
    except tomllib.TOMLDecodeError:
        return {}


def _json(p: Path) -> dict:
    try:
        data = json.loads(_text(p)) if p.is_file() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _first(repo: Path, names: tuple[str, ...]) -> str:
    for n in names:
        if (repo / n).exists():
            return n
    return ""


def _targets(text: str, rx: re.Pattern[str]) -> set[str]:
    return {m.group(1) for m in rx.finditer(text)}


# ------------------------------------------------------------ sinais Python


def _dep_strings(data: dict) -> list[str]:
    """Todo lugar de pyproject.toml onde uma dep pode aparecer declarada."""
    out: list[str] = []

    def eat(v) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for i in v:
                eat(i)
        elif isinstance(v, dict):
            out.extend(v.keys())

    project = data.get("project", {})
    eat(project.get("dependencies"))
    eat(project.get("optional-dependencies"))
    eat(data.get("dependency-groups"))
    tool = data.get("tool", {})
    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    eat(poetry.get("dev-dependencies"))
    for grp in (poetry.get("group") or {}).values():
        if isinstance(grp, dict):
            eat(grp.get("dependencies"))
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    eat(uv.get("dev-dependencies"))
    return [s for s in out if isinstance(s, str)]


def _pytest_marker(repo: Path) -> tuple[str, bool]:
    """(marcador, forte). Heurística completa da pesquisa, config declarada
    primeiro: só ela fixa rootdir/testpaths, e é isso que decide se `pytest`
    puro é seguro. `.pytest_cache/` fica por último — é artefato transitório,
    some num clone limpo, então não pode mandar no comando."""
    if (repo / "pytest.ini").is_file():
        return "pytest.ini", True
    if "[pytest]" in _text(repo / "tox.ini"):
        return "tox.ini [pytest]", True
    if "[tool:pytest]" in _text(repo / "setup.cfg"):
        return "setup.cfg [tool:pytest]", True
    pyproject = repo / "pyproject.toml"
    if "[tool.pytest.ini_options]" in _text(pyproject):
        return "pyproject.toml [tool.pytest.ini_options]", True
    if any(d.strip().lower().startswith("pytest") for d in _dep_strings(_toml(pyproject))):
        return "pyproject.toml dep pytest", False
    for req in sorted(repo.glob("requirements*.txt")):
        for line in _text(req).splitlines():
            if line.strip().lower().startswith("pytest"):
                return f"{req.name} dep pytest", False
    if (repo / ".pytest_cache").is_dir():
        return ".pytest_cache/", False
    return "", False


def _py_lint(repo: Path) -> str | None:
    """Só emite linter CONFIGURADO. Mandar `flake8` num repo sem flake8 é
    inventar comando quebrado — pior que não dizer nada."""
    if (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        return "ruff check ."
    if "[tool.ruff]" in _text(repo / "pyproject.toml"):
        return "ruff check ."
    if (repo / ".flake8").is_file() or "[flake8]" in _text(repo / "setup.cfg") + _text(
        repo / "tox.ini"
    ):
        return "flake8"
    return None


def _pytest_argv(repo: Path, strong: bool) -> str:
    """Sem config declarada o pytest coleta a árvore inteira — em repo que
    guarda fixtures de teste dentro de si (o harness-core é um) a coleção
    estoura. Sinal fraco + diretório de testes -> escopa nele."""
    tdir = "" if strong else _tests_dir(repo)
    return f"pytest {tdir}/" if tdir else "pytest"


def _tests_dir(repo: Path) -> str:
    """Diretório de testes estilo pytest sem config declarada. Extensão nossa à
    tabela: repo com `tests/test_*.py` e nenhum marcador de config roda pytest
    na prática (é o caso do próprio harness-core)."""
    for name in ("tests", "test"):
        d = repo / name
        if d.is_dir() and any(d.glob("test_*.py")):
            return name
    return ""


# ------------------------------------------------------------- sinais Node


def _node_pm(repo: Path, pkg: dict) -> tuple[str, str]:
    """(gerenciador, marcador). `packageManager` vence lockfile (nixpacks)."""
    pm = pkg.get("packageManager")
    if isinstance(pm, str):
        name = pm.split("@")[0].strip()
        if name in ("npm", "yarn", "pnpm", "bun"):
            return name, "package.json packageManager"
    for lock, name in (
        ("package-lock.json", "npm"),
        ("yarn.lock", "yarn"),
        ("pnpm-lock.yaml", "pnpm"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    ):
        if (repo / lock).exists():
            return name, lock
    return "", ""


def _script(pkg: dict, name: str) -> str:
    scripts = pkg.get("scripts")
    v = scripts.get(name) if isinstance(scripts, dict) else None
    return v if isinstance(v, str) else ""


# ------------------------------------------------------------------ detecção


def _detect_commands(repo: Path) -> Profile:
    P = Profile

    # 1. justfile — task runner genérico vence linguagem (ordem do upstream)
    just = _first(repo, ("justfile", "Justfile", ".justfile"))
    if just:
        rec = _targets(_text(repo / just), JUST_RECIPE_RE)
        if "test" in rec:
            return P(
                "just",
                "just test",
                "just lint" if "lint" in rec else None,
                "just build" if "build" in rec else None,
                just,
                f"{just} tem receita `test:`",
            )

    # 2. Makefile — só vale com alvo `test:` de verdade (marcador fantasma)
    mk = _first(repo, ("Makefile", "makefile", "GNUmakefile"))
    if mk:
        tgt = _targets(_text(repo / mk), MAKE_TARGET_RE)
        if "test" in tgt:
            return P(
                "make",
                "make test",
                "make lint" if "lint" in tgt else None,
                "make build" if "build" in tgt else None,
                mk,
                f"{mk} tem alvo `test:`",
            )

    # 3. workspace/monorepo ANTES de linguagem — rodar pytest/npm test na raiz
    #    de um monorepo é quase sempre errado
    pkg = _json(repo / "package.json")
    for marker, stack, tmpl in (
        ("pnpm-workspace.yaml", "pnpm-workspace", "pnpm -r {}"),
        ("turbo.json", "turborepo", "turbo run {}"),
        ("nx.json", "nx", "nx run-many -t {}"),
    ):
        if (repo / marker).is_file():
            return P(
                stack,
                tmpl.format("test"),
                tmpl.format("lint"),
                tmpl.format("build"),
                marker,
                f"{marker} presente — monorepo, comandos no nível do workspace",
            )
    if pkg.get("workspaces"):
        pm, _ = _node_pm(repo, pkg)
        pm = pm or "npm"
        return P(
            f"{pm}-workspaces",
            f"{pm} test --workspaces",
            f"{pm} run lint --workspaces",
            f"{pm} run build --workspaces",
            "package.json workspaces",
            "package.json declara workspaces — monorepo antes de linguagem",
        )

    # 4. gerenciador Python por LOCKFILE (`[tool.poetry]` não implica poetry
    #    instalado) + pytest detectado
    pytest_marker, strong = _pytest_marker(repo)
    if pytest_marker:
        argv = _pytest_argv(repo, strong)
        for lock, pm in (("uv.lock", "uv"), ("pdm.lock", "pdm"), ("poetry.lock", "poetry")):
            if (repo / lock).is_file():
                lint = _py_lint(repo)
                return P(
                    f"python-{pm}",
                    f"{pm} run {argv}",
                    f"{pm} run {lint}" if lint else None,
                    None,
                    lock,
                    f"{lock} + pytest ({pytest_marker})",
                )

        # 5. pytest puro
        lint = _py_lint(repo)
        return P(
            "python-pytest",
            argv,
            lint,
            None,
            pytest_marker,
            f"pytest detectado por {pytest_marker}",
        )

    # 6. Django sem pytest (pytest-django venceria acima)
    if (repo / "manage.py").is_file():
        return P(
            "django",
            "python3 manage.py test",
            None,
            None,
            "manage.py",
            "manage.py sem pytest detectado",
        )

    # 6b. tests/ com test_*.py e nenhuma config — extensão nossa (ver _tests_dir)
    tdir = _tests_dir(repo)
    if tdir:
        return P(
            "python-pytest",
            f"pytest {tdir}/",
            _py_lint(repo),
            None,
            f"{tdir}/test_*.py",
            f"{tdir}/ tem test_*.py sem config de pytest — heurística",
        )

    # 7. fallback Python
    pyfile = _first(
        repo, ("pyproject.toml", "setup.py", "tox.ini", "setup.cfg", "requirements.txt")
    )
    if pyfile:
        return P(
            "python",
            "python3 -m unittest",
            _py_lint(repo),
            "python3 -m build" if pyfile == "pyproject.toml" else None,
            pyfile,
            f"{pyfile} sem pytest — fallback unittest",
        )

    # 8. Go — nunca `go test` puro (módulo com subpacotes daria zero testes)
    if (repo / "go.mod").is_file():
        return P(
            "go", "go test ./...", "go vet ./...", "go build ./...", "go.mod", "go.mod presente"
        )

    # 9. Rust
    if (repo / "Cargo.toml").is_file():
        return P(
            "rust",
            "cargo test",
            "cargo clippy -- -D warnings",
            "cargo build",
            "Cargo.toml",
            "Cargo.toml presente",
        )

    # 10. Node: exige scripts.test E lockfile, e o script não pode ser o stub
    if pkg:
        pm, pm_marker = _node_pm(repo, pkg)
        test_script = _script(pkg, "test")
        if pm and pm != "bun" and test_script and not STUB_TEST_RE.search(test_script):
            return P(
                pm,
                f"{pm} test",
                f"{pm} run lint" if _script(pkg, "lint") else None,
                f"{pm} run build" if _script(pkg, "build") else None,
                f"package.json scripts.test + {pm_marker}",
                f"scripts.test real + {pm_marker} -> {pm}",
            )

    # 10b. bun não exige script
    bun = _first(repo, ("bun.lockb", "bun.lock"))
    if bun:
        return P(
            "bun",
            "bun test",
            None,
            "bun run build" if _script(pkg, "build") else None,
            bun,
            f"{bun} presente — bun test não exige script",
        )

    # 11. TypeScript sem script de teste: typecheck é o mais perto que dá
    if (repo / "tsconfig.json").is_file():
        return P(
            "typescript",
            "npx tsc --noEmit",
            None,
            "npx tsc",
            "tsconfig.json",
            "tsconfig.json sem scripts.test — comando é TYPECHECK, não teste",
        )

    return P()


def _conventions(repo: Path) -> str:
    parts = []
    for name in CONVENTION_FILES:
        p = repo / name
        if p.is_file():
            body = _text(p).strip()
            if body:
                parts.append(f"# {name}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > CONVENTIONS_MAX:
        text = text[:CONVENTIONS_MAX].rstrip() + "\n… (truncado)"
    return text


def detect(repo_path: str | Path) -> Profile:
    """Detecta stack e comandos de `repo_path`. Nunca levanta: repo ilegível ou
    sem marcador vira Profile(stack="unknown") com tudo None."""
    repo = Path(repo_path).resolve()
    prof = _detect_commands(repo)
    prof.conventions = _conventions(repo)
    return prof


# ------------------------------------------------------------ (de)serialização


def _toml_str(s: str) -> str:
    """json.dumps produz exatamente os escapes que a basic string do TOML
    aceita (\\", \\\\, \\n, \\t, \\uXXXX) — round-trip garantido por tomllib."""
    return json.dumps(s, ensure_ascii=False)


def to_toml(prof: Profile, detected_at: float | None = None) -> str:
    lines = [
        "# gerado por profile.py — detecção determinística de stack/comandos",
        "[profile]",
        f"detected_at = {int(detected_at if detected_at is not None else time.time())}",
        f"stack = {_toml_str(prof.stack)}",
    ]
    for field in ("test_cmd", "lint_cmd", "build_cmd"):
        v = getattr(prof, field)
        if v:
            lines.append(f"{field} = {_toml_str(v)}")
    lines.append(f"matched_marker = {_toml_str(prof.matched_marker)}")
    lines.append(f"debug_line = {_toml_str(prof.debug_line)}")
    if prof.conventions:
        lines.append(f"conventions = {_toml_str(prof.conventions)}")
    return "\n".join(lines) + "\n"


def profile_path(repo_path: str | Path) -> Path:
    return Path(repo_path).resolve() / ".harness" / "profile.toml"


def write_profile(repo_path: str | Path) -> Path:
    """Detecta e serializa em <repo>/.harness/profile.toml. Retorna o path."""
    out = profile_path(repo_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_toml(detect(repo_path)), encoding="utf-8")
    return out


def load_profile(repo_path: str | Path) -> Profile:
    """Cache com validade de 24h: lê o .harness/profile.toml se for recente,
    senão re-detecta e regrava. Falha de escrita (fs read-only) não é fatal."""
    p = profile_path(repo_path)
    data = _toml(p).get("profile", {}) if p.is_file() else {}
    if data and time.time() - float(data.get("detected_at", 0)) < CACHE_TTL_S:
        return Profile(
            stack=data.get("stack", "unknown"),
            test_cmd=data.get("test_cmd"),
            lint_cmd=data.get("lint_cmd"),
            build_cmd=data.get("build_cmd"),
            matched_marker=data.get("matched_marker", ""),
            debug_line=data.get("debug_line", ""),
            conventions=data.get("conventions", ""),
        )
    prof = detect(repo_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(to_toml(prof), encoding="utf-8")
    except OSError:
        pass
    return prof


def prompt_block(prof: Profile) -> str:
    """Bloco curto pro system prompt. Vazio quando não há nada a dizer — não
    gastar contexto do modelo com "stack: unknown"."""
    if prof.stack == "unknown" and not prof.conventions:
        return ""
    facts = [f"Projeto: stack {prof.stack}."]
    for label, cmd in (
        ("Testes", prof.test_cmd),
        ("Lint", prof.lint_cmd),
        ("Build", prof.build_cmd),
    ):
        if cmd:
            facts.append(f"{label}: `{cmd}`.")
    block = "\n" + " ".join(facts) + "\n"
    if prof.conventions:
        block += f"Convenções do projeto:\n{prof.conventions}\n"
    return block


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.stdout.write(to_toml(detect(target)))
