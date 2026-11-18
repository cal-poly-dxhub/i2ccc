"""Storing: store keys, and the one place that decides new / updated / unchanged
(_commit, _unchanged) and merges Record metadata."""
import hashlib
import logging
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .errors import DownloadError, Reason, TransientError
from .google_urls import parse_google_url
from .models import DEFERRED, NEW, UNCHANGED, UPDATED, Context, DownloadResult, Source
from .. import store as _store
from ..scraper import PAGE, PAGE_TEXT, url_key
from ..store import KEY_PATTERN, ConflictError, Content, Record, Staged, sha256_of
from ..url_classifier import FILE, GOOGLE_DRIVE, asset_id, classify_url

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def _log_result(result: DownloadResult) -> None:
    if result.status in (NEW, UPDATED):
        log.info('%s %s: %s -> %s', result.status.capitalize(), result.type, result.url,
                 ', '.join(result.files) or '(folder)')
    elif result.status == UNCHANGED:
        log.info('Unchanged %s: %s', result.type, result.url)


def page_key(url: str) -> str:
    return f'pages/{_url_hash(url)}'


def _url_hash(url: str) -> str:
    return hashlib.sha1(url_key(url).encode()).hexdigest()[:16]


def failed_result(url: str, kind: str, error: 'DownloadError | TransientError',
                  title: str = '') -> DownloadResult:
    """A FAILED result for url, with the store key it would have had and the error's reason."""
    return DownloadResult(url=url, type=kind, key=_predict_key(kind, url), title=title,
                          error=str(error), reason=error.reason)


_NOT_REACHED = {
    Reason.TIMEOUT: 'Not attempted: the run ran out of time. Run it again to carry on.',
    Reason.DISK_FULL: 'Not attempted: the disk filled up. Run it again to carry on.',
}


def deferred_result(url: str, kind: str, reason: Reason, title: str = '') -> DownloadResult:
    """A DEFERRED result: the run stopped before reaching url, and nothing was done for it."""
    return DownloadResult(url=url, type=kind, status=DEFERRED, key=_predict_key(kind, url),
                          title=title, error=_NOT_REACHED.get(reason, 'Not attempted.'),
                          reason=reason)


def make_key(prefix: str, ident: str) -> str:
    """The store key "<prefix>/<ident>", or DownloadError(INVALID_URL) if the ID isn't one we
    can store under (e.g. a mangled YouTube ID in a link) -- never a crash mid-run."""
    key = f'{prefix}/{ident}'
    if not KEY_PATTERN.fullmatch(key):
        raise DownloadError(f'Unusable {prefix} ID in link: {ident!r}', Reason.INVALID_URL)
    return key


def _key_for(kind: str, url: str) -> str:
    """The store key url has, from the URL alone. Raises DownloadError for a bad link."""
    if kind == FILE:
        return make_key('files', _url_hash(url))
    if kind in (PAGE, PAGE_TEXT):
        return page_key(url)
    if kind == GOOGLE_DRIVE:
        link_kind, file_id = parse_google_url(url)
        return '' if link_kind == 'folder' else make_key('google_drive', file_id)
    identity = asset_id(url)
    if not identity:
        return ''
    prefix, _, ident = identity.partition(':')
    return make_key(prefix, ident)


def _predict_key(kind: str, url: str) -> str:
    """_key_for(), or '' if the link is bad (for reporting failures)."""
    try:
        return _key_for(kind, url)
    except (DownloadError, KeyError, IndexError):
        return ''


def _commit(ctx: Context, source: Source, kind: str, key: str, files: list[tuple[str, Content]],
            title: str, content_type: str, source_version: str,
            previous: Optional[Record], links: Optional[list[str]] = None) -> DownloadResult:
    """Store freshly downloaded files under key -- unless they're identical to what's stored,
    in which case only changed metadata is written.

    links: for a page, the store keys of the documents/videos it links to. A page whose text is
    unchanged but whose links changed is stored again, so its saved HTML doesn't go stale. (A
    stored page with no recorded links, e.g. from an older version, counts as unchanged and
    just gets them recorded.)"""
    sha = sha256_of(files[0][1])
    suffixes = [suffix for suffix, _ in files]

    def attempt(prev: Optional[Record]) -> DownloadResult:
        same_links = links is None or prev is None or prev.links in (None, links)
        if (prev is not None and prev.content_sha256 == sha and same_links
                and [ctx.store.suffix_of(key, name) for name in prev.files] == suffixes):
            return _unchanged(ctx, source, prev, title=title, source_version=source_version,
                              kind=kind, content_type=content_type, links=links)
        found_on = _merge_found_on(prev, source)
        folder_path = source.folder_path or (prev.folder_path if prev else '')
        # Extract parent folder name from the folder path (last component, e.g. "Day 2" from "Slides 2026 / Day 2")
        parent_folder_name = folder_path.split(' / ')[-1] if folder_path else ''
        # For Drive items, the folder URL is in found_on; for others, extract from found_on if it's a Drive folder
        parent_folder_url = ''
        if found_on:
            for url in found_on:
                if '/drive/folders/' in url or '/drive/u/' in url:
                    parent_folder_url = url
                    break
        record = Record(
            key=key, source_url=source.url, type=kind, files=[], content_sha256=sha, title=title,
            link_text=source.link_text or (prev.link_text if prev else ''),
            context=_merged_context(prev, source),
            folder_path=folder_path,
            found_on=found_on, content_type=content_type,
            source_version=source_version, fetched_at=_store.now(),
            links=links if links is not None else (prev.links if prev else None),
            unlinked_at='' if found_on or prev is None else prev.unlinked_at,
            is_subscriber_content=False,  # For now, all content is non-subscriber
            parent_folder_name=parent_folder_name,
            parent_folder_url=parent_folder_url,
            relative_start_time=None)  # Video chunks not yet implemented
        record = ctx.store.put(record, files, expected=prev)  # refused if someone wrote since
        return DownloadResult(url=source.url, type=kind, status=UPDATED if prev else NEW,
                              key=key, files=record.files, title=title)

    return _retry_on_conflict(ctx, key, previous, attempt)


CONFLICT_RETRIES = 8


def _retry_on_conflict(ctx: Context, key: str, previous: Optional[Record],
                       attempt: Callable[[Optional[Record]], object]):
    """Run attempt(previous) -- which merges what it knows into the Record `previous` and writes
    the result with expected=previous. If another writer (an overlapping run, another thread)
    changed the Record in the meantime, the write is refused: read the Record again and redo the
    merge on top of their version, so neither update is lost. After CONFLICT_RETRIES refusals
    in a row, TransientError: the caller can simply try again later."""
    for n in range(CONFLICT_RETRIES):
        try:
            return attempt(previous)
        except ConflictError:
            log.info('%s was updated by another run; merging with its version', key)
            time.sleep(random.uniform(0, 0.02 * (n + 1)))  # don't collide in lockstep
            previous = ctx.store.get(key)
    raise TransientError(f'{key}: its record kept changing under us ({CONFLICT_RETRIES} '
                         'conflicting writers in a row); try again later')


@dataclass
class Downloaded:
    """What a fetcher's download step produced, for fetch_versioned()."""
    files: list[tuple[str, Content]]  # (suffix, path in the work dir or Staged), main first
    title: str
    content_type: str
    version: Optional[str] = None   # a version learned while downloading (an ETag); None: the
                                    # version fetch_versioned() was given
    links: Optional[list[str]] = None  # pages: see _commit()


NOT_MODIFIED = object()  # a download step's answer when the source says it hasn't changed


def fetch_versioned(ctx: Context, source: Source, kind: str, key: str,
                    previous: Optional[Record], download: Callable[[Path], object],
                    version: str = '', title: str = '') -> Optional[DownloadResult]:
    """The shape every fetcher shares. If the stored Record already has `version`, report the
    item unchanged without downloading. Otherwise run download(work_dir) in a fresh work
    directory and commit the Downloaded it returns. download may instead return NOT_MODIFIED
    (unchanged after all), or None: it couldn't, and the caller will try another way -- then
    fetch_versioned() returns None too."""
    if _is_current(ctx, previous, version):
        return _unchanged(ctx, source, previous, title=title)
    with ctx.work_dir() as work:
        got = download(work)
        if got is NOT_MODIFIED:
            return _unchanged(ctx, source, previous)
        if got is None:
            return None
        try:
            return _commit(ctx, source, kind, key, got.files, got.title, got.content_type,
                           version if got.version is None else got.version, previous,
                           links=got.links)
        finally:  # unchanged, refused or failed: staged content not put() mustn't pile up
            for _, content in got.files:
                if isinstance(content, Staged):
                    ctx.store.discard(content)


def _merged_record(previous: Record, source: Source, title: str = '', source_version: str = '',
                   kind: str = '', content_type: str = '', links: Optional[list[str]] = None,
                   **changes) -> Record:
    """previous with what this sighting adds: another page in found_on, a better title or
    context, a changed type, newly recorded links. `changes` are set outright."""
    found_on = _merge_found_on(previous, source)
    folder_path = source.folder_path or previous.folder_path
    # Recalculate parent folder metadata from the merged state
    parent_folder_name = folder_path.split(' / ')[-1] if folder_path else ''
    parent_folder_url = ''
    for url in found_on:
        if '/drive/folders/' in url or '/drive/u/' in url:
            parent_folder_url = url
            break
    return replace(previous,
                   type=kind or previous.type,
                   content_type=content_type or previous.content_type,
                   title=title or previous.title,
                   link_text=source.link_text or previous.link_text,
                   context=_merged_context(previous, source),
                   folder_path=folder_path,
                   found_on=found_on,
                   source_version=source_version or previous.source_version,
                   links=previous.links if links is None else links,
                   unlinked_at='' if found_on else previous.unlinked_at,
                   parent_folder_name=parent_folder_name,
                   parent_folder_url=parent_folder_url,
                   **changes)


def _unchanged(ctx: Context, source: Source, previous: Record, title: str = '',
               source_version: str = '', kind: str = '', content_type: str = '',
               links: Optional[list[str]] = None, **changes) -> DownloadResult:
    """The stored content is current. Rewrite the Record only if its metadata changed --
    including its type (a page that gained document links goes from page_text to page).
    source_url stays the first URL it was stored from."""
    def attempt(prev: Optional[Record]) -> DownloadResult:
        if prev is None:
            raise TransientError(f'{previous.key}: its record disappeared while updating it')
        # After a conflict, `prev` is the other writer's version. If their content differs from the
        # content we compared against, our version marker and links describe something else.
        same = prev is previous or prev.content_sha256 == previous.content_sha256
        record = _merged_record(prev, source, title, source_version if same else '', kind,
                                content_type, links if same else None, **changes)
        if record != prev:
            ctx.store.put_record(record, expected=prev)
        return DownloadResult(url=source.url, type=record.type, status=UNCHANGED, key=prev.key,
                              files=list(prev.files), title=record.title)

    return _retry_on_conflict(ctx, previous.key, previous, attempt)


def _merged_context(previous: Optional[Record], source: Source) -> str:
    """The Record's context: the source's, else the stored one -- except that Records written
    before folder_path existed kept a Drive folder path in context, which is cleared once
    the path is known to be a folder path."""
    if source.context or previous is None:
        return source.context
    folder_path = source.folder_path or previous.folder_path
    return '' if folder_path and previous.context == folder_path else previous.context


def _merge_found_on(previous: Optional[Record], source: Source) -> list[str]:
    found_on = list(previous.found_on) if previous else []
    for page in source.found_on:
        if page and page not in found_on:
            found_on.append(page)
    return found_on


def link_keys(urls: Iterable[str]) -> list[str]:
    """The store keys of the documents/videos at urls, sorted, once each. Links that aren't
    documents or videos, and ones that can't be stored (a mangled ID), are left out."""
    keys = set()
    for url in urls:
        kind = classify_url(url)
        try:
            if kind == GOOGLE_DRIVE:  # a folder too: it isn't stored, but a change is a change
                keys.add(make_key('google_drive', parse_google_url(url)[1]))
            elif kind:
                keys.add(_key_for(kind, url))
        except DownloadError:
            continue
    return sorted(keys)


def unlink_page(ctx: Context, key: str, page_url: str) -> None:
    """The page at page_url no longer links to the item at key: drop it from the item's
    found_on (whatever the spelling of its URL). If that leaves no page, unlinked_at records
    when. The item stays in the store -- removing it is for whoever consumes the store."""
    gone = url_key(page_url)

    def attempt(record: Optional[Record]) -> None:
        if record is None:
            return
        found_on = [url for url in record.found_on if url_key(url) != gone]
        if found_on == record.found_on:
            return
        log.info('%s no longer links to %s', page_url, key)
        ctx.store.put_record(replace(
            record, found_on=found_on,
            unlinked_at='' if found_on else (record.unlinked_at or _store.now())), expected=record)

    _retry_on_conflict(ctx, key, ctx.store.get(key), attempt)


CAPTION_SUFFIXES = ('.vtt', '.srt')


def captions_due(ctx: Context, previous: Optional[Record]) -> bool:
    """True if a stored recording has no captions and it's time to look for them again.

    Captions are often added after upload (YouTube's automatic ones take hours), so a recording
    stored without them is checked again -- at most every Config.caption_recheck_hours, for
    Config.caption_recheck_days after it was downloaded -- and then left alone. Only the
    captions are fetched, never the video again."""
    config = ctx.config
    if previous is None or not ctx.options.captions or config.caption_recheck_days <= 0:
        return False
    if any(name.endswith(CAPTION_SUFFIXES) for name in previous.files):
        return False
    fetched = _store.parse_time(previous.fetched_at)
    if fetched is None:
        return False
    current = _store.parse_time(_store.now()) or datetime.now(timezone.utc)
    if current - fetched > timedelta(days=config.caption_recheck_days):
        return False
    last_check = _store.parse_time(previous.captions_checked_at) or fetched
    return current - last_check >= timedelta(hours=config.caption_recheck_hours)


def add_captions(ctx: Context, source: Source, previous: Record,
                 captions: list[tuple[str, Path]]) -> DownloadResult:
    """The outcome of a captions-only re-check of a stored recording: add the captions found
    (UPDATED; the video stays as it is), or note that none exist yet (UNCHANGED)."""
    stamp = _store.now()
    if not captions:
        return _unchanged(ctx, source, previous, captions_checked_at=stamp)

    def attempt(prev: Optional[Record]) -> DownloadResult:
        if prev is None:
            raise TransientError(f'{previous.key}: its record disappeared while updating it')
        record = _merged_record(prev, source, captions_checked_at=stamp)
        record = ctx.store.put(record, captions, keep_other_files=True, expected=prev)
        return DownloadResult(url=source.url, type=record.type, status=UPDATED, key=record.key,
                              files=record.files, title=record.title)

    return _retry_on_conflict(ctx, previous.key, previous, attempt)


def _is_current(ctx: Context, previous: Optional[Record], version: str) -> bool:
    """True if the stored Record already has this source version (so skip the download)."""
    return (not ctx.options.force and previous is not None and bool(version)
            and previous.source_version == version)

