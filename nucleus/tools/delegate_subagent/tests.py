import json
import subprocess
import sys
from pathlib import Path

payload = {
    "goal": "summarize isolated context",
    "context": {"note": "ok"},
    "allowed_tools": [],
    "memory_scope": "provided_context_only",
    "sandbox_tier": "read_only",
    "max_steps": 1,
    "parent_task_id": "package-test-parent",
}
completed = subprocess.run(
    [sys.executable, str(Path(__file__).with_name("tool.py"))],
    input=json.dumps(payload),
    text=True,
    encoding="utf-8",
    capture_output=True,
    check=False,
)
assert completed.returncode == 0, completed.stderr
result = json.loads(completed.stdout)
assert result["parent_task_id"] == "package-test-parent"
assert "run_id" in result
