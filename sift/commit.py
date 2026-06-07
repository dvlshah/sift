"""Phase 4: COMMIT — apply extract.log to the manifest in one transaction.

Single writer (the manifest is SQLite WAL; we serialize writes here).
Either every URL in the run lands, or none do — readers never see a half-applied run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from . import CRAWLER_VERSION, paths
from .extract import ExtractResult
from .fetch import FetchResult
from .integrity import CHAIN_GENESIS, with_chain
from .manifest import apply_fetch_result, get_row, now_utc, transaction


def _load_extract_log(extract_log: Path) -> dict[str, ExtractResult]:
    out: dict[str, ExtractResult] = {}
    if not extract_log.exists():
        return out
    with extract_log.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
                er = ExtractResult(**obj)
                out[er.url] = er
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def _load_fetch_log_sync(fetch_log: Path) -> list[FetchResult]:
    out: list[FetchResult] = []
    if not fetch_log.exists():
        return out
    with fetch_log.open() as f:
        for line in f:
            try:
                out.append(FetchResult(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def commit(
    conn: sqlite3.Connection,
    fetch_log: Path,
    extract_log: Path,
    *,
    root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> dict[str, int]:
    """Apply the fetch + extract logs to the manifest atomically.

    For each URL in fetch.log we emit one manifest update. The extract log
    supplies the content_hash/normalizer/extractor versions.

    If `root` is provided, also appends one changelog line per content-hash
    transition (added / changed / gone) to <root>/changelog.jsonl.
    """
    fetches = _load_fetch_log_sync(fetch_log)
    extracts = _load_extract_log(extract_log)
    now = now_utc()

    counts = {"applied": 0, "errors": 0, "304": 0, "gone": 0, "fresh": 0,
              "changelog_added": 0, "changelog_changed": 0, "changelog_gone": 0}
    changelog_entries: list[dict] = []

    with transaction(conn):
        for fr in fetches:
            er = extracts.get(fr.url)
            error: Optional[str] = fr.error
            content_hash: Optional[str] = er.content_hash if er else None
            extractor_v: Optional[str] = er.extractor_version if er else None
            normalizer_v: Optional[str] = er.normalizer_version if er else None
            # If extraction failed despite a successful fetch, surface that as an error.
            if er is not None and not er.ok and fr.error is None and fr.status < 400 and fr.status != 304:
                error = er.reason or "extract-failed"

            # Capture previous state BEFORE the update so we can diff.
            prev = get_row(conn, fr.url)
            prev_hash = prev.content_hash if prev else None
            prev_state = prev.state if prev else "UNSEEN"

            apply_fetch_result(
                conn,
                url=fr.url,
                now=now,
                http_status=fr.status,
                http_etag=fr.etag,
                http_last_modified=fr.last_modified,
                raw_hash=fr.raw_hash,
                content_hash=content_hash,
                crawler_version=CRAWLER_VERSION,
                extractor_version=extractor_v,
                normalizer_version=normalizer_v,
                error=error,
                browser_version=fr.browser_version,
            )
            if error:
                counts["errors"] += 1
            elif fr.status == 304:
                counts["304"] += 1
            elif fr.status in (404, 410):
                counts["gone"] += 1
                if prev_state != "GONE":
                    changelog_entries.append({
                        "ts": now, "url": fr.url, "change_type": "gone",
                        "old_hash": prev_hash, "new_hash": None,
                        "run_id": run_id, "tier": prev.tier if prev else None,
                    })
                    counts["changelog_gone"] += 1
            else:
                counts["fresh"] += 1
                if content_hash and content_hash != prev_hash:
                    ct = "added" if prev_hash is None else "changed"
                    changelog_entries.append({
                        "ts": now, "url": fr.url, "change_type": ct,
                        "old_hash": prev_hash, "new_hash": content_hash,
                        "run_id": run_id, "tier": prev.tier if prev else None,
                    })
                    counts[f"changelog_{ct}"] += 1
            counts["applied"] += 1

    # Append changelog OUTSIDE the SQL transaction (it's a separate file).
    # Done after commit so a crash here doesn't leave the manifest unrecorded —
    # at worst the changelog is missing entries we can rebuild from runs/.
    #
    # Each entry is hash-chained to the previous one: prev_hash + entry_hash
    # fields make the log replay-verifiable. Tampering with any entry (or
    # silently dropping one) breaks the chain at that point.
    if root is not None and changelog_entries:
        cl = paths.changelog_path(root)
        cl.parent.mkdir(parents=True, exist_ok=True)
        # Recover the last entry's hash so we chain correctly across runs.
        prev_hash = _last_entry_hash(cl)
        with cl.open("a") as f:
            for entry in changelog_entries:
                chained = with_chain(prev_hash, entry)
                f.write(json.dumps(chained, separators=(",", ":")) + "\n")
                prev_hash = chained["entry_hash"]
    return counts


def _last_entry_hash(changelog_path: Path) -> str:
    """Return the entry_hash of the last entry in the changelog, or the
    CHAIN_GENESIS sentinel if the file is empty/missing."""
    if not changelog_path.exists():
        return CHAIN_GENESIS
    last_line = ""
    with changelog_path.open() as f:
        for line in f:
            if line.strip():
                last_line = line
    if not last_line:
        return CHAIN_GENESIS
    try:
        last = json.loads(last_line)
    except json.JSONDecodeError:
        return CHAIN_GENESIS
    return last.get("entry_hash") or CHAIN_GENESIS
