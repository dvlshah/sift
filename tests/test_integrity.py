"""Pure integrity helpers: Merkle root, chain hash, chain verification."""

import json

import pytest

from sift.integrity import (
    CHAIN_GENESIS,
    canonical_entry_bytes,
    chain_hash,
    compute_corpus_root,
    leaf_hash,
    merkle_root,
    verify_chain,
    with_chain,
)


class TestLeafHash:
    def test_deterministic(self):
        assert leaf_hash("https://x/a", "abc") == leaf_hash("https://x/a", "abc")

    def test_different_inputs_different_hash(self):
        assert leaf_hash("https://x/a", "abc") != leaf_hash("https://x/b", "abc")
        assert leaf_hash("https://x/a", "abc") != leaf_hash("https://x/a", "def")


class TestMerkleRoot:
    def test_empty_is_none(self):
        assert merkle_root([]) is None

    def test_single_leaf(self):
        h = "a" * 64
        # With one leaf, the root is that leaf itself.
        assert merkle_root([h]) == h

    def test_order_independent(self):
        leaves = ["a" * 64, "b" * 64, "c" * 64, "d" * 64]
        a = merkle_root(leaves)
        b = merkle_root(reversed(leaves))
        c = merkle_root(["c" * 64, "a" * 64, "d" * 64, "b" * 64])
        assert a == b == c

    def test_deterministic(self):
        leaves = [f"{i:064x}" for i in range(10)]
        assert merkle_root(leaves) == merkle_root(leaves)

    def test_odd_count_handled(self):
        # 3 leaves → trailing duplicate at level 1
        r = merkle_root(["a" * 64, "b" * 64, "c" * 64])
        assert r is not None
        assert len(r) == 64  # hex sha256

    def test_change_in_any_leaf_changes_root(self):
        leaves = ["a" * 64, "b" * 64, "c" * 64, "d" * 64]
        original = merkle_root(leaves)
        for i in range(len(leaves)):
            tampered = list(leaves)
            tampered[i] = "f" * 64
            assert merkle_root(tampered) != original


class TestComputeCorpusRoot:
    def test_skips_rows_without_content_hash(self):
        rows = [("https://x/a", "h1"), ("https://x/b", ""), ("https://x/c", "h2")]
        root, n = compute_corpus_root(rows)
        assert n == 2
        # Should equal the root computed without the empty row
        manual_root, _ = compute_corpus_root([("https://x/a", "h1"), ("https://x/c", "h2")])
        assert root == manual_root

    def test_empty_corpus(self):
        root, n = compute_corpus_root([])
        assert root is None
        assert n == 0


class TestChainHash:
    def test_canonical_strips_chain_fields(self):
        entry = {"ts": "2026-01-01", "url": "https://x", "prev_hash": "X", "entry_hash": "Y"}
        canon = canonical_entry_bytes(entry)
        decoded = json.loads(canon)
        assert "prev_hash" not in decoded
        assert "entry_hash" not in decoded
        assert decoded == {"ts": "2026-01-01", "url": "https://x"}

    def test_canonical_sorts_keys(self):
        a = canonical_entry_bytes({"b": 1, "a": 2})
        b = canonical_entry_bytes({"a": 2, "b": 1})
        assert a == b

    def test_chain_hash_deterministic(self):
        e = {"ts": "2026", "url": "https://x"}
        prev = "sha256:" + "0" * 64
        assert chain_hash(prev, e) == chain_hash(prev, e)

    def test_chain_hash_changes_with_prev(self):
        e = {"ts": "2026", "url": "https://x"}
        h1 = chain_hash("sha256:" + "0" * 64, e)
        h2 = chain_hash("sha256:" + "1" * 64, e)
        assert h1 != h2

    def test_with_chain_adds_fields(self):
        e = {"ts": "2026", "url": "https://x"}
        chained = with_chain(CHAIN_GENESIS, e)
        assert chained["prev_hash"] == CHAIN_GENESIS
        assert chained["entry_hash"].startswith("sha256:")
        # Original fields preserved
        assert chained["ts"] == "2026"
        assert chained["url"] == "https://x"


class TestVerifyChain:
    def _build_chain(self, entries: list[dict]) -> list[dict]:
        """Build a valid chained log from a list of entries."""
        out: list[dict] = []
        prev = ""
        for e in entries:
            chained = with_chain(prev, e)
            out.append(chained)
            prev = chained["entry_hash"]
        return out

    def test_valid_chain_passes(self):
        chain = self._build_chain([
            {"ts": "t1", "url": "a"},
            {"ts": "t2", "url": "b"},
            {"ts": "t3", "url": "c"},
        ])
        ok, idx, reason = verify_chain(chain)
        assert ok
        assert idx is None
        assert reason is None

    def test_empty_chain_passes(self):
        ok, idx, reason = verify_chain([])
        assert ok

    def test_tampered_field_caught(self):
        """Modify a content field — entry_hash should no longer match."""
        chain = self._build_chain([{"ts": "t1", "url": "a"}, {"ts": "t2", "url": "b"}])
        chain[0]["url"] = "TAMPERED"
        ok, idx, reason = verify_chain(chain)
        assert not ok
        assert idx == 0
        assert "entry_hash mismatch" in reason

    def test_deleted_entry_caught(self):
        """Drop a middle entry — chain breaks at the next entry's prev_hash."""
        chain = self._build_chain([
            {"ts": "t1"}, {"ts": "t2"}, {"ts": "t3"},
        ])
        chain.pop(1)
        ok, idx, reason = verify_chain(chain)
        assert not ok
        # The deletion shifts: what was entry 2 is now at index 1, and its
        # prev_hash points at the deleted entry 1's hash, not the new index 0's.
        assert idx == 1
        assert "prev_hash mismatch" in reason

    def test_inserted_entry_caught(self):
        """Add a fake entry — its hash won't chain to the next one."""
        chain = self._build_chain([{"ts": "t1"}, {"ts": "t2"}])
        fake = {"ts": "fake", "prev_hash": chain[0]["entry_hash"],
                "entry_hash": "sha256:" + "f" * 64}
        chain.insert(1, fake)
        ok, idx, reason = verify_chain(chain)
        assert not ok
        # The fake entry's stored entry_hash is fictitious
        assert idx == 1
        assert "entry_hash mismatch" in reason

    def test_first_entry_must_chain_to_genesis(self):
        chain = self._build_chain([{"ts": "t1"}])
        chain[0]["prev_hash"] = "sha256:" + "1" * 64
        # Recompute its entry_hash to make the field self-consistent,
        # but the prev_hash != CHAIN_GENESIS should still trip the check.
        chain[0]["entry_hash"] = "sha256:" + chain_hash(chain[0]["prev_hash"], chain[0])
        ok, idx, reason = verify_chain(chain)
        assert not ok
        assert idx == 0
