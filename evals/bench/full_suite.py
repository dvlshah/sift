"""Full-suite harness: build a fresh, capped index per fixture, then run
the per-stage bench across all of them.

Design constraints (per the user's directive):

* **Cap per-site at N URLs** (default 500) so we don't build 24K-URL ATO
  crawls for every fixture in the suite. Sampling-based stress test, not
  exhaustive corpora.
* **Resumable** — each fixture's index lives at ``<root_dir>/<slug>/``;
  if that root already has a published ``current/`` snapshot, skip the
  init/seed/run unless ``--rebuild`` is set.
* **Bounded cost** on Firecrawl: optional ``--enable-firecrawl-fallback``
  with a per-site credit ceiling (default 50) — Shopify-class sites
  exercise the fallback at-cap; clean sites pay $0.
* **Honest failure surfacing** — a site that 0-seeds, fetch-blocks, or
  publishes-degraded surfaces in the per-fixture report as the operator-
  facing signal, not a harness exception.

The aggregate roll-up renders one row per fixture × stage, with use-case
roll-ups and an overall pass/fail grid suitable for nightly tracking.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .fixtures.sites import POSITIVE_FIXTURES, SiteFixture


# ---- Per-site config template ----------------------------------------------

# GenericProfile uniformly across the suite so we're measuring sift's
# out-of-box capability, not per-site profile work. Browser disabled — the
# Firecrawl fallback (when enabled) handles bot-blocked sites; SPAs without
# Firecrawl are surfaced as "0 md" honestly.
_CFG_TEMPLATE = """\
[site]
profile = "sift.sites.generic:GenericProfile"

[browser]
enabled = false

[crawl]
rate_per_sec = 4.0
concurrency  = 4
timeout_sec  = 20.0
retries      = 1
user_agent   = "Mozilla/5.0 (compatible; sift-bench/1.0)"

[crawl.firecrawl]
enabled              = {firecrawl_enabled}
fallback_statuses    = [401, 403]
proxy                = "auto"
max_credits_per_run  = {firecrawl_budget}
max_cache_age_ms     = 0
rate_per_sec         = 1.0
concurrency          = 2
timeout_sec          = 60.0

[publish]
coverage_floor     = 0.001
hash_sample_rate   = 0.05
hash_sample_min    = 1
schema_sample_size = 5

[seed]
host_allow             = ["{host}"]
use_default_excludes   = true
"""


# ---- Suite state -----------------------------------------------------------

@dataclass
class FixtureRun:
    fixture: SiteFixture
    root: Path
    status: str = "pending"     # pending | skipped | seeded | published | failed
    seeded: int = 0
    fetched: int = 0
    md_count: int = 0
    md_mean_bytes: int = 0
    firecrawl_credits: int = 0
    wall_seconds: float = 0.0
    notes: list = field(default_factory=list)
    error: Optional[str] = None
    bench: Optional[dict] = None     # filled in by run_bench_for_fixture()


# ---- Helpers ---------------------------------------------------------------

def _sh(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _last_json(s: str) -> Optional[dict]:
    """Pluck the final JSON object off the tail of stdout. Sift commands
    write their summary as a `{...}` block at end-of-output."""
    j = s.rfind("}")
    if j < 0:
        return None
    depth = 0
    for i in range(j, -1, -1):
        c = s[i]
        if c == "}":
            depth += 1
        elif c == "{":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _latest_run_id(root: Path) -> Optional[str]:
    runs = root / "runs"
    if not runs.exists():
        return None
    candidates = sorted(p.name for p in runs.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


def _has_published_current(root: Path) -> bool:
    """True iff the latest run has a snapshot.json (publish completed).

    Avoids ``(root / "current").exists()`` because the publish-time symlink
    is relative — on macOS with ``/tmp`` itself a symlink to ``/private/tmp``,
    Path.exists() can return False for a symlink chain that the OS still
    resolves cleanly for actual file IO. Checking snapshot.json on the
    latest run dir directly is robust to that artifact.
    """
    run_id = _latest_run_id(root)
    if run_id is None:
        return False
    return (root / "runs" / run_id / "snapshot.json").exists()


# ---- Per-fixture pipeline --------------------------------------------------

def build_index(
    fixture: SiteFixture,
    *,
    root_dir: Path,
    limit: int,
    enable_firecrawl_fallback: bool,
    firecrawl_budget: int,
    rebuild: bool,
) -> FixtureRun:
    """Init + seed + run for one fixture, capped at `limit` URLs.

    Resumable: if `<root_dir>/<slug>/current` exists and `rebuild` is False,
    returns immediately with status='skipped' (honoring the user's "don't
    rebuild full indexes" intent).
    """
    root = root_dir / fixture.slug
    result = FixtureRun(fixture=fixture, root=root)
    t0 = time.time()

    if _has_published_current(root) and not rebuild:
        result.status = "skipped"
        result.notes.append(f"existing publish at {root}/current; pass "
                            "--rebuild to redo")
        return result

    if rebuild and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    cfg = root / "sift.toml"
    cfg.write_text(_CFG_TEMPLATE.format(
        host=fixture.host,
        firecrawl_enabled=("true" if enable_firecrawl_fallback else "false"),
        firecrawl_budget=firecrawl_budget,
    ))

    # 1. init
    p = _sh(["sift", "init", "--root", str(root)])
    if p.returncode != 0:
        result.status = "failed"
        result.error = f"init: {p.stderr[:200]}"
        result.wall_seconds = round(time.time() - t0, 2)
        return result

    # 2. seed — dispatch on fixture.discovery
    if fixture.discovery == "firecrawl":
        seed_flags = ["--from-firecrawl-map", fixture.source,
                      "--firecrawl-limit", str(limit * 2)]
    elif fixture.discovery == "auto-sitemap":
        seed_flags = ["--from-domain", fixture.source]
    else:
        seed_flags = ["--from-sitemap", fixture.source]

    try:
        p = _sh(["sift", "seed", "--root", str(root), "--config", str(cfg),
                 *seed_flags], timeout=180)
    except subprocess.TimeoutExpired:
        result.status = "failed"
        result.error = "seed: timeout (180s)"
        result.wall_seconds = round(time.time() - t0, 2)
        return result

    seed_summary = _last_json(p.stdout)
    if seed_summary:
        result.seeded = seed_summary.get("total_in_manifest", 0)
    if p.returncode != 0 or result.seeded == 0:
        result.status = "failed"
        result.error = (f"seed (rc={p.returncode}, seeded={result.seeded}): "
                        + (p.stderr or p.stdout)[-300:])
        result.wall_seconds = round(time.time() - t0, 2)
        return result
    result.status = "seeded"

    # 3. run with --limit
    run_cmd = ["sift", "run", "--root", str(root), "--config", str(cfg),
               "--limit", str(limit)]
    if enable_firecrawl_fallback:
        run_cmd.append("--firecrawl-fallback")
    try:
        p = _sh(run_cmd, timeout=600)
    except subprocess.TimeoutExpired:
        result.status = "failed"
        result.error = "run: timeout (600s)"
        result.wall_seconds = round(time.time() - t0, 2)
        return result

    run_summary = _last_json(p.stdout) or {}
    if p.returncode not in (0, 2):       # 0 = published, 2 = degraded
        result.status = "failed"
        result.error = (f"run (rc={p.returncode}): "
                        + (p.stderr or p.stdout)[-300:])
        result.wall_seconds = round(time.time() - t0, 2)
        return result

    result.firecrawl_credits = (
        (run_summary.get("firecrawl") or {}).get("credits_used", 0)
    )
    result.status = ("published" if run_summary.get("published")
                     else "degraded")

    # 4. count md files
    run_id = _latest_run_id(root)
    if run_id is None:
        result.notes.append("no run dir after run completed")
    else:
        run_dir = root / "runs" / run_id
        mds = list(run_dir.glob("md/**/*.md"))
        result.md_count = len(mds)
        if mds:
            total = sum(m.stat().st_size for m in mds)
            result.md_mean_bytes = round(total / len(mds))
            result.fetched = result.md_count

    result.wall_seconds = round(time.time() - t0, 2)
    return result


def run_bench_for_fixture(fr: FixtureRun, *, sample: int = 15) -> None:
    """Run the per-stage bench against a built index and stash the JSON
    on ``fr.bench``. Skips bench when the index never published (skipped /
    failed / degraded-with-no-current).
    """
    if not _has_published_current(fr.root):
        return
    from . import runner
    run_id = _latest_run_id(fr.root)
    if run_id is None:
        return
    fr.bench = runner.run_for_fixture(fr.root, run_id, fr.fixture,
                                       sample=sample)


# ---- Suite orchestrator ----------------------------------------------------

def run_suite(
    root_dir: Path,
    *,
    fixtures: Iterable[SiteFixture] = POSITIVE_FIXTURES,
    limit: int = 500,
    sample: int = 15,
    enable_firecrawl_fallback: bool = False,
    firecrawl_budget: int = 50,
    rebuild: bool = False,
    site_workers: int = 1,
    progress=print,
) -> dict:
    """Run the suite over all fixtures. Returns a dict suitable for
    JSON dump and ``aggregate_report.render()``.

    ``site_workers`` > 1 builds that many fixtures concurrently. Each site is
    fully independent (own ``<root_dir>/<slug>/`` + its own init/seed/run
    subprocesses), so wall-time drops toward the slowest single site rather
    than the sum. Determinism is unaffected — a page's content_hash doesn't
    depend on what other sites are doing.
    """
    root_dir.mkdir(parents=True, exist_ok=True)
    fixtures_list = list(fixtures)
    n = len(fixtures_list)
    overall_t0 = time.time()

    def _build_and_bench(fix: SiteFixture) -> FixtureRun:
        fr = build_index(
            fix, root_dir=root_dir, limit=limit,
            enable_firecrawl_fallback=enable_firecrawl_fallback,
            firecrawl_budget=firecrawl_budget,
            rebuild=rebuild,
        )
        run_bench_for_fixture(fr, sample=sample)
        return fr

    indexed = list(enumerate(fixtures_list, start=1))
    if site_workers and site_workers > 1:
        # Real CPU work lives in the per-site subprocesses, so a thread pool
        # just waits on them (no GIL contention). Results are re-keyed to the
        # original fixture order so the report stays stable regardless of
        # completion order.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        progress(f"building {n} fixture(s) across {site_workers} parallel "
                 f"workers (each site = independent subprocesses)")
        by_index: dict[int, FixtureRun] = {}
        with ThreadPoolExecutor(max_workers=site_workers) as ex:
            futs = {ex.submit(_build_and_bench, fix): (i, fix)
                    for i, fix in indexed}
            for done, fut in enumerate(as_completed(futs), start=1):
                i, fix = futs[fut]
                fr = fut.result()
                by_index[i] = fr
                progress(f"[{done}/{n}] {fix.use_case}/{fix.slug} ({fix.host}) "
                         f"→ status={fr.status} md={fr.md_count} "
                         f"mean={fr.md_mean_bytes}B credits={fr.firecrawl_credits} "
                         f"({fr.wall_seconds:.1f}s)")
        results = [by_index[i] for i, _ in indexed]
    else:
        results = []
        for i, fix in indexed:
            progress(f"[{i}/{n}] {fix.use_case}/{fix.slug} ({fix.host}) "
                     f"discovery={fix.discovery}")
            fr = _build_and_bench(fix)
            results.append(fr)
            progress(f"  → status={fr.status} seeded={fr.seeded} md={fr.md_count} "
                     f"mean={fr.md_mean_bytes}B credits={fr.firecrawl_credits} "
                     f"({fr.wall_seconds:.1f}s)")
    overall = round(time.time() - overall_t0, 2)
    return {
        "config": {
            "limit": limit,
            "sample_per_fixture": sample,
            "firecrawl_fallback": enable_firecrawl_fallback,
            "firecrawl_budget_per_site": firecrawl_budget,
            "rebuild": rebuild,
        },
        "total_wall_seconds": overall,
        "fixtures_attempted": len(results),
        "results": [_fixture_run_to_dict(fr) for fr in results],
    }


def _fixture_run_to_dict(fr: FixtureRun) -> dict:
    return {
        "use_case": fr.fixture.use_case,
        "slug": fr.fixture.slug,
        "host": fr.fixture.host,
        "discovery": fr.fixture.discovery,
        "status": fr.status,
        "seeded": fr.seeded,
        "fetched": fr.fetched,
        "md_count": fr.md_count,
        "md_mean_bytes": fr.md_mean_bytes,
        "firecrawl_credits": fr.firecrawl_credits,
        "wall_seconds": fr.wall_seconds,
        "notes": fr.notes,
        "error": fr.error,
        "bench": fr.bench,
    }
