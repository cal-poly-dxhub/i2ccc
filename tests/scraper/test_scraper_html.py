"""Reading pages: link filtering, document discovery, text, and page types -- on small synthetic
pages for each rule, and on saved snapshots of real Internet2 pages as regression tests."""
from bs4 import BeautifulSoup

from i2cc_scraper import scraper
from conftest import fixture_html

PAGE = 'https://example.org/events/2025/'

# A page with site chrome around the main content.
CHROME_PAGE = f'''<html><head><title>Events 2025</title></head><body>
<header><a href="/login">Log in</a> <a href="/news/">News</a></header>
<nav><a href="/about/">About</a> <a href="https://www.youtube.com/watch?v=navvideo">Nav video</a></nav>
<main>
  <p class="breadcrumbs"><a href="/">Home</a> / <a href="/events/">Events</a></p>
  <h2>Opening keynote</h2>
  <p><a href="https://docs.google.com/presentation/d/KEYNOTE/edit">Slides</a></p>
  <p><a href="/events/2025/keynote/">Abstract</a>
     <a href="/events/2025/#schedule">Jump to schedule</a>
     <a href="mailto:events@example.org">Email us</a>
     <a href="/wp-content/uploads/map.png">Map</a>
     <a href="/feed/">RSS</a></p>
  <h2>Panel</h2>
  <iframe src="https://www.youtube.com/embed/PANELVID" title="Panel recording"></iframe>
  <a href="/wp-content/uploads/handout.pdf">Handout</a>
  <form><button>Sign up</button></form>
</main>
<aside><a href="/sidebar-page/">Related</a></aside>
<footer><a href="https://www.youtube.com/channel/UCxyz">Our channel</a> <a href="/privacy/">Privacy</a></footer>
</body></html>'''


class TestExtractUrls:
    def test_only_main_content_links_are_followed(self):
        urls = scraper.extract_urls(CHROME_PAGE, PAGE)
        assert 'https://example.org/events/2025/keynote/' in urls
        for chrome in ('/login', '/news/', '/about/', '/sidebar-page/', '/privacy/'):
            assert 'https://example.org' + chrome not in urls

    def test_navigation_and_utility_links_are_dropped(self):
        urls = scraper.extract_urls(CHROME_PAGE, PAGE)
        assert 'https://example.org/' not in urls                # homepage
        assert 'https://example.org/events/' not in urls         # breadcrumb (ancestor)
        assert 'https://example.org/events/2025/' not in urls    # the page itself (via #anchor)
        assert 'https://example.org/feed/' not in urls           # feed
        assert 'https://example.org/wp-content/uploads/map.png' not in urls  # static file
        assert not any(u.startswith('mailto:') for u in urls)

    def test_documents_in_content_are_kept(self):
        urls = scraper.extract_urls(CHROME_PAGE, PAGE)
        assert 'https://docs.google.com/presentation/d/KEYNOTE/edit' in urls
        assert 'https://www.youtube.com/embed/PANELVID' in urls
        assert 'https://example.org/wp-content/uploads/handout.pdf' in urls

    def test_important_only_false_returns_every_link(self):
        urls = scraper.extract_urls(CHROME_PAGE, PAGE, important_only=False)
        assert 'https://example.org/login' in urls
        assert 'https://example.org/privacy/' in urls
        assert 'https://example.org/' in urls

    def test_urls_are_absolute_unique_and_fragment_free(self):
        html = ('<main><a href="a#one">1</a><a href="a#two">2</a><a href="./a">3</a>'
                '<a href="//cdn.example.net/x">4</a></main>')
        assert scraper.extract_urls(html, 'https://example.org/dir/page') == [
            'https://example.org/dir/a', 'https://cdn.example.net/x']

    def test_base_href_is_respected(self):
        html = '<head><base href="https://other.example/root/"></head><main><a href="x">x</a></main>'
        assert scraper.extract_urls(html, 'https://example.org/page') == [
            'https://other.example/root/x']

    def test_descendants_and_siblings_are_kept(self):
        page = 'https://example.org/cloud/class/schedule/'
        html = ('<main><a href="/cloud/class/schedule/week-1/">child</a>'
                '<a href="/cloud/class/past-events/">sibling</a>'
                '<a href="/cloud/class/">parent</a></main>')
        assert scraper.extract_urls(html, page) == [
            'https://example.org/cloud/class/schedule/week-1/',
            'https://example.org/cloud/class/past-events/']


class TestExtractAssets:
    def test_finds_documents_anywhere_with_titles_and_session_context(self):
        assets = scraper.extract_assets(BeautifulSoup(CHROME_PAGE, 'html.parser'), PAGE)
        by_url = {a.url: a for a in assets}
        keynote = by_url['https://docs.google.com/presentation/d/KEYNOTE/edit']
        assert (keynote.type, keynote.title, keynote.context) == (
            'google_drive', 'Slides', 'Opening keynote')
        panel = by_url['https://www.youtube.com/embed/PANELVID']
        assert (panel.type, panel.title, panel.context) == ('youtube', 'Panel recording', 'Panel')
        assert by_url['https://example.org/wp-content/uploads/handout.pdf'].type == 'file'
        # Documents count wherever they are linked -- even in the nav -- but channels never do.
        assert 'https://www.youtube.com/watch?v=navvideo' in by_url
        assert 'https://www.youtube.com/channel/UCxyz' not in by_url


    def test_context_from_a_schedule_table_row(self):
        html = '''<main><h3>Tuesday, 5/19</h3><table><tbody>
          <tr><td>8 – 9 a.m.</td><td>Breakfast</td><td></td></tr>
          <tr><td>9:45 – 10:15 a.m.</td>
              <td>Presentation: “<a href="/abstracts/#a1">A Journey into the Cloud</a>”<br/>John Conklin, Salk</td>
              <td><a href="https://docs.google.com/presentation/d/DECK/edit">Slides</a></td></tr>
        </tbody></table></main>'''
        [deck] = scraper.extract_assets(BeautifulSoup(html, 'html.parser'), PAGE)
        assert deck.context == ('Tuesday, 5/19 · 9:45 – 10:15 a.m. · '
                                'Presentation: “A Journey into the Cloud” John Conklin, Salk')

    def test_context_from_a_paragraph_leaves_out_document_links(self):
        html = '''<main><h4>Opening Up Research Cloud</h4>
          <p><strong>Geert Soet, SURF</strong><br/>12 pm |
             <a href="https://docs.google.com/presentation/d/DECK/edit">Slides</a> |
             <a href="https://www.youtube.com/watch?v=VID">Recording</a></p></main>'''
        assets = scraper.extract_assets(BeautifulSoup(html, 'html.parser'), PAGE)
        assert [a.context for a in assets] == ['Opening Up Research Cloud · Geert Soet, SURF 12 pm'] * 2

    def test_long_context_is_shortened(self):
        html = f'<main><p>{"word " * 100}<a href="https://youtu.be/VID">video</a></p></main>'
        [video] = scraper.extract_assets(BeautifulSoup(html, 'html.parser'), PAGE)
        assert len(video.context) == scraper.CONTEXT_MAX and video.context.endswith('…')

    def test_same_document_linked_twice_counts_once(self):
        html = '''<main>
          <h2>Talk</h2><a href="https://drive.google.com/file/d/FILE1/view?usp=sharing">Slides</a>
          <h2>Other talk</h2><a href="https://docs.google.com/presentation/d/FILE1/edit">Slides</a>
          <iframe src="https://www.youtube.com/embed/VID" title="Recording"></iframe>
          <a href="https://youtu.be/VID?t=30">Watch</a></main>'''
        assets = scraper.extract_assets(BeautifulSoup(html, 'html.parser'), PAGE)
        assert [(a.url, a.context) for a in assets] == [
            ('https://drive.google.com/file/d/FILE1/view?usp=sharing', 'Talk'),
            ('https://www.youtube.com/embed/VID', 'Other talk')]


class TestExtractText:
    def test_main_text_without_chrome_forms_or_scripts(self):
        text = scraper.extract_text(BeautifulSoup(CHROME_PAGE, 'html.parser'))
        assert 'Opening keynote' in text and 'Panel' in text
        for chrome in ('Log in', 'Nav video', 'Related', 'Our channel', 'Sign up'):
            assert chrome not in text

    def test_falls_back_to_body_without_a_main_element(self):
        html = '<body><nav>Menu</nav><div><p>Hello <b>world</b></p></div><footer>Foot</footer></body>'
        assert scraper.extract_text(BeautifulSoup(html, 'html.parser')) == 'Hello\nworld'


class TestScrapeHtml:
    def test_page_with_documents_gets_text_html_and_links(self):
        result = scraper.scrape_html(CHROME_PAGE, PAGE)
        assert result.type == scraper.PAGE
        assert result.title == 'Events 2025'
        assert 'Opening keynote' in result.text
        assert result.html.startswith('<html>')
        assert len(result.links) == 4

    def test_links_only_leaves_out_text_and_html(self):
        result = scraper.scrape_html(CHROME_PAGE, PAGE, text_with_links=False)
        assert (result.type, result.text, result.html) == (scraper.PAGE, '', '')
        assert len(result.links) == 4

    def test_page_without_documents_is_page_text(self):
        html = '<html><title>About</title><main><p>Just words.</p></main></html>'
        result = scraper.scrape_html(html, 'https://example.org/about/')
        assert (result.type, result.text, result.links) == (scraper.PAGE_TEXT, 'Just words.', [])
        assert result.html == html

    def test_bytes_are_decoded_using_the_declared_charset(self):
        html = '<html><head><meta charset="windows-1252"></head><main>It’s café</main></html>'
        result = scraper.scrape_html(html.encode('windows-1252'), 'https://example.org/')
        assert result.text == 'It’s café'
        assert 'It’s café' in result.html

    def test_direct_document_link_is_not_fetched(self, monkeypatch):
        monkeypatch.setattr(scraper, 'fetch', lambda *a, **k: 1 / 0)  # would fail if called
        result = scraper.scrape('https://youtu.be/abc')
        assert (result.type, result.text, result.links) == ('youtube', '', [])


class TestUrlHelpers:
    def test_url_key_ignores_case_www_trailing_slash_and_fragment(self):
        assert (scraper.url_key('HTTPS://WWW.Example.org/a/b/#x')
                == scraper.url_key('https://example.org/a/b')
                == 'https://example.org/a/b')
        assert scraper.url_key('https://example.org/a?x=1') != scraper.url_key('https://example.org/a?x=2')

    def test_url_key_identifies_documents_by_id(self):
        assert (scraper.url_key('https://drive.google.com/file/d/X/view?usp=sharing')
                == scraper.url_key('https://docs.google.com/presentation/d/X/edit')
                == 'google_drive:X')
        assert scraper.url_key('https://youtu.be/V?t=1') == scraper.url_key(
            'https://www.youtube.com/watch?v=V&list=L') == 'youtube:V'

    def test_domain_allowed_includes_subdomains_only(self):
        allowed = {'internet2.edu'}
        assert scraper._domain_allowed('https://www.internet2.edu/x', allowed)
        assert scraper._domain_allowed('https://spaces.at.internet2.edu/x', allowed)
        assert not scraper._domain_allowed('https://notinternet2.edu/x', allowed)
        assert not scraper._domain_allowed('https://internet2.edu.evil.example/x', allowed)


# --------------------------------------------------------------------------------------------
# Regression tests on saved snapshots of real pages (tests/fixtures/)
# --------------------------------------------------------------------------------------------

ABSTRACTS = 'https://internet2.edu/cloud-forum/2026-cloud-forum-schedule/cloud-forum-2026-abstracts/'
CICP = 'https://internet2.edu/services/cloud-infrastructure-community-program/'
ARCHIVES = 'https://internet2.edu/cloud-forum/archives/'


def test_snapshot_abstracts_page():
    result = scraper.scrape_html(fixture_html('abstracts.html'), ABSTRACTS)
    assert result.type == scraper.PAGE
    assert len(result.links) == 19
    assert {a.type for a in result.links} == {'google_drive'}
    assert result.links[0].title == 'Slides'
    assert result.links[0].context.startswith('A Journey into the Cloud for Science')
    assert 'John Conklin, Salk Institute' in result.links[0].context
    assert 'Salk Institute' in result.text
    assert len(result.text.split()) > 4000

    urls = scraper.extract_urls(fixture_html('abstracts.html'), ABSTRACTS)
    assert len(urls) == 20
    assert 'https://indiana-university.github.io/internet2-cloud-forum-path-to-cloud-coe/' in urls
    assert 'https://internet2.edu' not in urls                                   # homepage
    assert 'https://internet2.edu/cloud-forum/2026-cloud-forum-schedule/' not in urls  # breadcrumb


def test_snapshot_cicp_page_is_page_text():
    result = scraper.scrape_html(fixture_html('cicp.html'), CICP)
    assert result.type == scraper.PAGE_TEXT
    assert result.links == []
    assert len(result.text.split()) > 1000
    assert 'Cloud Infrastructure Community Program' in result.text

    urls = scraper.extract_urls(fixture_html('cicp.html'), CICP)
    assert 'https://internet2.edu/services/amazon-web-services/' in urls
    assert 'https://internet2.edu/network' not in urls  # site menu
    assert not any('facebook.com' in u or 'linkedin.com' in u for u in urls)  # footer


def test_snapshot_schedule_page():
    page = 'https://internet2.edu/cloud-forum/2026-cloud-forum-schedule/'
    result = scraper.scrape_html(fixture_html('schedule-2026.html'), page)
    # 22 "Slides" links, one of which (a copy-paste error on the page) is a second link to the
    # CMMC session's file: 21 different documents.
    assert len(result.links) == 21
    contexts = [a.context for a in result.links]
    assert contexts[0] == 'Tuesday, 5/19 · 9 – 9:45 a.m. · Opening Remarks and Survey Review'
    assert contexts[1].startswith('Tuesday, 5/19 · 9:45 – 10:15 a.m. · Presentation: “A Journey')
    assert contexts[1].endswith('John Conklin, Salk Institute for Biological Studies')
    assert all(c.split(' · ')[0] in ('Tuesday, 5/19', 'Wednesday, 5/20', 'Thursday, 5/21')
               for c in contexts)


def test_snapshot_archives_page():
    result = scraper.scrape_html(fixture_html('archives.html'), ARCHIVES)
    assert len(result.links) == 14
    folders = [a for a in result.links if '/folders/' in a.url]
    assert len(folders) == 6

    urls = scraper.extract_urls(fixture_html('archives.html'), ARCHIVES)
    assert 'https://internet2.edu/cloud-forum/2025-cloud-forum-schedule/' in urls
    assert 'https://internet2.edu/cloud-forum/2019-schedule/' in urls
    assert 'https://internet2.edu/cloud-forum/' not in urls  # breadcrumb
