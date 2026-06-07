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
