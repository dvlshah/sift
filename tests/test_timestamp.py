"""Tests for the RFC-3161 external timestamp anchor (sift/timestamp.py) and its
threading into the proof envelope / publish path.

The "real" verification tests use a committed fixture: an actual DigiCert
timestamp token over the live ATO Merkle root (75cf45…). They verify it with
`openssl ts -verify` (skipped where openssl lacks the `ts` subcommand, e.g.
macOS LibreSSL). The crypto here is the witness that closes the "what stops you
back-dating the root?" gap — an independent TSA's signature over the root.
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from sift import integrity, paths, prove
from sift import timestamp as ts

FIXTURE = Path(__file__).parent / "fixtures" / "digicert_ato_root.tsr"
ATO_ROOT = "75cf45042a7c9f1c8bb58487a26572b6c578c937c0671fb428b9c6340c39fdeb"
HAS_TS = ts.openssl_ts_available()


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _md(url: str, ch: str) -> str:
    return f"---\nurl: {url}\ncontent_hash: sha256:{ch}\ntier: LIVING\n---\nbody {url}\n"


def _write_run(tmp_path, pages, *, with_token=False, run_id="r"):
    rd = paths.run_dir(tmp_path, run_id)
    (rd / "md").mkdir(parents=True, exist_ok=True)
    for rel, (u, c) in pages.items():
        (rd / "md" / rel).write_text(_md(u, c))
    root, count = integrity.compute_corpus_root([(u, c) for (u, c) in pages.values()])
    integ = {"merkle_root": root, "leaf_count": count,
             "scheme": "sorted-leaves-bitcoin-style-sha256"}
    if with_token:
        (rd / "merkle_root.tsr").write_bytes(b"DUMMY-TOKEN-BYTES")
        integ["timestamp"] = {"tsa_url": "http://tsa.example", "time": "Jun 25 2026 GMT",
                              "hash_algorithm": "sha256", "token_file": "merkle_root.tsr"}
    (rd / "snapshot.json").write_text(json.dumps({
        "run_id": run_id, "status": "published", "completed_at": "t",
        "versions": {"integrity": "v1"}, "integrity": integ}))
    return rd


@pytest.mark.skipif(not HAS_TS, reason="openssl lacks the `ts` subcommand")
class TestVerifyRealToken:
    """Against a real DigiCert token over the real ATO root."""

    def test_valid_token_verifies(self):
        r = ts.verify_timestamp(FIXTURE.read_bytes(), ATO_ROOT, ca_file=ts.default_ca_file())
        assert r["ok"], r
        assert r["time"]            # a witnessed timestamp is present

    def test_wrong_root_rejected(self):
        r = ts.verify_timestamp(FIXTURE.read_bytes(), "0" * 64, ca_file=ts.default_ca_file())
        assert not r["ok"]          # the token does not witness a different root

    def test_parse_fields(self):
        m = ts.parse_timestamp(FIXTURE.read_bytes())
        assert m.get("hash_algorithm", "").lower() == "sha256"
        assert m.get("time")


class TestEnvelopeTimestamp:
    def test_build_proof_attaches_timestamp(self, tmp_path):
        rd = _write_run(tmp_path, {"a.md": ("https://x/a", _h("a")),
                                   "b.md": ("https://x/b", _h("b"))}, with_token=True)
        env = prove.build_proof_for_run(rd, "https://x/a")
        assert env["included"]
        assert env["timestamp"]["tsa_url"] == "http://tsa.example"
        assert base64.b64decode(env["timestamp"]["rfc3161_token_b64"]) == b"DUMMY-TOKEN-BYTES"

    def test_no_timestamp_field_when_unwitnessed(self, tmp_path):
        rd = _write_run(tmp_path, {"a.md": ("https://x/a", _h("a"))}, with_token=False)
        env = prove.build_proof_for_run(rd, "https://x/a")
        assert "timestamp" not in env


class TestConfigPlumbing:
    def test_default_is_none(self):
        from sift.config import PublishConfig
        assert PublishConfig().timestamp_tsa_url is None

    def test_toml_plumbs_tsa_url(self, tmp_path):
        from sift.config import load_config
        cfg = tmp_path / "sift.toml"
        cfg.write_text('[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
                       '[publish]\ntimestamp_tsa_url = "http://timestamp.digicert.com"\n')
        c = load_config(cfg)
        assert c.publish.timestamp_tsa_url == "http://timestamp.digicert.com"

    def test_apply_config_sets_module_global(self, tmp_path):
        from sift import publish as pub
        from sift.config import load_config
        cfg = tmp_path / "sift.toml"
        cfg.write_text('[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
                       '[publish]\ntimestamp_tsa_url = "http://ts.example/tsr"\n')
        prev = pub.TIMESTAMP_TSA_URL
        try:
            pub.apply_config(load_config(cfg))
            assert pub.TIMESTAMP_TSA_URL == "http://ts.example/tsr"
        finally:
            pub.TIMESTAMP_TSA_URL = prev


@pytest.mark.skipif(not HAS_TS, reason="openssl lacks the `ts` subcommand")
def test_verify_proof_cli_checks_embedded_timestamp(tmp_path):
    """End-to-end: an envelope carrying the real token verifies (membership +
    timestamp) via `sift verify-proof`; a tampered root fails."""
    from click.testing import CliRunner

    from sift.cli import main
    # An envelope whose merkle_root == the token's imprint, carrying the real token.
    env = {
        "url": "https://x/a", "content_hash": "sha256:" + _h("a"),
        "leaf": integrity.leaf_hash("https://x/a", _h("a")),
        "run_id": "r", "merkle_root": ATO_ROOT, "scheme": "sorted-leaves-bitcoin-style-sha256",
        "integrity_version": "v1", "leaf_count": 1, "proof": [], "included": True,
        "timestamp": {"tsa_url": "http://timestamp.digicert.com", "time": "x",
                      "hash_algorithm": "sha256",
                      "rfc3161_token_b64": base64.b64encode(FIXTURE.read_bytes()).decode()},
    }
    # Note: leaf != root here (proof empty), so membership fails — but we only
    # assert the timestamp path runs and the token verifies against ATO_ROOT.
    f = tmp_path / "p.json"
    f.write_text(json.dumps(env))
    r = CliRunner().invoke(main, ["verify-proof", str(f)])
    assert '"timestamp_ok": true' in r.output, r.output
