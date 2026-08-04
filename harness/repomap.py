"""Mapa do repositório por PageRank: quais arquivos e símbolos importam.

`find_symbol` responde "onde está X" — só serve quando o modelo já sabe o nome
de X. Num repo que ele nunca viu, a primeira pergunta é outra: por onde começar.
Sem resposta, executor pequeno faz `ls` na raiz, abre o arquivo de nome mais
bonito e queima contexto em código periférico.

O grafo é o mesmo do aider: aresta `arquivo_que_referencia -> arquivo_que_define`
para cada nome do índice de símbolos, peso `ocorrências / nº de arquivos que
definem o nome` (nome comum como `run` espalha peso, nome único concentra). Só
texto FORA de string e comentário conta — o `_blank_literals` do `symbols` é o
mesmo pré-processamento do `find_references`, então "referência" quer dizer a
mesma coisa nas duas tools. Auto-aresta não entra: arquivo se citar não é sinal.

PageRank puro (20 iterações fixas, sem tolerância) porque determinismo vale mais
que precisão no 6º dígito: duas chamadas iguais têm que dar a MESMA saída, senão
o mapa vira ruído de diff no contexto do agente.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from harness.symbols import HARNESS_SUBDIR, _blank_literals, _lang, _load, _read, index_workspace

CACHE_FILE = "repomap.json"
CACHE_VERSION = 1

# Amortecimento clássico do PageRank.
DAMPING = 0.85
# Iterações fixas em vez de convergência por epsilon: barato e determinístico.
ITERACOES = 20
# Orçamento default em tokens; a saída é cortada em ~4 chars por token.
BUDGET_TOKENS = 1000
CHARS_POR_TOKEN = 4

# Identificador em py/js/ts. `$` entra porque é nome válido em JS.
_TOKEN = re.compile(r"\b[A-Za-z_$][\w$]*\b")

VAZIO = "repo_map: índice vazio"


def cache_path(ws: str | Path) -> Path:
    """`<ws>/.harness/repomap.json` — um cache por workspace."""
    return Path(ws) / HARNESS_SUBDIR / CACHE_FILE


def _fingerprint(registros: dict) -> str:
    """Sha1 de `(rel, mtime_ns, size)` de todo o índice.

    Mesmo material que invalida o índice de símbolos: se nenhum arquivo mudou de
    tamanho ou data, o grafo é o mesmo e não há o que recalcular.
    """
    material = [
        (rel, registros[rel].get("mtime_ns"), registros[rel].get("size"))
        for rel in sorted(registros)
        if isinstance(registros[rel], dict)
    ]
    bruto = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha1(bruto.encode("utf-8")).hexdigest()


def _defs(registros: dict) -> dict[str, list[str]]:
    """`{nome: [arquivos que definem]}`, sem repetir arquivo.

    Arquivo que define `run` duas vezes (método homônimo em duas classes) não
    vale peso dobrado — o que interessa é quantos ARQUIVOS disputam o nome.
    """
    por_nome: dict[str, set[str]] = {}
    for rel, registro in registros.items():
        if not isinstance(registro, dict):
            continue
        for simbolo in registro.get("symbols", []):
            if simbolo:
                por_nome.setdefault(simbolo[0], set()).add(rel)
    return {nome: sorted(arquivos) for nome, arquivos in por_nome.items()}


def _grafo(raiz: Path, registros: dict, defs: dict[str, list[str]]):
    """Arestas `origem -> {destino: peso}` e entradas `(arquivo, nome) -> peso`.

    As entradas por nome são o que dá score de SÍMBOLO depois: um arquivo pode
    ser importante por causa de uma função só, e é essa função que o mapa tem
    que citar.
    """
    arestas: dict[str, dict[str, float]] = {}
    entradas: dict[tuple[str, str], float] = {}
    for rel in sorted(registros):
        path = raiz / rel
        lang = _lang(path.suffix)
        if lang is None:
            continue
        try:
            texto = _read(path)
        except OSError:
            continue
        contagem = Counter(_TOKEN.findall(_blank_literals(texto, lang)))
        for nome, vezes in contagem.items():
            donos = defs.get(nome)
            if not donos:
                continue
            peso = vezes / len(donos)
            for dono in donos:
                if dono == rel:
                    continue  # sem auto-aresta
                arestas.setdefault(rel, {})[dono] = (
                    arestas.setdefault(rel, {}).get(dono, 0.0) + peso
                )
                chave = (dono, nome)
                entradas[chave] = entradas.get(chave, 0.0) + peso
    return arestas, entradas


def _pagerank(nos: list[str], arestas: dict[str, dict[str, float]]) -> dict[str, float]:
    """PageRank ponderado. Nó sem saída (dangling) redistribui uniforme."""
    n = len(nos)
    if not n:
        return {}
    rank = dict.fromkeys(nos, 1.0 / n)
    saida = {origem: sum(destinos.values()) for origem, destinos in arestas.items()}
    for _ in range(ITERACOES):
        acumulado = dict.fromkeys(nos, 0.0)
        vazado = 0.0
        for no, r in rank.items():
            total = saida.get(no, 0.0)
            if total <= 0:
                vazado += r
                continue
            for destino, peso in arestas[no].items():
                acumulado[destino] = acumulado.get(destino, 0.0) + r * peso / total
        base = (1.0 - DAMPING) / n + DAMPING * vazado / n
        rank = {no: base + DAMPING * acumulado.get(no, 0.0) for no in nos}
    return rank


def rank_symbols(ws: str | Path) -> list[dict]:
    """Símbolos do workspace ordenados por relevância no grafo de referências.

    Score = rank do arquivo × peso de entrada daquele nome. Símbolo que ninguém
    referencia fica com 0 e cai para o fim — não sai da lista, só perde o
    orçamento primeiro.
    """
    raiz = Path(ws)
    index_workspace(raiz)  # garante índice fresco
    registros = _load(raiz)
    if not registros:
        return []
    defs = _defs(registros)
    arestas, entradas = _grafo(raiz, registros, defs)
    rank = _pagerank(sorted(registros), arestas)
    achados: list[dict] = []
    for rel in sorted(registros):
        registro = registros[rel]
        if not isinstance(registro, dict):
            continue
        for simbolo in registro.get("symbols", []):
            nome, linha, kind, sig = (list(simbolo) + ["", 0, "", ""])[:4]
            achados.append(
                {
                    "name": nome,
                    "path": rel,
                    "line": linha,
                    "kind": kind,
                    "signature": sig,
                    "score": rank.get(rel, 0.0) * entradas.get((rel, nome), 0.0),
                }
            )
    # Desempate por path/linha: score igual não pode virar ordem instável.
    achados.sort(key=lambda a: (-a["score"], a["path"], a["line"], a["name"]))
    return achados


def _formata(achados: list[dict], limite: int) -> str:
    """Saída agrupada por arquivo, no formato do `_formata_simbolos`, cortada.

    Ordem dos arquivos = melhor score de símbolo dentro dele; ordem dentro do
    arquivo = a mesma do ranking. O corte é por linha inteira: meia linha de
    assinatura não ajuda ninguém.
    """
    if not achados:
        return VAZIO
    melhor: dict[str, float] = {}
    for a in achados:
        if a["path"] not in melhor:
            melhor[a["path"]] = a["score"]
    ordem = sorted(melhor, key=lambda p: (-melhor[p], p))
    partes = [f"repo_map ({len(ordem)} arquivos):"]
    usado = len(partes[0])
    for rel in ordem:
        cabecalho = f"{rel}:"
        if usado + len(cabecalho) + 1 > limite:
            break
        pendente = [cabecalho]
        gasto = len(cabecalho) + 1
        for a in achados:
            if a["path"] != rel:
                continue
            linha = f"  {a['line']}  {a['kind']} {a['name']}  {a['signature']}".rstrip()
            if usado + gasto + len(linha) + 1 > limite:
                break
            pendente.append(linha)
            gasto += len(linha) + 1
        if len(pendente) == 1:
            break  # cabeçalho sem símbolo é linha gasta à toa
        partes.extend(pendente)
        usado += gasto
    if len(partes) == 1:
        # Orçamento menor que o cabeçalho + uma linha: devolve o topo cru,
        # truncado. "índice vazio" aqui seria mentira — o índice tem conteúdo.
        topo = achados[0]
        cru = f"{topo['path']}:{topo['line']} {topo['kind']} {topo['name']}  {topo['signature']}"
        return cru.rstrip()[:limite]
    return "\n".join(partes)


def _cache_load(raiz: Path) -> dict:
    try:
        dados = json.loads(cache_path(raiz).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(dados, dict) or dados.get("version") != CACHE_VERSION:
        return {}
    return dados


def _cache_save(raiz: Path, dados: dict) -> None:
    destino = cache_path(raiz)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, destino)
    except OSError:
        pass  # workspace read-only: o mapa desta chamada ainda serve


def repo_map(ws: str | Path, budget_tokens: int = BUDGET_TOKENS) -> str:
    """Mapa do repo em texto, dentro do orçamento de tokens pedido."""
    raiz = Path(ws)
    limite = max(1, int(budget_tokens)) * CHARS_POR_TOKEN
    index_workspace(raiz)
    registros = _load(raiz)
    impressao = _fingerprint(registros)
    cache = _cache_load(raiz)
    if cache.get("fingerprint") == impressao and cache.get("budget") == budget_tokens:
        mapa = cache.get("map")
        if isinstance(mapa, str):
            return mapa
    mapa = _formata(rank_symbols(raiz), limite)
    _cache_save(
        raiz,
        {
            "version": CACHE_VERSION,
            "fingerprint": impressao,
            "budget": budget_tokens,
            "map": mapa,
        },
    )
    return mapa


def make_repomap_tools(ws: str | Path) -> list:
    """Tool LangChain deste módulo com o workspace fixado.

    Erro é string de retorno, nunca exceção: exceção em tool node derruba o run.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def repo_map_tool(budget_tokens: int = BUDGET_TOKENS) -> str:
        """Panorama do repo: arquivos e símbolos mais referenciados."""
        try:
            return repo_map(base, budget_tokens)
        except Exception as exc:
            return f"repo_map falhou: {type(exc).__name__}: {exc}"

    return [
        StructuredTool.from_function(
            func=repo_map_tool,
            name="repo_map",
            description=(
                "Panorama do repositório: os arquivos mais centrais (PageRank sobre quem "
                "referencia quem) e, dentro de cada um, os símbolos mais usados por outros "
                "arquivos. Use como PRIMEIRA tool num repo que você não conhece, antes de ls "
                "ou read_file: responde 'por onde começar' em uma saída, no lugar de abrir "
                "arquivos no chute. `budget_tokens` limita o tamanho da saída (default 1000). "
                "Depois disso use find_symbol/read_file no que o mapa apontou."
            ),
        ),
    ]


__all__ = [
    "BUDGET_TOKENS",
    "cache_path",
    "make_repomap_tools",
    "rank_symbols",
    "repo_map",
]
