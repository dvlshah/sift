---
name: sift
description: Install, build, operate, and query a sift index — the deterministic, content-hashed, always-current docs corpus that grep-first agents read and cite over MCP. Use when setting up sift (pip install; sift init/seed/run/publish), turning a website or docs site into a verifiable markdown+facts corpus, wiring sift-mcp into a harness via .mcp.json, or refreshing/operating an index. ALSO use whenever a sift MCP server is connected (mcp__sift__* tools snapshot_status, grep_corpus, read_md, read_facts, query_manifest) and you must answer from that corpus and cite the exact source + content_hash + date — begin such sessions with snapshot_status. Triggers - "index this site for my agent", "build/refresh a sift corpus", "set up sift", "query the sift index", "what does <indexed source> say, with proof". Do NOT use for one-off scraping of a single page, general web search, or a source needed only once — sift is for a standing, complete, citable index of an evolving source.
tags: [sift, mcp, indexing, retrieval, provenance, grep-first, crawl, agent-corpus]
author: deval
version: 1.0.0
---

# sift

sift turns a website into a **trustworthy markdown corpus + structured facts** that LLM agents grep, read, and cite on demand. Files on disk, not vectors. Every page is content-hashed and dated, so any answer can be proved back to the exact source and snapshot. Agents read the published `current/` snapshot over a read-only MCP server.

**Why sift instead of a scrape/fetch tool?** A one-off scrape gives you the 3 pages it happened to fetch, with no proof of what they said or when. sift gives you the **complete** crawled corpus of a source (grep the whole thing, not what live-browse stumbled onto) and **provenance** (hash-verifiable, dated content you can cite). Reach for sift when an agent must stay correct about an *evolving* body of knowledge and may need to prove what it said. If the user only needs one page once, this is the wrong tool — use a scrape/fetch tool.

## Two jobs, one router

This skill covers the whole lifecycle. Figure out which job you're in, then jump:

| You want to… | Go to |
|---|---|
| **Answer a question** from a connected sift corpus | [Query a connected corpus](#query-a-connected-corpus) ← most common; start here |
| Install sift on a machine | [Install](#install) |
| **Build a new index** from a website | [Build an index](#build-an-index) |
| Wire sift into Claude Code / Cursor | [Wire into MCP](#wire-into-mcp) |
| Keep an index fresh / run it in production | [Operate & keep fresh](#operate--keep-fresh) |
| Let the agent grow the index on demand | [Write mode](#write-mode-opt-in) |
| Prove / verify an answer | [Provenance & verification](#provenance--verification) |
| Index a brand-new kind of site | [Site profiles](#site-profiles) |
| Something's broken | [Troubleshooting](#troubleshooting) |

**The MCP server ships its own usage instructions** (a short "snapshot_status first, then grep→read, be token-efficient, cite provenance" brief). Those are the floor. This skill is the ceiling: the full build/operate lifecycle the server can't teach (it only exists once an index does), plus the deeper retrieval, citation, and coverage patterns below. Nothing here contradicts the server brief.

### Is sift already here?

- `sift` and `sift-mcp` are console-script entry points created by `pip install` — check with `sift --version` / `which sift-mcp`, and `pip show -f sift` to locate the repo.
- A sift MCP server may already be connected as `sift` (single-index) and/or `sift-multi` (multi-index). When those tools are present, you're in the **Query** job — don't rebuild anything.

---

## Query a connected corpus

The high-frequency job: a `sift` / `sift-multi` MCP server is connected and you must answer from it.

**1. Start by confirming what's available.**
- **Single index:** call `snapshot_status` — confirms a published snapshot and reports the `run_id`, freshness, coverage, gate results, and an artifact inventory. It never errors; if it reports **unpublished**, stop and surface that — the read tools will refuse with a hard error and there's nothing to answer from yet.
- **Multi-index** (`sift-multi`, or any server exposing `list_indexes`): call `list_indexes` **first** — it's both the corpus picker and the cross-index health check (per-index page count, last-published, degraded state). Pick the index by description/domain, then pass `index=<slug>` on every call (`snapshot_status index=<slug>` to drill into one).

If you were invoked with **no specific question** (e.g. a bare `/sift`), stop after this step: report what's available and wait for the actual query — don't run speculative searches.

**2. Locate, then drill — never read the whole corpus.** Every tool output is capped (table below). The efficient loop:

1. `grep_corpus` to find where something is — best for identifiers (section numbers, form codes, anchor names like `{#cents-per-kilometre}`, exact phrases). Use `files_only=true` to narrow to filenames first.
2. `read_md` the hit, using `offset`/`limit` to page through long files instead of re-reading from the top.
3. `read_facts` when the answer is a **number, rate, threshold, or deadline** — facts are atomic structured records with `$schema` + `source_url` + `content_hash`, more reliable than prose.
4. `query_manifest` for cross-cutting questions the filesystem can't answer ("pages changed in the last 7 days", "all FRESH pages under parent_guide X"). SELECT/WITH only. Discover schema with `SELECT sql FROM sqlite_master WHERE type='table'`.
5. `glob_corpus` / `list_dir` to explore the path tree when you don't yet know the shape ("all 2025 forms", what's under `facts/`).
6. `changed_since(since=<run_id>)` to stay current across sessions — remember the `run_id` from `snapshot_status`, then pull only the added/modified/removed pages since it and `read_md` just those, instead of re-reading. Store the new `cursor` it returns.
7. `diff_md(path, from=<run_id>)` to read only the *lines* that changed in a page, not the whole page; and `as_of=<run_id>` on `read_md` / `grep_corpus` / `read_facts` to read a past **published** snapshot — replay/audit, a stable view across a long task, or seeing what a page said before a change.

**Output caps — design your call around them:**

| Tool | Cap | When you hit it |
|---|---|---|
| `read_md`, `read_facts` | 20,000 chars | page with `offset`/`limit` |
| `grep_corpus` | 200 matches | refine the regex, or `files_only=true` |
| `glob_corpus` | 500 paths | narrow the pattern |
| `list_dir` | 500 entries | go one directory deeper |
| `query_manifest` | 500 rows | add `LIMIT` / a tighter `WHERE` |
| `changed_since` | 500 per group | raise `limit`, page with `offset`, or narrow with `path_prefix` / `tier` |
| `diff_md` | 16,000 chars | the hunk truncates on huge pages; lower `context` |

**3. Cite with provenance.** Every markdown file leads with YAML frontmatter carrying `url`, `fetched_at`, `content_hash`, `tier`, and anchors. When you state a fact from the corpus, cite the **source url + `content_hash` + `fetched_at`** (and the `run_id` from `snapshot_status`). That dated, hash-pinned citation is sift's whole point — don't drop it on answers that matter.

**4. Verify before high-stakes citation.** For anything you'll act on or present as authoritative, call `read_md` with `verify=true`. The server re-hashes the body and compares to the frontmatter `content_hash`. Match → prepends `[verify=ok …]`. **Mismatch → `isError`: the file was modified since publish. Treat it as untrusted and do not cite it.**

**5. If the corpus doesn't have it, say so.** When grep/glob/query come up empty, the honest answer is "not in this index" — never backfill from training data and present it as if it came from the source. If write mode is enabled and you know the URL, you may grow the index instead (see [Write mode](#write-mode-opt-in)); otherwise surface the gap.

**Worked example** — "What's the 2025–26 cents-per-kilometre car rate, with proof?"

```
snapshot_status                                  # published? run_id? fresh?
grep_corpus pattern="cents per kilometre" ignore_case=true files_only=true
list_dir path="facts/ato-rate-table-v1/"         # numbers live in facts/
read_facts path="facts/ato-rate-table-v1/cents-per-km-2025-26.json"
# Answer + cite: value, source_url, content_hash, fetched_at, run_id.
# If it's the headline number, also: read_md verify=true on the source page.
```

Full tool schemas (every parameter, default, return shape, multi-index `index="*"` fan-out rules): **`reference/mcp-tools.md`**.

---

## Install

From the **sift repo root** (the directory with `pyproject.toml`; the `sift/` package lives in a subdir). Requires Python ≥ 3.11.

```bash
pip install -e ".[dev,evals]"     # CLI + MCP server + eval suite + test deps
# minimal runtime only: pip install -e .
```

Optional escalation tiers for hardened / JS-rendered sites (the fetch ladder is `native httpx → curl_cffi → browser → Firecrawl`, each tried only on need):

```bash
pip install -e ".[impersonate]"   # tier 2: curl_cffi TLS impersonation — free, no browser
pip install -e ".[browser]" && python -m playwright install chromium   # tier 3: render JS
```

- **`[impersonate]`** (free, self-hosted) defeats most Cloudflare/Akamai/Imperva *fingerprint* blocks — enable with `--impersonate-fallback` or `[crawl.impersonate].enabled`. Try this first for 401/403/429.
- **`[browser]`** renders JS-only pages; ~150–300 MB RAM per render, **off** by default (`[browser].enabled`). Chromium launches lazily on first render, so an enabled-but-unused browser costs nothing; it also degrades gracefully if the dep is missing (the run continues on the other tiers).
- **Firecrawl** (`--firecrawl-fallback`, paid) is the last resort for JS-challenge edges; never fires on thin content unless `[crawl.firecrawl].escalate_on_thin=true`.

Entry points after install: `sift` (pipeline CLI), `sift-mcp` (MCP server), `sift-evals` (eval harness). Verify with `sift --help`.

---

## Build an index

Turn a site into a published corpus. Copy-paste quickstart, then the detail:

```bash
sift init   --root ./index
sift seed   --root ./index --from-sitemap https://example.com/sitemap.xml
sift run    --root ./index --limit 50          # smoke test first — see below
sift run    --root ./index                     # full crawl once happy
sift verify --root ./index --skip-signature
sift-mcp    --root ./index                     # serve it
```

**Step 0 — pick a profile and lock the host.** The profile owns per-site URL classification, facts schemas, and browser routing; `host_allow` is the safety boundary for what gets crawled. In `sift.toml`:

```toml
[site]
profile = "sift.sites.generic:GenericProfile"   # safe default for any site
[seed]
host_allow = ["example.com"]
```

Shipped profiles: `ato` (default), `generic`, `generic_browser` (every URL via browser), `augov`/`augov:SAGovProfile`, `stripe`, `mdn`, `python_docs`. Use `generic` for an arbitrary site — every URL becomes `LIVING`, no facts, no browser. See [Site profiles](#site-profiles) to add one.

**Step 1 — seed URLs.** Pick the discovery source that fits what you know:

| You have… | Use |
|---|---|
| a sitemap URL | `--from-sitemap https://site/sitemap.xml` (recurses sitemap-indexes) |
| just the domain | `--from-domain https://site` (auto-finds sitemaps via robots.txt + well-known paths) |
| no/sparse/bot-blocked sitemap | `--from-firecrawl-map https://site` (needs `FIRECRAWL_API_KEY`; `--firecrawl-search KW` to scope) |
| a discovery JSON dump | `--from-json links.json` |

Sources combine in one invocation. Scope with `--host-allow`, `--exclude REGEX`, `--no-default-excludes`.

**Step 2 — run the pipeline.** `sift run` does plan → fetch → extract → commit → publish with per-phase timing. **Always smoke-test first** with `--limit 50` (and a polite `--rate`) to confirm extraction quality before committing to thousands of fetches. Scope by refresh tier with repeatable `--tier` (`FROZEN` / `CURRENT_FORMS` / `LIVING` / `NEWS`).

> **Capped-crawl gotcha (real footgun):** the publish **coverage gate (G3)** requires ≥99% of *seeded* URLs to reach a terminal state. An intentional `--limit` or `--tier` run leaves most seeds un-fetched, so G3 fails and publish **degrades** (exit 2, `current/` does not flip). Tell the gate it was intentional: `--coverage-base planned` (with `--limit`) or `--coverage-base filtered-tiers` (with `--tier`).

**Exit codes** (`sift run`, for cron/CI): `0` published (gates passed, `current/` flipped) · `1` pipeline error (crashed before publish) · `2` degraded (a gate failed; `snapshot.json` written with `status=degraded`, `current/` unchanged).

Scale reference: a full ATO crawl (~8,000 URLs) runs ~30 min at 5 req/s and lands ~770 MB. Plan disk and politeness accordingly.

Full CLI (every subcommand + flag, per-phase commands, `re-extract`/`backup`/`verify*`): **`reference/cli.md`**.

---

## Wire into MCP

`sift-mcp` speaks MCP over stdio and is **read-only by default**. Point `--root` at the index root that contains the `current/` symlink.

```json
{
  "mcpServers": {
    "sift": {
      "command": "sift-mcp",
      "args": ["--root", "/abs/path/to/index"]
    }
  }
}
```

- **Multi-index:** point `--root` at a **parent directory** containing several index roots. The server auto-detects this (no flag), exposes `list_indexes`, and adds an `index=<slug>` parameter to every content tool. Mode is frozen at startup. Tune freshness-vs-cost of newly-built sub-indexes with `--registry-ttl-ms` (default 1000).
- **Write tools:** add `--enable-index` to expose `index_url`/`index_status` (off by default — see [Write mode](#write-mode-opt-in)).
- Use **absolute paths** in `args`. The server hard-fails read tools with an actionable message if no `current/` snapshot exists yet.

---

## Operate & keep fresh

- **Refresh** = re-run `seed` + `run` on a schedule. Conditional GETs mean only changed pages are re-fetched and re-extracted; unchanged pages are skipped cheaply.
- **Cron** (high-churn nightly, broad weekly, plus backup):

  ```cron
  0 17 * * *  cd /srv/index && sift seed --root . --from-sitemap https://example.com/sitemap.xml && sift run --root . --tier NEWS --tier LIVING
  0 16 * * 0  cd /srv/index && sift run --root . --tier NEWS --tier LIVING --tier CURRENT_FORMS
  0 4  * * *  sift backup --root /srv/index --keep 14
  ```

- **After bumping `EXTRACTOR_VERSION` / `NORMALIZER_VERSION`** (changed extraction logic, not a refetch): `sift re-extract --root ./index`. Re-derives `content_hash`es from cached raw blobs — **no network** — preserves the changelog, emits `changed` diffs, and is idempotent. Add `--include-frozen` to also refresh historical rows.
- **Backups:** `sift backup` is an online SQLite backup (safe under concurrent writes); `sift verify-backup` runs `PRAGMA integrity_check` + schema sanity. Pair with `rclone`/`rsync` for off-machine copies — there is no built-in remote storage.
- **Known gaps** (v0.1.0): no run-dir/raw-blob GC (manual `rm -rf runs/<old>` + manifest `VACUUM`); stdout logging only (pipe through a log shipper); MCP is stdio-only (wrap with an HTTP/MCP proxy to host it); only the rate-table facts extractor is wired.

---

## Write mode (opt-in)

Lets a connected agent grow the index when the corpus is missing something. Launch the server with `sift-mcp --root R --enable-index`. Adds two tools:

- `index_url` — seed 1–20 absolute `http(s)` URLs and trigger an incremental background crawl. Returns a `run_id` **immediately**.
- `index_status` — poll that `run_id`. Status is `running` → `succeeded` / `degraded` / `failed`. Completed runs are durable in the manifest (queryable forever); in-progress jobs don't survive a server restart.

**The coverage loop:** `grep_corpus`/`glob_corpus`/`query_manifest` come up empty → `index_url(urls=[…])` → poll `index_status(run_id)` until `succeeded` → `read_md` the new pages (no separate publish step). Or watch `list_indexes`' `unseen_count` for the gap.

**Guardrails (don't fight them):** every URL's host must be in the target index's `seed.host_allow` — off-list URLs are refused with the accepted hosts listed. One crawl per index at a time; cross-index concurrency is bounded by `--max-concurrent-crawls` (default 4). Write mode is deliberately **off** in strict/hosted deployments so every call is a read.

---

## Provenance & verification

This is the wedge — give it weight on answers that matter.

- **Frontmatter on every md file:** `url`, `fetched_at`, `content_hash`, raw hash, `tier`, `audience`, `fy_years`, `anchors`, and four version pins (crawler/extractor/normalizer/classifier). That's your citation payload.
- **Per-read trust:** `read_md verify=true` re-hashes the body vs. the stored `content_hash` (see [Query](#query-a-connected-corpus) step 4).
- **Corpus-level integrity (CLI):**

  | Command | Proves |
  |---|---|
  | `sift verify --root R --skip-signature` | runs all checks (drop the flag only if GPG signing is configured) |
  | `sift verify-snapshot` | recomputes the Merkle root from the manifest and compares to `snapshot.json` |
  | `sift verify-changelog` | walks the hash-chained changelog (`entry_hash = sha256(prev_hash ‖ entry)`) |
  | `sift verify-signature` | GPG-verifies the `snapshot.json` detach signature (if `[publish].gpg_key_id` set) |

---

## Site profiles

A `SiteProfile` isolates everything site-specific (URL→tier classification, `parent_guide` extraction, default excludes, dynamic-content patterns stripped before hashing, section taxonomy, facts schemas + extractors, and browser routing) from the generic pipeline. The core never names a site.

Use `generic` for any site with no special structure. Add one only when you need real classification or facts:

```python
# sift/sites/irs.py
import re
from . import SiteProfile

class IRSProfile(SiteProfile):
    name = "irs"
    primary_host = "www.irs.gov"
    @property
    def default_excludes(self): return (r"^/coronavirus/", r"^/spanish/")
    def classify_tier(self, url, current_year_start): ...   # IRS uses calendar years
    def audience(self, url): ...                            # /individuals/, /businesses/
```

Then set `profile = "sift.sites.irs:IRSProfile"` in `sift.toml`, reseed, and run — no core changes. Annotated `sift.toml` and the full profile contract: **`reference/config.md`**.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Read tools return "No published snapshot at `<root>/current/`" | pipeline never completed a passing publish | `snapshot_status` for gate detail; finish `sift run`; for capped runs see coverage gate below |
| `sift run` exits **2** / G3 coverage gate failed on a deliberately partial crawl | most seeded URLs not in a terminal state | `--coverage-base planned` (with `--limit`) or `filtered-tiers` (with `--tier`) |
| SPA page is empty or missing | browser fetch off; URL was `SKIPPED_BROWSER_DISABLED` | `pip install -e ".[browser]" && playwright install chromium`, set `[browser].enabled=true`, ensure the profile routes it |
| Fetch fails with 401/403/429 | bot protection on the source | add `--impersonate-fallback` first (free, `[impersonate]` extra — clears most fingerprint blocks); then `[browser].enabled=true` for JS; then `--firecrawl-fallback` (paid) for JS-challenge edges. Kasada-class sites remain out of reach |
| 200 but extracted page is empty (SPA shell / challenge) | content is JS-rendered or a soft block | the content-quality trigger auto-escalates when a tier is wired — add `--impersonate-fallback` and/or `[browser].enabled=true` |
| `read_md verify=true` → `isError` (hash mismatch) | file changed since publish — untrusted | re-publish from a clean run; do **not** cite the file |
| Multi-index tool errors that an index is required | called a content tool without `index=` | `list_indexes`, then pass `index=<slug>` (`read_md`/`read_facts` always require it) |
| `index_url` refused / "not writeable" | server lacks `--enable-index`, host not in `seed.host_allow`, or the sub-index's `sift.toml` has no `[seed].host_allow` | enable the flag; add the host; give the sub-index an allow-list and restart |

---

## Reference

Read on demand — keep `SKILL.md` lean:

- **`reference/cli.md`** — every `sift` subcommand and flag (pipeline phases, `re-extract`, `backup`, the `verify*` family, `manifest-query`, `status`).
- **`reference/mcp-tools.md`** — every MCP tool: parameters, defaults, return shapes, output caps, and the single- vs multi-index (`index=` / `index="*"`) rules.
- **`reference/config.md`** — annotated `sift.toml` (every section + key) and the `SiteProfile` contract.

Architecture, integrity model, and the full design narrative live in the repo `README.md`.
