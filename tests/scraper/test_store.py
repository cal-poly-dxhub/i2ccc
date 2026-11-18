"""The behaviour every Store must have, parametrized over the implementations: LocalStore, and
S3Store against AWS Emulator (skipped without it; see conftest.emulator)."""
import errno
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from i2cc_scraper.store import UNCHECKED, ConflictError, LocalStore, Record

STORES = ['local', 's3']


@pytest.fixture(params=STORES)
def any_store(request, tmp_path):
    if request.param == 's3':
        return request.getfixturevalue('s3_store')
    return LocalStore(tmp_path / 'store')


def record(key='pages/abc123', **fields) -> Record:
    return Record(key=key, source_url='https://example.org/a', type='page', files=[],
                  content_sha256='0' * 64, **fields)


def local_file(tmp_path, name, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def name(store, key, suffix) -> str:
    """What the store names key's file with this suffix (for the records built by record())."""
    if isinstance(store, LocalStore):
        return key + suffix
    return store.file_name(record(key), suffix)


def read(store, stored_name) -> bytes:
    if isinstance(store, LocalStore):
        return store.path(stored_name).read_bytes()
    return store.read(stored_name)


def exists(store, stored_name) -> bool:
    if isinstance(store, LocalStore):
        return store.path(stored_name).exists()
    try:
        store.read(stored_name)
        return True
    except store.client.exceptions.NoSuchKey:
        return False


def raw_record(store, key) -> str:
    if isinstance(store, LocalStore):
        return store.path(key + '.metadata.json').read_text()
    return store.client.get_object(Bucket=store.bucket,
                                   Key=store.prefix + key + '.metadata.json')['Body'].read().decode()


def test_missing_key_is_none(any_store):
    assert any_store.get('pages/nothing') is None


def test_put_then_get(any_store, tmp_path):
    stored = any_store.put(record(title='A', found_on=['https://example.org/']), [
        ('.txt', local_file(tmp_path, 'a.txt', b'text'))])
    got = any_store.get('pages/abc123')
    assert got == stored and len(got.files) == 1
    assert read(any_store, got.files[0]) == b'text'


def test_staged_content_is_put_and_then_gone_from_staging(any_store, tmp_path):
    staged = any_store.stage(iter([b'%PDF', b'-1.4']))
    assert (staged.size, len(staged.sha256)) == (8, 64)
    stored = any_store.put(record('google_drive/deck'), [('.pdf', staged)])
    assert read(any_store, stored.files[0]) == b'%PDF-1.4'
    any_store.discard(staged)                                    # already gone: a no-op


def test_staging_that_fails_leaves_nothing(any_store):
    def chunks():
        yield b'part'
        raise ConnectionError('dropped')
    with pytest.raises(ConnectionError):
        any_store.stage(chunks())
    assert any_store.get('google_drive/deck') is None


def test_put_replaces_content_and_removes_files_no_longer_used(any_store, tmp_path):
    any_store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd.pdf', b'v1'))])
    any_store.put(record('google_drive/deck'), [('.pptx', local_file(tmp_path, 'd.pptx', b'v2'))])
    got = any_store.get('google_drive/deck')
    assert got.files == [name(any_store, 'google_drive/deck', '.pptx')]
    assert not exists(any_store, name(any_store, 'google_drive/deck', '.pdf'))


def test_put_can_add_files_and_keep_the_others(any_store, tmp_path):
    video, vtt = name(any_store, 'youtube/v', '.mp4'), name(any_store, 'youtube/v', '.en.vtt')
    any_store.put(record('youtube/v'), [('.mp4', local_file(tmp_path, 'v.mp4', b'video'))])
    stored = any_store.get('youtube/v')
    any_store.put(stored, [('.en.vtt', local_file(tmp_path, 's.vtt', b'WEBVTT'))],
                  keep_other_files=True)
    assert any_store.get('youtube/v').files == [video, vtt]
    assert read(any_store, video) == b'video'
    # adding a file under a suffix that exists replaces just that file
    again = any_store.get('youtube/v')
    any_store.put(again, [('.en.vtt', local_file(tmp_path, 's2.vtt', b'WEBVTT 2'))],
                  keep_other_files=True)
    assert any_store.get('youtube/v').files == [video, vtt]
    assert read(any_store, vtt) == b'WEBVTT 2'


def test_put_record_changes_only_metadata(any_store, tmp_path):
    stored = any_store.put(record(), [('.txt', local_file(tmp_path, 'a.txt', b'text'))])
    stored.found_on = ['https://example.org/one', 'https://example.org/two']
    any_store.put_record(stored)
    assert any_store.get('pages/abc123').found_on == stored.found_on
    assert read(any_store, name(any_store, 'pages/abc123', '.txt')) == b'text'


def test_delete(any_store, tmp_path):
    any_store.put(record(), [('.txt', local_file(tmp_path, 'a.txt', b'text'))])
    any_store.delete('pages/abc123')
    assert any_store.get('pages/abc123') is None
    assert not exists(any_store, name(any_store, 'pages/abc123', '.txt'))
    any_store.delete('pages/abc123')  # deleting again is fine


@pytest.mark.parametrize('key', ['../x', 'pages/../../x', 'pages/a b', '/etc/passwd', 'pages/',
                                 'Pages/x', 'pages/a/b', 'pages/é'])
def test_invalid_keys_are_rejected(any_store, key):
    with pytest.raises(ValueError):
        any_store.get(key)
    with pytest.raises(ValueError):
        any_store.put_record(record(key))


@pytest.mark.parametrize('suffix', ['', 'pdf', '/x', '.PDF', '..', '.a.b.c', '.toolongextension'])
def test_invalid_suffixes_are_rejected(any_store, tmp_path, suffix):
    with pytest.raises(ValueError):
        any_store.put(record(), [(suffix, local_file(tmp_path, 'a', b'x'))])


def test_record_json_ignores_unknown_fields():
    """An older reader must cope with Records written by a newer version."""
    text = record().to_json().replace('"key"', '"added_later": 1, "key"')
    assert Record.from_json(text) == record()


# --- conditional writes: several writers, one Record ---------------------------------------------

def test_get_tags_each_version_and_the_tag_is_not_stored_in_the_record(any_store, tmp_path):
    written = any_store.put(record(), [('.txt', local_file(tmp_path, 'a.txt', b'x'))])
    first = any_store.get('pages/abc123')
    assert first.etag and first.etag == written.etag
    any_store.put_record(replace(first, title='changed'))
    second = any_store.get('pages/abc123')
    assert second.etag != first.etag
    assert 'etag' not in raw_record(any_store, 'pages/abc123')
    assert first == replace(first, etag='anything')            # not part of what a Record *is*


def test_a_write_based_on_a_stale_read_is_refused_and_changes_nothing(any_store, tmp_path):
    pdf = name(any_store, 'google_drive/deck', '.pdf')
    any_store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd.pdf', b'v1'))])
    stale = any_store.get('google_drive/deck')
    any_store.put_record(replace(stale, title='someone else was here'))     # a second writer

    new_file = local_file(tmp_path, 'd.pptx', b'v2')
    with pytest.raises(ConflictError):
        any_store.put(replace(stale, title='mine'), [('.pptx', new_file)], expected=stale)
    with pytest.raises(ConflictError):
        any_store.put_record(replace(stale, title='mine'), expected=stale)

    assert any_store.get('google_drive/deck').title == 'someone else was here'   # theirs stands
    assert any_store.get('google_drive/deck').files == [pdf]
    assert read(any_store, pdf) == b'v1'
    assert new_file.read_bytes() == b'v2'                       # still there, ready to retry


def test_a_write_based_on_the_current_read_goes_through(any_store, tmp_path):
    any_store.put(record(), [('.txt', local_file(tmp_path, 'a.txt', b'x'))])
    current = any_store.get('pages/abc123')
    any_store.put_record(replace(current, title='next'), expected=current)
    again = any_store.get('pages/abc123')
    any_store.put(replace(again, title='and next'), [('.txt', local_file(tmp_path, 'b.txt', b'y'))],
                  expected=again)
    assert any_store.get('pages/abc123').title == 'and next'


def test_expected_none_means_there_must_be_no_record_yet(any_store, tmp_path):
    first = any_store.put(record(), [('.txt', local_file(tmp_path, 'a.txt', b'x'))], expected=None)
    assert any_store.get('pages/abc123') == first
    with pytest.raises(ConflictError):                          # someone beat us to creating it
        any_store.put(record(), [('.txt', local_file(tmp_path, 'b.txt', b'y'))], expected=None)
    with pytest.raises(ConflictError):
        any_store.put_record(record(), expected=None)
    assert read(any_store, name(any_store, 'pages/abc123', '.txt')) == b'x'


def test_expected_must_be_a_record_that_was_read_from_the_store(any_store):
    with pytest.raises(ValueError):
        any_store.put_record(record(), expected=record())        # built by hand: no version
    any_store.put_record(record(), expected=UNCHECKED)           # explicitly unchecked is fine


def test_concurrent_read_merge_write_loses_nothing(any_store):
    """Eight writers each add their own entry to one Record, the way ingest() adds found_on."""
    any_store.put_record(record('google_drive/deck'))
    start = threading.Barrier(8)
    conflicts = []

    def add_entry(i: int) -> None:
        start.wait()                                             # everyone reads at about once
        while True:
            current = any_store.get('google_drive/deck')
            update = replace(current, found_on=current.found_on + [f'https://x.org/p{i}'])
            try:
                any_store.put_record(update, expected=current)
                return
            except ConflictError:
                conflicts.append(i)

    threads = [threading.Thread(target=add_entry, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(any_store.get('google_drive/deck').found_on) == [f'https://x.org/p{i}' for i in range(8)]
    assert conflicts                                             # the race really happened


def test_exactly_one_of_several_creators_wins(any_store, tmp_path):
    start = threading.Barrier(4)
    outcomes = []

    def create(i: int) -> None:
        source = local_file(tmp_path, f'f{i}.txt', str(i).encode())
        start.wait()
        try:
            any_store.put(record(), [('.txt', source)], expected=None)
            outcomes.append('won')
        except ConflictError:
            outcomes.append('lost')

    threads = [threading.Thread(target=create, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(outcomes) == ['lost', 'lost', 'lost', 'won']
    winner = any_store.get('pages/abc123')
    assert read(any_store, winner.files[0]) in (b'0', b'1', b'2', b'3')


# --- crash safety (LocalStore: the failure is injected into its own steps) ------------------------

def test_a_crash_before_the_record_is_written_never_loses_the_old_content(tmp_path, monkeypatch):
    """The old order deleted the previous file before writing the Record, so a kill in between
    left a Record listing a file that no longer existed -- which the next run called 'unchanged'.
    Now the Record is written first and the removals come after."""
    store = LocalStore(tmp_path / 'store')
    store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd.pdf', b'v1'))])

    def die(self, rec):
        raise MemoryError('process killed here')
    monkeypatch.setattr(LocalStore, '_write_record', die)
    with pytest.raises(MemoryError):
        store.put(record('google_drive/deck'), [('.pptx', local_file(tmp_path, 'd.pptx', b'v2'))])
    monkeypatch.undo()

    survivor = store.get('google_drive/deck')
    assert survivor.files == ['google_drive/deck.pdf']
    assert read(store, 'google_drive/deck.pdf') == b'v1'          # the file the Record lists exists
    assert all(store.path(name).exists() for name in survivor.files)


def test_a_leftover_from_an_interrupted_write_is_cleaned_up_by_the_next_one(tmp_path):
    store = LocalStore(tmp_path / 'store')
    store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd.pdf', b'v1'))])
    stray = store.path('google_drive/deck.pptx')                  # what a killed write leaves
    stray.write_bytes(b'unlisted')
    store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd2.pdf', b'v2'))])
    assert not stray.exists() and read(store, 'google_drive/deck.pdf') == b'v2'


def test_adding_a_file_does_not_remove_the_others(tmp_path):
    store = LocalStore(tmp_path / 'store')
    store.put(record('youtube/v'), [('.mp4', local_file(tmp_path, 'v.mp4', b'video'))])
    store.put(store.get('youtube/v'), [('.en.vtt', local_file(tmp_path, 's.vtt', b'WEBVTT'))],
              keep_other_files=True)
    assert sorted(p.name for p in (store.root / 'youtube').iterdir() if not p.name.startswith('.')) \
        == ['v.en.vtt', 'v.metadata.json', 'v.mp4']


def test_delete_never_leaves_a_record_listing_missing_files(tmp_path, monkeypatch):
    """Interrupted after the first removal: the Record must already be gone (a leftover file is
    harmless; a Record pointing at nothing is what made 'unchanged' lie)."""
    store = LocalStore(tmp_path / 'store')
    store.put(record('youtube/v'), [('.mp4', local_file(tmp_path, 'v.mp4', b'video'))])
    store.put(store.get('youtube/v'), [('.en.vtt', local_file(tmp_path, 's.vtt', b'WEBVTT'))],
              keep_other_files=True)
    real_unlink, calls = Path.unlink, []

    def dying_unlink(self, *args, **kwargs):
        calls.append(self.name)
        if len(calls) == 2:                                          # after the first removal
            raise MemoryError('process killed here')
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', dying_unlink)
    with pytest.raises(MemoryError):
        store.delete('youtube/v')
    monkeypatch.undo()
    survivor = store.get('youtube/v')
    assert survivor is None or all(store.path(name).exists() for name in survivor.files)


def test_a_move_that_fails_partway_leaves_the_store_as_it_was(tmp_path, monkeypatch):
    """The second of two files can't be moved in (a full disk, copying across devices): nothing
    that was stored may change, and no hidden partial file may be left behind."""
    store = LocalStore(tmp_path / 'store')
    store.put(record('google_drive/deck'), [('.pdf', local_file(tmp_path, 'd.pdf', b'v1'))])
    real_move, calls = shutil.move, []

    def flaky_move(src, dst, *args, **kwargs):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_move(src, dst, *args, **kwargs)
    monkeypatch.setattr(shutil, 'move', flaky_move)
    with pytest.raises(OSError):
        store.put(record('google_drive/deck'), [('.pptx', local_file(tmp_path, 'a.pptx', b'new')),
                                                ('.en.vtt', local_file(tmp_path, 'b.vtt', b'new'))])
    monkeypatch.undo()
    names = sorted(p.name for p in (store.root / 'google_drive').iterdir())
    assert names == ['deck.metadata.json', 'deck.pdf']               # as before; no .part files
    assert store.get('google_drive/deck').files == ['google_drive/deck.pdf']
    assert read(store, 'google_drive/deck.pdf') == b'v1'
