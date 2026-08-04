"""Isolamento de ambiente da suíte inteira.

Existe por um acidente concreto: com `harness.paths`, qualquer teste que rode de
um cwd sem `config/` resolve config e dado para `~/.harness` — e a suíte passou
a criar (e povoar) o `~/.harness` REAL da máquina de quem roda `pytest`. Teste
não escreve no home de ninguém.
"""

from __future__ import annotations

import os

import pytest

from harness import paths

# Envs que decidem onde o harness lê e escreve. Nenhuma delas pode sobreviver a
# um teste: `harness do` chama `os.environ.setdefault` nas duas últimas (ver
# `do.pin_home_paths`), e monkeypatch não desfaz o que o código sob teste setou.
PATH_ENVS = (paths.HOME_ENV, paths.CONFIG_DIR_ENV, paths.DATA_DIR_ENV)


@pytest.fixture(autouse=True)
def harness_home(tmp_path_factory, monkeypatch):
    """`$HARNESS_HOME` num tmpdir por teste, e as envs de path restauradas no fim.

    Autouse e por teste (não por sessão): o custo de um `mktemp` é irrelevante
    perto de um teste vazar registro de projeto para o seguinte. Quem precisa de
    outro home continua fazendo `monkeypatch.setenv`/`setattr` por cima — este
    fixture roda ANTES (autouse vem primeiro no mesmo escopo).
    """
    antes = {env: os.environ.get(env) for env in PATH_ENVS}
    home = tmp_path_factory.mktemp("harness-home")
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    yield home
    for env, valor in antes.items():
        if valor is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = valor
