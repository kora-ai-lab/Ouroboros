from __future__ import annotations

import json, os, sys
from pathlib import Path

try:
    _home = Path.home()
except (RuntimeError, OSError):
    _home = Path.cwd()
WORKSPACE_FILE = _home / ".ouroboros" / "workspace.json"


def _load() -> dict:
    if WORKSPACE_FILE.exists():
        return json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
    return {"projects": {}, "current": None}


def _save(data: dict) -> None:
    WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_projects() -> dict:
    data = _load()
    return {"projects": list(data["projects"].keys()), "current": data.get("current")}


def add_project(name: str, path: str) -> dict:
    data = _load()
    resolved = str(Path(path).resolve())
    data["projects"][name] = {"root": resolved, "github": None, "description": ""}
    _save(data)
    return {"added": name, "root": resolved}


def switch_project(name: str) -> dict:
    data = _load()
    if name not in data["projects"]:
        return {"error": f"Unknown project: {name}"}
    data["current"] = name
    _save(data)
    return {"switched": name, "project": data["projects"][name]}


def sync_project(name: str) -> dict:
    data = _load()
    if name not in data["projects"]:
        return {"error": f"Unknown project: {name}"}
    proj = data["projects"][name]
    root = Path(proj["root"])
    if not root.exists():
        return {"error": f"Root not found: {proj['root']}"}
    dirs = [d.name for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    files = [f.name for f in sorted(root.iterdir()) if f.is_file() and not f.name.startswith(".")]
    return {"synced": name, "root": proj["root"], "dirs": dirs[:20], "files": files[:20]}


def discover_projects() -> dict:
    found = {}
    for entry in Path(".").iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            git_dir = entry / ".git"
            if git_dir.exists():
                found[entry.name] = str(entry.resolve())
    data = _load()
    for name, root in found.items():
        if name not in data["projects"]:
            data["projects"][name] = {"root": root, "github": None, "description": ""}
    _save(data)
    return {"discovered": len(found), "projects": list(found.keys())}


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    action = str(args.get("action", "list"))
    if action == "list":
        result = list_projects()
    elif action == "add":
        result = add_project(str(args.get("name", "")), str(args.get("path", "")))
    elif action == "switch":
        result = switch_project(str(args.get("name", "")))
    elif action == "sync":
        result = sync_project(str(args.get("name", "")))
    elif action == "discover":
        result = discover_projects()
    else:
        result = {"error": f"Unknown action: {action}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
