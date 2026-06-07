"""Render bench results as a markdown roll-up.

One section per pipeline stage with a table of fixtures × scores. Failure
cases get a short explanation; passing fixtures get the headline number.
Designed so a human reading the report at the end of a `sift-evals bench`
run can answer: did every stage pass for every use case?
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _row(host: str, *cells: Any) -> str:
    return "| " + " | ".join([f"`{host}`"] + [str(c) for c in cells]) + " |"


def _verdict(passed: bool) -> str:
    return "✓ pass" if passed else "✗ fail"


def render(results: dict) -> str:
    lines: list[str] = []
    lines.append("# sift eval-bench — full per-stage report\n")
    lines.append(f"Run: `{results.get('run_id', '?')}`\n")

    # Per-stage aggregation across the 12 fixtures.
    fixtures = results.get("fixtures", [])
    n = len(fixtures)
    lines.append(f"Fixtures evaluated: **{n}**.\n")

    # Stage 4 (extract) — the implemented heart of v1.
    lines.append("\n## Stage 4 — extract\n")
    lines.append("| Site | Use case | Structural mean | Anchor ratio | "
                 "Use-case quality | Verdict |")
    lines.append("|---|---|---:|---:|---:|---|")
    for f in fixtures:
        fix = f["fixture"]
        ext = f["stages"]["4_extract"]
        struct = ext["structural"]
        anch = ext["anchor"]
        uc = ext["use_case"]
        all_pass = struct["passed"] and anch["passed"] and uc["passed"]
        lines.append(_row(
            fix["host"], fix["use_case"],
            f"{struct['mean_ratio']:.2f}",
            f"{anch['mean_anchor_ratio']:.2f}",
            f"{uc['mean_score']:.2f}",
            _verdict(all_pass),
        ))

    # Stage 3 (fetch)
    lines.append("\n## Stage 3 — fetch\n")
    lines.append("| Site | FRESH | Fetchable | Success rate | "
                 "Firecrawl-touched | Cond-GET efficiency | Verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for f in fixtures:
        fix = f["fixture"]
        fetch = f["stages"]["3_fetch"]
        succ = fetch["success"]
        fc = fetch["firecrawl"]
        cg = fetch.get("conditional_get") or {}
        cg_str = (f"{cg.get('efficiency', 0):.2%} "
                  f"({cg.get('not_modified_responses', 0)}/"
                  f"{cg.get('fetch_conditional_decisions', 0)})"
                  if cg.get("fetch_conditional_decisions") else "n/a")
        lines.append(_row(
            fix["host"],
            succ["fresh"],
            succ["fetchable"],
            f"{succ['rate']:.2%}",
            fc["firecrawl_fresh_rows"],
            cg_str,
            _verdict(succ["passed"]),
        ))

    # Stage 1 (seed) — synthetic, index-wide
    seed = results.get("index_wide", {}).get("1_seed", {})
    if seed:
        lines.append("\n## Stage 1 — seed (synthetic)\n")
        d = seed.get("dedup", {})
        h = seed.get("host_allow", {})
        lines.append(f"- **Dedup correctness**: {d.get('correct', 0)}/"
                     f"{d.get('cases', 0)} canonical equivalence classes "
                     f"({d.get('rate', 0):.2%}) — {_verdict(d.get('passed', False))}")
        lines.append(f"- **Host-allow correctness**: {h.get('inserted', 0)} "
                     f"on-host inserted, {h.get('skipped_host', 0)} off-host "
                     f"filtered — {_verdict(h.get('passed', False))}")

    # Stage 2 (plan) — synthetic, index-wide
    plan = results.get("index_wide", {}).get("2_plan", {}).get(
        "decision_correctness", {})
    if plan:
        lines.append("\n## Stage 2 — plan (synthetic decision-correctness)\n")
        lines.append(f"- Cases: **{plan.get('n_cases', 0)}** synthetic states "
                     f"(UNSEEN, within-floor, past-ceiling, conditional, "
                     f"within-TTL, past-TTL)")
        lines.append(f"- Correct decisions: **{plan.get('n_correct', 0)}/"
                     f"{plan.get('n_cases', 0)}** "
                     f"({plan.get('rate', 0):.2%}) — "
                     f"{_verdict(plan.get('passed', False))}")
        if plan.get("failures"):
            lines.append("\nFailures:")
            for ff in plan["failures"]:
                lines.append(f"- `{ff['url']}`: expected={ff['expected']}, "
                             f"actual={ff['actual']}")

    # Stage 5 (commit) — index-wide
    commit = results.get("index_wide", {}).get("5_commit", {}).get(
        "changelog", {})
    if commit:
        lines.append("\n## Stage 5 — commit (changelog chain integrity)\n")
        lines.append(f"- Entries: **{commit.get('entries', 0)}**, "
                     f"chain breaks: **{commit.get('breaks', 0)}** — "
                     f"{_verdict(commit.get('passed', False))}")
        if commit.get("note"):
            lines.append(f"- {commit['note']}")

    # Stage 6 (publish) — index-wide
    pub = results.get("index_wide", {}).get("6_publish", {}).get("gates", {})
    if pub:
        lines.append("\n## Stage 6 — publish (index-wide)\n")
        lines.append(f"Status: **{pub.get('status', '?')}**, "
                     f"published: **{pub.get('published', False)}**, "
                     f"failed gates: **{len(pub.get('failed_gates') or [])}**")
        if pub.get("failed_gates"):
            lines.append("\nFailing gates:")
            for g in pub["failed_gates"]:
                lines.append(f"- `{g.get('name')}`: {g.get('detail')}")

    # Stage 7 (MCP)
    mcp = results.get("index_wide", {}).get("7_mcp", {}).get("verify", {})
    if mcp:
        lines.append("\n## Stage 7 — MCP read\n")
        lines.append(f"- `read_md verify=true` round-trip: "
                     f"{mcp['n_verified']}/{mcp['n_sampled']} OK "
                     f"({mcp['rate']:.2%}) — {_verdict(mcp['passed'])}")
        if mcp.get("failures"):
            lines.append("\nVerify failures (first 5):")
            for ff in mcp["failures"]:
                lines.append(f"- `{ff['path']}`: {ff['error']}")

    # Stages 1, 2, 5 — surface "scaffolded" status so it's obvious what's
    # coming in follow-on work
    lines.append("\n## Pipeline stages — implementation status\n")
    lines.append("| Stage | Status |")
    lines.append("|---|---|")
    lines.append("| 1 seed     | **implemented** — dedup + host-allow synthetic |")
    lines.append("| 2 plan     | **implemented** — synthetic decision-correctness |")
    lines.append("| 3 fetch    | **implemented** — success, Firecrawl-touched, "
                 "conditional-GET efficiency |")
    lines.append("| 4 extract  | **implemented** — structural, anchor, "
                 "use-case |")
    lines.append("| 5 commit   | **implemented** — changelog chain integrity |")
    lines.append("| 6 publish  | **implemented** — gate rollup from "
                 "snapshot.json |")
    lines.append("| 7 MCP read | **implemented** — read_md verify round-trip "
                 "(grep precision/recall deferred — needs annotated queries) |")

    return "\n".join(lines) + "\n"


def write_report(results: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "report.md"
    p.write_text(render(results))
    return p
