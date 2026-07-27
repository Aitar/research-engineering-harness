from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from typer.testing import CliRunner

from reharness.cli import app

runner = CliRunner()


def invoke_in(path: Path, args: list[str]):
    old = Path.cwd()
    os.chdir(path)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(old)


def test_cli_init_task_run_and_summary(project_dir: Path) -> None:
    result = invoke_in(project_dir, ["init", "--name", "CLI", "--json"])
    assert result.exit_code == 0, result.output
    project = json.loads(result.output)
    assert project["name"] == "CLI"

    result = invoke_in(
        project_dir,
        ["task", "start", "--type", "research", "--goal", "CLI goal", "--json"],
    )
    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["id"]

    result = invoke_in(
        project_dir,
        ["run", task_id, "--json", "--", sys.executable, "-c", "print('cli')"],
    )
    assert result.exit_code == 0, result.output
    run_data = json.loads(result.output)
    assert run_data["stdout"] == "cli\n"

    result = invoke_in(
        project_dir,
        ["task", "succeed", task_id, "--summary", "done", "--result-type", "positive", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "succeeded"

    result = invoke_in(project_dir, ["summary", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["tasks"] == 1


def test_cli_evidence_verify_failure_returns_nonzero(project_dir: Path) -> None:
    result = invoke_in(project_dir, ["init", "--name", "CLI"])
    assert result.exit_code == 0
    source = project_dir / "evidence.txt"
    source.write_text("original", encoding="utf-8")
    result = invoke_in(
        project_dir,
        ["evidence", "capture", "--file", str(source), "--type", "experiment_result", "--json"],
    )
    data = json.loads(result.output)
    stored = project_dir / data["storage_uri"]
    stored.write_text("changed", encoding="utf-8")
    result = invoke_in(project_dir, ["evidence", "verify", data["id"], "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["valid"] is False


def test_cli_doctor_clean(project_dir: Path) -> None:
    assert invoke_in(project_dir, ["init", "--name", "CLI"]).exit_code == 0
    result = invoke_in(project_dir, ["doctor"])
    assert result.exit_code == 0
    assert "No consistency problems" in result.output


def test_cli_full_research_and_engineering_workflow(project_dir: Path) -> None:
    import subprocess

    assert invoke_in(project_dir, ["init", "--name", "Full CLI", "--description", "workflow"]).exit_code == 0
    assert invoke_in(project_dir, ["project", "show", "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["brief"]).exit_code == 0
    assert invoke_in(project_dir, ["context", "--topic", "workflow", "--budget", "1000"]).exit_code == 0
    assert invoke_in(project_dir, ["render"]).exit_code == 0

    task_result = invoke_in(
        project_dir,
        [
            "task", "start", "--type", "research", "--goal", "Run research", "--criterion", "capture",
            "--constraint", "offline", "--json",
        ],
    )
    task_id = json.loads(task_result.output)["id"]
    payload = project_dir / "payload.json"
    payload.write_text('{"value": 1}', encoding="utf-8")
    assert invoke_in(
        project_dir,
        ["task", "step", task_id, "--type", "observation_recorded", "--summary", "observed", "--payload-file", str(payload), "--json"],
    ).exit_code == 0
    plan = project_dir / "plan.md"
    plan.write_text("Revised plan", encoding="utf-8")
    assert invoke_in(project_dir, ["task", "revise-plan", task_id, "--plan-file", str(plan), "--reason", "new evidence"]).exit_code == 0

    source = project_dir / "formal.txt"
    source.write_text("formal evidence", encoding="utf-8")
    captured = invoke_in(
        project_dir,
        ["evidence", "capture", "--file", str(source), "--type", "experiment_result", "--task", task_id, "--json"],
    )
    evidence_id = json.loads(captured.output)["id"]
    assert invoke_in(project_dir, ["evidence", "show", evidence_id, "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["evidence", "verify-all", "--json"]).exit_code == 0

    conclusion_create = invoke_in(
        project_dir,
        ["conclusion", "create", "--claim", "The workflow works", "--falsification", "A repeat fails", "--json"],
    )
    conclusion_id = json.loads(conclusion_create.output)["id"]
    assert invoke_in(
        project_dir,
        ["conclusion", "support", conclusion_id, "--evidence", evidence_id, "--reason", "captured", "--json"],
    ).exit_code == 0
    assert invoke_in(project_dir, ["conclusion", "show", conclusion_id, "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["conclusion", "list", "--status", "supported", "--json"]).exit_code == 0

    replacement_result = invoke_in(
        project_dir, ["conclusion", "create", "--claim", "The narrower workflow works", "--json"]
    )
    replacement_id = json.loads(replacement_result.output)["id"]
    assert invoke_in(
        project_dir,
        ["conclusion", "supersede", conclusion_id, "--by", replacement_id, "--reason", "more precise", "--json"],
    ).exit_code == 0
    assert invoke_in(project_dir, ["task", "succeed", task_id, "--summary", "research complete", "--result-type", "positive"]).exit_code == 0

    requirement_result = invoke_in(
        project_dir,
        ["requirement", "create", "--description", "Ship workflow", "--criterion", "smoke passes", "--json"],
    )
    req_id = json.loads(requirement_result.output)["id"]
    assert invoke_in(project_dir, ["requirement", "accept", req_id]).exit_code == 0
    req_plan = project_dir / "req-plan.md"
    req_plan.write_text("Implement and test", encoding="utf-8")
    assert invoke_in(project_dir, ["requirement", "plan", req_id, "--file", str(req_plan), "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["requirement", "start", req_id]).exit_code == 0

    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_dir, text=True, capture_output=True, check=True).stdout.strip()
    (project_dir / "workflow.py").write_text("READY = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "workflow.py"], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-qm", "workflow"], cwd=project_dir, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_dir, text=True, capture_output=True, check=True).stdout.strip()
    change_result = invoke_in(
        project_dir,
        ["change", "capture", "--base", base, "--head", head, "--requirement", req_id, "--json"],
    )
    assert change_result.exit_code == 0, change_result.output
    change_id = json.loads(change_result.output)["id"]
    assert invoke_in(project_dir, ["requirement", "implemented", req_id]).exit_code == 0

    artifact = project_dir / "app.bin"
    artifact.write_bytes(b"app")
    build_result = invoke_in(
        project_dir,
        ["build", "capture", "--artifact", str(artifact), "--change", change_id, "--json"],
    )
    assert build_result.exit_code == 0, build_result.output
    build_id = json.loads(build_result.output)["id"]

    criteria = project_dir / "pass.json"
    criteria.write_text('{"exit_code": 0, "min_total": 1}', encoding="utf-8")
    test_result = invoke_in(
        project_dir,
        [
            "test", "define", "--name", "Smoke", "--type", "smoke",
            "--command", sys.executable, "--command", "-c", "--command", "print('pass')",
            "--requirement", req_id, "--pass-criteria-file", str(criteria), "--json",
        ],
    )
    assert test_result.exit_code == 0, test_result.output
    spec_id = json.loads(test_result.output)["id"]
    run_result = invoke_in(
        project_dir,
        ["test", "run", spec_id, "--build", build_id, "--json"],
    )
    assert run_result.exit_code == 0, run_result.output
    test_run_id = json.loads(run_result.output)["id"]
    assert invoke_in(
        project_dir,
        ["requirement", "verify", req_id, "--test-run", test_run_id, "--json"],
    ).exit_code == 0
    assert invoke_in(project_dir, ["requirement", "show", req_id, "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["requirement", "list", "--status", "verified", "--json"]).exit_code == 0
    assert invoke_in(project_dir, ["doctor", "--json"]).exit_code == 0
