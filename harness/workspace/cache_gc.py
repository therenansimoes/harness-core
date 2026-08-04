"""Teto do cache compartilhado de dependência (`$HARNESS_DATA_DIR/cache`).

`flow_tools._env` aponta `UV_CACHE_DIR` e `npm_config_cache` para cá de
propósito: baixar o mesmo tarball a cada run é o gasto mais bobo do loop. O
efeito colateral é que o cache só cresce — em máquina de dev isso vira dezenas
de GB sem ninguém pedir.

`gc` é LRU com uma trava: nada tocado nas últimas 24h é removido, mesmo que o
teto esteja estourado. Cache de run VIVO não pode desaparecer no meio de um
`uv sync`; melhor devolver "não coube" e avisar do que quebrar um run.

Remove ARQUIVO, não árvore: o cache do uv/npm é conteúdo endereçado por hash e
tolera entrada faltando (rebaixa e re-baixa). Diretório que ficou vazio sai
depois, para o cache não virar um cemitério de dirs.
"""

from __future__ import annotations

import time
from pathlib import Path

from harness.ledger.store import data_dir

CACHE_SUBDIR = "cache"
# Só o que o harness mesmo popula. Outro dir dentro de `cache/` é de quem o
# criou e não se apaga por conta própria.
MANAGED: tuple[str, ...] = ("uv", "npm")
DEFAULT_MAX_GB = 10.0
# Piso de idade: cache tocado agora pode ser de run em voo.
KEEP_RECENT_S = 24 * 3600
GB = 1024**3


def cache_root(data: Path | None = None) -> Path:
    return (Path(data) if data is not None else data_dir()) / CACHE_SUBDIR


def usage(data: Path | None = None) -> tuple[int, int]:
    """`(bytes, arquivos)` dos subdiretórios gerenciados. Cache ausente => zero."""
    total = files = 0
    for entrada in _entries(cache_root(data)):
        total += entrada[1]
        files += 1
    return total, files


def gc(
    max_gb: float = DEFAULT_MAX_GB,
    data: Path | None = None,
    now: float | None = None,
) -> dict:
    """Traz o cache para baixo de `max_gb` removendo o mais frio primeiro.

    `{"before", "after", "removed", "freed", "skipped_recent", "max_bytes"}`
    (bytes em `before`/`after`/`freed`). Já sob o teto: no-op, `removed=0`.
    """
    root = cache_root(data)
    agora = time.time() if now is None else now
    teto = int(max_gb * GB)
    entradas = _entries(root)
    antes = sum(size for _, size, _ in entradas)
    if antes <= teto:
        return {
            "before": antes, "after": antes, "removed": 0, "freed": 0,
            "skipped_recent": 0, "max_bytes": teto,
        }

    # Mais frio primeiro; o quente (<24h) nem entra na lista de candidatos.
    frios = sorted(
        (e for e in entradas if agora - e[2] >= KEEP_RECENT_S), key=lambda e: e[2]
    )
    quentes = len(entradas) - len(frios)
    total, removidos, liberados = antes, 0, 0
    for path, size, _ in frios:
        if total <= teto:
            break
        try:
            path.unlink()
        except OSError:  # sumiu no meio (outro gc, outro run) ou sem permissão
            continue
        total -= size
        liberados += size
        removidos += 1
    _prune_empty(root)
    return {
        "before": antes, "after": total, "removed": removidos, "freed": liberados,
        "skipped_recent": quentes, "max_bytes": teto,
    }


def _entries(root: Path) -> list[tuple[Path, int, float]]:
    """`(path, bytes, último_uso)` de cada arquivo gerenciado.

    Último uso = `max(atime, mtime)`: fs montado com `noatime` (default em
    muita instalação Linux) não atualiza atime, e aí mtime é o melhor sinal.
    Symlink não é seguido nem contado — o tamanho real está no destino.
    """
    out: list[tuple[Path, int, float]] = []
    for sub in MANAGED:
        base = root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                st = path.lstat()
            except OSError:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            out.append((path, st.st_size, max(st.st_atime, st.st_mtime)))
    return out


def _prune_empty(root: Path) -> None:
    """Diretório vazio deixado pela remoção. De baixo para cima: pai só some
    depois do filho. As raízes gerenciadas ficam, mesmo vazias."""
    for sub in MANAGED:
        base = root / sub
        if not base.is_dir():
            continue
        dirs = sorted(
            (p for p in base.rglob("*") if p.is_dir() and not p.is_symlink()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for d in dirs:
            try:
                d.rmdir()
            except OSError:  # não está vazio: fim de linha deste ramo
                continue


def human(nbytes: int) -> str:
    """Tamanho para log de uma linha. GB com uma decimal é a unidade do teto."""
    if nbytes >= GB:
        return f"{nbytes / GB:.1f}GB"
    if nbytes >= 1024**2:
        return f"{nbytes / 1024**2:.0f}MB"
    return f"{nbytes}B"
