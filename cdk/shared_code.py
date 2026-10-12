"""
Bundles src/ingest/shared/processing_config.py into the ingest Lambdas, and
exposes it to the CDK code so the state machine branches on the same names.
"""

import hashlib
import os
import shutil
import sys

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as lambda_

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_DIR = os.path.join(REPO_ROOT, "src", "ingest", "shared")

if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)
import processing_config  # noqa: E402,F401


def _shared_files():
    return sorted(
        os.path.join(SHARED_DIR, f) for f in os.listdir(SHARED_DIR) if f.endswith(".py")
    )


def shared_hash() -> str:
    """
    Content hash of the shared modules. Docker build contexts only hash their
    path, so image assets pass this as extra_hash to rebuild on changes.
    """
    digest = hashlib.sha256()
    for path in _shared_files():
        digest.update(os.path.basename(path).encode())
        with open(path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


@jsii.implements(ILocalBundling)
class _CopyWithShared:
    """Copies the Lambda's own directory plus the shared modules, no Docker."""

    def __init__(self, source_dir: str):
        self._source_dir = source_dir

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        shutil.copytree(
            self._source_dir,
            output_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "Dockerfile"),
        )
        for path in _shared_files():
            shutil.copy2(path, output_dir)
        return True


def code_with_shared(source_dir: str) -> lambda_.Code:
    """Lambda zip asset of `source_dir` with the shared modules added."""
    return lambda_.Code.from_asset(
        source_dir,
        # Hash the bundled output so edits to the shared modules redeploy
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=BundlingOptions(
            # Required by the API but unused: local bundling always succeeds
            image=DockerImage.from_registry("unused"),
            local=_CopyWithShared(source_dir),
        ),
    )
