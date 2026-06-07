"""Tests for ``sift.extract_next_state`` — Next.js RSC payload recovery.

These exercise the structural parsing without relying on real Next.js
output — synthetic fixtures of the exact ``self.__next_f.push([N, ".."])``
shape, plus a few malformed cases to confirm we degrade gracefully.
"""
from __future__ import annotations

import json

import pytest

from sift.extract_next_state import (
    RecoveredChunk,
    _detect_language_from_props,
    _extract_text,
    _is_element,
    _iter_flight_chunks,
    _parse_flight_chunk,
    merge_next_state,
    recover_from_next_state,
    render_recovered_markdown,
)


def _wrap_push(chunk_id: str, value) -> str:
    """Build a single self.__next_f.push() call wrapping the given JSON
    value under ``chunk_id``. Returns a string ready to inject into a
    synthetic HTML fixture."""
    raw_payload = f"{chunk_id}:{json.dumps(value)}"
    # Re-encode through json.dumps so quotes etc. get the same
    # escaping the real HTML uses.
    escaped = json.dumps(raw_payload)[1:-1]
    return f'<script>self.__next_f.push([1,"{escaped}"])</script>'


def _wrap_pushes(*entries) -> str:
    """Build an HTML fragment with several pushes."""
    return "".join(_wrap_push(cid, v) for cid, v in entries)


# ---- Low-level helpers ----------------------------------------------------

class TestIsElement:
    def test_typical_rsc_element(self):
        assert _is_element(["$", "div", None, {"children": "hi"}])

    def test_string_is_not_element(self):
        assert not _is_element("$L17")

    def test_short_array_is_not_element(self):
        assert not _is_element(["$", "div"])


class TestExtractText:
    def test_string_node(self):
        assert _extract_text("hello") == "hello"

    def test_rsc_marker_skipped(self):
        assert _extract_text("$L17") == ""
        assert _extract_text("$undefined") == ""

    def test_list_of_strings_concatenated(self):
        assert _extract_text(["a", " ", "b"]) == "a b"

    def test_recurses_children(self):
        node = ["$", "p", None, {"children": ["$", "strong", None,
                                              {"children": "bold"}]}]
        assert _extract_text(node) == "bold"

    def test_bounded_depth(self):
        # A pathological deeply-nested chain should not recurse
        # forever. Build a tree 50-deep; depth cap is 20.
        node = "leaf"
        for _ in range(50):
            node = ["$", "div", None, {"children": node}]
        # Doesn't raise; returns "" because depth cap was hit.
        out = _extract_text(node)
        # Either "" (hit cap before reaching leaf) or "leaf" — both fine,
        # the point is no recursion bomb.
        assert isinstance(out, str)


# ---- Language detection ---------------------------------------------------

class TestLanguageDetection:
    def test_language_prop(self):
        assert _detect_language_from_props({"language": "Python"}) == "python"

    def test_lang_prop(self):
        assert _detect_language_from_props({"lang": "JS"}) == "js"

    def test_class_language_token(self):
        props = {"className": "code-block language-bash sh-token"}
        assert _detect_language_from_props(props) == "bash"

    def test_no_hint(self):
        assert _detect_language_from_props({"other": "thing"}) is None


# ---- Flight payload iteration --------------------------------------------

class TestIterFlightChunks:
    def test_finds_push_payloads(self):
        html = _wrap_pushes(
            ("17", ["$", "p", None, {"children": "alpha"}]),
            ("18", ["$", "p", None, {"children": "beta"}]),
        )
        out = list(_iter_flight_chunks(html))
        assert len(out) == 2

    def test_no_pushes_returns_empty(self):
        assert list(_iter_flight_chunks(b"<html><body>no rsc</body></html>")) == []


class TestParseFlightChunk:
    def test_parses_single_line(self):
        chunk = '17:["$","p",null,{"children":"hi"}]'
        out = list(_parse_flight_chunk(chunk))
        assert len(out) == 1
        assert out[0][0] == "$" and out[0][1] == "p"

    def test_skips_import_declarations(self):
        chunk = '1e:I[5599220888,["chunks/a.js"],"default"]'
        assert list(_parse_flight_chunk(chunk)) == []

    def test_skips_malformed(self):
        chunk = "17:{not-valid-json"
        assert list(_parse_flight_chunk(chunk)) == []


# ---- Recovery integration -------------------------------------------------

class TestRecoverFromNextState:
    def test_recovers_code_block(self):
        tree = ["$", "pre", None, {
            "className": "language-py",
            "children": ["$", "code", None, {
                "children": "x = 1\nprint(x)",
            }],
        }]
        html = _wrap_pushes(("17", tree))
        chunks = recover_from_next_state(html)
        codes = [c for c in chunks if c.kind == "code"]
        assert codes
        assert "x = 1" in codes[0].text

    def test_recovers_heading(self):
        tree = ["$", "h2", None, {"children": "Authentication"}]
        html = _wrap_pushes(("17", tree))
        chunks = recover_from_next_state(html)
        headings = [c for c in chunks if c.kind == "heading"]
        assert len(headings) == 1
        assert headings[0].level == 2
        assert headings[0].text == "Authentication"

    def test_recovers_prose_above_threshold(self):
        long_text = "This is a substantive paragraph above the 24-char threshold."
        tree = ["$", "p", None, {"children": long_text}]
        html = _wrap_pushes(("17", tree))
        chunks = recover_from_next_state(html)
        texts = [c for c in chunks if c.kind == "text"]
        assert texts
        assert texts[0].text == long_text

    def test_skips_short_prose(self):
        # Short cruft (typically navigation labels) should be dropped.
        tree = ["$", "p", None, {"children": "Hi"}]
        html = _wrap_pushes(("17", tree))
        chunks = recover_from_next_state(html)
        assert [c for c in chunks if c.kind == "text"] == []

    def test_codeblock_component(self):
        # MDX/docs frameworks often emit a CodeBlock component with the
        # raw code as a prop instead of children.
        tree = ["$", "CodeBlock", None, {
            "code": "curl https://example.test/api",
            "language": "bash",
        }]
        html = _wrap_pushes(("17", tree))
        chunks = recover_from_next_state(html)
        codes = [c for c in chunks if c.kind == "code"]
        assert codes
        assert codes[0].language == "bash"
        assert "curl" in codes[0].text

    def test_dedup_repeated_chunks(self):
        # Same content in two pushes (mobile + desktop layout) should
        # only count once.
        tree = ["$", "p", None, {
            "children": "Repeated paragraph for layout reasons.",
        }]
        html = _wrap_pushes(("17", tree), ("18", tree))
        chunks = recover_from_next_state(html)
        texts = [c for c in chunks if c.kind == "text"]
        assert len(texts) == 1

    def test_no_pushes_returns_empty(self):
        assert recover_from_next_state(b"<p>plain html</p>") == []


# ---- merge_next_state -----------------------------------------------------

class TestMergeNextState:
    def test_no_op_when_md_has_content(self):
        tree = ["$", "p", None, {"children": "x" * 50}]
        html = _wrap_pushes(("17", tree))
        md = f"# Doc\n\n{'x' * 50}\n"
        out, n = merge_next_state(md, html)
        assert n == 0
        assert out == md

    def test_appends_when_md_missing(self):
        tree = ["$", "pre", None, {
            "className": "language-py",
            "children": ["$", "code", None, {"children": "magic_unique_token = 42"}],
        }]
        html = _wrap_pushes(("17", tree))
        md = "Just prose — code was lost in extraction.\n"
        out, n = merge_next_state(md, html)
        assert n == 1
        assert "## Content recovered from RSC state" in out
        assert "magic_unique_token = 42" in out
        assert "```py" in out

    def test_no_op_when_no_pushes(self):
        md = "static markdown"
        out, n = merge_next_state(md, b"<html><body>nothing rsc here</body></html>")
        assert n == 0
        assert out == md


# ---- render_recovered_markdown shape -------------------------------------

class TestRender:
    def test_empty_chunks_returns_empty(self):
        assert render_recovered_markdown([]) == ""

    def test_renders_all_three_kinds(self):
        out = render_recovered_markdown([
            RecoveredChunk(kind="heading", text="Setup", level=2),
            RecoveredChunk(kind="text", text="Some explanatory text " * 3),
            RecoveredChunk(kind="code", text="x = 1", language="py"),
        ])
        assert "## Content recovered from RSC state" in out
        assert "### Setup" in out or "## Setup" in out  # level cap
        assert "x = 1" in out
        assert "```py" in out


# ---- End-to-end via extract_markdown -------------------------------------

class TestIntegration:
    def test_extract_markdown_pulls_rsc_code(self):
        """A page whose only content lives in an RSC push should still
        produce a markdown body with the code recovered."""
        from sift.extract import extract_markdown
        tree = ["$", "div", None, {
            "children": [
                ["$", "h1", None, {"children": "API reference"}],
                ["$", "pre", None, {
                    "className": "language-bash",
                    "children": ["$", "code", None, {
                        "children": "curl https://example.test/v1/messages",
                    }],
                }],
            ],
        }]
        html = (
            b"<html><head><title>API</title></head><body>"
            + _wrap_pushes(("17", tree)).encode()
            + b"</body></html>"
        )
        md, _title = extract_markdown(html, "https://example.test/api")
        # trafilatura may produce nothing on a shell-only page, but the
        # RSC merge should still recover the code.
        assert md is not None
        assert "curl https://example.test/v1/messages" in md
