"""Pure state-machine: given (manifest row, tier, sitemap lastmod, now),
return one of {FETCH, FETCH_CONDITIONAL, SKIP, TOMBSTONE_PURGE}.

Every threshold lives here so the planning phase is fully reproducible
from inputs alone — no clock outside `now`, no I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .classify import Tier
from .manifest import (
    ManifestRow,
    STATE_FAILED,
    STATE_GONE,
)

# Refresh intervals per tier: (floor, ceiling) for adaptive recrawl.
# Floor = minimum gap between fetches; ceiling = cap for unchanged-streak backoff.
# Module-level (mutable) so apply_config() can swap them at CLI startup.
TIER_INTERVALS: dict[Tier, tuple[timedelta, timedelta]] = {
    Tier.NEWS:           (timedelta(days=1),  timedelta(days=7)),
    Tier.LIVING:         (timedelta(days=7),  timedelta(days=90)),
    Tier.CURRENT_FORMS:  (timedelta(days=14), timedelta(days=180)),
    Tier.FROZEN:         (timedelta(days=365), timedelta(days=730)),
}

# How long a 404/410 stays tombstoned before we re-probe.
TOMBSTONE_TTL: dict[Tier, timedelta] = {
    Tier.NEWS:           timedelta(days=30),
    Tier.LIVING:         timedelta(days=90),
    Tier.CURRENT_FORMS:  timedelta(days=180),
    Tier.FROZEN:         timedelta(days=730),
}

# Retry budget per URL per run before promoting to GONE.
MAX_FAILURES_BEFORE_GONE: dict[Tier, int] = {
    Tier.NEWS:           10,
    Tier.LIVING:         20,
    Tier.CURRENT_FORMS:  20,
    Tier.FROZEN:         5,
}


def apply_config(cfg) -> None:
    """Replace tier intervals / tombstone TTLs / failure budgets from an IndexConfig.

    Idempotent. Called once during CLI startup. Tests reset state by passing
    a default-IndexConfig if they need to.
    """
    global TIER_INTERVALS, TOMBSTONE_TTL, MAX_FAILURES_BEFORE_GONE
    TIER_INTERVALS = {
        Tier[name]: (tc.floor, tc.ceiling) for name, tc in cfg.tiers.items()
    }
    TOMBSTONE_TTL = {
        Tier[name]: tc.tombstone_ttl for name, tc in cfg.tiers.items()
    }
    MAX_FAILURES_BEFORE_GONE = {
        Tier[name]: tc.max_failures for name, tc in cfg.tiers.items()
    }


class Decision(str, Enum):
    FETCH = "FETCH"                         # unconditional GET
    FETCH_CONDITIONAL = "FETCH_CONDITIONAL" # GET with If-None-Match / If-Modified-Since
    SKIP = "SKIP"                           # within recrawl interval, no sitemap signal
    TOMBSTONE_PURGE = "TOMBSTONE_PURGE"     # GONE row past TTL with no re-discovery
    # Operator disabled [browser].enabled but profile.requires_browser(url)=True.
    # plan.py short-circuits to this value via route_to_browser_disabled() before
    # calling decide(); decide() itself never returns this value. See P0-3.
    SKIPPED_BROWSER_DISABLED = "SKIPPED_BROWSER_DISABLED"


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # Accept both with and without trailing Z; assume UTC if naive.
    s = s.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def next_recrawl_interval(row: ManifestRow, tier: Tier) -> timedelta:
    """Adaptive backoff: floor * 2^unchanged_streak, clamped to ceiling."""
    floor, ceiling = TIER_INTERVALS[tier]
    streak = max(0, row.unchanged_streak)
    # Clamp the exponent so we don't overflow on very stable pages.
    exp = min(streak, 16)
    return max(floor, min(floor * (2 ** exp), ceiling))


def tombstone_ttl(tier: Tier) -> timedelta:
    return TOMBSTONE_TTL[tier]


def decide(
    row: Optional[ManifestRow],
    tier: Tier,
    sitemap_lastmod: Optional[str],
    now: datetime,
    *,
    extractor_version: str,
    normalizer_version: str,
) -> tuple[Decision, str]:
    """Return (Decision, human-readable reason).

    Decision rules (in priority order):
      1. UNSEEN row -> FETCH (no etag yet, full GET).
      2. FROZEN tier + content already extracted at current versions -> SKIP.
      3. GONE tombstone within TTL and not re-listed by sitemap -> SKIP;
         past TTL -> TOMBSTONE_PURGE.
      4. FAILED with budget remaining -> FETCH (retry; clears partial state).
      5. Sitemap lastmod newer than what we've seen -> FETCH_CONDITIONAL.
      6. Within tier's recrawl interval -> SKIP.
      7. Otherwise -> FETCH_CONDITIONAL.

    Versions: if extractor or normalizer version moved, we still SKIP fetch
    (extract phase will re-derive from cached raw HTML).
    """
    if row is None:
        return Decision.FETCH, "unseen"

    if row.state == STATE_GONE:
        last_attempt = _parse_iso(row.last_attempted_at) or _parse_iso(row.first_seen_at)
        if last_attempt and (now - last_attempt) < tombstone_ttl(tier):
            # Re-probe early only if sitemap explicitly re-listed it newer than the tombstone.
            sm = _parse_iso(sitemap_lastmod)
            if sm and last_attempt and sm > last_attempt:
                return Decision.FETCH, "gone-but-resurrected-in-sitemap"
            return Decision.SKIP, "tombstoned"
        return Decision.TOMBSTONE_PURGE, "tombstone-ttl-expired"

    if tier == Tier.FROZEN and row.content_hash and row.state != STATE_FAILED:
        # Already extracted with current versions? SKIP entirely.
        if (row.extractor_version == extractor_version and
                row.normalizer_version == normalizer_version):
            return Decision.SKIP, "frozen-current-versions"
        # Versions moved — extract phase re-derives from cached raw. No refetch.
        return Decision.SKIP, "frozen-extract-will-redrive"

    if row.state == STATE_FAILED:
        if row.fail_count < MAX_FAILURES_BEFORE_GONE[tier]:
            return Decision.FETCH, f"retry-fail-count-{row.fail_count}"
        return Decision.SKIP, "failure-budget-exhausted"

    sm = _parse_iso(sitemap_lastmod)
    last_fetched = _parse_iso(row.last_fetched_at)

    if last_fetched is None:
        # Have a row but never fetched (e.g. seeded but never crawled).
        return Decision.FETCH, "never-fetched"

    # Sitemap signal: only refetch if the page was modified AFTER our last
    # successful fetch. Comparing against sitemap_lastmod_seen (the old logic)
    # caused spurious re-fetches whenever we seeded a sitemap for the first
    # time — every URL would appear "newer" relative to a previously-NULL seen,
    # even though our content was already current.
    if sm is not None and sm > last_fetched:
        return Decision.FETCH_CONDITIONAL, "sitemap-lastmod-after-last-fetch"

    interval = next_recrawl_interval(row, tier)
    if (now - last_fetched) < interval:
        return Decision.SKIP, f"within-interval-{interval.days}d"

    return Decision.FETCH_CONDITIONAL, "interval-elapsed"
