"""load_roles: papel é dado do genoma; todo caminho torto vira [] ou papel fora."""

import pytest

from harness.backends.agent_roles import load_roles, roles_manual

DOIS_PAPEIS = """\
[agents.a]
description = "faz a"
prompt = "prompt a"
tools = ["ls", "read_file"]

[agents.b]
enabled = false
description = "faz b"
prompt = "prompt b"
tools = ["execute"]
"""


def _write(tmp_path, text, name="agents.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _fake_model():
    """Modelo de mentira só para a lib compilar o subagent aninhado."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    return GenericFakeChatModel(messages=iter([]))


def test_toml_real_do_repo_tem_os_dois_papeis():
    """O arquivo versionado tem que carregar no formato que a lib espera.

    Sem `model` o conductor não consegue montar a `task` dele e sai — o que
    sobra é exatamente o que existia antes dele."""
    roles = load_roles("config/agents.toml")
    assert [r["name"] for r in roles] == ["planner", "reviewer"]
    for r in roles:
        assert set(r) >= {"name", "description", "system_prompt"}
        assert r["description"] and r["system_prompt"].strip()
        # `prompt` é o nome no toml; a lib só entende `system_prompt`.
        assert "prompt" not in r


def test_arquivo_ausente_e_toml_torto_viram_lista_vazia(tmp_path):
    assert load_roles(tmp_path / "nao-existe.toml") == []
    assert load_roles(_write(tmp_path, "[agents.a\nisso não é toml")) == []
    assert load_roles(_write(tmp_path, "sem_secao_agents = 1")) == []


def test_papel_sem_campo_obrigatorio_sai(tmp_path):
    path = _write(
        tmp_path,
        '[agents.a]\ndescription = "só isso"\n[agents.b]\nprompt = "só isso"\n',
    )
    assert load_roles(path) == []


def test_enabled_false_exclui_papel(tmp_path):
    roles = load_roles(_write(tmp_path, DOIS_PAPEIS))
    assert [r["name"] for r in roles] == ["a"]


def test_allowlist_do_run_filtra_e_derruba_papel(tmp_path):
    """Papel que sobra sem tool sai: subagent com mais permissão que o agente
    principal seria escalada de privilégio."""
    path = _write(
        tmp_path,
        '[agents.leitor]\ndescription = "lê"\nprompt = "p"\n'
        'tools = ["ls", "read_file", "execute"]\n'
        '[agents.shell]\ndescription = "roda"\nprompt = "p"\ntools = ["execute"]\n',
    )
    roles = load_roles(path, allowed=("ls", "read_file"))
    assert [r["name"] for r in roles] == ["leitor"]
    assert "ls, read_file" in roles[0]["system_prompt"]
    assert "execute" not in roles[0]["system_prompt"]


def test_backend_gera_middleware_que_restringe_as_tools(tmp_path):
    """Com backend, a restrição é um FilesystemMiddleware próprio do papel —
    `tools=` de SubAgent é aditivo e só aceita objeto de tool, não nome."""
    pytest.importorskip("deepagents")
    from deepagents.backends.local_shell import LocalShellBackend

    fs = LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    roles = load_roles("config/agents.toml", backend=fs)
    planner = next(r for r in roles if r["name"] == "planner")
    (mw,) = planner["middleware"]
    nomes = {t.name for t in mw.tools}
    assert "write_file" not in nomes and "execute" not in nomes
    assert {"ls", "read_file", "glob", "grep"} <= nomes


def test_sem_backend_nao_tem_middleware(tmp_path):
    roles = load_roles("config/agents.toml")
    assert all("middleware" not in r for r in roles)


def test_model_por_papel_entra_na_spec_e_inherit_nao(tmp_path):
    """`model` é opcional: só o papel que pediu outro modelo ganha a chave."""
    p = _write(
        tmp_path,
        """
[agents.custom]
description = "faz custom"
prompt = "prompt custom"
model = "openai:qwen3-coder"

[agents.herdeiro]
description = "herda"
prompt = "prompt herdeiro"
model = "inherit"

[agents.omisso]
description = "sem campo"
prompt = "prompt omisso"
""",
    )
    por_nome = {r["name"]: r for r in load_roles(p)}
    assert por_nome["custom"]["model"] == "openai:qwen3-coder"
    assert "model" not in por_nome["herdeiro"]
    assert "model" not in por_nome["omisso"]


def test_toml_real_do_repo_herda_o_modelo_do_run():
    """Um modelo por vez na máquina: papel versionado não aponta outro peso."""
    assert all("model" not in r for r in load_roles("config/agents.toml"))


def test_conductor_entra_na_spec_do_principal(tmp_path):
    """Com backend e modelo do run, o orquestrador existe para o agente principal."""
    pytest.importorskip("deepagents")
    from deepagents.backends.local_shell import LocalShellBackend

    fs = LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    roles = load_roles("config/agents.toml", backend=fs, model=_fake_model())
    assert "conductor" in [r["name"] for r in roles]
    assert "conductor" in roles_manual(roles)


def test_conductor_nao_pode_chamar_conductor(tmp_path):
    """ANTI-RECURSÃO ESTRUTURAL: a lista de subagents que o conductor recebe é
    planner/reviewer e nada mais — nem ele mesmo. E papel que não delega não
    ganha `SubAgentMiddleware`, então não há caminho de volta por outro papel."""
    pytest.importorskip("deepagents")
    from deepagents.backends.local_shell import LocalShellBackend
    from deepagents.middleware.subagents import SubAgentMiddleware

    fs = LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    roles = {
        r["name"]: r for r in load_roles("config/agents.toml", backend=fs, model=_fake_model())
    }

    (task_mw,) = [
        mw for mw in roles["conductor"]["middleware"] if isinstance(mw, SubAgentMiddleware)
    ]
    assert task_mw.subagent_names == frozenset({"planner", "reviewer"})
    assert "conductor" not in task_mw.subagent_names

    for name in ("planner", "reviewer"):
        assert not [
            mw for mw in roles[name].get("middleware", []) if isinstance(mw, SubAgentMiddleware)
        ]


def test_conductor_enabled_false_sai(tmp_path):
    roles = load_roles(
        _write(
            tmp_path,
            '[agents.planner]\ndescription = "p"\nprompt = "p"\ntools = ["ls"]\n'
            "[agents.conductor]\nenabled = false\n"
            'description = "c"\nprompt = "c"\ntools = ["ls"]\n'
            'delegates_to = ["planner"]\n',
        ),
        model=_fake_model(),
    )
    assert [r["name"] for r in roles] == ["planner"]


def test_delegates_to_sem_modelo_derruba_o_delegador(tmp_path):
    """Sem modelo não há subagent aninhado: papel que prometeu delegar e não
    delega sai, e os alvos dele continuam de pé."""
    roles = load_roles(
        _write(
            tmp_path,
            '[agents.planner]\ndescription = "p"\nprompt = "p"\ntools = ["ls"]\n'
            '[agents.chefe]\ndescription = "c"\nprompt = "c"\ntools = ["ls"]\n'
            'delegates_to = ["planner"]\n',
        )
    )
    assert [r["name"] for r in roles] == ["planner"]


def test_manual_uma_linha_por_papel():
    roles = load_roles("config/agents.toml")
    manual = roles_manual(roles)
    assert "task(subagent_type='planner')" in manual
    assert "task(subagent_type='reviewer')" in manual
    assert roles_manual([]) == ""
