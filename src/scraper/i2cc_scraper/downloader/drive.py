"""Google Drive: files and exports (Drive API or public links), and expand(), which
turns a folder into its files."""
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .commit import (Downloaded, _is_current, _unchanged, failed_result, fetch_versioned,
                     make_key)
from .errors import (DeadlineExceeded, DiskFullError, DownloadError, NotDownloadable, Reason,
                     TransientError, classify_error, permanent_error)
from .google_urls import EXPORT_FORMATS, _export_url, _original_url, parse_google_url
from .http import _clean_extension, _clean_title, _save
from .models import FAILED, NEW, UNCHANGED, UPDATED, Context, DownloadResult, Source
from ..scraper import new_session, timeout_of
from ..url_classifier import GOOGLE_DRIVE

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def fetch_google_drive(source: Source, ctx: Context) -> DownloadResult:
    """A Google Drive file, or a Google Slides/Docs/Sheets file exported, as
    google_drive/<file id>.

    Slides and Docs are exported as PDF and Sheets as xlsx by default (Options.export_format
    overrides it, e.g. 'pptx', 'docx', 'txt', 'csv'). Many Cloud Forum "slides" are actually
    PowerPoint files uploaded to Drive and opened in Slides; Options.original downloads that
    uploaded file (.pptx) instead of a PDF export. Plain Drive file links
    (drive.google.com/file/d/...) are always the original file.

    A folder link is expand()ed into its files, which are then ingest()ed one by one; the
    result sums them up (children). To spread a big folder over several workers, call
    expand() yourself and ingest() each item separately.
    """
    link_kind, file_id = parse_google_url(source.url)
    meta = ctx.drive.get(file_id) if ctx.drive is not None else None
    if _is_folder(link_kind, meta):
        return _fetch_folder(source, ctx)
    if meta is not None:
        return _api_item(source, ctx, meta, link_kind)
    return _public_item(source, ctx, link_kind, file_id)


def _is_folder(link_kind: str, meta: Optional[dict]) -> bool:
    from ..drive_api import FOLDER
    return meta['mimeType'] == FOLDER if meta is not None else link_kind == 'folder'


def _public_item(source: Source, ctx: Context, kind: str, file_id: str) -> DownloadResult:
    """One Drive file through public links. These give no version to compare, so the file is
    downloaded and its hash compared with the stored one (Google's exports are byte-for-byte
    repeatable, so an unchanged deck is reported unchanged)."""
    key = make_key('google_drive', file_id)
    previous = ctx.store.get(key)
    fmt = None if kind == 'file' else (ctx.options.export_format or EXPORT_FORMATS[kind])

    def download(work: Path) -> Downloaded:
        if kind == 'file':
            saved = _save(ctx, _original_url(file_id), None)
        else:
            # Export (or with Options.original, the uploaded file, e.g. a .pptx opened in
            # Slides); if that fails for good, try the other: native Google files have no
            # original, and a big upload's export can fail (Google answers HTTP 432).
            urls = [(_export_url(kind, file_id, fmt), fmt), (_original_url(file_id), None)]
            if ctx.options.original:
                urls.reverse()
            try:
                saved = _save(ctx, urls[0][0], urls[0][1])
            except Exception as exc:
                _raise_unless_permanent(exc)
                log.info('%s failed (%s); trying %s', urls[0][0], exc, urls[1][0])
                try:
                    saved = _save(ctx, urls[1][0], urls[1][1])
                except Exception as second:
                    _raise_unless_permanent(second)  # a deadline, a full disk, a bug: not this
                    raise exc  # both routes failed for good: the first error says more
        return Downloaded([('.' + saved.ext, saved.content)], saved.title, saved.content_type)

    return fetch_versioned(ctx, source, GOOGLE_DRIVE, key, previous, download)


def _fetch_folder(source: Source, ctx: Context) -> DownloadResult:
    """A Drive folder: expand() it, then ingest() every file in it. One file failing doesn't
    stop the rest; if any failed temporarily, TransientError is raised once the rest are done
    (they're stored, so a retry finds them unchanged)."""
    from .api import ingest  # here, not at the top: api imports this module

    expanded = expand(source, ctx)
    if not expanded.items and [f.url for f in expanded.failures] == [source.url]:
        return expanded.failures[0]  # the folder itself couldn't be listed: keep its reason
    folder = _FolderResult(source.url, expanded.title)
    for failure in expanded.failures:
        folder.add(failure)
    for item in expanded.items:
        try:
            folder.add(ingest(item, ctx))
        except (DeadlineExceeded, DiskFullError):
            raise  # the run is over: don't start the folder's other files
        except TransientError as exc:
            folder.add_failure(item.link_text or item.url, item.url, exc)
    return folder.result()


@dataclass
class Expanded:
    """What expand() found: the items to ingest() (JSON-serializable with dataclasses.asdict;
    Source.of() reads them back), and anything that couldn't be listed."""
    items: list[Source]
    failures: list[DownloadResult] = field(default_factory=list)
    title: str = ''                   # the folder's name ('' for a non-folder entry)


def expand(entry: 'Source | dict | str', ctx: Context) -> Expanded:
    """The work items for an entry. A Google Drive folder becomes every file in it and its
    subfolders (each with found_on = the folder it's in, and folder_path = the path, e.g.
    "Slides 2026 / Day 2"); anything else is just itself, with no network request.

    This is how a big folder is split up: expand() it in one step, then ingest() each item
    separately (e.g. a Step Functions Map). Listing is cheap; downloading is not.

    Shortcuts are followed, folders already seen (shortcut loops) skipped, and items with no
    file form (Google Forms, Sites) left out. A folder or entry that can't be listed is a
    FAILED result in `failures`, and the rest are still listed. Raises TransientError if a
    listing fails temporarily (nothing has been stored yet, so just retry).
    """
    source = Source.of(entry)
    kind = source.kind
    if kind != GOOGLE_DRIVE:
        return Expanded(items=[source])
    expanded = Expanded(items=[])
    try:
        link_kind, file_id = parse_google_url(source.url)
        if link_kind != 'folder' and re.search(r'/d/', urlparse(source.url).path):
            return Expanded(items=[source])  # a /d/<id> link is a file: no need to ask
        meta = ctx.drive.get(file_id) if ctx.drive is not None else None
        if not _is_folder(link_kind, meta):
            return Expanded(items=[source])
        if meta is not None:
            expanded.title = meta.get('name', '')
            _expand_api(ctx, meta, (), set(), expanded)
        else:
            _expand_public(ctx, file_id, (), set(), expanded)
    except Exception as exc:
        _add_listing_failure(expanded, source.url, source.link_text, exc)
    log.info('Expanded %s: %d item(s), %d failure(s)', source.url, len(expanded.items),
             len(expanded.failures))
    return expanded


def _add_listing_failure(expanded: Expanded, url: str, name: str, exc: Exception) -> None:
    error = permanent_error(exc)
    if isinstance(error, NotDownloadable):
        log.info('Ignoring %s', error)
        return
    log.warning('Could not list %s: %s', url, error)
    expanded.failures.append(failed_result(url, GOOGLE_DRIVE, error, name))


def _expand_public(ctx: Context, folder_id: str, path: tuple[str, ...], seen: set[str],
                   expanded: Expanded) -> None:
    seen.add(folder_id)
    name, entries = list_google_folder(folder_id, ctx.session)
    if not path:
        expanded.title = name
    folder_url = f'https://drive.google.com/drive/folders/{folder_id}'
    here = path + (name,)
    for entry_name, entry_url in entries:
        try:
            if _no_file_form(entry_url):
                log.info('Ignoring "%s": a Google Form/Site has no file form', entry_name)
                continue
            kind, entry_id = parse_google_url(entry_url)
            if kind == 'folder':
                if entry_id not in seen:
                    _expand_public(ctx, entry_id, here, seen, expanded)
                continue
            _add_file(expanded, seen, entry_id, entry_url, entry_name, folder_url, here)
        except Exception as exc:
            _add_listing_failure(expanded, entry_url, entry_name, exc)


def _add_file(expanded: Expanded, seen: set[str], file_id: str, url: str, name: str,
              folder_url: str, here: tuple[str, ...]) -> None:
    """Add a file listed in a folder to expanded.items -- once, however many times it's listed
    (in two subfolders, or also through a shortcut)."""
    if f'file:{file_id}' in seen:
        return
    seen.add(f'file:{file_id}')
    expanded.items.append(Source(url=url, type=GOOGLE_DRIVE, found_on=[folder_url],
                                 link_text=name, folder_path=' / '.join(here)))


def _no_file_form(url: str) -> bool:
    """A Google Form or Site link: listed in folders, but nothing to download."""
    parsed = urlparse(url)
    return (parsed.netloc == 'sites.google.com'
            or (parsed.netloc == 'docs.google.com' and parsed.path.startswith('/forms/')))


def _expand_api(ctx: Context, meta: dict, path: tuple[str, ...], seen: set[str],
                expanded: Expanded) -> None:
    from ..drive_api import FOLDER, GOOGLE_APPS_PREFIX, SHORTCUT

    seen.add(meta['id'])
    folder_url = _drive_web_url(meta)
    here = path + (meta['name'],)
    for child in ctx.drive.list_folder(meta['id']):
        child_url = _drive_web_url(child)
        try:
            if child['mimeType'] == SHORTCUT:
                child = ctx.drive.get(child['shortcutDetails']['targetId'], resolve_shortcut=False)
                child_url = _drive_web_url(child)
            mime = child['mimeType']
            if mime == FOLDER:
                if child['id'] not in seen:
                    _expand_api(ctx, child, here, seen, expanded)
                continue
            if mime.startswith(GOOGLE_APPS_PREFIX) and mime not in NATIVE_KINDS:
                log.info('Ignoring "%s": a Google %s has no file form', child.get('name'),
                         mime.rsplit('.', 1)[-1])
                continue
            _add_file(expanded, seen, child['id'], child_url, child.get('name', ''), folder_url,
                      here)
        except Exception as exc:
            _add_listing_failure(expanded, child_url, child.get('name', '?'), exc)


class _FolderResult:
    """Collects a folder's items into one DownloadResult. A folder isn't stored itself."""

    def __init__(self, url: str, name: str):
        self.url, self.name = url, name
        self.children: list[DownloadResult] = []
        self.errors: list[str] = []
        self.transient = False

    def add(self, child: DownloadResult) -> None:
        if child.reason == Reason.NOT_DOWNLOADABLE:  # e.g. a Google Form: not a failure
            log.info('Ignoring %s: %s', child.url, child.error)
            return
        self.children.append(child)
        if child.error:
            self.errors.append(f'{child.title or child.url}: {child.error}')

    def add_failure(self, entry_name: str, entry_url: str, exc: Exception) -> None:
        """Record a failed item, so one bad file doesn't stop the rest. Not-downloadable items
        (Forms, Sites) are skipped silently; anything that isn't a download failure re-raises."""
        try:
            error = permanent_error(exc)
        except TransientError as transient:  # recorded; result() raises once the rest are done
            error, self.transient = transient, True
        if isinstance(error, NotDownloadable):
            log.info('Ignoring %s', error)
            return
        log.warning('Failed %s (%s): %s', entry_name, entry_url, error)
        self.errors.append(f'{entry_name}: {error}')
        self.children.append(failed_result(entry_url, GOOGLE_DRIVE, error, entry_name))

    def result(self) -> DownloadResult:
        """The folder's result; raises TransientError if any item failed temporarily."""
        if self.transient:
            raise TransientError(f'Some items in Drive folder "{self.name}" failed temporarily: '
                                 + '; '.join(self.errors))
        return folder_result(self.url, self.name, self.children, self.errors)


def folder_result(url: str, title: str, children: list[DownloadResult], errors: list[str],
                  with_children: bool = True) -> DownloadResult:
    """One DownloadResult for a Drive folder, from its items' results. errors: one line per
    failed item ("<name>: <error>"). Its status: UPDATED if any item is new or updated,
    FAILED if every item failed, else UNCHANGED; failures make its reason PARTIAL (or ERROR
    if all failed). with_children=False leaves the items out (download_all() reports them
    as rows of their own)."""
    statuses = {c.status for c in children}
    if NEW in statuses or UPDATED in statuses:
        status = UPDATED
    elif errors and statuses <= {FAILED}:
        status = FAILED
    else:
        status = UNCHANGED
    error = f'{len(errors)} item(s) failed in "{title}": ' + '; '.join(errors) if errors else ''
    return DownloadResult(
        url=url, type=GOOGLE_DRIVE, status=status, title=title,
        files=[f for c in children for f in c.files], error=error,
        reason=(Reason.PARTIAL if status != FAILED else Reason.ERROR) if error else '',
        children=children if with_children else [])


def list_google_folder(folder_id: str,
                       session: Optional[requests.Session] = None) -> tuple[str, list[tuple[str, str]]]:
    """(folder name, [(entry name, entry URL), ...]) for a public Drive folder.

    Uses Drive's embeddable folder view, a plain HTML listing that needs no API key. Entry URLs
    are file links, Docs/Slides/Sheets links, or folder links (drive.google.com/drive/folders/...).
    """
    url = f'https://drive.google.com/embeddedfolderview?id={folder_id}'
    if session is None:
        with new_session() as session:
            return list_google_folder(folder_id, session)
    response = session.get(url, timeout=timeout_of(session))
    if response.status_code in (401, 403, 404):
        raise DownloadError(
            f'Google returned HTTP {response.status_code} for folder {folder_id}: it is probably '
            'not shared publicly, or it was deleted. Private folders need service-account access.',
            Reason.NOT_FOUND if response.status_code == 404 else Reason.ACCESS_DENIED)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    name = soup.title.get_text(strip=True) if soup.title else folder_id
    entries = []
    for entry in soup.select('.flip-entry'):
        link = entry.find('a', href=True)
        title = entry.select_one('.flip-entry-title')
        if link is not None:
            entries.append((title.get_text(strip=True) if title else link['href'], link['href']))
    return name, entries


# Native Google file types, by MIME type, and the link "kind" whose export defaults they share.
NATIVE_KINDS = {
    'application/vnd.google-apps.presentation': 'presentation',
    'application/vnd.google-apps.document': 'document',
    'application/vnd.google-apps.spreadsheet': 'spreadsheets',
    'application/vnd.google-apps.drawing': 'drawing',
}


EXPORT_FORMATS_API = {**EXPORT_FORMATS, 'drawing': 'pdf'}


def _api_item(source: Source, ctx: Context, meta: dict, link_kind: Optional[str]) -> DownloadResult:
    """One Drive file described by its API metadata. Its modifiedTime (native files) or
    md5Checksum (uploads) is the source_version: if it matches the stored Record, nothing is
    downloaded."""
    from ..drive_api import EXPORT_MIME_TYPES, GOOGLE_APPS_PREFIX

    file_id, name, mime = meta['id'], meta['name'], meta['mimeType']
    key = make_key('google_drive', file_id)
    previous = ctx.store.get(key)
    title = _clean_title(name)

    # Native Google Slides/Docs/Sheets/Drawings: export.
    if mime.startswith(GOOGLE_APPS_PREFIX):
        kind = NATIVE_KINDS.get(mime)
        if kind is None:
            raise NotDownloadable(f'"{name}" is a Google {mime.rsplit(".", 1)[-1]}, '
                                  'which can\'t be downloaded')
        fmt = ctx.options.export_format or EXPORT_FORMATS_API[kind]
        if fmt not in EXPORT_MIME_TYPES:
            raise DownloadError(f'Unsupported export format: {fmt}')

        def export(work: Path) -> Downloaded:
            ctx.check_deadline()
            ctx.downloading()
            content = ctx.store.stage(ctx.guard(ctx.drive.export(meta, EXPORT_MIME_TYPES[fmt])))
            return Downloaded([('.' + fmt, content)], title, EXPORT_MIME_TYPES[fmt])

        return fetch_versioned(ctx, source, GOOGLE_DRIVE, key, previous, export,
                               _drive_version(meta.get('modifiedTime'), fmt), title)

    # An uploaded file (PDF, .pptx, image, ...). If it was linked in the Slides/Docs editor
    # (e.g. a .pptx opened in Slides), export it like the public path does -- the API can't
    # convert uploads, so this uses the public export link, falling back to the original.
    marker = meta.get('md5Checksum') or meta.get('modifiedTime')
    ext = (_clean_extension(Path(name).suffix.lstrip('.'))
           or (mimetypes.guess_extension(mime) or '.bin').lstrip('.'))
    plain = _drive_version(marker, ext)   # the uploaded file itself, as stored
    version = plain
    if link_kind in EXPORT_FORMATS and not ctx.options.original:
        fmt = ctx.options.export_format or EXPORT_FORMATS[link_kind]
        # When Google refuses the export (HTTP 432 for a big file) we store the original and
        # note that in its version. While the file is unchanged that note is current, so the
        # refused export isn't requested again on every run.
        refused = f'{plain};export_refused={fmt}' if plain else ''
        if _is_current(ctx, previous, refused):
            return _unchanged(ctx, source, previous, title=title)

        def public_export(work: Path) -> Optional[Downloaded]:
            try:
                saved = _save(ctx, _export_url(link_kind, file_id, fmt), fmt)
            except Exception as exc:
                _raise_unless_permanent(exc)  # e.g. Google's HTTP 432 for a too-big export
                log.info('No %s export of "%s" (%s); downloading the original', fmt, name, exc)
                return None  # fall back to the original, below
            return Downloaded([('.' + fmt, saved.content)], title, saved.content_type)

        result = fetch_versioned(ctx, source, GOOGLE_DRIVE, key, previous, public_export,
                                 _drive_version(marker, fmt), title)
        if result is not None:
            return result
        version = refused
        if _is_current(ctx, previous, plain):
            # The original was stored before we noted the refusal (or by an `original` run):
            # it's right as it is. Add the note, without downloading it again.
            return _unchanged(ctx, source, previous, title=title, source_version=refused)

    def original(work: Path) -> Downloaded:
        ctx.check_deadline()
        ctx.downloading()
        content = ctx.store.stage(ctx.guard(ctx.drive.download(file_id)))
        return Downloaded([('.' + ext, content)], title, mime)

    return fetch_versioned(ctx, source, GOOGLE_DRIVE, key, previous, original, version, title)


def _raise_unless_permanent(exc: Exception) -> None:
    """Re-raise exc unless it's a permanent download failure (worth trying another route)."""
    error = classify_error(exc)
    if not isinstance(error, DownloadError):
        raise exc


def _drive_version(marker: Optional[str], fmt: str) -> str:
    """source_version for a Drive item: its change marker plus the stored format (so changing
    export_format counts as a change). '' if Drive gave no marker."""
    return f'{marker};format={fmt}' if marker else ''


def _drive_web_url(meta: dict) -> str:
    from ..drive_api import FOLDER
    if meta.get('mimeType') == FOLDER:
        return f'https://drive.google.com/drive/folders/{meta["id"]}'
    return f'https://drive.google.com/file/d/{meta["id"]}/view'
