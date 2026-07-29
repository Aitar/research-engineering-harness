from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from reharness.db import session_scope
from reharness.models import Relation, RequirementPlanVersion
from reharness.services import Harness, HarnessError, StateTransitionError


def commit_file(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


def create_implemented_requirement(harness: Harness) -> tuple[str, str]:
    req = harness.create_requirement("Support legacy tokens", ["valid token migrates"])
    harness.transition_requirement(req.id, "accepted")
    harness.transition_requirement(req.id, "in_progress")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=harness.root, text=True, capture_output=True, check=True
    ).stdout.strip()
    head = commit_file(harness.root, "feature.py", "enabled = True\n", "implement feature")
    change = harness.capture_change(base, head, requirement_ids=[req.id])
    implemented = harness.transition_requirement(req.id, "implemented")
    assert implemented.status == "implemented"
    return req.id, change.id


def test_requirement_lifecycle_and_plan_versions(harness: Harness) -> None:
    req = harness.create_requirement("Add auth", ["login succeeds"], ["no plaintext secrets"])
    assert req.status == "draft"
    harness.transition_requirement(req.id, "accepted")
    first = harness.add_requirement_plan(req.id, "Plan v1")
    second = harness.add_requirement_plan(req.id, "Plan v2", "Need transactions")
    assert (first.version, second.version) == (1, 2)
    with session_scope(harness.root) as session:
        versions = session.scalars(
            select(RequirementPlanVersion)
            .where(RequirementPlanVersion.requirement_id == req.id)
            .order_by(RequirementPlanVersion.version)
        ).all()
        assert [p.plan_markdown for p in versions] == ["Plan v1", "Plan v2"]


def test_requirement_cannot_implement_without_change(harness: Harness) -> None:
    req = harness.create_requirement("Add auth")
    harness.transition_requirement(req.id, "accepted")
    harness.transition_requirement(req.id, "in_progress")
    with pytest.raises(HarnessError):
        harness.transition_requirement(req.id, "implemented")


def test_capture_change_and_build(harness: Harness) -> None:
    req = harness.create_requirement("Feature")
    harness.transition_requirement(req.id, "accepted")
    harness.transition_requirement(req.id, "in_progress")
    task = harness.start_task("development", "Implement feature", requirement_ids=[req.id])
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=harness.root, text=True, capture_output=True, check=True
    ).stdout.strip()
    head = commit_file(harness.root, "code.py", "print('feature')\n", "feature")
    change = harness.capture_change(base, head, task.id, [req.id])
    artifact = harness.root / "dist.bin"
    artifact.write_bytes(b"binary")
    build = harness.capture_build(artifact, change_id=change.id)
    assert change.patch_hash
    assert build.artifact_hash
    assert build.commit_sha == head


def test_test_spec_rejects_unknown_type_and_requirement(harness: Harness) -> None:
    with pytest.raises(HarnessError):
        harness.define_test("Bad", "unknown", ["true"])
    with pytest.raises(HarnessError):
        harness.define_test("Bad req", "unit", ["true"], ["REQ-NOTFOUND"])


def test_test_run_pass_fail_and_error(harness: Harness) -> None:
    pass_spec = harness.define_test("Pass", "unit", [sys.executable, "-c", "print('ok')"])
    fail_spec = harness.define_test("Fail", "unit", [sys.executable, "-c", "import sys;sys.exit(1)"])
    error_spec = harness.define_test("Error", "unit", ["missing-executable-xyz"])
    assert harness.run_test(pass_spec.id).status == "passed"
    assert harness.run_test(fail_spec.id).status == "failed"
    assert harness.run_test(error_spec.id).status == "error"


def test_junit_counts_are_imported(harness: Harness) -> None:
    spec = harness.define_test("JUnit", "regression", [sys.executable, "-c", "pass"])
    report = harness.root / "junit.xml"
    report.write_text(
        '<testsuite tests="4" failures="1" errors="0" skipped="1"></testsuite>',
        encoding="utf-8",
    )
    run = harness.run_test(spec.id, junit_path=report)
    assert run.status == "failed"
    assert run.total_count == 4
    assert run.passed_count == 2
    assert run.failed_count == 1
    assert run.skipped_count == 1


def test_requirement_verification_requires_covered_passing_test(harness: Harness) -> None:
    req_id, change_id = create_implemented_requirement(harness)
    artifact = harness.root / "verification-build.bin"
    artifact.write_bytes(b"verified-build")
    build = harness.capture_build(artifact, change_id=change_id)
    uncovered = harness.define_test("Uncovered", "unit", [sys.executable, "-c", "pass"])
    uncovered_run = harness.run_test(uncovered.id, build_id=build.id)
    with pytest.raises(HarnessError):
        harness.verify_requirement(req_id, uncovered_run.id)

    covered_fail = harness.define_test(
        "Covered fail", "unit", [sys.executable, "-c", "import sys;sys.exit(1)"], [req_id]
    )
    fail_run = harness.run_test(covered_fail.id, build_id=build.id)
    with pytest.raises(HarnessError):
        harness.verify_requirement(req_id, fail_run.id)

    covered_pass = harness.define_test(
        "Covered pass", "smoke", [sys.executable, "-c", "pass"], [req_id]
    )
    pass_run = harness.run_test(covered_pass.id, build_id=build.id)
    verified = harness.verify_requirement(req_id, pass_run.id)
    assert verified.status == "verified"
    with session_scope(harness.root) as session:
        relation = session.scalar(
            select(Relation).where(
                Relation.source_id == pass_run.id,
                Relation.relation_type == "verifies",
                Relation.target_id == req_id,
            )
        )
        assert relation is not None


def test_requirement_must_be_implemented_before_verify(harness: Harness) -> None:
    req = harness.create_requirement("Feature")
    spec = harness.define_test("Pass", "unit", [sys.executable, "-c", "pass"], [req.id])
    run = harness.run_test(spec.id)
    with pytest.raises(StateTransitionError):
        harness.verify_requirement(req.id, run.id)
