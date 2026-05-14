from __future__ import annotations

import json, sys, asyncio
from pathlib import Path

NUCLEUS_DIR = Path(__file__).resolve().parents[2]
if str(NUCLEUS_DIR) not in sys.path:
    sys.path.insert(0, str(NUCLEUS_DIR))

from subagents import SubagentSpec, run_subagent_async, LEVEL_ORDER


def _level_index(title: str) -> int:
    return LEVEL_ORDER.index(title) if title in LEVEL_ORDER else -1


def _children(title: str, max_depth: int) -> list[str]:
    idx = _level_index(title)
    if idx < 0:
        return []
    max_child = min(idx - 1, max_depth - 1)
    return LEVEL_ORDER[max(0, max_child):idx]


async def _delegate(spec: SubagentSpec, depth: int, max_depth: int) -> dict:
    run = await run_subagent_async(spec)
    result = run.result or {}
    if depth >= max_depth or not spec.can_delegate():
        return result
    children_titles = _children(spec.title, max_depth - depth)
    if not children_titles or not result.get("observations"):
        return result
    subtasks = []
    for i, obs in enumerate(result.get("observations", [])[:len(children_titles)]):
        child_title = children_titles[i]
        child_spec = SubagentSpec(
            goal=f"Subtask {i+1} of: {spec.goal}",
            context={"parent_result": obs, "scope": spec.scope},
            allowed_tools=spec.allowed_tools,
            sandbox_tier=spec.sandbox_tier,
            max_steps=max(1, spec.max_steps // 2),
            parent_task_id=spec.parent_task_id,
            title=child_title,
            scope=spec.scope,
        )
        subtasks.append(_delegate(child_spec, depth + 1, max_depth))
    child_results = await asyncio.gather(*subtasks)
    result["sub_delegations"] = child_results
    return result


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    goal = str(args.get("goal", ""))
    title = str(args.get("title", "system_god"))
    scope = args.get("scope", {})
    max_depth = max(1, min(int(args.get("max_depth", 3)), 5))

    spec = SubagentSpec(
        goal=goal,
        context=args.get("context", {}),
        allowed_tools=["execute_python", "register_tool", "delegate_subagent"],
        sandbox_tier="workspace_write",
        max_steps=6,
        parent_task_id=scope.get("task_id", "delegation_router"),
        title=title,
        scope=scope,
    )

    result = asyncio.run(_delegate(spec, 0, max_depth))
    print(json.dumps({"status": "ok", "goal": goal, "title": title, "result": result}))


if __name__ == "__main__":
    main()
