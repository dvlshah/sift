"""Recursive link frontier: extract_links + LinkFrontierSource."""
from sift import paths
from sift.fetch import store_body
from sift.manifest import init_schema, now_utc, open_db, transaction, upsert_seed
from sift.sources.frontier import LinkFrontierSource, extract_links

HOSTS = frozenset({"ex.test"})


class TestExtractLinks:
    def test_in_scope_links_extracted(self):
        html = b'<html><body><a href="/a">A</a><a href="/sub/b">B</a></body></html>'
        got = extract_links(html, "https://ex.test/page", HOSTS)
        assert "https://ex.test/a" in got
        assert any(u.endswith("/sub/b") for u in got)

    def test_off_host_excluded(self):
        html = b'<a href="https://off.test/x">x</a><a href="//cdn.other/y">y</a>'
        assert extract_links(html, "https://ex.test/", HOSTS) == set()

    def test_non_html_yields_nothing(self):
        assert extract_links(b"%PDF-1.4 stuff", "https://ex.test/", HOSTS) == set()
        assert extract_links(b'{"a": 1}', "https://ex.test/", HOSTS) == set()

    def test_non_navigational_hrefs_skipped(self):
        html = (b'<a href="#frag">f</a><a href="mailto:x@y.z">m</a>'
                b'<a href="javascript:void(0)">j</a><a href="/real">r</a>')
        assert extract_links(html, "https://ex.test/", HOSTS) == {"https://ex.test/real"}


def _fetched_root(tmp_path, html, url="https://ex.test/page"):
    conn = open_db(paths.manifest_path(tmp_path))
    init_schema(conn)
    rh, _ = store_body(tmp_path, html)
    with transaction(conn):
        upsert_seed(conn, url, "LIVING", None, "cv", None, now_utc())
    conn.execute("UPDATE manifest SET raw_hash=?, state='FRESH' WHERE url=?", (rh, url))
    conn.commit()
    conn.close()
    return tmp_path


class TestLinkFrontierSource:
    def test_discovers_new_in_scope_urls(self, tmp_path):
        html = b'<a href="/a">A</a><a href="/b">B</a><a href="https://off.test/x">o</a>'
        root = _fetched_root(tmp_path, html)
        got = {u for u, _ in LinkFrontierSource(root, allowed_hosts=["ex.test"], max_urls=10).discover()}
        assert got == {"https://ex.test/a", "https://ex.test/b"}

    def test_already_seeded_url_not_re_yielded(self, tmp_path):
        html = b'<a href="https://ex.test/page">self</a><a href="/new">n</a>'
        root = _fetched_root(tmp_path, html)
        got = {u for u, _ in LinkFrontierSource(root, allowed_hosts=["ex.test"], max_urls=10).discover()}
        assert got == {"https://ex.test/new"}

    def test_max_urls_bounds_output(self, tmp_path):
        html = b"".join(f'<a href="/p{i}">{i}</a>'.encode() for i in range(20))
        root = _fetched_root(tmp_path, html)
        got = list(LinkFrontierSource(root, allowed_hosts=["ex.test"], max_urls=5).discover())
        assert len(got) == 5

    def test_second_pass_harvests_nothing(self, tmp_path):
        root = _fetched_root(tmp_path, b'<a href="/a">A</a>')
        first = list(LinkFrontierSource(root, allowed_hosts=["ex.test"], max_urls=10).discover())
        assert first  # first pass found a link
        second = list(LinkFrontierSource(root, allowed_hosts=["ex.test"], max_urls=10).discover())
        assert second == []  # harvest-state file records the blob as drained

    def test_empty_allowed_hosts_yields_nothing(self, tmp_path):
        root = _fetched_root(tmp_path, b'<a href="/a">A</a>')
        assert list(LinkFrontierSource(root, allowed_hosts=[], max_urls=10).discover()) == []
