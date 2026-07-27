from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reharness.cli import app
from reharness.services import Harness, HarnessError, NotFoundError

runner = CliRunner()


def invoke_in(path: Path, args: list[str]):
    old = Path.cwd()
    os.chdir(path)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(old)


def test_project_update_and_query_methods(harness: Harness) -> None:
    project = harness.update_project(description="Updated", status="paused", default_branch="main")
    assert project.status == "paused"
    assert harness.project_data()["description"] == "Updated"
    with pytest.raises(HarnessError):
        harness.update_project(status="unknown")


def test_task_show_and_list(harness: Harness) -> None:
    task = harness.start_task("research", "query task")
    harness.add_task_step(task.id, "observation_recorded", "observed")
    data = harness.task_data(task.id)
    assert data["events"][-1]["summary"] == "observed"
    assert harness.list_tasks("in_progress")[0]["id"] == task.id
    with pytest.raises(NotFoundError):
        harness.task_data("TASK-MISSING")


def test_conclusion_and_requirement_show_list(harness: Harness) -> None:
    conclusion = harness.create_conclusion("query conclusion")
    requirement = harness.create_requirement("query requirement")
    assert harness.conclusion_data(conclusion.id)["claim"] == "query conclusion"
    assert harness.list_conclusions("exploring")[0]["id"] == conclusion.id
    assert harness.requirement_data(requirement.id)["description"] == "query requirement"
    assert harness.list_requirements("draft")[0]["id"] == requirement.id
    with pytest.raises(HarnessError):
        harness.list_conclusions("bad")
    with pytest.raises(HarnessError):
        harness.list_requirements("bad")


def test_pass_criteria_minimums_and_skips(harness: Harness) -> None:
    spec = harness.define_test(
        "Criteria",
        "regression",
        [sys.executable, "-c", "pass"],
        pass_criteria={"exit_code": 0, "min_total": 5, "min_passed": 4, "max_skipped": 0},
    )
    report = harness.root / "criteria.xml"
    report.write_text(
        '<testsuite tests="5" failures="0" errors="0" skipped="1"></testsuite>',
        encoding="utf-8",
    )
    run = harness.run_test(spec.id, junit_path=report)
    assert run.status == "failed"


def test_import_junit_does_not_execute_spec_command(harness: Harness) -> None:
    marker = harness.root / "should-not-exist"
    spec = harness.define_test(
        "Import only",
        "unit",
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
        pass_criteria={"exit_code": 0, "min_total": 2},
    )
    report = harness.root / "import.xml"
    report.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0"></testsuite>',
        encoding="utf-8",
    )
    run = harness.import_junit(spec.id, report)
    assert run.status == "passed"
    assert not marker.exists()
    assert harness.test_run_data(run.id)["passed"] == 2


def test_import_junit_missing_and_bad_ids(harness: Harness) -> None:
    spec = harness.define_test("Import", "unit", [sys.executable, "-c", "pass"])
    with pytest.raises(HarnessError):
        harness.import_junit(spec.id, Path("missing.xml"))
    report = harness.root / "report.xml"
    report.write_text('<testsuite tests="1" failures="0"/>', encoding="utf-8")
    with pytest.raises(NotFoundError):
        harness.import_junit("TEST-MISSING", report)


def test_cli_query_update_and_import(project_dir: Path) -> None:
    assert invoke_in(project_dir, ["init", "--name", "CLI"]).exit_code == 0
    update = invoke_in(project_dir, ["project", "update", "--status", "paused", "--json"])
    assert update.exit_code == 0
    assert json.loads(update.output)["status"] == "paused"

    task_result = invoke_in(
        project_dir, ["task", "start", "--type", "testing", "--goal", "query", "--json"]
    )
    task_id = json.loads(task_result.output)["id"]
    assert invoke_in(project_dir, ["task", "show", task_id, "--json"]).exit_code == 0
    listed = invoke_in(project_dir, ["task", "list", "--status", "in_progress", "--json"])
    assert json.loads(listed.output)[0]["id"] == task_id

    define = invoke_in(
        project_dir,
        [
            "test",
            "define",
            "--name",
            "import",
            "--type",
            "unit",
            "--command",
            sys.executable,
            "--command",
            "-c",
            "--command",
            "pass",
            "--json",
        ],
    )
    assert define.exit_code == 0, define.output
    spec_id = json.loads(define.output)["id"]
    report = project_dir / "cli-junit.xml"
    report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    imported = invoke_in(
        project_dir, ["test", "import", spec_id, "--junit", str(report), "--json"]
    )
    assert imported.exit_code == 0, imported.output
    run_id = json.loads(imported.output)["id"]
    shown = invoke_in(project_dir, ["test", "show", run_id, "--json"])
    assert json.loads(shown.output)["status"] == "passed"
