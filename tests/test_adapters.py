"""A frota LoRA é matcher determinístico sobre metadado: nenhum teste aqui toca
rede, servidor MLX ou modelo. O que precisa ser verdade:

  - sem registro (arquivo ausente, `[[adapter]]` nenhum) a resposta é None, não
    exceção: a base sempre atende e a frota é opt-in;
  - `unit.adapter` vence o matcher, e id que não existe no registro é ERRO —
    pedir peso no dedo e receber a base em silêncio é o pior dos mundos;
  - o ranking casa `match` contra o prompt, `paths` fura a fila e score zero
    NUNCA seleciona: falso positivo aqui troca o peso do modelo;
  - config torta derruba o load, uma exceção por tipo de estrago;
  - o registro REAL do repo (config/adapters.toml) carrega — se um `ref` sumir
    do disco, é aqui que aparece.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.graph import run_graph
from harness.routing import CONFIG_DIR_ENV
from harness.routing.adapters import (
    AdapterError,
    get_adapter,
    load_adapters,
    runs_local,
    select_adapter,
)
from harness.types import Selection, UnitSpec

REPO = Path(__file__).resolve().parent.parent


def _unit(prompt: str, uid: str = "u", kind: str | None = None, adapter: str | None = None):
    return UnitSpec(
        id=uid, path=Path("."), prompt=prompt, verify_cmd="true", kind=kind, adapter=adapter
    )


def _registry(tmp_path: Path, body: str, ref: Path | None = None) -> Path:
    """Escreve um adapters.toml num config/ só do teste. `{REF}` no corpo vira um
    diretório que existe de verdade — a validação de `ref` é sobre o disco."""
    ref = ref or (tmp_path / "peso")
    ref.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    path = cfg / "adapters.toml"
    path.write_text(body.replace("{REF}", str(ref)), encoding="utf-8")
    return path


ONE = """\
[[adapter]]
id = "sql"
runtime = "mlx"
served_model = "mlx-community/Qwen3.5-4B-4bit"
ref = "{REF}"
kinds = ["code"]
match = ["sql", "sqlite", "consulta"]
paths = ["*.sql"]
temperature = 0.0
max_tokens = 128
"""


# --------------------------------------------------------------------------- vazio


def test_sem_arquivo_a_frota_e_vazia(tmp_path, monkeypatch):
    """Ausência de registro é o default de quem nunca treinou nada — não é erro."""
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "sem-config"))
    assert load_adapters() == []
    assert select_adapter(_unit("gere o SQL da consulta"), "code") is None
    assert get_adapter("sql") is None


def test_arquivo_sem_nenhum_adapter(tmp_path, monkeypatch):
    _registry(tmp_path, "# registro vazio de propósito\n")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    assert load_adapters() == []
    assert select_adapter(_unit("gere o SQL"), "code") is None


# --------------------------------------------------------------------------- seleção


def test_load_le_o_card_inteiro(tmp_path):
    a = load_adapters(_registry(tmp_path, ONE))[0]
    assert (a.id, a.runtime, a.served_model) == ("sql", "mlx", "mlx-community/Qwen3.5-4B-4bit")
    assert (a.kinds, a.paths) == (("code",), ("*.sql",))
    # Sampling do card sobrepõe o default do backend; ausente é None ("não opinar").
    assert (a.temperature, a.max_tokens, a.top_p) == (0.0, 128, None)
    assert (a.enable_thinking, a.output, a.priority) == (False, "text", 0)
    # Card que não fala de tool calling usa o do chat template do base.
    assert (a.system, a.tool_format) == ("", "native")


def test_explicito_vence_o_matcher(tmp_path):
    """Prompt sem token nenhum de SQL: quem escolhe é a unidade."""
    path = _registry(tmp_path, ONE)
    got = select_adapter(_unit("troque a cor do botão", adapter="sql"), "content", path=path)
    assert got is not None and got.id == "sql"


def test_explicito_desconhecido_e_erro(tmp_path):
    path = _registry(tmp_path, ONE)
    with pytest.raises(AdapterError, match="não está no registro"):
        select_adapter(_unit("qualquer coisa", adapter="csv"), "code", path=path)


def test_ranking_por_token_do_match(tmp_path):
    path = _registry(tmp_path, ONE)
    got = select_adapter(_unit("escreva a consulta sqlite que soma os pedidos"), "code", path=path)
    assert got is not None and got.id == "sql"


def test_prompt_sem_token_em_comum_fica_na_base(tmp_path):
    """Score zero não seleciona — trocar o peso do modelo por engano custa caro."""
    path = _registry(tmp_path, ONE)
    assert select_adapter(_unit("ajuste a margem do rodapé"), "code", path=path) is None


def test_kind_fora_da_lista_nao_casa(tmp_path):
    path = _registry(tmp_path, ONE)
    assert select_adapter(_unit("gere a consulta sqlite"), "content", path=path) is None


def test_kinds_vazio_vale_para_todo_kind(tmp_path):
    body = ONE.replace('kinds = ["code"]', "kinds = []")
    path = _registry(tmp_path, body)
    got = select_adapter(_unit("gere a consulta sqlite"), "content", path=path)
    assert got is not None and got.id == "sql"


def test_path_trigger_fura_o_ranking(tmp_path):
    """Glob de `paths` casa sem nenhum token em comum — é o eixo determinístico."""
    path = _registry(tmp_path, ONE)
    got = select_adapter(_unit("faça o que o arquivo pede"), "code", ["db/report.sql"], path=path)
    assert got is not None and got.id == "sql"


def test_files_none_usa_os_paths_citados_no_prompt(tmp_path):
    path = _registry(tmp_path, ONE)
    got = select_adapter(_unit("aplique o que está em relatorio.sql"), "code", path=path)
    assert got is not None and got.id == "sql"


def test_priority_desempata_score_igual(tmp_path):
    body = (
        ONE
        + """
[[adapter]]
id = "sql-especialista"
runtime = "mlx"
served_model = "mlx-community/Qwen3.5-4B-4bit"
ref = "{REF}"
kinds = ["code"]
match = ["sql", "sqlite", "consulta"]
priority = 5
"""
    )
    path = _registry(tmp_path, body)
    got = select_adapter(_unit("escreva a consulta sqlite"), "code", path=path)
    assert got is not None and got.id == "sql-especialista"


def test_maior_score_ganha_de_maior_priority(tmp_path):
    """Priority é DESEMPATE, não atalho: quem casa mais token ganha."""
    body = (
        ONE
        + """
[[adapter]]
id = "generico"
runtime = "mlx"
served_model = "mlx-community/Qwen3.5-4B-4bit"
ref = "{REF}"
kinds = ["code"]
match = ["sql"]
priority = 9
"""
    )
    path = _registry(tmp_path, body)
    got = select_adapter(_unit("escreva a consulta sqlite do relatório"), "code", path=path)
    assert got is not None and got.id == "sql"


def test_get_adapter_resolve_o_id(tmp_path):
    path = _registry(tmp_path, ONE)
    assert get_adapter("sql", path).ref.endswith("peso")
    # Id fora do registro não derruba o backend: base pura.
    assert get_adapter("csv", path) is None
    assert get_adapter(None, path) is None


def test_runs_local_so_aceita_o_runtime_da_maquina():
    assert runs_local("deepagents", "openai:qwen3.5-9b-mlx") is True
    # Escalação pra nuvem não tem onde aplicar peso local.
    assert runs_local("claude_code", "haiku") is False
    assert runs_local("deepagents", "anthropic:claude-sonnet-4-5") is False
    assert runs_local("deepagents", None) is False


# --------------------------------------------------------------------------- no grafo


def _sel(backend: str = "deepagents", model: str = "openai:qwen3.5-9b-mlx"):
    return Selection(backend=backend, model=model, tier="t0", kind="code", max_turns=3)


def test_route_escolhe_o_adapter_na_tentativa_zero(tmp_path, monkeypatch):
    _registry(tmp_path, ONE)
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    state = {"unit": _unit("escreva a consulta sqlite do relatório")}
    assert run_graph._adapter(state, _sel(), attempt=0) == "sql"


def test_adapter_congela_entre_tentativas(tmp_path, monkeypatch):
    """A tentativa 1 NÃO reescolhe: o retry muda o tier, não o peso — senão o
    ledger compara duas coisas diferentes com o mesmo rótulo."""
    _registry(tmp_path, ONE)
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    state = {"unit": _unit("ajuste a margem do rodapé"), "adapter": "sql"}
    assert run_graph._adapter(state, _sel(), attempt=1) == "sql"
    # Sem carimbo no estado (checkpoint antigo, ou tentativa 0 sem match): base.
    assert run_graph._adapter({"unit": state["unit"]}, _sel(), attempt=1) is None


def test_escalada_pra_nuvem_zera_o_adapter(tmp_path, monkeypatch):
    _registry(tmp_path, ONE)
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    state = {"unit": _unit("escreva a consulta sqlite"), "adapter": "sql"}
    assert run_graph._adapter(state, _sel("claude_code", "haiku"), attempt=1) is None
    # E o registro nem é lido quando o executor não é local: config apontando
    # para um diretório vazio continua passando.
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "sem-config"))
    assert run_graph._adapter(state, _sel("mock", ""), attempt=0) is None


# --------------------------------------------------------------------------- config torta


@pytest.mark.parametrize(
    ("body", "erro"),
    [
        (ONE + ONE, "duplicado"),
        (ONE.replace('id = "sql"', 'id = ""'), "sem id"),
        (ONE.replace('runtime = "mlx"', 'runtime = "vllm"'), "runtime"),
        (
            ONE.replace('served_model = "mlx-community/Qwen3.5-4B-4bit"', 'served_model = ""'),
            "served_model",
        ),
        (ONE.replace('ref = "{REF}"', 'ref = "/nao/existe/no/disco"'), "não é um diretório"),
        (ONE.replace('ref = "{REF}"', 'ref = ""'), "ref vazio"),
        (ONE.replace('kinds = ["code"]', 'kinds = ["sql"]'), "não são Kind"),
        (ONE + 'output = "parquet"\n', "output"),
        (ONE + 'tool_format = "xlam"\n', "tool_format"),
        # Vocabulário válido, backend ainda sem suporte: erro DIFERENTE de typo.
        (ONE + 'tool_format = "hermes"\n', "aguardando suporte no backend"),
        (ONE.replace("temperature = 0.0", 'temperature = "quente"'), "temperature inválido"),
        ("[[adapter]\nid = 'x'\n", "inválido"),
    ],
)
def test_config_torta_derruba_o_load(tmp_path, body, erro):
    """Registro com typo virando "sem adapter" em silêncio esconde o bug e roda
    com o peso errado — mesma doutrina de `router.load_config`."""
    path = _registry(tmp_path, body)
    with pytest.raises(AdapterError, match=erro):
        load_adapters(path)


# --------------------------------------------------------------------------- registro real


def test_registro_do_repo_carrega():
    frota = load_adapters(REPO / "config" / "adapters.toml")
    sql = next(a for a in frota if a.id == "sql")
    assert sql.runtime == "mlx"
    assert Path(sql.ref).is_dir()
    assert (sql.temperature, sql.max_tokens, sql.enable_thinking) == (0.0, 128, False)
