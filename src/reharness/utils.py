from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIXES = {
    "project": "PRJ",
    "task": "TASK",
    "event": "EVT",
    "conclusion": "CON",
    "requirement": "REQ",
    "plan": "PLAN",
    "change": "CHG",
    "build": "BUILD",
    "test_spec": "TEST",
    "test_run": "TRUN",
    "evidence": "EVD",
    "snapshot": "SNP",
    "artifact": "ART",
    "relation": "REL",
    "audit": "AUD",
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(kind: str) -> str:
    prefix = PREFIXES[kind]
    return f"{prefix}-{uuid.uuid4().hex[:20].upper()}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_slug(value: str, max_length: int = 64) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    return value.strip("-")[:max_length] or "item"


def run_git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def git_snapshot(root: Path) -> dict[str, Any]:
    inside = run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        return {
            "repository": None,
            "commit": None,
            "branch": None,
            "dirty": False,
            "patch_hash": None,
            "untracked": [],
            "submodules": [],
        }

    commit = run_git(root, "rev-parse", "HEAD")
    branch = run_git(root, "branch", "--show-current")
    managed_exclusions = (
        ":(exclude).harness/**",
        ":(exclude)harness-artifacts/**",
        ":(exclude)harness-docs/**",
    )
    status = run_git(
        root, "status", "--porcelain=v1", "-uall", "--", ".", *managed_exclusions
    )
    diff = run_git(root, "diff", "--binary", "HEAD", "--", ".", *managed_exclusions)
    remote = run_git(root, "config", "--get", "remote.origin.url")
    submodules = run_git(root, "submodule", "status", "--recursive")

    untracked = []
    for line in status.stdout.splitlines():
        if line.startswith("?? "):
            relative = line[3:]
            if relative.startswith((".harness/", "harness-artifacts/", "harness-docs/")):
                continue
            path = root / relative
            if path.is_file():
                untracked.append(
                    {
                        "path": relative,
                        "sha256": sha256_file(path),
                        "size": path.stat().st_size,
                    }
                )
            else:
                untracked.append({"path": relative, "sha256": None, "size": None})

    patch_bytes = diff.stdout.encode("utf-8")
    return {
        "repository": remote.stdout.strip() or None,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()),
        "patch_hash": sha256_bytes(patch_bytes) if patch_bytes else None,
        "patch": diff.stdout if diff.stdout else None,
        "untracked": untracked,
        "submodules": [line.strip() for line in submodules.stdout.splitlines() if line.strip()],
    }


def dependency_lock_hash(root: Path) -> str | None:
    candidates = [
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.txt",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
    ]
    found = []
    for name in candidates:
        path = root / name
        if path.exists() and path.is_file():
            found.append((name, sha256_file(path)))
    if not found:
        return None
    return sha256_bytes(json_dumps(found).encode("utf-8"))


def environment_snapshot(root: Path) -> dict[str, Any]:
    env_names = sorted(
        key
        for key in os.environ
        if not any(token in key.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "KEY"))
    )
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version,
        "executable": sys.executable,
        "dependency_lock_hash": dependency_lock_hash(root),
        "environment_variable_names": env_names,
    }
