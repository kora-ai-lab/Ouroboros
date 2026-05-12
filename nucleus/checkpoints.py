from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHECKPOINTS_SUBDIR = "checkpoints"
_WRITE_MODES = {"w", "a", "x", "+"}
_PATHLIKE_MUTATION_CALLS = {
    "write_text",
    "write_bytes",
    "unlink",
    "rmdir",
    "remove",
    "rename",
    "replace",
    "touch",
}
_OS_PATH_ARG_MUTATIONS = {"remove", "unlink", "rmdir", "rename", "replace"}


def checkpoints_dir(data_dir: Path) -> Path:
    return data_dir / CHECKPOINTS_SUBDIR


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkpoint_id() -> str:
    return f"cp-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _metadata_path(data_dir: Path, checkpoint_id: str) -> Path:
    return checkpoints_dir(data_dir) / f"{checkpoint_id}.json"


def _entry_backup_path(work_dir: Path, index: int, source: Path) -> Path:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    suffix = source.name or "path"
    return work_dir / f"{index:04d}-{digest}-{suffix}"


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _path_from_call(node: ast.Call, root_dir: Path) -> Path | None:
    if node.args:
        text = _literal_string(node.args[0])
        if text:
            return resolve_checkpoint_path(text, root_dir)
    return None


def _path_from_path_constructor(node: ast.AST, root_dir: Path) -> Path | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "Path":
        return _path_from_call(node, root_dir)
    if isinstance(func, ast.Attribute) and func.attr == "Path":
        return _path_from_call(node, root_dir)
    return None


def _mode_is_write(mode: str) -> bool:
    return any(flag in mode for flag in _WRITE_MODES)


def _open_call_target(node: ast.Call, root_dir: Path) -> Path | None:
    func = node.func
    is_builtin_open = isinstance(func, ast.Name) and func.id == "open"
    is_path_open = isinstance(func, ast.Attribute) and func.attr == "open"
    if not is_builtin_open and not is_path_open:
        return None

    mode = "r"
    if is_builtin_open:
        if len(node.args) > 1:
            mode = _literal_string(node.args[1]) or mode
        target = _path_from_call(node, root_dir)
    else:
        if node.args:
            mode = _literal_string(node.args[0]) or mode
        target = _path_from_path_constructor(func.value, root_dir)
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = _literal_string(keyword.value) or mode
    if not _mode_is_write(mode):
        return None
    return target


def resolve_checkpoint_path(path_text: str, root_dir: Path) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = root_dir / path
    return path.resolve()


def infer_affected_files(code: str, root_dir: Path) -> list[Path]:
    """Best-effort static inference for literal file paths mutated by Python code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    paths: list[Path] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        open_target = _open_call_target(node, root_dir)
        if open_target is not None:
            paths.append(open_target)
            continue

        if isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            if func_name in _PATHLIKE_MUTATION_CALLS:
                target = _path_from_path_constructor(node.func.value, root_dir)
                if target is not None:
                    paths.append(target)
                if func_name in {"rename", "replace"} and node.args:
                    dest = _literal_string(node.args[0])
                    if dest:
                        paths.append(resolve_checkpoint_path(dest, root_dir))
            if func_name in _OS_PATH_ARG_MUTATIONS:
                target = _path_from_call(node, root_dir)
                if target is not None:
                    paths.append(target)
                if func_name in {"rename", "replace"} and len(node.args) > 1:
                    dest = _literal_string(node.args[1])
                    if dest:
                        paths.append(resolve_checkpoint_path(dest, root_dir))

    unique: dict[str, Path] = {}
    for path in paths:
        unique[str(path)] = path
    return list(unique.values())


def _copy_path(source: Path, destination: Path) -> str:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
        return "directory"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)
    return "file"


def _git_available(root_dir: Path) -> bool:
    return (root_dir / ".git").exists() and subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


def _git_snapshot_paths(root_dir: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root_dir,
        capture_output=True,
        check=False,
    ).stdout.split(b"\0")
    others = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root_dir,
        capture_output=True,
        check=False,
    ).stdout.split(b"\0")
    paths: list[Path] = []
    for raw in tracked + others:
        if not raw:
            continue
        text = raw.decode("utf-8", errors="surrogateescape")
        path = (root_dir / text).resolve()
        if path.exists() and ".git" not in path.parts:
            paths.append(path)
    return paths


def create_checkpoint(
    affected_paths: list[Path] | None,
    *,
    root_dir: Path,
    data_dir: Path,
    reason: str,
    code: str | None = None,
) -> dict[str, Any]:
    """Create a restorable checkpoint for paths, or a Git-aware repo snapshot."""
    root_dir = root_dir.resolve()
    data_dir = data_dir.resolve()
    checkpoint_id = _checkpoint_id()
    base_dir = checkpoints_dir(data_dir)
    work_dir = base_dir / checkpoint_id
    files_dir = work_dir / "files"
    base_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    explicit_paths = list(affected_paths or [])
    strategy = "files" if explicit_paths else "git_repo" if _git_available(root_dir) else "none"
    snapshot_paths = explicit_paths or (_git_snapshot_paths(root_dir) if strategy == "git_repo" else [])

    entries: list[dict[str, Any]] = []
    for index, source in enumerate(snapshot_paths):
        source = source.resolve()
        if checkpoints_dir(data_dir).resolve() == source or checkpoints_dir(data_dir).resolve() in source.parents:
            continue
        entry: dict[str, Any] = {"path": str(source), "existed": source.exists()}
        if source.exists():
            backup_path = _entry_backup_path(files_dir, index, source)
            entry["kind"] = _copy_path(source, backup_path)
            entry["backup"] = str(backup_path.relative_to(base_dir))
        entries.append(entry)

    metadata: dict[str, Any] = {
        "id": checkpoint_id,
        "created_at": _utc_now(),
        "reason": reason,
        "strategy": strategy,
        "root_dir": str(root_dir),
        "affected_paths_inferred": bool(explicit_paths),
        "path_count": len(entries),
        "paths": entries,
    }
    if code is not None:
        metadata["code_preview"] = code[:1000]
    if strategy == "git_repo":
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode == 0:
            metadata["git_head"] = head.stdout.strip()
    _metadata_path(data_dir, checkpoint_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def latest_checkpoint(data_dir: Path) -> dict[str, Any] | None:
    base_dir = checkpoints_dir(data_dir)
    if not base_dir.exists():
        return None
    metadata_files = sorted(base_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not metadata_files:
        return None
    return json.loads(metadata_files[0].read_text(encoding="utf-8"))


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _current_repo_files(root_dir: Path, checkpoints_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root_dir.rglob("*"):
        resolved = path.resolve()
        if path.name == ".git" or ".git" in path.parts:
            continue
        if checkpoints_root == resolved or checkpoints_root in resolved.parents:
            continue
        if resolved.is_file() or resolved.is_symlink():
            files.append(resolved)
    return files


def restore_checkpoint(checkpoint: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    base_dir = checkpoints_dir(data_dir).resolve()
    restored: list[str] = []
    removed: list[str] = []
    checkpoint_paths = {str(Path(entry["path"]).resolve()) for entry in checkpoint.get("paths", [])}

    if checkpoint.get("strategy") == "git_repo":
        root_dir = Path(checkpoint.get("root_dir", ".")).resolve()
        for current in _current_repo_files(root_dir, base_dir):
            if str(current) not in checkpoint_paths:
                _remove_path(current)
                removed.append(str(current))

    for entry in checkpoint.get("paths", []):
        target = Path(entry["path"]).resolve()
        if entry.get("existed"):
            backup = base_dir / str(entry["backup"])
            if target.exists():
                _remove_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.get("kind") == "directory":
                shutil.copytree(backup, target, symlinks=True)
            else:
                shutil.copy2(backup, target, follow_symlinks=False)
            restored.append(str(target))
        elif target.exists():
            _remove_path(target)
            removed.append(str(target))
    return {"restored": restored, "removed": removed}


def restore_latest_checkpoint(*, data_dir: Path) -> dict[str, Any]:
    checkpoint = latest_checkpoint(data_dir)
    if checkpoint is None:
        return {"error": "No checkpoint is available.", "restored": [], "removed": []}
    result = restore_checkpoint(checkpoint, data_dir=data_dir)
    result["checkpoint"] = checkpoint
    return result
