from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from pydantic import BaseModel, Field

from agent_loop import TaskState, load_task_state, save_task_state


class SubagentSpec(BaseModel):
    goal: str
    context: dict[str, Any] | str = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    memory_scope: str = "isolated"
    sandbox_tier: str = "read_only"
    max_steps: int = 4
    parent_task_id: str


class SubagentRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_task_id: str
    spec: SubagentSpec
    state: TaskState
    result: dict[str, Any] = Field(default_factory=dict)
    denied_tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ModelAdapter(Protocol):
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str) -> AsyncIterator[str]: ...


ToolRunner = Callable[[str, dict[str, Any], SubagentSpec], Any]


class EchoAdapter:
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str) -> AsyncIterator[str]:
        yield f"Subagent completed: {messages[-1]['content']}"


def subagents_dir(data_dir: Path) -> Path:
    return data_dir / "subagents"


def subagent_path(data_dir: Path, run_id: str) -> Path:
    return subagents_dir(data_dir) / f"{run_id}.json"


def _save_run(run: SubagentRun, data_dir: Path) -> None:
    subagents_dir(data_dir).mkdir(parents=True, exist_ok=True)
    path = subagent_path(data_dir, run.run_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def load_subagent_run(data_dir: Path, run_id: str) -> SubagentRun | None:
    path = subagent_path(data_dir, run_id)
    if not path.exists():
        return None
    return SubagentRun.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _extract_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("arguments", {}), dict):
            calls.append(payload)
    return calls


def _strip_tool_calls(text: str) -> str:
    return re.sub(r"<tool_call>\s*\{.*?\}\s*</tool_call>", "", text, flags=re.DOTALL).strip()


def _system_prompt(spec: SubagentSpec) -> str:
    return (
        "You are an isolated subagent. Use only explicitly allowed tools. "
        f"Allowed tools: {', '.join(spec.allowed_tools) or 'none'}. "
        f"Memory scope: {spec.memory_scope}. Sandbox tier: {spec.sandbox_tier}. "
        "Do not request or reveal parent-private memory beyond the provided context."
    )


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def run_subagent_async(
    spec: SubagentSpec,
    *,
    data_dir: Path | None = None,
    model_adapter: ModelAdapter | None = None,
    tool_runner: ToolRunner | None = None,
    model: str = "subagent-local",
    provider: str = "local",
) -> SubagentRun:
    data_root = data_dir or Path(__file__).resolve().parent / "data"
    adapter = model_adapter or EchoAdapter()
    allowed_tools = set(spec.allowed_tools)
    state = TaskState(goal=spec.goal)
    run = SubagentRun(parent_task_id=spec.parent_task_id, spec=spec, state=state)
    _save_run(run, data_root)

    conversation = [
        {"role": "system", "content": _system_prompt(spec)},
        {"role": "user", "content": json.dumps({"goal": spec.goal, "context": spec.context}, ensure_ascii=True)},
    ]
    max_steps = max(1, int(spec.max_steps or 1))
    turns = 0
    while not state.done and turns < max_steps:
        turns += 1
        text = ""
        async for token in adapter.complete(conversation, model, provider):
            text += token
        calls = _extract_tool_calls(text)
        display = _strip_tool_calls(text)
        if not state.steps:
            state.add_plan(display or text)
        conversation.append({"role": "assistant", "content": text})
        if not calls:
            state.done = True
            state.mark_phase("final")
            state.artifacts["final_answer"] = display or text
            run.result = {"answer": state.artifacts["final_answer"], "observations": state.observations}
            break
        state.mark_phase("act")
        for call in calls:
            tool_name = str(call.get("name", ""))
            arguments = call.get("arguments", {}) if isinstance(call.get("arguments", {}), dict) else {}
            step = state.add_step(tool_name, arguments)
            if tool_name not in allowed_tools:
                result = {"error": f"Tool {tool_name} is not allowed for this subagent.", "allowed_tools": sorted(allowed_tools)}
                run.denied_tool_calls.append({"tool_name": tool_name, "arguments": arguments})
                observation = state.add_observation(step, result, False)
            else:
                if tool_runner is None:
                    result = {"error": f"No tool runner configured for {tool_name}."}
                else:
                    result = await _maybe_await(tool_runner(tool_name, arguments, spec))
                observation = state.add_observation(step, result, True)
            conversation.append({"role": "tool", "content": json.dumps(observation["result"], ensure_ascii=True)})
        state.mark_phase("observe")
        _save_run(run, data_root)
    if not state.done:
        state.done = True
        state.mark_phase("final")
        state.artifacts["final_answer"] = "Subagent stopped after reaching max_steps."
        run.result = {"answer": state.artifacts["final_answer"], "observations": state.observations, "reason": "step_limit"}
    run.state = state
    _save_run(run, data_root)

    parent = load_task_state(data_root, spec.parent_task_id)
    if parent is not None:
        parent.add_subagent_run({
            "run_id": run.run_id,
            "task_id": run.task_id,
            "goal": spec.goal,
            "result": run.result,
            "memory_scope": spec.memory_scope,
            "allowed_tools": spec.allowed_tools,
        })
        save_task_state(parent, data_root)
    return run


def run_subagent(spec: SubagentSpec, **kwargs: Any) -> SubagentRun:
    return asyncio.run(run_subagent_async(spec, **kwargs))
