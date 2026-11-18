"""Download what the scraper finds -- web pages, Drive/Docs files, website files, YouTube and
Panopto recordings -- into a Store (see store.py), with a metadata Record for each.

    ingest(entry, ctx)             -> DownloadResult  One crawl entry (or URL) into ctx.store.
                                                      Raises TransientError for failures that
                                                      are worth retrying later.
    save_page(entry, ctx)          -> DownloadResult  A crawled page's text.
    download(url, out)             -> DownloadResult  ingest() with a one-off Context; never
                                                      raises for a failed download.
    download_all(entries, out)     -> list[...]       Everything in a crawl_for_ingest() result.

A Context holds what a run shares: the store, options, config, HTTP session, and Drive client.

Change detection -- re-running over the same crawl writes only what changed:
  * Each item is stored under a key built from its URL or ID (pages/<url hash>,
    google_drive/<file id>, ...), so its previous Record is one lookup away.
  * Where the source says cheaply whether it changed, nothing is downloaded when it hasn't:
    Drive API modifiedTime/md5Checksum; a website file's ETag/Last-Modified (sent back as
    If-None-Match/If-Modified-Since). YouTube and Panopto recordings don't change once stored.
  * Otherwise the content is downloaded and its SHA-256 compared with the Record's: if it's
    the same, nothing is written. Crawled pages are always in this group: internet2.edu sends
    no ETag and ignores conditional requests (and the crawl fetched them anyway).
  * An unchanged item whose metadata changed (e.g. linked from one more page) gets only its
    Record rewritten. Options.force downloads everything again.

Failures: a permanent one (not shared, deleted, unsupported link, ...) is returned as a FAILED
result with a Reason; a temporary one that outlasted the retries (timeouts, HTTP 429/5xx) makes
ingest() raise TransientError, so an orchestrator can retry it later.

Google Drive is reached through the Drive API if the Context has a DriveClient (which also
reaches files shared only with the service account), else through public links.
"""
# The package split one module into several; these re-exports keep every name that
# downloader.X used to have, so callers and `python -m i2cc_scraper.downloader` work as before.
from ..config import Config  # noqa: F401
from ..scraper import CrawlResult  # noqa: F401
from ..scraper import FAILED as FAILED_PAGE  # noqa: F401
from ..url_classifier import FILE  # noqa: F401
from ..url_classifier import GOOGLE_DRIVE  # noqa: F401
from ..store import KEY_PATTERN  # noqa: F401
from ..store import LocalStore  # noqa: F401
from ..scraper import NOT_FETCHED  # noqa: F401
from ..scraper import PAGE  # noqa: F401
from ..scraper import PAGE_TEXT  # noqa: F401
from ..url_classifier import PANOPTO  # noqa: F401
from ..store import Record  # noqa: F401
from ..store import Store  # noqa: F401
from ..netguard import UnsafeURLError  # noqa: F401
from ..url_classifier import YOUTUBE  # noqa: F401
from ..url_classifier import asset_id  # noqa: F401
from ..url_classifier import classify_url  # noqa: F401
from .. import config as config_module  # noqa: F401
from ..scraper import crawl_for_ingest  # noqa: F401
from ..url_classifier import extension_for_mime  # noqa: F401
from ..scraper import new_session  # noqa: F401
from ..store import now  # noqa: F401
from ..scraper import timeout_of  # noqa: F401
from ..scraper import url_key  # noqa: F401
from .errors import (  # noqa: F401
    DeadlineExceeded, DiskFullError,
    Reason, DownloadError, NotDownloadable, TransientError, classify_error, _for_status)
from ..fileio import CHUNK_SIZE, sweep_stale_dirs  # noqa: F401
from .models import (  # noqa: F401
    NEW, UPDATED, UNCHANGED, FAILED, DEFERRED, DownloadResult, Options, Source, _field, Context)
from .google_urls import (  # noqa: F401
    EXPORT_FORMATS, GOOGLE_FAILURE_HINT, parse_google_url, _export_url, _original_url)
from .commit import (  # noqa: F401
    _log_result, page_key, _url_hash, make_key, _key_for, _predict_key, _commit, _unchanged, _merged_context, _merge_found_on, _is_current, link_keys, unlink_page, captions_due, add_captions, failed_result, deferred_result)
from .http import (  # noqa: F401
    _Saved, _save, _save_once, _http_version, _conditional_headers, _filename_from_headers, _clean_extension, _clean_title)
from .youtube import (  # noqa: F401
    fetch_youtube, _youtube_format, _youtube_error, _js_runtime, _strip_ansi)
from .panopto import (  # noqa: F401
    PANOPTO_ID, fetch_panopto, _panopto_captions)
from .files import (  # noqa: F401
    fetch_file)
from .drive import (  # noqa: F401
    fetch_google_drive, _is_folder, _public_item, _fetch_folder, Expanded, expand, _add_listing_failure, _expand_public, _no_file_form, _expand_api, _FolderResult, list_google_folder, NATIVE_KINDS, EXPORT_FORMATS_API, _api_item, _raise_unless_permanent, _drive_version, _drive_web_url)
from .api import (  # noqa: F401
    ingest, save_page, download, _report_transient, FETCHERS)
from .plan import (  # noqa: F401
    download_all, _folder_summary, dedupe, _group_key, _merge_items, _link_rank, CrawlReport, crawl_into_store)
from .cli import (  # noqa: F401
    main)

import logging  # noqa: E402

log = logging.getLogger('i2cc_scraper.downloader')  # the package's logger, as before
