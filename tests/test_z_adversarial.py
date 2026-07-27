from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from sqlalchemy import select

import reharness.services as services_module
from reharness.db import session_scope
from reharness.models import Build, Evidence, Project, Relation, RequirementPlanVersion, Snapshot, TaskEvent
from reharness.services import Harness, HarnessError, StateTransitionError
from reharness.utils import git_snapshot, new_id


def commit_file(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


def current_commit(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


def implemented_requirement(harness: Harness, *, with_task: bool = False):
    req = harness.create_requirement("Adversarial requirement", ["A real test passes"])
    harness.transition_requirement(req.id, "accepted")
    harness.transition_requirement(req.id, "in_progress")
    task = harness.start_task("development", "Implement adversarial requirement", requirement_ids=[req.id]) if with_task else None
    base = current_commit(harness.root)
    head = commit_file(
        harness.root,
        f"adversarial-{req.id}.py",
        f"REQUIREMENT_ID = {req.id!r}\nREADY = True\n",
        f"implement {req.id}",
    )
    change = harness.capture_change(base, head, task.id if task else None, [req.id])
    harness.transition_requirement(req.id, "implemented")
    return req, change, task


def build_for(harness: Harness, change_id: str, name: str = "artifact.bin", status: str = "succeeded") -> Build:
    artifact = harness.root / name
    artifact.write_bytes(name.encode())
    return harness.capture_build(artifact, status=status, change_id=change_id)


def test_concurrent_agents_append_contiguous_task_events(harness: Harness) -> None:
    task = harness.start_task("research", "Concurrent event logging")

    def append(index: int) -> int:
        return harness.add_task_step(
            task.id, "observation_recorded", f"observation {index}"
        ).sequence_number

    results: list[int] = []
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(append, index) for index in range(12)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except BaseException as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)

    assert not errors, errors
    assert sorted(results) == list(range(2, 14))
    with session_scope(harness.root, write=False) as session:
        sequences = list(
            session.scalars(
                select(TaskEvent.sequence_number)
                .where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.sequence_number)
            ).all()
        )
    assert sequences == list(range(1, 14))
    assert harness.doctor() == []


def test_concurrent_requirement_plan_versions_are_unique(harness: Harness) -> None:
    req = harness.create_requirement("Concurrent plan updates")

    def add(index: int) -> int:
        return harness.add_requirement_plan(req.id, f"plan {index}").version

    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(add, range(8)))

    assert sorted(versions) == list(range(1, 9))
    with session_scope(harness.root, write=False) as session:
        stored = list(
            session.scalars(
                select(RequirementPlanVersion.version)
                .where(RequirementPlanVersion.requirement_id == req.id)
                .order_by(RequirementPlanVersion.version)
            ).all()
        )
    assert stored == list(range(1, 9))


def test_domain_semantics_reject_arbitrary_events_evidence_and_relations(harness: Harness) -> None:
    task = harness.start_task("research", "Typed semantics")
    source = harness.root / "typed.txt"
    source.write_text("typed", encoding="utf-8")

    with pytest.raises(HarnessError, match="event type"):
        harness.add_task_step(task.id, "made_up_event", "bad")
    with pytest.raises(HarnessError, match="evidence type"):
        harness.capture_evidence(source, "made_up_evidence", task.id)
    with pytest.raises(HarnessError, match="failure reason"):
        harness.complete_task(task.id, False, "execution failed")

    with session_scope(harness.root) as session:
        project = session.scalar(select(Project))
        assert project is not None
        with pytest.raises(HarnessError, match="Invalid relation shape"):
            harness._add_relation(
                session, "task", task.id, "supports", "project", project.id
            )


def test_duplicate_relations_are_idempotent(harness: Harness) -> None:
    task = harness.start_task("research", "Relation idempotency")
    req = harness.create_requirement("Linked requirement")
    with session_scope(harness.root) as session:
        first = harness._add_relation(
            session, "task", task.id, "implements", "requirement", req.id
        )
        second = harness._add_relation(
            session, "task", task.id, "implements", "requirement", req.id
        )
        assert first.id == second.id
    with session_scope(harness.root, write=False) as session:
        count = len(
            session.scalars(
                select(Relation).where(
                    Relation.source_id == task.id,
                    Relation.relation_type == "implements",
                    Relation.target_id == req.id,
                )
            ).all()
        )
    assert count == 1


def test_tampered_evidence_cannot_formalize_a_conclusion(harness: Harness) -> None:
    source = harness.root / "claim-result.json"
    source.write_text('{"result": true}', encoding="utf-8")
    evidence = harness.capture_evidence(source, "experiment_result")
    stored = harness.root / harness.evidence_data(evidence.id)["storage_uri"]
    stored.write_text("tampered", encoding="utf-8")
    conclusion = harness.create_conclusion("The experiment is valid")

    with pytest.raises(HarnessError, match="integrity"):
        harness.support_conclusion(conclusion.id, [evidence.id])
    assert harness.conclusion_data(conclusion.id)["status"] == "exploring"


def test_change_uses_canonical_commits_rejects_empty_diff_and_rolls_back_artifacts(
    harness: Harness,
) -> None:
    base = current_commit(harness.root)
    with pytest.raises(HarnessError, match="at least one committed"):
        harness.capture_change("HEAD", "HEAD")

    head = commit_file(harness.root, "canonical.py", "VALUE = 1\n", "canonical change")
    change = harness.capture_change(f"{base[:8]}", "HEAD")
    assert change.base_commit == base
    assert change.head_commit == head

    next_head = commit_file(harness.root, "canonical.py", "VALUE = 2\n", "rollback change")
    before = {path for path in (harness.root / "harness-artifacts" / "evidence").iterdir()}
    with pytest.raises(HarnessError):
        harness.capture_change(head, next_head, requirement_ids=["REQ-MISSING"])
    after = {path for path in (harness.root / "harness-artifacts" / "evidence").iterdir()}
    assert after == before


def test_junit_parser_rejects_malformed_dtd_inconsistent_and_missing_reports(harness: Harness) -> None:
    spec = harness.define_test("Hardened JUnit", "unit", [sys.executable, "-c", "pass"])

    malformed = harness.root / "malformed.xml"
    malformed.write_text("<testsuite", encoding="utf-8")
    with pytest.raises(HarnessError, match="Invalid JUnit"):
        harness.import_junit(spec.id, malformed)

    dtd = harness.root / "dtd.xml"
    dtd.write_text('<!DOCTYPE x [<!ENTITY x "x">]><testsuite tests="1"/>', encoding="utf-8")
    with pytest.raises(HarnessError, match="DTD"):
        harness.import_junit(spec.id, dtd)

    inconsistent = harness.root / "inconsistent.xml"
    inconsistent.write_text(
        '<testsuite tests="1" failures="1" errors="1" skipped="0"/>', encoding="utf-8"
    )
    with pytest.raises(HarnessError, match="exceed"):
        harness.import_junit(spec.id, inconsistent)

    with pytest.raises(HarnessError, match="not found"):
        harness.run_test(spec.id, junit_path=Path("missing.xml"))

    empty = harness.root / "empty.xml"
    empty.write_text('<testsuite tests="0" failures="0" errors="0"/>', encoding="utf-8")
    assert harness.import_junit(spec.id, empty).status == "failed"


def test_pass_criteria_reject_unknown_boolean_and_negative_values(harness: Harness) -> None:
    with pytest.raises(HarnessError, match="Unsupported pass criteria"):
        harness.define_test("Unknown", "unit", ["true"], pass_criteria={"magic": 1})
    with pytest.raises(HarnessError, match="integer"):
        harness.define_test("Boolean", "unit", ["true"], pass_criteria={"min_total": True})
    with pytest.raises(HarnessError, match="negative"):
        harness.define_test("Negative", "unit", ["true"], pass_criteria={"min_total": -1})
    with pytest.raises(HarnessError, match="name"):
        harness.define_test("   ", "unit", ["true"])


def test_requirement_verification_rejects_missing_failed_unrelated_and_tampered_lineage(
    harness: Harness,
) -> None:
    req, change, _ = implemented_requirement(harness)
    spec = harness.define_test(
        "Covered", "smoke", [sys.executable, "-c", "pass"], [req.id]
    )

    no_build_run = harness.run_test(spec.id)
    with pytest.raises(HarnessError, match="bound to a Build"):
        harness.verify_requirement(req.id, no_build_run.id)

    failed_build = build_for(harness, change.id, "failed-build.bin", status="failed")
    failed_build_run = harness.run_test(spec.id, build_id=failed_build.id)
    with pytest.raises(HarnessError, match="succeeded Build"):
        harness.verify_requirement(req.id, failed_build_run.id)

    other_req, other_change, _ = implemented_requirement(harness)
    unrelated_build = build_for(harness, other_change.id, "unrelated.bin")
    unrelated_run = harness.run_test(spec.id, build_id=unrelated_build.id)
    with pytest.raises(HarnessError, match="does not implement"):
        harness.verify_requirement(req.id, unrelated_run.id)
    assert harness.requirement_data(other_req.id)["status"] == "implemented"

    valid_build = build_for(harness, change.id, "valid.bin")
    tampered_report_run = harness.run_test(spec.id, build_id=valid_build.id)
    report_evidence = harness.evidence_data(tampered_report_run.evidence_id)
    (harness.root / report_evidence["storage_uri"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(HarnessError, match="integrity"):
        harness.verify_requirement(req.id, tampered_report_run.id)

    clean_run = harness.run_test(spec.id, build_id=valid_build.id)
    with session_scope(harness.root, write=False) as session:
        build_relation = session.scalar(
            select(Relation).where(
                Relation.source_type == "build",
                Relation.source_id == valid_build.id,
                Relation.relation_type == "produces",
                Relation.target_type == "evidence",
            )
        )
        assert build_relation is not None
        build_evidence = session.get(Evidence, build_relation.target_id)
        assert build_evidence is not None
        build_path = harness.root / build_evidence.storage_uri
    build_path.write_bytes(b"tampered-build")
    with pytest.raises(HarnessError, match="integrity"):
        harness.verify_requirement(req.id, clean_run.id)


def test_requirement_verification_uses_complete_traceable_chain(harness: Harness) -> None:
    req, change, _ = implemented_requirement(harness)
    build = build_for(harness, change.id, "traceable.bin")
    spec = harness.define_test(
        "Traceable smoke", "smoke", [sys.executable, "-c", "pass"], [req.id]
    )
    run = harness.run_test(spec.id, build_id=build.id)
    assert harness.verify_requirement(req.id, run.id).status == "verified"

    with session_scope(harness.root, write=False) as session:
        evaluates = session.scalar(
            select(Relation).where(
                Relation.source_type == "test_run",
                Relation.source_id == run.id,
                Relation.relation_type == "evaluates",
                Relation.target_id == build.id,
            )
        )
        wrong_verifies = session.scalar(
            select(Relation).where(
                Relation.source_type == "test_run",
                Relation.source_id == run.id,
                Relation.relation_type == "verifies",
                Relation.target_type == "build",
            )
        )
    assert evaluates is not None
    assert wrong_verifies is None
    requirement_doc = (harness.root / "harness-docs" / "requirements" / f"{req.id}.md").read_text(
        encoding="utf-8"
    )
    assert f"change:{change.id}" in requirement_doc
    assert f"test_run:{run.id}" in requirement_doc


def test_completed_task_rejects_new_evidence_change_and_test_work(harness: Harness) -> None:
    task = harness.start_task("development", "Terminal task")
    harness.complete_task(task.id, True, "Task is complete")
    source = harness.root / "late.txt"
    source.write_text("late", encoding="utf-8")

    with pytest.raises(StateTransitionError):
        harness.capture_evidence(source, "experiment_result", task.id)
    with pytest.raises(StateTransitionError):
        harness.run_test(
            harness.define_test("Late test", "unit", [sys.executable, "-c", "pass"]).id,
            task_id=task.id,
        )

    base = current_commit(harness.root)
    head = commit_file(harness.root, "late.py", "LATE = True\n", "late change")
    with pytest.raises(StateTransitionError):
        harness.capture_change(base, head, task.id)


def test_render_failure_commits_database_and_marks_views_stale(harness: Harness, monkeypatch) -> None:
    task = harness.start_task("research", "Render failure")

    def fail_render(*args, **kwargs):
        raise OSError("simulated render failure")

    monkeypatch.setattr(services_module, "render_task", fail_render)
    event = harness.add_task_step(task.id, "observation_recorded", "committed observation")

    with session_scope(harness.root, write=False) as session:
        assert session.get(TaskEvent, event.id) is not None
    assert (harness.root / ".harness" / "render-stale.json").exists()
    assert any(finding["code"] == "RENDER_STALE" for finding in harness.doctor())

    monkeypatch.undo()
    harness.refresh()
    assert not (harness.root / ".harness" / "render-stale.json").exists()


def test_ids_have_sufficient_entropy_and_untracked_files_are_hashed(harness: Harness) -> None:
    ids = {new_id("event") for _ in range(10_000)}
    assert len(ids) == 10_000
    assert min(len(value.split("-", 1)[1]) for value in ids) >= 20

    untracked = harness.root / "untracked-data.bin"
    untracked.write_bytes(b"untracked")
    snapshot = git_snapshot(harness.root)
    item = next(entry for entry in snapshot["untracked"] if entry["path"] == untracked.name)
    assert item["sha256"]
    assert item["size"] == len(b"untracked")

    task = harness.start_task("research", "Snapshot classification")
    result = harness.run_command(task.id, [sys.executable, "-c", "pass"])
    with session_scope(harness.root, write=False) as session:
        stored = session.get(Snapshot, result["snapshot_id"])
        assert stored is not None
        assert stored.reproducibility == "partial"
