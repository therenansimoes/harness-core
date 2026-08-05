"""Guards puros: orçamento, parada atada ao verify e trava de oscilação.

Tudo injetado, nada de ledger: o que se testa aqui é a decisão, não o grafo —
o runtime está em `test_reorg_runtime`.
"""

from harness.governor.guards import (
    G_BUDGET,
    G_FREEZE,
    G_VERIFY,
    GuardsConfig,
    budget_exceeded,
    flipflop,
    frozen,
    load_guards,
    verify_stop,
)

CFG = GuardsConfig()


def _rows(*pares: tuple[str, str]) -> list[dict]:
    return [{"rule_id": r, "state": s} for r, s in pares]


def test_secao_ausente_usa_defaults(tmp_path):
    """Toml sem `[guards]` (e arquivo inexistente) não é config vazia: é o
    default congelado — guard que se desliga quando o toml não fala dele
    esconderia a regressão."""
    p = tmp_path / "governor.toml"
    p.write_text("[deadline]\nrun_s = 900.0\n", encoding="utf-8")
    assert load_guards(p) == GuardsConfig()
    assert load_guards(tmp_path / "nao_existe.toml") == GuardsConfig()


def test_campo_torto_cai_no_default(tmp_path):
    """Campo ilegível degrada campo a campo; zero é VÁLIDO ("desligado"), não
    torto — o loader tem que saber a diferença."""
    p = tmp_path / "governor.toml"
    p.write_text(
        '[guards]\nmax_cost_usd = "muito"\nmax_attempts = true\nflipflop_window = 1\n',
        encoding="utf-8",
    )
    assert load_guards(p) == GuardsConfig()

    p.write_text("[guards]\nmax_cost_usd = 0\nverify_fail_stop = 0\n", encoding="utf-8")
    cfg = load_guards(p)
    assert cfg.max_cost_usd == 0.0
    assert cfg.verify_fail_stop == 0


def test_budget_custo_no_limiar_exato():
    """`>=` no teto exato, igual ao governor: teto é teto. Zero desliga."""
    v = budget_exceeded(3.0, 0, 0.0, CFG)
    assert v.fired and v.guard_id == G_BUDGET
    assert v.reason.startswith("guard:budget:cost")
    assert v.signal == {"kind": "cost", "spent_usd": 3.0, "cap_usd": 3.0}

    assert not budget_exceeded(2.9999, 0, 0.0, CFG).fired
    assert not budget_exceeded(9999.0, 0, 0.0, GuardsConfig(max_cost_usd=0.0)).fired


def test_budget_ordem_deterministica():
    """Dois tetos estourados dão sempre o mesmo motivo: custo primeiro."""
    v = budget_exceeded(10.0, 10, 0.0, CFG)
    assert v.fired
    assert v.reason.startswith("guard:budget:cost")


def test_budget_attempts_e_wall():
    v = budget_exceeded(0.0, 4, 0.0, CFG)
    assert v.fired and v.signal == {"kind": "attempts", "attempt": 4, "cap": 4}
    assert not budget_exceeded(0.0, 3, 0.0, CFG).fired

    v = budget_exceeded(0.0, 0, 600.0, CFG)
    assert v.fired and v.signal["kind"] == "wall"
    assert "600.0s" in v.reason

    # Sinal ilegível vira zero e zero não estoura teto nenhum.
    assert not budget_exceeded(None, "x", "muito", CFG).fired


def test_verify_stop_limiar():
    assert not verify_stop(2, CFG).fired
    v = verify_stop(3, CFG)
    assert v.fired and v.guard_id == G_VERIFY
    assert v.reason == "guard:verify_stop:3x_top_tier"
    assert v.signal == {"consecutive_fails": 3, "cap": 3}
    assert not verify_stop(99, GuardsConfig(verify_fail_stop=0)).fired


def test_flipflop_reativacao_dispara():
    """Aplicada -> revertida -> aplicada é o reorg desfazendo a si mesmo."""
    rows = _rows(
        ("escalate_route", "applied"),
        ("escalate_route", "reverted"),
        ("escalate_route", "applied"),
    )
    v = flipflop(rows, CFG)
    assert v.fired and v.guard_id == G_FREEZE
    assert v.reason == "guard:freeze:escalate_route"
    assert v.signal["rule_id"] == "escalate_route"


def test_flipflop_sem_reativacao_nao_dispara():
    assert not flipflop(_rows(("escalate_route", "applied")), CFG).fired
    assert not flipflop(
        _rows(("escalate_route", "applied"), ("escalate_route", "reverted")), CFG
    ).fired
    # Duas regras DIFERENTES intercaladas com uma reversão não são oscilação.
    assert not flipflop(
        _rows(
            ("escalate_route", "applied"),
            ("insert_reviewer", "applied"),
            ("escalate_route", "reverted"),
            ("insert_reviewer", "applied"),
        ),
        CFG,
    ).fired


def test_flipflop_janela_corta_o_passado():
    """O par aplicada/revertida empurrado para fora da janela por linhas novas
    não conta: oscilação velha é história, não sinal."""
    rows = _rows(
        ("escalate_route", "applied"),
        ("escalate_route", "reverted"),
        ("insert_reviewer", "applied"),
        ("escalate_route", "applied"),
    )
    assert flipflop(rows, GuardsConfig(flipflop_window=6)).fired
    assert not flipflop(rows, GuardsConfig(flipflop_window=2)).fired


def test_flipflop_ignora_linhas_de_guard_e_lixo():
    """Linha de guard, linha torta e state ausente (que vale "applied") não
    derrubam nada nem inventam oscilação."""
    rows = [
        {"rule_id": "guard:budget", "state": "applied"},
        {"rule_id": "guard:budget", "state": "reverted"},
        {"rule_id": "guard:budget", "state": "applied"},
        "lixo",
        None,
        {"rule_id": 7, "state": "applied"},
        {"state": "applied"},
    ]
    assert not flipflop(rows, CFG).fired

    # `state` ausente vale "applied", igual ao diff_active do reorg.
    completos = [
        {"rule_id": "escalate_route"},
        {"rule_id": "escalate_route", "state": "reverted"},
        {"rule_id": "escalate_route"},
    ]
    assert flipflop(completos, CFG).fired


def test_frozen():
    assert frozen([{"rule_id": G_FREEZE}])
    assert not frozen([])
    assert not frozen([{"rule_id": "guard:budget"}])
    assert not frozen(["lixo", None, {"sem_rule": 1}])
