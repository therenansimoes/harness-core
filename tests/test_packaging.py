"""Guarda do force-include da wheel: `config/` é enumerado arquivo a arquivo.

Enumerar em vez de mandar o diretório inteiro é o que mantém
`config/projects.toml` — registro LOCAL, com paths absolutos da máquina do dono
do checkout — fora do pacote, já que `exclude` não filtra force-include no
hatchling. O preço é esquecer de listar config nova; estes testes cobram a
lista contra o que o git realmente rastreia, então o esquecimento fica vermelho
no mesmo commit que cria o arquivo.
"""

import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# O arquivo que NÃO pode viajar: local por definição, inútil (ou nocivo) na wheel.
LOCAL_ONLY = "config/projects.toml"


def _force_include() -> dict[str, str]:
    """Mapa origem→destino do force-include do target wheel."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def _tracked_config() -> list[str]:
    """Arquivos rastreados sob `config/`, direto do git — nunca uma lista fixa."""
    proc = subprocess.run(
        ["git", "ls-files", "config/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files indisponível: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line]


def _cobre(entrada: str, caminho: str) -> bool:
    """Entrada de force-include pega o caminho — como arquivo ou como diretório."""
    return caminho == entrada or caminho.startswith(entrada + "/")


def test_toda_config_rastreada_esta_no_force_include():
    entradas = _force_include()
    faltando = [
        caminho
        for caminho in _tracked_config()
        if caminho != LOCAL_ONLY and not any(_cobre(e, caminho) for e in entradas)
    ]

    assert not faltando, (
        f"config rastreada fora da wheel — adicione ao force-include do pyproject: {faltando}"
    )


def test_nenhuma_entrada_carrega_o_registro_local():
    pegando = [e for e in _force_include() if _cobre(e, LOCAL_ONLY)]

    # `"config"` sozinho é o caso que fez a pendência existir: leva o diretório
    # inteiro do disco, rastreado ou não.
    assert not pegando, f"{LOCAL_ONLY} voltaria para a wheel via {pegando}"
