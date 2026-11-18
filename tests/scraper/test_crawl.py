"""Crawling a small local site: depth, documents, filtering, failures, and the `found` set."""
import pytest

from i2cc_scraper import scraper

YOUTUBE = 'https://www.youtube.com/watch?v=VIDEO1'
DRIVE_2025 = 'https://docs.google.com/presentation/d/DECK2025/edit'
NAV = '<nav><a href="/contact/">Contact</a></nav>'
FOOTER = '<footer><a href="/privacy/">Privacy</a></footer>'


@pytest.fixture
def events_site(site):
    """
    /events/                 page: a video, a PDF, and links to the pages below
      /events/2025/          page: a Drive deck, and a link to /events/2025/talks/
        /events/2025/talks/  page_text
      /about/                page_text
      /missing/              404
      /waf/                  AWS WAF challenge
      http://localhost:.../elsewhere/   another domain
    """
    site.page('/events/', f'''
        <h2>Keynote</h2><a href="{YOUTUBE}">Recording</a>
        <a href="/files/handout.pdf">Handout</a>
        <a href="/events/2025/">2025</a> <a href="/about/">About</a>
        <a href="/missing/">Broken</a> <a href="/waf/">Wiki</a>
        <a href="/get?doc=agenda">Agenda</a> <a href="/export?fmt=raw">Data</a>
        <a href="/">Home</a>
        <a href="{site.base.replace("127.0.0.1", "localhost")}/elsewhere/">Elsewhere</a>
        ''', extra=NAV + FOOTER)
    site.page('/events/2025/', f'''
        <a href="/events/">Back to events</a>
        <h2>Panel</h2><a href="{DRIVE_2025}">Slides</a>
        <a href="/events/2025/talks/">Talks</a>''', extra=NAV)
    site.page('/events/2025/talks/', '<p>Talk descriptions.</p>')
    site.page('/about/', '<p>About us.</p>')
    site.page('/elsewhere/', '<p>On another domain.</p>')
    site.route('/waf/', b'', status=202, headers={'x-amzn-waf-action': 'challenge'})
    site.route('/get?doc=agenda', b'%PDF-1.4 ' + b'x' * 100_000, content_type='application/pdf')
    site.route('/export?fmt=raw', b'data', content_type='application/octet-stream')
    return site


def by_url(results, site):
    return {r.url.replace(site.base, ''): r for r in results}


def test_depth_1_reports_pages_documents_and_failures(events_site):
    results = scraper.crawl_for_ingest(events_site.url('/events/'), max_depth=1, delay=0)
    found = by_url(results, events_site)

    assert found['/events/'].type == scraper.PAGE and found['/events/'].depth == 0
    assert found['/events/2025/'].type == scraper.PAGE
    assert found['/about/'].type == scraper.PAGE_TEXT
    assert found['/missing/'].type == scraper.FAILED and '404' in found['/missing/'].error
    assert found['/waf/'].type == scraper.FAILED and 'WAF' in found['/waf/'].error

    video = found[YOUTUBE]
    assert (video.type, video.found_on, video.title, video.context) == (
        'youtube', events_site.url('/events/'), 'Recording', 'Keynote')
    assert found['/files/handout.pdf'].type == 'file'

    # Pages at the depth limit are read, so their documents are reported...
    assert found[DRIVE_2025].found_on == events_site.url('/events/2025/')
    # ...but their page links aren't followed.
    assert '/events/2025/talks/' not in found
    # Navigation, homepage, breadcrumbs, and other domains are never followed.
    for skipped in ('/contact/', '/privacy/', '/', '/elsewhere/'):
        assert skipped not in found
    assert not any('localhost' in r.url for r in results)


def test_pages_have_text_and_html(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), 1, delay=0), events_site)
    assert 'About us.' in found['/about/'].text
    assert found['/about/'].html.startswith('<html>')
    assert 'Keynote' in found['/events/'].text
    assert found[YOUTUBE].html == '' and found[YOUTUBE].text == ''


def test_links_only_drops_text_of_pages_with_documents(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), 1, delay=0,
                                            text_with_links=False), events_site)
    assert found['/events/'].text == '' and found['/events/'].html == ''
    assert found['/about/'].text == 'About us.'  # page_text pages keep their text


def test_depth_0_is_the_start_page_and_its_documents(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), 0, delay=0), events_site)
    assert set(found) == {'/events/', YOUTUBE, '/files/handout.pdf'}


def test_depth_2_follows_one_more_level(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), 2, delay=0), events_site)
    assert found['/events/2025/talks/'].type == scraper.PAGE_TEXT
    assert found['/events/2025/talks/'].depth == 2


def test_allowed_domains_can_include_other_domains(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), 1, delay=0,
                                            allowed_domains=['127.0.0.1', 'localhost']),
                   events_site)
    elsewhere = [r for r in found.values() if r.url.endswith('/elsewhere/')]
    assert len(elsewhere) == 1 and elsewhere[0].type == scraper.PAGE_TEXT


def test_documents_are_never_fetched(events_site):
    scraper.crawl_for_ingest(events_site.url('/events/'), 1, delay=0)
    assert events_site.hits['/files/handout.pdf'] == 0


def test_found_set_is_shared_between_crawls(events_site):
    found: set[str] = set()
    first = scraper.crawl_for_ingest(events_site.url('/events/'), 1, found=found, delay=0)
    second = scraper.crawl_for_ingest(events_site.url('/events/2025/'), 1, found=found, delay=0)
    assert len(first) > 5
    # /events/2025/ was found by the first crawl: it's crawled again but not reported again,
    # and nothing it links to is new except the talks page.
    assert [r.url for r in second] == [events_site.url('/events/2025/talks/')]


def test_max_pages_stops_the_crawl(events_site):
    results = scraper.crawl_for_ingest(events_site.url('/events/'), 2, delay=0, max_pages=1)
    assert events_site.hits['/events/'] == 1
    assert events_site.hits['/events/2025/'] == 0
    # The pages it didn't get to are reported, not silently left looking like pages:
    unread = [r for r in results if r.type == scraper.NOT_FETCHED]
    assert {r.url.replace(events_site.base, '') for r in unread} >= {'/events/2025/', '/about/'}
    assert all('max_pages=1' in r.error for r in unread)


def test_time_limit_stops_the_crawl(events_site, monkeypatch):
    clock = iter(range(0, 1000, 10))  # every reading of the clock is 10 s later
    monkeypatch.setattr(scraper.time, 'monotonic', lambda: next(clock))
    results = scraper.crawl_for_ingest(events_site.url('/events/'), 2, delay=0, time_limit=15)
    assert events_site.hits['/events/'] == 1 and events_site.hits['/events/2025/'] == 0
    unread = [r for r in results if r.type == scraper.NOT_FETCHED]
    assert unread and all('time limit (15 s)' in r.error for r in unread)


def test_on_page_gets_each_page_and_the_results_drop_its_content(events_site):
    seen = []

    def on_page(page):
        seen.append((page.url.replace(events_site.base, ''), bool(page.text), bool(page.html)))
        page.key = 'pages/x'

    results = scraper.crawl_for_ingest(events_site.url('/events/'), 1, on_page=on_page)
    assert ('/events/', True, True) in seen and ('/about/', True, True) in seen
    pages = [r for r in results if r.type in (scraper.PAGE, scraper.PAGE_TEXT)]
    assert pages and all(r.text == r.html == '' and r.key == 'pages/x' for r in pages)


def test_url_mode_does_not_read_the_last_level(events_site):
    urls = scraper.crawl(events_site.url('/events/'), 1, delay=0)
    assert events_site.url('/events/2025/') in urls
    assert DRIVE_2025 not in urls          # /events/2025/ itself wasn't fetched
    assert events_site.hits['/events/2025/'] == 0


def test_direct_document_start_url(events_site):
    results = scraper.crawl_for_ingest(YOUTUBE, 2, delay=0)
    assert [(r.url, r.type) for r in results] == [(YOUTUBE, 'youtube')]


def test_extensionless_document_link_is_a_file_by_content_type(events_site):
    found = by_url(scraper.crawl_for_ingest(events_site.url('/events/'), max_depth=1), events_site)
    agenda = found['/get?doc=agenda']
    assert (agenda.type, agenda.error, agenda.found_on) == (
        scraper.FILE, '', events_site.url('/events/'))
    # Unknown non-HTML types are still failures:
    assert found['/export?fmt=raw'].type == scraper.FAILED and 'octet-stream' in found['/export?fmt=raw'].error


def test_a_later_crawl_sharing_found_reads_the_pages_a_cut_short_one_missed(events_site):
    found = set()
    scraper.crawl_for_ingest(events_site.url('/events/'), 1, found=found, max_pages=1)
    assert events_site.hits['/about/'] == 0
    scraper.crawl_for_ingest(events_site.url('/events/'), 1, found=found)
    assert events_site.hits['/about/'] == 1
