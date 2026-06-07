"""Integrity primitives: Merkle tree over content hashes + chained log entries.

Two cryptographic constructs, both deterministic given canonical input:

  * `merkle_root(leaves)` — Bitcoin-style binary tree. Lets snapshot.json
    commit to the full set of content hashes with one root, so an auditor
    can verify the entire snapshot bit-for-bit by recomputing the root.

  * `chain_hash(prev, entry)` — each changelog entry includes the previous
    entry's hash. Walking forward, any tampered entry breaks the chain at
    that point — silent insertion/deletion becomes detectable.

Pure functions. No I/O. No external deps beyond hashlib + json (stdlib).
Versioned via `INTEGRITY_VERSION` so future format changes don't
invalidate old roots silently.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Optional

from ._io import sha256_hex

INTEGRITY_VERSION = "v1"


# ---- Merkle ----------------------------------------------------------------

def leaf_hash(url: str, content_hash: str) -> str:
    """Per-URL leaf hash: SHA-256(canonical_bytes(url, content_hash)).

    Canonical form: 'url:content_hash' joined by literal colon. We don't use
    JSON here to avoid encoding ambiguities; both inputs are ASCII-safe in
    our pipeline (canonicalized URLs + hex digests).
    """
    payload = f"{url}:{content_hash}".encode("utf-8")
    return sha256_hex(payload)


def merkle_root(leaves: Iterable[str]) -> Optional[str]:
    """Compute the root of a Bitcoin-style binary Merkle tree.

    Input: an iterable of hex digests (leaf hashes). Sort them first so the
    root is independent of insertion order. Returns None for empty input
    (caller should record as "no leaves" rather than mistake for a value).

    Odd-count levels duplicate the trailing node — standard Bitcoin pattern.
    """
    sorted_leaves = sorted(leaves)
    if not sorted_leaves:
        return None
    level = sorted_leaves
    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]  # duplicate the orphan
        next_level: list[str] = []
        for i in range(0, len(level), 2):
            combined = level[i] + level[i + 1]
            next_level.append(sha256_hex(combined.encode("utf-8")))
        level = next_level
    return level[0]


def compute_corpus_root(rows: Iterable[tuple[str, str]]) -> tuple[Optional[str], int]:
    """Convenience for callers: given an iterable of (url, content_hash) pairs,
    return (root_hex, leaf_count). Skips rows with empty content_hash."""
    leaves: list[str] = []
    for url, content_hash in rows:
        if not content_hash:
            continue
        leaves.append(leaf_hash(url, content_hash))
    return merkle_root(leaves), len(leaves)


# ---- Chained log -----------------------------------------------------------

# The keys we strip before hashing — they're either added by chain_hash itself
# (entry_hash/prev_hash) or are ergonomic-only fields that shouldn't affect
# the chain identity.
_CHAIN_HASH_STRIP_KEYS = frozenset({"entry_hash", "prev_hash"})

# Genesis prev_hash for the very first entry.
CHAIN_GENESIS = "sha256:" + "0" * 64


def canonical_entry_bytes(entry: dict) -> bytes:
    """Stable JSON serialization of a changelog entry minus the chain fields.

    sort_keys=True + separators=(",", ":") + utf-8 → byte-identical across
    runs, Python versions, and machines. This is what gets hashed.
    """
    payload = {k: v for k, v in entry.items() if k not in _CHAIN_HASH_STRIP_KEYS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def chain_hash(prev_hash: str, entry: dict) -> str:
    """Compute the entry_hash for one chained log entry.

    Definition: SHA-256(prev_hash_hex + canonical_bytes(entry_minus_chain_fields)).
    `prev_hash` should be CHAIN_GENESIS for the first entry, else the prior
    entry's entry_hash.

    Returns the hex digest (without 'sha256:' prefix). Callers that store it
    should add the prefix themselves to keep the on-disk format consistent
    with how we already store hashes.
    """
    prev_clean = prev_hash.removeprefix("sha256:") if prev_hash else "0" * 64
    h = hashlib.sha256()
    h.update(prev_clean.encode("utf-8"))
    h.update(canonical_entry_bytes(entry))
    return h.hexdigest()


def with_chain(prev_hash: str, entry: dict) -> dict:
    """Return entry with prev_hash + entry_hash fields populated.

    Convenience for the commit phase that wants to write the entry directly.
    """
    out = dict(entry)
    out["prev_hash"] = prev_hash if prev_hash else CHAIN_GENESIS
    out["entry_hash"] = "sha256:" + chain_hash(prev_hash, entry)
    return out


def verify_chain(entries: Iterable[dict]) -> tuple[bool, Optional[int], Optional[str]]:
    """Walk a sequence of changelog entries and verify the hash chain.

    Returns (ok, first_bad_index, reason). On success: (True, None, None).
    On failure: (False, index, human-readable reason).

    The first entry's prev_hash must equal CHAIN_GENESIS; each subsequent
    entry's prev_hash must equal the prior entry's entry_hash; and every
    entry's entry_hash must match the chain_hash(prev_hash, entry).
    """
    last_entry_hash: Optional[str] = None
    for i, entry in enumerate(entries):
        prev = entry.get("prev_hash")
        stored = entry.get("entry_hash")
        if prev is None or stored is None:
            return False, i, "entry missing prev_hash/entry_hash field"
        # Genesis check on first entry
        expected_prev = last_entry_hash if last_entry_hash is not None else CHAIN_GENESIS
        if prev != expected_prev:
            return False, i, (
                f"prev_hash mismatch: stored={prev[:16]}... expected={expected_prev[:16]}..."
            )
        recomputed = "sha256:" + chain_hash(prev, entry)
        if recomputed != stored:
            return False, i, (
                f"entry_hash mismatch: stored={stored[:16]}... recomputed={recomputed[:16]}..."
            )
        last_entry_hash = stored
    return True, None, None
