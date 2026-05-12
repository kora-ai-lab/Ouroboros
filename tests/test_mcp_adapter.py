import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nucleus"))
from mcp_adapter import MCPAdapter


class FakeMCPClient:
    async def list_tools(self):
        return [
            {
                "name": "arbitrary_echo",
                "description": "Echoes any payload without domain-specific kernel code.",
                "inputSchema": {"type": "object"},
            }
        ]

    async def call_tool(self, name, arguments=None):
        return {"content": [{"type": "text", "text": f"{name}:{(arguments or {}).get('message')}"}]}

    async def list_resources(self):
        return [{"uri": "fake://resource", "name": "Fake Resource"}]

    async def read_resource(self, uri):
        return {"contents": [{"uri": uri, "text": "resource body"}]}

    async def list_prompts(self):
        return [{"name": "fake_prompt", "description": "A fake prompt"}]


def write_config(tmp_path):
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps({"servers": [{"name": "fake", "transport": "fake"}]}),
        encoding="utf-8",
    )
    return config_path


def test_mcp_adapter_discovers_and_calls_arbitrary_tool(tmp_path):
    adapter = MCPAdapter(write_config(tmp_path), client_factory=lambda _config: FakeMCPClient())

    assert adapter.list_servers() == [{"name": "fake", "transport": "fake", "enabled": True}]
    tools = asyncio.run(adapter.list_tools("fake"))
    assert tools[0]["name"] == "arbitrary_echo"

    result = asyncio.run(adapter.call_tool("fake", "arbitrary_echo", {"message": "hello"}))
    assert result["content"][0]["text"] == "arbitrary_echo:hello"


def test_mcp_adapter_lists_resources_reads_resource_and_lists_prompts(tmp_path):
    adapter = MCPAdapter(write_config(tmp_path), client_factory=lambda _config: FakeMCPClient())

    assert asyncio.run(adapter.list_resources("fake"))[0]["uri"] == "fake://resource"
    assert asyncio.run(adapter.read_resource("fake", "fake://resource"))["contents"][0]["text"] == "resource body"
    assert asyncio.run(adapter.list_prompts("fake"))[0]["name"] == "fake_prompt"
