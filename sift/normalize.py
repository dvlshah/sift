"""Deterministic content normalization for stable hashing.

`normalize_for_hash` strips dynamic boilerplate that would otherwise produce
false-positive content changes (e.g. a rotating timestamp making the hash
different on every fetch even when the body is identical).

What's site-agnostic and stays here:
  * Unicode NFC
  * CRLF → LF
  * Per-line trailing whitespace
  * Blank-line collapse
  * Surrounding whitespace trim

What's site-specific and comes from the profile:
  * `dynamic_patterns` — regex patterns of boilerplate to strip. ATO has
    "Last modified:", "QC ####" quick-codes, the Commonwealth copyright
    year. Other sites have their own rotating boilerplate.

Bumping `NORMALIZER_VERSION` invalidates all stored content_hashes; the
extract phase will recompute them from cached raw HTML without refetching.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .sites import current_profile

NORMALIZER_VERSION = "v2"  # base algorithm version; effective = normalizer_version()

_BLANK_LINE_RUN = re.compile(r"\n{3,}")


def normalizer_version() -> str:
    """Effective normalizer version: the base algorithm version plus a
    fingerprint of the active profile's ``dynamic_patterns``.

    ``normalize_for_hash`` strips the active profile's ``dynamic_patterns``, so
    the content_hash depends on them — but the base version string can't see
    them. Without this fingerprint, editing a profile's patterns would leave
    every stored ``normalizer_version`` reading ``"v2"``, and the idempotency
    short-circuit (decide/extract compare the stored version against this one)
    would skip re-extraction — silently leaving content_hashes that no longer
    match what the normalizer now produces.

    A profile with no patterns (the pipeline default) returns the bare base
    version, so existing zero-pattern indexes don't re-extract on upgrade.
    """
    pats = current_profile().dynamic_patterns
    if not pats:
        return NORMALIZER_VERSION
    # JSON-encode [pattern, int(flags)] per pattern: unambiguous (injective —
    # no separator a regex source could forge, unlike a NUL join) and stable
    # across Python minors (int(flags) doesn't change, unlike str(flags)/repr).
    payload = json.dumps(
        [[p.pattern, int(p.flags)] for p in pats],
        ensure_ascii=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{NORMALIZER_VERSION}+{digest}"


def normalize_for_hash(text: str) -> str:
    """Stable, deterministic transform whose output is what we hash.

    Pure: same input + NORMALIZER_VERSION + active profile's dynamic_patterns
    = same output. No clock, no I/O.
    """
    if not text:
        return ""
    # 1. Unicode NFC so visually-identical chars don't hash differently.
    text = unicodedata.normalize("NFC", text)
    # 2. Strip site-specific dynamic boilerplate from the active profile.
    # The pipeline default has zero patterns; ATOProfile contributes 6.
    for pat in current_profile().dynamic_patterns:
        # Patterns can be either deletion or replacement. If the source
        # contains a capturing group like Commonwealth-of-Australia, we
        # substitute back to keep the prefix; otherwise we delete.
        if "(" in pat.pattern and "©" in pat.pattern:
            text = pat.sub(r"\1", text)
        else:
            text = pat.sub("", text)
    # 3. Normalize line endings to LF.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 4. Trim per-line trailing whitespace.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # 5. Collapse runs of 3+ blank lines to exactly 2.
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    # 6. Strip surrounding whitespace.
    return text.strip()
