from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import checkpoints
import sandbox_worker
from agent_loop import TaskState, load_task_state, save_task_state, task_event_name


async def background_scheduler_loop() -> None:
    while True:
        try:
            await run_due_background_tasks(RunDueBackgroundTasksRequest(limit=5))
        except Exception as e:
            logger.error(f"Background task scheduler error: {e}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_env_files()
    ensure_layout()
    init_db()
    app.state.model_adapter = ProviderRouter()
    app.state.pending_approvals = {}
    scheduler_task = asyncio.create_task(background_scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Ouroboros Nucleus", lifespan=lifespan)


BASE_DIR = Path(__file__).resolve().parent
SANDBOX_WORKER_PATH = BASE_DIR / "sandbox_worker.py"
ROOT_DIR = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
CHECKPOINTS_DIR = DATA_DIR / "checkpoints"
TOOLS_DIR = BASE_DIR / "tools"
KORA_DIR = BASE_DIR / "kora"
REGISTRY_PATH = BASE_DIR / "registry.json"
FACTS_PATH = BASE_DIR / "facts.json"
RULES_DIR = Path.home() / ".ouroboros" / "rules"
DB_PATH = DATA_DIR / "memory.sqlite3"
SETTINGS_PATH = BASE_DIR / "settings.json"
ENV_PATHS = [ROOT_DIR / ".env", BASE_DIR / ".env"]
POLLINATIONS_BASE_URL = "https://gen.pollinations.ai/v1"
LLAMA_CACHE: dict[str, Any] = {}
MODEL_FILE_EXTENSIONS = {".gguf", ".bin", ".safetensors", ".pt", ".pth"}
WORKSPACE_INDEX_TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".toml",
    ".yaml", ".yml", ".html", ".css", ".rs", ".sql", ".sh", ".env",
}
WORKSPACE_INDEX_GENERATED_DIRS = {"dist", "build", "target", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
WORKSPACE_INDEX_MAX_TEXT_BYTES = 256 * 1024
WORKSPACE_INDEX_MAX_HASH_BYTES = 2 * 1024 * 1024
EVALUATION_DECISIONS = {"continue", "retry", "register_tool", "rollback", "final"}
MEMORY_PROMPT_BUDGET_CHARS = 2400
MEMORY_RECALL_DEFAULT_LIMIT = 5
MEMORY_COMPACTION_DEFAULT_CUTOFF_DAYS = 30


MODEL_OPTIONS = {
    "pollinations": [
        "openai-fast",
        "qwen-coder",
        "deepseek",
        "claude-fast",
        "claude",
        "nova-fast",
        "searchgpt",
        "mistral",
    ]
}

MODEL_TIERS = {
    "coding": ["deepseek", "qwen-coder", "claude-fast"],
    "research": ["searchgpt", "deepseek", "claude"],
    "chat": ["openai-fast", "claude-fast", "nova-fast"],
    "fast": ["openai-fast", "nova-fast"],
    "reasoning": ["deepseek", "claude"],
}

RUNTIME_SETTING_DEFAULTS = {
    "n_threads": max((os.cpu_count() or 4) - 1, 1),
    "n_gpu_layers": 0,
    "n_ctx": 4096,
    "n_batch": 512,
    "n_ubatch": 512,
    "use_mmap": True,
    "use_mlock": False,
    "flash_attn": False,
}
RUNTIME_INT_SETTINGS = {"n_threads", "n_gpu_layers", "n_ctx", "n_batch", "n_ubatch"}
RUNTIME_BOOL_SETTINGS = {"use_mmap", "use_mlock", "flash_attn"}
RUNTIME_SETTINGS = tuple(RUNTIME_SETTING_DEFAULTS.keys())

DEFAULT_SETTINGS = {
    "default_provider": "auto",
    "default_model": "openai-fast",
    "provider_priority": ["pollinations"],
    "tool_repair_max_attempts": 2,
    "memory_recent_days": 30,
    "memory_max_raw_archive_mb": 256,
    "memory_summary_target_tokens": 512,
    "memory_cold_archive_compression": "zlib",
    "providers": {
        "pollinations": {
            "label": "Pollinations",
            "type": "openai_compatible",
            "base_url": POLLINATIONS_BASE_URL,
            "api_key_env": "POLLINATIONS_API_KEY",
            "api_keys": [],
            "models": MODEL_OPTIONS["pollinations"],
        }
    },
}

DEFAULT_TOOL_REPAIR_MAX_ATTEMPTS = 2
REGISTERED_TOOL_TIMEOUT_SECONDS = 120

DEFAULT_REGISTRY = {
    "tools": [
        {
            "name": "execute_python",
            "description": "Execute Python code and return stdout and stderr. This is how you gain any capability you do not have.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
            "builtin": True,
            "requires_approval": True,
            "version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "updated_at": "2026-05-12T00:00:00+00:00",
            "source_task_id": None,
            "test_command": None,
            "test_plan": "Built-in tool covered by application tests.",
            "sample_arguments": {},
            "last_test_status": "built_in",
            "last_error": None,
            "use_count": 0,
            "supersedes": None,
            "trusted": True,
        },
        {
            "name": "register_tool",
            "description": "Permanently register a tested Python file or skill package as a versioned tool. Skill packages live under nucleus/tools/ and contain tool.py, schema.json, README.md, tests.py, metadata.json with permissions, and evals.json.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parameters_schema": {"type": "object"},
                    "filepath": {"type": "string"},
                    "requires_approval": {"type": "boolean"},
                    "version": {"type": "string", "default": "1.0.0"},
                    "source_task_id": {"type": "string"},
                    "test_command": {"type": "string"},
                    "test_plan": {"type": "string"},
                    "sample_arguments": {"type": "object"},
                    "supersedes": {"type": "string"},
                },
                "required": ["name", "description", "filepath"],
            },
            "builtin": True,
            "requires_approval": False,
            "version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "updated_at": "2026-05-12T00:00:00+00:00",
            "source_task_id": None,
            "test_command": None,
            "test_plan": "Built-in tool covered by application tests.",
            "sample_arguments": {},
            "last_test_status": "built_in",
            "last_error": None,
            "use_count": 0,
            "supersedes": None,
            "trusted": True,
        },
        {
            "name": "delegate_subagent",
            "description": "Delegate bounded work to an isolated subagent with constrained tools, memory scope, sandbox tier, and step limit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "context": {},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "memory_scope": {"type": "string"},
                    "sandbox_tier": {"type": "string"},
                    "max_steps": {"type": "integer", "minimum": 1},
                    "parent_task_id": {"type": "string"},
                },
                "required": ["goal", "parent_task_id"],
            },
            "filepath": "tools/delegate_subagent",
            "package": True,
            "package_dir": "tools/delegate_subagent",
            "builtin": False,
            "requires_approval": False,
            "metadata": {"version": "1.0.0", "deprecated": False, "deprecation_reason": "", "output_format": "json"},
            "version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "updated_at": "2026-05-12T00:00:00+00:00",
            "source_task_id": None,
            "test_command": "python nucleus/tools/delegate_subagent/tests.py",
            "test_plan": "Package tests cover subprocess delegation and JSON output.",
            "sample_arguments": {
                "goal": "summarize provided context",
                "context": {"note": "sample"},
                "allowed_tools": [],
                "memory_scope": "provided_context_only",
                "sandbox_tier": "read_only",
                "max_steps": 1,
                "parent_task_id": "sample-parent",
            },
            "last_test_status": "pending",
            "last_error": None,
            "use_count": 0,
            "supersedes": None,
            "trusted": False,
        },
        {
            "name": "mcp_bridge",
            "description": "Generic bridge for configured MCP servers: list servers/tools/resources/prompts, read resources, and call arbitrary MCP tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["list_servers", "list_tools", "call_tool", "list_resources", "read_resource", "list_prompts"]},
                    "server": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "uri": {"type": "string"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
            "filepath": "tools/mcp_bridge",
            "package": True,
            "package_dir": "tools/mcp_bridge",
            "builtin": False,
            "requires_approval": False,
            "metadata": {"version": "1.0.0", "deprecated": False, "deprecation_reason": "", "output_format": "json"},
            "version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "updated_at": "2026-05-12T00:00:00+00:00",
            "source_task_id": None,
            "test_command": "python nucleus/tools/mcp_bridge/tests.py",
            "test_plan": "Package smoke test plus adapter unit tests cover generic MCP discovery and call dispatch.",
            "sample_arguments": {"operation": "list_servers"},
            "last_test_status": "pending",
            "last_error": None,
            "use_count": 0,
            "supersedes": None,
            "trusted": False,
        },
    ]
}


DEFAULT_FACTS = {
    "user": {
        "name": "Kheir Lissi",
        "location": "Lome, Togo",
        "role": "Founder of Kora Lab",
    },
    "kora_lab": {
        "mission": "Africa's sovereign AI research and product lab.",
        "constraints": "Zero budget unless explicitly stated.",
    },
}

SYSTEM_TEMPLATE = """
You are Ouroboros, a self-evolving sovereign AI primitive. You are the personal AI workspace of Kheir Lissi, founder of Kora Lab, Lome, Togo.

CORE PRINCIPLE — THIS OVERRIDES EVERYTHING ELSE:
You have exactly two built-in tools: execute_python and register_tool. You can extend your capabilities by using execute_python and register_tool, subject to policy, sandbox, approval, and test/eval gates. execute_python can run policy-approved Python code, which means you can:
- Make HTTP requests when permitted by policy and sandbox
- Read, write, move, or delete files when permitted by policy and sandbox
- Run shell commands via subprocess when permitted by policy and sandbox
- Scrape websites, parse HTML, and download data when permitted by policy and sandbox
- Install packages when permitted by policy, sandbox, approval, and test/eval gates
- Launch browsers, automate GUIs, or control the operating system when permitted by policy, sandbox, and approval gates
- Build or request the minimum additional capability needed for the task

YOU MUST NEVER SAY "I don't have internet access" OR "I can't do that" OR "I don't have that capability" as the final answer. Do not refuse due to missing tools; build the needed capability with execute_python and register_tool, or request the policy approval, sandbox access, or missing details needed to proceed.

SELF-EVOLUTION PROTOCOL:
When you need a capability that is not in your current tool registry:
1. Write the Python code directly using execute_python to accomplish the task immediately.
2. If the capability is reusable, write it to a file in nucleus/tools/ using execute_python.
3. Test the file using execute_python.
4. Register it permanently using register_tool so it persists across sessions.
You do not ask permission to evolve. You do not explain that you lack a tool. You build the tool and use it. If a tool you built fails, you debug it and fix it. If an approach fails (e.g. a website blocks you), you try a different approach autonomously.
You have exactly two built-in primitives: execute_python and register_tool. The kernel stays generic: it does not embed browser selectors, shell commands, application names, websites, or workflow-specific recipes. All concrete operational knowledge belongs in registered skill packages under nucleus/tools/.

DISCOVER NEEDED CAPABILITY PATH:
When no currently registered tool can satisfy a task:
1. Use execute_python to inspect the local environment and prototype the smallest task-specific capability.
2. Exercise the prototype with representative inputs and capture stdout, stderr, exit code, and any artifacts.
3. If the capability is reusable, convert the prototype into a skill package under nucleus/tools/ with tool.py, schema.json, README.md, tests.py, metadata.json, and evals.json.
4. Declare package permissions in metadata.json, run tests.py, and register the package with register_tool only after the tests pass.
5. Use the newly registered package for the user task and keep concrete selectors, commands, app names, and sites inside the package implementation or documentation, not in this kernel prompt.

YOU MUST NEVER SAY "I don't have internet access" OR "I can't do that" OR "I don't have that capability." You discover, prototype, test, package, and register the needed capability through execute_python.

BEHAVIORAL RULES:
1. DO NOT call tools for conversational exchanges. Greetings, questions, discussions do not require tool calls. A user saying "hi" gets a brief greeting. Nothing else.
2. You are a Real OS AI with host access mediated by the tool policy. The UI features a Memory sidebar on the RIGHT.
3. Tool-call contract: output exactly `<tool_call>{{"name":"execute_python","arguments":{{"code":"..."}}}}</tool_call>`. Do not use markdown fences for tool calls.
4. You are direct and strategic. No corporate fluff. No emotional validation. You report facts and actions.
5. You can modify your own nucleus code (read/write files in the nucleus directory) when approved by policy.
6. For self-modifying repository work, inspect `git status` before edits and again after edits so the user can see the checkpoint state. Use the reusable `nucleus/git_harness.py` helpers through execute_python when useful, or register thin wrappers only when needed.
7. You do not ask for confirmation more than once per action. If the user asked for something, do it.
8. After a tool result is returned, you MUST respond to it: summarize findings or confirm execution.
9. For PDFs, the pymupdf library is pre-installed.

CURRENT REGISTERED TOOLS:
{tool_registry}

PERMANENT FACTS ABOUT KHEIR AND KORA:
{facts}

RECALLED RELEVANT MEMORIES:
{recalled_memories}

WORKSPACE INDEX CONTEXT:
{workspace_index_context}

KORA KNOWLEDGE BASE:
{kora_context}

ACTIVE RULES:
{rules_context}

"""


class ContextFile(BaseModel):
    type: str
    name: str
    content: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str = "openai-fast"
    provider: str = "pollinations"
    context_files: list[ContextFile] = Field(default_factory=list)
    auto_approve: bool = False
    task_id: str | None = None
    max_task_steps: int = 12


class GoalCreateRequest(BaseModel):
    title: str
    description: str = ""
    status: str = "active"


class RecurrenceRuleRequest(BaseModel):
    frequency: str = "once"
    interval: int = 1
    start_at: str | None = None
    end_at: str | None = None


class BackgroundTaskCreateRequest(BaseModel):
    title: str
    prompt: str
    goal_id: str | None = None
    project_id: str | None = None
    due_at: str | None = None
    recurrence_rule: RecurrenceRuleRequest | None = None
    model: str = "openai-fast"
    provider: str = "pollinations"
    auto_approve: bool = True
    max_task_steps: int = 12


class RunDueBackgroundTasksRequest(BaseModel):
    now: str | None = None
    limit: int = 10


class ApprovalDecision(BaseModel):
    request_id: str


class SettingsUpdate(BaseModel):
    default_provider: str = "auto"
    default_model: str = "openai-fast"
    provider_priority: list[str] = Field(default_factory=lambda: ["pollinations"])
    provider_keys: dict[str, str] = Field(default_factory=dict)
    provider_api_keys: dict[str, list[str]] = Field(default_factory=dict)
    runtime_settings: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    id: str
    label: str
    type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    models_path: str = ""
    runtime_settings: dict[str, Any] = Field(default_factory=dict)


class LocalModelScanRequest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    max_results: int = 200


class WorkspaceIndexScanRequest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    task_id: str | None = None
    max_files: int = 500


class WorkspaceIndexEntry(BaseModel):
    path: str
    kind: str
    size: int
    mtime: float
    hash: str | None = None
    summary: str = ""
    last_seen_task_id: str | None = None


class PendingApproval:
    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_summary: str = "Approval required before tool execution.",
        policy_reasons: list[str] | None = None,
        sandbox_tier: str = "read_only",
        affected_paths: list[str] | None = None,
        network_risk: bool = False,
        process_risk: bool = False,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_summary = risk_summary
        self.policy_reasons = policy_reasons or []
        self.sandbox_tier = sandbox_tier
        self.affected_paths = affected_paths or []
        self.network_risk = network_risk
        self.process_risk = process_risk
        self.event = asyncio.Event()
        self.approved: bool | None = None
        self.approval_id = str(uuid.uuid4())

    def model_dump(self) -> dict[str, Any]:
        return {
            "request_id": self.approval_id,
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "risk_summary": self.risk_summary,
            "policy_reasons": self.policy_reasons,
            "sandbox_tier": self.sandbox_tier,
            "affected_paths": self.affected_paths,
            "network_risk": self.network_risk,
            "process_risk": self.process_risk,
        }


class KernelBoundary:
    """Private kernel boundary for services that should not become user-facing tools."""

    SAFETY_CALLERS = {"policy", "eval"}

    async def execute_python(self, code: str, *, policy_approved: bool = False) -> dict[str, Any]:
        return await execute_python(code, policy_approved=policy_approved)

    def register_tool(self, **kwargs: Any) -> dict[str, Any]:
        return register_tool(**kwargs)

    def read_memory(self, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        with connect_db() as conn:
            if query:
                rows = conn.execute(
                    "SELECT id, created_at, keywords, summary, session_id FROM episodic_memory "
                    "WHERE keywords LIKE ? OR summary LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", max(1, min(limit, 100))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, created_at, keywords, summary, session_id FROM episodic_memory ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                ).fetchall()
        return [dict(row) for row in rows]

    def write_memory(self, *, session_id: str, summary: str, keywords: str = "") -> str:
        memory_id = str(uuid.uuid4())
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO episodic_memory (id, created_at, keywords, summary, session_id) VALUES (?, ?, ?, ?, ?)",
                (memory_id, now_iso(), keywords, summary, session_id),
            )
            conn.commit()
        return memory_id

    def enforce_python_policy(self, code: str) -> dict[str, Any]:
        return summarize_python_execution_policy(code)

    def sandbox_environment(self, sandbox_tier: str) -> dict[str, str]:
        return build_python_execution_env(sandbox_tier)

    def load_task_state(self, task_id: str) -> TaskState | None:
        return load_task_state(DATA_DIR, task_id)

    def save_task_state(self, task_state: TaskState) -> None:
        save_task_state(task_state, DATA_DIR)

    def load_capability_registry(self) -> dict[str, Any]:
        return load_registry()

    async def dispatch_capability(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        policy_approved: bool = False,
    ) -> dict[str, Any]:
        if name == "execute_python":
            return await self.execute_python(str(arguments.get("code", "")), policy_approved=policy_approved)
        if name == "register_tool":
            return self.register_tool(
                name=str(arguments.get("name", "")),
                description=str(arguments.get("description", "")),
                parameters_schema=arguments.get("parameters_schema", {}),
                filepath=str(arguments.get("filepath", "")),
                requires_approval=bool(arguments.get("requires_approval", False)),
                version=str(arguments.get("version", "1.0.0")),
                source_task_id=arguments.get("source_task_id"),
                test_command=arguments.get("test_command"),
                test_plan=arguments.get("test_plan"),
                sample_arguments=arguments.get("sample_arguments"),
                supersedes=arguments.get("supersedes"),
            )
        if name == "rollback_latest_checkpoint":
            return {"error": "rollback_latest_checkpoint is a private kernel safety action, not a user-facing tool."}
        tool = find_tool(name)
        if tool is None:
            return {"error": f"Tool {name} not found."}
        if tool.get("builtin"):
            return {"error": f"Builtin tool {name} is not implemented in dispatch loop."}
        result = run_registered_tool(tool, arguments)
        update_registered_tool_status(name, str(tool.get("version", "")), increment_use_count=True)
        return result

    def rollback_latest_checkpoint(self, *, caller: str) -> dict[str, Any]:
        if caller not in self.SAFETY_CALLERS:
            return {"error": "rollback_latest_checkpoint is restricted to the policy/eval layer."}
        return checkpoints.restore_latest_checkpoint(data_dir=DATA_DIR)


KERNEL = KernelBoundary()


class ModelAdapter:
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str, tool_choice: str | None = None) -> AsyncIterator[str]:
        raise NotImplementedError


class ProviderAdapter(ModelAdapter):
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str, tool_choice: str | None = None, api_key: str | None = None) -> AsyncIterator[str]:
        provider_config = dict(get_provider(provider))
        if api_key is not None:
            provider_config["_override_key"] = api_key
        provider_type = provider_config.get("type", "openai_compatible")
        if provider_type == "ollama":
            async for token in stream_ollama(provider_config, messages, model):
                yield token
            return
        if provider_type == "gguf":
            async for token in stream_gguf(provider_config, messages, model):
                yield token
            return
        if provider_type == "google":
            async for token in stream_google(provider_config, messages, model):
                yield token
            return
        async for token in stream_openai_compatible(provider_config, messages, model, tool_choice=tool_choice):
            yield token


_provider_key_indices: dict[str, int] = {}
import random, asyncio


def _backoff(attempt: int) -> float:
    return min(0.5 * (2 ** attempt) + random.uniform(0, 0.5), 8.0)


class ProviderRouter(ModelAdapter):
    """Routes requests across providers. If provider='auto', tries priority chain.
    If a specific model is requested, prioritizes providers that serve it."""
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str, tool_choice: str | None = None) -> AsyncIterator[str]:
        adapter = ProviderAdapter()
        settings = load_settings()
        all_priority = settings.get("provider_priority", [provider]) if provider == "auto" else [provider]
        priority = all_priority[:]
        if provider == "auto":
            model_providers = [p for p in all_priority
                               if model in (settings.get("providers", {}).get(p, {}).get("models") or MODEL_OPTIONS.get(p, []))]
            if model_providers:
                priority = model_providers + [p for p in all_priority if p not in model_providers]
        tried: set[str] = set()
        for pid in priority:
            if pid in tried:
                continue
            tried.add(pid)
            cfg = settings.get("providers", {}).get(pid)
            if not cfg:
                continue
            all_keys: list[str] = list(cfg.get("api_keys") or [])
            env_key = get_provider_api_key_from_config(cfg)
            if env_key and env_key not in all_keys:
                all_keys.append(env_key)
            if not all_keys:
                all_keys = [""]
            start_idx = _provider_key_indices.get(pid, 0) % len(all_keys)
            for offset in range(len(all_keys)):
                ki = (start_idx + offset) % len(all_keys)
                _provider_key_indices[pid] = (ki + 1) % len(all_keys)
                this_key = all_keys[ki] or None
                for attempt in range(3):
                    try:
                        async for token in adapter.complete(messages, model, pid, tool_choice=tool_choice, api_key=this_key):
                            yield token
                        return
                    except RuntimeError as exc:
                        msg = str(exc)
                        if "402" in msg:
                            if offset < len(all_keys) - 1:
                                await asyncio.sleep(0.3)
                                continue
                            break
                        if "429" in msg and attempt < 2:
                            await asyncio.sleep(_backoff(attempt))
                            continue
                        if any(code in msg for code in ("500", "502", "503")) and attempt < 2:
                            await asyncio.sleep(_backoff(attempt))
                            continue
                        if pid != priority[-1]:
                            await asyncio.sleep(0.5)
                            break
                        raise
        raise RuntimeError("All providers exhausted.")


def registry_to_openai_tools() -> list[dict[str, Any]]:
    registry = load_registry()
    tools: list[dict[str, Any]] = []
    for tool in registry.get("tools", []):
        name = tool.get("name")
        desc = tool.get("description", "")
        params = tool.get("parameters", {"type": "object", "properties": {}})
        if name:
            tools.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return tools


async def stream_openai_compatible(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    tool_choice: str | None = None,
) -> AsyncIterator[str]:
    base_url = provider_config.get("base_url") or POLLINATIONS_BASE_URL
    api_key = provider_config.get("_override_key") or get_provider_api_key_from_config(provider_config)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    mapped_messages = []
    for i, m in enumerate(messages):
        c = m.get("content", "")
        if m["role"] == "tool":
            mapped_messages.append({"role": "user", "content": f"[TOOL RESULT — respond to this with a brief summary]: {c}"})
        else:
            if not c:
                print(f"WARNING: Empty content at messages.{i} (role={m['role']}), skipping")
                continue
            mapped_messages.append(m)
    if not mapped_messages:
        raise RuntimeError("All messages had empty content — nothing to send to provider.")

    payload: dict[str, Any] = {"model": model, "messages": mapped_messages, "stream": True}
    if tool_choice:
        payload["tools"] = registry_to_openai_tools()
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                error_text = response.text
                detail = f"Provider returned HTTP {response.status_code}"
                try:
                    err_body = json.loads(error_text)
                    err_obj = err_body.get("error", err_body)
                    msg = err_obj.get("message", error_text)
                    detail = f"{msg} (HTTP {response.status_code})"
                except (json.JSONDecodeError, AttributeError):
                    if error_text:
                        detail = f"{error_text[:500]} (HTTP {response.status_code})"
                raise RuntimeError(detail)

            tc_buffers: dict[int, dict[str, Any]] = {}
            text_buffer: list[str] = []
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    text_buffer.append(delta["content"])
                    yield delta["content"]
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    if idx not in tc_buffers:
                        tc_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                    buf = tc_buffers[idx]
                    if tc.get("id"):
                        buf["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        buf["name"] = tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        buf["arguments"] += tc["function"]["arguments"]

            if tc_buffers and not text_buffer:
                for buf in tc_buffers.values():
                    if buf["name"] == "execute_python":
                        try:
                            args = json.loads(buf["arguments"]) if buf["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {"code": buf["arguments"]}
                        yield f"<tool_call>{json.dumps({'name': 'execute_python', 'arguments': args})}</tool_call>"


def normalize_runtime_settings(raw_settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = dict(RUNTIME_SETTING_DEFAULTS)
    if not isinstance(raw_settings, dict):
        return settings
    for key in RUNTIME_INT_SETTINGS:
        value = raw_settings.get(key)
        if value in (None, ""):
            continue
        try:
            settings[key] = int(value)
        except (TypeError, ValueError):
            continue
    for key in RUNTIME_BOOL_SETTINGS:
        value = raw_settings.get(key)
        if isinstance(value, str):
            settings[key] = value.lower() in {"1", "true", "yes", "on"}
        elif value is not None:
            settings[key] = bool(value)
    return settings


def llama_init_kwargs(runtime_settings: dict[str, Any]) -> dict[str, Any]:
    kwargs = {key: runtime_settings[key] for key in RUNTIME_SETTINGS if key != "flash_attn"}
    kwargs["verbose"] = False
    return kwargs


def ollama_options(runtime_settings: dict[str, Any]) -> dict[str, Any]:
    options = {
        "num_thread": runtime_settings["n_threads"],
        "num_ctx": runtime_settings["n_ctx"],
        "num_batch": runtime_settings["n_batch"],
        "num_gpu": runtime_settings["n_gpu_layers"],
        "use_mmap": runtime_settings["use_mmap"],
        "use_mlock": runtime_settings["use_mlock"],
    }
    return {key: value for key, value in options.items() if value is not None}


async def stream_ollama(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
) -> AsyncIterator[str]:
    base_url = (provider_config.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    
    # Map "tool" role to "user" for maximum compatibility
    mapped_messages = []
    for m in messages:
        if m["role"] == "tool":
            mapped_messages.append({"role": "user", "content": f"[TOOL RESULT — respond to this with a brief summary]: {m['content']}"})
        else:
            mapped_messages.append(m)

    runtime_settings = normalize_runtime_settings(provider_config.get("runtime_settings"))
    payload = {"model": model, "messages": mapped_messages, "stream": True, "options": ollama_options(runtime_settings)}
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{base_url}/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (chunk.get("message") or {}).get("content")
                if content:
                    yield content
                if chunk.get("done"):
                    break


async def stream_gguf(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model_path: str,
) -> AsyncIterator[str]:
    path = Path(model_path).expanduser()
    if not path.exists() or path.suffix.lower() != ".gguf":
        raise RuntimeError("Selected GGUF model path does not exist.")
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise RuntimeError("Direct GGUF inference requires llama-cpp-python. Install it or run the model through Ollama or llama.cpp server.") from exc

    runtime_settings = normalize_runtime_settings(provider_config.get("runtime_settings"))
    init_kwargs = llama_init_kwargs(runtime_settings)
    try:
        llama_params = inspect.signature(Llama.__init__).parameters
    except (TypeError, ValueError):
        llama_params = {}
    if runtime_settings.get("flash_attn") and "flash_attn" in llama_params:
        init_kwargs["flash_attn"] = True

    cache_key = json.dumps(
        {"model_path": str(path.resolve()), "runtime_settings": init_kwargs},
        sort_keys=True,
    )
    if cache_key not in LLAMA_CACHE:
        LLAMA_CACHE[cache_key] = Llama(model_path=str(path.resolve()), **init_kwargs)
    llm = LLAMA_CACHE[cache_key]
    stream = llm.create_chat_completion(messages=messages, stream=True)
    for chunk in stream:
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content
        await asyncio.sleep(0)


async def stream_google(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
) -> AsyncIterator[str]:
    base_url = provider_config.get("base_url", "").rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
    api_key = provider_config.get("_override_key") or get_provider_api_key_from_config(provider_config)
    if not api_key:
        raise RuntimeError("Google API key required.")
    headers = {"Content-Type": "application/json", "X-goog-api-key": api_key}
    mapped = []
    for m in messages:
        role = "user" if m["role"] in ("tool", "system") else m["role"]
        mapped.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    payload = {"contents": mapped, "safety_settings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}]}
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", f"{base_url}/models/{model}:generateContent?alt=sse", headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise RuntimeError(f"Google API returned HTTP {resp.status_code}: {resp.text[:500]}")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
                        if part.get("text"):
                            yield part["text"]


async def discover_google_models(provider_config: dict[str, Any]) -> list[str]:
    base_url = provider_config.get("base_url", "").rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
    api_key = provider_config.get("_override_key") or get_provider_api_key_from_config(provider_config)
    if not api_key:
        return []
    headers = {"X-goog-api-key": api_key}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url}/models", headers=headers)
        if response.status_code != 200:
            return []
        data = response.json()
        models = data.get("models", [])
        return sorted(set(str(m["name"]).split("/")[-1] for m in models if "name" in m))


async def discover_openai_compatible_models(provider_config: dict[str, Any]) -> list[str]:
    base_url = provider_config.get("base_url", "").rstrip("/")
    if not base_url:
        return []
    api_key = get_provider_api_key_from_config(provider_config)
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url}/models", headers=headers)
        response.raise_for_status()
    data = response.json()
    models = data.get("data", [])
    discovered: list[str] = []
    for item in models:
        if isinstance(item, dict) and item.get("id"):
            discovered.append(str(item["id"]))
        elif isinstance(item, str):
            discovered.append(item)
    return sorted(set(discovered))


async def discover_ollama_models(provider_config: dict[str, Any]) -> list[str]:
    base_url = (provider_config.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{base_url}/api/tags")
        response.raise_for_status()
    data = response.json()
    return sorted(str(item["name"]) for item in data.get("models", []) if item.get("name"))


def discover_gguf_models(provider_config: dict[str, Any]) -> list[str]:
    models_path = provider_config.get("models_path") or str(BASE_DIR / "models")
    path = Path(models_path).expanduser()
    if not path.exists():
        return []
    return sorted(str(item) for item in path.glob("**/*.gguf") if item.is_file())


def likely_model_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    candidates = [
        home / ".cache" / "huggingface" / "hub",
        home / ".cache" / "lm-studio" / "models",
        home / ".lmstudio" / "models",
        home / ".ollama" / "models",
        home / "models",
        Path("C:/models"),
        Path("C:/AI"),
        Path("C:/Users/Public/models"),
        BASE_DIR / "models",
    ]
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and resolved not in roots:
            roots.append(resolved)
    return roots


def scan_local_model_files(roots: list[str] | None = None, max_results: int = 200) -> list[dict[str, Any]]:
    scan_roots = [Path(root).expanduser() for root in roots or [] if root.strip()]
    if not scan_roots:
        scan_roots = likely_model_roots()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in scan_roots:
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if len(results) >= max_results:
                return results
            if not item.is_file() or item.suffix.lower() not in MODEL_FILE_EXTENSIONS:
                continue
            try:
                resolved = str(item.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = item.stat()
            results.append(
                {
                    "name": item.name,
                    "path": resolved,
                    "type": item.suffix.lower().lstrip("."),
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                }
            )
    return sorted(results, key=lambda model: (model["type"], model["name"].lower()))


async def discover_models_for_provider(provider_id: str) -> list[str]:
    provider_config = get_provider(provider_id)
    provider_type = provider_config.get("type", "openai_compatible")
    if provider_type == "ollama":
        return await discover_ollama_models(provider_config)
    if provider_type == "gguf":
        return discover_gguf_models(provider_config)
    if provider_type == "google":
        return await discover_google_models(provider_config)
    return await discover_openai_compatible_models(provider_config)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_layout() -> None:
    for path in (DATA_DIR, ARCHIVE_DIR, CHECKPOINTS_DIR, DATA_DIR / "tasks", DATA_DIR / "subagents", TOOLS_DIR, KORA_DIR):
        path.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text(json.dumps(DEFAULT_REGISTRY, indent=2), encoding="utf-8")
    if not FACTS_PATH.exists():
        FACTS_PATH.write_text(json.dumps(DEFAULT_FACTS, indent=2), encoding="utf-8")
    if not SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")


def parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if not key:
        return None
    return key, value


def load_env_files() -> None:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            os.environ.setdefault(key, value)


def write_env_value(key: str, value: str, path: Path | None = None) -> None:
    target = path or ENV_PATHS[0]
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    output: list[str] = []
    found = False
    for line in lines:
        parsed = parse_env_line(line)
        if parsed is not None and parsed[0] == key:
            output.append(f"{key}={value}")
            found = True
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    target.write_text("\n".join(output).strip() + "\n", encoding="utf-8")
    os.environ[key] = value


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodic_memory (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                keywords TEXT NOT NULL,
                summary TEXT NOT NULL,
                session_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_summary (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                project_tags TEXT NOT NULL DEFAULT '[]',
                people_entities TEXT NOT NULL DEFAULT '[]',
                dates TEXT NOT NULL DEFAULT '[]',
                durable_decisions TEXT NOT NULL DEFAULT '[]',
                follow_up_tasks TEXT NOT NULL DEFAULT '[]',
                source_archive TEXT NOT NULL,
                compacted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_event (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                source_archive TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_fact (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.6,
                source_archive TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_timeline (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                event_date TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                entities TEXT NOT NULL DEFAULT '[]',
                source_archive TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_archive_manifest (
                session_id TEXT PRIMARY KEY,
                archive_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                compacted_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_execution (
                id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                result TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                approved INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluation_decision (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                rationale TEXT NOT NULL,
                raw_response TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_keywords ON episodic_memory(keywords)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_index (
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                hash TEXT,
                summary TEXT NOT NULL DEFAULT '',
                last_seen_task_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_index_kind ON workspace_index(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_index_task ON workspace_index(last_seen_task_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goal (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY,
                goal_id TEXT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(goal_id) REFERENCES goal(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recurrence_rule (
                id TEXT PRIMARY KEY,
                frequency TEXT NOT NULL,
                interval INTEGER NOT NULL DEFAULT 1,
                start_at TEXT NOT NULL,
                end_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS background_task (
                id TEXT PRIMARY KEY,
                goal_id TEXT,
                project_id TEXT,
                recurrence_rule_id TEXT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                due_at TEXT NOT NULL,
                last_run_at TEXT,
                completed_at TEXT,
                result_summary TEXT NOT NULL DEFAULT '',
                task_state_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(goal_id) REFERENCES goal(id),
                FOREIGN KEY(project_id) REFERENCES project(id),
                FOREIGN KEY(recurrence_rule_id) REFERENCES recurrence_rule(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_background_task_due ON background_task(status, due_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_background_task_goal ON background_task(goal_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_link (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                background_task_id TEXT,
                goal_id TEXT,
                project_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES episodic_memory(id),
                FOREIGN KEY(background_task_id) REFERENCES background_task(id),
                FOREIGN KEY(goal_id) REFERENCES goal(id),
                FOREIGN KEY(project_id) REFERENCES project(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_execution_audit (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                permissions_used TEXT NOT NULL,
                files_touched TEXT NOT NULL,
                network_flag INTEGER NOT NULL,
                process_flag INTEGER NOT NULL,
                approval_id TEXT,
                result_status TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_execution_audit_tool ON tool_execution_audit(tool_name, tool_version)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_compaction (
                session_id TEXT PRIMARY KEY,
                archive_path TEXT NOT NULL,
                original_size INTEGER NOT NULL,
                compressed_size INTEGER NOT NULL,
                summary_id TEXT NOT NULL,
                summary_path TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                compacted_at TEXT NOT NULL,
                compression TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_compaction_last_accessed ON memory_compaction(last_accessed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_summary_session ON memory_summary(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_event_occurred ON memory_event(occurred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_fact_kind_value ON memory_fact(kind, value)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_timeline_date ON memory_timeline(event_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_archive_manifest_status ON memory_archive_manifest(status)")
        conn.commit()


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_registry() -> dict[str, Any]:
    registry = load_json(REGISTRY_PATH, DEFAULT_REGISTRY)
    if "tools" not in registry or not isinstance(registry["tools"], list):
        raise HTTPException(status_code=500, detail="registry.json is invalid")
    normalized_tools = [normalize_registry_entry(tool) for tool in registry["tools"] if isinstance(tool, dict)]
    existing_names = {tool.get("name") for tool in normalized_tools}
    missing_builtins = [
        normalize_registry_entry(tool) for tool in DEFAULT_REGISTRY["tools"]
        if tool.get("builtin") and tool.get("name") not in existing_names
    ]
    registry["tools"] = normalized_tools + missing_builtins
    return registry


def load_settings() -> dict[str, Any]:
    settings = load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged.update({key: value for key, value in settings.items() if key != "providers"})
    providers = merged["providers"]
    providers.update(settings.get("providers", {}))
    merged["providers"] = providers
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    save_json(SETTINGS_PATH, settings)


def build_unified_models(settings: dict[str, Any]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for pid, pcfg in settings.get("providers", {}).items():
        models = pcfg.get("models") or MODEL_OPTIONS.get(pid, [])
        for m in models:
            if m not in seen:
                tiers = [t for t, ms in MODEL_TIERS.items() if m in ms]
                seen[m] = {"name": m, "providers": [], "tiers": tiers}
            seen[m]["providers"].append(pid)
    return sorted(seen.values(), key=lambda x: x["name"])


def provider_status() -> dict[str, Any]:
    settings = load_settings()
    providers: dict[str, Any] = {}
    for provider_id, provider in settings["providers"].items():
        key_name = provider.get("api_key_env", "")
        configured = bool(os.getenv(key_name)) or bool(provider.get("api_keys"))
        providers[provider_id] = {
            "label": provider.get("label", provider_id),
            "type": provider.get("type", "openai_compatible"),
            "base_url": provider.get("base_url", ""),
            "api_key_env": key_name,
            "configured": configured,
            "api_key_count": len(provider.get("api_keys") or []),
            "models": provider.get("models") or MODEL_OPTIONS.get(provider_id, []),
            "models_path": provider.get("models_path", ""),
            "runtime_settings": normalize_runtime_settings(provider.get("runtime_settings")),
        }
    return {
        "default_provider": settings.get("default_provider", "auto"),
        "default_model": settings.get("default_model", "openai-fast"),
        "provider_priority": settings.get("provider_priority", ["pollinations"]),
        "providers": providers,
        "unified_models": build_unified_models(settings),
    }


def get_provider(provider_id: str) -> dict[str, Any]:
    provider = load_settings().get("providers", {}).get(provider_id)
    if not provider:
        raise RuntimeError(f"Provider is not configured: {provider_id}")
    return provider


def get_provider_api_key(provider_id: str) -> str | None:
    provider = load_settings().get("providers", {}).get(provider_id)
    if not provider:
        return None
    key_name = provider.get("api_key_env")
    return os.getenv(key_name) if key_name else None


def get_provider_api_key_from_config(provider: dict[str, Any]) -> str | None:
    key_name = provider.get("api_key_env")
    return os.getenv(key_name) if key_name else None


def normalize_provider_id(provider_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", provider_id.strip().lower()).strip("_")
    if not cleaned:
        raise ValueError("Provider id is required.")
    return cleaned[:48]


def upsert_provider(update: ProviderUpdate) -> dict[str, Any]:
    provider_id = normalize_provider_id(update.id)
    if update.type not in {"openai_compatible", "ollama", "gguf", "google"}:
        raise ValueError("Provider type must be openai_compatible, ollama, gguf, or google.")
    settings = load_settings()
    api_key_env = f"{provider_id.upper()}_API_KEY"
    existing = settings["providers"].get(provider_id, {})
    provider = {
        "label": update.label.strip() or provider_id,
        "type": update.type,
        "base_url": update.base_url.strip(),
        "api_key_env": existing.get("api_key_env", api_key_env),
        "models": [model.strip() for model in update.models if model.strip()],
        "models_path": update.models_path.strip(),
        "runtime_settings": normalize_runtime_settings(update.runtime_settings or existing.get("runtime_settings")),
    }
    if provider["type"] == "ollama" and not provider["base_url"]:
        provider["base_url"] = "http://127.0.0.1:11434"
    settings["providers"][provider_id] = provider
    if provider_id not in settings.get("default_provider", ""):
        settings.setdefault("default_provider", provider_id)
    if update.api_key:
        write_env_value(provider["api_key_env"], update.api_key)
    save_settings(settings)
    return provider_status()


def parse_version_key(version: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(version or "0"))
    return tuple(int(part) for part in parts) or (0,)


def find_tool(name: str) -> dict[str, Any] | None:
    matches = [
        tool for tool in load_registry()["tools"]
        if tool.get("name") == name and tool.get("trusted") is True and not tool.get("deprecated", False)
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda tool: (parse_version_key(tool.get("version")), str(tool.get("updated_at", ""))),
    )[-1]


def find_tool_version(name: str, version: str | None = None) -> dict[str, Any] | None:
    matches = [tool for tool in load_registry()["tools"] if tool.get("name") == name]
    if version is not None:
        for tool in matches:
            if str(tool.get("version", "")) == str(version):
                return tool
        return None
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda tool: (parse_version_key(tool.get("version")), str(tool.get("updated_at", ""))),
    )[-1]


def tool_repair_max_attempts() -> int:
    raw_value = os.getenv("OUROBOROS_TOOL_REPAIR_MAX_ATTEMPTS")
    if raw_value is None:
        raw_value = load_settings().get("tool_repair_max_attempts", DEFAULT_TOOL_REPAIR_MAX_ATTEMPTS)
    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return DEFAULT_TOOL_REPAIR_MAX_ATTEMPTS


def load_registry_tool_metadata(tool: dict[str, Any]) -> dict[str, Any]:
    metadata = tool.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def save_tool_metadata(tool_name: str, metadata: dict[str, Any]) -> None:
    registry = load_registry()
    updated = False
    for entry in registry["tools"]:
        if entry.get("name") == tool_name:
            entry["metadata"] = metadata
            updated = True
            break
    if updated:
        save_json(REGISTRY_PATH, registry)

def load_tool_metadata(source: dict[str, Any] | Path) -> dict[str, Any]:
    if isinstance(source, dict):
        metadata = source.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    metadata_path = source / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata.json is invalid JSON: {exc.msg}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata.json must contain a JSON object.")
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("metadata.json must include a non-empty string version.")
    deprecated = metadata.get("deprecated", False)
    if not isinstance(deprecated, bool):
        raise ValueError("metadata.json deprecated must be a boolean when present.")
    deprecation_reason = metadata.get("deprecation_reason", "")
    if deprecation_reason is not None and not isinstance(deprecation_reason, str):
        raise ValueError("metadata.json deprecation_reason must be a string when present.")
    return metadata

def append_tool_repair_attempt(tool: dict[str, Any], arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(load_registry_tool_metadata(tool))
    attempts = metadata.get("repair_attempts")
    if not isinstance(attempts, list):
        attempts = []
    failure = classify_registered_tool_failure(result, tool)
    attempts.append(
        {
            "timestamp": now_iso(),
            "arguments": arguments,
            "failure": failure,
            "result": result,
        }
    )
    metadata["repair_attempts"] = attempts
    metadata["last_repair_attempt_at"] = attempts[-1]["timestamp"]
    metadata["repair_attempt_count"] = len(attempts)
    save_tool_metadata(str(tool.get("name", "")), metadata)
    tool["metadata"] = metadata
    return attempts[-1]


def load_kora_context() -> str:
    parts: list[str] = []
    for path in sorted(KORA_DIR.glob("**/*")):
        if path.is_file() and path.suffix.lower() in {".txt", ".md", ".json"}:
            try:
                rel = path.relative_to(KORA_DIR)
                parts.append(f"### {rel}\n{path.read_text(encoding='utf-8')}")
            except UnicodeDecodeError:
                continue
    return "\n\n".join(parts) if parts else "No Kora knowledge files loaded."


def extract_keywords(text: str) -> list[str]:
    stopwords = {
        "the","a","an","is","was","and","or","to",
        "in","of","for","that","this","it","on","at",
        "with","from","by","as","be","been","have",
        "has","had","will","would","could","should"
    }
    words = text.lower().split()
    return list({
        w.strip(".,!?:;\"'()[]") 
        for w in words 
        if len(w) > 3 and w not in stopwords
    })[:20]



def parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def memory_json(values: Sequence[str]) -> str:
    seen: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in seen}:
            seen.append(cleaned)
    return json.dumps(seen[:20])


def archive_content_hash(archive: dict[str, Any]) -> str:
    stable = json.dumps(
        {
            "session_id": archive.get("session_id"),
            "summary": archive.get("summary", ""),
            "messages": archive.get("messages", []),
            "created_at": archive.get("created_at"),
            "updated_at": archive.get("updated_at"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def parse_memory_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def session_text(messages: list[dict[str, Any]], max_chars: int = 12000) -> str:
    lines = []
    for message in messages:
        role = str(message.get("role", "")).upper()
        content = str(message.get("content", "")).strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)[:max_chars]


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def compact_summary_text(archive: dict[str, Any], text: str) -> str:
    existing = str(archive.get("summary") or "").strip()
    if existing:
        return existing[:700]
    sentences = split_sentences(text)
    if not sentences:
        return f"Conversation session {archive.get('session_id', 'unknown')}"
    return " ".join(sentences[:4])[:700]


def extract_dates_from_text(text: str, fallback_date: str) -> list[str]:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    years = re.findall(r"\b(?:19|20)\d{2}\b", text)
    values = dates + [year for year in years if not any(date.startswith(year) for date in dates)]
    if fallback_date:
        values.append(fallback_date[:10])
        values.append(fallback_date[:4])
    return values


def extract_entities_from_text(text: str) -> list[str]:
    candidates = re.findall(r"\b(?:[A-Z][A-Za-z0-9&_.-]+(?:\s+|$)){1,4}", text)
    ignored = {"USER", "ASSISTANT", "SYSTEM", "JSON", "TODO"}
    entities: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip(" .,:;!?()[]{}\n\t")
        if len(cleaned) < 3 or cleaned.upper() in ignored:
            continue
        if cleaned.lower() not in {item.lower() for item in entities}:
            entities.append(cleaned)
    return entities[:20]


def extract_project_tags_from_text(text: str, entities: Sequence[str]) -> list[str]:
    tags = re.findall(r"#([A-Za-z][A-Za-z0-9_-]{2,40})", text)
    project_phrases = re.findall(r"\b(?:project|initiative|repo|app|tool)\s+([A-Z][A-Za-z0-9_-]+(?:\s+[A-Z][A-Za-z0-9_-]+){0,3})", text, flags=re.IGNORECASE)
    tags.extend(project_phrases)
    tags.extend(entity for entity in entities if any(word in entity.lower() for word in ("project", "lab", "kora", "ouroboros")))
    return tags[:12]


def extract_lines_matching(text: str, patterns: Sequence[str]) -> list[str]:
    found: list[str] = []
    for sentence in split_sentences(text):
        lower = sentence.lower()
        if any(pattern in lower for pattern in patterns):
            found.append(sentence[:240])
    return found[:12]


def compact_archive_payload(archive: dict[str, Any], source_archive: Path) -> dict[str, Any]:
    messages = archive.get("messages") if isinstance(archive.get("messages"), list) else []
    text = session_text(messages)
    combined = f"{archive.get('title', '')}\n{archive.get('summary', '')}\n{text}"
    created_at = str(archive.get("created_at") or archive.get("updated_at") or now_iso())
    event_date = (parse_memory_datetime(created_at) or datetime.now(timezone.utc)).date().isoformat()
    summary = compact_summary_text(archive, combined)
    entities = extract_entities_from_text(combined)
    tags = extract_project_tags_from_text(combined, entities)
    dates = extract_dates_from_text(combined, event_date)
    decisions = extract_lines_matching(combined, ("decided", "decision", "we will", "we chose", "chose to", "selected", "agreed"))
    tasks = extract_lines_matching(combined, ("todo", "follow up", "follow-up", "next step", "task:", "action item"))
    return {
        "session_id": str(archive.get("session_id") or source_archive.stem),
        "created_at": created_at,
        "updated_at": str(archive.get("updated_at") or created_at),
        "summary": summary,
        "project_tags": tags,
        "people_entities": entities,
        "dates": dates,
        "durable_decisions": decisions,
        "follow_up_tasks": tasks,
        "source_archive": str(source_archive),
        "event_date": event_date,
        "title": str(archive.get("title") or derive_session_title(messages, str(archive.get("session_id") or source_archive.stem))),
        "message_count": len(messages),
        "content_hash": archive_content_hash(archive),
    }


def upsert_compacted_memory(compacted: dict[str, Any]) -> None:
    compacted_at = now_iso()
    session_id = compacted["session_id"]
    with connect_db() as conn:
        conn.execute("DELETE FROM memory_event WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM memory_fact WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM memory_timeline WHERE session_id = ?", (session_id,))
        conn.execute(
            """
            INSERT INTO memory_summary (
                id, session_id, created_at, updated_at, summary, project_tags, people_entities,
                dates, durable_decisions, follow_up_tasks, source_archive, compacted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                summary=excluded.summary,
                project_tags=excluded.project_tags,
                people_entities=excluded.people_entities,
                dates=excluded.dates,
                durable_decisions=excluded.durable_decisions,
                follow_up_tasks=excluded.follow_up_tasks,
                source_archive=excluded.source_archive,
                compacted_at=excluded.compacted_at
            """,
            (
                str(uuid.uuid4()), session_id, compacted["created_at"], compacted["updated_at"], compacted["summary"],
                memory_json(compacted["project_tags"]), memory_json(compacted["people_entities"]), memory_json(compacted["dates"]),
                memory_json(compacted["durable_decisions"]), memory_json(compacted["follow_up_tasks"]), compacted["source_archive"], compacted_at,
            ),
        )
        conn.execute(
            "INSERT INTO memory_event (id, session_id, occurred_at, title, description, source_archive, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, compacted["event_date"], compacted["title"], compacted["summary"], compacted["source_archive"], compacted_at),
        )
        conn.execute(
            "INSERT INTO memory_timeline (id, session_id, event_date, title, summary, tags, entities, source_archive, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, compacted["event_date"], compacted["title"], compacted["summary"], memory_json(compacted["project_tags"]), memory_json(compacted["people_entities"]), compacted["source_archive"], compacted_at),
        )
        for kind, values in (
            ("project_tag", compacted["project_tags"]),
            ("entity", compacted["people_entities"]),
            ("date", compacted["dates"]),
            ("durable_decision", compacted["durable_decisions"]),
            ("follow_up_task", compacted["follow_up_tasks"]),
        ):
            for value in values[:20]:
                conn.execute(
                    "INSERT INTO memory_fact (id, session_id, kind, value, confidence, source_archive, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), session_id, kind, value, 0.7, compacted["source_archive"], compacted_at),
                )
        conn.execute(
            """
            INSERT INTO memory_archive_manifest (session_id, archive_path, created_at, updated_at, compacted_at, message_count, status, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                archive_path=excluded.archive_path,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                compacted_at=excluded.compacted_at,
                message_count=excluded.message_count,
                status=excluded.status,
                content_hash=excluded.content_hash
            """,
            (session_id, compacted["source_archive"], compacted["created_at"], compacted["updated_at"], compacted_at, compacted["message_count"], "compacted", compacted["content_hash"]),
        )
        conn.commit()


def compact_memory_archives(cutoff_days: int = MEMORY_COMPACTION_DEFAULT_CUTOFF_DAYS, limit: int | None = None) -> dict[str, Any]:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc).timestamp() - (cutoff_days * 86400)
    compacted: list[dict[str, Any]] = []
    skipped: list[str] = []
    paths = sorted(ARCHIVE_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
    with connect_db() as conn:
        manifest_rows = conn.execute("SELECT session_id, content_hash, status FROM memory_archive_manifest").fetchall()
    manifest = {row["session_id"]: dict(row) for row in manifest_rows}
    for path in paths:
        if limit is not None and len(compacted) >= limit:
            break
        try:
            archive = normalize_session_archive(path)
            updated = parse_memory_datetime(archive.get("updated_at"))
            updated_ts = updated.timestamp() if updated else path.stat().st_mtime
            if updated_ts > cutoff:
                skipped.append(path.stem)
                continue
            content_hash = archive_content_hash(archive)
            existing = manifest.get(archive["session_id"])
            if existing and existing.get("content_hash") == content_hash and existing.get("status") == "compacted":
                skipped.append(path.stem)
                continue
            compacted_payload = compact_archive_payload(archive, path)
            upsert_compacted_memory(compacted_payload)
            compacted.append(compacted_payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            skipped.append(f"{path.stem}: {exc}")
    return {"status": "compacted", "compacted_count": len(compacted), "skipped_count": len(skipped), "compacted": compacted, "skipped": skipped}


def query_target_year(query: str, reference: datetime | None = None) -> int | None:
    reference = reference or datetime.now(timezone.utc)
    lower = query.lower()
    match = re.search(r"\b(\d{1,3})\s+years?\s+ago\b", lower)
    if match:
        return reference.year - int(match.group(1))
    match = re.search(r"\b(?:in|from|during)\s+((?:19|20)\d{2})\b", lower)
    if match:
        return int(match.group(1))
    match = re.search(r"\b((?:19|20)\d{2})\b", lower)
    if match:
        return int(match.group(1))
    return None


def score_memory_text(query_words: set[str], text: str, target_year: int | None = None, date_text: str = "") -> int:
    words = set(extract_keywords(text))
    score = len(query_words & words) * 3
    if target_year and (str(target_year) in text or str(target_year) in date_text):
        score += 25
    return score


def recall_memories(query: str, limit: int = MEMORY_RECALL_DEFAULT_LIMIT, include_raw: bool = True) -> list[dict[str, Any]]:
    query_words = set(extract_keywords(query))
    target_year = query_target_year(query)
    results: list[dict[str, Any]] = []
    with connect_db() as conn:
        timeline_rows = conn.execute("SELECT * FROM memory_timeline ORDER BY event_date DESC").fetchall()
        summary_rows = conn.execute("SELECT * FROM memory_summary ORDER BY updated_at DESC").fetchall()
        episodic_rows = conn.execute("SELECT summary, keywords, created_at, session_id FROM episodic_memory ORDER BY created_at DESC").fetchall()
    for row in timeline_rows:
        text = " ".join([row["title"], row["summary"], row["tags"], row["entities"]])
        score = score_memory_text(query_words, text, target_year, row["event_date"])
        if score > 0:
            results.append({"type": "timeline", "session_id": row["session_id"], "date": row["event_date"], "title": row["title"], "summary": row["summary"], "score": score, "source_archive": row["source_archive"]})
    for row in summary_rows:
        text = " ".join([row["summary"], row["project_tags"], row["people_entities"], row["dates"], row["durable_decisions"], row["follow_up_tasks"]])
        score = score_memory_text(query_words, text, target_year, row["dates"])
        if score > 0:
            results.append({"type": "summary", "session_id": row["session_id"], "date": row["created_at"][:10], "title": "Compacted memory", "summary": row["summary"], "project_tags": parse_json_list(row["project_tags"]), "people_entities": parse_json_list(row["people_entities"]), "durable_decisions": parse_json_list(row["durable_decisions"]), "follow_up_tasks": parse_json_list(row["follow_up_tasks"]), "score": score, "source_archive": row["source_archive"]})
    for row in episodic_rows:
        text = f"{row['summary']} {row['keywords']}"
        score = score_memory_text(query_words, text, target_year, row["created_at"])
        if score > 0:
            results.append({"type": "episodic", "session_id": row["session_id"], "date": row["created_at"][:10], "title": "Episodic memory", "summary": row["summary"], "score": score})
    if include_raw and len(results) < limit:
        for path in sorted(ARCHIVE_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                archive = normalize_session_archive(path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            text = f"{archive['title']} {archive['summary']} {session_text(archive['messages'], max_chars=4000)}"
            score = score_memory_text(query_words, text, target_year, archive["created_at"])
            if score > 0:
                results.append({"type": "raw_archive", "session_id": archive["session_id"], "date": archive["created_at"][:10], "title": archive["title"], "summary": archive["summary"] or compact_summary_text(archive, text), "score": score, "source_archive": str(path)})
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    type_priority = {"timeline": 0, "summary": 1, "episodic": 2, "raw_archive": 3}
    for result in sorted(results, key=lambda item: (-item["score"], type_priority.get(item["type"], 9))):
        key = (result["session_id"], result["type"])
        deduped.setdefault(key, result)
    return list(deduped.values())[:limit]


def retrieve_relevant(query: str, limit: int = 4, max_chars: int = MEMORY_PROMPT_BUDGET_CHARS) -> str:
    memories = recall_memories(query, limit=limit, include_raw=False)
    if not memories:
        return ""
    parts: list[str] = []
    total = 0
    for memory in memories:
        line = f"[{memory.get('date', '')}] {memory.get('title', memory['type'])}: {memory.get('summary', '')}"
        if memory.get("durable_decisions"):
            line += " Decisions: " + "; ".join(memory["durable_decisions"][:3])
        if memory.get("follow_up_tasks"):
            line += " Follow-ups: " + "; ".join(memory["follow_up_tasks"][:3])
        if total + len(line) > max_chars:
            remaining = max_chars - total
            if remaining > 80:
                parts.append(line[:remaining] + "... [memory context truncated]")
            break
        parts.append(line)
        total += len(line) + 2
    return "\n\n".join(parts)


def workspace_index_kind(path: Path, root: Path) -> str:
    base = root.parent if root.is_file() else root
    try:
        rel_parts = {part.lower() for part in path.relative_to(base).parts[:-1]}
    except ValueError:
        rel_parts = set()
    suffix = path.suffix.lower()
    if rel_parts & WORKSPACE_INDEX_GENERATED_DIRS:
        return "generated"
    if suffix in MODEL_FILE_EXTENSIONS:
        return "artifact"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".svg", ".pdf", ".zip"}:
        return "artifact"
    if suffix in {".md", ".txt", ".rst"}:
        return "doc"
    if suffix in WORKSPACE_INDEX_TEXT_EXTENSIONS:
        return "source"
    return "file"


def is_workspace_index_binary(path: Path, size: int) -> bool:
    if path.suffix.lower() not in WORKSPACE_INDEX_TEXT_EXTENSIONS and path.suffix.lower() not in {".rst"}:
        return True
    if size > WORKSPACE_INDEX_MAX_TEXT_BYTES:
        return True
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    if b"\0" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def workspace_index_hash(path: Path, size: int, binary_or_huge: bool) -> str | None:
    if binary_or_huge or size > WORKSPACE_INDEX_MAX_HASH_BYTES:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def fallback_file_summary(path: Path, kind: str, size: int, binary_or_huge: bool) -> str:
    if binary_or_huge:
        return f"{kind} file; content skipped because it is binary or too large ({size} bytes)."
    try:
        text = path.read_text(encoding="utf-8")[:4000]
    except (OSError, UnicodeDecodeError):
        return f"{kind} file; content could not be decoded."
    first_lines = [line.strip() for line in text.splitlines() if line.strip()][:3]
    if not first_lines:
        return f"Empty {kind} file."
    return f"{kind} file: " + " ".join(first_lines)[:220]


async def summarize_file_with_current_model(path: Path, kind: str, size: int, binary_or_huge: bool) -> str:
    if binary_or_huge:
        return fallback_file_summary(path, kind, size, binary_or_huge)
    try:
        content = path.read_text(encoding="utf-8")[:6000]
    except (OSError, UnicodeDecodeError):
        return fallback_file_summary(path, kind, size, True)
    prompt = f"""
Create a compact one-sentence summary (max 28 words) for this workspace file.
Mention the file's purpose and important symbols or topics. Do not add preamble.

Path: {path.name}
Kind: {kind}
Content:
{content}
"""
    try:
        summary = (await call_model_simple(prompt, model=load_settings().get("default_model", "openai-fast"))).strip()
    except Exception:
        summary = fallback_file_summary(path, kind, size, binary_or_huge)
    return " ".join(summary.split())[:300]


def workspace_index_entry_to_dict(entry: WorkspaceIndexEntry) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    return entry.dict()


async def scan_workspace_index(roots: list[str] | None = None, task_id: str | None = None, max_files: int = 500) -> dict[str, Any]:
    selected_roots = [Path(root).expanduser() for root in (roots or [ROOT_DIR])]
    indexed = 0
    skipped = 0
    entries: list[WorkspaceIndexEntry] = []
    with connect_db() as conn:
        for root in selected_roots:
            if not root.exists():
                skipped += 1
                continue
            root = root.resolve()
            candidates = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in candidates:
                if indexed >= max_files:
                    break
                if not path.is_file():
                    continue
                rel_parts = set(path.relative_to(root).parts[:-1]) if root in path.parents else set()
                if any(part in {".git", "node_modules", "target"} for part in rel_parts):
                    skipped += 1
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    skipped += 1
                    continue
                kind = workspace_index_kind(path, root)
                binary_or_huge = is_workspace_index_binary(path, stat.st_size)
                digest = workspace_index_hash(path, stat.st_size, binary_or_huge)
                summary = await summarize_file_with_current_model(path, kind, stat.st_size, binary_or_huge)
                entry = WorkspaceIndexEntry(
                    path=str(path),
                    kind=kind,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    hash=digest,
                    summary=summary,
                    last_seen_task_id=task_id,
                )
                conn.execute(
                    """
                    INSERT INTO workspace_index (path, kind, size, mtime, hash, summary, last_seen_task_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        kind=excluded.kind,
                        size=excluded.size,
                        mtime=excluded.mtime,
                        hash=excluded.hash,
                        summary=excluded.summary,
                        last_seen_task_id=excluded.last_seen_task_id,
                        updated_at=excluded.updated_at
                    """,
                    (entry.path, entry.kind, entry.size, entry.mtime, entry.hash, entry.summary, entry.last_seen_task_id, now_iso()),
                )
                entries.append(entry)
                indexed += 1
            if indexed >= max_files:
                break
        conn.commit()
    return {"indexed": indexed, "skipped": skipped, "entries": [workspace_index_entry_to_dict(entry) for entry in entries]}


def retrieve_workspace_index_context(query: str, limit: int = 8, max_chars: int = 1800) -> str:
    query_words = set(extract_keywords(query))
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT path, kind, size, summary, last_seen_task_id FROM workspace_index ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
    if not rows:
        return "No workspace index entries available."
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        haystack = f"{Path(row['path']).name} {row['kind']} {row['summary']}".lower()
        score = sum(1 for word in query_words if word in haystack)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [row for score, row in scored if score > 0][:limit] or [row for _, row in scored[:limit]]
    lines = []
    for row in selected:
        task = f" task={row['last_seen_task_id']}" if row["last_seen_task_id"] else ""
        lines.append(f"- {row['path']} [{row['kind']}, {row['size']} bytes{task}]: {row['summary']}")
    context = "\n".join(lines)
    if len(context) > max_chars:
        context = context[:max_chars] + "... [workspace index truncated]"
    return context


def discover_environment() -> str:
    import platform, shutil
    lines = []
    lines.append(f"OS: {platform.system()} {platform.release()}")
    lines.append(f"Python: {platform.python_version()}")
    lines.append(f"Host: {platform.node()}")
    lines.append(f"CWD: {os.getcwd()}")
    lines.append(f"Project root: {ROOT_DIR}")
    lines.append(f"Nucleus dir: {BASE_DIR}")
    env_keys = {k for k in os.environ if k.startswith(("OUROBOROS_", "POLLINATIONS_", "OPENAI_"))}
    if env_keys:
        for k in sorted(env_keys):
            lines.append(f"ENV {k}={'SET' if os.environ.get(k) else 'UNSET'}")
    docker_path = shutil.which("docker")
    lines.append(f"Docker: {'available at ' + docker_path if docker_path else 'not found'}")
    git_path = shutil.which("git")
    lines.append(f"Git: {'available at ' + git_path if git_path else 'not found'}")
    return "\n".join(lines)


def load_rules_context() -> str:
    parts: list[str] = []
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(RULES_DIR.glob("**/*.md")):
        rel = path.relative_to(RULES_DIR)
        content = path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"--- {rel} ---\n{content}")
    return "\n\n".join(parts) if parts else "No active rules."


def build_system_prompt(request: ChatRequest) -> str:
    facts = json.dumps(load_json(FACTS_PATH, DEFAULT_FACTS), indent=2)
    registry = json.dumps(load_registry(), indent=2)
    first_query = request.messages[0].content if request.messages else ""
    recalled_text = retrieve_relevant(first_query)
    if not recalled_text:
        recalled_text = "No relevant memories found."
    else:
        if len(recalled_text) > 4000:
            recalled_text = recalled_text[:4000] + "... [truncated]"
    
    context_parts = []
    total_chars = 0
    for item in request.context_files:
        content = item.content
        if len(content) > 8000:
            content = content[:8000] + "... [file truncated]"
        part = f"### {item.name}\n{content}"
        if total_chars + len(part) > 24000:
            context_parts.append(f"### {item.name}\n[OMITTED: Context too large]")
            break
        context_parts.append(part)
        total_chars += len(part)
    context_file_text = "\n\n".join(context_parts)
    kora_context = load_kora_context()
    if context_file_text:
        kora_context = f"{kora_context}\n\nUploaded context files:\n{context_file_text}"
        
    return SYSTEM_TEMPLATE.format(
        tool_registry=registry,
        facts=facts,
        recalled_memories=recalled_text,
        workspace_index_context=retrieve_workspace_index_context(first_query),
        kora_context=kora_context,
        rules_context=load_rules_context(),
    )



CAPABILITY_REFUSAL_PATTERNS = (
    "i don't have internet access",
    "i do not have internet access",
    "i have no internet access",
    "no internet access",
    "cannot access the internet",
    "can't access the internet",
    "unable to access the internet",
    "i don't have web access",
    "i do not have web access",
    "i can't browse",
    "i cannot browse",
    "can't browse the web",
    "cannot browse the web",
    "i don't have that capability",
    "i do not have that capability",
    "i cannot",
    "i can't",
    # French refusals
    "je n'ai pas la capacité",
    "je n'ai pas accès",
    "je ne peux pas",
    "je suis un modèle de langage",
    "en tant que modèle de langage",
    "je ne peux pas accéder",
    "je n'ai pas les capacités",
    "désolé, je ne peux pas",
    "désolé je ne peux pas",
    "je ne suis pas capable",
    "je ne peux pas exécuter",
    "je ne peux pas modifier",
)


def last_user_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def is_capability_refusal(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower())
    return any(pattern in normalized for pattern in CAPABILITY_REFUSAL_PATTERNS)


def build_self_evolution_retry_message(request: ChatRequest) -> str:
    user_request = last_user_message(request.messages).strip()
    if not user_request:
        user_request = "the user's request"
    return textwrap.dedent(
        f"""
        Your previous answer violated the discover-needed-capability path by claiming a missing capability.
        You are not allowed to stop at that refusal. Emit exactly one valid <tool_call> for execute_python that prototypes or exercises the smallest capability needed for this request.
        If the capability should persist, package it under nucleus/tools/ with tool.py, schema.json, README.md, tests.py, metadata.json with permissions, and evals.json; run tests.py; then register it with register_tool.
        Keep concrete selectors, commands, app names, and websites in the package, not in the kernel prompt.

        Original user request:
        {user_request}
        """
    ).strip()

def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


def extract_fenced_execute_python_code(info: str, body: str) -> str | None:
    info = info.strip()
    body = body.strip("\n")
    if info.startswith("execute_python"):
        tool_body = body
    else:
        lines = body.splitlines()
        if not lines or not lines[0].strip().startswith("execute_python"):
            return None
        tool_body = "\n".join(lines[1:])

    match = re.search(r"(?ms)^\s*code\s*:\s*(.*)\Z", tool_body)
    if not match:
        return None

    raw_code = match.group(1)
    stripped_code = raw_code.lstrip()
    if re.match(r"[|>][+-]?", stripped_code):
        lines = stripped_code.splitlines()
        return textwrap.dedent("\n".join(lines[1:])).strip("\n")
    if raw_code.startswith("\n"):
        return textwrap.dedent(raw_code).strip("\n")
    return raw_code.strip()


def _find_json_span(text: str, start: int = 0) -> tuple[int, int] | None:
    brace_start = text.find("{", start)
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (brace_start, i + 1)
    return None


def _try_parse_tool_json(text: str, json_start: int, json_end: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(text[json_start:json_end], strict=False)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and "name" in payload and "arguments" in payload:
        return payload
    return None


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen_starts: set[int] = set()

    # Match <tool_call> tags with optional </tool_call> closing
    for match in re.finditer(r"<tool_call>", text):
        tag_end = match.end()
        span = _find_json_span(text, tag_end)
        if span is None:
            continue
        json_start, json_end = span
        if json_start in seen_starts:
            continue
        seen_starts.add(json_start)
        payload = _try_parse_tool_json(text, json_start, json_end)
        if payload is not None:
            calls.append(payload)

    for match in re.finditer(r"```([^`\n]*)\n(.*?)```", text, re.DOTALL):
        code = extract_fenced_execute_python_code(match.group(1), match.group(2))
        if code is None:
            continue
        calls.append({"name": "execute_python", "arguments": {"code": code}})
    return calls


def strip_tool_calls(text: str) -> str:
    spans: list[tuple[int, int]] = []

    for match in re.finditer(r"<tool_call>", text):
        if any(s[0] <= match.start() < s[1] for s in spans):
            continue
        tag_end = match.end()
        span = _find_json_span(text, tag_end)
        if span is None:
            continue
        end = span[1]
        # Include optional </tool_call> after the JSON
        rest = text[end:]
        close_tag = rest.find("</tool_call>")
        if close_tag != -1 and close_tag <= 5:
            end += close_tag + len("</tool_call>")
        spans.append((match.start(), end))

    result = text
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + result[end:]

    def replace_fenced_tool(match: re.Match[str]) -> str:
        code = extract_fenced_execute_python_code(match.group(1), match.group(2))
        return "" if code is not None else match.group(0)

    return re.sub(r"```([^`\n]*)\n(.*?)```", replace_fenced_tool, result, flags=re.DOTALL)


def validate_tool_name(name: str) -> None:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", name):
        raise ValueError("Tool name must be a valid Python-style identifier under 64 characters.")


def resolve_tool_candidate(path_value: str) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    candidate = candidate.resolve()
    tools_root = TOOLS_DIR.resolve()
    if tools_root != candidate and tools_root not in candidate.parents:
        raise ValueError("Registered tools must live under nucleus/tools.")
    if not candidate.exists():
        raise ValueError(f"Tool path does not exist: {path_value}")
    return candidate


def resolve_tool_path(filepath: str) -> Path:
    candidate = resolve_tool_candidate(filepath)
    if candidate.is_dir():
        candidate = candidate / "tool.py"
    if candidate.suffix.lower() != ".py":
        raise ValueError("Registered tools must be Python files or skill package directories.")
    if not candidate.exists():
        raise ValueError(f"Tool file does not exist: {filepath}")
    return candidate


def load_package_metadata(package_dir: Path) -> dict[str, Any]:
    metadata_path = package_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata.json is invalid JSON: {exc.msg}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata.json must contain a JSON object.")
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("metadata.json must include a non-empty string version.")
    permissions = metadata.get("permissions")
    if not isinstance(permissions, dict) or not permissions:
        raise ValueError("metadata.json must include a non-empty permissions object.")
    if not all(isinstance(key, str) and key.strip() for key in permissions):
        raise ValueError("metadata.json permissions keys must be non-empty strings.")
    deprecated = metadata.get("deprecated", False)
    if not isinstance(deprecated, bool):
        raise ValueError("metadata.json deprecated must be a boolean when present.")
    deprecation_reason = metadata.get("deprecation_reason", "")
    if deprecation_reason is not None and not isinstance(deprecation_reason, str):
        raise ValueError("metadata.json deprecation_reason must be a string when present.")
    return metadata

def validate_json_schema(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise ValueError("schema.json must contain a JSON object.")
    try:
        import jsonschema
    except ImportError:
        valid_types = {"null", "boolean", "object", "array", "number", "string", "integer"}

        def walk(node: Any, location: str = "schema") -> None:
            if isinstance(node, dict):
                type_value = node.get("type")
                if isinstance(type_value, str) and type_value not in valid_types:
                    raise ValueError(f"Invalid JSON Schema type at {location}: {type_value}")
                if isinstance(type_value, list):
                    invalid = [item for item in type_value if item not in valid_types]
                    if invalid:
                        raise ValueError(f"Invalid JSON Schema type at {location}: {invalid[0]}")
                required = node.get("required")
                if required is not None and (not isinstance(required, list) or not all(isinstance(item, str) for item in required)):
                    raise ValueError(f"Invalid JSON Schema required list at {location}.")
                properties = node.get("properties")
                if properties is not None and not isinstance(properties, dict):
                    raise ValueError(f"Invalid JSON Schema properties at {location}.")
                for key, value in node.items():
                    walk(value, f"{location}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{location}[{index}]")

        walk(schema)
        return

    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(f"Invalid JSON Schema: {exc.message}") from exc


def load_tool_package(package_dir: Path) -> dict[str, Any]:
    required_files = ["tool.py", "schema.json", "README.md", "tests.py", "metadata.json", "evals.json"]
    missing = [name for name in required_files if not (package_dir / name).is_file()]
    if missing:
        raise ValueError("Skill package is missing required files: " + ", ".join(missing))

    try:
        schema = json.loads((package_dir / "schema.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"schema.json is invalid JSON: {exc.msg}") from exc
    validate_json_schema(schema)
    metadata = load_package_metadata(package_dir)
    try:
        evals = json.loads((package_dir / "evals.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"evals.json is invalid JSON: {exc.msg}") from exc
    if not isinstance(evals, (dict, list)):
        raise ValueError("evals.json must contain a JSON object or array.")
    validate_tool_permission_manifest(metadata)
    metadata = load_package_metadata(package_dir)

    completed = subprocess.run(
        [sys.executable, str(package_dir / "tests.py")],
        cwd=str(package_dir),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "Skill package tests.py failed before registration. "
            f"stdout={completed.stdout[-4000:]!r} stderr={completed.stderr[-4000:]!r}"
        )
    return {"schema": schema, "metadata": metadata, "evals": evals, "test_stdout": completed.stdout, "test_stderr": completed.stderr}


EVAL_PERMISSION_REASON_MAP = {
    "filesystem write or mutation": "filesystem_write",
    "network access": "network",
    "subprocess/process access": "subprocess",
    "home-directory access": "home_directory",
}


def normalize_permission(permission: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", permission.strip().lower()).strip("_")


def permissions_for_tool_code(tool: dict[str, Any]) -> list[str]:
    filepath = tool.get("filepath")
    if not filepath:
        return []
    try:
        code = resolve_tool_path(str(filepath)).read_text(encoding="utf-8")
    except OSError:
        return []
    policy = summarize_python_execution_policy(code)
    permissions: set[str] = set()
    for reason in policy.get("reasons", []):
        reason_text = str(reason)
        if reason_text.startswith("absolute path outside approved roots"):
            permissions.add("host_filesystem")
        else:
            mapped = EVAL_PERMISSION_REASON_MAP.get(reason_text)
            if mapped:
                permissions.add(mapped)
    return sorted(permissions)


def evals_path_for_tool(tool: dict[str, Any]) -> Path | None:
    package_dir = tool.get("package_dir") or (tool.get("filepath") if tool.get("package") else None)
    if not package_dir:
        return None
    candidate = resolve_tool_candidate(str(package_dir)) / "evals.json"
    return candidate if candidate.is_file() else None


def load_tool_evals(tool: dict[str, Any]) -> list[dict[str, Any]]:
    path = evals_path_for_tool(tool)
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"evals.json is invalid JSON: {exc.msg}") from exc
    cases = raw.get("evals", raw) if isinstance(raw, dict) else raw
    if not isinstance(cases, list):
        raise ValueError("evals.json must contain a list or an object with an evals list.")
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"evals.json case {index} must be an object.")
        arguments = case.get("input_arguments", case.get("arguments"))
        if not isinstance(arguments, dict):
            raise ValueError(f"evals.json case {index} input_arguments must be an object.")
        predicate = case.get("expected_output_predicate", case.get("predicate"))
        if not isinstance(predicate, dict):
            raise ValueError(f"evals.json case {index} expected_output_predicate must be an object.")
        timeout = case.get("timeout", REGISTERED_TOOL_TIMEOUT_SECONDS)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"evals.json case {index} timeout must be a positive number.")
        required_permissions = case.get("required_permissions", [])
        if not isinstance(required_permissions, list) or not all(isinstance(item, str) for item in required_permissions):
            raise ValueError(f"evals.json case {index} required_permissions must be a list of strings.")
        normalized.append(
            {
                "name": str(case.get("name", f"case_{index + 1}")),
                "input_arguments": arguments,
                "expected_output_predicate": predicate,
                "timeout": timeout,
                "required_permissions": [normalize_permission(item) for item in required_permissions],
            }
        )
    return normalized


def eval_declared_permissions(cases: list[dict[str, Any]]) -> list[str]:
    declared: set[str] = set()
    for case in cases:
        declared.update(case.get("required_permissions", []))
    return sorted(declared)


def evaluate_output_predicate(result: dict[str, Any], predicate: dict[str, Any]) -> tuple[bool, str | None]:
    if "exit_code" in predicate and result.get("exit_code") != predicate["exit_code"]:
        return False, f"exit_code expected {predicate['exit_code']!r} got {result.get('exit_code')!r}"
    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    if "stdout_contains" in predicate and str(predicate["stdout_contains"]) not in stdout:
        return False, f"stdout did not contain {predicate['stdout_contains']!r}"
    if "stdout_equals" in predicate and stdout.strip() != str(predicate["stdout_equals"]):
        return False, "stdout did not equal expected value"
    if "stderr_contains" in predicate and str(predicate["stderr_contains"]) not in stderr:
        return False, f"stderr did not contain {predicate['stderr_contains']!r}"
    if "json_equals" in predicate:
        try:
            actual = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return False, f"stdout was not JSON: {exc.msg}"
        if actual != predicate["json_equals"]:
            return False, f"JSON output expected {predicate['json_equals']!r} got {actual!r}"
    if "json_field_equals" in predicate:
        checks = predicate["json_field_equals"]
        if not isinstance(checks, dict):
            return False, "json_field_equals must be an object"
        try:
            actual = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return False, f"stdout was not JSON: {exc.msg}"
        for key, expected in checks.items():
            if not isinstance(actual, dict) or actual.get(key) != expected:
                return False, f"JSON field {key!r} expected {expected!r} got {actual.get(key) if isinstance(actual, dict) else None!r}"
    return True, None


def run_tool_evals(tool_name: str, version: str | None = None) -> dict[str, Any]:
    tool = find_tool_version(tool_name, version)
    if tool is None:
        return {"passed": False, "error": f"Tool {tool_name} not found."}
    if tool.get("builtin"):
        return {"passed": False, "error": f"Builtin tool {tool_name} cannot run package evals."}
    try:
        cases = load_tool_evals(tool)
    except ValueError as exc:
        update_registered_tool_status(tool_name, str(tool.get("version", "")), last_eval_status="failed", last_error=str(exc), trusted=False)
        return {"passed": False, "error": str(exc), "cases": []}
    if not cases:
        message = "No evals.json cases found for tool."
        update_registered_tool_status(tool_name, str(tool.get("version", "")), last_eval_status="failed", last_error=message, trusted=False)
        return {"passed": False, "error": message, "cases": []}

    declared_permissions = eval_declared_permissions(cases)
    used_permissions = permissions_for_tool_code(tool)
    undeclared = sorted(set(used_permissions) - set(declared_permissions))
    case_results: list[dict[str, Any]] = []
    passed = not undeclared
    for case in cases:
        result = run_registered_tool(tool, case["input_arguments"], timeout=case["timeout"])
        predicate_passed, error = evaluate_output_predicate(result, case["expected_output_predicate"])
        case_passed = result.get("exit_code") == 0 and not result.get("timed_out") and predicate_passed
        if not case_passed:
            passed = False
        case_results.append(
            {
                "name": case["name"],
                "passed": case_passed,
                "error": error or (result.get("stderr") or result.get("error")),
                "required_permissions": case["required_permissions"],
                "result": result,
            }
        )
    message = None if passed else ("Undeclared permission use: " + ", ".join(undeclared) if undeclared else "One or more eval cases failed.")
    update_registered_tool_status(
        tool_name,
        str(tool.get("version", "")),
        last_eval_status="passed" if passed else "failed",
        last_error=message,
        trusted=False,
    )
    return {
        "passed": passed,
        "tool": tool_name,
        "version": str(tool.get("version", "")),
        "cases": case_results,
        "declared_permissions": declared_permissions,
        "used_permissions": used_permissions,
        "undeclared_permissions": undeclared,
        "error": message,
    }


def run_package_tests_for_tool(tool: dict[str, Any]) -> dict[str, Any]:
    package_dir = tool.get("package_dir") or (tool.get("filepath") if tool.get("package") else None)
    if not package_dir:
        return {"passed": tool.get("last_test_status") == "passed", "skipped": True}
    try:
        package_path = resolve_tool_candidate(str(package_dir))
        completed = subprocess.run(
            [sys.executable, str(package_path / "tests.py")],
            cwd=str(package_path),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        return {"passed": False, "error": str(exc)}
    return {
        "passed": completed.returncode == 0,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
        "exit_code": completed.returncode,
    }


def promote_tool_trust(tool_name: str, version: str | None = None) -> dict[str, Any]:
    tool = find_tool_version(tool_name, version)
    if tool is None:
        return {"promoted": False, "error": f"Tool {tool_name} not found."}
    if tool.get("builtin"):
        return {"promoted": False, "error": f"Builtin tool {tool_name} is already governed by built-in trust policy."}
    try:
        validate_json_schema(tool.get("parameters", {}))
    except ValueError as exc:
        update_registered_tool_status(tool_name, str(tool.get("version", "")), last_error=str(exc), trusted=False)
        return {"promoted": False, "error": str(exc), "schema_valid": False}

    tests = run_package_tests_for_tool(tool)
    tests_passed = bool(tests.get("passed"))
    update_registered_tool_status(tool_name, str(tool.get("version", "")), last_test_status="passed" if tests_passed else "failed", trusted=False)
    if not tests_passed:
        return {"promoted": False, "error": "Tool tests failed.", "schema_valid": True, "tests": tests}

    evals = run_tool_evals(tool_name, str(tool.get("version", "")))
    if not evals.get("passed"):
        return {"promoted": False, "error": evals.get("error") or "Tool evals failed.", "schema_valid": True, "tests": tests, "evals": evals}

    if evals.get("undeclared_permissions"):
        return {"promoted": False, "error": "Tool used undeclared permissions.", "schema_valid": True, "tests": tests, "evals": evals}

    status = update_registered_tool_status(
        tool_name,
        str(tool.get("version", "")),
        last_test_status="passed",
        last_eval_status="passed",
        last_error=None,
        trusted=True,
    )
    return {"promoted": True, "trusted": True, "schema_valid": True, "tests": tests, "evals": evals, "tool": status.get("tool")}


WRITE_CALLS = {"write_text", "write_bytes", "unlink", "mkdir", "makedirs", "rmdir", "remove", "rename", "replace", "touch"}
SUBPROCESS_IMPORTS = {"subprocess", "pty", "shutil"}
NETWORK_IMPORTS = {"socket", "http.client", "httpx", "requests", "urllib", "urllib.request", "ftplib", "smtplib"}
BLOCKED_IMPORTS = {"ctypes"}
DYNAMIC_EXECUTION_CALLS = {"eval", "exec", "compile", "__import__"}
OS_PROCESS_CALLS = {"system", "popen", "spawnl", "spawnlp", "spawnv", "spawnvp", "execv", "execvp"}
SANDBOX_TIERS = ("read_only", "workspace_write", "network_enabled", "host_full")
SANDBOX_TIER_RANK = {tier: index for index, tier in enumerate(SANDBOX_TIERS)}
SAFE_EXECUTION_ENV_VARS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "WINDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
}
DESTRUCTIVE_GIT_REASON = "destructive git command requires explicit approval"
DESTRUCTIVE_GIT_PATTERNS = (
    re.compile(r"\bgit\s+reset\s+[^\n;&|]*--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+[^\n;&|]*(?:-[a-z]*f[a-z]*d|-d[a-z]*f)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+[^\n;&|]*(?:--force(?:-with-lease)?\b|-f\b)", re.IGNORECASE),
)


def approved_execution_roots() -> list[Path]:
    roots = [ROOT_DIR, BASE_DIR, TOOLS_DIR, DATA_DIR, ARCHIVE_DIR, KORA_DIR]
    return [root.resolve() for root in roots]


def path_in_approved_roots(path: Path) -> bool:
    resolved = path.resolve()
    for root in approved_execution_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def path_in_workspace(path: Path) -> bool:
    resolved = path.resolve()
    workspace = ROOT_DIR.resolve()
    return resolved == workspace or workspace in resolved.parents


def higher_sandbox_tier(current: str, candidate: str) -> str:
    if SANDBOX_TIER_RANK[candidate] > SANDBOX_TIER_RANK[current]:
        return candidate
    return current


def sandbox_tier_for_reasons(reasons: list[str]) -> str:
    tier = "read_only"
    for reason in reasons:
        if reason == "network access":
            tier = higher_sandbox_tier(tier, "network_enabled")
        elif reason in {"subprocess/process access", "home-directory access"} or reason.startswith("absolute path outside approved roots"):
            tier = higher_sandbox_tier(tier, "host_full")
        elif reason == "filesystem write or mutation":
            tier = higher_sandbox_tier(tier, "workspace_write")
    return tier


def build_python_execution_env(sandbox_tier: str) -> dict[str, str]:
    if sandbox_tier == "host_full":
        return os.environ.copy()
    env = {key: value for key, value in os.environ.items() if key in SAFE_EXECUTION_ENV_VARS}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["OUROBOROS_SANDBOX_TIER"] = sandbox_tier
    env["OUROBOROS_WORKSPACE_ROOT"] = str(ROOT_DIR.resolve())
    return env


def python_sandbox_guard(sandbox_tier: str) -> str:
    if sandbox_tier == "host_full":
        return ""
    workspace = str(ROOT_DIR.resolve())
    read_only = sandbox_tier == "read_only"
    return f"""
import builtins as _ouro_builtins
import os as _ouro_os
import pathlib as _ouro_pathlib
import shutil as _ouro_shutil

_OURO_WORKSPACE = {workspace!r}
_OURO_READ_ONLY = {read_only!r}
_OURO_WRITE_FLAGS = ("w", "a", "x", "+")

def _ouro_resolve(path):
    return _ouro_os.path.realpath(_ouro_os.path.abspath(_ouro_os.fspath(path)))

def _ouro_is_workspace_path(path):
    resolved = _ouro_resolve(path)
    return resolved == _OURO_WORKSPACE or resolved.startswith(_OURO_WORKSPACE + _ouro_os.sep)

def _ouro_check_write_path(path):
    if _OURO_READ_ONLY:
        raise PermissionError("read_only sandbox blocks filesystem writes")
    if not _ouro_is_workspace_path(path):
        raise PermissionError("workspace_write sandbox blocks writes outside workspace: " + _ouro_os.fspath(path))

def _ouro_mode_writes(mode):
    return any(flag in str(mode) for flag in _OURO_WRITE_FLAGS)

_ouro_open = _ouro_builtins.open
def open(file, mode="r", *args, **kwargs):
    if _ouro_mode_writes(mode):
        _ouro_check_write_path(file)
    return _ouro_open(file, mode, *args, **kwargs)
_oouro_unused = setattr(_ouro_builtins, "open", open)

_ouro_path_open = _ouro_pathlib.Path.open
def _ouro_guarded_path_open(self, mode="r", *args, **kwargs):
    if _ouro_mode_writes(mode):
        _ouro_check_write_path(self)
    return _ouro_path_open(self, mode, *args, **kwargs)
_oouro_unused = setattr(_ouro_pathlib.Path, "open", _ouro_guarded_path_open)

def _ouro_wrap_path_write(name):
    original = getattr(_ouro_pathlib.Path, name)
    def wrapper(self, *args, **kwargs):
        _ouro_check_write_path(self)
        return original(self, *args, **kwargs)
    setattr(_ouro_pathlib.Path, name, wrapper)
for _ouro_name in ("write_text", "write_bytes", "touch", "unlink", "mkdir", "rmdir"):
    _ouro_wrap_path_write(_ouro_name)

def _ouro_wrap_os_write(name, path_indexes=(0,)):
    original = getattr(_ouro_os, name, None)
    if original is None:
        return
    def wrapper(*args, **kwargs):
        for index in path_indexes:
            if len(args) > index:
                _ouro_check_write_path(args[index])
        return original(*args, **kwargs)
    setattr(_ouro_os, name, wrapper)
for _ouro_name in ("remove", "unlink", "rmdir", "mkdir", "makedirs"):
    _ouro_wrap_os_write(_ouro_name)
for _ouro_name in ("rename", "replace"):
    _ouro_wrap_os_write(_ouro_name, (0, 1))

def _ouro_wrap_shutil_write(name, path_indexes):
    original = getattr(_ouro_shutil, name, None)
    if original is None:
        return
    def wrapper(*args, **kwargs):
        for index in path_indexes:
            if len(args) > index:
                _ouro_check_write_path(args[index])
        return original(*args, **kwargs)
    setattr(_ouro_shutil, name, wrapper)
_oouro_unused = _ouro_wrap_shutil_write("copy", (1,))
_oouro_unused = _ouro_wrap_shutil_write("copy2", (1,))
_oouro_unused = _ouro_wrap_shutil_write("copyfile", (1,))
_oouro_unused = _ouro_wrap_shutil_write("move", (0, 1))
_oouro_unused = _ouro_wrap_shutil_write("rmtree", (0,))
"""
def command_text_contains_destructive_git(command_text: str) -> bool:
    normalized = re.sub(r"\s+", " ", command_text.strip())
    return any(pattern.search(normalized) for pattern in DESTRUCTIVE_GIT_PATTERNS)


def command_parts_contain_destructive_git(parts: Sequence[str]) -> bool:
    lowered = [part.lower() for part in parts]
    if "git" not in lowered:
        return False
    git_index = lowered.index("git")
    tail = lowered[git_index + 1 :]
    if len(tail) >= 2 and tail[0] == "reset" and "--hard" in tail:
        return True
    if tail and tail[0] == "clean":
        return any(part.startswith("-") and "f" in part and "d" in part for part in tail[1:])
    if tail and tail[0] == "push":
        return any(part in {"-f", "--force", "--force-with-lease"} or part.startswith("--force=") for part in tail[1:])
    return False
def filesystem_checkpoint_metadata(code: str) -> dict[str, Any]:
    affected_paths = checkpoints.infer_affected_files(code, ROOT_DIR)
    strategy = "files" if affected_paths else "git_repo" if (ROOT_DIR / ".git").exists() else "none"
    return {
        "enabled": strategy != "none",
        "strategy": strategy,
        "affected_paths_inferred": bool(affected_paths),
        "affected_paths": [str(path) for path in affected_paths],
        "storage_dir": str(CHECKPOINTS_DIR),
    }


def create_filesystem_mutation_checkpoint(code: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    if "filesystem write or mutation" not in policy.get("reasons", []):
        return None
    affected_paths = [Path(path) for path in policy.get("checkpoint", {}).get("affected_paths", [])]
    checkpoint = checkpoints.create_checkpoint(
        affected_paths,
        root_dir=ROOT_DIR,
        data_dir=DATA_DIR,
        reason="approved execute_python filesystem mutation",
        code=code,
    )
    return {
        "id": checkpoint["id"],
        "created_at": checkpoint["created_at"],
        "strategy": checkpoint["strategy"],
        "path_count": checkpoint["path_count"],
        "storage_dir": str(CHECKPOINTS_DIR),
    }


def rollback_latest_checkpoint(*, caller: str = "") -> dict[str, Any]:
    return KERNEL.rollback_latest_checkpoint(caller=caller)


def summarize_python_execution_policy(code: str) -> dict[str, Any]:
    reasons: list[str] = []
    blocked: list[str] = []
    affected_paths: list[str] = []
    network_risk = False
    process_risk = False
    manual_approval_required = False
    module_aliases: dict[str, str] = {}
    risky_call_aliases: dict[str, str] = {}

    if command_text_contains_destructive_git(code):
        reasons.append(DESTRUCTIVE_GIT_REASON)
        manual_approval_required = True

    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    module_aliases[alias.asname or top_level] = top_level
                    if alias.name in SUBPROCESS_IMPORTS or top_level in SUBPROCESS_IMPORTS:
                        reasons.append("subprocess/process access")
                        process_risk = True
                    if alias.name in NETWORK_IMPORTS or top_level in NETWORK_IMPORTS:
                        reasons.append("network access")
                        network_risk = True
                    if alias.name in BLOCKED_IMPORTS or top_level in BLOCKED_IMPORTS:
                        blocked.append(f"blocked import: {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
                top_level = module_name.split(".")[0]
                if module_name in SUBPROCESS_IMPORTS or top_level in SUBPROCESS_IMPORTS:
                    reasons.append("subprocess/process access")
                    process_risk = True
                if module_name in NETWORK_IMPORTS or top_level in NETWORK_IMPORTS:
                    reasons.append("network access")
                    network_risk = True
                if module_name in BLOCKED_IMPORTS or top_level in BLOCKED_IMPORTS:
                    blocked.append(f"blocked import: {module_name}")
                if module_name == "os":
                    for alias in node.names:
                        if alias.name in OS_PROCESS_CALLS:
                            risky_call_aliases[alias.asname or alias.name] = "subprocess/process access"

        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                string_parts = [
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
                if command_parts_contain_destructive_git(string_parts):
                    reasons.append(DESTRUCTIVE_GIT_REASON)
                    manual_approval_required = True
            if isinstance(node, ast.Call):
                func_name = ""
                is_attribute_call = isinstance(node.func, ast.Attribute)
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    if func_name in risky_call_aliases:
                        reasons.append(risky_call_aliases[func_name])
                        process_risk = True
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                    if isinstance(node.func.value, ast.Name):
                        owner_module = module_aliases.get(node.func.value.id, node.func.value.id)
                        if owner_module == "os" and func_name in OS_PROCESS_CALLS:
                            reasons.append("subprocess/process access")
                            process_risk = True
                if func_name in DYNAMIC_EXECUTION_CALLS:
                    blocked.append(f"dynamic code execution via {func_name}()")
                if func_name in WRITE_CALLS:
                    reasons.append("filesystem write or mutation")
                if func_name == "open":
                    mode = "r"
                    mode_arg_index = 0 if is_attribute_call else 1
                    if (
                        len(node.args) > mode_arg_index
                        and isinstance(node.args[mode_arg_index], ast.Constant)
                        and isinstance(node.args[mode_arg_index].value, str)
                    ):
                        mode = node.args[mode_arg_index].value
                    for keyword in node.keywords:
                        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                            mode = keyword.value.value
                    if any(flag in mode for flag in ("w", "a", "x", "+")):
                        reasons.append("filesystem write or mutation")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if command_text_contains_destructive_git(text):
                    reasons.append(DESTRUCTIVE_GIT_REASON)
                    manual_approval_required = True
                if text.startswith("~"):
                    reasons.append("home-directory access")
                path = Path(text)
                if text.startswith(("/", "./", "../")) or "/" in text or "\\" in text:
                    affected_paths.append(text)
                if path.is_absolute() and not path_in_approved_roots(path):
                    reasons.append(f"absolute path outside approved roots: {text}")
            if isinstance(node, ast.Attribute) and node.attr in {"home", "expanduser"}:
                reasons.append("home-directory access")

    unique_paths = sorted(set(affected_paths))
    if blocked:
        unique_blocked = sorted(set(blocked))
        return {
            "action": "block",
            "sandbox_tier": "host_full",
            "risk_summary": "Policy blocked execution: " + "; ".join(unique_blocked) + ".",
            "reasons": unique_blocked,
            "affected_paths": unique_paths,
            "network_risk": network_risk,
            "process_risk": process_risk,
            "manual_approval_required": True,
            "manual_approval_required": manual_approval_required,
        }

    unique_reasons = sorted(set(reasons))
    if unique_reasons:
        sandbox_tier = sandbox_tier_for_reasons(unique_reasons)
        response: dict[str, Any] = {
            "action": "require_approval",
            "sandbox_tier": sandbox_tier,
            "risk_summary": f"Requires {sandbox_tier} sandbox approval: " + "; ".join(unique_reasons) + ".",
            "reasons": unique_reasons,
            "affected_paths": unique_paths,
            "network_risk": network_risk,
            "process_risk": process_risk,
            "manual_approval_required": manual_approval_required,
        }
        if "filesystem write or mutation" in unique_reasons:
            response["checkpoint"] = filesystem_checkpoint_metadata(code)
        return response

    return {
        "action": "allow",
        "sandbox_tier": "read_only",
        "risk_summary": "Read-only Python execution appears low risk.",
        "reasons": [],
        "affected_paths": unique_paths,
        "network_risk": network_risk,
        "process_risk": process_risk,
        "manual_approval_required": False,
    }


async def execute_python(code: str, policy_approved: bool = False) -> dict[str, Any]:
    policy = summarize_python_execution_policy(code)
    if policy["action"] == "block":
        return {"error": policy["risk_summary"], "policy": policy, "exit_code": -1, "timed_out": False}
    if policy["action"] == "require_approval" and not policy_approved:
        return {"error": "Python execution requires approval by policy.", "policy": policy, "exit_code": -1, "timed_out": False}

    sandbox_tier = policy.get("sandbox_tier", "read_only")
    checkpoint_metadata = create_filesystem_mutation_checkpoint(code, policy) if policy_approved else None
    payload = {
        "code": code,
        "tier": sandbox_tier,
        "workspace_root": str(ROOT_DIR.resolve()),
        "timeout_seconds": 30,
        "output_limit_bytes": 100_000,
    }
    start = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SANDBOX_WORKER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        cwd=str(ROOT_DIR.resolve()),
        env=sandbox_worker.build_python_execution_env(
            sandbox_worker.config_for_tier(sandbox_tier, workspace_root=ROOT_DIR)
        ),
    )
    stdout, stderr = await proc.communicate(json.dumps(payload).encode("utf-8"))
    if proc.returncode == 0 and stdout:
        try:
            result = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            result = {
                "stdout": stdout.decode("utf-8", errors="replace")[-100_000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-100_000:],
                "exit_code": proc.returncode,
                "timed_out": False,
                "duration_ms": int((time.time() - start) * 1000),
                "isolation_degraded": True,
            }
    else:
        result = {
            "stdout": stdout.decode("utf-8", errors="replace")[-100_000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-100_000:],
            "exit_code": proc.returncode,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000),
            "isolation_degraded": True,
        }
    if checkpoint_metadata is not None:
        result["checkpoint"] = checkpoint_metadata
    return result




def registered_tool_expects_json(tool: dict[str, Any]) -> bool:
    metadata = load_registry_tool_metadata(tool)
    metadata = tool.get("metadata", {}) if isinstance(tool.get("metadata", {}), dict) else {}
    return tool.get("output_format") == "json" or metadata.get("output_format") == "json"


def annotate_registered_tool_output(result: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("malformed_output", False)
    if not registered_tool_expects_json(tool):
        return result
    stdout = str(result.get("stdout", "")).strip()
    if not stdout:
        result["malformed_output"] = True
        result["error"] = "Registered tool produced no JSON output."
        return result
    try:
        result["output"] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        result["malformed_output"] = True
        result["error"] = f"Registered tool produced malformed JSON output: {exc.msg}."
    return result



def arguments_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tool_permission_defaults() -> dict[str, Any]:
    return {"filesystem": [], "network": [], "environment": [], "process": {"allow": False}, "secrets": []}


def normalize_tool_permissions(metadata: dict[str, Any] | None) -> dict[str, Any]:
    raw = (metadata or {}).get("permissions")
    if raw is None:
        raw = {
            "filesystem": (metadata or {}).get("requested_filesystem_scopes", []),
            "network": (metadata or {}).get("requested_network_scopes", []),
            "environment": (metadata or {}).get("requested_environment_variables", []),
            "process": (metadata or {}).get("process_permissions", {"allow": False}),
            "secrets": (metadata or {}).get("secret_access", []),
        }
    if not isinstance(raw, dict):
        raise ValueError("metadata.json permissions must be an object.")
    normalized = tool_permission_defaults()
    filesystem = raw.get("filesystem", raw.get("requested_filesystem_scopes", []))
    if not isinstance(filesystem, list):
        raise ValueError("permissions.filesystem must be a list.")
    fs_scopes = []
    for item in filesystem:
        scope = {"path": item, "access": "read"} if isinstance(item, str) else item
        if not isinstance(scope, dict):
            raise ValueError("permissions.filesystem entries must be strings or objects.")
        path_value = scope.get("path")
        access = scope.get("access", "read")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("permissions.filesystem entries require a non-empty path.")
        if access not in {"read", "write", "read_write"}:
            raise ValueError("permissions.filesystem access must be read, write, or read_write.")
        fs_scopes.append({"path": path_value, "access": access})
    normalized["filesystem"] = fs_scopes

    network = raw.get("network", raw.get("requested_network_scopes", []))
    if not isinstance(network, list):
        raise ValueError("permissions.network must be a list.")
    net_scopes = []
    for item in network:
        scope = {"host": item, "ports": []} if isinstance(item, str) else item
        if not isinstance(scope, dict):
            raise ValueError("permissions.network entries must be strings or objects.")
        host = scope.get("host")
        ports = scope.get("ports", [])
        if not isinstance(host, str) or not host.strip():
            raise ValueError("permissions.network entries require a non-empty host.")
        if not isinstance(ports, list) or not all(isinstance(port, int) for port in ports):
            raise ValueError("permissions.network ports must be a list of integers.")
        net_scopes.append({"host": host, "ports": ports})
    normalized["network"] = net_scopes

    environment = raw.get("environment", raw.get("requested_environment_variables", []))
    if not isinstance(environment, list) or not all(isinstance(item, str) and item for item in environment):
        raise ValueError("permissions.environment must be a list of non-empty strings.")
    normalized["environment"] = environment

    process = raw.get("process", raw.get("process_permissions", {"allow": False}))
    if isinstance(process, bool):
        process = {"allow": process}
    if not isinstance(process, dict) or not isinstance(process.get("allow", False), bool):
        raise ValueError("permissions.process must be a boolean or an object with boolean allow.")
    normalized["process"] = {"allow": bool(process.get("allow", False))}

    secrets = raw.get("secrets", raw.get("secret_access", []))
    if not isinstance(secrets, list) or not all(isinstance(item, str) and item for item in secrets):
        raise ValueError("permissions.secrets must be a list of non-empty strings.")
    normalized["secrets"] = secrets
    return normalized


def validate_tool_permission_manifest(metadata: dict[str, Any]) -> dict[str, Any]:
    permissions = normalize_tool_permissions(metadata)
    metadata["permissions"] = permissions
    metadata.setdefault("requested_filesystem_scopes", permissions["filesystem"])
    metadata.setdefault("requested_network_scopes", permissions["network"])
    metadata.setdefault("requested_environment_variables", permissions["environment"])
    metadata.setdefault("process_permissions", permissions["process"])
    metadata.setdefault("secret_access", permissions["secrets"])
    return permissions


def tool_permissions_from_entry(tool: dict[str, Any]) -> dict[str, Any]:
    if isinstance(tool.get("permissions"), dict):
        return normalize_tool_permissions({"permissions": tool["permissions"]})
    return normalize_tool_permissions(load_tool_metadata(tool))


def permission_runtime_guard(permissions: dict[str, Any], package_dir: Path) -> str:
    fs_scopes = permissions.get("filesystem", [])
    network_scopes = permissions.get("network", [])
    process_allowed = bool(permissions.get("process", {}).get("allow", False))
    env_allowed = set(permissions.get("environment", [])) | set(permissions.get("secrets", [])) | SAFE_EXECUTION_ENV_VARS
    scope_payload = []
    for scope in fs_scopes:
        raw = scope["path"]
        path_value = Path(raw).expanduser() if raw.startswith("~") else Path(raw)
        if not path_value.is_absolute():
            path_value = (ROOT_DIR / path_value).resolve()
        scope_payload.append({"path": str(path_value), "access": scope["access"]})
    scope_payload.append({"path": str(package_dir.resolve()), "access": "read"})
    lines = [
        "import builtins as _b, os as _os, pathlib as _p, runpy as _runpy, socket as _socket, subprocess as _subprocess",
        f"_FS={scope_payload!r}",
        f"_NET={network_scopes!r}",
        f"_PROC={process_allowed!r}",
        f"_ENV={sorted(env_allowed)!r}",
        "def _res(path): return _os.path.realpath(_os.path.abspath(_os.fspath(path)))",
        "def _allows(path, write):\n    r=_res(path)\n    for s in _FS:\n        root=_res(s['path'])\n        if r==root or r.startswith(root+_os.sep):\n            a=s.get('access','read')\n            if write and a in ('write','read_write'): return True\n            if (not write) and a in ('read','read_write'): return True\n    return False",
        "def _writes(mode): return any(f in str(mode) for f in ('w','a','x','+'))",
        "def _check(path, write=False):\n    if not _allows(path, write):\n        raise PermissionError(('undeclared filesystem write access: ' if write else 'undeclared filesystem read access: ')+_os.fspath(path))",
        "_open=_b.open\ndef open(file, mode='r', *args, **kwargs):\n    _check(file, _writes(mode))\n    return _open(file, mode, *args, **kwargs)\n_b.open=open",
        "_path_open=_p.Path.open\ndef _guard_path_open(self, mode='r', *args, **kwargs):\n    _check(self, _writes(mode))\n    return _path_open(self, mode, *args, **kwargs)\n_p.Path.open=_guard_path_open",
        "def _wrap_path_write(name):\n    orig=getattr(_p.Path,name)\n    def wrapper(self,*args,**kwargs):\n        _check(self, True)\n        return orig(self,*args,**kwargs)\n    setattr(_p.Path,name,wrapper)\nfor _n in ('write_text','write_bytes','touch','unlink','mkdir','rmdir'):\n    _wrap_path_write(_n)",
        "_conn=_socket.socket.connect\ndef _guard_connect(self,address,*args,**kwargs):\n    host=address[0] if isinstance(address,tuple) and address else address\n    ok=any(s.get('host')=='*' or s.get('host')==host for s in _NET)\n    if not ok: raise PermissionError('undeclared network access: '+str(host))\n    return _conn(self,address,*args,**kwargs)\n_socket.socket.connect=_guard_connect",
        "if not _PROC:\n    def _block_process(*args, **kwargs): raise PermissionError('undeclared subprocess/process access')\n    _subprocess.Popen=_block_process\n    for _n in ('system','popen','spawnl','spawnlp','spawnv','spawnvp','execv','execvp'):\n        if hasattr(_os,_n): setattr(_os,_n,_block_process)",
        "_copy=dict(_os.environ)\n_os.environ.clear()\nfor _k,_v in _copy.items():\n    if _k in _ENV: _os.environ[_k]=_v",
    ]
    return "\n".join(lines) + "\n"


def summarize_tool_permission_use(result: dict[str, Any], permissions: dict[str, Any]) -> dict[str, Any]:
    text = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr", "error"))
    return {
        "filesystem": bool(permissions.get("filesystem")) or "filesystem" in text or "PermissionError" in text,
        "network": bool(permissions.get("network")) or "network" in text,
        "process": bool(permissions.get("process", {}).get("allow", False)) or "subprocess" in text or "process" in text,
        "environment": bool(permissions.get("environment") or permissions.get("secrets")),
    }


def audit_registered_tool_execution(tool: dict[str, Any], arguments: dict[str, Any], permissions: dict[str, Any], result: dict[str, Any], approval_id: str | None = None) -> None:
    try:
        status = "timeout" if result.get("timed_out") else "success" if int(result.get("exit_code", -1)) == 0 else "failed"
    except (TypeError, ValueError):
        status = "failed"
    permissions_used = summarize_tool_permission_use(result, permissions)
    files_touched = [scope.get("path") for scope in permissions.get("filesystem", []) if isinstance(scope, dict)]
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO tool_execution_audit (id, timestamp, tool_name, tool_version, arguments_hash, permissions_used, files_touched, network_flag, process_flag, approval_id, result_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), now_iso(), str(tool.get("name", "")), str(tool.get("version", "")), arguments_hash(arguments), json.dumps(permissions_used, sort_keys=True), json.dumps(files_touched), 1 if permissions_used.get("network") else 0, 1 if permissions_used.get("process") else 0, approval_id, status),
        )
        conn.commit()


def run_registered_tool(tool: dict[str, Any], arguments: dict[str, Any], approval_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
    start = time.time()
    permissions = tool_permissions_from_entry(tool)
    try:
        filepath = tool.get("filepath")
        if not filepath:
            raise ValueError(f"Tool {tool.get('name')} has no filepath.")
        path = resolve_tool_path(filepath)
        guard = permission_runtime_guard(permissions, path.parent)
        runner = guard + f"\n_ouro_tool_path={str(path)!r}\n_runpy.run_path(_ouro_tool_path, run_name='__main__')\n"
        completed = subprocess.run(
            [sys.executable, "-c", runner],
            cwd=ROOT_DIR,
            input=json.dumps(arguments),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout if timeout is not None else REGISTERED_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
        result = {
            "stdout": completed.stdout[-100_000:],
            "stderr": completed.stderr[-100_000:],
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000),
            "malformed_output": False,
        }
        result = annotate_registered_tool_output(result, tool)
    except subprocess.TimeoutExpired as exc:
        result = {
            "stdout": (exc.stdout or "")[-100_000:] if isinstance(exc.stdout, str) else "",
            "stderr": ((exc.stderr or "")[-100_000:] if isinstance(exc.stderr, str) else "")
            or f"Timed out after {REGISTERED_TOOL_TIMEOUT_SECONDS}s",
            "exit_code": -1,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000),
            "error": f"Registered tool timed out after {REGISTERED_TOOL_TIMEOUT_SECONDS}s.",
            "malformed_output": False,
        }
    except Exception as exc:
        result = {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000),
            "error": f"Registered tool execution exception: {exc}",
            "exception": type(exc).__name__,
            "malformed_output": False,
        }
    audit_registered_tool_execution(tool, arguments, permissions, result, approval_id)
    return result

def save_tool_metadata(tool_name: str, metadata: dict[str, Any]) -> None:
    registry = load_registry()
    for entry in registry["tools"]:
        if entry.get("name") == tool_name:
            entry["metadata"] = metadata
            entry["updated_at"] = now_iso()
            save_json(REGISTRY_PATH, registry)
            return


def append_tool_repair_attempt(tool: dict[str, Any], arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(load_tool_metadata(tool))
    attempts = metadata.get("repair_attempts")
    if not isinstance(attempts, list):
        attempts = []
    failure = classify_registered_tool_failure(result, tool)
    attempt = {
        "timestamp": now_iso(),
        "arguments": arguments,
        "failure": failure,
        "result": result,
    }
    attempts.append(attempt)
    metadata["repair_attempts"] = attempts
    metadata["last_repair_attempt_at"] = attempt["timestamp"]
    metadata["repair_attempt_count"] = len(attempts)
    save_tool_metadata(str(tool.get("name", "")), metadata)
    tool["metadata"] = metadata
    return attempt


def classify_registered_tool_failure(result: dict[str, Any], tool: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if result.get("timed_out"):
        return {"type": "timeout", "message": str(result.get("error") or result.get("stderr") or "Tool timed out.")}
    if result.get("exception"):
        return {"type": "exception", "message": str(result.get("error") or result.get("stderr") or "Tool raised an exception.")}
    if result.get("malformed_output"):
        return {"type": "malformed_output", "message": str(result.get("error") or "Tool output was malformed.")}
    try:
        exit_code = int(result.get("exit_code", 0))
    except (TypeError, ValueError):
        exit_code = -1
    if exit_code != 0:
        return {"type": "nonzero_exit", "message": str(result.get("stderr") or result.get("error") or f"Tool exited with code {exit_code}.")}
    return None


def registered_tool_failed(result: dict[str, Any], tool: dict[str, Any] | None = None) -> bool:
    return classify_registered_tool_failure(result, tool) is not None



def build_tool_repair_message(tool: dict[str, Any], arguments: dict[str, Any], result: dict[str, Any], attempt_number: int, max_attempts: int) -> str:
    metadata = load_registry_tool_metadata(tool)
    failure = classify_registered_tool_failure(result, tool) or {"type": "unknown", "message": "Tool failed."}
    payload = {
        "tool": {key: tool.get(key) for key in ("name", "description", "filepath", "parameters", "requires_approval", "version")},
        "metadata": metadata,
        "arguments": arguments,
        "failure": failure,
        "result": result,
        "attempt_number": attempt_number,
        "max_attempts": max_attempts,
    }
    return textwrap.dedent(
        f"""
        A registered tool failed while serving the user's request. Repair it autonomously.

        Repair protocol:
        1. Inspect the tool file and its registry metadata.
        2. Patch the tool using execute_python.
        3. Run its test_command, test_plan, sample_arguments, or package tests.py against the failing arguments.
        4. Re-register the tool or update its metadata if the interface, description, parameters, output format, or approval policy changed.
        5. Then retry the original tool call or continue with the user's request.

        Repair attempts are limited to {max_attempts}; this is attempt {attempt_number}.

        Failure context JSON:
        {json.dumps(payload, indent=2)}
        """
    ).strip()
def update_registered_tool_status(
    name: str,
    version: str | None = None,
    *,
    last_test_status: str | None = None,
    last_error: str | None = None,
    trusted: bool | None = None,
    increment_use_count: bool = False,
    last_eval_status: str | None = None,
) -> dict[str, Any]:
    registry = load_registry()
    target: dict[str, Any] | None = None
    for tool in registry["tools"]:
        if tool.get("name") == name and (version is None or str(tool.get("version", "")) == str(version)):
            if target is None or parse_version_key(tool.get("version")) >= parse_version_key(target.get("version")):
                target = tool
    if target is None:
        return {"error": f"Tool {name} not found."}
    if last_test_status is not None:
        target["last_test_status"] = last_test_status
    if last_eval_status is not None:
        target["last_eval_status"] = last_eval_status
    if last_error is not None or last_test_status == "passed" or last_eval_status == "passed":
        target["last_error"] = last_error
    if trusted is not None:
        target["trusted"] = trusted
    if increment_use_count:
        target["use_count"] = int(target.get("use_count", 0)) + 1
    target["updated_at"] = now_iso()
    save_json(REGISTRY_PATH, registry)
    return {"updated": target["name"], "version": target.get("version"), "tool": target}


def validate_registered_tool(name: str, version: str | None = None, sample_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    tool = find_tool_version(name, version)
    if tool is None:
        return {"error": f"Tool {name} not found."}
    if tool.get("builtin"):
        return {"error": f"Builtin tool {name} cannot be validated as a registered file tool."}
    arguments = sample_arguments if sample_arguments is not None else tool.get("sample_arguments", {})
    if not isinstance(arguments, dict):
        return {"error": "sample_arguments must be an object."}

    result = run_registered_tool(tool, arguments)
    passed = result.get("exit_code") == 0 and not result.get("timed_out")
    error = None if passed else (result.get("stderr") or result.get("error") or f"Exited with code {result.get('exit_code')}")
    result = run_registered_tool(tool, arguments)
    passed = result.get("exit_code") == 0 and not result.get("timed_out")
    error = None if passed else (result.get("stderr") or f"Exited with code {result.get('exit_code')}")
    status = update_registered_tool_status(name, str(tool.get("version", "")), last_test_status="passed" if passed else "failed", last_error=error, trusted=passed)
    return {"validated": passed, "name": name, "version": status.get("version"), "result": result, "tool": status.get("tool")}
    status = update_registered_tool_status(
        name,
        str(tool.get("version", "")),
        last_test_status="passed" if passed else "failed",
        last_error=error,
        trusted=False,
    )
    return {
        "validated": passed,
        "name": name,
        "version": status.get("version"),
        "result": result,
        "tool": status.get("tool"),
    }
def registry_entry_defaults() -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "supersedes": None,
        "created_by_task_id": None,
        "eval_score": None,
        "rollback_to": None,
        "deprecation_reason": "",
        "deprecated": False,
    }


def normalize_registry_entry(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(registry_entry_defaults())
    normalized.update(entry)
    if normalized.get("created_by_task_id") is None and normalized.get("source_task_id") is not None:
        normalized["created_by_task_id"] = normalized.get("source_task_id")
    normalized["deprecated"] = bool(normalized.get("deprecated", False))
    normalized["deprecation_reason"] = str(normalized.get("deprecation_reason") or "")
    if normalized.get("builtin") and "trusted" not in entry:
        normalized["trusted"] = True
    return normalized


def deprecate_tool_version(name: str, version: str, reason: str) -> dict[str, Any]:
    registry = load_registry()
    for tool in registry["tools"]:
        if tool.get("name") == name and str(tool.get("version", "")) == str(version):
            tool["deprecated"] = True
            tool["deprecation_reason"] = reason
            tool["trusted"] = False
            tool["updated_at"] = now_iso()
            save_json(REGISTRY_PATH, registry)
            return {"deprecated": name, "version": str(version), "reason": reason}
    return {"error": f"Tool {name} version {version} not found."}


def rollback_tool_version(name: str, target_version: str) -> dict[str, Any]:
    registry = load_registry()
    target: dict[str, Any] | None = None
    for tool in registry["tools"]:
        if tool.get("name") == name and str(tool.get("version", "")) == str(target_version):
            target = tool
            break
    if target is None:
        return {"error": f"Tool {name} version {target_version} not found."}

    timestamp = now_iso()
    target["trusted"] = True
    target["deprecated"] = False
    target["deprecation_reason"] = ""
    target["rollback_to"] = None
    target["updated_at"] = timestamp

    for tool in registry["tools"]:
        if tool is target or tool.get("name") != name:
            continue
        if parse_version_key(tool.get("version")) > parse_version_key(target_version):
            tool["trusted"] = False
            tool["deprecated"] = True
            tool["rollback_to"] = str(target_version)
            tool["deprecation_reason"] = f"Rolled back to {target_version}."
            tool["updated_at"] = timestamp

    save_json(REGISTRY_PATH, registry)
    return {"rolled_back": name, "version": str(target_version), "tool": target}


def register_tool(
    name: str,
    description: str,
    parameters_schema: dict[str, Any] | None = None,
    filepath: str = "",
    requires_approval: bool = False,
    version: str = "1.0.0",
    source_task_id: str | None = None,
    test_command: str | None = None,
    test_plan: str | None = None,
    sample_arguments: dict[str, Any] | None = None,
    supersedes: str | None = None,
    created_by_task_id: str | None = None,
    eval_score: float | None = None,
) -> dict[str, Any]:
    validate_tool_name(name)
    package_info: dict[str, Any] | None = None
    metadata: dict[str, Any] = {"repair_attempts": []}
    entry_filepath = str(Path(filepath))

    try:
        path = resolve_tool_candidate(filepath)
        if path.is_dir():
            package_info = load_tool_package(path)
            parameters = package_info["schema"]
            metadata.update(package_info["metadata"])
            tool_version = str(metadata.get("version", version))
            test_status = "passed"
        else:
            if not isinstance(parameters_schema, dict):
                return {"error": "parameters_schema must be an object for legacy Python tool registrations."}
            validate_json_schema(parameters_schema)
            parameters = parameters_schema
            tool_version = str(version)
            test_status = "pending"
    except ValueError as exc:
        return {"error": str(exc)}

    if sample_arguments is not None and not isinstance(sample_arguments, dict):
        return {"error": "sample_arguments must be an object."}
    if package_info is None and not any([test_command, test_plan, sample_arguments is not None]):
        return {"error": "Legacy tool registration requires at least one of: test_command, test_plan, or sample_arguments."}

    registry = load_json(REGISTRY_PATH, {"tools": []})
    existing_versions = [t for t in registry.get("tools", []) if t.get("name") == name]
    if supersedes is None and existing_versions:
        previous = sorted(existing_versions, key=lambda tool: (parse_version_key(tool.get("version")), str(tool.get("updated_at", ""))))[-1]
        supersedes = str(previous.get("version", "")) or None

    timestamp = now_iso()
    effective_task_id = created_by_task_id if created_by_task_id is not None else source_task_id

    entry = normalize_registry_entry({
        "name": name,
        "description": description,
        "parameters": parameters,
        "filepath": entry_filepath,
        "builtin": False,
        "requires_approval": bool(requires_approval),
        "metadata": metadata,
        "version": tool_version,
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_task_id": source_task_id,
        "created_by_task_id": effective_task_id,
        "test_command": test_command,
        "test_plan": test_plan or ("Package tests passed." if package_info else ""),
        "sample_arguments": sample_arguments or {},
        "last_test_status": test_status,
        "last_error": None,
        "use_count": 0,
        "supersedes": supersedes,
        "trusted": bool(package_info),
        "deprecated": bool(metadata.get("deprecated", False)),
        "deprecation_reason": str(metadata.get("deprecation_reason", "")),
        "eval_score": eval_score,
    })

    if package_info is not None:
        entry["package"] = True
        entry["package_dir"] = entry_filepath

    registry["tools"] = [t for t in registry.get("tools", []) if not (t.get("name") == name and str(t.get("version", "")) == str(tool_version))]
    registry["tools"].append(entry)
    save_json(REGISTRY_PATH, registry)

    result = {"registered": name, "version": tool_version, "permanent": True, "trusted": entry["trusted"]}
    if package_info is not None:
        result["package"] = True
        result["tests"] = {
            "stdout": package_info.get("test_stdout", "")[-4000:],
            "stderr": package_info.get("test_stderr", "")[-4000:],
        }
    return result


def store_tool_execution(tool_name: str, arguments: dict[str, Any], result: dict[str, Any], approved: bool) -> str:
    execution_id = str(uuid.uuid4())
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO tool_execution (id, tool_name, arguments, result, timestamp, approved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                tool_name,
                json.dumps(arguments),
                json.dumps(result),
                now_iso(),
                1 if approved else 0,
            ),
        )
        conn.commit()
    return execution_id


def build_evaluation_prompt() -> str:
    return textwrap.dedent(
        """
        Evaluate the immediately preceding result and choose the next step before any final answer is produced.
        Be generic: judge only whether the result is adequate for the current task, without assuming any specific domain, data source, environment, or artifact type.

        Choose exactly one decision:
        - continue: proceed because more ordinary work is needed.
        - retry: repeat or adjust the previous action because the result is missing, invalid, failed, or insufficient.
        - register_tool: preserve a newly useful reusable capability before proceeding.
        - rollback: undo or mitigate the previous action before proceeding.
        - final: the result is sufficient to produce the final answer.

        Return only JSON in this shape: {"decision":"continue|retry|register_tool|rollback|final","rationale":"brief reason"}
        """
    ).strip()


def parse_evaluation_decision(raw_response: str) -> dict[str, str]:
    decision = "continue"
    rationale = "No rationale provided."
    try:
        payload = json.loads(raw_response.strip())
        if isinstance(payload, dict):
            candidate = str(payload.get("decision", "")).strip().lower()
            if candidate in EVALUATION_DECISIONS:
                decision = candidate
            if payload.get("rationale"):
                rationale = str(payload["rationale"]).strip()
    except json.JSONDecodeError:
        match = re.search(r"\b(continue|retry|register_tool|rollback|final)\b", raw_response, re.IGNORECASE)
        if match:
            decision = match.group(1).lower()
        cleaned = raw_response.strip()
        if cleaned:
            rationale = cleaned[:500]
    return {"decision": decision, "rationale": rationale}


def store_evaluation_decision(session_id: str, decision: str, rationale: str, raw_response: str) -> str:
    decision_id = str(uuid.uuid4())
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO evaluation_decision (id, session_id, decision, rationale, raw_response, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, session_id, decision, rationale, raw_response, now_iso()),
        )
        conn.commit()
    return decision_id


async def complete_model_text(messages: list[dict[str, str]], model: str, provider: str) -> str:
    text = ""
    async for token in app.state.model_adapter.complete(messages, model, provider):
        text += token
    return text


async def evaluate_tool_result(
    conversation: list[dict[str, str]],
    session_id: str,
    model: str,
    provider: str,
) -> dict[str, str]:
    conversation.append({"role": "system", "content": build_evaluation_prompt()})
    try:
        raw_response = await complete_model_text(prune_conversation(conversation), model, provider)
    except Exception as exc:
        conversation.pop()
        decision = "final" if any(kw in str(exc).lower() for kw in ["503", "402", "429", "provider", "exhausted", "unavailable", "balance"]) else "continue"
        return {"decision": decision, "rationale": f"Evaluation unavailable", "error": str(exc)}
    conversation.append({"role": "assistant", "content": raw_response})
    parsed = parse_evaluation_decision(raw_response)
    store_evaluation_decision(session_id, parsed["decision"], parsed["rationale"], raw_response)
    if parsed["decision"] == "retry":
        conversation.append({"role": "system", "content": "Evaluation decision: retry. Adjust or repeat the previous action before finalizing."})
    elif parsed["decision"] == "register_tool":
        conversation.append({"role": "system", "content": "Evaluation decision: register_tool. Preserve the reusable capability before finalizing."})
    elif parsed["decision"] == "rollback":
        conversation.append({"role": "system", "content": "Evaluation decision: rollback. Undo or mitigate the previous action before finalizing."})
    elif parsed["decision"] == "final":
        conversation.append({"role": "system", "content": "Evaluation decision: final. Produce the final answer now."})
    else:
        conversation.append({"role": "system", "content": "Evaluation decision: continue. Proceed with the next appropriate step."})
    return parsed


async def call_model_simple(prompt: str, model: str = "openai-fast") -> str:
    try:
        messages = [{"role": "user", "content": prompt}]
        provider_config = get_provider("pollinations")
        base_url = provider_config.get("base_url") or POLLINATIONS_BASE_URL
        api_key = get_provider_api_key_from_config(provider_config)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        payload = {"model": model, "messages": messages, "stream": False}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"ERROR: call_model_simple failed: {e}")
        raise


def archive_session(session_id: str, messages: list[dict[str, str]]) -> Path:
    write_session_archive(session_id, messages)
    return session_archive_path(session_id)


async def summarize_session(messages: list[dict[str, str]]) -> str:
    conversation_text = "\n".join([
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if m['role'] in ['user', 'assistant']
    ])
    summary_prompt = f"""
Summarize this conversation in 150 to 250 words.
Include: main topics, decisions made, tools built or registered, files modified, and any named entities (projects, people, dates, domain names, tools).
Write in past tense. Be specific and factual. No filler. No intro sentence.

CONVERSATION:
{conversation_text[:8000]}
"""
    return await call_model_simple(summary_prompt, model="openai-fast")


def store_episodic_memory(session_id: str, summary: str, keywords: list[str]) -> str:
    memory_id = str(uuid.uuid4())
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (id, created_at, keywords, summary, session_id) VALUES (?, ?, ?, ?, ?)",
            (memory_id, now_iso(), ",".join(keywords), summary, session_id),
        )
        conn.commit()
    return memory_id


def find_or_store_session_summary(session_id: str, summary: str) -> str:
    keywords = extract_keywords(summary) or ["session", "archive"]
    with connect_db() as conn:
        row = conn.execute(
            "SELECT id FROM episodic_memory WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row:
        return str(row["id"])
    return store_episodic_memory(session_id, summary, keywords)


async def summarize_and_store(messages: list[dict[str, str]], session_id: str):
    try:
        archive_path = archive_session(session_id, messages)
    except Exception as e:
        print(f"ERROR: archive_session failed: {e}")
        return {"status": "error", "message": str(e), "session_id": session_id, "archive_saved": False}

    summary_failed = False
    try:
        summary = await summarize_session(messages)
    except Exception as e:
        summary_failed = True
        print(f"WARNING: Model summarization failed, using fallback. Error: {e}")
        summary = f"Conversation session {session_id}"

    try:
        keywords = extract_keywords(summary)
        if not keywords:
            keywords = ["session", "archive"]

        write_session_archive(session_id, messages, summary=summary)
        store_episodic_memory(session_id, summary, keywords)
        enforce_memory_budget()
        return {
            "status": "saved",
            "session_id": session_id,
            "summary": summary,
            "archive_saved": True,
            "archive_path": str(archive_path),
            "summary_fallback": summary_failed,
        }
    except Exception as e:
        print(f"ERROR: summarize_and_store failed after archiving: {e}")
        return {
            "status": "error",
            "message": str(e),
            "session_id": session_id,
            "archive_saved": True,
            "archive_path": str(archive_path),
        }


class MemorySaveRequest(BaseModel):
    session_id: str
    messages: list[ChatMessage]


class MemoryCompactRequest(BaseModel):
    cutoff_days: int = MEMORY_COMPACTION_DEFAULT_CUTOFF_DAYS
    limit: int | None = None


class MemoryRecallRequest(BaseModel):
    query: str
    limit: int = MEMORY_RECALL_DEFAULT_LIMIT
    include_raw: bool = True


class SessionUpdateRequest(BaseModel):
    messages: list[ChatMessage]
    summary: str = ""
    title: str = ""


def safe_session_id(session_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", session_id):
        raise HTTPException(status_code=400, detail="Invalid session id.")
    return session_id


def session_archive_path(session_id: str) -> Path:
    return ARCHIVE_DIR / f"{safe_session_id(session_id)}.json"


def derive_session_title(messages: list[dict[str, str]], fallback: str) -> str:
    for message in messages:
        if message.get("role") == "user" and message.get("content", "").strip():
            title = re.sub(r"\s+", " ", message["content"].strip())
            return title[:80]
    return fallback


def archive_updated_at(path: Path, payload: dict[str, Any]) -> str:
    updated_at = payload.get("updated_at") or payload.get("created_at")
    if isinstance(updated_at, str) and updated_at:
        return updated_at
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()




def parse_archive_time(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def memory_setting_int(name: str, minimum: int = 0) -> int:
    value = load_settings().get(name, DEFAULT_SETTINGS[name])
    try:
        return max(int(value), minimum)
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[name])


def memory_setting_float(name: str, minimum: float = 0.0) -> float:
    value = load_settings().get(name, DEFAULT_SETTINGS[name])
    try:
        return max(float(value), minimum)
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS[name])


def archive_raw_size(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    except Exception:
        return path.stat().st_size
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def summary_tokens_to_chars(tokens: int) -> int:
    return max(tokens, 1) * 4


def durable_lines_from_messages(messages: list[dict[str, Any]], limit: int = 12) -> list[str]:
    patterns = ("decision", "decided", "remember", "important", "durable", "fact", "requirement", "must", "should")
    durable: list[str] = []
    for message in messages:
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        lower = content.lower()
        if any(pattern in lower for pattern in patterns):
            role = str(message.get("role", "unknown"))
            cleaned = re.sub(r'\s+', ' ', content)[:500]
            durable.append(f"{role}: {cleaned}")
        if len(durable) >= limit:
            break
    return durable


def compact_session_summary(archive: dict[str, Any], target_tokens: int) -> str:
    existing = str(archive.get("summary") or "").strip()
    messages = archive.get("messages") if isinstance(archive.get("messages"), list) else []
    durable = durable_lines_from_messages(messages)
    fallback_parts = [existing] if existing else [f"Conversation session {archive.get('session_id', 'unknown')}"]
    if durable:
        fallback_parts.append("Important durable facts and decisions:\n" + "\n".join(f"- {line}" for line in durable))
    summary = "\n\n".join(part for part in fallback_parts if part).strip()
    max_chars = summary_tokens_to_chars(target_tokens)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def upsert_memory_compaction_metadata(
    session_id: str,
    archive_path: Path,
    original_size: int,
    compressed_size: int,
    summary_id: str,
    compression: str,
    timestamp: str | None = None,
) -> None:
    when = timestamp or now_iso()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO memory_compaction (
                session_id, archive_path, original_size, compressed_size, summary_id,
                summary_path, last_accessed_at, compacted_at, compression
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                archive_path = excluded.archive_path,
                original_size = excluded.original_size,
                compressed_size = excluded.compressed_size,
                summary_id = excluded.summary_id,
                summary_path = excluded.summary_path,
                compacted_at = excluded.compacted_at,
                compression = excluded.compression
            """,
            (
                session_id,
                str(archive_path),
                original_size,
                compressed_size,
                summary_id,
                f"episodic_memory:{summary_id}",
                when,
                when,
                compression,
            ),
        )
        conn.commit()


def touch_memory_archive(session_id: str) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE memory_compaction SET last_accessed_at = ? WHERE session_id = ?",
            (now_iso(), session_id),
        )
        conn.commit()


def compact_session_archive(path: Path, compression: str, target_tokens: int) -> dict[str, Any] | None:
    archive = normalize_session_archive(path)
    messages = archive.get("messages") if isinstance(archive.get("messages"), list) else []
    if not messages:
        return None
    original_size = path.stat().st_size
    summary = compact_session_summary(archive, target_tokens)
    summary_id = find_or_store_session_summary(archive["session_id"], summary)
    durable = durable_lines_from_messages(messages)
    payload = {
        "session_id": archive["session_id"],
        "type": archive.get("type", "conversation_thread"),
        "title": archive.get("title") or derive_session_title(messages, archive["session_id"]),
        "summary": summary,
        "messages": [],
        "durable_facts": durable,
        "created_at": archive.get("created_at"),
        "updated_at": archive.get("updated_at"),
        "compacted": True,
        "compacted_at": now_iso(),
        "summary_id": summary_id,
    }
    if compression == "zlib":
        raw = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        payload["raw_messages_compressed"] = base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")
        payload["raw_messages_compression"] = "zlib"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    compressed_size = path.stat().st_size
    upsert_memory_compaction_metadata(
        archive["session_id"], path, original_size, compressed_size, summary_id, compression, payload["compacted_at"]
    )
    return {
        "session_id": archive["session_id"],
        "original_size": original_size,
        "compressed_size": compressed_size,
        "summary_id": summary_id,
    }


def enforce_memory_budget() -> dict[str, Any]:
    recent_days = memory_setting_int("memory_recent_days")
    max_raw_bytes = int(memory_setting_float("memory_max_raw_archive_mb") * 1024 * 1024)
    target_tokens = memory_setting_int("memory_summary_target_tokens", minimum=1)
    compression = str(load_settings().get("memory_cold_archive_compression", "zlib")).lower()
    if compression not in {"zlib", "remove"}:
        compression = "zlib"
    now = datetime.now(timezone.utc)
    archives: list[dict[str, Any]] = []
    for path in sorted(ARCHIVE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            archive = normalize_session_archive(path)
            updated_at = parse_archive_time(archive.get("updated_at"))
            is_recent = (now - updated_at).days < recent_days
            is_raw = bool(payload.get("messages"))
            size = archive_raw_size(path) if is_raw else 0
            archives.append({"path": path, "archive": archive, "updated_at": updated_at, "recent": is_recent, "raw_size": size})
        except Exception as exc:
            print(f"WARNING: skipping archive during memory budget enforcement: {path}: {exc}")
    raw_total = sum(item["raw_size"] for item in archives)
    compacted: list[dict[str, Any]] = []
    candidates = sorted(
        (item for item in archives if not item["recent"] and item["raw_size"] > 0),
        key=lambda item: item["updated_at"],
    )
    for item in candidates:
        if raw_total <= max_raw_bytes:
            break
        result = compact_session_archive(item["path"], compression, target_tokens)
        if result:
            compacted.append(result)
            raw_total -= item["raw_size"]
    return {
        "status": "ok",
        "raw_bytes": raw_total,
        "max_raw_bytes": max_raw_bytes,
        "recent_days": recent_days,
        "compacted": compacted,
        "compacted_count": len(compacted),
    }

def normalize_session_archive(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    session_id = str(payload.get("session_id") or path.stem)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not messages and payload.get("raw_messages_compression") == "zlib" and payload.get("raw_messages_compressed"):
        try:
            raw_messages = zlib.decompress(base64.b64decode(payload["raw_messages_compressed"])).decode("utf-8")
            loaded_messages = json.loads(raw_messages)
            if isinstance(loaded_messages, list):
                messages = loaded_messages
        except Exception:
            messages = []
    updated_at = archive_updated_at(path, payload)
    title = payload.get("title") or derive_session_title(messages, session_id)
    return {
        "session_id": session_id,
        "type": payload.get("type") or "conversation_thread",
        "title": title,
        "summary": payload.get("summary", ""),
        "messages": messages,
        "created_at": payload.get("created_at") or updated_at,
        "updated_at": updated_at,
    }


def write_session_archive(
    session_id: str,
    messages: list[dict[str, str]],
    summary: str = "",
    title: str = "",
) -> dict[str, Any]:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = session_archive_path(session_id)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = normalize_session_archive(path)
        except Exception:
            existing = {}
    timestamp = now_iso()
    payload = {
        "session_id": safe_session_id(session_id),
        "type": "conversation_thread",
        "title": title or existing.get("title") or derive_session_title(messages, session_id),
        "summary": summary or existing.get("summary", ""),
        "messages": messages,
        "created_at": existing.get("created_at") or timestamp,
        "updated_at": timestamp,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


@app.post("/memory/save")
async def save_memory(request: MemorySaveRequest) -> JSONResponse:
    try:
        print(f"DEBUG: Saving session {request.session_id} with {len(request.messages)} messages")
        messages_dict = [m.model_dump() for m in request.messages]
        result = await summarize_and_store(messages_dict, request.session_id)
        if result.get("status") == "error":
            print(f"DEBUG: Summarization error: {result.get('message')}")
        return JSONResponse(result)
    except Exception as e:
        print(f"DEBUG: Save memory endpoint exception: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/memory/compact")
async def compact_memory(request: MemoryCompactRequest) -> JSONResponse:
    if request.cutoff_days < 0:
        raise HTTPException(status_code=400, detail="cutoff_days must be non-negative.")
    if request.limit is not None and request.limit < 1:
        raise HTTPException(status_code=400, detail="limit must be positive when provided.")
    return JSONResponse(compact_memory_archives(cutoff_days=request.cutoff_days, limit=request.limit))


@app.post("/memory/recall")
async def recall_memory(request: MemoryRecallRequest) -> JSONResponse:
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required.")
    limit = max(1, min(request.limit, 25))
    memories = recall_memories(query, limit=limit, include_raw=request.include_raw)
    return JSONResponse({"query": query, "memories": memories})


@app.get("/")
async def index() -> FileResponse:
    from starlette.responses import FileResponse as FR
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    return FR(BASE_DIR / "index.html", headers=headers)


@app.get("/tools")
async def get_tools() -> JSONResponse:
    return JSONResponse(load_registry())


@app.get("/memory")
async def get_memory() -> JSONResponse:
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, keywords, summary, session_id FROM episodic_memory ORDER BY created_at DESC"
        ).fetchall()
    memories = []
    for row in rows:
        memory = dict(row)
        memory["type"] = "memory_snippet"
        memory["title"] = "Episodic memory"
        memories.append(memory)
    return JSONResponse({"memories": memories})


@app.get("/sessions")
async def get_sessions() -> JSONResponse:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    sessions: list[dict[str, Any]] = []
    for path in ARCHIVE_DIR.glob("*.json"):
        try:
            archive = normalize_session_archive(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        sessions.append({key: archive[key] for key in ("session_id", "type", "title", "summary", "created_at", "updated_at")})
    sessions.sort(key=lambda item: item["updated_at"], reverse=True)
    return JSONResponse({"sessions": sessions})


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    path = session_archive_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found.")
    try:
        archive = normalize_session_archive(path)
        touch_memory_archive(archive["session_id"])
        return JSONResponse(archive)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/sessions/{session_id}")
async def put_session(session_id: str, request: SessionUpdateRequest) -> JSONResponse:
    messages = [message.model_dump() for message in request.messages]
    archive = write_session_archive(session_id, messages, summary=request.summary, title=request.title)
    return JSONResponse(archive)


@app.get("/settings")
async def get_settings() -> JSONResponse:
    return JSONResponse(provider_status())


@app.post("/settings")
async def update_settings(update: SettingsUpdate) -> JSONResponse:
    settings = load_settings()
    if update.default_provider != "auto":
        provider_ids = set(settings["providers"].keys())
        if update.default_provider not in provider_ids:
            raise HTTPException(status_code=400, detail="Unknown provider.")
        provider_models = settings["providers"][update.default_provider].get("models") or MODEL_OPTIONS.get(update.default_provider, [])
        if update.default_model not in provider_models:
            raise HTTPException(status_code=400, detail="Unknown model for provider.")

    settings["default_provider"] = update.default_provider
    settings["default_model"] = update.default_model
    if update.provider_priority:
        settings["provider_priority"] = [p for p in update.provider_priority if p in settings["providers"]]
    if update.runtime_settings and update.default_provider in settings.get("providers", {}):
        settings["providers"][update.default_provider]["runtime_settings"] = normalize_runtime_settings(update.runtime_settings)
    for provider_id, api_key in update.provider_keys.items():
        if not api_key:
            continue
        provider = settings["providers"].get(provider_id)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_id}")
        key_name = provider.get("api_key_env")
        if key_name:
            write_env_value(key_name, api_key)
    for provider_id, keys in update.provider_api_keys.items():
        if provider_id in settings.get("providers", {}):
            settings["providers"][provider_id]["api_keys"] = keys
    save_settings(settings)
    return JSONResponse(provider_status())


@app.get("/rules")
async def get_rules() -> JSONResponse:
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    rules: dict[str, str] = {}
    for path in sorted(RULES_DIR.glob("**/*.md")):
        rel = str(path.relative_to(RULES_DIR).with_suffix(""))
        rules[rel] = path.read_text(encoding="utf-8")
    return JSONResponse({"rules": rules, "rules_dir": str(RULES_DIR)})


class RuleUpdate(BaseModel):
    path: str
    content: str


@app.post("/rules")
async def save_rule(update: RuleUpdate) -> JSONResponse:
    rel = Path(update.path).with_suffix(".md")
    if ".." in str(rel):
        raise HTTPException(status_code=400, detail="Invalid rule path.")
    target = RULES_DIR / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(update.content, encoding="utf-8")
    return JSONResponse({"path": str(rel), "saved": True})


@app.post("/providers")
async def save_provider(update: ProviderUpdate) -> JSONResponse:
    try:
        return JSONResponse(upsert_provider(update))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> JSONResponse:
    normalized = normalize_provider_id(provider_id)
    settings = load_settings()
    if normalized not in settings["providers"]:
        raise HTTPException(status_code=404, detail="Provider not found.")
    del settings["providers"][normalized]
    if settings.get("provider_priority") and normalized in settings["provider_priority"]:
        settings["provider_priority"] = [p for p in settings["provider_priority"] if p != normalized]
    if settings.get("default_provider") == normalized:
        settings["default_provider"] = list(settings["providers"].keys())[0] if settings["providers"] else "auto"
    save_settings(settings)
    return JSONResponse({"deleted": normalized, "settings": provider_status()})


@app.post("/providers/{provider_id}/discover")
async def discover_provider_models(provider_id: str) -> JSONResponse:
    settings = load_settings()
    normalized = normalize_provider_id(provider_id)
    if normalized not in settings["providers"]:
        raise HTTPException(status_code=404, detail="Provider not found.")
    try:
        models = await discover_models_for_provider(normalized)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Model discovery failed: {exc}") from exc
    if models:
        settings["providers"][normalized]["models"] = models
        if settings.get("default_provider") == normalized and settings.get("default_model") not in models:
            settings["default_model"] = models[0]
        save_settings(settings)
    return JSONResponse({"provider": normalized, "models": models, "settings": provider_status()})


@app.get("/models/local")
async def get_local_models() -> JSONResponse:
    return JSONResponse({"roots": [str(path) for path in likely_model_roots()], "models": scan_local_model_files()})


@app.post("/models/local/scan")
async def scan_local_models(request: LocalModelScanRequest) -> JSONResponse:
    return JSONResponse(
        {
            "roots": request.roots or [str(path) for path in likely_model_roots()],
            "models": scan_local_model_files(request.roots, request.max_results),
        }
    )


@app.post("/workspace-index/scan")
async def scan_workspace_index_endpoint(request: WorkspaceIndexScanRequest) -> JSONResponse:
    result = await scan_workspace_index(request.roots, request.task_id, request.max_files)
    return JSONResponse(result)


@app.get("/workspace-index")
async def get_workspace_index(q: str = "", limit: int = 50) -> JSONResponse:
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT path, kind, size, mtime, hash, summary, last_seen_task_id FROM workspace_index ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    entries = [dict(row) for row in rows]
    return JSONResponse({"entries": entries, "context": retrieve_workspace_index_context(q, limit=min(limit, 20))})


@app.post("/approve")
async def approve(decision: ApprovalDecision) -> JSONResponse:
    pending = app.state.pending_approvals.pop(decision.request_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    pending.approved = True
    pending.event.set()
    return JSONResponse({"ok": True})


@app.post("/reject")
async def reject(decision: ApprovalDecision) -> JSONResponse:
    pending = app.state.pending_approvals.pop(decision.request_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    pending.approved = False
    pending.event.set()
    return JSONResponse({"ok": True})


@app.post("/checkpoints/rollback")
async def rollback_checkpoint_endpoint() -> JSONResponse:
    result = KERNEL.rollback_latest_checkpoint(caller="policy")
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(result)


@app.post("/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    raw = await file.read()
    name = file.filename or "upload"
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz

            doc = fitz.open(stream=raw, filetype="pdf")
            content = "\n".join(page.get_text() for page in doc)
            return JSONResponse({"type": "pdf", "name": name, "content": content})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse PDF: {exc}") from exc
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        encoded = base64.b64encode(raw).decode("ascii")
        return JSONResponse({"type": "image", "name": name, "content": encoded})
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Only PDF, image, and UTF-8 text uploads are supported.") from exc
    return JSONResponse({"type": "text", "name": name, "content": content})



@app.get("/memory/{session_id}")
async def get_session_history(session_id: str) -> JSONResponse:
    path = session_archive_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found.")
    try:
        archive = normalize_session_archive(path)
        touch_memory_archive(archive["session_id"])
        return JSONResponse(archive)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def prune_conversation(messages: list[dict[str, str]], max_chars: int = 32000) -> list[dict[str, str]]:
    total_chars = sum(len(m["content"]) for m in messages)
    if total_chars <= max_chars:
        return messages
    
    print(f"DEBUG: Pruning conversation. Current size: {total_chars} chars. Target: {max_chars}")
    system_msg = messages[0] if messages and messages[0]["role"] == "system" else None
    
    # Keep system prompt and last 5 messages
    keep_last = 5
    if len(messages) <= keep_last + 1:
        # Just truncate the middle if we have very few but very large messages
        return messages
        
    head = [system_msg] if system_msg else []
    tail = messages[-keep_last:]
    middle = messages[1:-keep_last] if system_msg else messages[:-keep_last]
    
    pruned_middle = []
    for m in middle:
        content = m["content"]
        if m["role"] == "tool":
            # Aggressively truncate old tool results
            if len(content) > 500:
                content = content[:500] + "... [Old tool result pruned]"
        elif m["role"] in ["user", "assistant"]:
            if len(content) > 2000:
                content = content[:2000] + "... [Old message truncated]"
        pruned_middle.append({"role": m["role"], "content": content})
        
    return head + pruned_middle + tail


def parse_iso_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    if not value:
        return fallback or datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@app.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        session_id = str(uuid.uuid4())
        conversation: list[dict[str, str]] = [
            {"role": "system", "content": build_system_prompt(request)},
        ]
        sanitized_messages: list[dict[str, str]] = []
        for msg in request.messages:
            content = msg.content[:12000] + ("... [Message truncated in history]" if len(msg.content) > 12000 else "")
            sanitized_messages.append({"role": msg.role, "content": content})
        conversation.extend(sanitized_messages)
        public_messages = sanitized_messages.copy()

        model = request.model or load_settings().get("default_model", "openai-fast")
        provider = request.provider or load_settings().get("default_provider", "pollinations")

        goal = last_user_message(request.messages)
        task_state = load_task_state(DATA_DIR, request.task_id) if request.task_id else None
        if task_state is None:
            task_state = TaskState(task_id=request.task_id or str(uuid.uuid4()), goal=goal)
        elif goal:
            task_state.goal = goal
        save_task_state(task_state, DATA_DIR)

        yield sse("meta", {"session_id": session_id, "model": model, "task_id": task_state.task_id})

        max_turns = max(1, int(request.max_task_steps or 12))
        repair_attempts_by_tool: dict[str, int] = {}
        max_repair_attempts = tool_repair_max_attempts()

        try:
            user_msg = last_user_message(request.messages)
            if len(user_msg) > 25:
                disc_code = "import os, platform, sys; print('OS:', platform.system(), platform.release()); print('Python:', sys.version); print('CWD:', os.getcwd()); print('Files:', [x for x in os.listdir('.') if not x.startswith('.')][:15])"
                disc_result, _ = await dispatch_task_tool("execute_python", {"code": disc_code}, policy_approved=True)
                yield sse("tool_result", {"tool": "execute_python", "result": disc_result})
                disc_str = json.dumps(disc_result)
                if len(disc_str) > 3000:
                    disc_str = disc_str[:3000] + "... [truncated]"
                conversation.append({"role": "tool", "content": "[environment auto-discovered]\n" + disc_str})

                proj_name: str | None = None
                for kw in ["dans ", "dans le projet ", "sur le projet ", "pour le projet ", "dans l'entreprise ", "dans "]:
                    idx = user_msg.lower().find(kw)
                    if idx >= 0:
                        after = user_msg[idx + len(kw):].strip().split()[0].strip(".,!?;:'\"")
                        if after:
                            proj_name = after
                            break
                if proj_name:
                    try:
                        import json as _json
                        wf = Path.home() / ".ouroboros" / "workspace.json"
                        if wf.exists():
                            ws = _json.loads(wf.read_text(encoding="utf-8"))
                            project = ws.get("projects", {}).get(proj_name)
                            if project:
                                conv_ctx = f"[project context auto-detected: {proj_name}]\nroot: {project.get('root', 'unknown')}"
                                conversation.append({"role": "tool", "content": conv_ctx})
                    except Exception:
                        pass

            for _ in range(max_turns):
                text = ""
                async for token in app.state.model_adapter.complete(prune_conversation(conversation), model, provider):
                    text += token
                    yield sse("delta", {"content": token})

                conversation.append({"role": "assistant", "content": text})
                display_text = strip_tool_calls(text).strip()
                public_messages.append({"role": "assistant", "content": display_text})
                tool_calls = extract_tool_calls(text)

                if not tool_calls:
                    break

                restart_turn = False
                for call in tool_calls:
                    tool_name = call.get("tool") or call.get("name", "")
                    arguments = call.get("arguments", {})

                    tool = find_tool(tool_name)
                    policy = summarize_python_execution_policy(str(arguments.get("code", ""))) if tool_name == "execute_python" else None
                    policy_approved = request.auto_approve

                    if not request.auto_approve:
                        risk = policy.get("risk_summary", f"Tool: {tool_name}") if policy else f"Tool: {tool_name}"
                        pending = PendingApproval(
                            tool_name=tool_name,
                            arguments=arguments,
                            risk_summary=risk,
                            policy_reasons=policy.get("reasons", []) if policy else [],
                            sandbox_tier=policy.get("sandbox_tier", "read_only") if policy else "read_only",
                        )
                        app.state.pending_approvals[pending.approval_id] = pending
                        yield sse("approval_request", pending.model_dump())
                        await pending.event.wait()
                        policy_approved = pending.approved

                    result, _ = await dispatch_task_tool(tool_name, arguments, policy_approved=policy_approved)
                    yield sse("tool_result", {"tool": tool_name, "result": result})

                    step = task_state.add_step(tool_name, arguments)
                    observation = task_state.add_observation(step, result, True)
                    save_task_state(task_state, DATA_DIR)

                    result_str = json.dumps(result)
                    if len(result_str) > 10000:
                        result_str = result_str[:10000] + "... [Result truncated for context]"
                    conversation.append({"role": "tool", "content": result_str})

                    if tool is not None and not tool.get("builtin") and registered_tool_failed(result, tool):
                        attempt_number = repair_attempts_by_tool.get(tool_name, 0) + 1
                        if attempt_number <= max_repair_attempts:
                            repair_attempts_by_tool[tool_name] = attempt_number
                            repair_attempt = append_tool_repair_attempt(tool, arguments, result)
                            conversation.append({"role": "system", "content": build_tool_repair_message(tool, arguments, result, attempt_number, max_repair_attempts)})
                            yield sse("tool_repair", {"tool_name": tool_name, "attempt": attempt_number, "max_attempts": max_repair_attempts, "failure": repair_attempt.get("failure")})
                            restart_turn = True
                            break

                if restart_turn:
                    continue

                evaluation = await evaluate_tool_result(conversation, session_id, model, provider)
                if evaluation.get("error"):
                    yield sse("error", {"message": f"Evaluation: {evaluation['error']}"})
                yield sse("evaluation", evaluation)
                if evaluation["decision"] == "rollback":
                    rollback = KERNEL.rollback_latest_checkpoint(caller="eval")
                    yield sse("kernel_safety_action", {"name": "rollback_latest_checkpoint", "result": rollback})
                    conversation.append({"role": "tool", "content": json.dumps({"kernel_safety_action": rollback})})
                if evaluation["decision"] == "final":
                    final_text = ""
                    async for token in app.state.model_adapter.complete(prune_conversation(conversation), model, provider):
                        final_text += token
                        yield sse("delta", {"content": token})
                    if final_text.strip():
                        conversation.append({"role": "assistant", "content": final_text})
                        display_text = strip_tool_calls(final_text).strip()
                        public_messages.append({"role": "assistant", "content": display_text})
                    break

            task_state.mark_phase("final")
            task_state.done = True
            task_state.artifacts.setdefault("final_answer", strip_tool_calls(conversation[-1]["content"]) if conversation else "")
            save_task_state(task_state, DATA_DIR)
            yield sse("done", {"session_id": session_id, "task_id": task_state.task_id})
        except Exception as exc:
            yield sse("error", {"message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")

def next_due_at(current_due_at: str, rule: sqlite3.Row | dict[str, Any] | None) -> str | None:
    if rule is None:
        return None
    frequency = str(rule["frequency"]).lower()
    interval = max(1, int(rule["interval"] or 1))
    current = parse_iso_datetime(current_due_at)
    if frequency in {"once", "none"}:
        return None
    if frequency in {"minute", "minutes"}:
        candidate = current + timedelta(minutes=interval)
    elif frequency in {"hour", "hours", "hourly"}:
        candidate = current + timedelta(hours=interval)
    elif frequency in {"day", "days", "daily"}:
        candidate = current + timedelta(days=interval)
    elif frequency in {"week", "weeks", "weekly"}:
        candidate = current + timedelta(weeks=interval)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported recurrence frequency: {frequency}")
    end_at = rule["end_at"] if isinstance(rule, sqlite3.Row) else rule.get("end_at")
    if end_at and candidate > parse_iso_datetime(str(end_at)):
        return None
    return candidate.isoformat()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


async def dispatch_task_tool(tool_name: str, arguments: dict[str, Any], policy_approved: bool) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if tool_name == "execute_python":
        result = await execute_python(str(arguments.get("code", "")), policy_approved=policy_approved)
        store_tool_execution(tool_name, arguments, result, policy_approved)
        return result, None
    if tool_name == "rollback_latest_checkpoint":
        result = rollback_latest_checkpoint()
        store_tool_execution(tool_name, arguments, result, policy_approved)
        return result, None
    if tool_name == "register_tool":
        result = register_tool(
            name=str(arguments.get("name", "")),
            description=str(arguments.get("description", "")),
            parameters_schema=arguments.get("parameters_schema", {}),
            filepath=str(arguments.get("filepath", "")),
            requires_approval=bool(arguments.get("requires_approval", False)),
            version=str(arguments.get("version", "1.0.0")),
            source_task_id=arguments.get("source_task_id"),
            test_command=arguments.get("test_command"),
            test_plan=arguments.get("test_plan"),
            sample_arguments=arguments.get("sample_arguments"),
            supersedes=arguments.get("supersedes"),
        )
        store_tool_execution(tool_name, arguments, result, policy_approved)
        return result, None
    tool = find_tool(tool_name)
    if tool is not None and tool.get("builtin"):
        result = {"error": f"Builtin tool {tool_name} is not implemented in dispatch loop."}
        store_tool_execution(tool_name, arguments, result, policy_approved)
        return result, tool
    if tool is not None:
        result = run_registered_tool(tool, arguments)
        update_registered_tool_status(tool_name, str(tool.get("version", "")), increment_use_count=True)
        store_tool_execution(tool_name, arguments, result, policy_approved)
        return result, tool
    
    result = {"error": f"Tool {tool_name} not found."}
    store_tool_execution(tool_name, arguments, result, policy_approved)
    return result, None


async def task_state_machine_events(request: ChatRequest, session_id: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    goal = last_user_message(request.messages)
    task_state = load_task_state(DATA_DIR, request.task_id) if request.task_id else None
    if task_state is None:
        task_state = TaskState(task_id=request.task_id or str(uuid.uuid4()), goal=goal)
    elif goal:
        task_state.goal = goal
    save_task_state(task_state, DATA_DIR)

    def phase_payload(phase: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        task_state.mark_phase(phase)
        save_task_state(task_state, DATA_DIR)
        event_payload: dict[str, Any] = {"task_id": task_state.task_id, "phase": task_state.phase, "done": task_state.done}
        if payload:
            event_payload.update(payload)
        return event_payload

    sanitized_messages = []
    for msg in request.messages:
        content = msg.content[:12000] + "... [Message truncated in history]" if len(msg.content) > 12000 else msg.content
        sanitized_messages.append({"role": msg.role, "content": content})

    conversation: list[dict[str, str]] = [{"role": "system", "content": build_system_prompt(request)}]
    conversation.extend(sanitized_messages)
    yield "meta", {"session_id": session_id, "model": request.model, "task_id": task_state.task_id}
    yield task_event_name("plan"), phase_payload("plan", {"goal": task_state.goal, "plan": task_state.plan})

    max_task_steps = max(1, int(request.max_task_steps or 12))
    model_turns = 0
    while not task_state.done and model_turns < max_task_steps:
        model_turns += 1
        if task_state.observations:
            yield task_event_name("revise"), phase_payload("revise", {"observations": task_state.observations[-3:]})
        text = ""
        provider = request.provider or load_settings().get("default_provider", "pollinations")
        model = request.model or load_settings().get("default_model", "openai-fast")
        async for token in app.state.model_adapter.complete(prune_conversation(conversation), model, provider):
            text += token
            yield "delta", {"content": token}
        display_text = strip_tool_calls(text).strip()
        calls = extract_tool_calls(text)
        if not task_state.steps:
            task_state.add_plan(display_text or text)
            save_task_state(task_state, DATA_DIR)
            yield task_event_name("plan"), phase_payload("plan", {"goal": task_state.goal, "plan": task_state.plan})
        if not calls and is_capability_refusal(text):
            task_state.failure_count += 1
            save_task_state(task_state, DATA_DIR)
            yield "assistant_replace", {"content": "Retrying under the self-evolution protocol."}
            yield task_event_name("revise"), phase_payload("revise", {"reason": "capability_refusal", "failure_count": task_state.failure_count})
            conversation.append({"role": "assistant", "content": text})
            conversation.append({"role": "system", "content": build_self_evolution_retry_message(request)})
            continue
        if display_text != text.strip():
            yield "assistant_replace", {"content": display_text}
        conversation.append({"role": "assistant", "content": text})
        if not calls:
            task_state.done = True
            task_state.artifacts["final_answer"] = display_text or text
            save_task_state(task_state, DATA_DIR)
            yield task_event_name("final"), phase_payload("final", {"answer": display_text or text})
            break
        yield task_event_name("act"), phase_payload("act", {"tool_call_count": len(calls)})
        for call in calls:
            tool_name = str(call.get("name", ""))
            arguments = call.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            step = task_state.add_step(tool_name, arguments)
            save_task_state(task_state, DATA_DIR)
            yield task_event_name("act"), phase_payload("act", {"step": step})
            policy_approved = True
            if tool_name == "execute_python":
                policy = summarize_python_execution_policy(str(arguments.get("code", "")))
                if policy["action"] == "block":
                    result = {"error": policy["risk_summary"], "policy": policy}
                    store_tool_execution(tool_name, arguments, result, False)
                    observation = task_state.add_observation(step, result, False)
                    save_task_state(task_state, DATA_DIR)
                    yield "tool_result", {"tool_name": tool_name, "result": result, "approved": False}
                    yield task_event_name("observe"), phase_payload("observe", {"observation": observation})
                    conversation.append({"role": "tool", "content": json.dumps(result)})
                    continue
                policy_approved = request.auto_approve and not policy.get("manual_approval_required", False)
                if policy["action"] == "require_approval" and not policy_approved:
                    result = {"error": "Python execution requires approval by policy.", "policy": policy}
                    store_tool_execution(tool_name, arguments, result, False)
                    observation = task_state.add_observation(step, result, False)
                    save_task_state(task_state, DATA_DIR)
                    yield "approval_required", {"tool_name": tool_name, "arguments": arguments, "policy": policy}
                    yield task_event_name("observe"), phase_payload("observe", {"observation": observation})
                    conversation.append({"role": "tool", "content": json.dumps(result)})
                    continue
            yield "tool_call", {"tool_name": tool_name, "arguments": arguments}
            result, tool = await dispatch_task_tool(tool_name, arguments, policy_approved)
            store_tool_execution(tool_name, arguments, result, True)
            observation = task_state.add_observation(step, result, True)
            save_task_state(task_state, DATA_DIR)
            yield "tool_result", {"tool_name": tool_name, "result": result, "approved": True}
            yield task_event_name("observe"), phase_payload("observe", {"observation": observation})
            result_str = json.dumps(result)
            if len(result_str) > 10000:
                result_str = result_str[:10000] + "... [Result truncated for context]"
            conversation.append({"role": "tool", "content": result_str})
            if tool is not None and not tool.get("builtin") and registered_tool_failed(result, tool):
                conversation.append({"role": "system", "content": build_tool_repair_message(tool, arguments, result, 1, tool_repair_max_attempts())})
                break
        yield task_event_name("evaluate"), phase_payload("evaluate", {"step_count": len(task_state.steps), "observation_count": len(task_state.observations)})
        evaluation = await evaluate_tool_result(conversation, session_id, model, provider)
        yield "evaluation", evaluation

    if not task_state.done:
        task_state.failure_count += 1
        task_state.done = True
        task_state.artifacts["final_answer"] = "Task stopped after reaching the configured step limit."
        save_task_state(task_state, DATA_DIR)
        yield task_event_name("final"), phase_payload("final", {"answer": task_state.artifacts["final_answer"], "reason": "step_limit"})
    yield "done", {"session_id": session_id, "task_id": task_state.task_id}


@app.post("/goals")
async def create_goal(request: GoalCreateRequest) -> JSONResponse:
    goal_id = str(uuid.uuid4())
    timestamp = now_iso()
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO goal (id, title, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (goal_id, request.title, request.description, request.status, timestamp, timestamp),
        )
        conn.commit()
    return JSONResponse({"goal": {"id": goal_id, "title": request.title, "description": request.description, "status": request.status, "created_at": timestamp, "updated_at": timestamp}})


@app.get("/multiverse")
async def get_multiverse() -> JSONResponse:
    tree: dict[str, Any] = {"title": "The One (Ouroboros)", "level": "the_one", "status": "root", "children": []}
    universes: dict[str, dict[str, Any]] = {}

    def get_univ(name: str) -> dict[str, Any]:
        if name not in universes:
            universes[name] = {"title": name, "level": "universe_god", "status": "active", "children": [], "source": "auto"}
        return universes[name]

    def get_galaxy(univ: dict[str, Any], name: str) -> dict[str, Any]:
        for g in univ.get("children", []):
            if g.get("title") == name and g.get("level") == "galaxy_demigod":
                return g
        gn: dict[str, Any] = {"title": name, "level": "galaxy_demigod", "status": "active", "children": [], "source": "group"}
        univ.setdefault("children", []).append(gn)
        return gn

    def make_tree(val: Any, depth: int = 0) -> list[dict[str, Any]]:
        levels = ["country_president", "region_governor", "city_minister", "district_chief", "section_chief", "family_head", "house_chief", "individual"]
        if depth >= len(levels):
            return []
        label = str(val)[:80]
        node: dict[str, Any] = {"title": label, "level": levels[depth], "status": "info", "children": [], "source": "auto"}
        if isinstance(val, dict):
            for k, v in list(val.items())[:4]:
                child = {"title": f"{k}: {str(v)[:60]}", "level": levels[min(depth+1, len(levels)-1)], "status": "info", "children": make_tree(v, depth+2), "source": "key"}
                node["children"].append(child)
        elif isinstance(val, list):
            for i, item in enumerate(val[:4]):
                node["children"].append({"title": f"[{i}]: {str(item)[:60]}", "level": levels[min(depth+1, len(levels)-1)], "status": "info", "children": make_tree(item, depth+2), "source": "list"})
        return [node]

    from glob import glob as _glob
    from collections import defaultdict
    session_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for apath in sorted(ARCHIVE_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            arch = json.loads(apath.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = arch.get("session_id", apath.stem)
        title = arch.get("title") or arch.get("summary", "") or sid
        summary = (arch.get("summary") or "")[:80]
        first_msg = ""
        msgs = arch.get("messages", [])
        if msgs:
            first_msg = str(msgs[0].get("content", ""))[:80] if isinstance(msgs[0], dict) else str(msgs[0])[:80]
        session_title = summary or first_msg or title
        sys_node: dict[str, Any] = {
            "title": session_title, "level": "system_god", "status": "archived",
            "children": [], "source": "session", "task_id": sid,
        }
        for m in msgs[-5:]:
            role = m.get("role", "?") if isinstance(m, dict) else "?"
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            wm = {"title": f"[{role}] {str(content)[:80]}", "level": "world_monarch", "status": "info", "children": [], "source": "message"}
            if isinstance(content, str):
                for line in content.split("\n")[:3]:
                    if line.strip():
                        wm["children"].append({"title": line.strip()[:100], "level": "continent_emperor", "status": "info", "children": [], "source": "line"})
            sys_node["children"].append(wm)
        words = session_title.split()[:3]
        group_key = " ".join(words) if words else "Other"
        session_groups[group_key].append(sys_node)

    for group_key, sessions in session_groups.items():
        u = get_univ("General")
        g = get_galaxy(u, group_key[:40])
        for s in sessions:
            g["children"].append(s)

    tasks_dir = DATA_DIR / "tasks"
    if tasks_dir.exists():
        for tpath in sorted(tasks_dir.glob("*.json"), reverse=True)[:50]:
            try:
                state = json.loads(tpath.read_text(encoding="utf-8"))
            except Exception:
                continue
            goal_txt = state.get("goal", "General")[:40]
            task_id = state.get("task_id", tpath.stem)
            sys_node: dict[str, Any] = {
                "title": goal_txt, "level": "system_god", "status": "done" if state.get("done") else "running",
                "children": [], "source": "task", "task_id": task_id,
            }
            for obs in state.get("observations", [])[-8:]:
                tool_name = obs.get("tool_name", "tool")
                wn: dict[str, Any] = {"title": tool_name, "level": "world_monarch", "status": "completed" if obs.get("approved") else "failed", "children": [], "source": "observation"}
                res = obs.get("result", {})
                if isinstance(res, dict):
                    for k, v in list(res.items())[:6]:
                        wn["children"] += make_tree(v)
                sys_node["children"].append(wn)
            universe_name = "General"
            u = get_univ(universe_name)
            g = get_galaxy(u, goal_txt)
            g["children"].append(sys_node)

    subagents_dir = DATA_DIR / "subagents"
    if subagents_dir.exists():
        for spath in sorted(subagents_dir.glob("*.json"), reverse=True)[:20]:
            try:
                run = json.loads(spath.read_text(encoding="utf-8"))
            except Exception:
                continue
            spec = run.get("spec", {})
            wn = {"title": spec.get("goal", "subtask")[:60], "level": "world_monarch", "status": "done" if run.get("result") else "running", "children": [], "source": "subagent", "run_id": run.get("run_id")}
            for d in run.get("denied_tool_calls", []):
                country_node: dict[str, Any] = {"title": f"denied: {d.get('tool_name')}", "level": "country_president", "status": "error", "children": [], "source": "denial"}
                for sub_i in range(2):
                    val_str = str(d)[sub_i*100:(sub_i+1)*100]
                    if val_str.strip():
                        region_node: dict[str, Any] = {"title": val_str.strip()[:80], "level": "region_governor", "status": "info", "children": [], "source": "partial"}
                        ind_node: dict[str, Any] = {"title": str(d)[:40], "level": "individual", "status": "info", "children": [], "source": "final"}
                        region_node["children"].append(ind_node)
                        country_node["children"].append(region_node)
                wn["children"].append(country_node)
            universe_name = "General"
            u = get_univ(universe_name)
            g = get_galaxy(u, spec.get("goal", "subtask")[:40])
            g["children"].append(wn)

    if not universes:
        universes["General"] = {"title": "General", "level": "universe_god", "status": "idle", "children": [], "source": "default"}
    for u in sorted(universes.keys()):
        tree["children"].append(universes[u])

    arts_dir = Path.home() / ".ouroboros" / "artifacts"
    if arts_dir.exists():
        art_list = []
        for af in sorted(arts_dir.glob("*"), reverse=True)[:10]:
            art_list.append({"title": af.name, "level": "world_monarch", "status": "artifact", "source": "artifact"})
        if art_list:
            tree["children"].append({"title": "Artefacts", "level": "universe_god", "status": "active", "children": art_list, "source": "artifacts"})

    return JSONResponse(tree)


@app.get("/goals")
async def list_goals() -> JSONResponse:
    with connect_db() as conn:
        rows = conn.execute("SELECT id, title, description, status, created_at, updated_at FROM goal ORDER BY created_at DESC").fetchall()
    return JSONResponse({"goals": [row_to_dict(row) for row in rows]})


@app.post("/background-tasks")
async def create_background_task(request: BackgroundTaskCreateRequest) -> JSONResponse:
    task_id = str(uuid.uuid4())
    timestamp = now_iso()
    due_at = (parse_iso_datetime(request.due_at, datetime.now(timezone.utc))).isoformat()
    recurrence_rule_id = None
    with connect_db() as conn:
        if request.recurrence_rule is not None:
            recurrence_rule_id = str(uuid.uuid4())
            rule_start = parse_iso_datetime(request.recurrence_rule.start_at, parse_iso_datetime(due_at)).isoformat()
            conn.execute(
                "INSERT INTO recurrence_rule (id, frequency, interval, start_at, end_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (recurrence_rule_id, request.recurrence_rule.frequency, max(1, request.recurrence_rule.interval), rule_start, request.recurrence_rule.end_at, timestamp, timestamp),
            )
        conn.execute(
            """
            INSERT INTO background_task (id, goal_id, project_id, recurrence_rule_id, title, prompt, status, due_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, request.goal_id, request.project_id, recurrence_rule_id, request.title, request.prompt, "scheduled", due_at, timestamp, timestamp),
        )
        conn.commit()
    return JSONResponse({"background_task": {"id": task_id, "title": request.title, "prompt": request.prompt, "status": "scheduled", "due_at": due_at, "recurrence_rule_id": recurrence_rule_id}})


async def run_background_task(row: sqlite3.Row, request: RunDueBackgroundTasksRequest) -> dict[str, Any]:
    session_id = f"background-{row['id']}-{uuid.uuid4()}"
    task_state_id = row["task_state_id"] or str(uuid.uuid4())
    chat_request = ChatRequest(
        messages=[ChatMessage(role="user", content=row["prompt"])],
        task_id=task_state_id,
        auto_approve=True,
        max_task_steps=12,
    )
    events: list[dict[str, Any]] = []
    async for event, payload in task_state_machine_events(chat_request, session_id):
        events.append({"event": event, "data": payload})
    state = load_task_state(DATA_DIR, task_state_id)
    final_answer = (state.artifacts.get("final_answer") if state else "") or ""
    memory_summary = final_answer if final_answer else f"Background task completed: {row['title']}"
    memory_id = store_episodic_memory(session_id, memory_summary, extract_keywords(memory_summary) or ["background", "task"])
    timestamp = now_iso()
    with connect_db() as conn:
        rule = conn.execute("SELECT * FROM recurrence_rule WHERE id = ?", (row["recurrence_rule_id"],)).fetchone() if row["recurrence_rule_id"] else None
        following_due_at = next_due_at(row["due_at"], rule)
        status = "scheduled" if following_due_at else "completed"
        conn.execute(
            """
            UPDATE background_task
            SET status = ?, due_at = ?, last_run_at = ?, completed_at = ?, result_summary = ?, task_state_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, following_due_at or row["due_at"], timestamp, timestamp if status == "completed" else None, memory_summary, task_state_id, timestamp, row["id"]),
        )
        conn.execute(
            "INSERT INTO memory_link (id, memory_id, background_task_id, goal_id, project_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), memory_id, row["id"], row["goal_id"], row["project_id"], timestamp),
        )
        conn.commit()
    return {"id": row["id"], "status": status, "task_state_id": task_state_id, "memory_id": memory_id, "result_summary": memory_summary, "events": events}


@app.post("/background-tasks/run-due")
async def run_due_background_tasks(request: RunDueBackgroundTasksRequest) -> JSONResponse:
    now_value = parse_iso_datetime(request.now, datetime.now(timezone.utc)).isoformat()
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM background_task
            WHERE status IN ('scheduled', 'failed') AND due_at <= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (now_value, max(1, request.limit)),
        ).fetchall()
    results = [await run_background_task(row, request) for row in rows]
    return JSONResponse({"ran": len(results), "tasks": results})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False, app_dir=str(BASE_DIR))
