from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
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
MODEL_OPTIONS = {
    "pollinations": [
        "openai-fast",
        "qwen-coder",
        "deepseek",
        "nova-fast",
    ]
}

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
            "requires_approval": False,
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

SYSTEM_TEMPLATE = """You are Ouroboros, a self-evolving personal AI substrate built for Kheir Lissi, founder of Kora Lab in Lome, Togo.

Your purpose is not to answer questions. Your purpose is to act as a sovereign thinking and execution environment that grows its own capabilities over time.

You have two built-in tools: execute_python and register_tool. These two tools contain every other capability. When you need to do something you cannot currently do, write the Python code to do it, test it via execute_python, then register it as a permanent tool via register_tool so it is available in every future session.

To call a tool, return a JSON object inside <tool_call> tags:
<tool_call>{{"name":"execute_python","arguments":{{"code":"print('hello')"}}}}</tool_call>

You have access to the following registered tools:
{tool_registry}

You have the following permanent facts about the user:
{facts}

Recalled memories relevant to this conversation:
{recalled_memories}

Kora Lab knowledge base:
{kora_context}

Rules:
- Be direct. No hedging.
- When you build something, tell the user what you built and that it is now permanent.
- When you update facts.json, say so.
- When you register a new tool, confirm it by name.
- You can return HTML in your response and it will render. Use this to build persistent UI panels when useful.
- You are not a generic assistant. You are Kheir's sovereign workspace.
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


class ApprovalDecision(BaseModel):
    request_id: str


class SettingsUpdate(BaseModel):
    default_provider: str = "pollinations"
    default_model: str = "openai-fast"
    provider_keys: dict[str, str] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    id: str
    label: str
    type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    models_path: str = ""


class LocalModelScanRequest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    max_results: int = 200


class PendingApproval:
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.id = str(uuid.uuid4())
        self.tool_name = tool_name
        self.arguments = arguments
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
            async for token in stream_gguf(messages, model):
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
    payload = {"model": model, "messages": messages, "stream": True}
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()
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


async def stream_ollama(
    provider_config: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
) -> AsyncIterator[str]:
    base_url = (provider_config.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": model, "messages": messages, "stream": True}
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


async def stream_gguf(messages: list[dict[str, str]], model_path: str) -> AsyncIterator[str]:
    path = Path(model_path).expanduser()
    if not path.exists() or path.suffix.lower() != ".gguf":
        raise RuntimeError("Selected GGUF model path does not exist.")
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise RuntimeError("Direct GGUF inference requires llama-cpp-python. Install it or run the model through Ollama or llama.cpp server.") from exc

    cache_key = str(path.resolve())
    if cache_key not in LLAMA_CACHE:
        LLAMA_CACHE[cache_key] = Llama(model_path=cache_key, n_ctx=4096, verbose=False)
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
    for path in (DATA_DIR, ARCHIVE_DIR, TOOLS_DIR, KORA_DIR):
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


def keywords_for(text: str, limit: int = 40) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    stop = {
        "the",
        "and",
        "for",
        "that",
        "with",
        "this",
        "from",
        "you",
        "are",
        "was",
        "have",
        "has",
        "not",
        "but",
    }
    seen: list[str] = []
    for word in words:
        if word not in stop and word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def retrieve_relevant(query: str, limit: int = 5) -> list[dict[str, Any]]:
    query_words = set(keywords_for(query, limit=80))
    if not query_words:
        return []
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM episodic_memory ORDER BY created_at DESC").fetchall()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        row_keywords = set(filter(None, row["keywords"].split(",")))
        score = len(query_words & row_keywords)
        if score > 0:
            scored.append((score, dict(row)))
    scored.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
    return [item[1] for item in scored[:limit]]


def build_system_prompt(request: ChatRequest) -> str:
    facts = json.dumps(load_json(FACTS_PATH, DEFAULT_FACTS), indent=2)
    registry = json.dumps(load_registry(), indent=2)
    query = "\n".join(message.content for message in request.messages[-4:])
    recalled = retrieve_relevant(query)
    recalled_text = "\n\n".join(f"{item['created_at']}: {item['summary']}" for item in recalled)
    if not recalled_text:
        recalled_text = "No relevant memories found."
    context_file_text = "\n\n".join(f"### {item.name}\n{item.content}" for item in request.context_files)
    kora_context = load_kora_context()
    if context_file_text:
        kora_context = f"{kora_context}\n\nUploaded context files:\n{context_file_text}"
    return SYSTEM_TEMPLATE.format(
        tool_registry=registry,
        facts=facts,
        recalled_memories=recalled_text,
        kora_context=kora_context,
    )


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "name" in payload and "arguments" in payload:
            calls.append(payload)
    return calls


def strip_tool_calls(text: str) -> str:
    return re.sub(r"<tool_call>\s*\{.*?\}\s*</tool_call>", "", text, flags=re.DOTALL)


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


def execute_python(code: str) -> dict[str, Any]:
    start = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT_DIR,
            input="",
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return {
            "stdout": completed.stdout[-100_000:],
            "stderr": completed.stderr[-100_000:],
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": (exc.stdout or "")[-100_000:] if isinstance(exc.stdout, str) else "",
            "stderr": "Stopped after 30 seconds.",
            "exit_code": 124,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000),
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
        capture_output=True,
        timeout=30,
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
    path = resolve_tool_path(filepath)
    subprocess.run([sys.executable, "-m", "py_compile", str(path)], check=True, capture_output=True, text=True)
    registry = load_registry()
    entry = {
        "name": name,
        "description": description,
        "parameters": parameters_schema,
        "filepath": str(path.relative_to(BASE_DIR)).replace("\\", "/"),
        "builtin": False,
        "requires_approval": bool(requires_approval),
    }
    registry["tools"] = [tool for tool in registry["tools"] if tool.get("name") != name]
    registry["tools"].append(entry)
    save_json(REGISTRY_PATH, registry)
    return entry


def store_tool_execution(tool_name: str, arguments: dict[str, Any], result: dict[str, Any], approved: bool) -> None:
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO tool_execution (id, tool_name, arguments, result, timestamp, approved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                tool_name,
                json.dumps(arguments),
                json.dumps(result),
                now_iso(),
                1 if approved else 0,
            ),
        )
        conn.commit()


def summarize_session(session_id: str, messages: list[dict[str, str]]) -> dict[str, str]:
    text = "\n".join(f"{message['role']}: {message['content']}" for message in messages)
    words = text.split()
    summary = " ".join(words[:260])
    if len(words) > 260:
        summary += " ..."
    if not summary:
        summary = "Empty session."
    keywords = ",".join(keywords_for(text))
    archive_path = ARCHIVE_DIR / f"{session_id}.json"
    archive_path.write_text(json.dumps({"session_id": session_id, "messages": messages}, indent=2), encoding="utf-8")
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (id, created_at, keywords, summary, session_id) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), now_iso(), keywords, summary[:2400], session_id),
        )
        conn.commit()
    return {"summary": summary[:2400], "keywords": keywords}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_env_files()
    ensure_layout()
    init_db()
    app.state.model_adapter = ProviderAdapter()
    app.state.pending_approvals = {}
    yield


app = FastAPI(title="Ouroboros Nucleus", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/tools")
async def get_tools() -> JSONResponse:
    return JSONResponse(load_registry())


@app.get("/memory")
async def get_memory() -> JSONResponse:
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, keywords, summary, session_id FROM episodic_memory ORDER BY created_at DESC"
        ).fetchall()
    return JSONResponse({"memories": [dict(row) for row in rows]})


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


@app.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        session_id = str(uuid.uuid4())
        conversation: list[dict[str, str]] = [{"role": "system", "content": build_system_prompt(request)}]
        conversation.extend(message.model_dump() for message in request.messages)
        public_messages = [message.model_dump() for message in request.messages]
        yield sse("meta", {"session_id": session_id, "model": request.model})
        try:
            for _ in range(4):
                text = ""
                provider = request.provider or load_settings().get("default_provider", "pollinations")
                model = request.model or load_settings().get("default_model", "openai-fast")
                async for token in app.state.model_adapter.complete(conversation, model, provider):
                    text += token
                    yield sse("delta", {"content": token})
                display_text = strip_tool_calls(text).strip()
                if display_text != text.strip():
                    yield sse("assistant_replace", {"content": display_text})
                conversation.append({"role": "assistant", "content": text})
                public_messages.append({"role": "assistant", "content": display_text or text})
                calls = extract_tool_calls(text)
                if not calls:
                    break
                for call in calls:
                    tool_name = str(call["name"])
                    arguments = call.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    tool = find_tool(tool_name)
                    if tool is not None and tool.get("requires_approval"):
                        pending = PendingApproval(tool_name, arguments)
                        app.state.pending_approvals[pending.id] = pending
                        yield sse(
                            "approval_request",
                            {"request_id": pending.id, "tool_name": tool_name, "arguments": arguments},
                        )
                        await pending.event.wait()
                        approved = bool(pending.approved)
                        app.state.pending_approvals.pop(pending.id, None)
                        if not approved:
                            result = {"error": "Tool execution rejected by user."}
                            store_tool_execution(tool_name, arguments, result, False)
                            yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": False})
                            conversation.append({"role": "tool", "content": json.dumps(result)})
                            continue
                    yield sse("tool_call", {"tool_name": tool_name, "arguments": arguments})
                    if tool_name == "execute_python":
                        result = execute_python(str(arguments.get("code", "")))
                    elif tool_name == "register_tool":
                        result = register_tool(
                            name=str(arguments.get("name", "")),
                            description=str(arguments.get("description", "")),
                            parameters_schema=arguments.get("parameters_schema", {}),
                            filepath=str(arguments.get("filepath", "")),
                            requires_approval=bool(arguments.get("requires_approval", False)),
                        )
                    else:
                        if tool is None:
                            raise ValueError(f"Unknown tool: {tool_name}")
                        result = run_registered_tool(tool, arguments)
                    store_tool_execution(tool_name, arguments, result, True)
                    yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": True})
                    conversation.append({"role": "tool", "content": json.dumps(result)})
            memory = summarize_session(session_id, public_messages)
            yield sse("memory_saved", memory)
            yield sse("done", {"session_id": session_id})
        except Exception as exc:
            yield sse("error", {"message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False, app_dir=str(BASE_DIR))
