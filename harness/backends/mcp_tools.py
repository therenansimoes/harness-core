"""Tools MCP para o executor deepagents, lidas de config/mcp.toml.

Contrato: `load_mcp_tools` retorna lista de tools LangChain-compatíveis e
NUNCA levanta — arquivo ausente, toml quebrado, tudo desabilitado, extra
`langchain-mcp-adapters` desinstalado ou erro de conexão viram `[]` com uma
linha no stderr. O import do adapter é lazy (mesma regra do resto do repo:
LangChain nunca no topo).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def load_mcp_tools(config_path: str | Path = "config/mcp.toml") -> list:
    try:
        path = Path(config_path)
        if not path.is_file():
            return []

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        connections = _connections(data.get("servers", {}))
        if not connections:
            return []

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            print(
                "mcp_tools: langchain-mcp-adapters não instalado; servidores MCP ignorados",
                file=sys.stderr,
            )
            return []

        client = MultiServerMCPClient(connections)
        tools = _run_coro(client.get_tools())
        return list(tools) if tools else []
    except Exception as exc:  # broad de propósito: MCP é opcional, nunca derruba o run
        print(f"mcp_tools: falha ao carregar tools MCP: {exc}", file=sys.stderr)
        return []


def _connections(servers: dict) -> dict:
    """Monta o dict de conexões do MultiServerMCPClient; só entra enabled = true."""
    connections: dict = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        transport = cfg.get("transport", "stdio")
        if transport == "stdio":
            connections[name] = {
                "transport": "stdio",
                "command": cfg["command"],
                "args": list(cfg.get("args", [])),
            }
        elif transport == "streamable_http":
            connections[name] = {"transport": "streamable_http", "url": cfg["url"]}
        else:
            print(
                f"mcp_tools: servidor {name!r} com transport desconhecido {transport!r}, ignorado",
                file=sys.stderr,
            )
    return connections


def _run_coro(coro):
    """asyncio.run, mas seguro se já houver loop rodando (roda em thread própria)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
