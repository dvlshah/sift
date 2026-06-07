"""Eval bench orchestrator.

Two entry shapes:

* :func:`run_for_index` — given a sift index root + run_id + optional
  fixture filter, run every implemented per-stage eval and write
  ``results.json`` + ``report.md`` to ``<root>/evals/<run_id>/bench/``.
* :func:`run_for_fixture` — same but scoped to a single SiteFixture, for
  per-site drill-down.

This is the v1 of the bench. Stage 1 (seed), Stage 2 (plan), and Stage 5
(commit) are scaffolded but not implemented; the runner surfaces their
"not implemented" status rather than skipping them silently so the user
can see what's coming.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from sift import paths

from .fixtures.sites import POSITIVE_FIXTURES, SiteFixture, USE_CASES, by_slug
from .per_stage import commit as stage_commit
from .per_stage import extract as stage_extract
from .per_stage import fetch as stage_fetch
from .per_stage import mcp as stage_mcp
from .per_stage import plan as stage_plan
from .per_stage import publish as stage_publish
from .per_stage import seed as stage_seed


def run_for_fixture(
    root: Path,
    run_id: str,
    fixture: SiteFixture,
    *,
    sample: int = 20,
) -> dict:
    """Run every implemented per-host eval against one fixture's host.

    Stage 2 (plan) is synthetic — it runs once per bench, not per fixture,
    and is surfaced under the index-wide rollup instead of here.
    """
    return {
        "fixture": {"use_case": fixture.use_case, "slug": fixture.slug,
                    "host": fixture.host, "discovery": fixture.discovery},
        "stages": {
            # Stages 1, 2, 5 are synthetic / index-wide — rolled up under
            # `index_wide` instead of duplicated per fixture.
            "3_fetch":    stage_fetch.run_fetch_evals(root, run_id,
                                                      fixture=fixture),
            "4_extract":  stage_extract.run_extract_evals(root, run_id, fixture,
                                                         sample=sample),
        },
    }


def run_for_index(
    root: Path,
    run_id: str,
    *,
    fixtures: tuple[SiteFixture, ...] = POSITIVE_FIXTURES,
    sample: int = 20,
) -> dict:
    """Run the bench for every passed fixture (default: all 24 positive
    cases). Index-wide evals (synthetic + non-fixture-bound) run once.
    """
    per_fixture = [
        run_for_fixture(root, run_id, f, sample=sample)
        for f in fixtures
    ]
    # Index-wide rollups: stages that run once per bench, not per host.
    index_wide = {
        "1_seed":    stage_seed.run_seed_evals(),
        "2_plan":    stage_plan.run_plan_evals(),
        "5_commit":  stage_commit.run_commit_evals(root),
        "6_publish": stage_publish.run_publish_evals(root, run_id),
        "7_mcp":     stage_mcp.run_mcp_evals(root, run_id, sample=sample),
    }
    return {
        "run_id": run_id,
        "fixtures": per_fixture,
        "index_wide": index_wide,
    }


def write_results(results: dict, out_dir: Path) -> Path:
    """Write the bench results JSON + return the path. Caller renders the
    markdown report via :mod:`evals.bench.report`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "bench.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    return out


def bench_output_dir(root: Path, run_id: str) -> Path:
    """Output dir for this run's bench artifacts. Mirrors the
    ``_evals_dir`` helper in ``evals/cli.py`` — kept local to avoid a tight
    import coupling between the bench package and the per-eval CLI."""
    return root / "evals" / run_id / "bench"
