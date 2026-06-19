<div align="center">

<img src="assets/sift.gif" alt="sift" width="480">

**Deterministic, content-hashed website indexing for grep-first AI agents — served over MCP.**

[![Tests](https://github.com/dvlshah/sift/actions/workflows/tests.yml/badge.svg)](https://github.com/dvlshah/sift/actions/workflows/tests.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-stdio-purple)](https://modelcontextprotocol.io/)

</div>

sift turns any website you can reach by URL into a complete, always-current, **verifiable** corpus that an AI agent reads over MCP — files on disk, not vectors. Every page is content-hashed and dated, so any answer can be proved back to the exact source, hash, and snapshot. Self-hosted: your data and your proof stay yours.

- **Provable** — same input → same `content_hash` → same Merkle root; a hash-chained changelog; optional GPG-signed snapshots; per-read `verify=true`.
- **Any site, self-hosted** — point it at any `http(s)` site (static HTML, or JS-rendered SPAs via the optional browser path). A pluggable `SiteProfile` handles per-site logic with no core changes.
- **Complete & grep-native** — the full crawled corpus as markdown + structured facts that agents `read` / `grep` / `glob` / query — not a few browsed pages, not opaque vector similarity.
- **Incremental & low-ops** — conditional GETs re-extract only what changed; bump a transformer version and re-derive from cached raw with no refetch.

> **Open core.** This repo is the open-source engine (pipeline + MCP server), Apache-2.0, and runs fully on its own. A hosted platform built on it is in development.

### Two ways to start

- 🤖 **Hand it to your coding agent** — paste one prompt; it installs sift, builds an index, wires MCP, and learns to query. → **[For your coding agent](#-for-your-coding-agent)**
- 🧑 **Do it yourself** — manual CLI quickstart, plus the full architecture / CLI / config / integrity reference. → **[For humans](#-for-humans)**

---

# 🤖 For your coding agent

Everything an agent needs: a one-paste setup prompt, the read loop it follows at runtime, and the skill that is its deep reference.

## One-paste setup

Paste the block below into your coding agent — **Claude Code, Cursor, Codex, or any MCP-aware agent**. It first explains sift in plain terms and asks which site you want to index, then installs the engine, drops the sift skill into your repo, builds a starter index, wires the read-only MCP server, and shows you how to query it — end to end.

````text
Set up **sift** in this project — a deterministic, content-hashed website indexer that
serves a verifiable markdown corpus to AI agents over MCP (https://github.com/dvlshah/sift).
Do the steps in order. Needs Python 3.11+.

0 — EXPLAIN, THEN ASK ME (do this BEFORE installing anything)
First, explain sift to me in 4–6 plain-English lines — assume I've never heard of it:
  • WHAT it is: it crawls a whole website/docs site into local markdown files (plus structured
    facts) that you, the agent, can grep / read / cite — files on disk, not a vector database.
  • CORE FEATURES: (1) complete — the full site, not the few pages a live web-fetch happens to
    land on; (2) verifiable — every page is content-hashed + dated, so any answer can be proved
    back to the exact source and snapshot; (3) always-current — re-running the crawl refetches
    only what changed; (4) read over MCP — you query it with provenance, read-only by default.
  • WHY IT'S NEEDED: a one-off scrape gives you 3 pages with no proof of what they said or when.
    sift keeps an agent correct about an evolving body of docs AND able to prove what it cited.
    (If someone only needs one page once, sift is the wrong tool — a plain fetch is fine.)
Then ASK ME: "Which website or docs site do you want to index locally?" — wait for my answer
and use it as TARGET_SITE everywhere below. Do not run any commands until I answer.

1 — INSTALL THE ENGINE
```bash
pip install sift-engine          # adds the `sift`, `sift-mcp`, `sift-evals` commands
sift --version && which sift-mcp  # confirm it's on PATH
```

2 — INSTALL THE SIFT SKILL INTO THIS REPO
Download the skill so you (the agent) know how to build, operate, and query an index.
Read .claude/skills/sift/SKILL.md after downloading — it is the source of truth for the rest.
```bash
SKILL=https://raw.githubusercontent.com/dvlshah/sift/main/.claude/skills/sift
mkdir -p .claude/skills/sift/reference
curl -fsSL $SKILL/SKILL.md -o .claude/skills/sift/SKILL.md
for f in cli config mcp-tools; do curl -fsSL $SKILL/reference/$f.md -o .claude/skills/sift/reference/$f.md; done
```

3 — BUILD A SMALL STARTER INDEX (using the TARGET_SITE I gave you in step 0)
Write a sift.toml (generic profile + host allow-list = the host of TARGET_SITE), then build a
capped, publishable smoke-test index:
```bash
cat > sift.toml <<'TOML'
[site]
profile = "sift.sites.generic:GenericProfile"
[seed]
host_allow = ["HOST_OF_TARGET_SITE"]      # e.g. docs.example.com — derive from TARGET_SITE
TOML
sift init   --root ./sift-index
sift seed   --root ./sift-index --config sift.toml --from-domain TARGET_SITE
sift run    --root ./sift-index --config sift.toml --limit 25 --coverage-base planned
sift verify --root ./sift-index --skip-signature
```
Drop `--limit 25 --coverage-base planned` for a full crawl once extraction looks good.
Bot-blocked host → add `--firecrawl-fallback` (needs FIRECRAWL_API_KEY).
JS-rendered SPA → `pip install 'sift-engine[browser]' && python -m playwright install chromium`,
then set profile = "sift.sites.generic_browser:GenericBrowserProfile" and add `[browser]\nenabled = true`.

4 — WIRE THE READ-ONLY MCP SERVER
Use the ABSOLUTE path to ./sift-index. Add this to the project's .mcp.json (Claude Code / Cursor / Codex):
```json
{ "mcpServers": { "sift": { "command": "sift-mcp", "args": ["--root", "ABSOLUTE/PATH/TO/sift-index"] } } }
```
Claude Code shortcut: `claude mcp add sift -- sift-mcp --root "$(pwd)/sift-index"`
Then restart the MCP client so the `sift` tools load.

5 — SHOW ME THE QUERY LOOP
Call the `snapshot_status` tool to confirm the index is published, then explain the loop:
snapshot_status first → grep_corpus to locate → read_md / read_facts to drill in →
cite source_url + content_hash + fetched_at. Mention that re-running `sift run` refreshes the
index, and that the /sift skill covers building, operating, and querying in depth.

(TARGET_SITE = the site I name when you ask in step 0, e.g. https://docs.example.com)
````

Prefer to do it by hand? The same steps, unrolled, are in the [Quickstart](#quickstart).

## How your agent queries the corpus

Once `sift-mcp` is wired, the agent works the index **read-only** over 7 tools. The loop: **`snapshot_status` first → `grep_corpus` to locate → `read_md` / `read_facts` to drill in → cite `source_url` + `content_hash` + `fetched_at`.** Every output is capped — locate, then drill; never read the whole corpus.

| Tool | Purpose |
|---|---|
| `snapshot_status` | Published yes/no, run_id, gate results, artifact inventory. **Call first.** Never errors. |
| `grep_corpus` | Regex over the markdown tree — best for identifiers/exact phrases (capped at 200 matches) |
| `read_md` | Read one markdown file (`offset`/`limit` to page; `verify=true` re-hashes before you cite) |
| `read_facts` | Read one `facts/<schema>/*.json` with `$schema` + `source_url` + `content_hash` provenance |
| `glob_corpus` | List files by fnmatch glob (capped at 500) |
| `list_dir` | Cheap directory enumeration |
| `query_manifest` | Read-only SQL against `manifest.db` for cross-cutting queries |

For high-stakes citations, `read_md verify=true` re-hashes the body against the stored `content_hash`; a mismatch returns `isError` (the file changed since publish — don't cite it).

## The /sift skill

Step 2 of the setup prompt drops the **`/sift` skill** into `.claude/skills/sift/`. That's the agent's deep reference — the full build → operate → query lifecycle the MCP server can't teach (it only exists once an index does), plus the retrieval, citation, and coverage patterns. The skill is what your agent reads; this README is for you.

---

# 🧑 For humans

[Scope](#scope) · [Quickstart](#quickstart) · [What you get](#what-you-get) · [Architecture](#architecture) · [CLI](#cli-reference) · [Config](#configuration) · [MCP](#mcp-server-deploy-it) · [Integrity](#integrity-guarantees) · [Profiles](#site-profiles) · [Develop](#develop)

## Scope

**Today:** any `http(s)` URL — HTML pages and PDFs. Discover URLs from a `sitemap.xml`, whole-domain sitemap auto-discovery, a Firecrawl map, or a plain URL list. JS-rendered SPAs go through the optional Playwright path; bot-blocked or rate-limited hosts through the optional Firecrawl fallback. Works on public sites and on internal ones your machine can reach (add the host to the allow-list).

**Not yet (roadmap — and good first contributions):** non-URL sources — local files and folders, git repos, API-only knowledge bases (Notion, Confluence, Slack, Google Drive), and databases. The pipeline is source-agnostic once content is in, so these land as ingestion *connectors*.

## Quickstart

Requires Python 3.11+.

```bash
pip install sift-engine

# 1. create an index root
sift init --root ./index
# 2. seed URLs — ships with an ATO reference profile that needs no config
sift seed --root ./index --from-sitemap https://www.ato.gov.au/sitemap.xml
# 3. build a small index first — --coverage-base planned = the --limit cap is intentional
sift run --root ./index --limit 25 --coverage-base planned
# 4. verify end-to-end integrity
sift verify --root ./index --skip-signature
# 5. serve it to an agent over MCP (read-only)
sift-mcp --root ./index
```

**Indexing a different site?** Drop a `sift.toml` next to your index with the generic profile + host allow-list:

```toml
[site]
profile = "sift.sites.generic:GenericProfile"

[seed]
host_allow = ["docs.example.com"]
```

```bash
sift seed --root ./index --config sift.toml --from-domain https://docs.example.com
sift run  --root ./index --config sift.toml --limit 25 --coverage-base planned
```

Drop `--limit` / `--coverage-base` for a full crawl. JS-rendered SPAs need the optional browser stack: `pip install 'sift-engine[browser]' && python -m playwright install chromium`.

## What you get

After a run, the index root contains:

```
<root>/
├── manifest.db                  SQLite — single source of truth for URL state
├── raw/<aa>/<sha256>.html.gz    Content-addressed raw HTML/PDF blobs
├── changelog.jsonl              Append-only, hash-chained per-content-change log
├── current/                     Symlink → the most-recent passing snapshot
├── runs/<run_id>/
│   ├── INDEX.md                 Always-loaded pointer table for agents
│   ├── routes.tsv               url → md_path map (grep/awk friendly)
│   ├── sections/<top>/INDEX.md  Per-section drill-down indexes
│   ├── md/<url-path>.md         Markdown mirror of the URL tree
│   ├── facts/<schema>/*.json    Atomic structured records (rate tables, etc.)
│   ├── artifacts/by_guide/*.md  Multi-page guide rollups
│   └── snapshot.json            Gate results, version pins, Merkle root, gpg sig (opt)
└── backups/manifest-*.db        Online SQLite backups (run on cron)
```

Every markdown file leads with YAML frontmatter: URL, fetch timestamp, raw + content hashes, tier, audience, FY years, anchors, and four version pins (crawler, extractor, normalizer, classifier). Re-verify any file in `O(1)` by re-normalizing the body and comparing its SHA-256 to the stored `content_hash`.

## Architecture

Five sequential phases, each idempotent and resumable from a checkpoint:

```
 seed   ──►  Add URLs to the manifest (tier + parent_guide assigned per site profile)
 plan   ──►  Per-URL decision: FETCH / FETCH_CONDITIONAL / SKIP / TOMBSTONE_PURGE
             (pure function of manifest state, sitemap lastmod, clock, versions)
 fetch  ──►  HTTP (async httpx + per-host token bucket + conditional GETs) or,
             per profile, the Playwright browser path. Raw stored by SHA-256.
 extract──►  HTML→markdown (trafilatura) / PDF→text (pypdf); deterministic
             anchor injection + hash normalization → content_hash
 commit ──►  One SQLite transaction applies all outcomes; appends chained
             entries to changelog.jsonl per content change
 publish──►  5 verification gates → atomic symlink swap to current/;
             Merkle root over all content_hashes written to snapshot.json
```

Each transformation is versioned independently (`CRAWLER_VERSION`, `EXTRACTOR_VERSION`, `NORMALIZER_VERSION`, `CLASSIFIER_VERSION`, `INTEGRITY_VERSION`) — bump one and `sift re-extract` re-derives from cached raw with no network. Failures are contained per-URL: one bad page never breaks a snapshot, and the coverage gate blocks publish if too many URLs are non-terminal.

## CLI reference

`--root` is required on every command; `--config PATH` (default `./sift.toml` / `./sift.local.toml`) is accepted on the pipeline commands. CLI flags override config.

**Pipeline**

| Command | Purpose |
|---|---|
| `sift init` | Create `manifest.db`; surface changelog state |
| `sift seed` | Add URLs via `--from-sitemap` / `--from-domain` / `--from-firecrawl-map` / `--from-json` |
| `sift plan` / `fetch` / `extract` / `commit` | Run a single phase (`--run-id` for fetch/extract/commit) |
| `sift run` | plan → fetch → extract → commit → publish, with per-phase timings (`--limit`, `--tier`, `--rate`, `--coverage-base`, `--firecrawl-fallback`, `--only-urls`) |
| `sift publish --run-id ID` | 5 verification gates + atomic symlink swap |
| `sift status` | Counts by state + tier, version pins, recent runs |

**Operational**

| Command | Purpose |
|---|---|
| `sift re-extract` | Re-derive `content_hash`es from cached raw (no network); preserves the changelog. Run after an extractor/normalizer version bump |
| `sift purge` | Drop manifest rows whose plan decision is `TOMBSTONE_PURGE` (`--dry-run` to preview) |
| `sift backup [--to PATH] [--keep N]` | Online SQLite backup, safe under concurrent writes |
| `sift verify-backup BACKUP` | `PRAGMA integrity_check` + schema sanity on a backup |

**Integrity & read access**

| Command | Purpose |
|---|---|
| `sift verify [--skip-signature]` | Merkle root + changelog chain + optional GPG, in one |
| `sift verify-snapshot` / `verify-changelog` / `verify-signature` | The individual integrity checks |
| `sift manifest-query "SELECT ..."` | Read-only SQL against `manifest.db` (refuses non-`SELECT`/`WITH`) |

## Configuration

A single TOML file (`sift.toml` in cwd, or `--config PATH`) controls everything tunable:

```toml
[site]
profile = "sift.sites.ato:ATOProfile"   # or sift.sites.generic:GenericProfile

[fy]
current_start_year = 2025                # FY cutoff for the FROZEN tier

[crawl]
rate_per_sec = 5.0                       # per-host token bucket
concurrency  = 8
timeout_sec  = 30.0
retries      = 3

[publish]
coverage_floor   = 0.99                  # fraction of seeded URLs that must reach a terminal state
hash_sample_rate = 0.01                  # 1% of md files re-hashed each publish
gpg_key_id       = ""                    # optional: detach-sign snapshot.json

[seed]
host_allow             = ["www.ato.gov.au"]
use_default_excludes   = true
extra_exclude_patterns = ["^/other-languages/"]

[browser]                                # optional; only used if a profile opts a URL in
enabled        = false                   # default off → SPAs become SKIPPED_BROWSER_DISABLED
wait_until     = "domcontentloaded"      # profiles can override (ATO uses "networkidle")

# [tiers.NEWS] / [tiers.LIVING] / [tiers.CURRENT_FORMS] / [tiers.FROZEN]
# each: floor_days, ceiling_days, tombstone_ttl_days, max_failures
```

## MCP server (deploy it)

`sift-mcp --root /path/to/index` serves the index over stdio, **read-only by default** — the 7 tools and the read loop are in [How your agent queries the corpus](#how-your-agent-queries-the-corpus). Point `--root` at the index root that holds the `current/` symlink, and use **absolute paths**; the server hard-fails reads with an actionable message if no `current/` snapshot exists yet.

```json
{
  "mcpServers": {
    "sift": { "command": "sift-mcp", "args": ["--root", "/abs/path/to/index"] }
  }
}
```

- **Multi-index** — point `--root` at a *parent directory* of several index roots; the server auto-exposes `list_indexes` plus an `index=<slug>` parameter on every content tool (`index="*"` fans out the reads).
- **Write tools** — `--enable-index` exposes `index_url` (seed allow-listed URLs + background crawl, returns a `run_id` immediately) and `index_status` (poll by `run_id`). One crawl per index, capped across indexes by `--max-concurrent-crawls` (default 4); each is an isolated `sift seed && sift run` subprocess, so a failed fetch can't take down the read server. Off by default — the standard deployment is strictly read-only.

## Integrity guarantees

| Property | Mechanism | Verified by |
|---|---|---|
| Same input → same `content_hash` | Deterministic `extract` + versioned `normalize_for_hash` | `tests/test_integrity.py`, `sift-evals determinism` |
| Snapshot is bit-identical to publish time | Merkle root over all `(url, content_hash)` in `snapshot.json` | `sift verify-snapshot` |
| Changelog hasn't been tampered with | SHA-256 chain: `entry_hash = sha256(prev_hash ‖ canonical(entry))` | `sift verify-changelog` |
| Per-file integrity on agent reads | `read_md verify=true` re-hashes the body vs. frontmatter | MCP returns `isError` on mismatch |
| Every FRESH row has a real md file | Publish gate `manifest_fs_integrity` | publish blocks on orphan/missing files |
| Every `facts/*.json` validates against its `$schema` | Publish gate `facts_validation` (Draft 2020-12) | publish blocks on invalid facts |
| Optional cryptographic signature | `[publish].gpg_key_id` → `gpg --detach-sign` | `sift verify-signature` |

**Known gaps:** no content-pinning against the source server (TLS is the fetch-time root of trust); the MCP per-read hash isn't chained back to the GPG signature automatically; no built-in off-machine storage (pair `sift backup` with `rclone`/`rsync`).

## Site profiles

Every site-specific decision lives in a `SiteProfile` subclass under `sift/sites/` — URL→tier classification, `parent_guide` extraction, default excludes, dynamic-content patterns stripped before hashing, section taxonomy, facts schemas, and browser routing. The core pipeline never names a site. Ships `generic` (every URL `LIVING`, no facts, HTTP only — the right starting point for any site), `generic_browser`, and reference profiles (`ato`, `augov`, `mdn`, `python_docs`, `stripe`); the default is `sift.sites.ato:ATOProfile` (~330 lines).

Adding a site is usually a small subclass — no core changes:

```python
# sift/sites/irs.py
import re
from . import SiteProfile

class IRSProfile(SiteProfile):
    name = "irs"
    primary_host = "www.irs.gov"

    @property
    def default_excludes(self):
        return (r"^/coronavirus/", r"^/spanish/")

    def classify_tier(self, url, current_year_start):
        ...   # IRS uses calendar years, not FY
```

Then set `profile = "sift.sites.irs:IRSProfile"` in `sift.toml`, reseed, and run.

## Develop

```bash
pip install -e ".[dev,evals]"   # runtime + test + eval-suite deps
pytest -q                        # full suite — hermetic (HTTP mocked), no network needed
ruff check . && ruff format .    # lint + format
```

The optional eval harness is the `sift-evals` CLI (installed via the `[evals]` extra) — performance, determinism, structural-fidelity, facts, and agent-in-the-loop benchmarks (`sift-evals --help`). See **[CONTRIBUTING.md](./CONTRIBUTING.md)** for the full guide: conventional commits, the `SiteProfile` extension path, the determinism invariant, and CI (every PR runs the suite on Python 3.11 / 3.12 / 3.13).

## Project status

**0.1.0 — initial public release.** Full test suite green on Python 3.11–3.13. Known limitations (PRs welcome):

- No run-dir / raw-blob garbage collection yet — storage grows; reclaim with `rm -rf runs/<old>` + manifest `VACUUM`.
- Logging is stdout-only (no structured logging); no alerting beyond cron exit codes.
- MCP transport is stdio only — wrap with an HTTP/MCP proxy to host it.
- One facts extractor is wired (rate tables); other schemas exist without extractors.
- Kasada-class anti-bot remains out of reach; the Firecrawl path handles most Cloudflare/Akamai.

---

## Contributing

Bug reports and features via [GitHub Issues](https://github.com/dvlshah/sift/issues/new/choose); see [CONTRIBUTING.md](./CONTRIBUTING.md). Found a security issue? Follow the private disclosure process in [SECURITY.md](./SECURITY.md) — please don't open a public issue.

## License

[Apache-2.0](./LICENSE) — Copyright © 2026 Deval Shah.
