"""Tests for the time-travel read family: as_of ("Flux Capacitor") on the
content read tools + diff_md ("Difference Engine").

A fixture lays down real per-run md trees across two PUBLISHED runs (A, then B
which is current) and one later DEGRADED run (C). Each run dir is a complete,
content-hashed snapshot — exactly what the pipeline retains — so the tools read
real historical content, and the published-only rule is exercised against a real
degraded run.
"""

import asyncio
import json

import pytest

from sift import mcp_server, paths
from sift._io import split_frontmatter
from sift.extract import hash_normalized_body

RUN_A = "20260101T000001Z"
RUN_B = "20260101T000002Z"
RUN_C = "20260101T000003Z"
TS = {RUN_A: "2026-01-01T00:00:01Z", RUN_B: "2026-01-01T00:00:02Z",
      RUN_C: "2026-01-01T00:00:03Z"}


def _md(url, body):
    """A markdown file whose frontmatter content_hash is the REAL normalized
    hash of the body — so reads, diffs, and verify all agree."""
    text = f"---\nurl: {url}\ncontent_hash: sha256:__H__\ntier: LIVING\n---\n{body}\n"
    _, b = split_frontmatter(text)
    return text.replace("__H__", hash_normalized_body(b))


def _write_run(root, run_id, status, *, pages, facts=None, current=False):
    rd = paths.run_dir(root, run_id)
    (rd / "md").mkdir(parents=True, exist_ok=True)
    for rel, body in pages.items():
        f = rd / "md" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_md(f"https://x/{rel}", body))
    for rel, obj in (facts or {}).items():
        ff = rd / "facts" / rel
        ff.parent.mkdir(parents=True, exist_ok=True)
        ff.write_text(json.dumps(obj))
    (rd / "snapshot.json").write_text(json.dumps({
        "run_id": run_id, "status": status,
        "started_at": TS[run_id], "completed_at": TS[run_id],
        "integrity": {"merkle_root": "m-" + run_id},
    }))
    if current:
        link = paths.current_symlink(root)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(rd.resolve(), target_is_directory=True)
    return rd


@pytest.fixture
def index(tmp_path):
    root = tmp_path
    _write_run(root, RUN_A, "published", pages={
        "p0.md": "only in A — will be removed",
        "p1.md": "alpha\nOLD LINE\ngamma",
        "p2.md": "stable line",
    }, facts={"rates/r.json": {"$schema": "x", "rate": "old",
                              "effective_from": "2024-07-01"}})
    _write_run(root, RUN_B, "published", current=True, pages={
        "p1.md": "alpha\nNEW LINE\ngamma",   # modified
        "p2.md": "stable line",              # unchanged
        "p3.md": "brand new page",           # added
    }, facts={"rates/r.json": {"$schema": "x", "rate": "new",
                              "effective_from": "2025-07-01"}})
    _write_run(root, RUN_C, "degraded", pages={
        "p1.md": "alpha\nDEGRADED\ngamma",   # never published
    })
    return root


def _call(server, name, arguments):
    handler = (server.request_handlers.get("tools/call")
               or server.request_handlers[mcp_server.mcp_types.CallToolRequest])
    req = mcp_server.mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_server.mcp_types.CallToolRequestParams(
            name=name, arguments=arguments),
    )
    return asyncio.run(handler(req)).root


def _text(r):
    assert r.content
    return r.content[0].text


# ---- _resolve_as_of: the brain of time-travel ----------------------------

class TestResolveAsOf:
    def test_run_id_resolves_to_run_dir(self, index):
        rd, err = mcp_server._resolve_as_of(index, RUN_A)
        assert err is None and rd.name == RUN_A

    def test_degraded_run_refused(self, index):
        rd, err = mcp_server._resolve_as_of(index, RUN_C)
        assert rd is None and "never published" in _text(err)

    def test_unknown_run_id_graceful(self, index):
        rd, err = mcp_server._resolve_as_of(index, "nope")
        assert rd is None and "not a known run_id" in _text(err)

    def test_timestamp_resolves_to_run_current_then(self, index):
        # at A's completion → A; at/after B → B (C is degraded, never counts)
        assert mcp_server._resolve_as_of(index, TS[RUN_A])[0].name == RUN_A
        assert mcp_server._resolve_as_of(index, TS[RUN_B])[0].name == RUN_B
        assert mcp_server._resolve_as_of(index, TS[RUN_C])[0].name == RUN_B

    def test_timestamp_before_any_publish(self, index):
        rd, err = mcp_server._resolve_as_of(index, "2026-01-01T00:00:00Z")
        assert rd is None and "No published snapshot at or before" in _text(err)


# ---- as_of on the read tools (through the server dispatch) ----------------

class TestAsOfReads:
    def test_read_md_historical_vs_current(self, index):
        server = mcp_server.build_server(index)
        hist = _text(_call(server, "read_md", {"path": "md/p1.md", "as_of": RUN_A}))
        assert "OLD LINE" in hist and "NEW LINE" not in hist
        assert f"as_of run={RUN_A}" in hist and "HISTORICAL" in hist
        cur = _text(_call(server, "read_md", {"path": "md/p1.md"}))
        assert "NEW LINE" in cur and "HISTORICAL" not in cur

    def test_read_facts_as_of(self, index):
        server = mcp_server.build_server(index)
        old = _text(_call(server, "read_facts",
                          {"path": "facts/rates/r.json", "as_of": RUN_A}))
        assert '"rate": "old"' in old
        new = _text(_call(server, "read_facts", {"path": "facts/rates/r.json"}))
        assert '"rate": "new"' in new

    def test_grep_as_of_searches_historical_tree(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "grep_corpus", {"pattern": "OLD LINE", "as_of": RUN_A})
        assert not r.isError and "p1.md" in _text(r)

    def test_as_of_degraded_refused(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "read_md", {"path": "md/p1.md", "as_of": RUN_C})
        assert r.isError and "never published" in _text(r)

    def test_query_manifest_rejects_as_of(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "query_manifest",
                  {"sql": "SELECT 1", "as_of": RUN_A})
        assert r.isError and "does not support as_of" in _text(r)


class TestVerifyComposesWithAsOf:
    def test_historical_body_verifies(self, index):
        # index_root=None → verify uses the default profile, matching the hash
        # the fixture computed; proves a historical read is hash-verifiable.
        rd = paths.run_dir(index, RUN_A)
        r = mcp_server.tool_read_md(rd, "md/p1.md", verify=True, index_root=None)
        assert not r.isError and "verify=ok" in _text(r)


# ---- diff_md: the Difference Engine ---------------------------------------

class TestDiffMd:
    def test_modified_page_returns_hunks(self, index):
        out = json.loads(_text(mcp_server.tool_diff_md(index, "md/p1.md", RUN_A, RUN_B)))
        assert out["status"] == "modified"
        assert out["added_lines"] >= 1 and out["removed_lines"] >= 1
        assert "NEW LINE" in out["diff"] and "OLD LINE" in out["diff"]
        assert out["from"]["content_hash"] != out["to"]["content_hash"]
        assert out["from"]["run_id"] == RUN_A and out["to"]["run_id"] == RUN_B

    def test_unchanged_page_short_circuits(self, index):
        out = json.loads(_text(mcp_server.tool_diff_md(index, "md/p2.md", RUN_A, RUN_B)))
        assert out["status"] == "unchanged" and out["diff"] == ""

    def test_added_page(self, index):
        out = json.loads(_text(mcp_server.tool_diff_md(index, "md/p3.md", RUN_A, RUN_B)))
        assert out["status"] == "added" and out["added_lines"] >= 1

    def test_removed_page(self, index):
        out = json.loads(_text(mcp_server.tool_diff_md(index, "md/p0.md", RUN_A, RUN_B)))
        assert out["status"] == "removed" and out["removed_lines"] >= 1

    def test_to_defaults_to_current(self, index):
        # omitting `to` diffs against the current published snapshot (B)
        out = json.loads(_text(mcp_server.tool_diff_md(index, "md/p1.md", RUN_A)))
        assert out["to"]["run_id"] == RUN_B and out["status"] == "modified"

    def test_md_prefix_optional(self, index):
        out = json.loads(_text(mcp_server.tool_diff_md(index, "p1.md", RUN_A, RUN_B)))
        assert out["path"] == "md/p1.md" and out["status"] == "modified"

    def test_from_degraded_refused(self, index):
        r = mcp_server.tool_diff_md(index, "md/p1.md", RUN_C, RUN_B)
        assert r.isError and "never published" in _text(r)

    def test_diff_md_via_dispatch(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "diff_md", {"path": "md/p1.md", "from": RUN_A})
        assert not r.isError
        assert json.loads(_text(r))["status"] == "modified"

    def test_diff_md_requires_from(self, index):
        server = mcp_server.build_server(index)
        # omitted → rejected by the schema's `required` list
        r = _call(server, "diff_md", {"path": "md/p1.md"})
        assert r.isError and "from" in _text(r).lower()
        # present-but-empty → caught by the dispatch guard
        r2 = _call(server, "diff_md", {"path": "md/p1.md", "from": ""})
        assert r2.isError and "requires `from`" in _text(r2)


class TestDescriptorWiring:
    def test_diff_md_registered_read_only(self):
        tools = {t.name: t for t in mcp_server._tool_descriptors()}
        assert "diff_md" in tools
        assert tools["diff_md"].annotations.readOnlyHint is True
        assert set(tools["diff_md"].inputSchema["required"]) == {"path", "from"}

    def test_as_of_param_on_read_tools_only(self):
        tools = {t.name: t for t in mcp_server._tool_descriptors()}
        for n in ("read_md", "grep_corpus", "glob_corpus", "list_dir", "read_facts"):
            assert "as_of" in tools[n].inputSchema["properties"], n
        # query_manifest must NOT advertise as_of (manifest is current-only)
        assert "as_of" not in tools["query_manifest"].inputSchema["properties"]
