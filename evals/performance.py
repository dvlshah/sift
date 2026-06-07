"""Performance baseline — per-phase wall time, throughput, resource usage.

Inputs:
    <root>/runs/<run_id>/snapshot.json   — gates, counts, versions
    <root>/_logs/full-run-<ts>.log       — CLI per-phase timings + /usr/bin/time output
    <root>/manifest.db                   — for run-level counts

Output: a dict suitable for embedding in baseline_report.json.

We pull the canonical timings from the `run` command's structured JSON output
(the last well-formed JSON object in the log). When the timings aren't
available (legacy runs, or run via individual phase commands), we still
report what we can from snapshot.json + filesystem stats.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from sift import paths


@dataclass
class PhaseTimings:
    plan_sec: Optional[float] = None
    fetch_sec: Optional[float] = None
    extract_sec: Optional[float] = None
    commit_sec: Optional[float] = None
    publish_sec: Optional[float] = None
    total_sec: Optional[float] = None


@dataclass
class ResourceUsage:
    peak_rss_bytes: Optional[int] = None
    user_cpu_sec: Optional[float] = None
    sys_cpu_sec: Optional[float] = None
    wall_sec: Optional[float] = None
    cpu_utilization_pct: Optional[float] = None
    page_reclaims: Optional[int] = None
    voluntary_context_switches: Optional[int] = None
    involuntary_context_switches: Optional[int] = None


@dataclass
class PerformanceMetrics:
    run_id: str
    phase_timings: PhaseTimings = field(default_factory=PhaseTimings)
    resources: ResourceUsage = field(default_factory=ResourceUsage)

    # Derived throughput
    fetch_throughput_req_per_sec: Optional[float] = None
    extract_throughput_pages_per_sec: Optional[float] = None
    extract_ms_per_page_mean: Optional[float] = None

    # Counts at end of run
    counts_by_state: dict[str, int] = field(default_factory=dict)
    counts_by_tier: dict[str, int] = field(default_factory=dict)

    # Per-phase share of wall time
    phase_share_pct: dict[str, float] = field(default_factory=dict)

    # Provenance for the eval
    snapshot_path: Optional[str] = None
    log_path: Optional[str] = None


# /usr/bin/time -l on macOS emits "  <value>  <label>" lines
_TIME_FIELDS = {
    "maximum resident set size":      ("peak_rss_bytes", int),
    "page reclaims":                  ("page_reclaims", int),
    "voluntary context switches":     ("voluntary_context_switches", int),
    "involuntary context switches":   ("involuntary_context_switches", int),
}
_TIME_HEADER_RE = re.compile(r"^\s*([\d.]+)\s+real\s+([\d.]+)\s+user\s+([\d.]+)\s+sys\s*$")


def _parse_time_l_block(text: str, usage: ResourceUsage) -> None:
    """Best-effort parse of `/usr/bin/time -l` output (macOS) — accumulates
    fields onto `usage` in place. Linux's GNU time has a different format;
    this parser will simply find fewer fields."""
    for line in text.splitlines():
        m = _TIME_HEADER_RE.match(line)
        if m:
            usage.wall_sec = float(m.group(1))
            usage.user_cpu_sec = float(m.group(2))
            usage.sys_cpu_sec = float(m.group(3))
            continue
        stripped = line.strip()
        for needle, (attr, cast) in _TIME_FIELDS.items():
            if stripped.endswith(needle):
                head = stripped[: -len(needle)].strip()
                try:
                    setattr(usage, attr, cast(head))
                except ValueError:
                    pass
                break


def _last_json_object(text: str) -> Optional[dict]:
    """Find the last '{' ... '}' that parses as JSON. Run command echoes the
    summary as the final block; that's what we want."""
    # Scan from the back for balanced braces.
    depth = 0
    end_idx: Optional[int] = None
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == "}":
            if end_idx is None:
                end_idx = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end_idx is not None:
                try:
                    return json.loads(text[i : end_idx + 1])
                except json.JSONDecodeError:
                    end_idx = None
                    depth = 0
    return None


def _find_latest_run_log(root: Path, run_id: str) -> Optional[Path]:
    """Match the most recent log whose name encodes a timestamp ≤ run_id.
    Falls back to the newest log if no exact match."""
    logs_dir = root / "_logs"
    if not logs_dir.exists():
        return None
    candidates = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    # Prefer one with the run_id as a substring of its filename.
    for p in candidates:
        if run_id in p.name:
            return p
    return candidates[-1]


def run(
    root: Path,
    run_id: str,
    *,
    conn=None,
) -> PerformanceMetrics:
    """Collect performance metrics for a snapshot.

    Reads:
      - snapshot.json (always — that's how we got the run_id)
      - the matching _logs/*.log if present (for /usr/bin/time + phase timings)
      - manifest.db for counts (passed in to avoid double-opening)
    """
    metrics = PerformanceMetrics(run_id=run_id)
    snap_path = paths.snapshot_path(root, run_id)
    if snap_path.exists():
        metrics.snapshot_path = str(snap_path)
        snap = json.loads(snap_path.read_text())
        metrics.counts_by_state = dict(snap.get("counts_by_state") or {})
        metrics.counts_by_tier = dict(snap.get("counts_by_tier") or {})

    log_path = _find_latest_run_log(root, run_id)
    if log_path is not None:
        metrics.log_path = str(log_path)
        text = log_path.read_text(errors="replace")

        # Parse /usr/bin/time -l block (it's at the very end after the JSON)
        _parse_time_l_block(text, metrics.resources)

        # Pull the structured run summary JSON
        summary = _last_json_object(text)
        if summary:
            tim = summary.get("timings_sec") or {}
            pt = metrics.phase_timings
            pt.plan_sec    = tim.get("plan")
            pt.fetch_sec   = tim.get("fetch")
            pt.extract_sec = tim.get("extract")
            pt.commit_sec  = tim.get("commit")
            pt.publish_sec = tim.get("publish")
            pt.total_sec   = summary.get("total_sec")
            metrics.fetch_throughput_req_per_sec = summary.get(
                "fetch_throughput_req_per_sec"
            )

    # Derived throughput
    pt = metrics.phase_timings
    fresh = metrics.counts_by_state.get("FRESH", 0)
    if pt.extract_sec and pt.extract_sec > 0 and fresh > 0:
        metrics.extract_throughput_pages_per_sec = round(fresh / pt.extract_sec, 2)
        metrics.extract_ms_per_page_mean = round(1000 * pt.extract_sec / fresh, 2)

    # Phase share of wall time (only when we have all phases)
    if pt.total_sec and pt.total_sec > 0:
        for name, val in (
            ("plan",    pt.plan_sec),
            ("fetch",   pt.fetch_sec),
            ("extract", pt.extract_sec),
            ("commit",  pt.commit_sec),
            ("publish", pt.publish_sec),
        ):
            if val is not None:
                metrics.phase_share_pct[name] = round(100 * val / pt.total_sec, 1)

    # CPU utilization
    r = metrics.resources
    if r.wall_sec and r.wall_sec > 0 and (r.user_cpu_sec or 0) + (r.sys_cpu_sec or 0):
        metrics.resources.cpu_utilization_pct = round(
            100 * ((r.user_cpu_sec or 0) + (r.sys_cpu_sec or 0)) / r.wall_sec, 1
        )

    return metrics


def to_dict(m: PerformanceMetrics) -> dict:
    return asdict(m)
