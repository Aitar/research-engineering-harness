# Architecture

## Boundary

RE Harness is a semantic state and provenance layer above Git, local processes, test runners,
and CI reports. It does not replace those systems.

## Authority and views

```text
SQLite authority store
  ├── IDs and state machines
  ├── append-only events
  ├── relations and audit records
  ├── hashes and snapshots
  └── current indexes

Generated Markdown
  ├── project brief
  ├── task timelines
  ├── requirement plans
  └── conclusion evidence summaries

Artifact storage
  ├── command logs
  ├── JUnit reports
  ├── patches
  ├── build artifacts
  └── experiment results
```

LLMs call semantic commands. They do not update both SQLite and Markdown independently.

## State machines

### Task

```text
in_progress → succeeded
in_progress → failed
```

A successful task may produce a negative or inconclusive research result. A terminal task is
immutable: follow-up evidence, changes, builds, and tests must be associated with a new Task.

### Conclusion

```text
exploring → supported → refuted
exploring → refuted
supported → superseded
refuted → superseded
```

`supported` and `refuted` require evidence whose stored content currently matches its SHA-256.
`superseded` requires a replacement conclusion.

### Requirement

```text
draft → accepted → in_progress → implemented → verified
```

A non-empty captured Change is required for `implemented`. Verification requires a covered,
passing Test Run against a succeeded Build produced by a Change that implements the
Requirement. The test report and build artifact are re-verified at the transition boundary.

## Transaction boundaries

Each semantic write starts an SQLite `BEGIN IMMEDIATE` transaction. This serializes sequence
allocation for concurrent agents while retaining WAL-mode readers. Validation, state mutation,
relation creation, and audit logging commit atomically.

Markdown is a post-commit materialized view. Rendering uses atomic file replacement. If it
fails, the authoritative database remains committed and a stale-view marker is reported by
`harness doctor`; `harness render` rebuilds the views. Evidence copied before a failed database
transaction is removed during rollback cleanup.

## Evidence

Evidence is copied into a harness-controlled directory and recorded with SHA-256, size, MIME
type, source task/event, and metadata. Verification recomputes the digest. Missing or changed
content is reported by `harness doctor` and cannot be used for formal conclusion or requirement
state transitions.

## Snapshots

Command and test execution records:

- Git repository, canonical commit SHA, branch, dirty status, patch hash, hashed untracked-file
  manifest, and submodules;
- platform, architecture, runtime, dependency-lock hash, and safe environment-variable names;
- optional dataset, model, weight, tokenizer, prompt, container, and random-seed identifiers.

Dirty worktrees, missing commits, or missing dependency locks are marked partially
reproducible. Harness-managed database, generated Markdown, and artifact directories are
excluded from captured Git worktree provenance.

## Relations

Relations are constrained by source type, relation name, and target type. For example, a Test
Run `evaluates` a Build and may `verify` a Requirement only after the verification service has
validated the full Requirement → Change → Build → Test Run chain. Exact duplicate relation
submissions are idempotent.

## Retrieval projection

Historical retrieval is grep-first and provenance-aware. Exact IDs and hashes, structured SQL,
SQLite FTS5, bounded literal grep, and Relation traversal produce candidates. Search documents
and FTS rows are disposable, hash-verified projections; they never replace authoritative domain
records. See [`retrieval.md`](retrieval.md).

## Extensibility

The service layer is independent of Typer. MCP and HTTP adapters should call the same Harness
application methods so there is only one implementation of state validation.

See [`design-review.md`](design-review.md) for the latest adversarial review, fixed deviations,
and remaining design gaps.
