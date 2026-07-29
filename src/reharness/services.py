from __future__ import annotations

import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .db import HARNESS_DIR, init_database, session_scope
from .models import (
    Artifact,
    AuditEvent,
    Build,
    Change,
    Conclusion,
    Evidence,
    Project,
    Relation,
    Requirement,
    RequirementPlanVersion,
    SearchIndexState,
    Snapshot,
    Task,
    TaskEvent,
    TestRun,
    TestSpec,
)
from .render import render_all, render_brief, render_conclusion, render_requirement, render_task
from .retrieval.index import mark_index_stale
from .retrieval.models import SearchQuery
from .retrieval.service import RetrievalService
from .utils import (
    environment_snapshot,
    git_snapshot,
    json_dumps,
    json_loads,
    new_id,
    now_utc,
    run_git,
    sha256_bytes,
    sha256_file,
)

PROJECT_CONFIG = "config.yaml"
ARTIFACT_DIR = "harness-artifacts"

TASK_TYPES = {"research", "development", "debugging", "testing", "maintenance", "review"}
TASK_RESULT_TYPES = {"positive", "negative", "inconclusive", None}
CONCLUSION_STATUSES = {"exploring", "supported", "refuted", "superseded"}
REQUIREMENT_STATUSES = {
    "draft",
    "accepted",
    "in_progress",
    "implemented",
    "verified",
    "rejected",
    "superseded",
}
TEST_TYPES = {
    "unit",
    "integration",
    "smoke",
    "regression",
    "end_to_end",
    "performance",
    "security",
    "migration",
    "compatibility",
}
TASK_EVENT_TYPES = {
    "task_created",
    "goal_clarified",
    "plan_created",
    "plan_revised",
    "file_inspected",
    "code_changed",
    "command_started",
    "command_completed",
    "command_failed",
    "observation_recorded",
    "evidence_captured",
    "test_started",
    "test_completed",
    "commit_created",
    "build_created",
    "task_succeeded",
    "task_failed",
    "correction_added",
}
EVIDENCE_TYPES = {
    "command_log",
    "test_report",
    "experiment_result",
    "source_patch",
    "reproduction_script",
    "dataset_manifest",
    "model_manifest",
    "environment_snapshot",
    "build_artifact",
    "benchmark_result",
    "ci_report",
    "human_review",
}
PASS_CRITERIA_KEYS = {"exit_code", "max_failures", "min_passed", "min_total", "max_skipped"}
RELATIONS = {
    "supports",
    "refutes",
    "supersedes",
    "derived_from",
    "implements",
    "verifies",
    "evaluates",
    "produces",
    "depends_on",
    "reproduces",
    "contradicts",
    "covers",
    "follow_up_to",
    "retry_of",
    "extends",
}
RELATION_RULES = {
    ("conclusion", "supports", "evidence"),
    ("conclusion", "refutes", "evidence"),
    ("conclusion", "supersedes", "conclusion"),
    ("conclusion", "derived_from", "conclusion"),
    ("conclusion", "derived_from", "evidence"),
    ("conclusion", "contradicts", "conclusion"),
    ("task", "implements", "requirement"),
    ("task", "produces", "change"),
    ("task", "follow_up_to", "task"),
    ("task", "retry_of", "task"),
    ("task", "extends", "task"),
    ("task", "depends_on", "task"),
    ("change", "produces", "evidence"),
    ("change", "produces", "build"),
    ("change", "implements", "requirement"),
    ("change", "depends_on", "change"),
    ("build", "produces", "evidence"),
    ("build", "depends_on", "build"),
    ("test_spec", "covers", "requirement"),
    ("test_spec", "depends_on", "test_spec"),
    ("test_run", "produces", "evidence"),
    ("test_run", "evaluates", "build"),
    ("test_run", "verifies", "requirement"),
    ("snapshot", "produces", "evidence"),
    ("evidence", "produces", "evidence"),
    ("evidence", "derived_from", "evidence"),
    ("evidence", "reproduces", "evidence"),
    ("evidence", "contradicts", "evidence"),
    ("requirement", "depends_on", "requirement"),
}


class HarnessError(RuntimeError):
    pass


class StateTransitionError(HarnessError):
    pass


class NotFoundError(HarnessError):
    pass


def discover_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / HARNESS_DIR / PROJECT_CONFIG).exists():
            return candidate
    raise HarnessError("Not inside a harness project. Run `harness init` first.")


def read_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return text


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        lines = [line.strip().lstrip("- ").strip() for line in value.splitlines()]
        return [line for line in lines if line]
    raise HarnessError("Expected a list or text file with one item per line.")


def _entity_exists(session: Session, entity_type: str, entity_id: str) -> bool:
    model_map = {
        "project": Project,
        "task": Task,
        "conclusion": Conclusion,
        "requirement": Requirement,
        "change": Change,
        "build": Build,
        "test_spec": TestSpec,
        "test_run": TestRun,
        "evidence": Evidence,
        "snapshot": Snapshot,
    }
    model = model_map.get(entity_type)
    return model is not None and session.get(model, entity_id) is not None


class Harness:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _schedule_render(
        self,
        session: Session,
        *,
        project_id: str,
        task_ids: Iterable[str] = (),
        conclusion_ids: Iterable[str] = (),
        requirement_ids: Iterable[str] = (),
        brief: bool = False,
        all_views: bool = False,
    ) -> None:
        state = session.info.setdefault(
            "render_state",
            {
                "project_id": project_id,
                "task_ids": set(),
                "conclusion_ids": set(),
                "requirement_ids": set(),
                "brief": False,
                "all_views": False,
            },
        )
        state["task_ids"].update(task_ids)
        state["conclusion_ids"].update(conclusion_ids)
        state["requirement_ids"].update(requirement_ids)
        state["brief"] = state["brief"] or brief
        state["all_views"] = state["all_views"] or all_views
        if session.info.get("render_callback_registered"):
            return
        session.info["render_callback_registered"] = True
        session.info.setdefault("after_commit", []).append(lambda: self._flush_render_state(state))

    def _flush_render_state(self, state: dict[str, Any]) -> None:
        stale_path = self.root / HARNESS_DIR / "render-stale.json"
        try:
            with session_scope(self.root, write=False) as session:
                project = session.get(Project, state["project_id"])
                if project is None:
                    return
                if state["all_views"]:
                    render_all(session, self.root, project)
                else:
                    for task_id in state["task_ids"]:
                        task = session.get(Task, task_id)
                        if task is not None:
                            render_task(session, self.root, task)
                    for conclusion_id in state["conclusion_ids"]:
                        conclusion = session.get(Conclusion, conclusion_id)
                        if conclusion is not None:
                            render_conclusion(session, self.root, conclusion)
                    for requirement_id in state["requirement_ids"]:
                        requirement = session.get(Requirement, requirement_id)
                        if requirement is not None:
                            render_requirement(session, self.root, requirement)
                    if state["brief"]:
                        render_brief(session, self.root, project)
            stale_path.unlink(missing_ok=True)
        except Exception as exc:
            stale_path.parent.mkdir(parents=True, exist_ok=True)
            stale_path.write_text(
                json_dumps({"error": str(exc), "state": state, "recorded_at": now_utc()}),
                encoding="utf-8",
            )

    def _active_task(self, session: Session, task_id: str) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task.status != "in_progress":
            raise StateTransitionError("Cannot attach new work to a completed task.")
        return task

    def _assert_evidence_integrity(self, evidence: Evidence) -> Path:
        path = Path(evidence.storage_uri)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists() or not path.is_file():
            raise HarnessError(f"Evidence is unavailable: {evidence.id}")
        actual = sha256_file(path)
        if actual != evidence.sha256:
            raise HarnessError(f"Evidence integrity check failed: {evidence.id}")
        return path

    @staticmethod
    def _validated_pass_criteria(criteria: dict[str, Any] | None) -> dict[str, int]:
        value = dict(criteria or {"exit_code": 0, "min_total": 1})
        unknown = set(value) - PASS_CRITERIA_KEYS
        if unknown:
            raise HarnessError(f"Unsupported pass criteria: {', '.join(sorted(unknown))}")
        validated: dict[str, int] = {}
        for key, raw in value.items():
            if isinstance(raw, bool):
                raise HarnessError(f"Pass criterion {key} must be an integer.")
            try:
                parsed = int(raw)
            except (TypeError, ValueError) as exc:
                raise HarnessError(f"Pass criterion {key} must be an integer.") from exc
            if key != "exit_code" and parsed < 0:
                raise HarnessError(f"Pass criterion {key} cannot be negative.")
            validated[key] = parsed
        validated.setdefault("exit_code", 0)
        validated.setdefault("min_total", 1)
        return validated

    @classmethod
    def open(cls, root: Path | None = None) -> "Harness":
        return cls(discover_root(root))

    @classmethod
    def initialize(
        cls,
        root: Path,
        name: str,
        description: str = "",
        repository_uri: str | None = None,
    ) -> "Harness":
        if not name.strip():
            raise HarnessError("Project name cannot be empty.")
        root = root.resolve()
        config_path = root / HARNESS_DIR / PROJECT_CONFIG
        if config_path.exists():
            raise HarnessError(f"Harness already initialized at {root}")
        (root / HARNESS_DIR).mkdir(parents=True, exist_ok=True)
        (root / ARTIFACT_DIR).mkdir(parents=True, exist_ok=True)
        init_database(root)
        git_info = git_snapshot(root)
        config = {
            "schema_version": 1,
            "name": name,
            "artifact_dir": ARTIFACT_DIR,
            "docs_dir": "harness-docs",
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        harness = cls(root)
        with session_scope(root) as session:
            now = now_utc()
            project = Project(
                id=new_id("project"),
                name=name,
                description=description,
                status="active",
                repository_uri=repository_uri or git_info.get("repository"),
                default_branch=git_info.get("branch"),
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            session.flush()
            harness._audit(session, project.id, "project_initialized", "project", project.id, config)
            harness._schedule_render(session, project_id=project.id, all_views=True)
        return harness

    def _project(self, session: Session) -> Project:
        project = session.scalar(select(Project).limit(1))
        if project is None:
            raise HarnessError("Project database is missing its project record.")
        return project

    def _audit(
        self,
        session: Session,
        project_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        audit = AuditEvent(
            id=new_id("audit"),
            project_id=project_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload_json=json_dumps(payload or {}),
            created_at=now_utc(),
        )
        session.add(audit)
        mark_index_stale(session, project_id)
        return audit

    def _add_relation(
        self,
        session: Session,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Relation:
        if relation_type not in RELATIONS:
            raise HarnessError(f"Unsupported relation: {relation_type}")
        if not _entity_exists(session, source_type, source_id):
            raise NotFoundError(f"Missing source {source_type}:{source_id}")
        if not _entity_exists(session, target_type, target_id):
            raise NotFoundError(f"Missing target {target_type}:{target_id}")
        if (source_type, relation_type, target_type) not in RELATION_RULES:
            raise HarnessError(
                f"Invalid relation shape: {source_type} {relation_type} {target_type}"
            )
        existing = session.scalar(
            select(Relation).where(
                Relation.source_type == source_type,
                Relation.source_id == source_id,
                Relation.relation_type == relation_type,
                Relation.target_type == target_type,
                Relation.target_id == target_id,
            )
        )
        if existing is not None:
            return existing
        relation = Relation(
            id=new_id("relation"),
            source_type=source_type,
            source_id=source_id,
            relation_type=relation_type,
            target_type=target_type,
            target_id=target_id,
            metadata_json=json_dumps(metadata or {}),
            created_at=now_utc(),
        )
        session.add(relation)
        return relation

    def project_data(self) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            p = self._project(session)
            return {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "status": p.status,
                "repository_uri": p.repository_uri,
                "default_branch": p.default_branch,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }

    def refresh(self) -> Path:
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            render_all(session, self.root, project)
        (self.root / HARNESS_DIR / "render-stale.json").unlink(missing_ok=True)
        return self.root / "harness-docs" / "project-brief.md"

    def brief(self, level: str = "normal") -> str:
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            path = render_brief(session, self.root, project)
            content = path.read_text(encoding="utf-8")
            if level == "full":
                return content
            if level == "compact":
                lines = content.splitlines()
                return "\n".join(lines[:50]).rstrip() + "\n"
            if level != "normal":
                raise HarnessError("Brief level must be compact, normal, or full.")
            return content

    def context(
        self,
        topic: str = "",
        budget: int = 12000,
        strategy: str = "hybrid",
    ) -> str:
        try:
            return RetrievalService(self.root).build_context(
                SearchQuery(text=topic, strategy=strategy, limit=20, graph_depth=1),
                budget=budget,
            )
        except (ValueError, RuntimeError) as exc:
            raise HarnessError(str(exc)) from exc

    def search_history(
        self,
        text_query: str = "",
        *,
        entity_types: Iterable[str] = (),
        statuses: Iterable[str] = (),
        evidence_types: Iterable[str] = (),
        task_id: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = 20,
        strategy: str = "hybrid",
        graph_depth: int = 1,
        include_superseded: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            results = RetrievalService(self.root).search(
                SearchQuery(
                    text=text_query,
                    entity_types=tuple(entity_types),
                    statuses=tuple(statuses),
                    evidence_types=tuple(evidence_types),
                    task_id=task_id,
                    since=since,
                    until=until,
                    limit=limit,
                    strategy=strategy,
                    graph_depth=graph_depth,
                    include_superseded=include_superseded,
                )
            )
            return [result.to_dict() for result in results]
        except (ValueError, RuntimeError) as exc:
            raise HarnessError(str(exc)) from exc

    def trace_history(self, entity_id: str, depth: int = 1, max_nodes: int = 50) -> dict[str, Any]:
        try:
            return RetrievalService(self.root).trace(entity_id, depth=depth, max_nodes=max_nodes)
        except (ValueError, RuntimeError) as exc:
            raise HarnessError(str(exc)) from exc

    def list_evidence_history(
        self,
        *,
        evidence_type: str | None = None,
        task_id: str | None = None,
        integrity: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        try:
            return RetrievalService(self.root).list_evidence(
                evidence_type=evidence_type,
                task_id=task_id,
                integrity=integrity,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        except (ValueError, RuntimeError) as exc:
            raise HarnessError(str(exc)) from exc

    def evidence_usage(self, evidence_id: str) -> dict[str, Any]:
        try:
            return RetrievalService(self.root).evidence_usage(evidence_id)
        except (ValueError, RuntimeError) as exc:
            raise HarnessError(str(exc)) from exc

    def index_status(self) -> dict[str, Any]:
        try:
            return RetrievalService(self.root).index_status()
        except RuntimeError as exc:
            raise HarnessError(str(exc)) from exc

    def index_rebuild(self) -> dict[str, Any]:
        try:
            return RetrievalService(self.root).index_rebuild()
        except RuntimeError as exc:
            raise HarnessError(str(exc)) from exc

    def index_verify(self) -> dict[str, Any]:
        try:
            return RetrievalService(self.root).index_verify()
        except RuntimeError as exc:
            raise HarnessError(str(exc)) from exc

    def start_task(
        self,
        task_type: str,
        goal: str,
        success_criteria: Iterable[str] = (),
        constraints: Iterable[str] = (),
        requirement_ids: Iterable[str] = (),
    ) -> Task:
        if task_type not in TASK_TYPES:
            raise HarnessError(f"Unsupported task type: {task_type}")
        if not goal.strip():
            raise HarnessError("Task goal cannot be empty.")
        with session_scope(self.root) as session:
            project = self._project(session)
            now = now_utc()
            task = Task(
                id=new_id("task"),
                project_id=project.id,
                task_type=task_type,
                original_goal=goal.strip(),
                success_criteria_json=json_dumps(list(success_criteria)),
                constraints_json=json_dumps(list(constraints)),
                status="in_progress",
                created_at=now,
                started_at=now,
            )
            session.add(task)
            session.flush()
            self._add_task_event(session, task, "task_created", "Task created", {})
            for req_id in requirement_ids:
                req = session.get(Requirement, req_id)
                if req is None:
                    raise NotFoundError(f"Requirement not found: {req_id}")
                self._add_relation(session, "task", task.id, "implements", "requirement", req.id)
                if req.status == "accepted":
                    req.status = "in_progress"
                    req.updated_at = now_utc()
            self._audit(session, project.id, "task_started", "task", task.id)
            self._schedule_render(
                session, project_id=project.id, task_ids=[task.id], brief=True
            )
            return task

    def _add_task_event(
        self,
        session: Session,
        task: Task,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> TaskEvent:
        if event_type not in TASK_EVENT_TYPES:
            raise HarnessError(f"Unsupported task event type: {event_type}")
        if not summary.strip():
            raise HarnessError("Task event summary cannot be empty.")
        max_seq = session.scalar(
            select(func.max(TaskEvent.sequence_number)).where(TaskEvent.task_id == task.id)
        )
        event = TaskEvent(
            id=new_id("event"),
            task_id=task.id,
            sequence_number=(max_seq or 0) + 1,
            event_type=event_type,
            summary=summary,
            payload_json=json_dumps(payload or {}),
            evidence_id=evidence_id,
            created_at=now_utc(),
        )
        session.add(event)
        session.flush()
        return event

    def add_task_step(
        self,
        task_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> TaskEvent:
        with session_scope(self.root) as session:
            project = self._project(session)
            task = self._active_task(session, task_id)
            if evidence_id and session.get(Evidence, evidence_id) is None:
                raise NotFoundError(f"Evidence not found: {evidence_id}")
            event = self._add_task_event(session, task, event_type, summary, payload, evidence_id)
            self._audit(session, project.id, "task_step_added", "task", task.id, {"event_id": event.id})
            self._schedule_render(session, project_id=project.id, task_ids=[task.id])
            return event

    def revise_task_plan(self, task_id: str, plan: str, reason: str) -> TaskEvent:
        return self.add_task_step(
            task_id,
            "plan_revised",
            "Task plan revised",
            {"plan": plan, "reason": reason},
        )

    def complete_task(
        self,
        task_id: str,
        succeeded: bool,
        summary: str,
        result_type: str | None = None,
        failure_reason: str | None = None,
    ) -> Task:
        if result_type not in TASK_RESULT_TYPES:
            raise HarnessError("Result type must be positive, negative, inconclusive, or omitted.")
        if not summary.strip():
            raise HarnessError("Completed tasks require a non-empty result summary.")
        if not succeeded and not (failure_reason or "").strip():
            raise HarnessError("Failed tasks require a failure reason.")
        with session_scope(self.root) as session:
            project = self._project(session)
            task = session.get(Task, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "in_progress":
                raise StateTransitionError("Task is already completed.")
            task.status = "succeeded" if succeeded else "failed"
            task.result_type = result_type
            task.result_summary = summary
            task.failure_reason = failure_reason
            task.completed_at = now_utc()
            event_type = "task_succeeded" if succeeded else "task_failed"
            self._add_task_event(
                session,
                task,
                event_type,
                summary,
                {"result_type": result_type, "failure_reason": failure_reason},
            )
            self._audit(session, project.id, event_type, "task", task.id)
            self._schedule_render(
                session, project_id=project.id, task_ids=[task.id], brief=True
            )
            return task

    def create_snapshot(
        self,
        session: Session,
        project: Project,
        dataset_manifest_hash: str | None = None,
        model_hash: str | None = None,
        weight_hash: str | None = None,
        tokenizer_hash: str | None = None,
        prompt_hash: str | None = None,
        container_digest: str | None = None,
        random_seed: str | None = None,
        task_id: str | None = None,
    ) -> Snapshot:
        git_info = git_snapshot(self.root)
        env_info = environment_snapshot(self.root)
        git_metadata = {key: value for key, value in git_info.items() if key != "patch"}
        combined_environment = {"environment": env_info, "git": git_metadata}
        reproducibility = "full"
        if (
            git_info.get("commit") is None
            or git_info.get("dirty")
            or env_info.get("dependency_lock_hash") is None
        ):
            reproducibility = "partial"
        snapshot = Snapshot(
            id=new_id("snapshot"),
            project_id=project.id,
            git_commit=git_info.get("commit"),
            git_branch=git_info.get("branch"),
            git_dirty=bool(git_info.get("dirty")),
            patch_hash=git_info.get("patch_hash"),
            dataset_manifest_hash=dataset_manifest_hash,
            model_hash=model_hash,
            weight_hash=weight_hash,
            tokenizer_hash=tokenizer_hash,
            prompt_hash=prompt_hash,
            dependency_lock_hash=env_info.get("dependency_lock_hash"),
            container_digest=container_digest,
            environment_hash=sha256_bytes(json_dumps(combined_environment).encode("utf-8")),
            environment_json=json_dumps(combined_environment),
            hardware_json=json_dumps(
                {
                    "machine": env_info.get("machine"),
                    "platform": env_info.get("platform"),
                }
            ),
            random_seed=random_seed,
            reproducibility=reproducibility,
            created_at=now_utc(),
        )
        session.add(snapshot)
        session.flush()
        if git_info.get("patch"):
            patch_evidence = self._capture_text_in_session(
                session, project, str(git_info["patch"]), f"{snapshot.id}.patch", "source_patch", task_id,
                {"snapshot_id": snapshot.id, "dirty_worktree": True},
            )
            self._add_relation(session, "snapshot", snapshot.id, "produces", "evidence", patch_evidence.id)
        return snapshot

    def _artifact_target(self, evidence_id: str, source_name: str) -> Path:
        target_dir = self.root / ARTIFACT_DIR / "evidence" / evidence_id
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / Path(source_name).name

    def _capture_file_in_session(
        self,
        session: Session,
        project: Project,
        source: Path,
        evidence_type: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        copy: bool = True,
    ) -> Evidence:
        source = source.resolve()
        if not source.exists() or not source.is_file():
            raise HarnessError(f"Evidence file not found: {source}")
        evidence_id = new_id("evidence")
        target = self._artifact_target(evidence_id, source.name)
        if copy:
            shutil.copy2(source, target)
            session.info.setdefault("rollback_files", []).append(target)
        else:
            target = source
        digest = sha256_file(target)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        evidence = Evidence(
            id=evidence_id,
            project_id=project.id,
            task_id=task_id,
            evidence_type=evidence_type,
            storage_uri=str(target.relative_to(self.root)) if target.is_relative_to(self.root) else str(target),
            sha256=digest,
            size=target.stat().st_size,
            mime_type=mime,
            metadata_json=json_dumps(metadata or {}),
            created_at=now_utc(),
        )
        session.add(evidence)
        session.flush()
        artifact = Artifact(
            id=new_id("artifact"),
            project_id=project.id,
            evidence_id=evidence.id,
            path=evidence.storage_uri,
            sha256=digest,
            size=evidence.size,
            created_at=now_utc(),
        )
        session.add(artifact)
        return evidence

    def _capture_text_in_session(
        self,
        session: Session,
        project: Project,
        content: str,
        filename: str,
        evidence_type: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        temp_dir = self.root / HARNESS_DIR / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp = temp_dir / f"{new_id('artifact')}-{Path(filename).name}"
        temp.write_text(content, encoding="utf-8")
        try:
            return self._capture_file_in_session(
                session, project, temp, evidence_type, task_id, metadata, copy=True
            )
        finally:
            temp.unlink(missing_ok=True)

    def capture_evidence(
        self,
        source: Path,
        evidence_type: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        if evidence_type not in EVIDENCE_TYPES:
            raise HarnessError(f"Unsupported evidence type: {evidence_type}")
        with session_scope(self.root) as session:
            project = self._project(session)
            if task_id:
                self._active_task(session, task_id)
            evidence = self._capture_file_in_session(
                session, project, source, evidence_type, task_id, metadata
            )
            if task_id:
                task = session.get(Task, task_id)
                assert task is not None
                event = self._add_task_event(
                    session,
                    task,
                    "evidence_captured",
                    f"Captured {evidence_type} evidence",
                    {"source": str(source)},
                    evidence.id,
                )
                evidence.task_event_id = event.id
                self._schedule_render(session, project_id=project.id, task_ids=[task.id])
            self._audit(session, project.id, "evidence_captured", "evidence", evidence.id)
            return evidence

    def run_command(
        self,
        task_id: str,
        command: list[str],
        capture_paths: Iterable[Path] = (),
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        dataset_manifest_hash: str | None = None,
        model_hash: str | None = None,
        weight_hash: str | None = None,
        prompt_hash: str | None = None,
        random_seed: str | None = None,
    ) -> dict[str, Any]:
        if not command:
            raise HarnessError("Command cannot be empty.")
        with session_scope(self.root) as session:
            project = self._project(session)
            task = session.get(Task, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.status != "in_progress":
                raise StateTransitionError("Cannot run a command for a completed task.")
            snapshot = self.create_snapshot(
                session,
                project,
                dataset_manifest_hash=dataset_manifest_hash,
                model_hash=model_hash,
                weight_hash=weight_hash,
                prompt_hash=prompt_hash,
                random_seed=random_seed,
                task_id=task.id,
            )
            started = now_utc()
            start_event = self._add_task_event(
                session,
                task,
                "command_started",
                f"Started command: {shlex.join(command)}",
                {"command": command, "snapshot_id": snapshot.id},
            )
            session.flush()

            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)
            error_kind: str | None = None
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.root,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=proc_env,
                    check=False,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                returncode = 124
                stdout = exc.stdout or ""
                stderr = (exc.stderr or "") + f"\nCommand timed out after {timeout} seconds."
                error_kind = "timeout"
            except OSError as exc:
                returncode = 127
                stdout = ""
                stderr = str(exc)
                error_kind = "execution_error"

            finished = now_utc()
            log = {
                "command": command,
                "cwd": str(self.root),
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_seconds": (finished - started).total_seconds(),
                "exit_code": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "snapshot_id": snapshot.id,
                "error_kind": error_kind,
            }
            evidence = self._capture_text_in_session(
                session,
                project,
                json_dumps(log),
                "command-log.json",
                "command_log",
                task.id,
                {"snapshot_id": snapshot.id, "exit_code": returncode},
            )
            event_type = "command_completed" if returncode == 0 else "command_failed"
            end_event = self._add_task_event(
                session,
                task,
                event_type,
                f"Command exited with code {returncode}: {shlex.join(command)}",
                {
                    "command": command,
                    "exit_code": returncode,
                    "snapshot_id": snapshot.id,
                    "started_event_id": start_event.id,
                    "error_kind": error_kind,
                },
                evidence.id,
            )
            evidence.task_event_id = end_event.id

            captured = []
            for path in capture_paths:
                resolved = path if path.is_absolute() else self.root / path
                matches = list(resolved.parent.glob(resolved.name)) if any(c in resolved.name for c in "*?[") else [resolved]
                for match in matches:
                    if match.exists() and match.is_file():
                        extra = self._capture_file_in_session(
                            session,
                            project,
                            match,
                            "experiment_result",
                            task.id,
                            {"produced_by": evidence.id, "snapshot_id": snapshot.id},
                        )
                        captured.append(extra.id)
                        self._add_relation(
                            session, "evidence", evidence.id, "produces", "evidence", extra.id
                        )
            self._audit(
                session,
                project.id,
                event_type,
                "task",
                task.id,
                {"evidence_id": evidence.id, "captured": captured},
            )
            self._schedule_render(session, project_id=project.id, task_ids=[task.id])
            return {
                "task_id": task.id,
                "snapshot_id": snapshot.id,
                "evidence_id": evidence.id,
                "exit_code": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "captured_evidence_ids": captured,
            }

    def evidence_data(self, evidence_id: str) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise NotFoundError(f"Evidence not found: {evidence_id}")
            return {
                "id": evidence.id,
                "type": evidence.evidence_type,
                "storage_uri": evidence.storage_uri,
                "sha256": evidence.sha256,
                "size": evidence.size,
                "available": evidence.available,
                "integrity_status": evidence.integrity_status,
                "metadata": json_loads(evidence.metadata_json, {}),
                "created_at": evidence.created_at.isoformat(),
            }

    def verify_evidence(self, evidence_id: str) -> dict[str, Any]:
        with session_scope(self.root) as session:
            project = self._project(session)
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise NotFoundError(f"Evidence not found: {evidence_id}")
            path = Path(evidence.storage_uri)
            if not path.is_absolute():
                path = self.root / path
            if not path.exists():
                evidence.available = False
                evidence.integrity_status = "missing"
                valid = False
                actual = None
            else:
                actual = sha256_file(path)
                valid = actual == evidence.sha256
                evidence.available = True
                evidence.integrity_status = "valid" if valid else "mismatch"
            self._audit(
                session,
                project.id,
                "evidence_verified",
                "evidence",
                evidence.id,
                {"valid": valid, "actual_sha256": actual},
            )
            return {
                "evidence_id": evidence.id,
                "valid": valid,
                "expected_sha256": evidence.sha256,
                "actual_sha256": actual,
                "status": evidence.integrity_status,
            }

    def verify_all_evidence(self) -> list[dict[str, Any]]:
        with session_scope(self.root, write=False) as session:
            ids = list(session.scalars(select(Evidence.id)).all())
        return [self.verify_evidence(eid) for eid in ids]

    def create_conclusion(
        self,
        claim: str,
        scope: dict[str, Any] | None = None,
        falsification_criteria: str = "",
        details: str = "",
        confidence: str | None = None,
    ) -> Conclusion:
        if not claim.strip():
            raise HarnessError("Conclusion claim cannot be empty.")
        with session_scope(self.root) as session:
            project = self._project(session)
            now = now_utc()
            conclusion = Conclusion(
                id=new_id("conclusion"),
                project_id=project.id,
                claim=claim.strip(),
                status="exploring",
                scope_json=json_dumps(scope or {}),
                falsification_criteria=falsification_criteria,
                confidence=confidence,
                details_markdown=details,
                created_at=now,
                updated_at=now,
            )
            session.add(conclusion)
            session.flush()
            self._audit(session, project.id, "conclusion_created", "conclusion", conclusion.id)
            self._schedule_render(
                session, project_id=project.id, conclusion_ids=[conclusion.id], brief=True
            )
            return conclusion

    def _transition_conclusion(
        self,
        conclusion_id: str,
        target_status: str,
        evidence_ids: Iterable[str] = (),
        reason: str = "",
        replacement_id: str | None = None,
    ) -> Conclusion:
        if target_status not in CONCLUSION_STATUSES:
            raise HarnessError(f"Invalid conclusion status: {target_status}")
        allowed = {
            "exploring": {"supported", "refuted"},
            "supported": {"refuted", "superseded"},
            "refuted": {"superseded"},
            "superseded": set(),
        }
        with session_scope(self.root) as session:
            project = self._project(session)
            conclusion = session.get(Conclusion, conclusion_id)
            if conclusion is None:
                raise NotFoundError(f"Conclusion not found: {conclusion_id}")
            if target_status not in allowed[conclusion.status]:
                raise StateTransitionError(
                    f"Cannot transition conclusion from {conclusion.status} to {target_status}."
                )
            evidence_ids = list(evidence_ids)
            if target_status in {"supported", "refuted"} and not evidence_ids:
                raise HarnessError(f"{target_status} requires at least one evidence ID.")
            relation_type = "supports" if target_status == "supported" else "refutes"
            if target_status in {"supported", "refuted"}:
                for evidence_id in evidence_ids:
                    evidence = session.get(Evidence, evidence_id)
                    if evidence is None:
                        raise NotFoundError(f"Evidence not found: {evidence_id}")
                    self._assert_evidence_integrity(evidence)
                    self._add_relation(
                        session,
                        "conclusion",
                        conclusion.id,
                        relation_type,
                        "evidence",
                        evidence.id,
                        {"reason": reason},
                    )
            if target_status == "superseded":
                if not replacement_id:
                    raise HarnessError("Superseding requires a replacement conclusion.")
                replacement = session.get(Conclusion, replacement_id)
                if replacement is None:
                    raise NotFoundError(f"Replacement conclusion not found: {replacement_id}")
                if replacement.id == conclusion.id:
                    raise HarnessError("A conclusion cannot supersede itself.")
                conclusion.superseded_by = replacement.id
                self._add_relation(
                    session,
                    "conclusion",
                    replacement.id,
                    "supersedes",
                    "conclusion",
                    conclusion.id,
                    {"reason": reason},
                )
            conclusion.status = target_status
            conclusion.updated_at = now_utc()
            if reason:
                conclusion.details_markdown = (
                    conclusion.details_markdown.rstrip()
                    + f"\n\n## Status update {conclusion.updated_at.isoformat()}\n\n{reason}\n"
                ).lstrip()
            self._audit(
                session,
                project.id,
                f"conclusion_{target_status}",
                "conclusion",
                conclusion.id,
                {"evidence_ids": evidence_ids, "replacement_id": replacement_id},
            )
            render_ids = [conclusion.id]
            if replacement_id:
                render_ids.append(replacement_id)
            self._schedule_render(
                session, project_id=project.id, conclusion_ids=render_ids, brief=True
            )
            return conclusion

    def support_conclusion(self, conclusion_id: str, evidence_ids: Iterable[str], reason: str = "") -> Conclusion:
        return self._transition_conclusion(conclusion_id, "supported", evidence_ids, reason)

    def refute_conclusion(self, conclusion_id: str, evidence_ids: Iterable[str], reason: str = "") -> Conclusion:
        return self._transition_conclusion(conclusion_id, "refuted", evidence_ids, reason)

    def supersede_conclusion(self, conclusion_id: str, replacement_id: str, reason: str = "") -> Conclusion:
        return self._transition_conclusion(
            conclusion_id, "superseded", (), reason, replacement_id=replacement_id
        )

    def create_requirement(
        self,
        description: str,
        acceptance_criteria: Iterable[str] = (),
        constraints: Iterable[str] = (),
        priority: str = "medium",
    ) -> Requirement:
        if not description.strip():
            raise HarnessError("Requirement description cannot be empty.")
        if priority not in {"low", "medium", "high", "critical"}:
            raise HarnessError(f"Invalid requirement priority: {priority}")
        with session_scope(self.root) as session:
            project = self._project(session)
            now = now_utc()
            requirement = Requirement(
                id=new_id("requirement"),
                project_id=project.id,
                original_description=description.strip(),
                status="draft",
                priority=priority,
                acceptance_criteria_json=json_dumps(list(acceptance_criteria)),
                constraints_json=json_dumps(list(constraints)),
                created_at=now,
                updated_at=now,
            )
            session.add(requirement)
            session.flush()
            self._audit(session, project.id, "requirement_created", "requirement", requirement.id)
            self._schedule_render(
                session, project_id=project.id, requirement_ids=[requirement.id], brief=True
            )
            return requirement

    def transition_requirement(self, requirement_id: str, target: str) -> Requirement:
        if target not in REQUIREMENT_STATUSES:
            raise HarnessError(f"Invalid requirement status: {target}")
        allowed = {
            "draft": {"accepted", "rejected"},
            "accepted": {"in_progress", "rejected", "superseded"},
            "in_progress": {"implemented", "rejected", "superseded"},
            "implemented": {"verified", "in_progress", "superseded"},
            "verified": {"superseded"},
            "rejected": {"superseded"},
            "superseded": set(),
        }
        if target == "verified":
            raise StateTransitionError("Use verify_requirement with a passed test run.")
        with session_scope(self.root) as session:
            project = self._project(session)
            req = session.get(Requirement, requirement_id)
            if req is None:
                raise NotFoundError(f"Requirement not found: {requirement_id}")
            if target not in allowed[req.status]:
                raise StateTransitionError(f"Cannot transition requirement from {req.status} to {target}.")
            if target == "implemented":
                change_relation = session.scalar(
                    select(Relation).where(
                        Relation.source_type == "change",
                        Relation.relation_type == "implements",
                        Relation.target_type == "requirement",
                        Relation.target_id == req.id,
                    )
                )
                if change_relation is None:
                    raise HarnessError("Requirement cannot be implemented without a captured Change.")
            req.status = target
            req.updated_at = now_utc()
            self._audit(session, project.id, f"requirement_{target}", "requirement", req.id)
            self._schedule_render(
                session, project_id=project.id, requirement_ids=[req.id], brief=True
            )
            return req

    def add_requirement_plan(self, requirement_id: str, plan: str, reason: str | None = None) -> RequirementPlanVersion:
        if not plan.strip():
            raise HarnessError("Plan cannot be empty.")
        with session_scope(self.root) as session:
            project = self._project(session)
            req = session.get(Requirement, requirement_id)
            if req is None:
                raise NotFoundError(f"Requirement not found: {requirement_id}")
            max_version = session.scalar(
                select(func.max(RequirementPlanVersion.version)).where(
                    RequirementPlanVersion.requirement_id == req.id
                )
            )
            version = (max_version or 0) + 1
            plan_version = RequirementPlanVersion(
                id=new_id("plan"),
                requirement_id=req.id,
                version=version,
                plan_markdown=plan,
                reason_for_change=reason,
                created_at=now_utc(),
            )
            session.add(plan_version)
            req.updated_at = now_utc()
            self._audit(
                session,
                project.id,
                "requirement_plan_added",
                "requirement",
                req.id,
                {"version": version},
            )
            self._schedule_render(session, project_id=project.id, requirement_ids=[req.id])
            return plan_version

    def capture_change(
        self,
        base: str,
        head: str,
        task_id: str | None = None,
        requirement_ids: Iterable[str] = (),
        pull_request_reference: str | None = None,
    ) -> Change:
        requirement_ids = list(requirement_ids)
        with session_scope(self.root, write=False) as session:
            if task_id:
                self._active_task(session, task_id)
            for req_id in requirement_ids:
                if session.get(Requirement, req_id) is None:
                    raise NotFoundError(f"Requirement not found: {req_id}")
        base_proc = run_git(self.root, "rev-parse", f"{base}^{{commit}}")
        head_proc = run_git(self.root, "rev-parse", f"{head}^{{commit}}")
        if base_proc.returncode != 0:
            raise HarnessError(base_proc.stderr.strip() or f"Invalid base ref: {base}")
        if head_proc.returncode != 0:
            raise HarnessError(head_proc.stderr.strip() or f"Invalid head ref: {head}")
        base_commit = base_proc.stdout.strip()
        head_commit = head_proc.stdout.strip()
        if base_commit == head_commit:
            raise HarnessError("A Change must contain at least one committed code difference.")
        diff = run_git(self.root, "diff", "--binary", base_commit, head_commit)
        if diff.returncode != 0:
            raise HarnessError(diff.stderr.strip() or "Unable to create git diff.")
        if not diff.stdout:
            raise HarnessError("A Change cannot have an empty patch.")
        with session_scope(self.root) as session:
            project = self._project(session)
            if task_id:
                self._active_task(session, task_id)
            branch_proc = run_git(self.root, "branch", "--show-current")
            patch_hash = sha256_bytes(diff.stdout.encode("utf-8"))
            change = Change(
                id=new_id("change"),
                project_id=project.id,
                task_id=task_id,
                base_commit=base_commit,
                head_commit=head_commit,
                patch_hash=patch_hash,
                branch=branch_proc.stdout.strip() or None,
                pull_request_reference=pull_request_reference,
                status="captured",
                created_at=now_utc(),
            )
            session.add(change)
            session.flush()
            evidence = self._capture_text_in_session(
                session,
                project,
                diff.stdout,
                f"{change.id}.patch",
                "source_patch",
                task_id,
                {"base": base_commit, "head": head_commit, "change_id": change.id},
            )
            self._add_relation(session, "change", change.id, "produces", "evidence", evidence.id)
            if task_id:
                self._add_relation(session, "task", task_id, "produces", "change", change.id)
                task = session.get(Task, task_id)
                assert task is not None
                self._add_task_event(
                    session,
                    task,
                    "commit_created",
                    f"Captured code change {change.id}",
                    {"base": base_commit, "head": head_commit, "patch_hash": patch_hash},
                    evidence.id,
                )
            for req_id in requirement_ids:
                req = session.get(Requirement, req_id)
                if req is None:
                    raise NotFoundError(f"Requirement not found: {req_id}")
                self._add_relation(session, "change", change.id, "implements", "requirement", req.id)
            self._audit(session, project.id, "change_captured", "change", change.id)
            self._schedule_render(
                session,
                project_id=project.id,
                task_ids=[task_id] if task_id else [],
                requirement_ids=list(requirement_ids),
            )
            return change

    def capture_build(
        self,
        artifact_path: Path,
        status: str = "succeeded",
        change_id: str | None = None,
        container_digest: str | None = None,
    ) -> Build:
        if status not in {"queued", "running", "succeeded", "failed", "error", "cancelled"}:
            raise HarnessError(f"Invalid build status: {status}")
        with session_scope(self.root) as session:
            project = self._project(session)
            change = None
            if change_id:
                change = session.get(Change, change_id)
                if change is None:
                    raise NotFoundError(f"Change not found: {change_id}")
                if change.task_id:
                    self._active_task(session, change.task_id)
            source = artifact_path if artifact_path.is_absolute() else self.root / artifact_path
            evidence = self._capture_file_in_session(
                session,
                project,
                source,
                "build_artifact",
                change.task_id if change else None,
                {"change_id": change_id},
            )
            build = Build(
                id=new_id("build"),
                project_id=project.id,
                change_id=change_id,
                commit_sha=change.head_commit if change else git_snapshot(self.root).get("commit"),
                status=status,
                artifact_uri=evidence.storage_uri,
                artifact_hash=evidence.sha256,
                container_digest=container_digest,
                dependency_lock_hash=environment_snapshot(self.root).get("dependency_lock_hash"),
                created_at=now_utc(),
            )
            session.add(build)
            session.flush()
            self._add_relation(session, "build", build.id, "produces", "evidence", evidence.id)
            task_ids: list[str] = []
            if change:
                self._add_relation(session, "change", change.id, "produces", "build", build.id)
                if change.task_id:
                    task = self._active_task(session, change.task_id)
                    self._add_task_event(
                        session,
                        task,
                        "build_created",
                        f"Captured build {build.id} with status {status}",
                        {"build_id": build.id, "status": status, "change_id": change.id},
                        evidence.id,
                    )
                    task_ids.append(task.id)
            self._audit(session, project.id, "build_captured", "build", build.id)
            self._schedule_render(
                session, project_id=project.id, task_ids=task_ids, brief=True
            )
            return build

    def define_test(
        self,
        name: str,
        test_type: str,
        command: list[str],
        covers_requirements: Iterable[str] = (),
        pass_criteria: dict[str, Any] | None = None,
        environment_requirements: dict[str, Any] | None = None,
        data_requirements: dict[str, Any] | None = None,
    ) -> TestSpec:
        if not name.strip():
            raise HarnessError("Test name cannot be empty.")
        if test_type not in TEST_TYPES:
            raise HarnessError(f"Unsupported test type: {test_type}")
        if not command:
            raise HarnessError("Test command cannot be empty.")
        validated_criteria = self._validated_pass_criteria(pass_criteria)
        with session_scope(self.root) as session:
            project = self._project(session)
            req_ids = list(covers_requirements)
            for req_id in req_ids:
                if session.get(Requirement, req_id) is None:
                    raise NotFoundError(f"Requirement not found: {req_id}")
            spec = TestSpec(
                id=new_id("test_spec"),
                project_id=project.id,
                name=name,
                test_type=test_type,
                version=1,
                covers_requirements_json=json_dumps(req_ids),
                command_json=json_dumps(command),
                environment_requirements_json=json_dumps(environment_requirements or {}),
                data_requirements_json=json_dumps(data_requirements or {}),
                pass_criteria_json=json_dumps(validated_criteria),
                created_at=now_utc(),
            )
            session.add(spec)
            session.flush()
            for req_id in req_ids:
                self._add_relation(session, "test_spec", spec.id, "covers", "requirement", req_id)
            self._audit(session, project.id, "test_defined", "test_spec", spec.id)
            self._schedule_render(session, project_id=project.id, all_views=True)
            return spec

    def _parse_junit(self, path: Path) -> dict[str, int]:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise HarnessError("JUnit report exceeds the 20 MiB safety limit.")
        prefix = path.read_bytes()[:4096].lower()
        if b"<!doctype" in prefix or b"<!entity" in prefix:
            raise HarnessError("JUnit reports containing DTD or ENTITY declarations are rejected.")
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            raise HarnessError(f"Invalid JUnit report: {exc}") from exc
        if root.tag not in {"testsuite", "testsuites"}:
            raise HarnessError(f"Unsupported JUnit root element: {root.tag}")
        all_suites = list(root.iter("testsuite"))
        suites = [
            suite
            for suite in all_suites
            if not any(child.tag == "testsuite" for child in list(suite))
        ]
        if not suites and root.tag == "testsuite":
            suites = [root]
        total = failures = errors = skipped = 0
        try:
            for suite in suites:
                suite_total = int(suite.attrib.get("tests", 0))
                suite_failures = int(suite.attrib.get("failures", 0))
                suite_errors = int(suite.attrib.get("errors", 0))
                suite_skipped = int(suite.attrib.get("skipped", suite.attrib.get("disabled", 0)))
                values = (suite_total, suite_failures, suite_errors, suite_skipped)
                if any(value < 0 for value in values):
                    raise HarnessError("JUnit counts cannot be negative.")
                if suite_failures + suite_errors + suite_skipped > suite_total:
                    raise HarnessError("JUnit failure/error/skipped counts exceed total tests.")
                total += suite_total
                failures += suite_failures
                errors += suite_errors
                skipped += suite_skipped
        except ValueError as exc:
            raise HarnessError("JUnit count attributes must be integers.") from exc
        return {
            "total": total,
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "passed": total - failures - errors - skipped,
        }

    def _evaluate_test_result(
        self,
        criteria: dict[str, Any],
        exit_code: int,
        counts: dict[str, int],
        error_kind: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if error_kind or counts.get("errors", 0) > 0:
            return "error", [{"criterion": "execution", "passed": False, "actual": error_kind or counts.get("errors", 0)}]
        checks: list[dict[str, Any]] = []

        def add(name: str, passed: bool, actual: Any, expected: Any) -> None:
            checks.append({"criterion": name, "passed": passed, "actual": actual, "expected": expected})

        expected_exit = int(criteria.get("exit_code", 0))
        add("exit_code", exit_code == expected_exit, exit_code, expected_exit)
        if "max_failures" in criteria:
            limit = int(criteria["max_failures"])
            add("max_failures", counts.get("failures", 0) <= limit, counts.get("failures", 0), limit)
        else:
            add("failures", counts.get("failures", 0) == 0, counts.get("failures", 0), 0)
        if "min_passed" in criteria:
            limit = int(criteria["min_passed"])
            add("min_passed", counts.get("passed", 0) >= limit, counts.get("passed", 0), limit)
        if "min_total" in criteria:
            limit = int(criteria["min_total"])
            add("min_total", counts.get("total", 0) >= limit, counts.get("total", 0), limit)
        if "max_skipped" in criteria:
            limit = int(criteria["max_skipped"])
            add("max_skipped", counts.get("skipped", 0) <= limit, counts.get("skipped", 0), limit)
        return ("passed" if all(check["passed"] for check in checks) else "failed", checks)

    def run_test(
        self,
        test_spec_id: str,
        task_id: str | None = None,
        build_id: str | None = None,
        timeout: float | None = None,
        junit_path: Path | None = None,
    ) -> TestRun:
        with session_scope(self.root) as session:
            project = self._project(session)
            spec = session.get(TestSpec, test_spec_id)
            if spec is None:
                raise NotFoundError(f"Test specification not found: {test_spec_id}")
            if task_id:
                self._active_task(session, task_id)
            build = session.get(Build, build_id) if build_id else None
            if build_id and build is None:
                raise NotFoundError(f"Build not found: {build_id}")
            snapshot = self.create_snapshot(session, project, task_id=task_id)
            started = now_utc()
            command = json_loads(spec.command_json, [])
            error_kind = None
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.root,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                exit_code = completed.returncode
                stdout, stderr = completed.stdout, completed.stderr
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout, stderr = exc.stdout or "", (exc.stderr or "") + "\nTest timed out."
                error_kind = "timeout"
            except OSError as exc:
                exit_code = 127
                stdout, stderr = "", str(exc)
                error_kind = "execution_error"

            counts = {"total": 1, "passed": int(exit_code == 0), "failures": int(exit_code != 0), "errors": 0, "skipped": 0}
            junit_source = None
            if junit_path:
                junit_source = junit_path if junit_path.is_absolute() else self.root / junit_path
                if not junit_source.exists():
                    raise HarnessError(f"JUnit report not found: {junit_source}")
                counts = self._parse_junit(junit_source)
            criteria = self._validated_pass_criteria(json_loads(spec.pass_criteria_json, {}))
            status, evaluations = self._evaluate_test_result(criteria, exit_code, counts, error_kind)
            finished = now_utc()
            report = {
                "test_spec_id": spec.id,
                "command": command,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "counts": counts,
                "snapshot_id": snapshot.id,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "error_kind": error_kind,
                "pass_criteria": criteria,
                "criteria_evaluation": evaluations,
            }
            evidence = self._capture_text_in_session(
                session,
                project,
                json_dumps(report),
                "test-report.json",
                "test_report",
                task_id,
                {"test_spec_id": spec.id, "snapshot_id": snapshot.id},
            )
            if junit_source and junit_source.exists():
                junit_evidence = self._capture_file_in_session(
                    session,
                    project,
                    junit_source,
                    "test_report",
                    task_id,
                    {"format": "junit", "test_spec_id": spec.id},
                )
                self._add_relation(session, "evidence", evidence.id, "produces", "evidence", junit_evidence.id)
            test_run = TestRun(
                id=new_id("test_run"),
                test_spec_id=spec.id,
                task_id=task_id,
                build_id=build_id,
                snapshot_id=snapshot.id,
                commit_sha=build.commit_sha if build else snapshot.git_commit,
                status=status,
                started_at=started,
                finished_at=finished,
                total_count=counts["total"],
                passed_count=counts["passed"],
                failed_count=counts["failures"] + counts["errors"],
                skipped_count=counts["skipped"],
                result_summary=f"exit={exit_code}; {counts['passed']}/{counts['total']} passed",
                evidence_id=evidence.id,
            )
            session.add(test_run)
            session.flush()
            self._add_relation(session, "test_run", test_run.id, "produces", "evidence", evidence.id)
            if build:
                self._add_relation(session, "test_run", test_run.id, "evaluates", "build", build.id)
            if task_id:
                task = session.get(Task, task_id)
                assert task is not None
                event = self._add_task_event(
                    session,
                    task,
                    "test_completed",
                    f"Test {spec.id} completed with status {status}",
                    {"test_run_id": test_run.id, "status": status},
                    evidence.id,
                )
                evidence.task_event_id = event.id
            self._audit(session, project.id, "test_completed", "test_run", test_run.id)
            self._schedule_render(
                session, project_id=project.id, task_ids=[task_id] if task_id else [], brief=True
            )
            return test_run

    def import_junit(
        self,
        test_spec_id: str,
        junit_path: Path,
        task_id: str | None = None,
        build_id: str | None = None,
    ) -> TestRun:
        source = junit_path if junit_path.is_absolute() else self.root / junit_path
        if not source.exists():
            raise HarnessError(f"JUnit report not found: {source}")
        with session_scope(self.root) as session:
            project = self._project(session)
            spec = session.get(TestSpec, test_spec_id)
            if spec is None:
                raise NotFoundError(f"Test specification not found: {test_spec_id}")
            if task_id:
                self._active_task(session, task_id)
            build = session.get(Build, build_id) if build_id else None
            if build_id and build is None:
                raise NotFoundError(f"Build not found: {build_id}")
            snapshot = self.create_snapshot(session, project, task_id=task_id)
            counts = self._parse_junit(source)
            criteria = self._validated_pass_criteria(json_loads(spec.pass_criteria_json, {}))
            status, evaluations = self._evaluate_test_result(criteria, 0, counts, None)
            now = now_utc()
            junit_evidence = self._capture_file_in_session(
                session, project, source, "test_report", task_id,
                {"format": "junit", "test_spec_id": spec.id, "snapshot_id": snapshot.id},
            )
            report = {
                "test_spec_id": spec.id,
                "imported": True,
                "source": str(source),
                "counts": counts,
                "snapshot_id": snapshot.id,
                "pass_criteria": criteria,
                "criteria_evaluation": evaluations,
            }
            summary_evidence = self._capture_text_in_session(
                session, project, json_dumps(report), "import-summary.json", "test_report", task_id,
                {"test_spec_id": spec.id, "snapshot_id": snapshot.id},
            )
            self._add_relation(session, "evidence", summary_evidence.id, "produces", "evidence", junit_evidence.id)
            test_run = TestRun(
                id=new_id("test_run"), test_spec_id=spec.id, task_id=task_id, build_id=build_id,
                snapshot_id=snapshot.id, commit_sha=build.commit_sha if build else snapshot.git_commit,
                status=status, started_at=now, finished_at=now, total_count=counts["total"],
                passed_count=counts["passed"], failed_count=counts["failures"] + counts["errors"],
                skipped_count=counts["skipped"],
                result_summary=f"imported JUnit; {counts['passed']}/{counts['total']} passed",
                evidence_id=summary_evidence.id,
            )
            session.add(test_run)
            session.flush()
            self._add_relation(session, "test_run", test_run.id, "produces", "evidence", summary_evidence.id)
            if build:
                self._add_relation(session, "test_run", test_run.id, "evaluates", "build", build.id)
            if task_id:
                task = session.get(Task, task_id)
                assert task is not None
                event = self._add_task_event(
                    session, task, "test_completed", f"Imported test {spec.id} with status {status}",
                    {"test_run_id": test_run.id, "status": status}, summary_evidence.id,
                )
                summary_evidence.task_event_id = event.id
            self._audit(session, project.id, "test_imported", "test_run", test_run.id)
            self._schedule_render(
                session, project_id=project.id, task_ids=[task_id] if task_id else [], brief=True
            )
            return test_run

    def verify_requirement(self, requirement_id: str, test_run_id: str) -> Requirement:
        with session_scope(self.root) as session:
            project = self._project(session)
            req = session.get(Requirement, requirement_id)
            if req is None:
                raise NotFoundError(f"Requirement not found: {requirement_id}")
            if req.status != "implemented":
                raise StateTransitionError("Requirement must be implemented before verification.")
            test_run = session.get(TestRun, test_run_id)
            if test_run is None:
                raise NotFoundError(f"Test run not found: {test_run_id}")
            if test_run.status != "passed":
                raise HarnessError("Only a passed test run can verify a requirement.")
            if test_run.evidence_id is None:
                raise HarnessError("The test run has no formal report evidence.")
            test_evidence = session.get(Evidence, test_run.evidence_id)
            if test_evidence is None:
                raise HarnessError("The test run report evidence is missing.")
            self._assert_evidence_integrity(test_evidence)
            spec = session.get(TestSpec, test_run.test_spec_id)
            assert spec is not None
            covered = json_loads(spec.covers_requirements_json, [])
            if requirement_id not in covered:
                raise HarnessError("The test specification does not cover this requirement.")
            if test_run.build_id is None:
                raise HarnessError("Requirement verification requires a test run bound to a Build.")
            build = session.get(Build, test_run.build_id)
            if build is None:
                raise HarnessError("The test run references a missing Build.")
            if build.status != "succeeded":
                raise HarnessError("Only a succeeded Build can verify a requirement.")
            if build.change_id is None:
                raise HarnessError("The verified Build is not linked to a Change.")
            change = session.get(Change, build.change_id)
            if change is None:
                raise HarnessError("The verified Build references a missing Change.")
            if test_run.commit_sha != build.commit_sha:
                raise HarnessError("The test run commit does not match its Build commit.")
            implementation = session.scalar(
                select(Relation).where(
                    Relation.source_type == "change",
                    Relation.source_id == change.id,
                    Relation.relation_type == "implements",
                    Relation.target_type == "requirement",
                    Relation.target_id == req.id,
                )
            )
            if implementation is None:
                raise HarnessError("The tested Build does not implement this requirement.")
            build_evidence_relation = session.scalar(
                select(Relation).where(
                    Relation.source_type == "build",
                    Relation.source_id == build.id,
                    Relation.relation_type == "produces",
                    Relation.target_type == "evidence",
                )
            )
            if build_evidence_relation is None:
                raise HarnessError("The tested Build has no artifact evidence.")
            build_evidence = session.get(Evidence, build_evidence_relation.target_id)
            if build_evidence is None:
                raise HarnessError("The tested Build artifact evidence is missing.")
            self._assert_evidence_integrity(build_evidence)
            req.status = "verified"
            req.updated_at = now_utc()
            self._add_relation(session, "test_run", test_run.id, "verifies", "requirement", req.id)
            self._audit(
                session,
                project.id,
                "requirement_verified",
                "requirement",
                req.id,
                {"test_run_id": test_run.id},
            )
            self._schedule_render(
                session, project_id=project.id, requirement_ids=[req.id], brief=True
            )
            return req

    def doctor(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            conclusions = session.scalars(
                select(Conclusion).where(Conclusion.project_id == project.id)
            ).all()
            for conclusion in conclusions:
                if conclusion.status not in CONCLUSION_STATUSES:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "INVALID_CONCLUSION_STATUS",
                            "entity": conclusion.id,
                            "message": f"Invalid conclusion status: {conclusion.status}",
                        }
                    )
                if conclusion.status in {"supported", "refuted"}:
                    relation_type = "supports" if conclusion.status == "supported" else "refutes"
                    relation = session.scalar(
                        select(Relation).where(
                            Relation.source_type == "conclusion",
                            Relation.source_id == conclusion.id,
                            Relation.relation_type == relation_type,
                            Relation.target_type == "evidence",
                        )
                    )
                    if relation is None:
                        findings.append(
                            {
                                "severity": "error",
                                "code": "CONCLUSION_WITHOUT_EVIDENCE",
                                "entity": conclusion.id,
                                "message": f"{conclusion.status} conclusion has no {relation_type} evidence.",
                            }
                        )
                if conclusion.status == "superseded" and not conclusion.superseded_by:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "SUPERSEDED_WITHOUT_REPLACEMENT",
                            "entity": conclusion.id,
                            "message": "Superseded conclusion has no replacement.",
                        }
                    )

            tasks = session.scalars(select(Task).where(Task.project_id == project.id)).all()
            for task in tasks:
                if task.status not in {"in_progress", "succeeded", "failed"}:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "INVALID_TASK_STATUS",
                            "entity": task.id,
                            "message": f"Invalid task status: {task.status}",
                        }
                    )
                if task.status != "in_progress" and not task.result_summary:
                    findings.append(
                        {
                            "severity": "warning",
                            "code": "COMPLETED_TASK_WITHOUT_SUMMARY",
                            "entity": task.id,
                            "message": "Completed task has no result summary.",
                        }
                    )
                if task.status == "failed" and not task.failure_reason:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "FAILED_TASK_WITHOUT_REASON",
                            "entity": task.id,
                            "message": "Failed task has no failure reason.",
                        }
                    )
                sequences = list(
                    session.scalars(
                        select(TaskEvent.sequence_number)
                        .where(TaskEvent.task_id == task.id)
                        .order_by(TaskEvent.sequence_number)
                    ).all()
                )
                if sequences != list(range(1, len(sequences) + 1)):
                    findings.append(
                        {
                            "severity": "error",
                            "code": "TASK_EVENT_SEQUENCE_GAP",
                            "entity": task.id,
                            "message": "Task event sequence is not contiguous from 1.",
                        }
                    )

            requirements = session.scalars(
                select(Requirement).where(Requirement.project_id == project.id)
            ).all()
            for req in requirements:
                if req.status not in REQUIREMENT_STATUSES:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "INVALID_REQUIREMENT_STATUS",
                            "entity": req.id,
                            "message": f"Invalid requirement status: {req.status}",
                        }
                    )
                if req.status == "verified":
                    relation = session.scalar(
                        select(Relation).where(
                            Relation.relation_type == "verifies",
                            Relation.target_type == "requirement",
                            Relation.target_id == req.id,
                            Relation.source_type == "test_run",
                        )
                    )
                    if relation is None:
                        findings.append(
                            {
                                "severity": "error",
                                "code": "VERIFIED_WITHOUT_TEST",
                                "entity": req.id,
                                "message": "Verified requirement has no test run relation.",
                            }
                        )

            evidence_items = session.scalars(
                select(Evidence).where(Evidence.project_id == project.id)
            ).all()
            for evidence in evidence_items:
                path = Path(evidence.storage_uri)
                if not path.is_absolute():
                    path = self.root / path
                if not path.exists():
                    findings.append(
                        {
                            "severity": "error",
                            "code": "EVIDENCE_MISSING",
                            "entity": evidence.id,
                            "message": f"Evidence file is missing: {evidence.storage_uri}",
                        }
                    )
                elif sha256_file(path) != evidence.sha256:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "EVIDENCE_HASH_MISMATCH",
                            "entity": evidence.id,
                            "message": "Evidence content hash no longer matches.",
                        }
                    )

            for spec in session.scalars(select(TestSpec).where(TestSpec.project_id == project.id)).all():
                if not json_loads(spec.pass_criteria_json, {}):
                    findings.append(
                        {
                            "severity": "error",
                            "code": "TEST_WITHOUT_PASS_CRITERIA",
                            "entity": spec.id,
                            "message": "Test specification has no pass criteria.",
                        }
                    )
            for relation in session.scalars(select(Relation)).all():
                if (relation.source_type, relation.relation_type, relation.target_type) not in RELATION_RULES:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "INVALID_RELATION_SHAPE",
                            "entity": relation.id,
                            "message": (
                                f"Invalid relation: {relation.source_type} "
                                f"{relation.relation_type} {relation.target_type}"
                            ),
                        }
                    )
                if not _entity_exists(session, relation.source_type, relation.source_id) or not _entity_exists(
                    session, relation.target_type, relation.target_id
                ):
                    findings.append(
                        {
                            "severity": "error",
                            "code": "DANGLING_RELATION",
                            "entity": relation.id,
                            "message": "Relation references a missing source or target.",
                        }
                    )

            for change in session.scalars(select(Change).where(Change.project_id == project.id)).all():
                if not change.patch_hash or change.base_commit == change.head_commit:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "EMPTY_CHANGE",
                            "entity": change.id,
                            "message": "Captured Change has no meaningful committed patch.",
                        }
                    )

            for test_run in session.scalars(select(TestRun)).all():
                if test_run.status == "passed" and test_run.evidence_id is None:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "PASSED_TEST_WITHOUT_EVIDENCE",
                            "entity": test_run.id,
                            "message": "Passed Test Run has no report evidence.",
                        }
                    )

            integrity_rows = list(session.execute(text("PRAGMA integrity_check")).scalars())
            if integrity_rows != ["ok"]:
                findings.append(
                    {
                        "severity": "error",
                        "code": "DATABASE_INTEGRITY_FAILURE",
                        "entity": project.id,
                        "message": "; ".join(str(item) for item in integrity_rows),
                    }
                )

            stale_path = self.root / HARNESS_DIR / "render-stale.json"
            if stale_path.exists():
                findings.append(
                    {
                        "severity": "warning",
                        "code": "RENDER_STALE",
                        "entity": project.id,
                        "message": "Generated Markdown is stale; run harness render.",
                    }
                )

            search_state = session.get(SearchIndexState, project.id)
            if search_state is not None and search_state.status != "ready":
                findings.append(
                    {
                        "severity": "warning",
                        "code": "SEARCH_INDEX_STALE",
                        "entity": project.id,
                        "message": "Historical retrieval index is stale; run harness index rebuild.",
                    }
                )

            brief_path = self.root / "harness-docs" / "project-brief.md"
            if not brief_path.exists():
                findings.append(
                    {
                        "severity": "warning",
                        "code": "BRIEF_MISSING",
                        "entity": project.id,
                        "message": "Project brief is missing; run harness render.",
                    }
                )
        return findings

    def summary(self) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            counts = {}
            for name, model in {
                "tasks": Task,
                "conclusions": Conclusion,
                "requirements": Requirement,
                "evidence": Evidence,
                "test_specs": TestSpec,
                "test_runs": TestRun,
            }.items():
                counts[name] = session.scalar(select(func.count()).select_from(model)) or 0
            return {"project": project.name, **counts}

    def update_project(
        self,
        *,
        description: str | None = None,
        status: str | None = None,
        repository_uri: str | None = None,
        default_branch: str | None = None,
    ) -> Project:
        if status is not None and status not in {"active", "paused", "completed", "archived"}:
            raise HarnessError(f"Invalid project status: {status}")
        with session_scope(self.root) as session:
            project = self._project(session)
            if description is not None:
                project.description = description
            if status is not None:
                project.status = status
            if repository_uri is not None:
                project.repository_uri = repository_uri
            if default_branch is not None:
                project.default_branch = default_branch
            project.updated_at = now_utc()
            self._audit(session, project.id, "project_updated", "project", project.id)
            self._schedule_render(session, project_id=project.id, brief=True)
            return project

    def task_data(self, task_id: str) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            task = session.get(Task, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            events = session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.sequence_number)
            ).all()
            return {
                "id": task.id,
                "type": task.task_type,
                "goal": task.original_goal,
                "success_criteria": json_loads(task.success_criteria_json, []),
                "constraints": json_loads(task.constraints_json, []),
                "status": task.status,
                "result_type": task.result_type,
                "result_summary": task.result_summary,
                "failure_reason": task.failure_reason,
                "events": [
                    {
                        "id": e.id,
                        "sequence": e.sequence_number,
                        "type": e.event_type,
                        "summary": e.summary,
                        "payload": json_loads(e.payload_json, {}),
                        "evidence_id": e.evidence_id,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in events
                ],
            }

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            stmt = select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())
            if status:
                stmt = stmt.where(Task.status == status)
            tasks = session.scalars(stmt).all()
            return [
                {
                    "id": task.id,
                    "type": task.task_type,
                    "goal": task.original_goal,
                    "status": task.status,
                    "result_type": task.result_type,
                }
                for task in tasks
            ]

    def conclusion_data(self, conclusion_id: str) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            conclusion = session.get(Conclusion, conclusion_id)
            if conclusion is None:
                raise NotFoundError(f"Conclusion not found: {conclusion_id}")
            relations = session.scalars(
                select(Relation).where(
                    Relation.source_type == "conclusion", Relation.source_id == conclusion.id
                )
            ).all()
            return {
                "id": conclusion.id,
                "claim": conclusion.claim,
                "status": conclusion.status,
                "scope": json_loads(conclusion.scope_json, {}),
                "falsification_criteria": conclusion.falsification_criteria,
                "confidence": conclusion.confidence,
                "superseded_by": conclusion.superseded_by,
                "relations": [
                    {
                        "type": relation.relation_type,
                        "target_type": relation.target_type,
                        "target_id": relation.target_id,
                        "metadata": json_loads(relation.metadata_json, {}),
                    }
                    for relation in relations
                ],
            }

    def list_conclusions(self, status: str | None = None) -> list[dict[str, Any]]:
        if status and status not in CONCLUSION_STATUSES:
            raise HarnessError(f"Invalid conclusion status: {status}")
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            stmt = (
                select(Conclusion)
                .where(Conclusion.project_id == project.id)
                .order_by(Conclusion.updated_at.desc())
            )
            if status:
                stmt = stmt.where(Conclusion.status == status)
            return [
                {"id": c.id, "claim": c.claim, "status": c.status, "confidence": c.confidence}
                for c in session.scalars(stmt).all()
            ]

    def requirement_data(self, requirement_id: str) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            req = session.get(Requirement, requirement_id)
            if req is None:
                raise NotFoundError(f"Requirement not found: {requirement_id}")
            plans = session.scalars(
                select(RequirementPlanVersion)
                .where(RequirementPlanVersion.requirement_id == req.id)
                .order_by(RequirementPlanVersion.version)
            ).all()
            return {
                "id": req.id,
                "description": req.original_description,
                "status": req.status,
                "priority": req.priority,
                "acceptance_criteria": json_loads(req.acceptance_criteria_json, []),
                "constraints": json_loads(req.constraints_json, []),
                "superseded_by": req.superseded_by,
                "plans": [
                    {
                        "version": plan.version,
                        "plan": plan.plan_markdown,
                        "reason": plan.reason_for_change,
                    }
                    for plan in plans
                ],
            }

    def list_requirements(self, status: str | None = None) -> list[dict[str, Any]]:
        if status and status not in REQUIREMENT_STATUSES:
            raise HarnessError(f"Invalid requirement status: {status}")
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            stmt = (
                select(Requirement)
                .where(Requirement.project_id == project.id)
                .order_by(Requirement.updated_at.desc())
            )
            if status:
                stmt = stmt.where(Requirement.status == status)
            return [
                {
                    "id": req.id,
                    "description": req.original_description,
                    "status": req.status,
                    "priority": req.priority,
                }
                for req in session.scalars(stmt).all()
            ]

    def test_run_data(self, test_run_id: str) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            run = session.get(TestRun, test_run_id)
            if run is None:
                raise NotFoundError(f"Test run not found: {test_run_id}")
            return {
                "id": run.id,
                "test_spec_id": run.test_spec_id,
                "status": run.status,
                "commit": run.commit_sha,
                "total": run.total_count,
                "passed": run.passed_count,
                "failed": run.failed_count,
                "skipped": run.skipped_count,
                "evidence_id": run.evidence_id,
                "summary": run.result_summary,
            }
