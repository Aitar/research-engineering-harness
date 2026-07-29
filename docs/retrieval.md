# Grep-first hybrid retrieval

RE Harness retrieves historical project knowledge without a vector database. The first version
combines exact entity resolution, structured SQLite filters, SQLite FTS5/BM25, bounded literal
`ripgrep`, and relation-graph expansion.

The authoritative task, conclusion, requirement, evidence, build, and test tables remain the
source of truth. `search_documents`, the FTS virtual table, and index state are disposable
projections that can be deleted and rebuilt.

## Retrieval order

```text
exact ID / SHA
    -> structured SQL filters
    -> FTS5 lexical candidates
    -> safe repository grep
    -> relation and provenance expansion
    -> deterministic authority/integrity ranking
```

Exact identifiers are never displaced by fuzzy candidates. Superseded conclusions remain
visible for audit purposes, but their replacement is injected into the result set. Evidence
results include their current integrity status.

## Commands

Search formal project history:

```bash
harness search "AWQ high concurrency" \
  --type conclusion \
  --type evidence \
  --status supported \
  --status refuted \
  --strategy hybrid \
  --graph-depth 1
```

Search strategies are:

- `exact`: entity IDs and exact hashes;
- `lexical`: exact resolution, structured filters, and FTS5;
- `grep`: bounded literal file search;
- `hybrid`: exact, structured, FTS5, grep, and graph expansion.

Browse and reverse-trace evidence:

```bash
harness evidence list --type benchmark_result --integrity valid
harness evidence usage EVD-XXXXXXXXXXXXXXXXXXXX
```

Trace any formal entity:

```bash
harness trace CON-XXXXXXXXXXXXXXXXXXXX --depth 2 --max-nodes 50
```

Maintain the disposable index:

```bash
harness index status
harness index verify
harness index rebuild
```

Build an LLM context package:

```bash
harness context \
  --topic "authentication migration failure" \
  --strategy hybrid \
  --budget 12000
```

## Search projection

Each searchable entity is projected into one or more domain-sized documents rather than fixed
length token chunks. The projection records:

- entity and chunk identity;
- title and normalized searchable body;
- formal status and authority level;
- evidence integrity where applicable;
- source hash and source version;
- provenance metadata and timestamps.

CJK text is supplemented with character n-grams so searches do not depend on whitespace word
boundaries. Code symbols, IDs, hashes, paths, and error strings are preserved for exact lexical
matching.

## Index lifecycle

An initialized index is marked stale after an authoritative write. The next search rebuilds a
missing or stale index from the database. A failed rebuild does not roll back or corrupt the
formal project state. `harness doctor` reports an initialized stale index, and `harness index
verify` checks source hashes, missing/orphan projections, and FTS membership.

Old project databases are upgraded additively when retrieval is first used: the search tables
and FTS virtual table are created without changing existing formal records.

## Safe grep boundary

The grep backend treats the query as a fixed string, limits result count and output size, skips
binary and oversized files, applies a timeout, and searches only approved project roots. It
uses `ripgrep` when available and a bounded Python literal-search fallback otherwise. Search
matches are mapped back to formal entity IDs whenever their path belongs to generated Harness
views or managed Evidence.

## Context construction

Context output keeps records whole rather than cutting through the middle of an entity. Formal
conclusions and requirements receive more authority than task-event or repository-file matches.
Related evidence, replacement conclusions, tasks, builds, and test runs are added through the
relation graph while respecting the requested character budget.

## Deliberate exclusions

This version does not include embeddings, a vector database, semantic reranking, or an LLM query
planner. Those can be added later behind retrieval interfaces without changing the authoritative
data model or the provenance requirements of search results.
