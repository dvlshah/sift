"""Tombstone purge — drop manifest rows whose plan decision is TOMBSTONE_PURGE.

The decide-phase already emits ``Decision.TOMBSTONE_PURGE`` for URLs that
have been in GONE state past their per-tier tombstone TTL. Without an
actual delete step, those rows accumulate in the manifest indefinitely —
they don't affect correctness (the publish gates correctly classify GONE
as terminal), but they bloat the SQLite file and the changelog over time.

This module supplies the missing delete. Caller wraps in ``transaction()``.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

from .plan import PlanEntry


PURGE_DECISION = "TOMBSTONE_PURGE"


def purge_tombstones(
    conn: sqlite3.Connection,
    plan_entries: Sequence[PlanEntry] | Iterable[PlanEntry],
) -> dict[str, int]:
    """Delete manifest rows whose plan decision is TOMBSTONE_PURGE.

    Pure function over the plan's PlanEntry sequence — does not re-read the
    manifest or re-derive decisions. Caller is responsible for wrapping in
    a transaction; we DELETE inside the same connection so the operation
    composes with commit's transaction.

    Returns a counts dict: ``{"purged": N, "candidates": M}``. ``candidates``
    is the count of TOMBSTONE_PURGE decisions in the plan; ``purged`` is
    the count of rows actually deleted (some candidates may have already
    been removed by a previous run).
    """
    candidates = [e.url for e in plan_entries if e.decision == PURGE_DECISION]
    if not candidates:
        return {"purged": 0, "candidates": 0}

    # Use executemany for a single round-trip; rowcount is unreliable per-row
    # but the post-delete count is what matters.
    placeholders = ",".join(["?"] * len(candidates))
    pre = conn.execute(
        f"SELECT COUNT(*) FROM manifest WHERE url IN ({placeholders})",
        candidates,
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM manifest WHERE url IN ({placeholders})",
        candidates,
    )
    post = conn.execute(
        f"SELECT COUNT(*) FROM manifest WHERE url IN ({placeholders})",
        candidates,
    ).fetchone()[0]
    return {"purged": pre - post, "candidates": len(candidates)}
