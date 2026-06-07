"""consolidate_md_tree + tightened gate_manifest_fs_integrity.

The bug: incremental runs only wrote md for newly-fetched URLs; routes.tsv
referenced paths that didn't exist in current/, and the gate's old "warn but
pass" behavior hid the problem.
"""

import json
from pathlib import Path

import pytest

from sift import paths
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)
from sift.publish import (
    consolidate_md_tree,
    gate_manifest_fs_integrity,
)


def _seed(conn, url: str, tier: str, content_hash: str):
    now = now_utc()
    with transaction(conn):
        upsert_seed(conn, url, tier, None, "v1", None, now)
        apply_fetch_result(
            conn, url=url, now=now,
            http_status=200, http_etag=None, http_last_modified=None,
            raw_hash=f"raw_{content_hash}", content_hash=content_hash,
            crawler_version="v1.0.0",
            extractor_version="trafilatura-2.0.0-cfg3",
            normalizer_version="v1", error=None,
        )


def _write_md(root: Path, run_id: str, url: str, body: str):
    md = paths.md_path(root, run_id, url)
    md.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"url: {url}\n"
        "tier: LIVING\n"
        "content_hash: sha256:abc\n"
        "---\n"
    )
    md.write_text(fm + body)


class TestConsolidate:
    def test_links_forward_missing_md(self, tmp_path):
        """Common incremental case: a FRESH URL has md in run-1 but not run-2.
        consolidate_md_tree should link it into run-2."""
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        url = "https://www.ato.gov.au/x/y"
        _seed(conn, url, "LIVING", "c1")

        # Old run has the md
        _write_md(root, "run-1", url, "# X/Y body, plenty of text here." * 10)
        # New run does not
        new_run_dir = paths.run_dir(root, "run-2")
        (new_run_dir / "md").mkdir(parents=True)

        linked, missing = consolidate_md_tree(conn, root, "run-2")
        assert linked == 1
        assert missing == 0
        target = paths.md_path(root, "run-2", url)
        assert target.exists()
        # Hardlink: same inode as the source
        src = paths.md_path(root, "run-1", url)
        assert target.stat().st_ino == src.stat().st_ino

    def test_skips_when_already_present(self, tmp_path):
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        url = "https://www.ato.gov.au/x"
        _seed(conn, url, "LIVING", "c1")
        _write_md(root, "run-2", url, "body" * 20)
        linked, missing = consolidate_md_tree(conn, root, "run-2")
        assert linked == 0
        assert missing == 0

    def test_reports_still_missing(self, tmp_path):
        """If the URL is FRESH but no prior run has the md, report as still_missing."""
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        url = "https://www.ato.gov.au/lost"
        _seed(conn, url, "LIVING", "c1")
        # Nothing in any run dir
        (paths.run_dir(root, "run-2") / "md").mkdir(parents=True)
        linked, missing = consolidate_md_tree(conn, root, "run-2")
        assert linked == 0
        assert missing == 1

    def test_skips_rows_without_content_hash(self, tmp_path):
        """A row with state=FAILED or no content_hash has nothing to link."""
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        url = "https://www.ato.gov.au/no-content"
        # Seed-only, never fetched
        with transaction(conn):
            upsert_seed(conn, url, "LIVING", None, "v1", None, now_utc())
        (paths.run_dir(root, "run-2") / "md").mkdir(parents=True)
        linked, missing = consolidate_md_tree(conn, root, "run-2")
        assert linked == 0
        assert missing == 0


class TestGateAfterConsolidate:
    def test_gate_passes_when_consolidate_links_forward(self, tmp_path):
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        url = "https://www.ato.gov.au/x"
        _seed(conn, url, "LIVING", "c1")
        _write_md(root, "run-1", url, "body" * 20)
        new_run_dir = paths.run_dir(root, "run-2")
        (new_run_dir / "md").mkdir(parents=True)
        # Gate fails BEFORE consolidate
        ok, det = gate_manifest_fs_integrity(conn, root, "run-2")
        assert not ok, f"gate should fail pre-consolidate: {det}"
        # ... and passes after
        consolidate_md_tree(conn, root, "run-2")
        ok, det = gate_manifest_fs_integrity(conn, root, "run-2")
        assert ok, f"gate should pass post-consolidate: {det}"

    def test_gate_fails_on_genuinely_missing_md(self, tmp_path):
        """If no prior run has the md, consolidate can't help — gate must fail."""
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        _seed(conn, "https://www.ato.gov.au/lost", "LIVING", "c1")
        (paths.run_dir(root, "run-2") / "md").mkdir(parents=True)
        consolidate_md_tree(conn, root, "run-2")
        ok, det = gate_manifest_fs_integrity(conn, root, "run-2")
        assert not ok
        assert "missing md" in det
