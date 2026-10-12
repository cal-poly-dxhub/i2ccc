import subprocess

import boto3
import pytest

import chunk_video
import processing_config as pc
from tests.conftest import BUCKET

GB = 1024**3


def test_small_video_uses_default_chunk_duration():
    chunks = chunk_video.calculate_chunks(duration_seconds=3000, file_size_bytes=GB)
    assert [c["start_time"] for c in chunks] == [0, 1200, 2400]
    assert [c["duration"] for c in chunks] == [1200, 1200, 600]
    assert [c["chunk_num"] for c in chunks] == [1, 2, 3]


def test_large_video_chunks_fit_size_limit():
    size = 5 * GB
    duration = 7200
    chunks = chunk_video.calculate_chunks(duration, size)

    assert sum(c["duration"] for c in chunks) == pytest.approx(duration)
    bytes_per_second = size / duration
    for c in chunks:
        assert c["duration"] * bytes_per_second <= pc.VIDEO_MAX_SIZE_BYTES


def test_ffmpeg_seeks_before_input(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    chunk_video.create_video_chunk("in.mp4", "out.mp4", 1200, 600)

    [cmd] = calls
    # Input seeking (-ss before -i) jumps to the start point instead of
    # reading the file from the beginning for every chunk
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "1200"
    assert cmd[cmd.index("-t") + 1] == "600"
    assert cmd[cmd.index("-c") + 1] == "copy"


@pytest.fixture
def chunker(s3, monkeypatch):
    # The handler works in /tmp and removes its files when done
    monkeypatch.setattr(chunk_video, "s3_client", boto3.client("s3"))
    monkeypatch.setattr(chunk_video, "get_video_duration", lambda path: 3000.0)

    def fake_chunk(input_file, output_file, start_time, duration):
        with open(output_file, "wb") as f:
            f.write(b"chunk")

    monkeypatch.setattr(chunk_video, "create_video_chunk", fake_chunk)
    return s3


def _run(s3, key, metadata, size=GB):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"video", Metadata=metadata)
    event = {
        "s3_uri": f"s3://{BUCKET}/{key}",
        "file_size_bytes": size,
        "lambda_name": pc.PROCESS_VIDEO,
        "data_type": "mp4",
        "timestamp": "req-1",
    }
    return chunk_video.lambda_handler(event, None)


def test_chunks_inherit_original_metadata(chunker):
    metadata = {
        "member-content": "true",
        "source-url": "https://drive.google.com/file/d/abc",
        "parent-folder-name": "Webinars",
        "parent-folder-url": "https://drive.google.com/drive/folders/xyz",
    }
    result = _run(chunker, "folder/talk.mp4", metadata)

    assert result["total_chunks"] == 3
    for chunk, expected_start in zip(result["chunks"], [0, 1200, 2400]):
        assert chunk["key"] == pc.video_chunk_key("folder/talk.mp4", chunk["chunk_num"])
        head = chunker.head_object(Bucket=BUCKET, Key=chunk["key"])
        assert head["Metadata"] == {**metadata, "relative-start-time": str(expected_start)}


def test_relative_start_time_adds_to_existing_offset(chunker):
    result = _run(chunker, "part2.mp4", {"member-content": "false", "relative-start-time": "5400"})
    starts = [
        chunker.head_object(Bucket=BUCKET, Key=c["key"])["Metadata"]["relative-start-time"]
        for c in result["chunks"]
    ]
    assert starts == ["5400", "6600", "7800"]


def test_original_deleted_and_chunks_routed_as_video(chunker):
    result = _run(chunker, "talk.webm", {"member-content": "false"})

    keys = [o["Key"] for o in chunker.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert "talk.webm" not in keys
    assert sorted(keys) == [c["key"] for c in result["chunks"]]
    assert keys[0] == pc.video_chunk_key("talk.webm", 1)  # keeps its extension
    assert all(c["lambda_name"] == pc.PROCESS_VIDEO for c in result["chunks"])
