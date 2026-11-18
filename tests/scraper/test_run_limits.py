"""One bad item can't end a run, and a run can't outlive its time budget."""
import errno
import logging
import time
from collections import Counter
from dataclasses import replace

import pytest
import requests

from conftest import TEST_CONFIG, run_cli
from i2cc_scraper import scraper
from i2cc_scraper.downloader import (DEFERRED, FAILED, NEW, UNCHANGED, UPDATED, Context,
                                     DeadlineExceeded, DiskFullError, TransientError, api, commit,
                                     http, models, plan)
from i2cc_scraper import downloader
from i2cc_scraper.drive_api import DriveAPIError
from test_downloader import DECK, PDF, ROOT_URL, FakeDrive

FILE_LINK = 'https://drive.google.com/file/d/{}/view'


# --- one bad item doesn't end the run -------------------------------------------------------------

@pytest.fixture
def three_files(site):
    for i in (1, 2, 3):
        site.route(f'/d{i}.pdf', PDF + str(i).encode(), content_type='application/pdf')
    return [site.url(f'/d{i}.pdf') for i in (1, 2, 3)]


def break_second(monkeypatch, error: BaseException) -> None:
    """Make the fetcher for website files fail with `error` on the second file."""
    real = api.FETCHERS['file']

    def fetch(source, ctx):
        if source.url.endswith('/d2.pdf'):
            raise error
        return real(source, ctx)
    monkeypatch.setitem(api.FETCHERS, 'file', fetch)


def test_an_unexpected_error_fails_that_item_not_the_run(store, three_files, monkeypatch, caplog):
    break_second(monkeypatch, ValueError('a bug in the fetcher'))
    with caplog.at_level(logging.ERROR):
        results = downloader.download_all(three_files, store)
    assert [r.status for r in results] == [NEW, FAILED, NEW]       # the one after it still ran
    assert results[1].reason == 'error' and 'ValueError: a bug in the fetcher' in results[1].error
    assert any('Unexpected error' in r.getMessage() and r.exc_info for r in caplog.records)
    assert len(list((store.root / 'files').glob('*.pdf'))) == 2


def test_the_same_bad_item_every_run_does_not_starve_the_ones_after_it(store, three_files, monkeypatch):
    """A scheduled run would hit the same item first every time."""
    break_second(monkeypatch, ValueError('always'))
    for _ in range(3):
        results = downloader.download_all(three_files, store)
        assert [r.status for r in results] == [UNCHANGED if _ else NEW, FAILED, UNCHANGED if _ else NEW]


def test_a_full_disk_stops_the_run_and_defers_the_rest(site, store, three_files, monkeypatch):
    break_second(monkeypatch, OSError(errno.ENOSPC, 'No space left on device'))
    results = downloader.download_all(three_files, store)
    assert [r.status for r in results] == [NEW, FAILED, DEFERRED]
    assert [r.reason for r in results[1:]] == ['disk_full', 'disk_full']
    assert 'filled up' in results[2].error and site.hits['/d3.pdf'] == 0     # not even tried


def test_other_local_errors_are_just_that_items_problem(store, three_files, monkeypatch):
    break_second(monkeypatch, OSError(errno.EACCES, 'Permission denied'))
    assert [r.status for r in downloader.download_all(three_files, store)] == [NEW, FAILED, NEW]


def test_an_unexpected_error_while_listing_a_folder_fails_only_that_folder(site, store):
    site.route('/a.pdf', PDF, content_type='application/pdf')
    drive = FakeDrive()
    drive.list_folder = lambda folder_id: (_ for _ in ()).throw(RuntimeError('listing blew up'))
    results = downloader.download_all([ROOT_URL, site.url('/a.pdf')], store, drive=drive)
    assert [(r.status, r.reason) for r in results] == [(FAILED, 'error'), (NEW, '')]
    assert 'RuntimeError: listing blew up' in results[0].error


def test_a_page_that_cannot_be_stored_does_not_end_the_crawl(site, store, monkeypatch):
    site.page('/', '<a href="/a/">A</a> <a href="/b/">B</a> <a href="/deck.pdf">Slides</a>')
    site.page('/a/', '<p>A</p>')
    site.page('/b/', '<p>B</p>')
    real = plan.save_page

    def save(page, ctx):
        if page.url.endswith('/b/'):
            raise OSError(errno.EIO, 'disk error')
        return real(page, ctx)
    monkeypatch.setattr(plan, 'save_page', save)
    with Context(store) as ctx:
        report = downloader.crawl_into_store(site.url('/'), ctx, 1)
    assert [(p.status, p.reason) for p in report.pages] == [(NEW, ''), (NEW, ''), (FAILED, 'error')]
    assert [a.url for a in report.assets] == [site.url('/deck.pdf')]       # the crawl finished


# --- the time budget --------------------------------------------------------------------------------

class Ticker:
    """A clock a test moves by hand (for Context's time budget)."""
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ticker(monkeypatch):
    t = Ticker()
    monkeypatch.setattr(models, '_seconds', t)
    return t


def test_without_a_budget_nothing_is_limited(store):
    with Context(store) as ctx:
        assert ctx.time_left() is None and ctx.can_start()
        ctx.check_deadline()                                         # doesn't raise


def test_the_budget_counts_down_to_the_reserve_and_then_the_deadline(store, ticker):
    with Context(store, budget=100, reserve=30) as ctx:
        assert ctx.time_left() == 100 and ctx.can_start()
        ticker.now += 69
        assert ctx.can_start()                                       # 31 s left: more than the reserve
        ticker.now += 1
        assert not ctx.can_start() and ctx.time_left() == 30         # at the reserve: no new items
        ctx.check_deadline()                                         # ... but work underway may go on
        ticker.now += 31
        with pytest.raises(DeadlineExceeded) as raised:
            ctx.check_deadline()
    assert isinstance(raised.value, TransientError) and raised.value.reason == 'timeout'


def test_guard_stops_a_transfer_between_chunks_when_time_is_up(store, ticker):
    with Context(store, budget=10) as ctx:
        seen = []
        with pytest.raises(DeadlineExceeded):
            for chunk in ctx.guard(iter([b'a', b'b', b'c', b'd'])):
                seen.append(chunk)
                ticker.now += 4                                      # each chunk "takes" 4 s
    assert seen == [b'a', b'b', b'c']


def test_request_timeouts_shrink_with_the_time_left_but_never_below_a_second(store, ticker):
    with Context(store, config=TEST_CONFIG, budget=100) as ctx:
        assert scraper.timeout_of(ctx.session) == TEST_CONFIG.timeout       # 30 s, plenty of time
        ticker.now += 90
        assert scraper.timeout_of(ctx.session) == 10
        ticker.now += 95
        assert scraper.timeout_of(ctx.session) == 1.0


def test_retry_sleeps_never_outlast_the_budget(site, store):
    """A server saying "try again in 5 s" would otherwise cost three 5-second sleeps."""
    site.routes['/busy'] = lambda request, hit: request._send(
        503, {'Content-Type': 'text/plain', 'Retry-After': '5'}, b'busy')
    with Context(store, budget=1.5) as ctx:
        started = time.monotonic()
        with pytest.raises(requests.HTTPError):
            scraper.fetch(site.url('/busy'), ctx.session)
    assert time.monotonic() - started < 4


class SlowDrive(FakeDrive):
    """A Drive where fetching any file takes `seconds` on the test clock."""
    def __init__(self, ticker: Ticker, seconds: float):
        super().__init__()
        self.ticker, self.seconds = ticker, seconds

    def export(self, meta, mime_type):
        self.ticker.now += self.seconds
        return super().export(meta, mime_type)

    def download(self, file_id):
        self.ticker.now += self.seconds
        return super().download(file_id)


FOUR = [DECK, FILE_LINK.format('pdf'), FILE_LINK.format('pptx'), FILE_LINK.format('other')]


def test_items_not_reached_when_time_runs_out_are_deferred_and_a_rerun_finishes_them(store, ticker):
    drive = SlowDrive(ticker, 10)
    results = downloader.download_all(FOUR, store, drive=drive, budget=25, reserve=5)
    assert [r.status for r in results] == [NEW, NEW, DEFERRED, DEFERRED]    # 25 -> 15 -> 5: stop
    deferred = results[2]
    assert deferred.reason == 'timeout' and deferred.key == 'google_drive/pptx'
    assert 'ran out of time' in deferred.error and deferred.files == []
    assert store.get('google_drive/pptx') is None                           # nothing was done for it

    again = downloader.download_all(FOUR, store, drive=SlowDrive(ticker, 10))
    assert [r.status for r in again] == [UNCHANGED, UNCHANGED, NEW, NEW]    # carries on where it left off


def test_a_budget_that_is_already_spent_defers_everything(store, ticker):
    results = downloader.download_all(FOUR, store, drive=FakeDrive(), budget=0, reserve=0)
    assert [r.status for r in results] == [DEFERRED] * 4
    assert not any(store.root.rglob('*.pdf')) if store.root.exists() else True


def test_a_drive_folder_cut_short_is_reported_as_partly_done(store, ticker):
    results = downloader.download_all([ROOT_URL], store, drive=SlowDrive(ticker, 10),
                                      budget=25, reserve=5)
    folder = results[-1]
    # something got done; the rest wasn't reached -- which is not a failure of any item
    assert (folder.url, folder.status, folder.reason) == (ROOT_URL, UPDATED, 'timeout')
    assert '3 item(s) not reached' in folder.error and len(folder.files) == 2
    assert [r.status for r in results[:-1]].count(DEFERRED) == 3


def test_a_folder_not_reached_at_all_is_deferred_as_a_whole(store, ticker):
    results = downloader.download_all([ROOT_URL], store, drive=FakeDrive(), budget=0, reserve=0)
    assert [(r.url, r.status) for r in results] == [(ROOT_URL, DEFERRED)]


def test_a_transfer_that_outlasts_the_budget_is_cut_off_cleanly(site, store):
    """The first MiB arrives at once, the rest slowly: the budget (1 s) runs out between chunks,
    the transfer is dropped -- no partial file -- and the next item isn't started."""
    mib = 1 << 20

    def slow(request, hit):
        request.send_response(200)
        request.send_header('Content-Type', 'application/pdf')
        request.send_header('Content-Length', str(4 * mib))
        request.end_headers()
        try:
            for _ in range(4):
                request.wfile.write(b'x' * mib)
                request.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError):
            pass                                                     # we hung up on it, as meant
    site.routes['/big.pdf'] = slow
    started = time.monotonic()
    results = downloader.download_all([site.url('/big.pdf'), site.url('/after.pdf')], store,
                                      budget=1.0, reserve=0.1)
    assert time.monotonic() - started < 1.9                          # not the 2.8 s the body takes
    assert [(r.status, r.reason) for r in results] == [(FAILED, 'timeout'), (DEFERRED, 'timeout')]
    assert site.hits['/after.pdf'] == 0
    assert not [p for p in store.root.rglob('*') if p.is_file() and not p.name.startswith('.i2cc')]


def test_a_video_download_is_cut_off_when_the_budget_runs_out(store, fake_yt_dlp, ticker):
    """yt-dlp reports progress as it goes; our hook raises when time is up (verified against the
    real yt-dlp, which lets the exception through unchanged)."""
    fake_yt_dlp.during_download = lambda: setattr(ticker, 'now', ticker.now + 20)
    with Context(store, budget=10) as ctx, pytest.raises(DeadlineExceeded):
        downloader.ingest('https://www.youtube.com/watch?v=VIDEO1', ctx)
    assert store.get('youtube/VIDEO1') is None


def test_the_crawl_gets_a_share_of_the_budget(store, ticker, monkeypatch):
    seen = {}

    def fake_crawl(url, depth, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return []
    monkeypatch.setattr(plan, 'crawl_for_ingest', fake_crawl)
    start = 'https://example.org/'
    with Context(store, budget=100, reserve=10) as ctx:
        downloader.crawl_into_store(start, ctx, 1)
        assert seen['time_limit'] == pytest.approx(36)               # 40% of the 90 s after the reserve
        downloader.crawl_into_store(start, ctx, 1, crawl_share=0.1)
        assert seen['time_limit'] == pytest.approx(9)
        downloader.crawl_into_store(start, ctx, 1, time_limit=5)     # an explicit limit wins
        assert seen['time_limit'] == 5
        downloader.crawl_into_store(start, ctx, 1, time_limit=None)  # None means "not given"
        assert seen['time_limit'] == pytest.approx(36)
    with Context(store, config=replace(TEST_CONFIG, crawl_time_limit=7), budget=100, reserve=10) as ctx:
        downloader.crawl_into_store(start, ctx, 1)
        assert seen['time_limit'] == 7                               # the configured cap is lower
    with Context(store) as ctx:                                      # no budget: untouched
        downloader.crawl_into_store(start, ctx, 1)
        assert seen.get('time_limit') is None


def test_a_drive_folder_with_a_failure_and_a_deferral_reports_the_failure(store, ticker):
    drive = SlowDrive(ticker, 10)
    drive.fail['deck'] = DriveAPIError(403, 'Drive API HTTP 403: no permission')
    folder = downloader.download_all([ROOT_URL], store, drive=drive, budget=25, reserve=5)[-1]
    assert folder.reason == 'partial' and 'no permission' in folder.error and 'not reached' in folder.error


def test_cli_an_item_cut_off_mid_transfer_means_run_again_not_failure(site, tmp_path):
    mib = 1 << 20

    def slow(request, hit):
        request.send_response(200)
        request.send_header('Content-Type', 'application/pdf')
        request.send_header('Content-Length', str(4 * mib))
        request.end_headers()
        try:
            for _ in range(4):
                request.wfile.write(b'x' * mib)
                request.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError):
            pass
    site.page('/', '<a href="/big.pdf">Big</a> <a href="/small.pdf">Small</a>')
    site.routes['/big.pdf'] = slow
    site.route('/small.pdf', PDF, content_type='application/pdf')
    code, results, err = run_cli(['--crawl', site.url('/'), '--depth', '1', '--budget', '1.2',
                                  '--reserve', '0.1'], tmp_path)
    by_url = {r['url'].rsplit('/', 1)[-1]: r for r in results}
    assert (by_url['big.pdf']['status'], by_url['big.pdf']['reason']) == ('failed', 'timeout')
    assert by_url['small.pdf']['status'] == 'deferred'
    assert code == 3 and 'run again' in err                           # not 1: no item is at fault


def test_cli_a_real_failure_still_exits_1_even_if_time_also_ran_out(site, tmp_path):
    site.page('/', '<a href="/gone.pdf">Gone</a>')                    # 404
    code, results, _ = run_cli(['--crawl', site.url('/'), '--depth', '1', '--budget', '30'], tmp_path)
    assert code == 1 and [r['reason'] for r in results if r['status'] == 'failed'] == ['not_found']


def test_cli_budget_stops_cleanly_and_a_second_run_finishes_the_job(site, tmp_path):
    def slow_pdf(i):
        def handler(request, hit):
            time.sleep(0.4)
            request._send(200, {'Content-Type': 'application/pdf'}, PDF + str(i).encode())
        return handler
    site.page('/', ''.join(f'<a href="/d{i}.pdf">D{i}</a>' for i in range(10)))
    for i in range(10):
        site.routes[f'/d{i}.pdf'] = slow_pdf(i)

    code, results, err = run_cli(['--crawl', site.url('/'), '--depth', '1', '--budget', '3',
                                  '--reserve', '1'], tmp_path)
    first = Counter(r['status'] for r in results)
    assert code == 3 and 'run again' in err                          # not 1: nothing failed
    assert first['failed'] == 0 and first['deferred'] >= 2 and first['new'] >= 3
    assert all(r['reason'] == 'timeout' for r in results if r['status'] == 'deferred')

    code, results, err = run_cli(['--crawl', site.url('/'), '--depth', '1'], tmp_path)
    second = Counter(r['status'] for r in results)
    assert code == 0 and second['deferred'] == 0
    assert second['new'] == first['deferred']                        # exactly the ones left over
    assert second['unchanged'] == first['new']                       # all that was stored before


# --- found by review: the stop signals must not be swallowed on their way up -------------------------

def test_a_full_disk_while_saving_a_page_stops_the_run(store, monkeypatch):
    """save_page doesn't classify its own errors; the loop has to."""
    def save(page, ctx):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(plan, 'save_page', save)
    pages = [{'url': f'https://example.org/{n}/', 'type': 'page_text', 'title': n, 'text': 'x',
              'html': '<p>x</p>'} for n in 'ab']
    results = downloader.download_all(pages, store)
    assert [(r.status, r.reason) for r in results] == [(FAILED, 'disk_full'), (DEFERRED, 'disk_full')]


def test_a_crawl_that_cannot_store_a_page_for_lack_of_disk_says_so(site, store, monkeypatch):
    site.page('/', '<p>Home</p>')

    def save(page, ctx):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(plan, 'save_page', save)
    with Context(store) as ctx:
        report = downloader.crawl_into_store(site.url('/'), ctx, 0)
    assert [(p.status, p.reason) for p in report.pages] == [(FAILED, 'disk_full')]


@pytest.mark.parametrize('second, expected', [
    (DeadlineExceeded('out of time'), DeadlineExceeded),
    (OSError(errno.ENOSPC, 'No space left on device'), DiskFullError),
])
def test_when_the_second_drive_route_runs_out_of_time_or_disk_that_is_what_is_reported(
        store, monkeypatch, second, expected):
    """Public links: the export is tried, then the original. "Both failed" must not hide that
    the second one stopped because the run's time or disk ran out."""
    urls = []

    def save(ctx, url, work, ext=None, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise downloader.DownloadError('the export was refused')
        raise second
    monkeypatch.setattr(downloader.drive, '_save', save)
    with Context(store) as ctx, pytest.raises(expected):
        downloader.ingest('https://docs.google.com/presentation/d/ABC123/edit', ctx)
    assert len(urls) == 2


def test_when_both_drive_routes_fail_for_good_the_first_error_is_reported(store, monkeypatch):
    calls = []

    def save(ctx, url, work, ext=None, **kwargs):
        calls.append(url)
        raise downloader.DownloadError(f'refused {len(calls)}', downloader.Reason.ACCESS_DENIED)
    monkeypatch.setattr(downloader.drive, '_save', save)
    result = downloader.download('https://docs.google.com/presentation/d/ABC123/edit', store)
    assert (result.status, result.reason, result.error) == (FAILED, 'access_denied', 'refused 1')


@pytest.mark.parametrize('error, expected', [
    (DeadlineExceeded('out of time'), DeadlineExceeded),
    (OSError(errno.ENOSPC, 'No space left on device'), DiskFullError),
])
def test_a_folder_stops_at_the_first_file_that_runs_out_of_time_or_disk(store, error, expected):
    drive = FakeDrive()
    for file_id in ('deck', 'doc', 'pdf', 'other', 'pptx'):
        drive.fail[file_id] = error
    with Context(store, drive=drive) as ctx, pytest.raises(expected):
        downloader.ingest(ROOT_URL, ctx)
    assert len(drive.calls) == 1                                    # not one attempt per file


def test_a_folder_still_carries_on_past_an_ordinary_temporary_failure(store):
    drive = FakeDrive()
    drive.fail['deck'] = DriveAPIError(503, 'Drive API HTTP 503: busy')
    with Context(store, drive=drive) as ctx, pytest.raises(TransientError, match='temporarily'):
        downloader.ingest(ROOT_URL, ctx)
    assert store.get('google_drive/pdf') is not None                # the others were stored


def test_a_crawl_cut_short_by_a_limit_means_run_again_not_failure(site, tmp_path):
    site.page('/', '<a href="/a/">A</a> <a href="/b/">B</a>')
    site.page('/a/', '<p>A</p>')
    site.page('/b/', '<p>B</p>')
    code, results, err = run_cli(['--crawl', site.url('/'), '--depth', '1'], tmp_path,
                                 env={'I2CC_MAX_PAGES': '1'})
    assert code == 3 and 'run again' in err
    assert sorted(r['reason'] for r in results if r['status'] == 'failed') == ['not_fetched'] * 2


def test_save_checks_the_clock_before_each_attempt(site, store, ticker):
    with Context(store, budget=5) as ctx:
        ticker.now += 10
        with pytest.raises(DeadlineExceeded):
            http._save(ctx, site.url('/never.pdf'), None)
    assert site.hits['/never.pdf'] == 0                             # not even asked


def test_a_conflict_retry_does_not_stamp_an_old_version_onto_new_content(store):
    """We found the content unchanged and meant to note the source's version; meanwhile another
    run stored different content under a newer version. Theirs must stay."""
    from dataclasses import replace
    drive = FakeDrive()
    downloader.download(DECK, store, drive=drive)
    ours = store.get('google_drive/deck')
    theirs = replace(ours, content_sha256='f' * 64, source_version='NEWER;format=pdf')
    store.put_record(theirs)                                        # their write lands first
    with Context(store) as ctx:
        commit._unchanged(ctx, downloader.Source(url=DECK, type='google_drive',
                                                 found_on=['https://x.org/p/']),
                          ours, source_version='OLDER;format=pdf')
    merged = store.get('google_drive/deck')
    assert merged.source_version == 'NEWER;format=pdf'              # not overwritten with OLDER
    assert merged.content_sha256 == 'f' * 64 and merged.found_on == ['https://x.org/p/']
