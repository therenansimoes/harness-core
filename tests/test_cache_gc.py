"""Teto LRU do cache de dependência: frio sai, quente (<24h) fica."""

import os
import time

import pytest

from harness.workspace import cache_gc


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _arquivo(root, rel: str, size: int, dias: float) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    quando = time.time() - dias * 86400
    os.utime(p, (quando, quando))


def test_sem_cache_nao_faz_nada(data):
    r = cache_gc.gc(max_gb=1 / cache_gc.GB, data=data)
    assert r == {
        "before": 0, "after": 0, "removed": 0, "freed": 0,
        "skipped_recent": 0, "max_bytes": 1,
    }


def test_abaixo_do_teto_e_noop(data):
    _arquivo(data / "cache", "uv/a/pkg.tar", 1000, dias=10)
    r = cache_gc.gc(max_gb=1, data=data)
    assert r["removed"] == 0 and r["before"] == r["after"] == 1000
    assert (data / "cache" / "uv" / "a" / "pkg.tar").exists()


def test_remove_mais_frio_primeiro_ate_caber(data):
    root = data / "cache"
    _arquivo(root, "uv/velho.tar", 600, dias=30)
    _arquivo(root, "npm/medio.tar", 600, dias=10)
    _arquivo(root, "uv/novo-mas-frio.tar", 600, dias=2)
    max_gb = 1300 / cache_gc.GB  # cabem dois dos três

    r = cache_gc.gc(max_gb=max_gb, data=root.parent)

    assert r["removed"] == 1
    assert r["freed"] == 600
    assert not (root / "uv" / "velho.tar").exists()
    assert (root / "npm" / "medio.tar").exists()
    assert (root / "uv" / "novo-mas-frio.tar").exists()
    assert r["after"] <= r["max_bytes"]


def test_tocado_nas_ultimas_24h_nunca_sai(data):
    root = data / "cache"
    _arquivo(root, "uv/quente.tar", 5000, dias=0.1)
    r = cache_gc.gc(max_gb=1 / cache_gc.GB, data=data)
    assert r["removed"] == 0
    assert r["skipped_recent"] == 1
    assert r["after"] > r["max_bytes"]  # reporta que não coube, não apaga
    assert (root / "uv" / "quente.tar").exists()


def test_dir_alheio_no_cache_e_intocado(data):
    root = data / "cache"
    _arquivo(root, "uv/frio.tar", 900, dias=40)
    _arquivo(root, "outro/nao-e-nosso.tar", 900, dias=40)

    r = cache_gc.gc(max_gb=1 / cache_gc.GB, data=data)

    assert r["before"] == 900  # só o gerenciado é contado
    assert not (root / "uv" / "frio.tar").exists()
    assert (root / "outro" / "nao-e-nosso.tar").exists()


def test_dir_vazio_e_podado(data):
    root = data / "cache"
    _arquivo(root, "uv/a/b/c/pkg.tar", 900, dias=40)
    cache_gc.gc(max_gb=1 / cache_gc.GB, data=data)
    assert (root / "uv").is_dir()  # a raiz gerenciada fica
    assert not (root / "uv" / "a").exists()


def test_usage_conta_arquivos(data):
    root = data / "cache"
    _arquivo(root, "uv/a.tar", 100, dias=1)
    _arquivo(root, "npm/b.tar", 250, dias=1)
    assert cache_gc.usage(data) == (350, 2)


def test_doctor_avisa_acima_do_teto(data, monkeypatch):
    from harness import doctor

    _arquivo(data / "cache", "uv/grande.tar", 4096, dias=5)
    monkeypatch.setattr(cache_gc, "DEFAULT_MAX_GB", 1024 / cache_gc.GB)
    c = doctor._cache(data)
    assert c.status == doctor.WARN and "cache-gc" in c.detail

    monkeypatch.setattr(cache_gc, "DEFAULT_MAX_GB", 1.0)
    assert doctor._cache(data).status == doctor.OK
