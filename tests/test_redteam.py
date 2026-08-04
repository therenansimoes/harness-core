"""Ação redteam: contra-exemplo vira exame de quarentena, ou nada acontece."""

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from harness import cli
from harness.backends import registry
from harness.genome.genome import Genome
from harness.improve import redteam
from harness.improve.mutate import GenomeViolation
from harness.improve.redteam import (
    RedteamError,
    apply_redteam,
    propose_redteam,
)
from harness.types import ExecResult

# Genoma sintético (mesmo motivo do test_research): o teste não depende do
# config/genome.toml do repo para saber que quarantine é mutável.
GENOME_COM_QUARENTENA = Genome(
    immutable=("benchmarks/sealed/**",), mutable=("benchmarks/quarantine/**",)
)
GENOME_SEM_QUARENTENA = Genome(immutable=("benchmarks/sealed/**",), mutable=("config/*.toml",))

SPECS = [
    {
        "id": "diff-minimo-vs-bug-estrutural",
        "kind": "code",
        "prompt": "O bug exige mudar 3 funções; a skill manda diff mínimo.",
        "verify_cmd": "true",
        "attacks": "python-fixes: diff mínimo",
    },
    {
        "id": "verify ambíguo",
        "prompt": "verify_cmd passa sem exercitar o fix.",
        "verify_cmd": "false",
    },
]


class FakeBackend:
    """Backend determinístico: devolve sempre o mesmo texto no workspace."""

    name = "redteam-fake"
    payload = json.dumps(SPECS)
    ok = True

    def capabilities(self):  # pragma: no cover — o redteam não consulta
        raise NotImplementedError

    def preflight(self):  # pragma: no cover — idem
        raise NotImplementedError

    def execute(self, req):
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / "out.json").write_text(self.payload, encoding="utf-8")
        return ExecResult(
            ok=self.ok,
            exit_reason="done" if self.ok else "backend_error",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=("out.json",),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Raiz com as instruções vigentes que o red-team ataca."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "python-fixes.md").write_text(
        '---\nname = "python-fixes"\nkinds = ["code"]\n'
        'description = "diff mínimo"\n---\n\n- Diff mínimo: mude só o que causa o bug.\n',
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "executor.md").write_text(
        "Execute a unidade e rode o verify.\n", encoding="utf-8"
    )
    return tmp_path


def _backend(payload: str | None = None, ok: bool = True) -> str:
    """Registra o fake e devolve o nome; o unregister é do teste."""
    cls = type(
        "Fake",
        (FakeBackend,),
        {"payload": payload if payload is not None else FakeBackend.payload, "ok": ok},
    )
    registry.register(FakeBackend.name, cls)
    return FakeBackend.name


def test_backend_valido_materializa_quarentena_no_formato_do_synthesize(root: Path):
    name = _backend()
    try:
        proposal = propose_redteam(backend=name, root=root)
    finally:
        registry.unregister(name)

    assert proposal is not None
    assert [s.slug for s in proposal.specs] == [
        "diff-minimo-vs-bug-estrutural",
        "verify-ambiguo",  # slug do synthesize: acento é transliterado, formato idêntico
    ]
    assert "skills/python-fixes.md" in proposal.sources
    assert "prompts/executor.md" in proposal.sources

    created = apply_redteam(proposal, root=root, genome=GENOME_COM_QUARENTENA)

    quarantine = root / "benchmarks" / "quarantine"
    assert created == [
        quarantine / "diff-minimo-vs-bug-estrutural",
        quarantine / "verify-ambiguo",
    ]
    # formato do synthesize: mesmos campos, mesmo [origin], mesmo loader
    data = tomllib.loads((created[0] / "unit.toml").read_text(encoding="utf-8"))
    assert data["id"] == "diff-minimo-vs-bug-estrutural"
    assert data["kind"] == "code"
    assert data["verify_cmd"] == "true"
    assert data["origin"] == {"run_id": f"redteam:{name}", "exit_reason": "redteam"}
    assert cli.load_unit(created[0]).id == data["id"]
    # spec sem kind não inventa kind (o synthesize também omite)
    assert "kind" not in tomllib.loads((created[1] / "unit.toml").read_text(encoding="utf-8"))
    # nada foi escrito fora da quarentena
    assert not (root / "benchmarks" / "sealed").exists()


@pytest.mark.parametrize(
    "payload,ok",
    [
        ("isto não é json", True),
        (json.dumps({"nada": 1}), True),
        (json.dumps([{"id": "sem-verify", "prompt": "p"}]), True),
        (json.dumps(SPECS), False),  # backend falhou
    ],
)
def test_backend_invalido_e_noop(root: Path, payload: str, ok: bool):
    name = _backend(payload, ok=ok)
    try:
        assert propose_redteam(backend=name, root=root) is None
    finally:
        registry.unregister(name)
    assert not (root / "benchmarks").exists()


def test_apply_fail_closed_recusa_lote_inteiro(root: Path):
    """Uma spec inválida (ou genoma fechado) não deixa metade do lote no disco."""
    name = _backend()
    try:
        proposal = propose_redteam(backend=name, root=root)
    finally:
        registry.unregister(name)
    assert proposal is not None

    with pytest.raises(GenomeViolation):
        apply_redteam(proposal, root=root, genome=GENOME_SEM_QUARENTENA)
    assert not (root / "benchmarks").exists()

    # spec que passou pelo parse mas não sobrevive à validação estrutural
    quebrada = replace(proposal.specs[0], verify_cmd="   ")
    lote = replace(proposal, specs=(proposal.specs[1], quebrada))
    with pytest.raises(RedteamError):
        apply_redteam(lote, root=root, genome=GENOME_COM_QUARENTENA)
    assert not (root / "benchmarks").exists()


def test_prompt_cita_as_instrucoes_vigentes(root: Path):
    targets, sources = redteam.read_targets(root)
    prompt = redteam.redteam_prompt(targets, n=2)
    assert "Diff mínimo" in prompt and "rode o verify" in prompt
    assert "2 tarefas ADVERSARIAIS" in prompt
    assert sources == ("skills/python-fixes.md", "prompts/executor.md")


def test_sem_instrucao_nao_ha_proposta(tmp_path: Path):
    """Raiz sem skills e sem prompt: não há o que atacar => None, sem backend."""
    assert propose_redteam(backend="inexistente", root=tmp_path) is None
