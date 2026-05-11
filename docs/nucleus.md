# Ouroboros Nucleus

The Python nucleus is the primary Ouroboros implementation.

It lives under `nucleus/` and contains:

- `server.py`: FastAPI server, memory store, model adapter, tool execution
- `index.html`: no-framework browser UI
- `registry.json`: persistent tool registry
- `facts.json`: permanent facts injected into every session
- `kora/`: local Kora Lab knowledge base
- `tools/`: registered Python tools
- `data/`: SQLite memory and archived sessions

## Run

```bash
python nucleus/server.py
```

Set `POLLINATIONS_API_KEY` in the environment before real chat use.

The server also loads root `.env` and `nucleus/.env`. The static UI Settings panel can save the Pollinations key into root `.env`, choose the default provider, and choose the default model. `.env.example` is the committed template. `.env` is local only.

## Providers

Settings supports three provider types:

- OpenAI compatible: any endpoint with `/chat/completions` and `/models`, including Pollinations, local llama.cpp servers, and compatible hosted APIs.
- Ollama: local Ollama at `http://127.0.0.1:11434`, discovered through `/api/tags`, chatted through `/api/chat`.
- Local GGUF folder: discovers `.gguf` files from a folder. Direct GGUF inference works when `llama-cpp-python` is installed. Without it, run the model through Ollama or llama.cpp server and register that server as OpenAI compatible.

For LM Arena or any other free model gateway, add it as an OpenAI-compatible provider if it exposes a compatible base URL. Put the base URL in Settings and use Discover models.

Local model auto-scan checks model-specific folders only, such as Hugging Face cache, LM Studio, Ollama, `~/models`, `C:/models`, and `C:/AI`. This avoids freezing the app by recursively scanning all Downloads and Documents. Use the optional folder field in Settings to scan a specific directory when needed.

## Test

```bash
pytest -q tests/test_nucleus.py
```

The tests use a fake adapter and do not call Pollinations.
