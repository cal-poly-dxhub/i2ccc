"""Cleaning YouTube's rolling automatic captions: each spoken line once, nothing else lost."""
from conftest import FIXTURES
from i2cc_scraper.captions import clean_vtt, clean_vtt_file, srt_to_vtt

# The first 40 lines of the real captions of an Internet2 video (automatic, English).
ROLLING = (FIXTURES / 'youtube-rolling.vtt').read_text()
HEADER = 'WEBVTT\nKind: captions\nLanguage: en'


def cues(vtt: str) -> list[tuple[str, str]]:
    """(timing, text) of each cue in a cleaned file."""
    out = []
    for block in vtt.strip().split('\n\n')[1:]:
        timing, *text = block.split('\n')
        out.append((timing, '\n'.join(text)))
    return out


def test_a_real_automatic_caption_file_says_each_line_once():
    assert cues(clean_vtt(ROLLING)) == [
        ('00:00:08.480 --> 00:00:10.589', "net plus first and foremost isn't a"),
        ('00:00:10.599 --> 00:00:12.549', "technical solution it's really a"),
        ('00:00:12.559 --> 00:00:14.470', "portfolio of solutions that's going to"),
        ('00:00:14.480 --> 00:00:15.829', 'enable the community to work in a new'),
        ('00:00:15.839 --> 00:00:18.029', 'way we have Innovation happening'),
    ]


def test_the_result_is_valid_webvtt_with_no_markup_left():
    cleaned = clean_vtt(ROLLING)
    assert cleaned.startswith(HEADER + '\n\n') and cleaned.endswith('\n')
    assert '<' not in cleaned and 'align:' not in cleaned          # no word tags, no cue settings
    words = cleaned.split()
    assert len([w for w in words if w == 'new']) == 1               # every line exactly once


def test_cleaning_twice_changes_nothing_more():
    once = clean_vtt(ROLLING)
    assert clean_vtt(once) == once


def test_uploaded_captions_are_left_exactly_as_they_are():
    """No word-timing tags = not the rolling layout. Such files may repeat a line on purpose."""
    manual = ('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNo.\n\n'
              '00:00:02.000 --> 00:00:03.000\nNo.\n\n'
              '00:00:03.000 --> 00:00:04.000\n<i>Thank you</i> &gt;&gt; Next\nNo.\n')
    assert clean_vtt(manual) == manual


def test_a_new_one_word_line_without_tags_is_kept():
    rolling = (f'{HEADER}\n\n'
               '00:00:01.000 --> 00:00:02.000\n \nhello<00:00:01.500><c> there</c>\n\n'
               '00:00:02.000 --> 00:00:02.010\nhello there\n \n\n'
               '00:00:02.010 --> 00:00:03.000\nhello there\nyes\n\n'          # "yes": new, no tags
               '00:00:03.000 --> 00:00:03.010\nyes\n \n\n'
               '00:00:03.010 --> 00:00:04.000\nyes\nnow<00:00:03.500><c> then</c>\n')
    assert [text for _, text in cues(clean_vtt(rolling))] == ['hello there', 'yes', 'now then']


def test_a_line_spoken_twice_in_a_row_is_kept_twice():
    rolling = (f'{HEADER}\n\n'
               '00:00:01.000 --> 00:00:02.000\n \nno<00:00:01.200><c> no</c>\n\n'
               '00:00:02.000 --> 00:00:02.010\nno no\n \n\n'
               '00:00:02.010 --> 00:00:03.000\nno no\nno<00:00:02.500><c> no</c>\n')
    assert [text for _, text in cues(clean_vtt(rolling))] == ['no no', 'no no']


def test_escaped_characters_and_other_markup_survive():
    rolling = (f'{HEADER}\n\n'
               '00:00:01.000 --> 00:00:02.000\n \n&gt;&gt;<00:00:01.100><c> Q&amp;A</c>'
               '<00:00:01.500><c> <i>time</i></c>\n')
    assert cues(clean_vtt(rolling))[0][1] == '&gt;&gt; Q&amp;A <i>time</i>'


def test_windows_line_endings_are_handled():
    assert cues(clean_vtt(ROLLING.replace('\n', '\r\n'))) == cues(clean_vtt(ROLLING))


def test_a_file_with_only_a_header_stays_valid():
    assert clean_vtt(HEADER + '\n\n00:00:01.000 --> 00:00:01.010\n<00:00:01.000>\n') == HEADER + '\n'


def test_clean_vtt_file_rewrites_in_place_and_ignores_unreadable_files(tmp_path):
    path = tmp_path / 'video.en.vtt'
    path.write_text(ROLLING, encoding='utf-8')
    clean_vtt_file(path)
    assert path.read_text(encoding='utf-8') == clean_vtt(ROLLING)

    binary = tmp_path / 'odd.vtt'
    binary.write_bytes(b'\xff\xfe\x00 not utf-8')
    clean_vtt_file(binary)
    assert binary.read_bytes() == b'\xff\xfe\x00 not utf-8'


def test_panopto_srt_becomes_webvtt():
    srt = '\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\nWelcome, everyone\r\n\r\n2\r\n00:01:02,040 --> 00:01:04,000\r\nto 1,000 attendees\r\n'
    assert srt_to_vtt(srt) == ('WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nWelcome, everyone\n\n'
                               '2\n00:01:02.040 --> 00:01:04.000\nto 1,000 attendees\n')
