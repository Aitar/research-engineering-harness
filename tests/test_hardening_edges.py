from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from reharness.db import session_scope
from reharness.execution import run_streamed
from reharness.provenance import verify_provenance
from reharness.services import Harness, HarnessError, NotFoundError


def test_idempotency_replays_supported_create_operations(harness: Harness) -> None:
    first_conclusion = harness.create_conclusion(
        "Idempotent conclusion", idempotency_key="conclusion-request"
    )
    replayed_conclusion = harness.create_conclusion(
        "Idempotent conclusion", idempotency_key="conclusion-request"
    )
    assert replayed_conclusion.id == first_conclusion.id

    first_requirement = harness.create_requirement(
        "Idempotent requirement", idempotency_key="requirement-request"
    )
    replayed_requirement = harness.create_requirement(
        "Idempotent requirement", idempotency_key="requirement-request"
    )
    assert replayed_requirement.id == first_requirement.id

    first_spec = harness.define_test(
        "Idempotent test",
        "unit",
        [sys.executable, "-c", "pass"],
        idempotency_key="test-spec-request",
    )
    replayed_spec = harness.define_test(
        "Idempotent test",
        "unit",
        [sys.executable, "-c", "pass"],
        idempotency_key="test-spec-request",
    )
    assert replayed_spec.id == first_spec.id


def test_idempotency_key_validation_and_environment_default(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(HarnessError, match="1 to 255"):
        harness.start_task("research", "blank key", idempotency_key="   ")
    with pytest.raises(HarnessError, match="1 to 255"):
        harness.start_task("research", "long key", idempotency_key="x" * 256)

    monkeypatch.setenv("REHARNESS_IDEMPOTENCY_KEY", "environment-request")
    first = harness.start_task("research", "environment key")
    replay = harness.start_task("research", "environment key")
    assert replay.id == first.id


def test_idempotent_command_replays_original_response(harness: Harness) -> None:
    task = harness.start_task("testing", "Replay command result")
    first = harness.run_command(
        task.id,
        [sys.executable, "-c", "print('once')"],
        idempotency_key="command-request",
    )
    replay = harness.run_command(
        task.id,
        [sys.executable, "-c", "print('once')"],
        idempotency_key="command-request",
    )
    assert replay == first
    assert replay["stdout"] == "once\n"


def test_ci_trust_rejects_invalid_keys_and_missing_revocation(harness: Harness) -> None:
    with pytest.raises(HarnessError, match="public key"):
        harness.trust_ci_provider("provider", "key", "not a PEM key")
    with pytest.raises(NotFoundError, match="not found"):
        harness.revoke_ci_provider("provider", "missing")


def test_doctor_reports_stale_idempotency_reservation(harness: Harness) -> None:
    task = harness.start_task("testing", "Create project identity")
    assert task.id
    with session_scope(harness.root) as session:
        project = harness._project(session)
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        session.execute(
            text(
                "INSERT INTO idempotency_requests(project_id,operation,request_key,request_hash,"
                "status,created_at,updated_at) VALUES "
                "(:project,'command.run','stale-request','hash','in_progress',:old,:old)"
            ),
            {"project": project.id, "old": old},
        )
    findings = harness.doctor()
    stale = next(item for item in findings if item["code"] == "STALE_IDEMPOTENCY_REQUEST")
    assert stale["entity"] == "command.run:stale-request"


def test_stream_executor_validates_inputs_and_records_launch_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        run_streamed([], cwd=tmp_path, output_dir=tmp_path / "empty")
    with pytest.raises(ValueError, match="negative"):
        run_streamed(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            output_dir=tmp_path / "negative",
            max_output_bytes=-1,
        )
    result = run_streamed(
        ["missing-executable-for-hardening-test"],
        cwd=tmp_path,
        output_dir=tmp_path / "missing",
    )
    assert result.returncode == 127
    assert result.error_kind == "execution_error"
    assert "missing-executable" in result.stderr.tail
    assert result.stdout.path.read_bytes() == b""


def test_provenance_parser_rejects_invalid_documents(tmp_path: Path) -> None:
    provenance = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sig"
    signature.write_text("not-base64", encoding="utf-8")

    provenance.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid CI provenance JSON"):
        verify_provenance(provenance, signature, "not-a-key")

    provenance.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        verify_provenance(provenance, signature, "not-a-key")

    provenance.write_text(json.dumps({"provider": "ci"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        verify_provenance(provenance, signature, "not-a-key")
