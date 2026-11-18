"""Files linked directly from websites (PDFs, Office documents, media)."""
from pathlib import Path
from urllib.parse import unquote, urlparse

from .commit import NOT_MODIFIED, Downloaded, _key_for, fetch_versioned
from .http import _save
from .models import Context, DownloadResult, Source
from ..url_classifier import FILE


def fetch_file(source: Source, ctx: Context) -> DownloadResult:
    """A file linked directly from a website (e.g. a PDF in WordPress's uploads), stored as
    files/<url hash>. Its title is its name from the Content-Disposition header, else the URL.

    The server's ETag/Last-Modified is kept as the Record's source_version and sent back as
    If-None-Match/If-Modified-Since next time: a 304, or the same validator, means unchanged
    without downloading. Without validators, the file is downloaded and its hash compared."""
    key = _key_for(FILE, source.url)
    previous = ctx.store.get(key)
    known = previous.source_version if previous and not ctx.options.force else ''
    url_name = unquote(urlparse(source.url).path.rstrip('/').rsplit('/', 1)[-1])

    def download(work: Path):
        saved = _save(ctx, source.url, None,
                      failure_hint='the link may be broken, or the file needs a sign-in',
                      fallback_name=url_name, known_version=known)
        if saved is None:
            return NOT_MODIFIED
        return Downloaded([('.' + saved.ext, saved.content)], saved.title, saved.content_type,
                          version=saved.version)

    return fetch_versioned(ctx, source, FILE, key, previous, download)
