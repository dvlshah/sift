"""Phase 1: PLAN — produce per-URL decisions from manifest state.

Reads the manifest, writes plan.jsonl. One line per URL with a Decision +
the conditional-GET headers (etag, last_modified) needed by the fetch phase.

Sitemap lastmod is per-URL input and may come from sitemap.xml or from a
previously-seeded URL dump that included it. Pass `sitemap_lastmod_by_url`
to override what's stored in the manifest.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .browser import BROWSER_VERSION  # module-level constant; importing here is safe
from .classify import Tier
from .decide import Decision, decide
from .manifest import iter_all

if TYPE_CHECKING:
    from .config import IndexConfig
    from .sites import SiteProfile


@dataclass
class PlanEntry:
    url: str
    tier: str
    parent_guide: Optional[str]
    decision: str
    reason: str
    etag: Optional[str]
    last_modified: Optional[str]
    sitemap_lastmod: Optional[str]


def route_to_browser_disabled(
    url: str,
    profile: "SiteProfile",
    cfg: "IndexConfig",
) -> bool:
    """Return True iff this URL needs the browser but the operator turned it off.

    Named helper (not inlined) so future ``route_to_*`` siblings — per-host
    opt-outs, transport selection — have a clean home. See P0-3 in the
    design doc. Pure function of its arguments; no I/O.
    """
    if getattr(cfg, "browser", None) is None:
        return False
    if cfg.browser.enabled:
        return False
    return bool(profile.requires_browser(url))


def plan(
    conn: sqlite3.Connection,
    plan_path: Path,
    *,
    now: datetime,
    extractor_version: str,
    normalizer_version: str,
    profile: "SiteProfile",
    cfg: "IndexConfig",
    sitemap_lastmod_by_url: Optional[dict[str, str]] = None,
    only_urls: Optional[set[str]] = None,
) -> dict[str, int]:
    """Walk every row in the manifest, decide, and emit plan.jsonl.

    Returns a counter dict, e.g. {'FETCH': 50, 'FETCH_CONDITIONAL': 200, 'SKIP': 4500}.
    Idempotent: same manifest + now + sitemap input -> same plan.jsonl byte-for-byte
    (within JSON key ordering; we sort keys for that).

    ``profile`` and ``cfg`` are required so the per-URL pre-decide route check
    (browser-disabled short-circuit) can run without thread-local state.

    ``only_urls`` scopes the plan to a specific set of URLs. Manifest rows
    outside the set are SKIPPED entirely — not emitted to plan.jsonl, not
    counted. Use for targeted backfill (``sift run --only-urls ...``)
    where the operator wants to fetch just a handful of newly-seeded URLs
    without re-evaluating the rest of the manifest. The fetch phase then
    sees a tiny plan and the run completes in seconds, not minutes.
    """
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {d.value: 0 for d in Decision}
    counts.update({f"tier:{t.value}": 0 for t in Tier})
    if only_urls is not None:
        counts["skipped_not_in_only_urls"] = 0

    sm_map = sitemap_lastmod_by_url or {}

    with plan_path.open("w") as f:
        for row in iter_all(conn):
            if only_urls is not None and row.url not in only_urls:
                counts["skipped_not_in_only_urls"] += 1
                continue
            tier = Tier(row.tier)
            sm = sm_map.get(row.url, row.sitemap_lastmod_seen)
            if route_to_browser_disabled(row.url, profile, cfg):
                decision = Decision.SKIPPED_BROWSER_DISABLED
                reason = "[browser].enabled=false but profile.requires_browser(url)=true"
            else:
                decision, reason = decide(
                    row, tier, sm, now,
                    extractor_version=extractor_version,
                    normalizer_version=normalizer_version,
                )
                # §8.2: browser_version invalidation. A row whose previous fetch
                # used the browser path stores a non-NULL browser_version; if it
                # differs from the current BROWSER_VERSION (crawl4ai bump, init
                # script change, etc.) the rendered HTML may have shifted enough
                # that we should re-fetch. Mirrors the EXTRACTOR_VERSION /
                # NORMALIZER_VERSION rules already in decide(); promotes SKIP to
                # FETCH_CONDITIONAL but leaves explicit FETCH / TOMBSTONE_PURGE
                # alone (those carry their own stronger signal).
                if (
                    row.browser_version is not None
                    and row.browser_version != BROWSER_VERSION
                    and decision == Decision.SKIP
                ):
                    decision = Decision.FETCH_CONDITIONAL
                    reason = f"browser_version bump ({row.browser_version} -> {BROWSER_VERSION})"
            entry = PlanEntry(
                url=row.url,
                tier=row.tier,
                parent_guide=row.parent_guide,
                decision=decision.value,
                reason=reason,
                etag=row.http_etag,
                last_modified=row.http_last_modified,
                sitemap_lastmod=sm,
            )
            f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
            counts[decision.value] += 1
            counts[f"tier:{row.tier}"] += 1
    return counts


def load_plan(plan_path: Path) -> list[PlanEntry]:
    out: list[PlanEntry] = []
    with plan_path.open() as f:
        for line in f:
            obj = json.loads(line)
            out.append(PlanEntry(**obj))
    return out
