from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .normalizer import normalize_text

SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", "__pycache__"}
MAX_FILE_SIZE = 2 * 1024 * 1024


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str


class GrepBackend:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def search(self, query: str, *, limit: int = 40, timeout: float = 3.0) -> list[GrepMatch]:
        if not query.strip() or limit <= 0:
            return []
        if shutil.which("rg"):
            try:
                return self._ripgrep(query, limit=limit, timeout=timeout)
            except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                pass
        return self._python_search(query, limit=limit)

    def _roots(self) -> list[Path]:
        candidates = [
            self.root / "harness-docs",
            self.root / "harness-artifacts" / "evidence",
            self.root / "docs",
            self.root / "src",
            self.root / "tests",
        ]
        return [path for path in candidates if path.exists() and path.is_dir() and not path.is_symlink()]

    def _ripgrep(self, query: str, *, limit: int, timeout: float) -> list[GrepMatch]:
        roots = self._roots()
        if not roots:
            return []
        command = [
            "rg",
            "--json",
            "--fixed-strings",
            "--ignore-case",
            "--no-messages",
            "--max-filesize",
            str(MAX_FILE_SIZE),
            "--glob",
            "!.git/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!build/**",
            "--glob",
            "!dist/**",
            "--",
            query,
            *[str(path) for path in roots],
        ]
        completed = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise subprocess.SubprocessError(completed.stderr.strip())
        matches: list[GrepMatch] = []
        for raw_line in completed.stdout.splitlines():
            event = json.loads(raw_line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            absolute = Path(data["path"]["text"]).resolve()
            try:
                relative = absolute.relative_to(self.root).as_posix()
            except ValueError:
                continue
            line = data["lines"]["text"].rstrip("\r\n")
            matches.append(GrepMatch(relative, int(data["line_number"]), line[:1000]))
            if len(matches) >= limit:
                break
        return matches

    def _python_search(self, query: str, *, limit: int) -> list[GrepMatch]:
        needle = normalize_text(query)
        matches: list[GrepMatch] = []
        for base in self._roots():
            for directory, names, files in os.walk(base, followlinks=False):
                names[:] = [name for name in names if name not in SKIP_DIRS]
                for filename in files:
                    path = Path(directory) / filename
                    if path.is_symlink():
                        continue
                    try:
                        if path.stat().st_size > MAX_FILE_SIZE:
                            continue
                        content = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for line_number, line in enumerate(content.splitlines(), 1):
                        if needle in normalize_text(line):
                            matches.append(
                                GrepMatch(path.relative_to(self.root).as_posix(), line_number, line[:1000])
                            )
                            if len(matches) >= limit:
                                return matches
        return matches
