from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from reharness.cli import app

runner = CliRunner()


def invoke_in(path: Path, args: list[str]):
    previous = Path.cwd()
    os.chdir(path)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(previous)


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


def test_research_task_succeeds_while_hypothesis_is_refuted(project_dir: Path) -> None:
    """A negative result is valuable evidence, not a failed research task."""
    assert invoke_in(project_dir, ["init", "--name", "Refutation workflow"]).exit_code == 0

    conclusion_result = invoke_in(
        project_dir,
        [
            "conclusion",
            "create",
            "--claim",
            "The cache reduces P95 latency by at least 20 percent",
            "--falsification",
            "A fixed-snapshot repetition improves P95 by less than 20 percent",
            "--json",
        ],
    )
    assert conclusion_result.exit_code == 0, conclusion_result.output
    conclusion_id = json.loads(conclusion_result.output)["id"]

    task_result = invoke_in(
        project_dir,
        [
            "task",
            "start",
            "--type",
            "research",
            "--goal",
            "Measure whether the cache reduces P95 latency by at least 20 percent",
            "--criterion",
            "Produce a fixed experiment result",
            "--constraint",
            "Do not change the input dataset",
            "--json",
        ],
    )
    assert task_result.exit_code == 0, task_result.output
    task_id = json.loads(task_result.output)["id"]

    script = (
        "from pathlib import Path; "
        "Path('experiment-result.json').write_text("
        "'{\"baseline_p95_ms\": 100, \"candidate_p95_ms\": 95, \"improvement_percent\": 5}', "
        "encoding='utf-8')"
    )
    run_result = invoke_in(
        project_dir,
        [
            "run",
            task_id,
            "--capture",
            "experiment-result.json",
            "--json",
            "--",
            sys.executable,
            "-c",
            script,
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    run_data = json.loads(run_result.output)
    result_evidence_id = run_data["captured_evidence_ids"][0]

    refute_result = invoke_in(
        project_dir,
        [
            "conclusion",
            "refute",
            conclusion_id,
            "--evidence",
            result_evidence_id,
            "--reason",
            "The fixed experiment observed only a five percent improvement",
            "--json",
        ],
    )
    assert refute_result.exit_code == 0, refute_result.output
    assert json.loads(refute_result.output)["status"] == "refuted"

    complete_result = invoke_in(
        project_dir,
        [
            "task",
            "succeed",
            task_id,
            "--summary",
            "Experiment completed successfully and falsified the hypothesis",
            "--result-type",
            "negative",
            "--json",
        ],
    )
    assert complete_result.exit_code == 0, complete_result.output

    task_data = json.loads(invoke_in(project_dir, ["task", "show", task_id, "--json"]).output)
    conclusion_data = json.loads(
        invoke_in(project_dir, ["conclusion", "show", conclusion_id, "--json"]).output
    )
    brief = invoke_in(project_dir, ["brief"]).output

    assert task_data["status"] == "succeeded"
    assert task_data["result_type"] == "negative"
    assert any(event["type"] == "command_completed" for event in task_data["events"])
    assert any(event["type"] == "task_succeeded" for event in task_data["events"])
    assert conclusion_data["status"] == "refuted"
    assert any(
        relation["type"] == "refutes" and relation["target_id"] == result_evidence_id
        for relation in conclusion_data["relations"]
    )
    assert "`refuted`" in brief
    assert invoke_in(project_dir, ["doctor", "--json"]).exit_code == 0


def test_failed_smoke_test_is_fixed_rebuilt_and_then_verifies_requirement(project_dir: Path) -> None:
    """A failed build/test must not verify a requirement; a later fixed build may do so."""
    assert invoke_in(project_dir, ["init", "--name", "Recovery workflow"]).exit_code == 0

    requirement_result = invoke_in(
        project_dir,
        [
            "requirement",
            "create",
            "--description",
            "The feature reports itself ready",
            "--criterion",
            "The smoke command exits with code zero",
            "--json",
        ],
    )
    assert requirement_result.exit_code == 0, requirement_result.output
    requirement_id = json.loads(requirement_result.output)["id"]
    assert invoke_in(project_dir, ["requirement", "accept", requirement_id]).exit_code == 0
    assert invoke_in(project_dir, ["requirement", "start", requirement_id]).exit_code == 0

    development_task_result = invoke_in(
        project_dir,
        [
            "task",
            "start",
            "--type",
            "development",
            "--goal",
            "Implement the readiness feature",
            "--requirement",
            requirement_id,
            "--json",
        ],
    )
    development_task_id = json.loads(development_task_result.output)["id"]

    initial_base = current_commit(project_dir)
    buggy_commit = commit_file(project_dir, "feature.py", "READY = False\n", "implement buggy feature")
    change_result = invoke_in(
        project_dir,
        [
            "change",
            "capture",
            "--base",
            initial_base,
            "--head",
            buggy_commit,
            "--task",
            development_task_id,
            "--requirement",
            requirement_id,
            "--json",
        ],
    )
    assert change_result.exit_code == 0, change_result.output
    buggy_change_id = json.loads(change_result.output)["id"]
    assert invoke_in(project_dir, ["requirement", "implemented", requirement_id]).exit_code == 0

    buggy_artifact = project_dir / "feature-v1.bin"
    buggy_artifact.write_bytes(b"buggy-build")
    build_result = invoke_in(
        project_dir,
        [
            "build",
            "capture",
            "--artifact",
            str(buggy_artifact),
            "--change",
            buggy_change_id,
            "--json",
        ],
    )
    assert build_result.exit_code == 0, build_result.output
    buggy_build_id = json.loads(build_result.output)["id"]

    criteria = project_dir / "smoke-criteria.json"
    criteria.write_text('{"exit_code": 0, "min_total": 1}', encoding="utf-8")
    test_spec_result = invoke_in(
        project_dir,
        [
            "test",
            "define",
            "--name",
            "Readiness smoke test",
            "--type",
            "smoke",
            "--command",
            sys.executable,
            "--command",
            "-c",
            "--command",
            "import feature; raise SystemExit(0 if feature.READY else 1)",
            "--requirement",
            requirement_id,
            "--pass-criteria-file",
            str(criteria),
            "--json",
        ],
    )
    assert test_spec_result.exit_code == 0, test_spec_result.output
    test_spec_id = json.loads(test_spec_result.output)["id"]

    failed_run_result = invoke_in(
        project_dir,
        [
            "test",
            "run",
            test_spec_id,
            "--task",
            development_task_id,
            "--build",
            buggy_build_id,
            "--json",
        ],
    )
    assert failed_run_result.exit_code == 1, failed_run_result.output
    failed_run_data = json.loads(failed_run_result.output)
    assert failed_run_data["status"] == "failed"

    failed_verification = invoke_in(
        project_dir,
        [
            "requirement",
            "verify",
            requirement_id,
            "--test-run",
            failed_run_data["id"],
            "--json",
        ],
    )
    assert failed_verification.exit_code == 2
    requirement_after_failure = json.loads(
        invoke_in(project_dir, ["requirement", "show", requirement_id, "--json"]).output
    )
    assert requirement_after_failure["status"] == "implemented"

    assert invoke_in(
        project_dir,
        [
            "task",
            "succeed",
            development_task_id,
            "--summary",
            "Initial implementation completed, but smoke testing found a defect",
            "--result-type",
            "negative",
        ],
    ).exit_code == 0

    fix_task_result = invoke_in(
        project_dir,
        [
            "task",
            "start",
            "--type",
            "debugging",
            "--goal",
            "Fix the readiness defect found by the smoke test",
            "--requirement",
            requirement_id,
            "--json",
        ],
    )
    assert fix_task_result.exit_code == 0, fix_task_result.output
    fix_task_id = json.loads(fix_task_result.output)["id"]

    fixed_commit = commit_file(project_dir, "feature.py", "READY = True\n", "fix readiness feature")
    fixed_change_result = invoke_in(
        project_dir,
        [
            "change",
            "capture",
            "--base",
            buggy_commit,
            "--head",
            fixed_commit,
            "--task",
            fix_task_id,
            "--requirement",
            requirement_id,
            "--json",
        ],
    )
    assert fixed_change_result.exit_code == 0, fixed_change_result.output
    fixed_change_id = json.loads(fixed_change_result.output)["id"]

    fixed_artifact = project_dir / "feature-v2.bin"
    fixed_artifact.write_bytes(b"fixed-build")
    fixed_build_result = invoke_in(
        project_dir,
        [
            "build",
            "capture",
            "--artifact",
            str(fixed_artifact),
            "--change",
            fixed_change_id,
            "--json",
        ],
    )
    assert fixed_build_result.exit_code == 0, fixed_build_result.output
    fixed_build_id = json.loads(fixed_build_result.output)["id"]

    passed_run_result = invoke_in(
        project_dir,
        [
            "test",
            "run",
            test_spec_id,
            "--task",
            fix_task_id,
            "--build",
            fixed_build_id,
            "--json",
        ],
    )
    assert passed_run_result.exit_code == 0, passed_run_result.output
    passed_run_data = json.loads(passed_run_result.output)
    assert passed_run_data["status"] == "passed"

    verification_result = invoke_in(
        project_dir,
        [
            "requirement",
            "verify",
            requirement_id,
            "--test-run",
            passed_run_data["id"],
            "--json",
        ],
    )
    assert verification_result.exit_code == 0, verification_result.output
    assert json.loads(verification_result.output)["status"] == "verified"

    assert invoke_in(
        project_dir,
        [
            "task",
            "succeed",
            fix_task_id,
            "--summary",
            "The defect was fixed and the smoke test passed",
            "--result-type",
            "positive",
        ],
    ).exit_code == 0

    final_requirement = json.loads(
        invoke_in(project_dir, ["requirement", "show", requirement_id, "--json"]).output
    )
    fix_task = json.loads(invoke_in(project_dir, ["task", "show", fix_task_id, "--json"]).output)
    brief = invoke_in(project_dir, ["brief"]).output

    assert final_requirement["status"] == "verified"
    assert any(
        event["type"] == "test_completed" and event["payload"]["status"] == "passed"
        for event in fix_task["events"]
    )
    assert "`verified`" in brief
    assert invoke_in(project_dir, ["doctor", "--json"]).exit_code == 0
