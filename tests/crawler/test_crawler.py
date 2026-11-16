"""The crawler stub: text extraction and the result it reports for the admin API."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "crawler"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "admin_api"))

import crawler as handler  # noqa: E402
import sources  # noqa: E402

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
HTML = """<html><head><title> My  Page! </title><style>a{}</style></head>
<body><h1>Hello</h1><p>One &amp; two</p><script>x()</script><div>three</div></body></html>"""


def test_extract_text_keeps_visible_text_and_title():
    title, text = handler.extract_text(HTML)
    assert title == "My Page!"
    assert text == "Hello\nOne & two\nthree"


def test_slugify_stays_within_the_admin_api_rules():
    assert handler.slugify("My Page!! / ünï") == "my-page-n"
    assert handler.slugify("!!!") == "page"
    assert len(handler.slugify("a" * 500)) == handler.MAX_SLUG_LENGTH


def test_crawl_reports_a_document_the_admin_api_accepts():
    request = {
        "source_id": SOURCE_ID,
        "crawl_id": "c",
        "url": "https://example.org/a",
        "bucket": "b",
        "prefix": f"web/{SOURCE_ID}/",
    }
    s3 = MagicMock()
    with patch.object(handler, "fetch_html", return_value=HTML):
        result = handler.crawl(request, s3)

    doc = result["documents"][0]
    assert sources.is_valid_key(doc["s3_key"], SOURCE_ID)
    assert s3.put_object.call_args.kwargs["Body"] == b"Hello\nOne & two\nthree"
    assert sources._validated_documents(SOURCE_ID, result["documents"])


def test_failure_is_reported_with_the_crawl_id():
    event = {"source_id": SOURCE_ID, "crawl_id": "c", "url": "ftp://x", "bucket": "b", "prefix": "p/"}
    with patch.object(handler, "report") as report, patch.object(handler.boto3, "client"):
        handler.lambda_handler(event, None)
    result = report.call_args.args[0]
    assert (result["status"], result["crawl_id"]) == ("failed", "c")
