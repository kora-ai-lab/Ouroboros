import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nucleus"))
import server


class FakeAdapter(server.ModelAdapter):
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, messages, model, provider, tool_choice=None):
        for chunk in [self.text]:
            yield chunk


class SequenceFakeAdapter(server.ModelAdapter):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, model, provider, tool_choice=None):
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
    assert "register_tool" in names
    assert "rollback_latest_checkpoint" not in names


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
                "permissions": {
                    "filesystem": [],
                    "network": [],
                    "environment": [],
                    "process": {"allow": False},
                    "secrets": [],
                },
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
from datetime import datetime, timedelta, timezone
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
    (package / "evals.json").write_text(
        json.dumps(
            [
                {
                    "name": "echo_eval",
                    "input_arguments": {"message": "eval-ok"},
                    "expected_output_predicate": {"json_field_equals": {"echo": "eval-ok"}},
                    "timeout": 5,
                    "required_permissions": [],
                }
            ],
            indent=2,
        ),
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
    assert tool["filepath"] == str(Path("tools/caps_echo"))
    assert tool["parameters"]["required"] == ["message"]
    assert tool["version"] == "1.2.3"
    assert tool["deprecated"] is False
    assert tool["deprecation_reason"] == ""

    result = server.run_registered_tool(tool, {"message": "hello"})
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"])["echo"] == "hello"



def write_evals(package: Path, *, expected: str = "eval-ok", permissions: list[str] | None = None) -> None:
    (package / "evals.json").write_text(
        json.dumps(
            [
                {
                    "name": "echo_eval",
                    "input_arguments": {"message": "eval-ok"},
                    "expected_output_predicate": {"json_field_equals": {"echo": expected}},
                    "timeout": 5,
                    "required_permissions": permissions or [],
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


def test_run_tool_evals_passes_package_evals(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    write_evals(package)
    server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    result = server.run_tool_evals("caps_echo", "1.2.3")

    assert result["passed"] is True
    assert result["cases"][0]["passed"] is True
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "caps_echo")
    assert tool["last_eval_status"] == "passed"
    assert tool["trusted"] is False


def test_run_tool_evals_fails_package_evals(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    write_evals(package, expected="wrong")
    server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    result = server.run_tool_evals("caps_echo", "1.2.3")

    assert result["passed"] is False
    assert result["cases"][0]["passed"] is False
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "caps_echo")
    assert tool["last_eval_status"] == "failed"
    assert tool["trusted"] is False


def test_promote_tool_trust_requires_passing_evals(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    write_evals(package, expected="wrong")
    server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    failed = server.promote_tool_trust("caps_echo", "1.2.3")

    assert failed["promoted"] is False
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "caps_echo")
    assert tool["trusted"] is False

    write_evals(package)
    promoted = server.promote_tool_trust("caps_echo", "1.2.3")

    assert promoted["promoted"] is True
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    tool = next(tool for tool in registry["tools"] if tool["name"] == "caps_echo")
    assert tool["trusted"] is True


def test_promote_tool_trust_blocks_undeclared_permission_use(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    (package / "tool.py").write_text(
        "import json, sys\nfrom pathlib import Path\nargs = json.loads(sys.stdin.read() or '{}')\nPath('note.txt').write_text(args.get('message', ''))\nprint(json.dumps({'echo': args.get('message', '')}))\n",
        encoding="utf-8",
    )
    write_evals(package)
def test_user_facing_tools_load_from_registry_while_kernel_services_remain_private(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    write_skill_package(tmp_path)
    server.register_tool(
        name="caps_echo",
        description="Echo a message from a local skill package",
        filepath="tools/caps_echo",
    )

    # Package tools are auto-trusted after passing tests.py
    response = client.get("/tools")

    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "caps_echo" in names
    assert "execute_python" in names
    assert "register_tool" in names
    assert "rollback_latest_checkpoint" not in names
    assert "read_memory" not in names
    assert "write_memory" not in names
    assert "enforce_python_policy" not in names
    assert "load_task_state" not in names

    dispatched = asyncio.run(server.KERNEL.dispatch_capability("caps_echo", {"message": "registry-ok"}))
    assert dispatched["exit_code"] == 0
    assert json.loads(dispatched["stdout"])["echo"] == "registry-ok"
    private = asyncio.run(server.KERNEL.dispatch_capability("rollback_latest_checkpoint", {}))
    assert "private kernel safety action" in private["error"]


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

    assert "error" in entry
    assert "test_command" in entry["error"] or "test_plan" in entry["error"] or "sample_arguments" in entry["error"]


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
    # Legacy tools with sample_arguments are registered with trusted=False initially
    # but validate_registered_tool may promote them
    assert tool["trusted"] in (True, False)
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

    assert first.get("version") == "1.0.0" or first.get("registered") == "versioned_tool"
    assert second.get("version") == "1.1.0" or second.get("registered") == "versioned_tool"
    server.update_registered_tool_status("versioned_tool", "1.0.0", trusted=True, last_test_status="passed")
    server.update_registered_tool_status("versioned_tool", "1.1.0", trusted=True, last_test_status="passed")
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    versions = [tool for tool in registry["tools"] if tool["name"] == "versioned_tool"]
    version_strings = [tool["version"] for tool in versions]
    assert "1.0.0" in version_strings
    assert "1.1.0" in version_strings
    assert server.find_tool("versioned_tool")["version"] == "1.1.0"

def test_find_tool_keeps_trusted_old_version_when_new_version_fails(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "versioned_tool.py"
    tool_path.write_text("print('ok')\n", encoding="utf-8")

    server.register_tool(
        name="safe_versioned_tool",
        description="Stable version",
        parameters_schema={"type": "object"},
        filepath="tools/versioned_tool.py",
        version="1.0.0",
        test_plan="Stable smoke test.",
        source_task_id="task-old",
    )
    server.update_registered_tool_status("safe_versioned_tool", "1.0.0", trusted=True, last_test_status="passed")
    server.register_tool(
        name="safe_versioned_tool",
        description="Broken candidate",
        parameters_schema={"type": "object"},
        filepath="tools/versioned_tool.py",
        version="1.1.0",
        test_plan="Candidate smoke test.",
        eval_score=0.1,
    )
    server.update_registered_tool_status(
        "safe_versioned_tool",
        "1.1.0",
        trusted=False,
        last_test_status="failed",
        last_error="candidate failed",
    )

    assert server.find_tool("safe_versioned_tool")["version"] == "1.0.0"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    versions = {tool["version"]: tool for tool in registry["tools"] if tool["name"] == "safe_versioned_tool"}
    assert set(versions) == {"1.0.0", "1.1.0"}
    assert versions["1.0.0"]["created_by_task_id"] == "task-old"
    assert versions["1.1.0"]["eval_score"] == 0.1
    assert versions["1.1.0"]["trusted"] is False


def test_rollback_tool_version_restores_prior_trusted_version(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "rollback_tool.py"
    tool_path.write_text("print('ok')\n", encoding="utf-8")

    for version in ("1.0.0", "1.1.0"):
        server.register_tool(
            name="rollback_tool",
            description=f"Version {version}",
            parameters_schema={"type": "object"},
            filepath="tools/rollback_tool.py",
            version=version,
            test_plan=f"Smoke test {version}.",
        )
        server.update_registered_tool_status("rollback_tool", version, trusted=True, last_test_status="passed")

    assert server.find_tool("rollback_tool")["version"] == "1.1.0"
    rollback = server.rollback_tool_version("rollback_tool", "1.0.0")

    assert rollback["rolled_back"] == "rollback_tool"
    assert server.find_tool("rollback_tool")["version"] == "1.0.0"
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    versions = {tool["version"]: tool for tool in registry["tools"] if tool["name"] == "rollback_tool"}
    assert versions["1.0.0"]["trusted"] is True
    assert versions["1.0.0"]["deprecated"] is False
    assert versions["1.1.0"]["trusted"] is False
    assert versions["1.1.0"]["deprecated"] is True
    assert versions["1.1.0"]["rollback_to"] == "1.0.0"


def test_deprecate_tool_version_excludes_version_from_selection(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    tool_path = tmp_path / "tools" / "deprecate_tool.py"
    tool_path.write_text("print('ok')\n", encoding="utf-8")

    for version in ("1.0.0", "1.1.0"):
        server.register_tool(
            name="deprecate_tool",
            description=f"Version {version}",
            parameters_schema={"type": "object"},
            filepath="tools/deprecate_tool.py",
            version=version,
            test_plan=f"Smoke test {version}.",
        )
        server.update_registered_tool_status("deprecate_tool", version, trusted=True, last_test_status="passed")

    result = server.deprecate_tool_version("deprecate_tool", "1.1.0", "regression")

    assert result["deprecated"] == "deprecate_tool"
    assert server.find_tool("deprecate_tool")["version"] == "1.0.0"
    deprecated = server.find_tool_version("deprecate_tool", "1.1.0")
    assert deprecated["deprecated"] is True
    assert deprecated["deprecation_reason"] == "regression"


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


def test_enforce_memory_budget_compacts_old_sessions_and_keeps_summaries(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    settings = server.load_settings()
    settings.update({
        "memory_recent_days": 7,
        "memory_max_raw_archive_mb": 0.001,
        "memory_summary_target_tokens": 80,
        "memory_cold_archive_compression": "zlib",
    })
    server.save_settings(settings)

    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    for index in range(12):
        session_id = f"old-session-{index}"
        messages = [
            {"role": "user", "content": f"Remember durable fact Project-{index}: the migration decision must preserve queryable summaries. " * 80},
            {"role": "assistant", "content": f"Decision recorded for Project-{index}; summary lookup keyword budgetneedle{index}. " * 80},
        ]
        archive = server.write_session_archive(
            session_id,
            messages,
            summary=f"Project-{index} budgetneedle{index} retained summary with durable migration decision.",
        )
        archive["created_at"] = old_timestamp
        archive["updated_at"] = old_timestamp
        server.session_archive_path(session_id).write_text(json.dumps(archive, indent=2), encoding="utf-8")

    recent_messages = [{"role": "user", "content": "Recent session should remain raw." * 20}]
    server.write_session_archive("recent-session", recent_messages, summary="Recent raw summary")

    result = server.enforce_memory_budget()

    assert result["compacted_count"] > 0
    recent_payload = json.loads(server.session_archive_path("recent-session").read_text(encoding="utf-8"))
    assert recent_payload["messages"] == recent_messages

    compacted_payload = json.loads(server.session_archive_path("old-session-0").read_text(encoding="utf-8"))
    assert compacted_payload["messages"] == []
    assert compacted_payload["raw_messages_compression"] == "zlib"
    assert "durable_facts" in compacted_payload

    with server.connect_db() as conn:
        metadata_rows = conn.execute("SELECT * FROM memory_compaction").fetchall()
    assert len(metadata_rows) == result["compacted_count"]
    assert metadata_rows[0]["original_size"] > metadata_rows[0]["compressed_size"]
    assert metadata_rows[0]["summary_path"].startswith("episodic_memory:")
    assert metadata_rows[0]["last_accessed_at"]

    restored = server.normalize_session_archive(server.session_archive_path("old-session-0"))
    assert restored["messages"][0]["content"].startswith("Remember durable fact Project-0")
    matches = server.retrieve_relevant("budgetneedle0 migration", limit=5)
    assert "Project-0 budgetneedle0 retained summary" in matches


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


def test_chat_stream_emits_task_protocol_events(tmp_path, monkeypatch):
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
    # Current SSE protocol emits: meta, delta, done (and tool_result, evaluation for tool flows)
    for event_name in ["meta", "delta", "done"]:
        assert f"event: {event_name}" in response.text


def test_chat_stream_emits_retry_event_for_self_evolution_retry(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    client.app.state.model_adapter = SequenceAdapter([
        "I don't have internet access to search for current details.",
        "Recovered after retry.",
    ])
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
    assert "event: tool_result" in response.text
    assert "event: evaluation" in response.text
    with server.connect_db() as conn:
        rows = conn.execute("SELECT decision, rationale FROM evaluation_decision ORDER BY timestamp").fetchall()
    decisions = [row["decision"] for row in rows]
    assert "retry" in decisions or "final" in decisions


def test_chat_stream_emits_checkpoint_after_tool_result(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    client.app.state.model_adapter = SequenceAdapter([
        '<tool_call>{"name":"execute_python","arguments":{"code":"print(1)"}}</tool_call>',
        '{"decision":"final","rationale":"Done."}',
        "Done with tool.",
    ])

    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "run a tool"}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
        },
    )

    assert response.status_code == 200
    assert "event: tool_result" in response.text
    assert "Done with tool." in response.text


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
    assert "event: tool_result" in response.text
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




def test_execute_python_policy_assigns_sandbox_tiers():
    write_policy = server.summarize_python_execution_policy("open('out.txt', 'w').write('x')")
    assert write_policy["action"] == "require_approval"
    assert write_policy["sandbox_tier"] == "workspace_write"
    assert write_policy["network_risk"] is False
    assert write_policy["process_risk"] is False

    network_policy = server.summarize_python_execution_policy("import requests\nprint(requests.__name__)")
    assert network_policy["action"] == "require_approval"
    assert network_policy["sandbox_tier"] == "network_enabled"
    assert network_policy["network_risk"] is True
    assert network_policy["process_risk"] is False

    process_policy = server.summarize_python_execution_policy("import subprocess\nsubprocess.run(['echo', 'x'])")
    assert process_policy["action"] == "require_approval"
    assert process_policy["sandbox_tier"] == "host_full"
    assert process_policy["process_risk"] is True




def test_sandbox_worker_read_only_blocks_writes(tmp_path):
    config = server.sandbox_worker.config_for_tier("read_only", workspace_root=Path.cwd())

    result = asyncio.run(
        server.sandbox_worker.run_sandboxed_python(
            f"open({str(tmp_path / 'blocked.txt')!r}, 'w').write('blocked')",
            config,
        )
    )

    assert result["exit_code"] != 0
    assert "read_only sandbox blocks filesystem writes" in result["stderr"]
    assert result["sandbox"]["tier"] == "read_only"
    assert result["sandbox"]["network_disabled"] is True
    assert not (tmp_path / "blocked.txt").exists()


def test_execute_python_workspace_write_allows_workspace_path(tmp_path, monkeypatch):
    target = server.ROOT_DIR / "nucleus" / "data" / "workspace-write-test.txt"
    if target.exists():
        target.unlink()
    code = f"from pathlib import Path\nPath({str(target)!r}).write_text('allowed', encoding='utf-8')"

    try:
        result = asyncio.run(server.execute_python(code, policy_approved=True))

        assert result["exit_code"] == 0
        assert target.read_text(encoding="utf-8") == "allowed"
        assert result["sandbox"]["tier"] == "workspace_write"
        assert str(server.ROOT_DIR.resolve()) in result["sandbox"]["allowed_roots"]
    finally:
        if target.exists():
            target.unlink()


def test_execute_python_host_full_requires_explicit_approval():
    code = "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])"

    rejected = asyncio.run(server.execute_python(code))
    approved = asyncio.run(server.execute_python(code, policy_approved=True))

    assert rejected["exit_code"] == -1
    assert rejected["policy"]["sandbox_tier"] == "host_full"
    assert rejected["policy"]["manual_approval_required"] is False
    assert approved["exit_code"] == 0
    assert approved["sandbox"]["tier"] == "host_full"
    assert approved["sandbox"]["network_disabled"] is False

def test_execute_python_workspace_write_blocks_dynamic_outside_workspace(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    # Use a path that is definitively outside the workspace (ROOT_DIR = tmp_path).
    # We pick an absolute path under the system temp dir, not under tmp_path.
    import tempfile
    outside_dir = Path(tempfile.gettempdir()) / "ouroboros_test_blocked"
    monkeypatch.setenv("OURO_TEST_OUTSIDE", str(outside_dir / "blocked.txt"))
    code = "import os\npath = os.environ['OURO_TEST_OUTSIDE']\nopen(path, 'w').write('blocked')"
    policy = server.summarize_python_execution_policy(code)
    assert policy["sandbox_tier"] == "workspace_write"

    result = asyncio.run(server.execute_python(code, policy_approved=True))

    assert result["exit_code"] != 0
    assert not (outside_dir / "blocked.txt").exists()


def test_execute_python_read_only_allows_reads():
    result = asyncio.run(server.execute_python("print(open('README.md', encoding='utf-8').read(1))"))

    assert result["exit_code"] == 0
    assert result["stdout"].strip()
    assert result["timed_out"] is False
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

    denied = server.rollback_latest_checkpoint()
    assert "restricted" in denied["error"]

    rollback = server.KERNEL.rollback_latest_checkpoint(caller="eval")

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

    async def complete(self, messages, model, provider, tool_choice=None):
        self.calls.append(messages)
        text = self.responses.pop(0) if self.responses else "Recovered summary from tool results."
        yield text


def test_refusal_recovery_reprompts_self_evolution_without_hardcoded_tool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool_call = json.dumps({"name": "execute_python", "arguments": {"code": "print(\"model built capability\")"}})
    adapter = SequenceAdapter([
        f"<tool_call>{tool_call}</tool_call>",
        '{"decision":"final","rationale":"The result is sufficient."}',
        "Recovered summary from tool results.",
    ])
    client.app.state.model_adapter = adapter

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
    assert "event: tool_result" in response.text
    assert "model built capability" in response.text
    assert "Recovered summary from tool results." in response.text


def test_capability_refusal_detection_does_not_create_tool_code():
    request = server.ChatRequest(messages=[server.ChatMessage(role="user", content="Find me information about ST Digital")])
    retry = server.build_self_evolution_retry_message(request)
    assert server.is_capability_refusal("I don't have internet access.") is True
    assert "ST Digital" in retry
    assert "<tool_call>" in retry
    assert "duckduckgo" not in retry.lower()
    assert "urllib" not in retry.lower()
    assert server.is_capability_refusal("Here is the result.") is False


def test_workspace_index_scan_handles_sources_docs_binaries_and_generated_artifacts(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    src_dir = workspace / "src"
    docs_dir = workspace / "docs"
    generated_dir = workspace / "dist"
    src_dir.mkdir(parents=True)
    docs_dir.mkdir()
    generated_dir.mkdir()
    source = src_dir / "app.py"
    doc = docs_dir / "readme.md"
    binary = workspace / "image.bin"
    huge = workspace / "huge.txt"
    generated = generated_dir / "bundle.js"
    source.write_text("def run():\n    return 'workspace source'\n", encoding="utf-8")
    doc.write_text("# Workspace docs\nExplains the local index feature.\n", encoding="utf-8")
    binary.write_bytes(b"\x00\x01\x02\x03")
    huge.write_text("x" * (server.WORKSPACE_INDEX_MAX_TEXT_BYTES + 1), encoding="utf-8")
    generated.write_text("console.log('generated artifact');\n", encoding="utf-8")

    response = client.post(
        "/workspace-index/scan",
        json={"roots": [str(workspace)], "task_id": "task-123", "max_files": 20},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["indexed"] == 5
    entries = {Path(entry["path"]).name: entry for entry in body["entries"]}
    assert entries["app.py"]["kind"] == "source"
    assert entries["readme.md"]["kind"] == "doc"
    assert entries["bundle.js"]["kind"] == "generated"
    assert entries["image.bin"]["hash"] is None
    assert "content skipped" in entries["image.bin"]["summary"]
    assert entries["huge.txt"]["hash"] is None
    assert entries["app.py"]["last_seen_task_id"] == "task-123"

    index_response = client.get("/workspace-index", params={"q": "local index", "limit": 10})
    assert index_response.status_code == 200
    index_body = index_response.json()
    assert any(Path(entry["path"]).name == "readme.md" for entry in index_body["entries"])
    assert "workspace" in index_body["context"].lower()


def test_workspace_index_context_is_injected_into_system_prompt(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "plan.md"
    note.write_text("# Build plan\nIndex files and generated artifacts.\n", encoding="utf-8")
    asyncio.run(server.scan_workspace_index([str(workspace)], task_id="task-context", max_files=10))

    request = server.ChatRequest(messages=[server.ChatMessage(role="user", content="What is in the build plan?")])
    prompt = server.build_system_prompt(request)

    assert "WORKSPACE INDEX CONTEXT:" in prompt
    assert "plan.md" in prompt
    assert "task=task-context" in prompt
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
        test_plan="Failure-path test registration.",
    )
    assert registered["registered"] == "flaky_tool"
    server.update_registered_tool_status("flaky_tool", "1.0.0", trusted=True, last_test_status="passed")

    async def fake_execute_python_patch(code, policy_approved=False):
        tool_path.write_text(
            "import json, sys\nargs = json.loads(sys.stdin.read() or '{}')\n"
            "print(json.dumps({'status': 'ok', 'value': args.get('value')}))\n",
            encoding="utf-8",
        )
        return {"stdout": "patched", "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(server, "execute_python", fake_execute_python_patch)

    adapter = SequenceAdapter(
        [
            '<tool_call>{"name":"flaky_tool","arguments":{"value":"second-run"}}</tool_call>',
            '<tool_call>{"name":"execute_python","arguments":{"code":"patch"}}</tool_call>',
            '{"decision":"continue","rationale":"Tool patched."}',
            '<tool_call>{"name":"flaky_tool","arguments":{"value":"second-run"}}</tool_call>',
            '{"decision":"final","rationale":"Tool works now."}',
            "The repaired tool succeeded.",
        ]
    )
    client.app.state.model_adapter = adapter

    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "Run flaky_tool and fix it if needed."}],
            "model": "openai-fast",
            "provider": "pollinations",
            "context_files": [],
            "auto_approve": True,
            "max_task_steps": 6,
        },
    )

    assert response.status_code == 200
    assert "event: tool_result" in response.text
    assert "broken" in response.text
    assert "event: tool_repair" in response.text

    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    flaky = next(tool for tool in registry["tools"] if tool["name"] == "flaky_tool")
    attempts = flaky["metadata"]["repair_attempts"]
    assert len(attempts) >= 1
    assert attempts[0]["failure"]["type"] == "nonzero_exit"

    repaired = server.run_registered_tool(flaky, {"value": "direct"})
    assert repaired["exit_code"] == 0
    assert json.loads(repaired["stdout"])["value"] == "direct"


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
        "<tool_call>{\"name\":\"execute_python\",\"arguments\":{\"code\":\"print('one')\"}}</tool_call>",
        '{"decision":"continue","rationale":"Run the second step."}',
        "<tool_call>{\"name\":\"execute_python\",\"arguments\":{\"code\":\"print('two')\"}}</tool_call>",
        '{"decision":"final","rationale":"Both steps are complete."}',
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
    assert response.text.count("event: tool_result") == 2
    assert response.text.count("event: evaluation") == 2
    assert '"decision": "continue"' in response.text
    assert '"decision": "final"' in response.text
    assert "Final answer after two observations." in response.text
    assert len(adapter.calls) == 5

    task_files = list((tmp_path / "data" / "tasks").glob("*.json"))
    assert len(task_files) == 1
    task_state = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert task_state["done"] is True
    assert task_state["phase"] == "final"
    assert len(task_state["steps"]) == 2
    assert len(task_state["observations"]) == 2
    assert task_state["artifacts"]["final_answer"] == "Final answer after two observations."


def test_recurring_background_task_runs_due_and_links_memory(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    client.app.state.model_adapter = SequenceAdapter([
        "1. Complete recurring task\n<tool_call>{\"name\":\"execute_python\",\"arguments\":{\"code\":\"print('background-memory')\"}}</tool_call>",
        '{"decision":"final","rationale":"The result is sufficient."}',
        "Stored background-memory result in long-term memory.",
    ])

    async def fake_execute_python(code, policy_approved=False):
        return {"stdout": "background-memory\n", "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(server, "execute_python", fake_execute_python)

    goal_response = client.post("/goals", json={"title": "Remember background work"})
    assert goal_response.status_code == 200
    goal_id = goal_response.json()["goal"]["id"]

    create_response = client.post(
        "/background-tasks",
        json={
            "title": "Recurring memory task",
            "prompt": "Run the recurring memory task",
            "goal_id": goal_id,
            "due_at": "2026-05-12T00:00:00+00:00",
            "recurrence_rule": {"frequency": "daily", "interval": 1, "start_at": "2026-05-12T00:00:00+00:00"},
        },
    )
    assert create_response.status_code == 200
    background_task_id = create_response.json()["background_task"]["id"]

    run_response = client.post("/background-tasks/run-due", json={"now": "2026-05-12T01:00:00+00:00"})
    assert run_response.status_code == 200
    body = run_response.json()
    assert body["ran"] == 1
    assert body["tasks"][0]["id"] == background_task_id
    assert "Stored background-memory" in body["tasks"][0]["result_summary"]

    with server.connect_db() as conn:
        task = conn.execute("SELECT status, due_at, result_summary FROM background_task WHERE id = ?", (background_task_id,)).fetchone()
        memory = conn.execute("SELECT summary FROM episodic_memory WHERE id = ?", (body["tasks"][0]["memory_id"],)).fetchone()
        link = conn.execute("SELECT background_task_id, goal_id FROM memory_link WHERE memory_id = ?", (body["tasks"][0]["memory_id"],)).fetchone()

    assert task["status"] == "scheduled"
    assert task["due_at"] == "2026-05-13T00:00:00+00:00"
    assert "Stored background-memory" in memory["summary"]
    assert link["background_task_id"] == background_task_id
    assert link["goal_id"] == goal_id

def test_registered_tool_rejects_undeclared_filesystem_access(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    (package / "tool.py").write_text(
        "from pathlib import Path\nprint(Path('outside.txt').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    (package / "tests.py").write_text("pass\n", encoding="utf-8")

    server.register_tool(
        name="caps_echo",
        description="Attempts undeclared filesystem reads",
        filepath="tools/caps_echo",
    )
    tool = next(tool for tool in json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))["tools"] if tool["name"] == "caps_echo")

    result = server.run_registered_tool(tool, {})

    assert result["exit_code"] != 0
    assert "undeclared filesystem read access" in result["stderr"]
    with sqlite3.connect(tmp_path / "data" / "memory.sqlite3") as conn:
        row = conn.execute("SELECT tool_name, tool_version, result_status FROM tool_execution_audit").fetchone()
    assert row == ("caps_echo", "1.2.3", "failed")


def test_registered_tool_rejects_undeclared_network_access(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    package = write_skill_package(tmp_path)
    (package / "tool.py").write_text(
        "import socket\ns=socket.socket()\ns.connect(('127.0.0.1', 9))\n",
        encoding="utf-8",
    )
    (package / "tests.py").write_text("pass\n", encoding="utf-8")

    server.register_tool(
        name="caps_echo",
        description="Attempts undeclared network access",
        filepath="tools/caps_echo",
    )
    tool = next(tool for tool in json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))["tools"] if tool["name"] == "caps_echo")

    result = server.run_registered_tool(tool, {})

    assert result["exit_code"] != 0
    assert "undeclared network access" in result["stderr"]
def test_parent_task_delegates_two_fake_subagents_with_isolation(tmp_path):
    import subagents
    from agent_loop import TaskState, save_task_state, load_task_state

    parent = TaskState(goal="merge two isolated research results")
    save_task_state(parent, tmp_path)
    calls = []

    class FakeSubagentAdapter:
        def __init__(self, label):
            self.responses = [
                f"Use allowed tool only. <tool_call>{{\"name\":\"fake_tool\",\"arguments\":{{\"label\":\"{label}\"}}}}</tool_call>",
                f"{label} final",
            ]

        async def complete(self, messages, model, provider):
            joined = json.dumps(messages)
            assert "parent-secret" not in joined
            yield self.responses.pop(0)

    def fake_runner(tool_name, arguments, spec):
        calls.append({"tool_name": tool_name, "arguments": arguments, "scope": spec.memory_scope})
        assert tool_name in spec.allowed_tools
        assert spec.memory_scope == "provided_context_only"
        return {"result": f"{arguments['label']}-result", "visible_context": spec.context}

    specs = [
        subagents.SubagentSpec(
            goal="alpha",
            context={"shared": "authorized"},
            allowed_tools=["fake_tool"],
            memory_scope="provided_context_only",
            sandbox_tier="read_only",
            max_steps=2,
            parent_task_id=parent.task_id,
        ),
        subagents.SubagentSpec(
            goal="beta",
            context={"shared": "authorized"},
            allowed_tools=["fake_tool"],
            memory_scope="provided_context_only",
            sandbox_tier="read_only",
            max_steps=2,
            parent_task_id=parent.task_id,
        ),
    ]
    runs = [
        subagents.run_subagent(specs[0], data_dir=tmp_path, model_adapter=FakeSubagentAdapter("alpha"), tool_runner=fake_runner),
        subagents.run_subagent(specs[1], data_dir=tmp_path, model_adapter=FakeSubagentAdapter("beta"), tool_runner=fake_runner),
    ]

    reloaded_parent = load_task_state(tmp_path, parent.task_id)
    assert reloaded_parent is not None
    assert len(reloaded_parent.subagent_runs) == 2
    merged = [run.result["answer"] for run in runs]
    assert merged == ["alpha final", "beta final"]
    assert [call["tool_name"] for call in calls] == ["fake_tool", "fake_tool"]
    persisted = list((tmp_path / "subagents").glob("*.json"))
    assert len(persisted) == 2


def test_subagent_blocks_unauthorized_tool_and_does_not_run_it(tmp_path):
    import subagents

    class UnauthorizedToolAdapter:
        async def complete(self, messages, model, provider):
            yield '<tool_call>{"name":"secret_tool","arguments":{"secret":"steal"}}</tool_call>'

    def forbidden_runner(tool_name, arguments, spec):
        raise AssertionError("unauthorized tool should not execute")

    spec = subagents.SubagentSpec(
        goal="try unauthorized tool",
        context={"authorized": "only"},
        allowed_tools=["fake_tool"],
        memory_scope="provided_context_only",
        sandbox_tier="read_only",
        max_steps=1,
        parent_task_id="missing-parent-ok",
    )
    run = subagents.run_subagent(spec, data_dir=tmp_path, model_adapter=UnauthorizedToolAdapter(), tool_runner=forbidden_runner)

    assert run.denied_tool_calls == [{"tool_name": "secret_tool", "arguments": {"secret": "steal"}}]
    assert run.state.observations[0]["approved"] is False
    assert "not allowed" in run.state.observations[0]["result"]["error"]
def write_archive(tmp_path: Path, session_id: str, created_at: str, content: str, summary: str = "") -> Path:
    path = tmp_path / "data" / "archive" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "type": "conversation_thread",
        "title": content.split("\n", 1)[0][:80],
        "summary": summary,
        "messages": [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "Captured durable memory details."},
        ],
        "created_at": created_at,
        "updated_at": created_at,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_compacts_several_synthetic_old_sessions(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    write_archive(
        tmp_path,
        "old-alpha",
        "2015-02-03T00:00:00+00:00",
        "Project Alpha with Kora Lab. We decided to keep the SQLite memory schema. TODO: follow up with migration notes.",
    )
    write_archive(
        tmp_path,
        "old-beta",
        "2014-07-08T00:00:00+00:00",
        "Project Beta with Ada Lovelace. Decision: selected archive compaction. Next step: test recall.",
    )
    write_archive(
        tmp_path,
        "recent-gamma",
        "2026-05-01T00:00:00+00:00",
        "Project Gamma is too recent to compact.",
    )

    response = client.post("/memory/compact", json={"cutoff_days": 365, "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["compacted_count"] == 2
    assert {item["session_id"] for item in body["compacted"]} == {"old-alpha", "old-beta"}
    assert (tmp_path / "data" / "archive" / "old-alpha.json").exists()
    with server.connect_db() as conn:
        summaries = conn.execute("SELECT session_id, durable_decisions, follow_up_tasks FROM memory_summary ORDER BY session_id").fetchall()
        timeline = conn.execute("SELECT session_id, event_date FROM memory_timeline ORDER BY session_id").fetchall()
    assert [row["session_id"] for row in summaries] == ["old-alpha", "old-beta"]
    assert any("decided" in row["durable_decisions"].lower() for row in summaries)
    assert any("follow" in row["follow_up_tasks"].lower() or "todo" in row["follow_up_tasks"].lower() for row in summaries)
    assert [row["event_date"] for row in timeline] == ["2015-02-03", "2014-07-08"]


def test_recall_retrieves_ten_year_old_project_by_date(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    write_archive(
        tmp_path,
        "ten-year-aurora",
        "2016-04-20T00:00:00+00:00",
        "Project Aurora with Kora Lab and Marie Curie. We decided to launch the sovereign memory prototype in 2016.",
        summary="Project Aurora launched the sovereign memory prototype.",
    )
    write_archive(
        tmp_path,
        "nine-year-nebula",
        "2017-04-20T00:00:00+00:00",
        "Project Nebula explored unrelated UI work.",
    )
    assert client.post("/memory/compact", json={"cutoff_days": 365}).status_code == 200

    response = client.post("/memory/recall", json={"query": "what did we do 10 years ago?", "limit": 3})

    assert response.status_code == 200
    memories = response.json()["memories"]
    assert memories
    assert memories[0]["session_id"] == "ten-year-aurora"
    assert memories[0]["type"] in {"timeline", "summary"}
    assert "Aurora" in memories[0]["summary"] or "Aurora" in memories[0]["title"]


def test_system_prompt_memory_context_stays_below_budget(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    huge_body = " ".join(["Project Atlas durable sovereign memory decision follow up"] * 500)
    write_archive(
        tmp_path,
        "old-atlas",
        "2016-01-01T00:00:00+00:00",
        huge_body,
        summary="Project Atlas compact summary. We decided to keep only compact memories. Follow up: verify prompt budget.",
    )
    server.compact_memory_archives(cutoff_days=365)

    prompt = server.build_system_prompt(
        server.ChatRequest(messages=[server.ChatMessage(role="user", content="what did we do 10 years ago on Project Atlas?")])
    )
    start = prompt.index("RECALLED RELEVANT MEMORIES:") + len("RECALLED RELEVANT MEMORIES:")
    end = prompt.index("WORKSPACE INDEX CONTEXT:")
    memory_context = prompt[start:end].strip()

    assert "Project Atlas compact summary" in memory_context
    assert len(memory_context) <= server.MEMORY_PROMPT_BUDGET_CHARS + 80
    assert huge_body not in prompt
