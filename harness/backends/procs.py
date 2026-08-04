"""Processos de vida longa do run: subir servidor, sondar, matar tudo no fim.

`execute` é síncrono e com timeout curto por desenho — `npm run dev` nele é
comando pendurado que queima o orçamento inteiro do run. Servidor entra por
aqui: sobe em background, o registro de quem subiu mora em
`<ws>/.harness/procs.json`, e o nó terminal do grafo mata o que ficou vivo.

Três decisões que este módulo carrega:

- **`start_new_session=True` é obrigatório.** `npm run dev` é um wrapper que
  gera filho; matar só o pai deixa o servidor real órfão segurando a porta (e
  o `git worktree remove --force` do dispose falhando). Sessão nova = process
  group próprio = `killpg` alcança a árvore toda.
- **Porta é alocada, nunca fixa.** Runs paralelos no mesmo host colidiriam em
  `:3000` e o segundo leria a resposta do primeiro — falso verde silencioso.
- **`local_probe` é a cerca OPOSTA à do `web_fetch`.** Aqui só loopback, e só
  porta registrada por ESTE workspace; lá, loopback é justamente o proibido.

Sem import de LangChain no topo: `run_graph` e `provision` importam este módulo
no caminho normal, e nenhum dos dois deve puxar o mundo do agente.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HARNESS_SUBDIR = ".harness"
PROCS_FILE = "procs.json"
LOCK_FILE = "procs.lock"

LOOPBACK = "127.0.0.1"

# Readiness: um dev server de node leva ~5-10s para abrir a porta; acima de 30s
# é servidor que não vai subir, e o run tem mais o que fazer.
DEFAULT_TIMEOUT = 30
POLL_S = 0.5
# Log de um servidor que morreu no boot: o erro está nas últimas linhas.
CRASH_LOG_LINES = 15
# Resposta da sonda: 20k é página de app inteira; acima disso é despejo.
MAX_PROBE_BYTES = 20_000
# SIGTERM primeiro (o server fecha socket e salva), SIGKILL depois.
TERM_GRACE_S = 3.0
PROBE_TIMEOUT_S = 10
PROBE_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH")


# --------------------------------------------------------------------------- #
# registro em .harness/procs.json
# --------------------------------------------------------------------------- #


def _harness_dir(ws: Path) -> Path:
    path = ws / HARNESS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def procs_path(ws: str | Path) -> Path:
    return Path(ws) / HARNESS_SUBDIR / PROCS_FILE


def read_procs(ws: str | Path) -> list[dict]:
    """Registro do workspace, ou lista vazia. Fail-open: json corrompido não
    derruba o cleanup — pior que perder o registro é o run morrer no record."""
    try:
        raw = procs_path(ws).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _write_procs(ws: Path, entries: list[dict]) -> None:
    """tmp + rename sob flock: leitor concorrente nunca vê arquivo meio escrito."""
    target = procs_path(ws)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


class _Registry:
    """`with _Registry(ws) as reg:` — entries sob flock, gravadas na saída.

    O lock é arquivo separado do dado: `procs.json` é substituído por rename, e
    flock no inode antigo não protege o novo.
    """

    def __init__(self, ws: Path) -> None:
        self.ws = ws
        self.entries: list[dict] = []
        self._fh = None

    def __enter__(self) -> _Registry:
        self._fh = open(_harness_dir(self.ws) / LOCK_FILE, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except OSError:
            pass  # filesystem sem flock: seguir sem lock é melhor que travar o run
        self.entries = read_procs(self.ws)
        return self

    def __exit__(self, *exc) -> None:
        try:
            _write_procs(self.ws, self.entries)
        except OSError:
            pass
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()


# --------------------------------------------------------------------------- #
# porta
# --------------------------------------------------------------------------- #


def alloc_port() -> int:
    """Porta livre pedida ao kernel (bind em :0 e solta).

    Há uma janela entre soltar e o servidor bindar; é o mesmo truque que todo
    test runner usa, e é o preço de não ter porta fixa colidindo entre runs.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


def start(
    ws: str | Path,
    command: str,
    port: int | None = None,
    wait_path: str = "/",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Sobe `command` em background e espera a porta responder.

    Devolve o registro do processo com `status`: `ready` (a porta respondeu),
    `crashed` (morreu antes, com `log_tail`) ou `timeout` (vivo mas mudo — fica
    registrado, o cleanup mata). `blocked` quando a cerca do shell recusa.
    """
    # Lazy: safe_shell puxa deepagents (e LangChain); este módulo é importado
    # por run_graph/provision, que não devem carregar o mundo do agente.
    from harness.backends.safe_shell import check_command

    workspace = Path(ws)
    reason = check_command(command, workspace)
    if reason:
        return {"status": "blocked", "command": command, "reason": reason}

    if port is None:
        port = alloc_port()
    proc_id = uuid.uuid4().hex[:8]
    log_path = _harness_dir(workspace) / f"proc-{proc_id}.log"

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"status": "blocked", "command": command, "reason": f"comando não parseia: {exc}"}
    if not argv:
        return {"status": "blocked", "command": command, "reason": "comando vazio"}

    log = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            env={**os.environ, "PORT": str(port)},
            stdout=log,
            stderr=log,
            # Sessão nova: `npm run dev` gera filho, e matar só o pai deixa o
            # servidor real órfão segurando a porta.
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        log.close()
        return {
            "status": "crashed",
            "command": command,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        # O filho já herdou o fd; o nosso não serve para mais nada.
        log.close()

    entry = {
        "id": proc_id,
        "pid": proc.pid,
        "pgid": _pgid_of(proc.pid),
        "port": port,
        "command": command,
        "run_id": workspace.name,
        "started_ts": time.time(),
        "log": str(log_path),
        "harness_pid": os.getpid(),
    }
    with _Registry(workspace) as reg:
        reg.entries.append(dict(entry))

    deadline = time.time() + max(1, int(timeout))
    while True:
        code = proc.poll()
        if code is not None:
            _forget(workspace, proc_id)
            return {
                **entry,
                "status": "crashed",
                "exit_code": code,
                "log_tail": _tail(log_path),
            }
        if _responde(port, wait_path):
            return {**entry, "status": "ready"}
        if time.time() >= deadline:
            # Vivo mas mudo: fica registrado de propósito, o cleanup mata.
            return {**entry, "status": "timeout", "log_tail": _tail(log_path)}
        time.sleep(POLL_S)


def _pgid_of(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except OSError:
        return pid  # start_new_session garante pgid == pid


def _tail(log_path: Path, lines: int = CRASH_LOG_LINES) -> str:
    try:
        return "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _responde(port: int, path: str) -> bool:
    """True se a porta devolveu QUALQUER resposta HTTP.

    404 e 500 são servidor no ar — readiness é sobre o socket atender, não
    sobre a rota existir.
    """
    try:
        urllib.request.urlopen(_url(port, path), timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _url(port: int, path: str) -> str:
    path = path if str(path).startswith("/") else f"/{path}"
    return f"http://{LOOPBACK}:{port}{path}"


# --------------------------------------------------------------------------- #
# local_probe
# --------------------------------------------------------------------------- #


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect não é seguido: o 302 do server poderia apontar para fora da
    cerca, e o hop seguinte não passaria por validação nenhuma."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_probe(ws: str | Path, port: int, path: str = "/", method: str = "GET") -> str:
    """web_fetch = mundo (loopback proibido); local_probe = servidores DESTA
    run (só loopback + porta registrada). Cercas opostas de propósito."""
    workspace = Path(ws)
    method = str(method or "GET").upper()
    if method not in PROBE_METHODS:
        return f"local_probe recusado: método {method!r} não suportado"

    entry = next((e for e in read_procs(workspace) if _as_int(e.get("port")) == _as_int(port)), None)
    if entry is None:
        return (
            f"local_probe recusado: porta {port} não está registrada neste workspace. "
            "Só servidores subidos por start_server nesta run são sondáveis "
            "(use start_server e a porta que ele devolveu)."
        )
    if not _vivo(_as_int(entry.get("pid"))):
        return f"local_probe recusado: o processo da porta {port} não está mais vivo (id={entry.get('id')})"

    url = _url(_as_int(port), path)
    req = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener(_SemRedirect)
    try:
        with opener.open(req, timeout=PROBE_TIMEOUT_S) as resp:
            status, ctype, corpo = resp.status, resp.headers.get("Content-Type", ""), resp.read(MAX_PROBE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # 3xx cai aqui (redirect não seguido) junto com 4xx/5xx: é resposta do
        # servidor, e o modelo precisa ler o status.
        status, ctype, corpo = exc.code, exc.headers.get("Content-Type", ""), exc.read(MAX_PROBE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"local_probe falhou em {url}: {type(exc).__name__}: {exc}"

    truncou = len(corpo) > MAX_PROBE_BYTES
    texto = corpo[:MAX_PROBE_BYTES].decode("utf-8", "replace")
    if truncou:
        texto += f"\n\n[truncado em {MAX_PROBE_BYTES} bytes]"
    return f"{method} {url} -> {status} {ctype}\n\n{texto}"


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


# --------------------------------------------------------------------------- #
# stop / kill_all
# --------------------------------------------------------------------------- #


def stop(ws: str | Path, id: str) -> int:
    """Mata um processo registrado. Devolve quantos morreram (0 ou 1)."""
    workspace = Path(ws)
    mortos = 0
    with _Registry(workspace) as reg:
        restantes = []
        for entry in reg.entries:
            if entry.get("id") != id:
                restantes.append(entry)
                continue
            if _mata(entry):
                mortos += 1
        reg.entries = restantes
    return mortos


def kill_all(ws: str | Path) -> int:
    """Mata TODOS os processos registrados no workspace e zera o registro.

    Nunca levanta: é chamada no nó terminal do grafo e no dispose, e falhar aqui
    transformaria um run concluído em run quebrado.
    """
    try:
        workspace = Path(ws)
        if not procs_path(workspace).is_file():
            return 0
        mortos = 0
        with _Registry(workspace) as reg:
            for entry in reg.entries:
                if _mata(entry):
                    mortos += 1
            reg.entries = []
        return mortos
    except Exception:
        return 0


def _mata(entry: dict) -> bool:
    """SIGTERM no grupo, 3s, SIGKILL. False quando não havia o que matar."""
    pid, pgid = _as_int(entry.get("pid")), _as_int(entry.get("pgid"))
    if pid <= 0 or pgid <= 0:
        return False
    try:
        # Janela conhecida de PID reuse: entre o processo morrer e chegarmos
        # aqui, o SO pode ter dado o mesmo pid a outro processo. Conferir o pgid
        # fecha o caso comum (pid novo cai no grupo de quem o criou, não no
        # nosso); o caso patológico (pid E pgid reusados juntos) não tem defesa
        # barata, e o registro é apagado logo depois.
        if os.getpgid(pid) != pgid:
            return False
    except OSError:
        return False  # já morreu
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + TERM_GRACE_S
    while time.time() < deadline:
        if not _vivo(pid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    _vivo(pid)  # colhe o zumbi que o SIGKILL deixou
    return True


def _vivo(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        # Filho nosso morto vira zumbi, e zumbi responde a `kill -0`: colher
        # antes é o que faz "morreu" significar morreu.
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _forget(ws: Path, proc_id: str) -> None:
    with _Registry(ws) as reg:
        reg.entries = [e for e in reg.entries if e.get("id") != proc_id]


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def make_proc_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado.

    Erro é string de retorno, nunca exceção: exceção em tool node derruba o run.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def start_server(command: str, wait_path: str = "/", timeout: int = DEFAULT_TIMEOUT) -> str:
        """Sobe um servidor em background e espera a porta responder."""
        try:
            info = start(base, command, wait_path=wait_path, timeout=timeout)
        except Exception as exc:
            return f"start_server falhou: {type(exc).__name__}: {exc}"
        status = info.get("status")
        if status == "blocked":
            return f"start_server bloqueado pela cerca do harness: {info.get('reason')}"
        if status == "crashed":
            tail = info.get("log_tail") or info.get("reason") or ""
            return (
                f"start_server: o processo morreu antes de responder "
                f"(exit={info.get('exit_code')}). Últimas linhas do log:\n{tail}"
            )
        if status == "timeout":
            return (
                f"start_server: porta {info.get('port')} não respondeu em {timeout}s "
                f"(id={info.get('id')}, ainda vivo). Log:\n{info.get('log_tail') or ''}"
            )
        return (
            f"start_server ok: id={info.get('id')} porta={info.get('port')} "
            f"log={info.get('log')}. Sonde com local_probe(port={info.get('port')})."
        )

    def probe(port: int, path: str = "/", method: str = "GET") -> str:
        """Faz uma requisição a um servidor desta run (só loopback)."""
        try:
            return local_probe(base, port, path, method)
        except Exception as exc:
            return f"local_probe falhou: {type(exc).__name__}: {exc}"

    def stop_server(id: str) -> str:
        """Mata um servidor subido por start_server."""
        try:
            return f"stop_server: {stop(base, id)} processo(s) morto(s) (id={id})"
        except Exception as exc:
            return f"stop_server falhou: {type(exc).__name__}: {exc}"

    return [
        StructuredTool.from_function(
            func=start_server,
            name="start_server",
            description=(
                "Sobe um servidor de vida longa em background (npm run dev, uvicorn, "
                "python -m http.server) e espera a porta responder. NÃO use execute para "
                "isso: execute é síncrono e o comando pendura até o timeout. A porta é "
                "escolhida pelo harness e exportada como $PORT no ambiente do comando — "
                "não fixe 3000. Devolve id e porta; sonde com local_probe."
            ),
        ),
        StructuredTool.from_function(
            func=probe,
            name="local_probe",
            description=(
                "Requisição HTTP a um servidor subido nesta run (só 127.0.0.1 e só porta "
                "registrada por start_server). É a tool para VERIFICAR que a página/rota "
                "responde. Para a internet pública use web_fetch; loopback lá é bloqueado."
            ),
        ),
        StructuredTool.from_function(
            func=stop_server,
            name="stop_server",
            description=(
                "Mata um servidor subido por start_server (o id que ele devolveu), junto "
                "com os processos filhos. Servidores restantes morrem no fim do run."
            ),
        ),
    ]


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_PROBE_BYTES",
    "alloc_port",
    "kill_all",
    "local_probe",
    "make_proc_tools",
    "procs_path",
    "read_procs",
    "start",
    "stop",
]
