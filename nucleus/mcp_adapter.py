from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "mcp_servers.json"


class MCPClient(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def list_resources(self) -> list[dict[str, Any]]: ...
    async def read_resource(self, uri: str) -> dict[str, Any]: ...
    async def list_prompts(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    enabled: bool = True
    module: str | None = None
    factory: str = "create_client"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MCPServerConfig":
        if not isinstance(raw.get("name"), str) or not raw["name"].strip():
            raise ValueError("Each MCP server must include a non-empty name.")
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {raw['name']} args must be a list of strings.")
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"MCP server {raw['name']} env must be a string map.")
        return cls(
            name=raw["name"].strip(),
            transport=str(raw.get("transport", "stdio")),
            command=raw.get("command"),
            args=tuple(args),
            env=env,
            enabled=bool(raw.get("enabled", True)),
            module=raw.get("module"),
            factory=str(raw.get("factory", "create_client")),
        )

    def public_dict(self) -> dict[str, Any]:
        return {"name": self.name, "transport": self.transport, "enabled": self.enabled}


class StdioMCPClient:
    """Minimal JSON-RPC stdio MCP client for generic server discovery and calls."""

    def __init__(self, config: MCPServerConfig) -> None:
        if not config.command:
            raise ValueError(f"MCP server {config.name} requires a command for stdio transport.")
        self.config = config
        self._next_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "StdioMCPClient":
        env = os.environ.copy()
        env.update(self.config.env or {})
        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "ouroboros", "version": "1.0.0"}})
        await self._notify("notifications/initialized", {})
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self._process.kill()

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP process is not running.")
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        if not self._process or not self._process.stdout:
            raise RuntimeError("MCP process is not running.")
        headers: dict[str, str] = {}
        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server {self.config.name} closed stdout.")
            stripped = line.decode("ascii", errors="replace").strip()
            if not stripped:
                break
            key, _, value = stripped.partition(":")
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise RuntimeError(f"MCP server {self.config.name} sent a message without Content-Length.")
        body = await self._process.stdout.readexactly(length)
        return json.loads(body.decode("utf-8"))

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._write_message(payload)
            while True:
                message = await self._read_message()
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"MCP {method} failed: {message['error']}")
                return message.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        return list((await self._request("tools/list")).get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(await self._request("tools/call", {"name": name, "arguments": arguments or {}}))

    async def list_resources(self) -> list[dict[str, Any]]:
        return list((await self._request("resources/list")).get("resources", []))

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return dict(await self._request("resources/read", {"uri": uri}))

    async def list_prompts(self) -> list[dict[str, Any]]:
        return list((await self._request("prompts/list")).get("prompts", []))


ClientFactory = Callable[[MCPServerConfig], MCPClient | Awaitable[MCPClient]]


class MCPAdapter:
    def __init__(self, config_path: str | Path | None = None, client_factory: ClientFactory | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("OUROBOROS_MCP_CONFIG") or DEFAULT_CONFIG_PATH)
        self.client_factory = client_factory

    def load_server_configs(self) -> dict[str, MCPServerConfig]:
        if not self.config_path.exists():
            return {}
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        servers = data.get("servers", []) if isinstance(data, dict) else []
        if not isinstance(servers, list):
            raise ValueError("MCP config must contain a servers list.")
        configs = [MCPServerConfig.from_dict(item) for item in servers if isinstance(item, dict)]
        return {config.name: config for config in configs if config.enabled}

    def list_servers(self) -> list[dict[str, Any]]:
        return [config.public_dict() for config in self.load_server_configs().values()]

    def _config(self, server_name: str) -> MCPServerConfig:
        configs = self.load_server_configs()
        if server_name not in configs:
            raise KeyError(f"MCP server is not configured or enabled: {server_name}")
        return configs[server_name]

    async def _make_client(self, config: MCPServerConfig) -> MCPClient:
        if self.client_factory is not None:
            client = self.client_factory(config)
            return await client if hasattr(client, "__await__") else client
        if config.transport == "python_module":
            if not config.module:
                raise ValueError(f"MCP server {config.name} requires module for python_module transport.")
            module = importlib.import_module(config.module)
            return getattr(module, config.factory)(config)
        return StdioMCPClient(config)

    async def _with_client(self, server_name: str, callback: Callable[[MCPClient], Awaitable[Any]]) -> Any:
        config = self._config(server_name)
        client = await self._make_client(config)
        if hasattr(client, "__aenter__"):
            async with client:  # type: ignore[func-returns-value]
                return await callback(client)
        return await callback(client)

    async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        return await self._with_client(server_name, lambda client: client.list_tools())

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._with_client(server_name, lambda client: client.call_tool(tool_name, arguments or {}))

    async def list_resources(self, server_name: str) -> list[dict[str, Any]]:
        return await self._with_client(server_name, lambda client: client.list_resources())

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        return await self._with_client(server_name, lambda client: client.read_resource(uri))

    async def list_prompts(self, server_name: str) -> list[dict[str, Any]]:
        return await self._with_client(server_name, lambda client: client.list_prompts())


async def dispatch(operation: str, server: str | None = None, **kwargs: Any) -> Any:
    adapter = MCPAdapter()
    if operation == "list_servers":
        return {"servers": adapter.list_servers()}
    if not server:
        raise ValueError(f"operation {operation} requires a server name.")
    if operation == "list_tools":
        return {"tools": await adapter.list_tools(server)}
    if operation == "call_tool":
        return await adapter.call_tool(server, str(kwargs["tool_name"]), kwargs.get("arguments") or {})
    if operation == "list_resources":
        return {"resources": await adapter.list_resources(server)}
    if operation == "read_resource":
        return await adapter.read_resource(server, str(kwargs["uri"]))
    if operation == "list_prompts":
        return {"prompts": await adapter.list_prompts(server)}
    raise ValueError(f"Unsupported MCP bridge operation: {operation}")


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(asyncio.run(dispatch(**payload))))
