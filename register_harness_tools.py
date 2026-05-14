#!/usr/bin/env python3
"""Register all harness tools into the Ouroboros registry."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "nucleus"))
import server

tools = [
    ("delegation_router", "Recursively delegate tasks across hierarchy levels with title/scope isolation."),
    ("project_manager", "Discover, add, switch, and track project contexts."),
    ("context_link", "Cross-project context retrieval linking memories across universes."),
    ("report_builder", "Generate structured reports (markdown/html/json) saved as artefacts."),
    ("github_bridge", "GitHub API integration for repo/issue management."),
]

for name, desc in tools:
    result = server.register_tool(
        name=name,
        description=desc,
        filepath=f"tools/{name}",
    )
    ok = result.get("registered") or result.get("error", "?")
    print(f"[{'OK' if result.get('registered') else 'FAIL'}] {name}: {ok}")
