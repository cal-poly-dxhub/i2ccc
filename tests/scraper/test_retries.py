"""Temporary failures are retried; permanent ones fail at once."""
import time

import pytest
import requests

from dataclasses import replace

from conftest import OK_PAGE, flaky
from i2cc_scraper import config, downloader, scraper

@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_temporary_errors_are_retried(site, status):
    site.routes['/flaky'] = flaky(2, status)
    assert scraper.fetch(site.url('/flaky')).content == OK_PAGE
    assert site.hits['/flaky'] == 3


def test_gives_up_after_the_retry_limit(site):
    site.routes['/down'] = flaky(99)
    with pytest.raises(requests.HTTPError):
        scraper.fetch(site.url('/down'))
    assert site.hits['/down'] == 1 + config.default().retries


@pytest.mark.parametrize('status', [403, 404])
def test_permanent_errors_are_not_retried(site, status):
    site.route('/gone', b'no', status=status)
    with pytest.raises(requests.HTTPError):
        scraper.fetch(site.url('/gone'))
    assert site.hits['/gone'] == 1


def test_waf_challenge_is_not_retried(site):
    site.route('/wiki', b'', status=202, headers={'x-amzn-waf-action': 'challenge'})
    with pytest.raises(RuntimeError, match='WAF'):
        scraper.fetch(site.url('/wiki'))
    assert site.hits['/wiki'] == 1


def test_retry_after_is_honoured_but_capped(site):
    config.set_default(replace(config.default(), retry_after_max=0.2))
    site.routes['/slow-down'] = flaky(1, 429, {'Retry-After': '3600'})
    start = time.monotonic()
    assert scraper.fetch(site.url('/slow-down')).content == OK_PAGE
    assert time.monotonic() - start < 5  # waited ~0.2 s, not an hour


def test_crawl_survives_a_flaky_page(site):
    site.page('/start/', '<a href="/flaky/">Flaky</a>')
    site.routes['/flaky/'] = flaky(2)
    results = scraper.crawl_for_ingest(site.url('/start/'), 1, delay=0)
    assert [r.type for r in results] == [scraper.PAGE_TEXT, scraper.PAGE_TEXT]


# --- downloads interrupted partway through -----------------------------------------------------

BODY = bytes(range(256)) * 400  # 100 KB


def drops_connection(times: int):
    """A route that sends part of the file and then hangs up, `times` times."""
    def handler(request, hit):
        request.send_response(200)
        request.send_header('Content-Type', 'application/pdf')
        request.send_header('Content-Length', str(len(BODY)))
        request.end_headers()
        if hit <= times:
            request.wfile.write(BODY[:1000])
            request.close_connection = True
        else:
            request.wfile.write(BODY)
    return handler


def test_interrupted_download_is_started_again(site, store):
    site.routes['/talk.pdf'] = drops_connection(2)
    result = downloader.download(site.url('/talk.pdf'), store)
    assert result.error == ''
    assert site.hits['/talk.pdf'] == 3
    assert store.path(result.files[0]).read_bytes() == BODY
    assert not list(store.root.rglob('*.part'))


def test_download_that_keeps_breaking_is_a_temporary_failure(site, store):
    site.routes['/talk.pdf'] = drops_connection(99)
    result = downloader.download(site.url('/talk.pdf'), store)
    assert (result.status, result.reason, result.files) == ('failed', 'unavailable', [])
    assert site.hits['/talk.pdf'] == config.default().download_attempts
    assert not any(p.is_file() for p in store.root.rglob('*'))  # nothing left behind


def test_ingest_raises_temporary_failures_for_the_caller_to_retry(site, store):
    site.routes['/talk.pdf'] = flaky(99, 503)
    with downloader.Context(store) as ctx, pytest.raises(downloader.TransientError):
        downloader.ingest(site.url('/talk.pdf'), ctx)


def test_download_retries_before_it_starts(site, store):
    def handler(request, hit):
        if hit <= 2:
            request._send(503, {'Content-Type': 'text/plain'}, b'busy')
        else:
            request._send(200, {'Content-Type': 'application/pdf'}, BODY)
    site.routes['/busy.pdf'] = handler
    result = downloader.download(site.url('/busy.pdf'), store)
    assert result.error == '' and store.path(result.files[0]).read_bytes() == BODY
