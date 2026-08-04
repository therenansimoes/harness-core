"""Índice de símbolos do workspace: onde cada nome é DEFINIDO e quem o usa.

Motivo de existir: num repo que o modelo não conhece, achar `handleSubmit` custa
um `grep` cego + dois `read_file` inteiros — e o grep devolve o uso antes da
definição. Aqui o custo é um índice construído uma vez e reusado: a resposta é
`arquivo:linha` com a assinatura ao lado, em ~20 linhas de saída.

Contrato deste módulo:
- ZERO dependência nova: `.py` sai do `ast` da stdlib, `.js/.ts` e `.html` saem de
  regex rodada DEPOIS de apagar strings e comentários (parser de verdade para JS
  exigiria tree-sitter, que não tem wheel para o Python deste repo);
- o índice é persistido em `<ws>/.harness/symbols.json` e invalidado por
  `(path, mtime_ns, size)`: arquivo intocado NÃO é lido de novo, arquivo apagado
  cai do índice na varredura seguinte;
- teto de MAX_FILES arquivos: acima disso o índice deixa de ser barato e o que o
  modelo precisa quase nunca está no rabo da varredura;
- nada aqui levanta exceção por arquivo ruim (binário, sintaxe quebrada, encoding
  errado): símbolo é dica, e um arquivo ilegível não pode derrubar a busca.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

HARNESS_SUBDIR = ".harness"
INDEX_FILE = "symbols.json"
INDEX_VERSION = 1

# Teto da varredura: conta arquivo INDEXÁVEL (extensão suportada), não a árvore.
MAX_FILES = 2000
# Topo devolvido por consulta. Mais que isso volta a ser despejo de grep.
MAX_HITS = 20
# Assinatura é uma linha; linha absurda é truncada para não estourar o contexto.
MAX_SIG = 200

# Diretórios que nunca entram: dependência, build e o próprio estado do harness.
SKIP_DIRS = frozenset(
    {".venv", "venv", "node_modules", "dist", "build", ".git", ".harness", "__pycache__"}
)

_PY_SUFFIXES = (".py",)
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_HTML_SUFFIXES = (".html", ".htm")

_JS_FUNCTION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
)
_JS_CLASS = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"
)
_JS_BINDING = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=:]")
_JS_ARROW = re.compile(r"=>|\bfunction\b")
# Só .ts/.tsx: em .js isso casaria `type = 3` de qualquer objeto.
_TS_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?(interface|type|enum)\s+([A-Za-z_$][\w$]*)"
)

_HTML_TAG = re.compile(r"<([A-Za-z][\w-]*)((?:\s[^<>]*)?)>", re.DOTALL)
_HTML_ID = re.compile(r"""\bid\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>]+))""")
# Landmarks: um `<section id="pricing">` é alvo de âncora e de CSS, não um id qualquer.
_HTML_LANDMARKS = frozenset({"section", "main", "nav"})


def index_path(ws: str | Path) -> Path:
    """`<ws>/.harness/symbols.json` — um índice por workspace."""
    return Path(ws) / HARNESS_SUBDIR / INDEX_FILE


def _read(path: Path) -> str:
    """Único ponto de I/O de conteúdo do módulo.

    Existe isolado de propósito: é o que a invalidação por mtime evita chamar, e
    é onde um teste conta leituras para provar que o cache está valendo.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _lang(suffix: str) -> str | None:
    low = suffix.lower()
    if low in _PY_SUFFIXES:
        return "py"
    if low in _JS_SUFFIXES:
        return "js"
    if low in _HTML_SUFFIXES:
        return "html"
    return None


def _walk(ws: Path) -> list[Path]:
    """Arquivos indexáveis do workspace, em ordem estável, até MAX_FILES.

    Ordem determinística importa: com teto, ordem instável faz o índice mudar de
    conteúdo entre duas varreduras iguais.
    """
    out: list[Path] = []
    for raiz, dirnames, filenames in os.walk(ws):
        # Pasta oculta também cai: `.mypy_cache`/`.next` são build de alguém.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for nome in sorted(filenames):
            if _lang(Path(nome).suffix) is None:
                continue
            out.append(Path(raiz) / nome)
            if len(out) >= MAX_FILES:
                return out
    return out


def _blank_literals(text: str, lang: str) -> str:
    """Devolve o texto com strings e comentários trocados por espaço.

    Mantém offsets e `\\n` no lugar: o resultado casa linha a linha com o
    original, então regex roda no texto limpo e a assinatura sai do original.
    Em HTML só comentário é apagado — valor de atributo É o dado (`id="x"`).
    """
    n = len(text)
    out = list(text)

    def apaga(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if text[k] != "\n":
                out[k] = " "

    quotes = "\"'`" if lang == "js" else "\"'"
    i = 0
    while i < n:
        ch = text[i]
        if lang == "html":
            if text.startswith("<!--", i):
                fim = text.find("-->", i)
                fim = n if fim < 0 else fim + 3
                apaga(i, fim)
                i = fim
                continue
            i += 1
            continue
        if lang == "py" and (text.startswith('"""', i) or text.startswith("'''", i)):
            marca = text[i : i + 3]
            fim = text.find(marca, i + 3)
            fim = n if fim < 0 else fim + 3
            apaga(i, fim)
            i = fim
            continue
        if ch in quotes:
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    j += 1
                    break
                # String de uma linha não atravessa `\n` (menos template literal).
                if text[j] == "\n" and not (lang == "js" and ch == "`"):
                    break
                j += 1
            apaga(i, j)
            i = j
            continue
        if lang == "py" and ch == "#":
            fim = text.find("\n", i)
            apaga(i, n if fim < 0 else fim)
            i = n if fim < 0 else fim
            continue
        if lang == "js" and text.startswith("//", i):
            fim = text.find("\n", i)
            apaga(i, n if fim < 0 else fim)
            i = n if fim < 0 else fim
            continue
        if lang == "js" and text.startswith("/*", i):
            fim = text.find("*/", i)
            fim = n if fim < 0 else fim + 2
            apaga(i, fim)
            i = fim
            continue
        i += 1
    return "".join(out)


def _sig(linhas: list[str], lineno: int) -> str:
    """Assinatura = a linha EXATA da definição, sem indentação, truncada."""
    if 1 <= lineno <= len(linhas):
        return linhas[lineno - 1].strip()[:MAX_SIG]
    return ""


def _py_symbols(text: str) -> list[list]:
    """class/def/async def do módulo, incluindo os aninhados (métodos)."""
    try:
        arvore = ast.parse(text)
    except (SyntaxError, ValueError):
        return []  # arquivo em edição não pode cegar o índice inteiro
    linhas = text.splitlines()
    achados: list[list] = []
    for node in ast.walk(arvore):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async def"
        elif isinstance(node, ast.FunctionDef):
            kind = "def"
        else:
            continue
        # `lineno` do node é a linha do `class`/`def`, não a do decorator — é a
        # linha que o humano quer abrir, e a assinatura sai dela.
        achados.append([node.name, node.lineno, kind, _sig(linhas, node.lineno)])
    achados.sort(key=lambda a: a[1])
    return achados


def _js_symbols(text: str, ts: bool) -> list[list]:
    limpo = _blank_literals(text, "js")
    originais = text.splitlines()
    achados: list[list] = []
    for idx, linha in enumerate(limpo.splitlines(), start=1):
        sig = _sig(originais, idx)
        m = _JS_FUNCTION.match(linha)
        if m:
            achados.append([m.group(1), idx, "function", sig])
            continue
        m = _JS_CLASS.match(linha)
        if m:
            achados.append([m.group(1), idx, "class", sig])
            continue
        if ts:
            m = _TS_DECL.match(linha)
            if m:
                achados.append([m.group(2), idx, m.group(1), sig])
                continue
        m = _JS_BINDING.match(linha)
        if m:
            # `const f = () => {}` é função; `const N = 3` é constante.
            kind = "function" if _JS_ARROW.search(linha) else "const"
            achados.append([m.group(1), idx, kind, sig])
    return achados


def _html_symbols(text: str) -> list[list]:
    limpo = _blank_literals(text, "html")
    originais = text.splitlines()
    achados: list[list] = []
    vistos: set[tuple[str, int]] = set()
    for m in _HTML_TAG.finditer(limpo):
        tag = m.group(1).lower()
        attrs = m.group(2) or ""
        alvo = _HTML_ID.search(attrs)
        if not alvo:
            continue
        nome = alvo.group(1) or alvo.group(2) or alvo.group(3) or ""
        if not nome:
            continue
        lineno = limpo.count("\n", 0, m.start()) + 1
        if (nome, lineno) in vistos:
            continue
        vistos.add((nome, lineno))
        kind = tag if tag in _HTML_LANDMARKS else "id"
        achados.append([nome, lineno, kind, _sig(originais, lineno)])
    return achados


def _extract(lang: str, suffix: str, text: str) -> list[list]:
    if lang == "py":
        return _py_symbols(text)
    if lang == "js":
        return _js_symbols(text, ts=suffix.lower() in (".ts", ".tsx"))
    return _html_symbols(text)


def _load(ws: Path) -> dict:
    try:
        dados = json.loads(index_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(dados, dict) or dados.get("version") != INDEX_VERSION:
        return {}  # formato antigo: reindexa em vez de adivinhar
    arquivos = dados.get("files")
    return arquivos if isinstance(arquivos, dict) else {}


def _save(ws: Path, arquivos: dict) -> None:
    destino = index_path(ws)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": INDEX_VERSION, "files": arquivos}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, destino)
    except OSError:
        pass  # workspace read-only: o índice em memória desta chamada ainda serve


def index_workspace(ws: str | Path) -> dict[str, list[tuple[str, int, str, str]]]:
    """Varre o workspace e devolve `{nome: [(path, linha, kind, assinatura)]}`.

    Reusa o que está em `.harness/symbols.json` para todo arquivo cujo
    `(mtime_ns, size)` não mudou — o custo da segunda chamada é o `stat()`.
    """
    raiz = Path(ws)
    antigo = _load(raiz)
    atual: dict[str, dict] = {}
    for path in _walk(raiz):
        try:
            st = path.stat()
            rel = path.relative_to(raiz).as_posix()
        except (OSError, ValueError):
            continue
        anterior = antigo.get(rel)
        if (
            isinstance(anterior, dict)
            and anterior.get("mtime_ns") == st.st_mtime_ns
            and anterior.get("size") == st.st_size
        ):
            atual[rel] = anterior
            continue
        lang = _lang(path.suffix)
        if lang is None:
            continue
        try:
            texto = _read(path)
        except OSError:
            continue
        atual[rel] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "symbols": _extract(lang, path.suffix, texto),
        }
    if atual != antigo:
        # Arquivo apagado não está em `atual`: sai do índice sem passo extra.
        _save(raiz, atual)
    indice: dict[str, list[tuple[str, int, str, str]]] = {}
    for rel, registro in atual.items():
        for nome, linha, kind, sig in registro.get("symbols", []):
            indice.setdefault(nome, []).append((rel, linha, kind, sig))
    return indice


def find_symbol(ws: str | Path, name: str) -> list[dict]:
    """Definições de `name`: casamento exato primeiro, depois prefixo. Topo 20."""
    indice = index_workspace(ws)
    exatos = indice.get(name, [])
    prefixos: list[tuple[str, tuple]] = []
    if len(exatos) < MAX_HITS:
        baixo = name.lower()
        for nome, ocorrencias in sorted(indice.items()):
            if nome == name or not nome.lower().startswith(baixo):
                continue
            for oc in ocorrencias:
                prefixos.append((nome, oc))
    achados = [(name, oc) for oc in exatos] + prefixos
    return [
        {"name": nome, "path": path, "line": linha, "kind": kind, "signature": sig}
        for nome, (path, linha, kind, sig) in achados[:MAX_HITS]
    ]


def find_references(ws: str | Path, name: str) -> list[dict]:
    """Usos de `name` como palavra inteira, fora de string e comentário. Topo 20.

    Varre só os arquivos que o índice conhece — o mesmo recorte da definição, o
    que evita varrer `node_modules` atrás de um nome do projeto.
    """
    raiz = Path(ws)
    index_workspace(raiz)  # garante índice fresco (e o recorte de arquivos)
    alvo = re.compile(rf"\b{re.escape(name)}\b")
    achados: list[dict] = []
    for rel in sorted(_load(raiz)):
        path = raiz / rel
        lang = _lang(path.suffix)
        if lang is None:
            continue
        try:
            texto = _read(path)
        except OSError:
            continue
        limpo = _blank_literals(texto, lang)
        originais = texto.splitlines()
        for idx, linha in enumerate(limpo.splitlines(), start=1):
            if not alvo.search(linha):
                continue
            achados.append({"path": rel, "line": idx, "text": _sig(originais, idx)})
            if len(achados) >= MAX_HITS:
                return achados
    return achados


def signature_of(ws: str | Path, name: str) -> str:
    """A assinatura da primeira definição exata de `name` (`""` se não achou)."""
    for achado in find_symbol(ws, name):
        if achado["name"] == name:
            return achado["signature"]
    return ""


def _formata_simbolos(achados: list[dict], name: str) -> str:
    if not achados:
        return f"find_symbol: nenhuma definição de `{name}` no índice"
    linhas = [
        f"{a['path']}:{a['line']}  {a['kind']} {a['name']}  {a['signature']}" for a in achados
    ]
    return f"find_symbol `{name}` ({len(achados)}):\n" + "\n".join(linhas)


def _formata_refs(achados: list[dict], name: str) -> str:
    if not achados:
        return f"find_references: nenhum uso de `{name}` fora de string/comentário"
    linhas = [f"{a['path']}:{a['line']}  {a['text']}" for a in achados]
    return f"find_references `{name}` ({len(achados)}):\n" + "\n".join(linhas)


def make_symbol_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado.

    Erro é string de retorno, nunca exceção: exceção em tool node derruba o run.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def find_symbol_tool(name: str) -> str:
        """Onde um nome é DEFINIDO no workspace, com arquivo, linha e assinatura."""
        try:
            return _formata_simbolos(find_symbol(base, name), name)
        except Exception as exc:
            return f"find_symbol falhou: {type(exc).__name__}: {exc}"

    def find_references_tool(name: str) -> str:
        """Onde um nome é USADO no workspace, ignorando string e comentário."""
        try:
            return _formata_refs(find_references(base, name), name)
        except Exception as exc:
            return f"find_references falhou: {type(exc).__name__}: {exc}"

    def signature_of_tool(name: str) -> str:
        """A linha da assinatura de uma função/classe, sem abrir o arquivo."""
        try:
            sig = signature_of(base, name)
        except Exception as exc:
            return f"signature_of falhou: {type(exc).__name__}: {exc}"
        return sig or f"signature_of: `{name}` não está no índice de símbolos"

    return [
        StructuredTool.from_function(
            func=find_symbol_tool,
            name="find_symbol",
            description=(
                "Diz ONDE um nome é definido: arquivo, linha, kind e a linha da assinatura. "
                "Indexa .py (ast), .js/.jsx/.ts/.tsx (function/class/const/arrow) e ids de "
                ".html. Casa nome exato e, se sobrar espaço, por prefixo (topo 20). "
                "Use ANTES de grep ou read_file num repo que você não conhece: é ~10x mais "
                "barato em contexto e devolve a DEFINIÇÃO, não os 40 usos. Não achou nada "
                "significa que o nome não é definido aqui (veio de dependência ou você errou "
                "o nome) — não vale varrer o repo à mão atrás dele."
            ),
        ),
        StructuredTool.from_function(
            func=find_references_tool,
            name="find_references",
            description=(
                "Diz QUEM usa um nome: arquivo, linha e o texto da linha, só nos arquivos "
                "indexados e só fora de string e comentário (nome dentro de string não conta). "
                "Topo 20. Use antes de renomear ou apagar algo: mostra o raio do estrago em "
                "uma saída, no lugar de um grep que também casa o próprio nome no comentário."
            ),
        ),
        StructuredTool.from_function(
            func=signature_of_tool,
            name="signature_of",
            description=(
                "Devolve a linha da assinatura de uma função ou classe (ex.: "
                "'def render(ws, port=None):'). Use antes de CHAMAR algo que você não escreveu: "
                "resolve a ordem e o nome dos parâmetros em uma linha, sem gastar um read_file "
                "no arquivo inteiro para descobrir a mesma coisa."
            ),
        ),
    ]


__all__ = [
    "MAX_FILES",
    "MAX_HITS",
    "SKIP_DIRS",
    "find_references",
    "find_symbol",
    "index_path",
    "index_workspace",
    "make_symbol_tools",
    "signature_of",
]
