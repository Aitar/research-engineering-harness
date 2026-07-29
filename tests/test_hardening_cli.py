from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import reharness.hardening_cli as cli_module
from reharness.hardening_cli import app
from reharness.services import HarnessError

runner = CliRunner()


class FakeHarness:
    def __init__(self) -> None:
        self.trusted: tuple[str, str, str] | None = None
        self.revoked: tuple[str, str] | None = None
        self.import_status = "passed"
        self.fail: str | None = None

    def _raise_if_needed(self, operation: str) -> None:
        if self.fail == operation:
            raise HarnessError(f"{operation} failed")

    def migrate(self):
        self._raise_if_needed("migrate")
        return {"applied_versions": [2], "current_version": 2, "latest_version": 2}

    def migration_status(self):
        self._raise_if_needed("schema")
        return {"current_version": 2, "latest_version": 2, "pending": 0}

    def trust_ci_provider(self, provider: str, key_id: str, public_key: str) -> None:
        self._raise_if_needed("trust")
        self.trusted = (provider, key_id, public_key)

    def revoke_ci_provider(self, provider: str, key_id: str) -> None:
        self._raise_if_needed("revoke")
        self.revoked = (provider, key_id)

    def import_junit(self, *args):
        self._raise_if_needed("import")
        return SimpleNamespace(
            id="TRUN-TEST",
            status=self.import_status,
            build_id="BUILD-TEST",
            evidence_id="EVD-TEST",
        )


def test_admin_schema_and_trust_commands(monkeypatch, tmp_path: Path) -> None:
    fake = FakeHarness()
    monkeypatch.setattr(cli_module, "_harness", lambda: fake)

    status = runner.invoke(app, ["schema-status"])
    assert status.exit_code == 0
    assert '"pending": 0' in status.output

    migrate = runner.invoke(app, ["migrate"])
    assert migrate.exit_code == 0
    assert '"applied_versions"' in migrate.output

    key = tmp_path / "provider.pub"
    key.write_text("public-key", encoding="utf-8")
    trusted = runner.invoke(
        app,
        [
            "ci",
            "trust",
            "--provider",
            "github-actions",
            "--key-id",
            "release-key",
            "--public-key",
            str(key),
        ],
    )
    assert trusted.exit_code == 0
    assert fake.trusted == ("github-actions", "release-key", "public-key")

    revoked = runner.invoke(
        app,
        ["ci", "revoke", "--provider", "github-actions", "--key-id", "release-key"],
    )
    assert revoked.exit_code == 0
    assert fake.revoked == ("github-actions", "release-key")


def test_admin_signed_import_and_failure_exits(monkeypatch, tmp_path: Path) -> None:
    fake = FakeHarness()
    monkeypatch.setattr(cli_module, "_harness", lambda: fake)
    junit = tmp_path / "junit.xml"
    provenance = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sig"
    for path in (junit, provenance, signature):
        path.write_text("value", encoding="utf-8")

    args = [
        "ci",
        "import-junit",
        "TEST-TEST",
        "--junit",
        str(junit),
        "--build",
        "BUILD-TEST",
        "--provenance",
        str(provenance),
        "--signature",
        str(signature),
        "--task",
        "TASK-TEST",
        "--idempotency-key",
        "request-1",
    ]
    imported = runner.invoke(app, args)
    assert imported.exit_code == 0
    assert '"status": "passed"' in imported.output

    fake.import_status = "failed"
    failed_test = runner.invoke(app, args)
    assert failed_test.exit_code == 1

    fake.fail = "import"
    failed_import = runner.invoke(app, args)
    assert failed_import.exit_code == 2
    assert "import failed" in failed_import.output

    fake.fail = "schema"
    failed_schema = runner.invoke(app, ["schema-status"])
    assert failed_schema.exit_code == 2
    assert "schema failed" in failed_schema.output


def test_admin_trust_handles_file_and_service_errors(monkeypatch, tmp_path: Path) -> None:
    fake = FakeHarness()
    monkeypatch.setattr(cli_module, "_harness", lambda: fake)
    key = tmp_path / "provider.pub"
    key.write_text("public-key", encoding="utf-8")

    fake.fail = "trust"
    result = runner.invoke(
        app,
        [
            "ci",
            "trust",
            "--provider",
            "provider",
            "--key-id",
            "key",
            "--public-key",
            str(key),
        ],
    )
    assert result.exit_code == 2
    assert "trust failed" in result.output

    fake.fail = "revoke"
    result = runner.invoke(
        app,
        ["ci", "revoke", "--provider", "provider", "--key-id", "key"],
    )
    assert result.exit_code == 2
    assert "revoke failed" in result.output
