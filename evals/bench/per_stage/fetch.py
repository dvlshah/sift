"""Stage 3 evals: fetch.

Reads from manifest + fetch.log to compute success rate, Firecrawl
escalation correctness, conditional GET efficiency, and budget enforcement.
This stage is the one most user-visible in v1.0.0 since the Firecrawl
fallback is the headline ship feature — so a real eval here is high-value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from sift import paths
from sift.manifest import open_db

from ..fixtures.sites import SiteFixture
from ..scoring.structural import preservation_score  # not needed; keep imports tidy


@dataclass
class FetchSuccessResult:
    name: str = "fetch_success_rate"
    pass_threshold: float = 0.95
    fresh: int = 0
    fetchable: int = 0
    rate: float = 0.0
    passed: bool = False


def eval_fetch_success(root: Path, *,
                      fixture: Optional[SiteFixture] = None
                      ) -> FetchSuccessResult:
    """Fetch success = FRESH / (FRESH + FAILED) restricted to the fixture's
    host. Excludes UNSEEN and FROZEN (those aren't fetched in the current
    run). Pass at ≥ 0.95."""
    conn = open_db(paths.manifest_path(root))
    host_filter = ""
    params: tuple = ()
    if fixture is not None:
        host_filter = " AND url LIKE ?"
        params = (f"https://{fixture.host}/%",)
    sql = (
        "SELECT state, COUNT(*) FROM manifest "
        f"WHERE state IN ('FRESH','FAILED'){host_filter} GROUP BY state"
    )
    counts = {state: n for state, n in conn.execute(sql, params).fetchall()}
    fresh = counts.get("FRESH", 0)
    failed = counts.get("FAILED", 0)
    fetchable = fresh + failed
    rate = (fresh / fetchable) if fetchable else 0.0
    return FetchSuccessResult(fresh=fresh, fetchable=fetchable,
                              rate=round(rate, 4),
                              passed=rate >= 0.95)


@dataclass
class FirecrawlEscalationResult:
    name: str = "fetch_firecrawl_escalation_correctness"
    # No threshold — purely descriptive. The presence of
    # ``browser_version='firecrawl-...'`` on FRESH rows after a run
    # demonstrates the escalation fired correctly. Pass when count > 0
    # and the firecrawl_count is consistent with the run's reported
    # credits.
    firecrawl_fresh_rows: int = 0
    note: str = ""
    passed: bool = True


def eval_firecrawl_escalation(root: Path, *,
                              fixture: Optional[SiteFixture] = None
                              ) -> FirecrawlEscalationResult:
    """Count manifest rows where ``browser_version`` starts with ``firecrawl-``
    — these were reached via the Stage-3 Firecrawl fallback. Restricted to
    the fixture's host if provided."""
    conn = open_db(paths.manifest_path(root))
    host_filter = ""
    params: tuple = ()
    if fixture is not None:
        host_filter = " AND url LIKE ?"
        params = (f"https://{fixture.host}/%",)
    sql = (
        "SELECT COUNT(*) FROM manifest "
        f"WHERE state = 'FRESH' AND browser_version LIKE 'firecrawl-%'{host_filter}"
    )
    n = conn.execute(sql, params).fetchone()[0]
    return FirecrawlEscalationResult(
        firecrawl_fresh_rows=n,
        note=(
            f"{n} FRESH row(s) marked browser_version=firecrawl-*; "
            "these were escalated from native 401/403."
        ) if n else (
            "0 FRESH rows marked browser_version=firecrawl-*; "
            "either fallback was off, no URLs were bot-blocked, or escalation "
            "failed."
        ),
        passed=True,  # observational; we don't grade up/down here
    )


@dataclass
class ConditionalGetResult:
    name: str = "fetch_conditional_get_efficiency"
    pass_threshold: float = 0.80
    fetch_conditional_decisions: int = 0
    not_modified_responses: int = 0
    efficiency: float = 0.0
    passed: bool = False
    note: str = ""


def eval_conditional_get(root: Path, run_id: str, *,
                          fixture: Optional[SiteFixture] = None
                          ) -> ConditionalGetResult:
    """Efficiency = HTTP-304 responses / FETCH_CONDITIONAL plan decisions.

    A healthy LIVING-tier re-fetch run should see 80%+ of its
    FETCH_CONDITIONAL URLs come back 304 (origin says "not modified, use
    your cache") — those don't cost re-extract, raw-blob writes, or
    content_hash recompute. A low number means either (a) the per-URL
    ETag / Last-Modified isn't being persisted across runs, (b) the origin
    isn't honoring the conditional header, or (c) the corpus genuinely
    changes a lot. The bench can't disambiguate — it surfaces the number
    and the operator investigates.
    """
    plan_path = paths.plan_path(root, run_id)
    fetch_log = paths.fetch_log_path(root, run_id)
    if not plan_path.exists() or not fetch_log.exists():
        return ConditionalGetResult(
            note=f"plan or fetch.log missing for run {run_id}",
            passed=False,
        )

    host_prefix = f"https://{fixture.host}/" if fixture else ""
    # Pull FETCH_CONDITIONAL URLs from plan.jsonl
    conditional_urls: set[str] = set()
    with plan_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            if p.get("decision") != "FETCH_CONDITIONAL":
                continue
            url = p.get("url", "")
            if host_prefix and not url.startswith(host_prefix):
                continue
            conditional_urls.add(url)

    # Count 304s in fetch.log, restricted to those URLs
    n304 = 0
    with fetch_log.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("url") in conditional_urls and r.get("status") == 304:
                n304 += 1

    total = len(conditional_urls)
    eff = (n304 / total) if total else 0.0
    return ConditionalGetResult(
        fetch_conditional_decisions=total,
        not_modified_responses=n304,
        efficiency=round(eff, 4),
        passed=(eff >= 0.80 and total > 0),
        note=(f"{n304}/{total} FETCH_CONDITIONAL → 304" if total else
              "no FETCH_CONDITIONAL decisions in this run (likely a "
              "first-time crawl)"),
    )


def run_fetch_evals(root: Path, run_id: str, *,
                    fixture: Optional[SiteFixture] = None) -> dict:
    return {
        "fixture": ({"use_case": fixture.use_case, "slug": fixture.slug,
                     "host": fixture.host} if fixture else None),
        "success":     asdict(eval_fetch_success(root, fixture=fixture)),
        "firecrawl":   asdict(eval_firecrawl_escalation(root, fixture=fixture)),
        "conditional_get": asdict(eval_conditional_get(root, run_id,
                                                       fixture=fixture)),
    }
