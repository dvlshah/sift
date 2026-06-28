"""API-as-content: the json-api extract strategy (find the content HTML field,
run it through the HTML extractor; pretty-print a pure data API)."""
import json

from sift.extract import (
    EXTRACTOR_VERSION_HTML,
    EXTRACTOR_VERSION_JSON,
    PRIMARY_STRATEGIES,
    _json_applies,
    extract_json,
    select_primary,
)
from sift.extract_strategy import ExtractInput

_BODY = ("<h2>VAT rates</h2><p>The standard VAT rate is 20%. Most goods and "
         "services are charged at this rate when you buy or sell them. </p>"
         "<p>Some goods and services qualify for a reduced rate of 5%, and a "
         "few are zero-rated. Check the detailed list before you file.</p>")


def _inp(raw=b'{"a": 1}', content_type=None, body_kind=None):
    return ExtractInput(raw=raw, url="https://x/api", content_type=content_type,
                        body_kind=body_kind)


class TestJsonRouting:
    def test_json_object_body_routes_to_json(self):
        assert select_primary(_inp(raw=b'{"a": 1}'), PRIMARY_STRATEGIES).kind == "json"

    def test_json_array_body_sniffs_json(self):
        assert _json_applies(_inp(raw=b'[1, 2, 3]'))

    def test_leading_whitespace_still_sniffs_json(self):
        assert _json_applies(_inp(raw=b'\n\t  {"a": 1}'))

    def test_routing_is_content_type_independent(self):
        # The re-extract path has no content_type — routing MUST key on the raw
        # blob, else a JSON row re-routes to HTML and breaks determinism.
        assert _json_applies(_inp(raw=b'{"a": 1}', content_type=None))
        assert select_primary(_inp(raw=b'{"a": 1}', content_type=None),
                              PRIMARY_STRATEGIES).kind == "json"

    def test_profile_body_kind_json_overrides(self):
        assert _json_applies(_inp(raw=b"not json at all", body_kind="json"))

    def test_html_not_routed_to_json(self):
        assert not _json_applies(_inp(raw=b"<html>hi</html>"))
        assert select_primary(_inp(raw=b"<html>hi</html>", content_type="text/html"),
                              PRIMARY_STRATEGIES).kind == "html"

    def test_profile_claiming_other_kind_blocks_json(self):
        assert not _json_applies(_inp(raw=b'{"a": 1}', body_kind="markdown"))


class TestJsonExtract:
    def test_html_content_field_extracted_metadata_excluded(self):
        body = json.dumps({
            "title": "VAT rates",
            "content_id": "noise-abc-123",
            "details": {"body": _BODY},
        }).encode()
        md, title = extract_json(body, "https://www.gov.uk/api/content/vat-rates")
        assert title == "VAT rates"
        assert md.startswith("# VAT rates")
        assert "standard VAT rate is 20%" in md
        assert "noise-abc-123" not in md  # only the content field, not metadata

    def test_all_html_fields_concatenated_not_just_largest(self):
        # Completeness: every HTML-bearing field is included, not only the largest
        # (the bug that silently dropped multi-part GOV.UK guide sections).
        summary = "<p>" + "This summary paragraph has plenty of words to survive. " * 4 + "</p>"
        body = json.dumps({
            "summary": summary,
            "details": {"body": _BODY},
        }).encode()
        md, _ = extract_json(body, "https://x/api")
        assert "standard VAT rate is 20%" in md        # the body field
        assert "summary paragraph has plenty" in md    # AND the summary (not dropped)

    def test_data_api_pretty_printed(self):
        body = json.dumps({"name": "rate table", "rate": 20, "unit": "percent"}).encode()
        md, title = extract_json(body, "https://x/api/rates.json")
        assert title == "rate table"
        assert "```json" in md and '"rate": 20' in md

    def test_deterministic(self):
        body = json.dumps({"title": "T", "details": {"body": _BODY}}).encode()
        a, _ = extract_json(body, "https://x/a")
        b, _ = extract_json(body, "https://x/a")
        assert a is not None and a == b

    def test_malformed_json_returns_none(self):
        assert extract_json(b"{not valid json", "https://x/api") == (None, None)

    def test_empty_object_returns_none(self):
        md, _ = extract_json(b"{}", "https://x/api")
        assert md is None

    def test_version_embeds_html_extractor(self):
        assert "json-v1" in EXTRACTOR_VERSION_JSON
        assert EXTRACTOR_VERSION_HTML in EXTRACTOR_VERSION_JSON
        assert EXTRACTOR_VERSION_JSON != EXTRACTOR_VERSION_HTML

    def test_multipart_html_fields_all_included(self):
        # GOV.UK-guide shape: each section in details.parts[]. Every part must
        # survive — the bug was keeping only the single largest field.
        p1 = "<h2>Part one</h2><p>" + "First section content here. " * 8 + "</p>"
        p2 = "<h2>Part two</h2><p>" + "Second section content here. " * 8 + "</p>"
        body = json.dumps({"title": "Guide", "details": {"parts": [
            {"title": "one", "body": p1}, {"title": "two", "body": p2}]}}).encode()
        md, _ = extract_json(body, "https://www.gov.uk/api/content/guide")
        assert "First section content" in md
        assert "Second section content" in md  # the 2nd part is NOT dropped

    def test_thin_html_field_does_not_leak_metadata(self):
        # An HTML field too thin to extract must not fall through to a JSON dump
        # of the metadata (content_id, analytics ids, ...).
        body = json.dumps({"title": "T", "content_id": "secret-meta-xyz",
                           "details": {"body": "<p>.</p>"}}).encode()
        md, _ = extract_json(body, "https://x/api")
        assert "secret-meta-xyz" not in (md or "")

    def test_bom_prefixed_json(self):
        raw = b"\xef\xbb\xbf" + json.dumps({"name": "rates", "v": 1}).encode()
        assert _json_applies(_inp(raw=raw))         # BOM doesn't defeat routing
        md, title = extract_json(raw, "https://x/api")
        assert md is not None and title == "rates"  # ...or json.loads
