"""API-as-source acquisition transport (PR1).

Covers the four load-bearing pieces:
  * ``CVEProfile.api_url`` routing — positive, adversarial, host-spoof safe.
  * the ``FetchInput`` transport seam — GET the API, keep the canonical url.
  * cross-process determinism of the json extract via the transport.
  * the seed-time robots gate on the *API* URL — a site that Disallows its API
    is skipped end-to-end through the real ``seed`` command.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from aiolimiter import AsyncLimiter
from click.testing import CliRunner

from sift import paths
from sift import sites as sites_mod
from sift.cli import main
from sift.fetch import FetchInput, fetch_all, fetch_one
from sift.manifest import iter_all, open_db
from sift.sites import SiteProfile
from sift.sites.cve import CVEProfile

CANON = "https://www.cve.org/CVERecord?id=CVE-2021-44228"
API = "https://cveawg.mitre.org/api/cve/CVE-2021-44228"


@pytest.fixture(autouse=True)
def _isolate_profile():
    """The CLI + the extract tests mutate the global profile singleton; snapshot
    and restore it so a CVE-routed test never leaks into the rest of the suite."""
    prev = sites_mod.current_profile()
    yield
    sites_mod.set_profile(prev)


class TestApiUrlMapping:
    def test_record_page_maps_to_services_api(self):
        assert CVEProfile().api_url(CANON) == API

    def test_trailing_slash_and_extra_params_ok(self):
        p = CVEProfile()
        assert (p.api_url("https://www.cve.org/CVERecord/?id=CVE-2024-3094")
                == "https://cveawg.mitre.org/api/cve/CVE-2024-3094")
        assert p.api_url("https://www.cve.org/CVERecord?id=CVE-2021-44228&utm=x") == API

    def test_lowercased_id_is_normalized_upper(self):
        # The API serves the canonical upper-case 'CVE-' form.
        assert CVEProfile().api_url("https://www.cve.org/CVERecord?id=cve-2021-44228") == API

    def test_non_record_urls_do_not_route(self):
        p = CVEProfile()
        for u in [
            "https://www.cve.org/CVERecord",                    # shell, no id
            "https://www.cve.org/CVERecord?id=CVE-1&id=CVE-2",   # ambiguous
            "https://www.cve.org/Search?q=log4j",                # not a record
            "https://www.cve.org/about/Metrics",                 # other page
        ]:
            assert p.api_url(u) is None, u

    def test_malicious_ids_rejected(self):
        p = CVEProfile()
        for bad in ["../../etc/passwd", "CVE-x", "DROP%20TABLE", "CVE-2021-", "CVE--1"]:
            assert p.api_url(f"https://www.cve.org/CVERecord?id={bad}") is None, bad

    def test_host_spoof_rejected(self):
        p = CVEProfile()
        assert p.api_url("https://www.cve.org.evil.test/CVERecord?id=CVE-2021-44228") is None
        assert p.api_url("https://evilwww.cve.org/CVERecord?id=CVE-2021-44228") is None

    def test_default_profile_never_routes(self):
        # Opt-in: a profile that doesn't override stays HTML-only.
        assert SiteProfile().api_url(CANON) is None


class TestFetchTransport:
    def test_target_url_defaults_to_canonical(self):
        inp = FetchInput(url=CANON, decision="FETCH", etag=None, last_modified=None)
        assert inp.target_url == CANON  # no api_url -> fetch the page itself

    def test_target_url_is_api_when_routed(self):
        inp = FetchInput(url=CANON, decision="FETCH", etag=None,
                         last_modified=None, fetch_url=API)
        assert inp.url == CANON       # manifest + citation stay canonical
        assert inp.target_url == API  # but the GET targets the API


class TestApiExtractDeterminism:
    """Re-extracting the stored API JSON must be byte-identical and route to json
    on BOTH the fresh (content_type set) and re-extract (content_type None) paths,
    so the content_hash is stable cross-process."""

    def _extract(self, raw, content_type):
        from sift.extract import PRIMARY_STRATEGIES, select_primary
        from sift.extract_strategy import ExtractInput
        inp = ExtractInput(
            raw=raw, url=CANON, content_type=content_type,
            body_kind=sites_mod.current_profile().body_kind(CANON, content_type=content_type),
        )
        strat = select_primary(inp, PRIMARY_STRATEGIES)
        return strat.kind, strat.extract(raw, CANON)

    def test_cve_record_extracts_deterministically(self):
        raw = (Path(__file__).parent / "fixtures" / "cve_log4shell.json").read_bytes()
        sites_mod.set_profile(CVEProfile())
        k1, (md1, t1) = self._extract(raw, "application/json")  # fresh fetch
        k2, (md2, t2) = self._extract(raw, None)                # re-extract, no CT
        assert k1 == k2 == "json"   # raw-byte sniff routes both paths
        assert md1 == md2           # byte-identical -> stable content_hash
        assert t1 == t2 == (
            "Apache Log4j2 JNDI features do not protect against attacker "
            "controlled LDAP and other JNDI related endpoints"
        )  # nested containers.cna.title, not the 'CVERecord' url slug
        assert "Log4j2" in md1      # the vulnerability description is indexed


class TestSeedRobotsApiGate:
    """Seed honors robots on the URL it will actually fetch (the API), not just
    the canonical page — so a site that Disallows its API is skipped."""

    def _seed(self, tmp_path, robots):
        (tmp_path / "src.json").write_text(json.dumps({"links": [CANON]}))
        (tmp_path / "sift.toml").write_text(
            '[site]\nprofile = "sift.sites.cve:CVEProfile"\n'
            '[seed]\nhost_allow = ["www.cve.org"]\n'
        )
        with respx.mock(assert_all_called=False) as router:
            for host, body in robots.items():
                router.get(f"https://{host}/robots.txt").mock(
                    return_value=httpx.Response(200, text=body))
            return CliRunner().invoke(main, [
                "seed", "--root", str(tmp_path),
                "--config", str(tmp_path / "sift.toml"),
                "--from-json", str(tmp_path / "src.json"),
            ])

    def _manifest_urls(self, tmp_path):
        conn = open_db(paths.manifest_path(tmp_path))
        try:
            return [r.url for r in iter_all(conn)]
        finally:
            conn.close()

    def test_api_disallowed_skips_the_row(self, tmp_path):
        # cve.org allows the page, but cveawg Disallows /api/ -> skip the row.
        res = self._seed(tmp_path, {
            "www.cve.org": "User-agent: *\nAllow: /\n",
            "cveawg.mitre.org": "User-agent: *\nDisallow: /api/\n",
        })
        assert res.exit_code == 0, res.output
        assert self._manifest_urls(tmp_path) == []  # nothing admitted

    def test_api_allowed_seeds_canonical_url(self, tmp_path):
        res = self._seed(tmp_path, {
            "www.cve.org": "User-agent: *\nAllow: /\n",
            "cveawg.mitre.org": "User-agent: *\nAllow: /\n",
        })
        assert res.exit_code == 0, res.output
        assert self._manifest_urls(tmp_path) == [CANON]  # canonical url admitted


# ---- the real fetch_one path (SSRF guard + escalation interaction) ----------

ALLOW = frozenset({"www.cve.org"})  # seed host_allow = the canonical host only


class _FakeImpersonate:
    """Records whether the escalation ladder was entered."""
    escalate_statuses = (403, 429, 503)

    def __init__(self):
        self.calls = 0

    async def fetch(self, inp, root, *, allowed_hosts=None):
        self.calls += 1
        return None  # 'pool could not serve' — the native failure then stands


async def _fetch_api_row(tmp_path, *, impersonate_pool=None):
    inp = FetchInput(url=CANON, decision="FETCH", etag=None,
                     last_modified=None, fetch_url=API)
    async with httpx.AsyncClient() as client:
        return await fetch_one(
            client, inp, tmp_path, AsyncLimiter(1000, 1), asyncio.Semaphore(8),
            retries=0, impersonate_pool=impersonate_pool, allowed_hosts=ALLOW,
            thin_text_threshold=500,
        )


@respx.mock
async def test_api_fetch_stores_body_from_declared_cross_host_api(tmp_path):
    # The GET targets the API (cveawg); the SSRF guard allows that declared host
    # though it's off the seed allow-list; the row keeps the canonical cve.org url.
    raw = (Path(__file__).parent / "fixtures" / "cve_log4shell.json").read_bytes()
    route = respx.get(API).mock(return_value=httpx.Response(
        200, content=raw, headers={"content-type": "application/json"}))
    res = await _fetch_api_row(tmp_path)
    assert route.called                       # the API was fetched, not the page
    assert res.status == 200 and res.raw_hash  # body stored despite off-allowlist host
    assert res.url == CANON                    # citation stays canonical


@respx.mock
async def test_api_block_is_native_only_no_shell_fallback(tmp_path):
    # A 403 from the API must NOT escalate — escalation re-fetches the cve.org
    # SHELL and would index it. The failure surfaces; no body is stored.
    respx.get(API).mock(return_value=httpx.Response(403, text="blocked"))
    imp = _FakeImpersonate()
    res = await _fetch_api_row(tmp_path, impersonate_pool=imp)
    assert imp.calls == 0        # api rows null the escalation tiers
    assert res.raw_hash is None  # the shell was never fetched/stored
    assert res.status == 403


@respx.mock
async def test_api_redirect_off_declared_hosts_rejected(tmp_path):
    # Trusting the declared API host must not open a redirect hole: a hop to a
    # third host is still rejected by the SSRF guard.
    respx.get(API).mock(return_value=httpx.Response(
        302, headers={"location": "https://evil.test/x"}))
    respx.get("https://evil.test/x").mock(
        return_value=httpx.Response(200, content=b'{"x":1}'))
    res = await _fetch_api_row(tmp_path)
    assert res.raw_hash is None
    assert "redirect-off-allowlist:evil.test" in (res.error or "")


class _BrowserAndApiProfile(SiteProfile):
    """Pathological: claims a URL needs the browser AND exposes an api_url. The
    api transport must win (native fetch of the API), never the browser shell —
    else the no-escalation + SSRF guards would be silently bypassed."""

    def requires_browser(self, url):
        return True

    def api_url(self, url):
        return API if url == CANON else None


@respx.mock
async def test_api_url_takes_precedence_over_requires_browser(tmp_path):
    # browser_pool=None: if the api row were (wrongly) routed to the browser
    # path, fetch_all would raise "browser rendering ... no BrowserPool". It must
    # not — the api row is forced native to the API.
    raw = (Path(__file__).parent / "fixtures" / "cve_log4shell.json").read_bytes()
    respx.get(API).mock(return_value=httpx.Response(
        200, content=raw, headers={"content-type": "application/json"}))
    log = tmp_path / "fetch.log"
    inp = FetchInput(url=CANON, decision="FETCH", etag=None,
                     last_modified=None, fetch_url=API)
    count = await fetch_all([inp], tmp_path, log, profile=_BrowserAndApiProfile(),
                            browser_pool=None, allowed_hosts=ALLOW)
    assert count == 1
    rec = json.loads(log.read_text().strip())
    assert rec["url"] == CANON            # citation stays canonical
    assert rec["browser_version"] is None  # NOT browser-rendered
    assert rec["raw_hash"]                 # the API JSON was stored
