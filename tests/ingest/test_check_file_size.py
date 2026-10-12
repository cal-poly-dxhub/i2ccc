import boto3
import pytest

import check_file_size
import processing_config as pc
from tests.conftest import BUCKET


@pytest.fixture
def client(s3, monkeypatch):
    monkeypatch.setattr(check_file_size, "s3_client", boto3.client("s3"))
    return s3


def test_uses_shared_threshold():
    assert check_file_size.VIDEO_MAX_SIZE_BYTES == pc.VIDEO_MAX_SIZE_BYTES


@pytest.mark.parametrize(
    "size, needs_chunking",
    [(99, False), (100, False), (101, True)],
)
def test_needs_chunking_only_above_threshold(client, monkeypatch, size, needs_chunking):
    # Shrink the threshold so the boundary can be tested with tiny objects
    monkeypatch.setattr(check_file_size, "VIDEO_MAX_SIZE_BYTES", 100)
    client.put_object(Bucket=BUCKET, Key="v.mp4", Body=b"x" * size)

    event = {"s3_uri": f"s3://{BUCKET}/v.mp4", "lambda_name": pc.PROCESS_VIDEO, "data_type": "mp4"}
    result = check_file_size.lambda_handler(event, None)

    assert result["needs_chunking"] is needs_chunking
    assert result["file_size_bytes"] == size
    assert result["size_threshold_gb"] == pc.VIDEO_MAX_SIZE_GB
    # Original event fields flow through to the next state
    assert result["lambda_name"] == pc.PROCESS_VIDEO
    assert result["data_type"] == "mp4"
