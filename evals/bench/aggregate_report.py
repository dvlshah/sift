"""Aggregate report for the full 24-site suite — one row per fixture, grouped
by use case, with a headline grid at the top so a reader can see "did
everything pass" in one glance.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _verdict(passed: bool) -> str:
    return "✓" if passed else "✗"


def _percent(num: float, den: float) -> str:
    return f"{(num / den * 100):.0f}%" if den else "—"


def render(suite: dict) -> str:
    cfg = suite.get("config", {})
    results = suite.get("results", [])
    lines: list[str] = []

    lines.append("# sift eval-bench — full 24-site suite\n")
    lines.append(
        f"Config: limit={cfg.get('limit')} URLs/site · "
        f"sample={cfg.get('sample_per_fixture')} pages/site for bench · "
        f"firecrawl-fallback={'on' if cfg.get('firecrawl_fallback') else 'off'} "
        f"(budget {cfg.get('firecrawl_budget_per_site')} credits/site) · "
        f"wall {suite.get('total_wall_seconds', 0):.0f}s\n"
    )

    # ---- Headline grid -----------------------------------------------------

    n = len(results)
    n_published = sum(1 for r in results if r.get("status") == "published")
    n_degraded = sum(1 for r in results if r.get("status") == "degraded")
    n_failed = sum(1 for r in results
                   if r.get("status") in ("failed", "skipped"))
    total_md = sum(r.get("md_count", 0) for r in results)
    total_credits = sum(r.get("firecrawl_credits", 0) for r in results)

    lines.append("## Headline\n")
    lines.append(f"- **{n_published}/{n}** sites cleanly **published** "
                 f"(+{n_degraded} degraded, {n_failed} failed)")
    lines.append(f"- **{total_md:,}** md files indexed across the suite")
    lines.append(f"- **{total_credits}** Firecrawl credits used\n")

    # ---- Per-use-case rollup ----------------------------------------------

    by_uc: dict[str, list[dict]] = {}
    for r in results:
        by_uc.setdefault(r["use_case"], []).append(r)

    lines.append("## Per-fixture results (grouped by use case)\n")
    for use_case in sorted(by_uc):
        rs = by_uc[use_case]
        ok = sum(1 for r in rs if r.get("status") == "published")
        lines.append(f"\n### {use_case} — {ok}/{len(rs)} cleanly published\n")
        lines.append("| Site | Discovery | Seeded | md files | mean md | "
                     "Status | Credits | Wall |")
        lines.append("|---|---|---:|---:|---:|---|---:|---:|")
        for r in rs:
            verdict = {
                "published": "✓ published",
                "degraded":  "△ degraded",
                "skipped":   "↺ skipped",
                "failed":    "✗ failed",
                "seeded":    "△ seeded-only",
            }.get(r.get("status", ""), r.get("status", "?"))
            lines.append(
                f"| `{r['host']}` | `{r['discovery']}` | "
                f"{r.get('seeded', 0):,} | {r.get('md_count', 0)} | "
                f"{r.get('md_mean_bytes', 0):,}B | {verdict} | "
                f"{r.get('firecrawl_credits', 0)} | "
                f"{r.get('wall_seconds', 0):.0f}s |"
            )
        # failure notes inline
        for r in rs:
            if r.get("error"):
                lines.append(f"\n> **{r['slug']}** error: `{r['error'][:200]}`")

    # ---- Per-stage pass-rate grid (Stage 4 extract, Stage 3 fetch) --------

    lines.append("\n## Stage 4 — extract quality (across published fixtures)\n")
    lines.append("| Site | Structural | Anchor | Use-case | Verdict |")
    lines.append("|---|---:|---:|---:|---|")
    for r in results:
        if r.get("status") not in ("published", "degraded"):
            continue
        bench = r.get("bench") or {}
        stages = bench.get("stages") or {}
        ext = stages.get("4_extract") or {}
        struct = (ext.get("structural") or {})
        anch = (ext.get("anchor") or {})
        uc = (ext.get("use_case") or {})
        all_pass = (struct.get("passed") and anch.get("passed")
                    and uc.get("passed"))
        lines.append(
            f"| `{r['host']}` | "
            f"{struct.get('mean_ratio', 0):.2f} | "
            f"{anch.get('mean_anchor_ratio', 0):.2f} | "
            f"{uc.get('mean_score', 0):.2f} | "
            f"{_verdict(bool(all_pass))} |"
        )

    return "\n".join(lines) + "\n"


def write_report(suite: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(suite))
    return out_path
