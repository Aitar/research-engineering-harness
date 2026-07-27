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

## Design rules

- Prefer semantic operations over generic CRUD.
- Keep formal task goals and original requirement descriptions immutable.
- Keep task events append-only.
- Require evidence for formal conclusion changes.
- Require passing covered tests for requirement verification.
- Keep the local core usable without a service or network connection.
