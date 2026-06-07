# sift CLI reference

Every `sift` subcommand and flag, verified against `sift v0.1.0`. The CLI is a flat `click` group — there are **no global options**; `--root` and `--config` are per-command.

- `--root PATH` — index root (holds `manifest.db`, `raw/`, `runs/`, `current/`). **Required on every subcommand.**
- `--config PATH` — config TOML. Omitted → searches `./sift.local.toml` then `./sift.toml`; falls back to built-in defaults. CLI flags override config values. Present on every phase/verify command (not on `init`, `status`, `backup`, `verify-backup`, `manifest-query`).

Tier values (used by `--tier`, repeatable): `FROZEN`, `CURRENT_FORMS`, `LIVING`, `NEWS`.

## Pipeline phases

### `sift init`
Initialize `manifest.db` schema. Idempotent — won't clobber existing state; surfaces whether you're extending a changelog chain or starting fresh.
- `--root` (required).

### `sift seed`
Add URLs to the manifest from one or more discovery sources. Each URL is canonicalized, classified, and upserted unless it matches an exclude. Sources combine in one invocation.
- `--from-json PATH` — seed from a `{links:[{url,...}]}` dump.
- `--from-sitemap URL` — fetch one `sitemap.xml` (recurses sitemap-indexes).
- `--from-domain URL` — auto-discover every sitemap for a domain (robots.txt `Sitemap:` directives + well-known paths `/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`, …; handles gzip + plain-text). Use when you only know the domain.
- `--from-firecrawl-map URL` — Firecrawl `/v2/map` (blended sitemap + cached crawl + SERP). Closes the gap for sites with no/sparse/bot-blocked sitemaps. Needs `FIRECRAWL_API_KEY`.
- `--firecrawl-limit N` — max URLs from `--from-firecrawl-map` (default 500).
- `--firecrawl-search KW` — server-side keyword filter for map results (scope a large site).
- `--firecrawl-include-subdomains` — include subdomain URLs in map results.
- `--host-allow HOST` — only seed these hosts (repeatable; overrides `[seed].host_allow`).
- `--exclude REGEX` — extra skip patterns (repeatable; appended to config).
- `--no-default-excludes` — disable built-in excludes (`/sitemap*`, `/api/*`, `/print/*`, …).

### `sift plan`
Phase 1: emit `plan.jsonl` (per-URL FETCH / FETCH_CONDITIONAL / SKIP / TOMBSTONE_PURGE).
- `--run-id TEXT` — reuse an existing run-id (else generated).
- `--only-urls PATH` — scope to URLs in a file (one per line; blanks + `#` comments skipped).

### `sift fetch`
Phase 2: async fetch per `plan.jsonl`. Idempotent; resumes via `fetch.log`.
- `--run-id TEXT` (**required**).
- `--limit N` — stop after N fetches (smoke tests).
- `--rate FLOAT` — req/s per host (default: `crawl.rate_per_sec`).
- `--concurrency N` — in-flight requests (default: `crawl.concurrency`).
- `--decisions TEXT` — only fetch these decisions (default `FETCH`, `FETCH_CONDITIONAL`).
- `--tier TEXT` — only these tiers (repeatable).
- `--firecrawl-fallback` — on 401/403, escalate via Firecrawl `/v2/scrape` (costs credits, capped by `[crawl.firecrawl].max_credits_per_run`; needs `FIRECRAWL_API_KEY`).

### `sift extract`
Phase 3: HTML→markdown (trafilatura) / PDF→text (pypdf) → `content_hash`.
- `--run-id TEXT` (**required**).

### `sift commit`
Phase 4: apply fetch+extract logs to the manifest in one SQLite transaction; append chained changelog entries.
- `--run-id TEXT` (**required**).

### `sift purge`
Drop manifest rows whose plan decision is `TOMBSTONE_PURGE` (URLs GONE past their tier `tombstone_ttl_days`). `sift run` already does this between commit and publish; use standalone after tweaking TTLs.
- `--dry-run` — show what would be purged.

### `sift publish`
Phase 5: verify **5 gates**, build artifacts, atomically flip the `current/` symlink. Writes `snapshot.json` (Merkle root, version pins, gate results).
- `--run-id TEXT` (**required**).
- `--skip-artifacts` — skip `llms.txt` / `by_guide` rollups (faster smoke tests).

The 5 gates: **coverage (G3)** ≥ `[publish].coverage_floor` of seeded URLs terminal · **hash_sample** (re-hash a sample) · **schema_sample** (structural sanity) · **facts_validation** (every `facts/*.json` valid against its `$schema`) · **manifest_fs_integrity** (every FRESH row has a real md file; no orphans).

### `sift run`
Orchestrates plan → fetch → extract → commit → publish with per-phase timing.
- `--limit N` — cap fetches this run (first-time smoke runs).
- `--rate FLOAT`, `--concurrency N` — as `fetch`.
- `--tier TEXT` — only these tiers (repeatable).
- `--coverage-base MODE` — base the G3 coverage fraction on something other than total manifest rows. `planned` = `min(--limit, total)`; `filtered-tiers` = count of `--tier` rows. **Use this on any intentional capped/tier-scoped crawl** or G3 spuriously degrades.
- `--run-id TEXT` — mint the run-id up front (must be unique; the runs-table PK). Used by `index_url`.
- `--firecrawl-fallback` — as `fetch`.
- `--only-urls PATH` — scope the plan to URLs in a file; rows outside the set are SKIPPED entirely (not planned, not fetched). For targeted backfills — adding one URL won't trigger a full-corpus expansion.

**Exit codes** (for cron/CI): `0` published (gates passed, `current/` flipped) · `1` pipeline error (crashed pre-publish) · `2` degraded (a gate failed; `snapshot.json` written `status=degraded`, `current/` unchanged).

## Operational

### `sift re-extract`
Re-extract every eligible row from its cached raw blob — **no network**. Run after an `EXTRACTOR_VERSION`/`NORMALIZER_VERSION` bump or a profile swap. Idempotent (rows already at current versions are short-circuited); produces proper `old_hash → new_hash` `changed` changelog entries.
- `--run-id TEXT` — default: new timestamp + `-reextract`.
- `--tier TEXT` — restrict to tiers (repeatable).
- `--include-frozen` — also re-extract FROZEN rows (default: FRESH only).
- `--publish / --no-publish` — run publish after commit (default `--publish`).

### `sift backup`
Online SQLite backup of `manifest.db` via `sqlite3.Connection.backup()` — safe under concurrent writes. Writes copy + sha256 + size.
- `--to PATH` — destination (default `<root>/backups/manifest-<UTC>.db`).
- `--keep N` — keep only the N most recent backups in `<root>/backups/` (default: keep all).

### `sift verify-backup BACKUP_PATH`
Verify a backup is a valid, complete SQLite file with the manifest schema (`PRAGMA integrity_check` + schema + row count). Positional `BACKUP_PATH`; `--root` required.

## Integrity verification

### `sift verify`
Run all checks: Merkle root + changelog chain + (optional) GPG. **Exit 0 iff all pass; exit 2 on any failure** with diagnostics.
- `--run-id TEXT` — defaults to the `current/` target.
- `--skip-signature` — skip the GPG check (use when not signing).

### `sift verify-snapshot`
Recompute the Merkle root from the manifest and compare to `snapshot.json`. `O(N)`. `--run-id` defaults to `current/` target.

### `sift verify-changelog`
Walk `changelog.jsonl`, verify each entry's `prev_hash` + `entry_hash`. Any tampering breaks the chain at that point.

### `sift verify-signature`
GPG-verify the `snapshot.json` detach signature (only meaningful if `[publish].gpg_key_id` is set). `--run-id` defaults to `current/` target.

## Read access

### `sift manifest-query SQL`
Read-only `SELECT`/`WITH` against `manifest.db` (refuses anything else). Positional `SQL`.
- `--format [json|tsv]` — default `json`.

### `sift status`
Counts by state + tier and the currently-published run. `--root` only.
