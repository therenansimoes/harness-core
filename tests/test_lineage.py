"""Linhagem das mutações de código: load, árvore, enrich com ledger, CLI."""

import json

from harness import cli
from harness.improve import lineage
from harness.ledger import store
from harness.types import MutationRow


def _write_jsonl(path, entries):
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def _entries():
    return [
        {"id": "aaaa1111bbbb", "parent_id": None, "target": "x.py", "ts": "t1"},
        {"id": "cccc2222dddd", "parent_id": "aaaa1111bbbb", "target": "x.py", "ts": "t2"},
        {"id": "eeee3333ffff", "parent_id": None, "target": "y.py", "ts": "t3"},
    ]


def _mutation(mid, verdict):
    return MutationRow(
        mutation_id=mid, rule_id="r", verdict=verdict, arm_a="a", arm_b="b",
        applied_at="t", reverted=False, note=None,
    )


def test_load_arquivo_ausente(tmp_path):
    assert lineage.load_lineage(tmp_path / "nao_existe.jsonl") == []


def test_load_linha_torta_pula_com_aviso(tmp_path, capsys):
    p = tmp_path / "lineage.jsonl"
    p.write_text(
        json.dumps(_entries()[0]) + "\n"
        + "{isso nao e json\n"
        + json.dumps({"id": "sem_campos"}) + "\n"
        + json.dumps(_entries()[2]) + "\n",
        encoding="utf-8",
    )
    out = lineage.load_lineage(p)
    assert [e["id"] for e in out] == ["aaaa1111bbbb", "eeee3333ffff"]
    err = capsys.readouterr().err
    assert "2" in err and "inválida" in err


def test_build_tree_aninha_filho_no_parent():
    roots = lineage.build_tree(_entries())
    assert [r["id"] for r in roots] == ["aaaa1111bbbb", "eeee3333ffff"]
    assert [c["id"] for c in roots[0]["children"]] == ["cccc2222dddd"]
    assert roots[1]["children"] == []


def test_build_tree_parent_desconhecido_vira_raiz():
    roots = lineage.build_tree(
        [{"id": "orfao", "parent_id": "sumido", "target": "z.py", "ts": "t"}]
    )
    assert [r["id"] for r in roots] == ["orfao"]


def test_enrich_junta_verdict_do_ledger(tmp_path):
    db = tmp_path / "runs.sqlite"
    store.record_mutation(_mutation("aaaa1111bbbb", "KEEP"), path=db)
    entries = lineage.enrich(_entries(), db_path=db)
    by_id = {e["id"]: e["verdict"] for e in entries}
    assert by_id["aaaa1111bbbb"] == "KEEP"
    assert by_id["cccc2222dddd"] is None


def test_load_mescla_verdict_do_jsonl(tmp_path):
    p = tmp_path / "lineage.jsonl"
    _write_jsonl(
        p,
        _entries()
        + [
            {"id": "aaaa1111bbbb", "verdict": "KEEP", "ts": "t4"},
            {"id": "cccc2222dddd", "verdict": "DISCARD", "ts": "t5"},
        ],
    )
    out = lineage.load_lineage(p)
    assert [e["id"] for e in out] == [e["id"] for e in _entries()]
    by_id = {e["id"]: e.get("verdict") for e in out}
    assert by_id["aaaa1111bbbb"] == "KEEP"
    assert by_id["cccc2222dddd"] == "DISCARD"
    assert by_id["eeee3333ffff"] is None


def test_load_verdict_sem_proposta_ignora_com_aviso(tmp_path, capsys):
    p = tmp_path / "lineage.jsonl"
    _write_jsonl(p, _entries() + [{"id": "fantasma", "verdict": "KEEP", "ts": "t9"}])
    out = lineage.load_lineage(p)
    assert [e["id"] for e in out] == [e["id"] for e in _entries()]
    err = capsys.readouterr().err
    assert "veredito" in err and "sem proposta" in err


def test_enrich_jsonl_tem_precedencia_sobre_ledger(tmp_path):
    db = tmp_path / "runs.sqlite"
    store.record_mutation(_mutation("aaaa1111bbbb", "KEEP"), path=db)
    entries = _entries()
    entries[0]["verdict"] = "DISCARD"  # jsonl diz DISCARD, ledger diz KEEP
    out = lineage.enrich(entries, db_path=db)
    assert out[0]["verdict"] == "DISCARD"


def test_render_verdict_do_jsonl_e_sem_marca_na_antiga(tmp_path):
    p = tmp_path / "lineage.jsonl"
    _write_jsonl(
        p,
        _entries()
        + [
            {"id": "aaaa1111bbbb", "verdict": "KEEP", "ts": "t4"},
            {"id": "cccc2222dddd", "verdict": "DISCARD", "ts": "t5"},
        ],
    )
    tree = lineage.build_tree(
        lineage.enrich(lineage.load_lineage(p), db_path=tmp_path / "nada.sqlite")
    )
    out = lineage.render(tree)
    assert "[KEEP]" in out and "[DISCARD]" in out
    assert "eeee3333" in out and "[?]" in out  # linhagem antiga sem verdict


def test_enrich_sem_db_tudo_none(tmp_path):
    entries = lineage.enrich(_entries(), db_path=tmp_path / "nada.sqlite")
    assert all(e["verdict"] is None for e in entries)
    assert not (tmp_path / "nada.sqlite").exists()


def test_render_tem_ids_verdicts_e_indenta(tmp_path):
    db = tmp_path / "runs.sqlite"
    store.record_mutation(_mutation("aaaa1111bbbb", "KEEP"), path=db)
    store.record_mutation(_mutation("cccc2222dddd", "DISCARD"), path=db)
    tree = lineage.build_tree(lineage.enrich(_entries(), db_path=db))
    out = lineage.render(tree)
    assert "aaaa1111" in out and "cccc2222" in out and "eeee3333" in out
    assert "[KEEP]" in out and "[DISCARD]" in out and "[?]" in out
    assert "  └─ cccc2222" in out  # filho indentado sob a raiz


def test_cli_lineage_imprime_arvore(tmp_path, capsys):
    f = tmp_path / "lineage.jsonl"
    _write_jsonl(f, _entries())
    db = tmp_path / "runs.sqlite"
    store.record_mutation(_mutation("cccc2222dddd", "KEEP"), path=db)
    rc = cli.main(["lineage", "--file", str(f), "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaaa1111" in out and "[KEEP]" in out


def test_cli_lineage_limit_corta_raizes_antigas(tmp_path, capsys):
    f = tmp_path / "lineage.jsonl"
    _write_jsonl(f, _entries())
    rc = cli.main(["lineage", "--file", str(f), "--limit", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "eeee3333" in out and "aaaa1111" not in out


def test_cli_lineage_vazio_mensagem_clara(tmp_path, capsys):
    rc = cli.main(["lineage", "--file", str(tmp_path / "nada.jsonl")])
    assert rc == 0
    assert "sem linhagem" in capsys.readouterr().out
