from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

import yaml
from sqlalchemy import select, text

from .db import HARNESS_DIR, make_engine, session_scope
from .execution import ProcessResult, run_streamed
from .migrations import LATEST_SCHEMA_VERSION, schema_status, upgrade_engine
from .models import Conclusion, Evidence, Project, Requirement, Task, TestRun, TestSpec
from .provenance import load_public_key
from .services import (
    PROJECT_CONFIG,
    Harness as BaseHarness,
    HarnessError,
    NotFoundError,
    discover_root,
)
from .test_provenance import TestProvenanceMixin
from .utils import json_dumps, json_loads, new_id, now_utc, sha256_bytes

T = TypeVar("T")
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
TAIL_BYTES = 64 * 1024
_MODELS: dict[str, type[Any]] = {
    "task": Task,
    "conclusion": Conclusion,
    "requirement": Requirement,
    "test_spec": TestSpec,
    "test_run": TestRun,
}


def _hash(value: Any) -> str:
    return sha256_bytes(json_dumps(value).encode())


def _process_data(result: ProcessResult) -> dict[str, Any]:
    def stream(item: Any) -> dict[str, Any]:
        return {
            "bytes_seen": item.bytes_seen,
            "bytes_stored": item.bytes_stored,
            "truncated": item.truncated,
            "tail": item.tail,
        }

    return {
        "exit_code": result.returncode,
        "duration_seconds": result.duration_seconds,
        "error_kind": result.error_kind,
        "timed_out": result.timed_out,
        "process_tree_terminated": result.process_tree_terminated,
        "stdout": stream(result.stdout),
        "stderr": stream(result.stderr),
    }


class HardenedHarness(TestProvenanceMixin, BaseHarness):
    @classmethod
    def open(cls, root: Path | None = None) -> "HardenedHarness":
        resolved = discover_root(root)
        engine = make_engine(resolved)
        try:
            upgrade_engine(engine)
        finally:
            engine.dispose()
        return cls(resolved)

    @classmethod
    def initialize(
        cls,
        root: Path,
        name: str,
        description: str = "",
        repository_uri: str | None = None,
    ) -> "HardenedHarness":
        harness = super().initialize(root, name, description, repository_uri)
        harness._write_schema_version()
        return harness

    def _write_schema_version(self) -> None:
        path = self.root / HARNESS_DIR / PROJECT_CONFIG
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config["schema_version"] = LATEST_SCHEMA_VERSION
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def migrate(self) -> dict[str, Any]:
        engine = make_engine(self.root)
        try:
            applied = upgrade_engine(engine)
            result = schema_status(engine)
        finally:
            engine.dispose()
        self._write_schema_version()
        return {"applied_versions": applied, **result}

    def migration_status(self) -> dict[str, Any]:
        engine = make_engine(self.root)
        try:
            return schema_status(engine)
        finally:
            engine.dispose()

    @staticmethod
    def _key(value: str | None) -> str | None:
        key = value or os.environ.get("REHARNESS_IDEMPOTENCY_KEY")
        if key is None:
            return None
        key = key.strip()
        if not key or len(key) > 255:
            raise HarnessError("Idempotency key must contain 1 to 255 characters.")
        return key

    def _reserve(self, operation: str, key: str, payload: Any) -> dict[str, Any] | None:
        digest = _hash(payload)
        with session_scope(self.root) as session:
            project = self._project(session)
            params = {"project": project.id, "operation": operation, "key": key}
            row = session.execute(
                text(
                    "SELECT request_hash,status,entity_type,entity_id,response_json "
                    "FROM idempotency_requests WHERE project_id=:project "
                    "AND operation=:operation AND request_key=:key"
                ),
                params,
            ).mappings().one_or_none()
            if row:
                if row["request_hash"] != digest:
                    raise HarnessError(
                        "Idempotency key was already used with a different request payload."
                    )
                if row["status"] == "completed":
                    return dict(row)
                if row["status"] == "in_progress":
                    raise HarnessError("Idempotent request is already in progress.")
                session.execute(
                    text(
                        "UPDATE idempotency_requests SET status='in_progress',error=NULL,"
                        "updated_at=:now WHERE project_id=:project AND operation=:operation "
                        "AND request_key=:key"
                    ),
                    {**params, "now": now_utc().isoformat()},
                )
                return None
            now = now_utc().isoformat()
            session.execute(
                text(
                    "INSERT INTO idempotency_requests(project_id,operation,request_key,"
                    "request_hash,status,created_at,updated_at) VALUES "
                    "(:project,:operation,:key,:digest,'in_progress',:now,:now)"
                ),
                {**params, "digest": digest, "now": now},
            )
        return None

    def _finish(
        self,
        operation: str,
        key: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        response: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        status = "failed" if error else "completed"
        with session_scope(self.root) as session:
            project = self._project(session)
            session.execute(
                text(
                    "UPDATE idempotency_requests SET status=:status,entity_type=:etype,"
                    "entity_id=:eid,response_json=:response,error=:error,updated_at=:now "
                    "WHERE project_id=:project AND operation=:operation AND request_key=:key"
                ),
                {
                    "status": status,
                    "etype": entity_type,
                    "eid": entity_id,
                    "response": json_dumps(response) if response is not None else None,
                    "error": str(error)[:4000] if error else None,
                    "now": now_utc().isoformat(),
                    "project": project.id,
                    "operation": operation,
                    "key": key,
                },
            )

    def _idem_entity(
        self,
        operation: str,
        payload: Any,
        entity_type: str,
        key_value: str | None,
        call: Callable[[], T],
    ) -> T:
        key = self._key(key_value)
        if key is None:
            return call()
        stored = self._reserve(operation, key, payload)
        if stored:
            model = _MODELS[entity_type]
            with session_scope(self.root, write=False) as session:
                entity = session.get(model, stored["entity_id"])
                if entity is None:
                    raise HarnessError("Stored idempotency result is missing.")
                return entity
        try:
            result = call()
        except BaseException as exc:
            self._finish(operation, key, error=exc)
            raise
        self._finish(
            operation,
            key,
            entity_type=entity_type,
            entity_id=str(getattr(result, "id")),
        )
        return result

    def _idem_value(
        self,
        operation: str,
        payload: Any,
        key_value: str | None,
        call: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        key = self._key(key_value)
        if key is None:
            return call()
        stored = self._reserve(operation, key, payload)
        if stored:
            value = json_loads(stored["response_json"], None)
            if not isinstance(value, dict):
                raise HarnessError("Stored idempotency response is invalid.")
            return value
        try:
            result = call()
        except BaseException as exc:
            self._finish(operation, key, error=exc)
            raise
        self._finish(operation, key, response=result)
        return result

    def start_task(
        self,
        task_type: str,
        goal: str,
        success_criteria: Iterable[str] = (),
        constraints: Iterable[str] = (),
        requirement_ids: Iterable[str] = (),
        idempotency_key: str | None = None,
    ) -> Task:
        criteria, limits, requirements = (
            list(success_criteria),
            list(constraints),
            list(requirement_ids),
        )
        payload = [task_type, goal, criteria, limits, requirements]
        return self._idem_entity(
            "task.start",
            payload,
            "task",
            idempotency_key,
            lambda: super(HardenedHarness, self).start_task(
                task_type, goal, criteria, limits, requirements
            ),
        )

    def create_conclusion(
        self,
        claim: str,
        scope: dict[str, Any] | None = None,
        falsification_criteria: str = "",
        details: str = "",
        confidence: str | None = None,
        idempotency_key: str | None = None,
    ) -> Conclusion:
        payload = [claim, scope or {}, falsification_criteria, details, confidence]
        return self._idem_entity(
            "conclusion.create",
            payload,
            "conclusion",
            idempotency_key,
            lambda: super(HardenedHarness, self).create_conclusion(
                claim, scope, falsification_criteria, details, confidence
            ),
        )

    def create_requirement(
        self,
        description: str,
        acceptance_criteria: Iterable[str] = (),
        constraints: Iterable[str] = (),
        priority: str = "medium",
        idempotency_key: str | None = None,
    ) -> Requirement:
        criteria, limits = list(acceptance_criteria), list(constraints)
        return self._idem_entity(
            "requirement.create",
            [description, criteria, limits, priority],
            "requirement",
            idempotency_key,
            lambda: super(HardenedHarness, self).create_requirement(
                description, criteria, limits, priority
            ),
        )

    def define_test(
        self,
        name: str,
        test_type: str,
        command: list[str],
        covers_requirements: Iterable[str] = (),
        pass_criteria: dict[str, Any] | None = None,
        environment_requirements: dict[str, Any] | None = None,
        data_requirements: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> TestSpec:
        requirements = list(covers_requirements)
        payload = [
            name,
            test_type,
            command,
            requirements,
            pass_criteria or {},
            environment_requirements or {},
            data_requirements or {},
        ]
        return self._idem_entity(
            "test.define",
            payload,
            "test_spec",
            idempotency_key,
            lambda: super(HardenedHarness, self).define_test(
                name,
                test_type,
                command,
                requirements,
                pass_criteria,
                environment_requirements,
                data_requirements,
            ),
        )

    def trust_ci_provider(self, provider: str, key_id: str, public_key_pem: str) -> None:
        provider, key_id = provider.strip(), key_id.strip()
        if not provider or not key_id:
            raise HarnessError("Provider and key ID are required.")
        try:
            load_public_key(public_key_pem)
        except ValueError as exc:
            raise HarnessError(str(exc)) from exc
        with session_scope(self.root) as session:
            project = self._project(session)
            now = now_utc().isoformat()
            session.execute(
                text(
                    "INSERT INTO ci_trust_roots(project_id,provider,key_id,algorithm,"
                    "public_key_pem,enabled,created_at,updated_at) VALUES "
                    "(:project,:provider,:key,'ed25519',:pem,1,:now,:now) "
                    "ON CONFLICT(project_id,provider,key_id) DO UPDATE SET "
                    "public_key_pem=excluded.public_key_pem,enabled=1,updated_at=excluded.updated_at"
                ),
                {
                    "project": project.id,
                    "provider": provider,
                    "key": key_id,
                    "pem": public_key_pem,
                    "now": now,
                },
            )

    def revoke_ci_provider(self, provider: str, key_id: str) -> None:
        with session_scope(self.root) as session:
            project = self._project(session)
            result = session.execute(
                text(
                    "UPDATE ci_trust_roots SET enabled=0,updated_at=:now WHERE "
                    "project_id=:project AND provider=:provider AND key_id=:key"
                ),
                {
                    "now": now_utc().isoformat(),
                    "project": project.id,
                    "provider": provider,
                    "key": key_id,
                },
            )
            if result.rowcount == 0:
                raise NotFoundError(f"CI trust root not found: {provider}:{key_id}")

    def _trusted_key(self, session: Any, project: str, provider: str, key_id: str) -> str:
        pem = session.execute(
            text(
                "SELECT public_key_pem FROM ci_trust_roots WHERE project_id=:project "
                "AND provider=:provider AND key_id=:key AND enabled=1"
            ),
            {"project": project, "provider": provider, "key": key_id},
        ).scalar_one_or_none()
        if pem is None:
            raise HarnessError(f"Untrusted CI provider key: {provider}:{key_id}")
        return str(pem)

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
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        tail_bytes: int = TAIL_BYTES,
        termination_grace_seconds: float = 2.0,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captures = list(capture_paths)
        payload = [
            task_id,
            command,
            [str(path) for path in captures],
            timeout,
            env or {},
            dataset_manifest_hash,
            model_hash,
            weight_hash,
            prompt_hash,
            random_seed,
            max_output_bytes,
        ]
        return self._idem_value(
            "command.run",
            payload,
            idempotency_key,
            lambda: self._run_command(
                task_id,
                command,
                captures,
                timeout,
                env,
                dataset_manifest_hash,
                model_hash,
                weight_hash,
                prompt_hash,
                random_seed,
                max_output_bytes,
                tail_bytes,
                termination_grace_seconds,
            ),
        )

    def _run_command(
        self,
        task_id: str,
        command: list[str],
        captures: list[Path],
        timeout: float | None,
        env: dict[str, str] | None,
        dataset_hash: str | None,
        model_hash: str | None,
        weight_hash: str | None,
        prompt_hash: str | None,
        seed: str | None,
        max_bytes: int,
        tail_bytes: int,
        grace: float,
    ) -> dict[str, Any]:
        if not command:
            raise HarnessError("Command cannot be empty.")
        with session_scope(self.root) as session:
            project = self._project(session)
            task = self._active_task(session, task_id)
            snapshot = self.create_snapshot(
                session,
                project,
                dataset_manifest_hash=dataset_hash,
                model_hash=model_hash,
                weight_hash=weight_hash,
                prompt_hash=prompt_hash,
                random_seed=seed,
                task_id=task.id,
            )
            started = now_utc()
            event = self._add_task_event(
                session,
                task,
                "command_started",
                f"Started command: {' '.join(command)}",
                {"command": command, "snapshot_id": snapshot.id},
            )
            snapshot_id, event_id = snapshot.id, event.id
        directory = self.root / HARNESS_DIR / "tmp" / "executions" / new_id("artifact")
        process_env = os.environ.copy()
        process_env.update(env or {})
        result = run_streamed(
            command,
            cwd=self.root,
            output_dir=directory,
            timeout=timeout,
            env=process_env,
            max_output_bytes=max_bytes,
            tail_bytes=tail_bytes,
            termination_grace_seconds=grace,
        )
        try:
            with session_scope(self.root) as session:
                project = self._project(session)
                task = self._active_task(session, task_id)
                streams = []
                for name, item in (("stdout", result.stdout), ("stderr", result.stderr)):
                    streams.append(
                        self._capture_file_in_session(
                            session,
                            project,
                            item.path,
                            "command_log",
                            task.id,
                            {
                                "stream": name,
                                "bytes_seen": item.bytes_seen,
                                "truncated": item.truncated,
                            },
                        )
                    )
                report = {
                    "command": command,
                    "started_at": started.isoformat(),
                    "finished_at": now_utc().isoformat(),
                    "snapshot_id": snapshot_id,
                    "started_event_id": event_id,
                    "execution": _process_data(result),
                    "stream_evidence_ids": [item.id for item in streams],
                }
                evidence = self._capture_text_in_session(
                    session,
                    project,
                    json_dumps(report),
                    "command-log.json",
                    "command_log",
                    task.id,
                    {"snapshot_id": snapshot_id, "exit_code": result.returncode},
                )
                for item in streams:
                    self._add_relation(
                        session, "evidence", evidence.id, "produces", "evidence", item.id
                    )
                event_type = "command_completed" if result.returncode == 0 else "command_failed"
                ended = self._add_task_event(
                    session,
                    task,
                    event_type,
                    f"Command exited with code {result.returncode}: {' '.join(command)}",
                    {
                        "exit_code": result.returncode,
                        "snapshot_id": snapshot_id,
                        "started_event_id": event_id,
                        "error_kind": result.error_kind,
                        "process_tree_terminated": result.process_tree_terminated,
                    },
                    evidence.id,
                )
                evidence.task_event_id = ended.id
                captured: list[str] = []
                for path in captures:
                    resolved = path if path.is_absolute() else self.root / path
                    matches = (
                        list(resolved.parent.glob(resolved.name))
                        if any(char in resolved.name for char in "*?[")
                        else [resolved]
                    )
                    for match in matches:
                        if match.is_file():
                            extra = self._capture_file_in_session(
                                session,
                                project,
                                match,
                                "experiment_result",
                                task.id,
                                {"produced_by": evidence.id, "snapshot_id": snapshot_id},
                            )
                            captured.append(extra.id)
                            self._add_relation(
                                session,
                                "evidence",
                                evidence.id,
                                "produces",
                                "evidence",
                                extra.id,
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
                    "snapshot_id": snapshot_id,
                    "evidence_id": evidence.id,
                    "exit_code": result.returncode,
                    "stdout": result.stdout.tail,
                    "stderr": result.stderr.tail,
                    "stdout_truncated": result.stdout.truncated,
                    "stderr_truncated": result.stderr.truncated,
                    "process_tree_terminated": result.process_tree_terminated,
                    "captured_evidence_ids": captured,
                }
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def doctor(self) -> list[dict[str, str]]:
        findings = super().doctor()
        status = self.migration_status()
        if status["current_version"] != status["latest_version"]:
            findings.append(
                {
                    "severity": "error",
                    "code": "SCHEMA_MIGRATION_PENDING",
                    "entity": "database",
                    "message": "Database schema migration is pending.",
                }
            )
        with session_scope(self.root, write=False) as session:
            project = self._project(session)
            for run in session.scalars(
                select(TestRun).where(TestRun.build_id.is_not(None), TestRun.status == "passed")
            ).all():
                evidence = session.get(Evidence, run.evidence_id) if run.evidence_id else None
                if evidence is None:
                    continue
                try:
                    usage = self._read_test_report(evidence).get("build_usage")
                except HarnessError:
                    continue
                if not isinstance(usage, dict) or usage.get("verified") is not True:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "TEST_BUILD_USAGE_UNPROVEN",
                            "entity": run.id,
                            "message": "Passed build-bound test lacks a Build usage proof.",
                        }
                    )
            cutoff = datetime.now(UTC).timestamp() - 900
            rows = session.execute(
                text(
                    "SELECT operation,request_key,updated_at FROM idempotency_requests "
                    "WHERE project_id=:project AND status='in_progress'"
                ),
                {"project": project.id},
            ).mappings()
            for row in rows:
                try:
                    stale = datetime.fromisoformat(str(row["updated_at"])).timestamp() < cutoff
                except ValueError:
                    stale = True
                if stale:
                    findings.append(
                        {
                            "severity": "warning",
                            "code": "STALE_IDEMPOTENCY_REQUEST",
                            "entity": f"{row['operation']}:{row['request_key']}",
                            "message": "Request has remained in progress for over 15 minutes.",
                        }
                    )
        return findings
