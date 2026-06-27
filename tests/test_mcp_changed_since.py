"""Tests for the changed_since temporal-diff tool.

We author a REAL hash-chained changelog (via integrity.with_chain) across a
sequence of runs — some published, one degraded — plus a snapshot.json per run
and a current/ symlink. This exercises tool_changed_since end to end: window
boundaries, per-URL net collapse, the align-to-published guarantee, filters,
paging, and the cursor round-trip. The chain it writes stays verifiable by
sift.integrity.verify_chain, so the fixture itself is provenance-correct.
"""

import json

import pytest

from sift import mcp_server, paths
from sift.integrity import CHAIN_GENESIS, verify_chain, with_chain


def _text(result):
    assert result.content
    return result.content[0].text


def _body(result):
    assert not result.isError, _text(result)
    return json.loads(_text(result))


def _write_index(root, runs):
    """Materialize an index from ``runs``.

    Each run: {run_id, ts, status, current?, entries:[(change_type, url,
    old_hash, new_hash, tier)]}. Entries are appended to one chained
    changelog.jsonl in run order; each run also gets a runs/<id>/snapshot.json
    with completed_at=ts. ``current`` marks the published run the symlink
    points at.
    """
    cl = paths.changelog_path(root)
    cl.parent.mkdir(parents=True, exist_ok=True)
    prev = CHAIN_GENESIS
    total = 0
    with cl.open("w") as f:
        for run in runs:
            for (ct, url, old, new, tier) in run["entries"]:
                entry = {
                    "ts": run["ts"], "url": url, "change_type": ct,
                    "old_hash": old, "new_hash": new,
                    "run_id": run["run_id"], "tier": tier,
                }
                chained = with_chain(prev, entry)
                f.write(json.dumps(chained, separators=(",", ":")) + "\n")
                prev = chained["entry_hash"]
                total += 1
            run["_total_after"] = total
    # snapshot.json per run (written after, so changelog_total_entries is known)
    for run in runs:
        rd = paths.run_dir(root, run["run_id"])
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "snapshot.json").write_text(json.dumps({
            "run_id": run["run_id"],
            "status": run["status"],
            "started_at": run["ts"],
            "completed_at": run["ts"],
            "integrity": {
                "merkle_root": "merkle-" + run["run_id"],
                "changelog_total_entries": run["_total_after"],
                "scheme": "sorted-leaves-bitcoin-style-sha256",
            },
        }, indent=2))
    cur_run = next(r["run_id"] for r in runs if r.get("current"))
    link = paths.current_symlink(root)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(paths.run_dir(root, cur_run).resolve(), target_is_directory=True)
    return root


# A baseline (A) + two more published runs (B, C) + a LATER degraded run (D).
# u3 changes in BOTH B and C (to test net collapse); u5 is added only by the
# degraded run D (to test the align-to-published exclusion).
RUNS = [
    {"run_id": "20260101T000001Z", "ts": "2026-01-01T00:00:01Z",
     "status": "published", "entries": [
         ("added", "https://x/u1", None, "a1", "LIVING"),
         ("added", "https://x/u2", None, "a2", "LIVING"),
         ("added", "https://x/u3", None, "a3", "LIVING"),
     ]},
    {"run_id": "20260101T000002Z", "ts": "2026-01-01T00:00:02Z",
     "status": "published", "entries": [
         ("changed", "https://x/u1", "a1", "b1", "LIVING"),
         ("gone",    "https://x/u2", "a2", None, "LIVING"),
         ("changed", "https://x/u3", "a3", "b3", "LIVING"),
         ("added",   "https://x/u4", None, "b4", "FROZEN"),
     ]},
    {"run_id": "20260101T000003Z", "ts": "2026-01-01T00:00:03Z",
     "status": "published", "current": True, "entries": [
         ("changed", "https://x/u3", "b3", "c3", "LIVING"),
     ]},
    {"run_id": "20260101T000004Z", "ts": "2026-01-01T00:00:04Z",
     "status": "degraded", "entries": [   # ran AFTER current — must NOT leak
         ("added", "https://x/u5", None, "d5", "LIVING"),
     ]},
]


@pytest.fixture
def index(tmp_path):
    return _write_index(tmp_path, [dict(r) for r in RUNS])


def _by_url(items):
    return {i["url"]: i for i in items}


class TestChainIsReal:
    def test_authored_changelog_verifies(self, index):
        entries = [json.loads(ln) for ln in
                   paths.changelog_path(index).read_text().splitlines() if ln.strip()]
        ok, bad, reason = verify_chain(entries)
        assert ok, f"chain broke at {bad}: {reason}"


class TestDeltaFromBaseline:
    def test_net_delta_since_first_run(self, index):
        # since = run A → window (A, C]: includes B + C, excludes A and D.
        out = _body(mcp_server.tool_changed_since(index, "20260101T000001Z"))
        assert out["to"]["run_id"] == "20260101T000003Z"
        assert out["cursor"] == "20260101T000003Z"

        added = _by_url(out["added"])
        modified = _by_url(out["modified"])
        removed = _by_url(out["removed"])

        # u4 added; u2 removed; u1 modified a1->b1; u3 modified a3->c3 (collapsed)
        assert set(added) == {"https://x/u4"}
        assert added["https://x/u4"]["new_hash"] == "b4"
        assert set(removed) == {"https://x/u2"}
        assert removed["https://x/u2"]["old_hash"] == "a2"
        assert set(modified) == {"https://x/u1", "https://x/u3"}
        assert modified["https://x/u1"] == {
            "url": "https://x/u1", "ts": "2026-01-01T00:00:02Z", "tier": "LIVING",
            "entry_hash": modified["https://x/u1"]["entry_hash"],
            "old_hash": "a1", "new_hash": "b1",
        }
        # u3 collapsed across B and C: net a3 -> c3 (the intermediate b3 is gone)
        assert modified["https://x/u3"]["old_hash"] == "a3"
        assert modified["https://x/u3"]["new_hash"] == "c3"
        assert out["counts"] == {"added": 1, "modified": 2, "removed": 1,
                                 "changed_urls": 4}

    def test_degraded_run_does_not_leak(self, index):
        # u5 was added by the degraded run D (ts after current). It must be
        # absent from EVERY group — this is the align-to-published guarantee.
        out = _body(mcp_server.tool_changed_since(index, "20260101T000001Z"))
        all_urls = ({i["url"] for i in out["added"]}
                    | {i["url"] for i in out["modified"]}
                    | {i["url"] for i in out["removed"]})
        assert "https://x/u5" not in all_urls

    def test_provenance_and_chain_tip_present(self, index):
        out = _body(mcp_server.tool_changed_since(index, "20260101T000001Z"))
        assert "sift verify-changelog" in out["provenance"]
        # chain tip is the entry_hash of the last in-window entry (run C's u3)
        assert out["chain_tip_entry_hash"].startswith("sha256:")
        assert out["to"]["merkle_root"] == "merkle-20260101T000003Z"


class TestCursorRoundTrip:
    def test_since_current_is_up_to_date(self, index):
        out = _body(mcp_server.tool_changed_since(index, "20260101T000003Z"))
        assert out["up_to_date"] is True
        assert out["counts"]["changed_urls"] == 0
        assert out["added"] == [] and out["modified"] == [] and out["removed"] == []

    def test_intermediate_cursor(self, index):
        # since = run B → window (B, C]: only u3 changed (b3 -> c3).
        out = _body(mcp_server.tool_changed_since(index, "20260101T000002Z"))
        assert out["counts"] == {"added": 0, "modified": 1, "removed": 0,
                                 "changed_urls": 1}
        assert _by_url(out["modified"])["https://x/u3"]["old_hash"] == "b3"
        assert _by_url(out["modified"])["https://x/u3"]["new_hash"] == "c3"

    def test_timestamp_cursor_equivalent_to_run_id(self, index):
        by_run = _body(mcp_server.tool_changed_since(index, "20260101T000001Z"))
        by_ts = _body(mcp_server.tool_changed_since(index, "2026-01-01T00:00:01Z"))
        assert by_run["counts"] == by_ts["counts"]


class TestFilters:
    def test_tier_filter(self, index):
        out = _body(mcp_server.tool_changed_since(
            index, "20260101T000001Z", tier="FROZEN"))
        urls = {i["url"] for grp in ("added", "modified", "removed")
                for i in out[grp]}
        assert urls == {"https://x/u4"}  # only the FROZEN page

    def test_path_prefix_filter(self, index):
        out = _body(mcp_server.tool_changed_since(
            index, "20260101T000001Z", path_prefix="https://x/u1"))
        urls = {i["url"] for grp in ("added", "modified", "removed")
                for i in out[grp]}
        assert urls == {"https://x/u1"}


class TestPaging:
    def test_limit_truncates_and_flags(self, index):
        out = _body(mcp_server.tool_changed_since(
            index, "20260101T000001Z", limit=1))
        # counts are the TRUE totals; lists are capped at 1 per group.
        assert out["counts"]["modified"] == 2
        assert len(out["modified"]) == 1
        assert out["truncated"] is True
        assert "truncation_hint" in out


class TestErrors:
    def test_unresolvable_since(self, index):
        r = mcp_server.tool_changed_since(index, "not-a-run-or-ts")
        assert r.isError
        assert "neither a known run_id nor an ISO-8601" in _text(r)

    def test_no_published_snapshot(self, tmp_path):
        # An index with a changelog but no current/ symlink can't bound a diff.
        (tmp_path / "changelog.jsonl").write_text("")
        r = mcp_server.tool_changed_since(tmp_path, "2026-01-01T00:00:01Z")
        assert r.isError
        assert "published baseline" in _text(r)


class TestDispatchWiring:
    def test_changed_since_is_registered_and_read_only(self):
        tools = {t.name: t for t in mcp_server._tool_descriptors()}
        assert "changed_since" in tools
        assert tools["changed_since"].annotations.readOnlyHint is True
        assert "since" in tools["changed_since"].inputSchema["required"]

    def test_empty_since_is_rejected(self, index):
        r = mcp_server.tool_changed_since(index, "")
        assert r.isError

    def test_changed_since_is_fanout_eligible(self):
        # Omitting `index` in multi-mode must fan out, not error — so the tool
        # has to be in the fan-out allow-list alongside snapshot_status.
        assert "changed_since" in mcp_server._FANOUT_TOOLS
