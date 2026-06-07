# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

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
