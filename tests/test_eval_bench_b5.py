"""B5 evals: seed (dedup + host_allow) and commit (changelog integrity)."""
from __future__ import annotations

import json

from evals.bench.per_stage.seed import (
    eval_seed_dedup, eval_seed_host_allow,
)
from evals.bench.per_stage.commit import eval_changelog_integrity


# ---- seed_dedup -----------------------------------------------------------

class TestSeedDedup:
    def test_collapses_canonical_equivalence_classes(self):
        """Drives the canonicalizer end-to-end via upsert_seed. If
        ``canonicalize_url`` regresses on trailing-slash, host-case, or
        fragment removal (the documented contract), this fails with a
        per-class failure list showing the canonical forms that landed."""
        r = eval_seed_dedup()
        assert r.cases == 3
        assert r.rate == 1.0, (
            f"dedup correctness failed: {r.failures}"
        )
        assert r.passed is True


# ---- seed_host_allow ------------------------------------------------------

class TestSeedHostAllow:
    def test_three_on_host_three_off_host(self):
        r = eval_seed_host_allow()
        assert r.inserted == 3
        assert r.skipped_host == 3
        assert r.passed is True


# ---- changelog integrity --------------------------------------------------

class TestChangelogIntegrity:
    def test_missing_file_returns_safe_zero(self, tmp_path):
        r = eval_changelog_integrity(tmp_path)
        assert r.passed is False
        assert "no changelog" in r.note

    def test_empty_file_returns_zero(self, tmp_path):
        from sift import paths
        cl = paths.changelog_path(tmp_path)
        cl.parent.mkdir(parents=True, exist_ok=True)
        cl.write_text("")
        r = eval_changelog_integrity(tmp_path)
        assert r.entries == 0
        assert r.passed is False
        assert "empty" in r.note

    def test_valid_chain_passes(self, tmp_path):
        """Build a real two-entry chain via the canonical integrity helpers
        and verify the eval reports it intact."""
        from sift import paths
        from sift.integrity import CHAIN_GENESIS, with_chain

        cl = paths.changelog_path(tmp_path)
        cl.parent.mkdir(parents=True, exist_ok=True)

        # First entry: prev_hash = genesis
        e1 = with_chain(CHAIN_GENESIS, {"event": "test", "url": "https://x/1", "n": 1})
        e2 = with_chain(e1["entry_hash"], {"event": "test", "url": "https://x/2", "n": 2})

        cl.write_text(json.dumps(e1) + "\n" + json.dumps(e2) + "\n")
        r = eval_changelog_integrity(tmp_path)
        assert r.entries == 2
        assert r.breaks == 0
        assert r.passed is True
        assert "intact" in r.note

    def test_tampered_chain_fails(self, tmp_path):
        from sift import paths
        from sift.integrity import CHAIN_GENESIS, with_chain

        cl = paths.changelog_path(tmp_path)
        cl.parent.mkdir(parents=True, exist_ok=True)

        e1 = with_chain(CHAIN_GENESIS, {"event": "test", "n": 1})
        # Tamper: change a field after the hash was computed. Re-hashing the
        # tampered entry will not match the stored entry_hash.
        tampered = dict(e1)
        tampered["n"] = 999
        cl.write_text(json.dumps(tampered) + "\n")
        r = eval_changelog_integrity(tmp_path)
        assert r.entries == 1
        assert r.breaks == 1
        assert r.passed is False
        assert "broke" in r.note
