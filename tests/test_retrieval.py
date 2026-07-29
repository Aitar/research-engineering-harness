from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from reharness.cli import app
from reharness.db import session_scope
from reharness.models import SearchDocument, SearchIndexState
from reharness.retrieval.grep import GrepBackend
from reharness.retrieval.models import SearchQuery
from reharness.retrieval.normalizer import (
    extract_entity_ids,
    extract_hashes,
    fts_query,
    lexical_terms,
    normalize_text,
)
from reharness.retrieval.service import RetrievalService
from reharness.services import Harness, HarnessError

runner = CliRunner()


def test_normalization_extracts_ids_hashes_and_cjk_terms() -> None:
    assert normalize_text("  ＡＷＱ 高并发  ") == "awq 高并发"
    terms = lexical_terms("量化模型高并发吞吐下降 AWQ.verify")
    assert {"量化", "高并发", "吞吐", "awq.verify"} <= set(terms)
    assert '"高并发"' in fts_query("高并发")
    assert extract_entity_ids("look at evd-abcdef1234567890 and TASK-12345678") == [
        "EVD-ABCDEF1234567890",
        "TASK-12345678",
    ]
    digest = "a" * 64
    assert extract_hashes(f"sha={digest}") == [digest]


def test_index_lifecycle_stale_rebuild_and_verify(harness: Harness) -> None:
    assert harness.index_status()["status"] == "missing"
    rebuilt = harness.index_rebuild()
    assert rebuilt["status"] == "ready"
    assert rebuilt["document_count"] == rebuilt["fts_count"] >= 1
    assert harness.index_verify()["valid"] is True

    conclusion = harness.create_conclusion("A newly written searchable conclusion")
    assert harness.index_status()["status"] == "stale"
    results = harness.search_history("newly written searchable", strategy="lexical")
    assert results[0]["entity_id"] == conclusion.id
    assert harness.index_status()["status"] == "ready"


def test_exact_id_hash_and_structured_search(harness: Harness) -> None:
    task = harness.start_task("research", "Investigate exact retrieval")
    source = harness.root / "exact-result.json"
    source.write_text('{"finding": "exact evidence"}', encoding="utf-8")
    evidence = harness.capture_evidence(source, "experiment_result", task.id)
    conclusion = harness.create_conclusion("Exact retrieval is deterministic")
    harness.support_conclusion(conclusion.id, [evidence.id])

    by_id = harness.search_history(evidence.id, strategy="exact")
    assert by_id[0]["entity_id"] == evidence.id
    assert "exact_id" in by_id[0]["match_sources"]

    by_hash = harness.search_history(evidence.sha256, strategy="exact")
    assert by_hash[0]["entity_id"] == evidence.id
    assert "exact_hash" in by_hash[0]["match_sources"]

    supported = harness.search_history(
        "", entity_types=["conclusion"], statuses=["supported"], strategy="lexical"
    )
    assert [item["entity_id"] for item in supported] == [conclusion.id]


def test_fts_search_handles_chinese_and_filters(harness: Harness) -> None:
    task = harness.start_task("research", "研究量化模型的请求处理能力")
    source = harness.root / "benchmark.json"
    source.write_text('{"并发": 64, "吞吐": 123}', encoding="utf-8")
    evidence = harness.capture_evidence(source, "benchmark_result", task.id)
    conclusion = harness.create_conclusion("量化模型在高并发短序列下吞吐下降")
    harness.support_conclusion(conclusion.id, [evidence.id])

    results = harness.search_history("高并发吞吐", strategy="lexical", limit=10)
    ids = {item["entity_id"] for item in results}
    assert conclusion.id in ids
    assert evidence.id in ids

    evidence_only = harness.search_history(
        "吞吐",
        entity_types=["evidence"],
        evidence_types=["benchmark_result"],
        task_id=task.id,
        strategy="lexical",
    )
    assert [item["entity_id"] for item in evidence_only] == [evidence.id]

    assert harness.search_history(
        "吞吐", entity_types=["evidence"], evidence_types=["test_report"], strategy="lexical"
    ) == []


def test_safe_grep_searches_repository_files_and_fixed_strings(harness: Harness) -> None:
    source = harness.root / "src" / "widget.py"
    source.parent.mkdir()
    source.write_text("def verify_widget(value):\n    return value[42] + '(literal)'\n", encoding="utf-8")

    results = harness.search_history("value[42] + '(literal)'", strategy="grep")
    assert results[0]["entity_type"] == "repository_file"
    assert results[0]["entity_id"] == "src/widget.py"
    assert "grep" in results[0]["match_sources"]


def test_grep_python_fallback_and_safety(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    source = harness.root / "docs" / "fallback.md"
    source.parent.mkdir(exist_ok=True)
    source.write_text("Fallback needle appears here.\n", encoding="utf-8")
    outside = harness.root.parent / "outside-secret.txt"
    outside.write_text("Fallback needle outside", encoding="utf-8")
    link = harness.root / "docs" / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pass
    huge = harness.root / "docs" / "huge.log"
    huge.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    monkeypatch.setattr("reharness.retrieval.grep.shutil.which", lambda _: None)
    matches = GrepBackend(harness.root).search("Fallback needle")
    assert any(match.path == "docs/fallback.md" for match in matches)
    assert all("outside" not in match.path for match in matches)
    assert all(match.path != "docs/huge.log" for match in matches)


def test_evidence_list_usage_and_integrity_filter(harness: Harness) -> None:
    task = harness.start_task("research", "Capture evidence usage")
    source = harness.root / "usage.txt"
    source.write_text("supporting observation", encoding="utf-8")
    evidence = harness.capture_evidence(source, "human_review", task.id)
    conclusion = harness.create_conclusion("Human review supports the observation")
    harness.support_conclusion(conclusion.id, [evidence.id])

    rows = harness.list_evidence_history(
        evidence_type="human_review", task_id=task.id, integrity="valid"
    )
    assert [row["id"] for row in rows] == [evidence.id]
    usage = harness.evidence_usage(evidence.id)
    assert usage["integrity"] == "valid"
    assert any(
        edge["source_id"] == conclusion.id
        and edge["relation"] == "supports"
        and edge["target_id"] == evidence.id
        for edge in usage["usages"]
    )
    assert any(edge["relation"] == "recorded_in" for edge in usage["usages"])

    managed = harness.root / harness.evidence_data(evidence.id)["storage_uri"]
    managed.write_text("tampered", encoding="utf-8")
    corrupted = harness.list_evidence_history(integrity="corrupted")
    assert evidence.id in {row["id"] for row in corrupted}
    assert harness.list_evidence_history(integrity="valid") == []


def test_trace_expands_conclusion_evidence_task_and_event(harness: Harness) -> None:
    task = harness.start_task("research", "Trace a result")
    source = harness.root / "trace.json"
    source.write_text('{"ok": true}', encoding="utf-8")
    evidence = harness.capture_evidence(source, "experiment_result", task.id)
    conclusion = harness.create_conclusion("The traced experiment succeeded")
    harness.support_conclusion(conclusion.id, [evidence.id])

    trace = harness.trace_history(conclusion.id, depth=2)
    node_ids = {node["entity_id"] for node in trace["nodes"]}
    assert {conclusion.id, evidence.id, task.id} <= node_ids
    assert any(edge["relation"] == "supports" for edge in trace["edges"])
    assert any(edge["relation"] == "recorded_in" for edge in trace["edges"])
    with pytest.raises(HarnessError):
        harness.trace_history("UNKNOWN-12345678")


def test_superseded_result_injects_current_replacement(harness: Harness) -> None:
    old = harness.create_conclusion("Legacy cache policy improves throughput")
    support_file = harness.root / "legacy-support.txt"
    support_file.write_text("legacy benchmark support", encoding="utf-8")
    support = harness.capture_evidence(support_file, "benchmark_result")
    harness.support_conclusion(old.id, [support.id])
    replacement = harness.create_conclusion("Updated cache policy only improves tail latency")
    harness.supersede_conclusion(old.id, replacement.id, "New benchmark changed the interpretation")

    results = harness.search_history("Legacy cache policy", strategy="lexical", limit=10)
    ids = [item["entity_id"] for item in results]
    assert old.id in ids
    assert replacement.id in ids
    replacement_result = next(item for item in results if item["entity_id"] == replacement.id)
    assert "replacement" in replacement_result["match_sources"]
    assert replacement_result["metadata"]["replaces"] == old.id

    status_filtered = harness.search_history(
        "Legacy cache policy", statuses=["superseded"], strategy="lexical", graph_depth=0
    )
    assert replacement.id in {item["entity_id"] for item in status_filtered}

    current_only = harness.search_history(
        "Legacy cache policy", strategy="lexical", include_superseded=False
    )
    assert old.id not in {item["entity_id"] for item in current_only}


def test_index_verify_detects_missing_hash_and_fts_corruption(harness: Harness) -> None:
    conclusion = harness.create_conclusion("Index corruption sentinel")
    harness.index_rebuild()
    with session_scope(harness.root) as session:
        document = session.scalar(
            select(SearchDocument).where(SearchDocument.entity_id == conclusion.id)
        )
        assert document is not None
        document.source_hash = "0" * 64
        session.execute(
            text("DELETE FROM search_documents_fts WHERE document_id = :document_id"),
            {"document_id": document.id},
        )
    verified = harness.index_verify()
    codes = {finding["code"] for finding in verified["findings"]}
    assert "SEARCH_INDEX_HASH_MISMATCH" in codes
    assert "SEARCH_FTS_DOCUMENT_MISMATCH" in codes
    assert verified["valid"] is False
    assert harness.index_rebuild()["status"] == "ready"
    assert harness.index_verify()["valid"] is True


def test_additive_search_schema_is_created_for_legacy_database(harness: Harness) -> None:
    with session_scope(harness.root) as session:
        session.execute(text("DROP TABLE search_documents_fts"))
        session.execute(text("DROP TABLE search_documents"))
        session.execute(text("DROP TABLE search_index_state"))
    service = RetrievalService(harness.root)
    assert service.index_status()["status"] == "missing"
    assert service.index_rebuild()["status"] == "ready"


def test_context_builder_is_bounded_and_provenance_aware(harness: Harness) -> None:
    source = harness.root / "context.txt"
    source.write_text("context evidence for deterministic retrieval", encoding="utf-8")
    evidence = harness.capture_evidence(source, "experiment_result")
    conclusion = harness.create_conclusion("Deterministic retrieval context is useful")
    harness.support_conclusion(conclusion.id, [evidence.id])

    context = harness.context("deterministic retrieval", budget=1000)
    assert len(context) <= 1000
    assert "# Retrieval context" in context
    assert "Related conclusions" in context or "Related evidence" in context
    assert conclusion.id in context or evidence.id in context
    assert "Index: ready" in context
    with pytest.raises(HarnessError):
        harness.context("x", budget=100)


def test_search_date_limits_and_argument_validation(harness: Harness) -> None:
    conclusion = harness.create_conclusion("Date-filtered history")
    now = datetime.now(UTC)
    assert conclusion.id in {
        item["entity_id"]
        for item in harness.search_history("Date-filtered", since=now - timedelta(minutes=1))
    }
    assert harness.search_history("Date-filtered", since=now + timedelta(days=1)) == []
    with pytest.raises(HarnessError):
        harness.search_history("x", limit=0)
    with pytest.raises(HarnessError):
        harness.search_history("x", strategy="vector")
    with pytest.raises(HarnessError):
        harness.trace_history(conclusion.id, depth=4)
    with pytest.raises(HarnessError):
        harness.list_evidence_history(limit=0)


def test_cli_search_trace_evidence_and_index_commands(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task = harness.start_task("research", "CLI retrieval workflow")
    source = harness.root / "cli-evidence.txt"
    source.write_text("CLI search needle", encoding="utf-8")
    evidence = harness.capture_evidence(source, "experiment_result", task.id)
    conclusion = harness.create_conclusion("CLI search needle conclusion")
    harness.support_conclusion(conclusion.id, [evidence.id])
    monkeypatch.setattr("reharness.cli._harness", lambda: harness)

    rebuild = runner.invoke(app, ["index", "rebuild", "--json"])
    assert rebuild.exit_code == 0, rebuild.output
    assert json.loads(rebuild.output)["status"] == "ready"

    search = runner.invoke(app, ["search", "CLI search needle", "--json"])
    assert search.exit_code == 0, search.output
    assert conclusion.id in {item["entity_id"] for item in json.loads(search.output)}

    trace = runner.invoke(app, ["trace", conclusion.id, "--depth", "2", "--json"])
    assert trace.exit_code == 0, trace.output
    assert json.loads(trace.output)["root"]["entity_id"] == conclusion.id

    evidence_list = runner.invoke(app, ["evidence", "list", "--type", "experiment_result", "--json"])
    assert evidence_list.exit_code == 0, evidence_list.output
    assert evidence.id in {item["id"] for item in json.loads(evidence_list.output)}

    usage = runner.invoke(app, ["evidence", "usage", evidence.id, "--json"])
    assert usage.exit_code == 0, usage.output
    assert json.loads(usage.output)["evidence_id"] == evidence.id

    verify = runner.invoke(app, ["index", "verify", "--json"])
    assert verify.exit_code == 0, verify.output
    assert json.loads(verify.output)["valid"] is True
