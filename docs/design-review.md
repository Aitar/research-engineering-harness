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
empty changes, passed tests without evidence, stale rendered views, and SQLite integrity
failures in addition to the existing checks.

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
- ID collision sampling and untracked-file provenance checks.

## Known remaining gaps

These are intentionally recorded rather than hidden by the coverage number:

1. Agent command retries do not yet accept idempotency/request keys. Exact duplicate relations
   are safe, but repeated create/start commands can still create multiple objects.
2. The schema has a version field but no migration framework for upgrading existing databases.
3. The system proves which Build a Test Run references, but cannot cryptographically prove an
   arbitrary test command actually executed that artifact without a build-specific runner or
   attestation format.
4. Imported CI reports do not yet carry provider-signed provenance or the original CI job exit
   status.
5. Subprocess output is captured in memory; configurable streaming limits and process-tree
   termination are still needed for hostile or unbounded commands.
6. Superseding conclusions are not yet required to be supported before replacing an older
   conclusion; that policy should be decided explicitly.
