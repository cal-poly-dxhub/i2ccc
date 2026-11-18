"""Where downloaded content and its metadata are kept.

Each piece of content (a web page, a Drive deck, a video) is stored under a stable *key* derived
from its URL or ID alone -- never its title -- so the same source always lands in the same place
and "have we seen this?" is a single lookup:

    pages/<url hash>          a crawled page:   .txt (title, URL, extracted text)
    files/<url hash>          a file on a website (PDF, Office, media)
    google_drive/<file id>    a Drive file or Slides/Docs/Sheets export
    youtube/<video id>        a video + captions
    panopto/<session id>      a recording + captions

A key's content files are "<key><suffix>" (e.g. "google_drive/1P8j.pdf", "youtube/abc.en.vtt"),
and its Record -- source URL, where it was found, hash, version -- is "<key>.metadata.json".

Writes happen in an order that a crash can't turn into a lie: new files first, then the Record
that describes them, then removal of files the new Record no longer lists. A crash leaves, at
worst, an unlisted leftover file (removed by the next write) -- never a Record that lists a file
that is gone.

Several writers can work on one store at once (overlapping runs, threads). The Record is the
shared state, so writes to it can be conditional: get() tags each Record with a version
(Record.etag), and put()/put_record() take `expected=<the Record you read>` and raise
ConflictError, changing nothing, if someone else has written it since. The caller re-reads,
merges and tries again (see downloader.commit).

Downloads are streamed into a store's staging area (stage()) as they arrive, so a big video
never needs local disk; put() then places the staged content under its key.

Store is the interface the downloader uses; LocalStore keeps everything in a directory, and
aws.S3Store in an S3 bucket -- see tests/test_store.py for the behaviour any Store must have.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Protocol, Union

try:
    import fcntl  # POSIX: lets separate processes share a LocalStore safely
except ImportError:  # pragma: no cover  (Windows: threads in one process only)
    fcntl = None

SCHEMA_VERSION = 1
METADATA_SUFFIX = '.metadata.json'
LOCK_NAME = '.i2cc-store.lock'
STAGING_DIR = '.staging'

# "<prefix>/<id>": lowercase prefix, then an ID of letters, digits, '_' and '-' only. Every key
# is built by the code from hashes and IDs, so this is a safety net against path tricks.
KEY_PATTERN = re.compile(r'[a-z_]+/[A-Za-z0-9_-]{1,128}')
SUFFIX_PATTERN = re.compile(r'(\.[a-z0-9]{1,10}){1,2}')


@dataclass
class Record:
    """Metadata for one stored item: what it is, where it came from, and how to tell whether
    the source has changed since."""
    key: str
    source_url: str                   # the URL it was downloaded from (as linked)
    type: str                         # page, page_text, google_drive, youtube, panopto, file
    files: list[str]                  # store names of its content files, main file first
    content_sha256: str               # SHA-256 of the main file: changes iff the content does
    title: str = ''                   # the item's own title (page title, Drive name, video title)
    link_text: str = ''               # the text of the link it was found through, if any
    context: str = ''                 # heading above a page's link to it (e.g. a session name)
    folder_path: str = ''             # Drive folder it was found in (e.g. "Slides 2026 / Day 2")
    found_on: list[str] = field(default_factory=list)  # pages (or folders) linking to it
    content_type: str = ''            # MIME type of the main file
    source_version: str = ''          # cheap change marker from the source (Drive modifiedTime,
                                      # HTTP ETag/Last-Modified); '' if the source has none
    fetched_at: str = ''              # when this content was downloaded (UTC, ISO 8601)
    captions_checked_at: str = ''     # recordings: when captions were last looked for after the
                                      # download ('' = not since); see downloader.captions_due
    links: Optional[list[str]] = None  # pages: store keys of the documents/videos it linked to
                                       # when last saved (None: not recorded, e.g. older Records)
    unlinked_at: str = ''             # when the last page linking to it stopped linking to it
                                      # ('' = still linked, or never linked from a page)
    is_subscriber_content: bool = False  # True for content behind auth/paywall; controls badge
                                         # display in the RAG UI (blue "CICP" vs gray "Public")
    parent_folder_name: str = ''      # Name of parent collection (e.g. "AWS Training Materials")
    parent_folder_url: str = ''       # Link to parent folder (e.g. Drive folder URL)
    relative_start_time: Optional[int] = None  # Video chunks: offset in seconds from original
                                               # video start; None for non-chunked content
    schema_version: int = SCHEMA_VERSION
    # Not part of the Record's content and never written to it: which stored version of the
    # Record this object is (set by Store.get/put/put_record; '' for one not read from a store).
    # What `expected=` is checked against. Ignored when comparing Records.
    etag: str = field(default='', compare=False, repr=False)

    def to_json(self) -> str:
        data = asdict(self)
        data.pop('etag')
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> 'Record':
        data = json.loads(text)
        known = {f.name for f in fields(cls)} - {'etag'}
        return cls(**{k: v for k, v in data.items() if k in known})


class ConflictError(Exception):
    """A conditional write found the stored Record changed since it was read. Nothing was
    written (and local files given to put() are still where they were)."""


class _Unchecked:
    def __repr__(self) -> str:
        return 'UNCHECKED'


UNCHECKED = _Unchecked()  # put(..., expected=UNCHECKED): write whatever is there


def parse_time(stamp: str) -> Optional[datetime]:
    """A timestamp written by now() as a timezone-aware datetime ('' or malformed: None).
    One without a UTC offset (hand-edited, imported) is taken to be UTC."""
    try:
        parsed = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def check_key(key: str) -> str:
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError(f'Invalid store key: {key!r}')
    return key


def check_suffix(suffix: str) -> str:
    if not SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError(f'Invalid file suffix: {suffix!r}')
    return suffix


@dataclass(frozen=True)
class Staged:
    """Content streamed into a store's staging area by Store.stage(): under no key yet, and
    invisible to readers of the store, until put() places it."""
    ref: str        # where the store keeps it (a path, an object key)
    sha256: str
    size: int


Content = Union[Path, Staged]


def sha256_of(content: Content) -> str:
    if isinstance(content, Staged):
        return content.sha256
    digest = hashlib.sha256()
    with content.open('rb') as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def check_expected(key: str, current: Optional[Record], expected: object) -> None:
    """Raise ConflictError unless the stored Record (current) is the one a write is based on
    (see Store.put's `expected`)."""
    if expected is UNCHECKED:
        return
    if expected is None:
        matches = current is None
    elif isinstance(expected, Record) and expected.etag:
        matches = current is not None and current.etag == expected.etag
    else:
        raise ValueError('expected must be a Record read from this store, None, or UNCHECKED')
    if not matches:
        raise ConflictError(f'{key} was changed by someone else since it was read')


class Store(Protocol):
    def get(self, key: str) -> Optional[Record]:
        """The Record stored for key, or None. Its `etag` says which version this is."""

    def stage(self, chunks: Iterable[bytes]) -> Staged:
        """Write chunks (e.g. a streamed HTTP response) to the staging area, hashing them as
        they go. If iterating them fails, whatever was written is removed and the error
        propagates."""

    def discard(self, staged: Staged) -> None:
        """Remove staged content that wasn't put() (a no-op if it was, or is already gone)."""

    def suffix_of(self, key: str, name: str) -> str:
        """The suffix a stored file of key (a name from Record.files) was put() with."""

    def put(self, record: Record, files: list[tuple[str, Content]],
            keep_other_files: bool = False, expected: object = UNCHECKED) -> Record:
        """Store files -- (suffix, local path or Staged) pairs, main file first -- as key's content,
        then the record, with record.files set to the stored names; then remove files that
        were stored before and aren't listed any more. Local files may be moved. The Record is
        written after the new files and before the removals, so a crash never leaves a Record
        listing a file that is gone.

        expected: the Record this write is based on -- the one you got from get() (None: there
        was none). If the stored Record isn't that one any more, ConflictError is raised and
        nothing is changed, not even the files. Default UNCHECKED: write regardless. On success
        record.etag is the new version.

        keep_other_files: add to the stored content instead -- files already stored under
        other suffixes stay (and stay listed in record.files, before the new ones), and only
        files with the same suffixes as `files` are replaced. For adding e.g. captions to a
        stored video without handling the video again. (A store that keeps files in a remote
        service must list what is already stored under the key itself: `record` may be a
        fresh object.)"""

    def put_record(self, record: Record, expected: object = UNCHECKED) -> None:
        """Replace only the Record (for metadata changes that leave the content as it is).
        expected: as for put()."""

    def delete(self, key: str) -> None:
        """Remove key's content files and Record, if any."""


class LocalStore:
    """A Store in a local directory: <root>/<key><suffix> and <root>/<key>.metadata.json.

    Writes take a lock on a file in the directory, so threads and processes sharing the
    directory can't interleave a check and a write."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._thread_lock = threading.RLock()

    def path(self, name: str) -> Path:
        """Local path of a stored file name (from Record.files)."""
        return self.root / name

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if fcntl is None:  # pragma: no cover
                yield
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / LOCK_NAME, 'a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)  # released when the file is closed
                yield

    def stage(self, chunks: Iterable[bytes]) -> Staged:
        staging = self.root / STAGING_DIR
        staging.mkdir(parents=True, exist_ok=True)
        path = staging / f'{uuid.uuid4().hex}.part'
        digest, size = hashlib.sha256(), 0
        try:
            with path.open('wb') as f:
                for chunk in chunks:
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return Staged(str(path), digest.hexdigest(), size)

    def discard(self, staged: Staged) -> None:
        Path(staged.ref).unlink(missing_ok=True)

    def suffix_of(self, key: str, name: str) -> str:
        return name.removeprefix(key)

    def get(self, key: str) -> Optional[Record]:
        meta = self.root / (check_key(key) + METADATA_SUFFIX)
        try:
            data = meta.read_bytes()
        except FileNotFoundError:
            return None
        record = Record.from_json(data.decode('utf-8'))
        record.etag = hashlib.sha1(data).hexdigest()
        return record

    def put(self, record: Record, files: list[tuple[str, Content]],
            keep_other_files: bool = False, expected: object = UNCHECKED) -> Record:
        check_key(record.key)
        files = [(suffix, Path(c.ref) if isinstance(c, Staged) else c) for suffix, c in files]
        with self._locked():
            current = self.get(record.key)
            check_expected(record.key, current, expected)   # before anything moves
            # Stage every file next to its destination first (a move across devices copies, and
            # can fail -- a full disk): nothing existing is touched until all of them are in.
            staged: list[tuple[Path, Path]] = []
            names = []
            try:
                for suffix, source in files:
                    name = record.key + check_suffix(suffix)
                    dest = self.root / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    partial = dest.with_name(f'.{dest.name}.{uuid.uuid4().hex}.part')
                    staged.append((partial, dest))
                    shutil.move(source, partial)
                    names.append(name)
            except BaseException:
                for partial, _ in staged:
                    partial.unlink(missing_ok=True)
                raise
            for partial, dest in staged:
                os.replace(partial, dest)
            old_files = list(current.files) if current else []
            if keep_other_files:
                names = old_files + [name for name in names if name not in old_files]
            record.files = names
            self._write_record(record)                   # the record, after the new files ...
            if not keep_other_files:                     # ... and only then the removals
                self._remove_unlisted(record)
        return record

    def put_record(self, record: Record, expected: object = UNCHECKED) -> None:
        check_key(record.key)
        with self._locked():
            check_expected(record.key, self.get(record.key), expected)
            self._write_record(record)

    def _write_record(self, record: Record) -> None:
        meta = self.root / (record.key + METADATA_SUFFIX)
        meta.parent.mkdir(parents=True, exist_ok=True)
        data = record.to_json().encode('utf-8')
        # A unique temporary name, and normal permissions (unlike tempfile.mkstemp's 0600),
        # matching the content files.
        tmp = meta.with_name(f'.{meta.name}.{uuid.uuid4().hex}.part')
        try:
            tmp.write_bytes(data)
            os.replace(tmp, meta)
        finally:
            tmp.unlink(missing_ok=True)
        record.etag = hashlib.sha1(data).hexdigest()

    def _remove_unlisted(self, record: Record) -> None:
        """Delete files stored under record's key that the record doesn't list: the ones it
        replaced, and any left behind by a write that was interrupted. (Keys contain no '.',
        so "<name>.*" is only ever this key's files.)"""
        directory = self.root / Path(record.key).parent
        stem = Path(record.key).name
        keep = {Path(name).name for name in record.files} | {stem + METADATA_SUFFIX}
        for path in directory.glob(f'{stem}.*'):
            if path.name not in keep and path.is_file():
                path.unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        """Remove the Record first, then the files -- so a crash in between leaves unlisted
        leftovers (the next write to the key removes them), never a Record listing missing files."""
        with self._locked():
            (self.root / (check_key(key) + METADATA_SUFFIX)).unlink(missing_ok=True)
            directory = self.root / Path(key).parent
            for path in directory.glob(f'{Path(key).name}.*'):
                if path.is_file():
                    path.unlink(missing_ok=True)
