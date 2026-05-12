from __future__ import annotations

import json
import sys
from pathlib import Path

NUCLEUS_DIR = Path(__file__).resolve().parents[2]
if str(NUCLEUS_DIR) not in sys.path:
    sys.path.insert(0, str(NUCLEUS_DIR))

from subagents import SubagentSpec, run_subagent  # noqa: E402


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    spec = SubagentSpec(**args)
    run = run_subagent(spec)
    print(json.dumps({
        "run_id": run.run_id,
        "task_id": run.task_id,
        "parent_task_id": run.parent_task_id,
        "result": run.result,
        "denied_tool_calls": run.denied_tool_calls,
    }))


if __name__ == "__main__":
    main()
