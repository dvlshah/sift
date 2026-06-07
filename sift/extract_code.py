"""Code-block recovery for HTML-to-markdown extraction.

Why this module exists
======================

trafilatura is excellent at picking out the prose body of an article-
shaped page, but for *docs* sites it has a specific blind spot: tabbed
code blocks. Mintlify, Docusaurus, Nextra, and a handful of other
documentation platforms render tabbed code as multiple sibling
``<pre><code>`` elements where only the active tab is visible. The
inactive panels carry the ``aria-hidden="true"`` / ``hidden`` / a
``role="tabpanel" data-state="inactive"`` shape; trafilatura's
content-vs-boilerplate scorer reads "hidden" as "navigation" and drops
them. Result: the markdown shows the tab labels but the code areas
come through empty.

That's a critical failure for the agent-first wedge — the coding-agent
corpora (Stripe, PostHog, Anthropic, OpenAI docs) live or die on
having usable code samples in the indexed text. An agent that grep_'s
for ``Idempotency-Key`` and finds the label but not the JavaScript
example can't do its job.

The fix here is deliberately surgical:

  1. Walk every ``<pre><code>`` node in the original HTML — including
     the hidden tab panels — and dedupe by code signature.
  2. Compare each block against the trafilatura-produced markdown.
     Blocks whose first 30 trimmed characters don't appear in the
     markdown are considered "missing."
  3. Append the missing blocks to the markdown under a clearly-labelled
     "## Code samples (recovered)" section. That keeps the original
     trafilatura flow intact and makes it obvious to a reading agent
     that these are auxiliary extractions, not part of the prose.

The merge is deterministic (same HTML in → same blocks appended) so
``EXTRACTOR_VERSION`` bumps cleanly. False positives (a block already
in the markdown gets duplicated under "Code samples") cost a little
context but are recoverable; false negatives (a block silently
dropped) are not, so the comparison is intentionally permissive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

import lxml.html
import lxml.etree


@dataclass(frozen=True)
class CodeBlock:
    """One ``<pre><code>`` block harvested from the HTML.

    ``language`` is parsed from common syntax-highlighter CSS class
    conventions (Prism's ``language-X``, highlight.js's ``hljs X`` /
    ``language-X``, Shiki, GitHub's ``highlight-source-X``). ``None``
    when no hint is present — the caller emits an unlabelled fence."""
    language: Optional[str]
    code: str


# CSS class prefixes that pin a language onto a <pre> / <code>. Order
# doesn't matter — first hit wins. Adding a new prefix is a one-line
# change here.
_LANGUAGE_CLASS_PREFIXES = (
    "language-",            # Prism, Mintlify, Docusaurus
    "lang-",                # Older Prism, some hand-rolled
    "highlight-source-",    # GitHub
    "highlight-text-",      # GitHub (for prose text)
)

# Normalise the common long-form names to their conventional short
# tokens used in markdown fences. Keeps grep_corpus matches consistent.
_LANGUAGE_ALIASES = {
    "javascript": "js",
    "typescript": "ts",
    "python": "py",
    "shell": "bash",
    "sh": "bash",
    "yml": "yaml",
}


def _detect_language(class_attr: str) -> Optional[str]:
    """Return the language token (lowercased + aliased) from a class
    attribute, or None if no recognised prefix is found."""
    if not class_attr:
        return None
    for token in class_attr.split():
        for prefix in _LANGUAGE_CLASS_PREFIXES:
            if token.startswith(prefix):
                lang = token[len(prefix):].strip().lower()
                if lang:
                    return _LANGUAGE_ALIASES.get(lang, lang)
    return None


def _parse_html(html: Union[bytes, str]) -> Optional[lxml.html.HtmlElement]:
    """Parse permissively. Returns ``None`` when the body isn't HTML or
    is malformed beyond lxml's tolerance — at that point we just leave
    trafilatura's output as-is."""
    try:
        return lxml.html.fromstring(html)
    except (lxml.etree.ParserError, lxml.etree.XMLSyntaxError, ValueError):
        return None


def extract_code_blocks(html: Union[bytes, str]) -> list[CodeBlock]:
    """Walk every ``<pre><code>`` node in the HTML, including hidden tab
    panels, and return them as CodeBlocks.

    Dedup by the first 60 chars of the trimmed code body — a single
    page may have the same block in multiple tab arrangements
    (visible + repeated mobile layout); we only keep one copy.

    Very short bodies (<4 trimmed chars) are skipped — those are
    typically inline ``<code>`` runs that happened to live inside a
    ``<pre>`` for layout reasons, not real samples.
    """
    tree = _parse_html(html)
    if tree is None:
        return []

    blocks: list[CodeBlock] = []
    seen_signatures: set[str] = set()
    for code in tree.iter("code"):
        parent = code.getparent()
        if parent is None or parent.tag != "pre":
            # We only want block-level code, not inline `code`.
            continue
        text = (code.text_content() or "").rstrip()
        trimmed = text.strip()
        if len(trimmed) < 4:
            continue
        signature = trimmed[:60]
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        lang = (_detect_language(code.get("class") or "")
                or _detect_language(parent.get("class") or ""))
        blocks.append(CodeBlock(language=lang, code=text))
    return blocks


# How many leading trimmed chars from a block we look for in the
# trafilatura markdown to decide "this is already there." 30 is wide
# enough to match through markdown's escaping noise on typical code,
# narrow enough to not collide with prose.
_MD_PRESENCE_PROBE = 30


def merge_missing_code_blocks(
    md: str, html: Union[bytes, str],
) -> tuple[str, int]:
    """Append code blocks present in the HTML but missing from the
    markdown to the end of the document.

    Returns ``(merged_markdown, n_appended)``. When the markdown
    already contains every code block (typical for prose-heavy pages
    on Python docs / RFC editor), this is a no-op and returns
    ``(md, 0)``.

    Append shape: a single ``## Code samples (recovered)`` heading
    followed by fenced blocks (with language hint when known). Same
    heading + ordering rules every call so the output is byte-stable
    against the same input — extractor-version determinism holds.
    """
    blocks = extract_code_blocks(html)
    if not blocks:
        return md, 0

    missing: list[CodeBlock] = []
    for blk in blocks:
        signature = blk.code.strip()[:_MD_PRESENCE_PROBE]
        if not signature:
            continue
        if signature not in md:
            missing.append(blk)

    if not missing:
        return md, 0

    chunks: list[str] = ["", "## Code samples (recovered)", ""]
    for blk in missing:
        fence_lang = blk.language or ""
        chunks.append(f"```{fence_lang}")
        chunks.append(blk.code.rstrip())
        chunks.append("```")
        chunks.append("")
    return md + "\n".join(chunks), len(missing)
