"""Tool implementations exposed to Claude under each eval condition.

Three tool sets, mapped 1:1 to the three eval conditions:

  * ``closed-book`` — empty set; the model relies on parametric knowledge.
  * ``sift-grep``   — ``grep_index`` + ``read_page``. ``grep_index`` runs a
                      ripgrep-style regex search across the published markdown
                      files under ``<root>/current/md/``; ``read_page``
                      fetches the full markdown for a specific URL via
                      ``sift.paths.md_path``. The shape is deliberately the
                      cheapest thing a sift-using agent might wire up so the
                      lift attributed to sift is conservative.
  * ``web-fetch``   — ``fetch_url`` over httpx. Polite default UA, 10s
                      timeout, content truncated to ~30K chars so the agent
                      can't pull a single 5MB page into context. No
                      Cloudflare-bypass logic; the goal is to measure what
                      "a vanilla agent that just hits the web" produces, not
                      to give web-fetch a free upgrade to sift's fallback.

All tool callables share the same Python signature:
``Callable[[dict], dict]`` — they take the tool's input args as a dict and
return a result dict ready to JSON-encode into the tool_result block.

Errors are returned as ``{"error": "..."}`` payloads so the agent loop can
keep going (matching how real Claude tool-use returns surface errors).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

from sift import paths
from sift.classify import canonicalize_url, safe_path_segments


# ---- shared types ----------------------------------------------------------

ToolFn = Callable[[dict], dict]


@dataclass(frozen=True)
class ToolSpec:
    """A single tool exposed to Claude. The ``schema`` matches Anthropic's
    tool-use input format; ``fn`` is the local callable that runs when the
    model emits a tool_use block for ``name``."""
    name: str
    description: str
    schema: dict
    fn: ToolFn


# ---- sift-grep tools -------------------------------------------------------

# Cap of result snippets we hand back per grep call. Each snippet is a few
# lines so the total tool_result body stays bounded — large bodies inflate
# context and bias the comparison toward grep-heavy strategies.
_GREP_MAX_RESULTS = 12
_GREP_CONTEXT_LINES = 2
_READ_PAGE_MAX_CHARS = 30_000


def _md_root(root: Path, run_id: Optional[str]) -> Path:
    """Resolve the directory the markdown files live in.

    If ``run_id`` is None, follow the ``current`` symlink — same convention
    as ``sift mcp`` and ``sift-evals baseline``.
    """
    if run_id is None:
        cur = paths.current_symlink(root)
        if cur.exists():
            return cur.resolve() / "md"
        raise FileNotFoundError(
            f"{root}: no <root>/current symlink and no --run-id passed"
        )
    return paths.run_dir(root, run_id) / "md"


def make_grep_tool(root: Path, run_id: Optional[str] = None) -> ToolSpec:
    """Build the ``grep_index`` tool. Uses ripgrep when available (much
    faster) and falls back to a pure-Python regex walk.

    The ripgrep fallback matters: the eval harness CI image may not include
    ``rg``, and we don't want the tool to be silently absent under one
    condition vs another. The pure-Python path is slower but deterministic
    and uses only the standard library.
    """
    md_root = _md_root(root, run_id)

    def grep_index(args: dict) -> dict:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return {"error": "missing required 'pattern' argument"}
        try:
            re.compile(pattern)
        except re.error as e:
            return {"error": f"invalid regex: {e}"}
        max_results = int(args.get("max_results") or _GREP_MAX_RESULTS)
        max_results = max(1, min(max_results, _GREP_MAX_RESULTS))

        # Try ripgrep first — it handles large corpora much faster and
        # supports the same regex flavor we want.
        rg_path = _which("rg")
        if rg_path is not None:
            matches = _grep_with_rg(rg_path, md_root, pattern, max_results)
        else:
            matches = _grep_pure_python(md_root, pattern, max_results)
        return {
            "pattern": pattern,
            "matches": matches,
            "total_matches": len(matches),
            "engine": "ripgrep" if rg_path else "python",
        }

    return ToolSpec(
        name="grep_index",
        description=(
            "Regex search over the published sift index. Returns up to "
            f"{_GREP_MAX_RESULTS} short snippets, each with the source URL "
            "and the matched line. Use this first to locate the right page "
            "before calling read_page on the full content."
        ),
        schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Regular expression to search for. POSIX-style; "
                        "case-sensitive by default — use (?i) to relax."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"Cap on returned snippets (default {_GREP_MAX_RESULTS}). "
                        "Lower this when you only need to confirm a single page."
                    ),
                    "minimum": 1,
                    "maximum": _GREP_MAX_RESULTS,
                },
            },
            "required": ["pattern"],
        },
        fn=grep_index,
    )


def _which(prog: str) -> Optional[str]:
    """Tiny ``shutil.which`` replacement that returns the absolute path or
    ``None``. Inlined to avoid the import-time cost in the hot path."""
    import shutil
    return shutil.which(prog)


def _grep_with_rg(rg: str, md_root: Path, pattern: str,
                  max_results: int) -> list[dict]:
    if not md_root.exists():
        return []
    # ``--json`` gives us a stable shape: one match per line, no need to
    # parse ripgrep's text output. ``-m`` caps matches per file so a single
    # noisy file can't dominate the result list.
    cmd = [rg, "--json", "-C", str(_GREP_CONTEXT_LINES),
           "-m", "3", "--", pattern, str(md_root)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True,
                              text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return []
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        if len(out) >= max_results:
            break
        try:
            ev = _parse_rg_event(line)
        except Exception:
            continue
        if ev:
            out.append(ev)
    return out


def _parse_rg_event(line: str) -> Optional[dict]:
    import json as _json
    body = _json.loads(line)
    if body.get("type") != "match":
        return None
    data = body.get("data") or {}
    path = (data.get("path") or {}).get("text") or ""
    line_no = data.get("line_number")
    text = ((data.get("lines") or {}).get("text") or "").rstrip()
    return {
        "file": path,
        "line": line_no,
        "snippet": text[:240],
        "url": _path_to_url(Path(path)),
    }


def _grep_pure_python(md_root: Path, pattern: str,
                      max_results: int) -> list[dict]:
    if not md_root.exists():
        return []
    rx = re.compile(pattern)
    out: list[dict] = []
    for md_file in md_root.rglob("*.md"):
        try:
            for i, raw_line in enumerate(md_file.read_text(
                    encoding="utf-8", errors="replace"
            ).splitlines(), start=1):
                if rx.search(raw_line):
                    out.append({
                        "file": str(md_file),
                        "line": i,
                        "snippet": raw_line.rstrip()[:240],
                        "url": _path_to_url(md_file),
                    })
                    if len(out) >= max_results:
                        return out
        except OSError:
            continue
    return out


def _path_to_url(md_file: Path) -> Optional[str]:
    """Best-effort: read the front-matter ``url:`` line if it exists; else
    return ``None``. We don't try to reconstruct a URL from the path because
    the host isn't encoded in the filesystem layout (the path is just the
    URL's path component)."""
    try:
        with md_file.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048)
        # sift's frontmatter is YAML between ``---`` markers; the URL is
        # typically the first ``url:`` line.
        m = re.search(r"^url:\s*(\S+)", head, flags=re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        return None
    return None


def make_read_tool(root: Path, run_id: Optional[str] = None) -> ToolSpec:
    """Build the ``read_page`` tool — fetches the markdown body for a
    specific URL. Caps output at ``_READ_PAGE_MAX_CHARS`` so a single big
    page (e.g. a full RFC) can't consume an entire context window."""
    md_root = _md_root(root, run_id)
    rid = run_id if run_id is not None else paths.current_symlink(root).resolve().name

    def read_page(args: dict) -> dict:
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "missing required 'url' argument"}
        canonical = canonicalize_url(url)
        try:
            p = paths.md_path(root, rid, canonical)
        except Exception as e:
            return {"error": f"failed to resolve path: {e}"}
        if not p.exists():
            return {"error": f"no page indexed for {canonical}", "url": canonical}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"error": f"read failed: {e}"}
        truncated = len(text) > _READ_PAGE_MAX_CHARS
        return {
            "url": canonical,
            "content": text[:_READ_PAGE_MAX_CHARS],
            "truncated": truncated,
            "bytes_total": len(text),
        }

    return ToolSpec(
        name="read_page",
        description=(
            "Return the full sift-indexed markdown body for a specific URL. "
            f"Truncates at {_READ_PAGE_MAX_CHARS} chars. Use this after "
            "grep_index has narrowed down the right page."
        ),
        schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "The canonical URL to read, e.g. "
                        "https://docs.python.org/3/library/pathlib.html"
                    ),
                },
            },
            "required": ["url"],
        },
        fn=read_page,
    )


# ---- web-fetch tool --------------------------------------------------------

_WEB_FETCH_TIMEOUT = 10.0
_WEB_FETCH_MAX_CHARS = 30_000
_WEB_FETCH_UA = "sift-agent-eval/1.0 (+https://github.com/dvlshah/sift)"


def make_fetch_tool() -> ToolSpec:
    """Build the ``fetch_url`` tool — plain HTTP GET, no JS, no bot-bypass.

    Deliberately bare so the comparison is "vanilla agent" vs "sift agent".
    If we layered Firecrawl into this tool the web-fetch condition would
    inherit sift's edge for free.
    """
    def fetch_url(args: dict) -> dict:
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "missing required 'url' argument"}
        if not url.startswith(("http://", "https://")):
            return {"error": "url must be absolute (http:// or https://)"}
        try:
            with httpx.Client(
                    timeout=_WEB_FETCH_TIMEOUT,
                    headers={"User-Agent": _WEB_FETCH_UA},
                    follow_redirects=True,
            ) as c:
                resp = c.get(url)
        except httpx.HTTPError as e:
            return {"error": f"fetch failed: {e}", "url": url}
        text = resp.text
        truncated = len(text) > _WEB_FETCH_MAX_CHARS
        return {
            "url": str(resp.url),
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "content": text[:_WEB_FETCH_MAX_CHARS],
            "truncated": truncated,
            "bytes_total": len(text),
        }

    return ToolSpec(
        name="fetch_url",
        description=(
            "HTTP GET a single URL and return the raw response body. "
            f"Truncates at {_WEB_FETCH_MAX_CHARS} chars. Does NOT execute "
            "JavaScript and does not bypass bot-blocks. Use as a last-resort "
            "lookup; some sites will return 403 or a Cloudflare challenge."
        ),
        schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to fetch.",
                },
            },
            "required": ["url"],
        },
        fn=fetch_url,
    )


# ---- condition → toolset registry -----------------------------------------

def tools_for(condition: str, *, root: Optional[Path] = None,
              run_id: Optional[str] = None) -> list[ToolSpec]:
    """Return the tool set for one of the named eval conditions.

    ``root`` is required for ``sift-grep`` (it's the path of the sift index
    the agent will query); ignored for the other conditions.
    """
    if condition == "closed-book":
        return []
    if condition == "sift-grep":
        if root is None:
            raise ValueError("sift-grep condition requires `root`")
        return [make_grep_tool(root, run_id), make_read_tool(root, run_id)]
    if condition == "web-fetch":
        return [make_fetch_tool()]
    raise ValueError(f"unknown condition: {condition!r}")


# Used by the runner for stable iteration order.
CONDITIONS: tuple[str, ...] = ("closed-book", "sift-grep", "web-fetch")
