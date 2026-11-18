"""S3Store specifics, against AWS Emulator: the layout and object metadata i2ccc's ingestion
reads, streaming big downloads, and telling ingestion about replaced files."""
from dataclasses import replace
from datetime import timedelta

import pytest

from i2cc_scraper import downloader
from i2cc_scraper.aws import IngestInvalidator, S3Store, file_id, file_title, s3store
from i2cc_scraper.downloader import NEW, UNCHANGED, UPDATED, Context, TransientError
from i2cc_scraper.store import ConflictError, Record


class Recorder:
    """An Invalidator that writes down what it's told (or fails, with .fail set)."""
    def __init__(self):
        self.calls, self.fail = [], None

    def invalidate(self, s3_uri, fid):
        if self.fail:
            raise self.fail
        self.calls.append(s3_uri)


@pytest.fixture
def told(s3_store):
    s3_store.invalidator = Recorder()
    return s3_store.invalidator


def record(key='google_drive/deck', sha='a' * 64, **fields) -> Record:
    fields.setdefault('source_url', 'https://example.org/deck')
    return Record(key=key, type='google_drive', files=[], content_sha256=sha, **fields)


def local(tmp_path, name, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def head(store, name):
    return store.client.head_object(Bucket=store.bucket, Key=store.object_key(name))


def keys(store):
    response = store.client.list_objects_v2(Bucket=store.bucket, Prefix=store.prefix)
    return sorted(o['Key'][len(store.prefix):] for o in response.get('Contents', []))


# --- layout and metadata ---------------------------------------------------------------------

def test_files_are_named_by_title_under_a_content_hash(s3_store, tmp_path):
    stored = s3_store.put(record(title='Cloud Forum: Day 1 / Keynote'),
                          [('.pdf', local(tmp_path, 'd.pdf', b'%PDF'))])
    assert stored.files == ['google_drive/deck/aaaaaaaaaaaa/Cloud Forum Day 1 Keynote.pdf']
    assert keys(s3_store) == ['google_drive/deck.metadata.json', stored.files[0]]


@pytest.mark.parametrize('title, expected', [
    ('Café ☕ talk', 'Cafe talk'), ('../../etc/passwd', 'etc passwd'), ('a?b#c%d', 'a b c d'),
    ('', 'deck'), ('...', 'deck'), ('x' * 300, 'x' * 100), ("Women's IT (2026) & more", "Women's IT (2026) & more"),
])
def test_file_titles_are_safe_ascii_names(title, expected):
    assert file_title(title, 'google_drive/deck') == expected


@pytest.mark.parametrize('title, suffix', [('Talk v1.2', '.pdf'), ('Keynote', '.en.vtt'),
                                           ('Notes.de', '.en.vtt'), ('Report 2026.final', '.mp4')])
def test_the_suffix_is_found_again_from_the_name(s3_store, title, suffix):
    rec = record(title=title)
    assert s3_store.suffix_of(rec.key, s3_store.file_name(rec, suffix)) == suffix


def test_objects_carry_the_metadata_ingestion_reads(s3_store, tmp_path):
    rec = record('youtube/VIDEO1', title='Keynote', source_url='https://www.youtube.com/watch?v=VIDEO1',
                 parent_folder_name='Día 2', parent_folder_url='https://drive.google.com/drive/folders/F',
                 content_type='video/mp4')
    stored = s3_store.put(rec, [('.mp4', local(tmp_path, 'v.mp4', b'video')),
                                ('.en.vtt', local(tmp_path, 'v.vtt', b'WEBVTT\n'))])
    video, captions = (head(s3_store, n) for n in stored.files)
    assert video['Metadata'] == {
        'member-content': 'false', 'source-url': 'https://www.youtube.com/watch?v=VIDEO1',
        'parent-folder-name': 'Dia 2', 'parent-folder-url': 'https://drive.google.com/drive/folders/F',
        'relative-start-time': '0', 'scraper-key': 'youtube/VIDEO1',
        'scraper-file-id': file_id(s3_store.object_key(stored.files[0]))}
    assert video['ContentType'] == 'video/mp4' and captions['ContentType'] == 'text/vtt'
    assert 'relative-start-time' not in captions['Metadata']           # media files only
    assert captions['Metadata']['scraper-file-id'] != video['Metadata']['scraper-file-id']


def test_a_metadata_only_change_updates_the_objects_metadata(s3_store, tmp_path):
    stored = s3_store.put(record(), [('.pdf', local(tmp_path, 'd.pdf', b'%PDF'))])
    s3_store.put_record(replace(s3_store.get(stored.key), parent_folder_name='Slides 2026'))
    assert head(s3_store, stored.files[0])['Metadata']['parent-folder-name'] == 'Slides 2026'
    assert s3_store.read(stored.files[0]) == b'%PDF'


# --- streaming ---------------------------------------------------------------------------------

def test_a_big_download_is_streamed_in_parts(s3_store):
    part = s3store.PART_SIZE
    staged = s3_store.stage(iter([b'a' * part, b'b' * (part // 2), b'c' * (part // 2), b'end']))
    assert staged.size == 2 * part + 3
    stored = s3_store.put(record(sha=staged.sha256), [('.mp4', staged)])
    body = s3_store.read(stored.files[0])
    assert len(body) == staged.size and body.endswith(b'cend')
    assert not [k for k in keys(s3_store) if '.staging' in k]                # consumed


def test_a_broken_big_download_leaves_no_upload_behind(s3_store):
    def chunks():
        yield b'a' * (s3store.PART_SIZE + 1)
        raise ConnectionError('dropped')
    with pytest.raises(ConnectionError):
        s3_store.stage(chunks())
    uploads = s3_store.client.list_multipart_uploads(Bucket=s3_store.bucket,
                                                     Prefix=s3_store.prefix)
    assert not uploads.get('Uploads') and keys(s3_store) == []


# --- telling ingestion -------------------------------------------------------------------------

def test_new_content_makes_ingestion_forget_the_old_files(s3_store, told, tmp_path):
    first = s3_store.put(record(sha='1' * 64), [('.pdf', local(tmp_path, 'a.pdf', b'v1'))])
    assert told.calls == []                                          # nothing to forget yet
    second = s3_store.put(replace(record(sha='2' * 64), title='New'),
                          [('.pdf', local(tmp_path, 'b.pdf', b'v2'))], expected=first)
    assert told.calls == [s3_store.uri(first.files[0])] * 2          # before, and after
    assert keys(s3_store) == ['google_drive/deck.metadata.json', second.files[0]]


def test_the_same_content_stored_again_is_not_reingested(s3_store, told, tmp_path):
    s3_store.put(record(), [('.txt', local(tmp_path, 'a.txt', b'text'))])
    s3_store.put(record(), [('.txt', local(tmp_path, 'b.txt', b'text'))])   # e.g. links changed
    assert told.calls == []


def test_captions_fetched_again_are_reingested(s3_store, told, tmp_path):
    video = s3_store.put(record('youtube/v'), [('.mp4', local(tmp_path, 'v.mp4', b'video'))])
    s3_store.put(s3_store.get('youtube/v'), [('.en.vtt', local(tmp_path, 'a.vtt', b'1'))],
                 keep_other_files=True)
    assert told.calls == []
    stored = s3_store.put(s3_store.get('youtube/v'), [('.en.vtt', local(tmp_path, 'b.vtt', b'2'))],
                          keep_other_files=True)
    assert set(told.calls) == {s3_store.uri(stored.files[1])}
    assert video.files[0] == stored.files[0]


def test_delete_makes_ingestion_forget_everything(s3_store, told, tmp_path):
    stored = s3_store.put(record(), [('.pdf', local(tmp_path, 'a.pdf', b'v1'))])
    s3_store.delete(stored.key)
    assert told.calls == [s3_store.uri(stored.files[0])] and keys(s3_store) == []


def test_if_ingestion_cannot_be_told_nothing_changes(s3_store, told, tmp_path):
    first = s3_store.put(record(sha='1' * 64), [('.pdf', local(tmp_path, 'a.pdf', b'v1'))])
    told.fail = ConnectionError('OpenSearch is down')
    with pytest.raises(TransientError):
        s3_store.put(record(sha='2' * 64), [('.pdf', local(tmp_path, 'b.pdf', b'v2'))],
                     expected=first)
    assert s3_store.get(first.key) == first
    assert keys(s3_store) == ['google_drive/deck.metadata.json', first.files[0]]


# --- conflicts and leftovers -------------------------------------------------------------------

def test_a_write_that_loses_a_race_removes_what_it_placed(s3_store, tmp_path, monkeypatch):
    base = s3_store.put(record(sha='1' * 64), [('.pdf', local(tmp_path, 'a.pdf', b'v1'))])
    real_write = S3Store._write_record

    def someone_else_first(self, rec, expected):
        monkeypatch.setattr(S3Store, '_write_record', real_write)
        self.put(record(sha='3' * 64, title='Theirs'), [('.pdf', local(tmp_path, 'c.pdf', b'v3'))])
        real_write(self, rec, expected)
    monkeypatch.setattr(S3Store, '_write_record', someone_else_first)
    with pytest.raises(ConflictError):
        s3_store.put(record(sha='2' * 64, title='Mine'), [('.pdf', local(tmp_path, 'b.pdf', b'v2'))],
                     expected=base)
    theirs = s3_store.get(base.key)
    assert theirs.title == 'Theirs'
    assert keys(s3_store) == ['google_drive/deck.metadata.json', theirs.files[0]]


def test_leftovers_of_a_killed_write_are_removed_once_old(s3_store, tmp_path, monkeypatch):
    stored = s3_store.put(record(), [('.pdf', local(tmp_path, 'a.pdf', b'v1'))])
    stray = 'google_drive/deck/bbbbbbbbbbbb/Half done.pdf'
    s3_store.client.put_object(Bucket=s3_store.bucket, Key=s3_store.object_key(stray), Body=b'x')
    s3_store.put_record(s3_store.get(stored.key))
    assert stray in keys(s3_store)                         # young: maybe someone's write
    monkeypatch.setattr(s3store, 'LEFTOVER_AGE', timedelta(minutes=-5))
    s3_store.put_record(s3_store.get(stored.key))
    assert keys(s3_store) == ['google_drive/deck.metadata.json', stored.files[0]]


# --- the downloader on S3 ----------------------------------------------------------------------

def test_a_website_file_is_streamed_into_s3_and_rechecked(site, s3_store, told):
    site.route('/files/Annual%20Report.pdf', b'%PDF v1', content_type='application/pdf')
    url = site.url('/files/Annual%20Report.pdf')
    first = downloader.download(url, s3_store)
    assert (first.status, first.files[0].rsplit('/', 1)[1]) == (NEW, 'Annual Report.pdf')
    assert downloader.download(url, s3_store).status == UNCHANGED
    site.route('/files/Annual%20Report.pdf', b'%PDF v2', content_type='application/pdf')
    second = downloader.download(url, s3_store)
    assert second.status == UPDATED and s3_store.read(second.files[0]) == b'%PDF v2'
    assert set(told.calls) == {s3_store.uri(first.files[0])}
    assert not [k for k in keys(s3_store) if '.staging' in k]


def test_a_youtube_video_is_streamed_into_s3_in_ranges(s3_store, fake_yt_dlp, site, monkeypatch):
    monkeypatch.setattr(downloader.youtube, 'RANGE_SIZE', 2)
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1', s3_store)
    assert [n.rsplit('/', 1)[1] for n in result.files] == ['Keynote recording.mp4',
                                                           'Keynote recording.en.vtt']
    assert s3_store.read(result.files[0]) == b'video'
    assert site.hits['/videoplayback'] == 3                        # 'vi', 'de', 'o'
    assert head(s3_store, result.files[0])['Metadata']['relative-start-time'] == '0'


def test_a_page_is_stored_as_text_named_by_its_title(s3_store):
    entry = {'url': 'https://example.org/a/', 'type': 'page_text', 'title': 'Cloud Forum 2026',
             'text': 'Hello'}
    with Context(s3_store) as ctx:
        result = downloader.save_page(entry, ctx)
    assert result.files[0].endswith('/Cloud Forum 2026.txt')
    assert s3_store.read(result.files[0]).startswith(b'Title: Cloud Forum 2026\n')


# --- IngestInvalidator --------------------------------------------------------------------------

class FakeSearch:
    """Stands in for OpenSearch Serverless: search by the file-id term, bulk delete by _id."""
    def __init__(self, docs):
        self.docs = dict(docs)   # _id -> scraper-file-id

    def search(self, index, body):
        fid = body['query']['term']['metadata.scraper-file-id.keyword']
        ids = sorted(i for i, f in self.docs.items() if f == fid)
        after = body.get('search_after', [''])[0]
        hits = [{'_id': i, 'sort': [i]} for i in ids if i > after][:body['size']]
        return {'hits': {'hits': hits}}

    def bulk(self, body):
        for action in body:
            self.docs.pop(action['delete']['_id'], None)
        return {'items': [{'delete': {'status': 200}} for _ in body]}


def test_the_invalidator_clears_the_processed_entry_and_the_vectors(emulator, monkeypatch):
    table = 'processed-files-test'
    try:
        emulator.dynamodb.create_table(
            TableName=table, BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName': 's3_uri', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 's3_uri', 'AttributeType': 'S'}])
        emulator.dynamodb.get_waiter('table_exists').wait(TableName=table)
    except emulator.dynamodb.exceptions.ResourceInUseException:
        pass
    uri = 's3://bucket/youtube/v/abc/Talk.mp4'
    emulator.dynamodb.put_item(TableName=table, Item={'s3_uri': {'S': uri}})
    monkeypatch.setattr('i2cc_scraper.aws.invalidate.PAGE', 2)    # several pages of hits
    search = FakeSearch({f'd{i}': 'F1' for i in range(5)} | {'other': 'F2'})
    IngestInvalidator(table, search, 'chatbot-vs', dynamodb=emulator.dynamodb).invalidate(uri, 'F1')
    assert 'Item' not in emulator.dynamodb.get_item(TableName=table, Key={'s3_uri': {'S': uri}})
    assert search.docs == {'other': 'F2'}
