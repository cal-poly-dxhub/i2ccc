"""Planning a run: crawl_into_store(), dedupe(), and the local download_all() loop."""
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TYPE_CHECKING

from .api import FETCHERS, as_store, ingest, save_page
from .commit import _predict_key, deferred_result, failed_result
from .drive import Expanded, expand, folder_result
from .errors import DeadlineExceeded, DiskFullError, DownloadError, Reason, classify_error
from .google_urls import EXPORT_FORMATS, parse_google_url
from .models import (DEFERRED, FAILED, _field, Context, DownloadResult, Options,
    Source)
from ..config import Config
from ..scraper import (NOT_FETCHED, PAGE, PAGE_TEXT, crawl_for_ingest, CrawlResult,
    FAILED as FAILED_PAGE, url_key)
from ..store import Store
from ..url_classifier import GOOGLE_DRIVE, classify_url

if TYPE_CHECKING:
    from ..drive_api import DriveClient

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def download_all(entries: Iterable['dict | str | object'], out: 'Store | Path | str' = 'downloads',
                 options: Optional[Options] = None, drive: Optional['DriveClient'] = None,
                 config: Optional[Config] = None, save_pages: bool = True, *,
                 ctx: Optional[Context] = None, budget: Optional[float] = None,
                 reserve: float = 30.0) -> list[DownloadResult]:
    """Everything in a crawl_for_ingest() result, in one process. In order:

      1. save_page() every crawled page (with save_pages)
      2. dedupe() the assets, expand() Drive folders into their files, dedupe() again (a file
         may be both linked directly and in a linked folder)
      3. ingest() each item once

    entries: CrawlResults, their dicts (i2cc-scrape --depth JSON), or plain URLs. Failed or unread
    crawl pages, and pages without text (e.g. a --no-text crawl), are ignored. Any other
    URL that isn't a video, document or file we can fetch comes back as a FAILED result with
    reason `unsupported`, so a link you hand over is never silently dropped.

    One item can't end the run. Whatever goes wrong with it -- a temporary failure, a bug, an
    unexpected error -- it becomes a FAILED result (reason `unavailable` / `error`) and the
    rest carry on. Every entry gets a result, in this order: pages, then expand() failures,
    items, and a summary row for each linked Drive folder.

    Time. With budget=<seconds> (see Context), items stop being started once less than `reserve`
    seconds are left. Those not reached come back as DEFERRED results -- nothing was done for
    them -- and the item underway when the time ran out as FAILED with reason `timeout`. Run
    again to carry on: what is stored and unchanged is skipped. A full disk stops the run the
    same way (reason `disk_full`).

    ctx: run with this Context -- its store, options, config, Drive client and time budget; the
    arguments above that build one are then ignored -- so one budget can cover a crawl and the
    downloads that follow. It is not closed.
    """
    if ctx is not None:
        return _download_all(entries, ctx, save_pages)
    with Context(as_store(out), options, config, drive, budget=budget, reserve=reserve) as own:
        return _download_all(entries, own, save_pages)


def _download_all(entries: Iterable['dict | str | object'], ctx: Context,
                  save_pages: bool) -> list[DownloadResult]:
    results: list[DownloadResult] = []
    assets: list[Source] = []
    halt: list[Reason] = []   # why the rest of the run is deferred, once it is (at most one)

    def attempt(url: str, kind: str, run: Callable[[], Any], title: str = ''):
        """run() as one unit of work: (its value, None), or (None, the result to report
        instead) if the run has stopped or this unit fails. Nothing escapes."""
        if not halt and not ctx.can_start():
            halt.append(Reason.TIMEOUT)
            log.warning('Stopping: %.0f s left of the time budget; the rest is deferred',
                        ctx.time_left() or 0)
        if halt:
            return None, deferred_result(url, kind, halt[0], title)
        kind = kind or classify_url(url) or ''
        try:
            return run(), None
        except Exception as exc:  # whatever it is, it is this unit's failure, not the run's
            error = classify_error(exc)  # (save_page and expand don't classify for themselves)
            if isinstance(error, (DeadlineExceeded, DiskFullError)):
                halt.append(error.reason)  # the rest would fail the same way
                log.warning('Stopping: %s', error)
            elif error is None:  # a bug or something unexpected
                log.exception('Unexpected error for %s', url)
                error = DownloadError(f'{type(exc).__name__}: {exc}', Reason.ERROR)
            return None, failed_result(url, kind, error, title)

    for entry in entries:
        source = Source.of(entry)
        is_page = source.type in (PAGE, PAGE_TEXT) and not isinstance(entry, str)
        if is_page:
            if save_pages and _field(entry, 'text'):
                saved, problem = attempt(source.url, source.type, lambda: save_page(entry, ctx))
                results.append(saved or problem)
        elif source.type in (FAILED_PAGE, NOT_FETCHED):
            continue  # a crawl's unread pages: the crawl reports those (CrawlReport.problems)
        else:
            assets.append(source)  # an asset -- or a URL we can't handle: ingest() says so

    items: list[Source] = []
    failures: dict[str, DownloadResult] = {}           # one per item, even if listed twice
    folders: list[tuple[Source, Expanded]] = []
    for asset in dedupe(assets, ctx.options):
        expanded, problem = attempt(asset.url, asset.type, lambda: expand(asset, ctx),
                                    asset.link_text)
        if problem:
            results.append(problem)
            continue
        items += expanded.items
        for failure in expanded.failures:
            failures.setdefault(failure.key or failure.url, failure)
        if expanded.title or expanded.items != [asset]:  # it was a folder
            folders.append((asset, expanded))
    results += failures.values()
    done: dict[str, DownloadResult] = {}
    for item in dedupe(items, ctx.options):
        result, problem = attempt(item.url, item.type, lambda: ingest(item, ctx), item.link_text)
        results.append(result or problem)
        done[_group_key(item)] = result or problem
    # Each folder gets a summary row (its files are reported above, once each).
    for asset, expanded in folders:
        children = [done[k] for k in dict.fromkeys(map(_group_key, expanded.items)) if k in done]
        children += [f for f in expanded.failures]
        results.append(_folder_summary(asset, expanded.title, children))
    if halt:
        log.warning('%d item(s) not reached (%s); run again to carry on',
                    sum(r.status == DEFERRED for r in results), halt[0])
    return results


def _folder_summary(asset: Source, title: str, children: list[DownloadResult]) -> DownloadResult:
    """download_all()'s row for a linked Drive folder: its overall status and files. children
    are left out (they're rows of their own), but failures are summed up in `error`."""
    deferred = [c for c in children if c.status == DEFERRED]
    if children and len(deferred) == len(children):  # the run stopped before reaching any of it
        return DownloadResult(url=asset.url, type=GOOGLE_DRIVE, status=DEFERRED, title=title,
                              error=deferred[0].error, reason=deferred[0].reason)
    reached = [c for c in children if c.status != DEFERRED]
    errors = [f'{c.title or c.url}: {c.error}' for c in reached if c.status == FAILED]
    row = folder_result(asset.url, title, reached, errors, with_children=False)
    if deferred:
        note = f'{len(deferred)} item(s) not reached: {deferred[0].error}'
        if errors:   # something really failed too: that is the headline
            row = replace(row, error=f'{row.error}; {note}')
        else:        # only time (or disk) ran out: say so, don't call it a failure
            row = replace(row, error=note, reason=deferred[0].reason)
    return row


def dedupe(items: Iterable['Source | dict | str'], options: Optional[Options] = None
           ) -> list[Source]:
    """One work item per stored item, in order of first appearance. No network requests.

    Items that would be stored under the same key -- the same deck linked from several pages,
    or both linked directly and listed in a Drive folder, or the same link spelled differently
    (?usp=..., youtu.be/..., www., a trailing slash) -- become one, with:

      * found_on: every page/folder, in order
      * link_text and context: from a page's link if there is one (more telling than a name
        in a folder listing), else the first non-empty
      * folder_path: the longest (the fullest path, whichever folder came first) -- so an
        item found both on a page and in a folder keeps both
      * url: for a Drive file linked both in the editor (/presentation/d/X, exported) and as a
        file (/file/d/X, the original), the link that matches options.original -- the stored
        file would otherwise depend on which link came first

    Run it before expand() and again after (folders' files are only known then).
    """
    options = options or Options()
    groups: dict[str, list[Source]] = {}
    for item in map(Source.of, items):
        groups.setdefault(_group_key(item), []).append(item)
    return [_merge_items(group, options) for group in groups.values()]


def _group_key(source: Source) -> str:
    kind = source.kind
    if kind == GOOGLE_DRIVE:
        try:
            return 'google_drive/' + parse_google_url(source.url)[1]  # files and folders alike
        except DownloadError:
            return 'url:' + url_key(source.url)
    return _predict_key(kind, source.url) or 'url:' + url_key(source.url)


def _merge_items(group: list[Source], options: Options) -> Source:
    if len(group) == 1:
        return group[0]
    best = min(group, key=lambda s: _link_rank(s, options))  # min() keeps the first of ties
    from_pages = [s for s in group if not s.folder_path] + [s for s in group if s.folder_path]
    found_on: list[str] = []
    for item in group:
        found_on += [page for page in item.found_on if page and page not in found_on]
    return replace(
        best, found_on=found_on,
        type=next((s.type for s in group if s.type in FETCHERS), best.type),
        link_text=next((s.link_text for s in from_pages if s.link_text), ''),
        context=next((s.context for s in from_pages if s.context), ''),
        folder_path=max((s.folder_path for s in group), key=len))


def _link_rank(source: Source, options: Options) -> int:
    """Lower is preferred: which of several links to one Drive file to download through."""
    try:
        link_kind = parse_google_url(source.url)[0]
    except DownloadError:
        return 2
    if link_kind in EXPORT_FORMATS:  # opened in the Slides/Docs/Sheets editor: exported
        return 1 if options.original else 0
    if link_kind == 'file':          # a file link: the original
        return 0 if options.original else 1
    return 2


@dataclass
class CrawlReport:
    """What crawl_into_store() did. `results` is crawl_for_ingest()'s list, but pages carry
    their store `key` instead of their text/html, so it stays small (and JSON-serializable)."""
    results: list[CrawlResult]
    pages: list[DownloadResult]       # save_page() result for each page read

    @property
    def assets(self) -> list[CrawlResult]:
        """The documents and recordings found: what to ingest() (or expand() first)."""
        return [r for r in self.results if r.type in FETCHERS]

    @property
    def problems(self) -> list[DownloadResult]:
        """Pages that failed to load or weren't read, as FAILED results (for reports)."""
        return [DownloadResult(url=r.url, type=PAGE, error=r.error,
                               reason=Reason.NOT_FETCHED if r.type == NOT_FETCHED else Reason.ERROR)
                for r in self.results if r.type in (FAILED_PAGE, NOT_FETCHED)]

    @property
    def truncated(self) -> bool:
        """True if max_pages or the time limit stopped the crawl before every page was read."""
        return any(r.type == NOT_FETCHED for r in self.results)


def crawl_into_store(start_url: str, ctx: Context, max_depth: int = 1, *,
                     crawl_share: float = 0.4, **crawl_options) -> CrawlReport:
    """crawl_for_ingest(), storing each page (save_page) as soon as it's read, so page content
    never piles up in memory or in the results. crawl_options go to crawl_for_ingest()
    (allowed_domains, max_pages, time_limit, ...); its config is ctx.config.

    With a time budget on ctx, the crawl may use `crawl_share` of what is left (after the
    reserve) unless you pass a time_limit of your own; pages it doesn't reach are reported as
    not fetched, and the rest of the budget stays for downloading. A page that can't be stored
    is a FAILED entry in report.pages -- it doesn't end the crawl.

    The step before ingesting: crawl_into_store(), then dedupe() report.assets, expand()
    each, dedupe() again, and ingest() each item. Raises netguard.UnsafeURLError for a start URL that may not be crawled.
    """
    pages: list[DownloadResult] = []

    def on_page(page: CrawlResult) -> None:
        ctx.progress.discovered(page.url, page.type, title=page.title)
        if page.text:
            ctx.progress.content(page.url, 'checking')
            try:
                result = save_page(page, ctx)
            except Exception as exc:
                error = classify_error(exc)
                if error is None:
                    log.exception('Could not store page %s', page.url)
                    error = DownloadError(f'{type(exc).__name__}: {exc}', Reason.ERROR)
                else:
                    log.warning('Could not store page %s: %s', page.url, error)
                result = failed_result(page.url, page.type, error, page.title)
            page.key = result.key
            pages.append(result)
            ctx.progress.content(page.url, 'failed' if result.status == FAILED else 'stored',
                                 result=asdict(result), error=result.error, reason=result.reason)
        else:
            ctx.progress.content(page.url, 'skipped', reason='no_text')

    left = ctx.time_left()
    if left is not None and crawl_options.get('time_limit') is None:
        share = max(1.0, (left - ctx.reserve) * crawl_share)
        configured = ctx.config.crawl_time_limit
        crawl_options['time_limit'] = min(share, configured) if configured else share

    results = crawl_for_ingest(start_url, max_depth, config=ctx.config, on_page=on_page,
                               progress=ctx.progress,
                               **crawl_options)
    report = CrawlReport(results=results, pages=pages)
    if report.truncated:
        log.warning('The crawl of %s stopped early: %d page(s) not read', start_url,
                    sum(r.type == NOT_FETCHED for r in results))
    return report
