from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reharness.services import Harness


def init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    init_git(tmp_path)
    return tmp_path


@pytest.fixture
def harness(project_dir: Path) -> Harness:
    return Harness.initialize(project_dir, "Fixture Project", "Test project")
