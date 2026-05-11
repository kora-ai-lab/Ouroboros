# PROGRESS - Ouroboros

Updated: 2026-05-10

## Current Direction
The Python nucleus is now the primary implementation path.

Built under `nucleus/`:
- FastAPI server
- Static HTML frontend
- SQLite episodic memory
- `facts.json`
- `registry.json`
- Built-in `execute_python`
- Built-in `register_tool`
- Upload parsing
- Root scripts for nucleus run and tests

The previous Tauri app remains as legacy and should not be deleted unless Kheir explicitly asks.

## Legacy Tauri Status
The older Tauri v2 implementation remains in place with its source, docs, and build artifacts. It is not the active target for new work unless explicitly revived.
