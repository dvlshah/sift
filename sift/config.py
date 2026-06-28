"""Single source of truth for operational indexing parameters.

Load order:
    1. CLI flag (--config PATH) — explicit file
    2. ./sift.toml — convention
    3. ./sift.local.toml — gitignored local overrides
    4. Built-in defaults

CLI flags still override anything from the config file at call time
(e.g. `sift run --rate 5` wins over `[crawl] rate_per_sec = 3`).

Why a config file at all: lets you keep a `weekly.toml` and a `daily.toml`
side-by-side, version them in git, and audit what parameters produced
any given snapshot. snapshot.json records the merged config so a future
auditor can reconstruct the run.

Format: TOML (tomllib in stdlib for 3.11+, zero extra deps).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional


# ---- Dataclasses ------------------------------------------------------------


@dataclass(frozen=True)
class FirecrawlScrapeConfig:
    """Firecrawl ``/v2/scrape`` fallback fetcher.

    Off by default. When ``enabled`` and the native HTTP fetcher returns one of
    ``fallback_statuses`` (default 401/403 — the Cloudflare / Akamai edge
    bot-block signatures), sift escalates that single URL to Firecrawl, takes
    the returned ``data.html``, and feeds it through the normal extract path
    so ``content_hash`` provenance stays under sift's control end-to-end.

    Cost is tracked in credits (``metadata.creditsUsed`` per response). Hard
    ceiling at ``max_credits_per_run``; past that, native failures stand and a
    warn line fires. Auth comes from the ``FIRECRAWL_API_KEY`` env var — never
    from this config file.

    Cache: ``max_cache_age_ms`` controls Firecrawl's server-side cache lookup.
    Default 0 = always fresh from origin (correct for a fallback fetcher where
    freshness is the whole reason we're calling). Operators trading freshness
    for cost can align it with their tier's ``floor_days``.
    """
    enabled: bool = False
    fallback_statuses: tuple[int, ...] = (401, 403)
    proxy: str = "auto"                  # "basic" | "enhanced" | "auto"
    max_credits_per_run: int = 100
    max_cache_age_ms: int = 0            # 0 = always fresh from origin
    rate_per_sec: float = 0.5
    concurrency: int = 2
    timeout_sec: float = 60.0
    # When True, a 200-but-thin page (empty SPA shell / JS challenge) may
    # escalate to Firecrawl too. Off by default: the *free* curl_cffi tier
    # handles thin content first, and we don't want thin pages silently
    # burning paid credits unless the operator opts in.
    escalate_on_thin: bool = False


@dataclass(frozen=True)
class ImpersonateConfig:
    """Tier-2 TLS-impersonation fetcher (curl_cffi). Free, self-hosted.

    When ``enabled`` and the native httpx fetch hits an ``escalate_statuses``
    code (default 403/429/503 — fingerprint/bot-manager blocks), a network
    failure, or a thin 200, sift re-fetches the URL with a real browser's
    TLS/HTTP2 fingerprint. Clears most "hardened" edges (Cloudflare/Akamai/
    Imperva) with no browser and no per-request cost — so it sits *before*
    the paid Firecrawl tier and absorbs the bulk of the escalation load.
    Optional dep: ``pip install 'sift-engine[impersonate]'``.
    """
    enabled: bool = False
    impersonate: str = "chrome"          # curl_cffi target: chrome|safari|edge|...
    escalate_statuses: tuple[int, ...] = (403, 429, 503)
    thin_text_threshold: int = 500       # pool's own re-check after impersonating
    rate_per_sec: float = 1.0
    concurrency: int = 4
    timeout_sec: float = 30.0


@dataclass(frozen=True)
class CrawlConfig:
    rate_per_sec: float = 3.0
    concurrency: int = 8
    timeout_sec: float = 30.0
    retries: int = 3
    user_agent: Optional[str] = None  # None -> built-in identifier in fetch.py
    # Respect robots.txt Disallow rules: a Disallowed URL is dropped at seed time
    # (never fetched). Set False only for sources you have permission to index. A
    # missing/unreachable robots.txt allows everything (standard semantics).
    respect_robots: bool = True
    # Content-quality escalation trigger: a 2xx whose visible text is below this
    # routes UP the transport ladder instead of being committed. 0 disables it
    # (back-compat for callers that don't opt in).
    thin_text_threshold: int = 500
    # Adaptive per-host floor: after this many native blocks, a host's remaining
    # URLs skip the native round-trip and start at the escalation ladder. 0 = off.
    host_block_floor: int = 3
    firecrawl: FirecrawlScrapeConfig = field(default_factory=FirecrawlScrapeConfig)
    impersonate: ImpersonateConfig = field(default_factory=ImpersonateConfig)


@dataclass(frozen=True)
class TierConfig:
    """Per-tier refresh + tombstone behavior. Days as ints for TOML simplicity."""
    floor_days: int
    ceiling_days: int
    tombstone_ttl_days: int
    max_failures: int

    @property
    def floor(self) -> timedelta:
        return timedelta(days=self.floor_days)

    @property
    def ceiling(self) -> timedelta:
        return timedelta(days=self.ceiling_days)

    @property
    def tombstone_ttl(self) -> timedelta:
        return timedelta(days=self.tombstone_ttl_days)


@dataclass(frozen=True)
class PublishConfig:
    coverage_floor: float = 0.99
    hash_sample_rate: float = 0.01
    hash_sample_min: int = 25
    schema_sample_size: int = 50
    # Optional GPG key for detach-signing snapshot.json. None disables signing.
    # Set to a key id (long form or fingerprint) when you want audit-grade
    # snapshot integrity.
    gpg_key_id: Optional[str] = None
    # Optional RFC-3161 Time-Stamp Authority URL (e.g. http://timestamp.digicert.com).
    # None disables external timestamping. When set, each publish anchors the
    # snapshot's merkle_root to this independent TSA — a third party's witness to
    # the root's date, verifiable with `openssl ts -verify`.
    timestamp_tsa_url: Optional[str] = None


@dataclass(frozen=True)
class SeedConfig:
    host_allow: tuple[str, ...] = ("www.ato.gov.au",)
    use_default_excludes: bool = True
    extra_exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteConfig:
    """Per-site profile selection.

    The profile is a `module:Class` import path resolved at CLI startup.
    Defaults to ATOProfile so existing operators keep working without config
    changes. To target a different site, write a SiteProfile subclass and
    point this at it:

        [site]
        profile = "mycorp.sites.irs:IRSProfile"
    """
    profile: str = "sift.sites.ato:ATOProfile"


@dataclass
class BrowserConfigDefaults:
    """[browser] section — defaults for the crawl4ai-backed renderer.

    Not frozen because callers and tests adjust ``enabled`` at runtime
    (the CLI may toggle it from --no-browser; tests flip it for branch
    coverage). The other config dataclasses are frozen because their values
    feed phase functions; these defaults feed the long-lived BrowserPool
    instead, which holds a private snapshot.

    Init-scripts live here (not on BrowserFetchConfig) because Playwright's
    add_init_script binds per-context — they have to be set at
    AsyncWebCrawler construction. See P0-1 in the design doc.
    """
    enabled: bool = True
    concurrency: int = 2
    page_timeout_s: int = 60
    wait_until: str = "networkidle"
    flatten_shadow_dom: bool = False
    remove_consent_popups: bool = False
    user_agent: str = ""
    # Tuple (not list) so the dataclass is hashable if a caller ever needs it.
    init_scripts: tuple[str, ...] = ()


# Default tier configuration — matches the operational design discussed in
# decide.py (NEWS daily-ish, LIVING weekly, CURRENT_FORMS bi-weekly, FROZEN annual).
DEFAULT_TIERS: dict[str, TierConfig] = {
    "NEWS":           TierConfig(floor_days=1,   ceiling_days=7,   tombstone_ttl_days=30,  max_failures=10),
    "LIVING":         TierConfig(floor_days=7,   ceiling_days=90,  tombstone_ttl_days=90,  max_failures=20),
    "CURRENT_FORMS":  TierConfig(floor_days=14,  ceiling_days=180, tombstone_ttl_days=180, max_failures=20),
    "FROZEN":         TierConfig(floor_days=365, ceiling_days=730, tombstone_ttl_days=730, max_failures=5),
}


@dataclass(frozen=True)
class IndexConfig:
    """The whole config tree, loaded once at CLI startup."""
    current_fy_start_year: int = 2025
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    seed: SeedConfig = field(default_factory=SeedConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    browser: BrowserConfigDefaults = field(default_factory=BrowserConfigDefaults)
    tiers: dict[str, TierConfig] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    # Path the config was loaded from (None if pure defaults). Used for snapshot.json.
    source_path: Optional[str] = None


# ---- Loader -----------------------------------------------------------------


# Standard search paths, in priority order. Earlier = wins.
DEFAULT_SEARCH_PATHS: tuple[str, ...] = (
    "sift.local.toml",
    "sift.toml",
)


def _resolve_config_path(explicit: Optional[Path]) -> Optional[Path]:
    """Pick the config file to use. Explicit arg wins; else search defaults
    in the current working directory."""
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return explicit
    for name in DEFAULT_SEARCH_PATHS:
        p = Path(name)
        if p.exists():
            return p
    return None


def _parse_firecrawl_config(raw: dict[str, Any]) -> FirecrawlScrapeConfig:
    """Parse ``[crawl.firecrawl]`` subsection. Missing keys take dataclass
    defaults so operators can flip ``enabled = true`` and inherit everything
    else."""
    return FirecrawlScrapeConfig(
        enabled=bool(raw.get("enabled", False)),
        fallback_statuses=tuple(int(s) for s in raw.get("fallback_statuses", (401, 403))),
        proxy=str(raw.get("proxy", "auto")),
        max_credits_per_run=int(raw.get("max_credits_per_run", 100)),
        max_cache_age_ms=int(raw.get("max_cache_age_ms", 0)),
        rate_per_sec=float(raw.get("rate_per_sec", 0.5)),
        concurrency=int(raw.get("concurrency", 2)),
        timeout_sec=float(raw.get("timeout_sec", 60.0)),
        escalate_on_thin=bool(raw.get("escalate_on_thin", False)),
    )


def _parse_impersonate_config(raw: dict[str, Any]) -> ImpersonateConfig:
    """Parse ``[crawl.impersonate]`` subsection. Missing keys take defaults so
    operators can flip ``enabled = true`` and inherit everything else."""
    return ImpersonateConfig(
        enabled=bool(raw.get("enabled", False)),
        impersonate=str(raw.get("impersonate", "chrome")),
        escalate_statuses=tuple(int(s) for s in raw.get("escalate_statuses", (403, 429, 503))),
        thin_text_threshold=int(raw.get("thin_text_threshold", 500)),
        rate_per_sec=float(raw.get("rate_per_sec", 1.0)),
        concurrency=int(raw.get("concurrency", 4)),
        timeout_sec=float(raw.get("timeout_sec", 30.0)),
    )


def _parse_tiers(raw: dict[str, Any]) -> dict[str, TierConfig]:
    """Tier subsection allows partial overrides — missing keys fall back to
    DEFAULT_TIERS for that tier, so users can change one number without
    re-specifying the rest."""
    out = dict(DEFAULT_TIERS)
    for name, vals in raw.items():
        base = out.get(name) or DEFAULT_TIERS.get(name)
        if base is None:
            raise ValueError(
                f"unknown tier '{name}' in [tiers] section. "
                f"Valid: {sorted(DEFAULT_TIERS)}"
            )
        merged = {
            "floor_days":          vals.get("floor_days",          base.floor_days),
            "ceiling_days":        vals.get("ceiling_days",        base.ceiling_days),
            "tombstone_ttl_days":  vals.get("tombstone_ttl_days",  base.tombstone_ttl_days),
            "max_failures":        vals.get("max_failures",        base.max_failures),
        }
        # Light validation — catch obvious typos before a 7-day crawl.
        if merged["floor_days"] <= 0 or merged["ceiling_days"] < merged["floor_days"]:
            raise ValueError(
                f"tier '{name}': floor_days must be > 0 and <= ceiling_days, "
                f"got floor={merged['floor_days']} ceiling={merged['ceiling_days']}"
            )
        out[name] = TierConfig(**merged)
    return out


def load_config(path: Optional[Path] = None) -> IndexConfig:
    """Resolve the config file (or use defaults) and return an IndexConfig.

    The returned object is immutable (frozen dataclasses), so passing it
    through phase calls is safe — no module can accidentally mutate it.
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        return IndexConfig(source_path=None)

    with resolved.open("rb") as f:
        data = tomllib.load(f)

    crawl_raw = data.get("crawl", {})
    publish_raw = data.get("publish", {})
    seed_raw = data.get("seed", {})
    site_raw = data.get("site", {})
    browser_raw = data.get("browser", {})
    fy_raw = data.get("fy", {})
    tiers_raw = data.get("tiers", {})

    return IndexConfig(
        current_fy_start_year=int(fy_raw.get("current_start_year", 2025)),
        crawl=CrawlConfig(
            rate_per_sec=float(crawl_raw.get("rate_per_sec", 3.0)),
            concurrency=int(crawl_raw.get("concurrency", 8)),
            timeout_sec=float(crawl_raw.get("timeout_sec", 30.0)),
            retries=int(crawl_raw.get("retries", 3)),
            user_agent=crawl_raw.get("user_agent"),
            respect_robots=bool(crawl_raw.get("respect_robots", True)),
            thin_text_threshold=int(crawl_raw.get("thin_text_threshold", 500)),
            host_block_floor=int(crawl_raw.get("host_block_floor", 3)),
            firecrawl=_parse_firecrawl_config(crawl_raw.get("firecrawl", {})),
            impersonate=_parse_impersonate_config(crawl_raw.get("impersonate", {})),
        ),
        publish=PublishConfig(
            coverage_floor=float(publish_raw.get("coverage_floor", 0.99)),
            hash_sample_rate=float(publish_raw.get("hash_sample_rate", 0.01)),
            hash_sample_min=int(publish_raw.get("hash_sample_min", 25)),
            schema_sample_size=int(publish_raw.get("schema_sample_size", 50)),
            gpg_key_id=publish_raw.get("gpg_key_id") or None,
            timestamp_tsa_url=publish_raw.get("timestamp_tsa_url") or None,
        ),
        seed=SeedConfig(
            host_allow=tuple(seed_raw.get("host_allow", ("www.ato.gov.au",))),
            use_default_excludes=bool(seed_raw.get("use_default_excludes", True)),
            extra_exclude_patterns=tuple(seed_raw.get("extra_exclude_patterns", ())),
        ),
        site=SiteConfig(
            profile=site_raw.get("profile", "sift.sites.ato:ATOProfile"),
        ),
        browser=BrowserConfigDefaults(
            enabled=bool(browser_raw.get("enabled", True)),
            concurrency=int(browser_raw.get("concurrency", 2)),
            page_timeout_s=int(browser_raw.get("page_timeout_s", 60)),
            wait_until=str(browser_raw.get("wait_until", "networkidle")),
            flatten_shadow_dom=bool(browser_raw.get("flatten_shadow_dom", False)),
            remove_consent_popups=bool(browser_raw.get("remove_consent_popups", False)),
            user_agent=str(browser_raw.get("user_agent", "") or ""),
            init_scripts=tuple(browser_raw.get("init_scripts", ()) or ()),
        ),
        tiers=_parse_tiers(tiers_raw),
        source_path=str(resolved),
    )
