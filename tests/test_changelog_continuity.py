"""gate_changelog_continuity: the hash-chained changelog must extend the
previously published one, never silently restart (genesis change) or shrink
(truncation)."""

import json
from pathlib import Path

from sift import paths, publish


def _write_changelog(root: Path, genesis_run: str, n_entries: int) -> None:
    cl = paths.changelog_path(root)
    cl.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"run_id": genesis_run if i == 0 else f"{genesis_run}-{i}",
                    "seq": i})
        for i in range(n_entries)
    ]
    cl.write_text("\n".join(lines) + "\n")


def _publish_prior(root: Path, genesis_run: str, total_entries: int) -> None:
    """Stand up a prior published snapshot: a run dir whose snapshot.json records
    the changelog genesis + total, with `current` pointing at it."""
    prior = paths.run_dir(root, "PRIOR")
    prior.mkdir(parents=True, exist_ok=True)
    (prior / "snapshot.json").write_text(json.dumps({
        "integrity": {
            "changelog_genesis_run": genesis_run,
            "changelog_total_entries": total_entries,
        }
    }))
    paths.current_symlink(root).symlink_to(
        Path("runs") / "PRIOR", target_is_directory=True)


class TestChangelogContinuityGate:
    def test_fresh_index_passes(self, tmp_path):
        ok, detail = publish.gate_changelog_continuity(tmp_path, "r1")
        assert ok
        assert "fresh index" in detail

    def test_append_only_growth_passes(self, tmp_path):
        _publish_prior(tmp_path, "G", 3)
        _write_changelog(tmp_path, "G", 5)  # same genesis, grew 3 -> 5
        ok, detail = publish.gate_changelog_continuity(tmp_path, "r1")
        assert ok, detail
        assert "continuous" in detail

    def test_genesis_change_fails(self, tmp_path):
        _publish_prior(tmp_path, "G", 3)
        _write_changelog(tmp_path, "H", 5)  # different genesis -> wiped chain
        ok, detail = publish.gate_changelog_continuity(tmp_path, "r1")
        assert not ok
        assert "genesis changed" in detail

    def test_truncation_fails(self, tmp_path):
        _publish_prior(tmp_path, "G", 5)
        _write_changelog(tmp_path, "G", 2)  # same genesis but shrank 5 -> 2
        ok, detail = publish.gate_changelog_continuity(tmp_path, "r1")
        assert not ok
        assert "shrank" in detail
