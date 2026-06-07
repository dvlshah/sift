"""re_extract_corpus — re-derive content_hashes from cached raw without losing history.

The critical test: stale manifest content_hash → re-extract produces a NEW
content_hash → commit emits a `changed` changelog entry with old + new hashes.
This is the audit-trail behavior the operator needs after a version bump.
"""

import json
from pathlib import Path

import pytest

from sift import CRAWLER_VERSION, paths
from sift.commit import commit
from sift.extract import re_extract_corpus
from sift.fetch import sha256_hex, write_raw_blob
from sift.manifest import (
    apply_fetch_result, get_row, init_schema, now_utc, open_db, transaction,
    upsert_seed,
)


# Minimal HTML with a couple of headings so trafilatura produces a non-trivial
# markdown body
_HTML = b"""<!DOCTYPE html>
<html><body>
<h1>Test page</h1>
<h2>Section A</h2>
<p>This is the body text used to verify that re-extraction works.
It needs enough content for trafilatura to keep, not strip as boilerplate.</p>
<h2>Section B</h2>
<p>More body text. The point is to produce a stable, hashable markdown body.</p>
</body></html>"""


def _setup_index_with_stale_hash(root: Path) -> tuple:
    """Seed manifest with one URL whose stored content_hash is wrong
    (simulates a stale-hash state after a version bump)."""
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    url = "https://www.example.com/test-page"
    now = now_utc()

    # Write raw blob
    raw_hash = sha256_hex(_HTML)
    write_raw_blob(root, raw_hash, _HTML)

    # Seed with a deliberately STALE content_hash (simulating an old
    # cfg2 extractor's output that no longer matches what cfg3 would produce)
    stale_hash = "0" * 64  # deliberately wrong
    with transaction(conn):
        upsert_seed(conn, url, "LIVING", None, "v1", None, now)
        apply_fetch_result(
            conn, url=url, now=now,
            http_status=200, http_etag=None, http_last_modified=None,
            raw_hash=raw_hash, content_hash=stale_hash,
            crawler_version=CRAWLER_VERSION,
            extractor_version="trafilatura-2.0.0-cfg2-OLD",  # old version
            normalizer_version="v0-OLD",
            error=None,
        )
    return conn, url, raw_hash, stale_hash


class TestReExtractProducesChangedEntries:
    def test_stale_hash_becomes_changed_entry(self, tmp_path):
        """The signal the operator wants: when re-extract finds a stale hash,
        commit emits a `change_type=changed` entry with both old and new hashes."""
        root = tmp_path
        conn, url, raw_hash, stale_hash = _setup_index_with_stale_hash(root)

        # Re-extract
        run_id = "test-re-extract-1"
        fl, el, n = re_extract_corpus(
            root, run_id=run_id, conn=conn,
            crawler_version=CRAWLER_VERSION,
        )
        assert n == 1, "should have re-extracted the one stale row"

        # Commit — should produce a `changed` entry
        counts = commit(conn, fl, el, root=root, run_id=run_id)
        assert counts["changelog_changed"] == 1, (
            f"expected one `changed` entry, got {counts}"
        )

        # Inspect the changelog
        cl = paths.changelog_path(root)
        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["url"] == url
        assert entry["change_type"] == "changed"
        assert entry["old_hash"] == stale_hash, "old_hash should be the stale value"
        assert entry["new_hash"] is not None
        assert entry["new_hash"] != stale_hash, "new_hash should differ from stale"
        # Chain integrity
        assert entry["prev_hash"].startswith("sha256:")
        assert entry["entry_hash"].startswith("sha256:")


class TestReExtractIdempotent:
    def test_second_run_no_changes(self, tmp_path):
        """After re-extract converges on the current versions, a second
        re-extract should produce ZERO changelog entries — the short-circuit
        in extract_one kicks in."""
        root = tmp_path
        conn, _, _, _ = _setup_index_with_stale_hash(root)

        # First run: stale → changed
        run_id_1 = "test-1"
        fl, el, _ = re_extract_corpus(
            root, run_id=run_id_1, conn=conn, crawler_version=CRAWLER_VERSION,
        )
        counts_1 = commit(conn, fl, el, root=root, run_id=run_id_1)
        assert counts_1["changelog_changed"] == 1

        # Second run: should be a no-op for changelog (same hashes now)
        run_id_2 = "test-2"
        fl2, el2, n = re_extract_corpus(
            root, run_id=run_id_2, conn=conn, crawler_version=CRAWLER_VERSION,
        )
        counts_2 = commit(conn, fl2, el2, root=root, run_id=run_id_2)
        assert counts_2["changelog_changed"] == 0
        assert counts_2["changelog_added"] == 0


class TestReExtractRespectsTierFilter:
    def test_tier_filter_excludes_others(self, tmp_path):
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        now = now_utc()
        # Two URLs in different tiers, both with raw blobs
        for url, tier in [
            ("https://x/living", "LIVING"),
            ("https://x/forms-and-instructions/2025", "CURRENT_FORMS"),
        ]:
            raw = sha256_hex(_HTML + url.encode())  # unique per URL
            write_raw_blob(root, raw, _HTML + url.encode())
            with transaction(conn):
                upsert_seed(conn, url, tier, None, "v1", None, now)
                apply_fetch_result(
                    conn, url=url, now=now, http_status=200,
                    http_etag=None, http_last_modified=None,
                    raw_hash=raw, content_hash="0" * 64,
                    crawler_version="v1", extractor_version="old",
                    normalizer_version="old", error=None,
                )

        fl, el, n = re_extract_corpus(
            root, run_id="t1", conn=conn,
            crawler_version=CRAWLER_VERSION,
            tiers=("LIVING",),
        )
        assert n == 1  # only the LIVING one


class TestReExtractPreservesChangelog:
    def test_chain_continues_across_re_extract(self, tmp_path):
        """A re-extract is a normal commit — entries chain forward from
        any existing changelog rather than starting a new chain."""
        root = tmp_path
        cl = paths.changelog_path(root)
        # Pre-seed an existing changelog entry (simulating prior history)
        cl.write_text('{"ts":"2026-01-01","url":"https://x/old","change_type":"added",'
                      '"old_hash":null,"new_hash":"sha256:abc","run_id":"prior-run",'
                      '"tier":"LIVING",'
                      '"prev_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000",'
                      '"entry_hash":"sha256:prior-entry-hash-here-stub-stub-stub-stub-stub-stub-stub-12"}\n')

        conn, url, _, _ = _setup_index_with_stale_hash(root)
        fl, el, _ = re_extract_corpus(
            root, run_id="re-run", conn=conn, crawler_version=CRAWLER_VERSION,
        )
        commit(conn, fl, el, root=root, run_id="re-run")

        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        assert len(entries) == 2, "prior + new entry"
        # New entry's prev_hash should reference the prior entry's entry_hash
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
