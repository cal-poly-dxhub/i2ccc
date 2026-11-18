"""Writing downloads to disk."""
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before

CHUNK_SIZE = 1 << 20  # 1 MiB: how much of a download is read and written at a time
WORK_DIR_PREFIX = 'i2cc-'  # the name every download's temporary directory starts with


def write_atomically(dest: Path, chunks: Iterable[bytes]) -> None:
    """Write chunks (e.g. a streamed HTTP response) to dest. They go to "<dest>.part" first,
    which is renamed to dest only when complete; if anything fails -- the connection drops,
    the disk fills -- the partial file is deleted, so dest is either whole or absent."""
    partial = dest.with_name(dest.name + '.part')
    try:
        with partial.open('wb') as f:
            for chunk in chunks:
                f.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)


def sweep_stale_dirs(root: Optional[Path | str] = None, older_than: float = 3600.0,
                     prefix: str = WORK_DIR_PREFIX) -> int:
    """Delete leftover work directories: directories under `root` (default: the system temp
    directory) whose name starts with `prefix` and in which nothing has been touched for
    `older_than` seconds. They are what a run killed mid-download -- a Lambda timeout, an
    out-of-memory kill -- leaves behind, and a reused container would keep filling its /tmp
    with them. Returns how many were removed.

    Call it once when a long-lived process starts. `older_than` must be longer than the longest
    download that may be running: a directory counts as in use while any file in it is being
    written (a directory's own timestamp doesn't change then, so the files are checked too)."""
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - older_than
    removed = 0
    try:
        candidates = [p for p in base.iterdir()
                      if p.name.startswith(prefix) and p.is_dir() and not p.is_symlink()]
    except OSError:
        return 0
    for directory in candidates:
        try:
            newest = max([directory.stat().st_mtime] + [p.stat().st_mtime
                                                        for p in directory.rglob('*')])
            if newest < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
                log.info('Removed stale work directory %s', directory)
        except OSError:
            continue  # it vanished, or isn't ours to read: leave it
    return removed
