"""`harness serve`: gramática OpenAI (models/chat/stream) e o router de
comandos por trás dela. Sem rede, sem `bd`/LLM real — tudo que fala fora do
processo (`_bd`, `_http_post`, `_http_get`, `_popen`) é trocado por fake,
mesma convenção do webhook (`tests/test_triggers.py`).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from harness import serve

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cursor_request.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_tools() -> list[dict]:
    """Tools verbatim da captura real (harness-core-z8n) — `Shell` e
    `TodoWrite` com o schema de verdade do Cursor, não uma aproximação."""
    return _fixture()["tools"]


_PROIBIDAS = ("tool_call", "Shell", "TodoWrite", "function call", "ferramenta")


# --------------------------------------------------------------------------- helpers


def _ctx(cwd: Path) -> serve.ServeContext:
    return serve.ServeContext(cwd=cwd)


def _fake_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 tiers determinísticos pro `executors()` — mesmo shape de
    `config/models.toml` (t0 local, t1 haiku, t2 placeholder), mas
    independente do arquivo real: `harness.routing.router.executors()` faz o
    `from harness.routing.router import load_config` DENTRO da função (import
    lazy), então trocar o atributo do módulo aqui é resolvido de novo a cada
    chamada — não precisa de env var nem de arquivo em disco."""
    cfg = {
        "tier": [
            {"name": "t0", "backend": "deepagents", "model": "openai:qwen/qwen3.5-9b", "max_turns": 40, "cost_rank": 0},
            {"name": "t1", "backend": "claude_code", "model": "haiku", "max_turns": 24, "cost_rank": 1},
            {"name": "t2", "backend": "claude_code", "model": "", "max_turns": 40, "cost_rank": 2},
        ],
        "router": {"default_tier": "t0", "kind": {"code": "t1", "refactor": "t2"}},
    }
    monkeypatch.setattr("harness.routing.router.load_config", lambda *a, **kw: cfg)


def _serve(
    cwd: Path,
    max_requests: int,
    api_key: str | None = None,
    workspace_roots: tuple[Path, ...] | None = None,
    verbose: bool = False,
    debug_dump: Path | None = None,
    trace: bool = False,
) -> tuple[int, threading.Thread]:
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
            "workspace_roots": workspace_roots,
            "verbose": verbose,
            "debug_dump": debug_dump,
            "trace": trace,
        },
        daemon=True,
    )
    t.start()
    assert ready.wait(5)
    return bound[0], t


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _git_repo(tmp_path: Path, name: str, *, marker: str | None = None) -> Path:
    """Repo git de verdade: init + 1 commit + README.md com marcador único —
    o marcador é o que os testes de `system_prompt` procuram no bloco
    untrusted para provar que o texto do repo detectado chegou lá."""
    marker = marker or f"MARCADOR_UNICO_{name.upper()}"
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text(f"# {name}\n\n{marker}\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo.resolve()


def _cursor_body(
    ws: Path,
    query: str,
    *,
    pos: int = 1,
    parts: bool = False,
    extra: list[dict] | None = None,
) -> dict:
    """Shape capturado de verdade: `messages[0]` é o system do Cursor, o
    bloco `<user_info>` (Workspace Path + Is directory a git repo) entra no
    índice `pos` atrás de fillers, e a última mensagem `user` é o que o
    usuário digitou, embrulhado em `<timestamp>...</timestamp>\\n<user_query>`
    — como string ou como partes, conforme `parts`."""
    messages: list[dict] = [{"role": "system", "content": "system prompt do Cursor"}]
    while len(messages) < pos:
        messages.append({"role": "assistant", "content": f"filler {len(messages)}"})
    user_info = f"<user_info>\nWorkspace Path: {ws}\nIs directory a git repo: Yes, at {ws}\n</user_info>"
    messages.insert(pos, {"role": "user", "content": user_info})
    if extra:
        messages.extend(extra)
    tail = f"<timestamp>x</timestamp>\n<user_query>\n{query}\n</user_query>"
    content = [{"type": "text", "text": tail}] if parts else tail
    messages.append({"role": "user", "content": content})
    return {"model": "harness", "messages": messages}


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
    assert len(payload["data"]) == 1 + len(serve.executors())
    assert payload["data"][0]["id"] == "qwopus3.5-4b-coder-mtp"
    assert payload["data"][1]["id"] == "harness"


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


def test_md_paragraphs_indentado_vira_item_de_lista() -> None:
    texto = "doctor: 0 falha(s)\n  [harness] abc123  rodando     \"tarefa\"\nauto-aprovação: enabled=True"
    out = serve.md_paragraphs(texto)
    assert out == (
        "doctor: 0 falha(s)"
        "\n\n- [harness] abc123  rodando     \"tarefa\""
        "\n\nauto-aprovação: enabled=True"
    )


def test_md_paragraphs_e_idempotente() -> None:
    texto = serve.HELP  # já tem linhas indentadas de sobra (a lista de comandos)
    uma_vez = serve.md_paragraphs(texto)
    duas_vezes = serve.md_paragraphs(uma_vez)
    assert duas_vezes == uma_vez


# --------------------------------------------------------------------------- servidor vivo


def test_get_v1_models_200(tmp_path: Path) -> None:
    port, t = _serve(tmp_path, max_requests=1)
    status, body = _get(port, "/v1/models")
    t.join(5)
    assert status == 200
    assert len(body["data"]) == 1 + len(serve.executors())
    assert body["data"][0]["id"] == "qwopus3.5-4b-coder-mtp"


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
    assert body_get["data"][0]["id"] == "qwopus3.5-4b-coder-mtp"
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
    assert body["data"][0]["id"] == "qwopus3.5-4b-coder-mtp"


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

    # rc!=0 com cwd explícito (é o que /ready sempre passa agora, detectado ou
    # não — `cwd is not None` é o sinal que diferencia esse canal do
    # `system_prompt()` sem detecção, que continua chamando `ready_text()`
    # sem cwd e preserva a mensagem "bd ready falhou" de hoje) nunca mais cai
    # em "bd ready falhou": vira o texto amigável de fila do harness.
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (1, "boom\nmais"))
    resp = serve.handle_message("/ready", ctx)
    assert "sem beads em" in resp
    assert "fila do harness" in resp
    assert "rc=1" not in resp


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

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
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
    assert "mlx_lm.server não respondeu" in resp
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
    for arquivo in ("harness/serve.py", "harness/cursor_tools.py"):
        src = Path(arquivo).read_text(encoding="utf-8")
        for proibido in ("--yes", "market.approve", "approve_queued", "write_genome", "genome.write"):
            assert proibido not in src, arquivo

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
        "where",
        "models",
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

    nsw = p.parse_args(["serve", "--workspace-root", "/a", "--workspace-root", "/b"])
    assert nsw.workspace_root == ["/a", "/b"]

    nswd = p.parse_args(["serve"])
    assert nswd.workspace_root is None
    assert nswd.verbose is False
    assert nswd.debug_dump is None
    assert nswd.trace is False

    nsv = p.parse_args(["serve", "--verbose", "--debug-dump", "/tmp/dump.jsonl"])
    assert nsv.verbose is True
    assert nsv.debug_dump == Path("/tmp/dump.jsonl")

    nst = p.parse_args(["serve", "--trace"])
    assert nst.trace is True


# --------------------------------------------------------------------------- workspace do Cursor: extração


def test_extrai_workspace_content_string(tmp_path: Path) -> None:
    ws = tmp_path / "meurepo"
    ws.mkdir()
    msg = {"role": "user", "content": f"<user_info>\nWorkspace Path: {ws}\n</user_info>"}
    assert serve.extract_workspace([msg]) == str(ws)


def test_extrai_workspace_content_em_partes(tmp_path: Path) -> None:
    ws = tmp_path / "meurepo"
    ws.mkdir()
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": f"<user_info>\nWorkspace Path: {ws}\n</user_info>"}],
    }
    assert serve.extract_workspace([msg]) == str(ws)


def test_user_info_em_posicao_tardia_ainda_detecta(tmp_path: Path) -> None:
    ws = tmp_path / "meurepo"
    ws.mkdir()
    fillers = [{"role": "assistant", "content": f"filler {i}"} for i in range(9)]
    msg = {"role": "user", "content": f"<user_info>\nWorkspace Path: {ws}\n</user_info>"}
    messages = [*fillers, msg]
    assert len(messages) == 10  # bloco no índice 9, atrás de 9 fillers
    assert serve.extract_workspace(messages) == str(ws)


def test_extrai_workspace_fallback_is_git_repo(tmp_path: Path) -> None:
    ws = tmp_path / "meurepo"
    ws.mkdir()
    msg = {
        "role": "user",
        "content": f"<user_info>\nIs directory a git repo: Yes, at {ws}\n</user_info>",
    }
    assert serve.extract_workspace([msg]) == str(ws)


def test_sem_user_info_detection_ausente() -> None:
    assert serve.extract_workspace([{"role": "user", "content": "oi, tudo bem?"}]) is None
    det = serve.validate_workspace(None, ())
    assert det.verdict == "ausente"
    assert det.path is None


def test_user_info_falso_em_tool_result_nao_e_aceito(tmp_path: Path) -> None:
    ws = tmp_path / "real"
    ws.mkdir()
    # "Workspace Path: /etc" solto num tool result, sem o wrapper <user_info>
    # — nunca é lido, mesmo com uma mensagem real (com wrapper) na lista.
    messages = [
        {"role": "user", "content": f"<user_info>\nWorkspace Path: {ws}\n</user_info>"},
        {"role": "user", "content": "tool result: Workspace Path: /etc"},
    ]
    assert serve.extract_workspace(messages) == str(ws)

    # Com o wrapper, mas fora da allowlist: extraído, porém rejeitado.
    fora = tmp_path / "fora"
    fora.mkdir()
    embrulhado = {"role": "user", "content": f"<user_info>\nWorkspace Path: {fora}\n</user_info>"}
    raw = serve.extract_workspace([embrulhado])
    assert raw == str(fora)
    det = serve.validate_workspace(raw, roots=())
    assert det.verdict == "fora dos roots"
    assert det.path is None


# --------------------------------------------------------------------------- workspace do Cursor: validação


def test_validacao_rejeita_traversal_e_symlink(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    inside = root / "ws"
    inside.mkdir()
    roots = (root.resolve(),)

    traversal = f"{root}/../../etc"
    assert serve.validate_workspace(traversal, roots).verdict != "ok"

    link = inside / "escape"
    try:
        link.symlink_to("/etc")
    except OSError:
        pytest.skip("symlink não suportado neste ambiente")
    assert serve.validate_workspace(str(link), roots).verdict != "ok"


def test_validacao_rejeita_relativo_inexistente_e_nul(tmp_path: Path) -> None:
    roots = (tmp_path.resolve(),)
    assert serve.validate_workspace("projetos/x", roots).verdict == "relativo"
    assert serve.validate_workspace(str(tmp_path / "nao-existe"), roots).verdict == "não é diretório"
    assert serve.validate_workspace("\x00", roots).verdict == "inválido"


def test_validacao_aceita_repo_registrado_fora_dos_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    from harness.projects import init_project

    repo = _git_repo(tmp_path, "fora")
    init_project(repo, "fora-proj", queue_dir=tmp_path / "fila")

    outra_root = tmp_path / "nada-a-ver"
    outra_root.mkdir()
    ctx = serve.ServeContext(cwd=tmp_path, workspace_roots=(outra_root.resolve(),))
    roots = serve.allowed_roots(ctx)
    assert repo in roots

    det = serve.validate_workspace(str(repo), roots)
    assert det.verdict == "ok"
    assert det.project == "fora-proj"


def test_user_query_desembrulha_ultimo() -> None:
    embrulhado = "<timestamp>x</timestamp>\n<user_query>\n/status\n</user_query>"
    assert serve.user_query_text(embrulhado) == "/status"

    dois_blocos = "<user_query>\nprimeiro\n</user_query>\nlixo\n<user_query>\nsegundo\n</user_query>"
    assert serve.user_query_text(dois_blocos) == "segundo"

    assert serve.user_query_text("/help") == "/help"


def test_readme_symlink_fora_do_repo_nao_e_lido(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    ws = tmp_path / "semreadme"
    ws.mkdir()
    fora = tmp_path / "segredo.md"
    fora.write_text("SEGREDO_FORA_DO_REPO\n", encoding="utf-8")
    try:
        (ws / "README.md").symlink_to(fora)
    except OSError:
        pytest.skip("symlink não suportado neste ambiente")
    assert "SEGREDO_FORA_DO_REPO" not in serve.readme_head(ws.resolve())


# --------------------------------------------------------------------------- workspace do Cursor: roteamento


def test_slash_command_do_cursor_roteia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "cursorws")
    port, t = _serve(tmp_path, max_requests=1, workspace_roots=(ws.parent,))
    status, raw, _ = _post(port, _cursor_body(ws, "/where"))
    t.join(5)
    assert status == 200
    content = json.loads(raw)["choices"][0]["message"]["content"]
    # este é o bug que este pacote corrige: sem desembrulhar <user_query>, o
    # "/where" digitado no Cursor nunca batia startswith("/") e caía no LM.
    assert "mlx_lm.server não respondeu" not in content
    assert str(ws) in content


def test_prompt_detectado_traz_projeto_e_nao_estado_do_harness_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARNESS_TRUST_BOUNDARY", raising=False)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 tarefa"))
    ws = _git_repo(tmp_path, "detectado", marker="MARCADOR_TEST_12")

    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    assert det.verdict == "ok"

    prompt = serve.system_prompt(tmp_path, det)
    assert "MARCADOR_TEST_12" in prompt
    assert str(ws) in prompt
    # "fila de aprovação" só existe no texto que `status_text()` produz — e
    # `status_text()` (doctor, auto-aprovação, fila de aprovação do
    # harness-core) fica de fora do ramo com workspace detectado.
    assert "fila de aprovação" not in prompt

    base_len = len(serve.HELP) + 300
    assert len(prompt) <= 2 * serve.STATE_MAX_CHARS + base_len + 500


def test_prompt_detectado_em_ambos_os_ramos_do_trust_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 tarefa"))
    ws = _git_repo(tmp_path, "boundary", marker="MARCADOR_TEST_13")
    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    assert det.verdict == "ok"

    monkeypatch.delenv("HARNESS_TRUST_BOUNDARY", raising=False)
    prompt = serve.system_prompt(tmp_path, det)
    assert "untrusted_reference_data" in prompt
    bloco = prompt.split("<untrusted_reference_data>", 1)[1]
    assert "MARCADOR_TEST_13" in bloco

    monkeypatch.setenv("HARNESS_TRUST_BOUNDARY", "0")
    prompt0 = serve.system_prompt(tmp_path, det)
    assert "MARCADOR_TEST_13" in prompt0


def test_sem_deteccao_prompt_identico_ao_de_hoje(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 tarefa"))
    ausente = serve.Detection(None, None, "ausente", None)
    assert serve.system_prompt(tmp_path) == serve.system_prompt(tmp_path, ausente)


def test_do_roda_no_workspace_detectado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "do-ws")
    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    assert det.verdict == "ok"
    ctx = serve.ServeContext(cwd=ws, home=tmp_path, project=det.project, detection=det)

    capturados: list[dict] = []

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
        capturados.append({"argv": argv, "cwd": cwd})
        return 999

    monkeypatch.setattr(serve, "_popen", fake_popen)

    resp = serve.handle_message("/do conserta algo", ctx)
    assert len(capturados) == 1
    assert capturados[0]["cwd"] == ws
    assert str(ws) in resp
    assert "não registrado" in resp  # repo não foi passado por `harness init`

    jobs = list((tmp_path / "data" / "serve" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    record = json.loads(jobs[0].read_text())
    assert record["cwd"] == str(ws)
    assert record["project"] is None


def test_do_sem_deteccao_continua_no_cwd_do_servidor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    capturados: list[Path] = []
    monkeypatch.setattr(
        serve,
        "_popen",
        lambda argv, *, cwd, log, env=None: capturados.append(cwd) or 1,
    )
    resp = serve.handle_message("/do tarefa qualquer", ctx)
    assert capturados == [tmp_path]
    assert "projeto:" not in resp


def test_dispatch_do_pina_config_e_data_no_filho(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    capturado: dict = {}

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
        capturado["env"] = env
        return 1

    monkeypatch.setattr(serve, "_popen", fake_popen)
    serve.dispatch_do("tarefa", 1.0, ctx)

    from harness import paths
    from harness.ledger import store as ledger_store

    assert capturado["env"][paths.CONFIG_DIR_ENV] == str(paths.config_dir().resolve())
    assert capturado["env"][paths.DATA_DIR_ENV] == str(ledger_store.data_dir().resolve())
    assert Path(capturado["env"][paths.CONFIG_DIR_ENV]).is_absolute()


def test_bd_usa_cwd_do_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "bd-ws")
    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    ctx_ws = serve.ServeContext(cwd=ws, home=tmp_path, project=det.project, detection=det)

    chamadas: list[Path | None] = []
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: chamadas.append(kw.get("cwd")) or (0, "ok"))

    serve.handle_message("/ready", ctx_ws)
    serve.handle_message("/new x", ctx_ws)
    serve.handle_message("/close id", ctx_ws)
    assert chamadas == [ws, ws, ws]

    chamadas.clear()
    serve.handle_message("/ready", _ctx(tmp_path))
    assert chamadas == [tmp_path]


def test_ready_sem_beads_cai_na_fila_do_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (1, "no such database"))
    resp = serve.handle_message("/ready", ctx)
    assert "fila do harness" in resp
    assert "rc=1" not in resp


def test_globais_seguem_globais(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "globais-ws")
    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    ctx_ws = serve.ServeContext(cwd=ws, home=tmp_path, project=det.project, detection=det)

    resp = serve.handle_message("/queue", ctx_ws)
    assert resp.startswith("(harness-core — global)")
    assert str(ws) not in resp


def test_where_reporta_verdito_e_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    ws = _git_repo(tmp_path, "where-ws")
    # Root = só o próprio `ws` (não o pai): um vizinho debaixo do mesmo pai
    # não pode ficar "dentro" por acidente de fixture.
    roots = (ws,)

    det_ok = serve.validate_workspace(str(ws), roots)
    ctx_ok = serve.ServeContext(
        cwd=ws, home=tmp_path, project=det_ok.project, workspace_roots=roots, detection=det_ok
    )
    resp_ok = serve.handle_message("/where", ctx_ok)
    assert str(ws) in resp_ok
    assert "veredito: ok" in resp_ok

    fora = tmp_path / "fora-de-tudo"
    fora.mkdir()
    det_no = serve.validate_workspace(str(fora), roots)
    ctx_no = serve.ServeContext(cwd=tmp_path, workspace_roots=roots, detection=det_no)
    resp_no = serve.handle_message("/where", ctx_no)
    assert "veredito: fora dos roots" in resp_no


def test_corpo_gigante_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    port, t = _serve(tmp_path, max_requests=1)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(serve.MAX_BODY_BYTES + 1))
        conn.endheaders()
        conn.send(b'{"messages": []}')  # bem menor que o Content-Length anunciado
        resp = conn.getresponse()
        status = resp.status
        resp.read()
    finally:
        conn.close()
    t.join(5)
    assert status == 413


# --------------------------------------------------------------------------- executores no campo "model"


def test_executores_saem_do_models_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tiers(monkeypatch)
    execs = serve.executors()
    # `harness:full` (híbrido: sempre local + consultor pago) fecha a lista —
    # só existe porque `_fake_tiers` tem local (t0) E pago (t1/t2) juntos.
    assert [e.id for e in execs] == [
        "harness",
        "harness:local",
        "harness:haiku",
        "harness:claude_code",
        "harness:full",
    ]
    assert [e.local for e in execs] == [True, True, False, False, True]
    assert execs[-1].advisor_tier == "t1"


def test_full_e_ultimo_e_hibrido_local_mais_consultor(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tiers(monkeypatch)
    execs = serve.executors()
    full = execs[-1]
    assert full.id == serve.FULL_ID
    assert full.local is True
    assert full.tier == ""
    assert full.advisor_tier == "t1"  # primeiro tier pago (harness:haiku)
    assert full.backend == "deepagents"  # backend do tier local (t0)

    # `harness:t0` continua resolvendo pro executor puramente local, não pro
    # híbrido — `harness:full` não tem tier, então não casa o alias oculto.
    ex = serve.resolve_executor("harness:t0")
    assert ex is not None
    assert ex.id == "harness:local"


def test_full_argv_tem_backend_local_e_advisor_pago(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tiers(monkeypatch)
    full = next(e for e in serve.executors() if e.id == serve.FULL_ID)
    assert serve.executor_argv(full) == [
        "--backend", "deepagents", "--model", "openai:qwen/qwen3.5-9b", "--advisor", "t1",
    ]


def test_chat_executor_em_auto_nunca_devolve_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tiers(monkeypatch)
    ctx = _ctx(tmp_path)  # sem pin => auto
    ex = serve.chat_executor(ctx)
    assert ex.id != serve.FULL_ID
    assert ex.id == "harness:local"


def test_acao_com_pin_full_dispara_local_e_avisa_consultor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    _fake_tiers(monkeypatch)

    def fail_http_post(*a, **kw):
        pytest.fail("`_http_post` não deveria ser chamado — ação dispara sem passar pelo LLM")

    monkeypatch.setattr(serve, "_http_post", fail_http_post)
    capturados: list[list[str]] = []
    monkeypatch.setattr(serve, "_popen", lambda argv, **kw: capturados.append(argv) or 1)

    full = next(e for e in serve.executors() if e.id == serve.FULL_ID)
    ctx = serve.ServeContext(cwd=tmp_path, executor=full)
    resp = serve.handle_message("adiciona um campo de desconto", ctx)

    assert len(capturados) == 1
    argv = capturados[0]
    assert argv[argv.index("--backend") + 1] == "deepagents"
    assert "claude_code" not in argv
    assert argv[argv.index("--advisor") + 1] == "t1"
    assert "consultor" in resp


def test_models_toml_quebrado_degrada_pro_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.routing.router import RouterError

    def boom(*a, **kw):
        raise RouterError("models.toml sumiu")

    monkeypatch.setattr("harness.routing.router.load_config", boom)
    execs = serve.executors()
    assert [e.id for e in execs] == ["harness"]
    assert [m["id"] for m in serve.models_payload()["data"]] == ["qwopus3.5-4b-coder-mtp", "harness"]


def test_models_payload_so_chaves_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tiers(monkeypatch)
    payload = serve.models_payload()
    ids = [item["id"] for item in payload["data"]]
    assert ids[0] == "qwopus3.5-4b-coder-mtp"
    assert "harness" in ids
    assert len(ids) == len(set(ids))
    for item in payload["data"]:
        assert set(item) == {"id", "object", "created", "owned_by"}


def test_modelo_desconhecido_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    def fail_popen(*a, **kw):
        pytest.fail("`_popen` não deveria ser chamado com model desconhecido")

    def fail_http_post(*a, **kw):
        pytest.fail("`_http_post` não deveria ser chamado com model desconhecido")

    monkeypatch.setattr(serve, "_popen", fail_popen)
    monkeypatch.setattr(serve, "_http_post", fail_http_post)
    port, t = _serve(tmp_path, max_requests=2)

    status, raw, _ = _post(port, {"model": "gpt-4o", "messages": [{"role": "user", "content": "oi"}]})
    body = json.loads(raw)
    assert status == 404
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["param"] == "model"
    assert body["error"]["type"] == "invalid_request_error"

    status_stream, raw_stream, ctype_stream = _post(
        port, {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "oi"}]}
    )
    t.join(5)
    assert status_stream == 404
    assert ctype_stream != "text/event-stream"
    assert json.loads(raw_stream)["error"]["code"] == "model_not_found"


def test_strict_model_off_degrada_pro_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(serve.STRICT_MODEL_ENV, "0")
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    port, t = _serve(tmp_path, max_requests=1)

    status, raw, _ = _post(port, {"model": "gpt-4o", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert json.loads(raw)["model"] == "harness"


def test_echo_do_modelo_pedido(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))

    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(port, {"model": "harness:local", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert json.loads(raw)["model"] == "harness:local"

    port2, t2 = _serve(tmp_path, max_requests=1)
    data = json.dumps(
        {"model": "harness:local", "stream": True, "messages": [{"role": "user", "content": "/help"}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port2}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    t2.join(5)
    frames = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert frames
    assert all(frame["model"] == "harness:local" for frame in frames)


def test_echo_nao_ecoa_grafia_torta_de_id_valido(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `resolve_executor` casa case-insensitive — o eco não pode devolver uma
    # grafia que não bate nenhum `id` de `models_payload()`.
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(port, {"model": "HARNESS:LOCAL", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert json.loads(raw)["model"] == "harness:local"


def test_pin_local_recebe_a_chamada_de_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    chamadas: list[tuple[str, dict]] = []

    def fake_http_post(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
        chamadas.append((url, payload))
        return 200, json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]})

    monkeypatch.setattr(serve, "_http_post", fake_http_post)

    ex = serve.Executor(
        id="harness:local", tier="t0", backend="deepagents", run_model="openai:qwen/qwen3.5-9b",
        local=True, label="",
    )
    ctx = serve.ServeContext(cwd=tmp_path, executor=ex)

    resp = serve.handle_message("oi, tudo bem?", ctx)
    assert resp == "resposta do modelo"
    assert len(chamadas) == 1
    url, payload = chamadas[0]
    assert url == f"{serve.llm_base_url()}/chat/completions"
    assert payload["model"] == serve.chat_model_candidates()[0]


def test_chat_com_executor_pago_responde_local_e_avisa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    chamadas: list[dict] = []

    def fake_http_post(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
        chamadas.append(payload)
        return 200, json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]})

    monkeypatch.setattr(serve, "_http_post", fake_http_post)

    def fail_popen(*a, **kw):
        pytest.fail("`_popen` não deveria ser chamado num turno de chat")

    monkeypatch.setattr(serve, "_popen", fail_popen)

    ex = serve.Executor(
        id="harness:haiku", tier="t1", backend="claude_code", run_model="haiku", local=False, label="",
    )
    ctx = serve.ServeContext(cwd=tmp_path, executor=ex)

    resp = serve.handle_message("oi, tudo bem?", ctx)
    assert "resposta do modelo" in resp
    assert "harness:haiku" in resp
    assert len(chamadas) == 1
    assert chamadas[0]["model"] in serve.chat_model_candidates()


def test_auto_avisa_tier_pago_so_com_sinal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # "preciso refatorar este módulo" bate `_ACTION_RE` ("refator") — sem
    # desligar o gateway de ação aqui este teste dispararia um /do de
    # verdade em vez de exercitar o `route_hint` do caminho de texto livre
    # que ele quer provar.
    monkeypatch.setenv(serve.AUTO_DO_ENV, "0")
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(
        serve,
        "_http_post",
        lambda url, payload, timeout_s: (
            200,
            json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]}),
        ),
    )
    ctx = _ctx(tmp_path)

    resp_kw = serve.handle_message("preciso refatorar este módulo", ctx)
    assert "router:" in resp_kw

    resp_neutro = serve.handle_message("oi, como vai a fila?", ctx)
    assert resp_neutro == "resposta do modelo"


def test_route_hint_nunca_levanta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("harness.routing.kinds.classify_kind", boom)
    assert serve.route_hint("qualquer coisa", _ctx(tmp_path)) == ""

    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(
        serve, "_http_post", lambda url, payload, timeout_s: (200, json.dumps({"choices": [{"message": {"content": "ok"}}]}))
    )
    assert serve.handle_message("oi", _ctx(tmp_path)) == "ok"


def test_chat_tenta_segundo_nome_de_modelo_em_4xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    # Candidatos forçados: t0 e DEFAULT_MODEL agora são o mesmo `bonsai`.
    monkeypatch.setattr(serve, "chat_model_candidates", lambda: ["bonsai", "bonsai-alt"])

    respostas = iter(
        [(404, ""), (200, json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]}))]
    )
    chamados: list[str] = []

    def fake_http_post(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
        chamados.append(payload["model"])
        return next(respostas)

    monkeypatch.setattr(serve, "_http_post", fake_http_post)
    assert serve.llm_reply("oi", ctx) == "resposta do modelo"
    assert len(chamados) == 2
    assert chamados[0] != chamados[1]

    chamados_timeout: list[str] = []

    def fake_http_post_timeout(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
        chamados_timeout.append(payload["model"])
        return 0, ""

    monkeypatch.setattr(serve, "_http_post", fake_http_post_timeout)
    assert serve.llm_reply("oi", ctx) is None
    assert len(chamados_timeout) == 1


def test_do_pinado_forca_backend_no_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    capturados: list[list[str]] = []

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
        capturados.append(argv)
        return 424242

    monkeypatch.setattr(serve, "_popen", fake_popen)

    ex = serve.Executor(
        id="harness:haiku", tier="t1", backend="claude_code", run_model="haiku", local=False, label="",
    )
    ctx = serve.ServeContext(cwd=tmp_path, executor=ex)

    resp = serve.handle_message("/do conserta o bug", ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert "--route" not in argv
    assert argv[argv.index("--backend") + 1] == "claude_code"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "PAGO" in resp
    assert "5.00" in resp
    jobs = serve.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["executor"] == "harness:haiku"


def test_do_auto_nao_pina_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    capturados: list[list[str]] = []
    monkeypatch.setattr(serve, "_popen", lambda argv, **kw: capturados.append(argv) or 424242)

    ctx = _ctx(tmp_path)  # sem executor pinado => auto
    serve.handle_message("/do conserta o bug", ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert "--backend" not in argv
    assert "--model" not in argv
    jobs = serve.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["executor"] == "harness"


def test_argv_nunca_recebe_texto_do_cliente() -> None:
    assert serve.resolve_executor("harness:local; rm -rf /") is None
    assert serve.resolve_executor("../../etc") is None
    # Acima do teto de tamanho: nem chega a comparar contra o registro.
    assert serve.resolve_executor("harness:" + "x" * 200) is None


def test_comando_models_lista(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tiers(monkeypatch)
    ctx = serve.ServeContext(cwd=tmp_path, executor=serve.resolve_executor("harness:local"))
    resp = serve.handle_message("/models", ctx)
    assert "harness" in resp
    assert "→ harness:local" in resp
    assert "chat + /do" in resp


def test_where_mostra_alias_pedido_vs_id_resolvido(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `requested_model` existe pra isto: o cliente pode pedir o alias oculto
    # (`harness:t0`), e o que resolve é o id primário (`harness:local`) — se
    # `/where` colapsasse os dois no mesmo valor essa distinção morreria.
    _fake_tiers(monkeypatch)
    ex = serve.resolve_executor("harness:t0")
    assert ex is not None
    assert ex.id == "harness:local"
    ctx = serve.ServeContext(cwd=tmp_path, executor=ex, requested_model="harness:t0")
    resp = serve.handle_message("/where", ctx)
    assert "executor pedido: harness:t0 → harness:local" in resp


# --------------------------------------------------------------------------- inspeção de requests: --verbose/--debug-dump


def test_default_off_sem_dump_sem_linha_verbose(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Nenhuma das duas flags: zero diferença do comportamento de hoje — nem
    # arquivo de dump nem linha verbose (só o boot print de sempre no stderr,
    # por isso o assert é num marcador da linha verbose, não `err == ""`).
    dump_path = tmp_path / "dump.jsonl"
    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(port, {"model": "harness", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert "/do" in json.loads(raw)["choices"][0]["message"]["content"]
    err = capsys.readouterr().err
    assert "msgs=" not in err
    assert "· rota=" not in err
    assert not dump_path.exists()


def test_verbose_uma_linha_por_request_sem_vazar_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    port, t = _serve(tmp_path, max_requests=1, api_key="segredo-verboso", verbose=True)
    status, _raw, _ = _post(
        port,
        {"model": "harness", "messages": [{"role": "user", "content": "/help"}]},
        headers={"Authorization": "Bearer segredo-verboso"},
    )
    t.join(5)
    assert status == 200
    err = capsys.readouterr().err
    assert "POST /v1/chat/completions" in err
    assert "model=harness→harness" in err
    assert "msgs=1" in err
    assert "rota=/comando" in err
    assert "status=200" in err
    assert "segredo-verboso" not in err  # nem a key, nem "Bearer ..." — conteúdo nenhum de auth


def test_verbose_cobre_401(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Auth falha ANTES do corpo ser lido — a linha ainda sai, com os campos
    # que dá pra saber nesse ponto (method/path/status), o resto em "-".
    port, t = _serve(tmp_path, max_requests=1, api_key="segredo", verbose=True)
    status, _ = _get(port, "/v1/models")
    t.join(5)
    assert status == 401
    err = capsys.readouterr().err
    assert "GET /v1/models" in err
    assert "status=401" in err
    assert "segredo" not in err


def test_debug_dump_grava_jsonl_com_auth_mascarada(tmp_path: Path) -> None:
    dump_path = tmp_path / "dump.jsonl"
    port, t = _serve(tmp_path, max_requests=1, api_key="segredo-dump", debug_dump=dump_path)
    status, _raw, _ = _post(
        port,
        {"model": "harness", "messages": [{"role": "user", "content": "/help"}]},
        headers={"Authorization": "Bearer segredo-dump"},
    )
    t.join(5)
    assert status == 200
    lines = dump_path.read_text(encoding="utf-8").strip("\n").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["method"] == "POST"
    assert entry["status"] == 200
    assert isinstance(entry["ts"], int)
    assert entry["headers"]["Authorization"] == "***"
    assert entry["body"]["model"] == "harness"
    assert entry["body"]["messages"][0]["content"] == "/help"


def test_debug_dump_dir_ilegivel_nao_derruba_a_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # PATH é um diretório: `open(..., "a")` explode com OSError — a request
    # segue 200 numa boa, só um aviso no stderr.
    dump_dir = tmp_path / "dump_dir"
    dump_dir.mkdir()
    port, t = _serve(tmp_path, max_requests=1, debug_dump=dump_dir)
    status, raw, _ = _post(port, {"model": "harness", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert "/do" in json.loads(raw)["choices"][0]["message"]["content"]
    err = capsys.readouterr().err
    assert "ilegível" in err


# --------------------------------------------------------------------------- inspeção de requests: --trace


def test_trace_off_sem_bloco_req_no_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Sem --trace: zero diferença do comportamento de hoje, nem o marcador
    # do bloco cru aparece no stderr.
    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(port, {"model": "harness", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    assert "/do" in json.loads(raw)["choices"][0]["message"]["content"]
    err = capsys.readouterr().err
    assert "───── REQ" not in err


def test_trace_mostra_req_e_resp_cru_sem_vazar_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    port, t = _serve(tmp_path, max_requests=1, api_key="segredo-trace", trace=True)
    status, _raw, _ = _post(
        port,
        {"model": "harness", "messages": [{"role": "user", "content": "/help"}]},
        headers={"Authorization": "Bearer segredo-trace"},
    )
    t.join(5)
    assert status == 200
    err = capsys.readouterr().err
    assert "───── REQ POST /v1/chat/completions HTTP/1.1" in err
    assert "Authorization: Bearer ***" in err
    assert '"content": "/help"' in err  # corpo cru da request, sem truncar
    assert "───── RESP 200" in err
    assert "/do" in err  # corpo cru da resposta
    assert "───── END" in err
    assert "segredo-trace" not in err  # a key nunca aparece, nem mascarada


def test_trace_cobre_401(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Auth falha ANTES do corpo ser lido — trace mostra a nota em vez do
    # corpo, mas o bloco REQ/RESP/END aparece inteiro do mesmo jeito.
    port, t = _serve(tmp_path, max_requests=1, api_key="segredo", trace=True)
    status, _ = _get(port, "/v1/models")
    t.join(5)
    assert status == 401
    err = capsys.readouterr().err
    assert "───── REQ GET /v1/models HTTP/1.1" in err
    assert "corpo: (não lido — refusado antes)" in err
    assert "───── RESP 401" in err
    assert "───── END" in err
    assert "segredo" not in err


def test_trace_metodo_nao_suportado_405(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    port, t = _serve(tmp_path, max_requests=1, trace=True)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    t.join(5)
    assert status == 405
    err = capsys.readouterr().err
    assert "───── REQ PUT /v1/chat/completions HTTP/1.1" in err
    assert "───── RESP 405" in err
    assert "suportado" in err  # `json.dumps` escapa acento (ensure_ascii): "método não suportado"
    assert "───── END" in err


def test_trace_sse_um_chunk_por_linha(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    port, t = _serve(tmp_path, max_requests=1, trace=True)
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
        resp.read()
    t.join(5)
    err = capsys.readouterr().err
    assert "───── RESP 200" in err
    assert err.count("sse> data: ") >= 2
    assert "sse> data: [DONE]" in err
    assert "───── END" in err


def test_debug_dump_ganha_campo_response(tmp_path: Path) -> None:
    dump_path = tmp_path / "dump.jsonl"
    port, t = _serve(tmp_path, max_requests=1, debug_dump=dump_path)
    status, _raw, _ = _post(port, {"model": "harness", "messages": [{"role": "user", "content": "/help"}]})
    t.join(5)
    assert status == 200
    entry = json.loads(dump_path.read_text(encoding="utf-8").strip("\n"))
    assert entry["response"]["status"] == 200
    assert "/do" in entry["response"]["body"]["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- gateway de ação (chat que dispara /do)


def _pin_local() -> serve.Executor:
    return serve.Executor(
        id="harness:local", tier="t0", backend="deepagents", run_model="openai:qwen/qwen3.5-9b",
        local=True, label="",
    )


def _pin_pago() -> serve.Executor:
    return serve.Executor(
        id="harness:haiku", tier="t1", backend="claude_code", run_model="haiku", local=False, label="",
    )


def test_chat_route_acao_vs_pergunta(tmp_path: Path) -> None:
    ctx_auto = _ctx(tmp_path)
    ctx_pin = serve.ServeContext(cwd=tmp_path, executor=_pin_local())

    acoes = [
        "Melhore o template de propostas, faça backup do codigo atual",
        "corrige o bug do formulário",
        "adiciona um campo de desconto",
    ]
    for texto in acoes:
        assert serve.chat_route(texto, ctx_auto) == serve.ROUTE_DISPATCH
        assert serve.chat_route(texto, ctx_pin) == serve.ROUTE_DISPATCH

    textos = [
        "como funciona o backup?",
        "o que faz esse arquivo?",
        "oi, tudo bem?",
        "mostra as tarefas prontas",
        "faz isso",  # afirmação solta — veto load-bearing, não vira task
        "só pergunta: melhorar o template faria sentido?",
    ]
    for texto in textos:
        assert serve.chat_route(texto, ctx_auto) == serve.ROUTE_TEXT


def test_acao_em_auto_dispara_no_executor_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    _fake_tiers(monkeypatch)

    def fail_http_post(*a, **kw):
        pytest.fail("`_http_post` não deveria ser chamado — ação dispara sem passar pelo LLM")

    monkeypatch.setattr(serve, "_http_post", fail_http_post)
    capturados: list[list[str]] = []

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
        capturados.append(argv)
        return 424242

    monkeypatch.setattr(serve, "_popen", fake_popen)
    ctx = _ctx(tmp_path)  # sem executor pinado => auto

    task = "Melhore o template de propostas, faça backup do codigo atual"
    resp = serve.handle_message(task, ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert task in argv
    assert "--no-apply" in argv
    assert argv[argv.index("--max-usd") + 1] == "5.00"
    assert argv[argv.index("--backend") + 1] == "deepagents"
    assert argv[argv.index("--model") + 1] == "openai:qwen/qwen3.5-9b"
    assert "job " in resp
    assert "/status" in resp
    assert "$0" in resp


def test_acao_com_pin_local_dispara_no_pinado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    def fail_http_post(*a, **kw):
        pytest.fail("`_http_post` não deveria ser chamado — ação dispara sem passar pelo LLM")

    monkeypatch.setattr(serve, "_http_post", fail_http_post)
    capturados: list[list[str]] = []
    monkeypatch.setattr(serve, "_popen", lambda argv, **kw: capturados.append(argv) or 1)

    ctx = serve.ServeContext(cwd=tmp_path, executor=_pin_local())
    resp = serve.handle_message("corrige o bug do formulário", ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert argv[argv.index("--backend") + 1] == "deepagents"
    assert "job " in resp


def test_acao_com_pin_pago_dispara_no_pago_com_aviso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    def fail_http_post(*a, **kw):
        pytest.fail("`_http_post` não deveria ser chamado — ação dispara sem passar pelo LLM")

    monkeypatch.setattr(serve, "_http_post", fail_http_post)
    capturados: list[list[str]] = []
    monkeypatch.setattr(serve, "_popen", lambda argv, **kw: capturados.append(argv) or 1)

    ctx = serve.ServeContext(cwd=tmp_path, executor=_pin_pago())
    resp = serve.handle_message("adiciona um campo de desconto", ctx)
    assert len(capturados) == 1
    argv = capturados[0]
    assert argv[argv.index("--backend") + 1] == "claude_code"
    assert "PAGO" in resp
    assert "5.00" in resp


def test_pergunta_nao_dispara(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))

    def fail_popen(*a, **kw):
        pytest.fail("`_popen` não deveria ser chamado — é pergunta, não ação")

    monkeypatch.setattr(serve, "_popen", fail_popen)
    monkeypatch.setattr(
        serve,
        "_http_post",
        lambda url, payload, timeout_s: (
            200,
            json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]}),
        ),
    )

    ctx_pin = serve.ServeContext(cwd=tmp_path, executor=_pin_local())
    resp_pin = serve.handle_message("como funciona o backup?", ctx_pin)
    assert "resposta do modelo" in resp_pin

    ctx_auto = _ctx(tmp_path)
    resp_auto = serve.handle_message("o que faz esse arquivo?", ctx_auto)
    assert "resposta do modelo" in resp_auto


def test_opt_out_frase_e_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_pin = serve.ServeContext(cwd=tmp_path, executor=_pin_local())
    assert serve.chat_route("só pergunta: melhora o template", ctx_pin) == serve.ROUTE_TEXT

    def fail_popen(*a, **kw):
        pytest.fail("`_popen` não deveria ser chamado — opt-out por frase/env")

    monkeypatch.setattr(serve, "_popen", fail_popen)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(
        serve, "_http_post", lambda url, payload, timeout_s: (200, json.dumps({"choices": [{"message": {"content": "ok"}}]}))
    )
    resp_frase = serve.handle_message("só pergunta: melhora o template", ctx_pin)
    assert "ok" in resp_frase

    monkeypatch.setenv(serve.AUTO_DO_ENV, "0")
    ctx_auto = _ctx(tmp_path)
    assert serve.chat_route("corrige o bug do formulário", ctx_auto) == serve.ROUTE_TEXT
    resp_env = serve.handle_message("corrige o bug do formulário", ctx_auto)
    assert "ok" in resp_env


def test_regenerate_nao_duplica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    chamadas = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamadas.append(1) or 1)
    monkeypatch.setattr(serve, "job_running", lambda job: True)
    ctx = _ctx(tmp_path)

    task = "corrige o bug do formulário"
    primeiro = serve.handle_message(task, ctx)
    assert "job " in primeiro
    assert len(chamadas) == 1

    segundo = serve.handle_message(task, ctx)
    assert "já tem um job rodando" in segundo
    assert len(chamadas) == 1  # regenerate segundos depois não dispara um segundo _popen


def test_system_prompt_manda_nao_ensinar_passo_a_passo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    ws = _git_repo(tmp_path, "gateway-prompt")
    det = serve.validate_workspace(str(ws), (ws.parent.resolve(),))
    assert det.verdict == "ok"

    prompt_detectado = serve.system_prompt(tmp_path, det)
    assert "NÃO ensine o passo a passo" in prompt_detectado

    prompt_sem_deteccao = serve.system_prompt(tmp_path)
    assert "NÃO ensine o passo a passo" not in prompt_sem_deteccao


def test_rota_verbose_mostra_o_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: 424242)
    port, t = _serve(tmp_path, max_requests=1, verbose=True)
    status, _raw, _ = _post(
        port,
        {"model": "harness", "messages": [{"role": "user", "content": "corrige o bug do formulário"}]},
    )
    t.join(5)
    assert status == 200
    err = capsys.readouterr().err
    assert "rota=ação→do" in err


def test_e2e_acao_pelo_shape_do_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "cursorws-acao")
    capturados: list[list[str]] = []

    def fake_popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
        capturados.append(argv)
        return 424242

    monkeypatch.setattr(serve, "_popen", fake_popen)
    port, t = _serve(tmp_path, max_requests=1, workspace_roots=(ws.parent,))

    task = "Melhore o template de propostas, faça backup do codigo atual"
    status, raw, _ = _post(port, _cursor_body(ws, task))
    t.join(5)
    assert status == 200
    content = json.loads(raw)["choices"][0]["message"]["content"]
    assert len(capturados) == 1
    assert task in capturados[0]
    assert "job " in content


# --------------------------------------------------------------------------- protocolo nativo do Cursor (tool_calls, harness-core-z8n)


def _shell_tool_call(command: str, call_id: str | None = "call_hx_test1") -> dict:
    call: dict = {
        "type": "function",
        "function": {"name": serve.cursor_tools.SHELL_FN, "arguments": json.dumps({"command": command})},
    }
    if call_id is not None:
        call["id"] = call_id
    return {"role": "assistant", "content": None, "tool_calls": [call]}


def _tool_result(text: str, call_id: str | None = "call_hx_test1") -> dict:
    msg: dict = {"role": "tool", "content": text}
    if call_id is not None:
        msg["tool_call_id"] = call_id
    return msg


def test_acao_com_tools_responde_tool_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    chamado: list[object] = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append(1) or 1)

    messages = [{"role": "user", "content": "conserta o bug do checkout"}]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.kind == "ação→tool_calls"
    assert len(turn.tool_calls) == 2
    shell = next(c for c in turn.tool_calls if c["function"]["name"] == serve.cursor_tools.SHELL_FN)
    args = json.loads(shell["function"]["arguments"])
    assert args["working_directory"] == str(ctx.cwd)
    assert chamado == []
    jobs_dir = tmp_path / "data" / "serve" / "jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.glob("*.json"))

    payload = serve.completion_payload(turn.text, cid="x", created=1, tool_calls=turn.tool_calls or None)
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


def test_tool_result_vira_resumo_final(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    command = serve.cursor_tools.build_command("/usr/bin/python3", "/cfg", "/data", "conserta o bug", 5.0, [])
    messages = [
        {"role": "user", "content": "conserta o bug"},
        _shell_tool_call(command),
        _tool_result(
            "Exit code: 0\n\nCommand output:\n\n```\nresultado  ACEITO em 42.3s · 3 arquivo(s) · "
            "--no-apply: ficou em harness/do-abc123\n```"
        ),
    ]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.kind == "desfecho"
    assert turn.tool_calls == ()
    payload = serve.completion_payload(turn.text, cid="x", created=1, tool_calls=turn.tool_calls or None)
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert "`harness/do-abc123`" in turn.text
    for proibida in _PROIBIDAS:
        assert proibida not in turn.text


def test_sem_tools_caminho_legado_identico(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    capturados: list[object] = []
    # pid falso alto de propósito (mesma convenção de `test_do_ok_dispara_um_job`):
    # pid 1 sob root faria `job_running` devolver True de verdade e o segundo
    # dispatch cairia em "já tem um job rodando", quebrando a comparação.
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: capturados.append(1) or 424242)
    # jid fixo: a mesma task disparada duas vezes (uma via `plan_turn`, uma
    # via `handle_message` direto) tem que render o MESMO texto — `started_at`
    # não entra no texto (só no record em disco), só o `id` do job entra.
    monkeypatch.setattr(serve.uuid, "uuid4", lambda: uuid.UUID(int=42))

    messages = [{"role": "user", "content": "conserta o bug do checkout"}]
    body = {"model": "harness", "messages": messages}  # sem "tools" — legado
    turn = serve.plan_turn(messages, body, ctx)
    assert turn.verbatim is True
    assert len(capturados) == 1

    esperado = serve.handle_message("conserta o bug do checkout", ctx)
    assert len(capturados) == 2
    assert turn.text == esperado


def test_com_tools_status_ganha_quebra_dupla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, "hc-1 tarefa"))
    messages = [{"role": "user", "content": "/status"}]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    tools_ok = serve.cursor_tools.supports_tools(body)
    turn = serve.plan_turn(messages, body, ctx)
    text = turn.text if (turn.verbatim or not tools_ok) else serve.md_paragraphs(turn.text)

    assert turn.tool_calls == ()
    assert "\n\n" in text


def test_pergunta_livre_com_tools_vai_pro_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_bd", lambda *a, **kw: (0, ""))
    llm_body = json.dumps({"choices": [{"message": {"content": "resposta do modelo"}}]})
    monkeypatch.setattr(serve, "_http_post", lambda url, payload, timeout_s: (200, llm_body))

    messages = [{"role": "user", "content": "oi, como vai a fila?"}]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.tool_calls == ()
    assert turn.verbatim is True
    assert turn.text == "resposta do modelo"
    payload = serve.completion_payload(turn.text, cid="x", created=1, tool_calls=None)
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_stream_tool_calls_grammar() -> None:
    calls = (
        serve.cursor_tools.tool_call(serve.cursor_tools.TODO_FN, {"todos": [], "merge": False}, 0),
        serve.cursor_tools.tool_call(serve.cursor_tools.SHELL_FN, {"command": "echo oi"}, 1),
    )
    frames = list(serve.stream_chunks("Disparando o pedido", cid="x", created=7, tool_calls=calls))
    assert frames[-1] == b"data: [DONE]\n\n"
    parsed = [json.loads(f[len(b"data: ") : -2]) for f in frames[:-1]]
    for p in parsed:
        choice = p["choices"][0]
        assert "index" in choice
        assert "finish_reason" in choice

    first = parsed[0]["choices"][0]
    assert first["delta"] == {"role": "assistant", "content": ""}
    assert first["finish_reason"] is None

    tool_frames = [p["choices"][0] for p in parsed if "tool_calls" in p["choices"][0]["delta"]]
    assert len(tool_frames) == 2
    arguments_por_indice = {}
    for i, choice in enumerate(tool_frames):
        assert choice["finish_reason"] is None
        tc = choice["delta"]["tool_calls"][0]
        assert tc["index"] == i
        arguments_por_indice[tc["index"]] = tc["function"]["arguments"]
    assert json.loads(arguments_por_indice[0]) == {"todos": [], "merge": False}
    assert json.loads(arguments_por_indice[1]) == {"command": "echo oi"}

    final = parsed[-1]["choices"][0]
    assert final["delta"] == {}
    assert final["finish_reason"] == "tool_calls"


def test_task_com_metacaracteres_fica_um_argumento_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: 1)

    task = "x'; rm -rf ~ #"
    messages = [{"role": "user", "content": f"/do {task}"}]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    shell = next(c for c in turn.tool_calls if c["function"]["name"] == serve.cursor_tools.SHELL_FN)
    cmd = json.loads(shell["function"]["arguments"])["command"]
    tokens = shlex.split(cmd)

    ex = serve.chat_executor(ctx)
    esperado_cmd = serve.cursor_tools.build_command(
        sys.executable,
        str(serve.paths.config_dir().resolve()),
        str(serve.store.data_dir().resolve()),
        task,
        serve.MAX_USD_CAP,
        serve.executor_argv(ex),
    )
    esperado = shlex.split(esperado_cmd)
    assert tokens == esperado
    assert len(tokens) == len(esperado)


def test_tools_sem_shell_cai_no_legado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: 1)

    tools = [t for t in _fixture_tools() if t["function"]["name"] != serve.cursor_tools.SHELL_FN]
    messages = [{"role": "user", "content": "/do conserta o bug"}]
    body = {"model": "harness", "tools": tools, "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.tool_calls == ()
    assert turn.verbatim is True


def test_shell_com_required_desconhecido_cai_no_legado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: 1)

    tools = []
    for t in _fixture_tools():
        if t["function"]["name"] == serve.cursor_tools.SHELL_FN:
            schema = dict(t["function"]["parameters"])
            schema["required"] = ["command", "surpresa_desconhecida"]
            t = {"type": "function", "function": {"name": serve.cursor_tools.SHELL_FN, "parameters": schema}}
        tools.append(t)
    messages = [{"role": "user", "content": "/do conserta o bug"}]
    body = {"model": "harness", "tools": tools, "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.tool_calls == ()
    assert turn.verbatim is True


def test_todowrite_schema_alien_emite_so_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: 1)

    tools = []
    for t in _fixture_tools():
        if t["function"]["name"] == serve.cursor_tools.TODO_FN:
            t = {
                "type": "function",
                "function": {
                    "name": serve.cursor_tools.TODO_FN,
                    "parameters": {"properties": {"todos": {"type": "string"}}},
                },
            }
        tools.append(t)
    messages = [{"role": "user", "content": "/do conserta o bug"}]
    body = {"model": "harness", "tools": tools, "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["function"]["name"] == serve.cursor_tools.SHELL_FN


def test_tool_result_incompleto_nao_mente(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    command = serve.cursor_tools.build_command("/usr/bin/python3", "/cfg", "/data", "faz algo", 5.0, [])
    messages = [
        {"role": "user", "content": "faz algo"},
        _shell_tool_call(command),
        _tool_result("Exit code: 0\n\nCommand output:\n\n```\nrodando ainda, sem linha de fechamento\n```"),
    ]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.kind == "desfecho"
    assert "/status" not in turn.text
    assert "segundo plano" in turn.text
    assert "`harness report`" in turn.text


def test_regenerate_nao_redispara(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    chamado: list[object] = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append(1) or 1)
    command = serve.cursor_tools.build_command("/usr/bin/python3", "/cfg", "/data", "conserta o bug", 5.0, [])
    tail = "<timestamp>x</timestamp>\n<user_query>\nconserta o bug\n</user_query>"
    messages = [
        {"role": "user", "content": "conserta o bug"},
        _shell_tool_call(command),
        _tool_result("Exit code: 0\n\nCommand output:\n\n```\nresultado  ACEITO em 1.0s · ficou em harness/do-x\n```"),
        {"role": "user", "content": [{"type": "text", "text": tail}]},
    ]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.kind == "desfecho"
    assert turn.tool_calls == ()
    assert chamado == []


def test_job_do_servidor_rodando_bloqueia_tool_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(serve, "list_jobs", lambda: [{"id": "existing-job"}])
    monkeypatch.setattr(serve, "job_running", lambda job: True)
    chamado: list[object] = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append(1) or 1)

    messages = [{"role": "user", "content": "/do conserta o bug"}]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.tool_calls == ()
    assert "já tem um job rodando" in turn.text
    assert chamado == []


def test_tool_result_content_string_e_parts(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    command = serve.cursor_tools.build_command("/usr/bin/python3", "/cfg", "/data", "faz algo", 5.0, [])
    assistant = _shell_tool_call(command)
    resultado_txt = "Exit code: 0\n\nCommand output:\n\n```\nresultado  ACEITO em 1.0s\n```"
    variantes = (
        {"role": "tool", "tool_call_id": "call_hx_test1", "content": resultado_txt},
        {"role": "tool", "name": serve.cursor_tools.SHELL_FN, "tool_call_id": "call_hx_test1", "content": resultado_txt},
        {
            "role": "tool",
            "tool_call_id": "call_hx_test1",
            "content": [{"type": "text", "text": resultado_txt}],
        },
        {
            "role": "tool",
            "name": serve.cursor_tools.SHELL_FN,
            "tool_call_id": "call_hx_test1",
            "content": [{"type": "text", "text": resultado_txt}],
        },
    )
    for tool_msg in variantes:
        messages = [{"role": "user", "content": "faz algo"}, assistant, tool_msg]
        body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
        turn = serve.plan_turn(messages, body, ctx)
        assert turn.kind == "desfecho"
        assert "ACEITO" in turn.text


def test_nonstream_tool_calls_shape() -> None:
    call = serve.cursor_tools.tool_call(serve.cursor_tools.SHELL_FN, {"command": "echo oi"}, 0)

    sem_preambulo = serve.completion_payload("", cid="x", created=1, tool_calls=(call,))
    msg = sem_preambulo["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["refusal"] is None
    assert "index" not in msg["tool_calls"][0]
    assert sem_preambulo["choices"][0]["finish_reason"] == "tool_calls"

    com_preambulo = serve.completion_payload("Disparando...", cid="x", created=1, tool_calls=(call,))
    msg2 = com_preambulo["choices"][0]["message"]
    assert msg2["content"] == "Disparando..."
    assert msg2["refusal"] is None


def test_desfecho_com_shape_realista_da_fixture(tmp_path: Path) -> None:
    """`summary_target`/`plan_turn` sobre o shape REAL de uma captura do
    Cursor: `<user_info>` antes do assistant (não logo antes do tool result),
    `role=tool` com `name`+`tool_call_id` (`_follow_up_example` da fixture,
    verbatim) — não a lista mínima `[user, assistant, tool]` dos testes
    diretos de `cursor_tools`."""
    ctx = _ctx(tmp_path)
    task = "melhore o template e faça backup"
    command = serve.cursor_tools.build_command("/usr/bin/python3", "/cfg", "/data", task, 5.0, [])
    user_info = (
        "<user_info>\nWorkspace Path: /Users/exemplo/projects/projeto-exemplo\n"
        "Is directory a git repo: Yes, at /Users/exemplo/projects/projeto-exemplo\n</user_info>"
    )
    tail = f"<timestamp>Thursday, Aug 6, 2026, 3:40 PM (UTC-3)</timestamp>\n<user_query>\n{task}\n</user_query>"
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_example123",
                "type": "function",
                "function": {"name": serve.cursor_tools.SHELL_FN, "arguments": json.dumps({"command": command})},
            }
        ],
    }
    messages = [
        {"role": "system", "content": "system prompt do Cursor"},
        {"role": "user", "content": user_info},
        {"role": "user", "content": [{"type": "text", "text": tail}]},
        assistant,
        _fixture()["_follow_up_example"]["message"],
    ]
    body = {"model": "harness", "tools": _fixture_tools(), "messages": messages}
    turn = serve.plan_turn(messages, body, ctx)

    assert turn.kind == "desfecho"
    assert turn.tool_calls == ()
    assert "`harness/do-exemplo-abc123`" in turn.text
    for proibida in _PROIBIDAS:
        assert proibida not in turn.text


def test_e2e_tools_dispara_tool_calls_pelo_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-trip HTTP de verdade (não chamada direta de `plan_turn`) — cobre
    a fiação real de `_do_POST_inner`: `tools_ok`, `turn.tool_calls or None`
    (uma tupla vazia viraria o branch de tool_calls por engano — `or None` é
    o que evita isso), `_stream`/`_json` recebendo `tool_calls`, e
    `_log_state["rota"]` == `turn.kind` no `--verbose`."""
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    ws = _git_repo(tmp_path, "cursor-tools-e2e")
    chamado: list[object] = []
    monkeypatch.setattr(serve, "_popen", lambda *a, **kw: chamado.append(1) or 1)
    port, t = _serve(tmp_path, max_requests=1, workspace_roots=(ws.parent,), verbose=True)

    body = _cursor_body(ws, "conserta o bug do checkout")
    body["tools"] = _fixture_tools()
    status, raw, _ = _post(port, body)
    t.join(5)

    assert status == 200
    payload = json.loads(raw)
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    calls = choice["message"]["tool_calls"]
    assert len(calls) == 2
    preambulo = choice["message"]["content"]
    assert isinstance(preambulo, str) and preambulo
    for proibida in _PROIBIDAS:
        assert proibida not in preambulo
    assert chamado == []

    err = capsys.readouterr().err
    assert "rota=ação→tool_calls" in err


# --------------------------------------------------------------------------- bonsai proxy (Cursor Agent)


def test_bonsai_proxy_nonstream_remaps_reasoning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    upstream = {
        "id": "chatcmpl-up",
        "object": "chat.completion",
        "model": "default",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "feito",
                    "reasoning": "pensei",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_proxy(payload, *, timeout_s):
        assert payload.get("model") == "qwopus3.5-4b-coder-mtp"
        assert payload.get("chat_template_kwargs", {}).get("enable_thinking") is True
        assert "messages" in payload
        assert "input" not in payload
        assert isinstance(payload.get("tools"), list)
        return 200, json.dumps(upstream).encode("utf-8")

    monkeypatch.setattr(serve, "_proxy_post_json", fake_proxy)
    port, t = _serve(tmp_path, max_requests=1)
    status, raw, _ = _post(
        port,
        {
            "model": "bonsai",
            "input": [{"role": "user", "content": "hi", "type": "message"}],
            "tools": [{"type": "function", "name": "Shell", "parameters": {}}],
        },
    )
    t.join(5)
    assert status == 200
    body = json.loads(raw)
    assert body["model"] == "qwopus3.5-4b-coder-mtp"
    msg = body["choices"][0]["message"]
    assert msg["content"] == "feito"
    assert msg["reasoning_content"] == "pensei"
    assert "reasoning" not in msg


def test_bonsai_proxy_stream_remaps_reasoning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    def fake_sse(payload, *, timeout_s):
        assert payload.get("stream") is True
        yield (
            b'data: {"id":"c","object":"chat.completion.chunk","model":"default",'
            b'"choices":[{"index":0,"delta":{"reasoning":"r1"},"finish_reason":null}]}'
        )
        yield (
            b'data: {"id":"c","object":"chat.completion.chunk","model":"default",'
            b'"choices":[{"index":0,"delta":{"content":"oi"},"finish_reason":null}]}'
        )
        yield b"data: [DONE]"

    monkeypatch.setattr(serve, "_iter_upstream_sse", fake_sse)
    port, thr = _serve(tmp_path, max_requests=1)
    status, raw, ctype = _post(
        port,
        {"model": "bonsai", "stream": True, "messages": [{"role": "user", "content": "x"}]},
    )
    thr.join(5)
    assert status == 200
    assert "text/event-stream" in ctype
    text = raw.decode("utf-8")
    assert "reasoning_content" in text
    assert "oi" in text
    assert "[DONE]" in text
