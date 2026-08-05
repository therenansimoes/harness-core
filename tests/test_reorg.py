"""Reorg puro: as quatro regras, a precedência entre elas e a reversão."""

from harness.governor.reorg import (
    R_COLLAPSE,
    R_ESCALATE,
    R_REVIEWER,
    R_SKIP,
    ReorgConfig,
    ReorgDecision,
    ReorgSignals,
    decide,
    diff_active,
    load_reorg,
)

CFG = ReorgConfig()


def _sig(**over) -> ReorgSignals:
    base = {
        "failure_classes": {},
        "area_counts": {},
        "total_runs": 0,
        "spent_usd": 0.0,
        "task_value_usd": 0.0,
        "prompt_chars": 4000,
        "kind": "code",
        "attempt": 0,
    }
    return ReorgSignals(**{**base, **over})


def _ids(decisions) -> list[str]:
    return [d.rule_id for d in decisions]


def test_secao_ausente_usa_defaults(tmp_path):
    """Toml sem `[reorg]` (e arquivo inexistente) não é config vazia: é o
    default congelado. Reorg que se desliga quando o toml não fala dele
    esconderia a regressão em vez de manter o comportamento de fábrica."""
    p = tmp_path / "governor.toml"
    p.write_text("[deadline]\nrun_s = 900.0\n", encoding="utf-8")
    assert load_reorg(p) == ReorgConfig()
    assert load_reorg(tmp_path / "nao_existe.toml") == ReorgConfig()


def test_r1_dispara_com_duas_falhas_mesma_classe():
    d = decide(_sig(failure_classes={"verify_failed": 2}), CFG)
    assert _ids(d) == [R_ESCALATE]
    assert (d[0].tier_delta, d[0].effect) == (1, "applied")
    assert d[0].signal == {"failure_class": "verify_failed", "count": 2}


def test_r1_nao_dispara_com_uma():
    """Uma falha é ruído; a regra existe para o erro que VOLTA — duas classes
    diferentes com uma ocorrência cada também não somam."""
    assert decide(_sig(failure_classes={"verify_failed": 1}), CFG) == []
    assert decide(_sig(failure_classes={"a": 1, "b": 1}), CFG) == []


def test_r2_area_concentrada_e_recorded():
    d = decide(_sig(area_counts={"api": 3, "web": 1}, total_runs=4), CFG)
    assert _ids(d) == [R_REVIEWER]
    assert (d[0].effect, d[0].tier_delta) == ("recorded", 0)
    assert d[0].signal["area"] == "api"

    # Mesma concentração com amostra abaixo do piso não acusa nada.
    assert decide(_sig(area_counts={"api": 2}, total_runs=3), CFG) == []


def test_r3_custo_acima_do_valor():
    d = decide(_sig(spent_usd=2.0, task_value_usd=1.0), CFG)
    assert _ids(d) == [R_COLLAPSE]
    assert d[0].signal["roles_cap"] == 1
    assert d[0].effect == "recorded"

    # Valor desconhecido não condena: sem denominador todo centavo "estouraria".
    assert decide(_sig(spent_usd=2.0, task_value_usd=0.0), CFG) == []


def test_r4_trivial_bloqueia_escalada():
    d = decide(_sig(prompt_chars=40, kind="config"), CFG)
    assert _ids(d) == [R_SKIP]
    assert (d[0].tier_delta, d[0].escalate_blocked, d[0].effect) == (-1, True, "applied")

    # Curto mas de um kind que não é raso: continua orquestrado.
    assert decide(_sig(prompt_chars=40, kind="refactor"), CFG) == []


def test_r4_vence_r1():
    """Tarefa trivial que falhou repetido fica SIMPLES, não cara: pagar o tier
    de cima pelo mesmo prompt de três linhas é o gasto que a regra corta."""
    d = decide(_sig(prompt_chars=40, kind="config", failure_classes={"verify_failed": 5}), CFG)
    assert _ids(d) == [R_SKIP]


def test_diff_active_detecta_reversao():
    prev = [
        {"rule_id": R_ESCALATE, "state": "applied", "signal": {"count": 2}},
        {"rule_id": R_REVIEWER, "state": "applied", "signal": {"area": "api"}},
    ]
    agora = decide(_sig(area_counts={"api": 4}, total_runs=4), CFG)
    novas, revertidas = diff_active(prev, agora)

    assert novas == []  # reviewer já estava no ledger
    assert [p["rule_id"] for p in revertidas] == [R_ESCALATE]

    # Regra já revertida não é revertida de novo: o último estado é que vale.
    prev2 = [*prev, {"rule_id": R_ESCALATE, "state": "reverted"}]
    novas2, revertidas2 = diff_active(prev2, agora)
    assert (novas2, revertidas2) == ([], [])

    # E decisão inédita entra como nova.
    novas3, _ = diff_active([], [ReorgDecision(R_SKIP, "skip_orchestration", "x")])
    assert _ids(novas3) == [R_SKIP]


def test_decide_nunca_levanta_com_config_lixo(tmp_path):
    """Config com tipo torto degrada campo a campo, e `decide` engole sinal
    ilegível: reorg quebrado não pode ser motivo de run derrubado."""
    p = tmp_path / "governor.toml"
    p.write_text(
        "[reorg]\n"
        'enabled = "talvez"\n'
        'repeat_failures = "muitas"\n'
        "area_ratio = 9.0\n"
        "area_min_n = -3\n"
        "cost_value_ratio = 0\n"
        "trivial_max_chars = 0\n"
        "trivial_kinds = 7\n",
        encoding="utf-8",
    )
    assert load_reorg(p) == ReorgConfig()

    torto = ReorgSignals(
        failure_classes={"x": "dois", 3: 9},  # type: ignore[dict-item]
        area_counts="nada",  # type: ignore[arg-type]
        total_runs=None,  # type: ignore[arg-type]
        spent_usd="grátis",  # type: ignore[arg-type]
        task_value_usd=None,  # type: ignore[arg-type]
        prompt_chars="muitos",  # type: ignore[arg-type]
        kind=None,  # type: ignore[arg-type]
        attempt="dois",  # type: ignore[arg-type]
    )
    assert decide(torto, load_reorg(p)) == []
    assert decide(_sig(failure_classes={"v": 2}), ReorgConfig(enabled=False)) == []
