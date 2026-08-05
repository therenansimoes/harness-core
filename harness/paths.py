"""Onde o harness lê config e escreve dado — precedência ÚNICA, em call-time.

Antes daqui cada módulo resolvia sozinho: `Path("config")` no router,
`Path("data")` no ledger, `parents[2]/"config"` no ruler. Isso funciona rodando
de dentro do checkout e só de lá: instalado por wheel, `parents[2]` é
site-packages e `Path("config")` é o cwd de quem chamou. As resoluções viram
estas funções, e a ordem é sempre a mesma:

    env explícita  >  árvore do cwd  >  `~/.harness`  >  defaults empacotados

Duas regras que este módulo NÃO quebra:

- nenhum import de `harness.*` — o ledger importa daqui, então a seta só pode
  apontar para fora;
- nada de I/O no import — quem chama pode ter trocado cwd ou env depois de o
  módulo carregar (o autopilot faz exatamente isso com `$HARNESS_CONFIG_DIR`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME_ENV = "HARNESS_HOME"
CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"
DATA_DIR_ENV = "HARNESS_DATA_DIR"
EVALS_DIR_ENV = "HARNESS_EVALS_DIR"

CONFIG_SUBDIR = "config"
DATA_SUBDIR = "data"
SKILLS_SUBDIR = "skills"
EVALS_SUBDIR = "evals"

# `~/.harness`: mesma convenção de CLI que guarda estado no home, e é o único
# lugar gravável garantido quando o pacote está num site-packages read-only.
HOME_ROOT = Path(os.environ.get(HOME_ENV, Path.home() / ".harness"))

# `harness/_defaults/` só existe na wheel (force-include do pyproject). No
# checkout os mesmos dados são `config/`, `skills/` e `templates/` na raiz do
# repo, irmãos do pacote.
_PACKAGE_ROOT = Path(__file__).resolve().parent
_BUNDLED_DEFAULTS = "_defaults"


def home_root() -> Path:
    """`HOME_ROOT`, mas relendo a env — o valor do import é só o default.

    A env vale em call-time porque teste (e wrapper de shell) a seta depois de
    o módulo já estar carregado; `HOME_ROOT` continua sendo o ponto de
    monkeypatch para quem não quer mexer no ambiente.
    """
    env = os.environ.get(HOME_ENV)
    return Path(env) if env else HOME_ROOT


def source_root() -> Path | None:
    """Raiz do checkout do harness, ou `None` quando rodando de uma wheel.

    Os dois marcadores juntos, não um: `pyproject.toml` sozinho é qualquer
    projeto Python, e `config/` sozinho pode ser config de usuário copiada.
    """
    root = _PACKAGE_ROOT.parent
    if (root / CONFIG_SUBDIR).is_dir() and (root / "pyproject.toml").is_file():
        return root
    return None


def packaged_defaults() -> Path:
    """Raiz dos dados versionados que VIAJAM com o pacote (read-only).

    Contém `config/`, `skills/` e `templates/`. É o último recurso de leitura e
    a fonte do `ensure_user_config()` — ninguém escreve aqui.
    """
    bundled = _PACKAGE_ROOT / _BUNDLED_DEFAULTS
    if bundled.is_dir():
        return bundled
    return source_root() or bundled


def config_dir() -> Path:
    """Onde vivem os TOML calibráveis.

    `./config` do cwd vence `~/.harness/config` mesmo fora de um checkout: quem
    tem um `config/` ao lado quer calibrar AQUELE projeto — é a resolução
    legada, e rodando do repo nada muda de comportamento.
    """
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    cwd_config = Path(CONFIG_SUBDIR)
    return cwd_config if cwd_config.is_dir() else home_root() / CONFIG_SUBDIR


def data_dir() -> Path:
    """Onde o ledger, os workspaces e os logs escrevem.

    O gatilho do `./data` é o `./config` do cwd, não o próprio `./data`: o
    banco nasce vazio e nunca existiria antes do primeiro run. Fora de uma
    árvore com config, escrever é no home — rodar o harness num diretório
    qualquer não pode espalhar `data/` por onde o usuário passou.
    """
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return Path(DATA_SUBDIR) if Path(CONFIG_SUBDIR).is_dir() else home_root() / DATA_SUBDIR


def skills_dir() -> Path:
    """`skills/` irmão do `config_dir()` em uso; senão o do cwd; senão o do pacote.

    Amarrado ao config primeiro, e não ao cwd, de propósito: quem aponta
    `$HARNESS_CONFIG_DIR` para uma árvore quer as skills DAQUELA árvore. O
    `skills/` solto do cwd continua valendo (é a resolução legada do loader, e
    é como o teste monta uma árvore de skill sem config nenhuma).
    """
    sibling = config_dir().parent / SKILLS_SUBDIR
    if sibling.is_dir():
        return sibling
    cwd_skills = Path(SKILLS_SUBDIR)
    return cwd_skills if cwd_skills.is_dir() else packaged_defaults() / SKILLS_SUBDIR


def evals_dir() -> Path:
    """`evals/` irmão do `config_dir()` em uso; senão o do cwd; senão o do pacote.

    Mesma precedência de `skills_dir()` — o bundle de eval espelha o path do
    artefato avaliado, então tem que resolver na MESMA árvore em que a skill
    resolveu, senão o exame congelado de uma árvore julgaria a skill de outra.

    A env própria vem antes de tudo (e `skills_dir()` não tem equivalente) por
    um motivo estreito: o bundle é dado de teste, e a suíte precisa apontá-lo
    para um tmpdir sem ter que montar uma árvore de config inteira em volta.
    """
    env = os.environ.get(EVALS_DIR_ENV)
    if env:
        return Path(env)
    sibling = config_dir().parent / EVALS_SUBDIR
    if sibling.is_dir():
        return sibling
    cwd_evals = Path(EVALS_SUBDIR)
    return cwd_evals if cwd_evals.is_dir() else packaged_defaults() / EVALS_SUBDIR


def config_file(name: str) -> Path:
    """Caminho de um TOML de config, com fallback para o default empacotado.

    Devolve o do `config_dir()` quando ele existe; senão o de
    `packaged_defaults()`, que pode não existir também — quem lê já trata
    ausência como "usa o default congelado no código".
    """
    local = config_dir() / name
    return local if local.is_file() else packaged_defaults() / CONFIG_SUBDIR / name


def ensure_user_config() -> Path:
    """Cria `~/.harness/config` com os TOML de default que faltam. Idempotente.

    NUNCA sobrescreve: config calibrada pelo usuário (ou pelo loop de melhoria)
    é dado, não cache. TOML novo de uma versão nova entra; o que já está fica.
    """
    root = home_root()
    conf = root / CONFIG_SUBDIR
    conf.mkdir(parents=True, exist_ok=True)
    (root / DATA_SUBDIR).mkdir(parents=True, exist_ok=True)
    src = packaged_defaults() / CONFIG_SUBDIR
    if src.is_dir():
        for default in sorted(src.glob("*.toml")):
            target = conf / default.name
            if not target.exists():
                shutil.copyfile(default, target)
    return conf
