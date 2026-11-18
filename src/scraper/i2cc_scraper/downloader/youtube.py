"""YouTube videos (via yt-dlp), at a low quality by default."""
import logging
import re
import shutil
from pathlib import Path
from typing import Iterator, Optional

from ..captions import clean_vtt_file
from ..fileio import CHUNK_SIZE
from ..scraper import timeout_of
from .commit import (Downloaded, _is_current, _key_for, add_captions, captions_due,
                     fetch_versioned)
from .errors import DownloadError, Reason, TransientError
from .models import Context, DownloadResult, Source
from ..url_classifier import YOUTUBE

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def fetch_youtube(source: Source, ctx: Context) -> DownloadResult:
    """A YouTube video as mp4, at most Options.max_height pixels tall (default 360), or with
    audio_only just its audio (m4a). With captions, English subtitles too -- uploaded ones if
    the video has them, otherwise YouTube's automatic captions -- as "<key>.en.vtt".

    The quality is the Record's source_version: a stored video is downloaded again only if
    it was stored at another quality (or with Options.force) -- published videos don't
    change. See _youtube_format() for which file is picked.

    yt-dlp picks the format and saves the captions; a single-file format is then streamed
    straight into the store (no local copy of the video). One that needs ffmpeg to merge
    separate video and audio streams is downloaded by yt-dlp into the work directory."""
    import yt_dlp  # imported here so the rest works without yt-dlp installed

    opts = ctx.options
    key = _key_for(YOUTUBE, source.url)
    video_url = _watch_url(key)
    previous = ctx.store.get(key)
    version = 'audio' if opts.audio_only else f'video<={int(opts.max_height)}p'

    if _is_current(ctx, previous, version) and captions_due(ctx, previous):
        return _recheck_captions(source, ctx, previous)

    def download(work: Path) -> Downloaded:
        fmt = _youtube_format(int(opts.max_height), opts.audio_only,
                              bool(shutil.which('ffmpeg')))
        ctx.downloading()
        try:
            with yt_dlp.YoutubeDL({**_ydl_options(ctx, work, fmt), 'skip_download': True}) as ydl:
                info = ydl.extract_info(video_url, download=True)  # picks format, saves captions
            chosen = _streamable(info)
            if chosen is None:
                with yt_dlp.YoutubeDL(_ydl_options(ctx, work, fmt)) as ydl:
                    info = ydl.extract_info(video_url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            raise _youtube_error(exc) from exc

        if chosen is not None:
            ext = re.sub(r'[^a-z0-9]', '', str(chosen.get('ext') or 'mp4').lower()) or 'mp4'
            video = ctx.store.stage(ctx.guard(_ranged_chunks(
                ctx, chosen['url'], chosen.get('http_headers') or {})))
            files = [('.' + ext, video)]
        else:
            downloads = [Path(d['filepath']) for d in info.get('requested_downloads', [])
                         if d.get('filepath')]
            if not downloads or not downloads[0].exists():
                raise DownloadError(f'yt-dlp saved no file for {source.url}')
            files = [('.' + downloads[0].suffix.lstrip('.').lower(), downloads[0])]
        files += _subtitle_files(info)
        content_type = 'audio/mp4' if opts.audio_only else 'video/mp4'
        return Downloaded(files, info.get('title', ''), content_type)

    return fetch_versioned(ctx, source, YOUTUBE, key, previous, download, version)


def _streamable(info: dict) -> Optional[dict]:
    """The format yt-dlp chose, if it is one file served over plain HTTP(S) -- not separate
    video and audio streams to merge, nor a segmented stream (HLS/DASH)."""
    chosen = (info.get('requested_downloads') or [{}])[0]
    if chosen.get('requested_formats') or chosen.get('protocol') not in ('http', 'https'):
        return None
    return chosen if chosen.get('url') else None


RANGE_SIZE = 10 << 20  # YouTube throttles one long request; yt-dlp also asks for ranges this size


def _ranged_chunks(ctx: Context, url: str, headers: dict) -> Iterator[bytes]:
    """The file at url, requested RANGE_SIZE bytes at a time."""
    start = 0
    while True:
        request = {**headers, 'Range': f'bytes={start}-{start + RANGE_SIZE - 1}'}
        with ctx.session.get(url, headers=request, stream=True,
                             timeout=timeout_of(ctx.session)) as response:
            if response.status_code == 416:  # the last range ended exactly at the end
                return
            response.raise_for_status()
            got = 0
            for chunk in response.iter_content(CHUNK_SIZE):
                got += len(chunk)
                yield chunk
            if response.status_code != 206:  # the server ignored the range: that was all of it
                return
            total = response.headers.get('content-range', '').rpartition('/')[2]
        start += got
        if got < RANGE_SIZE or (total.isdigit() and start >= int(total)):
            return


def _watch_url(key: str) -> str:
    """The plain link to the video stored under key ("youtube/<id>"). yt-dlp is given this, not
    the link we were handed: a link with &list=... would otherwise make it download the whole
    playlist, and &t=, &index= etc. are noise."""
    return f'https://www.youtube.com/watch?v={key.split("/", 1)[1]}'


def _ydl_options(ctx: Context, work: Path, fmt: Optional[str]) -> dict:
    """yt-dlp options: fetch format `fmt` (None: no particular one) into work, with English
    captions if Options.captions."""
    options = {
        'noplaylist': True,  # just the video, even if it's in a playlist
        'progress_hooks': [lambda _status: ctx.check_deadline()],  # stop cleanly when time's up
        'merge_output_format': 'mp4',
        'outtmpl': {'default': str(work / 'video.%(ext)s')},
        'writesubtitles': ctx.options.captions,
        'writeautomaticsub': ctx.options.captions,
        'subtitleslangs': ['en'],
        'subtitlesformat': 'vtt',
        'quiet': True,
        'noprogress': True,
        'no_warnings': False,
        'socket_timeout': ctx.config.timeout,
        'http_headers': {'User-Agent': ctx.config.user_agent},
    }
    if fmt:
        options['format'] = fmt
    runtime = _js_runtime()
    if runtime:
        options['js_runtimes'] = {runtime: {}}  # YouTube needs JavaScript for formats
    return options


def _subtitle_files(info: dict) -> list[tuple[str, Path]]:
    """(suffix, path) for each caption file yt-dlp saved for the video described by info.
    YouTube's automatic captions repeat every line three times (captions.py); each file is
    cleaned here, before it's stored, so nothing downstream sees the repeats."""
    files = []
    for lang, sub in (info.get('requested_subtitles') or {}).items():
        if sub.get('filepath') and Path(sub['filepath']).exists():
            lang = re.sub(r'[^a-z0-9]', '', lang.lower())[:10] or 'en'
            clean_vtt_file(Path(sub['filepath']))
            files.append((f'.{lang}.vtt', Path(sub['filepath'])))
    return files


def _recheck_captions(source: Source, ctx: Context, previous) -> DownloadResult:
    """A stored video has no captions yet: look for them again -- YouTube adds automatic ones
    hours after upload -- and add them if they exist, without fetching the video again. A
    failure here is logged, not an error (the video itself is stored and fine), and counts as
    a check, so a rate-limited or private video is tried once per Config.caption_recheck_hours
    rather than on every run."""
    import yt_dlp

    with ctx.work_dir() as work:
        options = {**_ydl_options(ctx, work, None), 'skip_download': True,
                   'writesubtitles': True, 'writeautomaticsub': True}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(_watch_url(previous.key), download=True)
        except yt_dlp.utils.DownloadError as exc:
            log.warning('Could not re-check captions for %s: %s', source.url,
                        _youtube_error(exc))
            return add_captions(ctx, source, previous, [])
        return add_captions(ctx, source, previous, _subtitle_files(info))


def _youtube_format(max_height: int, audio_only: bool, have_ffmpeg: bool) -> str:
    """A yt-dlp format selector: the first alternative available wins.

    Ready-made mp4s (video and audio in one file; YouTube still offers these at low
    resolutions) come first, because they need no ffmpeg to merge -- one less dependency in a
    Lambda image. Separate video + audio streams, merged by ffmpeg, are next, only if ffmpeg
    is installed. If nothing is at most max_height tall, the lowest quality is taken, not the
    highest. H.264 (avc1) is preferred: it plays everywhere."""
    if audio_only:
        return 'ba[ext=m4a]/ba'
    h = max_height
    single = [f'b[height<={h}][ext=mp4][vcodec^=avc1]', f'b[height<={h}][ext=mp4]',
              f'b[height<={h}]']
    merged = [f'bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]', f'bv*[height<={h}]+ba']
    lowest = ['wv*+wa', 'w'] if have_ffmpeg else ['w']
    return '/'.join(single + (merged if have_ffmpeg else []) + lowest)


def _youtube_error(exc: Exception) -> 'DownloadError | TransientError':
    message = _strip_ansi(str(exc)).removeprefix('ERROR: ')
    lower = message.lower()
    if 'confirm your age' in lower or 'private video' in lower or 'members-only' in lower:
        return DownloadError(message, Reason.ACCESS_DENIED)
    if 'not a bot' in lower or 'sign in to confirm' in lower:
        return DownloadError(message, Reason.BLOCKED)
    if any(s in lower for s in ('video unavailable', 'has been removed', 'does not exist',
                                'http error 404')):
        return DownloadError(message, Reason.NOT_FOUND)
    if any(s in lower for s in ('http error 429', 'http error 5', 'timed out',
                                'connection reset', 'temporary failure')):
        return TransientError(message)
    return DownloadError(message)


def _js_runtime() -> Optional[str]:
    """A JavaScript runtime yt-dlp can use, preferring the one it recommends."""
    return next((r for r in ('deno', 'node', 'bun') if shutil.which(r)), None)


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', text)
