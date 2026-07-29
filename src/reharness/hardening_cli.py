from __future__ import annotations

import json
from pathlib import Path

import typer

from .services import Harness, HarnessError

app = typer.Typer(
    name="harness-admin",
    help="Schema migration, CI trust, and signed report administration.",
    no_args_is_help=True,
)
ci_app = typer.Typer(help="Manage trusted CI providers and signed imports.", no_args_is_help=True)
app.add_typer(ci_app, name="ci")


def _harness() -> Harness:
    return Harness.open()


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(2)


@app.command("migrate")
def migrate() -> None:
    """Apply all pending database migrations."""
    try:
        _emit(_harness().migrate())
    except HarnessError as exc:
        _fail(exc)


@app.command("schema-status")
def schema_status() -> None:
    """Show current and latest database schema versions."""
    try:
        _emit(_harness().migration_status())
    except HarnessError as exc:
        _fail(exc)


@ci_app.command("trust")
def trust_ci_provider(
    provider: str = typer.Option(...),
    key_id: str = typer.Option(...),
    public_key: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Trust an Ed25519 CI provider public key for this project."""
    try:
        _harness().trust_ci_provider(
            provider, key_id, public_key.read_text(encoding="utf-8")
        )
        _emit({"provider": provider, "key_id": key_id, "trusted": True})
    except (HarnessError, OSError) as exc:
        _fail(exc)


@ci_app.command("revoke")
def revoke_ci_provider(
    provider: str = typer.Option(...),
    key_id: str = typer.Option(...),
) -> None:
    """Disable a previously trusted CI provider key."""
    try:
        _harness().revoke_ci_provider(provider, key_id)
        _emit({"provider": provider, "key_id": key_id, "trusted": False})
    except HarnessError as exc:
        _fail(exc)


@ci_app.command("import-junit")
def import_signed_junit(
    test_spec_id: str,
    junit: Path = typer.Option(..., exists=True, dir_okay=False),
    build_id: str = typer.Option(..., "--build"),
    provenance: Path = typer.Option(..., exists=True, dir_okay=False),
    signature: Path = typer.Option(..., exists=True, dir_okay=False),
    task_id: str | None = typer.Option(None, "--task"),
    idempotency_key: str | None = typer.Option(None),
) -> None:
    """Import a JUnit report with provider-signed Build provenance."""
    try:
        run = _harness().import_junit(
            test_spec_id,
            junit,
            task_id,
            build_id,
            provenance,
            signature,
            idempotency_key,
        )
        _emit(
            {
                "id": run.id,
                "status": run.status,
                "build_id": run.build_id,
                "evidence_id": run.evidence_id,
            }
        )
        if run.status != "passed":
            raise typer.Exit(1)
    except HarnessError as exc:
        _fail(exc)
