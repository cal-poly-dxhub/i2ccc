from unittest.mock import MagicMock

import pytest

import google_drive_processor as gdp
import processing_config as pc
from s3_uploader import S3Uploader
from tests.conftest import BUCKET

SOURCE_URL = "https://drive.google.com/file/d/abc123/view?usp=drive_link"


@pytest.fixture
def drive_service(monkeypatch):
    service = MagicMock()
    service.permissions().list().execute.return_value = {"permissions": []}
    monkeypatch.setattr(gdp.service_account.Credentials, "from_service_account_info", MagicMock())
    monkeypatch.setattr(gdp, "build", lambda *a, **kw: service)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return service


@pytest.fixture
def make_processor(s3, drive_service, tmp_path):
    def make(skip_existing=False):
        uploader = S3Uploader(bucket_name=BUCKET, region_name="us-west-2")
        return gdp.GoogleDriveProcessor("{}", uploader, str(tmp_path), skip_existing)

    return make


def _drive_file(name, mime_type, file_id="abc123"):
    return {"id": file_id, "name": name, "mimeType": mime_type}


def _keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_has_no_local_video_chunking():
    for attr in ["process_video_file", "calculate_chunks", "should_split_video", "_create_video_chunk"]:
        assert not hasattr(gdp.GoogleDriveProcessor, attr)
    for const in ["SIZE_THRESHOLD_GB", "MAX_CHUNK_SIZE_GB", "DURATION_THRESHOLD_HOURS", "PIPELINE_SUPPORTED_EXTENSIONS"]:
        assert not hasattr(gdp, const)


def test_video_uploads_whole_with_metadata(make_processor, drive_service, s3):
    drive_service.files().get_media().execute.return_value = b"big video bytes"
    processor = make_processor()

    processor._download_and_upload_file(
        _drive_file("Webinar.mp4", "video/mp4"),
        is_subscriber_content=True,
        source_url=SOURCE_URL,
        parent_folder_name="Webinars",
        parent_folder_url="https://drive.google.com/drive/folders/xyz",
    )

    # One object, original name - the state machine does any chunking
    assert _keys(s3) == ["Webinar.mp4"]
    obj = s3.get_object(Bucket=BUCKET, Key="Webinar.mp4")
    assert obj["Body"].read() == b"big video bytes"
    assert obj["Metadata"] == {
        "member-content": "true",
        "source-url": "https://drive.google.com/file/d/abc123",
        "parent-folder-name": "Webinars",
        "parent-folder-url": "https://drive.google.com/drive/folders/xyz",
    }
    assert processor.stats["uploaded"] == 1
    assert processor.stats["uploaded_files"] == ["Webinar.mp4"]


@pytest.mark.parametrize("name, mime_type", [("photo.png", "image/png"), ("bundle.zip", "application/zip")])
def test_unsupported_types_skipped_before_download(make_processor, drive_service, s3, name, mime_type):
    processor = make_processor()
    drive_service.files().get_media.reset_mock()

    assert processor._download_and_upload_file(_drive_file(name, mime_type), False) is None

    drive_service.files().get_media.assert_not_called()
    assert _keys(s3) == []
    assert processor.stats["unsupported_skipped"] == 1


@pytest.mark.parametrize(
    "name, mime_type, expected",
    [
        ("talk.mp4", "video/mp4", ["talk.mp4", "talk_chunk_001.mp4"]),
        ("notes.txt", "text/plain", ["notes.txt"]),
        ("Agenda", "application/vnd.google-apps.document", ["Agenda.pdf", "Agenda.docx"]),
        ("Deck", "application/vnd.google-apps.presentation", ["Deck.pdf", "Deck.pptx"]),
        ("Sheet", "application/vnd.google-apps.spreadsheet", ["Sheet.xlsx"]),
    ],
)
def test_predicted_s3_keys(make_processor, name, mime_type, expected):
    assert make_processor()._predicted_s3_keys(name, mime_type) == expected


def test_predicted_chunk_key_matches_pipeline_naming(make_processor):
    # webm used to be predicted as _chunk_001.mp4
    keys = make_processor()._predicted_s3_keys("talk.webm", "video/webm")
    assert keys == ["talk.webm", pc.video_chunk_key("talk.webm", 1)]


def test_skip_existing_recognises_pipeline_chunked_video(make_processor, drive_service, s3):
    # The chunker deleted the original, leaving only its chunks
    s3.put_object(Bucket=BUCKET, Key=pc.video_chunk_key("Webinar.mp4", 1), Body=b"c")
    processor = make_processor(skip_existing=True)
    drive_service.files().get_media.reset_mock()

    assert processor._download_and_upload_file(_drive_file("Webinar.mp4", "video/mp4"), False) is None

    drive_service.files().get_media.assert_not_called()
    assert processor.stats["already_in_s3"] == 1


def test_public_file_is_not_member_content(make_processor, drive_service, s3):
    drive_service.files().get_media().execute.return_value = b"text"
    drive_service.permissions().list().execute.return_value = {
        "permissions": [{"type": "anyone", "role": "reader"}]
    }

    make_processor()._download_and_upload_file(_drive_file("notes.txt", "text/plain"), True)

    assert s3.head_object(Bucket=BUCKET, Key="notes.txt")["Metadata"]["member-content"] == "false"
