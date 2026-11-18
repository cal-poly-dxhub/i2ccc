"""Streaming a download into the store's staging area, with conditional requests, and the
filename/extension/title helpers for what servers send."""
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import unquote

import requests

from .errors import DownloadError, Reason
from .google_urls import GOOGLE_FAILURE_HINT
from .models import Context
from ..fileio import CHUNK_SIZE
from ..scraper import timeout_of
from ..store import Staged
from ..url_classifier import extension_for_mime

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


@dataclass
class _Saved:
    content: Staged     # the downloaded file, in the store's staging area
    ext: str            # its extension, checked by _clean_extension
    title: str          # a human name, from Content-Disposition or the fallback name
    content_type: str   # the response's MIME type, without parameters
    version: str        # the response's ETag/Last-Modified as a source_version ('' if none)


def _save(ctx: Context, url: str, ext: Optional[str] = None,
          failure_hint: str = GOOGLE_FAILURE_HINT,
          clean_stem: Optional[Callable[[str], str]] = None,
          fallback_name: Optional[str] = None, known_version: str = '') -> Optional[_Saved]:
    """Stream url into the store's staging area. An error page (HTML, or HTTP 401/403/404/410)
    raises DownloadError with failure_hint. A download interrupted partway is restarted
    (Config.download_attempts).

    known_version: the stored source_version. It's sent as If-None-Match/If-Modified-Since,
    and if the server answers 304 -- or with the same validator -- None is returned without
    downloading. Without known_version, a _Saved is always returned.
    """
    attempts = ctx.config.download_attempts
    for attempt in range(1, attempts + 1):
        ctx.check_deadline()
        try:
            return _save_once(ctx, url, ext, failure_hint, clean_stem, fallback_name,
                              known_version)
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt == attempts:
                raise
            log.warning('Download of %s interrupted (%s); starting again (%d/%d)', url,
                        type(exc).__name__, attempt + 1, attempts)
    raise AssertionError('unreachable')


def _save_once(ctx: Context, url: str, ext: Optional[str], failure_hint: str,
               clean_stem: Optional[Callable[[str], str]], fallback_name: Optional[str],
               known_version: str) -> Optional[_Saved]:
    headers = _conditional_headers(known_version)
    with ctx.session.get(url, stream=True, timeout=timeout_of(ctx.session), headers=headers) as r:
        if r.status_code == 304 and known_version:
            return None
        content_type = r.headers.get('content-type', '')
        if r.status_code in (401, 403, 404, 410) or (r.ok and 'text/html' in content_type):
            reason = Reason.NOT_FOUND if r.status_code in (404, 410) else Reason.ACCESS_DENIED
            raise DownloadError(
                f'HTTP {r.status_code} ({content_type or "?"}) instead of the file: '
                f'{failure_hint}', reason)
        r.raise_for_status()
        version = _http_version(r.headers)
        if known_version and version == known_version:
            return None  # the server ignored the conditional request, but says it's the same
        ctx.downloading()

        name = (_filename_from_headers(r.headers.get('content-disposition', ''))
                or fallback_name or '')
        stem, dot, suffix = name.rpartition('.')
        suffix = _clean_extension(suffix)
        if not dot or not stem or not suffix:  # no usable extension in the name
            stem = name
            suffix = extension_for_mime(content_type) or 'bin'
        stem = _clean_title(stem)
        if clean_stem:
            stem = clean_stem(stem)
        final_ext = ext or suffix

        content = ctx.store.stage(ctx.guard(r.iter_content(CHUNK_SIZE)))  # all of it, or none
    return _Saved(content=content, ext=final_ext, title=stem,
                  content_type=content_type.split(';', 1)[0].strip(), version=version)


def _http_version(headers) -> str:
    """A source_version from a response's validators: 'etag=<ETag>' or
    'last-modified=<date>', '' if it has neither."""
    if headers.get('etag'):
        return f'etag={headers["etag"]}'
    if headers.get('last-modified'):
        return f'last-modified={headers["last-modified"]}'
    return ''


def _conditional_headers(version: str) -> dict:
    """The request headers that ask "has it changed since `version`?"."""
    kind, _, value = version.partition('=')
    if kind == 'etag' and value:
        return {'If-None-Match': value}
    if kind == 'last-modified' and value:
        return {'If-Modified-Since': value}
    return {}


def _filename_from_headers(content_disposition: str) -> str:
    """The filename from a Content-Disposition header, preferring the RFC 5987 filename*= form
    ("filename*=UTF-8''Caf%C3%A9.pdf"), then filename="..." or a bare filename=token.

    The header comes from the server, so only the last path component is kept (RFC 6266 sec.
    4.3): "../../x.pdf" or "C:\\x.pdf" is just "x.pdf". Names are only used as titles; files
    are stored under keys built from IDs, never under these names.
    """
    name = ''
    match = re.search(r"filename\*\s*=\s*([\w!#$%&+^`{}~-]+)'[^']*'([^;\s]+)",
                      content_disposition, re.I)
    if match:
        try:
            name = unquote(match.group(2), encoding=match.group(1), errors='replace')
        except LookupError:  # unknown charset
            name = unquote(match.group(2))
    if not name:
        match = (re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', content_disposition, re.I)
                 or re.search(r'filename\s*=\s*([^;"]+)', content_disposition, re.I))
        name = re.sub(r'\\(["\\])', r'\1', match.group(1)) if match else ''
    return re.split(r'[\\/]', name)[-1].strip()


def _clean_extension(ext: str) -> str:
    """ext lowercased if it looks like a real file extension (1-10 letters/digits), else ''.
    Extensions from server headers and Drive file names become part of stored names."""
    ext = ext.strip().lower()
    return ext if re.fullmatch(r'[a-z0-9]{1,10}', ext) else ''


# Extensions of office documents, which an exported upload's name may still carry.
_OFFICE_EXTENSIONS = r'pptx?|docx?|xlsx?|odp|odt|ods|key|pages'


def _clean_title(name: str) -> str:
    """A file name as a title: control characters removed, and a leftover Office extension
    dropped ("Talk.pptx" -> "Talk"; an exported upload is named e.g. "Talk.pptx.pdf")."""
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip()
    stem, dot, suffix = name.rpartition('.')
    if dot and stem and re.fullmatch(f'{_OFFICE_EXTENSIONS}|pdf', suffix, re.I):
        name = stem  # "Talk.pptx" -> "Talk", "Talk.pptx.pdf" -> "Talk.pptx" ...
    return re.sub(fr'\.({_OFFICE_EXTENSIONS})$', '', name, flags=re.I).strip()  # ... -> "Talk"
