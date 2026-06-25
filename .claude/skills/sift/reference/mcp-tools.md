# sift MCP tools reference

The `sift-mcp` server (stdio, read-only by default) verified against `sift v0.1.0`.

## Launching the server

```bash
sift-mcp --root /abs/path/to/index            # single index
sift-mcp --root /abs/path/to/parent-dir       # multi-index (auto-detected)
sift-mcp --root R --enable-index              # + write tools
```

| Flag | Default | Purpose |
|---|---|---|
| `--root PATH` (required) | — | A single index root (has `current/`), **or** a parent dir of several roots → multi-index mode. |
| `--config PATH` | `./sift.toml` | **Single-index only.** Read at startup for the write allow-list, threaded to the crawl subprocess. Ignored in multi-index mode (each sub-index's own `sift.toml` is read at write time). |
| `--enable-index` | off | Expose `index_url`/`index_status`. Off → strictly read-only. |
| `--max-concurrent-crawls N` | 4 (1–32) | Cap on simultaneous `index_url` crawls across all slugs. |
| `--registry-ttl-ms N` | 1000 (0–60000) | How long the multi-index registry is cached between calls; a freshly built sub-index appears within this TTL without a restart. 0 = rebuild every call. |

**Mode is auto-detected and frozen at startup** (switching mid-session would break clients that cached the tool list). Multi-index mode adds `list_indexes` and an `index` parameter to every content tool.

## Server-shipped instructions

The server sets a server-level `instructions` brief (the agent sees it on connect): *snapshot_status first (remember its run_id; next session changed_since to pull only the delta) → grep to locate, read_md to drill (offset/limit), glob/list_dir to explore, read_facts/query_manifest for structured lookups → be token-efficient (everything is capped) → cite content_hash + fetched_at + url.* Multi-index adds "list_indexes first, pass `index=<slug>`, `index="*"` fans out." Write mode adds "index_url then poll index_status until succeeded." This skill's [Query](../SKILL.md#query-a-connected-corpus) section is the expanded version.

## Read tools (always available)

### `snapshot_status`
**Call first.** Reports published yes/no, `run_id`, gate results, counts by state/tier, version pins, artifact inventory, and suggested entry points. **Never errors** (works pre-publish to diagnose).
- `index` (multi only, optional; `"*"` fans out).

### `changed_since`
Net **added / modified / removed** pages since a cursor — the diff feed for staying current without re-reading. Store the `run_id` from `snapshot_status`; next session pass it back to pull only what moved, then `read_md` just those and store the new `cursor`. Read from the hash-chained `changelog.jsonl`; the delta is bounded to the **published** snapshot, so it matches what `read_md` serves (a later unpublished run never leaks in).
- `since` (required) — a `run_id` (preferred) or ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
- `path_prefix` (optional) — only URLs starting with this prefix.
- `tier` (optional) — only pages in this tier (e.g. `LIVING`, `FROZEN`).
- `limit` (default 500) / `offset` (default 0) — per group, newest-first.
- `index` (multi only, optional; `"*"` fans out).
- Returns `counts`, the three lists (each item: `url`, `old_hash`/`new_hash`, `tier`, `entry_hash`), a fresh `cursor`, and `chain_tip_entry_hash`. Empty delta with `up_to_date=true` → you're current.

### `read_md`
Read one markdown file. Use **after** locating it — `read_md` does not search. Returns YAML frontmatter (url, content_hash, tier, audience, fy_years, anchors) + body.
- `path` (required) — relative to `current/`, e.g. `md/individuals/your-return.md` or `INDEX.md`.
- `offset` (default 0) — start char.
- `limit` (default **20000**) — max chars. Page long files with offset/limit instead of re-reading.
- `verify` (default false) — re-hash body vs frontmatter `content_hash`. Match → `[verify=ok …]` header; **mismatch → `isError`, file is untrusted.**
- `index` (multi only, **required**).
- **Cap:** 20,000 chars (truncates with size note).

### `read_facts`
Read one `facts/<schema>/*.json` (atomic structured record with `$schema`, `source_url`, `content_hash`). **Prefer over `read_md` for numbers, rates, thresholds, deadlines.** Discover via `list_dir facts/…` or `glob_corpus 'facts/**/*.json'`; schemas live at `facts/schemas/<schema>.json`.
- `path` (required) — relative to `current/`.
- `index` (multi only, **required**).
- **Cap:** 20,000 chars.

### `grep_corpus`
Regex over the corpus (defaults to `md/`). Returns `file:line:snippet`. **Best for identifiers** (section numbers, form codes, anchors like `{#cents-per-kilometre}`, exact phrases) — faster + more precise than semantic search.
- `pattern` (required) — Python regex.
- `path` (default `md/`) — file/dir to search (try `routes.tsv` for url→path).
- `ignore_case` (default false).
- `files_only` (default false) — filenames only (narrow before `read_md`).
- `context` (default 0, max 5) — context lines per match.
- `index` (multi only, optional; `"*"` fans out).
- **Cap:** 200 matches (then refine, or `files_only=true`).

### `glob_corpus`
List files matching an fnmatch glob, relative to `current/`. For path-shape queries: `md/forms/**/2025*`, `facts/**/*.json`, `sections/*/INDEX.md`.
- `pattern` (required).
- `index` (multi only, optional; `"*"` fans out).
- **Cap:** 500 paths.

### `list_dir`
Immediate directory contents, one line per entry as `d|f <size> <name>`. Cheap exploration: `.`, `md/`, `sections/`, `facts/`.
- `path` (default `.`) — relative to `current/`.
- `index` (multi only, optional; `"*"` fans out).
- **Cap:** 500 entries.

### `query_manifest`
Read-only `SELECT`/`WITH` against `manifest.db` (the structured index of every URL). For cross-cutting queries: "FRESH pages under parent_guide X", "most-recently-changed", "URLs missing from this snapshot". Schema discovery: `SELECT sql FROM sqlite_master WHERE type='table'`. Returns JSON array.
- `sql` (required) — non-SELECT refused.
- `index` (multi only, optional; `"*"` fans out).
- **Cap:** 500 rows (use `LIMIT`).

## Multi-index tools

### `list_indexes`
**Call first in any multi-index session** — content tools need an `index=<slug>` that comes from here. Returns per-index: slug, description, domain, tags, page_count, last_published, `accepts_writes`, `unseen_count`, recent runs. Pick by description/domain; filter by tags. No parameters.

### `index` parameter rules (multi-index mode)
| Tool | `index` | `index="*"` fan-out |
|---|---|---|
| `read_md`, `read_facts` | **required** | no |
| `index_url`, `index_status` | **required** | no |
| `grep_corpus`, `glob_corpus`, `list_dir`, `query_manifest`, `snapshot_status`, `changed_since` | optional | yes (slower, noisier — scope when you can) |

## Write tools (only with `--enable-index`)

### `index_url`
Seed 1–20 absolute `http(s)` URLs and trigger an incremental background crawl. Returns a `run_id` **immediately** + a `poll` hint. Already-indexed URLs are re-planned (refetched if due, else skipped); unseen URLs are seeded + fetched.
- `urls` (required) — array, 1–20, each on the index's allow-listed host(s).
- `index` (multi only, **required**).
- **Allow-list:** every URL's connect-host must be in the target index's `seed.host_allow`; off-list URLs are refused with the accepted hosts listed (defeats `https://allowed@evil.com/` tricks). Annotations: `readOnlyHint=false`, `openWorldHint=true`.

### `index_status`
Poll a job by `run_id`. Status `running` → `succeeded` / `degraded` / `failed`, with phase, timestamps, per-state counts, and `published_as_current`. On `succeeded` the new pages are readable via `read_md` with no extra publish step. Completed runs are durable in the manifest (queryable forever); in-progress jobs do not survive a server restart.
- `run_id` (required).
- `index` (multi only, **required**).

## Refusal behavior

Read tools (except `snapshot_status`) hard-fail with `isError` when there's no published snapshot:

> No published snapshot at `<root>/current/`. The index hasn't completed a successful publish yet. Call snapshot_status for details, then run `sift publish --root <root> --run-id <id>` once the pipeline finishes and all gates pass.

`.mcp.json` wiring and the build lifecycle are in [SKILL.md](../SKILL.md).
