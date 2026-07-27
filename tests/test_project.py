from __future__ import annotations

from pathlib import Path

import pytest

from reharness.services import Harness, HarnessError, discover_root


def test_initialize_creates_database_docs_and_artifacts(project_dir: Path) -> None:
    harness = Harness.initialize(project_dir, "Demo", "Description")
    assert (project_dir / ".harness" / "harness.db").exists()
    assert (project_dir / ".harness" / "config.yaml").exists()
    assert (project_dir / "harness-docs" / "project-brief.md").exists()
    assert (project_dir / "harness-artifacts").is_dir()
    assert harness.project_data()["name"] == "Demo"


def test_initialize_twice_fails(project_dir: Path) -> None:
    Harness.initialize(project_dir, "Demo")
    with pytest.raises(HarnessError):
        Harness.initialize(project_dir, "Again")


def test_discover_root_from_child(harness: Harness) -> None:
    child = harness.root / "a" / "b"
    child.mkdir(parents=True)
    assert discover_root(child) == harness.root


def test_brief_and_context_are_bounded(harness: Harness) -> None:
    task = harness.start_task("research", "Investigate cache latency")
    conclusion = harness.create_conclusion("Cache may reduce latency")
    brief = harness.brief()
    context = harness.context("cache", budget=800)
    assert task.id in brief
    assert conclusion.id in brief
    assert "Related conclusions" in context
    assert len(context) <= 800


def test_context_rejects_tiny_budget(harness: Harness) -> None:
    with pytest.raises(HarnessError):
        harness.context(budget=100)


def test_summary_counts(harness: Harness) -> None:
    harness.start_task("research", "One")
    harness.create_conclusion("Two")
    data = harness.summary()
    assert data["tasks"] == 1
    assert data["conclusions"] == 1
