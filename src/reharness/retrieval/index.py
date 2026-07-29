from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..db import init_database, session_scope
from ..models import (
    Build,
    Change,
    Conclusion,
    Evidence,
    Project,
    Relation,
    Requirement,
    RequirementPlanVersion,
    SearchDocument,
    SearchIndexState,
    Task,
    TaskEvent,
    TestRun,
    TestSpec,
)
from ..utils import json_dumps, json_loads, now_utc, sha256_bytes, sha256_file
from .normalizer import index_text

HIGH_VALUE_EVENTS = {
    "plan_created",
    "plan_revised",
    "command_failed",
    "observation_recorded",
    "test_completed",
    "correction_added",
    "task_succeeded",
    "task_failed",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".log",
    ".patch",
    ".diff",
    ".py",
    ".toml",
}
MAX_INDEXED_EVIDENCE_BYTES = 128 * 1024


@dataclass(frozen=True)
class ProjectedDocument:
    id: str
    project_id: str
    entity_type: str
    entity_id: str
    chunk_type: str
    title: str
    body: str
    status: str | None
    authority_level: int
    integrity_status: str | None
    source_hash: str
    metadata: dict[str, Any]
    created_at: Any
    updated_at: Any


def mark_index_stale(session: Session, project_id: str) -> None:
    state = session.get(SearchIndexState, project_id)
    if state is not None:
        state.status = "stale"
        state.last_error = None


def _document(
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    chunk_type: str,
    title: str,
    body: str,
    status: str | None,
    authority_level: int,
    integrity_status: str | None,
    metadata: dict[str, Any],
    created_at: Any,
    updated_at: Any,
) -> ProjectedDocument:
    source = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "chunk_type": chunk_type,
        "title": title,
        "body": body,
        "status": status,
        "authority_level": authority_level,
        "integrity_status": integrity_status,
        "metadata": metadata,
        "updated_at": str(updated_at),
    }
    return ProjectedDocument(
        id=f"{entity_type}:{entity_id}:{chunk_type}",
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        chunk_type=chunk_type,
        title=title,
        body=index_text(title, body),
        status=status,
        authority_level=authority_level,
        integrity_status=integrity_status,
        source_hash=sha256_bytes(json_dumps(source).encode("utf-8")),
        metadata=metadata,
        created_at=created_at,
        updated_at=updated_at,
    )


def _relation_rows(session: Session, entity_type: str, entity_id: str) -> list[Relation]:
    return list(
        session.scalars(
            select(Relation).where(
                ((Relation.source_type == entity_type) & (Relation.source_id == entity_id))
                | ((Relation.target_type == entity_type) & (Relation.target_id == entity_id))
            )
        ).all()
    )


def _relation_text(rows: Iterable[Relation]) -> str:
    return "\n".join(
        f"{row.source_type}:{row.source_id} {row.relation_type} "
        f"{row.target_type}:{row.target_id}"
        for row in rows
    )


def _evidence_path(root: Path, evidence: Evidence) -> Path:
    path = Path(evidence.storage_uri)
    return path if path.is_absolute() else root / path


def _evidence_integrity(root: Path, evidence: Evidence) -> str:
    path = _evidence_path(root, evidence)
    if not path.exists() or not path.is_file():
        return "missing"
    try:
        return "valid" if sha256_file(path) == evidence.sha256 else "corrupted"
    except OSError:
        return "unavailable"


def _evidence_text(root: Path, evidence: Evidence) -> str:
    path = _evidence_path(root, evidence)
    if not path.exists() or not path.is_file() or path.stat().st_size > MAX_INDEXED_EVIDENCE_BYTES:
        return ""
    is_text = evidence.mime_type.startswith("text/") or path.suffix.lower() in TEXT_EXTENSIONS
    if not is_text:
        return ""
    try:
        return path.read_text(encoding="utf-8")[:MAX_INDEXED_EVIDENCE_BYTES]
    except (OSError, UnicodeDecodeError):
        return ""


def project_documents(session: Session, root: Path, project: Project) -> list[ProjectedDocument]:
    documents: list[ProjectedDocument] = []
    documents.append(
        _document(
            project_id=project.id,
            entity_type="project",
            entity_id=project.id,
            chunk_type="summary",
            title=project.name,
            body=f"{project.description}\nstatus: {project.status}\nrepository: {project.repository_uri or ''}",
            status=project.status,
            authority_level=100,
            integrity_status=None,
            metadata={"default_branch": project.default_branch},
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
    )

    conclusions = session.scalars(
        select(Conclusion).where(Conclusion.project_id == project.id)
    ).all()
    for conclusion in conclusions:
        relations = _relation_rows(session, "conclusion", conclusion.id)
        authority = 100 if conclusion.status in {"supported", "refuted"} else 60
        if conclusion.status == "superseded":
            authority = 35
        body = "\n".join(
            part
            for part in [
                conclusion.claim,
                conclusion.details_markdown,
                f"scope: {json_dumps(json_loads(conclusion.scope_json, {}))}",
                f"falsification: {conclusion.falsification_criteria}",
                f"confidence: {conclusion.confidence or ''}",
                _relation_text(relations),
            ]
            if part.strip()
        )
        documents.append(
            _document(
                project_id=project.id,
                entity_type="conclusion",
                entity_id=conclusion.id,
                chunk_type="summary",
                title=conclusion.claim,
                body=body,
                status=conclusion.status,
                authority_level=authority,
                integrity_status=None,
                metadata={
                    "superseded_by": conclusion.superseded_by,
                    "relations": [row.id for row in relations],
                },
                created_at=conclusion.created_at,
                updated_at=conclusion.updated_at,
            )
        )

    tasks = session.scalars(select(Task).where(Task.project_id == project.id)).all()
    for task in tasks:
        events = session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .order_by(TaskEvent.sequence_number)
        ).all()
        high_value = [event for event in events if event.event_type in HIGH_VALUE_EVENTS]
        event_text = "\n".join(
            f"#{event.sequence_number} {event.event_type}: {event.summary}"
            for event in high_value
        )
        body = "\n".join(
            part
            for part in [
                task.original_goal,
                f"success criteria: {json_dumps(json_loads(task.success_criteria_json, []))}",
                f"constraints: {json_dumps(json_loads(task.constraints_json, []))}",
                f"result type: {task.result_type or ''}",
                f"result: {task.result_summary or ''}",
                f"failure: {task.failure_reason or ''}",
                event_text,
            ]
            if part.strip()
        )
        authority = 75 if task.status != "in_progress" else 50
        documents.append(
            _document(
                project_id=project.id,
                entity_type="task",
                entity_id=task.id,
                chunk_type="summary",
                title=task.original_goal,
                body=body,
                status=task.status,
                authority_level=authority,
                integrity_status=None,
                metadata={"task_type": task.task_type, "result_type": task.result_type},
                created_at=task.created_at,
                updated_at=task.completed_at or task.created_at,
            )
        )

    requirements = session.scalars(
        select(Requirement).where(Requirement.project_id == project.id)
    ).all()
    for requirement in requirements:
        plans = session.scalars(
            select(RequirementPlanVersion)
            .where(RequirementPlanVersion.requirement_id == requirement.id)
            .order_by(RequirementPlanVersion.version)
        ).all()
        relations = _relation_rows(session, "requirement", requirement.id)
        body = "\n".join(
            [
                requirement.original_description,
                f"acceptance criteria: {json_dumps(json_loads(requirement.acceptance_criteria_json, []))}",
                f"constraints: {json_dumps(json_loads(requirement.constraints_json, []))}",
                *[
                    f"plan v{plan.version}: {plan.plan_markdown}\nreason: {plan.reason_for_change or ''}"
                    for plan in plans
                ],
                _relation_text(relations),
            ]
        )
        authority = 95 if requirement.status == "verified" else 65
        if requirement.status in {"rejected", "superseded"}:
            authority = 35
        documents.append(
            _document(
                project_id=project.id,
                entity_type="requirement",
                entity_id=requirement.id,
                chunk_type="summary",
                title=requirement.original_description,
                body=body,
                status=requirement.status,
                authority_level=authority,
                integrity_status=None,
                metadata={"priority": requirement.priority, "superseded_by": requirement.superseded_by},
                created_at=requirement.created_at,
                updated_at=requirement.updated_at,
            )
        )

    evidence_rows = session.scalars(
        select(Evidence).where(Evidence.project_id == project.id)
    ).all()
    for evidence in evidence_rows:
        integrity = _evidence_integrity(root, evidence)
        relations = _relation_rows(session, "evidence", evidence.id)
        content = _evidence_text(root, evidence)
        body = "\n".join(
            part
            for part in [
                f"type: {evidence.evidence_type}",
                f"storage: {evidence.storage_uri}",
                f"sha256: {evidence.sha256}",
                f"mime: {evidence.mime_type}",
                f"metadata: {json_dumps(json_loads(evidence.metadata_json, {}))}",
                _relation_text(relations),
                content,
            ]
            if part.strip()
        )
        documents.append(
            _document(
                project_id=project.id,
                entity_type="evidence",
                entity_id=evidence.id,
                chunk_type="content" if content else "metadata",
                title=f"{evidence.evidence_type} {Path(evidence.storage_uri).name}",
                body=body,
                status="available" if evidence.available else "unavailable",
                authority_level=85 if integrity == "valid" else 20,
                integrity_status=integrity,
                metadata={
                    "evidence_type": evidence.evidence_type,
                    "task_id": evidence.task_id,
                    "task_event_id": evidence.task_event_id,
                    "storage_uri": evidence.storage_uri,
                    "sha256": evidence.sha256,
                    "size": evidence.size,
                },
                created_at=evidence.created_at,
                updated_at=evidence.created_at,
            )
        )

    test_specs = session.scalars(
        select(TestSpec).where(TestSpec.project_id == project.id)
    ).all()
    specs = {spec.id: spec for spec in test_specs}
    for spec in test_specs:
        documents.append(
            _document(
                project_id=project.id,
                entity_type="test_spec",
                entity_id=spec.id,
                chunk_type="summary",
                title=spec.name,
                body="\n".join(
                    [
                        f"type: {spec.test_type}",
                        f"command: {json_dumps(json_loads(spec.command_json, []))}",
                        f"covers: {json_dumps(json_loads(spec.covers_requirements_json, []))}",
                        f"pass criteria: {json_dumps(json_loads(spec.pass_criteria_json, {}))}",
                    ]
                ),
                status="defined",
                authority_level=55,
                integrity_status=None,
                metadata={"test_type": spec.test_type},
                created_at=spec.created_at,
                updated_at=spec.created_at,
            )
        )

    test_runs = session.scalars(select(TestRun)).all()
    for run in test_runs:
        spec = specs.get(run.test_spec_id)
        title = f"{spec.name if spec else run.test_spec_id} — {run.status}"
        documents.append(
            _document(
                project_id=project.id,
                entity_type="test_run",
                entity_id=run.id,
                chunk_type="summary",
                title=title,
                body="\n".join(
                    [
                        run.result_summary,
                        f"status: {run.status}",
                        f"commit: {run.commit_sha or ''}",
                        f"counts: total={run.total_count} passed={run.passed_count} "
                        f"failed={run.failed_count} skipped={run.skipped_count}",
                        f"build: {run.build_id or ''}",
                        f"evidence: {run.evidence_id or ''}",
                    ]
                ),
                status=run.status,
                authority_level=80 if run.status == "passed" else 60,
                integrity_status=None,
                metadata={
                    "test_spec_id": run.test_spec_id,
                    "task_id": run.task_id,
                    "build_id": run.build_id,
                    "evidence_id": run.evidence_id,
                },
                created_at=run.started_at,
                updated_at=run.finished_at or run.started_at,
            )
        )

    changes = session.scalars(select(Change).where(Change.project_id == project.id)).all()
    for change in changes:
        relations = _relation_rows(session, "change", change.id)
        documents.append(
            _document(
                project_id=project.id,
                entity_type="change",
                entity_id=change.id,
                chunk_type="summary",
                title=f"Change {change.id}",
                body="\n".join(
                    [
                        f"base: {change.base_commit or ''}",
                        f"head: {change.head_commit or ''}",
                        f"branch: {change.branch or ''}",
                        f"patch hash: {change.patch_hash or ''}",
                        _relation_text(relations),
                    ]
                ),
                status=change.status,
                authority_level=70,
                integrity_status=None,
                metadata={"task_id": change.task_id},
                created_at=change.created_at,
                updated_at=change.created_at,
            )
        )

    builds = session.scalars(select(Build).where(Build.project_id == project.id)).all()
    for build in builds:
        relations = _relation_rows(session, "build", build.id)
        documents.append(
            _document(
                project_id=project.id,
                entity_type="build",
                entity_id=build.id,
                chunk_type="summary",
                title=f"Build {build.id} — {build.status}",
                body="\n".join(
                    [
                        f"change: {build.change_id or ''}",
                        f"commit: {build.commit_sha or ''}",
                        f"artifact: {build.artifact_uri or ''}",
                        f"artifact hash: {build.artifact_hash or ''}",
                        _relation_text(relations),
                    ]
                ),
                status=build.status,
                authority_level=75 if build.status == "succeeded" else 50,
                integrity_status=None,
                metadata={"change_id": build.change_id},
                created_at=build.created_at,
                updated_at=build.created_at,
            )
        )

    return documents


class SearchIndexer:
    def __init__(self, root: Path):
        self.root = root.resolve()
        # Additive schema initialization keeps retrieval usable for projects created
        # before the search projection tables existed.
        init_database(self.root)

    def status(self) -> dict[str, Any]:
        with session_scope(self.root, write=False) as session:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise RuntimeError("Project database is missing its project record.")
            state = session.get(SearchIndexState, project.id)
            document_count = session.scalar(
                select(func.count()).select_from(SearchDocument).where(SearchDocument.project_id == project.id)
            ) or 0
            fts_count = session.execute(
                text("SELECT count(*) FROM search_documents_fts WHERE project_id = :project_id"),
                {"project_id": project.id},
            ).scalar_one()
            return {
                "project_id": project.id,
                "status": state.status if state else "missing",
                "document_count": int(document_count),
                "fts_count": int(fts_count),
                "source_fingerprint": state.source_fingerprint if state else None,
                "last_indexed_at": state.last_indexed_at.isoformat() if state and state.last_indexed_at else None,
                "last_error": state.last_error if state else None,
            }

    def rebuild(self) -> dict[str, Any]:
        try:
            with session_scope(self.root) as session:
                project = session.scalar(select(Project).limit(1))
                if project is None:
                    raise RuntimeError("Project database is missing its project record.")
                state = session.get(SearchIndexState, project.id)
                if state is None:
                    state = SearchIndexState(project_id=project.id, status="rebuilding", document_count=0)
                    session.add(state)
                else:
                    state.status = "rebuilding"
                    state.last_error = None
                documents = project_documents(session, self.root, project)
                session.execute(delete(SearchDocument).where(SearchDocument.project_id == project.id))
                session.execute(
                    text("DELETE FROM search_documents_fts WHERE project_id = :project_id"),
                    {"project_id": project.id},
                )
                indexed_at = now_utc()
                for item in documents:
                    session.add(
                        SearchDocument(
                            id=item.id,
                            project_id=item.project_id,
                            entity_type=item.entity_type,
                            entity_id=item.entity_id,
                            chunk_type=item.chunk_type,
                            title=item.title,
                            body=item.body,
                            status=item.status,
                            authority_level=item.authority_level,
                            integrity_status=item.integrity_status,
                            source_hash=item.source_hash,
                            metadata_json=json_dumps(item.metadata),
                            created_at=item.created_at,
                            updated_at=item.updated_at,
                            indexed_at=indexed_at,
                        )
                    )
                    session.execute(
                        text(
                            """
                            INSERT INTO search_documents_fts
                                (document_id, project_id, entity_type, title, body)
                            VALUES (:document_id, :project_id, :entity_type, :title, :body)
                            """
                        ),
                        {
                            "document_id": item.id,
                            "project_id": item.project_id,
                            "entity_type": item.entity_type,
                            "title": item.title,
                            "body": item.body,
                        },
                    )
                fingerprint = sha256_bytes(
                    "\n".join(
                        f"{item.id}:{item.source_hash}"
                        for item in sorted(documents, key=lambda document: document.id)
                    ).encode()
                )
                state.status = "ready"
                state.document_count = len(documents)
                state.source_fingerprint = fingerprint
                state.last_indexed_at = indexed_at
                state.last_error = None
            return self.status()
        except Exception as exc:
            try:
                with session_scope(self.root) as session:
                    project = session.scalar(select(Project).limit(1))
                    if project is not None:
                        state = session.get(SearchIndexState, project.id)
                        if state is None:
                            state = SearchIndexState(
                                project_id=project.id,
                                status="failed",
                                document_count=0,
                            )
                            session.add(state)
                        else:
                            state.status = "failed"
                        state.last_error = str(exc)
            except Exception:
                pass
            raise RuntimeError(f"Search index rebuild failed: {exc}") from exc

    def ensure_ready(self) -> dict[str, Any]:
        current = self.status()
        if current["status"] != "ready" or current["document_count"] != current["fts_count"]:
            return self.rebuild()
        return current

    def verify(self) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        with session_scope(self.root, write=False) as session:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise RuntimeError("Project database is missing its project record.")
            expected = {document.id: document for document in project_documents(session, self.root, project)}
            actual = {
                document.id: document
                for document in session.scalars(
                    select(SearchDocument).where(SearchDocument.project_id == project.id)
                ).all()
            }
            for missing in sorted(set(expected) - set(actual)):
                findings.append({"code": "SEARCH_INDEX_MISSING_DOCUMENT", "document_id": missing})
            for orphan in sorted(set(actual) - set(expected)):
                findings.append({"code": "SEARCH_INDEX_ORPHAN_DOCUMENT", "document_id": orphan})
            for document_id in sorted(set(expected) & set(actual)):
                if expected[document_id].source_hash != actual[document_id].source_hash:
                    findings.append({"code": "SEARCH_INDEX_HASH_MISMATCH", "document_id": document_id})
            fts_ids = {
                row[0]
                for row in session.execute(
                    text("SELECT document_id FROM search_documents_fts WHERE project_id = :project_id"),
                    {"project_id": project.id},
                ).all()
            }
            if fts_ids != set(actual):
                findings.append(
                    {
                        "code": "SEARCH_FTS_DOCUMENT_MISMATCH",
                        "document_id": project.id,
                    }
                )
            state = session.get(SearchIndexState, project.id)
            if state is None or state.status != "ready":
                findings.append({"code": "SEARCH_INDEX_STALE", "document_id": project.id})
        return {"valid": not findings, "findings": findings, **self.status()}
