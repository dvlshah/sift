# Design — browser fetch capability

> Status: **Scope frozen — ready for implementation**
> Authors: Deval Shah, Claude (Opus 4.7)
> Target version: sift v0.2.0
> Companion test specs: [`tests/test_browser_contract.py`](../../tests/test_browser_contract.py)
> Scope frozen: 2026-05-25 (commit `feat/browser-fetch-design`)

> **Review history (resolved).** Twelve review items raised pre-implementation
> and resolved across four commits on this branch (P0×4 → P1×4 → P2-1 →
> P2×2). Each resolution is documented inline as a `**Resolved (Pn-m)**`
> paragraph adjacent to the section it affected. Implementation PR should
> not introduce new design questions without a corresponding doc update.

## 1. Goal & non-goals

### Goal

Let a `SiteProfile` opt arbitrary URLs into **browser rendering** so the
fetch phase can index JS-rendered pages (SPAs) into the same content-addressed
raw store the HTTP path uses today. Everything downstream — `extract`,
`commit`, `publish`, the gates, the integrity story — stays unchanged.

The concrete trigger is the existing README limitation:

> No Playwright SPA branch — JS-rendered pages (e.g. ATO Legal Database SPA)
> silently fail extraction.

But the design is site-agnostic: any future profile that needs browser rendering
gets it for free by overriding two methods on `SiteProfile`.

### Non-goals

- **No second runtime renderer.** Crawl4AI is the only browser fetcher
  shipped. The abstraction we add is sift-internal so we *could* swap, not
  because we plan to.
- **No facts-extraction-by-LLM-schema-generation.** Crawl4AI's
  `JsonCssExtractionStrategy.generate_schema()` is interesting and could
  close sift's facts-coverage gap (~0.78 today), but it's a separate
  initiative that doesn't need this design.
- **No managed agents / live MCP / dashboard / hosted service.** Sift is
  a single-process library + CLI. That doesn't change.
- **No API-direct fetch shortcuts.** The probe found that ATO exposes
  `/_next/data/.../legaldatabase.json` and `/API/v1/law/lawservices/*` —
  interesting, but that's a future ATOProfile-specific optimization
  (`SiteProfile.fetch_override(url)` hook), not part of this design.
- **No new pipeline phase.** Fetch routes via SiteProfile, downstream
  is identical.

## 2. Constraint: loose coupling

Crawl4AI is a 38k-line library with a wide surface area (4-phase JS pipeline,
shadow DOM flattening, 25+ CMP removers, virtual scroll, undetected browser,
hooks, dispatchers, extraction strategies). **Sift will use ≤5% of it.**

Coupling rule: **one file in sift imports `crawl4ai`. Exactly one.**

That file is `sift/browser.py`. Every other module in sift talks to sift-owned
dataclasses (`RenderedPage`, `BrowserFetchConfig`) and one async function
(`render(url, config)`). If we ever want to swap to bare Playwright or a
different library, we rewrite `sift/browser.py` and nothing else.

## 3. Architecture

### 3.1 The single coupling point

```
sift/browser.py                    # NEW — only file that imports crawl4ai
├── @dataclass BrowserFetchConfig  # sift-owned, no crawl4ai types
├── @dataclass RenderedPage        # sift-owned, no crawl4ai types
├── class BrowserNotInstalledError # raised when the [browser] extra is missing
├── class BrowserFetchError        # all crawl4ai errors get coerced to this
└── async def render(url, config) -> RenderedPage
       # Lazy-imports crawl4ai inside the function body.
       # Translates BrowserFetchConfig -> CrawlerRunConfig.
       # Translates crawl4ai's result -> RenderedPage.
       # Crawl4ai types never escape this module.
```

### 3.2 Caller surfaces (unchanged + minimal additions)

> Full file-by-file enumeration with every helper name lives in §12.4.
> This section is the orientation sketch — the canonical list is §12.4.

```
sift/browser.py                    # NEW — single crawl4ai coupling point
├── BrowserFetchConfig             # per-fetch dataclass
├── BrowserConfigDefaults          # per-context (TOML) dataclass — lives in config.py
├── RenderedPage                   # lowercase-key + whitelisted headers
├── BrowserPool                    # owns shared AsyncWebCrawler
├── render(url, config, pool)      # Playwright Response hook, capture-then-filter
├── _capture_navigation_headers    # helper, testable in isolation
├── _project_headers               # lowercase + whitelist filter
├── check_browser_available()      # eager-check entrypoint
└── BROWSER_VERSION                # co-located here (P2-1)

sift/status.py                     # NEW — compute_status_summary(root) -> dict
└── (F1) Extracted from cli.py's status command so the contract tests can
       import + call it directly. cli.py's `sift status` becomes a thin
       click.echo(json.dumps(compute_status_summary(root))) wrapper.
       Matches existing pattern (paths.py, publish.py, integrity.py all
       separate compute from CLI).

sift/fetch.py                      # MODIFIED — one branch, ~25 lines added
└── _fetch_browser via sift.browser.render (selected by profile.requires_browser)

sift/sites/__init__.py             # MODIFIED — 2 new methods on SiteProfile
├── requires_browser(url) -> bool             # default: False
└── browser_config(url)    -> BrowserFetchConfig | None  # default: None

sift/sites/ato.py                  # MODIFIED — opt /single-page-applications/ in

sift/config.py                     # MODIFIED — [browser] section + BrowserConfigDefaults

sift/decide.py                     # MODIFIED — Decision.SKIPPED_BROWSER_DISABLED member

sift/plan.py                       # MODIFIED — route_to_browser_disabled() helper
                                   # + plan() signature gains profile + cfg kwargs (F2)

sift/manifest.py                   # MODIFIED — SCHEMA_VERSION=2 + _migrate + browser_version column

sift/publish.py                    # MODIFIED — _TERMINAL_STATES + _is_terminal_state helper

sift/cli.py                        # MODIFIED — eager check_browser_available()
                                   # + status command becomes wrapper over sift.status

pyproject.toml                     # MODIFIED — opt-in extra
└── [project.optional-dependencies].browser = ["crawl4ai>=0.8.6"]
```

That's the full surface: **2 new files, 9 modified files, 1 optional dep.**

## 4. Data flow

### 4.1 With the new path active

```
plan      → (unchanged) per-URL decision
fetch     → for each URL:
              if profile.requires_browser(url):
                  cfg = profile.browser_config(url) or BrowserFetchConfig()
                  page = await sift.browser.render(url, cfg)
                  raw_bytes = page.html.encode("utf-8")
              else:
                  raw_bytes = await _fetch_http(url)  # existing path
              raw_hash = sha256(raw_bytes)
              write raw/<aa>/<sha256>.html.gz         # SAME content-addressing
              append to fetch.log                      # SAME log shape
extract   → (unchanged) trafilatura over cached raw
commit    → (unchanged)
publish   → (unchanged) — gates verify content_hash, not raw_sha256
```

### 4.2 The `RenderedPage` ↔ `FetchResult` contract

`render()` returns a sift dataclass; `fetch.py` projects it into the existing
`FetchResult` shape so nothing downstream changes:

| `RenderedPage` field | Mapped to `FetchResult` field |
|---|---|
| `html: str` | hashed → `raw_hash`, written to raw blob |
| `final_url: str` (after redirects) | logged via existing redirect-tracking path |
| `status_code: int` (always 200 if `success=True`) | `status` |
| `elapsed_ms: int` | logged for telemetry only |
| `headers: dict[str,str] \| None` (best-effort from network capture) | `etag`, `last_modified` if present |
| `error: str \| None` | if non-None, `FetchResult.error` |

**Conditional-fetch behavior for browser URLs**: `decide()` may return
`FETCH_CONDITIONAL` for any URL based on the existing interval/sitemap
rules, regardless of transport. What differs at the transport layer is
*whether conditional headers can be sent*:

- **HTTP path** (existing): `_one_request` sends `If-None-Match` /
  `If-Modified-Since` from the row's stored `etag` / `last-modified`.
  Origin can reply `304 Not Modified`, skipping the body transfer.
- **Browser path with headers captured (P1-1 + P1-4 happy path)**: the
  Playwright Response hook persisted `etag` / `last-modified` from the
  previous render. `_fetch_browser` could in principle pass these to
  crawl4ai's request (Playwright's `extra_http_headers` on the context
  before navigation), enabling true 304 responses. **Decision: defer
  this optimization to v0.3.0.** Browser-render time is dominated by
  hydration, not body transfer; the conditional-fetch win is marginal.
  v0.2.0 always renders even when headers are present — same correctness,
  modest cost.
- **Browser path with no headers (hook failed or first fetch)**: `etag`
  / `last-modified` are `None`. Even on `FETCH_CONDITIONAL`, no conditional
  headers can be sent. Effectively a full re-render. Acceptable: ATO's
  ~100 SPA URLs at 5s each is 8 minutes on a daily cron.

### 4.3 Storage & versioning

- **Raw blob** — same content-addressed path: `raw/<aa>/<sha256>.html.gz`.
  Storage is unified across http/browser paths.
- **`raw_sha256` churn** — rendered HTML changes slightly between renders
  (Akamai trace IDs, build IDs in script tags). Confirmed by the probe:
  6 changed lines across 945 KB. Each refresh costs a re-extract, but
  `content_hash` (post-normalize) is stable, so the Merkle root + changelog
  are unaffected. Already proven by our `test_spa.py` run: byte-identical
  markdown across two renders.
- **`BROWSER_VERSION` pin** — `"chromium-148.0.7778.96+crawl4ai-0.8.6"`.
  Stored alongside `crawler/extractor/normalizer/classifier` versions
  in the manifest. Bumping it makes `plan` emit `FETCH_CONDITIONAL` for
  any URL whose stored `browser_version` differs — i.e. a Chromium upgrade
  triggers re-fetch only for browser-fetched URLs, not http-fetched ones.

## 5. The `sift/browser.py` contract

### 5.1 `BrowserFetchConfig` (sift-owned, no crawl4ai types)

```python
@dataclass(frozen=True)
class BrowserFetchConfig:
    """Per-fetch knobs. Inherits defaults from [browser] config section.

    Per-fetch surface only. Init-scripts (Playwright's add_init_script,
    which is per-context not per-page) live on BrowserConfigDefaults
    because crawl4ai binds them at AsyncWebCrawler construction — see
    §7 and the P0-1 resolution below.
    """

    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "networkidle"
    page_timeout_s: float = 60.0
    wait_for: str | None = None            # CSS selector or "js:..." expression
    js_code_before_wait: str | None = None # SPA kick, runs POST-navigation
    delay_before_return_html_s: float = 0.0

    # Off by default — opt in per-site only if needed. ATO doesn't need any.
    flatten_shadow_dom: bool = False
    remove_consent_popups: bool = False
```

**Resolved (P0-1)**: `extra_init_scripts` is gone from this dataclass. Init
scripts are bound at `AsyncWebCrawler` construction (Playwright's
`context.add_init_script()` runs once per context). Putting them on a
per-fetch config either (a) forced a fresh crawler per call — defeats
`BrowserPool`'s shared-crawler design (§12.2), 5–10s startup × every render
— or (b) silently ignored them after the first call. Moved to
`BrowserConfigDefaults` in §7 where the lifecycle matches. Per-call JS
escape hatch remains `js_code_before_wait`, which runs *after* navigation
and is correct for ~90% of cases (consent dismissal, lazy-load triggers,
tab clicks).

**Why this exact surface**: covers every knob we'd touch for ATO (none —
defaults work). Covers the next likely future profiles (consent banners,
shadow DOM, JS-gated tabs). Power users get an escape hatch. We don't
expose: `virtual_scroll_config` (overkill), `proxy_config` (not needed
for any planned profile), `MemoryAdaptiveDispatcher` (sift's existing
`aiolimiter` is fine at our scale), extraction strategies (we use
trafilatura).

### 5.2 `RenderedPage` (sift-owned)

```python
PERSISTED_HEADER_KEYS: frozenset[str] = frozenset({
    "etag", "last-modified", "cache-control",
})

@dataclass(frozen=True)
class RenderedPage:
    """The sift-owned shape crawl4ai's result projects into.

    `headers` invariants (pinned by contract tests):
      * Keys are **always lowercased** (P1-2). Playwright's response.headers
        is already lowercased and httpx convention matches, but we pin it
        here to prevent downstream `.get("ETag")` vs `.get("etag")` bugs.
      * Keys are **whitelisted** to PERSISTED_HEADER_KEYS (P1-4). Anything
        else (content-length, content-encoding, server, set-cookie, ...)
        describes the SPA shell, not the rendered content, and would
        confuse anyone reading manifest rows. Dropped at the boundary
        before RenderedPage is constructed.
      * None when no navigation response was captured — caller treats
        that as "no conditional-fetch headers known."
    """
    html: str
    final_url: str
    status_code: int
    elapsed_ms: int
    headers: dict[str, str] | None = None
    error: str | None = None
```

No `network_requests`, no `markdown`, no `screenshot` fields. We deliberately
discard everything crawl4ai returns that we don't use. If a future feature
needs network capture (e.g. an API-discovery tool), we add a separate function
(`capture_network(url, config) -> list[NetworkEvent]`) rather than expanding
`RenderedPage`.

**Resolved (P1-2)**: header keys are lowercased — pinned in the dataclass
docstring and by the `test_headers_keys_are_lowercased` contract test.

**Resolved (P1-4)**: persisted-header whitelist is exposed as
`PERSISTED_HEADER_KEYS` (frozenset, importable from `sift.browser`) so the
filter has one canonical source. `sift.browser._project_headers(raw_dict)
-> dict[str, str]` lowercases-then-filters and is the only path into
`RenderedPage.headers`. The `test_headers_whitelist_drops_non_cache_keys`
contract test pins the behavior.

### 5.3 The `render()` function

```python
async def render(
    url: str,
    config: BrowserFetchConfig,
    pool: "BrowserPool",
) -> RenderedPage:
    """Render `url` with a headless browser.

    Acquires a render slot from `pool` (semaphore-gated). The slot yields
    the *shared* AsyncWebCrawler instance — a new browser context is opened
    for this fetch (cheap, ~50ms), used, and closed; the underlying crawler
    process stays alive across calls.

    Lazy-imports crawl4ai. Raises BrowserNotInstalledError if the [browser]
    extra is missing. Raises BrowserFetchError on render failure (network,
    timeout, navigation error). Never raises crawl4ai's own exception types
    — they get coerced.

    Caller's responsibility: construct one `BrowserPool` per sift process,
    pass the same instance to every `render()` call, and call
    `await pool.aclose()` at shutdown. fetch.py wires this once per run.
    """
```

**Resolved (P0-2)**: render() takes a `BrowserPool` argument. The pool owns
the long-lived `AsyncWebCrawler`. Fresh per-page context, shared per-process
crawler. The §12.2 contract section is updated in parallel; the
`test_BrowserPool_yields_shared_crawler` contract test below pins the
behavior so an implementation can't legitimately create one crawler per
acquire and still pass the existing semaphore test.

### 5.4 Errors

```python
class BrowserNotInstalledError(ImportError):
    """Raised when `import crawl4ai` fails. Message points at:
        pip install 'sift[browser]' && python -m playwright install chromium
    """

class BrowserFetchError(Exception):
    """Wraps any crawl4ai exception. Caller sees this; never the underlying type."""
```

### 5.5 What `sift.browser` does NOT do

- Concurrency control (caller owns it via the fetch-phase semaphore)
- Caching (the raw blob store handles it)
- Retries (fetch.py's existing retry loop wraps `render()`)
- Logging (raises clear exceptions; fetch.py logs them)

Single responsibility: "render this URL, give me back the HTML."

### 5.6 Lazy-import discipline (P2-3)

The lazy-import contract (§4 of TestLazyCrawl4AIImport — "importing
`sift.browser` must NOT pull in crawl4ai") is silently breakable. Any
`from . import browser` chain that evaluates `check_browser_available()`
or `render()` at import time will pass linting and break the test.

Implementation rules:

1. The first line of `sift/browser.py` after the module docstring is the
   comment block:
   ```python
   # DO NOT import crawl4ai at module level.
   # Pulls ~38k LOC + Playwright + Chromium import chain into every
   # sift CLI startup, including http-only users. See:
   #   - tests/test_browser_contract.py :: TestLazyCrawl4AIImport
   #   - docs/design/browser-fetch.md   :: §5.6
   ```
2. `import crawl4ai` lives **inside the function bodies** of every public
   surface that needs it: `render()`, `check_browser_available()`,
   `BrowserPool._ensure_crawler()`. Never at module top.
3. The eager startup check from §12.3 calls `check_browser_available()`
   from `sift/cli.py`, AFTER config is loaded. Not from `sift/browser.py`'s
   import-time path. The two are mutually consistent only because the
   eager check is opt-in via config, not import-time.

The `test_no_eager_crawl4ai_import` contract test catches violations by
dropping `crawl4ai` from `sys.modules`, re-importing `sift.browser`, and
checking that `crawl4ai` is still absent. Any module-level import or
import-time-call regression turns it into a real failure (xfail strict→pass).

## 6. `SiteProfile` additions

Two new methods, both with safe defaults that preserve the current behavior
of every existing profile:

```python
class SiteProfile:
    def requires_browser(self, url: str) -> bool:
        """Whether this URL needs browser rendering. Default: never."""
        return False

    def browser_config(self, url: str) -> "BrowserFetchConfig | None":
        """Per-URL browser knobs. Default: None (use [browser] config section
        defaults). Profiles can return different configs for different URL
        patterns (e.g. consent-banner sites get remove_consent_popups=True)."""
        return None
```

Forward reference for `BrowserFetchConfig` because importing
`sift.browser` from `sift.sites` would force crawl4ai's lazy import on
package load. The TYPE_CHECKING block handles this cleanly.

### `ATOProfile` changes

- Remove `r"^/single-page-applications/"` from `default_excludes`
- Add `requires_browser(url)` returning `True` for that path prefix
- No `browser_config` override needed — defaults work (proven by `test_spa.py`)

## 7. Config additions

```toml
[browser]
enabled = true              # if false, profile.requires_browser is ignored,
                            # URL falls through to http path (which will fail)
concurrency = 2             # per-process concurrent renders
                            # (separate from [crawl].concurrency)
page_timeout_s = 60
wait_until = "networkidle"  # default for BrowserFetchConfig

# Defaults below are off — opt-in per site via SiteProfile.browser_config()
flatten_shadow_dom = false
remove_consent_popups = false
user_agent = ""             # empty -> sift's default UA string

# Per-context init scripts (P0-1 resolution): bound at AsyncWebCrawler
# construction, so they belong here, not on BrowserFetchConfig. Run before
# any page navigation in every context. Typical use: stealth patches
# (override navigator.webdriver, etc.) or fingerprint normalisation.
init_scripts = []           # list of JS strings; empty by default
```

Same TOML resolution semantics as the other sections: CLI flag > env var
(none yet for [browser]) > TOML > built-in defaults.

## 8. Versioning

Add to `sift/browser.py` (co-located with the rest of the browser surface):

```python
BROWSER_VERSION = "crawl4ai-0.8.6"
```

**Resolved (P2-1)**: previous draft put `BROWSER_VERSION` in a new
`sift/version.py`. That broke the existing pattern — today every phase's
version constant is co-located with the phase:
`CRAWLER_VERSION` in `sift/__init__.py`, `CLASSIFIER_VERSION` in
`classify.py`, `EXTRACTOR_VERSION` in `extract.py`, `NORMALIZER_VERSION`
in `normalize.py`. Introducing `sift/version.py` for one constant would
be half a refactor. Co-locating in `sift/browser.py` matches the
pattern with the fewest moving parts. Contract-test imports updated
accordingly (`from sift.browser import BROWSER_VERSION`).

**Resolved (P1-3)**: the previous draft pinned both Chromium and crawl4ai
(`"chromium-148.0.7778.96+crawl4ai-0.8.6"`). That's fragile — crawl4ai
bundles Playwright, which manages its own Chromium binary, and a
`playwright install chromium` after a Playwright bump silently changes
the rendering binary without anyone touching the constant. Pinning
crawl4ai alone makes the dep's own pin the proxy: a crawl4ai patch
version usually pins or guards Playwright/Chromium behavior changes,
and a crawl4ai bump is the right invalidation trigger anyway. The
discovery-via-Playwright-internals alternative
(`playwright._impl._driver.compute_driver_executable()`) was rejected —
adds a runtime dependency on Playwright privates, exactly the
private-API coupling this design is avoiding.

**Drift detection without drift-as-cache-invalidation**: `sift status`
reports the actual Chromium version that crawl4ai resolved at startup
under `versions.browser_runtime_chromium`. One-time lookup, cached for
the session, surfaced for operator visibility but never used as an
invalidation key. If the bundled Chromium changes under us, operators
see it in `sift status`; if it changes in a way that affects rendering,
the next crawl4ai release is the place that fix lands and the constant
bump is what invalidates blobs. Pinned by the
`test_status_reports_browser_runtime_chromium` contract test.

### 8.1 Schema migration (P0-4)

The previous draft assumed migration was "additive, no cost." It isn't —
today's `manifest.py` writes `SCHEMA_VERSION = 1` into `meta` on init but
never reads it back, and `init_schema()` uses `CREATE TABLE IF NOT EXISTS`,
which silently skips column-additions on existing DBs. The first
`sift plan` against a v0.1.0 manifest would crash with
`OperationalError: no such column: browser_version`.

**Real migration path** that this design adds:

```python
# sift/manifest.py
SCHEMA_VERSION = 2          # bumped from 1

_MIGRATIONS: dict[int, str] = {
    # from-version → SQL applied to reach from-version + 1
    1: "ALTER TABLE manifest ADD COLUMN browser_version TEXT;",
}

def _migrate(conn: sqlite3.Connection, from_v: int, to_v: int) -> None:
    """Apply each migration in order from from_v up to to_v.

    Migrations are single-statement DDL today (one ALTER per version bump).
    Each runs in its own transaction so a partial failure leaves a clear
    `meta.schema_version` to recover from.
    """
    for v in range(from_v, to_v):
        sql = _MIGRATIONS[v]
        with conn:                                 # implicit transaction
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(v + 1)),
            )

def init_schema(conn: sqlite3.Connection) -> None:
    # 1. CREATE TABLE IF NOT EXISTS (existing behavior) — fresh DBs land at v2
    #    directly because the CREATE-TABLE definition already includes the
    #    new column.
    conn.executescript(_SCHEMA_DDL)
    # 2. Read what version this DB *is* at (after the CREATE).
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    current_v = int(row[0]) if row else SCHEMA_VERSION
    # 3. If older than SCHEMA_VERSION, run migrations.
    if current_v < SCHEMA_VERSION:
        _migrate(conn, current_v, SCHEMA_VERSION)
    # 4. Always write the current version (handles fresh-DB case where row
    #    might be absent if the CREATE didn't seed it — current init code
    #    seeds it; safety belt only).
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
```

**Why per-version transactions, not one big transaction**: SQLite DDL
isn't atomic across multiple statements without an explicit `BEGIN`/`COMMIT`,
and a partial failure that left some columns added without bumping the
version would make the next `init_schema` retry them and fail on duplicate
column. The per-version pattern is what every mature SQLite ORM does for
the same reason.

**Why store both CREATE-TABLE-current and migration steps**: fresh DBs
get the current schema in one shot via `CREATE TABLE IF NOT EXISTS` (with
the new column already in the DDL). Existing DBs hit the IF-NOT-EXISTS
no-op then run the ALTER migrations. Both paths converge at the same
final schema. No code branch on "is this a new DB" — the version comparison
handles it.

**Test (added to the contract file)**: synthesize a v1 DB in `tmp_path` by
applying the *old* `CREATE TABLE` DDL + writing `("schema_version", "1")`
to meta. Call `init_schema()`. Assert: the `browser_version` column exists
in the `manifest` PRAGMA, and `meta.schema_version` reads `"2"`.

**Forward-compat note**: future schema bumps add an entry to `_MIGRATIONS`
and update `SCHEMA_VERSION`. No further changes to `init_schema()`. The
migration mechanism doesn't need to evolve until we hit a multi-statement
or data-rewriting migration; we'll cross that bridge when it lands.

### 8.2 Plan-phase rule for `browser_version` invalidation

A URL whose previous fetch used the browser path stores a non-NULL
`browser_version` (the column added by the P0-4 migration). On subsequent
plans, if that stored value doesn't match the current `BROWSER_VERSION`,
rendering may have changed (crawl4ai patch bump, new init scripts, etc.)
and the URL is marked for re-fetch. The shape mirrors the existing
`EXTRACTOR_VERSION` / `NORMALIZER_VERSION` invalidation rules already in
`decide.py`:

```python
# in plan.py, after the standard decide() call
if row.browser_version is not None and row.browser_version != BROWSER_VERSION:
    decision = Decision.FETCH_CONDITIONAL
```

`FETCH_CONDITIONAL` for v0.2.0 is effectively a full re-render on the
browser path — even when `etag` / `last-modified` are captured (P1-1),
we defer plumbing them through to crawl4ai's request as
`extra_http_headers` until v0.3.0 (see §4.2 case 2). New states are
covered separately: `SKIPPED_BROWSER_DISABLED` is added by P0-3 for the
operator-opt-out case, not the invalidation case.

## 9. Test plan

Five layers, all in `tests/`:

1. **Contract tests** — `test_browser_contract.py` (this design doc's
   companion). Failing tests that document the public surface
   (`RenderedPage`, `BrowserFetchConfig`, `render`, `BrowserNotInstalledError`).
   These pin behavior BEFORE implementation.

2. **Unit tests** — `test_browser.py` (added during implementation).
   `render()` with a mocked crawl4ai (`unittest.mock.patch("crawl4ai.AsyncWebCrawler")`).
   Asserts: error coercion, lazy import, config translation, return shape.

3. **Fetch-branch tests** — `test_fetch_browser.py` (added during impl).
   Synthetic SiteProfile with `requires_browser=True` on half its URLs.
   Mocks `sift.browser.render`. Asserts: routing decisions, raw blob storage
   under unified path, fetch.log shape unchanged, browser_version recorded.

4. **SiteProfile tests** — extend `test_sites.py` with default-value
   checks for `requires_browser` and `browser_config`.

5. **Real integration** — `test_browser_real.py`, **skipped** unless
   `SIFT_REAL_BROWSER=1` env var is set. Hits one public SPA
   (e.g. `https://www.ato.gov.au/single-page-applications/legaldatabase`),
   asserts: render success, html len > 100KB, no exceptions raised. CI
   doesn't run this (Chromium download per CI job is expensive); local
   developers can opt in.

## 10. Migration / renderer swaps

Because renderer imports are quarantined to `sift/browser.py`, swapping
to a different renderer is mechanical:

```
1. Update [project.optional-dependencies].browser to the new dep
2. Rewrite sift/browser.py's render() to call the new lib
3. Translate the new lib's result into RenderedPage
4. Translate the new lib's exceptions into BrowserFetchError
5. Bump BROWSER_VERSION (forces re-fetch of every browser-tagged row, §8.2)
```

No fetch.py change, no SiteProfile change, no manifest schema change,
no callsite anywhere else.

Likely future swaps to consider:

| When | Swap to | Why |
|---|---|---|
| Hit sites Playwright's default fingerprint can't bypass | `patchright` (undetected Chromium) | Patchright patches Playwright to avoid `navigator.webdriver`-style detection |
| Need to run on edge/serverless | `playwright-aws-lambda` | Bundles Chromium for Lambda |
| Need cross-browser (Firefox/WebKit) | `playwright.async_api` already supports it | Same surface, different `launch()` |

### 10.1 v0.2.0 shipped with Playwright (not crawl4ai)

**Original plan** (commits up to `079662a` on `feat/browser-fetch-impl`):
ship with crawl4ai 0.8.6 as the renderer, on the theory that its
markdown-generation and content-filter features might be reusable later.

**What happened:** during pre-merge validation against real-world SPAs
beyond the ATO reference site, crawl4ai's `AsyncWebCrawler.arun()` hung
indefinitely on analytics-heavy SPAs (verified reproducibly with
`domain.com.au`). The same URL rendered cleanly in 1.1s via bare
Playwright (`page.goto` + `page.content`). Isolation tests (raw
crawl4ai, no sift wrapper) hung at the same point — the bug is upstream,
not in our wrapper. Evidence preserved in [`archive/crawl4ai/`](../../archive/crawl4ai/).

**Decision:** swap renderer to bare `playwright.async_api`. Justified by
the design's §10 swap contract: only `sift/browser.py` and the `[browser]`
extra change. Trade-offs:

| Property | crawl4ai 0.8.6 | bare Playwright 1.60 |
|---|---|---|
| Dep tree size | ~300 MB (litellm, transformers, BM25, etc.) | ~50 MB (Playwright + Chromium download) |
| Maintainership | Single-maintainer OSS, recent supply-chain compromise patched | Microsoft, used by Cypress competitors |
| Post-nav overhead | ~15 hooks (markdown, network capture, consent, shadow-DOM, etc.) | None — `page.content()` returns HTML |
| Sites tested working | ATO Legal DB | ATO Legal DB + Domain (575KB rendered) |
| Sites hanging | `domain.com.au` (and likely class of analytics-heavy SPAs) | None observed |
| Anti-bot bypass for RE | Still returns 429 (no IP/UA tricks help) | Still returns 429 (same) |

**Sift uses ~0% of crawl4ai's value-add** (markdown gen, LLM extraction,
content filters) because trafilatura handles all of that downstream. The
swap drops cost without losing capability.

**Defaults also retuned** as part of the swap (one operational lesson
learned during validation):
- `wait_until` default: `networkidle` → `domcontentloaded`. Networkidle
  works for calm-network sites (ATO) but hangs forever on analytics-heavy
  pages.
- `delay_before_return_html_s` default: 0.0 → 3.0. Pairs with
  `domcontentloaded` to give SPAs time to hydrate.
- `page_timeout_s` default: 60.0 → 30.0. Half the wait before a hanging
  request fails the row (still loud, just sooner).

ATO continues to work with the new defaults (its DOM is ready at
`domcontentloaded`); sites that genuinely need `networkidle` set it via
`SiteProfile.browser_config(url)`.

**BROWSER_VERSION format change:** `crawl4ai-0.8.6` → `playwright-1.60`.
Per §8.2's invalidation rule, this forces re-fetch of every
browser-tagged row on the next plan cycle after upgrade — desirable,
since the rendered HTML now comes from a different stack.

## 11. Out of scope (intentionally)

| Feature | Why deferred |
|---|---|
| `sift discover-api <url>` CLI | Useful for profile authoring; defer until we author a second profile. |
| `MemoryAdaptiveDispatcher` integration | Sift's existing aiolimiter is fine at current scale (<10k URLs/run). |
| `JsonCssExtractionStrategy.generate_schema()` for facts | Separate initiative; closes the facts-coverage gap but not coupled to browser fetch. |
| URL-pattern config-driven routing (`[browser.routes]`) | SiteProfile method covers it; YAGNI for now. |
| Per-render screenshots / PDFs | No consumer in sift today. |
| Network capture in `RenderedPage` | No consumer in sift today. |
| Cookie / auth state persistence | No site profile needs it today. |
| Browser recycling / persistent context | Premature optimization. |

## 12. Resolved decisions

> This section started as resolutions for the three pre-implementation
> open questions (response headers, browser concurrency, missing-dep
> handling) and grew during the review pass to absorb the P-level
> resolutions whose natural home is the same subsections: P0-2
> (shared-crawler lifecycle, in §12.2), P0-3 (skip-state routing, in
> §12.3), and P1-1 (redirect-chain last-wins, in §12.1). Other P-level
> resolutions live inline in the sections they affect (P0-1 in §5.1,
> P0-4 in §8.1, P1-2/P1-4 in §5.2, P1-3/P2-1 in §8, P2-2 in §4.2,
> P2-3 in §5.6).
>
> Shared principle driving every answer: **fail loudly at the boundary
> the operator controls; degrade observably at the boundary we don't.**

### 12.1 Response headers in `RenderedPage` — Playwright Response hook, graceful-None fallback

**Decision**: `sift.browser.render()` installs a Playwright `page.on("response", ...)`
listener (via the `on_page_context_created` crawl4ai hook) that captures
navigation responses. Headers (`etag`, `last-modified`, `cache-control` —
see §5.2 for the persisted-key whitelist, P1-4) are extracted and populated
on `RenderedPage.headers`. On any capture failure, `headers` is `None`
and the URL falls through to full re-render on the next plan cycle.

**Redirect handling (P1-1, F12 refined)**: a SPA navigation through Akamai's
edge can be 3–6 hops (301/302 chain → final 200). The naive
`response.request.is_navigation_request()` filter fires on *every* hop —
and the first hop's `ETag` describes the redirect response, not the final
HTML, so it's useless for cache validation. A pure "last wins" rule isn't
sufficient either: navigations can terminate in 5xx errors, and a failed
final response's headers shouldn't be persisted (no useful `ETag` for
cache validation).

**Resolution**: the hook captures every navigation response unconditionally;
`_capture_navigation_headers` does **capture-then-filter**: walk the event
list, keep only those with `status in (200, 304)`, return the *last* one
of those (or `None` if none match). The two-step pattern handles three
cases cleanly:

| Navigation chain | Captured |
|---|---|
| `301 → 302 → 200 OK` | headers of the 200 (last 200/304) |
| `200 OK → 304 (conditional re-fetch)` | headers of the 304 (last 200/304) |
| `301 → 302 → 503` | `None` (no 200/304 in chain) |
| `503 only` | `None` (no 200/304 in chain) |

Pinned by the `test_response_hook_last_navigation_wins` contract test
(happy-path redirect chain) and the `test_response_hook_returns_none_on_5xx_terminal`
contract test (error-termination case).

**Why not crawl4ai's `capture_network_requests=True`**: known v0.8.6 bug
where `text_body` capture fails on binary responses (`Error capturing
response details: cannot access local variable 'text_body'`), surfaced
during our probe. Network-capture is a debug/diagnostic surface that
crawl4ai's maintainers have refactored between minor releases. Subscribing
to Playwright's native event surface bypasses that fragility.

**Why not HEAD-before-render**: efficient but couples the browser path
to the http path, and ATO's SPA shell may not return meaningful `ETag`
on the static HTML (the actual content is hydrated client-side). Deferred
as `BrowserFetchConfig.head_check: bool = False` opt-in for v0.3.0 if
production metrics show high re-render rates on SPA-heavy profiles.

**Observability**: `sift status` reports `browser_urls_with_cached_headers`
as a fraction. If the fraction trends toward 0, that's the signal to
enable `head_check` or investigate the hook.

### 12.2 Browser concurrency — process-wide, behind a `BrowserPool` interface

**Decision**: a `BrowserPool` that owns *both* a process-wide
`asyncio.Semaphore(N)` for concurrency AND a single lazily-initialized
shared `AsyncWebCrawler` for crawler-process reuse. `N` comes from
`[browser].concurrency` (default 2).

```python
class BrowserPool:
    """Owns the shared AsyncWebCrawler and gates concurrent renders.

    Construct once per sift process; pass the same instance to every
    render() call; close at shutdown. Crawler is lazy-init on first acquire
    (~5-10s startup paid once, not per fetch).
    """

    def __init__(self, concurrency: int, defaults: "BrowserConfigDefaults"): ...

    async def acquire(self, url: str) -> "AsyncContextManager[AsyncWebCrawler]":
        """Async context manager. Blocks until a render slot is available,
        yields the *shared* underlying crawler, releases the slot on exit.
        The crawler is reused across acquires; only the browser context is
        new per render.
        """

    async def aclose(self) -> None:
        """Close the underlying crawler. Idempotent. Call at app shutdown."""
```

The `url` argument is unused today but reserved for a future per-host
implementation. Adding per-host concurrency later means swapping the
internal semaphore for a `dict[host, Semaphore]`; no callsite changes,
no config breakage.

**Why not per-host now**: the binding constraint is RAM (150–300 MB per
browser page), and per-host caps don't help with process-wide RAM budgets.
Sift's typical deployment is one site profile per index — per-host adds
a knob nobody tunes.

**Why an abstraction at all when YAGNI says don't**: the *interface* is the
abstraction, not a parametric design. `BrowserPool` collapses three
concerns (concurrency, crawler lifecycle, future per-host policy) behind
one object the callsite already needs to hold. The alternative — exposing
`Semaphore` + `AsyncWebCrawler` separately to fetch.py — would force fetch.py
to coordinate them correctly. That's the actual cost saved.

**Resolved (P0-2)**: the previous draft talked about acquire() like a
semaphore-only abstraction. That left the crawler-lifecycle question
implicit, and the default reading (fresh crawler per render = 5–10s of
startup × every SPA URL) is wasteful. The spec is now explicit:
`BrowserPool` owns the shared crawler. The contract test
`test_BrowserPool_yields_shared_crawler` below pins this — two acquires
return the same underlying `AsyncWebCrawler` instance.

### 12.3 `BrowserNotInstalledError` — eager fail gated on `[browser].enabled`

**Decision**: at sift CLI startup, if `[browser].enabled=true` in config,
attempt `import crawl4ai` once. On `ImportError`, raise
`BrowserNotInstalledError` with the install hint. If `[browser].enabled=false`,
no import attempted — and any URL the active profile flags as
`requires_browser=True` is short-circuited by `plan.py` to a new member
of the existing `Decision` enum: `Decision.SKIPPED_BROWSER_DISABLED`.

**Three vocabularies kept distinct** (matching how `decide`/`manifest`/`publish`
already work today, per the P0-3 resolution below):

1. **Decision** (`Decision` enum in `decide.py`) gets a new member
   `SKIPPED_BROWSER_DISABLED = "SKIPPED_BROWSER_DISABLED"`, alongside the
   existing `FETCH`, `FETCH_CONDITIONAL`, `SKIP`, `TOMBSTONE_PURGE`.
2. **Manifest state** stores the string literal `"SKIPPED_BROWSER_DISABLED"`
   in the `state` column. Same shape as today's `"FRESH"`, `"GONE"`,
   `"FROZEN"`, `"FAILED"`, `"UNSEEN"` literals. **No exported constant** —
   the codebase doesn't export `manifest.FRESH` either, and we're not going
   to start.
3. **Coverage-gate terminal set** is extracted from today's inlined
   `states.get("FRESH",0) + states.get("GONE",0) + states.get("FROZEN",0)`
   in `publish.gate_coverage` into a module-level
   `_TERMINAL_STATES = {"FRESH", "GONE", "FROZEN", "SKIPPED_BROWSER_DISABLED"}`
   plus an `_is_terminal_state(s: str) -> bool` helper. `gate_coverage` is
   refactored to use the helper.

**Why route in `plan.py` and not in `decide()`**: `decide()` is a pure
function of `(row, sitemap_lastmod, clock, versions)` — it knows nothing
about site profiles or browser config. Threading those through breaks its
contract. The routing concern ("should this URL even reach the fetcher?")
belongs in `plan.py`, which already loops URLs and reads config. A named
`route_to_browser_disabled(url, profile, cfg) -> bool` helper in `plan.py`
lives in one named place, is testable in isolation, and creates a clean
seam for future `route_to_*` siblings (per-host opt-outs, transport
selection, etc.) — avoids the "inline `if` that grows legs" trap.

**`plan()` signature change (F2)**: today's `plan()` is `plan(conn, plan_path,
*, now, extractor_version, normalizer_version, sitemap_lastmod_by_url=None)`.
The browser-disabled routing needs `profile` and `cfg` in scope, so the
signature gains two kwargs:

```python
def plan(
    conn: sqlite3.Connection,
    plan_path: Path,
    *,
    now: datetime,
    extractor_version: str,
    normalizer_version: str,
    profile: SiteProfile,           # NEW — for requires_browser() check
    cfg: IndexConfig,               # NEW — for [browser].enabled check
    sitemap_lastmod_by_url: Optional[dict[str, str]] = None,
) -> dict[str, int]: ...
```

Existing call sites in `cli.py` already have both in scope (every command
calls `_load_cli_config` which returns `IndexConfig`; `current_profile()`
is available from `sift.sites`). Passing them explicitly beats using
`current_profile()` inside `plan()` — keeps the data-flow obvious and the
function testable without thread-local state.

**Five configuration cases handled cleanly**:

| `[browser].enabled` | crawl4ai installed | Profile says `requires_browser=True` | Behavior |
|---|---|---|---|
| `true` | yes | yes | Render |
| `true` | no | — | **Eager fail** at startup |
| `false` | — | yes | URL → `Decision.SKIPPED_BROWSER_DISABLED` (state literal `"SKIPPED_BROWSER_DISABLED"` written on commit) |
| `false` | — | no | Normal http path |
| `true` | yes | no for this URL | Normal http path (no-op for browser surface) |

**Why eager**: a daily cron that fails 3 hours in because the first SPA URL
in the queue hits a missing dep is a real ops failure. Fail-fast at startup
turns that into a 5-second error with a clear hint. The cost is a few
milliseconds of import time at startup when the dep is present.

**Why a new manifest state instead of reusing `SKIPPED`**: distinguishes
"operator explicitly disabled browser" from "manifest says skip for some
other reason." `sift status` reports the count, so operators can see what
disabling browser actually costs them in URL coverage.

**Additive schema change**: see §8 (P0-4 resolution) for the migration
spec. No existing URL has this state value, so production rows are
untouched on upgrade.

**Resolved (P0-3)**: the previous draft conflated three vocabularies that
the existing codebase keeps distinct (`Decision` enum, manifest state
literal, terminal-state set). It also invented a `decide.decide_for_url`
entrypoint that doesn't exist and would break `decide()`'s pure-function
contract. The spec is now reworked to live in `plan.py`'s
`route_to_browser_disabled()` helper. Contract tests in
`test_browser_contract.py` are rewritten to: (a) drop the
`manifest.SKIPPED_BROWSER_DISABLED` constant check, (b) route through
`plan.py` against a real tmp manifest instead of the fictional
`decide_for_url`, (c) keep the `_is_terminal_state` helper check.

### 12.4 Implementation ordering implications

These decisions ratchet the implementation PR's scope. Concretely:

 1. `sift/browser.py`:
    * Module-top lazy-import discipline comment (P2-3)
    * `BrowserFetchConfig` (per-fetch, no `extra_init_scripts`)
    * `RenderedPage` (lowercase keys + `PERSISTED_HEADER_KEYS` whitelist
      enforced by `_project_headers`)
    * `BrowserPool(concurrency, defaults)` owning the shared
      `AsyncWebCrawler` (lazy-init + `aclose()`)
    * `render(url, config, pool)` with Playwright Response hook
      (last-wins on redirect chains)
    * `_capture_navigation_headers(events)` helper testable in isolation
    * `check_browser_available()` (lazy import; raises
      `BrowserNotInstalledError`)
    * `BROWSER_VERSION = "crawl4ai-0.8.6"` co-located here
 2. `sift/sites/__init__.py`:
    * `SiteProfile.requires_browser(url)` default False
    * `SiteProfile.browser_config(url)` default None
 3. `sift/sites/ato.py`:
    * `requires_browser` returns True for `/single-page-applications/`
    * `default_excludes` drops that pattern
 4. `sift/config.py`:
    * `BrowserConfigDefaults` dataclass with `init_scripts` and `user_agent`
    * `IndexConfig.browser` attribute + TOML parse
 5. `sift/manifest.py`:
    * `SCHEMA_VERSION = 2`
    * `_MIGRATIONS` registry + `_migrate(conn, from_v, to_v)`
    * `init_schema()` reads existing version, calls `_migrate` when behind
    * New `browser_version` column in CREATE TABLE
 6. `sift/decide.py`:
    * `Decision.SKIPPED_BROWSER_DISABLED` enum member added
 7. `sift/plan.py`:
    * `plan()` signature gains `profile: SiteProfile` and `cfg: IndexConfig`
      kwargs (F2)
    * `route_to_browser_disabled(url, profile, cfg)` helper
    * `plan()` calls helper before `decide()` to short-circuit
 8. `sift/publish.py`:
    * `_TERMINAL_STATES` set extracted to module level (includes the new
      state)
    * `_is_terminal_state(s)` helper
    * `gate_coverage` refactored to use the helper
 9. `sift/status.py` (NEW, F1):
    * `compute_status_summary(root: Path) -> dict[str, Any]` — extracted
      from cli.py's status command so contract tests can import + call
      directly. Includes the 3 new metrics:
      `skipped_browser_disabled`, `browser_urls_with_cached_headers`,
      `versions.browser_runtime_chromium` (one-time lookup, cached
      per-process)
10. `sift/cli.py`:
    * Eager `check_browser_available()` call at startup (in
      `_load_cli_config` or equivalent), gated on `cfg.browser.enabled`
    * `status` command becomes a thin wrapper:
      `click.echo(json.dumps(compute_status_summary(root), indent=2, default=str))`
11. `pyproject.toml`:
    * `[project.optional-dependencies].browser = ["crawl4ai>=0.8.6"]`

Test contract additions in `test_browser_contract.py` already pin every
item above (67 xfail tests, all `strict=True`).

## 13. Scope frozen

This design is frozen as of 2026-05-25. Implementation PR
(`feat/browser-fetch-impl`) opens against this branch.

### What "frozen" means in practice

- **No new design questions** without a doc update committed first. If
  implementation surfaces an unanswerable question, stop coding and
  amend this doc.
- **No new public-surface additions** beyond what §12.4 enumerates.
  Internal helpers (`_capture_navigation_headers`, `_project_headers`,
  `_TERMINAL_STATES`, `_migrate`, `route_to_browser_disabled`) are
  explicitly named here so impl PRs can't quietly invent new ones.
- **No xfail-test additions** that aren't pinned by this doc. The
  contract file currently has 67 xfails; if impl needs more, the doc
  needs more first.
- **xfail → xpass transitions** are the implementation PR's measure of
  progress. Each test that flips removes a `strict=True` xfail marker;
  the test count drops only when the contract is met.

### What's NOT frozen (intentionally deferred to v0.3.0)

These were considered, rejected for v0.2.0, and documented in §11 or
inline:

- HEAD-before-render optimization (P1-1 footnote; only if production
  metrics show high re-render rates)
- Per-host `BrowserPool` concurrency (P0-2; `acquire(url)` already
  accepts the arg, internal implementation can swap later)
- Sending captured `ETag` / `Last-Modified` as `If-None-Match` /
  `If-Modified-Since` in browser path (P2-2 case 2; deferred — body
  transfer isn't the bottleneck in render time)
- `sift discover-api` CLI (network-capture tooling for profile authors)
- `JsonCssExtractionStrategy.generate_schema()` to bootstrap facts
  extractors (closes facts-coverage gap; separate initiative)
- `MemoryAdaptiveDispatcher` for production browser-fetch deployments
- URL-pattern config-driven routing (`[browser.routes]`)
- Per-render screenshots / PDFs
- `network_requests` capture surface on `RenderedPage`
- Cookie / auth state persistence
- Browser recycling / persistent context
- `patchright` undetected browser swap (ATO is polite; not needed)

Each can be added later under its own design doc + contract-test pass.
The single-file crawl4ai coupling (`sift/browser.py`) means any of them
can be added without touching the rest of sift.

### Review history

| Commit | What |
|---|---|
| `7049d22` | Initial design doc + 37 xfail tests |
| `46db043` | §12 resolved-decisions section + 15 xfail tests |
| `942f4e4` | P0×4 review items resolved + 10 xfail tests |
| `9134008` | P1×4 + P2-1 resolved + 5 xfail tests |
| `45c4bd7` | P2-2 + P2-3 resolved (doc-only) |
| (this commit) | Scope freeze: banner, §12.4 update, §13 added |

Implementation PR is `feat/browser-fetch-impl`, branching from this commit.
