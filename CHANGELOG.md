# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`changed_since` MCP tool** — the temporal diff feed. Given a cursor (a
  `run_id` from `snapshot_status`, or an ISO-8601 timestamp), returns the net
  added / modified / removed pages up to the current published snapshot, read
  from the hash-chained `changelog.jsonl`. Lets an agent stay current by pulling
  only the delta instead of re-reading the corpus, then storing the new cursor.
  The window is bounded to the **published** snapshot, so transitions from a
  later unpublished/degraded run never leak in (the delta matches what `read_md`
  serves). Fan-out-eligible in multi-index mode; the server `instructions` now
  teach the cursor loop.
- **`diff_md` MCP tool** — unified diff of one page between two published
  snapshots ("the Difference Engine"). Returns only the changed hunks plus both
  content_hashes and a +/- summary, so an agent reads the lines that moved, not
  the whole page. Pairs with `changed_since`: which pages moved → which lines.
- **`as_of` time-travel reads** — `read_md` / `grep_corpus` / `glob_corpus` /
  `list_dir` / `read_facts` take an optional `as_of` (run_id or ISO-8601
  timestamp) to read a past **published** snapshot from the retained run history
  ("Flux Capacitor") — for replay/audit, a stable view across a long task, or
  inspecting a page before a change.
- **`prove` MCP tool + `sift prove` / `sift verify-proof` CLI + a standalone
  stdlib verifier** — proof-carrying answers. `prove` emits a self-contained
  Merkle **inclusion proof** that a page's `content_hash` is committed by a
  published snapshot's `merkle_root`; a third party verifies it offline with
  `python -m sift.verify_proof <file>` (no sift install, no trust in the server).
  The prover reconstructs the snapshot's leaf set from the run's md tree —
  **falling back to the manifest** — and refuses unless it reproduces the stored
  root exactly, so a proof can only attest the published commitment. Composes
  with `as_of` (prove a past snapshot). Scope is stated honestly: *membership +
  dated byte-integrity*, **not** non-membership or "current truth"
  (`SECURITY.md`). Stress-tested across 33 live indexes (33,227 pages, 0
  failures). Read surface is now ten tools.
- **RFC-3161 external timestamp anchor** — set `[publish].timestamp_tsa_url` and
  every publish obtains a signed Time-Stamp Token over the `merkle_root` from a
  third-party Time-Stamp Authority (e.g. DigiCert), stored at
  `runs/<id>/merkle_root.tsr`. It turns the snapshot's date from
  operator-self-asserted into an **independent witness** — the root can't be
  back-dated past the TSA's signature. `prove` embeds the token in the envelope;
  `sift verify-proof` checks it inline; new `sift verify-timestamp` checks a
  snapshot's token directly; all are verifiable with plain `openssl ts -verify`.
  Non-fatal by design: a TSA outage logs an "unwitnessed" gate row, never blocks
  the publish. eIDAS-/auditor-recognized format. See `SECURITY.md`.
- **`[crawl] respect_robots`** — robots.txt `Disallow` is now enforced at seed
  (default `true`): a disallowed URL never enters the manifest, and a broad
  `Disallow` correctly overrides a sitemap that lists the path. Uses `protego`
  (RFC 9309 wildcards + longest-match precedence); `5xx`/`429` back off, a
  missing/unreachable robots.txt allows all. `skipped_robots` is surfaced in the
  seed summary. Set `false` only for sources you have permission to index.
- **Content-admission gate** — a non-empty `2xx` that is actually a bot-challenge
  interstitial (Cloudflare IUAM, Incapsula, PerimeterX, DataDome) is refused at
  extract (`admission-challenge-page`) instead of being hashed and signed as real
  content. A structure-vs-content test (a challenge marker in the raw HTML but
  not in the extracted prose) keeps real pages — even ones that *discuss* a
  bot-manager — admitted.
- **Determinism + derivation-env recording at publish** — a new advisory gate
  re-extracts a sample straight from the cached raw blobs and compares
  `content_hash`, catching extractor nondeterminism the stored-markdown hash
  sample couldn't see; the snapshot now also records the native derivation
  environment (`python` / `unicode` / `lxml` / `libxml2`).
- **Changelog-continuity gate** — publish refuses a changelog genesis change or
  length regression versus the prior published snapshot, closing a
  truncate/re-genesis forgery of the append-only history.

### Changed
- **Coverage reports the indexed-content fraction, not lifecycle-closed.** The
  snapshot publishes `indexed_fraction` (content-bearing FRESH/FROZEN ÷ expected)
  alongside `resolved_fraction` (includes GONE/SKIPPED) with an explicit
  `denominator_basis`, so a green `coverage=1.0` can no longer hide rows that
  resolved to GONE/SKIPPED with no content.
- **`normalizer_version` fingerprints the active profile's `dynamic_patterns`**
  (`v2` → `v2+<hash>`), so editing a profile's noise-stripping correctly
  invalidates stored `content_hash`es instead of silently reusing stale ones.
  Zero-pattern profiles keep the bare `v2` (no re-extraction churn on upgrade).

### Security
- **SSRF allow-list enforced on the browser fetch path** — a redirect that lands
  off the configured host allow-list is dropped (fail-closed, including an opaque
  `final_url`), matching the native fetcher and preventing link-local/metadata
  responses from being captured into the corpus.

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
