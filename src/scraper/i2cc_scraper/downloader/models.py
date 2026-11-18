"""The data a run passes around: Options, Source (a work item), DownloadResult, and
Context (what a run shares: store, options, config, session, Drive client)."""
import logging
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, TYPE_CHECKING

import requests

from .. import config as config_module
from ..config import Config
from ..fileio import WORK_DIR_PREFIX
from .errors import DeadlineExceeded
from ..scraper import new_session
from ..url_classifier import classify_url
from ..store import Store

if TYPE_CHECKING:
    from ..drive_api import DriveClient

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


# DownloadResult.status
NEW = 'new'              # stored for the first time


UPDATED = 'updated'      # the source changed; the new version replaced the old


UNCHANGED = 'unchanged'  # already stored and the source hasn't changed: nothing downloaded or


                         # nothing written (at most the Record's metadata)
FAILED = 'failed'        # see `error` and `reason`
DEFERRED = 'deferred'    # not attempted: the run ran out of time (or disk) before reaching it.
                         # Nothing was changed; run again to get to it.


@dataclass
class DownloadResult:
    url: str
    type: str                                       # YOUTUBE, GOOGLE_DRIVE, PAGE, ...
    status: str = FAILED                            # NEW, UPDATED, UNCHANGED, FAILED
    key: str = ''                                   # store key ('' for folders / bad links)
    files: list[str] = field(default_factory=list)  # store names, main file first
    title: str = ''
    error: str = ''                                 # FAILED, or folders with failed items
    reason: str = ''                                # a Reason, when error is set
    children: list['DownloadResult'] = field(default_factory=list)  # folders: their items

    @property
    def skipped(self) -> bool:
        return self.status == UNCHANGED


@dataclass
class Options:
    """What to download, from the command-line flags or a job's input."""
    export_format: Optional[str] = None   # Slides/Docs/Sheets export (default pdf; Sheets xlsx)
    original: bool = False                # Drive: the uploaded file (e.g. .pptx), not an export
    max_height: int = 360                 # YouTube: video height, at most (low: ingestion
                                          # needs the content, not the picture quality)
    audio_only: bool = False              # YouTube: audio only
    captions: bool = True                 # YouTube/Panopto: also English captions
    force: bool = False                   # download again even if the source looks unchanged


@dataclass
class Source:
    """What we know about a URL before downloading it, usually from its CrawlResult."""
    url: str
    type: str = ''                    # crawl type; '' (or a page type) = classify_url(url)
    found_on: list[str] = field(default_factory=list)  # pages (or folders) that link to it
    link_text: str = ''               # a page link's text, or the name in a Drive folder
    context: str = ''                 # heading above a page's link to it (e.g. a session name)
    folder_path: str = ''             # Drive folder it's in (e.g. "Slides 2026 / Day 2")

    @property
    def kind(self) -> str:
        """Which fetcher handles it: its crawl type if a fetcher is registered for that type
        (api.FETCHERS, including ones added at run time), else what classify_url() makes of
        the URL ('' if nothing does -- e.g. a page)."""
        from .api import FETCHERS  # at call time: api imports this module
        return self.type if self.type in FETCHERS else (classify_url(self.url) or '')

    @classmethod
    def of(cls, entry: 'Source | dict | str | object') -> 'Source':
        """From a URL, a CrawlResult or its dict (i2cc-scrape --depth JSON), or a Source's
        dict -- so work items survive a JSON round trip between steps."""
        if isinstance(entry, Source):
            return entry
        if isinstance(entry, str):
            return cls(url=entry)
        link_text = _field(entry, 'link_text')  # a Source's own dict (asdict) ...
        if link_text is None:
            link_text = _field(entry, 'title', '')  # ... or a CrawlResult's, whose title it is
        found_on = _field(entry, 'found_on') or []
        if isinstance(found_on, str):  # a CrawlResult's single page
            found_on = [found_on]
        return cls(url=_field(entry, 'url'), type=_field(entry, 'type', '') or '',
                   found_on=list(found_on), link_text=link_text or '',
                   context=_field(entry, 'context', '') or '',
                   folder_path=_field(entry, 'folder_path', '') or '')


def _field(entry, name: str, default=None):
    return entry.get(name, default) if isinstance(entry, dict) else getattr(entry, name, default)


def _seconds() -> float:
    """The clock the time budget runs on (a seam for tests)."""
    return time.monotonic()


class Context:
    """What a run shares: where things are stored, what to download, and the connections.
    Build one per run (or per Lambda container) and close it when done; it's also a context
    manager. A session or drive passed in is not closed by close().

    Time budget. budget=<seconds> says how long the run may take -- in a Lambda, the invocation's
    remaining time (get_remaining_time_in_millis) less a margin for reporting. Then:
      * can_start() is False once less than `reserve` seconds are left: a loop over items should
        stop starting new ones and report the rest as DEFERRED;
      * work already underway may use the rest: transfers check the clock as they go and stop
        cleanly (DeadlineExceeded) at zero -- nothing half-written, never killed mid-write;
      * retry sleeps and request timeouts are capped by what is left.
    Without a budget nothing is limited."""

    def __init__(self, store: Store, options: Optional[Options] = None,
                 config: Optional[Config] = None, drive: Optional['DriveClient'] = None,
                 session: Optional[requests.Session] = None,
                 work_dir: Optional[Path | str] = None,
                 on_download: Optional[Callable[[], None]] = None,
                 budget: Optional[float] = None, reserve: float = 30.0):
        self.store = store
        self.on_download = on_download
        self.options = options or Options()
        self.config = config or config_module.default()
        self.drive = drive
        self.deadline = None if budget is None else _seconds() + budget
        self.reserve = reserve
        self.session = session or new_session(self.config, time_left=self.time_left)
        self._own_session = session is None
        self._work_root = Path(work_dir) if work_dir else None  # default: the system temp dir

    def time_left(self) -> Optional[float]:
        """Seconds left in the time budget (negative once it's over), or None: no budget."""
        return None if self.deadline is None else self.deadline - _seconds()

    def can_start(self) -> bool:
        """Whether there is time to begin another item: more than `reserve` seconds left."""
        left = self.time_left()
        return left is None or left > self.reserve

    def check_deadline(self) -> None:
        """Raise DeadlineExceeded if the time budget is used up."""
        left = self.time_left()
        if left is not None and left <= 0:
            raise DeadlineExceeded('The run ran out of time; nothing was left half-done. '
                                   'Run it again to carry on.')

    def guard(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        """chunks, but raising DeadlineExceeded when the budget runs out between two of them."""
        for chunk in chunks:
            self.check_deadline()
            yield chunk

    def downloading(self) -> None:
        """Called just before file bytes are fetched from a source -- not for a metadata check
        or a "not modified" answer, which is how a caller (the TUI) can tell a download from a
        skip. Calls on_download, if set; a failing callback is logged, never fatal."""
        if self.on_download is not None:
            try:
                self.on_download()
            except Exception:
                log.warning('on_download callback failed', exc_info=True)

    def close(self) -> None:
        if self._own_session:
            self.session.close()

    def __enter__(self) -> 'Context':
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def work_dir(self) -> Iterator[Path]:
        """A temporary directory to download one item into; removed afterwards."""
        if self._work_root is not None:
            self._work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._work_root, prefix=WORK_DIR_PREFIX) as path:
            yield Path(path)
