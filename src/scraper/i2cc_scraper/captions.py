"""Cleaning caption files.

YouTube's *automatic* captions come as WebVTT in a "rolling" layout meant for on-screen display:

    00:00:08.480 --> 00:00:10.589 align:start position:0%

    net<00:00:08.840><c> plus</c><00:00:09.519><c> first</c><00:00:09.719><c> and</c>...

    00:00:10.589 --> 00:00:10.599 align:start position:0%
    net plus first and foremost isn't a


    00:00:10.599 --> 00:00:12.549 align:start position:0%
    net plus first and foremost isn't a
    technical<00:00:11.000><c> solution</c>...

Every spoken line appears three times -- with word-timing tags in the cue where it's new, alone in
a 10 ms filler cue, and again at the top of the next cue -- so a transcript read straight from the
file says everything three times. clean_vtt() keeps each line once, with the cue it was spoken in.

Panopto's captions come as SRT; srt_to_vtt() turns them into WebVTT, so every stored caption file
has one format (and one extension, which ingestion routes to its text pipeline).
"""
import re
from pathlib import Path

# Inline word timing (<00:00:08.840>) and the <c> wrappers YouTube puts around words. Other tags
# (<i>, <b>, <v Speaker>) are real WebVTT markup and are left alone.
_INLINE_TIME = re.compile(r'<(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}>')
_ROLLING_TAG = re.compile(r'<(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}>|</?c(?:\.[\w.-]+)?>')
_TIMING = re.compile(r'^\s*(\S+)\s+-->\s+(\S+)')


def clean_vtt(text: str) -> str:
    """text with YouTube's rolling automatic captions rewritten so each line appears once.

    A cue's *new* line is the one with inline timing tags, or any line that wasn't already shown
    in the previous cue; carried-over copies and the empty filler cues disappear, and the
    word-timing markup and cue settings (align/position) are dropped. The header (WEBVTT, Kind,
    Language) is kept, and the result is valid WebVTT with the cues otherwise as they were.

    A file without inline timing tags -- uploaded captions, or already cleaned -- is returned
    unchanged: those can legitimately repeat a line, and there is nothing rolling to undo.
    """
    if not _INLINE_TIME.search(text):
        return text
    blocks = re.split(r'\n{2,}', text.replace('\r\n', '\n').replace('\r', '\n').strip('\n'))
    header, cues = blocks[0], []
    for block in blocks[1:]:
        lines = block.split('\n')
        index = next((i for i, line in enumerate(lines) if '-->' in line), None)
        timing = _TIMING.match(lines[index]) if index is not None else None
        if timing:  # NOTE/STYLE blocks and anything without a timing line are display-only
            cues.append((timing.group(1), timing.group(2), lines[index + 1:]))

    kept_cues: list[tuple[str, str, list[str]]] = []
    previous: list[str] = []  # the lines the previous cue showed, tags removed
    for start, end, raw_lines in cues:
        shown, fresh = [], []
        for raw in raw_lines:
            line = ' '.join(_ROLLING_TAG.sub('', raw).split())
            if not line:
                continue
            shown.append(line)
            if _INLINE_TIME.search(raw) or line not in previous:
                fresh.append(line)
        previous = shown
        if fresh:
            kept_cues.append((start, end, fresh))

    out = [header]
    out += [f'{start} --> {end}\n' + '\n'.join(lines) for start, end, lines in kept_cues]
    return '\n\n'.join(out) + '\n'


def clean_vtt_file(path: Path) -> None:
    """clean_vtt() applied to a file, in place. A file that can't be read as UTF-8 text is left
    as it is."""
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return
    cleaned = clean_vtt(text)
    if cleaned != text:
        path.write_text(cleaned, encoding='utf-8')


_SRT_TIME = re.compile(r'(\d{1,2}:\d{2}:\d{2}),(\d{3})')


def srt_to_vtt(text: str) -> str:
    """SRT captions as WebVTT: a header, and "00:00:01,000" times written "00:00:01.000"."""
    lines = text.lstrip('\ufeff').replace('\r\n', '\n').split('\n')
    out = [_SRT_TIME.sub(r'\1.\2', line) if '-->' in line else line for line in lines]
    return 'WEBVTT\n\n' + '\n'.join(out).strip('\n') + '\n'
