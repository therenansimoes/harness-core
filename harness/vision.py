"""Olhos no próprio trabalho: um VLM local julga o SCREENSHOT, não o HTML.

O `ui-verify` prova que a tela renderizou (asset 200, PNG com tamanho de página
com conteúdo). Isso separa "não pintou nada" de "pintou" — e para aí. Nada ali
distingue uma página pintada e feia de uma pintada e boa, e é exatamente esse
buraco que sobrou depois que o gate de asset morto foi tapado.

Este módulo é a camada de cima: manda o PNG para um endpoint OpenAI-compatível
(mlx_lm.server na 1235 por default) com um modelo que enxerga, e pede nota + bullets
contra uma rubrica de 5 eixos. Determinístico não é — por isso NÃO é a régua
binária. É subcheck de peso pequeno na régua graduada (`[checks]` do unit.toml).

A regra que atravessa o arquivo inteiro: **sem visão, ninguém reprova**. Sem
bloco `[vision]` no `models.toml`, servidor mudo, HTTP 500, resposta que não é
JSON — tudo devolve `unavailable="visão indisponível"` com `ok=None`, exit 0 no
CLI. Um juiz probabilístico que reprova quando o servidor cai não é juiz, é
sorteio; e uma máquina sem modelo de visão instalado não pode ficar com a fila
travada por causa disso.

`compare_reference` existe porque nota absoluta em VLM pequeno é ruim: o mesmo
modelo dá 7 e 4 para a mesma tela em dois requests. Comparação PAREADA (as duas
imagens no MESMO request, "qual está melhor") é bem mais estável — é essa que
serve de régua quando existe um alvo visual para comparar.
"""

from __future__ import annotations

import base64
import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from harness.routing import config_dir

MODELS_FILE = "models.toml"
RUBRIC_FILE = "rubric.toml"
VISION_SECTION = "vision"

# Mesmo endpoint do t0 (mlx_lm.server :1235) e mesma env var de override:
# quem aponta o backend para outro servidor aponta a visão junto, por construção.
DEFAULT_BASE_URL = "http://127.0.0.1:1235/v1"
BASE_URL_ENV = "OPENAI_BASE_URL"
KEY_ENV = "OPENAI_API_KEY"
# `init_chat_model` quer o prefixo; o HTTP cru não. Aqui a string vai como id de
# modelo do endpoint, então o prefixo sai.
OPENAI_PREFIX = "openai:"

# Defaults calibrados para VLM que pensa antes de responder: o thinking consome
# o orçamento primeiro, e 700 tokens não sobravam nem para abrir o JSON. Ver a
# nota em `[vision]` no models.toml.
DEFAULT_TIMEOUT_S = 240.0
DEFAULT_MAX_TOKENS = 3000

UNAVAILABLE = "visão indisponível"

JUDGE_SHAPE = '{"nota": number 0-10, "ok": bool, "bullets": [str, ...]}'
COMPARE_SHAPE = '{"melhor": "a"|"b", "motivo": str}'

# Rubrica default: 5 eixos que um VLM pequeno consegue julgar OLHANDO, sem
# adivinhar intenção de produto. Calibrável em config/rubric.toml (zona mutável
# do genoma) — o loop pode afinar o texto, a régua binária não depende dele.
DEFAULT_RUBRIC = """1. HIERARQUIA: dá para saber em 1 segundo o que é título, o que é corpo e o
   que é ação? Tamanhos diferentes de propósito, ou tudo do mesmo tamanho?
2. CONTRASTE: texto legível sobre o fundo? Nada de cinza claro em branco.
3. ALINHAMENTO: elementos alinhados numa grade, margens iguais, nada
   encavalado nem saindo da tela.
4. ESTADO VAZIO: a página tem conteúdo de verdade, ou é placeholder/lorem
   ipsum/caixa vazia esperando dados?
5. RESPONSIVO: o conteúdo respeita a largura, sem barra horizontal, sem texto
   cortado nem linha de 200 caracteres."""


# --------------------------------------------------------------------------- config


def load_vision(config_path: Path | None = None) -> dict | None:
    """`[vision]` do models.toml, ou None quando não há visão configurada.

    None é o caminho NORMAL numa máquina sem modelo de visão carregado — não é
    erro, e por isso não levanta nada nem escreve em stderr.
    """
    path = Path(config_path) if config_path is not None else config_dir() / MODELS_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    block = data.get(VISION_SECTION)
    if not isinstance(block, dict):
        return None
    model = str(block.get("model") or "").strip()
    if not model:
        return None
    return {
        "model": model,
        "timeout_s": _num(block.get("timeout_s"), DEFAULT_TIMEOUT_S),
        "max_tokens": int(_num(block.get("max_tokens"), DEFAULT_MAX_TOKENS)),
    }


def load_rubric(config_path: Path | None = None) -> str:
    """Rubrica em texto. `config/rubric.toml` sobrescreve a embutida.

    Duas formas aceitas: `rubric = "texto livre"` (ganha) ou `[axes]` com
    nome -> descrição, que vira lista numerada. TOML torto cai na default: uma
    rubrica quebrada não deve derrubar o juiz que já é fail-open.
    """
    path = Path(config_path) if config_path is not None else config_dir() / RUBRIC_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DEFAULT_RUBRIC
    texto = data.get("rubric")
    if isinstance(texto, str) and texto.strip():
        return texto.strip()
    axes = data.get("axes")
    if isinstance(axes, dict) and axes:
        linhas = [
            f"{i}. {str(name).upper()}: {desc}"
            for i, (name, desc) in enumerate(axes.items(), start=1)
            if isinstance(desc, str) and desc.strip()
        ]
        if linhas:
            return "\n".join(linhas)
    return DEFAULT_RUBRIC


def _num(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


# --------------------------------------------------------------------------- juízo


def judge_image(
    png: Path | str,
    question: str | None = None,
    rubric: str | None = None,
) -> dict:
    """Nota 0-10 + bullets para UMA tela. Nunca levanta, nunca reprova sozinha.

    `unavailable` preenchido = não houve juízo (sem modelo, sem servidor,
    resposta ilegível). Nesse caso `ok` e `nota` são None e quem chama passa —
    ausência de juiz não é veredito negativo.
    """
    shot = Path(png)
    cfg = load_vision()
    if cfg is None:
        return _sem_juizo(f"sem [{VISION_SECTION}] em {MODELS_FILE}")
    data_uri = _data_uri(shot)
    if data_uri is None:
        return _sem_juizo(f"PNG ilegível: {shot}")

    prompt = _judge_prompt(question, rubric if rubric is not None else load_rubric())
    status, corpo = _chat(cfg, [_bloco(prompt, [data_uri])])
    if status != 200:
        return _sem_juizo(f"{base_url()} respondeu {_status(status)}")
    raw = _content(corpo)
    veredito = parse_judge(raw)
    if veredito is None:
        return _sem_juizo(_sem_forma(corpo, JUDGE_SHAPE), raw=raw)
    veredito["raw"] = raw
    veredito["unavailable"] = None
    return veredito


def compare_reference(
    png: Path | str,
    ref_png: Path | str,
    question: str | None = None,
) -> dict:
    """Comparação PAREADA: `a` = a tela nova, `b` = a referência, no MESMO request.

    Mais confiável que nota absoluta em VLM pequeno, e é a régua que interessa
    quando existe um alvo visual: "ficou melhor que isto?" tem resposta; "esta
    tela vale 7?" não tem.
    """
    cfg = load_vision()
    if cfg is None:
        return _sem_par(f"sem [{VISION_SECTION}] em {MODELS_FILE}")
    uri_a, uri_b = _data_uri(Path(png)), _data_uri(Path(ref_png))
    if uri_a is None or uri_b is None:
        faltando = png if uri_a is None else ref_png
        return _sem_par(f"PNG ilegível: {faltando}")

    prompt = _compare_prompt(question)
    status, corpo = _chat(cfg, [_bloco(prompt, [uri_a, uri_b])])
    if status != 200:
        return _sem_par(f"{base_url()} respondeu {_status(status)}")
    raw = _content(corpo)
    veredito = parse_compare(raw)
    if veredito is None:
        return _sem_par(_sem_forma(corpo, COMPARE_SHAPE), raw=raw)
    veredito["raw"] = raw
    veredito["unavailable"] = None
    return veredito


def _sem_juizo(motivo: str, raw: str = "") -> dict:
    return {
        "ok": None,
        "nota": None,
        "bullets": [],
        "raw": raw,
        "unavailable": f"{UNAVAILABLE} ({motivo})",
    }


def _sem_par(motivo: str, raw: str = "") -> dict:
    return {
        "melhor": None,
        "motivo": "",
        "raw": raw,
        "unavailable": f"{UNAVAILABLE} ({motivo})",
    }


def _status(code: int) -> str:
    return "sem resposta" if code == 0 else str(code)


# --------------------------------------------------------------------------- prompt


def _judge_prompt(question: str | None, rubric: str) -> str:
    extra = f"\nPergunta específica desta unidade: {question}\n" if question else ""
    return (
        "Você é revisor de interface. A imagem é o screenshot de uma página web "
        "renderizada em 1280x2000.\n\n"
        f"Julgue OLHANDO, contra estes eixos:\n{rubric}\n{extra}\n"
        "Nota 0-10: 0 = tela vazia ou ilegível, 5 = renderizou mas cru/desalinhado, "
        "8+ = hierarquia clara, contraste bom, alinhado e com conteúdo real. "
        "`ok` = a tela está aceitável para um humano ver.\n"
        "Cada bullet cita UM problema concreto e onde ele está — nada de elogio "
        "genérico.\n\n"
        f"Responda SOMENTE com JSON no formato {JUDGE_SHAPE}, sem texto em volta."
    )


def _compare_prompt(question: str | None) -> str:
    extra = f"\nCritério desta unidade: {question}\n" if question else ""
    return (
        "Você é revisor de interface. São dois screenshots da MESMA página: a "
        "primeira imagem é a versão A (nova), a segunda é a versão B "
        "(referência).\n\n"
        "Qual das duas está melhor de hierarquia, contraste, alinhamento e "
        f"conteúdo real?{extra}\n"
        "Se estiverem equivalentes, escolha B — empate não é melhoria.\n\n"
        f"Responda SOMENTE com JSON no formato {COMPARE_SHAPE}, sem texto em volta."
    )


def _bloco(texto: str, data_uris: list[str]) -> dict:
    """Mensagem multimodal do formato OpenAI: texto primeiro, imagens depois."""
    content: list[dict] = [{"type": "text", "text": texto}]
    content += [{"type": "image_url", "image_url": {"url": uri}} for uri in data_uris]
    return {"role": "user", "content": content}


def _data_uri(png: Path) -> str | None:
    try:
        raw = png.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- HTTP


def _chat(cfg: dict, messages: list[dict]) -> tuple[int, str]:
    model = str(cfg["model"]).removeprefix(OPENAI_PREFIX)
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": cfg["max_tokens"],
        # Juiz com temperatura alta muda de nota entre dois runs idênticos e
        # transforma o subcheck em ruído no score.
        "temperature": 0,
    }
    return _http_post(f"{base_url()}/chat/completions", payload, float(cfg["timeout_s"]))


def _http_post(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
    """(status, corpo). Status 0 = não respondeu.

    Indireção também para o teste trocar o servidor por um fake — mesma
    convenção do `uiverify.chrome()`.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    key = os.environ.get(KEY_ENV, "")
    if key:
        # LM Studio ignora; endpoint compatível na nuvem exige.
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def _content(corpo: str) -> str:
    """Texto da primeira choice. Corpo torto devolve string vazia, não exceção.

    Modelo com thinking (o qwen3.5 local pensa SEMPRE, e não desliga: medido, o
    LM Studio aceita `chat_template_kwargs enable_thinking=false`,
    `reasoning_effort=none` e `/no_think` no prompt e ignora os três) manda a
    prosa em `reasoning_content` e o JSON em `content`. Se o orçamento de
    `max_tokens` acabar no meio do thinking, `content` volta VAZIO com
    `finish_reason: length` e o juiz sumia sem motivo legível — 1943 tokens só de
    reasoning contra os 1600 que o bloco pedia. Quem conserta isso é o orçamento
    no models.toml; ler o reasoning aqui é a rede, para o caso do modelo fechar o
    JSON lá dentro em vez de no content.
    """
    try:
        msg = json.loads(corpo)["choices"][0]["message"]
        return str(msg["content"] or "") or str(msg.get("reasoning_content") or "")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        return ""


def _sem_forma(corpo: str, forma: str) -> str:
    """Motivo do parse falho. Truncagem e forma errada pedem conserto diferente:
    uma é `max_tokens` curto no models.toml, a outra é o modelo desobedecendo.
    """
    try:
        cortou = json.loads(corpo)["choices"][0].get("finish_reason") == "length"
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        cortou = False
    if cortou:
        return f"max_tokens acabou antes da resposta (esperado {forma})"
    return f"resposta não é {forma}"


# --------------------------------------------------------------------------- parse


def parse_judge(raw: str) -> dict | None:
    """JSON estrito, exceto pela cerca de código. None = resposta inútil.

    Modelo pequeno erra a forma: às vezes devolve o `{"ok","motivo"}` do
    `--ask`. Esse caso é aproveitado (`parse_ask` já sabe lê-lo) e vira nota
    sintética nos extremos, porque a informação que ele deu é binária.
    """
    obj = _loads(raw)
    if isinstance(obj, dict):
        nota = _nota(obj.get("nota"))
        if nota is not None:
            ok = obj.get("ok")
            return {
                "ok": bool(ok) if isinstance(ok, bool) else nota >= 6.0,
                "nota": nota,
                "bullets": _bullets(obj.get("bullets")),
            }
    from harness.uiverify import parse_ask  # lazy: só o fallback precisa

    fallback = parse_ask(raw)
    if fallback is None:
        return None
    ok, motivo = fallback
    return {"ok": ok, "nota": 8.0 if ok else 3.0, "bullets": [] if ok else [motivo]}


def parse_compare(raw: str) -> dict | None:
    obj = _loads(raw)
    if not isinstance(obj, dict):
        return None
    melhor = str(obj.get("melhor") or "").strip().lower()
    if melhor not in ("a", "b"):
        return None
    motivo = obj.get("motivo")
    return {"melhor": melhor, "motivo": motivo if isinstance(motivo, str) else ""}


def _loads(raw: str) -> object:
    """JSON de dentro da cerca ```json, ou do primeiro `{...}` do texto."""
    from harness.uiverify import _FENCE  # lazy: a cerca é a mesma do ui-verify

    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    ini, fim = text.find("{"), text.rfind("}")
    if ini < 0 or fim <= ini:
        return None
    try:
        return json.loads(text[ini : fim + 1])
    except json.JSONDecodeError:
        return None


def _nota(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        nota = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(10.0, nota))


def _bullets(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(b).strip() for b in value if str(b).strip()]


__all__ = [
    "DEFAULT_RUBRIC",
    "UNAVAILABLE",
    "base_url",
    "compare_reference",
    "judge_image",
    "load_rubric",
    "load_vision",
    "parse_compare",
    "parse_judge",
]
