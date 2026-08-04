"""Sono do harness: recorrente funde em skill, órfão sai do recall, nada morre.

O relógio é injetado em toda parte — "mais velho que 7 dias" é a decisão inteira
da ação, e teste que depende do `datetime.now()` do processo só falha em
setembro.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.genome.genome import Genome
from harness.improve import dream
from harness.memory import episodic
from harness.triggers import watch

# Genoma sintético (mesmo motivo do test_research/test_redteam): o teste não
# depende do config/genome.toml do repo para saber que skills é mutável.
GENOME = Genome(immutable=("benchmarks/sealed/**",), mutable=("skills/**",))

# Três traces que só diferem em número: a assinatura tem que colidir, senão
# "recorrente" nunca acontece na prática.
RECORRENTES = [
    "AssertionError: expected 2 got 3 in check_total at line 10",
    "AssertionError: expected 5 got 8 in check_total at line 23",
    "AssertionError: expected 1 got 9 in check_total at line 77",
]
ORFAO = "TimeoutError: backend nunca respondeu ao handshake"


def _traceback(erro: str, linha: int = 3) -> str:
    """Traceback Python de verdade: o boilerplate é idêntico em toda falha de
    assert, e é exatamente ele que fundia lições diferentes numa assinatura só."""
    return (
        "Traceback (most recent call last):\n"
        f'  File "<stdin>", line {linha}, in <module>\n'
        "    assert cond, msg\n"
        f"{erro}"
    )


NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    """Data dir global isolado: a episódica é keyed em HARNESS_DATA_DIR."""
    data = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data))
    return data / "runs.sqlite"


def _record(db: Path, traces, kind: str = "code", unit: str = "u1") -> None:
    for i, trace in enumerate(traces):
        assert episodic.record_failure(kind, f"{unit}-{i}", trace, db_path=db)


def test_recorrentes_viram_licao_e_skill(db, tmp_path):
    """3 episódios com a mesma assinatura => 1 lição e 1 skill candidata."""
    _record(db, RECORRENTES)
    proposal = dream.propose_dream(now=NOW, db_path=db)

    assert proposal is not None
    assert len(proposal.lessons) == 1
    lesson = proposal.lessons[0]
    assert (lesson.kind, lesson.n) == ("code", 3)
    assert lesson.episode_ids == (1, 2, 3)
    assert proposal.orphans == ()  # recorrente não é órfão
    assert proposal.skill is not None
    assert proposal.skill.target_file.startswith("skills/dream-code-")
    assert "3 episódios" in proposal.dream_report

    record = dream.apply_dream(
        proposal,
        root=tmp_path,
        genome=GENOME,
        data_dir=tmp_path / "data",
        db_path=db,
        now=NOW,
    )
    skill = tmp_path / record.skill_path
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n") and 'kinds = ["code"]' in text
    assert "check_total" in text
    report = Path(record.report_path)
    assert report.name == "20260803T120000Z.md"
    assert report.read_text(encoding="utf-8") == proposal.dream_report
    assert record.archived == 0  # não havia órfão: nada foi escondido


def test_boilerplate_de_traceback_nao_funde_asserts_diferentes():
    """Dois asserts diferentes com o MESMO boilerplate => duas assinaturas."""
    persiste = _traceback("AssertionError: tema nao persiste apos reload", linha=3)
    botao = _traceback("AssertionError: botao de tema nao alterna o atributo", linha=17)

    sig_persiste = dream.signature(persiste)
    sig_botao = dream.signature(botao)
    assert sig_persiste != sig_botao
    # A assinatura é do erro real, não do frame: nenhuma palavra de traceback.
    for sig in (sig_persiste, sig_botao):
        assert not (set(sig.split()) & dream._SIG_STOPWORDS), sig
    assert sig_persiste.startswith("assertionerror tema")

    # Mesmo assert com outros números continua colidindo (senão nada é recorrente).
    outro = _traceback("AssertionError: tema nao persiste apos reload", linha=42)
    assert dream.signature(outro) == sig_persiste
    assert dream.signature(persiste) == sig_persiste  # determinístico


def test_orfaos_arquivados_somem_do_recall(db, tmp_path):
    """Órfão velho sai do recall no apply — e continua no banco (soft)."""
    _record(db, [ORFAO])
    assert episodic.recall("code", "TimeoutError handshake", db_path=db) == [ORFAO]

    # 8 dias depois o episódio único é órfão; ontem ele não seria.
    later = NOW + timedelta(days=8)
    assert dream.propose_dream(now=NOW, db_path=db) is None
    proposal = dream.propose_dream(now=later, db_path=db)
    assert proposal is not None
    assert [o.id for o in proposal.orphans] == [1]
    assert proposal.skill is None  # sem recorrente não há skill

    record = dream.apply_dream(
        proposal,
        root=tmp_path,
        genome=GENOME,
        data_dir=tmp_path / "data",
        db_path=db,
        now=later,
    )
    assert (record.archived, record.skill_path) == (1, "")
    assert episodic.recall("code", "TimeoutError handshake", db_path=db) == []
    assert episodic.archived_ids(db_path=db) == [1]
    # NUNCA DELETE: a linha do índice continua lá, só invisível ao recall.
    assert len(episodic.episodes(db_path=db)) == 0
    import sqlite3

    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT count(*) FROM {episodic.TABLE}").fetchone()[0] == 1


def test_banco_vazio_nao_sonha(db):
    """Sem episódio não há consolidação: None, não sonho vazio."""
    assert dream.propose_dream(now=NOW, db_path=db) is None
    with pytest.raises(dream.DreamError):
        dream.apply_dream(dream.DreamProposal(dream_report="x"))


def test_should_dream_precisa_das_duas_condicoes(tmp_path):
    """Tempo E runs. Faltando qualquer um dos dois, não sonha."""
    from harness.ledger import store
    from harness.types import RunRow

    db = tmp_path / "runs.sqlite"
    dreams = tmp_path / "dreams"
    dreams.mkdir()

    def add_runs(n: int, at: datetime) -> None:
        for i in range(n):
            store.record_run(
                RunRow(
                    run_id=f"r-{at.timestamp()}-{i}",
                    unit_id="u",
                    project=None,
                    backend="mock",
                    model=None,
                    tier=None,
                    kind="code",
                    ok=True,
                    exit_reason="ok",
                    sec_total=1.0,
                    sec_provision=0.0,
                    cost_usd=None,
                    intervention=False,
                    created_at=at.isoformat(timespec="seconds"),
                ),
                path=db,
            )

    def dreamed_at(at: datetime) -> None:
        report = dreams / "last.md"
        report.write_text("# dream", encoding="utf-8")
        import os

        os.utime(report, (at.timestamp(), at.timestamp()))

    dreamed_at(NOW - timedelta(hours=48))
    add_runs(4, NOW - timedelta(hours=1))

    # horas ok, runs de menos
    assert watch.should_dream(db, NOW, dreams_dir=dreams) is False
    add_runs(1, NOW - timedelta(hours=1))
    # horas ok, runs ok
    assert watch.should_dream(db, NOW, dreams_dir=dreams) is True

    dreamed_at(NOW - timedelta(hours=2))
    # horas de menos, runs ok (as runs são anteriores ao sono, e nem contam)
    assert watch.should_dream(db, NOW, dreams_dir=dreams) is False
    # horas de menos, runs de menos
    assert watch.should_dream(db, NOW, min_runs=99, dreams_dir=dreams) is False
