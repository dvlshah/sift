<div align="center">

<img src="assets/sift.gif" alt="sift" width="480">

**Give your coding agent a complete, always-fresh copy of any website — with proof of every answer.**

[![Tests](https://github.com/dvlshah/sift/actions/workflows/tests.yml/badge.svg)](https://github.com/dvlshah/sift/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/sift-engine?color=blue)](https://pypi.org/project/sift-engine/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-stdio-purple)](https://modelcontextprotocol.io/)

</div>

sift crawls a site into local markdown + structured facts your agent greps, reads, and cites — files on disk, not vectors, read over MCP. Every page is **content-hashed and dated**, so answers trace back to the exact source and snapshot. Re-run anytime; only changed pages are refetched. Self-hosted.

## ❌ Without sift

Your agent's built-in web-fetch gives you:

- **Stale** answers — the provider's crawler cached that page weeks ago
- **Partial** answers — just the one page it happened to land on
- **Unprovable** answers — no record of what the source said, or when

## ✅ With sift

- **Always fresh** — you control the crawl; conditional GETs refetch only what changed, and `changed_since` lets your agent pull just the delta instead of re-reading the corpus
- **Provable** — content-hash + date on every page; cite the source, hash, and snapshot, read any past snapshot (`as_of`), or emit a self-contained `prove` inclusion proof a third party verifies offline (`python -m sift.verify_proof`) without trusting the server
- **Complete & grep-native** — the whole site on disk, not a handful of retrieved snippets
- **Self-hosted** — any `http(s)` site you can reach, public or internal; your data stays yours

---

## Get started

Requires Python 3.11+.

### 🤖 Let your coding agent set it up — recommended

Paste one prompt into **Claude Code, Cursor, Codex, or any MCP-aware agent**. It explains sift, asks which site to index, then installs the engine, builds an index, wires up MCP, and shows you how to query it — end to end.

<details>
<summary><b>📋 Copy the one-paste setup prompt</b></summary>

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
SKILL=https://raw.githubusercontent.com/dvlshah/sift/v0.2.0/.claude/skills/sift
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
Hardened / bot-blocked host (Cloudflare/Akamai/Imperva) → add `--impersonate-fallback`
(free, TLS-fingerprint impersonation; `pip install 'sift-engine[impersonate]'`).
JS-rendered SPA → `pip install 'sift-engine[browser]' && python -m playwright install chromium`,
then add `[browser]\nenabled = true` (it joins the ladder as a free render tier).
Still blocked (JS-challenge edges) → `--firecrawl-fallback` (paid; needs FIRECRAWL_API_KEY).
These compose into one escalation ladder: native → impersonate → browser → Firecrawl.

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

</details>

### 🧑 Or do it yourself

```bash
pip install sift-engine

# the ATO sitemap below uses a bundled profile, so this runs with zero config
sift init   --root ./index
sift seed   --root ./index --from-sitemap https://www.ato.gov.au/sitemap.xml
sift run    --root ./index --limit 25 --coverage-base planned   # smoke-test first
sift verify --root ./index --skip-signature
sift-mcp    --root ./index   # serve the index to your agent
```

Then point your agent at it (use an absolute path):

```json
{
  "mcpServers": {
    "sift": { "command": "sift-mcp", "args": ["--root", "/abs/path/to/index"] }
  }
}
```

Indexing your own site? Add a `sift.toml` (generic profile + host allow-list) — see [Configuration](.claude/skills/sift/reference/config.md).

---

## How it works

```
seed → plan → fetch → extract → commit → publish
```

Five idempotent phases. `publish` runs 5 verification gates, then atomically swaps the `current/` snapshot and writes a Merkle root over every page hash. Deterministic: **same input → same `content_hash` → same Merkle root** — so any reader re-verifies a page in O(1), or the whole snapshot end-to-end with `sift verify`.

Your agent reads the published snapshot **read-only** over MCP: `snapshot_status` → `grep_corpus` → `read_md` / `read_facts` → cite source + hash + date.

## Docs

- **[CLI reference](.claude/skills/sift/reference/cli.md)** — every command and flag
- **[Configuration & site profiles](.claude/skills/sift/reference/config.md)** — `sift.toml` and the `SiteProfile` contract
- **[MCP tools](.claude/skills/sift/reference/mcp-tools.md)** — parameters, output caps, multi-index mode
- **[Corpus format & integrity contract](./corpus.contract.md)** — on-disk layout and what each read tool returns
- **[Contributing](./CONTRIBUTING.md)** · **[Security](./SECURITY.md)**

> **Open core, Apache-2.0.** This repo is the full open-source engine (pipeline + MCP server) and runs standalone. A hosted platform built on it is in development.

**Status — v0.2.0**; tests green on Python 3.11–3.13. Adds the tiered fetch transport (native → curl_cffi → browser → Firecrawl) for hardened and JS-rendered sites. Known limits: no run-dir GC yet, stdout-only logging, stdio-only MCP transport. Issues & roadmap → [GitHub Issues](https://github.com/dvlshah/sift/issues).

## License

[Apache-2.0](./LICENSE) — © 2026 Deval Shah.
