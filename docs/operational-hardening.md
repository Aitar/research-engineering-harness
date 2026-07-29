# Operational hardening

This release adds request idempotency, versioned database migrations, Build-bound test execution,
provider-signed CI provenance, bounded subprocess output, and process-tree termination.

## Database migrations

Harness databases now use `PRAGMA user_version` plus an append-only `schema_migrations` table.
Opening a project applies additive migrations automatically. Operators can inspect or apply them
explicitly:

```bash
harness-admin schema-status
harness-admin migrate
```

Schema version 2 adds the idempotency registry and trusted CI-provider key registry. Migrations
run transactionally and may be executed repeatedly.

## Idempotent agent requests

Create/start and execution operations accept an optional idempotency key. The same key and same
canonical request payload return the original entity or response; reusing the key with different
parameters is rejected. Concurrent duplicates are blocked while the first request is in progress.

Python callers can pass `idempotency_key=`. Existing CLI callers can set one key per invocation:

```bash
REHARNESS_IDEMPOTENCY_KEY=agent-request-42 \
  harness task start --type research --goal "Measure retrieval latency" --json
```

The registry covers Task, Conclusion, Requirement, TestSpec, command execution, local TestRun,
and imported TestRun creation. `harness doctor` reports reservations that remain in progress for
more than 15 minutes so a crash can be reconciled rather than silently replayed.

## Build-bound local tests

A TestSpec that can verify a Requirement must explicitly bind the captured Build artifact into
its command with the `{build_artifact}` placeholder:

```bash
harness test define \
  --name "package smoke" \
  --type smoke \
  --command python \
  --command scripts/verify_package.py \
  --command '{build_artifact}' \
  --requirement REQ-XXXXXXXXXXXXXXXXXXXX
```

Before execution, Harness:

1. re-verifies the Build Evidence hash;
2. copies it to a hash-named, read-only staging path;
3. substitutes that exact path into the command;
4. records pre- and post-execution hashes;
5. stores the resolved command and Build-usage proof in the TestRun report.

Merely supplying `--build` without the placeholder still records an `evaluates` relation, but it
cannot verify a Requirement.

## Provider-signed CI reports

A CI provider signs a canonical JSON provenance document with an Ed25519 key. Register the public
key once per project:

```bash
harness-admin ci trust \
  --provider github-actions \
  --key-id release-key-2026 \
  --public-key ci-release-key.pub
```

Import the report with its provenance and detached base64 signature:

```bash
harness-admin ci import-junit TEST-XXXXXXXXXXXXXXXXXXXX \
  --junit reports/junit.xml \
  --build BUILD-XXXXXXXXXXXXXXXXXXXX \
  --provenance reports/provenance.json \
  --signature reports/provenance.sig
```

The signed payload must include provider/key identity, repository, workflow/run/job identifiers,
commit SHA, Build ID, Build artifact SHA-256, JUnit SHA-256, TestSpec command SHA-256, issue time,
and a unique nonce. Import verifies the signature and all local identities. Requirement
verification repeats the signature and integrity checks at the state-transition boundary.
Unsigned JUnit imports remain useful as historical evidence but cannot verify a Requirement.

## Bounded subprocess execution

Commands and tests now stream stdout and stderr directly to temporary files instead of retaining
complete output in memory. Each stream has a configurable byte cap and bounded tail for immediate
CLI display. Full retained streams are copied into Evidence and truncation is explicit in the
formal report.

On timeout, POSIX processes run in a new session and Harness sends `SIGTERM`, then `SIGKILL` to the
whole process group after a grace period. Windows execution uses a new process group and
`taskkill /T /F` to terminate descendants. The report records whether process-tree termination
was required and completed.

## Trust boundary

The local Build proof establishes that the exact hashed artifact was supplied to the launched
command and remained unchanged during execution. It cannot prove the semantic correctness of an
arbitrary test program. CI proof establishes that a configured provider key attested to the
Build/report/command identities; key distribution and CI-runner security remain operator trust
responsibilities.
