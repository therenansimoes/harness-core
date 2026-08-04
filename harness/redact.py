"""Redação de segredo em texto que vira evidência.

Log de run é lido por humano, entra em relatório e às vezes volta para o
modelo: token que passeia por ali é vazamento permanente. `redact` roda em
cima de qualquer texto ANTES de ele ser gravado (`install-audit.log`,
`tests.log`, `setup.log`, tail de `proc-*.log`) e troca o segredo por `***`.

Duas famílias de padrão, de propósito:

- **forma conhecida** (`sk-…`, `ghp_…`, `AKIA…`, `xox…`, `Bearer …`,
  `SENHA=…`): pega segredo de terceiro que o comando cuspiu, mesmo o que este
  processo nunca viu.
- **valor do próprio env** cujo NOME casa `KEY|TOKEN|SECRET|PASSWORD`: pega o
  segredo de forma livre, que nenhum regex acerta. É o caso mais comum no
  loop — a chave está em `os.environ` e o subprocess a ecoou.

Nada aqui levanta: redação é higiene, não pode derrubar a gravação do log.
"""

from __future__ import annotations

import os
import re

MASK = "***"
# Abaixo disso não é segredo, é sufixo curto que apareceria em todo lugar do
# log (e mascarar `PATH`-like quebraria diagnóstico mais do que protege).
MIN_ENV_VALUE = 8
# Nome de env que denuncia segredo. Substring, não prefixo: `MY_API_TOKEN` e
# `OPENAI_API_KEY` casam igual.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|SENHA|CREDENTIAL", re.IGNORECASE)

# Ordem importa pouco (os padrões não se sobrepõem), mas mantida estável para
# o teste poder afirmar o resultado exato.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI/Anthropic e afins: sk-…, sk-ant-…, sk-proj-…
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), MASK),
    # GitHub: ghp_ (PAT), gho_/ghu_/ghs_/ghr_ (OAuth/app/refresh)
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), MASK),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    # Slack: xoxb-/xoxp-/xoxa-/xoxs-/xoxr-
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    # Google API key
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    # Authorization: Bearer <tok> — o esquema fica, o token não.
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # `NOME=valor` / `NOME: valor` com nome de segredo. `[^\s'\"]+` para não
    # comer a linha inteira nem as aspas que delimitam o valor.
    (
        re.compile(
            r"(?P<name>[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|SENHA)"
            r"[A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*)(?P<q>[\"']?)(?P<val>[^\s\"']+)",
            re.IGNORECASE,
        ),
        r"\g<name>\g<sep>\g<q>" + MASK,
    ),
)


def env_secrets(env: dict[str, str] | None = None) -> list[str]:
    """Valores de env que não podem aparecer em log. Mais longo primeiro: um
    valor que seja prefixo de outro não pode mascarar meia string do maior."""
    src = os.environ if env is None else env
    vals = {
        v
        for k, v in src.items()
        if SECRET_NAME.search(k) and isinstance(v, str) and len(v.strip()) >= MIN_ENV_VALUE
    }
    return sorted((v.strip() for v in vals), key=len, reverse=True)


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """`text` com todo segredo reconhecido trocado por `***`.

    Idempotente na prática: `***` não casa nenhum padrão, reaplicar não muda
    nada — o mesmo log pode passar por aqui duas vezes sem estragar.
    """
    if not text:
        return text
    out = text
    for valor in env_secrets(env):
        out = out.replace(valor, MASK)
    for pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out
