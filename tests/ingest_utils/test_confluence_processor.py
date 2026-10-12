import pytest
import yaml

import confluence_processor as cp
import processing_config as pc
from tests.conftest import BUCKET

ASSETS = [
    {"url": "https://example.org/files/talk.mp4", "file_type": "mp4", "is_subscriber_content": True},
    {"url": "https://example.org/files/slides.pdf", "file_type": "pdf", "is_subscriber_content": False},
    {"url": "https://example.org/files/photo.png", "file_type": "png", "is_subscriber_content": False},
    {"url": "https://example.org/files/report.docx", "file_type": "docx", "is_subscriber_content": False},
    {"url": "https://drive.google.com/drive/folders/xyz", "file_type": "folder", "is_subscriber_content": True},
]


def test_uses_shared_supported_extensions():
    assert cp.SUPPORTED_EXTENSIONS is pc.SUPPORTED_EXTENSIONS
    assert not hasattr(cp, "PIPELINE_SUPPORTED_EXTENSIONS")


@pytest.fixture
def run_main(s3, monkeypatch, tmp_path):
    """Runs main() against moto S3 with a fake scraper and downloader."""
    config = {
        "confluence_urls": ["https://confluence.example.org/page"],
        "s3_bucket_name": BUCKET,
        "aws_region": "us-west-2",
        "env_file": str(tmp_path / "names.env"),
    }

    def run(skip_existing=False):
        config["skip_existing_s3_files"] = skip_existing
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.safe_dump(config))

        def fake_open(path, *args, **kwargs):
            # config.yaml is gitignored, so point main() at a test copy
            if str(path).endswith("config.yaml"):
                path = config_file
            return open(path, *args, **kwargs)

        downloaded = []

        def fake_download(url, output_path):
            downloaded.append(url)
            with open(output_path, "wb") as f:
                f.write(b"data")
            return output_path

        scraper = type("Scraper", (), {"__init__": lambda self, **kw: None, "scrape_assets": lambda self: [dict(a) for a in ASSETS]})
        monkeypatch.chdir(tmp_path)
        # Shadows open() inside confluence_processor only
        monkeypatch.setattr(cp, "open", fake_open, raising=False)
        monkeypatch.setattr(cp, "ConfluenceScraper", scraper)
        monkeypatch.setattr(cp, "download_file", fake_download)
        return cp.main(), downloaded

    return run


def test_only_pipeline_supported_files_are_downloaded(run_main, s3):
    summary, downloaded = run_main()

    assert downloaded == [
        "https://example.org/files/talk.mp4",
        "https://example.org/files/slides.pdf",
    ]
    assert summary["unsupported_skipped"] == 2
    assert summary["drive_folders_deferred"] == 1
    assert summary["uploaded"] == 2
    assert s3.head_object(Bucket=BUCKET, Key="talk.mp4")["Metadata"]["member-content"] == "true"


def test_skip_existing_recognises_pipeline_chunked_video(run_main, s3):
    # The chunker deleted the original, leaving only its chunks
    s3.put_object(Bucket=BUCKET, Key=pc.video_chunk_key("talk.mp4", 1), Body=b"c")

    summary, downloaded = run_main(skip_existing=True)

    assert downloaded == ["https://example.org/files/slides.pdf"]
    assert summary["already_in_s3"] == 1
