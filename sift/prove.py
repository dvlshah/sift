"""Proof-carrying answers: reconstruct a published run's leaf set from its md
tree, self-check it against the stored Merkle root, and emit a self-contained
inclusion-proof envelope.

The load-bearing safety rule (see ``load_verified_leafset``): a proof is NEVER
emitted unless the leaves reconstructed from ``runs/<id>/md/`` recompute to the
exact ``merkle_root`` already stored in that run's ``snapshot.json``. So a proof
can only ever attest to the published commitment — a corrupt/tampered run dir
refuses rather than produces a misleading proof.

The leaf source is the retained md tree (not the manifest): the manifest is
current-state-only and can't reconstruct a historical run, while every published
run's md tree is pinned to the leaf set ``write_snapshot`` hashed (publish gate
G1 enforces md↔row parity bidirectionally). So one uniform path serves both
current and ``as_of`` proofs.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._io import parse_frontmatter, read_snapshot, split_frontmatter
from .classify import canonicalize_url
from .manifest import open_manifest_ro
from .integrity import (
    INTEGRITY_VERSION,
    build_proof_envelope,
    compute_corpus_root,
    leaf_hash,
    merkle_proof,
)


class ProofError(Exception):
    """Refusal to emit a proof. Raised instead of ever returning a partial,
    best-effort, or unverifiable proof — the caller surfaces the message."""


@dataclass(frozen=True)
class VerifiedLeafSet:
    """A run's leaf set, proven to reproduce the stored snapshot root."""
    run_id: str
    root: str
    completed_at: Optional[str]
    scheme: Optional[str]
    integrity_version: str
    leaf_count: Optional[int]
    sorted_leaves: list           # sorted leaf hashes — the exact tree input
    url_to_hash: dict             # url -> bare content_hash hex
    source: str                   # which leaf source reproduced the root: "md" | "manifest"


def reconstruct_leaves(run_dir: Path) -> tuple[list[tuple[str, str]], list[Path]]:
    """Return (pairs, malformed) where pairs is [(url, content_hash_hex)] parsed
    from every md file's frontmatter (the 'sha256:' prefix stripped to bare hex),
    and malformed is any md file with missing/invalid frontmatter. Never re-hashes
    the body — the leaf commits to the *published* content_hash string."""
    md_root = run_dir / "md"
    if not md_root.is_dir():
        return [], []                       # no md tree → let the manifest source try
    pairs: list[tuple[str, str]] = []
    malformed: list[Path] = []
    for f in md_root.rglob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            malformed.append(f)
            continue
        fm, _ = split_frontmatter(text)
        if not fm:
            malformed.append(f)
            continue
        meta = parse_frontmatter(fm)
        url = meta.get("url")
        if not url:
            malformed.append(f)
            continue
        ch = meta.get("content_hash", "").removeprefix("sha256:")
        if ch == "":
            continue  # matches compute_corpus_root skipping falsy content_hash
        pairs.append((url, ch))
    return pairs, malformed


def _manifest_pairs(manifest_path: Path) -> list[tuple[str, str]]:
    """(url, content_hash) for FRESH/FROZEN rows with a content_hash — the exact
    set ``write_snapshot`` hashes. A valid leaf source ONLY when it reproduces
    the stored root (true for the current run; the self-check enforces it)."""
    conn = open_manifest_ro(manifest_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT url, content_hash FROM manifest WHERE state IN ('FRESH','FROZEN') "
            "AND content_hash IS NOT NULL AND content_hash != ''"
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows]


def load_verified_leafset(
    run_dir: Path, *, manifest_path: Optional[Path] = None,
) -> VerifiedLeafSet:
    """Return ``run_dir``'s leaf set from the first source that PROVABLY
    reproduces the stored snapshot root. Sources, in order: the run's md tree
    (the only source valid for a historical run), then the live manifest (a sound
    fallback — used only when it reproduces the stored root, which holds for the
    current run; the self-check rejects it otherwise, e.g. for a stale historical
    root). Raises ``ProofError`` if no source reproduces the root — a proof is
    never emitted against an unverified leaf set. Generalizes the leaf source so
    indexes whose md tree drifted from the committed set (md count < leaf_count)
    are still provable via the manifest."""
    snap = read_snapshot(run_dir)
    integ = snap.get("integrity") or {}
    stored_root = integ.get("merkle_root")
    stored_count = integ.get("leaf_count")
    if not stored_root:
        raise ProofError(
            f"run {run_dir.name}: no integrity.merkle_root "
            "(pre-integrity-v1 snapshot); cannot anchor a proof."
        )

    attempts: list[str] = []

    def _try(source: str, pairs: list[tuple[str, str]]) -> Optional[VerifiedLeafSet]:
        root, count = compute_corpus_root(pairs)
        attempts.append(f"{source}: count={count} root={str(root)[:12] if root else None}")
        if root != stored_root:
            return None
        url_to_hash: dict[str, str] = {}
        for (u, c) in pairs:
            if u in url_to_hash and url_to_hash[u] != c:
                raise ProofError(
                    f"REFUSING TO PROVE: url {u} maps to two content_hashes in "
                    f"{run_dir.name} ({source} source); source corrupt."
                )
            url_to_hash[u] = c
        versions = snap.get("versions") or {}
        return VerifiedLeafSet(
            run_id=run_dir.name, root=stored_root,
            completed_at=snap.get("completed_at"),
            scheme=integ.get("scheme"),
            integrity_version=versions.get("integrity") or INTEGRITY_VERSION,
            leaf_count=stored_count,
            sorted_leaves=sorted(leaf_hash(u, c) for (u, c) in pairs),
            url_to_hash=url_to_hash, source=source,
        )

    # Source 1: the retained md tree (valid for current AND historical runs).
    pairs_md, malformed = reconstruct_leaves(run_dir)
    if malformed:
        attempts.append(f"md: {len(malformed)} malformed (e.g. {malformed[0].name})")
    else:
        vls = _try("md", pairs_md)
        if vls is not None:
            return vls

    # Source 2: the live manifest (sound only when it reproduces the stored root).
    if manifest_path is not None and Path(manifest_path).exists():
        vls = _try("manifest", _manifest_pairs(Path(manifest_path)))
        if vls is not None:
            return vls

    raise ProofError(
        "REFUSING TO PROVE: no leaf source reproduces the stored root for "
        f"{run_dir.name} (stored {stored_root[:16]}…, leaf_count={stored_count}). "
        f"Tried [{'; '.join(attempts)}]. The run's md tree and the live manifest "
        "are both incomplete or out of sync with this snapshot; run "
        f"`sift verify-snapshot --run-id {run_dir.name}` and treat as untrusted."
    )


def build_proof_for_run(
    run_dir: Path, url: str, *, manifest_path: Optional[Path] = None,
) -> dict:
    """Self-check ``run_dir``, locate ``url``, and return either the inclusion
    envelope (``included: True``) or a not-in-corpus answer (``included: False``).
    Raises ``ProofError`` on refusal. The caller resolves ``run_dir`` (current or
    ``as_of``) in its own idiom (MCP error result / CLI exit code) and passes the
    index ``manifest_path`` so the manifest can serve as a fallback leaf source."""
    vls = load_verified_leafset(run_dir, manifest_path=manifest_path)

    # Match the query URL to a committed leaf. The published leaf used the
    # frontmatter URL verbatim, so the matched dict KEY is the URL to hash.
    leaf_url = canonicalize_url(url)
    ch = vls.url_to_hash.get(leaf_url)
    if ch is None:                       # fall back to the raw URL as given
        ch = vls.url_to_hash.get(url)
        leaf_url = url
    if ch is None:
        return {
            "included": False,
            "url": url,
            "run_id": vls.run_id,
            "merkle_root": vls.root,
            "leaf_source": vls.source,
            "reason": "url not in this published snapshot",
            "hint": ("Locate it with grep_corpus / changed_since, or check the "
                     "trailing slash / canonical form of the URL."),
        }

    leaf = leaf_hash(leaf_url, ch)
    proof = merkle_proof(vls.sorted_leaves, leaf)
    if proof is None:                    # invariant: leaf is in the verified set
        raise ProofError(
            f"internal: leaf for {leaf_url} not found in the verified leaf set "
            f"of {vls.run_id} despite a content_hash match."
        )
    envelope = build_proof_envelope(
        url=leaf_url,
        content_hash_hex=ch,
        leaf=leaf,
        run_id=vls.run_id,
        completed_at=vls.completed_at,
        merkle_root=vls.root,
        leaf_count=vls.leaf_count or 0,
        scheme=vls.scheme or "sorted-leaves-bitcoin-style-sha256",
        integrity_version=vls.integrity_version,
        proof=proof,
    )
    envelope["included"] = True
    envelope["leaf_source"] = vls.source

    # Attach the external RFC-3161 timestamp token, if this snapshot was witnessed,
    # so the proof carries its own independent date-witness — verifiable with
    # `openssl ts -verify` or `sift verify-timestamp`, without trusting sift.
    tsr = run_dir / "merkle_root.tsr"
    ts_meta = (read_snapshot(run_dir).get("integrity") or {}).get("timestamp")
    if tsr.is_file() and ts_meta:
        envelope["timestamp"] = {
            "tsa_url": ts_meta.get("tsa_url"),
            "time": ts_meta.get("time"),
            "hash_algorithm": ts_meta.get("hash_algorithm", "sha256"),
            "rfc3161_token_b64": base64.b64encode(tsr.read_bytes()).decode("ascii"),
        }
    return envelope
