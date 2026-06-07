"""SQLite-backed manifest: the single source of truth for URL state.

The manifest is the only durable state the pipeline reads or writes outside of
content blobs. Each phase reads it (or its own checkpoint file) and produces an
append-only log; only `commit` writes back, in a single transaction.

Schema is migration-aware via the `meta` table's `schema_version` row.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA_VERSION = 2

# Per-version DDL applied to reach (key + 1). Each runs in its own transaction.
# Forward-only — never edit a past migration once it's shipped.
_MIGRATIONS: dict[int, str] = {
    # v1 -> v2: browser_version column for the browser-fetch capability.
    1: "ALTER TABLE manifest ADD COLUMN browser_version TEXT;",
}

# State machine values used by the decide-phase state machine (see decide.py).
# Only the values branched on are exported; intermediate states like
# UNSEEN/FRESH/STALE/FROZEN are written as string literals in SQL.
STATE_GONE = "GONE"
STATE_FAILED = "FAILED"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest (
    url                  TEXT PRIMARY KEY,
    tier                 TEXT NOT NULL,
    parent_guide         TEXT,
    state                TEXT NOT NULL DEFAULT 'UNSEEN',

    -- Discovery
    sitemap_lastmod_seen TEXT,
    first_seen_at        TEXT NOT NULL,

    -- Fetch metadata
    last_fetched_at      TEXT,
    last_attempted_at    TEXT,
    http_status          INTEGER,
    http_etag            TEXT,
    http_last_modified   TEXT,

    -- Content
    raw_hash             TEXT,        -- sha256 of raw HTML body
    content_hash         TEXT,        -- sha256 of normalize_for_hash(markdown)
    last_changed_at      TEXT,        -- ISO 8601 UTC; updated only when content_hash changes
    unchanged_streak     INTEGER NOT NULL DEFAULT 0,

    -- Provenance versions (any bump triggers re-derive in the right phase)
    crawler_version      TEXT,
    extractor_version    TEXT,
    normalizer_version   TEXT,
    classifier_version   TEXT,
    browser_version      TEXT,        -- non-NULL iff fetched via the browser path

    -- Failure tracking
    fail_count           INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_manifest_state        ON manifest(state);
CREATE INDEX IF NOT EXISTS idx_manifest_tier         ON manifest(tier);
CREATE INDEX IF NOT EXISTS idx_manifest_parent_guide ON manifest(parent_guide);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    phase        TEXT,                -- last phase attempted
    status       TEXT,                -- running / succeeded / degraded / failed
    counts_json  TEXT,                -- per-tier counters
    error        TEXT
);
"""


@dataclass
class ManifestRow:
    url: str
    tier: str
    parent_guide: Optional[str]
    state: str
    sitemap_lastmod_seen: Optional[str]
    first_seen_at: str
    last_fetched_at: Optional[str]
    last_attempted_at: Optional[str]
    http_status: Optional[int]
    http_etag: Optional[str]
    http_last_modified: Optional[str]
    raw_hash: Optional[str]
    content_hash: Optional[str]
    last_changed_at: Optional[str]
    unchanged_streak: int
    crawler_version: Optional[str]
    extractor_version: Optional[str]
    normalizer_version: Optional[str]
    classifier_version: Optional[str]
    browser_version: Optional[str]
    fail_count: int
    last_error: Optional[str]


def now_utc() -> str:
    """ISO 8601 UTC timestamp; second precision is plenty for our purposes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path: Path) -> sqlite3.Connection:
    """Open (or create) the manifest DB with WAL + sane defaults."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def open_manifest_ro(db: Path) -> Optional[sqlite3.Connection]:
    """Open a manifest sqlite file read-only (``?mode=ro``) with row access.

    Returns ``None`` if the file is missing or unreadable so callers — the
    MCP discovery/query path, ``sift verify`` — degrade gracefully rather
    than fail because one index lost its manifest. The read-only counterpart
    to :func:`open_db` (which creates + WAL-configures for writes)."""
    db = Path(db)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _migrate(conn: sqlite3.Connection, from_v: int, to_v: int) -> None:
    """Apply each migration in order from ``from_v`` up to ``to_v``.

    Migrations are single-statement DDL today (one ALTER per version bump).
    Each runs in its own transaction so a partial failure leaves a clear
    ``meta.schema_version`` to recover from. Forward-only; never rolls back
    across versions.
    """
    for v in range(from_v, to_v):
        sql = _MIGRATIONS[v]
        with conn:  # implicit transaction
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(v + 1)),
            )


def init_schema(conn: sqlite3.Connection) -> None:
    """Ensure the manifest DB is at the current schema version.

    Fresh DBs: CREATE TABLE IF NOT EXISTS lands directly at the current schema
    (the DDL already includes every column). The schema_version row is then
    written via INSERT OR REPLACE.

    Existing DBs: the CREATE-IF-NOT-EXISTS is a no-op on the old tables. We
    read the existing meta.schema_version and run any pending migrations.
    Both paths converge at the same final schema.
    """
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is not None:
        try:
            current_v = int(row[0])
        except (TypeError, ValueError):
            current_v = SCHEMA_VERSION  # corrupt sentinel → assume current
        if current_v < SCHEMA_VERSION:
            _migrate(conn, current_v, SCHEMA_VERSION)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit-BEGIN transaction (we run autocommit at the connection level
    so individual statements don't implicitly commit)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _row(r: sqlite3.Row | None) -> Optional[ManifestRow]:
    return ManifestRow(**dict(r)) if r else None


def get_row(conn: sqlite3.Connection, url: str) -> Optional[ManifestRow]:
    return _row(conn.execute("SELECT * FROM manifest WHERE url = ?", (url,)).fetchone())


def iter_all(conn: sqlite3.Connection, tier: Optional[str] = None) -> Iterator[ManifestRow]:
    if tier is None:
        cur = conn.execute("SELECT * FROM manifest ORDER BY url")
    else:
        cur = conn.execute(
            "SELECT * FROM manifest WHERE tier = ? ORDER BY url", (tier,)
        )
    for r in cur:
        yield ManifestRow(**dict(r))


def upsert_seed(
    conn: sqlite3.Connection,
    url: str,
    tier: str,
    parent_guide_: Optional[str],
    classifier_version: str,
    sitemap_lastmod: Optional[str],
    now: str,
) -> str:
    """Insert a URL if new, or refresh tier/parent_guide if the classifier version moved.

    Returns 'inserted', 'reclassified', or 'noop'.
    """
    existing = conn.execute(
        "SELECT classifier_version, tier, parent_guide FROM manifest WHERE url = ?",
        (url,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO manifest(url, tier, parent_guide, state, first_seen_at,
                                 classifier_version, sitemap_lastmod_seen)
            VALUES (?, ?, ?, 'UNSEEN', ?, ?, ?)
            """,
            (url, tier, parent_guide_, now, classifier_version, sitemap_lastmod),
        )
        return "inserted"
    if existing["classifier_version"] != classifier_version or existing["tier"] != tier:
        conn.execute(
            """
            UPDATE manifest
               SET tier = ?, parent_guide = ?, classifier_version = ?,
                   sitemap_lastmod_seen = COALESCE(?, sitemap_lastmod_seen)
             WHERE url = ?
            """,
            (tier, parent_guide_, classifier_version, sitemap_lastmod, url),
        )
        return "reclassified"
    # Always update sitemap_lastmod_seen if a newer one arrived.
    if sitemap_lastmod is not None:
        conn.execute(
            """
            UPDATE manifest
               SET sitemap_lastmod_seen = ?
             WHERE url = ? AND (sitemap_lastmod_seen IS NULL OR sitemap_lastmod_seen < ?)
            """,
            (sitemap_lastmod, url, sitemap_lastmod),
        )
    return "noop"


def apply_fetch_result(
    conn: sqlite3.Connection,
    *,
    url: str,
    now: str,
    http_status: int,
    http_etag: Optional[str],
    http_last_modified: Optional[str],
    raw_hash: Optional[str],
    content_hash: Optional[str],
    crawler_version: str,
    extractor_version: Optional[str],
    normalizer_version: Optional[str],
    error: Optional[str],
    browser_version: Optional[str] = None,
) -> None:
    """Apply a single URL's fetch+extract outcome. Caller wraps in transaction().

    ``browser_version`` is None for the http transport and non-None for the
    browser transport (typically :data:`sift.browser.BROWSER_VERSION`).
    Persisted with COALESCE so http re-fetches don't clobber a browser-tagged
    row's version on accident, and so error paths preserve the prior value.
    """
    prev = conn.execute(
        "SELECT content_hash, unchanged_streak, fail_count, state FROM manifest WHERE url = ?",
        (url,),
    ).fetchone()

    if error is not None:
        new_state = STATE_FAILED
        conn.execute(
            """
            UPDATE manifest SET
                last_attempted_at = ?,
                http_status       = ?,
                fail_count        = fail_count + 1,
                last_error        = ?,
                state             = ?,
                crawler_version   = ?,
                browser_version   = COALESCE(?, browser_version)
            WHERE url = ?
            """,
            (now, http_status, error, new_state, crawler_version, browser_version, url),
        )
        return

    if http_status in (404, 410):
        conn.execute(
            """
            UPDATE manifest SET
                last_attempted_at = ?,
                last_fetched_at   = ?,
                http_status       = ?,
                state             = ?,
                crawler_version   = ?,
                browser_version   = COALESCE(?, browser_version),
                last_error        = NULL
            WHERE url = ?
            """,
            (now, now, http_status, STATE_GONE, crawler_version, browser_version, url),
        )
        return

    if http_status == 304:
        # Server says nothing changed. Touch the freshness check; do not change content.
        conn.execute(
            """
            UPDATE manifest SET
                last_attempted_at = ?,
                last_fetched_at   = ?,
                http_status       = ?,
                http_etag          = COALESCE(?, http_etag),
                http_last_modified = COALESCE(?, http_last_modified),
                unchanged_streak  = unchanged_streak + 1,
                fail_count        = 0,
                state             = 'FRESH',
                crawler_version   = ?,
                browser_version   = COALESCE(?, browser_version),
                last_error        = NULL
            WHERE url = ?
            """,
            (now, now, http_status, http_etag, http_last_modified,
             crawler_version, browser_version, url),
        )
        return

    # 200 OK (or 2xx with body)
    prev_hash = prev["content_hash"] if prev else None
    changed = (content_hash is not None) and (content_hash != prev_hash)
    new_last_changed = now if changed or prev_hash is None else None
    new_streak = 0 if changed else (prev["unchanged_streak"] if prev else 0) + 1

    conn.execute(
        """
        UPDATE manifest SET
            last_attempted_at  = ?,
            last_fetched_at    = ?,
            http_status        = ?,
            http_etag          = ?,
            http_last_modified = ?,
            raw_hash           = ?,
            content_hash       = COALESCE(?, content_hash),
            last_changed_at    = COALESCE(?, last_changed_at),
            unchanged_streak   = ?,
            fail_count         = 0,
            state              = 'FRESH',
            crawler_version    = ?,
            extractor_version  = COALESCE(?, extractor_version),
            normalizer_version = COALESCE(?, normalizer_version),
            browser_version    = COALESCE(?, browser_version),
            last_error         = NULL
        WHERE url = ?
        """,
        (
            now, now, http_status, http_etag, http_last_modified,
            raw_hash, content_hash, new_last_changed, new_streak,
            crawler_version, extractor_version, normalizer_version,
            browser_version, url,
        ),
    )


def record_run_start(conn: sqlite3.Connection, run_id: str, now: str) -> None:
    conn.execute(
        "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
        (run_id, now),
    )


def record_run_phase(conn: sqlite3.Connection, run_id: str, phase: str) -> None:
    conn.execute("UPDATE runs SET phase = ? WHERE run_id = ?", (phase, run_id))


def record_run_end(
    conn: sqlite3.Connection,
    run_id: str,
    now: str,
    status: str,
    counts_json: str,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE runs SET completed_at = ?, status = ?, counts_json = ?, error = ? WHERE run_id = ?",
        (now, status, counts_json, error, run_id),
    )


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["state"]: row["n"]
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM manifest GROUP BY state")
    }


def counts_by_tier(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["tier"]: row["n"]
        for row in conn.execute("SELECT tier, COUNT(*) AS n FROM manifest GROUP BY tier")
    }
