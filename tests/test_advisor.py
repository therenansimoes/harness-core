"""Advisor como checker PAGO do retry (padrão worker/checker, como `reflect`,
mas gastando dinheiro em vez de $0).

O contrato testado: (1) run saudável nunca paga um centavo; (2) verify
vermelho com `--advisor` armado dispara UM turno read-only e o texto entra no
prompt da tentativa seguinte; (3) desarmado é byte a byte o comportamento de
hoje; (4) consultor que escreve arquivo tem o texto descartado (custo
gravado do mesmo jeito — é o invariante do dinheiro); (5) consultor que
levanta degrada pro retry local puro; (6) o teto explícito (`--max-usd`) conta
o gasto do consultor; (7) a tabela de vetos de `should_advise`; (8) a
topologia default tem `advise` depois de `reflect`; (9) `extract_text` nos
formatos que um trace de verdade pode assumir."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.governor import ceiling
from harness.graph import advisor, topology
from harness.graph.run_graph import run_unit
from harness.improve import topology_grammar
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

MISSING = "exigido.txt"
MOTIVO_TAIL = "MOTIVO_UNICO_XYZ"
# `echo` + `false`: tail determinístico entre sistemas (ao contrário da
# mensagem de erro do grep, que varia de wording por plataforma).
VERIFY_CMD = f"echo {MOTIVO_TAIL} && false"


class LocalBackend:
    """Nunca cria o arquivo que a régua cobra: as tentativas reprovam até o
    teto. O que interessa nos testes é o PROMPT de cada chamada — é ali que o
    conselho do consultor (ou a ausência dele) tem de aparecer."""

    name: ClassVar[str] = "local"

    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.prompts.append(req.prompt)
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / "outro.txt").write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=("outro.txt",),
            session_id=None,
            trace_path=req.trace_path,
        )


class PagoBackend:
    """Fake do consultor PAGO. Grava o `ExecRequest` inteiro (pra inspecionar
    `tools`/`trace_path`/`prompt`) e escreve o diagnóstico no `trace_path` —
    é isto que `advisor.extract_text` lê de volta."""

    name: ClassVar[str] = "pago"

    def __init__(
        self,
        calls: list[ExecRequest],
        *,
        texto: str = "FAÇA: 1. arquivo exigido.txt — crie com 'pronto' dentro.",
        cost_usd: float = 0.03,
        ok: bool = True,
        files_changed: tuple[str, ...] = (),
        boom: bool = False,
    ) -> None:
        self.calls = calls
        self.texto = texto
        self.cost_usd = cost_usd
        self.ok = ok
        self.files_changed = files_changed
        self.boom = boom

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=True,
            tools=frozenset(advisor.READ_ONLY_TOOLS),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append(req)
        if self.boom:
            raise RuntimeError("consultor caiu")
        req.trace_path.parent.mkdir(parents=True, exist_ok=True)
        req.trace_path.write_text(json.dumps({"result": self.texto}), encoding="utf-8")
        return ExecResult(
            ok=self.ok,
            exit_reason="done" if self.ok else "error",
            turns=1,
            cost_usd=self.cost_usd,
            tokens_in=10,
            tokens_out=20,
            files_changed=self.files_changed,
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def local():
    prompts: list[str] = []
    registry.register("local", lambda: LocalBackend(prompts))
    yield prompts
    registry.unregister("local")


@pytest.fixture
def pago():
    calls: list[ExecRequest] = []
    box = {"backend": PagoBackend(calls)}
    registry.register("pago", lambda: box["backend"])
    yield calls, box
    registry.unregister("pago")


def _unit(tmp_path: Path, name: str, verify_cmd: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "faça a coisa"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    return unit


ADVISOR = ("pago", "modelo-consultor", "tier-pago")


# --- e2e via run_unit ----------------------------------------------------------


def test_run_saudavel_nunca_chama_consultor(data_dir, tmp_path, local, pago):
    calls, _ = pago
    unit = _unit(tmp_path, "saudavel", "true")
    final = run_unit(unit, "local", None, data_dir, thread_id="t-saudavel", advisor=ADVISOR)

    assert final["decision"].action == "accept"
    assert final["attempt"] == 0
    assert calls == []
    assert [e for e in final["events"] if e["node"] == "advise"] == []
    assert store.get_node(final["run_id"], "advise", data_dir / store.DB_NAME, attempt=0) is None


def test_verify_vermelho_chama_um_turno_pago_e_o_texto_entra_no_prompt_seguinte(
    data_dir, tmp_path, local, pago
):
    calls, _ = pago
    unit = _unit(tmp_path, "vermelho", VERIFY_CMD)
    final = run_unit(
        unit, "local", None, data_dir, thread_id="t-vermelho", max_attempts=2, advisor=ADVISOR
    )

    assert len(calls) == 1
    req = calls[0]
    assert req.tools == advisor.READ_ONLY_TOOLS
    ws = Path(final["workspace"]).resolve()
    assert ws not in req.trace_path.resolve().parents
    assert VERIFY_CMD in req.prompt
    assert MOTIVO_TAIL in req.prompt

    assert len(local) == 2, "duas tentativas, dois prompts pro executor local"
    assert advisor.ADVISOR_HEADER not in local[0]
    assert advisor.ADVISOR_HEADER in local[1]
    assert "FAÇA: 1. arquivo exigido.txt" in local[1]

    advise_evts = [e for e in final["events"] if e["node"] == "advise"]
    assert len(advise_evts) == 1
    assert advise_evts[0]["called"] is True
    assert advise_evts[0]["cost_usd"] == 0.03


def test_desarmado_e_byte_a_byte_o_de_hoje(data_dir, tmp_path, local, pago):
    calls, _ = pago
    unit = _unit(tmp_path, "desarmado", VERIFY_CMD)
    final = run_unit(unit, "local", None, data_dir, thread_id="t-desarmado", max_attempts=2)

    assert calls == []
    assert [e for e in final["events"] if e["node"] == "advise"] == []
    assert len(local) == 2
    assert advisor.ADVISOR_HEADER not in local[1]


def test_consultor_que_escreve_arquivo_tem_o_texto_descartado(data_dir, tmp_path, local, pago):
    calls, box = pago
    box["backend"] = PagoBackend(calls, files_changed=("evil.txt",))
    unit = _unit(tmp_path, "escritor", VERIFY_CMD)
    final = run_unit(
        unit, "local", None, data_dir, thread_id="t-escritor", max_attempts=2, advisor=ADVISOR
    )

    assert len(calls) == 1
    assert advisor.ADVISOR_HEADER not in local[1]

    advise_evts = [e for e in final["events"] if e["node"] == "advise"]
    assert len(advise_evts) == 1
    assert advise_evts[0]["ignored"] == "escreveu_arquivo"
    assert advise_evts[0]["cost_usd"] == 0.03  # custo saiu mesmo com o texto descartado

    saved = store.get_node(final["run_id"], "advise", data_dir / store.DB_NAME, attempt=1)
    assert saved["ignored"] == "escreveu_arquivo"
    assert saved["text"] == ""
    assert saved["cost_usd"] == 0.03


def test_falha_do_consultor_degrada_para_retry_local(data_dir, tmp_path, local, pago):
    calls, box = pago
    box["backend"] = PagoBackend(calls, boom=True)
    unit = _unit(tmp_path, "explode", VERIFY_CMD)
    final = run_unit(
        unit, "local", None, data_dir, thread_id="t-explode", max_attempts=2, advisor=ADVISOR
    )

    assert final["decision"].action == "escalate_human"
    assert final["attempt"] == 1
    assert len(local) == 2

    advise_evts = [e for e in final["events"] if e["node"] == "advise"]
    assert len(advise_evts) == 1
    assert advise_evts[0]["called"] is False
    assert advise_evts[0]["reason"].startswith("erro:")

    # Hint do reflect (grátis) sobrevive à queda do consultor: continua no
    # segundo prompt, mesmo lugar de sempre.
    assert "Feedback da tentativa anterior" in local[1]
    assert advisor.ADVISOR_HEADER not in local[1]


@pytest.fixture
def sem_pressao_ambiente(tmp_path, monkeypatch):
    """Isola `pressure.cost_cap_usd` — mesmo raciocínio de `test_ceiling.py`."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "governor.toml").write_text("[pressure]\ncost_cap_usd = 0.0\n", encoding="utf-8")
    # A topologia/política com `advise` precisa continuar visível: copia os
    # dois arquivos reais em vez de deixar `config_dir()` cair no fallback
    # embutido (sem `reflect`/`advise`), que tornaria este teste um teste de
    # outra topologia.
    repo_config = Path(__file__).resolve().parent.parent / "config"
    (cfg / "topology.toml").write_text(
        (repo_config / "topology.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (cfg / "graph.toml").write_text(
        (repo_config / "graph.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def test_teto_conta_o_gasto_do_consultor(
    data_dir, tmp_path, local, pago, sem_pressao_ambiente
):
    calls, box = pago
    box["backend"] = PagoBackend(calls, cost_usd=0.03)

    # Unitário: `advise` de `through_attempt + 1` entra na soma.
    db = data_dir / store.DB_NAME
    store.record_node("r-unit", "advise", {"cost_usd": 0.03, "called": True}, db, attempt=1)
    assert ceiling.spent_for_run("r-unit", db, 0) == pytest.approx(0.03)

    # e2e: o consultor gasta mais do que o teto do comando cabe.
    unit = _unit(tmp_path, "estoura", VERIFY_CMD)
    final = run_unit(
        unit, "local", None, data_dir, thread_id="t-estoura", max_attempts=3,
        max_usd=0.02, advisor=ADVISOR,
    )

    execs = [e for e in final["events"] if e["node"] == "execute"]
    assert len(execs) == 2
    assert execs[1]["exit_reason"] == ceiling.BUDGET_EXIT
    assert final["decision"].action == "escalate_human"
    assert ceiling.BREACH_REASON in final["decision"].reason


# --- unitário: helpers puros --------------------------------------------------


def test_should_advise_tabela():
    base = dict(attempt=1, exit_reason="done", verify_passed=False, used=0, cap=1)

    assert advisor.should_advise(armed=False, **base) == (False, "desarmado")
    assert advisor.should_advise(armed=True, **{**base, "attempt": 0}) == (
        False,
        "primeira_tentativa",
    )
    assert advisor.should_advise(armed=True, **{**base, "exit_reason": "blocked"}) == (
        False,
        "exit:blocked",
    )
    assert advisor.should_advise(armed=True, **{**base, "exit_reason": "budget"}) == (
        False,
        "exit:budget",
    )
    assert advisor.should_advise(armed=True, **{**base, "used": 1, "cap": 1}) == (
        False,
        "teto_de_turnos",
    )
    assert advisor.should_advise(armed=True, **{**base, "verify_passed": None}) == (
        False,
        "sem_material",
    )
    assert advisor.should_advise(armed=True, **{**base, "verify_passed": True}) == (
        False,
        "verify_verde",
    )
    assert advisor.should_advise(armed=True, **base) == (True, "verify_vermelho")


def test_topologia_default_tem_advise_depois_do_reflect():
    spec = topology.load_spec()
    edges = [tuple(e) for e in spec["edges"]]
    assert "advise" in spec["nodes"]
    assert ("reflect", "advise") in edges
    assert ("advise", "route") in edges
    assert ("reflect", "route") not in edges
    topology.build(spec)  # compila: default do repo é spec válida
    assert topology_grammar.check(spec) == []


def test_extract_text_formatos(tmp_path):
    dict_path = tmp_path / "dict.json"
    dict_path.write_text(json.dumps({"result": "diagnóstico direto"}), encoding="utf-8")
    assert advisor.extract_text(dict_path) == "diagnóstico direto"

    jsonl_path = tmp_path / "trace.jsonl"
    jsonl_path.write_text(
        '{"type": "start"}\n{"type": "final", "text": "diagnóstico do fim"}\n',
        encoding="utf-8",
    )
    assert advisor.extract_text(jsonl_path) == "diagnóstico do fim"

    raw_path = tmp_path / "raw.txt"
    raw_path.write_text("texto cru, sem json nenhum", encoding="utf-8")
    assert advisor.extract_text(raw_path) == "texto cru, sem json nenhum"

    assert advisor.extract_text(tmp_path / "nao_existe.json") == ""
