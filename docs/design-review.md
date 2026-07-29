# Design Review and Hardening Report

This review checks the implementation against the project's stated goals: semantic state
management, append-only task history, evidence-backed conclusions, traceable engineering
verification, reproducibility, transaction safety, and safe concurrent use by LLM agents.

## Fixed in this review

### Concurrent writers

SQLite write transactions now start with `BEGIN IMMEDIATE`. This serializes task-event and
requirement-plan sequence allocation, preventing concurrent agents from selecting the same
next sequence number.

### Domain semantics

Task event types, evidence types, requirement priorities, pass-criteria keys, and relation
shapes are validated by the application layer. Relations are also idempotent when the exact
same edge is submitted twice.

### Evidence integrity

A conclusion cannot become supported or refuted using missing or modified evidence. Formal
requirement verification checks the test report and build artifact hashes again at the moment
of verification.

### Engineering verification chain

A requirement can enter `verified` only when all of the following are true:

```text
Requirement <-implements- Change <-produces- Build <-evaluates- Test Run
```

The Build must have succeeded, the Test Run must have passed against the same commit, the test
specification must cover the Requirement, and both the test report and build artifact must be
intact.

### Change identity

Captured Git refs are resolved to immutable commit SHAs. Empty diffs and identical base/head
commits are rejected, so an empty Change cannot be used to mark a Requirement implemented.

### Test report hardening

JUnit import rejects malformed XML, DTD/entity declarations, invalid or inconsistent counts,
oversized reports, zero-test success by default, and missing explicitly requested reports.
Pass criteria reject unknown keys, booleans, and negative limits.

### Terminal tasks

Completed tasks reject late evidence, changes, builds, tests, and task events. Follow-up work
must use a new Task rather than mutating a completed audit trail.

### Transaction and view consistency

Markdown rendering now runs after the authoritative database transaction commits. Rendering
uses atomic file replacement. A rendering failure leaves the database committed and writes a
stale-view marker that `harness doctor` reports; `harness render` repairs and clears it.
Copied artifacts are removed when the database transaction rolls back.

### Provenance detail

Generated IDs use 80 random bits instead of 40. Untracked Git files are recorded with path,
SHA-256, and size. Snapshot reproducibility is reported as full only when the worktree is clean,
a commit exists, and a dependency lock is available.

### Diagnostics

`harness doctor` now detects invalid states, sequence gaps, malformed or dangling relations,
empty changes, passed tests without evidence, stale rendered views, SQLite integrity failures,
pending schema migrations, stale idempotency reservations, and build-bound tests without a
formal Build-usage proof.

## Operational hardening follow-up

### Request idempotency

Agent-facing create/start and execution operations accept request keys. The registry stores a
canonical request hash and the original entity/response, returns completed results on safe
replay, rejects key reuse with different parameters, and blocks concurrent duplicates.
Reservations left in progress by a crashed process are surfaced by `harness doctor`.

### Database migrations

The database now has a transactional migration framework using `PRAGMA user_version` and an
append-only `schema_migrations` table. Existing v1 databases upgrade additively to schema v2;
opening a project applies pending migrations, while `harness-admin` exposes explicit status and
upgrade commands.

### Build-bound test execution

A local TestRun can verify a Requirement only when the TestSpec explicitly contains the
`{build_artifact}` placeholder. Harness re-verifies the Build evidence, stages a read-only
hash-named copy, substitutes the exact path, and records pre/post hashes and the resolved command.
Merely attaching a Build ID creates an audit relation but is not accepted as proof of use.

### Provider-signed CI provenance

CI imports may carry an Ed25519-signed canonical provenance document. Trusted provider keys are
stored per project. The signature binds provider/run/job identity, repository, workflow, commit,
Build ID and artifact hash, JUnit hash, and TestSpec command hash. Import checks all identities,
and Requirement verification repeats signature and evidence-integrity verification. Unsigned
imports remain historical evidence but cannot verify a Requirement.

### Bounded subprocess execution

Commands and tests stream stdout and stderr to bounded files rather than accumulating complete
output in memory. Formal reports contain byte counts, truncation flags, bounded tails, and stream
Evidence IDs. On timeout, POSIX execution terminates the entire process group with TERM/KILL;
Windows uses `taskkill /T /F` for descendant cleanup.

See [`operational-hardening.md`](operational-hardening.md) for the trust model and operator flow.

## Aggressive tests added

The adversarial suite includes:

- concurrent task-event and requirement-plan writers;
- duplicate and semantically invalid relations;
- tampered conclusion evidence;
- empty and moving Git refs;
- malformed, hostile, inconsistent, missing, and zero-test JUnit reports;
- unknown and type-confused pass criteria;
- verification attempts without a build, with a failed build, with an unrelated change, and
  with tampered test/build evidence;
- a complete valid Requirement -> Change -> Build -> Test Run verification chain;
- mutations attempted after task completion;
- database rollback and render-failure injection;
- ID collision sampling and untracked-file provenance checks;
- v1-to-v2 schema migration and repeat execution;
- idempotent replay and mismatched-payload rejection;
- bounded high-volume subprocess output;
- timeout cleanup of descendant processes;
- Build ID attachment without artifact use;
- explicit Build artifact binding;
- valid provider-signed CI imports and forged-signature rejection.

## Known remaining gaps

These are intentionally recorded rather than hidden by the coverage number:

1. A process crash after a semantic side effect commits but before the idempotency registry is
   finalized leaves an `in_progress` reservation. The system refuses unsafe automatic replay and
   reports the stale reservation for operator reconciliation; a future service mode should add
   leases and a transactional outbox/recovery protocol.
2. Local Build binding proves that the exact hashed artifact was supplied to the launched process
   and remained unchanged during execution. It cannot prove that arbitrary test-program logic is
   semantically correct or that every internal code path consumed the artifact.
3. Provider-signed CI provenance depends on secure key distribution and the provider runner's own
   security. Native OIDC/Sigstore adapters and provider-specific claim policies remain future
   integration work.
4. Superseding conclusions are not yet required to be supported before replacing an older
   conclusion; that policy should be decided explicitly.
