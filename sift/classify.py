"""URL canonicalization + tier classification.

This module is **site-agnostic**. All site-specific decisions delegate to
the active SiteProfile (see `sift.sites`):

  * `classify_tier(url)` → profile.classify_tier(url, current_year_start)
  * `audience(url)`       → profile.audience(url)
  * `fy_years(url)`       → profile.fy_years(url)
  * `parent_guide(url)`   → profile.parent_guide(url)
  * `DEFAULT_EXCLUDE_PATTERNS` → profile.default_excludes

What stays here:

  * `Tier` enum — generic refresh-rhythm taxonomy (LIVING/NEWS/CURRENT_FORMS/FROZEN)
  * `canonicalize_url` — lowercase host, sort query, strip fragment + tracking
  * `safe_path_segments` — filesystem-safe URL path → segments
  * Module-level FY start year (settable from config)

Bumping `CLASSIFIER_VERSION` causes the seed phase to re-classify all rows
on next run — bump whenever the profile's tier/audience logic moves.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .sites import current_profile

CLASSIFIER_VERSION = "v2"  # bumped when classify moved to profile-delegation

# Module-level FY-start year, settable via set_current_fy_start_year().
# The profile uses this to decide past/current/future year boundaries.
_CURRENT_FY_START_YEAR = 2025


def set_current_fy_start_year(year: int) -> None:
    """Override the FY cutoff. Called once during CLI startup after config load."""
    global _CURRENT_FY_START_YEAR
    _CURRENT_FY_START_YEAR = year


def current_fy_start_year() -> int:
    return _CURRENT_FY_START_YEAR


# Tracking-only query params dropped during canonicalization. Generic — same
# set is junk across every site.
_DROP_PARAM_PREFIXES = ("utm_", "mc_", "_hs")
_DROP_PARAM_EXACT = frozenset({"gclid", "fbclid", "msclkid", "yclid", "wbraid", "gbraid"})


class Tier(str, Enum):
    """Refresh-rhythm taxonomy. Generic across sites — the profile decides
    which URLs fall into which tier."""
    FROZEN = "FROZEN"                  # historical, immutable
    CURRENT_FORMS = "CURRENT_FORMS"    # annual-cycle content
    LIVING = "LIVING"                  # evergreen, weekly refresh
    NEWS = "NEWS"                      # high churn, daily refresh


# ---- URL helpers (generic) -------------------------------------------------


def canonicalize_url(url: str) -> str:
    """Stable URL form: lowercased host, no fragment, sorted non-tracking
    query params, trailing slash stripped (except root)."""
    p = urlparse(url.strip())
    host = p.netloc.lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    qs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.startswith(_DROP_PARAM_PREFIXES) and k not in _DROP_PARAM_EXACT
    ]
    qs.sort()
    return urlunparse((p.scheme.lower(), host, path, "", urlencode(qs), ""))


_MAX_URL_LEN = 2048      # ~ HTTP spec's de-facto cap; URLs longer than this
                         # are almost always malformed concatenations from a
                         # discovery source that joined multiple links.
_MAX_PATH_SEGMENT = 200  # POSIX PATH_MAX leaves ~1024 bytes for the whole
                         # path; cap each segment so a deep tree can still fit
                         # under the limit and we don't blow up on mkdir.


def is_malformed_url(url: str) -> bool:
    """Sanity-check URLs from upstream discovery sources before they hit
    seed/plan/fetch. Catches the two classes we've seen in real bench runs:

    1. **Concatenation artifacts** — Firecrawl /map occasionally returns
       multiple URLs joined by ``%20`` (URL-encoded space). They have the
       shape ``https://x/a%20https://x/b%20...`` and survive
       ``canonicalize_url`` since they're syntactically a valid URL with a
       weird path. They blow up at md_path generation because the resulting
       filesystem path exceeds PATH_MAX.
    2. **Implausible length** — anything over 2048 chars (the de-facto HTTP
       URL cap) is almost certainly a discovery-source bug, not a real page.
    """
    if len(url) > _MAX_URL_LEN:
        return True
    # Multiple scheme markers in the path == concatenation artifact
    return "%20https:" in url or "%20http:" in url


def safe_path_segments(url: str) -> list[str]:
    """URL path -> filesystem-safe segments for mirroring under md/.

    Each segment is replaced char-by-char (non-``[A-Za-z0-9._-]`` → ``_``)
    and capped at ``_MAX_PATH_SEGMENT`` chars so a deep tree fits under
    POSIX PATH_MAX even for very long URLs.
    """
    parts = [seg for seg in urlparse(url).path.split("/") if seg]
    safe = [re.sub(r"[^A-Za-z0-9._\-]", "_", seg) for seg in parts]
    return [s[:_MAX_PATH_SEGMENT] for s in safe]


# ---- Profile delegation ----------------------------------------------------


def classify_tier(url: str) -> Tier:
    """Map URL to a Tier via the active site profile."""
    return Tier(current_profile().classify_tier(url, _CURRENT_FY_START_YEAR))


def audience(url: str) -> str:
    """Coarse audience tag via the active site profile."""
    return current_profile().audience(url)


def fy_years(url: str) -> list[str]:
    """All financial/calendar years extracted from the URL, formatted
    per the site's convention."""
    return current_profile().fy_years(url)


def parent_guide(url: str) -> Optional[str]:
    """For multi-page guides, return the guide slug (else None)."""
    return current_profile().parent_guide(url)


# ---- Exclude patterns (delegated) ------------------------------------------


# Module-level constant — resolved once at import time against the active
# profile (see sift.sites.current_profile). Consumed by the seed-time
# exclude wiring in cli.seed.
DEFAULT_EXCLUDE_PATTERNS = current_profile().default_excludes


def compile_excludes(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    """Compile a tuple of regex strings into pattern objects."""
    return [re.compile(p) for p in patterns]


def is_excluded(url: str, compiled: list[re.Pattern[str]]) -> bool:
    """True if the URL's path matches any of the compiled excludes."""
    path = urlparse(url).path or "/"
    return any(p.search(path) for p in compiled)
