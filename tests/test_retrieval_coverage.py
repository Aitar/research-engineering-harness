from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

import reharness.retrieval.grep as grep_module
import reharness.retrieval.index as index_module
from reharness.cli import app
from reharness.db import init_database, session_scope
from reharness.models import (
    Artifact,
    SearchDocument,
    SearchIndexState,
    TaskEvent,
    TestRun as RunModel,
)
from reharness.retrieval.grep import GrepBackend
from reharness.retrieval.index import SearchIndexer, _evidence_integrity, _evidence_text
from reharness.retrieval.models import SearchQuery
from reharness.retrieval.service import RetrievalService, _clean_body, _naive_utc
from reharness.services import Harness, HarnessError

runner = CliRunner()


def _commit(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def test_full_engineering_graph_and_all_trace_entity_types(harness: Harness) -> None:
    requirement = harness.create_requirement("Trace every engineering entity", ["smoke passes"])
    plan = harness.add_requirement_plan(requirement.id, "Implement, build, and test")
    harness.transition_requirement(requirement.id, "accepted")
    task = harness.start_task("development", "Implement traceable feature", requirement_ids=[requirement.id])
    base = _head(harness.root)
    head = _commit(harness.root, "traceable.py", "READY = True\n", "implement traceable feature")
    change = harness.capture_change(base, head, task.id, [requirement.id])
    harness.transition_requirement(requirement.id, "implemented")
    artifact_path = harness.root / "traceable.bin"
    artifact_path.write_bytes(b"traceable build")
    build = harness.capture_build(artifact_path, change_id=change.id)
    spec = harness.define_test(
        "Traceable smoke",
        "smoke",
        [sys.executable, "-c", "pass"],
        [requirement.id],
    )
    run = harness.run_test(spec.id, task.id, build.id)
    harness.verify_requirement(requirement.id, run.id)

    with session_scope(harness.root, write=False) as session:
        stored_run = session.get(RunModel, run.id)
        assert stored_run and stored_run.snapshot_id and stored_run.evidence_id
        event = session.scalar(
            select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.evidence_id == run.evidence_id)
        )
        assert event is not None
        artifact = session.scalar(select(Artifact).where(Artifact.evidence_id == run.evidence_id))
        assert artifact is not None
        snapshot_id = stored_run.snapshot_id

    status = harness.index_rebuild()
    assert status["status"] == "ready"
    searchable_types = {
        item["entity_type"]
        for item in harness.search_history("traceable", strategy="lexical", limit=50, graph_depth=0)
    }
    assert {"task", "build", "test_spec", "test_run"} <= searchable_types
    assert harness.search_history(requirement.id, strategy="exact", graph_depth=0)[0]["entity_id"] == requirement.id
    assert harness.search_history(change.id, strategy="exact", graph_depth=0)[0]["entity_id"] == change.id

    targets = [
        task.id,
        event.id,
        run.id,
        build.id,
        change.id,
        spec.id,
        snapshot_id,
        plan.id,
        artifact.id,
        requirement.id,
    ]
    for entity_id in targets:
        trace = harness.trace_history(entity_id, depth=1, max_nodes=100)
        assert trace["root"]["entity_id"] == entity_id
        assert trace["nodes"]

    assert any(edge["relation"] == "instance_of" for edge in harness.trace_history(run.id)["edges"])
    assert any(edge["relation"] == "built_from" for edge in harness.trace_history(build.id)["edges"])
    assert any(edge["relation"] == "has_plan" for edge in harness.trace_history(plan.id)["edges"])
    assert harness.trace_history(task.id, depth=3, max_nodes=1)["truncated"] is True
    with pytest.raises(HarnessError):
        harness.trace_history(task.id, max_nodes=0)


def test_index_projects_nontext_missing_large_invalid_and_rejected_records(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = harness.root / "weights.bin"
    binary.write_bytes(b"\x00\x01\x02")
    binary_evidence = harness.capture_evidence(binary, "model_manifest")

    invalid_text = harness.root / "invalid.txt"
    invalid_text.write_bytes(b"\xff\xfe\xfd")
    invalid_evidence = harness.capture_evidence(invalid_text, "human_review")

    large = harness.root / "large.log"
    large.write_text("x" * (128 * 1024 + 10), encoding="utf-8")
    large_evidence = harness.capture_evidence(large, "command_log")

    missing = harness.root / "missing.txt"
    missing.write_text("later removed", encoding="utf-8")
    missing_evidence = harness.capture_evidence(missing, "experiment_result")
    managed_missing = harness.root / harness.evidence_data(missing_evidence.id)["storage_uri"]
    managed_missing.unlink()

    requirement = harness.create_requirement("Rejected search record")
    harness.transition_requirement(requirement.id, "rejected")
    harness.index_rebuild()

    by_id = {item["entity_id"]: item for item in harness.search_history("", limit=100, graph_depth=0)}
    assert by_id[binary_evidence.id]["integrity_status"] == "valid"
    assert by_id[missing_evidence.id]["integrity_status"] == "missing"
    assert by_id[requirement.id]["authority_level"] == 35

    with session_scope(harness.root, write=False) as session:
        invalid_row = session.get(index_module.Evidence, invalid_evidence.id)
        large_row = session.get(index_module.Evidence, large_evidence.id)
        binary_row = session.get(index_module.Evidence, binary_evidence.id)
        assert invalid_row and large_row and binary_row
        assert _evidence_text(harness.root, invalid_row) == ""
        assert _evidence_text(harness.root, large_row) == ""
        assert _evidence_text(harness.root, binary_row) == ""
        monkeypatch.setattr(index_module, "sha256_file", lambda _: (_ for _ in ()).throw(OSError("denied")))
        assert _evidence_integrity(harness.root, binary_row) == "unavailable"


def test_index_failure_state_missing_orphan_and_stale_findings(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    conclusion = harness.create_conclusion("Index failure sentinel")
    harness.index_rebuild()

    original = index_module.project_documents
    monkeypatch.setattr(index_module, "project_documents", lambda *args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(HarnessError, match="rebuild failed"):
        harness.index_rebuild()
    failed = harness.index_status()
    assert failed["status"] == "failed"
    assert "boom" in failed["last_error"]
    monkeypatch.setattr(index_module, "project_documents", original)
    harness.index_rebuild()

    with session_scope(harness.root) as session:
        project_id = harness.project_data()["id"]
        missing_doc = session.scalar(select(SearchDocument).where(SearchDocument.entity_id == conclusion.id))
        assert missing_doc is not None
        session.delete(missing_doc)
        session.add(
            SearchDocument(
                id="orphan:record:summary",
                project_id=project_id,
                entity_type="conclusion",
                entity_id="CON-FFFFFFFFFFFFFFFFFFFF",
                chunk_type="summary",
                title="orphan",
                body="orphan",
                status="exploring",
                authority_level=1,
                integrity_status=None,
                source_hash="f" * 64,
                metadata_json="{}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                indexed_at=datetime.now(UTC),
            )
        )
        state = session.get(SearchIndexState, project_id)
        assert state is not None
        state.status = "stale"
    verify = harness.index_verify()
    codes = {item["code"] for item in verify["findings"]}
    assert {"SEARCH_INDEX_MISSING_DOCUMENT", "SEARCH_INDEX_ORPHAN_DOCUMENT", "SEARCH_INDEX_STALE"} <= codes


def test_grep_defensive_branches(harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = GrepBackend(harness.root)
    assert backend.search("") == []
    assert backend.search("x", limit=0) == []
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    assert GrepBackend(empty_root)._ripgrep("x", limit=1, timeout=1) == []

    fallback = harness.root / "docs" / "fallback-branch.md"
    fallback.parent.mkdir(exist_ok=True)
    fallback.write_text("fallback branch needle\nfallback branch needle\n", encoding="utf-8")

    monkeypatch.setattr(grep_module.shutil, "which", lambda _: "/usr/bin/rg")
    monkeypatch.setattr(
        grep_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="failed"),
    )
    assert backend.search("fallback branch needle", limit=1)[0].path == "docs/fallback-branch.md"

    inside = (harness.root / "docs" / "inside.md").resolve()
    outside = (harness.root.parent / "outside.md").resolve()
    events = [
        json.dumps({"type": "begin", "data": {}}),
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(outside)},
                    "lines": {"text": "outside\n"},
                    "line_number": 1,
                },
            }
        ),
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(inside)},
                    "lines": {"text": "inside branch needle\n"},
                    "line_number": 2,
                },
            }
        ),
    ]
    monkeypatch.setattr(
        grep_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="\n".join(events), stderr=""),
    )
    matches = backend._ripgrep("inside branch needle", limit=1, timeout=1)
    assert matches == [grep_module.GrepMatch("docs/inside.md", 2, "inside branch needle")]

    monkeypatch.setattr(
        grep_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    assert backend.search("fallback branch needle", limit=1)[0].path == "docs/fallback-branch.md"


def test_search_filter_merge_replacement_and_context_fallback_branches(harness: Harness) -> None:
    long = "long body " * 300
    assert _clean_body(long).endswith("…")
    assert _naive_utc(None) is None
    naive = datetime.now()
    assert _naive_utc(naive) is naive

    supported_file = harness.root / "supported.txt"
    supported_file.write_text("shared status phrase", encoding="utf-8")
    evidence = harness.capture_evidence(supported_file, "experiment_result")
    supported = harness.create_conclusion("Shared status phrase conclusion")
    harness.support_conclusion(supported.id, [evidence.id])
    harness.create_conclusion("Shared status phrase exploring")

    filtered = harness.search_history(
        "shared status phrase",
        entity_types=["conclusion"],
        statuses=["supported"],
        graph_depth=0,
    )
    assert [item["entity_id"] for item in filtered] == [supported.id]

    exact_title = harness.search_history("Shared status phrase conclusion", strategy="exact", graph_depth=0)
    assert exact_title[0]["entity_id"] == supported.id
    assert "exact_phrase" in exact_title[0]["match_sources"]

    old_file = harness.root / "old.txt"
    old_file.write_text("old exact support", encoding="utf-8")
    old_evidence = harness.capture_evidence(old_file, "experiment_result")
    old = harness.create_conclusion("Old exact replacement query")
    harness.support_conclusion(old.id, [old_evidence.id])
    replacement = harness.create_conclusion("Completely different replacement")
    harness.supersede_conclusion(old.id, replacement.id)
    replacement_results = harness.search_history(old.id, strategy="exact", graph_depth=0)
    replacement_item = next(item for item in replacement_results if item["entity_id"] == replacement.id)
    assert replacement_item["match_sources"] == ["replacement"]

    no_results = RetrievalService(harness.root).build_context(
        SearchQuery(text="no-such-exact-record", strategy="exact", graph_depth=0),
        budget=700,
    )
    assert "No matching historical records" in no_results
    with pytest.raises(HarnessError):
        harness.search_history("x", graph_depth=4)

    assert harness.search_history(
        "shared status phrase", strategy="grep", entity_types=["evidence"], graph_depth=0
    )
    assert harness.search_history(
        "shared status phrase", strategy="grep", entity_types=["build"], graph_depth=0
    ) == []


def test_list_evidence_dates_offsets_missing_usage_and_bare_project_errors(harness: Harness, tmp_path: Path) -> None:
    first = harness.root / "first.txt"
    first.write_text("first", encoding="utf-8")
    first_evidence = harness.capture_evidence(first, "human_review")
    second = harness.root / "second.txt"
    second.write_text("second", encoding="utf-8")
    second_evidence = harness.capture_evidence(second, "human_review")
    rows = harness.list_evidence_history(limit=1, offset=1)
    assert rows[0]["id"] == first_evidence.id
    future = datetime.now(UTC) + timedelta(days=1)
    assert harness.list_evidence_history(until=future)
    with pytest.raises(HarnessError):
        harness.list_evidence_history(offset=-1)
    with pytest.raises(HarnessError):
        harness.evidence_usage("EVD-FFFFFFFFFFFFFFFFFFFF")

    bare = tmp_path / "bare-search"
    bare.mkdir()
    init_database(bare)
    indexer = SearchIndexer(bare)
    with pytest.raises(RuntimeError):
        indexer.status()
    with pytest.raises(RuntimeError):
        indexer.rebuild()
    with pytest.raises(RuntimeError):
        indexer.verify()
    service = RetrievalService(bare)
    with pytest.raises(RuntimeError):
        service.search(SearchQuery(text="x"))
    with pytest.raises(RuntimeError):
        service.list_evidence()
    with pytest.raises(RuntimeError):
        service.build_context(SearchQuery(text="x"))


def test_cli_human_outputs_invalid_date_and_failure_exit(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    source = harness.root / "cli-human.txt"
    source.write_text("human CLI retrieval", encoding="utf-8")
    evidence = harness.capture_evidence(source, "human_review")
    conclusion = harness.create_conclusion("Human CLI retrieval conclusion")
    harness.support_conclusion(conclusion.id, [evidence.id])
    monkeypatch.setattr("reharness.cli._harness", lambda: harness)

    assert runner.invoke(app, ["index", "status"]).exit_code == 0
    assert runner.invoke(app, ["index", "rebuild"]).exit_code == 0
    search = runner.invoke(app, ["search", "human CLI retrieval"])
    assert search.exit_code == 0 and conclusion.id in search.output
    trace = runner.invoke(app, ["trace", conclusion.id])
    assert trace.exit_code == 0 and "--supports-->" in trace.output
    evidence_list = runner.invoke(app, ["evidence", "list"])
    assert evidence_list.exit_code == 0 and evidence.id in evidence_list.output
    usage = runner.invoke(app, ["evidence", "usage", evidence.id])
    assert usage.exit_code == 0 and "integrity=valid" in usage.output
    assert runner.invoke(app, ["context", "--topic", "human CLI", "--strategy", "exact"]).exit_code == 0
    invalid_date = runner.invoke(app, ["search", "x", "--since", "not-a-date"])
    assert invalid_date.exit_code == 2

    with session_scope(harness.root) as session:
        project_id = harness.project_data()["id"]
        state = session.get(SearchIndexState, project_id)
        assert state is not None
        state.status = "stale"
    failed_verify = runner.invoke(app, ["index", "verify", "--json"])
    assert failed_verify.exit_code == 1
