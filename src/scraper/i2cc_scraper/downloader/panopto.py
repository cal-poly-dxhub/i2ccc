"""Panopto sessions (public ones, through the podcast download link)."""
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from ..captions import srt_to_vtt
from .commit import Downloaded, _unchanged, add_captions, captions_due, fetch_versioned, make_key
from .errors import DownloadError, Reason
from .http import _save
from .models import Context, DownloadResult, Source
from ..scraper import timeout_of
from ..url_classifier import PANOPTO

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


PANOPTO_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def fetch_panopto(source: Source, ctx: Context) -> DownloadResult:
    """A Panopto session (Viewer.aspx?id=... or Embed.aspx?id=... link) as mp4, through its
    podcast download link (works for sessions viewable without signing in). With captions, its
    English captions too, converted to WebVTT as "<key>.en.vtt", if it has any. A stored session is never
    downloaded again (unless Options.force); one stored without captions is checked again
    for them (see commit.captions_due), fetching only the captions."""
    parsed = urlparse(source.url)
    session_id = parse_qs(parsed.query).get('id', [''])[0].lower()
    if not PANOPTO_ID.match(session_id):
        raise DownloadError(f'No Panopto session id in {source.url}', Reason.INVALID_URL)
    base = f'{parsed.scheme}://{parsed.netloc}/Panopto'
    key = make_key('panopto', session_id)
    previous = ctx.store.get(key)
    if previous is not None and not ctx.options.force:
        if captions_due(ctx, previous):  # stored without captions: look again, captions only
            with ctx.work_dir() as work:
                vtt = _panopto_captions(ctx, base, session_id, work)
                return add_captions(ctx, source, previous, [('.en.vtt', vtt)] if vtt else [])
        return _unchanged(ctx, source, previous)

    def download(work: Path) -> Downloaded:
        video = _save(ctx, f'{base}/Podcast/Download/{session_id}.mp4?mediaTargetType=videoPodcast',
                      'mp4',
                      failure_hint='the session may not exist, or it needs a Panopto sign-in '
                                   '(private sessions need Panopto API access)',
                      clean_stem=lambda stem: stem.removesuffix('_default'))
        files = [('.mp4', video.content)]
        if ctx.options.captions:
            vtt = _panopto_captions(ctx, base, session_id, work)
            if vtt:
                files.append(('.en.vtt', vtt))
        return Downloaded(files, video.title, 'video/mp4')

    return fetch_versioned(ctx, source, PANOPTO, key, previous, download)


def _panopto_captions(ctx: Context, base: str, session_id: str, work: Path) -> Optional[Path]:
    """The session's English captions, saved in work as WebVTT; None if it has none. A caption failure is
    logged but doesn't fail the video download."""
    url = f'{base}/Pages/Transcription/GenerateSRT.ashx?id={session_id}&language=0'
    try:
        response = ctx.session.get(url, timeout=timeout_of(ctx.session))
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning('Could not get Panopto captions for %s: %s', session_id, exc)
        return None
    if not response.content.strip() or 'text/html' in response.headers.get('content-type', ''):
        log.info('No captions for Panopto session %s', session_id)
        return None
    path = work / 'captions.vtt'
    path.write_text(srt_to_vtt(response.content.decode('utf-8', 'replace')), encoding='utf-8')
    return path
