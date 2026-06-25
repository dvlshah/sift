# sift corpus contract

_Single source of truth for what a published sift index looks like on disk and what an MCP read tool returns. Authoritative for both the **Python writer** (this repo: `sift run` → `sift publish`) and any **alternate reader** (e.g. a downstream consumer application that re-implements the read tools)._

> **Contract version:** `corpus@1`. The `snapshot.json` published by every run pins this version. Readers MUST refuse corpora whose `corpus_contract_version` differs from a version they know how to handle.

> **Parity discipline:** anytime a writer changes anything in this document, any alternate reader must change in lockstep. A byte-identical parity suite over fixture corpus paths (see § Parity Test) guards against divergence. Merging changes that diverge fails the build.

## 1. Why this exists

A deployment may have more than one reader of the engine's output:
- The Python engine itself (`sift status`, `sift verify`, `sift-mcp`).
- An alternate MCP gateway that re-implements the read tools for lower latency (no Python subprocess per call). Co-located with disk, embedded in a long-running process.

For an alternate reader to be a drop-in equivalent the engine's on-disk contract must be explicit. This document is that contract. It is shorter than the engine's full implementation by design — it pins only the **stable, externally observable surface** that readers depend on. Internal implementation details (extractor versions, normalizer internals, etc.) are out of scope.

## 2. Workspace directory layout (read-only view)

A published sift workspace at `<ROOT>/` exposes exactly:

```
<ROOT>/
  manifest.db                         SQLite — only the schema columns in §3.5 are public
  current → runs/<published_run_id>   Symlink — atomically flipped on a successful publish
  changelog.jsonl                     Append-only; one JSON object per change record
  runs/<run_id>/                      Working state for one pipeline invocation
    plan.jsonl                        Phase 1 output
    fetch.log                         Phase 2 output (one FetchResult per line)
    extract.log                       Phase 3 output
    md/<url-path>.md                  Phase 3 produced markdown (canonical)
    md/<url-path>.meta.json           Per-page extraction provenance
    facts/<schema-path>/<id>.json     Phase 3 produced structured facts (optional)
    artifacts/
      INDEX.md                        Phase 5 — root agent-surface index
      routes.tsv                      Phase 5 — URL → md path lookup
      sections/<top>/INDEX.md         Phase 5 — per-section drill-down (optional)
      llms.txt                        Phase 5 — rollup variant (optional)
      llms-full.txt                   Phase 5 — rollup variant (optional)
    snapshot.json                     Phase 5 — manifest of versions, counts, gates, integrity
```

**Readers MUST only read paths under `current/`.** Other `runs/<run_id>/` directories are working state and may be partial, mid-write, or about to be evicted by retention. The symlink flip is the only "this is publishable" signal.

**Readers MUST NOT follow symlinks outside the workspace root.** `current/` is the only symlink they encounter; anything else is a bug.

## 3. File-format contracts

### 3.1 `current/artifacts/INDEX.md`

UTF-8 markdown. Always present after a successful publish. Used as the entry point for grep-first navigation.

- Starts with an H1: `# <site host> · sift index`.
- Contains a "Sections" subtree linking to `sections/<top>/INDEX.md` files (when sections exist).
- Contains an "Artifacts" subtree linking to `routes.tsv`, `llms.txt`, etc. (when present).
- Contains a "Recipes" subtree with example `grep` patterns the reader can suggest.

**Reader guarantee:** byte-for-byte stable for a given `snapshot.json.integrity.merkle_root`.

### 3.2 `current/artifacts/routes.tsv`

UTF-8 TSV. **Header on line 1**; data rows follow. Required columns, in order:

```
url    md_path    tier    content_hash    fetched_at    audience    fy_years
```

- `url` — absolute, normalized (lowercase host, no trailing slash on root, no fragment).
- `md_path` — relative path under `current/`, always begins `md/`.
- `tier` — string, free-form. Site-profile-defined.
- `content_hash` — `sha256:` + 64 lowercase hex chars.
- `fetched_at` — ISO 8601 UTC, second precision (`2026-05-30T11:22:33Z`).
- `audience` — string, free-form (may be empty).
- `fy_years` — comma-separated integers (may be empty).

**Reader guarantee:** rows are sorted by `url` ascending. Duplicates MUST NOT appear. Tabs inside values are forbidden (writer must reject at extract time).

### 3.3 `current/md/**/*.md`

UTF-8 markdown. Each file begins with a YAML frontmatter block delimited by `---` lines.

Required frontmatter keys:

```yaml
---
url: https://docs.stripe.com/api
content_hash: sha256:7c41…e0b9
tier: api-reference
fetched_at: 2026-05-30T11:22:33Z
audience: developer
fy_years: []
anchors:
  - id: payment-intents
    title: PaymentIntents
    line: 42
---
```

- `url` — same form as in `routes.tsv`.
- `content_hash` — sha256 of `normalize_for_hash(markdown_body)`. The body is everything after the closing `---` plus a trailing newline. `normalize_for_hash` is defined in §6 — the reader MUST be able to verify this hash byte-identically.
- `tier`, `audience`, `fy_years` — same semantics as `routes.tsv`.
- `anchors` — array of `{id, title, line}` triples. `line` is 1-indexed and refers to a line in the markdown body **after** the frontmatter (line 1 = first body line).

Optional keys are permitted; readers MUST ignore unknown keys.

### 3.4 `current/facts/**/*.json`

UTF-8 JSON, one object per file. Required keys:

```json
{
  "$schema": "https://schemas.sift.dev/v1/rate-table.json",
  "source_url": "https://docs.stripe.com/pricing",
  "source_md_path": "md/pricing.md",
  "content_hash": "sha256:…",
  "fetched_at": "2026-05-30T11:22:33Z",
  "data": { /* schema-specific payload */ }
}
```

- `$schema` — fully qualified URL identifying the JSON Schema. The reader does NOT validate against it (that's the writer's job at extract time); it surfaces it verbatim in tool responses.
- `source_url`, `content_hash`, `fetched_at` — same semantics as `md/*.md` frontmatter.
- `source_md_path` — the `md/` file that produced this fact (for back-references).
- `data` — the schema-specific payload. Opaque to readers.

### 3.5 `current/snapshot.json`

UTF-8 JSON. The "what was published" manifest. The reader's primary integrity + version check.

```json
{
  "corpus_contract_version": "corpus@1",
  "run_id": "20260530T112233Z",
  "status": "ok",
  "counts_by_state": { "FRESH": 12750, "FROZEN": 0, "GONE": 12, "FAILED": 3 },
  "counts_by_tier": { "api-reference": 8120, "guides": 4630 },
  "versions": {
    "crawler": "1.4.0",
    "extractor": "1.7.2",
    "normalizer": "1.3.0",
    "classifier": "1.1.0"
  },
  "gates": {
    "G1_coverage":   { "pass": true,  "value": 0.9994, "floor": 0.99 },
    "G2_freshness":  { "pass": true,  "value": 0.9821 },
    "G3_uniqueness": { "pass": true,  "value": 1.0 },
    "G4_integrity":  { "pass": true,  "value": 1.0 },
    "G5_schemas":    { "pass": true,  "value": 1.0 }
  },
  "integrity": {
    "merkle_root": "sha256:9e1c…d40a",
    "leaf_count":  12750
  },
  "published_at": "2026-05-30T11:25:01Z"
}
```

Required keys: every key shown above. The reader MUST:
- refuse a corpus where `corpus_contract_version` is not in its supported set;
- refuse a corpus where any `versions.*` is not in its supported set (the gateway's parity test fixes its known versions; an unknown extractor version means the on-disk shape may have drifted and bytes-identical output is no longer guaranteed);
- surface `integrity.merkle_root` in `snapshot_status` tool responses as a tamper-evidence proxy.

### 3.6 `manifest.db` (public surface only)

SQLite, WAL-journaled. Writers hold the write lock; readers MUST open as read-only (`mode=ro` in the SQLite URI).

Public tables and columns the reader may query:

```sql
-- table: manifest  (one row per URL ever observed)
url                TEXT PRIMARY KEY
state              TEXT          -- 'FRESH' | 'FROZEN' | 'GONE' | 'FAILED' | 'UNSEEN'
content_hash       TEXT          -- 'sha256:…'  (null if never extracted)
raw_hash           TEXT          -- 'sha256:…'  (null if never fetched)
tier               TEXT
last_fetched_at    TEXT          -- ISO 8601 UTC
last_changed_at    TEXT          -- ISO 8601 UTC
unchanged_streak   INTEGER
http_etag          TEXT
http_last_modified TEXT
```

All other tables and columns are **private** — the reader MUST NOT depend on their presence, shape, or semantics.

## 4. MCP read-tool contracts

The eight read tools below are the public surface any MCP gateway exposes. Each is specified as `name(args) → result`.

### 4.1 `snapshot_status(index_root: str) → { ... }`

Returns the contents of `current/snapshot.json` plus a short summary of artifact inventory. Pre-publish (no `current/`), returns `{ status: "no_published_run", reason: "current/ symlink missing" }` and HTTP 200.

### 4.2 `read_md(path, offset=0, limit=20000, verify=false) → { ... }`

Reads `current/<path>`. `path` MUST start with `md/`. Returns frontmatter as a parsed object and the body as a string slice (`offset..offset+limit` UTF-8 chars). If `verify=true`, recomputes `content_hash` over the body and refuses to return if it doesn't match the frontmatter (`isError: true`).

### 4.3 `grep_corpus(pattern, path="md/", ignore_case=false, context=0, max_matches=200) → [{ file, line, snippet }]`

Regex over UTF-8 markdown files under `current/<path>/**/*.md`. Returns up to `max_matches` hits. Each hit carries `file` (relative to `current/`), `line` (1-indexed), and `snippet` (a single line with optional ± `context` lines joined by `\n`).

**Reader implementation note:** an alternate reader should shell to a long-lived `ripgrep` binary rather than spawning an in-process regex engine for non-trivial corpora. The parity test confirms identical hit-by-hit output between Python `re` and `ripgrep` for the agreed pattern subset.

### 4.4 `glob_corpus(pattern, max_results=500) → [path]`

`fnmatch`-style glob, relative to `current/`. Returns up to `max_results` paths. Sorted ascending.

### 4.5 `list_dir(path=".") → [{ name, kind, size }]`

Lists immediate children of `current/<path>`. `kind` is `"file"` or `"dir"`. `size` is bytes for files, 0 for dirs.

### 4.6 `query_manifest(sql) → [row]`

Read-only SQLite SELECT against the **public** columns of `manifest.db` (§3.5). The reader MUST reject any query that:
- is not a single `SELECT` statement;
- references a non-public table or column (§3.5);
- exceeds 500 rows in result.

### 4.7 `read_facts(path) → { ... }`

Reads `current/<path>`. `path` MUST start with `facts/`. Returns the parsed JSON object verbatim.

### 4.8 `changed_since(since, path_prefix=null, tier=null, limit=500, offset=0) → { ... }`

Net content delta between `since` and the current published snapshot, read from the index-root `changelog.jsonl` (§2) — **not** `current/`. `since` is a `run_id` (resolved to that run's `completed_at`) or an ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`). The window is `(since_ts, published_ts]`: the upper bound is the **published** snapshot's `completed_at`, so a reader MUST exclude entries from any later (unpublished/degraded) run. Per URL, collapse all in-window entries to one net delta — `old_hash` from the first in-window entry, `new_hash` from the last — and drop any whose net `old_hash == new_hash`. Classify: `added` (net `old_hash` null), `modified` (both present and differ), `removed` (net `new_hash` null). Returns `{ from, to, counts, added, modified, removed, cursor, chain_tip_entry_hash, truncated }` where `cursor` is the current published `run_id`; each list is capped at `limit` per group (newest-first). No published snapshot → `isError: true`.

## 5. content_hash semantics

Both writer and reader MUST compute `content_hash` as:

```
sha256( normalize_for_hash( markdown_body_utf8 ) )
```

`normalize_for_hash` is the following deterministic transform, in order:

1. Strip BOM if present.
2. Normalize all line endings to `\n`.
3. Strip trailing whitespace from every line.
4. Collapse runs of `\n\n\n+` to `\n\n` (one blank line max).
5. Ensure exactly one trailing `\n`.
6. UTF-8 encode.

The hex output is lowercase. Frontmatter is NOT part of the hash.

## 6. Parity test

Owned by the downstream consumer application that re-implements the read tools. Runs in CI on every PR that can affect read-tool output.

For each fixture corpus path:
1. Invoke the Python engine's read tool with given args.
2. Invoke the alternate reader's read tool with identical args.
3. Assert byte-identical output (or, for structured returns, deep-equal after canonical JSON serialization).

Any divergence is a release blocker. Fixtures cover every tool, plus edge cases:
- empty `md/` directories,
- `grep_corpus` patterns hitting > `max_matches`,
- `read_md` with `verify=true` against a deliberately mutated body (must `isError: true` on both sides),
- `query_manifest` with `SELECT` that references private columns (must `isError: true` on both sides, identical error class),
- `snapshot_status` against a corpus with `current/` missing.

## 7. Backwards-compatibility policy

The on-disk contract may grow but MUST NOT break.

- New fields in `routes.tsv`: append at end of the column list. Readers ignore extra columns.
- New keys in YAML frontmatter or JSON facts: readers ignore unknown keys.
- New columns in `manifest.db` public surface: append; readers query by name.
- New tool: add to §4 and bump `corpus_contract_version` if it changes existing tool semantics.
- Removing or renaming a field: requires a contract version bump (`corpus@1 → corpus@2`) and a coordinated reader update. Old corpora keep working with old readers.

`corpus_contract_version` in `snapshot.json` is the single canonical version string. Readers maintain a list of supported versions; corpora outside that list are refused (`snapshot_status` returns `isError: true` with a recovery hint to upgrade the reader).

## 8. Sources of truth and ownership

| Thing | Owner | Where it lives |
|---|---|---|
| The contract itself | this file | `./corpus.contract.md` |
| Python writer | sift engine maintainer | `./sift/**` |
| Alternate reader | downstream consumer application | the consumer's own repository |
| Parity test | downstream consumer application | the consumer's own repository |
| Fixtures | downstream consumer application | the consumer's own repository |

A change to any of writer / reader / fixtures MUST update this file in the same PR. CI rejects PRs that change the writer's output shape without a matching contract bump.
