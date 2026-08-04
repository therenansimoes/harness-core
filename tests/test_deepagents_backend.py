import builtins
import json
import os
from pathlib import Path

import pytest

from harness.backends import deepagents_backend as da
from harness.types import ExecRequest

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_fix"


@pytest.fixture
def no_deepagents(monkeypatch):
    """Simula o extra desinstalado — o teste não pode depender do ambiente."""
    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name == "deepagents" or name.startswith("deepagents."):
            raise ImportError("No module named 'deepagents'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_preflight_without_lib_is_not_ok(no_deepagents):
    pre = da.DeepagentsBackend().preflight()
    assert pre.ok is False
    assert "harness-core[deepagents]" in pre.reason


def test_execute_without_lib_is_blocked(no_deepagents, tmp_path):
    req = ExecRequest(prompt="x", workspace=tmp_path, trace_path=tmp_path / "trace.jsonl")
    res = da.DeepagentsBackend().execute(req)
    assert (res.ok, res.exit_reason, res.files_changed) == (False, "blocked", ())
    assert "deepagents" in json.loads(res.trace_path.read_text())["error"]


def test_preflight_reports_lmstudio_down(monkeypatch):
    """Porta morta: o servidor não responde e a mensagem diz o que fazer."""
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1/v1")
    pre = da.DeepagentsBackend(model="openai:qwen3.5-9b-mlx").preflight()
    assert pre.ok is False
    assert "LM Studio não respondeu" in pre.reason
    assert "lms server start" in pre.reason


def test_preflight_reports_model_not_served(monkeypatch):
    """Servidor vivo, modelo ausente da lista: bloqueia dizendo qual carregar."""
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)
    monkeypatch.setattr(da, "_lmstudio_models", lambda url: {"outro-mlx"})
    pre = da.DeepagentsBackend(model="openai:qwen3.5-9b-mlx").preflight()
    assert pre.ok is False
    assert "não está baixado/servido" in pre.reason
    assert "outro-mlx" in pre.reason


def test_preflight_ok_when_model_is_served(monkeypatch):
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)
    monkeypatch.setattr(da, "_lmstudio_models", lambda url: {"qwen3.5-9b-mlx"})
    pre = da.DeepagentsBackend(model="openai:qwen3.5-9b-mlx").preflight()
    assert pre.ok is True, pre.reason


def test_bootstrap_aponta_o_cliente_openai_pro_lmstudio(monkeypatch):
    """Preflight sonda loopback; sem estes defaults o chat ia pra nuvem e o run
    morria em 401 DEPOIS de o preflight passar."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    da._bootstrap_env()
    assert os.environ["OPENAI_BASE_URL"] == da.LMSTUDIO_BASE_URL
    assert os.environ["OPENAI_API_KEY"]


def test_bootstrap_nao_sobrescreve_env_explicito(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-de-verdade")
    da._bootstrap_env()
    assert os.environ["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert os.environ["OPENAI_API_KEY"] == "sk-de-verdade"


def test_lmstudio_base_url_vem_do_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9999/v1/")
    assert da._lmstudio_base_url() == "http://127.0.0.1:9999/v1"
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert da._lmstudio_base_url() == da.LMSTUDIO_BASE_URL


def test_capabilities_are_declared():
    caps = da.DeepagentsBackend().capabilities()
    assert caps.model_selectable is True
    assert "edit_file" in caps.tools
    # `execute` só é honesto porque o backend é sandbox (test_backend_suporta_execucao)
    assert "execute" in caps.tools


# --- pricing ---------------------------------------------------------------


def test_pricing_file_has_only_free_local_models():
    pricing = da.load_pricing(Path("config"))
    assert pricing, "config/models.toml sem [pricing]"
    # locais grátis: só os modelos MLX servidos pelo LM Studio em loopback
    local_openai = {
        "openai:qwen3.5-9b-optiq",
        "openai:qwen3.5-9b-mlx",
        "openai:bonsai-27b-mlx",
        "openai:google/gemma-4-e4b",
    }
    assert all(k in local_openai for k in pricing)
    assert all(
        v.get("input_per_mtok") == 0.0 and v.get("output_per_mtok") == 0.0
        for v in pricing.values()
    )


def test_cost_off_table_local_is_none():
    """Runtime local não ganha mais desconto implícito: fora da tabela = None."""
    assert da.cost_usd("openai:qwen3.5-9b-mlx", 1000, 1000, pricing={}) is None


def test_cost_unknown_model_is_none():
    assert da.cost_usd("gpt-mistério", 1000, 1000, pricing={}) is None
    assert da.cost_usd(None, 0, 0, pricing={}) is None


def test_cost_uses_table_per_mtok():
    pricing = {"acme:big": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}
    assert da.cost_usd("acme:big", 1_000_000, 2_000_000, pricing=pricing) == 33.0


# --- files_changed ---------------------------------------------------------


def test_snapshot_diff_detects_write_edit_and_delete(tmp_path):
    (tmp_path / "sub").mkdir()
    keep = tmp_path / "keep.txt"
    keep.write_text("k")
    gone = tmp_path / "sub" / "gone.txt"
    gone.write_text("g")
    before = da._snapshot(tmp_path)

    gone.unlink()
    (tmp_path / "novo.txt").write_text("n")
    keep.write_text("keep mudou")

    assert da._diff(before, da._snapshot(tmp_path)) == ("keep.txt", "novo.txt", "sub/gone.txt")


def test_snapshot_ignores_trace_file(tmp_path):
    trace = tmp_path / "trace.jsonl"
    before = da._snapshot(tmp_path, exclude=trace)
    trace.write_text("{}\n")
    assert da._diff(before, da._snapshot(tmp_path, exclude=trace)) == ()


def test_timeout_helper_gives_up(tmp_path):
    import time

    with pytest.raises(TimeoutError):
        da._with_timeout(lambda: time.sleep(5), 0.05)
    assert da._with_timeout(lambda: 42, 5) == 42


# --- allowlist de tools (precisa da lib, não da rede) ----------------------


def _agent_tool_names(agent) -> set[str]:
    return set(agent.nodes["tools"].bound.tools_by_name)


def test_allowlist_intersects_only_filesystem_tools():
    pytest.importorskip("deepagents")
    assert da._fs_allowlist(("ls", "read_file", "note_write")) == ["ls", "read_file"]
    assert da._fs_allowlist(("note_write",)) == []
    assert da._fs_allowlist(()) == []


def test_middleware_replacement_actually_restricts_tools(tmp_path, monkeypatch):
    """Pendência 1 do RESEARCH: `FilesystemMiddleware` via `middleware=` substitui."""
    pytest.importorskip("deepagents")
    # langchain-openai exige chave até para endpoint local; o LM Studio ignora o
    # valor. Nenhuma chamada de rede acontece aqui — só a montagem do grafo.
    monkeypatch.setenv("OPENAI_API_KEY", "lm-studio")
    base = ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx", max_turns=3)

    default, _ = da._build_agent(base)
    assert {"write_file", "execute", "delete"} <= _agent_tool_names(default)

    from dataclasses import replace

    restrito, _ = da._build_agent(replace(base, tools=("ls", "read_file", "edit_file")))
    assert "execute" not in _agent_tool_names(restrito)
    assert {"ls", "read_file", "edit_file"} <= _agent_tool_names(restrito)


def _middleware_names(tmp_path, monkeypatch) -> list[str]:
    """Nomes das classes do stack de middleware que chega ao create_deep_agent."""
    import deepagents

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado["middleware"] = kw["middleware"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    return [type(m).__name__ for m in capturado["middleware"]]


def test_compactacao_e_retry_de_tool_estao_no_stack(tmp_path, monkeypatch):
    """Sem eles o run longo do 9B estoura o ctx e erro transitório de tool
    queima turno do agente."""
    pytest.importorskip("deepagents")
    nomes = _middleware_names(tmp_path, monkeypatch)
    assert "ContextEditingMiddleware" in nomes
    assert "ToolRetryMiddleware" in nomes


def test_compactacao_limpa_tool_result_velho_acima_do_gatilho():
    """Comportamento, não presença: o edit é determinístico e roda offline."""
    pytest.importorskip("deepagents")
    from langchain.agents.middleware import ClearToolUsesEdit
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.messages.utils import count_tokens_approximately

    def par(i: int, nome: str, corpo: str):
        call = {"name": nome, "args": {"path": f"/f{i}.py"}, "id": f"c{i}"}
        return [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=corpo, tool_call_id=f"c{i}", name=nome),
        ]

    gordo = "x " * 12000  # ~6k tokens por tool result: 6 pares passam do gatilho
    messages = [HumanMessage(content="tarefa")]
    for i in range(3):
        messages += par(i, "read_file", gordo)
    messages += par(3, "write_file", gordo)  # excluído do clear
    messages += par(4, "read_file", gordo)
    messages += par(5, "read_file", gordo)
    assert count_tokens_approximately(messages) > da.CONTEXT_TRIGGER_TOKENS

    edit = ClearToolUsesEdit(
        trigger=da.CONTEXT_TRIGGER_TOKENS,
        clear_at_least=0,
        keep=da.CONTEXT_KEEP_TOOL_RESULTS,
        exclude_tools=("write_file", "edit_file"),
    )
    edit.apply(messages, count_tokens=count_tokens_approximately)

    corpos = [(m.name, m.content) for m in messages if isinstance(m, ToolMessage)]
    assert corpos[0] == ("read_file", "[cleared]")   # os velhos saem
    assert corpos[1] == ("read_file", "[cleared]")
    assert corpos[2] == ("read_file", "[cleared]")
    assert corpos[3] == ("write_file", gordo)        # exclude_tools respeitado
    assert corpos[-1] == ("read_file", gordo)        # keep=2 preserva os recentes
    assert corpos[-2] == ("read_file", gordo)
    # `clear_at_least=0` limpa tudo de uma vez e o contexto volta pra BAIXO do
    # gatilho. Com cota parcial ele pararia na primeira limpeza e o turno
    # seguinte seria cortado do mesmo jeito.
    assert count_tokens_approximately(messages) < da.CONTEXT_TRIGGER_TOKENS


def test_compactacao_nao_toca_conversa_curta():
    pytest.importorskip("deepagents")
    from langchain.agents.middleware import ClearToolUsesEdit
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.messages.utils import count_tokens_approximately

    messages = [
        HumanMessage(content="tarefa"),
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "c0"}]),
        ToolMessage(content="print(1)", tool_call_id="c0", name="read_file"),
    ]
    ClearToolUsesEdit(trigger=da.CONTEXT_TRIGGER_TOKENS).apply(
        messages, count_tokens=count_tokens_approximately
    )
    assert messages[-1].content == "print(1)"


def test_backend_suporta_execucao(tmp_path, monkeypatch):
    """`execute` só chega ao modelo se o backend passa em `supports_execution`.

    Com `FilesystemBackend` a tool existe no grafo mas o `FilesystemMiddleware`
    a remove em request-time — o agente nunca rodava o verify_cmd."""
    pytest.importorskip("deepagents")
    import deepagents
    from deepagents.middleware.filesystem import supports_execution

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado["backend"] = kw["backend"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    assert supports_execution(capturado["backend"]) is True


def test_kind_no_request_injeta_skill_no_system_prompt(tmp_path, monkeypatch):
    """Sem `kind` no ExecRequest a skill de `kinds = ["code"]` nunca é injetada."""
    pytest.importorskip("deepagents")
    import deepagents

    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "fake.md").write_text(
        '---\nname = "fake-code-skill"\nkinds = ["code"]\n'
        'description = "fixture"\n---\nMARCADOR-DA-SKILL\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

    capturado: dict[str, str] = {}

    def spy(*a, **kw):
        capturado["system_prompt"] = kw["system_prompt"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)

    ws = tmp_path / "ws"
    base = ExecRequest(prompt="x", workspace=ws, model="openai:qwen3.5-9b-mlx", kind="code")
    da._build_agent(base)
    # O system prompt fica com o índice; o corpo é dado e viaja no bloco não
    # confiável (fronteira de confiança, harness/trust_boundary.py).
    assert "fake-code-skill" in capturado["system_prompt"]
    assert "MARCADOR-DA-SKILL" not in capturado["system_prompt"]
    assert "MARCADOR-DA-SKILL" in (da._untrusted_block(base) or "")

    from dataclasses import replace

    sem_kind = replace(base, kind=None)
    da._build_agent(sem_kind)
    assert "fake-code-skill" not in capturado["system_prompt"]
    assert "MARCADOR-DA-SKILL" not in (da._untrusted_block(sem_kind) or "")


def test_system_prompt_separa_shell_de_filesystem_virtual(tmp_path, monkeypatch):
    """Diagnóstico do u4a: o modelo rodava `ls /dist` no shell real (raiz da
    máquina), recebia "não existe" e encerrava sem escrever nada; e repetia
    edit_file com a indentação dos números de linha do read_file até desistir.
    As duas pontes moram no system prompt do backend."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, str] = {}

    def spy(*a, **kw):
        capturado["system_prompt"] = kw["system_prompt"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    prompt = capturado["system_prompt"]
    assert "RELATIVO" in prompt  # `execute` é shell real com cwd no workspace
    assert "números de linha" in prompt  # read_file numera, o arquivo não
    assert "write_file" in prompt  # saída do loop de edit_file que não casa


def test_manual_das_tools_entra_no_system_prompt(tmp_path, monkeypatch):
    """Cada modelo usa tool do seu jeito: sem o manual (o que faz, assinatura,
    exemplo, pegadinha) o run "explica" a mudança e nada vira arquivo."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, str] = {}

    def spy(*a, **kw):
        capturado["system_prompt"] = kw["system_prompt"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    prompt = capturado["system_prompt"]
    assert "Manual das tools" in prompt
    for tool in ("ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "delete", "task"):
        assert f"## {tool}" in prompt


def test_papeis_de_subagent_vao_como_subagents_e_manual(tmp_path, monkeypatch):
    """Papel é dado (config/agents.toml): a spec chega em `subagents=` e o
    modelo ganha uma linha por papel no prompt — sem isso a tool `task` existe
    com o `general-purpose` default e nenhuma pista de quando usar."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado.update(kw)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    nomes = [s["name"] for s in capturado["subagents"]]
    assert nomes == ["planner", "reviewer"]
    assert all("system_prompt" in s for s in capturado["subagents"])
    assert "task(subagent_type='planner')" in capturado["system_prompt"]


def test_sem_papel_nao_passa_subagents(tmp_path, monkeypatch):
    """Fail-open: agents.toml ausente/torto => chamada idêntica à de antes."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado.update(kw)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    monkeypatch.setattr(da, "load_roles", lambda **kw: [])
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    assert "subagents" not in capturado
    # O marcador é a PRIMEIRA linha do manual de papéis: `subagent_type` sozinho
    # não serve mais de proxy — o protocolo do executor.md também nomeia a tool.
    assert "Você pode delegar micro tarefas" not in capturado["system_prompt"]


def test_manual_por_modelo_ganha_do_geral_com_fallback(tmp_path, monkeypatch):
    """Variação por modelo: prompts/tools/<provider>_<modelo>.md, senão
    prompts/tools/<provider>.md, senão o geral; nenhum => "" (fail-open)."""
    monkeypatch.setattr(da, "TOOLS_PROMPT_DIR", tmp_path / "tools")
    monkeypatch.setattr(da, "TOOLS_PROMPT_PATH", tmp_path / "tools.md")
    (tmp_path / "tools").mkdir()
    assert da._tools_prompt("openai:qwen3.5-9b-mlx") == ""
    (tmp_path / "tools.md").write_text("GERAL", encoding="utf-8")
    assert da._tools_prompt("openai:qwen3.5-9b-mlx") == "GERAL"
    (tmp_path / "tools" / "openai.md").write_text("PROVIDER", encoding="utf-8")
    assert da._tools_prompt("openai:qwen3.5-9b-mlx") == "PROVIDER"
    (tmp_path / "tools" / "openai_qwen3.5-9b-mlx.md").write_text("MODELO", encoding="utf-8")
    assert da._tools_prompt("openai:qwen3.5-9b-mlx") == "MODELO"
    assert da._tools_prompt(None) == "GERAL"


def test_shell_do_agente_e_o_cercado(tmp_path, monkeypatch):
    """O backend de shell tem que ser o SafeShellBackend: `virtual_mode` não
    cobre `execute`, então sem a cerca o loop autônomo alcança a máquina."""
    pytest.importorskip("deepagents")
    import deepagents

    from harness.backends.safe_shell import SafeShellBackend

    capturado: dict[str, object] = {}

    def spy(*a, **kw):
        capturado["backend"] = kw["backend"]
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    assert isinstance(capturado["backend"], SafeShellBackend)


def test_model_instance_tem_temperature_baixa():
    """Fix do thinking/temperature: instância, não string crua (com fail-open)."""
    pytest.importorskip("langchain")
    for model in ("openai:qwen3.5-9b-mlx", "anthropic:claude-sonnet-4-5"):
        got = da._model_for(model)
        if isinstance(got, str):
            continue  # provider sem credencial local: fallback pra string crua
        assert got.temperature == da.MODEL_TEMPERATURE
    assert da._model_for(None) is None


def test_thinking_canal_por_provider():
    """No openai:* (LM Studio) o thinking já vem ligado do servidor, e o
    `extra_body` fica só para vLLM/llama.cpp compatíveis."""
    assert da._thinking_kwargs("openai:qwen3.5-9b-mlx") == {
        "extra_body": da.THINKING_EXTRA_BODY
    }
    assert da._thinking_kwargs("anthropic:claude-sonnet-4-5") == {}


def test_model_for_passa_o_kwarg_do_provider(monkeypatch):
    """Roteamento sem rede: o kwarg certo chega no init_chat_model."""
    pytest.importorskip("langchain")
    import langchain.chat_models as lcm

    visto: list[dict] = []
    monkeypatch.setattr(
        lcm, "init_chat_model", lambda m, **kw: visto.append(kw) or f"model:{m}"
    )
    assert da._model_for("openai:qwen3.5-9b-mlx") == "model:openai:qwen3.5-9b-mlx"
    assert visto[-1] == {
        "temperature": da.MODEL_TEMPERATURE,
        "extra_body": da.THINKING_EXTRA_BODY,
    }
    da._model_for("anthropic:claude-sonnet-4-5")
    assert visto[-1] == {"temperature": da.MODEL_TEMPERATURE}


def test_model_for_cai_pra_temperature_se_provider_rejeita_kwarg(monkeypatch):
    """Provider que não conhece `reasoning` não pode derrubar o backend."""
    pytest.importorskip("langchain")
    import langchain.chat_models as lcm

    visto: list[dict] = []

    def fake(m, **kw):
        visto.append(kw)
        if "extra_body" in kw:
            raise TypeError("unexpected keyword 'extra_body'")
        return f"model:{m}"

    monkeypatch.setattr(lcm, "init_chat_model", fake)
    assert da._model_for("openai:qwen3.5-9b-mlx") == "model:openai:qwen3.5-9b-mlx"
    assert visto == [
        {"temperature": da.MODEL_TEMPERATURE, "extra_body": da.THINKING_EXTRA_BODY},
        {"temperature": da.MODEL_TEMPERATURE},
    ]


# --- stalled e trace parcial ------------------------------------------------


class FakeMsg:
    def __init__(self, type, content, tool_calls=None, response_metadata=None):
        self.type = type
        self.content = content
        self.tool_calls = tool_calls
        if response_metadata is not None:
            self.response_metadata = response_metadata


class FakeUsage:
    usage_metadata: dict = {}


def _fake_backend(monkeypatch, invoke):
    """Backend com o grafo trocado por `invoke` — sem lib, sem rede."""
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)

    class FakeAgent:
        def invoke(self, payload, config):
            return invoke(payload, config)

    monkeypatch.setattr(da, "_build_agent", lambda req: (FakeAgent(), FakeUsage()))
    return da.DeepagentsBackend()


def _req(tmp_path, **kw):
    kw.setdefault("prompt", "x")
    return ExecRequest(workspace=tmp_path, trace_path=tmp_path / "trace.jsonl", **kw)


def test_desistencia_silenciosa_vira_stalled(tmp_path, monkeypatch):
    """u4a: zero escrita e zero texto final saía como done/ok=True no ledger."""
    backend = _fake_backend(
        monkeypatch, lambda payload, config: {"messages": [FakeMsg("ai", "")]}
    )
    res = backend.execute(_req(tmp_path))
    assert (res.ok, res.exit_reason, res.files_changed) == (False, "stalled", ())


@pytest.mark.parametrize(
    "escreve, texto",
    [
        (True, ""),  # escreveu e ficou calado: entregou algo
        (False, "relatório do que encontrei"),  # não escreveu mas respondeu
    ],
)
def test_stalled_exige_as_duas_condicoes(tmp_path, monkeypatch, escreve, texto):
    def invoke(payload, config):
        if escreve:
            (tmp_path / "x.py").write_text("print(1)\n", encoding="utf-8")
        return {"messages": [FakeMsg("ai", texto)]}

    res = _fake_backend(monkeypatch, invoke).execute(_req(tmp_path))
    assert (res.ok, res.exit_reason) == (True, "done")


def test_usage_do_callback_vira_tokens_no_result(tmp_path, monkeypatch):
    """Token é primeira classe no resultado: soma o usage de todos os modelos do
    run. Custo em dólar depende da tabela de preço do dia, token não."""
    monkeypatch.setattr(
        FakeUsage,
        "usage_metadata",
        {
            "modelo-a": {"input_tokens": 900, "output_tokens": 120},
            "modelo-b": {"input_tokens": 100, "output_tokens": 30},
        },
    )

    def invoke(payload, config):
        (tmp_path / "x.py").write_text("print(1)\n", encoding="utf-8")
        return {"messages": [FakeMsg("ai", "feito")]}

    res = _fake_backend(monkeypatch, invoke).execute(_req(tmp_path))
    assert (res.tokens_in, res.tokens_out) == (1000, 150)


def test_resposta_cortada_no_teto_de_tokens_vira_truncated(tmp_path, monkeypatch):
    """`finish_reason=length` não é conclusão: braço cortado ≠ braço ruim."""

    def invoke(payload, config):
        (tmp_path / "x.py").write_text("print(1)\n", encoding="utf-8")
        return {
            "messages": [
                FakeMsg("ai", "comecei a explicar e", response_metadata={"finish_reason": "length"})
            ]
        }

    res = _fake_backend(monkeypatch, invoke).execute(_req(tmp_path))
    assert (res.ok, res.exit_reason) == (False, "truncated")
    # o run escreveu: o diff continua registrado, a régua é quem julga depois
    assert res.files_changed == ("x.py",)


def test_truncated_ganha_de_stalled_e_perde_pro_limite_de_turnos(tmp_path, monkeypatch):
    cortada = FakeMsg("ai", "", response_metadata={"finish_reason": "length"})
    res = _fake_backend(monkeypatch, lambda p, c: {"messages": [cortada]}).execute(_req(tmp_path))
    assert res.exit_reason == "truncated"  # e não "stalled"

    sentinela = FakeMsg(
        "ai",
        da.LIMIT_MESSAGE_PREFIX + " run limit",
        response_metadata={"finish_reason": "length"},
    )
    res = _fake_backend(monkeypatch, lambda p, c: {"messages": [sentinela]}).execute(_req(tmp_path))
    assert res.exit_reason == "max_turns"


@pytest.mark.parametrize(
    "meta",
    [
        None,                             # provider sem metadata: fail-open
        {},                               # metadata vazio
        {"finish_reason": "stop"},        # terminou normal
        {"finish_reason": None},          # campo presente e nulo
        {"outra_coisa": "length"},        # chave inesperada não engana
    ],
)
def test_sem_sinal_de_corte_o_comportamento_e_o_de_antes(tmp_path, monkeypatch, meta):
    msg = FakeMsg("ai", "terminei", response_metadata=meta)
    res = _fake_backend(monkeypatch, lambda p, c: {"messages": [msg]}).execute(_req(tmp_path))
    assert (res.ok, res.exit_reason) == (True, "done")


def test_stop_reason_do_anthropic_tambem_conta(tmp_path, monkeypatch):
    msg = FakeMsg("ai", "cortado", response_metadata={"stop_reason": "max_tokens"})
    res = _fake_backend(monkeypatch, lambda p, c: {"messages": [msg]}).execute(_req(tmp_path))
    assert res.exit_reason == "truncated"


def test_truncated_olha_so_a_ultima_resposta(tmp_path, monkeypatch):
    """Corte num turno do meio o modelo já contornou; o que conta é o fim."""
    messages = [
        FakeMsg("ai", "cortei aqui", response_metadata={"finish_reason": "length"}),
        FakeMsg("tool", "ok"),
        FakeMsg("ai", "agora terminei", response_metadata={"finish_reason": "stop"}),
    ]
    res = _fake_backend(monkeypatch, lambda p, c: {"messages": messages}).execute(_req(tmp_path))
    assert (res.ok, res.exit_reason) == (True, "done")


def test_timeout_materializa_trace_parcial(tmp_path, monkeypatch):
    """Timeout descartava o trace inteiro — justamente o run que precisa dele."""
    import time

    class FakeGen:
        message = FakeMsg("ai", "pensando alto")

    class FakeLLMResult:
        generations = [[FakeGen()]]

    def invoke(payload, config):
        config["callbacks"][1].on_llm_end(FakeLLMResult())
        time.sleep(5)

    res = _fake_backend(monkeypatch, invoke).execute(_req(tmp_path, timeout_s=0.2))
    assert res.exit_reason == "timeout"
    linhas = [json.loads(l) for l in res.trace_path.read_text().splitlines()]
    assert any("error" in r for r in linhas)
    assert sum(1 for r in linhas if r.get("type") == "ai") == 1


# --- LM Studio de verdade ---------------------------------------------------


@pytest.mark.lmstudio
def test_e2e_tiny_fix_with_lmstudio(tmp_path):
    from harness import cli

    model = "openai:qwen3.5-9b-mlx"
    unit = cli.load_unit(FIXTURE)
    cli.seed_workspace(unit, tmp_path)
    backend = da.DeepagentsBackend(model=model)
    pre = backend.preflight()
    if not pre.ok:
        pytest.skip(pre.reason)  # servidor local ausente não é falha de teste

    res = backend.execute(
        ExecRequest(
            prompt=unit.prompt,
            workspace=tmp_path,
            model=model,
            max_turns=8,
            timeout_s=300.0,
            trace_path=tmp_path / "trace.jsonl",
        )
    )
    assert res.cost_usd == 0.0
    assert "target.py" in res.files_changed
    assert "a + b" in (tmp_path / "target.py").read_text()
