"""Failure types and classify_error(): permanent (DownloadError, with a Reason) vs.
temporary (TransientError)."""
import errno
from enum import StrEnum

import requests

from ..netguard import UnsafeURLError


class Reason(StrEnum):
    """Why a download failed (DownloadResult.reason), so failures can be counted and acted on
    without parsing messages."""
    UNSUPPORTED = 'unsupported'            # not a link we know how to download
    INVALID_URL = 'invalid_url'            # malformed, or refused by netguard (private address)
    NOT_FOUND = 'not_found'                # 404/410, deleted, removed video
    ACCESS_DENIED = 'access_denied'        # private, needs a sign-in, not shared with the
                                           # service account
    NOT_DOWNLOADABLE = 'not_downloadable'  # exists, but has no file form (Google Form, Site)
    BLOCKED = 'blocked'                    # the site refused us as a bot (YouTube)
    AUTH = 'auth'                          # our own credentials were rejected
    PARTIAL = 'partial'                    # a folder in which some items failed
    UNAVAILABLE = 'unavailable'            # a temporary failure that outlasted the retries
    NOT_FETCHED = 'not_fetched'            # a crawl stopped (max_pages/time limit) before it
    TIMEOUT = 'timeout'                    # the run's time budget ran out (Context budget)
    DISK_FULL = 'disk_full'                # no space left to download into
    ERROR = 'error'                        # anything else


class DownloadError(Exception):
    """A permanent failure: retrying soon won't help."""

    def __init__(self, message: str, reason: Reason = Reason.ERROR):
        super().__init__(message)
        self.reason = Reason(reason)


class NotDownloadable(DownloadError):
    """The item exists but has no file form (e.g. a Google Form or Site). Folders skip these."""

    def __init__(self, message: str):
        super().__init__(message, Reason.NOT_DOWNLOADABLE)


class TransientError(Exception):
    """A temporary failure (timeout, connection error, HTTP 429/5xx) that outlasted the
    in-process retries. ingest() raises it so the caller can retry the item later."""
    reason = Reason.UNAVAILABLE


class DeadlineExceeded(TransientError):
    """The run's time budget (Context.budget) ran out. Nothing is half-written; run it again."""
    reason = Reason.TIMEOUT


class DiskFullError(TransientError):
    """There is no space left to download into. Not the item's fault: a fresh run (or a
    bigger /tmp) will do. Callers should stop starting items, since the rest would fail too."""
    reason = Reason.DISK_FULL


def classify_error(exc: BaseException) -> 'DownloadError | TransientError | None':
    """exc as a DownloadError (permanent) or TransientError (retry later), or None if it isn't
    a download failure at all (a bug, or a local problem such as a permissions error). A full
    disk is a TransientError (DiskFullError): not the item's fault, and a callback loop should
    stop on it."""
    from google.auth import exceptions as google_errors

    from ..drive_api import DriveAPIError

    if isinstance(exc, (DownloadError, TransientError)):
        return exc
    if isinstance(exc, (UnsafeURLError, requests.exceptions.InvalidURL,
                        requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema)):
        return DownloadError(str(exc), Reason.INVALID_URL)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return _for_status(exc.response.status_code, str(exc))
    if isinstance(exc, requests.exceptions.SSLError):  # a bad certificate won't fix itself soon
        return DownloadError(f'TLS error: {exc}')
    if isinstance(exc, (requests.ConnectionError, requests.Timeout,
                        requests.exceptions.ChunkedEncodingError)):
        return TransientError(f'{type(exc).__name__}: {exc}')
    if isinstance(exc, requests.RequestException):
        return DownloadError(str(exc))
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return DiskFullError(f'No space left to download into: {exc}')
    if isinstance(exc, DriveAPIError):
        if exc.status == 403 and 'ratelimit' in str(exc).lower().replace(' ', ''):
            return TransientError(str(exc))  # Drive reports rate limiting as 403
        return _for_status(exc.status, str(exc))
    if isinstance(exc, google_errors.TransportError):
        return TransientError(f'Google auth: {exc}')
    if isinstance(exc, google_errors.GoogleAuthError):
        return DownloadError(f'Google service-account credentials rejected: {exc}', Reason.AUTH)
    return None


def permanent_error(exc: BaseException) -> DownloadError:
    """exc as a permanent DownloadError. Anything else is raised: a temporary failure as a
    TransientError (chained to exc), and exc itself if it isn't a download failure at all."""
    error = classify_error(exc)
    if error is None:
        raise exc
    if isinstance(error, TransientError):
        if error is exc:
            raise exc
        raise error from exc
    return error


def _for_status(status: int, message: str) -> 'DownloadError | TransientError':
    if status in (401, 403):
        return DownloadError(message, Reason.ACCESS_DENIED)
    if status in (404, 410):
        return DownloadError(message, Reason.NOT_FOUND)
    if status == 429 or status >= 500:
        return TransientError(message)
    return DownloadError(message)
