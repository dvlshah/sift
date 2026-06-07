"""Shared low-level I/O + serialization primitives.

This is a pure-stdlib **leaf** module (no intra-package imports) so any
sift module can depend on it without risking an import cycle. It exists to
single-source the handful of primitives that were previously re-rolled in
2–4 places each — hashing, atomic writes, frontmatter parsing, snapshot
reads. Centralizing them means the write path and the verify path can't
drift on *how* a hash is taken or a file is parsed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional


# ---- Hashing ---------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    """sha256 of ``data`` as a lowercase hex string. THE hashing primitive —
    fetch (raw-blob ids), extract (content hash), integrity (merkle leaves),
    and publish (gate sampling) all route through this so they can't diverge."""
    return hashlib.sha256(data).hexdigest()


# ---- Atomic writes ---------------------------------------------------------

def atomic_write_text(path: Path, content: str) -> None:
    """Write via tmp + rename so a crash mid-write never leaves a partial
    file. Creates parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


# ---- Frontmatter -----------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)


def split_frontmatter(text: str) -> tuple[Optional[str], str]:
    """Split a published md file into ``(frontmatter_block, body)``.

    Returns ``(None, text)`` when there's no leading ``---`` frontmatter.
    The frontmatter block excludes the fence lines; the body is everything
    after the closing ``---``."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def parse_frontmatter(fm: str) -> dict[str, str]:
    """Parse a frontmatter block into a flat ``{key: value}`` dict. Lines
    without a ``:`` are skipped; values are stripped. Intentionally minimal
    (not a full YAML parser) — sift frontmatter is flat ``key: value``."""
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


# ---- Snapshot reads --------------------------------------------------------

def read_snapshot(run_dir: Path) -> dict:
    """Read a run's ``snapshot.json``. Returns ``{}`` on any miss (absent /
    unreadable / malformed) so callers degrade gracefully rather than raise
    on a torn snapshot."""
    snap = Path(run_dir) / "snapshot.json"
    if not snap.exists():
        return {}
    try:
        return json.loads(snap.read_text())
    except (OSError, ValueError):
        return {}
