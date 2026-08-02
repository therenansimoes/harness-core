"""Kind é rótulo, não preço — e é determinístico ou não é nada.

O cfg dos testes de classificação é o REAL (config/kinds.toml): se alguém
calibrar as palavras e um arquétipo trocar de kind, o teste tem que gritar.
Os testes de load quebram uma coisa de cada vez em cima de um TOML mínimo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.routing import kinds
from harness.types import UnitSpec

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return kinds.load_kinds(REPO / "config" / "kinds.toml")


def _unit(prompt: str, kind: str | None = None) -> UnitSpec:
    return UnitSpec(id="u", path=Path("."), prompt=prompt, verify_cmd="true", kind=kind)


BASE_TOML = """
[weights]
keyword = 2
extension = 1
filename = 1

[precedence]
order = ["config", "content", "code"]
fallback = "code"

[kind.config]
extensions = [".toml"]
keywords = ["config"]

[kind.content]
extensions = [".md"]
keywords = ["texto"]

[kind.code]
extensions = [".py"]
keywords = ["bug"]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "kinds.toml"
    p.write_text(text, encoding="utf-8")
    return p


# -------------------------------------------------------------------- load


def test_load_rejeita_kind_inexistente(tmp_path):
    bad = BASE_TOML.replace("[kind.code]", "[kind.poesia]").replace(
        'order = ["config", "content", "code"]', 'order = ["config", "content", "poesia"]'
    )
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_rejeita_regra_desconhecida(tmp_path):
    bad = BASE_TOML.replace('keywords = ["bug"]', 'palavras = ["bug"]')
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_rejeita_extensao_sem_ponto(tmp_path):
    bad = BASE_TOML.replace('extensions = [".py"]', 'extensions = ["py"]')
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_rejeita_order_incompleta(tmp_path):
    bad = BASE_TOML.replace('order = ["config", "content", "code"]', 'order = ["config", "code"]')
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_rejeita_fallback_fora(tmp_path):
    bad = BASE_TOML.replace('fallback = "code"', 'fallback = "infra"')
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_rejeita_peso_desconhecido(tmp_path):
    bad = BASE_TOML.replace("keyword = 2", "keyword = 2\nmagia = 9")
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(_write(tmp_path, bad))


def test_load_arquivo_ausente(tmp_path):
    with pytest.raises(kinds.KindError):
        kinds.load_kinds(tmp_path / "nao_existe.toml")


# ------------------------------------------------------------ classificação


EXEMPLOS = [
    ("code", "implementar a função de parse do CSV em harness/parse.py"),
    ("content", "reescrever o texto da seção de contato em src/pages/contato.astro"),
    ("config", "habilitar a flag de cache em app.yaml"),
    ("refactor", "refatorar o schema de conteúdo do site em src/content/config.ts"),
    ("infra", "Dockerfile: subir o container do site com nginx"),
]


@pytest.mark.parametrize("esperado,prompt", EXEMPLOS)
def test_classifica_cada_kind(cfg, esperado, prompt):
    kind, reasons = kinds.classify_kind(_unit(prompt), cfg)
    assert kind == esperado, reasons
    assert reasons[-1].startswith(f"kind:{esperado}:"), reasons


def test_fallback_sem_sinal(cfg):
    kind, reasons = kinds.classify_kind(_unit("atualizar os dados da planilha do orçamento"), cfg)
    assert kind == "code"
    assert reasons == ["fallback:code"]


def test_extensao_sozinha_basta(cfg):
    kind, reasons = kinds.classify_kind(_unit("criar README.md com as instruções de uso"), cfg)
    assert kind == "content", reasons
    assert "content:ext:.md" in reasons


def test_kind_explicito_vence_o_classificador(cfg):
    unit = _unit("reescrever o texto do README.md", kind="infra")
    assert kinds.classify_kind(unit, cfg) == ("infra", ["explicit:infra"])


def test_kind_explicito_invalido_falha(cfg):
    with pytest.raises(kinds.KindError):
        kinds.classify_kind(_unit("qualquer coisa", kind="poesia"), cfg)


def test_keyword_pesa_mais_que_extensao(cfg):
    # .astro é content, mas "refatorar" é a ação que manda.
    kind, reasons = kinds.classify_kind(_unit("refatorar o Base.astro"), cfg)
    assert kind == "refactor", reasons


def test_keyword_casa_no_inicio_da_palavra(cfg):
    # "contexto" não pode virar content por causa de "texto".
    kind, reasons = kinds.classify_kind(_unit("passar mais contexto pro agente"), cfg)
    assert kind == "code", reasons
    assert reasons == ["fallback:code"]


def test_empate_resolve_pela_precedencia(tmp_path):
    cfg = kinds.load_kinds(_write(tmp_path, BASE_TOML))
    kind, reasons = kinds.classify_kind(_unit("mexer em a.toml e b.md"), cfg)
    assert kind == "config", reasons  # config vem antes de content em [precedence]
    assert "tiebreak:precedence:config" in reasons


def test_reasons_listam_cada_sinal(cfg):
    kind, reasons = kinds.classify_kind(_unit("corrigir o bug de índice em src/list.ts"), cfg)
    assert kind == "code"
    assert "code:kw:bug" in reasons
    assert "code:ext:.ts" in reasons
