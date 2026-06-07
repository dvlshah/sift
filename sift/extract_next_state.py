"""Content recovery from Next.js RSC streaming payloads.

When a Next.js App Router page is server-rendered, the page's React
tree is serialized as a sequence of ``self.__next_f.push([N, "<payload>"])``
calls in the HTML. The payloads use the Flight format (Next's RSC
streaming wire protocol):

    <chunk-id>:<rest>

where ``rest`` is either an "I[...]" import declaration or a JSON
React-tree fragment of the shape ``["$", "tag-or-ref", key, props]``.

This module walks those trees looking for element nodes whose tag is
a known content-bearing HTML tag (``pre``, ``code``, ``h1`` ... ``h6``,
``p``, ``blockquote``, ``li``), collects their text, and emits a
markdown chunk the caller appends to trafilatura's output.

What this gets right
====================

For sites whose RSC payload actually carries the page content inline
— **Docusaurus, older Mintlify, hand-rolled Next.js docs** — this
recovers code blocks and prose that trafilatura would otherwise drop
because the HTML around them looks like navigation chrome to its
content-vs-boilerplate scorer.

Honestly-noted limitations
==========================

For sites that defer the page body to a client-side fetch of
``/_next/data/<build-hash>/<path>.json`` (e.g. newer
``platform.claude.com``), the static HTML only contains the layout
shell + navigation; the RSC stream has navigation data but no body.
This extractor is a **no-op** in that case — the content isn't there
to recover. The follow-on fix for those sites is either browser
routing or a separate ``_next/data`` re-fetch step.

Reading agents looking at the output get a clearly-marked
"## Content recovered from RSC state" section so they don't confuse
the recovered material with the trafilatura main flow.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator, Optional, Union


# Tags we'll extract text content from. Excludes layout/control
# elements (div, span, button, nav, etc.) — those are noise. Keep this
# tight; widening it tends to bring in i18n catalog entries and other
# state cruft the model can't use.
_CONTENT_TAGS = frozenset({
    "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "blockquote", "li",
})

# Standalone code-block component names commonly used in MDX/docs
# frameworks. Their props typically carry the raw code string.
_CODE_COMPONENT_NAMES = frozenset({
    "CodeBlock", "Code", "CodeGroup", "RequestExample", "ResponseExample",
})

# Tags whose presence in props indicates "this is a heading" even when
# the wrapping React component name is something custom.
_HEADING_LEVEL_FROM_TAG = {
    "h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6,
}

_NEXT_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[(\d+),"((?:[^"\\]|\\.)*)"\]\)',
    re.DOTALL,
)
_FLIGHT_LINE_RE = re.compile(r"^([0-9a-f]+):(.*)$", re.DOTALL)


@dataclass(frozen=True)
class RecoveredChunk:
    """One snippet harvested from an RSC tree.

    ``kind`` distinguishes how the renderer should treat it:
      * ``"heading"``  — emit as ``## ...`` (level from extractor)
      * ``"code"``     — emit as fenced code block (with language)
      * ``"text"``     — emit as a paragraph
    """
    kind: str
    text: str
    language: Optional[str] = None
    level: int = 0


def _decode_flight_string(escaped: str) -> str:
    """The push payloads are JS-string literals embedded in the HTML.
    Decode by parsing them as a JSON string (round-trip through the
    same escaping rules)."""
    try:
        return json.loads('"' + escaped + '"')
    except json.JSONDecodeError:
        return ""


def _iter_flight_chunks(html: Union[bytes, str]) -> Iterator[str]:
    """Yield every `self.__next_f.push([N, "..."])` payload, decoded."""
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    for m in _NEXT_PUSH_RE.finditer(text):
        decoded = _decode_flight_string(m.group(2))
        if decoded:
            yield decoded


def _parse_flight_chunk(chunk: str) -> Iterator[object]:
    """A chunk contains one or more ``<id>:<json>`` lines. Yield each
    JSON value successfully parsed; skip "I[...]" import declarations
    and anything that doesn't parse cleanly. Tolerant of partial
    streams — recoverable lines come through, broken ones are
    skipped."""
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _FLIGHT_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group(2).strip()
        if not rest or rest.startswith(("I[", "HL[", "L:")):
            continue
        # JSON value follows. Use raw_decode so we don't choke on the
        # rest of the line.
        try:
            value, _end = json.JSONDecoder().raw_decode(rest)
        except (json.JSONDecodeError, ValueError):
            continue
        yield value


def _is_element(node: object) -> bool:
    """RSC elements are encoded as ``["$", "tag", key, props]``. Use
    that shape as the discriminator."""
    return (
        isinstance(node, list)
        and len(node) >= 4
        and node[0] == "$"
        and isinstance(node[1], str)
    )


def _extract_text(node: object) -> str:
    """Recursively flatten any RSC subtree to its concatenated text.
    Walks ``children`` keys + bare string nodes. Bounded depth to
    defend against pathological structures."""
    return _extract_text_bounded(node, depth=0)


def _extract_text_bounded(node: object, *, depth: int) -> str:
    if depth > 20:
        return ""
    if isinstance(node, str):
        # Skip RSC special markers like "$L17" / "$undefined".
        if node.startswith("$") and len(node) < 12:
            return ""
        return node
    if isinstance(node, list):
        if _is_element(node):
            props = node[3] if len(node) > 3 else None
            if isinstance(props, dict):
                return _extract_text_bounded(
                    props.get("children"), depth=depth + 1,
                )
            return ""
        return "".join(
            _extract_text_bounded(child, depth=depth + 1) for child in node
        )
    if isinstance(node, dict):
        # Some nodes are bare props dicts (RSC has a few of these).
        return _extract_text_bounded(node.get("children"), depth=depth + 1)
    return ""


def _walk_tree(node: object, *, depth: int = 0) -> Iterator[RecoveredChunk]:
    """DFS over the RSC tree, yielding recovered content chunks for
    every element whose tag we know carries content."""
    if depth > 30:
        return
    if not isinstance(node, (list, dict)):
        return
    if isinstance(node, list):
        if _is_element(node):
            tag = node[1]
            props = node[3] if len(node) > 3 else {}
            props = props if isinstance(props, dict) else {}
            if tag in _HEADING_LEVEL_FROM_TAG:
                text = _extract_text(props.get("children")).strip()
                if text:
                    yield RecoveredChunk(
                        kind="heading", text=text,
                        level=_HEADING_LEVEL_FROM_TAG[tag],
                    )
            elif tag in ("pre", "code"):
                text = _extract_text(props.get("children")).strip()
                if text:
                    language = _detect_language_from_props(props)
                    yield RecoveredChunk(
                        kind="code", text=text, language=language,
                    )
            elif tag in _CONTENT_TAGS:
                text = _extract_text(props.get("children")).strip()
                if len(text) >= 24:        # threshold so we skip "menu" cruft
                    yield RecoveredChunk(kind="text", text=text)
            elif tag in _CODE_COMPONENT_NAMES:
                code = props.get("code")
                if isinstance(code, str) and code.strip():
                    yield RecoveredChunk(
                        kind="code", text=code.strip(),
                        language=props.get("language") or props.get("lang"),
                    )
            # Always recurse into children regardless — code blocks
            # often live deep inside layout components.
            children = props.get("children")
            if children is not None:
                yield from _walk_tree(children, depth=depth + 1)
        else:
            for child in node:
                yield from _walk_tree(child, depth=depth + 1)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_tree(value, depth=depth + 1)


def _detect_language_from_props(props: dict) -> Optional[str]:
    """Some RSC code elements carry a class like ``language-py`` on
    their props; others use a ``language`` / ``lang`` prop. Try both."""
    for key in ("language", "lang"):
        val = props.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    class_attr = props.get("className") or props.get("class") or ""
    if isinstance(class_attr, str):
        m = re.search(r"language-(\w+)", class_attr)
        if m:
            return m.group(1).lower()
    return None


def recover_from_next_state(html: Union[bytes, str]) -> list[RecoveredChunk]:
    """Extract content chunks from every Next.js RSC push in the HTML.

    Dedup is by ``(kind, first-60-chars-of-text)`` so repeated nav
    fragments in multiple payloads don't multiply.
    """
    out: list[RecoveredChunk] = []
    seen: set[tuple[str, str]] = set()
    for chunk_text in _iter_flight_chunks(html):
        for parsed in _parse_flight_chunk(chunk_text):
            for rec in _walk_tree(parsed):
                key = (rec.kind, rec.text[:60])
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


def render_recovered_markdown(chunks: list[RecoveredChunk]) -> str:
    """Render a list of RecoveredChunks as a markdown fragment, with a
    clearly-labelled section heading so a reading agent can tell this
    is auxiliary RSC recovery, not the main content."""
    if not chunks:
        return ""
    lines: list[str] = ["", "## Content recovered from RSC state", ""]
    for c in chunks:
        if c.kind == "heading":
            prefix = "#" * max(2, c.level + 1)   # cap at h2+ to stay under main title
            lines.append(f"{prefix} {c.text}")
            lines.append("")
        elif c.kind == "code":
            lines.append(f"```{c.language or ''}")
            lines.append(c.text)
            lines.append("```")
            lines.append("")
        else:
            lines.append(c.text)
            lines.append("")
    return "\n".join(lines)


def merge_next_state(md: str, html: Union[bytes, str]) -> tuple[str, int]:
    """Append RSC-recovered chunks that aren't already in ``md`` to the
    end of the markdown.

    Returns ``(merged_markdown, n_appended)``. The presence check is
    the same first-30-chars-substring test the code-block recovery
    uses; same false-positive vs false-negative tradeoff."""
    chunks = recover_from_next_state(html)
    if not chunks:
        return md, 0
    novel: list[RecoveredChunk] = []
    for c in chunks:
        signature = c.text.strip()[:30]
        if signature and signature not in md:
            novel.append(c)
    if not novel:
        return md, 0
    return md + "\n" + render_recovered_markdown(novel), len(novel)
