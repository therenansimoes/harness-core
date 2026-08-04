"""ui-verify: a régua de unidade que olha a TELA, não só o exit code do build.

O buraco que isto tapa é real e medido: um `dist/` cujo único stylesheet aponta
para um caminho morto sai 0 no `npm run build`, passa em todo `grep` do HTML e
levou ACCEPT — com a página chegando crua no navegador. Build verde não é prova
de tela viva.

O que este módulo faz, nesta ordem: serve o `dist/` numa porta efêmera de
loopback, pede a página, exige que TODO asset local citado no HTML responda 200
nesse mesmo servidor, tira um screenshot com Chrome headless e exige que o PNG
tenha tamanho de página com conteúdo. Tudo determinístico, zero LLM e zero
dependência nova — o navegador que já está na máquina basta (ADOÇÃO).

Calibração do `--min-kb` (Chrome 151, 1280x2000, os dois workspaces reais do
site da fazenda): página em branco 11.4kb · HTML cru sem CSS 32.0kb · página com
CSS 28.4kb. O default de 20kb separa "não renderizou nada" de "renderizou", que
é o que o número consegue afirmar honestamente — quem distingue estilizado de
cru é o check de asset (e, opt-in, o `--ask`).

O Chrome do macOS ESCREVE o PNG e não sai (medido em 151.0.7922.71: o arquivo
fica pronto em ~2.1s e o processo segue vivo até ser morto). Por isso a espera
aqui não é `subprocess.run(timeout=...)`: é aguardar o PNG ficar COMPLETO (magic
number + chunk IEND) e então matar o processo. Um check que trava um minuto por
causa disso não seria usado por ninguém.

Não toda referência morta é tela morta, e confundir as duas custou run reprovado
em produção: numa fila progressiva o nav da unidade 1 já aponta para as quatro
páginas do site, e u5/u6 é que as criam. O `<a href>` para uma página que ainda
não existe é buraco de COMPLETUDE (job da unidade final), não de renderização —
vira aviso. Quebra renderização o que o navegador carrega junto com a página:
`<script src>`, `<link>` de stylesheet/ícone e `<img src>`; esses continuam
reprovando. Quem quer o gate completo pede `--strict-links`.

O `--ask` é opt-in por unidade e custa alguns centavos: manda o screenshot pro
Claude CLI em haiku e exige um JSON. Sem ele, nenhum token é gasto. Isto continua
sendo verify de UNIDADE — quem escolhe a régua é o autor da unidade —, não juiz
do loop de improve.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

SHOT_NAME = "ui-verify.png"
DEFAULT_MIN_KB = 20.0
ASSET_KINDS = ("css", "js")

# `rel` de `<link>` que o navegador busca ao pintar a página. `canonical`,
# `alternate` e afins descrevem o site para robô: morto ali não é tela morta.
RENDER_RELS = frozenset(
    {"stylesheet", "icon", "shortcut", "apple-touch-icon", "mask-icon", "preload",
     "manifest"}
)
MISSING_PAGE = "pagina linkada ausente"

SHOT_TIMEOUT_S = 30.0
ASSET_TIMEOUT_S = 5.0
ASK_TIMEOUT_S = 60.0
POLL_S = 0.2

# Ordem de busca do navegador. O caminho do bundle do macOS vem primeiro porque
# lá o Chrome não está no PATH; depois os nomes usados em Linux/CI.
CHROME_ENV = "HARNESS_CHROME"
CHROMES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "chromium",
    "google-chrome",
    "chromium-browser",
    "google-chrome-stable",
)
MISSING_CHROME = (
    f"Chrome/Chromium não encontrado (tentado: {CHROME_ENV}, "
    "/Applications/Google Chrome.app, chromium, google-chrome)"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"

ASK_CLI = "claude"
ASK_MODEL = "haiku"
ASK_PERMISSION_MODE = "acceptEdits"
ASK_SHAPE = '{"ok": bool, "motivo": str}'

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_REF = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']*)["']""", re.I)
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']*)["']""", re.I)
_IMG_SRC = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']*)["']""", re.I)
_STYLE_INLINE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_ATTR = re.compile(r"""\b([\w:-]+)\s*=\s*["']([^"']*)["']""")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```$", re.S)


@dataclass(frozen=True)
class Result:
    """O veredito e a evidência que o sustenta."""

    failures: tuple[str, ...]
    shot: Path | None
    shot_kb: float
    checked: int
    ok_assets: int
    # Referência morta que não impede a página de renderizar: sai no relatório,
    # não no exit code.
    warnings: tuple[str, ...] = ()


def verify(
    dist: Path | str,
    *,
    url_path: str = "/",
    min_kb: float = DEFAULT_MIN_KB,
    expect: tuple[str, ...] = (),
    shot_out: Path | str | None = None,
    ask: str | None = None,
    strict_links: bool = False,
) -> Result:
    """Roda a régua inteira sobre um `dist/` e devolve todas as falhas.

    Todas, não a primeira: quem lê o relatório quer saber que o CSS morreu E que
    a imagem some junto, não descobrir uma por vez a cada rodada.

    `strict_links=True` volta ao gate completo, em que `<a href>` local morto
    também reprova — útil para a unidade FINAL de uma fila, que é a dona da
    completude do site.
    """
    root = Path(dist).resolve()
    shot = Path(shot_out).resolve() if shot_out else Path.cwd() / SHOT_NAME
    if not root.is_dir():
        return Result((f"dist não é diretório: {root}",), None, 0.0, 0, 0)

    with serve(root) as base:
        page = urljoin(base, url_path.lstrip("/"))
        status, body = _get(page)
        if status != 200:
            return Result((f"página {url_path} respondeu {_status(status)}",), None, 0.0, 0, 0)
        html = body.decode("utf-8", "replace")

        # Um GET por referência: os três checks abaixo leem o MESMO mapa, senão a
        # mesma folha de estilo é baixada três vezes por rodada.
        fetched = {ref: _get(urljoin(page, ref)) for ref in _refs(html)}
        # Uma referência que aparece nas duas classes (o mesmo caminho em `<img
        # src>` e em `<a href>`) é asset: quem carrega junto com a página manda.
        assets = _asset_refs(html)
        dead = [ref for ref, (status, _) in fetched.items() if status != 200]
        failures = [
            f"asset {_status(fetched[ref][0])}: {ref}"
            for ref in dead
            if strict_links or ref in assets
        ]
        warnings = [
            f"{MISSING_PAGE}: {ref}"
            for ref in dead
            if not strict_links and ref not in assets
        ]
        failures += _check_expected(html, expect, fetched)
        shot_kb, shot_fail = _check_shot(page, shot, min_kb)
        failures += shot_fail
        checked = len(fetched)
        ok_assets = sum(1 for status, _ in fetched.values() if status == 200)

    if ask and shot.is_file():
        failures += _check_ask(shot, ask)
    return Result(
        tuple(failures),
        shot if shot.is_file() else None,
        shot_kb,
        checked,
        ok_assets,
        tuple(warnings),
    )


# --------------------------------------------------------------------------- servidor


@contextmanager
def serve(root: Path) -> Iterator[str]:
    """Serve `root` em porta efêmera de loopback enquanto o bloco roda.

    Loopback e porta 0 por design: a régua não abre porta previsível para a rede
    só para conferir um build.
    """
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        """Log de acesso aqui é ruído no meio do relatório do verify."""


def _get(url: str) -> tuple[int, bytes]:
    """(status, corpo). Status 0 = não respondeu (conexão, timeout, URL torta)."""
    try:
        with urllib.request.urlopen(url, timeout=ASSET_TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b""


def _status(code: int) -> str:
    return "sem resposta" if code == 0 else str(code)


# --------------------------------------------------------------------------- assets


def _refs(html: str) -> list[str]:
    """Referências locais únicas, na ordem em que aparecem no HTML.

    Externo (`https:`, `//cdn`, `mailto:`) fica de fora: a régua responde pelo
    que o build gerou, não pela disponibilidade da internet de quem rodou.
    """
    seen: dict[str, None] = {}
    for ref in _REF.findall(html):
        if _is_local(ref):
            seen.setdefault(ref, None)
    return list(seen)


def _is_local(ref: str) -> bool:
    ref = ref.strip()
    if not ref or ref.startswith("#") or ref.startswith("//"):
        return False
    return not _SCHEME.match(ref)


def _asset_refs(html: str) -> set[str]:
    """Referências locais cujo 404 quebra a RENDERIZAÇÃO da página.

    O resto (`<a href>` para outra página, `rel=canonical`) o navegador só busca
    se alguém clicar — ausência ali é completude de site, não tela crua.
    """
    refs = _SCRIPT_SRC.findall(html) + _IMG_SRC.findall(html) + _render_links(html)
    return {ref for ref in refs if _is_local(ref)}


def _render_links(html: str) -> list[str]:
    hrefs = []
    for tag in _LINK.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        rel = attrs.get("rel", "").lower().split()
        if attrs.get("href") and RENDER_RELS.intersection(rel):
            hrefs.append(attrs["href"])
    return hrefs


def _stylesheets(html: str) -> list[str]:
    hrefs = []
    for tag in _LINK.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        rel = attrs.get("rel", "").lower().split()
        if "stylesheet" in rel and attrs.get("href"):
            hrefs.append(attrs["href"])
    return hrefs


def _check_expected(
    html: str, expect: tuple[str, ...], fetched: dict[str, tuple[int, bytes]]
) -> list[str]:
    """`--expect-asset css` exige ≥1 stylesheet que CARREGA — o `<link>` existir
    não conta, foi exatamente assim que o dist quebrado passou.

    CSS embutido em `<style>` conta: o Astro inlina folha pequena por default, e
    uma régua que reprova build legítimo é removida na primeira semana.
    """
    out = []
    for kind in expect:
        if kind == "css":
            inline = any(block.strip() for block in _STYLE_INLINE.findall(html))
            if not inline and not _any_loads(_stylesheets(html), fetched):
                out.append("css: nenhum stylesheet carregável (nem <style> inline)")
        elif kind == "js":
            if not _any_loads(_SCRIPT_SRC.findall(html), fetched):
                out.append("js: nenhum script carregável")
    return out


def _any_loads(refs: list[str], fetched: dict[str, tuple[int, bytes]]) -> bool:
    """200 com corpo. Asset de 0 byte é link vivo apontando para o vazio."""
    return any(
        fetched.get(ref, (0, b""))[0] == 200 and fetched.get(ref, (0, b""))[1]
        for ref in refs
        if _is_local(ref)
    )


# --------------------------------------------------------------------------- screenshot


def _check_shot(page: str, shot: Path, min_kb: float) -> tuple[float, list[str]]:
    err = screenshot(page, shot)
    if err:
        return 0.0, [err]
    kb = shot.stat().st_size / 1024
    if kb < min_kb:
        return kb, [f"screenshot {kb:.1f}kb < {min_kb:.1f}kb (tela provavelmente vazia)"]
    return kb, []


def chrome() -> str | None:
    """Indireção também para o teste trocar o navegador por um script fake."""
    override = os.environ.get(CHROME_ENV)
    if override:
        return override if Path(override).is_file() else shutil.which(override)
    for cand in CHROMES:
        if Path(cand).is_file():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def screenshot(url: str, out: Path, timeout_s: float = SHOT_TIMEOUT_S) -> str | None:
    """Screenshot da `url` em `out`. Devolve o motivo da falha, ou None se deu.

    Não espera o Chrome sair — ver o docstring do módulo: no macOS ele escreve o
    PNG e continua vivo. O sinal de pronto é o próprio arquivo estar íntegro.
    """
    exe = chrome()
    if exe is None:
        return MISSING_CHROME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    argv = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--screenshot={out}",
        "--window-size=1280,2000",
        url,
    ]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return f"screenshot: {type(exc).__name__}: {exc}"
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _png_ready(out):
                return None
            if proc.poll() is not None:
                # Saiu sem PNG íntegro: mais uma olhada (pode ter escrito no fim)
                # e então é falha de verdade.
                return None if _png_ready(out) else f"screenshot: chrome saiu {proc.returncode} sem PNG"
            time.sleep(POLL_S)
        return f"screenshot: sem PNG completo em {timeout_s:.0f}s"
    finally:
        proc.kill()
        proc.wait()


def _png_ready(path: Path) -> bool:
    """PNG COMPLETO: assinatura no início e chunk IEND no fim.

    Tamanho estável seria palpite; IEND é o fim do formato. A diferença aparece
    quando o PNG é lido enquanto o Chrome ainda escreve.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return data.startswith(PNG_MAGIC) and data.endswith(PNG_IEND)


# --------------------------------------------------------------------------- --ask


def _check_ask(shot: Path, question: str) -> list[str]:
    exe = shutil.which(ASK_CLI)
    if exe is None:
        return [f"--ask: {ASK_CLI} CLI não encontrado"]
    prompt = (
        f"A tela renderizada está na imagem `{shot.name}` do diretório atual — "
        f"leia-a com a tool Read antes de responder.\n\n"
        f"Pergunta: {question}\n\n"
        f"Responda SOMENTE com JSON no formato {ASK_SHAPE}, sem texto em volta."
    )
    argv = [
        exe,
        "-p",
        "--model",
        ASK_MODEL,
        "--safe-mode",
        "--permission-mode",
        ASK_PERMISSION_MODE,
        "--tools",
        "Read",
        # Variádico no CLI: fica por último para não engolir outro argumento.
        "--add-dir",
        str(shot.parent),
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(shot.parent),
            input=prompt,  # prompt por stdin, igual ao backend claude_code
            capture_output=True,
            text=True,
            timeout=ASK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [f"--ask: sem resposta em {ASK_TIMEOUT_S:.0f}s"]
    except OSError as exc:
        return [f"--ask: {type(exc).__name__}: {exc}"]
    if proc.returncode != 0:
        return [f"--ask: {ASK_CLI} saiu {proc.returncode}: {proc.stderr.strip()[:200]}"]
    verdict = parse_ask(proc.stdout)
    if verdict is None:
        return [f"--ask: resposta não é {ASK_SHAPE}: {proc.stdout.strip()[:200]}"]
    ok, motivo = verdict
    return [] if ok else [f"--ask: {motivo}"]


def parse_ask(stdout: str) -> tuple[bool, str] | None:
    """JSON estrito, exceto pela cerca de código — que é formatação, não resposta
    (o haiku devolve ```json ... ``` mesmo mandado responder só o JSON). Qualquer
    outra coisa é None, e None é falha: régua que adivinha não é régua."""
    text = (stdout or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    ok, motivo = obj.get("ok"), obj.get("motivo")
    if not isinstance(ok, bool) or not isinstance(motivo, str):
        return None
    return ok, motivo
