#!/usr/bin/env python3
"""isolation.py — roda verify.py dentro de um container Docker descartável.

Degrau "isolamento sério" da escada (STATUS.md): o agente (claude CLI)
continua rodando FORA do container, editando o workspace normalmente; só o
VERIFY roda dentro, isolado e sem rede. run_task.py liga isso com
`--isolation docker` (default segue tmpdir puro, sem mudança).

Limitação conhecida: como o agente edita o workspace fora do container e o
verify roda dentro, qualquer diferença de ambiente entre o host e a imagem
(versão de python, libs instaladas) pode fazer uma task passar fora e falhar
dentro (ou vice-versa) — nenhuma das suites atuais (tasks/, benchmarks/sealed)
depende de nada além de stdlib, então isso não se manifesta hoje, mas uma
task que dependesse de um pacote pip presente só no host quebraria assim.

    docker build -t harness-runner:latest -f docker/Dockerfile.runner .
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_IMAGE = "harness-runner:latest"
DOCKERFILE = Path(__file__).parent / "docker" / "Dockerfile.runner"


def docker_available() -> bool:
    """True se o binário docker existe e o daemon responde."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ensure_image(image: str = DEFAULT_IMAGE, timeout: int = 300) -> None:
    """Builda a imagem se ela ainda não existir localmente. Idempotente."""
    check = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, timeout=10
    )
    if check.returncode == 0:
        return
    build = subprocess.run(
        ["docker", "build", "-t", image, "-f", str(DOCKERFILE), str(DOCKERFILE.parent.parent)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if build.returncode != 0:
        raise RuntimeError(f"docker build falhou: {build.stdout[-2000:]}\n{build.stderr[-2000:]}")


def run_verify_in_container(
    ws: Path, verify: Path, image: str = DEFAULT_IMAGE, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Roda `python3 verify.py` dentro de um container --rm, --network none,
    com ws montado em /ws (rw) e verify.py montado em /verify.py (ro).

    Path("algo") dentro de verify.py resolve contra o cwd do processo (/ws),
    não contra a localização do script — por isso o mount separado funciona
    sem precisar copiar verify.py para dentro do workspace.
    """
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "-v", f"{ws.resolve()}:/ws",
        "-v", f"{verify.resolve()}:/verify.py:ro",
        "-w", "/ws",
        image,
        "python3", "/verify.py",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"docker:timeout {e}")


if __name__ == "__main__":
    print("docker disponível" if docker_available() else "docker indisponível")
    sys.exit(0)
