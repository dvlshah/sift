"""Tombstone purge tests.

Verifies that:
  * purge_tombstones() deletes only TOMBSTONE_PURGE-decisioned URLs
  * Other URLs (FETCH / FETCH_CONDITIONAL / SKIP) are untouched
  * Returns accurate counts (candidates vs actually purged)
  * Handles empty plans safely
  * Idempotent on re-run (already-purged URLs report as 0 purged)
"""

from __future__ import annotations

import pytest

from sift.manifest import init_schema, open_db, transaction, upsert_seed, now_utc
from sift.plan import PlanEntry
from sift.purge import purge_tombstones, PURGE_DECISION


def _seed_url(conn, url: str, *, tier: str = "LIVING"):
    """Insert a manifest row via the standard seed path."""
    with transaction(conn):
        upsert_seed(
            conn, url=url, tier=tier,
            parent_guide_=None, classifier_version="v1",
            sitemap_lastmod=None, now=now_utc(),
        )


def _plan_entry(url: str, decision: str = PURGE_DECISION) -> PlanEntry:
    return PlanEntry(
        url=url, tier="LIVING", parent_guide=None,
        decision=decision, reason="test",
        etag=None, last_modified=None, sitemap_lastmod=None,
    )


class TestPurgeTombstones:

    def test_empty_plan_is_noop(self, tmp_path):
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        with transaction(conn):
            result = purge_tombstones(conn, [])
        assert result == {"purged": 0, "candidates": 0}

    def test_deletes_only_tombstone_purge_decisions(self, tmp_path):
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        _seed_url(conn, "https://x/keep-fetch")
        _seed_url(conn, "https://x/keep-skip")
        _seed_url(conn, "https://x/purge-me")
        _seed_url(conn, "https://x/keep-conditional")

        plan = [
            _plan_entry("https://x/keep-fetch", decision="FETCH"),
            _plan_entry("https://x/keep-skip", decision="SKIP"),
            _plan_entry("https://x/purge-me", decision=PURGE_DECISION),
            _plan_entry("https://x/keep-conditional", decision="FETCH_CONDITIONAL"),
        ]
        with transaction(conn):
            result = purge_tombstones(conn, plan)

        assert result == {"purged": 1, "candidates": 1}

        # Verify the right rows survived
        rows = {r[0] for r in conn.execute("SELECT url FROM manifest").fetchall()}
        assert rows == {
            "https://x/keep-fetch",
            "https://x/keep-skip",
            "https://x/keep-conditional",
        }
        assert "https://x/purge-me" not in rows

    def test_multiple_purges_at_once(self, tmp_path):
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        urls = [f"https://x/purge-{i}" for i in range(5)]
        for u in urls:
            _seed_url(conn, u)

        plan = [_plan_entry(u, decision=PURGE_DECISION) for u in urls]
        with transaction(conn):
            result = purge_tombstones(conn, plan)
        assert result == {"purged": 5, "candidates": 5}
        assert conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] == 0

    def test_purge_candidate_not_in_manifest_reports_zero_purged(self, tmp_path):
        """Plan can carry TOMBSTONE_PURGE for a URL that was already deleted
        (e.g. previous run purged it but plan was regenerated). Should
        report purged=0 since nothing was actually deleted."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        # No URLs seeded
        plan = [_plan_entry("https://x/ghost", decision=PURGE_DECISION)]
        with transaction(conn):
            result = purge_tombstones(conn, plan)
        assert result == {"purged": 0, "candidates": 1}

    def test_idempotent_on_rerun(self, tmp_path):
        """Re-running purge with the same plan after a successful purge
        should report 0 purged, 1 candidate (still in the plan but no row
        to delete)."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        _seed_url(conn, "https://x/purge-me")
        plan = [_plan_entry("https://x/purge-me", decision=PURGE_DECISION)]

        with transaction(conn):
            r1 = purge_tombstones(conn, plan)
        assert r1 == {"purged": 1, "candidates": 1}

        with transaction(conn):
            r2 = purge_tombstones(conn, plan)
        assert r2 == {"purged": 0, "candidates": 1}

    def test_purge_with_generator_input(self, tmp_path):
        """purge_tombstones accepts any Iterable[PlanEntry], not just lists."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        _seed_url(conn, "https://x/purge-1")
        _seed_url(conn, "https://x/purge-2")
        _seed_url(conn, "https://x/keep")

        def plan_gen():
            yield _plan_entry("https://x/purge-1", decision=PURGE_DECISION)
            yield _plan_entry("https://x/keep", decision="SKIP")
            yield _plan_entry("https://x/purge-2", decision=PURGE_DECISION)

        with transaction(conn):
            result = purge_tombstones(conn, plan_gen())
        assert result == {"purged": 2, "candidates": 2}
        rows = {r[0] for r in conn.execute("SELECT url FROM manifest").fetchall()}
        assert rows == {"https://x/keep"}


class TestPurgeIntegration:
    """Integration: drive the standard pipeline + verify purge fires
    correctly when called between commit and publish."""

    def test_purge_in_pipeline_doesnt_affect_unrelated_decisions(self, tmp_path):
        """After commit+purge, only TOMBSTONE_PURGE rows are gone."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        # 3 seeded URLs: one will be purged, two kept
        for u in ("https://x/a", "https://x/b", "https://x/c"):
            _seed_url(conn, u)

        # Synthetic plan as it would come out of decide
        plan = [
            _plan_entry("https://x/a", decision="FETCH"),
            _plan_entry("https://x/b", decision=PURGE_DECISION),
            _plan_entry("https://x/c", decision="SKIP"),
        ]

        # Simulate commit running first (just verify rows still there)
        assert conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] == 3

        # Now purge
        with transaction(conn):
            result = purge_tombstones(conn, plan)

        assert result["purged"] == 1
        # The publish coverage gate would now see 2 rows instead of 3
        remaining = {r[0] for r in conn.execute("SELECT url FROM manifest").fetchall()}
        assert remaining == {"https://x/a", "https://x/c"}
