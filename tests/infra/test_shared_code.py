import os
import shutil

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from cdk import shared_code

SHARED_FILE = "processing_config.py"


def _bundle(tmp_path, monkeypatch, source_dir):
    monkeypatch.chdir(shared_code.REPO_ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(app, "TestStack")
    lambda_.Function(
        stack,
        "Fn",
        runtime=lambda_.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=shared_code.code_with_shared(source_dir),
    )
    assembly = app.synth()
    [asset_dir] = [d for d in os.listdir(assembly.directory) if d.startswith("asset.")]
    return os.path.join(assembly.directory, asset_dir)


def test_routing_bundle_contains_handler_and_shared_module(tmp_path, monkeypatch):
    asset_dir = _bundle(tmp_path, monkeypatch, "src/ingest/routing")
    files = set(os.listdir(asset_dir))
    assert {"routing_lambda.py", SHARED_FILE} <= files


def test_video_chunker_bundle_excludes_dockerfile(tmp_path, monkeypatch):
    asset_dir = _bundle(tmp_path, monkeypatch, "src/ingest/video_chunker")
    files = set(os.listdir(asset_dir))
    assert {"check_file_size.py", SHARED_FILE} <= files
    assert "Dockerfile" not in files


def test_shared_change_changes_asset_hash(tmp_path, monkeypatch):
    # Source-hashed assets would ignore the shared module; OUTPUT hashing must not
    src = tmp_path / "fn"
    shared = tmp_path / "shared"
    src.mkdir()
    shared.mkdir()
    (src / "index.py").write_text("x = 1\n")
    shutil.copy(os.path.join(shared_code.SHARED_DIR, SHARED_FILE), shared / SHARED_FILE)
    monkeypatch.setattr(shared_code, "SHARED_DIR", str(shared))

    first = os.path.basename(_bundle(tmp_path / "a", monkeypatch, str(src)))
    same = os.path.basename(_bundle(tmp_path / "a2", monkeypatch, str(src)))
    (shared / SHARED_FILE).write_text("# changed\n")
    second = os.path.basename(_bundle(tmp_path / "b", monkeypatch, str(src)))

    assert first == same
    assert first != second


def test_shared_hash_tracks_content(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / SHARED_FILE).write_text("A = 1\n")
    monkeypatch.setattr(shared_code, "SHARED_DIR", str(shared))
    before = shared_code.shared_hash()
    (shared / SHARED_FILE).write_text("A = 2\n")
    assert shared_code.shared_hash() != before
