import pytest

from i2cc_scraper.url_classifier import (FILE, GOOGLE_DRIVE, PANOPTO, YOUTUBE, asset_id, classify_url,
                            file_extension, get_domain, is_youtube_host)

PANOPTO_ID = '19e7ee4f-62b6-4856-a056-b4ac00dbb57e'


@pytest.mark.parametrize('url, expected', [
    # YouTube: single videos only
    ('https://www.youtube.com/watch?v=rEDqGLGK5qE', YOUTUBE),
    ('https://m.youtube.com/watch?v=abc', YOUTUBE),
    ('https://youtu.be/rEDqGLGK5qE', YOUTUBE),
    ('https://www.youtube.com/embed/abc', YOUTUBE),
    ('https://www.youtube-nocookie.com/embed/abc', YOUTUBE),
    ('https://www.youtube.com/shorts/abc', YOUTUBE),
    ('https://www.youtube.com/live/abc', YOUTUBE),
    ('https://music.youtube.com/watch?v=abc', YOUTUBE),       # any YouTube subdomain
    ('https://www.youtube.com/v/abc', YOUTUBE),               # the old embed form
    ('https://www.youtube.com/watch/?v=abc', YOUTUBE),        # trailing slash
    ('https://www.youtube.com/watch?v=abc&list=PL123&index=4', YOUTUBE),  # video inside a playlist
    ('https://www.youtube.com/@internet2', None),
    ('https://www.youtube.com/results?search_query=internet2', None),
    ('https://www.youtube.com/', None),
    ('https://www.youtube.com/channel/UC6gxuEGpxW07lUFD0IEpRQA', None),
    ('https://www.youtube.com/playlist?list=PL123', None),
    ('https://www.youtube.com/watch', None),
    # Panopto: session viewer/embed links with an id
    (f'https://internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id={PANOPTO_ID}', PANOPTO),
    (f'https://internet2.hosted.panopto.com/Panopto/Pages/Embed.aspx?id={PANOPTO_ID}', PANOPTO),
    ('https://internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx', None),
    ('https://internet2.hosted.panopto.com/Panopto/Pages/Sessions/List.aspx', None),
    # Google Drive / Docs: files, Slides/Docs/Sheets, folders
    ('https://drive.google.com/file/d/19kfYW74KSwGHIAGmlL6TYvhFNsyUYlY8/view?usp=drive_link',
     GOOGLE_DRIVE),
    ('https://docs.google.com/presentation/d/1GYV/edit?usp=drive_link', GOOGLE_DRIVE),
    ('https://docs.google.com/document/d/1abc/edit', GOOGLE_DRIVE),
    ('https://docs.google.com/spreadsheets/d/1abc/edit', GOOGLE_DRIVE),
    ('https://drive.google.com/drive/folders/1KpLl', GOOGLE_DRIVE),
    ('https://drive.google.com/drive/u/0/folders/1KpLl?usp=sharing', GOOGLE_DRIVE),
    ('https://drive.google.com/open?id=abc', GOOGLE_DRIVE),
    ('https://drive.google.com/uc?id=abc&export=download', GOOGLE_DRIVE),
    ('https://drive.google.com/folderview?id=abc', GOOGLE_DRIVE),
    ('https://docs.google.com/forms/d/abc/viewform', None),
    ('https://drive.google.com/drive/my-drive', None),
    # Files on other websites, by extension
    ('http://internet2.edu/wp-content/uploads/2022/09/Knight-Center-Floor-Plans.pdf', FILE),
    ('https://example.org/talks/Keynote.PPTX?download=1', FILE),
    ('https://example.org/recording.mp4', FILE),
    ('https://example.org/slides.pdf#page=3', FILE),
    ('https://example.org/logo.png', None),
    ('https://example.org/pdf/', None),
    ('https://example.org/page.html', None),
    # Ordinary pages
    ('https://internet2.edu/cloud-forum/archives/', None),
    ('https://www.google.com/', None),
    ('', None),
])
def test_classify_url(url, expected):
    assert classify_url(url) == expected


@pytest.mark.parametrize('url, expected', [
    ('youtu.be/abc', YOUTUBE),
    ('www.youtube.com/watch?v=abc', YOUTUBE),
    ('HTTPS://WWW.YouTube.com/watch?v=abc', YOUTUBE),
    (f'internet2.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id={PANOPTO_ID}', PANOPTO),
])
def test_classify_url_without_scheme_or_in_other_case(url, expected):
    assert classify_url(url) == expected


@pytest.mark.parametrize('url, domain', [
    ('https://www.youtube.com/watch?v=x', 'youtube.com'),
    ('https://user:pw@drive.google.com/file/d/x', 'drive.google.com'),
    ('https://internet2.hosted.panopto.com:443/Panopto/', 'internet2.hosted.panopto.com'),
    ('HTTPS://WWW.Internet2.EDU/', 'internet2.edu'),
    ('docs.google.com#frag', 'docs.google.com'),
    ('youtu.be/abc', 'youtu.be'),
    # An "@" later in the URL must not be mistaken for a "user@" prefix.
    ('https://www.youtube.com/watch?v=x&email=a@b.com', 'youtube.com'),
    ('https://example.org/path/a@b.com', 'example.org'),
    ('', ''),
])
def test_get_domain(url, domain):
    assert get_domain(url) == domain


def test_link_to_another_site_is_judged_by_its_own_domain():
    # A tracking/redirect link that merely mentions a YouTube URL isn't a YouTube link.
    assert classify_url('https://evil.example/?u=https://www.youtube.com/watch?v=x') is None


@pytest.mark.parametrize('url, ext', [
    ('https://x.org/a/Slides.PDF?dl=1', 'pdf'),
    ('https://x.org/a/archive.tar.gz', 'gz'),
    ('https://x.org/a/', ''),
    ('https://x.org/', ''),
])
def test_file_extension(url, ext):
    assert file_extension(url) == ext


@pytest.mark.parametrize('url, identity', [
    ('https://www.youtube.com/watch?v=abc&list=PL1', 'youtube:abc'),
    ('https://youtu.be/abc?t=30', 'youtube:abc'),
    ('https://www.youtube.com/embed/abc?autoplay=1', 'youtube:abc'),
    ('https://www.youtube.com/shorts/abc', 'youtube:abc'),
    (f'https://internet2.hosted.panopto.com/Panopto/Pages/Embed.aspx?id={PANOPTO_ID.upper()}',
     f'panopto:{PANOPTO_ID}'),
    ('https://docs.google.com/presentation/d/X1/edit?usp=drive_link', 'google_drive:X1'),
    ('https://drive.google.com/file/d/X1/view', 'google_drive:X1'),
    ('https://drive.google.com/open?id=X1', 'google_drive:X1'),
    ('https://drive.google.com/drive/u/0/folders/F1?usp=sharing', 'google_drive:F1'),
    ('https://example.org/slides.pdf', None),
    ('https://example.org/page', None),
])
def test_asset_id(url, identity):
    assert asset_id(url) == identity


@pytest.mark.parametrize('url, expected', [
    ('https://www.youtube.com/watch?v=abc', True), ('https://youtu.be/abc', True),
    ('https://music.youtube.com/', True), ('https://m.youtube.com/@x', True),
    ('https://www.youtube-nocookie.com/embed/abc', True), ('youtube.com/playlist?list=PL1', True),
    ('https://www.youtube.com.evil.example/watch?v=abc', False),
    ('https://notyoutube.com/', False), ('https://internet2.edu/youtube.com/', False),
])
def test_is_youtube_host(url, expected):
    assert is_youtube_host(url) is expected


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=abc', 'https://youtu.be/abc?t=30', 'https://music.youtube.com/watch?v=abc',
    'https://www.youtube.com/v/abc', 'https://www.youtube.com/watch/?v=abc&list=PL1&index=2',
    'https://www.youtube.com/embed/abc', 'https://www.youtube.com/shorts/abc',
])
def test_every_form_of_one_video_has_the_same_identity(url):
    assert asset_id(url) == 'youtube:abc'
