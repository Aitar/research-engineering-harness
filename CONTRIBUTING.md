# Contributing

## Setup

```bash
python -m pip install -e ".[dev]"
```

## Tests

```bash
pytest -W error::ResourceWarning
pytest --cov=reharness --cov-fail-under=95
```

Every state transition or provenance rule change should include both a success-path and a
failure-path test. CLI changes should have at least one Typer `CliRunner` integration test.
Changes to task event sequencing, requirement plan versioning, evidence verification, or
rendering transactions should include a concurrency or injected-failure test when applicable.

## Design rules

- Prefer semantic operations over generic CRUD.
- Keep formal task goals and original requirement descriptions immutable.
- Keep task events append-only and reject work attached to terminal tasks.
- Require intact evidence for formal conclusion changes.
- Require a traceable Requirement → Change → Build → Test Run chain for verification.
- Treat Markdown as a post-commit materialized view of the authoritative database.
- Keep the local core usable without a service or network connection.

Review [`docs/design-review.md`](docs/design-review.md) before relaxing provenance, transaction,
or verification invariants.
