"""`harness serve`: gramática OpenAI (models/chat/stream) e o router de
comandos por trás dela. Sem rede, sem `bd`/LLM real — tudo que fala fora do
processo (`_bd`, `_http_post`, `_http_get`, `_popen`) é trocado por fake,
mesma convenção do webhook (`tests/test_triggers.py`).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from harness import serve

# --------------------------------------------------------------------------- helpers


def _ctx(cwd: Path) -> serve.ServeContext:
    return serve.ServeContext(cwd=cwd)


def _serve(cwd: Path, max_requests: int, api_key: str | None = None) -> tuple[int, threading.Thread]:
    bound: list[int] = []
    ready = threading.Event()

    def on_bind(port: int) -> None:
        bound.append(port)
        ready.set()

    t = threading.Thread(
        target=serve.serve,
        kwargs={
            "port": 0,
            "host": "127.0.0.1",
            "cwd": cwd,
            "on_bind": on_bind,
            "max_requests": max_requests,
            "api_key": api_key,
        },
        daemon=True,
    )
    t.start()
    assert ready.wait(5)
    return bound[0], t


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post(port: int, body: dict, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, raw, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), ""


# --------------------------------------------------------------------------- OpenAI shapes


def test_models_payload_um_modelo_harness() -> None:
    payload = serve.models_payload()
    assert payload["object"] == "list"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["id"] == "harness"


def test_completion_payload_shape() -> None:
    payload = serve.completion_payload("oi", cid="chatcmpl-x", created=7)
    choice = payload["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_stream_chunks_grammar() -> None:
    frames = list(serve.stream_chunks("olá mundo", cid="x", created=7))
    for f in frames:
        assert f.startswith(b"data: ")
        assert f.endswith(b"\n\n")
    first = json.loads(frames[0][len(b"data: ") : -2])
    assert first["choices"][0]["delta"]["role"] == "assistant"
    assert frames[-1] == b"data: [DONE]\n\n"
    closer = json.loads(frames[-2][len(b"data: ") : -2])
    assert closer["choices"][0]["finish_reason"] == "stop"
    assert closer["choices"][0]["delta"] == {}


def test_stream_chunks_texto_vazio_ainda_abre_e_fecha() -> None:
    frames = list(serve.stream_chunks("", cid="x", created=7))
    assert len(frames) == 3
    assert frames[-1] == b"data: [DONE]\n\n"


# --------------------------------------------------------------------------- servidor vivo


def test_get_v1_models_200(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=1)
    status, body = _get(port, "/v1/models")
    t.join(5)
    assert status == 200
    assert body["data"][0]["id"] == "harness"


def test_post_chat_completions_help(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(port, {"model": "harness", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    body = json.loads(raw)
    assert "/do" in body["choices"][0]["message"]["content"]


def test_post_chat_completions_stream(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=1)
    data = json.dumps(
        {"model": "harness", "stream": True, "messages": [{"role": "user", "content": "/help"}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
    t.join(5)
    assert ctype == "text/event-stream"
    assert "chat.completion.chunk" in body
    assert body.rstrip().endswith("data: [DONE]")


# --------------------------------------------------------------------------- api key


def test_api_key_bearer_correto_200_em_todas_rotas(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=2, api_key="segredo")
    status_get, body_get = _get(port, "/v1/models", headers={"Authorization": "Bearer segredo"})
    status_post, raw_post, _ = _post(
        port,
        {"model": "harness", "messages": [{"role": "user", "content": "/help"}]},
        headers={"Authorization": "Bearer segredo"},
    )
    t.join(5)
    assert status_get == 200
    assert body_get["data"][0]["id"] == "harness"
    assert status_post == 200
    assert "/do" in json.loads(raw_post)["choices"][0]["message"]["content"]


def test_api_key_faltando_ou_errada_401_em_todas_rotas(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=4, api_key="segredo")
    status_get_sem, body_get_sem = _get(port, "/v1/models")
    status_get_errada, _ = _get(port, "/v1/models", headers={"Authorization": "Bearer errada"})
    status_post_sem, raw_post_sem, _ = _post(port, {"messages": []})
    status_post_errada, _, _ = _post(port, {"messages": []}, headers={"Authorization": "Bearer errada"})
    t.join(5)
    assert status_get_sem == 401
    assert body_get_sem == {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
    assert status_get_errada == 401
    assert status_post_sem == 401
    assert json.loads(raw_post_sem) == {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
    assert status_post_errada == 401


def test_sem_key_loopback_continua_sem_auth(tmp_path: Path) -> None:
    # Mesmo caminho de `_serve` sem `api_key`: comportamento de hoje intacto.
    port, t = _serve(tmp_path, max_requests=1)
    status, body = _get(port, "/v1/models")
    t.join(5)
    assert status == 200
    assert body["data"][0]["id"] == "harness"


def test_auth_status_fail_closed_sem_key_fora_do_loopback() -> None:
    # Porteiro puro (sem socket): mesmo desenho de `webhook.screen_request`.
    assert serve.is_loopback("127.0.0.1")
    assert serve.is_loopback("localhost")
    assert serve.is_loopback("::1")
    assert not serve.is_loopback("0.0.0.0")

    assert serve.auth_status(None, True, "Bearer qualquer") == serve.FORBIDDEN
    assert serve.auth_status(None, False, None) is None  # loopback sem key: passa
    # sem "Bearer ": `_bearer` devolve None, key correta não bate contra None
    assert serve.auth_status("segredo", True, "segredo") == serve.UNAUTHORIZED
    assert serve.auth_status("segredo", True, "Bearer segredo") is None


# --------------------------------------------------------------------------- router puro


def test_help_e_comando_desconhecido(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ajuda = serve.handle_message("/help", ctx)
    assert "/do" in ajuda
    desconhecido = serve.handle_message("/nope", ctx)
    assert "comando desconhecido" in desconhecido
    assert "/do" in desconhecido


def test_ready_rc_variantes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)

    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 faz isso\nhc-2 faz aquilo"))
    assert "hc-1 faz isso" in serve.handle_message("/ready", ctx)

    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (127, ""))
    assert "bd não instalado" in serve.handle_message("/ready", ctx)

    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (1, "boom\nmais"))
    resp = serve.handle_message("/ready", ctx)
    assert "bd ready falhou (rc=1)" in resp


def test_new_e_close_argv_exato(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    chamadas: list[tuple] = []

    def fake_bd(*args: str, **kw: object) -> tuple[int, str]:
        chamadas.append(args)
        return 0, "hc-12 criado"

    monkeypatch.setattr(serve, "_bd", fake_bd)

    assert serve.handle_message("/new titulo", ctx) == "criada: hc-12 criado"
    assert chamadas[-1] == ("create", "titulo")

    assert serve.handle_message("/close hc-12", ctx) == "fechada: hc-12 criado"
    assert chamadas[-1] == ("close", "hc-12")

    antes = len(chamadas)
    assert serve.handle_message("/new", ctx) == "uso: /new <título>"
    assert serve.handle_message("/close", ctx) == "uso: /close <id>"
    assert len(chamadas) == antes  # nenhuma chamada extra a _bd


def test_do_max_usd_acima_do_teto_nao_dispara(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    chamado = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append((a, kw)) or 1)

    resp = serve.handle_message("/do 'x' --max-usd 9", ctx)
    assert "passa do teto do servidor (5.00)" in resp
    assert chamado == []


def test_do_ok_dispara_um_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    capturados: list[list[str]] = []

    def fake_popen(argv: list[str], *, cwd: Path, log: Path) -> int:
        capturados.append(argv)
        return 424242

    monkeypatch.setattr(serve, "_popen", fake_popen)

    resp = serve.handle_message("/do conserta o bug", ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert "do" in argv
    assert "conserta o bug" in argv
    assert "--no-apply" in argv
    # O teto (default do servidor, sem `--max-usd` no pedido) viaja no argv —
    # `cmd_do` é quem aplica de verdade agora, fail-closed, não só o governor.
    assert argv[argv.index("--max-usd") + 1] == "5.00"

    jobs = list((tmp_path / "data" / "serve" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    assert "job " in resp
    assert "/status" in resp


def test_do_max_usd_zero_ou_negativo_recusa_sem_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    chamado = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append((a, kw)) or 1)

    for valor in ("0", "-1"):
        resp = serve.handle_message(f"/do 'x' --max-usd {valor}", ctx)
        assert "--max-usd tem que ser maior que zero" in resp
    assert chamado == []


def test_do_recusa_segundo_job_enquanto_primeiro_roda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    chamadas = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamadas.append(1) or 1)
    monkeypatch.setattr(serve, "job_running", lambda job: True)

    primeiro = serve.handle_message("/do primeira tarefa", ctx)
    assert "job " in primeiro
    assert len(chamadas) == 1

    segundo = serve.handle_message("/do segunda tarefa", ctx)
    assert "já tem um job rodando" in segundo
    assert len(chamadas) == 1


# --------------------------------------------------------------------------- LLM path


def test_texto_livre_llm_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))

    body = json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]})
    monkeypatch.setattr(serve, "_http_post", lambda url, payload, timeout_s: (200, body))

    resp = serve.handle_message("oi, como vai a fila?", ctx)
    assert resp == "resposta do modelo"


def test_texto_livre_sem_llm_cai_no_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(serve, "_http_post", lambda url, payload, timeout_s: (0, ""))

    resp = serve.handle_message("oi", ctx)
    assert "LM Studio não respondeu" in resp
    assert "/do" in resp


def test_system_prompt_traz_bloco_untrusted_e_tamanho_limitado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARNESS_TRUST_BOUNDARY", raising=False)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 tarefa"))

    prompt = serve.system_prompt(tmp_path)
    assert "untrusted_reference_data" in prompt
    assert str(tmp_path) in prompt
    base_len = len(serve.HELP) + 300
    assert len(prompt) <= 2 * serve.STATE_MAX_CHARS + base_len + 500


# --------------------------------------------------------------------------- segurança estrutural


def test_fonte_nao_tem_caminho_de_escrita_perigosa() -> None:
    src = Path("harness/serve.py").read_text(encoding="utf-8")
    for proibido in ("--yes", "market.approve", "approve_queued", "write_genome", "genome.write"):
        assert proibido not in src

    assert set(serve._COMMANDS) == {
        "help",
        "status",
        "ready",
        "queue",
        "history",
        "market",
        "new",
        "close",
        "do",
    }


def test_parser_wiring() -> None:
    from harness.cli import build_parser

    p = build_parser()
    ns = p.parse_args(["serve"])
    assert ns.port == 8765
    assert ns.host == "127.0.0.1"
    assert ns.api_key is None

    ns0 = p.parse_args(["serve", "--port", "0"])
    assert ns0.port == 0

    nsk = p.parse_args(["serve", "--api-key", "segredo"])
    assert nsk.api_key == "segredo"
