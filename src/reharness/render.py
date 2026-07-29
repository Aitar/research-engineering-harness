from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Build,
    Conclusion,
    Evidence,
    Project,
    Relation,
    Requirement,
    RequirementPlanVersion,
    Task,
    TaskEvent,
    TestRun,
    TestSpec,
)
from .utils import json_loads

DOCS_DIR = "harness-docs"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_brief(session: Session, root: Path, project: Project) -> Path:
    tasks = session.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())
    ).all()
    conclusions = session.scalars(
        select(Conclusion)
        .where(Conclusion.project_id == project.id)
        .order_by(Conclusion.updated_at.desc())
    ).all()
    requirements = session.scalars(
        select(Requirement)
        .where(Requirement.project_id == project.id)
        .order_by(Requirement.updated_at.desc())
    ).all()
    builds = session.scalars(
        select(Build).where(Build.project_id == project.id).order_by(Build.created_at.desc())
    ).all()
    tests = session.scalars(select(TestRun).order_by(TestRun.started_at.desc())).all()

    lines = [
        f"# {project.name} — Project Brief",
        "",
        f"> Status: `{project.status}`",
        "",
        "## Project goal",
        "",
        project.description or "No project description recorded.",
        "",
        "## Current conclusions",
        "",
    ]
    if conclusions:
        for item in conclusions:
            lines.append(
                f"- **[{item.id}](conclusions/{item.id}.md)** `{item.status}` — {item.claim}"
            )
    else:
        lines.append("- No conclusions recorded.")

    lines += ["", "## Active work", ""]
    active = [task for task in tasks if task.status == "in_progress"]
    if active:
        for task in active[:20]:
            lines.append(f"- **[{task.id}](tasks/{task.id}.md)** `{task.task_type}` — {task.original_goal}")
    else:
        lines.append("- No active tasks.")

    lines += ["", "## Requirements", ""]
    if requirements:
        for req in requirements[:30]:
            lines.append(
                f"- **[{req.id}](requirements/{req.id}.md)** `{req.status}` — {req.original_description}"
            )
    else:
        lines.append("- No requirements recorded.")

    lines += ["", "## Latest build and test status", ""]
    if builds:
        build = builds[0]
        lines.append(
            f"- Latest build: **{build.id}** `{build.status}` at commit "
            f"`{build.commit_sha or 'unknown'}`"
        )
    else:
        lines.append("- No builds recorded.")
    if tests:
        test = tests[0]
        lines.append(
            f"- Latest test run: **{test.id}** `{test.status}` "
            f"({test.passed_count}/{test.total_count} passed)"
        )
    else:
        lines.append("- No test runs recorded.")

    lines += ["", "## Risks and incomplete state", ""]
    exploring = [c for c in conclusions if c.status == "exploring"]
    failed_tasks = [t for t in tasks if t.status == "failed"]
    unverified = [r for r in requirements if r.status in {"accepted", "in_progress", "implemented"}]
    if exploring:
        lines.append(f"- {len(exploring)} conclusion(s) remain exploring.")
    if failed_tasks:
        lines.append(f"- {len(failed_tasks)} task(s) failed and may need follow-up.")
    if unverified:
        lines.append(f"- {len(unverified)} accepted requirement(s) are not verified.")
    if not (exploring or failed_tasks or unverified):
        lines.append("- No automatically detected project risks.")

    path = root / DOCS_DIR / "project-brief.md"
    _write(path, "\n".join(lines))
    return path


def render_task(session: Session, root: Path, task: Task) -> Path:
    events = session.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task.id)
        .order_by(TaskEvent.sequence_number)
    ).all()
    lines = [
        f"# {task.id}",
        "",
        f"- Type: `{task.task_type}`",
        f"- Status: `{task.status}`",
        f"- Started: `{task.started_at.isoformat()}`",
        f"- Completed: `{task.completed_at.isoformat() if task.completed_at else '—'}`",
        "",
        "## Fixed goal",
        "",
        task.original_goal,
        "",
        "## Success criteria",
        "",
    ]
    criteria = json_loads(task.success_criteria_json, [])
    lines.extend([f"- {item}" for item in criteria] or ["- None recorded."])
    lines += ["", "## Constraints", ""]
    constraints = json_loads(task.constraints_json, [])
    lines.extend([f"- {item}" for item in constraints] or ["- None recorded."])
    lines += ["", "## Event timeline", ""]
    if events:
        for event in events:
            lines += [
                f"### {event.sequence_number}. `{event.event_type}`",
                "",
                f"{event.created_at.isoformat()} — {event.summary}",
                "",
            ]
            payload = json_loads(event.payload_json, {})
            if payload:
                lines += ["```json", __import__("json").dumps(payload, ensure_ascii=False, indent=2), "```", ""]
            if event.evidence_id:
                lines += [f"Evidence: `{event.evidence_id}`", ""]
    else:
        lines.append("No task events recorded.")
    lines += ["", "## Result", "", task.result_summary or "Task has not been completed."]
    if task.failure_reason:
        lines += ["", "## Failure reason", "", task.failure_reason]
    path = root / DOCS_DIR / "tasks" / f"{task.id}.md"
    _write(path, "\n".join(lines))
    return path


def _relation_evidence(session: Session, conclusion_id: str, relation_type: str) -> list[tuple[Relation, Evidence | None]]:
    relations = session.scalars(
        select(Relation).where(
            Relation.source_type == "conclusion",
            Relation.source_id == conclusion_id,
            Relation.relation_type == relation_type,
        )
    ).all()
    output = []
    for relation in relations:
        evidence = session.get(Evidence, relation.target_id) if relation.target_type == "evidence" else None
        output.append((relation, evidence))
    return output


def render_conclusion(session: Session, root: Path, conclusion: Conclusion) -> Path:
    support = _relation_evidence(session, conclusion.id, "supports")
    refute = _relation_evidence(session, conclusion.id, "refutes")
    lines = [
        f"# {conclusion.id}",
        "",
        f"- Status: `{conclusion.status}`",
        f"- Confidence: `{conclusion.confidence or 'unspecified'}`",
        f"- Superseded by: `{conclusion.superseded_by or '—'}`",
        "",
        "## Claim",
        "",
        conclusion.claim,
        "",
        "## Scope",
        "",
        "```json",
        __import__("json").dumps(json_loads(conclusion.scope_json, {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Falsification criteria",
        "",
        conclusion.falsification_criteria or "Not recorded.",
        "",
        "## Supporting evidence",
        "",
    ]
    lines.extend(
        [f"- `{e.id}` — `{e.sha256}` — {e.storage_uri}" for _, e in support if e]
        or ["- None."]
    )
    lines += ["", "## Refuting evidence", ""]
    lines.extend(
        [f"- `{e.id}` — `{e.sha256}` — {e.storage_uri}" for _, e in refute if e]
        or ["- None."]
    )
    lines += ["", "## Detailed analysis", "", conclusion.details_markdown or "No details recorded."]
    path = root / DOCS_DIR / "conclusions" / f"{conclusion.id}.md"
    _write(path, "\n".join(lines))
    return path


def render_requirement(session: Session, root: Path, req: Requirement) -> Path:
    plans = session.scalars(
        select(RequirementPlanVersion)
        .where(RequirementPlanVersion.requirement_id == req.id)
        .order_by(RequirementPlanVersion.version)
    ).all()
    outgoing_relations = session.scalars(
        select(Relation).where(Relation.source_type == "requirement", Relation.source_id == req.id)
    ).all()
    incoming_relations = session.scalars(
        select(Relation).where(Relation.target_type == "requirement", Relation.target_id == req.id)
    ).all()
    lines = [
        f"# {req.id}",
        "",
        f"- Status: `{req.status}`",
        f"- Priority: `{req.priority}`",
        f"- Superseded by: `{req.superseded_by or '—'}`",
        "",
        "## Original description",
        "",
        req.original_description,
        "",
        "## Acceptance criteria",
        "",
    ]
    lines.extend(
        [f"- {item}" for item in json_loads(req.acceptance_criteria_json, [])]
        or ["- None recorded."]
    )
    lines += ["", "## Constraints", ""]
    lines.extend(
        [f"- {item}" for item in json_loads(req.constraints_json, [])]
        or ["- None recorded."]
    )
    lines += ["", "## Plan versions", ""]
    if plans:
        for plan in plans:
            lines += [
                f"### Version {plan.version}",
                "",
                plan.plan_markdown,
                "",
                f"Reason: {plan.reason_for_change or 'Initial plan'}",
                "",
            ]
    else:
        lines.append("No plan recorded.")
    lines += ["", "## Relations", ""]
    relation_lines = [
        f"- `{rel.relation_type}` → `{rel.target_type}:{rel.target_id}`"
        for rel in outgoing_relations
    ]
    relation_lines.extend(
        f"- `{rel.source_type}:{rel.source_id}` → `{rel.relation_type}` → this requirement"
        for rel in incoming_relations
    )
    lines.extend(relation_lines or ["- None."])
    path = root / DOCS_DIR / "requirements" / f"{req.id}.md"
    _write(path, "\n".join(lines))
    return path


def render_indexes(session: Session, root: Path, project: Project) -> None:
    conclusions = session.scalars(
        select(Conclusion).where(Conclusion.project_id == project.id).order_by(Conclusion.id)
    ).all()
    requirements = session.scalars(
        select(Requirement).where(Requirement.project_id == project.id).order_by(Requirement.id)
    ).all()
    tests = session.scalars(
        select(TestSpec).where(TestSpec.project_id == project.id).order_by(TestSpec.id)
    ).all()

    c_lines = ["# Conclusion Index", ""]
    c_lines.extend(
        [f"- [{c.id}]({c.id}.md) `{c.status}` — {c.claim}" for c in conclusions]
        or ["- No conclusions."]
    )
    _write(root / DOCS_DIR / "conclusions" / "index.md", "\n".join(c_lines))

    r_lines = ["# Requirement Index", ""]
    r_lines.extend(
        [f"- [{r.id}]({r.id}.md) `{r.status}` — {r.original_description}" for r in requirements]
        or ["- No requirements."]
    )
    _write(root / DOCS_DIR / "requirements" / "index.md", "\n".join(r_lines))

    t_lines = ["# Test Specification Index", ""]
    t_lines.extend(
        [f"- `{t.id}` `{t.test_type}` — {t.name}" for t in tests] or ["- No test specifications."]
    )
    _write(root / DOCS_DIR / "tests" / "index.md", "\n".join(t_lines))


def render_all(session: Session, root: Path, project: Project) -> None:
    render_brief(session, root, project)
    for task in session.scalars(select(Task).where(Task.project_id == project.id)).all():
        render_task(session, root, task)
    for conclusion in session.scalars(
        select(Conclusion).where(Conclusion.project_id == project.id)
    ).all():
        render_conclusion(session, root, conclusion)
    for req in session.scalars(select(Requirement).where(Requirement.project_id == project.id)).all():
        render_requirement(session, root, req)
    render_indexes(session, root, project)
