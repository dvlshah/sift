"""Content-admission gate: refuse to commit a non-empty bot-challenge page.

looks_thin only *escalates* the fetch on a challenge page; when escalation is
disabled or every transport tier is blocked, the interstitial reaches extract.
The HTML extractor yields it as NON-empty content (so the upstream empty-check
doesn't catch it), and it would otherwise be hashed and signed as real content.
"""
import pytest

from sift import CRAWLER_VERSION, paths
from sift.extract import extract_one, reextract_and_hash
from sift.fetch import FetchResult, sha256_hex, write_raw_blob
from sift.manifest import init_schema, now_utc, open_db
from sift.quality import admit_content
from sift.sites import SiteProfile, current_profile, set_profile

# A realistic Cloudflare "Just a moment" interstitial: a hard vendor fingerprint
# (cf-browser-verification + /cdn-cgi/challenge-platform) plus ~150 chars of
# visible boilerplate that the HTML extractor reproduces as non-empty content.
CF_CHALLENGE = b"""<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div class="cf-browser-verification cf-im-under-attack">
<h1>Checking your browser before accessing example.com.</h1>
<p>This process is automatic. Your browser will redirect to your requested
content shortly. Please allow up to 5 seconds. DDoS protection by Cloudflare.</p>
</div><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>
</body></html>"""


@pytest.fixture(autouse=True)
def _generic_profile():
    """Route through the generic HTML extractor; restore afterwards."""
    prev = current_profile()
    set_profile(SiteProfile())
    yield
    set_profile(prev)


# ---- the helper -------------------------------------------------------------

def test_extractor_yields_nonempty_so_empty_check_is_insufficient():
    # Premise: the challenge page is NOT caught by the upstream ok=False path.
    res = reextract_and_hash(CF_CHALLENGE, "https://ex.test/blocked", content_type="text/html")
    assert res.ok
    assert 0 < len((res.annotated_md or "").strip()) < 512


def test_admission_rejects_challenge_interstitial():
    res = reextract_and_hash(CF_CHALLENGE, "https://ex.test/blocked", content_type="text/html")
    ok, reason = admit_content(CF_CHALLENGE, res.annotated_md, "text/html")
    assert ok is False
    assert reason == "admission-challenge-page"


def test_hard_marker_but_substantial_content_is_admitted():
    # A security write-up that names a bot-manager but has real content: the
    # length guard keeps it admitted — no false-positive data loss.
    long_body = "DataDome is a bot-management vendor used by many sites. " * 30  # >512
    ok, _ = admit_content(b"<html>... datadome ...</html>", long_body, "text/html")
    assert ok is True


def test_short_page_without_vendor_marker_is_admitted():
    body = "See also: installation, configuration, troubleshooting."
    ok, _ = admit_content(b"<html><body>" + body.encode() + b"</body></html>", body, "text/html")
    assert ok is True


def test_weak_generic_phrase_alone_does_not_reject():
    # "access denied" can appear in legitimate prose; only HARD vendor markers
    # trip admission, so a short page mentioning it (no vendor token) is admitted.
    body = "Error 403: access denied means the server refused your request."
    ok, _ = admit_content(b"<html><body>" + body.encode() + b"</body></html>", body, "text/html")
    assert ok is True


def test_non_html_is_never_a_challenge():
    ok, _ = admit_content(b"%PDF-1.5 ... datadome ...", "tiny pdf text", "application/pdf")
    assert ok is True


def test_missing_content_type_is_judged_as_html():
    ok, reason = admit_content(CF_CHALLENGE, "Just a moment Checking your browser", None)
    assert ok is False
    assert reason == "admission-challenge-page"


# ---- end-to-end wiring through extract_one ----------------------------------

def test_extract_one_rejects_challenge_page(tmp_path):
    root = tmp_path
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    url = "https://ex.test/blocked"
    raw_hash = sha256_hex(CF_CHALLENGE)
    write_raw_blob(root, raw_hash, CF_CHALLENGE)
    fr = FetchResult(
        url=url, decision="FETCH", status=200, etag=None, last_modified=None,
        raw_hash=raw_hash, raw_bytes=len(CF_CHALLENGE), fetched_at=now_utc(),
        error=None, content_type="text/html; charset=utf-8",
    )
    res = extract_one(fr, root=root, run_id="t-adm", conn=conn, crawler_version=CRAWLER_VERSION)
    assert res.ok is False
    assert res.reason == "admission-challenge-page"
    assert res.content_hash is None
    # The junk was never written to the corpus.
    assert not paths.md_path(root, "t-adm", url).exists()
