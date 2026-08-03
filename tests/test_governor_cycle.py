"""Teto do CICLO (`cycle_s`): quem o mede e quem ele não mata.

O prazo do run já tinha consumidor (`budget.deadline_ts`, via `--deadline-s`);
`cycle_s` era config sem ninguém do outro lado. Aqui: o limiar corta na entrada
do nó, pelo mesmo `escalate` do deadline, e config zerada/ciclo sem marca não
corta nada.
"""

from __future__ import annotations

from harness.governor import governor as gov_mod
from harness.graph import autopilot_graph as ag
from harness.graph.state import Budget
from harness.improve import escalate as esc

CYCLE_S = 60.0
T0 = 1_000_000.0


def _state(started_ts: float | None) -> ag.AutopilotState:
    """Estado mínimo para o `_expired`: prazo do run desligado de propósito,
    para que o único corte possível seja o do ciclo."""
    return {
        "cycle": 0,
        "cycles": 1,
        "units": ["u"],
        "budget": Budget(deadline_ts=None),
        "cycle_started_ts": started_ts,
        "mutation": None,
    }  # type: ignore[return-value]


def _cfg(now: float):
    return {"configurable": {ag.CFG_CLOCK: lambda: now}}


def test_cycle_s_corta_no_limiar_pelo_escalate(monkeypatch):
    """elapsed == cycle_s já é cutoff, e o nó sai pelo `escalate` do deadline."""
    gov = gov_mod.Governor(cycle_s=CYCLE_S)
    monkeypatch.setattr(gov_mod, "load_gov", lambda *a, **k: gov)

    assert gov_mod.check_cycle(T0, T0 + CYCLE_S - 0.001, gov) == gov_mod.CONTINUE
    assert gov_mod.check_cycle(T0, T0 + CYCLE_S, gov) == gov_mod.CUTOFF

    assert ag._expired(_state(T0), "propose", _cfg(T0 + CYCLE_S - 1)) is None

    stop = ag._expired(_state(T0), "propose", _cfg(T0 + CYCLE_S))
    assert stop is not None
    ev = stop["escalation"]["evidence"]
    assert stop["escalation"]["reason"] == esc.DEADLINE
    assert (ev["node"], ev["governor"]) == ("propose", "governor:ciclo_estourado")
    assert ev["cycle_s"] == CYCLE_S


def test_cycle_s_fail_open(tmp_path, monkeypatch):
    """Sem teto (0) ou sem marca de início, o ciclo não é cortado por ninguém."""
    p = tmp_path / "governor.toml"
    p.write_text("[deadline]\ncycle_s = 0\n", encoding="utf-8")
    gov = gov_mod.load_gov(p)
    assert gov.cycle_s == 0.0  # zero é "sem corte", não o default de fábrica
    assert gov_mod.check_cycle(T0, T0 + 10_000.0, gov) == gov_mod.CONTINUE

    monkeypatch.setattr(gov_mod, "load_gov", lambda *a, **k: gov)
    assert ag._expired(_state(T0), "apply", _cfg(T0 + 10_000.0)) is None

    # Teto ligado, mas ciclo sem marca (thread de antes do campo): segue.
    com_teto = gov_mod.Governor(cycle_s=CYCLE_S)
    assert gov_mod.check_cycle(None, T0 + 10_000.0, com_teto) == gov_mod.CONTINUE
    monkeypatch.setattr(gov_mod, "load_gov", lambda *a, **k: com_teto)
    assert ag._expired(_state(None), "apply", _cfg(T0 + 10_000.0)) is None
