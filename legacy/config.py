#!/usr/bin/env python3
"""config.py — configuração local do harness. stdlib only.

Precedência (mais forte primeiro):
    1. variáveis de ambiente HARNESS_*
    2. <repo>/.harness/config.toml        (config do projeto, versionável ou não)
    3. ~/.config/harness-core/config.toml (config da máquina: allowlist, paths)
    4. defaults deste arquivo

Nada aqui tem efeito de rede. Segredo não mora no repo: a allowlist e o número
do dono vivem em ~/.config, fora do git.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
USER_CONFIG = Path(os.environ.get("HARNESS_CONFIG_HOME", Path.home() / ".config" / "harness-core"))

DEFAULTS = {
    "whatsapp": {
        "service_url": "http://127.0.0.1:8787",
        "allowlist": [],          # JIDs autorizados. VAZIO = nada entra, nada sai.
        "owner": "",              # JID do dono, para comandos de assist
        # Política de outbound. FALSE por default e é assim que tem que ficar:
        # com auto-reply ligado, qualquer bug no parser de comandos vira mensagem
        # enviada sozinha. Ligue só conscientemente.
        "allow_auto_reply_to_owner": False,
        "inbox_path": str(ROOT / "channel" / "whatsapp" / "inbox.jsonl"),
        "timeout_s": 20,
    },
    "harness": {
        "version_pin": "",        # .harness/ de um projeto pode pinar uma versão
        "results_dir": "",
        # autopilot (D6): tetos do loop sem supervisão. Os três primeiros são
        # o freio — o loop para no PRIMEIRO que estourar, não no último.
        "autopilot_wall_clock_s": 1200,
        "autopilot_budget_usd": 1.00,
        "autopilot_max_iterations": 30,
        "autopilot_self_every": 3,       # a cada N passos de fila, 1 passo de auto-evolução
        "autopilot_probation_runs": 3,   # runs de observação depois de um merge
        # Escrita fora da raiz do repo é a única forma de o autopilot estragar
        # algo que não é dele. FALSE por default e é assim que tem que ficar.
        "autopilot_allow_external_work_path": False,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"config inválido em {p}: {e}")


def load(repo: Path | None = None) -> dict:
    repo = repo or ROOT
    cfg = _merge(DEFAULTS, _read_toml(USER_CONFIG / "config.toml"))
    cfg = _merge(cfg, _read_toml(repo / ".harness" / "config.toml"))

    wa = cfg["whatsapp"]
    if os.environ.get("HARNESS_WA_URL"):
        wa["service_url"] = os.environ["HARNESS_WA_URL"]
    if os.environ.get("HARNESS_WA_ALLOWLIST"):
        wa["allowlist"] = [s.strip() for s in os.environ["HARNESS_WA_ALLOWLIST"].split(",") if s.strip()]
    if os.environ.get("HARNESS_WA_OWNER"):
        wa["owner"] = os.environ["HARNESS_WA_OWNER"]
    if os.environ.get("HARNESS_WA_INBOX"):
        wa["inbox_path"] = os.environ["HARNESS_WA_INBOX"]
    if os.environ.get("HARNESS_WA_AUTO_REPLY"):
        wa["allow_auto_reply_to_owner"] = os.environ["HARNESS_WA_AUTO_REPLY"].lower() in ("1", "true", "yes")

    h = cfg["harness"]
    if os.environ.get("HARNESS_AP_MINUTES"):
        h["autopilot_wall_clock_s"] = int(float(os.environ["HARNESS_AP_MINUTES"]) * 60)
    if os.environ.get("HARNESS_AP_BUDGET"):
        h["autopilot_budget_usd"] = float(os.environ["HARNESS_AP_BUDGET"])

    # O dono é sempre tratado como autorizado, mas nunca implicitamente:
    # se está configurado, entra na allowlist de forma explícita e visível.
    if wa["owner"] and wa["owner"] not in wa["allowlist"]:
        wa["allowlist"] = [*wa["allowlist"], wa["owner"]]
    return cfg


def example_toml() -> str:
    return """# ~/.config/harness-core/config.toml
[whatsapp]
service_url = "http://127.0.0.1:8787"
owner = "5511999999999@s.whatsapp.net"
allowlist = ["5511999999999@s.whatsapp.net"]
allow_auto_reply_to_owner = false   # true = respostas ao dono saem sem confirmar
"""


if __name__ == "__main__":
    import json

    c = load()
    c_show = _merge(c, {})
    print(json.dumps(c_show, indent=2, ensure_ascii=False))
    print(f"\n(user config: {USER_CONFIG / 'config.toml'} "
          f"{'existe' if (USER_CONFIG / 'config.toml').exists() else 'NÃO existe'})")
