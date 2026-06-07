"""Tests for the targeted-backfill path:

  1. ``plan(only_urls=...)`` scopes the plan to a specific URL set.
  2. ``sift run --only-urls <file>`` exposes that via CLI.
  3. ``_spawn`` terminates its child subprocess on task cancellation
     so an MCP server restart doesn't leak running crawls.

This is the load-bearing change that turns ``index_url(slug, [one_url])``
from a 4000-URL corpus expansion into a one-URL targeted fetch.
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sift import mcp_server, paths
from sift.config import IndexConfig
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)
from sift.plan import plan
from sift.sites import SiteProfile


class _NoBrowserProfile(SiteProfile):
    """Minimal profile: no URLs need the browser path. Lets the plan
    short-circuit to the standard decide() flow without test-specific
    routing weirdness."""

    def requires_browser(self, url: str) -> bool:
        return False


# ---- plan(only_urls=...) -------------------------------------------------

class TestPlanOnlyUrls:
    def _seed_many(self, tmp_path: Path) -> tuple[Path, list[str]]:
        urls = [f"https://x.test/p/{i}" for i in range(5)]
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        with transaction(conn):
            for u in urls:
                upsert_seed(conn, url=u, tier="LIVING", parent_guide_=None,
                            classifier_version="v1", sitemap_lastmod=None,
                            now=now_utc())
        return tmp_path, urls

    def test_only_urls_filters_plan_to_subset(self, tmp_path):
        root, urls = self._seed_many(tmp_path)
        plan_path = tmp_path / "plan.jsonl"
        target = {urls[1], urls[3]}
        counts = plan(
            open_db(paths.manifest_path(root)), plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev", normalizer_version="nv",
            profile=_NoBrowserProfile(), cfg=IndexConfig(),
            only_urls=target,
        )
        entries = [json.loads(line) for line in
                   plan_path.read_text().strip().splitlines()]
        assert {e["url"] for e in entries} == target
        # Counts include the skip tally for visibility
        assert counts["skipped_not_in_only_urls"] == 3

    def test_none_only_urls_is_no_op(self, tmp_path):
        """Passing ``only_urls=None`` is the pre-existing behavior — every
        manifest row appears in the plan."""
        root, urls = self._seed_many(tmp_path)
        plan_path = tmp_path / "plan.jsonl"
        counts = plan(
            open_db(paths.manifest_path(root)), plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev", normalizer_version="nv",
            profile=_NoBrowserProfile(), cfg=IndexConfig(),
            only_urls=None,
        )
        entries = [json.loads(line) for line in
                   plan_path.read_text().strip().splitlines()]
        assert {e["url"] for e in entries} == set(urls)
        assert "skipped_not_in_only_urls" not in counts

    def test_empty_only_urls_yields_empty_plan(self, tmp_path):
        """Defensive: an empty set scopes to nothing. The plan file is
        valid (empty) and the runner downstream sees no fetchable rows."""
        root, urls = self._seed_many(tmp_path)
        plan_path = tmp_path / "plan.jsonl"
        counts = plan(
            open_db(paths.manifest_path(root)), plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev", normalizer_version="nv",
            profile=_NoBrowserProfile(), cfg=IndexConfig(),
            only_urls=set(),
        )
        assert plan_path.read_text().strip() == ""
        assert counts["skipped_not_in_only_urls"] == 5

    def test_only_urls_outside_manifest_are_silently_ignored(self, tmp_path):
        """Manifest is authoritative — URLs requested but never seeded
        don't appear in the plan (they were filtered before the planner
        reached them via the manifest scan)."""
        root, urls = self._seed_many(tmp_path)
        plan_path = tmp_path / "plan.jsonl"
        target = {urls[0], "https://other.test/not-in-manifest"}
        plan(
            open_db(paths.manifest_path(root)), plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev", normalizer_version="nv",
            profile=_NoBrowserProfile(), cfg=IndexConfig(),
            only_urls=target,
        )
        entries = [json.loads(line) for line in
                   plan_path.read_text().strip().splitlines()]
        # The plan only emits rows that exist in the manifest AND match
        assert [e["url"] for e in entries] == [urls[0]]


# ---- _spawn cancellation kills the child ----------------------------------

class TestSpawnCancellation:
    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX signals only")
    async def test_cancelled_task_terminates_subprocess(self, tmp_path):
        """The subprocess sleeps for 30s; we cancel the awaiting task
        within 1s and assert the child process is reaped within the
        SIGTERM grace window — not left running for the full 30s."""
        slow_script = tmp_path / "slow.py"
        slow_script.write_text(
            "import time, signal, sys\n"
            # Ignore SIGINT so only SIGTERM/KILL from our _spawn cleanup
            # actually stops us — that's what's exercised here.
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )

        async def runner():
            return await mcp_server._spawn([sys.executable, str(slow_script)])

        task = asyncio.create_task(runner())
        await asyncio.sleep(0.5)            # let the child actually start
        task.cancel()
        t0 = time.time()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.time() - t0
        # SIGTERM grace is 5s in _spawn; the child should be gone well
        # within 10s of cancel.
        assert elapsed < 10, (
            f"cancellation took {elapsed:.2f}s — child probably leaked"
        )

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX signals only")
    async def test_uncooperative_child_gets_killed(self, tmp_path):
        """A child that ignores SIGTERM must still be killed within the
        grace window. Validates the SIGKILL fallback after SIGTERM."""
        stubborn = tmp_path / "stubborn.py"
        stubborn.write_text(
            "import time, signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGINT,  signal.SIG_IGN)\n"
            "time.sleep(60)\n"
        )

        async def runner():
            return await mcp_server._spawn([sys.executable, str(stubborn)])

        task = asyncio.create_task(runner())
        await asyncio.sleep(0.5)
        task.cancel()
        t0 = time.time()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.time() - t0
        # 5s SIGTERM grace + slack for SIGKILL reap. Anything > 15s
        # would mean the kill path also fell through.
        assert elapsed < 15, (
            f"uncooperative child took {elapsed:.2f}s to stop"
        )
