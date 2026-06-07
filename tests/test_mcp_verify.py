"""MCP read_md verify=True path: pass on intact file, fail on tampered."""

import hashlib

import pytest

from sift import mcp_server, paths
from sift.normalize import normalize_for_hash


@pytest.fixture
def root_with_md(tmp_path):
    """Set up a current/ snapshot with one md file whose content_hash matches the body."""
    root = tmp_path
    run_id = "test-run"
    url = "https://www.ato.gov.au/x"
    body = (
        "# Test page\n\n## Section A\n\nThis is the body text we'll be re-hashing.\n"
        "Plenty of words to make the normalized output well-defined.\n"
    )
    content_hash = hashlib.sha256(normalize_for_hash(body).encode()).hexdigest()
    md = paths.md_path(root, run_id, url)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        f"---\nurl: {url}\ntier: LIVING\ncontent_hash: sha256:{content_hash}\n---\n{body}"
    )
    # current symlink
    cur = paths.current_symlink(root)
    cur.symlink_to(paths.run_dir(root, run_id).resolve(), target_is_directory=True)
    return root, md, content_hash, body


def _text(r):
    return r.content[0].text


class TestVerifyMode:
    def test_verify_passes_on_intact_file(self, root_with_md):
        root, md, content_hash, body = root_with_md
        cur, _ = mcp_server._resolve_root(root)
        rel = str(md.relative_to(paths.run_dir(root, "test-run")))
        r = mcp_server.tool_read_md(cur, rel, verify=True)
        assert not r.isError
        text = _text(r)
        assert "verify=ok" in text
        assert f"sha256:{content_hash[:16]}" in text

    def test_verify_fails_on_tampered_body(self, root_with_md):
        root, md, content_hash, body = root_with_md
        cur, _ = mcp_server._resolve_root(root)
        rel = str(md.relative_to(paths.run_dir(root, "test-run")))
        # Tamper with the body
        original = md.read_text()
        md.write_text(original.replace("body text", "TAMPERED text"))
        r = mcp_server.tool_read_md(cur, rel, verify=True)
        assert r.isError
        text = _text(r)
        assert "INTEGRITY FAILURE" in text
        assert "stored content_hash:" in text
        assert "recomputed content_hash:" in text

    def test_verify_fails_on_missing_frontmatter(self, root_with_md):
        root, md, _, _ = root_with_md
        cur, _ = mcp_server._resolve_root(root)
        rel = str(md.relative_to(paths.run_dir(root, "test-run")))
        md.write_text("no frontmatter just body")
        r = mcp_server.tool_read_md(cur, rel, verify=True)
        assert r.isError
        assert "verify failed: no frontmatter" in _text(r)

    def test_verify_false_skips_check(self, root_with_md):
        """verify=False (default) returns the file without the verify header."""
        root, md, _, _ = root_with_md
        cur, _ = mcp_server._resolve_root(root)
        rel = str(md.relative_to(paths.run_dir(root, "test-run")))
        original = md.read_text()
        md.write_text(original.replace("body text", "TAMPERED text"))
        r = mcp_server.tool_read_md(cur, rel, verify=False)
        # No error — verify=False doesn't check
        assert not r.isError
        assert "verify=ok" not in _text(r)

    def test_verify_in_schema(self):
        """The new verify arg appears in the read_md tool's schema."""
        descs = mcp_server._tool_descriptors()
        read_md = next(d for d in descs if d.name == "read_md")
        assert "verify" in read_md.inputSchema["properties"]
        assert read_md.inputSchema["properties"]["verify"]["type"] == "boolean"
        assert read_md.inputSchema["properties"]["verify"]["default"] is False
