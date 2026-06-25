"""Tests for proof-carrying answers: integrity.merkle_proof/fold_proof, the
prove orchestration (md-reconstruction + root self-check), the MCP `prove` tool,
and the standalone stdlib verifier.

The binding guarantee under test: a proof is emitted ONLY if the leaves
reconstructed from a run's md tree recompute to the root stored in its
snapshot.json, and any tamper to the envelope fails verification.
"""

import asyncio
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from sift import integrity, mcp_server, paths, prove, verify_proof

SCHEME = "sorted-leaves-bitcoin-style-sha256"


# ---- fixtures -------------------------------------------------------------

def _md(url: str, ch: str) -> str:
    return f"---\nurl: {url}\ncontent_hash: sha256:{ch}\ntier: LIVING\n---\nbody of {url}\n"


def _h(seed: str) -> str:
    """A deterministic 64-hex content_hash from a seed (real hex, not a real body hash)."""
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


def _write_run(root: Path, run_id: str, pages: dict, *, status="published",
               current=True) -> Path:
    """pages: {relpath: (url, content_hash_hex)}. Writes md + a snapshot.json whose
    merkle_root is the REAL root of those leaves (so the self-check passes)."""
    rd = paths.run_dir(root, run_id)
    (rd / "md").mkdir(parents=True, exist_ok=True)
    for rel, (url, ch) in pages.items():
        f = rd / "md" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_md(url, ch))
    root_hex, count = integrity.compute_corpus_root([(u, c) for (u, c) in pages.values()])
    (rd / "snapshot.json").write_text(json.dumps({
        "run_id": run_id, "status": status, "completed_at": "2026-01-01T00:00:01Z",
        "versions": {"integrity": integrity.INTEGRITY_VERSION},
        "integrity": {"merkle_root": root_hex, "leaf_count": count, "scheme": SCHEME},
    }))
    if current:
        link = paths.current_symlink(root)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(rd.resolve(), target_is_directory=True)
    return rd


@pytest.fixture
def index(tmp_path):
    pages = {
        "a.md": ("https://x/alpha", _h("alpha")),
        "b.md": ("https://x/beta", _h("beta")),
        "c.md": ("https://x/gamma", _h("gamma")),
        "d.md": ("https://x/delta", _h("delta")),
        "e.md": ("https://x/epsilon", _h("epsilon")),   # 5 leaves → odd internal level
    }
    _write_run(tmp_path, "20260101T000001Z", pages)
    return tmp_path


def _run_dir(index):
    return paths.published_run_dir(index)


# ---- property: every leaf folds to the stored root ------------------------

class TestProofFoldsToRoot:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 9, 16, 17])
    def test_every_leaf_proves_for_size(self, n):
        rng = random.Random(n)
        leaves = sorted({integrity.leaf_hash(f"u{i}", _h(f"{n}:{i}:{rng.random()}"))
                         for i in range(n)})
        root = integrity.merkle_root(leaves)
        for leaf in leaves:
            proof = integrity.merkle_proof(leaves, leaf)
            assert proof is not None
            assert integrity.fold_proof(leaf, proof) == root, f"n={n} leaf={leaf[:8]}"

    def test_cross_check_vs_compute_corpus_root(self):
        pairs = [(f"https://x/{i}", _h(str(i))) for i in range(13)]
        root_a, _ = integrity.compute_corpus_root(pairs)
        root_b = integrity.merkle_root([integrity.leaf_hash(u, c) for u, c in pairs])
        assert root_a == root_b

    def test_order_independent(self):
        pairs = [(f"https://x/{i}", _h(str(i))) for i in range(11)]
        a, _ = integrity.compute_corpus_root(pairs)
        random.Random(0).shuffle(pairs)
        b, _ = integrity.compute_corpus_root(pairs)
        assert a == b

    def test_missing_target_returns_none(self):
        leaves = [integrity.leaf_hash(f"u{i}", _h(str(i))) for i in range(4)]
        assert integrity.merkle_proof(leaves, "f" * 64) is None


# ---- the prove orchestration (self-check) ---------------------------------

class TestBuildProof:
    def test_proves_and_verifies(self, index):
        env = prove.build_proof_for_run(_run_dir(index), "https://x/alpha")
        assert env["included"] is True
        assert env["merkle_root"] == json.loads(
            (_run_dir(index) / "snapshot.json").read_text())["integrity"]["merkle_root"]
        assert integrity.fold_proof(env["leaf"], env["proof"]) == env["merkle_root"]
        ok, _ = verify_proof.verify(env)
        assert ok

    def test_every_page_proves(self, index):
        for url in ("https://x/alpha", "https://x/beta", "https://x/gamma",
                    "https://x/delta", "https://x/epsilon"):
            env = prove.build_proof_for_run(_run_dir(index), url)
            assert env["included"] is True
            assert verify_proof.verify(env)[0]

    def test_absent_url_not_proved(self, index):
        env = prove.build_proof_for_run(_run_dir(index), "https://x/nope")
        assert env["included"] is False and "not in this published snapshot" in env["reason"]

    def test_refuses_tampered_md(self, index):
        # edit one md's content_hash → md root != stored; no manifest → refuse
        md = _run_dir(index) / "md" / "a.md"
        md.write_text(md.read_text().replace(_h("alpha"), _h("EVIL")))
        with pytest.raises(prove.ProofError, match="no leaf source reproduces"):
            prove.build_proof_for_run(_run_dir(index), "https://x/beta")

    def test_refuses_malformed_md(self, index):
        (_run_dir(index) / "md" / "junk.md").write_text("no frontmatter here")
        with pytest.raises(prove.ProofError, match="malformed"):
            prove.build_proof_for_run(_run_dir(index), "https://x/alpha")

    def test_refuses_pre_integrity_snapshot(self, tmp_path):
        rd = _write_run(tmp_path, "r0", {"a.md": ("https://x/a", _h("a"))})
        snap = rd / "snapshot.json"
        s = json.loads(snap.read_text())
        s["integrity"].pop("merkle_root")
        snap.write_text(json.dumps(s))
        with pytest.raises(prove.ProofError, match="no integrity.merkle_root"):
            prove.build_proof_for_run(rd, "https://x/a")

    def test_single_leaf(self, tmp_path):
        rd = _write_run(tmp_path, "r1", {"a.md": ("https://x/solo", _h("solo"))})
        env = prove.build_proof_for_run(rd, "https://x/solo")
        assert env["included"] and env["proof"] == []          # empty path
        assert env["leaf"] == env["merkle_root"]               # merkle_root([x]) == x
        assert verify_proof.verify(env)[0]


class TestMultiSourceLeafSet:
    """Real indexes can have an incomplete md tree (md count < committed
    leaf_count). The prover must fall back to the manifest when it reproduces the
    stored root, and refuse when no source does. (Found by live stress test.)"""

    def _manifest(self, root, rows):
        from sift.manifest import init_schema, now_utc, open_db
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        for u, c in rows:
            conn.execute(
                "INSERT INTO manifest(url,tier,state,content_hash,first_seen_at) "
                "VALUES (?,?,?,?,?)", (u, "LIVING", "FRESH", c, now_utc()))

    def test_manifest_fallback_when_md_incomplete(self, tmp_path):
        allp = {"https://x/p1": _h("p1"), "https://x/p2": _h("p2"), "https://x/p3": _h("p3")}
        stored, count = integrity.compute_corpus_root(list(allp.items()))
        rd = paths.run_dir(tmp_path, "r")
        (rd / "md").mkdir(parents=True)
        for i, (u, c) in enumerate(list(allp.items())[:2]):   # md has only 2 of 3
            (rd / "md" / f"{i}.md").write_text(_md(u, c))
        (rd / "snapshot.json").write_text(json.dumps({
            "run_id": "r", "status": "published", "completed_at": "t",
            "versions": {"integrity": "v1"},
            "integrity": {"merkle_root": stored, "leaf_count": count, "scheme": SCHEME}}))
        self._manifest(tmp_path, allp.items())          # manifest has all 3
        env = prove.build_proof_for_run(rd, "https://x/p3",
                                        manifest_path=paths.manifest_path(tmp_path))
        assert env["included"] and env["leaf_source"] == "manifest"
        assert verify_proof.verify(env)[0]

    def test_refuses_when_no_source_reproduces_root(self, tmp_path):
        allp = {"https://x/p1": _h("p1"), "https://x/p2": _h("p2"), "https://x/p3": _h("p3")}
        stored, count = integrity.compute_corpus_root(list(allp.items()))
        rd = paths.run_dir(tmp_path, "r")
        (rd / "md").mkdir(parents=True)
        (rd / "md" / "0.md").write_text(_md("https://x/p1", _h("p1")))  # md: 1 of 3
        (rd / "snapshot.json").write_text(json.dumps({
            "run_id": "r", "status": "published", "completed_at": "t",
            "versions": {"integrity": "v1"},
            "integrity": {"merkle_root": stored, "leaf_count": count, "scheme": SCHEME}}))
        self._manifest(tmp_path, [("https://x/p1", _h("p1")),
                                  ("https://x/p2", _h("p2"))])          # manifest: 2 of 3
        with pytest.raises(prove.ProofError, match="no leaf source reproduces"):
            prove.build_proof_for_run(rd, "https://x/p1",
                                      manifest_path=paths.manifest_path(tmp_path))


# ---- adversarial: every tamper must fail ----------------------------------

class TestTamperRejected:
    @pytest.fixture
    def env(self, index):
        return prove.build_proof_for_run(_run_dir(index), "https://x/gamma")

    def test_tampered_leaf(self, env):
        env = dict(env)
        env["content_hash"] = "sha256:" + _h("forged")
        assert not verify_proof.verify(env)[0]

    def test_tampered_sibling(self, env):
        import copy
        env = copy.deepcopy(env)
        s = env["proof"][0]["sibling"]
        env["proof"][0]["sibling"] = ("0" if s[0] != "0" else "f") + s[1:]
        assert not verify_proof.verify(env)[0]

    def test_tampered_position(self, env):
        import copy
        env = copy.deepcopy(env)
        env["proof"][0]["position"] = "left" if env["proof"][0]["position"] == "right" else "right"
        assert not verify_proof.verify(env)[0]

    def test_tampered_root(self, env):
        env = dict(env)
        env["merkle_root"] = "f" * 64
        assert not verify_proof.verify(env)[0]

    def test_expect_root_binding(self, env):
        assert verify_proof.verify(env, expect_root=env["merkle_root"])[0]
        assert not verify_proof.verify(env, expect_root="0" * 64)[0]

    def test_wrong_integrity_version(self, env):
        env = dict(env)
        env["integrity_version"] = "v99"
        ok, notes = verify_proof.verify(env)
        assert not ok and "unsupported integrity_version" in notes[0]


# ---- security invariants --------------------------------------------------

class TestSecurityInvariants:
    def test_cve_2012_2459_present_in_primitive_but_unreachable(self):
        # The bitcoin-style construction (sort + odd-node duplication) DOES
        # collide on duplicating the MAX leaf — merkle_root(L) == merkle_root(
        # L + [max(L)]) — the textbook CVE-2012-2459. (Note: the spec swarm
        # claimed these differ; they do not for max-duplication. Documented here
        # honestly.) sift is NOT exposed because leaves reconstructed from
        # distinct md files are unique (md path is a function of url; same-url-
        # different-hash is refused; distinct-url-same-leaf is second-preimage-
        # hard), so a duplicated-leaf multiset can never be the committed corpus.
        leaves = sorted(integrity.leaf_hash(f"u{i}", _h(str(i))) for i in range(3))
        assert integrity.merkle_root(leaves) == integrity.merkle_root(leaves + [leaves[-1]])
        pairs = [(f"https://x/{i}", _h(str(i))) for i in range(6)]
        leafset = [integrity.leaf_hash(u, c) for u, c in pairs]
        assert len(leafset) == len(set(leafset))   # unique → CVE unreachable

    def test_domain_separation_holds(self):
        # leaf preimage contains ':'; internal preimage is 128 hex chars, no ':'.
        pairs = [(f"https://x/{i}", _h(str(i))) for i in range(8)]
        for u, c in pairs:
            assert ":" in f"{u}:{c}"
        a, b = integrity.leaf_hash("u", _h("1")), integrity.leaf_hash("v", _h("2"))
        internal_preimage = a + b
        assert len(internal_preimage) == 128 and ":" not in internal_preimage
        assert all(ch in "0123456789abcdef" for ch in internal_preimage)


# ---- standalone verifier (subprocess lockstep) ----------------------------

class TestStandaloneVerifier:
    def test_subprocess_accepts_and_rejects(self, index, tmp_path):
        env = prove.build_proof_for_run(_run_dir(index), "https://x/delta")
        good = tmp_path / "proof.json"
        good.write_text(json.dumps(env))
        r = subprocess.run([sys.executable, "-m", "sift.verify_proof", str(good)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

        bad = dict(env)
        bad["merkle_root"] = "f" * 64
        badf = tmp_path / "bad.json"
        badf.write_text(json.dumps(bad))
        r2 = subprocess.run([sys.executable, "-m", "sift.verify_proof", str(badf)],
                            capture_output=True, text=True)
        assert r2.returncode == 1            # invalid, not a crash

    def test_subprocess_expect_root(self, index, tmp_path):
        env = prove.build_proof_for_run(_run_dir(index), "https://x/delta")
        f = tmp_path / "p.json"
        f.write_text(json.dumps(env))
        r = subprocess.run([sys.executable, "-m", "sift.verify_proof", str(f),
                            "--expect-root", "0" * 64], capture_output=True, text=True)
        assert r.returncode == 1


# ---- MCP + CLI wiring -----------------------------------------------------

def _call(server, name, arguments):
    handler = (server.request_handlers.get("tools/call")
               or server.request_handlers[mcp_server.mcp_types.CallToolRequest])
    req = mcp_server.mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_server.mcp_types.CallToolRequestParams(name=name, arguments=arguments))
    return asyncio.run(handler(req)).root


class TestWiring:
    def test_mcp_prove(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "prove", {"url": "https://x/alpha"})
        assert not r.isError
        env = json.loads(r.content[0].text)
        assert env["included"] and verify_proof.verify(env)[0]

    def test_mcp_prove_requires_url(self, index):
        server = mcp_server.build_server(index)
        r = _call(server, "prove", {})
        assert r.isError

    def test_prove_registered_read_only_not_fanout(self):
        tools = {t.name: t for t in mcp_server._tool_descriptors()}
        assert "prove" in tools
        assert tools["prove"].annotations.readOnlyHint is True
        assert "prove" not in mcp_server._FANOUT_TOOLS

    def test_cli_prove_and_verify(self, index, tmp_path):
        from click.testing import CliRunner
        from sift.cli import main
        out = tmp_path / "proof.json"
        r = CliRunner().invoke(main, ["prove", "--root", str(index),
                                      "--url", "https://x/beta", "--out", str(out)])
        assert r.exit_code == 0, r.output
        r2 = CliRunner().invoke(main, ["verify-proof", str(out)])
        assert r2.exit_code == 0, r2.output
        assert '"ok": true' in r2.output
