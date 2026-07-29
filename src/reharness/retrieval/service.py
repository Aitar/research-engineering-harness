from __future__ import annotations

import re
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    Artifact,
    Build,
    Change,
    Conclusion,
    Evidence,
    Project,
    Relation,
    Requirement,
    RequirementPlanVersion,
    SearchDocument,
    Snapshot,
    Task,
    TaskEvent,
    TestRun,
    TestSpec,
)
from ..utils import json_loads, sha256_file
from .grep import GrepBackend, GrepMatch
from .index import SearchIndexer, _evidence_integrity
from .models import SearchQuery, SearchResult
from .normalizer import extract_entity_ids, extract_hashes, fts_query, normalize_text

ENTITY_PREFIXES = {
    "PRJ": "project",
    "TASK": "task",
    "EVT": "task_event",
    "CON": "conclusion",
    "REQ": "requirement",
    "PLAN": "requirement_plan",
    "CHG": "change",
    "BUILD": "build",
    "TEST": "test_spec",
    "TRUN": "test_run",
    "EVD": "evidence",
    "SNP": "snapshot",
    "ART": "artifact",
}
MODEL_BY_TYPE = {
    "project": Project,
    "task": Task,
    "task_event": TaskEvent,
    "conclusion": Conclusion,
    "requirement": Requirement,
    "requirement_plan": RequirementPlanVersion,
    "change": Change,
    "build": Build,
    "test_spec": TestSpec,
    "test_run": TestRun,
    "evidence": Evidence,
    "snapshot": Snapshot,
    "artifact": Artifact,
}
DOC_PATH_PATTERNS = [
    (re.compile(r"^harness-docs/tasks/(TASK-[A-F0-9]+)\.md$", re.I), "task"),
    (re.compile(r"^harness-docs/conclusions/(CON-[A-F0-9]+)\.md$", re.I), "conclusion"),
    (re.compile(r"^harness-docs/requirements/(REQ-[A-F0-9]+)\.md$", re.I), "requirement"),
    (re.compile(r"^harness-artifacts/evidence/(EVD-[A-F0-9]+)/", re.I), "evidence"),
]


def _clean_body(body: str, limit: int = 1400) -> str:
    clean = body.split("\n\n__lexical_tokens__", 1)[0].strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _entity_type_for_id(entity_id: str) -> str | None:
    prefix = entity_id.split("-", 1)[0].upper()
    return ENTITY_PREFIXES.get(prefix)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _datetime_filter(column: Any, since: datetime | None, until: datetime | None) -> list[Any]:
    conditions: list[Any] = []
    if since is not None:
        conditions.append(column >= _naive_utc(since))
    if until is not None:
        conditions.append(column <= _naive_utc(until))
    return conditions


class RetrievalService:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.indexer = SearchIndexer(self.root)
        self.grep = GrepBackend(self.root)

    def index_status(self) -> dict[str, Any]:
        return self.indexer.status()

    def index_rebuild(self) -> dict[str, Any]:
        return self.indexer.rebuild()

    def index_verify(self) -> dict[str, Any]:
        return self.indexer.verify()

    def search(self, query: SearchQuery) -> list[SearchResult]:
        if query.limit < 1 or query.limit > 200:
            raise ValueError("Search limit must be between 1 and 200.")
        if query.strategy not in {"exact", "lexical", "grep", "hybrid"}:
            raise ValueError("Search strategy must be exact, lexical, grep, or hybrid.")
        if query.graph_depth < 0 or query.graph_depth > 3:
            raise ValueError("Graph depth must be between 0 and 3.")
        self.indexer.ensure_ready()
        candidates: dict[tuple[str, str], SearchResult] = {}
        with session_scope(self.root, write=False) as session:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise RuntimeError("Project database is missing its project record.")
            if query.strategy in {"exact", "lexical", "hybrid"}:
                self._add_exact_candidates(session, project.id, query, candidates)
            if query.strategy in {"lexical", "hybrid"}:
                self._add_fts_candidates(session, project.id, query, candidates)
            if not query.text.strip():
                self._add_structured_candidates(session, project.id, query, candidates)

        if query.strategy in {"grep", "hybrid"} and query.text.strip():
            self._add_grep_candidates(query, candidates)

        ranked = sorted(candidates.values(), key=lambda item: (-item.score, item.entity_type, item.entity_id))
        if query.graph_depth:
            ranked = self._expand_graph(ranked[: query.limit], query.graph_depth, candidates, query)
        ranked = self._inject_replacements(ranked, candidates, query)
        return sorted(ranked, key=lambda item: (-item.score, item.entity_type, item.entity_id))[: query.limit]

    def _base_document_stmt(self, project_id: str, query: SearchQuery):
        stmt = select(SearchDocument).where(SearchDocument.project_id == project_id)
        if query.entity_types:
            stmt = stmt.where(SearchDocument.entity_type.in_(query.entity_types))
        if query.statuses:
            stmt = stmt.where(SearchDocument.status.in_(query.statuses))
        if not query.include_superseded:
            stmt = stmt.where(SearchDocument.status != "superseded")
        if query.since:
            stmt = stmt.where(SearchDocument.updated_at >= _naive_utc(query.since))
        if query.until:
            stmt = stmt.where(SearchDocument.updated_at <= _naive_utc(query.until))
        return stmt

    @staticmethod
    def _matches_query_filters(document: SearchDocument, query: SearchQuery) -> bool:
        metadata = json_loads(document.metadata_json, {})
        if query.entity_types and document.entity_type not in query.entity_types:
            return False
        if query.statuses and document.status not in query.statuses:
            return False
        if not query.include_superseded and document.status == "superseded":
            return False
        since = _naive_utc(query.since)
        until = _naive_utc(query.until)
        updated = _naive_utc(document.updated_at)
        if since and updated and updated < since:
            return False
        if until and updated and updated > until:
            return False
        if query.evidence_types:
            if document.entity_type != "evidence":
                return False
            if metadata.get("evidence_type") not in query.evidence_types:
                return False
        if query.task_id:
            if document.entity_type == "task":
                if document.entity_id != query.task_id:
                    return False
            elif metadata.get("task_id") != query.task_id:
                return False
        return True

    def _document_result(
        self,
        document: SearchDocument,
        *,
        score: float,
        source: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> SearchResult:
        metadata = json_loads(document.metadata_json, {})
        if extra_metadata:
            metadata.update(extra_metadata)
        adjusted = score + document.authority_level / 10
        if document.integrity_status == "valid":
            adjusted += 10
        elif document.integrity_status in {"corrupted", "missing", "unavailable"}:
            adjusted -= 80
        if document.status == "superseded":
            adjusted -= 25
        return SearchResult(
            entity_type=document.entity_type,
            entity_id=document.entity_id,
            chunk_type=document.chunk_type,
            title=document.title,
            excerpt=_clean_body(document.body),
            status=document.status,
            authority_level=document.authority_level,
            integrity_status=document.integrity_status,
            score=adjusted,
            match_sources=[source],
            source_hash=document.source_hash,
            metadata=metadata,
        )

    @staticmethod
    def _merge(candidates: dict[tuple[str, str], SearchResult], result: SearchResult) -> None:
        existing = candidates.get(result.key)
        if existing is None:
            candidates[result.key] = result
            return
        existing.score = max(existing.score, result.score)
        for source in result.match_sources:
            if source not in existing.match_sources:
                existing.match_sources.append(source)
        existing.metadata.update(result.metadata)
        if len(result.excerpt) > len(existing.excerpt):
            existing.excerpt = result.excerpt

    def _add_exact_candidates(
        self,
        session: Session,
        project_id: str,
        query: SearchQuery,
        candidates: dict[tuple[str, str], SearchResult],
    ) -> None:
        ids = extract_entity_ids(query.text)
        for entity_id in ids:
            stmt = self._base_document_stmt(project_id, query).where(
                SearchDocument.entity_id == entity_id
            )
            document = session.scalar(stmt.order_by(SearchDocument.authority_level.desc()).limit(1))
            if document and self._matches_query_filters(document, query):
                self._merge(candidates, self._document_result(document, score=10_000, source="exact_id"))
        hashes = extract_hashes(query.text)
        if hashes:
            evidence_rows = session.scalars(
                select(Evidence).where(Evidence.project_id == project_id, Evidence.sha256.in_(hashes))
            ).all()
            for evidence in evidence_rows:
                document = session.scalar(
                    self._base_document_stmt(project_id, query)
                    .where(SearchDocument.entity_type == "evidence", SearchDocument.entity_id == evidence.id)
                    .limit(1)
                )
                if document and self._matches_query_filters(document, query):
                    self._merge(candidates, self._document_result(document, score=9_500, source="exact_hash"))
        normalized = normalize_text(query.text)
        if normalized and len(normalized) <= 128:
            documents = session.scalars(
                self._base_document_stmt(project_id, query).where(
                    (SearchDocument.title.collate("NOCASE") == query.text.strip())
                    | (SearchDocument.entity_id.collate("NOCASE") == query.text.strip())
                )
            ).all()
            for document in documents:
                if self._matches_query_filters(document, query):
                    self._merge(candidates, self._document_result(document, score=8_000, source="exact_phrase"))

    def _add_structured_candidates(
        self,
        session: Session,
        project_id: str,
        query: SearchQuery,
        candidates: dict[tuple[str, str], SearchResult],
    ) -> None:
        stmt = self._base_document_stmt(project_id, query).order_by(
            SearchDocument.authority_level.desc(), SearchDocument.updated_at.desc()
        )
        for rank, document in enumerate(session.scalars(stmt.limit(query.limit * 4)).all(), 1):
            if not self._matches_query_filters(document, query):
                continue
            self._merge(
                candidates,
                self._document_result(document, score=500 - rank, source="structured"),
            )

    def _add_fts_candidates(
        self,
        session: Session,
        project_id: str,
        query: SearchQuery,
        candidates: dict[tuple[str, str], SearchResult],
    ) -> None:
        expression = fts_query(query.text)
        if not expression:
            return
        clauses = ["f.project_id = :project_id", "search_documents_fts MATCH :query"]
        parameters: dict[str, Any] = {"project_id": project_id, "query": expression, "limit": query.limit * 4}
        if query.entity_types:
            placeholders = []
            for index, entity_type in enumerate(query.entity_types):
                key = f"entity_type_{index}"
                placeholders.append(f":{key}")
                parameters[key] = entity_type
            clauses.append(f"f.entity_type IN ({','.join(placeholders)})")
        sql = text(
            f"""
            SELECT f.document_id, bm25(search_documents_fts) AS rank
            FROM search_documents_fts AS f
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT :limit
            """
        )
        rows = session.execute(sql, parameters).all()
        for position, (document_id, raw_rank) in enumerate(rows, 1):
            document = session.get(SearchDocument, document_id)
            if document is None:
                continue
            if query.statuses and document.status not in query.statuses:
                continue
            if not query.include_superseded and document.status == "superseded":
                continue
            if not self._matches_query_filters(document, query):
                continue
            rank_bonus = max(0.0, 1.0 / (1.0 + abs(float(raw_rank or 0.0))))
            self._merge(
                candidates,
                self._document_result(
                    document,
                    score=1_000 - position + rank_bonus,
                    source="fts",
                    extra_metadata={"fts_rank": float(raw_rank or 0.0)},
                ),
            )

    def _map_grep_match(self, match: GrepMatch) -> tuple[str, str]:
        for pattern, entity_type in DOC_PATH_PATTERNS:
            found = pattern.search(match.path)
            if found:
                return entity_type, found.group(1).upper()
        return "repository_file", match.path

    def _add_grep_candidates(
        self,
        query: SearchQuery,
        candidates: dict[tuple[str, str], SearchResult],
    ) -> None:
        matches = self.grep.search(query.text, limit=query.limit * 3)
        for rank, match in enumerate(matches, 1):
            entity_type, entity_id = self._map_grep_match(match)
            if query.entity_types and entity_type not in query.entity_types:
                continue
            key = (entity_type, entity_id)
            if key in candidates:
                result = SearchResult(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    chunk_type="grep",
                    title=match.path,
                    excerpt=f"{match.path}:{match.line_number}: {match.line}",
                    status=candidates[key].status,
                    authority_level=candidates[key].authority_level,
                    integrity_status=candidates[key].integrity_status,
                    score=700 - rank,
                    match_sources=["grep"],
                    metadata={"grep_matches": [f"{match.path}:{match.line_number}"]},
                )
                self._merge(candidates, result)
                continue
            if entity_type != "repository_file":
                self.indexer.ensure_ready()
                with session_scope(self.root, write=False) as session:
                    document = session.scalar(
                        select(SearchDocument)
                        .where(
                            SearchDocument.entity_type == entity_type,
                            SearchDocument.entity_id == entity_id,
                        )
                        .order_by(SearchDocument.authority_level.desc())
                        .limit(1)
                    )
                if document and self._matches_query_filters(document, query):
                    self._merge(
                        candidates,
                        self._document_result(
                            document,
                            score=700 - rank,
                            source="grep",
                            extra_metadata={"grep_matches": [f"{match.path}:{match.line_number}"]},
                        ),
                    )
                    continue
            if query.statuses or query.evidence_types or query.task_id or query.since or query.until:
                continue
            self._merge(
                candidates,
                SearchResult(
                    entity_type="repository_file",
                    entity_id=match.path,
                    chunk_type="line",
                    title=match.path,
                    excerpt=f"line {match.line_number}: {match.line}",
                    status=None,
                    authority_level=30,
                    integrity_status=None,
                    score=700 - rank,
                    match_sources=["grep"],
                    metadata={"path": match.path, "line_number": match.line_number},
                ),
            )

    def _expand_graph(
        self,
        initial: list[SearchResult],
        depth: int,
        candidates: dict[tuple[str, str], SearchResult],
        query: SearchQuery,
    ) -> list[SearchResult]:
        expanded = list(initial)
        seen = {item.key for item in expanded}
        queue = deque((item.entity_type, item.entity_id, 0, item.score) for item in initial)
        while queue and len(expanded) < 100:
            entity_type, entity_id, level, parent_score = queue.popleft()
            if level >= depth or entity_type == "repository_file":
                continue
            trace = self.trace(entity_id, depth=1, max_nodes=30)
            for node in trace["nodes"]:
                key = (node["entity_type"], node["entity_id"])
                if key in seen or key == (entity_type, entity_id):
                    continue
                seen.add(key)
                document = self._document_for_entity(*key)
                if document is None or not self._matches_query_filters(document, query):
                    continue
                result = self._document_result(
                    document,
                    score=max(100.0, parent_score * 0.35) - level,
                    source="graph",
                )
                candidates[key] = result
                expanded.append(result)
                queue.append((key[0], key[1], level + 1, result.score))
        return expanded

    def _inject_replacements(
        self,
        ranked: list[SearchResult],
        candidates: dict[tuple[str, str], SearchResult],
        query: SearchQuery,
    ) -> list[SearchResult]:
        output = list(ranked)
        seen = {item.key for item in output}
        replacement_query = replace(query, statuses=())
        for item in list(ranked):
            replacement = item.metadata.get("superseded_by")
            if not replacement:
                continue
            replacement_type = item.entity_type
            key = (replacement_type, replacement)
            if key in seen:
                existing = candidates.get(key)
                if existing is not None:
                    if "replacement" not in existing.match_sources:
                        existing.match_sources.append("replacement")
                    existing.metadata.setdefault("replaces", item.entity_id)
                    existing.score = max(existing.score, item.score + 1)
                continue
            document = self._document_for_entity(*key)
            if document is None or not self._matches_query_filters(document, replacement_query):
                continue
            result = self._document_result(
                document,
                score=item.score + 1,
                source="replacement",
                extra_metadata={"replaces": item.entity_id},
            )
            candidates[key] = result
            output.append(result)
            seen.add(key)
        return output

    def _document_for_entity(self, entity_type: str, entity_id: str) -> SearchDocument | None:
        with session_scope(self.root, write=False) as session:
            return session.scalar(
                select(SearchDocument)
                .where(
                    SearchDocument.entity_type == entity_type,
                    SearchDocument.entity_id == entity_id,
                )
                .order_by(SearchDocument.authority_level.desc())
                .limit(1)
            )

    def trace(self, entity_id: str, *, depth: int = 1, max_nodes: int = 50) -> dict[str, Any]:
        if depth < 0 or depth > 3:
            raise ValueError("Trace depth must be between 0 and 3.")
        if max_nodes < 1 or max_nodes > 200:
            raise ValueError("Trace max_nodes must be between 1 and 200.")
        entity_id = entity_id.upper()
        entity_type = _entity_type_for_id(entity_id)
        if entity_type is None:
            raise ValueError(f"Cannot infer entity type from ID: {entity_id}")
        self.indexer.ensure_ready()
        nodes: dict[tuple[str, str], dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        queue = deque([(entity_type, entity_id, 0)])
        truncated = False
        with session_scope(self.root, write=False) as session:
            if not self._entity_exists(session, entity_type, entity_id):
                raise ValueError(f"Entity not found: {entity_id}")
            while queue and len(nodes) < max_nodes:
                current_type, current_id, level = queue.popleft()
                key = (current_type, current_id)
                if key in nodes:
                    continue
                nodes[key] = self._node_summary(session, current_type, current_id)
                if level >= depth:
                    continue
                for edge in self._direct_edges(session, current_type, current_id):
                    edge_key = (
                        edge["source_type"],
                        edge["source_id"],
                        edge["relation"],
                        edge["target_type"],
                        edge["target_id"],
                    )
                    if not any(
                        (
                            item["source_type"],
                            item["source_id"],
                            item["relation"],
                            item["target_type"],
                            item["target_id"],
                        )
                        == edge_key
                        for item in edges
                    ):
                        edges.append(edge)
                    for next_type, next_id in [
                        (edge["source_type"], edge["source_id"]),
                        (edge["target_type"], edge["target_id"]),
                    ]:
                        if (next_type, next_id) not in nodes:
                            if len(nodes) + len(queue) < max_nodes:
                                queue.append((next_type, next_id, level + 1))
                            else:
                                truncated = True
        return {
            "root": {"entity_type": entity_type, "entity_id": entity_id},
            "nodes": list(nodes.values()),
            "edges": edges,
            "truncated": truncated or bool(queue),
        }

    @staticmethod
    def _entity_exists(session: Session, entity_type: str, entity_id: str) -> bool:
        model = MODEL_BY_TYPE.get(entity_type)
        return model is not None and session.get(model, entity_id) is not None

    def _node_summary(self, session: Session, entity_type: str, entity_id: str) -> dict[str, Any]:
        document = session.scalar(
            select(SearchDocument)
            .where(
                SearchDocument.entity_type == entity_type,
                SearchDocument.entity_id == entity_id,
            )
            .order_by(SearchDocument.authority_level.desc())
            .limit(1)
        )
        if document:
            return {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "title": document.title,
                "status": document.status,
                "integrity_status": document.integrity_status,
                "authority_level": document.authority_level,
            }
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": entity_id,
            "status": None,
            "integrity_status": None,
            "authority_level": 0,
        }

    def _direct_edges(self, session: Session, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        edges = [
            {
                "source_type": row.source_type,
                "source_id": row.source_id,
                "relation": row.relation_type,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "synthetic": False,
            }
            for row in session.scalars(
                select(Relation).where(
                    ((Relation.source_type == entity_type) & (Relation.source_id == entity_id))
                    | ((Relation.target_type == entity_type) & (Relation.target_id == entity_id))
                )
            ).all()
        ]

        def add(source_type: str, source_id: str | None, relation: str, target_type: str, target_id: str | None) -> None:
            if source_id and target_id:
                edges.append(
                    {
                        "source_type": source_type,
                        "source_id": source_id,
                        "relation": relation,
                        "target_type": target_type,
                        "target_id": target_id,
                        "synthetic": True,
                    }
                )

        if entity_type == "evidence":
            evidence = session.get(Evidence, entity_id)
            assert evidence is not None
            add("evidence", evidence.id, "recorded_in", "task", evidence.task_id)
            add("evidence", evidence.id, "recorded_by", "task_event", evidence.task_event_id)
            for event in session.scalars(select(TaskEvent).where(TaskEvent.evidence_id == entity_id)).all():
                add("task_event", event.id, "references", "evidence", entity_id)
            for run in session.scalars(select(TestRun).where(TestRun.evidence_id == entity_id)).all():
                add("test_run", run.id, "produces", "evidence", entity_id)
            for artifact in session.scalars(select(Artifact).where(Artifact.evidence_id == entity_id)).all():
                add("artifact", artifact.id, "records", "evidence", entity_id)
        elif entity_type == "task":
            for event in session.scalars(select(TaskEvent).where(TaskEvent.task_id == entity_id)).all():
                add("task", entity_id, "has_event", "task_event", event.id)
            for evidence in session.scalars(select(Evidence).where(Evidence.task_id == entity_id)).all():
                add("task", entity_id, "produces", "evidence", evidence.id)
            for change in session.scalars(select(Change).where(Change.task_id == entity_id)).all():
                add("task", entity_id, "produces", "change", change.id)
            for run in session.scalars(select(TestRun).where(TestRun.task_id == entity_id)).all():
                add("task", entity_id, "runs", "test_run", run.id)
        elif entity_type == "task_event":
            event = session.get(TaskEvent, entity_id)
            assert event is not None
            add("task", event.task_id, "has_event", "task_event", event.id)
            add("task_event", event.id, "references", "evidence", event.evidence_id)
        elif entity_type == "test_run":
            run = session.get(TestRun, entity_id)
            assert run is not None
            add("test_run", run.id, "instance_of", "test_spec", run.test_spec_id)
            add("test_run", run.id, "belongs_to", "task", run.task_id)
            add("test_run", run.id, "evaluates", "build", run.build_id)
            add("test_run", run.id, "uses", "snapshot", run.snapshot_id)
            add("test_run", run.id, "produces", "evidence", run.evidence_id)
        elif entity_type == "build":
            build = session.get(Build, entity_id)
            assert build is not None
            add("build", build.id, "built_from", "change", build.change_id)
            for run in session.scalars(select(TestRun).where(TestRun.build_id == entity_id)).all():
                add("test_run", run.id, "evaluates", "build", build.id)
        elif entity_type == "change":
            change = session.get(Change, entity_id)
            assert change is not None
            add("change", change.id, "belongs_to", "task", change.task_id)
            for build in session.scalars(select(Build).where(Build.change_id == entity_id)).all():
                add("change", change.id, "produces", "build", build.id)
        elif entity_type == "test_spec":
            for run in session.scalars(select(TestRun).where(TestRun.test_spec_id == entity_id)).all():
                add("test_run", run.id, "instance_of", "test_spec", entity_id)
        elif entity_type == "snapshot":
            for run in session.scalars(select(TestRun).where(TestRun.snapshot_id == entity_id)).all():
                add("test_run", run.id, "uses", "snapshot", entity_id)
        elif entity_type == "requirement_plan":
            plan = session.get(RequirementPlanVersion, entity_id)
            assert plan is not None
            add("requirement", plan.requirement_id, "has_plan", "requirement_plan", plan.id)
        elif entity_type == "conclusion":
            conclusion = session.get(Conclusion, entity_id)
            assert conclusion is not None
            add("conclusion", conclusion.id, "superseded_by", "conclusion", conclusion.superseded_by)
            for prior in session.scalars(select(Conclusion).where(Conclusion.superseded_by == entity_id)).all():
                add("conclusion", prior.id, "superseded_by", "conclusion", entity_id)
        elif entity_type == "requirement":
            requirement = session.get(Requirement, entity_id)
            assert requirement is not None
            add("requirement", requirement.id, "superseded_by", "requirement", requirement.superseded_by)
            for plan in session.scalars(
                select(RequirementPlanVersion).where(RequirementPlanVersion.requirement_id == entity_id)
            ).all():
                add("requirement", entity_id, "has_plan", "requirement_plan", plan.id)
        return edges

    def list_evidence(
        self,
        *,
        evidence_type: str | None = None,
        task_id: str | None = None,
        integrity: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("Evidence limit must be between 1 and 500.")
        if offset < 0:
            raise ValueError("Evidence offset cannot be negative.")
        with session_scope(self.root, write=False) as session:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise RuntimeError("Project database is missing its project record.")
            conditions: list[Any] = [Evidence.project_id == project.id]
            if evidence_type:
                conditions.append(Evidence.evidence_type == evidence_type)
            if task_id:
                conditions.append(Evidence.task_id == task_id)
            conditions.extend(_datetime_filter(Evidence.created_at, since, until))
            rows = session.scalars(
                select(Evidence)
                .where(and_(*conditions))
                .order_by(Evidence.created_at.desc())
                .offset(offset)
                .limit(limit * 3 if integrity else limit)
            ).all()
            output = []
            for evidence in rows:
                current_integrity = _evidence_integrity(self.root, evidence)
                if integrity and current_integrity != integrity:
                    continue
                output.append(
                    {
                        "id": evidence.id,
                        "type": evidence.evidence_type,
                        "task_id": evidence.task_id,
                        "task_event_id": evidence.task_event_id,
                        "storage_uri": evidence.storage_uri,
                        "sha256": evidence.sha256,
                        "size": evidence.size,
                        "mime_type": evidence.mime_type,
                        "integrity": current_integrity,
                        "created_at": evidence.created_at.isoformat(),
                        "metadata": json_loads(evidence.metadata_json, {}),
                    }
                )
                if len(output) >= limit:
                    break
            return output

    def evidence_usage(self, evidence_id: str) -> dict[str, Any]:
        evidence_id = evidence_id.upper()
        with session_scope(self.root, write=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise ValueError(f"Evidence not found: {evidence_id}")
            integrity = _evidence_integrity(self.root, evidence)
        trace = self.trace(evidence_id, depth=1, max_nodes=100)
        return {
            "evidence_id": evidence_id,
            "integrity": integrity,
            "usages": [
                edge
                for edge in trace["edges"]
                if edge["source_id"] == evidence_id or edge["target_id"] == evidence_id
            ],
            "nodes": trace["nodes"],
        }

    def build_context(self, query: SearchQuery, *, budget: int = 12_000) -> str:
        if budget < 500:
            raise ValueError("Context budget must be at least 500 characters.")
        results = self.search(query)
        with session_scope(self.root, write=False) as session:
            project = session.scalar(select(Project).limit(1))
            if project is None:
                raise RuntimeError("Project database is missing its project record.")
            header = (
                f"# Retrieval context\n\n"
                f"Project: {project.name} (`{project.id}`)\n\n"
                f"Query: {query.text or '(latest structured records)'}\n\n"
                f"Index: {self.indexer.status()['status']}\n"
            )
        sections = [header]
        used = len(header)
        group_names = {
            "conclusion": "Related conclusions",
            "evidence": "Related evidence",
            "requirement": "Related requirements",
            "task": "Related tasks",
            "test_run": "Related test runs",
            "change": "Related changes",
            "build": "Related builds",
            "repository_file": "Related repository files",
        }
        current_group: str | None = None
        for result in results:
            group = group_names.get(result.entity_type, "Related records")
            group_heading = "" if group == current_group else f"\n## {group}\n"
            metadata_lines = [f"Match: {', '.join(result.match_sources)}"]
            if result.status:
                metadata_lines.append(f"Status: {result.status}")
            if result.integrity_status:
                metadata_lines.append(f"Integrity: {result.integrity_status}")
            replacement = result.metadata.get("superseded_by")
            if replacement:
                metadata_lines.append(f"Superseded by: {replacement}")
            block = (
                group_heading
                + f"\n### {result.entity_id} — {result.title}\n\n"
                + "\n".join(metadata_lines)
                + f"\n\n{result.excerpt}\n"
            )
            if used + len(block) > budget:
                break
            sections.append(block)
            used += len(block)
            current_group = group
        if len(sections) == 1:
            fallback = "\nNo matching historical records were found.\n"
            if used + len(fallback) <= budget:
                sections.append(fallback)
        return "".join(sections)
