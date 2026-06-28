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


def _inp(content_type="application/json", body_kind=None, raw=b"{}"):
    return ExtractInput(raw=raw, url="https://x/api", content_type=content_type,
                        body_kind=body_kind)


class TestJsonRouting:
    def test_json_content_type_routes_to_json(self):
        assert select_primary(_inp("application/json"), PRIMARY_STRATEGIES).kind == "json"

    def test_vendor_json_content_types(self):
        assert _json_applies(_inp("application/ld+json"))
        assert _json_applies(_inp("application/hal+json; charset=utf-8"))

    def test_profile_body_kind_json(self):
        assert _json_applies(_inp(content_type="text/plain", body_kind="json"))

    def test_html_not_routed_to_json(self):
        assert not _json_applies(_inp(content_type="text/html"))
        s = select_primary(_inp("text/html", raw=b"<html>hi</html>"), PRIMARY_STRATEGIES)
        assert s.kind == "html"

    def test_profile_claiming_other_kind_blocks_json(self):
        assert not _json_applies(_inp("application/json", body_kind="markdown"))


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

    def test_largest_html_field_wins(self):
        body = json.dumps({
            "summary": "<p>tiny summary</p>",
            "details": {"body": _BODY},
        }).encode()
        md, _ = extract_json(body, "https://x/api")
        assert "standard VAT rate is 20%" in md
        assert "tiny summary" not in md

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
