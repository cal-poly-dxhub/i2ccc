import ast
import pathlib

import pytest

import processing_config as pc

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# MediaFormat values Amazon Transcribe accepts - audio/video extensions are
# passed through as-is by the state machine
TRANSCRIBE_MEDIA_FORMATS = {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}


def test_processors_match_state_machine_branches():
    # cdk/ingest.py branches on video/audio/pdf and sends everything else to
    # text; a new processor needs a new branch there
    assert set(pc.PROCESSORS) == {
        pc.PROCESS_VIDEO,
        pc.PROCESS_AUDIO,
        pc.PROCESS_PDF,
        pc.PROCESS_TEXT,
    }


def test_each_extension_has_one_processor():
    all_exts = [ext for exts in pc.PROCESSORS.values() for ext in exts]
    assert len(all_exts) == len(set(all_exts))
    assert set(all_exts) == pc.SUPPORTED_EXTENSIONS == set(pc.EXTENSION_TO_PROCESSOR)


def test_extensions_are_lowercase_without_dot():
    for ext in pc.SUPPORTED_EXTENSIONS:
        assert ext == ext.lower() and not ext.startswith(".")


@pytest.mark.parametrize("processor", [pc.PROCESS_VIDEO, pc.PROCESS_AUDIO])
def test_transcribed_extensions_are_transcribe_formats(processor):
    assert set(pc.PROCESSORS[processor]) <= TRANSCRIBE_MEDIA_FORMATS


def test_current_extension_mapping():
    # Pin the mapping so changes are deliberate
    assert pc.EXTENSION_TO_PROCESSOR == {
        "mp4": "process-video",
        "webm": "process-video",
        "pdf": "process-pdf",
        "mp3": "process-audio",
        "wav": "process-audio",
        "flac": "process-audio",
        "m4a": "process-audio",
        "txt": "process-text",
        "vtt": "process-text",
    }


def test_video_size_limits():
    assert pc.VIDEO_MAX_SIZE_GB == 1.8
    assert pc.VIDEO_MAX_SIZE_BYTES == int(1.8 * 1024**3)
    assert pc.VIDEO_MAX_SIZE_GB < 2  # buffer below the 2 GB processing limit
    assert pc.VIDEO_CHUNK_DURATION_SECONDS == 20 * 60


@pytest.mark.parametrize(
    "key, expected",
    [
        ("talk.mp4", "mp4"),
        ("folder/Talk.MP4", "mp4"),
        ("archive.tar.gz", "gz"),
        ("no_extension", ""),
    ],
)
def test_extension_of(key, expected):
    assert pc.extension_of(key) == expected


@pytest.mark.parametrize(
    "key, num, expected",
    [
        ("talk.mp4", 1, "talk_chunk_001.mp4"),
        ("folder/talk.webm", 12, "folder/talk_chunk_012.webm"),
    ],
)
def test_video_chunk_key(key, num, expected):
    assert pc.video_chunk_key(key, num) == expected


@pytest.mark.parametrize(
    "key, expected",
    [
        ("talk.mp4", ["talk.mp4", "talk_chunk_001.mp4"]),
        ("talk.webm", ["talk.webm", "talk_chunk_001.webm"]),
        ("slides.pdf", ["slides.pdf"]),
        ("audio.mp3", ["audio.mp3"]),
    ],
)
def test_pipeline_output_keys(key, expected):
    assert pc.pipeline_output_keys(key) == expected


def _string_constants(path):
    tree = ast.parse(path.read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/ingest/routing/routing_lambda.py",
        "src/ingest/video_chunker/chunk_video.py",
        "cdk/ingest.py",
        "ingest_utils/confluence_processor/google_drive_processor.py",
        "ingest_utils/confluence_processor/confluence_processor.py",
    ],
)
def test_no_hardcoded_processor_names(path):
    # Consumers must use the constants, not re-type the names
    hardcoded = _string_constants(REPO_ROOT / path) & set(pc.PROCESSORS)
    assert not hardcoded, f"{path} hardcodes {hardcoded}"
