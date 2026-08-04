"""`harness vision-judge` / `view_render`: o que esta suíte protege é o FAIL-OPEN.

Um juiz probabilístico que reprova quando o servidor de visão está fora
transforma "não medi" em "está ruim", e a fila inteira trava numa máquina sem
modelo de visão carregado. Por isso quase todo teste aqui é sobre o caminho
degradado: sem `[vision]`, HTTP 500, resposta que não é JSON.

Chrome e servidor de visão são fakes: o navegador pelo mesmo script do
test_uiverify (o CI roda ubuntu sem Chrome) e o VLM por monkeypatch em
`vision._http_post`, que existe exatamente para isso. Nenhum token é gasto nem
na suíte nem no CI.
"""

import json
import stat
import sys

import pytest

from harness import cli, quality_baseline, uiverify, vision
from harness.backends import dom_tools

FAKE_CHROME = '''#!{python}
import os, sys
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--screenshot="))
n = int(os.environ.get("FAKE_SHOT_BYTES", "40000"))
with open(out, "wb") as fh:
    fh.write(b"\\x89PNG\\r\\n\\x1a\\n" + b"x" * n + b"IEND\\xaeB`\\x82")
'''


@pytest.fixture(autouse=True)
def fake_chrome(tmp_path, monkeypatch):
    exe = tmp_path / "fake-chrome"
    exe.write_text(FAKE_CHROME.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(uiverify.CHROME_ENV, str(exe))
    monkeypatch.setenv("FAKE_SHOT_BYTES", "40000")
    return exe


@pytest.fixture
def cfg_sem_vision(tmp_path, monkeypatch):
    """config/ sem bloco [vision] — o estado da máquina antes do probe."""
    cfg = tmp_path / "config-sem"
    cfg.mkdir()
    (cfg / "models.toml").write_text('[router]\ndefault_tier = "t0"\n', encoding="utf-8")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture
def cfg_com_vision(tmp_path, monkeypatch):
    cfg = tmp_path / "config-com"
    cfg.mkdir()
    (cfg / "models.toml").write_text(
        '[vision]\nmodel = "openai:fake-vlm"\ntimeout_s = 5\nmax_tokens = 200\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(cfg))
    return cfg


def make_dist(tmp_path, body="<h1>oi</h1>"):
    dist = tmp_path / "ws" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        f"<!DOCTYPE html><html><head></head><body>{body}</body></html>", encoding="utf-8"
    )
    return dist


def resposta(content: str) -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}}]})


def fake_post(chamadas: list, retorno):
    def _post(url, payload, timeout_s):
        chamadas.append({"url": url, "payload": payload, "timeout_s": timeout_s})
        return retorno

    return _post


def png(path, n=40000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * n + b"IEND\xaeB`\x82")
    return path


# --------------------------------------------------------------------------- fail-open


def test_sem_vision_unavailable_exit_zero(tmp_path, cfg_sem_vision, capsys):
    """Sem [vision] no models.toml: nada de exceção, nada de reprova."""
    make_dist(tmp_path)
    assert vision.load_vision() is None

    code = cli.main(["vision-judge", "--dist", "dist", "--ws", str(tmp_path / "ws")])
    err = capsys.readouterr().err

    assert code == 0
    assert vision.UNAVAILABLE in err
    assert "FALHA" not in err


def test_http_500_fail_open(tmp_path, cfg_com_vision, monkeypatch, capsys):
    """Servidor de visão respondendo 500 é fail-open, não reprovação."""
    chamadas: list = []
    monkeypatch.setattr(vision, "_http_post", fake_post(chamadas, (500, "")))
    make_dist(tmp_path)

    code = cli.main(["vision-judge", "--dist", "dist", "--ws", str(tmp_path / "ws")])
    err = capsys.readouterr().err

    assert code == 0
    assert len(chamadas) == 1          # tentou de verdade
    assert vision.UNAVAILABLE in err


def test_resposta_ilegivel_fail_open(tmp_path, cfg_com_vision, monkeypatch):
    """Modelo que devolve prosa em vez de JSON não vira nota inventada."""
    monkeypatch.setattr(
        vision, "_http_post", fake_post([], resposta("achei bonito, parabéns"))
    )
    res = vision.judge_image(png(tmp_path / "s.png"))

    assert res["nota"] is None and res["ok"] is None
    assert vision.UNAVAILABLE in res["unavailable"]
    assert res["raw"] == "achei bonito, parabéns"


# --------------------------------------------------------------------------- parse


def test_json_cercado_parseia(tmp_path, cfg_com_vision, monkeypatch):
    """```json ... ``` é formatação, não resposta: a cerca sai antes do parse."""
    cercado = '```json\n{"nota": 7.5, "ok": true, "bullets": ["contraste fraco no rodapé"]}\n```'
    monkeypatch.setattr(vision, "_http_post", fake_post([], resposta(cercado)))

    res = vision.judge_image(png(tmp_path / "s.png"), question="e o rodapé?")

    assert res["unavailable"] is None
    assert res["nota"] == 7.5 and res["ok"] is True
    assert res["bullets"] == ["contraste fraco no rodapé"]


def test_payload_leva_data_uri_e_config(tmp_path, cfg_com_vision, monkeypatch):
    """A imagem vai como data-URI base64 e o modelo/timeout vêm do [vision]."""
    chamadas: list = []
    monkeypatch.setattr(
        vision, "_http_post", fake_post(chamadas, resposta('{"nota": 9, "ok": true, "bullets": []}'))
    )
    vision.judge_image(png(tmp_path / "s.png"))

    req = chamadas[0]
    assert req["url"].endswith("/chat/completions")
    assert req["payload"]["model"] == "fake-vlm"      # prefixo openai: sai no HTTP cru
    assert req["timeout_s"] == 5.0
    imagem = req["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert imagem.startswith("data:image/png;base64,")


def test_rubrica_default_e_override(tmp_path, cfg_com_vision, monkeypatch):
    """5 eixos embutidos; config/rubric.toml (zona mutável) sobrescreve."""
    for eixo in ("HIERARQUIA", "CONTRASTE", "ALINHAMENTO", "ESTADO VAZIO", "RESPONSIVO"):
        assert eixo in vision.DEFAULT_RUBRIC
    assert vision.load_rubric() == vision.DEFAULT_RUBRIC

    (cfg_com_vision / "rubric.toml").write_text(
        'rubric = "1. SÓ ISSO: a tela tem conteúdo?"\n', encoding="utf-8"
    )
    assert vision.load_rubric() == "1. SÓ ISSO: a tela tem conteúdo?"

    chamadas: list = []
    monkeypatch.setattr(
        vision, "_http_post", fake_post(chamadas, resposta('{"nota": 6, "ok": true}'))
    )
    vision.judge_image(png(tmp_path / "s.png"))
    assert "SÓ ISSO" in chamadas[0]["payload"]["messages"][0]["content"][0]["text"]


def test_thinking_com_content_vazio_cai_no_reasoning(tmp_path, cfg_com_vision, monkeypatch):
    """Modelo que pensa e fecha o JSON no `reasoning_content` ainda é juízo.

    Formato real do LM Studio servindo o qwen3.5: `content` vazio e a resposta
    dentro do reasoning. Sem a rede, o juiz sumia com "resposta não é {...}".
    """
    corpo = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "",
                        "reasoning_content": 'Vou olhar os eixos... {"nota": 4, "ok": false, "bullets": ["rodapé encavalado"]}',
                    },
                }
            ]
        }
    )
    monkeypatch.setattr(vision, "_http_post", fake_post([], (200, corpo)))

    res = vision.judge_image(png(tmp_path / "s.png"))

    assert res["unavailable"] is None
    assert res["nota"] == 4.0 and res["ok"] is False
    assert res["bullets"] == ["rodapé encavalado"]


def test_truncado_diz_max_tokens_e_nao_forma_errada(tmp_path, cfg_com_vision, monkeypatch):
    """`finish_reason: length` é orçamento curto, não modelo desobediente.

    O motivo tem que apontar o conserto certo: era isso que faltava quando o
    thinking comia os 1600 tokens inteiros e o juiz saía sem explicação.
    """
    corpo = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "analisando a tela"},
                }
            ]
        }
    )
    monkeypatch.setattr(vision, "_http_post", fake_post([], (200, corpo)))

    res = vision.judge_image(png(tmp_path / "s.png"))

    assert res["nota"] is None and res["ok"] is None
    assert "max_tokens acabou" in res["unavailable"]


def test_orcamento_default_cabe_no_thinking(tmp_path, monkeypatch):
    """Bloco sem `max_tokens` não pode reintroduzir o corte pela porta do default.

    Medido no servidor vivo: 1943 tokens só de reasoning num juízo de tela real.
    """
    cfg = tmp_path / "config-magro"
    cfg.mkdir()
    (cfg / "models.toml").write_text('[vision]\nmodel = "openai:fake-vlm"\n', encoding="utf-8")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(cfg))

    assert vision.load_vision()["max_tokens"] >= 3000


# --------------------------------------------------------------------------- pareado


def test_compare_reference_parse_a_e_b(tmp_path, cfg_com_vision, monkeypatch):
    """Comparação pareada: duas imagens no MESMO request, veredito a|b."""
    chamadas: list = []
    monkeypatch.setattr(
        vision,
        "_http_post",
        fake_post(chamadas, resposta('{"melhor": "a", "motivo": "hierarquia mais clara"}')),
    )
    novo, ref = png(tmp_path / "novo.png"), png(tmp_path / "ref.png", 30000)

    res = vision.compare_reference(novo, ref)
    assert res["melhor"] == "a" and res["motivo"] == "hierarquia mais clara"
    assert res["unavailable"] is None
    content = chamadas[0]["payload"]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text", "image_url", "image_url"]

    monkeypatch.setattr(
        vision, "_http_post", fake_post([], resposta('{"melhor": "B", "motivo": "empate"}'))
    )
    assert vision.compare_reference(novo, ref)["melhor"] == "b"

    monkeypatch.setattr(vision, "_http_post", fake_post([], resposta('{"melhor": "nenhuma"}')))
    fora = vision.compare_reference(novo, ref)
    assert fora["melhor"] is None and vision.UNAVAILABLE in fora["unavailable"]


def test_cli_ref_reprova_quando_b_ganha(tmp_path, cfg_com_vision, monkeypatch, capsys):
    monkeypatch.setattr(
        vision, "_http_post", fake_post([], resposta('{"melhor": "b", "motivo": "a nova está crua"}'))
    )
    make_dist(tmp_path)
    ref = png(tmp_path / "ref.png")

    code = cli.main(
        ["vision-judge", "--dist", "dist", "--ws", str(tmp_path / "ws"), "--ref", str(ref)]
    )
    assert code == 1
    assert "FALHA" in capsys.readouterr().err


# --------------------------------------------------------------------------- tela vazia


def test_png_vazio_nao_chama_http(tmp_path, cfg_com_vision, monkeypatch, capsys):
    """PNG abaixo de 20kb é tela vazia: reprova SEM gastar o modelo de visão."""
    chamadas: list = []
    monkeypatch.setattr(vision, "_http_post", fake_post(chamadas, resposta('{"nota": 10}')))
    monkeypatch.setenv("FAKE_SHOT_BYTES", "500")
    make_dist(tmp_path, body="")

    code = cli.main(["vision-judge", "--dist", "dist", "--ws", str(tmp_path / "ws")])

    assert code == 1
    assert chamadas == []
    assert "tela provavelmente vazia" in capsys.readouterr().err


def test_view_render_recusa_porta_nao_registrada(tmp_path, cfg_sem_vision):
    """A cerca do local_probe, igual: só servidor desta run."""
    ws = tmp_path / "ws"
    ws.mkdir()
    shot, kb, erro = dom_tools.render(ws, port=54321)

    assert shot is None and kb == 0.0
    assert "não está registrada" in erro


def test_view_render_dist_grava_shot(tmp_path, cfg_sem_vision):
    """Sem visão configurada a tool ainda serve: diz que renderizou e não mente."""
    make_dist(tmp_path)
    tools = dom_tools.make_view_tools(tmp_path / "ws")
    assert [t.name for t in tools] == ["view_render"]

    out = tools[0].invoke({"dist_path": "dist"})

    assert "renderizou" in out and vision.UNAVAILABLE in out
    shots = list((tmp_path / "ws" / ".harness" / "shots").glob("dist-*.png"))
    assert len(shots) == 1


# --------------------------------------------------------------------------- baseline


def test_baseline_grava_e_compara(tmp_path):
    ws = tmp_path / "ws"
    assert quality_baseline.load_baseline(ws) is None

    path = quality_baseline.save_baseline(ws, {"nota": 6.5, "a11y": 0.9})
    assert path == ws / ".harness" / "quality-baseline.json"
    assert quality_baseline.load_baseline(ws) == {"nota": 6.5, "a11y": 0.9}

    quality_baseline.save_baseline(ws, {"nota": 7.0})   # merge, não sobrescreve tudo
    assert quality_baseline.load_baseline(ws) == {"nota": 7.0, "a11y": 0.9}

    path.write_text("{isto não é json", encoding="utf-8")
    assert quality_baseline.load_baseline(ws) is None   # piso ilegível = sem piso


def test_min_nota_baseline_regua_relativa(tmp_path, cfg_com_vision, monkeypatch, capsys):
    """Sem baseline passa e grava; depois a nota anterior é o piso."""
    ws = tmp_path / "ws"
    make_dist(tmp_path)
    monkeypatch.setattr(vision, "_http_post", fake_post([], resposta('{"nota": 7, "ok": true}')))

    argv = ["vision-judge", "--dist", "dist", "--ws", str(ws), "--min-nota", "baseline"]
    assert cli.main(argv) == 0
    assert quality_baseline.load_baseline(ws) == {"nota": 7.0}

    # Mesma tela, nota pior: agora existe piso e o gate reprova (anti-platô).
    monkeypatch.setattr(vision, "_http_post", fake_post([], resposta('{"nota": 5, "ok": true}')))
    assert cli.main(argv) == 1
    assert "FALHA" in capsys.readouterr().err
    assert quality_baseline.load_baseline(ws) == {"nota": 7.0}   # piso não afrouxa


def test_min_nota_absoluto(tmp_path, cfg_com_vision, monkeypatch):
    """O piso default é absoluto e não grava baseline nenhum."""
    ws = tmp_path / "ws"
    make_dist(tmp_path)
    monkeypatch.setattr(vision, "_http_post", fake_post([], resposta('{"nota": 6.2, "ok": true}')))

    assert cli.main(["vision-judge", "--dist", "dist", "--ws", str(ws), "--min-nota", "6"]) == 0
    assert cli.main(["vision-judge", "--dist", "dist", "--ws", str(ws), "--min-nota", "8"]) == 1
    assert quality_baseline.load_baseline(ws) is None


def test_check_de_regua_graduada_nao_precisa_de_tipo_novo(tmp_path):
    """`[checks]` é nome -> {cmd, weight}: vision-judge entra como qualquer cmd."""
    unit = tmp_path / "unit.toml"
    unit.write_text(
        'id = "u1"\nprompt = "faz"\nverify_cmd = "true"\n\n'
        '[checks.visao]\ncmd = "harness vision-judge --dist dist --min-nota 6"\nweight = 2\n',
        encoding="utf-8",
    )
    spec = cli.load_unit(unit)

    check = next(c for c in spec.checks if c.name == "visao")
    assert check.cmd == "harness vision-judge --dist dist --min-nota 6"
    assert check.weight == 2.0
