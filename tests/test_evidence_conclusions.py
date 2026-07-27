from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from reharness.db import session_scope
from reharness.models import Relation
from reharness.services import Harness, HarnessError, StateTransitionError


def make_evidence(harness: Harness, name: str = "result.txt") -> str:
    path = harness.root / name
    path.write_text("evidence", encoding="utf-8")
    return harness.capture_evidence(path, "experiment_result").id


def test_capture_and_verify_evidence(harness: Harness) -> None:
    evidence_id = make_evidence(harness)
    result = harness.verify_evidence(evidence_id)
    assert result["valid"] is True


def test_evidence_tamper_is_detected(harness: Harness) -> None:
    evidence_id = make_evidence(harness)
    data = harness.evidence_data(evidence_id)
    path = harness.root / data["storage_uri"]
    path.write_text("tampered", encoding="utf-8")
    result = harness.verify_evidence(evidence_id)
    assert result["valid"] is False
    assert result["status"] == "mismatch"
    assert any(f["code"] == "EVIDENCE_HASH_MISMATCH" for f in harness.doctor())


def test_missing_evidence_is_detected(harness: Harness) -> None:
    evidence_id = make_evidence(harness)
    data = harness.evidence_data(evidence_id)
    (harness.root / data["storage_uri"]).unlink()
    result = harness.verify_evidence(evidence_id)
    assert result["status"] == "missing"


def test_supported_conclusion_requires_evidence(harness: Harness) -> None:
    conclusion = harness.create_conclusion("A claim")
    with pytest.raises(HarnessError):
        harness.support_conclusion(conclusion.id, [])


def test_conclusion_support_refute_and_relations(harness: Harness) -> None:
    support = make_evidence(harness, "support.txt")
    refute = make_evidence(harness, "refute.txt")
    conclusion = harness.create_conclusion(
        "Cache helps",
        scope={"dataset": "D1"},
        falsification_criteria="Repeat without improvement",
    )
    supported = harness.support_conclusion(conclusion.id, [support], "Observed improvement")
    assert supported.status == "supported"
    refuted = harness.refute_conclusion(conclusion.id, [refute], "Larger sample contradicted it")
    assert refuted.status == "refuted"
    with session_scope(harness.root) as session:
        relations = session.scalars(
            select(Relation).where(Relation.source_id == conclusion.id).order_by(Relation.created_at)
        ).all()
        assert [r.relation_type for r in relations] == ["supports", "refutes"]


def test_invalid_conclusion_transition(harness: Harness) -> None:
    evidence = make_evidence(harness)
    conclusion = harness.create_conclusion("Claim")
    harness.refute_conclusion(conclusion.id, [evidence])
    with pytest.raises(StateTransitionError):
        harness.support_conclusion(conclusion.id, [evidence])


def test_supersede_conclusion(harness: Harness) -> None:
    evidence = make_evidence(harness)
    old = harness.create_conclusion("Broad claim")
    harness.support_conclusion(old.id, [evidence])
    new = harness.create_conclusion("Narrower claim")
    result = harness.supersede_conclusion(old.id, new.id, "More precise")
    assert result.status == "superseded"
    assert result.superseded_by == new.id


def test_conclusion_cannot_supersede_itself(harness: Harness) -> None:
    evidence = make_evidence(harness)
    conclusion = harness.create_conclusion("Claim")
    harness.support_conclusion(conclusion.id, [evidence])
    with pytest.raises(HarnessError):
        harness.supersede_conclusion(conclusion.id, conclusion.id)
