"""RE Harness: an AI-native research and engineering ledger."""

from typing import Any

from . import services as _services
from .hardening import HardenedHarness


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


_services.Harness = Harness

__version__ = "0.2.0"

__all__ = ["Harness", "__version__"]
