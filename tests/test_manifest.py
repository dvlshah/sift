"""Manifest schema + apply_fetch_result transitions."""

from pathlib import Path

import pytest

from sift.manifest import (
    apply_fetch_result,
    counts_by_state,
    counts_by_tier,
    get_row,
    init_schema,
    now_utc,
    open_db,
    transaction,
    upsert_seed,
)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "manifest.db")
    init_schema(c)
    yield c
    c.close()


class TestUpsertSeed:
    def test_insert_new(self, conn):
        with transaction(conn):
            outcome = upsert_seed(
                conn, "https://x/y", "LIVING", None, "v1", None, now_utc()
            )
        assert outcome == "inserted"
        row = get_row(conn, "https://x/y")
        assert row is not None
        assert row.state == "UNSEEN"

    def test_noop_when_unchanged(self, conn):
        with transaction(conn):
            upsert_seed(conn, "https://x/y", "LIVING", None, "v1", None, now_utc())
        with transaction(conn):
            outcome = upsert_seed(conn, "https://x/y", "LIVING", None, "v1", None, now_utc())
        assert outcome == "noop"

    def test_reclassify_on_version_bump(self, conn):
        with transaction(conn):
            upsert_seed(conn, "https://x/y", "LIVING", None, "v1", None, now_utc())
        with transaction(conn):
            outcome = upsert_seed(conn, "https://x/y", "FROZEN", None, "v2", None, now_utc())
        assert outcome == "reclassified"
        assert get_row(conn, "https://x/y").tier == "FROZEN"

    def test_sitemap_lastmod_advances(self, conn):
        with transaction(conn):
            upsert_seed(conn, "https://x/y", "LIVING", None, "v1",
                        "2026-01-01T00:00:00Z", now_utc())
        with transaction(conn):
            upsert_seed(conn, "https://x/y", "LIVING", None, "v1",
                        "2026-05-01T00:00:00Z", now_utc())
        assert get_row(conn, "https://x/y").sitemap_lastmod_seen == "2026-05-01T00:00:00Z"


class TestApplyFetchResult:
    def _seed(self, conn, url="https://x/y"):
        with transaction(conn):
            upsert_seed(conn, url, "LIVING", None, "v1", None, now_utc())

    def test_200_fresh_first_time(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag='W/"a"', http_last_modified=None,
                raw_hash="r1", content_hash="c1",
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        row = get_row(conn, "https://x/y")
        assert row.state == "FRESH"
        assert row.content_hash == "c1"
        assert row.unchanged_streak == 0  # first fetch has no prior to compare against
        assert row.last_changed_at is not None

    def test_200_unchanged_increments_streak(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="r1", content_hash="c1",
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        prev_last_changed = get_row(conn, "https://x/y").last_changed_at
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="r1", content_hash="c1",  # same hash
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        row = get_row(conn, "https://x/y")
        assert row.content_hash == "c1"
        assert row.unchanged_streak == 1  # incremented from 0
        assert row.last_changed_at == prev_last_changed  # NOT updated

    def test_200_content_change_resets_streak(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="r1", content_hash="c1",
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="r2", content_hash="c2",  # NEW
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        row = get_row(conn, "https://x/y")
        assert row.content_hash == "c2"
        assert row.unchanged_streak == 0

    def test_304_no_content_change(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="r1", content_hash="c1",
                crawler_version="v1", extractor_version="ext", normalizer_version="n1",
                error=None,
            )
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=304, http_etag=None, http_last_modified=None,
                raw_hash=None, content_hash=None,
                crawler_version="v1", extractor_version=None, normalizer_version=None,
                error=None,
            )
        row = get_row(conn, "https://x/y")
        assert row.state == "FRESH"
        assert row.content_hash == "c1"  # preserved
        assert row.unchanged_streak == 1  # 304 increments from 0

    def test_404_becomes_gone(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=404, http_etag=None, http_last_modified=None,
                raw_hash=None, content_hash=None,
                crawler_version="v1", extractor_version=None, normalizer_version=None,
                error=None,
            )
        assert get_row(conn, "https://x/y").state == "GONE"

    def test_error_becomes_failed_increments_count(self, conn):
        self._seed(conn)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/y", now=now_utc(),
                http_status=0, http_etag=None, http_last_modified=None,
                raw_hash=None, content_hash=None,
                crawler_version="v1", extractor_version=None, normalizer_version=None,
                error="ConnectionError",
            )
        row = get_row(conn, "https://x/y")
        assert row.state == "FAILED"
        assert row.fail_count == 1
        assert row.last_error == "ConnectionError"


class TestCounts:
    def test_by_tier_and_state(self, conn):
        with transaction(conn):
            upsert_seed(conn, "https://x/a", "LIVING", None, "v1", None, now_utc())
            upsert_seed(conn, "https://x/b", "FROZEN", "g", "v1", None, now_utc())
            upsert_seed(conn, "https://x/c", "LIVING", None, "v1", None, now_utc())
        assert counts_by_tier(conn) == {"LIVING": 2, "FROZEN": 1}
        assert counts_by_state(conn) == {"UNSEEN": 3}
