# Security Policy

## Supported versions

Security fixes land on the latest released minor and `main`. Older tags are not back-patched.

| Version | Supported |
|---|---|
| `0.1.x` / `main` | ✅ |
| `< 1.1` | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) (the repo's **Security → Report a vulnerability**).

Include: affected version/commit, a description, reproduction steps or a PoC, and the impact you observed. We aim to **acknowledge within 3 business days** and to ship a fix or mitigation for confirmed issues within **30 days**, coordinating disclosure with you.

## Security model & scope

sift is a content pipeline plus a local (stdio) MCP server. In scope:

- **MCP server (`sift-mcp`).** Read-only by default. The write tools (`index_url` / `index_status`) are exposed only behind `--enable-index`, and `index_url` only crawls hosts on the index's configured `seed.host_allow`. In scope: allow-list bypass, path traversal outside the index root, or `query_manifest` running anything other than read-only `SELECT`/`WITH`.
- **Crawler / fetcher.** sift fetches operator-configured URLs. In scope: SSRF via crafted seeds/sitemaps, or a fetched page influencing the host beyond stored content.
- **Integrity guarantees.** The determinism / Merkle-root / hash-chained-changelog properties are load-bearing. Making `read_md verify=true` pass on tampered content, or forging a snapshot that `sift verify` accepts, is high-severity.

Out of scope: third-party dependency CVEs (report upstream; we bump on fix), DoS from pointing sift at a hostile/enormous site you control, and secrets you place in your own `sift.toml`/environment. The optional Firecrawl path uses your own `FIRECRAWL_API_KEY` — key management is the operator's responsibility.

## Operating sift safely

- Keep the MCP server **read-only** (omit `--enable-index`) unless you need agent-driven crawls; when enabled, keep `seed.host_allow` tight.
- Crawl only sources you're authorized to crawl, at a polite `rate_per_sec`.
- Treat `manifest.db` and the index root as trusted local state; don't expose the stdio MCP server to untrusted network peers without your own auth layer.

## Proof-carrying answers (`prove` / `verify-proof`)

`prove` emits a self-contained Merkle **inclusion proof** that a page's `content_hash` is committed by a published snapshot's `merkle_root`; anyone can re-check it with `python -m sift.verify_proof <file>` (stdlib only, no sift install). Be precise about what the root does and does not attest:

- **It attests** that `(url, content_hash)` was a **member** of the published snapshot identified by `run_id` + `merkle_root`, **dated** by that snapshot — *membership + dated byte-integrity of a published page*. The date is the snapshot's own `completed_at` — **self-asserted by the operator** unless the snapshot carries an RFC-3161 timestamp (see below), which makes it independently witnessed. The prover refuses unless the leaf set reconstructed from the run (its md tree, or the manifest as a fallback) reproduces the stored root exactly, so a proof can only ever attest the published commitment.
- **It does NOT attest** non-membership ("this URL was never indexed"), completeness / non-suppression ("nothing was hidden"), or current truth ("this is still the latest"). A proof is a statement about *one* snapshot at *one* time; "is it current?" is answered operationally by `snapshot_status` + `changed_since`, not cryptographically. An `included: false` result is a statement about *that* snapshot, not a cryptographic non-existence proof.

**Tree convention (for re-implementers).** `leaf = sha256(utf8(url + ":" + content_hash_hex))` where `content_hash_hex` is `content_hash` minus any `sha256:` prefix; interior nodes hash the **concatenated 64-char lowercase-hex strings** of the two children (128 hex chars → utf-8), **not** raw 32-byte digests and **not** double-SHA-256. The leaves are sorted; an odd level duplicates its trailing node. The envelope's `scheme` and `integrity_version` pin this exactly — a verifier that disagrees on either refuses rather than false-passes.

**External timestamp anchor (RFC-3161).** A snapshot's date is otherwise the operator's own `completed_at`, so nothing stops a malicious operator back-dating a root. Set `[publish].timestamp_tsa_url` (e.g. `http://timestamp.digicert.com`) and every publish requests a signed **RFC-3161 Time-Stamp Token** over the `merkle_root` from that Time-Stamp Authority, stored at `runs/<id>/merkle_root.tsr`. The token is an **independent witness** — a third party (the TSA), neither sift nor the operator, cryptographically attests the root existed at the stated time, so the root cannot be back-dated past the witness. `prove` embeds the token in the envelope; `sift verify-proof`, `sift verify-timestamp`, and plain `openssl ts -verify -digest <root> -in merkle_root.tsr` all check it against the root. RFC-3161 is the timestamp format auditors and eIDAS recognize. The anchor is **non-fatal**: a TSA outage logs an "unwitnessed" gate row rather than failing the publish (the snapshot is then dated only by `completed_at`). A verifier treats a *present-but-invalid* token as a failure and an *absent* token as a self-asserted date — never as a pass.

**Accepted posture (v1).** The tree has no RFC-6962 leaf/node domain-separation prefix and is the bitcoin-style construction. CVE-2012-2459 (duplicate-leaf root collision) is **present in the primitive but unreachable**: corpus leaves are unique (`url` is a manifest primary key; a distinct-url-same-leaf collision is second-preimage-hard), and the prover's root self-check rejects any leaf set that doesn't reproduce the stored root. A future `integrity_version` may adopt domain separation; the version pin makes that a clean cutover (old verifiers refuse, never false-pass). Forging an inclusion proof for non-member content, or making `verify-proof` accept a tampered envelope, is high-severity — please report it.
