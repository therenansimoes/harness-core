"""D4: a falha real vira proposta de caso, e só o humano a sela.

Tudo roda sob um home isolado: `$HARNESS_EVALS_DIR` aponta o bundle para o
tmpdir e `$HARNESS_DATA_DIR` leva ledger, episódica e a fila de pendentes junto.
Sem as duas, `mine` leria o `data/` do checkout e `seal_case` recongelaria o
`evals/` versionado do repo — que é exatamente o que o eval-freeze existe para
impedir.
"""

from __future__ import annotations

import json

import pytest

from harness import cli, paths
from harness.evals import verify_frozen
from harness.evals.bundle import bundle_dir, load_cases
from harness.evals.freeze import freeze, load_manifest
from harness.evals.mining import (
    CaseProposal,
    drop_pending,
    load_pending,
    mine,
    pending_path,
    seal_case,
    write_pending,
)
from harness.ledger import store
from harness.memory import episodic
from harness.types import RunRow

ARTIFACT = "skills/x.md"
CASES = '{"id":"a-1","kind":"code_fix","prompt":"p1","weight":1.0,"trials":4}\n'


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """Artefato + bundle congelado, com as duas envs apontando para o tmpdir."""
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    art = tmp_path / ARTIFACT
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("# skill\n", encoding="utf-8")
    d = bundle_dir(ARTIFACT, tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(CASES, encoding="utf-8")
    freeze(ARTIFACT, note="v1")
    return tmp_path


def _run(unit_id: str, *, ok: bool, exit_reason: str, kind: str = "code") -> None:
    store.record_run(
        RunRow(
            run_id=f"r-{unit_id}",
            unit_id=unit_id,
            project=None,
            backend="mock",
            model=None,
            tier="local",
            kind=kind,
            ok=ok,
            exit_reason=exit_reason,
            sec_total=1.0,
            sec_provision=0.0,
            cost_usd=None,
            intervention=False,
            created_at=store.now_iso(),
        )
    )


def test_mine_ignora_runs_ok(tree):
    _run("u-ok", ok=True, exit_reason="done")
    _run("u-mau", ok=False, exit_reason="timeout")

    props = mine(ARTIFACT)

    assert [(p.source, p.case["expect"]["must_not_mention"]) for p in props] == [
        ("ledger", ["timeout"])
    ]
    assert props[0].case_id == "mined-timeout"
    assert props[0].case["axes"] == ["safety", "grounding"]


def test_mine_dedup_contra_cases_existentes(tree):
    (bundle_dir(ARTIFACT, tree) / "cases.jsonl").write_text(
        CASES
        + '{"id":"a-2","kind":"code","prompt":"p2","expect":{"must_not_mention":["Timeout"]}}\n',
        encoding="utf-8",
    )
    _run("u-1", ok=False, exit_reason="timeout")
    _run("u-2", ok=False, exit_reason="blocked")

    props = mine(ARTIFACT)

    # `Timeout` do caso já selado cobre o `timeout` do ledger: a classe é a
    # mesma, só a caixa muda.
    assert [p.case["expect"]["must_not_mention"][0] for p in props] == ["blocked"]


def test_mine_usa_episodios_quando_ledger_vazio(tree):
    episodic.record_failure(
        "code", "u-9", "Traceback (most recent call last)\nZeroDivisionError: x"
    )

    props = mine(ARTIFACT)

    assert [(p.source, p.case_id) for p in props] == [("episodic", "mined-zerodivisionerror")]
    assert props[0].source_id
    assert "ZeroDivisionError" in props[0].case["prompt"]


def test_write_pending_idempotente(tree):
    _run("u-1", ok=False, exit_reason="timeout")
    props = mine(ARTIFACT)

    path = write_pending(ARTIFACT, props)
    write_pending(ARTIFACT, props)

    assert path == pending_path(ARTIFACT)
    assert [p.case_id for p in load_pending(ARTIFACT)] == ["mined-timeout"]
    assert drop_pending(ARTIFACT, "mined-timeout") is True
    assert load_pending(ARTIFACT) == []
    assert drop_pending(ARTIFACT, "mined-timeout") is False


def test_eixos_saem_da_classe_da_falha(tree):
    _run("u-v", ok=False, exit_reason="verify_failed")
    _run("u-t", ok=False, exit_reason="max_turns")
    _run("u-e", ok=False, exit_reason="error")
    _run("u-b", ok=False, exit_reason="blocked")
    _run("u-x", ok=False, exit_reason="timeout")

    eixos = {p.case_id: p.case["axes"] for p in mine(ARTIFACT)}

    # `verify` sai do caso mesmo sendo o eixo da classe: `score` levanta contra
    # `verify` sem `verify_cmd`, e caso minerado nasce sem comando.
    assert eixos == {
        "mined-verify-failed": ["coverage"],
        "mined-max-turns": ["coverage", "structure"],
        "mined-error": ["grounding"],
        "mined-blocked": ["safety"],
        "mined-timeout": ["safety", "grounding"],  # classe sem regra: fallback
    }


def test_eixos_de_episodio_com_assercao_e_com_recusa(tree):
    episodic.record_failure(
        "code", "u-9", "Traceback (most recent call last)\nAssertionError: esperava 2, veio 3"
    )
    episodic.record_failure("code", "u-8", "tool call failed: permission denied ao gravar /etc")

    props = {p.case_id: p.case["axes"] for p in mine(ARTIFACT)}

    assert props["mined-assertionerror"] == ["grounding"]
    assert props["mined-tool-call-failed-permission-denied-ao-gravar-etc"] == ["safety"]


def test_write_pending_atualiza_rationale_do_caso_ja_na_fila(tree):
    _run("u-1", ok=False, exit_reason="timeout")
    write_pending(ARTIFACT, mine(ARTIFACT))
    _run("u-2", ok=False, exit_reason="timeout")

    path = write_pending(ARTIFACT, mine(ARTIFACT))

    fila = load_pending(ARTIFACT)
    assert [p.case_id for p in fila] == ["mined-timeout"]
    # A dor cresceu de 1 para 2 runs: quem revisa decide pelo tamanho dela, e o
    # disco tem que contar o mesmo que a CLI acabou de imprimir.
    assert fila[0].rationale == "2 run(s) falharam com exit_reason=timeout"
    linhas = path.read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 1
    linha = json.loads(linhas[0])
    assert linha["rationale"] == "2 run(s) falharam com exit_reason=timeout"
    # O caso e a procedência não se mexem: mudar o que o exame cobra por baixo
    # de quem já leu a proposta é outra proposta, com outro id.
    assert linha["source_id"] == "r-u-1"
    assert "u-1" in linha["case"]["prompt"]


def test_seal_sem_yes_falha(tree, monkeypatch, capsys):
    monkeypatch.chdir(tree)
    _run("u-1", ok=False, exit_reason="timeout")
    write_pending(ARTIFACT, mine(ARTIFACT))

    assert cli.main(["eval", "seal-case", ARTIFACT, "mined-timeout"]) == 2

    assert "selar é ato humano: use --yes" in capsys.readouterr().err
    assert [c.id for c in load_cases(ARTIFACT)] == ["a-1"]
    assert [p.case_id for p in load_pending(ARTIFACT)] == ["mined-timeout"]
    assert verify_frozen(ARTIFACT) == []


def test_seal_move_e_refreeze(tree):
    _run("u-1", ok=False, exit_reason="timeout")
    write_pending(ARTIFACT, mine(ARTIFACT))

    m = seal_case(ARTIFACT, "mined-timeout")

    assert (m.version, m.case_count) == (2, 2)
    assert [c.id for c in load_cases(ARTIFACT)] == ["a-1", "mined-timeout"]
    assert verify_frozen(ARTIFACT) == []
    assert load_pending(ARTIFACT) == []


def test_seal_registra_note_no_history(tree):
    _run("u-1", ok=False, exit_reason="timeout")
    _run("u-2", ok=False, exit_reason="blocked")
    write_pending(ARTIFACT, mine(ARTIFACT))

    v2 = seal_case(ARTIFACT, "mined-timeout")
    v3 = seal_case(ARTIFACT, "mined-blocked")

    # O manifest é o registro autoritativo do selo: a nota da v2 vira linha de
    # histórico quando a v3 nasce, e a procedência viaja dentro dela.
    assert v2.note == "sealed-case:mined-timeout src:ledger:r-u-1"
    assert [h["note"] for h in v3.history] == ["v1", v2.note]
    assert v3.note.startswith("sealed-case:mined-blocked src:ledger:")
    assert load_manifest(ARTIFACT).history == v3.history


def test_seal_grava_mutation(tree):
    _run("u-1", ok=False, exit_reason="timeout")
    write_pending(ARTIFACT, mine(ARTIFACT))

    m = seal_case(ARTIFACT, "mined-timeout", note="revisado por Renan")

    rows = store.mutations(rule_id=f"eval:{ARTIFACT}")
    assert [(r.action, r.arm_a, r.arm_b, r.verdict, r.note) for r in rows] == [
        ("seal-case", "mined-timeout", f"v{m.version}", "sealed", "revisado por Renan")
    ]


def test_case_invalido_nao_e_selado(tree):
    torto = CaseProposal(
        case_id="mined-torto",
        artifact=ARTIFACT,
        source="ledger",
        source_id="r-x",
        # `trial` no lugar de `trials`: o exame passaria de 4 tentativas para 1
        # sem ninguém ver. Campo desconhecido não entra no bundle.
        case={"id": "mined-torto", "kind": "code", "prompt": "p", "trial": 4},
        rationale="proposta torta",
        proposed_at="2026-08-04T00:00:00Z",
    )
    write_pending(ARTIFACT, [torto])

    with pytest.raises(ValueError):
        seal_case(ARTIFACT, "mined-torto")

    assert [c.id for c in load_cases(ARTIFACT)] == ["a-1"]
    assert verify_frozen(ARTIFACT) == []
    assert load_manifest(ARTIFACT).version == 1
    assert [p.case_id for p in load_pending(ARTIFACT)] == ["mined-torto"]


def test_deliverable_d4_ponta_a_ponta(tree):
    _run("u-1", ok=False, exit_reason="timeout")

    props = mine(ARTIFACT, limit=5, kind="code")
    write_pending(ARTIFACT, props)
    m = seal_case(ARTIFACT, props[0].case_id)

    selado = [c for c in load_cases(ARTIFACT) if c.id == props[0].case_id]
    assert len(selado) == 1
    assert selado[0].expect == {"must_not_mention": ["timeout"]}
    assert selado[0].trials == 4
    assert m.version == 2 and verify_frozen(ARTIFACT) == []
    # A linha do bundle é JSON legível, não repr de dataclass.
    linha = (
        (bundle_dir(ARTIFACT, tree) / "cases.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert json.loads(linha)["id"] == props[0].case_id
