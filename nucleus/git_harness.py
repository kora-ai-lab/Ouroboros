"""Reusable Git harness for Ouroboros agents.

The functions in this module are intentionally plain Python helpers, not built-in
special-case tools. Agents can call them through ``execute_python`` or create thin
registered wrappers in ``nucleus/tools/`` when a workflow needs reusable Git tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_TIMEOUT_SECONDS = 30


class GitHarnessError(RuntimeError):
    """Raised when a Git command fails and the caller requested an exception."""


def _repo_path(repo: str | Path = ".") -> Path:
    return Path(repo).expanduser().resolve()


def _run_git(
    repo: str | Path,
    args: Sequence[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    check: bool = False,
) -> dict[str, Any]:
    """Run a Git command in ``repo`` and return a serializable result."""
    repo_path = _repo_path(repo)
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    result: dict[str, Any] = {
        "command": ["git", *args],
        "repo": str(repo_path),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "ok": completed.returncode == 0,
    }
    if check and completed.returncode != 0:
        raise GitHarnessError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def _git_output(repo: str | Path, args: Sequence[str], *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    result = _run_git(repo, args, timeout=timeout, check=True)
    return str(result["stdout"])


def _current_branch(repo: str | Path) -> str:
    name = _run_git(repo, ["branch", "--show-current"])
    if name["ok"] and name["stdout"].strip():
        return name["stdout"].strip()
    head = _run_git(repo, ["rev-parse", "--short", "HEAD"])
    if head["ok"]:
        return f"HEAD detached at {head['stdout'].strip()}"
    return "unknown"


def _parse_porcelain_v1(output: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status_code = line[:2]
        path_text = line[3:]
        old_path = ""
        new_path = path_text
        if " -> " in path_text:
            old_path, new_path = path_text.split(" -> ", 1)
        files.append(
            {
                "path": new_path.strip('"'),
                "old_path": old_path.strip('"'),
                "index_status": status_code[0],
                "worktree_status": status_code[1],
                "status": status_code,
            }
        )
    return files


def _normalize_paths(paths: Iterable[str] | None) -> list[str]:
    return [str(path) for path in paths or []]


def status(repo: str | Path = ".") -> dict[str, Any]:
    """Return branch, cleanliness, and porcelain status for a repository."""
    porcelain = _run_git(repo, ["status", "--porcelain=v1", "--branch"])
    files_output = _run_git(repo, ["status", "--porcelain=v1"])
    files = _parse_porcelain_v1(files_output["stdout"]) if files_output["ok"] else []
    return {
        "repo": str(_repo_path(repo)),
        "branch": _current_branch(repo),
        "clean": porcelain["ok"] and not files,
        "files": files,
        "stdout": porcelain["stdout"],
        "stderr": porcelain["stderr"],
        "exit_code": porcelain["exit_code"],
        "ok": porcelain["ok"],
    }


def diff(
    repo: str | Path = ".",
    *,
    staged: bool = False,
    paths: Iterable[str] | None = None,
    context_lines: int = 3,
) -> dict[str, Any]:
    """Return a unified diff for worktree or staged changes."""
    args = ["diff", f"--unified={context_lines}"]
    if staged:
        args.append("--staged")
    path_list = _normalize_paths(paths)
    if path_list:
        args.extend(["--", *path_list])
    result = _run_git(repo, args)
    result["staged"] = staged
    result["paths"] = path_list
    return result


def branch(repo: str | Path = ".", *, all_branches: bool = False) -> dict[str, Any]:
    """Return the current branch and local/all branch listing."""
    args = ["branch", "--list"]
    if all_branches:
        args.insert(1, "--all")
    result = _run_git(repo, args)
    branches = [line.replace("*", "", 1).strip() for line in result["stdout"].splitlines() if line.strip()]
    return {
        **result,
        "current": _current_branch(repo),
        "branches": branches,
        "all": all_branches,
    }


def list_changed_files(repo: str | Path = ".") -> dict[str, Any]:
    """Return changed file records parsed from ``git status --porcelain``."""
    result = _run_git(repo, ["status", "--porcelain=v1"])
    files = _parse_porcelain_v1(result["stdout"]) if result["ok"] else []
    return {**result, "files": files, "paths": [item["path"] for item in files]}


def commit(
    message: str,
    repo: str | Path = ".",
    *,
    all_changes: bool = True,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Create a checkpoint commit and return before/after status snapshots.

    By default all tracked, untracked, and deleted files are staged first with
    ``git add -A`` so the commit represents a complete repository checkpoint.
    """
    if not message.strip():
        raise ValueError("Commit message must not be empty.")

    before = status(repo)
    staged = None
    if all_changes:
        staged = _run_git(repo, ["add", "-A"])
        if not staged["ok"]:
            return {"ok": False, "stage": staged, "before": before, "after": status(repo)}

    pending = status(repo)
    if pending["clean"] and not allow_empty:
        return {"ok": True, "committed": False, "reason": "no changes", "before": before, "after": pending}

    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    committed = _run_git(repo, args)
    after = status(repo)
    head = _run_git(repo, ["rev-parse", "HEAD"])
    return {
        "ok": committed["ok"],
        "committed": committed["ok"],
        "commit": committed,
        "stage": staged,
        "hash": head["stdout"].strip() if head["ok"] else "",
        "before": before,
        "after": after,
    }


def restore(
    repo: str | Path = ".",
    *,
    paths: Iterable[str] | None = None,
    staged: bool = False,
    worktree: bool = True,
) -> dict[str, Any]:
    """Restore tracked paths from HEAD/index using ``git restore``.

    ``paths`` is required to avoid accidentally discarding the whole worktree.
    """
    path_list = _normalize_paths(paths)
    if not path_list:
        raise ValueError("restore requires at least one explicit path.")
    args = ["restore"]
    if staged:
        args.append("--staged")
    if worktree:
        args.append("--worktree")
    args.extend(["--", *path_list])
    result = _run_git(repo, args)
    return {**result, "paths": path_list, "after": status(repo)}
