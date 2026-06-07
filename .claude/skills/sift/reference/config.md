# sift config & profile reference

## Config discovery & precedence

A single TOML controls everything tunable. Resolution: `--config PATH` if given, else search the working dir for `sift.local.toml` (first) then `sift.toml`; else built-in defaults. **CLI flags override config; config overrides defaults.** Use `sift.local.toml` (gitignored) for per-machine overrides like a faster `rate_per_sec` or `[browser].enabled=false`.

## Annotated `sift.toml`

```toml
# ---- Site profile: per-site URL classification + facts schemas -------------
[site]
profile = "sift.sites.ato:ATOProfile"   # "module:ClassName"; see profiles below

# ---- Financial year: bump once per FY (July 1). year < this => FROZEN tier --
[fy]
current_start_year = 2025

# ---- Crawl politeness + parallelism ----------------------------------------
[crawl]
rate_per_sec = 5.0        # per-host token bucket (CLI: --rate)
concurrency  = 8          # in-flight HTTP cap, still rate-limited (CLI: --concurrency)
timeout_sec  = 30.0
retries      = 3          # per-URL transient-error retries within one run
# user_agent = "my-crawler/1.0 (+contact)"   # unset => sift's identifying UA
# [crawl.firecrawl]  max_credits_per_run = N  # caps --firecrawl-fallback spend

# ---- Publish-gate thresholds -----------------------------------------------
[publish]
coverage_floor     = 0.99   # G3: fraction of seeded URLs that must reach terminal
hash_sample_rate   = 0.01   # 1% of md files re-hashed each publish
hash_sample_min    = 25     # ...but never fewer than this
schema_sample_size = 50     # files checked for structural sanity
# gpg_key_id = "..."        # set => gpg --detach-sign snapshot.json

# ---- Seed-time URL filtering -----------------------------------------------
[seed]
host_allow            = ["www.ato.gov.au"]   # the crawl + write-mode boundary
use_default_excludes  = true                  # /sitemap*, /api/*, /print/*, ...
extra_exclude_patterns = ["^/other-languages/", "^/media-centre/"]

# ---- Browser fetch (optional; only if a profile opts a URL in) -------------
[browser]
enabled        = false              # true needs the [browser] extra + chromium
concurrency    = 2                  # per-process renders, independent of [crawl]
page_timeout_s = 30
wait_until     = "domcontentloaded" # profiles override (ATO uses "networkidle")
user_agent     = ""                 # empty => sift's UA
init_scripts   = []                 # per-context add_init_script hooks

# ---- Per-tier refresh + tombstone behavior ---------------------------------
# Each tier inherits unspecified fields from built-in defaults.
[tiers.NEWS]          # high churn   — floor 1d / ceiling 7d   / ttl 30d  / maxfail 10
[tiers.LIVING]        # evergreen    — floor 7d / ceiling 90d  / ttl 90d  / maxfail 20
[tiers.CURRENT_FORMS] # annual cycle — floor 14d/ ceiling 180d / ttl 180d / maxfail 20
[tiers.FROZEN]        # historical   — floor 365d/ceiling 730d / ttl 730d / maxfail 5
#   floor_days   — min age before a refetch is even considered
#   ceiling_days — max age before a refetch is forced
#   tombstone_ttl_days — how long a GONE URL lingers before TOMBSTONE_PURGE
#   max_failures — consecutive failures before a URL is parked
```

### Multi-index note
For a multi-index server, each sub-index directory carries its **own** `sift.toml`. Write mode reads the per-slug `[seed].host_allow` at write time — a sub-index with no `host_allow` is **not writeable** (the server has nothing to enforce against), even with `--enable-index`.

## Tiers

Refresh-rhythm taxonomy, generic across sites; the **profile** decides which URLs land in which tier.

| Tier | Meaning | Refresh rhythm |
|---|---|---|
| `NEWS` | high-churn announcements | daily-ish |
| `LIVING` | evergreen guidance | weekly-ish |
| `CURRENT_FORMS` | annual-cycle content | per season |
| `FROZEN` | historical, immutable (year < `[fy].current_start_year`) | rarely |

## Shipped `SiteProfile`s

| `profile =` | For |
|---|---|
| `sift.sites.ato:ATOProfile` | Australian Taxation Office (reference profile; default). SPAs under `/single-page-applications/` opt into browser. |
| `sift.sites.generic:GenericProfile` | **Any site, no special structure** — every URL `LIVING`, no facts, HTTP only. Best first choice for a new site. |
| `sift.sites.generic_browser:GenericBrowserProfile` | Generic, but every URL via browser (JS-heavy sites). |
| `sift.sites.augov:AUGovProfile` / `:SAGovProfile` | AU government base / SA-specific. |
| `sift.sites.stripe:StripeDocsProfile` | Stripe API docs. |
| `sift.sites.mdn:MDNProfile` | MDN web reference. |
| `sift.sites.python_docs:PythonDocsProfile` | Python language docs. |

## The `SiteProfile` contract

A profile isolates all site-specific logic from the core pipeline. It owns:

- **URL classification** — `classify_tier(url, current_year_start)`, `audience(url)`, year-extraction format.
- **`parent_guide` extraction** — grouping multi-page guides.
- **`default_excludes`** — per-site URL skip patterns.
- **`dynamic_patterns`** — regexes for volatile content (timestamps, nonces) stripped *before* hashing, so `content_hash` is stable across fetches.
- **Section taxonomy** — top-level grouping for the agent-facing `INDEX.md`.
- **Facts schemas + extractor registry** — what structured records to mine and how.
- **Transport routing** — `requires_browser(url)` flips a URL to the browser path; `browser_config(url)` returns a per-URL `BrowserFetchConfig` override.

Minimal example:

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

    @property
    def dynamic_patterns(self):
        return (re.compile(r"^Page Last Reviewed.*$", re.M),)

    def classify_tier(self, url, current_year_start):
        ...   # IRS uses calendar years, not FY

    def audience(self, url):
        ...   # /individuals/, /businesses/, /charities-non-profits/
```

Then set `profile = "sift.sites.irs:IRSProfile"` in `sift.toml`, reseed, and run — no core code changes. Start from `GenericProfile` and add only the methods you need.
