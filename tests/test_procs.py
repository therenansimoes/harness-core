"""Processos de vida longa: servidor REAL, porta real, morte real.

Nada aqui é mockado de propósito — o valor deste módulo está em três coisas que
mock não prova: a porta abre e responde, `killpg` alcança o FILHO do processo
que subimos (o caso `npm run dev`, que é wrapper), e o cleanup nunca levanta.

O servidor de teste é um `sh -c '... & wait'`: o `&` força o shell a forkar, o
que reproduz a árvore pai+filho. Matar só o pai deixaria o `http.server` vivo
segurando a porta.
"""

import json
import os
import subprocess
import time

import pytest

from harness.backends import procs

SERVER = 'sh -c "python3 -m http.server $PORT --bind 127.0.0.1 & wait"'


def _pids_do_grupo(pgid: int) -> list[int]:
    proc = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
    return [int(line) for line in proc.stdout.split() if line.strip()]


def _morreu(pid: int, prazo: float = 5.0) -> bool:
    """`os.kill(pid, 0)` levanta — com prazo: o neto é colhido pelo init, não
    por nós, e isso não é instantâneo."""
    fim = time.time() + prazo
    while time.time() < fim:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "index.html").write_text("<h1>vivo</h1>\n", encoding="utf-8")
    yield tmp_path
    procs.kill_all(tmp_path)


def test_alloc_port_devolve_porta_livre():
    porta = procs.alloc_port()

    assert 1024 < porta < 65536
    # livre de verdade: dá para bindar nela agora
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", porta))


def test_start_sobe_servidor_real_e_probe_responde_200(ws):
    info = procs.start(ws, SERVER, wait_path="/", timeout=20)

    assert info["status"] == "ready", info
    assert info["port"] and info["run_id"] == ws.name
    # o registro está no disco, com o pid e o pgid de quem subiu
    registro = json.loads(procs.procs_path(ws).read_text(encoding="utf-8"))
    assert [e["id"] for e in registro] == [info["id"]]
    assert registro[0]["harness_pid"] == os.getpid()

    saida = procs.local_probe(ws, info["port"], "/")

    assert "-> 200" in saida
    assert "vivo" in saida  # é o index.html DESTE workspace, não outro run


def test_kill_all_mata_pai_e_filho(ws):
    """O caso `npm run dev`: matar só o pai deixa o servidor real órfão."""
    info = procs.start(ws, SERVER, timeout=20)
    assert info["status"] == "ready", info
    grupo = _pids_do_grupo(info["pgid"])
    assert len(grupo) >= 2, f"o teste precisa de pai+filho, veio {grupo}"

    mortos = procs.kill_all(ws)

    assert mortos == 1
    for pid in grupo:
        assert _morreu(pid), f"pid {pid} sobreviveu ao kill_all"
    assert procs.read_procs(ws) == []


def test_probe_recusa_porta_nao_registrada(ws):
    info = procs.start(ws, SERVER, timeout=20)
    assert info["status"] == "ready", info

    saida = procs.local_probe(ws, int(info["port"]) + 1, "/")

    assert "recusado" in saida and "não está registrada" in saida


def test_probe_recusa_porta_de_outro_workspace(ws, tmp_path):
    """Porta viva, servidor vivo, workspace errado: a cerca é POR run."""
    info = procs.start(ws, SERVER, timeout=20)
    assert info["status"] == "ready", info
    outro = tmp_path / "outro-ws"
    outro.mkdir()

    assert "recusado" in procs.local_probe(outro, info["port"], "/")


def test_start_de_comando_que_morre_volta_crashed_com_log(ws):
    info = procs.start(ws, 'sh -c "echo boom-no-boot >&2; exit 3"', timeout=10)

    assert info["status"] == "crashed"
    assert info["exit_code"] == 3
    assert "boom-no-boot" in info["log_tail"]
    # crashed não fica no registro: cleanup não tem o que matar
    assert procs.read_procs(ws) == []


def test_start_recusa_comando_bloqueado_pela_cerca(ws):
    info = procs.start(ws, "sudo npm run dev")

    assert info["status"] == "blocked"
    assert "sudo" in info["reason"]


def test_procs_json_corrompido_fail_open(ws):
    (ws / ".harness").mkdir(exist_ok=True)
    procs.procs_path(ws).write_text("{isto não é json}", encoding="utf-8")

    assert procs.read_procs(ws) == []
    assert procs.kill_all(ws) == 0  # nunca levanta


def test_kill_all_sem_registro_e_zero(tmp_path):
    assert procs.kill_all(tmp_path / "nem-existe") == 0


def test_stop_ignora_id_desconhecido(ws):
    info = procs.start(ws, SERVER, timeout=20)
    assert info["status"] == "ready", info

    assert procs.stop(ws, "nao-existe") == 0
    assert [e["id"] for e in procs.read_procs(ws)] == [info["id"]]  # não apagou o vivo
    assert procs.stop(ws, info["id"]) == 1
    assert procs.read_procs(ws) == []


def test_doctor_ve_orfao_como_aviso(ws, monkeypatch, tmp_path):
    """Registro com harness_pid morto = servidor sem dono. AVISO, nunca FALHA."""
    from harness import doctor

    data = tmp_path / "data"
    run_ws = data / "ws" / "run-morto"
    (run_ws / ".harness").mkdir(parents=True)
    procs.procs_path(run_ws).write_text(
        json.dumps([{"id": "x", "pid": 1, "pgid": 1, "port": 1, "harness_pid": 2 ** 31 - 1}]),
        encoding="utf-8",
    )

    check = doctor._procs(data)

    assert check.status == doctor.WARN
    assert "órfão" in check.detail
