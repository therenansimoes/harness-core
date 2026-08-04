"""doctor: o diagnóstico que roda antes de acusar o harness de quebrado.

É a verificação global da SPEC em forma de comando — preflight de todos os
backends, genoma que carrega e fingerprinta, telemetria de terceiro desligada,
msgpack estrito, `data/` gravável, `config/*.toml` que parseiam, catálogo
válido. Zero chamada de LLM: um check que custa dinheiro não é diagnóstico, é
run. A única rede é a sonda em loopback do runtime local (`GET /v1/models` no
LM Studio), que não gasta token.

Duas severidades, e a diferença importa. **FALHA** é coisa nossa quebrada — o
genoma não carrega, o catálogo não parseia, o diretório de dados não aceita
escrita: sem isso o harness não roda direito e o exit code diz isso. **aviso**
é o mundo lá fora não estando pronto — backend sem credencial, servidor local
desligado. Backend indisponível não é defeito do harness (o `mock` sempre está
lá), e um doctor que sai 1 porque o LM Studio não está no ar vira ruído que
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
        _skills(base),
        _topology(base),
        _plugin_nodes(base),
        _actions(),
        *_optional_toml(base),
        _lineage(data_dir),
        _procs(data_dir),
        _executor(base),
        _lmstudio(base),
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


def _skills(base: Path) -> Check:
    """Loader nunca levanta por contrato; exceção aqui é bug nosso."""
    try:
        from harness.skills.loader import load_skills

        skills = load_skills(base / "skills")
    except Exception as exc:
        return Check("skills", FAIL, f"{type(exc).__name__}: {exc}")
    if not skills:
        return Check("skills", OK, f"{base / 'skills'} sem skill (dir ausente ou vazio)")
    return Check("skills", OK, f"{len(skills)} skill(s): {', '.join(s.name for s in skills)}")


def _topology(base: Path) -> Check:
    """Spec torta/ausente não é FALHA: build_run_graph cai no default por desenho."""
    try:
        from harness.graph import topology

        path = base / CONFIG_SUBDIR / topology.TOPOLOGY_TOML
        spec = topology.load_spec(path)
        topology.compile_spec(spec)
    except Exception as exc:
        return Check(
            "topology", WARN,
            f"{type(exc).__name__}: {exc} — run_graph usa a topologia default",
        )
    return Check(
        "topology", OK,
        f"{path} compila: nodes={len(spec.get('nodes', []))} "
        f"edges={len(spec.get('edges', []))}",
    )


def _plugin_nodes(base: Path) -> Check:
    """Nó de plugin recusado NUNCA é FALHA, pelo mesmo motivo do _topology: o
    grafo roda sem ele. O que o doctor faz aqui é mostrar o que foi recusado e
    por quê — recusa silenciosa é como um ack esquecido parece bug de código."""
    try:
        from harness.graph import plugin_nodes

        folder = plugin_nodes.nodes_dir(base)
        if plugin_nodes.disabled():
            return Check(
                "plugin_nodes", OK,
                f"{plugin_nodes.KILL_SWITCH}="
                f"{os.environ.get(plugin_nodes.KILL_SWITCH, '')} — nenhum nó de "
                f"plugin carrega, nem aprovado",
            )
        result = plugin_nodes.register_all(root=base)
    except Exception as exc:
        return Check("plugin_nodes", WARN, f"{type(exc).__name__}: {exc}")
    if not result:
        return Check("plugin_nodes", OK, f"{folder} sem nó (dir ausente ou vazio)")
    ok = sorted(n for n, r in result.items() if r == "registered")
    bad = sorted((n, r) for n, r in result.items() if r != "registered")
    detail = f"{len(ok)} registrado(s)" + (f": {', '.join(ok)}" if ok else "")
    if bad:
        recusas = "; ".join(f"{n}: {r}" for n, r in bad)
        return Check("plugin_nodes", WARN, f"{detail} — recusados: {recusas}")
    return Check("plugin_nodes", OK, detail)


def _actions() -> Check:
    try:
        from harness.improve.target import actions

        acts = actions()
    except Exception as exc:
        return Check("actions", FAIL, f"registry não carrega: {type(exc).__name__}: {exc}")
    if not acts:
        return Check("actions", FAIL, "registry carrega mas nenhuma ação registrada")
    return Check("actions", OK, f"{len(acts)} ação(ões): {', '.join(sorted(acts))}")


def _optional_toml(base: Path) -> list[Check]:
    """ruler.toml e mcp.toml são opcionais: ausente é ok com nota, torto é FALHA."""
    out: list[Check] = []
    for stem in ("ruler", "mcp"):
        path = base / CONFIG_SUBDIR / f"{stem}.toml"
        if not path.is_file():
            out.append(Check(stem, OK, f"{path} ausente (opcional)"))
            continue
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            out.append(Check(stem, FAIL, f"{path} não parseia: {exc}"))
            continue
        out.append(Check(stem, OK, f"{path} parseia"))
    return out


def _lineage(data_dir: Path) -> Check:
    path = data_dir / "lineage.jsonl"
    if not path.is_file():
        return Check("lineage", OK, f"{path} ainda não existe (nasce no primeiro codegen)")
    try:
        from harness.improve.lineage import load_lineage

        entries = load_lineage(path)
    except Exception as exc:
        return Check("lineage", FAIL, f"{path} ilegível: {type(exc).__name__}: {exc}")
    return Check("lineage", OK, f"{path} entradas={len(entries)}")


def _procs(data_dir: Path) -> Check:
    """Servidor sobrevivendo ao run que o subiu segura porta e worktree.

    Órfão é registro cujo `harness_pid` já morreu — ninguém mais vai chamar o
    cleanup dele. AVISO, nunca FALHA: processo pendurado é sujeira da máquina
    (`harness procs --reap` limpa), não harness quebrado.
    """
    from harness.backends import procs

    root = data_dir / "ws"
    vivos = orfaos = 0
    for path in root.glob(f"*/{procs.HARNESS_SUBDIR}/{procs.PROCS_FILE}"):
        for entry in procs.read_procs(path.parent.parent):
            try:
                os.kill(int(entry.get("harness_pid")), 0)
            except (OSError, TypeError, ValueError):
                orfaos += 1
            else:
                vivos += 1
    if orfaos:
        return Check(
            "procs", WARN, f"{orfaos} processo(s) órfão(s) — `harness procs --reap` limpa"
        )
    return Check("procs", OK, f"0 processos órfãos (registrados de runs vivos: {vivos})")


def _executor(base: Path) -> Check:
    path = base / "prompts" / "executor.md"
    if not path.is_file():
        return Check("executor", WARN, f"{path} ausente — executor roda sem prompt base")
    return Check("executor", OK, f"{path} ({path.stat().st_size} bytes)")


def _lmstudio(base: Path) -> Check:
    """Runtime local: LM Studio é o único (Ollama cortado em 2026-08-04).

    Mesma sonda do preflight do backend, no modelo do tier local mais barato —
    é o modelo que a maioria dos runs vai usar. Servidor desligado é AVISO: o
    mundo lá fora não estar pronto não é defeito do harness."""
    # Import local: o backend puxa skills/roles e só é útil aqui.
    from harness.backends.deepagents_backend import OPENAI_PREFIX, _lmstudio_preflight

    path = base / CONFIG_SUBDIR / "models.toml"
    try:
        tiers = tomllib.loads(path.read_text(encoding="utf-8")).get("tier", [])
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Check("lmstudio", WARN, f"{path}: {exc}")
    local = sorted(
        (t for t in tiers if str(t.get("model", "")).startswith(OPENAI_PREFIX)),
        key=lambda t: t.get("cost_rank", 0),
    )
    if not local:
        return Check("lmstudio", OK, "nenhum tier usa modelo local")
    pre = _lmstudio_preflight(str(local[0]["model"]))
    return Check("lmstudio", OK if pre.ok else WARN, pre.reason)


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
