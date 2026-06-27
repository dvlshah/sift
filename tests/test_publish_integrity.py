"""Publish-time integrity additions: facts gate, Merkle root, snapshot signing."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sift import paths
from sift.integrity import compute_corpus_root
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)
from sift.publish import (
    gate_facts_validation,
    gpg_sign_snapshot,
    write_snapshot,
)


@pytest.fixture
def small_index(tmp_path):
    root = tmp_path
    run_id = "test-run"
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    now = now_utc()
    for i, (url, tier) in enumerate([
        ("https://www.ato.gov.au/a", "LIVING"),
        ("https://www.ato.gov.au/b", "LIVING"),
        ("https://www.ato.gov.au/c", "FROZEN"),
    ]):
        with transaction(conn):
            upsert_seed(conn, url, tier, None, "v1", None, now)
            apply_fetch_result(
                conn, url=url, now=now, http_status=200,
                http_etag=None, http_last_modified=None,
                raw_hash=f"r{i:064d}", content_hash=f"c{i:064d}",
                crawler_version="v1", extractor_version="ext",
                normalizer_version="v1", error=None,
            )
    return root, run_id, conn


class TestFactsValidationGate:
    def test_passes_when_no_facts(self, small_index):
        root, run_id, _ = small_index
        ok, det = gate_facts_validation(root, run_id)
        assert ok
        assert "no facts" in det

    def test_passes_on_valid_facts(self, small_index):
        root, run_id, _ = small_index
        facts = paths.run_dir(root, run_id) / "facts"
        (facts / "schemas").mkdir(parents=True)
        (facts / "schemas" / "test-v1.json").write_text(json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "test-v1",
            "type": "object",
            "required": ["$schema", "x"],
            "properties": {"$schema": {"const": "test-v1"}, "x": {"type": "integer"}},
        }))
        (facts / "test-v1").mkdir()
        (facts / "test-v1" / "ok.json").write_text(
            json.dumps({"$schema": "test-v1", "x": 1})
        )
        ok, det = gate_facts_validation(root, run_id)
        assert ok
        assert "1/1 facts files valid" in det

    def test_fails_on_invalid_facts(self, small_index):
        root, run_id, _ = small_index
        facts = paths.run_dir(root, run_id) / "facts"
        (facts / "schemas").mkdir(parents=True)
        (facts / "schemas" / "test-v1.json").write_text(json.dumps({
            "$id": "test-v1", "type": "object",
            "required": ["$schema", "x"],
            "properties": {"$schema": {"const": "test-v1"}, "x": {"type": "integer"}},
        }))
        (facts / "test-v1").mkdir()
        (facts / "test-v1" / "bad.json").write_text(
            json.dumps({"$schema": "test-v1", "x": "not-an-integer"})
        )
        ok, det = gate_facts_validation(root, run_id)
        assert not ok
        assert "invalid facts" in det

    def test_fails_on_unknown_schema(self, small_index):
        root, run_id, _ = small_index
        facts = paths.run_dir(root, run_id) / "facts"
        (facts / "test-v1").mkdir(parents=True)
        # No schemas/ dir at all
        (facts / "test-v1" / "x.json").write_text(json.dumps({"$schema": "ghost"}))
        ok, det = gate_facts_validation(root, run_id)
        assert not ok


class TestSnapshotMerkleRoot:
    def test_root_present_in_snapshot(self, small_index):
        root, run_id, conn = small_index
        snap_path = write_snapshot(
            root, run_id, conn=conn,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            expected_urls=3,
            gate_results=[("dummy", True, "ok")],
            status="published",
        )
        snap = json.loads(snap_path.read_text())
        integ = snap.get("integrity") or {}
        assert integ.get("merkle_root") is not None
        assert integ.get("leaf_count") == 3
        assert integ.get("scheme") == "sorted-leaves-bitcoin-style-sha256"
        assert snap["versions"]["integrity"] == "v1"

    def test_root_matches_recomputation(self, small_index):
        root, run_id, conn = small_index
        snap_path = write_snapshot(
            root, run_id, conn=conn,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            expected_urls=3,
            gate_results=[],
            status="published",
        )
        snap = json.loads(snap_path.read_text())
        stored = snap["integrity"]["merkle_root"]
        # Recompute from the manifest the same way an auditor would
        from sift.manifest import iter_all
        rows = [(r.url, r.content_hash) for r in iter_all(conn)
                if r.state in ("FRESH", "FROZEN") and r.content_hash]
        recomputed, _ = compute_corpus_root(rows)
        assert stored == recomputed


class TestSnapshotDerivationEnv:
    """The snapshot records the native derivation stack (lxml/libxml2/unicode/
    python) the content_hash depends on, so a verifier reseeding on a different
    stack can detect drift instead of silently recomputing a divergent Merkle
    root and crying tamper."""

    def test_env_recorded(self, small_index):
        root, run_id, conn = small_index
        snap_path = write_snapshot(
            root, run_id, conn=conn,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            expected_urls=3, gate_results=[], status="published",
        )
        snap = json.loads(snap_path.read_text())
        env = snap.get("derivation_env") or {}
        # python + unicode are always resolvable. lxml/libxml2 are best-effort
        # (omitted if lxml can't import) — assert them only when present, matching
        # _derivation_env's documented contract rather than mandating them.
        assert env.get("python")
        assert env.get("unicode")
        if "lxml" in env:
            assert env["lxml"]
            assert env["libxml2"]


def _gpg_available_with_key() -> tuple[bool, str]:
    """Best-effort check: is gpg installed AND does it have at least one secret key?"""
    try:
        result = subprocess.run(
            ["gpg", "--list-secret-keys", "--with-colons"],
            capture_output=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return False, ""
        # Find the first secret key id from the output
        for line in result.stdout.decode().splitlines():
            if line.startswith("sec:"):
                # next 'fpr:' line gives the fingerprint
                continue
            if line.startswith("fpr:"):
                fields = line.split(":")
                if len(fields) >= 10 and fields[9]:
                    return True, fields[9]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""
    return False, ""


class TestGpgSign:
    def test_returns_none_when_gpg_missing(self, small_index):
        # If gpg isn't on PATH (or key doesn't exist), function returns None
        # without raising. We test the no-key branch by passing a bogus key.
        root, run_id, conn = small_index
        snap_path = write_snapshot(
            root, run_id, conn=conn, started_at="t", completed_at="t",
            expected_urls=3, gate_results=[], status="published",
        )
        sig = gpg_sign_snapshot(snap_path, "0000000000000000000000000000000000000000")
        # Either no gpg installed (None) or bogus key fails (None). Both fine.
        assert sig is None
