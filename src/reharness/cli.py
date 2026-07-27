from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

from .services import Harness, HarnessError, as_list, read_structured_file
from .utils import json_dumps, json_loads

app = typer.Typer(
    name="harness",
    help="AI-native research and engineering ledger.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
task_app = typer.Typer(help="Manage append-only tasks.", no_args_is_help=True)
conclusion_app = typer.Typer(help="Manage falsifiable conclusions.", no_args_is_help=True)
requirement_app = typer.Typer(help="Manage versioned requirements.", no_args_is_help=True)
evidence_app = typer.Typer(help="Capture and verify evidence.", no_args_is_help=True)
test_app = typer.Typer(help="Define and run tests.", no_args_is_help=True)
change_app = typer.Typer(help="Capture code changes.", no_args_is_help=True)
build_app = typer.Typer(help="Capture builds.", no_args_is_help=True)
project_app = typer.Typer(help="Inspect project metadata.", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(conclusion_app, name="conclusion")
app.add_typer(requirement_app, name="requirement")
app.add_typer(evidence_app, name="evidence")
app.add_typer(test_app, name="test")
app.add_typer(change_app, name="change")
app.add_typer(build_app, name="build")


def _harness() -> Harness:
    return Harness.open()


def _emit(data: Any, json_output: bool = False) -> None:
    if json_output:
        typer.echo(json_dumps(data))
    elif isinstance(data, str):
        typer.echo(data, nl=not data.endswith("\n"))
    elif isinstance(data, dict):
        for key, value in data.items():
            typer.echo(f"{key}: {value}")
    else:
        typer.echo(data)


def _read_text(value: Optional[str], file: Optional[Path], field_name: str) -> str:
    if file:
        return file.read_text(encoding="utf-8")
    if value is not None:
        return value
    raise HarnessError(f"Provide --{field_name} or --{field_name}-file.")


def _read_list(value: list[str] | None, file: Path | None) -> list[str]:
    result = list(value or [])
    if file:
        result.extend(as_list(read_structured_file(file)))
    return result


def _read_mapping(file: Path | None) -> dict[str, Any]:
    if file is None:
        return {}
    value = read_structured_file(file)
    if not isinstance(value, dict):
        raise HarnessError(f"Expected mapping in {file}")
    return value


def _handle_error(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(2)


@app.command()
def init(
    name: str = typer.Option(..., help="Project name."),
    description: str = typer.Option("", help="Project description."),
    path: Path = typer.Option(Path("."), help="Project directory."),
    repository_uri: str | None = typer.Option(None, help="Repository URI override."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Initialize a harness project."""
    try:
        harness = Harness.initialize(path, name, description, repository_uri)
        _emit({"root": str(harness.root), **harness.project_data()}, json_output)
    except HarnessError as exc:
        _handle_error(exc)


@project_app.command("show")
def project_show(json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().project_data(), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@app.command()
def brief(
    level: str = typer.Option("normal", help="compact, normal, or full"),
) -> None:
    """Print the generated project brief."""
    try:
        typer.echo(_harness().brief(level), nl=False)
    except HarnessError as exc:
        _handle_error(exc)


@app.command()
def context(
    topic: str = typer.Option("", help="Topic used to select related records."),
    budget: int = typer.Option(12000, help="Maximum output characters."),
) -> None:
    """Generate a bounded context package for an LLM."""
    try:
        typer.echo(_harness().context(topic, budget), nl=False)
    except HarnessError as exc:
        _handle_error(exc)


@app.command()
def render() -> None:
    """Regenerate all Markdown views."""
    try:
        path = _harness().refresh()
        typer.echo(str(path))
    except HarnessError as exc:
        _handle_error(exc)


@app.command()
def doctor(json_output: bool = typer.Option(False, "--json")) -> None:
    """Check state, evidence integrity, and provenance consistency."""
    try:
        findings = _harness().doctor()
        if json_output:
            _emit(findings, True)
        elif not findings:
            typer.echo("No consistency problems found.")
        else:
            for finding in findings:
                typer.echo(
                    f"[{finding['severity']}] {finding['code']} {finding['entity']}: "
                    f"{finding['message']}"
                )
            raise typer.Exit(1 if any(f["severity"] == "error" for f in findings) else 0)
    except HarnessError as exc:
        _handle_error(exc)


@app.command()
def summary(json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().summary(), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@task_app.command("start")
def task_start(
    task_type: str = typer.Option(..., "--type"),
    goal: str | None = typer.Option(None),
    goal_file: Path | None = typer.Option(None),
    criterion: list[str] | None = typer.Option(None, "--criterion"),
    criteria_file: Path | None = typer.Option(None),
    constraint: list[str] | None = typer.Option(None, "--constraint"),
    constraints_file: Path | None = typer.Option(None),
    requirement: list[str] | None = typer.Option(None, "--requirement"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        fixed_goal = _read_text(goal, goal_file, "goal")
        task = _harness().start_task(
            task_type,
            fixed_goal,
            _read_list(criterion, criteria_file),
            _read_list(constraint, constraints_file),
            requirement or [],
        )
        _emit({"id": task.id, "status": task.status, "goal": task.original_goal}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@task_app.command("step")
def task_step(
    task_id: str,
    event_type: str = typer.Option("observation_recorded", "--type"),
    summary: str = typer.Option(...),
    payload_file: Path | None = typer.Option(None),
    evidence: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        payload = _read_mapping(payload_file)
        event = _harness().add_task_step(task_id, event_type, summary, payload, evidence)
        _emit({"id": event.id, "sequence": event.sequence_number, "type": event.event_type}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@task_app.command("revise-plan")
def task_revise_plan(
    task_id: str,
    plan_file: Path = typer.Option(...),
    reason: str = typer.Option(...),
) -> None:
    try:
        event = _harness().revise_task_plan(task_id, plan_file.read_text(encoding="utf-8"), reason)
        typer.echo(event.id)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@task_app.command("succeed")
def task_succeed(
    task_id: str,
    summary: str | None = typer.Option(None),
    summary_file: Path | None = typer.Option(None),
    result_type: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        text = _read_text(summary, summary_file, "summary")
        task = _harness().complete_task(task_id, True, text, result_type)
        _emit({"id": task.id, "status": task.status, "result_type": task.result_type}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@task_app.command("fail")
def task_fail(
    task_id: str,
    summary: str | None = typer.Option(None),
    summary_file: Path | None = typer.Option(None),
    reason: str = typer.Option(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        text = _read_text(summary, summary_file, "summary")
        task = _harness().complete_task(task_id, False, text, failure_reason=reason)
        _emit({"id": task.id, "status": task.status, "failure_reason": task.failure_reason}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    task_id: str = typer.Argument(...),
    capture: list[Path] | None = typer.Option(None, "--capture"),
    timeout: float | None = typer.Option(None),
    dataset_manifest_hash: str | None = typer.Option(None),
    model_hash: str | None = typer.Option(None),
    weight_hash: str | None = typer.Option(None),
    prompt_hash: str | None = typer.Option(None),
    random_seed: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a command and capture logs, snapshot, and produced evidence.

    Put the command after `--`, for example: `harness run TASK-... -- pytest -q`.
    """
    try:
        command = list(ctx.args)
        result = _harness().run_command(
            task_id,
            command,
            capture or [],
            timeout,
            dataset_manifest_hash=dataset_manifest_hash,
            model_hash=model_hash,
            weight_hash=weight_hash,
            prompt_hash=prompt_hash,
            random_seed=random_seed,
        )
        if json_output:
            _emit(result, True)
        else:
            typer.echo(result["stdout"], nl=not result["stdout"].endswith("\n"))
            if result["stderr"]:
                typer.echo(result["stderr"], err=True)
            typer.echo(f"evidence: {result['evidence_id']}")
            if result["exit_code"] != 0:
                raise typer.Exit(result["exit_code"] if result["exit_code"] <= 125 else 1)
    except HarnessError as exc:
        _handle_error(exc)


@evidence_app.command("capture")
def evidence_capture(
    file: Path = typer.Option(...),
    evidence_type: str = typer.Option(..., "--type"),
    task_id: str | None = typer.Option(None, "--task"),
    metadata_file: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        evidence = _harness().capture_evidence(file, evidence_type, task_id, _read_mapping(metadata_file))
        _emit(_harness().evidence_data(evidence.id), json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@evidence_app.command("show")
def evidence_show(evidence_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().evidence_data(evidence_id), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@evidence_app.command("verify")
def evidence_verify(evidence_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        result = _harness().verify_evidence(evidence_id)
        _emit(result, json_output)
        if not result["valid"]:
            raise typer.Exit(1)
    except HarnessError as exc:
        _handle_error(exc)


@evidence_app.command("verify-all")
def evidence_verify_all(json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        result = _harness().verify_all_evidence()
        _emit(result, json_output)
        if any(not item["valid"] for item in result):
            raise typer.Exit(1)
    except HarnessError as exc:
        _handle_error(exc)


@conclusion_app.command("create")
def conclusion_create(
    claim: str | None = typer.Option(None),
    claim_file: Path | None = typer.Option(None),
    scope_file: Path | None = typer.Option(None),
    falsification: str = typer.Option(""),
    falsification_file: Path | None = typer.Option(None),
    details_file: Path | None = typer.Option(None),
    confidence: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        claim_text = _read_text(claim, claim_file, "claim")
        falsification_text = (
            falsification_file.read_text(encoding="utf-8") if falsification_file else falsification
        )
        details = details_file.read_text(encoding="utf-8") if details_file else ""
        conclusion = _harness().create_conclusion(
            claim_text, _read_mapping(scope_file), falsification_text, details, confidence
        )
        _emit({"id": conclusion.id, "status": conclusion.status, "claim": conclusion.claim}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@conclusion_app.command("support")
def conclusion_support(
    conclusion_id: str,
    evidence: list[str] = typer.Option(..., "--evidence"),
    reason: str = typer.Option(""),
    reason_file: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        reason_text = reason_file.read_text(encoding="utf-8") if reason_file else reason
        conclusion = _harness().support_conclusion(conclusion_id, evidence, reason_text)
        _emit({"id": conclusion.id, "status": conclusion.status}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@conclusion_app.command("refute")
def conclusion_refute(
    conclusion_id: str,
    evidence: list[str] = typer.Option(..., "--evidence"),
    reason: str = typer.Option(""),
    reason_file: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        reason_text = reason_file.read_text(encoding="utf-8") if reason_file else reason
        conclusion = _harness().refute_conclusion(conclusion_id, evidence, reason_text)
        _emit({"id": conclusion.id, "status": conclusion.status}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@conclusion_app.command("supersede")
def conclusion_supersede(
    conclusion_id: str,
    by: str = typer.Option(..., "--by"),
    reason: str = typer.Option(""),
    reason_file: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        reason_text = reason_file.read_text(encoding="utf-8") if reason_file else reason
        conclusion = _harness().supersede_conclusion(conclusion_id, by, reason_text)
        _emit({"id": conclusion.id, "status": conclusion.status, "superseded_by": by}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@requirement_app.command("create")
def requirement_create(
    description: str | None = typer.Option(None),
    description_file: Path | None = typer.Option(None),
    criterion: list[str] | None = typer.Option(None, "--criterion"),
    acceptance_file: Path | None = typer.Option(None),
    constraint: list[str] | None = typer.Option(None, "--constraint"),
    constraints_file: Path | None = typer.Option(None),
    priority: str = typer.Option("medium"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        fixed_description = _read_text(description, description_file, "description")
        req = _harness().create_requirement(
            fixed_description,
            _read_list(criterion, acceptance_file),
            _read_list(constraint, constraints_file),
            priority,
        )
        _emit({"id": req.id, "status": req.status, "description": req.original_description}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@requirement_app.command("accept")
def requirement_accept(requirement_id: str) -> None:
    try:
        typer.echo(_harness().transition_requirement(requirement_id, "accepted").id)
    except HarnessError as exc:
        _handle_error(exc)


@requirement_app.command("start")
def requirement_start(requirement_id: str) -> None:
    try:
        typer.echo(_harness().transition_requirement(requirement_id, "in_progress").id)
    except HarnessError as exc:
        _handle_error(exc)


@requirement_app.command("implemented")
def requirement_implemented(requirement_id: str) -> None:
    try:
        typer.echo(_harness().transition_requirement(requirement_id, "implemented").id)
    except HarnessError as exc:
        _handle_error(exc)


@requirement_app.command("plan")
def requirement_plan(
    requirement_id: str,
    file: Path = typer.Option(...),
    reason: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        plan = _harness().add_requirement_plan(
            requirement_id, file.read_text(encoding="utf-8"), reason
        )
        _emit({"id": plan.id, "version": plan.version}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@requirement_app.command("verify")
def requirement_verify(
    requirement_id: str,
    test_run: str = typer.Option(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        req = _harness().verify_requirement(requirement_id, test_run)
        _emit({"id": req.id, "status": req.status, "test_run": test_run}, json_output)
    except HarnessError as exc:
        _handle_error(exc)


@change_app.command("capture")
def change_capture(
    base: str = typer.Option(...),
    head: str = typer.Option(...),
    task_id: str | None = typer.Option(None, "--task"),
    requirement: list[str] | None = typer.Option(None, "--requirement"),
    pull_request: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        change = _harness().capture_change(base, head, task_id, requirement or [], pull_request)
        _emit(
            {
                "id": change.id,
                "base": change.base_commit,
                "head": change.head_commit,
                "patch_hash": change.patch_hash,
            },
            json_output,
        )
    except HarnessError as exc:
        _handle_error(exc)


@build_app.command("capture")
def build_capture(
    artifact: Path = typer.Option(...),
    status: str = typer.Option("succeeded"),
    change: str | None = typer.Option(None),
    container_digest: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        build = _harness().capture_build(artifact, status, change, container_digest)
        _emit(
            {
                "id": build.id,
                "status": build.status,
                "artifact_hash": build.artifact_hash,
                "commit": build.commit_sha,
            },
            json_output,
        )
    except HarnessError as exc:
        _handle_error(exc)


@test_app.command("define")
def test_define(
    name: str = typer.Option(...),
    test_type: str = typer.Option(..., "--type"),
    command: list[str] = typer.Option(..., "--command"),
    requirement: list[str] | None = typer.Option(None, "--requirement"),
    pass_criteria_file: Path | None = typer.Option(None),
    environment_file: Path | None = typer.Option(None),
    data_file: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        spec = _harness().define_test(
            name,
            test_type,
            command,
            requirement or [],
            _read_mapping(pass_criteria_file) or {"exit_code": 0},
            _read_mapping(environment_file),
            _read_mapping(data_file),
        )
        _emit({"id": spec.id, "name": spec.name, "type": spec.test_type}, json_output)
    except (HarnessError, OSError) as exc:
        _handle_error(exc)


@test_app.command("run")
def test_run_command(
    test_spec_id: str,
    task_id: str | None = typer.Option(None, "--task"),
    build_id: str | None = typer.Option(None, "--build"),
    timeout: float | None = typer.Option(None),
    junit: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        run = _harness().run_test(test_spec_id, task_id, build_id, timeout, junit)
        _emit(
            {
                "id": run.id,
                "status": run.status,
                "passed": run.passed_count,
                "total": run.total_count,
                "evidence_id": run.evidence_id,
            },
            json_output,
        )
        if run.status not in {"passed"}:
            raise typer.Exit(1)
    except HarnessError as exc:
        _handle_error(exc)


@project_app.command("update")
def project_update(
    description: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    repository_uri: str | None = typer.Option(None),
    default_branch: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        project = _harness().update_project(
            description=description, status=status, repository_uri=repository_uri, default_branch=default_branch
        )
        _emit({"id": project.id, "status": project.status, "description": project.description}, json_output)
    except HarnessError as exc:
        _handle_error(exc)


@task_app.command("show")
def task_show(task_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().task_data(task_id), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@task_app.command("list")
def task_list(status: str | None = typer.Option(None), json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().list_tasks(status), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@conclusion_app.command("show")
def conclusion_show(conclusion_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().conclusion_data(conclusion_id), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@conclusion_app.command("list")
def conclusion_list(status: str | None = typer.Option(None), json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().list_conclusions(status), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@requirement_app.command("show")
def requirement_show(requirement_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().requirement_data(requirement_id), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@requirement_app.command("list")
def requirement_list(status: str | None = typer.Option(None), json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().list_requirements(status), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@test_app.command("import")
def test_import(
    test_spec_id: str,
    junit: Path = typer.Option(...),
    task_id: str | None = typer.Option(None, "--task"),
    build_id: str | None = typer.Option(None, "--build"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        run = _harness().import_junit(test_spec_id, junit, task_id, build_id)
        _emit(_harness().test_run_data(run.id), json_output)
        if run.status != "passed":
            raise typer.Exit(1)
    except HarnessError as exc:
        _handle_error(exc)


@test_app.command("show")
def test_show(test_run_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        _emit(_harness().test_run_data(test_run_id), json_output)
    except HarnessError as exc:
        _handle_error(exc)


@app.callback()
def main() -> None:
    """RE Harness CLI."""


if __name__ == "__main__":
    app()
