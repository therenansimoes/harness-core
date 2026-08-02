"""doctor: o diagnóstico que roda antes de acusar o harness de quebrado.

É a verificação global da SPEC em forma de comando — preflight de todos os
backends, genoma que carrega e fingerprinta, telemetria de terceiro desligada,
msgpack estrito, `data/` gravável, `config/*.toml` que parseiam, catálogo
válido. Zero chamada de LLM, zero rede: um check que custa dinheiro não é
diagnóstico, é run.

Duas severidades, e a diferença importa. **FALHA** é coisa nossa quebrada — o
genoma não carrega, o catálogo não parseia, o diretório de dados não aceita
escrita: sem isso o harness não roda direito e o exit code diz isso. **aviso**
é o mundo lá fora não estando pronto — backend sem credencial, servidor local
desligado. Backend indisponível não é defeito do harness (o `mock` sempre está
lá), e um doctor que sai 1 porque o Ollama não está no ar vira ruído que
ninguém lê.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from harness.backends import registry
from harness.genome.genome import load
from harness.genome.tamper import fingerprint, immutable_files
from harness.improve import CONFIG_SUBDIR, genome_path, root_dir
from harness.improve.target import CatalogError, load_catalog
from harness.ledger import store

OK = "ok"
FAIL = "FALHA"
WARN = "aviso"

# Vars de telemetria consideradas "ligadas". Só o token explícito conta:
# `LANGSMITH_ENDPOINT=https://…` é configuração de quem optou por LangSmith em
# outro projeto, não tracing ligado neste processo.
TRACING_PREFIXES = ("LANGSMITH_", "LANGCHAIN_TRACING")
TRACING_ON = frozenset({"1", "true", "yes", "on"})

STRICT_MSGPACK = "LANGGRAPH_STRICT_MSGPACK"


@dataclass(frozen=True)
class Check:
    """Uma linha do relatório: o que foi checado, como saiu, e a evidência."""

    name: str
    status: str
    detail: str


def checks(root: Path | str | None = None, data: Path | None = None) -> list[Check]:
    """Todos os checks, na ordem em que são impressos."""
    base = root_dir(root)
    data_dir = Path(data) if data is not None else store.data_dir()
    return [
        _genome(base),
        _tracing(),
        _msgpack(),
        *_config(base),
        _data(data_dir),
        _ledger(data_dir / store.DB_NAME),
        *_backends(),
    ]


def failures(result: list[Check]) -> list[Check]:
    """Só o que impede o harness de rodar — aviso não entra."""
    return [c for c in result if c.status == FAIL]


def _genome(base: Path) -> Check:
    path = genome_path(base)
    try:
        g = load(path)
        fp = fingerprint(g, base)
        files = immutable_files(g, base)
    except (OSError, ValueError) as exc:
        return Check("genome", FAIL, str(exc))
    return Check(
        "genome",
        OK,
        f"{path} fingerprint={fp[:12]} imutáveis={len(files)} "
        f"padrões={len(g.immutable)}+{len(g.mutable)}",
    )


def _tracing() -> Check:
    on = sorted(
        k
        for k, v in os.environ.items()
        if k.startswith(TRACING_PREFIXES) and v.strip().lower() in TRACING_ON
    )
    if on:
        return Check("tracing", FAIL, f"telemetria ligada: {', '.join(on)}")
    return Check("tracing", OK, "LANGSMITH_*/LANGCHAIN_TRACING_* desligados")


def _msgpack() -> Check:
    value = os.environ.get(STRICT_MSGPACK, "")
    if value.strip().lower() != "true":
        # langgraph-checkpoint 3.x: sem modo estrito, um DB comprometido executa
        # código na desserialização. O bootstrap da CLI liga; se chegou aqui
        # desligado, alguém desligou de propósito.
        return Check("msgpack", FAIL, f"{STRICT_MSGPACK}={value or '(vazio)'}, esperado true")
    return Check("msgpack", OK, f"{STRICT_MSGPACK}=true")


def _config(base: Path) -> list[Check]:
    folder = base / CONFIG_SUBDIR
    if not folder.is_dir():
        return [Check("config", FAIL, f"{folder} não existe")]
    files = sorted(folder.glob("*.toml"))
    if not files:
        return [Check("config", FAIL, f"{folder} sem nenhum *.toml")]
    broken: list[str] = []
    for f in files:
        try:
            tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            broken.append(f"{f.name} ({exc})")
    config = (
        Check("config", FAIL, f"{len(broken)} de {len(files)} não parseiam: "
                              f"{'; '.join(broken)}")
        if broken
        else Check("config", OK, f"{len(files)} toml: "
                                 f"{', '.join(f.name for f in files)}")
    )
    return [config, _catalog(base)]


def _catalog(base: Path) -> Check:
    try:
        rules, cfg = load_catalog(root=base)
    except CatalogError as exc:
        return Check("catalog", FAIL, str(exc))
    if not rules:
        # Não é FALHA: catálogo vazio é loop sem gradiente, que escala pro
        # humano por desenho. É aviso porque `harness improve` não tem o que
        # fazer até alguém escrever uma regra.
        return Check("catalog", WARN, "nenhuma [[rule]] — improve não tem o que propor")
    return Check(
        "catalog",
        OK,
        f"{len(rules)} regra(s), n_per_arm={cfg['n_per_arm']:g} "
        f"window={cfg['window']:g}",
    )


def _data(path: Path) -> Check:
    """`data/` aceita escrita? Sonda de verdade quando existe; permissão do pai
    quando não — doctor é diagnóstico e não sai criando árvore por conta."""
    if not path.exists():
        parent = next((p for p in path.parents if p.exists()), Path("."))
        if not os.access(parent, os.W_OK):
            return Check("data", FAIL, f"{path} não existe e {parent} não é gravável")
        return Check("data", OK, f"{path} ainda não existe; {parent} é gravável")
    if not path.is_dir():
        return Check("data", FAIL, f"{path} existe e não é diretório")
    probe = path / f".doctor.{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("data", FAIL, f"{path} não é gravável: {exc}")
    return Check("data", OK, f"{path} gravável")


def _ledger(db: Path) -> Check:
    if not db.is_file():
        return Check("ledger", OK, f"{db} ainda não existe (nasce no primeiro run)")
    try:
        with sqlite3.connect(db) as conn:
            runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            muts = conn.execute("SELECT count(*) FROM mutations").fetchone()[0]
    except sqlite3.Error as exc:
        return Check("ledger", FAIL, f"{db} ilegível: {exc}")
    return Check("ledger", OK, f"{db} runs={runs} mutações={muts}")


def _backends() -> list[Check]:
    out: list[Check] = []
    for name in registry.available():
        try:
            pre = registry.get_backend(name).preflight()
        except Exception as exc:
            # Preflight que EXPLODE é bug nosso (ele é determinístico e sem
            # rede por contrato); indisponibilidade se responde com ok=False.
            out.append(Check(f"backend:{name}", FAIL, f"{type(exc).__name__}: {exc}"))
            continue
        status = OK if pre.ok else WARN
        out.append(Check(f"backend:{name}", status, pre.reason))
    return out
