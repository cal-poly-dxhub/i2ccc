import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

YOUTUBE = 'youtube'
PANOPTO = 'panopto'
GOOGLE_DRIVE = 'google_drive'
FILE = 'file'  # a document/media file on any other site, recognized by its extension
ASSET_TYPES = frozenset({YOUTUBE, PANOPTO, GOOGLE_DRIVE, FILE})

FILE_EXTENSIONS = frozenset({
    'pdf', 'ppt', 'pptx', 'pps', 'ppsx', 'doc', 'docx', 'xls', 'xlsx', 'odp', 'odt', 'ods', 'rtf',
    'mp4', 'm4v', 'mov', 'webm', 'mp3', 'm4a', 'wav',
})

# Content-Types of the files above, for links whose URL has no telling extension (e.g.
# "/download?id=7"): the crawler and downloader recognize them by the server's answer instead.
MIME_EXTENSIONS = {
    'application/pdf': 'pdf',
    'application/vnd.ms-powerpoint': 'ppt',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow': 'ppsx',
    'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.ms-excel': 'xls',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.oasis.opendocument.presentation': 'odp',
    'application/vnd.oasis.opendocument.text': 'odt',
    'application/vnd.oasis.opendocument.spreadsheet': 'ods',
    'application/rtf': 'rtf', 'text/rtf': 'rtf',
    'video/mp4': 'mp4', 'video/x-m4v': 'm4v', 'video/quicktime': 'mov', 'video/webm': 'webm',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a',
    'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/wave': 'wav',
}

DOMAIN_REGEX = re.compile(r'^(?:https?://)?(?:[^@/\n?#]+@)?(?:www\.)?([^:/\n?#]+)', re.IGNORECASE)

YOUTUBE_DOMAINS = {'youtube.com', 'youtube-nocookie.com', 'youtu.be'}  # and their subdomains
PANOPTO_DOMAIN_SUFFIXES = ('.panopto.com', '.panopto.eu')


def get_domain(url: str) -> str:
    match = DOMAIN_REGEX.match(url.strip())
    return match.group(1).lower() if match else ''


def is_youtube_host(url: str) -> bool:
    """True for any YouTube address -- youtube.com and its subdomains (m., music., ...),
    youtube-nocookie.com, youtu.be -- whether or not the URL is a single video. Such a URL that
    classify_url() doesn't call a video (a playlist, channel, search or home page) is never
    crawled: those pages are built by JavaScript, so there's nothing to read."""
    domain = get_domain(url)
    return domain in YOUTUBE_DOMAINS or domain.endswith(('.youtube.com', '.youtube-nocookie.com'))


def classify_url(url: str) -> Optional[str]:
    domain = get_domain(url)
    if is_youtube_host(url):
        return _classify_youtube(domain, *_path_and_query(url))
    if domain.endswith(PANOPTO_DOMAIN_SUFFIXES):
        return _classify_panopto(*_path_and_query(url))
    if domain in ('docs.google.com', 'drive.google.com'):
        return _classify_google(domain, *_path_and_query(url))
    if domain and file_extension(url) in FILE_EXTENSIONS:
        return FILE
    return None


def asset_id(url: str) -> Optional[str]:
    """A stable identity for a video/document, so different links to the same one match:
    'youtube:<video id>', 'panopto:<session id>', 'google_drive:<file or folder id>'. None for
    FILE links and anything else (compare those by URL)."""
    kind = classify_url(url)
    if kind is None or kind == FILE:
        return None
    domain = get_domain(url)
    path, query = _path_and_query(url)
    if kind == YOUTUBE:
        if domain == 'youtu.be':
            video = path.strip('/').split('/')[0]
        elif path.rstrip('/') == '/watch':
            video = query['v'][0]
        else:  # /embed/<id>, /shorts/<id>, /live/<id>, /v/<id>
            video = path.split('/')[2]
        return f'{YOUTUBE}:{video}'
    if kind == PANOPTO:
        return f'{PANOPTO}:{query["id"][0].lower()}'
    match = re.search(r'/(?:d|folders)/([\w-]+)', path)
    return f'{GOOGLE_DRIVE}:{match.group(1) if match else query["id"][0]}'


def file_extension(url: str) -> str:
    """Lowercase extension of the path's last segment: '.../Slides.PDF?dl=1' -> 'pdf'."""
    last = _path_and_query(url)[0].rstrip('/').rsplit('/', 1)[-1]
    return last.rsplit('.', 1)[-1].lower() if '.' in last else ''


def extension_for_mime(content_type: str) -> str:
    """The FILE_EXTENSIONS extension for a Content-Type header ('application/pdf; x=y' -> 'pdf'),
    or '' if it isn't one of our document/media types."""
    return MIME_EXTENSIONS.get(content_type.split(';', 1)[0].strip().lower(), '')


def _path_and_query(url: str) -> tuple[str, dict[str, list[str]]]:
    """The URL's path and parsed query string. Scheme-less URLs ('youtu.be/abc') work too."""
    if '://' not in url:
        url = 'https://' + url.lstrip('/')
    parsed = urlparse(url.strip())
    return parsed.path, parse_qs(parsed.query)


def _classify_youtube(domain: str, path: str, query: dict) -> Optional[str]:
    if domain == 'youtu.be':  # youtu.be/<video id>
        return YOUTUBE if re.match(r'^/[\w-]+', path) else None
    if path.rstrip('/') == '/watch' and query.get('v'):  # youtube.com/watch?v=<id>
        return YOUTUBE
    if re.match(r'^/(embed|shorts|live|v)/[\w-]+', path):  # /embed/<id>, /shorts/<id>, ...
        return YOUTUBE
    return None  # channels, playlists, search, ...


def _classify_panopto(path: str, query: dict) -> Optional[str]:
    # <tenant>.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=<session id> (or Embed.aspx)
    if re.search(r'/Pages/(Viewer|Embed)\.aspx$', path, re.IGNORECASE) and query.get('id'):
        return PANOPTO
    return None  # session lists, folders, ...


def _classify_google(domain: str, path: str, query: dict) -> Optional[str]:
    if domain == 'docs.google.com':
        # Slides/Docs/Sheets (native, or uploaded Office files opened in them)
        if re.match(r'^/(presentation|document|spreadsheets|file)/d/[\w-]+', path):
            return GOOGLE_DRIVE
        return None  # Forms, ...
    if re.match(r'^/file/d/[\w-]+', path):  # drive.google.com/file/d/<id>/view
        return GOOGLE_DRIVE
    if re.match(r'^/drive/(u/\d+/)?folders/[\w-]+', path):  # folders, incl. /drive/u/0/folders/
        return GOOGLE_DRIVE
    if path in ('/open', '/uc', '/folderview', '/embeddedfolderview') and query.get('id'):
        return GOOGLE_DRIVE
    return None  # My Drive, shared-with-me, ...
