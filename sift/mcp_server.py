"""MCP server exposing a published index as task-shaped tools for grep-first agents.

Eight read-only tools, all output-capped:

    snapshot_status — published yes/no, gates, counts, artifact inventory (works pre-publish)
    changed_since   — net added/modified/removed pages since a cursor (the diff feed)
    read_md         — read one md file (with optional offset/limit, optional verify)
    grep_corpus     — regex search over md/ (returns file:line:context)
    glob_corpus     — list md files matching a glob (e.g. forms-and-instructions/**/2025*)
    list_dir        — directory listing under current/ or current/md/
    query_manifest  — read-only SELECT against manifest.db
    read_facts      — read one facts/*.json file (with schema validation hint)

Two write/status tools are exposed ONLY when launched with --enable-index
(off by default, so the server is read-only unless explicitly opted in):

    index_url       — seed URL(s) on an allow-listed host + trigger an incremental crawl
    index_status    — poll a background index job by run_id (reads the durable runs table)

Invocation:

    python -m sift.mcp_server --root /path/to/index               # read-only
    python -m sift.mcp_server --root /path/to/index --enable-index # allow writes

The server points at <root>/current/ (the published snapshot symlink). If
current/ doesn't exist yet (no successful publish), read tools return helpful
errors directing the user to run `sift publish`.

Design notes (per claude-agent-sdk tool best practices):
- Names verb-first; descriptions cover what / when / when-not / returns / preconditions
- Every input field has a description
- Pure reads use readOnlyHint
- Output capped (~20K chars/read, 200 grep matches, 500 glob results) with an
  escape-hatch (offset/limit) so the agent can ask for more if needed
- Errors return isError=True with a recovery hint, not raw stack traces
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import click
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import __version__, paths
from ._io import parse_frontmatter, read_snapshot, split_frontmatter
from .manifest import open_manifest_ro
from .registry import (
    IndexRegistry,
    RegistryCache,
    latest_run_dir,
)

# ---- Output budgets ---------------------------------------------------------
# Per-tool defaults. Agents can override via offset/limit; the harness handles
# LRU and per-file truncation, but the server still caps so a stray pattern
# can't dump a megabyte into context.

MAX_READ_CHARS = 20_000          # ~5K tokens
MAX_GREP_MATCHES = 200
MAX_GREP_LINE_CHARS = 240
MAX_GLOB_RESULTS = 500
MAX_LS_ENTRIES = 500
MAX_QUERY_ROWS = 500
MAX_CHANGED_ENTRIES = 500       # per group (added/modified/removed) in changed_since


# ---- Path resolution --------------------------------------------------------

def _published_run_dir(root: Path) -> Optional[Path]:
    """The genuinely-published run for this index, or None.

    Thin alias for the single canonical resolver in ``paths`` (the publish
    gate signal lives there so registry discovery, the content tools, and
    index_status can never disagree). A run is published iff ``current``
    points at it; a gate-degraded run that never flipped ``current``
    resolves to None — closing the provenance hole where the newest run
    was served as published regardless of gates.
    """
    return paths.published_run_dir(root)


def _resolve_root(root: Path) -> tuple[Path, bool]:
    """Return (resolved_path, is_published_snapshot).

    is_published_snapshot is True ONLY when a run is genuinely published
    (``current`` points at it — see ``_published_run_dir``). A degraded /
    never-published run resolves to (root, False) so the content tools'
    publish guard fires instead of silently serving ungated content.
    """
    published = _published_run_dir(root)
    if published is not None:
        return published, True
    return root.resolve(), False


_NO_SNAPSHOT_HINT = (
    "No published snapshot at <root>/current/. "
    "The index hasn't completed a successful publish yet. "
    "Call snapshot_status for details, then run `sift publish "
    "--root <root> --run-id <id>` once the pipeline finishes and all gates pass."
)


def _require_published(is_published: bool) -> Optional[mcp_types.CallToolResult]:
    """Guard helper: if there's no published snapshot, return an isError
    result directing the agent to call snapshot_status. Otherwise None."""
    if not is_published:
        return _err(_NO_SNAPSHOT_HINT)
    return None


def _safe_path(root: Path, rel: str) -> Optional[Path]:
    """Resolve `rel` relative to `root` and ensure it stays inside the root.
    Returns None on traversal attempts (..) escaping the root."""
    try:
        p = (root / rel).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p


def _truncate(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[truncated at {limit} chars; full size {len(text)} chars. "
        + f"Use offset/limit to read more of this {what}.]"
    )


def _err(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        isError=True,
    )


def _ok(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        isError=False,
    )


# ---- Tool implementations ---------------------------------------------------

def tool_read_md(
    root: Path,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_CHARS,
    verify: bool = False,
    index_root: Optional[Path] = None,
) -> mcp_types.CallToolResult:
    p = _safe_path(root, path)
    if p is None:
        return _err(
            f"Path '{path}' escapes the index root. "
            "Paths are relative to current/ (e.g. md/individuals-and-families/your-tax-return.md). "
            "Use list_dir or glob_corpus to discover valid paths."
        )
    if not p.exists():
        return _err(
            f"No file at '{path}'. "
            "Try `glob_corpus` with a partial path, or `query_manifest` to look up by URL."
        )
    if p.is_dir():
        return _err(
            f"'{path}' is a directory. Use list_dir to enumerate, or read INDEX.md if present."
        )
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _err(f"Could not read '{path}': {e}")

    # Optional integrity verification: recompute the content hash from the
    # body and compare to the frontmatter's claimed content_hash. Catches
    # any out-of-band modification of the file since publish.
    if verify:
        fm_text, body = split_frontmatter(text)
        if fm_text is None:
            return _err(f"verify failed: no frontmatter in '{path}'")
        stored = parse_frontmatter(fm_text).get("content_hash", "").removeprefix("sha256:")
        if not stored:
            return _err(f"verify failed: frontmatter missing content_hash in '{path}'")
        # The content_hash is profile-dependent (normalize_for_hash strips
        # the active profile's dynamic_patterns). Re-hash under the index's
        # OWN profile, not the package default — otherwise every non-ATO
        # page falsely reports INTEGRITY FAILURE. index_root is None only on
        # the legacy single-root call path that never set a non-default
        # profile, so skipping activation there preserves prior behavior.
        from sift.extract import hash_normalized_body
        if index_root is not None:
            from sift.index_profile import apply_index_profile
            try:
                apply_index_profile(index_root)
            except Exception:
                pass  # fall back to active profile; verify still runs
        recomputed = hash_normalized_body(body)
        if recomputed != stored:
            return _err(
                f"INTEGRITY FAILURE for '{path}':\n"
                f"  stored content_hash:     sha256:{stored}\n"
                f"  recomputed content_hash: sha256:{recomputed}\n"
                f"  The file's body has been modified since publish, OR the "
                f"normalizer version changed. Treat the file as untrusted."
            )
        # Prepend the verification result to the output so the agent has
        # explicit confirmation rather than silent success.
        text = (
            f"[verify=ok content_hash=sha256:{stored[:16]}... "
            f"normalizer_matches=True]\n" + text
        )

    if offset:
        text = text[offset:]
    text = _truncate(text, limit, "file")
    return _ok(text)


def tool_grep_corpus(
    root: Path,
    pattern: str,
    path: str = "md/",
    ignore_case: bool = False,
    files_only: bool = False,
    context: int = 0,
    max_matches: int = MAX_GREP_MATCHES,
) -> mcp_types.CallToolResult:
    p = _safe_path(root, path)
    if p is None:
        return _err(f"Path '{path}' escapes the index root.")
    if not p.exists():
        return _err(
            f"No such path '{path}'. Common starting points: md/, sections/, facts/."
        )
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return _err(f"Invalid regex '{pattern}': {e}")

    if p.is_file():
        files = [p]
    else:
        # Default to .md files when scanning a tree; agent can override by
        # passing a more specific path (e.g. routes.tsv).
        ext = ".md" if p.is_dir() else ""
        files = [f for f in p.rglob(f"*{ext}") if f.is_file()]

    matches: list[str] = []
    seen_files: set[Path] = set()
    for f in files:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if rx.search(line):
                rel = f.relative_to(root)
                if files_only:
                    if f not in seen_files:
                        seen_files.add(f)
                        matches.append(str(rel))
                else:
                    snippet = line.rstrip("\n")[:MAX_GREP_LINE_CHARS]
                    matches.append(f"{rel}:{i + 1}:{snippet}")
                    if context > 0:
                        for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                            if j == i:
                                continue
                            matches.append(f"{rel}-{j + 1}-{lines[j].rstrip()[:MAX_GREP_LINE_CHARS]}")
                if len(matches) >= max_matches:
                    break
        if len(matches) >= max_matches:
            break

    if not matches:
        return _ok(f"No matches for /{pattern}/ in {path}.")

    truncated_note = ""
    if len(matches) >= max_matches:
        truncated_note = (
            f"\n\n[stopped at {max_matches} matches. "
            "Refine the pattern or use files_only=true for a file-level view.]"
        )
    return _ok("\n".join(matches) + truncated_note)


def tool_glob_corpus(
    root: Path,
    pattern: str,
    max_results: int = MAX_GLOB_RESULTS,
) -> mcp_types.CallToolResult:
    # Use pathlib.Path.glob from the root.
    try:
        results = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if fnmatch(str(rel), pattern):
                results.append(str(rel))
                if len(results) >= max_results:
                    break
    except OSError as e:
        return _err(f"glob failed: {e}")
    if not results:
        return _ok(
            f"No files match '{pattern}'. "
            "Glob is fnmatch-style: use '*' for any segment, e.g. "
            "'md/individuals-and-families/*.md' or 'facts/**/*2025*.json'."
        )
    note = ""
    if len(results) >= max_results:
        note = f"\n\n[stopped at {max_results} results; narrow the pattern for more.]"
    return _ok("\n".join(sorted(results)) + note)


def tool_list_dir(root: Path, path: str = ".") -> mcp_types.CallToolResult:
    p = _safe_path(root, path)
    if p is None or not p.exists():
        return _err(f"No directory at '{path}'. Try '.' for the root.")
    if not p.is_dir():
        return _err(f"'{path}' is not a directory. Use read_md to read it.")
    entries = []
    try:
        for entry in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)):
            tag = "d" if entry.is_dir() else "f"
            size = "-" if entry.is_dir() else str(entry.stat().st_size)
            entries.append(f"{tag} {size:>10} {entry.name}")
            if len(entries) >= MAX_LS_ENTRIES:
                break
    except OSError as e:
        return _err(f"Could not list '{path}': {e}")
    return _ok("\n".join(entries) if entries else "[empty]")


def tool_query_manifest(
    root: Path,
    sql: str,
    *,
    index_root: Optional[Path] = None,
    max_rows: int = MAX_QUERY_ROWS,
) -> mcp_types.CallToolResult:
    s = sql.strip().lower()
    if not s.startswith(("select", "with")):
        return _err(
            "Only SELECT (or WITH...SELECT) queries are allowed. "
            "Schema: SELECT name FROM sqlite_master WHERE type='table'."
        )
    # The manifest lives at <index_root>/manifest.db; it's NOT inside the
    # per-run snapshot. Walk up from `root` (which is the resolved current/
    # snapshot dir) until we find it, capped at 3 levels.
    candidates = []
    if index_root is not None:
        candidates.append(index_root / "manifest.db")
    candidates.append(root / "manifest.db")
    p = root
    for _ in range(3):
        p = p.parent
        candidates.append(p / "manifest.db")
    db = next((c for c in candidates if c.exists()), None)
    if db is None:
        return _err(
            "No manifest.db reachable from this snapshot. "
            "Has the index been initialized with `sift init`?"
        )
    # try/finally so the read-only connection is always closed — in a
    # long-lived MCP server, a leaked fd per query_manifest call adds up.
    conn = open_manifest_ro(db)
    if conn is None:
        return _err(f"Query failed: could not open manifest at {db}")
    try:
        # fetchmany(max_rows+1), NOT list(...): a recursive-CTE or cross-join
        # bomb would otherwise materialize the ENTIRE result set into this
        # long-lived server before the row cap. sqlite generates rows lazily,
        # so fetching just one past the cap bounds both memory and time, and
        # the extra row tells us whether the result was actually truncated.
        rows = conn.execute(sql).fetchmany(max_rows + 1)
    except sqlite3.Error as e:
        return _err(f"Query failed: {e}")
    finally:
        conn.close()
    if not rows:
        return _ok("[no rows]")
    truncated = len(rows) > max_rows
    out = [dict(r) for r in rows[:max_rows]]
    body = json.dumps(out, indent=2, default=str)
    note = f"\n\n[truncated at {max_rows} rows]" if truncated else ""
    return _ok(body + note)


def tool_list_indexes(
    registry: IndexRegistry,
    *,
    enable_index: bool,
    job_state: _RegistryJobState,
) -> mcp_types.CallToolResult:
    """Return the registry of available sift indexes the agent can query
    + write metadata.

    The agent calls this FIRST in a multi-index session to pick a corpus
    before grepping AND to understand where it can expand coverage.
    Each index exposes:

      * slug / description / domain / tags / page_count / last_published
        — the discovery + selection signal.
      * accepts_writes / allowed_hosts — sift.toml capability. Tells the
        agent whether the per-index allow-list permits ``index_url``.
      * writeable — the AND of accepts_writes AND the server's
        ``--enable-index`` flag. This is the load-bearing boolean the
        agent should check before calling ``index_url``.
      * unseen_count — URLs the manifest knows about but hasn't fetched.
        Nonzero is the legibility cue for "ask me to backfill this index."
      * recent_runs — last few rows from the manifest's runs table,
        newest first. Distinguishes actively-crawled vs stale corpora.
      * active_run — set if a crawl is in flight RIGHT NOW (slug-keyed
        from in-memory state, so the agent can poll without guessing
        the run_id).
    """
    if not registry.indexes:
        return _ok(json.dumps({
            "indexes": [],
            "is_multi": registry.is_multi,
            "parent_path": str(registry.parent_path),
            "write_enabled": enable_index,
            "note": ("No sift indexes discovered under this path. The MCP "
                     "server was started against a parent directory with no "
                     "sift-shaped subdirectories. Run `sift init` + a full "
                     "pipeline before re-launching."),
        }, indent=2, default=str))
    indexes: list[dict] = []
    for d in registry.indexes:
        item = d.to_dict()
        # The agent should only check ONE boolean before deciding to
        # call index_url — fold both the per-slug capability and the
        # server-wide enablement into ``writeable``.
        item["writeable"] = bool(enable_index and d.accepts_writes)
        st = job_state.per_slug.get(d.slug) if registry.is_multi else (
            job_state.per_slug.get(_SINGLE_MODE_SLUG)
        )
        if st is not None and st.active():
            item["active_run"] = {"run_id": st.run_id, "phase": st.phase}
        indexes.append(item)
    next_step = (
        "Pick a slug from the indexes list, then call grep_corpus / "
        "read_md / list_dir with `index=<slug>`. For cross-corpus "
        "search across all indexes, pass `index=\"*\"` (slower, use "
        "sparingly). To extend coverage on a writeable index, call "
        "index_url with `index=<slug>` and the URLs."
        if registry.is_multi
        else "Single-index mode: tools work without an `index` argument."
    )
    return _ok(json.dumps({
        "indexes": indexes,
        "is_multi": registry.is_multi,
        "parent_path": str(registry.parent_path),
        "write_enabled": enable_index,
        "concurrent_crawl_cap": job_state.max_concurrent,
        "active_crawls": len(job_state.active_slugs()),
        "next_step": next_step,
    }, indent=2, default=str))


def tool_snapshot_status(index_root: Path) -> mcp_types.CallToolResult:
    """Report the published-snapshot state for the index root. Works whether
    or not a snapshot exists — designed as the first call an agent makes
    to confirm there's something to read.

    Reads from <index_root>/current/snapshot.json (if symlinked) plus the
    manifest for live counts. Never falls back, never errors on missing
    state — it's a status probe.
    """
    out: dict[str, Any] = {"index_root": str(index_root)}

    # A run is published iff `current` points at it (see _published_run_dir).
    # We do NOT treat a newest-but-unpublished (e.g. gate-degraded) run as
    # published — that was a provenance hole.
    resolved = _published_run_dir(index_root)

    if resolved is None:
        out["published"] = False
        # Diagnostic: surface a degraded/unpublished run if one exists, so
        # the agent knows a run RAN but didn't pass gates (vs. nothing ran).
        latest = latest_run_dir(index_root)
        if latest is not None:
            out["reason"] = (
                "a run exists but is NOT published (current/ does not point "
                "at it — it likely failed a publish gate)"
            )
            out["unpublished_latest_run"] = latest.name
            s = read_snapshot(latest)
            if s:
                out["unpublished_latest_status"] = s.get("status")
                out["unpublished_failed_gates"] = [
                    g.get("name") for g in (s.get("gates") or [])
                    if not g.get("passed")
                ]
        else:
            out["reason"] = "no run has been executed for this index yet"
        out["next_step"] = (
            "Run a full pipeline: sift init --root <root>; "
            "sift seed ...; sift run --root <root>. "
            "If a run completes but current/ stays unpointed, the publish "
            "gates degraded it — inspect unpublished_failed_gates."
        )
        return _ok(json.dumps(out, indent=2))

    out["published"] = True
    out["current_path"] = str(resolved)
    out["run_id"] = resolved.name

    # Snapshot details (gates, versions, counts) if snapshot.json present
    snap_path = resolved / "snapshot.json"
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text())
            out["snapshot"] = {
                "run_id":         snap.get("run_id"),
                "status":         snap.get("status"),
                "completed_at":   snap.get("completed_at"),
                "expected_urls":  snap.get("expected_urls"),
                "counts_by_state": snap.get("counts_by_state"),
                "counts_by_tier": snap.get("counts_by_tier"),
                "versions":       snap.get("versions"),
                "gates":          snap.get("gates"),
            }
        except (json.JSONDecodeError, OSError) as e:
            out["snapshot_parse_error"] = str(e)

    # Quick artifact inventory so the agent knows what surfaces exist
    inventory = {}
    for kind, glob in (("md_files", "md/**/*.md"),
                       ("section_indexes", "sections/*/INDEX.md"),
                       ("facts_files", "facts/**/*.json"),
                       ("by_guide_rollups", "artifacts/by_guide/*.md")):
        try:
            inventory[kind] = sum(1 for _ in resolved.glob(glob))
        except OSError:
            inventory[kind] = -1
    out["artifact_inventory"] = inventory

    # Hint to the agent on next reads
    out["entry_points"] = [
        "read_md INDEX.md          # always-loaded pointer table",
        "list_dir sections/        # drill-down by section",
        "list_dir facts/           # atomic JSON facts",
        "read_md routes.tsv        # url -> file map (grep-friendly)",
    ]
    return _ok(json.dumps(out, indent=2))


# ---- changed_since: the temporal diff primitive (the "live feed") -----------
# Reads the append-only, hash-chained <root>/changelog.jsonl and returns the
# NET content delta between a caller-held cursor and the CURRENT PUBLISHED
# snapshot. This is the read head over history the pipeline already records:
# an agent stores the run_id it last saw (from snapshot_status), then pulls
# only what changed instead of re-reading the whole corpus.
#
# The upper boundary is always the *published* snapshot (not the newest run on
# disk), so the delta matches exactly what read_md will serve — transitions
# from a later degraded/unpublished run have ts > the published completed_at
# and are excluded. The changelog itself lives at the INDEX ROOT (one level
# above current/), which is why this tool takes index_root, like snapshot_status
# and unlike the current/-scoped content tools.

_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _run_completed_at(index_root: Path, run_id: str) -> Optional[str]:
    """The completed_at (else started_at) timestamp for a run, or None.

    Prefers the run's snapshot.json (written for published AND degraded runs);
    falls back to the durable runs table. Turns a run_id cursor into a
    changelog timestamp boundary.
    """
    snap = read_snapshot(paths.run_dir(index_root, run_id))
    ts = snap.get("completed_at") or snap.get("started_at")
    if ts:
        return ts
    conn = open_manifest_ro(paths.manifest_path(index_root))
    if conn is None:
        return None
    try:
        r = conn.execute(
            "SELECT completed_at, started_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if r is not None:
            return r["completed_at"] or r["started_at"]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None


def _resolve_since(
    index_root: Path, since: str
) -> tuple[Optional[str], Optional[str]]:
    """Resolve the ``since`` cursor to a changelog timestamp boundary.

    ``since`` may be a run_id (resolved to that run's completed_at) or a bare
    ISO-8601 UTC timestamp (used as-is). Returns (ts, None) on success or
    (None, error) when it's neither a known run nor a valid timestamp.
    """
    s = since.strip()
    ts = _run_completed_at(index_root, s)
    if ts:
        return ts, None
    if _ISO_TS_RE.match(s):
        return s, None
    return None, (
        f"`since` value '{since}' is neither a known run_id nor an ISO-8601 "
        "UTC timestamp (YYYY-MM-DDTHH:MM:SSZ). Pass the run_id from "
        "snapshot_status (the snapshot you last read), or a timestamp."
    )


def tool_changed_since(
    index_root: Path,
    since: str,
    *,
    path_prefix: Optional[str] = None,
    tier: Optional[str] = None,
    limit: int = MAX_CHANGED_ENTRIES,
    offset: int = 0,
) -> mcp_types.CallToolResult:
    """Return the net added/modified/removed delta between ``since`` and the
    current published snapshot, read from the hash-chained changelog."""
    published = paths.published_run_dir(index_root)
    if published is None:
        return _err(
            "changed_since needs a published baseline, but this index has no "
            "published snapshot (current/ is unset). Call snapshot_status."
        )
    pub_snap = read_snapshot(published)
    upper_run = published.name
    upper_ts = pub_snap.get("completed_at") or _run_completed_at(index_root, upper_run)
    if not upper_ts:
        return _err(
            f"Published run {upper_run} has no resolvable completed_at; "
            "cannot bound the diff window."
        )

    since_ts, err = _resolve_since(index_root, since)
    if err is not None:
        return _err(err)
    assert since_ts is not None

    integ = pub_snap.get("integrity") or {}
    base = {
        "from": {"cursor": since, "resolved_ts": since_ts},
        "to": {
            "run_id": upper_run,
            "completed_at": upper_ts,
            "changelog_total_entries": integ.get("changelog_total_entries"),
            "merkle_root": integ.get("merkle_root"),
        },
        "cursor": upper_run,
    }

    # Cursor at/after the current published snapshot → you're up to date.
    if since_ts >= upper_ts:
        return _ok(json.dumps({
            **base,
            "counts": {"added": 0, "modified": 0, "removed": 0, "changed_urls": 0},
            "added": [], "modified": [], "removed": [],
            "up_to_date": True,
            "note": (
                "Your cursor is at or after the current published snapshot — no "
                "newer published changes. Re-check after the next publish."
            ),
        }, indent=2, default=str))

    cl = paths.changelog_path(index_root)
    if not cl.exists():
        return _ok(json.dumps({
            **base,
            "counts": {"added": 0, "modified": 0, "removed": 0, "changed_urls": 0},
            "added": [], "modified": [], "removed": [],
            "note": "No changelog.jsonl yet — the index has recorded no transitions.",
        }, indent=2, default=str))

    # Collapse every in-window transition to a NET per-URL delta. Window is
    # (since_ts, upper_ts]: strictly after the cursor, up to and including the
    # published snapshot. A URL's old_hash is the old_hash of its FIRST
    # in-window entry (its state at the cursor); its new_hash is the new_hash
    # of its LAST in-window entry (its state at the published snapshot). This
    # collapses a page that changed several times, and drops it entirely if the
    # net effect is a no-op (old == new).
    net: dict[str, dict] = {}
    chain_tip: Optional[str] = None
    malformed = 0
    with cl.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            ts = o.get("ts")
            url = o.get("url")
            if ts is None or url is None:
                continue
            if ts <= since_ts or ts > upper_ts:
                continue
            chain_tip = o.get("entry_hash") or chain_tip
            cur = net.get(url)
            if cur is None:
                net[url] = {
                    "first_old": o.get("old_hash"),
                    "new_hash": o.get("new_hash"),
                    "tier": o.get("tier"),
                    "ts": ts,
                    "type": o.get("change_type"),
                    "entry_hash": o.get("entry_hash"),
                }
            else:
                cur["new_hash"] = o.get("new_hash")
                cur["ts"] = ts
                cur["type"] = o.get("change_type")
                cur["entry_hash"] = o.get("entry_hash")
                cur["tier"] = o.get("tier")

    added: list[dict] = []
    modified: list[dict] = []
    removed: list[dict] = []
    for url, d in net.items():
        if path_prefix and not url.startswith(path_prefix):
            continue
        if tier and d.get("tier") != tier:
            continue
        old, new = d["first_old"], d["new_hash"]
        if old == new:
            continue  # net no-op across the window
        rec = {"url": url, "ts": d["ts"], "tier": d.get("tier"),
               "entry_hash": d.get("entry_hash")}
        if d.get("type") == "gone" or new is None:
            removed.append({**rec, "old_hash": old})
        elif old is None:
            added.append({**rec, "new_hash": new})
        else:
            modified.append({**rec, "old_hash": old, "new_hash": new})

    # Newest-first within each group; agents usually want recent churn first.
    for lst in (added, modified, removed):
        lst.sort(key=lambda r: r["ts"], reverse=True)

    def _page(lst: list[dict]) -> tuple[list[dict], bool]:
        return lst[offset: offset + limit], len(lst) > offset + limit

    a_pg, a_tr = _page(added)
    m_pg, m_tr = _page(modified)
    r_pg, r_tr = _page(removed)
    truncated = a_tr or m_tr or r_tr

    out: dict[str, Any] = {
        **base,
        "counts": {
            "added": len(added),
            "modified": len(modified),
            "removed": len(removed),
            "changed_urls": len(added) + len(modified) + len(removed),
        },
        "added": a_pg,
        "modified": m_pg,
        "removed": r_pg,
        "chain_tip_entry_hash": chain_tip,
        "truncated": truncated,
        "provenance": (
            "Each item is a leaf of the append-only, hash-chained changelog; "
            "this delta is a contiguous segment of that chain. Run "
            "`sift verify-changelog` to validate the chain, and "
            "read_md(verify=true) to confirm a page body against new_hash."
        ),
    }
    if malformed:
        out["malformed_changelog_lines"] = malformed
    if truncated:
        out["truncation_hint"] = (
            f"Showing up to {limit} per group (offset {offset}). Use a larger "
            "limit, page with offset, or narrow with path_prefix / tier."
        )
    return _ok(json.dumps(out, indent=2, default=str))


def tool_read_facts(root: Path, path: str) -> mcp_types.CallToolResult:
    p = _safe_path(root, path)
    if p is None or not p.exists():
        return _err(
            f"No facts file at '{path}'. "
            "Browse with `list_dir facts/` or `glob_corpus 'facts/**/*.json'`. "
            "Schemas are at facts/schemas/."
        )
    if p.is_dir():
        return _err(f"'{path}' is a directory; list_dir to see contents.")
    try:
        text = p.read_text(encoding="utf-8")
        obj = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"Could not parse JSON at '{path}': {e}")
    # Surface the schema id prominently so the agent can validate against it.
    schema = obj.get("$schema", "<no $schema>")
    src = obj.get("source_url", "<no source_url>")
    hsh = obj.get("content_hash", "<no content_hash>")
    header = (
        f"# {path}\n"
        f"schema: {schema}\n"
        f"source_url: {src}\n"
        f"content_hash: {hsh}\n"
        f"---\n"
    )
    body = json.dumps(obj, indent=2, sort_keys=True)
    return _ok(header + _truncate(body, MAX_READ_CHARS, "JSON file"))


# ---- Write capability: async index jobs (opt-in via --enable-index) ---------
# The read tools above never mutate anything. index_url is the one exception,
# gated behind a server flag so the default deployment keeps the "all read-only"
# guarantee. Security model: the agent may only crawl hosts the operator already
# allow-listed in config (seed.host_allow); everything else is refused.

MAX_INDEX_URLS = 20


def _new_index_run_id() -> str:
    """A run_id for an index-triggered crawl. Timestamp + short random suffix so
    two calls in the same second can't collide on the runs-table primary key,
    and an '-idx' marker so these runs are distinguishable from scheduled ones."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-idx{uuid.uuid4().hex[:6]}"


def _host_allowed(url: str, allow: set[str]) -> bool:
    """True iff `url` is http(s) and its connect-host is in the allow-list.

    The host is taken as the segment after any userinfo and before any port —
    i.e. what a client actually connects to. This defeats the
    'https://allowed.com@evil.com/' confusion where the real host is evil.com.
    """
    try:
        u = urlparse(url)
    except (ValueError, TypeError):
        return False
    if u.scheme not in ("http", "https"):
        return False
    netloc = (u.netloc or "").lower()
    host = netloc.split("@")[-1].split(":")[0]
    return bool(host) and host in allow


_SINGLE_MODE_SLUG = "__single__"
# Default cap on concurrent index_url crawls across ALL slugs. Beyond
# this, index_url returns a "too many concurrent crawls" error rather
# than queueing — MCP's request/response shape doesn't model a wait
# well, and queueing risks tying up a thread that can't be cancelled
# without an explicit cancel_index tool (deferred to v2).
_DEFAULT_MAX_CONCURRENT_CRAWLS = 4


@dataclass
class _IndexJobState:
    """Tracks one in-flight index job for ONE slug.

    Durable progress lives in the runs table (read by ``tool_index_status``);
    this object only holds the concurrency guard and the brief pre-run window
    (while ``sift seed`` runs, before ``sift run`` has recorded a runs row).
    A child crawl can't outlive the server, so losing this on restart is
    correct — completed runs are still queryable from the manifest by
    run_id.
    """
    run_id: Optional[str] = None
    phase: str = "idle"          # idle | seeding | running | failed
    error: Optional[str] = None
    task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)

    def active(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class _RegistryJobState:
    """Per-server registry of per-slug job state + cross-slug concurrency
    cap.

    Lifecycle:
      * Per-slug ``_IndexJobState`` entries are created lazily on first
        write to the slug.
      * The global cap is checked synchronously inside ``index_url`` —
        no queuing. Over-cap calls return an error pointing at the
        active slugs so the agent can wait on `index_status` for them.
      * Single-index mode reuses a single sentinel slug (``__single__``)
        so the state machine is uniform across modes.

    The cap defends the host: 4 concurrent crawls is roughly
    ``crawl.concurrency × 4`` simultaneous HTTP connections (per
    cfg). Operators with bigger boxes can raise the cap via
    ``--max-concurrent-crawls`` at server start.
    """
    per_slug: dict[str, _IndexJobState] = field(default_factory=dict)
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT_CRAWLS

    def for_slug(self, slug: str) -> _IndexJobState:
        st = self.per_slug.get(slug)
        if st is None:
            st = _IndexJobState()
            self.per_slug[slug] = st
        return st

    def active_slugs(self) -> list[str]:
        return [s for s, st in self.per_slug.items() if st.active()]

    def is_at_capacity(self) -> bool:
        return len(self.active_slugs()) >= self.max_concurrent


async def _spawn(cmd: list[str]) -> tuple[Optional[int], str]:
    """Run a subprocess to completion, capturing combined output.

    Returns ``(returncode, output_tail)``. Never raises on non-zero exit.

    Cancellation semantics: if the awaiting task is cancelled (server
    restart, client disconnect, explicit cancel_index in a future
    revision), the child process is terminated — SIGTERM, then SIGKILL
    after a 5-second grace. Without this, ``sift run`` subprocesses
    would outlive the parent MCP server and leak resources.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace")
    except asyncio.CancelledError:
        # Best-effort graceful shutdown: TERM, brief wait, then KILL.
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        raise


async def _run_index_job(
    state: _IndexJobState,
    index_root: Path,
    run_id: str,
    urls: list[str],
    config_path: Optional[Path],
) -> None:
    """Seed the URLs then run the pipeline as a subprocess chain. Updates
    the in-memory state for the pre-run window; the run's phases + final
    status are recorded by ``sift run`` itself in the runs table under
    our run_id.

    Targeted-write semantics: this writes the URLs to BOTH a seed-json
    file (consumed by ``sift seed --from-json``) and an only-urls file
    (consumed by ``sift run --only-urls``). Without the second, ``sift
    run``'s planner would treat every UNSEEN row in the manifest as a
    fetch candidate — and for a backfill on a large index that turns a
    "fetch one page" call into a "fetch thousands" expansion. The
    only-urls file restores the targeting agents expect.

    Cancellation: the ``_spawn`` helper terminates the child subprocess
    on task cancel (TERM then KILL after grace), so an MCP server
    restart doesn't leak ``sift run`` subprocesses.
    """
    sift_bin = shutil.which("sift")
    if sift_bin is None:
        state.phase = "failed"
        state.error = "the 'sift' CLI is not on PATH for the server process"
        return

    tmpdir = Path(tempfile.mkdtemp(prefix="sift-idx-"))
    seed_file = tmpdir / "seed.json"
    only_urls_file = tmpdir / "only_urls.txt"
    try:
        # URLs go into files read by `sift seed --from-json` /
        # `sift run --only-urls` — never onto a command line, so they
        # can't be interpreted as args or shell tokens.
        seed_file.write_text(json.dumps({"links": [{"url": u} for u in urls]}))
        only_urls_file.write_text("\n".join(urls) + "\n")
        base = ["--root", str(index_root)]
        cfg_args = ["--config", str(config_path)] if config_path else []

        state.phase = "seeding"
        rc, tail = await _spawn([sift_bin, "seed", *base, *cfg_args,
                                 "--from-json", str(seed_file)])
        if rc != 0:
            state.phase = "failed"
            state.error = f"seed failed (rc={rc}): {tail[-400:]}"
            return

        state.phase = "running"
        rc, tail = await _spawn([
            sift_bin, "run", *base, *cfg_args,
            "--run-id", run_id,
            "--only-urls", str(only_urls_file),
        ])
        # `sift run` exit codes: 0 published, 2 degraded gate (both ran
        # fine), 1/other = pipeline error. The runs row carries the
        # authoritative status; we only flag a hard crash that may have
        # left no terminal row.
        if rc not in (0, 2):
            state.phase = "failed"
            state.error = f"run failed (rc={rc}): {tail[-400:]}"
        else:
            state.phase = "idle"
    except asyncio.CancelledError:
        # Cancellation is the operator-initiated path (server restart,
        # future cancel_index call). The state is left as it was at the
        # last completed phase — durable run rows in the manifest still
        # reflect what made it through.
        state.phase = "cancelled"
        state.error = "job cancelled before completion"
        raise
    except Exception as e:  # never let a background task die silently
        state.phase = "failed"
        state.error = f"index job crashed: {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def tool_index_status(
    index_root: Path, run_id: str, state: _IndexJobState
) -> mcp_types.CallToolResult:
    """Report an index job's state. Prefers the durable runs table; falls back
    to in-memory state for the brief pre-run (seeding) window."""
    db = index_root / "manifest.db"
    row: Optional[dict] = None
    conn = open_manifest_ro(db)
    if conn is not None:
        try:
            r = conn.execute(
                "SELECT run_id, started_at, completed_at, phase, status, "
                "counts_json, error FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            row = dict(r) if r else None
        except sqlite3.Error as e:
            return _err(f"index_status query failed: {e}")
        finally:
            conn.close()

    if row is not None:
        # Use the canonical resolver, not a raw `current.resolve()`: the
        # latter reports published_as_current=False for a genuinely-
        # published run behind a non-resolving relative symlink (the macOS
        # /tmp overshoot), making the agent think fresh pages aren't live.
        resolved = _published_run_dir(index_root)
        published = bool(resolved is not None and resolved.name == run_id)
        out: dict[str, Any] = {
            "run_id": run_id,
            "status": row.get("status"),
            "phase": row.get("phase"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "published_as_current": published,
        }
        if row.get("counts_json"):
            try:
                out["counts"] = json.loads(row["counts_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        if row.get("error"):
            out["error"] = row["error"]
        status = row.get("status")
        if status == "succeeded":
            out["next_step"] = ("Published. Call snapshot_status to confirm, "
                                "then read the new page(s) with read_md.")
        elif status == "degraded":
            out["next_step"] = ("Pipeline ran but a publish gate degraded; "
                                "current/ did NOT flip. New content is fetched "
                                "but not published — inspect 'counts'/gates.")
        elif status == "running":
            out["next_step"] = (f"In progress (phase={row.get('phase')}). "
                                 "Poll index_status again shortly.")
        return _ok(json.dumps(out, indent=2, default=str))

    # No runs row yet — either still seeding, the seed step failed, or unknown.
    if state.run_id == run_id:
        if state.phase == "failed":
            return _ok(json.dumps(
                {"run_id": run_id, "status": "failed", "phase": "seed",
                 "error": state.error}, indent=2))
        return _ok(json.dumps(
            {"run_id": run_id, "status": state.phase,
             "note": "seeding underway; no run recorded yet. Poll again shortly."},
            indent=2))
    return _err(
        f"Unknown run_id '{run_id}'. In-progress jobs don't survive a server "
        "restart (the child crawl is killed with the server); completed runs "
        "persist in the manifest. Start a fresh index_url if needed."
    )


@dataclass(frozen=True)
class _WriteTarget:
    """Resolved write destination for one ``index_url`` / ``index_status``
    call. Built by ``_resolve_write_target``; encapsulates the per-slug
    index root, its allow-list, the config path to pass through to
    ``sift seed``/``sift run``, and the slug used to key the job state."""
    slug: str
    root: Path
    allow: frozenset[str]
    config_path: Optional[Path]


def _resolve_write_target(
    arguments: dict[str, Any],
    *,
    registry: IndexRegistry,
    legacy_root: Path,
    legacy_allow: set[str],
    legacy_config_path: Optional[Path],
) -> tuple[Optional[_WriteTarget], Optional[mcp_types.CallToolResult]]:
    """Pick the sift root + config to use for one write call.

    Single-index mode: returns the legacy root + the operator's
    ``--config`` path (or sift.toml under the root).

    Multi-index mode: requires an ``index`` argument; resolves to that
    slug's root and reads its sift.toml for the per-slug allow-list.
    Returns an error result if the slug is unknown or has no usable
    allow-list (i.e. its sift.toml lacks ``[seed].host_allow``).
    """
    if not registry.is_multi:
        return _WriteTarget(
            slug=_SINGLE_MODE_SLUG,
            root=legacy_root,
            allow=frozenset(h.lower() for h in legacy_allow),
            config_path=legacy_config_path,
        ), None

    slug = arguments.get("index")
    if not isinstance(slug, str) or not slug:
        return None, _err(
            "`index` (slug) is required in multi-index mode for index_url / "
            "index_status. Call list_indexes to see available slugs and "
            "which are writeable."
        )
    d = registry.by_slug(slug)
    if d is None:
        return None, _err(
            f"Unknown index slug `{slug}`. Registered: {registry.slugs()}. "
            "Call list_indexes for descriptions, domains, and writeable flags."
        )
    if not d.accepts_writes:
        return None, _err(
            f"Index `{slug}` is not writeable: its sift.toml has no "
            f"`[seed].host_allow`, so the MCP server has nothing to "
            f"enforce writes against. Add an allow-list to "
            f"{d.root / 'sift.toml'} and restart the server."
        )
    return _WriteTarget(
        slug=slug,
        root=d.root,
        allow=frozenset(d.allowed_hosts),
        config_path=d.root / "sift.toml",
    ), None


async def _dispatch_index(
    name: str,
    arguments: dict[str, Any],
    *,
    enable_index: bool,
    registry: IndexRegistry,
    job_state: _RegistryJobState,
    legacy_root: Path,
    legacy_allow: set[str],
    legacy_config_path: Optional[Path],
) -> Optional[mcp_types.CallToolResult]:
    """Handle the write/status tools. Returns None if ``name`` isn't one of
    them, so the caller falls through to the read-tool dispatch.

    Extracted from the server closure so the guard / validation /
    concurrency logic is unit-testable.

    Routing model in multi-index mode:
      * Required ``index`` argument selects the target sub-index by slug.
      * Per-slug job state (``_IndexJobState``) gates single-in-flight on
        that slug. A different slug can be writing concurrently.
      * A global cap on concurrent slugs gates total resource use.
      * Allow-list is the target sub-index's ``[seed].host_allow``,
        loaded from its sift.toml at server start.

    Single-index mode keeps the legacy behavior — no ``index`` argument
    needed, all writes go to the operator-supplied root.
    """
    if name not in ("index_url", "index_status"):
        return None
    if not enable_index:
        return _err(
            "Indexing is disabled on this server. Restart sift-mcp with "
            "--enable-index to allow write operations."
        )

    target, err = _resolve_write_target(
        arguments,
        registry=registry,
        legacy_root=legacy_root,
        legacy_allow=legacy_allow,
        legacy_config_path=legacy_config_path,
    )
    if err is not None:
        return err
    assert target is not None
    state = job_state.for_slug(target.slug)

    if name == "index_status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return _err("'run_id' (string) is required — get it from index_url.")
        return tool_index_status(target.root, run_id, state)

    # index_url
    urls = arguments.get("urls")
    if (not isinstance(urls, list) or not urls
            or not all(isinstance(u, str) for u in urls)):
        return _err("'urls' must be a non-empty array of strings.")
    if len(urls) > MAX_INDEX_URLS:
        return _err(f"Too many URLs ({len(urls)}); max {MAX_INDEX_URLS} per call.")
    rejected = [u for u in urls if not _host_allowed(u, target.allow)]
    if rejected:
        if registry.is_multi:
            hint = (
                f"This index only accepts URLs on {sorted(target.allow)}. "
                "Call list_indexes to find which slug owns the host you "
                "want to write — each slug enforces its own allow-list."
            )
        else:
            hint = f"This index only crawls its configured host(s) {sorted(target.allow)}."
        return _err(
            f"Refused {len(rejected)} of {len(urls)} URL(s) for "
            f"`{target.slug}`. First rejected: {rejected[:5]}. {hint}"
        )

    # Single in-flight job per slug. Synchronous check-and-set so two
    # near-simultaneous calls on the same slug can't race past this guard.
    if state.active():
        return _err(
            f"`{target.slug}` already has a crawl in progress "
            f"(run_id={state.run_id}, phase={state.phase}). "
            "Poll index_status, then retry — one crawl per index at a time."
        )

    if job_state.is_at_capacity():
        return _err(
            f"Global concurrent-crawl cap reached "
            f"({job_state.max_concurrent}). Active slugs: "
            f"{job_state.active_slugs()}. Poll index_status on those "
            "before triggering a new crawl, or restart the server with "
            "a higher --max-concurrent-crawls."
        )

    run_id = _new_index_run_id()
    state.run_id = run_id
    state.phase = "seeding"
    state.error = None
    state.task = asyncio.create_task(
        _run_index_job(state, target.root, run_id, list(urls), target.config_path)
    )
    body: dict[str, Any] = {
        "run_id": run_id,
        "status": "started",
        "urls": urls,
        "poll": (f"index_status with run_id={run_id} and index={target.slug}"
                 if registry.is_multi
                 else f"index_status with run_id={run_id}"),
    }
    if registry.is_multi:
        body["index"] = target.slug
    return _ok(json.dumps(body, indent=2))


# ---- MCP wiring -------------------------------------------------------------

_INDEX_PARAM = {
    "type": "string",
    "description": (
        "Slug of the sift index to query, as returned by list_indexes. "
        "REQUIRED in multi-index mode. Pass `\"*\"` to fan the call out "
        "across every registered index (slower; some tools cap matches "
        "harder under fan-out to keep output bounded)."
    ),
}


def _maybe_add_index_param(schema: dict, *, multi: bool,
                           required: bool) -> dict:
    """Add the optional/required ``index`` field to a tool's inputSchema
    when running in multi-index mode. No-ops in single-index mode so the
    schema stays compatible with existing agent prompts."""
    if not multi:
        return schema
    new_schema = dict(schema)
    props = dict(new_schema.get("properties") or {})
    props["index"] = _INDEX_PARAM
    new_schema["properties"] = props
    if required:
        req = list(new_schema.get("required") or [])
        if "index" not in req:
            req.append("index")
        new_schema["required"] = req
    return new_schema


def _tool_descriptors(*, include_index: bool = False,
                      multi: bool = False) -> list[mcp_types.Tool]:
    """The list_tools response. Descriptions are the agent's only window
    into when/why to call each tool — they're the highest-leverage prompts
    in this entire server.

    Tool composition:
      * ``list_indexes`` is prepended in multi-index mode so the agent
        learns about it before grep / read.
      * Existing content tools gain an ``index`` parameter in multi-mode
        (required for read_md / read_facts; optional + fan-outable for
        grep_corpus / glob_corpus / list_dir / query_manifest).
      * The index_url/index_status pair is appended only when
        --enable-index is on; in multi-index mode it's currently
        suppressed because routing writes by slug needs a host-allow
        story per-index that isn't designed yet.
    """
    tools: list[mcp_types.Tool] = []
    if multi:
        tools.append(mcp_types.Tool(
            name="list_indexes",
            description=(
                "Lists every sift index the MCP server has registered.\n"
                "Call FIRST in any multi-index session to see what's available — "
                "you can't call grep_corpus / read_md / list_dir without an "
                "`index=<slug>` argument, and that argument has to come from here.\n"
                "Returns per-index: slug, human description, primary domain, "
                "tags, current page count, and the run-id of the active publish.\n"
                "Use the description + domain to pick the right index for a query; "
                "use tags to filter when several indexes look plausible. "
                "Cross-corpus search is possible via `index=\"*\"` but slower and "
                "noisier — prefer scoped queries when you can."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ))

    tools.extend([
        mcp_types.Tool(
            name="snapshot_status",
            description=(
                "Reports whether the index has a published snapshot and what's in it.\n"
                "Call FIRST when starting a session to confirm there's something to read, "
                "or whenever read_md / grep_corpus return 'No published snapshot' errors.\n"
                "Returns: published yes/no, current run_id, gate results, counts by state "
                "and tier, version pins, and an artifact inventory (md/facts/sections counts).\n"
                "Always works — never errors, never falls back."
                + ("\nIn multi-index mode, pass `index=<slug>` to inspect a "
                   "specific index; omit `index` to fan out across all of them "
                   "(returns a list keyed by slug)." if multi else "")
            ),
            inputSchema=_maybe_add_index_param(
                {"type": "object", "properties": {}},
                multi=multi, required=False,
            ),
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="changed_since",
            description=(
                "Returns ONLY what changed since a cursor you hold — the net "
                "added / modified / removed pages between a prior snapshot and "
                "the current published one, read from the hash-chained changelog.\n"
                "Use to AVOID re-reading the whole corpus each session: store the "
                "run_id from snapshot_status, pass it back as `since`, then read_md "
                "only the pages this returns. The delta is bounded to the "
                "*published* snapshot, so it matches exactly what read_md serves "
                "(changes from a later unpublished run never leak in).\n"
                "`since` is a run_id (preferred — from snapshot_status) OR an "
                "ISO-8601 UTC timestamp. Each item carries url, old_hash/new_hash, "
                "tier, and the changelog entry_hash. Returns counts + a fresh "
                "`cursor` (the current run_id) to store for next time; an empty "
                "delta with up_to_date=true means you've already seen the current "
                "snapshot.\n"
                "Optional path_prefix / tier narrow the delta; results are "
                "newest-first, capped per group with offset paging."
                + ("\nIn multi-index mode pass index=<slug>, or omit / "
                   "index=\"*\" to fan out across all indexes." if multi else "")
            ),
            inputSchema=_maybe_add_index_param({
                "type": "object",
                "required": ["since"],
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "Cursor: a run_id you previously saw (from "
                            "snapshot_status — preferred) or an ISO-8601 UTC "
                            "timestamp (YYYY-MM-DDTHH:MM:SSZ). The delta covers "
                            "changes strictly after this point up to the current "
                            "published snapshot."
                        ),
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": (
                            "Only report URLs starting with this prefix, e.g. "
                            "'https://www.ato.gov.au/individuals'."
                        ),
                    },
                    "tier": {
                        "type": "string",
                        "description": "Only report pages in this tier, e.g. LIVING or FROZEN.",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1,
                        "default": MAX_CHANGED_ENTRIES,
                        "description": (
                            f"Max items per group (added/modified/removed). "
                            f"Default {MAX_CHANGED_ENTRIES}."
                        ),
                    },
                    "offset": {
                        "type": "integer", "minimum": 0, "default": 0,
                        "description": "Skip this many items per group (paging). Default 0.",
                    },
                },
            }, multi=multi, required=False),
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="read_md",
            description=(
                "Reads one markdown file from the indexed corpus.\n"
                "Use AFTER you've found the file via grep_corpus, glob_corpus, "
                "list_dir, or query_manifest — read_md does not search.\n"
                "Returns the file's YAML frontmatter (with url, content_hash, tier, "
                "audience, fy_years, anchors) followed by markdown body. "
                "Truncated at 20K chars by default; pass offset/limit to read more.\n"
                "Use this for both md/ files and INDEX.md / sections/*/INDEX.md.\n\n"
                "Set verify=true for high-stakes reads (anything you intend to cite "
                "or act on): the server re-hashes the body and compares to the "
                "frontmatter's claimed content_hash. Mismatch returns isError — "
                "the file has been modified since publish, treat as untrusted."
            ),
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to current/, e.g. 'md/individuals-and-families/your-tax-return.md' or 'INDEX.md'",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Character offset to start reading from. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "default": MAX_READ_CHARS,
                        "description": f"Max chars to return. Default {MAX_READ_CHARS}.",
                    },
                    "verify": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, re-hash the file body and compare to the "
                            "stored content_hash. On match: prepends a [verify=ok ...] "
                            "header. On mismatch: returns isError — the file is "
                            "untrusted, do not cite it."
                        ),
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="grep_corpus",
            description=(
                "Regex search over the corpus, defaulting to md/. Returns file:line:snippet.\n"
                "Use FIRST when looking up a specific identifier (section number, "
                "form code, anchor name like '{#cents-per-kilometre}', or an exact phrase). "
                "Faster and more precise than semantic search for identifier-heavy queries.\n"
                "Use files_only=true to get just filenames (good for narrowing before read_md).\n"
                "Capped at 200 matches; refine the pattern or set files_only=true if you hit the cap."
            ),
            inputSchema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Python regex pattern, e.g. 'cents per kilometre' or '\\\\$3\\\\s*million'",
                    },
                    "path": {
                        "type": "string",
                        "default": "md/",
                        "description": "Path or directory to search. Default 'md/'. Try 'routes.tsv' for URL lookups.",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "default": False,
                        "description": "Case-insensitive match. Default false.",
                    },
                    "files_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return matching filenames only (one per file). Default false.",
                    },
                    "context": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 0,
                        "description": "Lines of context around each match. Default 0.",
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="glob_corpus",
            description=(
                "Lists files matching an fnmatch-style glob, relative to current/.\n"
                "Use for path-shape queries: 'all 2025 forms', 'all rate-table facts', "
                "'all guides under foreign-income'.\n"
                "Examples: 'md/forms-and-instructions/**/2025*', 'facts/**/*.json', "
                "'sections/*/INDEX.md'.\n"
                "Returns up to 500 paths."
            ),
            inputSchema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "fnmatch glob, e.g. 'md/individuals-and-families/*.md' or 'facts/**/individual-resident*.json'",
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="list_dir",
            description=(
                "Lists immediate contents of a directory. Use for cheap exploration: "
                "'.' (root), 'md/', 'sections/', 'facts/', 'facts/ato-rate-table-v1/'.\n"
                "Returns one line per entry as 'd|f <size> <name>'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Directory path relative to current/. Default '.'.",
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="query_manifest",
            description=(
                "Runs a read-only SELECT against manifest.db (SQLite). The manifest "
                "is the structured index of every URL in the corpus.\n"
                "Use for cross-cutting queries that aren't single-file lookups: "
                "'all FRESH pages under parent_guide X', 'most-recently-changed pages', "
                "'pages by tier', 'URLs missing from this snapshot'.\n"
                "Schema discovery: SELECT sql FROM sqlite_master WHERE type='table'.\n"
                "Returns JSON array of rows, capped at 500 — use LIMIT in your SQL if needed."
            ),
            inputSchema={
                "type": "object",
                "required": ["sql"],
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SELECT or WITH...SELECT statement. Other statements are refused.",
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="read_facts",
            description=(
                "Reads one structured-facts JSON file from facts/. Preferred over "
                "read_md when the answer is a number, threshold, deadline, or rate — "
                "facts are atomic structured records with $schema, source_url, and "
                "content_hash provenance.\n"
                "Discover with: list_dir facts/, list_dir facts/ato-rate-table-v1/, "
                "or glob_corpus 'facts/**/*2025-26*.json'.\n"
                "Schemas live at facts/schemas/<schema>.json."
            ),
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to current/, e.g. 'facts/ato-rate-table-v1/individual-resident-2025-26.json'",
                    },
                },
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
    ])
    if include_index:
        tools += _index_tool_descriptors(multi=multi)

    # In multi-index mode, retrofit each tool's schema with the ``index``
    # parameter. The required-vs-optional decision is per-tool:
    #   * read_md, read_facts, index_url, index_status — REQUIRED.
    #     Reads need to know which corpus; writes need to know which
    #     allow-list to enforce and which slug's job state to gate.
    #   * grep_corpus, glob_corpus, list_dir, query_manifest, snapshot_status
    #     — optional (omit = fan-out to every index; the dispatcher
    #     handles the multi-call merge and adds a per-index header).
    #   * list_indexes — not retrofitted; it's the discovery tool itself.
    if multi:
        index_required = {"read_md", "read_facts", "index_url", "index_status"}
        skip = {"list_indexes"}
        retrofitted: list[mcp_types.Tool] = []
        for t in tools:
            if t.name in skip:
                retrofitted.append(t)
                continue
            new_schema = _maybe_add_index_param(
                t.inputSchema, multi=True,
                required=(t.name in index_required),
            )
            retrofitted.append(mcp_types.Tool(
                name=t.name, description=t.description,
                inputSchema=new_schema, annotations=t.annotations,
            ))
        tools = retrofitted
    return tools


def _index_tool_descriptors(*, multi: bool = False) -> list[mcp_types.Tool]:
    """The write/status tools, exposed only under --enable-index.

    In multi-index mode the tools take a required ``index`` parameter
    routing the call to a specific sub-index; the operator's per-slug
    ``[seed].host_allow`` is enforced. ``list_indexes`` is the canonical
    discovery path — its ``writeable`` flag tells the agent which slugs
    accept ``index_url``."""
    multi_paragraph = (
        "\nIn multi-index mode, pass `index=<slug>` to route the write to a "
        "specific sub-index. Each sub-index enforces its own allow-list "
        "(visible as `allowed_hosts` in list_indexes). Use list_indexes "
        "to find the slug that owns the URL's host AND has `writeable=true`."
    ) if multi else ""

    index_url_schema_props: dict[str, Any] = {
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_INDEX_URLS,
            "description": (
                "Absolute http(s) URLs to index, each on the target index's "
                f"allow-listed host(s). Max {MAX_INDEX_URLS} per call. "
                "Already-indexed URLs are re-planned (re-fetched if "
                "refresh-due, otherwise skipped); unseen URLs are seeded "
                "and fetched."
            ),
        },
    }
    index_url_required = ["urls"]

    index_status_schema_props: dict[str, Any] = {
        "run_id": {
            "type": "string",
            "description": ("The run_id returned by index_url. Run-ids are "
                            "globally unique (timestamp + random suffix)."),
        },
    }
    index_status_required = ["run_id"]

    return [
        mcp_types.Tool(
            name="index_url",
            description=(
                "Adds URLs to the index and triggers an incremental crawl. "
                "Use when grep / glob / query_manifest came up empty for content "
                "you need, OR when list_indexes shows nonzero `unseen_count` and "
                "the URLs you want are the gap.\n"
                "Returns IMMEDIATELY with run_id + `poll` hint — the crawl runs "
                "in the background. Poll index_status (run_id) until status is "
                "`succeeded`, then read_md the new pages. Typical end-to-end is "
                "seconds for small adds, minutes for large adds.\n"
                "Allow-list: every URL must be on the target index's "
                "`seed.host_allow`. Off-list URLs are refused with the "
                "accepted hosts in the error.\n"
                "Concurrency: one crawl per index at a time; cross-slug "
                "concurrency is bounded by the server's `concurrent_crawl_cap` "
                "(visible in list_indexes). Over-cap calls return an error "
                "listing currently active slugs so you can poll those instead."
                + multi_paragraph
            ),
            inputSchema={
                "type": "object",
                "required": index_url_required,
                "properties": index_url_schema_props,
            },
            # Not read-only — mutates the manifest and may flip current/.
            # openWorldHint: it fetches from the open web (allow-listed hosts),
            # so clients surface it as an external-effect tool, never auto-run.
            annotations=mcp_types.ToolAnnotations(
                readOnlyHint=False, openWorldHint=True),
        ),
        mcp_types.Tool(
            name="index_status",
            description=(
                "Reports the state of a background index job started by "
                "index_url. Pass the run_id you got back from index_url.\n"
                "Returns: status (running | succeeded | degraded | failed), "
                "current phase, timestamps, per-state counts, and whether the "
                "run is now the published current/ snapshot. When status is "
                "`succeeded` the new pages are readable via read_md without "
                "any extra publish step.\n"
                "In-progress jobs do not survive a server restart; completed "
                "runs are durable in the manifest and remain queryable by "
                "run_id forever."
                + multi_paragraph
            ),
            inputSchema={
                "type": "object",
                "required": index_status_required,
                "properties": index_status_schema_props,
            },
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
    ]


_FANOUT_HEADER = "===== index: {slug} ====="

# Tools that accept index="*" or no index in multi-mode (fan-out across
# every registered index). Tools NOT listed here require a specific slug
# in multi-mode, because their semantics ("read this specific file")
# don't generalize to multiple indexes.
_FANOUT_TOOLS = frozenset({
    "snapshot_status", "changed_since", "grep_corpus", "glob_corpus",
    "list_dir", "query_manifest",
})


def _fanout(
    registry: IndexRegistry,
    fn,                             # callable(index_root: Path) -> CallToolResult
    *,
    per_index_cap: int = 60,         # truncate each index's output so the fan-out total stays bounded
) -> mcp_types.CallToolResult:
    """Call ``fn`` once per registered index, stitching the results into a
    single response with per-index headers. Used by tools that accept
    ``index="*"`` (or no ``index`` arg in multi-mode).

    Errors from a sub-index are surfaced inline rather than failing the
    whole call — a missing snapshot on one index shouldn't block the
    others. The per-index cap defends against one index's grep dumping
    hundreds of lines and starving the rest from the agent's view."""
    chunks: list[str] = []
    any_ok = False
    for d in registry.indexes:
        try:
            res = fn(d.root)
        except Exception as e:
            chunks.append(_FANOUT_HEADER.format(slug=d.slug))
            chunks.append(f"[error: {e!r}]")
            continue
        if not res.content:
            continue
        # Each CallToolResult has content[0].text — concatenate as a
        # capped chunk, so the agent sees per-index sections.
        body = res.content[0].text if res.content else ""
        body_lines = body.splitlines()
        if len(body_lines) > per_index_cap:
            kept = body_lines[:per_index_cap]
            kept.append(
                f"[per-index cap of {per_index_cap} lines hit; refine "
                f"with index=\"{d.slug}\" for more]"
            )
            body = "\n".join(kept)
        chunks.append(_FANOUT_HEADER.format(slug=d.slug))
        chunks.append(body)
        if not res.isError:
            any_ok = True
    if not chunks:
        return _ok("No matches across any registered index.")
    text = "\n".join(chunks)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        # Only an error if EVERY index errored (none produced a usable
        # result). A benign "no published snapshot" / "no match" on one
        # index must not poison an otherwise-useful aggregate — the agent
        # harness branches on isError and would discard real matches.
        # Per-index errors are still surfaced inline in the text above.
        isError=not any_ok,
    )


def _server_instructions(*, multi: bool, enable_index: bool) -> str:
    """The MCP ``instructions`` string — the agent's standing operating
    procedure, surfaced by clients at initialization so the agent learns the
    call-order + provenance contract without a human reading a README. Codex
    prioritizes the first ~512 chars, so the what-it-is + call-snapshot_status-
    first contract is front-loaded. Deployment-agnostic on purpose: the same
    build_server backs the local stdio server, so no deployment-specific
    language here — deployment-specific wrappers may append additional notes."""
    parts = [
        "sift serves a verified, always-current index of a documentation corpus "
        "for grep-first retrieval. Every page is content-hashed and dated, so "
        "answers can cite the exact source and snapshot.",
        "",
        "Start every session with snapshot_status: it confirms a published "
        "snapshot exists and reports coverage, freshness, and the run id. If it "
        "reports unpublished, stop and surface that — the read tools will refuse.",
        "",
        "Remember the run_id snapshot_status returns. On a later session, call "
        "changed_since(since=<that run_id>) to pull ONLY the added/modified/removed "
        "pages since then — read_md just those instead of re-reading the corpus, "
        "then store the new cursor it returns. This is how you stay current cheaply.",
    ]
    if multi:
        parts.append(
            "Multi-index: call list_indexes first to choose a corpus, then pass "
            "index=<slug> on every tool (index=\"*\" fans out the read tools).")
    parts += [
        "",
        "Then: grep_corpus to locate pages by pattern (search before reading); "
        "read_md to read a hit (use offset/limit to page through long files "
        "instead of re-reading); glob_corpus / list_dir to explore the path "
        "tree; read_facts / query_manifest for structured lookups.",
        "",
        "Be token-efficient — every result is capped: locate with grep, then "
        "drill in with read_md offset/limit; don't try to read the whole corpus. "
        "Provenance: each page's frontmatter carries content_hash + fetched_at + "
        "source url; cite those when an answer must be verifiable.",
    ]
    if enable_index:
        parts.append(
            "To expand coverage, call index_url with an allow-listed URL, then "
            "poll index_status until it reports succeeded — the new page is then "
            "readable with read_md.")
    return "\n".join(parts)


def build_server(
    root: Path,
    *,
    enable_index: bool = False,
    host_allow: Optional[set[str]] = None,
    config_path: Optional[Path] = None,
    max_concurrent_crawls: int = _DEFAULT_MAX_CONCURRENT_CRAWLS,
    registry_ttl_seconds: float = 1.0,
) -> Server:
    """Wire the tool list + dispatcher onto a fresh MCP Server.

    ``root`` may be a single sift index root OR a parent directory
    containing many sift roots. ``IndexRegistry.discover`` figures out
    which — single-root mode keeps the original behavior (no ``index``
    parameter on tools), multi-root mode adds ``list_indexes`` + an
    ``index`` parameter for routing.

    ``host_allow`` and ``config_path`` are used in single-index mode
    (the legacy eager-load path). In multi-index mode each sub-index's
    sift.toml is loaded on demand from its ``[seed].host_allow`` — those
    args are ignored.

    ``max_concurrent_crawls`` caps total concurrent ``index_url`` jobs
    across all slugs. Default protects single-box deployments; bump for
    bigger hosts.

    ``registry_ttl_seconds`` controls how stale the registry can be
    before it's rebuilt. The cache means a freshly-built sub-index
    becomes visible within ``ttl_seconds`` without an MCP server
    restart — the load-bearing fix for the "I built an index but the
    server can't see it" friction. Set to 0 to rebuild on every call
    (expensive at scale); set higher to amortize across a burst.

    Tool calls operate on the resolved ``current/`` snapshot for
    content lookups but use the index root itself for the manifest DB.
    """
    # The initial discovery sets the schema branch (single vs multi
    # mode); switching between them mid-session would break clients
    # that cached the tool list, so ``is_multi`` is frozen at startup.
    # Per-slug membership refreshes via the registry cache below.
    initial_registry = IndexRegistry.discover(root)
    registry_cache = RegistryCache(root, ttl_seconds=registry_ttl_seconds)
    is_multi = initial_registry.is_multi
    # The agent's standing operating procedure (call-order + provenance +
    # token-efficiency contract), surfaced at initialization. Shaped by mode
    # and whether the write tool is enabled so it only describes live tools.
    server = Server("sift", version=__version__, instructions=_server_instructions(
        multi=is_multi, enable_index=enable_index))
    index_root = root.resolve()
    allow = host_allow or set()
    job_state = _RegistryJobState(max_concurrent=max_concurrent_crawls)

    def _resolve_for(
        registry: IndexRegistry, name: str, arguments: dict,
    ) -> tuple[Optional[Path], Optional[mcp_types.CallToolResult]]:
        """Return ``(index_root_or_None, error_result_or_None)`` for one
        tool call against ``registry``. ``None`` for the path means
        "fan out across all indexes" — only valid for the tools listed
        in ``_FANOUT_TOOLS``.

        ``registry`` is passed in (not closed over) so each call uses
        the freshly-cached view from ``registry_cache.get()`` rather
        than a stale snapshot taken at server start. That's what makes
        newly-built indexes visible without a server restart.
        """
        if not registry.is_multi:
            return index_root, None
        slug = arguments.get("index")
        if slug is None or slug == "":
            # Fan-out only legal for read-many tools.
            if name not in _FANOUT_TOOLS:
                return None, _err(
                    f"`index` parameter is required for {name} in multi-"
                    f"index mode. Call list_indexes to see available "
                    f"slugs (registered: {registry.slugs()})."
                )
            return None, None
        if slug == "*":
            if name not in _FANOUT_TOOLS:
                return None, _err(
                    f"Cross-corpus fan-out (`index=\"*\"`) is not supported "
                    f"for {name} — pass a specific slug instead. "
                    f"Available: {registry.slugs()}."
                )
            return None, None
        d = registry.by_slug(slug)
        if d is None:
            return None, _err(
                f"Unknown index slug `{slug}`. Registered: "
                f"{registry.slugs()}. Call list_indexes for descriptions."
            )
        return d.root, None

    @server.list_tools()
    async def _list() -> list[mcp_types.Tool]:
        # The single/multi schema decision is frozen at startup so a
        # client that cached the tool list doesn't see field
        # requirements flip mid-session. Per-slug membership is fresh
        # via the registry cache.
        return _tool_descriptors(
            include_index=enable_index, multi=is_multi,
        )

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        # Fetch a fresh-enough registry view for this call. The cache
        # absorbs the ~5-10ms per sub-index discovery cost across a
        # burst of related calls; new indexes show up within the TTL.
        registry = registry_cache.get()

        # list_indexes is the registry-discovery tool — only valid in
        # multi-index mode but always cheap, never falls back.
        if name == "list_indexes":
            if not is_multi:
                return _err(
                    "list_indexes is only available in multi-index mode. "
                    "This server was started against a single sift root."
                )
            return tool_list_indexes(
                registry, enable_index=enable_index, job_state=job_state,
            )

        # Write/status tools: routed per-slug in multi-mode, legacy
        # single-root in single-mode.
        idx = await _dispatch_index(
            name, arguments,
            enable_index=enable_index,
            registry=registry,
            job_state=job_state,
            legacy_root=index_root,
            legacy_allow=allow,
            legacy_config_path=config_path,
        )
        if idx is not None:
            return idx

        # Read tools: resolve the target index root from the `index` arg
        # (or fall back to the legacy single-root in single-mode). The
        # resolver returns either a single Path (scoped call) or None
        # (fan-out across all registered indexes).
        target, err = _resolve_for(registry, name, arguments)
        if err is not None:
            return err

        # snapshot_status special-cases: it's the diagnostic tool, must
        # not require a published snapshot. We route it BEFORE the
        # _require_published guard so an empty index still gets a useful
        # answer.
        if name == "snapshot_status":
            if target is None:           # fan-out
                return _fanout(registry,
                               lambda r: tool_snapshot_status(r))
            return tool_snapshot_status(target)

        # changed_since reads the index-root changelog (like snapshot_status,
        # NOT the current/ snapshot), so route it here before the scoped path.
        if name == "changed_since":
            since = arguments.get("since")
            if not isinstance(since, str) or not since:
                return _err(
                    "changed_since requires `since` — a run_id from "
                    "snapshot_status, or an ISO-8601 UTC timestamp."
                )
            cs_kwargs = dict(
                path_prefix=arguments.get("path_prefix"),
                tier=arguments.get("tier"),
                limit=int(arguments.get("limit", MAX_CHANGED_ENTRIES)),
                offset=int(arguments.get("offset", 0)),
            )
            if target is None:           # fan-out across all indexes
                return _fanout(
                    registry,
                    lambda r: tool_changed_since(r, since, **cs_kwargs),
                )
            return tool_changed_since(target, since, **cs_kwargs)

        def _scoped_call(idx_root: Path) -> mcp_types.CallToolResult:
            cur, is_published = _resolve_root(idx_root)
            guard = _require_published(is_published)
            if guard is not None:
                return guard
            try:
                if name == "read_md":
                    return tool_read_md(
                        cur, arguments["path"],
                        offset=arguments.get("offset", 0),
                        limit=arguments.get("limit", MAX_READ_CHARS),
                        verify=arguments.get("verify", False),
                        index_root=idx_root,
                    )
                if name == "grep_corpus":
                    return tool_grep_corpus(
                        cur, arguments["pattern"],
                        path=arguments.get("path", "md/"),
                        ignore_case=arguments.get("ignore_case", False),
                        files_only=arguments.get("files_only", False),
                        context=arguments.get("context", 0),
                    )
                if name == "glob_corpus":
                    return tool_glob_corpus(cur, arguments["pattern"])
                if name == "list_dir":
                    return tool_list_dir(cur, arguments.get("path", "."))
                if name == "query_manifest":
                    return tool_query_manifest(
                        cur, arguments["sql"], index_root=idx_root,
                    )
                if name == "read_facts":
                    return tool_read_facts(cur, arguments["path"])
            except KeyError as e:
                return _err(f"Missing required argument: {e}")
            avail = [t.name for t in _tool_descriptors(
                include_index=enable_index, multi=registry.is_multi,
            )]
            return _err(f"Unknown tool '{name}'. Available: {avail}")

        if target is None:
            return _fanout(registry, _scoped_call)
        return _scoped_call(target)

    # Expose the job registry so the server's shutdown path can cancel
    # in-flight crawls (cancellation triggers _spawn's SIGTERM->SIGKILL
    # of the child). Without this, `sift run` children orphan and outlive
    # the MCP server on shutdown — the leak the _spawn docstring claims
    # to prevent but couldn't reach on its own.
    server._sift_job_state = job_state  # type: ignore[attr-defined]
    return server


@click.command()
@click.option("--root", required=True, type=click.Path(exists=True, path_type=Path),
              help="Sift index root (with current/ symlink) for single-index mode, "
                   "OR a parent directory of multiple sift roots for multi-index mode. "
                   "In multi-mode the server exposes a `list_indexes` tool and all "
                   "content tools take an `index=<slug>` parameter.")
@click.option("--config", "config_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Config TOML. SINGLE-INDEX MODE ONLY: read at startup for the "
                   "host allow-list and threaded through to the crawl subprocess. "
                   "In multi-index mode the per-slug sift.toml under each sub-index "
                   "is loaded on demand instead.")
@click.option("--enable-index", is_flag=True, default=False,
              help="Expose the index_url/index_status write tools. OFF by default so the "
                   "server is strictly read-only. When on, index_url may only crawl hosts "
                   "in the relevant index's seed.host_allow.")
@click.option("--max-concurrent-crawls",
              type=click.IntRange(min=1, max=32),
              default=_DEFAULT_MAX_CONCURRENT_CRAWLS, show_default=True,
              help="Cap on simultaneous in-flight index_url crawls across all "
                   "slugs. Defends single-box deployments — each crawl spawns a "
                   "sift subprocess. Bump for bigger hosts.")
@click.option("--registry-ttl-ms",
              type=click.IntRange(min=0, max=60_000),
              default=1000, show_default=True,
              help="How long to cache the multi-index registry between "
                   "tool calls (milliseconds). A freshly-built sub-index "
                   "becomes visible within this TTL without an MCP server "
                   "restart. 0 rebuilds on every call (expensive at scale).")
def main(root: Path, config_path: Optional[Path], enable_index: bool,
         max_concurrent_crawls: int,
         registry_ttl_ms: int) -> None:
    """Run the MCP server over stdio. Wire up in Claude Code's settings.json:

        {
          "mcpServers": {
            "sift": {
              "command": "sift-mcp",
              "args": ["--root", "/abs/path/to/index"]
            }
          }
        }

    Add "--enable-index" to args to allow the agent to crawl new URLs.
    Single-index mode uses the operator's --config (or ./sift.toml) for
    the allow-list; multi-index mode reads each sub-index's per-slug
    sift.toml at write time, so --config is ignored.
    """
    host_allow: set[str] = set()
    resolved_config: Optional[Path] = None
    # Single-index allow-list is loaded eagerly so a misconfigured server
    # fails at startup (not at first write). In multi-index mode, every
    # sub-index has its own [seed].host_allow loaded by IndexRegistry —
    # there's nothing to pre-load at the parent.
    registry_probe = IndexRegistry.discover(root)
    if enable_index and not registry_probe.is_multi:
        from .config import _resolve_config_path, load_config
        cfg = load_config(config_path)
        host_allow = {h.lower() for h in cfg.seed.host_allow}
        rc = _resolve_config_path(config_path)
        resolved_config = rc.resolve() if rc is not None else None

    server = build_server(
        root,
        enable_index=enable_index,
        host_allow=host_allow,
        config_path=resolved_config,
        max_concurrent_crawls=max_concurrent_crawls,
        registry_ttl_seconds=registry_ttl_ms / 1000.0,
    )

    async def _cancel_inflight_crawls() -> None:
        """Cancel + await any in-flight index_url crawl tasks so their
        child subprocesses are terminated (via _spawn's CancelledError
        handler) rather than orphaned when the server stops."""
        js = getattr(server, "_sift_job_state", None)
        if js is None:
            return
        tasks = [st.task for st in js.per_slug.values()
                 if st.task is not None and not st.task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _run() -> None:
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream,
                    server.create_initialization_options(),
                )
        finally:
            # Runs on normal close AND on cancellation (Ctrl-C / SIGTERM
            # surfaces as KeyboardInterrupt/CancelledError through
            # asyncio.run), so crawls don't outlive the server.
            await _cancel_inflight_crawls()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
