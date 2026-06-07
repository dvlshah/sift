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

import re
import unicodedata

from .sites import current_profile

NORMALIZER_VERSION = "v2"  # bumped when dynamic patterns moved to profile

_BLANK_LINE_RUN = re.compile(r"\n{3,}")


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
