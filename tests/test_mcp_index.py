"""Tests for the opt-in index_url / index_status MCP write tools.

Covers the security-critical surface:
  * write tools are hidden + refused unless --enable-index
  * host allow-list enforcement (off-host, userinfo bypass, non-http schemes)
  * input validation (shape, count cap)
  * single in-flight job concurrency guard
  * the async job runner's seed->run subprocess chain (with _spawn stubbed)
  * index_status reads durable run state from the runs table
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sift import mcp_server, paths
from sift.manifest import (
    init_schema, now_utc, open_db, record_run_end, record_run_phase,
    record_run_start, transaction,
)
from sift.mcp_server import (
    _IndexJobState, _RegistryJobState, _dispatch_index, _host_allowed,
    _run_index_job, _tool_descriptors, tool_index_status,
)
from sift.registry import IndexRegistry

ALLOW = {"www.ato.gov.au"}


def _text(result):
    assert result.content
    return result.content[0].text


# ---- Descriptor gating ------------------------------------------------------

class TestDescriptorGating:
    def test_read_only_by_default(self):
        names = [t.name for t in _tool_descriptors()]
        assert "index_url" not in names
        assert "index_status" not in names

    def test_index_tools_appended_when_enabled(self):
        names = [t.name for t in _tool_descriptors(include_index=True)]
        assert names[-2:] == ["index_url", "index_status"]

    def test_index_url_is_not_read_only(self):
        tools = {t.name: t for t in _tool_descriptors(include_index=True)}
        # index_url mutates state — must NOT advertise readOnlyHint
        ann = tools["index_url"].annotations
        assert ann is None or not getattr(ann, "readOnlyHint", False)
        # index_status is a pure read
        assert tools["index_status"].annotations.readOnlyHint is True


# ---- Host allow-list (SSRF guard) ------------------------------------------

class TestHostAllowed:
    @pytest.mark.parametrize("url,ok", [
        ("https://www.ato.gov.au/x", True),
        ("http://www.ato.gov.au/x", True),
        ("https://www.ato.gov.au:443/x", True),         # explicit port ok
        ("https://evil.com/x", False),                  # other host
        ("https://www.ato.gov.au.evil.com/x", False),   # suffix trick
        ("https://www.ato.gov.au@evil.com/x", False),   # userinfo points elsewhere
        ("ftp://www.ato.gov.au/x", False),              # non-http scheme
        ("file:///etc/passwd", False),                  # file scheme
        ("not a url", False),
        ("", False),
    ])
    def test_cases(self, url, ok):
        assert _host_allowed(url, ALLOW) is ok


# ---- Dispatcher: gating, validation, host rejection, concurrency -----------

def _state():
    return _IndexJobState()


def _single_registry(root):
    """Fake single-index registry — bypasses the discovery step in tests
    that don't care about a real sift root."""
    return IndexRegistry(indexes=(), is_multi=False, parent_path=root)


def _single_job_state(state):
    """Build a registry-level job state seeded with ``state`` as the
    single-mode slug's entry. Lets tests inspect/mutate the per-slug
    state via the same _IndexJobState shape they used pre-refactor."""
    js = _RegistryJobState()
    if state is not None:
        js.per_slug[mcp_server._SINGLE_MODE_SLUG] = state
    return js


async def _dispatch(name, args, *, enable_index=True, allow=ALLOW,
                    state=None, job_state=None, root=None, config_path=None,
                    tmp_path=None):
    target_root = root or tmp_path
    if job_state is None:
        job_state = _single_job_state(state)
    return await _dispatch_index(
        name, args,
        enable_index=enable_index,
        registry=_single_registry(target_root),
        job_state=job_state,
        legacy_root=target_root,
        legacy_allow=allow,
        legacy_config_path=config_path,
    )


class TestDispatchGuard:
    async def test_non_index_tool_passes_through(self, tmp_path):
        r = await _dispatch("read_md", {"path": "x"}, tmp_path=tmp_path)
        assert r is None

    async def test_disabled_returns_error(self, tmp_path):
        r = await _dispatch("index_url", {"urls": ["https://www.ato.gov.au/x"]},
                            enable_index=False, tmp_path=tmp_path)
        assert r.isError
        assert "disabled" in _text(r).lower()

    async def test_index_status_disabled_too(self, tmp_path):
        r = await _dispatch("index_status", {"run_id": "x"},
                            enable_index=False, tmp_path=tmp_path)
        assert r.isError and "disabled" in _text(r).lower()


class TestIndexUrlValidation:
    @pytest.mark.parametrize("urls", [[], "notalist", [123], ["ok", 5]])
    async def test_bad_shape(self, tmp_path, urls):
        r = await _dispatch("index_url", {"urls": urls}, tmp_path=tmp_path)
        assert r.isError
        assert "array of strings" in _text(r) or "non-empty" in _text(r)

    async def test_missing_urls(self, tmp_path):
        r = await _dispatch("index_url", {}, tmp_path=tmp_path)
        assert r.isError

    async def test_too_many(self, tmp_path):
        urls = [f"https://www.ato.gov.au/{i}" for i in range(mcp_server.MAX_INDEX_URLS + 1)]
        r = await _dispatch("index_url", {"urls": urls}, tmp_path=tmp_path)
        assert r.isError and "Too many" in _text(r)

    async def test_off_host_rejected(self, tmp_path):
        r = await _dispatch("index_url", {"urls": ["https://evil.com/x"]}, tmp_path=tmp_path)
        assert r.isError
        assert "Refused" in _text(r)

    async def test_partial_rejection_blocks_whole_call(self, tmp_path):
        r = await _dispatch("index_url",
                            {"urls": ["https://www.ato.gov.au/ok", "https://evil.com/x"]},
                            tmp_path=tmp_path)
        assert r.isError and "Refused 1 of 2" in _text(r)


class TestIndexUrlLaunch:
    async def test_success_returns_run_id_and_sets_state(self, tmp_path, monkeypatch):
        # Stub the actual crawl so nothing spawns.
        called = {}

        async def fake_job(state, root, run_id, urls, config_path):
            called["run_id"] = run_id
            called["urls"] = urls
            state.phase = "idle"

        monkeypatch.setattr(mcp_server, "_run_index_job", fake_job)
        state = _state()
        r = await _dispatch("index_url", {"urls": ["https://www.ato.gov.au/x"]},
                            state=state, tmp_path=tmp_path)
        assert not r.isError
        payload = json.loads(_text(r))
        assert payload["status"] == "started"
        assert "-idx" in payload["run_id"]
        assert state.run_id == payload["run_id"]
        assert state.task is not None
        await state.task  # let the stub finish cleanly
        assert called["run_id"] == payload["run_id"]
        assert called["urls"] == ["https://www.ato.gov.au/x"]

    async def test_concurrency_guard(self, tmp_path, monkeypatch):
        async def never(*a, **k):
            await asyncio.sleep(60)

        monkeypatch.setattr(mcp_server, "_run_index_job", never)
        state = _state()
        r1 = await _dispatch("index_url", {"urls": ["https://www.ato.gov.au/a"]},
                             state=state, tmp_path=tmp_path)
        assert not r1.isError
        r2 = await _dispatch("index_url", {"urls": ["https://www.ato.gov.au/b"]},
                             state=state, tmp_path=tmp_path)
        assert r2.isError and "already has a crawl in progress" in _text(r2)
        state.task.cancel()


# ---- The async job runner (subprocess chain) -------------------------------

class TestRunIndexJob:
    async def test_seed_then_run_with_correct_args(self, tmp_path, monkeypatch):
        cmds: list = []
        # The fake spawn must capture --only-urls file CONTENTS before the
        # _run_index_job finally-block cleans the tmpdir; the path itself
        # is gone by the time the test asserts.
        only_urls_contents: list[str] = []

        async def fake_spawn(cmd):
            cmds.append(cmd)
            if "--only-urls" in cmd:
                p = Path(cmd[cmd.index("--only-urls") + 1])
                only_urls_contents.append(p.read_text())
            return 0, "ok"

        monkeypatch.setattr(mcp_server.shutil, "which", lambda _: "/usr/bin/sift")
        monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
        state = _IndexJobState(run_id="rid", phase="seeding")
        await _run_index_job(state, tmp_path, "rid",
                             ["https://www.ato.gov.au/x"], config_path=None)
        assert len(cmds) == 2
        assert cmds[0][1] == "seed" and "--from-json" in cmds[0]
        assert cmds[1][1] == "run" and "--run-id" in cmds[1]
        assert cmds[1][cmds[1].index("--run-id") + 1] == "rid"
        # Targeted backfill: the run command MUST pass --only-urls so the
        # plan is scoped to the agent's requested URLs (not the whole
        # UNSEEN backlog).
        assert "--only-urls" in cmds[1]
        assert only_urls_contents
        assert "https://www.ato.gov.au/x" in only_urls_contents[0]
        assert state.phase == "idle"

    async def test_config_path_threaded_through(self, tmp_path, monkeypatch):
        cmds = []

        async def fake_spawn(cmd):
            cmds.append(cmd)
            return 0, ""

        monkeypatch.setattr(mcp_server.shutil, "which", lambda _: "/usr/bin/sift")
        monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
        cfg = tmp_path / "sift.toml"
        state = _IndexJobState(run_id="rid")
        await _run_index_job(state, tmp_path, "rid",
                             ["https://www.ato.gov.au/x"], config_path=cfg)
        for c in cmds:
            assert "--config" in c and str(cfg) in c

    async def test_seed_failure_aborts_before_run(self, tmp_path, monkeypatch):
        cmds = []

        async def fake_spawn(cmd):
            cmds.append(cmd)
            return 1, "seed boom"

        monkeypatch.setattr(mcp_server.shutil, "which", lambda _: "/usr/bin/sift")
        monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
        state = _IndexJobState(run_id="rid")
        await _run_index_job(state, tmp_path, "rid",
                             ["https://www.ato.gov.au/x"], config_path=None)
        assert len(cmds) == 1  # run never invoked
        assert state.phase == "failed" and "seed failed" in state.error

    async def test_degraded_run_is_not_a_failure(self, tmp_path, monkeypatch):
        async def fake_spawn(cmd):
            return (0, "") if cmd[1] == "seed" else (2, "degraded")

        monkeypatch.setattr(mcp_server.shutil, "which", lambda _: "/usr/bin/sift")
        monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
        state = _IndexJobState(run_id="rid")
        await _run_index_job(state, tmp_path, "rid",
                             ["https://www.ato.gov.au/x"], config_path=None)
        assert state.phase == "idle"  # rc=2 = gate degraded, pipeline still ran

    async def test_missing_sift_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server.shutil, "which", lambda _: None)
        state = _IndexJobState(run_id="rid")
        await _run_index_job(state, tmp_path, "rid",
                             ["https://www.ato.gov.au/x"], config_path=None)
        assert state.phase == "failed" and "PATH" in state.error


# ---- index_status reads durable run state ----------------------------------

class TestIndexStatus:
    def _manifest_with_run(self, tmp_path, run_id, *, status, phase="publish",
                           counts=None):
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        with transaction(conn):
            record_run_start(conn, run_id, now_utc())
            record_run_phase(conn, run_id, phase)
            if status != "running":
                record_run_end(conn, run_id, now_utc(), status,
                               json.dumps(counts or {"FRESH": 1}), None)
        conn.close()

    def test_succeeded_run(self, tmp_path):
        self._manifest_with_run(tmp_path, "rid", status="succeeded")
        r = tool_index_status(tmp_path, "rid", _state())
        out = json.loads(_text(r))
        assert out["status"] == "succeeded"
        assert out["counts"] == {"FRESH": 1}
        assert "snapshot_status" in out["next_step"]

    def test_running_run(self, tmp_path):
        self._manifest_with_run(tmp_path, "rid", status="running", phase="fetch")
        r = tool_index_status(tmp_path, "rid", _state())
        out = json.loads(_text(r))
        assert out["status"] == "running" and out["phase"] == "fetch"

    def test_published_as_current_flag(self, tmp_path):
        self._manifest_with_run(tmp_path, "rid", status="succeeded")
        run_dir = paths.run_dir(tmp_path, "rid")
        run_dir.mkdir(parents=True, exist_ok=True)
        cur = paths.current_symlink(tmp_path)
        if cur.exists() or cur.is_symlink():
            cur.unlink()
        cur.symlink_to(run_dir.resolve(), target_is_directory=True)
        r = tool_index_status(tmp_path, "rid", _state())
        assert json.loads(_text(r))["published_as_current"] is True

    def test_unknown_run_id_errors(self, tmp_path):
        self._manifest_with_run(tmp_path, "other", status="succeeded")
        r = tool_index_status(tmp_path, "missing", _state())
        assert r.isError and "Unknown run_id" in _text(r)

    def test_seeding_window_falls_back_to_state(self, tmp_path):
        # No manifest yet — job is still in the pre-run seeding window.
        state = _IndexJobState(run_id="rid", phase="seeding")
        r = tool_index_status(tmp_path, "rid", state)
        out = json.loads(_text(r))
        assert out["status"] == "seeding"

    def test_seed_failure_surfaced_via_state(self, tmp_path):
        state = _IndexJobState(run_id="rid", phase="failed", error="seed failed (rc=1): boom")
        r = tool_index_status(tmp_path, "rid", state)
        out = json.loads(_text(r))
        assert out["status"] == "failed" and "boom" in out["error"]
