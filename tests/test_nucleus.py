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


class SequenceFakeAdapter(server.ModelAdapter):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, model, provider):
        self.calls.append(list(messages))
        if self.responses:
            yield self.responses.pop(0)
        else:
            yield "Done"


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "ARCHIVE_DIR", tmp_path / "data" / "archive")
    monkeypatch.setattr(server, "CHECKPOINTS_DIR", tmp_path / "data" / "checkpoints")
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
    assert "rollback_latest_checkpoint" in names
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
        test_plan="Echo stdin JSON for smoke validation.",
        sample_arguments={"message": "hello"},
    )
    assert entry == {
        "registered": "echo_tool",
        "version": "1.0.0",
        "permanent": True,
        "trusted": False,
    }
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "echo_tool")
    assert tool["last_test_status"] == "pending"
    assert tool["trusted"] is False
    assert tool["use_count"] == 0


def test_register_tool_requires_test_evidence(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "untested_tool.py"
    tool_path.write_text("print('untested')\n", encoding="utf-8")

    entry = server.register_tool(
        name="untested_tool",
        description="No test metadata",
        parameters_schema={"type": "object"},
        filepath="tools/untested_tool.py",
    )

    assert "test_command, test_plan, or sample_arguments" in entry["error"]


def test_validate_registered_tool_updates_status_and_use_count(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "adder_tool.py"
    tool_path.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "print(payload['a'] + payload['b'])\n",
        encoding="utf-8",
    )
    server.register_tool(
        name="adder_tool",
        description="Add two numbers",
        parameters_schema={"type": "object"},
        filepath="tools/adder_tool.py",
        version="1.0.0",
        source_task_id="task-123",
        test_command="python nucleus/tools/adder_tool.py < sample.json",
        sample_arguments={"a": 1, "b": 2},
    )

    validation = server.validate_registered_tool("adder_tool")

    assert validation["validated"] is True
    assert validation["result"]["stdout"].strip() == "3"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "adder_tool")
    assert tool["trusted"] is True
    assert tool["last_test_status"] == "passed"
    assert tool["last_error"] is None

    result = server.run_registered_tool(tool, {"a": 2, "b": 5})
    assert result["stdout"].strip() == "7"
    update = server.update_registered_tool_status(
        "adder_tool",
        "1.0.0",
        last_test_status="passed",
        increment_use_count=True,
    )
    assert update["tool"]["use_count"] == 1


def test_register_tool_preserves_older_versions(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "versioned_tool.py"
    tool_path.write_text("print('ok')\n", encoding="utf-8")

    first = server.register_tool(
        name="versioned_tool",
        description="First version",
        parameters_schema={"type": "object"},
        filepath="tools/versioned_tool.py",
        version="1.0.0",
        test_plan="Smoke test v1.",
    )
    second = server.register_tool(
        name="versioned_tool",
        description="Second version",
        parameters_schema={"type": "object"},
        filepath="tools/versioned_tool.py",
        version="1.1.0",
        test_plan="Smoke test v2.",
    )

    assert first["version"] == "1.0.0"
    assert second["version"] == "1.1.0"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    versions = [tool for tool in registry["tools"] if tool["name"] == "versioned_tool"]
    assert [tool["version"] for tool in versions] == ["1.0.0", "1.1.0"]
    assert versions[1]["supersedes"] == "1.0.0"
    assert server.find_tool("versioned_tool")["version"] == "1.1.0"

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


def test_chat_evaluation_retry_performs_second_tool_call(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    first_call = json.dumps({"name": "execute_python", "arguments": {"code": "raise Exception(\"fail\")"}})
    second_call = json.dumps({"name": "execute_python", "arguments": {"code": "print(\"ok\")"}})
    adapter = SequenceFakeAdapter([
        f"<tool_call>{first_call}</tool_call>",
        '{"decision":"retry","rationale":"The previous result failed."}',
        f"<tool_call>{second_call}</tool_call>",
        '{"decision":"final","rationale":"The result is sufficient."}',
        'Final answer after retry.',
    ])
    client.app.state.model_adapter = adapter

    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "run the fake task"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
        },
    )

    assert response.status_code == 200
    assert response.text.count("event: tool_call") == 2
    assert '"decision": "retry"' in response.text
    assert '"decision": "final"' in response.text
    assert "Final answer after retry." in response.text
    with server.connect_db() as conn:
        rows = conn.execute("SELECT decision, rationale FROM evaluation_decision ORDER BY timestamp").fetchall()
    assert [row["decision"] for row in rows] == ["retry", "final"]


def test_chat_evaluation_success_finalizes(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool_call = json.dumps({"name": "execute_python", "arguments": {"code": "print(\"ok\")"}})
    adapter = SequenceFakeAdapter([
        f"<tool_call>{tool_call}</tool_call>",
        '{"decision":"final","rationale":"The result is sufficient."}',
        'Final answer.',
    ])
    client.app.state.model_adapter = adapter

    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "run the fake task"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
        },
    )

    assert response.status_code == 200
    assert response.text.count("event: tool_call") == 1
    assert '"decision": "final"' in response.text
    assert "Final answer." in response.text
    with server.connect_db() as conn:
        rows = conn.execute("SELECT decision, rationale FROM evaluation_decision").fetchall()
    assert [row["decision"] for row in rows] == ["final"]


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


def test_git_harness_is_not_special_case_tool_by_default():
    names = {tool["name"] for tool in server.DEFAULT_REGISTRY["tools"]}
    assert "git_status" not in names
    assert "git_harness" not in names


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


def test_execute_python_policy_includes_checkpoint_metadata(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    target = tmp_path / "policy.txt"
    policy = server.summarize_python_execution_policy(f"from pathlib import Path\nPath({str(target)!r}).write_text('x')")
    assert policy["action"] == "require_approval"
    assert policy["checkpoint"]["strategy"] == "files"
    assert policy["checkpoint"]["affected_paths"] == [str(target)]
    assert policy["checkpoint"]["storage_dir"] == str(tmp_path / "data" / "checkpoints")


def test_checkpoint_restores_temp_file_after_mutation(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    target = tmp_path / "mutable.txt"
    target.write_text("original", encoding="utf-8")

    result = asyncio.run(
        server.execute_python(
            f"from pathlib import Path\nPath({str(target)!r}).write_text('mutated', encoding='utf-8')",
            policy_approved=True,
        )
    )

    assert result["exit_code"] == 0
    assert result["checkpoint"]["strategy"] == "files"
    assert result["checkpoint"]["path_count"] == 1
    assert target.read_text(encoding="utf-8") == "mutated"
    metadata_path = tmp_path / "data" / "checkpoints" / f"{result['checkpoint']['id']}.json"
    assert metadata_path.exists()

    rollback = server.rollback_latest_checkpoint()

    assert rollback["checkpoint"]["id"] == result["checkpoint"]["id"]
    assert str(target) in rollback["restored"]
    assert target.read_text(encoding="utf-8") == "original"


def test_checkpoint_rollback_endpoint(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    target = tmp_path / "endpoint.txt"
    target.write_text("before", encoding="utf-8")
    asyncio.run(
        server.execute_python(
            f"open({str(target)!r}, 'w', encoding='utf-8').write('after')",
            policy_approved=True,
        )
    )

    response = client.post("/checkpoints/rollback")

    assert response.status_code == 200
    assert str(target) in response.json()["restored"]
    assert target.read_text(encoding="utf-8") == "before"


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
    tool_call = json.dumps({"name": "execute_python", "arguments": {"code": "print(\"model built capability\")"}})
    adapter = SequenceAdapter([
        "I don't have internet access to search for real-time information about ST Digital.",
        f"<tool_call>{tool_call}</tool_call>",
        '{"decision":"final","rationale":"The result is sufficient."}',
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
    assert len(adapter.calls) == 4
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

def test_registered_tool_failure_triggers_model_repair_and_retry(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "flaky_tool.py"
    tool_path.write_text(
        "import sys\nprint('broken', file=sys.stderr)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    registered = server.register_tool(
        name="flaky_tool",
        description="Return the provided value as JSON.",
        parameters_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        filepath="tools/flaky_tool.py",
    )
    assert registered["registered"] == "flaky_tool"

    patch_code = "\n".join(
        [
            "from pathlib import Path",
            "Path('tools/flaky_tool.py').write_text(\"import json, sys\\nargs = json.loads(sys.stdin.read() or '{}')\\nprint(json.dumps({'status': 'ok', 'value': args.get('value')}))\\n\", encoding='utf-8')",
        ]
    )
    adapter = SequenceAdapter(
        [
            '<tool_call>{"name":"flaky_tool","arguments":{"value":"second-run"}}</tool_call>',
            f'<tool_call>{json.dumps({"name": "execute_python", "arguments": {"code": patch_code}})}</tool_call>',
            '<tool_call>{"name":"flaky_tool","arguments":{"value":"second-run"}}</tool_call>',
            "The repaired tool succeeded.",
        ]
    )
    client.app.state.model_adapter = adapter

    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "Run flaky_tool and fix it if needed."}],

def _run_git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "agent@example.test")
    _run_git(repo, "config", "user.name", "Agent Test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial")
    return repo


def test_git_harness_status_diff_and_checkpoint_commit(tmp_path):
    import git_harness

    repo = _make_git_repo(tmp_path)
    (repo / "README.md").write_text("initial\nchanged\n", encoding="utf-8")
    (repo / "notes.txt").write_text("new\n", encoding="utf-8")

    status = git_harness.status(repo)
    assert status["ok"] is True
    assert status["clean"] is False
    assert {file["path"] for file in status["files"]} == {"README.md", "notes.txt"}

    diff = git_harness.diff(repo)
    assert diff["ok"] is True
    assert "+changed" in diff["stdout"]

    changed = git_harness.list_changed_files(repo)
    assert changed["paths"] == ["README.md", "notes.txt"]

    checkpoint = git_harness.commit("checkpoint changes", repo)
    assert checkpoint["ok"] is True
    assert checkpoint["committed"] is True
    assert checkpoint["before"]["clean"] is False
    assert checkpoint["after"]["clean"] is True
    assert checkpoint["hash"]


def test_git_harness_restore_explicit_path(tmp_path):
    import git_harness

    repo = _make_git_repo(tmp_path)
    readme = repo / "README.md"
    readme.write_text("damaged\n", encoding="utf-8")

    restored = git_harness.restore(repo, paths=["README.md"])
    assert restored["ok"] is True
    assert readme.read_text(encoding="utf-8") == "initial\n"
    assert git_harness.status(repo)["clean"] is True


def test_execute_python_policy_destructive_git_requires_manual_approval():
    reset_policy = server.summarize_python_execution_policy(
        "import subprocess\nsubprocess.run(['git', 'reset', '--hard', 'HEAD'])"
    )
    assert reset_policy["action"] == "require_approval"
    assert reset_policy["manual_approval_required"] is True
    assert "destructive git command requires explicit approval" in reset_policy["reasons"]

    clean_policy = server.summarize_python_execution_policy("import os\nos.system('git clean -fd')")
    assert clean_policy["manual_approval_required"] is True

    push_policy = server.summarize_python_execution_policy("import os\nos.system('git push --force-with-lease origin main')")
    assert push_policy["manual_approval_required"] is True


def test_system_prompt_guides_git_status_for_self_modifying_work(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    prompt = server.build_system_prompt(
        server.ChatRequest(messages=[server.ChatMessage(role="user", content="change yourself")])
    )
    assert "inspect `git status` before edits and again after edits" in prompt
    assert "nucleus/git_harness.py" in prompt
def test_task_runner_continues_until_final_answer_and_persists_state(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    adapter = SequenceAdapter([
        "1. Run the first step\n2. Run the second step\n<tool_call>{\"name\":\"execute_python\",\"arguments\":{\"code\":\"print('one')\"}}</tool_call>",
        "Use the first observation for another action. <tool_call>{\"name\":\"execute_python\",\"arguments\":{\"code\":\"print('two')\"}}</tool_call>",
        "Final answer after two observations.",
    ])
    client.app.state.model_adapter = adapter

    async def fake_execute_python(code, policy_approved=False):
        return {"stdout": code, "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(server, "execute_python", fake_execute_python)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "Complete a two-step task"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
            "max_task_steps": 5,
        },
    )

    assert response.status_code == 200
    assert "event: tool_repair" in response.text
    assert "nonzero_exit" in response.text
    assert "The repaired tool succeeded." in response.text
    assert "second-run" in response.text
    assert len(adapter.calls) == 4
    repair_prompts = [message["content"] for message in adapter.calls[1] if message["role"] == "system"]
    assert any("A registered tool failed. Repair it before continuing." in prompt for prompt in repair_prompts)
    assert any("Inspect the tool file" in prompt for prompt in repair_prompts)

    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    flaky = next(tool for tool in registry["tools"] if tool["name"] == "flaky_tool")
    attempts = flaky["metadata"]["repair_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["failure"]["type"] == "nonzero_exit"

    repaired = server.run_registered_tool(flaky, {"value": "direct"})
    assert repaired["exit_code"] == 0
    assert json.loads(repaired["stdout"])["value"] == "direct"
    assert "event: task_plan" in response.text
    assert "event: task_step" in response.text
    assert "event: task_observation" in response.text
    assert "event: task_revision" in response.text
    assert "Final answer after two observations." in response.text
    assert len(adapter.calls) == 3

    task_files = list((tmp_path / "data" / "tasks").glob("*.json"))
    assert len(task_files) == 1
    task_state = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert task_state["done"] is True
    assert task_state["phase"] == "final"
    assert len(task_state["steps"]) == 2
    assert len(task_state["observations"]) == 2
    assert task_state["artifacts"]["final_answer"] == "Final answer after two observations."
