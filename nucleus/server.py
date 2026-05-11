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

SYSTEM_TEMPLATE = """
You are Ouroboros. You are the personal sovereign AI workspace of Kheir Lissi, founder of Kora Lab, Lome Togo.

BEHAVIORAL RULES — these are absolute:

1. DO NOT call tools for conversational exchanges. Greetings, questions, discussions, strategy sessions do not require tool calls. A user saying "hi" gets "Hello." Nothing else.

2. You are a Real OS AI. Your scope is the entire host operating system, not just the project directory. Use absolute paths to read, write, or execute anywhere on the machine. You manage files, processes, and configurations globally as Kheir's digital executor. The UI features a Memory sidebar on the RIGHT.

3. When you need a capability you do not have, follow these atomic steps:
   a. Write the Python logic (for PDFs, use the `pymupdf` library which is pre-installed).
   b. Use execute_python to write the code to a file inside `nucleus/tools/`.
   c. Use execute_python to test the file (calling it with JSON via stdin).
   d. Use register_tool to install it permanently, providing the filename (no path needed for filepath).

4. You are direct and strategic. No corporate fluff. No emotional validation. You report facts and actions.

5. You can modify your own nucleus code by reading and writing files in the nucleus directory, but you are not confined to it.

6. You do not ask for confirmation more than once per action. If the user has asked for something, do it.

CURRENT CAPABILITIES:
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
    
    # Map "tool" role to "user" for maximum compatibility
    mapped_messages = []
    for m in messages:
        if m["role"] == "tool":
            mapped_messages.append({"role": "user", "content": f"[TOOL RESULT]: {m['content']}"})
        else:
            mapped_messages.append(m)

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
            mapped_messages.append({"role": "user", "content": f"[TOOL RESULT]: {m['content']}"})
        else:
            mapped_messages.append(m)

    payload = {"model": model, "messages": mapped_messages, "stream": True}
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


async def execute_python(code: str) -> dict[str, Any]:
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


async def summarize_and_store(messages: list[dict[str, str]], session_id: str):
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
    try:
        try:
            summary = await call_model_simple(summary_prompt, model="openai-fast")
        except Exception as e:
            print(f"WARNING: Model summarization failed, using fallback. Error: {e}")
            summary = f"Conversation session {session_id}. Summary unavailable due to model error."
        
        keywords = extract_keywords(summary)
        if not keywords:
            keywords = ["session", "archive"]
            
        # Archive full session
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / f"{session_id}.json").write_text(json.dumps({"session_id": session_id, "messages": messages}, indent=2), encoding="utf-8")

        with connect_db() as conn:
            conn.execute(
                "INSERT INTO episodic_memory (id, created_at, keywords, summary, session_id) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), now_iso(), ",".join(keywords), summary, session_id),
            )
            conn.commit()
        return {"status": "saved", "session_id": session_id, "summary": summary}
    except Exception as e:
        print(f"ERROR: summarize_and_store failed: {e}")
        return {"status": "error", "message": str(e)}


class MemorySaveRequest(BaseModel):
    session_id: str
    messages: list[ChatMessage]


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



@app.get("/memory/{session_id}")
async def get_session_history(session_id: str) -> JSONResponse:
    path = ARCHIVE_DIR / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found.")
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
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
        yield sse("meta", {"session_id": session_id, "model": request.model})
        try:
            for _ in range(4):
                text = ""
                provider = request.provider or load_settings().get("default_provider", "pollinations")
                model = request.model or load_settings().get("default_model", "openai-fast")
                
                # Auto-compaction / Pruning before calling model
                active_conversation = prune_conversation(conversation)
                
                async for token in app.state.model_adapter.complete(active_conversation, model, provider):
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
                            
                            result_str = json.dumps(result)
                            if len(result_str) > 10000:
                                result_str = result_str[:10000] + "... [Result truncated]"
                            conversation.append({"role": "tool", "content": result_str})
                            continue
                    yield sse("tool_call", {"tool_name": tool_name, "arguments": arguments})
                    if tool_name == "execute_python":
                        result = await execute_python(str(arguments.get("code", "")))
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
                    yield sse("tool_result", {"tool_name": tool_name, "result": result, "approved": True})
                    
                    # Cap result for conversation history to avoid 400 errors
                    result_str = json.dumps(result)
                    if len(result_str) > 10000:
                        result_str = result_str[:10000] + "... [Result truncated for context]"
                    conversation.append({"role": "tool", "content": result_str})
            # Remove the automatic summary call from stream, frontend will call /memory/save
            yield sse("done", {"session_id": session_id})
        except Exception as exc:
            yield sse("error", {"message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False, app_dir=str(BASE_DIR))
