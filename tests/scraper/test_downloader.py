"""Downloader: website files, pages, change detection, error reasons, Google helpers, the Drive
API logic (with a fake Drive client), and YouTube/Panopto (with a fake yt-dlp / local server).
No network, no credentials."""
import copy
import threading

import pytest

from conftest import FIXTURES, etag_route, run_cli
from i2cc_scraper import downloader
from i2cc_scraper.downloader import (FAILED, NEW, UNCHANGED, UPDATED, Context, Options,
                                     TransientError)
from i2cc_scraper.drive_api import DriveAPIError

PDF = b'%PDF-1.4 fake pdf'


def content(store, result, i=0) -> bytes:
    return store.path(result.files[i]).read_bytes()


def metadata_bytes(store, key) -> bytes:
    return store.path(key + '.metadata.json').read_bytes()


# --- files on ordinary websites -------------------------------------------------------------------

def test_file_is_stored_under_its_url_key_with_a_record(site, store):
    site.route('/download?id=7', PDF, content_type='application/pdf',
               headers={'Content-Disposition': 'attachment; filename="Keynote Slides.pdf"'})
    result = downloader.download(site.url('/download?id=7'), store, kind='file')
    assert (result.status, result.title) == (NEW, 'Keynote Slides')
    assert result.key.startswith('files/') and result.files == [result.key + '.pdf']
    assert content(store, result) == PDF

    record = store.get(result.key)
    assert record.source_url == site.url('/download?id=7')
    assert (record.type, record.content_type, record.title) == (
        'file', 'application/pdf', 'Keynote Slides')
    assert len(record.content_sha256) == 64 and record.fetched_at


def test_file_without_validators_is_downloaded_but_not_rewritten_if_unchanged(site, store):
    site.route('/uploads/Floor%20Plans.pdf', PDF, content_type='application/pdf')
    url = site.url('/uploads/Floor%20Plans.pdf')
    first = downloader.download(url, store)
    meta = metadata_bytes(store, first.key)

    second = downloader.download(url, store)
    assert (second.status, second.files) == (UNCHANGED, first.files)
    assert site.hits['/uploads/Floor%20Plans.pdf'] == 2   # no ETag: must download to compare
    assert metadata_bytes(store, first.key) == meta        # ... but nothing was written


def test_file_with_an_etag_is_not_downloaded_again(site, store):
    seen = []
    site.routes['/a.pdf'] = etag_route('"v1"', PDF, seen)
    first = downloader.download(site.url('/a.pdf'), store)
    assert store.get(first.key).source_version == 'etag="v1"'

    second = downloader.download(site.url('/a.pdf'), store)
    assert second.status == UNCHANGED
    assert seen == [None, '"v1"']  # the second request was conditional, and got a 304


def test_server_ignoring_if_none_match_but_sending_the_same_etag(site, store):
    site.route('/a.pdf', PDF, content_type='application/pdf', headers={'ETag': '"v1"'})
    downloader.download(site.url('/a.pdf'), store)
    assert downloader.download(site.url('/a.pdf'), store).status == UNCHANGED


def test_changed_file_is_updated(site, store):
    site.route('/a.pdf', PDF, content_type='application/pdf', headers={'ETag': '"v1"'})
    first = downloader.download(site.url('/a.pdf'), store)
    site.route('/a.pdf', b'%PDF v2', content_type='application/pdf', headers={'ETag': '"v2"'})
    second = downloader.download(site.url('/a.pdf'), store)
    assert (second.status, second.key) == (UPDATED, first.key)
    assert content(store, second) == b'%PDF v2'
    assert store.get(first.key).source_version == 'etag="v2"'


def test_force_downloads_again_without_a_conditional_request(site, store):
    seen = []
    site.routes['/a.pdf'] = etag_route('"v1"', PDF, seen)
    downloader.download(site.url('/a.pdf'), store)
    again = downloader.download(site.url('/a.pdf'), store, force=True)
    assert seen == [None, None] and again.status == UNCHANGED  # same bytes: nothing written


@pytest.mark.parametrize('status, content_type, reason', [
    (404, 'text/html', 'not_found'),
    (403, 'text/html', 'access_denied'),
    (200, 'text/html', 'access_denied'),  # a sign-in page instead of the file
])
def test_file_error_pages_are_not_stored(site, store, status, content_type, reason):
    site.route('/slides.pdf', b'<html>Please sign in</html>', status=status,
               content_type=content_type)
    result = downloader.download(site.url('/slides.pdf'), store)
    assert (result.status, result.reason) == (FAILED, reason)
    assert result.files == [] and 'instead of the file' in result.error
    assert not store.root.exists() or not any(store.root.rglob('*'))


def test_unsupported_url_is_a_failed_result_not_an_exception(store):
    result = downloader.download('https://example.org/just-a-page', store)
    assert (result.status, result.reason) == (FAILED, 'unsupported')
    assert result.error.startswith('Not a YouTube')


def test_panopto_link_without_a_session_id(store):
    result = downloader.download(
        'https://internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=notauuid', store)
    assert result.reason == 'invalid_url' and 'No Panopto session id' in result.error


@pytest.mark.parametrize('disposition', [
    'attachment; filename="Report. final draft"',  # not an extension: use the MIME type
    'attachment; filename="x.<>|"',
    'attachment; filename="notes"',
])
def test_bad_extension_in_header_falls_back_to_content_type(site, store, disposition):
    site.route('/get', PDF, content_type='application/pdf',
               headers={'Content-Disposition': disposition})
    result = downloader.download(site.url('/get'), store, kind='file')
    assert result.files == [result.key + '.pdf']
    assert sorted(p.name for p in (store.root / 'files').iterdir()) == sorted(
        [result.key.split('/')[1] + '.pdf', result.key.split('/')[1] + '.metadata.json'])


def test_download_all_uses_the_crawl_type(site, store):
    """The crawler marks an extensionless link 'file' from its Content-Type; download_all must
    download it even though classify_url() alone can't tell."""
    site.route('/download?id=9', PDF, content_type='application/pdf')
    results = downloader.download_all([{'url': site.url('/download?id=9'), 'type': 'file'}],
                                      store)
    assert [(r.type, r.status) for r in results] == [('file', NEW)]
    assert results[0].files[0].endswith('.pdf')


# --- pages ------------------------------------------------------------------------------------------

def page(url='https://example.org/a/', text='Hello', html='<main>Hello</main>', title='Page A',
         kind='page_text', found_on=None):
    return {'url': url, 'type': kind, 'title': title, 'text': text, 'html': html,
            'found_on': found_on}


def test_save_page_stores_text(store):
    with Context(store) as ctx:
        result = downloader.save_page(page(), ctx)
    assert result.status == NEW and result.key.startswith('pages/')
    assert result.files == [result.key + '.txt']
    assert content(store, result, 0) == b'Title: Page A\nURL: https://example.org/a/\n\nHello\n'
    record = store.get(result.key)
    assert (record.type, record.title, record.content_type) == (
        'page_text', 'Page A', 'text/plain; charset=utf-8')


def test_page_key_ignores_title_and_url_spelling(store):
    with Context(store) as ctx:
        first = downloader.save_page(page(title='Old title'), ctx)
        second = downloader.save_page(page(url='https://EXAMPLE.org/a', title='New title'), ctx)
    assert first.key == second.key and second.status == UPDATED  # the text header changed
    assert len(list((store.root / 'pages').iterdir())) == 2   # .txt, .metadata.json


def test_page_with_same_text_is_unchanged_even_if_html_differs(store):
    with Context(store) as ctx:
        first = downloader.save_page(page(html='<main>Hello</main><!-- nonce 1 -->'), ctx)
        meta = metadata_bytes(store, first.key)
        second = downloader.save_page(page(html='<main>Hello</main><!-- nonce 2 -->'), ctx)
        assert second.status == UNCHANGED and metadata_bytes(store, first.key) == meta
        third = downloader.save_page(page(text='Hello, world'), ctx)
    assert third.status == UPDATED


def test_found_on_collects_every_linking_page(site, store):
    site.route('/deck.pdf', PDF, content_type='application/pdf', headers={'ETag': '"1"'})
    entry = {'url': site.url('/deck.pdf'), 'type': 'file', 'title': 'Slides', 'context': 'Keynote'}
    downloader.download_all([{**entry, 'found_on': 'https://example.org/schedule/'}], store)
    second = downloader.download_all([{**entry, 'found_on': 'https://example.org/abstracts/'}],
                                     store)
    assert second[0].status == UNCHANGED
    record = store.get(second[0].key)
    assert record.found_on == ['https://example.org/schedule/', 'https://example.org/abstracts/']
    assert (record.link_text, record.context) == ('Slides', 'Keynote')


def test_download_all_saves_pages_and_downloads_files(site, store):
    site.route('/a.pdf', PDF, content_type='application/pdf')
    entries = [
        page(url='https://example.org/', text='x', html='<p>x</p>', title='Home', kind='page'),
        {'url': 'https://example.org/t', 'type': 'page_text', 'html': '', 'text': ''},  # empty
        {'url': 'https://example.org/bad', 'type': 'failed', 'error': '404'},
        {'url': site.url('/a.pdf'), 'type': 'file'},
        site.url('/a.pdf'),  # plain URLs work too (this one is the same file: done once)
    ]
    results = downloader.download_all(entries, store)
    assert [(r.type, r.status, r.error) for r in results] == [('page', NEW, ''), ('file', NEW, '')]
    assert site.hits['/a.pdf'] == 1

    assert downloader.download_all(entries[:1], store, save_pages=False) == []


# --- error classification -----------------------------------------------------------------------

@pytest.mark.parametrize('exc, expected', [
    (DriveAPIError(404, 'File not found'), 'not_found'),
    (DriveAPIError(403, 'The caller does not have permission'), 'access_denied'),
    (DriveAPIError(403, 'User Rate Limit Exceeded'), 'transient'),
    (DriveAPIError(500, 'Backend Error'), 'transient'),
    (downloader.NotDownloadable('a Google Form'), 'not_downloadable'),
    (ValueError('a bug'), None),
    (OSError(13, 'Permission denied'), None),                        # a local problem: not ours to hide
    (OSError(28, 'No space left on device'), 'disk_full'),           # environmental, and temporary
])
def test_classify_error(exc, expected):
    error = downloader.classify_error(exc)
    if expected is None:
        assert error is None
    elif expected == 'transient':
        assert isinstance(error, TransientError)
    elif expected == 'disk_full':
        assert isinstance(error, downloader.errors.DiskFullError) and error.reason == 'disk_full'
        assert isinstance(error, TransientError)
    else:
        assert error.reason == expected


# --- Google helpers -----------------------------------------------------------------------------

@pytest.mark.parametrize('url, parsed', [
    ('https://docs.google.com/presentation/d/ABC/edit?usp=drive_link', ('presentation', 'ABC')),
    ('https://docs.google.com/spreadsheets/d/ABC/edit', ('spreadsheets', 'ABC')),
    ('https://drive.google.com/file/d/ABC/view', ('file', 'ABC')),
    ('https://drive.google.com/drive/folders/ABC?usp=sharing', ('folder', 'ABC')),
    ('https://drive.google.com/drive/u/1/folders/ABC', ('folder', 'ABC')),
    ('https://drive.google.com/folderview?id=ABC', ('folder', 'ABC')),
    ('https://drive.google.com/open?id=ABC', ('file', 'ABC')),
])
def test_parse_google_url(url, parsed):
    assert downloader.parse_google_url(url) == parsed


@pytest.mark.parametrize('url', [
    'https://drive.google.com/drive/my-drive',
    'https://drive.google.com/open?id=../../x',
])
def test_parse_google_url_rejects_other_links(url):
    with pytest.raises(downloader.DownloadError) as info:
        downloader.parse_google_url(url)
    assert info.value.reason == 'invalid_url'


@pytest.mark.parametrize('header, name', [
    ('attachment; filename="Tuesday 1430 - Yuan.pdf"', 'Tuesday 1430 - Yuan.pdf'),
    ("attachment; filename=\"a.pdf\"; filename*=UTF-8''Caf%C3%A9%20talk.pdf", 'Café talk.pdf'),
    ('inline', ''),
    ('attachment; filename=Bare Name.pdf', 'Bare Name.pdf'),
    ("attachment; filename*=iso-8859-1''Caf%E9.pdf", 'Café.pdf'),
    # Server-supplied paths are cut to their last component:
    ('attachment; filename="../../etc/passwd"', 'passwd'),
    ('attachment; filename="a.pdf/../../../tmp/x"', 'x'),
    ("attachment; filename*=UTF-8''..%2F..%2Fevil.sh", 'evil.sh'),
    ('attachment; filename="C:\\Users\\me\\Talk.pptx"', 'Talk.pptx'),
])
def test_filename_from_headers(header, name):
    assert downloader._filename_from_headers(header) == name


@pytest.mark.parametrize('name, title', [
    ('Talk.pptx', 'Talk'), ('Talk.pptx.pdf', 'Talk'), ('Handout.pdf', 'Handout'),
    ('Notes', 'Notes'), ('v2.0 roadmap', 'v2.0 roadmap'), ('Bad\x00name\n', 'Badname'),
])
def test_clean_title(name, title):
    assert downloader._clean_title(name) == title


def test_list_google_folder_parses_the_embedded_view():
    html = '''<html><title>Slides 2026</title><body>
      <div class="flip-entry" id="entry-F1"><div class="flip-entry-info">
        <a href="https://drive.google.com/drive/folders/F1"><div class="flip-entry-title">Day 2</div></a></div></div>
      <div class="flip-entry" id="entry-D1"><div class="flip-entry-info">
        <a href="https://drive.google.com/file/d/D1/view?usp=drive_web"><div class="flip-entry-title">talk.pdf</div></a></div></div>
    </body></html>'''

    class FakeResponse:
        status_code, text = 200, html
        def raise_for_status(self): pass

    class FakeSession:
        def get(self, url, **kwargs):
            assert url == 'https://drive.google.com/embeddedfolderview?id=ROOT'
            return FakeResponse()

    assert downloader.list_google_folder('ROOT', FakeSession()) == ('Slides 2026', [
        ('Day 2', 'https://drive.google.com/drive/folders/F1'),
        ('talk.pdf', 'https://drive.google.com/file/d/D1/view?usp=drive_web'),
    ])


# --- Drive API logic, with a fake client --------------------------------------------------------

G = 'application/vnd.google-apps.'
T1 = '2026-11-01T10:00:00.000Z'
DRIVE_FILES = {
    'root': {'id': 'root', 'name': 'Slides 2026', 'mimeType': G + 'folder'},
    'deck': {'id': 'deck', 'name': 'Keynote deck', 'mimeType': G + 'presentation',
             'modifiedTime': T1},
    'pptx': {'id': 'pptx', 'name': 'Panel.pptx', 'md5Checksum': 'aaa', 'modifiedTime': T1,
             'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'},
    'pdf': {'id': 'pdf', 'name': 'Handout.pdf', 'mimeType': 'application/pdf',
            'md5Checksum': 'bbb', 'modifiedTime': T1},
    'sub': {'id': 'sub', 'name': 'Day 2', 'mimeType': G + 'folder'},
    'doc': {'id': 'doc', 'name': 'Notes', 'mimeType': G + 'document', 'modifiedTime': T1},
    'loop': {'id': 'loop', 'name': 'Shortcut to root', 'mimeType': G + 'shortcut',
             'shortcutDetails': {'targetId': 'root'}},
    'link': {'id': 'link', 'name': 'Shortcut to other', 'mimeType': G + 'shortcut',
             'shortcutDetails': {'targetId': 'other'}},
    'other': {'id': 'other', 'name': 'Other.pdf', 'mimeType': 'application/pdf',
              'md5Checksum': 'ccc', 'modifiedTime': T1},
    'form': {'id': 'form', 'name': 'Feedback', 'mimeType': G + 'form'},
}
DRIVE_CHILDREN = {'root': ['sub', 'deck', 'pptx', 'pdf', 'form', 'link'], 'sub': ['doc', 'loop']}
ROOT_URL = 'https://drive.google.com/drive/folders/root'


class FakeDrive:
    email = 'ingest@example.iam.gserviceaccount.com'

    def __init__(self):
        self.files = copy.deepcopy(DRIVE_FILES)
        self.calls = []
        self.fail = {}  # file id -> exception to raise when its content is fetched
        self.listing_error = None

    def get(self, file_id, resolve_shortcut=True):
        if file_id not in self.files:
            raise DriveAPIError(404, f'Drive API HTTP 404: File not found: {file_id}.')
        meta = self.files[file_id]
        if resolve_shortcut and meta['mimeType'] == G + 'shortcut':
            return self.get(meta['shortcutDetails']['targetId'], resolve_shortcut=False)
        return meta

    def list_folder(self, folder_id):
        if self.listing_error is not None:
            raise self.listing_error
        return [self.files[c] for c in DRIVE_CHILDREN[folder_id]]

    def fail_listing(self, error: Exception) -> 'FakeDrive':
        """Make every folder listing raise error."""
        self.listing_error = error
        return self

    def download(self, file_id):
        self.calls.append(('download', file_id))
        if file_id in self.fail:
            raise self.fail[file_id]
        return iter([b'content of ' + file_id.encode()
                     + self.files[file_id].get('md5Checksum', '').encode()])

    def export(self, meta, mime_type):
        self.calls.append(('export', meta['id'], mime_type))
        if meta['id'] in self.fail:
            raise self.fail[meta['id']]
        return iter([f'export of {meta["id"]} at {meta.get("modifiedTime")} as {mime_type}'
                     .encode()])


def test_drive_folder_is_stored_item_by_item(store):
    drive = FakeDrive()
    result = downloader.download(ROOT_URL, store, drive=drive)
    assert (result.status, result.error, result.title) == (UPDATED, '', 'Slides 2026')
    assert sorted(result.files) == [
        'google_drive/deck.pdf', 'google_drive/doc.pdf', 'google_drive/other.pdf',  # via shortcut
        'google_drive/pdf.pdf', 'google_drive/pptx.pptx']                          # upload as-is
    # Native files are exported, uploads downloaded; the shortcut back to root isn't followed,
    # and the Google Form is skipped without counting as a failure.
    assert sorted(c[:2] for c in drive.calls) == [
        ('download', 'other'), ('download', 'pdf'), ('download', 'pptx'),
        ('export', 'deck'), ('export', 'doc')]

    notes = store.get('google_drive/doc')
    assert notes.found_on == ['https://drive.google.com/drive/folders/sub']
    assert (notes.title, notes.folder_path, notes.context) == ('Notes', 'Slides 2026 / Day 2', '')
    assert notes.source_version == f'{T1};format=pdf'
    assert store.get('google_drive/pptx').title == 'Panel'


def test_drive_rerun_downloads_nothing(store):
    drive = FakeDrive()
    downloader.download(ROOT_URL, store, drive=drive)
    drive.calls.clear()
    again = downloader.download(ROOT_URL, store, drive=drive)
    assert again.status == UNCHANGED and drive.calls == []
    assert {c.status for c in again.children} == {UNCHANGED}


def test_drive_modified_file_is_downloaded_again(store):
    drive = FakeDrive()
    downloader.download(ROOT_URL, store, drive=drive)
    drive.calls.clear()
    drive.files['deck']['modifiedTime'] = '2026-11-02T09:00:00.000Z'
    again = downloader.download(ROOT_URL, store, drive=drive)
    assert [c[:2] for c in drive.calls] == [('export', 'deck')]
    assert {c.key: c.status for c in again.children}['google_drive/deck'] == UPDATED
    assert again.status == UPDATED


def test_drive_export_format_change_replaces_the_stored_file(store):
    drive = FakeDrive()
    url = 'https://docs.google.com/presentation/d/deck/edit'
    downloader.download(url, store, drive=drive)
    result = downloader.download(url, store, drive=drive, export_format='pptx')
    assert result.status == UPDATED and result.files == ['google_drive/deck.pptx']
    assert drive.calls[-1][2].endswith('presentationml.presentation')
    assert not store.path('google_drive/deck.pdf').exists()  # the old export is removed


def test_drive_missing_file_is_not_found(store):
    result = downloader.download('https://drive.google.com/file/d/nope/view', store,
                                 drive=FakeDrive())
    assert (result.status, result.reason, result.key) == (FAILED, 'not_found', 'google_drive/nope')
    assert 'File not found' in result.error


def test_drive_form_link_is_not_downloadable(store):
    result = downloader.download('https://drive.google.com/file/d/form/view', store,
                                 drive=FakeDrive())
    assert result.reason == 'not_downloadable' and "can't be downloaded" in result.error


def test_drive_folder_with_a_private_file_is_partial(store):
    drive = FakeDrive()
    drive.fail['pdf'] = DriveAPIError(403, 'Drive API HTTP 403: no permission')
    result = downloader.download(ROOT_URL, store, drive=drive)
    assert (result.status, result.reason) == (UPDATED, 'partial')
    assert 'Handout.pdf' in result.error
    failed = [c for c in result.children if c.status == FAILED]
    assert [(c.key, c.reason) for c in failed] == [('google_drive/pdf', 'access_denied')]


def test_drive_folder_with_a_temporary_failure_raises_after_the_rest(store):
    drive = FakeDrive()
    drive.fail['pdf'] = DriveAPIError(503, 'Drive API HTTP 503: Backend Error')
    with Context(store, drive=drive) as ctx, pytest.raises(TransientError):
        downloader.ingest(ROOT_URL, ctx)
    assert store.get('google_drive/deck') is not None  # the others were stored
    assert store.get('google_drive/pdf') is None


# --- Panopto (local server standing in for Panopto) ---------------------------------------------

SESSION = '0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0'


def test_panopto_session_and_captions(site, store):
    site.route(f'/Panopto/Podcast/Download/{SESSION}.mp4?mediaTargetType=videoPodcast',
               b'mp4 data', content_type='video/mp4',
               headers={'Content-Disposition': 'attachment; filename="Opening Keynote_default.mp4"'})
    site.route(f'/Panopto/Pages/Transcription/GenerateSRT.ashx?id={SESSION}&language=0',
               b'1\n00:00:01,000 --> 00:00:02,000\nWelcome\n', content_type='text/plain')
    url = site.url(f'/Panopto/Pages/Viewer.aspx?id={SESSION}')
    result = downloader.download(url, store, kind='panopto')
    assert (result.status, result.title) == (NEW, 'Opening Keynote')
    assert result.files == [f'panopto/{SESSION}.mp4', f'panopto/{SESSION}.en.vtt']
    assert store.path(result.files[1]).read_text().startswith('WEBVTT\n\n1\n00:00:01.000 -->')

    again = downloader.download(url, store, kind='panopto')
    assert again.status == UNCHANGED
    assert sum(site.hits.values()) == 2  # the rerun made no requests


# --- YouTube (with a fake yt-dlp) ---------------------------------------------------------------

def test_youtube_video_and_captions(store, fake_yt_dlp):
    url = 'https://www.youtube.com/watch?v=VIDEO1'
    result = downloader.download(url, store)
    assert (result.status, result.title) == (NEW, 'Keynote recording')
    assert result.files == ['youtube/VIDEO1.mp4', 'youtube/VIDEO1.en.vtt']

    again = downloader.download('https://youtu.be/VIDEO1', store)  # same video, other link
    assert again.status == UNCHANGED and fake_yt_dlp.calls == [url]


@pytest.mark.parametrize('message, reason', [
    ("Sign in to confirm you're not a bot. Use --cookies-from-browser", 'blocked'),
    ('Private video. Sign in if you\'ve been granted access', 'access_denied'),
    ('Sign in to confirm your age. This video may be inappropriate', 'access_denied'),
    ('Video unavailable. This video has been removed by the uploader', 'not_found'),
    ('Unsupported URL: https://www.youtube.com/', 'error'),
])
def test_youtube_errors_have_reasons(store, fake_yt_dlp, message, reason):
    fake_yt_dlp.error = message
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1', store)
    assert (result.status, result.reason) == (FAILED, reason)


def test_youtube_rate_limit_is_temporary(store, fake_yt_dlp):
    fake_yt_dlp.error = 'Unable to download webpage: HTTP Error 429: Too Many Requests'
    with Context(store) as ctx, pytest.raises(TransientError):
        downloader.ingest('https://www.youtube.com/watch?v=VIDEO1', ctx)


def test_youtube_single_file_format_is_streamed_not_saved_by_yt_dlp(store, fake_yt_dlp, site):
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1', store)
    assert store.path(result.files[0]).read_bytes() == b'video'
    assert site.hits['/videoplayback'] == 1 and len(fake_yt_dlp.calls) == 1
    assert fake_yt_dlp.options[0]['skip_download'] is True       # yt-dlp only chose the format


def test_youtube_format_that_needs_merging_is_downloaded_by_yt_dlp(store, fake_yt_dlp, site):
    fake_yt_dlp.streamable = False
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1', store)
    assert result.status == NEW and store.path(result.files[0]).read_bytes() == b'video'
    assert result.files[1].endswith('.en.vtt')
    assert site.hits['/videoplayback'] == 0 and len(fake_yt_dlp.calls) == 2


def test_options_force_redownloads_youtube(store, fake_yt_dlp):
    downloader.download('https://www.youtube.com/watch?v=VIDEO1', store)
    with Context(store, Options(force=True)) as ctx:
        downloader.ingest('https://www.youtube.com/watch?v=VIDEO1', ctx)
    assert len(fake_yt_dlp.calls) == 2


# --- regressions found in review ------------------------------------------------------------

@pytest.mark.parametrize('url', ['https://youtu.be/abc.def',
                                 'https://www.youtube.com/watch?v=dQw4w9WgXcQ%26t%3D10',
                                 'https://drive.google.com/file/d/' + 'x' * 200 + '/view'])
def test_link_with_an_unusable_id_is_a_failed_result_not_a_crash(site, store, fake_yt_dlp, url):
    site.route('/next.pdf', PDF, content_type='application/pdf')
    results = downloader.download_all([url, site.url('/next.pdf')], store)
    assert (results[0].status, results[0].reason) == (FAILED, 'invalid_url')
    assert (len(results), results[1].status) == (2, NEW)  # and the run went on to the next item


def test_page_that_gains_document_links_changes_type(store):
    with Context(store) as ctx:
        downloader.save_page(page(kind='page_text'), ctx)
        again = downloader.save_page(page(kind='page'), ctx)
    assert (again.status, again.type) == (UNCHANGED, 'page')
    assert store.get(again.key).type == 'page'


def test_same_page_through_a_url_variant_is_unchanged(store):
    with Context(store) as ctx:
        first = downloader.save_page(page(url='https://example.org/a/'), ctx)
        again = downloader.save_page(page(url='https://www.example.org/a'), ctx)
    assert again.key == first.key and again.status == UNCHANGED


def test_tls_errors_are_permanent():
    import requests
    error = downloader.classify_error(requests.exceptions.SSLError('certificate verify failed'))
    assert isinstance(error, downloader.DownloadError) and error.reason == 'error'


# --- splitting work into items: crawl_into_store, expand, JSON round trips -----------------

def test_crawl_into_store_stores_pages_as_it_goes(site, store):
    site.page('/', '<a href="/a/">A</a> <h2>Talk</h2><a href="/deck.pdf">Slides</a>')
    site.page('/a/', '<p>About.</p>')
    site.route('/deck.pdf', PDF, content_type='application/pdf')
    with Context(store) as ctx:
        report = downloader.crawl_into_store(site.url('/'), ctx, 1)
        assert [(r.url.replace(site.base, ''), r.type) for r in report.assets] == [
            ('/deck.pdf', 'file')]
        assert [p.status for p in report.pages] == [NEW, NEW] and not report.truncated
        for r in report.results:
            assert r.text == r.html == ''        # stored, not carried around
        page_keys = {r.key for r in report.results if r.type in ('page', 'page_text')}
        assert page_keys == {p.key for p in report.pages} and all(page_keys)

        result = downloader.ingest(report.assets[0], ctx)
        assert result.status == NEW
        record = store.get(result.key)
        assert (record.found_on, record.link_text, record.context) == ([site.url('/')], 'Slides',
                                                                       'Talk')

        again = downloader.crawl_into_store(site.url('/'), ctx, 1)
        assert [p.status for p in again.pages] == [UNCHANGED, UNCHANGED]


def test_crawl_into_store_reports_a_cut_short_crawl(site, store):
    site.page('/', '<a href="/a/">A</a> <a href="/b/">B</a>')
    site.page('/a/', '<p>A</p>')
    site.page('/b/', '<p>B</p>')
    with Context(store) as ctx:
        report = downloader.crawl_into_store(site.url('/'), ctx, 1, max_pages=2)
    assert report.truncated and len(report.pages) == 2


def test_expand_leaves_a_non_folder_as_itself_without_requests(store):
    with Context(store, drive=FakeDrive()) as ctx:
        for url in ('https://www.youtube.com/watch?v=VIDEO1', 'https://example.org/a.pdf'):
            assert [i.url for i in downloader.expand(url, ctx).items] == [url]
    drive = FakeDrive()
    with Context(store, drive=drive) as ctx:
        url = 'https://docs.google.com/presentation/d/deck/edit'
        assert [i.url for i in downloader.expand(url, ctx).items] == [url]


def test_expand_api_folder_into_files(store):
    with Context(store, drive=FakeDrive()) as ctx:
        expanded = downloader.expand(ROOT_URL, ctx)
    assert expanded.title == 'Slides 2026' and expanded.failures == []
    assert sorted((i.url.split('/')[5], i.folder_path, i.found_on[0].rsplit('/', 1)[1])
                  for i in expanded.items) == [
        ('deck', 'Slides 2026', 'root'), ('doc', 'Slides 2026 / Day 2', 'sub'),
        ('other', 'Slides 2026', 'root'),          # through the shortcut
        ('pdf', 'Slides 2026', 'root'), ('pptx', 'Slides 2026', 'root')]
    # (the Google Form and the shortcut back to root are left out)


def test_expanded_items_survive_a_json_round_trip(store):
    """Work items go between steps as JSON: Source.of() must read asdict() back intact."""
    import json
    from dataclasses import asdict
    drive = FakeDrive()
    with Context(store, drive=drive) as ctx:
        items = downloader.expand(ROOT_URL, ctx).items
        wire = json.loads(json.dumps([asdict(i) for i in items]))
        assert [downloader.Source.of(d) for d in wire] == items
        results = [downloader.ingest(d, ctx) for d in wire]
    assert {r.status for r in results} == {NEW}
    notes = store.get('google_drive/doc')
    assert (notes.link_text, notes.folder_path) == ('Notes', 'Slides 2026 / Day 2')


def test_expand_public_folder_with_a_private_subfolder(store, monkeypatch):
    tree = {
        'root': ('Slides 2026', [('Day 1', 'https://drive.google.com/drive/folders/day1'),
                                 ('Private', 'https://drive.google.com/drive/folders/secret'),
                                 ('Form', 'https://docs.google.com/forms/d/F/viewform'),
                                 ('a.pdf', 'https://drive.google.com/file/d/A/view')]),
        'day1': ('Day 1', [('Talk', 'https://docs.google.com/presentation/d/T/edit'),
                           ('Back up', 'https://drive.google.com/drive/folders/root')]),
    }

    def fake_list(folder_id, session=None):
        if folder_id not in tree:
            raise downloader.DownloadError('HTTP 403', downloader.Reason.ACCESS_DENIED)
        return tree[folder_id]
    monkeypatch.setattr(downloader.drive, 'list_google_folder', fake_list)

    with Context(store) as ctx:
        expanded = downloader.expand(ROOT_URL, ctx)
    assert [(i.link_text, i.folder_path) for i in expanded.items] == [
        ('Talk', 'Slides 2026 / Day 1'), ('a.pdf', 'Slides 2026')]
    assert [(f.title, f.reason) for f in expanded.failures] == [('Private', 'access_denied')]
    # (the Form is left out, as through the API)


def test_expand_raises_when_a_listing_fails_temporarily(store):
    drive = FakeDrive().fail_listing(DriveAPIError(503, 'Drive API HTTP 503: Backend Error'))
    with Context(store, drive=drive) as ctx, pytest.raises(TransientError):
        downloader.expand(ROOT_URL, ctx)


def test_cli_crawl_then_download(site, tmp_path):
    site.page('/', '<h2>Talk</h2><a href="/deck.pdf">Slides</a>')
    site.route('/deck.pdf', PDF, content_type='application/pdf')
    code, results, err = run_cli(['--crawl', site.url('/'), '--depth', '1'], tmp_path)
    assert code == 0, err
    assert [(r['type'], r['status']) for r in results] == [('page', 'new'), ('file', 'new')]


# --- regressions found in the second review ------------------------------------------------

def test_folder_that_cannot_be_listed_keeps_its_reason(store):
    drive = FakeDrive().fail_listing(DriveAPIError(403, 'Drive API HTTP 403: no permission'))
    result = downloader.download(ROOT_URL, store, drive=drive)
    assert (result.status, result.reason, result.url) == (FAILED, 'access_denied', ROOT_URL)
    assert result.children == []


def test_expand_lists_a_file_once_even_if_reachable_twice(store):
    drive = FakeDrive()
    original = drive.list_folder
    drive.list_folder = lambda f: original(f) + ([drive.files['other']] if f == 'root' else [])
    with Context(store, drive=drive) as ctx:
        urls = [i.url for i in downloader.expand(ROOT_URL, ctx).items]
    assert len(urls) == len(set(urls))


def test_expand_needs_no_request_for_a_file_link(store):
    class NoCalls(FakeDrive):
        def get(self, *a, **k):
            raise AssertionError('no request expected')
    with Context(store, drive=NoCalls()) as ctx:
        url = 'https://drive.google.com/file/d/pdf/view'
        assert [i.url for i in downloader.expand(url, ctx).items] == [url]


def test_drive_upload_whose_export_fails_falls_back_to_the_original(store, monkeypatch):
    """Google answers some big uploads' PDF export with HTTP 432: use the uploaded file."""
    import requests
    drive = FakeDrive()
    real_save = downloader._save

    def save(ctx, url, ext=None, **kw):
        if '/export?' in url:
            response = requests.Response()
            response.status_code, response.url = 432, url
            raise requests.HTTPError('432 Client Error', response=response)
        return real_save(ctx, url, ext, **kw)
    monkeypatch.setattr(downloader.drive, '_save', save)
    result = downloader.download('https://docs.google.com/presentation/d/pptx/edit', store,
                                 drive=drive)
    assert (result.status, result.files) == (NEW, ['google_drive/pptx.pptx'])
    assert drive.calls == [('download', 'pptx')]


def test_cli_crawl_reports_a_failed_start_page(site, tmp_path):
    code, results, _ = run_cli(['--crawl', site.url('/missing/')], tmp_path)
    assert code == 1
    assert [(r['status'], r['reason']) for r in results] == [('failed', 'error')]


# --- YouTube quality ------------------------------------------------------------------------

def test_youtube_format_prefers_single_files_and_needs_ffmpeg_only_to_merge():
    with_ffmpeg = downloader._youtube_format(360, False, have_ffmpeg=True).split('/')
    without = downloader._youtube_format(360, False, have_ffmpeg=False).split('/')
    assert with_ffmpeg[0] == without[0] == 'b[height<=360][ext=mp4][vcodec^=avc1]'
    assert not any('+' in f for f in without)        # nothing that needs merging
    assert any('+' in f for f in with_ffmpeg)
    assert without[-1] == 'w' and 'b' not in without  # fallback: lowest quality, not highest
    assert downloader._youtube_format(360, True, True) == 'ba[ext=m4a]/ba'


def test_youtube_default_quality_is_low_and_recorded(store, fake_yt_dlp):
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1', store)
    assert store.get(result.key).source_version == 'video<=360p'


def test_youtube_quality_change_downloads_again(store, fake_yt_dlp):
    url = 'https://www.youtube.com/watch?v=VIDEO1'
    downloader.download(url, store, max_height=720)
    assert downloader.download(url, store, max_height=720).status == UNCHANGED
    downloader.download(url, store)  # now at the default, 360p
    assert len(fake_yt_dlp.calls) == 2
    assert store.get('youtube/VIDEO1').source_version == 'video<=360p'


# --- dedupe: one work item per stored item ---------------------------------------------------

DECK = 'https://docs.google.com/presentation/d/deck/edit'


def test_dedupe_merges_the_same_deck_linked_from_several_pages():
    items = downloader.dedupe([
        {'url': DECK + '?usp=sharing', 'type': 'google_drive', 'found_on': 'https://x.org/a',
         'title': 'Slides', 'context': 'Keynote'},
        {'url': 'https://www.youtube.com/watch?v=VIDEO1', 'type': 'youtube',
         'found_on': 'https://x.org/a'},
        {'url': DECK + '?usp=drive_link', 'type': 'google_drive', 'found_on': 'https://x.org/b',
         'title': 'Deck', 'context': 'Keynote recap'},
        {'url': 'https://youtu.be/VIDEO1', 'found_on': 'https://x.org/b'},
    ])
    assert [i.type for i in items] == ['google_drive', 'youtube']  # first-appearance order
    deck, video = items
    assert deck.found_on == ['https://x.org/a', 'https://x.org/b']
    assert (deck.link_text, deck.context) == ('Slides', 'Keynote')  # the first page's link
    assert video.found_on == ['https://x.org/a', 'https://x.org/b']


def test_dedupe_keeps_both_the_page_context_and_the_folder_path():
    from_folder = downloader.Source(url='https://drive.google.com/file/d/deck/view',
                                    type='google_drive', found_on=[ROOT_URL],
                                    link_text='Keynote deck.pptx', folder_path='Slides 2026')
    from_page = downloader.Source(url=DECK, type='google_drive', found_on=['https://x.org/a'],
                                  link_text='Slides', context='Keynote')
    [item] = downloader.dedupe([from_folder, from_page])
    assert (item.link_text, item.context, item.folder_path) == ('Slides', 'Keynote', 'Slides 2026')
    assert item.found_on == [ROOT_URL, 'https://x.org/a']


@pytest.mark.parametrize('original, chosen', [(False, DECK), (True, 'https://drive.google.com/file/d/deck/view')])
def test_dedupe_picks_the_drive_link_that_matches_the_options(original, chosen):
    links = ['https://drive.google.com/file/d/deck/view', DECK]
    for order in (links, links[::-1]):  # whichever comes first
        [item] = downloader.dedupe(order, Options(original=original))
        assert item.url == chosen


def test_dedupe_leaves_distinct_items_alone():
    urls = ['https://x.org/a.pdf', 'https://x.org/b.pdf', DECK,
            'https://drive.google.com/drive/folders/root', 'https://www.youtube.com/watch?v=V2']
    assert [i.url for i in downloader.dedupe(urls)] == urls


def test_deduped_items_survive_a_json_round_trip():
    import json
    from dataclasses import asdict
    [item] = downloader.dedupe([{'url': DECK, 'found_on': 'https://x.org/a', 'context': 'K'},
                                {'url': DECK, 'found_on': 'https://x.org/b'}])
    assert downloader.Source.of(json.loads(json.dumps(asdict(item)))) == item


def test_download_all_stores_a_deck_once_when_linked_directly_and_in_a_folder(store):
    drive = FakeDrive()
    entries = [
        {'url': ROOT_URL, 'type': 'google_drive', 'found_on': 'https://x.org/slides',
         'title': 'All slides'},
        {'url': DECK, 'type': 'google_drive', 'found_on': 'https://x.org/schedule',
         'title': 'Slides', 'context': 'Opening keynote'},
    ]
    results = downloader.download_all(entries, store, drive=drive)
    assert [c for c in drive.calls if c[1] == 'deck'] == [
        ('export', 'deck', 'application/pdf')]                      # downloaded once
    assert sum(r.key == 'google_drive/deck' for r in results) == 1   # reported once
    record = store.get('google_drive/deck')
    assert (record.link_text, record.context, record.folder_path) == (
        'Slides', 'Opening keynote', 'Slides 2026')
    assert set(record.found_on) == {ROOT_URL, 'https://x.org/schedule'}


# --- regressions found in the dedupe review --------------------------------------------------

def test_download_all_reports_each_listing_failure_once_and_a_row_per_folder(store):
    drive = FakeDrive()
    drive.files['bad'] = {'id': 'bad', 'name': 'Gone', 'mimeType': G + 'shortcut',
                          'shortcutDetails': {'targetId': 'deleted'}}
    DRIVE_CHILDREN_COPY = {**DRIVE_CHILDREN, 'sub': DRIVE_CHILDREN['sub'] + ['bad']}
    drive.list_folder = lambda f: [drive.files[c] for c in DRIVE_CHILDREN_COPY[f]]
    entries = [ROOT_URL, 'https://drive.google.com/drive/folders/sub']  # sub is inside root too
    results = downloader.download_all(entries, store, drive=drive)
    assert [r.reason for r in results if r.status == FAILED] == ['not_found']
    rows = {r.url: r for r in results if not r.key}
    assert rows[ROOT_URL].title == 'Slides 2026' and rows[ROOT_URL].reason == 'partial'
    assert 'google_drive/doc.pdf' in rows['https://drive.google.com/drive/folders/sub'].files
    assert sum(r.key == 'google_drive/doc' for r in results) == 1  # the file itself: once


def test_download_all_reports_an_empty_folder(store):
    drive = FakeDrive()
    drive.files['empty'] = {'id': 'empty', 'name': 'Nothing yet', 'mimeType': G + 'folder'}
    drive.list_folder = lambda f: []
    [row] = downloader.download_all(['https://drive.google.com/drive/folders/empty'], store,
                                    drive=drive)
    assert (row.title, row.status, row.files) == ('Nothing yet', UNCHANGED, [])


def test_old_records_with_the_folder_path_in_context_are_cleaned_up(store):
    drive = FakeDrive()
    downloader.download(ROOT_URL, store, drive=drive)
    old = store.get('google_drive/pdf')
    old.context, old.folder_path = 'Slides 2026', ''  # as the previous version stored it
    store.put_record(old)
    downloader.download(ROOT_URL, store, drive=drive)
    record = store.get('google_drive/pdf')
    assert (record.context, record.folder_path) == ('', 'Slides 2026')


def test_dedupe_keeps_the_fullest_folder_path_whatever_the_order():
    a = downloader.Source(url='https://drive.google.com/file/d/doc/view', folder_path='Day 2')
    b = downloader.Source(url='https://drive.google.com/file/d/doc/view',
                          folder_path='Slides 2026 / Day 2')
    for order in ([a, b], [b, a]):
        assert downloader.dedupe(order)[0].folder_path == 'Slides 2026 / Day 2'


# --- characterization: CLI and Panopto paths (pinned before refactoring) ----------------------

def test_cli_from_json_then_rerun(site, tmp_path):
    import json
    site.route('/a.pdf', PDF, content_type='application/pdf')
    crawl = [{'url': 'https://example.org/', 'type': 'page_text', 'title': 'Home', 'text': 'Hi',
              'html': '<p>Hi</p>', 'depth': 0},
             {'url': site.url('/a.pdf'), 'type': 'file', 'depth': 1,
              'found_on': 'https://example.org/'}]
    (tmp_path / 'crawl.json').write_text(json.dumps(crawl))
    code, out, err = run_cli(['--from-json', str(tmp_path / 'crawl.json')], tmp_path)
    assert code == 0 and [(r['type'], r['status']) for r in out] == [('page_text', 'new'),
                                                                    ('file', 'new')]
    assert 'Done: 2 new' in err
    code, out, err = run_cli(['--from-json', str(tmp_path / 'crawl.json')], tmp_path)
    assert code == 0 and [r['status'] for r in out] == ['unchanged', 'unchanged']


def test_cli_plain_urls_and_exit_code_on_failure(site, tmp_path):
    site.route('/a.pdf', PDF, content_type='application/pdf')
    code, out, _ = run_cli([site.url('/a.pdf'), site.url('/missing.pdf')], tmp_path)
    assert code == 1
    assert [(r['status'], r['reason']) for r in out] == [('new', ''), ('failed', 'not_found')]


def test_cli_without_arguments_is_a_usage_error(tmp_path):
    code, _, err = run_cli([], tmp_path)
    assert code == 2 and 'give one or more URLs' in err


def test_panopto_without_captions(site, store):
    site.route(f'/Panopto/Podcast/Download/{SESSION}.mp4?mediaTargetType=videoPodcast',
               b'mp4 data', content_type='video/mp4')
    url = site.url(f'/Panopto/Pages/Viewer.aspx?id={SESSION}')
    result = downloader.download(url, store, kind='panopto', captions=False)
    assert result.files == [f'panopto/{SESSION}.mp4']
    assert not any('GenerateSRT' in path for path in site.hits)


def test_panopto_missing_captions_are_not_a_failure(site, store):
    site.route(f'/Panopto/Podcast/Download/{SESSION}.mp4?mediaTargetType=videoPodcast',
               b'mp4 data', content_type='video/mp4')
    url = site.url(f'/Panopto/Pages/Viewer.aspx?id={SESSION}')  # GenerateSRT -> 404
    result = downloader.download(url, store, kind='panopto')
    assert (result.status, result.files) == (NEW, [f'panopto/{SESSION}.mp4'])


def test_panopto_session_that_needs_a_sign_in(site, store):
    site.route(f'/Panopto/Podcast/Download/{SESSION}.mp4?mediaTargetType=videoPodcast',
               b'<html>Sign in</html>', content_type='text/html')
    result = downloader.download(site.url(f'/Panopto/Pages/Viewer.aspx?id={SESSION}'), store,
                                 kind='panopto')
    assert (result.status, result.reason) == (FAILED, 'access_denied')


def test_a_fetcher_registered_at_run_time_is_used(store, monkeypatch):
    """FETCHERS is the registry: adding a fetcher there is enough (from review of the split)."""
    def fetch_custom(source, ctx):
        return downloader.DownloadResult(url=source.url, type='custom', status=NEW)
    monkeypatch.setitem(downloader.FETCHERS, 'custom', fetch_custom)
    with Context(store) as ctx:
        result = downloader.ingest({'url': 'https://example.org/x', 'type': 'custom'}, ctx)
        assert downloader.dedupe([{'url': 'https://example.org/x', 'type': 'custom'}])[0].kind == 'custom'
    assert (result.type, result.status) == ('custom', NEW)


# --- captions that appear after the video was stored ------------------------------------------

class Clock:
    """A settable clock for i2cc_scraper.store.now()."""
    def __init__(self):
        from datetime import datetime, timezone
        self.t = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

    def advance(self, **kwargs):
        from datetime import timedelta
        self.t += timedelta(**kwargs)

    def __call__(self) -> str:
        return self.t.isoformat(timespec='seconds')


@pytest.fixture
def clock(monkeypatch):
    from i2cc_scraper import store as store_module
    c = Clock()
    monkeypatch.setattr(store_module, 'now', c)
    return c


YT = 'https://www.youtube.com/watch?v=VIDEO1'


def test_captions_that_appear_later_are_added_without_the_video_again(store, fake_yt_dlp, clock):
    fake_yt_dlp.captions = False
    first = downloader.download(YT, store)
    assert first.files == ['youtube/VIDEO1.mp4']
    video_stamp = (store.path('youtube/VIDEO1.mp4').stat().st_mtime_ns,
                   store.get('youtube/VIDEO1').content_sha256)

    clock.advance(hours=2)                        # too soon to look again
    assert downloader.download(YT, store).status == UNCHANGED
    assert len(fake_yt_dlp.calls) == 1

    clock.advance(hours=24)                       # due: still none -> noted, nothing changes
    assert downloader.download(YT, store).status == UNCHANGED
    assert len(fake_yt_dlp.calls) == 2
    assert store.get('youtube/VIDEO1').captions_checked_at == clock()

    fake_yt_dlp.captions = True
    clock.advance(hours=24)                       # due again, and now there are captions
    result = downloader.download(YT, store)
    assert (result.status, result.files) == (UPDATED, ['youtube/VIDEO1.mp4',
                                                     'youtube/VIDEO1.en.vtt'])
    assert fake_yt_dlp.options[-1]['skip_download'] is True          # the video wasn't fetched
    assert (store.path('youtube/VIDEO1.mp4').stat().st_mtime_ns,
            store.get('youtube/VIDEO1').content_sha256) == video_stamp
    assert store.path('youtube/VIDEO1.en.vtt').read_text() == 'WEBVTT\n'

    clock.advance(days=3)                         # has captions: never looked at again
    assert downloader.download(YT, store).status == UNCHANGED
    assert len(fake_yt_dlp.calls) == 3


def test_caption_rechecks_stop_after_the_window(store, fake_yt_dlp, clock):
    fake_yt_dlp.captions = False
    downloader.download(YT, store)
    for _ in range(14):                           # a day apart, for the 14-day window
        clock.advance(days=1, minutes=1)
        downloader.download(YT, store)
    calls = len(fake_yt_dlp.calls)
    clock.advance(days=3)
    assert downloader.download(YT, store).status == UNCHANGED
    assert len(fake_yt_dlp.calls) == calls        # gave up: no more checks
    fake_yt_dlp.captions = True
    assert downloader.download(YT, store, force=True).files[-1].endswith('.en.vtt')  # --force


def test_caption_rechecks_can_be_turned_off_or_not_wanted(store, fake_yt_dlp, clock):
    from dataclasses import replace

    from i2cc_scraper import config
    fake_yt_dlp.captions = False
    downloader.download(YT, store)
    clock.advance(days=2)
    downloader.download(YT, store, captions=False)            # captions not wanted
    config.set_default(replace(config.default(), caption_recheck_days=0))
    downloader.download(YT, store)                            # re-checking switched off
    assert len(fake_yt_dlp.calls) == 1


def test_a_failed_caption_recheck_does_not_fail_the_stored_video(store, fake_yt_dlp, clock):
    fake_yt_dlp.captions = False
    downloader.download(YT, store)
    clock.advance(days=2)
    fake_yt_dlp.error = 'Video unavailable'
    result = downloader.download(YT, store)
    assert (result.status, result.error) == (UNCHANGED, '')
    assert len(fake_yt_dlp.calls) == 2

    # ... and it counts as a check: not repeated on every run, but again after the interval
    clock.advance(hours=3)
    downloader.download(YT, store)
    assert len(fake_yt_dlp.calls) == 2
    clock.advance(days=1)
    downloader.download(YT, store)
    assert len(fake_yt_dlp.calls) == 3


def test_panopto_captions_that_appear_later(site, store, clock):
    video = f'/Panopto/Podcast/Download/{SESSION}.mp4?mediaTargetType=videoPodcast'
    srt = f'/Panopto/Pages/Transcription/GenerateSRT.ashx?id={SESSION}&language=0'
    site.route(video, b'mp4 data', content_type='video/mp4')
    url = site.url(f'/Panopto/Pages/Viewer.aspx?id={SESSION}')
    assert downloader.download(url, store, kind='panopto').files == [f'panopto/{SESSION}.mp4']

    clock.advance(days=1, minutes=1)
    site.route(srt, b'1\n00:00:01,000 --> 00:00:02,000\nWelcome\n', content_type='text/plain')
    result = downloader.download(url, store, kind='panopto')
    assert (result.status, result.files) == (UPDATED, [f'panopto/{SESSION}.mp4',
                                                     f'panopto/{SESSION}.en.vtt'])
    assert site.hits[video] == 1                                  # the video: downloaded once
    clock.advance(days=5)
    assert downloader.download(url, store, kind='panopto').status == UNCHANGED
    assert site.hits[srt] == 2                                    # not asked again


# --- links that change while a page's text doesn't --------------------------------------------

PAGE_URL = 'https://x.org/schedule/'
DECK_URL = 'https://docs.google.com/presentation/d/deck/edit'
PDF_URL = 'https://drive.google.com/file/d/pdf/view'


def linked_page(*links, text='Agenda', url=PAGE_URL):
    """A crawled page whose text is `text` and which links to `links` (documents)."""
    html = '<main>' + ''.join(f'<a href="{link}">x</a>' for link in links) + '</main>'
    return {'url': url, 'type': 'page', 'title': 'Schedule', 'text': text, 'html': html,
            'links': list(links)}


def sighting(url, page=PAGE_URL):
    return {'url': url, 'type': 'google_drive', 'found_on': page}


def test_link_keys_are_the_documents_and_videos_once_each():
    keys = downloader.link_keys([DECK_URL, DECK_URL + '?usp=sharing', PDF_URL, 'https://x.org/about/',
                                 'https://youtu.be/abc.def', 'https://youtu.be/VIDEO1'])
    assert keys == ['google_drive/deck', 'google_drive/pdf', 'youtube/VIDEO1']


def test_a_swapped_link_rewrites_the_page_and_unlinks_the_old_item(store, clock):
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL), ctx)
        downloader.ingest(sighting(DECK_URL), ctx)
        assert store.get('google_drive/deck').found_on == [PAGE_URL]

        clock.advance(hours=1)
        again = downloader.save_page(linked_page(PDF_URL), ctx)    # same text, different link
        assert again.status == UPDATED
        assert store.get(again.key).links == ['google_drive/pdf']

        deck = store.get('google_drive/deck')
        assert (deck.found_on, deck.unlinked_at) == ([], clock())
        assert store.path('google_drive/deck.pdf').exists()        # nothing is deleted

        downloader.ingest(sighting(PDF_URL), ctx)
        assert store.get('google_drive/pdf').found_on == [PAGE_URL]
        assert store.get('google_drive/pdf').unlinked_at == ''


def test_an_item_still_linked_from_another_page_is_not_flagged(store):
    other = 'https://x.org/recap/'
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL), ctx)
        downloader.save_page(linked_page(DECK_URL, url=other), ctx)
        downloader.ingest(sighting(DECK_URL), ctx)
        downloader.ingest(sighting(DECK_URL, other), ctx)
        downloader.save_page(linked_page(PDF_URL), ctx)            # the schedule drops the deck
    deck = store.get('google_drive/deck')
    assert (deck.found_on, deck.unlinked_at) == ([other], '')


def test_a_link_moved_to_another_page_ends_up_linked(store, clock):
    other = 'https://x.org/recap/'
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL), ctx)
        downloader.ingest(sighting(DECK_URL), ctx)
        clock.advance(hours=1)
        downloader.save_page(linked_page(), ctx)                   # unlinked here ...
        assert store.get('google_drive/deck').unlinked_at == clock()
        downloader.save_page(linked_page(DECK_URL, url=other), ctx)
        downloader.ingest(sighting(DECK_URL, other), ctx)          # ... and linked there
    deck = store.get('google_drive/deck')
    assert (deck.found_on, deck.unlinked_at) == ([other], '')


def test_unlinking_ignores_how_the_page_url_is_spelled(store):
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL), ctx)
        downloader.ingest(sighting(DECK_URL, 'https://www.x.org/schedule'), ctx)  # a variant
        downloader.save_page(linked_page(), ctx)
    assert store.get('google_drive/deck').found_on == []


def test_same_links_in_another_order_is_unchanged(store):
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL, PDF_URL), ctx)
        again = downloader.save_page(linked_page(PDF_URL, DECK_URL), ctx)
    assert again.status == UNCHANGED


def test_pages_stored_before_links_were_recorded_are_just_backfilled(store):
    from dataclasses import replace
    with Context(store) as ctx:
        first = downloader.save_page(linked_page(DECK_URL), ctx)
        store.put_record(replace(store.get(first.key), links=None))   # as an older version wrote it
        before = store.path(first.files[0]).stat().st_mtime_ns
        again = downloader.save_page(linked_page(DECK_URL), ctx)
    assert again.status == UNCHANGED                                  # not re-ingested
    assert store.get(first.key).links == ['google_drive/deck']        # but now tracked
    assert store.path(first.files[0]).stat().st_mtime_ns == before    # files untouched


def test_pages_without_link_information_are_left_alone(store):
    """Entries from an older crawl JSON have no `links`: nothing is tracked or unlinked."""
    with Context(store, drive=FakeDrive()) as ctx:
        downloader.save_page(linked_page(DECK_URL), ctx)
        downloader.ingest(sighting(DECK_URL), ctx)
        old = linked_page(PDF_URL)
        del old['links']
        assert downloader.save_page(old, ctx).status == UNCHANGED
    assert store.get('google_drive/deck').found_on == [PAGE_URL]
    assert store.get('pages/' + downloader.page_key(PAGE_URL).split('/')[1]).links == ['google_drive/deck']


def test_crawl_results_carry_each_pages_document_links(site):
    from i2cc_scraper import scraper
    site.page('/', f'<a href="/a/">A</a> <a href="{DECK_URL}">Slides</a> <a href="/deck.pdf">PDF</a>')
    site.page('/a/', '<p>No documents.</p>')
    results = {r.url.replace(site.base, ''): r for r in scraper.crawl_for_ingest(site.url('/'), 1)}
    assert results['/'].links == [DECK_URL, site.url('/deck.pdf')]
    assert results['/a/'].links == []
    assert results[DECK_URL].links is None                           # only pages have links

    stored = scraper.crawl_for_ingest(site.url('/'), 1, on_page=lambda page: None)
    assert [r.links for r in stored if r.type in ('page', 'page_text')] == [None, None]


def test_swapping_a_linked_file_on_a_real_crawl(site, store, clock):
    """End to end: the page's text never changes; only which file it links to."""
    site.route('/a.pdf', PDF, content_type='application/pdf')
    site.route('/b.pdf', PDF + b' b', content_type='application/pdf')

    def run():
        results = {}
        with Context(store) as ctx:
            report = downloader.crawl_into_store(site.url('/'), ctx, 1)
            results['page'] = report.pages[0].status
            for item in downloader.dedupe(report.assets):
                results[item.url.replace(site.base, '')] = downloader.ingest(item, ctx).status
        return results

    site.page('/', '<p>Agenda</p><a href="/a.pdf">Slides</a>')
    assert run() == {'page': NEW, '/a.pdf': NEW}
    assert run() == {'page': UNCHANGED, '/a.pdf': UNCHANGED}
    clock.advance(hours=3)
    site.page('/', '<p>Agenda</p><a href="/b.pdf">Slides</a>')
    assert run() == {'page': UPDATED, '/b.pdf': NEW}
    old = store.get(downloader.make_key('files', downloader.commit._url_hash(site.url('/a.pdf'))))
    assert (old.found_on, old.unlinked_at) == ([], clock())


@pytest.mark.parametrize('stamp', ['2026-11-12T12:00:00', '2026-11-12', '2026-11-12T12:00:00+00:00',
                                   '2026-11-12T05:00:00-07:00'])
def test_timestamps_without_a_timezone_are_taken_as_utc(stamp):
    from datetime import timezone

    from i2cc_scraper.store import parse_time
    parsed = parse_time(stamp)
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    assert parsed.astimezone(timezone.utc).date().isoformat() == '2026-11-12'
    assert parse_time('') is None and parse_time('not a date') is None


def test_a_record_with_a_hand_written_timestamp_does_not_break_the_caption_check(
        store, fake_yt_dlp, clock):
    from dataclasses import replace
    fake_yt_dlp.captions = False
    downloader.download(YT, store)
    stored = store.get('youtube/VIDEO1')
    store.put_record(replace(stored, fetched_at='2026-11-12T12:00:00',      # no UTC offset
                             captions_checked_at='2026-11-12'))
    clock.advance(days=2)
    fake_yt_dlp.captions = True
    assert downloader.download(YT, store).status == UPDATED


# --- a refused Google export isn't requested again --------------------------------------------

PPTX_EDITOR = 'https://docs.google.com/presentation/d/pptx/edit'   # an uploaded .pptx, opened in Slides


@pytest.fixture
def refused_exports(monkeypatch):
    """Google answers every PDF export with HTTP 432 (too big). Returns the export attempts."""
    import requests
    attempts = []
    real_save = downloader.drive._save

    def save(ctx, url, ext=None, **kw):
        if '/export?' in url:
            attempts.append(url)
            response = requests.Response()
            response.status_code, response.url = 432, url
            raise requests.HTTPError('432 Client Error', response=response)
        return real_save(ctx, url, ext, **kw)
    monkeypatch.setattr(downloader.drive, '_save', save)
    return attempts


def test_a_refused_export_is_not_requested_on_every_run(store, refused_exports):
    drive = FakeDrive()
    first = downloader.download(PPTX_EDITOR, store, drive=drive)
    assert (first.status, first.files) == (NEW, ['google_drive/pptx.pptx'])
    assert len(refused_exports) == 1 and drive.calls == [('download', 'pptx')]
    assert store.get('google_drive/pptx').source_version == 'aaa;format=pptx;export_refused=pdf'

    stamps = {p.name: p.stat().st_mtime_ns for p in (store.root / 'google_drive').iterdir()}
    for _ in range(2):
        again = downloader.download(PPTX_EDITOR, store, drive=drive)
        assert again.status == UNCHANGED
    assert len(refused_exports) == 1                            # not asked again
    assert drive.calls == [('download', 'pptx')]                # nor downloaded
    assert {p.name: p.stat().st_mtime_ns for p in (store.root / 'google_drive').iterdir()} == stamps


def test_a_refused_export_is_asked_for_again_when_the_file_changes(store, refused_exports):
    drive = FakeDrive()
    downloader.download(PPTX_EDITOR, store, drive=drive)
    drive.files['pptx']['md5Checksum'] = 'bbb'
    result = downloader.download(PPTX_EDITOR, store, drive=drive)
    assert result.status == UPDATED and len(refused_exports) == 2
    assert store.get('google_drive/pptx').source_version == 'bbb;format=pptx;export_refused=pdf'


def test_force_asks_for_the_export_again(store, refused_exports):
    drive = FakeDrive()
    downloader.download(PPTX_EDITOR, store, drive=drive)
    downloader.download(PPTX_EDITOR, store, drive=drive, force=True)
    assert len(refused_exports) == 2 and drive.calls.count(('download', 'pptx')) == 2


def test_records_stored_before_the_note_get_it_without_a_download(store, refused_exports):
    """The original was stored as 'aaa;format=pptx' (by an earlier version, or an `original`
    run). The next run asks once more, then records the refusal -- no file download."""
    drive = FakeDrive()
    downloader.download(PPTX_EDITOR, store, drive=drive, original=True)
    assert store.get('google_drive/pptx').source_version == 'aaa;format=pptx'
    assert drive.calls == [('download', 'pptx')] and refused_exports == []

    result = downloader.download(PPTX_EDITOR, store, drive=drive)
    assert result.status == UNCHANGED and len(refused_exports) == 1
    assert drive.calls == [('download', 'pptx')]                 # the 100 MB file: not fetched again
    assert store.get('google_drive/pptx').source_version == 'aaa;format=pptx;export_refused=pdf'

    downloader.download(PPTX_EDITOR, store, drive=drive)
    assert len(refused_exports) == 1                             # and now it stays quiet


# --- YouTube: just the video ------------------------------------------------------------------

def test_a_video_in_a_playlist_link_downloads_only_the_video(store, fake_yt_dlp):
    result = downloader.download('https://www.youtube.com/watch?v=VIDEO1&list=PLx&index=3&t=40s',
                                 store)
    assert result.status == NEW and result.key == 'youtube/VIDEO1'
    assert fake_yt_dlp.calls == ['https://www.youtube.com/watch?v=VIDEO1']      # no list=, no t=
    assert fake_yt_dlp.options[0]['noplaylist'] is True
    assert store.get('youtube/VIDEO1').source_url.endswith('index=3&t=40s')   # the link as given


@pytest.mark.parametrize('url', ['https://youtu.be/VIDEO1?t=5', 'https://music.youtube.com/watch?v=VIDEO1',
                                 'https://www.youtube.com/v/VIDEO1'])
def test_every_video_link_form_reaches_yt_dlp_as_the_plain_watch_url(store, fake_yt_dlp, url):
    downloader.download(url, store)
    assert fake_yt_dlp.calls == ['https://www.youtube.com/watch?v=VIDEO1']


def test_the_caption_recheck_also_uses_the_plain_watch_url(store, fake_yt_dlp, clock):
    fake_yt_dlp.captions = False
    url = 'https://www.youtube.com/watch?v=VIDEO1&list=PLx'
    downloader.download(url, store)
    clock.advance(days=2)
    fake_yt_dlp.captions = True
    downloader.download(url, store)
    assert fake_yt_dlp.calls == ['https://www.youtube.com/watch?v=VIDEO1'] * 2
    assert all(o['noplaylist'] for o in fake_yt_dlp.options)


@pytest.mark.parametrize('url', ['https://www.youtube.com/playlist?list=PL1',
                                 'https://www.youtube.com/@internet2'])
def test_a_youtube_page_that_is_not_a_video_is_unsupported_with_a_clear_message(store, fake_yt_dlp, url):
    result = downloader.download(url, store)
    assert (result.status, result.reason) == (FAILED, 'unsupported')
    assert 'single video' in result.error and fake_yt_dlp.calls == []


def test_download_all_never_silently_drops_a_url_it_cannot_handle(store):
    entries = ['https://example.org/just-a-page', 'https://www.youtube.com/@internet2',
               {'url': 'https://example.org/bad', 'type': 'failed', 'error': '404'},      # a crawl's own failure
               {'url': 'https://example.org/later', 'type': 'not_fetched', 'error': 'stopped'}]
    results = downloader.download_all(entries, store)
    assert [(r.url, r.status, r.reason) for r in results] == [
        ('https://example.org/just-a-page', FAILED, 'unsupported'),
        ('https://www.youtube.com/@internet2', FAILED, 'unsupported')]
    assert 'single video' in results[1].error


def test_cli_reports_a_youtube_page_that_is_not_a_video(tmp_path):
    code, results, _ = run_cli(['https://www.youtube.com/playlist?list=PL1'], tmp_path)
    assert code == 1
    assert [(r['status'], r['reason']) for r in results] == [('failed', 'unsupported')]


# --- YouTube's automatic captions repeat every line: stored once ---------------------------------

ROLLING_VTT = (FIXTURES / 'youtube-rolling.vtt').read_text()


def test_stored_youtube_captions_have_no_repeated_lines(store, fake_yt_dlp):
    fake_yt_dlp.subtitle_text = ROLLING_VTT
    result = downloader.download(YT, store)
    stored = store.path(result.files[1]).read_text()
    assert result.files[1] == 'youtube/VIDEO1.en.vtt'
    assert stored.count('-->') == 5 and stored.count('foremost') == 1
    assert '<' not in stored                                  # and no word-timing markup


def test_captions_added_by_the_later_recheck_are_cleaned_too(store, fake_yt_dlp, clock):
    fake_yt_dlp.captions = False
    downloader.download(YT, store)
    clock.advance(days=2)
    fake_yt_dlp.captions, fake_yt_dlp.subtitle_text = True, ROLLING_VTT
    result = downloader.download(YT, store)
    assert result.status == UPDATED
    stored = store.path('youtube/VIDEO1.en.vtt').read_text()
    assert stored.count('-->') == 5 and stored.count('foremost') == 1


# --- overlapping runs on one item (conditional writes, retry and merge) -----------------------------

class SyncedDrive(FakeDrive):
    """Makes `parties` threads reach the download step together, so they have all read "nothing
    stored yet" before any of them writes."""
    def __init__(self, parties: int):
        super().__init__()
        self.barrier = threading.Barrier(parties)

    def export(self, meta, mime_type):
        self.barrier.wait(timeout=20)
        return super().export(meta, mime_type)


def run_in_threads(count, work):
    results = [None] * count
    errors = []

    def run(i):
        try:
            results[i] = work(i)
        except BaseException as exc:  # surfaced below, with the thread that raised it
            errors.append((i, exc))
    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, errors
    return results


def test_overlapping_runs_on_one_item_lose_no_found_on_entries(store):
    drive = SyncedDrive(4)
    pages = [f'https://x.org/page{i}/' for i in range(4)]

    def ingest(i):
        with Context(store, drive=drive) as ctx:
            return downloader.ingest({'url': DECK, 'type': 'google_drive', 'found_on': pages[i],
                                      'title': f'Slides {i}'}, ctx)
    results = run_in_threads(4, ingest)

    record = store.get('google_drive/deck')
    assert sorted(record.found_on) == sorted(pages)                 # every page is there
    assert sorted(r.status for r in results) == [NEW, UNCHANGED, UNCHANGED, UNCHANGED]
    assert record.files == ['google_drive/deck.pdf'] and record.link_text.startswith('Slides')
    assert store.path('google_drive/deck.pdf').read_bytes().startswith(b'export of deck')


def test_a_page_dropping_a_link_and_a_run_adding_one_do_not_overwrite_each_other(store, clock):
    """unlink_page() and ingest() both rewrite the same Record from opposite directions."""
    drive = FakeDrive()
    with Context(store, drive=drive) as ctx:
        downloader.ingest({'url': DECK, 'type': 'google_drive', 'found_on': 'https://x.org/old/'}, ctx)

    def work(i):
        with Context(store, drive=drive) as ctx:
            if i == 0:
                downloader.unlink_page(ctx, 'google_drive/deck', 'https://x.org/old/')
            else:
                downloader.ingest({'url': DECK, 'type': 'google_drive',
                                   'found_on': f'https://x.org/new{i}/'}, ctx)
    for _ in range(5):                                              # a few rounds: it's a race
        run_in_threads(4, work)
        record = store.get('google_drive/deck')
        assert 'https://x.org/old/' not in record.found_on
        assert {f'https://x.org/new{i}/' for i in (1, 2, 3)} <= set(record.found_on)
        with Context(store, drive=drive) as ctx:                    # reset for the next round
            downloader.ingest({'url': DECK, 'type': 'google_drive', 'found_on': 'https://x.org/old/'}, ctx)


def test_a_record_that_keeps_changing_under_us_is_a_temporary_failure(store, monkeypatch):
    """Not a hang and not a lost update: after a bounded number of tries, ask to be run again."""
    from i2cc_scraper import store as store_module

    def always_conflicts(self, record, *args, **kwargs):
        raise store_module.ConflictError('someone else, again')
    drive = FakeDrive()
    downloader.download(DECK, store, drive=drive)
    monkeypatch.setattr(store_module.LocalStore, 'put_record', always_conflicts)
    monkeypatch.setattr(downloader.commit.time, 'sleep', lambda s: None)
    with Context(store, drive=drive) as ctx, pytest.raises(TransientError, match='kept changing'):
        downloader.ingest({'url': DECK, 'type': 'google_drive', 'found_on': 'https://x.org/new/'}, ctx)


def test_record_includes_rag_metadata_fields(store):
    """New metadata fields for the RAG system are populated and stored."""
    from i2cc_scraper.downloader import Source, _commit
    with Context(store) as ctx:
        with ctx.work_dir() as work:
            test_file = work / 'test.pdf'
            test_file.write_bytes(b'test content')

            # A Drive file in a folder
            source = Source(
                url='https://drive.google.com/file/d/file123/view',
                type='google_drive',
                found_on=['https://drive.google.com/drive/folders/folder456'],
                folder_path='Conference 2026 / Day 1 / Keynotes',
                link_text='Opening Keynote'
            )

            _commit(ctx, source, 'google_drive', 'google_drive/file123',
                    [('.pdf', test_file)], 'Keynote Slides', 'application/pdf', 'v1', None)
    
    record = store.get('google_drive/file123')
    assert record.is_subscriber_content is False
    assert record.parent_folder_name == 'Keynotes'
    assert record.parent_folder_url == 'https://drive.google.com/drive/folders/folder456'
    assert record.relative_start_time is None
    
    # Verify it survives a JSON round trip
    import json
    data = json.loads(record.to_json())
    assert data['is_subscriber_content'] is False
    assert data['parent_folder_name'] == 'Keynotes'
    assert data['parent_folder_url'] == 'https://drive.google.com/drive/folders/folder456'
    assert data['relative_start_time'] is None


def test_page_records_have_empty_folder_metadata(store):
    """Pages don't have folder information, so parent folder fields are empty."""
    with Context(store) as ctx:
        result = downloader.save_page(page(), ctx)
    
    record = store.get(result.key)
    assert record.is_subscriber_content is False
    assert record.parent_folder_name == ''
    assert record.parent_folder_url == ''
    assert record.relative_start_time is None
