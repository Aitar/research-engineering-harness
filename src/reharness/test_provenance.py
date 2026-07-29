from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import HARNESS_DIR, session_scope
from .execution import run_streamed
from .models import Build, Change, Evidence, Project, Relation, Requirement, Task, TestRun, TestSpec
from .provenance import command_digest, verify_provenance
from .services import HarnessError, NotFoundError, StateTransitionError
from .utils import json_dumps, json_loads, new_id, now_utc, sha256_file

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
TAIL_BYTES = 64 * 1024
PLACEHOLDER = "{build_artifact}"


def _path(root: Path, evidence: Evidence) -> Path:
    value = Path(evidence.storage_uri)
    return value if value.is_absolute() else root / value


def _execution(result: Any) -> dict[str, Any]:
    return {
        "exit_code": result.returncode,
        "duration_seconds": result.duration_seconds,
        "error_kind": result.error_kind,
        "timed_out": result.timed_out,
        "process_tree_terminated": result.process_tree_terminated,
        "stdout": {
            "bytes_seen": result.stdout.bytes_seen,
            "bytes_stored": result.stdout.bytes_stored,
            "truncated": result.stdout.truncated,
            "tail": result.stdout.tail,
        },
        "stderr": {
            "bytes_seen": result.stderr.bytes_seen,
            "bytes_stored": result.stderr.bytes_stored,
            "truncated": result.stderr.truncated,
            "tail": result.stderr.tail,
        },
    }


class TestProvenanceMixin:
    def _build_evidence(self, session: Any, build: Build) -> Evidence:
        relation = session.scalar(
            select(Relation).where(
                Relation.source_type == "build",
                Relation.source_id == build.id,
                Relation.relation_type == "produces",
                Relation.target_type == "evidence",
            )
        )
        evidence = session.get(Evidence, relation.target_id) if relation else None
        if evidence is None:
            raise HarnessError("Build artifact evidence is missing.")
        self._assert_evidence_integrity(evidence)
        if evidence.sha256 != build.artifact_hash:
            raise HarnessError("Build artifact evidence does not match the Build hash.")
        return evidence

    def run_test(
        self,
        test_spec_id: str,
        task_id: str | None = None,
        build_id: str | None = None,
        timeout: float | None = None,
        junit_path: Path | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        tail_bytes: int = TAIL_BYTES,
        termination_grace_seconds: float = 2.0,
        idempotency_key: str | None = None,
    ) -> TestRun:
        payload = [
            test_spec_id,
            task_id,
            build_id,
            timeout,
            str(junit_path) if junit_path else None,
            max_output_bytes,
        ]
        return self._idem_entity(
            "test.run",
            payload,
            "test_run",
            idempotency_key,
            lambda: self._run_test(
                test_spec_id,
                task_id,
                build_id,
                timeout,
                junit_path,
                max_output_bytes,
                tail_bytes,
                termination_grace_seconds,
            ),
        )

    def _run_test(
        self,
        test_spec_id: str,
        task_id: str | None,
        build_id: str | None,
        timeout: float | None,
        junit_path: Path | None,
        max_bytes: int,
        tail_bytes: int,
        grace: float,
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
            artifact = self._build_evidence(session, build) if build else None
            snapshot = self.create_snapshot(session, project, task_id=task_id)
            template = list(json_loads(spec.command_json, []))
            criteria = self._validated_pass_criteria(json_loads(spec.pass_criteria_json, {}))
            started = now_utc()
            if task_id:
                task = self._active_task(session, task_id)
                self._add_task_event(
                    session,
                    task,
                    "test_started",
                    f"Started test {spec.id}",
                    {"build_id": build_id},
                )
            state = {
                "project_id": project.id,
                "snapshot_id": snapshot.id,
                "snapshot_commit": snapshot.git_commit,
                "template": template,
                "criteria": criteria,
                "build_id": build.id if build else None,
                "build_hash": build.artifact_hash if build else None,
                "build_commit": build.commit_sha if build else snapshot.git_commit,
                "artifact": str(_path(self.root, artifact)) if artifact else None,
                "artifact_name": Path(artifact.storage_uri).name if artifact else None,
            }

        directory = self.root / HARNESS_DIR / "tmp" / "executions" / new_id("artifact")
        directory.mkdir(parents=True, exist_ok=True)
        command = list(template)
        env = os.environ.copy()
        usage: dict[str, Any] = {
            "verified": build_id is None,
            "mode": "not_applicable" if build_id is None else "unproven",
            "build_id": build_id,
        }
        staged = None
        if state["artifact"]:
            staged_dir = directory / "build"
            staged_dir.mkdir()
            staged = staged_dir / f"{state['build_hash']}-{state['artifact_name']}"
            shutil.copy2(Path(state["artifact"]), staged)
            staged.chmod(0o444)
            before = sha256_file(staged)
            used = any(PLACEHOLDER in item for item in template)
            command = [item.replace(PLACEHOLDER, str(staged)) for item in template]
            env["REHARNESS_BUILD_ARTIFACT"] = str(staged)
            env["REHARNESS_BUILD_SHA256"] = str(state["build_hash"])
            usage = {
                "verified": False,
                "mode": "artifact_placeholder" if used else "environment_only",
                "build_id": build_id,
                "build_artifact_sha256": state["build_hash"],
                "placeholder_used": used,
                "pre_execution_sha256": before,
            }
        result = run_streamed(
            command,
            cwd=self.root,
            output_dir=directory / "streams",
            timeout=timeout,
            env=env,
            max_output_bytes=max_bytes,
            tail_bytes=tail_bytes,
            termination_grace_seconds=grace,
        )
        if staged:
            after = sha256_file(staged)
            usage["post_execution_sha256"] = after
            usage["verified"] = bool(
                usage["placeholder_used"]
                and usage["pre_execution_sha256"] == usage["build_artifact_sha256"] == after
            )
        error = result.error_kind
        if staged and usage["placeholder_used"] and not usage["verified"]:
            error = "build_binding_integrity_failure"
        counts = {
            "total": 1,
            "passed": int(result.returncode == 0),
            "failures": int(result.returncode != 0),
            "errors": 0,
            "skipped": 0,
        }
        junit = None
        if junit_path:
            junit = junit_path if junit_path.is_absolute() else self.root / junit_path
            if not junit.exists():
                shutil.rmtree(directory, ignore_errors=True)
                raise HarnessError(f"JUnit report not found: {junit}")
            counts = self._parse_junit(junit)
        status, checks = self._evaluate_test_result(
            state["criteria"], result.returncode, counts, error
        )
        finished = now_utc()
        try:
            with session_scope(self.root) as session:
                project = session.get(Project, state["project_id"])
                spec = session.get(TestSpec, test_spec_id)
                assert project is not None and spec is not None
                if task_id:
                    self._active_task(session, task_id)
                streams = []
                for name, item in (("stdout", result.stdout), ("stderr", result.stderr)):
                    streams.append(
                        self._capture_file_in_session(
                            session,
                            project,
                            item.path,
                            "test_report",
                            task_id,
                            {"stream": name, "test_spec_id": spec.id},
                        )
                    )
                report = {
                    "test_spec_id": spec.id,
                    "command_template": template,
                    "resolved_command": command,
                    "command_sha256": command_digest(template),
                    "exit_code": result.returncode,
                    "counts": counts,
                    "snapshot_id": state["snapshot_id"],
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "error_kind": error,
                    "pass_criteria": state["criteria"],
                    "criteria_evaluation": checks,
                    "execution": _execution(result),
                    "stream_evidence_ids": [item.id for item in streams],
                    "provenance_status": "local_execution",
                    "build_usage": usage,
                    "commit_sha": state["build_commit"],
                }
                evidence = self._capture_text_in_session(
                    session,
                    project,
                    json_dumps(report),
                    "test-report.json",
                    "test_report",
                    task_id,
                    {"test_spec_id": spec.id, "snapshot_id": state["snapshot_id"]},
                )
                for item in streams:
                    self._add_relation(
                        session, "evidence", evidence.id, "produces", "evidence", item.id
                    )
                if junit:
                    junit_evidence = self._capture_file_in_session(
                        session,
                        project,
                        junit,
                        "test_report",
                        task_id,
                        {"format": "junit", "test_spec_id": spec.id},
                    )
                    self._add_relation(
                        session,
                        "evidence",
                        evidence.id,
                        "produces",
                        "evidence",
                        junit_evidence.id,
                    )
                run = TestRun(
                    id=new_id("test_run"),
                    test_spec_id=spec.id,
                    task_id=task_id,
                    build_id=build_id,
                    snapshot_id=state["snapshot_id"],
                    commit_sha=state["build_commit"],
                    status=status,
                    started_at=started,
                    finished_at=finished,
                    total_count=counts["total"],
                    passed_count=counts["passed"],
                    failed_count=counts["failures"] + counts["errors"],
                    skipped_count=counts["skipped"],
                    result_summary=f"exit={result.returncode}; {counts['passed']}/{counts['total']} passed",
                    evidence_id=evidence.id,
                )
                session.add(run)
                session.flush()
                self._add_relation(session, "test_run", run.id, "produces", "evidence", evidence.id)
                if build_id:
                    self._add_relation(session, "test_run", run.id, "evaluates", "build", build_id)
                if task_id:
                    task = session.get(Task, task_id)
                    assert task is not None
                    event = self._add_task_event(
                        session,
                        task,
                        "test_completed",
                        f"Test {spec.id} completed with status {status}",
                        {"test_run_id": run.id, "build_usage_verified": usage["verified"]},
                        evidence.id,
                    )
                    evidence.task_event_id = event.id
                self._audit(
                    session,
                    project.id,
                    "test_completed",
                    "test_run",
                    run.id,
                    {"build_usage": usage},
                )
                self._schedule_render(
                    session,
                    project_id=project.id,
                    task_ids=[task_id] if task_id else [],
                    brief=True,
                )
                return run
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def import_junit(
        self,
        test_spec_id: str,
        junit_path: Path,
        task_id: str | None = None,
        build_id: str | None = None,
        provenance_path: Path | None = None,
        signature_path: Path | None = None,
        idempotency_key: str | None = None,
    ) -> TestRun:
        source = junit_path if junit_path.is_absolute() else self.root / junit_path
        payload = [
            test_spec_id,
            str(source),
            sha256_file(source) if source.exists() else None,
            task_id,
            build_id,
            str(provenance_path) if provenance_path else None,
            str(signature_path) if signature_path else None,
        ]
        return self._idem_entity(
            "test.import",
            payload,
            "test_run",
            idempotency_key,
            lambda: self._import_junit(
                test_spec_id,
                source,
                task_id,
                build_id,
                provenance_path,
                signature_path,
            ),
        )

    def _import_junit(
        self,
        test_spec_id: str,
        source: Path,
        task_id: str | None,
        build_id: str | None,
        provenance_path: Path | None,
        signature_path: Path | None,
    ) -> TestRun:
        if not source.exists():
            raise HarnessError(f"JUnit report not found: {source}")
        if (provenance_path is None) != (signature_path is None):
            raise HarnessError("CI provenance and signature must be provided together.")
        provenance = None
        provenance_file = None
        signature_file = None
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            spec = session.get(TestSpec, test_spec_id)
            if spec is None:
                raise NotFoundError(f"Test specification not found: {test_spec_id}")
            build = session.get(Build, build_id) if build_id else None
            if build_id and build is None:
                raise NotFoundError(f"Build not found: {build_id}")
            if task_id:
                self._active_task(session, task_id)
            if provenance_path and signature_path:
                provenance_file = (
                    provenance_path if provenance_path.is_absolute() else self.root / provenance_path
                )
                signature_file = (
                    signature_path if signature_path.is_absolute() else self.root / signature_path
                )
                raw = json.loads(provenance_file.read_text(encoding="utf-8"))
                key = self._trusted_key(
                    session, project.id, str(raw.get("provider")), str(raw.get("key_id"))
                )
                try:
                    provenance = verify_provenance(
                        provenance_file, signature_file, key
                    ).payload
                except ValueError as exc:
                    raise HarnessError(str(exc)) from exc
                if build is None:
                    raise HarnessError("Signed CI imports require a Build.")
                expected = {
                    "build_id": build.id,
                    "build_artifact_sha256": build.artifact_hash,
                    "commit_sha": build.commit_sha,
                    "report_sha256": sha256_file(source),
                    "command_sha256": command_digest(json_loads(spec.command_json, [])),
                }
                for name, value in expected.items():
                    if provenance.get(name) != value:
                        raise HarnessError(f"CI provenance field {name} does not match.")
            criteria = self._validated_pass_criteria(json_loads(spec.pass_criteria_json, {}))
        counts = self._parse_junit(source)
        status, checks = self._evaluate_test_result(criteria, 0, counts, None)
        now = now_utc()
        with session_scope(self.root) as session:
            project = self._project(session)
            spec = session.get(TestSpec, test_spec_id)
            assert spec is not None
            if task_id:
                self._active_task(session, task_id)
            build = session.get(Build, build_id) if build_id else None
            junit_evidence = self._capture_file_in_session(
                session,
                project,
                source,
                "test_report",
                task_id,
                {"format": "junit", "test_spec_id": spec.id},
            )
            extra: list[Evidence] = []
            provenance_status = "unsigned_import"
            usage = {"verified": False, "mode": "unsigned_import", "build_id": build_id}
            if provenance and provenance_file and signature_file:
                extra = [
                    self._capture_file_in_session(
                        session,
                        project,
                        provenance_file,
                        "ci_report",
                        task_id,
                        {"format": "provenance", "provider": provenance["provider"]},
                    ),
                    self._capture_file_in_session(
                        session,
                        project,
                        signature_file,
                        "ci_report",
                        task_id,
                        {"format": "ed25519", "key_id": provenance["key_id"]},
                    ),
                ]
                provenance_status = "provider_signed"
                usage = {
                    "verified": True,
                    "mode": "provider_attestation",
                    "build_id": provenance["build_id"],
                    "build_artifact_sha256": provenance["build_artifact_sha256"],
                    "commit_sha": provenance["commit_sha"],
                    "provider": provenance["provider"],
                    "key_id": provenance["key_id"],
                }
            report = {
                "test_spec_id": spec.id,
                "imported": True,
                "counts": counts,
                "pass_criteria": criteria,
                "criteria_evaluation": checks,
                "provenance_status": provenance_status,
                "provenance": provenance,
                "provenance_evidence_id": extra[0].id if extra else None,
                "signature_evidence_id": extra[1].id if extra else None,
                "build_usage": usage,
                "command_sha256": command_digest(json_loads(spec.command_json, [])),
            }
            evidence = self._capture_text_in_session(
                session,
                project,
                json_dumps(report),
                "import-summary.json",
                "test_report",
                task_id,
                {"test_spec_id": spec.id, "provenance_status": provenance_status},
            )
            for item in [junit_evidence, *extra]:
                self._add_relation(
                    session, "evidence", evidence.id, "produces", "evidence", item.id
                )
            run = TestRun(
                id=new_id("test_run"),
                test_spec_id=spec.id,
                task_id=task_id,
                build_id=build_id,
                snapshot_id=None,
                commit_sha=build.commit_sha if build else None,
                status=status,
                started_at=now,
                finished_at=now,
                total_count=counts["total"],
                passed_count=counts["passed"],
                failed_count=counts["failures"] + counts["errors"],
                skipped_count=counts["skipped"],
                result_summary=f"imported JUnit; {counts['passed']}/{counts['total']} passed",
                evidence_id=evidence.id,
            )
            session.add(run)
            session.flush()
            self._add_relation(session, "test_run", run.id, "produces", "evidence", evidence.id)
            if build:
                self._add_relation(session, "test_run", run.id, "evaluates", "build", build.id)
            if task_id:
                task = session.get(Task, task_id)
                assert task is not None
                event = self._add_task_event(
                    session,
                    task,
                    "test_completed",
                    f"Imported test {spec.id} with status {status}",
                    {"test_run_id": run.id, "provenance_status": provenance_status},
                    evidence.id,
                )
                evidence.task_event_id = event.id
            self._audit(
                session,
                project.id,
                "test_imported",
                "test_run",
                run.id,
                {"provenance_status": provenance_status, "build_usage": usage},
            )
            self._schedule_render(
                session,
                project_id=project.id,
                task_ids=[task_id] if task_id else [],
                brief=True,
            )
            return run

    def _read_test_report(self, evidence: Evidence) -> dict[str, Any]:
        path = self._assert_evidence_integrity(evidence)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(f"Invalid formal test report: {exc}") from exc
        if not isinstance(value, dict):
            raise HarnessError("Formal test report must be a JSON object.")
        return value

    def _reverify_ci(
        self,
        session: Any,
        project_id: str,
        report: dict[str, Any],
        build: Build,
        spec: TestSpec,
    ) -> None:
        payload = report.get("provenance")
        if not isinstance(payload, dict):
            raise HarnessError("Signed CI test report has no provenance payload.")
        provenance = session.get(Evidence, report.get("provenance_evidence_id"))
        signature = session.get(Evidence, report.get("signature_evidence_id"))
        if provenance is None or signature is None:
            raise HarnessError("Signed CI provenance evidence is missing.")
        key = self._trusted_key(
            session, project_id, str(payload.get("provider")), str(payload.get("key_id"))
        )
        try:
            verified = verify_provenance(
                self._assert_evidence_integrity(provenance),
                self._assert_evidence_integrity(signature),
                key,
            ).payload
        except ValueError as exc:
            raise HarnessError(str(exc)) from exc
        expected = {
            "build_id": build.id,
            "build_artifact_sha256": build.artifact_hash,
            "commit_sha": build.commit_sha,
            "command_sha256": command_digest(json_loads(spec.command_json, [])),
        }
        for name, value in expected.items():
            if verified.get(name) != value:
                raise HarnessError(f"Stored CI provenance field {name} no longer matches.")

    def verify_requirement(self, requirement_id: str, test_run_id: str) -> Requirement:
        with session_scope(self.root) as session:
            project = self._project(session)
            requirement = session.get(Requirement, requirement_id)
            if requirement is None:
                raise NotFoundError(f"Requirement not found: {requirement_id}")
            if requirement.status != "implemented":
                raise StateTransitionError("Requirement must be implemented before verification.")
            run = session.get(TestRun, test_run_id)
            if run is None:
                raise NotFoundError(f"Test run not found: {test_run_id}")
            if run.status != "passed":
                raise HarnessError("Only a passed test run can verify a requirement.")
            evidence = session.get(Evidence, run.evidence_id) if run.evidence_id else None
            if evidence is None:
                raise HarnessError("The test run report evidence is missing.")
            report = self._read_test_report(evidence)
            spec = session.get(TestSpec, run.test_spec_id)
            assert spec is not None
            if requirement_id not in json_loads(spec.covers_requirements_json, []):
                raise HarnessError("The test specification does not cover this requirement.")
            if run.build_id is None:
                raise HarnessError("Requirement verification requires a test run bound to a Build.")
            build = session.get(Build, run.build_id)
            if build is None:
                raise HarnessError("The test run references a missing Build.")
            if build.status != "succeeded":
                raise HarnessError("Only a succeeded Build can verify a requirement.")
            usage = report.get("build_usage")
            if not isinstance(usage, dict) or usage.get("verified") is not True:
                raise HarnessError(
                    "The test run does not prove use of the declared Build artifact. "
                    "Use {build_artifact} or provider-signed CI provenance."
                )
            if usage.get("build_id") != build.id:
                raise HarnessError("Test Build proof references a different Build.")
            if usage.get("build_artifact_sha256") != build.artifact_hash:
                raise HarnessError("Test Build proof has the wrong artifact hash.")
            source = report.get("provenance_status")
            if source == "provider_signed":
                self._reverify_ci(session, project.id, report, build, spec)
            elif source == "local_execution":
                if usage.get("mode") != "artifact_placeholder":
                    raise HarnessError(
                        "Local Build verification requires explicit {build_artifact} binding."
                    )
            else:
                raise HarnessError("Unsigned imported reports cannot verify a Requirement.")
            if build.change_id is None:
                raise HarnessError("The verified Build is not linked to a Change.")
            change = session.get(Change, build.change_id)
            if change is None:
                raise HarnessError("The verified Build references a missing Change.")
            if run.commit_sha != build.commit_sha:
                raise HarnessError("The test run commit does not match its Build commit.")
            relation = session.scalar(
                select(Relation).where(
                    Relation.source_type == "change",
                    Relation.source_id == change.id,
                    Relation.relation_type == "implements",
                    Relation.target_type == "requirement",
                    Relation.target_id == requirement.id,
                )
            )
            if relation is None:
                raise HarnessError("The tested Build does not implement this requirement.")
            self._build_evidence(session, build)
            requirement.status = "verified"
            requirement.updated_at = now_utc()
            self._add_relation(
                session, "test_run", run.id, "verifies", "requirement", requirement.id
            )
            self._audit(
                session,
                project.id,
                "requirement_verified",
                "requirement",
                requirement.id,
                {"test_run_id": run.id, "build_usage": usage},
            )
            self._schedule_render(
                session, project_id=project.id, requirement_ids=[requirement.id], brief=True
            )
            return requirement
