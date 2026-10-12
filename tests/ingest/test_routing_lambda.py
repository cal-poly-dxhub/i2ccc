import os
from types import SimpleNamespace

import boto3
import pytest

import processing_config as pc
import routing_lambda
from tests.conftest import BUCKET


@pytest.fixture
def table(aws, monkeypatch):
    dynamodb = boto3.resource("dynamodb")
    t = dynamodb.create_table(
        TableName=os.environ["PROCESSED_FILES_TABLE"],
        KeySchema=[{"AttributeName": "s3_uri", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "s3_uri", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # The module binds its table at import, before moto was active
    monkeypatch.setattr(routing_lambda, "processed_files_table", t)
    return t


def _route(**event):
    return routing_lambda.lambda_handler(
        {"Bucket": BUCKET, **event}, SimpleNamespace(aws_request_id="req-1")
    )


def test_routes_every_supported_extension_to_its_processor(s3, table):
    for ext in pc.SUPPORTED_EXTENSIONS:
        s3.put_object(Bucket=BUCKET, Key=f"file.{ext}", Body=b"x")

    routed = {r["key"]: r for r in _route()}

    assert set(routed) == {f"file.{ext}" for ext in pc.SUPPORTED_EXTENSIONS}
    for ext in pc.SUPPORTED_EXTENSIONS:
        item = routed[f"file.{ext}"]
        assert item["lambda_name"] == pc.EXTENSION_TO_PROCESSOR[ext]
        assert item["data_type"] == ext
        assert item["s3_uri"] == f"s3://{BUCKET}/file.{ext}"


def test_extension_match_is_case_insensitive(s3, table):
    s3.put_object(Bucket=BUCKET, Key="Talk.MP4", Body=b"x")
    [item] = _route()
    assert item["lambda_name"] == pc.PROCESS_VIDEO
    assert item["data_type"] == "mp4"


def test_skips_unsupported_quarantined_and_processed(s3, table):
    for key in ["image.png", "doc.docx", "quarantine/bad.mp4", "done.pdf", "new.pdf"]:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    table.put_item(Item={"s3_uri": f"s3://{BUCKET}/done.pdf"})

    assert [r["key"] for r in _route()] == ["new.pdf"]
