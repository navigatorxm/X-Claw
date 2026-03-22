"""
XClaw MCP Client — connect external Model Context Protocol servers.

MCP (Model Context Protocol) lets you plug any MCP-compatible tool server
into XClaw. Each server exposes tools; XClaw registers them into its
ToolRegistry so the LLM can call them like any other tool.

Supported transports:
  stdio  — spawn a subprocess, communicate via JSON-RPC over stdin/stdout
  http   — connect to an HTTP MCP server (POST /messages)

Config file: mcp_servers.json (in the project root)

Example mcp_servers.json:
  {
    "servers": [
      {
        "name": "filesystem",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {}
      },
      {
        "name": "my-api",
        "type": "http",
        "url": "http://localhost:3001/mcp"
      }
    ]
  }

Popular MCP servers (all via npx, no install needed):
  @modelcontextprotocol/server-filesystem  — read/write local files
  @modelcontextprotocol/server-brave-search — web search (BRAVE_API_KEY)
  @modelcontextprotocol/server-puppeteer   — headless browser
  @modelcontextprotocol/server-github      — GitHub operations
  @modelcontextprotocol/server-postgres    — query a Postgres DB
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path("mcp_servers.json")
_INIT_TIMEOUT = 15.0
_CALL_TIMEOUT = 30.0


# ── Stdio transport ─────────────────────────────────────────────────────────

class StdioMCPServer:
    """Manages a single MCP server process (stdio transport)."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.name = name
        self._command = command
        self._args = args
        self._env = {**os.environ, **(env or {})}
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        logger.info("[mcp] started %s (pid=%s)", self.name, self._proc.pid)
        await self._initialize()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def _send(self, method: str, params: dict | None = None) -> Any:
        async with self._lock:
            self._req_id += 1
            req = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params or {}}
            line = json.dumps(req) + "\n"
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=_CALL_TIMEOUT)
            if not raw:
                raise RuntimeError(f"MCP server {self.name} closed stdout")
            resp = json.loads(raw.decode())
            if "error" in resp:
                raise RuntimeError(f"MCP error from {self.name}: {resp['error']}")
            return resp.get("result")

    async def _initialize(self) -> None:
        result = await asyncio.wait_for(
            self._send("initialize", {
                "protocolVersion": "0.1.0",
                "clientInfo": {"name": "xclaw", "version": "3.0"},
                "capabilities": {},
            }),
            timeout=_INIT_TIMEOUT,
        )
        logger.info("[mcp] %s initialized: %s", self.name, result.get("serverInfo", {}).get("name", "?"))
        # Send initialized notification (fire-and-forget, no response)
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self._proc.stdin.write((json.dumps(notif) + "\n").encode())
        await self._proc.stdin.drain()

    async def list_tools(self) -> list[dict]:
        result = await self._send("tools/list")
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = await self._send("tools/call", {"name": tool_name, "arguments": arguments})
        # MCP tools/call returns {content: [{type: "text", text: "..."}]}
        if isinstance(result, dict):
            content = result.get("content", [])
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(parts) or json.dumps(result)
        return str(result)


# ── HTTP transport ───────────────────────────────────────────────────────────

class HttpMCPServer:
    """MCP server over HTTP (POST /messages)."""

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None) -> None:
        self.name = name
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._req_id = 0

    async def start(self) -> None:
        pass  # HTTP: no process to start

    async def stop(self) -> None:
        pass

    async def _send(self, method: str, params: dict | None = None) -> Any:
        import httpx
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params or {}}
        async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
            resp = await client.post(
                f"{self._url}/messages",
                json=payload,
                headers={"Content-Type": "application/json", **self._headers},
            )
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error from {self.name}: {data['error']}")
        return data.get("result")

    async def list_tools(self) -> list[dict]:
        result = await self._send("tools/list")
        return result.get("tools", []) if result else []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = await self._send("tools/call", {"name": tool_name, "arguments": arguments})
        if isinstance(result, dict):
            content = result.get("content", [])
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(parts) or json.dumps(result)
        return str(result)


# ── MCPManager ───────────────────────────────────────────────────────────────

class MCPManager:
    """
    Loads mcp_servers.json, starts all configured MCP servers,
    and registers their tools into the provided ToolRegistry.
    """

    def __init__(self, config_path: str | Path = _CONFIG_FILE) -> None:
        self._config_path = Path(config_path)
        self._servers: list[StdioMCPServer | HttpMCPServer] = []

    async def start_all(self, registry: "ToolRegistry") -> int:
        """Start all MCP servers from config and register their tools. Returns tool count."""
        if not self._config_path.exists():
            logger.debug("[mcp] no mcp_servers.json found — skipping")
            return 0

        try:
            config = json.loads(self._config_path.read_text())
        except Exception as exc:
            logger.warning("[mcp] failed to parse mcp_servers.json: %s", exc)
            return 0

        servers_cfg = config.get("servers", [])
        if not servers_cfg:
            return 0

        total = 0
        for cfg in servers_cfg:
            try:
                server = self._build_server(cfg)
                await server.start()
                self._servers.append(server)
                count = await self._register_tools(server, registry)
                total += count
                logger.info("[mcp] %s: registered %d tools", server.name, count)
            except Exception as exc:
                logger.warning("[mcp] failed to start server %s: %s", cfg.get("name", "?"), exc)

        return total

    async def stop_all(self) -> None:
        for server in self._servers:
            try:
                await server.stop()
            except Exception:
                pass

    def _build_server(self, cfg: dict) -> StdioMCPServer | HttpMCPServer:
        name = cfg.get("name", "unnamed")
        transport = cfg.get("type", "stdio")

        if transport == "stdio":
            cmd = cfg.get("command", "")
            args = cfg.get("args", [])
            raw_env = cfg.get("env", {})
            # Expand env var references like "${MY_VAR}"
            env = {}
            for k, v in raw_env.items():
                if v.startswith("${") and v.endswith("}"):
                    env[k] = os.getenv(v[2:-1], "")
                else:
                    env[k] = v
            return StdioMCPServer(name=name, command=cmd, args=args, env=env)

        if transport == "http":
            url = cfg.get("url", "")
            headers = cfg.get("headers", {})
            return HttpMCPServer(name=name, url=url, headers=headers)

        raise ValueError(f"Unknown MCP transport: {transport!r}")

    async def _register_tools(self, server: StdioMCPServer | HttpMCPServer, registry: "ToolRegistry") -> int:
        tools = await server.list_tools()
        count = 0
        for tool_def in tools:
            tool_name = tool_def.get("name", "")
            description = tool_def.get("description", tool_name)
            input_schema = tool_def.get("inputSchema", {})
            properties = input_schema.get("properties", {})
            required = input_schema.get("required", [])

            # Build a dynamic async function for this MCP tool
            fn = _make_mcp_tool_fn(server, tool_name, properties, required)
            fn.__name__ = f"mcp_{server.name}_{tool_name}"
            fn.__doc__ = f"[{server.name}] {description}"

            registry.register(fn, description=f"[{server.name}] {description}", name=fn.__name__)
            count += 1

        return count


def _make_mcp_tool_fn(server, tool_name: str, properties: dict, required: list):
    """
    Dynamically create an async function that calls an MCP tool.
    Parameters are inferred from the tool's inputSchema.
    """
    param_names = list(properties.keys())

    async def mcp_tool(**kwargs) -> str:
        arguments = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return await server.call_tool(tool_name, arguments)
        except Exception as exc:
            return f"MCP tool {tool_name} failed: {exc}"

    # Set a proper signature so ToolRegistry can introspect it
    import inspect
    params = [inspect.Parameter("self_session_id", inspect.Parameter.KEYWORD_ONLY,
                                annotation=str, default="")]
    for pname, pdef in properties.items():
        ptype = pdef.get("type", "string")
        annotation = {"string": str, "integer": int, "number": float, "boolean": bool}.get(ptype, str)
        default = inspect.Parameter.empty if pname in required else None
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=annotation, default=default))

    mcp_tool.__signature__ = inspect.Signature(params)
    return mcp_tool


# ── Convenience loader ───────────────────────────────────────────────────────

async def load_mcp_servers(registry: "ToolRegistry", config_path: str | Path = _CONFIG_FILE) -> int:
    """Load and register all MCP servers. Returns number of tools registered."""
    manager = MCPManager(config_path)
    return await manager.start_all(registry)
