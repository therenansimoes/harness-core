"""Aplica e reverte UMA chave de UM toml — cirurgia, não reescrita.

Reescrever o arquivo com um dumper de TOML seria trivial e destruiria o que
esses arquivos têm de mais caro: os comentários que explicam cada knob. Então a
mutação troca só o token do valor e devolve o token antigo dentro da `Mutation`
— `apply` seguido de `revert` é byte-idêntico, e é o teste que prova.

O genoma é consultado ANTES de escrever, nunca depois: um `check_patch` a
posteriori descobriria a violação com o arquivo já mudado. Três causas de
rejeição, todas fechadas:

    genome:immutable    a regra aponta para a zona que julga (régua, router…)
    genome:not_mutable  aponta para fora do que o genoma declarou calibrável
    genome:self_edit    aponta para o catálogo ou para o genoma — a regra que
                        afrouxa o critério de escolha de regras (ou a lista do
                        que pode ser mudado) aprova a si mesma

O alvo é normalizado por realpath antes dos três testes, com a MESMA rotina do
`check_patch`: sem isso `config/alias.toml -> catalog.toml` casaria `mutable` e
entraria pela porta dos fundos do self_edit.

A gramática de `key` é a mínima que os config do repo pedem: `tabela.chave` e
`array[i].chave`. Valor tem que ser escalar numa linha só; array multi-linha e
string tripla não são knob de calibração e viram erro explícito em vez de um
regex criativo.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `_relative` é privado de propósito (é detalhe do genoma), mas normalizar aqui
# por conta própria seria manter duas rotinas de resolução de path e descobrir a
# divergência no dia em que uma delas deixar passar um symlink.
from harness.genome.genome import Genome, _relative, check_patch, load, matches
from harness.improve import CATALOG_FILE, CONFIG_SUBDIR, GENOME_FILE, genome_path, root_dir

NOT_MUTABLE = "genome:not_mutable"
SELF_EDIT = "genome:self_edit"

# Os dois arquivos que descrevem o que o loop pode fazer. Ambos casam
# `config/*.toml` no genoma (o humano calibra os dois), e é justamente por isso
# que o loop não pode se apontar para eles.
SELF_EDIT_FILES = tuple(f"{CONFIG_SUBDIR}/{name}" for name in (CATALOG_FILE, GENOME_FILE))

_ASSIGN = re.compile(r"^(?P<pre>\s*)(?P<key>[A-Za-z0-9_\-]+|\"[^\"]*\")\s*=\s*")
_HEADER = re.compile(r"^\s*(?P<open>\[\[?)\s*(?P<name>[^\[\]]+?)\s*\]\]?\s*$")
_PATH_SEG = re.compile(r"^(?P<name>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


class MutationError(Exception):
    """A chave não existe, o valor não é escalar ou o arquivo mudou embaixo."""


class GenomeViolation(Exception):
    """Regra barrada pelo genoma. `violations` é o que vai para o ledger."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


@dataclass(frozen=True)
class Mutation:
    """O que foi mudado e o que é preciso para desfazer, em texto puro.

    Só campos `str`: a mutação atravessa o checkpoint do grafo, e estado que
    volta do banco como dict inerte não pode perder informação no caminho.
    """

    mutation_id: str
    rule_id: str
    target_file: str
    key: str
    before_raw: str
    after_raw: str
    applied_at: str


def mutation_id(rule_id: str, ts: str) -> str:
    """Determinístico de propósito: o mesmo ciclo retomado depois de um crash
    recalcula o MESMO id e o `INSERT OR IGNORE` do ledger não duplica a linha."""
    return hashlib.sha256(f"{rule_id}\0{ts}".encode("utf-8")).hexdigest()[:12]


def check(rule, root: Path | str | None = None, genome: Genome | None = None) -> list[str]:
    """Violações da regra contra o genoma. Lista vazia = pode aplicar."""
    base = root_dir(root)
    g = genome if genome is not None else load(genome_path(base))
    raw = Path(rule.target_file).as_posix()

    violations = check_patch(g, [raw], root=base)
    if violations:
        return violations
    # `check_patch` limpo garante que o path não escapa da raiz, então aqui
    # `_relative` nunca devolve None — e devolve o path JÁ resolvido, que é o
    # único contra o qual comparar catálogo e genoma.
    rel = _relative(raw, base) or raw
    if rel in SELF_EDIT_FILES:
        return [f"{SELF_EDIT}:{rel}"]
    if not matches(rel, g.mutable):
        return [f"{NOT_MUTABLE}:{rel}"]
    return []


def read_value(path: Path | str, key: str) -> Any:
    """Valor corrente da chave, pelo parser oficial — o regex só escreve."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        # Regra apontada para arquivo que nem é TOML entra por aqui; vira
        # MutationError para o chamador tratar como regra inaplicável, não como
        # crash do loop.
        raise MutationError(f"{path}: não é TOML válido ({e})") from e
    except UnicodeDecodeError as e:
        raise MutationError(f"{path}: não é texto ({e})") from e
    node: Any = data
    section, leaf = _split(key)
    for name, index in section:
        node = _descend(node, name, key)
        if index is not None:
            if not isinstance(node, list) or index >= len(node):
                raise MutationError(f"{path}: {key}: índice {index} fora de {name}")
            node = node[index]
    return _descend(node, leaf, key)


def apply(rule, ts: str, root: Path | str | None = None, genome: Genome | None = None) -> Mutation:
    """Checa o genoma, confere o `from` e troca o valor. Devolve a `Mutation`.

    O `from` da regra não é decoração: catálogo escrito contra uma versão
    antiga do config aplicaria um "de/para" que não descreve o arquivo, e o
    revert restauraria um valor que nunca esteve lá.
    """
    violations = check(rule, root=root, genome=genome)
    if violations:
        raise GenomeViolation(violations)

    path = root_dir(root) / rule.target_file
    current = read_value(path, rule.key)
    if current != rule.from_value:
        raise MutationError(
            f"{path}: {rule.key} vale {current!r}, a regra {rule.id!r} esperava "
            f"{rule.from_value!r} — catálogo desatualizado"
        )

    text = path.read_text(encoding="utf-8")
    start, end = _locate(text, rule.key, path)
    before_raw = text[start:end]
    after_raw = _render(rule.to_value)
    _write(path, text[:start] + after_raw + text[end:])

    applied = read_value(path, rule.key)
    if applied != rule.to_value:
        # Escreveu e não virou o que devia: desfaz na hora, não deixa meio-termo.
        _write(path, text)
        raise MutationError(
            f"{path}: {rule.key} virou {applied!r} em vez de {rule.to_value!r}"
        )
    return Mutation(
        mutation_id=mutation_id(rule.id, ts),
        rule_id=rule.id,
        target_file=rule.target_file,
        key=rule.key,
        before_raw=before_raw,
        after_raw=after_raw,
        applied_at=ts,
    )


def toggle(mutation: Mutation, root: Path | str | None = None, *, applied: bool) -> bool:
    """Liga (`applied=True`) ou desliga a mutação. True = o arquivo mudou agora.

    É o que monta os braços do A/B: a config é estado global do processo, então
    o braço não é "outro objeto", é o mesmo arquivo em outro estado. Idempotente
    de propósito — pedir o estado em que o arquivo já está não é erro, e é isso
    que faz o revert do resume não explodir.

    Token que não é nem o de antes nem o de depois é mudança de terceiro:
    sobrescrever em nome de rollback é o revert-em-silêncio que o `cli._revert`
    já ensinou a não fazer.
    """
    path = root_dir(root) / mutation.target_file
    text = path.read_text(encoding="utf-8")
    start, end = _locate(text, mutation.key, path)
    found = text[start:end]
    want = mutation.after_raw if applied else mutation.before_raw
    if found == want:
        return False
    if found != (mutation.before_raw if applied else mutation.after_raw):
        raise MutationError(
            f"{path}: {mutation.key} está {found!r}, a mutação "
            f"{mutation.mutation_id} conhece {mutation.before_raw!r}/"
            f"{mutation.after_raw!r}"
        )
    _write(path, text[:start] + want + text[end:])
    return True


def revert(mutation: Mutation, root: Path | str | None = None) -> bool:
    """Devolve o token original — byte a byte o que estava lá antes."""
    return toggle(mutation, root, applied=False)


def _write(path: Path, text: str) -> None:
    """Grava o toml inteiro de forma atômica: tmp irmão + `os.replace`.

    Crash no meio de um `write_text` deixa config TRUNCADA, que é o pior estado
    possível — pior que a mutação aplicada e pior que a mutação ausente, porque
    nem o revert nem o parser conseguem sair de lá. Com `os.replace` o arquivo
    ou é o de antes ou é o de depois.

    Escreve no realpath: se o config for um symlink, o alvo é que muda — trocar
    o link por um arquivo comum seria mudança estrutural que ninguém pediu.
    """
    real = Path(os.path.realpath(path))
    tmp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, os.stat(real).st_mode & 0o7777)   # tmp novo nasce com o umask
    os.replace(tmp, real)


# --- localização do token no texto --------------------------------------------


def _split(key: str) -> tuple[list[tuple[str, int | None]], str]:
    """`tier[0].max_turns` -> ([("tier", 0)], "max_turns")."""
    parts = key.split(".")
    section: list[tuple[str, int | None]] = []
    for raw in parts[:-1]:
        m = _PATH_SEG.match(raw)
        if m is None:
            raise MutationError(f"segmento de chave inválido: {raw!r} em {key!r}")
        idx = m.group("index")
        section.append((m.group("name"), int(idx) if idx is not None else None))
    return section, parts[-1]


def _descend(node: Any, name: str, key: str) -> Any:
    if not isinstance(node, dict) or name not in node:
        raise MutationError(f"chave inexistente: {key!r} (falta {name!r})")
    return node[name]


def _section_label(section: list[tuple[str, int | None]]) -> str:
    return ".".join(
        name if index is None else f"{name}[{index}]" for name, index in section
    )


def _locate(text: str, key: str, path: Path) -> tuple[int, int]:
    """Offsets (início, fim) do token do valor de `key` dentro de `text`."""
    section, leaf = _split(key)
    want = _section_label(section)

    pos = 0
    current = ""
    counters: dict[str, int] = {}
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        header = _HEADER.match(stripped) if stripped.startswith("[") else None
        if header is not None:
            name = header.group("name").strip()
            if header.group("open") == "[[":
                current = f"{name}[{counters.get(name, 0)}]"
                counters[name] = counters.get(name, 0) + 1
            else:
                current = name
        elif current == want and (m := _ASSIGN.match(line)) is not None:
            if m.group("key").strip('"') == leaf:
                return _value_span(line, m.end(), pos, key, path)
        pos += len(line)
    raise MutationError(f"{path}: chave não encontrada no texto: {key!r}")


def _value_span(line: str, col: int, base: int, key: str, path: Path) -> tuple[int, int]:
    """Fim do token do valor: fecha a aspa, ou para no comentário."""
    rest = line[col:]
    if rest[:3] in ('"""', "'''") or rest.rstrip("\r\n").endswith(("[", "{")):
        raise MutationError(f"{path}: {key!r} não é escalar de uma linha")
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end < 0:
            raise MutationError(f"{path}: {key!r}: string sem fechamento")
        length = end + 1
    else:
        length = len(rest.partition("#")[0].rstrip())
        if length == 0:
            raise MutationError(f"{path}: {key!r} sem valor na linha")
    return base + col, base + col + length


def _render(value: Any) -> str:
    """Valor Python -> token TOML. Só escalar: o resto não é knob."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise MutationError(f"valor não escalar não vira token TOML: {value!r}")
