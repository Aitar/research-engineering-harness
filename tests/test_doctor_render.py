from __future__ import annotations

from pathlib import Path

from reharness.services import Harness


def test_doctor_clean_project(harness: Harness) -> None:
    assert harness.doctor() == []


def test_render_all_contains_task_requirement_and_conclusion(harness: Harness) -> None:
    task = harness.start_task("research", "Study behavior")
    req = harness.create_requirement("Provide behavior")
    conclusion = harness.create_conclusion("Behavior is stable")
    harness.refresh()
    assert (harness.root / "harness-docs" / "tasks" / f"{task.id}.md").exists()
    assert (harness.root / "harness-docs" / "requirements" / f"{req.id}.md").exists()
    assert (harness.root / "harness-docs" / "conclusions" / f"{conclusion.id}.md").exists()
    brief = (harness.root / "harness-docs" / "project-brief.md").read_text(encoding="utf-8")
    assert task.id in brief and req.id in brief and conclusion.id in brief


def test_doctor_detects_missing_brief(harness: Harness) -> None:
    (harness.root / "harness-docs" / "project-brief.md").unlink()
    findings = harness.doctor()
    assert any(f["code"] == "BRIEF_MISSING" for f in findings)
