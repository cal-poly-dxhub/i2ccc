"""
Characterization tests for the content-sync collector (run_collector.main).

The whole collector runs against fakes at the library boundary: moto for S3,
`responses` for HTTP, a fake Drive v3 client, a fake pymediainfo, and fake
soffice/ffmpeg binaries on PATH. The test asserts on what ends up in the
bucket (key, metadata, content hash) plus the run summary, compared with a
golden file recorded from the current code. Quirks are pinned on purpose;
change the golden only for intentional behavior changes.

All fixture data is made up; see fixtures/confluence_page.html and
fixtures/drive_tree.json for which case each row or file covers.

Regenerate goldens:  UPDATE_GOLDEN=1 pytest tests/ingest_utils/test_collector_golden.py
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import boto3
import pytest
import responses
import yaml
from google.auth.credentials import AnonymousCredentials
from moto import mock_aws

import collector_fakes as fakes

REPO = Path(__file__).resolve().parents[2]
COLLECTOR_SRC = REPO / "ingest_utils" / "confluence_processor"
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = Path(__file__).parent / "golden"

BUCKET = "collector-test-bucket"
REGION = "us-west-2"
RUN_ID = "test-run"
PAGE_URL = "https://wiki.example.edu/display/EX/Example+Program+Calendar"
# A configured page with no calendar table
EMPTY_PAGE_URL = "https://wiki.example.edu/display/EX/Empty+Page"
EMPTY_PAGE = b"<html><body><table><tr><th>Other</th></tr></table></body></html>"

COLLECTOR_MODULES = [
    "run_collector",
    "confluence_processor",
    "confluence_scraper",
    "confluence_event_descriptions_to_s3",
    "google_drive_processor",
    "s3_uploader",
]

# Direct downloads linked from confluence_page.html
DIRECT_FILES = {
    "https://files.example.edu/webinars/direct-webinar.mp4": b"direct:direct-webinar.mp4\n",
    "https://files.example.edu/webinars/direct-webinar.vtt": b"direct:direct-webinar.vtt\n",
    "https://files.example.edu/webinars/direct-webinar.m4a": b"direct:direct-webinar.m4a\n",
    "https://files.example.edu/briefings/audio-only.m4a": b"direct:audio-only.m4a\n",
    "https://files.example.edu/members/strategy-slides.pdf?version=2": b"direct:strategy-slides.pdf\n",
    # Fallback for the Slides file listed under export_too_large in drive_tree.json
    "https://docs.google.com/presentation/d/f-slides-big/export/pptx": b"direct-export:f-slides-big.pptx\n",
}
MISSING_FILES = ["https://files.example.edu/missing/gone.pdf"]

# Objects already in the bucket for the skip-existing scenario
PRESEEDED_KEYS = [
    "direct-webinar.mp4",                  # Confluence direct asset
    "Example_Webinar__Direct_Media.txt",   # event description
    "Town Hall Transcript.pdf",            # Drive Doc, predicted converted name
    "Long Session_chunk_001.mp4",          # Drive video, predicted chunk name
    "quarantine/briefing.pdf",             # quarantined copy counts as existing
]
PRESEEDED_BODY = b"preexisting\n"


def _config(skip_existing: bool) -> dict:
    return {
        "s3_bucket_name": BUCKET,
        "aws_region": REGION,
        "confluence_urls": [PAGE_URL, EMPTY_PAGE_URL],
        "google_drive_download_dir": "google_drive_downloads",
        "skip_existing_s3_files": skip_existing,
    }


@pytest.fixture
def tree():
    return fakes.load_drive_tree()


@pytest.fixture
def collector_dir(tmp_path, monkeypatch):
    """
    Copies the collector into the same layout the Docker image uses
    (config.yaml two levels above the scripts) and makes it importable.
    """
    app = tmp_path / "app"
    target = app / "ingest_utils" / "confluence_processor"
    shutil.copytree(
        COLLECTOR_SRC,
        target,
        ignore=shutil.ignore_patterns(
            "*downloads*", "*.csv", "names.env*", "service-credentials.json",
            "run_summary.json", "__pycache__",
        ),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(target))
    for name in COLLECTOR_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield app
    for name in COLLECTOR_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch, tree):
    for var in (
        "AWS_PROFILE", "AWS_SESSION_TOKEN", "CONFLUENCE_API", "GOOGLE_API_KEY",
        "GOOGLE_DRIVE_CREDENTIALS", "GOOGLE_SERVICE_ACCOUNT_JSON", "RUN_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("RUN_ID", RUN_ID)
    bin_dir = tmp_path / "bin"
    fakes.install_fake_binaries(bin_dir, convert_fail_ids=tree.get("convert_fails", []))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return monkeypatch


@pytest.fixture
def fake_drive(monkeypatch, tree):
    import googleapiclient.discovery
    import pymediainfo
    from google.oauth2 import service_account

    drive = fakes.FakeDrive(tree)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: drive)
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        classmethod(lambda cls, info, **kw: AnonymousCredentials()),
    )
    monkeypatch.setattr(pymediainfo.MediaInfo, "parse", fakes.FakeMediaInfo(tree["durations_ms"]).parse)
    return drive


@pytest.fixture
def s3(isolated_env):
    # moto patches `requests` through its own responses mock, so it must start
    # before the `http` fixture or it shadows the fixture's registrations
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield client


@pytest.fixture
def http(s3):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(PAGE_URL, body=(FIXTURES / "confluence_page.html").read_bytes(), content_type="text/html")
        rsps.get(EMPTY_PAGE_URL, body=EMPTY_PAGE, content_type="text/html")
        for url, body in DIRECT_FILES.items():
            rsps.get(url, body=body)
        for url in MISSING_FILES:
            rsps.get(url, status=404)
        yield rsps


def _snapshot_bucket(s3) -> list:
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith("sync-runs/"):
                continue
            head = s3.head_object(Bucket=BUCKET, Key=key)
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            objects.append({
                "key": key,
                "metadata": head.get("Metadata", {}),
                "sha256": hashlib.sha256(body).hexdigest()[:16],
                "preexisting": body == PRESEEDED_BODY,
            })
    return sorted(objects, key=lambda o: o["key"])


def _strip_durations(value):
    if isinstance(value, dict):
        return {k: _strip_durations(v) for k, v in value.items() if k != "duration_seconds"}
    if isinstance(value, list):
        return [_strip_durations(v) for v in value]
    return value


def _run(app, s3, drive, http, config, preseed=()):
    (app / "config.yaml").write_text(yaml.safe_dump(config))
    for key in preseed:
        s3.put_object(Bucket=BUCKET, Key=key, Body=PRESEEDED_BODY)

    import run_collector

    exit_code = run_collector.main()

    prefix = f"sync-runs/{RUN_ID}"
    summary = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{prefix}/run_summary.json")["Body"].read())
    csv_body = s3.get_object(Bucket=BUCKET, Key=f"{prefix}/confluence_asset_links.csv")["Body"].read()
    return {
        "exit_code": exit_code,
        "run_summary": _strip_durations(summary),
        "asset_csv": csv_body.decode().splitlines(),
        "objects": _snapshot_bucket(s3),
        # What was fetched, so skipping before download is pinned too
        "drive_downloads": sorted(
            file_id for call, file_id in drive.calls if call in ("files.get_media", "files.export")
        ),
        "http_requests": sorted(
            (c.request.url, c.request.headers.get("Authorization")) for c in http.calls
        ),
    }


def _check_golden(name: str, actual: dict) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.getenv("UPDATE_GOLDEN") or not path.exists():
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(rendered)
        if not os.getenv("UPDATE_GOLDEN"):
            pytest.fail(f"Golden {path.name} did not exist; recorded it. Review and re-run.")
        return
    expected = json.loads(path.read_text())
    assert json.loads(rendered) == expected, (
        f"Collector output differs from {path.name}. If the change is intended, "
        "re-run with UPDATE_GOLDEN=1 and review the diff."
    )


def test_collector_fresh_bucket(collector_dir, isolated_env, fake_drive, s3, http):
    # Cloud-job style credentials: service account JSON in the environment,
    # and a placeholder API key that the collector should ignore
    isolated_env.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"type": "service_account"}))
    isolated_env.setenv("GOOGLE_API_KEY", "placeholder-not-a-key")
    result = _run(collector_dir, s3, fake_drive, http, _config(skip_existing=False))
    _check_golden("collector_fresh_bucket", result)


def test_collector_skip_existing(collector_dir, isolated_env, fake_drive, s3, http, tmp_path):
    # Local-run style credentials: service account file path, a real-looking
    # API key, and a Confluence token
    creds = tmp_path / "service-account.json"
    creds.write_text(json.dumps({"type": "service_account"}))
    isolated_env.setenv("GOOGLE_DRIVE_CREDENTIALS", str(creds))
    isolated_env.setenv("GOOGLE_API_KEY", "AIzaFakeKeyForTests")
    isolated_env.setenv("CONFLUENCE_API", "test-confluence-token")
    result = _run(collector_dir, s3, fake_drive, http, _config(skip_existing=True), preseed=PRESEEDED_KEYS)
    _check_golden("collector_skip_existing", result)
