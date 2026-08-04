"""Tools de arquivo de precisão para o executor: outline, edição por faixa de
linhas, inserção e append.

Motivo de existir: `read_file`/`edit_file` bastam em arquivo pequeno. Em
arquivo grande o modelo pequeno queima o contexto lendo tudo e ainda erra o
`old_string` byte a byte. `file_outline` devolve o mapa em vez do conteúdo, e
`edit_range` edita pelo NÚMERO da linha — o mesmo número que o `read_file`
mostra na margem —, o que remove o casamento exato da equação.

Contrato de tudo neste módulo:
- jail: o path é resolvido SOB a raiz do workspace; escapar volta erro;
- escrita atômica (tmp + `os.replace`) com backup do anterior em
  `.harness/edits/` (ring de 50);
- validação por extensão DEPOIS de escrever (.py, .json, .toml) e REVERT do
  backup byte a byte se o arquivo ficou inválido — o loop autônomo não deixa
  arquivo quebrado no workspace;
- erro é STRING de retorno, nunca exceção: a tool conversa com o modelo, e
  exceção em tool node derruba o run inteiro;
- gate READ-BEFORE-WRITE: editar arquivo existente exige leitura fresca dele
  (`smart_fs.needs_fresh_read`, o mesmo registro que o `read_file` alimenta).
  O gate mora na borda da tool, não em `replace_range`/`insert_after`/`append` —
  essas continuam sendo API de edição pura, chamável sem cerimônia.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path

# Cap do outline: acima disso o mapa deixa de ser mapa e volta a ser despejo.
MAX_OUTLINE_ENTRIES = 200
# Ring de backups por workspace. 50 edits é mais que um run inteiro.
BACKUP_RING = 50
# Linhas renumeradas devolvidas depois de uma escrita, a partir do ponto editado.
CONTEXT_LINES = 3

_PY_SKELETON = re.compile(r"^\s*(@|class\s|def\s|async\s+def\s)")
_MD_HEADING = re.compile(r"^#{1,6}\s")
_TOML_SECTION = re.compile(r"^\s*\[")
_JS_SKELETON = re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?(function\b|class\b|const\s+\w+\s*=)")

_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


class _Blocked(Exception):
    """Path fora do jail. Convertida em string de erro na borda da tool."""


def make_file_tools(root: str | Path) -> list:
    """Devolve as tools LangChain deste módulo com o jail fixado em `root`.

    Nomes NOVOS de propósito: `tools=` do `create_deep_agent` é aditivo e
    colisão de nome é last-wins SILENCIOSO — reusar `read_file` aqui apagaria
    a tool do FilesystemMiddleware sem um único aviso.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(root)

    def file_outline(path: str) -> str:
        """Esqueleto de um arquivo (definições, headings ou seções) sem o corpo."""
        return outline(base, path)

    def edit_range(
        path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        expect_first_line: str | None = None,
    ) -> str:
        """Substitui as linhas start_line..end_line (1-indexado, inclusivo)."""
        gate = _read_gate(base, path)
        if gate is not None:
            return gate
        out = replace_range(base, path, start_line, end_line, new_content, expect_first_line)
        _mark_written(base, path)
        return out

    def insert_lines(path: str, after_line: int, content: str) -> str:
        """Insere `content` DEPOIS da linha `after_line` (0 = topo do arquivo)."""
        gate = _read_gate(base, path)
        if gate is not None:
            return gate
        out = insert_after(base, path, after_line, content)
        _mark_written(base, path)
        return out

    def append_file(path: str, content: str) -> str:
        """Acrescenta `content` no fim do arquivo (cria se não existir)."""
        gate = _read_gate(base, path)
        if gate is not None:
            return gate
        out = append(base, path, content)
        _mark_written(base, path)
        return out

    return [
        StructuredTool.from_function(
            func=file_outline,
            name="file_outline",
            description=(
                "Mapa de um arquivo sem ler o conteúdo: classes/defs (.py), headings (.md), "
                "seções (.toml), chaves de nível 1-2 (.json), function/class/const (.js/.ts). "
                "Termina com o total de linhas e o tamanho. Use ANTES de read_file em arquivo grande."
            ),
        ),
        StructuredTool.from_function(
            func=edit_range,
            name="edit_range",
            description=(
                "Substitui um intervalo de linhas por outro conteúdo. start_line/end_line são "
                "1-indexados e inclusivos, os mesmos números que read_file mostra na margem. "
                "Passe expect_first_line com o texto exato da linha start_line para confirmar o "
                "alvo. Escrita atômica, com backup e revert se o arquivo ficar inválido."
            ),
        ),
        StructuredTool.from_function(
            func=insert_lines,
            name="insert_lines",
            description=(
                "Insere linhas novas sem apagar nada, depois da linha after_line "
                "(after_line=0 insere no topo). Escrita atômica, com backup e revert."
            ),
        ),
        StructuredTool.from_function(
            func=append_file,
            name="append_file",
            description=(
                "Acrescenta conteúdo no fim do arquivo (cria o arquivo se não existir). "
                "Escrita atômica, com backup e revert."
            ),
        ),
    ]


# --------------------------------------------------------------------------- #
# jail + leitura
# --------------------------------------------------------------------------- #


def _read_gate(root: Path, path: str) -> str | None:
    """Recusa (string para o modelo) se falta leitura fresca de `path`, ou `None`.

    Fail-open em tudo que não é o caso do gate: arquivo novo, diretório, binário,
    path fora do jail (o erro específico é da própria tool) e ausência do
    `smart_fs` (o extra `deepagents` pode não estar instalado — este módulo
    precisa importar sem ele).
    """
    text = _disk_text(root, path)
    if text is None:
        return None
    try:
        from harness.backends.smart_fs import needs_fresh_read
    except Exception:  # noqa: BLE001 - sem o gate a tool ainda funciona
        return None
    return needs_fresh_read(path, text)


def _mark_written(root: Path, path: str) -> None:
    """Registra o conteúdo pós-escrita como lido: quem escreveu sabe o que ficou
    lá, e barrar a edição seguinte seria só atrito."""
    text = _disk_text(root, path)
    if text is None:
        return
    try:
        from harness.backends.smart_fs import record_read
    except Exception:  # noqa: BLE001 - mesmo motivo de `_read_gate`
        return
    record_read(path, text)


def _disk_text(root: Path, path: str) -> str | None:
    """Conteúdo utf-8 do arquivo agora, ou `None` (novo/diretório/binário/fora)."""
    try:
        target = _resolve(root, path)
    except _Blocked:
        return None
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _resolve(root: Path, path: str) -> Path:
    """Path do modelo -> path real sob `root`.

    O filesystem do executor é virtual com raiz no workspace, então `/app.py` e
    `app.py` são o MESMO arquivo. `resolve()` antes da comparação porque o
    ataque barato é `../` e o segundo mais barato é symlink.
    """
    base = root.resolve()
    target = (base / str(path).lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise _Blocked(f"Erro: caminho fora do workspace: {path!r}")
    return target


def _load(root: Path, path: str) -> tuple[Path, bytes, str, list[str]]:
    """(path real, bytes crus, texto, linhas com terminador). Levanta _Blocked."""
    target = _resolve(root, path)
    if not target.exists():
        raise _Blocked(f"Erro: arquivo não existe: {path}")
    if target.is_dir():
        raise _Blocked(f"Erro: {path} é um diretório, não um arquivo")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Blocked(f"Erro: {path} não é texto utf-8; estas tools só editam texto") from None
    return target, raw, text, text.splitlines(keepends=True)


def _numbered(lines: list[str], first: int) -> str:
    """Linhas com a margem de numeração, no mesmo formato do read_file."""
    out = []
    for i, line in enumerate(lines):
        out.append(f"{first + i:>6}\t{line.rstrip()}\n")
    return "".join(out)


def _total(lines: list[str], size: int) -> str:
    return f"TOTAL: {len(lines)} linhas, {size / 1024:.1f} KB"


def _total_of(lines: list[str]) -> str:
    """`_total` para uma lista de linhas recém-montada (tamanho em utf-8)."""
    return _total(lines, len("".join(lines).encode("utf-8")))


# --------------------------------------------------------------------------- #
# file_outline
# --------------------------------------------------------------------------- #


def outline(root: Path, path: str) -> str:
    """Esqueleto do arquivo por extensão, com no máximo MAX_OUTLINE_ENTRIES entradas."""
    try:
        target, raw, text, lines = _load(root, path)
    except _Blocked as exc:
        return str(exc)

    suffix = target.suffix.lower()
    if suffix == ".json":
        entries = _outline_json(text, lines)
    elif suffix == ".py":
        entries = _outline_regex(lines, _PY_SKELETON)
    elif suffix in (".md", ".markdown"):
        entries = _outline_regex(lines, _MD_HEADING)
    elif suffix in (".toml", ".ini", ".cfg"):
        entries = _outline_regex(lines, _TOML_SECTION)
    elif suffix in _JS_SUFFIXES:
        entries = _outline_regex(lines, _JS_SKELETON)
    else:
        entries = _outline_toplevel(lines)

    head = entries[:MAX_OUTLINE_ENTRIES]
    body = "".join(head)
    if len(entries) > MAX_OUTLINE_ENTRIES:
        body += f"... (cortado em {MAX_OUTLINE_ENTRIES} entradas de {len(entries)})\n"
    if not body:
        body = "(nenhuma definição reconhecida neste arquivo)\n"
    return body + _total(lines, len(raw))


def _outline_regex(lines: list[str], pattern: re.Pattern[str]) -> list[str]:
    return [f"{n:>6}\t{line.rstrip()}\n" for n, line in enumerate(lines, 1) if pattern.match(line)]


def _outline_toplevel(lines: list[str]) -> list[str]:
    """Default: linhas não-indentadas e não-vazias. Serve para texto e conf solta."""
    return [
        f"{n:>6}\t{line.rstrip()}\n"
        for n, line in enumerate(lines, 1)
        if line.strip() and not line[0].isspace()
    ]


def _outline_json(text: str, lines: list[str]) -> list[str]:
    """Chaves de nível 1 e 2. JSON inválido cai no default (sem esconder o erro)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"(json inválido: {exc}); esqueleto por linha:\n", *_outline_toplevel(lines)]

    entries: list[str] = []

    def walk(obj: object, prefix: str, depth: int) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                entries.append(f"{prefix}{key}: {_jtype(value)}\n")
                if depth < 2 and isinstance(value, dict | list):
                    walk(value, f"{prefix}{key}.", depth + 1)
        elif isinstance(obj, list) and prefix:
            entries.append(f"{prefix}[0..{len(obj) - 1}]\n")

    walk(data, "", 1)
    return entries


def _jtype(value: object) -> str:
    if isinstance(value, dict):
        return f"objeto ({len(value)} chaves)"
    if isinstance(value, list):
        return f"lista ({len(value)} itens)"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "str"
    if isinstance(value, int | float):
        return "número"
    return "null"


# --------------------------------------------------------------------------- #
# escrita: backup, atômico, validação, revert
# --------------------------------------------------------------------------- #


def _slug(relative: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in relative).strip("_") or "arquivo"


def _backup(root: Path, target: Path, raw: bytes) -> Path:
    """Grava o conteúdo anterior em .harness/edits/<ts>-<slug> e poda o ring."""
    edits = root.resolve() / ".harness" / "edits"
    edits.mkdir(parents=True, exist_ok=True)
    try:
        relative = str(target.relative_to(root.resolve()))
    except ValueError:  # não deve acontecer: _resolve já garantiu o jail
        relative = target.name
    stamp = time.strftime("%Y%m%dT%H%M%S") + f".{time.time_ns() % 1_000_000_000 // 1000:06d}"
    dest = edits / f"{stamp}-{_slug(relative)}"
    dest.write_bytes(raw)
    _prune(edits)
    return dest


def _prune(edits: Path) -> None:
    """Ring de BACKUP_RING: o nome começa com timestamp, então ordem = idade."""
    backups = sorted(p for p in edits.iterdir() if p.is_file())
    for stale in backups[:-BACKUP_RING] if len(backups) > BACKUP_RING else []:
        stale.unlink(missing_ok=True)


def _atomic_write(target: Path, data: bytes) -> None:
    """tmp no MESMO diretório + os.replace: nunca existe arquivo meio escrito."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".harness-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _validate(target: Path, text: str) -> str | None:
    """None = ok; string = motivo. Só extensões com parser de confiança."""
    suffix = target.suffix.lower()
    try:
        if suffix in (".py", ".pyi"):
            compile(text, str(target), "exec")
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            tomllib.loads(text)
        else:
            return None
    except (SyntaxError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _commit(root: Path, target: Path, old_raw: bytes, new_text: str) -> tuple[str, bool]:
    """Backup + escrita + validação (+ revert). Devolve (frase, ok)."""
    backup = _backup(root, target, old_raw)
    _atomic_write(target, new_text.encode("utf-8"))
    problem = _validate(target, new_text)
    if problem is None:
        return "validação: ok", True
    _atomic_write(target, old_raw)  # revert byte a byte
    return (
        f"REVERTIDO: o arquivo ficaria inválido ({problem}). "
        f"Nada mudou; o conteúdo anterior está intacto (backup em "
        f".harness/edits/{backup.name}). Corrija o conteúdo e tente de novo.",
        False,
    )


# --------------------------------------------------------------------------- #
# edit_range / insert_lines / append_file
# --------------------------------------------------------------------------- #


def _block(new_content: str) -> list[str]:
    """Conteúdo do modelo -> linhas com terminador. String vazia = remoção."""
    if new_content == "":
        return []
    if not new_content.endswith("\n"):
        new_content += "\n"
    return new_content.splitlines(keepends=True)


def replace_range(
    root: Path,
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    expect_first_line: str | None = None,
) -> str:
    """Troca as linhas start_line..end_line (1-indexado, inclusivo) por new_content."""
    try:
        target, raw, _text, lines = _load(root, path)
    except _Blocked as exc:
        return str(exc)

    total = len(lines)
    if start_line < 1:
        return f"Erro: start_line={start_line}; a numeração é 1-indexada."
    if start_line > total:
        return f"Erro: start_line={start_line} passa do fim do arquivo ({total} linhas). Para acrescentar use append_file ou insert_lines(after_line={total})."
    if end_line < start_line:
        return (
            f"Erro: end_line={end_line} é menor que start_line={start_line}. "
            f"edit_range só substitui; para inserir sem apagar use "
            f"insert_lines(path={path!r}, after_line={start_line - 1}, content=...)."
        )

    clamped = min(end_line, total)
    if expect_first_line is not None:
        actual = lines[start_line - 1].rstrip("\n")
        if actual.rstrip() != expect_first_line.rstrip():
            window_start = max(1, start_line - 2)
            window = lines[window_start - 1 : start_line + 2]
            return (
                f"Erro: expect_first_line não casa com a linha {start_line}.\n"
                f"  esperado: {expect_first_line!r}\n"
                f"  real:     {actual!r}\n"
                f"Contexto real (linhas {window_start}-{window_start + len(window) - 1}):\n"
                f"{_numbered(window, window_start)}"
                f"Releia com read_file(file_path={path!r}, offset={window_start - 1}, limit=10) e refaça a faixa."
            )

    block = _block(new_content)
    new_lines = lines[: start_line - 1] + block + lines[clamped:]
    phrase, ok = _commit(root, target, raw, "".join(new_lines))
    if not ok:
        return phrase

    removed = clamped - start_line + 1
    head = f"linhas {start_line}-{clamped} substituídas ({removed}→{len(block)})"
    if not block:
        head = f"linhas {start_line}-{clamped} removidas ({removed}→0)"
    return f"{head}; {phrase}\n{_context(new_lines, start_line)}{_total_of(new_lines)}"


def insert_after(root: Path, path: str, after_line: int, content: str) -> str:
    """Insere `content` depois da linha `after_line`; 0 = topo do arquivo."""
    try:
        target, raw, _text, lines = _load(root, path)
    except _Blocked as exc:
        return str(exc)

    total = len(lines)
    if after_line < 0:
        return f"Erro: after_line={after_line}; use 0 para inserir no topo."
    if after_line > total:
        return f"Erro: after_line={after_line} passa do fim do arquivo ({total} linhas); use after_line={total} ou append_file."

    block = _block(content)
    if not block:
        return "Erro: content vazio; insert_lines sempre acrescenta pelo menos uma linha."
    before = lines[:after_line]
    # Linha anterior sem terminador colaria o texto novo no fim dela.
    if before and not before[-1].endswith("\n"):
        before = [*before[:-1], before[-1] + "\n"]
    new_lines = before + block + lines[after_line:]
    phrase, ok = _commit(root, target, raw, "".join(new_lines))
    if not ok:
        return phrase

    first = after_line + 1
    where = "no topo" if after_line == 0 else f"depois da linha {after_line}"
    return (
        f"{len(block)} linha(s) inseridas {where}; {phrase}\n"
        f"{_context(new_lines, first)}{_total_of(new_lines)}"
    )


def append(root: Path, path: str, content: str) -> str:
    """Acrescenta no fim do arquivo. Cria o arquivo se ele não existir."""
    try:
        target = _resolve(root, path)
    except _Blocked as exc:
        return str(exc)
    if target.is_dir():
        return f"Erro: {path} é um diretório, não um arquivo"

    existed = target.exists()
    raw = target.read_bytes() if existed else b""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"Erro: {path} não é texto utf-8; estas tools só editam texto"

    block = _block(content)
    if not block:
        return "Erro: content vazio; append_file sempre acrescenta pelo menos uma linha."
    if text and not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    new_lines = lines + block
    phrase, ok = _commit(root, target, raw, "".join(new_lines))
    if not ok:
        if not existed:  # o revert recriou um arquivo vazio que não existia
            target.unlink(missing_ok=True)
        return phrase

    first = len(lines) + 1
    verb = "criado com" if not existed else "acrescentadas"
    return (
        f"{path} {verb} {len(block)} linha(s) no fim; {phrase}\n"
        f"{_context(new_lines, first)}{_total_of(new_lines)}"
    )


def _context(new_lines: list[str], first: int) -> str:
    """CONTEXT_LINES linhas a partir do ponto editado, já RENUMERADAS."""
    window = new_lines[first - 1 : first - 1 + CONTEXT_LINES]
    if not window:
        return ""
    return _numbered(window, first)
