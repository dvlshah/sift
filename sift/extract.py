"""Phase 3: HTML -> markdown via trafilatura, with deterministic hashing.

Reads fetch.log + raw blobs, writes extract.log + staged md/<path>.md + meta.json.

Per-URL outcome (extract.log entry):
    {"url": ..., "raw_hash": ..., "content_hash": ..., "title": ..., "n_chars": ...,
     "extractor_version": "trafilatura-1.12.0", "normalizer_version": "v1",
     "ok": true, "reason": null}

For 304 entries the extract phase is a no-op pass-through (no content changed).
For new content where the raw_hash matches the manifest's stored raw_hash, we
also short-circuit (raw didn't change, so the markdown can't have either).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import trafilatura
try:
    import pypdf
    _PYPDF_VERSION = pypdf.__version__
except ImportError:  # pragma: no cover - pypdf is in deps now
    pypdf = None  # type: ignore[assignment]
    _PYPDF_VERSION = "missing"

try:
    import pdfplumber
    _PDFPLUMBER_VERSION = pdfplumber.__version__
except ImportError:  # pragma: no cover - pdfplumber is in deps now
    pdfplumber = None  # type: ignore[assignment]
    _PDFPLUMBER_VERSION = "missing"

from .extract_strategy import (
    ExtractInput,
    PrimaryStrategy,
    run_html_enrichers,
    select_primary,
)

from . import paths
from .classify import (
    audience as audience_for,
    classify_tier,
    fy_years as fy_years_for,
    parent_guide,
)
from ._io import atomic_write_text, sha256_hex
from .fetch import FetchResult, read_raw_blob
from .manifest import get_row
from .normalize import normalize_for_hash, normalizer_version
from .quality import admit_content
from .sites import current_profile

# Per-extractor versions. Each markdown row records the version of whatever
# extractor produced it, so a bump to one doesn't invalidate the other.
#   cfg3: deduplicate=False — trafilatura's dedupe uses a process-level LRU
#         cache that makes output order-dependent (different result per
#         re-extraction in isolation vs. mid-crawl). We need true
#         determinism for the hash gate.
#   cfg4-code: post-process trafilatura output through ``merge_missing_code_blocks``
#         so hidden tab panels on Mintlify / Docusaurus / Nextra-style
#         docs sites (Stripe, PostHog, Anthropic, OpenAI) don't drop
#         their code samples. Deterministic same-in-same-out.
#   cfg5-rsc: also merge content recovered from Next.js RSC streaming
#         payloads (``self.__next_f.push`` calls). Recovers code +
#         headings + prose from App Router sites where the content is
#         embedded in the RSC state. No-op on sites that defer the body
#         to client-side ``_next/data`` fetches (e.g. newer
#         platform.claude.com).
EXTRACTOR_VERSION_HTML = f"trafilatura-{trafilatura.__version__}-cfg5-rsc"
EXTRACTOR_VERSION_PDF  = f"pypdf-{_PYPDF_VERSION}+plumber-{_PDFPLUMBER_VERSION}-tbl1"
# Pass-through for endpoints that already serve Markdown (no extraction needed).
EXTRACTOR_VERSION_MD   = "passthrough-md-v1"

# Backwards-compat alias — used by other modules and tests. Mirrors the HTML path,
# which is what 99%+ of pages exercise.
EXTRACTOR_VERSION = EXTRACTOR_VERSION_HTML

# PDF magic bytes (first 4-5 bytes of any PDF file)
_log = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"

# Heading anchor injection: turn "## Foo bar" into "## Foo bar {#foo-bar}"
# unless a {#...} suffix is already present.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_ANCHOR_PRESENT_RE = re.compile(r"\{#[A-Za-z0-9\-_]+\}\s*$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\s\-]+")
_SLUG_WS_RE = re.compile(r"\s+")


@dataclass
class ExtractResult:
    url: str
    raw_hash: Optional[str]
    content_hash: Optional[str]
    title: Optional[str]
    n_chars: int
    extractor_version: str
    normalizer_version: str
    ok: bool
    reason: Optional[str]   # 'unchanged-raw', 'new-content', 'no-body', '304', 'extract-failed'

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"

    @classmethod
    def no_content(
        cls, url: str, *, raw_hash: Optional[str], reason: Optional[str],
        extractor_version: str, ok: bool = False,
    ) -> "ExtractResult":
        """A no-extracted-content result (content_hash/title None, n_chars 0)
        with normalizer_version filled in — the shared shape for the 304 /
        error / no-body / extract-failed outcomes. Only url, raw_hash,
        extractor_version, ok and reason vary."""
        return cls(
            url=url, raw_hash=raw_hash, content_hash=None, title=None, n_chars=0,
            extractor_version=extractor_version, normalizer_version=normalizer_version(),
            ok=ok, reason=reason,
        )


def extract_markdown(html: bytes, url: str) -> tuple[Optional[str], Optional[str]]:
    """Run trafilatura with a fixed config. Returns (markdown, title) or (None, None).

    The config is part of EXTRACTOR_VERSION_HTML's identity — change it and bump the version.
    """
    # include_tables=True preserves tax rate tables, which matter on ATO pages.
    # deduplicate=False: trafilatura's dedupe uses a process-level LRU cache,
    # which makes extraction order-dependent and breaks determinism guarantees.
    # Our normalize step handles dynamic-boilerplate stripping deterministically.
    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_formatting=True,
        include_comments=False,
        favor_precision=True,
        deduplicate=False,
        with_metadata=False,
    )
    # Title is parsed separately so we can store it in frontmatter
    # without bloating the body. Pull this BEFORE checking md is None
    # so a successful RSC recovery on a no-prose-HTML page still gets
    # a title.
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = meta.title if meta else None

    # Even when trafilatura discards the body (typical for SPA-shell
    # HTML where the main flow is empty), Next.js RSC payloads may
    # carry real content. Start with an empty md so the recovery pass
    # has somewhere to merge into; we still return None if BOTH come
    # back empty so callers can treat the page as un-extractable.
    started_empty = md is None
    if md is None:
        md = ""
    # Run the HTML enricher pipeline (code-block recovery, then Next.js
    # RSC recovery). Each pass merges content trafilatura's main-content
    # scorer dropped; see extract_strategy.HTML_ENRICHERS for the
    # registry and the per-enricher rationale in their own modules.
    md, n_recovered = run_html_enrichers(md, html)
    if started_empty and n_recovered == 0:
        # trafilatura had nothing AND nothing was recovered from the
        # static HTML — treat as a non-extractable page so the commit
        # phase records the failure correctly.
        return None, None
    return md, title


def is_pdf(body: bytes) -> bool:
    """Sniff for PDF magic bytes at (or just after) the start of the body.

    Real PDFs put %PDF- at offset 0; we tolerate a few leading whitespace
    or NUL bytes (some producers / byte-order quirks emit them). We do NOT
    scan the whole first 1KB for %PDF-: an HTML page that merely *mentions*
    the bytes (a docs page about PDFs, a code sample, a ``data:`` URI) would
    otherwise be misrouted to the PDF extractor and silently dropped.
    """
    return body[:1024].lstrip(b"\x00 \t\r\n\f\v").startswith(_PDF_MAGIC)


_PDF_TABLE_PAGE_LIMIT = 150  # bound pdfplumber's table pass on pathological PDFs


def _render_pdf_table(rows) -> str:
    """A pdfplumber table (rows of cells) -> GitHub-flavored markdown, or '' if it
    has fewer than two non-empty rows (not a useful table)."""
    norm = [
        [("" if c is None else str(c)).replace("\n", " ").replace("|", "\\|").strip()
         for c in (row or [])]
        for row in (rows or [])
    ]
    norm = [r for r in norm if any(c for c in r)]
    if len(norm) < 2:
        return ""
    width = max(len(r) for r in norm)
    norm = [r + [""] * (width - len(r)) for r in norm]
    lines = ["| " + " | ".join(norm[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(lines)


def _pdf_tables_by_page(body: bytes) -> dict[int, list[str]]:
    """Per-page structured tables (markdown) via pdfplumber — deterministic. The
    digital-PDF table lane: pypdf flattens tables into prose, pdfplumber recovers
    the grid. Returns {} if pdfplumber is unavailable or the PDF can't be opened,
    so the caller degrades cleanly to pypdf text only (never worse than before)."""
    if pdfplumber is None:
        return {}
    import io
    out: dict[int, list[str]] = {}
    try:
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            if len(pdf.pages) > _PDF_TABLE_PAGE_LIMIT:
                _log.warning(
                    "pdf table extraction capped at %d of %d pages (page text "
                    "is unaffected; tables beyond the cap are not structured)",
                    _PDF_TABLE_PAGE_LIMIT, len(pdf.pages),
                )
            for i, page in enumerate(pdf.pages[:_PDF_TABLE_PAGE_LIMIT], start=1):
                try:
                    raw = page.extract_tables()
                except Exception:
                    continue
                mds = [m for m in (_render_pdf_table(t) for t in raw) if m]
                if mds:
                    out[i] = mds
    except Exception:
        return {}
    return out


def extract_pdf(body: bytes, url: str) -> tuple[Optional[str], Optional[str]]:
    """PDF -> markdown. Returns (markdown, title) or (None, None).

    Deterministic given identical input bytes. Per page we emit pypdf's text
    (reliable across forms whose text lives in annotations) and append any
    structured tables pdfplumber recovers as GitHub-flavored markdown — the
    table data pypdf would otherwise flatten into unreadable prose. The title is
    the PDF metadata title (or the URL basename). Scanned-image PDFs yield empty
    output (handled as extract-failed); text-encoded PDFs come through cleanly.
    """
    if pypdf is None:
        return None, None
    import io
    try:
        reader = pypdf.PdfReader(io.BytesIO(body), strict=False)
    except Exception:
        return None, None

    # Try to honor pypdf's metadata title; fall back to URL basename.
    title: Optional[str] = None
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip() or None
    except Exception:
        title = None
    if not title:
        from urllib.parse import urlparse as _u
        title = _u(url).path.rsplit("/", 1)[-1] or "PDF document"

    tables_by_page = _pdf_tables_by_page(body)
    pages_md: list[str] = []
    try:
        for i, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            text = text.strip()
            tbls = tables_by_page.get(i, [])
            if not text and not tbls:
                continue
            parts = [text] if text else []
            if tbls:
                parts.append("\n\n".join(tbls))
            pages_md.append(f"## Page {i}\n\n" + "\n\n".join(parts))
    except Exception:
        # Encrypted / corrupt / unsupported PDF — leave as None so the
        # extract phase records 'extract-failed' with a clean reason.
        return None, None

    if not pages_md:
        return None, None
    md = f"# {title}\n\n" + "\n\n".join(pages_md)
    return md, title


def _is_pdf_url(url: str) -> bool:
    """Quick URL-shape hint — used as a tie-breaker before we read the body."""
    return url.lower().split("?", 1)[0].endswith(".pdf")


def extract_passthrough_md(body: bytes, url: str) -> tuple[Optional[str], Optional[str]]:
    """Markdown source -> markdown, unchanged. Returns (markdown, title) or (None, None).

    Some docs hosts serve a clean Markdown variant directly (e.g. Stripe's
    ``docs.stripe.com/<page>.md`` LLM-friendly endpoints). For those the HTML
    extractor is the wrong tool — trafilatura parses the markdown as HTML and
    usually yields nothing — so the body already *is* the content: decode and
    pass it through. Deterministic given identical bytes. Title is the first
    ATX H1 (``# ...``), if present.
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None, None
    m = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    title = m.group(1).strip() if m else None
    return text, (title or None)


# ---- Primary-strategy registry --------------------------------------------
# The competing primaries, in priority order. ``select_primary`` returns
# the first whose predicate matches; the HTML strategy is the terminal
# fallback (predicate always True). This replaces the if/elif/else that
# used to live in ``extract_one`` — same dispatch, now a registry an
# acquisition/quality layer can introspect.

def _md_applies(inp: ExtractInput) -> bool:
    """Markdown pass-through: the profile explicitly classified this body
    as markdown (a .md docs variant / llms.txt). The HTML extractor
    mangles those, so they're stored as-is."""
    return inp.body_kind == "markdown"


def _pdf_applies(inp: ExtractInput) -> bool:
    """PDF: the profile said so, OR the profile deferred and the body
    sniffs as a PDF (magic bytes) / the URL ends in .pdf."""
    return inp.body_kind == "pdf" or (
        inp.body_kind is None and (is_pdf(inp.raw) or _is_pdf_url(inp.url))
    )


def _html_applies(inp: ExtractInput) -> bool:
    """HTML (trafilatura + enrichers): the terminal fallback — handles
    everything the markdown/PDF predicates didn't claim."""
    return True


PRIMARY_STRATEGIES: tuple[PrimaryStrategy, ...] = (
    PrimaryStrategy("markdown-passthrough", "md", EXTRACTOR_VERSION_MD,
                    _md_applies, extract_passthrough_md),
    PrimaryStrategy("pdf", "pdf", EXTRACTOR_VERSION_PDF,
                    _pdf_applies, extract_pdf),
    PrimaryStrategy("html-trafilatura", "html", EXTRACTOR_VERSION_HTML,
                    _html_applies, extract_markdown),
)


def slugify(text: str) -> str:
    """Heading text -> stable anchor slug (a-z, 0-9, dashes only)."""
    s = text.lower().strip()
    s = _SLUG_STRIP_RE.sub("", s)
    s = _SLUG_WS_RE.sub("-", s).strip("-")
    return s[:80]


def inject_heading_anchors(md: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Add {#slug} to every heading that doesn't already have one.

    Returns (annotated_markdown, [(level, slug, text), ...]).

    Slug collisions within a single document are disambiguated with -2, -3, …
    so grep on '{#some-anchor}' always finds exactly one heading per file.
    """
    seen: Counter[str] = Counter()
    anchors: list[tuple[int, str, str]] = []

    def _replace(m: re.Match) -> str:
        hashes, text = m.group(1), m.group(2)
        if _ANCHOR_PRESENT_RE.search(text):
            return m.group(0)
        base = slugify(text) or "section"
        n = seen[base]
        seen[base] += 1
        slug = base if n == 0 else f"{base}-{n + 1}"
        anchors.append((len(hashes), slug, text))
        return f"{hashes} {text} {{#{slug}}}"

    return _HEADING_RE.sub(_replace, md), anchors


def build_frontmatter(
    *,
    url: str,
    title: Optional[str],
    fetched_at: str,
    http_status: int,
    raw_hash: str,
    content_hash: str,
    extractor_version: str,
    normalizer_version: str,
    crawler_version: str,
    tier: str,
    parent_guide_: Optional[str],
    sitemap_lastmod: Optional[str],
    audience: str,
    fy_years: list[str],
    anchors: list[str],
) -> str:
    """YAML front-matter that lets downstream agents self-verify and discover the page."""
    lines = [
        "---",
        f"url: {url}",
        f"title: {json.dumps(title) if title else 'null'}",
        f"fetched_at: {fetched_at}",
        f"http_status: {http_status}",
        f"sitemap_lastmod: {sitemap_lastmod or 'null'}",
        f"raw_hash: sha256:{raw_hash}",
        f"content_hash: sha256:{content_hash}",
        f"crawler_version: {crawler_version}",
        f"extractor_version: {extractor_version}",
        f"normalizer_version: {normalizer_version}",
        f"tier: {tier}",
        f"parent_guide: {parent_guide_ or 'null'}",
        f"audience: {audience}",
        f"fy_years: {json.dumps(fy_years)}",
        f"anchors: {json.dumps(anchors)}",
        "---",
        "",
    ]
    return "\n".join(lines)


@dataclass
class ReextractResult:
    """Output of the canonical re-extract-from-raw pipeline — everything a
    consumer needs to reproduce a published page's content_hash + body."""
    ok: bool
    content_hash: Optional[str]
    annotated_md: Optional[str]
    title: Optional[str]
    anchor_slugs: list[str]
    extractor_version: str
    kind: str


def hash_normalized_body(annotated_md: str) -> str:
    """THE content-hash step: normalize (under the active profile) then
    sha256. Single-sourced so production (``extract_one``), the determinism
    eval, and ``read_md --verify`` can never drift on how the hash is taken.
    The active profile matters — see sift.index_profile.apply_index_profile.
    """
    return sha256_hex(normalize_for_hash(annotated_md).encode("utf-8"))


def reextract_and_hash(
    raw: bytes, url: str, *, content_type: Optional[str] = None,
) -> ReextractResult:
    """Canonical re-extract -> anchor -> normalize -> hash from a raw blob.

    Pure function of (raw, url, active site profile). This is the ONE
    dispatch used by ``extract_one`` (production), the determinism eval,
    and any other re-hash consumer, so they route the markdown-passthrough
    / PDF / HTML primary selection and the enricher pipeline identically.
    Re-implementing a subset of this (as the determinism eval used to —
    skipping the markdown-passthrough primary) is exactly the drift this
    consolidates away.

    The caller must have activated the index's profile first
    (sift.index_profile.apply_index_profile) — the hash depends on it.
    """
    inp = ExtractInput(
        raw=raw, url=url, content_type=content_type,
        body_kind=current_profile().body_kind(url, content_type=content_type),
    )
    primary = select_primary(inp, PRIMARY_STRATEGIES)
    md, title = primary.extract(raw, url)
    if md is None:
        return ReextractResult(
            ok=False, content_hash=None, annotated_md=None, title=None,
            anchor_slugs=[], extractor_version=primary.version, kind=primary.kind,
        )
    annotated_md, anchor_tuples = inject_heading_anchors(md)
    return ReextractResult(
        ok=True,
        content_hash=hash_normalized_body(annotated_md),
        annotated_md=annotated_md,
        title=title,
        anchor_slugs=[a[1] for a in anchor_tuples],
        extractor_version=primary.version,
        kind=primary.kind,
    )


def extract_one(
    fetch: FetchResult,
    *,
    root: Path,
    run_id: str,
    conn: sqlite3.Connection,
    crawler_version: str,
) -> ExtractResult:
    """Process one fetch outcome. Idempotent: same fetch -> same extract result.

    Containment boundary: any unexpected exception from the blob read,
    parse/hash, or file write in the inner implementation is caught and
    recorded as a single ok=False ("extract-error:<Type>") row. One
    pathological blob can never abort the whole extract_all batch — the
    page is marked failed and the loop moves on, mirroring the per-URL
    isolation the fetch phase already provides.
    """
    try:
        return _extract_one(
            fetch, root=root, run_id=run_id, conn=conn,
            crawler_version=crawler_version,
        )
    except Exception as e:
        return ExtractResult.no_content(
            fetch.url, raw_hash=(fetch.raw_hash or None),
            reason=f"extract-error:{type(e).__name__}", extractor_version=EXTRACTOR_VERSION,
        )


def _extract_one(
    fetch: FetchResult,
    *,
    root: Path,
    run_id: str,
    conn: sqlite3.Connection,
    crawler_version: str,
) -> ExtractResult:
    url = fetch.url
    prev = get_row(conn, url)

    # 304: nothing to extract, no md file written for this run.
    if fetch.status == 304:
        return ExtractResult.no_content(
            url, raw_hash=None, reason="304", extractor_version=EXTRACTOR_VERSION, ok=True,
        )

    # 4xx/5xx: nothing to extract.
    if fetch.error or fetch.status >= 400:
        return ExtractResult.no_content(
            url, raw_hash=None, reason=fetch.error or f"http-{fetch.status}",
            extractor_version=EXTRACTOR_VERSION,
        )

    if not fetch.raw_hash:
        return ExtractResult.no_content(
            url, raw_hash=None, reason="no-raw-hash", extractor_version=EXTRACTOR_VERSION,
        )

    raw = read_raw_blob(root, fetch.raw_hash)

    # Route via the primary-strategy registry: markdown pass-through for
    # endpoints that already ship Markdown (the HTML extractor mangles
    # them), then the generic body sniff (PDF by magic bytes / URL, else
    # HTML). PDFs are common in /law/view/pdf/* on ATO. The version
    # recorded on the row reflects the extractor that actually ran, so a
    # bump to one path doesn't invalidate the others' hashes.
    inp = ExtractInput(
        raw=raw, url=url, content_type=fetch.content_type,
        body_kind=current_profile().body_kind(url, content_type=fetch.content_type),
    )
    primary = select_primary(inp, PRIMARY_STRATEGIES)
    current_extractor_version = primary.version
    extract_kind = primary.kind
    norm_v = normalizer_version()  # effective version: profile-fingerprinted

    # Short-circuit: raw_hash unchanged AND extractor/normalizer versions match
    # AND we have a content_hash on file -> nothing to do.
    if (prev is not None and prev.raw_hash == fetch.raw_hash
            and prev.content_hash and prev.extractor_version == current_extractor_version
            and prev.normalizer_version == norm_v):
        return ExtractResult(
            url=url, raw_hash=fetch.raw_hash, content_hash=prev.content_hash,
            title=None, n_chars=0,
            extractor_version=current_extractor_version, normalizer_version=norm_v,
            ok=True, reason="unchanged-raw",
        )

    # Re-extract + hash via the canonical pipeline (same dispatch the
    # determinism eval + read_md --verify use, so they can't drift).
    res = reextract_and_hash(raw, url, content_type=fetch.content_type)
    if not res.ok:
        return ExtractResult.no_content(
            url, raw_hash=fetch.raw_hash, reason=f"extract-failed-{extract_kind}",
            extractor_version=current_extractor_version,
        )

    # Content-admission (trust boundary): refuse to commit a non-empty extraction
    # that is actually a bot-challenge interstitial. looks_thin only *escalates*
    # at fetch time; if escalation is off or every transport tier is blocked, the
    # challenge body reaches here and would otherwise be hashed + signed as real
    # content. Conservative (hard vendor marker + short body) -> real pages pass.
    admit_ok, admit_reason = admit_content(raw, res.annotated_md, fetch.content_type)
    if not admit_ok:
        return ExtractResult.no_content(
            url, raw_hash=fetch.raw_hash, reason=admit_reason,
            extractor_version=current_extractor_version,
        )
    title = res.title
    annotated_md = res.annotated_md
    anchor_slugs = res.anchor_slugs
    content_hash = res.content_hash

    # Compose final markdown file: frontmatter + body. The body is the annotated md
    # (not the normalized version) — normalization is for hashing only, the file
    # itself stays human-readable with browsable anchors.
    tier = classify_tier(url).value
    pg = parent_guide(url)
    aud = audience_for(url)
    fys = fy_years_for(url)
    sm_lastmod = prev.sitemap_lastmod_seen if prev else None
    frontmatter = build_frontmatter(
        url=url, title=title, fetched_at=fetch.fetched_at, http_status=fetch.status,
        raw_hash=fetch.raw_hash, content_hash=content_hash,
        extractor_version=current_extractor_version, normalizer_version=norm_v,
        crawler_version=crawler_version, tier=tier, parent_guide_=pg,
        sitemap_lastmod=sm_lastmod,
        audience=aud, fy_years=fys, anchors=anchor_slugs,
    )
    md_file = paths.md_path(root, run_id, url)
    meta_file = paths.meta_path(root, run_id, url)
    atomic_write_text(md_file, frontmatter + annotated_md.rstrip() + "\n")
    atomic_write_text(meta_file, json.dumps({
        "url": url, "title": title, "fetched_at": fetch.fetched_at,
        "http_status": fetch.status, "raw_hash": f"sha256:{fetch.raw_hash}",
        "content_hash": f"sha256:{content_hash}",
        "crawler_version": crawler_version,
        "extractor_version": current_extractor_version,
        "normalizer_version": norm_v,
        "tier": tier, "parent_guide": pg,
        "audience": aud, "fy_years": fys,
        "anchors": anchor_slugs,
        "sitemap_lastmod": sm_lastmod, "n_chars": len(annotated_md),
    }, indent=2))

    return ExtractResult(
        url=url, raw_hash=fetch.raw_hash, content_hash=content_hash,
        title=title, n_chars=len(annotated_md),
        extractor_version=current_extractor_version, normalizer_version=norm_v,
        ok=True, reason=f"new-content-{extract_kind}",
    )


def re_extract_corpus(
    root: Path,
    *,
    run_id: str,
    conn: sqlite3.Connection,
    crawler_version: str,
    fresh_only: bool = True,
    tiers: Optional[tuple[str, ...]] = None,
) -> tuple[Path, Path, int]:
    """Re-extract every FRESH/FROZEN row from its cached raw blob, producing
    a new fetch.log + extract.log for `run_id` that the commit phase can apply.

    Used when an extractor or normalizer version bump (or a profile swap) means
    stored content_hashes are stale. Synthesizes one fetch.log entry per
    eligible row (status=200, raw_hash from manifest), then runs extract_all
    which short-circuits any row whose extractor_version + normalizer_version
    already match the current versions (so this is idempotent).

    The commit phase then naturally produces "changed" changelog entries with
    the proper old_hash → new_hash diff, because the manifest still holds
    the prior content_hash.

    Returns (fetch_log_path, extract_log_path, urls_processed).
    Caller is responsible for invoking commit + publish on the resulting logs.
    """
    from .manifest import iter_all, now_utc
    fl = paths.fetch_log_path(root, run_id)
    el = paths.extract_log_path(root, run_id)
    fl.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for row in iter_all(conn):
        if fresh_only and row.state != "FRESH":
            continue
        if not fresh_only and row.state not in ("FRESH", "FROZEN"):
            continue
        if not row.raw_hash:
            continue
        if tiers is not None and row.tier not in tiers:
            continue
        rows.append(row)

    # Synthesize fetch.log entries from manifest state
    with fl.open("w") as f:
        for row in rows:
            fr = FetchResult(
                url=row.url, decision="FETCH", status=200,
                etag=row.http_etag, last_modified=row.http_last_modified,
                raw_hash=row.raw_hash,
                raw_bytes=0,  # extract phase doesn't read this
                fetched_at=row.last_fetched_at or now_utc(),
                error=None,
            )
            f.write(fr.to_json_line())

    # Run extract — short-circuits when raw_hash + extractor/normalizer versions match
    fetches = []
    with fl.open() as f:
        for line in f:
            try:
                fetches.append(FetchResult(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue

    n = extract_all(
        fetches, root=root, run_id=run_id, conn=conn,
        crawler_version=crawler_version, extract_log=el,
    )
    return fl, el, n


def extract_all(
    fetches: Iterable[FetchResult],
    *,
    root: Path,
    run_id: str,
    conn: sqlite3.Connection,
    crawler_version: str,
    extract_log: Path,
) -> int:
    """Run extract over a stream of fetch results. Appends to extract_log."""
    extract_log.parent.mkdir(parents=True, exist_ok=True)
    # Resume: skip URLs already in extract.log.
    done: set[str] = set()
    if extract_log.exists():
        with extract_log.open() as f:
            for line in f:
                try:
                    done.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue
    count = 0
    with extract_log.open("a") as out:
        for fr in fetches:
            if fr.url in done:
                continue
            res = extract_one(fr, root=root, run_id=run_id, conn=conn,
                              crawler_version=crawler_version)
            out.write(res.to_json_line())
            out.flush()
            count += 1
    return count
