"""The terminal UI, driven headlessly (Textual's test pilot) against the local test server:
what each row says while it works and when it's done -- in particular whether a file was
really downloaded or just found unchanged."""
import asyncio
import logging
import time
from pathlib import Path

import pytest

pytest.importorskip('textual')

import tui  # noqa: E402
from i2cc_scraper import downloader  # noqa: E402

PDF = b'%PDF-1.4 ' + b'x' * 20_000
BUSY = (tui.CRAWLING, tui.QUEUED, tui.CHECKING, tui.DOWNLOADING)


@pytest.fixture(autouse=True)
def keep_logging_intact():
    """The app replaces the root logger's handlers; put them back for the other tests."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def streamed_pdf(etag: str, body: bytes = PDF):
    """A PDF with an ETag whose body arrives slowly (headers first), so a real download is
    visible in the UI for a moment; a conditional request gets a 304."""
    def handler(request, hit):
        if request.headers.get('If-None-Match') == etag:
            request.send_response(304)
            request.send_header('ETag', etag)
            request.end_headers()
            return
        request.send_response(200)
        request.send_header('Content-Type', 'application/pdf')
        request.send_header('ETag', etag)
        request.send_header('Content-Length', str(len(body)))
        request.end_headers()
        request.wfile.write(body[:1000])
        request.wfile.flush()
        time.sleep(0.6)
        request.wfile.write(body[1000:])
    return handler


async def run_session(out: Path, url: str):
    """One TUI session: add `url`, wait until every row has settled. Returns the statuses the
    deck row went through, the row, and the status text the table showed for it at the end."""
    app = tui.IngestApp(out, 1)
    seen: list[str] = []
    async with app.run_test() as pilot:
        app.add_source(url, 1)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await pilot.pause(0.03)
            deck = next((i for i in app.items.values() if i.url.endswith('/deck.pdf')), None)
            if deck is not None and (not seen or seen[-1] != deck.status):
                seen.append(deck.status)
            if app.items and not any(i.status in BUSY for i in app.items.values()):
                break
        await pilot.pause(0.2)
        shown = str(app.query_one(tui.DataTable).get_cell(deck.id, 'status'))
    return seen, deck, shown


def test_a_download_and_a_skip_look_different(site, tmp_path):
    site.page('/', '<h2>Keynote</h2><a href="/deck.pdf">Slides</a>')
    site.routes['/deck.pdf'] = streamed_pdf('"v1"')
    out = tmp_path / 'downloads'

    seen, deck, shown = asyncio.run(run_session(out, site.url('/')))
    assert tui.DOWNLOADING in seen and seen[-1] == tui.NEW                   # really downloaded
    assert deck.fetched and shown == '✓ New' and deck.outcome_note() == 'downloaded and stored'

    seen, deck, shown = asyncio.run(run_session(out, site.url('/')))         # a new session
    assert tui.DOWNLOADING not in seen and not deck.fetched                  # nothing downloaded
    assert seen[-1] == tui.UNCHANGED and shown == '✓ Unchanged'
    assert deck.outcome_note() == 'already stored — nothing downloaded'


def test_a_changed_file_is_shown_as_updated(site, tmp_path):
    site.page('/', '<a href="/deck.pdf">Slides</a>')
    site.routes['/deck.pdf'] = streamed_pdf('"v1"')
    out = tmp_path / 'downloads'
    asyncio.run(run_session(out, site.url('/')))
    site.routes['/deck.pdf'] = streamed_pdf('"v2"', PDF + b' revised')       # new ETag, new bytes
    seen, deck, shown = asyncio.run(run_session(out, site.url('/')))
    assert deck.fetched and shown == '✓ Updated'


def test_files_without_a_change_marker_say_they_were_downloaded_to_compare(site, tmp_path):
    site.page('/', '<a href="/deck.pdf">Slides</a>')
    site.route('/deck.pdf', PDF, content_type='application/pdf')              # no ETag at all
    out = tmp_path / 'downloads'
    asyncio.run(run_session(out, site.url('/')))
    seen, deck, shown = asyncio.run(run_session(out, site.url('/')))
    assert shown == '✓ Unchanged' and deck.fetched       # it had to download to find out
    assert deck.outcome_note() == 'downloaded to compare — identical, nothing written'


def test_records_made_by_the_ui_say_where_the_file_was_found(site, tmp_path):
    site.page('/', '<h2>Keynote</h2><a href="/deck.pdf">Slides</a>')
    site.route('/deck.pdf', PDF, content_type='application/pdf')
    out = tmp_path / 'downloads'
    asyncio.run(run_session(out, site.url('/')))
    key = downloader.make_key('files', downloader.commit._url_hash(site.url('/deck.pdf')))
    record = downloader.LocalStore(out).get(key)
    assert record.found_on == [site.url('/')]
    assert (record.link_text, record.context) == ('Slides', 'Keynote')


def test_a_new_etag_with_identical_bytes_is_still_unchanged(site, tmp_path):
    site.page('/', '<a href="/deck.pdf">Slides</a>')
    site.routes['/deck.pdf'] = streamed_pdf('"v1"')
    out = tmp_path / 'downloads'
    asyncio.run(run_session(out, site.url('/')))
    site.routes['/deck.pdf'] = streamed_pdf('"v2"')                          # same bytes
    seen, deck, shown = asyncio.run(run_session(out, site.url('/')))
    assert deck.fetched and shown == '✓ Unchanged'
    assert deck.outcome_note() == 'downloaded to compare — identical, nothing written'


def test_a_youtube_video_link_goes_straight_to_download_without_a_crawl(monkeypatch, tmp_path):
    """Whatever the link carries (a playlist, a timestamp), the row is queued for download and
    nothing is crawled. The download itself is stubbed: no network."""
    def crawl(*args, **kwargs):
        raise AssertionError('a video link must not be crawled')
    monkeypatch.setattr(tui.scraper, 'crawl_for_ingest', crawl)
    ingested = []

    def ingest(source, ctx):
        ingested.append(source)
        return downloader.DownloadResult(url=source.url, type='youtube', status='new',
                                         key='youtube/VIDEO1', files=['youtube/VIDEO1.mp4'],
                                         title='A talk')
    monkeypatch.setattr(tui.downloader, 'ingest', ingest)
    url = 'https://www.youtube.com/watch?v=VIDEO1&list=PL1&index=3'

    async def go():
        app = tui.IngestApp(tmp_path / 'downloads', 1)
        async with app.run_test() as pilot:
            app.add_source(url, 1)
            row = next(iter(app.items.values()))
            first = row.status
            for _ in range(200):
                await pilot.pause(0.03)
                if row.status not in BUSY:
                    break
            return first, row
    first, row = asyncio.run(go())
    assert first == tui.QUEUED                       # not "Crawling"
    assert (row.status, row.kind) == (tui.NEW, 'youtube')
    assert [s.url for s in ingested] == [url] and ingested[0].type == 'youtube'


def test_a_youtube_playlist_link_is_refused_in_the_ui_without_a_request(tmp_path):
    async def go():
        app = tui.IngestApp(tmp_path / 'downloads', 1)
        async with app.run_test() as pilot:
            app.add_source('https://www.youtube.com/playlist?list=PL1', 1)
            row = next(iter(app.items.values()))
            for _ in range(200):
                await pilot.pause(0.03)
                if row.status not in BUSY:
                    break
            return row
    row = asyncio.run(go())                          # a request would trip the no-internet guard
    assert row.status == tui.ERROR and 'never crawled' in row.error
