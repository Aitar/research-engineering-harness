from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from reharness.db import session_scope
from reharness.models import Evidence, Relation, Snapshot, Task, TaskEvent
from reharness.services import Harness, HarnessError, StateTransitionError


def test_task_start_records_fixed_goal_and_event(harness: Harness) -> None:
    task = harness.start_task(
        "research",
        "Fixed goal",
        ["result exists"],
        ["no production data"],
    )
    with session_scope(harness.root) as session:
        stored = session.get(Task, task.id)
        assert stored is not None
        assert stored.original_goal == "Fixed goal"
        events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()
        assert [e.sequence_number for e in events] == [1]
        assert events[0].event_type == "task_created"


def test_task_events_are_sequential_and_append_only(harness: Harness) -> None:
    task = harness.start_task("development", "Implement parser")
    first = harness.add_task_step(task.id, "file_inspected", "Read parser")
    second = harness.revise_task_plan(task.id, "Use streaming parser", "Memory usage")
    assert first.sequence_number == 2
    assert second.sequence_number == 3
    with session_scope(harness.root) as session:
        events = session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.sequence_number)
        ).all()
        assert [e.event_type for e in events] == ["task_created", "file_inspected", "plan_revised"]


def test_completed_task_rejects_steps_and_second_completion(harness: Harness) -> None:
    task = harness.start_task("research", "Complete me")
    harness.complete_task(task.id, True, "done", "negative")
    with pytest.raises(StateTransitionError):
        harness.add_task_step(task.id, "observation_recorded", "late")
    with pytest.raises(StateTransitionError):
        harness.complete_task(task.id, True, "again")


def test_task_success_can_have_negative_result(harness: Harness) -> None:
    task = harness.start_task("research", "Try falsification")
    result = harness.complete_task(task.id, True, "Hypothesis disproved", "negative")
    assert result.status == "succeeded"
    assert result.result_type == "negative"


def test_invalid_task_type_and_result_type(harness: Harness) -> None:
    with pytest.raises(HarnessError):
        harness.start_task("unknown", "goal")
    task = harness.start_task("research", "goal")
    with pytest.raises(HarnessError):
        harness.complete_task(task.id, True, "done", "maybe")


def test_run_command_success_captures_snapshot_log_and_output(harness: Harness) -> None:
    task = harness.start_task("testing", "Run command")
    output = harness.root / "result.json"
    result = harness.run_command(
        task.id,
        [sys.executable, "-c", "from pathlib import Path; Path('result.json').write_text('ok'); print('hello')"],
        [output],
        random_seed="42",
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert len(result["captured_evidence_ids"]) == 1
    with session_scope(harness.root) as session:
        evidence = session.get(Evidence, result["evidence_id"])
        snapshot = session.get(Snapshot, result["snapshot_id"])
        assert evidence is not None and evidence.evidence_type == "command_log"
        assert snapshot is not None
        assert snapshot.git_commit
        assert snapshot.random_seed == "42"


def test_run_command_failure_is_recorded_without_completing_task(harness: Harness) -> None:
    task = harness.start_task("testing", "Fail command")
    result = harness.run_command(task.id, [sys.executable, "-c", "import sys; print('bad'); sys.exit(3)"])
    assert result["exit_code"] == 3
    with session_scope(harness.root) as session:
        stored = session.get(Task, task.id)
        events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()
        assert stored is not None and stored.status == "in_progress"
        assert events[-1].event_type == "command_failed"


def test_run_command_timeout(harness: Harness) -> None:
    task = harness.start_task("testing", "Timeout")
    result = harness.run_command(task.id, [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
    assert result["exit_code"] == 124
    assert "timed out" in result["stderr"].lower()


def test_run_command_missing_executable(harness: Harness) -> None:
    task = harness.start_task("testing", "Missing executable")
    result = harness.run_command(task.id, ["definitely-not-a-real-executable"])
    assert result["exit_code"] == 127


def test_run_requires_active_task(harness: Harness) -> None:
    task = harness.start_task("testing", "Closed")
    harness.complete_task(task.id, True, "done")
    with pytest.raises(StateTransitionError):
        harness.run_command(task.id, [sys.executable, "-c", "print(1)"])


def test_dirty_worktree_patch_is_captured_as_evidence(harness: Harness) -> None:
    (harness.root / "README.md").write_text("# changed\n", encoding="utf-8")
    task = harness.start_task("research", "Capture dirty patch")
    result = harness.run_command(task.id, [sys.executable, "-c", "print('done')"])
    with session_scope(harness.root) as session:
        snapshot = session.get(Snapshot, result["snapshot_id"])
        assert snapshot is not None and snapshot.git_dirty is True
        relation = session.scalar(
            select(Relation).where(
                Relation.source_type == "snapshot",
                Relation.source_id == snapshot.id,
                Relation.relation_type == "produces",
            )
        )
        assert relation is not None
        patch_evidence = session.get(Evidence, relation.target_id)
        assert patch_evidence is not None and patch_evidence.evidence_type == "source_patch"
