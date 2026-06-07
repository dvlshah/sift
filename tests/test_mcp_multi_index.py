"""Tests for the multi-index MCP server mode.

Covers the dispatcher behavior introduced when ``--root`` points at a
parent directory of sift indexes rather than a single sift root:

  * ``list_indexes`` returns registered indexes; only present in multi-mode
  * ``read_md`` / ``read_facts`` require ``index=<slug>``
  * ``grep_corpus`` / ``glob_corpus`` / ``list_dir`` / ``query_manifest``
    accept ``index=<slug>`` (scoped) OR omit / ``index="*"`` (fan-out)
  * Unknown slug returns a structured error pointing the agent at
    ``list_indexes`` and listing available slugs
  * Single-index mode (parent path IS itself a sift root) keeps the old
    behavior — no ``index`` param required anywhere
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sift import mcp_server, paths
from sift.registry import IndexRegistry


# ---- index builders ------------------------------------------------------

def _format_section(name: str, payload: dict) -> str:
    lines = [f"[{name}]"]
    for k, v in payload.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, (list, tuple)):
            lines.append(f"{k} = [" + ", ".join(repr(x) for x in v) + "]")
        else:
            lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


def _build_published_index(parent: Path, slug: str, *,
                           pages: int = 3,
                           toml_section: dict | None = None,
                           seed_section: dict | None = None,
                           unseen_urls: tuple[str, ...] = ()) -> Path:
    """Build a minimal sift root with a published current/ snapshot and
    ``pages`` markdown files under md/. Used to populate multi-root
    fixtures so the dispatcher has real content to route over.

    Optional ``seed_section`` writes a ``[seed]`` block (used by the
    writeable tests to populate ``host_allow``). ``unseen_urls`` are
    inserted into the manifest in state='UNSEEN' so list_indexes
    surfaces a nonzero unseen_count for that index.
    """
    root = parent / slug
    rid = "1970-01-01T00-00-00_test"
    md = paths.run_dir(root, rid) / "md"
    md.mkdir(parents=True)
    for i in range(pages):
        body = f"page {i} from {slug}\n"
        if i == 0:
            body += "special-token-x\n"
        (md / f"page-{i}.md").write_text(
            f"---\nurl: https://{slug}.test/p/{i}\n"
            f"content_hash: sha256:c{i}\n---\n{body}"
        )
    paths.snapshot_path(root, rid).write_text(
        json.dumps({"counts_by_state": {"FRESH": pages}})
    )
    sections: list[str] = []
    if toml_section is not None:
        sections.append(_format_section("index", toml_section))
    if seed_section is not None:
        sections.append(_format_section("seed", seed_section))
    if sections:
        (root / "sift.toml").write_text("\n".join(sections))
    (root / "current").symlink_to(paths.run_dir(root, rid))

    # Manifest with optional UNSEEN rows for unseen_count tests
    from sift.manifest import init_schema, now_utc, open_db, transaction, upsert_seed
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    if unseen_urls:
        now = now_utc()
        with transaction(conn):
            for url in unseen_urls:
                upsert_seed(conn, url, "LIVING", None, "v1", None, now)
    conn.close()
    return root


@pytest.fixture
def multi_root(tmp_path):
    """Parent dir containing three published indexes. Two have descriptive
    [index] sections; one falls back to dir-name slug + inferred domain."""
    _build_published_index(
        tmp_path, "alpha", pages=3,
        toml_section={"description": "alpha docs",
                      "domain": "alpha.test"},
    )
    _build_published_index(
        tmp_path, "beta", pages=2,
        toml_section={"description": "beta docs",
                      "domain": "beta.test"},
    )
    _build_published_index(tmp_path, "gamma-unlabeled", pages=1)
    return tmp_path


@pytest.fixture
def single_root(tmp_path):
    """One published sift index — the legacy single-root layout."""
    return _build_published_index(tmp_path, "solo", pages=2,
                                  toml_section={
                                      "slug": "solo-slug",
                                      "description": "solo docs",
                                  })


# ---- helpers --------------------------------------------------------------

def _call(server, name: str, arguments: dict):
    """Drive the MCP server's call_tool handler synchronously. Each call
    runs in its own event loop — use ``acall`` instead for tests that
    chain multiple calls and need the background tasks from earlier
    calls to still be live."""
    handler = server.request_handlers.get("tools/call")
    if handler is None:
        from mcp.types import CallToolRequest
        handler = server.request_handlers[CallToolRequest]
    req_obj = mcp_server.mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_server.mcp_types.CallToolRequestParams(
            name=name, arguments=arguments,
        ),
    )
    result = asyncio.run(handler(req_obj))
    return result.root


async def acall(server, name: str, arguments: dict):
    """Async version of ``_call`` — does not create a fresh event loop, so
    background tasks created during one call survive into later calls.
    Use for tests that exercise per-slug or global concurrency state."""
    handler = server.request_handlers.get("tools/call")
    if handler is None:
        from mcp.types import CallToolRequest
        handler = server.request_handlers[CallToolRequest]
    req_obj = mcp_server.mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_server.mcp_types.CallToolRequestParams(
            name=name, arguments=arguments,
        ),
    )
    result = await handler(req_obj)
    return result.root


def _list_tools(server):
    handler = server.request_handlers.get("tools/list")
    if handler is None:
        from mcp.types import ListToolsRequest
        handler = server.request_handlers[ListToolsRequest]
    req = mcp_server.mcp_types.ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(req))
    return result.root.tools


def _text(server_result):
    """Extract the text body from a CallToolResult-shaped server result."""
    res = server_result
    # In multi-handler responses the SDK wraps content sometimes; unwrap.
    if hasattr(res, "content"):
        return res.content[0].text if res.content else ""
    return str(res)


# ---- mode detection -------------------------------------------------------

class TestMode:
    def test_multi_mode_lists_list_indexes_tool(self, multi_root):
        server = mcp_server.build_server(multi_root)
        names = [t.name for t in _list_tools(server)]
        assert "list_indexes" in names

    def test_single_mode_omits_list_indexes_tool(self, single_root):
        server = mcp_server.build_server(single_root)
        names = [t.name for t in _list_tools(server)]
        assert "list_indexes" not in names

    def test_multi_mode_adds_index_param_to_read_md(self, multi_root):
        server = mcp_server.build_server(multi_root)
        tools = {t.name: t for t in _list_tools(server)}
        read_md = tools["read_md"]
        assert "index" in (read_md.inputSchema.get("properties") or {})
        assert "index" in (read_md.inputSchema.get("required") or [])

    def test_multi_mode_index_param_optional_on_grep(self, multi_root):
        server = mcp_server.build_server(multi_root)
        tools = {t.name: t for t in _list_tools(server)}
        grep = tools["grep_corpus"]
        assert "index" in (grep.inputSchema.get("properties") or {})
        assert "index" not in (grep.inputSchema.get("required") or [])

    def test_single_mode_no_index_param(self, single_root):
        server = mcp_server.build_server(single_root)
        tools = {t.name: t for t in _list_tools(server)}
        for n in ("read_md", "grep_corpus", "list_dir", "read_facts"):
            assert "index" not in (tools[n].inputSchema.get("properties") or {}), \
                f"{n} should not have an index param in single-mode"


# ---- list_indexes ---------------------------------------------------------

class TestListIndexes:
    def test_returns_each_registered_index(self, multi_root):
        server = mcp_server.build_server(multi_root)
        res = _call(server, "list_indexes", {})
        body = json.loads(_text(res))
        slugs = sorted(d["slug"] for d in body["indexes"])
        assert slugs == ["alpha", "beta", "gamma-unlabeled"]
        # Operator-supplied description on alpha; inferred on gamma
        by_slug = {d["slug"]: d for d in body["indexes"]}
        assert by_slug["alpha"]["description"] == "alpha docs"
        assert "gamma-unlabeled" in by_slug["gamma-unlabeled"]["description"]
        # page_count from snapshot.json
        assert by_slug["alpha"]["page_count"] == 3
        assert by_slug["beta"]["page_count"] == 2

    def test_single_mode_rejects_list_indexes(self, single_root):
        server = mcp_server.build_server(single_root)
        res = _call(server, "list_indexes", {})
        assert res.isError
        assert "only available in multi-index" in _text(res)


# ---- routing: read_md -----------------------------------------------------

class TestRouting:
    def test_read_md_routes_by_slug(self, multi_root):
        server = mcp_server.build_server(multi_root)
        res = _call(server, "read_md", {
            "index": "alpha", "path": "md/page-0.md",
        })
        assert not res.isError, _text(res)
        assert "alpha" in _text(res)

    def test_read_md_missing_index_errors(self, multi_root):
        # The MCP SDK validates required fields against the schema before
        # the call reaches our dispatcher, so a missing `index` raises a
        # schema-validation error rather than our custom hint. Either way,
        # the model sees that `index` is required — which is what matters
        # for self-correction.
        server = mcp_server.build_server(multi_root)
        res = _call(server, "read_md", {"path": "md/page-0.md"})
        assert res.isError
        assert "index" in _text(res).lower()

    def test_read_md_unknown_slug_errors(self, multi_root):
        server = mcp_server.build_server(multi_root)
        res = _call(server, "read_md", {
            "index": "not-a-thing", "path": "md/page-0.md",
        })
        assert res.isError
        assert "Unknown index slug" in _text(res)

    def test_single_mode_read_md_works_without_index_param(self, single_root):
        server = mcp_server.build_server(single_root)
        res = _call(server, "read_md", {"path": "md/page-0.md"})
        assert not res.isError, _text(res)
        assert "solo" in _text(res)


# ---- routing: grep_corpus (scoped + fan-out) ------------------------------

class TestGrep:
    def test_scoped_grep_returns_one_match_no_fanout_header(self, multi_root):
        # special-token-x is in page-0.md of every fixture index. Scoped
        # to alpha, we should see exactly one match line + no fan-out
        # headers (those only appear in cross-corpus mode).
        server = mcp_server.build_server(multi_root)
        res = _call(server, "grep_corpus", {
            "index": "alpha", "pattern": "special-token-x",
        })
        text = _text(res)
        assert "special-token-x" in text
        assert "===== index:" not in text
        # Single match, not the 3 we'd see in fan-out mode
        assert text.count("special-token-x") == 1

    def test_fanout_grep_includes_per_index_headers(self, multi_root):
        # Make the token appear in every index so we can confirm fan-out
        for slug in ("alpha", "beta", "gamma-unlabeled"):
            for p in (multi_root / slug).rglob("page-0.md"):
                p.write_text(p.read_text() + "\nfanout-needle\n")
        server = mcp_server.build_server(multi_root)
        # No `index` arg → fan out
        res = _call(server, "grep_corpus", {"pattern": "fanout-needle"})
        text = _text(res)
        for slug in ("alpha", "beta", "gamma-unlabeled"):
            assert f"index: {slug}" in text

    def test_star_index_fans_out_explicitly(self, multi_root):
        server = mcp_server.build_server(multi_root)
        # Add the token to two of three indexes
        for slug in ("alpha", "beta"):
            for p in (multi_root / slug).rglob("page-0.md"):
                p.write_text(p.read_text() + "\nstar-fan\n")
        res = _call(server, "grep_corpus", {
            "pattern": "star-fan", "index": "*",
        })
        text = _text(res)
        assert "index: alpha" in text
        assert "index: beta" in text


# ---- query_manifest scoping ----------------------------------------------

class TestQueryManifest:
    def test_query_against_known_slug(self, multi_root):
        # Just confirm dispatch — the manifest may be empty (no rows
        # registered for fixture pages), but the SQL should run.
        # Initialize the manifest so query has a table.
        from sift.manifest import init_schema, open_db
        for slug in ("alpha", "beta", "gamma-unlabeled"):
            conn = open_db(paths.manifest_path(multi_root / slug))
            init_schema(conn)
            conn.close()
        server = mcp_server.build_server(multi_root)
        res = _call(server, "query_manifest", {
            "index": "alpha",
            "sql": "SELECT name FROM sqlite_master WHERE type='table'",
        })
        assert not res.isError, _text(res)


# ---- snapshot_status fan-out fallback ------------------------------------

class TestSnapshotStatus:
    def test_scoped_status(self, multi_root):
        server = mcp_server.build_server(multi_root)
        res = _call(server, "snapshot_status", {"index": "alpha"})
        body = json.loads(_text(res))
        assert body["published"] is True

    def test_fanout_status_returns_per_index_sections(self, multi_root):
        server = mcp_server.build_server(multi_root)
        res = _call(server, "snapshot_status", {})
        text = _text(res)
        for slug in ("alpha", "beta", "gamma-unlabeled"):
            assert f"index: {slug}" in text


# ---- Write-side discovery: writeable flag + unseen_count ------------------

@pytest.fixture
def writeable_multi_root(tmp_path):
    """A multi-root fixture where one index has a writable [seed].host_allow
    and another doesn't. unseen_count populated on the writeable one."""
    _build_published_index(
        tmp_path, "writable", pages=2,
        toml_section={"description": "writable corpus",
                      "domain": "writable.test"},
        seed_section={"host_allow": ["writable.test"]},
        unseen_urls=("https://writable.test/p/100",
                     "https://writable.test/p/101"),
    )
    _build_published_index(
        tmp_path, "readonly", pages=2,
        toml_section={"description": "no allow-list",
                      "domain": "readonly.test"},
    )
    return tmp_path


class TestListIndexesWriteSurface:
    def test_writeable_flag_reflects_allow_list_and_enable_flag(
            self, writeable_multi_root):
        # --enable-index OFF → everything is writeable=False regardless of
        # sift.toml.
        server = mcp_server.build_server(writeable_multi_root)
        body = json.loads(_text(_call(server, "list_indexes", {})))
        assert body["write_enabled"] is False
        for d in body["indexes"]:
            assert d["writeable"] is False

        # --enable-index ON → only the slug with host_allow flips to True.
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        body = json.loads(_text(_call(server, "list_indexes", {})))
        assert body["write_enabled"] is True
        per = {d["slug"]: d for d in body["indexes"]}
        assert per["writable"]["writeable"] is True
        assert per["writable"]["allowed_hosts"] == ["writable.test"]
        assert per["readonly"]["writeable"] is False
        assert per["readonly"]["accepts_writes"] is False

    def test_unseen_count_surfaces_in_list_indexes(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        body = json.loads(_text(_call(server, "list_indexes", {})))
        per = {d["slug"]: d for d in body["indexes"]}
        # Two UNSEEN rows inserted in fixture
        assert per["writable"]["unseen_count"] == 2
        # No manifest writes on readonly fixture
        assert per["readonly"]["unseen_count"] == 0

    def test_concurrent_cap_visible(self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
            max_concurrent_crawls=7,
        )
        body = json.loads(_text(_call(server, "list_indexes", {})))
        assert body["concurrent_crawl_cap"] == 7
        assert body["active_crawls"] == 0


# ---- Write-side dispatch: index_url + index_status routing ----------------

class TestWriteRouting:
    """Most tests stub _run_index_job so the subprocess chain doesn't fire.
    The harness validates *up to* the create_task hand-off — that's where
    routing decisions live."""

    @pytest.fixture(autouse=True)
    def stub_run(self, monkeypatch):
        async def never(*a, **k):
            import asyncio
            await asyncio.sleep(60)
        monkeypatch.setattr(mcp_server, "_run_index_job", never)

    def test_index_url_routes_by_slug(self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_url", {
            "index": "writable",
            "urls": ["https://writable.test/p/200"],
        })
        body = json.loads(_text(res))
        assert body["status"] == "started"
        assert body["index"] == "writable"
        assert "-idx" in body["run_id"]
        assert "writable" in body["poll"]

    def test_missing_index_arg_in_multi_mode_is_schema_error(
            self, writeable_multi_root):
        # The SDK enforces required fields before our dispatcher sees the
        # call, so the agent learns "index is required" at schema-validation
        # time.
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_url", {
            "urls": ["https://writable.test/p/200"],
        })
        assert res.isError
        assert "index" in _text(res).lower()

    def test_unknown_slug_lists_registered_slugs(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_url", {
            "index": "does-not-exist",
            "urls": ["https://writable.test/p/200"],
        })
        assert res.isError
        body = _text(res)
        assert "Unknown index slug" in body
        for slug in ("writable", "readonly"):
            assert slug in body

    def test_unwriteable_slug_returns_helpful_error(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_url", {
            "index": "readonly",
            "urls": ["https://readonly.test/p/200"],
        })
        assert res.isError
        msg = _text(res)
        assert "not writeable" in msg
        assert "sift.toml" in msg

    def test_off_host_url_rejected_with_allowed_hosts_in_error(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_url", {
            "index": "writable",
            "urls": ["https://evil.test/p/1"],
        })
        assert res.isError
        msg = _text(res)
        assert "writable.test" in msg            # the allowed host
        assert "list_indexes" in msg              # the recovery hint

    async def test_two_calls_on_same_slug_returns_busy_error(
            self, writeable_multi_root):
        # async so the background task created by the first call survives
        # to the second call (same event loop).
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        r1 = await acall(server, "index_url", {
            "index": "writable",
            "urls": ["https://writable.test/p/200"],
        })
        assert not r1.isError
        r2 = await acall(server, "index_url", {
            "index": "writable",
            "urls": ["https://writable.test/p/201"],
        })
        assert r2.isError
        msg = _text(r2)
        assert "already has a crawl" in msg
        assert json.loads(_text(r1))["run_id"] in msg

    async def test_cross_slug_concurrency_allowed(self, tmp_path):
        # Two writable slugs — separate per-slug state, both can run.
        _build_published_index(
            tmp_path, "x1", pages=1,
            seed_section={"host_allow": ["x1.test"]},
        )
        _build_published_index(
            tmp_path, "x2", pages=1,
            seed_section={"host_allow": ["x2.test"]},
        )
        server = mcp_server.build_server(tmp_path, enable_index=True)
        r1 = await acall(server, "index_url", {
            "index": "x1", "urls": ["https://x1.test/p/1"]})
        r2 = await acall(server, "index_url", {
            "index": "x2", "urls": ["https://x2.test/p/1"]})
        assert not r1.isError, _text(r1)
        assert not r2.isError, _text(r2)
        b1 = json.loads(_text(r1))
        b2 = json.loads(_text(r2))
        assert b1["run_id"] != b2["run_id"]

    async def test_global_concurrent_cap_blocks_third(self, tmp_path):
        for slug in ("a", "b", "c"):
            _build_published_index(
                tmp_path, slug, pages=1,
                seed_section={"host_allow": [f"{slug}.test"]},
            )
        server = mcp_server.build_server(
            tmp_path, enable_index=True, max_concurrent_crawls=2,
        )
        r1 = await acall(server, "index_url", {
            "index": "a", "urls": ["https://a.test/p/1"]})
        r2 = await acall(server, "index_url", {
            "index": "b", "urls": ["https://b.test/p/1"]})
        r3 = await acall(server, "index_url", {
            "index": "c", "urls": ["https://c.test/p/1"]})
        assert not r1.isError
        assert not r2.isError
        assert r3.isError
        msg = _text(r3)
        assert "cap" in msg.lower()
        assert "a" in msg and "b" in msg     # active slugs surfaced

    def test_index_status_routes_by_slug_in_multi_mode(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        # No run with this id yet; we expect an "unknown run_id" error
        # routed to the right index — the validation step succeeded.
        res = _call(server, "index_status", {
            "index": "writable", "run_id": "never-started",
        })
        assert res.isError
        assert "Unknown run_id" in _text(res)

    def test_index_status_missing_index_in_multi_mode_is_schema_error(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        res = _call(server, "index_status", {"run_id": "rid"})
        assert res.isError
        assert "index" in _text(res).lower()

    async def test_new_index_appears_without_restart(self, tmp_path):
        """The agent-first loop breaker fix: a sub-index built AFTER the
        MCP server started should show up in list_indexes once the
        registry TTL elapses, without a server restart.

        Before this fix, the agent had to ask the operator to drop and
        re-add the MCP server every time it built a new index — that's
        the friction the user's harness session called out as the #1
        gap to close."""
        # Start the server pointing at an empty parent dir.
        _build_published_index(tmp_path, "before",
                               seed_section={"host_allow": ["before.test"]})
        server = mcp_server.build_server(
            tmp_path, enable_index=True,
            registry_ttl_seconds=0.0,        # rebuild on every call
        )
        body = json.loads(_text(await acall(server, "list_indexes", {})))
        assert {d["slug"] for d in body["indexes"]} == {"before"}

        # Build a NEW index after the server is already running.
        _build_published_index(tmp_path, "after-bootstrap",
                               seed_section={"host_allow": ["after.test"]})

        # The next list_indexes call must see it — no restart needed.
        body = json.loads(_text(await acall(server, "list_indexes", {})))
        slugs = {d["slug"] for d in body["indexes"]}
        assert "after-bootstrap" in slugs
        assert "before" in slugs

    async def test_registry_ttl_caches_within_window(self, tmp_path):
        """Within the cache TTL, a freshly-built index should NOT yet
        appear — proves the cache is doing what it says. After the TTL
        elapses, it does. Important for performance reasoning so an
        agent doing 50 grep calls in 200ms doesn't trigger 50 full
        discoveries."""
        _build_published_index(tmp_path, "before",
                               seed_section={"host_allow": ["before.test"]})
        server = mcp_server.build_server(
            tmp_path, enable_index=True,
            registry_ttl_seconds=10.0,      # long enough to test
        )
        # Warm the cache
        await acall(server, "list_indexes", {})
        # Build a new index — within the TTL, it's not yet visible
        _build_published_index(tmp_path, "during-ttl",
                               seed_section={"host_allow": ["after.test"]})
        body = json.loads(_text(await acall(server, "list_indexes", {})))
        slugs = {d["slug"] for d in body["indexes"]}
        # The agent sees the stale cache here — that's the tradeoff
        # operators tune via --registry-ttl-ms.
        assert "during-ttl" not in slugs

    async def test_active_run_visible_in_list_indexes_during_crawl(
            self, writeable_multi_root):
        server = mcp_server.build_server(
            writeable_multi_root, enable_index=True,
        )
        await acall(server, "index_url", {
            "index": "writable",
            "urls": ["https://writable.test/p/300"],
        })
        body = json.loads(_text(await acall(server, "list_indexes", {})))
        per = {d["slug"]: d for d in body["indexes"]}
        assert per["writable"].get("active_run") is not None
        assert per["writable"]["active_run"]["phase"] in (
            "seeding", "running"
        )
        assert body["active_crawls"] == 1


# ---- Single-mode write-side regression -----------------------------------

class TestSingleModeWriteCompat:
    """The legacy single-root deployment must keep working without an
    `index` argument on index_url / index_status."""

    def test_single_mode_index_url_no_index_arg_needed(self, tmp_path):
        # Build a single-root with [seed].host_allow so index_url is usable.
        _build_published_index(
            tmp_path, "_root_marker",   # this slug shouldn't be visible
            seed_section={"host_allow": ["solo.test"]},
        )
        # NOTE: build_server points at the sub-dir directly → single mode
        root = tmp_path / "_root_marker"
        server = mcp_server.build_server(
            root, enable_index=True,
            host_allow={"solo.test"},
            config_path=root / "sift.toml",
        )
        # Multi-mode tools shouldn't appear
        names = [t.name for t in _list_tools(server)]
        assert "list_indexes" not in names
        # No index arg required in the schema either
        tools = {t.name: t for t in _list_tools(server)}
        assert "index" not in (tools["index_url"].inputSchema.get("properties") or {})
