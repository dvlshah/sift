"""Filesystem layout for the index. Centralized so every phase agrees on paths.

    <root>/
      manifest.db                       # single source of truth
      raw/<aa>/<sha256>.html.gz         # content-addressed raw HTML (gzip-compressed)
      runs/<run_id>/
        plan.jsonl                      # phase 1 output
        fetch.log                       # phase 2 output (one JSON object per line)
        extract.log                     # phase 3 output
        md/<url-path>.md                # phase 3 markdown (staged)
        md/<url-path>.meta.json         # phase 3 provenance (staged)
        artifacts/llms.txt              # phase 5
        artifacts/llms-full.txt         # phase 5
        snapshot.json                   # phase 5
      current -> runs/<run_id>          # symlink, flipped atomically on publish
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .classify import safe_path_segments


def manifest_path(root: Path) -> Path:
    return root / "manifest.db"


def raw_path(root: Path, raw_hash: str) -> Path:
    """Two-level fanout under raw/ to avoid millions of files in one dir."""
    return root / "raw" / raw_hash[:2] / f"{raw_hash}.html.gz"


def run_dir(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


def plan_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "plan.jsonl"


def fetch_log_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "fetch.log"


def extract_log_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "extract.log"


def md_path(root: Path, run_id: str, url: str) -> Path:
    parts = safe_path_segments(url)
    if not parts:
        parts = ["_root"]
    return run_dir(root, run_id) / "md" / Path(*parts).with_suffix(".md")


def meta_path(root: Path, run_id: str, url: str) -> Path:
    return md_path(root, run_id, url).with_suffix(".meta.json")


def snapshot_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "snapshot.json"


def artifacts_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "artifacts"


def current_symlink(root: Path) -> Path:
    return root / "current"


def published_run_dir(root: Path) -> Optional[Path]:
    """Return the run dir that is ACTUALLY published, or None.

    A run is published iff the ``current`` symlink points at it — that
    flip is the sole gated action in publish(), so it's the one true
    "passed all gates" signal. We honor the symlink by NAME (via
    ``readlink``) even when it doesn't resolve, which covers the macOS
    ``/tmp -> /private/tmp`` case where a relative symlink lexists but
    ``Path.exists()`` returns False.

    Critically, this does NOT fall back to "newest runs/ dir": a run that
    FAILED its publish gates still writes ``runs/<id>/`` (md + a
    snapshot.json with status=degraded) but never flips ``current``. The
    old latest_run_dir fallback served those gate-failed runs as the
    published snapshot — a provenance hole. An index whose only run
    degraded correctly resolves to None (unpublished).

    This is the single canonical resolver: registry discovery, the MCP
    content tools, and index_status all delegate here so they can never
    disagree about what is published.
    """
    cur = current_symlink(root)
    if cur.exists():
        return cur.resolve()
    if cur.is_symlink():
        # Broken/relative symlink: trust the name it points at, resolved
        # under this root's runs/. A published run's symlink names its run.
        try:
            named = Path(cur.readlink()).name
        except OSError:
            named = ""
        if named:
            candidate = run_dir(root, named)
            if candidate.is_dir():
                return candidate
    return None


def backups_dir(root: Path) -> Path:
    """Where SQLite manifest backups land. Lives at the index root so a
    nightly `sift backup` produces files alongside the live manifest."""
    return root / "backups"


def changelog_path(root: Path) -> Path:
    """Append-only across all runs. Lives at the index root, not per-run."""
    return root / "changelog.jsonl"


def index_md_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "INDEX.md"


def routes_tsv_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "routes.tsv"


def section_index_path(root: Path, run_id: str, section: str) -> Path:
    return run_dir(root, run_id) / "sections" / section / "INDEX.md"


def facts_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "facts"
