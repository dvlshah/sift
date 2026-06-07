"""``sift status`` summary — extracted from cli.py so it's testable.

The CLI command is a thin wrapper that JSON-encodes the dict returned here.
Keeping the compute step separate (matching paths.py, publish.py, integrity.py)
lets contract tests import + call it directly without spawning subprocesses.

See ``docs/design/browser-fetch.md`` §12.4 step 9.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import CRAWLER_VERSION, paths
from .classify import CLASSIFIER_VERSION
from .extract import EXTRACTOR_VERSION
from .manifest import counts_by_state, counts_by_tier, init_schema, open_db
from .normalize import NORMALIZER_VERSION


# Module-level cache so we only resolve the runtime Chromium version once per
# process. Surfacing in sift status is informational (drift detection), not
# load-bearing for any decision, so a stale read after a Chromium upgrade
# mid-process is acceptable.
_RUNTIME_CHROMIUM_CACHE: Optional[str] = None
_RUNTIME_CHROMIUM_RESOLVED = False


def _resolve_runtime_chromium() -> Optional[str]:
    """Best-effort Chromium version lookup. ``None`` if the browser stack
    isn't installed or the version probe fails — never raises."""
    global _RUNTIME_CHROMIUM_CACHE, _RUNTIME_CHROMIUM_RESOLVED
    if _RUNTIME_CHROMIUM_RESOLVED:
        return _RUNTIME_CHROMIUM_CACHE
    _RUNTIME_CHROMIUM_RESOLVED = True
    try:
        import playwright  # type: ignore[import-untyped]
        version = getattr(playwright, "__version__", None)
        if version:
            _RUNTIME_CHROMIUM_CACHE = f"playwright-{version}"
    except ImportError:
        _RUNTIME_CHROMIUM_CACHE = None
    return _RUNTIME_CHROMIUM_CACHE


def _total_rows(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM manifest").fetchone()
    return int(row["n"]) if row else 0


def _browser_url_cache_stats(conn) -> tuple[int, int]:
    """Return (browser_urls_total, browser_urls_with_etag_or_lastmod).

    A browser-fetched row has a non-NULL ``browser_version``. Of those, ones
    with an http_etag or http_last_modified persisted from the Response hook
    are eligible for conditional re-fetch — operators can watch this ratio
    fall to spot hook regressions.
    """
    total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM manifest WHERE browser_version IS NOT NULL"
    ).fetchone()
    total = int(total_row["n"]) if total_row else 0
    if total == 0:
        return 0, 0
    cached_row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM manifest
        WHERE browser_version IS NOT NULL
          AND (http_etag IS NOT NULL OR http_last_modified IS NOT NULL)
        """
    ).fetchone()
    cached = int(cached_row["n"]) if cached_row else 0
    return total, cached


def compute_status_summary(root: Path) -> dict[str, Any]:
    """Build the summary dict used by ``sift status``.

    Tolerates a missing manifest DB (returns a defaults-only structure) so
    contract tests can call against an empty ``tmp_path``.
    """
    root = Path(root)
    manifest_path = paths.manifest_path(root)
    by_state: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    manifest_rows = 0
    browser_total = 0
    browser_cached = 0
    recent_runs: list[dict[str, Any]] = []

    if manifest_path.exists():
        conn = open_db(manifest_path)
        try:
            init_schema(conn)
            by_state = counts_by_state(conn)
            by_tier = counts_by_tier(conn)
            manifest_rows = _total_rows(conn)
            browser_total, browser_cached = _browser_url_cache_stats(conn)
            recent_runs = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"
                )
            ]
        finally:
            conn.close()

    current = paths.current_symlink(root)
    cur_target = str(current.resolve()) if current.exists() else None

    cached_ratio: Optional[float] = (
        (browser_cached / browser_total) if browser_total > 0 else None
    )

    from .browser import BROWSER_VERSION

    return {
        "root": str(root),
        "manifest_rows": manifest_rows,
        "by_state": by_state,
        "by_tier": by_tier,
        "current": cur_target,
        "recent_runs": recent_runs,
        # New browser observability — surfaces the operator-opt-out count and
        # the cache-headers ratio so a regression in the Response hook is
        # visible in `sift status` rather than only at the next plan cycle.
        "skipped_browser_disabled": int(by_state.get("SKIPPED_BROWSER_DISABLED", 0)),
        "browser_urls_with_cached_headers": {
            "total": browser_total,
            "with_cache_headers": browser_cached,
            "ratio": cached_ratio,
        },
        "versions": {
            "crawler": CRAWLER_VERSION,
            "extractor": EXTRACTOR_VERSION,
            "normalizer": NORMALIZER_VERSION,
            "classifier": CLASSIFIER_VERSION,
            "browser": BROWSER_VERSION,
            "browser_runtime_chromium": _resolve_runtime_chromium(),
        },
    }
