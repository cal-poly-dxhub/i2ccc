"""ingest() one item, save_page(), download() one URL, and the FETCHERS registry."""
import logging
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

import requests

from .commit import (Downloaded, _log_result, failed_result, fetch_versioned, link_keys,
                     page_key, unlink_page)
from .drive import fetch_google_drive
from .errors import DownloadError, Reason, TransientError, permanent_error
from .files import fetch_file
from .models import _field, Context, DownloadResult, Options, Source
from .panopto import fetch_panopto
from .youtube import fetch_youtube
from ..config import Config
from ..scraper import PAGE, PAGE_TEXT
from ..store import LocalStore, Store
from ..url_classifier import (FILE, GOOGLE_DRIVE, PANOPTO, YOUTUBE, classify_url,
                              is_youtube_host)

if TYPE_CHECKING:
    from ..drive_api import DriveClient

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def ingest(entry: 'Source | dict | str', ctx: Context) -> DownloadResult:
    """Download one item while reporting its attempts and terminal result."""
    source = Source.of(entry)
    ctx.progress.discovered(source.url, source.kind, title=source.link_text)
    for parent in source.found_on:
        ctx.progress.discovered(source.url, source.kind, parent=parent, title=source.link_text,
                                context=source.context)
    ctx.progress.content(source.url, 'checking')
    previous_url, ctx.current_url = ctx.current_url, source.url
    try:
        result = _ingest(source, ctx)
    except TransientError as exc:
        ctx.progress.content(source.url, 'retrying', error=str(exc), reason=exc.reason)
        raise
    except Exception as exc:
        ctx.progress.content(source.url, 'failed', error=str(exc), reason='error')
        raise
    finally:
        ctx.current_url = previous_url
    ctx.progress.content(source.url, 'failed' if result.status == 'failed' else 'stored',
                         result=asdict(result), error=result.error, reason=result.reason)
    return result


def _ingest(entry: 'Source | dict | str', ctx: Context) -> DownloadResult:
    """Download one asset into ctx.store, unless it's stored and unchanged.

    Returns a DownloadResult; a permanent failure is a FAILED result with a Reason. Raises
    TransientError for a temporary failure worth retrying later. Other exceptions (e.g. a full
    disk) are bugs or environment problems and propagate.
    """
    source = Source.of(entry)
    kind = source.kind
    if kind not in FETCHERS:
        error = ('A YouTube link must be a single video (youtube.com/watch?v=..., youtu.be/...), '
                 'not a playlist, channel or search page'
                 if is_youtube_host(source.url) else
                 'Not a YouTube, Panopto, Google Drive, or file link')
        return DownloadResult(url=source.url, type=kind or source.type, error=error,
                              reason=Reason.UNSUPPORTED)
    try:
        result = FETCHERS[kind](source, ctx)
    except Exception as exc:
        try:
            error = permanent_error(exc)
        except TransientError as transient:
            log.warning('Temporary failure for %s: %s', source.url, transient)
            raise
        log.warning('Failed %s: %s', source.url, error)
        return failed_result(source.url, kind, error, source.link_text)
    _log_result(result)
    return result


def save_page(entry: 'dict | object', ctx: Context) -> DownloadResult:
    """Store a crawled page (a CrawlResult or its dict): its text as "<key>.txt" -- headed by
    its title and URL. The text is the main file, so a page whose extracted text doesn't change
    (e.g. a markup-only change) counts as unchanged.

    The documents and videos the page links to (entry.links) are recorded in its Record. If
    they change while its text doesn't, the page is stored again; and items it no longer links
    to lose it from their found_on (see unlink_page). Page-to-page links aren't tracked: a menu
    or "related posts" widget would otherwise rewrite pages constantly."""
    source = Source.of(entry)
    kind = source.type if source.type in (PAGE, PAGE_TEXT) else PAGE
    title = _field(entry, 'title', '') or ''
    text = _field(entry, 'text', '') or ''
    key = page_key(source.url)
    previous = ctx.store.get(key)
    raw_links = _field(entry, 'links')
    links = None if raw_links is None else link_keys(raw_links)

    def write(work: Path) -> Downloaded:
        if not text:
            raise DownloadError(f'No text for page {source.url}')
        # The URL in the header is the one first stored, so reaching the same page through a
        # variant ("www.", trailing slash) doesn't count as a change.
        header_url = previous.source_url if previous else source.url
        path = work / 'page.txt'
        path.write_text(f'Title: {title}\nURL: {header_url}\n\n{text}\n', encoding='utf-8')
        files = [('.txt', path)]
        content_type = 'text/plain; charset=utf-8'
        return Downloaded(files, title, content_type, links=links)

    # A page's Record describes the page itself, not a link to it.
    page = replace(source, link_text='', context='', folder_path='')
    result = fetch_versioned(ctx, page, kind, key, previous, write)
    if links is not None and previous is not None:
        for gone in sorted(set(previous.links or []) - set(links)):
            unlink_page(ctx, gone, source.url)
    _log_result(result)
    return result



def as_store(out: 'Store | Path | str') -> Store:
    """out itself if it's a Store, else a LocalStore in that directory."""
    return out if not isinstance(out, (str, Path)) else LocalStore(out)


def download(url: str, out: 'Store | Path | str' = 'downloads', kind: Optional[str] = None, *,
             drive: Optional['DriveClient'] = None, session: Optional[requests.Session] = None,
             config: Optional[Config] = None, **options) -> DownloadResult:
    """Download one URL into out (a Store, or a directory for a LocalStore). Never raises for a
    failed download: temporary failures are FAILED results with reason UNAVAILABLE. options
    are Options fields (export_format=..., force=True, ...). kind overrides classify_url(url),
    e.g. FILE for a link the crawler recognised by its Content-Type."""
    store = as_store(out)
    with Context(store, Options(**options), config, drive, session) as ctx:
        return _report_transient(lambda: ingest(Source(url, kind or ''), ctx), url, kind or '')


def _report_transient(run: Callable[[], DownloadResult], url: str, kind: str) -> DownloadResult:
    try:
        return run()
    except TransientError as exc:
        return failed_result(url, kind or classify_url(url) or '', exc)


FETCHERS: dict[str, Callable[[Source, Context], DownloadResult]] = {
    YOUTUBE: fetch_youtube,
    PANOPTO: fetch_panopto,
    GOOGLE_DRIVE: fetch_google_drive,
    FILE: fetch_file,
}
