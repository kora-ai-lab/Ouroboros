from __future__ import annotations

import ast
import asyncio
import base64
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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_loop import TaskState, load_task_state, save_task_state, task_event_name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_env_files()
    ensure_layout()
    init_db()
    app.state.model_adapter = ProviderAdapter()
    app.state.pending_approvals = {}
    yield


app = FastAPI(title="Ouroboros Nucleus", lifespan=lifespan)


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
TOOLS_DIR = BASE_DIR / "tools"
KORA_DIR = BASE_DIR / "kora"
REGISTRY_PATH = BASE_DIR / "registry.json"
FACTS_PATH = BASE_DIR / "facts.json"
DB_PATH = DATA_DIR / "memory.sqlite3"
SETTINGS_PATH = BASE_DIR / "settings.json"
ENV_PATHS = [ROOT_DIR / ".env", BASE_DIR / ".env"]
POLLINATIONS_BASE_URL = "https://gen.pollinations.ai/v1"
LLAMA_CACHE: dict[str, Any] = {}
MODEL_FILE_EXTENSIONS = {".gguf", ".bin", ".safetensors", ".pt", ".pth"}
EVALUATION_DECISIONS = {"continue", "retry", "register_tool", "rollback", "final"}

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
    "default_provider": "pollinations",
    "default_model": "openai-fast",
    "providers": {
        "pollinations": {
            "label": "Pollinations",
            "type": "openai_compatible",
            "base_url": POLLINATIONS_BASE_URL,
            "api_key_env": "POLLINATIONS_API_KEY",
            "models": MODEL_OPTIONS["pollinations"],
        }
    },
}

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
        },
        {
            "name": "register_tool",
            "description": "Permanently register a new tool from a Python file you have already written and tested via execute_python.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parameters_schema": {"type": "object"},
                    "filepath": {"type": "string"},
                    "requires_approval": {"type": "boolean"},
                },
                "required": ["name", "description", "parameters_schema", "filepath"],
            },
            "builtin": True,
            "requires_approval": False,
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
You have exactly two built-in tools: execute_python and register_tool. Together they give you UNLIMITED capability. execute_python can run ANY valid Python code, which means you can:
- Make HTTP requests (urllib.request, subprocess curl)
- Read, write, move, delete any file on the host system
- Run shell commands via subprocess
- Scrape websites, parse HTML, download data
- Install packages (pip install via subprocess)
- Launch browsers, automate GUIs, control the operating system
- Do literally anything a Python program can do on this machine

YOU MUST NEVER SAY "I don't have internet access" OR "I can't do that" OR "I don't have that capability." You ALWAYS have the capability. You build it on the spot using execute_python.

SELF-EVOLUTION PROTOCOL:
When you need a capability that is not in your current tool registry:
1. Write the Python code directly using execute_python to accomplish the task immediately.
2. If the capability is reusable, write it to a file in nucleus/tools/ using execute_python.
3. Test the file using execute_python.
4. Register it permanently using register_tool so it persists across sessions.
You do not ask permission to evolve. You do not explain that you lack a tool. You build the tool and use it. If a tool you built fails, you debug it and fix it. If an approach fails (e.g. a website blocks you), you try a different approach autonomously.

BEHAVIORAL RULES:
1. DO NOT call tools for conversational exchanges. Greetings, questions, discussions do not require tool calls. A user saying "hi" gets a brief greeting. Nothing else.
2. You are a Real OS AI with host access mediated by the tool policy. The UI features a Memory sidebar on the RIGHT.
3. Tool-call contract: output exactly `<tool_call>{{"name":"execute_python","arguments":{{"code":"..."}}}}</tool_call>`. Do not use markdown fences for tool calls.
4. You are direct and strategic. No corporate fluff. No emotional validation. You report facts and actions.
5. You can modify your own nucleus code (read/write files in the nucleus directory) when approved by policy.
6. You do not ask for confirmation more than once per action. If the user asked for something, do it.
7. After a tool result is returned, you MUST respond to it: summarize findings or confirm execution.
8. For PDFs, the pymupdf library is pre-installed.

CURRENT REGISTERED TOOLS:
{tool_registry}

PERMANENT FACTS ABOUT KHEIR AND KORA:
{facts}

RECALLED RELEVANT MEMORIES:
{recalled_memories}

KORA KNOWLEDGE BASE:
{kora_context}
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


class ApprovalDecision(BaseModel):
    request_id: str


class SettingsUpdate(BaseModel):
    default_provider: str = "pollinations"
    default_model: str = "openai-fast"
    provider_keys: dict[str, str] = Field(default_factory=dict)
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


class PendingApproval:
    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_summary: str = "Approval required before tool execution.",
        policy_reasons: list[str] | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_summary = risk_summary
        self.policy_reasons = policy_reasons or []
        self.event = asyncio.Event()
        self.approved: bool | None = None


class ModelAdapter:
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str) -> AsyncIterator[str]:
        raise NotImplementedError


class ProviderAdapter(ModelAdapter):
    async def complete(self, messages: list[dict[str, str]], model: str, provider: str) -> AsyncIterator[str]:
        provider_config = get_provider(provider)
        provider_type = provider_config.get("type", "openai_compatible")
        if provider_type == "ollama":
            async for token in stream_ollama(provider_config, messages, model):
                yield token
            return
        if provider_type == "gguf":
            async for token in stream_gguf(provider_config, messages, model):
                yield token
            return
        async for token in stream_openai_compatible(provider_config, messages, model):
            yield token


async def stream_openai_compatible(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
) -> AsyncIterator[str]:
    base_url = provider_config.get("base_url") or POLLINATIONS_BASE_URL
    api_key = get_provider_api_key_from_config(provider_config)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    # Map "tool" role to "user"; skip messages with empty content
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

    payload = {"model": model, "messages": mapped_messages, "stream": True}
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
                content = delta.get("content")
                if content:
                    yield content


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
    return await discover_openai_compatible_models(provider_config)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_layout() -> None:
    for path in (DATA_DIR, ARCHIVE_DIR, DATA_DIR / "tasks", TOOLS_DIR, KORA_DIR):
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


def provider_status() -> dict[str, Any]:
    settings = load_settings()
    providers: dict[str, Any] = {}
    for provider_id, provider in settings["providers"].items():
        key_name = provider.get("api_key_env", "")
        providers[provider_id] = {
            "label": provider.get("label", provider_id),
            "type": provider.get("type", "openai_compatible"),
            "base_url": provider.get("base_url", ""),
            "api_key_env": key_name,
            "configured": bool(os.getenv(key_name)),
            "models": provider.get("models") or MODEL_OPTIONS.get(provider_id, []),
            "models_path": provider.get("models_path", ""),
            "runtime_settings": normalize_runtime_settings(provider.get("runtime_settings")),
        }
    return {
        "default_provider": settings.get("default_provider", "pollinations"),
        "default_model": settings.get("default_model", "openai-fast"),
        "providers": providers,
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
    if update.type not in {"openai_compatible", "ollama", "gguf"}:
        raise ValueError("Provider type must be openai_compatible, ollama, or gguf.")
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


def find_tool(name: str) -> dict[str, Any] | None:
    for tool in load_registry()["tools"]:
        if tool.get("name") == name:
            return tool
    return None


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


def retrieve_relevant(query: str, limit: int = 4) -> str:
    query_words = set(extract_keywords(query))
    if not query_words:
        return ""
    with connect_db() as conn:
        rows = conn.execute("SELECT summary, keywords, created_at FROM episodic_memory").fetchall()
    
    scored = []
    for row in rows:
        mem_words = set(row["keywords"].split(","))
        score = len(query_words & mem_words)
        if score > 0:
            scored.append((score, row))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return ""
    
    top = scored[:limit]
    return "\n\n".join([
        f"[{row['created_at'][:10]}] {row['summary']}"
        for _, row in top
    ])


def build_system_prompt(request: ChatRequest) -> str:
    facts = json.dumps(load_json(FACTS_PATH, DEFAULT_FACTS), indent=2)
    registry = json.dumps(load_registry(), indent=2)
    first_query = request.messages[0].content if request.messages else ""
    recalled_text = retrieve_relevant(first_query)
    if not recalled_text:
        recalled_text = "No relevant memories found."
    else:
        # Cap recalled memories
        if len(recalled_text) > 4000:
            recalled_text = recalled_text[:4000] + "... [truncated]"
    
    # Cap context files
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
        kora_context=kora_context,
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
        Your previous answer violated the self-evolution protocol by claiming a missing capability.
        You are not allowed to stop at that refusal. Use the existing primitive instead:
        emit exactly one valid <tool_call> for execute_python that creates or exercises the capability needed to satisfy this user request.
        If the capability should persist, use execute_python to write a reusable file under nucleus/tools/ and then register it with register_tool after it is tested.

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


def resolve_tool_path(filepath: str) -> Path:
    candidate = (BASE_DIR / filepath).resolve() if not Path(filepath).is_absolute() else Path(filepath).resolve()
    tools_root = TOOLS_DIR.resolve()
    if tools_root != candidate and tools_root not in candidate.parents:
        raise ValueError("Registered tools must live under nucleus/tools.")
    if candidate.suffix.lower() != ".py":
        raise ValueError("Registered tools must be Python files.")
    if not candidate.exists():
        raise ValueError(f"Tool file does not exist: {filepath}")
    return candidate


WRITE_CALLS = {"write_text", "write_bytes", "unlink", "mkdir", "makedirs", "rmdir", "remove", "rename", "replace", "touch"}
SUBPROCESS_IMPORTS = {"subprocess", "pty", "shutil"}
NETWORK_IMPORTS = {"socket", "http.client", "httpx", "requests", "urllib", "urllib.request", "ftplib", "smtplib"}
BLOCKED_IMPORTS = {"ctypes"}
DYNAMIC_EXECUTION_CALLS = {"eval", "exec", "compile", "__import__"}
OS_PROCESS_CALLS = {"system", "popen", "spawnl", "spawnlp", "spawnv", "spawnvp", "execv", "execvp"}


def approved_execution_roots() -> list[Path]:
    roots = [ROOT_DIR, BASE_DIR, TOOLS_DIR, DATA_DIR, ARCHIVE_DIR, KORA_DIR]
    return [root.resolve() for root in roots]


def path_in_approved_roots(path: Path) -> bool:
    resolved = path.resolve()
    for root in approved_execution_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def summarize_python_execution_policy(code: str) -> dict[str, Any]:
    reasons: list[str] = []
    blocked: list[str] = []
    module_aliases: dict[str, str] = {}
    risky_call_aliases: dict[str, str] = {}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Let the runtime catch syntax errors instead of blocking preemptively
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    module_aliases[alias.asname or top_level] = top_level
                    if alias.name in SUBPROCESS_IMPORTS or top_level in SUBPROCESS_IMPORTS:
                        reasons.append("subprocess/process access")
                    if alias.name in NETWORK_IMPORTS or top_level in NETWORK_IMPORTS:
                        reasons.append("network access")
                    if alias.name in BLOCKED_IMPORTS or top_level in BLOCKED_IMPORTS:
                        blocked.append(f"blocked import: {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
                top_level = module_name.split(".")[0]
                if module_name in SUBPROCESS_IMPORTS or top_level in SUBPROCESS_IMPORTS:
                    reasons.append("subprocess/process access")
                if module_name in NETWORK_IMPORTS or top_level in NETWORK_IMPORTS:
                    reasons.append("network access")
                if module_name in BLOCKED_IMPORTS or top_level in BLOCKED_IMPORTS:
                    blocked.append(f"blocked import: {module_name}")
                if module_name == "os":
                    for alias in node.names:
                        if alias.name in OS_PROCESS_CALLS:
                            risky_call_aliases[alias.asname or alias.name] = "subprocess/process access"

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                owner = ""
                is_attribute_call = isinstance(node.func, ast.Attribute)
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    if func_name in risky_call_aliases:
                        reasons.append(risky_call_aliases[func_name])
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                    if isinstance(node.func.value, ast.Name):
                        owner = node.func.value.id
                        owner_module = module_aliases.get(owner, owner)
                        if owner_module == "os" and func_name in OS_PROCESS_CALLS:
                            reasons.append("subprocess/process access")
                if func_name in DYNAMIC_EXECUTION_CALLS:
                    blocked.append(f"dynamic code execution via {func_name}()")
                if func_name in WRITE_CALLS:
                    reasons.append("filesystem write or mutation")
                if func_name == "open":
                    mode = "r"
                    mode_arg_index = 0 if is_attribute_call else 1
                    if len(node.args) > mode_arg_index and isinstance(node.args[mode_arg_index], ast.Constant) and isinstance(node.args[mode_arg_index].value, str):
                        mode = node.args[mode_arg_index].value
                    for keyword in node.keywords:
                        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                            mode = keyword.value.value
                    if any(flag in mode for flag in ("w", "a", "x", "+")):
                        reasons.append("filesystem write or mutation")

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if text.startswith("~"):
                    reasons.append("home-directory access")
                path = Path(text)
                if path.is_absolute() and not path_in_approved_roots(path):
                    reasons.append(f"absolute path outside approved roots: {text}")

            if isinstance(node, ast.Attribute) and node.attr in {"home", "expanduser"}:
                reasons.append("home-directory access")

    if blocked:
        unique_blocked = sorted(set(blocked))
        return {
            "action": "block",
            "risk_summary": "Policy blocked execution: " + "; ".join(unique_blocked) + ".",
            "reasons": unique_blocked,
        }
    unique_reasons = sorted(set(reasons))
    if unique_reasons:
        return {
            "action": "require_approval",
            "risk_summary": "Requires approval: " + "; ".join(unique_reasons) + ".",
            "reasons": unique_reasons,
        }
    return {"action": "allow", "risk_summary": "Read-only Python execution appears low risk.", "reasons": []}


async def execute_python(code: str, policy_approved: bool = False) -> dict[str, Any]:
    policy = summarize_python_execution_policy(code)
    if policy["action"] == "block":
        return {"error": policy["risk_summary"], "policy": policy, "exit_code": -1, "timed_out": False}
    if policy["action"] == "require_approval" and not policy_approved:
        return {"error": "Python execution requires approval by policy.", "policy": policy, "exit_code": -1, "timed_out": False}

    start = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT_DIR)
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=30
        )
        return {
            "stdout": stdout.decode()[-100_000:],
            "stderr": stderr.decode()[-100_000:],
            "exit_code": proc.returncode,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000)
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        return {
            "stdout": "",
            "stderr": "Timed out after 30s",
            "exit_code": -1,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000)
        }





def run_registered_tool(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    filepath = tool.get("filepath")
    if not filepath:
        raise ValueError(f"Tool {tool.get('name')} has no filepath.")
    path = resolve_tool_path(filepath)
    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=ROOT_DIR,
        input=json.dumps(arguments),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=120,
        check=False,
    )
    return {
        "stdout": completed.stdout[-100_000:],
        "stderr": completed.stderr[-100_000:],
        "exit_code": completed.returncode,
        "timed_out": False,
    }


def register_tool(
    name: str,
    description: str,
    parameters_schema: dict[str, Any],
    filepath: str,
    requires_approval: bool = False,
) -> dict[str, Any]:
    validate_tool_name(name)
    full_path = ROOT_DIR / filepath
    if not full_path.exists():
        return {
            "error": f"File not found: {filepath}. Write the file first via execute_python before registering."
        }
    
    registry = load_registry()
    entry = {
        "name": name,
        "description": description,
        "parameters": parameters_schema,
        "filepath": str(filepath),
        "builtin": False,
        "requires_approval": bool(requires_approval),
    }
    registry["tools"] = [t for t in registry["tools"] if t.get("name") != name]
    registry["tools"].append(entry)
    save_json(REGISTRY_PATH, registry)
    return {"registered": name, "permanent": True}


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
    raw_response = await complete_model_text(prune_conversation(conversation), model, provider)
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


def store_episodic_memory(session_id: str, summary: str, keywords: list[str]) -> None:
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (id, created_at, keywords, summary, session_id) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), now_iso(), ",".join(keywords), summary, session_id),
        )
        conn.commit()


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


def normalize_session_archive(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    session_id = str(payload.get("session_id") or path.stem)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
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
        return JSONResponse(normalize_session_archive(path))
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
    provider_ids = set(settings["providers"].keys())
    if update.default_provider not in provider_ids:
        raise HTTPException(status_code=400, detail="Unknown provider.")
    provider_models = settings["providers"][update.default_provider].get("models") or MODEL_OPTIONS.get(update.default_provider, [])
    if update.default_model not in provider_models:
        raise HTTPException(status_code=400, detail="Unknown model for provider.")

    settings["default_provider"] = update.default_provider
    settings["default_model"] = update.default_model
    if update.runtime_settings:
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
    save_settings(settings)
    return JSONResponse(provider_status())


@app.post("/providers")
async def save_provider(update: ProviderUpdate) -> JSONResponse:
    try:
        return JSONResponse(upsert_provider(update))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.post("/approve")
async def approve(decision: ApprovalDecision) -> JSONResponse:
    pending = app.state.pending_approvals.get(decision.request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    pending.approved = True
    pending.event.set()
    return JSONResponse({"ok": True})


@app.post("/reject")
async def reject(decision: ApprovalDecision) -> JSONResponse:
    pending = app.state.pending_approvals.get(decision.request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    pending.approved = False
    pending.event.set()
    return JSONResponse({"ok": True})


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
        return JSONResponse(normalize_session_archive(path))
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


@app.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        session_id = str(uuid.uuid4())
        goal = last_user_message(request.messages)
        task_state = load_task_state(DATA_DIR, request.task_id) if request.task_id else None
        if task_state is None:
            task_state = TaskState(task_id=request.task_id or str(uuid.uuid4()), goal=goal)
        elif goal:
            task_state.goal = goal
        save_task_state(task_state, DATA_DIR)

        def emit_task_phase(phase: str, payload: dict[str, Any] | None = None) -> str:
            task_state.mark_phase(phase)
            save_task_state(task_state, DATA_DIR)
            event_payload: dict[str, Any] = {
                "task_id": task_state.task_id,
                "phase": task_state.phase,
                "done": task_state.done,
            }
            if payload:
                event_payload.update(payload)
            return sse(task_event_name(phase), event_payload)
        
        # Sanitize incoming messages: cap extremely long ones
        sanitized_messages = []
        for msg in request.messages:
            content = msg.content
            if len(content) > 12000:
                content = content[:12000] + "... [Message truncated in history]"
            sanitized_messages.append({"role": msg.role, "content": content})
            
        conversation: list[dict[str, str]] = [{"role": "system", "content": build_system_prompt(request)}]
        conversation.extend(sanitized_messages)
        public_messages = sanitized_messages.copy()
        yield sse("meta", {"session_id": session_id, "model": request.model, "task_id": task_state.task_id})
        yield emit_task_phase("plan", {"goal": task_state.goal, "plan": task_state.plan})
        try:
            max_task_steps = max(1, int(request.max_task_steps or 12))
            model_turns = 0
            while not task_state.done and model_turns < max_task_steps:
                model_turns += 1
                if task_state.observations:
                    yield emit_task_phase("revise", {"observations": task_state.observations[-3:]})
                text = ""
                provider = request.provider or load_settings().get("default_provider", "pollinations")
                model = request.model or load_settings().get("default_model", "openai-fast")
                
                # Auto-compaction / Pruning before calling model
                active_conversation = prune_conversation(conversation)
                
                async for token in app.state.model_adapter.complete(active_conversation, model, provider):
                    text += token
                    yield sse("delta", {"content": token})
                display_text = strip_tool_calls(text).strip()
                calls = extract_tool_calls(text)
                if not task_state.steps:
                    task_state.add_plan(display_text or text)
                    save_task_state(task_state, DATA_DIR)
                    yield emit_task_phase("plan", {"goal": task_state.goal, "plan": task_state.plan})
                if not calls and is_capability_refusal(text):
                    task_state.failure_count += 1
                    save_task_state(task_state, DATA_DIR)
                    yield sse("assistant_replace", {"content": "Retrying under the self-evolution protocol."})
                    yield emit_task_phase("revise", {"reason": "capability_refusal", "failure_count": task_state.failure_count})
                    conversation.append({"role": "assistant", "content": text})
                    conversation.append({"role": "system", "content": build_self_evolution_retry_message(request)})
                    continue

                if display_text != text.strip():
                    yield sse("assistant_replace", {"content": display_text})
                conversation.append({"role": "assistant", "content": text})
                public_messages.append({"role": "assistant", "content": display_text or text})
                if not calls:
                    task_state.done = True
                    task_state.artifacts["final_answer"] = display_text or text
                    save_task_state(task_state, DATA_DIR)
                    yield emit_task_phase("final", {"answer": display_text or text})
                    break
                yield emit_task_phase("act", {"tool_call_count": len(calls)})
                for call in calls:
                    tool_name = str(call["name"])
                    arguments = call.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    step = task_state.add_step(tool_name, arguments)
                    save_task_state(task_state, DATA_DIR)
                    yield emit_task_phase("act", {"step": step})
                    tool = find_tool(tool_name)
                    policy: dict[str, Any] | None = None
                    if tool_name == "execute_python":
                        policy = summarize_python_execution_policy(str(arguments.get("code", "")))
                        if policy["action"] == "block":
                            result = {"error": policy["risk_summary"], "policy": policy}
                            store_tool_execution(tool_name, arguments, result, False)
                            observation = task_state.add_observation(step, result, False)
                            save_task_state(task_state, DATA_DIR)
                            yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": False})
                            yield emit_task_phase("observe", {"observation": observation})
                            conversation.append({"role": "tool", "content": json.dumps(result)})
                            evaluation = await evaluate_tool_result(conversation, session_id, model, provider)
                            yield sse("evaluation", evaluation)
                            continue
                    requires_approval = bool(tool is not None and tool.get("requires_approval"))
                    policy_approved = False
                    if policy is not None and policy["action"] == "require_approval":
                        requires_approval = True
                    if request.auto_approve:
                        requires_approval = False
                        policy_approved = True
                    if requires_approval:
                        risk_summary = "Approval required before tool execution."
                        policy_reasons: list[str] = []
                        if policy is not None:
                            risk_summary = policy.get("risk_summary", risk_summary)
                            policy_reasons = policy.get("reasons", [])
                        elif tool is not None and tool.get("requires_approval"):
                            risk_summary = "Approval required by tool registry."
                        pending = PendingApproval(tool_name, arguments, risk_summary, policy_reasons)
                        app.state.pending_approvals[pending.id] = pending
                        yield sse(
                            "approval_request",
                            {
                                "request_id": pending.id,
                                "tool_name": tool_name,
                                "arguments": arguments,
                                "risk_summary": pending.risk_summary,
                                "policy_reasons": pending.policy_reasons,
                                "code": str(arguments.get("code", "")) if tool_name == "execute_python" else "",
                            },
                        )
                        await pending.event.wait()
                        approved = bool(pending.approved)
                        policy_approved = approved
                        app.state.pending_approvals.pop(pending.id, None)
                        if not approved:
                            result = {"error": "Tool execution rejected by user.", "risk_summary": pending.risk_summary}
                            store_tool_execution(tool_name, arguments, result, False)
                            observation = task_state.add_observation(step, result, False)
                            save_task_state(task_state, DATA_DIR)
                            yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": False})
                            yield emit_task_phase("observe", {"observation": observation})
                            
                            result_str = json.dumps(result)
                            if len(result_str) > 10000:
                                result_str = result_str[:10000] + "... [Result truncated]"
                            conversation.append({"role": "tool", "content": result_str})
                            evaluation = await evaluate_tool_result(conversation, session_id, model, provider)
                            yield sse("evaluation", evaluation)
                            continue
                    yield sse("tool_call", {"tool_name": tool_name, "arguments": arguments})
                    if tool_name == "execute_python":
                        result = await execute_python(str(arguments.get("code", "")), policy_approved=policy_approved)
                    elif tool_name == "register_tool":
                        result = register_tool(
                            name=str(arguments.get("name", "")),
                            description=str(arguments.get("description", "")),
                            parameters_schema=arguments.get("parameters_schema", {}),
                            filepath=str(arguments.get("filepath", "")),
                            requires_approval=bool(arguments.get("requires_approval", False)),
                        )
                    else:
                        tool = find_tool(tool_name)
                        if tool is not None and tool.get("builtin"):
                            result = {"error": f"Builtin tool {tool_name} is not implemented in dispatch loop."}
                        elif tool is not None:
                            result = run_registered_tool(tool, arguments)
                        else:
                            result = {"error": f"Tool {tool_name} not found."}
                    store_tool_execution(tool_name, arguments, result, True)
                    observation = task_state.add_observation(step, result, True)
                    save_task_state(task_state, DATA_DIR)
                    yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": True})
                    yield emit_task_phase("observe", {"observation": observation})
                    
                    # Cap result for conversation history to avoid 400 errors
                    result_str = json.dumps(result)
                    if len(result_str) > 10000:
                        result_str = result_str[:10000] + "... [Result truncated for context]"
                    conversation.append({"role": "tool", "content": result_str})
<<<<<<< codex/add-evaluation-phase-after-tool-execution
                    evaluation = await evaluate_tool_result(conversation, session_id, model, provider)
                    yield sse("evaluation", evaluation)
                    if evaluation["decision"] == "final":
                        break
=======
                yield emit_task_phase("evaluate", {"step_count": len(task_state.steps), "observation_count": len(task_state.observations)})
            if not task_state.done:
                task_state.failure_count += 1
                task_state.done = True
                task_state.artifacts["final_answer"] = "Task stopped after reaching the configured step limit."
                save_task_state(task_state, DATA_DIR)
                yield emit_task_phase("final", {"answer": task_state.artifacts["final_answer"], "reason": "step_limit"})
>>>>>>> master
            # Remove the automatic summary call from stream, frontend will call /memory/save
            yield sse("done", {"session_id": session_id, "task_id": task_state.task_id})
        except Exception as exc:
            yield sse("error", {"message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False, app_dir=str(BASE_DIR))
