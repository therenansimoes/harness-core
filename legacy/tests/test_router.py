#!/usr/bin/env python3
"""Testa router.py (D7) — a escolha de modelo é determinística ou não é nada.

Nenhum teste aqui chama backend, rede ou LLM: o router é aritmética sobre
metadado. O que precisa ser verdade: config inválida NÃO vira default
silencioso, os três arquétipos de task caem nos três tiers, o prior histórico
só opina com amostra e nunca sobe e desce ao mesmo tempo, e cada falha escala
exatamente um rank até o teto.

    python3 -m pytest tests/test_router.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import router  # noqa: E402

# ---------------------------------------------------------------- fixtures

# TOML mínimo com a MESMA forma do models.toml real — os testes de validação
# quebram uma coisa de cada vez em cima dele.
BASE_TOML = """
version = 1
default_tier = "sonnet"
max_attempts = 3
min_n = 6
prior_floor = 0.50
prior_ceiling = 0.80

[[tier]]
name = "haiku"
rank = 0
model = "m-haiku"
max_turns = 12
est_cost_per_run = 0.05

[[tier]]
name = "sonnet"
rank = 1
model = "m-sonnet"
max_turns = 30
est_cost_per_run = 0.45

[[tier]]
name = "opus"
rank = 2
model = "m-opus"
max_turns = 40
est_cost_per_run = 2.00

[thresholds]
haiku = 2
sonnet = 6

[[signal]]
name = "prompt_long"
kind = "prompt_chars_gt"
value = 1500
weight = 2
"""


@pytest.fixture()
def cfg():
    """A régua REAL — se models.toml mudar e os arquétipos saírem do tier, o
    teste tem que gritar; é para isso que ele existe."""
    return router.load_models()


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "models.toml"
    p.write_text(text)
    return p


FILLER = "conteudo do bloco com informacao util para o agente. "


def _prompt(chars: int, files: list[str] = (), head: str = "") -> str:
    body = head + " " + " ".join(files) + " "
    return body + FILLER * ((max(0, chars - len(body)) // len(FILLER)) + 1)


# ------------------------------------------------------------------- load


def test_load_models_valida_ranks_contiguos(tmp_path):
    furo = BASE_TOML.replace('name = "opus"\nrank = 2', 'name = "opus"\nrank = 3')
    with pytest.raises(router.RouterError):
        router.load_models(_write(tmp_path, furo))
    dup = BASE_TOML.replace('name = "opus"\nrank = 2', 'name = "opus"\nrank = 1')
    with pytest.raises(router.RouterError):
        router.load_models(_write(tmp_path, dup))


def test_load_models_rejeita_kind_desconhecido(tmp_path):
    bad = BASE_TOML.replace('kind = "prompt_chars_gt"', 'kind = "magia"')
    with pytest.raises(router.RouterError):
        router.load_models(_write(tmp_path, bad))


def test_load_models_rejeita_threshold_no_topo(tmp_path):
    bad = BASE_TOML.replace("[thresholds]\nhaiku = 2", "[thresholds]\nopus = 9\nhaiku = 2")
    with pytest.raises(router.RouterError):
        router.load_models(_write(tmp_path, bad))


def test_load_models_arquivo_ausente(tmp_path):
    with pytest.raises(router.RouterError):
        router.load_models(tmp_path / "nao_existe.toml")


# ------------------------------------------------------------------ score


def test_score_task_mecanico_vai_haiku(cfg):
    prompt = _prompt(300, ["src/pages/index.astro"], "ajuste o alt da imagem")
    sel = router.select(prompt, "", "", cfg=cfg)
    assert sel.tier.name == "haiku", sel.reasons
    assert sel.score <= 2, sel.reasons


def test_score_task_medio_vai_sonnet(cfg):
    prompt = _prompt(2000, ["Header.astro", "Footer.astro", "Base.astro"])
    sel = router.select(prompt, "", "", cfg=cfg)
    assert sel.tier.name == "sonnet", sel.reasons
    assert 3 <= sel.score <= 6, sel.reasons


def test_score_task_arquitetural_vai_opus(cfg):
    files = ["a.astro", "b.astro", "c.astro", "d.astro", "e.astro"]
    prompt = _prompt(3500, files, "refatorar o schema de conteudo do site")
    sel = router.select(prompt, "", "", cfg=cfg)
    assert sel.tier.name == "opus", sel.reasons


def test_reasons_explicam_score(cfg):
    prompt = _prompt(3500, ["a.astro", "b.astro", "c.astro"], "refatorar o schema")
    feats = router.task_features(prompt, "", "")
    sc, reasons = router.score_task(feats, cfg)
    assert reasons
    assert sum(int(re.search(r"([+-]\d+)$", r).group(1)) for r in reasons) == sc


# ------------------------------------------------------------------ prior


def _rows(n: int, task_class: str, tier: str, success: str) -> list[dict]:
    return [{"notes": f"tier:{tier}; class:{task_class}", "success": success} for _ in range(n)]


MECANICO = _prompt(300, ["index.astro"], "ajuste o alt da imagem")
MEDIO = _prompt(2000, ["Header.astro", "Footer.astro", "Base.astro"])


def test_history_prior_bump(cfg):
    sel = router.select(MECANICO, "", "", rows=_rows(6, "haiku", "haiku", "0"), cfg=cfg)
    assert sel.task_class == "haiku"
    assert sel.tier.name == "sonnet"
    assert "prior_bump+1" in sel.reasons


def test_history_prior_demote(cfg):
    # 16 e não 8: Wilson lower de 8/8 é 0.68, abaixo do prior_ceiling=0.80 —
    # a régua é conservadora de propósito (não degenera em "100%").
    sel = router.select(MEDIO, "", "", rows=_rows(16, "sonnet", "haiku", "1"), cfg=cfg)
    assert sel.task_class == "sonnet"
    assert sel.tier.name == "haiku"
    assert "prior_demote-1" in sel.reasons


def test_history_prior_ignora_amostra_pequena(cfg):
    sel = router.select(MECANICO, "", "", rows=_rows(3, "haiku", "haiku", "0"), cfg=cfg)
    assert sel.tier.name == "haiku"
    assert not [r for r in sel.reasons if r.startswith("prior_")]


def test_history_prior_nunca_bump_e_demote_juntos(cfg):
    rows = _rows(6, "sonnet", "sonnet", "0") + _rows(16, "sonnet", "haiku", "1")
    sel = router.select(MEDIO, "", "", rows=rows, cfg=cfg)
    priors = [r for r in sel.reasons if r.startswith("prior_")]
    assert len(priors) <= 1, sel.reasons
    assert priors == ["prior_bump+1"]  # o tier corrente falhando vence


# ------------------------------------------------------------- escalation


def test_escalation_sobe_um_rank_por_attempt(cfg):
    assert router.select(MECANICO, "", "", attempt=1, cfg=cfg).tier.name == "sonnet"
    assert router.select(MECANICO, "", "", attempt=2, cfg=cfg).tier.name == "opus"


def test_escalation_clampa_no_topo(cfg):
    sel = router.select(MECANICO, "", "", attempt=5, cfg=cfg)
    assert sel.tier.name == "opus"
    assert sel.escalated_from == "haiku"


def test_should_escalate_falso_em_tamper(cfg):
    sel = router.select(MECANICO, "", "", cfg=cfg)
    assert router.should_escalate("tamper:verify_modified", 0, sel, cfg) is False
    assert router.should_escalate("verify:falhou", 0, sel, cfg) is True


def test_should_escalate_falso_no_max_attempts(cfg):
    sel = router.select(MECANICO, "", "", attempt=2, cfg=cfg)
    assert cfg["max_attempts"] == 3
    assert router.should_escalate("verify:falhou", 2, sel, cfg) is False


def test_env_for_seta_modelo_e_turns(cfg):
    sel = router.select(MECANICO, "", "", cfg=cfg)
    env = router.env_for(sel)
    assert env["HARNESS_MODEL"] == sel.tier.model
    assert env["HARNESS_MAX_TURNS"] == str(sel.tier.max_turns)
