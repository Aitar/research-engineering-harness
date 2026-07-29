from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from reharness.db import make_engine
from reharness.provenance import canonical_json_bytes, command_digest
from reharness.services import Harness, HarnessError
from reharness.utils import sha256_file


def _commit_file(root: Path, name: str, content: str) -> tuple[str, str]:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", f"add {name}"], cwd=root, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return base, head


def _implemented_requirement_and_build(harness: Harness):
    req = harness.create_requirement("Verify the exact packaged artifact", ["artifact tested"])
    harness.transition_requirement(req.id, "accepted")
    harness.transition_requirement(req.id, "in_progress")
    source_name = f"bound-{req.id}.py"
    base, head = _commit_file(
        harness.root,
        source_name,
        f"REQUIREMENT_ID = {req.id!r}\n",
    )
    change = harness.capture_change(base, head, requirement_ids=[req.id])
    harness.transition_requirement(req.id, "implemented")
    artifact = harness.root / f"bound-{req.id}.bin"
    artifact.write_bytes(f"exact-build-content:{req.id}".encode())
    build = harness.capture_build(artifact, change_id=change.id)
    return req, build


def test_existing_v1_database_is_migrated_on_open(harness: Harness) -> None:
    engine = make_engine(harness.root)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE idempotency_requests")
            connection.exec_driver_sql("DROP TABLE ci_trust_roots")
            connection.exec_driver_sql("DELETE FROM schema_migrations WHERE version > 1")
            connection.exec_driver_sql("PRAGMA user_version = 1")
    finally:
        engine.dispose()

    reopened = Harness.open(harness.root)
    status = reopened.migration_status()
    assert status["current_version"] == status["latest_version"] == 2
    engine = make_engine(harness.root)
    try:
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
    finally:
        engine.dispose()
    assert {"idempotency_requests", "ci_trust_roots"} <= tables


def test_idempotency_replays_result_and_rejects_payload_change(harness: Harness) -> None:
    first = harness.start_task("research", "Stable request", idempotency_key="request-1")
    replay = harness.start_task("research", "Stable request", idempotency_key="request-1")
    assert replay.id == first.id
    assert len(harness.list_tasks()) == 1

    with pytest.raises(HarnessError, match="different request payload"):
        harness.start_task("research", "Changed request", idempotency_key="request-1")


def test_command_output_is_bounded_and_streamed_to_evidence(harness: Harness) -> None:
    task = harness.start_task("testing", "Bound command output")
    result = harness.run_command(
        task.id,
        [sys.executable, "-c", "print('x' * 200000)"],
        max_output_bytes=1024,
        tail_bytes=128,
    )
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) <= 128
    evidence = harness.evidence_data(result["evidence_id"])
    report = json.loads((harness.root / evidence["storage_uri"]).read_text(encoding="utf-8"))
    assert report["execution"]["stdout"]["bytes_seen"] > 100000
    assert report["execution"]["stdout"]["bytes_stored"] == 1024
    assert "x" * 10000 not in json.dumps(report)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_timeout_terminates_descendant_process_tree(harness: Harness) -> None:
    task = harness.start_task("testing", "Kill descendants on timeout")
    pid_file = harness.root / "child.pid"
    program = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "open(sys.argv[1],'w').write(str(p.pid));time.sleep(30)"
    )
    result = harness.run_command(
        task.id,
        [sys.executable, "-c", program, str(pid_file)],
        timeout=1.0,
        termination_grace_seconds=0.2,
    )
    assert result["exit_code"] == 124
    assert result["process_tree_terminated"] is True
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    time.sleep(0.1)
    proc_stat = Path(f"/proc/{child_pid}/stat")
    if proc_stat.exists():
        assert proc_stat.read_text(encoding="utf-8").split()[2] == "Z"


def test_requirement_rejects_unbound_command_and_accepts_explicit_build_binding(
    harness: Harness,
) -> None:
    req, build = _implemented_requirement_and_build(harness)
    unbound = harness.define_test(
        "Unbound",
        "smoke",
        [sys.executable, "-c", "pass"],
        [req.id],
    )
    unbound_run = harness.run_test(unbound.id, build_id=build.id)
    assert unbound_run.status == "passed"
    with pytest.raises(HarnessError, match="does not prove use"):
        harness.verify_requirement(req.id, unbound_run.id)

    bound = harness.define_test(
        "Bound",
        "smoke",
        [
            sys.executable,
            "-c",
            "import pathlib,sys; assert pathlib.Path(sys.argv[1]).read_bytes()",
            "{build_artifact}",
        ],
        [req.id],
    )
    bound_run = harness.run_test(bound.id, build_id=build.id)
    assert harness.verify_requirement(req.id, bound_run.id).status == "verified"


def test_signed_ci_provenance_is_verified_and_rechecked(harness: Harness) -> None:
    req, build = _implemented_requirement_and_build(harness)
    spec = harness.define_test(
        "CI bound",
        "smoke",
        ["ci-runner", "{build_artifact}"],
        [req.id],
    )
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    harness.trust_ci_provider("github-actions", "release-key", public_pem.decode())

    junit = harness.root / "signed-junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    provenance = {
        "provider": "github-actions",
        "key_id": "release-key",
        "repository": "Aitar/research-engineering-harness",
        "workflow": "ci.yml",
        "run_id": "123",
        "job_id": "456",
        "commit_sha": build.commit_sha,
        "build_id": build.id,
        "build_artifact_sha256": build.artifact_hash,
        "report_sha256": sha256_file(junit),
        "command_sha256": command_digest(["ci-runner", "{build_artifact}"]),
        "issued_at": "2026-07-29T07:00:00Z",
        "nonce": "unique-run-123-456",
    }
    provenance_path = harness.root / "provenance.json"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    signature_path = harness.root / "provenance.sig"
    signature_path.write_bytes(
        base64.b64encode(private_key.sign(canonical_json_bytes(provenance)))
    )
    run = harness.import_junit(
        spec.id,
        junit,
        build_id=build.id,
        provenance_path=provenance_path,
        signature_path=signature_path,
    )
    assert run.status == "passed"
    assert harness.verify_requirement(req.id, run.id).status == "verified"

    req2, build2 = _implemented_requirement_and_build(harness)
    spec2 = harness.define_test("CI forged", "smoke", ["ci", "{build_artifact}"], [req2.id])
    forged = dict(provenance)
    forged.update(
        {
            "build_id": build2.id,
            "build_artifact_sha256": build2.artifact_hash,
            "commit_sha": build2.commit_sha,
            "command_sha256": command_digest(["ci", "{build_artifact}"]),
        }
    )
    forged_path = harness.root / "forged.json"
    forged_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(HarnessError, match="signature verification failed"):
        harness.import_junit(
            spec2.id,
            junit,
            build_id=build2.id,
            provenance_path=forged_path,
            signature_path=signature_path,
        )
