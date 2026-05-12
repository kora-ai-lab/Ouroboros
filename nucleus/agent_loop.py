from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


PHASE_EVENTS = {
    "plan": "task_plan",
    "act": "task_step",
    "observe": "task_observation",
    "evaluate": "task_evaluation",
    "revise": "task_revision",
    "final": "task_final",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskState(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    phase: str = "plan"
    plan: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    subagent_runs: list[dict[str, Any]] = Field(default_factory=list)
    failure_count: int = 0
    done: bool = False
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    def mark_phase(self, phase: str) -> None:
        self.phase = phase
        self.updated_at = now_iso()

    def add_plan(self, text: str) -> None:
        plan_items = extract_plan_items(text)
        if plan_items:
            self.plan = plan_items
        elif text.strip() and not self.plan:
            self.plan = [text.strip()]
        self.updated_at = now_iso()

    def add_step(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        step = {
            "index": len(self.steps) + 1,
            "tool_name": tool_name,
            "arguments": arguments,
            "status": "running",
            "created_at": now_iso(),
        }
        self.steps.append(step)
        self.updated_at = now_iso()
        return step

    def add_subagent_run(self, run: dict[str, Any]) -> None:
        self.subagent_runs.append(run)
        self.updated_at = now_iso()

    def add_observation(self, step: dict[str, Any], result: dict[str, Any], approved: bool) -> dict[str, Any]:
        step["status"] = "completed" if approved else "rejected"
        step["completed_at"] = now_iso()
        observation = {
            "step_index": step["index"],
            "tool_name": step["tool_name"],
            "result": result,
            "approved": approved,
            "created_at": now_iso(),
        }
        self.observations.append(observation)
        self.updated_at = now_iso()
        return observation


def extract_plan_items(text: str) -> list[str]:
    items: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = line.lstrip("-*").strip()
        if len(line) > 2 and line[0].isdigit() and line[1] in {".", ")"}:
            line = line[2:].strip()
        if line:
            items.append(line)
    return items[:20]


def tasks_dir(data_dir: Path) -> Path:
    return data_dir / "tasks"


def task_path(data_dir: Path, task_id: str) -> Path:
    return tasks_dir(data_dir) / f"{task_id}.json"


def save_task_state(state: TaskState, data_dir: Path) -> None:
    directory = tasks_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state.updated_at = now_iso()
    path = task_path(data_dir, state.task_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)


def load_task_state(data_dir: Path, task_id: str) -> TaskState | None:
    path = task_path(data_dir, task_id)
    if not path.exists():
        return None
    return TaskState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def task_event_name(phase: str) -> str:
    return PHASE_EVENTS.get(phase, "task_phase")
