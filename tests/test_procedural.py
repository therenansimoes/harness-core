"""Mineração procedural: corpus SINTÉTICO de traces no disco, lift decidindo.

Corpus sintético e não fixture de run real pelo motivo de sempre: o sinal aqui é
a DIFERENÇA entre o corpus aceito e o falho, e trace real não deixa escolher os
dois lados. Cada teste escreve os jsonl com a própria mão e o ledger aponta para
eles pelo `run_id`.
"""

import json
from pathlib import Path

import pytest

from harness.genome.genome import Genome
from harness.improve import procedural
from harness.ledger import store
from harness.types import RunRow

# Genoma sintético (mesmo motivo do test_dream): o teste não depende do
# config/genome.toml do repo para saber que skills é mutável.
GENOME = Genome(immutable=("benchmarks/sealed/**",), mutable=("skills/**",))

# O procedimento que a mineração tem que achar. Nomes de tool, sem argumento.
VENCEDOR = ["ls", "read_file", "edit_range"]
# Caminho dos falhos: mesmas ferramentas de leitura, ordem que não conclui nada.
PERDEDOR = ["ls", "grep", "ls", "grep"]


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    """Data dir global isolado: logs e ledger são keyed em HARNESS_DATA_DIR."""
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _trace(data: Path, run_id: str, tools, attempt: int = 1) -> Path:
    """Um `trace.<attempt>.jsonl` no formato do `_write_trace` do deepagents:
    uma linha `ai` com `tool_calls`, uma linha `tool` com a saída."""
    run_dir = data / "logs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"trace.{attempt}.jsonl"
    lines = []
    for tool in tools:
        lines.append(json.dumps({"type": "ai", "content": "[tool_calls]", "tool_calls": [tool]}))
        lines.append(json.dumps({"type": "tool", "content": f"saída de {tool}"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(data: Path, run_id: str, tools, ok: bool) -> None:
    """Uma linha de ledger mais o trace dela: o join é o `run_id`."""
    _trace(data, run_id, tools)
    store.record_run(
        RunRow(
            run_id=run_id,
            unit_id=run_id,
            project=None,
            backend="mock",
            model=None,
            tier="t0",
            kind="code",
            ok=ok,
            exit_reason="ok" if ok else "verify_failed",
            sec_total=1.0,
            sec_provision=0.0,
            cost_usd=0.0,
            intervention=False,
            created_at=store.now_iso(),
        ),
        path=data / "runs.sqlite",
    )


def _corpus(data: Path, aceitos, falhos) -> None:
    for i, tools in enumerate(aceitos):
        _run(data, f"ok-{i}", tools, ok=True)
    for i, tools in enumerate(falhos):
        _run(data, f"bad-{i}", tools, ok=False)


def test_padrao_dos_aceitos_vira_skill(data, tmp_path):
    """3 aceitos com ls→read_file→edit_range, ausente nos falhos => skill."""
    _corpus(data, [VENCEDOR] * 3, [PERDEDOR] * 3)

    proposal = procedural.propose_procedural()
    assert proposal is not None
    pattern = proposal.pattern
    assert pattern.tools == ("ls", "read_file", "edit_range")  # o mais longo ganha o empate
    assert (pattern.support, pattern.fail_support) == (3, 0)
    assert pattern.lift >= procedural.MIN_LIFT
    assert pattern.run_ids == ("ok-0", "ok-1", "ok-2")  # paper trail do placar
    assert proposal.skill.target_file.startswith("skills/proc-code-")

    record = procedural.apply_procedural(proposal, root=tmp_path, genome=GENOME)
    text = (tmp_path / record.skill_path).read_text(encoding="utf-8")
    assert text.startswith("---\n") and 'kinds = ["code"]' in text
    assert "`ls`" in text and "`edit_range`" in text
    assert "de 3 runs ACEITOS" in text  # a evidência está escrita no corpo
    assert (record.support, record.kind) == (3, "code")


def test_ngrama_comum_aos_falhos_morre_no_lift(data):
    """Mesmo n-grama nos dois corpora não ensina nada: lift 1.0, sem proposta."""
    _corpus(data, [VENCEDOR] * 3, [VENCEDOR] * 3)

    assert procedural.propose_procedural() is None
    found, scanned = procedural.mine()
    assert found == [] and scanned == 6  # leu tudo e não achou sinal


def test_frequencia_alta_sem_lift_perde_do_padrao_raro(data):
    """`ls→grep` aparece em TODO run; o vencedor é o que separa os corpora."""
    _corpus(data, [["ls", "grep", "read_file", "edit_range"]] * 3, [["ls", "grep", "ls"]] * 3)

    found, _ = procedural.mine()
    assert found, "nenhum padrão passou o lift"
    assert ("ls", "grep") not in [p.tools for p in found]
    assert found[0].tools[-1] == "edit_range"


def test_trace_corrompido_e_ignorado_linha_a_linha(data, tmp_path):
    """Linha inválida, json não-dict e arquivo truncado: ignora a linha e segue."""
    _corpus(data, [VENCEDOR] * 3, [PERDEDOR] * 3)
    trace = data / "logs" / "ok-0" / "trace.1.jsonl"
    good = trace.read_text(encoding="utf-8").splitlines()
    trace.write_text(
        "\n".join(["{lixo nao json", "[1, 2, 3]", "", *good, '{"type": "ai", "content"']) + "\n",
        encoding="utf-8",
    )

    assert procedural.tool_sequence("ok-0") == tuple(VENCEDOR)  # nada perdido, nada levantado
    proposal = procedural.propose_procedural()
    assert proposal is not None
    assert proposal.pattern.support == 3


def test_menos_de_tres_aceitos_nao_propoe(data):
    """Suporte 2 é coincidência (ou o mesmo run em duas tentativas): None."""
    _corpus(data, [VENCEDOR] * 2, [PERDEDOR] * 3)

    assert procedural.propose_procedural() is None


def test_run_sem_trace_nao_conta_como_evidencia(data):
    """Ledger com run e disco sem trace: o run não entra em nenhum corpus."""
    _corpus(data, [VENCEDOR] * 3, [PERDEDOR] * 3)
    store.record_run(
        RunRow(
            run_id="ok-fantasma",
            unit_id="u",
            project=None,
            backend="mock",
            model=None,
            tier="t0",
            kind="code",
            ok=True,
            exit_reason="ok",
            sec_total=1.0,
            sec_provision=0.0,
            cost_usd=0.0,
            intervention=False,
            created_at=store.now_iso(),
        ),
        path=data / "runs.sqlite",
    )

    found, scanned = procedural.mine()
    assert scanned == 6  # o fantasma não diluiu o denominador
    assert found[0].ok_runs == 3


def test_tentativas_do_mesmo_run_sao_um_procedimento(data):
    """`trace.1` e `trace.2` são o mesmo caminho até o veredito, na ordem."""
    _trace(data, "ok-multi", ["ls", "read_file"], attempt=1)
    _trace(data, "ok-multi", ["edit_range"], attempt=2)

    assert procedural.tool_sequence("ok-multi") == ("ls", "read_file", "edit_range")


def test_apply_recusa_fora_do_genoma(data, tmp_path):
    """Skill fora da zona mutável é GenomeViolation antes de tocar o disco."""
    from harness.improve import mutate

    _corpus(data, [VENCEDOR] * 3, [PERDEDOR] * 3)
    proposal = procedural.propose_procedural()
    assert proposal is not None
    with pytest.raises(mutate.GenomeViolation):
        procedural.apply_procedural(
            proposal, root=tmp_path, genome=Genome(immutable=("skills/**",), mutable=())
        )
    assert not (tmp_path / proposal.target_file).exists()
