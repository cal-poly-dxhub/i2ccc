"""Discover ingestible content (videos, slide decks, page text) on Internet2 web pages.

There are three entry points, from simplest to most involved:

    scrape(url)       -> ScrapeResult  One page: what to ingest from it -- its text, plus any
                                       video/document links on it (see scrape()).
    find_urls(url)    -> list[str]     One page: every important URL it links to.
    crawl(url, depth) -> list[str]     Many pages: follows find_urls() breadth-first to a depth.
    crawl_for_ingest(url, depth)       Like crawl(), but returns a CrawlResult for every URL found,
                      -> list[CrawlResult]  saying what kind of ingestion it needs.

Key ideas used throughout:

  * Assets are direct links to things we ingest: YouTube videos, Panopto recordings, and Google
    Drive/Docs files. url_classifier.classify_url() recognises them. Assets are never fetched or
    crawled into.
  * Important links: when finding URLs, we only look at the page's main content area (not menus,
    headers, or footers) and drop navigation links such as the homepage and breadcrumbs.
    See "Link filtering" below.

The file is laid out top-down: public API first, then the helpers it's built from.
"""
import argparse
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config as config_module
from .config import Config
from .logs import setup_logging
from .progress import NULL_PROGRESS, Progress
from .netguard import (  # noqa: F401  (UnsafeURLError re-exported)
    GuardedAdapter, UnsafeURLError, domain_allowed, validate_start_url)
from .url_classifier import (  # asset types re-exported for callers using scraper.YOUTUBE etc.
    FILE, GOOGLE_DRIVE, PANOPTO, YOUTUBE, asset_id, classify_url, extension_for_mime,  # noqa: F401
    get_domain, is_youtube_host)

# HTTP statuses retried as temporary failures (see Config.retries).
RETRY_STATUSES = (429, 500, 502, 503, 504)

log = logging.getLogger(__name__)


# =============================================================================================
# Results
# =============================================================================================

# Values for Asset.type / ScrapeResult.type / CrawlResult.type: the asset types YOUTUBE, PANOPTO,
# and GOOGLE_DRIVE (from url_classifier), plus:
PAGE = 'page'            # a page with video/document links: ingest its text and its links
PAGE_TEXT = 'page_text'  # a page with no video/document links: ingest its text
FAILED = 'failed'        # crawl_for_ingest() couldn't load the page; see CrawlResult.error
NOT_FETCHED = 'not_fetched'  # crawl_for_ingest() stopped (max_pages / time_limit) before
                             # reading it; see CrawlResult.error. Crawl again to get it.


@dataclass
class Asset:
    """A video/document link found on a page."""
    url: str
    type: str          # YOUTUBE, PANOPTO, GOOGLE_DRIVE, or FILE
    title: str = ''    # link text, e.g. "Slides"
    context: str = ''  # what the link is for: nearest heading + its table row/paragraph text


@dataclass
class ScrapeResult:
    """What scrape() found at a URL."""
    url: str
    type: str          # PAGE, PAGE_TEXT, or an asset type if the URL is a direct video/document link
    title: str = ''
    text: str = ''     # page text to ingest; empty when there is none to ingest
    links: list[Asset] = field(default_factory=list)
    html: str = ''     # the page's raw HTML, kept for debugging but not saved


@dataclass
class CrawlResult:
    """One URL found by crawl_for_ingest(), and the ingestion it needs (`type`):

      * YOUTUBE / PANOPTO / GOOGLE_DRIVE / FILE -- download and ingest the video/document. A
                    link is FILE by its extension, or -- for one like "/download?id=7" -- because
                    fetching it as a page got a document Content-Type (e.g. application/pdf)
      * PAGE_TEXT -- a page without documents: ingest `text`
      * PAGE      -- a page with documents: ingest `text` (unless text_with_links=False
                     left it empty); its documents are listed separately, found_on pointing to it
      * FAILED    -- the page couldn't be loaded; `error` says why
      * NOT_FETCHED -- the crawl stopped (max_pages or time_limit) before reading this page
    """
    url: str
    type: str
    depth: int                       # 0 for the start URL
    found_on: Optional[str] = None   # URL of the page that linked here (None for the start URL)
    title: str = ''                  # page title, or link text for an asset
    context: str = ''                # assets: nearest heading above the link (e.g. session name)
    text: str = ''                   # page text to ingest
    html: str = ''                   # pages: the raw HTML, kept for debugging but not saved
    error: str = ''
    key: str = ''                    # pages: store key, when on_page stored the content
    links: Optional[list[str]] = None  # pages: URLs of the documents/videos on it (None: not a
                                       # page that was read, or cleared once on_page stored it)


# =============================================================================================
# Public API
# =============================================================================================

def scrape(url: str, text_with_links: bool = True,
           config: Optional[Config] = None) -> ScrapeResult:
    """Fetch one page and decide what to ingest from it:

      * direct video/document link -> type is the asset type; not fetched, ingest it as-is
      * page with video/document links -> type PAGE; ingest `text` and `links`
      * page without any -> type PAGE_TEXT; ingest `text`

    Every web page's own text is ingested -- e.g. the Cloud Forum session abstracts next to their
    slides. text_with_links=False leaves `text` and `html` empty for PAGE results, so pages
    with documents are ingested only through their documents (the admin-UI mockup reads that
    way). `html` is the page's raw HTML, kept for debugging but not saved to the store.
    """
    kind = classify_url(url)
    if kind is not None:
        return ScrapeResult(url=url, type=kind)
    validate_start_url(url, config)
    response = fetch(url, config=config)
    return _scrape_soup(_parse(response.content), response.url, text_with_links,
                        _html_of(response))


def scrape_html(html: str | bytes, url: str, text_with_links: bool = True) -> ScrapeResult:
    """scrape() for HTML you already have. url is used to resolve relative links."""
    soup = _parse(html)
    if isinstance(html, bytes):
        html = html.decode(soup.original_encoding or 'utf-8', errors='replace')
    return _scrape_soup(soup, url, text_with_links, html)


def _scrape_soup(soup: BeautifulSoup, url: str, text_with_links: bool,
                 html: str) -> ScrapeResult:
    title = _clean_text(soup.title.get_text()) if soup.title else ''
    links = extract_assets(soup, url)
    if not links:
        return ScrapeResult(url=url, type=PAGE_TEXT, title=title, text=extract_text(soup),
                            html=html)
    if not text_with_links:
        return ScrapeResult(url=url, type=PAGE, title=title, links=links)
    return ScrapeResult(url=url, type=PAGE, title=title, text=extract_text(soup), links=links,
                        html=html)


def find_urls(url: str, important_only: bool = True,
              session: Optional[requests.Session] = None,
              config: Optional[Config] = None) -> list[str]:
    """Fetch one page and return the URLs it links to (see extract_urls).

    Direct video/document links and non-HTML responses (e.g. PDFs) have nothing to follow, so
    they return [] -- this keeps a crawler from wandering into YouTube/Panopto/Drive pages.
    """
    if classify_url(url) is not None:
        return []
    validate_start_url(url, config)
    response = fetch(url, session, config=config)
    if not _is_html(response):
        return []
    return extract_urls(response.content, response.url, important_only)


def extract_urls(html: str | bytes, page_url: str, important_only: bool = True) -> list[str]:
    """find_urls() for HTML you already have: absolute http(s) URLs, deduplicated, in page order.

    important_only (default): only links in the page's main content, minus navigation links
    (see is_unimportant). important_only=False: every link on the page.
    """
    return _urls_in_soup(_parse(html), page_url, important_only)


def _urls_in_soup(soup: BeautifulSoup, page_url: str, important_only: bool) -> list[str]:
    root = _content_root(soup) if important_only else soup
    urls: dict[str, None] = {}  # dict as an ordered set
    for _el, url in _iter_links(root, _base_url(soup, page_url)):
        if important_only and is_unimportant(url, page_url):
            continue
        urls[url] = None
    return list(urls)


def crawl(start_url: str, max_depth: int = 1,
          allowed_domains: Optional[Iterable[str]] = None,
          found: Optional[set[str]] = None,
          important_only: bool = True,
          max_pages: Optional[int] = None,
          delay: Optional[float] = None,
          config: Optional[Config] = None) -> list[str]:
    """Breadth-first crawl from start_url. Returns the new URLs found, in discovery order.

    Depth: start_url is depth 0, links on it are depth 1, and so on. Pages at depth < max_depth
    are fetched; URLs at max_depth are recorded but not fetched. So max_depth=0 returns just
    start_url, and max_depth=1 returns start_url plus the links on it.

    allowed_domains: a URL is only returned/fetched if its host is one of these or a subdomain
    (``internet2.edu`` covers ``www.internet2.edu`` and ``spaces.at.internet2.edu``). Defaults to
    start_url's domain. Assets (videos/documents) are returned whatever their domain, but never
    fetched.

    found: URLs already found, in url_key() form. These are skipped, and every URL this crawl finds
    is added, so sharing one set across several crawls avoids repeats. start_url is crawled even
    if it's already in found (it just isn't returned again).

    Pages that fail to load (404, WAF challenge, timeout, ...) are logged and skipped. At most
    max_pages pages are fetched, waiting `delay` seconds between requests (defaults: the
    config's max_pages and crawl_delay).

    start_url must pass netguard.validate_start_url() (http(s), an allowed domain, a public
    address), else UnsafeURLError is raised. Every request, redirects included, is refused if it
    would reach a private address (config.block_private_addresses).
    """
    results = _crawl(start_url, max_depth, allowed_domains, found, important_only, max_pages,
                     delay, fetch_last_level=False, text_with_links=False, config=config)
    return [r.url for r in results]


def crawl_for_ingest(start_url: str, max_depth: int = 1,
                     allowed_domains: Optional[Iterable[str]] = None,
                     found: Optional[set[str]] = None,
                     important_only: bool = True,
                     text_with_links: bool = True,
                     max_pages: Optional[int] = None,
                     delay: Optional[float] = None,
                     config: Optional[Config] = None,
                     time_limit: Optional[float] = None,
                     on_page: Optional[Callable[[CrawlResult], None]] = None,
                     progress: Progress = NULL_PROGRESS,
                     ) -> list[CrawlResult]:
    """crawl(), but returns a CrawlResult per URL saying what ingestion it needs.

    Crawls the same pages as crawl(). The difference is that pages at max_depth are fetched too,
    because a page's type -- PAGE or PAGE_TEXT -- is only known once it's been read. Their page
    links still aren't followed, but their documents (videos/Drive files) are returned, so every
    PAGE result's documents are in the list. Failed pages are returned with type FAILED instead
    of being dropped. text_with_links is as for scrape().

    Limits: the crawl stops after max_pages pages, or once time_limit seconds have passed
    (checked before each request, so allow for one request's timeout and retries on top)
    (defaults: the config's max_pages and crawl_time_limit; 0 = no time limit) -- so a caller
    with a hard deadline (a Lambda) can finish cleanly. Pages it didn't get to are returned as
    NOT_FETCHED, so a cut-short crawl is visible rather than silently incomplete.

    on_page: called with each page as soon as it's read, text and html included. The page's
    text and html are then dropped from its result (set to ''), so the returned list stays
    small however many pages are crawled -- the callback is expected to have stored them
    (it may set result.key to say where). See downloader.crawl_into_store().
    """
    return _crawl(start_url, max_depth, allowed_domains, found, important_only, max_pages,
                  delay, fetch_last_level=True, text_with_links=text_with_links, config=config,
                  time_limit=time_limit, on_page=on_page, progress=progress)


def _crawl(start_url: str, max_depth: int, allowed_domains: Optional[Iterable[str]],
           found: Optional[set[str]], important_only: bool, max_pages: Optional[int],
           delay: Optional[float], fetch_last_level: bool, text_with_links: bool,
           config: Optional[Config], time_limit: Optional[float] = None,
           on_page: Optional[Callable[[CrawlResult], None]] = None,
           progress: Progress = NULL_PROGRESS) -> list[CrawlResult]:
    """The breadth-first crawl behind crawl() and crawl_for_ingest()."""
    config = config or config_module.default()
    max_pages = config.max_pages if max_pages is None else max_pages
    delay = config.crawl_delay if delay is None else delay
    time_limit = config.crawl_time_limit if time_limit is None else time_limit
    started = time.monotonic()
    if classify_url(start_url) is None:  # a page, so it will be fetched
        validate_start_url(start_url, config)
    allowed = {d.lower().removeprefix('www.') for d in (allowed_domains or [get_domain(start_url)])}
    found = set() if found is None else found
    results: list[CrawlResult] = []
    queue: deque[CrawlResult] = deque()  # pages waiting to be fetched

    def add(result: CrawlResult) -> None:
        """Record a newly found URL, and queue it if it's a page."""
        key = url_key(result.url)
        if result.type == PAGE and (is_youtube_host(result.url)
                                    or not _domain_allowed(result.url, allowed)):
            return  # off-site, or a YouTube page (never crawled, whatever the allowed domains)
        progress.discovered(result.url, result.type, parent=result.found_on or '',
                            title=result.title, context=result.context)
        if key in found:
            return
        found.add(key)
        results.append(result)
        if result.type == PAGE:
            queue.append(result)

    start = CrawlResult(url=start_url, type=classify_url(start_url) or PAGE, depth=0)
    if url_key(start_url) in found:
        if start.type == PAGE:
            queue.append(start)  # crawl it anyway; it just isn't returned again
    else:
        add(start)

    last_level = max_depth if fetch_last_level else max_depth - 1
    pages_fetched = 0
    with new_session(config) as session:
        while queue:
            page = queue.popleft()
            if page.depth > last_level:
                continue
            stop = ''
            if pages_fetched >= max_pages:
                stop = f'the crawl reached max_pages={max_pages}'
            elif time_limit and time.monotonic() - started + (delay if pages_fetched else 0) \
                    >= time_limit:
                stop = f'the crawl reached its time limit ({time_limit:g} s)'
            if stop:
                unread = [page] + [p for p in queue if p.depth <= last_level]
                log.warning('Stopping: %s; %d page(s) not read', stop, len(unread))
                if fetch_last_level:  # crawl_for_ingest(): say which pages weren't read
                    for p in unread:
                        p.type, p.error = NOT_FETCHED, f'Not read: {stop}'
                        progress.discovery(p.url, 'deferred', error=p.error, reason='limit')
                        progress.content(p.url, 'deferred', error=p.error, reason='limit')
                        found.discard(url_key(p.url))  # so a later crawl sharing `found` reads it
                break
            if pages_fetched and delay:
                time.sleep(delay)
            pages_fetched += 1
            progress.discovery(page.url, 'running')

            try:
                response = fetch(page.url, session, html_only=True, config=config)
            except NotHTML as exc:
                if extension_for_mime(exc.content_type):
                    log.info('depth %d: %s is a file (%s)', page.depth, page.url, exc.content_type)
                    page.type = FILE
                    progress.discovery(page.url, 'not_applicable')
                else:
                    log.warning('Skipping %s: %s', page.url, exc)
                    page.type, page.error = FAILED, str(exc)
                    progress.discovery(page.url, 'failed', error=page.error, reason='unsupported')
                    progress.content(page.url, 'failed', error=page.error, reason='unsupported')
                continue
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                log.warning('Skipping %s: %s', page.url, exc)
                page.type, page.error = FAILED, str(exc)
                progress.discovery(page.url, 'failed', error=page.error, reason='fetch_error')
                progress.content(page.url, 'failed', error=page.error, reason='fetch_error')
                continue

            soup = _parse(response.content)
            scraped = _scrape_soup(soup, response.url, text_with_links, _html_of(response))
            page.type, page.title, page.text = scraped.type, scraped.title, scraped.text
            page.html = scraped.html
            page.links = [a.url for a in scraped.links]
            if on_page is not None:
                on_page(page)
                page.text = page.html = ''  # stored by on_page; keep the results small
                page.links = None
            assets = {a.url: a for a in scraped.links}
            if page.depth >= max_depth:
                # Read only for its type: don't follow its page links. Do report its documents,
                # though -- a PAGE result means "ingest its links", and they cost no requests.
                links = []
            else:
                links = _urls_in_soup(soup, response.url, important_only)
            log.info('depth %d: %s -> %d links, %d documents', page.depth, page.url, len(links),
                     len(assets))
            for url in links + [a for a in assets if a not in links]:
                asset = assets.get(url)
                kind = asset.type if asset else classify_url(url)
                add(CrawlResult(url=url, type=kind or PAGE, depth=page.depth + 1,
                                found_on=page.url,
                                title=asset.title if asset else '',
                                context=asset.context if asset else ''))
            progress.discovery(page.url, 'complete')
    return results


# =============================================================================================
# Assets: video/document links on a page (what counts as one: url_classifier.classify_url)
# =============================================================================================

def extract_assets(soup: BeautifulSoup, page_url: str) -> list[Asset]:
    """Video/document links on the page (links and embeds), in page order.

    Searches the whole page, not just the main content, so no recording is missed. Links to the
    same video/file (e.g. with different ?usp=... parameters) count once: see asset_id().
    """
    assets: dict[str, Asset] = {}
    for el, url in _iter_links(soup, _base_url(soup, page_url)):
        kind = classify_url(url)
        if kind is None:
            continue
        key = asset_id(url) or url
        title = _clean_text(el.get_text(' ') if el.name == 'a' else el.get('title', ''))
        if key not in assets:
            assets[key] = Asset(url=url, type=kind, title=title, context=_link_context(el))
        elif title and not assets[key].title:
            assets[key].title = title  # e.g. first occurrence was an embed with no title
    return list(assets.values())


CONTEXT_MAX = 200  # characters of surrounding text kept in Asset.context
BLOCK_BREAKS = {'br', 'p', 'div', 'li', 'td', 'th', 'tr', 'dd', 'dt'}


def _link_context(el: Tag) -> str:
    """What a document link is for: the nearest heading above it, plus the rest of its table
    row (schedule tables: time, session title, speaker) or of its list item/paragraph.

        "Tuesday, 5/19 · 9:45 – 10:15 a.m. · Presentation: “A Journey into the Cloud…” John Conklin"
    """
    parts = [_nearest_heading(el)]
    row = el.find_parent('tr')
    if row is not None:
        cells = [c for c in row.find_all(['td', 'th'], recursive=False) if el not in c.descendants]
        parts += [_text_without_documents(c, el) for c in cells]
    else:
        block = el.find_parent(['li', 'p', 'dd', 'figcaption'])
        if block is not None:
            parts.append(_text_without_documents(block, el))
    unique = [p for i, p in enumerate(parts) if p and p not in parts[:i]]
    return ' · '.join(unique)


def _text_without_documents(container: Tag, link: Tag) -> str:
    """container's text, leaving out `link` and other document links ("Slides | Recording"),
    trimmed of separators and shortened to CONTEXT_MAX."""
    skip = [link] + [a for a in container.find_all('a', href=True) if classify_url(a['href'])]
    pieces = []
    for node in container.descendants:
        if any(node is d or d in node.parents for d in skip):
            continue
        if isinstance(node, Tag):
            if node.name in BLOCK_BREAKS:
                pieces.append(' ')  # line break / new block: words don't run together
        else:
            pieces.append(str(node))  # inline text joins as written: “<a>Title</a>”
    text = _clean_text(''.join(pieces)).strip(' |·-–—:,')
    return text if len(text) <= CONTEXT_MAX else text[:CONTEXT_MAX - 1].rstrip() + '…'


# =============================================================================================
# Link filtering: which links are navigation rather than content
# =============================================================================================
#
# Two layers, applied by extract_urls(important_only=True):
#   1. Only links inside the page's main content area count (_content_root): links in menus,
#      headers, footers, and sidebars are repeated on every page and are ignored.
#   2. Each remaining link is checked by is_unimportant().

# Where the main content lives; the first selector that matches wins (else <body>).
CONTENT_SELECTORS = ['main', '[role=main]', 'article', '#main-content', '#content']

# Site chrome removed from the content area.
CHROME_SELECTOR = ('nav, header, footer, aside, [role=navigation], [role=banner], '
                   '[role=contentinfo], [role=complementary]')

# Static files: nothing to ingest or crawl. (PDFs are deliberately not listed.)
SKIP_EXTENSIONS = re.compile(
    r'\.(jpe?g|png|gif|svg|webp|ico|css|js|json|xml|rss|atom|ics|zip|gz|tar|exe|dmg|woff2?|ttf)$',
    re.I)

# Utility pages: login, search, feeds, admin, and WordPress tag/category/author archives.
SKIP_PATHS = re.compile(
    r'/(login|logout|signin|signup|register|search|feed|rss|tag|category|author|wp-admin|'
    r'wp-login\.php|wp-json|xmlrpc\.php)(/|\.|$)'
    r'|/(login|logout|dosearchsite)\.action$',  # Confluence
    re.I)


def is_unimportant(url: str, page_url: str) -> bool:
    """True for a link (found on page_url) that is navigation rather than content:

      * the homepage of any site
      * static files and utility pages (SKIP_EXTENSIONS, SKIP_PATHS)
      * the page linking to itself
      * breadcrumbs: ancestors of the current page (/services, linked from /services/cicp).
        Descendants and siblings are kept.

    Assets (videos/documents) are never unimportant.
    """
    if classify_url(url) is not None:
        return False
    parsed = urlparse(url)
    if parsed.path.strip('/') == '' and not parsed.query:
        return True
    if SKIP_EXTENSIONS.search(parsed.path) or SKIP_PATHS.search(parsed.path):
        return True
    if get_domain(url) == get_domain(page_url):
        segments, page_segments = _path_segments(url), _path_segments(page_url)
        if segments == page_segments and parsed.query == urlparse(page_url).query:
            return True
        if len(segments) < len(page_segments) and page_segments[:len(segments)] == segments:
            return True
    return False


def _content_root(soup: BeautifulSoup) -> BeautifulSoup:
    """A copy of the page's main content area, with site chrome removed."""
    root = next((m for m in map(soup.select_one, CONTENT_SELECTORS) if m is not None), None)
    root = BeautifulSoup(str(root or soup.body or soup), 'html.parser')  # copy; don't mutate soup
    for tag in root.select(CHROME_SELECTOR):
        tag.decompose()
    return root


# =============================================================================================
# Fetching and reading HTML
# =============================================================================================

class _CappedRetry(Retry):
    """Retry that never waits longer than retry_after_max, even if Retry-After asks for more."""

    def __init__(self, *args, retry_after_max: float = 60,
                 time_left: Optional[Callable[[], Optional[float]]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_after_max = retry_after_max
        self.time_left = time_left

    def new(self, **kwargs):  # urllib3 makes a new Retry per attempt; keep the caps
        retry = super().new(**kwargs)
        retry.retry_after_max = self.retry_after_max
        retry.time_left = self.time_left
        return retry

    def _cap(self, seconds):
        """seconds, but never more than the run has left (a sleep that outlasts the time
        budget would only get the run killed in the middle of it)."""
        left = self.time_left() if self.time_left else None
        return seconds if left is None or seconds is None else min(seconds, max(0.0, left))

    def get_retry_after(self, response):
        seconds = super().get_retry_after(response)
        return None if seconds is None else self._cap(min(seconds, self.retry_after_max))

    def get_backoff_time(self):
        return self._cap(super().get_backoff_time())


def new_session(config: Optional[Config] = None,
                time_left: Optional[Callable[[], Optional[float]]] = None) -> requests.Session:
    """A requests Session that retries temporary failures (see Config.retries) and, with
    config.block_private_addresses, refuses to connect to non-public addresses.

    time_left: a function returning the seconds left in the run's time budget (or None for
    no limit). Retry sleeps and request timeouts are capped by it."""
    config = config or config_module.default()
    n = config.retries
    retry = _CappedRetry(total=n, connect=n, read=n, status=n,
                         backoff_factor=config.retry_backoff, backoff_max=config.retry_after_max,
                         backoff_jitter=config.retry_jitter, status_forcelist=RETRY_STATUSES,
                         allowed_methods=frozenset({'GET', 'HEAD'}),
                         respect_retry_after_header=True, raise_on_status=False,
                         retry_after_max=config.retry_after_max, time_left=time_left)
    session = requests.Session()
    adapter_class = GuardedAdapter if config.block_private_addresses else HTTPAdapter
    adapter = adapter_class(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers['User-Agent'] = config.user_agent
    session.request_timeout = config.timeout  # read by fetch() and the downloader
    session.time_left = time_left             # ... together with this, by timeout_of()
    return session


class NotHTML(ValueError):
    """fetch(html_only=True) got something other than HTML; content_type says what."""

    def __init__(self, url: str, content_type: str):
        super().__init__(f'Not an HTML page ({content_type or "no Content-Type"})')
        self.url, self.content_type = url, content_type


def fetch(url: str, session: Optional[requests.Session] = None,
          html_only: bool = False, config: Optional[Config] = None) -> requests.Response:
    """GET a URL (retrying temporary failures), raising on HTTP errors, empty responses, and AWS
    WAF challenges. With html_only, a non-HTML response raises NotHTML after reading only its
    headers, so a link that turns out to be a 500 MB video isn't downloaded just to look at it.
    """
    if session is None:
        with new_session(config) as session:
            return fetch(url, session, html_only)
    response = session.get(url, timeout=timeout_of(session), stream=html_only)
    try:
        response.raise_for_status()
        if response.headers.get('x-amzn-waf-action') == 'challenge':
            raise RuntimeError(
                f'{url} returned an AWS WAF browser challenge. Unable to process this page!'
            )
        if html_only and not _is_html(response):
            raise NotHTML(url, response.headers.get('content-type', ''))
    except BaseException:
        response.close()  # with stream=True, don't leave the connection half-read
        raise
    if not response.content:
        raise ValueError(f'Empty response from {url} (HTTP {response.status_code})')
    return response


def timeout_of(session: requests.Session) -> float:
    """The request timeout for a session from new_session() (else the default config's), cut
    down to the time left in the run's budget -- but at least a second, so a request started
    just before the deadline still has a chance to answer."""
    timeout = getattr(session, 'request_timeout', None) or config_module.default().timeout
    time_left = getattr(session, 'time_left', None)
    left = time_left() if time_left else None
    return timeout if left is None else max(1.0, min(timeout, left))


def extract_text(soup: BeautifulSoup) -> str:
    """The page's main readable text, one line per block, without menus, forms, or scripts."""
    root = _content_root(soup)
    for tag in root.find_all(['script', 'style', 'noscript', 'form', 'svg', 'iframe', 'button']):
        tag.decompose()
    lines = (_clean_text(s) for s in root.get_text('\n').split('\n'))
    return '\n'.join(line for line in lines if line)


def _html_of(response: requests.Response) -> str:
    """The response body as text: in the charset the server declares, else as detected."""
    if 'charset' in response.headers.get('content-type', '').lower():
        return response.text
    soup_encoding = BeautifulSoup(response.content[:4096], 'html.parser').original_encoding
    return response.content.decode(soup_encoding or 'utf-8', errors='replace')


def _is_html(response: requests.Response) -> bool:
    return 'html' in response.headers.get('content-type', 'text/html')


def _parse(html: str | bytes) -> BeautifulSoup:
    if not html:
        raise ValueError('Cannot parse empty HTML')
    return BeautifulSoup(html, 'html.parser')


def _base_url(soup: BeautifulSoup, page_url: str) -> str:
    """The URL relative links resolve against: page_url, unless the page has a <base href>."""
    base_tag = soup.find('base', href=True)
    return urljoin(page_url, base_tag['href']) if base_tag is not None else page_url


def _iter_links(root: BeautifulSoup | Tag, base_url: str) -> Iterator[tuple[Tag, str]]:
    """(element, absolute URL) for every <a href> and <iframe src> under root, in page order.

    URLs have their #fragment removed; non-http(s) links (mailto:, javascript:, ...) are skipped.
    """
    for el in root.find_all(['a', 'iframe']):
        raw = el.get('href' if el.name == 'a' else 'src')
        if not isinstance(raw, str) or not raw.strip():
            continue
        url, _fragment = urldefrag(urljoin(base_url, raw.strip()))
        if urlparse(url).scheme in ('http', 'https'):
            yield el, url


def _nearest_heading(el: Tag) -> str:
    heading = el.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    return _clean_text(heading.get_text(' ')) if heading else ''


def _clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


# =============================================================================================
# URL helpers
# =============================================================================================

def url_key(url: str) -> str:
    """Normalized form for deciding whether two URLs are the same thing, used for crawl()'s
    `found` set. Videos/Drive files: their identity (asset_id), whatever the link looks like.
    Anything else: lowercase scheme/host, no 'www.', no trailing slash, no fragment."""
    identity = asset_id(url)
    if identity:
        return identity
    parsed = urlparse(urldefrag(url)[0])
    path = parsed.path.rstrip('/') or '/'
    query = f'?{parsed.query}' if parsed.query else ''
    return f'{parsed.scheme.lower()}://{get_domain(url)}{path}{query}'


def _domain_allowed(url: str, allowed_domains: set[str]) -> bool:
    return domain_allowed(get_domain(url), allowed_domains)


def _path_segments(url: str) -> list[str]:
    return [seg for seg in urlparse(url).path.split('/') if seg]


# =============================================================================================
# Command line
# =============================================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Discover page text and slide/recording links.',
        epilog='Modes: default prints scrape() JSON; --urls prints find_urls(); '
               '--depth N prints crawl_for_ingest() JSON; --depth N --urls prints crawl().')
    parser.add_argument('url', help='Page or direct video/document URL')
    parser.add_argument('--urls', action='store_true',
                        help='Just print the URLs found on the page, one per line')
    parser.add_argument('--depth', type=int,
                        help='Crawl to this depth (start page = 0) and print what to ingest as JSON '
                             '(with --urls: just the URLs found)')
    parser.add_argument('--domain', action='append', dest='domains',
                        help="Domain the crawl may follow (repeatable; default: the URL's domain)")
    parser.add_argument('--all', action='store_true',
                        help='Keep navigation/boilerplate links instead of filtering them out')
    parser.add_argument('--html', type=Path,
                        help='Parse a saved HTML file instead of fetching URL (URL is still used '
                             'to resolve relative links). Not used with --depth')
    parser.add_argument('--links-only', action='store_true',
                        help="Pages with video/document links: ingest only their documents, not "
                             "the page's own text (default: both)")
    parser.add_argument('--no-text', action='store_true',
                        help='Omit page text and HTML from JSON output (display only)')
    args = parser.parse_args()
    setup_logging()
    important_only = not args.all

    try:
        if args.depth is not None and args.urls:
            _print_lines(crawl(args.url, args.depth, args.domains, important_only=important_only))
        elif args.depth is not None:
            results = crawl_for_ingest(args.url, args.depth, args.domains,
                                       important_only=important_only,
                                       text_with_links=not args.links_only)
            if args.no_text:
                for r in results:
                    r.text = r.html = ''
            print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
        elif args.urls:
            if args.html is not None:
                _print_lines(extract_urls(args.html.read_bytes(), args.url, important_only))
            else:
                _print_lines(find_urls(args.url, important_only))
        else:
            if args.html is not None:
                result = scrape_html(args.html.read_bytes(), args.url, not args.links_only)
            else:
                result = scrape(args.url, not args.links_only)
            if args.no_text:
                result.text = result.html = ''
            print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f'Error: {exc}\n')


def _print_lines(lines: Iterable[str]) -> None:
    for line in lines:
        print(line)


if __name__ == '__main__':
    main()
