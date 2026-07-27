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

A successful task may produce a negative or inconclusive research result.

### Conclusion

```text
exploring → supported → refuted
exploring → refuted
supported → superseded
refuted → superseded
```

`supported` and `refuted` require evidence. `superseded` requires a replacement conclusion.

### Requirement

```text
draft → accepted → in_progress → implemented → verified
```

A captured Change is required for `implemented`; a covered passing Test Run is required for
`verified`.

## Transaction boundaries

Each semantic operation performs validation, state mutation, relation creation, and audit
logging in one database transaction. Markdown is rendered after the authoritative mutation.
If rendering fails, the database remains authoritative and `harness render` repairs views.

## Evidence

Evidence is copied into a harness-controlled directory and recorded with SHA-256, size, MIME
type, source task/event, and metadata. Verification recomputes the digest. Missing or changed
content is reported by `harness doctor`.

## Snapshots

Command and test execution records:

- Git repository, commit, branch, dirty status, patch hash, untracked manifest, submodules;
- platform, architecture, runtime, dependency-lock hash, and safe environment-variable names;
- optional dataset, model, weight, tokenizer, prompt, container, and random-seed identifiers.

Dirty or uncommitted Git states are marked partially reproducible.

## Extensibility

The service layer is independent of Typer. MCP and HTTP adapters should call the same Harness
application methods so there is only one implementation of state validation.
