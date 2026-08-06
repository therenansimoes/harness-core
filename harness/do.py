"""`harness do`: da pasta do usuário até uma unidade rodável, sem ele saber nada.

Todo o resto da CLI assume que existe um `unit.toml` escrito por alguém que
conhece o vocabulário (id, kind, verify_cmd, project). Este módulo é a ponte:
recebe uma frase em português e um diretório qualquer, e devolve as quatro
coisas que o grafo exige — repositório git, projeto registrado, régua
determinística e unidade em disco.

Cada função aqui é conservadora de propósito: o repo do usuário é dele, e a
única escrita que fazemos lá é `.harness/units/<id>/` (ignorada pelo git, e
apagada no fim do run). O `git init` só acontece onde não havia repo nenhum.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import unicodedata
import uuid
from pathlib import Path

from harness import paths

UNIT_FILE = "unit.toml"
UNITS_REL = Path(".harness/units")
GITIGNORE_LINE = ".harness/"
INITIAL_COMMIT = "harness: snapshot inicial"

# Identidade de commit própria: repo virgem pode não ter `user.name` configurado,
# e o `harness do` não pode morrer por causa disso. Mesma dupla que o
# `projects.integrate` usa no merge.
GIT_IDENTITY = ("-c", "user.name=harness", "-c", "user.email=harness@harness.local")

PYTEST_CMD = "python -m pytest -q"
NPM_CMD = "npm test --silent"
MAKE_CMD = "make test"
CARGO_CMD = "cargo test -q"
GO_CMD = "go test ./..."
# Sem suíte nenhuma no repo, a única coisa verificável é que o agente ESCREVEU.
# É régua fraca e assumida como tal — mas passa no `add.validate_verify_cmd`
# (não é trivial: falha quando o run não mudou arquivo nenhum) e é infinitamente
# melhor que aceitar um run que não fez nada.
#
# NÃO é mais `git status --porcelain | grep -q .`: a régua roda na worktree
# isolada, que já nasce suja do PRÓPRIO harness (`ws_setup.ensure` grava
# `.harness/setup.log` e `env_file` antes do agente, tools somam
# symbols/repomap/procs.json depois) — `git status` conta esse lixo como
# "mudou" e o `grep -q .` casa mesmo quando o agente não escreveu nada.
# `harness proof-of-write` compara contra o baseline gravado no INSTANTE do
# provision (`workspace/writeproof.py`), não contra zero: única referência
# honesta de "o que é trabalho do agente". Reprova (exit != 0) sem baseline,
# sem repo, ou sem arquivo novo — nenhum falso-aceite alcançável.
FALLBACK_CMD = "harness proof-of-write"

MAKE_TEST_RE = re.compile(r"^test:", re.MULTILINE)
# Tamanho do pedaço legível do id. O sufixo aleatório é que garante unicidade;
# o slug existe para o humano reconhecer a unidade no `git log` e na branch.
SLUG_MAX = 24


def slug(texto: str) -> str:
    """Frase em português -> `[a-z0-9-]`. Acento vira a letra base.

    `add.SLUG_RE` (que valida id de unidade em todo o resto da CLI) só aceita
    ASCII minúsculo, e o id vira nome de branch e de diretório — normalizar
    aqui é mais barato que descobrir no `git checkout`.
    """
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    bruto = re.sub(r"[^a-z0-9]+", "-", plano.lower())
    return re.sub(r"-{2,}", "-", bruto).strip("-")


def new_unit_id(texto: str) -> str:
    """`do-<pedido>-<aleatório>`. O sufixo é o que deixa repetir o mesmo pedido."""
    corpo = slug(texto)[:SLUG_MAX].strip("-") or "pedido"
    return f"do-{corpo}-{uuid.uuid4().hex[:6]}"


def pin_home_paths() -> None:
    """Trava config/data no `~/.harness` ANTES de qualquer resolução de path.

    `paths.config_dir()` prefere um `config/` do cwd — resolução legada correta
    dentro do checkout do harness e errada aqui: `harness do` roda dentro do
    repo do USUÁRIO, e um projeto Django com `config/` na raiz veria o harness
    gravar `projects.toml` e o ledger dentro dele. Env explícita continua
    vencendo (é `setdefault`), então quem aponta para uma árvore de propósito
    não perde nada.
    """
    os.environ.setdefault(paths.CONFIG_DIR_ENV, str(paths.home_root() / paths.CONFIG_SUBDIR))
    os.environ.setdefault(paths.DATA_DIR_ENV, str(paths.home_root() / paths.DATA_SUBDIR))


# --------------------------------------------------------------------------- git


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def repo_root(cwd: Path) -> Path | None:
    """Raiz do repo que contém `cwd`, ou None se não houver nenhum acima."""
    proc = _git(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top).resolve() if top else None


def current_branch(repo: Path) -> str:
    proc = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip() or "HEAD"


def _has_commit(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0


def _commit_tudo(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, *GIT_IDENTITY, "commit", "-q", "-m", msg)


def ensure_repo(cwd: Path) -> tuple[Path, bool]:
    """Garante repo git COM pelo menos um commit. Devolve `(repo, criou_repo)`.

    O grafo provisiona o workspace com `git worktree` a partir do HEAD do repo,
    então "pasta sem git" e "repo sem commit" são as duas formas de não ter por
    onde começar — as duas viram um snapshot inicial aqui.

    Working tree suja NÃO bloqueia: o run acontece num worktree isolado e nada
    do que o agente faz encosta no que o usuário estava editando. Só o merge do
    fim exige limpeza, e aí o aviso daqui já foi dado.
    """
    achado = repo_root(cwd)
    if achado is None:
        repo = Path(cwd).resolve()
        _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
        _semear_gitignore(repo)
        _semear_exclude(repo)
        _commit_tudo(repo, INITIAL_COMMIT)
        return repo, True

    _semear_exclude(achado)
    if not _has_commit(achado):
        _commit_tudo(achado, INITIAL_COMMIT)
        return achado, False

    sujo = _git(achado, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if sujo:
        print(
            f"aviso: {achado} tem mudança não commitada — o run não encosta nela "
            "(roda em worktree separado), mas o merge do fim vai pedir a árvore limpa",
            file=sys.stderr,
        )
    return achado, False


def _semear_gitignore(repo: Path) -> None:
    """`.harness/` fora do git. Só em repo que acabamos de criar: mexer no
    `.gitignore` de um repo que já existia é editar arquivo do usuário."""
    alvo = repo / ".gitignore"
    if alvo.exists():
        return
    alvo.write_text(f"{GITIGNORE_LINE}\n", encoding="utf-8")


def _semear_exclude(repo: Path) -> None:
    """`.harness/` fora do painel Changes MESMO quando o repo já tem
    `.gitignore` próprio (`_semear_gitignore` só entra no repo que a gente
    acabou de criar — este roda para TODO repo, novo ou existente).

    `info/exclude` é local ao `.git` e untracked: não vira diff pro usuário
    revisar, e worktree ligada (é assim que `_provision` roda unidade de
    projeto) herda do git-common-dir. Fail-open — não é a defesa principal
    (isso é `workspace/writeproof.py`, L1), só limpa o sintoma do painel."""
    proc = _git(repo, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        return
    common = proc.stdout.strip()
    if not common:
        return
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    exclude = common_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        atual = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if GITIGNORE_LINE in atual.splitlines():
            return
        with exclude.open("a", encoding="utf-8") as fh:
            if atual and not atual.endswith("\n"):
                fh.write("\n")
            fh.write(f"{GITIGNORE_LINE}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- régua


def detect_verify(repo: Path) -> tuple[str, str]:
    """Régua determinística a partir do que existe no repo. `(cmd, motivo)`.

    Primeiro match vence, e a ordem é por força da evidência: suíte declarada em
    config vale mais que diretório com nome sugestivo, que vale mais que
    ecossistema. O motivo viaja junto porque é o que o usuário lê para saber se
    adivinhamos certo (e trocar com `--verify-cmd` quando não).
    """
    repo = Path(repo)
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file() and "[tool.pytest" in _texto(pyproject):
        return PYTEST_CMD, "pyproject.toml [tool.pytest]"
    if (repo / "tests").is_dir():
        return PYTEST_CMD, "tests/"
    if (repo / "pytest.ini").is_file():
        return PYTEST_CMD, "pytest.ini"

    package = repo / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(_texto(package)).get("scripts")
        except ValueError:
            scripts = None
        if isinstance(scripts, dict) and scripts.get("test"):
            return NPM_CMD, "package.json scripts.test"

    makefile = repo / "Makefile"
    if makefile.is_file() and MAKE_TEST_RE.search(_texto(makefile)):
        return MAKE_CMD, "Makefile alvo test"

    if (repo / "Cargo.toml").is_file():
        return CARGO_CMD, "Cargo.toml"
    if (repo / "go.mod").is_file():
        return GO_CMD, "go.mod"
    return FALLBACK_CMD, "nenhuma suíte encontrada — a régua só prova que houve escrita"


def _texto(path: Path) -> str:
    """Leitura best-effort: arquivo ilegível é a mesma coisa que ausente para a
    detecção, e derrubar o `harness do` por causa de um `Makefile` binário seria
    trocar um palpite por um traceback."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- projeto


def ensure_project(repo: Path) -> str:
    """Registra (ou atualiza) o repo em `projects.toml` e devolve o nome.

    Idempotente pelo `init_project`. A fila fica em `data_dir()/projects/<nome>`
    e não no default relativo (`projects/<nome>/queue`, que resolve contra o
    cwd): o cwd aqui é o repo do usuário, e `harness do` não cria pasta de
    infraestrutura dentro dele.

    Repo JÁ registrado sob outro nome não vira entrada nova: quem tinha
    `/…/bancada-app` como `bancada` e rodou `harness do` de dentro da pasta
    ganhava um segundo projeto `bancada-app` apontando para o mesmo repo, com
    fila, histórico e custo partidos entre os dois nomes sem um aviso sequer.
    """
    from harness.projects import init_project

    repo = Path(repo).resolve()
    if (registrado := registered_name(repo)) is not None:
        # Só reusar o nome, sem re-registrar: o `init_project` sobrescreve a
        # entrada inteira, e os defaults do `do` apagariam `build_cmd`,
        # `setup_cmd` e `env_file` que o usuário pôs lá na mão.
        print(f"usando projeto já registrado: {registrado}")
        return registrado
    nome = project_name(repo)
    init_project(
        repo,
        nome,
        verify_default=detect_verify(repo)[0],
        queue_dir=paths.data_dir().resolve() / "projects" / nome / "queue",
    )
    return nome


def registered_name(repo: Path) -> str | None:
    """Nome sob o qual este repo já está em `projects.toml`, ou None.

    Compara path RESOLVIDO dos dois lados: o registro pode ter vindo relativo
    (`repo = "projects/x"`), com `~` ou com symlink no meio, e o que identifica
    um projeto é o repositório, não a string que alguém digitou.
    """
    from harness.projects import load_projects

    repo = Path(repo).resolve()
    for nome, proj in load_projects().items():
        if Path(proj.repo).expanduser().resolve() == repo:
            return nome
    return None


def project_name(repo: Path) -> str:
    """`slug(nome da pasta)`, com sufixo de hash quando o nome já é de outro repo.

    Duas pastas `api/` em árvores diferentes é o caso normal de quem tem muitos
    projetos; deixar a segunda sobrescrever o registro da primeira apagaria o
    histórico do ledger de um projeto que ainda existe.
    """
    from harness.projects import load_projects

    repo = Path(repo).resolve()
    base = slug(repo.name) or "projeto"
    if len(base) < 2:  # `add.SLUG_RE` exige 2 caracteres no mínimo
        base = f"{base}0"
    registrado = load_projects().get(base)
    if registrado is None or Path(registrado.repo).resolve() == repo:
        return base
    return f"{base}-{hashlib.sha1(str(repo).encode('utf-8')).hexdigest()[:6]}"


# --------------------------------------------------------------------------- unidade

# Regra de entrega colada em TODO pedido do `do`. Numa tarefa grande o agente
# deixou `COMMIT_INSTRUCTIONS.txt` e `DASHBOARD_CHANGES.md` na raiz do repo, e o
# integrate commitou os dois no branch default: no `do` não existe revisor entre
# o que o agente escreve em arquivo e o repo do usuário. Explicação é resposta,
# não artefato.
# `run_graph._gate_text` remove ESTE texto antes de medir o gate do plano — o
# bloco sozinho estoura PLAN_PROMPT_CHARS e casa PLAN_TRIGGERS. Mudou aqui,
# o teste de drift em tests/test_planning.py quebra.
ENTREGA_LIMPA = """Regras de entrega (valem acima de qualquer preferência sua):
- Entregue SÓ os arquivos do produto: o que o pedido acima precisa para funcionar.
- NÃO crie arquivo de anotação, resumo, instrução ou plano (README de mudanças, \
RESUMO, TODO, NOTES, COMMIT_*, CHANGES_*) — tudo que você escreve em arquivo é \
commitado no repositório do usuário.
- A explicação do que você fez vai na sua RESPOSTA, não em arquivo."""


def unit_prompt(task: str) -> str:
    """Pedido do usuário + a convenção de entrega que ele não sabe que precisa pedir.

    O pedido vem primeiro e literal: quem abre o `unit.toml` tem de reconhecer a
    própria frase antes de qualquer texto nosso.
    """
    return f"{task.strip()}\n\n{ENTREGA_LIMPA}\n"


def write_unit(
    repo: Path,
    unit_id: str,
    prompt: str,
    verify_cmd: str,
    project: str,
    kind: str | None = None,
) -> Path:
    """Grava `.harness/units/<id>/unit.toml` e devolve o diretório.

    Dentro do repo (e não em tmp) porque a unidade é o registro do que foi
    pedido: enquanto o run acontece, ela é o único lugar onde o pedido original
    está escrito, e um crash não pode levá-la junto. Quem não passa
    `--keep-unit` vê o diretório sumir no fim.
    """
    unit_dir = Path(repo) / UNITS_REL / unit_id
    linhas = [
        "# Gerado por `harness do`. Sem --keep-unit, some quando o run termina.",
        f"id = {_toml_str(unit_id)}",
        f"project = {_toml_str(project)}",
    ]
    if kind:
        linhas.append(f"kind = {_toml_str(kind)}")
    linhas += [
        f"verify_cmd = {_toml_str(verify_cmd)}",
        f"prompt = {_toml_str(prompt)}",
        "",
    ]
    conteudo = "\n".join(linhas)
    tomllib.loads(conteudo)  # round-trip: o que sai daqui SEMPRE carrega
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_FILE).write_text(conteudo, encoding="utf-8")
    return unit_dir


def _toml_str(value: str) -> str:
    """Literal (`'''...'''`) quando o texto cabe nele, senão basic string.

    O literal preserva o pedido EXATAMENTE como o usuário digitou (nenhum
    escape) e é o que ele vai ler se abrir o arquivo — que é metade do ponto de
    gravar a unidade no repo. Texto com a própria delimitação dentro cai no
    `json.dumps`, que produz basic string TOML válida (mesmo truque do `add`).
    """
    if "'''" not in value and not value.startswith("\n") and not value.endswith("'"):
        return f"'''{value}'''"
    return json.dumps(value, ensure_ascii=False)
