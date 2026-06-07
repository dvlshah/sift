"""sift-evals — eval suite CLI.

Each subcommand writes a JSON result under <root>/evals/<run_id>/<name>.json.
The `baseline` subcommand runs everything and stitches the per-eval JSONs
into one consolidated baseline_report.json + a human-readable markdown
summary.

Usage:
    sift-evals baseline    --root ./index                # run all
    sift-evals performance --root ./index
    sift-evals efficiency  --root ./index
    sift-evals determinism --root ./index --sample 50
    sift-evals structural  --root ./index --sample 100
    sift-evals facts       --root ./index
    sift-evals llm-judge   --root ./index --sample 30    # needs ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import click

# Alias modules so command function names don't shadow them in the click decorators
from . import EVAL_VERSION
from . import determinism as det_mod
from . import efficiency as eff_mod
from . import facts_coverage as coverage_mod
from . import facts_validation as facts_mod
from . import performance as perf_mod
from . import structural as struct_mod
from sift import paths
from sift.manifest import open_db


def _root_opt(f):
    return click.option(
        "--root", required=True, type=click.Path(exists=True, path_type=Path),
        help="Index root directory (manifest.db + runs/ + current/)",
    )(f)


def _resolve_run_id(root: Path, run_id: Optional[str]) -> str:
    """If no --run-id passed, use the current/ symlink target."""
    if run_id:
        return run_id
    cur = root / "current"
    if cur.exists():
        return cur.resolve().name
    raise click.UsageError(
        "no --run-id given and no <root>/current symlink — explicit --run-id required"
    )


def _evals_dir(root: Path, run_id: str) -> Path:
    p = root / "evals" / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(out_path: Path, payload: dict) -> Path:
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


@click.group()
def main():
    """sift-evals — pipeline baseline metrics."""


@main.command("performance")
@_root_opt
@click.option("--run-id", default=None)
def performance_cmd(root: Path, run_id: Optional[str]):
    """Per-phase wall time + throughput + resource usage (from snapshot.json + run log)."""
    run_id = _resolve_run_id(root, run_id)
    res = perf_mod.run(root, run_id)
    out = _write(_evals_dir(root, run_id) / "performance.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(json.dumps(asdict(res), indent=2, default=str))


@main.command("efficiency")
@_root_opt
@click.option("--run-id", default=None)
def efficiency_cmd(root: Path, run_id: Optional[str]):
    """Disk footprint + per-page costs + dedup ratio."""
    run_id = _resolve_run_id(root, run_id)
    res = eff_mod.run(root, run_id)
    out = _write(_evals_dir(root, run_id) / "efficiency.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(json.dumps(asdict(res), indent=2, default=str))


@main.command("determinism")
@_root_opt
@click.option("--run-id", default=None)
@click.option("--sample", type=int, default=50)
def determinism_cmd(root: Path, run_id: Optional[str], sample: int):
    """Re-extract a sample of pages; verify content_hash matches the manifest."""
    run_id = _resolve_run_id(root, run_id)
    conn = open_db(paths.manifest_path(root))
    res = det_mod.run(root, run_id, conn=conn, sample=sample)
    out = _write(_evals_dir(root, run_id) / "determinism.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(
        f"matches={res.matches}/{res.sample_size} mismatches={res.mismatches} "
        f"version_skew={res.skipped_version_skew} "
        f"skipped={res.skipped_no_raw + res.skipped_extract_failed}"
    )
    if res.skipped_version_skew:
        click.echo(
            f"note: {res.skipped_version_skew} page(s) were extracted by an "
            "older extractor version — differing hashes there are EXPECTED "
            "skew, not non-determinism. Run `sift re-extract` to refresh them."
        )


@main.command("structural")
@_root_opt
@click.option("--run-id", default=None)
@click.option("--sample", type=int, default=100)
def structural_cmd(root: Path, run_id: Optional[str], sample: int):
    """HTML vs markdown structural diff over a sample. Flags pages with anomalous ratios."""
    run_id = _resolve_run_id(root, run_id)
    conn = open_db(paths.manifest_path(root))
    res = struct_mod.run(root, run_id, conn=conn, sample=sample)
    out = _write(_evals_dir(root, run_id) / "structural.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(
        f"evaluated={res.pages_evaluated}/{res.sample_size}  "
        f"flagged={res.flagged_count} "
        f"median: headings={res.median_heading_ratio} "
        f"tables={res.median_table_ratio} text={res.median_text_ratio}"
    )


@main.command("facts")
@_root_opt
@click.option("--run-id", default=None)
def facts_cmd(root: Path, run_id: Optional[str]):
    """Validate every facts/*.json against its declared $schema."""
    run_id = _resolve_run_id(root, run_id)
    res = facts_mod.run(root, run_id)
    out = _write(_evals_dir(root, run_id) / "facts_validation.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(
        f"valid={res.facts_valid}/{res.facts_total} "
        f"invalid={res.facts_invalid} schemas_found={res.schemas_found}"
    )


@main.command("facts-coverage")
@_root_opt
@click.option("--run-id", default=None)
@click.option("--scan-limit", type=int, default=None,
              help="Cap pages scanned (for fast smoke tests)")
def facts_coverage_cmd(root: Path, run_id: Optional[str], scan_limit: Optional[int]):
    """Detect rate-table-shaped pages that produced no facts file (extractor gaps)."""
    run_id = _resolve_run_id(root, run_id)
    conn = open_db(paths.manifest_path(root))
    res = coverage_mod.run(root, run_id, conn=conn, scan_limit=scan_limit)
    out = _write(_evals_dir(root, run_id) / "facts_coverage.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(
        f"candidates={res.pages_with_rate_table_shape} gaps={len(res.coverage_gaps)} "
        f"coverage_ratio={res.coverage_ratio}"
    )
    if res.coverage_gaps:
        click.echo("\ntop gap sections:")
        for section, n in sorted(res.gaps_by_section.items(), key=lambda x: -x[1])[:5]:
            click.echo(f"  {n:>4}  /{section}/")


@main.command("llm-judge")
@_root_opt
@click.option("--run-id", default=None)
@click.option("--sample", type=int, default=30)
def llm_judge_cmd(root: Path, run_id: Optional[str], sample: int):
    """Score extraction fidelity with Claude Opus 4.7 over a stratified sample.

    Requires ANTHROPIC_API_KEY. Uses prompt caching on the rubric, so per-call
    cost drops sharply after the first call."""
    from . import llm_judge
    run_id = _resolve_run_id(root, run_id)
    conn = open_db(paths.manifest_path(root))
    res = llm_judge.run(root, run_id, conn=conn, sample=sample)
    out = _write(_evals_dir(root, run_id) / "llm_judge.json", asdict(res))
    click.echo(f"wrote {out}")
    click.echo(
        f"judged={res.pages_judged}/{res.sample_size} "
        f"mean_overall={res.mean_overall_faithfulness}/5 "
        f"cost=${res.estimated_cost_usd} wall={res.wall_sec}s"
    )


@main.command()
@_root_opt
@click.option("--run-id", default=None)
@click.option("--sample-structural", type=int, default=100)
@click.option("--sample-determinism", type=int, default=50)
@click.option("--sample-llm", type=int, default=30)
@click.option("--skip-llm", is_flag=True, help="Skip the LLM-judge eval (skip API cost)")
def baseline(
    root: Path, run_id: Optional[str],
    sample_structural: int, sample_determinism: int, sample_llm: int,
    skip_llm: bool,
):
    """Run every eval against the current snapshot and write baseline_report.json."""
    run_id = _resolve_run_id(root, run_id)
    conn = open_db(paths.manifest_path(root))
    out_dir = _evals_dir(root, run_id)
    report: dict = {
        "eval_version": EVAL_VERSION,
        "run_id": run_id,
        "results": {},
        "errors": {},
    }

    def _safe(name: str, fn):
        click.echo(f"[{name}] running...")
        try:
            res = fn()
            _write(out_dir / f"{name}.json", asdict(res))
            report["results"][name] = asdict(res)
            click.echo(f"[{name}] ok")
        except Exception as e:
            report["errors"][name] = str(e)
            click.echo(f"[{name}] FAILED: {e}", err=True)

    _safe("performance",      lambda: perf_mod.run(root, run_id))
    _safe("efficiency",       lambda: eff_mod.run(root, run_id))
    _safe("determinism",      lambda: det_mod.run(root, run_id, conn=conn, sample=sample_determinism))
    _safe("structural",       lambda: struct_mod.run(root, run_id, conn=conn, sample=sample_structural))
    _safe("facts_validation", lambda: facts_mod.run(root, run_id))
    _safe("facts_coverage",   lambda: coverage_mod.run(root, run_id, conn=conn))
    if not skip_llm:
        from . import llm_judge
        _safe("llm_judge",    lambda: llm_judge.run(root, run_id, conn=conn, sample=sample_llm))
    else:
        click.echo("[llm_judge] skipped (--skip-llm)")

    (out_dir / "baseline_report.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    summary = _render_markdown_summary(report)
    (out_dir / "baseline_report.md").write_text(summary)
    click.echo(f"\nbaseline_report.json -> {out_dir / 'baseline_report.json'}")
    click.echo(f"baseline_report.md   -> {out_dir / 'baseline_report.md'}")
    click.echo()
    click.echo(summary)


@main.command("bench-suite")
@click.option("--root-dir", required=True, type=click.Path(path_type=Path),
              help="Shared parent dir; each fixture gets <root-dir>/<slug>/")
@click.option("--limit", type=int, default=500, show_default=True,
              help="Max URLs to fetch per site (caps full-corpus crawls)")
@click.option("--sample", type=int, default=15, show_default=True,
              help="Pages per fixture to score in the bench")
@click.option("--firecrawl-fallback", is_flag=True, default=False,
              help="Enable Firecrawl /v2/scrape for 401/403 native failures. "
                   "Costs credits; capped per-site by --firecrawl-budget.")
@click.option("--firecrawl-budget", type=int, default=50, show_default=True,
              help="Per-site credit cap when --firecrawl-fallback is on")
@click.option("--rebuild", is_flag=True, default=False,
              help="Rebuild even when an existing publish is present")
@click.option("--slug", multiple=True, default=(),
              help="Only run these slugs (repeat: --slug ato --slug mdn)")
@click.option("--use-case", multiple=True, default=(),
              help="Only run fixtures in these use cases")
@click.option("--site-workers", type=int, default=1, show_default=True,
              help="Build this many sites concurrently. Each site is "
                   "independent (own index + init/seed/run subprocesses), so "
                   "wall-time drops toward the slowest single site. Cap near "
                   "CPU count; with --firecrawl-fallback, concurrent sites "
                   "share the Firecrawl API rate limit.")
def bench_suite_cmd(root_dir: Path, limit: int, sample: int,
                    firecrawl_fallback: bool, firecrawl_budget: int,
                    rebuild: bool, slug: tuple[str, ...],
                    use_case: tuple[str, ...], site_workers: int):
    """Run the full 24-site eval bench. Builds a capped index per fixture
    + runs all per-stage evals + writes an aggregate report.

    Output:
      <root-dir>/suite.json     # raw per-fixture results
      <root-dir>/suite.md       # aggregate markdown report
      <root-dir>/<slug>/        # one sift index per fixture
    """
    from .bench import aggregate_report
    from .bench.full_suite import run_suite
    from .bench.fixtures.sites import POSITIVE_FIXTURES

    fixtures = POSITIVE_FIXTURES
    if slug:
        fixtures = tuple(f for f in fixtures if f.slug in set(slug))
    if use_case:
        fixtures = tuple(f for f in fixtures if f.use_case in set(use_case))
    if not fixtures:
        raise click.UsageError("no fixtures match the --slug / --use-case filters")

    click.echo(f"running suite over {len(fixtures)} fixture(s), limit={limit}")
    result = run_suite(
        root_dir,
        fixtures=fixtures,
        limit=limit,
        sample=sample,
        enable_firecrawl_fallback=firecrawl_fallback,
        firecrawl_budget=firecrawl_budget,
        rebuild=rebuild,
        site_workers=site_workers,
        progress=lambda s: click.echo(s),
    )
    json_path = root_dir / "suite.json"
    md_path = root_dir / "suite.md"
    json_path.write_text(json.dumps(result, indent=2, default=str))
    aggregate_report.write_report(result, md_path)
    click.echo(f"\nsuite.json -> {json_path}")
    click.echo(f"suite.md   -> {md_path}")


@main.command("agent-bench")
@click.option("--sift-root", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to a sift index root (the dir with manifest.db + current/)")
@click.option("--sift-run-id", type=str, default=None,
              help="Specific run-id to query; defaults to the current/ symlink target")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path),
              help="Where to write agent_bench.json + report.md (created if missing)")
@click.option("--condition", "conditions", multiple=True, default=(),
              help="Restrict to specific conditions (repeat). "
                   "Default: closed-book, sift-grep, web-fetch.")
@click.option("--qid", "qids", multiple=True, default=(),
              help="Restrict to specific question ids (repeat). Default: all.")
@click.option("--agent-model", type=str, default="claude-opus-4-7", show_default=True,
              help="Anthropic model id for the agent under test")
@click.option("--judge-model", type=str, default="claude-opus-4-7", show_default=True,
              help="Anthropic model id for the LLM judge")
@click.option("--no-resume", is_flag=True, default=False,
              help="Re-run from scratch even if agent_bench.json already exists")
def agent_bench_cmd(sift_root: Path, sift_run_id: Optional[str],
                    output_dir: Path,
                    conditions: tuple[str, ...], qids: tuple[str, ...],
                    agent_model: str, judge_model: str, no_resume: bool):
    """Agent-in-the-loop bench: closed-book vs sift-grep vs web-fetch on
    ~20 hand-curated questions.

    Output:
        <output-dir>/agent_bench.json   # raw per-cell results
        <output-dir>/report.md          # human-readable summary

    Requires ANTHROPIC_API_KEY in the environment.
    """
    from .agent_loop.questions import QUESTIONS, by_qid
    from .agent_loop.report import write_report
    from .agent_loop.runner import run_suite
    from .agent_loop.tools import CONDITIONS

    cond_list = list(conditions) if conditions else list(CONDITIONS)
    if qids:
        qs = []
        for qid in qids:
            q = by_qid(qid)
            if q is None:
                raise click.UsageError(f"unknown qid: {qid}")
            qs.append(q)
        questions = tuple(qs)
    else:
        questions = QUESTIONS

    output_dir.mkdir(parents=True, exist_ok=True)
    suite = run_suite(
        sift_root=sift_root, sift_run_id=sift_run_id,
        questions=questions, conditions=cond_list,
        agent_model=agent_model, judge_model=judge_model,
        output_dir=output_dir, resume=not no_resume,
        progress=lambda s: click.echo(s),
    )
    payload = suite.to_dict()
    json_path = output_dir / "agent_bench.json"
    md_path = output_dir / "report.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    md = write_report(payload)
    md_path.write_text(md)
    click.echo(f"\nagent_bench.json -> {json_path}")
    click.echo(f"report.md        -> {md_path}")
    click.echo()
    click.echo(md)


@main.command("bench-drift")
@click.option("--current", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the current suite.json (or a directory containing it)")
@click.option("--baseline", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the baseline suite.json (or directory containing it)")
@click.option("--rate-eps", type=float, default=0.05, show_default=True,
              help="Numeric-rate threshold (in absolute points) below which "
                   "deltas are treated as jitter, not drift.")
@click.option("--count-pct-eps", type=float, default=0.20, show_default=True,
              help="Count-delta threshold (as fraction of max(base, cur)) "
                   "below which seeded/fetched/md_count drift is ignored.")
@click.option("--json-out", type=click.Path(path_type=Path), default=None,
              help="Write structured drift output to this JSON file")
@click.option("--md-out", type=click.Path(path_type=Path), default=None,
              help="Write the human-readable drift report to this markdown file")
@click.option("--fail-on-regression", is_flag=True, default=False,
              help="Exit non-zero if any regression delta is detected. "
                   "Used by CI to gate merges.")
def bench_drift_cmd(current: Path, baseline: Path,
                    rate_eps: float, count_pct_eps: float,
                    json_out: Optional[Path], md_out: Optional[Path],
                    fail_on_regression: bool):
    """Diff two bench ``suite.json`` runs and surface deltas.

    Usage:
        sift-evals bench-drift \\
            --current  ./out/suite.json \\
            --baseline evals/bench/results/v1.0-baseline-2026-05-31.json \\
            --fail-on-regression
    """
    from .bench import drift as drift_mod

    def _resolve(p: Path) -> Path:
        # Convenience: accept either the JSON file directly or the directory
        # the suite was written into.
        return p / "suite.json" if p.is_dir() else p

    cur_path = _resolve(current)
    base_path = _resolve(baseline)
    cur_body = drift_mod.load_suite(cur_path)
    base_body = drift_mod.load_suite(base_path)
    report = drift_mod.compute_drift(
        base_body, cur_body,
        rate_eps=rate_eps, count_pct_eps=count_pct_eps,
        baseline_path=base_path, current_path=cur_path,
    )

    md = drift_mod.render_markdown(report)
    click.echo(md)
    if md_out is not None:
        md_out.write_text(md)
        click.echo(f"\nwrote {md_out}")
    if json_out is not None:
        json_out.write_text(json.dumps(report.to_dict(), indent=2))
        click.echo(f"wrote {json_out}")

    if fail_on_regression and report.has_regressions:
        raise click.ClickException(
            f"{report.regressions} regression(s) detected vs baseline"
        )


@main.command("bench")
@click.option("--root", required=True, type=click.Path(exists=True, path_type=Path),
              help="Index root (manifest.db + runs/ live here)")
@click.option("--run-id", type=str, default=None,
              help="Run to evaluate (default: latest published)")
@click.option("--slug", type=str, default=None,
              help="Run for one fixture only (e.g. 'mdn'); default: all 12")
@click.option("--sample", type=int, default=20,
              help="Pages per fixture to score (default 20)")
def bench_cmd(root: Path, run_id: Optional[str], slug: Optional[str],
              sample: int):
    """Run the eval-bench: per-stage per-fixture quality scoring.

    Output: <root>/evals/<run_id>/bench/bench.json + report.md.

    See ``evals/bench/runner.py`` for the implemented evals (extract /
    fetch / publish / mcp); seed / plan / commit are scaffolded for follow-on.
    """
    from .bench import runner, report as bench_report
    from .bench.fixtures.sites import POSITIVE_FIXTURES, by_slug

    run_id = _resolve_run_id(root, run_id)
    fixtures = (by_slug(slug),) if slug else POSITIVE_FIXTURES
    if slug and fixtures[0] is None:
        raise click.UsageError(f"unknown fixture slug: {slug}")
    click.echo(f"running eval-bench against run_id={run_id}, "
               f"fixtures={[f.slug for f in fixtures]}")
    results = runner.run_for_index(root, run_id,
                                    fixtures=fixtures, sample=sample)
    out_dir = runner.bench_output_dir(root, run_id)
    json_path = runner.write_results(results, out_dir)
    md_path = bench_report.write_report(results, out_dir)
    click.echo(f"\nbench.json -> {json_path}")
    click.echo(f"report.md  -> {md_path}")
    click.echo()
    click.echo(md_path.read_text())


def _render_markdown_summary(report: dict) -> str:
    """Human-readable summary across all evals in one report."""
    lines: list[str] = [f"# Baseline report — run `{report.get('run_id')}`", ""]
    r = report.get("results", {})

    perf = r.get("performance") or {}
    tim = perf.get("phase_timings") or {}
    res = perf.get("resources") or {}
    if perf:
        lines += [
            "## Performance",
            f"- **Total wall**: {tim.get('total_sec', '?')}s",
            f"- **Fetch**: {tim.get('fetch_sec', '?')}s "
            f"({perf.get('fetch_throughput_req_per_sec', '?')} req/s)",
            f"- **Extract**: {tim.get('extract_sec', '?')}s "
            f"({perf.get('extract_ms_per_page_mean', '?')} ms/page mean)",
            f"- **Commit**: {tim.get('commit_sec', '?')}s",
            f"- **Publish**: {tim.get('publish_sec', '?')}s",
            f"- **Peak RSS**: {(res.get('peak_rss_bytes') or 0) // (1024*1024)} MB",
            f"- **CPU**: {res.get('cpu_utilization_pct', '?')}% utilization",
            "",
        ]

    eff = r.get("efficiency") or {}
    disk = eff.get("disk") or {}
    pp = eff.get("per_page") or {}
    dedup = eff.get("dedup") or {}
    if eff:
        lines += [
            "## Efficiency",
            f"- **Total disk**: {disk.get('total_bytes', 0) // (1024*1024)} MB",
            f"  - Raw HTML (gzipped): {disk.get('raw_blobs_bytes', 0) // (1024*1024)} MB"
            f" ({disk.get('raw_blobs_count', 0)} blobs)",
            f"  - Markdown: {disk.get('md_files_bytes', 0) // (1024*1024)} MB"
            f" ({disk.get('md_files_count', 0)} files)",
            f"  - Manifest DB: {disk.get('manifest_db_bytes', 0) // (1024*1024)} MB",
            f"  - Facts JSON: {disk.get('facts_bytes', 0) // 1024} KB ({disk.get('facts_count', 0)} files)",
            f"  - Changelog: {eff.get('changelog_entries', 0)} entries",
            f"- **Per-page cost**: raw {pp.get('raw_bytes_per_page', '?')} B"
            f"  / md {pp.get('md_bytes_per_page', '?')} B"
            f"  / manifest {pp.get('manifest_bytes_per_row', '?')} B/row",
            f"- **Raw-blob dedup**: {dedup.get('unique_raw_hashes', 0)} unique / "
            f"{dedup.get('fresh_rows_with_raw_hash', 0)} fresh rows "
            f"(ratio {dedup.get('blob_to_row_ratio', '?')})",
            "",
        ]

    det = r.get("determinism") or {}
    if det:
        lines += [
            "## Determinism (re-extract → hash matches)",
            f"- **Matches**: {det.get('matches', 0)} / {det.get('sample_size', 0)} sampled",
            f"- **Mismatches**: {det.get('mismatches', 0)} (P0 if non-zero — same "
            "extractor version, different hash = non-determinism)",
            f"- **Version skew**: {det.get('skipped_version_skew', 0)} (expected — "
            "page predates an extractor bump; run `sift re-extract`)",
            f"- **Skipped**: no-raw={det.get('skipped_no_raw', 0)}, "
            f"extract-failed={det.get('skipped_extract_failed', 0)}",
            "",
        ]

    struct = r.get("structural") or {}
    if struct:
        lines += [
            "## Structural quality (HTML vs markdown counts)",
            f"- **Evaluated**: {struct.get('pages_evaluated', 0)} / {struct.get('sample_size', 0)} sampled",
            f"- **Flagged anomalous**: {struct.get('flagged_count', 0)} pages",
            f"- **Median ratios**: headings={struct.get('median_heading_ratio', '?')} "
            f"tables={struct.get('median_table_ratio', '?')} "
            f"links={struct.get('median_link_ratio', '?')} "
            f"text={struct.get('median_text_ratio', '?')}",
            "",
        ]

    facts = r.get("facts_validation") or {}
    if facts:
        lines += [
            "## Facts validation",
            f"- **Valid**: {facts.get('facts_valid', 0)} / {facts.get('facts_total', 0)} facts files",
            f"- **Invalid**: {facts.get('facts_invalid', 0)}",
            f"- **Schemas registered**: {facts.get('schemas_found', 0)}",
            "",
        ]

    cov = r.get("facts_coverage") or {}
    if cov:
        gap_count = len(cov.get('coverage_gaps') or [])
        lines += [
            "## Facts coverage (extractor gaps)",
            f"- **Pages scanned**: {cov.get('pages_scanned', 0)}",
            f"- **Rate-table-shaped pages**: {cov.get('pages_with_rate_table_shape', 0)}",
            f"- **Pages with facts emitted**: {cov.get('facts_files_emitted', 0)}",
            f"- **Gaps (shape but no facts)**: {gap_count}",
            f"- **Coverage ratio**: {cov.get('coverage_ratio', '?')}",
        ]
        if cov.get('gaps_by_section'):
            lines.append("- **Gaps by section**:")
            for section, n in sorted(cov['gaps_by_section'].items(), key=lambda x: -x[1])[:5]:
                lines.append(f"    - `/{section}/`: {n}")
        lines.append("")

    llm = r.get("llm_judge") or {}
    if llm:
        lines += [
            "## LLM-judge fidelity scores (1-5)",
            f"- **Judged**: {llm.get('pages_judged', 0)} / {llm.get('sample_size', 0)}",
            f"- **Overall**: {llm.get('mean_overall_faithfulness', '?')}/5",
            f"- **Title**: {llm.get('mean_title_accuracy', '?')}  "
            f"**Body**: {llm.get('mean_body_coverage', '?')}  "
            f"**Headings**: {llm.get('mean_heading_preservation', '?')}",
            f"- **Tables**: {llm.get('mean_table_preservation', 'n/a')}  "
            f"**Links**: {llm.get('mean_link_preservation', 'n/a')}",
            f"- **Cost**: ${llm.get('estimated_cost_usd', 0)} / "
            f"{llm.get('wall_sec', '?')}s wall",
            "",
        ]

    if report.get("errors"):
        lines += ["## Errors during baseline"]
        for name, msg in report["errors"].items():
            lines.append(f"- **{name}**: {msg}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
