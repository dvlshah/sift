"""Schema-sanity gate: hard failures vs tolerance-based soft failures, tier-aware."""

from pathlib import Path

import pytest

from sift import paths, publish


def _write_md(root: Path, run_id: str, name: str, tier: str, body: str) -> Path:
    md = paths.run_dir(root, run_id) / "md" / name
    md.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"url: https://www.ato.gov.au/{name.replace('.md','')}\n"
        "title: Test page\n"
        f"tier: {tier}\n"
        "content_hash: sha256:abc\n"
        "---\n"
    )
    md.write_text(fm + body)
    return md


class TestCoverageGate:
    def test_failure_message_points_at_planned(self, tmp_path):
        """terminal << expected (a capped/narrow crawl) fails G3 with an
        actionable hint at --coverage-base planned, not just a bare number."""
        from sift.manifest import (init_schema, now_utc, open_db, transaction,
                                    upsert_seed)
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        now = now_utc()
        with transaction(conn):
            for i in range(3):
                upsert_seed(conn, f"https://x/{i}", "LIVING", None, "v1", None, now)
            conn.execute("UPDATE manifest SET state='FRESH' "
                         "WHERE url IN ('https://x/0', 'https://x/1')")
        ok, detail = publish.gate_coverage(conn, expected_urls=10_000)
        assert not ok
        assert "--coverage-base planned" in detail

    def test_passing_gate_message_stays_terse(self, tmp_path):
        from sift.manifest import (init_schema, now_utc, open_db, transaction,
                                    upsert_seed)
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        now = now_utc()
        with transaction(conn):
            upsert_seed(conn, "https://x/0", "LIVING", None, "v1", None, now)
            conn.execute("UPDATE manifest SET state='FRESH' WHERE url='https://x/0'")
        ok, detail = publish.gate_coverage(conn, expected_urls=1)
        assert ok
        assert "planned" not in detail


class TestHardFailures:
    def test_missing_frontmatter_fails(self, tmp_path):
        run_id = "r1"
        md = paths.run_dir(tmp_path, run_id) / "md" / "broken.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("just a body, no frontmatter at all")
        ok, detail = publish.gate_schema_sanity(tmp_path, run_id)
        assert not ok
        assert "missing frontmatter" in detail

    def test_missing_url_in_frontmatter_fails(self, tmp_path):
        run_id = "r1"
        md = paths.run_dir(tmp_path, run_id) / "md" / "broken.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("---\ntier: LIVING\ncontent_hash: sha256:x\n---\nbody text long enough" * 5)
        ok, detail = publish.gate_schema_sanity(tmp_path, run_id)
        assert not ok
        assert "missing url" in detail


class TestFrozenStubsAllowed:
    def test_single_short_frozen_passes(self, tmp_path):
        """The original bug: a single 44-char FROZEN appendix stub should not
        fail the gate for an otherwise healthy 4,900-page corpus."""
        run_id = "r1"
        # 1 short FROZEN stub + 49 healthy non-FROZEN pages
        _write_md(tmp_path, run_id, "appendix.md", "FROZEN", "# Appendix")
        for i in range(49):
            _write_md(
                tmp_path, run_id, f"page-{i:02d}.md", "LIVING",
                "A" * 500,  # plenty of body
            )
        ok, detail = publish.gate_schema_sanity(tmp_path, run_id)
        assert ok, f"gate should pass: {detail}"
        assert "FROZEN" in detail  # detail mentions the FROZEN stub count

    def test_many_short_frozen_passes(self, tmp_path):
        run_id = "r1"
        for i in range(40):
            _write_md(tmp_path, run_id, f"frozen-{i:02d}.md", "FROZEN", "# stub")
        for i in range(10):
            _write_md(tmp_path, run_id, f"live-{i:02d}.md", "LIVING", "B" * 500)
        ok, _ = publish.gate_schema_sanity(tmp_path, run_id)
        assert ok


class TestNonFrozenTolerance:
    def test_one_short_living_within_tolerance(self, tmp_path):
        """1 short body in 50-sample = 2% < 5% tolerance → still passes."""
        run_id = "r1"
        _write_md(tmp_path, run_id, "stub.md", "LIVING", "tiny")
        for i in range(49):
            _write_md(tmp_path, run_id, f"page-{i:02d}.md", "LIVING", "C" * 500)
        ok, _ = publish.gate_schema_sanity(tmp_path, run_id)
        assert ok

    def test_many_short_living_fails(self, tmp_path):
        """6 short bodies in 50 LIVING sample = 12% > 5% → fails."""
        run_id = "r1"
        for i in range(6):
            _write_md(tmp_path, run_id, f"stub-{i}.md", "LIVING", "tiny")
        for i in range(44):
            _write_md(tmp_path, run_id, f"page-{i:02d}.md", "LIVING", "D" * 500)
        ok, detail = publish.gate_schema_sanity(tmp_path, run_id)
        assert not ok
        assert "tolerance" in detail
        # Detail should name example offenders
        assert "stub-" in detail


class TestEmpty:
    def test_no_md_dir_passes(self, tmp_path):
        ok, _ = publish.gate_schema_sanity(tmp_path, "missing-run")
        assert ok

    def test_empty_md_dir_passes(self, tmp_path):
        paths.run_dir(tmp_path, "r1").joinpath("md").mkdir(parents=True)
        ok, _ = publish.gate_schema_sanity(tmp_path, "r1")
        assert ok


class TestSnapshotCoverageFractions:
    """The published snapshot reports indexed (content-bearing) coverage
    separately from lifecycle-resolved coverage, so a stale seed full of
    404 -> GONE rows cannot inflate the badge number."""

    def test_gone_inflates_resolved_but_not_indexed(self, tmp_path):
        import json

        from sift.manifest import (init_schema, now_utc, open_db, transaction,
                                   upsert_seed)
        run_id = "r1"
        paths.run_dir(tmp_path, run_id).mkdir(parents=True, exist_ok=True)
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        now = now_utc()
        with transaction(conn):
            for i in range(10):
                upsert_seed(conn, f"https://x/{i}", "LIVING", None, "v1", None, now)
            # 5 genuinely indexed (FRESH + content_hash), 3 dead (GONE, no
            # content), 2 never reached (UNSEEN).
            conn.execute(
                "UPDATE manifest SET state='FRESH', content_hash='sha256:abc' "
                "WHERE url IN ('https://x/0','https://x/1','https://x/2',"
                "'https://x/3','https://x/4')"
            )
            conn.execute(
                "UPDATE manifest SET state='GONE' "
                "WHERE url IN ('https://x/5','https://x/6','https://x/7')"
            )
        publish.write_snapshot(
            tmp_path, run_id, conn=conn, started_at=now, completed_at=now,
            expected_urls=10, gate_results=[], status="published",
        )
        snap = json.loads(paths.snapshot_path(tmp_path, run_id).read_text())
        cov = snap["coverage"]
        assert cov["indexed_count"] == 5
        assert cov["resolved_count"] == 8          # FRESH(5) + GONE(3)
        assert cov["indexed_fraction"] == 0.5
        assert cov["resolved_fraction"] == 0.8
        # The whole point: dead links inflate "resolved" but never "indexed".
        assert cov["indexed_fraction"] < cov["resolved_fraction"]

    def test_zero_expected_is_none_not_zerodiv(self, tmp_path):
        import json

        from sift.manifest import init_schema, now_utc, open_db
        run_id = "r1"
        paths.run_dir(tmp_path, run_id).mkdir(parents=True, exist_ok=True)
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        now = now_utc()
        publish.write_snapshot(
            tmp_path, run_id, conn=conn, started_at=now, completed_at=now,
            expected_urls=0, gate_results=[], status="published",
        )
        snap = json.loads(paths.snapshot_path(tmp_path, run_id).read_text())
        assert snap["coverage"]["indexed_fraction"] is None
        assert snap["coverage"]["resolved_fraction"] is None

    def test_frozen_with_hash_counts_in_both(self, tmp_path):
        # FROZEN-with-hash counts in BOTH indexed and resolved; FROZEN-without-
        # hash counts in resolved only — locks the numerator definition.
        import json

        from sift.manifest import (init_schema, now_utc, open_db, transaction,
                                   upsert_seed)
        run_id = "r1"
        paths.run_dir(tmp_path, run_id).mkdir(parents=True, exist_ok=True)
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        now = now_utc()
        with transaction(conn):
            for i in range(10):
                upsert_seed(conn, f"https://x/{i}", "LIVING", None, "v1", None, now)
            conn.execute(
                "UPDATE manifest SET state='FRESH', content_hash='sha256:abc' "
                "WHERE url IN ('https://x/0','https://x/1','https://x/2',"
                "'https://x/3','https://x/4')"
            )
            conn.execute(
                "UPDATE manifest SET state='GONE' "
                "WHERE url IN ('https://x/5','https://x/6','https://x/7')"
            )
            conn.execute("UPDATE manifest SET state='FROZEN', "
                         "content_hash='sha256:def' WHERE url='https://x/8'")
            conn.execute("UPDATE manifest SET state='FROZEN', "
                         "content_hash=NULL WHERE url='https://x/9'")
        publish.write_snapshot(
            tmp_path, run_id, conn=conn, started_at=now, completed_at=now,
            expected_urls=10, gate_results=[], status="published",
        )
        cov = json.loads(
            paths.snapshot_path(tmp_path, run_id).read_text())["coverage"]
        assert cov["indexed_count"] == 6     # 5 FRESH + 1 FROZEN-with-hash
        assert cov["resolved_count"] == 10   # FRESH5 + GONE3 + FROZEN2
        assert cov["denominator_basis"] == "manifest_total"
