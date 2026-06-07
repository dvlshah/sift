"""Stage 6 evals: publish.

Implemented: ``publish_gate_summary`` reads the run's snapshot.json and
surfaces gate pass/fail. Stage-6 is rich (atomic swap, signature round-trip)
but most of those need fault-injection fixtures, deferred to Phase B5.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from sift import paths


@dataclass
class PublishGateResult:
    name: str = "publish_gate_summary"
    pass_threshold: str = "all gates green"
    published: bool = False
    status: str = "unknown"
    gates: list = None
    failed_gates: list = None
    passed: bool = False
    note: str = ""


def eval_publish_gates(root: Path, run_id: str) -> PublishGateResult:
    """Read ``runs/<run_id>/snapshot.json`` and report gate outcomes.

    Surfaces what publish_phase already computed — not a new measurement, but
    a single-place rollup so the bench's per-stage view is complete.
    """
    snap_path = paths.run_dir(root, run_id) / "snapshot.json"
    if not snap_path.exists():
        return PublishGateResult(
            note=f"no snapshot.json at {snap_path} — run never reached publish?",
            passed=False,
        )
    try:
        snap = json.loads(snap_path.read_text())
    except json.JSONDecodeError as e:
        return PublishGateResult(
            note=f"snapshot.json parse error: {e}",
            passed=False,
        )
    gates = snap.get("gates", [])
    failed = [g for g in gates if g and not g.get("passed", True)]
    status = snap.get("status", "unknown")
    return PublishGateResult(
        published=(status == "published"),
        status=status,
        gates=gates,
        failed_gates=failed,
        passed=(status == "published" and not failed),
    )


def run_publish_evals(root: Path, run_id: str) -> dict:
    return {"gates": asdict(eval_publish_gates(root, run_id))}
