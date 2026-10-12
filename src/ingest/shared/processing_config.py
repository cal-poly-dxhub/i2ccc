"""
Single source of truth for which files the ingest pipeline processes and how
large videos are chunked.

Bundled as a top-level module into every consumer, so import it as
``import processing_config``:
- routing / check_file_size Lambdas: copied into the zip asset by
  cdk/shared_code.py
- chunk_video Lambda image: COPY --from=shared in its Dockerfile
- collector (ingest_utils/confluence_processor): via pipeline_config.py
- CDK (cdk/ingest.py): state machine Choice branches
"""

import os

# Processor names - the router emits these as `lambda_name` and the state
# machine Choice in cdk/ingest.py branches on them. Adding a processor here
# also needs a branch there (tests/ingest/test_processing_config.py checks).
PROCESS_VIDEO = "process-video"
PROCESS_AUDIO = "process-audio"
PROCESS_PDF = "process-pdf"
PROCESS_TEXT = "process-text"

# Video/audio extensions are passed straight to Transcribe as MediaFormat, so
# they must be formats Transcribe accepts
PROCESSORS = {
    PROCESS_VIDEO: ("mp4", "webm"),
    PROCESS_AUDIO: ("mp3", "wav", "flac", "m4a"),
    PROCESS_PDF: ("pdf",),
    PROCESS_TEXT: ("txt", "vtt"),
}

EXTENSION_TO_PROCESSOR = {
    ext: processor for processor, exts in PROCESSORS.items() for ext in exts
}
SUPPORTED_EXTENSIONS = frozenset(EXTENSION_TO_PROCESSOR)

# Videos larger than this are split by the ChunkLargeVideo state, and each
# chunk targets this size (buffer below the 2 GB processing limit)
VIDEO_MAX_SIZE_GB = 1.8
VIDEO_MAX_SIZE_BYTES = int(VIDEO_MAX_SIZE_GB * 1024**3)
# Chunk length when size alone doesn't force shorter chunks
VIDEO_CHUNK_DURATION_SECONDS = 20 * 60

__all__ = [
    "PROCESS_VIDEO",
    "PROCESS_AUDIO",
    "PROCESS_PDF",
    "PROCESS_TEXT",
    "PROCESSORS",
    "EXTENSION_TO_PROCESSOR",
    "SUPPORTED_EXTENSIONS",
    "VIDEO_MAX_SIZE_GB",
    "VIDEO_MAX_SIZE_BYTES",
    "VIDEO_CHUNK_DURATION_SECONDS",
    "extension_of",
    "video_chunk_key",
    "pipeline_output_keys",
]


def extension_of(key: str) -> str:
    """Lower-case extension without the dot ('' if none)."""
    return os.path.splitext(key)[1].lstrip(".").lower()


def video_chunk_key(key: str, chunk_num: int) -> str:
    """S3 key the chunker writes chunk `chunk_num` (1-based) of `key` to."""
    base, ext = os.path.splitext(key)
    return f"{base}_chunk_{chunk_num:03d}{ext}"


def pipeline_output_keys(key: str) -> list:
    """
    Keys under which an uploaded object can exist after the pipeline has seen
    it. Large videos are deleted after chunking, so their first chunk is the
    only trace left - collectors check these to avoid re-uploading.
    """
    if EXTENSION_TO_PROCESSOR.get(extension_of(key)) == PROCESS_VIDEO:
        return [key, video_chunk_key(key, 1)]
    return [key]
