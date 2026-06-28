"""RobotsGate: respect robots.txt Disallow at seed time."""

import httpx
import respx

from sift.robots import RobotsGate

ROBOTS = """\
User-agent: *
Disallow: /private/
Allow: /
"""


@respx.mock
def test_disallowed_path_blocked():
    respx.get("https://ex.test/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS))
    gate = RobotsGate("sift-test")
    assert gate.allowed("https://ex.test/public/page")
    assert not gate.allowed("https://ex.test/private/secret")


@respx.mock
def test_missing_robots_allows_all():
    # 404 -> no rules -> allow (standard semantics).
    respx.get("https://ex.test/robots.txt").mock(return_value=httpx.Response(404))
    gate = RobotsGate("sift-test")
    assert gate.allowed("https://ex.test/anything")


@respx.mock
def test_unreachable_robots_allows():
    # A flaky/unreachable robots.txt must not halt the crawl.
    respx.get("https://ex.test/robots.txt").mock(
        side_effect=httpx.ConnectError("boom"))
    gate = RobotsGate("sift-test")
    assert gate.allowed("https://ex.test/anything")


@respx.mock
def test_robots_fetched_once_per_origin():
    route = respx.get("https://ex.test/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS))
    gate = RobotsGate("sift-test")
    gate.allowed("https://ex.test/a")
    gate.allowed("https://ex.test/b")
    gate.allowed("https://ex.test/private/c")
    assert route.call_count == 1  # cached per origin


def test_unparseable_url_is_allowed():
    # No scheme/host -> not ours to block here (host_allow handles those).
    gate = RobotsGate("sift-test")
    assert gate.allowed("not-a-url")


@respx.mock
def test_server_error_disallows():
    # 5xx -> server signals unavailable -> complete disallow (RFC 9309), so we
    # back off rather than crawl through the signal.
    respx.get("https://ex.test/robots.txt").mock(return_value=httpx.Response(503))
    gate = RobotsGate("sift-test")
    assert not gate.allowed("https://ex.test/anything")


@respx.mock
def test_rate_limited_disallows():
    # 429 -> overloaded -> treat as disallow, same as 5xx.
    respx.get("https://ex.test/robots.txt").mock(return_value=httpx.Response(429))
    gate = RobotsGate("sift-test")
    assert not gate.allowed("https://ex.test/anything")


@respx.mock
def test_bom_prefixed_robots_is_enforced():
    # Some IIS origins serve a UTF-8 BOM. Decoding as plain utf-8 leaves the BOM
    # glued to the first "User-agent:" line, RobotFileParser drops the group, and
    # the Disallow silently stops applying. utf-8-sig must strip it.
    body = ("﻿" + ROBOTS).encode("utf-8")
    respx.get("https://ex.test/robots.txt").mock(
        return_value=httpx.Response(200, content=body))
    gate = RobotsGate("sift-test")
    assert not gate.allowed("https://ex.test/private/secret")
    assert gate.allowed("https://ex.test/public/page")


@respx.mock
def test_empty_200_allows_all():
    # A 200 with an empty body == no rules -> allow.
    respx.get("https://ex.test/robots.txt").mock(return_value=httpx.Response(200, text=""))
    gate = RobotsGate("sift-test")
    assert gate.allowed("https://ex.test/anything")


@respx.mock
def test_prewarm_populates_cache_without_later_fetches():
    route = respx.get("https://ex.test/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS))
    gate = RobotsGate("sift-test")
    # prewarm dedupes by origin: 3 URLs, same origin -> 1 fetch.
    gate.prewarm([
        "https://ex.test/a",
        "https://ex.test/private/b",
        "https://ex.test/c",
    ])
    assert route.call_count == 1
    # Subsequent checks hit the warm cache -> still 1 fetch total.
    assert gate.allowed("https://ex.test/public/page")
    assert not gate.allowed("https://ex.test/private/secret")
    assert route.call_count == 1
