"""Source crawler (stub): fetches one HTML page and stores its visible text.

Invoked asynchronously by the admin API with
    {"source_id", "crawl_id", "url", "bucket", "prefix"}
and reports back by invoking the admin API function directly with
    {"crawl_result": {...}}   (contract: sources.record_crawl_result)

It does not follow links, download PDFs or handle video yet. Everything it finds is
one `page` document, which is also the root. Needs only certifi (see requirements.txt).
"""

import json
import logging
import os
import re
import ssl
import urllib.request
from html.parser import HTMLParser

import boto3
import certifi

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The Lambda runtime's system CA bundle lacks the root behind internet2.edu's certificate
# (InCommon, signed by eMudhra emSign). certifi's Mozilla bundle has it.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

MAX_HTML_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20
MAX_SLUG_LENGTH = 150

SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "nav", "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}  # fmt: skip


class TextExtractor(HTMLParser):
    """Collects the visible text and the <title> of an HTML document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self._parts.append(data)

    def text(self):
        lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(self._parts).split("\n"))
        return "\n".join(line for line in lines if line)


def extract_text(html):
    """(title, text) of an HTML string."""
    parser = TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(parser.title.split()), parser.text()


def slugify(value):
    """Lowercase [a-z0-9-] only, within the length the admin API accepts."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:MAX_SLUG_LENGTH].strip("-")
    return slug or "page"


def fetch_html(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Only http and https URLs can be crawled")
    request = urllib.request.Request(url, headers={"User-Agent": "i2ccc-source-crawler/0.1"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
        content_type = response.headers.get_content_type()
        if content_type not in ("text/html", "application/xhtml+xml"):
            raise ValueError(f"Expected an HTML page, got {content_type}")
        body = response.read(MAX_HTML_BYTES + 1)
        if len(body) > MAX_HTML_BYTES:
            raise ValueError("Page is too large")
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def crawl(request, s3):
    """Fetches the page, uploads its text and returns the crawl_result payload."""
    source_id = request["source_id"]
    html = fetch_html(request["url"])
    title, text = extract_text(html)
    if not text:
        raise ValueError("The page has no text")

    # web/<source_id>/<source_id>__<slug>.txt: the key convention the admin API enforces
    key = f"{request['prefix']}{source_id}__{slugify(title or request['url'])}.txt"
    s3.put_object(Bucket=request["bucket"], Key=key, Body=text.encode("utf-8"), ContentType="text/plain")
    return {
        "source_id": source_id,
        "crawl_id": request["crawl_id"],
        "status": "success",
        "documents": [
            {"s3_key": key, "url": request["url"], "title": title or request["url"], "type": "page", "is_root": True}
        ],
    }


def report(result):
    boto3.client("lambda").invoke(
        FunctionName=os.environ["ADMIN_API_FUNCTION_NAME"],
        InvocationType="RequestResponse",
        Payload=json.dumps({"crawl_result": result}).encode(),
    )


def lambda_handler(event, context):
    try:
        result = crawl(event, boto3.client("s3"))
    except Exception as e:
        logger.exception("Crawl failed for %s", event.get("url"))
        result = {
            "source_id": event.get("source_id"),
            "crawl_id": event.get("crawl_id"),
            "status": "failed",
            "error": str(e) or type(e).__name__,
        }
    report(result)
    return {"status": result["status"]}
