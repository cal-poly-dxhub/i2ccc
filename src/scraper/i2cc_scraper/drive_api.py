"""Google Drive API (v3) access with a service account.

Used by the downloader when service-account credentials are available. Compared with the public
links it uses otherwise, the API can also reach files and folders that are shared only with the
service account, tells files and folders apart from any link, and lists folders officially.

The service account sees files shared publicly ("anyone with the link") and anything explicitly
shared with its email address (DriveClient.email) -- share private folders with that address.

Credentials are passed in, never searched for by library code: DriveClient(key) takes the
service-account key as a dict (e.g. fetched from a secrets manager), a JSON string, or a key
file path. DriveClient.from_env() builds one from $I2CC_GOOGLE_CREDENTIALS_JSON (the key itself)
or $GOOGLE_APPLICATION_CREDENTIALS (a key file). find_credentials() -- also ./credentials.json --
is for the command line only.
"""
import json
import os
from pathlib import Path
from typing import Iterator, Mapping, Optional

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .fileio import CHUNK_SIZE

API = 'https://www.googleapis.com/drive/v3'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
TIMEOUT = 60

FOLDER = 'application/vnd.google-apps.folder'
SHORTCUT = 'application/vnd.google-apps.shortcut'
GOOGLE_APPS_PREFIX = 'application/vnd.google-apps.'  # native Docs/Slides/Sheets/... files

# Export MIME types by file extension, for native Google files.
EXPORT_MIME_TYPES = {
    'pdf': 'application/pdf',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'odp': 'application/vnd.oasis.opendocument.presentation',
    'odt': 'application/vnd.oasis.opendocument.text',
    'ods': 'application/vnd.oasis.opendocument.spreadsheet',
    'txt': 'text/plain',
    'csv': 'text/csv',
    'html': 'text/html',
    'png': 'image/png',
    'svg': 'image/svg+xml',
}

# modifiedTime/version/md5Checksum (md5 only for uploaded files) are the change markers the
# downloader compares to skip unchanged files without downloading them.
FILE_FIELDS = 'id,name,mimeType,size,shortcutDetails,exportLinks,modifiedTime,version,md5Checksum'

CREDENTIALS_JSON_ENV = 'I2CC_GOOGLE_CREDENTIALS_JSON'


class DriveAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class DriveClient:
    """A small Drive API client: file metadata, folder listing, downloads, and exports."""

    def __init__(self, key: Mapping | str | Path, timeout: float = TIMEOUT):
        """key: the service-account key as a dict or JSON string, or the path of a key file.
        Raises ValueError for a malformed key, OSError for an unreadable file."""
        if isinstance(key, str) and key.lstrip().startswith('{'):
            key = json.loads(key)
        if isinstance(key, Mapping):
            credentials = service_account.Credentials.from_service_account_info(
                dict(key), scopes=SCOPES)
        else:
            credentials = service_account.Credentials.from_service_account_file(
                str(key), scopes=SCOPES)
        self.timeout = timeout
        self.email: str = credentials.service_account_email
        self.session = AuthorizedSession(credentials)
        # Retry temporary failures, including Drive's rate limiting (429) and 5xx errors.
        retry = Retry(total=3, backoff_factor=1, backoff_max=60,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({'GET'}), respect_retry_after_header=True,
                      raise_on_status=False)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional['DriveClient']:
        """A client from $I2CC_GOOGLE_CREDENTIALS_JSON (key JSON) or
        $GOOGLE_APPLICATION_CREDENTIALS (key file path), or None if neither is set."""
        env = os.environ if env is None else env
        if env.get(CREDENTIALS_JSON_ENV):
            return cls(env[CREDENTIALS_JSON_ENV])
        if env.get('GOOGLE_APPLICATION_CREDENTIALS'):
            return cls(env['GOOGLE_APPLICATION_CREDENTIALS'])
        return None

    def close(self) -> None:
        self.session.close()

    # -- metadata ----------------------------------------------------------------------------

    def get(self, file_id: str, resolve_shortcut: bool = True) -> dict:
        """The file's metadata (FILE_FIELDS). Shortcuts are followed to their target."""
        meta = self._json(f'{API}/files/{file_id}',
                          {'fields': FILE_FIELDS, 'supportsAllDrives': 'true'})
        if resolve_shortcut and meta.get('mimeType') == SHORTCUT:
            return self.get(meta['shortcutDetails']['targetId'], resolve_shortcut=False)
        return meta

    def list_folder(self, folder_id: str) -> list[dict]:
        """Metadata of every item directly in a folder (not trashed), shortcuts unresolved."""
        items: list[dict] = []
        params = {
            'q': f"'{folder_id}' in parents and trashed = false",
            'fields': f'nextPageToken,files({FILE_FIELDS})',
            'pageSize': '1000',
            'orderBy': 'folder,name',
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        }
        while True:
            page = self._json(f'{API}/files', params)
            items += page.get('files', [])
            if not page.get('nextPageToken'):
                return items
            params['pageToken'] = page['nextPageToken']

    # -- content -----------------------------------------------------------------------------

    def download(self, file_id: str) -> Iterator[bytes]:
        """A non-Google file's content (PDF, PPTX, video, ...), streamed in chunks."""
        return self._chunks(f'{API}/files/{file_id}',
                            {'alt': 'media', 'supportsAllDrives': 'true'})

    def export(self, meta: dict, mime_type: str) -> Iterator[bytes]:
        """A native Google file (Docs/Slides/Sheets) exported as mime_type, streamed in chunks.

        files.export is limited to 10 MB; bigger exports go through the file's exportLinks.
        """
        try:
            yield from self._chunks(f'{API}/files/{meta["id"]}/export', {'mimeType': mime_type})
        except DriveAPIError as exc:  # only raised before the first chunk
            link = (meta.get('exportLinks') or {}).get(mime_type)
            if exc.status != 403 or not link:
                raise
            yield from self._chunks(link, {})

    # -- HTTP --------------------------------------------------------------------------------

    def _json(self, url: str, params: dict) -> dict:
        response = self.session.get(url, params=params, timeout=self.timeout)
        self._raise_for_status(response)
        return response.json()

    def _chunks(self, url: str, params: dict) -> Iterator[bytes]:
        with self.session.get(url, params=params, stream=True, timeout=self.timeout) as response:
            self._raise_for_status(response)
            yield from response.iter_content(CHUNK_SIZE)

    def _raise_for_status(self, response) -> None:
        if response.ok:
            return
        try:
            message = response.json()['error']['message']
        except (ValueError, KeyError, TypeError):
            message = response.text[:200]
        if response.status_code == 404:
            message = (f'{message} -- the file may not exist, or it isn\'t shared with the '
                       f'service account ({self.email}) or publicly')
        raise DriveAPIError(response.status_code,
                            f'Drive API HTTP {response.status_code}: {message}')


CREDENTIALS_FILENAME = 'credentials.json'


def find_credentials() -> Optional[Path]:
    """For the command line: $GOOGLE_APPLICATION_CREDENTIALS if set, else credentials.json in
    the current directory, else None. ($I2CC_GOOGLE_CREDENTIALS_JSON: see cli_drive_client.)"""
    env = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if env:
        return Path(env)
    candidate = Path.cwd() / CREDENTIALS_FILENAME
    return candidate if candidate.is_file() else None


def cli_drive_client() -> Optional[DriveClient]:
    """For the command line and TUI: DriveClient.from_env(), else ./credentials.json, else None
    (public links)."""
    client = DriveClient.from_env()
    if client is None and (path := find_credentials()) is not None:
        client = DriveClient(path)
    return client
