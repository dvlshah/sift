"""Bench drift detection — diff two ``suite.json`` files and surface deltas.

Used by the CI workflow + ``sift-evals bench-drift`` to flag regressions
between a baseline run (e.g. ``evals/bench/results/v1.0-baseline-2026-05-31.json``)
and the current branch's run, without re-baselining numbers a human hasn't
reviewed.

What counts as drift, ranked by severity:

  * **Status regression** — a fixture that was ``published`` is now ``failed``
    (or fell back to ``degraded``). This is always loud.
  * **Eval pass/fail flip** — a per-stage eval that was passing now fails
    (or vice versa). Often the proximate cause of a status regression.
  * **Rate delta** — a numeric eval rate moved by more than ``rate_eps``
    (default 0.05 = 5 percentage points). The sign matters — drops are
    flagged as regressions, gains as improvements.
  * **Count delta** — ``seeded`` / ``fetched`` / ``md_count`` shifted by
    more than ``count_pct_eps`` (default 20%). Catches "we lost half the
    URLs" without overreporting normal page-churn jitter.
  * **New / removed fixtures** — fixture set changed (e.g. a slug was
    renamed). Surfaced separately so reviewers know what's missing from
    each side rather than reading it as a regression.

Output: a single :class:`Drift` dataclass plus a structured ``deltas`` list,
JSON-serializable for CI artifacts and human-readable via
``render_markdown``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# A single observation about how the current run diverged from the baseline.
# The severity ladder mirrors the docstring: status > eval-flip > rate >
# count > set-membership.
SEVERITY_ORDER = ("status", "eval_flip", "rate", "count", "membership")


@dataclass(frozen=True)
class Delta:
    severity: str         # one of SEVERITY_ORDER
    slug: str             # fixture slug (or "<suite>" for cross-fixture facts)
    kind: str             # short label, e.g. "status", "fetch_success_rate"
    baseline: object      # baseline value (str / int / float / None)
    current: object       # current value
    is_regression: bool   # true if the change is bad for sift
    note: str             # one-line human description


@dataclass
class Drift:
    baseline_path: Optional[Path]
    current_path: Optional[Path]
    deltas: list[Delta] = field(default_factory=list)
    # Quick counts so CI can branch on totals without re-walking deltas.
    regressions: int = 0
    improvements: int = 0
    membership_changes: int = 0

    @property
    def has_regressions(self) -> bool:
        return self.regressions > 0

    def to_dict(self) -> dict:
        return {
            "baseline": str(self.baseline_path) if self.baseline_path else None,
            "current": str(self.current_path) if self.current_path else None,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "membership_changes": self.membership_changes,
            "deltas": [
                {
                    "severity": d.severity, "slug": d.slug, "kind": d.kind,
                    "baseline": d.baseline, "current": d.current,
                    "is_regression": d.is_regression, "note": d.note,
                }
                for d in self.deltas
            ],
        }


def _by_slug(suite: dict) -> dict[str, dict]:
    return {r["slug"]: r for r in (suite.get("results") or [])}


def _iter_eval_blocks(fixture_result: dict) -> list[tuple[str, str, dict]]:
    """Yield ``(stage_key, eval_key, block)`` for every per-stage eval inside
    one fixture result. ``block`` is the raw dict produced by the eval (which
    may contain ``passed``, ``rate``, ``name``, plus eval-specific fields).
    """
    out: list[tuple[str, str, dict]] = []
    bench = (fixture_result.get("bench") or {})
    stages = bench.get("stages") or {}
    for stage_key, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        for eval_key, block in stage.items():
            # Skip the ``fixture`` echo block — it's metadata, not an eval.
            if eval_key == "fixture" or not isinstance(block, dict):
                continue
            if "passed" in block or "rate" in block:
                out.append((stage_key, eval_key, block))
    return out


def _status_severity(status: Optional[str]) -> int:
    """Lower is worse. Used to detect status regressions."""
    return {
        None: 0, "failed": 1, "seeded": 2, "fetched": 3,
        "degraded": 4, "published": 5,
    }.get(status, 0)


# Count-delta tolerance. 20% papers over jitter from the upstream sitemap
# churning a few pages between runs; bigger swings get flagged.
_COUNT_PCT_EPS = 0.20
# Numeric-rate tolerance. 0.05 = 5 percentage points. Calibrated against the
# v1.0 baseline where most stable evals fluctuate < 2pp run to run.
_RATE_EPS = 0.05


def compute_drift(
    baseline: dict, current: dict,
    *,
    rate_eps: float = _RATE_EPS,
    count_pct_eps: float = _COUNT_PCT_EPS,
    baseline_path: Optional[Path] = None,
    current_path: Optional[Path] = None,
) -> Drift:
    """Diff two ``suite.json`` dicts and produce a :class:`Drift` report.

    Pure function — no I/O. The CLI command loads both files and hands them
    in; tests construct synthetic dicts directly.
    """
    base_by_slug = _by_slug(baseline)
    cur_by_slug = _by_slug(current)
    drift = Drift(baseline_path=baseline_path, current_path=current_path)

    # Membership: removed / added fixtures.
    for slug in sorted(set(base_by_slug) - set(cur_by_slug)):
        drift.deltas.append(Delta(
            severity="membership", slug=slug, kind="removed",
            baseline=base_by_slug[slug].get("status"), current=None,
            is_regression=False,
            note=f"{slug}: present in baseline, absent from current",
        ))
        drift.membership_changes += 1
    for slug in sorted(set(cur_by_slug) - set(base_by_slug)):
        drift.deltas.append(Delta(
            severity="membership", slug=slug, kind="added",
            baseline=None, current=cur_by_slug[slug].get("status"),
            is_regression=False,
            note=f"{slug}: new in current, no baseline to compare",
        ))
        drift.membership_changes += 1

    # Per-fixture comparisons.
    for slug in sorted(set(base_by_slug) & set(cur_by_slug)):
        b = base_by_slug[slug]
        c = cur_by_slug[slug]
        _diff_fixture(drift, slug, b, c,
                      rate_eps=rate_eps, count_pct_eps=count_pct_eps)

    # Tally regressions / improvements (membership excluded — set
    # membership changes are surfaced separately).
    for d in drift.deltas:
        if d.severity == "membership":
            continue
        if d.is_regression:
            drift.regressions += 1
        else:
            drift.improvements += 1

    # Sort deltas by severity then slug for stable downstream output.
    drift.deltas.sort(
        key=lambda d: (SEVERITY_ORDER.index(d.severity), d.slug, d.kind),
    )
    return drift


def _diff_fixture(drift: Drift, slug: str, base: dict, cur: dict,
                  *, rate_eps: float, count_pct_eps: float) -> None:
    # 1. Status delta — most severe.
    b_status, c_status = base.get("status"), cur.get("status")
    if b_status != c_status:
        worsened = _status_severity(c_status) < _status_severity(b_status)
        drift.deltas.append(Delta(
            severity="status", slug=slug, kind="status",
            baseline=b_status, current=c_status,
            is_regression=worsened,
            note=f"{slug}: status {b_status!r} → {c_status!r}",
        ))

    # 2. Per-eval pass/fail flips + rate deltas.
    base_evals = {(s, e): blk for s, e, blk in _iter_eval_blocks(base)}
    cur_evals = {(s, e): blk for s, e, blk in _iter_eval_blocks(cur)}
    for key in sorted(set(base_evals) & set(cur_evals)):
        bblk, cblk = base_evals[key], cur_evals[key]
        eval_label = f"{key[0]}.{key[1]}"

        # Pass/fail flip.
        bp, cp = bblk.get("passed"), cblk.get("passed")
        if bp is not None and cp is not None and bp != cp:
            drift.deltas.append(Delta(
                severity="eval_flip", slug=slug, kind=eval_label,
                baseline=bool(bp), current=bool(cp),
                is_regression=(bp is True and cp is False),
                note=f"{slug}: {eval_label} {bp} → {cp}",
            ))

        # Rate delta (when both sides report a numeric rate).
        br, cr = bblk.get("rate"), cblk.get("rate")
        if isinstance(br, (int, float)) and isinstance(cr, (int, float)):
            if abs(cr - br) >= rate_eps:
                drift.deltas.append(Delta(
                    severity="rate", slug=slug, kind=eval_label,
                    baseline=round(br, 3), current=round(cr, 3),
                    is_regression=(cr < br),
                    note=(f"{slug}: {eval_label} rate "
                          f"{br:.3f} → {cr:.3f} "
                          f"({'-' if cr < br else '+'}{abs(cr - br):.3f})"),
                ))

    # 3. Count deltas — seeded / fetched / md_count.
    for field_name in ("seeded", "fetched", "md_count"):
        bn = base.get(field_name) or 0
        cn = cur.get(field_name) or 0
        if bn == 0 and cn == 0:
            continue
        # Use the larger side as the denominator to keep "0 → 100" loud
        # without producing infinities.
        denom = max(bn, cn, 1)
        pct = (cn - bn) / denom
        if abs(pct) >= count_pct_eps:
            drift.deltas.append(Delta(
                severity="count", slug=slug, kind=field_name,
                baseline=bn, current=cn,
                is_regression=(cn < bn),
                note=(f"{slug}: {field_name} {bn} → {cn} "
                      f"({'-' if pct < 0 else '+'}{abs(pct):.0%})"),
            ))


def load_suite(path: Path) -> dict:
    """Load a ``suite.json`` file. Raises ``ValueError`` on schema mismatch
    so a stale baseline produces an actionable error rather than silent
    drift-of-nothing."""
    body = json.loads(Path(path).read_text())
    if "results" not in body or not isinstance(body["results"], list):
        raise ValueError(f"{path}: missing 'results' list — not a suite.json?")
    return body


# ---- rendering -------------------------------------------------------------

def render_markdown(drift: Drift) -> str:
    """Human-readable summary. Designed to be readable in a GitHub Actions
    job summary and in a terminal."""
    lines = ["# Bench drift", ""]
    lines.append(f"- **Baseline**: `{drift.baseline_path}`")
    lines.append(f"- **Current**:  `{drift.current_path}`")
    lines.append(
        f"- **Regressions**: {drift.regressions}  "
        f"**Improvements**: {drift.improvements}  "
        f"**Membership changes**: {drift.membership_changes}"
    )
    lines.append("")

    if not drift.deltas:
        lines.append("_No drift detected within configured thresholds._")
        return "\n".join(lines)

    by_severity: dict[str, list[Delta]] = {s: [] for s in SEVERITY_ORDER}
    for d in drift.deltas:
        by_severity[d.severity].append(d)

    headings = {
        "status": "## Status changes",
        "eval_flip": "## Eval pass/fail flips",
        "rate": "## Rate deltas",
        "count": "## Count deltas",
        "membership": "## Fixture set membership",
    }
    for sev in SEVERITY_ORDER:
        bucket = by_severity[sev]
        if not bucket:
            continue
        lines.append(headings[sev])
        lines.append("")
        for d in bucket:
            if sev == "membership":
                tag = "[change]"
            else:
                tag = "[REGRESSION]" if d.is_regression else "[improvement]"
            lines.append(f"- {tag} {d.note}")
        lines.append("")

    return "\n".join(lines)
