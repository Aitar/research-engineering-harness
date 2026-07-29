# RE Harness

Repository: <https://github.com/Aitar/research-engineering-harness>

**RE Harness** is a local-first, AI-native research and engineering ledger for LLM agents.
It gives coding and research agents a small semantic CLI for maintaining project state,
append-only task logs, falsifiable conclusions, versioned requirements, reproducible evidence,
builds, and test verification.

It is deliberately **not** Jira, an MLOps dashboard, or an agent orchestration framework.
The database is the authoritative state source; Markdown is a generated, reviewable view.

## Why

Long-running LLM work tends to lose three things:

1. the exact goal that existed before execution;
2. the evidence that justified a conclusion or acceptance decision;
3. the relationship between a requirement, code change, build, and test result.

RE Harness preserves those links:

```text
Research:
Task → Task Events → Snapshot → Evidence → Conclusion → Project Brief

Engineering:
Requirement → Plan → Task → Change → Build → Test Spec → Test Run → Verification
```

## Features

- SQLite authority store with WAL, foreign keys, and transactional semantic operations.
- Generated Markdown project brief and detailed task, requirement, and conclusion views.
- Fixed task goals with append-only event logs.
- Conclusion states: `exploring`, `supported`, `refuted`, `superseded`.
- Requirement states separated from implementation and verification.
- Automatic command evidence capture: stdout, stderr, exit code, duration, Git state, environment, and hashes.
- SHA-256 integrity verification for evidence and build artifacts.
- Git commit, dirty-worktree patch hash, dependency-lock hash, model/data/prompt hash fields.
- Test specifications with predeclared pass criteria and JUnit support.
- Grep-first hybrid retrieval using exact lookup, SQLite FTS5, safe `ripgrep`, and relation expansion.
- Provenance-aware context packages for LLM session recovery.
- `harness doctor` consistency and evidence-integrity checks.
- JSON output on agent-facing commands.

## Installation

```bash
python -m pip install -e .
```

For development:

```bash
python -m pip install -e ".[dev]"
```

Python 3.11 or newer is required.

## Quick start

Initialize inside a Git repository:

```bash
harness init \
  --name "retrieval-research" \
  --description "Research and implement retrieval quality improvements"
```

Read the current project state:

```bash
harness brief
harness context --topic "reranker latency" --budget 12000
```

Start a research task:

```bash
harness task start \
  --type research \
  --goal "Determine whether reranking improves citation precision" \
  --criterion "Run the fixed dataset at least three times" \
  --criterion "Report precision, latency, and cost" \
  --constraint "Do not modify the evaluation set" \
  --json
```

Execute a command and capture evidence:

```bash
harness run TASK-XXXXXXXXXX \
  --capture results/*.json \
  --dataset-manifest-hash sha256:... \
  --model-hash sha256:... \
  -- python benchmark.py --top-k 20
```

Create and support a conclusion:

```bash
harness conclusion create \
  --claim "Reranking improves citation precision when top-k is at least 20"

harness conclusion support CON-XXXXXXXXXX \
  --evidence EVD-XXXXXXXXXX \
  --reason "Three fixed-snapshot runs exceeded the acceptance threshold"
```

## Engineering workflow

Create a versioned requirement:

```bash
harness requirement create \
  --description "Legacy refresh tokens remain usable after the upgrade" \
  --criterion "Valid legacy tokens migrate successfully" \
  --criterion "Expired or revoked tokens remain rejected"

harness requirement accept REQ-XXXXXXXXXX
harness requirement plan REQ-XXXXXXXXXX --file implementation-plan.md
harness requirement start REQ-XXXXXXXXXX
```

Capture the code change and build:

```bash
harness change capture \
  --base <base-commit> \
  --head <head-commit> \
  --requirement REQ-XXXXXXXXXX

harness requirement implemented REQ-XXXXXXXXXX

harness build capture \
  --artifact dist/package.whl \
  --change CHG-XXXXXXXXXX
```

Define and run a test:

```bash
harness test define \
  --name "legacy token smoke" \
  --type smoke \
  --command python \
  --command -m \
  --command pytest \
  --command tests/test_legacy_token.py \
  --requirement REQ-XXXXXXXXXX \
  --pass-criteria-file test-pass-criteria.json

harness test run TEST-XXXXXXXXXX --build BUILD-XXXXXXXXXX --json
harness requirement verify REQ-XXXXXXXXXX --test-run TRUN-XXXXXXXXXX
```

A requirement cannot enter `verified` unless the test run passed and its specification
explicitly covers that requirement.

## Test pass criteria

A test specification stores its criteria before execution. Supported keys are:

```json
{
  "exit_code": 0,
  "max_failures": 0,
  "min_passed": 10,
  "min_total": 10,
  "max_skipped": 0
}
```

JUnit reports can be used during execution or imported from CI:

```bash
harness test import TEST-XXXXXXXXXX --junit reports/junit.xml --json
```

## Historical retrieval

Search historical conclusions, tasks, requirements, evidence, builds, tests, and repository
text without a vector database:

```bash
harness search "reranker latency" --strategy hybrid
harness evidence list --type benchmark_result --integrity valid
harness evidence usage EVD-XXXXXXXXXXXXXXXXXXXX
harness trace CON-XXXXXXXXXXXXXXXXXXXX --depth 2
```

The retrieval order is exact ID/Hash resolution, structured SQLite filters, FTS5/BM25, bounded
literal `ripgrep`, and relation-graph expansion. Search tables are disposable projections; the
formal project database remains authoritative. Missing or stale projections can be repaired with:

```bash
harness index status
harness index verify
harness index rebuild
```

See [`docs/retrieval.md`](docs/retrieval.md) for architecture, safety limits, indexing semantics,
and the deliberate exclusion of vector retrieval from this version.

## Generated workspace

```text
.harness/
├── harness.db
└── config.yaml

harness-docs/
├── project-brief.md
├── tasks/
├── conclusions/
├── requirements/
└── tests/

harness-artifacts/
└── evidence/
```

- `.harness/harness.db` is authoritative.
- `harness-docs/` is generated and can be rebuilt with `harness render`.
- `harness-artifacts/` contains copied evidence and content hashes.

## Agent protocol

At session start:

```text
1. Run harness context.
2. Inspect related conclusions, requirements, and historical tasks.
3. Define one fixed goal, success criteria, and constraints.
4. Create the task before making external changes.
```

During work:

```text
1. Run external commands through harness run.
2. Record only externally meaningful observations and plan changes.
3. Capture raw logs, reports, scripts, datasets, and artifacts as evidence.
4. Never change a formal conclusion without evidence.
```

At completion:

```text
1. Mark the task succeeded or failed.
2. Keep task success separate from hypothesis truth.
3. Update conclusions and requirements using formal evidence/test runs.
4. Run harness render and harness doctor.
```

A full prompt/skill template is available in [`docs/agent-protocol.md`](docs/agent-protocol.md).

## Integrity model

SHA-256 proves that captured content has not changed. It does not prove that a method,
dataset, or conclusion is correct. RE Harness therefore stores both:

- immutable content identity and provenance snapshots;
- human/LLM-readable methods, limitations, criteria, and reasoning summaries.

The tool intentionally does not store hidden chain-of-thought. It records observable actions,
inputs, outputs, decisions, and evidence.

## Validation

The repository includes 95 unit, integration, failure-path, integrity, concurrency, retrieval,
Git, JUnit, transaction-fault, and CLI end-to-end tests. The latest architecture hardening findings,
fixes, adversarial scenarios, and known remaining gaps are documented in
[`docs/design-review.md`](docs/design-review.md).

```bash
pytest -W error::ResourceWarning
pytest --cov=reharness --cov-fail-under=95
```

## Current scope

This release implements the local research and engineering core. Planned extension points
include MCP, CI-provider adapters, PostgreSQL service mode, optional semantic/vector retrieval,
and remote artifact storage. These are intentionally kept outside the local MVP so the core remains small
and auditable.

## License

Apache License 2.0.
