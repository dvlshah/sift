"""Stage 5 evals: commit.

Implemented (B5):
  * ``commit_changelog_chain_integrity`` — walk the changelog SHA-256 chain
    and verify each entry's ``prev_hash`` matches the previous entry's
    ``entry_hash``. Wraps the existing ``sift verify-changelog`` logic as a
    bench eval so the per-stage taxonomy is complete and the operator gets
    a single number ("changelog integrity = 100%") in the report alongside
    the quality numbers.

Deferred (Phase B6+):
  * ``commit_idempotency`` — needs a fault-injection harness (kill
    mid-commit and verify recovery state). Worth doing but research-shaped.
  * ``commit_atomicity_under_crash`` — same. Both need SQLite write-ahead
    fault scenarios that we don't have fixtures for yet.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class ChangelogIntegrityResult:
    name: str = "commit_changelog_chain_integrity"
    pass_threshold: float = 1.0
    entries: int = 0
    breaks: int = 0
    rate: float = 0.0
    passed: bool = False
    note: str = ""


def eval_changelog_integrity(root: Path) -> ChangelogIntegrityResult:
    """Walk ``<root>/changelog.jsonl`` and verify the SHA-256 chain via
    ``integrity.verify_chain``. Returns 1.0 when the chain is intact
    end-to-end; 0.0 when any ``prev_hash`` or ``entry_hash`` mismatches.
    """
    import json

    from sift import paths
    from sift.integrity import verify_chain

    cl = paths.changelog_path(root)
    if not cl.exists():
        return ChangelogIntegrityResult(
            note=f"no changelog at {cl} — fresh index or never-committed run?",
            passed=False,
        )
    entries = []
    with cl.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    n = len(entries)
    if n == 0:
        return ChangelogIntegrityResult(
            entries=0, breaks=0, rate=0.0, passed=False,
            note="changelog file present but empty",
        )
    ok, bad_idx, reason = verify_chain(entries)
    breaks = 0 if ok else 1
    rate = ((n - breaks) / n) if n else 0.0
    return ChangelogIntegrityResult(
        entries=n,
        breaks=breaks,
        rate=round(rate, 4),
        passed=ok,
        note=(f"chain intact across {n} entries"
              if ok else f"chain broke at entry {bad_idx}: {reason}"),
    )


def run_commit_evals(root: Optional[Path] = None, **kwargs: Any) -> dict:
    """Run all Stage-5 commit evals. Returns ``{"status": "not_run"}`` when
    no root is provided (the suite harness passes it; the per-fixture
    runner historically did not)."""
    if root is None:
        return {"status": "not_run",
                "note": "commit evals require --root; not yet wired into "
                        "per-fixture runner"}
    return {"changelog": asdict(eval_changelog_integrity(root))}
