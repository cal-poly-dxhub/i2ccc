"""Config from the environment, the start-URL allowlist, and private-address blocking."""
import pytest

from i2cc_scraper import netguard, scraper
from i2cc_scraper.config import Config
from i2cc_scraper.netguard import UnsafeURLError, validate_start_url

PUBLIC_IP = '93.184.216.34'
GUARDED = Config(retry_backoff=0, retry_jitter=0, crawl_delay=0, block_private_addresses=True)


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve names from a table instead of DNS (unknown names don't resolve)."""
    table = {}

    def resolve(host):
        if host not in table:
            raise netguard.socket.gaierror('no such host')
        return [(None, None, None, '', (table[host], 0))]
    monkeypatch.setattr(netguard, '_resolve', resolve)
    return table


# --- Config ---------------------------------------------------------------------------------

def test_config_from_env():
    config = Config.from_env({'I2CC_TIMEOUT': '60', 'I2CC_RETRIES': '5',
                              'I2CC_BLOCK_PRIVATE_ADDRESSES': 'no',
                              'I2CC_ALLOWED_START_DOMAINS': 'Internet2.edu, incommon.org',
                              'UNRELATED': 'x'})
    assert (config.timeout, config.retries, config.block_private_addresses) == (60.0, 5, False)
    assert config.allowed_start_domains == ('internet2.edu', 'incommon.org')
    assert config.user_agent == Config().user_agent  # unset: default


@pytest.mark.parametrize('name, value', [('I2CC_RETRIES', 'three'),
                                         ('I2CC_BLOCK_PRIVATE_ADDRESSES', 'maybe')])
def test_config_from_env_rejects_bad_values(name, value):
    with pytest.raises(ValueError, match=name):
        Config.from_env({name: value})


# --- start URLs -----------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/', 'http://localhost:8080/', 'http://10.0.3.7/', 'http://192.168.1.1/',
    'http://169.254.169.254/latest/meta-data/',  # cloud metadata endpoint
    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://0.0.0.0/',
])
def test_private_addresses_are_refused(fake_dns, url):
    fake_dns['localhost'] = '127.0.0.1'
    with pytest.raises(UnsafeURLError, match='private or internal'):
        validate_start_url(url, GUARDED)


@pytest.mark.parametrize('url', ['ftp://internet2.edu/', 'file:///etc/passwd', 'javascript:x',
                                 'https:///no-host'])
def test_only_http_urls_with_a_host(url):
    with pytest.raises(UnsafeURLError):
        validate_start_url(url, GUARDED)


def test_allowed_start_domains(fake_dns):
    config = Config(allowed_start_domains=('internet2.edu',))
    for host in ('internet2.edu', 'www.internet2.edu', 'spaces.at.internet2.edu',
                 'internet2.edu.evil.com', 'evilinternet2.edu'):
        fake_dns[host] = PUBLIC_IP
    for ok in ('https://internet2.edu/', 'https://www.internet2.edu/cloud-forum/',
               'https://spaces.at.internet2.edu/x'):
        assert validate_start_url(ok, config) == ok
    for bad in ('https://internet2.edu.evil.com/', 'https://evilinternet2.edu/'):
        with pytest.raises(UnsafeURLError, match='not an allowed domain'):
            validate_start_url(bad, config)


def test_crawl_refuses_a_disallowed_start_url():
    config = Config(allowed_start_domains=('internet2.edu',), block_private_addresses=False)
    with pytest.raises(UnsafeURLError):
        scraper.crawl_for_ingest('https://example.com/', 1, config=config)


def test_direct_asset_links_skip_the_allowlist():
    """A staff member may submit a YouTube or Drive link directly: it isn't crawled."""
    config = Config(allowed_start_domains=('internet2.edu',))
    results = scraper.crawl_for_ingest('https://www.youtube.com/watch?v=VIDEO1', 1, config=config)
    assert [r.type for r in results] == ['youtube']


# --- every request, including redirects ----------------------------------------------------

def test_crawling_the_local_server_is_refused_when_guarded(site):
    site.page('/', '<p>internal</p>')
    with pytest.raises(UnsafeURLError):
        scraper.crawl_for_ingest(site.url('/'), 1, config=GUARDED)
    assert site.hits['/'] == 0


def test_redirect_to_a_private_address_is_refused(site, fake_dns):
    # The page is reached as "localhost", which our fake DNS calls public (the real connection
    # still goes to the local test server); it redirects to 127.0.0.1, which must be refused.
    fake_dns['localhost'] = PUBLIC_IP
    site.route('/go', b'', status=302, headers={'Location': site.url('/secret')})
    site.route('/secret', b'<p>internal</p>')
    with pytest.raises(UnsafeURLError):
        scraper.fetch(site.url('/go').replace('127.0.0.1', 'localhost'), config=GUARDED)
    assert (site.hits['/go'], site.hits['/secret']) == (1, 0)


def test_config_from_env_accepts_floats_for_float_fields():
    config = Config.from_env({'I2CC_TIMEOUT': '2.5', 'I2CC_RETRY_AFTER_MAX': '0.5'})
    assert (config.timeout, config.retry_after_max) == (2.5, 0.5)


# --- YouTube is never crawled ------------------------------------------------------------------

VIDEO_LINKS = ['https://www.youtube.com/watch?v=VIDEO1', 'https://youtu.be/VIDEO1',
               'https://music.youtube.com/watch?v=VIDEO1', 'https://www.youtube.com/v/VIDEO1',
               'https://www.youtube.com/watch?v=VIDEO1&list=PL1&index=2']
OTHER_YOUTUBE = ['https://www.youtube.com/playlist?list=PL1', 'https://www.youtube.com/@internet2',
                 'https://www.youtube.com/channel/UC1', 'https://www.youtube.com/results?search_query=x',
                 'https://www.youtube.com/', 'https://studio.youtube.com/']


@pytest.fixture
def no_page_fetches(monkeypatch):
    """Fail if the crawler tries to read any page."""
    def fetch(url, *args, **kwargs):
        raise AssertionError(f'tried to fetch {url}')
    monkeypatch.setattr(scraper, 'fetch', fetch)


@pytest.mark.parametrize('url', VIDEO_LINKS)
def test_a_video_link_is_not_crawled_just_returned_as_a_video(no_page_fetches, url):
    results = scraper.crawl_for_ingest(url, 2)
    assert [(r.url, r.type) for r in results] == [(url, 'youtube')]
    assert scraper.scrape(url).type == 'youtube' and scraper.find_urls(url) == []


@pytest.mark.parametrize('url', OTHER_YOUTUBE)
def test_other_youtube_pages_are_refused_not_crawled(no_page_fetches, url):
    for call in (lambda: scraper.crawl_for_ingest(url, 1), lambda: scraper.crawl(url, 1),
                 lambda: scraper.scrape(url), lambda: scraper.find_urls(url)):
        with pytest.raises(UnsafeURLError, match='never crawled'):
            call()


def test_youtube_is_refused_even_when_it_is_an_allowed_domain():
    config = Config(allowed_start_domains=('youtube.com',), block_private_addresses=False)
    with pytest.raises(UnsafeURLError, match='never crawled'):
        validate_start_url('https://www.youtube.com/playlist?list=PL1', config)


def test_a_link_to_a_youtube_page_is_not_followed_even_if_youtube_is_allowed(site):
    """The test server would answer a request for a YouTube URL with a connection error that the
    no-internet guard turns into a failure -- so this also proves nothing was requested."""
    site.page('/', '<a href="https://www.youtube.com/playlist?list=PL1">Playlist</a>'
                   '<a href="/about/">About</a>')
    site.page('/about/', '<p>About.</p>')
    results = scraper.crawl_for_ingest(site.url('/'), 2, allowed_domains=['127.0.0.1', 'youtube.com'])
    urls = {r.url.replace(site.base, '') for r in results}
    assert urls == {'/', '/about/'}
