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
    monkeypatch.setattr(server, "ROOT_DIR", tmp_path)
    server.ensure_layout()
    server.init_db()
    client = TestClient(server.app)
    client.app.state.model_adapter = FakeAdapter("Test response")
    client.app.state.pending_approvals = {}

    async def fake_call_model(prompt, model="nova-fast"):
        return "Fake summary containing Kora Lab and sovereign memory."
    monkeypatch.setattr(server, "call_model_simple", fake_call_model)

    return client


def test_registry_loads_builtin_tools(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/tools")
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "execute_python" in names
    assert "register_tool" in names


def test_execute_python_success():
    result = asyncio.run(server.execute_python("print('ok')"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"
    assert result["timed_out"] is False


def test_execute_python_timeout():
    result = asyncio.run(server.execute_python("import time; time.sleep(31)"))
    assert result["exit_code"] == -1
    assert result["timed_out"] is True


def test_extract_tool_calls_reads_tool_call_json():
    text = 'Before <tool_call>{"name":"execute_python","arguments":{"code":"print(1)"}}</tool_call> After'
    assert server.extract_tool_calls(text) == [
        {"name": "execute_python", "arguments": {"code": "print(1)"}}
    ]


def test_extract_tool_calls_reads_fenced_execute_python_format():
    text = """```execute_python
code: |
  from pathlib import Path
  print(Path.cwd())
```"""
    assert server.extract_tool_calls(text) == [
        {
            "name": "execute_python",
            "arguments": {"code": "from pathlib import Path\nprint(Path.cwd())"},
        }
    ]


def test_extract_tool_calls_ignores_invalid_tool_text_safely():
    text = """
<tool_call>{not valid json}</tool_call>
```execute_python
not_code: print(1)
```
```python
print("not a tool")
```
"""
    assert server.extract_tool_calls(text) == []


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
    assert entry["registered"] == "echo_tool"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert any(tool["name"] == "echo_tool" for tool in registry["tools"])

def write_skill_package(base: Path) -> Path:
    package = base / "tools" / "caps_echo"
    package.mkdir(parents=True)
    (package / "tool.py").write_text(
        """
import json
import sys

args = json.loads(sys.stdin.read() or '{}')
print(json.dumps({'echo': args.get('message', '')}))
""".lstrip(),
        encoding="utf-8",
    )
    (package / "schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (package / "README.md").write_text("# caps_echo\n\nEchoes a message.\n", encoding="utf-8")
    (package / "metadata.json").write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "deprecated": False,
                "deprecation_reason": "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (package / "tests.py").write_text(
        """
import json
import subprocess
import sys
from pathlib import Path

completed = subprocess.run(
    [sys.executable, str(Path(__file__).with_name('tool.py'))],
    input=json.dumps({'message': 'package-ok'}),
    text=True,
    encoding='utf-8',
    capture_output=True,
    check=False,
)
assert completed.returncode == 0, completed.stderr
assert json.loads(completed.stdout)['echo'] == 'package-ok'
""".lstrip(),
        encoding="utf-8",
    )
    return package


def test_skill_package_validates_registers_and_runs(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    write_skill_package(tmp_path)

    entry = server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    assert entry["registered"] == "caps_echo"
    assert entry["package"] is True
    assert entry["version"] == "1.2.3"

    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "caps_echo")
    assert tool["package"] is True
    assert tool["filepath"] == "tools/caps_echo"
    assert tool["parameters"]["required"] == ["message"]
    assert tool["version"] == "1.2.3"
    assert tool["deprecated"] is False
    assert tool["deprecation_reason"] == ""

    result = server.run_registered_tool(tool, {"message": "hello"})
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"])["echo"] == "hello"


def test_skill_package_must_pass_tests_before_registration(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    (package / "tests.py").write_text("raise SystemExit(7)\n", encoding="utf-8")

    entry = server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    assert "tests.py failed" in entry["error"]
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert all(tool["name"] != "caps_echo" for tool in registry["tools"])


def test_skill_package_requires_valid_schema_before_registration(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    (package / "schema.json").write_text(
        json.dumps({"type": "not-a-json-schema-type"}),
        encoding="utf-8",
    )

    entry = server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    assert "Invalid JSON Schema type" in entry["error"] or "Invalid JSON Schema" in entry["error"]
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert all(tool["name"] != "caps_echo" for tool in registry["tools"])


def test_retrieve_relevant_memory(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    asyncio.run(server.summarize_and_store(
        [{"role": "user", "content": "Kora Lab sovereign AI memory test"}],
        "session-1",
    ))
    matches = server.retrieve_relevant("sovereign memory", limit=5)
    assert "Fake summary containing Kora Lab and sovereign memory." in matches


def test_summarize_and_store_archives_when_provider_fails(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    session_id = "session-provider-failure"
    messages = [{"role": "user", "content": "Archive this before summary."}]
    archive_path = tmp_path / "data" / "archive" / f"{session_id}.json"

    async def failing_call_model(prompt, model="openai-fast"):
        assert archive_path.exists()
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(server, "call_model_simple", failing_call_model)

    result = asyncio.run(server.summarize_and_store(messages, session_id))

    assert result["status"] == "saved"
    assert result["archive_saved"] is True
    assert result["summary_fallback"] is True
    assert result["summary"] == f"Conversation session {session_id}"
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    assert archive["session_id"] == session_id
    assert archive["messages"] == messages
    assert archive["summary"] == f"Conversation session {session_id}"
    memory_response = asyncio.run(server.get_memory())
    memories = json.loads(memory_response.body)["memories"]
    assert memories[0]["session_id"] == session_id
    assert memories[0]["summary"] == f"Conversation session {session_id}"


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


def test_execute_python_default_registry_requires_approval():
    tool = next(tool for tool in server.DEFAULT_REGISTRY["tools"] if tool["name"] == "execute_python")
    assert tool["requires_approval"] is True


def test_execute_python_policy_classifies_risks():
    policy = server.summarize_python_execution_policy("import subprocess\nsubprocess.run(['echo', 'x'])")
    assert policy["action"] == "require_approval"
    assert "subprocess/process access" in policy["reasons"]


def test_execute_python_policy_detects_os_aliases():
    policy = server.summarize_python_execution_policy("import os as o\no.system('echo x')")
    assert policy["action"] == "require_approval"
    assert "subprocess/process access" in policy["reasons"]

    from_import_policy = server.summarize_python_execution_policy("from os import system as run\nrun('echo x')")
    assert from_import_policy["action"] == "require_approval"
    assert "subprocess/process access" in from_import_policy["reasons"]


def test_execute_python_policy_detects_path_open_write():
    policy = server.summarize_python_execution_policy("from pathlib import Path\nPath('out.txt').open('w').write('x')")
    assert policy["action"] == "require_approval"
    assert "filesystem write or mutation" in policy["reasons"]


def test_execute_python_approved_execution(tmp_path):
    target = tmp_path / "approved.txt"
    code = f"open({str(target)!r}, 'w').write('approved')"
    result = asyncio.run(server.execute_python(code, policy_approved=True))
    assert result["exit_code"] == 0
    assert target.read_text(encoding="utf-8") == "approved"


def test_execute_python_rejected_execution(tmp_path):
    target = tmp_path / "rejected.txt"
    code = f"open({str(target)!r}, 'w').write('rejected')"
    result = asyncio.run(server.execute_python(code))
    assert result["exit_code"] == -1
    assert result["policy"]["action"] == "require_approval"
    assert not target.exists()


def test_execute_python_policy_blocked_execution():
    result = asyncio.run(server.execute_python("eval('1 + 1')"))
    assert result["exit_code"] == -1
    assert result["policy"]["action"] == "block"
    assert "dynamic code execution" in result["error"]

class SequenceAdapter(server.ModelAdapter):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, model, provider):
        self.calls.append(messages)
        text = self.responses.pop(0) if self.responses else "Recovered summary from tool results."
        yield text


def test_refusal_recovery_reprompts_self_evolution_without_hardcoded_tool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    adapter = SequenceAdapter([
        "I don't have internet access to search for real-time information about ST Digital.",
        '<tool_call>{"name":"execute_python","arguments":{"code":"print(\\\"model built capability\\\")"}}</tool_call>',
        "Recovered summary from tool results.",
    ])
    client.app.state.model_adapter = adapter

    async def fake_execute_python(code, policy_approved=False):
        assert code == 'print("model built capability")'
        return {"stdout": "model built capability", "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(server, "execute_python", fake_execute_python)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "Find me all the information about ST Digital the cloud provider"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
        },
    )

    assert response.status_code == 200
    assert "event: assistant_replace" in response.text
    assert "Retrying under the self-evolution protocol." in response.text
    assert "event: tool_call" in response.text
    assert "model built capability" in response.text
    assert "Recovered summary from tool results." in response.text
    assert len(adapter.calls) == 3
    retry_messages = [message for message in adapter.calls[1] if message["role"] == "system"]
    assert any("previous answer violated the self-evolution protocol" in message["content"] for message in retry_messages)


def test_capability_refusal_detection_does_not_create_tool_code():
    request = server.ChatRequest(messages=[server.ChatMessage(role="user", content="Find me information about ST Digital")])
    retry = server.build_self_evolution_retry_message(request)
    assert server.is_capability_refusal("I don't have internet access.") is True
    assert "ST Digital" in retry
    assert "<tool_call>" in retry
    assert "duckduckgo" not in retry.lower()
    assert "urllib" not in retry.lower()
    assert server.is_capability_refusal("Here is the result.") is False
