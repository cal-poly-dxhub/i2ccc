"""Google Drive/Docs link parsing and the export/download URLs built from file IDs."""
import re
from urllib.parse import parse_qs, urlparse

from .errors import DownloadError, Reason


# Default export format for native Google files ("Google Slides" etc.).
EXPORT_FORMATS = {'presentation': 'pdf', 'document': 'pdf', 'spreadsheets': 'xlsx'}


GOOGLE_FAILURE_HINT = ('it is probably not shared publicly, or it was deleted. '
                       'Private files need service-account access.')


def parse_google_url(url: str) -> tuple[str, str]:
    """('presentation' | 'document' | 'spreadsheets' | 'file' | 'folder', id) for a Drive/Docs
    URL."""
    parsed = urlparse(url)
    match = re.match(r'^/(presentation|document|spreadsheets|file)/d/([\w-]+)', parsed.path, re.ASCII)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r'^/drive/(?:u/\d+/)?folders/([\w-]+)', parsed.path, re.ASCII)
    if match:
        return 'folder', match.group(1)
    file_id = parse_qs(parsed.query).get('id', [''])[0]
    if file_id and not re.fullmatch(r'[\w-]+', file_id, re.ASCII):
        raise DownloadError(f'Not a recognised Google Drive/Docs link: {url}', Reason.INVALID_URL)
    if file_id and parsed.path in ('/folderview', '/embeddedfolderview'):
        return 'folder', file_id
    if file_id:
        return 'file', file_id  # drive.google.com/open?id=... or /uc?id=...
    raise DownloadError(f'Not a recognised Google Drive/Docs link: {url}', Reason.INVALID_URL)


def _export_url(kind: str, file_id: str, fmt: str) -> str:
    return f'https://docs.google.com/{kind}/d/{file_id}/export?format={fmt}'


def _original_url(file_id: str) -> str:
    # confirm=t skips the "can't scan this large file for viruses" page
    return f'https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t'
