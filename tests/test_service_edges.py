from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from reharness import models
from reharness.db import init_database, session_scope
from reharness.models import Conclusion, Project, Requirement, Task
from reharness.services import (
    Harness,
    HarnessError,
    NotFoundError,
    StateTransitionError,
    as_list,
    discover_root,
    read_structured_file,
)
from reharness.utils import dependency_lock_hash, git_snapshot, json_loads, safe_slug


def test_structured_helpers_and_root_errors(tmp_path: Path) -> None:
    yaml_file = tmp_path / "items.yaml"
    yaml_file.write_text("- one\n- two\n", encoding="utf-8")
    text_file = tmp_path / "items.txt"
    text_file.write_text("- alpha\n\nbeta\n", encoding="utf-8")

    assert read_structured_file(yaml_file) == ["one", "two"]
    assert read_structured_file(text_file).startswith("- alpha")
    assert as_list(None) == []
    assert as_list([1, "two"]) == ["1", "two"]
    assert as_list(text_file.read_text(encoding="utf-8")) == ["alpha", "beta"]
    with pytest.raises(HarnessError):
        as_list({"bad": "shape"})
    with pytest.raises(HarnessError):
        discover_root(tmp_path)

    assert json_loads(None, {"default": True}) == {"default": True}
    assert safe_slug("  Hello, 世界!  ") == "hello-世界"
    assert git_snapshot(tmp_path)["commit"] is None
    (tmp_path / "requirements.txt").write_text("pytest==9\n", encoding="utf-8")
    assert dependency_lock_hash(tmp_path)


def test_missing_project_and_relation_validation(harness: Harness, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    init_database(bare)
    with pytest.raises(HarnessError):
        Harness(bare).project_data()

    with session_scope(harness.root) as session:
        project = session.scalar(select(Project))
        assert project is not None
        with pytest.raises(HarnessError):
            harness._add_relation(session, "project", project.id, "unknown", "project", project.id)
        with pytest.raises(NotFoundError):
            harness._add_relation(session, "task", "TASK-MISSING", "depends_on", "project", project.id)
        with pytest.raises(NotFoundError):
            harness._add_relation(session, "project", project.id, "depends_on", "task", "TASK-MISSING")


def test_brief_context_and_task_edge_paths(harness: Harness) -> None:
    assert "Project Brief" in harness.brief("full")
    assert len(harness.brief("compact").splitlines()) <= 50
    with pytest.raises(HarnessError):
        harness.brief("tiny")
    with pytest.raises(HarnessError):
        harness.start_task("research", "   ")
    with pytest.raises(NotFoundError):
        harness.start_task("research", "goal", requirement_ids=["REQ-MISSING"])

    req = harness.create_requirement("Indexed requirement")
    harness.transition_requirement(req.id, "accepted")
    task = harness.start_task("development", "Implement indexed requirement", requirement_ids=[req.id])
    assert harness.requirement_data(req.id)["status"] == "in_progress"
    assert req.id in harness.context("indexed", 3000)

    with pytest.raises(NotFoundError):
        harness.add_task_step("TASK-MISSING", "observation_recorded", "missing")
    with pytest.raises(NotFoundError):
        harness.add_task_step(task.id, "observation_recorded", "missing evidence", evidence_id="EVD-MISSING")
    with pytest.raises(NotFoundError):
        harness.complete_task("TASK-MISSING", True, "done")


def test_evidence_and_command_edge_paths(harness: Harness, tmp_path: Path) -> None:
    with pytest.raises(HarnessError):
        harness.capture_evidence(tmp_path / "missing.txt", "experiment_result")
    with pytest.raises(NotFoundError):
        harness.capture_evidence(harness.root / "README.md", "experiment_result", "TASK-MISSING")
    with pytest.raises(HarnessError):
        harness.run_command("TASK-MISSING", [])
    with pytest.raises(NotFoundError):
        harness.run_command("TASK-MISSING", [sys.executable, "-c", "pass"])
    with pytest.raises(NotFoundError):
        harness.evidence_data("EVD-MISSING")
    with pytest.raises(NotFoundError):
        harness.verify_evidence("EVD-MISSING")

    task = harness.start_task("testing", "Capture glob and environment")
    result = harness.run_command(
        task.id,
        [sys.executable, "-c", "import os; from pathlib import Path; Path('captured-1.txt').write_text(os.environ['HARNESS_TEST'])"],
        [Path("captured-*.txt")],
        env={"HARNESS_TEST": "yes"},
    )
    assert len(result["captured_evidence_ids"]) == 1

    with session_scope(harness.root) as session:
        project = session.scalar(select(Project))
        assert project is not None
        external = tmp_path / "external.txt"
        external.write_text("external", encoding="utf-8")
        evidence = harness._capture_file_in_session(session, project, external, "experiment_result", copy=False)
        assert evidence.storage_uri == "external.txt"


def test_conclusion_and_requirement_error_paths(harness: Harness) -> None:
    with pytest.raises(HarnessError):
        harness.create_conclusion("   ")
    with pytest.raises(HarnessError):
        harness._transition_conclusion("CON-MISSING", "invalid")
    with pytest.raises(NotFoundError):
        harness.support_conclusion("CON-MISSING", ["EVD-MISSING"])

    conclusion = harness.create_conclusion("Claim")
    with pytest.raises(NotFoundError):
        harness.support_conclusion(conclusion.id, ["EVD-MISSING"])
    with pytest.raises(HarnessError):
        harness._transition_conclusion(conclusion.id, "superseded")
    support_file = harness.root / "support.txt"
    support_file.write_text("support", encoding="utf-8")
    support = harness.capture_evidence(support_file, "experiment_result")
    harness.support_conclusion(conclusion.id, [support.id])
    with pytest.raises(NotFoundError):
        harness.supersede_conclusion(conclusion.id, "CON-MISSING")

    with pytest.raises(HarnessError):
        harness.create_requirement("  ")
    with pytest.raises(HarnessError):
        harness.transition_requirement("REQ-MISSING", "unknown")
    with pytest.raises(StateTransitionError):
        harness.transition_requirement("REQ-MISSING", "verified")
    with pytest.raises(NotFoundError):
        harness.transition_requirement("REQ-MISSING", "accepted")

    req = harness.create_requirement("Requirement")
    with pytest.raises(StateTransitionError):
        harness.transition_requirement(req.id, "implemented")
    with pytest.raises(HarnessError):
        harness.add_requirement_plan(req.id, "  ")
    with pytest.raises(NotFoundError):
        harness.add_requirement_plan("REQ-MISSING", "plan")
    with pytest.raises(NotFoundError):
        harness.conclusion_data("CON-MISSING")
    with pytest.raises(NotFoundError):
        harness.requirement_data("REQ-MISSING")
    with pytest.raises(NotFoundError):
        harness.test_run_data("TRUN-MISSING")


def test_change_build_and_test_edge_paths(harness: Harness) -> None:
    with pytest.raises(HarnessError):
        harness.capture_change("not-a-ref", "also-not-a-ref")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=harness.root, text=True, capture_output=True, check=True
    ).stdout.strip()
    with pytest.raises(NotFoundError):
        harness.capture_change(head, head, task_id="TASK-MISSING")
    with pytest.raises(NotFoundError):
        harness.capture_change(head, head, requirement_ids=["REQ-MISSING"])

    artifact = harness.root / "artifact.bin"
    artifact.write_bytes(b"artifact")
    with pytest.raises(HarnessError):
        harness.capture_build(artifact, status="invalid")
    with pytest.raises(NotFoundError):
        harness.capture_build(artifact, change_id="CHG-MISSING")
    with pytest.raises(HarnessError):
        harness.define_test("Empty", "unit", [])
    with pytest.raises(NotFoundError):
        harness.run_test("TEST-MISSING")

    spec = harness.define_test("Pass", "unit", [sys.executable, "-c", "pass"])
    with pytest.raises(NotFoundError):
        harness.run_test(spec.id, task_id="TASK-MISSING")
    with pytest.raises(NotFoundError):
        harness.run_test(spec.id, build_id="BUILD-MISSING")

    task = harness.start_task("testing", "Run linked test")
    build = harness.capture_build(artifact)
    assert harness.run_test(spec.id, task.id, build.id).status == "passed"

    timeout_spec = harness.define_test(
        "Timeout", "unit", [sys.executable, "-c", "import time; time.sleep(1)"]
    )
    assert harness.run_test(timeout_spec.id, timeout=0.01).status == "error"

    suites = harness.root / "suites.xml"
    suites.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" disabled="1"/></testsuites>',
        encoding="utf-8",
    )
    imported = harness.import_junit(spec.id, suites, task.id, build.id)
    assert imported.skipped_count == 1
    with pytest.raises(NotFoundError):
        harness.import_junit(spec.id, suites, task_id="TASK-MISSING")
    with pytest.raises(NotFoundError):
        harness.import_junit(spec.id, suites, build_id="BUILD-MISSING")

    status, checks = harness._evaluate_test_result(
        {"exit_code": 0, "max_failures": 1},
        0,
        {"total": 2, "passed": 1, "failures": 1, "errors": 0, "skipped": 0},
        None,
    )
    assert status == "passed"
    assert any(check["criterion"] == "max_failures" for check in checks)


def test_doctor_finds_corrupted_formal_state(harness: Harness) -> None:
    conclusion = harness.create_conclusion("Formal claim")
    requirement = harness.create_requirement("Formal requirement")
    task = harness.start_task("research", "Finished without summary")
    spec = harness.define_test("No criteria", "unit", [sys.executable, "-c", "pass"])

    with session_scope(harness.root) as session:
        stored_conclusion = session.get(Conclusion, conclusion.id)
        stored_requirement = session.get(Requirement, requirement.id)
        stored_task = session.get(Task, task.id)
        stored_spec = session.get(models.TestSpec, spec.id)
        assert stored_conclusion and stored_requirement and stored_task and stored_spec
        stored_conclusion.status = "supported"
        stored_requirement.status = "verified"
        stored_task.status = "succeeded"
        stored_task.result_summary = None
        stored_spec.pass_criteria_json = "{}"

    codes = {finding["code"] for finding in harness.doctor()}
    assert {
        "CONCLUSION_WITHOUT_EVIDENCE",
        "COMPLETED_TASK_WITHOUT_SUMMARY",
        "VERIFIED_WITHOUT_TEST",
        "TEST_WITHOUT_PASS_CRITERIA",
    } <= codes

    with session_scope(harness.root) as session:
        stored_conclusion = session.get(Conclusion, conclusion.id)
        assert stored_conclusion
        stored_conclusion.status = "superseded"
        stored_conclusion.superseded_by = None
    assert "SUPERSEDED_WITHOUT_REPLACEMENT" in {f["code"] for f in harness.doctor()}


def test_update_project_all_fields(harness: Harness) -> None:
    project = harness.update_project(
        description="description",
        repository_uri="https://example.test/repo.git",
        default_branch="trunk",
    )
    assert project.repository_uri == "https://example.test/repo.git"
    assert project.default_branch == "trunk"
