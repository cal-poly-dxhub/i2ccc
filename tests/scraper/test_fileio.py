"""write_atomically: a download is either whole at its destination or not there at all."""
import os
import time

import pytest

from i2cc_scraper.fileio import sweep_stale_dirs, write_atomically
from i2cc_scraper.store import LocalStore


def test_complete_write(tmp_path):
    dest = tmp_path / 'deck.pdf'
    write_atomically(dest, [b'%PDF', b'-1.4'])
    assert dest.read_bytes() == b'%PDF-1.4'
    assert list(tmp_path.iterdir()) == [dest]


def test_interrupted_write_leaves_nothing(tmp_path):
    def chunks():
        yield b'first half'
        raise ConnectionError('dropped')
    with pytest.raises(ConnectionError):
        write_atomically(tmp_path / 'deck.pdf', chunks())
    assert list(tmp_path.iterdir()) == []  # no deck.pdf, no deck.pdf.part


def test_interrupted_write_keeps_an_existing_file(tmp_path):
    dest = tmp_path / 'deck.pdf'
    dest.write_bytes(b'old')

    def chunks():
        yield b'new'
        raise OSError(28, 'No space left on device')
    with pytest.raises(OSError):
        write_atomically(dest, chunks())
    assert dest.read_bytes() == b'old' and list(tmp_path.iterdir()) == [dest]


# --- leftovers of a run that was killed ------------------------------------------------------------

def age(path, seconds: float) -> None:
    """Make path (and everything in it) look like it was last touched `seconds` ago."""
    stamp = time.time() - seconds
    for p in [path, *path.rglob('*')]:
        os.utime(p, (stamp, stamp))


def test_stale_work_directories_are_removed_and_nothing_else(tmp_path):
    dead = tmp_path / 'i2cc-dead'
    dead.mkdir()
    (dead / 'video.mp4.part').write_bytes(b'x' * 100)
    age(dead, 7200)
    fresh = tmp_path / 'i2cc-fresh'
    fresh.mkdir()
    (fresh / 'file.pdf').write_bytes(b'x')
    other = tmp_path / 'somebody-elses'
    other.mkdir()
    age(other, 7200)
    stray_file = tmp_path / 'i2cc-a-file-not-a-directory'
    stray_file.write_bytes(b'x')
    os.utime(stray_file, (1, 1))                                      # old, but a file

    assert sweep_stale_dirs(tmp_path, older_than=3600) == 1
    assert not dead.exists()
    assert fresh.exists() and other.exists() and stray_file.exists()


def test_a_directory_with_a_file_still_being_written_is_not_stale(tmp_path):
    """Writing to a file doesn't change its directory's timestamp: look inside."""
    live = tmp_path / 'i2cc-live'
    live.mkdir()
    download = live / 'download.pdf.part'
    download.write_bytes(b'x')
    os.utime(live, (time.time() - 7200,) * 2)                        # the directory itself looks old
    assert sweep_stale_dirs(tmp_path, older_than=3600) == 0 and live.exists()


def test_sweeping_a_missing_directory_is_harmless(tmp_path):
    assert sweep_stale_dirs(tmp_path / 'nowhere') == 0


def test_work_directories_use_the_prefix_the_sweep_looks_for(tmp_path):
    from i2cc_scraper.downloader import Context
    with Context(LocalStore(tmp_path / 's'), work_dir=tmp_path / 'w') as ctx, ctx.work_dir() as work:
        assert work.name.startswith('i2cc-') and work.parent == tmp_path / 'w'
