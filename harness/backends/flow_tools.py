"""Tools de fluxo para o executor: `install_deps`, `run_tests`, `run_lint`,
`local_screenshot` e `detect_stack`.

Mesmo contrato do `web_tools.py`: import de LangChain é lazy, `load_flow_tools`
NUNCA levanta (qualquer falha vira `[]` com uma linha no stderr) e o módulo
importa sem nenhuma dependência opcional instalada.

Estas tools existem porque o caminho "modelo escreve o comando no shell" cobra
caro por algo que é sempre igual: descobrir o gerenciador, montar o venv, achar
o binário, ler 400 linhas de log de npm. Aqui o comando é do harness e o modelo
recebe só o veredito — quantos pacotes, quais testes falharam, em que linha.

PEGADINHA CENTRAL: o subprocess é PRÓPRIO, não passa pelo
`SafeShellBackend.execute` (cujo teto é `MAX_TIMEOUT = 120` s — `npm ci` de
projeto médio estoura isso). A cerca continua valendo: cada comando passa por
`safe_shell.check_command` ANTES do subprocess, e o motivo do bloqueio volta
como output normal da tool.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALL_TIMEOUT_S = 600  # `npm ci` frio passa fácil dos 120s do SafeShellBackend
TEST_TIMEOUT_S = 300
LINT_TIMEOUT_S = 120
STACK_TIMEOUT_S = 60

MAX_ERRO_CHARS = 2000  # teto do trecho de erro devolvido ao modelo
MAX_ERRO_LINHAS = 20
MAX_FALHAS = 5  # falhas de teste detalhadas; o resto fica no log
MAX_MSG_CHARS = 200
MAX_LINT_LINHAS = 10

TESTS_LOG = ".harness/tests.log"
INSTALL_AUDIT = ".harness/install-audit.log"
# Ponteiro para o env do projeto, escrito no provisionamento do workspace.
ENV_FILE_PTR = ".harness/env_file"
PROCS_JSON = ".harness/procs.json"

LOCKFILES = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "requirements.txt": "pip",
}

# Erro de `npm ci` que significa "o lock não serve": cair para `npm install`
# resolve, qualquer outro erro é problema real e não se insiste.
_LOCK_RUIM = re.compile(r"(?i)\block ?file\b|\bEUSAGE\b")
# `npm ERR!` é npm<=9 e `npm error` é npm>=10: os dois na cerca do filtro, senão
# o modelo recebe log de warning e nenhuma linha do erro real.
_RUIDO_ERRO = re.compile(r"^(npm ERR!|npm error|error|ERROR)")


def warn(msg: str) -> None:
    """Uma linha no stderr. Falha de tool de fluxo é diagnóstico, não crash."""
    print(f"flow_tools: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- ambiente


def _data_dir() -> Path:
    """Mesma convenção do ledger: `$HARNESS_DATA_DIR`, default `data`."""
    return Path(os.environ.get("HARNESS_DATA_DIR", "data"))


def ws_env_file(workspace: Path) -> Path | None:
    """Env de projeto do workspace, se houver: `<ws>/.harness/env_file` guarda o
    path (absoluto) escrito no provisionamento. Ponteiro em arquivo porque a
    tool só conhece o ws — não há vínculo ws→projeto em memória aqui."""
    ptr = Path(workspace) / ENV_FILE_PTR
    if not ptr.is_file():
        return None
    try:
        alvo = ptr.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return Path(alvo) if alvo else None


def _env(workspace: Path, env_file: Path | str | None = None) -> dict[str, str]:
    """Env do subprocess: venv do workspace no PATH, cache no data dir.

    O cache compartilhado é de propósito — baixar o mesmo tarball a cada run é
    o gasto mais bobo do loop. Vai por VARIÁVEL, nunca por flag: `--cache-dir`
    está na `GLOBAL_FLAGS` da cerca e o comando seria recusado.

    `env_file` (default: o ponteiro do ws) traz o env do projeto. O env do
    processo ganha do arquivo: quem exportou a var na mão está sobrescrevendo
    de propósito, e um `.env` esquecido não pode mudar o run silenciosamente.
    """
    from harness.projects import load_env_file

    venv = workspace / ".venv"
    env = dict(os.environ)
    do_projeto = load_env_file(env_file if env_file is not None else ws_env_file(workspace))
    for nome, valor in do_projeto.items():
        env.setdefault(nome, valor)
    cache = _data_dir() / "cache"
    for nome, sub in (("UV_CACHE_DIR", "uv"), ("npm_config_cache", "npm")):
        destino = cache / sub
        try:
            destino.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # sem cache o comando ainda roda
            warn(f"cache {sub} indisponível: {exc}")
        env[nome] = str(destino.resolve()) if destino.exists() else str(destino)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join([str(venv / "bin"), env.get("PATH", "")])
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    return env


class Blocked(Exception):
    """Comando recusado pela cerca. Vira output da tool, nunca crash do run."""


def _cerca(cmd: list[str], workspace: Path, cerca_argv0: bool = True) -> str | None:
    """Motivo do bloqueio, ou None. Fail-open se o `safe_shell` não importar.

    O import é lazy porque `safe_shell` puxa `deepagents`: este módulo tem que
    importar num ambiente sem o extra do executor. Sem a cerca disponível o
    comando passa — todos os comandos daqui são montados pelo harness, não pelo
    modelo, então a checagem é defesa em profundidade, não o único portão.

    `cerca_argv0=False` tira o PROGRAMA da checagem, nunca os argumentos: quando
    o binário é o linter/interpretador do venv DO HARNESS, o path é absoluto e
    fora do workspace por construção — a cerca o recusaria e a tool morreria em
    "caminho absoluto fora do workspace" por um path que o modelo não escolheu.
    Os argumentos (que é onde path de workspace e flag de instalador aparecem)
    continuam passando inteiros.
    """
    try:
        from harness.backends.safe_shell import check_command
    except Exception as exc:
        warn(f"cerca indisponível ({exc}); comando do harness segue")
        return None
    alvo = cmd if cerca_argv0 else cmd[1:]
    return check_command(" ".join(shlex.quote(part) for part in alvo), workspace)


def _run(
    cmd: list[str],
    workspace: Path,
    timeout: float,
    cerca_argv0: bool = True,
) -> subprocess.CompletedProcess:
    """Roda `cmd` no workspace com o env do venv. Levanta `Blocked` se a cerca recusar."""
    motivo = _cerca(cmd, workspace, cerca_argv0)
    if motivo:
        raise Blocked(f"bloqueado pela cerca: {motivo}")
    return subprocess.run(
        cmd,
        cwd=str(workspace),
        env=_env(workspace),
        shell=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _do_harness(prog: str, workspace: Path) -> bool:
    """True se o programa é binário resolvido pelo harness (absoluto, fora do ws).

    Só vale para argv[0] que ESTE módulo escolheu (`sys.executable`, o ruff do
    venv do harness). Comando vindo do modelo passa pela cerca inteiro.
    """
    if not os.path.isabs(prog):
        return False
    try:
        raiz = workspace.resolve()
        return raiz not in Path(prog).resolve().parents
    except OSError:
        return True


def _saida(proc: subprocess.CompletedProcess) -> str:
    return f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()


def _erro_filtrado(texto: str) -> str:
    """Últimas linhas que parecem erro; o resto do log é ruído para o modelo."""
    linhas = [linha for linha in texto.splitlines() if _RUIDO_ERRO.match(linha.strip())]
    if not linhas:  # nenhum marcador conhecido: as últimas linhas cruas servem
        linhas = [linha for linha in texto.splitlines() if linha.strip()]
    return "\n".join(linhas[-MAX_ERRO_LINHAS:])[:MAX_ERRO_CHARS]


# --------------------------------------------------------------------------- stack


def detect_stack(workspace: str | Path = ".") -> dict:
    """Que tipo de projeto é este. Pura: só olha arquivos, não roda nada."""
    ws = Path(workspace)
    pyproject = (ws / "pyproject.toml").is_file()
    requirements = (ws / "requirements.txt").is_file()
    package_json = ws / "package.json"
    locks = [nome for nome in LOCKFILES if (ws / nome).is_file()]

    scripts: dict[str, str] = {}
    test_script = None
    if package_json.is_file():
        try:
            dados = json.loads(package_json.read_text(encoding="utf-8"))
            bruto = dados.get("scripts")
            if isinstance(bruto, dict):
                scripts = {k: v for k, v in bruto.items() if isinstance(v, str)}
            test_script = scripts.get("test")
        except (OSError, ValueError) as exc:  # package.json quebrado não é stack node inválida
            warn(f"package.json ilegível: {exc}")

    return {
        "python": pyproject or requirements,
        "node": package_json.is_file(),
        "pyproject": pyproject,
        "requirements": requirements,
        "venv": (ws / ".venv" / "bin" / "python").is_file(),
        "lockfiles": locks,
        "gerenciador_node": _gerenciador_node(locks),
        "scripts": scripts,
        "test_script": test_script,
    }


def _gerenciador_node(locks: list[str]) -> str:
    """Lockfile manda no gerenciador; sem lock, npm."""
    for nome in ("pnpm-lock.yaml", "yarn.lock", "bun.lockb"):
        if nome in locks:
            return LOCKFILES[nome]
    return "npm"


# --------------------------------------------------------------------------- install


def _pacotes_python(workspace: Path) -> list[str]:
    try:
        proc = _run(["uv", "pip", "freeze"], workspace, STACK_TIMEOUT_S)
    except (Blocked, OSError, subprocess.SubprocessError):
        return []
    return sorted(linha.strip() for linha in (proc.stdout or "").splitlines() if linha.strip())


def _pacotes_node(workspace: Path) -> list[str]:
    try:
        proc = _run(["npm", "ls", "--depth=0", "--json"], workspace, STACK_TIMEOUT_S)
        dados = json.loads(proc.stdout or "{}")
    except (Blocked, OSError, ValueError, subprocess.SubprocessError):
        return []
    deps = dados.get("dependencies")
    if not isinstance(deps, dict):
        return []
    return sorted(f"{k}=={(v or {}).get('version', '?')}" for k, v in deps.items())


def _audita_install(workspace: Path, antes: list[str], depois: list[str], rotulo: str) -> None:
    """Delta de pacotes em append-only: um run que instalou deixa rastro."""
    from harness.redact import redact

    try:
        log = workspace / INSTALL_AUDIT
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        antes_set, depois_set = set(antes), set(depois)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{rotulo}\tantes={len(antes)}\tdepois={len(depois)}\n")
            for pacote in sorted(depois_set - antes_set):
                # Nome de pacote pode carregar URL com token (`pkg @ git+https://…`).
                fh.write(redact(f"{ts}\t{rotulo}\t+{pacote}\n"))
            for pacote in sorted(antes_set - depois_set):
                fh.write(redact(f"{ts}\t{rotulo}\t-{pacote}\n"))
    except OSError as exc:  # auditoria é diagnóstico, não pode derrubar a tool
        warn(f"install audit falhou: {exc}")


def install_deps(workspace: str | Path = ".", timeout: float = INSTALL_TIMEOUT_S) -> str:
    """Resolve as dependências do workspace com o gerenciador que o projeto usa.

    Timeout 600s em subprocess PRÓPRIO: `npm ci` frio não cabe nos 120s de teto
    do `SafeShellBackend.execute`, e é justamente esse comando que o modelo mais
    precisa rodar antes de qualquer teste.
    """
    ws = Path(workspace)
    stack = detect_stack(ws)
    if not stack["python"] and not stack["node"]:
        return "install_deps: nenhum manifesto (pyproject.toml, requirements.txt ou package.json)"

    partes: list[str] = []
    inicio = time.monotonic()
    if stack["python"]:
        partes.append(_install_python(ws, stack, timeout))
    if stack["node"]:
        partes.append(_install_node(ws, stack, timeout))
    return "\n".join(partes) + f"\ntotal sec={time.monotonic() - inicio:.1f}"


def _install_python(workspace: Path, stack: dict, timeout: float) -> str:
    antes = _pacotes_python(workspace) if stack["venv"] else []
    inicio = time.monotonic()
    try:
        if not stack["venv"]:
            proc = _run(["uv", "venv"], workspace, timeout)
            if proc.returncode != 0:
                return f"ok=false gerenciador=uv etapa=venv\n{_erro_filtrado(_saida(proc))}"
        alvo = ["-e", "."] if stack["pyproject"] else ["-r", "requirements.txt"]
        proc = _run(["uv", "pip", "install", *alvo], workspace, timeout)
    except Blocked as exc:
        return f"ok=false gerenciador=uv {exc}"
    except subprocess.TimeoutExpired:
        return f"ok=false gerenciador=uv timeout={timeout:.0f}s"
    except OSError as exc:
        return f"ok=false gerenciador=uv erro={exc}"

    depois = _pacotes_python(workspace)
    _audita_install(workspace, antes, depois, "python")
    sec = time.monotonic() - inicio
    if proc.returncode != 0:
        return (
            f"ok=false gerenciador=uv sec={sec:.1f}\n{_erro_filtrado(_saida(proc))}"
        )
    novos = len(set(depois) - set(antes))
    return f"ok=true gerenciador=uv pacotes={len(depois)} novos={novos} sec={sec:.1f}"


def _install_node(workspace: Path, stack: dict, timeout: float) -> str:
    gerenciador = stack["gerenciador_node"]
    antes = _pacotes_node(workspace)
    inicio = time.monotonic()
    primeiro = {
        "npm": ["npm", "ci"],
        "pnpm": ["pnpm", "install", "--frozen-lockfile"],
        "yarn": ["yarn", "install", "--frozen-lockfile"],
        "bun": ["bun", "install", "--frozen-lockfile"],
    }[gerenciador]
    fallback = {
        "npm": ["npm", "install"],
        "pnpm": ["pnpm", "install"],
        "yarn": ["yarn", "install"],
        "bun": ["bun", "install"],
    }[gerenciador]

    try:
        proc = _run(primeiro, workspace, timeout)
        saida = _saida(proc)
        if proc.returncode != 0 and _LOCK_RUIM.search(saida):
            # Lock ausente/dessincronizado: `install` regenera. Qualquer outro
            # erro é real e insistir só gasta os 600s de novo.
            proc = _run(fallback, workspace, timeout)
            saida = _saida(proc)
            primeiro = fallback
    except Blocked as exc:
        return f"ok=false gerenciador={gerenciador} {exc}"
    except subprocess.TimeoutExpired:
        return f"ok=false gerenciador={gerenciador} timeout={timeout:.0f}s"
    except OSError as exc:
        return f"ok=false gerenciador={gerenciador} erro={exc}"

    depois = _pacotes_node(workspace)
    _audita_install(workspace, antes, depois, gerenciador)
    sec = time.monotonic() - inicio
    cmd = " ".join(primeiro)
    if proc.returncode != 0:
        return f"ok=false gerenciador={gerenciador} cmd={cmd!r} sec={sec:.1f}\n{_erro_filtrado(saida)}"
    return (
        f"ok=true gerenciador={gerenciador} cmd={cmd!r} "
        f"pacotes={len(depois)} novos={len(set(depois) - set(antes))} sec={sec:.1f}"
    )


# --------------------------------------------------------------------------- parsers

# pytest: `FAILED tests/test_x.py::test_y - AssertionError: msg`
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:::(\S+))?(?:\s+-\s+(.*))?$")
# contagem da linha final: `=== 2 failed, 3 passed in 0.12s ===`
_PYTEST_CONTA = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed)")
# `tests/test_x.py:42: AssertionError` no corpo do traceback
_PYTEST_LINHA = re.compile(r"^(\S+\.py):(\d+):")

# jest/vitest: `FAIL src/a.test.ts > soma` / `✕ soma (2 ms)` / `Tests: 1 failed, 2 passed`
_JEST_TESTE = re.compile(r"^\s*[✕×]\s+\S")
_JEST_ARQUIVO = re.compile(r"^\s*FAIL\s+\S")
_JEST_SUMARIO = re.compile(r"(?im)^\s*(?:tests?|test files)\b:?\s*(.*)$")
_JEST_CONTA = re.compile(r"(?i)(\d+)\s+(passed|failed)")
# `❯ src/a.test.ts:7:24`, `at src/a.test.ts:7:24`, `src/a.test.ts:7`
_JEST_LOCAL = re.compile(r"([\w./\\-]+\.[jt]sx?):(\d+)")


def _corta(msg: str) -> str:
    msg = " ".join((msg or "").split())
    return msg[:MAX_MSG_CHARS]


def parse_pytest(out: str) -> dict:
    """Saída de `pytest -q` → contagem + até 5 falhas com file/line/test/msg. Pura."""
    resultado = {"passed": 0, "failed": 0, "errors": 0, "failures": []}
    linhas = (out or "").splitlines()

    for numero, rotulo in _PYTEST_CONTA.findall(out or ""):
        if rotulo == "passed":
            resultado["passed"] = int(numero)
        elif rotulo == "failed":
            resultado["failed"] = int(numero)
        elif rotulo.startswith("error"):
            resultado["errors"] = int(numero)

    # Linha do traceback mais próxima de cada FAILED: o arquivo:linha do erro.
    ultima_linha: dict[str, int] = {}
    for linha in linhas:
        casa = _PYTEST_LINHA.match(linha.strip())
        if casa:
            ultima_linha[casa.group(1)] = int(casa.group(2))

    for linha in linhas:
        casa = _PYTEST_FAILED.match(linha.strip())
        if not casa or len(resultado["failures"]) >= MAX_FALHAS:
            continue
        arquivo, teste, msg = casa.group(1), casa.group(2) or "", casa.group(3) or ""
        resultado["failures"].append(
            {
                "file": arquivo,
                "line": ultima_linha.get(arquivo, 0),
                "test": teste,
                "msg": _corta(msg),
            }
        )
    if not resultado["failed"] and resultado["failures"]:
        resultado["failed"] = len(resultado["failures"])
    return resultado


def parse_jest(out: str) -> dict:
    """Saída de jest/vitest → mesmo formato do `parse_pytest`. Pura."""
    resultado = {"passed": 0, "failed": 0, "errors": 0, "failures": []}
    texto = out or ""
    # A linha de sumário vem em dois formatos (`Tests: 1 failed, 2 passed` do
    # jest e `Tests  1 failed | 4 passed (5)` do vitest): pega-se a LINHA e
    # depois os pares dentro dela, senão o segundo par da linha se perde.
    for linha in _JEST_SUMARIO.findall(texto):
        for numero, rotulo in _JEST_CONTA.findall(linha):
            chave = "passed" if rotulo.lower() == "passed" else "failed"
            resultado[chave] = max(resultado[chave], int(numero))

    linhas = texto.splitlines()
    # `✕ nome do teste` é por TESTE e `FAIL arquivo` é por ARQUIVO: quando os
    # dois aparecem (vitest), reportar os dois duplicaria a mesma falha. O `✕`
    # ganha porque nomeia o teste; o `FAIL` só entra quando não há nenhum.
    marcados = [(i, linha) for i, linha in enumerate(linhas) if _JEST_TESTE.match(linha)]
    if not marcados:
        marcados = [(i, linha) for i, linha in enumerate(linhas) if _JEST_ARQUIVO.match(linha)]

    for indice, linha in marcados[:MAX_FALHAS]:
        titulo = linha.strip().lstrip("✕×").removeprefix("FAIL").strip()
        arquivo, numero_linha = _local_jest(titulo, linhas[indice + 1 :])
        resultado["failures"].append(
            {
                "file": arquivo,
                "line": numero_linha,
                "test": _corta(titulo.split(">")[-1].strip() if ">" in titulo else titulo),
                "msg": _corta(_msg_jest(linhas[indice + 1 : indice + 12])),
            }
        )
    if not resultado["failed"] and resultado["failures"]:
        resultado["failed"] = len(resultado["failures"])
    return resultado


def _local_jest(titulo: str, seguintes: list[str]) -> tuple[str, int]:
    """Arquivo:linha da falha: do próprio título (`FAIL a.test.ts`) ou do stack.

    O stack pode estar bem depois do marcador (o vitest lista os `✕` no resumo
    do arquivo e só imprime o `❯ arquivo:linha` no detalhe), então a busca não
    tem janela — a primeira referência a arquivo:linha depois do marcador vale.
    """
    primeiro = titulo.split(">")[0].strip()
    arquivo = primeiro if re.search(r"\.[jt]sx?$", primeiro) else ""
    for linha in seguintes:
        casa = _JEST_LOCAL.search(linha)
        if casa and (not arquivo or casa.group(1).endswith(arquivo)):
            return (arquivo or casa.group(1)), int(casa.group(2))
    return arquivo, 0


def _msg_jest(seguintes: list[str]) -> str:
    """Primeira linha depois do marcador que parece a mensagem do erro."""
    for linha in seguintes:
        limpa = linha.strip()
        if limpa.startswith(("AssertionError", "Error:", "→", "expected", "TypeError")):
            return limpa.lstrip("→ ")
    return ""


# --------------------------------------------------------------------------- tests


def _grava_log(workspace: Path, relativo: str, texto: str) -> str:
    """Grava o log completo e devolve o path COMO O MODELO O VÊ (fs virtual).

    Passa pela redação: log de teste é a evidência que humano e modelo leem, e
    teste que ecoa `Authorization:` não pode virar segredo em disco."""
    from harness.redact import redact

    destino = workspace / relativo
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(redact(texto), encoding="utf-8")
    except OSError as exc:
        warn(f"log {relativo} falhou: {exc}")
        return "(log indisponível)"
    return "/" + relativo


def run_tests(
    workspace: str | Path = ".",
    cmd: str | None = None,
    timeout: float = TEST_TIMEOUT_S,
) -> str:
    """Roda a suíte e devolve o veredito. Log completo em `.harness/tests.log`."""
    ws = Path(workspace)
    stack = detect_stack(ws)
    if cmd:
        argv, parser = shlex.split(cmd), None
    elif stack["python"]:
        python = ".venv/bin/python" if stack["venv"] else sys.executable
        argv, parser = [python, "-m", "pytest", "-q"], parse_pytest
    elif stack["node"] and stack["test_script"]:
        argv, parser = ["npm", "test", "--silent"], parse_jest
    else:
        return "run_tests: nenhuma suíte detectada (sem pytest e sem script test no package.json)"

    try:
        proc = _run(argv, ws, timeout, cerca_argv0=not (parser and _do_harness(argv[0], ws)))
    except Blocked as exc:
        return f"run_tests: {exc}"
    except subprocess.TimeoutExpired:
        return f"run_tests: timeout={timeout:.0f}s em {' '.join(argv)!r}"
    except OSError as exc:
        return f"run_tests: falha ao rodar {' '.join(argv)!r}: {exc}"

    saida = _saida(proc)
    log = _grava_log(ws, TESTS_LOG, saida)
    if parser is None:
        # Comando do modelo: não se adivinha o formato, o rc é o veredito.
        parser = parse_pytest if "pytest" in cmd else parse_jest
    dados = parser(saida)

    cabeca = (
        f"ok={'true' if proc.returncode == 0 else 'false'} cmd={' '.join(argv)!r} "
        f"passed={dados['passed']} failed={dados['failed']} errors={dados['errors']} log={log}"
    )
    if not dados["failures"]:
        if proc.returncode != 0:
            return f"{cabeca}\n{_erro_filtrado(saida)}"
        return cabeca
    detalhe = "\n".join(
        f"- {f['file']}:{f['line']} {f['test']}: {f['msg']}".rstrip(": ")
        for f in dados["failures"]
    )
    return f"{cabeca}\n{detalhe}"


# --------------------------------------------------------------------------- lint

_LINT_ALVO = "skipped:no-linter"
_ESLINT_CONFIGS = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)


def _ruff_argv(workspace: Path) -> list[str] | None:
    """Onde está o ruff, na ordem do mais específico ao mais genérico."""
    local = workspace / ".venv" / "bin" / "ruff"
    if local.is_file():
        return [str(local)]
    achado = shutil.which("ruff")
    if achado:
        return [achado]
    try:  # o ruff do venv DO HARNESS, via -m: não depende do PATH do run
        import importlib.util

        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
    except (ImportError, ValueError):
        pass
    return None


def _eslint_config(workspace: Path) -> bool:
    if any((workspace / nome).is_file() for nome in _ESLINT_CONFIGS):
        return True
    pkg = workspace / "package.json"
    if not pkg.is_file():
        return False
    try:
        return "eslintConfig" in json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def run_lint(workspace: str | Path = ".", fix: bool = False) -> str:
    """Lint do que existe no workspace. Sem linter resolvível não é erro."""
    ws = Path(workspace)
    stack = detect_stack(ws)
    partes: list[str] = []

    if stack["python"]:
        argv = _ruff_argv(ws)
        if argv is None:
            partes.append(f"ruff {_LINT_ALVO}")
        else:
            extra = ["check", "--output-format=concise", "."]
            if fix:
                extra.insert(1, "--fix")
            partes.append(_lint_run([*argv, *extra], ws, "ruff"))

    if stack["node"] and _eslint_config(ws):
        extra = ["--no-install", "eslint", ".", "-f", "compact"]
        if fix:
            extra.insert(3, "--fix")
        partes.append(_lint_run(["npx", *extra], ws, "eslint"))

    if not partes:
        return f"run_lint: {_LINT_ALVO} (nem stack python nem config de eslint)"
    return "\n".join(partes)


def _lint_run(argv: list[str], workspace: Path, rotulo: str) -> str:
    try:
        proc = _run(
            argv,
            workspace,
            LINT_TIMEOUT_S,
            cerca_argv0=not _do_harness(argv[0], workspace),
        )
    except Blocked as exc:
        return f"{rotulo}: {exc}"
    except subprocess.TimeoutExpired:
        return f"{rotulo}: timeout={LINT_TIMEOUT_S}s"
    except OSError as exc:
        return f"{rotulo}: falha ao rodar: {exc}"

    saida = _saida(proc)
    achados = [
        linha.strip()
        for linha in saida.splitlines()
        if re.match(r"^\S+:\d+", linha.strip()) or ": line " in linha
    ]
    if proc.returncode == 0 and not achados:
        return f"{rotulo} ok=true 0 erros"
    if not achados:  # rc!=0 sem achado parseável: erro do próprio linter
        return f"{rotulo} ok=false\n{_erro_filtrado(saida)}"
    cabeca = f"{rotulo} ok=false {len(achados)} erros"
    return cabeca + "\n" + "\n".join(achados[:MAX_LINT_LINHAS])


# --------------------------------------------------------------------------- screenshot


def _portas_registradas(workspace: Path) -> set[int]:
    """Portas de `.harness/procs.json` — contrato `{id,pid,port,...}` por processo.

    Fonte primária é o `procs.read_procs` (mesmo lock/rename que escreve o
    arquivo); o json cru é fallback para quando o módulo não importa. Só porta
    registrada entra: apontar o browser para porta arbitrária de `127.0.0.1` é a
    mesma classe de furo que a cerca de SSRF fecha no `web_fetch`.
    """
    try:
        from harness.backends import procs

        registros: object = procs.read_procs(workspace)
    except Exception:
        registros = None
    if registros is None:
        try:
            registros = json.loads((workspace / PROCS_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
    if isinstance(registros, dict):
        registros = registros.get("procs", registros.values())
    portas: set[int] = set()
    for registro in registros or []:
        porta = (registro or {}).get("port") if isinstance(registro, dict) else None
        if isinstance(porta, int):
            portas.add(porta)
        elif isinstance(porta, str) and porta.isdigit():
            portas.add(int(porta))
    return portas


def local_screenshot(
    port: int,
    path: str = "/",
    out: str = "shot.png",
    workspace: str | Path = ".",
) -> str:
    """PNG do app local em `127.0.0.1:port`. Só porta registrada em `.harness/procs.json`."""
    ws = Path(workspace)
    try:
        porta = int(port)
    except (TypeError, ValueError):
        return f"local_screenshot: porta inválida ({port!r})"

    registradas = _portas_registradas(ws)
    if porta not in registradas:
        conhecidas = ", ".join(str(p) for p in sorted(registradas)) or "nenhuma"
        return (
            f"local_screenshot: porta {porta} não está registrada em /{PROCS_JSON} "
            f"(registradas: {conhecidas}); suba o servidor pela tool de processo primeiro"
        )

    destino = (ws / out).resolve()
    try:
        if not (destino == ws.resolve() or ws.resolve() in destino.parents):
            return f"local_screenshot: {out!r} sai do workspace"
    except OSError as exc:
        return f"local_screenshot: destino inválido: {exc}"

    from harness import uiverify

    url = f"http://127.0.0.1:{porta}{path if path.startswith('/') else '/' + path}"
    motivo = uiverify.screenshot(url, destino)
    if motivo:
        return f"local_screenshot: {motivo}"
    tamanho = destino.stat().st_size if destino.is_file() else 0
    return f"ok=true url={url} arquivo=/{destino.relative_to(ws.resolve())} bytes={tamanho}"


# --------------------------------------------------------------------------- carga


def load_flow_tools(workspace: str | Path = ".") -> list:
    """Tools LangChain prontas para o backend. `[]` em QUALQUER falha."""
    try:
        from langchain_core.tools import tool

        @tool
        def install_deps_tool() -> str:
            """Instala as dependências do projeto com o gerenciador que ele já usa.

            Detecta pyproject.toml/requirements.txt (uv) e package.json (npm,
            pnpm ou yarn conforme o lockfile) e resolve tudo. Sem argumentos:
            quem manda é o manifesto do workspace, não um palpite seu.

            Exemplo: install_deps()
            """
            return install_deps(workspace)

        @tool
        def run_tests_tool(cmd: str | None = None) -> str:
            """Roda a suíte de testes e devolve quantos passaram e as falhas com arquivo:linha.

            Args:
                cmd: comando alternativo, se o default não servir. Omita para
                    usar `pytest -q` (projeto python) ou `npm test` (node).

            Exemplo: run_tests()
            Exemplo: run_tests(cmd="npm test -- --run src/soma.test.ts")
            O log completo fica em /.harness/tests.log — leia com read_file se
            as 5 falhas resumidas não bastarem.
            """
            return run_tests(workspace, cmd)

        @tool
        def run_lint_tool(fix: bool = False) -> str:
            """Roda o linter (ruff no python, eslint no node) e lista os erros com arquivo:linha.

            Args:
                fix: True para corrigir automaticamente o que o linter sabe corrigir.

            Exemplo: run_lint()
            Exemplo: run_lint(fix=True)
            `skipped:no-linter` significa que não há linter disponível — não é
            erro seu e não há o que consertar.
            """
            return run_lint(workspace, fix)

        @tool
        def local_screenshot_tool(port: int, path: str = "/", out: str = "shot.png") -> str:
            """Tira um screenshot PNG de uma página servida em 127.0.0.1 na porta dada.

            Args:
                port: porta do servidor local. Precisa estar registrada em
                    /.harness/procs.json (ou seja: subida pela tool de processo).
                path: caminho da página, começando com "/". Default "/".
                out: nome do arquivo PNG dentro do workspace. Default "shot.png".

            Exemplo: local_screenshot(port=5173)
            Exemplo: local_screenshot(port=3000, path="/login", out="login.png")
            """
            return local_screenshot(port, path, out, workspace)

        @tool
        def detect_stack_tool() -> str:
            """Diz que tipo de projeto é este: python, node, lockfiles e scripts do package.json.

            Só olha arquivos, não roda nada — barato, use antes de chutar comando.

            Exemplo: detect_stack()
            """
            return json.dumps(detect_stack(workspace), ensure_ascii=False, sort_keys=True)

        # `tool` usa o nome da função; o modelo tem que ver os nomes do manual.
        for func, nome in (
            (install_deps_tool, "install_deps"),
            (run_tests_tool, "run_tests"),
            (run_lint_tool, "run_lint"),
            (local_screenshot_tool, "local_screenshot"),
            (detect_stack_tool, "detect_stack"),
        ):
            func.name = nome
        return [
            install_deps_tool,
            run_tests_tool,
            run_lint_tool,
            local_screenshot_tool,
            detect_stack_tool,
        ]
    except Exception as exc:  # broad de propósito: fluxo é opcional, nunca derruba o run
        warn(f"falha ao carregar tools de fluxo: {exc}")
        return []


def __getattr__(name: str):
    """`FLOW_TOOLS` é lazy: LangChain não pode ser importado no topo deste módulo."""
    if name == "FLOW_TOOLS":
        return load_flow_tools()
    raise AttributeError(name)
