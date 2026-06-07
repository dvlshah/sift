"""Render the agent-loop bench result as a human-readable markdown report.

The report's job is to answer the headline question — *does sift help the
agent get more answers right?* — in numbers a non-engineer can read. Four
sections, in priority order:

  1. **Headline lift**: mean correctness per condition + sift's lift over
     closed-book / over web-fetch. This IS the result.
  2. **Per-use-case breakdown**: where sift wins biggest. Tax + change-
     monitoring should dominate (freshness); coding-agents may not move
     much (parametric knowledge is strong).
  3. **Per-question table**: every cell, sortable in a markdown viewer.
     For drill-down when a number looks surprising.
  4. **Cost + tool-use**: tokens spent per condition + how many tools the
     agent reached for under sift-grep / web-fetch.

Nothing here calls an LLM — input is a SuiteResult dict, output is a string.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Iterable


def _round(x: float, n: int = 2) -> float:
    return round(float(x), n)


def _by_condition(cells: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for c in cells:
        out[c["condition"]].append(c)
    return out


def _mean_correctness(cells: Iterable[dict]) -> float:
    scores = [int((c.get("judge") or {}).get("correctness") or 0)
              for c in cells]
    scores = [s for s in scores if s > 0]
    if not scores:
        return 0.0
    return _round(mean(scores))


def _pct(cells: Iterable[dict], predicate) -> float:
    cells = list(cells)
    if not cells:
        return 0.0
    n = sum(1 for c in cells if predicate(c))
    return _round(n / len(cells), 3)


def _refused(c: dict) -> bool:
    return bool((c.get("agent") or {}).get("refused"))


def _passing(c: dict, threshold: int = 4) -> bool:
    return int((c.get("judge") or {}).get("correctness") or 0) >= threshold


def _has_citation(c: dict) -> bool:
    return bool((c.get("judge") or {}).get("citation_present"))


def _faithful_citation(c: dict) -> bool:
    return bool((c.get("judge") or {}).get("citation_faithful"))


def _qid_to_question(suite: dict) -> dict[str, dict]:
    return {q["qid"]: q for q in (suite.get("questions") or [])}


def write_report(suite: dict) -> str:
    """Render ``suite`` (the return of ``SuiteResult.to_dict()``) as
    markdown."""
    results = suite.get("results") or []
    by_cond = _by_condition(results)
    conditions = suite.get("config", {}).get("conditions") or sorted(by_cond)
    qmap = _qid_to_question(suite)

    lines: list[str] = ["# Agent-loop bench", ""]
    lines.append(f"- **Agent model**: `{suite['config'].get('agent_model')}`")
    lines.append(f"- **Judge model**: `{suite['config'].get('judge_model')}`")
    lines.append(f"- **Sift index**:  `{suite['config'].get('sift_root')}`"
                 f" (run `{suite['config'].get('sift_run_id') or 'current'}`)")
    lines.append(f"- **Conditions**:  {', '.join(conditions)}")
    lines.append(f"- **Questions**:   {suite['config'].get('n_questions')}")
    lines.append(f"- **Wall time**:   {suite['config'].get('total_wall_seconds', '?')}s")
    lines.append("")

    # 1. Headline lift
    lines.append("## 1. Headline correctness")
    lines.append("")
    lines.append("| condition | mean correctness (1-5) | pass-rate (≥4) | refusal rate | n |")
    lines.append("|---|---:|---:|---:|---:|")
    for cond in conditions:
        bucket = by_cond.get(cond) or []
        lines.append(
            f"| `{cond}` | {_mean_correctness(bucket)} "
            f"| {_pct(bucket, _passing)} "
            f"| {_pct(bucket, _refused)} "
            f"| {len(bucket)} |"
        )
    # Sift-grep lift (most likely the headline number)
    sg = by_cond.get("sift-grep") or []
    cb = by_cond.get("closed-book") or []
    wf = by_cond.get("web-fetch") or []
    if sg and cb:
        lift = _round(_mean_correctness(sg) - _mean_correctness(cb))
        lines.append("")
        lines.append(f"**sift-grep lift over closed-book**: {lift:+}/5 "
                     f"({_mean_correctness(sg)} − {_mean_correctness(cb)})")
    if sg and wf:
        lift = _round(_mean_correctness(sg) - _mean_correctness(wf))
        lines.append(f"**sift-grep lift over web-fetch**:   {lift:+}/5 "
                     f"({_mean_correctness(sg)} − {_mean_correctness(wf)})")
    lines.append("")

    # 2. Per-use-case
    lines.append("## 2. Per-use-case correctness")
    lines.append("")
    use_cases = sorted({q["use_case"] for q in qmap.values()})
    header = "| use case | " + " | ".join(f"`{c}`" for c in conditions) + " |"
    sep = "|---|" + "|".join("---:" for _ in conditions) + "|"
    lines.append(header)
    lines.append(sep)
    for uc in use_cases:
        qids_in_uc = {q["qid"] for q in qmap.values() if q["use_case"] == uc}
        row = [uc]
        for cond in conditions:
            cells = [c for c in (by_cond.get(cond) or [])
                     if c["qid"] in qids_in_uc]
            row.append(str(_mean_correctness(cells)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 3. Per-question table
    lines.append("## 3. Per-question results")
    lines.append("")
    header = "| qid | use case | " + " | ".join(f"`{c}`" for c in conditions) + " | fresh? |"
    sep = "|---|---|" + "|".join("---:" for _ in conditions) + "|---|"
    lines.append(header)
    lines.append(sep)
    for qid, q in sorted(qmap.items()):
        row = [qid, q["use_case"]]
        for cond in conditions:
            cell = next((c for c in (by_cond.get(cond) or [])
                         if c["qid"] == qid), None)
            score = int(((cell or {}).get("judge") or {}).get("correctness") or 0)
            row.append(str(score) if score else "—")
        row.append("yes" if q.get("fresh_sensitive") else "")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 4. Citations + tool-use
    lines.append("## 4. Citation behavior")
    lines.append("")
    lines.append("| condition | cites any URL | cites a gold-host URL |")
    lines.append("|---|---:|---:|")
    for cond in conditions:
        bucket = by_cond.get(cond) or []
        lines.append(
            f"| `{cond}` | {_pct(bucket, _has_citation)} "
            f"| {_pct(bucket, _faithful_citation)} |"
        )
    lines.append("")

    # 5. Cost
    lines.append("## 5. Cost (total tokens)")
    lines.append("")
    totals = suite.get("totals") or {}
    a = totals.get("agent_tokens") or {}
    j = totals.get("judge_tokens") or {}
    lines.append(f"- **Agent tokens**: in={a.get('input', 0):,} "
                 f"out={a.get('output', 0):,} "
                 f"cache_read={a.get('cache_read', 0):,} "
                 f"cache_write={a.get('cache_write', 0):,}")
    lines.append(f"- **Judge tokens**: in={j.get('input', 0):,} "
                 f"out={j.get('output', 0):,} "
                 f"cache_read={j.get('cache_read', 0):,} "
                 f"cache_write={j.get('cache_write', 0):,}")
    lines.append("")

    # 6. Notable failures (judge reasons for cells scored ≤ 2)
    failures = [c for c in results
                if int((c.get("judge") or {}).get("correctness") or 0) in (1, 2)
                and (c.get("judge") or {}).get("brief_reason")]
    if failures:
        lines.append("## 6. Notable failures (correctness ≤ 2)")
        lines.append("")
        for c in failures:
            reason = ((c.get("judge") or {}).get("brief_reason") or "").strip()
            lines.append(f"- `{c['qid']}` / `{c['condition']}` — {reason}")
        lines.append("")

    return "\n".join(lines)
