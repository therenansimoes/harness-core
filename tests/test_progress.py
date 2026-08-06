"""`harness.progress`: o sinal de vida que o `harness do` mostra em stderr.

O que estes testes protegem não é o texto exato, é a promessa: uma etapa
concluída aparece em português, um nó desconhecido não trava nada, o
heartbeat prova vida enquanto ninguém termina, e a thread nunca sobrevive ao
`with` nem derruba o run se a saída já fechou.
"""

import io
import time

from harness.progress import Progress


def test_stage_imprime_a_etapa_em_portugues():
    buf = io.StringIO()
    p = Progress(out=buf)

    p.stage("execute")

    saida = buf.getvalue()
    assert "o agente terminou de trabalhar" in saida
    assert "(0s)" in saida


def test_stage_de_no_desconhecido_usa_o_nome_cru():
    buf = io.StringIO()
    p = Progress(out=buf)

    p.stage("nó_de_plugin")

    assert "nó_de_plugin" in buf.getvalue()


def test_heartbeat_da_sinal_de_vida_enquanto_ninguem_termina():
    buf = io.StringIO()
    with Progress(out=buf, every_s=0.01):
        time.sleep(0.1)

    saida = buf.getvalue()
    assert "de execução" in saida
    assert "começando" in saida


def test_stop_encerra_a_thread():
    buf = io.StringIO()
    with Progress(out=buf, every_s=0.01) as p:
        time.sleep(0.05)

    assert p._thread.is_alive() is False
    tamanho = len(buf.getvalue())
    time.sleep(0.05)
    assert len(buf.getvalue()) == tamanho


def test_progress_nunca_derruba_o_run():
    buf = io.StringIO()
    buf.close()
    p = Progress(out=buf)

    p.stage("plan")  # não deve levantar mesmo com a saída fechada
