"""Tests for ``sift.extract_code`` — code-block recovery for docs sites.

This is the wedge fix called out in the agent harness session: tabbed
code blocks on Mintlify / Docusaurus / Nextra sites were silently
dropped by trafilatura, leaving the markdown showing tab labels but
empty code areas. The recovery pass walks the raw HTML for every
``<pre><code>`` block (including hidden tab panels) and appends what
trafilatura missed.
"""
from __future__ import annotations

import pytest

from sift.extract_code import (
    CodeBlock,
    extract_code_blocks,
    merge_missing_code_blocks,
)


# ---- _detect_language via extract_code_blocks ----------------------------

class TestLanguageDetection:
    def test_prism_language_class(self):
        html = """
        <pre><code class="language-python">x = 1</code></pre>
        """
        blocks = extract_code_blocks(html)
        assert len(blocks) == 1
        assert blocks[0].language == "py"
        assert blocks[0].code.strip() == "x = 1"

    def test_old_prism_lang_class(self):
        html = '<pre><code class="lang-javascript">a = b</code></pre>'
        assert extract_code_blocks(html)[0].language == "js"

    def test_github_highlight_class(self):
        html = '<pre><code class="highlight-source-shell">echo hi</code></pre>'
        assert extract_code_blocks(html)[0].language == "bash"

    def test_no_class_yields_none(self):
        html = "<pre><code>raw text</code></pre>"
        assert extract_code_blocks(html)[0].language is None

    def test_language_on_pre_used_when_code_unset(self):
        html = '<pre class="language-yaml"><code>a: b</code></pre>'
        assert extract_code_blocks(html)[0].language == "yaml"

    def test_short_alias_passthrough(self):
        # ``ts`` is already short — no alias rewriting needed.
        html = '<pre><code class="language-ts">const x = 1</code></pre>'
        assert extract_code_blocks(html)[0].language == "ts"


# ---- extract_code_blocks walks tab panels --------------------------------

class TestCodeBlockWalker:
    def test_tabbed_blocks_all_returned(self):
        # Mintlify-shaped: tablist + hidden panels. trafilatura would
        # typically drop the hidden ones.
        html = """
        <div role="tablist">
            <button role="tab" data-state="active">JavaScript</button>
            <button role="tab" data-state="inactive">Python</button>
        </div>
        <div role="tabpanel" data-state="active">
            <pre><code class="language-js">const x = 1;</code></pre>
        </div>
        <div role="tabpanel" data-state="inactive" hidden>
            <pre><code class="language-py">x = 1</code></pre>
        </div>
        """
        blocks = extract_code_blocks(html)
        assert {b.language for b in blocks} == {"js", "py"}

    def test_dedup_by_signature(self):
        # Same code repeats (e.g. mobile vs desktop layouts); only one
        # copy should survive.
        html = """
        <pre><code class="language-py">print('hello world')</code></pre>
        <pre><code class="language-py">print('hello world')</code></pre>
        """
        blocks = extract_code_blocks(html)
        assert len(blocks) == 1

    def test_inline_code_ignored(self):
        # <code> not under <pre> is inline — never returned as a block.
        html = "<p>Use <code>str()</code> on the value.</p>"
        assert extract_code_blocks(html) == []

    def test_short_blocks_ignored(self):
        # `< 4` trimmed chars is too small to be a real sample.
        html = "<pre><code>x</code></pre>"
        assert extract_code_blocks(html) == []

    def test_malformed_html_returns_empty(self):
        """Defensive: tolerant of arbitrary input. lxml's fromstring
        raises on completely empty input; we treat that as 'nothing to
        recover'."""
        assert extract_code_blocks("") == []


# ---- merge_missing_code_blocks integrates with markdown -----------------

class TestMergeMissingCodeBlocks:
    def test_no_op_when_md_already_has_all_blocks(self):
        html = "<pre><code>x = 1</code></pre>"
        md = "Some prose.\n\n```\nx = 1\n```\n"
        out, n = merge_missing_code_blocks(md, html)
        assert n == 0
        assert out == md

    def test_appends_missing_block(self):
        html = '<pre><code class="language-py">x = 12345_signature</code></pre>'
        md = "Prose only — trafilatura missed the code.\n"
        out, n = merge_missing_code_blocks(md, html)
        assert n == 1
        assert "## Code samples (recovered)" in out
        assert "```py" in out
        assert "12345_signature" in out

    def test_partial_recovery_only_appends_missing(self):
        # Two blocks; trafilatura got the first, missed the second.
        html = """
        <pre><code class="language-js">const visible = 'first';</code></pre>
        <pre><code class="language-py">missing = 'second_distinct'</code></pre>
        """
        md = "```\nconst visible = 'first';\n```\nProse follows.\n"
        out, n = merge_missing_code_blocks(md, html)
        assert n == 1
        assert "second_distinct" in out
        # The visible one is NOT duplicated — already in md.
        assert out.count("const visible = 'first';") == 1

    def test_deterministic_output(self):
        html = """
        <pre><code class="language-py">a = 1</code></pre>
        <pre><code class="language-js">b = 2</code></pre>
        """
        md = "Prose.\n"
        out_a, _ = merge_missing_code_blocks(md, html)
        out_b, _ = merge_missing_code_blocks(md, html)
        assert out_a == out_b

    def test_no_html_blocks_means_no_op(self):
        html = "<p>Just prose.</p>"
        md = "Prose."
        out, n = merge_missing_code_blocks(md, html)
        assert n == 0
        assert out == md


# ---- End-to-end through extract_markdown ----------------------------------

class TestIntegration:
    def test_extract_markdown_recovers_tabbed_code(self):
        """The load-bearing integration test: a Mintlify-shaped page
        with a hidden tab panel should NOT lose the code. This is the
        failure the agent harness session reported on posthog-docs."""
        from sift.extract import extract_markdown

        html = b"""<!DOCTYPE html>
        <html><head><title>Docs page</title></head>
        <body>
        <main>
          <h1>Set up your project</h1>
          <p>Install the SDK using your preferred package manager.</p>
          <div role="tablist">
            <button role="tab" data-state="active">npm</button>
            <button role="tab" data-state="inactive">pip</button>
          </div>
          <div role="tabpanel" data-state="active">
            <pre><code class="language-bash">npm install posthog-js_demo</code></pre>
          </div>
          <div role="tabpanel" data-state="inactive" hidden>
            <pre><code class="language-bash">pip install posthog_demo_python</code></pre>
          </div>
        </main>
        </body></html>
        """
        md, title = extract_markdown(html, "https://example.test/docs/setup")
        assert md is not None
        # Both code samples — visible AND hidden tab — must reach the
        # markdown so an agent grepping for the install command finds
        # both languages.
        assert "posthog-js_demo" in md
        assert "posthog_demo_python" in md
