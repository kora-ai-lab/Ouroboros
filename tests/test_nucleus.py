import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nucleus"))
import server


class FakeAdapter(server.ModelAdapter):
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, messages, model, provider):
        for chunk in [self.text]:
            yield chunk


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "ARCHIVE_DIR", tmp_path / "data" / "archive")
    monkeypatch.setattr(server, "TOOLS_DIR", tmp_path / "tools")
    monkeypatch.setattr(server, "KORA_DIR", tmp_path / "kora")
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(server, "FACTS_PATH", tmp_path / "facts.json")
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(server, "ENV_PATHS", [tmp_path / ".env"])
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "data" / "memory.sqlite3")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    server.ensure_layout()
    server.init_db()
    client = TestClient(server.app)
    client.app.state.model_adapter = FakeAdapter("Test response")
    client.app.state.pending_approvals = {}
    return client


def test_registry_loads_builtin_tools(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/tools")
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "execute_python" in names
    assert "register_tool" in names


def test_execute_python_success():
    result = server.execute_python("print('ok')")
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"
    assert result["timed_out"] is False


def test_execute_python_timeout():
    result = server.execute_python("import time; time.sleep(31)")
    assert result["exit_code"] == 124
    assert result["timed_out"] is True


def test_register_tool_validates_and_persists(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "echo_tool.py"
    tool_path.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    entry = server.register_tool(
        name="echo_tool",
        description="Echo JSON input",
        parameters_schema={"type": "object"},
        filepath="tools/echo_tool.py",
    )
    assert entry["name"] == "echo_tool"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert any(tool["name"] == "echo_tool" for tool in registry["tools"])


def test_retrieve_relevant_memory(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    server.summarize_session(
        "session-1",
        [{"role": "user", "content": "Kora Lab sovereign AI memory test"}],
    )
    matches = server.retrieve_relevant("sovereign memory", limit=5)
    assert len(matches) == 1
    assert matches[0]["session_id"] == "session-1"


def test_upload_text(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.post("/upload", files={"file": ("note.txt", b"hello", "text/plain")})
    assert response.status_code == 200
    assert response.json() == {"type": "text", "name": "note.txt", "content": "hello"}


def test_chat_stream_uses_fake_adapter_and_saves_memory(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
        },
    )
    assert response.status_code == 200
    assert "event: delta" in response.text
    assert "Test response" in response.text
    memory_response = client.get("/memory")
    assert len(memory_response.json()["memories"]) == 1


def test_settings_save_updates_env_and_defaults(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/settings",
        json={
            "default_provider": "pollinations",
            "default_model": "qwen-coder",
            "provider_keys": {"pollinations": "test-key"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["default_model"] == "qwen-coder"
    assert body["providers"]["pollinations"]["configured"] is True
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == "POLLINATIONS_API_KEY=test-key"


def test_custom_provider_can_be_saved(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/providers",
        json={
            "id": "local-openai",
            "label": "Local OpenAI",
            "type": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "local-key",
            "models": ["local-model"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "local_openai" in body["providers"]
    assert body["providers"]["local_openai"]["models"] == ["local-model"]
    assert "LOCAL_OPENAI_API_KEY=local-key" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_gguf_discovery_lists_local_models(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "kora.gguf").write_text("not a real model", encoding="utf-8")
    client.post(
        "/providers",
        json={
            "id": "gguf",
            "label": "GGUF",
            "type": "gguf",
            "models_path": str(models_dir),
        },
    )
    response = client.post("/providers/gguf/discover")
    assert response.status_code == 200
    assert response.json()["models"] == [str(models_dir / "kora.gguf")]


def test_local_model_scan_lists_common_model_files(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    model = models_dir / "kora.gguf"
    model.write_text("model", encoding="utf-8")
    results = server.scan_local_model_files([str(models_dir)])
    assert results[0]["name"] == "kora.gguf"
    assert results[0]["path"] == str(model.resolve())


def test_strip_tool_calls_removes_raw_tool_markup():
    text = 'Before <tool_call>{"name":"execute_python","arguments":{"code":"print(1)"}}</tool_call> After'
    assert server.strip_tool_calls(text).strip() == "Before  After"
