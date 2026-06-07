"""Commit phase now chain-hashes changelog entries; verify the on-disk format."""

import json

import pytest

from sift import paths
from sift.commit import commit
from sift.extract import ExtractResult
from sift.fetch import FetchResult
from sift.integrity import CHAIN_GENESIS, verify_chain
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)


def _seed_and_extract(root, run_id, url, content_hash):
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    now = now_utc()
    with transaction(conn):
        upsert_seed(conn, url, "LIVING", None, "v1", None, now)

    fetch_log = paths.fetch_log_path(root, run_id)
    fetch_log.parent.mkdir(parents=True, exist_ok=True)
    fr = FetchResult(
        url=url, decision="FETCH", status=200,
        etag=None, last_modified=None,
        raw_hash="r" * 64, raw_bytes=100,
        fetched_at=now, error=None,
    )
    fetch_log.write_text(fr.to_json_line())

    extract_log = paths.extract_log_path(root, run_id)
    er = ExtractResult(
        url=url, raw_hash="r" * 64, content_hash=content_hash,
        title="t", n_chars=100,
        extractor_version="ext", normalizer_version="v1",
        ok=True, reason="new-content",
    )
    extract_log.write_text(er.to_json_line())
    return conn, fetch_log, extract_log


class TestChainedChangelog:
    def test_first_run_starts_at_genesis(self, tmp_path):
        root = tmp_path
        conn, fl, el = _seed_and_extract(root, "r1", "https://x/a", "c1")
        commit(conn, fl, el, root=root, run_id="r1")
        cl = paths.changelog_path(root)
        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["prev_hash"] == CHAIN_GENESIS
        assert entries[0]["entry_hash"].startswith("sha256:")

    def test_chain_continues_across_commits(self, tmp_path):
        """Second commit picks up the prior entry's hash."""
        root = tmp_path
        conn, fl, el = _seed_and_extract(root, "r1", "https://x/a", "c1")
        commit(conn, fl, el, root=root, run_id="r1")
        # Second URL, same conn
        with transaction(conn):
            upsert_seed(conn, "https://x/b", "LIVING", None, "v1", None, now_utc())
        fl2 = paths.fetch_log_path(root, "r2")
        fl2.parent.mkdir(parents=True, exist_ok=True)
        fl2.write_text(FetchResult(
            url="https://x/b", decision="FETCH", status=200,
            etag=None, last_modified=None,
            raw_hash="s" * 64, raw_bytes=100,
            fetched_at=now_utc(), error=None,
        ).to_json_line())
        el2 = paths.extract_log_path(root, "r2")
        el2.write_text(ExtractResult(
            url="https://x/b", raw_hash="s" * 64, content_hash="c2",
            title="t", n_chars=100,
            extractor_version="ext", normalizer_version="v1",
            ok=True, reason="new-content",
        ).to_json_line())
        commit(conn, fl2, el2, root=root, run_id="r2")
        # Verify the chain
        cl = paths.changelog_path(root)
        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        assert len(entries) == 2
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
        ok, _, _ = verify_chain(entries)
        assert ok

    def test_full_chain_verifies(self, tmp_path):
        root = tmp_path
        conn, fl, el = _seed_and_extract(root, "r1", "https://x/a", "c1")
        commit(conn, fl, el, root=root, run_id="r1")
        cl = paths.changelog_path(root)
        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        ok, idx, reason = verify_chain(entries)
        assert ok, f"chain should verify: {reason}"

    def test_tampering_breaks_verification(self, tmp_path):
        root = tmp_path
        conn, fl, el = _seed_and_extract(root, "r1", "https://x/a", "c1")
        commit(conn, fl, el, root=root, run_id="r1")
        cl = paths.changelog_path(root)
        # Tamper with the file directly
        text = cl.read_text()
        tampered = text.replace('"added"', '"deleted"')
        cl.write_text(tampered)
        entries = [json.loads(line) for line in cl.read_text().splitlines() if line.strip()]
        ok, idx, reason = verify_chain(entries)
        assert not ok
        assert idx == 0
