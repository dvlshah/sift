"""Stage 2 evals: plan.

Implemented (B3):
  * ``plan_decision_correctness`` — synthetic in-memory manifest with rows in
    known states + ages, run plan, assert decisions match expectations.
    This is unit-test-shaped, but as a bench eval it produces a single
    correctness number ("100% of synthetic cases produce the expected
    decision") that the operator can graph over time alongside the
    quality numbers from extract.

Deferred (Phase B5):
  * ``plan_tier_classification`` — URL → tier per use case.
  * ``plan_browser_version_invalidation`` — bump BROWSER_VERSION → browser
    rows promote to FETCH_CONDITIONAL.
  * ``plan_firecrawl_version_invalidation`` — same for Firecrawl rows.
"""
from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class PlanDecisionCorrectnessResult:
    name: str = "plan_decision_correctness"
    pass_threshold: float = 1.0
    n_cases: int = 0
    n_correct: int = 0
    rate: float = 0.0
    passed: bool = False
    failures: list = None


def eval_plan_decision_correctness() -> PlanDecisionCorrectnessResult:
    """Synthetic eval: build a tiny manifest with rows in known states,
    drive ``plan_phase``, assert per-row decisions.

    Cases — the planner's actual rules (per sift/decide.py:115-123):

      1. UNSEEN URL                       → FETCH (no row exists yet)
      2. FRESH within refresh_floor       → SKIP (interval not elapsed)
      3. FRESH past refresh_ceiling       → FETCH_CONDITIONAL (interval
         elapsed — the etag check at fetch-time decides 200 vs 304,
         which is why this returns CONDITIONAL even with no stored etag)
      4. FRESH between floor and ceiling
         with an http_etag stored         → FETCH_CONDITIONAL
      5. GONE within tombstone TTL        → SKIP (still tombstoned)
      6. GONE past tombstone TTL          → TOMBSTONE_PURGE (the planner
         uses last_attempted_at — NOT last_fetched_at — for the GONE age
         check, so the fixture must set BOTH to express "GONE since X")
    """
    from sift.manifest import init_schema, open_db, transaction, upsert_seed
    from sift.plan import plan as plan_phase, load_plan
    from sift.config import IndexConfig, TierConfig
    from sift.classify import CLASSIFIER_VERSION
    from sift.extract import EXTRACTOR_VERSION
    from sift.normalize import normalizer_version
    from sift import decide as decide_mod
    from sift import paths

    # Set up an in-memory-ish manifest in a temp dir
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = paths.manifest_path(root)
        conn = open_db(db_path)
        init_schema(conn)

        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        now_iso = now.isoformat()

        # Tier config we'll use for the planner — short floor/ceiling/TTL so
        # we can express "past ceiling" / "past TTL" with realistic age deltas.
        living = TierConfig(
            floor_days=7, ceiling_days=30,
            tombstone_ttl_days=14, max_failures=5,
        )
        cfg = IndexConfig(tiers={"LIVING": living})
        # CRITICAL: decide.py reads tier intervals + tombstone TTL from
        # module-globals populated by apply_config. Without this call the
        # planner uses default TTLs (typically 90+ days), and our 20-day-old
        # GONE fixture lands within those, returning SKIP not TOMBSTONE_PURGE.
        # This was the load-bearing missing call in the bench's first run.
        decide_mod.apply_config(cfg)

        # The fixture cases. Each is (url, setup_state, expected_decision)
        # `setup_state` is a 4-tuple of (state, last_fetched_at, etag, fail_count)
        cases = [
            # 1. UNSEEN — no fetch ever happened
            ("https://x.test/unseen",
             ("UNSEEN", None, None, 0),
             "FETCH"),
            # 2. FRESH within refresh_floor (fetched 3 days ago, floor=7) → SKIP
            ("https://x.test/fresh-within-floor",
             ("FRESH", (now - timedelta(days=3)).isoformat(), None, 0),
             "SKIP"),
            # 3. FRESH past ceiling (fetched 40 days ago, ceiling=30) →
            #    FETCH_CONDITIONAL. The planner always emits CONDITIONAL
            #    past interval; the fetch-time etag check determines whether
            #    we send If-None-Match. With no stored etag, the request
            #    effectively degrades to unconditional anyway.
            ("https://x.test/fresh-past-ceiling",
             ("FRESH", (now - timedelta(days=40)).isoformat(), None, 0),
             "FETCH_CONDITIONAL"),
            # 4. FRESH between floor and ceiling (15d ago) with etag → FETCH_CONDITIONAL
            ("https://x.test/fresh-conditional",
             ("FRESH", (now - timedelta(days=15)).isoformat(),
              'W/"abc-etag"', 0),
             "FETCH_CONDITIONAL"),
            # 5. GONE within tombstone_ttl (10d ago, ttl=14) → SKIP
            ("https://x.test/gone-within-ttl",
             ("GONE", (now - timedelta(days=10)).isoformat(), None, 0),
             "SKIP"),
            # 6. GONE past tombstone_ttl (20d ago, ttl=14) → TOMBSTONE_PURGE
            ("https://x.test/gone-past-ttl",
             ("GONE", (now - timedelta(days=20)).isoformat(), None, 0),
             "TOMBSTONE_PURGE"),
        ]

        # Set up the manifest rows. upsert_seed creates them as UNSEEN; for
        # cases 2-6 we then UPDATE the row to express the target state.
        with transaction(conn):
            for url, _setup, _expected in cases:
                upsert_seed(
                    conn, url=url, tier="LIVING", parent_guide_=None,
                    classifier_version=CLASSIFIER_VERSION,
                    sitemap_lastmod=None, now=now_iso,
                )

        for url, (state, last_fetched, etag, fc), _exp in cases:
            if state == "UNSEEN":
                continue
            # NB: the planner reads last_attempted_at (not last_fetched_at) for
            # the GONE-tombstone age check. Mirror the same timestamp into
            # both so the fixture expresses "this row has been in `state` since
            # `last_fetched`" cleanly.
            conn.execute(
                "UPDATE manifest SET state=?, last_fetched_at=?, "
                "last_attempted_at=?, http_etag=?, fail_count=? WHERE url=?",
                (state, last_fetched, last_fetched, etag, fc, url),
            )
        conn.commit()

        # Drive the planner against an open profile (GenericProfile);
        # tier-level decisions don't depend on profile-specific behavior.
        from sift.sites.generic import GenericProfile
        plan_path = paths.plan_path(root, "synthetic-run")
        plan_phase(
            conn, plan_path, now=now,
            extractor_version=EXTRACTOR_VERSION,
            normalizer_version=normalizer_version(),
            profile=GenericProfile(),
            cfg=cfg,
        )

        # Read decisions back from plan.jsonl
        entries = {e.url: e.decision for e in load_plan(plan_path)}

        failures: list[dict] = []
        n_correct = 0
        for url, _setup, expected in cases:
            actual = entries.get(url, "<MISSING>")
            if actual == expected:
                n_correct += 1
            else:
                failures.append({"url": url, "expected": expected,
                                 "actual": actual})

    rate = n_correct / len(cases) if cases else 0.0
    return PlanDecisionCorrectnessResult(
        n_cases=len(cases),
        n_correct=n_correct,
        rate=round(rate, 4),
        passed=(rate == 1.0),
        failures=failures,
    )


def run_plan_evals(*args: Any, **kwargs: Any) -> dict:
    """Synthetic — does not take a root or fixture (it builds its own manifest).
    Run once per bench, not per fixture."""
    return {"decision_correctness":
            asdict(eval_plan_decision_correctness())}
