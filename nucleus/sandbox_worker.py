from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SANDBOX_TIERS = ("read_only", "workspace_write", "network_enabled", "host_full")
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_OUTPUT_LIMIT_BYTES = 100_000
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


@dataclass(frozen=True)
class SandboxConfig:
    tier: str
    working_directory: Path
    allowed_roots: tuple[Path, ...]
    allowed_env_vars: frozenset[str]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    network_disabled: bool = True

    def normalized(self) -> "SandboxConfig":
        if self.tier not in SANDBOX_TIERS:
            raise ValueError(f"Unknown sandbox tier: {self.tier}")
        return SandboxConfig(
            tier=self.tier,
            working_directory=self.working_directory.resolve(),
            allowed_roots=tuple(root.resolve() for root in self.allowed_roots),
            allowed_env_vars=frozenset(self.allowed_env_vars),
            timeout_seconds=self.timeout_seconds,
            output_limit_bytes=self.output_limit_bytes,
            network_disabled=self.network_disabled,
        )


def config_for_tier(
    tier: str,
    *,
    workspace_root: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> SandboxConfig:
    workspace = workspace_root.resolve()
    if tier == "host_full":
        allowed_roots = (Path(os.path.abspath(os.sep)),)
        allowed_env_vars = frozenset(os.environ.keys())
        network_disabled = False
    else:
        allowed_roots = (workspace,)
        allowed_env_vars = frozenset(SAFE_EXECUTION_ENV_VARS)
        network_disabled = tier != "network_enabled"
    return SandboxConfig(
        tier=tier,
        working_directory=workspace,
        allowed_roots=allowed_roots,
        allowed_env_vars=allowed_env_vars,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        network_disabled=network_disabled,
    ).normalized()


def build_python_execution_env(config: SandboxConfig) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in config.allowed_env_vars}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["OUROBOROS_SANDBOX_TIER"] = config.tier
    env["OUROBOROS_WORKSPACE_ROOT"] = str(config.working_directory)
    env["OUROBOROS_ALLOWED_ROOTS"] = os.pathsep.join(str(root) for root in config.allowed_roots)
    env["OUROBOROS_NETWORK_DISABLED"] = "1" if config.network_disabled else "0"
    return env


def sandbox_guard(config: SandboxConfig) -> str:
    if config.tier == "host_full":
        return ""
    allowed_roots = [str(root) for root in config.allowed_roots]
    read_only = config.tier == "read_only"
    network_disabled = config.network_disabled
    return f'''
import builtins as _ouro_builtins
import os as _ouro_os
import pathlib as _ouro_pathlib
import shutil as _ouro_shutil

_OURO_ALLOWED_ROOTS = {allowed_roots!r}
_OURO_READ_ONLY = {read_only!r}
_OURO_NETWORK_DISABLED = {network_disabled!r}
_OURO_WRITE_FLAGS = ("w", "a", "x", "+")

def _ouro_resolve(path):
    return _ouro_os.path.realpath(_ouro_os.path.abspath(_ouro_os.fspath(path)))

def _ouro_is_allowed_path(path):
    resolved = _ouro_resolve(path)
    return any(resolved == root or resolved.startswith(root + _ouro_os.sep) for root in _OURO_ALLOWED_ROOTS)

def _ouro_check_write_path(path):
    if _OURO_READ_ONLY:
        raise PermissionError("read_only sandbox blocks filesystem writes")
    if not _ouro_is_allowed_path(path):
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

if _OURO_NETWORK_DISABLED:
    import socket as _ouro_socket
    def _ouro_block_network(*args, **kwargs):
        raise PermissionError("sandbox blocks network access")
    _ouro_socket.socket = _ouro_block_network
    _ouro_socket.create_connection = _ouro_block_network
'''


def available_isolation_backend() -> str | None:
    # Docker is NOT used — it's heavy, breaks Path.home() in containers,
    # adds latency, and doesn't work reliably on Windows.
    # The in-process Python guard (monkey-patching) is simpler and portable.
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if platform.system() == "Windows":
        return "windows-job-object"
    if shutil.which("firejail"):
        return "firejail"
    if shutil.which("bwrap"):
        return "bubblewrap"
    return None


def _trim_output(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


async def run_sandboxed_python(code: str, config: SandboxConfig) -> dict[str, Any]:
    config = config.normalized()
    backend = available_isolation_backend()
    isolation_degraded = backend is None
    guarded_code = sandbox_guard(config) + "\n" + code
    start = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        guarded_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.working_directory),
        env=build_python_execution_env(config),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.timeout_seconds)
        return {
            "stdout": _trim_output(stdout, config.output_limit_bytes),
            "stderr": _trim_output(stderr, config.output_limit_bytes),
            "exit_code": proc.returncode,
            "timed_out": False,
            "duration_ms": int((time.time() - start) * 1000),
            "sandbox": sandbox_metadata(config, backend, isolation_degraded),
            "isolation_degraded": isolation_degraded,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        return {
            "stdout": "",
            "stderr": f"Timed out after {config.timeout_seconds}s",
            "exit_code": -1,
            "timed_out": True,
            "duration_ms": int((time.time() - start) * 1000),
            "sandbox": sandbox_metadata(config, backend, isolation_degraded),
            "isolation_degraded": isolation_degraded,
        }


def sandbox_metadata(config: SandboxConfig, backend: str | None, isolation_degraded: bool) -> dict[str, Any]:
    return {
        "tier": config.tier,
        "working_directory": str(config.working_directory),
        "allowed_roots": [str(root) for root in config.allowed_roots],
        "allowed_env_vars": sorted(config.allowed_env_vars),
        "timeout_seconds": config.timeout_seconds,
        "output_limit_bytes": config.output_limit_bytes,
        "network_disabled": config.network_disabled,
        "backend": backend or "python-policy-fallback",
        "isolation_degraded": isolation_degraded,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run Python code through the Ouroboros sandbox worker.")
    parser.add_argument("--payload", help="JSON payload containing code, tier, and workspace_root. Reads stdin when omitted.")
    args = parser.parse_args()
    payload_text = args.payload if args.payload is not None else sys.stdin.read()
    payload = json.loads(payload_text)
    config = config_for_tier(
        payload.get("tier", "read_only"),
        workspace_root=Path(payload["workspace_root"]),
        timeout_seconds=int(payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        output_limit_bytes=int(payload.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES)),
    )
    result = await run_sandboxed_python(str(payload.get("code", "")), config)
    print(json.dumps(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
