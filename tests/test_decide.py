"""State-machine truth table for decide()."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sift.classify import Tier
from sift.decide import (
    Decision,
    TIER_INTERVALS,
    decide,
    next_recrawl_interval,
    tombstone_ttl,
)
from sift.manifest import ManifestRow

NOW = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
EXTRACT_V = "trafilatura-2.0.0-cfg1"
NORM_V = "v1"


def make_row(**overrides) -> ManifestRow:
    base = dict(
        url="https://www.ato.gov.au/x",
        tier="LIVING",
        parent_guide=None,
        state="FRESH",
        sitemap_lastmod_seen=None,
        first_seen_at="2026-01-01T00:00:00Z",
        last_fetched_at="2026-05-01T00:00:00Z",
        last_attempted_at="2026-05-01T00:00:00Z",
        http_status=200,
        http_etag='W/"abc"',
        http_last_modified="Wed, 01 May 2026 00:00:00 GMT",
        raw_hash="rawhash",
        content_hash="contenthash",
        last_changed_at="2026-04-01T00:00:00Z",
        unchanged_streak=2,
        crawler_version="v1.0.0",
        extractor_version=EXTRACT_V,
        normalizer_version=NORM_V,
        classifier_version="v1",
        browser_version=None,
        fail_count=0,
        last_error=None,
    )
    base.update(overrides)
    return ManifestRow(**base)


def call(row, tier=Tier.LIVING, sitemap=None, now=NOW):
    return decide(row, tier, sitemap, now,
                  extractor_version=EXTRACT_V, normalizer_version=NORM_V)


class TestUnseen:
    def test_no_row_means_fetch(self):
        d, _ = call(None)
        assert d == Decision.FETCH


class TestFrozen:
    def test_frozen_with_current_versions_skip(self):
        row = make_row(tier="FROZEN", extractor_version=EXTRACT_V, normalizer_version=NORM_V)
        d, reason = call(row, tier=Tier.FROZEN)
        assert d == Decision.SKIP
        assert "frozen" in reason

    def test_frozen_extractor_bump_still_skips_fetch(self):
        row = make_row(tier="FROZEN", extractor_version="old", normalizer_version=NORM_V)
        d, reason = call(row, tier=Tier.FROZEN)
        # Re-derive locally; do not refetch.
        assert d == Decision.SKIP

    def test_frozen_with_no_content_yet_fetches(self):
        row = make_row(tier="FROZEN", state="UNSEEN", content_hash=None,
                       last_fetched_at=None, last_attempted_at=None)
        d, _ = call(row, tier=Tier.FROZEN)
        # state machine treats this as: never-fetched -> FETCH
        assert d == Decision.FETCH


class TestGone:
    def test_within_ttl_skip(self):
        row = make_row(state="GONE",
                       last_attempted_at=(NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.SKIP

    def test_past_ttl_purge(self):
        # LIVING tombstone ttl = 90 days
        row = make_row(state="GONE",
                       last_attempted_at=(NOW - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.TOMBSTONE_PURGE

    def test_resurrected_in_sitemap(self):
        last_attempt = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_sitemap = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = make_row(state="GONE", last_attempted_at=last_attempt)
        d, _ = call(row, tier=Tier.LIVING, sitemap=new_sitemap)
        assert d == Decision.FETCH


class TestFailed:
    def test_within_budget_retries(self):
        row = make_row(state="FAILED", fail_count=3)
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.FETCH

    def test_exhausted_budget_skips(self):
        row = make_row(state="FAILED", fail_count=100)
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.SKIP


class TestSitemapLastmod:
    def test_sitemap_after_last_fetch_triggers_conditional(self):
        """Page modified after we last got it -> conditional refetch."""
        row = make_row(last_fetched_at="2026-05-20T00:00:00Z")
        d, reason = call(row, tier=Tier.LIVING,
                         sitemap="2026-05-23T00:00:00Z")
        assert d == Decision.FETCH_CONDITIONAL
        assert "sitemap-lastmod-after-last-fetch" in reason

    def test_sitemap_older_than_last_fetch_no_signal(self):
        """Sitemap says page was last modified BEFORE we fetched it -> our
        content is current, fall through to interval check (which says SKIP
        because we just fetched it)."""
        row = make_row(last_fetched_at="2026-05-23T23:00:00Z")
        d, _ = call(row, tier=Tier.LIVING,
                    sitemap="2026-05-01T00:00:00Z")
        assert d == Decision.SKIP

    def test_first_time_sitemap_known_does_not_trigger_refetch(self):
        """The original bug: a fresh sitemap seed shouldn't refetch URLs we
        already have fresh content for, even if sitemap_lastmod_seen was NULL."""
        # Last fetched 1 hour ago; sitemap reports lastmod from 6 months ago
        row = make_row(
            sitemap_lastmod_seen=None,  # never seen sitemap signal before
            last_fetched_at="2026-05-23T23:00:00Z",
        )
        d, _ = call(row, tier=Tier.LIVING,
                    sitemap="2025-11-15T00:00:00Z")  # months before last fetch
        assert d == Decision.SKIP  # would have been FETCH_CONDITIONAL under old logic


class TestIntervals:
    def test_within_interval_skip(self):
        row = make_row(last_fetched_at="2026-05-23T00:00:00Z", unchanged_streak=0)
        d, reason = call(row, tier=Tier.LIVING)
        assert d == Decision.SKIP
        assert "within-interval" in reason

    def test_past_interval_conditional(self):
        # LIVING floor = 7d; last_fetched 30d ago
        row = make_row(last_fetched_at="2026-04-15T00:00:00Z", unchanged_streak=0)
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.FETCH_CONDITIONAL

    def test_never_fetched_means_fetch(self):
        row = make_row(last_fetched_at=None, state="UNSEEN")
        d, _ = call(row, tier=Tier.LIVING)
        assert d == Decision.FETCH


class TestAdaptiveBackoff:
    def test_streak_zero(self):
        row = make_row(unchanged_streak=0)
        assert next_recrawl_interval(row, Tier.LIVING) == TIER_INTERVALS[Tier.LIVING][0]

    def test_streak_grows_to_ceiling(self):
        row = make_row(unchanged_streak=20)
        i = next_recrawl_interval(row, Tier.LIVING)
        assert i == TIER_INTERVALS[Tier.LIVING][1]  # ceiling

    def test_pure_deterministic(self):
        row = make_row(unchanged_streak=5)
        a = next_recrawl_interval(row, Tier.LIVING)
        b = next_recrawl_interval(row, Tier.LIVING)
        assert a == b
