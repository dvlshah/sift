"""curl_cffi impersonate-target rotation.

A block status (403/429/503) retries a diverse fingerprint before escalating —
a 403 often clears on a different TLS profile. A thin 200 (JS-challenge shell)
escalates immediately, since a different fingerprint can't render JS and we must
not re-hammer a host that already served a page.
"""
import asyncio

import pytest

from sift.config import ImpersonateConfig
from sift.fetch import FetchInput
from sift.sources.impersonate import CurlCffiScrapePool, EscalateError

GOOD = b"<html><body>" + b"real documentation content here. " * 60 + b"</body></html>"
THIN = b"<html><body>hi</body></html>"


class _Resp:
    def __init__(self, status, body=GOOD, url="https://ex.test/", ct="text/html"):
        self.status_code = status
        self.content = body
        self.url = url
        self.headers = {"content-type": ct}


def _pool(fallbacks=("chrome124", "safari17_0")):
    cfg = ImpersonateConfig(
        enabled=True, impersonate="chrome", impersonate_fallbacks=fallbacks,
        thin_text_threshold=500, rate_per_sec=1000.0, concurrency=4,
    )
    return CurlCffiScrapePool(cfg)


def _patch(pool, mapping):
    """Replace _get with a fake mapping target -> _Resp|None; record try order."""
    tried = []

    def fake_get(url, target):
        tried.append(target)
        return mapping.get(target)

    pool._get = fake_get
    return tried


def _fetch(pool, tmp_path):
    inp = FetchInput(url="https://ex.test/x", decision="FETCH", etag=None, last_modified=None)
    return asyncio.run(pool.fetch(inp, tmp_path, allowed_hosts=None))


def test_rotation_recovers_on_fallback(tmp_path):
    pool = _pool()
    tried = _patch(pool, {"chrome": _Resp(403), "chrome124": _Resp(200)})
    fr = _fetch(pool, tmp_path)
    assert fr.status == 200
    assert tried == ["chrome", "chrome124"]  # rotated to the first fallback
    assert pool.calls_succeeded == 1


def test_rotation_exhausts_then_escalates(tmp_path):
    pool = _pool()
    tried = _patch(pool, {"chrome": _Resp(403), "chrome124": _Resp(403), "safari17_0": _Resp(403)})
    with pytest.raises(EscalateError):
        _fetch(pool, tmp_path)
    assert tried == ["chrome", "chrome124", "safari17_0"]  # tried all, then gave up


def test_no_rotation_on_thin(tmp_path):
    pool = _pool()
    tried = _patch(pool, {"chrome": _Resp(200, body=THIN)})
    with pytest.raises(EscalateError, match="still-thin"):
        _fetch(pool, tmp_path)
    assert tried == ["chrome"]  # thin -> escalate immediately, no fallbacks


def test_first_target_success_no_rotation(tmp_path):
    pool = _pool()
    tried = _patch(pool, {"chrome": _Resp(200)})
    fr = _fetch(pool, tmp_path)
    assert fr.status == 200
    assert tried == ["chrome"]  # success on the first target, fast path


def test_non_block_status_does_not_rotate(tmp_path):
    pool = _pool()
    tried = _patch(pool, {"chrome": _Resp(404)})
    with pytest.raises(EscalateError, match="http-404"):
        _fetch(pool, tmp_path)
    assert tried == ["chrome"]  # 404 is a real not-found, not a block


def test_empty_fallbacks_disables_rotation(tmp_path):
    pool = _pool(fallbacks=())
    tried = _patch(pool, {"chrome": _Resp(403)})
    with pytest.raises(EscalateError):
        _fetch(pool, tmp_path)
    assert tried == ["chrome"]  # rotation disabled
