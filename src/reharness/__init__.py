"""RE Harness: an AI-native research and engineering ledger."""

from typing import Any

from sqlalchemy import select

from . import services as _services
from .db import session_scope
from .hardening import HardenedHarness
from .models import Evidence, Relation, TestRun
from .services import HarnessError
from .utils import json_loads


class Harness(HardenedHarness):
    """Public hardened service with compatibility-normalized read models."""

    def task_data(self, task_id: str) -> dict[str, Any]:
        data = super().task_data(task_id)
        for event in data["events"]:
            if event["type"] != "test_completed":
                continue
            payload = event["payload"]
            if "status" in payload or not payload.get("test_run_id"):
                continue
            payload["status"] = self.test_run_data(payload["test_run_id"])["status"]
        return data

    def verify_requirement(self, requirement_id: str, test_run_id: str):
        # Signed provenance binds the original JUnit digest. Recheck the captured
        # JUnit Evidence at the formal transition boundary, not only at import time.
        with session_scope(self.root, write=False) as session:
            run = session.get(TestRun, test_run_id)
            report_evidence = session.get(Evidence, run.evidence_id) if run and run.evidence_id else None
            if report_evidence is not None:
                report = self._read_test_report(report_evidence)
                provenance = report.get("provenance")
                if report.get("provenance_status") == "provider_signed" and isinstance(provenance, dict):
                    relations = session.scalars(
                        select(Relation).where(
                            Relation.source_type == "evidence",
                            Relation.source_id == report_evidence.id,
                            Relation.relation_type == "produces",
                            Relation.target_type == "evidence",
                        )
                    ).all()
                    junit_evidence = None
                    for relation in relations:
                        candidate = session.get(Evidence, relation.target_id)
                        if candidate and json_loads(candidate.metadata_json, {}).get("format") == "junit":
                            junit_evidence = candidate
                            break
                    if junit_evidence is None:
                        raise HarnessError("Signed CI JUnit evidence is missing.")
                    self._assert_evidence_integrity(junit_evidence)
                    if junit_evidence.sha256 != provenance.get("report_sha256"):
                        raise HarnessError("Signed CI JUnit hash no longer matches provenance.")
        return super().verify_requirement(requirement_id, test_run_id)


_services.Harness = Harness

__version__ = "0.2.0"

__all__ = ["Harness", "__version__"]
