# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] — Tiered fetch transport

Crawl hardened (bot-managed) and JS-rendered sites with a self-hosted-by-default
escalation ladder, and stop silently indexing empty SPA shells.

### Added
- **Tiered fetch transport** — per URL, sift now escalates only on need:
  `native httpx → curl_cffi (TLS impersonation) → headless browser → Firecrawl`.
  curl_cffi defeats most Cloudflare/Akamai/Imperva fingerprint blocks for free,
  no browser; the browser is a real escalation rung (not just profile routing);
  Firecrawl is the optional paid last resort.
- **Content-quality escalation trigger** — a `2xx` response carrying an empty SPA
  shell or a JS-challenge interstitial (previously committed as junk) is detected
  (`sift.quality.looks_thin`) and routed up the ladder instead.
- **Adaptive per-host floor** — after a host repeatedly blocks the native fetcher,
  its remaining URLs skip the doomed round-trip (and its 429/503 retry-backoff)
  and start at the escalation ladder. Pure speed heuristic; correctness unchanged.
- **`[impersonate]` extra** (`pip install 'sift-engine[impersonate]'`, curl_cffi)
  and the `--impersonate-fallback` flag on `sift run` / `sift fetch`.
- **Config** — `[crawl.impersonate]` section, `[crawl].thin_text_threshold`, and
  `[crawl.firecrawl].escalate_on_thin` (keep thin pages off the paid tier by default).

### Changed
- The browser tier degrades gracefully: a fallback-only run no longer hard-fails
  at startup when Playwright is missing (profile-required SPA URLs still fail fast).
- README slimmed to a landing page; the one-paste agent setup pins the `/sift`
  skill fetch to a release tag.

### Fixed
- `sift-mcp` reports sift's own version, not the MCP SDK's.

[0.2.0]: https://github.com/dvlshah/sift/releases/tag/v0.2.0

## [0.1.0] — Initial public release

First public release of the sift engine.

### Added
- **Deterministic pipeline** — `seed → plan → fetch → extract → commit → publish`, each phase idempotent and resumable from a checkpoint. Same input → same `content_hash` → same Merkle root.
- **CLI** (`sift`) — `init`, `seed` (sitemap / whole-domain / Firecrawl-map / URL-list discovery), the per-phase commands, `run` (end-to-end), `re-extract` (re-derive from cached raw, no network), `status`, `purge`, `backup` / `verify-backup`, `manifest-query`, and the `verify` family (snapshot Merkle root, changelog chain, optional GPG signature).
- **MCP server** (`sift-mcp`) — 7 read-only tools (`snapshot_status`, `grep_corpus`, `read_md`, `read_facts`, `glob_corpus`, `list_dir`, `query_manifest`). Multi-index mode adds `list_indexes` + per-call `index=<slug>` routing. Opt-in write tools (`index_url`, `index_status`) behind `--enable-index`.
- **Integrity** — content-hashed pages, hash-chained `changelog.jsonl`, a Merkle root in `snapshot.json`, optional GPG-signed snapshots, and per-read hash verification (`read_md verify=true`).
- **Site profiles** — a pluggable `SiteProfile` isolates all per-site logic from the core pipeline; ships `generic`, `generic_browser`, and reference profiles (`ato`, `augov`, `mdn`, `python_docs`, `stripe`).
- **Optional extras** — Playwright browser-fetch for JS-rendered SPAs (`[browser]`); Firecrawl map/scrape fallback for sitemap-less or bot-protected hosts.
- **Eval suite** (`sift-evals`, `[evals]` extra) — performance, determinism, structural-fidelity, facts, and agent-in-the-loop benchmarks.

[0.1.0]: https://github.com/dvlshah/sift/releases/tag/v0.1.0
