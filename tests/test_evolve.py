"""Evolução: população converge, operadores determinísticos, archive persiste."""

import random

from harness.evolve.archive import Archive
from harness.evolve.population import crossover, mutate_config, run_population

N_TRIALS = 20
OPTIMUM = 10.0


def evaluate_synthetic(config: dict) -> tuple[int, int]:
    """Sucesso cai com a distância de `x` ao ótimo conhecido. Determinístico."""
    dist = abs(config.get("x", 0.0) - OPTIMUM)
    succ = max(0, N_TRIALS - int(dist * 2))
    return min(succ, N_TRIALS), N_TRIALS


def test_mutate_deterministico_e_nao_muta_base():
    base = {"x": 5.0, "n": 4, "flag": True, "name": "a", "sub": {"y": 1.0}}
    m1 = mutate_config(base, random.Random(42))
    m2 = mutate_config(base, random.Random(42))
    assert m1 == m2
    assert base == {"x": 5.0, "n": 4, "flag": True, "name": "a", "sub": {"y": 1.0}}
    assert m1["name"] == "a"  # não-numérico intacto
    assert isinstance(m1["x"], float) and isinstance(m1["n"], int)


def test_crossover_deterministico_mistura_chaves():
    a = {"x": 1.0, "y": 2.0, "only_a": 1}
    b = {"x": 9.0, "y": 8.0, "only_b": 2}
    c1 = crossover(a, b, random.Random(7))
    c2 = crossover(a, b, random.Random(7))
    assert c1 == c2
    assert c1["only_a"] == 1 and c1["only_b"] == 2
    assert c1["x"] in (1.0, 9.0) and c1["y"] in (2.0, 8.0)


def test_populacao_converge_para_otimo():
    rng = random.Random(0)
    seeds = [{"x": rng.uniform(0.0, 5.0)} for _ in range(8)]
    final = run_population(
        evaluate_synthetic, seeds, generations=15, pop_size=8, rng=random.Random(1)
    )
    best = final[0]
    baseline = max(evaluate_synthetic(s)[0] for s in seeds)
    assert best.successes > baseline  # melhorou sobre as seeds
    assert abs(best.config["x"] - OPTIMUM) < 2.0
    assert best.wilson_low > 0.6
    assert best.trials == N_TRIALS


def test_populacao_deterministica_com_mesmo_rng():
    seeds = [{"x": 1.0}, {"x": 3.0}]
    r1 = run_population(evaluate_synthetic, seeds, 5, 6, random.Random(9))
    r2 = run_population(evaluate_synthetic, seeds, 5, 6, random.Random(9))
    assert [i.config for i in r1] == [i.config for i in r2]


def test_archive_mantem_melhor_por_nicho(tmp_path):
    db = tmp_path / "archive.sqlite"
    arc = Archive(db)
    assert arc.add(("fix", "low"), {"x": 1}, 0.5) is True
    assert arc.add(("fix", "low"), {"x": 2}, 0.9) is True
    assert arc.add(("fix", "low"), {"x": 3}, 0.7) is False  # pior não entra
    cfg, score = arc.best(("fix", "low"))
    assert cfg == {"x": 2} and score == 0.9
    arc.add(("feature", "high"), {"x": 4}, 0.3)
    assert set(arc.niches()) == {("fix", "low"), ("feature", "high")}
    arc.close()


def test_archive_sobrevive_reopen(tmp_path):
    db = tmp_path / "archive.sqlite"
    a1 = Archive(db)
    a1.add(("fix", "mid"), {"x": 7}, 0.8)
    a1.close()
    a2 = Archive(db)
    assert a2.best(("fix", "mid")) == ({"x": 7}, 0.8)
    assert a2.niches() == [("fix", "mid")]
    a2.close()


def test_archive_bucket_invalido(tmp_path):
    arc = Archive(tmp_path / "a.sqlite")
    try:
        arc.add(("fix", "huge"), {}, 0.1)
        assert False, "deveria rejeitar bucket inválido"
    except ValueError:
        pass
    finally:
        arc.close()
