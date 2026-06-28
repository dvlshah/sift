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
