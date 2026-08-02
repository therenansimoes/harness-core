"""As guardas do loop de melhoria: o que ele TEM que recusar antes de rodar.

Cada teste aqui é um jeito conhecido de o loop se enganar — apontar para o
próprio catálogo por um symlink, medir com braço menor que a régua, repetir
para sempre um experimento que empata, começar por cima de um config sujo,
ignorar o próprio deadline. Tudo com backend mock: zero modelo, zero rede.
"""

import shutil
from pathlib import Path

import pytest

from harness.graph import autopilot_graph
from harness.graph.autopilot_graph import run_autopilot
from harness.improve import escalate, mutate
from harness.improve.target import (
    DEFAULTS,
    CatalogError,
    Rule,
    load_catalog,
    pick_target,
    with_ledger_priors,
)
from harness.ledger import store
from harness.ruler.wilson import MIN_N
from harness.types import MutationRow, RunRow

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
UNIT = Path(__file__).parent / "fixtures" / "echo"
KNOB = "router.prior_floor"

CATALOG = """
[improve]
sec_cost_usd = 0.0001
n_per_arm = 6

[[rule]]
id = "floor_up"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: piso mais alto escala mais cedo"
"""

# Regra escrita ao contrário do arquivo: o `to` dela É o valor que o config
# sempre teve. Nada foi aplicado, e mesmo assim o `to` está lá.
CATALOG_TO_IGUAL_AO_BASELINE = """
[improve]
n_per_arm = 6

[[rule]]
id = "floor_down"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.65
to = 0.50
fails_on = ["verify_failed"]
hypothesis = "sintética: catálogo escrito contra outra versão do config"
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Raiz isolada: `config/` real do repo, catálogo sintético, data própria."""
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "catalog.toml").write_text(CATALOG, encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def db(sandbox: Path) -> Path:
    return sandbox / "data" / store.DB_NAME


def row(exit_reason: str = "verify_failed", sec: float = 10.0) -> RunRow:
    return RunRow(
        run_id="r", unit_id="u", project=None, backend="mock", model=None,
        tier="t0", kind="code", ok=False, exit_reason=exit_reason, sec_total=sec,
        sec_provision=0.0, cost_usd=None, intervention=False,
        created_at=store.now_iso(),
    )


def rule(id_: str = "r") -> Rule:
    return Rule(
        id=id_, target_file="config/models.toml", key=KNOB,
        from_value=0.50, to_value=0.65, fails_on=("verify_failed",),
    )


def seed_failures(sandbox: Path, n: int = 3) -> None:
    """Gradiente: sem falha no ledger, `pick_target` não tem o que atacar."""
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"seed{i}", unit_id="echo", project=None, backend="mock",
                model=None, tier="t0", kind="code", ok=False,
                exit_reason="verify_failed", sec_total=10.0, sec_provision=0.0,
                cost_usd=0.0, intervention=False, created_at=store.now_iso(),
            ),
            path=db(sandbox),
        )


# --- self_edit: o loop não se aponta para as próprias regras ---------------------


def test_symlink_para_o_catalogo_e_self_edit(sandbox):
    """`config/alias.toml -> catalog.toml` casa `config/*.toml` e passaria batido
    se o self_edit comparasse o path escrito no catálogo em vez do resolvido."""
    (sandbox / "config" / "alias.toml").symlink_to("catalog.toml")
    r = Rule(id="x", target_file="config/alias.toml", key=KNOB,
             from_value=0.50, to_value=0.65)

    violations = mutate.check(r, root=sandbox)

    assert violations == [f"{mutate.SELF_EDIT}:config/catalog.toml"]


def test_regra_apontada_pro_genoma_e_self_edit(sandbox):
    """O genoma também é `config/*.toml`: quem edita a lista do que pode mudar
    se dá permissão para o que quiser."""
    r = Rule(id="x", target_file="config/genome.toml", key="immutable",
             from_value=1, to_value=2)

    violations = mutate.check(r, root=sandbox)

    assert violations == [f"{mutate.SELF_EDIT}:config/genome.toml"]


def test_path_com_volta_normaliza_antes_do_self_edit(sandbox):
    r = Rule(id="x", target_file="config/../config/catalog.toml", key=KNOB,
             from_value=0.50, to_value=0.65)

    assert mutate.check(r, root=sandbox) == [f"{mutate.SELF_EDIT}:config/catalog.toml"]


# --- catálogo: amostra que a régua não sabe julgar -------------------------------


def test_n_per_arm_abaixo_do_min_n_nao_carrega(tmp_path):
    """Braço de 3 é INCONCLUSIVE por construção — experimento estéril e caro."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "catalog.toml").write_text(
        "[improve]\nn_per_arm = 3\n", encoding="utf-8"
    )

    with pytest.raises(CatalogError, match=f"n_per_arm = 3 < MIN_N = {MIN_N}"):
        load_catalog(root=tmp_path)


# --- prior: empate repetido tem que cansar ---------------------------------------


def test_dois_inconclusive_derrubam_o_prior_ate_o_alvo_sumir():
    """Sem isto o loop repetiria o mesmo experimento eternamente: INCONCLUSIVE
    não muda o prior, o ganho não muda, a mesma regra é escolhida de novo."""
    history = [row()] * 4
    catalog = [rule()]
    cfg = {"min_gain": 0.0004}          # entre o ganho de prior 0.50 e o de 0.25

    assert pick_target(history, catalog, cfg).gain == pytest.approx(
        4 / 4 * 10.0 * DEFAULTS["sec_cost_usd"] * 0.5
    )

    empatou = with_ledger_priors(catalog, [
        MutationRow("m1", "r", "INCONCLUSIVE", "3/6", "3/6", "t", True),
        MutationRow("m2", "r", "INCONCLUSIVE", "4/6", "4/6", "t", True),
    ])

    assert (empatou[0].prior_succ, empatou[0].prior_n) == (0, 2)
    assert empatou[0].prior() == pytest.approx(0.25)
    # ganho caiu abaixo do que o experimento custa: NO_GRADIENT -> humano.
    assert pick_target(history, empatou, cfg) is None


def test_regra_rejeitada_pelo_genoma_sai_da_fila():
    """REJECTED não é veredito da régua: é parede. Insistir bate na mesma."""
    out = with_ledger_priors(
        [rule("barrada"), rule("livre")],
        [MutationRow("m1", "barrada", "REJECTED", "0/0", "0/0", "t", False,
                     "genome:self_edit:config/catalog.toml")],
    )

    assert [r.id for r in out] == ["livre"]


# --- boot: config sujo é crash entre apply e record ------------------------------


def crash_after_apply(sandbox: Path, rule_id: str = "floor_up") -> None:
    """Crash entre `apply` e `record`: o toml muta e o ledger fica só com o
    marcador do nó — a evidência que o `_apply` de verdade grava ANTES de rodar
    qualquer coisa, e a única que sobrevive a um SIGKILL."""
    rules, _ = load_catalog(root=sandbox)
    alvo = next(r for r in rules if r.id == rule_id)
    m = mutate.apply(alvo, store.now_iso(), root=sandbox)
    store.record_node(
        "improve-crash",
        autopilot_graph.APPLY_NODE,
        {"rule_id": m.rule_id, "mutation_id": m.mutation_id,
         "target_file": m.target_file, "key": m.key},
        path=db(sandbox),
    )


def test_start_recusa_config_sujo(sandbox):
    """`apply` sem `record` (crash no meio) deixa o braço A já mutado: o A/B
    compararia a mutação contra ela mesma. Thread nova tem que recusar."""
    crash_after_apply(sandbox)
    assert mutate.read_value(sandbox / "config" / "models.toml", KNOB) == 0.65

    with pytest.raises(ValueError, match="mutação pendente.*floor_up"):
        run_autopilot(sandbox / "data", units=[UNIT], root=sandbox)


def test_start_aceita_to_igual_ao_baseline_sem_rastro_de_apply(sandbox):
    """O `to` da regra ser o valor de hoje NÃO é config sujo.

    Falso positivo do guard antigo (comparava só o token): catálogo escrito
    contra outra versão do arquivo travava o loop para sempre com "mutação
    pendente" num repo em que ninguém aplicou nada. Sem marcador de `apply` e
    sem ABORTED no ledger, não há aplicação para acusar.
    """
    (sandbox / "config" / "catalog.toml").write_text(
        CATALOG_TO_IGUAL_AO_BASELINE, encoding="utf-8"
    )
    assert mutate.read_value(sandbox / "config" / "models.toml", KNOB) == 0.50

    rules, _ = load_catalog(root=sandbox)
    assert autopilot_graph._pending_rules(rules, sandbox, db(sandbox)) == []

    # e o loop começa: a regra só não é aplicável (o `from` não descreve o
    # arquivo), o que é NO_GRADIENT, não recusa de boot.
    report = run_autopilot(sandbox / "data", units=[UNIT], root=sandbox)
    assert report.escalation["reason"] == escalate.NO_GRADIENT


def test_start_recusa_aborted_nao_revertida(sandbox):
    """A outra prova de sujeira: o ledger diz ABORTED e não diz ter voltado."""
    rules, _ = load_catalog(root=sandbox)
    mutate.apply(next(r for r in rules if r.id == "floor_up"), "t", root=sandbox)
    store.record_mutation(
        MutationRow("m1", "floor_up", "ABORTED", "0/0", "0/0", store.now_iso(), False),
        path=db(sandbox),
    )

    with pytest.raises(ValueError, match="mutação pendente.*floor_up"):
        run_autopilot(sandbox / "data", units=[UNIT], root=sandbox)


def test_start_aceita_config_mutada_com_keep_no_ledger(sandbox):
    """Mesma config, com o KEEP registrado: aí a mutação TEM dono e o loop segue."""
    rules, _ = load_catalog(root=sandbox)
    mutate.apply(next(r for r in rules if r.id == "floor_up"), "t", root=sandbox)
    store.record_mutation(
        MutationRow("m1", "floor_up", "KEEP", "0/6", "6/6", store.now_iso(), False),
        path=db(sandbox),
    )

    report = run_autopilot(sandbox / "data", units=[UNIT], root=sandbox)

    assert report.escalation["reason"] == escalate.NO_GRADIENT


def test_keep_antigo_nao_vira_pendencia_depois_de_500_mutacoes(sandbox):
    """O veredito não expira. Antes, `node_payloads` lia TODAS as marcas de
    `apply` e as mutações vinham pela janela de 500: 500 experimentos depois, o
    KEEP saía da janela, a marca virava "apply sem veredito" e o boot recusava
    para sempre um repo em que ninguém deixou nada pendente.
    """
    rules, _ = load_catalog(root=sandbox)
    m = mutate.apply(next(r for r in rules if r.id == "floor_up"), "t", root=sandbox)
    store.record_node(
        "improve-antigo",
        autopilot_graph.APPLY_NODE,
        {"rule_id": m.rule_id, "mutation_id": m.mutation_id,
         "target_file": m.target_file, "key": m.key},
        path=db(sandbox),
    )
    store.record_mutation(
        MutationRow(m.mutation_id, "floor_up", "KEEP", "0/6", "6/6", "t", False),
        path=db(sandbox),
    )
    assert autopilot_graph._pending_rules(rules, sandbox, db(sandbox)) == []

    for i in range(500):                     # experimentos de OUTRA regra depois
        store.record_mutation(
            MutationRow(f"outra-{i:04d}", "outra_regra", "REVERT", "3/6", "3/6",
                        "t", True),
            path=db(sandbox),
        )

    assert autopilot_graph._pending_rules(rules, sandbox, db(sandbox)) == []
    report = run_autopilot(sandbox / "data", units=[UNIT], root=sandbox)
    assert report.escalation["reason"] == escalate.NO_GRADIENT


def test_ciclo_completo_deixa_marca_de_apply_e_nao_fica_pendente(sandbox):
    """A marca vem do nó `apply` de verdade, não só do teste que simula o crash:
    ciclo inteiro grava marca E veredito, e o boot seguinte não acusa nada."""
    seed_failures(sandbox)

    run_autopilot(sandbox / "data", units=[UNIT], root=sandbox, backend="mock")

    marcas = store.node_payloads(autopilot_graph.APPLY_NODE, path=db(sandbox))
    assert [m["rule_id"] for m in marcas] == ["floor_up"]
    julgadas = {m.mutation_id for m in store.mutations(path=db(sandbox))}
    assert marcas[0]["mutation_id"] in julgadas

    rules, _ = load_catalog(root=sandbox)
    assert autopilot_graph._pending_rules(rules, sandbox, db(sandbox)) == []


def test_start_recusa_config_sujo_pela_cli(sandbox, capsys):
    from harness import cli

    crash_after_apply(sandbox)

    assert cli.main(["improve", "--unit", str(UNIT)]) == 1
    assert "mutação pendente" in capsys.readouterr().err


# --- resposta do humano: a flag não pode ser ignorada em silêncio ----------------


def test_answer_sem_resume_e_erro_mesmo_igual_ao_default(sandbox, capsys):
    """`--answer` sem `--resume` é erro SEMPRE. Com a checagem antiga
    (`!= IMPROVE_ANSWER`), digitar exatamente o JSON default passava batido: o
    humano achava que tinha respondido a escalação e nada tinha sido respondido.
    """
    from harness import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["improve", "--unit", str(UNIT), "--answer", cli.IMPROVE_ANSWER])

    assert exc.value.code == 2
    assert "--answer só faz sentido com --resume" in capsys.readouterr().err


def test_resume_nao_reimprime_escalacao_ja_respondida(sandbox, capsys):
    """Escalação respondida sai do estado: o stderr do resume não pede de novo
    uma ajuda que já veio (e com o `thread=` de uma thread encerrada)."""
    from harness import cli

    assert cli.main(["improve", "--unit", str(UNIT), "--backend", "mock"]) == 0
    err = capsys.readouterr().err
    assert "escalate no_gradient" in err
    thread = err.split("thread=")[1].split()[0]

    rc = cli.main([
        "improve", "--unit", str(UNIT), "--backend", "mock",
        "--resume", thread, "--answer", '{"action":"abort"}',
    ])

    assert rc == 0
    assert "escalate" not in capsys.readouterr().err


# --- deadline: o relógio tem que interromper alguma coisa ------------------------


class Relogio:
    """Relógio de mentira que anda `step` a cada consulta.

    O deadline do autopilot é medido em `time.time()`, e o teste precisa que ele
    estoure DENTRO do A/B — com relógio real isso vira sleep e flakiness. Só o
    módulo `autopilot_graph` enxerga este objeto; o resto do mundo usa o relógio
    de verdade.
    """

    def __init__(self, t0: float = 1_000_000.0, step: float = 1.0) -> None:
        self.now = t0
        self.step = step

    def time(self) -> float:
        self.now += self.step
        return self.now


def test_deadline_no_meio_do_ab_aborta_reverte_e_escala(sandbox, monkeypatch):
    """Deadline de 10 "segundos" com 12 runs pela frente: as primeiras rodam, o
    relógio estoura entre duas delas e o experimento morre inteiro."""
    seed_failures(sandbox)
    alvo = sandbox / "config" / "models.toml"
    antes = alvo.read_bytes()
    monkeypatch.setattr(autopilot_graph, "time", Relogio())

    report = run_autopilot(
        sandbox / "data", units=[UNIT], root=sandbox, backend="mock", deadline_s=10
    )

    assert report.escalation["reason"] == escalate.DEADLINE
    # o ponto do fix: a parada é DENTRO do experimento, não na entrada de um nó
    assert report.escalation["evidence"]["node"] == "fanout_ab"
    assert report.escalation["evidence"]["reverted"] is True
    assert alvo.read_bytes() == antes          # revert imediato, sem esperar o humano

    runs = [r for r in store.history(path=db(sandbox), limit=100) if r.run_id[:4] != "seed"]
    assert 0 < len(runs) < 12, "o A/B tem que ter começado e não ter terminado"
    # A linha do ledger é gravada NA PARADA, não no resume: o humano pode nunca
    # responder, e mutação aplicada sem rastro é o que o replay não pode ter.
    parado = store.mutations(path=db(sandbox))
    assert [(m.verdict, m.reverted, m.note) for m in parado] == [("ABORTED", True, "deadline")]

    fim = run_autopilot(
        sandbox / "data", units=[UNIT], root=sandbox, backend="mock",
        thread_id=report.thread_id, resume={"action": escalate.ABORT},
    )

    assert len(store.mutations(path=db(sandbox))) == 1   # o resume não duplica
    linha = store.mutations(path=db(sandbox))[0]
    assert (linha.verdict, linha.reverted, linha.note) == ("ABORTED", True, "deadline")
    assert fim.results[0]["arm_a"] == "0/0"    # braço parcial não vira amostra
    assert alvo.read_bytes() == antes


def test_deadline_s_zero_nao_e_sem_deadline(sandbox, capsys):
    """`--deadline-s 0` é "o tempo acabou", não "tempo infinito" — e com
    `if deadline_s` no lugar de `is not None` era exatamente o contrário."""
    from harness import cli

    seed_failures(sandbox)
    antes = (sandbox / "config" / "models.toml").read_bytes()

    rc = cli.main([
        "improve", "--cycles", "1", "--deadline-s", "0",
        "--unit", str(UNIT), "--backend", "mock",
    ])

    assert rc == 0
    assert "escalate deadline" in capsys.readouterr().err
    novas = [r for r in store.history(path=db(sandbox), limit=100) if r.run_id[:4] != "seed"]
    assert novas == []
    assert (sandbox / "config" / "models.toml").read_bytes() == antes
